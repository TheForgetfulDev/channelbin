"""Each sync records the entries it skipped on its own AccountSyncLog row (dev/changelog/926):
stream URLs with no "://" (skipped_malformed_urls) and stream_ids already seen earlier in the
same sync (skipped_duplicate_stream_ids). Before, the counts existed only inside the
MALFORMED_CHANNEL_URLS / DUPLICATE_STREAM_IDS_SKIPPED alert titles, so the account page had
nothing to show and the alerts could not stop without the numbers disappearing.

End-to-end through the real _do_sync (M3U path, mocked requests.get), so what is proven is
that the counts reach the row, not the counter in isolation - that half is
tests/test_duplicate_stream_id_skip.py.

No network, no real ffmpeg - see CLAUDE.md §Testing.
Run standalone:
  python3 -m unittest tests.test_account_sync_skip_counts
"""
import os
import sys
import threading
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import db  # noqa: E402
from app.accounts import _do_sync  # noqa: E402
from app.database import AccountSyncLog, M3uAccount  # noqa: E402
from tests.support import make_test_app  # noqa: E402

M3U_URL = 'http://provider.test/playlist.m3u8?user=realuser&pass=realpass'


class SyncLogSkipCountTests(unittest.TestCase):
    def setUp(self):
        self.t = make_test_app()
        self.account = M3uAccount(name='Skip Count Test', m3u_url=M3U_URL, status='OK')
        db.session.add(self.account)
        db.session.commit()

    def tearDown(self):
        self.t.cleanup()

    def _sync(self, playlist_body):
        # duplicated from tests/test_duplicate_stream_id_skip.py - a per-file test harness
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

    def _latest_log(self):
        return AccountSyncLog.query.filter_by(account_id=self.account.id).order_by(
            AccountSyncLog.id.desc()).first()

    def test_skipped_entries_are_counted_on_the_sync_log(self):
        # 123.ts and 123.m3u8 collide on stream_id 123 (the real collision shape from
        # dev/docs/BUGS.md 2026-08-30); "http" and "https" are the placeholder rows a real
        # provider lists with no usable URL, and each gets its own hashed stream_id, so both
        # reach the malformed check rather than colliding with each other.
        playlist = (
            '#EXTM3U\n'
            '#EXTINF:-1 tvg-id="a.test",Channel A\n'
            'http://provider.test/u/p/123.ts\n'
            '#EXTINF:-1 tvg-id="b.test",Channel B\n'
            'http://provider.test/u/p/123.m3u8\n'
            '#EXTINF:-1 tvg-id="c.test",Placeholder C\n'
            'http\n'
            '#EXTINF:-1 tvg-id="d.test",Placeholder D\n'
            'https\n'
        )
        self._sync(playlist)

        log = self._latest_log()
        self.assertEqual(log.status, 'SUCCESS')
        self.assertEqual(log.skipped_malformed_urls, 2)
        self.assertEqual(log.skipped_duplicate_stream_ids, 1)

    def test_a_clean_sync_records_a_true_zero_not_untracked(self):
        """Product Principle 1: NULL means "not tracked", so a sync that ran with the counter
        in place and skipped nothing must store 0, or the page would call it untracked."""
        playlist = (
            '#EXTM3U\n'
            '#EXTINF:-1 tvg-id="a.test",Channel A\n'
            'http://provider.test/u/p/1.ts\n'
        )
        self._sync(playlist)

        log = self._latest_log()
        self.assertEqual(log.skipped_malformed_urls, 0)
        self.assertEqual(log.skipped_duplicate_stream_ids, 0)


if __name__ == '__main__':
    unittest.main(verbosity=2)
