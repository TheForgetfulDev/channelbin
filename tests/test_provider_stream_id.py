"""channels.provider_stream_id - the provider's stream id out of the stream URL, stamped at
sync and kept beside url_normalizable (dev/changelog/1171, DESIGN-account-providers.md §7).

The duplicate fold keys on this id for accounts on a provider, so the column has to mean
exactly "the <numeric id> of the URL's user/pass/id triplet": NULL when there is no triplet
(a radio mount, a third-party feed, a provider .mp3), text as the provider wrote it, and
restamped whenever the URL moves.

A feature, not a defect fix, so there is no dev/docs/BUGS.md entry to cite. The migration
step's resume behavior is covered in tests/test_migration_backfill_resume.py::M087ResumeTests.

No network, no real ffmpeg - see CLAUDE.md §Testing.
Run standalone:
  python3 -m unittest tests.test_provider_stream_id
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import db  # noqa: E402
from app.accounts import _upsert_channels, url_provider_stream_id  # noqa: E402
from app.database import Channel, M3uAccount  # noqa: E402
from tests.support import make_test_app  # noqa: E402


class UrlProviderStreamIdTests(unittest.TestCase):
    """The pure helper - the definition the column, the sync and the migration all share."""

    def test_the_id_is_the_trailing_number_of_the_triplet(self):
        for url in ('http://edge.example.test/live/u/p/4471.ts',
                    'http://edge.example.test:8080/u/p/4471',
                    'https://edge.example.test/live/u/p/4471.m3u8'):
            self.assertEqual(url_provider_stream_id(url), '4471', url)

    def test_the_id_is_text_and_never_cast(self):
        self.assertEqual(url_provider_stream_id('http://edge.example.test/u/p/0042.ts'), '0042')

    def test_a_url_without_a_triplet_has_no_id(self):
        """The strict parse, not "any trailing number": these are the rows a looser key
        matched on the live database and the provider fold must leave on their URL."""
        for url in ('http://radio.example.test/stream',
                    'http://radio.example.test/u/p/114.mp3',
                    'http://cdn.example.org/static/1.ts',
                    'http',
                    ''):
            self.assertIsNone(url_provider_stream_id(url), url)


class _SyncCase(unittest.TestCase):
    def setUp(self):
        self.t = make_test_app()
        self.account = M3uAccount(name='Id Test', m3u_url='http://panel.example.test/get.php',
                                  status='OK')
        db.session.add(self.account)
        db.session.commit()

    def tearDown(self):
        self.t.cleanup()

    def _sync(self, streams):
        _upsert_channels(self.account, streams)
        db.session.commit()
        db.session.expire_all()

    def _ids(self):
        return {c.stream_id: c.provider_stream_id
                for c in Channel.query.filter_by(account_id=self.account.id)}


class UpsertStampTests(_SyncCase):

    def test_a_new_channel_is_stamped_and_a_mount_gets_null(self):
        self._sync([
            {'stream_id': 1, 'name': 'A', '_stream_url': 'http://edge.example.test/live/u/p/4471.ts'},
            {'stream_id': 2, 'name': 'B', '_stream_url': 'http://radio.example.test/u/p/9.mp3'},
        ])
        self.assertEqual(self._ids(), {1: '4471', 2: None})

    def test_the_id_and_url_normalizable_never_disagree(self):
        self._sync([
            {'stream_id': 1, 'name': 'A', '_stream_url': 'http://edge.example.test/u/p/5'},
            {'stream_id': 2, 'name': 'B', '_stream_url': 'http://radio.example.test/mount'},
        ])
        for ch in Channel.query.filter_by(account_id=self.account.id):
            self.assertEqual(ch.provider_stream_id is not None, ch.url_normalizable, ch.stream_id)

    def test_a_sync_restamps_an_id_that_moved(self):
        self._sync([{'stream_id': 1, 'name': 'A',
                     '_stream_url': 'http://edge.example.test/live/u/p/100.ts'}])
        self._sync([{'stream_id': 1, 'name': 'A',
                     '_stream_url': 'http://edge.example.test/live/u/p/200.ts'}])
        self.assertEqual(self._ids(), {1: '200'})

    def test_a_sync_clears_the_id_when_the_triplet_goes_away(self):
        self._sync([{'stream_id': 1, 'name': 'A',
                     '_stream_url': 'http://edge.example.test/live/u/p/100.ts'}])
        self._sync([{'stream_id': 1, 'name': 'A',
                     '_stream_url': 'http://proxy.example.test/watch?ch=100'}])
        self.assertEqual(self._ids(), {1: None})

    def test_a_row_missing_only_its_id_is_repaired_by_the_next_sync(self):
        """A row whose URL did not move still counts as changed when its id is wrong, so a
        sync heals a stale stamp rather than leaving it for the life of the row."""
        self._sync([{'stream_id': 1, 'name': 'A',
                     '_stream_url': 'http://edge.example.test/live/u/p/100.ts'}])
        Channel.query.filter_by(account_id=self.account.id).update(
            {'provider_stream_id': None})
        db.session.commit()
        self._sync([{'stream_id': 1, 'name': 'A',
                     '_stream_url': 'http://edge.example.test/live/u/p/100.ts'}])
        self.assertEqual(self._ids(), {1: '100'})


class RenormalizeStampTests(_SyncCase):
    """_renormalize_chunk is the one other writer of url_normalizable, so it keeps the id in
    lockstep with it."""

    def test_renormalize_stamps_a_missing_id(self):
        from app.routes.accounts import _renormalize_chunk
        self._sync([{'stream_id': 1, 'name': 'A',
                     '_stream_url': 'http://edge.example.test/live/u/p/100.ts'}])
        Channel.query.filter_by(account_id=self.account.id).update(
            {'provider_stream_id': None})
        db.session.commit()
        ids = [c.id for c in Channel.query.filter_by(account_id=self.account.id)]
        with self.t.app.test_request_context():
            _renormalize_chunk(ids, 'hls')
        db.session.expire_all()
        self.assertEqual(self._ids(), {1: '100'})


if __name__ == '__main__':
    unittest.main()
