"""Tier 2 - dead-stream fast-fail retries instead of abandoning the recording window.

Guards the backlog item worked as burndown-3 #12: recording 73 was scheduled for 85 minutes,
hit three early failures in 15s, and was marked FAILED for good with 84 minutes of its window
unused - even though the provider might have served the stream again minutes later. Design
decisions (2026-08-12): retry at 1/2/5/15 minutes then hourly (hardcoded cadence), a
configurable hard cap on total attempts (watchdog.dead_stream_max_retry_attempts, default 10)
independent of the recording's own window, a new RETRYING status distinct from IN_PROGRESS, and
retry only kicks in after group failover is exhausted or doesn't apply (the existing branch
structure in app/watchdog.py already guarantees this - failover is always tried first).

No network and no provider host: every child here is `sys.executable -c ...`, a local argv with
no URL in it, which tests/support/netguard.py permits. `persist_final_thumbnail` and
`apply_recording_health_observation` are stubbed out for the same reason the sibling
connection-release/failure-cause suites stub them - best-effort production code that reads
`load_config()`'s real `/dvr` defaults or blends into a channel's health score, neither of
which these tests assert on. Every harness runs under `make_test_app()`'s default
`start_scheduler=False`, so `schedule_dead_stream_retry` takes its documented no-scheduler
no-op path - that path itself is what `NoSchedulerGracefulNoopTests` asserts on directly; every
other test here only cares about the DB-side transition, not the APScheduler job.
"""
import json
import os
import subprocess
import sys
import threading
import time
import unittest
from datetime import datetime, timedelta
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import app.config as cfgmod  # noqa: E402
import app.recorder as recorder  # noqa: E402
import app.scheduler as sched  # noqa: E402
import app.watchdog as wdmod  # noqa: E402
from app import connection_limits as connlim  # noqa: E402
from app import db  # noqa: E402
from app.alerts import RECORDING_RETRYING  # noqa: E402
from app.database import (  # noqa: E402
    RECORDING_FAILED_DEAD_STREAM, RECORDING_RESUMED, RECORDING_RETRY_SCHEDULED,
    Alert, Recording, RecordingEvent,
)
from app.watchdog import (  # noqa: E402
    _dead_stream_retry_delay_minutes, finalize_dead_stream_retry_exhausted,
)
from tests.support import seed  # noqa: E402
from tests.support.app import make_test_app  # noqa: E402
from tests.support.watchdog import RestartHarness, sleeper  # noqa: E402


class RetryDelayScheduleTests(unittest.TestCase):
    """The hardcoded 1/2/5/15-then-hourly cadence (deliberately hardcoded, only the cap
    is configurable) - a pure function, no watchdog rig needed."""

    def test_first_four_attempts_follow_the_named_minutes(self):
        self.assertEqual([_dead_stream_retry_delay_minutes(n) for n in (1, 2, 3, 4)],
                         [1, 2, 5, 15])

    def test_fifth_attempt_and_beyond_is_hourly(self):
        self.assertEqual([_dead_stream_retry_delay_minutes(n) for n in (5, 6, 20)],
                         [60, 60, 60])

    def test_the_longest_wait_caps_the_ladder(self):
        """dev/changelog/1264: hourly waits checked prod recording 26's channel twice in
        the last two and a half hours of a live race; the default cap is 10 minutes."""
        self.assertEqual([_dead_stream_retry_delay_minutes(n, 10) for n in range(1, 8)],
                         [1, 2, 5, 10, 10, 10, 10])
        self.assertEqual([_dead_stream_retry_delay_minutes(n, 60) for n in (4, 5)], [15, 60])

    def test_the_default_cap_is_ten_minutes(self):
        from app.config import _DEFAULTS
        self.assertEqual(_DEFAULTS['watchdog']['dead_stream_retry_max_delay_minutes'], 10)


