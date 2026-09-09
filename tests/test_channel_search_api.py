"""The channel search's JSON endpoints: the contract phase B builds its page against.

`tests/test_channel_search.py` pins what the engine ANSWERS; this pins what the endpoint
SAYS - which rows come back, what each row carries, and what an unanswerable request does.
The split matters because the two fail differently: an engine defect is a wrong result set,
while an endpoint defect is a right result set described wrongly, and a page rendered from a
wrong description looks fine until someone checks a number against the database.

The defects these guard against, each of which is invisible on screen:

* a payload that silently drops one of the ten approved columns, so a picker entry renders
  blank for everyone and nobody can tell whether the data or the column is at fault
* an unanswerable state (unknown field, sort, dimension, grain) answered with a DIFFERENT
  search instead of a 400 - the registry exists precisely so a typo in a link is loud
* `facets=` - "count nothing, just give me rows" - going back to counting everything, which
  is the difference between a 46ms keystroke and a 667ms one on the production database
* an enrichment done per row rather than per page. That one is guarded by query COUNT in
  tests/test_scaling_pages.py::test_channel_search_api rather than here, because the count
  is what makes it deterministic; here we only pin that the enrichments are correct.
* the DUP/KEPT badge disagreeing with the engine's own keep-rule, so the row the list keeps
  and the row the badge calls kept are different channels

Every row assertion is made against a corpus shaped like the real data - a duplicate cluster
whose winner is decided by a different rung than health alone, a channel whose only match is
in a program description, a provider-removed channel, and a tag that matches by pattern.
"""
import os
import sys
import unittest
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tests.support.app import make_test_app  # noqa: E402
from tests.support import seed  # noqa: E402
from tests.support.search import show_all_query, unfolded_query  # noqa: E402
from app import db  # noqa: E402
from app.accounts import NORM_DISABLED, NORM_MPEGTS  # noqa: E402
from app.channel_search import (DEFAULT_FIELDS, DIMENSIONS, FIELDS, GRAIN_CHANNELS,  # noqa: E402
                                SORTS, STANDING_OPTIONS, VISIBLE_DIMENSIONS,
                                visible_dimensions_for)
from app.channel_search_rows import (KEEP_REASON_HEALTH, KEEP_REASON_ID,  # noqa: E402
                                     KEEP_REASON_IN_GROUP, KEEP_REASON_IN_GUIDE)
from app.database import (Channel, ChannelGroupMember, EPGEntry, Tag,  # noqa: E402
                          TagPattern)
from app.search_index import rebuild_search_indexes  # noqa: E402

#: The query fragment that leaves every standing option NOT removing rows - what a bare
#: `standing=` used to mean before the inversion (dev/changelog/778).
SHOW_ALL = show_all_query()

SEARCH_URL = '/api/channels/search'
COUNTS_URL = '/api/channels/search/counts'
CATALOG_URL = '/api/channels/search/catalog'


def _with_members(query: str) -> str:
    """`query` with the group fold turned off, unless it already names a standing set.

    Two of this module's channels are in a group, and since dev/changelog/811 the default
    search folds a grouped channel into its group's row. That is the right default and
    `GroupRowTests` pins it - but every other assertion here is about a CHANNEL's own
    payload and wants the members in the list. A query that spells its own standing
    (`SHOW_ALL`, or one option by name) is left exactly as it asked.
    """
    if 'standing=' in query:
        return query
    return '&'.join(x for x in (query, unfolded_query()) if x)


