"""The answer key (sim/answer_key.py, DESIGN-simulator.md §3.5, §3.12): what a recording holds,
worked out from its files and the simulator's ledger, and checked against figures someone else
reported.

Characterization tests of a new checker, not guards for a past defect. The recordings are made
two ways. Hand-built ones are cut straight from a stream's bytes, which still carry the
channel's own timestamps, so where the checker places each frame can be compared with an
independent reading of those timestamps. Real ones are the recorder's own argv on the ffmpeg
this app ships, which rewrites the timestamps - the reason the checker reads content rather
than time. Each test runs a few seconds of real time.
"""
import copy
import datetime as dt
import os
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
from sim import answer_key as ak  # noqa: E402
from sim import ts as sim_ts  # noqa: E402

from tests.support.sim_streams import (VIDEO_PID, library, live_ids, read_stream,  # noqa: E402
                                       stream_world, whole_packets)

FRAME = 1001 / 30000        # one frame of the test loop


def at(seconds: float) -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc).replace(tzinfo=None) + dt.timedelta(seconds=seconds)


def video_pes(data: bytes) -> list:
    """(packet number, PTS) of every video PES start in a stretch of stream, in the order
    sent - decode order."""
    out = []
    for g in range(len(data) // sim_ts.TS):
        o = g * sim_ts.TS
        if not data[o + 1] & 0x40 or ((data[o + 1] & 0x1F) << 8) | data[o + 2] != VIDEO_PID:
            continue
        p = o + 4
        if (data[o + 3] >> 4) & 2:
            p += 1 + data[p]
        if data[p:p + 3] == b'\x00\x00\x01' and data[p + 7] >> 6 & 2:
            out.append((g, sim_ts._read_ts(data, p + 9)))
    return out


class _AnswerKey(unittest.TestCase):
    def setUp(self):
        if not shutil.which('ffmpeg') or not shutil.which('ffprobe'):
            self.skipTest('ffmpeg and ffprobe are needed')
        self.lib = library()
        self.world = stream_world()
        self.sim = serve_for_test(self, self.world, content=self.lib)
        self.sid = live_ids(self.world)[0]
        self.url = self.sim.stream_urls('cove-tv', 'reeduser')[self.sid]
        self.label = f'cove:{self.sid}'
        self.dir = tempfile.mkdtemp(prefix='sim-answer-key-')
        self.addCleanup(shutil.rmtree, self.dir, True)

    def capture(self, seconds: int, read_timeout: int = 1) -> str:
        """The recorder's argv for one segment, run to its end. stderr goes to a file, never a
        pipe (dev/changelog/430)."""
        cfg = copy.deepcopy(_DEFAULTS)
        cfg['ffmpeg']['read_timeout_seconds'] = read_timeout
        out = os.path.join(self.dir, f'segment-{time.monotonic_ns()}.ts')
        argv = build_capture_cmd(cfg, self.url, out, seconds, pace_realtime=False,
                                 keep_video_track=True)
        with open(out + '.stderr', 'wb') as err:
            proc = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                    stderr=err)
            try:
                proc.wait(seconds + 30)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()
                self.fail('the capture outlived its own -t')
        return out

    def channel_seconds(self, pts: int, near: float) -> float:
        """A wire PTS as channel time, read from the timestamp alone: the channel's clock is its
        PTS less its base, unwrapped to the 33-bit pass nearest `near`."""
        base = self.sim.headend.timing(self.label, 'test_tiny').pts_offset
        c = (pts - base) % sim_ts.PTS_WRAP
        wraps = round((near * sim_ts.PTS_HZ - c) / sim_ts.PTS_WRAP)
        return (c + wraps * sim_ts.PTS_WRAP) / sim_ts.PTS_HZ

    def settled(self):
        """Wait until every connection's ledger row is closed. A connection the client has
        left is noticed at the server's next write, so moving the clock before then would send
        the jump into a socket nobody reads and stretch that row's `offered`."""
        end = time.monotonic() + 3
        while any(r.open for r in self.sim.ledger.entries(endpoint='stream')):
            self.assertLess(time.monotonic(), end, 'a connection stayed open')
            time.sleep(0.05)

    def assertAnswer(self, answer, condition, msg=''):
        self.assertTrue(condition, f'{msg}\n{answer.describe()}')


