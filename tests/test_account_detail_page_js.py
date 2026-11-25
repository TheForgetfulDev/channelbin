"""Tier 0 - the account page keeping itself current through a sync, driven in a real DOM.

The page was load-once: it said "Syncing now" until reloaded, never moved "Next sync in",
and its sticky bottom bar kept offering Sync now after a sync had started. It now swaps its
live regions for a fresh server render when base.html's /api/nav-status poll reports a sync
signature it was not rendered at, or the render is a minute old - and every part of that
happens in the browser, so none of it is visible from a response body.

tests/support/account_detail_page.mjs runs the shipped util.js and account scripts against
the pages, nav-status payloads and syncs-API answers the Flask app really gave at three
database states (never synced, a sync running, that sync finished) and reports; every
assertion lives here, so a failure reads as a sentence about the page.

What it cannot cover: jsdom computes no layout and has no IntersectionObserver, so whether
the sticky bar really shows and hides is browser work. dev/changelog/1152.

Runs against a throwaway temp SQLite DB - never the live dvr.db.
  python3 -m unittest tests.test_account_detail_page_js
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
from tests.support import seed  # noqa: E402
from app import db  # noqa: E402
from app.database import (Account, AccountHost, AccountSyncLog, EpgSource,  # noqa: E402
                          EpgSourceChannel, EpgSourceSubscription)

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HARNESS = os.path.join(REPO, 'tests', 'support', 'account_detail_page.mjs')
JSDOM = os.path.join(REPO, 'node_modules', 'jsdom')

# The page draws this many runs before "All N syncs" (app/routes/accounts.py HISTORY_SHOWN).
HISTORY_SHOWN = 10

_RESULT = None


def _observe():
    """Render the three states once, drive every scenario once in node, return it all."""
    global _RESULT
    if _RESULT is not None:
        return _RESULT
    t = make_test_app()
    tmp = tempfile.mkdtemp(prefix='account_detail_js_')
    try:
        client = t.app.test_client()
        # Fresh has never synced. Busy has more past runs than the page shows, so it has an
        # "All N syncs" control to expand.
        fresh = seed.make_account(name='Fresh', channel_count=0)
        fresh.status = 'UNSYNCED'
        busy = seed.make_account(name='Busy', channel_count=100)
        now = datetime.utcnow()
        busy.last_sync_at = now - timedelta(hours=1)
        for i in range(HISTORY_SHOWN + 1):
            started = now - timedelta(hours=i + 1)
            db.session.add(AccountSyncLog(account_id=busy.id, started_at=started,
                                          completed_at=started + timedelta(seconds=30),
                                          status='SUCCESS', channels_synced=100))
        # A host row, so the page carries a Use switch (dev/changelog/1671).
        host = AccountHost(account_id=busy.id, host='tv1.example.test', is_active=True)
        db.session.add(host)
        # A guide covering one channel, so the Sources card has a scorecard line to load
        # after the page draws (dev/changelog/1717).
        src = seed.make_epg_source(busy)
        seed.make_channel(busy, name='Guided', epg_channel_id='g1.test', epg_source_id=src.id)
        db.session.add(EpgSourceChannel(source_id=src.id, xml_id='g1.test', entry_count=12,
                                        distinct_titles=1, horizon_until=now + timedelta(days=2),
                                        described=6, subtitled=0, categorized=0, rated=0,
                                        flagged=0, doubled=0))
        # Two guides in order, so the Sources card offers Move up on the second, whose
        # preview the page asks for before it moves anything (dev/changelog/1723).
        ordered = seed.make_account(name='Ordered')
        seed.make_epg_source(ordered)
        db.session.flush()
        second = EpgSource(kind='url', owner_account_id=ordered.id, name='Second',
                           url='http://second.test/epg.xml')
        db.session.add(second)
        db.session.flush()
        db.session.add(EpgSourceSubscription(source_id=second.id, account_id=ordered.id,
                                             priority=2))
        db.session.commit()
        ids = {'fresh': fresh.id, 'busy': busy.id}
        with open(os.path.join(tmp, 'meta.json'), 'w', encoding='utf-8') as f:
            json.dump({**ids, 'host': host.id, 'ordered': ordered.id, 'second': second.id}, f)

        def snapshot(state):
            urls = {'fresh.html': f'/accounts/{ids["fresh"]}',
                    'busy.html': f'/accounts/{ids["busy"]}',
                    'nav.json': '/api/nav-status',
                    'syncs.json': f'/api/accounts/{ids["busy"]}/syncs',
                    'scores.json': f'/api/accounts/{ids["busy"]}/epg-scorecard?view=card',
                    'ordered.html': f'/accounts/{ordered.id}',
                    'preview.json': (f'/api/accounts/{ordered.id}/epg-sources/{second.id}/'
                                     'move-preview?direction=up')}
            for suffix, url in urls.items():
                resp = client.get(url)
                if resp.status_code != 200:
                    raise AssertionError(f'{url} answered {resp.status_code} at state {state}')
                with open(os.path.join(tmp, f'{state}.{suffix}'), 'w', encoding='utf-8') as f:
                    f.write(resp.get_data(as_text=True))

        snapshot('never')

        # Opened the way app/accounts.py opens a real run: status SYNCING, log IN_PROGRESS.
        running = {}
        for key in ids:
            db.session.get(Account, ids[key]).status = 'SYNCING'
            log = AccountSyncLog(account_id=ids[key], started_at=datetime.utcnow(),
                                 status='IN_PROGRESS')
            db.session.add(log)
            db.session.flush()
            running[key] = log.id
        db.session.commit()
        snapshot('syncing')

        for key in ids:
            acc = db.session.get(Account, ids[key])
            acc.status = 'OK'
            acc.channel_count = 120
            acc.last_sync_at = datetime.utcnow()
            log = db.session.get(AccountSyncLog, running[key])
            log.status = 'SUCCESS'
            # Under a second, as a 48-channel first sync really runs (dev/changelog/1291).
            log.completed_at = log.started_at + timedelta(milliseconds=400)
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
class _Base:
    SCENARIO = ''

    @classmethod
    def setUpClass(cls):
        cls.obs = _observe()[cls.SCENARIO]
        if isinstance(cls.obs, dict) and 'error' in cls.obs:
            raise AssertionError(f'{cls.SCENARIO} threw in the page:\n{cls.obs["error"]}')

    def test_the_page_ran_without_errors(self):
        self.assertEqual(self.obs['errors'], [])


class PageAsItOpensTests(_Base, unittest.TestCase):
    SCENARIO = 'boot'

    def test_the_page_registers_its_hook_on_the_nav_status_poll(self):
        self.assertEqual(self.obs['hook'], 'function')

    def test_the_page_was_rendered_at_the_signature_the_poll_reports(self):
        self.assertTrue(self.obs['navSig'], 'nav-status must carry account_sync')
        self.assertEqual(self.obs['stateSig'], self.obs['navSig'])

    def test_an_unchanged_signature_fetches_nothing(self):
        """base.html's own first poll has run by now. A page that re-rendered on every poll
        would cost a page render per open tab every few seconds for nothing."""
        self.assertEqual(self.obs['pageFetches'], 0)

    def test_a_running_sync_reads_as_running_in_the_history(self):
        """dev/docs/BUGS.md 2026-09-27 @ 04:50:19 PM: a running sync's row says
        IN_PROGRESS, which the history's badge map did not name - it drew the raw word and
        warned in the console."""
        self.assertEqual(self.obs['histBadges'][0], 'Running')
        self.assertFalse([w for w in self.obs['warnings'] if 'sync log status' in w],
                         self.obs['warnings'])


class SyncFinishesTests(_Base, unittest.TestCase):
    """The reported defect itself: "Syncing now" and Cancel sync outlived the sync."""
    SCENARIO = 'finished'

    def test_the_status_bar_follows_the_sync(self):
        self.assertIn('Syncing now', self.obs['before']['barMsg'])
        self.assertIn('Synced', self.obs['after']['barMsg'])
        self.assertIn('120 channels', self.obs['after']['barMsg'])
        self.assertEqual(self.obs['pageFetches'], 1)

    def test_both_bars_offer_sync_again_rather_than_cancel(self):
        """The inline and sticky bars render the one primary from one macro, so both are
        in the swapped set or they disagree."""
        for bar in ('barActs', 'stickyActs'):
            with self.subTest(bar=bar):
                self.assertIn('cancel-sync', self.obs['before'][bar])
                self.assertIn('sync', self.obs['after'][bar])
                self.assertNotIn('cancel-sync', self.obs['after'][bar])

    def test_the_header_badge_and_the_state_follow_too(self):
        self.assertEqual(self.obs['before']['headBadge'], 'Syncing')
        self.assertEqual(self.obs['after']['headBadge'], 'OK')
        self.assertEqual(self.obs['after']['stateStatus'], 'OK')

    def test_the_phone_sheet_reads_the_refreshed_status(self):
        """Force EPG resync is disabled while syncing. Read from the load-time status, it
        would stay disabled after the sync ended."""
        self.assertIs(self.obs['sheetForceEpgDisabled'], False)

    def test_the_history_is_redrawn_by_its_own_renderer_not_swapped(self):
        self.assertTrue(self.obs['histBoxKept'], 'the history region must never be swapped')
        self.assertEqual(self.obs['before']['histBadges'][0], 'Running')
        self.assertEqual(self.obs['after']['histBadges'][0], 'Success')
        self.assertEqual(self.obs['after']['histRows'], 10)

    def test_the_sticky_bar_keeps_its_node_and_the_observer_follows_the_new_inline_bar(self):
        """The sticky bar carries whether it is showing, so only its contents are swapped.
        The inline bar IS replaced, and an observer left on the detached one would pin the
        sticky bar on for good."""
        self.assertTrue(self.obs['sameStickyBar'])
        self.assertTrue(self.obs['barReplaced'])
        self.assertEqual(self.obs['observed'], ['current'])

    def test_the_page_takes_the_new_signature_and_then_settles(self):
        self.assertEqual(self.obs['after']['stateSig'], self.obs['navSig'])
        self.assertEqual(self.obs['pageFetchesAfterNextPoll'], 1)


class FirstSyncTests(_Base, unittest.TestCase):
    """A page opened on a never-synced account: its history card holds the "No syncs yet"
    state, which is server-rendered, until the first run exists."""
    SCENARIO = 'first_sync'

    def test_the_empty_history_gives_way_to_the_first_run(self):
        self.assertTrue(self.obs['before']['histEmpty'])
        self.assertFalse(self.obs['during']['histEmpty'])
        self.assertEqual(self.obs['during']['histBadges'], ['Running'])
        self.assertEqual(self.obs['after']['histBadges'], ['Success'])

    def test_a_sub_second_run_says_so_rather_than_0m(self):
        """A first-time user read "Duration 0m" for a sync it had just watched finish
        (dev/docs/BUGS.md 2026-10-26 08:57:27 AM)."""
        self.assertEqual(self.obs['after']['histDurs'], ['under 1s'])

    def test_the_first_sync_button_becomes_cancel_then_sync(self):
        self.assertIn('sync', self.obs['before']['barActs'])
        self.assertIn('cancel-sync', self.obs['during']['barActs'])
        self.assertIn('sync', self.obs['after']['barActs'])

    def test_activity_follows(self):
        self.assertIn('Nothing has happened yet', self.obs['before']['activity'])
        self.assertIn('Sync started.', self.obs['during']['activity'])
        self.assertIn('Sync finished', self.obs['after']['activity'])


class OpenMenuTests(_Base, unittest.TestCase):
    SCENARIO = 'menu_open'

    def test_an_open_menu_holds_the_refresh_back(self):
        """Replacing the action bar under its open kebab would close it mid-choice."""
        self.assertTrue(self.obs['menuOpen'])
        self.assertEqual(self.obs['whileOpen']['pageFetches'], 0)
        self.assertIn('Syncing now', self.obs['whileOpen']['barMsg'])

    def test_the_refresh_lands_once_the_menu_closes(self):
        self.assertEqual(self.obs['afterClose']['pageFetches'], 1)
        self.assertIn('Synced', self.obs['afterClose']['barMsg'])


class AgedRenderTests(_Base, unittest.TestCase):
    """"Synced 4m ago" and "Next sync in 23h 44m" drift while no sync changes anything."""
    SCENARIO = 'aged'

    def test_a_fresh_render_is_not_refetched(self):
        self.assertEqual(self.obs['fresh'], 0)

    def test_a_minute_old_render_refreshes_only_in_a_visible_tab(self):
        self.assertEqual(self.obs['hiddenTab'], 0)
        self.assertEqual(self.obs['visibleTab'], 1)

    def test_the_refresh_restarts_the_minute(self):
        self.assertEqual(self.obs['rightAfter'], 1)

    def test_unchanged_runs_are_not_redrawn(self):
        self.assertTrue(self.obs['sameHistRow'])


class FailedRefreshTests(_Base, unittest.TestCase):
    SCENARIO = 'failed_fetch'

    def test_a_failed_refresh_leaves_the_page_as_it_was_and_says_so(self):
        failed = self.obs['failed']
        self.assertEqual(failed['pageFetches'], 1)
        self.assertIn('Syncing now', failed['barMsg'])
        self.assertEqual(failed['stateSig'], self.obs['syncingSig'],
                         'a 500 page must not be read as if it were the account page')
        self.assertTrue(any('refresh failed' in w for w in failed['warnings']), failed['warnings'])

    def test_the_next_poll_retries(self):
        self.assertEqual(self.obs['retried']['pageFetches'], 2)
        self.assertIn('Synced', self.obs['retried']['barMsg'])


class ExpandedHistoryTests(_Base, unittest.TestCase):
    """"All N syncs" is the user's choice and outlives a refresh."""
    SCENARIO = 'expanded'

    def test_the_expansion_survives_the_refresh(self):
        self.assertEqual(self.obs['collapsed']['histRows'], HISTORY_SHOWN)
        self.assertEqual(self.obs['opened']['histRows'], HISTORY_SHOWN + 2)
        self.assertEqual(self.obs['opened']['moreBtn'], 'Show fewer')
        refreshed = self.obs['refreshed']
        self.assertEqual(refreshed['moreBtn'], 'Show fewer',
                         'the swapped-in button must not read "All N syncs" while expanded')
        self.assertEqual(refreshed['histRows'], HISTORY_SHOWN + 2)

    def test_an_expanded_history_is_refetched_in_full(self):
        """The first page in the state blob is not the whole list; drawing it would
        silently collapse the section."""
        self.assertEqual(self.obs['opened']['syncsFetches'], 1)
        self.assertEqual(self.obs['refreshed']['syncsFetches'], 2)
        self.assertEqual(self.obs['refreshed']['histBadges'][0], 'Success')

    def test_the_swapped_button_still_collapses(self):
        self.assertEqual(self.obs['closed']['histRows'], HISTORY_SHOWN)
        self.assertIn('All 12 syncs', self.obs['closed']['moreBtn'])


