"""The channel search engine (app/channel_search.py): what its answers must keep being.

This is the test net phase A owes before anything is built on top of the engine - the JSON
endpoint, then the desktop and mobile pages. The engine is imported by nothing yet, so these
are the only thing that will notice when a phase-B edit changes an answer.

**The defect class this file exists for is a search that is wrong while looking healthy.**
Every assertion below is about a result set, a count, or a raise - never about how the query
was spelled - because the engine has three interchangeable code paths for the same question
(the FTS index, the LIKE fallback on the deduped chan_prog cache, and the LIKE fallback
straight off epg_entries) and all three have to give the same answer. Where a test does pin a
spelling, it says why in its own docstring.

Design decisions being guarded, each of which a future editor could plausibly "simplify" back:

* values within one dimension OR, dimensions AND (the shipped recordings list ANDs both, so
  picking two of anything there matches nothing - the defect this model exists to not copy)
* every search field switched off matches NOTHING, not everything
* a facet is counted with its own filter removed, or the rail dies the moment it is used
* the duplicate keep-rule is a cascade over the whole table, so which copy survives cannot
  change because of what the user typed
* standing options are counted, not silently applied
* chan_prog_fts MATCH is column-scoped, or a titles search silently becomes a description one

Sibling files: tests/test_channel_search_swap.py covers apply_channel_search (the older,
narrower helper the Browse page uses) and the index-staleness matrix underneath both.
"""
import os
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
from app.accounts import NORM_DISABLED, NORM_MPEGTS  # noqa: E402
from app.channel_groups import set_participation  # noqa: E402
from app import channel_tester  # noqa: E402
from app.database import (AccountSyncLog, Channel, ChannelGroup,  # noqa: E402
                          ChannelGroupMember, EPGEntry, OnDemandTestJob, Tag, TagPattern)
from app.channel_search import (  # noqa: E402
    DEFAULT_FIELDS, DEFAULT_SORT, DEFAULT_STANDING, GRAIN_AIRINGS, GROUP_ANY,
    HEALTH_VALUES, MAX_PAGE_SIZE, OTHER_DUP_URL, OTHER_GUIDE_OWN_ROW, OTHER_GUIDE_VIA_GROUP,
    OTHER_IN_GUIDE, OTHER_MONITORED, OTHER_NEW, OTHER_REMOVED, OTHER_VALUES,
    STANDING_OPTIONS, DimensionFilter, SearchContext, SearchState, SearchStateError,
    search, standing_applied)
from app.search_index import rebuild_search_indexes  # noqa: E402

# The config the engine is handed, spelled out rather than read from the real config.yaml:
# channel_missing_after_days drives the "deleted by provider" predicate, and a test whose
# answer moves when a setting is edited is not a test.
TEST_CFG = {'sync': {'channel_missing_after_days': 7}}


class _EngineTestCase(unittest.TestCase):
    """A temp app plus a small corpus exercising every dimension at once.

    The corpus is deliberately shaped like the real data rather than like a fixture: a
    duplicate cluster sharing one stream URL, a channel whose only match is in a program
    description, an untested channel, a provider-removed channel, and the `US| ESPN2 HD`
    name shape that a word-based tokenizer would fail on.
    """

    def setUp(self):
        self.t = make_test_app()
        self.ctx_mgr = self.t.app.app_context()
        self.ctx_mgr.push()
        self._seed()

    def tearDown(self):
        self.ctx_mgr.pop()
        self.t.cleanup()

    # -- seeding ----------------------------------------------------------

    def _seed(self):
        now = datetime.utcnow()
        # url_normalization is set explicitly on both accounts so the `notnorm` option's
        # behavior does not depend on the global default in the real config.yaml.
        self.acct = seed.make_account(name='Alpha', url_normalization=NORM_MPEGTS,
                                      last_sync_at=now)
        self.acct_b = seed.make_account(name='Beta', url_normalization=NORM_DISABLED,
                                        last_sync_at=now)

        self.espn2 = self._channel('US| ESPN2 HD', category_name='Sports', health_score=90.0,
                                   in_guide=True)
        self.deportes = self._channel('ESPN Deportes', category_name='Sports',
                                      health_score=60.0)
        self.discovery = self._channel('Discovery Channel', category_name='Docs',
                                       health_score=40.0)
        self.bbc = self._channel('BBC World News', category_name='News')
        self.ae = self._channel('A & E', category_name='Docs', health_score=85.0,
                                url_normalizable=False)
        self.futbol = self._channel('FUTBOL Total', category_name='Sports',
                                    health_score=75.0, account=self.acct_b,
                                    url_normalizable=False)

        # The duplicate cluster: three rows on one stream URL. espn2 is in_guide, so it wins
        # the first rung of the keep-rule cascade; dup_high would win on health alone, which
        # is what makes the cascade visible.
        self.dup_high = self._channel('Sky Sports Action', category_name='Sports',
                                      health_score=99.0)
        self.dup_low = self._channel('Sky Sports Action HD', category_name='Sports',
                                     health_score=10.0)
        for ch in (self.espn2, self.dup_high, self.dup_low):
            ch.stream_url = 'http://example.test/live/shared'
            ch.is_duplicate_stream_url = True

        # Provider-removed: last seen before the cutoff, on an account that has synced since.
        self.removed = self._channel('Gone Fishing TV', category_name='Docs',
                                     health_score=55.0,
                                     last_seen_at=now - timedelta(days=30))

        self._epg(self.discovery, 'Wembley Cup Final', sub_title='Semi',
                  description='Liverpool play at Wembley in the NASCAR-free zone')
        self._epg(self.bbc, 'Nightly News', description='World headlines')
        self._epg(self.deportes, 'Premier League Live', description='Matchday coverage')

        self.tag_nascar = self._tag('nascar', ['NASCAR'])
        self.tag_espn = self._tag('espn', ['ESPN'])
        self.tag_empty = self._tag('unfinished', [])

        self.group = seed.make_group(name='Fox', members=[self.espn2, self.bbc])
        db.session.commit()

    def _channel(self, name, account=None, **kw):
        kw.setdefault('last_seen_at', datetime.utcnow())
        return seed.make_channel(account or self.acct, name=name, **kw)

    def _epg(self, channel, title, sub_title=None, description=None, offset_minutes=-30):
        """One program that is ON RIGHT NOW, and still ends in the future.

        Both halves are load-bearing. On now, because since dev/changelog/861 the channel
        grain's program fields ask what the channel is airing at this instant - a showing
        starting in 30 minutes is deliberately invisible there now. Ending in the future,
        because chan_prog is built over `stop_time >= now` and the tag machinery still reads
        it, so a fixture that had drifted into the past would silently stop testing tags.
        """
        start = datetime.utcnow() + timedelta(minutes=offset_minutes)
        entry = EPGEntry(channel_id=channel.id, title=title, sub_title=sub_title,
                         description=description, start_time=start,
                         stop_time=start + timedelta(hours=1))
        db.session.add(entry)
        db.session.flush()
        return entry

    def _tag(self, name, patterns):
        tag = Tag(name=name)
        db.session.add(tag)
        db.session.flush()
        for pattern in patterns:
            db.session.add(TagPattern(tag_id=tag.id, pattern=pattern))
        db.session.flush()
        return tag

    # -- running ----------------------------------------------------------

    def build_indexes(self):
        """Populate ch_fts and chan_prog/chan_prog_fts, and prove they read as usable."""
        rebuild_search_indexes('test')
        ctx = self.context()
        ready, reason = ctx.readiness_for(())
        self.assertTrue(ready, f'channels index should be ready after a rebuild: {reason}')
        return ctx

    def context(self):
        return SearchContext.build(TEST_CFG)

    def run_search(self, state=None, ctx=None, **kw):
        state = state or SearchState(**kw)
        return search(state, ctx or self.context())

    def names(self, state=None, ctx=None, **kw):
        """The CHANNEL rows' names. A page can also hold group rows since
        dev/changelog/811; `group_names()` below is how a test asks about those, and keeping
        the two apart is what stops a group called Fox reading as a channel called Fox."""
        return sorted(r.name for r in self.run_search(state, ctx, **kw).rows
                      if isinstance(r, Channel))

    def group_names(self, state=None, ctx=None, **kw):
        return sorted(r.name for r in self.run_search(state, ctx, **kw).rows
                      if isinstance(r, ChannelGroup))

    def all_names(self):
        """Every seeded channel, for the "matches everything" comparisons."""
        return sorted(c.name for c in Channel.query.all())

    def no_standing(self, **kw):
        """A state where no standing option removes a row - the plain "what matches"
        question.

        Several options hide seeded rows by default, so a test about text matching that
        forgot this would be asserting on the standing options instead.

        **Not `frozenset()`** - see `only_hiding()`, which spells out why the empty set is
        now the most restrictive search rather than the loosest.
        """
        return SearchState(standing=only_hiding(), facets=(), **kw)


