"""Tier 2 - the FTS5 trigram search indexes (dev/changelog/364).

Pins the contract app/search_index.py has to hold up, because everything the channel-search
revamp is built on assumes it:

  * The index tables exist on a brand-new database. They are raw virtual tables, so
    db.create_all() cannot build them and run_migrations() skips every step on a fresh DB -
    if create_app() ever stops calling ensure_search_index_schema(), search silently loses
    its index on every new install and in every test.
  * A trigram MATCH returns exactly what `LIKE '%q%'` returns, including on case,
    punctuation, ampersands and embedded spaces. This is the whole justification for the
    swap: if it is an approximation rather than a drop-in, the swap is a wrong-data bug.
  * chan_prog holds deduped (channel_id, title, sub_title, description) over FUTURE airings
    only, and a rebuild reflects whatever the EPG table says at that moment.
  * The programs rebuild is chunked, every chunk is bounded, and the index declares itself
    unusable for the duration - one write lock, 10s busy_timeout, recordings in flight.
  * Program columns are scoped per search: asking for titles must not silently search
    descriptions now that both live in one index.
  * A failed rebuild is loud and self-describing: state FAILED, an ERROR alert, and
    search_index_ready() False so callers fall back to LIKE instead of trusting an empty
    index. A later success dismisses the standing alert.
  * Migration 21 brings an existing database up indexed rather than empty.
"""
import os
import sys
import unittest
from datetime import datetime, timedelta
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import text  # noqa: E402

from tests.support.app import make_test_app  # noqa: E402
from tests.support.seed import make_account, make_channel  # noqa: E402
from app import db  # noqa: E402
from app import migrations as M  # noqa: E402
from app import search_index as SI  # noqa: E402
from app.database import Alert, Channel, EPGEntry, SearchIndexState  # noqa: E402


# Deliberately nasty: every one of these is a shape the trigram tokenizer could plausibly
# treat differently from LIKE - mid-word substrings, a pipe, a slash, an ampersand, an
# accent, mixed case, an embedded space.
CORPUS = [
    'US| ESPN2 HD',
    'espn news',
    'Fútbol Total',
    '24/7 Music',
    'A & E HD',
    'Sports HD',
    'Discovery Channel',
]


def _fts_channel_names(query):
    return sorted(r[0] for r in db.session.execute(text(
        'SELECT c.name FROM ch_fts JOIN channels c ON c.id = ch_fts.rowid '
        'WHERE ch_fts MATCH :m'), {'m': SI.fts_match_term(query)}).fetchall())


def _like_channel_names(query):
    return sorted(r[0] for r in db.session.execute(text(
        'SELECT name FROM channels WHERE name LIKE :p'), {'p': f'%{query}%'}).fetchall())


def _table_names():
    return {r[0] for r in db.session.execute(text(
        "SELECT name FROM sqlite_master WHERE name IN "
        "('ch_fts', 'chan_prog', 'chan_prog_fts')")).fetchall()}


class SchemaPresenceTests(unittest.TestCase):
    def setUp(self):
        self.t = make_test_app()

    def tearDown(self):
        self.t.cleanup()

    def test_index_tables_exist_on_a_new_app(self):
        self.assertEqual(_table_names(), {'ch_fts', 'chan_prog', 'chan_prog_fts'},
                         'create_app() must call ensure_search_index_schema() - create_all() '
                         'cannot build a virtual table and the migration runner skips every '
                         'step on a fresh DB')

    def test_ensure_is_idempotent(self):
        SI.ensure_search_index_schema()
        SI.ensure_search_index_schema()
        self.assertEqual(_table_names(), {'ch_fts', 'chan_prog', 'chan_prog_fts'})

    def test_index_is_not_ready_until_something_builds_it(self):
        """A fresh install has the tables and no content. Reporting that as usable would make
        every search answer "nothing matches" - the exact silent-wrong-results failure."""
        self.assertFalse(SI.search_index_ready())
        self.assertFalse(SI.search_index_ready(SI.SEARCH_INDEX_CHANNELS))


