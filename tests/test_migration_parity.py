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


def _statements(literal):
    """The statements of one string literal, split on the `;` between them: a literal holding
    one statement comes back as itself, an executescript body as each statement it runs."""
    return [s.strip() for s in literal.split(';') if s.strip()]


def hand_written_create_tables(source):
    """[(table, ddl)] for every CREATE TABLE statement in a string literal of `source` - a
    literal that is one statement, or any statement of an executescript body. The sports
    steps (112 onward) are executescripts, and a scan that read only whole-literal statements
    saw none of them (dev/docs/BUGS.md 2026-11-09). Steps that build a table in pieces
    (f-strings over a loop) are not literal statements and are not here; none of the dead
    bodies is written that way."""
    found = []
    for node in ast.walk(ast.parse(source)):
        if not isinstance(node, ast.Constant) or not isinstance(node.value, str):
            continue
        for text in _statements(node.value):
            m = _CREATE_TABLE_RE.match(text)
            if m and text.endswith(')'):
                found.append((m.group(1), text))
    return found


def _iterable_strings(iterable, pos, tree):
    """The strings a `for` loop's literal iterable binds at `pos` of each element (-1: the
    element itself). A name stands for the module-level tuple or list assigned to it."""
    if isinstance(iterable, ast.Name):
        for n in ast.walk(tree):
            if isinstance(n, ast.Assign) and any(
                    isinstance(t, ast.Name) and t.id == iterable.id for t in n.targets):
                iterable = n.value
                break
    if not isinstance(iterable, (ast.Tuple, ast.List)):
        return set()
    out = set()
    for e in iterable.elts:
        if pos >= 0 and isinstance(e, (ast.Tuple, ast.List)) and len(e.elts) > pos:
            e = e.elts[pos]
        elif pos >= 0:
            continue
        if isinstance(e, ast.Constant) and isinstance(e.value, str):
            out.add(e.value)
    return out


def _loop_values(node, name, parents, tree):
    """The strings the `for` loop enclosing `node` binds to `name`."""
    scope = parents.get(node)
    while scope is not None:
        if isinstance(scope, ast.For):
            target, pos = scope.target, None
            if isinstance(target, ast.Name) and target.id == name:
                pos = -1
            elif isinstance(target, ast.Tuple):
                pos = next((k for k, t in enumerate(target.elts)
                            if isinstance(t, ast.Name) and t.id == name), None)
            if pos is not None:
                return _iterable_strings(scope.iter, pos, tree)
        scope = parents.get(scope)
    return set()


def columns_added_later(source):
    """Every column name an ALTER TABLE ... ADD COLUMN in `source` adds: spelled in the
    statement, or bound by the loop around an f-string's `ADD COLUMN {name}` from the loop's
    literal iterable - a tuple of bare names, or of `(name, ddl)` pairs, or a module-level
    tuple it names. Only those loops: a string tuple that feeds no ADD COLUMN (a step's own
    table of column defaults) names nothing a dead CREATE TABLE may lack. A dead CREATE
    TABLE is the model as of its own step, so a model column one of these adds is expected
    to be absent from it."""
    tree = ast.parse(source)
    parents = {child: node for node in ast.walk(tree) for child in ast.iter_child_nodes(node)}
    names = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            names.update(re.findall(r'ADD COLUMN\s+(\w+)', node.value))
            for placeholder in re.findall(r'ADD COLUMN\s+\{(\w+)\}', node.value):
                names |= _loop_values(node, placeholder, parents, tree)
        elif isinstance(node, ast.JoinedStr):
            for i, part in enumerate(node.values[:-1]):
                following = node.values[i + 1]
                if (isinstance(part, ast.Constant) and isinstance(part.value, str)
                        and re.search(r'ADD COLUMN\s*$', part.value)
                        and isinstance(following, ast.FormattedValue)
                        and isinstance(following.value, ast.Name)):
                    names |= _loop_values(node, following.value.id, parents, tree)
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
        import importlib
        importlib.import_module('app.database')   # populates db.metadata when run alone
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
                         'epg_sources',
                         # executescript bodies: several statements in one literal
                         'sports_leagues', 'sports_events', 'sports_images'):
            self.assertIn(expected, tables)


