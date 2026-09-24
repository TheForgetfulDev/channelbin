"""
Guards dev/changelog/632-unify-on-demand-job-terminal-status.md: the on-demand-job
"stopped RUNNING" terminal-status decision tree (recurring -> SCHEDULED with its next
run; a one-off whose kept DateTrigger is still registered -> SCHEDULED; otherwise
COMPLETED/CANCELLED) used to be written three separate times - channel_tester.py's
_save_job_final_status, scheduler.py's startup _cancel_running_ondemand sweep, and
routes/channel_tests.py's force_cancel_on_demand_job - and is now one shared
app.scheduler.finalize_on_demand_job_status(). This tests the shared helper directly
plus the two call sites (force-cancel route, startup sweep) that previously had no
targeted coverage of their own status-transition outcomes, so a future drift across
the three sites fails here instead of going unnoticed again.
"""
import unittest
from datetime import datetime, timedelta

from app import db
from app.database import OnDemandTestJob
from tests.support.app import make_test_app
from tests.support import seed


class FinalizeOnDemandJobStatusTests(unittest.TestCase):
    """Direct unit tests of the extracted decision tree (no live scheduler needed for
    these branches - job.scheduler_job_id is left unset)."""

    def setUp(self):
        self.t = make_test_app()
        self.channel = seed.make_channel(seed.make_account())
        db.session.commit()

    def tearDown(self):
        self.t.cleanup()

    def test_recurring_job_reverts_to_scheduled(self):
        from app.scheduler import finalize_on_demand_job_status
        job = seed.make_test_job(name='Recurring', channels=[self.channel],
                                  status='RUNNING', recurring=True)
        db.session.commit()

        kind = finalize_on_demand_job_status(job, completed=True)

        self.assertEqual(kind, 'recurring')
        self.assertEqual(job.status, 'SCHEDULED')

    def test_recurring_job_stopped_early_does_not_advance_last_full_run_at(self):
        from app.scheduler import finalize_on_demand_job_status
        job = seed.make_test_job(name='Recurring', channels=[self.channel],
                                  status='RUNNING', recurring=True)
        db.session.commit()

        finalize_on_demand_job_status(job, completed=False)

        self.assertIsNone(job.last_full_run_at)

    def test_recurring_job_completed_advances_last_full_run_at(self):
        from app.scheduler import finalize_on_demand_job_status
        job = seed.make_test_job(name='Recurring', channels=[self.channel],
                                  status='RUNNING', recurring=True)
        db.session.commit()

        finalize_on_demand_job_status(job, completed=True)

        self.assertIsNotNone(job.last_full_run_at)

    def test_one_off_with_no_kept_schedule_goes_completed(self):
        from app.scheduler import finalize_on_demand_job_status
        job = seed.make_test_job(name='One-off', channels=[self.channel],
                                  status='RUNNING', recurring=False)
        db.session.commit()

        kind = finalize_on_demand_job_status(job, completed=True)

        self.assertEqual(kind, 'finished')
        self.assertEqual(job.status, 'COMPLETED')

    def test_one_off_with_no_kept_schedule_stopped_early_goes_cancelled(self):
        from app.scheduler import finalize_on_demand_job_status
        job = seed.make_test_job(name='One-off', channels=[self.channel],
                                  status='RUNNING', recurring=False)
        db.session.commit()

        kind = finalize_on_demand_job_status(job, completed=False)

        self.assertEqual(kind, 'finished')
        self.assertEqual(job.status, 'CANCELLED')


class FinalizeOnDemandJobStatusKeptScheduleTests(unittest.TestCase):
    """The 'kept schedule' branch needs a real registered APScheduler DateTrigger job -
    this is how "Run Now, keep schedule" is distinguished from an ad hoc run."""

    def setUp(self):
        self.t = make_test_app(start_scheduler=True)
        self.channel = seed.make_channel(seed.make_account())
        db.session.commit()

    def tearDown(self):
        self.t.cleanup()

    def _register_kept_job(self, job):
        from app.scheduler import _add_job, _on_demand_job_trigger
        aps_job_id = f'od_job_{job.id}'
        future = datetime.utcnow() + timedelta(hours=1)
        _add_job(func=_on_demand_job_trigger, trigger='date', run_date=future,
                  args=[job.id], id=aps_job_id, replace_existing=True)
        job.scheduler_job_id = aps_job_id
        db.session.commit()

    def test_one_off_with_kept_schedule_reverts_to_scheduled_not_terminal(self):
        from app.scheduler import finalize_on_demand_job_status
        job = seed.make_test_job(name='Kept one-off', channels=[self.channel],
                                  status='RUNNING', recurring=False)
        db.session.commit()
        self._register_kept_job(job)

        kind = finalize_on_demand_job_status(job, completed=True)

        self.assertEqual(kind, 'kept')
        self.assertEqual(job.status, 'SCHEDULED')
        self.assertIsNotNone(job.scheduled_start_time)