class SearchStateUrlTests(_EngineTestCase):
    """`from_params` / `to_params` are the API other pages build their links out of.

    A change to a parameter name here is a breaking change to every entry point, not an
    internal rename - so the round-trip and the raise-on-unknown behavior are both pinned.
    """

    def parse(self, pairs):
        return SearchState.from_params(MultiDict(pairs))

    def test_defaults_when_nothing_is_passed(self):
        state = self.parse([])
        self.assertEqual(state.q, '')
        self.assertEqual(state.fields, DEFAULT_FIELDS)
        self.assertTrue(state.match_all)
        self.assertEqual(state.filters, ())
        self.assertEqual(state.standing, DEFAULT_STANDING)
        self.assertEqual(state.sort, DEFAULT_SORT)
        self.assertEqual(state.page, 1)
        self.assertIsNone(state.facets)
        self.assertIsNone(state.add_to_group)
        self.assertIsNone(state.replace_rec)

    def test_round_trips_through_to_params(self):
        state = SearchState(
            q='espn hd', fields=('name', 'url', 'epg-desc'), match_all=False,
            filters=(DimensionFilter('acct', ('2',)),
                     DimensionFilter('cat', ('Sports',), ('News',))),
            standing=frozenset({'shownoepg'}), sort='health', sort_desc=True,
            page=3, page_size=25, facets=('cat', 'tag'), add_to_group=7)
        self.assertEqual(self.parse(state.to_params()), state)

    def test_the_replace_action_context_round_trips(self):
        """The second action context (dev/changelog/416), and it has to survive to_params()
        or "Find another airing" would lose what it came to do on the first keystroke - the
        page rewrites the address bar from this function on every render."""
        state = SearchState(q='wembley', grain=GRAIN_AIRINGS, replace_rec=42)
        self.assertEqual(dict(state.to_params()).get('replace_rec'), '42')
        self.assertEqual(self.parse(state.to_params()), state)

    def test_a_non_numeric_replace_rec_is_dropped_rather_than_erroring(self):
        """Same leniency `add_to_group` has, and for the same reason: an action context is
        a decoration on a search, so a mangled one opens the search undecorated instead of
        400ing the page that carried it."""
        self.assertIsNone(self.parse([('replace_rec', 'nonsense')]).replace_rec)

    def test_filters_come_back_in_registry_order(self):
        """`from_params` walks DIMENSIONS, so a link written in any order parses into one
        canonical order. Worth pinning because SearchState is compared by value - two states
        meaning the same search must not read as different ones."""
        state = self.parse([('f.cat', 'Sports'), ('f.tag', 'nascar'), ('f.acct', '1')])
        self.assertEqual([f.key for f in state.filters], ['tag', 'acct', 'cat'])

    def test_multi_valued_params_are_repeated_never_comma_joined(self):
        """Category, tag and group names are user/provider text. Comma-separating them would
        silently split `Sports, News & Docs` into values that match nothing."""
        state = self.parse([('f.cat', 'Sports, News & Docs'), ('f.cat', 'Movies')])
        self.assertEqual(state.filter_for('cat').values, ('Sports, News & Docs', 'Movies'))
        self.assertEqual(self.parse(state.to_params()), state)

    def test_standing_absent_means_defaults_and_empty_means_none(self):
        """The only way to turn a default-on option off is to send the key present-but-empty,
        so the two cases cannot be allowed to collapse into each other."""
        self.assertEqual(self.parse([]).standing, DEFAULT_STANDING)
        self.assertEqual(self.parse([('standing', '')]).standing, frozenset())
        self.assertEqual(self.parse([('standing', 'shownoepg')]).standing,
                         frozenset({'shownoepg'}))

    def test_empty_standing_survives_the_round_trip(self):
        state = SearchState(standing=frozenset())
        self.assertEqual(self.parse(state.to_params()).standing, frozenset())

    def test_facets_absent_means_all_and_empty_means_none(self):
        """`facets=` is how the endpoint asks for rows with no facet counts at all - the
        46ms path the engine's own measurements tell it to use while the user is typing. It
        has to be expressible in a URL, and it has to survive the round trip: writing an
        empty tuple as nothing at all would read back as "count every dimension"."""
        self.assertIsNone(self.parse([]).facets)
        self.assertEqual(self.parse([('facets', '')]).facets, ())
        self.assertEqual(self.parse([('facets', 'cat'), ('facets', 'tag')]).facets,
                         ('cat', 'tag'))
        self.assertEqual(self.parse(SearchState(facets=()).to_params()).facets, ())

    def test_page_size_is_clamped_and_garbage_falls_back(self):
        self.assertEqual(self.parse([('per_page', str(MAX_PAGE_SIZE * 10))]).page_size,
                         MAX_PAGE_SIZE)
        self.assertEqual(self.parse([('page', 'banana')]).page, 1)
        self.assertEqual(self.parse([('page', '-4')]).page, 1)

    def test_sort_direction_is_carried_by_the_leading_minus(self):
        state = self.parse([('sort', '-health')])
        self.assertEqual((state.sort, state.sort_desc), ('health', True))
        self.assertEqual(self.parse(state.to_params()), state)

    def test_unknown_keys_raise_rather_than_being_ignored(self):
        """A typo in a link has to be a 400 the author sees, not an empty result they debug
        for an hour. Each of these is a separate registry, hence a case each."""
        for pairs, expect in (
                ([('in', 'nope')], 'unknown search field'),
                ([('f.nope', 'x')], 'unknown filter dimension'),
                ([('x.nope', 'x')], 'unknown filter dimension'),
                ([('sort', 'airing')], 'cannot sort channels by'),
                ([('standing', 'nope')], 'unknown standing option'),
                ([('facets', 'nope')], 'unknown facet dimension'),
                ([('grain', 'nope')], 'unknown result grain'),
                # Per-grain since the airing grain shipped: `name` orders channels and means
                # nothing for a row that is a program, so it is a 400 over there and not a
                # quietly different order (dev/changelog/412).
                ([('grain', 'airings'), ('sort', 'name')], 'cannot sort airings by'),
                ([('sort', 'when')], 'cannot sort channels by'),
        ):
            with self.subTest(pairs=pairs):
                with self.assertRaises(SearchStateError) as cm:
                    self.parse(pairs)
                self.assertIn(expect, str(cm.exception))

    def test_the_four_mockup_columns_that_are_not_server_sortable_stay_rejected(self):
        """`airing`, `status`, `groups` and `tags` are sortable in the approved mockup and
        deliberately not here (a correlated EPG lookup per row, a derived lifecycle state,
        and two multi-valued columns). Sorting the current page instead would reorder 100
        rows out of 136,130 and call it a sort, so the registry has to keep saying no."""
        for key in ('airing', 'status', 'groups', 'tags'):
            with self.subTest(key=key), self.assertRaises(SearchStateError):
                self.parse([('sort', key)])

    def test_an_unimplemented_grain_is_refused_by_search_itself(self):
        """Not only by the parser: a caller constructing the state in Python must hit the
        same wall, or a grain added to the URL vocabulary before it has a query lands as an
        empty list rather than an error."""
        with self.assertRaises(SearchStateError):
            search(SearchState(grain='seasons'), self.context())

    def test_an_out_of_grain_sort_is_refused_by_search_itself(self):
        """`from_params` cannot be the only guard: a hand-built state skips it entirely, and
        honouring a sort this grain does not have would mean ordering by something else and
        calling it the requested sort."""
        with self.assertRaises(SearchStateError):
            search(SearchState(grain='airings', sort='name'), self.context())


