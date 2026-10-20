"""A migrated database and a fresh build end up with the same schema (dev/changelog/1246).

create_all() builds every table in the CURRENT model's shape before the migration runner
starts, so on a real upgrade only the ALTER/UPDATE half of each step does anything, and on
a fresh database no step runs at all. Three things follow, and this file holds each:

  * Every registered step is a no-op against the current schema - run all of them, in
    order, on a fresh create_all() build and nothing in sqlite_master moves. Step 50 broke
    this quietly for a year: its `CREATE UNIQUE INDEX IF NOT EXISTS uq_health_exclusion`
    found no index under that name (the model's UniqueConstraint is backed by an autoindex)
    and added a second one, so every migrated database carried two. Step 95 drops it, and
    this test is what keeps the next one from shipping.
  * A step's hand-written CREATE TABLE is dead on every real database, so nothing noticed
    when two of them stopped matching the model (account_hosts.position and
    account_logins.position declared `DEFAULT 0` where the models had a Python-side default
    only). Each such body is compiled on a bare connection and diffed against the model.
  * A step's INSERT names a frozen column list against a table of the current model's
    shape, so a NOT NULL column added later with a Python-side default and no server_default
    aborts the upgrade on the constraint (dev/docs/BUGS.md 2026-08-16 @ 11:41:29 AM ET).
    Every literal INSERT in app/migrations.py is checked against the current model, not
    only migration 4's.

Runs against throwaway SQLite files - never the live dvr.db.
  python3 -m unittest tests.test_migration_parity
"""
import ast
import os
import re
import sqlite3
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy.schema import CreateTable  # noqa: E402
from sqlalchemy.dialects import sqlite as sqlite_dialect  # noqa: E402

from tests.support.app import make_test_app  # noqa: E402
from app import db  # noqa: E402
from app import migrations as M  # noqa: E402

MIGRATIONS_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                               'app', 'migrations.py')

#: Step 41 refuses any existing database that reaches it (dev/changelog/741), so no database
#: this app accepts has a version below it and steps 1-41 never run on a real upgrade. The
#: no-op check drives the steps a database can still cross: everything after the floor.
#: Steps below it are not merely dead, they are written against tables of that era (step
#: 10 writes channel_group_members.disabled_reason, which step 41's rebuild deleted).
REFUSAL_FLOOR = 41

#: Tables whose rows a step legitimately writes, or that the runner itself owns; their
#: CONTENT is not the schema under test, and the backfill ledger is created on demand.
_SCHEMA_NOISE_TABLES = {'migration_backfills', 'schema_migrations', 'sqlite_sequence'}


def _schema_snapshot(cur):
    """Every (type, name, tbl_name, sql) in sqlite_master, minus the runner's own tables
    and SQLite's internal rows, as a sorted list. The stored DDL text is what create_all()
    and a migrated database must agree on - not merely the column list."""
    rows = cur.execute(
        "SELECT type, name, tbl_name, sql FROM sqlite_master ORDER BY type, name").fetchall()
    return sorted(r for r in rows
                  if r[2] not in _SCHEMA_NOISE_TABLES and not r[1].startswith('sqlite_'))


class EveryStepIsANoOpOnTheCurrentSchemaTests(unittest.TestCase):

    def test_running_every_step_on_a_fresh_build_changes_nothing(self):
        t = make_test_app(fresh_schema=True)
        try:
            conn = db.engine.raw_connection()
            try:
                cur = conn.cursor()
                before = _schema_snapshot(cur)
                cfg = {'channel_testing': {'enabled': True, 'schedule_hour': 5, 'test_days': 3}}
                with mock.patch('app.config.load_config', return_value=cfg):
                    for version, _description, fn in M.SCHEMA_MIGRATIONS:
                        if version <= REFUSAL_FLOOR:
                            continue
                        with self.subTest(step=version):
                            fn(conn, cur)
                            conn.commit()
                after = _schema_snapshot(cur)
            finally:
                conn.close()
        finally:
            t.cleanup()
        self.assertEqual(
            before, after,
            'a migration step changed the schema of a database that was already in the '
            'current shape - a migrated database and a fresh build now differ')

    def test_the_floor_is_still_a_refusal(self):
        """The premise of skipping steps 1-41: step 41 raises on any existing database, so
        a database below it never runs them. If that ever softens, the floor moves."""
        version, _description, fn = M.SCHEMA_MIGRATIONS[REFUSAL_FLOOR - 1]
        self.assertEqual(version, REFUSAL_FLOOR)
        conn = sqlite3.connect(':memory:')
        try:
            cur = conn.cursor()
            cur.execute('CREATE TABLE channel_groups (id INTEGER PRIMARY KEY)')
            with self.assertRaises(SystemExit):
                fn(conn, cur)
        finally:
            conn.close()