class ActionRefreshTests(_Base, unittest.TestCase):
    SCENARIO = 'action'

    def test_cancel_sync_refreshes_in_place_rather_than_reloading(self):
        self.assertEqual(len([p for p in self.obs['posts'] if p.endswith('/sync/cancel')]), 1)
        self.assertEqual(self.obs['navigations'], 0, 'a reload is reported as a navigation')
        self.assertEqual(self.obs['pageFetches'], 1)
        self.assertIn('Synced', self.obs['barMsg'])



class UseSwitchTests(_Base, unittest.TestCase):
    """dev/changelog/1671: a host's Use switch posts a real boolean to its own route, then
    refreshes the page in place; a refused flip puts the switch back."""
    SCENARIO = 'switch'

    def test_a_flip_posts_the_value_and_refreshes_in_place(self):
        posts = self.obs['after']['posts']
        self.assertEqual(len(posts), 1)
        self.assertTrue(posts[0]['path'].endswith(f'/hosts/{self.obs["hostId"]}/enabled'),
                        posts[0]['path'])
        self.assertEqual(json.loads(posts[0]['body']), {'enabled': False})
        self.assertEqual(self.obs['after']['pageFetches'], 1)

    def test_a_refused_flip_goes_back_and_refetches_nothing(self):
        self.assertTrue(self.obs['refusedChecked'])
        self.assertFalse(self.obs['refusedDisabled'])
        self.assertEqual(self.obs['refusedPageFetches'], 1)