class _ApiTestCase(unittest.TestCase):

    def setUp(self):
        self.t = make_test_app()
        self.ctx_mgr = self.t.app.app_context()
        self.ctx_mgr.push()
        self._seed()

    def tearDown(self):
        self.ctx_mgr.pop()
        self.t.cleanup()

    def _seed(self):
        now = datetime.utcnow()
        self.acct = seed.make_account(name='Alpha', url_normalization=NORM_MPEGTS,
                                      last_sync_at=now)
        self.acct_b = seed.make_account(name='Beta', url_normalization=NORM_DISABLED,
                                        last_sync_at=now)

        self.espn2 = self._channel('US| ESPN2 HD', category_name='Sports', health_score=90.0,
                                   in_guide=True)
        self.discovery = self._channel('Discovery Channel', category_name='Docs',
                                       health_score=40.0)
        self.untested = self._channel('Test Card', category_name='Docs')
        # Normalization is ON for this account and this URL has nothing to rebuild from, so
        # the row carries the "Not normalized" badge. Standing option `notnorm` hides it by
        # default, which is why the badge assertion turns that option off.
        self.raw = self._channel('Radio Mount', category_name='Radio', health_score=70.0,
                                 url_normalizable=False)
        # The duplicate cluster: espn2 is in_guide, so it wins the FIRST rung even though
        # dup_high has the better score - which is what makes the cascade visible rather
        # than looking like a plain max(health).
        self.dup_high = self._channel('Sky Sports Action', category_name='Sports',
                                      health_score=99.0)
        self.dup_low = self._channel('Sky Sports Action HD', category_name='Sports',
                                     health_score=10.0)
        for ch in (self.espn2, self.dup_high, self.dup_low):
            ch.stream_url = 'http://example.test/live/user1/pass1/77'
            ch.is_duplicate_stream_url = True

        self.removed = self._channel('Gone Fishing TV', category_name='Docs',
                                     health_score=55.0,
                                     last_seen_at=now - timedelta(days=30))

        # On air right now, so the "now airing" column has something to carry; the search
        # below matches on its DESCRIPTION, which is the case the why-chip exists for.
        self.airing = EPGEntry(
            channel_id=self.discovery.id, title='Wembley Cup Final', sub_title='Semi Final',
            description='Liverpool play at Wembley',
            start_time=now - timedelta(minutes=10), stop_time=now + timedelta(minutes=50))
        db.session.add(self.airing)
        # espn2 airs something that ALSO matches its name, which is what makes "the name
        # wins" a real assertion rather than a vacuous one: without this, a name search
        # matches nothing else on the row and the chip is empty either way.
        db.session.add(EPGEntry(
            channel_id=self.espn2.id, title='ESPN Sunday Night', description='Highlights',
            start_time=now + timedelta(hours=2), stop_time=now + timedelta(hours=3)))

        tag = Tag(name='sports-tag')
        db.session.add(tag)
        db.session.flush()
        db.session.add(TagPattern(tag_id=tag.id, pattern='ESPN'))
        self.tag = tag

        self.group = seed.make_group(name='Fox', members=[self.espn2, self.discovery])
        # A second, health-check-only group over the same channel. M:N membership means a
        # channel really can be in both at once, so the row's groups list has to name both
        # rather than picking one (see test_groups_and_tags_come_back_per_row below).
        seed.make_group(name='Nightly checks', members=[self.espn2], in_guide=False,
                        recording=False)
        db.session.commit()

    def _channel(self, name, **kw):
        kw.setdefault('last_seen_at', datetime.utcnow())
        return seed.make_channel(self.acct, name=name, **kw)

    # -- helpers ----------------------------------------------------------

    def get(self, query='', expect=200):
        """A search, with the members of this corpus's groups left in the list.

        Two of these channels are in a group, and since dev/changelog/811 the default search
        folds a grouped channel into its group's row. That is the right default and
        `GroupRowTests` below pins it - but every assertion in the rest of this module is
        about a CHANNEL's own payload, so they ask for the members back. Injected here rather
        than at ~60 call sites, and only when the caller named no standing set of its own, so
        a test that spells its own standing (`SHOW_ALL`, or one option by name) still gets
        exactly what it asked for.
        """
        resp = self.t.client.get(SEARCH_URL + ('?' + _with_members(query)
                                               if _with_members(query) else ''))
        self.assertEqual(resp.status_code, expect,
                         f'{query!r} returned {resp.status_code}: {resp.get_json()}')
        return resp.get_json()

    def rows(self, query=''):
        return self.get(query)['rows']

    def channel_rows(self, query=''):
        """The channel rows alone. A page can also hold GROUP rows since
        dev/changelog/811, and a group carries a deliberately different payload - no
        account, no stream, no duplicate cluster - so an assertion about a channel column
        has to say which kind it means rather than reading every row."""
        return [r for r in self.rows(query) if r['kind'] == 'channel']

    def row_named(self, name, query=''):
        rows = self.rows(query)
        matches = [r for r in rows if r['name'] == name]
        self.assertEqual(len(matches), 1,
                         f'expected exactly one {name!r} row, got {[r["name"] for r in rows]}')
        return matches[0]


class EnvelopeTests(_ApiTestCase):
    """The response envelope and the error contract.

    A state this engine cannot honour must be a 400 naming the problem. Answering a
    DIFFERENT search instead is the silent-wrong-answer failure the registry exists to
    prevent - and it is exactly what a `getattr(SORTS, ...) or DEFAULT` style fallback would
    do.
    """

    def test_success_envelope_carries_every_key_the_page_renders_from(self):
        payload = self.get()
        for key in ('success', 'grain', 'rows', 'total', 'page', 'page_size', 'pages',
                    'standing_hidden', 'facets', 'facets_counted', 'degraded',
                    'query_string'):
            self.assertIn(key, payload, f'{key} missing from the search response')
        self.assertTrue(payload['success'])
        self.assertEqual(payload['grain'], 'channels')

    def test_unknown_sort_is_a_400_naming_what_is_sortable(self):
        resp = self.t.client.get(SEARCH_URL + '?sort=airing')
        self.assertEqual(resp.status_code, 400)
        self.assertIn('airing', resp.get_json()['error'])

    def test_unknown_field_dimension_and_grain_are_each_a_400(self):
        for query in ('in=nope', 'f.nope=1', 'x.nope=1', 'grain=nope', 'standing=nope',
                      'facets=nope', 'grain=airings&sort=name'):
            resp = self.t.client.get(SEARCH_URL + '?' + query)
            self.assertEqual(resp.status_code, 400, f'{query} should be rejected')
            self.assertIn('error', resp.get_json())

    def test_the_error_envelope_is_the_projects_own(self):
        """`{'error': msg}` with a 4xx - not `{'success': False}`, not a 200 with an empty
        list. jsonFetch and showConflictError both key off this shape."""
        payload = self.t.client.get(SEARCH_URL + '?sort=nope').get_json()
        self.assertEqual(list(payload), ['error'])

    def test_query_string_round_trips_the_state_it_understood(self):
        """What the page puts in the address bar and other surfaces link to. It must be the
        state the SERVER understood, not the one the client sent - a link built from the
        request as typed would carry a parameter the engine ignored."""
        payload = self.get('q=espn&sort=-health&f.cat=Sports&' + SHOW_ALL)
        self.assertIn('q=espn', payload['query_string'])
        self.assertIn('sort=-health', payload['query_string'])
        self.assertIn('f.cat=Sports', payload['query_string'])
        self.assertIn('standing=showdup', payload['query_string'])
        again = self.t.client.get(SEARCH_URL + '?' + payload['query_string']).get_json()
        self.assertEqual([r['id'] for r in again['rows']],
                         [r['id'] for r in payload['rows']])

    def test_a_duration_filter_round_trips_through_the_url_like_any_other_dimension(self):
        """`duration` (migration 36, dev/changelog/594) has no dedicated URL parsing - it
        goes through the same generic `f.<dim.key>=<value>` parser every dimension does, so
        this pins that registering it was enough."""
        resp = self.t.client.get(
            SEARCH_URL + '?grain=airings&f.duration=dur:30..')
        self.assertEqual(resp.status_code, 200)
        payload = resp.get_json()
        self.assertIn('f.duration=dur%3A30..', payload['query_string'])
        again = self.t.client.get(SEARCH_URL + '?' + payload['query_string']).get_json()
        self.assertEqual([r['id'] for r in again['rows']], [r['id'] for r in payload['rows']])

    def test_degraded_says_so_when_the_index_is_not_built(self):
        """A search that is correct but ten times slower is exactly the hidden behavior this
        project's founding principle says to surface."""
        self.assertTrue(self.get('q=espn')['degraded'])
        rebuild_search_indexes('test')
        self.assertEqual(self.get('q=espn')['degraded'], '')


