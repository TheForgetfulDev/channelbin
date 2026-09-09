"""How a page of search results is fetched - `app/channel_search.py::_page_rows`.

dev/changelog/697 changed the row query from `... ORDER BY x LIMIT 100 OFFSET k` to the same
query with an id-only subquery carrying the LIMIT/OFFSET, so the k skipped rows are built as
bare primary keys instead of full rows. Two claims, tested separately:

  - PageShapeTests is the real guard. It pins the shape that IS the optimization (the offset
    is consumed by an id subquery, not the outer row query) and the property that shape must
    never lose (it is ONE statement). These fail against the pre-697 implementation.

    The one-statement half deserves its own note, because it is the half a future refactor is
    most likely to break while "keeping" the optimization: the two-round-trip spelling of the
    same idea measures identically and is wrong. pysqlite runs SELECTs in autocommit, so no
    read transaction spans two statements, and a delete landing between the id fetch and the
    row fetch silently returns a short page.

  - PageEquivalenceTests is a characterization suite. It asserts the new shape returns exactly
    what the old one returned across every sort, both directions and several depths, on both
    grains. It passes against the pre-697 implementation too, deliberately - "the answer did
    not move" is the entire claim being pinned, the same way tests/test_duplicate_flag_
    recompute.py::RecomputeResultTests pins its own rewrite.

The seeds here are small, so these assert shape and equivalence, never timing. The timings
that motivated the change were taken against a local snapshot of the live 1.9 GB database and
are recorded in dev/changelog/697.

No network, no real ffmpeg - see CLAUDE.md §Testing.
Run standalone:
  python3 -m unittest tests.test_search_pagination_shape
"""
import os
import re
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tests.support.app import make_test_app  # noqa: E402
from tests.support.iocount import IOCounter, all_engines  # noqa: E402
from tests.support import seed  # noqa: E402
from app import db  # noqa: E402
from app.database import Channel, EPGEntry  # noqa: E402
from app.channel_search import (  # noqa: E402
    SORTS, SORTS_AIRINGS, SearchContext, SearchState, _airing_query, _with_tiebreak,
    dimension_predicates, search, standing_predicates, text_predicates,
)


def _flat(sql):
    """Statement text with runs of whitespace collapsed, so an assertion about clause
    order does not depend on where SQLAlchemy chose to wrap a line."""
    return re.sub(r'\s+', ' ', sql).strip()


class _Base(unittest.TestCase):
    #: Enough rows that page 2 exists at page_size=3 on both grains.
    CHANNELS = 12

    def setUp(self):
        self.t = make_test_app()
        self.account = seed.make_account(name='Pagination Acct')
        self.channels = []
        for i in range(self.CHANNELS):
            ch = seed.make_channel(
                self.account, stream_id=1000 + i,
                name=f'Channel {i:02d}',
                in_guide=True,
            )
            ch.category_name = f'Cat {i % 3}'
            self.channels.append(ch)
        db.session.flush()
        for i, ch in enumerate(self.channels):
            for j in range(2):
                seed.make_epg_entry(ch, title=f'Show {i:02d}-{j}',
                                    offset_minutes=60 * (i + j) + 30)
        db.session.commit()

    def tearDown(self):
        self.t.cleanup()

    def _all_statements(self, state):
        """Every SELECT `search()` runs, counts and facets switched off."""
        ctx = SearchContext.build()
        with IOCounter(all_engines()) as c:
            result = search(state, ctx, want_counts=False, want_facets=False)
        return result, [s for s in c.statements if s.lstrip().upper().startswith('SELECT')]

    def _row_statements(self, state):
        """The PAGE query alone - the statements carrying the LIMIT/OFFSET.

        Filtered rather than "everything `search()` ran", because since dev/changelog/811 a
        channel-grain search also runs the group query, and an airings search with the
        collapse on also asks which groups each surviving row won. Both are deliberately
        SEPARATE statements (DESIGN-group-search-rows.md §5.2's first rule: group rows are
        spliced into the page and never unioned into the id subquery, because that subquery
        staying one statement over one primary key is the optimization). Neither carries a
        LIMIT or an OFFSET, so this isolates the page query without naming them.
        """
        result, stmts = self._all_statements(state)
        return result, [s for s in stmts if ' OFFSET ' in _flat(s).upper()]


