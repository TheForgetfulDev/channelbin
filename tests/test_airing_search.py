"""The AIRING grain of the channel search (dev/changelog/412).

`grain=airings` answers "when is this on" through the same engine that answers "which of my
channels": one `SearchState`, one set of registries, one facet counter, one envelope. These
are **characterization tests for new behavior**, not regression guards - nothing here existed
to be broken - with two exceptions that are called out in their own docstrings:

* `PlannerEquivalenceTests` is a real guard. The planner picks between two ways of running
  the same query, and the whole design rests on their answers being identical; a test that
  only checked the fast one would let them drift silently, which is the defect class this
  project's search tests exist for.
* `ClusterScopeTests` pins two bugs found before the airing grain shipped (dev/changelog/412).
  Both were rows disappearing entirely - the quietest kind of wrong - and both came from
  copying `dup`'s whole-table ranking into an option that is about the result set.
* `QueryPlanTests` guards the 2026-08-01 first-paint defect (dev/docs/BUGS.md, changelog 420)
  and the `when`-window defect found by the measurement that closed it (changelog 421).
  Read its docstring before changing anything it asserts: what it pins is a query PLAN, and
  the seeds here are five rows, so it can only assert the shape that produced the plan.

What is deliberately NOT re-tested here: anything the channel grain already covers in
`tests/test_channel_search.py`. The dimensions, the standing options and the search fields
are the same registries and the same predicates; what is new is the grain scoping, the row
payload, and the planner.
"""
import os
import re
import sys
import unittest
from datetime import datetime, timedelta
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from werkzeug.datastructures import MultiDict  # noqa: E402

from tests.support.app import make_test_app  # noqa: E402
from tests.support.iocount import IOCounter, all_engines  # noqa: E402
from tests.support import seed  # noqa: E402
from tests.support.search import only_hiding  # noqa: E402
from app import db  # noqa: E402
from app.database import EPGEntry, Tag, TagPattern  # noqa: E402
from sqlalchemy import select  # noqa: E402

from app import channel_search  # noqa: E402
from app.channel_search import (  # noqa: E402
    DEFAULT_SORT_BY_GRAIN, DIMENSION_BY_KEY, FIELD_BY_KEY, GRAIN_AIRINGS, GRAIN_CHANNELS,
    DimensionFilter, SearchContext, SearchState, SearchStateError,
    airing_narrowing_decision, compute_facets, default_fields_for,
    default_standing_for, dimensions_for, parse_duration, parse_terms, parse_when_custom,
    parse_when_next, search, standing_applied, standing_options_for, text_predicates,
    visible_dimensions_for,
    WHEN_INDEX_DEFEAT_WIDTH, _facet_counts, clear_standing_breakdown_cache,
    _group_collapse_losers, _group_ranked_entries)
from app.channel_search_rows import build_rows  # noqa: E402
from app.search_index import rebuild_search_indexes  # noqa: E402

TEST_CFG = {'sync': {'channel_missing_after_days': 7}}


class _AiringTestCase(unittest.TestCase):
    """A corpus with the shapes that make this grain different from the channel grain.

    Three channels, two of them in one channel-kind group airing the SAME program at the same
    time (which is what "collapse channel groups" exists for), one showing that has already
    ended, one that is on right now, and one two days out. Every time is relative to the
    clock so `now` / `today` / `tomorrow` mean what they say.
    """

    def setUp(self):
        self.t = make_test_app()
        self.ctx_mgr = self.t.app.app_context()
        self.ctx_mgr.push()
        self._seed()

    def tearDown(self):
        self.ctx_mgr.pop()
        self.t.cleanup()

    def _seed(self):
        self.now = datetime.utcnow()
        self.acct = seed.make_account(name='Alpha', last_sync_at=self.now)
        self.espn = seed.make_channel(self.acct, name='ESPN2 HD', category_name='Sports',
                                      health_score=90.0, in_guide=True,
                                      last_seen_at=self.now)
        self.sky = seed.make_channel(self.acct, name='Sky Sports', category_name='Sports',
                                     health_score=50.0, last_seen_at=self.now)
        self.bbc = seed.make_channel(self.acct, name='BBC News', category_name='News',
                                     last_seen_at=self.now)

        # The two group members carry identical listings, which is exactly the real shape:
        # a group is duplicate feeds of one logical channel.
        self.cup_espn = self._epg(self.espn, 'Wembley Cup Final', 30,
                                  desc='Liverpool at Wembley')
        self.cup_sky = self._epg(self.sky, 'Wembley Cup Final', 30,
                                 desc='Liverpool at Wembley')
        self.on_now = self._epg(self.bbc, 'Nightly News', -30, desc='World headlines')
        self.later = self._epg(self.bbc, 'Wembley Highlights', 60 * 40, desc='Two days out')
        self.ended = self._epg(self.espn, 'Old Wembley Show', -200)

        self.group = seed.make_group(name='Fox', members=[self.espn, self.sky])
        self.tag = Tag(name='sports')
        db.session.add(self.tag)
        db.session.flush()
        db.session.add(TagPattern(tag_id=self.tag.id, pattern='Wembley'))
        db.session.commit()

    def _epg(self, channel, title, offset_minutes, duration=60, desc=None, sub=None):
        start = self.now + timedelta(minutes=offset_minutes)
        entry = EPGEntry(channel_id=channel.id, title=title, sub_title=sub, description=desc,
                         start_time=start, stop_time=start + timedelta(minutes=duration))
        db.session.add(entry)
        db.session.flush()
        return entry

    # -- running ----------------------------------------------------------

    def context(self):
        return SearchContext.build(TEST_CFG)

    def state(self, **kw):
        kw.setdefault('facets', ())
        return SearchState(grain=GRAIN_AIRINGS, **kw)

    def run_search(self, **kw):
        return search(self.state(**kw), self.context())

    def titles(self, **kw):
        return [r.title for r in self.run_search(**kw).rows]

    def rows(self, **kw):
        state = self.state(**kw)
        ctx = self.context()
        return build_rows(search(state, ctx), state, ctx)

    def bare(self, **kw):
        """No standing options at all - the plain "what showings are there" question."""
        return self.state(standing=only_hiding(), **kw)


