"""Guards dev/docs/BUGS.md 2026-08-14 04:10 "A launch failure at recording start leaves an
IN_PROGRESS zombie with no watchdog and no retry".

When subprocess.Popen itself fails, _launch_segment used to increment consecutive_failures
and return. Below the give-up threshold that left no process, no segment row, and - because
state.current_segment_num only advances after a successful spawn - nothing for the watchdog
to key off. On the first launch there is no watchdog at all, so the recording sat IN_PROGRESS
with nothing capturing until its stop-time job fired and concatenation ran over zero segments.

The mid-recording case is not the safe one the original report assumed: the watchdog's three
failover call sites relaunch and then break to the outer loop, which re-reads the previous,
already-ended segment and spins on a 1s wait forever.

No real ffmpeg and no network: subprocess.Popen is patched, and everything is written under
the test's own temp dir. Runtime config goes through TestApp.sandbox_config() because
_launch_segment calls load_config() itself, which make_test_app()'s overrides do not reach
(CLAUDE.md, Testing).
  python3 -m unittest tests.test_launch_failure_retry
"""
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tests.support.app import make_test_app  # noqa: E402
from tests.support import seed  # noqa: E402

from app import db  # noqa: E402
from app import recorder  # noqa: E402
from app.database import (  # noqa: E402
    Recording, RecordingEvent, RecordingProfile, RecordingSegment,
    RESTART_ATTEMPTED, RESTART_FAILED, RECORDING_FAILED,
)


def _fake_proc(pid=4242):
    proc = mock.MagicMock()
    proc.pid = pid
    proc.poll.return_value = 0
    return proc


class _LaunchRetryTestCase(unittest.TestCase):
    """A temp dvr_output_dir every runtime load_config() call sees, plus a profile whose
    watchdog overrides drive the retry cadence without patching config at all."""

    # Subclasses override before setUp runs its profile insert.
    restart_delay = 0
    max_failures = 10

    def setUp(self):
        self.t = make_test_app()
        self.dvr = os.path.join(self.t._tmpdir, 'dvr')
        os.makedirs(self.dvr, exist_ok=True)
        self.t.sandbox_config({'recording': {
            'dvr_output_dir': self.dvr,
            'capture_log_dir': os.path.join(self.t._tmpdir, 'caplogs'),
            'live_thumbnail': {'enabled': False},
        }})
        self.profile = RecordingProfile(
            name='fast-retry',
            restart_delay_seconds=self.restart_delay,
            max_consecutive_failures=self.max_failures,
        )
        db.session.add(self.profile)
        db.session.flush()

    def tearDown(self):
        self.t.cleanup()

    def _recording(self, status='SCHEDULED'):
        """A URL-only recording: channel_id None keeps connection-slot acquisition and
        channel-health snapshotting out of the way of what these tests assert."""
        rec = seed.make_recording(status=status, name='Launch Failure',
                                  profile_id=self.profile.id)
        db.session.commit()
        return rec

    def _join_retry(self, recording_id, timeout=5):
        """Wait for the pending relaunch, then quiesce whatever it started.

        The quiesce half is load-bearing, not tidiness. A successful relaunch starts a
        WatchdogThread, and these tests hand it a process whose poll() reports it has
        already exited - so it correctly sees a dead capture and relaunches, which is a
        second writer to popen.call_count and to the event log. Measured here: the count
        is 2 the instant the retry thread joins and 4 a second later, so the assertions
        below pass or fail on how fast the machine is (dev/changelog/903). What is under
        test is the retry, not what the watchdog does after it succeeds.
        """
        # getattr, so that with the fix reverted these tests fail on the behavior they
        # assert rather than on RecordingState not carrying the field yet.
        state = recorder.get_state(recording_id)
        thread = getattr(state, 'launch_retry', None) if state is not None else None
        if thread is not None:
            thread.join(timeout=timeout)
        state = recorder.get_state(recording_id)
        if state is not None:
            state.stop_event.set()
            watchdog = getattr(state, 'watchdog', None)
            if watchdog is not None:
                watchdog.join(timeout=timeout)
        return thread

    def _events(self, recording_id, event_type):
        return [e for e in RecordingEvent.query.filter_by(
            recording_id=recording_id, event_type=event_type).all()]


