"""
Guards dev/changelog/310-notify-on-manual-health-check-completion.md: a manual
(one-off) on-demand health check that finishes running must raise a
HEALTH_CHECK_COMPLETE alert, while a recurring job that just loops back to
SCHEDULED must stay silent - only a genuinely-terminal run notifies.
"""
import unittest
from unittest import mock

from app import channel_tester, db
from app.database import OnDemandTestJob
from tests.support.app import make_test_app
from tests.support import seed


class HealthCheckCompleteAlertTests(unittest.TestCase):
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

    def test_one_off_completion_raises_health_check_complete_alert(self):
        job, alert_spy = self._run(completed=True)
        self.assertEqual(job.status, 'COMPLETED')
        alert_spy.assert_called_once()
        self.assertEqual(alert_spy.call_args.args[0], 'HEALTH_CHECK_COMPLETE')
        self.assertIn('Nightly Check', alert_spy.call_args.kwargs['title'])

    def test_one_off_stopped_early_still_notifies(self):
        job, alert_spy = self._run(completed=False)
        self.assertEqual(job.status, 'CANCELLED')
        alert_spy.assert_called_once()
        self.assertEqual(alert_spy.call_args.args[0], 'HEALTH_CHECK_COMPLETE')

    def test_recurring_job_finish_stays_silent(self):
        job, alert_spy = self._run(completed=True, recurring=True)
        self.assertEqual(job.status, 'SCHEDULED')
        alert_spy.assert_not_called()


if __name__ == '__main__':
    unittest.main()