class GrainScopingTests(_AiringTestCase):
    """A registry entry belongs to one grain or to both, and the difference is enforced."""

    def test_the_when_dimension_exists_only_on_the_airing_grain(self):
        self.assertIn('when', [d.key for d in visible_dimensions_for(GRAIN_AIRINGS)])
        self.assertNotIn('when', [d.key for d in visible_dimensions_for(GRAIN_CHANNELS)])

    def test_the_duration_dimension_exists_only_on_the_airing_grain(self):
        """A channel has no length of its own, only a showing does - same reasoning as
        `when`."""
        self.assertIn('duration', [d.key for d in visible_dimensions_for(GRAIN_AIRINGS)])
        self.assertNotIn('duration', [d.key for d in visible_dimensions_for(GRAIN_CHANNELS)])

    def test_a_grain_only_dimension_leads_its_rail(self):
        """Mockup 25 P9: a dimension that exists only on the grain you just switched
        into is the reason you switched. The channel rail has no grain-only dimension and is
        therefore unchanged, which is the other half of the same rule."""
        self.assertEqual(dimensions_for(GRAIN_AIRINGS)[0].key, 'when')
        self.assertEqual([d.key for d in dimensions_for(GRAIN_CHANNELS)][0], 'tag')

    def test_the_three_airing_standing_options_are_offered_on_one_grain_only(self):
        airing_keys = [s.key for s in standing_options_for(GRAIN_AIRINGS)]
        channel_keys = [s.key for s in standing_options_for(GRAIN_CHANNELS)]
        for key in ('showpast', 'firstonly', 'grpdedup'):
            self.assertIn(key, airing_keys)
            self.assertNotIn(key, channel_keys)

    def test_the_defaults_are_per_grain(self):
        """Asserted as the set that lands in the URL AND as what it does, since the
        inversion (dev/changelog/778) made those near-inverses of each other: a hider that
        is on by default is a `show*` key that is ABSENT. Pinning only the set would let a
        later edit flip `hides_when_on` and keep this green while both grains changed."""
        self.assertEqual(default_standing_for(GRAIN_CHANNELS),
                         frozenset({'shownoepg', 'showuntested', 'showmembers'}))
        self.assertEqual(default_standing_for(GRAIN_AIRINGS),
                         frozenset({'shownoepg', 'showuntested', 'grpdedup'}))
        # `showmembers` is channel-grain only and since dev/changelog/860 does NOT hide by
        # default - a group member keeps a row of its own. It is therefore in the first set
        # and in neither `hiding` set below; the airing grain has no equivalent at all and
        # expresses the collapse through `grpdedup`, which does still hide by default.
        for grain, expect in ((GRAIN_CHANNELS, {'showhidden', 'showdup', 'shownotnorm'}),
                              (GRAIN_AIRINGS, {'showhidden', 'showdup', 'shownotnorm',
                                               'showpast', 'grpdedup'})):
            hiding = {s.key for s in standing_options_for(grain)
                      if standing_applied(default_standing_for(grain), s.key)}
            self.assertEqual(hiding, expect, grain)
        self.assertEqual(DEFAULT_SORT_BY_GRAIN[GRAIN_CHANNELS], 'name')
        self.assertEqual(DEFAULT_SORT_BY_GRAIN[GRAIN_AIRINGS], 'when')
        # `fields` is a per-grain default too (dev/changelog/860), and each grain's scope is
        # its OWN primary field: neither grain reaches across into the other's by default.
        self.assertEqual(default_fields_for(GRAIN_CHANNELS), ('name',))
        self.assertEqual(default_fields_for(GRAIN_AIRINGS),
                         ('epg-title', 'epg-sub', 'epg-desc'))

    def test_the_default_scope_on_this_grain_is_the_programs_own_text(self):
        """dev/changelog/899. A row on this grain IS a showing, so the three program fields
        are the whole default: with `name` in it, a typed word dragged in every showing on a
        channel whose NAME carried it - `BBC News` returning its entire schedule for a word
        that appears in no program on it. The channel name is one tick away in the Search in
        pane, and it is what the CHANNEL grain defaults to.

        Both directions, because either alone would pass on a broken build: the
        channel-name-only hit must be absent by default AND present once `name` is ticked
        on. Both index states, because the FTS and LIKE paths build the scope separately.
        """
        for indexed in (False, True):
            with self.subTest(indexed=indexed):
                if indexed:
                    rebuild_search_indexes('test')
                self.assertEqual(self.titles(q='bbc'), [])
                self.assertEqual(
                    self.titles(q='bbc',
                                fields=default_fields_for(GRAIN_AIRINGS) + ('name',)),
                    ['Nightly News', 'Wembley Highlights'])

    def test_an_out_of_grain_sort_is_a_400_but_an_out_of_grain_filter_is_ignored(self):
        """The asymmetry is deliberate and is what makes a parked chip survive a reload
        (mockup 25 P2). An ignored filter widens the result set and the page draws the chip
        struck through; an honoured-as-something-else sort would be wrong data wearing the
        right label, so it raises."""
        with self.assertRaises(SearchStateError):
            SearchState.from_params(MultiDict([('grain', 'airings'), ('sort', 'name')]))
        parsed = SearchState.from_params(MultiDict([('f.when', 'today')]))
        self.assertEqual(parsed.grain, GRAIN_CHANNELS)
        self.assertEqual(parsed.filter_for('when').values, ('today',))
        # And it still round-trips, or parking would not survive the address bar.
        self.assertIn(('f.when', 'today'), parsed.to_params())
        # ...and narrows nothing on the grain that cannot express it.
        every = search(SearchState(facets=()), self.context()).total
        parked = search(SearchState(facets=(), filters=(DimensionFilter('when', ('today',)),)),
                        self.context()).total
        self.assertEqual(every, parked)

    def test_an_out_of_grain_standing_option_hides_nothing_and_is_kept(self):
        result = search(SearchState(facets=(), standing=only_hiding('showpast')), self.context())
        self.assertEqual(result.standing_hidden, {})
        self.assertGreater(result.total, 0)


class TagGrainScopeTests(_AiringTestCase):
    """`tag` is the second dimension (besides `when`) that means something different per
    grain (DESIGN-channel-search.md §1.1, added 2026-08-07). On the airing grain it asks
    "does THIS showing's own text carry the tag", the same question the row's own
    `matched_tags` badge answers - not "does the channel carry it anywhere, ever."

    `self.tag`'s only pattern is `Wembley`. BBC News airs two showings: `Nightly News` (no
    Wembley) and `Wembley Highlights` (two days out). Before this was fixed, filtering the
    airing grain by this tag matched `Nightly News` too, because BBC-the-channel carries the
    tag via its OTHER showing - dev/docs/BUGS.md 2026-08-07.

    Since dev/changelog/862 the channel grain is narrower too - "airing it right now" rather
    than "airing it ever" - so BBC News no longer matches there either. The two grains still
    differ, and the difference is still which row the answer is about: one showing, or the
    one program a channel row can display.
    """

    def test_the_airing_grain_tag_filter_only_matches_the_showings_own_text(self):
        titles = self.titles(filters=(DimensionFilter('tag', ('sports',)),),
                             standing=only_hiding())
        self.assertNotIn('Nightly News', titles)
        self.assertEqual(sorted(titles),
                         ['Old Wembley Show', 'Wembley Cup Final', 'Wembley Cup Final',
                          'Wembley Highlights'])

    def test_the_channel_grain_tag_filter_asks_what_is_on_right_now(self):
        """Still deliberately different from the airing grain above, but the difference moved
        (dev/changelog/862). The channel grain used to mean "carries the tag anywhere, ever",
        so BBC News matched on the strength of `Wembley Highlights` two days out while its own
        row read `Nightly News`; it now asks what the `Now airing` column answers. `Cup
        Channel` is named so nothing but its current program can match."""
        onnow = seed.make_channel(self.acct, name='Cup Channel', last_seen_at=self.now)
        self._epg(onnow, 'Wembley Cup Final', -10)
        db.session.commit()
        names = sorted(r.name for r in search(
            SearchState(facets=(), filters=(DimensionFilter('tag', ('sports',)),)),
            self.context()).rows)
        self.assertEqual(names, ['Cup Channel'])

    def test_the_tag_facet_count_agrees_with_the_filter_on_the_channel_grain(self):
        """The same invariant as the airing-grain case below, on the grain whose meaning
        changed: the rail counts with one expression and the filter runs with another, so
        they have to be the same expression."""
        onnow = seed.make_channel(self.acct, name='Cup Channel', last_seen_at=self.now)
        self._epg(onnow, 'Wembley Cup Final', -10)
        db.session.commit()
        state = SearchState(facets=('tag',), grain=GRAIN_CHANNELS)
        ctx = self.context()
        counted = _facet_counts(DIMENSION_BY_KEY['tag'], state, ctx, text_predicates(state, ctx))
        filtered_total = search(SearchState(facets=(), grain=GRAIN_CHANNELS,
                                            filters=(DimensionFilter('tag', ('sports',)),)),
                                ctx).total
        self.assertEqual(counted['sports'], filtered_total)
        self.assertEqual(filtered_total, 1)

    def test_the_tag_facet_count_agrees_with_the_filter_on_the_airing_grain(self):
        """DESIGN-channel-search.md §1.1's own invariant: the rail's count has to equal what
        clicking it returns, on whichever grain is active."""
        state = self.bare()
        ctx = self.context()
        counted = _facet_counts(DIMENSION_BY_KEY['tag'], state, ctx, text_predicates(state, ctx))
        filtered_total = search(self.bare(
            filters=(DimensionFilter('tag', ('sports',)),)), ctx).total
        self.assertEqual(counted['sports'], filtered_total)