class LaunchFailureAtStartTests(_LaunchRetryTestCase):
    def test_first_launch_failure_is_retried_and_the_capture_starts(self):
        """The headline defect: one failed spawn used to end the recording's capture for
        good while the row stayed IN_PROGRESS."""
        rec = self._recording()
        rid = rec.id
        with mock.patch.object(recorder.subprocess, 'Popen',
                               side_effect=[OSError('ffmpeg not found'), _fake_proc()]) as popen:
            recorder.start_recording(self.t.app, rid)
            self._join_retry(rid)

        self.assertEqual(popen.call_count, 2, 'the failed spawn was never retried')
        db.session.expire_all()
        self.assertEqual(db.session.get(Recording, rid).status, 'IN_PROGRESS')
        segs = RecordingSegment.query.filter_by(recording_id=rid).all()
        self.assertEqual([s.segment_number for s in segs], [1],
                         'the retry must relaunch the SAME segment number, not skip one')

    def test_the_retry_leaves_the_recording_watched(self):
        """A retry that captures but starts no watchdog would strand the recording the
        next time the feed stalled."""
        rec = self._recording()
        rid = rec.id
        with mock.patch.object(recorder.subprocess, 'Popen',
                               side_effect=[OSError('boom'), _fake_proc()]):
            recorder.start_recording(self.t.app, rid)
            self._join_retry(rid)

        state = recorder.get_state(rid)
        self.assertIsNotNone(state)
        self.assertEqual(state.current_segment_num, 1)
        self.assertIsNotNone(state.watchdog, 'no watchdog was started by the retry')

    def test_the_pending_retry_is_announced(self):
        """Product Principle 1: an IN_PROGRESS recording with no process must say why and
        say that a relaunch is coming."""
        rec = self._recording()
        rid = rec.id
        with mock.patch.object(recorder.subprocess, 'Popen',
                               side_effect=[OSError('ffmpeg not found'), _fake_proc()]):
            recorder.start_recording(self.t.app, rid)
            self._join_retry(rid)

        failures = self._events(rid, RESTART_FAILED)
        self.assertEqual(len(failures), 1)
        self.assertIn('ffmpeg not found', failures[0].detail)
        self.assertEqual(failures[0].segment_number, 1)

        attempts = self._events(rid, RESTART_ATTEMPTED)
        self.assertEqual(len(attempts), 1, 'no event announced the pending retry')
        self.assertIn('retrying the launch', attempts[0].detail)
        self.assertIn('segment 1', attempts[0].detail)

    def test_resume_also_retries(self):
        """resume_recording is the other launch site with no watchdog running yet."""
        rec = self._recording(status='IN_PROGRESS')
        rid = rec.id
        with mock.patch.object(recorder.subprocess, 'Popen',
                               side_effect=[OSError('boom'), _fake_proc()]) as popen:
            recorder.resume_recording(self.t.app, rid)
            self._join_retry(rid)

        self.assertEqual(popen.call_count, 2)
        self.assertEqual(
            [s.segment_number for s in RecordingSegment.query.filter_by(recording_id=rid).all()],
            [1])

    def test_a_mid_recording_relaunch_failure_is_retried_too(self):
        """The watchdog's failover call sites relaunch and break to an outer loop that
        cannot see a segment which was never created, so they strand the same way."""
        rec = self._recording(status='IN_PROGRESS')
        rid = rec.id
        state = recorder.RecordingState(current_segment_num=6)
        with recorder._lock:
            recorder._active[rid] = state

        with mock.patch.object(recorder.subprocess, 'Popen',
                               side_effect=[OSError('boom'), _fake_proc()]) as popen:
            recorder._launch_segment(self.t.app, rid, seg_num=7)
            self._join_retry(rid)

        self.assertEqual(popen.call_count, 2)
        self.assertEqual(
            [s.segment_number for s in RecordingSegment.query.filter_by(recording_id=rid).all()],
            [7])


