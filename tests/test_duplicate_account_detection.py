"""Tier 2 - duplicate account detection (dev/changelog/318, "Detect/prevent duplicate
account creation").

Nothing previously stopped the same IPTV subscription from being added twice - the same
M3U URL added again, or the same Xtream server+username added again. Decided
2026-07-25: same-type-only detection (an m3u account and an xtream account are never
cross-checked), and advisory only - the save still goes through, with a warning flash
naming the conflicting account.
"""
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tests.support import make_test_app  # noqa: E402
from app import db  # noqa: E402
from app.database import Account  # noqa: E402


def _make_account(name, account_type='m3u', **kw):
    kw.setdefault('base_url', '')
    kw.setdefault('username', '')
    kw.setdefault('password', '')
    acc = Account(name=name, account_type=account_type, status='OK', **kw)
    db.session.add(acc)
    db.session.flush()
    return acc


class DuplicateAccountDetectionTests(unittest.TestCase):
    def setUp(self):
        self.t = make_test_app()
        self.t.app.config['WTF_CSRF_ENABLED'] = False
        # Not what's under test here, and make_test_app() doesn't start a real
        # APScheduler (start_scheduler=False), so the route's post-commit
        # schedule_account_sync() call would hit a None scheduler.
        self._sched_patch = mock.patch('app.scheduler.schedule_account_sync')
        self._sched_patch.start()

    def tearDown(self):
        self._sched_patch.stop()
        self.t.cleanup()

    def test_duplicate_m3u_url_warns_but_still_creates(self):
        _make_account('Existing M3U', m3u_url='http://example.test/list.m3u')
        db.session.commit()

        resp = self.t.client.post('/accounts/new', data={
            'name': 'New M3U',
            'account_type': 'm3u',
            'm3u_url': 'http://example.test/list.m3u',
        }, follow_redirects=True)

        self.assertEqual(resp.status_code, 200)
        self.assertIn(b'Existing M3U', resp.data)
        self.assertIn(b'already uses this M3U URL', resp.data)
        self.assertEqual(Account.query.filter_by(name='New M3U').count(), 1)

    def test_duplicate_xtream_server_and_username_warns_but_still_creates(self):
        _make_account('Existing Xtream', account_type='xtream',
                      base_url='http://panel.example.test',
                      username='joe', password='s3cret')
        db.session.commit()

        resp = self.t.client.post('/accounts/new', data={
            'name': 'New Xtream',
            'account_type': 'xtream',
            'base_url': 'http://panel.example.test',
            'username': 'joe',
            'password': 'different-password',
        }, follow_redirects=True)

        self.assertEqual(resp.status_code, 200)
        self.assertIn(b'Existing Xtream', resp.data)
        self.assertIn(b'already uses this server and username', resp.data)
        self.assertEqual(Account.query.filter_by(name='New Xtream').count(), 1)

    def test_different_type_same_subscription_is_not_flagged(self):
        # Same-type-only by decision - an m3u account whose URL embeds the same
        # username/password as an xtream account is NOT cross-checked.
        _make_account('Existing M3U',
                      m3u_url='http://panel.example.test/get.php?username=joe&password=s3cret&type=m3u')
        db.session.commit()

        resp = self.t.client.post('/accounts/new', data={
            'name': 'New Xtream',
            'account_type': 'xtream',
            'base_url': 'http://panel.example.test',
            'username': 'joe',
            'password': 's3cret',
        }, follow_redirects=True)

        self.assertEqual(resp.status_code, 200)
        self.assertNotIn(b'already uses this', resp.data)
        self.assertEqual(Account.query.filter_by(name='New Xtream').count(), 1)

    def test_no_warning_when_urls_differ(self):
        _make_account('Existing M3U', m3u_url='http://example.test/list-a.m3u')
        db.session.commit()

        resp = self.t.client.post('/accounts/new', data={
            'name': 'New M3U',
            'account_type': 'm3u',
            'm3u_url': 'http://example.test/list-b.m3u',
        }, follow_redirects=True)

        self.assertEqual(resp.status_code, 200)
        self.assertNotIn(b'already uses this', resp.data)

    def test_edit_into_a_duplicate_warns_and_excludes_self(self):
        """Editing is a modal over the JSON API since dev/changelog/456, so the warning is
        an advisory field on a 200 response rather than a flash on a redirect. What it must
        still be is ADVISORY - it reports, it never blocks the save."""
        _make_account('Account A', m3u_url='http://example.test/list-a.m3u')
        acct_b = _make_account('Account B', m3u_url='http://example.test/list-b.m3u')
        db.session.commit()

        # Editing B to keep its own URL must not flag itself as a duplicate of itself.
        resp = self.t.client.post(f'/api/accounts/{acct_b.id}', json={
            'name': 'Account B',
            'account_type': 'm3u',
            'm3u_url': 'http://example.test/list-b.m3u',
        })
        self.assertEqual(resp.status_code, 200)
        self.assertIsNone(resp.get_json()['warning'])

        # Editing B's URL to match A's now correctly flags the conflict - and still saves.
        resp = self.t.client.post(f'/api/accounts/{acct_b.id}', json={
            'name': 'Account B',
            'account_type': 'm3u',
            'm3u_url': 'http://example.test/list-a.m3u',
        })
        self.assertEqual(resp.status_code, 200)
        warning = resp.get_json()['warning']
        self.assertIn('Account A', warning)
        self.assertIn('already uses this M3U URL', warning)
        db.session.expire_all()
        self.assertEqual(db.session.get(Account, acct_b.id).m3u_url,
                         'http://example.test/list-a.m3u',
                         'the duplicate warning is advisory - the save still happened')


if __name__ == '__main__':
    unittest.main()
