"""EPG collapse guard - wipe prevention (DESIGN-sync-resilience.md §4, changelog/244).

Before this fix, `_import_xmltv` deleted all future EPG for an account's channels BEFORE
parsing the fetch, so a transient empty/garbage XMLTV response silently wiped the guide.
This pins the two-phase parse: count what the real import would create first (sharing one
match predicate with the real import loop, `_match_program`, so the two can't drift), and
refuse the import - keeping old EPG untouched - when the projected count is below
`sync.epg_collapse_threshold_percent` percent of the baseline (0 = guard disabled; baseline
0, i.e. a first-ever sync, is exempt - nothing to compare against).

The baseline is `_visible_epg_baseline()` - a LIVE count of the entries this account holds
for channels that are not hidden - rather than the cached `account.epg_entry_count` it read
until dev/changelog/781. Hiding a channel deletes its EPG and takes it out of the projected
count, so a cached total from before a large hide would read a legitimate drop as a provider
collapse and freeze that account's guide. The fixtures below therefore seed the number of
rows they mean as the baseline; setting the cached column alone no longer arms anything. "Force EPG Resync" (a route flag threaded through
sync_account -> _do_sync -> _sync_epg_from_url / _import_xmltv) bypasses the guard for one
sync.

Covers:
  - MatchPredicateTests: `_match_program` / `_count_projected_epg_entries` pure logic
    (channel match, case sensitivity, import window).
  - ImportGuardTests: `_import_xmltv`'s guard math directly - refuse/pass/disabled/
    first-sync exemption/force bypass, and that a refusal never touches existing rows.
  - DoSyncAlertRoutingTests: end-to-end `_do_sync` - PARTIAL status,
    SYNC_EPG_COLLAPSE_REFUSED (never SYNC_EPG_FETCH_FAILED) raised/dismissed/refreshed
    correctly, force_epg_resync end-to-end.
  - RouteForceEpgResyncTests: the route threads the flag through to sync_account,
    independent of the pre-existing conflict-override `force` flag.

No network: `requests.get` is patched at `app.accounts.requests.get`. Runs against a
throwaway temp SQLite DB - never the live dvr.db.
  python3 -m unittest tests.test_sync_epg_collapse_guard
"""
import os
import sys
import threading
import unittest
from datetime import datetime, timedelta
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import event  # noqa: E402

from app import db  # noqa: E402
from app.accounts import (  # noqa: E402
    _do_sync, _import_xmltv, _count_projected_epg_entries,
)
from app.database import Account, Alert, AccountSyncLog, Channel, EPGEntry, M3uAccount  # noqa: E402
from tests.support import make_test_app  # noqa: E402
from tests.support import seed  # noqa: E402

M3U_URL = 'http://provider.test/playlist.m3u8?user=realuser&pass=realpass'
EPG_URL = 'http://provider.test/xmltv.php?username=realuser&password=realpass'

M3U_PLAYLIST = (
    '#EXTM3U\n'
    '#EXTINF:-1 tvg-id="ch1.test",Channel 1\n'
    'http://provider.test/stream1.ts\n'
)


def _xmltv(entries):
    """entries: iterable of (epg_channel_id, offset_minutes, duration_minutes). One
    <programme> per entry, well inside the default 3-day import window."""
    now = datetime.utcnow()
    parts = ['<?xml version="1.0" encoding="UTF-8"?><tv>']
    for chid, offset_min, dur_min in entries:
        start = now + timedelta(minutes=offset_min)
        stop = start + timedelta(minutes=dur_min)
        parts.append(
            f'<programme start="{start.strftime("%Y%m%d%H%M%S")} +0000" '
            f'stop="{stop.strftime("%Y%m%d%H%M%S")} +0000" channel="{chid}">'
            f'<title>Show</title></programme>'
        )
    parts.append('</tv>')
    return ''.join(parts).encode('utf-8')


