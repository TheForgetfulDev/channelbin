"""EPG-fetch failures must be observable, not swallowed to a silent SUCCESS.

Pins DESIGN-sync-resilience.md §2-3 (Sync resilience A / changelog/242): before this fix,
`refresh_source` caught any fetch exception, logged a warning, and returned a bare `0` -
`_do_sync` had no way to distinguish "feed fetch died" from "healthy feed with zero entries",
so the sync always finished `AccountSyncLog.status = 'SUCCESS'` even when EPG silently didn't
update (real occurrence: sync_log 122, `InvalidChunkLength`, no alert, account stayed OK).

Two things are pinned:
  1. `refresh_source` itself returns `(0, reason)` on a fetch exception, `reason` starting
     `'fetch failed:'` and creds-masked (never the raw credentialed URL from the exception text).
  2. `_do_sync` end-to-end: a degraded EPG fetch finishes the sync `PARTIAL` (not `SUCCESS`),
     `error_message` carries the reason, `Account.status` stays `OK` (control value, unaffected -
     DESIGN-sync-resilience.md §2), and a `EPG_SOURCE_FETCH_FAILED` alert is raised. A subsequent
     healthy sync auto-dismisses that standing alert rather than leaving it live forever.

No network: `requests.get` is patched at `app.accounts.requests.get` so the m3u playlist fetch
succeeds locally while the epg_url fetch raises - no real socket is ever opened.

Runs against a throwaway temp SQLite DB - never the live dvr.db.
  python3 tests/test_sync_epg_fetch_failure.py
"""
import os
import sys
import threading
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import db  # noqa: E402
from app.accounts import _do_sync, refresh_source  # noqa: E402
from app.database import Account, Alert, AccountSyncLog, Channel, M3uAccount  # noqa: E402
from tests.support import make_test_app  # noqa: E402
from tests.test_sync_epg_collapse_guard import _xmltv  # noqa: E402
from tests.support.seed import make_epg_source  # noqa: E402

M3U_URL = 'http://provider.test/playlist.m3u8?user=realuser&pass=realpass'
EPG_URL = 'http://provider.test/xmltv.php?username=realuser&password=realpass'

M3U_PLAYLIST = (
    '#EXTM3U\n'
    '#EXTINF:-1 tvg-id="ch1.test",Channel 1\n'
    'http://provider.test/stream1.ts\n'
)


def _fake_get_epg_fetch_fails(url, **kwargs):
    if url == M3U_URL:
        resp = mock.Mock()
        resp.raise_for_status = mock.Mock()
        resp.status_code = 200
        resp.content = M3U_PLAYLIST.encode('utf-8')
        return resp
    if url == EPG_URL:
        # Deliberately embeds the credentialed URL in the exception text, like a real
        # requests exception stringifying the request it was attempting.
        raise ConnectionError(f'Connection refused to {EPG_URL}')
    raise AssertionError(f'unexpected requests.get call: {url}')


def _fake_get_all_healthy(url, **kwargs):
    if url == M3U_URL:
        resp = mock.Mock()
        resp.raise_for_status = mock.Mock()
        resp.status_code = 200
        resp.content = M3U_PLAYLIST.encode('utf-8')
        return resp
    if url == EPG_URL:
        resp = mock.Mock()
        resp.raise_for_status = mock.Mock()
        resp.status_code = 200
        # One real listing: a feed with no <programme> at all is itself reported as a
        # degradation (dev/changelog/1100), so it cannot stand in for a healthy one.
        resp.content = _xmltv([('ch1.test', 60, 30)])
        return resp
    raise AssertionError(f'unexpected requests.get call: {url}')


class SyncEpgFromUrlUnitTests(unittest.TestCase):
    """Unit-level: refresh_source's own return shape on a fetch exception."""

    def setUp(self):
        self.t = make_test_app()
        self.account = M3uAccount(name='Unit Test Account', m3u_url=M3U_URL,
                                  epg_url=EPG_URL, status='OK')
        db.session.add(self.account)
        self.source_id = make_epg_source(self.account).id
        db.session.commit()

    def tearDown(self):
        self.t.cleanup()

    def test_fetch_exception_returns_zero_and_reason(self):
        with mock.patch('app.accounts.requests.get',
                        side_effect=ConnectionError(f'Connection refused to {EPG_URL}')):
            count, reason = refresh_source(make_epg_source(self.account, url=EPG_URL), timeout=5, epg_days=3)
        self.assertEqual(count, 0)
        self.assertIsNotNone(reason)
        self.assertTrue(reason.startswith('fetch failed:'))
        # The exception text embeds the credentialed URL - it must never survive un-masked
        # into a value that gets persisted (AccountSyncLog.error_message) or shown in the UI.
        self.assertNotIn('realuser', reason)
        self.assertNotIn('realpass', reason)

    def test_healthy_fetch_returns_none_reason(self):
        resp = mock.Mock()
        resp.raise_for_status = mock.Mock()
        resp.status_code = 200
        resp.content = b'<?xml version="1.0"?><tv></tv>'
        with mock.patch('app.accounts.requests.get', return_value=resp):
            count, reason = refresh_source(make_epg_source(self.account, url=EPG_URL), timeout=5, epg_days=3)
        self.assertIsNone(reason)
        self.assertEqual(count, 0)  # empty <tv/>, but a real (not swallowed) zero