class RebuildingIndexNamesTests(unittest.TestCase):
    """rebuilding_index_names() is the one DB-row read shared by the restart guard
    (tools/check_busy.py mirrors its query directly) and the dashboard's background-task
    indicator (dev/changelog/461) - both must agree on what "rebuilding" means."""

    def setUp(self):
        self.t = make_test_app()

    def tearDown(self):
        self.t.cleanup()

    def _state(self, name, status):
        row = SearchIndexState(name=name, status=status)
        db.session.add(row)
        db.session.commit()
        return row

    def test_empty_when_nothing_is_building(self):
        self.assertEqual(SI.rebuilding_index_names(), [])

    def test_a_fresh_install_with_no_state_rows_is_empty(self):
        self.assertEqual(SearchIndexState.query.count(), 0)
        self.assertEqual(SI.rebuilding_index_names(), [])

    def test_names_a_building_index(self):
        self._state(SI.SEARCH_INDEX_PROGRAMS, SI.STATUS_BUILDING)
        self.assertEqual(SI.rebuilding_index_names(), [SI.SEARCH_INDEX_PROGRAMS])

    def test_ok_and_failed_rows_are_not_reported(self):
        self._state(SI.SEARCH_INDEX_CHANNELS, SI.STATUS_OK)
        self._state(SI.SEARCH_INDEX_PROGRAMS, SI.STATUS_FAILED)
        self.assertEqual(SI.rebuilding_index_names(), [])

    def test_reports_every_building_index(self):
        self._state(SI.SEARCH_INDEX_CHANNELS, SI.STATUS_BUILDING)
        self._state(SI.SEARCH_INDEX_PROGRAMS, SI.STATUS_BUILDING)
        self.assertEqual(sorted(SI.rebuilding_index_names()),
                         sorted([SI.SEARCH_INDEX_CHANNELS, SI.SEARCH_INDEX_PROGRAMS]))


class ChannelParityTests(unittest.TestCase):
    """The trigram index must be a drop-in for LIKE, not an approximation."""

    def setUp(self):
        self.t = make_test_app()
        acc = make_account()
        for name in CORPUS:
            make_channel(acc, name=name, category_name='Sports')
        db.session.commit()
        SI.rebuild_search_indexes('test')

    def tearDown(self):
        self.t.cleanup()

    def test_fts_matches_exactly_what_like_matches(self):
        for query in ['espn', 'ESPN', 'ESPN2', 'fútbol', '24/7', 'a & e', 'Sports HD',
                      'US|', 'covery Chan']:
            with self.subTest(query=query):
                self.assertEqual(_fts_channel_names(query), _like_channel_names(query))
                self.assertTrue(_like_channel_names(query),
                                f'{query!r} matches nothing at all - the case proves nothing')

    def test_match_is_case_insensitive_both_directions(self):
        self.assertEqual(_fts_channel_names('espn'), _fts_channel_names('ESPN'))

    def test_other_indexed_columns_are_searchable(self):
        """F4 widens search past the name; the index has to actually carry those columns."""
        ch = make_channel(make_account(name='Second'), name='Indexed Columns',
                          category_name='Documentaries')
        db.session.commit()
        SI.rebuild_search_indexes('test')
        for query in [ch.stream_url[-12:], ch.epg_channel_id, 'Documentaries']:
            with self.subTest(query=query):
                hits = db.session.execute(text(
                    'SELECT c.id FROM ch_fts JOIN channels c ON c.id = ch_fts.rowid '
                    'WHERE ch_fts MATCH :m'), {'m': SI.fts_match_term(query)}).fetchall()
                self.assertIn(ch.id, [r[0] for r in hits])

    def test_two_character_query_is_blind(self):
        """CHARACTERIZATION, not a regression guard - this documents the trigram cliff rather
        than a fixed defect. Below three characters FTS returns zero rows instead of erroring,
        which is why every caller must check TRIGRAM_MIN_CHARS and fall back to LIKE."""
        self.assertEqual(len(SI.fts_match_term('HD')), 4)
        self.assertEqual(_fts_channel_names('HD'), [])
        self.assertTrue(_like_channel_names('HD'))

    def test_match_term_escapes_quotes_and_operators(self):
        """A bare user string handed to MATCH is parsed as an FTS5 query expression, so an
        unescaped quote is a syntax error and `*` is a prefix operator. Both must be literal."""
        self.assertEqual(SI.fts_match_term('a"b'), '"a""b"')
        acc = make_account(name='Operators')
        make_channel(acc, name='Say "Yes" Now')
        make_channel(acc, name='Star * Channel')
        make_channel(acc, name='Minus - Channel')
        db.session.commit()
        SI.rebuild_search_indexes('test')
        for query in ['Say "Yes"', 'Star * Ch', 'Minus - Ch']:
            with self.subTest(query=query):
                self.assertEqual(_fts_channel_names(query), _like_channel_names(query))