class MatchPredicateTests(unittest.TestCase):
    """`_match_program` / `_count_projected_epg_entries` pure logic."""

    def setUp(self):
        self.channel_map = {'ch1.test': [1], 'ch2.test': [2, 3]}
        self.window_start = datetime.utcnow() - timedelta(hours=1)
        self.window_end = datetime.utcnow() + timedelta(days=3)

    def test_count_matches_only_in_window_and_known_channels(self):
        xml = _xmltv([
            ('ch1.test', 60, 30),               # in scope, 1 channel id
            ('ch2.test', 120, 30),               # in scope, 2 channel ids
            ('unknown.test', 60, 30),            # unmatched channel
            ('ch1.test', 60 * 24 * 10, 30),      # way outside the window
        ])
        count, parse_error = _count_projected_epg_entries(xml, self.channel_map, False,
                                                          self.window_start, self.window_end)
        self.assertEqual(count, 1 + 2)
        self.assertIsNone(parse_error)

    def test_case_insensitive_matching_when_not_case_sensitive(self):
        xml = _xmltv([('CH1.TEST', 60, 30)])
        count, parse_error = _count_projected_epg_entries(xml, self.channel_map, False,
                                                          self.window_start, self.window_end)
        self.assertEqual(count, 1)
        self.assertIsNone(parse_error)

    def test_case_sensitive_matching_rejects_case_variant(self):
        xml = _xmltv([('CH1.TEST', 60, 30)])
        count, parse_error = _count_projected_epg_entries(xml, self.channel_map, True,
                                                          self.window_start, self.window_end)
        self.assertEqual(count, 0)
        self.assertIsNone(parse_error)


class ImportGuardTests(unittest.TestCase):
    """`_import_xmltv`'s own guard math, directly - no fetch/HTTP involved."""

    def setUp(self):
        self.t = make_test_app()
        self.account = M3uAccount(name='Guard Test', m3u_url=M3U_URL, status='OK')
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

    def _seed_old_epg(self, count):
        now = datetime.utcnow()
        for i in range(count):
            db.session.add(EPGEntry(
                channel_id=self.channel.id, title=f'Old {i}',
                start_time=now + timedelta(hours=i), stop_time=now + timedelta(hours=i, minutes=30),
            ))
        db.session.commit()

    def _cfg(self, threshold_pct=20):
        return {'sync': {'epg_collapse_threshold_percent': threshold_pct}}

    def test_first_sync_baseline_zero_is_exempt(self):
        # epg_entry_count defaults to 0 - nothing to compare against, guard never fires
        # even though a single entry would fail any nonzero baseline comparison.
        xml = _xmltv([('ch1.test', 60, 30)])
        synced, reason = _import_xmltv(self.account, xml, epg_days=3, cfg=self._cfg())
        self.assertIsNone(reason)
        self.assertEqual(synced, 1)

    def test_projected_below_threshold_is_refused_and_old_epg_survives(self):
        self._seed_old_epg(100)
        xml = _xmltv([('ch1.test', 60, 30)])  # 1 entry, well below 20% of 100 (= 20 required)

        synced, reason = _import_xmltv(self.account, xml, epg_days=3, cfg=self._cfg())

        self.assertEqual(synced, 0)
        self.assertIsNotNone(reason)
        self.assertTrue(reason.startswith('import refused:'))
        self.assertEqual(EPGEntry.query.filter_by(channel_id=self.channel.id).count(), 100,
                         'old EPG must survive a refused import completely untouched')

    def test_projected_at_or_above_threshold_proceeds_normally(self):
        self._seed_old_epg(10)
        # threshold 20% of 10 = 2 required; provide exactly 2.
        xml = _xmltv([('ch1.test', 60, 30), ('ch1.test', 180, 30)])

        synced, reason = _import_xmltv(self.account, xml, epg_days=3, cfg=self._cfg())

        self.assertIsNone(reason)
        self.assertEqual(synced, 2)
        titles = {e.title for e in EPGEntry.query.filter_by(channel_id=self.channel.id).all()}
        self.assertNotIn('Old 0', titles, 'a passed guard must still delete-then-replace as before')

    def test_threshold_zero_disables_the_guard(self):
        self._seed_old_epg(100)
        xml = _xmltv([('ch1.test', 60, 30)])  # would otherwise be refused

        synced, reason = _import_xmltv(self.account, xml, epg_days=3,
                                       cfg=self._cfg(threshold_pct=0))

        self.assertIsNone(reason)
        self.assertEqual(synced, 1)

    def test_force_epg_resync_bypasses_a_refusal(self):
        self._seed_old_epg(100)
        xml = _xmltv([('ch1.test', 60, 30)])

        synced, reason = _import_xmltv(self.account, xml, epg_days=3, cfg=self._cfg(),
                                       force_epg_resync=True)

        self.assertIsNone(reason)
        self.assertEqual(synced, 1)


