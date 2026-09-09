"""The WAL size limit (database.wal_size_limit_mb) reaches every connection, and bounds
retention WITHOUT ever constraining a write.

Guards dev/changelog/424 and dev/docs/BUGS.md 2026-08-01 @ 12:5x. Production shipped on
SQLite's `journal_size_limit = -1`, "never truncate", so dvr.db-wal sat at 2758.7MB against a
1598.6MB database with only 1.2MB of it live - the high-water mark of a single 16-minute
incident, kept forever. Nothing in the tree bounded it; on this box the ceiling was 41GB of
free disk.

The failure modes this file exists for:

  * setting the pragma in only one of the two places configure_sqlite_pragmas() writes
    (the connect listener AND the already-pooled connection it primes) - a silent half-fix,
    since which one you get depends on pool state,
  * configuring the Flask-SQLAlchemy engines but not APScheduler's separate jobstore engine,
    which writes the same file - journal_size_limit is per-CONNECTION, so a WAL bounded on two
    engines is still unbounded whenever the third happens to do the commit,
  * and the one that would do real damage: **a future editor reading this setting as a quota
    and tuning it down to "save space".** It is not a quota. A transaction grows the WAL to
    whatever it needs and commits, however far past the limit that goes. If that ever stops
    being true, a large EPG sync starts failing on a small setting.
"""
import logging
import os
import unittest

from sqlalchemy import create_engine, text

from app import db
from app.config import load_config
from app.db_utils import (BACKGROUND_BIND, DEFAULT_WAL_SIZE_LIMIT_MB,
                          _wal_size_limit_pragma_value, configure_sqlite_pragmas,
                          current_wal_size_bytes, wal_size_bytes)
from tests.support import make_test_app

MB = 1024 * 1024


def _limit(conn):
    return conn.execute(text('PRAGMA journal_size_limit')).scalar()


class PragmaValueTests(unittest.TestCase):
    """The MB -> bytes conversion, and the deliberate 0-means-unlimited escape hatch."""

    def test_positive_mb_becomes_bytes(self):
        self.assertEqual(_wal_size_limit_pragma_value(256), 256 * MB)
        self.assertEqual(_wal_size_limit_pragma_value(8), 8 * MB)

    def test_zero_means_no_limit(self):
        """0 is a real choice (the pre-2026-08-01 behavior), not a typo to be corrected.

        Unlike cache_size, where 0 would silently disable the page cache, -1 here is exactly
        what SQLite ships with - so it is passed through rather than replaced by the default.
        """
        self.assertEqual(_wal_size_limit_pragma_value(0), -1)

    def test_garbage_and_negatives_fall_back_to_the_default(self):
        expected = DEFAULT_WAL_SIZE_LIMIT_MB * MB
        for bad in (-5, None, 'two-fifty-six', ''):
            with self.subTest(bad=bad):
                with self.assertLogs('app.db_utils', level=logging.WARNING):
                    self.assertEqual(_wal_size_limit_pragma_value(bad), expected)


