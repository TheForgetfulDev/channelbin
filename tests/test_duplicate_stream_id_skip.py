"""Duplicate stream_id entries within one sync are counted, logged and recorded rather than
silently dropped (dev/docs/BUGS.md 2026-08-30 "Duplicate stream_id entries in one playlist
are dropped with no trace").

`_upsert_channels` used to `continue` on a stream_id already seen this sync with no counter,
no log line and no alert - unlike the sibling skipped_malformed case, which is surfaced. Since
the URL-derived id takes the last numeric path segment, two different URLs for what a provider
intends as one slot (`/u/p/123.ts` vs `/u/p/123.m3u8`) collide, and a genuine loss (two
DIFFERENT channels colliding on the same id) looked identical from the outside - nothing named
the reason a channel count came out slightly lower than the feed's own count.

Covers:
  - UpsertDuplicateCountTests: _upsert_channels counts and samples duplicates directly, the
    first-seen entry (not the last) wins, and a clean sync reports zero.
  - DuplicateAlertWiringTests: end-to-end through _do_sync (mocked requests.get, M3U path) -
    two playlist URLs that collide on the same numeric tail record the count on that sync's
    own AccountSyncLog row, and a collision-free playlist records zero. The count reached the
    user as a DUPLICATE_STREAM_IDS_SKIPPED alert until dev/changelog/926 gave it a column and
    dev/changelog/928 retired the alert.

No network, no real ffmpeg - see CLAUDE.md §Testing.
Run standalone:
  python3 -m unittest tests.test_duplicate_stream_id_skip
"""
import os
import sys
import threading
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import db  # noqa: E402
from app.accounts import _do_sync, _upsert_channels  # noqa: E402
from app.database import AccountSyncLog, Alert, Channel, M3uAccount  # noqa: E402
from tests.support import make_test_app  # noqa: E402

M3U_URL = 'http://provider.test/playlist.m3u8?user=realuser&pass=realpass'


def _stream(sid, **overrides):
    """One M3U-sourced stream dict. `_stream_url` is present, so no URL construction."""
    base = {
        'stream_id': sid,
        'name': f'Ch{sid}',
        '_stream_url': f'http://provider.test/live/u/p/{sid}.ts',
    }
    base.update(overrides)
    return base


class UpsertDuplicateCountTests(unittest.TestCase):
    def setUp(self):
        self.t = make_test_app()
        self.account = M3uAccount(name='Dup Test', m3u_url=M3U_URL, status='OK')
        db.session.add(self.account)
        db.session.commit()

    def tearDown(self):
        self.t.cleanup()

    def test_duplicate_stream_id_is_counted_and_skipped(self):
        synced, malformed, dup, _drifted, _new = _upsert_channels(
            self.account, [_stream(1), _stream(1), _stream(2)])
        db.session.commit()

        self.assertEqual(dup, 1)
        self.assertEqual(synced, 2, 'the duplicate must not count toward synced')
        self.assertEqual(malformed, 0)
        self.assertEqual(Channel.query.filter_by(account_id=self.account.id).count(), 2)

    def test_first_seen_entry_wins_not_the_last(self):
        _upsert_channels(self.account, [
            _stream(1, name='First'),
            _stream(1, name='Second'),
        ])
        db.session.commit()

        ch = Channel.query.filter_by(account_id=self.account.id, stream_id=1).first()
        self.assertEqual(
            ch.name, 'First',
            'a later duplicate overwrote the first-seen row instead of being skipped')

    def test_sample_is_capped_rather_than_unbounded(self):
        """The count is exact even though the sample kept for the log line is capped."""
        _, _, dup, _, _ = _upsert_channels(self.account, [_stream(1)] * 50)
        db.session.commit()

        self.assertEqual(dup, 49)

    def test_no_duplicates_reports_zero(self):
        _, _, dup, _, _ = _upsert_channels(self.account, [_stream(1), _stream(2)])
        db.session.commit()

        self.assertEqual(dup, 0)


class DuplicateAlertWiringTests(unittest.TestCase):
    """End-to-end through the real _do_sync (M3U path, mocked requests.get) - proves the
    call-site wiring and the alert, not just the counter in isolation."""

    def setUp(self):
        self.t = make_test_app()
        self.account = M3uAccount(name='Wiring Dup Test', m3u_url=M3U_URL, status='OK')
        db.session.add(self.account)
        db.session.commit()

    def tearDown(self):
        self.t.cleanup()

    def _sync(self, playlist_body):
        def _fake_get(url, **kwargs):
            if url == M3U_URL:
                resp = mock.Mock()
                resp.raise_for_status = mock.Mock()
                resp.content = playlist_body.encode('utf-8')
                return resp
            raise AssertionError(f'unexpected requests.get call: {url}')

        with mock.patch('app.accounts.requests.get', side_effect=_fake_get):
            _do_sync(self.account.id, threading.Event())
        db.session.expire_all()

    def _last_sync_log(self):
        return (AccountSyncLog.query
                .filter_by(account_id=self.account.id)
                .order_by(AccountSyncLog.id.desc()).first())

    def test_colliding_urls_record_the_count_on_the_sync(self):
        # Different extensions, same numeric tail - both parse to stream_id 123
        # (app/accounts.py::_parse_m3u_as_streams's sid_match), the real collision shape
        # named in the bug, not a synthetic one.
        playlist = (
            '#EXTM3U\n'
            '#EXTINF:-1 tvg-id="a.test",Channel A\n'
            'http://provider.test/u/p/123.ts\n'
            '#EXTINF:-1 tvg-id="b.test",Channel B\n'
            'http://provider.test/u/p/123.m3u8\n'
        )
        self._sync(playlist)

        self.assertEqual(self._last_sync_log().skipped_duplicate_stream_ids, 1,
                         'the count belongs to the sync that skipped them')
        self.assertEqual(
            Channel.query.filter_by(account_id=self.account.id).count(), 1,
            'the colliding entry must be dropped, not both kept as separate channels')
        self.assertIsNone(
            Alert.query.filter_by(alert_type='DUPLICATE_STREAM_IDS_SKIPPED').first(),
            'the count is shown on the account, never raised as an alert')

    def test_no_collision_records_zero(self):
        playlist = (
            '#EXTM3U\n'
            '#EXTINF:-1 tvg-id="a.test",Channel A\n'
            'http://provider.test/u/p/1.ts\n'
            '#EXTINF:-1 tvg-id="b.test",Channel B\n'
            'http://provider.test/u/p/2.ts\n'
        )
        self._sync(playlist)

        self.assertEqual(self._last_sync_log().skipped_duplicate_stream_ids, 0,
                         'zero is a measured answer, distinct from "not tracked"')


if __name__ == '__main__':
    unittest.main(verbosity=2)
