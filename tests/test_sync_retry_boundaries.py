"""A lock retry on the channel upsert must not re-download the provider's channel list.

Guards dev/docs/BUGS.md 2026-08-16 @ 12:11:03 PM ET. Both account types wrapped their
provider fetch inside the `retry_on_locked` closure that upserts and commits the channels,
and `retry_on_locked` re-runs its whole decorated body. So one "database is locked" on that
commit re-downloaded a multi-MB M3U playlist (or re-issued three Xtream API calls) - up to
four extra times - against providers that almost universally allow one connection at a
time, potentially while a recording is live on that same account.

The injection here is the real mechanism, not an approximation: `retry_on_locked` is
replaced with one that fails the channel closure's FIRST commit with the exact
OperationalError SQLite raises, rolls the session back the way the real decorator does, and
then re-runs the closure. Whatever the sync chose to put inside that closure therefore runs
twice - which is precisely the thing under test - and the assertions count provider round
trips across the whole sync.

No network: the M3U case patches `requests.get`, the Xtream case syncs from a dump through
FileXtreamClient (CLAUDE.md §Testing).

Run standalone:
  python3 -m unittest tests.test_sync_retry_boundaries
"""
import functools
import json
import os
import sys
import threading
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy.exc import OperationalError  # noqa: E402

from app import db  # noqa: E402
from app import accounts as accounts_mod  # noqa: E402
from app.accounts import NORM_MPEGTS_LIVE, _do_sync  # noqa: E402
from app.config import _deep_merge, load_config  # noqa: E402
from app.database import Account, Channel, XtreamAccount  # noqa: E402
from app.xtream_client import FileXtreamClient  # noqa: E402
from tests.support import make_test_app  # noqa: E402

CHANNEL_COUNT = 6

M3U_URL = 'http://provider.test/playlist.m3u'
BASE_URL = 'http://provider.test:8080'


def _locked_commit(*_args, **_kwargs):
    raise OperationalError('COMMIT', {}, Exception('database is locked'))


def one_lock_retry_on_the_channel_closure():
    """Patch `retry_on_locked` so the channel-upsert closure loses its first commit.

    Matches on the closure's own name, so the other decorated closures in a sync
    (`_mark_syncing_and_commit`, `_mark_success_and_commit`, ...) keep their real behavior
    and run exactly once. The name test covers the pre-fix names too, so this fixture
    still injects when the production change is reverted.
    """
    real_retry = accounts_mod.retry_on_locked

    def patched_retry(*d_args, **d_kwargs):
        decorator = real_retry(*d_args, **d_kwargs)

        def wrap(func):
            wrapped = decorator(func)
            if 'channels_and_commit' not in func.__name__:
                return wrapped
            fired = []

            @functools.wraps(func)
            def run(*args, **kwargs):
                if not fired:
                    fired.append(True)
                    with mock.patch.object(db.session, 'commit', _locked_commit):
                        try:
                            func(*args, **kwargs)
                        except OperationalError:
                            pass
                    db.session.rollback()
                return wrapped(*args, **kwargs)

            return run

        return wrap

    return mock.patch.object(accounts_mod, 'retry_on_locked', patched_retry)


class M3uFetchIsOutsideTheRetryTests(unittest.TestCase):
    """The playlist download is the expensive, connection-consuming half."""

    def setUp(self):
        self.t = make_test_app()
        # No epg_url on purpose: it would be a second requests.get and this test counts
        # them. The EPG fetch has always sat outside any retry closure.
        self.account = Account(name='M3U Provider', account_type='m3u',
                               m3u_url=M3U_URL, status='OK')
        db.session.add(self.account)
        db.session.commit()
        self.account_id = self.account.id

    def tearDown(self):
        self.t.cleanup()

    def _playlist(self):
        lines = ['#EXTM3U']
        for sid in range(1, CHANNEL_COUNT + 1):
            lines.append(f'#EXTINF:-1 tvg-id="ch{sid}.test" tvg-name="Channel {sid}",Channel {sid}')
            lines.append(f'http://provider.test/live/u/p/{sid}.ts')
        return ('\n'.join(lines) + '\n').encode()

    def _sync_with_one_lock_retry(self):
        body = self._playlist()
        fetches = []

        def fake_get(url, **_kwargs):
            fetches.append(url)
            resp = mock.Mock()
            resp.content = body
            resp.raise_for_status.return_value = None
            resp.status_code = 200
            return resp

        with mock.patch.object(accounts_mod.requests, 'get', fake_get):
            with one_lock_retry_on_the_channel_closure():
                _do_sync(self.account_id, threading.Event())
        db.session.expire_all()
        return fetches

    def test_playlist_is_downloaded_once_despite_the_retry(self):
        fetches = self._sync_with_one_lock_retry()
        self.assertEqual(len(fetches), 1,
                         f'the playlist must be fetched once per sync, got {fetches}')

    def test_the_retry_still_lands_every_channel(self):
        """The point of narrowing the closure is that the DB tail still retries."""
        self._sync_with_one_lock_retry()
        channels = Channel.query.filter_by(account_id=self.account_id).all()
        self.assertEqual(len(channels), CHANNEL_COUNT)
        self.assertEqual(sorted(int(c.stream_id) for c in channels),
                         list(range(1, CHANNEL_COUNT + 1)))

    def test_the_retry_does_not_duplicate_channels(self):
        """CONTROL: re-running the upsert must not create a second copy of each row."""
        self._sync_with_one_lock_retry()
        self.assertEqual(Channel.query.filter_by(account_id=self.account_id).count(),
                         CHANNEL_COUNT)

    def test_the_account_finishes_the_sync_successfully(self):
        """CONTROL: a lost commit that the retry recovers from is not a failed sync."""
        self._sync_with_one_lock_retry()
        account = db.session.get(Account, self.account_id)
        self.assertEqual(account.status, 'OK')
        self.assertEqual(account.channel_count, CHANNEL_COUNT)