class NotAQuotaTests(unittest.TestCase):
    """The invariant a future editor must not break: the limit never constrains a write.

    Measured behavior, not an assumption - see the step 0 table in dev/changelog/424. If this
    goes red, the setting has been turned into a quota and a large EPG sync will start failing
    on a small value.
    """

    def setUp(self):
        self.t = make_test_app()

    def tearDown(self):
        self.t.cleanup()

    def test_a_transaction_far_larger_than_the_limit_still_commits(self):
        engine = create_engine('sqlite:///' + self.t.db_path)
        try:
            configure_sqlite_pragmas(engine, cache_size_mb=8, wal_size_limit_mb=1)
            engine.dispose()
            with engine.connect() as conn:
                self.assertEqual(_limit(conn), 1 * MB)
                conn.exec_driver_sql('PRAGMA wal_autocheckpoint=0')  # isolate the mechanism
                conn.exec_driver_sql('CREATE TABLE big (id INTEGER PRIMARY KEY, blob BLOB)')
                conn.commit()
                payload = b'x' * 4000
                for _ in range(5000):
                    conn.exec_driver_sql('INSERT INTO big (blob) VALUES (?)', (payload,))
                conn.commit()
                self.assertEqual(
                    conn.exec_driver_sql('SELECT count(*) FROM big').scalar(), 5000,
                    'every row of a transaction ~20x the limit must be committed')
                self.assertGreater(
                    wal_size_bytes(self.t.db_path), 4 * MB,
                    'the WAL must be free to grow past the limit while a write needs it')
        finally:
            engine.dispose()

    def test_a_wal_under_the_limit_is_never_truncated(self):
        """No churn in steady state: under the limit, the file is left completely alone.

        This is the other half of "generous default". A limit low enough to bite on every
        cycle would trade unbounded growth for permanent truncate-then-re-extend work.
        """
        engine = create_engine('sqlite:///' + self.t.db_path)
        try:
            configure_sqlite_pragmas(engine, cache_size_mb=8, wal_size_limit_mb=64)
            engine.dispose()
            with engine.connect() as conn:
                conn.exec_driver_sql('CREATE TABLE small (id INTEGER PRIMARY KEY, v TEXT)')
                conn.exec_driver_sql("INSERT INTO small (v) VALUES ('x')")
                conn.commit()
                sizes = []
                for _ in range(4):
                    conn.exec_driver_sql("INSERT INTO small (v) VALUES ('y')")
                    conn.commit()
                    conn.exec_driver_sql('PRAGMA wal_checkpoint(PASSIVE)')
                    conn.commit()
                    sizes.append(wal_size_bytes(self.t.db_path))
                self.assertLess(max(sizes), 64 * MB)
                self.assertEqual(len(set(sizes)), 1,
                                 f'WAL size churned under the limit: {sizes}')
        finally:
            engine.dispose()


class EngineConfigurationTests(unittest.TestCase):
    """Both places configure_sqlite_pragmas() writes pragmas must set journal_size_limit."""

    def setUp(self):
        self.t = make_test_app()

    def tearDown(self):
        self.t.cleanup()

    def test_connections_opened_after_configuration_carry_it(self):
        engine = create_engine('sqlite:///' + self.t.db_path)
        try:
            configure_sqlite_pragmas(engine, cache_size_mb=8, wal_size_limit_mb=8)
            engine.dispose()  # force a genuinely new physical connection
            with engine.connect() as conn:
                self.assertEqual(_limit(conn), 8 * MB)
        finally:
            engine.dispose()

    def test_the_already_pooled_connection_carries_it_too(self):
        """The half-fix guard: a connection that predates the listener still gets it.

        journal_size_limit is per-connection and not stored in the database file, so a
        connection that misses it stays unbounded for its whole pooled life.
        """
        engine = create_engine('sqlite:///' + self.t.db_path)
        try:
            with engine.connect() as conn:
                _limit(conn)  # opens a physical connection, pooled on release
            configure_sqlite_pragmas(engine, cache_size_mb=8, wal_size_limit_mb=8)
            with engine.connect() as conn:
                self.assertEqual(_limit(conn), 8 * MB)
        finally:
            engine.dispose()


class AppEngineTests(unittest.TestCase):
    """Both of the app's own engines honor database.wal_size_limit_mb."""

    def test_configured_value_reaches_both_pools(self):
        t = make_test_app(extra_overrides={'database': {'wal_size_limit_mb': 8}})
        try:
            for bind in (None, BACKGROUND_BIND):
                with self.subTest(bind=bind):
                    with db.engines[bind].connect() as conn:
                        self.assertEqual(_limit(conn), 8 * MB)
        finally:
            t.cleanup()

    def test_default_is_not_sqlites_never_truncate(self):
        t = make_test_app()
        try:
            expected = load_config()['database']['wal_size_limit_mb'] * MB
            with db.engine.connect() as conn:
                self.assertEqual(_limit(conn), expected)
                self.assertNotEqual(_limit(conn), -1,
                                    'a -1 here is the unbounded SQLite default the WAL '
                                    'policy replaced')
        finally:
            t.cleanup()


