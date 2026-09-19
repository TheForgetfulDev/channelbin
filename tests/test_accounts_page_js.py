"""Tier 0 - the /accounts list keeping itself current, driven in a real DOM.

The list was load-once: a sync that finished while the page was open left its row reading
SYNCING, its next sync reading "running" and its numbers stale until a manual reload
(dev/docs/BUGS.md 2026-09-11 06:38). It now re-renders when base.html's /api/nav-status
poll reports a sync signature its rows were not rendered at, and every part of that happens
in the browser - a page whose hook never registered serves exactly the same markup, so none
of it is visible from a response body.

tests/support/accounts_page.mjs runs the shipped util.js and account scripts against the
pages and nav-status payloads the Flask app really answered at three database states (no
accounts, one mid-sync, that sync finished) and reports; every assertion lives here, so a
failure reads as a sentence about the list.

What it cannot cover: jsdom computes no layout, so the column widths and the hidden count's
own line are browser work. dev/changelog/921.

Runs against a throwaway temp SQLite DB - never the live dvr.db.
  python3 -m unittest tests.test_accounts_page_js
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tests.support.app import make_test_app  # noqa: E402
from tests.support import seed  # noqa: E402
from app import account_stats, db  # noqa: E402
from app.database import Account, AccountStatDay, AccountSyncLog  # noqa: E402

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HARNESS = os.path.join(REPO, 'tests', 'support', 'accounts_page.mjs')
JSDOM = os.path.join(REPO, 'node_modules', 'jsdom')

_RESULT = None


def _observe():
    """Render the three states once, drive every scenario once in node, return it all."""
    global _RESULT
    if _RESULT is not None:
        return _RESULT
    t = make_test_app()
    tmp = tempfile.mkdtemp(prefix='accounts_page_js_')
    try:
        client = t.app.test_client()

        def snapshot(state):
            for suffix, url in (('html', '/accounts'), ('json', '/api/nav-status')):
                resp = client.get(url)
                if resp.status_code != 200:
                    raise AssertionError(f'{url} answered {resp.status_code} at state {state}')
                with open(os.path.join(tmp, f'{state}.{suffix}'), 'w', encoding='utf-8') as f:
                    f.write(resp.get_data(as_text=True))

        snapshot('empty')

        alpha_id = seed.make_account(name='Alpha', channel_count=100).id
        beta_id = seed.make_account(name='Beta', channel_count=50).id
        # Ledger rows for today, so the stats section has a table worth sorting and a trend
        # column worth hovering: Beta recorded more, Alpha was checked and Beta never was.
        today = account_stats.today_local().isoformat()
        for aid, capture, passed, failed in ((alpha_id, 3600, 5, 5), (beta_id, 7200, 0, 0)):
            db.session.add(AccountStatDay(account_id=aid, day=today, capture_seconds=capture,
                                          segments=1, recordings=1, stalls=0,
                                          checks_passed=passed, checks_failed=failed,
                                          failovers_away=0))
        db.session.get(Account, alpha_id).status = 'SYNCING'
        log = AccountSyncLog(account_id=alpha_id, started_at=datetime.utcnow(),
                             status='IN_PROGRESS')
        db.session.add(log)
        db.session.commit()
        log_id = log.id
        snapshot('syncing')

        alpha = db.session.get(Account, alpha_id)
        alpha.status = 'OK'
        alpha.channel_count = 120
        alpha.last_sync_at = datetime.utcnow()
        log = db.session.get(AccountSyncLog, log_id)
        log.status = 'SUCCESS'
        log.completed_at = datetime.utcnow()
        log.channels_synced = 120
        db.session.commit()
        snapshot('done')

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

    def test_the_list_registers_its_hook_on_the_nav_status_poll(self):
        self.assertEqual(self.obs['hook'], 'function')

    def test_the_rows_were_rendered_at_the_signature_the_poll_reports(self):
        self.assertTrue(self.obs['navSig'], 'nav-status must carry account_sync')
        self.assertEqual(self.obs['liveSig'], self.obs['navSig'])

    def test_an_unchanged_signature_fetches_nothing(self):
        """base.html's own first poll has run by now. A list that re-rendered on every
        poll would cost a page render per open tab every 15 seconds for nothing."""
        self.assertEqual(self.obs['pageFetches'], 0)
        self.assertEqual(self.obs['alpha']['status'], 'SYNCING')


class SyncFinishesTests(_Base):
    """The reported defect itself (dev/docs/BUGS.md 2026-09-11 06:38)."""
    SCENARIO = 'finished'

    def test_the_finished_sync_replaces_the_syncing_row(self):
        self.assertEqual(self.obs['pageFetches'], 1)
        self.assertEqual(self.obs['alpha']['status'], 'OK')
        self.assertIn('st-ok', self.obs['alpha']['cls'])
        self.assertNotIn('st-sync', self.obs['alpha']['cls'])

    def test_the_kebab_offers_sync_again_rather_than_cancel(self):
        self.assertIn('sync', self.obs['alpha']['acts'])
        self.assertNotIn('cancel-sync', self.obs['alpha']['acts'])

    def test_the_header_total_moves_with_the_rows(self):
        """The header total is the sum of the column beneath it - a swap that refreshed the
        rows and not the total would make the page disagree with itself."""
        self.assertIn('150 channels', self.obs['totalBefore'])
        self.assertIn('170 channels', self.obs['totalAfter'])

    def test_the_region_takes_the_new_signature_and_then_settles(self):
        """The swapped-in region carries the signature it was rendered at, so the very next
        poll matches and fetches nothing. A region that kept the old one would re-render
        on every poll for the life of the tab."""
        self.assertFalse(self.obs['sameNode'], 'the row must be the fresh render, not patched')
        self.assertEqual(self.obs['liveSig'], self.obs['navSig'])
        self.assertEqual(self.obs['pageFetchesAfterNextPoll'], 1)

    def test_swapped_rows_still_navigate_and_their_dead_zones_still_do_not(self):
        """Handlers bound per row go with the row. A click on the fresh row's blank area
        opens the account; the sparkline and the kebab do not, and the kebab still opens."""
        self.assertEqual(self.obs['navAfterRowClick'], 1)
        self.assertEqual(self.obs['navAfterDeadZoneClicks'], 1)
        self.assertTrue(self.obs['menuOpensOnSwappedRow'])


class OpenMenuTests(_Base):
    SCENARIO = 'menu_open'

    def test_an_open_menu_holds_the_refresh_back(self):
        """Replacing the row under an open kebab would close it mid-choice."""
        self.assertTrue(self.obs['menuOpen'])
        self.assertEqual(self.obs['whileOpen']['pageFetches'], 0)
        self.assertEqual(self.obs['whileOpen']['alpha']['status'], 'SYNCING')

    def test_the_refresh_lands_once_the_menu_closes(self):
        self.assertEqual(self.obs['afterClose']['pageFetches'], 1)
        self.assertEqual(self.obs['afterClose']['alpha']['status'], 'OK')


class AgedRenderTests(_Base):
    """"Synced 4m ago" and "in 23h 44m" drift while no sync changes anything."""
    SCENARIO = 'aged'

    def test_a_fresh_render_is_not_refetched(self):
        self.assertEqual(self.obs['fresh'], 0)

    def test_a_minute_old_render_refreshes_only_in_a_visible_tab(self):
        self.assertEqual(self.obs['hiddenTab'], 0)
        self.assertEqual(self.obs['visibleTab'], 1)

    def test_the_refresh_restarts_the_minute(self):
        self.assertEqual(self.obs['rightAfter'], 1)


class FailedRefreshTests(_Base):
    SCENARIO = 'failed_fetch'

    def test_a_failed_refresh_leaves_the_page_as_it_was_and_says_so(self):
        failed = self.obs['failed']
        self.assertEqual(failed['pageFetches'], 1)
        self.assertEqual(failed['alpha']['status'], 'SYNCING')
        self.assertEqual(failed['liveSig'], self.obs['syncingSig'],
                         'a 500 page must not be swapped in as if it were the list')
        self.assertTrue(any('refresh failed' in w for w in failed['warnings']), failed['warnings'])

    def test_the_next_poll_retries(self):
        """The in-flight guard is released on failure too, or one bad answer would stop
        the list refreshing for the life of the tab."""
        self.assertEqual(self.obs['retried']['pageFetches'], 2)
        self.assertEqual(self.obs['retried']['alpha']['status'], 'OK')


class FromEmptyTests(_Base):
    SCENARIO = 'from_empty'

    def test_the_first_accounts_replace_the_empty_state_without_a_reload(self):
        self.assertEqual(self.obs['before'], {'rows': 0, 'empty': True})
        self.assertEqual(self.obs['after']['rows'], 2)
        self.assertFalse(self.obs['after']['empty'])
        self.assertIn('2 accounts', self.obs['after']['total'])


if __name__ == '__main__':
    unittest.main(verbosity=2)


class StatsSectionTests(_Base):
    """The account stats under the list (dev/changelog/1029) are outside the live region:
    the minute swap must not redraw them or throw away a sort the user picked."""
    SCENARIO = 'stats_swap'

    def test_the_list_swap_leaves_the_stats_section_alone(self):
        self.assertTrue(self.obs['hadSection'])
        self.assertEqual(self.obs['pageFetches'], 1, 'the list itself did refresh')
        self.assertTrue(self.obs['sameSection'], 'the section was replaced by the swap')
        self.assertTrue(self.obs['sortKept'])


class WindowChipTests(_Base):
    SCENARIO = 'window_chip'

    def test_a_chip_saves_the_window_to_the_shared_pref_then_follows_its_link(self):
        self.assertEqual(len(self.obs['posts']), 1)
        post = self.obs['posts'][0]
        self.assertEqual(post['path'], '/api/user-prefs/account_stats_window')
        self.assertEqual(json.loads(post['body']), {'value': '7d'})
        self.assertEqual(self.obs['navigations'], 1)


class TableSortTests(_Base):
    SCENARIO = 'sort'

    def test_names_sort_both_ways_and_the_totals_row_stays_last(self):
        self.assertEqual(self.obs['initial'], ['alpha', 'beta', 'total'])
        self.assertEqual(self.obs['nameAsc'], ['alpha', 'beta', 'total'])
        self.assertEqual(self.obs['nameDesc'], ['beta', 'alpha', 'total'])

    def test_a_number_column_sorts_biggest_first(self):
        self.assertEqual(self.obs['captureDesc'], ['beta', 'alpha', 'total'])
        self.assertEqual(self.obs['ariaCapture'], 'descending')

    def test_an_unmeasured_pass_rate_sorts_last_and_the_phone_chip_drives_the_same_sort(self):
        """Beta was never checked. Its pass rate is no measurement, not 0%, so it goes
        last rather than being read as the worst."""
        self.assertEqual(self.obs['rateDesc'], ['alpha', 'beta', 'total'])
        self.assertIn('Pass rate', self.obs['chipLabel'])


class ColumnTooltipTests(_Base):
    SCENARIO = 'column_tip'

    def test_the_column_tooltip_names_each_account_in_its_color(self):
        shown = self.obs['shown']
        self.assertTrue(shown['visible'])
        self.assertIn('Alpha', shown['text'])
        self.assertIn('Beta', shown['text'])
        self.assertEqual(len(shown['dots']), 2)

    def test_a_tap_elsewhere_closes_it(self):
        self.assertTrue(self.obs['hiddenAfterTapElsewhere'])