class TextMatchingTests(_EngineTestCase):
    """What the user typed, across both index states."""

    def test_the_default_scope_on_this_grain_is_the_channels_own_name(self):
        """dev/changelog/860. A row on this grain IS a channel, so the name is the whole
        default: with `epg-title` in it, a typed word matched channels whose only connection
        to it was a program on tonight, which reads as the search being wrong rather than as
        a wider net. The program fields are one tick away, and they are what the AIRING
        grain defaults to."""
        self.build_indexes()
        self.assertEqual(self.names(self.no_standing(q='espn')),
                         ['ESPN Deportes', 'US| ESPN2 HD'])
        # Discovery Channel matches on its program title alone, so the default scope must
        # NOT return it - and adding that field back must.
        self.assertEqual(self.names(self.no_standing(q='wembley')), [])
        self.assertEqual(
            self.names(self.no_standing(q='wembley', fields=('name', 'epg-title'))),
            ['Discovery Channel'])

    def test_a_program_field_on_this_grain_asks_only_what_is_on_right_now(self):
        """dev/changelog/861. A channel row shows exactly one program - its `Now airing`
        column - so a row matched on a showing later tonight has no visible connection to
        what was typed, and its "why" chip has to name a program the row is not showing.
        The airing grain is where "when is this on" is asked.

        Both directions, because either alone would pass on a broken build: the future
        showing must be absent AND the current one present.
        """
        later = self._channel('Later Tonight TV')
        self._epg(later, 'Curling Marathon', offset_minutes=180)
        onnow = self._channel('On Now TV')
        self._epg(onnow, 'Curling Marathon')
        db.session.commit()
        for indexed in (False, True):
            with self.subTest(indexed=indexed):
                if indexed:
                    self.build_indexes()
                self.assertEqual(
                    self.names(self.no_standing(q='curling', fields=('epg-title',))),
                    ['On Now TV'])

    def test_the_now_scope_reaches_the_why_chip_and_the_field_hit(self):
        """The two display surfaces that answer "and why is this row here" have to agree
        with the predicate that put it there, or the row arrives explained by a program it
        is not airing. `matching_programs` used to read chan_prog, which is deduped across
        showings and so cannot express "now" at all."""
        from app.channel_search import field_hit_predicate, matching_programs
        later = self._channel('Later Tonight TV')
        self._epg(later, 'Curling Marathon', offset_minutes=180)
        onnow = self._channel('On Now TV')
        self._epg(onnow, 'Curling Marathon')
        db.session.commit()
        self.build_indexes()
        state = self.no_standing(q='curling', fields=('epg-title',))
        ctx = self.context()
        # Asked about BOTH ids, as the row builder does for a page of rows. The channel whose
        # only matching showing is tonight must be explained by nothing at all.
        named = matching_programs([later.id, onnow.id], state, ctx)
        self.assertNotIn(later.id, named)
        self.assertEqual(named[onnow.id][0], 'Curling Marathon')
        # And the field-hit expression, which draws the "matched on Program title" chip.
        hits = dict(db.session.query(
            Channel.id, field_hit_predicate('epg-title', state, ctx)).filter(
                Channel.id.in_([later.id, onnow.id])).all())
        self.assertEqual([bool(hits[later.id]), bool(hits[onnow.id])], [False, True])

    def test_the_index_and_the_fallback_agree(self):
        """The whole risk of an index is that it answers differently from the scan it
        replaced. Same states, index built vs. never built."""
        states = [self.no_standing(q=q) for q in
                  ('espn', 'ESPN', 'SPN2', 'wembley', 'news', 'zzz-nothing')]
        unindexed = [self.names(s) for s in states]
        self.build_indexes()
        self.assertEqual([self.names(s) for s in states], unindexed)

    def test_terms_and_together_by_default_and_or_under_match_any(self):
        self.build_indexes()
        self.assertEqual(self.names(self.no_standing(q='espn deportes')), ['ESPN Deportes'])
        self.assertEqual(self.names(self.no_standing(q='espn deportes', match_all=False)),
                         ['ESPN Deportes', 'US| ESPN2 HD'])

    def test_each_term_is_matched_against_its_own_text(self):
        """Two terms are two MATCHes against the same FTS table in one query. When they
        shared a bind name the last value bound won for both, so `espn wembley` returned the
        channel airing Wembley - which has no `espn` anywhere - and reversing the two words
        changed the answer entirely (dev/docs/BUGS.md 2026-07-30 02:32 PM).
        """
        self.build_indexes()
        # Spelled out rather than left to the default: `wembley` is program text, and this
        # grain's default scope is the channel's own name (dev/changelog/860), so on the
        # default the second term could not match anywhere and the collision this guards
        # would be invisible.
        both = ('name', 'epg-title')
        self.assertEqual(self.names(self.no_standing(q='espn wembley', fields=both)), [])
        self.assertEqual(self.names(self.no_standing(q='wembley espn', fields=both)), [])
        self.assertEqual(self.names(self.no_standing(q='espn wembley', fields=both,
                                                     match_all=False)),
                         ['Discovery Channel', 'ESPN Deportes', 'US| ESPN2 HD'])

    def test_two_wildcard_terms_are_matched_against_their_own_patterns(self):
        """The same collision on the prefilter path, which builds its own MATCH per term."""
        self.build_indexes()
        both = ('name', 'epg-title')
        self.assertEqual(self.names(self.no_standing(q='Disc*ry Wemb*ey', fields=both)),
                         ['Discovery Channel'])
        self.assertEqual(self.names(self.no_standing(q='Disc*ry Prem*re', fields=both)), [])

    def test_an_excluded_term_is_and_not_in_both_modes(self):
        """`-word` is an AND NOT whichever mode is on: "not this" is never something you
        want ORed into a wider result."""
        for match_all in (True, False):
            with self.subTest(match_all=match_all):
                self.assertEqual(
                    self.names(self.no_standing(q='espn -deportes', match_all=match_all)),
                    ['US| ESPN2 HD'])

    def test_a_quoted_phrase_is_one_term(self):
        self.build_indexes()
        self.assertEqual(self.names(self.no_standing(q='"World News"')), ['BBC World News'])
        # Unquoted, the same two words are two terms that must both match somewhere.
        self.assertEqual(self.names(self.no_standing(q='"News World"')), [])

    def test_fts_operators_in_the_query_are_literal_text(self):
        """The user is describing a substring, not writing a boolean expression. An
        unescaped `AND`/`:`/`(` reaching FTS5 raises or, worse, silently means something."""
        self.build_indexes()
        for q in ('A & E', 'AND', 'name:', '(', '""', '24/7'):
            with self.subTest(q=q):
                self.run_search(self.no_standing(q=q))
        self.assertEqual(self.names(self.no_standing(q='A & E')), ['A & E'])

    def test_every_field_switched_off_matches_nothing(self):
        """Matching everything would look exactly like the search ignoring what was typed."""
        self.build_indexes()
        self.assertEqual(self.names(self.no_standing(q='espn', fields=())), [])

    def test_an_empty_query_matches_everything(self):
        self.build_indexes()
        self.assertEqual(self.names(self.no_standing()), self.all_names())

    def test_a_short_term_still_matches_on_the_like_path(self):
        """Below the trigram minimum FTS5 matches nothing and does not error - the single
        easiest way to ship a silently-empty search."""
        self.build_indexes()
        self.assertEqual(self.names(self.no_standing(q='HD')),
                         ['Sky Sports Action HD', 'US| ESPN2 HD'])

    def test_non_default_fields_are_searchable(self):
        self.build_indexes()
        self.assertEqual(self.names(self.no_standing(q='Docs', fields=('cat',))),
                         ['A & E', 'Discovery Channel', 'Gone Fishing TV'])
        self.assertEqual(self.names(self.no_standing(q='live/shared', fields=('url',))),
                         ['Sky Sports Action', 'Sky Sports Action HD', 'US| ESPN2 HD'])

    def test_stream_id_is_searchable_as_text(self):
        """stream_id is an INTEGER the user types as text, and it is the one field with no
        FTS column behind it - so it takes the CAST + LIKE path in both index states."""
        for build in (False, True):
            if build:
                self.build_indexes()
            with self.subTest(indexed=build):
                self.assertEqual(
                    self.names(self.no_standing(q=str(self.bbc.stream_id), fields=('sid',))),
                    ['BBC World News'])