class GuideScopeTests(_AiringTestCase):
    """"Only what is in my guide" on the airing grain - the gap the retired Extended Search
    modal's `Only show channels in guide` checkbox left behind (dev/changelog/416, closed by
    734).

    `f.other=guide` used to read `Channel.in_guide`, which is not the guide: the column also
    records "restore this as a guide row if its group dissolves", so it claims channels that
    have no guide row and misses every channel that reaches the guide through a group. A
    channel group IS one guide row, so a showing on any of its members is a showing in the
    guide - which is the whole shape this grain could not express.

    The seeded corpus is exactly that shape: `Fox` is an in-guide group holding ESPN2 HD (its
    own flag on, so since dev/changelog/751 it is BOTH a guide row of its own and a member of
    the group's row) and Sky Sports (no flag of its own). Both air `Wembley Cup Final` at the
    same time, which is why `grpdedup` is off in most of these - collapsing is a separate
    question from scope.
    """

    def guide_titles(self, **kw):
        kw.setdefault('standing', only_hiding())
        return sorted(self.titles(filters=(DimensionFilter('other', ('guide',)),), **kw))

    def test_a_showing_on_a_group_member_with_no_flag_is_in_the_guide(self):
        """Sky Sports carries no `in_guide` of its own and never will - nothing sets a
        member's flag. Its showings reach the guide through the `Fox` row, and before this
        the airing grain had no way to say so."""
        self.assertFalse(self.sky.in_guide)
        self.assertIn('Wembley Cup Final', self.guide_titles())
        self.assertEqual(self.guide_titles(),
                         ['Old Wembley Show', 'Wembley Cup Final', 'Wembley Cup Final'])

    def test_a_flagged_member_stays_in_scope_when_its_group_leaves_the_guide(self):
        """The `Fox Sports 1` shape, inverted by dev/changelog/751. Taking the group out of
        the guide takes Sky Sports' showings with it - it reached the guide only through the
        group - but ESPN2 HD carries its own flag, so its own row and its own showings
        survive. Before the auto-hide was deleted, that flag bought it nothing and this
        returned an empty list."""
        self.group.in_guide = False
        db.session.commit()
        self.assertTrue(self.espn.in_guide)
        self.assertEqual(self.guide_titles(),
                         ['Old Wembley Show', 'Wembley Cup Final'])

    def test_showings_on_a_channel_outside_the_guide_are_excluded(self):
        """BBC News is in no group and has no flag, so nothing it airs is in the guide."""
        titles = self.guide_titles()
        self.assertNotIn('Nightly News', titles)
        self.assertNotIn('Wembley Highlights', titles)

    def test_it_composes_with_collapse_channel_groups(self):
        """Scope and collapsing are separate questions and must stay so: with both on, the
        group's duplicated listing becomes one row rather than being dropped or doubled."""
        titles = self.guide_titles(standing=only_hiding('grpdedup'))
        self.assertEqual(titles.count('Wembley Cup Final'), 1)

    def test_the_facet_count_agrees_with_the_filter(self):
        """The rail's number and the list it opens are the same question asked twice."""
        state = self.bare()
        ctx = self.context()
        counted = _facet_counts(DIMENSION_BY_KEY['other'], state, ctx,
                                text_predicates(state, ctx))
        filtered_total = search(self.bare(
            filters=(DimensionFilter('other', ('guide',)),)), ctx).total
        self.assertEqual(counted['guide'], filtered_total)


class WhenDimensionTests(_AiringTestCase):
    """`when` is the only dimension whose values carry their own state."""

    def test_the_static_values_select_what_they_say(self):
        self.assertEqual(self.titles(standing=only_hiding(),
                                     filters=(DimensionFilter('when', ('now',)),)),
                         ['Nightly News'])
        today = self.titles(standing=only_hiding(),
                            filters=(DimensionFilter('when', ('today',)),))
        self.assertIn('Nightly News', today)
        self.assertNotIn('Wembley Highlights', today)

    def test_a_relative_window_is_parsed_out_of_its_own_value(self):
        self.assertEqual(parse_when_next('next:3:hours'), timedelta(hours=3))
        self.assertEqual(parse_when_next('next:90:minutes'), timedelta(minutes=90))
        self.assertEqual(parse_when_next('next:2:days'), timedelta(days=2))
        titles = self.titles(standing=only_hiding(),
                             filters=(DimensionFilter('when', ('next:6:hours',)),))
        self.assertEqual(sorted(titles), ['Wembley Cup Final', 'Wembley Cup Final'])

    def test_a_custom_range_uses_dots_because_a_wall_clock_time_contains_a_colon(self):
        """`custom:<from>:<to>` cannot be split back apart - `2026-08-01T19:00` has a colon
        in it. This is the reason the separator is `..`, and getting it wrong would show up
        as a silently unparseable window rather than an error."""
        parsed = parse_when_custom('custom:2026-08-01T19:00..2026-08-02T06:30')
        self.assertIsNotNone(parsed)
        self.assertEqual(len(parsed), 2)
        # One bound on its own is a real window.
        self.assertEqual(parse_when_custom('custom:2026-08-01T19:00..')[1], None)
        self.assertEqual(parse_when_custom('custom:..2026-08-01T19:00')[0], None)

    def test_an_unusable_window_matches_nothing_rather_than_raising(self):
        """A number the user is still typing is not a registry key. 400ing the search
        someone is mid-way through filling in is a broken page, not an error message."""
        for value in ('next:x:hours', 'next:0:hours', 'next:3:fortnights', 'custom:junk..'):
            with self.subTest(value=value):
                self.assertEqual(
                    self.titles(standing=only_hiding(),
                                filters=(DimensionFilter('when', (value,)),)), [])

    def test_the_facet_counts_the_static_values_plus_whatever_the_state_names(self):
        """There are infinitely many `next:`/`custom:` values, so the vocabulary cannot be
        enumerated - but a window the user has typed has to carry a count, or the control
        they just filled in reads as broken."""
        state = SearchState(grain=GRAIN_AIRINGS, standing=only_hiding(),
                            facets=('when',),
                            filters=(DimensionFilter('when', ('next:6:hours',)),))
        counts = search(state, self.context()).facets['when']
        self.assertEqual(set(counts) - {'next:6:hours'}, {'now', 'today', 'tomorrow'})
        self.assertEqual(counts['next:6:hours'], 2)

    def test_day_boundaries_follow_the_display_timezone(self):
        """A day starts where the person reading the page is. Storage is naive UTC, so a
        `today` computed at 00:00 UTC would put a Sydney evening in yesterday's bucket."""
        ctx = self.context()
        start, stop = ctx.day_bounds['today']
        self.assertLessEqual(start, ctx.now)
        self.assertLess(ctx.now, stop)
        # A local day is 23, 24 or 25 hours long - never anything else, and never computed
        # by adding 24h to a UTC value.
        self.assertIn(round((stop - start).total_seconds() / 3600), (23, 24, 25))
        self.assertEqual(ctx.day_bounds['tomorrow'][0], stop)