class ScorecardLineTests(_Base, unittest.TestCase):
    """dev/changelog/1717: each source's scorecard line costs 0.6 s on prod's largest
    accounts, so the page draws without it and fills it in from the API's card view."""
    SCENARIO = 'scores'

    def test_the_page_draws_with_the_line_hidden_and_empty(self):
        lines = self.obs['atFirstPaint']['scoreLines']
        self.assertEqual(len(lines), 1)
        self.assertEqual((lines[0]['hidden'], lines[0]['text']), (True, ''))
        self.assertNotIn('by the last day it runs through', self.obs['atFirstPaint']['reachTip'])

    def test_the_line_and_the_days_arrive_after_it(self):
        line = self.obs['loaded']['scoreLines'][0]
        self.assertEqual(self.obs['loaded']['scoreFetches'], 1)
        self.assertFalse(line['hidden'])
        self.assertEqual(line['text'], 'Covers 1 channel here, 1 only here. 1 lists one program all day.')
        self.assertIn('50% described', line['tip'])
        tip = self.obs['loaded']['reachTip']
        self.assertTrue(tip.startswith('Guide reaches.'), tip)
        self.assertIn('1 channel with a guide here, by the last day it runs through:', tip)

    def test_a_swap_puts_the_line_back_without_asking_again(self):
        aged = self.obs['aged']
        self.assertEqual(aged['pageFetches'], 1, 'the minute-old render was swapped')
        self.assertEqual(aged['scoreFetches'], 1)
        self.assertEqual(aged['scoreLines'], self.obs['loaded']['scoreLines'])
        self.assertEqual(aged['reachTip'], self.obs['loaded']['reachTip'],
                         'the days are added once, never twice')

    def test_a_sync_reads_it_again(self):
        self.assertEqual(self.obs['synced']['scoreFetches'], 2)
        self.assertFalse(self.obs['synced']['scoreLines'][0]['hidden'])


