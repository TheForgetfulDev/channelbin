"""app/routes/jobs.py - Run Now must not be offered on jobs run_job_now() cannot run
(dev/changelog/613).

Before this, every recurring job listed in _RECURRING_META got a working-looking
run_url regardless of whether run_job_now() actually implemented it, so hc_window_dispatch,
hc_window_close, recording_retention_daily, db_maintenance_daily and logo_cache_fetch all
rendered a normal "Run now" menu item that a real click answered with a generic
"Run Now is not supported for this job" 400.

Runs against a throwaway temp SQLite DB - never the live dvr.db.
    python3 -m unittest tests.test_jobs_run_now_unsupported
"""
import os
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tests.support.app import make_test_app  # noqa: E402


class BuildJobListRunNowGatingTests(unittest.TestCase):
    """_build_job_list() must only attach run_url to jobs run_job_now() actually
    implements, and must explain itself for the rest rather than staying silent."""

    UNSUPPORTED = (
        'hc_window_dispatch', 'hc_window_close', 'recording_retention_daily',
        'db_maintenance_daily', 'logo_cache_fetch',
    )

    def setUp(self):
        self.t = make_test_app(start_scheduler=True)

    def tearDown(self):
        self.t.cleanup()

    def _items_by_id(self):
        from app.routes.jobs import _build_job_list
        return {i['id']: i for i in _build_job_list()}

    def test_config_backup_daily_keeps_a_working_run_url(self):
        item = self._items_by_id()['config_backup_daily']
        self.assertEqual(item['run_url'], '/api/jobs/config_backup_daily/run-now')
        self.assertNotIn('run_disabled_reason', item)

    def test_unsupported_recurring_jobs_get_no_run_url(self):
        items = self._items_by_id()
        for job_id in self.UNSUPPORTED:
            self.assertNotIn('run_url', items[job_id],
                              f'{job_id} must not offer a Run Now button run_job_now() '
                              'will 400 on')

    def test_unsupported_recurring_jobs_carry_a_non_empty_reason(self):
        items = self._items_by_id()
        for job_id in self.UNSUPPORTED:
            reason = items[job_id].get('run_disabled_reason')
            self.assertTrue(reason, f'{job_id} must explain why Run Now is unavailable')
            self.assertIn('not supported yet', reason)

    def test_dispatch_reason_names_the_configured_interval(self):
        """The interval in the toast must reflect the real config value, not a hardcoded
        default - otherwise a user who changed dispatch_interval_minutes gets told the
        wrong number for their own setting."""
        from app import config as config_mod

        real_load_config = config_mod.load_config

        def patched(*args, **kwargs):
            cfg = real_load_config(*args, **kwargs)
            cfg['channel_testing']['window']['dispatch_interval_minutes'] = 17
            return cfg

        with patch('app.config.load_config', side_effect=patched):
            item = self._items_by_id()['hc_window_dispatch']

        self.assertIn('17 minutes', item['run_disabled_reason'])
        self.assertIn('channel_testing.window.dispatch_interval_minutes',
                       item['run_disabled_reason'])


class RunNowRouteUnsupportedFallbackTests(unittest.TestCase):
    """The route-level 400 (defense in depth, per CLAUDE.md's "enforcement lives
    server-side") must still refuse a direct call for a job _build_job_list now
    marks unsupported."""

    def setUp(self):
        self.t = make_test_app(start_scheduler=True)
        self.client = self.t.app.test_client()

    def tearDown(self):
        self.t.cleanup()

    def test_direct_call_for_an_unsupported_job_still_answers_400(self):
        resp = self.client.post('/api/jobs/hc_window_dispatch/run-now')
        self.assertEqual(resp.status_code, 400)
        self.assertIn('error', resp.get_json())