class DurationDimensionTests(_AiringTestCase):
    """`duration` (migration 36, dev/changelog/594) - the program-length filter.

    Every fixture row from `_AiringTestCase._seed()` is a 60-minute showing (`_epg()`'s
    default), so a bound that brackets 60 minutes matches the whole fixture and tells you
    nothing about the bound itself - each test that needs to distinguish short vs. long
    seeds its own rows via `_seed_lengths()` rather than relying on the base fixture to vary.
    """

    def _seed_lengths(self):
        self.short = self._epg(self.bbc, 'Quick Update', 500, duration=15)
        self.long = self._epg(self.bbc, 'Marathon Coverage', 600, duration=240)

    def test_a_lower_bound_alone_is_longer_than(self):
        self._seed_lengths()
        titles = self.titles(standing=only_hiding(),
                             filters=(DimensionFilter('duration', ('dur:120..',)),))
        self.assertEqual(titles, ['Marathon Coverage'])

    def test_an_upper_bound_alone_is_shorter_than(self):
        self._seed_lengths()
        titles = self.titles(standing=only_hiding(),
                             filters=(DimensionFilter('duration', ('dur:..30',)),))
        self.assertEqual(titles, ['Quick Update'])

    def test_both_bounds_together_is_a_real_range(self):
        self._seed_lengths()
        titles = self.titles(standing=only_hiding(),
                             filters=(DimensionFilter('duration', ('dur:45..90',)),))
        # Every base-fixture 60-minute showing, and neither the 15- nor the 240-minute one.
        self.assertEqual(sorted(titles), sorted([
            'Wembley Cup Final', 'Wembley Cup Final', 'Nightly News',
            'Wembley Highlights', 'Old Wembley Show']))

    def test_parse_duration_handles_one_sided_and_malformed_values(self):
        self.assertEqual(parse_duration('dur:30..'), (30, None))
        self.assertEqual(parse_duration('dur:..240'), (None, 240))
        self.assertEqual(parse_duration('dur:30..240'), (30, 240))
        # Neither side present is not a filter, same as parse_when_custom.
        self.assertIsNone(parse_duration('dur:..'))
        self.assertIsNone(parse_duration('dur:junk..'))
        self.assertIsNone(parse_duration('not-dur:30..60'))

    def test_an_unusable_value_matches_nothing_rather_than_raising(self):
        """A bound still being typed is not a registry key - 400ing the search someone is
        mid-way through filling in is a broken page, not an error message."""
        for value in ('dur:..', 'dur:junk..', 'dur:-5..'):
            with self.subTest(value=value):
                self.assertEqual(
                    self.titles(standing=only_hiding(),
                                filters=(DimensionFilter('duration', (value,)),)), [])

    def test_the_facet_counts_only_the_states_own_value_not_a_static_vocabulary(self):
        """Unlike `when`, duration has no closed vocabulary at all - an unfiltered facet
        count is empty, and only a value the user has actually typed carries a count."""
        self._seed_lengths()
        empty_state = SearchState(grain=GRAIN_AIRINGS, standing=only_hiding(),
                                  facets=('duration',))
        self.assertEqual(search(empty_state, self.context()).facets['duration'], {})

        state = SearchState(grain=GRAIN_AIRINGS, standing=only_hiding(),
                            facets=('duration',),
                            filters=(DimensionFilter('duration', ('dur:120..',)),))
        counts = search(state, self.context()).facets['duration']
        self.assertEqual(counts, {'dur:120..': 1})


class StandingOptionTests(_AiringTestCase):

    def test_past_hides_ended_showings_and_says_how_many(self):
        result = self.run_search(standing=only_hiding('showpast'))
        self.assertNotIn('Old Wembley Show', [r.title for r in result.rows])
        self.assertEqual(result.standing_hidden, {'showpast': 1})
        # The numbers add up: what it hid plus what is left is what you get by turning it off.
        self.assertEqual(result.total + 1, search(self.bare(), self.context()).total)

    def test_grpdedup_keeps_one_row_per_group_on_the_best_member(self):
        """`guide_search` did this unconditionally; here it is switchable and disclosed.
        ESPN2 wins because its effective score is higher, which is the same rule the recorder
        uses to pick a member - a collapse that kept a different member than the recorder
        would pick is a row that records the wrong feed."""
        result = self.run_search(standing=only_hiding('grpdedup'), q='wembley cup')
        titles = [(r.title, r.channel_id) for r in result.rows]
        self.assertEqual(titles, [('Wembley Cup Final', self.espn.id)])
        self.assertEqual(result.standing_hidden, {'grpdedup': 1})

    def test_grpdedup_off_shows_every_member(self):
        result = self.run_search(standing=only_hiding(), q='wembley cup')
        self.assertEqual(sorted(r.channel_id for r in result.rows),
                         sorted([self.espn.id, self.sky.id]))

    def test_firstonly_keeps_each_channels_earliest_matching_showing(self):
        result = self.run_search(standing=only_hiding('firstonly'))
        by_channel = {}
        for row in result.rows:
            by_channel.setdefault(row.channel_id, []).append(row)
        self.assertTrue(all(len(v) == 1 for v in by_channel.values()))


class ClusterScopeTests(_AiringTestCase):
    """The two cluster options rank over the RESULT SET, not over the whole table.

    Both cases below failed before the airing grain shipped (dev/changelog/412), when they
    were spelled the way `dup` is spelled on the channel grain. `dup` ranks over the whole
    table on purpose -
    which copy of a duplicated feed is "the one kept" is an identity question and must not
    change because the user typed something. These two are the opposite: they are statements
    about the result set, and ranking them over the whole table makes rows vanish entirely.
    """

    def test_firstonly_does_not_lose_a_channel_whose_earliest_showing_has_ended(self):
        """ESPN2's earliest showing ever is `Old Wembley Show`, which `past` has already
        removed. Ranked over the whole table, ESPN2's surviving Wembley Cup Final would be a
        "later showing" and the channel would contribute NO row at all."""
        result = self.run_search(standing=only_hiding('showpast', 'firstonly'))
        self.assertIn(self.espn.id, {r.channel_id for r in result.rows})

    def test_grpdedup_falls_back_to_the_next_member_when_the_best_one_is_filtered_out(self):
        """Filter to Sky's showing alone and it must survive. Ranked over the whole table,
        ESPN2 would still be the group's winner for that program, so the only row the filter
        left would be discarded and the search would return nothing."""
        result = self.run_search(standing=only_hiding('grpdedup'),
                                 filters=(DimensionFilter('chan', (str(self.sky.id),)),))
        self.assertEqual([r.channel_id for r in result.rows], [self.sky.id])


class GroupCollapseSpellingTests(_AiringTestCase):
    """`grpdedup`'s loser set is one pass over the ranked join, and must hide the same rows.

    Until dev/changelog/657 it was spelled `grouped EXCEPT winners` - two SELECTs over one
    windowed subquery, so SQLite materialized the whole `epg_entries x channels x
    channel_group_members x channel_groups` join and its window sort twice per statement and
    reconciled them through a temp B-tree. The option is on by default and `base_predicates()`
    attaches it to the page query, the breakdown and every facet aggregate alike, so that
    doubled join was paid on nearly every airings statement: 2.5s (unfiltered breakdown) and
    1.7s (unfiltered page) against a 2.19M-row `epg_entries`.

    A rewrite that only looks equivalent is the whole risk, so both halves are asserted: the
    two spellings' id sets, and the behavior that separates the correct single-pass spelling
    (`min(rn) > 1`) from the naive one (`rn > 1`). The naive one agrees on everything the base
    corpus contains - it needs a channel in MORE THAN ONE group to diverge, which is why this
    class seeds a second group rather than reusing the inherited one.
    """

    def setUp(self):
        super().setUp()
        # Sky is now in two channel-kind groups, and the cup is on all three channels at the
        # same time. Sky LOSES the cup to ESPN2 in "Fox" (90 vs 50) and WINS it in "Freeview"
        # (50 vs 20). An airing is one row and cannot be emitted once per group, so placing
        # second in one group is not enough to hide it.
        self.dim = seed.make_channel(self.acct, name='Grainy Sports', category_name='Sports',
                                     health_score=20.0, last_seen_at=self.now)
        self.cup_dim = self._epg(self.dim, 'Wembley Cup Final', 30,
                                 desc='Liverpool at Wembley')
        self.group2 = seed.make_group(name='Freeview', members=[self.sky, self.dim])
        db.session.commit()

    def _ids(self, stmt):
        return sorted(row[0] for row in db.session.execute(stmt))

    def _except_spelling(self, row_preds=()):
        """The pre-657 spelling, over the SAME ranked window the live one uses.

        Sharing `_group_ranked_entries` rather than copying its window spec is the point: a
        copied spec would agree with itself forever and start failing spuriously the first
        time the ranking rule legitimately changes.
        """
        ranked = _group_ranked_entries(row_preds)
        return select(ranked.c.id).except_(select(ranked.c.id).where(ranked.c.rn == 1))

    def test_the_single_pass_spelling_hides_exactly_what_except_hid(self):
        self.assertEqual(self._ids(_group_collapse_losers()),
                         self._ids(self._except_spelling()))

    def test_the_two_spellings_agree_on_a_narrowed_window_too(self):
        """`grpdedup` is a cluster option: it ranks over the rows the search would otherwise
        show, not over the whole table (see `ClusterScopeTests`). So the equivalence has to
        hold for a narrowed `row_preds` as well, which is how it is actually called."""
        preds = (EPGEntry.title == 'Wembley Cup Final',)
        self.assertEqual(self._ids(_group_collapse_losers(preds)),
                         self._ids(self._except_spelling(preds)))

    def test_a_showing_that_wins_one_group_and_places_second_in_another_is_kept(self):
        """The case a bare `rn > 1` gets wrong, and the reason the aggregate is `min(rn)`.

        Sky's cup entry ranks 2 in Fox and 1 in Freeview; only the weakest member's copy,
        which wins nowhere, is hidden."""
        losers = self._ids(_group_collapse_losers())
        self.assertIn(self.cup_dim.id, losers)
        self.assertNotIn(self.cup_sky.id, losers)
        self.assertNotIn(self.cup_espn.id, losers)

    def test_the_search_keeps_one_row_per_group_winner_and_says_how_many_it_hid(self):
        """End to end through `search()`, not just the subquery: two group winners survive
        and the disclosure count matches what was actually removed."""
        result = self.run_search(standing=only_hiding('grpdedup'), q='wembley cup')
        self.assertEqual(sorted(r.channel_id for r in result.rows),
                         sorted([self.espn.id, self.sky.id]))
        self.assertEqual(result.standing_hidden, {'grpdedup': 1})


