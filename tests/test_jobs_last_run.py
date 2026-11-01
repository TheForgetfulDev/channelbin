"""/jobs says how each job's last run went, not only when the next one is (dev/changelog/1351).

Every row was a next run; a daily backup that had failed for a week looked exactly like one
that succeeded. Invariants:

  (a) A sync row carries its newest finished sync - a success with its duration, an error
      with its reason, a skipped run with why - in the tone the outcome earns.
  (b) A health check row and a system job row read the newest `job_runs` row, whatever its
      outcome; a retry row reads its base job's.
  (c) An EPG source's row reads the source's own last_status, and a source never refreshed
      has no line rather than a false "not recorded".
  (d) A recurring job with no history tracked at all says so in words; a one-shot does not
      pretend to a history.
  (e) The page renders the line in the tone's class, so a failed run reads as one.

  python3 -m unittest tests.test_jobs_last_run
"""
import unittest
from datetime import datetime, timedelta
from unittest import mock

from apscheduler.triggers.date import DateTrigger

from app import db
from app import scheduler as scheduler_mod
from app.database import (
    AccountSyncLog, JOB_RUN_FAILED, JOB_RUN_SUCCESS, EPG_STATUS_FAILED, SYNC_LOG_ERROR,
    SYNC_LOG_SKIPPED, SYNC_LOG_SUCCESS, record_job_run,
)
from app.routes.jobs import LAST_RUN_BAD, LAST_RUN_OK, LAST_RUN_WARN
from tests.support import seed
from tests.support.app import make_test_app
from tests.support.scheduler_fakes import FakeJob, fake_scheduler

START = datetime(2026, 1, 1, 3, 0)


class _Case(unittest.TestCase):
    def setUp(self):
        self.t = make_test_app()
        self.now = datetime.utcnow()

    def tearDown(self):
        self.t.cleanup()

    def _items(self, jobs):
        from app.routes.jobs import _build_job_list
        db.session.commit()
        with mock.patch.object(scheduler_mod, 'get_scheduler',
                               return_value=fake_scheduler(jobs)):
            return {item['id']: item for item in _build_job_list()}

    def _sync_log(self, acc, status, minutes=2, error=None, when=START):
        db.session.add(AccountSyncLog(account_id=acc.id, started_at=when,
                                      completed_at=when + timedelta(minutes=minutes),
                                      status=status, error_message=error))


class SyncRowsTests(_Case):
    """(a)"""

    def test_a_successful_sync_says_when_and_how_long(self):
        acc = seed.make_account(name='Acct')
        self._sync_log(acc, SYNC_LOG_SUCCESS, minutes=2)
        row = self._items([FakeJob(f'account_sync_{acc.id}', self.now + timedelta(hours=1))])
        item = row[f'account_sync_{acc.id}']
        self.assertTrue(item['last_run_line'].startswith('Last run '), item['last_run_line'])
        self.assertIn('finished in about 2 minutes', item['last_run_line'])
        self.assertEqual(item['last_run_tone'], LAST_RUN_OK)
        self.assertTrue(item['last_run_tracked'])

    def test_a_failed_sync_names_its_reason_in_the_bad_tone(self):
        acc = seed.make_account(name='Acct')
        self._sync_log(acc, SYNC_LOG_SUCCESS, when=START)
        self._sync_log(acc, SYNC_LOG_ERROR, error='provider answered 503',
                       when=START + timedelta(days=1))
        item = self._items([FakeJob(f'account_sync_{acc.id}',
                                    self.now + timedelta(hours=1))])[f'account_sync_{acc.id}']
        self.assertIn('failed - provider answered 503', item['last_run_line'])
        self.assertEqual(item['last_run_tone'], LAST_RUN_BAD)

    def test_a_skipped_sync_says_why_in_the_warning_tone(self):
        acc = seed.make_account(name='Acct')
        self._sync_log(acc, SYNC_LOG_SKIPPED, minutes=0, error='a recording was in progress')
        item = self._items([FakeJob(f'account_sync_{acc.id}',
                                    self.now + timedelta(hours=1))])[f'account_sync_{acc.id}']
        self.assertIn('skipped - a recording was in progress', item['last_run_line'])
        self.assertEqual(item['last_run_tone'], LAST_RUN_WARN)

    def test_a_sync_retry_reads_the_accounts_history(self):
        acc = seed.make_account(name='Acct')
        self._sync_log(acc, SYNC_LOG_ERROR, error='timed out')
        item = self._items([FakeJob(f'account_sync_retry_{acc.id}',
                                    self.now + timedelta(minutes=20),
                                    trigger=DateTrigger())])[f'account_sync_retry_{acc.id}']
        self.assertIn('failed - timed out', item['last_run_line'])


