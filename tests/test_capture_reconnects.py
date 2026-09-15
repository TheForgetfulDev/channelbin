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

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tests.support.app import make_test_app  # noqa: E402
from tests.support import seed  # noqa: E402
from app import db  # noqa: E402
from app import recorder  # noqa: E402
from app.database import DIAGNOSTICS, RecordingEvent, RecordingSegment  # noqa: E402
from app.proc_utils import (RECONNECT_MARKER, count_stderr_matches,  # noqa: E402
                            read_stderr_tail)

# One real line as ffmpeg 7.1 writes it on this box, from the loopback capture in
# dev/changelog/958. The byte offset, the delay and the error text all vary run to run,
# which is why the marker is matched as a substring rather than a whole line.
_RECONNECT_LINE = ('[http @ 0x609b972a8400] Will reconnect at 12144456 in 0 second(s), '
                   'error=Connection timed out.\n')
_PROGRESS_LINE = ('frame= 1880 fps=188 q=-1.0 size=   11776KiB time=00:01:02.55 '
                  'bitrate=1542.2kbits/s speed=6.25x\n')


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
        _code, _tail, reconnects, _missing = recorder.collect_segment_diagnostics(self.rec.id)
        self.assertEqual(reconnects, (2, True))

    def test_clean_segment_reports_zero(self):
        self._spool_with(_PROGRESS_LINE * 40)
        _code, _tail, reconnects, _missing = recorder.collect_segment_diagnostics(self.rec.id)
        self.assertEqual(reconnects, (0, True))

    def test_spool_is_still_unlinked_after_counting(self):
        """Teardown releases everything: the extra read must not leave the file behind."""
        path = self._spool_with(_RECONNECT_LINE)
        recorder.collect_segment_diagnostics(self.rec.id)
        self.assertFalse(os.path.exists(path))


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

    def _record(self, code, tail, reconnects=(0, True)):
        recorder.record_segment_diagnostics(self.rec.id, self.seg, code, tail, reconnects)
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


if __name__ == '__main__':
    unittest.main(verbosity=2)
