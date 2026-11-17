"""Stopping the app does not log an ERROR for a job the scheduler could no longer start.

Guards dev/docs/BUGS.md 2026-11-16 (restart) and dev/changelog/1591. run.py's signal handler
ended the main thread without telling the scheduler, so its thread went on dispatching into
a thread pool Python had already closed to new work: every job that came due in that window
logged "Error submitting job ... cannot schedule new futures after shutdown" at ERROR.

No database, no app: a real BackgroundScheduler on its default in-memory jobstore.
  python3 -m unittest tests.test_scheduler_stop
"""
import ast
import logging
import os
import sys
import unittest
from datetime import datetime, timedelta, timezone
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from apscheduler.schedulers.base import STATE_PAUSED, STATE_RUNNING  # noqa: E402

from app import scheduler as sched  # noqa: E402

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _noop():
    pass


class _SchedulerCase(unittest.TestCase):
    def setUp(self):
        self.scheduler = sched._GuardedScheduler()
        # Paused so the loop thread never runs a pass of its own: each test calls
        # _process_jobs() itself.
        self.scheduler.start(paused=True)
        self.addCleanup(self._shutdown)
        for name, value in (('_scheduler', self.scheduler), ('_stopping', False)):
            patcher = mock.patch.object(sched, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def _shutdown(self):
        if self.scheduler.running:
            self.scheduler.shutdown(wait=False)

    def _add_due_job(self):
        self.scheduler.add_job(_noop, 'date', id='due',
                               run_date=datetime.now(timezone.utc) - timedelta(seconds=5),
                               misfire_grace_time=None)

    def _running_without_waking_the_loop(self):
        """Mark the scheduler running without resume(), which wakes the loop thread: that
        thread would run its own pass beside the one the test calls, and take the job."""
        self.scheduler.state = STATE_RUNNING

    def _close_the_pool(self):
        """What the interpreter does to every ThreadPoolExecutor once the main thread exits."""
        self.scheduler._lookup_executor('default')._pool.shutdown(wait=False)


class StopTakingWorkTests(_SchedulerCase):
    def test_a_stopping_scheduler_submits_nothing(self):
        self._add_due_job()
        self._running_without_waking_the_loop()
        sched.stop_taking_work()
        self.assertEqual(STATE_PAUSED, self.scheduler.state)
        self._close_the_pool()
        with self.assertNoLogs('apscheduler', level='WARNING'):
            self.scheduler._process_jobs()
        self.assertIsNotNone(self.scheduler.get_job('due'),
                             'a job due while stopping must stay stored for the next start')

    def test_it_is_safe_with_no_scheduler(self):
        with mock.patch.object(sched, '_scheduler', None):
            sched.stop_taking_work()
        self.assertTrue(sched._stopping)


class SubmitErrorWhileStoppingTests(_SchedulerCase):
    """The pass that was already running when the stop arrived."""

    def _failed_submit(self):
        self._add_due_job()
        self._close_the_pool()
        self._running_without_waking_the_loop()
        with self.assertLogs('apscheduler.scheduler', level='INFO') as captured:
            self.scheduler._process_jobs()
        return [r for r in captured.records if 'Error submitting job' in r.getMessage()]

    def test_while_stopping_it_is_an_info_line_with_no_traceback(self):
        with mock.patch.object(sched, '_stopping', True):
            records = self._failed_submit()
        self.assertEqual(1, len(records))
        self.assertEqual(logging.INFO, records[0].levelno)
        self.assertEqual('INFO', records[0].levelname)
        self.assertIsNone(records[0].exc_info)
        self.assertIn('ChannelBin is stopping', records[0].getMessage())

    def test_while_running_it_is_still_an_error(self):
        records = self._failed_submit()
        self.assertEqual(1, len(records))
        self.assertEqual(logging.ERROR, records[0].levelno)
        self.assertIsNotNone(records[0].exc_info)

    def test_another_error_while_stopping_is_left_alone(self):
        with mock.patch.object(sched, '_stopping', True), \
                self.assertLogs('apscheduler.scheduler', level='ERROR') as captured:
            logging.getLogger('apscheduler.scheduler').error('Something else broke')
        self.assertEqual(logging.ERROR, captured.records[0].levelno)


class ShutdownHandlerWiringTests(unittest.TestCase):
    """Read statically because run.py builds the real app at import time."""

    def test_the_signal_handler_stops_the_scheduler_first(self):
        with open(os.path.join(_REPO_ROOT, 'run.py')) as fh:
            tree = ast.parse(fh.read())
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef) and node.name == '_handle_shutdown':
                calls = [n.value.func.id for n in node.body
                         if isinstance(n, ast.Expr) and isinstance(n.value, ast.Call)
                         and isinstance(n.value.func, ast.Name)]
                self.assertEqual('stop_taking_work', calls[0], calls)
                return
        self.fail('run.py has no _handle_shutdown')


if __name__ == '__main__':
    unittest.main()
