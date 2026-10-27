"""Tier 1/2 - counting the times ffmpeg dropped a stream and reconnected WITHOUT the
segment ending, and saying so on the segment's diagnostics event.

Guards dev/docs/BUGS.md 2026-09-14 @ 07:05:04 AM ET. An in-process reconnect is the repair
that ffmpeg.read_timeout_seconds exists to make possible (dev/changelog/958), and before this
it left no trace at all: a segment that silently reconnected eight times was indistinguishable
from a clean one on every surface the app has. The count cannot come from the stderr TAIL,
which is the obvious cheap answer and is wrong - the tail is 20 lines and a capture's last 20
stderr lines are almost always 20 progress lines, so the reconnects have long scrolled past.

No network: every fixture here is a file written directly, and the only child processes are
`sys.executable -c ...` with no URL in the argv (tests/support/netguard.py).
"""
import json
import os
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tests.support.app import make_test_app  # noqa: E402
from tests.support import seed  # noqa: E402
from app import db  # noqa: E402
from app import recorder  # noqa: E402
from app.database import DIAGNOSTICS, Recording, RecordingEvent, RecordingSegment  # noqa: E402
from app.proc_utils import (RECONNECT_MARKER, ReconnectJumps,  # noqa: E402
                            count_stderr_matches, read_reconnect_jumps, read_stderr_tail)

# One real line as ffmpeg 7.1 writes it on this box, from the loopback capture in
# dev/changelog/958. The byte offset, the delay and the error text all vary run to run,
# which is why the marker is matched as a substring rather than a whole line.
_RECONNECT_LINE = ('[http @ 0x609b972a8400] Will reconnect at 12144456 in 0 second(s), '
                   'error=Connection timed out.\n')
_PROGRESS_LINE = ('frame= 1880 fps=188 q=-1.0 size=   11776KiB time=00:01:02.55 '
                  'bitrate=1542.2kbits/s speed=6.25x\n')

# What ffmpeg 7.1.5 wrote after reconnects to a simulated provider that cuts a login's
# oldest connection off and answers the new one from its buffer (dev/changelog/1237). The
# ordinary case closes the backward jump once, on whichever stream's new packet the
# demuxer hands over first; the doubled case is a connection that broke inside the audio's
# frame, so the audio's last old packet (corrupt) came out after the video's jump was
# already closed and ffmpeg closed it again. The third is the usual residual: the other
# stream's first packet a fraction of a second off, closed up too, which is not a replay.
_JUMP_SINGLE = (
    '[http @ 0x5f2594fbab00] Will reconnect at 1454744 in 0 second(s), error=Input/output error.\n'
    '[mpegts @ 0x5f2595010340] Packet corrupt (stream = 0, dts = 3371283782).\n'
    '[in#0/mpegts @ 0x5f2595010080] corrupt input packet in stream 0\n'
    '[vist#0:0/h264 @ 0x5f25950ec9c0] timestamp discontinuity (stream id=256): -4471134, '
    'new offset= 4471134\n')
_JUMP_DOUBLED = (
    '[http @ 0x5f2594fbab00] Will reconnect at 899956 in 0 second(s), error=Input/output error.\n'
    '[mpegts @ 0x5f2595010340] Packet corrupt (stream = 0, dts = 3373265762).\n'
    '[in#0/mpegts @ 0x5f2595010080] corrupt input packet in stream 0\n'
    '[vist#0:0/h264 @ 0x5f25950ec9c0] timestamp discontinuity (stream id=256): -5138467, '
    'new offset= 14414401\n'
    '[mpegts @ 0x5f2595010340] PES packet size mismatch\n'
    '[mpegts @ 0x5f2595010340] Packet corrupt (stream = 1, dts = 3373235669).\n'
    '[in#0/mpegts @ 0x5f2595010080] corrupt input packet in stream 1\n'
    '    Last message repeated 12 times\n'
    '[aist#0:1/aac @ 0x5f2595034cc0] timestamp discontinuity (stream id=257): -5077333, '
    'new offset= 19491734\n')
