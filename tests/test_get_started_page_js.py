"""Tier 0 - the Get started page moving on by itself, in a real DOM.

The first-sync step says "This page moves on by itself when it finishes". That promise is
static/js/get-started.js swapping in a fresh server render when base.html's
/api/nav-status poll reports a sync signature the page was not rendered at
(dev/changelog/1280). From Python a page whose hooks never registered renders exactly the
same markup, so the promise is checked here. Invariants:

  (a) A poll that agrees with what the page was rendered at fetches nothing.
  (b) A first sync starting re-renders the steps once, and the page then converges.
  (c) A first sync finishing moves the account step to done and leads with the guide,
      the header count and the menu link following, with no reload.

tests/support/get_started_page.mjs replays each scenario against the markup the route
really rendered; every assertion lives here.

  python3 -m unittest tests.test_get_started_page_js
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

from app import db, readiness, setup_steps  # noqa: E402
from app.database import (  # noqa: E402
    Account, AccountSyncLog, ACCOUNT_STATUS_OK, ACCOUNT_STATUS_SYNCING, ACCOUNT_STATUS_UNSYNCED,
)
from tests.support import seed  # noqa: E402
from tests.support.app import make_test_app  # noqa: E402
from tests.support.readiness import patched, result  # noqa: E402

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HARNESS = os.path.join(REPO, 'tests', 'support', 'get_started_page.mjs')
JSDOM = os.path.join(REPO, 'node_modules', 'jsdom')
MACHINE_READY = {cid: result(readiness.READY, f'{cid} fine') for cid in setup_steps.MACHINE_CHECKS}

_RESULT = None


def _observe():
    global _RESULT
    if _RESULT is not None:
        return _RESULT
    t = make_test_app()
    tmp = tempfile.mkdtemp(prefix='get_started_js_')
    try:
        client = t.app.test_client()

        def snapshot(state):
            with patched(**MACHINE_READY):
                for suffix, url in (('html', '/get-started'), ('json', '/api/nav-status')):
                    resp = client.get(url)
                    if resp.status_code != 200:
                        raise AssertionError(f'{url} answered {resp.status_code} at {state}')
                    with open(os.path.join(tmp, f'{state}.{suffix}'), 'w', encoding='utf-8') as f:
                        f.write(resp.get_data(as_text=True))

        acc = seed.make_account(name='Northwind TV')
        acc.status = ACCOUNT_STATUS_UNSYNCED
        acc.next_sync_at = datetime.utcnow() + timedelta(minutes=4)
        db.session.commit()
        acc_id = acc.id
        snapshot('pending')

        acc = db.session.get(Account, acc_id)
        acc.status = ACCOUNT_STATUS_SYNCING
        log = AccountSyncLog(account_id=acc_id, started_at=datetime.utcnow(),
                             status='IN_PROGRESS')
        db.session.add(log)
        db.session.commit()
        log_id = log.id
        snapshot('syncing')

        acc = db.session.get(Account, acc_id)
        acc.status = ACCOUNT_STATUS_OK
        acc.channel_count = 48
        acc.last_sync_at = datetime.utcnow()
        log = db.session.get(AccountSyncLog, log_id)
        log.status = 'SUCCESS'
        log.completed_at = datetime.utcnow()
        db.session.commit()
        snapshot('synced')

        proc = subprocess.run([shutil.which('node'), HARNESS, tmp, REPO],
                              capture_output=True, text=True, timeout=120, cwd=REPO)
        if proc.returncode != 0:
            raise AssertionError(f'harness failed:\n{proc.stdout[-2000:]}\n{proc.stderr[-4000:]}')
        _RESULT = json.loads(proc.stdout)
        return _RESULT
    finally:
        t.cleanup()
        shutil.rmtree(tmp, ignore_errors=True)


@unittest.skipUnless(shutil.which('node') and os.path.isdir(JSDOM), 'needs node + jsdom')
class GetStartedLiveTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.r = _observe()

    def test_an_agreeing_poll_fetches_nothing(self):
        view = self.r['unchanged']
        self.assertEqual(view['errors'], [])
        self.assertEqual(view['pageFetches'], 0)
        self.assertIn('account:wait', view['steps'])

    def test_a_first_sync_starting_re_renders_once_then_converges(self):
        self.assertEqual(self.r['started']['pageFetches'], 1)
        self.assertEqual(self.r['startedAgain']['pageFetches'], 1)
        self.assertIn('account:wait', self.r['started']['steps'])

    def test_a_first_sync_finishing_moves_the_page_on_without_a_reload(self):
        view = self.r['finished']
        self.assertEqual(view['errors'], [])
        self.assertEqual(view['pageFetches'], 2)
        self.assertEqual(view['steps'][:4],
                         ['account:done', 'machine:done', 'guide:now', 'record:later'])
        self.assertEqual(view['sub'], '2 of 4 steps done')
        self.assertTrue(view['navLinks'])
        self.assertTrue(all(link == {'shown': True, 'count': '2 left'}
                            for link in view['navLinks']), view['navLinks'])


if __name__ == '__main__':
    unittest.main()