class RowPayloadTests(_ApiTestCase):
    """Every field the approved mockup's ten columns render from, in one payload.

    All ten columns plus the DUP/KEPT badge data and the why-field ship batched together,
    not a minimal payload widened later (2026-07-30). A column whose data
    is missing renders blank, which reads as "this channel has no category" rather than as a
    bug - so each one is asserted by value here.
    """

    def test_every_approved_column_is_present_on_every_row(self):
        for row in self.channel_rows():
            for key in ('id', 'name', 'health', 'health_band', 'airing', 'lifecycle',
                        'lifecycle_date', 'not_normalized', 'account', 'category',
                        'stream_id', 'epg_channel_id', 'stream_url', 'groups', 'tags',
                        'dup', 'kept', 'why', 'in_guide', 'guide_via', 'notes'):
                self.assertIn(key, row, f'{key} missing from row {row["name"]!r}')

    def test_the_scalar_columns_carry_the_channels_own_values(self):
        row = self.row_named('Discovery Channel')
        self.assertEqual(row['category'], 'Docs')
        self.assertEqual(row['stream_id'], self.discovery.stream_id)
        self.assertEqual(row['epg_channel_id'], self.discovery.epg_channel_id)
        self.assertEqual(row['health'], 40.0)
        self.assertEqual(row['health_band'], 'poor')
        self.assertEqual(row['account'], {'id': self.acct.id, 'name': 'Alpha',
                                          'color': self.acct.color})

    def test_health_is_the_effective_score_and_never_tested_is_not_zero(self):
        """The badge's own number: observed plus the manual adjustment. None and 0 are
        different answers - one is "no observation", the other is "observed, terrible" - and
        a payload that sent 0 for both would band an untested channel as poor."""
        self.discovery.manual_health_adjustment = 45
        db.session.commit()
        self.assertEqual(self.row_named('Discovery Channel')['health'], 85.0)
        self.assertEqual(self.row_named('Discovery Channel')['health_band'], 'good')

        untested = self.row_named('Test Card')
        self.assertIsNone(untested['health'])
        self.assertEqual(untested['health_band'], 'untested')

    def test_the_stream_url_is_masked(self):
        """A stream URL routinely carries the account's credentials in its path and this one
        is bound for a JSON response any browser tab can read (DESIGN-secrets.md §4.2)."""
        row = self.row_named('US| ESPN2 HD')
        self.assertNotIn('user1', row['stream_url'])
        self.assertNotIn('pass1', row['stream_url'])
        self.assertIn('***', row['stream_url'])

    def test_now_airing_is_what_is_on_right_now(self):
        row = self.row_named('Discovery Channel')
        self.assertEqual(row['airing']['title'], 'Wembley Cup Final')
        self.assertEqual(row['airing']['sub_title'], 'Semi Final')
        self.assertIsNone(self.row_named('Test Card')['airing'])

    def test_a_program_that_has_finished_is_not_now_airing(self):
        """"Now" is a time question, so it comes off epg_entries and not off the deduped
        chan_prog projection, which has no per-airing times at all."""
        self.airing.start_time = datetime.utcnow() - timedelta(hours=3)
        self.airing.stop_time = datetime.utcnow() - timedelta(hours=2)
        db.session.commit()
        self.assertIsNone(self.row_named('Discovery Channel')['airing'])

    def test_groups_and_tags_come_back_per_row(self):
        row = self.row_named('US| ESPN2 HD')
        # Every group this channel belongs to, health-check-only ones included. The badge
        # used to hide them on the grounds that a check bag "is not a channel group", which
        # stopped being true when kind was deleted: there is one noun, and a group the
        # channel is in is a fact worth showing (dev/changelog/741).
        self.assertEqual(sorted(g['name'] for g in row['groups']),
                         ['Fox', 'Nightly checks'])
        self.assertEqual([t['name'] for t in row['tags']], ['sports-tag'])
        self.assertEqual(row['tags'][0]['color'], self.tag.color)
        self.assertEqual(self.row_named('Test Card')['tags'], [])

    def test_a_tag_is_carried_by_what_a_channel_airs_not_only_by_its_name(self):
        """The half of the tag rule that is easy to drop: a generically-named channel earns
        its tag from the program it is airing. Dropping it makes tags look right on the
        obvious rows and silently wrong on the rows they were invented for."""
        db.session.add(TagPattern(tag_id=self.tag.id, pattern='Wembley'))
        db.session.commit()
        self.assertEqual([t['name'] for t in self.row_named('Discovery Channel')['tags']],
                         ['sports-tag'])

    def test_the_lifecycle_state_and_its_badge_date(self):
        row = self.row_named('Gone Fishing TV', 'f.other=removed')
        self.assertEqual(row['lifecycle'], 'missing')
        self.assertEqual(row['lifecycle_date'], self.removed.last_seen_at.strftime('%Y-%m-%d'))
        self.assertIsNone(self.row_named('Discovery Channel')['lifecycle'])

    def test_not_normalized_is_only_true_where_normalization_is_actually_on(self):
        """"Normalization left this URL alone" describes every channel on an account with
        the mode disabled, so the badge would be noise there rather than a finding."""
        self.assertTrue(self.row_named('Radio Mount', SHOW_ALL)['not_normalized'])

        self.acct.url_normalization = NORM_DISABLED
        db.session.commit()
        self.assertFalse(self.row_named('Radio Mount', SHOW_ALL)['not_normalized'])