class AddedLaterScanCorrectnessTests(unittest.TestCase):
    """columns_added_later() reads the loops that add columns and nothing else: a version
    that harvested every string tuple in the file excused 24 columns of the sports dead
    bodies through the step's own table of defaults (dev/docs/BUGS.md 2026-11-09)."""

    def test_bare_names_pairs_and_a_named_tuple(self):
        src = ("NEW = (('late', 'TEXT'), ('later', 'INTEGER'))\n"
               "def step(conn, cur):\n"
               "    for column in ('poster_url', 'thumb_url'):\n"
               "        cur.execute(f'ALTER TABLE t ADD COLUMN {column} TEXT')\n"
               "    for name, ddl in (('a', 'TEXT'), ('b', 'INTEGER')):\n"
               "        cur.execute(f'ALTER TABLE t ADD COLUMN {name} {ddl}')\n"
               "    for col, definition in NEW:\n"
               "        cur.execute('ALTER TABLE t ADD COLUMN {col} {definition}'.format(\n"
               "            col=col, definition=definition))\n"
               "    cur.execute('ALTER TABLE t ADD COLUMN spelled INTEGER')\n")
        self.assertEqual(columns_added_later(src),
                         {'poster_url', 'thumb_url', 'a', 'b', 'late', 'later', 'spelled'})

    def test_a_string_tuple_that_adds_nothing_names_nothing(self):
        src = ("DEFAULTS = (('widgets', 'tries', '0'), ('widgets', 'names', \"'[]'\"))\n"
               "def step(conn, cur):\n"
               "    for table, column, literal in DEFAULTS:\n"
               "        cur.execute(f'UPDATE {table} SET {column} = {literal}')\n")
        self.assertEqual(columns_added_later(src), set())


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

    def test_each_create_table_of_an_executescript_body_is_found(self):
        """One literal, several statements: both tables, and the index between them is not
        one (dev/docs/BUGS.md 2026-11-09)."""
        src = ('def step(conn, cur):\n'
               '    cur.executescript("""\n'
               '        CREATE TABLE IF NOT EXISTS widgets (\n'
               '            id INTEGER PRIMARY KEY,\n'
               '            tries INTEGER NOT NULL DEFAULT 0\n'
               '        );\n'
               '        CREATE INDEX IF NOT EXISTS ix_widgets_tries ON widgets (tries);\n'
               '        CREATE TABLE IF NOT EXISTS gadgets (\n'
               '            id INTEGER PRIMARY KEY\n'
               '        );\n'
               '    """)\n')
        found = hand_written_create_tables(src)
        self.assertEqual([t for t, _ in found], ['widgets', 'gadgets'])
        self.assertTrue(found[0][1].startswith('CREATE TABLE IF NOT EXISTS widgets'))
        self.assertTrue(found[0][1].endswith(')'))


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