class _RetryHarness(RestartHarness):
    """Same rig as the sibling connection-release/failure-cause suites: a real account +
    channel so connlim.release() has something to resolve, and the health-score/thumbnail
    side effects of a give-up stubbed out."""

    _extra_watchdog_cfg = {}

    def setUp(self):
        super().setUp()
        acct = seed.make_account(name='Retry Test Account', max_connections=1)
        ch = seed.make_channel(acct, name='Retry Test Channel')
        db.session.commit()
        self.account_id = acct.id
        rec = db.session.get(Recording, self.rid)
        rec.channel_id = ch.id
        db.session.commit()

        connlim._holders.clear()
        self.assertTrue(connlim.try_acquire(self.account_id, 'recording', self.rid),
                        'setup could not acquire the slot the retry-scheduling is supposed '
                        'to release')

        self._extra_watchdog_cfg = {}
        self._thumb_patcher = mock.patch.object(recorder, 'persist_final_thumbnail')
        self._thumb_patcher.start()
        self._health_patcher = mock.patch(
            'app.health_score.apply_recording_health_observation')
        self._health_patcher.start()

    def tearDown(self):
        self._health_patcher.stop()
        self._thumb_patcher.stop()
        connlim._holders.clear()
        super().tearDown()

    def _cfg(self, stall_timeout, restart_delay=0):
        cfg = super()._cfg(stall_timeout, restart_delay)
        cfg['watchdog'].update(self._extra_watchdog_cfg)
        return cfg

    def _holders(self):
        return list(connlim._holders.get(self.account_id, []))

    def _last_event(self, event_type):
        return RecordingEvent.query.filter_by(
            recording_id=self.rid, event_type=event_type
        ).order_by(RecordingEvent.id.desc()).first()


class ScheduleRetryInsteadOfImmediateFailureTests(_RetryHarness):

    def test_dead_stream_trip_with_budget_and_window_left_enters_retrying_not_failed(self):
        self._extra_watchdog_cfg = {'early_fail_abort_count': 1,
                                    'dead_stream_max_retry_attempts': 3}
        self.state.process = sleeper()
        with mock.patch.object(recorder, 'failover_group_member', return_value=False):
            rec = self._run_until_event(RECORDING_RETRY_SCHEDULED, stall_timeout=1,
                                        restart_delay=0, produces_data=False)

        self.assertEqual(rec.status, 'RETRYING')
        self.assertEqual(rec.dead_stream_retry_count, 1)
        self.assertIsNotNone(rec.next_retry_at)
        self.assertGreater(rec.next_retry_at, datetime.utcnow(),
                           'next_retry_at should be in the future (first attempt = 1 minute)')
        self.assertLess(rec.next_retry_at, datetime.utcnow() + timedelta(minutes=2))

        evt = self._last_event(RECORDING_RETRY_SCHEDULED)
        self.assertIn('attempt 1/3', evt.detail)
        self.assertIn('1 min', evt.detail)
        # The streak and cause ride on the event so the recording page can word the trip
        # the way the alert does (app/recording_story.py, dev/changelog/1270).
        extra = json.loads(evt.extra_data)
        self.assertEqual(extra['streak'], 1)
        self.assertIn('cause', extra)

        self.assertIsNone(
            RecordingEvent.query.filter_by(
                recording_id=self.rid, event_type=RECORDING_FAILED_DEAD_STREAM).first(),
            'a scheduled retry must not also fire the terminal give-up event')

    def test_connection_slot_is_released_while_waiting_to_retry(self):
        self._extra_watchdog_cfg = {'early_fail_abort_count': 1,
                                    'dead_stream_max_retry_attempts': 3}
        self.state.process = sleeper()
        with mock.patch.object(recorder, 'failover_group_member', return_value=False):
            self._run_until_event(RECORDING_RETRY_SCHEDULED, stall_timeout=1,
                                  restart_delay=0, produces_data=False)

        self.assertEqual(
            self._holders(), [],
            'RETRYING must free the connection slot - nothing is capturing during the wait')
        self.assertTrue(connlim.try_acquire(self.account_id, 'recording', 99999),
                        'a different recording still could not get the slot')