class GuideViaTests(_ApiTestCase):
    """`guide_via` - the third guide state, dev/changelog/759.

    `in_guide` means "has a guide row of its own" and nothing else since dev/changelog/751,
    but the "In your guide" filter matches the WIDER question that
    `channel_groups.guide_scope_channel_ids()` defines - own row OR member of a group that
    has one. Without this field the payload is narrower than the filter it is rendered
    under, so filtering by "In your guide" returns rows whose every control says
    "+ Add to Guide". These pin the two questions apart.
    """

    def test_a_grouped_channel_with_no_row_of_its_own_still_names_its_guide_group(self):
        """The case the whole field exists for: Discovery has in_guide False, and its
        listings are on screen anyway through the Fox group's row."""
        row = self.row_named('Discovery Channel')
        self.assertFalse(row['in_guide'])
        self.assertEqual([g['name'] for g in row['guide_via']], ['Fox'])

    def test_guide_via_lists_only_groups_that_are_in_the_guide(self):
        """espn2 is in Fox (in the guide) and Nightly checks (not). A group with no row of
        its own puts nothing on screen, so naming it here would invent a guide entry."""
        row = self.row_named('US| ESPN2 HD')
        self.assertEqual(sorted(g['name'] for g in row['groups']),
                         ['Fox', 'Nightly checks'])
        self.assertEqual([g['name'] for g in row['guide_via']], ['Fox'])

    def test_guide_via_is_empty_for_a_channel_in_no_in_guide_group(self):
        self.assertEqual(self.row_named('Test Card')['guide_via'], [])

    def test_the_two_facts_are_independent_and_can_both_be_true(self):
        """Joining a group stopped hiding a channel's own row (dev/changelog/751), so "own
        row" and "via a group" are two separate facts that can hold at once. A payload that
        made them exclusive would have to be wrong in one direction or the other."""
        row = self.row_named('US| ESPN2 HD')
        self.assertTrue(row['in_guide'])
        self.assertEqual([g['name'] for g in row['guide_via']], ['Fox'])

    def test_guide_via_follows_the_group_flag_rather_than_the_membership(self):
        self.group.in_guide = False
        db.session.commit()
        self.assertEqual(self.row_named('Discovery Channel')['guide_via'], [])

    def test_the_airing_grain_carries_it_on_the_channel_side(self):
        """Every channel-side value on an airing row is the channel grain's own, so a field
        added to one grain and not the other is the two grains disagreeing about a channel."""
        resp = self.t.client.get(SEARCH_URL + '?grain=airings&q=wembley')
        rows = resp.get_json()['rows']
        self.assertTrue(rows, 'expected at least one airing row')
        self.assertEqual([g['name'] for g in rows[0]['channel']['guide_via']], ['Fox'])
        self.assertFalse(rows[0]['channel']['in_guide'])

    def test_the_in_your_guide_filter_and_guide_via_agree(self):
        """The defect this closes, stated as an invariant: every row the guide-scope filter
        returns can say WHY it is in the guide - its own row, a group's, or both. A row
        matching the filter with neither is the confusing mix item #10 names."""
        rows = self.rows(SHOW_ALL + '&f.other=guide')
        self.assertTrue(rows)
        for row in rows:
            self.assertTrue(row['in_guide'] or row['guide_via'],
                            f'{row["name"]!r} matched the guide filter but explains nothing')