# ---------------------------------------------------------------------------------------
# The hand-written CREATE TABLE bodies versus the models
# ---------------------------------------------------------------------------------------

_CREATE_TABLE_RE = re.compile(r'CREATE TABLE IF NOT EXISTS\s+(\w+)\s*\(', re.I)


def hand_written_create_tables(source):
    """[(table, ddl)] for every string literal in `source` that is a whole CREATE TABLE
    statement. Steps that build a table in pieces (f-strings over a loop) are not literal
    statements and are not here; none of the dead bodies is written that way."""
    found = []
    for node in ast.walk(ast.parse(source)):
        if not isinstance(node, ast.Constant) or not isinstance(node.value, str):
            continue
        text = node.value.strip()
        m = _CREATE_TABLE_RE.match(text)
        if m and text.endswith(')'):
            found.append((m.group(1), text))
    return found


def columns_added_later(source):
    """Every column name an ALTER TABLE ... ADD COLUMN in `source` adds, whether spelled in
    the statement or supplied by a loop over `(name, ddl)` tuples. A dead CREATE TABLE is
    the model as of its own step, so a model column one of these adds is expected to be
    absent from it."""
    names = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            for m in re.finditer(r'ADD COLUMN\s+(\w+)', node.value):
                names.add(m.group(1))
        elif isinstance(node, (ast.Tuple, ast.List)) and node.elts \
                and isinstance(node.elts[0], ast.Constant) \
                and isinstance(node.elts[0].value, str):
            names.add(node.elts[0].value)
    return names


def _columns_of(cur, table):
    """{name: (type, notnull, dflt_value)} for `table`, with the primary key left out of
    notnull: `id INTEGER PRIMARY KEY` reports notnull=0 where the model's `id INTEGER NOT
    NULL` plus a table PRIMARY KEY reports 1, and both are the same column."""
    out = {}
    for _cid, name, ctype, notnull, dflt, pk in cur.execute(f'PRAGMA table_info({table})'):
        out[name] = (' '.join(ctype.upper().split()), 0 if pk else notnull, dflt)
    return out


def _model_columns(table):
    import importlib
    importlib.import_module('app.database')
    tbl = db.metadata.tables[table]
    ddl = str(CreateTable(tbl).compile(dialect=sqlite_dialect.dialect()))
    conn = sqlite3.connect(':memory:')
    try:
        conn.execute(ddl)
        return _columns_of(conn.cursor(), table)
    finally:
        conn.close()


class MigrationCreateTableParityTests(unittest.TestCase):

    def _hand_written(self):
        with open(MIGRATIONS_PATH, encoding='utf-8') as fh:
            return hand_written_create_tables(fh.read())

    def test_every_hand_written_create_table_matches_the_model(self):
        offenders = []
        with open(MIGRATIONS_PATH, encoding='utf-8') as fh:
            added_later = columns_added_later(fh.read())
        for table, ddl in self._hand_written():
            if table not in db.metadata.tables and table != 'migration_backfills':
                offenders.append(f'{table}: no such model')
                continue
            if table == 'migration_backfills':
                continue   # the runner's own ledger, not a model
            conn = sqlite3.connect(':memory:')
            try:
                conn.execute(ddl)
                stepped = _columns_of(conn.cursor(), table)
            finally:
                conn.close()
            modeled = _model_columns(table)
            for name in sorted(set(stepped) | set(modeled)):
                if name not in modeled:
                    offenders.append(f'{table}.{name}: in the step, not in the model')
                elif name not in stepped:
                    if name in added_later:
                        continue   # a later ADD COLUMN step's column, not this vintage's
                    offenders.append(f'{table}.{name}: in the model, not in the step')
                elif stepped[name] != modeled[name]:
                    offenders.append(f'{table}.{name}: step says {stepped[name]}, '
                                     f'model says {modeled[name]}')
        self.assertEqual(offenders, [],
                         'a hand-written CREATE TABLE in app/migrations.py has drifted from '
                         'the model (it is dead on every real database, so nothing else '
                         'notices):\n' + '\n'.join(offenders))

    def test_the_scan_sees_the_dead_bodies(self):
        tables = {t for t, _ddl in self._hand_written()}
        for expected in ('channel_health_exclusions', 'account_hosts', 'account_logins',
                         'epg_sources'):
            self.assertIn(expected, tables)


