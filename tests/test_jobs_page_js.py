"""Tier 0 - the /jobs table keeping itself current, driven in a real DOM.

The Scheduled Jobs page was load-once behind a manual Refresh button: next-run times and
"in 23m" countdowns never moved, and a job that fired kept its old row until a reload. It
now swaps in a fresh server render when base.html's /api/nav-status poll reports that the
running background work or the recording signature moved, and once a minute in a visible
tab (dev/changelog/1164). All of that happens in the browser - a page whose hooks never
registered serves exactly the same markup.

tests/support/jobs_page.mjs runs the shipped util.js and jobs.js against pages the Flask app
really rendered (the job list patched between three states) and reports; every assertion
lives here.

Runs against a throwaway temp SQLite DB - never the live dvr.db.
  python3 -m unittest tests.test_jobs_page_js
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tests.support.app import make_test_app  # noqa: E402

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HARNESS = os.path.join(REPO, 'tests', 'support', 'jobs_page.mjs')
JSDOM = os.path.join(REPO, 'node_modules', 'jsdom')


def _job(job_id, name, next_run, relative, **extra):
    job = {'id': job_id, 'display_name': name, 'type': 'recurring',
           'next_run_et': next_run, 'next_run_relative': relative, 'stop_run_et': None,
           'schedule_description': 'every 12 hours', 'edit_url': None, 'overlap': 'green'}
    job.update(extra)
    return job


# State A: the sync is due; state B: it has fired, moved on, and a recording was scheduled.
STATE_A = [
    _job('account_sync_1', 'Account sync: Alpha', 'Sep 29, 2026 03:00 PM', 'in 5m',
         skip_url='/api/jobs/account_sync_1/skip'),
    _job('config_backup_daily', 'Config Backup', 'Sep 30, 2026 04:00 AM', 'in 13h'),
]
STATE_B = [
    _job('account_sync_1', 'Account sync: Alpha', 'Sep 30, 2026 03:00 AM', 'in 12h',
         skip_url='/api/jobs/account_sync_1/skip'),
    _job('config_backup_daily', 'Config Backup', 'Sep 30, 2026 04:00 AM', 'in 13h'),
    _job('start_7', 'Record: The Bear', 'Sep 29, 2026 09:00 PM', 'in 6h', type='one_off'),
]

_RESULT = None


def _observe():
    global _RESULT
    if _RESULT is not None:
        return _RESULT
    t = make_test_app()
    tmp = tempfile.mkdtemp(prefix='jobs_page_js_')
    try:
        client = t.app.test_client()

        def write(name, body):
            with open(os.path.join(tmp, name), 'w', encoding='utf-8') as f:
                f.write(body)

        for state, jobs in (('empty', []), ('a', STATE_A), ('b', STATE_B)):
            with mock.patch('app.routes.jobs._build_job_list', return_value=jobs):
                resp = client.get('/jobs')
            if resp.status_code != 200:
                raise AssertionError(f'/jobs answered {resp.status_code} at state {state}')
            write(f'{state}.html', resp.get_data(as_text=True))
        resp = client.get('/api/nav-status')
        if resp.status_code != 200:
            raise AssertionError(f'/api/nav-status answered {resp.status_code}')
        write('nav.json', resp.get_data(as_text=True))

        proc = subprocess.run([shutil.which('node'), HARNESS, tmp, REPO],
                              capture_output=True, text=True, timeout=120, cwd=REPO)
        if proc.returncode != 0:
            raise AssertionError(f'harness failed:\n{proc.stdout[-2000:]}\n{proc.stderr[-4000:]}')
        _RESULT = json.loads(proc.stdout)
        return _RESULT
    finally:
        t.cleanup()
        shutil.rmtree(tmp, ignore_errors=True)


@unittest.skipIf(shutil.which('node') is None, 'node not installed')
@unittest.skipIf(not os.path.isdir(JSDOM), 'jsdom not installed (npm install)')
class _Base(unittest.TestCase):
    SCENARIO = ''

    @classmethod
    def setUpClass(cls):
        if not cls.SCENARIO:
            raise unittest.SkipTest('base class - carries the shared cases only')
        cls.obs = _observe()[cls.SCENARIO]
        if isinstance(cls.obs, dict) and 'error' in cls.obs:
            raise AssertionError(f'{cls.SCENARIO} threw in the page:\n{cls.obs["error"]}')

    def test_the_page_ran_without_errors(self):
        self.assertEqual(self.obs['errors'], [])


A_ROWS = ['Account sync: Alpha', 'Config Backup']
B_ROWS = ['Account sync: Alpha', 'Config Backup', 'Record: The Bear']


class PageAsItOpensTests(_Base):
    SCENARIO = 'boot'

    def test_the_page_registers_both_hooks_on_the_nav_status_poll(self):
        self.assertEqual(self.obs['hooks'], ['function', 'function'])

    def test_the_manual_refresh_button_is_gone(self):
        self.assertFalse(self.obs['refreshButton'])

    def test_polls_that_report_nothing_new_fetch_nothing(self):
        """A table that re-rendered on every poll would cost a page render per open tab
        every 15 seconds for nothing."""
        self.assertEqual(self.obs['pageFetches'], 0)
        self.assertEqual(self.obs['rows'], A_ROWS)


class BackgroundChangeTests(_Base):
    SCENARIO = 'background_change'

    def test_a_sync_starting_swaps_in_the_new_table_and_count(self):
        self.assertEqual(self.obs['after']['pageFetches'], 1)
        self.assertEqual(self.obs['after']['rows'], B_ROWS)
        self.assertEqual(self.obs['after']['count'], '3 jobs')

    def test_it_settles_once_the_change_is_taken(self):
        self.assertEqual(self.obs['afterNextPoll'], 1)


class RecordingChangeTests(_Base):
    """A recording's start/stop jobs are rows here, so its signature moving is a trigger."""
    SCENARIO = 'recording_change'

    def test_a_recording_signature_change_swaps_the_table(self):
        self.assertEqual(self.obs['pageFetches'], 1)
        self.assertEqual(self.obs['rows'], B_ROWS)


