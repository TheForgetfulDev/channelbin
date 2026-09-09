"""Account sync never deletes channels, no matter how far the upstream feed shrinks.

Pins the central finding of DESIGN-sync-resilience.md §1 (fableFINAL2 #3): `_upsert_channels`
- the single funnel both the M3U and Xtream paths reach - is insert/update ONLY. A transient
upstream failure that returns an empty or drastically shortened feed therefore cannot wipe the
guide, dissolve a group, or orphan a scheduled recording. That claim was reached by reading the
code; these tests are the empirical half, and they exist to keep it true: any future change that
introduces channel deletion (hard or soft) breaks them loudly.

Drives the real end-to-end `_do_sync` path with a synthetic shrinking Xtream feed via
FileXtreamClient, reading dump files written under the test's own temp dir. No network: the dump
dirs deliberately contain no xmltv.xml, so the EPG branch short-circuits.

Note on what is NOT asserted here: today a feed that collapses to zero completes with
status=OK and emits no alert. That silence is a known gap, tracked separately as "Sync
resilience C" (DESIGN-sync-resilience.md §5) which adds an advisory SYNC_FEED_SHRUNK alert.
C changes the *observability* of this path, never the no-delete invariant below.

Runs against a throwaway temp SQLite DB - never the live dvr.db.
  python3 tests/test_account_sync_shrink.py
"""
import json
import os
import sys
import threading
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import db  # noqa: E402
from app.accounts import NORM_MPEGTS_LIVE, _do_sync  # noqa: E402
from app.config import _deep_merge, load_config  # noqa: E402
from app.database import (  # noqa: E402
    AccountSyncLog, Channel, ChannelGroup, Recording, XtreamAccount,
)
from tests.support import make_test_app  # noqa: E402
from tests.support.seed import make_channel, make_group, make_recording  # noqa: E402

BASE_URL = 'http://provider.test:8080'
USERNAME = 'testuser'
PASSWORD = 'testpass'


def _stream(sid):
    """One Xtream live-stream dict as the JSON API returns it (no _stream_url, so
    _upsert_channels constructs the URL from the account credentials like it does in prod)."""
    return {
        'stream_id': sid,
        'name': f'Channel {sid}',
        'stream_icon': f'http://provider.test/logo{sid}.png',
        'category_id': '1',
        'epg_channel_id': f'ch{sid}.test',
    }