class CreateTableScanCorrectnessTests(unittest.TestCase):

    def test_a_whole_statement_literal_is_found(self):
        src = ("def step(conn, cur):\n"
               "    cur.execute('''\n"
               "        CREATE TABLE IF NOT EXISTS widgets (\n"
               "            id INTEGER PRIMARY KEY,\n"
               "            name TEXT NOT NULL\n"
               "        )\n"
               "    ''')\n")
        self.assertEqual([t for t, _ in hand_written_create_tables(src)], ['widgets'])

    def test_a_create_table_mentioned_in_prose_is_not_a_statement(self):
        src = ("def step(conn, cur):\n"
               "    '''CREATE TABLE IF NOT EXISTS is how this used to be written.'''\n"
               "    cur.execute('ALTER TABLE widgets ADD COLUMN flag INTEGER')\n")
        self.assertEqual(hand_written_create_tables(src), [])


# ---------------------------------------------------------------------------------------
# Every literal INSERT supplies the current model's NOT NULL columns
# ---------------------------------------------------------------------------------------

_INSERT_RE = re.compile(r'INSERT(?:\s+OR\s+\w+)?\s+INTO\s+(\w+)\s*\(([^)]*)\)', re.I)


def literal_inserts(source):
    """[(table, [columns])] for every string literal INSERT in `source` whose column list
    is spelled out. A list built at run time (`({})`.format(...)) carries no names to check
    and is skipped - migration 4 derives its list from PRAGMA table_info on purpose."""
    found = []
    for node in ast.walk(ast.parse(source)):
        if not isinstance(node, ast.Constant) or not isinstance(node.value, str):
            continue
        for m in _INSERT_RE.finditer(node.value):
            cols = [c.strip() for c in m.group(2).split(',') if c.strip()]
            if cols and all(re.fullmatch(r'\w+', c) for c in cols):
                found.append((m.group(1), cols))
    return found


def _required_columns(table):
    """The current model's columns an INSERT must name: NOT NULL, no SQL default, and not
    the primary key (SQLite supplies that)."""
    tbl = db.metadata.tables[table]
    return sorted(c.name for c in tbl.columns
                  if not c.nullable and c.server_default is None and not c.primary_key)


class MigrationInsertVintageTests(unittest.TestCase):

    def _inserts(self):
        with open(MIGRATIONS_PATH, encoding='utf-8') as fh:
            return literal_inserts(fh.read())

    def test_every_literal_insert_supplies_the_current_models_not_null_columns(self):
        import importlib
        importlib.import_module('app.database')
        offenders = []
        for table, cols in self._inserts():
            if table not in db.metadata.tables:
                continue   # the runner's own ledger, or a step's temporary table
            missing = sorted(set(_required_columns(table)) - set(cols))
            if missing:
                offenders.append(f'{table}: INSERT omits NOT NULL column(s) {missing}')
        self.assertEqual(offenders, [],
                         'a migration INSERT would die on the constraint against a table '
                         'create_all() built in the current shape, aborting the upgrade:\n'
                         + '\n'.join(offenders))

    def test_the_scan_sees_the_three_steps_the_rule_was_written_for(self):
        tables = [t for t, _c in self._inserts()]
        for expected in ('on_demand_test_jobs', 'epg_sources', 'epg_source_subscriptions'):
            self.assertIn(expected, tables)


class InsertScanCorrectnessTests(unittest.TestCase):

    def test_a_split_literal_is_read_whole(self):
        src = ("cur.execute('INSERT INTO epg_sources (kind, name, '\n"
               "            'owner_account_id) VALUES (?, ?, ?)')\n")
        self.assertEqual(literal_inserts(src),
                         [('epg_sources', ['kind', 'name', 'owner_account_id'])])

    def test_a_derived_column_list_is_skipped(self):
        src = "cur.execute('INSERT INTO on_demand_test_jobs ({}) VALUES ({})'.format(a, b))\n"
        self.assertEqual(literal_inserts(src), [])

    def test_a_missing_required_column_is_reported(self):
        self.assertIn('name', _required_columns('epg_sources'))


# ---------------------------------------------------------------------------------------
# Migration 95: the duplicate unique index
# ---------------------------------------------------------------------------------------

def _unique_indexes(cur, table):
    return [(row[1], row[3]) for row in cur.execute(f'PRAGMA index_list({table})') if row[2]]


