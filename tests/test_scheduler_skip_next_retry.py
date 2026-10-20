"""skip_next_run() writes the jobstore through the module's retry wrapper.

app/scheduler.py's rule: every jobstore write goes through a `retry_on_locked` wrapper,
because the jobstore's own engine can hit "database is locked" past busy_timeout and an
unretried failure 500s the calling route (seen live 2026-07-16). add_job and remove_job
complied; `skip_next_run()` called `_scheduler.modify_job()` directly, so a lock at that
instant 500ed the Jobs page's skip action (dev/changelog/1246).

No scheduler is started: the module global is replaced with a stand-in whose modify_job
raises the lock error once and then succeeds.
  python3 -m unittest tests.test_scheduler_skip_next_retry
"""
import os
import sys
import unittest
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest import mock

from sqlalchemy.exc import OperationalError

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import scheduler  # noqa: E402


class _Trigger:
    def get_next_fire_time(self, previous, now):
        return previous + timedelta(hours=1)


class _LockedOnceScheduler:
    def __init__(self, job):
        self.job = job
        self.modify_calls = []

    def get_job(self, job_id):
        return self.job

    def modify_job(self, job_id, **changes):
        self.modify_calls.append((job_id, changes))
        if len(self.modify_calls) == 1:
            raise OperationalError('UPDATE apscheduler_jobs', {},
                                   Exception('database is locked'))


class SkipNextRunRetriesTests(unittest.TestCase):

    def test_a_locked_jobstore_write_is_retried_and_the_skip_lands(self):
        first = datetime(2026, 1, 1, 12, tzinfo=timezone.utc)
        job = SimpleNamespace(next_run_time=first, trigger=_Trigger())
        fake = _LockedOnceScheduler(job)
        with mock.patch.object(scheduler, '_scheduler', fake):
            got = scheduler.skip_next_run('sync_account_1')
        self.assertEqual(got, first + timedelta(hours=1))
        self.assertEqual(len(fake.modify_calls), 2, 'the locked write was not retried')
        self.assertEqual(fake.modify_calls[-1][1], {'next_run_time': first + timedelta(hours=1)})

    def test_a_missing_job_is_still_none(self):
        class _NoJob:
            def get_job(self, job_id):
                return None
        with mock.patch.object(scheduler, '_scheduler', _NoJob()):
            self.assertIsNone(scheduler.skip_next_run('nope'))


if __name__ == '__main__':
    unittest.main(verbosity=2)
