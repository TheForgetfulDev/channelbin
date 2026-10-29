"""Each stream fault as ffmpeg meets it: the recorder's own argv on the ffmpeg this app ships,
pointed at the simulator, showing the signature the changelog measured on a real feed or a
real capture (DESIGN-simulator.md §3.11, §7).

Characterization tests of the simulator against those measurements, not guards for a past
defect: what ChannelBin does about each fault is the capture tier's. The checks that need
minutes of real time - a 256 KiB write cadence, a replay after a read timeout - are
dev/tools/sim_stream_signatures.py's, reported in dev/changelog/1221.
"""
import copy
import datetime as dt
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.config import _DEFAULTS  # noqa: E402
from app.proc_utils import build_capture_cmd  # noqa: E402
from sim import serve_for_test  # noqa: E402

from tests.support.sim_streams import library, live_ids, origin, stream_world  # noqa: E402

TIMED_OUT = 'Error opening input: Connection timed out'


def at(seconds: float) -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc).replace(tzinfo=None) + dt.timedelta(seconds=seconds)


class _Capture(unittest.TestCase):
    backend = {}
    extra = ()

    def setUp(self):
        if not shutil.which('ffmpeg') or not shutil.which('ffprobe'):
            self.skipTest('ffmpeg and ffprobe are needed')
        self.lib = library(*self.extra)
        self.world = stream_world(**self.backend)
        self.sim = serve_for_test(self, self.world, content=self.lib)
        self.ids = live_ids(self.world)
        self.sid = self.ids[0]
        self.url = self.sim.stream_urls('cove-tv', 'reeduser')[self.sid]
        self.label = f'cove:{self.sid}'
        self.dir = tempfile.mkdtemp(prefix='sim-signature-')
        self.addCleanup(shutil.rmtree, self.dir, True)

    def argv(self, url, seconds=0, read_timeout=1, keep_video_track=True):
        cfg = copy.deepcopy(_DEFAULTS)
        cfg['ffmpeg']['read_timeout_seconds'] = read_timeout
        out = os.path.join(self.dir, f'capture-{time.monotonic_ns()}.ts')
        return build_capture_cmd(cfg, url, out, seconds, pace_realtime=False,
                                 keep_video_track=keep_video_track), out

    def capture(self, url=None, seconds=0, *, read_timeout=1, keep_video_track=True,
                stop_after=30.0, grace=5.0, argv=None):
        """Run a capture to its end or for `stop_after` seconds, whichever comes first.
        Returns (exit code, stderr, output bytes, wall seconds). stderr goes to a file, never
        a pipe (dev/changelog/430)."""
        if argv is None:
            argv, out = self.argv(url or self.url, seconds, read_timeout, keep_video_track)
        else:
            out = argv[-1]
        err_path = os.path.join(self.dir, f'stderr-{time.monotonic_ns()}.txt')
        started = time.monotonic()
        with open(err_path, 'wb') as err:
            proc = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                    stderr=err)
            try:
                rc = proc.wait(stop_after)
            except subprocess.TimeoutExpired:
                proc.terminate()
                try:
                    rc = proc.wait(grace)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    rc = proc.wait()
        with open(err_path, encoding='utf-8', errors='replace') as f:
            text = f.read()
        size = os.path.getsize(out) if os.path.exists(out) else 0
        return rc, text, size, time.monotonic() - started

    def progress(self, text) -> list:
        """(frame, seconds) from each progress line."""
        out = []
        for frame, h, m, s in re.findall(r'frame=\s*(\d+).*?time=(\d+):(\d+):([\d.]+)', text):
            out.append((int(frame), int(h) * 3600 + int(m) * 60 + float(s)))
        return out