class WildcardTests(_EngineTestCase):
    """Glob wildcards (feature F5): `*` any run, `?` one character, and nothing else."""

    def test_star_matches_a_run_in_the_middle(self):
        for build in (False, True):
            if build:
                self.build_indexes()
            with self.subTest(indexed=build):
                self.assertEqual(self.names(self.no_standing(q='ESP*2')), ['US| ESPN2 HD'])

    def test_question_mark_matches_exactly_one_character(self):
        for build in (False, True):
            if build:
                self.build_indexes()
            with self.subTest(indexed=build):
                # Quoted, because a space splits a query into two terms - `?` is a wildcard
                # inside one term, not a way to write a phrase.
                self.assertEqual(self.names(self.no_standing(q='"ESPN? HD"')),
                                 ['US| ESPN2 HD'])
                self.assertEqual(self.names(self.no_standing(q='"ESPN?? HD"')), [])

    def test_a_wildcard_reaches_program_text_too(self):
        """The program field is spelled out: this grain's default scope is the channel's own
        name (dev/changelog/860), so on the default there is no program text for a wildcard
        to reach and this would pass against a search that never looked."""
        for build in (False, True):
            if build:
                self.build_indexes()
            with self.subTest(indexed=build):
                self.assertEqual(
                    self.names(self.no_standing(q='Wemb*ey', fields=('name', 'epg-title'))),
                    ['Discovery Channel'])

    def test_a_typed_percent_or_underscore_is_literal_not_a_like_metacharacter(self):
        """The LIKE path is what answers a wildcard, so a `%` or `_` the user typed has to be
        escaped - otherwise `A_E` quietly matches `A & E` and every other three-character
        substring, which is a wrong answer that looks like a clever one."""
        self.build_indexes()
        self.assertEqual(self.names(self.no_standing(q='A_E*')), [])
        self.assertEqual(self.names(self.no_standing(q='%*')), [])
        self.assertEqual(self.names(self.no_standing(q='Discovery%*')), [])

    def test_the_prefilter_does_not_narrow_away_a_real_match(self):
        """A wildcard is answered by narrowing with the trigram index and then applying the
        real pattern. The narrowing is only sound because every string a glob matches
        contains each of its literal runs - so a term whose runs sit in different columns,
        or below the trigram minimum, must still find its row."""
        self.build_indexes()
        self.assertEqual(self.names(self.no_standing(q='Disc*ry Ch*nel')),
                         ['Discovery Channel'])
        # Neither run reaches the trigram minimum, so there is no prefilter to be had and
        # the pattern has to be applied to the base table alone. Scoped to the name to keep
        # the expectation about the pattern rather than about what anything is airing.
        self.assertEqual(self.names(self.no_standing(q='A*E', fields=('name',))),
                         ['A & E', 'Discovery Channel'])


class DescriptionScopeTests(_EngineTestCase):
    """The A3 half: descriptions are a normal working scope field, and a scoped one."""

    def test_a_description_only_word_is_found_only_when_that_field_is_on(self):
        for build in (False, True):
            if build:
                self.build_indexes()
            with self.subTest(indexed=build):
                fields = DEFAULT_FIELDS + ('epg-desc',)
                self.assertEqual(self.names(self.no_standing(q='Liverpool', fields=fields)),
                                 ['Discovery Channel'])
                self.assertEqual(self.names(self.no_standing(q='Liverpool')), [])

    def test_the_program_match_is_column_scoped(self):
        """chan_prog_fts carries title, sub_title and description in one index, so an
        unscoped MATCH would turn every titles-only search into a description search without
        anything looking wrong. `headlines` exists only in a description."""
        self.build_indexes()
        self.assertEqual(self.names(self.no_standing(q='headlines',
                                                     fields=('epg-title',))), [])
        self.assertEqual(self.names(self.no_standing(q='headlines',
                                                     fields=('epg-desc',))),
                         ['BBC World News'])
        self.assertEqual(self.names(self.no_standing(q='Semi', fields=('epg-sub',))),
                         ['Discovery Channel'])
        self.assertEqual(self.names(self.no_standing(q='Semi', fields=('epg-title',))), [])

    def test_only_future_programs_are_searchable(self):
        """chan_prog is built over `stop_time >= now`, and the unindexed path applies the
        same window on purpose - the two must not disagree about what a channel is airing."""
        past = EPGEntry(channel_id=self.ae.id, title='Ancient History',
                        start_time=datetime.utcnow() - timedelta(days=2),
                        stop_time=datetime.utcnow() - timedelta(days=2, hours=-1))
        db.session.add(past)
        db.session.commit()
        self.assertEqual(self.names(self.no_standing(q='Ancient')), [])
        self.build_indexes()
        self.assertEqual(self.names(self.no_standing(q='Ancient')), [])


class DegradedPathTests(_EngineTestCase):
    """A search that is correct but ten times slower has to say so - nothing silent."""

    def test_degraded_names_the_reason_when_the_index_is_unusable(self):
        result = self.run_search(self.no_standing(q='espn'))
        self.assertIn('never been built', result.degraded)

    def test_a_healthy_index_reports_no_degradation(self):
        ctx = self.build_indexes()
        self.assertEqual(self.run_search(self.no_standing(q='espn'), ctx).degraded, '')

    def test_an_empty_query_is_never_degraded(self):
        """There is no text to match, so no index was needed and nothing degraded."""
        self.assertEqual(self.run_search(self.no_standing()).degraded, '')

    def test_a_stale_program_index_does_not_degrade_a_channel_only_search(self):
        """Readiness is evaluated per scope. A name search must not be pushed onto the slow
        path just because the program index happens to be mid-rebuild."""
        self.build_indexes()
        self._epg(self.ae, 'Brand New Airing')
        db.session.commit()
        ctx = self.context()
        self.assertEqual(self.run_search(self.no_standing(q='espn', fields=('name',)),
                                         ctx).degraded, '')
        # ...and the same search WITH a program field in scope is degraded. Spelled out
        # rather than left to the default, which since dev/changelog/860 is the name alone.
        self.assertIn('stale',
                      self.run_search(self.no_standing(q='espn',
                                                       fields=('name', 'epg-title')),
                                      ctx).degraded)