class JobRunRowsTests(_Case):
    """(b)"""

    def test_a_system_jobs_newest_run_is_shown_whatever_its_outcome(self):
        record_job_run('config_backup_daily', START, START + timedelta(seconds=5),
                       JOB_RUN_SUCCESS)
        record_job_run('config_backup_daily', START + timedelta(days=1),
                       START + timedelta(days=1, seconds=3), JOB_RUN_FAILED)
        item = self._items([FakeJob('config_backup_daily',
                                    self.now + timedelta(hours=2))])['config_backup_daily']
        self.assertIn(': failed.', item['last_run_line'])
        self.assertEqual(item['last_run_tone'], LAST_RUN_BAD)
        # The estimate still averages the successful runs only.
        self.assertEqual(item['est_seconds'], 5)

    def test_a_health_check_row_reads_its_run_history(self):
        od = seed.make_test_job(name='Nightly', status='SCHEDULED', recurring=True,
                                recur_day=0, recur_hour=2, recur_minute=0)
        record_job_run(f'od_job_{od.id}', START, START + timedelta(minutes=9), JOB_RUN_SUCCESS)
        item = self._items([FakeJob(f'od_job_{od.id}',
                                    self.now + timedelta(hours=2))])[f'od_job_{od.id}']
        self.assertIn('finished in about 9 minutes', item['last_run_line'])
        self.assertEqual(item['last_run_tone'], LAST_RUN_OK)

    def test_a_maintenance_retry_reads_its_base_jobs_last_run(self):
        record_job_run('db_maintenance_daily', START, START + timedelta(minutes=1),
                       JOB_RUN_FAILED)
        item = self._items([FakeJob('db_maintenance_daily_retry',
                                    self.now + timedelta(minutes=30),
                                    trigger=DateTrigger())])['db_maintenance_daily_retry']
        self.assertIn(': failed.', item['last_run_line'])

    def test_a_tracked_job_with_no_run_yet_has_no_last_run_line(self):
        """The estimate line already says it has not finished a run here."""
        item = self._items([FakeJob('db_maintenance_daily',
                                    self.now + timedelta(hours=2))])['db_maintenance_daily']
        self.assertIsNone(item['last_run_line'])
        self.assertTrue(item['last_run_tracked'])
        self.assertIn('not known yet', item['duration_line'])


class EpgSourceRowsTests(_Case):
    """(c)"""

    def test_a_failed_refresh_is_read_off_the_source(self):
        acc = seed.make_account(name='Acct')
        src = seed.make_epg_source(acc)
        src.last_status = EPG_STATUS_FAILED
        src.last_error = 'answered with an M3U playlist'
        src.last_refresh_at = START
        item = self._items([FakeJob(f'epg_source_refresh_{src.id}',
                                    self.now + timedelta(hours=3))])[f'epg_source_refresh_{src.id}']
        self.assertIn('failed - answered with an M3U playlist', item['last_run_line'])
        self.assertEqual(item['last_run_tone'], LAST_RUN_BAD)

    def test_a_source_never_refreshed_has_no_line_and_is_not_called_untracked(self):
        acc = seed.make_account(name='Acct')
        src = seed.make_epg_source(acc)
        item = self._items([FakeJob(f'epg_source_refresh_{src.id}',
                                    self.now + timedelta(hours=3))])[f'epg_source_refresh_{src.id}']
        self.assertIsNone(item['last_run_line'])
        self.assertTrue(item['last_run_tracked'])


class UntrackedRowsTests(_Case):
    """(d)"""

    def test_a_recurring_probe_says_its_runs_are_not_recorded(self):
        item = self._items([FakeJob('storage_dirs_check',
                                    self.now + timedelta(minutes=3))])['storage_dirs_check']
        self.assertEqual(item['last_run_line'], 'Past runs are not recorded for this job.')
        self.assertFalse(item['last_run_tracked'])
        self.assertIsNone(item['last_run_tone'])

    def test_a_one_shot_carries_no_history_line(self):
        item = self._items([FakeJob('auto_rules_evaluate', self.now + timedelta(minutes=1),
                                    trigger=DateTrigger())])['auto_rules_evaluate']
        self.assertIsNone(item['last_run_line'])


class RenderedLineTests(_Case):
    """(e)"""

    def test_the_page_renders_a_failed_run_in_the_bad_tone(self):
        record_job_run('config_backup_daily', START, START + timedelta(seconds=3),
                       JOB_RUN_FAILED)
        db.session.commit()
        jobs = [FakeJob('config_backup_daily', self.now + timedelta(hours=2)),
                FakeJob('storage_dirs_check', self.now + timedelta(minutes=3))]
        with mock.patch.object(scheduler_mod, 'get_scheduler',
                               return_value=fake_scheduler(jobs)):
            html = self.t.client.get('/jobs').get_data(as_text=True)
        self.assertIn('class="jb-rel jb-last bad">Last run ', html)
        self.assertIn('Past runs are not recorded for this job.', html)


if __name__ == '__main__':
    unittest.main()
