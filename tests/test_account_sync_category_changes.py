"""Per-sync provider category changes on AccountSyncLog (dev/changelog/1211).

AccountSyncLog.categories_added / categories_removed hold the provider categories
(Channel.category_name) a sync found new or gone since the sync before it, each with its
channel count. The previous sync's categories must be read BEFORE the upsert, because the
upsert overwrites a moved channel's category_name. A first sync counts every category as new,
the way channels_added counts every channel; a sync whose previous sync cannot be identified
records NULL ("not tracked"), never a list of every category as new.

Also pins the channels_removed lower bound being read before the sync-log prune, which can
delete the row that bound comes from (dev/docs/BUGS.md 2026-10-16 07:18 PM).

Drives the real _do_sync path via FileXtreamClient against dump files, same harness as
test_account_sync_added_removed.py. No network.

  python3 tests/test_account_sync_category_changes.py
"""
import json
import os
import sys
import threading
import unittest
from datetime import timedelta
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import db  # noqa: E402
from app.accounts import NORM_MPEGTS_LIVE, _do_sync  # noqa: E402
from app.config import _deep_merge, load_config  # noqa: E402
from app.database import AccountSyncLog, Channel, XtreamAccount  # noqa: E402
from app.db_utils import retry_on_locked  # noqa: E402
from tests.support import make_test_app  # noqa: E402

BASE_URL = 'http://provider.test:8080'


def _stream(sid, category):
    return {
        'stream_id': sid,
        'name': f'Channel {sid}',
        'category_id': '1',
        'category_name': category,
        'epg_channel_id': f'ch{sid}.test',
    }