class SyncShrinkTests(unittest.TestCase):
    def setUp(self):
        self.t = make_test_app()
        self.dump_base = os.path.join(self.t._tmpdir, 'xtream_dumps')

        self.account = XtreamAccount(
            name='Shrink Test', base_url=BASE_URL,
            username=USERNAME, password=PASSWORD, status='OK',
            # Pinned so the upsert loop can't fall through to the global default and make
            # the expected stream_url depend on config.yaml. MPEG-TS-with-live specifically:
            # this dump is catalog-only (no _stream_url), so every URL here is CONSTRUCTED,
            # and construction reproduces the account's chosen mode. This mode is the form
            # construction used to hardcode, which keeps the expected URLs below unchanged.
            # It can no longer be Disabled - that is now a hard stop on this path, since
            # there would be no format to build the URLs in (DESIGN-live-vod.md §4.3).
            url_normalization=NORM_MPEGTS_LIVE)
        db.session.add(self.account)
        db.session.flush()

        # Five channels, three of them carrying state that a delete would destroy:
        # guide membership, group membership, and a scheduled recording's FK.
        self.channels = [make_channel(self.account, stream_id=i, name=f'Channel {i}')
                         for i in range(1, 6)]
        self.channels[0].in_guide = True
        self.channels[0].guide_sort_order = 7
        self.group = make_group(name='Shrink Group', members=[self.channels[1]])
        self.recording = make_recording(status='SCHEDULED', channel_id=self.channels[2].id)
        db.session.commit()

        self.dump_seq = 0

    def tearDown(self):
        self.t.cleanup()

    def _write_dump(self, stream_ids):
        """Write a dump dir holding exactly these stream_ids and return nothing.

        Each call writes a later-sorting directory name, so _get_latest_dump_dir()
        picks up the most recent one - that is how a feed "changes" between syncs.
        """
        self.dump_seq += 1
        d = os.path.join(self.dump_base, str(self.account.id), f'2026-01-01_{self.dump_seq:02d}')
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, 'auth.json'), 'w', encoding='utf-8') as f:
            json.dump({'user_info': {'auth': 1, 'status': 'Active'}}, f)
        with open(os.path.join(d, 'live_streams_json_api.json'), 'w', encoding='utf-8') as f:
            json.dump([_stream(sid) for sid in stream_ids], f)
        # No xmltv.xml on purpose - keeps the EPG branch off the network.

    def _sync(self):
        """Run one real _do_sync against the newest dump dir.

        load_config is patched at app.accounts (module-top import there) so the dump base
        resolves inside the test's temp dir instead of the repo's dev/samples/xtream.
        """
        cfg = _deep_merge(load_config(), {'debug': {'xtream_dump_dir': self.dump_base}})
        with mock.patch('app.accounts.load_config', return_value=cfg):
            _do_sync(self.account.id, threading.Event(), use_dump=True)
        db.session.expire_all()

    def _assert_state_intact(self):
        """Every channel row and every reference to one still exists, unchanged."""
        self.assertEqual(Channel.query.filter_by(account_id=self.account.id).count(), 5)

        guide_ch = db.session.get(Channel, self.channels[0].id)
        self.assertTrue(guide_ch.in_guide)
        self.assertEqual(guide_ch.guide_sort_order, 7)

        grouped_ch = db.session.get(Channel, self.channels[1].id)
        self.assertIn(self.group.id,
                      [m.group_id for m in grouped_ch.group_memberships])
        self.assertIsNotNone(db.session.get(ChannelGroup, self.group.id))

        rec = db.session.get(Recording, self.recording.id)
        self.assertEqual(rec.channel_id, self.channels[2].id)

    def test_full_feed_syncs_all_five(self):
        """Baseline: the fixture and dump plumbing actually drive a real sync."""
        self._write_dump([1, 2, 3, 4, 5])
        self._sync()

        self.assertEqual(self.account.status, 'OK')
        self.assertEqual(self.account.channel_count, 5)
        self._assert_state_intact()

        # URL really was rebuilt from the feed, i.e. the upsert ran rather than no-opping.
        ch = db.session.get(Channel, self.channels[0].id)
        self.assertEqual(ch.raw_stream_url, f'{BASE_URL}/live/{USERNAME}/{PASSWORD}/1.ts')

    def test_feed_collapsing_to_empty_deletes_nothing(self):
        """A sync returning zero streams must not remove a single channel or reference.

        This is the uninstall-grade scenario the task exists for: expired subscription or
        soft rate-limit hands back an empty playlist on sync #2.
        """
        self._write_dump([1, 2, 3, 4, 5])
        self._sync()

        self._write_dump([])
        self._sync()

        self._assert_state_intact()
        self.assertEqual(self.account.channel_count, 5)
        self.assertEqual(self.account.status, 'OK')

        log_row = AccountSyncLog.query.filter_by(account_id=self.account.id).order_by(
            AccountSyncLog.id.desc()).first()
        self.assertEqual(log_row.status, 'SUCCESS')
        # channels_synced counts what this sync SAW, not what survives in the DB - the two
        # diverging is exactly the signal Sync resilience C alerts on.
        self.assertEqual(log_row.channels_synced, 0)

    def test_feed_shrinking_below_half_deletes_nothing(self):
        """Partial collapse (5 channels -> 1) is the same invariant, and the absent four
        keep their guide/group/recording state for whenever the feed comes back."""
        self._write_dump([1, 2, 3, 4, 5])
        self._sync()

        self._write_dump([4])
        self._sync()

        self._assert_state_intact()
        self.assertEqual(self.account.channel_count, 5)

        log_row = AccountSyncLog.query.filter_by(account_id=self.account.id).order_by(
            AccountSyncLog.id.desc()).first()
        self.assertEqual(log_row.channels_synced, 1)

    def test_recovered_feed_updates_in_place_without_duplicating(self):
        """After a collapse and recovery the original rows are reused, not re-created -
        i.e. the channels were never gone, so nothing is orphaned or duplicated."""
        self._write_dump([1, 2, 3, 4, 5])
        self._sync()
        original_ids = sorted(c.id for c in Channel.query.filter_by(
            account_id=self.account.id).all())

        self._write_dump([])
        self._sync()
        self._write_dump([1, 2, 3, 4, 5])
        self._sync()

        recovered_ids = sorted(c.id for c in Channel.query.filter_by(
            account_id=self.account.id).all())
        self.assertEqual(recovered_ids, original_ids)
        self._assert_state_intact()


if __name__ == '__main__':
    unittest.main(verbosity=2)