class JobstoreEngineTests(unittest.TestCase):
    """APScheduler's separate engine gets it too, not as a special case.

    It talks to the same dvr.db as a near-constant writer. Because journal_size_limit is a
    per-connection setting, leaving this engine out would mean the WAL is bounded only when
    one of the other two engines happens to be the one committing.
    """

    def test_jobstore_engine_carries_the_configured_limit(self):
        t = make_test_app(start_scheduler=True,
                          extra_overrides={'database': {'wal_size_limit_mb': 8}})
        try:
            from app.scheduler import get_scheduler
            jobstore = get_scheduler()._jobstores['default']
            with jobstore.engine.connect() as conn:
                self.assertEqual(_limit(conn), 8 * MB)
        finally:
            t.cleanup()


class WalSizeHelperNoAppTests(unittest.TestCase):
    """The size probe's degenerate inputs. Deliberately builds no app - current_wal_size_bytes
    is called from log lines that must not raise when there is nothing to report on."""

    def test_missing_wal_file_reads_as_zero(self):
        self.assertEqual(wal_size_bytes('/nonexistent/path/to/nothing.db'), 0)

    def test_zero_outside_an_app_context(self):
        self.assertEqual(current_wal_size_bytes(), 0)


class WalSizeHelperTests(unittest.TestCase):
    """The size probe used by the daily maintenance job and the two attribution log lines."""

    def setUp(self):
        self.t = make_test_app()

    def tearDown(self):
        self.t.cleanup()

    def test_reports_the_test_apps_wal_not_productions(self):
        """The sandbox-escape guard (CLAUDE.md, BUGS.md 2026-07-18).

        current_wal_size_bytes() must resolve the path from app.config. A runtime
        load_config() would read the real config.yaml and report on the production dvr.db
        from inside a test - the same defect that once had test runs writing APScheduler
        rows into the live database.
        """
        db.session.execute(text('SELECT 1'))
        db.session.commit()
        self.assertTrue(self.t.db_path.startswith(self.t._tmpdir),
                        'the temp DB must not be the production one')
        self.assertEqual(current_wal_size_bytes(), wal_size_bytes(self.t.db_path))
        production_db = load_config()['database']['path']
        self.assertNotEqual(os.path.abspath(production_db),
                            os.path.abspath(self.t.db_path))


class MaintenanceJobTests(unittest.TestCase):
    """The daily WAL sub-task measures always and repairs only when over the limit.

    An unconditional nightly wal_checkpoint(TRUNCATE) would be the bug, not the thoroughness:
    it takes a healthy few-MB WAL to zero and makes the next day re-extend it from scratch.
    """

    def test_under_the_limit_it_logs_and_does_not_truncate(self):
        t = make_test_app(extra_overrides={'database': {'wal_size_limit_mb': 64}})
        try:
            from app.scheduler import _wal_maintenance
            db.session.execute(text('SELECT 1'))
            db.session.commit()
            before = wal_size_bytes(t.db_path)
            with self.assertLogs('app.scheduler', level=logging.INFO) as cm:
                _wal_maintenance(t.app)
            self.assertTrue(any('nothing to reclaim' in m for m in cm.output), cm.output)
            self.assertEqual(wal_size_bytes(t.db_path), before,
                             'a WAL under the limit must be left exactly as it was')
        finally:
            t.cleanup()

    def test_over_the_limit_it_truncates_and_warns(self):
        # 0MB limit so any WAL at all is "over", without having to write hundreds of MB.
        t = make_test_app(extra_overrides={'database': {'wal_size_limit_mb': 1}})
        try:
            from app.scheduler import _wal_maintenance
            # Force the file past the 1MB limit with real WAL frames.
            with db.engine.connect() as conn:
                conn.exec_driver_sql('PRAGMA wal_autocheckpoint=0')
                conn.exec_driver_sql('CREATE TABLE pad (id INTEGER PRIMARY KEY, blob BLOB)')
                conn.commit()
                payload = b'x' * 4000
                for _ in range(600):
                    conn.exec_driver_sql('INSERT INTO pad (blob) VALUES (?)', (payload,))
                conn.commit()
            self.assertGreater(wal_size_bytes(t.db_path), 1 * MB,
                               'setup failed to grow the WAL past the limit')
            with self.assertLogs('app.scheduler', level=logging.WARNING) as cm:
                _wal_maintenance(t.app)
            self.assertTrue(any('truncated to' in m for m in cm.output), cm.output)
            self.assertLess(wal_size_bytes(t.db_path), 1 * MB)
        finally:
            t.cleanup()


if __name__ == '__main__':
    unittest.main()