class LaunchFailureGiveUpTests(_LaunchRetryTestCase):
    max_failures = 1

    def test_reaching_the_threshold_fails_the_recording_and_schedules_nothing(self):
        rec = self._recording()
        rid = rec.id
        with mock.patch.object(recorder.subprocess, 'Popen',
                               side_effect=OSError('ffmpeg not found')) as popen:
            recorder.start_recording(self.t.app, rid)

        self.assertEqual(popen.call_count, 1, 'a terminal failure must not be retried')
        self.assertIsNone(recorder.get_state(rid), 'live state was left behind')
        db.session.expire_all()
        self.assertEqual(db.session.get(Recording, rid).status, 'FAILED')
        self.assertEqual(len(self._events(rid, RECORDING_FAILED)), 1)
        self.assertEqual(self._events(rid, RESTART_ATTEMPTED), [],
                         'a terminal failure must not announce a retry it will never make')

    def test_the_profile_threshold_is_what_decides(self):
        """max_consecutive_failures used to come straight from config.yaml here, so a
        recording profile's override applied to stalls but not to failed spawns."""
        rec = self._recording()
        rid = rec.id
        from app.config import load_config
        self.assertGreater(load_config()['watchdog']['max_consecutive_failures'],
                           self.max_failures,
                           'the global default must differ, or this proves nothing')
        with mock.patch.object(recorder.subprocess, 'Popen', side_effect=OSError('boom')):
            recorder.start_recording(self.t.app, rid)

        db.session.expire_all()
        self.assertEqual(db.session.get(Recording, rid).status, 'FAILED')


class LaunchRetryGuardTests(_LaunchRetryTestCase):
    """_schedule_launch_retry's own preconditions, driven directly so no timing races
    decide the result."""

    def _live(self, status='IN_PROGRESS'):
        rec = self._recording(status=status)
        state = recorder.RecordingState(current_segment_num=2)
        with recorder._lock:
            recorder._active[rec.id] = state
        return rec.id, state

    def test_teardown_during_the_wait_cancels_the_retry(self):
        rid, state = self._live()
        with mock.patch.object(recorder, '_launch_segment') as launch:
            recorder._schedule_launch_retry(self.t.app, rid, 3, delay=30)
            state.stop_event.set()
            state.launch_retry.join(timeout=5)
            self.assertFalse(state.launch_retry.is_alive(),
                             'the retry slept through teardown instead of waiting on stop_event')
            launch.assert_not_called()

    def test_nothing_is_scheduled_once_teardown_has_already_run(self):
        rid, state = self._live()
        state.stop_event.set()
        with mock.patch.object(recorder, '_launch_segment') as launch:
            recorder._schedule_launch_retry(self.t.app, rid, 3, delay=0)
            self.assertIsNone(state.launch_retry)
            launch.assert_not_called()

    def test_a_recording_that_left_in_progress_is_not_relaunched(self):
        rid, state = self._live()
        db.session.get(Recording, rid).status = 'ABORTED'
        db.session.commit()
        with mock.patch.object(recorder, '_launch_segment') as launch:
            recorder._schedule_launch_retry(self.t.app, rid, 3, delay=0)
            state.launch_retry.join(timeout=5)
            launch.assert_not_called()

    def test_a_replaced_state_object_is_not_relaunched_against(self):
        """A recording torn down and resumed under the waiting thread gets a fresh
        RecordingState; attaching the new process to the stale one hides it from every
        reader, including teardown."""
        rid, state = self._live()
        with mock.patch.object(recorder, '_launch_segment') as launch:
            recorder._schedule_launch_retry(self.t.app, rid, 3, delay=0.2)
            with recorder._lock:
                recorder._active[rid] = recorder.RecordingState(current_segment_num=9)
            state.launch_retry.join(timeout=5)
            launch.assert_not_called()

    def test_the_happy_path_relaunches_the_same_segment(self):
        rid, state = self._live()
        with mock.patch.object(recorder, '_launch_segment') as launch:
            recorder._schedule_launch_retry(self.t.app, rid, 3, delay=0)
            state.launch_retry.join(timeout=5)
            launch.assert_called_once_with(self.t.app, rid, 3)


if __name__ == '__main__':
    unittest.main()