class PositionDefaultRebuildTests(unittest.TestCase):
    """Migration 114 (dev/docs/BUGS.md 2026-11-05 @ 07:46 PM): an install that crossed steps 84
    and 85 before dev/changelog/1246 kept `position INTEGER NOT NULL` on account_hosts and
    account_logins, where a fresh build has DEFAULT 0. A raw INSERT omitting the column -
    what a future migration step is - aborted there and passed the suite."""

    TABLES = ('account_hosts', 'account_logins')
    # The stored DDL of a database that crossed 84/85 early, as prod's reads.
    AGED = re.compile(r'(position\s+INTEGER)\s+DEFAULT 0\s+NOT NULL')

    def _current_build(self):
        t = make_test_app()
        try:
            return {table: db.session.execute(db.text(
                "SELECT sql FROM sqlite_master WHERE type='table' AND name=:t"),
                {'t': table}).scalar() for table in self.TABLES}
        finally:
            t.cleanup()

    def _scratch(self, ddl):
        td = tempfile.TemporaryDirectory()
        self.addCleanup(td.cleanup)
        conn = sqlite3.connect(os.path.join(td.name, 'scratch.db'))
        self.addCleanup(conn.close)
        cur = conn.cursor()
        for sql in ddl.values():
            cur.execute(sql)
        cur.execute("INSERT INTO account_hosts (account_id, host, position, is_active, "
                    "created_at) VALUES (1, 'a.example', 3, 1, '2026-01-01')")
        cur.execute("INSERT INTO account_logins (account_id, login_id, position) "
                    "VALUES (1, 2, 4)")
        conn.commit()
        return conn, cur

    def _stored(self, cur):
        # SQLite quotes the name it rewrites on RENAME; nothing else in the DDL differs.
        return {table: cur.execute("SELECT sql FROM sqlite_master WHERE type='table' AND "
                                   "name=?", (table,)).fetchone()[0]
                .replace(f'CREATE TABLE "{table}"', f'CREATE TABLE {table}')
                for table in self.TABLES}

    def test_both_tables_gain_the_default_and_keep_everything_else(self):
        fresh = self._current_build()
        aged = {}
        for table, sql in fresh.items():
            self.assertRegex(sql, self.AGED, 'a fresh build carries the default')
            aged[table] = self.AGED.sub(r'\1 NOT NULL', sql)
            self.assertNotEqual(aged[table], sql)
        conn, cur = self._scratch(aged)
        with self.assertRaises(sqlite3.IntegrityError):
            cur.execute("INSERT INTO account_logins (account_id, login_id) VALUES (5, 6)")
        conn.rollback()

        M._m114_position_defaults(conn, cur)

        self.assertEqual(self._stored(cur), fresh, 'a migrated table reads as a fresh one')
        self.assertEqual(cur.execute('SELECT host, position, is_active FROM account_hosts')
                         .fetchall(), [('a.example', 3, 1)])
        self.assertEqual(cur.execute('SELECT account_id, login_id, position FROM '
                                     'account_logins').fetchall(), [(1, 2, 4)])
        # The raw INSERT a later migration step would write.
        cur.execute("INSERT INTO account_logins (account_id, login_id) VALUES (5, 6)")
        cur.execute("INSERT INTO account_hosts (account_id, host, created_at) "
                    "VALUES (1, 'b.example', '2026-01-01')")
        self.assertEqual(cur.execute('SELECT position FROM account_logins WHERE account_id=5')
                         .fetchone()[0], 0)
        self.assertEqual(cur.execute("SELECT position FROM account_hosts WHERE "
                                     "host='b.example'").fetchone()[0], 0)
        # The unique constraint came across with the table.
        with self.assertRaises(sqlite3.IntegrityError):
            cur.execute("INSERT INTO account_hosts (account_id, host, created_at) "
                        "VALUES (1, 'a.example', '2026-01-01')")

    def test_tables_already_carrying_the_default_are_left_alone(self):
        fresh = self._current_build()
        conn, cur = self._scratch(fresh)
        M._m114_position_defaults(conn, cur)
        self.assertEqual(self._stored(cur), fresh)
        self.assertEqual(cur.execute("SELECT name FROM sqlite_master WHERE name LIKE "
                                     "'%default_new'").fetchall(), [])

    def test_a_run_that_stopped_after_the_first_table_finishes_the_second(self):
        fresh = self._current_build()
        half = dict(fresh)
        half['account_logins'] = self.AGED.sub(r'\1 NOT NULL', fresh['account_logins'])
        conn, cur = self._scratch(half)
        M._m114_position_defaults(conn, cur)
        self.assertEqual(self._stored(cur), fresh)

    def test_the_hand_written_ddl_of_steps_84_and_85_is_left_alone(self):
        """A database that crossed 84 and 85 after dev/changelog/1246 carries
        `NOT NULL DEFAULT 0`, the other order. It has its default and is not rebuilt."""
        td = tempfile.TemporaryDirectory()
        self.addCleanup(td.cleanup)
        conn = sqlite3.connect(os.path.join(td.name, 'scratch.db'))
        self.addCleanup(conn.close)
        cur = conn.cursor()
        M._m084_account_hosts(conn, cur)
        M._m085_logins(conn, cur)
        before = self._stored(cur)
        M._m114_position_defaults(conn, cur)
        self.assertEqual(self._stored(cur), before)


