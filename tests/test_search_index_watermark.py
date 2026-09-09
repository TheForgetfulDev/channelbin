"""The channels search index goes stale only when text it actually indexes changes.

Guards dev/changelog/674. The watermark used to read `MAX(id)` + `MAX(last_seen_at)`, and
`last_seen_at` is stamped on every matched row of every sync whether the provider changed
anything or not - so a sync whose feed came back byte-identical declared the index stale,
sent every search to the LIKE fallback, and forced a full rebuild. The window that opened
was not the rebuild's ~9s: channel upserts commit early on purpose (to release the write
lock before the EPG fetch) while the rebuild only runs in the sync's `finally`, so it
spanned the whole EPG import phase, roughly 24 times a day.

`channels.search_text_updated_at` replaces it, and the point of a separate column is that
it moves for strictly less than `updated_at` does. ch_fts indexes exactly four columns
(name, stream_url, epg_channel_id, category_name); a health score, an `in_guide` toggle or
a provider logo swap changes the row but not a token in the index, and must not cost a
rebuild - the more so because nothing repairs staleness except a rebuild, and rebuilds only
fire from a sync, so a false stale at 3am degrades search for hours.

Covers:
  - FreshnessTests: what does and does not stale the index, through the real
    `_upsert_channels` and a real rebuild.
  - RenormalizeStampTests: the one writer of an indexed column outside the sync path.
  - WatermarkShapeTests: the stamp is indexed (an unindexed MAX is a full scan on every
    request), and the watermark reads the stamp rather than the liveness column.
  - MigrationTests: _m038 adds the column/index and re-records an already-fresh index's
    stored watermark in the new format, but leaves an already-stale one alone.

No network, no real ffmpeg - see CLAUDE.md §Testing.
Run standalone:
  python3 -m unittest tests.test_search_index_watermark
"""
import os
import sqlite3
import sys
import unittest
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import text  # noqa: E402

from app import db  # noqa: E402
from app import migrations as M  # noqa: E402
from app import search_index as SI  # noqa: E402
from app.accounts import _upsert_channels  # noqa: E402
from app.database import Channel, M3uAccount  # noqa: E402
from tests.support import make_test_app  # noqa: E402

M3U_URL = 'http://provider.test/playlist.m3u8?user=realuser&pass=realpass'


def _stream(sid, **overrides):
    """One M3U-sourced stream dict - `_stream_url` present, so no URL construction."""
    base = {
        'stream_id': sid,
        'name': f'Ch{sid}',
        '_stream_url': f'http://provider.test/live/u/p/{sid}.ts',
        'stream_icon': f'http://provider.test/logo{sid}.png',
        'category_id': '7',
        'category_name': 'Sports',
        'epg_channel_id': f'ch{sid}.test',
    }
    base.update(overrides)
    return base


class _WatermarkCase(unittest.TestCase):
    def setUp(self):
        self.t = make_test_app()
        self.account = M3uAccount(name='Watermark', m3u_url=M3U_URL, status='OK')
        db.session.add(self.account)
        db.session.commit()
        self.streams = [_stream(i) for i in range(1, 4)]
        self._sync(self.streams)
        SI.rebuild_search_indexes('test')

    def tearDown(self):
        self.t.cleanup()

    def _sync(self, streams):
        result = _upsert_channels(self.account, streams)
        db.session.commit()
        db.session.expire_all()
        return result

    def _channels_ready(self):
        ready, _reason = SI.search_index_readiness(SI.SEARCH_INDEX_CHANNELS)
        return ready


