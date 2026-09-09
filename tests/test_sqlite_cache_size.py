"""SQLite page cache (database.cache_size_mb) reaches every connection of every engine.

Guards dev/changelog/363. Production ran on SQLite's stock 2MB page cache against an
832MB database - never a considered choice, and worth 89ms -> 38ms on channel search and
1.4s -> 0.6s on EPG deep search once raised. The failure modes this file exists for:

  * setting the pragma in only one of the two places configure_sqlite_pragmas() writes
    (the connect listener AND the already-pooled connection it primes) - a silent
    half-fix, since which one you get depends on pool state,
  * configuring the main Flask-SQLAlchemy engine but not APScheduler's separate jobstore
    engine, which reads and writes the same file,
  * passing a bad config value straight through to SQLite, where cache_size=0 disables
    the page cache outright rather than erroring.
"""
import logging
import unittest

from sqlalchemy import create_engine, text

from app import db
from app.config import load_config
from app.db_utils import (DEFAULT_CACHE_SIZE_MB, _cache_size_pragma_value,
                          configure_sqlite_pragmas)
from tests.support import make_test_app


def _cache_size(conn):
    return conn.execute(text('PRAGMA cache_size')).scalar()


class PragmaValueTests(unittest.TestCase):
    """The MB -> SQLite-units conversion, where the sign carries the unit."""

    def test_positive_mb_becomes_negative_kib(self):
        # Negative means KiB; a POSITIVE 65536 would mean 65,536 *pages* (256MB at this
        # database's 4096-byte page_size), so the sign is load-bearing.
        self.assertEqual(_cache_size_pragma_value(64), -65536)
        self.assertEqual(_cache_size_pragma_value(8), -8192)

    def test_non_positive_and_garbage_fall_back_to_the_default(self):
        expected = -(DEFAULT_CACHE_SIZE_MB * 1024)
        for bad in (0, -5, None, 'sixty-four', ''):
            with self.subTest(bad=bad):
                # cache_size=0 disables the page cache; passing a typo through would turn
                # a config slip into a silent order-of-magnitude slowdown.
                with self.assertLogs('app.db_utils', level=logging.WARNING):
                    self.assertEqual(_cache_size_pragma_value(bad), expected)


class EngineConfigurationTests(unittest.TestCase):
    """Both places configure_sqlite_pragmas() writes pragmas must set cache_size."""

    def setUp(self):
        self.t = make_test_app()

    def tearDown(self):
        self.t.cleanup()

    def test_connections_opened_after_configuration_carry_it(self):
        engine = create_engine('sqlite:///' + self.t.db_path)
        try:
            configure_sqlite_pragmas(engine, cache_size_mb=8)
            engine.dispose()  # force a genuinely new physical connection
            with engine.connect() as conn:
                self.assertEqual(_cache_size(conn), -8192)
        finally:
            engine.dispose()

    def test_the_already_pooled_connection_carries_it_too(self):
        """The half-fix guard: a connection that predates the listener still gets it.

        The 'connect' listener only fires for new physical connections, so an engine that
        already has one pooled would keep the 2MB default until that connection happened
        to be recycled. That is why configure_sqlite_pragmas() also primes the live
        connection directly.
        """
        engine = create_engine('sqlite:///' + self.t.db_path)
        try:
            with engine.connect() as conn:
                _cache_size(conn)  # opens a physical connection, pooled on release
            configure_sqlite_pragmas(engine, cache_size_mb=8)
            with engine.connect() as conn:
                self.assertEqual(_cache_size(conn), -8192)
        finally:
            engine.dispose()


class AppEngineTests(unittest.TestCase):
    """The app's own engine honors database.cache_size_mb."""

    def test_configured_value_reaches_the_main_engine(self):
        t = make_test_app(extra_overrides={'database': {'cache_size_mb': 8}})
        try:
            with db.engine.connect() as conn:
                self.assertEqual(_cache_size(conn), -8192)
        finally:
            t.cleanup()

    def test_default_is_not_sqlites_2mb(self):
        t = make_test_app()
        try:
            expected = -(load_config()['database']['cache_size_mb'] * 1024)
            with db.engine.connect() as conn:
                self.assertEqual(_cache_size(conn), expected)
                self.assertNotEqual(_cache_size(conn), -2000)
        finally:
            t.cleanup()


class JobstoreEngineTests(unittest.TestCase):
    """APScheduler's separate engine gets the same treatment, not a special case.

    It talks to the same dvr.db as a near-constant writer, and configure_sqlite_pragmas()
    is applied to it for exactly that reason - a new pragma that reaches only the main
    engine leaves half the connections against this file on the old default.
    """

    def test_jobstore_engine_carries_the_configured_cache_size(self):
        t = make_test_app(start_scheduler=True,
                          extra_overrides={'database': {'cache_size_mb': 8}})
        try:
            import app.scheduler as sched
            engine = sched._scheduler._lookup_jobstore('default').engine
            with engine.connect() as conn:
                self.assertEqual(_cache_size(conn), -8192)
        finally:
            t.cleanup()


if __name__ == '__main__':
    unittest.main()
