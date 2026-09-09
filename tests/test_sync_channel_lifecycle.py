"""Channel lifecycle tracking + feed-shrink alert (DESIGN-sync-resilience.md §5,
changelog/245).

Channels are never deleted or disabled by sync - vanished channels used to linger
indefinitely with no signal, and new channels were indistinguishable from old ones. This
adds first_seen_at/last_seen_at (stamped in the one shared _upsert_channels choke point),
pure derived display states ("missing from provider" / "new", never stored - see
channel_lifecycle_state()), and three alerts raised at sync mark-success time:
SYNC_FEED_SHRUNK (WARN, standing, auto-resolving), SYNC_CHANNELS_MISSING /
SYNC_CHANNELS_NEW (INFO digests, per-transition, dismissed manually).

Covers:
  - UpsertChannelsLifecycleTests: _upsert_channels stamps first_seen_at/last_seen_at
    correctly (new vs. existing channel) and returns new_channel_ids.
  - ChannelLifecycleStateTests: the pure channel_lifecycle_state() derived-state
    function - missing/new/suppressed/disabled cases.
  - LifecycleAlertsUnitTests: _raise_channel_lifecycle_alerts() directly - feed-shrink
    fire/disable, the missing-digest's newly-crossing-only logic (and first-ever-check
    skip), the new-digest's first-sync-era suppression (by count and by earliest-sync
    recency).
  - DoSyncLifecycleWiringTests: end-to-end through the real _do_sync (mocked
    requests.get, M3U path) - proves the baseline-capture/call-site wiring, not just the
    alerts function in isolation.

No network, no real ffmpeg - see CLAUDE.md §Testing.
Run standalone:
  python3 -m unittest tests.test_sync_channel_lifecycle
"""
import os
import sys
import threading
import unittest
from datetime import datetime, timedelta
from unittest import mock

from sqlalchemy import event

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import db  # noqa: E402
from app.accounts import (  # noqa: E402
    _do_sync, _upsert_channels, channel_lifecycle_state, _raise_channel_lifecycle_alerts,
)
from app.database import AccountSyncLog, Alert, Channel, M3uAccount  # noqa: E402
from tests.support import make_test_app  # noqa: E402

M3U_URL = 'http://provider.test/playlist.m3u8?user=realuser&pass=realpass'


def _playlist(channel_ids):
    lines = ['#EXTM3U']
    for cid in channel_ids:
        lines.append(f'#EXTINF:-1 tvg-id="{cid}.test",{cid}')
        lines.append(f'http://provider.test/stream_{cid}.ts')
    return '\n'.join(lines) + '\n'


class UpsertChannelsLifecycleTests(unittest.TestCase):
    def setUp(self):
        self.t = make_test_app()
        self.account = M3uAccount(name='Upsert Test', m3u_url=M3U_URL, status='OK')
        db.session.add(self.account)
        db.session.commit()

    def tearDown(self):
        self.t.cleanup()

    def _streams(self, *ids):
        return [{'stream_id': i, 'name': f'Ch{i}', '_stream_url': f'http://example.test/live/{i}'}
                for i in ids]

    def test_new_channel_gets_both_timestamps_and_is_reported(self):
        synced, skipped, _dup, drifted, new_ids = _upsert_channels(self.account, self._streams(1))
        db.session.commit()

        ch = Channel.query.filter_by(account_id=self.account.id, stream_id=1).first()
        self.assertIsNotNone(ch.first_seen_at)
        self.assertIsNotNone(ch.last_seen_at)
        self.assertEqual(new_ids, [ch.id])

    def test_existing_channel_keeps_first_seen_but_bumps_last_seen(self):
        _upsert_channels(self.account, self._streams(1))
        db.session.commit()
        ch = Channel.query.filter_by(account_id=self.account.id, stream_id=1).first()
        original_first_seen = ch.first_seen_at
        ch.last_seen_at = datetime.utcnow() - timedelta(days=5)
        db.session.commit()

        synced, skipped, _dup, drifted, new_ids = _upsert_channels(self.account, self._streams(1))
        db.session.commit()

        db.session.expire_all()
        ch = Channel.query.filter_by(account_id=self.account.id, stream_id=1).first()
        self.assertEqual(ch.first_seen_at, original_first_seen)
        self.assertGreater(ch.last_seen_at, datetime.utcnow() - timedelta(minutes=1))
        self.assertEqual(new_ids, [], 'an existing channel must not be reported as new')

    def test_absent_channel_is_left_untouched(self):
        _upsert_channels(self.account, self._streams(1, 2))
        db.session.commit()
        ch2 = Channel.query.filter_by(account_id=self.account.id, stream_id=2).first()
        stale = datetime.utcnow() - timedelta(days=10)
        ch2.last_seen_at = stale
        db.session.commit()

        _upsert_channels(self.account, self._streams(1))  # ch2 absent this time
        db.session.commit()

        db.session.expire_all()
        ch2 = Channel.query.filter_by(account_id=self.account.id, stream_id=2).first()
        self.assertEqual(ch2.last_seen_at, stale, "an absent channel's last_seen_at must not move")


