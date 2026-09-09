"""Scheduled-job duration tracking (dev/changelog/592).

Before this, app/routes/jobs.py::_build_job_list modeled every job as a zero-length
instant - nothing stored how long a completed run actually took, so /jobs could not
show an estimated runtime and its own overlap detector was comparing zero-length
intervals (a job that actually overlaps a recording could show green).

Two data sources feed the estimate: AccountSyncLog (already existed, reused rather
than duplicated - CLAUDE.md's "search before you write") for account syncs, and the
new JobRun table for the three system jobs that had no run history at all (config
backup, recording retention, DB maintenance).

Runs against a throwaway temp SQLite DB - never the live dvr.db.
    python3 -m unittest tests.test_job_duration_tracking
"""
import os
import sys
import unittest
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tests.support.app import make_test_app  # noqa: E402
from tests.support import seed  # noqa: E402
from app import db  # noqa: E402
from app.database import (  # noqa: E402
    JobRun, AccountSyncLog, JOB_RUN_SUCCESS, JOB_RUN_FAILED, JOB_RUN_HISTORY_LIMIT,
    record_job_run, get_job_duration_estimate,
)
from app.accounts import get_sync_duration_estimate  # noqa: E402
from app.fmt_utils import fmt_job_duration_line  # noqa: E402
from app.tz_utils import UTC  # noqa: E402
import app.scheduler as sched  # noqa: E402


class RecordJobRunPruningTests(unittest.TestCase):
    """record_job_run() must keep exactly the most recent JOB_RUN_HISTORY_LIMIT rows per
    job_id - an unbounded history table for a job that runs every few hours forever is
    the "no hidden growth" failure this caps against."""

    def setUp(self):
        self.t = make_test_app()

    def tearDown(self):
        self.t.cleanup()

    def test_history_is_capped_at_the_limit(self):
        base = datetime(2026, 1, 1)
        for i in range(JOB_RUN_HISTORY_LIMIT + 5):
            started = base + timedelta(hours=i)
            record_job_run('probe_job', started, started + timedelta(seconds=10), JOB_RUN_SUCCESS)

        rows = JobRun.query.filter_by(job_id='probe_job').all()
        self.assertEqual(len(rows), JOB_RUN_HISTORY_LIMIT)

    def test_pruning_keeps_the_most_recent_runs(self):
        base = datetime(2026, 1, 1)
        for i in range(JOB_RUN_HISTORY_LIMIT + 5):
            started = base + timedelta(hours=i)
            record_job_run('probe_job', started, started + timedelta(seconds=10), JOB_RUN_SUCCESS)

        remaining = {row.started_at for row in JobRun.query.filter_by(job_id='probe_job').all()}
        oldest_five = {base + timedelta(hours=i) for i in range(5)}
        self.assertFalse(remaining & oldest_five, 'the five oldest runs must have been pruned')

    def test_other_job_ids_are_not_touched_by_pruning(self):
        base = datetime(2026, 1, 1)
        record_job_run('job_a', base, base + timedelta(seconds=5), JOB_RUN_SUCCESS)
        for i in range(JOB_RUN_HISTORY_LIMIT + 5):
            started = base + timedelta(hours=i)
            record_job_run('job_b', started, started + timedelta(seconds=10), JOB_RUN_SUCCESS)

        self.assertEqual(JobRun.query.filter_by(job_id='job_a').count(), 1)
        self.assertEqual(JobRun.query.filter_by(job_id='job_b').count(), JOB_RUN_HISTORY_LIMIT)


