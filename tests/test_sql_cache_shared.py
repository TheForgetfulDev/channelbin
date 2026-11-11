"""tests/support/sqlcache.py: a test app reuses the SQL the apps before it compiled.

A test process builds a new app, with new engines, for nearly every test. SQLAlchemy files
each compiled statement under the dialect object of the engine that ran it, and every
engine used to build its own dialect, so every app compiled every statement it ran from
nothing and left all of it for the garbage collector one test later. The helper gives
every SQLite engine one dialect and one statement cache (dev/changelog/1493).

Three things are asserted.

The sharing: a second app doing the same work runs the very compiled statements the first
app made, the ORM's INSERT, UPDATE and DELETE included.

The bound: one dialect however many apps are built, neither the shared cache nor the ORM's
per-model caches any larger after more apps doing the same work, and a finished app's
engines really freed. The per-model caches once held every finished app's dialect for the
rest of the run, and full garbage collections cost 907 of the 3,984 seconds the four
workers spent inside tests (dev/changelog/1492). A process-wide cache that held on to an
engine would bring that back in a new place, and the engine check is what would catch it.

The fidelity: a page runs the same SQL, statement for statement, with the cache as
without it; a statement compiled under one app reads the next app's rows and takes the
next query's values; an engine built with different dialect arguments shares nothing; and
the stock dialect class other tests build by hand is untouched.

If a test here goes red after a SQLAlchemy upgrade, fix or delete the helper, never relax
the assertion. The suite is correct without the helper, just slower.
"""
import gc
import os
import sys
import unittest
import weakref

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import create_engine, event  # noqa: E402
from sqlalchemy.dialects import sqlite as sqlite_dialect  # noqa: E402
from sqlalchemy.engine import Engine  # noqa: E402

from tests.support import sqlcache  # noqa: E402
from tests.support.app import make_test_app  # noqa: E402
from tests.support.seed import make_account, make_channel, make_recording  # noqa: E402

from app import db  # noqa: E402
from app.database import Account, Channel  # noqa: E402

PAGES = ('/', '/recordings', '/channels')


class _Executions:
    """Every statement any engine sends to SQLite while the block runs.

    `sql` is the text of each one in order. `compiled` is the compiled statement behind
    each, where there is one: SQL handed to the driver as a plain string (a pragma, say)
    is never compiled. `cacheable` leaves out what SQLAlchemy never caches on any engine,
    SAVEPOINT for one.

    The collector is off while the block runs. A session finds an object it already
    loaded without asking the database, but only while the object is still alive, and two
    rows that point at each other stay alive until a collection happens to run. Left on,
    the same work could send one SELECT more on one run than on the next.
    """

    def __init__(self):
        self.sql = []
        self.compiled = []

    @property
    def cacheable(self):
        return [compiled for compiled in self.compiled if compiled.cache_key is not None]

    def _record(self, conn, cursor, statement, parameters, context, executemany):
        self.sql.append(statement)
        if context is not None and context.compiled is not None:
            self.compiled.append(context.compiled)

    def __enter__(self):
        self._gc_was_on = gc.isenabled()
        gc.disable()
        event.listen(Engine, 'before_cursor_execute', self._record)
        return self

    def __exit__(self, *exc):
        event.remove(Engine, 'before_cursor_execute', self._record)
        if self._gc_was_on:
            gc.enable()


def _seed():
    account = make_account()
    channel = make_channel(account, in_guide=True)
    make_recording(channel_id=channel.id)
    db.session.commit()
    return account


def _work(client):
    """Reads through the pages, then one INSERT, one UPDATE and one DELETE of each kind."""
    account = _seed()
    for page in PAGES:
        status = client.get(page).status_code
        if status != 200:
            raise AssertionError(f'{page} answered {status}')
    account.name = 'Renamed'
    extra = make_channel(account, name='Extra')
    db.session.commit()
    db.session.delete(extra)
    db.session.commit()


class SharingTests(unittest.TestCase):
    """The second app compiles nothing the first one already compiled."""

    def test_every_app_is_handed_the_same_dialect(self):
        self.assertTrue(
            sqlcache.is_installed(),
            'tests/__init__.py did not install sqlcache, or SQLAlchemy refused the '
            'registration - stderr at the top of the run says which')
        seen = []
        for _ in range(2):
            t = make_test_app()
            try:
                engines = list(db.engines.values())
                self.assertGreaterEqual(len(engines), 2, 'expected the UI and background pools')
                seen.extend(engine.dialect for engine in engines)
            finally:
                t.cleanup()
        self.assertEqual(len({id(dialect) for dialect in seen}), 1)
        self.assertIsInstance(seen[0], sqlcache.SharedSQLiteDialect)
        self.assertIn(seen[0], sqlcache.shared_dialects())

    def test_a_second_app_runs_the_statements_the_first_one_compiled(self):
        first = make_test_app()
        try:
            with _Executions() as cold:
                _work(first.client)
        finally:
            first.cleanup()
        held = sqlcache.held()
        self.assertGreater(held, 0, 'the first app left nothing in the shared cache')

        second = make_test_app()
        try:
            with _Executions() as warm:
                _work(second.client)
        finally:
            second.cleanup()

        self.assertGreater(len(warm.cacheable), 20,
                           'the work ran too little SQL to prove anything')
        self.assertEqual(warm.sql, cold.sql)
        known = {id(compiled) for compiled in cold.cacheable}
        recompiled = [str(compiled) for compiled in warm.cacheable if id(compiled) not in known]
        self.maxDiff = None
        self.assertEqual(
            recompiled, [],
            f'{len(recompiled)} of {len(warm.cacheable)} statements were compiled again by '
            f'the second app')
        self.assertEqual(sqlcache.held(), held)


