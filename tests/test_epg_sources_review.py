"""Tier 2 - the EPG sources review's findings (dev/changelog/1181, fixed in
dev/changelog/1298). Each class pins one of them:

  - ImportGuardTests: the import keeps a channel on its stored winner when the directory
    ranks a source first that holds none of the channel's rows yet (a key saved as "waits"),
    as the other two resolvers already did; that source's own import then moves it.
  - EmptyMapTests: an import with no keyed channel still clears what the source held, so a
    cleared last key does not leave a frozen schedule under an OK badge.
  - CrashGapTests: a channel whose rows moved but whose pointer did not (a crash between
    the two commits) is repaired by the next re-resolve, because the held check reads both
    tables.
  - OrphanAlternatesTests: a channel left with no winner keeps no alternates (§4), from the
    import and from a re-resolve.
  - IndexTests: epg_alternate_entries is reached by channel and channels by source through
    an index, on a fresh build and after migration 102.
  - InterruptedRefreshTests: a refresh the restart cut off is written as cut short, not
    left saying OK.
  - RefreshGuardRouteTests: every user action that re-resolves channels or moves rows
    answers 409 while any source the account reads is refreshing - one helper, one wording.

No network: every feed is a local byte string (CLAUDE.md §Testing).
"""
import os
import sqlite3
import sys
import tempfile
import unittest
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import db  # noqa: E402
from app import migrations as M  # noqa: E402
from app.accounts import import_source  # noqa: E402
from app.database import (Alert, Channel, ChannelEvent, EpgSource, EpgSourceChannel,  # noqa: E402
                          CHANNEL_EPG_SOURCE_CHANGED, EPG_STATUS_OK, EPG_STATUS_TRUNCATED)
from app.epg_sources import (KEY_NO_KEY, KEY_WAITS, demote, move_source, promote,  # noqa: E402
                             reresolve_channels, set_channel_key, set_source_override)
from tests.support import make_test_app, seed  # noqa: E402
from tests.support.seed import make_epg_source  # noqa: E402
from tests.test_epg_sources import CFG, REAL, _Fixture, _schedule, _xmltv  # noqa: E402

OTHER = ['Other News', 'Other Film', 'Other Sport']
NO_GUARD = {'sync': {'epg_collapse_threshold_percent': 0}}


def _set_key(ch, source, key):
    change = set_channel_key(db.session.get(Channel, ch.id), db.session.get(EpgSource, source.id),
                             key, case_sensitive=False)
    db.session.commit()
    db.session.expire_all()
    return change


def _source_events(ch):
    return ChannelEvent.query.filter_by(channel_id=ch.id,
                                        event_type=CHANNEL_EPG_SOURCE_CHANGED).all()


class ImportGuardTests(_Fixture):
    """dev/docs/BUGS.md 2026-10-26: `_import_source()` pointed a channel at a source holding
    none of its rows; reresolve_channels() and set_source_override() never did."""

    def setUp(self):
        super().setUp()
        # self.src is first in priority and lists `p-c.test` with a real schedule that no
        # channel carries; `ext` is second and lists the channel's own id.
        self.ch = self._channel('Alpha C', 'c.test')
        self.ext = self._second_source()
        import_source(self.src, _xmltv(_schedule('p-c.test', REAL) + _schedule('other.test', REAL)),
                      epg_days=3, cfg=CFG)
        import_source(self.ext, _xmltv(_schedule('c.test', OTHER)), epg_days=3, cfg=CFG)
        self.assertEqual(self._winner(self.ch), self.ext.id)
        change = _set_key(self.ch, self.src, 'p-c.test')
        self.assertEqual(change.outcome, KEY_WAITS)
        self.assertEqual(self._winner(self.ch), self.ext.id, 'the key waits, as designed')

    def test_a_sibling_import_leaves_the_channel_on_the_source_that_holds_its_rows(self):
        import_source(db.session.get(EpgSource, self.ext.id), _xmltv(_schedule('c.test', OTHER)),
                      epg_days=3, cfg=CFG)
        self.assertEqual(self._winner(self.ch), self.ext.id)
        self.assertEqual({t for _s, t in self._active(self.ch)}, set(OTHER),
                         'the guide must not go dark waiting for the other source')
        self.assertEqual(self._alternates(self.ch), [])
        self.assertEqual(_source_events(self.ch), [])
        src = db.session.get(EpgSource, self.src.id)
        self.assertEqual(src.active_channel_count, 0)

    def test_the_waiting_sources_own_import_then_moves_it(self):
        import_source(db.session.get(EpgSource, self.src.id),
                      _xmltv(_schedule('p-c.test', REAL) + _schedule('other.test', REAL)),
                      epg_days=3, cfg=CFG)
        self.assertEqual(self._winner(self.ch), self.src.id)
        self.assertEqual({t for _s, t in self._active(self.ch)}, set(REAL))
        self.assertEqual({s for s, _t in self._alternates(self.ch)}, {self.ext.id})
        self.assertEqual(len(_source_events(self.ch)), 1)