class PlannerEquivalenceTests(_AiringTestCase):
    """The two query paths must return the same rows. This one IS a guard.

    The planner exists because neither path wins outright (the measurements are in
    `app/search_index.py`), and it is only safe because the index can never decide a row -
    it can only narrow which rows the real LIKE is applied to. Assert that, not the plan.
    """

    def _ids(self, q, narrow, **kw):
        state = SearchState(grain=GRAIN_AIRINGS, q=q, facets=(), standing=only_hiding(),
                            fields=('name', 'epg-title', 'epg-desc'), **kw)
        ctx = SearchContext.build(TEST_CFG)
        with mock.patch('app.channel_search.airing_narrowing_decision',
                        return_value=(narrow, '')):
            return sorted(r.id for r in search(state, ctx).rows)

    def test_both_paths_agree_on_every_term_shape(self):
        rebuild_search_indexes('test')
        for q, kw in (('wembley', {}), ('news', {}), ('liverpool', {}),
                      ('wembley news', {'match_all': False}),
                      ('wembley news', {'match_all': True}),
                      ('wembley -highlights', {}),
                      ('"wembley cup"', {})):
            with self.subTest(q=q, **kw):
                self.assertEqual(self._ids(q, True, **kw), self._ids(q, False, **kw))

    def test_the_planner_refuses_to_narrow_where_the_index_cannot_answer(self):
        """Each of these is correctness, not tuning. A wildcard is not a trigram phrase, a
        short term makes an FTS MATCH return zero rows rather than erroring, and chan_prog
        holds future showings only - so narrowing with `past` off would silently drop exactly
        the rows the user turned it off to see."""
        rebuild_search_indexes('test')
        ctx = self.context()
        cases = {
            'wem*': 'wildcard',
            'ab': 'characters',
        }
        for q, expect in cases.items():
            with self.subTest(q=q):
                narrow, why = airing_narrowing_decision(
                    SearchState(grain=GRAIN_AIRINGS, q=q, fields=('epg-title',)), ctx)
                self.assertFalse(narrow)
                self.assertIn(expect, why)
        narrow, why = airing_narrowing_decision(
            SearchState(grain=GRAIN_AIRINGS, q='wembley', fields=('epg-title',),
                        standing=only_hiding()), ctx)
        self.assertFalse(narrow)
        self.assertIn('future', why)

    def test_a_stale_index_forces_the_scan_rather_than_serving_partial_rows(self):
        """The indexes are never built in this test, so readiness says no. A search that is
        correct but slower is the right answer; one that is fast and wrong is not."""
        narrow, why = airing_narrowing_decision(
            SearchState(grain=GRAIN_AIRINGS, q='wembley', fields=('epg-title',)),
            self.context())
        self.assertFalse(narrow)
        self.assertTrue(why)

    def test_the_cost_lever_is_on_and_a_common_term_is_refused(self):
        """The threshold is a measured value, not a default (dev/changelog/677): past it the
        narrowed channel set covers most of `epg_entries`, so narrowing narrows nothing and
        only adds a sort. Both halves are pinned - a rare term narrows and a term over the
        threshold does not - so the lever cannot rot into something that no longer works."""
        import app.search_index as index_mod
        rebuild_search_indexes('test')
        state = SearchState(grain=GRAIN_AIRINGS, q='wembley', fields=('epg-title',))

        ctx = self.context()
        self.assertIsNotNone(index_mod.AIRING_PROBE_MAX_ROWS)
        self.assertEqual(airing_narrowing_decision(state, ctx), (True, ''))
        self.assertTrue(ctx.probe_cache, 'the probe must run while a threshold is set')

        ctx = self.context()
        with mock.patch.object(index_mod, 'AIRING_PROBE_MAX_ROWS', 1), \
                mock.patch('app.channel_search.AIRING_PROBE_MAX_ROWS', 1):
            narrow, _why = airing_narrowing_decision(state, ctx)
        self.assertFalse(narrow, 'a term at the threshold must fall back to the scan')

    def test_the_probe_cap_stays_above_the_threshold_it_feeds(self):
        """`airing_probe_count` stops counting at `AIRING_PROBE_LIMIT`, so a cap at or below
        `AIRING_PROBE_MAX_ROWS` makes every term report the cap and compare as too common -
        silently disabling narrowing for everything. The cap was 20,000 when the threshold
        was chosen, which capped a term that wins and a term that loses at the same number
        (dev/changelog/677)."""
        import app.search_index as index_mod
        self.assertGreater(index_mod.AIRING_PROBE_LIMIT, index_mod.AIRING_PROBE_MAX_ROWS)

    def test_an_unindexed_search_still_finds_the_same_rows(self):
        self.assertEqual(sorted(self.titles(standing=only_hiding(), q='wembley')),
                         ['Old Wembley Show', 'Wembley Cup Final', 'Wembley Cup Final',
                          'Wembley Highlights'])


