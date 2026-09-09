"""Drop the fsync from the suite's throwaway SQLite databases.

SQLite's default `synchronous=FULL` fsyncs on every commit so a database survives a power
cut. A test database does not need to survive anything: make_test_app() creates it in a
temp dir and cleanup() deletes it seconds later. Profiling put `sqlite3.Connection.commit`
near the top of the suite's in-process time at ~13ms a call - that is the disk, not the
database. `synchronous=OFF` removes the fsync and leaves everything else about the engine
alone. Measured on the full suite: ~6s.

TEST-ONLY. The pragma is applied by a listener installed in this process, so production's
own engines (app/db_utils.py::configure_sqlite_pragmas) are untouched and keep FULL
durability - which they need, since dvr.db is real data.

What this does NOT change: journal mode stays WAL and busy_timeout stays 10s, both still
set by configure_sqlite_pragmas on every engine the app builds. synchronous is orthogonal
to both - it governs when SQLite waits for the filesystem, not how it journals or how it
behaves under contention. So the lock-contention tests (retry_on_locked, the concurrency
suite) still exercise exactly what they did before.
"""
import sys

_installed = False


def install():
    """Apply `PRAGMA synchronous=OFF` to every SQLAlchemy connection opened in this process.

    Returns True if installed. Like the other speed helpers here it never fails a run, but
    it does say so on stderr if it could not install (CLAUDE.md: nothing silent).
    """
    global _installed
    if _installed:
        return True

    try:
        from sqlalchemy import event
        from sqlalchemy.engine import Engine
    except ImportError as exc:
        print(f'[sqlitespeed] NOT installed: {exc}. The test suite will still run '
              f'correctly, just slower.', file=sys.stderr)
        return False

    @event.listens_for(Engine, 'connect')
    def _set_synchronous_off(dbapi_conn, connection_record):
        cursor = dbapi_conn.cursor()
        try:
            cursor.execute('PRAGMA synchronous=OFF')
        finally:
            cursor.close()

    _installed = True
    return True


def is_installed():
    return _installed