class ChannelLifecycleStateTests(unittest.TestCase):
    """The pure derived-state function - no I/O, precomputed aggregates passed in."""

    def setUp(self):
        self.account = M3uAccount(name='State Test', m3u_url=M3U_URL, status='OK')
        self.channel = Channel(
            account_id=None, stream_id=1, name='Ch1',
            stream_url='http://example.test/live/1',
        )
        self.cfg = {'sync': {'channel_missing_after_days': 7, 'channel_new_within_days': 3}}

    def test_old_last_seen_with_account_synced_since_is_missing(self):
        now = datetime.utcnow()
        self.channel.last_seen_at = now - timedelta(days=10)
        self.account.last_sync_at = now - timedelta(days=1)
        state, since = channel_lifecycle_state(self.channel, self.account, self.cfg, 5, None)
        self.assertEqual(state, 'missing')
        self.assertEqual(since, self.channel.last_seen_at)

    def test_old_last_seen_but_account_not_synced_since_is_not_missing(self):
        """A paused/erroring account must not brand its whole roster missing when the
        feed was never actually consulted again."""
        now = datetime.utcnow()
        self.channel.last_seen_at = now - timedelta(days=10)
        self.account.last_sync_at = now - timedelta(days=15)  # older than last_seen_at
        state, _ = channel_lifecycle_state(self.channel, self.account, self.cfg, 5, None)
        self.assertIsNone(state)

    def test_recent_last_seen_is_not_missing(self):
        now = datetime.utcnow()
        self.channel.last_seen_at = now - timedelta(hours=1)
        self.account.last_sync_at = now
        state, _ = channel_lifecycle_state(self.channel, self.account, self.cfg, 5, None)
        self.assertIsNone(state)

    def test_recent_first_seen_with_established_account_is_new(self):
        now = datetime.utcnow()
        self.channel.first_seen_at = now - timedelta(days=1)
        earliest = now - timedelta(days=30)
        state, since = channel_lifecycle_state(self.channel, self.account, self.cfg, 5, earliest)
        self.assertEqual(state, 'new')
        self.assertEqual(since, self.channel.first_seen_at)

    def test_new_suppressed_by_low_completed_sync_count(self):
        now = datetime.utcnow()
        self.channel.first_seen_at = now - timedelta(days=1)
        earliest = now - timedelta(days=30)
        state, _ = channel_lifecycle_state(self.channel, self.account, self.cfg, 1, earliest)
        self.assertIsNone(state)

    def test_new_suppressed_by_recent_earliest_sync(self):
        now = datetime.utcnow()
        self.channel.first_seen_at = now - timedelta(days=1)
        earliest = now - timedelta(hours=1)  # account's own first-sync era, still recent
        state, _ = channel_lifecycle_state(self.channel, self.account, self.cfg, 5, earliest)
        self.assertIsNone(state)

    def test_missing_days_zero_disables_missing_state(self):
        cfg = {'sync': {'channel_missing_after_days': 0, 'channel_new_within_days': 3}}
        now = datetime.utcnow()
        self.channel.last_seen_at = now - timedelta(days=100)
        self.account.last_sync_at = now
        state, _ = channel_lifecycle_state(self.channel, self.account, cfg, 5, None)
        self.assertIsNone(state)

    def test_new_days_zero_disables_new_state(self):
        cfg = {'sync': {'channel_missing_after_days': 7, 'channel_new_within_days': 0}}
        now = datetime.utcnow()
        self.channel.first_seen_at = now
        state, _ = channel_lifecycle_state(self.channel, self.account, cfg, 5, now - timedelta(days=30))
        self.assertIsNone(state)


