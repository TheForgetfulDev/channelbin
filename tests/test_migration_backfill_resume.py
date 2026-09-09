"""Tier 2 - a migration's data backfill survives an interrupted upgrade
(dev/docs/BUGS.md 2026-08-16 "interrupted migration backfills are silently skipped").

Four steps add a column, commit that, and only then populate it. Deciding whether to run
the backfill by asking "did I add this column just now" means a process death in the window
between the column's commit and the version stamp makes the retry conclude there is nothing
left to do: it skips the backfill, stamps the version, and no later run ever revisits it.
The column is present and its data is wrong, permanently and silently.

Every test below kills a step partway through and then re-runs it, which is exactly what a
restarted container does. The invariant, per step: a backfill that did not finish is run
again by the next attempt, and one that did finish is not.

Ten of the fifteen fail against the pre-fix tree. The five that pass there are deliberate
controls on the other half of the invariant - the `..._is_not_run_again` pair,
`test_a_completed_baseline_backfills_nothing_on_a_re_run`,
`test_columns_that_already_existed_owe_nothing`, and
`test_a_channel_with_no_score_is_still_backfilled` - which pin behavior the fix had to
preserve rather than behavior it introduced. They are characterization tests, not guards.
"""
import os
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tests.support.app import make_test_app  # noqa: E402
from tests.support import seed  # noqa: E402
from app import db  # noqa: E402
from app import migrations as M  # noqa: E402


class _SimulatedCrash(RuntimeError):
    """Stands in for the process dying mid-step (container restart, OOM kill, power cut)."""


class _CrashingCursor:
    """Cursor proxy that raises the first time it is handed a statement containing
    `trigger`, so a step can be interrupted at a chosen point instead of by luck."""

    def __init__(self, cur, trigger):
        self._cur = cur
        self._trigger = trigger
        self.fired = False

    def execute(self, sql, *args):
        if not self.fired and self._trigger in sql:
            self.fired = True
            raise _SimulatedCrash(sql)
        return self._cur.execute(sql, *args)

    def __getattr__(self, name):
        return getattr(self._cur, name)


def _scratch(td, *ddl):
    import sqlite3
    conn = sqlite3.connect(os.path.join(td, 'scratch.db'))
    cur = conn.cursor()
    for statement in ddl:
        cur.execute(statement)
    conn.commit()
    return conn, cur


class BackfillLedgerTests(unittest.TestCase):
    """The facility itself: an obligation is durable once registered, and clearing it is
    what stops the work from being repeated."""

    def test_obligation_is_pending_until_it_is_finished(self):
        with tempfile.TemporaryDirectory() as td:
            conn, cur = _scratch(td)
            self.assertFalse(M._backfill_pending(cur, 'x'),
                             'an unregistered name owes nothing')

            M._register_backfill(conn, cur, 'x')
            self.assertTrue(M._backfill_pending(cur, 'x'))

            M._finish_backfill(conn, cur, 'x')
            self.assertFalse(M._backfill_pending(cur, 'x'))
            conn.close()

    def test_registration_is_committed_before_it_returns(self):
        """The obligation has to be committed before the ALTER it belongs to, or a crash
        between the two leaves a column with no record that it still owes data - which is
        the original defect wearing a smaller window."""
        import sqlite3
        with tempfile.TemporaryDirectory() as td:
            conn, cur = _scratch(td)
            M._register_backfill(conn, cur, 'x')

            other = sqlite3.connect(os.path.join(td, 'scratch.db'))
            row = other.execute(
                'SELECT completed_at FROM migration_backfills WHERE name = ?', ('x',)).fetchone()
            other.close()
            conn.close()
            self.assertEqual(row, (None,),
                             'a second connection can already see the pending obligation')

    def test_resuming_says_so_and_a_first_run_does_not(self):
        with tempfile.TemporaryDirectory() as td:
            conn, cur = _scratch(td)
            M._register_backfill(conn, cur, 'x')

            with self.assertLogs('app.migrations', level='WARNING') as logs:
                self.assertTrue(M._backfill_needed(cur, 'x', registered_now=False))
            self.assertIn('x', logs.output[0])
            self.assertIn('did not finish', logs.output[0])
            conn.close()


