"""Tier 0 - the Live Dashboard's two self-refreshing sections, in a real DOM.

Two of the Dashboard's five sections used to go stale on a page left open: the accounts
rows said "Synced 4m ago" an hour later and never noticed a sync start, finish or fail,
and the health section only ever came alive if the page happened to load DURING a run,
because startHealthCheckWidget() returned at once when the server had rendered no
#hc-card. Both now swap in a fresh server render off base.html's /api/nav-status poll
(dev/changelog/1082). Invariants:

  (a) Both hooks are registered, and a payload that agrees with what the page already
      shows fetches nothing.
  (b) A sync signature that differs from the one the accounts section was rendered at
      swaps that section, and only that section, and then converges.
  (c) A sort the user picked survives the swap, and the swapped-in headers still sort.
  (d) A health check that starts after page load puts the card on the page and starts its
      poll; repeated nav polls during the run never start a second poll chain.
  (e) A run that ends is noticed by the card's own poll, which restores the empty state
      without reloading the page and then stops asking.
  (f) A failed refresh leaves the page exactly as it was, says so, and is retried.
  (g) An unchanged signature still refreshes once the render is a minute old, in a
      visible tab and a visible section only.

tests/support/dashboard_sections.mjs replays each scenario against the markup the
dashboard route really rendered; every assertion lives here.

  python3 -m unittest tests.test_dashboard_section_refresh_js
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

from app import db  # noqa: E402
from app.database import Account, AccountSyncLog  # noqa: E402
from app.routes.dashboard import BG_KIND_HEALTH_CHECK  # noqa: E402
from tests.support import seed  # noqa: E402
from tests.support.app import make_test_app  # noqa: E402

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HARNESS = os.path.join(REPO, 'tests', 'support', 'dashboard_sections.mjs')
JSDOM = os.path.join(REPO, 'node_modules', 'jsdom')

_RESULT = None

# Four database states the scenarios move the fake server between. idle and checking share
# their ACCOUNT state on purpose, so the sync signature does not move underneath a health
# scenario and add a page fetch nobody asked for.
STATES = ('idle', 'checking', 'syncing', 'synced')


def _observe():
    """Render the four states once, drive every scenario once in node, return it all."""
    global _RESULT
    if _RESULT is not None:
        return _RESULT
    import app.channel_tester as ct
    t = make_test_app()
    tmp = tempfile.mkdtemp(prefix='dashboard_sections_js_')
    try:
        client = t.app.test_client()

        def snapshot(state):
            pages = {'html': '/', 'json': '/api/nav-status'}
            for suffix, url in pages.items():
                resp = client.get(url)
                if resp.status_code != 200:
                    raise AssertionError(f'{url} answered {resp.status_code} at state {state}')
                with open(os.path.join(tmp, f'{state}.{suffix}'), 'w', encoding='utf-8') as f:
                    f.write(resp.get_data(as_text=True))
            run = client.get('/api/channel-tests/active-run')
            return json.loads(run.get_data(as_text=True))

        active_run = {}
        # Created out of alphabetical order, because the rows render in creation order and
        # a name sort that changes nothing proves nothing.
        base = datetime.utcnow() - timedelta(days=1)
        zulu = seed.make_account(name='Zulu', channel_count=50, created_at=base)
        alpha = seed.make_account(name='Alpha', channel_count=100,
                                  created_at=base + timedelta(minutes=1))
        alpha_id, zulu_id = alpha.id, zulu.id
        db.session.commit()
        active_run['idle'] = snapshot('idle')

        # A health check running, with nothing else changed.
        with t.app.app_context():
            chans = [seed.make_channel(db.session.get(Account, zulu_id), name=f'Ch {i}')
                     for i in range(4)]
            job = seed.make_test_job(name='Nightly', channels=chans, status='RUNNING')
            db.session.commit()
            job_id = job.id
        with ct._lock:
            ct._state = ct.RunState(
                is_running=True, current_phase='testing', current_job_id=job_id,
                current_channel_name='Ch 1', total_channels=4, completed_channels=1,
                nominal_channel_seconds=35.0,
                run_started_at=datetime.utcnow() - timedelta(seconds=40),
                eta_seconds=105,
            )
        active_run['checking'] = snapshot('checking')

        # A SECOND run, of a differently-named job, started at a different instant. The
        # nav payload carries a health check in both states, so nothing on that side can
        # tell them apart - only run_started_at does.
        with t.app.app_context():
            chans2 = [seed.make_channel(db.session.get(Account, zulu_id), name=f'Two {i}')
                      for i in range(3)]
            job2 = seed.make_test_job(name='Weekend sweep', channels=chans2, status='RUNNING')
            db.session.commit()
            job2_id = job2.id
        with ct._lock:
            ct._state = ct.RunState(
                is_running=True, current_phase='testing', current_job_id=job2_id,
                current_channel_name='Two 0', total_channels=3, completed_channels=2,
                nominal_channel_seconds=35.0,
                run_started_at=datetime.utcnow() - timedelta(seconds=5),
                eta_seconds=35,
            )
        active_run['checking2'] = snapshot('checking2')
        with ct._lock:
            ct._state = ct.RunState()

        # A sync running on Alpha.
        db.session.get(Account, alpha_id).status = 'SYNCING'
        log = AccountSyncLog(account_id=alpha_id, started_at=datetime.utcnow(),
                             status='IN_PROGRESS')
        db.session.add(log)
        db.session.commit()
        log_id = log.id
        active_run['syncing'] = snapshot('syncing')

        # ...and finished.
        acc = db.session.get(Account, alpha_id)
        acc.status = 'OK'
        acc.channel_count = 120
        acc.last_sync_at = datetime.utcnow()
        log = db.session.get(AccountSyncLog, log_id)
        log.status = 'SUCCESS'
        log.completed_at = datetime.utcnow()
        log.channels_synced = 120
        db.session.commit()
        active_run['synced'] = snapshot('synced')

        with open(os.path.join(tmp, 'active-run.json'), 'w', encoding='utf-8') as f:
            json.dump(active_run, f)

        proc = subprocess.run([shutil.which('node'), HARNESS, tmp, REPO],
                              capture_output=True, text=True, timeout=180, cwd=REPO)
        if proc.returncode != 0:
            raise AssertionError(f'harness failed:\n{proc.stdout[-2000:]}\n{proc.stderr[-4000:]}')
        _RESULT = json.loads(proc.stdout)
        return _RESULT
    finally:
        with ct._lock:
            ct._state = ct.RunState()
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


class BootTests(_Base):
    SCENARIO = 'boot'

    def test_both_hooks_are_registered_on_the_nav_status_poll(self):
        """base.html calls whichever hooks a page defines. A page that defines neither
        renders identically and simply never updates."""
        self.assertEqual(self.obs['accountHook'], 'function')
        self.assertEqual(self.obs['backgroundHook'], 'function')

    def test_the_accounts_section_carries_the_signature_the_poll_reports(self):
        """The comparison is between two readings of accounts.sync_signature(), so the
        page's attribute has to be that same string, not a lookalike."""
        self.assertEqual(self.obs['syncSig'], self.obs['navSig'])

    def test_a_payload_that_agrees_with_the_page_fetches_nothing(self):
        """base.html's own first poll has run by now. A page that re-rendered on every
        poll would refetch itself every 15 seconds for the life of the tab."""
        self.assertEqual(self.obs['pageFetches'], 0)

    def test_no_health_card_means_no_poll_at_all(self):
        self.assertFalse(self.obs['hasCard'])
        self.assertEqual(self.obs['runFetches'], 0)