class LifecycleAlertsUnitTests(unittest.TestCase):
    """_raise_channel_lifecycle_alerts() called directly - controlled Channel/
    AccountSyncLog rows, no HTTP/sync involved."""

    def setUp(self):
        self.t = make_test_app()
        self.account = M3uAccount(name='Alerts Test', m3u_url=M3U_URL, status='OK')
        db.session.add(self.account)
        db.session.commit()
        self.next_stream_id = 1

    def tearDown(self):
        self.t.cleanup()

    def _seed_channel(self, last_seen_at=None, first_seen_at=None, name=None):
        sid = self.next_stream_id
        self.next_stream_id += 1
        ch = Channel(
            account_id=self.account.id, stream_id=sid, name=name or f'ch{sid}',
            stream_url=f'http://example.test/live/{sid}',
            last_seen_at=last_seen_at, first_seen_at=first_seen_at,
        )
        db.session.add(ch)
        db.session.flush()
        return ch

    def _cfg(self, **overrides):
        cfg = {'feed_shrink_percent': 50, 'channel_missing_after_days': 7,
              'channel_new_within_days': 3}
        cfg.update(overrides)
        return {'sync': cfg}

    def _feed_shrunk_alert(self):
        return Alert.query.filter_by(
            alert_type='SYNC_FEED_SHRUNK', source=f'account:{self.account.id}:feed-shrunk').first()

    def _missing_alert(self):
        return Alert.query.filter_by(
            alert_type='SYNC_CHANNELS_MISSING',
            source=f'account:{self.account.id}:channels-missing').first()

    def _new_alert(self):
        return Alert.query.filter_by(
            alert_type='SYNC_CHANNELS_NEW', source=f'account:{self.account.id}:channels-new').first()

    # ── Feed shrink ────────────────────────────────────────────────────────────

    def test_feed_shrink_fires_above_threshold(self):
        now = datetime.utcnow()
        for _ in range(10):
            self._seed_channel(last_seen_at=now - timedelta(hours=1))
        db.session.commit()

        _raise_channel_lifecycle_alerts(self.account, self._cfg(), now, None, 10, [])

        self.assertIsNotNone(self._feed_shrunk_alert())

    def test_feed_shrink_percent_zero_disables(self):
        now = datetime.utcnow()
        for _ in range(10):
            self._seed_channel(last_seen_at=now - timedelta(hours=1))
        db.session.commit()

        _raise_channel_lifecycle_alerts(self.account, self._cfg(feed_shrink_percent=0), now, None, 10, [])

        self.assertIsNone(self._feed_shrunk_alert())

    def test_feed_shrink_below_threshold_does_not_fire(self):
        now = datetime.utcnow()
        # Only 1 of 10 baseline channels absent (10%), well under the 50% default.
        # The 9 "seen" channels are stamped exactly at sync_time, matching what
        # _upsert_channels would have just done for a channel touched this sync.
        self._seed_channel(last_seen_at=now - timedelta(hours=1))
        for _ in range(9):
            self._seed_channel(last_seen_at=now)
        db.session.commit()

        _raise_channel_lifecycle_alerts(self.account, self._cfg(), now, None, 10, [])

        self.assertIsNone(self._feed_shrunk_alert())

    def test_feed_shrink_resolves_on_recovery(self):
        now = datetime.utcnow()
        channels = [self._seed_channel(last_seen_at=now - timedelta(hours=1)) for _ in range(10)]
        db.session.commit()
        _raise_channel_lifecycle_alerts(self.account, self._cfg(), now, None, 10, [])
        self.assertIsNotNone(self._feed_shrunk_alert())
        self.assertIsNone(self._feed_shrunk_alert().dismissed_at)

        recovery_time = datetime.utcnow()
        for ch in channels:
            ch.last_seen_at = recovery_time
        db.session.commit()
        _raise_channel_lifecycle_alerts(self.account, self._cfg(), recovery_time, now, 10, [])

        db.session.expire_all()
        self.assertIsNotNone(self._feed_shrunk_alert().dismissed_at)

    # ── Missing digest ─────────────────────────────────────────────────────────

    def test_missing_digest_only_includes_newly_crossing_channels(self):
        now = datetime.utcnow()
        self._seed_channel(last_seen_at=now - timedelta(days=8), name='JustCrossed')
        self._seed_channel(last_seen_at=now - timedelta(days=20), name='LongMissing')
        db.session.commit()
        previous_last_sync_at = now - timedelta(days=7, hours=12)

        _raise_channel_lifecycle_alerts(self.account, self._cfg(), now, previous_last_sync_at, 2, [])

        alert = self._missing_alert()
        self.assertIsNotNone(alert)
        self.assertIn('JustCrossed', alert.body)
        self.assertNotIn('LongMissing', alert.body)

    def test_missing_digest_skipped_on_first_ever_check(self):
        now = datetime.utcnow()
        self._seed_channel(last_seen_at=now - timedelta(days=30))
        db.session.commit()

        _raise_channel_lifecycle_alerts(self.account, self._cfg(), now, None, 1, [])

        self.assertIsNone(self._missing_alert())

    def test_missing_days_zero_disables_digest(self):
        now = datetime.utcnow()
        self._seed_channel(last_seen_at=now - timedelta(days=30))
        db.session.commit()

        _raise_channel_lifecycle_alerts(
            self.account, self._cfg(channel_missing_after_days=0), now,
            now - timedelta(days=29), 1, [])

        self.assertIsNone(self._missing_alert())

    # ── New digest ─────────────────────────────────────────────────────────────

    def test_new_digest_fires_when_not_suppressed(self):
        now = datetime.utcnow()
        ch = self._seed_channel(first_seen_at=now, name='FreshChannel')
        db.session.commit()
        old = now - timedelta(days=30)
        for _ in range(2):
            db.session.add(AccountSyncLog(account_id=self.account.id, started_at=old, status='SUCCESS'))
        db.session.commit()

        _raise_channel_lifecycle_alerts(self.account, self._cfg(), now, None, 1, [ch.id])

        alert = self._new_alert()
        self.assertIsNotNone(alert)
        self.assertIn('FreshChannel', alert.body)

    def test_new_digest_suppressed_by_low_completed_count(self):
        now = datetime.utcnow()
        ch = self._seed_channel(first_seen_at=now)
        db.session.commit()
        # No AccountSyncLog rows at all -> completed_count = 0.

        _raise_channel_lifecycle_alerts(self.account, self._cfg(), now, None, 1, [ch.id])

        self.assertIsNone(self._new_alert())

    def test_new_digest_suppressed_by_recent_earliest_sync(self):
        now = datetime.utcnow()
        ch = self._seed_channel(first_seen_at=now)
        db.session.commit()
        recent = now - timedelta(hours=1)
        for _ in range(2):
            db.session.add(AccountSyncLog(account_id=self.account.id, started_at=recent, status='SUCCESS'))
        db.session.commit()

        _raise_channel_lifecycle_alerts(self.account, self._cfg(), now, None, 1, [ch.id])

        self.assertIsNone(self._new_alert())

    def test_new_days_zero_disables_digest(self):
        now = datetime.utcnow()
        ch = self._seed_channel(first_seen_at=now)
        db.session.commit()
        old = now - timedelta(days=30)
        for _ in range(2):
            db.session.add(AccountSyncLog(account_id=self.account.id, started_at=old, status='SUCCESS'))
        db.session.commit()

        _raise_channel_lifecycle_alerts(
            self.account, self._cfg(channel_new_within_days=0), now, None, 1, [ch.id])

        self.assertIsNone(self._new_alert())

    def test_new_digest_count_is_exact_when_more_than_five_new_channels(self):
        # new_channel_ids query is capped to a 5-name sample (dev/docs/BUGS.md 2026-08-15)
        # so the reported total must come from len(new_channel_ids), not the sample rowset.
        now = datetime.utcnow()
        channels = [self._seed_channel(first_seen_at=now, name=f'Fresh{i}') for i in range(8)]
        db.session.commit()
        old = now - timedelta(days=30)
        for _ in range(2):
            db.session.add(AccountSyncLog(account_id=self.account.id, started_at=old, status='SUCCESS'))
        db.session.commit()

        _raise_channel_lifecycle_alerts(
            self.account, self._cfg(), now, None, 1, [ch.id for ch in channels])

        alert = self._new_alert()
        self.assertIsNotNone(alert)
        self.assertIn('8 new channel(s)', alert.title)
        self.assertIn('and 3 more', alert.body)

    def test_new_digest_sample_query_parameter_count_does_not_scale_with_new_channels(self):
        # The old code queried Channel.id.in_(new_channel_ids) - one SQL parameter per new
        # channel id. A mass re-add sync can put tens of thousands of ids in that list, which
        # would risk the same "too many SQL variables" failure as the EPG delete
        # (dev/docs/BUGS.md 2026-08-15). The sample query is now capped to 5 ids regardless
        # of how many channels are new.
        now = datetime.utcnow()
        channels = [self._seed_channel(first_seen_at=now, name=f'Fresh{i}') for i in range(20)]
        db.session.commit()
        old = now - timedelta(days=30)
        for _ in range(2):
            db.session.add(AccountSyncLog(account_id=self.account.id, started_at=old, status='SUCCESS'))
        db.session.commit()

        captured = []

        def _on_execute(conn, cursor, statement, parameters, context, executemany):
            if 'FROM channels' in statement and ' IN (' in statement:
                captured.append(parameters)

        engines = {eng for eng in db.engines.values()}
        for eng in engines:
            event.listen(eng, 'before_cursor_execute', _on_execute)
        try:
            _raise_channel_lifecycle_alerts(
                self.account, self._cfg(), now, None, 1, [ch.id for ch in channels])
        finally:
            for eng in engines:
                event.remove(eng, 'before_cursor_execute', _on_execute)

        self.assertEqual(len(captured), 1, 'expected exactly one channel-sample select')
        param_count = len(captured[0])
        self.assertLessEqual(
            param_count, 5,
            f'sample query bound {param_count} parameters for 20 new channels - it must stay '
            'capped at 5 regardless of how many channels are new')