class RetryPastTheWindowTests(_RetryHarness):
    """dev/changelog/1264: a retry that would come at or after the stop time is not
    scheduled. Prod recording 26's last trip came 27 minutes before its stop time and
    announced "Retrying (attempt 7/10) in 60 min", an attempt that could never fire
    (dev/docs/BUGS.md 2026-10-21 10:45 PM). The capture ends at the trip instead: joined
    when it captured anything, failed as a dead stream when it captured nothing."""

    def _window_ends_in(self, seconds):
        self._extra_watchdog_cfg = {'early_fail_abort_count': 1,
                                    'dead_stream_max_retry_attempts': 10}
        rec = db.session.get(Recording, self.rid)
        rec.stop_time = datetime.utcnow() + timedelta(seconds=seconds)  # 1 min retry is past it
        db.session.commit()

    def test_nothing_captured_fails_as_a_dead_stream_and_says_why(self):
        self._window_ends_in(40)
        with open(self.seg_path, 'wb'):
            pass  # the segment captured nothing
        self.state.process = sleeper()
        with mock.patch.object(recorder, 'failover_group_member', return_value=False):
            rec = self._run_until_event(RECORDING_FAILED_DEAD_STREAM, stall_timeout=1,
                                        restart_delay=0, produces_data=False)

        self.assertEqual(rec.status, 'FAILED')
        self.assertEqual(rec.dead_stream_retry_count, 0)
        self.assertIsNone(self._last_event(RECORDING_RETRY_SCHEDULED),
                          'a retry past the window must not be scheduled')
        self.assertIn('before another retry could come',
                      self._last_event(RECORDING_FAILED_DEAD_STREAM).detail)

    def test_a_capture_that_got_something_is_joined_now(self):
        self._window_ends_in(40)
        stopped = threading.Event()
        calls = []

        def _stop(app, recording_id, reason='STOP_TIME_REACHED', join_reason=None):
            calls.append((recording_id, reason, join_reason))
            stopped.set()

        self.state.process = sleeper()
        with mock.patch.object(recorder, 'failover_group_member', return_value=False), \
             mock.patch.object(recorder, 'stop_recording', side_effect=_stop), \
             mock.patch.object(cfgmod, 'load_config', return_value=self._cfg(1, 0)), \
             mock.patch.object(recorder, '_launch_segment',
                               self._stub_launch_next(produces_data=False)):
            self.wd = wdmod.WatchdogThread(self.rid, self.state, self.t.app)
            self.wd.start()
            self.assertTrue(stopped.wait(45), 'the capture was not ended at the trip')
            self.wd.join(timeout=15)

        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0][1], 'DEAD_STREAM_NO_RETRY')
        self.assertIn('before another retry could come', calls[0][2])
        db.session.expire_all()
        rec = db.session.get(Recording, self.rid)
        self.assertNotEqual(rec.status, 'RETRYING')
        self.assertIsNone(self._last_event(RECORDING_RETRY_SCHEDULED))

    def test_a_retry_inside_the_window_still_says_when(self):
        self._extra_watchdog_cfg = {'early_fail_abort_count': 1,
                                    'dead_stream_max_retry_attempts': 10}
        self.state.process = sleeper()
        with mock.patch.object(recorder, 'failover_group_member', return_value=False):
            self._run_until_event(RECORDING_RETRY_SCHEDULED, stall_timeout=1,
                                  restart_delay=0, produces_data=False)
        evt = self._last_event(RECORDING_RETRY_SCHEDULED)
        self.assertIn('Retrying (attempt 1/10) in 1 min', evt.detail)