class M024ResumeTests(unittest.TestCase):
    """Migration 24 - channels.url_normalizable. The worst of the four: the flag gates the
    "Hide 'not normalized' URLs" standing option, which is on by default, so a half-written
    backfill quietly drops channels out of every search with nothing saying why."""

    # The step also builds five facet indexes, so the scratch table carries their columns.
    _DDL = ('CREATE TABLE channels (id INTEGER PRIMARY KEY, raw_stream_url TEXT, '
            'stream_url TEXT, category_name TEXT, in_guide BOOLEAN, '
            'is_duplicate_stream_url BOOLEAN, test_enabled BOOLEAN, health_score FLOAT, '
            'manual_health_adjustment INTEGER)',)

    def _seed(self, conn, cur):
        cur.execute("INSERT INTO channels (id, raw_stream_url, stream_url) VALUES "
                    "(1, 'http://host/user/pass/123', 'http://host/user/pass/123'), "
                    "(2, 'http://host/radio-mount', 'http://host/radio-mount')")
        conn.commit()

    def _flags(self, cur):
        return dict(cur.execute('SELECT id, url_normalizable FROM channels ORDER BY id'))

    def test_interrupted_backfill_is_finished_by_the_next_run(self):
        with tempfile.TemporaryDirectory() as td:
            conn, cur = _scratch(td, *self._DDL)
            self._seed(conn, cur)

            crashing = _CrashingCursor(cur, 'SELECT id, raw_stream_url')
            with self.assertRaises(_SimulatedCrash):
                M._m024_channel_search_support(conn, crashing)
            self.assertIn('url_normalizable',
                          {r[1] for r in cur.execute('PRAGMA table_info(channels)')},
                          'the column was committed before the crash - that is the trap')
            self.assertEqual(self._flags(cur), {1: 1, 2: 1}, 'nothing was backfilled yet')

            M._m024_channel_search_support(conn, cur)

            self.assertEqual(self._flags(cur), {1: 1, 2: 0},
                             'the retry finished the backfill instead of skipping it')
            conn.close()

    def test_a_crash_between_chunks_still_finishes(self):
        """The chunked writes commit as they go, so an interrupted run can leave the flags
        partly written - the state that reads most convincingly as "already done"."""
        with tempfile.TemporaryDirectory() as td:
            conn, cur = _scratch(td, *self._DDL)
            self._seed(conn, cur)

            crashing = _CrashingCursor(cur, 'UPDATE channels SET url_normalizable = 0')
            with self.assertRaises(_SimulatedCrash):
                M._m024_channel_search_support(conn, crashing)

            M._m024_channel_search_support(conn, cur)

            self.assertEqual(self._flags(cur), {1: 1, 2: 0})
            conn.close()

    def test_a_finished_backfill_is_not_run_again(self):
        with tempfile.TemporaryDirectory() as td:
            conn, cur = _scratch(td, *self._DDL)
            self._seed(conn, cur)
            M._m024_channel_search_support(conn, cur)

            # A flag hand-edited after the step completed must survive a re-run: the step
            # is done, and re-running one is a no-op by design.
            cur.execute('UPDATE channels SET url_normalizable = 1 WHERE id = 2')
            conn.commit()
            M._m024_channel_search_support(conn, cur)

            self.assertEqual(self._flags(cur), {1: 1, 2: 1})
            conn.close()