class XtreamFetchIsOutsideTheRetryTests(unittest.TestCase):
    """Three provider API calls, on accounts that report max_connections: 1."""

    def setUp(self):
        self.t = make_test_app()
        self.dump_base = os.path.join(self.t._tmpdir, 'xtream_dumps')
        self.account = XtreamAccount(name='Xtream Provider', base_url=BASE_URL,
                                     username='u', password='p', status='OK',
                                     url_normalization=NORM_MPEGTS_LIVE)
        db.session.add(self.account)
        db.session.commit()
        self.account_id = self.account.id
        self._write_dump()

    def tearDown(self):
        self.t.cleanup()

    def _write_dump(self):
        d = os.path.join(self.dump_base, str(self.account_id), '2026-01-01_01')
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, 'auth.json'), 'w', encoding='utf-8') as f:
            json.dump({'user_info': {'auth': 1, 'status': 'Active'}}, f)
        with open(os.path.join(d, 'live_streams_json_api.json'), 'w', encoding='utf-8') as f:
            json.dump([{'stream_id': sid, 'name': f'Channel {sid}', 'category_id': '1',
                        'epg_channel_id': f'ch{sid}.test'}
                       for sid in range(1, CHANNEL_COUNT + 1)], f)
        # No xmltv.xml on purpose - keeps the EPG branch out of this sync entirely.

    def _sync_with_one_lock_retry(self):
        cfg = _deep_merge(load_config(), {'debug': {'xtream_dump_dir': self.dump_base}})
        real_streams = FileXtreamClient.get_live_streams
        real_ids = FileXtreamClient.get_live_stream_ids
        calls = []

        def counting_streams(client):
            calls.append('get_live_streams')
            return real_streams(client)

        def counting_ids(client):
            calls.append('get_live_stream_ids')
            return real_ids(client)

        with mock.patch.object(FileXtreamClient, 'get_live_streams', counting_streams), \
                mock.patch.object(FileXtreamClient, 'get_live_stream_ids', counting_ids), \
                mock.patch.object(accounts_mod, 'load_config', return_value=cfg):
            with one_lock_retry_on_the_channel_closure():
                _do_sync(self.account_id, threading.Event(), use_dump=True)
        db.session.expire_all()
        return calls

    def test_the_catalog_is_asked_once_despite_the_retry(self):
        calls = self._sync_with_one_lock_retry()
        self.assertEqual(calls.count('get_live_streams'), 1,
                         f'one channel-list fetch per sync, got {calls}')
        self.assertEqual(calls.count('get_live_stream_ids'), 1,
                         f'one classification fetch per sync, got {calls}')

    def test_the_retry_still_lands_every_channel(self):
        self._sync_with_one_lock_retry()
        self.assertEqual(Channel.query.filter_by(account_id=self.account_id).count(),
                         CHANNEL_COUNT)

    def test_the_account_finishes_the_sync_successfully(self):
        """CONTROL: see the M3U case."""
        self._sync_with_one_lock_retry()
        account = db.session.get(Account, self.account_id)
        self.assertEqual(account.status, 'OK')
        self.assertEqual(account.channel_count, CHANNEL_COUNT)


if __name__ == '__main__':
    unittest.main()
