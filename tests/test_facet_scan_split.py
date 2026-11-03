"""The facet rail's deferred join: it is equivalent, it is gated, and "Any group" works.

`_combined_facet_scan()` answers account / category / health / Other / When / Day of week in
one pass. On
the airing grain that pass used to join `channels` to every surviving showing and then
aggregate; since dev/changelog/729 it applies the epg-side predicates first, tallies showings
per channel, and joins only that tally. Same counts, far fewer joined rows.

Two of the three classes here are real guards rather than characterization:

* `SplitEquivalenceTests` is the one that matters. Two query shapes now answer the same
  question, and the fast one is chosen automatically, so nothing but a test stands between a
  mis-placed predicate and counts that are silently wrong - a predicate on the wrong side of
  the split changes numbers, it does not raise. This is the same reasoning
  `test_airing_search.py::PlannerEquivalenceTests` records for the index planner.
* `GroupAnyFacetTests` pins a shipped 500 (dev/docs/BUGS.md 2026-08-18): the `group` value
  predicate did not restrict its correlation, so with "Collapse channel groups" on - the
  default - asking the rail about "Any group" raised InvalidRequestError instead of counting.

`SplitGateTests` is closer to characterization, but the gate is a performance contract with
measurements behind it, so it is pinned rather than left to drift.
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import event  # noqa: E402
from sqlalchemy.engine import Engine  # noqa: E402

from app import channel_search as cs, db  # noqa: E402
from app.channel_search import (  # noqa: E402
    DOW_VALUES, GRAIN_AIRINGS, GRAIN_CHANNELS, SearchState, compute_facets, split_predicates,
)
from app.database import ChannelGroupMember  # noqa: E402
from tests.support import seed  # noqa: E402
from tests.support.search import only_hiding  # noqa: E402
from tests.test_airing_search import TEST_CFG, _AiringTestCase  # noqa: E402


class _SplitTestCase(_AiringTestCase):
    """Helpers for driving the two shapes of the combined scan against one state."""

    def _shared_keys(self, state):
        """The dimensions compute_facets() would hand to the combined scan for this state."""
        wanted = [d for d in cs.visible_dimensions_for(state.grain)
                  if state.facets is None or d.key in state.facets]
        filtered = {f.key for f in state.filters if f.values or f.ex}
        return tuple(d.key for d in wanted
                     if d.key in cs._SCAN_DIMENSIONS and d.key not in filtered)

    def both_shapes(self, state):
        """(split result, joined result) for one state, or (None, joined) when it cannot
        split. Facets default to every dimension - the rail's own request, not `state()`'s
        rows-only default."""
        ctx = self.context()
        text_preds = cs.text_predicates(state, ctx)
        shared = self._shared_keys(state)
        self.assertTrue(shared, 'this state gives the combined scan nothing to do')
        preds = cs.base_predicates(state, ctx, text_preds=text_preds)
        split = split_predicates(state, ctx, text_preds)
        joined = cs._combined_facet_scan(shared, state, ctx, preds, split=None)
        if split is None:
            return None, joined
        return cs._combined_facet_scan(shared, state, ctx, preds, split=split), joined

    def rail_state(self, grain=GRAIN_AIRINGS, **kw):
        kw.setdefault('facets', None)
        return SearchState(grain=grain, **kw)


class SplitEquivalenceTests(_SplitTestCase):
    """The deferred-join shape and the joined shape must produce identical counts.

    Every state here is one the rail actually issues. A case that cannot split returns None
    and is asserted as such rather than skipped, so a gate that silently stops splitting
    everything would fail this too.
    """

    def test_landing_page_splits_and_agrees(self):
        split, joined = self.both_shapes(self.rail_state())
        self.assertIsNotNone(split, 'the no-query landing page is the case this exists for')
        self.assertEqual(split, joined)

    def test_every_splittable_state_agrees(self):
        cases = {
            'landing': self.rail_state(),
            'nothing hidden': self.rail_state(standing=only_hiding()),
            'when filter': self.rail_state(
                filters=(cs.DimensionFilter('when', ('today',), ()),)),
            'when excluded': self.rail_state(
                filters=(cs.DimensionFilter('when', (), ('today',)),)),
            # dev/changelog/1187: the weekday is a key of the tally itself, so a day or time
            # filter on the epg side has to leave both shapes agreeing about every day.
            'day filter': self.rail_state(
                filters=(cs.DimensionFilter('dow', DOW_VALUES[:3], ()),)),
            'time filter': self.rail_state(
                filters=(cs.DimensionFilter('tod', ('tod:00:00..12:00',), ()),)),
            'time excluded': self.rail_state(
                filters=(cs.DimensionFilter('tod', (), ('tod:22:00..02:00',)),)),
            'firstonly on': self.rail_state(
                standing=only_hiding('showpast', 'firstonly')),
            'grpdedup off': self.rail_state(
                standing=only_hiding('showpast', 'showdup')),
        }
        for label, state in cases.items():
            with self.subTest(label):
                split, joined = self.both_shapes(state)
                self.assertIsNotNone(split, f'{label} should take the split path')
                self.assertEqual(split, joined)

    def test_counts_are_not_trivially_empty(self):
        """An equivalence test over two empty dicts proves nothing - pin that the corpus
        actually reaches the aggregates."""
        split, _joined = self.both_shapes(self.rail_state())
        self.assertTrue(sum(split['acct'].values()))
        self.assertTrue(sum(split['when'].values()))
        self.assertTrue(sum(split['cat'].values()))
        self.assertEqual(sum(split['dow'].values()), sum(split['acct'].values()),
                         'every counted showing starts on exactly one day')


class SplitGateTests(_SplitTestCase):
    """What forces the joined shape, and why. Each of these is a measured cost decision."""

    def test_the_landing_page_splits(self):
        self.assertIsNotNone(
            split_predicates(self.rail_state(), self.context(), []))

    def test_a_typed_query_does_not_split(self):
        state = self.rail_state(q='wembley', fields=('name', 'epg-title'))
        ctx = self.context()
        self.assertIsNotNone(cs.text_predicates(state, ctx), 'query should build predicates')
        self.assertIsNone(split_predicates(state, ctx, cs.text_predicates(state, ctx)))

    def test_a_channel_side_filter_does_not_split(self):
        """The joined shape can apply a picked filter before touching epg_entries; the tally
        cannot. Splitting a filtered state is measurably slower, not faster."""
        for key, value in (('cat', 'Sports'), ('acct', '1'), ('health', 'good'),
                           ('other', 'guide'), ('group', cs.GROUP_ANY)):
            with self.subTest(key):
                state = self.rail_state(
                    filters=(cs.DimensionFilter(key, (value,), ()),))
                self.assertIsNone(split_predicates(state, self.context(), []))

    def test_an_epg_side_filter_still_splits(self):
        for key, value in (('when', 'today'), ('duration', 'min:10'), ('dow', 'sat'),
                           ('tod', 'tod:18:00..23:59')):
            with self.subTest(key):
                state = self.rail_state(
                    filters=(cs.DimensionFilter(key, (value,), ()),))
                self.assertIsNotNone(split_predicates(state, self.context(), []))

    def test_a_selective_standing_option_does_not_split(self):
        for key in ('shownoepg', 'showuntested'):
            with self.subTest(key):
                state = self.rail_state(standing=only_hiding('showpast', key))
                self.assertIsNone(split_predicates(state, self.context(), []))

    def test_an_unregistered_dimension_forces_the_joined_shape(self):
        """The failure mode this protects against is a NEW dimension defaulting into a side.
        A predicate placed wrongly changes counts silently, so absence must mean "do not
        split", never "assume channel"."""
        state = self.rail_state(filters=(cs.DimensionFilter('tag', ('sports',), ()),))
        self.assertNotIn('tag', cs._DIMENSION_SIDE)
        self.assertIsNone(split_predicates(state, self.context(), []))

    def test_the_channel_grain_never_takes_the_tally(self):
        """There is no epg-side half to defer - the tally would be a pointless subquery."""
        state = self.rail_state(grain=GRAIN_CHANNELS)
        ctx = self.context()
        shared = self._shared_keys(state)
        preds = cs.base_predicates(state, ctx)
        with_split = cs._combined_facet_scan(
            shared, state, ctx, preds, split=split_predicates(state, ctx, []))
        self.assertEqual(with_split,
                         cs._combined_facet_scan(shared, state, ctx, preds, split=None))


