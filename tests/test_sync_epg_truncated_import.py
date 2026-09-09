"""A truncated/malformed XMLTV must never finish as a healthy sync (dev/changelog/719).

`_import_xmltv` deletes the account's future EPG in its own committed transaction BEFORE
the parse loop (deliberately - the write lock must not be held across the parse). So when
the payload stopped parsing partway, the guide was left holding only what had been
imported while the sync log said plain SUCCESS: a 95%-empty guide reported as healthy,
which is the founding product principle inverted. The parse-error branch now returns an
`import truncated:` degradation reason, which finishes the sync PARTIAL and raises the
standing SYNC_EPG_IMPORT_TRUNCATED alert.

The collapse guard's count pass had the mirror-image defect: no exception handling at all,
so the identical payload raised out of `_count_projected_epg_entries` and failed the WHOLE
sync as ERROR - misattributing an EPG-only fault to a channel sync that had already
committed. It now reports the parse error to its caller, which refuses the import on that
fact rather than on the threshold math, keeping the old EPG.

Covers:
  - CountPassTests: `_count_projected_epg_entries` reports rather than raises, and returns
    what parsed before the break.
  - TruncatedImportTests: `_import_xmltv`'s reason, its count honesty (entries still
    buffered when the parse died are flushed, not counted as imported and dropped), and
    that a truncation caught by the count pass refuses instead of deleting.
  - AlertRoutingTests: end-to-end `_do_sync` - PARTIAL, the truncated alert and not either
    sibling type, auto-dismiss on recovery, no stacking across repeats.
  - DegradationPrefixTests: every reason prefix this module writes is registered, and an
    unregistered one still reaches a surface instead of vanishing.

No network: `requests.get` is patched at `app.accounts.requests.get` and the fixtures are
local byte strings. Runs against a throwaway temp SQLite DB - never the live dvr.db.
  python3 -m unittest tests.test_sync_epg_truncated_import
"""
import os
import sys
import threading
import unittest
from datetime import datetime, timedelta
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import db  # noqa: E402
from app.accounts import (  # noqa: E402
    EPG_DEGRADATION_ALERT_TYPES, _count_projected_epg_entries, _do_sync,
    _epg_degradation_alert_type, _import_xmltv,
)
from app.alerts import ALERT_TYPES  # noqa: E402
from app.database import Account, AccountSyncLog, Alert, Channel, EPGEntry, M3uAccount  # noqa: E402
from tests.support import make_test_app  # noqa: E402

M3U_URL = 'http://provider.test/playlist.m3u8?user=realuser&pass=realpass'
EPG_URL = 'http://provider.test/xmltv.php?username=realuser&password=realpass'

M3U_PLAYLIST = (
    '#EXTM3U\n'
    '#EXTINF:-1 tvg-id="ch1.test",Channel 1\n'
    'http://provider.test/stream1.ts\n'
)


def _programs(count, chid='ch1.test'):
    """`count` well-formed <programme> elements, all inside the default import window."""
    now = datetime.utcnow()
    parts = []
    for i in range(count):
        start = now + timedelta(minutes=60 + i * 60)
        stop = start + timedelta(minutes=30)
        parts.append(
            f'<programme start="{start.strftime("%Y%m%d%H%M%S")} +0000" '
            f'stop="{stop.strftime("%Y%m%d%H%M%S")} +0000" channel="{chid}">'
            f'<title>Show {i}</title></programme>'
        )
    return ''.join(parts)


def _xmltv(count, chid='ch1.test'):
    """A complete, well-formed XMLTV document."""
    return (f'<?xml version="1.0" encoding="UTF-8"?><tv>{_programs(count, chid)}</tv>'
            ).encode('utf-8')


def _truncated_xmltv(count, chid='ch1.test'):
    """`count` complete programs, then the document simply stops - a download cut off
    mid-flight. The closing </tv> never arrives, so iterparse raises partway through."""
    return (f'<?xml version="1.0" encoding="UTF-8"?><tv>{_programs(count, chid)}'
            '<programme start="20990101000000 +0000" stop="209901').encode('utf-8')


def _fake_get_factory(xml_bytes):
    def _fake_get(url, **kwargs):
        resp = mock.Mock()
        resp.raise_for_status = mock.Mock()
        if url == M3U_URL:
            resp.content = M3U_PLAYLIST.encode('utf-8')
            return resp
        if url == EPG_URL:
            resp.content = xml_bytes
            return resp
        raise AssertionError(f'unexpected requests.get call: {url}')
    return _fake_get