class SyncFinishedTests(_Base):
    SCENARIO = 'sync_finished'

    def test_the_section_is_swapped_for_a_fresh_server_render(self):
        self.assertEqual(self.obs['pageFetches'], 1)
        self.assertFalse(self.obs['sameSection'])
        self.assertIn('alpha:OK', self.obs['statuses'])

    def test_only_that_section_is_replaced(self):
        """One writer per region: the recording sections are SSE's, and a swap that took
        them with it would wipe every live value on the page."""
        self.assertTrue(self.obs['otherSectionUntouched'])

    def test_the_section_keeps_its_place_in_the_customize_order(self):
        self.assertEqual(self.obs['order'][0], 'timeline')
        self.assertIn('accounts', self.obs['order'])
        self.assertEqual(len(self.obs['order']), len(set(self.obs['order'])))

    def test_the_section_takes_the_new_signature_and_then_settles(self):
        self.assertEqual(self.obs['syncSig'], self.obs['navSig'])
        self.assertEqual(self.obs['pageFetchesAfterNextPoll'], 1)


class SortTests(_Base):
    SCENARIO = 'sort_survives'

    def test_the_sort_was_actually_applied_before_the_swap(self):
        self.assertEqual(self.obs['sortedBefore'], 'name:▴')
        self.assertEqual(self.obs['namesBefore'], ['alpha', 'zulu'])

    def test_the_sort_survives_the_swap(self):
        """A rebuild re-applies every piece of active state. The sort lived in a closure
        bound to the old headers, so it silently reverted to the server's order."""
        self.assertEqual(self.obs['sortedAfter'], 'name:▴')
        self.assertEqual(self.obs['namesAfter'], ['alpha', 'zulu'])

    def test_the_swapped_in_headers_still_sort(self):
        self.assertEqual(self.obs['reversible']['sorted'], 'name:▾')
        self.assertEqual(self.obs['reversible']['names'], ['zulu', 'alpha'])