class GroupAnyFacetTests(_AiringTestCase):
    """Guards dev/docs/BUGS.md 2026-08-18: "Any group" 500ed the whole facet rail.

    `f.group=__any__` builds an EXISTS whose only FROM is channel_group_members. That
    predicate ends up nested inside `grpdedup`'s ranking subquery, which selects from that
    same table - so unrestricted auto-correlation removed the subquery's own FROM and
    SQLAlchemy raised. A NAMED group escaped only because its extra join left a FROM behind,
    which is why this broke exactly one value of one facet.
    """

    def _facets(self, values, standing):
        state = SearchState(grain=GRAIN_AIRINGS, facets=None, standing=standing,
                            filters=(cs.DimensionFilter('group', values, ()),))
        return compute_facets(state, cs.SearchContext.build(TEST_CFG))

    def test_any_group_filter_counts_instead_of_raising(self):
        facets = self._facets((cs.GROUP_ANY,), cs.default_standing_for(GRAIN_AIRINGS))
        self.assertIn('acct', facets)
        self.assertTrue(sum(facets['acct'].values()),
                        'the two grouped channels have showings, so this cannot be zero')

    def test_any_group_filter_works_with_collapse_off_too(self):
        """The bug needed grpdedup to reproduce; the fix must not depend on it either way."""
        facets = self._facets((cs.GROUP_ANY,), frozenset({'past', 'dup'}))
        self.assertTrue(sum(facets['acct'].values()))

    def test_a_named_group_filter_still_counts(self):
        facets = self._facets((self.group.name,), cs.default_standing_for(GRAIN_AIRINGS))
        self.assertTrue(sum(facets['acct'].values()))