class ForceCancelOnDemandJobRouteTests(unittest.TestCase):
    """routes/channel_tests.py::force_cancel_on_demand_job - previously had no test of
    its own; now delegates to the shared decision tree like the other two sites."""

    def setUp(self):
        self.t = make_test_app()
        self.t.app.config['WTF_CSRF_ENABLED'] = False
        self.client = self.t.client
        self.ctx = self.t.app.app_context()
        self.ctx.push()
        self.channel = seed.make_channel(seed.make_account())
        db.session.commit()

    def tearDown(self):
        self.ctx.pop()
        self.t.cleanup()

    def test_only_running_jobs_can_be_force_cancelled(self):
        job = seed.make_test_job(name='Scheduled job', channels=[self.channel],
                                  status='SCHEDULED', recurring=False)
        db.session.commit()

        resp = self.client.post(f'/api/channel-tests/on-demand/{job.id}/force-cancel')

        self.assertEqual(resp.status_code, 400)

    def test_recurring_job_reverts_to_scheduled(self):
        job = seed.make_test_job(name='Recurring', channels=[self.channel],
                                  status='RUNNING', recurring=True)
        db.session.commit()

        resp = self.client.post(f'/api/channel-tests/on-demand/{job.id}/force-cancel')

        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.get_json()['status'], 'SCHEDULED')

    def test_one_off_with_no_kept_schedule_goes_cancelled_never_completed(self):
        job = seed.make_test_job(name='One-off', channels=[self.channel],
                                  status='RUNNING', recurring=False)
        db.session.commit()

        resp = self.client.post(f'/api/channel-tests/on-demand/{job.id}/force-cancel')

        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.get_json()['status'], 'CANCELLED')


class CancelRunningOndemandStartupSweepTests(unittest.TestCase):
    """scheduler.py's resume_in_progress_recordings() sweep for jobs caught RUNNING at
    a restart - the third call site of the shared decision tree."""

    def setUp(self):
        self.t = make_test_app(start_scheduler=True)
        self.channel = seed.make_channel(seed.make_account())
        db.session.commit()

    def tearDown(self):
        self.t.cleanup()

    def test_recurring_running_job_reverts_to_scheduled_at_restart(self):
        """Seeded with a real time: a recurring row with no hour/minute used to register an
        every-second CronTrigger here, so this test raced a real health-check run against
        teardown (dev/docs/BUGS.md 2026-09-23 @ 08:15:12 PM)."""
        from app.scheduler import resume_in_progress_recordings
        job = seed.make_test_job(name='Recurring', channels=[self.channel],
                                  status='RUNNING', recurring=True,
                                  recur_hour=2, recur_minute=0, recur_day=0)
        db.session.commit()

        resume_in_progress_recordings(self.t.app)

        db.session.expire_all()
        refreshed = db.session.get(OnDemandTestJob, job.id)
        self.assertEqual(refreshed.status, 'SCHEDULED')

    def test_one_off_running_job_with_no_kept_schedule_is_cancelled_at_restart(self):
        from app.scheduler import resume_in_progress_recordings
        job = seed.make_test_job(name='One-off', channels=[self.channel],
                                  status='RUNNING', recurring=False)
        db.session.commit()

        resume_in_progress_recordings(self.t.app)

        db.session.expire_all()
        refreshed = db.session.get(OnDemandTestJob, job.id)
        self.assertEqual(refreshed.status, 'CANCELLED')