class HandBuiltTests(_AnswerKey):
    def test_frames_are_placed_where_their_own_timestamps_say(self):
        """A segment cut from one connection's bytes with 30 frames taken out of the middle:
        the checker places both halves at the channel time their wire PTS reads (to the
        microsecond), and the hole is the 30 frames, sent but not in the recording."""
        got = read_stream(self.url, 2.5)
        data = whole_packets(got['data'])
        pes = video_pes(data)
        self.assertGreater(len(pes), 90)
        a, b = 40, 70
        cut = data[:pes[a][0] * sim_ts.TS] + data[pes[b][0] * sim_ts.TS:]
        path = os.path.join(self.dir, 'hand-built.ts')
        with open(path, 'wb') as f:
            f.write(cut)
        answer = ak.check(self.sim, [path], min_gap=0.25)
        self.assertAnswer(answer, not answer.problems, 'no problems')
        row = self.sim.ledger.entries(endpoint='stream', status=200)[0]
        near = row.offered[0]
        first = [self.channel_seconds(p, near) for _, p in pes[:a]]
        second = [self.channel_seconds(p, near) for _, p in pes[b:-1]]
        runs = answer.stretches
        self.assertEqual([s.row for s in runs], [row.seq, row.seq], answer.describe())
        self.assertAlmostEqual(runs[0].start, min(first), places=3)
        self.assertAlmostEqual(runs[1].start, min(second), places=3)
        self.assertEqual(len(answer.lost), 1, answer.describe())
        lost = answer.lost[0]
        self.assertEqual(lost.reason, ak.REASON_NOT_RECORDED)
        self.assertEqual(lost.rows, (row.seq,))
        # B-frames present out of decode order, so either edge can move by a frame or two.
        self.assertAlmostEqual(lost.seconds, (b - a) * FRAME, delta=4 * FRAME)
        self.assertAlmostEqual(answer.repeated_seconds, 0.0, places=6)

    def test_a_recording_that_ends_inside_the_burst_still_belongs_to_its_connection(self):
        """Guards dev/docs/BUGS.md 2026-10-19 (a connection answered after the extent's last
        frame is shown was dropped from the loss explanation): a segment holding only the first
        2 s of a connect burst - content sent from before the connection was answered, so the
        extent ends about a second before the row's `at` - with ten frames cut out of its
        middle. The hole is sent-but-not-recorded on that row, never "no connection was
        sending"."""
        data = whole_packets(read_stream(self.url, 1.5)['data'])
        pes = video_pes(data)
        self.assertGreater(len(pes), 120)
        keep, a, b = 60, 25, 35
        cut = data[:pes[a][0] * sim_ts.TS] + data[pes[b][0] * sim_ts.TS:pes[keep][0] * sim_ts.TS]
        path = os.path.join(self.dir, 'burst-only.ts')
        with open(path, 'wb') as f:
            f.write(cut)
        answer = ak.check(self.sim, [path], min_gap=0.25)
        self.assertAnswer(answer, not answer.problems, 'no problems')
        row = self.sim.ledger.entries(endpoint='stream', status=200)[0]
        self.assertAnswer(answer, answer.extent[1] < row.at, 'the extent ends before the row '
                          'was answered')
        self.assertEqual([s.row for s in answer.stretches], [row.seq, row.seq], answer.describe())
        self.assertEqual(len(answer.lost), 1, answer.describe())
        lost = answer.lost[0]
        self.assertEqual(lost.reason, ak.REASON_NOT_RECORDED, answer.describe())
        self.assertEqual(lost.rows, (row.seq,))
        self.assertAlmostEqual(lost.seconds, (b - a) * FRAME, delta=4 * FRAME)

    def test_content_the_ledger_never_sent_is_a_problem(self):
        """With the ledger cleared, the same bytes match nothing the simulator says it served,
        and the answer says so instead of reporting an empty recording as fine."""
        data = whole_packets(read_stream(self.url, 1.5)['data'])
        path = os.path.join(self.dir, 'orphan.ts')
        with open(path, 'wb') as f:
            f.write(data)
        time.sleep(0.3)
        self.sim.ledger.clear()
        answer = ak.check(self.sim, [path], settle=0)
        self.assertEqual(answer.stretches, [])
        self.assertTrue(any('no frame of any loop' in p for p in answer.problems),
                        answer.problems)

    def test_an_empty_segment_file_holds_nothing_and_is_no_problem(self):
        """A capture that never received a byte leaves a zero-length file behind (the
        capture tier's restart that produced no data, dev/changelog/1223). It is read as a
        segment holding nothing, beside the real one, rather than failing ffprobe."""
        data = whole_packets(read_stream(self.url, 1.5)['data'])
        real = os.path.join(self.dir, 'real.ts')
        with open(real, 'wb') as f:
            f.write(data)
        empty = os.path.join(self.dir, 'empty.ts')
        open(empty, 'wb').close()
        answer = ak.check(self.sim, [real, empty])
        self.assertEqual(answer.segment_count, 2)
        self.assertTrue(answer.segments[1].empty)
        self.assertEqual(answer.segments[1].held_seconds, 0.0)
        self.assertFalse(answer.segments[0].empty)
        self.assertEqual(answer.problems, [], answer.describe())


    def test_a_cleared_withholding_fault_explains_only_what_it_withheld(self):
        """Guards dev/docs/BUGS.md 2026-10-19 (a cleared video_stops explained its whole
        connection): with the fault cleared before the check, a stretch the connection sent
        without video is still lost as withheld to the frame, and a stretch cut out of the
        recording by hand elsewhere on the same connection is lost as not recorded - never
        put down to the fault by its name alone."""
        start = at(-3.0)
        fault = self.sim.inject('video_stops', self.label, start=start,
                                end=start + dt.timedelta(seconds=2))
        data = whole_packets(read_stream(self.url, 2.5)['data'])
        self.sim.clear(fault)
        pes = video_pes(data)
        self.assertGreater(len(pes), 120)
        a, b = len(pes) - 45, len(pes) - 15      # inside the live part, after the window
        cut = data[:pes[a][0] * sim_ts.TS] + data[pes[b][0] * sim_ts.TS:]
        path = os.path.join(self.dir, 'cleared-fault.ts')
        with open(path, 'wb') as f:
            f.write(cut)
        answer = ak.check(self.sim, [path], min_gap=0.25)
        self.assertAnswer(answer, not answer.problems, 'no problems')
        row = self.sim.ledger.entries(endpoint='stream', status=200)[0]
        self.assertEqual([w[0] for w in row.withheld], ['video_stops'])
        withheld = [x for x in answer.lost if x.reason == ak.REASON_WITHHELD]
        unrecorded = [x for x in answer.lost if x.reason == ak.REASON_NOT_RECORDED]
        self.assertAnswer(answer, len(withheld) == 1 and len(unrecorded) == 1, 'one of each')
        self.assertTrue('video_stops' in withheld[0].detail
                        and 'no longer known' not in withheld[0].detail, answer.describe())
        # The test loop's B-frames present up to three frames away from where they are sent,
        # so where an edge falls inside a group of four, a held anchor frame presents inside
        # the hole and shortens it by up to three frames. The holes are therefore read from the
        # file's own timestamps, never from the count of frames taken out (dev/docs/BUGS.md
        # 2026-10-20).
        near = row.offered[0]
        pts = sorted(self.channel_seconds(p, near) for _, p in video_pes(cut))
        holes = [(x + FRAME, y) for x, y in zip(pts, pts[1:]) if y - x - FRAME >= 0.25]
        self.assertEqual(len(holes), 2, answer.describe())
        (w_lo, w_hi), (u_lo, u_hi) = holes
        # The fault's first withheld packet can fall inside a frame. That frame's header is
        # still on the wire, so the timestamps alone read it as held, but its bytes are not
        # whole and the checker rightly counts it lost: the hole may start up to one anchor
        # (four frames) earlier than the timestamps say. Every other edge is exact - the hand
        # cut falls on frame starts, and a frame whose header was withheld is in neither view.
        msg = answer.describe()
        self.assertLessEqual(withheld[0].start, w_lo + FRAME / 2, msg)
        self.assertGreaterEqual(withheld[0].start, w_lo - 4.5 * FRAME, msg)
        self.assertAlmostEqual(withheld[0].end, w_hi, delta=FRAME / 2, msg=msg)
        self.assertAlmostEqual(unrecorded[0].start, u_lo, delta=FRAME / 2, msg=msg)
        self.assertAlmostEqual(unrecorded[0].end, u_hi, delta=FRAME / 2, msg=msg)
        # The window is 2 s, 60 frames by send time; the edges move it by a few at most.
        self.assertAnswer(answer, 55 * FRAME - 1e-3 <= withheld[0].seconds <= 65 * FRAME + 1e-3,
                          f'{withheld[0].seconds / FRAME:.2f} frames withheld')
        s0 = start.replace(tzinfo=dt.timezone.utc).timestamp()
        self.assertAnswer(answer, s0 + 0.6 < withheld[0].start < s0 + 1.0, 'starts at the window')
        self.assertAnswer(answer, unrecorded[0].start > withheld[0].end, 'the cut is after it')

    def test_serving_limits_placement_to_the_channels_named(self):
        """Two channels cut from one loop show the same frames at different times, so a run
        captured from one can fit the other's connection when both are in the ledger - two
        recordings capturing at once, as the capture tier's connection-limit scenario does
        (dev/changelog/1223). Named with `serving`, every frame lands on the channel the
        recording was served from."""
        other = live_ids(self.world)[1]
        other_url = self.sim.stream_urls('cove-tv', 'fernuser')[other]
        read_stream(self.url, 4.0)                          # channel one: its own row
        data = whole_packets(read_stream(other_url, 2.5)['data'])
        path = os.path.join(self.dir, 'other.ts')
        with open(path, 'wb') as f:
            f.write(data)
        label = f'cove:{other}'
        answer = ak.check(self.sim, [path], serving=[label])
        self.assertTrue(answer.stretches, answer.describe())
        self.assertEqual({s.channel for s in answer.stretches}, {label}, answer.describe())
        self.assertEqual(answer.problems, [], answer.describe())