class ChannelMapFanoutTests(unittest.TestCase):
    """`_import_xmltv`'s channel_map build reads id/epg_channel_id as a column-tuple query
    (dev/changelog/689) rather than hydrating full Channel ORM rows. That must still fan one
    epg_channel_id out to every channel that shares it (setdefault + append across query
    rows) and merge case variants into the same bucket - both are easy to lose in a
    naive column-query conversion (e.g. a dict overwrite instead of an appended list)."""

    def setUp(self):
        self.t = make_test_app()
        self.account = M3uAccount(name='Fanout Test', m3u_url=M3U_URL, status='OK')
        db.session.add(self.account)
        db.session.flush()
        self.ch1 = Channel(
            account_id=self.account.id, stream_id=1, name='Ch1',
            stream_url='http://example.test/live/1', epg_channel_id='shared.test',
        )
        self.ch2 = Channel(
            account_id=self.account.id, stream_id=2, name='Ch2',
            stream_url='http://example.test/live/2', epg_channel_id='SHARED.TEST',
        )
        db.session.add_all([self.ch1, self.ch2])
        db.session.commit()

    def tearDown(self):
        self.t.cleanup()

    def test_program_fans_out_to_every_channel_sharing_the_epg_id(self):
        xml = _xmltv([('shared.test', 60, 30)])
        synced, reason = _import_xmltv(
            self.account, xml, epg_days=3,
            cfg={'sync': {'epg_collapse_threshold_percent': 0}})

        self.assertIsNone(reason)
        self.assertEqual(synced, 2, 'one programme mapped to two channels must import two rows')
        self.assertEqual(EPGEntry.query.filter_by(channel_id=self.ch1.id).count(), 1)
        self.assertEqual(EPGEntry.query.filter_by(channel_id=self.ch2.id).count(), 1)


class DeleteScopeSubqueryTests(unittest.TestCase):
    """The pre-import delete now scopes channel ids via a subquery
    (Channel.account_id == :a AND epg_channel_id truthy) instead of a materialized
    Python id list, so it never binds one SQL variable per channel (dev/docs/BUGS.md
    2026-08-15). Proves the subquery still selects exactly the right rows: this
    account's EPG-mapped channel, and nothing belonging to another account or to a
    channel with no epg_channel_id."""

    def setUp(self):
        self.t = make_test_app()
        self.account = M3uAccount(name='Guard Test', m3u_url=M3U_URL, status='OK')
        self.other_account = M3uAccount(
            name='Other Account', m3u_url='http://other.test/playlist.m3u8', status='OK')
        db.session.add_all([self.account, self.other_account])
        db.session.flush()
        self.channel = Channel(
            account_id=self.account.id, stream_id=1, name='Ch1',
            stream_url='http://example.test/live/1', epg_channel_id='ch1.test',
        )
        self.no_epg_channel = Channel(
            account_id=self.account.id, stream_id=2, name='NoEpgId',
            stream_url='http://example.test/live/2', epg_channel_id='',
        )
        self.other_channel = Channel(
            account_id=self.other_account.id, stream_id=1, name='OtherCh1',
            stream_url='http://other.test/live/1', epg_channel_id='ch1.test',
        )
        db.session.add_all([self.channel, self.no_epg_channel, self.other_channel])
        db.session.commit()

    def tearDown(self):
        self.t.cleanup()

    def test_delete_scoped_to_this_accounts_epg_mapped_channels_only(self):
        now = datetime.utcnow()
        db.session.add(EPGEntry(
            channel_id=self.channel.id, title='Old', start_time=now, stop_time=now + timedelta(minutes=30)))
        db.session.add(EPGEntry(
            channel_id=self.other_channel.id, title='Other Old',
            start_time=now, stop_time=now + timedelta(minutes=30)))
        db.session.commit()

        xml = _xmltv([('ch1.test', 60, 30)])
        synced, reason = _import_xmltv(
            self.account, xml, epg_days=3,
            cfg={'sync': {'epg_collapse_threshold_percent': 0}})

        self.assertIsNone(reason)
        self.assertEqual(synced, 1)
        titles = {e.title for e in EPGEntry.query.filter_by(channel_id=self.channel.id).all()}
        self.assertNotIn('Old', titles, 'this account\'s stale EPG must be deleted')
        other_titles = {e.title for e in EPGEntry.query.filter_by(channel_id=self.other_channel.id).all()}
        self.assertIn('Other Old', other_titles,
                       'another account sharing the same epg_channel_id must be untouched')

    def test_delete_statement_parameter_count_does_not_scale_with_channel_count(self):
        # The old code bound one SQL parameter per channel id in the delete's IN-list, so
        # with enough EPG-mapped channels it would exceed SQLite's SQLITE_MAX_VARIABLE_NUMBER
        # (32,766) and the import would die with "too many SQL variables" - the largest real
        # account (34,012 channels) hits this (dev/docs/BUGS.md 2026-08-15). A subquery binds
        # a constant few params no matter how many channels exist. 20 channels here is already
        # enough to distinguish "N+ params" (old code) from "a small constant" (new code)
        # without needing to seed tens of thousands of rows.
        for i in range(20):
            db.session.add(Channel(
                account_id=self.account.id, stream_id=100 + i, name=f'Extra{i}',
                stream_url=f'http://example.test/live/extra{i}', epg_channel_id=f'extra{i}.test',
            ))
        db.session.commit()

        captured = []

        def _on_execute(conn, cursor, statement, parameters, context, executemany):
            if statement.strip().upper().startswith('DELETE FROM epg_entries'.upper()):
                captured.append(parameters)

        engines = {eng for eng in db.engines.values()}
        for eng in engines:
            event.listen(eng, 'before_cursor_execute', _on_execute)
        try:
            xml = _xmltv([('ch1.test', 60, 30)])
            synced, reason = _import_xmltv(
                self.account, xml, epg_days=3,
                cfg={'sync': {'epg_collapse_threshold_percent': 0}})
        finally:
            for eng in engines:
                event.remove(eng, 'before_cursor_execute', _on_execute)

        self.assertIsNone(reason)
        self.assertEqual(len(captured), 1, 'expected exactly one delete statement')
        param_count = len(captured[0])
        self.assertLessEqual(
            param_count, 10,
            f'delete bound {param_count} parameters for 21 EPG-mapped channels - the '
            'parameter count must not scale with channel count (subquery, not an id list)')