class EmptyMapTests(_Fixture):
    """dev/docs/BUGS.md 2026-10-26: the empty-channel-map branch returned before the delete,
    the pointer write and the recount."""

    def setUp(self):
        super().setUp()
        self.ch = self._channel('Alpha Keyed', None)
        _set_key(self.ch, self.src, 'c.test')
        import_source(self.src, _xmltv(_schedule('c.test', REAL)), epg_days=3, cfg=CFG)
        self.assertEqual(self._winner(self.ch), self.src.id)
        change = _set_key(self.ch, self.src, None)
        self.assertEqual(change.outcome, KEY_NO_KEY)
        self.assertEqual(len(self._active(self.ch)), 3, 'kept until the next refresh, as said')

    def _refresh(self):
        return import_source(db.session.get(EpgSource, self.src.id),
                             _xmltv(_schedule('c.test', REAL)), epg_days=3, cfg=CFG)

    def test_the_next_refresh_clears_the_listings_and_says_the_guide_was_lost(self):
        synced, reason = self._refresh()
        self.assertEqual((synced, reason), (0, None))
        self.assertEqual(self._active(self.ch), [])
        self.assertEqual(self._alternates(self.ch), [])
        self.assertIsNone(self._winner(self.ch))
        alert = Alert.query.filter_by(alert_type='EPG_SOURCE_COVERAGE_LOST',
                                      source=f'epg-source:{self.src.id}:coverage').first()
        self.assertIsNotNone(alert)
        self.assertIn('Alpha Keyed', alert.body)

    def test_the_counts_and_the_directory_follow(self):
        self._refresh()
        db.session.expire_all()
        src = db.session.get(EpgSource, self.src.id)
        self.assertEqual(src.last_status, EPG_STATUS_OK)
        self.assertEqual((src.entry_count, src.channel_count, src.active_channel_count),
                         (0, 0, 0))
        self.assertEqual(EpgSourceChannel.query.filter_by(source_id=src.id).count(), 1,
                         'the directory is still written for the review page')

    def test_a_malformed_feed_is_still_refused_over_a_baseline(self):
        synced, reason = import_source(db.session.get(EpgSource, self.src.id),
                                       b'<tv><programme start="x"', epg_days=3, cfg=CFG)
        self.assertEqual(synced, 0)
        self.assertTrue(reason.startswith('import refused:'), reason)
        self.assertEqual(len(self._active(self.ch)), 3, 'a refusal keeps the old rows')


