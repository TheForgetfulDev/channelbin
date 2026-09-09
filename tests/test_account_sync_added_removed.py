"""Per-sync channels_added/channels_removed on AccountSyncLog (dev/changelog/480).

Pins the account-activity breakdown feature: AccountSyncLog.channels_added is
len(new_channel_ids) for that run, and AccountSyncLog.channels_removed is the immediate
per-sync diff - channels the immediately preceding sync touched that this sync did not -
never the delayed channel_missing_after_days threshold _raise_channel_lifecycle_alerts
uses for its own alert. A first sync (no previous sync to diff against) gets
channels_removed == 0, a true zero rather than "not tracked".

Drives the real _do_sync path via FileXtreamClient against dump files, same harness as
test_account_sync_shrink.py. No network: dump dirs carry no xmltv.xml so the EPG branch
short-circuits.

  python3 tests/test_account_sync_added_removed.py
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
from app.database import AccountSyncLog, XtreamAccount  # noqa: E402
from tests.support import make_test_app  # noqa: E402

BASE_URL = 'http://provider.test:8080'
USERNAME = 'testuser'
PASSWORD = 'testpass'


def _stream(sid):
    return {
        'stream_id': sid,
        'name': f'Channel {sid}',
        'stream_icon': f'http://provider.test/logo{sid}.png',
        'category_id': '1',
        'epg_channel_id': f'ch{sid}.test',
    }


class SyncAddedRemovedTests(unittest.TestCase):
    def setUp(self):
        self.t = make_test_app()
        self.dump_base = os.path.join(self.t._tmpdir, 'xtream_dumps')

        self.account = XtreamAccount(
            name='Added Removed Test', base_url=BASE_URL,
            username=USERNAME, password=PASSWORD, status='OK',
            url_normalization=NORM_MPEGTS_LIVE)
        db.session.add(self.account)
        db.session.flush()
        self.dump_seq = 0

    def tearDown(self):
        self.t.cleanup()

    def _write_dump(self, stream_ids):
        self.dump_seq += 1
        d = os.path.join(self.dump_base, str(self.account.id), f'2026-01-01_{self.dump_seq:02d}')
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, 'auth.json'), 'w', encoding='utf-8') as f:
            json.dump({'user_info': {'auth': 1, 'status': 'Active'}}, f)
        with open(os.path.join(d, 'live_streams_json_api.json'), 'w', encoding='utf-8') as f:
            json.dump([_stream(sid) for sid in stream_ids], f)

    def _sync(self):
        cfg = _deep_merge(load_config(), {'debug': {'xtream_dump_dir': self.dump_base}})
        with mock.patch('app.accounts.load_config', return_value=cfg):
            _do_sync(self.account.id, threading.Event(), use_dump=True)
        db.session.expire_all()

    def _latest_log(self):
        return AccountSyncLog.query.filter_by(account_id=self.account.id).order_by(
            AccountSyncLog.id.desc()).first()

    def test_first_sync_has_no_removed_to_diff_against(self):
        self._write_dump([1, 2, 3, 4, 5])
        self._sync()

        log = self._latest_log()
        self.assertEqual(log.channels_added, 5)
        self.assertEqual(log.channels_removed, 0,
                          'no previous sync to diff against - a true zero, not "not tracked"')

    def test_second_sync_reports_the_actual_added_removed_diff(self):
        self._write_dump([1, 2, 3, 4, 5])
        self._sync()

        # 4 stays, 1/2/3/5 drop out, 6/7 are new.
        self._write_dump([4, 6, 7])
        self._sync()

        log = self._latest_log()
        self.assertEqual(log.channels_added, 2, 'channels 6 and 7 are new this sync')
        self.assertEqual(log.channels_removed, 4,
                          'channels 1, 2, 3, 5 were touched last sync but not this one')

    def test_removed_count_is_the_immediate_diff_not_cumulative(self):
        """A third sync's removed count must reflect only what changed since sync 2, not
        re-count channels that were already absent as of sync 2."""
        self._write_dump([1, 2, 3, 4, 5])
        self._sync()

        self._write_dump([4, 6, 7])
        self._sync()

        # Only channel 6 drops out this time - 1/2/3/5 were already gone as of sync 2 and
        # must not be re-counted as newly removed.
        self._write_dump([4, 7])
        self._sync()

        log = self._latest_log()
        self.assertEqual(log.channels_added, 0)
        self.assertEqual(log.channels_removed, 1,
                          'only channel 6 dropped out between sync 2 and sync 3')

    def test_unchanged_feed_reports_zero_added_and_zero_removed(self):
        self._write_dump([1, 2, 3])
        self._sync()

        self._write_dump([1, 2, 3])
        self._sync()

        log = self._latest_log()
        self.assertEqual(log.channels_added, 0)
        self.assertEqual(log.channels_removed, 0)


if __name__ == '__main__':
    unittest.main(verbosity=2)