class BoundTests(unittest.TestCase):
    """Nothing process-wide grows with the number of apps built."""

    def test_a_finished_apps_engines_are_freed(self):
        engines = []
        for _ in range(3):
            t = make_test_app()
            try:
                _work(t.client)
                engines.extend(weakref.ref(engine) for engine in db.engines.values())
                # The engine the session really wrote through: outside a request that is
                # the background pool's, not db.engine's.
                engines.append(weakref.ref(db.session.get_bind()))
            finally:
                t.cleanup()
            del t

        last = make_test_app()
        try:
            # A pool lets go of its connections through finalizers, so one pass frees an
            # engine's outer objects and only a later one reaches the rest.
            for _ in range(4):
                gc.collect()
            alive = [ref for ref in engines if ref() is not None]
            self.assertEqual(
                len(alive), 0,
                f'{len(alive)} of {len(engines)} engines of finished apps are still alive; '
                f'something process-wide is holding on to them')
        finally:
            last.cleanup()

    def test_more_apps_do_not_mean_more_dialects(self):
        t = make_test_app()
        t.cleanup()
        before = len(sqlcache.shared_dialects())
        for _ in range(3):
            t = make_test_app()
            try:
                _work(t.client)
            finally:
                t.cleanup()
        self.assertEqual(len(sqlcache.shared_dialects()), before)

    def test_the_per_model_caches_stop_growing(self):
        # SQLAlchemy keeps each model's INSERT, UPDATE and DELETE on the model's mapper,
        # not in the engine's cache, and files them under the dialect too. One entry per
        # app per statement here is the leak of dev/changelog/1492.
        def held():
            return sum(len(mapper._compiled_cache) for mapper in db.Model.registry.mappers)

        t = make_test_app()
        try:
            _work(t.client)
        finally:
            t.cleanup()
        before = held()
        self.assertGreater(before, 0, 'the work wrote nothing through the ORM')
        for _ in range(3):
            t = make_test_app()
            try:
                _work(t.client)
            finally:
                t.cleanup()
        self.assertEqual(held(), before)

    def test_the_cache_has_a_size(self):
        self.assertEqual(sqlcache._statements.capacity, sqlcache.CACHE_SIZE)


class FidelityTests(unittest.TestCase):
    """A cached statement does exactly what a freshly compiled one does."""

    def _page_sql(self, cached):
        t = make_test_app()
        try:
            if not cached:
                for engine in db.engines.values():
                    engine.update_execution_options(compiled_cache=None)
            _seed()
            with _Executions() as seen:
                for page in PAGES:
                    self.assertEqual(t.client.get(page).status_code, 200, page)
            return seen
        finally:
            t.cleanup()

    def test_a_page_runs_the_same_sql_with_the_cache_as_without_it(self):
        # Once first, so anything a process does only on its first request is behind us.
        self._page_sql(cached=True)
        fresh = self._page_sql(cached=False)
        cached = self._page_sql(cached=True)

        self.assertGreater(len(fresh.sql), 20)
        self.assertEqual(cached.sql, fresh.sql)
        # The comparison is only worth something if the two runs really differed.
        self.assertEqual(len({id(c) for c in fresh.compiled}), len(fresh.compiled),
                         'the run with the cache switched off reused a compiled statement')

    def test_a_cached_statement_reads_the_next_apps_rows(self):
        def named(name):
            return [a.name for a in Account.query.filter(Account.name == name).all()]

        first = make_test_app()
        try:
            make_account(name='Only in the first app')
            db.session.commit()
            self.assertEqual(named('Only in the first app'), ['Only in the first app'])
        finally:
            first.cleanup()

        second = make_test_app()
        try:
            make_account(name='Only in the second app')
            db.session.commit()
            self.assertEqual(named('Only in the first app'), [])
            self.assertEqual(named('Only in the second app'), ['Only in the second app'])
        finally:
            second.cleanup()

    def test_writes_through_cached_statements_land(self):
        for _ in range(2):
            t = make_test_app()
            try:
                account = make_account(name='Kept')
                channel = make_channel(account, name='Dropped')
                db.session.commit()
                account.name = 'Renamed'
                db.session.commit()
                db.session.delete(channel)
                db.session.commit()
                db.session.expire_all()

                self.assertEqual([a.name for a in Account.query.all()], ['Renamed'])
                self.assertEqual(Channel.query.count(), 0)
            finally:
                t.cleanup()

    def test_different_dialect_arguments_share_nothing(self):
        plain = create_engine('sqlite://')
        same = create_engine('sqlite://')
        other = create_engine('sqlite://', native_datetime=True)
        try:
            self.assertIs(plain.dialect, same.dialect)
            self.assertIsNot(plain.dialect, other.dialect)
            self.assertTrue(other.dialect.native_datetime)
            self.assertFalse(plain.dialect.native_datetime)
        finally:
            for engine in (plain, same, other):
                engine.dispose()

    def test_the_stock_dialect_class_is_untouched(self):
        # The migration tests compile DDL against sqlite.dialect() built by hand.
        one, two = sqlite_dialect.dialect(), sqlite_dialect.dialect()
        self.assertIsNot(one, two)
        self.assertNotIsInstance(one, sqlcache.SharedSQLiteDialect)


if __name__ == '__main__':
    unittest.main()
