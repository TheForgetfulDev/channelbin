"""The APScheduler loop thread survives a failed pass, and a dead one is reported.

Guards dev/docs/BUGS.md 2026-09-23 @ 09:51:18 PM ET: APScheduler 3.11.2's _process_jobs()
guards only its get_due_jobs() read. The jobstore write it makes after handing a job to a
worker is unguarded and _main_loop() has no try, so a "database is locked" that outlasts
busy_timeout at that moment ended the loop thread - while scheduler.running stayed True,
readiness kept saying "Running, with N jobs registered", and nothing scheduled ever fired
again. Measured on the reference box before the fix: a write lock held 14s across the
bookkeeping killed the thread, and a job registered afterwards never ran.

Three layers are covered here, each on its own:
  * _RetryingJobStore retries the two writes APScheduler makes on its own, the way every
    jobstore write this app makes already is.
  * _GuardedScheduler keeps the loop alive through whatever gets past that, raises a
    SCHEDULER_PASS_FAILED alert, and treats a failure after shutdown() flipped the state as
    the shutdown race it is - one INFO line, no alert.
  * scheduler_is_live() asks the thread, readiness reports a dead one as a PROBLEM, and
    the SCHEDULER_STOPPED alert a dying loop raises is dismissed by the next start.

Runs against a throwaway temp SQLite DB - never the live dvr.db. Every job callable is
module-level because APScheduler pickles a textual reference into the jobstore.
    python3 -m unittest tests.test_scheduler_liveness
"""
import logging
import os
import sqlite3
import sys
import threading
import time
import unittest
from datetime import datetime
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from apscheduler.jobstores.base import JobLookupError  # noqa: E402
from apscheduler.jobstores.sqlalchemy import SQLAlchemyJobStore  # noqa: E402
from apscheduler.schedulers.background import BackgroundScheduler  # noqa: E402
from apscheduler.schedulers.base import STATE_RUNNING, STATE_STOPPED  # noqa: E402
from sqlalchemy.exc import OperationalError  # noqa: E402

from app import db, readiness  # noqa: E402
from app import scheduler as sched  # noqa: E402
from app.alerts import SCHEDULER_PASS_FAILED, SCHEDULER_STOPPED  # noqa: E402
from app.database import Alert  # noqa: E402
from tests.support.app import make_test_app  # noqa: E402

_JOB_ID = 'liveness_probe'
_runs = []
_ran = threading.Event()


def _probe_job():
    _runs.append(time.monotonic())
    _ran.set()


def _locked_error():
    return OperationalError('DELETE FROM apscheduler_jobs WHERE id = ?', (_JOB_ID,),
                            sqlite3.OperationalError('database is locked'))


def _failing_remove(fail_times):
    """A stand-in for SQLAlchemyJobStore.remove_job: the probe job's removal raises a
    locked error `fail_times` times and then goes through to the real DELETE. Any other
    job id (the system jobs a live scheduler registers) is untouched. A plain function
    rather than a callable object so it binds as a method when patched onto the class."""
    real = SQLAlchemyJobStore.remove_job
    state = {'calls': 0}

    def remove_job(store, job_id):
        if job_id != _JOB_ID:
            return real(store, job_id)
        state['calls'] += 1
        if state['calls'] <= fail_times:
            raise _locked_error()
        return real(store, job_id)

    remove_job.state = state
    return remove_job


def _wait_until(predicate, timeout, what):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.05)
    raise AssertionError(f'gave up after {timeout}s waiting for {what}')


class _LiveSchedulerCase(unittest.TestCase):
    def setUp(self):
        _runs.clear()
        _ran.clear()
        self.t = make_test_app(start_scheduler=True)
        self.ctx = self.t.app.app_context()
        self.ctx.push()
        self.scheduler = sched.get_scheduler()

    def tearDown(self):
        self.ctx.pop()
        self.t.cleanup()

    def _job_gone(self):
        return self.scheduler.get_job(_JOB_ID) is None

    def _open_alerts(self, alert_type):
        db.session.expire_all()
        return Alert.query.filter_by(alert_type=alert_type, dismissed_at=None).all()