def _fake_get_factory(xml_bytes):
    def _fake_get(url, **kwargs):
        if url == M3U_URL:
            resp = mock.Mock()
            resp.raise_for_status = mock.Mock()
            resp.content = M3U_PLAYLIST.encode('utf-8')
            return resp
        if url == EPG_URL:
            resp = mock.Mock()
            resp.raise_for_status = mock.Mock()
            resp.content = xml_bytes
            return resp
        raise AssertionError(f'unexpected requests.get call: {url}')
    return _fake_get


class DoSyncAlertRoutingTests(unittest.TestCase):
    """End-to-end `_do_sync`: PARTIAL status + the SYNC_EPG_COLLAPSE_REFUSED alert,
    kept distinct from SYNC_EPG_FETCH_FAILED (item A's alert type)."""

    def setUp(self):
        self.t = make_test_app()
        self.account = M3uAccount(name='Routing Test', m3u_url=M3U_URL, epg_url=EPG_URL, status='OK')
        db.session.add(self.account)
        db.session.commit()

    def tearDown(self):
        self.t.cleanup()

    def _healthy_xml(self):
        return _xmltv([('ch1.test', 60 + i * 60, 30) for i in range(20)])

    def _sync(self, xml_bytes, force_epg_resync=False):
        with mock.patch('app.accounts.requests.get', side_effect=_fake_get_factory(xml_bytes)):
            _do_sync(self.account.id, threading.Event(), force_epg_resync=force_epg_resync)
        db.session.expire_all()

    def _latest_log(self):
        return (AccountSyncLog.query.filter_by(account_id=self.account.id)
                .order_by(AccountSyncLog.id.desc()).first())

    def _collapse_alert(self):
        return Alert.query.filter_by(
            alert_type='SYNC_EPG_COLLAPSE_REFUSED',
            source=f'account:{self.account.id}:epg-collapse').first()

    def _fetch_alert(self):
        return Alert.query.filter_by(
            alert_type='SYNC_EPG_FETCH_FAILED',
            source=f'account:{self.account.id}:epg-fetch').first()

    def test_collapse_refusal_finishes_partial_with_the_right_alert_not_fetch_failed(self):
        self._sync(self._healthy_xml())  # establish a healthy baseline (20 entries)
        account = db.session.get(Account, self.account.id)
        self.assertGreater(account.epg_entry_count, 0)

        self._sync(_xmltv([('ch1.test', 60, 30)]))  # collapsed feed: 1 entry

        log_row = self._latest_log()
        self.assertEqual(log_row.status, 'PARTIAL')
        self.assertIn('import refused', log_row.error_message)
        self.assertIsNotNone(self._collapse_alert())
        self.assertIsNone(self._collapse_alert().dismissed_at)
        self.assertIsNone(self._fetch_alert(), 'must not fire the fetch-failed alert type')

        # Old EPG (from the healthy sync) survived the refusal.
        self.assertGreater(
            EPGEntry.query.join(Channel).filter(Channel.account_id == self.account.id).count(), 0)

    def test_recovery_dismisses_the_collapse_alert(self):
        self._sync(self._healthy_xml())
        self._sync(_xmltv([('ch1.test', 60, 30)]))
        self.assertIsNotNone(self._collapse_alert())
        self.assertIsNone(self._collapse_alert().dismissed_at)

        self._sync(self._healthy_xml())

        log_row = self._latest_log()
        self.assertEqual(log_row.status, 'SUCCESS')
        self.assertIsNotNone(self._collapse_alert().dismissed_at)

    def test_repeated_refusals_refresh_not_stack(self):
        self._sync(self._healthy_xml())
        self._sync(_xmltv([('ch1.test', 60, 30)]))
        first_id = self._collapse_alert().id

        self._sync(_xmltv([('ch1.test', 60, 30)]))
        second = self._collapse_alert()

        self.assertEqual(second.id, first_id)
        self.assertEqual(
            Alert.query.filter_by(alert_type='SYNC_EPG_COLLAPSE_REFUSED',
                                  source=f'account:{self.account.id}:epg-collapse').count(),
            1)

    def test_force_epg_resync_bypasses_end_to_end(self):
        self._sync(self._healthy_xml())

        self._sync(_xmltv([('ch1.test', 60, 30)]), force_epg_resync=True)

        log_row = self._latest_log()
        self.assertEqual(log_row.status, 'SUCCESS')
        self.assertIsNone(log_row.error_message)
        self.assertIsNone(self._collapse_alert())

    def test_fully_healthy_sync_never_fires_the_collapse_alert(self):
        self._sync(self._healthy_xml())
        self.assertIsNone(self._collapse_alert())