class DimensionFilterTests(_EngineTestCase):
    """The facet filters: how values combine, and what each dimension means."""

    def filtered(self, key, values=(), ex=(), **kw):
        return self.names(self.no_standing(
            filters=(DimensionFilter(key, tuple(values), tuple(ex)),), **kw))

    def test_values_within_a_dimension_or(self):
        """The shipped recordings list ANDs same-field values, so picking two of anything
        there matches nothing. That is the defect this model exists not to copy."""
        self.assertEqual(self.filtered('cat', ['News', 'Docs']),
                         ['A & E', 'BBC World News', 'Discovery Channel', 'Gone Fishing TV'])

    def test_dimensions_and(self):
        both = self.names(self.no_standing(filters=(
            DimensionFilter('cat', ('Sports',)),
            DimensionFilter('acct', (str(self.acct_b.id),)))))
        self.assertEqual(both, ['FUTBOL Total'])

    def test_exclusion_is_the_third_state(self):
        kept = self.filtered('cat', ex=['Sports'])
        self.assertNotIn('US| ESPN2 HD', kept)
        self.assertIn('BBC World News', kept)

    def test_include_and_exclude_can_be_set_on_one_dimension_at_once(self):
        self.assertEqual(self.filtered('cat', values=['Sports', 'News'], ex=['News']),
                         ['ESPN Deportes', 'FUTBOL Total', 'Sky Sports Action',
                          'Sky Sports Action HD', 'US| ESPN2 HD'])

    def test_group_membership_and_the_any_pseudo_value(self):
        self.assertEqual(self.filtered('group', ['Fox']), ['BBC World News', 'US| ESPN2 HD'])
        self.assertEqual(self.filtered('group', [GROUP_ANY]),
                         ['BBC World News', 'US| ESPN2 HD'])
        # Excluding "in any group at all" is how you find the channels in none.
        self.assertNotIn('US| ESPN2 HD', self.filtered('group', ex=[GROUP_ANY]))

    def test_a_tag_matches_on_the_name_or_on_something_airing(self):
        """A tag is a set of literal patterns, not a membership table, and the airing half
        is what makes a tag mean anything on a generically-named channel. `NASCAR` appears
        only inside one program description."""
        self.build_indexes()
        self.assertEqual(self.filtered('tag', ['espn']), ['ESPN Deportes', 'US| ESPN2 HD'])
        self.assertEqual(self.filtered('tag', ['nascar']), ['Discovery Channel'])

    def test_a_tag_with_no_patterns_matches_nothing(self):
        """An empty pattern set is an unfinished tag. The alternative reads as "every
        channel carries this tag"."""
        self.assertEqual(self.filtered('tag', ['unfinished']), [])

    def test_health_bands_use_the_apps_own_cut_points(self):
        # Four bands since dev/changelog/771 - Great (90+) split off the top of Good.
        self.assertEqual(self.filtered('health', ['great']),
                         ['Sky Sports Action', 'US| ESPN2 HD'])
        self.assertEqual(self.filtered('health', ['good']), ['A & E'])
        self.assertEqual(self.filtered('health', ['fair']),
                         ['ESPN Deportes', 'FUTBOL Total', 'Gone Fishing TV'])
        self.assertEqual(self.filtered('health', ['poor']),
                         ['Discovery Channel', 'Sky Sports Action HD'])
        self.assertEqual(self.filtered('health', ['untested']), ['BBC World News'])

    def test_the_band_moves_with_the_manual_adjustment(self):
        """The badges show observed + manual adjustment, so banding on the raw score alone
        would put a channel in one band here and another one on its own page."""
        self.discovery.manual_health_adjustment = 45.0   # 40 -> 85
        db.session.commit()
        self.assertIn('Discovery Channel', self.filtered('health', ['good']))
        self.assertNotIn('Discovery Channel', self.filtered('health', ['poor']))

    def test_health_band_edges(self):
        self.discovery.health_score = 79.0
        self.bbc.health_score = 80.0
        self.ae.health_score = 49.0
        self.espn2.health_score = 89.0
        db.session.commit()
        self.assertIn('Discovery Channel', self.filtered('health', ['fair']))
        self.assertIn('BBC World News', self.filtered('health', ['good']))
        self.assertIn('US| ESPN2 HD', self.filtered('health', ['good']))
        self.assertIn('A & E', self.filtered('health', ['poor']))

    def test_other_carries_the_three_flags(self):
        # `guide` is scope, not Channel.in_guide: BBC World News carries no flag of its own
        # and is here because it is a member of the in-guide `Fox` group. GuideScopeTests
        # below is where that distinction is pinned down.
        self.assertEqual(self.filtered('other', [OTHER_IN_GUIDE]),
                         ['BBC World News', 'US| ESPN2 HD'])
        self.assertEqual(self.filtered('other', [OTHER_DUP_URL]),
                         ['Sky Sports Action', 'Sky Sports Action HD', 'US| ESPN2 HD'])
        self.assertEqual(self.filtered('other', [OTHER_REMOVED]), ['Gone Fishing TV'])

    def test_removed_reads_the_configured_window(self):
        """`channel_missing_after_days: 0` disables the concept, and the predicate has to
        collapse to "nothing" rather than to "everything"."""
        ctx = SearchContext.build({'sync': {'channel_missing_after_days': 0}})
        state = self.no_standing(filters=(DimensionFilter('other', (OTHER_REMOVED,)),))
        self.assertEqual(self.names(state, ctx), [])

    def _completed_sync(self, account, started_at):
        db.session.add(AccountSyncLog(account_id=account.id, started_at=started_at,
                                      completed_at=started_at, status='SUCCESS'))

    def test_new_is_suppressed_during_the_accounts_first_sync_era(self):
        """A freshly-seeded account (0 completed syncs, same as a brand-new production
        account) must not show its channels as 'new', mirroring the digest alert's own
        suppression - a recently-added channel on such an account is exactly what a first
        sync produces wholesale, not a genuine addition."""
        self._channel('Freshly Synced', account=self.acct,
                      first_seen_at=datetime.utcnow() - timedelta(hours=1))
        db.session.commit()
        self.assertNotIn('Freshly Synced', self.filtered('other', [OTHER_NEW]))

    def test_new_once_the_account_is_past_its_first_sync_era(self):
        """Two completed syncs, the earliest well outside the 'new' window, is what takes an
        account out of its first-sync era - matching channel_lifecycle_state()'s own rule."""
        self._completed_sync(self.acct, datetime.utcnow() - timedelta(days=10))
        self._completed_sync(self.acct, datetime.utcnow() - timedelta(days=1))
        self._channel('Freshly Synced', account=self.acct,
                      first_seen_at=datetime.utcnow() - timedelta(hours=1))
        self._channel('Long Established', account=self.acct,
                      first_seen_at=datetime.utcnow() - timedelta(days=30))
        db.session.commit()
        self.assertEqual(self.filtered('other', [OTHER_NEW]), ['Freshly Synced'])
        self.assertNotIn('Long Established', self.filtered('other', [OTHER_NEW]))

    def test_new_reads_the_configured_window(self):
        """`channel_new_within_days: 0` disables the concept, and the predicate has to
        collapse to "nothing" rather than to "everything"."""
        self._completed_sync(self.acct, datetime.utcnow() - timedelta(days=10))
        self._completed_sync(self.acct, datetime.utcnow() - timedelta(days=1))
        self._channel('Freshly Synced', account=self.acct,
                      first_seen_at=datetime.utcnow() - timedelta(hours=1))
        db.session.commit()
        ctx = SearchContext.build({'sync': {'channel_new_within_days': 0}})
        state = self.no_standing(filters=(DimensionFilter('other', (OTHER_NEW,)),))
        self.assertEqual(self.names(state, ctx), [])

    def test_the_hidden_channel_dimension_pins_one_row(self):
        self.assertEqual(self.filtered('chan', [str(self.bbc.id)]), ['BBC World News'])

    def test_a_filter_and_a_query_narrow_together(self):
        self.build_indexes()
        self.assertEqual(
            self.names(self.no_standing(q='espn',
                                        filters=(DimensionFilter('cat', ('Sports',)),))),
            ['ESPN Deportes', 'US| ESPN2 HD'])


class GuideScopeTests(_EngineTestCase):
    """`f.other=guide` is guide SCOPE, never the raw `Channel.in_guide` column.

    The column used to carry two facts at once - "is a guide row" and "restore this as a guide
    row if its group dissolves" - so reading it to answer the first both claimed channels that
    had no guide row and missed every channel reaching the guide through a group. Measured on
    the live database when this was written: the flag was true on 6 channels while 19 fed the
    guide's 8 rows (dev/changelog/734).

    Since dev/changelog/751 the column is honest, so only the second half of that disagreement
    is left: scope is still wider than the column, because a member with its own flag off has
    its listings on screen through the group's row.

    The seeded corpus already has the shape: `Fox` is an in-guide group holding US| ESPN2 HD
    (flag on) and BBC World News (flag off).
    """

    def guide_names(self):
        return self.names(self.no_standing(
            filters=(DimensionFilter('other', (OTHER_IN_GUIDE,)),)))

    def test_a_group_member_with_no_flag_of_its_own_is_in_scope(self):
        """The whole point: a channel that joined the guide THROUGH its group. Nothing ever
        sets a member's own flag - add_channel_to_guide flips the group's instead."""
        self.assertFalse(self.bbc.in_guide)
        self.assertIn('BBC World News', self.guide_names())

    def test_a_flagged_member_stays_in_scope_when_its_group_leaves_the_guide(self):
        """The `Fox Sports 1` shape, inverted by dev/changelog/751: the flag now buys the
        member its own row, so taking the group out of the guide drops only the member that
        reached the guide through it. Before the auto-hide was deleted, this asserted the
        opposite - grouping a flagged channel cost it a row it still claimed to have."""
        self.group.in_guide = False
        db.session.commit()
        self.assertTrue(self.espn2.in_guide)
        self.assertIn('US| ESPN2 HD', self.guide_names())
        self.assertNotIn('BBC World News', self.guide_names())

    def test_a_plain_in_guide_channel_needs_no_group(self):
        """A channel with its own guide row and no membership anywhere is the simple case,
        and must not be lost to the group half of the union."""
        self.deportes.in_guide = True
        db.session.commit()
        self.assertIn('ESPN Deportes', self.guide_names())

    def test_no_membership_ever_takes_a_channel_out_of_scope(self):
        """Joining a group costs a channel nothing (dev/changelog/751). This used to be
        narrower - it read "only a kind='channel' group is a guide row", so a check-only
        group was the exception that spared its members. There are no kinds and no exception
        now: no group of any shape suppresses a member's own row."""
        self.deportes.in_guide = True
        seed.make_group(name='Nightly check', members=[self.deportes], recording=False)
        db.session.commit()
        self.assertIn('ESPN Deportes', self.guide_names())

    def test_a_recording_disabled_member_stays_in_scope(self):
        """Deliberate, and for _group_collapse_losers' reason: dropping members whose
        Recording switch is off would hide the entire schedule of a group that has nothing
        else. Asserted on `recording_enabled`, the column that replaced `disabled_reason` -
        the old spelling assigned an attribute the model no longer maps, so it set nothing
        and this passed without exercising anything."""
        member = next(m for m in self.group.memberships if m.channel_id == self.bbc.id)
        set_participation(member, 'recording_enabled', False)
        db.session.commit()
        self.assertIn('BBC World News', self.guide_names())

    def test_the_facet_count_matches_the_filter(self):
        """The rail's number and the list it opens are the same question asked twice - the
        exact disagreement that made the account page's own count wrong by 3x."""
        self.assertEqual(self.facets()['other'][OTHER_IN_GUIDE], len(self.guide_names()))

    def facets(self):
        state = SearchState(standing=only_hiding(), facets=('other',))
        return search(state, self.context()).facets