class UnsortedTests(_Base):
    SCENARIO = 'unsorted_stays'

    def test_an_unsorted_section_stays_in_the_servers_order(self):
        """Unsorted is a real state, not a missing one: nothing may invent a sort for a
        user who never picked one."""
        self.assertEqual(self.obs['namesBefore'], ['zulu', 'alpha'])
        self.assertEqual(self.obs['namesAfter'], ['zulu', 'alpha'])
        self.assertIsNone(self.obs['sorted'])


class CheckStartsTests(_Base):
    SCENARIO = 'check_starts'

    def test_the_page_opened_with_no_run(self):
        self.assertFalse(self.obs['before']['hasCard'])
        self.assertTrue(self.obs['before']['empty'])
        self.assertEqual(self.obs['before']['runFetches'], 0)

    def test_a_run_that_starts_later_puts_the_card_on_the_page(self):
        self.assertTrue(self.obs['after']['hasCard'])
        self.assertFalse(self.obs['after']['empty'])
        self.assertEqual(self.obs['after']['pageFetches'], 1)

    def test_the_fresh_card_starts_its_own_poll(self):
        self.assertTrue(self.obs['pollingAfterStart'])
        self.assertEqual(self.obs['after']['progress'], '1 / 4')

    def test_the_card_shows_the_running_badge_not_a_finished_one(self):
        self.assertIn('RUNNING', self.obs['after']['badge'])


class OnePollChainTests(_Base):
    SCENARIO = 'one_poll_chain'

    def test_the_run_is_polled(self):
        self.assertTrue(self.obs['polled'])

    def test_starting_the_widget_again_never_adds_a_second_chain(self):
        """refreshSection('health') re-enters startHealthCheckWidget on every swap, and a
        run that outlives a swap would leave two chains writing one card and asking
        /api/channel-tests/active-run twice as often. Counted as polls in flight at once
        against an endpoint that never answers - one chain parks exactly one request."""
        self.assertEqual(self.obs['chains'], 1)

    def test_a_steady_run_swaps_nothing(self):
        self.assertEqual(self.obs['pageFetches'], 0)
        self.assertTrue(self.obs['hasCard'])


class RunChangesTests(_Base):
    SCENARIO = 'run_changes'

    def test_the_card_started_on_the_first_run(self):
        self.assertEqual(self.obs['first']['name'], 'Nightly')
        self.assertEqual(self.obs['first']['progress'], '1 / 4')

    def test_a_second_run_replaces_the_card_rather_than_counting_under_the_old_name(self):
        """The nav payload carries a health check at both readings, so the background hook
        has no edge to react to; only the card's own poll can tell the runs apart. Without
        this the cells would keep updating under the finished run's name and link."""
        self.assertEqual(self.obs['second']['name'], 'Weekend sweep')
        self.assertEqual(self.obs['second']['progress'], '2 / 3')
        self.assertEqual(self.obs['pageFetches'], 1)
        self.assertTrue(self.obs['hasCard'])

    def test_the_live_chain_adopts_the_new_card_rather_than_doubling(self):
        """The swap re-enters the widget while the run is still going, which is the one
        path that can leave two chains on one card."""
        self.assertEqual(self.obs['chains'], 1)


class CheckEndsTests(_Base):
    SCENARIO = 'check_ends'

    def test_the_page_opened_mid_run(self):
        self.assertTrue(self.obs['running']['hasCard'])
        self.assertIn('RUNNING', self.obs['running']['badge'])

    def test_the_finished_run_restores_the_empty_state(self):
        self.assertFalse(self.obs['ended']['hasCard'])
        self.assertTrue(self.obs['ended']['empty'])
        self.assertEqual(self.obs['ended']['pageFetches'], 1)

    def test_it_does_not_reload_the_page(self):
        """The finished run used to trigger location.reload(), which threw away the
        timeline's scroll position, every section's sort and the SSE connection to
        whatever was still recording, to refresh one row."""
        self.assertEqual(self.obs['ended']['navigations'], 0)

    def test_the_poll_stops_with_the_card(self):
        self.assertTrue(self.obs['ended']['pollStopped'])

    def test_the_nav_poll_then_agrees_and_asks_for_nothing_more(self):
        self.assertEqual(self.obs['ended']['pageFetchesAfterNavPoll'], 1)