class CaptureTests(_AnswerKey):
    def test_a_reconnect_inside_a_segment_holds_the_burst_twice(self):
        """A silent connection, then ffmpeg's own reconnect inside the segment: the new
        connection's burst sends again what the first one sent, so the segment holds that
        stretch twice. Its file timeline counts the repeat; the distinct content does not."""
        self.sim.inject('silent_socket', self.label, times=1, after_bytes=200_000)
        path = self.capture(8)
        answer = ak.check(self.sim, [path], min_gap=0.25)
        self.assertAnswer(answer, not answer.problems, 'no problems')
        runs = answer.stretches
        self.assertEqual(len(runs), 2, answer.describe())
        silent, again = (self.sim.ledger.entries(seq=s.row)[0] for s in runs)
        self.assertIn('silent_socket', silent.faults)
        self.assertGreater(again.seq, silent.seq)
        self.assertAnswer(answer, runs[1].start < runs[0].end, 'the second run starts inside the '
                          'first')
        overlap = sum(max(0.0, min(b0, b1) - max(a0, a1))
                      for a0, b0 in runs[0].pieces for a1, b1 in runs[1].pieces)
        self.assertAlmostEqual(answer.repeated_seconds, overlap, places=5)
        self.assertGreater(answer.repeated_seconds, 1.0)
        self.assertEqual(answer.lost, [])
        seg = answer.segments[0]
        self.assertLessEqual(seg.unidentified, 1)   # at most the frame the silence cut short
        self.assertGreater(seg.file_video_seconds - seg.distinct_seconds, 1.0)

    def test_a_gap_between_segments_is_lost_and_says_no_connection_was_sending(self):
        """Two segments with the channel clock moved 30 s on between them: the gap is lost,
        no connection was sending it, and the reason names how the connection before it
        ended. The burst covers 4 s of the 30, and the cut point up to one keyframe more.
        What the first connection sent past the end of its 3 s segment - most of a 4 s burst
        read at once - is lost too, as sent but not recorded."""
        first = self.capture(3)
        self.settled()
        self.sim.set_clock(lambda: at(30))
        second = self.capture(3)
        answer = ak.check(self.sim, [first, second], min_gap=0.25)
        self.assertAnswer(answer, not answer.problems, 'no problems')
        unsent = [x for x in answer.lost if x.reason == ak.REASON_NOT_SENT]
        self.assertEqual(len(unsent), 1, answer.describe())
        self.assertIn('ended before it', unsent[0].detail)
        self.assertAnswer(answer, 20 < unsent[0].seconds < 31, 'about 30 s less the burst and '
                          'what the first connection sent past its segment')
        unrecorded = [x for x in answer.lost if x.reason != ak.REASON_NOT_SENT]
        self.assertEqual({x.reason for x in unrecorded}, {ak.REASON_NOT_RECORDED})
        self.assertLess(sum(x.seconds for x in unrecorded), 4.5)
        self.assertEqual(answer.segments[0].rows, (answer.stretches[0].row,))
        self.assertGreater(answer.segments[1].rows[0], answer.segments[0].rows[0])

    def test_withheld_video_is_lost_where_the_fault_withheld_it(self):
        """video_stops for 2 s of channel inside the burst: the recording loses those 2 s of
        frames, the loss starts where the window does
        plus the time a frame is sent ahead of when it is shown (0.68 to 0.89 s on this loop),
        and only the frames inside the window are put down to the fault - a B-frame missing
        where the segment ends is not."""
        start = at(-3.0)
        self.sim.inject('video_stops', self.label, start=start, end=start + dt.timedelta(
            seconds=2))
        answer = ak.check(self.sim, [self.capture(5)])
        self.assertAnswer(answer, not answer.problems, 'no problems')
        withheld = [x for x in answer.lost if x.reason == ak.REASON_WITHHELD]
        self.assertAnswer(answer, withheld, 'something withheld')
        self.assertTrue(all('video_stops' in x.detail for x in withheld))
        # 2 s is 59.94 frames, so 59 or 60 first packets fall in it by where the window lands on
        # the frame grid, one either way more where a big frame's packets spread the send times,
        # and one more when the window's start cuts a frame short.
        total = sum(x.seconds for x in withheld)
        self.assertAnswer(answer, 58 * FRAME - 1e-3 <= total <= 62 * FRAME + 1e-3,
                          f'{total / FRAME:.2f} frames withheld')
        s0 = start.replace(tzinfo=dt.timezone.utc).timestamp()
        lead = withheld[0].start - s0
        self.assertAnswer(answer, 0.6 < lead < 1.0, f'lead {lead:.3f}')
        self.assertAnswer(answer, withheld[-1].end < s0 + 2 + 1.0, 'nothing past the window')
        self.assertEqual({x.reason for x in answer.lost} - {ak.REASON_WITHHELD},
                         {ak.REASON_NOT_RECORDED} if len(withheld) < len(answer.lost) else set())
        self.assertGreater(answer.segments[0].audio_seconds, answer.segments[0].held_seconds)

    def test_the_placeholder_is_counted_and_explains_the_loss_around_it(self):
        """A segment of the placeholder clip, then the channel: the clip is counted as
        placeholder and placed nowhere, and the stretch of the recording's window before the
        channel came back is lost to the placeholder. The fault is left standing for the clip's
        segment: ffmpeg opens a file by reading its end and then fetching it again, and a
        `times: 1` would let that second fetch reach the channel."""
        window_start = at(0)
        f = self.sim.inject('placeholder', self.label, profile='test_tiny_low',
                            clip_seconds=4.0, speed=20.0)
        clip = self.capture(10)
        self.sim.clear(f)
        time.sleep(6.0)
        answer = ak.check(self.sim, [clip, self.capture(3)], window=(window_start, at(0)),
                          min_gap=0.25)
        self.assertAnswer(answer, not answer.problems, 'no problems')
        self.assertAlmostEqual(answer.segments[0].placeholder_seconds, 4.0, delta=0.2)
        self.assertEqual(answer.segments[0].stretches, [])
        self.assertEqual(answer.segments[1].channels, (self.label,))
        self.assertIn(ak.REASON_PLACEHOLDER, [x.reason for x in answer.lost], answer.describe())

    def test_a_refused_attempt_between_segments_is_named(self):
        """A 405 at open between two segments, and the next attempt later than the burst can
        reach back: the stretch lost after the refusal is lost as refused, naming the status."""
        first = self.capture(3)
        self.sim.inject('refuse_at_open', self.label, times=1)
        failed = self.capture(2)
        self.assertFalse(os.path.exists(failed) and os.path.getsize(failed))
        time.sleep(7.0)
        answer = ak.check(self.sim, [first, self.capture(3)], min_gap=0.25)
        self.assertAnswer(answer, not answer.problems, 'no problems')
        refused = [x for x in answer.lost if x.reason == ak.REASON_REFUSED]
        self.assertEqual(len(refused), 1, answer.describe())
        self.assertIn('405', refused[0].detail)

    def test_a_format_change_shows_in_each_segment_s_loop(self):
        """format_change on the second connection: each segment names the loop it holds."""
        first = self.capture(2)
        self.sim.inject('format_change', self.label, times=1, profile='test_tiny_low')
        second = self.capture(2)
        answer = ak.check(self.sim, [first, second])
        self.assertAnswer(answer, not answer.problems, 'no problems')
        self.assertEqual([s.profiles for s in answer.segments],
                         [('test_tiny',), ('test_tiny_low',)])
        self.assertEqual(answer.channels, (self.label,))


