"""Ask a test database once which tables it has, instead of once per table.

create_app() calls db.create_all() on every start, and SQLAlchemy answers "which of these
tables are missing" by asking the database about each one in turn: one PRAGMA per model,
54 of them, before it creates anything. A test app's database is a copy of the schema
template (tests/support/app.py::_template_db_path), which already holds every table, so
the 54 answers are always the same and nothing is ever created. That was 7.2 ms of the
43 ms an app took to build on the idle box, on every one of the ~7,000 apps a full run
builds (dev/changelog/1494).

install() wraps flask_sqlalchemy.SQLAlchemy.create_all. The wrapper reads the table names
out of `sqlite_master` in one query, and if every table the models declare is there it
returns, because the real call would have found the same thing and created nothing.
Everything else goes to the real create_all() untouched: a database missing even one
table (the schema template being built, a fresh_schema=True app, a migration test's
hand-made database), an engine that is not SQLite, a table with a schema name, a listener
on the metadata's create events (those fire even when nothing is created), or a caller
asking for particular binds.

TEST-ONLY. Nothing in app/ is touched. Production starts once, so the 54 questions cost
it a few milliseconds at boot and nothing after.

Why skipping is safe. create_all() never alters a table that exists: it does not add a
column, an index or a constraint to one, it only creates the tables that are absent
(adding to an existing table is what app/migrations.py is for). So when none are absent
it runs no DDL at all, and the wrapper leaves out nothing but the 54 questions. The
wrapper's test for "exists" is narrower than SQLAlchemy's, never wider: SQLAlchemy also
counts a temporary table of the same name, the wrapper only counts what is in the main
database and hands anything else to the real call.

tests/test_table_check_fidelity.py asserts all of it, including that the real
create_all() run against the same database emits no DDL.
"""
import sys

_real_create_all = None

# How many calls took each path, for the fidelity test to read.
counts = {'skipped': 0, 'real': 0}


def _every_table_exists(db):
    """True only when the real create_all() would find nothing to create."""
    metadatas = getattr(db, 'metadatas', None)
    if not metadatas:
        return False
    for key, metadata in metadatas.items():
        engine = db.engines.get(key)
        if engine is None or engine.dialect.name != 'sqlite':
            return False
        if metadata.dispatch.before_create or metadata.dispatch.after_create:
            return False
        wanted = set()
        for table in metadata.tables.values():
            if table.schema is not None:
                return False
            wanted.add(table.name)
        with engine.connect() as conn:
            have = {row[0] for row in conn.exec_driver_sql(
                "SELECT name FROM sqlite_master WHERE type IN ('table', 'view')")}
        if not wanted <= have:
            return False
    return True


def _create_all(self, bind_key='__all__'):
    if bind_key == '__all__' and _every_table_exists(self):
        counts['skipped'] += 1
        return None
    counts['real'] += 1
    return _real_create_all(self, bind_key)


def install():
    """Wrap SQLAlchemy.create_all. Returns True if installed.

    Never raises and never fails a run: the suite is correct without it, just slower. If
    Flask-SQLAlchemy is not what this was written against, that is said on stderr rather
    than left to show up as a slow run.
    """
    global _real_create_all
    if _real_create_all is not None:
        return True

    try:
        from flask_sqlalchemy import SQLAlchemy
        missing = [name for name in ('create_all', 'engines') if not hasattr(SQLAlchemy, name)]
    except ImportError as exc:
        missing = [str(exc)]
    if missing:
        print(f'[tablecheck] NOT installed: Flask-SQLAlchemy has changed ({missing}). The '
              f'test suite will still run correctly, just slower: every test app will ask '
              f'its database about each table in turn again.', file=sys.stderr)
        return False

    _real_create_all = SQLAlchemy.create_all
    SQLAlchemy.create_all = _create_all
    return True


def uninstall():
    """Put the real create_all() back. Used by the fidelity test."""
    global _real_create_all
    if _real_create_all is None:
        return
    from flask_sqlalchemy import SQLAlchemy
    SQLAlchemy.create_all = _real_create_all
    _real_create_all = None


def is_installed():
    return _real_create_all is not None