class OpenMenuTests(_Base):
    SCENARIO = 'menu_open'

    def test_an_open_menu_holds_the_refresh_back(self):
        self.assertTrue(self.obs['menuOpen'])
        self.assertEqual(self.obs['whileOpen']['pageFetches'], 0)
        self.assertEqual(self.obs['whileOpen']['rows'], A_ROWS)

    def test_the_held_change_lands_on_the_first_poll_after_the_menu_closes(self):
        """The change was reported while the menu was open; the next poll reports nothing
        new. A page that only remembered the last signature would drop it here and wait for
        the minute tick."""
        self.assertEqual(self.obs['afterClose']['pageFetches'], 1)
        self.assertEqual(self.obs['afterClose']['rows'], B_ROWS)


class AgedRenderTests(_Base):
    """"in 5m" drifts while nothing changes."""
    SCENARIO = 'aged'

    def test_a_fresh_render_is_not_refetched(self):
        self.assertEqual(self.obs['fresh'], 0)

    def test_a_minute_old_render_refreshes_only_in_a_visible_tab(self):
        self.assertEqual(self.obs['hiddenTab'], 0)
        self.assertEqual(self.obs['visibleTab'], 1)

    def test_the_refresh_restarts_the_minute(self):
        self.assertEqual(self.obs['rightAfter'], 1)


class SkipActionTests(_Base):
    SCENARIO = 'skip_action'

    def test_skip_swaps_the_table_instead_of_reloading_the_page(self):
        self.assertTrue(self.obs['modalShown'])
        self.assertEqual(self.obs['posts'], ['/api/jobs/account_sync_1/skip'])
        self.assertTrue(self.obs['modalGone'])
        self.assertEqual(self.obs['pageFetches'], 1)
        self.assertEqual(self.obs['rows'], B_ROWS)
        self.assertEqual(self.obs['navigations'], 0)


class FromEmptyTests(_Base):
    SCENARIO = 'from_empty'

    def test_the_first_jobs_replace_the_empty_state(self):
        self.assertEqual(self.obs['before'], {'rows': 0, 'empty': True, 'count': '0 jobs'})
        self.assertEqual(self.obs['after'], {'rows': 2, 'empty': False, 'count': '2 jobs'})


if __name__ == '__main__':
    unittest.main(verbosity=2)