class DuplicateBadgeTests(_ApiTestCase):
    """The DUP and KEPT badges, which are the one place the payload restates a decision the
    engine already made. If the two disagree, the list keeps one channel and the badge calls
    a different one kept - and both look plausible on screen.
    """

    def test_the_dup_badge_carries_the_cluster_size_and_the_other_members(self):
        row = self.row_named('US| ESPN2 HD')
        self.assertEqual(row['dup']['count'], 3)
        self.assertEqual(sorted(o['name'] for o in row['dup']['others']),
                         ['Sky Sports Action', 'Sky Sports Action HD'])
        self.assertEqual(row['dup']['others_hidden'], 0)

    def test_the_cluster_carries_every_member_id_for_the_drill_in(self):
        """The badge drills in with `f.chan=` over the whole cluster, and it cannot drill in
        by searching the stream URL instead: the payload MASKS stream URLs, so the text on
        screen is not the text in the database. `others` is the tooltip's list and is capped
        at DUP_TOOLTIP_MEMBERS, so `ids` is what the drill-in reads and it must be complete
        and must include the row's own id."""
        row = self.row_named('US| ESPN2 HD')
        self.assertEqual(len(row['dup']['ids']), row['dup']['count'])
        self.assertIn(row['id'], row['dup']['ids'])
        self.assertEqual(sorted(row['dup']['ids']),
                         sorted([self.espn2.id, self.dup_high.id, self.dup_low.id]))

    def test_a_channel_with_no_duplicate_carries_no_cluster(self):
        self.assertIsNone(self.row_named('Discovery Channel')['dup'])

    def test_kept_names_the_rung_that_actually_decided_it(self):
        """A cascade, not four exclusive rules: in your guide, then in a channel group, then
        the best health, then the lowest id. espn2 wins on the FIRST rung despite dup_high
        scoring higher, so a payload that reported "the best health score" here would be
        describing a rule the engine does not run."""
        row = self.row_named('US| ESPN2 HD')
        self.assertTrue(row['kept'])
        self.assertEqual(row['dup']['kept_id'], self.espn2.id)
        self.assertEqual(row['dup']['kept_reason'], KEEP_REASON_IN_GUIDE)

    def test_the_group_rung_beats_a_better_health_score(self):
        """dev/changelog/759. With no copy holding a guide row, the one the user has
        deliberately put in a group wins - even against a 99 to espn2's 90. Without this
        rung the search hides the copy the user curated and shows an untouched one, which
        is the whole reason the rung was added."""
        self.espn2.in_guide = False
        db.session.commit()
        row = self.row_named('Sky Sports Action', SHOW_ALL)
        self.assertEqual(row['dup']['kept_id'], self.espn2.id)
        self.assertEqual(row['dup']['kept_reason'], KEEP_REASON_IN_GROUP)

    def test_the_group_rung_counts_a_group_that_is_not_in_the_guide(self):
        """ANY group, deliberately. A health-check-only group is still a channel the user
        curated and is monitoring, so it must not lose to an untouched copy."""
        self.espn2.in_guide = False
        ChannelGroupMember.query.filter_by(channel_id=self.espn2.id).delete()
        # dup_low is the WORST-scoring copy (10 vs 99), so only the group rung can explain
        # it winning - health and id both point elsewhere.
        seed.make_group(name='Watchlist', members=[self.dup_low], in_guide=False,
                        recording=False)
        db.session.commit()
        row = self.row_named('Sky Sports Action', SHOW_ALL)
        self.assertEqual(row['dup']['kept_id'], self.dup_low.id)
        self.assertEqual(row['dup']['kept_reason'], KEEP_REASON_IN_GROUP)

    def test_the_keep_reason_falls_through_to_health_then_to_id(self):
        self.espn2.in_guide = False
        # Every membership goes, or the group rung decides this cluster before health is
        # ever consulted and the fall-through below is never reached.
        ChannelGroupMember.query.filter_by(channel_id=self.espn2.id).delete()
        db.session.commit()
        row = self.row_named('Sky Sports Action', SHOW_ALL)
        self.assertEqual(row['dup']['kept_id'], self.dup_high.id)
        self.assertEqual(row['dup']['kept_reason'], KEEP_REASON_HEALTH)

        for ch in (self.espn2, self.dup_high, self.dup_low):
            ch.health_score = 50.0
        db.session.commit()
        row = self.row_named('Sky Sports Action', SHOW_ALL)
        self.assertEqual(row['dup']['kept_id'], min(self.espn2.id, self.dup_high.id,
                                                    self.dup_low.id))
        self.assertEqual(row['dup']['kept_reason'], KEEP_REASON_ID)

    def test_the_hidden_rows_follow_the_group_rung_too(self):
        """The badge explains, the SQL hides - two spellings of one cascade
        (`_keep_rank` and `_duplicate_losers`). A rung added to one and not the other is a
        badge naming a channel the list did not actually keep."""
        self.espn2.in_guide = False
        db.session.commit()
        names = [r['name'] for r in self.rows()]
        survivors = [n for n in names
                     if n in ('US| ESPN2 HD', 'Sky Sports Action', 'Sky Sports Action HD')]
        self.assertEqual(survivors, ['US| ESPN2 HD'])
        self.assertTrue(self.row_named('US| ESPN2 HD')['kept'])

    def test_the_kept_row_is_the_row_the_engine_actually_keeps(self):
        """The badge and the filter have to name the same channel. With duplicates hidden,
        exactly one of the cluster survives, and it must be the one flagged kept."""
        names = [r['name'] for r in self.rows()]
        survivors = [n for n in names
                     if n in ('US| ESPN2 HD', 'Sky Sports Action', 'Sky Sports Action HD')]
        self.assertEqual(survivors, ['US| ESPN2 HD'])
        self.assertTrue(self.row_named('US| ESPN2 HD')['kept'])

    def test_nothing_is_kept_when_hide_duplicates_is_off(self):
        """With every copy shown, none of them is "the one kept" - the badge would be
        claiming a decision that was not made."""
        rows = [r for r in self.channel_rows(SHOW_ALL) if r['dup']]
        self.assertTrue(rows)
        self.assertFalse(any(r['kept'] for r in rows))


class WhyChipTests(_ApiTestCase):
    """Which field earned the row.

    The chip exists for a surprise - "this matched something it is AIRING" - so a row whose
    NAME matched must carry no chip at all, or every row on a name search sprouts one and the
    signal is gone.
    """

    def test_a_name_match_needs_no_explanation(self):
        """espn2's name AND the program it airs both match, so this is the case where the
        chip has something it COULD say and must not: the name is the obvious reason the row
        is there, and explaining it would put a chip on every row of a name search."""
        self.assertIsNone(self.row_named('US| ESPN2 HD', 'q=espn')['why'])

    def test_a_program_match_names_the_field_and_the_program(self):
        # The scope is spelled out: this grain's default is the channel's own name
        # (dev/changelog/860), so the program field has to be asked for.
        row = self.row_named('Discovery Channel', 'q=wembley&in=name&in=epg-title')
        self.assertEqual(row['why']['field'], 'epg-title')
        self.assertEqual(row['why']['source'], 'program')
        self.assertEqual(row['why']['program']['title'], 'Wembley Cup Final')

    def test_a_description_only_match_says_description(self):
        """The description scope ships working and indexed (dev/changelog/395), so a row
        matched only there has to be able to say so - it is the least guessable of the ten
        reasons a row is in the list."""
        row = self.row_named('Discovery Channel', 'q=liverpool&in=name&in=epg-desc')
        self.assertEqual(row['why']['field'], 'epg-desc')
        self.assertEqual(row['why']['program']['description'],
                         'Liverpool play at Wembley')

    def test_the_chip_survives_the_unindexed_path(self):
        """Same answer with the index built and never built. The engine has three
        interchangeable paths to the same result set and the chip is drawn from the same
        predicates, so a chip that only works on one of them is a silent gap on exactly the
        searches that are already degraded."""
        query = 'q=wembley&in=name&in=epg-title'
        before = self.row_named('Discovery Channel', query)['why']
        rebuild_search_indexes('test')
        after = self.row_named('Discovery Channel', query)['why']
        self.assertEqual(before['field'], after['field'])
        self.assertEqual(before['program']['title'], after['program']['title'])

    def test_no_field_earned_a_row_that_two_fields_matched_between_them(self):
        """Under match-all a row can be in the list because one field carried one word and
        another carried the other - and then NEITHER earned it. "Cup" is only in the title
        and "Liverpool" only in the description, so the row matches and no single field
        does. Pointing at the title here would be naming a field that matched half the
        query."""
        query = 'q=cup+liverpool&in=epg-title&in=epg-desc'
        self.assertEqual([r['name'] for r in self.channel_rows(query)], ['Discovery Channel'])
        self.assertIsNone(self.row_named('Discovery Channel', query)['why'])

    def test_no_typed_query_means_no_chip_anywhere(self):
        self.assertFalse(any(r['why'] for r in self.channel_rows()))