class DuplicateExclusionIndexTests(unittest.TestCase):

    def test_step_95_drops_the_second_index_a_migrated_database_carries(self):
        t = make_test_app()
        try:
            conn = db.engine.raw_connection()
            try:
                cur = conn.cursor()
                M._m050_channel_health_exclusions(conn, cur)
                conn.commit()
                self.assertEqual(len(_unique_indexes(cur, 'channel_health_exclusions')), 2,
                                 'step 50 should have added the second index on a build '
                                 'whose table constraint already has an autoindex')
                M._m095_drop_duplicate_exclusion_index(conn, cur)
                remaining = _unique_indexes(cur, 'channel_health_exclusions')
                self.assertEqual(len(remaining), 1)
                self.assertEqual(remaining[0][1], 'u', 'the table constraint stays')
                # Re-runnable from the top.
                M._m095_drop_duplicate_exclusion_index(conn, cur)
                self.assertEqual(len(_unique_indexes(cur, 'channel_health_exclusions')), 1)
            finally:
                conn.close()
        finally:
            t.cleanup()

    def test_step_95_keeps_the_only_index_a_bare_table_has(self):
        """On a table step 50's own CREATE TABLE built there is no table constraint, so the
        named index is the only uniqueness there is."""
        td = tempfile.TemporaryDirectory()
        conn = sqlite3.connect(os.path.join(td.name, 'scratch.db'))
        try:
            cur = conn.cursor()
            M._m050_channel_health_exclusions(conn, cur)
            conn.commit()
            M._m095_drop_duplicate_exclusion_index(conn, cur)
            self.assertEqual(_unique_indexes(cur, 'channel_health_exclusions'),
                             [('uq_health_exclusion', 'c')])
        finally:
            conn.close()
            td.cleanup()


# ---------------------------------------------------------------------------------------
# Migration 96: the stored default of channel_groups.format_strategy
# ---------------------------------------------------------------------------------------

class ChannelGroupsDefaultRebuildTests(unittest.TestCase):

    def _current_build(self):
        """(create table sql, [index sql]) for channel_groups from a real create_all()."""
        t = make_test_app()
        try:
            rows = db.session.execute(db.text(
                "SELECT type, sql FROM sqlite_master WHERE tbl_name='channel_groups' "
                "AND sql IS NOT NULL ORDER BY type DESC, name")).fetchall()
        finally:
            t.cleanup()
        table = [sql for kind, sql in rows if kind == 'table'][0]
        return table, [sql for kind, sql in rows if kind == 'index']

    def _scratch(self, table_sql, index_sql):
        td = tempfile.TemporaryDirectory()
        conn = sqlite3.connect(os.path.join(td.name, 'scratch.db'))
        cur = conn.cursor()
        cur.execute(table_sql)
        for sql in index_sql:
            cur.execute(sql)
        cur.execute("INSERT INTO channel_groups (name, in_guide, format_strategy) "
                    "VALUES ('Sports', 1, 'highest_score')")
        conn.commit()
        return td, conn, cur

    def test_the_retired_default_is_rewritten_and_everything_else_kept(self):
        table_sql, index_sql = self._current_build()
        self.assertIn(M._CURRENT_STRATEGY_DEFAULT, table_sql)
        aged = table_sql.replace(M._CURRENT_STRATEGY_DEFAULT, M._RETIRED_STRATEGY_DEFAULT)
        td, conn, cur = self._scratch(aged, index_sql)
        try:
            M._m096_channel_groups_strategy_default(conn, cur)
            stored = cur.execute("SELECT sql FROM sqlite_master WHERE type='table' AND "
                                 "name='channel_groups'").fetchone()[0]
            self.assertIn(M._CURRENT_STRATEGY_DEFAULT, stored)
            self.assertNotIn(M._RETIRED_STRATEGY_DEFAULT, stored)
            self.assertEqual(cur.execute('SELECT name, format_strategy FROM channel_groups')
                             .fetchall(), [('Sports', 'highest_score')])
            carried = {r[0] for r in cur.execute(
                "SELECT name FROM sqlite_master WHERE type='index' AND tbl_name='channel_groups' "
                "AND sql IS NOT NULL")}
            self.assertEqual(carried, {re.search(r'INDEX (\w+)', s).group(1) for s in index_sql})
            # A raw INSERT that omits the column now mints the current default.
            cur.execute("INSERT INTO channel_groups (name, in_guide) VALUES ('News', 1)")
            self.assertEqual(cur.execute("SELECT format_strategy FROM channel_groups "
                                         "WHERE name='News'").fetchone()[0], 'highest_score')
        finally:
            conn.close()
            td.cleanup()

    def test_a_table_already_carrying_the_current_default_is_left_alone(self):
        table_sql, index_sql = self._current_build()
        td, conn, cur = self._scratch(table_sql, index_sql)
        try:
            before = cur.execute("SELECT sql FROM sqlite_master WHERE tbl_name='channel_groups' "
                                 "ORDER BY name").fetchall()
            M._m096_channel_groups_strategy_default(conn, cur)
            after = cur.execute("SELECT sql FROM sqlite_master WHERE tbl_name='channel_groups' "
                                "ORDER BY name").fetchall()
        finally:
            conn.close()
            td.cleanup()
        self.assertEqual(before, after)


if __name__ == '__main__':
    unittest.main(verbosity=2)