class GetJobDurationEstimateTests(unittest.TestCase):
    def setUp(self):
        self.t = make_test_app()

    def tearDown(self):
        self.t.cleanup()

    def test_untracked_job_returns_unknown(self):
        avg, runs = get_job_duration_estimate('never_ran_job')
        self.assertIsNone(avg)
        self.assertEqual(runs, 0)

    def test_average_and_count_over_successful_runs(self):
        base = datetime(2026, 1, 1)
        record_job_run('db_maintenance_daily', base, base + timedelta(seconds=60), JOB_RUN_SUCCESS)
        record_job_run('db_maintenance_daily', base + timedelta(days=1),
                        base + timedelta(days=1, seconds=120), JOB_RUN_SUCCESS)

        avg, runs = get_job_duration_estimate('db_maintenance_daily')
        self.assertEqual(avg, 90.0)
        self.assertEqual(runs, 2)

    def test_failed_runs_are_excluded_from_the_average(self):
        base = datetime(2026, 1, 1)
        record_job_run('db_maintenance_daily', base, base + timedelta(seconds=60), JOB_RUN_SUCCESS)
        record_job_run('db_maintenance_daily', base + timedelta(days=1),
                        base + timedelta(days=1, seconds=99999), JOB_RUN_FAILED)

        avg, runs = get_job_duration_estimate('db_maintenance_daily')
        self.assertEqual(avg, 60.0)
        self.assertEqual(runs, 1)


class SyncDurationEstimateTests(unittest.TestCase):
    """accounts.get_sync_duration_estimate() - reuses AccountSyncLog rather than a second
    history table, per CLAUDE.md's search-before-you-write rule."""

    def setUp(self):
        self.t = make_test_app()
        self.account = seed.make_account()
        db.session.commit()

    def tearDown(self):
        self.t.cleanup()

    def test_account_with_no_completed_sync_is_unknown(self):
        avg, runs = get_sync_duration_estimate(self.account.id)
        self.assertIsNone(avg)
        self.assertEqual(runs, 0)

    def test_success_and_partial_syncs_both_count(self):
        base = datetime(2026, 1, 1)
        db.session.add(AccountSyncLog(account_id=self.account.id, started_at=base,
                                       completed_at=base + timedelta(seconds=100),
                                       status='SUCCESS'))
        db.session.add(AccountSyncLog(account_id=self.account.id, started_at=base + timedelta(hours=4),
                                       completed_at=base + timedelta(hours=4, seconds=200),
                                       status='PARTIAL'))
        db.session.commit()

        avg, runs = get_sync_duration_estimate(self.account.id)
        self.assertEqual(avg, 150.0)
        self.assertEqual(runs, 2)

    def test_in_progress_error_and_cancelled_runs_are_excluded(self):
        base = datetime(2026, 1, 1)
        db.session.add(AccountSyncLog(account_id=self.account.id, started_at=base,
                                       completed_at=None, status='IN_PROGRESS'))
        db.session.add(AccountSyncLog(account_id=self.account.id, started_at=base + timedelta(hours=1),
                                       completed_at=base + timedelta(hours=1, seconds=99999),
                                       status='ERROR'))
        db.session.add(AccountSyncLog(account_id=self.account.id, started_at=base + timedelta(hours=2),
                                       completed_at=None, status='CANCELLED'))
        db.session.add(AccountSyncLog(account_id=self.account.id, started_at=base + timedelta(hours=3),
                                       completed_at=base + timedelta(hours=3, seconds=42),
                                       status='SUCCESS'))
        db.session.commit()

        avg, runs = get_sync_duration_estimate(self.account.id)
        self.assertEqual(avg, 42.0)
        self.assertEqual(runs, 1)

    def test_a_different_accounts_syncs_are_never_averaged_in(self):
        other = seed.make_account(name='Other account')
        db.session.commit()
        base = datetime(2026, 1, 1)
        db.session.add(AccountSyncLog(account_id=other.id, started_at=base,
                                       completed_at=base + timedelta(seconds=99999),
                                       status='SUCCESS'))
        db.session.commit()

        avg, runs = get_sync_duration_estimate(self.account.id)
        self.assertIsNone(avg)
        self.assertEqual(runs, 0)


class DurationLineWordingTests(unittest.TestCase):
    """fmt_job_duration_line()'s wording must match the approved dev/mockups/29
    dashboard-rail contract exactly - that mockup is the contract for these strings, not
    a draft to re-derive from (dev/mockups/verify29.js asserts the same two shapes)."""

    def test_unknown_job(self):
        self.assertEqual(
            fmt_job_duration_line(None, 0),
            'Expected runtime unknown - this job has never completed a run.')

    def test_known_average_states_what_it_is_an_average_of(self):
        line = fmt_job_duration_line(300, 3)
        self.assertIn('average of the last 3 runs', line)
        self.assertIn('about 5 minutes', line)

    def test_singular_run_is_not_pluralized(self):
        line = fmt_job_duration_line(60, 1)
        self.assertIn('average of the last 1 run)', line)
        self.assertNotIn('1 runs', line)