class ProgramIndexTests(unittest.TestCase):
    def setUp(self):
        self.t = make_test_app()
        self.acc = make_account()
        self.ch = make_channel(self.acc, name='Sports One')
        self.other = make_channel(self.acc, name='Sports Two')
        now = datetime.utcnow()
        # Two airings of the same program on the same channel: chan_prog must carry one row.
        db.session.add(EPGEntry(channel_id=self.ch.id, title='Monday Night Football',
                                sub_title='Week 3', start_time=now + timedelta(hours=1),
                                stop_time=now + timedelta(hours=3)))
        db.session.add(EPGEntry(channel_id=self.ch.id, title='Monday Night Football',
                                sub_title='Week 3', start_time=now + timedelta(hours=25),
                                stop_time=now + timedelta(hours=27)))
        # Already over: must NOT be indexed.
        db.session.add(EPGEntry(channel_id=self.other.id, title='Yesterday Curling',
                                sub_title=None, start_time=now - timedelta(hours=5),
                                stop_time=now - timedelta(hours=4)))
        db.session.commit()
        SI.rebuild_search_indexes('test')

    def tearDown(self):
        self.t.cleanup()

    def _chan_prog(self):
        return sorted(db.session.execute(text(
            'SELECT channel_id, title, sub_title FROM chan_prog')).fetchall())

    def test_future_airings_only_and_deduped(self):
        self.assertEqual(self._chan_prog(),
                         [(self.ch.id, 'Monday Night Football', 'Week 3')])

    def test_fts_finds_the_channel_by_what_it_airs(self):
        hits = db.session.execute(text(
            'SELECT cp.channel_id FROM chan_prog_fts JOIN chan_prog cp '
            'ON cp.id = chan_prog_fts.rowid WHERE chan_prog_fts MATCH :m'),
            {'m': SI.fts_match_term('night foot')}).fetchall()
        self.assertEqual([r[0] for r in hits], [self.ch.id])

    def test_rebuild_reflects_epg_changes(self):
        EPGEntry.query.filter_by(channel_id=self.ch.id).delete(synchronize_session=False)
        now = datetime.utcnow()
        db.session.add(EPGEntry(channel_id=self.other.id, title='Tomorrow Curling',
                                sub_title='Final', start_time=now + timedelta(hours=20),
                                stop_time=now + timedelta(hours=22)))
        db.session.commit()
        SI.rebuild_search_indexes('test')
        self.assertEqual(self._chan_prog(), [(self.other.id, 'Tomorrow Curling', 'Final')])
        stale = db.session.execute(text(
            'SELECT cp.channel_id FROM chan_prog_fts JOIN chan_prog cp '
            'ON cp.id = chan_prog_fts.rowid WHERE chan_prog_fts MATCH :m'),
            {'m': SI.fts_match_term('Monday Night Football')}).fetchall()
        self.assertEqual(stale, [], 'a rebuilt index must not still match deleted programs')

    def test_rebuild_records_state_per_index(self):
        by_name = {s.name: s for s in SearchIndexState.query.all()}
        self.assertEqual(set(by_name), set(SI.SEARCH_INDEX_NAMES))
        self.assertEqual(by_name[SI.SEARCH_INDEX_PROGRAMS].row_count, 1)
        self.assertEqual(by_name[SI.SEARCH_INDEX_CHANNELS].row_count, 2)
        for state in by_name.values():
            self.assertEqual(state.status, SI.STATUS_OK)
            self.assertIsNone(state.error)
            self.assertIsNotNone(state.rebuilt_at)
        self.assertTrue(SI.search_index_ready())

    def test_only_the_named_index_is_rebuilt(self):
        """The EPG-cleanup hook rebuilds programs alone; a channel rebuild there would be
        seconds of held write lock for data that did not change."""
        SearchIndexState.query.delete()
        db.session.commit()
        SI.rebuild_search_indexes('test', names=(SI.SEARCH_INDEX_PROGRAMS,))
        self.assertEqual([s.name for s in SearchIndexState.query.all()],
                         [SI.SEARCH_INDEX_PROGRAMS])


