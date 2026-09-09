"""Tier 2: the two connection pools, and the guarantee that UI traffic cannot starve
background work of a connection.

Guards dev/docs/BUGS.md 2026-08-01 (the sync-window search stampede's pool half). On
2026-08-01 there was one pool, sized by nobody - SQLAlchemy's 5 + 10 default applied because
no `pool_size`, `max_overflow` or `pool_timeout` appeared anywhere in the tree - and every
consumer drew from it first-come-first-served. Unindexed search scans held all fifteen
connections long enough that the account sync died outright:

    Sync failed for account 3: QueuePool limit of size 5 overflow 10 reached

The alert write that would have reported it failed the same way, so the worst symptom of the
whole incident was also its quietest. `dev/changelog/423` gives background work its own pool
so that is structurally impossible; these are the tests that say so.

Measured before and after with a reproduction holding every UI connection from request threads
and then asking a background-context session for one: **before, it failed after 30.0s with the
message above; after, it got one in 0.0s.** `BackgroundIsNotStarvedTests` is that reproduction.
"""
import os
import sys
import threading
import time
import unittest

from sqlalchemy import text

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tests.support.app import make_test_app  # noqa: E402
from app import db  # noqa: E402
from app.config import load_config  # noqa: E402
from app.db_utils import BACKGROUND_BIND, POOL_WARN_INTERVAL_SECONDS  # noqa: E402


class _PoolTestCase(unittest.TestCase):

    def setUp(self):
        self.t = make_test_app()

    def tearDown(self):
        self.t.cleanup()

    @staticmethod
    def ceiling(engine):
        return engine.pool.size() + engine.pool._max_overflow


class ConfiguredSizesReachTheEnginesTests(_PoolTestCase):
    """The numbers in config.yaml are the numbers the pools actually have.

    Not a tautology: before dev/changelog/423 there was no setting at all, and the way that
    stayed invisible for months is that nothing ever asserted the pool was anything in
    particular. A future edit that drops SQLALCHEMY_ENGINE_OPTIONS, or sets it after
    db.init_app() (which Flask-SQLAlchemy ignores - it builds the engines in init_app), puts
    the accidental default straight back with no other symptom.
    """

    def test_the_ui_pool_matches_the_configured_numbers(self):
        cfg = load_config()['database']
        pool = db.engines[None].pool
        self.assertEqual(pool.size(), cfg['pool_size'])
        self.assertEqual(pool._max_overflow, cfg['max_overflow'])
        self.assertEqual(pool._timeout, cfg['pool_timeout'])

    def test_the_background_pool_matches_the_configured_numbers(self):
        cfg = load_config()['database']
        pool = db.engines[BACKGROUND_BIND].pool
        self.assertEqual(pool.size(), cfg['background_pool_size'])
        self.assertEqual(pool._max_overflow, cfg['background_max_overflow'])
        self.assertEqual(pool._timeout, cfg['background_pool_timeout'])

    def test_the_background_pool_waits_longer_than_the_ui_pool(self):
        """A sync would rather be late than fail; a stuck request should fail loudly.

        The ordering is the design, not a coincidence of two numbers, so it is asserted as an
        ordering - it survives someone retuning both.

        Characterization, not a regression guard: it reads config.yaml's defaults, so it
        passes with the wiring reverted. What it protects is a later edit that retunes the
        two numbers into the wrong order.
        """
        cfg = load_config()['database']
        self.assertGreater(cfg['background_pool_timeout'], cfg['pool_timeout'])

    def test_both_pools_address_the_same_database_file(self):
        """Two pools, one dvr.db. A second bind pointed at a different file would be a whole
        second database that half the app writes to, which is not what this is."""
        self.assertEqual(str(db.engines[BACKGROUND_BIND].url), str(db.engines[None].url))
        self.assertIn(self.t.db_path, str(db.engines[BACKGROUND_BIND].url))


class PragmasReachBothEnginesTests(_PoolTestCase):
    """WAL, busy_timeout and the page cache are per CONNECTION, so a second engine does not
    inherit them - it has to be armed too.

    busy_timeout is the one that bites: SQLite's default is 0, meaning fail immediately on a
    locked database, and the background pool is where the account sync and the index rebuild
    write from. Leaving that engine unconfigured would put the fail-fast default on exactly
    the writers that most need to wait a lock out, and `retry_on_locked` would be papering
    over a 10-second cushion that was never installed.
    """

    def _pragmas(self, engine):
        with engine.connect() as conn:
            return {
                'journal_mode': conn.execute(text('PRAGMA journal_mode')).scalar(),
                'busy_timeout': conn.execute(text('PRAGMA busy_timeout')).scalar(),
                'cache_size': conn.execute(text('PRAGMA cache_size')).scalar(),
            }

    def test_the_background_engine_carries_the_same_pragmas_as_the_ui_engine(self):
        expected_cache = -(load_config()['database']['cache_size_mb'] * 1024)
        for label, engine in (('UI', db.engines[None]),
                              ('background', db.engines[BACKGROUND_BIND])):
            with self.subTest(engine=label):
                pragmas = self._pragmas(engine)
                self.assertEqual(pragmas['journal_mode'], 'wal')
                self.assertEqual(pragmas['busy_timeout'], 10000)
                self.assertEqual(pragmas['cache_size'], expected_cache)


