"""The daily EPG cleanup and the full programs rebuild no longer hold the write lock for one
long statement (dev/docs/BUGS.md 2026-11-05 @ 08:14 PM, dev/changelog/1456 and 1460).

On prod the cleanup deleted about 525,000 ended listings in one transaction, 56-74 s with the
write lock held every night, and the write probe logged "database is locked" inside that
window on 19 nights. The rebuild that follows cleared its FTS index with one 'delete-all',
44 s held on a copy of that database.

  - CleanupChunkTests: the cleanup deletes EPG_CLEANUP_CHUNK_ROWS per transaction, from both
    tables, and the stored counts still come out right.
  - CleanupChunkTests also holds the cleanup to the difference plus a compaction, not a full
    rebuild.
  - CompactTests: the compaction is FTS5's 'merge', -N once then N, one transaction a call,
    stopping at a no-op or a bound, and a failure leaves the index in use.
  - ClearTests: the index is read with no write lock held before FTS5's 'delete-all' runs,
    with secure_delete off for that statement and put back after it, in one transaction, and
    nothing writes an FTS5 shadow table directly.

No network, no ffmpeg.
"""
import os
import re
import sqlite3
import sys
import unittest
from datetime import datetime, timedelta
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import event, text  # noqa: E402

from app import accounts, db  # noqa: E402
from app import search_index as SI  # noqa: E402
from app.accounts import cleanup_old_epg_entries  # noqa: E402
from app.database import (Account, EPGEntry, EpgAlternateEntry, EpgSource,  # noqa: E402
                          SearchIndexState)
from tests.support import make_test_app, seed  # noqa: E402
from tests.support.seed import make_epg_source  # noqa: E402

class _Case(unittest.TestCase):

    def setUp(self):
        self.t = make_test_app()
        self.acct = seed.make_account(name='Alpha')
        self.src = make_epg_source(self.acct)
        self.ch = seed.make_channel(self.acct, name='One')
        db.session.commit()

    def tearDown(self):
        self.t.cleanup()

    def _listing(self, i, hours, model=EPGEntry):
        start = datetime.utcnow() + timedelta(hours=hours)
        # Long and distinct, so the index needs more than one page.
        words = ' '.join(f'word{i}x{n}' for n in range(120))
        row = model(channel_id=self.ch.id, source_id=self.src.id, title=f'Program {i}',
                    description=f'About program {i}. {words}', start_time=start,
                    stop_time=start + timedelta(minutes=30))
        db.session.add(row)


class CleanupChunkTests(_Case):

    def setUp(self):
        super().setUp()
        for i in range(7):
            self._listing(i, hours=-72)
        for i in range(7, 10):
            self._listing(i, hours=2)
        for i in range(5):
            self._listing(100 + i, hours=-72, model=EpgAlternateEntry)
        db.session.commit()

    def _run(self, chunk):
        """Run the cleanup, returning the rows each DELETE of a listing table removed and how
        many commits it took."""
        removed, commits = [], []
        engines = list(db.engines.values())

        def on_execute(conn, cursor, statement, parameters, context, executemany):
            if statement.startswith('DELETE FROM epg_'):
                removed.append((statement.split()[2], cursor.rowcount))

        def on_commit(conn):
            commits.append(1)

        for engine in engines:
            event.listen(engine, 'after_cursor_execute', on_execute)
            event.listen(engine, 'commit', on_commit)
        try:
            with mock.patch.object(accounts, 'EPG_CLEANUP_CHUNK_ROWS', chunk), \
                    mock.patch('app.search_index.rebuild_search_indexes'):
                cleanup_old_epg_entries(self.t.app)
        finally:
            for engine in engines:
                event.remove(engine, 'after_cursor_execute', on_execute)
                event.remove(engine, 'commit', on_commit)
        db.session.expire_all()
        return removed, len(commits)

    def test_no_transaction_deletes_more_than_one_chunk(self):
        removed, commits = self._run(chunk=2)
        self.assertEqual([n for table, n in removed if table == 'epg_entries'], [2, 2, 2, 1])
        self.assertEqual([n for table, n in removed if table == 'epg_alternate_entries'],
                         [2, 2, 1])
        self.assertGreaterEqual(commits, 7, 'each chunk commits before the next begins')

    def test_everything_ended_goes_and_everything_ahead_stays(self):
        self._run(chunk=3)
        self.assertEqual(sorted(e.title for e in EPGEntry.query),
                         ['Program 7', 'Program 8', 'Program 9'])
        self.assertEqual(EpgAlternateEntry.query.count(), 0)

    def test_the_stored_counts_are_recomputed_after_the_chunks(self):
        db.session.get(Account, self.acct.id).epg_entry_count = 10
        db.session.get(EpgSource, self.src.id).entry_count = 15
        db.session.commit()
        self._run(chunk=2)
        self.assertEqual(db.session.get(Account, self.acct.id).epg_entry_count, 3)
        self.assertEqual(db.session.get(EpgSource, self.src.id).entry_count, 3)

    def test_the_program_index_gets_the_difference_and_a_compaction(self):
        """Not a full rebuild: emptying the whole index is the step no documented FTS5
        command bounds (dev/changelog/1460)."""
        with mock.patch('app.search_index.rebuild_search_indexes') as rebuild:
            cleanup_old_epg_entries(self.t.app)
        rebuild.assert_called_once_with('EPG cleanup', names=(SI.SEARCH_INDEX_PROGRAMS,),
                                        only_changes=True, compact=True)

    def test_an_exact_multiple_of_the_chunk_ends_cleanly(self):
        EPGEntry.query.filter(EPGEntry.title == 'Program 0').delete()
        db.session.commit()
        removed, _commits = self._run(chunk=3)
        self.assertEqual([n for table, n in removed if table == 'epg_entries'], [3, 3, 0])