class M032ResumeTests(unittest.TestCase):
    """Migration 32 - recording_segments.channel_id, whose correlation pass commits in
    stages and used to return outright on "the column exists"."""

    _DDL = (
        'CREATE TABLE recordings (id INTEGER PRIMARY KEY, channel_id INTEGER, group_id INTEGER)',
        'CREATE TABLE recording_segments (id INTEGER PRIMARY KEY, recording_id INTEGER NOT NULL, '
        'segment_number INTEGER NOT NULL, started_at DATETIME)',
        'CREATE TABLE recording_events (id INTEGER PRIMARY KEY, recording_id INTEGER NOT NULL, '
        'timestamp DATETIME NOT NULL, event_type VARCHAR(64) NOT NULL, extra_data TEXT)',
    )

    def _seed(self, conn, cur):
        cur.execute('INSERT INTO recordings (id, channel_id, group_id) VALUES (1, 7, NULL)')
        cur.execute('INSERT INTO recording_segments (id, recording_id, segment_number) '
                    'VALUES (1, 1, 1), (2, 1, 2)')
        conn.commit()

    def test_interrupted_backfill_is_finished_by_the_next_run(self):
        with tempfile.TemporaryDirectory() as td:
            conn, cur = _scratch(td, *self._DDL)
            self._seed(conn, cur)

            crashing = _CrashingCursor(cur, 'UPDATE recording_segments SET channel_id = (')
            with self.assertRaises(_SimulatedCrash):
                M._m032_segment_channel_id(conn, crashing)
            self.assertIn('channel_id',
                          {r[1] for r in cur.execute('PRAGMA table_info(recording_segments)')})

            M._m032_segment_channel_id(conn, cur)

            self.assertEqual(
                cur.execute('SELECT id, channel_id FROM recording_segments ORDER BY id').fetchall(),
                [(1, 7), (2, 7)],
                'the retry correlated the segments instead of returning early')
            conn.close()

    def test_a_finished_backfill_is_not_run_again(self):
        with tempfile.TemporaryDirectory() as td:
            conn, cur = _scratch(td, *self._DDL)
            self._seed(conn, cur)
            M._m032_segment_channel_id(conn, cur)

            cur.execute('UPDATE recording_segments SET channel_id = NULL WHERE id = 2')
            conn.commit()
            M._m032_segment_channel_id(conn, cur)

            self.assertEqual(
                cur.execute('SELECT channel_id FROM recording_segments WHERE id = 2').fetchone(),
                (None,), 'a completed step does not redo its backfill')
            conn.close()


class M026ResumeTests(unittest.TestCase):
    """Migration 26 - channels.consecutive_test_failures. Its backfill is an ORM pass, so
    it is stubbed here; what is under test is whether the step calls it at all."""

    _DDL = ('CREATE TABLE channels (id INTEGER PRIMARY KEY, health_score FLOAT)',)

    def test_interrupted_backfill_is_finished_by_the_next_run(self):
        with tempfile.TemporaryDirectory() as td:
            conn, cur = _scratch(td, *self._DDL)

            with patch.object(M, '_backfill_consecutive_test_failures',
                              side_effect=_SimulatedCrash):
                with self.assertRaises(_SimulatedCrash):
                    M._m026_channel_failure_streak(conn, cur)

            with patch.object(M, '_backfill_consecutive_test_failures') as backfill:
                M._m026_channel_failure_streak(conn, cur)
            backfill.assert_called_once()

            with patch.object(M, '_backfill_consecutive_test_failures') as backfill:
                M._m026_channel_failure_streak(conn, cur)
            backfill.assert_not_called()
            conn.close()