class RoutingTests(_PoolTestCase):
    """One sentence, asserted: no request context means the background pool."""

    def test_work_with_no_request_behind_it_uses_the_background_pool(self):
        """A scheduler job, the account sync, the index rebuild, a recorder thread - none of
        them has a request context, and this is the single check that sends all of them to
        the pool UI traffic cannot drain."""
        self.assertIs(db.session.get_bind(), db.engines[BACKGROUND_BIND])

    def test_a_request_uses_the_ui_pool(self):
        """Precondition, not a guard - it passes with the routing reverted, because the
        default engine is what a request got before any of this existed. It is here so the
        pair above and below reads as a complete statement of the rule."""
        with self.t.app.test_request_context('/'):
            self.assertIs(db.session.get_bind(), db.engines[None])

    def test_an_explicitly_passed_bind_always_wins(self):
        """`bind=` means the caller already chose. Overriding an explicit choice would break
        SQLAlchemy's own internals, which pass one.

        Precondition: passes with the routing reverted too. It fails only against a future
        get_bind() that reroutes unconditionally."""
        chosen = db.engines[None]
        self.assertIs(db.session.get_bind(bind=chosen), chosen)

    def test_routing_survives_leaving_a_request(self):
        """Between transactions the decision is taken fresh. A scoped session outlives any
        single request context, and a permanently pinned one would send later background work
        to the UI pool (or worse, a later request's work to the background one)."""
        with self.t.app.test_request_context('/'):
            self.assertIs(db.session.get_bind(), db.engines[None])
        self.assertIs(db.session.get_bind(), db.engines[BACKGROUND_BIND])

    def test_one_transaction_never_spans_both_engines(self):
        """Inside an open transaction the first answer is reused, even if a request context is
        entered part-way through.

        This is not tidiness, it is correctness: rows flushed but not yet committed live on
        the connection the session opened, and a second connection cannot see them. Re-deciding
        mid-transaction made `tests/support/seed.py` rows - which flush without committing,
        deliberately - invisible to the request meant to render them.
        """
        db.session.execute(text('SELECT 1'))          # opens the transaction, outside a request
        self.assertTrue(db.session().in_transaction())
        with self.t.app.test_request_context('/'):
            self.assertIs(db.session.get_bind(), db.engines[BACKGROUND_BIND])
        db.session.rollback()
        with self.t.app.test_request_context('/'):
            self.assertIs(db.session.get_bind(), db.engines[None])

    def test_uncommitted_rows_are_visible_to_a_request_in_the_same_transaction(self):
        """The behavior the rule above exists for, asserted end to end rather than through
        get_bind(): flush without committing, then read through a request context."""
        from app.database import ChannelGroup

        db.session.add(ChannelGroup(name='pinned-transaction-probe'))
        db.session.flush()
        with self.t.app.test_request_context('/'):
            found = ChannelGroup.query.filter_by(name='pinned-transaction-probe').first()
        self.assertIsNotNone(found, 'a flushed-but-uncommitted row vanished inside a request')
        db.session.rollback()

    def test_a_query_run_outside_a_request_really_lands_on_the_background_engine(self):
        """get_bind() is the decision; this is the decision actually being used. Counted at
        the engine, so a future change that resolves the bind correctly and then executes
        somewhere else still fails here."""
        seen = []

        from sqlalchemy import event

        def on_execute(conn, cursor, statement, parameters, context, executemany):
            seen.append(statement)

        event.listen(db.engines[BACKGROUND_BIND], 'before_cursor_execute', on_execute)
        try:
            db.session.execute(text('SELECT COUNT(*) FROM channels')).scalar()
        finally:
            event.remove(db.engines[BACKGROUND_BIND], 'before_cursor_execute', on_execute)
        self.assertTrue(seen, 'no statement ran on the background engine')