class DescriptionIndexTests(unittest.TestCase):
    """chan_prog carries the program description as of 2026-07-30 (dev/changelog/395).

    Descriptions are where this app's searches actually land - an EPG title is often just
    "Live Sport" with the teams named only in the description - so these pin both halves:
    that description search finds those channels, and that it does NOT leak into callers who
    asked for titles only. The second half is the silent one: chan_prog_fts now has three
    columns, so an unscoped MATCH would quietly widen every existing caller's search.
    """

    def setUp(self):
        self.t = make_test_app()
        self.acc = make_account()
        self.ch = make_channel(self.acc, name='Sports One')
        self.other = make_channel(self.acc, name='Sports Two')
        now = datetime.utcnow()
        db.session.add(EPGEntry(
            channel_id=self.ch.id, title='Live Sport', sub_title=None,
            description='Wembley hosts Liverpool in the cup final.',
            start_time=now + timedelta(hours=1), stop_time=now + timedelta(hours=3)))
        db.session.add(EPGEntry(
            channel_id=self.other.id, title='Live Sport', sub_title=None,
            description='Coverage of the county cricket.',
            start_time=now + timedelta(hours=1), stop_time=now + timedelta(hours=3)))
        db.session.commit()
        SI.rebuild_search_indexes('test')
        self.readiness = SI.search_index_readiness()

    def tearDown(self):
        self.t.cleanup()

    def _search(self, q, **kwargs):
        return sorted(c.name for c in SI.apply_channel_search(
            Channel.query, q, readiness=self.readiness, **kwargs).all())

    def test_description_is_indexed_and_deduped_on(self):
        """Two airings identical but for their description must stay two rows: description is
        part of the DISTINCT, so collapsing them would lose one channel's only match."""
        rows = db.session.execute(text(
            'SELECT channel_id, title, description FROM chan_prog ORDER BY channel_id')
        ).fetchall()
        self.assertEqual([r[2] for r in rows],
                         ['Wembley hosts Liverpool in the cup final.',
                          'Coverage of the county cricket.'])

    def test_search_finds_a_channel_by_description(self):
        self.assertEqual(self._search('wembley', include_epg_description=True), ['Sports One'])

    def test_titles_only_does_not_match_descriptions(self):
        """The scoping guard. chan_prog_fts indexes description now, so an unscoped MATCH
        would silently turn every existing include_epg caller into a description search."""
        self.assertEqual(self._search('wembley', include_epg=True), [])
        self.assertEqual(self._search('live sport', include_epg=True),
                         ['Sports One', 'Sports Two'])

    def test_description_scope_alone_does_not_match_titles(self):
        self.assertEqual(self._search('live sport', include_epg_description=True), [])

    def test_fts_and_like_agree_on_descriptions(self):
        """The index is an optimization; LIKE is the fallback. If the two disagree, a stale
        index silently changes results instead of only slowing them down."""
        for q in ['wembley', 'cup final', 'county cric']:
            with self.subTest(q=q):
                indexed = self._search(q, include_epg_description=True)
                fallback = sorted(c.name for c in SI.apply_channel_search(
                    Channel.query, q, readiness=(False, 'forced'),
                    include_epg_description=True).all())
                self.assertEqual(indexed, fallback)
                self.assertTrue(indexed, f'{q!r} matches nothing - the case proves nothing')

    def test_description_search_still_answers_while_the_index_is_unusable(self):
        """No index, no wrong answers: the whole point of the readiness gate."""
        SearchIndexState.query.delete()
        db.session.commit()
        self.assertEqual(
            sorted(c.name for c in SI.apply_channel_search(
                Channel.query, 'wembley', include_epg_description=True).all()),
            ['Sports One'])