class OpenSignatureTests(_Capture):
    def test_405_at_open_ends_the_capture_with_nothing(self):
        """dev/changelog/436: `HTTP error 405 Method Not Allowed` at input open, exit 8, zero
        bytes, on every attempt."""
        self.sim.inject('refuse_at_open', self.label)
        for _ in range(2):
            rc, text, size, _ = self.capture(seconds=4)
            self.assertEqual((rc, size), (8, 0))
            self.assertIn('HTTP error 405 Method Not Allowed', text)
            self.assertIn('Server returned 4XX Client Error, but not one of 40{0,1,3,4}', text)

    def test_a_first_byte_slower_than_the_read_timeout_fails_the_open(self):
        """dev/changelog/958: -rw_timeout bounds the first read, and ffmpeg does not reconnect
        out of a timed-out open."""
        # ffmpeg gives up about 0.1 s past the read timeout. Each delay sits seconds from that
        # edge, because a loaded box eats into the margin from both sides: the simulator
        # answers late, and a starved ffmpeg can wake to an answer that came after its
        # deadline. A 0.5 s delay against a 1 s timeout went red in a full suite run
        # (dev/changelog/1328).
        f = self.sim.inject('slow_first_byte', self.label, seconds=6.0)
        rc, text, size, _ = self.capture(seconds=3, read_timeout=3)
        self.assertNotEqual(rc, 0)
        self.assertEqual(size, 0)
        self.assertIn(TIMED_OUT, text)
        self.sim.clear(f)
        self.sim.inject('slow_first_byte', self.label, seconds=0.5)
        rc, text, size, _ = self.capture(seconds=3, read_timeout=3)
        self.assertEqual(rc, 0)
        self.assertGreater(size, 0)

    def test_no_answer_at_open_times_the_open_out(self):
        """3.7% of prod's health checks: the server never answered."""
        self.sim.inject('no_answer', self.label)
        rc, text, size, _ = self.capture(seconds=3)
        self.assertNotEqual(rc, 0)
        self.assertEqual(size, 0)
        self.assertIn(TIMED_OUT, text)

    def test_the_placeholder_drains_in_seconds_and_exits_0(self):
        """dev/changelog/957 and dev recording 2's stderr: 600 s of clip in about 5 to 7 s,
        then two reconnects at its last byte that read End of file, then an I/O error and
        exit 0."""
        self.sim.inject('placeholder', self.label, profile='test_tiny_black')
        rc, text, size, wall = self.capture()
        self.assertEqual(rc, 0)
        length = next(r.bytes for r in self.sim.ledger.entries(endpoint='stream')
                      if r.status == 200 and 'the placeholder clip ended' in r.note)
        # Answered as a file, the clip is seekable, so ffmpeg reads its tail at open for the
        # duration and logs two reconnects there too; the real tail kept starts after it.
        after_open = text.split('Press [q]')[1]
        self.assertIn('Duration: 00:10:00.0', text)
        self.assertEqual(re.findall(r'Will reconnect at (\d+) in (\d) second\(s\), '
                                    r'error=End of file', after_open),
                         [(str(length), '0'), (str(length), '1')])
        self.assertIn('Error during demuxing: Input/output error', after_open)
        self.assertAlmostEqual(self.progress(text)[-1][1], 600.0, delta=0.5)
        self.assertGreater(wall, 4.0)
        self.assertLess(wall, 12.0)