class NarrowingConjunctTests(_AiringTestCase):
    """The top-level `channel_id IN (...)` conjunct (dev/changelog/677).

    `_term_predicate` can only put the narrowing inside an OR with the channel-name side,
    and an OR spanning two tables is undrivable by any index, so every typed airings search
    walked the whole future half of `epg_entries` however rare the term was. The conjunct is
    the same narrowing restated where the planner can drive it - implied by that OR, so
    redundant by construction.

    The identity tests below are the safety property, and they are guards: the conjunct is
    only sound because it can never remove a row the OR keeps, and a test that exercised
    only the narrowed path would let the two drift silently.
    """

    def _ids(self, on, **kw):
        kw.setdefault('fields', ('name', 'epg-title', 'epg-desc'))
        state = SearchState(grain=GRAIN_AIRINGS, facets=(), standing=only_hiding(), **kw)
        ctx = SearchContext.build(TEST_CFG)
        real = channel_search._airing_narrowing_conjuncts
        with mock.patch('app.channel_search.airing_narrowing_decision',
                        return_value=(True, '')), \
                mock.patch.object(channel_search, '_airing_narrowing_conjuncts',
                                  real if on else (lambda *a, **k: [])):
            res = search(state, ctx)
        return sorted(r.id for r in res.rows), res.total

    def test_the_conjunct_removes_no_row_the_or_keeps(self):
        rebuild_search_indexes('test')
        for kw in ({'q': 'wembley'}, {'q': 'news'}, {'q': 'liverpool'},
                   {'q': 'wembley news', 'match_all': True},
                   {'q': 'wembley news', 'match_all': False},
                   {'q': 'wembley -highlights'},
                   {'q': '"wembley cup"'},
                   {'q': 'wembley', 'fields': ('name',)},
                   {'q': 'wembley', 'fields': ('epg-title',)},
                   {'q': 'wembley', 'fields': ('sid', 'epg-title')}):
            with self.subTest(**kw):
                self.assertEqual(self._ids(True, **kw), self._ids(False, **kw))

    def test_a_channel_side_only_hit_survives_the_conjunct(self):
        """The half most easily lost. `BBC News` matches on channel NAME while none of its
        showings mention it, so a conjunct built from the program arm alone would drop every
        one of its airings - the OR's first arm has to be in the union too."""
        rebuild_search_indexes('test')
        ids, _total = self._ids(True, q='bbc', fields=('name', 'epg-title'))
        self.assertEqual(ids, sorted([self.on_now.id, self.later.id]))

    def test_it_narrows_on_included_terms_only(self):
        """`AND NOT (narrowing AND like)` is not `AND NOT like`, so an excluded term must
        never reach the union - narrowing to the channels an excluded term matches would
        keep exactly the rows the user asked to remove."""
        rebuild_search_indexes('test')
        ids, _total = self._ids(True, q='wembley -highlights',
                                fields=('name', 'epg-title'))
        self.assertNotIn(self.later.id, ids)
        self.assertIn(self.cup_espn.id, ids)

    def test_match_all_narrows_per_term_and_match_any_narrows_once(self):
        """Under match-all every term has to hold, so each term's own union is a valid
        narrowing by itself. Under match-any a row need satisfy only ONE term, so only the
        union across every term is implied - a per-term conjunct there would drop rows."""
        fields = tuple(FIELD_BY_KEY[k] for k in ('name', 'epg-title'))
        terms = parse_terms('wembley news')
        self.assertEqual(
            len(channel_search._airing_narrowing_conjuncts(terms, fields, True, True)), 2)
        self.assertEqual(
            len(channel_search._airing_narrowing_conjuncts(terms, fields, True, False)), 1)

    def test_no_channel_field_means_no_conjunct(self):
        """With no channel-side arm `_term_predicate` emits no OR at all, so its own
        `channel_id IN (...)` is already top-level and already drivable. Restating it makes
        the planner run the subquery twice - measured as a real regression on the live
        database (`football` over program fields alone, 348ms -> 476ms, dev/changelog/677)."""
        fields = tuple(FIELD_BY_KEY[k] for k in ('epg-title', 'epg-desc'))
        self.assertEqual(
            channel_search._airing_narrowing_conjuncts(
                parse_terms('wembley'), fields, True, True), [])

    def test_the_conjunct_is_absent_whenever_the_planner_refuses(self):
        """One gate, one meaning: everything `airing_narrowing_decision()` refuses for -
        the five correctness cases and the cost threshold - must take the conjunct with it,
        or a refusal that exists to protect correctness only half applies."""
        rebuild_search_indexes('test')
        fields = ('name', 'epg-title')
        for kw in ({'q': 'wem*'}, {'q': 'ab'}, {'q': '-wembley'},
                   {'q': 'wembley', 'standing': only_hiding()}):
            with self.subTest(**kw):
                kw.setdefault('standing', only_hiding('showpast'))
                state = SearchState(grain=GRAIN_AIRINGS, facets=(), fields=fields, **kw)
                ctx = SearchContext.build(TEST_CFG)
                self.assertFalse(airing_narrowing_decision(state, ctx)[0])
                preds = text_predicates(state, ctx)
                self.assertFalse(
                    any('epg_entries.channel_id IN' in str(p.compile()) for p in preds))

    def test_a_narrowed_search_actually_emits_it(self):
        """The other half of the test above - that the conjunct is reached at all on the
        ordinary path, so the refusal test cannot pass by the conjunct never existing."""
        rebuild_search_indexes('test')
        state = SearchState(grain=GRAIN_AIRINGS, q='wembley', facets=(),
                            fields=('name', 'epg-title'))
        ctx = SearchContext.build(TEST_CFG)
        self.assertTrue(airing_narrowing_decision(state, ctx)[0])
        preds = text_predicates(state, ctx)
        self.assertTrue(any('epg_entries.channel_id IN' in str(p.compile()) for p in preds))


class RowPayloadTests(_AiringTestCase):

    def test_a_row_carries_the_showing_and_the_channel_it_is_on(self):
        row = next(r for r in self.rows(standing=only_hiding(), q='"wembley cup"')
                   if r['channel']['id'] == self.espn.id)
        self.assertEqual(row['title'], 'Wembley Cup Final')
        self.assertEqual(row['description'], 'Liverpool at Wembley')
        self.assertEqual(row['channel']['name'], 'ESPN2 HD')
        self.assertEqual(row['channel']['health'], 90.0)
        self.assertEqual(row['channel']['account']['name'], 'Alpha')
        self.assertIn('Fox', [g['name'] for g in row['channel']['groups']])
        self.assertEqual([t['name'] for t in row['channel']['tags']], ['sports'])
        self.assertTrue(row['channel']['in_guide'])

    def test_times_go_out_as_naive_utc_iso_strings(self):
        row = self.rows(standing=only_hiding(), q='"wembley cup"')[0]
        for key in ('start_time', 'stop_time'):
            self.assertRegex(row[key], r'^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}$')

    def test_the_stream_url_is_masked(self):
        """A stream URL routinely carries the account's credentials in its path, and this
        payload is bound for a JSON response any browser tab can read."""
        self.espn.stream_url = 'http://host.test/user9/pass9/1234'
        db.session.commit()
        row = next(r for r in self.rows(standing=only_hiding(), q='"wembley cup"')
                   if r['channel']['id'] == self.espn.id)
        self.assertNotIn('pass9', row['channel']['stream_url'])

    def test_the_record_state_is_per_showing_and_names_the_past(self):
        rows = {r['title']: r for r in self.rows(standing=only_hiding())}
        self.assertEqual(rows['Old Wembley Show']['record_state'], 'past')
        self.assertEqual(rows['Nightly News']['record_state'], 'none')
        self.assertTrue(rows['Nightly News']['on_now'])
        self.assertTrue(rows['Old Wembley Show']['ended'])

    def test_a_scheduled_recording_is_matched_to_the_showing_it_covers(self):
        seed.make_recording(status='SCHEDULED', name='cup',
                            channel_id=self.espn.id,
                            start_time=self.cup_espn.start_time,
                            stop_time=self.cup_espn.stop_time)
        db.session.commit()
        rows = {(r['title'], r['channel']['id']): r for r in self.rows(standing=only_hiding())}
        self.assertEqual(rows[('Wembley Cup Final', self.espn.id)]['record_state'],
                         'scheduled')
        # ...and not to the OTHER member's identical showing, which nothing is recording.
        self.assertEqual(rows[('Wembley Cup Final', self.sky.id)]['record_state'], 'none')

    def test_a_suggested_filename_is_rendered_server_side(self):
        """The template can come from the channel's default profile and its {tag:...} tokens
        resolve against Tag rows - neither of which the browser has. Leaving it out would
        cost a round trip per Record click."""
        row = self.rows(standing=only_hiding(), q='"wembley cup"')[0]
        self.assertIn('Wembley Cup Final', row['suggested_name'])

    def test_the_why_chip_only_fires_for_a_channel_side_hit(self):
        """On this grain the program IS the row, so a title hit needs no explanation. Only a
        match on something not on screen - the channel's name, id or URL - is a surprise."""
        rows = self.rows(standing=only_hiding(), q='wembley', fields=('name', 'epg-title'))
        self.assertTrue(all(r['why'] is None for r in rows))
        rows = self.rows(standing=only_hiding(), q='ESPN2', fields=('name', 'epg-title'))
        self.assertTrue(rows)
        self.assertEqual({r['why']['field'] for r in rows}, {'name'})


