"""Compile each SQL statement once per test process instead of once per test app.

SQLAlchemy turns a statement into SQL text the first time an engine runs it and keeps the
result, so a long-lived app compiles each of its statements once. A test app is not
long-lived: make_test_app() builds a new one for nearly every test, each with new engines,
and a new engine starts with nothing compiled. One cache handed to every engine is not
enough on its own, because an engine files what it compiles under its dialect object and
every engine builds its own dialect - a statement saved by one app's engine can never be
found by the next app's. So the first page a test asked for spent 71 of its 116 ms
compiling statements the test before it had compiled too, and all of it was garbage one
test later: 16,200 objects per app for the garbage collector, against 3,075 for an app
that rendered nothing (dev/changelog/1493).

Two things change, both through interfaces SQLAlchemy documents:

  * Every SQLite engine built with the same dialect arguments gets the same dialect
    object. `sqlalchemy.dialects.registry.register()` points the `sqlite` URL scheme at
    SharedSQLiteDialect, a subclass that changes nothing about the dialect except handing
    back the object it already built.
  * Every such engine files what it compiles in one process-wide cache, set through the
    `compiled_cache` execution option from the dialect's `engine_created()` hook.

With one dialect, the caches the ORM keeps on each model for INSERT, UPDATE and DELETE are
read back by the next app as well. Before this they had to be emptied every time an app
was built, or each finished app's dialect stayed in memory for the rest of the run
(dev/changelog/1492).

TEST-ONLY. Nothing in app/ is touched, and production gains nothing from it: it builds its
engines once and compiles each statement once anyway.

Why sharing is safe. A SQLite dialect holds what is true of every SQLite connection in
the process - the driver module, the SQLite version, the arguments it was built with - and
its `initialize()` writes the same values again on each engine's first connection. What
differs between apps (the database file, the pool, the pragma listeners) lives on the
engine, which is still built fresh. A compiled statement is SQL text plus how to read each
result column; the values a query is run with are handed over on every execution, never
taken from the cache. An engine built with different dialect arguments gets its own
dialect object and so can never read another one's statements. And production already
runs this way: one engine for the life of the process, every statement after the first a
cache hit. The suite used to test the first run of every statement and almost never the
second.

What would break it: a dialect that starts keeping something per engine, or a statement
whose cache key leaves out something its SQL depends on. The second is a real bug in
production too, and the suite now has a chance of catching it.
tests/test_sql_cache_shared.py asserts the sharing, the bound and the fidelity.
"""
import sys
import threading

from sqlalchemy import util as sa_util
from sqlalchemy.dialects.sqlite.pysqlite import SQLiteDialect_pysqlite

# How many compiled statements the process keeps: the size SQLAlchemy gives one engine.
# A compiled statement is about 680 objects the garbage collector walks on every full
# collection, so a larger cache costs memory and longer pauses inside tests. Measured on
# 3,091 tests in one process: 500 against 4,000 was 899 s against 889 s, 613 MB against
# 957 MB at the peak, and a longest pause of 0.94 s against 1.55 s (dev/changelog/1493).
CACHE_SIZE = 500

_installed = False
_lock = threading.Lock()
_dialects = {}
_statements = sa_util.LRUCache(CACHE_SIZE)


class SharedSQLiteDialect(SQLiteDialect_pysqlite):
    """The stock pysqlite dialect, built once per distinct set of arguments."""

    # SQLAlchemy turns statement caching off for any dialect subclass that does not set
    # this on itself, so inheriting it is not enough.
    supports_statement_cache = True

    def __new__(cls, **kwargs):
        try:
            key = frozenset(kwargs.items())
        except TypeError:
            # An argument that cannot be compared (a dict, say) gets a dialect of its
            # own: never shared, so never wrong.
            dialect = super().__new__(cls)
            SQLiteDialect_pysqlite.__init__(dialect, **kwargs)
            return dialect
        with _lock:
            dialect = _dialects.get(key)
            if dialect is None:
                dialect = super().__new__(cls)
                SQLiteDialect_pysqlite.__init__(dialect, **kwargs)
                _dialects[key] = dialect
        return dialect

    def __init__(self, **kwargs):
        # Built in __new__, under the lock. Python calls this on every construction, and
        # running the real __init__ again would reset a dialect other engines are using.
        pass

    @classmethod
    def engine_created(cls, engine):
        super().engine_created(engine)
        engine.update_execution_options(compiled_cache=_statements)


def install():
    """Point every `sqlite://` engine built from here on at the shared dialect and cache.

    Returns True if installed. Never raises and never fails a run: the suite is correct
    without it, just slower. If SQLAlchemy did not take the registration, that is said on
    stderr rather than left to show up as a slow run (CLAUDE.md: nothing silent).
    """
    global _installed
    if _installed:
        return True

    from sqlalchemy.dialects import registry
    from sqlalchemy.engine import make_url

    names = ('sqlite', 'sqlite.pysqlite')
    for name in names:
        registry.register(name, __name__, 'SharedSQLiteDialect')
    if make_url('sqlite://').get_dialect() is not SharedSQLiteDialect:
        for name in names:
            registry.deregister(name)
        print('[sqlcache] NOT installed: SQLAlchemy did not hand back the registered '
              'dialect for a sqlite:// URL. The test suite will still run correctly, just '
              'slower: every test app will compile its own SQL again, and the ORM will keep '
              'each finished app\'s dialect in memory to the end of the run '
              '(dev/changelog/1492).', file=sys.stderr)
        return False

    _installed = True
    return True


def is_installed():
    return _installed


def held():
    """How many compiled statements the shared cache holds right now."""
    return len(_statements)


def shared_dialects():
    """The dialect objects built so far, one per distinct set of arguments."""
    with _lock:
        return list(_dialects.values())