_JUMP_RESIDUAL = (
    '[http @ 0x60935a2e6b00] Will reconnect at 882660 in 0 second(s), error=Input/output error.\n'
    '[vist#0:0/h264 @ 0x60935a4189c0] timestamp discontinuity (stream id=256): -4971634, '
    'new offset= 82843403\n'
    '[aist#0:1/aac @ 0x60935a360cc0] timestamp discontinuity (stream id=257): -148366, '
    'new offset= 82991769\n')
# A progress line ends in '\r', so the next message shares its '\n' line.
_JUMP_AFTER_PROGRESS = (
    'frame= 2446 fps= 44 q=-1.0 size=    4352KiB time=00:01:21.62 bitrate= 436.8kbits/s '
    'speed=1.48x    \r[http @ 0x60935a2e6b00] Will reconnect at 874388 in 0 second(s), '
    'error=Input/output error.\n'
    'frame= 2447 fps= 44 q=-1.0 size=    4352KiB time=00:01:21.66 bitrate= 436.8kbits/s '
    'speed=1.48x    \r[vist#0:0/h264 @ 0x60935a4189c0] timestamp discontinuity '
    '(stream id=256): -4778666, new offset= 26266801\n')
_FORWARD_JUMP = ('[vist#0:0/h264 @ 0x60935a4189c0] timestamp discontinuity (stream id=256): '
                 '12000000, new offset= -12000000\n')


class ReadReconnectJumpsTests(unittest.TestCase):
    """What ffmpeg's timestamp repair says each reconnect did to the file (dev/changelog/1237)."""

    def _spool(self, text):
        fd, path = tempfile.mkstemp()
        with os.fdopen(fd, 'wb') as fh:
            fh.write(text.encode())
        self.addCleanup(os.unlink, path)
        return path

    def test_a_closed_backward_jump_is_the_replay(self):
        path = self._spool(_PROGRESS_LINE * 5 + _JUMP_SINGLE + _PROGRESS_LINE * 5)
        self.assertEqual(read_reconnect_jumps(path), ReconnectJumps((4.471,), (), True))

    def test_a_jump_closed_once_per_stream_is_a_replay_and_a_hole(self):
        path = self._spool(_JUMP_SINGLE + _PROGRESS_LINE + _JUMP_DOUBLED)
        self.assertEqual(read_reconnect_jumps(path),
                         ReconnectJumps((4.471, 5.138), (5.077,), True))

    def test_the_other_streams_residual_is_not_a_hole(self):
        path = self._spool(_JUMP_RESIDUAL)
        self.assertEqual(read_reconnect_jumps(path), ReconnectJumps((4.972,), (), True))

    def test_a_jump_before_any_reconnect_is_not_attributed_to_one(self):
        """A provider's own timestamp jump, closed with no reconnect, is not a replay."""
        jump = _JUMP_SINGLE.split('\n', 1)[1]
        path = self._spool(jump + _PROGRESS_LINE + _JUMP_SINGLE)
        self.assertEqual(read_reconnect_jumps(path), ReconnectJumps((4.471,), (), True))

    def test_a_forward_jump_is_not_a_replay(self):
        path = self._spool(_RECONNECT_LINE + _FORWARD_JUMP)
        self.assertEqual(read_reconnect_jumps(path), ReconnectJumps((), (), True))

    def test_a_reconnect_that_closed_no_jump_contributes_nothing(self):
        path = self._spool(_RECONNECT_LINE * 3 + _JUMP_SINGLE)
        self.assertEqual(read_reconnect_jumps(path).replays, (4.471,))

    def test_a_jump_glued_to_a_progress_line_is_still_read(self):
        path = self._spool(_JUMP_AFTER_PROGRESS)
        self.assertEqual(read_reconnect_jumps(path), ReconnectJumps((4.779,), (), True))

    def test_truncated_scan_reports_incomplete(self):
        path = self._spool(_JUMP_SINGLE * 4)
        got = read_reconnect_jumps(path, max_bytes=len(_JUMP_SINGLE) * 2)
        self.assertFalse(got.complete)
        self.assertEqual(got.replays, (4.471, 4.471))

    def test_missing_or_empty_path_never_raises(self):
        self.assertEqual(read_reconnect_jumps('/nonexistent/spool.log'),
                         ReconnectJumps((), (), True))
        self.assertEqual(read_reconnect_jumps(None), ReconnectJumps((), (), True))
        self.assertEqual(read_reconnect_jumps(''), ReconnectJumps((), (), True))