class FailedRefreshTests(_Base):
    SCENARIO = 'failed_refresh'

    def test_a_failed_refresh_leaves_the_page_as_it_was(self):
        self.assertIn('alpha:SYNCING', self.obs['failed']['statuses'])
        self.assertEqual(self.obs['failed']['pageFetches'], 1)

    def test_it_says_so(self):
        self.assertTrue(any('refresh failed' in w for w in self.obs['failed']['warnings']),
                        self.obs['failed']['warnings'])

    def test_the_signature_is_not_advanced_by_a_failure(self):
        """Stamping the new signature on a section that was never replaced would make the
        page believe it was current, and nothing would ever retry."""
        self.assertIn('alpha:SYNCING', self.obs['failed']['statuses'])

    def test_the_next_poll_retries(self):
        self.assertEqual(self.obs['retried']['pageFetches'], 2)
        self.assertIn('alpha:OK', self.obs['retried']['statuses'])


class AgedTests(_Base):
    SCENARIO = 'aged'

    def test_a_fresh_render_is_not_refetched(self):
        self.assertEqual(self.obs['fresh'], 0)

    def test_a_minute_old_render_refreshes_only_in_a_visible_tab(self):
        """"Synced 4m ago" and "in 23h 44m" drift while nothing changes, so the signature
        alone cannot keep them true - but nobody is reading a background tab."""
        self.assertEqual(self.obs['hiddenTab'], 0)
        self.assertEqual(self.obs['visibleTab'], 1)

    def test_the_refresh_restarts_the_minute(self):
        self.assertEqual(self.obs['rightAfter'], 1)


class HiddenSectionTests(_Base):
    SCENARIO = 'hidden_section'

    def test_a_section_hidden_in_customize_is_not_refetched_on_the_tick(self):
        self.assertEqual(self.obs['whileHidden'], 0)

    def test_a_real_change_still_reaches_a_hidden_section(self):
        """Otherwise unhiding it would reveal a sync state from whenever the page loaded."""
        self.assertEqual(self.obs['onChange']['pageFetches'], 1)
        self.assertIn('alpha:SYNCING', self.obs['onChange']['statuses'])


class PayloadContractTests(unittest.TestCase):
    """The server half: the client keys a decision off `kind`, so `kind` has to be there.

    Matching on `label` was the alternative, and it is the shape that breaks silently -
    this payload's labels have already been reworded twice (dev/changelog/867, /954).
    """

    def setUp(self):
        self.t = make_test_app()
        self.client = self.t.app.test_client()

    def tearDown(self):
        import app.channel_tester as ct
        with ct._lock:
            ct._state = ct.RunState()
        self.t.cleanup()

    def test_a_running_health_check_carries_the_health_kind(self):
        import app.channel_tester as ct
        with self.t.app.app_context():
            acc = seed.make_account()
            chans = [seed.make_channel(acc, name=f'Ch {i}') for i in range(2)]
            job = seed.make_test_job(name='Nightly', channels=chans, status='RUNNING')
            db.session.commit()
            job_id = job.id
        with ct._lock:
            ct._state = ct.RunState(is_running=True, current_phase='testing',
                                    current_job_id=job_id, total_channels=2,
                                    completed_channels=0,
                                    run_started_at=datetime.utcnow())
        payload = self.client.get('/api/nav-status').get_json()
        kinds = [t.get('kind') for t in payload['activity']['background']['tasks']]
        self.assertIn(BG_KIND_HEALTH_CHECK, kinds)

    def test_every_background_task_carries_a_kind(self):
        """A reader added later should not have to go back and fill the field in at four
        call sites first."""
        with self.t.app.app_context():
            acc = seed.make_account()
            acc.status = 'SYNCING'
            seed.make_recording(status='CONVERTING', name='A film')
            db.session.commit()
        tasks = self.client.get('/api/nav-status').get_json()['activity']['background']['tasks']
        self.assertTrue(tasks)
        for task in tasks:
            self.assertTrue(task.get('kind'), task)

    def test_the_page_hands_the_health_kind_to_its_script(self):
        """dashboard.js reads the constant out of the page rather than re-typing it, the
        same way it reads the status labels."""
        html = self.client.get('/').get_data(as_text=True)
        self.assertIn('id="dash-bg-health-kind"', html)
        self.assertIn(json.dumps(BG_KIND_HEALTH_CHECK), html)

    def test_the_accounts_section_is_rendered_with_the_sync_signature(self):
        from app.accounts import sync_signature
        with self.t.app.app_context():
            seed.make_account(name='Alpha')
            db.session.commit()
            expected = sync_signature()
        html = self.client.get('/').get_data(as_text=True)
        self.assertIn(f'data-sec="accounts" data-sync-sig="{expected}"', html)


if __name__ == '__main__':
    unittest.main()