class SortTests(_AiringTestCase):

    def test_the_default_order_is_earliest_first(self):
        starts = [r.start_time for r in self.run_search(standing=only_hiding()).rows]
        self.assertEqual(starts, sorted(starts))

    def test_a_channel_column_can_order_airings_because_the_query_joins_channels(self):
        names = [r.channel_id for r in self.run_search(standing=only_hiding(),
                                                       sort='channel').rows]
        self.assertEqual(names[0], self.bbc.id)

    def test_descending_reverses_it(self):
        starts = [r.start_time
                  for r in self.run_search(standing=only_hiding(), sort_desc=True).rows]
        self.assertEqual(starts, sorted(starts, reverse=True))


class OrderByTiebreakTests(_AiringTestCase):
    """The row query's ORDER BY names each column once, and always ends in the id tiebreak.

    Seven of the nine airing sorts already end in `start_time`, and the tiebreak appended
    `start_time` again - so the emitted ORDER BY read `start_time, start_time, id`. A
    repeated term cannot change a single row's position (it only breaks ties the earlier
    term already broke), so no behavioral test can see it, which is exactly why it survived
    and why this asserts on the statement.

    It was never harmless to the query planner. While a standalone ix_epg_entries_start_time
    still existed beside ix_epg_entries_start_stop, the duplicate was the only thing keeping
    SQLite on the composite index; deleting it as obvious dead weight took the default
    airings page from 0.091s to 1.222s on the production database. Migration 39 removed that
    decoy index so the plan no longer depends on the spelling, and this keeps the spelling
    clean either way (dev/changelog/692).
    """

    def _order_by(self, **kwargs):
        with IOCounter(all_engines()) as counter:
            self.run_search(standing=only_hiding(), **kwargs)
        paged = [s for s in counter.statements if 'ORDER BY' in s and 'LIMIT' in s]
        self.assertTrue(paged, f'no paged row query was issued: {counter.statements}')
        return [s.rsplit('ORDER BY', 1)[1].split('LIMIT')[0] for s in paged]

    def test_no_sort_repeats_a_column_in_its_order_by(self):
        for sort in sorted(channel_search.SORTS_AIRINGS):
            for desc in (False, True):
                for clause in self._order_by(sort=sort, sort_desc=desc):
                    columns = re.findall(r'epg_entries\.(\w+)|channels\.(\w+)', clause)
                    flat = [a or b for a, b in columns]
                    self.assertEqual(
                        sorted(flat), sorted(set(flat)),
                        f'sort={sort} desc={desc} repeats a column: {clause.strip()}')

    def test_every_sort_still_ends_in_the_id_tiebreak(self):
        """Deduping must not be able to eat the tiebreak - without it pages overlap."""
        for sort in sorted(channel_search.SORTS_AIRINGS):
            for desc in (False, True):
                for clause in self._order_by(sort=sort, sort_desc=desc):
                    self.assertRegex(clause.strip(), r'epg_entries\.id\s*$',
                                     f'sort={sort} desc={desc} lost its id tiebreak')

    def test_the_default_sort_still_orders_by_start_time_first(self):
        clause = self._order_by(sort='when')[0]
        self.assertRegex(clause.strip(), r'^\s*epg_entries\.start_time\s*,')

    def test_pages_stay_disjoint_when_every_row_ties_on_the_sort_key(self):
        """The behavioral half: identical start times on every row, so only the tiebreak
        decides. A dedupe that dropped it would show up here rather than in a plan."""
        for entry in EPGEntry.query.all():
            entry.start_time = self.now
        db.session.commit()
        seen = []
        for page in (1, 2):
            seen.extend(r.id for r in self.run_search(standing=only_hiding(), page=page,
                                                      page_size=2).rows)
        self.assertEqual(len(seen), len(set(seen)))


class ApiTests(_AiringTestCase):

    def get(self, query=''):
        resp = self.t.client.get('/api/channels/search?grain=airings&' + query)
        self.assertEqual(resp.status_code, 200, resp.get_data(as_text=True)[:400])
        return resp.get_json()

    def test_the_envelope_names_the_grain_and_carries_airing_rows(self):
        payload = self.get('standing=')
        self.assertEqual(payload['grain'], 'airings')
        self.assertTrue(payload['rows'])
        self.assertIn('start_time', payload['rows'][0])
        self.assertIn('channel', payload['rows'][0])

    def test_the_query_string_round_trips_the_state(self):
        payload = self.get('q=wembley&f.when=today')
        self.assertIn('grain=airings', payload['query_string'])
        self.assertIn('f.when=today', payload['query_string'])

    def test_a_cross_grain_sort_is_a_400(self):
        resp = self.t.client.get('/api/channels/search?grain=airings&sort=name')
        self.assertEqual(resp.status_code, 400)
        self.assertIn('name', resp.get_json()['error'])

    def test_the_catalog_advertises_both_grains_and_their_registries(self):
        payload = self.t.client.get('/api/channels/search/catalog').get_json()
        self.assertEqual(payload['grains'], ['channels', 'airings'])
        airings = payload['by_grain']['airings']
        self.assertEqual(airings['dimensions'][0], 'when')
        self.assertIn('grpdedup', airings['standing_options'])
        self.assertEqual(airings['default_sort'], 'when')
        self.assertNotIn('name', airings['sorts'])
        self.assertIn('when', airings['sorts'])
        # The scope the page opens on, which is where the grain's default reaches the client
        # at all - `channel-search.js::defaultFieldsFor` reads exactly this key.
        self.assertEqual(airings['default_fields'], ['epg-title', 'epg-sub', 'epg-desc'])
        # The channel grain's own keys stay where every existing caller reads them.
        self.assertEqual(payload['default_sort'], 'name')
        self.assertEqual(payload['by_grain']['channels']['default_fields'], ['name'])
        self.assertEqual([w['value'] for w in payload['when_values']],
                         ['now', 'today', 'tomorrow'])