class GuideScopeHalvesTests(_EngineTestCase):
    """`guiderow` and `guidegroup` split `guide` into the two questions it could not answer.

    Guide scope is a union, so on its own it cannot say WHICH half puts a channel on screen -
    and on the live database that mattered: every channel reaching the guide did so through a
    group and not one held a row of its own, which the single filter rendered as an
    undifferentiated 37 (dev/changelog/791).

    The seeded corpus has the shape already: the in-guide `Fox` group holds US| ESPN2 HD
    (flag on) and BBC World News (flag off), so ESPN2 is honestly in BOTH halves.
    """

    def half(self, value):
        return self.names(self.no_standing(
            filters=(DimensionFilter('other', (value,)),)))

    def test_own_row_reads_the_column_and_only_the_column(self):
        """This value exists to expose `Channel.in_guide`'s own meaning, so a member with no
        flag of its own must NOT appear - the exact opposite of what `guide` answers."""
        self.assertFalse(self.bbc.in_guide)
        self.assertEqual(self.half(OTHER_GUIDE_OWN_ROW), ['US| ESPN2 HD'])

    def test_via_a_group_is_membership_not_row_less_membership(self):
        """Deliberately not "in scope but holding no row": ESPN2 holds a row AND sits in an
        in-guide group, so it is honestly in both halves. Subtracting would be a second,
        narrower definition of the grouped half and would break the union below."""
        self.assertTrue(self.espn2.in_guide)
        self.assertEqual(self.half(OTHER_GUIDE_VIA_GROUP),
                         ['BBC World News', 'US| ESPN2 HD'])

    def test_a_group_leaving_the_guide_empties_the_grouped_half(self):
        """The grouped half tracks the GROUP's flag, not membership alone - a member of a
        group that is not in the guide reaches the guide through nothing."""
        self.group.in_guide = False
        db.session.commit()
        self.assertEqual(self.half(OTHER_GUIDE_VIA_GROUP), [])
        self.assertEqual(self.half(OTHER_GUIDE_OWN_ROW), ['US| ESPN2 HD'])

    def test_the_two_halves_or_back_to_the_whole(self):
        """The identity that makes this a split rather than a third and fourth definition.
        Values within one dimension OR, so picking both has to reproduce `guide` exactly -
        if it ever stops holding, one of the three predicates has drifted."""
        both = self.names(self.no_standing(filters=(
            DimensionFilter('other', (OTHER_GUIDE_OWN_ROW, OTHER_GUIDE_VIA_GROUP)),)))
        whole = self.names(self.no_standing(
            filters=(DimensionFilter('other', (OTHER_IN_GUIDE,)),)))
        self.assertEqual(both, whole)
        self.assertEqual(both, ['BBC World News', 'US| ESPN2 HD'])

    def test_the_identity_survives_a_channel_in_neither_half(self):
        """Same identity re-checked against a corpus where the halves are disjoint, so it
        cannot be passing because one half happens to contain the other."""
        self.espn2.in_guide = False
        self.deportes.in_guide = True
        db.session.commit()
        self.assertEqual(self.half(OTHER_GUIDE_OWN_ROW), ['ESPN Deportes'])
        self.assertEqual(self.half(OTHER_GUIDE_VIA_GROUP),
                         ['BBC World News', 'US| ESPN2 HD'])
        both = self.names(self.no_standing(filters=(
            DimensionFilter('other', (OTHER_GUIDE_OWN_ROW, OTHER_GUIDE_VIA_GROUP)),)))
        self.assertEqual(both, self.names(self.no_standing(
            filters=(DimensionFilter('other', (OTHER_IN_GUIDE,)),))))

    def test_each_half_facet_count_matches_its_own_filter(self):
        """Same rule the whole already carries: the rail's number and the list it opens are
        one question asked twice."""
        facets = search(SearchState(standing=only_hiding(), facets=('other',)),
                        self.context()).facets['other']
        self.assertEqual(facets[OTHER_GUIDE_OWN_ROW], len(self.half(OTHER_GUIDE_OWN_ROW)))
        self.assertEqual(facets[OTHER_GUIDE_VIA_GROUP],
                         len(self.half(OTHER_GUIDE_VIA_GROUP)))


class MonitoredFilterTests(_EngineTestCase):
    """`f.other=monitored` - is anything re-checking this channel on a schedule RIGHT NOW.

    Coverage, not history. The `showuntested` standing option already answers "was this ever
    tested", which says nothing about whether anything will look at it again; excluding this
    value is how a user finds the channels their monitoring misses (dev/changelog/1065).

    Every assertion here is ultimately the same one: the filter must return exactly what
    `channel_tester.monitored_channel_ids()` returns. That function is the definition - it
    ranks members in Python to decide which one a scheduleless group's automatic check
    probes - and a second answer spelled in SQL would show up as a channel this page calls
    unmonitored while its group page says it is being checked.

    The seeded corpus supplies the shape: the in-guide `Fox` group holds US| ESPN2 HD and
    BBC World News, and nothing carries a schedule until a test adds one.
    """

    def _schedule(self, group, recurring=True, paused=False, status='SCHEDULED'):
        """An active recurring check on `group`. All three conditions matter -
        `channel_groups.active_recurring_jobs()` counts a job only when it is recurring,
        SCHEDULED and not paused."""
        job = seed.set_check(group, name=f'{group.name} check', status=status,
                             recurring=recurring, recur_paused=paused, recur_day=0,
                             recur_hour=3, recur_minute=0)
        db.session.commit()
        return job

    def monitored(self):
        return self.names(self.no_standing(
            filters=(DimensionFilter('other', (OTHER_MONITORED,)),)))

    def unmonitored(self):
        return self.names(self.no_standing(
            filters=(DimensionFilter('other', ex=(OTHER_MONITORED,)),)))

    def expected(self):
        """What the definition says, as names - the thing the filter has to reproduce."""
        from app.channel_tester import monitored_channel_ids
        ids = monitored_channel_ids()
        return sorted(c.name for c in Channel.query.filter(Channel.id.in_(ids)).all())

    def test_the_automatic_guide_check_is_coverage_on_its_own(self):
        """With no group schedule anywhere, the pinned TV Guide check still probes ONE
        member per guide row - so Fox's serving member is monitored and the other member is
        a genuine hole. Counting the whole group here would be the false-reassuring answer
        (dev/changelog/752)."""
        self.assertEqual(self.monitored(), ['US| ESPN2 HD'])
        self.assertEqual(self.monitored(), self.expected())
        self.assertIn('BBC World News', self.unmonitored())

    def test_empty_is_a_real_answer(self):
        """Pausing the automatic check leaves nothing monitored at all, and the filter says
        so both ways round rather than quietly matching everything."""
        system = OnDemandTestJob.query.filter_by(is_system=True).one()
        system.recur_paused = True
        db.session.commit()
        self.assertEqual(self.expected(), [])
        self.assertEqual(self.monitored(), [])
        self.assertEqual(self.unmonitored(), self.all_names())

    def test_a_groups_own_schedule_monitors_every_participating_member(self):
        """A group that carries its own schedule is checked member by member - that is the
        division of labor the automatic check's one-probe fallback exists against."""
        self._schedule(self.group)
        self.assertEqual(self.monitored(), ['BBC World News', 'US| ESPN2 HD'])
        self.assertEqual(self.monitored(), self.expected())

    def test_a_paused_or_one_shot_job_is_not_a_schedule_of_its_own(self):
        """"Active recurring" is read from active_recurring_jobs() rather than restated, so
        a paused job and a one-shot job both leave the group on the automatic check's single
        probe instead of promoting every member into coverage."""
        job = self._schedule(self.group, paused=True)
        self.assertEqual(self.monitored(), ['US| ESPN2 HD'])
        job.recur_paused = False
        job.recurring = False
        db.session.commit()
        self.assertEqual(self.monitored(), ['US| ESPN2 HD'])
        self.assertEqual(self.monitored(), self.expected())
        job.recurring = True
        db.session.commit()
        self.assertEqual(self.monitored(), ['BBC World News', 'US| ESPN2 HD'])

    def test_a_members_health_check_switch_off_takes_it_out_of_coverage(self):
        """The participation switch is what a run reads, so turning it off is a real hole -
        the channel keeps its membership and stops being checked."""
        self._schedule(self.group)
        member = next(m for m in self.group.memberships if m.channel_id == self.bbc.id)
        set_participation(member, 'test_enabled', False)
        db.session.commit()
        self.assertEqual(self.monitored(), ['US| ESPN2 HD'])
        self.assertEqual(self.monitored(), self.expected())
        self.assertIn('BBC World News', self.unmonitored())

    def test_the_channel_wide_off_switch_wins_over_the_membership(self):
        """Channel.test_enabled is an off switch and beats the membership's own (see
        DESIGN-channel-groups-model.md 4.2). check_run_channels resolves both, which is
        exactly why this filter reads it instead of joining the membership table itself."""
        self._schedule(self.group)
        self.espn2.test_enabled = False
        db.session.commit()
        self.assertEqual(self.monitored(), ['BBC World News'])
        self.assertEqual(self.monitored(), self.expected())

    def test_the_two_sides_partition_the_corpus(self):
        """Include and exclude are complements over the same set. If they ever stop adding
        back up to the whole, one of them has grown a second definition."""
        self._schedule(self.group)
        self.assertEqual(sorted(self.monitored() + self.unmonitored()), self.all_names())
        self.assertEqual(set(self.monitored()) & set(self.unmonitored()), set())

    def test_the_facet_count_matches_the_filter(self):
        """The rail's number and the list it opens are one question asked twice."""
        self._schedule(self.group)
        facets = search(SearchState(standing=only_hiding(), facets=('other',)),
                        self.context()).facets['other']
        self.assertEqual(facets[OTHER_MONITORED], len(self.monitored()))
        self.assertEqual(facets[OTHER_MONITORED], 2)

    def test_a_group_row_answers_for_its_members(self):
        """A group is a row on this grain, and it is monitored when any member is. Answering
        `false` for groups the way the provider-shaped values do would drop exactly the
        scheduleless groups whose one probed member is the fallback's whole point."""
        self._schedule(self.group)
        self.assertEqual(self.group_names(self.no_standing(
            filters=(DimensionFilter('other', (OTHER_MONITORED,)),))), ['Fox'])
        self.assertEqual(self.group_names(self.no_standing(
            filters=(DimensionFilter('other', ex=(OTHER_MONITORED,)),))), [])

    def test_the_id_set_is_resolved_once_per_request(self):
        """The set costs ~60ms against the production database, and the filter, the standing
        breakdown and every facet aggregate all ask for it - so the memo on SearchContext is
        load-bearing rather than tidiness (CLAUDE.md: no hidden I/O in per-row loops)."""
        self._schedule(self.group)
        ctx = self.context()
        calls = []
        real = channel_tester.monitored_channel_ids

        def counting():
            calls.append(1)
            return real()

        with mock.patch.object(channel_tester, 'monitored_channel_ids', counting):
            search(SearchState(standing=only_hiding(), facets=('other',),
                               filters=(DimensionFilter('other', (OTHER_MONITORED,)),)), ctx)
        self.assertEqual(sum(calls), 1)

    def test_a_request_that_never_asks_never_pays(self):
        """The memo is lazy on purpose: a search that neither filters on this value nor
        draws the Other rail must not spend the query budget resolving it."""
        self._schedule(self.group)
        ctx = self.context()
        with mock.patch.object(channel_tester, 'monitored_channel_ids',
                               side_effect=AssertionError('resolved without being asked')):
            search(SearchState(standing=only_hiding(), facets=()), ctx)
        self.assertIsNone(ctx.monitored_ids)


