"""Tier 1 call-site guards: an account's own m3u_url/epg_url never leaves in full.

Guards BUGS.md 2026-07-21 (path-token account URLs leaked whole to dvr.log,
account.last_error / AccountSyncLog.error_message and outbound push alerts, because
mask_creds' shape heuristics cannot recognize a path that IS the credential -
DESIGN-secrets.md §4.2).

No app build and no DB: both paths under test fail before touching the session, so an
unattached Account carries enough state. requests.get is patched, so nothing reaches the
network (tests/support/netguard.py would refuse it anyway).
"""
import os
import sys
import threading
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import accounts as accounts_mod, db  # noqa: E402
from app.database import Account, AccountSyncLog, EpgSource, M3uAccount  # noqa: E402
from tests.support import make_test_app  # noqa: E402

# The shape that defeats every mask_creds heuristic: no /live/ prefix, no query params,
# no userinfo, and a path too short to be the bare user/pass/id triple.
TOKEN = 'TESTPATHTOKEN1'
M3U_URL = f'https://example-provider.test/{TOKEN}'
EPG_URL = f'https://example-provider.test/{TOKEN}/epg.xml'


def _account():
    return Account(id=7, name='provider', account_type='m3u',
                   m3u_url=M3U_URL, epg_url=EPG_URL)


def _source():
    return EpgSource(id=3, kind='url', name='provider XMLTV', url=EPG_URL, owner_account_id=7)


class M3uFetchMaskingTests(unittest.TestCase):
    def test_non_playlist_error_message_hides_the_path_token(self):
        """The ValueError text is persisted verbatim into account.last_error."""
        resp = unittest.mock.Mock()
        resp.content = b'<html>login required</html>'
        resp.raise_for_status.return_value = None
        resp.status_code = 200
        with patch.object(accounts_mod.requests, 'get', return_value=resp):
            with self.assertRaises(ValueError) as ctx:
                accounts_mod._fetch_m3u_streams(_account(), 5, {})
        self.assertNotIn(TOKEN, str(ctx.exception))
        self.assertIn('example-provider.test/***', str(ctx.exception))

    def test_fetch_log_line_hides_the_path_token(self):
        resp = unittest.mock.Mock()
        resp.content = b'<html>login required</html>'
        resp.raise_for_status.return_value = None
        resp.status_code = 200
        with patch.object(accounts_mod.requests, 'get', return_value=resp):
            with self.assertLogs(accounts_mod.log, level='INFO') as logs:
                with self.assertRaises(ValueError):
                    accounts_mod._fetch_m3u_streams(_account(), 5, {})
        joined = '\n'.join(logs.output)
        self.assertNotIn(TOKEN, joined)
        self.assertIn('example-provider.test/***', joined)


class EpgFetchMaskingTests(unittest.TestCase):
    def test_fetch_failure_reason_hides_the_path_token(self):
        """The reason is persisted into AccountSyncLog.error_message and shown in the UI."""
        exc = Exception(f'HTTPConnectionPool: Max retries exceeded with url: {EPG_URL}')
        with patch.object(accounts_mod.requests, 'get', side_effect=exc):
            with self.assertLogs(accounts_mod.log, level='INFO') as logs:
                # No database here: the source is transient, so the refresh-in-flight stamp
                # (dev/changelog/1104) is patched out along with the outcome write.
                with patch.object(accounts_mod, '_report_source_outcome'), \
                     patch.object(accounts_mod, '_set_refreshing'):
                    imported, reason = accounts_mod.refresh_source(_source(), 5, 3, {})
        self.assertEqual(imported, 0)
        self.assertTrue(reason.startswith('fetch failed:'))
        self.assertNotIn(TOKEN, reason)
        self.assertIn('example-provider.test/***', reason)
        # the log line carrying the same exception must not leak it either
        self.assertNotIn(TOKEN, '\n'.join(logs.output))


class DoSyncErrorMessageMaskingTests(unittest.TestCase):
    """_do_sync's persisted failure text is the surface that leaves the box.

    account.last_error / AccountSyncLog.error_message are rendered in the UI and routed
    to outbound push notifications, so a raw path-token URL there is a credential leak
    off-machine, not just a noisy log line.
    """

    def setUp(self):
        self.t = make_test_app()
        self.account = M3uAccount(name='Token Provider', m3u_url=M3U_URL,
                                  epg_url=EPG_URL, status='OK')
        db.session.add(self.account)
        db.session.commit()

    def tearDown(self):
        self.t.cleanup()

    def test_failed_sync_persists_a_masked_error_message(self):
        exc = Exception(f'Max retries exceeded with url: {M3U_URL} (caused by NewConnectionError)')
        with patch.object(accounts_mod.requests, 'get', side_effect=exc):
            _do_sync = accounts_mod._do_sync
            _do_sync(self.account.id, threading.Event())
        db.session.expire_all()

        account = db.session.get(Account, self.account.id)
        self.assertEqual(account.status, 'ERROR')
        self.assertNotIn(TOKEN, account.last_error)
        self.assertIn('example-provider.test/***', account.last_error)

        log_row = (AccountSyncLog.query.filter_by(account_id=self.account.id)
                   .order_by(AccountSyncLog.id.desc()).first())
        self.assertEqual(log_row.status, 'ERROR')
        self.assertNotIn(TOKEN, log_row.error_message)


if __name__ == '__main__':
    unittest.main(verbosity=2)
