"""
A health check that finishes announces nothing (dev/changelog/928).

It used to raise HEALTH_CHECK_COMPLETE on a genuinely-terminal one-off run
(dev/changelog/310), while a recurring job looping back to SCHEDULED stayed
silent. A finished check is not a problem, and the job row it just committed
already carries the outcome and the time it finished - so the alert was retired
when alerts were narrowed to real problems (dev/changelog/923).

Kept as a guard rather than deleted: the terminal-vs-recurring distinction below
is still load-bearing for the job's own status, and re-adding a "finished!" alert
here is the obvious thing to do and is what was decided against.

No network, no real ffmpeg - see CLAUDE.md §Testing.
Run standalone:
  python3 -m unittest tests.test_health_check_complete_alert
"""
import unittest
from unittest import mock

from app import channel_tester, db
from app.database import Alert, OnDemandTestJob
from tests.support.app import make_test_app
from tests.support import seed


class HealthCheckCompletionTests(unittest.TestCase):
    def setUp(self):
        self.t = make_test_app()
        self.channel = seed.make_channel(seed.make_account())
        db.session.commit()

    def tearDown(self):
        self.t.cleanup()

    def _run(self, completed=True, recurring=False):
        job = seed.make_test_job(name='Nightly Check', channels=[self.channel],
                                  status='RUNNING', recurring=recurring)
        db.session.commit()
        with mock.patch('app.channel_tester.imminent_recording_conflict', return_value=None), \
             mock.patch('app.channel_tester._run_channel_loop', return_value=completed), \
             mock.patch('app.alerts.create_alert') as alert_spy:
            channel_tester.run_on_demand_test_job(self.t.app, job.id)
        db.session.expire_all()
        return db.session.get(OnDemandTestJob, job.id), alert_spy

    def test_one_off_completion_raises_no_alert_and_records_the_outcome(self):
        job, alert_spy = self._run(completed=True)
        self.assertEqual(job.status, 'COMPLETED')
        self.assertIsNotNone(job.completed_at,
                             'the job row is the surface: it must carry when it finished')
        alert_spy.assert_not_called()

    def test_one_off_stopped_early_raises_no_alert_and_records_the_outcome(self):
        job, alert_spy = self._run(completed=False)
        self.assertEqual(job.status, 'CANCELLED')
        self.assertIsNotNone(job.completed_at)
        alert_spy.assert_not_called()

    def test_recurring_job_finish_stays_silent_and_reschedules(self):
        job, alert_spy = self._run(completed=True, recurring=True)
        self.assertEqual(job.status, 'SCHEDULED')
        alert_spy.assert_not_called()

    def test_no_health_check_complete_row_is_written(self):
        """Belt and braces on the spy above - nothing reaches the table by another path."""
        self._run(completed=True)
        self.assertEqual(
            Alert.query.filter_by(alert_type='HEALTH_CHECK_COMPLETE').count(), 0)


if __name__ == '__main__':
    unittest.main()