class ScorecardFailedTests(_Base, unittest.TestCase):
    SCENARIO = 'scores_failed'

    def test_a_failed_read_leaves_the_page_as_it_was_drawn(self):
        self.assertEqual([(x['hidden'], x['text']) for x in self.obs['scoreLines']],
                         [(True, '')])
        self.assertTrue([w for w in self.obs['warnings'] if 'scorecard' in w])



class MovePreviewTests(_Base, unittest.TestCase):
    """dev/changelog/1723: Move up asks the server what the move would change, shows its
    sentences, and moves only when the user agrees - with `confirm` when the dialog also said
    the account would get its own order, so the server does not ask a second time."""
    SCENARIO = 'move_preview'

    def test_the_preview_is_asked_for_before_anything_moves(self):
        asked = self.obs['asked']
        self.assertEqual(len(asked['previews']), 1, asked['previews'])
        self.assertTrue(asked['previews'][0].endswith(
            f'/epg-sources/{self.obs["second"]}/move-preview?direction=up'), asked['previews'])
        self.assertEqual(asked['posts'], 0)

    def test_the_dialog_shows_the_servers_sentences(self):
        self.assertEqual(self.obs['asked']['items'], self.obs['served'])
        self.assertEqual(self.obs['asked']['title'], 'Move Second up')

    def test_cancel_moves_nothing(self):
        self.assertEqual(self.obs['cancelled'], {'posts': 0, 'open': False})

    def test_an_own_order_move_says_so_and_confirms_once(self):
        self.assertTrue(any('its own EPG order' in p for p in self.obs['ownParas']),
                        self.obs['ownParas'])
        self.assertEqual(len(self.obs['posts']), 1)
        post = self.obs['posts'][0]
        self.assertTrue(post['path'].endswith(f'/epg-sources/{self.obs["second"]}/move'), post)
        self.assertEqual(post['body'], {'direction': 'up', 'confirm': True})

    def test_a_failed_preview_says_so_and_never_blocks_the_move(self):
        self.assertEqual(len(self.obs['brokenItems']), 1)
        self.assertTrue(self.obs['brokenItems'][0].startswith('Could not work out what this '
                                                             'changes'), self.obs['brokenItems'])
        self.assertEqual(len(self.obs['brokenPosts']), 1)
        self.assertEqual(self.obs['brokenPosts'][0]['body'], {'direction': 'up'},
                         'no confirm: the server still asks about an own order it was not shown')


if __name__ == '__main__':
    unittest.main(verbosity=2)