class BackgroundIsNotStarvedTests(_PoolTestCase):
    """The incident itself, reproduced: every UI connection held, and a sync still gets one.

    This is the test the whole change exists for. With one pool it fails the way the real
    thing did - `QueuePool limit of size 5 overflow 10 reached` after the full pool_timeout -
    and no amount of tuning the single pool's size fixes it, because a UI burst can always be
    one request bigger than whatever headroom was left.
    """

    def test_a_background_session_gets_a_connection_while_the_ui_pool_is_full(self):
        ui = db.engines[None]
        ceiling = self.ceiling(ui)
        full = threading.Event()
        release = threading.Event()
        held = []
        lock = threading.Lock()

        def hog():
            # A request thread holding its connection while it works - a search scan, in the
            # incident. test_request_context so the routing sends it to the UI pool for the
            # same reason production would.
            with self.t.app.test_request_context('/api/channels/search?q=x'):
                conn = ui.connect()
                try:
                    conn.execute(text('SELECT 1'))
                    with lock:
                        held.append(conn)
                        if len(held) >= ceiling:
                            full.set()
                    release.wait(30)
                finally:
                    conn.close()

        threads = [threading.Thread(target=hog, daemon=True) for _ in range(ceiling)]
        try:
            for thread in threads:
                thread.start()
            self.assertTrue(full.wait(20), 'could not fill the UI pool')
            self.assertEqual(ui.pool.checkedout(), ceiling)

            outcome = {}

            def background():
                # No request context, exactly like the APScheduler sync job that died.
                with self.t.app.app_context():
                    started = time.monotonic()
                    try:
                        db.session.execute(text('SELECT COUNT(*) FROM channels')).scalar()
                        outcome['seconds'] = time.monotonic() - started
                    except Exception as exc:  # noqa: BLE001 - the failure IS the assertion
                        outcome['error'] = f'{type(exc).__name__}: {exc}'
                    finally:
                        db.session.remove()

            worker = threading.Thread(target=background, daemon=True)
            worker.start()
            worker.join(timeout=30)

            self.assertNotIn('error', outcome,
                             f'background work was starved of a connection: '
                             f'{outcome.get("error")}')
            self.assertIn('seconds', outcome, 'background work never finished')
            # Immediately, not eventually. A background session that merely outwaits the UI
            # burst has not been reserved anything - it got lucky, which is the state this
            # replaced.
            self.assertLess(outcome['seconds'], 5.0)
        finally:
            release.set()
            for thread in threads:
                thread.join(timeout=10)


class ExhaustionIsAnnouncedTests(_PoolTestCase):
    """A full pool says which pool it is, before anything fails.

    On 2026-08-01 the only trace of the pool running dry was a generic
    `Sync failed for account 3: ...` several layers up - the failure was observable, the
    *cause* was not, and telling them apart took an investigation. CLAUDE.md's
    failure-paths-must-be-observable rule applied to a resource rather than to a state.
    """

    def _fill_and_capture(self, engine, ceiling):
        with self.assertLogs('app.db_utils', level='WARNING') as captured:
            conns = [engine.connect() for _ in range(ceiling)]
            for conn in conns:
                conn.close()
        return captured.output

    def test_filling_the_ui_pool_logs_a_warning_naming_it(self):
        lines = self._fill_and_capture(db.engines[None], self.ceiling(db.engines[None]))
        self.assertTrue(any('UI database connection pool is full' in line for line in lines),
                        lines)

    def test_filling_the_background_pool_logs_a_warning_naming_it(self):
        engine = db.engines[BACKGROUND_BIND]
        lines = self._fill_and_capture(engine, self.ceiling(engine))
        self.assertTrue(
            any('background database connection pool is full' in line for line in lines),
            lines)

    def test_the_warning_does_not_repeat_on_every_checkout(self):
        """The condition persists for as long as the pool stays full, so an unthrottled line
        would be its own denial of service - it would flood dvr.log at exactly the moment the
        log is the only thing left to read."""
        engine = db.engines[None]
        ceiling = self.ceiling(engine)
        with self.assertLogs('app.db_utils', level='WARNING') as captured:
            for _ in range(3):
                conns = [engine.connect() for _ in range(ceiling)]
                for conn in conns:
                    conn.close()
        full_lines = [line for line in captured.output if 'pool is full' in line]
        self.assertEqual(len(full_lines), 1, captured.output)
        self.assertGreater(POOL_WARN_INTERVAL_SECONDS, 0)

    def test_an_unfull_pool_says_nothing(self):
        """Checking out fewer than the ceiling is ordinary operation. A warning there would
        train the reader to ignore the one that matters.

        Precondition: with no listener installed at all this passes trivially. It guards the
        opposite defect from its siblings - a threshold set too low, or dropped entirely."""
        engine = db.engines[None]
        # Measured from what is already checked out, not from zero: this app context's own
        # session is holding one, and a count typed as `ceiling - 1` would quietly become
        # `ceiling` and assert the opposite of what it says.
        room = self.ceiling(engine) - engine.pool.checkedout()
        conns = [engine.connect() for _ in range(room - 2)]
        try:
            with self.assertNoLogs('app.db_utils', level='WARNING'):
                extra = engine.connect()
                self.assertLess(engine.pool.checkedout(), self.ceiling(engine))
                extra.close()
        finally:
            for conn in conns:
                conn.close()


if __name__ == '__main__':
    unittest.main()
