"""tests/support/tablecheck.py must be invisible to everything except the clock.

create_app() calls db.create_all() on every start, and SQLAlchemy asks the database about
each of the models' tables in turn before creating the missing ones. The helper asks once,
and skips the call only when every table is there (dev/changelog/1494).

Two things are asserted.

That it is used: an app built from the schema template takes the short path, and a call
for particular binds does not.

The fidelity: on a database the short path was taken for, the real create_all() sends no
DDL and SQLAlchemy's own check finds every table; a table that is missing is created,
indexes and all; a database with no tables gets all of them; and a listener on the
metadata's create events, which SQLAlchemy calls even when nothing is created, is still
called.

If a test here goes red after a Flask-SQLAlchemy or SQLAlchemy upgrade, fix or delete the
helper, never relax the assertion. The suite is correct without the helper, just slower.
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from flask_sqlalchemy import SQLAlchemy  # noqa: E402
from sqlalchemy import event, inspect  # noqa: E402
from sqlalchemy.engine import Engine  # noqa: E402

from tests.support import tablecheck  # noqa: E402
from tests.support.app import make_test_app  # noqa: E402

from app import db  # noqa: E402


def _real_create_all():
    """SQLAlchemy's own create_all(), whether or not the helper is installed."""
    real = tablecheck._real_create_all or SQLAlchemy.create_all
    return real(db)


def _tables_in_database():
    with db.engine.connect() as conn:
        return {row[0] for row in conn.exec_driver_sql(
            "SELECT name FROM sqlite_master WHERE type = 'table'")}


def _indexes_in_database(table_name):
    return {index['name'] for index in inspect(db.engine).get_indexes(table_name)}


def _an_indexed_table():
    """A model table with an index of its own, the same one on every run."""
    for name in sorted(db.metadata.tables):
        table = db.metadata.tables[name]
        if any(index.name for index in table.indexes):
            return table
    raise AssertionError('no model table declares an index')


class _Statements:
    """The text of every statement any engine sends to SQLite while the block runs."""

    def __init__(self):
        self.sql = []

    def _record(self, conn, cursor, statement, parameters, context, executemany):
        self.sql.append(statement)

    def __enter__(self):
        event.listen(Engine, 'before_cursor_execute', self._record)
        return self

    def __exit__(self, *exc):
        event.remove(Engine, 'before_cursor_execute', self._record)

    @property
    def ddl(self):
        return [sql for sql in self.sql
                if sql.lstrip().upper().startswith(('CREATE', 'ALTER', 'DROP'))]


class TableCheckIsUsedTests(unittest.TestCase):

    def test_an_app_built_from_the_template_asks_once(self):
        before = dict(tablecheck.counts)
        testapp = make_test_app()
        self.addCleanup(testapp.cleanup)
        self.assertTrue(tablecheck.is_installed())
        self.assertEqual(tablecheck.counts['skipped'], before['skipped'] + 1)
        self.assertEqual(tablecheck.counts['real'], before['real'])

    def test_a_call_for_one_bind_goes_to_sqlalchemy(self):
        testapp = make_test_app()
        self.addCleanup(testapp.cleanup)
        before = dict(tablecheck.counts)
        db.create_all(bind_key=None)
        self.assertEqual(tablecheck.counts['real'], before['real'] + 1)
        self.assertEqual(tablecheck.counts['skipped'], before['skipped'])


class TableCheckFidelityTests(unittest.TestCase):

    def setUp(self):
        self.testapp = make_test_app()
        self.addCleanup(self.testapp.cleanup)

    def test_the_real_call_sends_no_ddl_where_the_short_path_was_taken(self):
        with _Statements() as seen:
            _real_create_all()
        self.assertEqual(seen.ddl, [])
        # It did look: one question per table is what the helper saves.
        self.assertGreaterEqual(len(seen.sql), len(db.metadata.tables))

    def test_sqlalchemy_finds_every_table_where_the_short_path_was_taken(self):
        inspector = inspect(db.engine)
        missing = [name for name in db.metadata.tables if not inspector.has_table(name)]
        self.assertEqual(missing, [])

    def test_a_missing_table_is_created_with_its_indexes(self):
        table = _an_indexed_table()
        wanted = {index.name for index in table.indexes if index.name}
        with db.engine.begin() as conn:
            conn.exec_driver_sql(f'DROP TABLE "{table.name}"')
        self.assertNotIn(table.name, _tables_in_database())

        with _Statements() as seen:
            db.create_all()

        self.assertIn(table.name, _tables_in_database())
        self.assertLessEqual(wanted, _indexes_in_database(table.name))
        created = [sql for sql in seen.ddl if sql.lstrip().upper().startswith('CREATE TABLE')]
        self.assertEqual(len(created), 1, created)

    def test_an_existing_table_is_never_altered(self):
        # create_all() adds nothing to a table that exists, with the helper or without
        # it: that is what makes leaving the call out safe.
        table = _an_indexed_table()
        index_name = sorted(index.name for index in table.indexes if index.name)[0]
        with db.engine.begin() as conn:
            conn.exec_driver_sql(f'DROP INDEX "{index_name}"')

        with _Statements() as seen:
            _real_create_all()

        self.assertEqual(seen.ddl, [])
        self.assertNotIn(index_name, _indexes_in_database(table.name))

    def test_a_listener_on_the_create_events_is_still_called(self):
        calls = []

        def _listener(target, connection, **kw):
            calls.append(kw.get('tables'))

        event.listen(db.metadata, 'before_create', _listener)
        try:
            before = dict(tablecheck.counts)
            db.create_all()
        finally:
            event.remove(db.metadata, 'before_create', _listener)

        self.assertEqual(calls, [[]])
        self.assertEqual(tablecheck.counts['skipped'], before['skipped'])


class FreshDatabaseTests(unittest.TestCase):

    def test_a_database_with_no_tables_gets_all_of_them(self):
        before = dict(tablecheck.counts)
        testapp = make_test_app(fresh_schema=True)
        self.addCleanup(testapp.cleanup)
        self.assertLessEqual(set(db.metadata.tables), _tables_in_database())
        self.assertEqual(tablecheck.counts['skipped'], before['skipped'])


if __name__ == '__main__':
    unittest.main()