class ClearTests(_Case):
    """How a full rebuild empties chan_prog_fts (dev/changelog/1460): it reads the index with
    no write lock held, then runs FTS5's own 'delete-all' with PRAGMA secure_delete off for
    that one statement, and never writes a shadow table itself."""

    def setUp(self):
        super().setUp()
        for i in range(40):
            self._listing(i, hours=2 + i)
        db.session.commit()
        SI.rebuild_search_indexes('seed')
        self.assertGreater(self._scalar('SELECT COUNT(*) FROM chan_prog_fts_data'), 3,
                           'the fixture must span several pages')

    def _scalar(self, sql):
        value = db.session.execute(text(sql)).scalar()
        db.session.rollback()
        return value

    def _matches(self, word):
        rows = db.session.execute(text(
            'SELECT COUNT(*) FROM chan_prog_fts WHERE chan_prog_fts MATCH :m'),
            {'m': SI.fts_match_term(word)}).scalar()
        db.session.rollback()
        return rows

    def _rebuild_recording(self, secure_delete=1):
        """Run a full programs rebuild on connections whose secure_delete starts at
        `secure_delete`, the way Debian's SQLite opens them. Returns, for every statement, (the
        SQL, whether a transaction was open on its connection after it, secure_delete after
        it)."""
        seen = []
        engines = list(db.engines.values())

        def on_connect(dbapi_conn, record):
            dbapi_conn.execute(f'PRAGMA secure_delete={secure_delete}')

        def on_execute(conn, cursor, statement, parameters, context, executemany):
            raw = cursor.connection
            seen.append((statement, raw.in_transaction,
                         raw.execute('PRAGMA secure_delete').fetchone()[0]))

        db.session.remove()
        for engine in engines:
            event.listen(engine, 'connect', on_connect)
            engine.dispose()
            event.listen(engine, 'after_cursor_execute', on_execute)
        try:
            self.assertTrue(SI.rebuild_search_indexes(
                'test', names=(SI.SEARCH_INDEX_PROGRAMS,))[SI.SEARCH_INDEX_PROGRAMS])
        finally:
            for engine in engines:
                event.remove(engine, 'after_cursor_execute', on_execute)
                event.remove(engine, 'connect', on_connect)
                engine.dispose()
        return seen

    @staticmethod
    def _position(seen, needle):
        return next(i for i, row in enumerate(seen) if needle in row[0])

    def test_the_index_is_read_before_the_write_lock_is_taken(self):
        """Cold, the reading is what held the lock for 32 s. Every shadow table the
        'delete-all' frees is read first, with no transaction open, so no writer waits on it."""
        seen = self._rebuild_recording()
        clear = self._position(seen, "'delete-all'")
        for table in ('chan_prog_fts_data', 'chan_prog_fts_idx', 'chan_prog_fts_docsize'):
            with self.subTest(table=table):
                read = self._position(seen, f'FROM {table}')
                self.assertLess(read, clear)
                self.assertFalse(seen[read][1], 'the read must not hold a write transaction')

    def test_delete_all_runs_with_secure_delete_off_and_puts_it_back(self):
        """On, every freed page is zeroed into the WAL: 600 MB on the production copy, and
        another writer waited 8.3 s behind it even with the pages read first."""
        seen = self._rebuild_recording(secure_delete=1)
        clear = self._position(seen, "'delete-all'")
        self.assertEqual(seen[clear][2], 0, 'secure_delete must be off for the delete-all')
        restore = next(row for row in seen[clear + 1:] if 'secure_delete=' in row[0])
        self.assertTrue(restore[1], 'it is put back inside the same unit, before its commit')
        self.assertEqual(restore[2], 1, 'the connection gets back the setting it had')
        self.assertTrue(all(row[2] == 1 for row in seen[clear + 1:]
                            if row is not restore and 'secure_delete=' not in row[0]),
                        'nothing after the clear runs with secure_delete off')

    def test_the_setting_put_back_is_the_one_the_connection_had(self):
        """FAST reads back as 2, and setting 2 means ON, so it is put back by name."""
        seen = self._rebuild_recording(secure_delete='FAST')
        clear = self._position(seen, "'delete-all'")
        self.assertEqual(seen[clear][2], 0)
        restore = next(row for row in seen[clear + 1:] if 'secure_delete=' in row[0])
        self.assertEqual(restore[2], 2)

    def test_no_statement_writes_an_fts5_shadow_table(self):
        """The shadow tables are FTS5's own; writing them directly is not a documented
        interface. Only FTS5's commands may change them."""
        shadow_write = re.compile(
            r'^\s*(DELETE\s+FROM|INSERT\s+(OR\s+\w+\s+)?INTO|UPDATE|REPLACE\s+INTO)\s+'
            r'["`]?\w+_fts_(data|idx|docsize|config|content)\b', re.IGNORECASE)
        seen = self._rebuild_recording()
        self.assertTrue(any("'delete-all'" in row[0] for row in seen))
        self.assertEqual([row[0] for row in seen if shadow_write.search(row[0])], [])

    def test_the_clear_is_one_transaction(self):
        units = [unit for unit in SI.rebuild_units(SI.SEARCH_INDEX_PROGRAMS, self._scalar)
                 if any('chan_prog_fts(chan_prog_fts)' in stmt for stmt in unit)]
        self.assertEqual(len(units), 1)
        self.assertEqual(len([s for s in units[0] if "'delete-all'" in s]), 1)

    def test_the_rebuilt_index_is_whole(self):
        SI.rebuild_search_indexes('test', names=(SI.SEARCH_INDEX_PROGRAMS,))
        self.assertEqual(self._scalar('SELECT COUNT(*) FROM chan_prog_fts'), 40)
        for i in (0, 17, 39):
            self.assertEqual(self._matches(f'word{i}x5'), 1)
        # FTS5's own check of the index against its content table; it raises on a mismatch.
        db.session.execute(text(
            "INSERT INTO chan_prog_fts(chan_prog_fts) VALUES('integrity-check')"))
        db.session.rollback()

    def test_the_rebuild_log_names_its_longest_transaction(self):
        with self.assertLogs('app.search_index', level='INFO') as logs:
            SI.rebuild_search_indexes('test', names=(SI.SEARCH_INDEX_PROGRAMS,))
        line = next(line for line in logs.output if 'rebuilt in' in line)
        self.assertRegex(line, r'full, \d+ transactions, the longest \d+ms \(number \d+: ')


