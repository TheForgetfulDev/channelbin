"""The search-index rebuild must survive a sync that fails after the channel upserts commit.

`_do_sync` commits channel upserts BEFORE fetching EPG, deliberately, so the write lock is not
held across a multi-second network fetch. That makes "channels committed, sync then died" an
ordinary outcome rather than an edge case - a provider timeout during the EPG fetch is the most
common way a sync fails at all. Before dev/changelog/365 the rebuild sat on the success path
only, so those committed channels were absent from an index whose state row still read OK, and
a renamed channel went on matching its old name until the next successful sync.

Two behaviors are pinned here:
  1. A sync that raises after the channel commit still rebuilds (the `finally`).
  2. A CANCELLED sync deliberately does not - cancel means stop now, and the watermark check
     in search_index_readiness() keeps that case correct by sending search back to LIKE.

No network: `requests.get` is patched at `app.accounts.requests.get`, and the EPG stage is
patched to raise. Runs against a throwaway temp SQLite DB - never the live dvr.db.
"""
import os
import sys
import threading
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import db  # noqa: E402
from app.accounts import _do_sync  # noqa: E402
from app.database import AccountSyncLog, Channel, M3uAccount  # noqa: E402
from app.search_index import (SEARCH_INDEX_CHANNELS, apply_channel_search,  # noqa: E402
                              rebuild_search_indexes, search_index_readiness)
from tests.support import make_test_app  # noqa: E402

M3U_URL = 'http://provider.test/playlist.m3u8'

M3U_PLAYLIST = (
    '#EXTM3U\n'
    '#EXTINF:-1 tvg-id="ch1.test",Sports Channel One\n'
    'http://provider.test/stream1.ts\n'
    '#EXTINF:-1 tvg-id="ch2.test",News Channel Two\n'
    'http://provider.test/stream2.ts\n'
)


def _fake_get(url, **kwargs):
    if url == M3U_URL:
        resp = mock.Mock()
        resp.raise_for_status = mock.Mock()
        resp.content = M3U_PLAYLIST.encode('utf-8')
        return resp
    raise AssertionError(f'unexpected requests.get call: {url}')


class SyncRebuildHookTests(unittest.TestCase):

    def setUp(self):
        self.t = make_test_app()
        self.account = M3uAccount(name='Failing Provider', m3u_url=M3U_URL,
                                  epg_url=None, status='OK')
        db.session.add(self.account)
        db.session.commit()
        self.account_id = self.account.id

    def tearDown(self):
        self.t.cleanup()

    def _searchable(self, q):
        readiness = search_index_readiness(SEARCH_INDEX_CHANNELS)
        return readiness, sorted(c.name for c in apply_channel_search(
            Channel.query, q, readiness=readiness))

    def test_sync_failing_at_the_epg_stage_still_rebuilds(self):
        """The realistic shape: channels land, the EPG stage blows up, sync ends ERROR."""
        with mock.patch('app.accounts.requests.get', side_effect=_fake_get), \
             mock.patch('app.accounts._sync_epg_from_url',
                        side_effect=RuntimeError('provider went away')):
            self.account.epg_url = 'http://provider.test/xmltv.php'
            db.session.commit()
            _do_sync(self.account_id, threading.Event())

        db.session.expire_all()
        log_row = AccountSyncLog.query.filter_by(account_id=self.account_id).first()
        self.assertEqual(log_row.status, 'ERROR', 'precondition: this sync must have failed')
        self.assertEqual(Channel.query.count(), 2, 'precondition: channels committed anyway')

        readiness, found = self._searchable('Sports')
        self.assertTrue(readiness[0],
                        f'a failed sync must still leave a usable index: {readiness[1]}')
        self.assertEqual(found, ['Sports Channel One'])

    def test_renamed_channel_does_not_survive_a_failed_sync_in_the_index(self):
        """The wrong-results half. The first sync indexes 'Sports Channel One'; the provider
        renames it and the next sync dies at EPG. Searching the OLD name must not match."""
        with mock.patch('app.accounts.requests.get', side_effect=_fake_get):
            _do_sync(self.account_id, threading.Event())
        db.session.expire_all()
        self.assertEqual(self._searchable('Sports')[1], ['Sports Channel One'])

        renamed = M3U_PLAYLIST.replace('Sports Channel One', 'Athletics Channel One')

        def _renamed_get(url, **kwargs):
            resp = mock.Mock()
            resp.raise_for_status = mock.Mock()
            resp.content = renamed.encode('utf-8')
            return resp

        with mock.patch('app.accounts.requests.get', side_effect=_renamed_get), \
             mock.patch('app.accounts._sync_epg_from_url',
                        side_effect=RuntimeError('provider went away')):
            self.account.epg_url = 'http://provider.test/xmltv.php'
            db.session.commit()
            _do_sync(self.account_id, threading.Event())

        db.session.expire_all()
        self.assertEqual(self._searchable('Sports')[1], [])
        self.assertEqual(self._searchable('Athletics')[1], ['Athletics Channel One'])

    def test_cancelled_sync_skips_the_rebuild_but_stays_correct(self):
        """Cancel must not answer with ~12s of held write lock. The index goes stale, and the
        watermark check is what keeps the results right - LIKE finds the new channel anyway.

        The cancel is fired from the EPG fetch rather than the playlist fetch because a sync
        now stops inside a phase as well as between them (dev/changelog/720): cancelling at
        the playlist fetch aborts before the channel upsert commits, which leaves no
        committed channels for this test to be about.
        """
        rebuild_search_indexes('baseline')          # something to go stale
        stop = threading.Event()
        epg_url = 'http://provider.test/xmltv.php'
        self.account.epg_url = epg_url
        db.session.commit()

        def _get_then_cancel(url, **kwargs):
            if url == epg_url:
                stop.set()                          # cancel lands after the channel commit
                resp = mock.Mock()
                resp.raise_for_status = mock.Mock()
                resp.content = b'<?xml version="1.0" encoding="UTF-8"?><tv></tv>'
                return resp
            return _fake_get(url, **kwargs)

        with mock.patch('app.accounts.requests.get', side_effect=_get_then_cancel):
            _do_sync(self.account_id, stop)

        db.session.expire_all()
        # The sync's own terminal row is asserted BEFORE the channel count, because it is
        # the only thing that says which phase stopped. This precondition failed once under
        # a sharded run with a bare `0 != 2` (dev/test-logs/20260818-001153/shard2.log) and
        # left nothing to tell "cancelled before the upsert" from "the upsert's commit
        # errored" - two different defects with the same symptom. It has not reproduced
        # since; when it recurs, this names the phase.
        log_row = AccountSyncLog.query.filter_by(account_id=self.account_id).first()
        self.assertEqual(log_row.status, 'CANCELLED',
                         f'sync ended {log_row.status}, not cancelled: {log_row.error_message}')
        self.assertIn('the channel list was updated', log_row.error_message or '',
                      'the cancel must have landed AFTER the channel upsert committed - a '
                      'cancel that saved nothing means it landed in an earlier phase')
        self.assertEqual(Channel.query.count(), 2, 'precondition: channels committed')
        readiness, found = self._searchable('Sports')
        self.assertFalse(readiness[0], 'a cancelled sync leaves the index stale by design')
        self.assertIn('stale', readiness[1])
        self.assertEqual(found, ['Sports Channel One'],
                         'stale index must not cost correctness - LIKE answers it')


if __name__ == '__main__':
    unittest.main()