class StandingOptionTests(_EngineTestCase):
    """Preferences that survive every search - and are counted, never silently applied."""

    def test_hide_duplicates_keeps_exactly_one_per_cluster(self):
        state = SearchState(standing=only_hiding('showdup'), facets=())
        kept = self.names(state)
        self.assertIn('US| ESPN2 HD', kept)
        self.assertNotIn('Sky Sports Action', kept)
        self.assertNotIn('Sky Sports Action HD', kept)

    def test_the_keep_rule_is_a_cascade_not_four_separate_rules(self):
        """In guide, then in a channel group, then health, then lowest id - each rung only
        gets a say when the one above it ties. dup_high has the best health by far and
        still loses twice over, which is the whole point of a cascade.

        The group rung was added by dev/changelog/759: with the auto-hide gone, the copy
        the user deliberately put in a group is the one worth keeping, and before it that
        copy could be hidden in favour of an untouched one."""
        state = SearchState(standing=only_hiding('showdup'), facets=())
        self.assertIn('US| ESPN2 HD', self.names(state))

        self.espn2.in_guide = False
        db.session.commit()
        self.assertIn('US| ESPN2 HD', self.names(state))          # group rung decides

        # Every membership goes, or health is never consulted for this cluster.
        ChannelGroupMember.query.filter_by(channel_id=self.espn2.id).delete()
        db.session.commit()
        self.assertIn('Sky Sports Action', self.names(state))     # health rung decides

        self.dup_high.health_score = self.espn2.health_score = self.dup_low.health_score
        db.session.commit()
        survivor = min((self.espn2, self.dup_high, self.dup_low), key=lambda c: c.id)
        self.assertIn(survivor.name, self.names(state))           # id rung decides

    def test_which_copy_survives_does_not_depend_on_what_was_typed(self):
        """The cascade is computed over the whole channels table on purpose. Scoped to the
        filtered set instead, searching for the loser's name would promote it to survivor
        and the KEPT badge would move around as the user types."""
        self.build_indexes()
        state = SearchState(q='Sky Sports Action HD', standing=only_hiding('showdup'), facets=())
        self.assertEqual(self.names(state), [])

    def test_kept_ids_only_mean_something_while_hide_duplicates_is_on(self):
        with_dup = self.run_search(SearchState(standing=only_hiding('showdup'), facets=()))
        self.assertEqual(with_dup.kept_ids, frozenset({self.espn2.id}))
        without = self.run_search(SearchState(standing=only_hiding(), facets=()))
        self.assertEqual(without.kept_ids, frozenset())

    def test_hide_unnormalizable_urls_only_applies_to_normalizing_accounts(self):
        """With normalization disabled on an account, "normalization left this URL alone"
        describes every one of its channels rather than a property worth hiding."""
        kept = self.names(SearchState(standing=only_hiding('shownotnorm'), facets=()))
        self.assertNotIn('A & E', kept)        # account Alpha normalizes, so this is hidden
        self.assertIn('FUTBOL Total', kept)    # account Beta does not, so this is not

    def test_hide_channels_with_no_epg_data(self):
        kept = self.names(SearchState(standing=only_hiding('shownoepg'), facets=()))
        self.assertEqual(kept, ['BBC World News', 'Discovery Channel', 'ESPN Deportes'])

    def test_hide_channels_with_no_epg_data_also_works_on_the_airings_grain(self):
        """The airings grain's base query already joins EPGEntry and Channel together, so
        unrestricted auto-correlation on `noepg`'s subquery struck both tables and left it
        with no FROM clause of its own - SQLAlchemy raised InvalidRequestError instead of
        running the search. dev/docs/BUGS.md 2026-08-11."""
        state = SearchState(grain=GRAIN_AIRINGS, standing=only_hiding('shownoepg'), facets=())
        titles = sorted(r.title for r in self.run_search(state).rows)
        self.assertEqual(titles, ['Nightly News', 'Premier League Live', 'Wembley Cup Final'])

    def test_hide_never_tested_channels(self):
        kept = self.names(SearchState(standing=only_hiding('showuntested'), facets=()))
        self.assertNotIn('BBC World News', kept)

    def test_the_breakdown_counts_what_each_option_took_out_and_adds_up(self):
        """"412 duplicates hidden" has to mean 412 rows this search would give back by
        turning the option off - a number the user cannot act on is worse than no number."""
        state = SearchState(standing=only_hiding('showdup', 'showuntested'), facets=())
        result = self.run_search(state)
        everything = self.run_search(SearchState(standing=only_hiding(), facets=()))
        self.assertEqual(result.standing_hidden, {'showdup': 2, 'showuntested': 1})
        self.assertEqual(result.total + sum(result.standing_hidden.values()),
                         everything.total)

    def test_a_row_hidden_twice_is_attributed_once(self):
        """The attribution CASE is ordered, so a row two options both hide is counted
        against the first in registry order. Counting it twice would make the numbers stop
        adding up to the total, which is worse than a slightly arbitrary attribution."""
        self.dup_low.health_score = None          # now hidden by dup AND by untested
        db.session.commit()
        result = self.run_search(SearchState(standing=only_hiding('showdup', 'showuntested'),
                                             facets=()))
        everything = self.run_search(SearchState(standing=only_hiding(), facets=()))
        self.assertEqual(sum(result.standing_hidden.values()) + result.total,
                         everything.total)
        self.assertEqual(result.standing_hidden.get('showdup'), 2)

    def test_the_breakdown_is_scoped_to_the_current_search(self):
        self.build_indexes()
        result = self.run_search(SearchState(q='Sky', standing=only_hiding('showdup'),
                                             facets=()))
        self.assertEqual(result.standing_hidden, {'showdup': 2})
        self.assertEqual(result.total, 0)

    def test_no_active_options_reports_nothing_hidden(self):
        result = self.run_search(SearchState(standing=only_hiding(), facets=()))
        self.assertEqual(result.standing_hidden, {})
        self.assertEqual(result.channel_total, Channel.query.count())

    def test_hidden_dup_and_notnorm_are_what_hide_by_default(self):
        """A hider that is on by default is only legal because the count line names it, so
        which ones hide by default is part of the contract, not a preference.

        `hidden` joined them with the hide feature (dev/changelog/775): hiding a channel
        means it is not offered anywhere, so every surface built on this engine inherits the
        default and only the channel search itself can turn it off.

        Asserted twice on purpose since the inversion (dev/changelog/778). The BEHAVIOR is
        the contract - these three remove rows from a link that says nothing about standing
        options - and `DEFAULT_STANDING` is now the near-inverse of it, because a default-on
        hider is spelled as a `show*` key that is absent. Pinning only the set would let a
        future edit flip `hides_when_on` and keep this green while the page changed."""
        self.assertEqual(
            {s.key for s in STANDING_OPTIONS if standing_applied(DEFAULT_STANDING, s.key)
             and not s.grain},
            {'showhidden', 'showdup', 'shownotnorm'})
        self.assertEqual(DEFAULT_STANDING,
                         frozenset({'shownoepg', 'showuntested', 'showmembers'}))


