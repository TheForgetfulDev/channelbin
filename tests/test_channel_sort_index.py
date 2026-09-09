"""The channel grain's default sort and the index that makes it affordable.

dev/changelog/699 did two things that only make sense together:

  - `channel_search.py::DEFAULT_SORT` moved from `category` to `name`, so `/channels` lands
    sorted by channel name.
  - `ix_channels_lower_name` on `(lower(name), id)` was added, so that sort walks an index
    and stops at LIMIT 100 instead of pouring every surviving row into a temp B-tree.

Shipping either one alone is a defect. Without the index the new default page sorts the
whole table on every request (measured 105.5ms against 9.0ms on the live 138,415-channel
database); without the default change the index is 10 MB nobody reads. So the tests here
assert the pair, and the index half is asserted THREE ways, because an index can go missing
in three independent ways and each has already happened in this tree at least once:

  1. it is declared on the model, so a fresh create_all() install gets it (the gap
     dev/changelog/695 found for _m024's five facet indexes),
  2. a migration creates it, so an existing database gets it too, and
  3. the two spell the expression identically - SQLite matches an expression index only
     against the identical expression text, so `(name, id)` on one side and
     `(lower(name), id)` on the other is an index the query silently never uses.

The timings that motivated all of this were taken against a local snapshot of the live
1.9 GB database and live in dev/changelog/699; nothing here asserts a duration. A seeded
test database is a few hundred rows and would report noise.

No network, no real ffmpeg - see CLAUDE.md §Testing.
Run standalone:
  python3 -m unittest tests.test_channel_sort_index
"""
import os
import re
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tests.support.app import make_test_app  # noqa: E402
from tests.support import seed  # noqa: E402
from app import db  # noqa: E402
from app.channel_search import (  # noqa: E402
    DEFAULT_SORT, DEFAULT_SORT_BY_GRAIN, GRAIN_AIRINGS, GRAIN_CHANNELS, SORTS,
    SearchContext, SearchState, search,
)

INDEX_NAME = 'ix_channels_lower_name'


def _flat(sql):
    return re.sub(r'\s+', ' ', sql).strip()


class DefaultSortTests(unittest.TestCase):
    """The channel grain lands on `name`; the airing grain still lands on `when`."""

    def setUp(self):
        self.t = make_test_app()

    def tearDown(self):
        self.t.cleanup()

    def test_channel_grain_defaults_to_name(self):
        self.assertEqual(DEFAULT_SORT, 'name')
        self.assertEqual(DEFAULT_SORT_BY_GRAIN[GRAIN_CHANNELS], 'name')

    def test_airing_grain_still_defaults_to_when(self):
        """The default flip was deliberately scoped to the channel grain only - flipping both
        would have made the Guide (EPG) tab land on a sort that costs 1.67s against `when`'s
        0.11s (dev/changelog/692)."""
        self.assertEqual(DEFAULT_SORT_BY_GRAIN[GRAIN_AIRINGS], 'when')

    def test_a_bare_state_resolves_to_the_name_sort(self):
        self.assertEqual(SearchState().sort, 'name')
        self.assertEqual(SearchState(grain=GRAIN_AIRINGS).sort, 'when')

    def test_the_default_sort_key_exists_in_the_registry(self):
        """A default naming a key the registry does not have is a 400 on the landing page."""
        self.assertIn(DEFAULT_SORT, SORTS)


class DefaultSortOrdersRowsByNameTests(unittest.TestCase):
    """The default page really comes back in case-insensitive name order."""

    def setUp(self):
        self.t = make_test_app()
        acct = seed.make_account(name='Sort Acct')
        # Mixed case on purpose: `lower(name)` and a plain `name` sort disagree about these,
        # so this also pins that the sort is the case-insensitive one.
        self.names = ['zulu', 'Alpha', 'mike', 'Bravo', 'echo']
        for i, n in enumerate(self.names):
            ch = seed.make_channel(acct, stream_id=500 + i, name=n, in_guide=True)
            ch.category_name = 'Zed' if n[0].lower() < 'm' else 'Aaa'
        db.session.commit()

    def tearDown(self):
        self.t.cleanup()

    def test_rows_come_back_case_insensitively_by_name(self):
        result = search(SearchState(), SearchContext.build(),
                        want_counts=False, want_facets=False)
        self.assertEqual([r.name for r in result.rows],
                         sorted(self.names, key=str.lower))

    def test_the_default_is_not_category_order(self):
        """Guards the change itself: category order would put the 'Aaa' rows first."""
        result = search(SearchState(), SearchContext.build(),
                        want_counts=False, want_facets=False)
        by_category = sorted(self.names,
                             key=lambda n: ('Zed' if n[0].lower() < 'm' else 'Aaa', n.lower()))
        self.assertNotEqual([r.name for r in result.rows], by_category)


