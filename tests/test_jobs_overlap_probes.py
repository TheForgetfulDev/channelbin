"""/jobs' overlap dot ignores the quick probes (dev/changelog/1352).

The yellow "runs close to another job" dot fired on any two next runs within 5 minutes.
Maintenance Window Dispatch and Storage Folder Check run every 5 minutes and cost nothing,
so they colored whatever was near them: 4 of 10 rows yellow on the dev box, none a real
conflict. Invariants:

  (a) A probe next to a real job colors neither side: the real job stays green and the
      probe is marked as not compared rather than as "nothing nearby".
  (b) Two real jobs within 5 minutes are still yellow, two at the same time still red.
  (c) The page says so: the probe's dot explains itself and the legend names the rule.

  python3 -m unittest tests.test_jobs_overlap_probes
"""
import unittest
from datetime import datetime, timedelta
from unittest import mock

from app import db
from app import scheduler as scheduler_mod
from app.routes.jobs import PROBE_JOBS
from tests.support import seed
from tests.support.app import make_test_app
from tests.support.scheduler_fakes import FakeJob, fake_scheduler


class OverlapProbeTests(unittest.TestCase):
    def setUp(self):
        self.t = make_test_app()
        self.now = datetime.utcnow()

    def tearDown(self):
        self.t.cleanup()

    def _items(self, jobs):
        from app.routes.jobs import _build_job_list
        with mock.patch.object(scheduler_mod, 'get_scheduler',
                               return_value=fake_scheduler(jobs)):
            return {item['id']: item for item in _build_job_list()}

    def test_a_probe_beside_a_sync_colors_neither(self):
        """(a)"""
        acc = seed.make_account(name='Acct')
        db.session.commit()
        items = self._items([
            FakeJob('storage_dirs_check', self.now + timedelta(minutes=10)),
            FakeJob('hc_window_dispatch', self.now + timedelta(minutes=11)),
            FakeJob(f'account_sync_{acc.id}', self.now + timedelta(minutes=12)),
        ])
        self.assertEqual(items[f'account_sync_{acc.id}']['overlap'], 'green')
        for probe in ('storage_dirs_check', 'hc_window_dispatch'):
            self.assertEqual(items[probe]['overlap'], 'green')
            self.assertTrue(items[probe].get('overlap_probe'), probe)
        self.assertNotIn('overlap_probe', items[f'account_sync_{acc.id}'])

    def test_two_real_jobs_close_together_are_still_marked(self):
        """(b) - red needs two runs whose measured lengths overlap; a job with no estimate
        is a point, and two points at one instant read as "within 5 minutes"."""
        from app.database import JOB_RUN_SUCCESS, record_job_run
        start = datetime(2026, 1, 1)
        record_job_run('config_backup_daily', start, start + timedelta(minutes=6),
                       JOB_RUN_SUCCESS)
        record_job_run('recording_retention_daily', start, start + timedelta(minutes=4),
                       JOB_RUN_SUCCESS)
        items = self._items([
            FakeJob('db_maintenance_daily', self.now + timedelta(minutes=10)),
            FakeJob('config_backup_daily', self.now + timedelta(minutes=12)),
            FakeJob('recording_retention_daily', self.now + timedelta(minutes=14)),
        ])
        self.assertEqual(items['db_maintenance_daily']['overlap'], 'yellow')
        self.assertEqual(items['config_backup_daily']['overlap'], 'red')
        self.assertEqual(items['recording_retention_daily']['overlap'], 'red')

    def test_every_probe_is_a_job_the_scheduler_registers(self):
        """The set is spelled by hand, so a renamed job would silently fall out of it."""
        from app.routes.jobs import RECURRING_META
        for probe in PROBE_JOBS:
            self.assertIn(probe, RECURRING_META, probe)

    def test_the_page_explains_the_probe_dot_and_the_legend_names_the_rule(self):
        """(c)"""
        jobs = [FakeJob('storage_dirs_check', self.now + timedelta(minutes=10)),
                FakeJob('db_maintenance_daily', self.now + timedelta(minutes=11))]
        with mock.patch.object(scheduler_mod, 'get_scheduler',
                               return_value=fake_scheduler(jobs)):
            html = self.t.client.get('/jobs').get_data(as_text=True)
        self.assertIn('Not compared&#10;A quick check', html)
        self.assertIn('Quick checks that run every few minutes are left out', html)
        self.assertNotIn('<tr data-overlap="yellow">', html)
        self.assertEqual(html.count('<tr data-overlap="green">'), 2)


if __name__ == '__main__':
    unittest.main()