class RetryingAlertTests(_RetryHarness):
    """dev/changelog/1264: a recording waiting on a dead stream raises one ERROR alert,
    and it comes down when the recording ends. Prod recording 26's only warning was
    dismissed when it started and nothing replaced it for three hours."""

    def _open(self):
        return Alert.query.filter_by(recording_id=self.rid, alert_type=RECORDING_RETRYING,
                                     dismissed_at=None).all()

    def test_entering_retrying_raises_one_error_alert(self):
        self._extra_watchdog_cfg = {'early_fail_abort_count': 1,
                                    'dead_stream_max_retry_attempts': 10}
        self.state.process = sleeper()
        with mock.patch.object(recorder, 'failover_group_member', return_value=False):
            rec = self._run_until_event(RECORDING_RETRY_SCHEDULED, stall_timeout=1,
                                        restart_delay=0, produces_data=False)
        rows = self._open()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].severity, 'ERROR')
        self.assertIn('not capturing anything', rows[0].body)
        self.assertIn('no more than 10 min apart', rows[0].body)

        # A second trip on the same outage raises nothing new.
        wdmod.raise_recording_retrying_alert(rec, 3, 10, 10, None, 60)
        self.assertEqual(len(self._open()), 1)

    def test_the_body_says_how_long_the_stream_must_keep_coming(self):
        """dev/changelog/1274 (first-time-user finding 27): the alert clears only once a segment
        grows past early_fail_window_seconds, so it stood about a minute after
        RESTART_SUCCEEDED on a page promising it clears itself. The body names the
        configured window, read by the watchdog, not a number of its own."""
        self._extra_watchdog_cfg = {'early_fail_abort_count': 1,
                                    'dead_stream_max_retry_attempts': 10,
                                    'early_fail_window_seconds': 47}
        self.state.process = sleeper()
        with mock.patch.object(recorder, 'failover_group_member', return_value=False):
            self._run_until_event(RECORDING_RETRY_SCHEDULED, stall_timeout=1,
                                  restart_delay=0, produces_data=False)
        rows = self._open()
        self.assertEqual(len(rows), 1)
        self.assertIn('kept coming for 47 s', rows[0].body)

    def test_the_recording_ending_clears_it(self):
        rec = db.session.get(Recording, self.rid)
        wdmod.raise_recording_retrying_alert(rec, 3, 1, 10, None, 60)
        self.assertEqual(len(self._open()), 1)
        with mock.patch('app.concatenator.do_concatenation'):
            recorder.stop_recording(self.t.app, self.rid)
        db.session.expire_all()
        self.assertEqual(self._open(), [])

    def test_a_segment_capturing_past_the_dead_stream_window_clears_it(self):
        """The stream came back: the segment is still growing once it can no longer count
        as an early failure, so "not capturing" would now be false."""
        rec = db.session.get(Recording, self.rid)
        wdmod.raise_recording_retrying_alert(rec, 3, 1, 10, None, 60)
        writer = subprocess.Popen(
            [sys.executable, '-c',
             'import sys, time\n'
             'while True:\n'
             '    open(sys.argv[1], "ab").write(b"G" * 4096); time.sleep(0.2)',
             self.seg_path], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.state.process = writer
        cfg = self._cfg(30, 0)
        cfg['watchdog']['early_fail_window_seconds'] = 2
        try:
            with mock.patch.object(cfgmod, 'load_config', return_value=cfg):
                self.wd = wdmod.WatchdogThread(self.rid, self.state, self.t.app)
                self.wd.start()
                deadline = time.monotonic() + 20
                while time.monotonic() < deadline and self._open():
                    time.sleep(0.25)
                    db.session.expire_all()
                self.state.stop_event.set()
                self.wd.join(timeout=15)
        finally:
            writer.kill()
            writer.wait(timeout=10)
        db.session.expire_all()
        self.assertEqual(self._open(), [])

    def test_aborting_clears_it(self):
        rec = db.session.get(Recording, self.rid)
        wdmod.raise_recording_retrying_alert(rec, 3, 1, 10, None, 60)
        recorder.abort_recording(self.t.app, self.rid)
        db.session.expire_all()
        self.assertEqual(self._open(), [])


class RetryBudgetExhaustedFallsThroughTests(_RetryHarness):

    def test_cap_already_reached_gives_up_immediately_instead_of_retrying_again(self):
        self._extra_watchdog_cfg = {'early_fail_abort_count': 1,
                                    'dead_stream_max_retry_attempts': 2}
        rec = db.session.get(Recording, self.rid)
        rec.dead_stream_retry_count = 2  # already at the cap
        db.session.commit()

        self.state.process = sleeper()
        with mock.patch.object(recorder, 'failover_group_member', return_value=False):
            rec = self._run_until_event(RECORDING_FAILED_DEAD_STREAM, stall_timeout=1,
                                        restart_delay=0, produces_data=False)

        self.assertEqual(rec.status, 'FAILED')
        self.assertEqual(rec.failure_reason, 'DEAD_STREAM_DETECTED')
        evt = self._last_event(RECORDING_FAILED_DEAD_STREAM)
        self.assertIn('2 retry attempt(s)', evt.detail,
                      'the terminal event should say how many retries were already tried')
        self.assertIsNone(
            RecordingEvent.query.filter_by(
                recording_id=self.rid, event_type=RECORDING_RETRY_SCHEDULED).first(),
            'the cap was already reached - no further retry should be scheduled')


class WindowExhaustedFallsThroughTests(_RetryHarness):

    def test_window_already_over_gives_up_immediately_even_with_retry_budget_left(self):
        self._extra_watchdog_cfg = {'early_fail_abort_count': 1,
                                    'dead_stream_max_retry_attempts': 10}
        rec = db.session.get(Recording, self.rid)
        rec.stop_time = datetime.utcnow() - timedelta(seconds=1)  # window already over
        db.session.commit()

        self.state.process = sleeper()
        with mock.patch.object(recorder, 'failover_group_member', return_value=False):
            rec = self._run_until_event(RECORDING_FAILED_DEAD_STREAM, stall_timeout=1,
                                        restart_delay=0, produces_data=False)

        self.assertEqual(rec.status, 'FAILED')
        self.assertIsNone(
            RecordingEvent.query.filter_by(
                recording_id=self.rid, event_type=RECORDING_RETRY_SCHEDULED).first(),
            'retrying past the recording\'s own scheduled window is pointless - no retry '
            'should be scheduled just because attempts remain')


class ZeroCapDisablesRetryTests(_RetryHarness):

    def test_max_retry_attempts_zero_behaves_like_before_the_feature(self):
        self._extra_watchdog_cfg = {'early_fail_abort_count': 1,
                                    'dead_stream_max_retry_attempts': 0}
        self.state.process = sleeper()
        with mock.patch.object(recorder, 'failover_group_member', return_value=False):
            rec = self._run_until_event(RECORDING_FAILED_DEAD_STREAM, stall_timeout=1,
                                        restart_delay=0, produces_data=False)

        self.assertEqual(rec.status, 'FAILED')
        self.assertEqual(rec.dead_stream_retry_count, 0)


class ResumeRecordingRetryingBranchTests(unittest.TestCase):
    """recorder.resume_recording()'s new RETRYING branch - the DB-transition half only,
    with _launch_segment stubbed so no real ffmpeg spawns."""

    def setUp(self):
        self.t = make_test_app()
        now = datetime.utcnow()
        self.rec = seed.make_recording(
            status='RETRYING', name='retry-resume', start_time=now - timedelta(minutes=5),
            stop_time=now + timedelta(hours=1), dead_stream_retry_count=2,
            next_retry_at=now + timedelta(seconds=5))
        db.session.commit()
        self.rid = self.rec.id

    def tearDown(self):
        self.t.cleanup()

    def test_resume_recording_clears_retrying_and_logs_resumed(self):
        with mock.patch.object(recorder, '_launch_segment'), \
             mock.patch.object(recorder, '_try_acquire_slot_with_preemption'):
            recorder.resume_recording(self.t.app, self.rid)

        db.session.expire_all()
        rec = db.session.get(Recording, self.rid)
        self.assertEqual(rec.status, 'IN_PROGRESS')
        self.assertIsNone(rec.next_retry_at)

        evt = RecordingEvent.query.filter_by(
            recording_id=self.rid, event_type=RECORDING_RESUMED
        ).order_by(RecordingEvent.id.desc()).first()
        self.assertIsNotNone(evt)
        self.assertIn('Retry attempt 2', evt.detail)


class FireDeadStreamRetryDispatchTests(unittest.TestCase):
    """recorder.fire_dead_stream_retry() - the retry_<id> job's target. Dispatch logic only,
    each real branch (resume vs. give up) mocked out so this stays a pure routing test."""

    def setUp(self):
        self.t = make_test_app()

    def tearDown(self):
        self.t.cleanup()

    def _make(self, status='RETRYING', stop_delta=timedelta(hours=1)):
        now = datetime.utcnow()
        rec = seed.make_recording(status=status, name='retry-fire',
                                  start_time=now - timedelta(minutes=5),
                                  stop_time=now + stop_delta, dead_stream_retry_count=1)
        db.session.commit()
        return rec.id

    def test_nonexistent_recording_is_a_silent_noop(self):
        with mock.patch.object(recorder, 'resume_recording') as resume, \
             mock.patch.object(wdmod, 'finalize_dead_stream_retry_exhausted') as finalize:
            recorder.fire_dead_stream_retry(self.t.app, 999999)
        resume.assert_not_called()
        finalize.assert_not_called()

    def test_recording_moved_on_is_a_noop(self):
        rid = self._make(status='ABORTED')
        with mock.patch.object(recorder, 'resume_recording') as resume, \
             mock.patch.object(wdmod, 'finalize_dead_stream_retry_exhausted') as finalize:
            recorder.fire_dead_stream_retry(self.t.app, rid)
        resume.assert_not_called()
        finalize.assert_not_called()

    def test_window_still_open_resumes(self):
        rid = self._make(status='RETRYING', stop_delta=timedelta(hours=1))
        with mock.patch.object(recorder, 'resume_recording') as resume:
            recorder.fire_dead_stream_retry(self.t.app, rid)
        resume.assert_called_once_with(self.t.app, rid)

    def test_window_ended_during_the_wait_gives_up_instead_of_resuming(self):
        rid = self._make(status='RETRYING', stop_delta=timedelta(seconds=-1))
        with mock.patch.object(recorder, 'resume_recording') as resume, \
             mock.patch(
                 'app.watchdog.finalize_dead_stream_retry_exhausted') as finalize:
            recorder.fire_dead_stream_retry(self.t.app, rid)
        resume.assert_not_called()
        finalize.assert_called_once()
        self.assertEqual(finalize.call_args.args[1], rid)


class FinalizeDeadStreamRetryExhaustedTests(unittest.TestCase):
    """watchdog.finalize_dead_stream_retry_exhausted() - the give-up path reached when a
    RETRYING recording's window ends before its next attempt fires."""

    def setUp(self):
        self.t = make_test_app()
        self._thumb_patcher = mock.patch.object(recorder, 'persist_final_thumbnail')
        self._thumb_patcher.start()
        self._health_patcher = mock.patch(
            'app.health_score.apply_recording_health_observation')
        self._health_patcher.start()

    def tearDown(self):
        self._health_patcher.stop()
        self._thumb_patcher.stop()
        self.t.cleanup()

    def test_marks_failed_dead_stream_with_the_attempt_count_and_cause(self):
        now = datetime.utcnow()
        rec = seed.make_recording(status='RETRYING', name='finalize-exhausted',
                                  start_time=now - timedelta(minutes=10),
                                  stop_time=now - timedelta(seconds=1),
                                  dead_stream_retry_count=4, next_retry_at=now)
        db.session.commit()
        rid = rec.id

        with self.t.app.app_context():
            finalize_dead_stream_retry_exhausted(self.t.app, rid, cause='window ended')

        db.session.expire_all()
        rec = db.session.get(Recording, rid)
        self.assertEqual(rec.status, 'FAILED')
        self.assertEqual(rec.failure_reason, 'DEAD_STREAM_DETECTED')
        self.assertIsNone(rec.next_retry_at)

        evt = RecordingEvent.query.filter_by(
            recording_id=rid, event_type=RECORDING_FAILED_DEAD_STREAM
        ).order_by(RecordingEvent.id.desc()).first()
        self.assertIsNotNone(evt)
        self.assertIn('4 retry attempt(s)', evt.detail)
        self.assertIn('window ended', evt.detail)

    def test_noop_if_the_recording_already_moved_on(self):
        now = datetime.utcnow()
        rec = seed.make_recording(status='ABORTED', name='finalize-moved-on',
                                  start_time=now - timedelta(minutes=10),
                                  stop_time=now - timedelta(seconds=1))
        db.session.commit()
        rid = rec.id

        with self.t.app.app_context():
            finalize_dead_stream_retry_exhausted(self.t.app, rid)

        db.session.expire_all()
        rec = db.session.get(Recording, rid)
        self.assertEqual(rec.status, 'ABORTED', 'must not overwrite a status it did not cause')
        self.assertIsNone(
            RecordingEvent.query.filter_by(
                recording_id=rid, event_type=RECORDING_FAILED_DEAD_STREAM).first())


class UnscheduleRecordingCancelsRetryJobTests(unittest.TestCase):
    """A pending retry_<id> job must be cancelled the same way start_<id>/stop_<id>/
    precheck_<id> already are - unschedule_recording is the one call every abort path
    already makes, so no bespoke cancel code should be needed."""

    def setUp(self):
        self.t = make_test_app(start_scheduler=True)

    def tearDown(self):
        self.t.cleanup()

    def test_unschedule_recording_removes_a_pending_retry_job(self):
        now = datetime.utcnow()
        rec = seed.make_recording(status='RETRYING', name='cancel-retry-job',
                                  start_time=now - timedelta(minutes=5),
                                  stop_time=now + timedelta(hours=1))
        db.session.commit()
        rid = rec.id

        sched.schedule_dead_stream_retry(rid, now + timedelta(minutes=1))
        self.assertIsNotNone(sched.get_scheduler().get_job(f'retry_{rid}'))

        sched.unschedule_recording(rid)

        self.assertIsNone(sched.get_scheduler().get_job(f'retry_{rid}'))


class NoSchedulerGracefulNoopTests(unittest.TestCase):
    """schedule_dead_stream_retry must not crash a watchdog thread when no scheduler is
    running (start_scheduler=False, the suite's default) - same defensive shape as the
    pre-existing remove_job_if_exists. A live production app always has a scheduler by the
    time any recording can reach RETRYING; this is purely a test-harness compatibility guard."""

    def setUp(self):
        self.t = make_test_app()  # start_scheduler=False

    def tearDown(self):
        self.t.cleanup()

    def test_no_scheduler_logs_a_warning_and_does_not_raise(self):
        self.assertIsNone(sched._scheduler)
        with self.assertLogs('app.scheduler', level='WARNING') as logs:
            sched.schedule_dead_stream_retry(12345, datetime.utcnow() + timedelta(minutes=1))
        self.assertTrue(any('retry_12345' in line for line in logs.output))


if __name__ == '__main__':
    unittest.main(verbosity=2)
