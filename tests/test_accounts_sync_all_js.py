"""Tier 0 - the Accounts list's Sync All button, driven in a real DOM.

dev/changelog/1443. The button is the sync queue's second way in, after the Readiness check's
"Sync N accounts now", and shares that button's confirm (static/js/sync-queue.js). Invariants:

  (a) The click starts nothing: it lists every account, in the list's order, and says they run
      one at a time with the pause between them. One account still asks.
  (b) Confirming is ONE request, carrying every account. No per-account sync is posted.
  (c) Cancel starts nothing.
  (d) Something holding the queue back is named, with the choice to wait or sync anyway, and only
      "Sync anyway" sends the override.
  (e) While a queue runs the label says how far it has got, the click shows where it stands and
      offers to stop it, and starts nothing.
  (f) A queue that ends on its own puts the label back and says so once. A stopped one says only
      that it stopped.
  (g) No accounts, no button; a first account appearing brings it without a reload.

tests/support/accounts_sync_all.mjs drives the shipped scripts against the markup /accounts
rendered and the status read the app answered at each state; every assertion is here.

  python3 -m unittest tests.test_accounts_sync_all_js
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from datetime import timedelta
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tests.support.app import make_test_app  # noqa: E402
from tests.support import seed  # noqa: E402
from app import db  # noqa: E402
from app.database import REC_STATUS_IN_PROGRESS  # noqa: E402

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HARNESS = os.path.join(REPO, 'tests', 'support', 'accounts_sync_all.mjs')
JSDOM = os.path.join(REPO, 'node_modules', 'jsdom')

_RESULT = None
_IDS = []


def _running(ids):
    return {'current': ids[1], 'pending': [], 'total': 2, 'done': 1, 'waiting': None}


def _observe():
    global _RESULT
    if _RESULT is not None:
        return _RESULT
    t = make_test_app()
    tmp = tempfile.mkdtemp(prefix='accounts_sync_all_js_')
    try:
        def snapshot(state):
            for suffix, url in (('html', '/accounts'), ('json', '/api/accounts/sync-queue')):
                resp = t.client.get(url)
                if resp.status_code != 200:
                    raise AssertionError(f'{url} answered {resp.status_code} at state {state}')
                with open(os.path.join(tmp, f'{state}.{suffix}'), 'w', encoding='utf-8') as f:
                    f.write(resp.get_data(as_text=True))

        snapshot('empty')
        first = seed.make_account(name='Zulu Feed', sync_enabled=True)
        db.session.commit()
        snapshot('one')
        # Added second and named to sort first, so the list's order and an alphabetical one
        # cannot be mistaken for each other.
        second = seed.make_account(name='Alpha Feed', sync_enabled=True)
        second.created_at = first.created_at + timedelta(minutes=1)
        db.session.commit()
        _IDS[:] = [first.id, second.id]
        snapshot('two')
        with mock.patch('app.sync_queue.status', return_value=_running(_IDS)):
            snapshot('running')
        seed.make_recording(name='Big Game', status=REC_STATUS_IN_PROGRESS)
        db.session.commit()
        snapshot('held')
        proc = subprocess.run(['node', HARNESS, tmp, REPO],
                              capture_output=True, text=True, timeout=120, cwd=REPO)
        if proc.returncode != 0:
            raise AssertionError(f'harness failed:\n{proc.stderr}')
        _RESULT = json.loads(proc.stdout)
        return _RESULT
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
        t.cleanup()


@unittest.skipUnless(os.path.isdir(JSDOM), 'jsdom is not installed')
class SyncAllTests(unittest.TestCase):

    def _clean(self, name):
        result = _observe()[name]
        self.assertNotIn('error', result, result.get('error'))
        self.assertEqual(result['errors'], [])
        return result

    def test_the_click_lists_every_account_in_the_list_order_and_starts_nothing(self):
        before = self._clean('confirmed')['before']
        self.assertEqual(before['posts'], 0)
        self.assertEqual(before['buttons'], ['Cancel', 'Sync 2 accounts'])
        self.assertIn('one at a time', before['text'])
        self.assertIn('a pause of 1m', before['text'])
        self.assertLess(before['text'].index('Zulu Feed'), before['text'].index('Alpha Feed'))

    def test_confirming_is_one_request_carrying_every_account(self):
        result = self._clean('confirmed')
        self.assertEqual(len(result['posts']), 1)
        post = result['posts'][0]
        self.assertEqual(post['path'], '/api/accounts/sync-queue')
        self.assertEqual(json.loads(post['body']), {'account_ids': _IDS, 'override': False})
        self.assertEqual(result['toasts'], ['Server message.'])
        self.assertTrue(result['modalGone'])

    def test_a_started_queue_shows_on_the_button_without_a_reload(self):
        self.assertEqual(self._clean('confirmed')['label'], 'Syncing: 1 of 2 done')

    def test_cancel_starts_nothing(self):
        result = self._clean('cancelled')
        self.assertEqual(result['posts'], [])
        self.assertEqual(result['label'], 'Sync All')

    def test_a_single_account_still_asks_first(self):
        result = self._clean('oneAccount')
        self.assertEqual(result['before']['posts'], 0)
        self.assertEqual(result['before']['buttons'], ['Cancel', 'Sync 1 account'])
        self.assertIn('Zulu Feed', result['before']['text'])
        self.assertNotIn('one at a time', result['before']['text'])
        self.assertEqual(json.loads(result['posts'][0]['body'])['account_ids'], _IDS[:1])

    def test_a_recording_is_named_and_waiting_is_the_default(self):
        result = self._clean('heldWait')
        self.assertIn('Big Game', result['before']['text'])
        self.assertEqual(result['before']['buttons'],
                         ['Cancel', 'Wait, then sync', 'Sync anyway'])
        self.assertEqual(json.loads(result['posts'][0]['body']),
                         {'account_ids': _IDS, 'override': False})

    def test_only_sync_anyway_sends_the_override(self):
        result = self._clean('heldAnyway')
        self.assertEqual(json.loads(result['posts'][0]['body']),
                         {'account_ids': _IDS, 'override': True})

    def test_a_running_queue_shows_where_it_stands_and_starts_nothing(self):
        result = self._clean('runningClose')
        self.assertEqual(result['before']['label'], 'Syncing: 1 of 2 done')
        self.assertEqual(result['before']['buttons'], ['Close', 'Stop syncing the rest'])
        self.assertIn('1 of 2 done, now Alpha Feed', result['before']['text'])
        self.assertEqual(result['posts'], [])

    def test_stopping_says_it_stopped_and_not_that_it_finished(self):
        result = self._clean('runningStop')
        self.assertEqual([p['path'] for p in result['posts']], ['/api/accounts/sync-queue/stop'])
        self.assertEqual(result['label'], 'Sync All')
        self.assertEqual(result['toasts'], ['Server message.'])

    def test_a_queue_ending_on_its_own_says_so_once(self):
        result = self._clean('endedOnItsOwn')
        self.assertEqual(result['first']['label'], 'Sync All')
        self.assertEqual(len(result['first']['toasts']), 1)
        self.assertIn('have finished', result['first']['toasts'][0])
        self.assertEqual(result['pageReads'], 2)
        self.assertEqual(len(result['toasts']), 1)

    def test_no_accounts_no_button_and_a_first_account_brings_it(self):
        result = self._clean('firstAccountAppears')
        self.assertIsNone(result['before'])
        self.assertEqual(result['label'], 'Sync All')


class SyncAllMarkupTests(unittest.TestCase):

    def setUp(self):
        self.t = make_test_app()
        self.addCleanup(self.t.cleanup)

    def test_it_sits_in_the_page_head_beside_add_account_and_loads_its_script(self):
        seed.make_account(name='Feed')
        db.session.commit()
        html = self.t.client.get('/accounts').get_data(as_text=True)
        head = html[html.index('class="page-head"'):html.index('class="page-about"')]
        self.assertLess(head.index('Sync All'), head.index('+ Add Account'))
        self.assertIn('data-url="/api/accounts/sync-queue"', head)
        self.assertLess(html.index('js/sync-queue.js'), html.index('js/accounts.js'))

    def test_the_readiness_page_loads_the_shared_confirm_before_it_needs_it(self):
        html = self.t.client.get('/readiness').get_data(as_text=True)
        self.assertLess(html.index('js/sync-queue.js'), html.index('js/readiness.js'))


if __name__ == '__main__':
    unittest.main()