class FreshnessTests(_WatermarkCase):
    def test_rebuild_leaves_the_index_fresh(self):
        """Control: the setUp rebuild is what every other test here measures against."""
        self.assertTrue(self._channels_ready())

    def test_identical_resync_does_not_stale_the_index(self):
        """The headline. A feed that came back byte-identical changed nothing the index
        contains, so it must not open a degraded window or force a rebuild."""
        self._sync(self.streams)
        self.assertTrue(self._channels_ready())

    def test_identical_resync_still_advances_last_seen_at(self):
        """The liveness stamp still moves - it feeds the channel-lifecycle alerts. This is
        the pairing that broke the old watermark, so it is worth pinning that keeping it
        no longer drags the index's freshness down with it."""
        before = self._channels_ready()
        stamped = datetime.utcnow()
        self._sync(self.streams)
        row = Channel.query.filter_by(account_id=self.account.id, stream_id=1).first()
        self.assertGreaterEqual(row.last_seen_at, stamped)
        self.assertTrue(before and self._channels_ready())

    def test_a_rename_stales_the_index(self):
        """The case the watermark exists for: ch_fts still matches the OLD name until it is
        rebuilt, so a rename must be caught."""
        self._sync([_stream(1, name='Renamed Sports HD')] + self.streams[1:])
        ready, reason = SI.search_index_readiness(SI.SEARCH_INDEX_CHANNELS)
        self.assertFalse(ready)
        self.assertIn('stale', reason)

    def test_a_stream_url_change_stales_the_index(self):
        self._sync([_stream(1, _stream_url='http://provider.test/live/u/p/9001.ts')]
                   + self.streams[1:])
        self.assertFalse(self._channels_ready())

    def test_an_epg_channel_id_change_stales_the_index(self):
        self._sync([_stream(1, epg_channel_id='renamed.test')] + self.streams[1:])
        self.assertFalse(self._channels_ready())

    def test_a_category_name_change_stales_the_index(self):
        self._sync([_stream(1, category_name='News')] + self.streams[1:])
        self.assertFalse(self._channels_ready())

    def test_a_logo_change_does_not_stale_the_index(self):
        """A real provider change that dirties the row - updated_at moves - but touches no
        column ch_fts indexes. Keying the watermark off updated_at would rebuild here."""
        self._sync([_stream(1, stream_icon='http://provider.test/new-logo.png')]
                   + self.streams[1:])
        row = Channel.query.filter_by(account_id=self.account.id, stream_id=1).first()
        self.assertEqual(row.logo_url, 'http://provider.test/new-logo.png')
        self.assertTrue(self._channels_ready())

    def test_a_category_id_change_does_not_stale_the_index(self):
        self._sync([_stream(1, category_id='42')] + self.streams[1:])
        self.assertTrue(self._channels_ready())

    def test_a_health_score_write_does_not_stale_the_index(self):
        """Every channel test writes a health score. Under a watermark keyed on updated_at
        this would degrade search until the next sync's rebuild, hours later - there is no
        janitor that repairs staleness on its own."""
        row = Channel.query.filter_by(account_id=self.account.id, stream_id=1).first()
        row.health_score = 42.5
        row.consecutive_test_failures = 3
        db.session.commit()
        db.session.expire_all()
        self.assertTrue(self._channels_ready())

    def test_an_in_guide_toggle_does_not_stale_the_index(self):
        row = Channel.query.filter_by(account_id=self.account.id, stream_id=1).first()
        row.in_guide = True
        db.session.commit()
        db.session.expire_all()
        self.assertTrue(self._channels_ready())

    def test_a_new_channel_stales_the_index(self):
        """Caught by MAX(id) rather than the stamp, which is why both halves are kept."""
        self._sync(self.streams + [_stream(99)])
        self.assertFalse(self._channels_ready())


class RenormalizeStampTests(_WatermarkCase):
    """`_renormalize_chunk` rewrites stream_url outside the sync path (the URL Normalization
    control in the edit-account modal), so it is the one other writer that has to stamp."""

    def test_a_url_rewrite_stales_the_index(self):
        from app.routes.accounts import _renormalize_chunk
        ids = [c.id for c in Channel.query.filter_by(account_id=self.account.id).all()]
        with self.t.app.test_request_context():
            changed = _renormalize_chunk(ids, 'hls')
        db.session.expire_all()
        self.assertTrue(changed, 'the mode change should have rewritten at least one URL')
        self.assertFalse(self._channels_ready())

    def test_a_rewrite_that_changes_nothing_leaves_the_index_fresh(self):
        """Re-running the mode the URLs already carry writes no rows at all, so there is
        nothing to stamp and nothing to rebuild."""
        from app.routes.accounts import _renormalize_chunk
        ids = [c.id for c in Channel.query.filter_by(account_id=self.account.id).all()]
        with self.t.app.test_request_context():
            _renormalize_chunk(ids, 'hls')
        SI.rebuild_search_indexes('test')
        with self.t.app.test_request_context():
            changed = _renormalize_chunk(ids, 'hls')
        db.session.expire_all()
        self.assertEqual(changed, 0)
        self.assertTrue(self._channels_ready())