class CountPassTests(unittest.TestCase):
    """`_count_projected_epg_entries` reports a parse failure instead of raising."""

    def setUp(self):
        self.channel_map = {'ch1.test': [1]}
        self.window_start = datetime.utcnow() - timedelta(hours=1)
        self.window_end = datetime.utcnow() + timedelta(days=3)

    def _count(self, xml_bytes):
        return _count_projected_epg_entries(xml_bytes, self.channel_map, False,
                                            self.window_start, self.window_end)

    def test_truncated_payload_reports_the_error_rather_than_raising(self):
        count, parse_error = self._count(_truncated_xmltv(4))
        self.assertIsNotNone(parse_error, 'a truncated payload must not raise out of the '
                                          'count pass and fail the whole sync')
        self.assertEqual(count, 4, 'the count returned alongside an error is what parsed '
                                   'before the break')

    def test_healthy_payload_still_reports_no_error(self):
        count, parse_error = self._count(_xmltv(4))
        self.assertEqual(count, 4)
        self.assertIsNone(parse_error)


class TruncatedImportTests(unittest.TestCase):
    """`_import_xmltv` on a payload that stops parsing - both sides of the delete."""

    def setUp(self):
        self.t = make_test_app()
        self.account = M3uAccount(name='Truncation Test', m3u_url=M3U_URL, status='OK')
        db.session.add(self.account)
        db.session.flush()
        self.channel = Channel(
            account_id=self.account.id, stream_id=1, name='Ch1',
            stream_url='http://example.test/live/1', epg_channel_id='ch1.test',
        )
        db.session.add(self.channel)
        db.session.commit()

    def tearDown(self):
        self.t.cleanup()

    def _epg_count(self):
        return EPGEntry.query.filter_by(channel_id=self.channel.id).count()

    def _seed_old_epg(self, count):
        now = datetime.utcnow()
        for i in range(count):
            db.session.add(EPGEntry(
                channel_id=self.channel.id, title=f'Old {i}',
                start_time=now + timedelta(hours=i),
                stop_time=now + timedelta(hours=i, minutes=30),
            ))
        db.session.commit()
        self.account.epg_entry_count = count
        db.session.commit()

    def test_truncation_after_the_delete_returns_a_named_degradation_reason(self):
        # Guard disabled, so nothing runs before the delete - the silent-success path.
        synced, reason = _import_xmltv(
            self.account, _truncated_xmltv(5), epg_days=3,
            cfg={'sync': {'epg_collapse_threshold_percent': 0}})

        self.assertIsNotNone(reason, 'a truncated import must not report a healthy sync')
        self.assertTrue(reason.startswith('import truncated:'), reason)
        self.assertEqual(synced, 5)
        self.assertIn('5 entries', reason, 'the reason names how much actually landed')

    def test_the_entries_that_did_parse_are_kept(self):
        synced, _reason = _import_xmltv(
            self.account, _truncated_xmltv(5), epg_days=3,
            cfg={'sync': {'epg_collapse_threshold_percent': 0}})
        self.assertEqual(self._epg_count(), 5)
        self.assertEqual(synced, 5)

    def test_reported_count_matches_what_was_actually_inserted(self):
        # `synced` counts rows appended to the pending batch, so anything still buffered
        # when the parse died was reported as imported and then dropped on the way out.
        synced, reason = _import_xmltv(
            self.account, _truncated_xmltv(7), epg_days=3,
            cfg={'sync': {'epg_collapse_threshold_percent': 0}})
        self.assertEqual(self._epg_count(), synced,
                         'the returned count must equal the rows actually in the database')
        self.assertIn(f'{synced} entries', reason)

    def test_truncation_caught_by_the_count_pass_refuses_and_keeps_the_old_epg(self):
        self._seed_old_epg(10)
        synced, reason = _import_xmltv(
            self.account, _truncated_xmltv(5), epg_days=3,
            cfg={'sync': {'epg_collapse_threshold_percent': 20}})

        self.assertEqual(synced, 0)
        self.assertTrue(reason.startswith('import refused:'), reason)
        self.assertIn('truncated or malformed', reason,
                      'the refusal must name the broken payload, not blame the entry count')
        self.assertEqual(self._epg_count(), 10, 'old EPG must survive a refusal')

    def test_force_epg_resync_imports_what_parses_from_a_truncated_payload(self):
        self._seed_old_epg(10)
        synced, reason = _import_xmltv(
            self.account, _truncated_xmltv(5), epg_days=3,
            cfg={'sync': {'epg_collapse_threshold_percent': 20}}, force_epg_resync=True)

        self.assertEqual(synced, 5)
        self.assertTrue(reason.startswith('import truncated:'), reason)
        self.assertEqual(self._epg_count(), 5, 'the bypass proceeds through the delete')

    def test_a_healthy_payload_is_still_reported_healthy(self):
        synced, reason = _import_xmltv(
            self.account, _xmltv(5), epg_days=3,
            cfg={'sync': {'epg_collapse_threshold_percent': 0}})
        self.assertEqual(synced, 5)
        self.assertIsNone(reason)