class PagingAndFacetTests(_ApiTestCase):

    def test_paging_reports_the_whole_result_not_the_page(self):
        payload = self.get('per_page=2&' + SHOW_ALL)
        self.assertEqual(len(payload['rows']), 2)
        self.assertEqual(payload['page_size'], 2)
        self.assertGreater(payload['total'], 2)
        self.assertEqual(payload['pages'],
                         (payload['total'] + 1) // 2)

    def test_pages_do_not_overlap(self):
        first = self.get('per_page=2&page=1&' + SHOW_ALL)['rows']
        second = self.get('per_page=2&page=2&' + SHOW_ALL)['rows']
        self.assertFalse({r['id'] for r in first} & {r['id'] for r in second})

    def test_facets_absent_counts_every_visible_dimension(self):
        payload = self.get()
        # This endpoint's default grain is `channels`, and `when` exists only on the airing
        # grain - so "every visible dimension" is per grain, not the whole registry.
        self.assertEqual(sorted(payload['facets']),
                         sorted(d.key for d in visible_dimensions_for(GRAIN_CHANNELS)))
        self.assertEqual(payload['facets_counted'], sorted(payload['facets']))

    def test_facets_empty_counts_nothing(self):
        """The rows-only fetch the page uses while the user is typing - 46ms against 667ms
        with the rail attached, measured on the production database. `facets=` is the only
        way to say "none", because an absent parameter means "all"."""
        payload = self.get('facets=')
        self.assertEqual(payload['facets'], {})
        self.assertEqual(payload['facets_counted'], [])
        self.assertTrue(payload['rows'])

    def test_one_named_facet_is_counted_alone(self):
        payload = self.get('facets=cat')
        self.assertEqual(list(payload['facets']), ['cat'])
        self.assertEqual(payload['facets']['cat']['Docs'], 3)

    def test_standing_hidden_is_reported_per_option(self):
        """Nothing is hidden silently: what a default-on option took out is named and
        counted, and the number is of THIS search's rows so the user can act on it."""
        payload = self.get()
        self.assertEqual(payload['standing_hidden']['showdup'], 2)
        # `channel_total`, not `total`: the standing options count what they took out of the
        # CHANNELS table, and `total` is the merged number the pager pages, group rows
        # included. Adding the two would be exactly the blended number
        # DESIGN-group-search-rows.md §5.2 calls the defect (dev/changelog/811).
        self.assertEqual(payload['channel_total'] + sum(payload['standing_hidden'].values()),
                         Channel.query.count())
        self.assertEqual(payload['total'],
                         payload['channel_total'] + payload['group_total'])


class CountsSplitTests(_ApiTestCase):
    """`counts=0` and `GET /api/channels/search/counts` (dev/changelog/598) - the row
    response no longer has to wait on `_standing_breakdown()`, which on the airing grain's
    unfiltered first paint is ~1.6s of a ~2.1s request the row query itself never needed.

    The contract: `counts=0` returns rows exactly as before but `total`/`pages`/
    `standing_hidden` come back JSON `null` (pending, not a real zero) instead of being
    computed; the new endpoint answers those three alone, and its answer must be identical
    to what the bundled endpoint would have returned for the same state.
    """

    def counts(self, query='', expect=200):
        # The same injection `get()` does, for the same reason and so the two stay
        # comparable: this class's whole point is that they answer identically, which they
        # cannot if one of them folds this corpus's group members and the other does not.
        query = _with_members(query)
        resp = self.t.client.get(COUNTS_URL + ('?' + query if query else ''))
        self.assertEqual(resp.status_code, expect,
                         f'{query!r} returned {resp.status_code}: {resp.get_json()}')
        return resp.get_json()

    def test_counts_0_leaves_totals_null_but_still_returns_rows(self):
        payload = self.get('counts=0')
        self.assertIsNone(payload['total'])
        self.assertIsNone(payload['pages'])
        self.assertIsNone(payload['standing_hidden'])
        self.assertTrue(payload['rows'])

    def test_default_is_unchanged_from_before_this_parameter_existed(self):
        """Every existing caller (other pages linking in, every other test in this file) is
        byte-identical - the parameter is opt-in, not a behavior change."""
        payload = self.get()
        self.assertIsInstance(payload['total'], int)
        self.assertIsInstance(payload['pages'], int)
        self.assertIsInstance(payload['standing_hidden'], dict)

    def test_counts_endpoint_matches_what_the_bundled_response_would_have_said(self):
        bundled = self.get(SHOW_ALL)
        alone = self.counts(SHOW_ALL)
        self.assertEqual(alone['total'], bundled['total'])
        self.assertEqual(alone['pages'], bundled['pages'])
        self.assertEqual(alone['standing_hidden'], bundled['standing_hidden'])

    def test_counts_endpoint_matches_with_default_standing_options(self):
        """Same assertion with the real default standing set active (not the SHOW_ALL
        override above), so the options that hide by default are exercised too."""
        bundled = self.get()
        alone = self.counts()
        self.assertEqual(alone['total'], bundled['total'])
        self.assertEqual(alone['standing_hidden'], bundled['standing_hidden'])

    def test_counts_endpoint_carries_no_rows_or_facets(self):
        payload = self.counts()
        self.assertEqual(
            set(payload),
            {'success', 'total', 'pages', 'standing_hidden', 'declined', 'declined_reason',
             # The two numbers behind `total` ride with it rather than being a third
             # request: the results heading names both kinds (dev/changelog/811).
             'channel_total', 'group_total'})

    def test_counts_endpoint_honours_filters_and_grain(self):
        """A TYPED search, so the bundled response declines its own total (this corpus has
        no built index, which is one of the degraded states) and the counts endpoint is the
        only one that answers it - dev/changelog/676. The point of the assertion is
        unchanged: the standalone endpoint honours `grain` and `q` rather than counting
        some other search."""
        bundled = self.get('grain=airings&q=espn')
        alone = self.counts('grain=airings&q=espn')
        self.assertIsNone(bundled['total'])
        self.assertEqual(bundled['declined'], ['counts', 'facets'])
        # Cheap on this corpus, so it is attempted and served rather than declined.
        self.assertEqual(alone['declined'], [])
        self.assertIsInstance(alone['total'], int)

    def test_counts_endpoint_400s_on_an_unanswerable_state_like_the_main_endpoint(self):
        payload = self.counts('grain=nope', expect=400)
        self.assertIn('error', payload)


class CatalogTests(_ApiTestCase):
    """The registries and vocabularies, served rather than duplicated.

    Adding a search field or a facet is meant to be one entry in the engine's registry. These
    assert the catalog is generated FROM those registries, so a hand-typed copy in the
    endpoint (or later in a template) fails here instead of drifting quietly.
    """

    def catalog(self):
        resp = self.t.client.get(CATALOG_URL)
        self.assertEqual(resp.status_code, 200)
        return resp.get_json()

    def test_the_registries_are_the_engines_own(self):
        payload = self.catalog()
        self.assertEqual([f['key'] for f in payload['fields']], [f.key for f in FIELDS])
        self.assertEqual([d['key'] for d in payload['dimensions']],
                         [d.key for d in DIMENSIONS])
        self.assertEqual([s['key'] for s in payload['standing_options']],
                         [s.key for s in STANDING_OPTIONS])
        self.assertEqual(payload['sorts'], sorted(SORTS))
        self.assertEqual(payload['default_fields'], list(DEFAULT_FIELDS))

    def test_the_dimension_order_is_the_registry_order(self):
        """The facet order is deliberate and the tuple IS the order - nothing sorts it
        downstream, so serializing it through a dict or a set would quietly reorder the
        rail."""
        payload = self.catalog()
        self.assertEqual([d['key'] for d in payload['dimensions'] if not d['hidden']],
                         [d.key for d in VISIBLE_DIMENSIONS])

    def test_the_vocabularies_cover_values_the_current_search_counts_zero_of(self):
        """The rail's three-state control has to let you exclude a value you cannot
        currently see, so the vocabulary is the whole table's, not the result's."""
        payload = self.catalog()
        self.assertIn('Radio', payload['categories'])
        self.assertEqual([a['name'] for a in payload['accounts']], ['Alpha', 'Beta'])
        self.assertIn('sports-tag', [t['name'] for t in payload['tags']])
        self.assertEqual(sorted(g['name'] for g in payload['groups']),
                         ['Fox', 'Nightly checks'])
        self.assertEqual(payload['total_channels'], Channel.query.count())

    def test_tag_patterns_ride_along(self):
        """The row badge says "matched by pattern: ..." - without the patterns the tooltip
        would have to ask the server per tag."""
        tags = {t['name']: t for t in self.catalog()['tags']}
        self.assertEqual(tags['sports-tag']['patterns'], ['ESPN'])

    def test_duration_is_scoped_to_the_airing_grain_like_when(self):
        """A channel has no length of its own, only a showing does - same reasoning `when`
        is grain-scoped on (dev/changelog/594)."""
        payload = self.catalog()
        self.assertIn('duration', payload['by_grain']['airings']['dimensions'])
        self.assertNotIn('duration', payload['by_grain']['channels']['dimensions'])


class GroupRowTests(_ApiTestCase):
    """What the endpoint SAYS about a channel group (dev/changelog/811).

    `tests/test_group_search_rows.py` pins which rows the engine returns and where they
    land; this pins the payload the page draws them from - a group's answers to the
    channel columns, the reason each unanswerable one carries, and the `kind` that keeps a
    renderer from drawing a group as a channel.

    THE DEFAULT SEARCH IS WHAT THESE ASK, deliberately: `get()` above unfolds this corpus
    for every other test in the file, and the fold is the default behavior nothing else
    here exercises.
    """

    def default_search(self, query=''):
        resp = self.t.client.get(SEARCH_URL + ('?' + query if query else ''))
        self.assertEqual(resp.status_code, 200, resp.get_json())
        return resp.get_json()

    def group_row(self, name='Fox', query=''):
        rows = [r for r in self.default_search(query)['rows']
                if r['kind'] == 'group' and r['name'] == name]
        self.assertEqual(len(rows), 1, f'expected one {name!r} group row')
        return rows[0]

    def test_every_row_says_what_kind_it_is(self):
        """Asked off the payload rather than inferred from which keys are present: a
        group's payload is deliberately a different shape, and a renderer guessing from
        the keys is one payload change away from drawing a group as a channel."""
        kinds = {r['kind'] for r in self.default_search()['rows']}
        self.assertEqual(kinds, {'channel', 'group'})

    def test_the_group_row_carries_its_own_numbers(self):
        row = self.group_row()
        self.assertEqual(row['member_count'], 2)
        self.assertEqual(row['recording_member_count'], 2)
        self.assertTrue(row['in_guide'])
        # The best-ranked recording-enabled member - the same rule the guide row and the
        # recorder use, so the row names the feed a recording would actually open.
        self.assertEqual(row['serving']['name'], 'US| ESPN2 HD')

    def test_the_channel_only_columns_say_why_they_are_empty(self):
        """§5.2's subtraction: `--` plus a reason, not a member's value wearing the
        group's name. The reason ships with the row because it is a fact about the data
        model, and the two widths word everything else differently."""
        row = self.group_row()
        for key in ('account', 'category', 'sid', 'tvg', 'url', 'groups', 'tags'):
            self.assertIn(key, row['no_value'], key)
            self.assertTrue(row['no_value'][key].strip(), key)

    def test_a_health_check_only_group_says_it_is_not_a_recording_source(self):
        row = self.group_row('Nightly checks', query='q=nightly')
        self.assertTrue(row['check_only'])

    def test_the_two_numbers_ride_with_the_rows(self):
        payload = self.default_search()
        self.assertEqual(payload['total'],
                         payload['channel_total'] + payload['group_total'])
        self.assertEqual(payload['group_total'], 2)

    def test_a_group_member_keeps_a_row_of_its_own_by_default(self):
        """dev/changelog/860 flipped `showmembers` on. The fold mirrored the TV Guide, and
        the guide was the wrong model to borrow from: this page is where you go to FIND a
        channel, and a search that silently answers "no such channel" because it is in a
        group is exactly the hidden behavior this project refuses. The group's own row is
        unaffected - both are on screen, which is what `guide_via` on the member explains."""
        payload = self.default_search()
        names = [r['name'] for r in payload['rows']]
        self.assertIn('Discovery Channel', names)
        self.assertIn('Fox', names)
        self.assertIn('US| ESPN2 HD', names)
        self.assertNotIn('showmembers', payload['standing_hidden'])

    def test_turning_the_fold_back_on_hides_the_member_and_counts_it(self):
        """The other half: it is still a switchable, DISCLOSED option, so turning it off
        removes the member's own row and the count line names what went."""
        payload = self.default_search('standing=shownoepg&standing=showuntested')
        names = [r['name'] for r in payload['rows']]
        self.assertNotIn('Discovery Channel', names)
        self.assertIn('Fox', names)
        # espn2 holds its own guide row, so it is never folded.
        self.assertIn('US| ESPN2 HD', names)
        self.assertEqual(payload['standing_hidden']['showmembers'], 1)


class AiringGroupLabelTests(_ApiTestCase):
    """The airing grain's collapse survivor, relabelled as the group it stands for.

    "Collapse channel groups" already picked that row by the group's own ranking rule; all
    that was missing was saying so (`DESIGN-group-search-rows.md` §5.1's shape B surviving
    inside C). The label is present only while the option is ON: with it off every member
    carries its own row for the same program, and naming one of them as the group would
    name a row its siblings are equally part of.
    """

    def _seed(self):
        super()._seed()
        now = datetime.utcnow()
        # The same program on both members of the Fox group, which is what the collapse
        # exists for: one row survives and it should be the group's.
        for ch in (self.espn2, self.discovery):
            db.session.add(EPGEntry(
                channel_id=ch.id, title='Shared Kickoff', description='Both feeds carry it',
                start_time=now + timedelta(hours=5), stop_time=now + timedelta(hours=6)))
        db.session.commit()

    def airings(self, query=''):
        resp = self.t.client.get(f'{SEARCH_URL}?grain=airings&{query}')
        self.assertEqual(resp.status_code, 200, resp.get_json())
        return [r for r in resp.get_json()['rows'] if r['title'] == 'Shared Kickoff']

    def test_the_surviving_row_names_the_group(self):
        rows = self.airings()
        self.assertEqual(len(rows), 1, 'the collapse should leave one row')
        self.assertIsNotNone(rows[0]['group'])
        self.assertEqual(rows[0]['group']['name'], 'Fox')
        # The member underneath is still named - "record the group" has to say what it
        # will open.
        self.assertEqual(rows[0]['channel']['name'], 'US| ESPN2 HD')

    def test_with_the_collapse_off_no_row_claims_to_be_the_group(self):
        rows = self.airings(query=show_all_query())
        self.assertEqual(len(rows), 2, 'both members should carry their own row')
        self.assertEqual([r['group'] for r in rows], [None, None])


class AiringRecordContextGroupTests(_ApiTestCase):
    """`?group=` on the record-context endpoint - what turns Record on a group's row into
    a group recording (dev/changelog/811).

    The rule it preserves is the one dev/changelog/793 settled: a group is never INFERRED
    for a fresh Record click. It is now sometimes ASKED FOR, by a row that says on screen
    that it is a group, and the endpoint validates the ask rather than trusting it.
    """

    def context(self, epg_id, query='', expect=200):
        resp = self.t.client.get(
            f'/api/channels/airings/{epg_id}/record-context' + (f'?{query}' if query else ''))
        self.assertEqual(resp.status_code, expect, resp.get_json())
        return resp.get_json()

    def test_no_group_is_invented_for_a_plain_record_click(self):
        payload = self.context(self.airing.id)
        self.assertIsNone(payload['program']['group_id'])
        self.assertIsNone(payload['group'])

    def test_an_explicit_group_is_honoured_and_named(self):
        payload = self.context(self.airing.id, f'group={self.group.id}')
        self.assertEqual(payload['program']['group_id'], self.group.id)
        self.assertEqual(payload['group']['name'], 'Fox')

    def test_a_group_the_showing_is_not_on_is_refused(self):
        other = seed.make_group(name='Unrelated', members=[self.untested], in_guide=False)
        db.session.commit()
        self.context(self.airing.id, f'group={other.id}', expect=400)

    def test_an_unknown_group_id_is_refused(self):
        self.context(self.airing.id, 'group=99999', expect=400)


if __name__ == '__main__':
    unittest.main(verbosity=2)