class ChunkedRebuildTests(unittest.TestCase):
    """The programs rebuild is chunked because a one-shot FTS population holds SQLite's single
    write lock for 39.5s against a 10s busy_timeout (measured 2026-07-30; TASK gate, and
    dev/docs/DESIGN-search-indexes.md section 5). Chunking is therefore a correctness
    constraint on recording, not a tuning knob - and it is what makes STATUS_BUILDING
    necessary, because the index is incomplete for the whole rebuild."""

    def setUp(self):
        self.t = make_test_app()
        self.acc = make_account()
        self.ch = make_channel(self.acc, name='Chunked')
        now = datetime.utcnow()
        for i in range(7):
            db.session.add(EPGEntry(
                channel_id=self.ch.id, title=f'Program {i}', sub_title=None,
                description=f'Description of program {i}',
                start_time=now + timedelta(hours=i + 1),
                stop_time=now + timedelta(hours=i + 2)))
        db.session.commit()

    def tearDown(self):
        self.t.cleanup()

    def test_every_chunk_is_bounded_by_fts_chunk_rows(self):
        """The bound is the whole safety property: a chunk that can grow without limit is a
        one-shot rebuild wearing a loop."""
        units = list(SI.rebuild_units(SI.SEARCH_INDEX_PROGRAMS, lambda sql: 250_000))
        chunks = [u[0] for u in units if 'INSERT INTO chan_prog_fts(rowid' in u[0]]
        self.assertEqual(len(chunks), 25, 'a 250,000-row table must chunk at 10,000 per batch')
        bounds = [(int(c.split('id > ')[1].split(' ')[0]),
                   int(c.split('id <= ')[1].split(' ')[0])) for c in chunks]
        for lo, hi in bounds:
            self.assertEqual(hi - lo, SI.FTS_CHUNK_ROWS)
        self.assertEqual(bounds[0][0], 0, 'the first chunk must start below the lowest id')
        self.assertGreaterEqual(bounds[-1][1], 250_000, 'the last chunk must cover the max id')
        self.assertEqual([b[0] for b in bounds[1:]], [b[1] for b in bounds[:-1]],
                         'chunks must abut exactly - a gap silently drops rows from the index')

    def test_a_chunked_rebuild_indexes_every_row(self):
        """Run the real rebuild with a chunk size small enough to need many batches."""
        with mock.patch.object(SI, 'FTS_CHUNK_ROWS', 2):
            SI.rebuild_search_indexes('test')
        indexed = db.session.execute(text(
            'SELECT COUNT(*) FROM chan_prog_fts')).scalar()
        self.assertEqual(indexed, 7, 'every chan_prog row must end up in the index')
        for i in range(7):
            with self.subTest(i=i):
                hits = db.session.execute(text(
                    'SELECT rowid FROM chan_prog_fts WHERE chan_prog_fts MATCH :m'),
                    {'m': SI.fts_match_term(f'program {i}')}).fetchall()
                self.assertEqual(len(hits), 1)

    def test_a_rebuild_that_replaces_fewer_rows_leaves_nothing_stale(self):
        """Chunks are id ranges over the repopulated table, so a shorter rebuild must not
        leave the tail of the previous one matching."""
        with mock.patch.object(SI, 'FTS_CHUNK_ROWS', 2):
            SI.rebuild_search_indexes('test')
            EPGEntry.query.filter(EPGEntry.title != 'Program 0').delete(
                synchronize_session=False)
            db.session.commit()
            SI.rebuild_search_indexes('test')
        hits = db.session.execute(text(
            'SELECT rowid FROM chan_prog_fts WHERE chan_prog_fts MATCH :m'),
            {'m': SI.fts_match_term('program 5')}).fetchall()
        self.assertEqual(hits, [], 'a rebuilt index must not still match deleted programs')

    def test_the_index_reports_itself_unusable_for_the_whole_rebuild(self):
        """STATUS_BUILDING. The first unit empties the index while its content table still
        holds the old rows, so from that commit until the last chunk a reader that trusted
        this index would get zero matches and no error - the silent-wrong-answer failure."""
        seen = []
        real_units = SI.rebuild_units

        def spy(name, scalar):
            for unit in real_units(name, scalar):
                state = SearchIndexState.query.filter_by(name=name).first()
                seen.append(None if state is None else state.status)
                yield unit

        with mock.patch.object(SI, 'rebuild_units', spy):
            SI.rebuild_search_indexes('test', names=(SI.SEARCH_INDEX_PROGRAMS,))

        self.assertTrue(seen)
        self.assertEqual(set(seen), {SI.STATUS_BUILDING},
                         'the state row must say BUILDING before the first unit runs')
        self.assertEqual(
            SearchIndexState.query.filter_by(name=SI.SEARCH_INDEX_PROGRAMS).one().status,
            SI.STATUS_OK)

    def test_building_is_never_reported_ready(self):
        SI.rebuild_search_indexes('test')
        self.assertTrue(SI.search_index_ready(SI.SEARCH_INDEX_PROGRAMS))
        state = SearchIndexState.query.filter_by(name=SI.SEARCH_INDEX_PROGRAMS).one()
        state.status = SI.STATUS_BUILDING
        db.session.commit()
        ready, reason = SI.search_index_readiness(SI.SEARCH_INDEX_PROGRAMS)
        self.assertFalse(ready, 'a half-populated index must send search back to LIKE')
        self.assertIn('rebuilt', reason)

    def test_the_write_lock_is_released_between_units(self):
        """Each unit is its own transaction. If they were one, the rebuild would hold the
        write lock for its whole duration, which is what the chunking exists to avoid."""
        commits = []
        real_commit = db.session.commit

        def counting_commit():
            commits.append(1)
            real_commit()

        with mock.patch.object(SI, 'FTS_CHUNK_ROWS', 2), \
                mock.patch.object(db.session, 'commit', counting_commit):
            SI.rebuild_search_indexes('test', names=(SI.SEARCH_INDEX_PROGRAMS,))
        # 2 static units + 4 chunks over 7 rows, plus the two state-row writes.
        self.assertGreaterEqual(len(commits), 6)