class RetryingJobStoreTests(_LiveSchedulerCase):
    def test_the_live_store_is_the_retrying_one(self):
        store = self.scheduler._lookup_jobstore('default')
        self.assertIsInstance(store, sched._RetryingJobStore)
        self.assertIsInstance(self.scheduler, sched._GuardedScheduler)

    def test_a_locked_post_dispatch_write_is_retried_and_the_loop_never_notices(self):
        failing = _failing_remove(fail_times=1)
        with mock.patch.object(SQLAlchemyJobStore, 'remove_job', failing):
            sched._add_job(func=_probe_job, trigger='date', run_date=datetime.utcnow(),
                           id=_JOB_ID, replace_existing=True)
            self.assertTrue(_ran.wait(timeout=5), 'the probe job never ran')
            _wait_until(self._job_gone, 5, 'the one-shot job to be removed after its run')

        self.assertEqual(failing.state['calls'], 2,
                         'one locked DELETE, one retry that went through')
        self.assertTrue(sched.scheduler_is_live())
        self.assertEqual(self._open_alerts(SCHEDULER_PASS_FAILED), [],
                         'a retry that succeeded is not a failed pass')


class GuardedLoopTests(_LiveSchedulerCase):
    def test_a_write_that_outlasts_every_retry_does_not_kill_the_loop(self):
        # Five attempts is retry_on_locked's ceiling; the sixth call is the retry pass the
        # guard schedules, and it goes through so the test can see the loop recover.
        failing = _failing_remove(fail_times=5)
        self.scheduler.jobstore_retry_interval = 0.5
        with mock.patch.object(SQLAlchemyJobStore, 'remove_job', failing):
            sched._add_job(func=_probe_job, trigger='date', run_date=datetime.utcnow(),
                           id=_JOB_ID, replace_existing=True)
            self.assertTrue(_ran.wait(timeout=5), 'the probe job never ran')
            _wait_until(lambda: bool(self._open_alerts(SCHEDULER_PASS_FAILED)), 15,
                        'the SCHEDULER_PASS_FAILED alert')
            _wait_until(self._job_gone, 10, 'the retry pass to remove the job')

        self.assertTrue(self.scheduler._thread.is_alive(),
                        'the loop thread died on a post-dispatch write - nothing scheduled '
                        'would ever fire again')
        self.assertTrue(sched.scheduler_is_live())
        self.assertGreaterEqual(failing.state['calls'], 6)
        [alert] = self._open_alerts(SCHEDULER_PASS_FAILED)
        self.assertEqual(alert.severity, 'ERROR')
        self.assertIn('database is locked', alert.body)
        self.assertIn('may run a second time', alert.body)
        self.assertEqual(self._open_alerts('LOG_ERROR'), [],
                         'the ERROR log line is marked already_alerted, so the log bridge '
                         'must not raise a second row for it')


class ShutdownRaceTests(unittest.TestCase):
    """The state-STOPPED branch, on a scheduler that is never started: shutdown() flips
    the state before it takes the jobstore lock, so a pass already past its dispatch has
    remove_job() raise JobLookupError. That is the traceback the v0.13.0 CI run and the
    2026-09-23 local loop printed inside green runs."""

    def setUp(self):
        self.scheduler = sched._GuardedScheduler()
        self.raised = []
        self.alerts = []

        def _raise_next():
            raise self.raised.pop()

        self.patches = [
            mock.patch.object(BackgroundScheduler, '_process_jobs', side_effect=_raise_next),
            mock.patch.object(sched, '_alert_from_scheduler_thread',
                              side_effect=lambda t, **kw: self.alerts.append((t, kw))),
        ]
        for p in self.patches:
            p.start()

    def tearDown(self):
        for p in self.patches:
            p.stop()

    def test_a_failure_after_shutdown_is_one_info_line_and_no_alert(self):
        self.scheduler.state = STATE_STOPPED
        self.raised.append(JobLookupError('drain_probe'))
        with self.assertLogs('app.scheduler', level='INFO') as logs:
            self.assertIsNone(self.scheduler._process_jobs())
        self.assertEqual([r.levelno for r in logs.records], [logging.INFO])
        self.assertIn('interrupted by shutdown', logs.output[0])
        self.assertEqual(self.alerts, [])

    def test_a_failure_while_running_alerts_and_schedules_a_retry(self):
        self.scheduler.state = STATE_RUNNING
        self.raised.append(_locked_error())
        with self.assertLogs('app.scheduler', level='ERROR') as logs:
            self.assertEqual(self.scheduler._process_jobs(),
                             self.scheduler.jobstore_retry_interval)
        [record] = logs.records
        self.assertTrue(record.already_alerted,
                        'the typed alert below is the one row; the log bridge must not add one')
        self.assertIsNotNone(record.exc_info, 'the traceback is what a person needs next')
        [(alert_type, kw)] = self.alerts
        self.assertEqual(alert_type, SCHEDULER_PASS_FAILED)
        self.assertIn('database is locked', kw['body'])

    def test_a_loop_that_exits_on_an_error_says_so_and_still_exits(self):
        with mock.patch.object(BackgroundScheduler, '_main_loop',
                               side_effect=RuntimeError('loop broke')), \
                self.assertLogs('app.scheduler', level='CRITICAL') as logs, \
                self.assertRaises(RuntimeError):
            self.scheduler._main_loop()
        [record] = logs.records
        self.assertTrue(record.already_alerted)
        [(alert_type, kw)] = self.alerts
        self.assertEqual(alert_type, SCHEDULER_STOPPED)
        self.assertEqual(kw['source'], 'scheduler')
        self.assertIn('until ChannelBin restarts', kw['body'])