class DoSyncLifecycleWiringTests(unittest.TestCase):
    """End-to-end through the real _do_sync (M3U path, mocked requests.get) - proves the
    baseline-capture/call-site wiring, not just the alerts function in isolation."""

    def setUp(self):
        self.t = make_test_app()
        self.account = M3uAccount(name='Wiring Test', m3u_url=M3U_URL, status='OK')
        db.session.add(self.account)
        db.session.commit()

    def tearDown(self):
        self.t.cleanup()

    def _sync(self, channel_ids):
        def _fake_get(url, **kwargs):
            if url == M3U_URL:
                resp = mock.Mock()
                resp.raise_for_status = mock.Mock()
                resp.content = _playlist(channel_ids).encode('utf-8')
                return resp
            raise AssertionError(f'unexpected requests.get call: {url}')

        with mock.patch('app.accounts.requests.get', side_effect=_fake_get):
            _do_sync(self.account.id, threading.Event())
        db.session.expire_all()

    def test_feed_shrink_alert_fires_and_resolves_end_to_end(self):
        self._sync([f'ch{i}' for i in range(10)])
        self._sync([f'ch{i}' for i in range(2)])  # 8 of 10 (80%) absent >= default 50%

        alert = Alert.query.filter_by(
            alert_type='SYNC_FEED_SHRUNK', source=f'account:{self.account.id}:feed-shrunk').first()
        self.assertIsNotNone(alert)
        self.assertIsNone(alert.dismissed_at)

        self._sync([f'ch{i}' for i in range(10)])  # full recovery
        db.session.expire_all()
        alert = Alert.query.filter_by(
            alert_type='SYNC_FEED_SHRUNK', source=f'account:{self.account.id}:feed-shrunk').first()
        self.assertIsNotNone(alert.dismissed_at)

    def test_new_channel_creates_timestamps_visible_via_do_sync(self):
        self._sync(['ch0'])
        ch = Channel.query.filter_by(account_id=self.account.id, epg_channel_id='ch0.test').first()
        self.assertIsNotNone(ch.first_seen_at)
        self.assertIsNotNone(ch.last_seen_at)


if __name__ == '__main__':
    unittest.main(verbosity=2)
