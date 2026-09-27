"""Tier 0 - the /alerts page keeping itself current, driven in a real DOM.

The Active and Past cards used to be load-once: the nav counts above them moved on every
poll while the page meant for reading alerts never showed a new one, nor a problem that
cleared itself, until a reload. The page now re-renders when base.html's /api/nav-status
poll reports an alert signature its cards were not rendered at, re-renders once a minute
so the ages stay true, and its own actions ask for that same re-render rather than editing
rows (dev/changelog/1131). Every part of that happens in the browser.

tests/support/alerts_page.mjs runs the shipped util.js, nav-alerts.js and alerts.js against
the pages and nav-status payloads the Flask app really answered at five database states and
reports; every assertion lives here.

Runs against a throwaway temp SQLite DB - never the live dvr.db.
  python3 -m unittest tests.test_alerts_page_js
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tests.support.app import make_test_app  # noqa: E402
from app import db  # noqa: E402
from app.database import Alert  # noqa: E402

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HARNESS = os.path.join(REPO, 'tests', 'support', 'alerts_page.mjs')
JSDOM = os.path.join(REPO, 'node_modules', 'jsdom')

_RESULT = None


def _observe():
    """Render the five states once, drive every scenario once in node, return it all."""
    global _RESULT
    if _RESULT is not None:
        return _RESULT
    t = make_test_app()
    tmp = tempfile.mkdtemp(prefix='alerts_page_js_')
    try:
        client = t.app.test_client()

        def snapshot(state):
            for suffix, url in (('html', '/alerts'), ('json', '/api/nav-status')):
                resp = client.get(url)
                if resp.status_code != 200:
                    raise AssertionError(f'{url} answered {resp.status_code} at state {state}')
                with open(os.path.join(tmp, f'{state}.{suffix}'), 'w', encoding='utf-8') as f:
                    f.write(resp.get_data(as_text=True))

        now = datetime.utcnow()
        # A standing problem the app clears itself (Active) and a one-time failure with a
        # body, so it has a details disclosure (Past).
        standing = Alert(alert_type='STORAGE_PATH_UNUSABLE', severity='ERROR',
                         title='DVR output directory is unusable', source='/dvr',
                         created_at=now - timedelta(minutes=30))
        failed = Alert(alert_type='CONCATENATION_FAILED', severity='ERROR',
                       title='Concatenation failed', body='ffmpeg exited 1\nsecond line',
                       source='concatenator', created_at=now - timedelta(minutes=20))
        db.session.add_all([standing, failed])
        db.session.commit()
        snapshot('base')

        note = Alert(alert_type='JOB_SKIPPED', severity='INFO', title='A job was skipped',
                     created_at=now - timedelta(minutes=1))
        db.session.add(note)
        db.session.commit()
        snapshot('raised')

        failed.read_at = datetime.utcnow()
        db.session.commit()
        snapshot('read')

        standing.dismissed_at = datetime.utcnow()
        db.session.commit()
        snapshot('cleared')

        for a in (failed, note):
            a.read_at = a.read_at or datetime.utcnow()
            a.dismissed_at = datetime.utcnow()
        db.session.commit()
        snapshot('empty')

        with open(os.path.join(tmp, 'ids.json'), 'w', encoding='utf-8') as f:
            json.dump({'standing': standing.id, 'failed': failed.id, 'note': note.id}, f)

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


class PageAsItOpensTests(_Base):
    SCENARIO = 'boot'

    def test_the_page_registers_its_hook_on_the_nav_status_poll(self):
        self.assertEqual(self.obs['hook'], 'function')

    def test_the_cards_were_rendered_at_the_signature_the_poll_reports(self):
        self.assertTrue(self.obs['navSig'], 'nav-status must carry alert_signature')
        self.assertEqual(self.obs['liveSig'], self.obs['navSig'])

    def test_an_unchanged_signature_fetches_nothing(self):
        """base.html's own first poll has run by now; a page that re-rendered on every poll
        would cost a render per open tab per poll for nothing."""
        self.assertEqual(self.obs['pageFetches'], 0)


class NewAlertTests(_Base):
    """The gap the item closed: a new alert reached the nav counts and never the page."""
    SCENARIO = 'raised'

    def test_the_new_alert_appears_without_a_reload(self):
        self.assertEqual(self.obs['before']['past'], ['failed'])
        self.assertEqual(self.obs['pageFetches'], 1)
        self.assertEqual(self.obs['past'], ['note', 'failed'])
        self.assertEqual(self.obs['active'], ['standing'])

    def test_the_unread_count_moves_with_the_cards(self):
        """An INFO alert never moves the nav's red or yellow count, so this header is the
        only count on screen that says it arrived."""
        self.assertEqual(self.obs['before']['unread'], '2 unread')
        self.assertEqual(self.obs['unread'], '3 unread')

    def test_the_region_takes_the_new_signature_and_then_settles(self):
        self.assertFalse(self.obs['sameNode'], 'rows must be the fresh render, not patched')
        self.assertEqual(self.obs['liveSig'], self.obs['navSig'])
        self.assertEqual(self.obs['pageFetchesAfterNextPoll'], 1)


class SelfClearedTests(_Base):
    SCENARIO = 'cleared'

    def test_a_problem_that_cleared_itself_leaves_the_active_card(self):
        self.assertEqual(self.obs['before'], ['active', 'past'])
        self.assertEqual(self.obs['after'], ['past'])


class EmptiedTests(_Base):
    SCENARIO = 'emptied'

    def test_the_empty_state_arrives_by_swap_not_by_reload(self):
        self.assertEqual(self.obs['cards'], [])
        self.assertTrue(self.obs['empty'])
        self.assertEqual(self.obs['navigations'], 0)
        self.assertEqual(self.obs['pageFetches'], 1)


class OpenMenuTests(_Base):
    SCENARIO = 'menu_open'

    def test_an_open_menu_holds_the_refresh_back(self):
        """Replacing the row under an open kebab would close it mid-choice."""
        self.assertTrue(self.obs['menuOpen'])
        self.assertEqual(self.obs['whileOpen']['pageFetches'], 0)
        self.assertEqual(self.obs['whileOpen']['past'], ['failed'])

    def test_the_refresh_lands_once_the_menu_closes(self):
        self.assertEqual(self.obs['afterClose']['pageFetches'], 1)
        self.assertEqual(self.obs['afterClose']['past'], ['note', 'failed'])


class OpenDetailsTests(_Base):
    """An open disclosure does not hold the page - one left open would freeze it - so the
    swap has to put it back open."""
    SCENARIO = 'details_open'

    def test_the_swap_is_not_held_by_an_open_disclosure(self):
        self.assertTrue(self.obs['openBefore'])
        self.assertEqual(self.obs['pageFetches'], 1)
        self.assertFalse(self.obs['sameNode'])

    def test_the_disclosure_is_open_on_the_fresh_row(self):
        self.assertTrue(self.obs['openAfter'])
        self.assertEqual(self.obs['toggleLabel'], 'Hide details')


class AgedRenderTests(_Base):
    """"20m 3s ago" drifts while no alert changes."""
    SCENARIO = 'aged'

    def test_a_fresh_render_is_not_refetched(self):
        self.assertEqual(self.obs['fresh'], 0)

    def test_a_minute_old_render_refreshes_only_in_a_visible_tab(self):
        self.assertEqual(self.obs['hiddenTab'], 0)
        self.assertEqual(self.obs['visibleTab'], 1)

    def test_the_refresh_restarts_the_minute(self):
        self.assertEqual(self.obs['rightAfter'], 1)


class MarkReadTests(_Base):
    """One writer per region: an action asks for the server's copy of the cards rather than
    editing the row, so the row says what the server says."""
    SCENARIO = 'mark_read'

    def test_the_page_does_not_write_the_row_itself(self):
        self.assertIn('/api/alerts/', self.obs['stale']['posts'][0])
        self.assertEqual(self.obs['stale']['pageFetches'], 1)
        self.assertTrue(self.obs['stale']['unread'])

    def test_the_servers_copy_lands_and_settles(self):
        landed = self.obs['landed']
        self.assertEqual(landed['pageFetches'], 2)
        self.assertFalse(landed['unread'])
        self.assertFalse(landed['readItem'], 'a read row offers no Mark read')
        self.assertEqual(landed['unreadLabel'], '2 unread')
        self.assertEqual(landed['liveSig'], landed['navSig'])

    def test_the_nav_counts_still_move_on_the_click(self):
        self.assertTrue(self.obs['landed']['navCountFetched'])


class FailedRefreshTests(_Base):
    SCENARIO = 'failed_fetch'

    def test_a_failed_refresh_leaves_the_page_and_says_so(self):
        self.assertEqual(self.obs['failed']['past'], ['failed'])
        self.assertTrue(any('refresh failed' in w for w in self.obs['failed']['warnings']))

    def test_the_next_poll_retries(self):
        self.assertEqual(self.obs['retried']['pageFetches'], 2)
        self.assertEqual(self.obs['retried']['past'], ['note', 'failed'])


if __name__ == '__main__':
    unittest.main()