class CountStderrMatchesTests(unittest.TestCase):
    """The pure counter."""

    def _spool(self, text):
        fd, path = tempfile.mkstemp()
        with os.fdopen(fd, 'wb') as fh:
            fh.write(text.encode())
        self.addCleanup(os.unlink, path)
        return path

    def test_counts_every_occurrence(self):
        path = self._spool(_RECONNECT_LINE * 8)
        self.assertEqual(count_stderr_matches(path), (8, True))

    def test_none_is_zero_and_complete(self):
        path = self._spool(_PROGRESS_LINE * 50)
        self.assertEqual(count_stderr_matches(path), (0, True))

    def test_finds_reconnects_buried_far_above_the_tail(self):
        """The reason this reads the whole file and not read_stderr_tail(): the tail is
        capped at 20 lines, and a capture's last 20 lines are progress stats."""
        path = self._spool(_RECONNECT_LINE * 3 + _PROGRESS_LINE * 500)
        self.assertEqual(count_stderr_matches(path)[0], 3)
        self.assertNotIn(RECONNECT_MARKER, read_stderr_tail(path))

    def test_counts_across_a_chunk_boundary(self):
        """The marker must still be seen when it straddles the 1MB read boundary, and must
        not be double-counted by the overlap that makes that work."""
        pad = 'x' * ((1 << 20) - len(RECONNECT_MARKER) // 2)
        path = self._spool(pad + _RECONNECT_LINE)
        self.assertEqual(count_stderr_matches(path), (1, True))

    def test_truncated_scan_reports_incomplete(self):
        path = self._spool(_RECONNECT_LINE * 10)
        count, complete = count_stderr_matches(path, max_bytes=len(_RECONNECT_LINE) * 3)
        self.assertFalse(complete)
        self.assertGreaterEqual(count, 1)
        self.assertLess(count, 10)

    def test_missing_file_never_raises(self):
        self.assertEqual(count_stderr_matches('/nonexistent/spool.log'), (0, True))

    def test_empty_path_never_raises(self):
        self.assertEqual(count_stderr_matches(None), (0, True))
        self.assertEqual(count_stderr_matches(''), (0, True))


class CollectReturnsReconnectsTests(unittest.TestCase):
    """collect_segment_diagnostics hands the count back alongside the exit code and tail,
    read before it unlinks the spool."""

    def setUp(self):
        self.t = make_test_app()
        self.ctx = self.t.app.app_context()
        self.ctx.push()
        self.rec = seed.make_recording(status='IN_PROGRESS')
        db.session.commit()
        self.state = recorder.RecordingState()
        with recorder._lock:
            recorder._active[self.rec.id] = self.state

    def tearDown(self):
        with recorder._lock:
            recorder._active.pop(self.rec.id, None)
        self.ctx.pop()
        self.t.cleanup()

    def _spool_with(self, text):
        path, fh = recorder._open_segment_stderr_spool(self.t.app, self.rec.id, 1)
        fh.write(text.encode())
        fh.flush()
        self.state.stderr_path, self.state.stderr_fh = path, fh
        self.state.process = subprocess.Popen(
            [sys.executable, '-c', 'import sys; sys.exit(0)'],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.state.process.wait(timeout=30)
        return path

    def test_reconnect_count_is_returned(self):
        self._spool_with(_RECONNECT_LINE * 2 + _PROGRESS_LINE * 200)
        _code, _tail, reconnects, _missing, _jumps = \
            recorder.collect_segment_diagnostics(self.rec.id)
        self.assertEqual(reconnects, (2, True))

    def test_clean_segment_reports_zero(self):
        self._spool_with(_PROGRESS_LINE * 40)
        _code, _tail, reconnects, _missing, _jumps = \
            recorder.collect_segment_diagnostics(self.rec.id)
        self.assertEqual(reconnects, (0, True))

    def test_spool_is_still_unlinked_after_counting(self):
        """Teardown releases everything: the extra read must not leave the file behind."""
        path = self._spool_with(_RECONNECT_LINE)
        recorder.collect_segment_diagnostics(self.rec.id)
        self.assertFalse(os.path.exists(path))

    def test_what_the_reconnects_did_to_the_file_is_returned_with_the_count(self):
        self._spool_with(_JUMP_SINGLE + _PROGRESS_LINE * 20 + _JUMP_DOUBLED)
        *_rest, reconnects, _missing, jumps = recorder.collect_segment_diagnostics(self.rec.id)
        self.assertEqual(reconnects, (2, True))
        self.assertEqual(jumps, ReconnectJumps((4.471, 5.138), (5.077,), True))

    def test_a_clean_segment_returns_no_jumps(self):
        self._spool_with(_PROGRESS_LINE * 40)
        self.assertIsNone(recorder.collect_segment_diagnostics(self.rec.id)[4])


class ReconnectDisclosureTests(unittest.TestCase):
    """What the user is actually told."""

    def setUp(self):
        self.t = make_test_app()
        self.ctx = self.t.app.app_context()
        self.ctx.push()
        self.rec = seed.make_recording(status='IN_PROGRESS', with_segment=True)
        db.session.commit()
        self.seg = RecordingSegment.query.filter_by(recording_id=self.rec.id).first()

    def tearDown(self):
        self.ctx.pop()
        self.t.cleanup()

    def _record(self, code, tail, reconnects=(0, True), jumps=None):
        recorder.record_segment_diagnostics(self.rec.id, self.seg, code, tail, reconnects,
                                            jumps=jumps)
        db.session.commit()

    def _events(self):
        return RecordingEvent.query.filter_by(
            recording_id=self.rec.id, event_type=DIAGNOSTICS).all()

    def test_count_reaches_the_detail_string(self):
        self._record(-15, '', (8, True))
        self.assertIn('8', self._events()[0].detail)
        self.assertIn('reconnected', self._events()[0].detail)

    def test_reconnects_break_the_uninformative_exit_silence(self):
        """A SIGTERM exit with no stderr normally emits nothing, on purpose - but a segment
        that reconnected eight times on the way there is not uninformative."""
        self._record(-15, '', (0, True))
        self.assertEqual(self._events(), [])
        self._record(-15, '', (8, True))
        self.assertEqual(len(self._events()), 1)

    def test_zero_reconnects_says_nothing_about_reconnecting(self):
        self._record(4, '', (0, True))
        self.assertNotIn('reconnect', self._events()[0].detail)

    def test_truncated_scan_is_reported_as_a_floor(self):
        """Never state a bounded count as a fact."""
        self._record(4, '', (12, False))
        self.assertIn('at least 12', self._events()[0].detail)

    def test_complete_count_is_stated_without_hedging(self):
        self._record(4, '', (12, True))
        self.assertIn('12 times', self._events()[0].detail)
        self.assertNotIn('at least', self._events()[0].detail)

    def test_single_complete_reconnect_is_singular(self):
        self._record(4, '', (1, True))
        self.assertIn('reconnected 1 time during', self._events()[0].detail)
        self.assertNotIn('1 times', self._events()[0].detail)

    def test_count_is_not_duplicated_into_extra_data(self):
        """CLAUDE.md measurements rule: the headline number goes in detail; extra_data
        carries only what has no other home."""
        self._record(4, 'boom', (3, True))
        extra = json.loads(self._events()[0].extra_data)
        self.assertEqual(set(extra), {'kind', 'stderr_tail'})

    def test_exit_code_is_still_reported_alongside(self):
        self._record(4, '', (3, True))
        self.assertIn('ffmpeg exited 4', self._events()[0].detail)

    def test_default_keeps_old_callers_working(self):
        recorder.record_segment_diagnostics(self.rec.id, self.seg, 4, '')
        db.session.commit()
        self.assertNotIn('reconnect', self._events()[0].detail)

    # The count's column (dev/changelog/1306): the Stats card and the Recordings list sum it,
    # where the detail string above is one line per segment in the activity log.
    def test_the_count_is_written_to_the_segment_row(self):
        self._record(-15, '', (8, True))
        db.session.expire_all()
        seg = db.session.get(RecordingSegment, self.seg.id)
        self.assertEqual(seg.reconnect_count, 8)
        self.assertFalse(seg.reconnects_truncated)

    def test_a_quiet_exit_still_records_a_measured_zero(self):
        """The early return that keeps a SIGTERM exit off the event log must not keep the
        count off the row: 0 reconnects is a fact, NULL would mean never counted."""
        self._record(-15, '', (0, True))
        self.assertEqual(self._events(), [])
        db.session.expire_all()
        self.assertEqual(db.session.get(RecordingSegment, self.seg.id).reconnect_count, 0)

    def test_a_truncated_scan_marks_the_row_as_a_floor(self):
        self._record(4, '', (12, False))
        db.session.expire_all()
        self.assertTrue(db.session.get(RecordingSegment, self.seg.id).reconnects_truncated)


    # What the reconnects did to the file (dev/changelog/1237): a file that plays 86 s for a
    # 60 s window is a number nobody can explain unless the event says the provider's buffer
    # was captured again after each drop.

    def test_the_seconds_held_twice_reach_the_detail_string(self):
        self._record(-15, '', (4, True), ReconnectJumps((4.0, 5.1, 5.4, 5.8), (), True))
        detail = self._events()[0].detail
        self.assertIn('holds the seconds before each drop twice', detail)
        self.assertIn('4.0, 5.1, 5.4 and 5.8 s, 20.3 s in all', detail)
        self.assertNotIn('hole', detail)

    def test_a_hole_is_named_with_its_size_and_that_nothing_is_missing(self):
        self._record(-15, '', (4, True), ReconnectJumps((4.0, 5.1, 5.4, 5.8), (5.1, 5.8), True))
        detail = self._events()[0].detail
        self.assertIn('at 2 of them ffmpeg closed the timestamp jump once per stream', detail)
        self.assertIn('hole of about 5.1 and 5.8 s', detail)
        self.assertIn('no content missing', detail)

    def test_one_replay_reads_as_one(self):
        self._record(-15, '', (1, True), ReconnectJumps((4.5,), (), True))
        self.assertIn('twice: 4.5 s, 4.5 s in all', self._events()[0].detail)

    def test_per_reconnect_seconds_ride_extra_data_and_nothing_else_new(self):
        """The lists have no column, so extra_data is their only home; the totals stay in the
        detail string (CLAUDE.md measurements rule)."""
        self._record(4, 'boom', (2, True), ReconnectJumps((4.0, 5.1), (5.1,), True))
        extra = json.loads(self._events()[0].extra_data)
        self.assertEqual(set(extra), {'kind', 'stderr_tail', 'reconnect_replay_seconds',
                                      'reconnect_hole_seconds'})
        self.assertEqual(extra['reconnect_replay_seconds'], [4.0, 5.1])
        self.assertEqual(extra['reconnect_hole_seconds'], [5.1])

    def test_jumps_without_a_reconnect_count_change_nothing(self):
        self._record(4, '', (0, True), ReconnectJumps((4.0,), (), True))
        self.assertNotIn('twice', self._events()[0].detail)
        self.assertEqual(set(json.loads(self._events()[0].extra_data)), {'kind'})

    def test_reconnects_that_closed_no_jump_say_nothing_about_replay(self):
        self._record(4, '', (3, True), ReconnectJumps((), (), True))
        self.assertIn('reconnected 3 times', self._events()[0].detail)
        self.assertNotIn('twice', self._events()[0].detail)


class ReconnectSurfacesTests(unittest.TestCase):
    """The recording's total, and the two surfaces that read it: the list row's health
    tooltip and the Stats card (dev/changelog/1306). Before the column the list read
    "0 stalls - clean capture" for a recording whose one segment reconnected eight times."""

    def setUp(self):
        self.t = make_test_app()
        self.now = datetime.utcnow()

    def tearDown(self):
        self.t.cleanup()

    def _rec(self, counts):
        rec = seed.make_recording(start_time=self.now - timedelta(seconds=3600),
                                  stop_time=self.now, status='COMPLETED')
        for n, (count, truncated) in enumerate(counts):
            db.session.add(RecordingSegment(
                recording_id=rec.id, segment_number=n, file_path=f'/nonexistent-{n}.ts',
                started_at=self.now - timedelta(seconds=3600 - n * 600),
                ended_at=self.now - timedelta(seconds=3000 - n * 600),
                bytes_recorded=4096, stall_count=0,
                reconnect_count=count, reconnects_truncated=truncated))
        db.session.commit()
        return rec.id

    def _row(self, rec_id):
        from app.routes.recordings import _index_row
        from app.tz_utils import get_display_tz
        db.session.expire_all()
        return _index_row(db.session.get(Recording, rec_id), datetime.utcnow(),
                          get_display_tz(), set())

    def test_the_recording_total_sums_its_segments(self):
        rid = self._rec([(3, False), (5, False), (None, None)])
        rec = db.session.get(Recording, rid)
        self.assertEqual(rec.reconnect_count, 8)
        self.assertFalse(rec.reconnects_truncated)

    def test_one_truncated_segment_makes_the_total_a_floor(self):
        rid = self._rec([(3, False), (5, True)])
        self.assertTrue(db.session.get(Recording, rid).reconnects_truncated)

    def test_the_list_row_no_longer_calls_a_reconnecting_capture_clean(self):
        health = self._row(self._rec([(8, False)]))['health']
        self.assertEqual(health['cls'], 'ok', 'a reconnect is ffmpeg\'s own repair - green')
        self.assertNotIn('clean capture', health['tip'])
        self.assertIn('reconnected 8 times inside a segment', health['tip'])

    def test_the_list_row_hedges_a_truncated_count(self):
        self.assertIn('at least 12 times', self._row(self._rec([(12, True)]))['health']['tip'])

    def test_a_single_reconnect_is_singular(self):
        self.assertIn('reconnected 1 time inside', self._row(self._rec([(1, False)]))['health']['tip'])

    def test_no_reconnects_keeps_the_clean_wording(self):
        tip = self._row(self._rec([(0, False)]))['health']['tip']
        self.assertIn('clean capture', tip)
        self.assertNotIn('reconnected', tip)

    def test_the_stats_card_has_a_reconnects_row(self):
        rid = self._rec([(8, False)])
        html = self.t.client.get(f'/recordings/{rid}').get_data(as_text=True)
        self.assertIn('Reconnects', html)
        self.assertIn('without the segment ending', html)


if __name__ == '__main__':
    unittest.main(verbosity=2)