class SportsDefaultRebuildTests(unittest.TestCase):
    """Migration 118 (dev/docs/BUGS.md 2026-11-09): a database whose sports tables came from
    create_all() between migration 112 and the models' server_defaults - every install that
    crossed 112 before 2026-11-07, fresh or upgraded, since create_all() builds a new table
    before the step's dead CREATE TABLE runs - has NOT NULL columns with no DEFAULT where a
    build made since carries `DEFAULT 0` or `DEFAULT '[]'`. The rebuild gives each column its
    default, derived from the stored DDL so every other character is kept."""

    # A fresh build spells each defaulted column `name TYPE DEFAULT x NOT NULL`.
    FRESH_DEFAULT = re.compile(r"\s+DEFAULT\s+('[^']*'|\S+)(?=\s+NOT NULL)")

    @classmethod
    def _tables(cls):
        return sorted({table for table, _col, _lit in M._SPORTS_DEFAULTS})

    def _current_build(self):
        """{table: (create sql, [index sql])} from a real create_all()."""
        t = make_test_app()
        try:
            out = {}
            for table in self._tables():
                rows = db.session.execute(db.text(
                    "SELECT type, sql FROM sqlite_master WHERE tbl_name=:t AND sql IS NOT NULL "
                    "ORDER BY type DESC, name"), {'t': table}).fetchall()
                out[table] = ([sql for kind, sql in rows if kind == 'table'][0],
                              [sql for kind, sql in rows if kind == 'index'])
            return out
        finally:
            t.cleanup()

    def _aged(self, table_sql, columns):
        """`table_sql` with the DEFAULT stripped from each of `columns`, as a fresh build made
        before the models declared them wrote it."""
        for column in columns:
            table_sql, n = re.subn(rf'(\b{column}\s+[A-Z]+(?:\(\d+\))?)' + self.FRESH_DEFAULT.pattern,
                                   r'\1', table_sql, count=1)
            self.assertEqual(n, 1, f'{column}: no `DEFAULT x NOT NULL` to strip in\n{table_sql}')
        return table_sql

    def _scratch(self, build):
        td = tempfile.TemporaryDirectory()
        self.addCleanup(td.cleanup)
        conn = sqlite3.connect(os.path.join(td.name, 'scratch.db'))
        self.addCleanup(conn.close)
        cur = conn.cursor()
        for table_sql, index_sql in build.values():
            cur.execute(table_sql)
            for sql in index_sql:
                cur.execute(sql)
        cur.execute("INSERT INTO sports_leagues (tsdb_id, name, match_terms) "
                    "VALUES (4387, 'NBA', '[\"nba\"]')")
        cur.execute("INSERT INTO sports_fetches (target, kind, league_id, dropped_count) "
                    "VALUES ('league:4387', 'league', 1, 2)")
        conn.commit()
        return conn, cur

    def _stored(self, cur):
        # SQLite quotes the name it rewrites on RENAME; nothing else in the DDL differs.
        return {table: cur.execute("SELECT sql FROM sqlite_master WHERE type='table' AND "
                                   "name=?", (table,)).fetchone()[0]
                .replace(f'CREATE TABLE "{table}"', f'CREATE TABLE {table}')
                for table in self._tables()}

    def _fresh_tables(self, build):
        return {table: sql for table, (sql, _idx) in build.items()}

    def test_the_list_is_what_the_models_declare(self):
        """The step's table is hand-written so it stays frozen; this holds it equal to the
        sports models, so a sports column gaining a server_default lands in the step too."""
        import importlib
        importlib.import_module('app.database')
        declared = set()
        for name, tbl in db.metadata.tables.items():
            if name.startswith('sports_') or name == 'recording_sports':
                for c in tbl.columns:
                    if c.server_default is not None:
                        declared.add((name, c.name, str(c.server_default.arg.text)))
        self.assertEqual(set(M._SPORTS_DEFAULTS), declared)
        self.assertEqual(len(M._SPORTS_DEFAULTS), len(set(M._SPORTS_DEFAULTS)))

    def test_every_table_gains_its_defaults_and_keeps_everything_else(self):
        build = self._current_build()
        fresh = self._fresh_tables(build)
        aged = {}
        for table in self._tables():
            columns = [c for t, c, _l in M._SPORTS_DEFAULTS if t == table]
            aged[table] = (self._aged(fresh[table], columns), build[table][1])
            self.assertNotEqual(aged[table][0], fresh[table])
        conn, cur = self._scratch(aged)
        with self.assertRaises(sqlite3.IntegrityError):
            cur.execute("INSERT INTO sports_leagues (tsdb_id, name) VALUES (4328, 'EPL')")
        conn.rollback()

        M._m118_sports_defaults(conn, cur)

        self.assertEqual(self._stored(cur), fresh, 'a migrated table reads as a fresh one')
        self.assertEqual(cur.execute('SELECT tsdb_id, name, match_terms FROM sports_leagues')
                         .fetchall(), [(4387, 'NBA', '["nba"]')])
        self.assertEqual(cur.execute('SELECT target, dropped_count FROM sports_fetches')
                         .fetchall(), [('league:4387', 2)])
        # The raw INSERT a later migration step would write now mints the default.
        cur.execute("INSERT INTO sports_leagues (tsdb_id, name) VALUES (4328, 'EPL')")
        self.assertEqual(cur.execute("SELECT match_terms FROM sports_leagues WHERE tsdb_id=4328")
                         .fetchone()[0], '[]')
        cur.execute("INSERT INTO sports_state DEFAULT VALUES")
        self.assertEqual(cur.execute("SELECT backoff_seconds, clean_calls, daily_offset_seconds "
                                     "FROM sports_state").fetchone(), (0, 0, 0))
        # Indexes and the unique constraints came across with the tables.
        carried = {r[0] for r in cur.execute(
            "SELECT name FROM sqlite_master WHERE type='index' AND sql IS NOT NULL")}
        expected = {re.search(r'INDEX (\w+)', s).group(1)
                    for _sql, idx in build.values() for s in idx}
        self.assertTrue(expected, 'the sports tables carry no named index to check')
        self.assertEqual(carried, expected)
        with self.assertRaises(sqlite3.IntegrityError):
            cur.execute("INSERT INTO sports_leagues (tsdb_id, name) VALUES (4387, 'again')")

    def test_tables_already_carrying_the_defaults_are_left_alone(self):
        build = self._current_build()
        conn, cur = self._scratch(build)
        M._m118_sports_defaults(conn, cur)
        self.assertEqual(self._stored(cur), self._fresh_tables(build))
        self.assertEqual(cur.execute("SELECT name FROM sqlite_master WHERE name LIKE "
                                     "'%default_new'").fetchall(), [])

    def test_a_run_that_stopped_after_one_table_finishes_the_rest(self):
        build = self._current_build()
        fresh = self._fresh_tables(build)
        half = dict(build)
        for table in ('sports_events', 'sports_images'):
            columns = [c for t, c, _l in M._SPORTS_DEFAULTS if t == table]
            half[table] = (self._aged(fresh[table], columns), build[table][1])
        conn, cur = self._scratch(half)
        M._m118_sports_defaults(conn, cur)
        self.assertEqual(self._stored(cur), fresh)

    def test_the_hand_written_ddl_of_the_sports_steps_is_left_alone(self):
        """A database that crossed 112, 115 and 117 carries `NOT NULL DEFAULT x`, the other
        order. It has its defaults and is not rebuilt."""
        td = tempfile.TemporaryDirectory()
        self.addCleanup(td.cleanup)
        conn = sqlite3.connect(os.path.join(td.name, 'scratch.db'))
        self.addCleanup(conn.close)
        cur = conn.cursor()
        cur.execute('CREATE TABLE recordings (id INTEGER PRIMARY KEY)')
        M._m112_sports_schedule(conn, cur)
        M._m115_sports_matches(conn, cur)
        M._m116_recording_sports(conn, cur)
        M._m117_sports_images(conn, cur)
        before = self._stored(cur)
        M._m118_sports_defaults(conn, cur)
        self.assertEqual(self._stored(cur), before)


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