class RebuildFailureTests(unittest.TestCase):
    def setUp(self):
        self.t = make_test_app()
        make_channel(make_account(), name='Anything')
        db.session.commit()

    def tearDown(self):
        self.t.cleanup()

    def _break_the_channel_rebuild(self):
        # A tuple of UNITS, each a tuple of statements - see REBUILD_SQL. A flat tuple of
        # strings would be read as one-character units and fail on a syntax error instead.
        return mock.patch.dict(
            SI.REBUILD_SQL,
            {SI.SEARCH_INDEX_CHANNELS: (('INSERT INTO no_such_table_at_all VALUES (1)',),)})

    def test_failure_is_recorded_alerted_and_not_raised(self):
        with self._break_the_channel_rebuild():
            results = SI.rebuild_search_indexes('test')

        self.assertFalse(results[SI.SEARCH_INDEX_CHANNELS])
        self.assertTrue(results[SI.SEARCH_INDEX_PROGRAMS],
                        'one index failing must not stop the other from rebuilding')

        state = SearchIndexState.query.filter_by(name=SI.SEARCH_INDEX_CHANNELS).one()
        self.assertEqual(state.status, SI.STATUS_FAILED)
        self.assertIn('no_such_table_at_all', state.error)

        alerts = Alert.query.filter_by(alert_type='SEARCH_INDEX_REBUILD_FAILED').all()
        self.assertEqual(len(alerts), 1)
        self.assertEqual(alerts[0].severity, 'ERROR')
        self.assertIn('channels', alerts[0].title)

    def test_a_failed_index_is_never_reported_ready(self):
        with self._break_the_channel_rebuild():
            SI.rebuild_search_indexes('test')
        self.assertFalse(SI.search_index_ready())
        self.assertFalse(SI.search_index_ready(SI.SEARCH_INDEX_CHANNELS))
        self.assertTrue(SI.search_index_ready(SI.SEARCH_INDEX_PROGRAMS),
                        'the healthy index must stay usable on its own')

    def test_a_later_success_clears_the_standing_alert(self):
        with self._break_the_channel_rebuild():
            SI.rebuild_search_indexes('test')
        SI.rebuild_search_indexes('test')

        self.assertTrue(SI.search_index_ready())
        standing = Alert.query.filter(
            Alert.alert_type == 'SEARCH_INDEX_REBUILD_FAILED',
            Alert.dismissed_at.is_(None)).all()
        self.assertEqual(standing, [], 'a recovered index must dismiss its own alert')

    def test_repeated_failures_refresh_one_alert_rather_than_stacking(self):
        with self._break_the_channel_rebuild():
            SI.rebuild_search_indexes('test')
            SI.rebuild_search_indexes('test')
            SI.rebuild_search_indexes('test')
        self.assertEqual(
            Alert.query.filter_by(alert_type='SEARCH_INDEX_REBUILD_FAILED').count(), 1)