class WatermarkShapeTests(_WatermarkCase):
    def test_the_channels_watermark_reads_the_stamp_not_the_liveness_column(self):
        sql = ' '.join(SI._SOURCE_WATERMARK_SQL[SI.SEARCH_INDEX_CHANNELS])
        self.assertIn('search_text_updated_at', sql)
        self.assertNotIn('last_seen_at', sql)

    def test_the_stamp_is_indexed(self):
        """An unindexed MAX() is a full table scan, and this one runs once per request -
        measured at 87ms against 138k channels on the production database. The index has to
        exist on a create_all() database too, not only on a migrated one (the defect class
        _m024's five facet indexes fell into)."""
        names = {r[0] for r in db.session.execute(text(
            "SELECT name FROM sqlite_master WHERE type='index' AND tbl_name='channels'"
        )).fetchall()}
        self.assertIn('ix_channels_search_text_updated_at', names)

    def test_the_stamped_column_set_matches_what_ch_fts_declares(self):
        """The one drift in this design a test can actually catch. Which columns stamp the
        watermark is a hand-maintained set in app/accounts.py, and it is only correct while it
        equals the ch_fts column list - add a column to the index without adding it here and
        changes to it are silently missing from search until something else stales the index.
        (That a *new writer* might forget to stamp at all stays a discipline, documented on
        Channel.search_text_updated_at, the same way retry_on_locked is.)"""
        from app.accounts import _SEARCH_TEXT_COLUMNS
        ddl = next(s for s in SI.SEARCH_INDEX_DDL if 'ch_fts' in s)
        body = ddl.split('fts5(', 1)[1].rsplit(')', 1)[0]
        declared = {part.strip() for part in body.split(',')}
        declared = {d for d in declared if d and '=' not in d}
        self.assertEqual(declared, set(_SEARCH_TEXT_COLUMNS))

    def test_the_stamp_max_plans_as_a_seek(self):
        plan = ' '.join(str(r[3]) for r in db.session.execute(text(
            'EXPLAIN QUERY PLAN SELECT MAX(search_text_updated_at) FROM channels'
        )).fetchall())
        self.assertNotIn('SCAN', plan.upper())


class MigrationTests(unittest.TestCase):
    """_m038 against a raw sqlite3 database, the way the runner calls it."""

    def setUp(self):
        self.t = make_test_app()
        self.path = os.path.join(self.t._tmpdir, 'm038.db')
        self.conn = sqlite3.connect(self.path)
        self.cur = self.conn.cursor()
        self.cur.execute('CREATE TABLE channels (id INTEGER PRIMARY KEY, '
                         'last_seen_at DATETIME)')
        self.cur.execute('CREATE TABLE search_index_state (id INTEGER PRIMARY KEY, '
                         'name TEXT, source_watermark TEXT)')
        self.cur.executemany('INSERT INTO channels (id, last_seen_at) VALUES (?, ?)',
                             [(1, '2026-08-15 21:50:58.448682'),
                              (2, '2026-08-15 21:50:58.448682')])
        self.conn.commit()

    def tearDown(self):
        self.conn.close()
        self.t.cleanup()

    def _watermark(self):
        return self.cur.execute(
            "SELECT source_watermark FROM search_index_state WHERE name='channels'"
        ).fetchone()[0]

    def _run(self):
        M._m038_channel_search_text_watermark(self.conn, self.cur)
        self.conn.commit()

    def test_adds_the_column_and_the_index(self):
        self._run()
        cols = {r[1] for r in self.cur.execute('PRAGMA table_info(channels)').fetchall()}
        self.assertIn('search_text_updated_at', cols)
        names = {r[0] for r in self.cur.execute(
            "SELECT name FROM sqlite_master WHERE type='index' AND tbl_name='channels'"
        ).fetchall()}
        self.assertIn('ix_channels_search_text_updated_at', names)

    def test_is_idempotent(self):
        self._run()
        self._run()
        cols = [r[1] for r in self.cur.execute('PRAGMA table_info(channels)').fetchall()]
        self.assertEqual(cols.count('search_text_updated_at'), 1)

    def test_leaves_no_rows_backfilled(self):
        """NULL is the intended state - it reads as "nothing indexed has changed since the
        column existed", and skipping the backfill keeps this step out of the
        interrupted-backfill class where a retry silently skips the data half."""
        self._run()
        self.assertEqual(
            self.cur.execute('SELECT COUNT(*) FROM channels '
                             'WHERE search_text_updated_at IS NOT NULL').fetchone()[0], 0)

    def test_rewrites_the_watermark_of_an_index_that_was_fresh(self):
        """Otherwise every existing install reads stale the instant it starts and pays a
        full rebuild for a format change that moved no data."""
        self.cur.execute("INSERT INTO search_index_state (name, source_watermark) "
                         "VALUES ('channels', '2/2026-08-15 21:50:58.448682')")
        self.conn.commit()
        self._run()
        self.assertEqual(self._watermark(), '2/')

    def test_leaves_the_watermark_of_an_index_that_was_already_stale(self):
        """A stale index must still read stale afterwards - the migration is not allowed to
        declare an index fresh that was not."""
        self.cur.execute("INSERT INTO search_index_state (name, source_watermark) "
                         "VALUES ('channels', '1/2026-08-14 09:00:00.000000')")
        self.conn.commit()
        self._run()
        self.assertEqual(self._watermark(), '1/2026-08-14 09:00:00.000000')

    def test_survives_a_database_with_no_search_index_state_table(self):
        self.cur.execute('DROP TABLE search_index_state')
        self.conn.commit()
        self._run()
        cols = {r[1] for r in self.cur.execute('PRAGMA table_info(channels)').fetchall()}
        self.assertIn('search_text_updated_at', cols)


if __name__ == '__main__':
    unittest.main()