class M001ResumeTests(unittest.TestCase):
    """Migration 1 - the pre-versioning baseline. Its three ORM backfills are the reason a
    data-shaped "already done" test is not enough: on a DB the old _migrate_db() mechanism
    already brought up to date, the columns legitimately pre-exist with their backfills
    long since run, so only a recorded obligation can tell the two states apart."""

    _DDL = (
        'CREATE TABLE accounts (id INTEGER PRIMARY KEY, name TEXT)',
        'CREATE TABLE channels (id INTEGER PRIMARY KEY, stream_url TEXT)',
        'CREATE TABLE recording_events (id INTEGER PRIMARY KEY, event_type VARCHAR(64))',
    )

    _BACKFILLS = ('_backfill_url_normalization', '_backfill_duplicate_flags',
                  '_backfill_health_scores')

    def _run(self, conn, cur, crash_on=None):
        """Run the step with all three backfills stubbed; returns their mocks."""
        with patch.object(M, self._BACKFILLS[0]) as url, \
                patch.object(M, self._BACKFILLS[1]) as dup, \
                patch.object(M, self._BACKFILLS[2]) as health:
            mocks = {'url': url, 'dup': dup, 'health': health}
            if crash_on:
                mocks[crash_on].side_effect = _SimulatedCrash
                with self.assertRaises(_SimulatedCrash):
                    M._m001_baseline(conn, cur)
            else:
                M._m001_baseline(conn, cur)
            return {k: v.call_count for k, v in mocks.items()}

    def test_a_crash_in_the_first_backfill_does_not_lose_the_other_two(self):
        with tempfile.TemporaryDirectory() as td:
            conn, cur = _scratch(td, *self._DDL)

            calls = self._run(conn, cur, crash_on='url')
            self.assertEqual(calls, {'url': 1, 'dup': 0, 'health': 0})
            self.assertIn('raw_stream_url',
                          {r[1] for r in cur.execute('PRAGMA table_info(channels)')},
                          'the columns were committed before the crash')

            calls = self._run(conn, cur)
            self.assertEqual(calls, {'url': 1, 'dup': 1, 'health': 1},
                             'the retry runs all three, not none of them')
            conn.close()

    def test_a_crash_in_the_last_backfill_only_repeats_that_one(self):
        with tempfile.TemporaryDirectory() as td:
            conn, cur = _scratch(td, *self._DDL)

            calls = self._run(conn, cur, crash_on='health')
            self.assertEqual(calls, {'url': 1, 'dup': 1, 'health': 1})

            calls = self._run(conn, cur)
            self.assertEqual(calls, {'url': 0, 'dup': 0, 'health': 1},
                             'the two that finished are done; only the interrupted one repeats')
            conn.close()

    def test_a_completed_baseline_backfills_nothing_on_a_re_run(self):
        with tempfile.TemporaryDirectory() as td:
            conn, cur = _scratch(td, *self._DDL)
            self._run(conn, cur)

            calls = self._run(conn, cur)
            self.assertEqual(calls, {'url': 0, 'dup': 0, 'health': 0})
            conn.close()

    def test_columns_that_already_existed_owe_nothing(self):
        """The pre-versioning DB the old mechanism already migrated: every column is there
        before version 1 runs, and re-deriving its scores and flags would be wrong work."""
        with tempfile.TemporaryDirectory() as td:
            conn, cur = _scratch(
                td,
                'CREATE TABLE accounts (id INTEGER PRIMARY KEY, name TEXT)',
                'CREATE TABLE channels (id INTEGER PRIMARY KEY, stream_url TEXT, '
                'raw_stream_url TEXT, is_duplicate_stream_url BOOLEAN NOT NULL DEFAULT 0, '
                'health_score FLOAT)',
                'CREATE TABLE recording_events (id INTEGER PRIMARY KEY, event_type VARCHAR(64))')

            calls = self._run(conn, cur)

            self.assertEqual(calls, {'url': 0, 'dup': 0, 'health': 0})
            conn.close()


class HealthScoreBackfillIdempotenceTests(unittest.TestCase):
    """_backfill_health_scores is the one backfill that folds each test into the channel's
    *current* score rather than recomputing it from scratch, so being re-run - which the
    ledger now makes possible, since completion is stamped in its own commit - would blend
    the same history in twice and invent a different number."""

    def test_a_channel_that_already_has_a_score_is_left_alone(self):
        t = make_test_app()
        try:
            with t.app.app_context():
                from app.database import Channel
                acct = seed.make_account()
                ch = seed.make_channel(acct, name='Already Scored', health_score=90)
                db.session.commit()
                seed.make_channel_test(ch, status='FAILED')
                db.session.commit()
                channel_id = ch.id

                M._backfill_health_scores()

                db.session.expire_all()
                self.assertEqual(db.session.get(Channel, channel_id).health_score, 90)
        finally:
            t.cleanup()

    def test_a_channel_with_no_score_is_still_backfilled(self):
        """The control: the filter must not be disabling the backfill outright."""
        t = make_test_app()
        try:
            with t.app.app_context():
                from app.database import Channel
                acct = seed.make_account()
                ch = seed.make_channel(acct, name='Never Scored')
                ch.health_score = None
                db.session.commit()
                seed.make_channel_test(ch, status='FAILED')
                db.session.commit()
                channel_id = ch.id

                M._backfill_health_scores()

                db.session.expire_all()
                self.assertIsNotNone(db.session.get(Channel, channel_id).health_score,
                                     'a never-scored channel with test history gets a score')
        finally:
            t.cleanup()


if __name__ == '__main__':
    unittest.main()
