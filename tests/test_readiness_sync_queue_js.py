"""Tier 0 - the Readiness check's "Sync N accounts now" button, driven in a real DOM.

dev/docs/BUGS.md 2026-11-05 @ 06:50:39 AM: the button posted once per account, so the first
account synced, the second answered an error, and the rest waited for their schedule.
Invariants:

  (a) The click says what will happen before anything starts: the accounts, one at a time, the
      pause between them.
  (b) Confirming is ONE request, carrying every account. No per-account sync is posted.
  (c) Cancel starts nothing.
  (d) Something holding the queue back is named, with the choice to wait or sync anyway, and
      only "Sync anyway" sends the override.
  (e) While a queue runs the page re-reads itself, and says when it ends.

tests/support/readiness_sync_queue.mjs drives the shipped scripts against the
markup /readiness rendered and the payload readiness.evaluate() built; every assertion is here.

  python3 -m unittest tests.test_readiness_sync_queue_js
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
from app import db, readiness  # noqa: E402

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HARNESS = os.path.join(REPO, 'tests', 'support', 'readiness_sync_queue.mjs')
JSDOM = os.path.join(REPO, 'node_modules', 'jsdom')

_RESULT = None


def _observe():
    global _RESULT
    if _RESULT is not None:
        return _RESULT
    t = make_test_app()
    tmp = tempfile.mkdtemp(prefix='readiness_sync_queue_js_')
    try:
        long_ago = datetime.utcnow() - timedelta(days=3)
        ids = [seed.make_account(name=name, last_sync_at=long_ago, sync_enabled=True).id
               for name in ('Alpha Feed', 'Beta Feed')]
        db.session.commit()
        page = t.client.get('/readiness').get_data(as_text=True)
        with open(os.path.join(tmp, 'page.html'), 'w', encoding='utf-8') as f:
            f.write(page)
        with open(os.path.join(tmp, 'payload.json'), 'w', encoding='utf-8') as f:
            json.dump(readiness.evaluate(), f, default=str)
        proc = subprocess.run(['node', HARNESS, tmp, REPO],
                              capture_output=True, text=True, timeout=120, cwd=REPO)
        if proc.returncode != 0:
            raise AssertionError(f'harness failed:\n{proc.stderr[-4000:]}')
        _RESULT = {'obs': json.loads(proc.stdout), 'ids': ids}
        return _RESULT
    finally:
        t.cleanup()
        shutil.rmtree(tmp, ignore_errors=True)


@unittest.skipIf(shutil.which('node') is None, 'node not installed')
@unittest.skipIf(not os.path.isdir(JSDOM), 'jsdom not installed (npm install)')
class SyncQueueButtonTests(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        result = _observe()
        cls.obs = result['obs']
        cls.ids = result['ids']
        for name, seen in cls.obs.items():
            if 'error' in seen:
                raise AssertionError(f'{name} threw in the page:\n{seen["error"]}')

    def test_every_scenario_ran_clean(self):
        for name, seen in self.obs.items():
            self.assertEqual(seen['errors'], [], name)

    def test_the_click_says_what_will_happen_and_starts_nothing_yet(self):
        before = self.obs['clear']['before']
        self.assertIn('one at a time', before['text'])
        self.assertIn('pause of 1m', before['text'])
        self.assertIn('Alpha Feed', before['text'])
        self.assertIn('Beta Feed', before['text'])
        self.assertIn('leave this page', before['text'])
        self.assertEqual(before['posts'], 0)

    def test_the_dialog_with_nothing_in_the_way_is_cancel_or_go(self):
        self.assertEqual(self.obs['clear']['before']['buttons'][-2:], ['Cancel', 'Sync 2 accounts'])

    def test_confirming_is_one_request_carrying_every_account(self):
        posts = self.obs['clear']['posts']
        self.assertEqual(len(posts), 1, posts)
        self.assertTrue(posts[0]['href'].endswith('/api/accounts/sync-queue'), posts)
        body = json.loads(posts[0]['body'])
        self.assertEqual(set(body['account_ids']), set(self.ids))
        self.assertFalse(body['override'])

    def test_the_server_sentence_is_the_toast(self):
        self.assertIn('Syncing 2 accounts, one at a time.', self.obs['clear']['toasts'])

    def test_cancel_starts_nothing(self):
        self.assertEqual(self.obs['cancelled']['posts'], [])

    def test_something_in_the_way_is_named_with_both_choices(self):
        before = self.obs['heldWait']['before']
        self.assertIn('Big Game', before['text'])
        self.assertEqual(before['buttons'][-3:], ['Cancel', 'Wait, then sync', 'Sync anyway'])

    def test_waiting_sends_no_override(self):
        self.assertFalse(json.loads(self.obs['heldWait']['posts'][0]['body'])['override'])

    def test_sync_anyway_sends_the_override(self):
        self.assertTrue(json.loads(self.obs['heldAnyway']['posts'][0]['body'])['override'])

    def test_a_running_queue_is_followed_and_its_end_is_announced(self):
        seen = self.obs['following']
        self.assertEqual(seen['reads'], 4)
        self.assertEqual(seen['readsLater'], 4)
        self.assertTrue(any('finished' in t for t in seen['toasts']), seen['toasts'])

    def test_no_queue_means_no_polling_and_no_toast(self):
        seen = self.obs['idle']
        self.assertEqual(seen['reads'], 1)
        self.assertEqual(seen['toasts'], [])


if __name__ == '__main__':
    unittest.main()