class PageShapeTests(_Base):
    """The offset is consumed by an id-only subquery, in a single statement.

    Fails against the pre-697 implementation: there the outer row query carried the
    LIMIT/OFFSET itself, so the final ORDER BY came before it rather than after.
    """

    def test_channel_page_offsets_an_id_subquery_not_the_row_query(self):
        state = SearchState(grain='channels', page=2, page_size=3)
        _, stmts = self._row_statements(state)
        self.assertEqual(len(stmts), 1, 'the page must be one statement')
        sql = _flat(stmts[0])
        self.assertIn('IN (SELECT channels.id FROM channels', sql,
                      'the skipped rows must be selected as bare ids')
        self.assertGreater(sql.rindex('ORDER BY'), sql.rindex('OFFSET'),
                           'OFFSET must sit inside the subquery, not on the outer query')

    def test_airing_page_offsets_an_id_subquery_not_the_row_query(self):
        state = SearchState(grain='airings', page=2, page_size=3)
        _, stmts = self._row_statements(state)
        self.assertEqual(len(stmts), 1, 'the page must be one statement')
        sql = _flat(stmts[0])
        self.assertIn('IN (SELECT epg_entries.id FROM epg_entries', sql,
                      'the skipped rows must be selected as bare ids')
        self.assertGreater(sql.rindex('ORDER BY'), sql.rindex('OFFSET'),
                           'OFFSET must sit inside the subquery, not on the outer query')

    def test_outer_row_query_carries_no_offset_of_its_own(self):
        """Belt to the clause-order assertion above: exactly one OFFSET in the statement.

        Two would mean the outer query kept its own, which reintroduces the full-row skip
        the subquery-OFFSET shape removed while still looking like that shape.
        """
        for grain in ('channels', 'airings'):
            with self.subTest(grain=grain):
                _, stmts = self._row_statements(
                    SearchState(grain=grain, page=3, page_size=3))
                self.assertEqual(_flat(stmts[0]).count('OFFSET'), 1)

    def test_page_stays_one_statement_at_every_depth(self):
        """The atomicity property. A two-round-trip rewrite would show up here as 2."""
        for grain in ('channels', 'airings'):
            for page in (1, 2, 3, 99):
                with self.subTest(grain=grain, page=page):
                    _, stmts = self._row_statements(
                        SearchState(grain=grain, page=page, page_size=3))
                    self.assertEqual(len(stmts), 1)

    def test_the_group_query_is_its_own_statement_and_never_touches_the_page_query(self):
        """§5.2's first rule, as a shape assertion (dev/changelog/811).

        The groups a search returns are fetched by ONE query of their own and merged into
        the page in Python. Unioning them into the id subquery instead would trade the
        400ms->135ms win at offset 100,000 for a feature that never needed it - there are
        tens of groups against six figures of channels.
        """
        state = SearchState(grain='channels', page=2, page_size=3)
        _, every = self._all_statements(state)
        group_stmts = [s for s in every if ' FROM channel_groups ' in _flat(s)]
        self.assertEqual(len(group_stmts), 1,
                         f'expected one group query, got {len(group_stmts)}')
        self.assertNotIn(' OFFSET ', _flat(group_stmts[0]).upper(),
                         'the group query must not be paged - every matching group is fetched')
        # The page query may still MENTION groups - the fold is a predicate over channels -
        # but it must never SELECT a group row: no union, and its id subquery is over one
        # table with one primary key.
        page = _flat(self._row_statements(state)[1][0])
        self.assertNotIn('UNION', page.upper())
        self.assertIn('IN (SELECT channels.id FROM channels', page)

    def test_airing_subquery_keeps_the_channel_join(self):
        """Four airing sorts order by Channel columns, so the outer half of the page query
        has to keep the join the subquery filtered through - dropping it is a 500, and
        dropping it only on those four sorts is a 500 nobody hits until they sort."""
        for sort in ('channel', 'health', 'account', 'category'):
            with self.subTest(sort=sort):
                result, stmts = self._row_statements(
                    SearchState(grain='airings', sort=sort, page=2, page_size=3))
                self.assertEqual(len(stmts), 1)
                sql = _flat(stmts[0])
                # Not an exact count: the `dup` standing option's ranking subquery joins
                # channels too, so the floor is what this asserts - one join for the id
                # subquery and one for the outer query that re-applies the same ORDER BY.
                self.assertGreaterEqual(sql.count('JOIN channels'), 2,
                                        'both halves of the page query need the join')
                self.assertIn('channels.', sql[sql.rindex('ORDER BY'):],
                              'the outer ORDER BY reads a Channel column')
                self.assertTrue(result.rows)