class DoSyncPartialStatusTests(unittest.TestCase):
    """End-to-end: _do_sync wiring of PARTIAL status + the EPG_SOURCE_FETCH_FAILED alert."""

    def setUp(self):
        self.t = make_test_app()
        self.account = M3uAccount(name='Partial Test Account', m3u_url=M3U_URL,
                                  epg_url=EPG_URL, status='OK')
        db.session.add(self.account)
        self.source_id = make_epg_source(self.account).id
        db.session.commit()

    def tearDown(self):
        self.t.cleanup()

    def _sync(self, fake_get):
        with mock.patch('app.accounts.requests.get', side_effect=fake_get):
            _do_sync(self.account.id, threading.Event())
        db.session.expire_all()

    def _latest_log(self):
        return (AccountSyncLog.query.filter_by(account_id=self.account.id)
                .order_by(AccountSyncLog.id.desc()).first())

    def _standing_alert(self):
        return Alert.query.filter_by(
            alert_type='EPG_SOURCE_FETCH_FAILED',
            source=f'epg-source:{self.source_id}:fetch',
        ).first()

    def test_epg_fetch_failure_finishes_partial_with_alert(self):
        self._sync(_fake_get_epg_fetch_fails)

        account = db.session.get(Account, self.account.id)
        self.assertEqual(account.status, 'OK')  # control value untouched, per design §2

        log_row = self._latest_log()
        self.assertEqual(log_row.status, 'PARTIAL')
        self.assertIsNotNone(log_row.error_message)
        self.assertIn('fetch failed', log_row.error_message)

        # Channel sync itself still succeeded - PARTIAL is about the EPG half only.
        self.assertEqual(Channel.query.filter_by(account_id=self.account.id).count(), 1)

        alert = self._standing_alert()
        self.assertIsNotNone(alert)
        self.assertIsNone(alert.dismissed_at)
        self.assertEqual(alert.severity, 'WARN')

    def test_healthy_sync_after_failure_dismisses_standing_alert(self):
        self._sync(_fake_get_epg_fetch_fails)
        self.assertIsNotNone(self._standing_alert())
        self.assertIsNone(self._standing_alert().dismissed_at)

        self._sync(_fake_get_all_healthy)

        log_row = self._latest_log()
        self.assertEqual(log_row.status, 'SUCCESS')
        self.assertIsNone(log_row.error_message)

        alert = self._standing_alert()
        self.assertIsNotNone(alert)
        self.assertIsNotNone(alert.dismissed_at)  # auto-resolved, not left standing forever

        # No second alert row was stacked for the same (type, source).
        self.assertEqual(
            Alert.query.filter_by(alert_type='EPG_SOURCE_FETCH_FAILED',
                                  source=f'epg-source:{self.source_id}:fetch').count(),
            1)

    def test_repeated_failures_refresh_one_alert_not_stack(self):
        self._sync(_fake_get_epg_fetch_fails)
        first_id = self._standing_alert().id

        self._sync(_fake_get_epg_fetch_fails)
        second = self._standing_alert()

        self.assertEqual(second.id, first_id)  # same row, refreshed - not a new one
        self.assertEqual(
            Alert.query.filter_by(alert_type='EPG_SOURCE_FETCH_FAILED',
                                  source=f'epg-source:{self.source_id}:fetch').count(),
            1)

    def test_fully_healthy_sync_stays_success_no_alert(self):
        self._sync(_fake_get_all_healthy)

        log_row = self._latest_log()
        self.assertEqual(log_row.status, 'SUCCESS')
        self.assertIsNone(log_row.error_message)
        self.assertIsNone(self._standing_alert())


if __name__ == '__main__':
    unittest.main(verbosity=2)