class StreamSignatureTests(_Capture):
    def test_a_silent_socket_is_repaired_by_a_reconnect_inside_the_capture(self):
        """dev/changelog/958: `Will reconnect at N in 0 second(s), error=Connection timed out`,
        and the capture carries on."""
        self.sim.inject('silent_socket', self.label, after_bytes=300_000, times=1)
        rc, text, size, _ = self.capture(seconds=6)
        sent = 300_000 // 188 * 188
        self.assertEqual(rc, 0)
        self.assertIn(f'Will reconnect at {sent} in 0 second(s), error=Connection timed out',
                      text)
        self.assertGreater(size, sent)

    def test_a_connection_closed_mid_stream_reads_as_prods_capture_tails_did(self):
        """Prod's DIAGNOSTICS tails (dev/changelog/430): `Stream ends prematurely` then
        `Will reconnect ... error=Input/output error`."""
        self.sim.inject('close_mid_stream', self.label, after_bytes=300_000, times=1)
        rc, text, _, _ = self.capture(seconds=6)
        sent = 300_000 // 188 * 188
        self.assertEqual(rc, 0)
        self.assertIn(f'Stream ends prematurely at {sent}, should be 18446744073709551615',
                      text)
        self.assertIn(f'Will reconnect at {sent} in 0 second(s), error=Input/output error', text)

    def test_hls_with_the_raw_stream_reconnect_flags_spins_on_the_playlist(self):
        """dev/changelog/195: a playlist answers EOF on every GET, so -reconnect_at_eof re-fetches
        it in a tight loop and nothing is written. The shipped argv leaves the flags off HLS."""
        hls = f'{origin(self.url)}/live/fernuser/fernpass1/{self.sid}.m3u8'
        ts_argv, out = self.argv(self.url, 6)
        self.assertIn('-reconnect_at_eof', ts_argv)
        forced = [hls if a == self.url else a for a in ts_argv]
        rc, text, size, _ = self.capture(argv=forced, stop_after=3)
        self.assertEqual(size, 0)
        self.assertGreater(text.count('error=End of file'), 20)
        self.assertIn("Skip ('#EXTM3U')", text)
        rc, text, size, _ = self.capture(hls, 3)
        self.assertEqual(rc, 0)
        self.assertGreater(size, 0)


class VideoOutSignatureTests(_Capture):
    """A 20 s burst puts a window in the channel's past where the capture reads it at once."""
    backend = {'burst_seconds': 20.0}

    def test_video_stopping_freezes_frame_and_time_on_7_1(self):
        """dev/changelog/1209, ffmpeg 7.1: frame= and time= both stop where the video did. The
        file growing in 256 KiB steps needs minutes; the dev runner checks that."""
        self.sim.inject('video_stops', self.label, start=at(-10), end=at(60))
        rc, text, _, _ = self.capture(seconds=30, stop_after=4)
        last = self.progress(text)[-1]
        # 10 s of video before the stop, plus up to one keyframe interval (2.14 s on this loop)
        # because a connection opens at the cut point before its burst.
        self.assertGreaterEqual(last[1], 9.5)
        self.assertLess(last[1], 12.8)
        self.assertLess(last[0], 12.8 * 30000 / 1001)
        tail = [p for p in self.progress(text)][-3:]
        self.assertEqual(len({p for p in tail}), 1, 'progress does not move')

    def test_connecting_while_the_video_is_out_drops_or_keeps_the_track(self):
        """dev/changelog/1210: under ffmpeg's default selection the video track has no codec
        parameters and is dropped; with -map 0:v? it is kept, with frame=0."""
        self.sim.inject('video_stops', self.label, start=at(-40), end=at(60))
        _, text, _, _ = self.capture(seconds=4, keep_video_track=False, stop_after=3, grace=1)
        self.assertIn('Could not find codec parameters for stream 0 (Video: h264', text)
        self.assertIn('unspecified size', text)
        mapping = text.split('Stream mapping:')[1].split('Press [q]')[0]
        self.assertNotIn('Stream #0:0 ->', mapping)
        _, text, _, _ = self.capture(seconds=4, keep_video_track=True, stop_after=3, grace=1)
        mapping = text.split('Stream mapping:')[1].split('Press [q]')[0]
        self.assertIn('Stream #0:0 -> #0:0 (copy)', mapping)
        frames = re.findall(r'frame=\s*(\d+)', text)
        self.assertTrue(frames)
        self.assertEqual(set(frames), {'0'}, 'the outage shows as frame=0 from the start')


if __name__ == '__main__':
    unittest.main()