class AlertRoutingTests(unittest.TestCase):
    """End-to-end `_do_sync`: PARTIAL plus SYNC_EPG_IMPORT_TRUNCATED, never a sibling."""

    def setUp(self):
        self.t = make_test_app()
        self.account = M3uAccount(name='Routing Test', m3u_url=M3U_URL, epg_url=EPG_URL,
                                  status='OK')
        db.session.add(self.account)
        db.session.commit()

    def tearDown(self):
        self.t.cleanup()

    def _sync(self, xml_bytes):
        with mock.patch('app.accounts.requests.get', side_effect=_fake_get_factory(xml_bytes)):
            _do_sync(self.account.id, threading.Event())
        db.session.expire_all()

    def _latest_log(self):
        return (AccountSyncLog.query.filter_by(account_id=self.account.id)
                .order_by(AccountSyncLog.id.desc()).first())

    def _alerts(self, alert_type, source_suffix):
        return Alert.query.filter_by(
            alert_type=alert_type,
            source=f'account:{self.account.id}:{source_suffix}').all()

    def _truncated_alerts(self):
        return self._alerts('SYNC_EPG_IMPORT_TRUNCATED', 'epg-truncated')

    def test_truncated_import_finishes_partial_with_its_own_alert(self):
        self._sync(_truncated_xmltv(5))

        log_row = self._latest_log()
        self.assertEqual(log_row.status, 'PARTIAL')
        self.assertIn('import truncated', log_row.error_message)

        alerts = self._truncated_alerts()
        self.assertEqual(len(alerts), 1)
        self.assertIsNone(alerts[0].dismissed_at)
        self.assertEqual(self._alerts('SYNC_EPG_FETCH_FAILED', 'epg-fetch'), [],
                         'the fetch succeeded - its alert promises the old EPG was kept, '
                         'which is false on this path')
        self.assertEqual(self._alerts('SYNC_EPG_COLLAPSE_REFUSED', 'epg-collapse'), [])

    def test_account_status_stays_ok_and_the_channel_sync_is_not_blamed(self):
        self._sync(_truncated_xmltv(5))
        account = db.session.get(Account, self.account.id)
        self.assertEqual(account.status, 'OK',
                         'an EPG-only degradation must not fail the whole sync')
        self.assertEqual(self._latest_log().channels_synced, 1)

    def test_repeated_truncated_syncs_refresh_rather_than_stack(self):
        self._sync(_truncated_xmltv(5))
        self._sync(_truncated_xmltv(5))
        self.assertEqual(len(self._truncated_alerts()), 1)

    def test_a_later_healthy_sync_dismisses_the_standing_alert(self):
        self._sync(_truncated_xmltv(5))
        self.assertIsNone(self._truncated_alerts()[0].dismissed_at)

        self._sync(_xmltv(20))

        self.assertEqual(self._latest_log().status, 'SUCCESS')
        self.assertIsNotNone(self._truncated_alerts()[0].dismissed_at)

    def test_a_healthy_sync_never_raises_it(self):
        self._sync(_xmltv(20))
        self.assertEqual(self._latest_log().status, 'SUCCESS')
        self.assertEqual(self._truncated_alerts(), [])


class DegradationPrefixTests(unittest.TestCase):
    """The prefix -> alert type map is the whole discriminator, so it has to be total."""

    def test_every_registered_type_exists_in_the_alert_registry(self):
        for prefix, alert_type in EPG_DEGRADATION_ALERT_TYPES.items():
            self.assertIn(alert_type, ALERT_TYPES, f'{prefix!r} routes to an unknown type')

    def test_each_registered_prefix_routes_to_its_own_type(self):
        for prefix, alert_type in EPG_DEGRADATION_ALERT_TYPES.items():
            self.assertEqual(_epg_degradation_alert_type(f'{prefix} something happened'),
                             alert_type)

    def test_a_healthy_sync_routes_to_no_type(self):
        self.assertIsNone(_epg_degradation_alert_type(None))
        self.assertIsNone(_epg_degradation_alert_type(''))

    def test_an_unregistered_prefix_still_reaches_a_surface(self):
        # The failure mode this guards is silence: an unrouted reason would leave a
        # degraded sync with no alert at all. Loud and misfiled beats silent.
        with self.assertLogs('app.accounts', level='ERROR'):
            self.assertEqual(_epg_degradation_alert_type('something new: happened'),
                             'SYNC_EPG_FETCH_FAILED')


if __name__ == '__main__':
    unittest.main()