class SyncCategoryChangesTests(unittest.TestCase):
    def setUp(self):
        self.t = make_test_app()
        self.dump_base = os.path.join(self.t._tmpdir, 'xtream_dumps')
        self.account = XtreamAccount(
            name='Category Test', base_url=BASE_URL, username='u', password='p',
            status='OK', url_normalization=NORM_MPEGTS_LIVE)
        db.session.add(self.account)
        db.session.commit()
        self.dump_seq = 0

    def tearDown(self):
        self.t.cleanup()

    def _write_dump(self, streams):
        """streams: {stream_id: category_name}"""
        self.dump_seq += 1
        d = os.path.join(self.dump_base, str(self.account.id), f'2026-01-01_{self.dump_seq:02d}')
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, 'auth.json'), 'w', encoding='utf-8') as f:
            json.dump({'user_info': {'auth': 1, 'status': 'Active'}}, f)
        with open(os.path.join(d, 'live_streams_json_api.json'), 'w', encoding='utf-8') as f:
            json.dump([_stream(sid, cat) for sid, cat in streams.items()], f)

    def _sync(self, streams, cfg_extra=None):
        self._write_dump(streams)
        cfg = _deep_merge(load_config(), {'debug': {'xtream_dump_dir': self.dump_base}})
        if cfg_extra:
            cfg = _deep_merge(cfg, cfg_extra)
        with mock.patch('app.accounts.load_config', return_value=cfg):
            _do_sync(self.account.id, threading.Event(), use_dump=True)
        db.session.expire_all()
        return AccountSyncLog.query.filter_by(account_id=self.account.id).order_by(
            AccountSyncLog.id.desc()).first()

    def test_first_sync_counts_every_category_as_new(self):
        log = self._sync({1: 'News', 2: 'News', 3: 'Sports'})
        self.assertEqual(log.category_changes(), {
            'added': [{'name': 'News', 'channels': 2}, {'name': 'Sports', 'channels': 1}],
            'removed': [],
        })

    def test_second_sync_reports_new_and_gone_categories_with_channel_counts(self):
        self._sync({1: 'News', 2: 'Sports', 3: 'Sports', 4: 'Kids'})
        log = self._sync({1: 'News', 5: 'Movies', 6: 'Movies', 7: 'Movies', 4: 'Kids'})
        self.assertEqual(log.category_changes(), {
            'added': [{'name': 'Movies', 'channels': 3}],
            'removed': [{'name': 'Sports', 'channels': 2}],
        })

    def test_a_channel_moving_category_is_one_new_and_one_gone(self):
        """The previous sync's names must be read before the upsert rewrites
        category_name - read after it, the old name has vanished from every row."""
        self._sync({1: 'US| SPORTS', 2: 'US| SPORTS'})
        log = self._sync({1: 'US| SPORTS HD', 2: 'US| SPORTS HD'})
        self.assertEqual(log.category_changes(), {
            'added': [{'name': 'US| SPORTS HD', 'channels': 2}],
            'removed': [{'name': 'US| SPORTS', 'channels': 2}],
        })

    def test_unchanged_feed_records_empty_lists_not_null(self):
        self._sync({1: 'News', 2: 'Sports'})
        log = self._sync({1: 'News', 2: 'Sports'})
        self.assertEqual(log.category_changes(), {'added': [], 'removed': []})

    def test_diff_is_against_the_immediately_preceding_sync(self):
        self._sync({1: 'News', 2: 'Sports'})
        self._sync({1: 'News'})
        log = self._sync({1: 'News'})
        self.assertEqual(log.category_changes(), {'added': [], 'removed': []},
                         'Sports went in sync 2 and must not be reported gone again')

    def test_blank_category_is_not_a_category(self):
        self._sync({1: 'News'})
        log = self._sync({1: 'News', 2: ''})
        self.assertEqual(log.category_changes(), {'added': [], 'removed': []})

    def test_largest_category_first(self):
        log = self._sync({1: 'B', 2: 'A', 3: 'C', 4: 'C'})
        self.assertEqual([c['name'] for c in log.category_changes()['added']], ['C', 'A', 'B'])

    def test_previous_sync_with_no_log_row_records_not_tracked(self):
        """Neither diff can be bounded without the previous sync's start, so both say so
        rather than reporting every category as new and a false zero removed."""
        self._sync({1: 'News', 2: 'Sports'})

        @retry_on_locked()
        def _drop_logs():
            AccountSyncLog.query.filter_by(account_id=self.account.id).delete()
            db.session.commit()
        _drop_logs()

        log = self._sync({1: 'News', 3: 'Movies'})
        self.assertIsNone(log.categories_added)
        self.assertIsNone(log.categories_removed)
        self.assertIsNone(log.category_changes())
        self.assertIsNone(log.channels_removed)

    def test_pruned_previous_log_still_bounds_the_diffs(self):
        """An account that last synced longer ago than sync_log_keep_days loses that sync's
        log row to the prune at the start of the next one. The bound is read before the
        prune, so both diffs are still real (dev/docs/BUGS.md 2026-10-16 07:18 PM)."""
        self._sync({1: 'News', 2: 'Sports', 3: 'Sports'})

        @retry_on_locked()
        def _age_everything():
            # The whole first sync moved 40 days back, every timestamp by the same amount.
            shift = timedelta(days=40)
            acct = db.session.get(XtreamAccount, self.account.id)
            prev = AccountSyncLog.query.filter_by(account_id=acct.id).one()
            prev.started_at -= shift
            prev.completed_at -= shift
            acct.last_sync_at = prev.completed_at
            for ch in Channel.query.filter_by(account_id=acct.id):
                ch.last_seen_at -= shift
                ch.first_seen_at -= shift
            db.session.commit()
        _age_everything()

        log = self._sync({1: 'News'}, {'sync': {'sync_log_keep_days': 30}})
        self.assertEqual(AccountSyncLog.query.filter_by(account_id=self.account.id).count(), 1,
                         'the 40-day-old log row must have been pruned for this to test anything')
        self.assertEqual(log.channels_removed, 2)
        self.assertEqual(log.category_changes(), {
            'added': [], 'removed': [{'name': 'Sports', 'channels': 2}]})


class CategoryChangesParseTests(unittest.TestCase):
    """AccountSyncLog.category_changes() never raises on a stored blob."""

    def test_null_either_side_is_not_tracked(self):
        self.assertIsNone(AccountSyncLog(categories_added='[]').category_changes())
        self.assertIsNone(AccountSyncLog(categories_removed='[]').category_changes())

    def test_unparseable_blob_is_not_tracked(self):
        self.assertIsNone(AccountSyncLog(categories_added='{oops',
                                         categories_removed='[]').category_changes())
        self.assertIsNone(AccountSyncLog(categories_added='{}',
                                         categories_removed='[]').category_changes())


if __name__ == '__main__':
    unittest.main(verbosity=2)