class DeadThreadIsReportedTests(_LiveSchedulerCase):
    def setUp(self):
        super().setUp()
        self.t.sandbox_output_dirs()

    def _finished_thread(self):
        t = threading.Thread(target=lambda: None)
        t.start()
        t.join()
        return t

    def test_a_live_loop_reads_as_live(self):
        self.assertTrue(sched.scheduler_is_live())
        ctx = readiness.Context()
        self.assertTrue(ctx.scheduler_running)
        self.assertTrue(ctx.scheduler_thread_alive)
        self.assertEqual(readiness._check_scheduler(ctx).status, readiness.READY)

    def test_a_dead_loop_is_a_problem_even_while_running_says_true(self):
        with mock.patch.object(self.scheduler, '_thread', self._finished_thread()):
            self.assertTrue(self.scheduler.running,
                            'precondition: the flag is what used to be asked, and it lies')
            self.assertFalse(sched.scheduler_is_live())
            ctx = readiness.Context()
            self.assertTrue(ctx.scheduler_running)
            self.assertFalse(ctx.scheduler_thread_alive)
            result = readiness._check_scheduler(ctx)
        self.assertEqual(result.status, readiness.PROBLEM)
        self.assertIn('thread has died', result.found)
        self.assertIn('until ChannelBin restarts', result.found)

    def test_no_scheduler_at_all_reads_as_not_live(self):
        with mock.patch.object(sched, '_scheduler', None):
            self.assertFalse(sched.scheduler_is_live())

    def test_the_activity_chip_drops_the_upcoming_jobs_of_a_dead_loop(self):
        from datetime import timedelta
        # A named system job inside the chip's one-hour window, so the live answer and
        # the dead answer differ on this seed.
        sched._add_job(func=_probe_job, trigger='date',
                       run_date=datetime.utcnow() + timedelta(minutes=5),
                       id='config_backup_daily', replace_existing=True)

        def background():
            resp = self.t.client.get('/api/activity/status')
            self.assertEqual(resp.status_code, 200)
            return resp.get_json()['background']

        live = background()
        self.assertEqual(live['next_scheduled']['label'], 'Config Backup')
        self.assertEqual(live['state'], 'dim')

        with mock.patch.object(self.scheduler, '_thread', self._finished_thread()):
            dead = background()
        self.assertIsNone(dead['next_scheduled'])
        self.assertEqual(dead['state'], 'hidden')


class StoppedAlertLifecycleTests(unittest.TestCase):
    def test_the_dying_thread_writes_a_real_row_from_outside_any_context(self):
        t = make_test_app(start_scheduler=True)
        try:
            worker = threading.Thread(
                target=sched._alert_from_scheduler_thread,
                args=(SCHEDULER_STOPPED,),
                kwargs={'title': 'The scheduler has stopped', 'body': 'b',
                        'source': 'scheduler'})
            worker.start()
            worker.join(timeout=10)
            self.assertFalse(worker.is_alive())
            with t.app.app_context():
                [row] = Alert.query.filter_by(alert_type=SCHEDULER_STOPPED).all()
                self.assertEqual(row.source, 'scheduler')
                self.assertIsNone(row.dismissed_at)
        finally:
            t.cleanup()

    def test_the_next_start_dismisses_the_standing_row(self):
        with mock.patch('app.alerts.dismiss_open_alerts') as dismiss:
            t = make_test_app(start_scheduler=True)
            try:
                dismiss.assert_any_call(SCHEDULER_STOPPED, 'scheduler')
            finally:
                t.cleanup()


if __name__ == '__main__':
    unittest.main()