def _join_sync_threads():
    for t in threading.enumerate():
        if t.name.startswith('account-sync-'):
            t.join(timeout=5)


class RouteForceEpgResyncTests(unittest.TestCase):
    """`routes/accounts.py::sync_account_api` threads force_epg_resync through, as an
    independent flag from the conflict-override `force` field.

    The route moved from a form POST to the JSON API in dev/changelog/456; the two flags
    still mean different things and neither may imply the other.
    """

    def setUp(self):
        self.t = make_test_app()
        self.t.app.config['WTF_CSRF_ENABLED'] = False
        self.account = seed.make_account(name='Force Resync Test')
        db.session.commit()
        self.account_id = self.account.id

    def tearDown(self):
        _join_sync_threads()
        self.t.cleanup()

    def _post(self, json_body=None):
        spy = mock.Mock()
        with mock.patch('app.accounts.sync_account', spy):
            resp = self.t.client.post(f'/api/accounts/{self.account_id}/sync', json=json_body)
            _join_sync_threads()
        return resp, spy

    def test_force_epg_resync_flag_is_threaded_through(self):
        _, spy = self._post({'force_epg_resync': True})
        spy.assert_called_once()
        self.assertTrue(spy.call_args.kwargs.get('force_epg_resync'))

    def test_default_is_false(self):
        _, spy = self._post()
        spy.assert_called_once()
        self.assertFalse(spy.call_args.kwargs.get('force_epg_resync'))

    def test_force_epg_resync_does_not_imply_the_conflict_override(self):
        """One flag, one meaning: forcing an EPG resync must not also walk past a
        recording-in-progress conflict the user was never shown."""
        with mock.patch('app.accounts.sync_conflicts', return_value=['Reactor is melting.']):
            resp, spy = self._post({'force_epg_resync': True})
        self.assertEqual(resp.status_code, 409)
        spy.assert_not_called()

    def test_the_action_is_offered_on_the_accounts_page(self):
        body = self.t.client.get('/accounts').get_data(as_text=True)
        self.assertIn('data-act="force-epg"', body)


if __name__ == '__main__':
    unittest.main(verbosity=2)