class CompareTests(_AnswerKey):
    def setUp(self):
        super().setUp()
        self.answer = ak.check(self.sim, [self.capture(3)], min_gap=0.25)

    def test_figures_that_agree_report_nothing(self):
        a = self.answer
        claims = {'distinct_seconds': a.distinct_seconds + 0.05, 'lost_seconds': 0.0,
                  'lost_count': 0, 'channels': [self.label],
                  'segments': [{'held_seconds': a.segments[0].held_seconds}]}
        self.assertEqual(a.compare(claims), [])

    def test_a_planted_wrong_number_is_caught_and_names_its_kind(self):
        """Every mismatch says what the figure measures, so a content duration is never
        silently read against a wall clock or a file's timeline."""
        a = self.answer
        wrong = a.compare({'distinct_seconds': a.distinct_seconds + 5,
                           'lost_count': 2,
                           'segments': [{'channels': ['cove:1']}]})
        self.assertEqual([m.field for m in wrong],
                         ['distinct_seconds', 'lost_count', 'segments[0].channels'])
        self.assertEqual(wrong[0].kind, ak.KIND_CONTENT)
        self.assertIn('the answer key says', str(wrong[0]))
        self.assertEqual(a.compare({'segments': [None, None]})[0].field, 'segment_count')

    def test_a_figure_it_does_not_know_is_an_error_not_a_pass(self):
        with self.assertRaises(ValueError):
            self.answer.compare({'lost_secs': 0})
        with self.assertRaises(ValueError):
            self.answer.compare({'segments': [{'content_seconds': 3}]})


if __name__ == '__main__':
    unittest.main()