class PageEquivalenceTests(_Base):
    """The new shape returns exactly what the old OFFSET shape returned.

    Characterization, not a regression guard - these pass against the pre-697 code too,
    because the claim is that nothing about the answer changed. Read the module docstring.
    """

    def _old_shape_ids(self, state, ctx):
        """The page as `search()` built it before dev/changelog/697."""
        narrowing = text_predicates(state, ctx) + dimension_predicates(state, ctx)
        kept = standing_predicates(state, ctx, narrowing)
        if state.grain == 'channels':
            query = Channel.query.filter(*narrowing, *kept)
            order = list(SORTS[state.sort]())
            if state.sort_desc:
                order = [o.desc() for o in order]
            order = _with_tiebreak(order, Channel.id)
        else:
            query = _airing_query().filter(*narrowing, *kept)
            order = list(SORTS_AIRINGS[state.sort]())
            if state.sort_desc:
                order = [o.desc() for o in order]
            order = _with_tiebreak(order, EPGEntry.start_time, EPGEntry.id)
        rows = (query.order_by(*order)
                .limit(state.page_size)
                .offset((state.page - 1) * state.page_size).all())
        return [r.id for r in rows]

    def _assert_same(self, **kw):
        ctx = SearchContext.build()
        state = SearchState(page_size=3, **kw)
        new = [r.id for r in search(state, ctx, want_counts=False,
                                    want_facets=False).rows]
        self.assertEqual(self._old_shape_ids(state, ctx), new)
        return new

    def test_every_channel_sort_both_directions_and_depths(self):
        for sort in SORTS:
            for desc in (False, True):
                for page in (1, 2, 4):
                    with self.subTest(sort=sort, desc=desc, page=page):
                        self._assert_same(grain='channels', sort=sort,
                                          sort_desc=desc, page=page)

    def test_every_airing_sort_both_directions_and_depths(self):
        for sort in SORTS_AIRINGS:
            for desc in (False, True):
                for page in (1, 2, 4):
                    with self.subTest(sort=sort, desc=desc, page=page):
                        self._assert_same(grain='airings', sort=sort,
                                          sort_desc=desc, page=page)

    def test_filtered_search_pages_identically(self):
        for grain in ('channels', 'airings'):
            for page in (1, 2):
                with self.subTest(grain=grain, page=page):
                    self._assert_same(grain=grain, q='Channel 0', page=page)

    def test_page_past_the_end_is_empty_on_both_shapes(self):
        for grain in ('channels', 'airings'):
            with self.subTest(grain=grain):
                self.assertEqual(self._assert_same(grain=grain, page=500), [])

    def test_a_page_that_does_not_fill_returns_the_remainder(self):
        """The last page is the one an id-subquery could plausibly truncate."""
        ctx = SearchContext.build()
        state = SearchState(grain='channels', page_size=5, page=3)
        ids = [r.id for r in search(state, ctx, want_counts=False,
                                    want_facets=False).rows]
        self.assertEqual(self._old_shape_ids(state, ctx), ids)
        self.assertEqual(len(ids), self.CHANNELS - 10)