class CrashGapTests(_Fixture):
    """dev/changelog/1298: rows moved, pointer not. Both tables are consistent; only the
    pointer is behind, and the held check used to read the one table that could not see it."""

    def setUp(self):
        super().setUp()
        self.ch = self._channel('Alpha C', 'c.test')
        self.ext = self._second_source()
        import_source(self.src, _xmltv(_schedule('c.test', REAL)), epg_days=3, cfg=CFG)
        import_source(self.ext, _xmltv(_schedule('c.test', OTHER)), epg_days=3, cfg=CFG)
        self.assertEqual(self._winner(self.ch), self.src.id)
        # The user pins the channel to ext, then a re-resolve back to src dies between the
        # row moves and the pointer write.
        set_source_override(db.session.get(Channel, self.ch.id), db.session.get(EpgSource, self.ext.id),
                            case_sensitive=False)
        db.session.commit()
        self.assertEqual(self._winner(self.ch), self.ext.id)
        db.session.get(Channel, self.ch.id).epg_source_override_id = None
        db.session.commit()
        demote([self.ch.id], self.ext.id)
        promote([self.ch.id], self.src.id)
        self.assertEqual({s for s, _t in self._active(self.ch)}, {self.src.id})
        self.assertEqual(self._winner(self.ch), self.ext.id, 'the crash state: pointer stale')

    def test_a_re_resolve_repairs_the_pointer(self):
        outcome = reresolve_channels([self.ch.id], False)
        self.assertEqual(outcome, {self.ch.id: self.src.id})
        self.assertEqual(self._winner(self.ch), self.src.id)
        self.assertEqual({s for s, _t in self._active(self.ch)}, {self.src.id})
        self.assertEqual({s for s, _t in self._alternates(self.ch)}, {self.ext.id})

    def test_clearing_an_override_repairs_it_too(self):
        ch = db.session.get(Channel, self.ch.id)
        ch.epg_source_override_id = self.ext.id
        db.session.commit()
        change = set_source_override(db.session.get(Channel, self.ch.id), None, case_sensitive=False)
        db.session.commit()
        self.assertEqual(change.winner, self.src.id)
        self.assertEqual(self._winner(self.ch), self.src.id)


class OrphanAlternatesTests(_Fixture):
    """§4: an alternate row is kept only for a channel with an active source. The import's
    statement for it dropped the OLD winner's alternates, which were never there."""

    def setUp(self):
        super().setUp()
        self.ch = self._channel('Alpha Keyed', None)
        self.ext = self._second_source()
        _set_key(self.ch, self.src, 'c.test')
        _set_key(self.ch, self.ext, 'c-ext.test')
        import_source(self.src, _xmltv(_schedule('c.test', REAL)), epg_days=3, cfg=CFG)
        import_source(self.ext, _xmltv(_schedule('c-ext.test', OTHER)), epg_days=3, cfg=CFG)
        self.assertEqual(self._winner(self.ch), self.src.id)
        self.assertEqual({s for s, _t in self._alternates(self.ch)}, {self.ext.id})
        # Clearing the key into ext keeps its alternates until its next refresh, as said.
        self.assertEqual(_set_key(self.ch, self.ext, None).outcome, KEY_NO_KEY)
        self.assertEqual({s for s, _t in self._alternates(self.ch)}, {self.ext.id})

    def test_an_import_that_orphans_the_channel_drops_every_sources_alternates(self):
        import_source(db.session.get(EpgSource, self.src.id), _xmltv(_schedule('zz.test', REAL)),
                      epg_days=3, cfg=NO_GUARD)
        self.assertIsNone(self._winner(self.ch))
        self.assertEqual(self._active(self.ch), [])
        self.assertEqual(self._alternates(self.ch), [])

    def test_a_re_resolve_that_orphans_the_channel_drops_them_too(self):
        self.assertEqual(_set_key(self.ch, self.src, None).outcome, KEY_NO_KEY)
        self.assertEqual(len(self._active(self.ch)), 3, 'kept until the next refresh')
        move_source(self.acct.id, self.ext.id, -1, False)
        self.assertIsNone(self._winner(self.ch))
        self.assertEqual(self._alternates(self.ch), [])
        self.assertEqual(self._active(self.ch), [])