class CompactTests(_Case):
    """The daily cleanup compacts the programs index with FTS5's documented incremental
    'merge' - one call with -N, then N until a call changes nothing - each call its own
    transaction (dev/changelog/1460)."""

    def setUp(self):
        super().setUp()
        for i in range(30):
            self._listing(i, hours=2 + i)
        db.session.commit()
        SI.rebuild_search_indexes('seed', names=(SI.SEARCH_INDEX_PROGRAMS,))
        # Several small differences, each its own FTS5 segment, so there is something to merge.
        for i in range(30, 36):
            self._listing(i, hours=2 + i)
            db.session.commit()
            with mock.patch.object(SI, 'DIFF_CHUNK_ROWS', 1):
                SI.rebuild_search_indexes('sync', names=(SI.SEARCH_INDEX_PROGRAMS,),
                                          only_changes=True)

    def _compact_recording(self, **patches):
        """Run the cleanup's rebuild with compaction. Returns ([(merge pages, commits before
        it)], every statement run, the log lines)."""
        merges, statements, commits = [], [], []
        engines = list(db.engines.values())

        def on_execute(conn, cursor, statement, parameters, context, executemany):
            statements.append(statement)
            found = re.search(r"VALUES\('merge', (-?\d+)\)", statement)
            if found:
                merges.append((int(found.group(1)), len(commits)))

        def on_commit(conn):
            commits.append(1)

        for engine in engines:
            event.listen(engine, 'after_cursor_execute', on_execute)
            event.listen(engine, 'commit', on_commit)
        try:
            with mock.patch.multiple(SI, **{'COMPACT_MERGE_PAGES': 1, **patches}), \
                    self.assertLogs('app.search_index', level='INFO') as logs:
                SI.rebuild_search_indexes('EPG cleanup', names=(SI.SEARCH_INDEX_PROGRAMS,),
                                          only_changes=True, compact=True)
        finally:
            for engine in engines:
                event.remove(engine, 'after_cursor_execute', on_execute)
                event.remove(engine, 'commit', on_commit)
        return merges, statements, logs.output

    def _state(self):
        db.session.expire_all()
        return SearchIndexState.query.filter_by(name=SI.SEARCH_INDEX_PROGRAMS).one()

    def _whole(self):
        self.assertEqual(db.session.execute(text(
            'SELECT COUNT(*) FROM chan_prog_fts')).scalar(), 36)
        for i in (0, 17, 35):
            self.assertEqual(db.session.execute(text(
                'SELECT COUNT(*) FROM chan_prog_fts WHERE chan_prog_fts MATCH :m'),
                {'m': SI.fts_match_term(f'word{i}x5')}).scalar(), 1)
        db.session.execute(text(
            "INSERT INTO chan_prog_fts(chan_prog_fts) VALUES('integrity-check')"))
        db.session.rollback()

    def test_merge_with_minus_n_once_then_n_each_in_its_own_transaction(self):
        merges, statements, logs = self._compact_recording(COMPACT_MERGE_PAGES=1)
        self.assertGreater(len(merges), 2, 'the fixture must leave something to merge')
        self.assertEqual(merges[0][0], -1)
        self.assertTrue(all(pages == 1 for pages, _ in merges[1:]), merges)
        commits_before = [n for _, n in merges]
        self.assertEqual(len(set(commits_before)), len(commits_before),
                         'no two merges may share a transaction')
        self.assertTrue(any('compacted in' in line for line in logs), logs)
        self._whole()

    def test_compaction_never_clears_the_index(self):
        """The daily path writes the difference; 'delete-all' is a full rebuild's alone."""
        _merges, statements, _logs = self._compact_recording()
        self.assertFalse([s for s in statements if "'delete-all'" in s])
        self.assertTrue(any("'merge'" in s for s in statements))

    def test_a_pass_cut_short_says_so_and_leaves_a_whole_index(self):
        merges, _statements, logs = self._compact_recording(
            COMPACT_MERGE_PAGES=1, _COMPACT_MAX_UNITS=2)
        self.assertEqual(len(merges), 2)
        self.assertTrue(any('WARNING' in line and 'still to do' in line for line in logs), logs)
        self.assertEqual(self._state().status, SI.STATUS_OK)
        self._whole()

    def test_a_failed_compaction_is_a_warning_and_the_index_stays_in_use(self):
        real_execute = db.session.execute

        def failing(statement, *args, **kwargs):
            if "'merge'" in str(statement):
                raise sqlite3.OperationalError('disk I/O error')
            return real_execute(statement, *args, **kwargs)

        with mock.patch.object(db.session, 'execute', side_effect=failing):
            _merges, _statements, logs = self._compact_recording()
        self.assertTrue(any('WARNING' in line and 'disk I/O error' in line for line in logs),
                        logs)
        self.assertEqual(self._state().status, SI.STATUS_OK)
        self._whole()

    def test_without_compact_nothing_merges(self):
        merges = []
        with mock.patch.object(SI, '_compact_programs', side_effect=merges.append):
            SI.rebuild_search_indexes('account 1 sync', names=(SI.SEARCH_INDEX_PROGRAMS,),
                                      only_changes=True)
        self.assertEqual(merges, [])


if __name__ == '__main__':
    unittest.main()