class BuildJobListDurationWiringTests(unittest.TestCase):
    """app/routes/jobs.py::_build_job_list actually attaches the estimate to the job
    dicts /jobs and /api/jobs/scheduled render, and uses it to fix the overlap
    detector's zero-length-interval blind spot (BUGS.md 2026-08-12)."""

    def setUp(self):
        self.t = make_test_app(start_scheduler=True)
        self.account = seed.make_account()
        db.session.commit()
        sched.schedule_account_sync(self.t.app, self.account.id)

    def tearDown(self):
        self.t.cleanup()

    def test_account_sync_with_no_history_reports_unknown(self):
        from app.routes.jobs import _build_job_list

        items = _build_job_list()
        item = next(i for i in items if i['id'] == f'account_sync_{self.account.id}')
        self.assertEqual(item['runs'], 0)
        self.assertEqual(item['duration_line'],
                          'Expected runtime unknown - this job has never completed a run.')

    def test_account_sync_with_history_reports_the_average(self):
        from app.routes.jobs import _build_job_list

        base = datetime(2026, 1, 1)
        for i in range(3):
            db.session.add(AccountSyncLog(
                account_id=self.account.id, started_at=base + timedelta(hours=i),
                completed_at=base + timedelta(hours=i, seconds=120), status='SUCCESS'))
        db.session.commit()

        items = _build_job_list()
        item = next(i for i in items if i['id'] == f'account_sync_{self.account.id}')
        self.assertEqual(item['runs'], 3)
        self.assertIn('average of the last 3 runs', item['duration_line'])

    def test_system_job_with_no_history_reports_unknown(self):
        from app.routes.jobs import _build_job_list

        items = _build_job_list()
        item = next(i for i in items if i['id'] == 'db_maintenance_daily')
        self.assertEqual(item['runs'], 0)
        self.assertEqual(item['duration_line'],
                          'Expected runtime unknown - this job has never completed a run.')

    def test_untracked_window_jobs_carry_no_duration_line(self):
        """hc_window_dispatch/hc_window_close are deliberately excluded - the approved
        dev/mockups/29 rail doesn't track them either."""
        from app.routes.jobs import _build_job_list

        items = _build_job_list()
        item = next(i for i in items if i['id'] == 'hc_window_dispatch')
        self.assertIsNone(item.get('duration_line'))

    def test_overlap_detector_uses_the_estimated_duration(self):
        """The zero-length-interval blind spot: before this fix, a job with a real
        multi-minute runtime was compared as a zero-length instant, so a recording
        starting seconds after it could never be flagged as overlapping."""
        from app.routes.jobs import _build_job_list
        from app import scheduler as sched_mod

        # Give the sync a known ~5-minute average.
        base = datetime(2026, 1, 1)
        db.session.add(AccountSyncLog(
            account_id=self.account.id, started_at=base,
            completed_at=base + timedelta(minutes=5), status='SUCCESS'))
        db.session.commit()

        # Force the sync's next run to a known moment, then schedule a recording that
        # starts partway through the sync's estimated 5-minute window.
        job_id = f'account_sync_{self.account.id}'
        next_run = datetime.utcnow() + timedelta(hours=1)
        sched_mod._scheduler.modify_job(job_id, next_run_time=next_run.replace(tzinfo=UTC))

        channel = seed.make_channel(self.account)
        db.session.commit()
        rec = seed.make_recording(status='SCHEDULED', channel_id=channel.id,
                                   start_time=next_run + timedelta(minutes=2),
                                   stop_time=next_run + timedelta(minutes=10))
        db.session.commit()
        sched_mod.schedule_recording(self.t.app, rec.id, rec.start_time, rec.stop_time)

        items = _build_job_list()
        sync_item = next(i for i in items if i['id'] == job_id)
        self.assertEqual(sync_item['overlap'], 'red',
                          'a recording starting inside the sync\'s estimated runtime must '
                          'be flagged as an overlap, not compared against a zero-length instant')


if __name__ == '__main__':
    unittest.main()
