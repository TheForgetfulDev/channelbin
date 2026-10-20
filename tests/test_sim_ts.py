"""The stream engine's MPEG-TS layer: a loop cut into an endless stream that is seamless across
every pass, and a checker that sees when one is not (DESIGN-simulator.md §3.3, §9.1;
dev/changelog/1219).

The 0b measurement found that ffmpeg's own `-stream_loop` from a TS file drops each pass's
first keyframe silently, and that ChannelBin's timeline scan could not see the hole. These
tests hold the simulator to the opposite: every frame present and evenly spaced across the
seam, continuity counters unbroken, a PCR at least every 100 ms, and a real decode with no
error.
"""
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sim import ts  # noqa: E402

from tests.support.sim_streams import library  # noqa: E402

FRAME90 = 3003      # one frame at 29.97 fps, 90 kHz ticks
AAC90 = 1920        # one 1,024-sample AAC frame at 48 kHz


class LoopTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.loop = library().load('test_tiny')
        cls.tmp = tempfile.mkdtemp(prefix='sim-ts-')

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def video_pid(self):
        return self.loop.video_pid

    def audio_pid(self):
        return next(p for p, t in self.loop.streams.items() if t in ts.AUDIO_TYPES)

    def test_the_loop_is_indexed_from_its_recorded_length(self):
        lp = self.loop
        self.assertEqual(lp.L90, 640 * FRAME90)
        self.assertEqual(len(lp.cuts), 10)
        self.assertEqual(lp.gop90, 64 * FRAME90)
        self.assertEqual(lp.streams[lp.video_pid], 0x1B)

    def test_three_passes_are_seamless(self):
        """Every frame present at an even spacing across both seams, nothing else wrong."""
        lp = self.loop
        k = 27_000_000
        data = lp.cut(k * lp.n, (k + 3) * lp.n, 123_456_789)
        r = ts.check(data)
        self.assertTrue(r.clean, r.problems())
        self.assertEqual(r.dts_steps[self.video_pid()], {FRAME90: 3 * 640 - 1})
        self.assertLessEqual(r.pcr_max_gap, ts.PCR_MAX_GAP_SECONDS)
        audio_steps = r.dts_steps[self.audio_pid()]
        # Audio PES carry several frames each, so the steps are whole multiples of a frame.
        self.assertTrue(all(step % AAC90 == 0 for step in audio_steps), audio_steps)

    def test_a_real_decode_of_three_passes_has_no_error_and_every_frame(self):
        if not shutil.which('ffprobe'):
            self.skipTest('ffprobe is not installed')
        lp = self.loop
        path = os.path.join(self.tmp, 'three.ts')
        with open(path, 'wb') as f:
            f.write(lp.cut(5 * lp.n, 8 * lp.n, 0))
        errors = os.path.join(self.tmp, 'decode.log')
        with open(errors, 'w') as err:
            rc = subprocess.run(['ffmpeg', '-hide_banner', '-v', 'error', '-i', path, '-f',
                                 'null', '-'], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                stderr=err, timeout=120).returncode
        self.assertEqual(rc, 0)
        with open(errors) as f:
            self.assertEqual(f.read().strip(), '')
        out = subprocess.run(['ffprobe', '-v', 'error', '-select_streams', 'v', '-show_entries',
                              'frame=pts', '-of', 'csv=p=0', path], capture_output=True,
                             text=True, stdin=subprocess.DEVNULL, timeout=120).stdout.split()
        pts = sorted(int(v.split(',')[0]) for v in out)
        self.assertEqual(len(pts), 3 * 640)
        self.assertEqual({b - a for a, b in zip(pts, pts[1:])}, {FRAME90})

    def test_the_stream_stays_seamless_across_the_33_bit_wrap(self):
        lp = self.loop
        base = ts.PTS_WRAP - lp.pcr0 - 5 * ts.PTS_HZ
        r = ts.check(lp.cut(0, 2 * lp.n, base))
        self.assertTrue(r.clean, r.problems())
        self.assertEqual(r.dts_steps[self.video_pid()], {FRAME90: 2 * 640 - 1})

    def test_the_same_range_always_gives_the_same_bytes(self):
        lp = self.loop
        g0 = 41 * lp.n + 777
        self.assertEqual(lp.cut(g0, g0 + 5000, 99), lp.cut(g0, g0 + 5000, 99))
        joined = lp.cut(g0, g0 + 2000, 99) + lp.cut(g0 + 2000, g0 + 5000, 99)
        self.assertEqual(joined, lp.cut(g0, g0 + 5000, 99))

    def test_send_times_and_packet_counts_agree_across_a_seam(self):
        lp = self.loop
        for g in (0, 1, lp.n - 1, lp.n, lp.n + 3, 9 * lp.n + 100):
            s = lp.send_time(g)
            self.assertGreaterEqual(lp.packets_through(s), g + 1, g)
            self.assertLessEqual(lp.packets_through(s - 1), g + 1, g)
        sends = [lp.send_time(g) for g in range(lp.n - 50, lp.n + 50)]
        self.assertEqual(sends, sorted(sends))

    def test_a_cut_point_starts_with_the_tables_and_a_keyframe(self):
        """An HLS segment starts at a cut point, so a client joining there can decode."""
        lp = self.loop
        for j, _pts in lp.cuts:
            first = lp.data[j * ts.TS:(j + 1) * ts.TS]
            self.assertEqual(((first[1] & 0x1F) << 8) | first[2], 0, 'PAT first')

    def test_the_checker_sees_a_lost_packet_and_a_pcr_gap(self):
        """Negative control: the checker the other tests trust does report damage."""
        lp = self.loop
        data = bytearray(lp.cut(0, 2 * lp.n, 0))
        vpid = self.video_pid()
        for i in range(500, 600):
            o = i * ts.TS
            if ((data[o + 1] & 0x1F) << 8) | data[o + 2] == vpid:
                del data[o:o + ts.TS]
                break
        self.assertTrue(ts.check(bytes(data)).cc_errors.get(vpid))
        pcrs = [i for i, *_ in lp.pcr_sites[2:6]]
        gap = bytes(lp.cut(0, pcrs[0] + 1, 0) + lp.cut(pcrs[-1], lp.n, 0))
        self.assertGreater(ts.check(gap).pcr_max_gap, ts.PCR_MAX_GAP_SECONDS)

    def test_the_checker_reads_a_stream_fed_in_odd_pieces_as_one(self):
        data = self.loop.cut(3 * self.loop.n, 5 * self.loop.n, 0)
        c = ts.Checker()
        for i in range(0, len(data), 100_003):
            c.feed(data[i:i + 100_003])
        self.assertEqual(c.report, ts.check(data))

    def test_bytes_that_are_not_a_loop_are_refused(self):
        with self.assertRaises(ts.TsError):
            ts.Loop(b'\x47' + b'\x00' * 186, 100)
        with self.assertRaises(ts.TsError):
            ts.Loop(b'\x00' * ts.TS * 4, 100)


if __name__ == '__main__':
    unittest.main()