class TimelessRecurringJobIsRefusedTests(unittest.TestCase):
    """dev/docs/BUGS.md 2026-09-23 @ 08:15:12 PM: schedule_on_demand_job() handed a
    recurring job with no recur_hour/recur_minute to APScheduler as
    CronTrigger(hour=None, minute=None), which fires every second. It must register nothing
    and say so."""

    def setUp(self):
        self.t = make_test_app(start_scheduler=True)
        self.channel = seed.make_channel(seed.make_account())
        db.session.commit()

    def tearDown(self):
        self.t.cleanup()

    def _open_alerts(self):
        from app.database import Alert
        return Alert.query.filter_by(alert_type='HEALTH_CHECK_SCHEDULE_INVALID',
                                     dismissed_at=None).all()

    def test_the_refusal_is_logged_at_warning(self):
        from app.scheduler import schedule_on_demand_job
        job = seed.make_test_job(name='No time', channels=[self.channel],
                                  status='SCHEDULED', recurring=True)
        db.session.commit()

        with self.assertLogs('app.scheduler', level='WARNING') as logs:
            schedule_on_demand_job(job)

        self.assertIn(f'OnDemandTestJob {job.id}', logs.output[0])

    def test_no_apscheduler_job_is_registered(self):
        from app.scheduler import get_scheduler, schedule_on_demand_job
        job = seed.make_test_job(name='No time', channels=[self.channel],
                                  status='SCHEDULED', recurring=True)
        db.session.commit()

        with self.assertNoLogs('app.scheduler', level='ERROR'):
            result = schedule_on_demand_job(job)

        self.assertEqual(result, (None, None))
        self.assertIsNone(get_scheduler().get_job(f'od_job_{job.id}'))

    def test_one_alert_names_the_job_and_a_repeat_does_not_duplicate_it(self):
        from app.scheduler import schedule_on_demand_job
        job = seed.make_test_job(name='No time', channels=[self.channel],
                                  status='SCHEDULED', recurring=True)
        db.session.commit()

        schedule_on_demand_job(job)
        schedule_on_demand_job(job)

        alerts = self._open_alerts()
        self.assertEqual(len(alerts), 1)
        self.assertEqual(alerts[0].source, f'od_job_{job.id}')
        self.assertEqual(alerts[0].title, 'Health check "No time" has no time set')

    def test_startup_sweep_leaves_it_scheduled_with_nothing_registered(self):
        from app.scheduler import get_scheduler, resume_in_progress_recordings
        job = seed.make_test_job(name='No time', channels=[self.channel],
                                  status='RUNNING', recurring=True)
        db.session.commit()

        resume_in_progress_recordings(self.t.app)

        db.session.expire_all()
        refreshed = db.session.get(OnDemandTestJob, job.id)
        self.assertEqual(refreshed.status, 'SCHEDULED')
        self.assertIsNone(refreshed.scheduler_job_id)
        self.assertIsNone(get_scheduler().get_job(f'od_job_{job.id}'))
        self.assertEqual(len(self._open_alerts()), 1)

    def test_a_job_with_a_time_is_still_registered(self):
        from app.scheduler import get_scheduler, schedule_on_demand_job
        job = seed.make_test_job(name='Timed', channels=[self.channel],
                                  status='SCHEDULED', recurring=True,
                                  recur_hour=2, recur_minute=0, recur_day=0)
        db.session.commit()

        aps_job_id, next_run = schedule_on_demand_job(job)

        self.assertEqual(aps_job_id, f'od_job_{job.id}')
        self.assertIsNotNone(next_run)
        self.assertIsNotNone(get_scheduler().get_job(aps_job_id))
        self.assertEqual(self._open_alerts(), [])


class FinalizeWithNoSchedulerTests(unittest.TestCase):
    """dev/docs/BUGS.md 2026-09-23 @ 08:15:12 PM: a health-check run that finished after the
    scheduler was gone raised AttributeError out of get_next_on_demand_run() and never saved
    its final status."""

    def setUp(self):
        self.t = make_test_app()
        self.channel = seed.make_channel(seed.make_account())
        db.session.commit()

    def tearDown(self):
        self.t.cleanup()

    def test_get_next_on_demand_run_is_none_without_a_scheduler(self):
        from unittest import mock
        from app.scheduler import get_next_on_demand_run
        job = seed.make_test_job(name='Recurring', channels=[self.channel],
                                  status='RUNNING', recurring=True,
                                  scheduler_job_id='od_job_1')
        db.session.commit()

        with mock.patch('app.scheduler._scheduler', None):
            self.assertIsNone(get_next_on_demand_run(job))

    def test_recurring_run_still_finalizes_without_a_scheduler(self):
        from unittest import mock
        from app.scheduler import finalize_on_demand_job_status
        job = seed.make_test_job(name='Recurring', channels=[self.channel],
                                  status='RUNNING', recurring=True,
                                  scheduler_job_id='od_job_1')
        db.session.commit()

        with mock.patch('app.scheduler._scheduler', None):
            kind = finalize_on_demand_job_status(job, completed=True)

        self.assertEqual(kind, 'recurring')
        self.assertEqual(job.status, 'SCHEDULED')


if __name__ == '__main__':
    unittest.main()