class AnyGroupCountTests(_AiringTestCase):
    """The "Any group" count answers one question two ways and has to stay one number.

    It was a COUNT(DISTINCT) over every membership's showings, nearly the whole cost of this
    facet (dev/changelog/1418). Without the DISTINCT a channel in two groups is the case that
    breaks, so these seed exactly that and compare the count to the list it promises.
    """

    def _seed(self):
        super()._seed()
        seed.make_group(name='Also ESPN', members=[self.espn])
        db.session.commit()

    def _any_count(self, grain, **kw):
        state = SearchState(grain=grain, facets=('group',), standing=only_hiding(), **kw)
        return compute_facets(state, cs.SearchContext.build(TEST_CFG))['group'][cs.GROUP_ANY]

    def _listed(self, grain):
        # Airing grain only: the channel grain's total also carries a row per group.
        state = SearchState(grain=grain, facets=(), standing=only_hiding(),
                            filters=(cs.DimensionFilter('group', (cs.GROUP_ANY,), ()),))
        return cs.search(state, cs.SearchContext.build(TEST_CFG)).total

    def test_a_channel_in_two_groups_counts_once_on_either_grain(self):
        self.assertEqual(self._any_count(GRAIN_CHANNELS), 2)
        self.assertEqual(self._any_count(GRAIN_AIRINGS), self._listed(GRAIN_AIRINGS))
        self.assertEqual(self._listed(GRAIN_AIRINGS), 3,
                         'ESPN has two showings and Sky one, and ESPN is in two groups')

    def test_the_count_matches_the_list_with_a_narrowing_filter(self):
        narrow = (cs.DimensionFilter('cat', ('Sports',), ()),)
        state = SearchState(grain=GRAIN_AIRINGS, facets=(), standing=only_hiding(),
                            filters=narrow + (cs.DimensionFilter('group', (cs.GROUP_ANY,), ()),))
        self.assertEqual(self._any_count(GRAIN_AIRINGS, filters=narrow),
                         cs.search(state, cs.SearchContext.build(TEST_CFG)).total)

    def test_no_memberships_counts_zero(self):
        ChannelGroupMember.query.delete()
        db.session.commit()
        self.assertEqual(self._any_count(GRAIN_AIRINGS), 0)

    def test_the_count_does_not_go_back_to_a_distinct_over_showings(self):
        statements = []

        def capture(_conn, _cursor, statement, *_rest):
            statements.append(statement)

        event.listen(Engine, 'before_cursor_execute', capture)
        try:
            self._any_count(GRAIN_AIRINGS)
        finally:
            event.remove(Engine, 'before_cursor_execute', capture)
        self.assertEqual([s for s in statements if 'count(distinct' in s.lower()], [])


if __name__ == '__main__':
    unittest.main()