class IndexTests(unittest.TestCase):
    """Migration 102's two indexes (dev/changelog/1298): the model declares them for a fresh
    build, the step adds them to an upgrading database, and the plans use them."""

    def setUp(self):
        self.t = make_test_app()

    def tearDown(self):
        self.t.cleanup()

    def _plan(self, sql):
        raw = db.engine.raw_connection()
        try:
            return ' | '.join(str(r[-1]) for r in
                              raw.cursor().execute('EXPLAIN QUERY PLAN ' + sql).fetchall())
        finally:
            raw.close()

    def test_alternates_are_reached_by_channel_alone(self):
        plan = self._plan('SELECT id FROM epg_alternate_entries WHERE channel_id = 5')
        self.assertIn('ix_epg_alt_channel', plan)
        self.assertNotIn('SCAN', plan)

    def test_a_source_and_channel_statement_still_takes_the_composite_index(self):
        plan = self._plan('DELETE FROM epg_alternate_entries WHERE source_id = 2 '
                          'AND channel_id IN (1, 2, 3)')
        self.assertIn('ix_epg_alt_source_channel', plan)

    def test_channels_are_counted_per_source_through_an_index(self):
        plan = self._plan('SELECT count(*) FROM channels WHERE epg_source_id = 3')
        self.assertIn('ix_channels_epg_source_id', plan)
        self.assertNotIn('SCAN', plan)

    def test_the_migration_adds_both_and_is_re_runnable(self):
        with tempfile.TemporaryDirectory() as d:
            conn = sqlite3.connect(os.path.join(d, 'm.db'))
            cur = conn.cursor()
            cur.execute('CREATE TABLE channels (id INTEGER PRIMARY KEY, epg_source_id INTEGER)')
            cur.execute('CREATE TABLE epg_alternate_entries (id INTEGER PRIMARY KEY, '
                        'source_id INTEGER NOT NULL, channel_id INTEGER NOT NULL)')
            for _ in range(2):
                M._m102_epg_source_indexes(conn, cur)
                names = {r[0] for r in cur.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'index'")}
                self.assertIn('ix_channels_epg_source_id', names)
                self.assertIn('ix_epg_alt_channel', names)
            conn.close()


class InterruptedRefreshTests(unittest.TestCase):
    """dev/docs/BUGS.md 2026-10-26: the startup clear of refresh_started_at wrote nothing
    else, so a source whose import the restart cut off kept the previous refresh's OK."""

    def setUp(self):
        self.t = make_test_app(start_scheduler=True)
        self.acct = seed.make_account(name='Alpha')
        self.src = make_epg_source(self.acct)
        self.src.last_status = EPG_STATUS_OK
        self.src.last_success_at = datetime(2026, 10, 25, 12, 0, 0)
        self.src.refresh_started_at = datetime(2026, 10, 26, 3, 0, 0)
        db.session.commit()

    def tearDown(self):
        self.t.cleanup()

    def _alert(self):
        return Alert.query.filter_by(alert_type='EPG_SOURCE_IMPORT_TRUNCATED',
                                     source=f'epg-source:{self.src.id}:truncated',
                                     dismissed_at=None).first()

    def test_the_source_is_marked_cut_short_with_the_restart_named(self):
        from app.scheduler import resume_in_progress_recordings
        resume_in_progress_recordings(self.t.app)
        db.session.expire_all()
        src = db.session.get(EpgSource, self.src.id)
        self.assertIsNone(src.refresh_started_at)
        self.assertEqual(src.last_status, EPG_STATUS_TRUNCATED)
        self.assertIn('restarted', src.last_error)
        self.assertEqual(src.last_success_at, datetime(2026, 10, 25, 12, 0, 0),
                         'not a success, so the success stamp stays where it was')
        alert = self._alert()
        self.assertIsNotNone(alert)
        self.assertIn('restarted', alert.body)

    def test_the_next_good_refresh_clears_it(self):
        from app.scheduler import resume_in_progress_recordings
        resume_in_progress_recordings(self.t.app)
        seed.make_channel(self.acct, name='Alpha C', epg_channel_id='c.test')
        db.session.commit()
        import_source(db.session.get(EpgSource, self.src.id), _xmltv(_schedule('c.test', REAL)),
                      epg_days=3, cfg=CFG)
        db.session.expire_all()
        self.assertEqual(db.session.get(EpgSource, self.src.id).last_status, EPG_STATUS_OK)
        self.assertIsNone(self._alert())


class RefreshGuardRouteTests(_Fixture):
    """dev/docs/BUGS.md 2026-10-26: six routes guarded against a running refresh six ways,
    and three of them not at all. A sibling source's refresh is the case the own-source
    checks missed: its import resolves the same channels."""

    def setUp(self):
        super().setUp()
        self.t.app.config['WTF_CSRF_ENABLED'] = False
        self.client = self.t.app.test_client()
        self.ch = self._channel('Alpha C', 'c.test')
        self.ext = self._second_source(name='Sibling')
        import_source(self.src, _xmltv(_schedule('c.test', REAL)), epg_days=3, cfg=CFG)
        import_source(self.ext, _xmltv(_schedule('c.test', OTHER)), epg_days=3, cfg=CFG)
        db.session.get(EpgSource, self.ext.id).refresh_started_at = datetime.utcnow()
        db.session.commit()

    def _assert_busy(self, r):
        self.assertEqual(r.status_code, 409, r.get_json())
        self.assertIn('"Sibling" is being refreshed right now', r.get_json()['error'])

    def test_set_key(self):
        self._assert_busy(self.client.post(f'/api/channels/{self.ch.id}/epg-key',
                                           json={'source_id': self.src.id, 'key': 'x.test'}))
        self.assertEqual(self._winner(self.ch), self.src.id)

    def test_clear_key(self):
        self._assert_busy(self.client.post(f'/api/channels/{self.ch.id}/epg-key',
                                           json={'source_id': self.src.id, 'key': ''}))

    def test_use_this_source(self):
        self._assert_busy(self.client.post(f'/api/channels/{self.ch.id}/epg-source-override',
                                           json={'source_id': self.src.id}))

    def test_move(self):
        self._assert_busy(self.client.post(
            f'/api/accounts/{self.acct.id}/epg-sources/{self.src.id}/move',
            json={'direction': 'down'}))

    def test_delete_keeps_its_own_wording(self):
        r = self.client.delete(f'/api/accounts/{self.acct.id}/epg-sources/{self.src.id}')
        self._assert_busy(r)
        self.assertIn('Delete it once the refresh has finished', r.get_json()['error'])
        self.assertIsNotNone(db.session.get(EpgSource, self.src.id))

    def test_stop_using(self):
        self._assert_busy(self.client.post(
            f'/api/accounts/{self.acct.id}/epg-sources/{self.src.id}/stop-using'))
        self.assertEqual(self._winner(self.ch), self.src.id)

    def test_subscribe(self):
        beta = seed.make_account(name='Beta')
        bsrc = make_epg_source(beta, name='Sibling')
        bsrc.refresh_started_at = datetime.utcnow()
        db.session.get(EpgSource, self.ext.id).refresh_started_at = None
        db.session.commit()
        self._assert_busy(self.client.post(f'/api/accounts/{beta.id}/epg-sources/subscribe',
                                           json={'source_id': self.src.id}))

    def test_accept_name_matches(self):
        self._assert_busy(self.client.post(
            f'/api/epg-sources/{self.src.id}/name-matches/accept',
            json={'picks': [{'channel_id': self.ch.id, 'xml_id': 'x.test'}]}))

    def test_nothing_refreshing_lets_every_one_through(self):
        db.session.get(EpgSource, self.ext.id).refresh_started_at = None
        db.session.commit()
        r = self.client.post(f'/api/accounts/{self.acct.id}/epg-sources/{self.src.id}/move',
                             json={'direction': 'down'})
        self.assertEqual(r.status_code, 200, r.get_json())
        r = self.client.post(f'/api/channels/{self.ch.id}/epg-source-override',
                             json={'source_id': self.src.id})
        self.assertEqual(r.status_code, 200, r.get_json())


if __name__ == '__main__':
    unittest.main()
