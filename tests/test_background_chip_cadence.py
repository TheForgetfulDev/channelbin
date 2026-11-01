"""The nav's background chip dims for any upcoming job on a cadence longer than an hour
(dev/changelog/1353).

The dim state used to name three kinds by hand - the config backup, the TV Guide Channels
check and account syncs - so database maintenance, retention, an EPG refresh or a group's
own check starting in ten minutes left the chip hidden. The rule is now cadence: a job that
recurs within the hour is always imminent and so never news; a daily or multi-hour one is.
Invariants:

  (a) routes/jobs.py::dims_background_chip: an interval over an hour or a daily cron counts;
      an hourly or faster interval does not; a probe never does; a recording's own jobs
      never do; a booked health check does; a retry inherits its base job's answer; a
      one-shot with no base (the rules check an import queues) or whose base is gone does
      not.
  (b) Through the live scheduler: database maintenance within the hour dims the chip and is
      named from /jobs' own table with its settings link; the hourly stats fold and the
      5- and 10-minute probes within the hour leave it hidden.

  python3 -m unittest tests.test_background_chip_cadence
"""
import unittest
from datetime import datetime, timedelta

from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.date import DateTrigger
from apscheduler.triggers.interval import IntervalTrigger

from app.routes.jobs import dims_background_chip
from app.tz_utils import UTC
from tests.support.app import make_test_app
from tests.support.scheduler_fakes import FakeJob, fake_scheduler
import app.scheduler as sched

ACTIVITY_URL = '/api/activity/status'


class CadenceRuleTests(unittest.TestCase):
    """(a)"""

    def _dims(self, job, others=()):
        return dims_background_chip(job, fake_scheduler([job, *others]).get_job)

    def test_a_multi_hour_sync_counts(self):
        self.assertTrue(self._dims(FakeJob('account_sync_3', datetime.utcnow(),
                                           IntervalTrigger(hours=6))))

    def test_a_daily_cron_counts(self):
        self.assertTrue(self._dims(FakeJob('db_maintenance_daily', datetime.utcnow(),
                                           CronTrigger(hour=3))))

    def test_an_hourly_or_faster_interval_does_not(self):
        self.assertFalse(self._dims(FakeJob('auto_rule_sweep', datetime.utcnow(),
                                            IntervalTrigger(hours=1))))
        self.assertFalse(self._dims(FakeJob('logo_cache_fetch', datetime.utcnow(),
                                            IntervalTrigger(minutes=5))))

    def test_a_probe_never_counts_whatever_its_trigger(self):
        self.assertFalse(self._dims(FakeJob('host_resolve_check', datetime.utcnow(),
                                            IntervalTrigger(hours=6))))

    def test_a_recordings_own_jobs_never_count(self):
        for job_id in ('start_5', 'stop_5', 'precheck_5', 'retry_5', 'resume_5'):
            self.assertFalse(self._dims(FakeJob(job_id, datetime.utcnow(), DateTrigger())),
                             job_id)

    def test_a_booked_health_check_counts(self):
        self.assertTrue(self._dims(FakeJob('od_job_7', datetime.utcnow(), DateTrigger())))

    def test_a_retry_inherits_its_base_jobs_answer(self):
        daily = FakeJob('db_maintenance_daily', datetime.utcnow(), CronTrigger(hour=3))
        hourly = FakeJob('auto_rule_sweep', datetime.utcnow(), IntervalTrigger(hours=1))
        self.assertTrue(self._dims(FakeJob('db_maintenance_daily_retry', datetime.utcnow(),
                                           DateTrigger()), [daily]))
        self.assertFalse(self._dims(FakeJob('auto_rule_sweep_retry', datetime.utcnow(),
                                            DateTrigger()), [hourly]))

    def test_a_retry_whose_base_is_gone_is_an_orphan_and_does_not(self):
        self.assertFalse(self._dims(FakeJob('account_sync_retry_9', datetime.utcnow(),
                                            DateTrigger())))
        self.assertFalse(self._dims(FakeJob('channel_hide_materialize_retry',
                                            datetime.utcnow(), DateTrigger())))

    def test_a_one_shot_with_no_base_does_not(self):
        self.assertFalse(self._dims(FakeJob('auto_rules_evaluate', datetime.utcnow(),
                                            DateTrigger())))


def _set_next_run(job_id, when_utc_aware):
    sched._scheduler.modify_job(job_id, next_run_time=when_utc_aware)


class LiveSchedulerTests(unittest.TestCase):
    """(b)"""

    def setUp(self):
        self.t = make_test_app(start_scheduler=True)
        # Every registered job starts outside the window, so each test decides what is in
        # it (the shape tests/test_activity_chip_dim.py uses, for the same reason).
        later = datetime.now(UTC) + timedelta(hours=5)
        for job in sched._scheduler.get_jobs():
            if job.next_run_time is not None:
                _set_next_run(job.id, later)

    def tearDown(self):
        self.t.cleanup()

    def _background(self):
        return self.t.client.get(ACTIVITY_URL).get_json()['background']

    def test_database_maintenance_within_the_hour_dims_and_is_named_from_jobs(self):
        _set_next_run('db_maintenance_daily', datetime.now(UTC) + timedelta(minutes=20))
        bg = self._background()
        self.assertEqual(bg['state'], 'dim')
        self.assertEqual(bg['next_scheduled']['label'], 'Database Maintenance')
        self.assertEqual(bg['next_scheduled']['href'], '/settings?q=keep_days')

    def test_recording_retention_within_the_hour_dims(self):
        _set_next_run('recording_retention_daily', datetime.now(UTC) + timedelta(minutes=20))
        self.assertEqual(self._background()['next_scheduled']['label'], 'Recording Retention')

    def test_the_hourly_stats_fold_and_the_quick_probes_leave_it_hidden(self):
        """The logo fetch is off in a test app, so its 5-minute interval is CadenceRuleTests'."""
        _set_next_run('account_stats_fold', datetime.now(UTC) + timedelta(minutes=5))
        _set_next_run('search_index_janitor', datetime.now(UTC) + timedelta(minutes=2))
        _set_next_run('storage_dirs_check', datetime.now(UTC) + timedelta(minutes=1))
        _set_next_run('hc_window_dispatch', datetime.now(UTC) + timedelta(minutes=1))
        bg = self._background()
        self.assertEqual(bg['state'], 'hidden')
        self.assertIsNone(bg['next_scheduled'])


if __name__ == '__main__':
    unittest.main()
