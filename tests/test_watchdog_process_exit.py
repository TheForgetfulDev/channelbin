"""Tier 2 - the watchdog notices an exited capture process instead of waiting out the
stall timeout.

Guards dev/docs/BUGS.md 2026-08-01 "Watchdog waits out the full stall timeout even when
ffmpeg has already exited". Design and reasoning: dev/changelog/431.

The defect was an omission rather than a wrong branch: the inner poll loop read
os.path.getsize() and nothing else, so a capture whose ffmpeg died the instant the stream
dropped still cost stall_timeout_seconds to notice plus restart_delay_seconds to relaunch.
On recording 71 (dev/changelog/429) that was ~15s x 75 stalls = 18.8 of the ~30 minutes of
content lost - more than the dead provider feed itself cost.

No network and no provider host: every child here is `sys.executable -c ...`, a local argv
with no URL in it, which tests/support/netguard.py permits. Segment files are written under
make_test_app's temp dir, never /dvr.
"""
import json
import os
import subprocess
import sys
import time
import unittest
from datetime import datetime, timedelta
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import app.config as cfgmod  # noqa: E402
import app.recorder as recorder  # noqa: E402
import app.watchdog as wdmod  # noqa: E402
from app import db  # noqa: E402
from app.database import STALL_DETECTED, RecordingEvent, RecordingSegment  # noqa: E402
from app.proc_utils import wait_for_file_data  # noqa: E402
from app.recorder import RecordingState  # noqa: E402
from app.watchdog import WatchdogThread  # noqa: E402
from tests.support import seed  # noqa: E402
from tests.support.app import make_test_app  # noqa: E402