class MigrationTests(unittest.TestCase):
    """Migration 21 is what brings an EXISTING database up indexed. The fresh path is
    covered by SchemaPresenceTests; this is the other half."""

    def setUp(self):
        self.t = make_test_app()
        acc = make_account()
        self.ch = make_channel(acc, name='Migrated Channel')
        now = datetime.utcnow()
        db.session.add(EPGEntry(channel_id=self.ch.id, title='Future Program',
                                sub_title=None, start_time=now + timedelta(hours=1),
                                stop_time=now + timedelta(hours=2)))
        db.session.commit()

    def tearDown(self):
        self.t.cleanup()

    def test_migration_creates_and_populates_the_indexes(self):
        conn = db.engine.raw_connection()
        try:
            cur = conn.cursor()
            for stmt in ['DROP TABLE IF EXISTS ch_fts',
                         'DROP TABLE IF EXISTS chan_prog_fts',
                         'DROP TABLE IF EXISTS chan_prog']:
                cur.execute(stmt)
            conn.commit()
            self.assertEqual(_table_names(), set(), 'precondition: indexes are gone')

            M._m021_search_indexes(conn, cur)
        finally:
            conn.close()

        db.session.expire_all()
        self.assertEqual(_table_names(), {'ch_fts', 'chan_prog', 'chan_prog_fts'})
        self.assertEqual(_fts_channel_names('Migrated'), ['Migrated Channel'])
        self.assertEqual(
            db.session.execute(text('SELECT title FROM chan_prog')).fetchall(),
            [('Future Program',)])
        for state in SearchIndexState.query.all():
            self.assertEqual(state.status, SI.STATUS_OK)
        # Not ready yet, and correctly so: m021 records no watermark, so the readiness gate
        # cannot confirm the index matches its source. m022 is what closes that
        # (dev/changelog/365) - a database that stopped at 21 searches on LIKE.
        self.assertFalse(SI.search_index_ready())

    def test_watermark_migration_makes_the_index_queryable(self):
        """The real upgrade path is 21 then 22. Only after 22 has recorded a watermark can
        search trust the index (dev/changelog/365)."""
        conn = db.engine.raw_connection()
        try:
            cur = conn.cursor()
            M._m021_search_indexes(conn, cur)
            M._m022_search_index_watermark(conn, cur)
        finally:
            conn.close()

        db.session.expire_all()
        self.assertTrue(SI.search_index_ready())
        for state in SearchIndexState.query.all():
            self.assertEqual(state.status, SI.STATUS_OK)
            self.assertTrue(state.source_watermark,
                            f'{state.name}: m022 must record a watermark, not leave it NULL')
        self.assertEqual(_fts_channel_names('Migrated'), ['Migrated Channel'])

    def test_migration_is_registered_as_the_current_version(self):
        versions = [v for v, _d, _fn in M.SCHEMA_MIGRATIONS]
        self.assertIn(21, versions)
        self.assertEqual(M.CURRENT_SCHEMA_VERSION, max(versions))

    def _revert_to_the_pre_description_shape(self, conn, cur):
        """Exactly the tables migrations 21 and 22 used to leave behind."""
        cur.execute('DROP TABLE IF EXISTS chan_prog_fts')
        cur.execute('DROP TABLE IF EXISTS chan_prog')
        cur.execute('CREATE TABLE chan_prog (id INTEGER PRIMARY KEY, '
                    'channel_id INTEGER NOT NULL, title TEXT, sub_title TEXT)')
        cur.execute("CREATE VIRTUAL TABLE chan_prog_fts USING fts5(title, sub_title, "
                    "content='chan_prog', content_rowid='id', tokenize='trigram')")
        conn.commit()

    def test_m023_widens_an_existing_index_to_carry_descriptions(self):
        """The upgrade path that matters: a database already at 22 has a two-column chan_prog
        and a two-column virtual table, and an FTS5 column set cannot be ALTERed."""
        db.session.add(EPGEntry(channel_id=self.ch.id, title='Live Sport', sub_title=None,
                                description='Wembley hosts the final.',
                                start_time=datetime.utcnow() + timedelta(hours=1),
                                stop_time=datetime.utcnow() + timedelta(hours=2)))
        db.session.commit()
        conn = db.engine.raw_connection()
        try:
            cur = conn.cursor()
            self._revert_to_the_pre_description_shape(conn, cur)
            M._m023_chan_prog_description(conn, cur)
        finally:
            conn.close()

        db.session.expire_all()
        cols = [r[1] for r in db.session.execute(text(
            'PRAGMA table_info(chan_prog_fts)')).fetchall()]
        self.assertIn('description', cols)
        hits = db.session.execute(text(
            'SELECT cp.channel_id FROM chan_prog_fts JOIN chan_prog cp '
            'ON cp.id = chan_prog_fts.rowid WHERE chan_prog_fts MATCH :m'),
            {'m': SI.fts_match_term('wembley')}).fetchall()
        self.assertEqual([r[0] for r in hits], [self.ch.id],
                         'm023 must leave the index actually answering description searches')
        state = SearchIndexState.query.filter_by(name=SI.SEARCH_INDEX_PROGRAMS).one()
        self.assertEqual(state.status, SI.STATUS_OK)
        self.assertTrue(state.source_watermark)

    def test_m022_survives_a_database_that_stopped_at_21(self):
        """m022 runs the CURRENT shared rebuild SQL, which names a column that did not exist
        when m022 shipped. Without the shape fix it dies on 'no such column: description'."""
        conn = db.engine.raw_connection()
        try:
            cur = conn.cursor()
            self._revert_to_the_pre_description_shape(conn, cur)
            M._m022_search_index_watermark(conn, cur)
        finally:
            conn.close()
        db.session.expire_all()
        self.assertTrue(SI.search_index_ready())


if __name__ == '__main__':
    unittest.main()