class LowerNameIndexTests(unittest.TestCase):
    """The index exists on a fresh build, and matches the sort's expression exactly."""

    def setUp(self):
        self.t = make_test_app()

    def tearDown(self):
        self.t.cleanup()

    def _index_sql(self):
        row = db.session.execute(db.text(
            "SELECT sql FROM sqlite_master WHERE type='index' AND name=:n"
        ), {'n': INDEX_NAME}).fetchone()
        return _flat(row[0]) if row and row[0] else None

    def test_a_fresh_database_has_the_index(self):
        """A fresh install never runs migrations - run_migrations() stamps it at
        CURRENT_SCHEMA_VERSION and returns - so an index living only in a migration is one
        every new install does without."""
        self.assertIsNotNone(self._index_sql(),
                             f'{INDEX_NAME} is missing from a create_all() database')

    def test_the_index_is_on_the_lowercased_name(self):
        """`name COLLATE NOCASE` or a plain `(name, id)` would both be perfectly good indexes
        that the planner would never choose for `ORDER BY lower(name), id`."""
        sql = self._index_sql() or ''
        self.assertRegex(sql.lower(), r'lower\s*\(\s*name\s*\)',
                         f'{INDEX_NAME} must index lower(name), not a bare or collated name')

    def test_the_index_carries_the_id_tiebreak(self):
        """`_search_channels` always appends Channel.id as a stable tiebreak, so an index
        without it stops driving the sort at the first duplicated name."""
        self.assertRegex((self._index_sql() or '').lower(), r'lower\s*\(\s*name\s*\)\s*,\s*id')

    def test_the_sort_expression_and_the_index_expression_are_spelled_the_same(self):
        """The two halves are in different files and only SQLite notices when they drift."""
        rendered = _flat(str(SORTS['name']()[0].compile(
            compile_kwargs={'literal_binds': True}))).lower()
        # The ORDER BY renders table-qualified (`lower(channels.name)`) and index DDL does
        # not (`lower(name)`); what has to match is the FUNCTION applied to that column.
        self.assertIn('lower(', rendered)
        self.assertTrue(rendered.endswith('.name)') or rendered.endswith('(name)'),
                        f'the name sort no longer orders by lower(name): {rendered}')
        self.assertRegex((self._index_sql() or '').lower(), r'lower\s*\(\s*name\s*\)')


class MigrationParityTests(unittest.TestCase):
    """A migrated database gets the same index a fresh one is built with."""

    def test_the_migration_step_is_registered(self):
        from app.migrations import (CURRENT_SCHEMA_VERSION, SCHEMA_MIGRATIONS,
                                    _m040_channel_lower_name_index)
        versions = {v: fn for v, _, fn in SCHEMA_MIGRATIONS}
        self.assertIn(40, versions)
        self.assertIs(versions[40], _m040_channel_lower_name_index)
        self.assertGreaterEqual(CURRENT_SCHEMA_VERSION, 40)

    def test_the_step_creates_the_index_on_a_database_without_it(self):
        import sqlite3
        from app.migrations import _m040_channel_lower_name_index

        conn = sqlite3.connect(':memory:')
        cur = conn.cursor()
        cur.execute('CREATE TABLE channels (id INTEGER PRIMARY KEY, name TEXT)')
        _m040_channel_lower_name_index(conn, cur)
        names = [r[0] for r in cur.execute(
            "SELECT name FROM sqlite_master WHERE type='index'").fetchall()]
        self.assertIn(INDEX_NAME, names)

    def test_the_step_is_re_runnable(self):
        """Completion is stamped in a different commit from the work, so a crash in that gap
        re-runs the step (dev/changelog/686). CREATE INDEX IF NOT EXISTS has to tolerate it."""
        import sqlite3
        from app.migrations import _m040_channel_lower_name_index

        conn = sqlite3.connect(':memory:')
        cur = conn.cursor()
        cur.execute('CREATE TABLE channels (id INTEGER PRIMARY KEY, name TEXT)')
        _m040_channel_lower_name_index(conn, cur)
        _m040_channel_lower_name_index(conn, cur)  # must not raise
        count = cur.execute(
            "SELECT count(*) FROM sqlite_master WHERE type='index' AND name=?",
            (INDEX_NAME,)).fetchone()[0]
        self.assertEqual(count, 1)


class QueryPlanTests(unittest.TestCase):
    """The planner actually drives the index for the default page.

    Asserted on the PLAN rather than on a duration: a seeded database is small enough that
    SQLite may reasonably choose a scan, so this seeds enough rows that the index is the
    cheaper option and then checks the plan text. The real timings are in dev/changelog/699.
    """

    def setUp(self):
        self.t = make_test_app()
        acct = seed.make_account(name='Plan Acct')
        for i in range(400):
            seed.make_channel(acct, stream_id=9000 + i, name=f'Chan {i:04d}', in_guide=True)
        db.session.commit()
        db.session.execute(db.text('ANALYZE'))
        db.session.commit()

    def tearDown(self):
        self.t.cleanup()

    def test_the_default_sort_uses_the_index_and_not_a_temp_btree(self):
        plan = [r[3] for r in db.session.execute(db.text(
            'EXPLAIN QUERY PLAN SELECT id FROM channels '
            'ORDER BY lower(name), id LIMIT 100')).fetchall()]
        joined = ' | '.join(plan)
        self.assertIn(INDEX_NAME, joined,
                      f'the default sort no longer drives {INDEX_NAME}: {joined}')
        self.assertNotIn('USE TEMP B-TREE FOR ORDER BY', joined,
                         f'the default sort is still fully sorting the table: {joined}')


if __name__ == '__main__':
    unittest.main(verbosity=2)