def _sleeper(seconds=120):
    """A live local child that outlives the test unless something kills it."""
    return subprocess.Popen([sys.executable, '-c', f'import time; time.sleep({seconds})'],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


class _WatchdogHarness(unittest.TestCase):
    """One recording, one open segment on a static file, one WatchdogThread under our own
    config. _launch_segment is stubbed throughout: a restart is not what any of these
    assert, and letting the real one run would spawn ffmpeg at a provider URL."""

    def setUp(self):
        self.t = make_test_app()
        self.wd = None
        self.launched = []
        self.state = RecordingState(current_segment_num=0)

        now = datetime.utcnow()
        rec = seed.make_recording(status='IN_PROGRESS', name='wdexit', started_at=now,
                                  start_time=now, stop_time=now + timedelta(hours=1))
        self.rid = rec.id
        self.seg_path = os.path.join(self.t._tmpdir, f'rec_{self.rid}_seg_000.ts')
        with open(self.seg_path, 'wb') as fh:
            fh.write(b'\x47' * 4096)
        db.session.add(RecordingSegment(recording_id=self.rid, segment_number=0,
                                        file_path=self.seg_path, started_at=now))
        db.session.commit()

        with recorder._lock:
            recorder._active[self.rid] = self.state

    def tearDown(self):
        self.state.stop_event.set()
        if self.wd is not None:
            self.wd.join(timeout=15)
        proc = self.state.process
        if isinstance(proc, subprocess.Popen) and proc.poll() is None:
            proc.kill()
            proc.wait(timeout=10)
        with recorder._lock:
            recorder._active.pop(self.rid, None)
        self.t.cleanup()

    def _exited_capture(self, code=1, stderr_text='Connection reset by peer\n'):
        """An ffmpeg stand-in that has ALREADY exited, spooling stderr the way the real
        capture does, so the exit code and tail this path records are real values."""
        path, fh = recorder._open_segment_stderr_spool(self.t.app, self.rid, 0)
        self.state.stderr_path, self.state.stderr_fh = path, fh
        proc = subprocess.Popen(
            [sys.executable, '-c',
             f'import sys; sys.stderr.write({stderr_text!r}); sys.stderr.flush(); '
             f'sys.exit({code})'],
            stdout=subprocess.DEVNULL, stderr=(fh or subprocess.DEVNULL))
        proc.wait(timeout=30)
        self.state.process = proc
        return proc

    def _cfg(self, stall_timeout, restart_delay=0):
        return cfgmod._deep_merge(cfgmod.load_config(), {'watchdog': {
            'poll_interval_seconds': 1,
            'stall_timeout_seconds': stall_timeout,
            'restart_delay_seconds': restart_delay,
            'max_consecutive_failures': 99,
            'early_fail_abort_count': 99,
        }})

    def _stub_launch(self, new_process=None):
        """Stand-in for _launch_segment. Sets stop_event so the watchdog winds down after
        one restart instead of looping; the real one is checked by its own tests."""
        def _stub(app, recording_id, seg_num):
            self.launched.append(seg_num)
            if new_process is not None:
                self.state.process = new_process
            self.state.stop_event.set()
        return _stub

    def _run_until_segment_closed(self, stall_timeout, new_process=None, timeout=45):
        """Start the real thread; return (segment row, seconds until it was closed)."""
        started = time.monotonic()
        elapsed = None
        with mock.patch.object(cfgmod, 'load_config',
                               return_value=self._cfg(stall_timeout)), \
             mock.patch.object(recorder, '_launch_segment',
                               self._stub_launch(new_process)):
            self.wd = WatchdogThread(self.rid, self.state, self.t.app)
            self.wd.start()
            deadline = time.monotonic() + timeout
            while time.monotonic() < deadline:
                db.session.expire_all()
                seg = RecordingSegment.query.filter_by(
                    recording_id=self.rid, segment_number=0).first()
                if seg.ended_at is not None:
                    elapsed = time.monotonic() - started
                    break
                time.sleep(0.2)
            self.state.stop_event.set()
            self.wd.join(timeout=15)
        db.session.expire_all()
        seg = RecordingSegment.query.filter_by(
            recording_id=self.rid, segment_number=0).first()
        return seg, elapsed

    def _stall_event(self):
        db.session.expire_all()
        return (RecordingEvent.query
                .filter_by(recording_id=self.rid, event_type=STALL_DETECTED)
                .order_by(RecordingEvent.id.desc()).first())


class ExitedCaptureIsNoticedTests(_WatchdogHarness):

    def test_an_exited_capture_is_noticed_within_a_poll_not_a_stall_timeout(self):
        """The whole point. stall_timeout is 30s here, so a run that only notices by the
        growth timer cannot come in under 10 - and the exit_reason assertion below fails
        deterministically either way, so falsification does not rest on the clock."""
        self._exited_capture()
        seg, elapsed = self._run_until_segment_closed(stall_timeout=30)

        self.assertIsNotNone(elapsed, 'the segment was never closed out')
        self.assertLess(elapsed, 10,
                        f'took {elapsed:.1f}s to notice an already-dead capture with a 30s '
                        f'stall timeout - it is still waiting out the growth timer')
        self.assertEqual(seg.exit_reason, 'PROCESS_EXITED')

    def test_the_row_says_the_process_exited_rather_than_claiming_a_kill(self):
        """STALL_KILLED here would be a lie the exit code sitting beside it contradicts: a
        positive code means ffmpeg gave up on its own, a negative one is our signal."""
        self._exited_capture(code=3)
        seg, _ = self._run_until_segment_closed(stall_timeout=30)

        self.assertEqual(seg.exit_reason, 'PROCESS_EXITED')
        self.assertEqual(seg.ffmpeg_exit_code, 3)
        self.assertEqual(seg.bytes_recorded, os.path.getsize(self.seg_path))

    def test_the_stall_event_names_the_process_exit(self):
        """Product Principle 1: the event log has to say which of the two shapes happened,
        not just that capture was interrupted."""
        self._exited_capture()
        self._run_until_segment_closed(stall_timeout=30)

        evt = self._stall_event()
        self.assertIsNotNone(evt, 'no STALL_DETECTED event was written')
        self.assertIn('exited on its own', evt.detail)
        self.assertEqual(json.loads(evt.extra_data)['stall_reason'], 'process_exited')

    def test_the_capture_stalls_counter_still_moves(self):
        """A dead process is still a capture interruption - it must not fall out of
        total_stall_count and quietly make the recording look healthier than it was."""
        self._exited_capture()
        self._run_until_segment_closed(stall_timeout=30)

        db.session.expire_all()
        from app.database import Recording
        self.assertEqual(db.session.get(Recording, self.rid).total_stall_count, 1)


class LiveButStalledCaptureTests(_WatchdogHarness):

    def test_a_live_capture_that_stops_growing_is_still_a_stall_kill(self):
        """The pre-existing behavior, which the new branch must not swallow: the process is
        alive, so only the growth timer can catch it and we are the ones doing the killing."""
        self.state.process = _sleeper()
        seg, elapsed = self._run_until_segment_closed(stall_timeout=2)

        self.assertIsNotNone(elapsed, 'the segment was never closed out')
        self.assertEqual(seg.exit_reason, 'STALL_KILLED')
        evt = self._stall_event()
        self.assertEqual(json.loads(evt.extra_data)['stall_reason'], 'no_growth')
        self.assertIn('File stalled at', evt.detail)


class DeliberateKillIsNotADeadFeedTests(_WatchdogHarness):

    def test_kill_all_active_is_not_read_as_a_dead_feed(self):
        """stop_event, not poll(), is what separates 'the feed died' from 'we killed it'.
        kill_all_active runs from the shutdown signal handler; if it kills without setting
        the flag, the watchdog reads the shutdown as a stall and spawns a replacement ffmpeg
        on the way out - an orphan writing to a segment the next process knows nothing
        about, which is the exact thing kill_all_active exists to prevent."""
        self.state.process = _sleeper()
        with mock.patch.object(cfgmod, 'load_config', return_value=self._cfg(30)), \
             mock.patch.object(recorder, '_launch_segment', self._stub_launch()):
            self.wd = WatchdogThread(self.rid, self.state, self.t.app)
            self.wd.start()
            time.sleep(2)  # let it poll a few times with a live process
            self.assertTrue(self.wd.is_alive(), 'watchdog exited before the shutdown kill')
            recorder.kill_all_active()
            self.wd.join(timeout=15)

        self.assertTrue(self.state.stop_event.is_set(),
                        'kill_all_active killed ffmpeg without setting stop_event')
        self.assertEqual(self.launched, [], 'a replacement segment was launched at shutdown')
        self.assertIsNone(self._stall_event(),
                          'the shutdown kill was recorded as a stream stall')


class RestartWaitTests(_WatchdogHarness):

    def test_the_restart_wait_is_handed_the_relaunched_process(self):
        """wait_for_file_data has always accepted proc= and documented that it returns False
        when the process exits before producing data; the watchdog simply never passed it, so
        a relaunch that died on connect also cost a full stall timeout to notice.

        Does not use _run_until_segment_closed: that helper stops the watchdog as soon as
        segment 0's ended_at is set, which happens as soon as the stall is recorded - before
        the restart branch reaches _launch_segment/wait_for_file_data at all. Racing the
        watchdog's own progress against the poll loop is what made this test flaky only
        under a loaded machine (dev/docs/BUGS.md 2026-08-15); waiting on the spy directly
        removes the race instead of relying on both sides finishing within 0.2s of each
        other."""
        self._exited_capture()
        relaunched = mock.Mock()
        relaunched.poll.return_value = None
        spy = mock.Mock(return_value=False)
        with mock.patch.object(wdmod, 'wait_for_file_data', spy), \
             mock.patch.object(cfgmod, 'load_config', return_value=self._cfg(30)), \
             mock.patch.object(recorder, '_launch_segment', self._stub_launch(relaunched)):
            self.wd = WatchdogThread(self.rid, self.state, self.t.app)
            self.wd.start()
            deadline = time.monotonic() + 45
            while time.monotonic() < deadline and not spy.called:
                time.sleep(0.05)
            self.state.stop_event.set()
            self.wd.join(timeout=15)

        self.assertTrue(spy.called, 'the restart never waited for data')
        self.assertIs(spy.call_args.kwargs.get('proc'), relaunched,
                      'wait_for_file_data was not given the relaunched process')


class ProcessExitedRenderTests(unittest.TestCase):
    """The recording detail page surface. A new exit_reason with no entry in the template's
    exit_pill map renders as a bare grey chip with no explanation, and the timeline's
    boundary de-dup gate only knew the one value."""

    def setUp(self):
        self.t = make_test_app()
        self.ctx = self.t.app.app_context()
        self.ctx.push()
        self.rec = seed.make_recording(status='COMPLETED')
        start = self.rec.start_time
        for num, reason in ((1, 'PROCESS_EXITED'), (2, 'STOP_TIME_REACHED')):
            db.session.add(RecordingSegment(
                recording_id=self.rec.id, segment_number=num,
                file_path=f'/dvr/x_seg_{num:03d}.ts',
                started_at=start + timedelta(minutes=num),
                ended_at=start + timedelta(minutes=num + 1),
                exit_reason=reason, bytes_recorded=2048, ffmpeg_exit_code=1))
        db.session.add(RecordingEvent(
            recording_id=self.rec.id, event_type=STALL_DETECTED, segment_number=1,
            detail='Capture process exited on its own at 2048 bytes - restarting',
            extra_data=json.dumps({'bytes': 2048, 'stall_reason': 'process_exited'})))
        db.session.commit()

    def tearDown(self):
        self.ctx.pop()
        self.t.cleanup()

    def _page(self):
        resp = self.t.client.get(f'/recordings/{self.rec.id}')
        self.assertEqual(resp.status_code, 200)
        return resp.get_data(as_text=True)

    def test_the_pill_is_explained_rather_than_rendered_bare(self):
        html = self._page()
        self.assertIn('PROCESS_EXITED', html)
        self.assertIn('exited on its own - ChannelBin did not kill it', html)

    def test_the_timeline_says_the_capture_exited_rather_than_that_we_killed_it(self):
        html = self._page()
        self.assertIn('Capture exited (seg 1)', html)
        self.assertNotIn('Stall (seg 1 killed)', html)

    def test_no_duplicate_segment_boundary_milestone(self):
        """The STALL_DETECTED event already marks this boundary; the gate at the segment
        loop suppresses a second marker, and it only knew STALL_KILLED."""
        self.assertNotIn('Segment boundary', self._page())


class WaitForFileDataContractTests(unittest.TestCase):
    """Characterization of the helper the fix leans on - it already behaved this way, and
    these fail only if someone changes the contract out from under the watchdog."""

    def test_a_dead_process_ends_the_wait_without_burning_the_timeout(self):
        proc = subprocess.Popen([sys.executable, '-c', 'raise SystemExit(1)'],
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        proc.wait(timeout=30)
        started = time.monotonic()
        self.assertFalse(wait_for_file_data(lambda: None, 30, proc=proc, poll_interval=0.1))
        self.assertLess(time.monotonic() - started, 5)

    def test_bytes_present_beat_a_dead_process(self):
        """Size is read before the proc-exit check, so data written just before death still
        counts as a successful start."""
        import tempfile
        fd, path = tempfile.mkstemp()
        os.write(fd, b'data')
        os.close(fd)
        self.addCleanup(os.unlink, path)
        proc = subprocess.Popen([sys.executable, '-c', 'raise SystemExit(1)'],
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        proc.wait(timeout=30)
        self.assertTrue(wait_for_file_data(lambda: path, 5, proc=proc, poll_interval=0.1))


if __name__ == '__main__':
    unittest.main()