class QueryPlanTests(_AiringTestCase):
    """The 2026-08-01 first-paint defect: the airing rail cost 24.6s and the rows request
    6.4s on the live database (dev/docs/BUGS.md 2026-08-01, dev/changelog/420), and its
    successor the same day - the rail's Today chip at 40.2s and On now at 11.5s
    (dev/changelog/421).

    **None of these measures wall clock, and none can.** The defect was a query PLAN -
    SQLite drove every statement off an index whose range matched a huge slice of a 1.47M-row
    table - and this corpus is five EPG rows, where the planner has no reason to make the
    same choice. So these pin the two things that produced the plan and are visible at any
    size: the SPELLING of the predicate, and the NUMBER of statements the rail issues. Same
    limitation as a jsdom test that cannot see layout: it guards the cause, not the symptom.
    """

    #: A bare (un-`+`-prefixed) range comparison on an epg_entries time column.
    def _bare(self, column):
        return re.compile(r'(?<!\+)epg_entries\.%s\s*(<=|>=|<|>)' % column)

    def _statements(self, **kw):
        state = SearchState(grain=GRAIN_AIRINGS, **kw)
        ctx = self.context()          # built outside the window - it runs its own queries
        # The `monitored` Other value resolves an id set in Python (channel_tester's
        # monitored_channel_ids), and it is a per-CONTEXT fact like everything else build()
        # reads - lazy only so a search that never asks for it does not pay. Warm it here
        # for the same reason the context itself is built outside the window: these counts
        # are about the shape of the facet pass, not about one-off context resolution.
        # That it resolves exactly once per request is asserted separately, by
        # test_channel_search.MonitoredFilterTests (dev/changelog/1065).
        ctx.monitored_channel_ids()
        # The unfiltered airing breakdown is cached (dev/changelog/598) - clear it first so
        # every measurement is a cold statement count, not a cache hit skewed by an earlier
        # call in the same test.
        clear_standing_breakdown_cache()
        with IOCounter(all_engines()) as counter:
            search(state, ctx)
        return counter.statements

    def _when(self, value, **kw):
        return self._statements(filters=(DimensionFilter('when', (value,), ()),), **kw)

    def test_the_past_option_is_spelled_so_sqlite_cannot_drive_off_the_stop_time_index(self):
        """`+epg_entries.stop_time`, never a bare column reference. The `+` is SQLite's
        documented no-op prefix and it is the entire fix: written bare, this predicate is an
        indexable range over half the table and every statement carrying it (the row query,
        the breakdown, all six facet aggregates) pays a random rowid lookup per matched row.

        Scoped to the default state on purpose - a `when` filter emits its own time ranges,
        and which of those may be indexable is the separate rule the tests below pin."""
        statements = self._statements(facets=())
        self.assertTrue(any('+epg_entries.stop_time' in s for s in statements),
                        'the past option no longer carries the index-defeating spelling')
        bare = re.compile(r'(?<!\+)epg_entries\.stop_time\s*(<=|>=|<|>)')
        offenders = [s for s in statements if bare.search(s)]
        self.assertEqual(offenders, [],
                         'a bare epg_entries.stop_time comparison is back in the default '
                         'airing search - it lets the planner drive off ix_epg_entries_'
                         'stop_time again')

    def test_a_wide_when_window_defeats_the_start_time_index(self):
        """A day window matched 366,592 of 1,474,199 rows on the live database, so driving
        the outer loop off `ix_epg_entries_start_stop` cost a random rowid lookup per matched
        row on each of the rail's 8-9 statements: 35.0s for the whole request against 6.55s
        with the index taken out of the planner's reach."""
        statements = self._when('today', facets=())
        self.assertTrue(any('+epg_entries.start_time' in s for s in statements),
                        'a day window no longer carries the index-defeating spelling')
        self.assertEqual([s for s in statements if self._bare('start_time').search(s)], [],
                         'a bare epg_entries.start_time range is back in a Today search - it '
                         'lets the planner drive off ix_epg_entries_start_stop again')

    def test_a_narrow_when_window_keeps_the_start_time_index(self):
        """The other half of the rule, and the reason it is a width test rather than a
        blanket one. At 24,337 matched rows the index still wins (4.12s against 4.76s
        defeated), because the defeated plan is a sequential scan whose cost barely moves
        with the match count while the indexed one scales with it.

        This one PASSES against the pre-fix code, because a narrow window was indexable
        before too - it is a characterization test, not a regression guard. What it stops is
        a future editor "cleaning up" the threshold into an unconditional defeat, which would
        regress every short window and which no measurement in the suite would catch."""
        statements = self._when('next:15:minutes', facets=())
        self.assertTrue(any(self._bare('start_time').search(s) for s in statements),
                        'a 15-minute window should still be indexable on start_time')
        self.assertEqual([s for s in statements if '+epg_entries.start_time' in s], [],
                         'a narrow window must not defeat the start_time index')

    def test_the_width_threshold_is_what_decides_not_the_kind_of_window(self):
        """Both sides of `WHEN_INDEX_DEFEAT_WIDTH` through ONE window kind, so the rule
        cannot be satisfied by special-casing `today` while `custom:`/`next:` keep whatever
        they had. Anchored to the constant rather than to a literal hour: moving the
        threshold is a measurement decision, moving the RULE is a defect."""
        margin = timedelta(minutes=5)
        start = self.now.replace(second=0, microsecond=0)

        def custom(width):
            stop = start + width
            value = 'custom:%s..%s' % (start.strftime('%Y-%m-%dT%H:%M'),
                                       stop.strftime('%Y-%m-%dT%H:%M'))
            return self._when(value, facets=())

        narrow = custom(WHEN_INDEX_DEFEAT_WIDTH - margin)
        self.assertEqual([s for s in narrow if '+epg_entries.start_time' in s], [],
                         'a window inside the threshold should stay indexable')
        wide = custom(WHEN_INDEX_DEFEAT_WIDTH + margin)
        self.assertTrue(any('+epg_entries.start_time' in s for s in wide),
                        'a window past the threshold should defeat the index')

    def test_an_open_ended_custom_window_counts_as_the_widest_one(self):
        """`custom:` with one side blank is a legal window (§2 of the design doc) and is
        unbounded, so it must fall on the defeat side. Deriving a width from a missing bound
        would either crash or - worse - read as zero and keep the index on the widest search
        the page can express."""
        for value in ('custom:..%s' % self.now.strftime('%Y-%m-%dT%H:%M'),
                      'custom:%s..' % self.now.strftime('%Y-%m-%dT%H:%M')):
            with self.subTest(value=value):
                statements = self._when(value, facets=())
                self.assertTrue(any('+epg_entries.start_time' in s for s in statements),
                                'an open-ended custom window must defeat the index')

    def test_on_now_defeats_the_stop_time_side_and_keeps_the_start_time_side(self):
        """"On now" is an interval OVERLAP and neither side is selective alone - 806,484 rows
        have started and 701,668 have not ended, to return 33,953. Which side is defeated is
        not cosmetic: defeating stop_time is 4.64s, defeating start_time INSTEAD is 25.5s
        (the planner falls onto the wider of the two indexes) and leaving both alone is
        10.28s. So this pins the asymmetry, not just "something is defeated"."""
        statements = self._when('now', facets=())
        self.assertEqual([s for s in statements if self._bare('stop_time').search(s)], [],
                         'the On now filter re-introduced a bare stop_time range - the '
                         'exact spelling the past option exists to avoid')
        self.assertTrue(any(self._bare('start_time').search(s) for s in statements),
                        'On now must leave start_time indexable - defeating that side '
                        'instead measured 5x slower than defeating stop_time')

    def test_the_four_scan_facets_cost_one_statement_between_them(self):
        """Account, category, health and Other are answered by ONE grouped pass on this
        grain, not four aggregates. Measured on the live database: four separate passes cost
        677 + 4609 + 4327 + 3908 = 13,521ms against 2117ms for the combined scan. Asserted as
        a delta against the same search with no facets, so unrelated query-count drift in the
        row path cannot move it."""
        without = len(self._statements(facets=()))
        with_four = len(self._statements(facets=('acct', 'cat', 'health', 'other')))
        self.assertEqual(with_four - without, 1,
                         'the four single-scan facet dimensions should add exactly one '
                         'statement between them, not one each')

    def test_a_filtered_dimension_still_falls_back_to_its_own_aggregate(self):
        """The combined pass counts each dimension against the same base, so a dimension the
        user has filtered ON cannot join it - its own filter has to come off first. Filtering
        one of the four therefore costs two statements, not one.

        The baseline also carries the cat filter (facets=()) rather than being fully
        unfiltered: an unfiltered airing search takes the cached-breakdown path
        (dev/changelog/598), which pays its own fixed watermark-check cost that a filtered
        search never does. Isolating the facet-only cost means holding that variable equal on
        both sides, not comparing a cached-path baseline to an uncached one."""
        state = dict(facets=('acct', 'cat', 'health', 'other'))
        cat_filter = (DimensionFilter('cat', ('Sports',), ()),)
        without = len(self._statements(filters=cat_filter, facets=()))
        filtered = len(self._statements(filters=cat_filter, **state))
        self.assertEqual(filtered - without, 2)

    def test_the_combined_pass_returns_exactly_what_the_separate_aggregates_did(self):
        """The correctness half. Collapsing four aggregates into one grouped scan changes
        which SQL runs, and must not change a single count - on the airing grain a channel is
        counted once per SHOWING on it, which is what makes these add up to the list total.

        Unlike the other three, this one PASSES against the pre-fix code too, because before
        the fix these two were the same code path. It is a characterization test, not a
        regression guard: what it stops is the collapsed pass drifting from the fallback
        later - the failure a filtered dimension would hit in production, and one no
        small-seed test would otherwise see."""
        state = SearchState(grain=GRAIN_AIRINGS,
                            facets=('acct', 'cat', 'health', 'other'))
        ctx = self.context()
        combined = compute_facets(state, ctx)
        text_preds = text_predicates(state, ctx)
        separate = {key: _facet_counts(DIMENSION_BY_KEY[key], state, ctx, text_preds)
                    for key in ('acct', 'cat', 'health', 'other')}
        self.assertEqual(combined, separate)
        # And they are showing counts, not channel counts - the point of the grain.
        self.assertEqual(sum(combined['cat'].values()),
                         search(SearchState(grain=GRAIN_AIRINGS, facets=()), ctx).total)


if __name__ == '__main__':
    unittest.main(verbosity=2)