class FacetCountTests(_EngineTestCase):
    """The rail's counts: what they include, and what they must not."""

    def state(self, **kw):
        """Standing options off (they hide seeded rows and this is not what these test), but
        `facets` left at its default so every dimension is counted."""
        return SearchState(standing=only_hiding(), **kw)

    def facets(self, **kw):
        return self.run_search(self.state(**kw)).facets

    def test_a_dimension_is_counted_with_its_own_filter_removed(self):
        """Picking Category = Sports must not drop every other category's count to zero, or
        the facet becomes unusable the moment it is used."""
        cats = self.facets(filters=(DimensionFilter('cat', ('Sports',)),))['cat']
        self.assertEqual(cats.get('News'), 1)
        self.assertEqual(cats.get('Sports'), 5)

    def test_another_dimensions_filter_still_narrows_the_count(self):
        """Only the dimension's own filter is removed. Everything else still applies, or the
        rail would promise rows the list then refuses to show."""
        acct = self.facets(filters=(DimensionFilter('acct', (str(self.acct_b.id),)),))
        self.assertEqual(acct['cat'], {'Sports': 1})

    def test_standing_options_gate_the_counts(self):
        """A count that ignored the standing options would offer rows the list is hiding."""
        self.assertEqual(self.facets()['cat']['Sports'], 5)
        with_dup = self.run_search(SearchState(standing=only_hiding('showdup'))).facets['cat']
        self.assertEqual(with_dup['Sports'], 3)

    def test_fixed_vocabulary_dimensions_are_seeded_at_zero(self):
        """The rail's three-state control has to render a value the user can still exclude,
        so a band nothing currently falls in is a real answer rather than an omission."""
        empty = self.facets(q='zzz-nothing')
        self.assertEqual(sorted(empty['health']), sorted(HEALTH_VALUES))
        self.assertEqual(set(empty['health'].values()), {0})
        self.assertEqual(empty['other'],
                         {OTHER_REMOVED: 0, OTHER_NEW: 0, OTHER_IN_GUIDE: 0,
                          OTHER_GUIDE_OWN_ROW: 0, OTHER_GUIDE_VIA_GROUP: 0, OTHER_DUP_URL: 0,
                          OTHER_MONITORED: 0})
        # Spelled out rather than built from OTHER_VALUES on purpose: this is the assertion
        # that a value added to the registry reaches the rail, so deriving it from the
        # registry would make it agree with itself and check nothing.
        self.assertEqual(sorted(empty['other']), sorted(OTHER_VALUES))

    def test_the_group_facet_counts_membership_and_any(self):
        groups = self.facets()['group']
        self.assertEqual(groups['Fox'], 2)
        self.assertEqual(groups[GROUP_ANY], 2)

    def test_the_tag_facet_counts_the_airing_half_too(self):
        self.build_indexes()
        tags = self.facets()['tag']
        self.assertEqual(tags['espn'], 2)
        self.assertEqual(tags['nascar'], 1)
        self.assertEqual(tags['unfinished'], 0)

    def test_the_combined_scan_and_the_per_dimension_fallback_agree(self):
        """Four dimensions ride one grouped scan while the user has not filtered on them,
        and fall back to an aggregate each the moment they have. Two code paths, one answer.

        Each case below filters on exactly one of the four, which is what moves that
        dimension onto the fallback - and since a dimension is counted with its own filter
        removed, its count must come back identical to the unfiltered one.
        """
        plain = self.facets()
        for key, filt in (('cat', DimensionFilter('cat', (), ('Sports',))),
                          ('acct', DimensionFilter('acct', (str(self.acct.id),))),
                          ('health', DimensionFilter('health', ('good',))),
                          ('other', DimensionFilter('other', (OTHER_IN_GUIDE,)))):
            with self.subTest(dimension=key):
                self.assertEqual(self.facets(filters=(filt,))[key], plain[key])

    def test_facets_names_which_dimensions_to_count(self):
        """The lever the endpoint uses to serve rows fast while the rail catches up. A
        dimension absent from the result means "not requested" - never zero."""
        self.assertEqual(list(self.facets(facets=('cat',))), ['cat'])
        self.assertEqual(self.facets(facets=()), {})

    def test_the_default_counts_every_visible_dimension_in_rail_order(self):
        """The tuple IS the order - nothing downstream sorts it."""
        self.assertEqual(list(self.facets()),
                         ['tag', 'acct', 'health', 'group', 'cat', 'other'])

    def test_the_counts_follow_the_typed_query(self):
        self.build_indexes()
        self.assertEqual(self.facets(q='espn')['cat'], {'Sports': 2})


class PagingAndSortTests(_EngineTestCase):
    """Server-side paging, and the tiebreak that keeps pages disjoint."""

    def test_totals_and_page_arithmetic(self):
        result = self.run_search(SearchState(standing=only_hiding(), facets=(), page_size=4))
        # `channel_total` is the channels; `total` is what the PAGER pages, which since
        # dev/changelog/811 is the channels plus this corpus's group rows.
        self.assertEqual(result.channel_total, Channel.query.count())
        self.assertEqual(result.total, result.channel_total + result.group_total)
        self.assertEqual(result.pages, (result.total + 3) // 4)
        self.assertEqual(len(result.rows), 4)
        self.assertEqual(result.page, 1)

    def test_pages_are_disjoint_when_the_sort_key_is_duplicated(self):
        """Every seeded channel gets the same category AND the same name, so the default
        sort's two expressions tie on every row and `Channel.id` is the only thing left
        deciding the order. Without it SQLite is free to return page 2 in an order
        inconsistent with page 1, and rows appear to duplicate or vanish as the user pages.
        """
        for ch in Channel.query.all():
            ch.category_name = 'Same'
            ch.name = 'Same'
        db.session.commit()
        seen = []
        for page in (1, 2, 3, 4):
            result = self.run_search(SearchState(standing=only_hiding(), facets=(),
                                                 page=page, page_size=3))
            seen.extend(r.id for r in result.rows if isinstance(r, Channel))
        self.assertEqual(len(seen), len(set(seen)))
        self.assertEqual(sorted(seen), sorted(c.id for c in Channel.query.all()))

    def test_the_row_query_orders_by_the_id_tiebreak(self):
        """The behavioral test above cannot prove this one, and that is worth saying out
        loud: at nine rows SQLite hands back the table in rowid order whether or not the
        tiebreak is there, so removing it changes nothing observable *here* while changing
        plenty at 136,130 rows paged with LIMIT/OFFSET off an index. The guarantee is only
        visible in the statement, so this asserts on the statement - the one place in this
        file that does. (`IOCounter.statements` exists for exactly this kind of shape check.)
        """
        with IOCounter(all_engines()) as counter:
            self.run_search(SearchState(standing=only_hiding(), facets=(), page_size=3))
        paged = [s for s in counter.statements if 'ORDER BY' in s and 'LIMIT' in s]
        self.assertTrue(paged, f'no paged row query was issued: {counter.statements}')
        for statement in paged:
            self.assertRegex(statement.split('ORDER BY')[-1], r'channels\.id')

    def test_sorting_by_name_ascending_and_descending(self):
        state = SearchState(standing=only_hiding(), facets=(), sort='name')
        asc = [r.name for r in self.run_search(state).rows]
        desc = [r.name for r in self.run_search(
            SearchState(standing=only_hiding(), facets=(), sort='name',
                        sort_desc=True)).rows]
        self.assertEqual(asc, sorted(asc, key=str.lower))
        self.assertEqual(desc, list(reversed(asc)))

    def test_sorting_by_health_puts_untested_where_sqlite_puts_null(self):
        rows = self.run_search(SearchState(standing=only_hiding(), facets=(),
                                           sort='health')).rows
        self.assertIsNone(rows[0].health_score)

    def test_a_page_past_the_end_is_empty_not_an_error(self):
        result = self.run_search(SearchState(standing=only_hiding(), facets=(), page=99))
        self.assertEqual(result.rows, [])
        self.assertGreater(result.total, 0)


if __name__ == '__main__':
    unittest.main(verbosity=2)
