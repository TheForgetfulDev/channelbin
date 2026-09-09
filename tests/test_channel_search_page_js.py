"""Tier 0 - the channel search PAGE, driven in a real DOM.

The Browse tab is 2,100 lines of client-side JavaScript over a JSON API, so nothing
between `/api/channels/search` answering correctly and the user seeing the right thing is
reachable from Python. That gap is not theoretical: `templates/channels/search.html`
handed the module its config as a bare top-level `const`, which is a lexical binding and
never a property of `window`, so boot threw on its first line and **not one listener was
registered** - through three whole steps of the build, every one of them verified with
curl and against the JSON endpoints (dev/docs/BUGS.md 2026-07-30 05:15 PM). A dead page and
an empty one look identical from outside, because the server-rendered markup IS the
"nothing active" state.

So this runs the shipped `static/js/util.js` + `static/js/channel-search.js` against the
markup the Flask route really rendered, answering their fetches with what the real
endpoints really returned over a seeded corpus, and asserts on what the page then did.
`tests/support/channel_search_page.mjs` drives it and reports observations; every
assertion lives here, so a failure reads as a sentence about the page rather than as a
node exit code. Same idea as `tests/test_check_modal_js.py`, one level up: that one calls
pure helpers, this one dispatches real events and reads the DOM back.

What this CANNOT cover, so that nobody reads a green run as more than it is: jsdom
computes no layout, so the frozen Channel-column width (`freezeNameCol()`) and every other
geometric question need a browser; and jsdom does not fetch `<script src>`, so the three
shared modals (check-modal.js, group-modal.js, dup-modal.js) are absent here.

The node process runs ONCE for the whole module and every class reads its slice of the
result - booting jsdom five times and waiting out the page's 250ms debounce is the
expensive part, and it is the same page each time.
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tests.support.app import make_test_app  # noqa: E402
from tests.support import seed  # noqa: E402
from tests.support.search import show_all_query  # noqa: E402
from app import db  # noqa: E402
from app.accounts import NORM_DISABLED, NORM_MPEGTS  # noqa: E402
from app.database import EPGEntry, Tag, TagPattern  # noqa: E402

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SHOW_ALL = show_all_query()

HARNESS = os.path.join(REPO, 'tests', 'support', 'channel_search_page.mjs')
JSDOM = os.path.join(REPO, 'node_modules', 'jsdom')

# The four fixtures the harness reads, and the request each is the real answer to.
_FIXTURES = {
    'rows.json': '/api/channels/search?facets=',
    # Nothing hidden, so a whole duplicate cluster is present and its members carry the
    # `dup.ids` the drill-in navigates by. SHOW_ALL rather than a bare `standing=`: since
    # the inversion (dev/changelog/778) an empty set hides everything it can.
    'rows_dup.json': '/api/channels/search?facets=&' + SHOW_ALL,
    # One row a page, so the pager really has a Next to click.
    'rows_paged.json': '/api/channels/search?facets=&per_page=1',
    # The AIRING grain, with nothing being hidden so all three seeded showings are
    # present - by default the ended one is hidden, and an ended showing is the state the
    # Record button has to render as disabled.
    #
    # ITS NUMBERS COME FROM THEIR OWN ENDPOINTS, and they have to: with the past in, the row
    # endpoint declines the total and the rail outright, because every aggregate over an
    # airing search that includes ended showings reads the whole table (dev/changelog/681).
    # That is the real answer to this URL, and the harness merges the two below back
    # together so the airing scenarios see the state the page actually settles into -
    # rows, then counts, then the rail. A scenario ABOUT declining passes `declineWhy`
    # instead, rather than depending on a fixture happening to be in that state.
    'rows_airings.json': '/api/channels/search?grain=airings&facets=&' + SHOW_ALL,
    'airings_counts.json': '/api/channels/search/counts?grain=airings&' + SHOW_ALL,
    'airings_facets.json': '/api/channels/search/facets?grain=airings&' + SHOW_ALL,
    # Beta has zero channels, so its id is genuinely absent from `facets.acct` (not
    # merely uncounted) - the real, counted 0 that a "Filter by" suggestion must not
    # offer (dev/docs/BUGS.md 2026-08-12, the 0-result suggestion bug).
    'rows_acctfacets.json': '/api/channels/search?facets=acct',
    'catalog.json': '/api/channels/search/catalog',
}

_RESULT = None


def _seed_corpus():
    """The same shape as tests/test_channel_search_api.py's corpus - a duplicate cluster
    whose winner is decided by the in-guide rung rather than by health, an out-of-guide
    row for the row actions, a tag that matches by pattern, and a group for the action
    context."""
    now = datetime.utcnow()
    acct = seed.make_account(name='Alpha', url_normalization=NORM_MPEGTS, last_sync_at=now)
    seed.make_account(name='Beta', url_normalization=NORM_DISABLED, last_sync_at=now)

    def channel(name, **kw):
        kw.setdefault('last_seen_at', now)
        return seed.make_channel(acct, name=name, **kw)

    espn2 = channel('US| ESPN2 HD', category_name='Sports', health_score=90.0, in_guide=True)
    discovery = channel('Discovery Channel', category_name='Docs', health_score=40.0)
    channel('Test Card', category_name='Docs')
    channel('Radio Mount', category_name='Radio', health_score=70.0, url_normalizable=False)
    dup_high = channel('Sky Sports Action', category_name='Sports', health_score=99.0)
    dup_low = channel('Sky Sports Action HD', category_name='Sports', health_score=10.0)
    for ch in (espn2, dup_high, dup_low):
        ch.stream_url = 'http://example.test/live/user1/pass1/77'
        ch.is_duplicate_stream_url = True
    channel('Gone Fishing TV', category_name='Docs', health_score=55.0,
            last_seen_at=now - timedelta(days=30))

    db.session.add(EPGEntry(
        channel_id=discovery.id, title='Wembley Cup Final', sub_title='Semi Final',
        description='Liverpool play at Wembley',
        start_time=now - timedelta(minutes=10), stop_time=now + timedelta(minutes=50)))
    # Two more showings ON THE SAME CHANNEL, so the airing grain has all three record
    # states to draw without changing which CHANNELS have EPG data - the `noepg` standing
    # option counts channels, and giving another channel an entry would move every
    # channel-grain observation in this module.
    db.session.add(EPGEntry(
        channel_id=discovery.id, title='Yesterday Match', sub_title='Replay',
        start_time=now - timedelta(hours=4), stop_time=now - timedelta(hours=3)))
    upcoming = EPGEntry(
        channel_id=discovery.id, title='Tomorrow Final', sub_title='Live',
        start_time=now + timedelta(days=1), stop_time=now + timedelta(days=1, hours=2))
    db.session.add(upcoming)
    db.session.flush()
    # A SCHEDULED recording overlapping that one, so `record_state` is 'scheduled' on it
    # and 'none' on its two neighbours - the per-showing distinction the payload exists
    # to make (DESIGN-channel-search.md 9.4).
    seed.make_recording(status='SCHEDULED', channel_id=discovery.id,
                        start_time=upcoming.start_time, stop_time=upcoming.stop_time)
    tag = Tag(name='sports-tag')
    db.session.add(tag)
    db.session.flush()
    db.session.add(TagPattern(tag_id=tag.id, pattern='ESPN'))
    seed.make_group(name='Fox', members=[espn2, discovery])
    db.session.commit()


def _run_harness():
    """Render the page and the endpoints for real, then hand them to node."""
    t = make_test_app()
    ctx = t.app.app_context()
    ctx.push()
    try:
        _seed_corpus()
        with tempfile.TemporaryDirectory() as tmp:
            page = t.client.get('/channels')
            if page.status_code != 200:
                raise AssertionError(f'/channels returned {page.status_code}')
            with open(os.path.join(tmp, 'page.html'), 'w') as fh:
                fh.write(page.get_data(as_text=True))
            for name, url in _FIXTURES.items():
                resp = t.client.get(url)
                if resp.status_code != 200:
                    raise AssertionError(f'{url} returned {resp.status_code}')
                with open(os.path.join(tmp, name), 'w') as fh:
                    fh.write(resp.get_data(as_text=True))
            proc = subprocess.run(['node', HARNESS, tmp, REPO], capture_output=True,
                                  text=True, cwd=REPO, timeout=300)
        if proc.returncode != 0 and not proc.stdout.strip():
            raise AssertionError(f'the jsdom harness failed:\n{proc.stderr}')
        result = json.loads(proc.stdout)
        if 'harness_error' in result:
            raise AssertionError(f'the jsdom harness threw:\n{result["harness_error"]}')
        return result
    finally:
        ctx.pop()
        t.cleanup()


def _observations():
    global _RESULT
    if _RESULT is None:
        _RESULT = _run_harness()
    return _RESULT


@unittest.skipIf(shutil.which('node') is None, 'node not installed')
@unittest.skipIf(not os.path.isdir(JSDOM), 'jsdom not installed (npm install)')
class _PageJs:
    """Mixin, not a TestCase: unittest collects every TestCase subclass in a module, so a
    shared base with test methods on it would run once more against no scenario. The skip
    decorators above are inherited by the real classes with it."""

    SCENARIO = ''

    @classmethod
    def setUpClass(cls):
        cls.obs = _observations()[cls.SCENARIO]

    def test_the_page_booted_without_throwing(self):
        """Every other assertion in the class is worthless if this one fails: a page
        whose JS died renders exactly the markup the server sent."""
        self.assertEqual(self.obs['errors'], [])


class BootTests(_PageJs, unittest.TestCase):
    """First paint, once the JS has actually run."""

    SCENARIO = 'boot'

    def test_the_config_object_is_reachable_as_a_window_property(self):
        """dev/docs/BUGS.md 2026-07-30 05:15 PM - a bare top-level `const` is a lexical
        binding, never `window.X`, and the whole page died on the first line."""
        self.assertTrue(self.obs['config_is_a_window_property'])

    def test_the_catalog_is_fetched_once_and_the_rows_follow(self):
        self.assertTrue(self.obs['fetched_catalog'])
        self.assertGreater(self.obs['row_count'], 0)

    def test_the_rows_are_fetched_with_no_facets_and_the_counts_follow_behind(self):
        """46ms against 667ms on the production database (dev/changelog/396). `facets=`
        is the only spelling of "count nothing" - absent means "count everything".

        ONE row request, not two. The rail used to be fetched from this same endpoint with
        `facets=<dims>&counts=0`, so the LIMIT-100 page query ran twice per keystroke and
        the second set of rows was discarded - ~3.4s of pure waste per unindexed airing
        keystroke on the live database. The rail has its own endpoint now
        (dev/changelog/676); `facet_dims_requested` below reads from it."""
        self.assertIn('facets=&', self.obs['rows_request'] + '&')
        self.assertEqual(self.obs['search_request_count'], 1,
                         'the page still fetches the row query more than once per paint')
        self.assertTrue(self.obs['facet_dims_requested'],
                        'the rail request asked for no facet counts at all')

    def test_the_rail_renders_one_card_per_visible_dimension(self):
        self.assertEqual(self.obs['rail_facets'],
                         [d['key'] for d in _observations()['catalog_dimensions']])

    def test_the_standing_options_are_the_rails_footer(self):
        self.assertTrue(self.obs['rail_has_standing_card'])

    def test_an_uncounted_facet_value_reads_as_not_counted_never_as_zero(self):
        """A dimension absent from `facets_counted` means "not requested". Rendering it
        as 0 would tell the user there are no Sports channels while the list shows
        them."""
        glyphs = set(self.obs['rail_uncounted_glyphs'])
        self.assertTrue(glyphs, 'nothing rendered as uncounted on the first paint')
        self.assertEqual(glyphs, {'--'})

    def test_the_well_starts_as_an_invitation_not_a_status_line(self):
        self.assertIn('add a filter', self.obs['well_html'])

    def test_all_words_is_the_default_match_mode(self):
        """Read off the JOINED control, which is the one live copy above the breakpoint
        since dev/changelog/811. The segmented spelling that used to sit beside the box is
        gone rather than hidden - it was a second live match-mode control on the same line,
        which is exactly the duplication that round removed from the chip row."""
        self.assertIn('All words', self.obs['words_join_html'])
        self.assertNotIn('Any word ', self.obs['words_join_html'].split('<div class="menu"')[0])

    def test_the_columns_picker_lists_every_column_with_the_defaults_ticked(self):
        """Nine columns, four of them on by default - Channel is pinned first and is
        deliberately not in the picker at all.

        The four are Health, Now airing, Groups and Account (dev/changelog/860). Status
        used to lead that set and is not in this list at all any more: it was a fixed 150px
        track that is blank on nearly every row, and its five badges moved to the name cell
        where they cost nothing on a row that has none."""
        self.assertEqual(len(self.obs['column_items']), 9)
        self.assertNotIn('status', self.obs['column_items'])
        self.assertEqual(sorted(self.obs['hidden_columns']),
                         sorted(['category', 'sid', 'tvg', 'url', 'tags']))
        self.assertNotIn('name', self.obs['column_items'])

    def test_only_the_registrys_own_sorts_get_a_sortable_header(self):
        """`airing`, `groups` and `tags` are not server-sortable (dev/changelog/396):
        sorting the current page instead would reorder 100 rows out of 134,399 and call it
        a sort."""
        self.assertTrue(self.obs['sortable_headers'])
        for key in self.obs['sortable_headers']:
            self.assertIn(key, _observations()['catalog_sorts'])
        for key in ('airing', 'groups', 'tags'):
            self.assertNotIn(key, self.obs['sortable_headers'])

    def test_the_selection_bar_starts_hidden(self):
        self.assertFalse(self.obs['selection_bar_shown'])


class SearchBoxTests(_PageJs, unittest.TestCase):
    """The chips, the scope switches and the + Filter popover."""

    SCENARIO = 'box'

    def test_typed_text_is_one_chip_carrying_the_whole_query(self):
        self.assertIn('cs-chip', self.obs['text_chip_html'])
        self.assertIn('espn wembley', self.obs['text_chip_html'])
        self.assertIn('data-rm="__alltext"', self.obs['text_chip_html'])
        self.assertEqual(self.obs['text_sent'], ['espn wembley'])

    def test_the_chip_class_is_cs_chip_and_never_the_toolbars_chip(self):
        """Production's `.chip` is the toolbar filter pill - a different component. Two
        meanings for one class name is how a future promotion breaks a page nobody was
        looking at."""
        self.assertNotIn('class="chip ', self.obs['text_chip_html'])
        self.assertNotIn('class="chip"', self.obs['text_chip_html'])

    def test_focusing_the_box_opens_the_scope_pane_and_the_suggestions(self):
        self.assertTrue(self.obs['sugg_open'])
        self.assertIn('Searching in', self.obs['sugg_html'])

    def test_every_registry_field_gets_a_switch_and_none_is_disabled(self):
        """Description search ships working and indexed (dev/changelog/395), so the
        "unavailable" treatment the mockups carried is a defect, not a leftover: UI text
        describing backend behaviour that is no longer true."""
        self.assertEqual(self.obs['sugg_field_switches'],
                         [f['key'] for f in _observations()['catalog_fields']])
        self.assertEqual(self.obs['sugg_disabled_switches'], 0)
        self.assertNotIn('no index', self.obs['sugg_html'].lower())

    def test_switching_a_field_on_writes_registry_order_not_click_order(self):
        """`in` is part of the URL, so a shared link must not depend on which switch was
        flipped first."""
        self.assertEqual(self.obs['fields_after_tick'], ['name', 'epg-title', 'epg-desc'])

    def test_turning_every_field_off_keeps_one_on_and_says_so(self):
        self.assertEqual(self.obs['fields_after_all_off'], ['name'])
        self.assertTrue(any('Channel name stayed on' in t for t in self.obs['toasts_after_all_off']),
                        self.obs['toasts_after_all_off'])

    def test_the_filter_popover_offers_every_visible_dimension_and_no_hidden_one(self):
        visible = [d['key'] for d in _observations()['catalog_dimensions']]
        self.assertEqual(self.obs['popover_dims'], visible)
        self.assertNotIn('chan', self.obs['popover_dims'])

    def test_the_popover_survives_drilling_in_and_picking_a_value(self):
        """dev/docs/BUGS.md 2026-07-30 05:15 PM - every branch of the panel's handler
        rewrites the panel, which DETACHES the clicked node, so util.js's document closer
        found no `.menu.open` ancestor and shut the popover on every pick. Any panel that
        rebuilds itself on click needs its own e.stopPropagation()."""
        self.assertTrue(self.obs['popover_open_after_open'])
        self.assertTrue(self.obs['popover_open_after_drilling_in'])
        self.assertTrue(self.obs['popover_offers_values'])

    def test_picking_a_value_filters_and_chips_it(self):
        self.assertEqual(self.obs['filters_after_pick'], [self.obs['picked_value']])
        self.assertIn('cs-chip', self.obs['well_after_pick'])

    def test_removing_the_chip_takes_the_filter_out_of_the_search(self):
        self.assertEqual(self.obs['filters_after_chip_removed'], [])

    def test_any_word_reaches_the_request(self):
        self.assertEqual(self.obs['match_after_any'], ['any'])

    def test_clear_all_clears_the_text_and_the_filters_but_not_the_scope(self):
        """"Search in" is a scope, not a filter - clearing it would silently change what
        counts as a match. The page says so in the toast."""
        after = self.obs['after_clear_all']
        self.assertNotIn('q', after)
        self.assertNotIn('f.cat', after)


class SuggestionZeroResultTests(_PageJs, unittest.TestCase):
    """dev/docs/BUGS.md 2026-08-12 - the "Filter by" suggestion menu must not offer a
    value that would return 0 results under the facet counts already loaded. Beta has no
    channels at all, so its account id is genuinely absent from `facets.acct` (a real 0,
    not the "--" not-yet-counted state)."""

    SCENARIO = 'suggestion_facets'

    def test_a_value_with_a_real_zero_count_is_not_suggested(self):
        self.assertNotIn('Beta', self.obs['sugg_html_zero_count'])

    def test_a_value_with_a_real_nonzero_count_is_still_suggested(self):
        self.assertIn('Alpha', self.obs['sugg_html_nonzero_count'])
        self.assertIn('>4<', self.obs['sugg_html_nonzero_count'])


class TableTests(_PageJs, unittest.TestCase):
    """The columns picker, the sort guard, the selection Map and the row actions."""

    SCENARIO = 'table'

    def test_showing_a_column_adds_it_to_the_header_and_to_every_row(self):
        self.assertNotIn('Category', ' '.join(self.obs['header_before']))
        self.assertIn('Category', ' '.join(self.obs['header_after_showing_category']))
        # 3 pinned cells (tick, logo, Channel) + the visible columns, and NO action cell -
        # this grain's rows carry no action since dev/changelog/860, and an empty trailing
        # track on every row is exactly the width this table did not have.
        self.assertEqual(self.obs['row_cell_count'], 3 + self.obs['visible_column_count'])
        self.assertEqual(self.obs['head_cell_count'], self.obs['row_cell_count'],
                         'the header and a row are separate grid containers - a cell count '
                         'that differs slides every label off its column')

    def test_the_column_setup_persists_to_the_server_not_to_local_storage(self):
        """DESIGN.md 3.11 - column setup follows the user across browsers, through the
        same generic /api/user-prefs row the recordings list uses."""
        self.assertIn('/api/user-prefs/channel_search_columns', self.obs['column_pref_url'])
        value = self.obs['column_pref_value']
        self.assertIsInstance(value.get('order'), list)
        self.assertNotIn('category', value.get('hidden'))

    def test_clicking_a_sortable_header_sorts_the_whole_result_set(self):
        self.assertTrue(self.obs['category_is_sortable'])
        self.assertEqual([s.lstrip('-') for s in self.obs['sort_after_click']], ['category'])

    def test_hiding_the_column_a_sort_runs_on_falls_back_to_channel_and_says_so(self):
        """A sort whose column is off screen is a silent state. Channel is pinned and
        cannot be hidden, so it is the only fallback that is always visible - and the
        order moved, so the page number no longer points at the same slice."""
        self.assertNotIn('category', [s.lstrip('-') for s in self.obs['sort_after_hiding_the_column']])
        self.assertTrue(any('sorted by Channel' in t
                            for t in self.obs['toasts_after_hiding_the_column']),
                        self.obs['toasts_after_hiding_the_column'])
        self.assertIn(self.obs['page_after_hiding_the_column'], ([], ['1']))

    def test_the_four_unsortable_columns_stay_unsortable_when_shown(self):
        for key in ('airing', 'status', 'groups', 'tags'):
            self.assertNotIn(key, self.obs['sortable_headers'])
        for key in self.obs['sortable_headers']:
            self.assertIn(key, self.obs['catalog_sorts'])

    def test_ticking_a_row_raises_the_bar_and_every_button_counts_it(self):
        self.assertTrue(self.obs['bar_shown_at_one'])
        for label in self.obs['buttons_at_one']:
            self.assertIn('(1)', label)

    def test_select_all_is_the_page_and_the_header_tick_agrees(self):
        """It cannot select 134,399 rows it has never fetched, so it selects the page."""
        self.assertTrue(self.obs['all_rows_ticked'])
        self.assertIn(f'({self.obs["page_row_count"]})', self.obs['buttons_after_select_all'])
        self.assertTrue(self.obs['header_tick_reflects_page'])

    def test_the_selection_survives_a_new_search_and_the_bar_says_how_many_are_out_of_view(self):
        self.assertIn(f'({self.obs["page_row_count"]})', self.obs['buttons_after_a_new_search'])
        self.assertIn('not in the current results', self.obs['selection_note'])

    def test_deselect_all_empties_the_selection_and_the_note(self):
        self.assertFalse(self.obs['bar_after_deselect'])
        self.assertEqual(self.obs['note_after_deselect'], '')

    def test_the_duplicate_drill_in_goes_by_channel_id_never_by_the_stream_url(self):
        """The row payload MASKS stream URLs (they routinely carry account credentials),
        so the text on screen is not the text in the database and searching it matches
        nothing. `dup.ids` carries the whole cluster; `dup.others` is the tooltip's list
        and is truncated at four."""
        self.assertTrue(self.obs['dup_badge_rendered'])
        self.assertEqual(len(self.obs['dup_payload_ids']), self.obs['dup_payload_count'])
        self.assertEqual(sorted(self.obs['drill_in_filters']),
                         sorted(str(i) for i in self.obs['dup_payload_ids']))

    def test_the_drill_in_turns_show_duplicates_on(self):
        """Otherwise the copies it just went to look at are the very rows that are hidden
        without it. ADDS the key rather than removing one (dev/changelog/778) - the old
        spelling deleted `dup`, which after the rename is a no-op that leaves the drill-in
        showing a single row and calling it the cluster."""
        self.assertIn('showdup', self.obs['drill_in_standing'])

    def test_the_way_back_restores_the_search_that_was_there_before(self):
        self.assertTrue(self.obs['back_button_offered'])
        self.assertEqual(self.obs['address_bar_after_back'],
                         self.obs['address_bar_before_drill_in'])
        self.assertFalse(self.obs['back_button_after_use'])

    def test_every_row_is_a_link_to_its_channel(self):
        self.assertGreater(self.obs['open_channel_targets'], 0)

    def test_a_row_in_the_guide_only_through_a_group_says_which_group(self):
        """dev/changelog/759. `in_guide` is "has a row of its own" and nothing else since
        dev/changelog/751, while the "In your guide" filter matches the wider guide SCOPE -
        so before this badge a row could be filtered in and still have nothing on it that
        agreed. Discovery is a member of the in-guide group Fox with no row of its own."""
        self.assertTrue(self.obs['guide_via_row_found'])
        self.assertEqual(self.obs['guide_via_badges'], ['In guide via Fox'])

    def test_the_badge_is_in_the_name_cell_and_nowhere_else(self):
        """It carries a group NAME, so it is variable-width. Every cell except the name cell
        is `overflow: hidden; white-space: nowrap` over a track sized without regard to a
        group's name, so a long name renders there clipped mid-word with no ellipsis - a
        defect jsdom computes no layout for and therefore cannot fail on. `.a-namecell` is
        the flex row that already holds DUP, KEPT and the why-chip and truncates only the
        name itself."""
        self.assertTrue(self.obs['guide_via_in_name_cell'])
        self.assertFalse(self.obs['guide_via_outside_name_cell'])

    def test_the_badge_opens_the_group_it_names(self):
        """dev/changelog/860. The badge's whole content is a group's name, so clicking it
        goes to that group rather than to the channel the row is about - and it is a real
        `<a href>`, which is also what stops the row's own open-the-channel handler firing
        underneath it (the delegated handler exempts `a, button, input, label`)."""
        self.assertRegex(self.obs['guide_via_href'], r'^/channel-groups/\d+$')
        self.assertIn('Fox', self.obs['guide_via_tip'])
        self.assertIn('row of its own', self.obs['guide_via_tip'])

    def test_the_account_cell_opens_the_account(self):
        """Same rule as the badge above: a provider's name on a row is an address."""
        self.assertRegex(self.obs['account_cell_href'], r'^/accounts/\d+$')

    def test_a_channel_in_no_in_guide_group_does_not_get_the_badge(self):
        """The other half of the invariant - the badge has to be ABSENT where it does not
        apply, or it says nothing at all. Test Card is in no group."""
        self.assertNotIn('In guide via Fox', self.obs['plain_row_badges'])


class PinnedNameColumnTests(_PageJs, unittest.TestCase):
    """The pinned Channel column's frozen width (dev/docs/BUGS.md 2026-08-20 09:12).

    The header and every row are separate grid containers, so a content-sized track
    resolves differently in each one; the page measures the column once and writes a
    single px value they all share. The defect was in WHAT it measured - it read the
    resolved track back, which reports the column's own min-width, so a cell whose
    content had outgrown the column measured exactly as wide as one that had not, and
    the badges in it were clipped with no way for anything to notice.
    """

    SCENARIO = 'name_col'

    def test_no_layout_engine_means_no_frozen_width(self):
        """The measurement value stays in place rather than a meaningless number being
        frozen from a DOM that computes no geometry - production behavior under jsdom,
        and the state every other scenario in the file runs in."""
        self.assertTrue(self.obs['track_unmeasured'].startswith('fit-content('),
                        self.obs['track_unmeasured'])

    def test_the_column_freezes_to_what_the_cell_actually_holds(self):
        """310px of content in a column whose min-width is 190px: the frozen value has
        to follow the content, or the cell is clipped for the life of the page."""
        self.assertEqual(self.obs['track_measured'], '310px')

    def test_the_cap_still_wins_over_a_very_wide_cell(self):
        """The cap is what stops this column pushing the table into a horizontal scroll
        (dev/changelog/414), so it outranks the measurement rather than the other way
        round."""
        self.assertEqual(self.obs['track_clamped'], '360px')


class SavedSearchTests(_PageJs, unittest.TestCase):
    """A saved search IS its URL - terms, scope fields, match mode, standing options and
    filters, in one query string (DESIGN-channel-search.md 3.1)."""

    SCENARIO = 'saved'

    def test_the_panel_lists_what_is_stored_and_badges_the_default(self):
        self.assertEqual([n.split(' ')[0] for n in self.obs['listed_names']], ['Needs', 'Duplicate'])
        self.assertEqual(self.obs['default_badges'], 1)

    def test_set_default_is_a_toggle(self):
        """Unlike the mockup: a page that could only ever be given a new opening search
        and never none has no way back to a bare /channels."""
        self.assertEqual(self.obs['default_button_labels'], ['set default', 'clear default'])

    def test_loading_one_applies_its_filters_and_its_standing_options(self):
        """The case the rule exists for: you cannot clean up duplicates while a standing
        option is hiding them, so the saved search carries `showdup`."""
        self.assertEqual(self.obs['loaded_filters'], ['dupurl'])
        self.assertIn('showdup', self.obs['loaded_standing'])

    def test_loading_closes_the_panel_and_names_what_is_being_shown(self):
        self.assertFalse(self.obs['panel_open_after_load'])
        self.assertEqual(self.obs['current_name_after_load'], 'Duplicate cleanup')
        self.assertEqual(self.obs['dirty_after_load'], 'none')

    def test_edited_is_derived_and_goes_away_again_when_the_edit_is_undone(self):
        """Not a `savedDirty` flag set by every handler - that is one missed call site
        away from a saved search quietly reporting itself unedited."""
        self.assertNotEqual(self.obs['dirty_after_editing'], 'none')
        self.assertEqual(self.obs['name_after_editing'], 'Duplicate cleanup')
        self.assertEqual(self.obs['dirty_after_undoing_the_edit'], 'none')

    def test_paging_through_a_saved_search_is_not_an_edit(self):
        self.assertTrue(self.obs['pager_has_next'], 'the pager fixture has no next page')
        self.assertEqual(self.obs['page_after_paging'], ['2'])
        self.assertEqual(self.obs['dirty_after_paging'], 'none')

    def test_saving_stores_the_whole_search_and_nothing_transient(self):
        self.assertIn('/api/user-prefs/channel_search_saved', self.obs['save_post_url'])
        self.assertEqual(self.obs['saved_names'],
                         ['Needs a check', 'Duplicate cleanup', 'My new search'])
        self.assertIn('showdup', self.obs['saved_record_standing'])
        self.assertEqual(self.obs['saved_record_filters'], ['dupurl'])
        # `facets` says which counts one REQUEST asked for, not what the search is.
        self.assertFalse(self.obs['saved_record_has_facets'])

    def test_the_name_box_prefills_with_the_loaded_search(self):
        self.assertEqual(self.obs['name_box_prefill'], 'Duplicate cleanup')

    def test_the_panel_stays_open_after_saving_and_after_deleting(self):
        self.assertTrue(self.obs['panel_open_after_save'])
        self.assertTrue(self.obs['panel_open_after_delete'])

    def test_setting_a_default_moves_it_and_clearing_it_leaves_none(self):
        self.assertEqual(self.obs['defaults_after_set'], [False, False, True])
        self.assertEqual(self.obs['defaults_after_clear'], [False, False, False])

    def test_deleting_removes_the_one_that_was_clicked(self):
        self.assertEqual(self.obs['names_after_delete'], ['Duplicate cleanup', 'My new search'])

    def test_clear_all_drops_the_claim_to_be_looking_at_a_saved_search(self):
        """A bare URL means "no search stated", which is also what the route's default
        redirect answers - so the page must stop naming a search it is no longer
        showing."""
        self.assertEqual(self.obs['current_name_after_clear_all'], '')
        self.assertEqual(self.obs['dirty_after_clear_all'], 'none')


class ActionContextTests(_PageJs, unittest.TestCase):
    """"You came here to add channels to group X" is an ERRAND, not a filter
    (DESIGN-channel-search.md 3). It is the one piece of pre-applied state that stays
    fixed until dismissed, because the user arrived specifically to do it."""

    SCENARIO = 'context'

    def test_the_context_is_up_with_nothing_selected(self):
        """group-modal's fixedGroup mode still offers suggestions at zero selected, so
        the bar has work to do before anything is ticked."""
        self.assertTrue(self.obs['context_shown'])
        self.assertTrue(self.obs['bar_shown_at_zero_selected'])

    def test_the_group_name_is_resolved_from_the_id_through_the_catalog(self):
        """`group_name` is not part of the URL contract - a display name from a URL is
        text the page would be trusting from whoever built the link."""
        self.assertIn(self.obs['group_name_from_catalog'], self.obs['context_label'])

    def test_the_context_never_becomes_a_chip(self):
        """It travels in the URL so a reload keeps the errand, but it is not a filter:
        no chip in the well, and no `f.` parameter, so it narrows nothing."""
        self.assertNotIn('cs-chip', self.obs['well_html'])
        self.assertEqual(self.obs['add_to_group_in_search_request'],
                         [self.obs['group_id_from_catalog']])
        self.assertFalse([k for k in self.obs['search_params'] if k.startswith('f.')])

    def test_dismissing_it_leaves_the_search_alone(self):
        self.assertFalse(self.obs['context_after_dismiss'])
        self.assertNotIn('add_to_group', self.obs['address_bar_after_dismiss'])


class BulkGuideActionTests(_PageJs, unittest.TestCase):
    """The bulk guide action reflects what is actually selected (dev/changelog/792).

    It used to read "+ Add selected to guide" for every selection, including one made
    entirely of channels that already had their own guide row - an offer to do nothing.
    The shipped shape: ONE action that becomes Add or Remove, and disables on a
    mixed selection while saying why. Two always-visible buttons and an Add that quietly
    applied to only half the selection were both rejected.
    """

    SCENARIO = 'guide_action'

    def test_the_fixture_holds_both_kinds_of_channel(self):
        """Without one of each, every assertion below would pass against a selection
        that could never have been mixed."""
        self.assertTrue(self.obs['fixture_has_both'])

    def test_a_selection_of_channels_not_in_the_guide_offers_add(self):
        self.assertEqual(self.obs['only_not_in_guide']['label'],
                         '+ Add selected to guide (1)')
        self.assertFalse(self.obs['only_not_in_guide']['disabled'])
        self.assertEqual(self.obs['only_not_in_guide']['note'], '')

    def test_a_selection_already_in_the_guide_offers_remove(self):
        """Remove, not Delete: the channel keeps everything except its guide row."""
        self.assertEqual(self.obs['only_in_guide']['label'],
                         'Remove selected from guide (1)')
        self.assertFalse(self.obs['only_in_guide']['disabled'])
        self.assertEqual(self.obs['only_in_guide']['note'], '')

    def test_a_mixed_selection_disables_the_action_and_says_why(self):
        """Going inert with no explanation was rejected in the same ruling - a disabled
        control that does not say why is a dead end."""
        self.assertTrue(self.obs['mixed']['disabled'])
        note = self.obs['mixed']['note']
        self.assertIn('already have their own guide row', note)
        self.assertIn('all in, or all out', note)

    def test_the_note_is_empty_in_every_state_that_is_not_mixed(self):
        """Server-rendered initial state must equal the "nothing active" state: the note
        is empty markup and JS only ever fills it in."""
        self.assertEqual(self.obs['nothing_selected']['note'], '')
        self.assertFalse(self.obs['nothing_selected']['disabled'])

    def test_removing_posts_the_whole_selection_in_one_request(self):
        """Unlike adding, removal asks no per-channel question, so a request per channel
        would buy nothing and cost a transaction and a hide recompute each."""
        self.assertIn('/api/guide/channels/remove', self.obs['remove_post_url'])
        self.assertEqual(self.obs['remove_post_count'], 1)
        self.assertEqual(len(self.obs['remove_post_body']['channel_ids']), 1)
        self.assertTrue(any('removed from your guide' in t
                            for t in self.obs['toasts_after_remove']))

    def test_the_selection_survives_a_bulk_remove(self):
        """The rows stay ticked and the button flips back to Add, so undoing an
        accidental bulk remove is one click."""
        self.assertTrue(self.obs['selection_survives_remove'])

    def test_no_row_carries_a_guide_control_at_this_width(self):
        """dev/changelog/860: the checkbox plus this bar is the ONE path to the guide from
        the desktop list. A channel row's "+ Add to Guide" was the widest thing on the row
        and stood next to a checkbox that already did the same job through the bar; a group
        row's was worse than redundant, because whether a group can fill a guide row depends
        on its participation switches and its format lock. Group rows have to actually be on
        screen for the second count to mean anything."""
        self.assertEqual(self.obs['row_guide_controls'], 0)
        self.assertEqual(self.obs['group_guide_controls'], 0)
        self.assertGreater(self.obs['group_rows_on_screen'], 0)


class MobilePageTests(_PageJs, unittest.TestCase):
    """The SAME page at 375, which is the whole point: one template, one module, one
    state model, and only the drawing differs. Approved design:
    dev/mockups/22-channel-search-mobile.html (dev/changelog/392-394).

    jsdom computes no layout, so nothing here is geometric - the harness's matchMedia
    stub decides which RENDERING runs, never how anything measures."""

    SCENARIO = 'mobile'

    def test_the_rail_is_not_built_at_all(self):
        """Not display:none'd: on a phone the rail and + Filter are the same thing, so
        a populated-but-hidden rail would be a second, invisible copy of every filter
        control sitting in the accessibility tree."""
        self.assertEqual(self.obs['rail_html'], '')

    def test_the_list_is_cards_and_not_table_rows(self):
        self.assertEqual(self.obs['table_rows'], 0)
        self.assertGreater(self.obs['card_count'], 0)

    def test_the_chip_row_carries_every_entry_point(self):
        """The chip row REPLACES the toolbar at this width - Filters, Searching in,
        Sort and Saved are all in it, because two live copies of one control is how
        the two come to disagree."""
        self.assertEqual(self.obs['chips'], ['scope', 'sort', 'saved'])

    def test_the_sort_chip_names_the_field_and_the_direction(self):
        """The sortable column headers go away with the table, so this chip is the
        only thing left saying what the list is ordered by (DESIGN.md 9.4)."""
        self.assertIn('Sort:', self.obs['sort_chip_text'])
        # The channel grain's default sort, which is `name` -> "Channel"
        # (dev/changelog/699). The chip names whatever the list is actually ordered by.
        self.assertIn('Channel', self.obs['sort_chip_text'])

    def test_every_card_value_carries_a_label(self):
        """Round 3's whole reason: round 1 put five values on one unlabeled mono line
        and it was impossible to tell which was which. Every value sits on two fixed
        tracks now, so the labels line up down the list."""
        labels, values = self.obs['first_card_label_value_pairs']
        self.assertEqual(labels, values)
        self.assertGreater(labels, 0)
        self.assertTrue(all(self.obs['first_card_labels']),
                        'a label track rendered empty, which is the unlabeled card again')

    def test_the_card_has_a_kebab_and_no_per_row_add_button(self):
        """Round 2: the bulk path is the checkbox plus the bottom bar whether
        the selection is one channel or fifty, and the single-channel actions moved
        behind the kebab."""
        self.assertTrue(self.obs['every_card_has_a_kebab'])
        self.assertEqual(self.obs['no_per_card_add_button'], 0)

    def test_the_selection_bar_uses_short_labels_so_all_three_stay_buttons(self):
        """The labels were what did not fit at 375, not the actions - which is why
        there is no overflow kebab here and nothing is behind a second tap."""
        for label in self.obs['sel_bar_labels']:
            self.assertLessEqual(len(label), 14, f'{label!r} is the desktop spelling')
        self.assertTrue(self.obs['body_has_selbar_class'],
                        'the fixed bar would cover the pager without this')

    def test_the_columns_button_becomes_the_fields_sheet(self):
        """One button, two things. A second button would be a control that exists at
        one width only and confuses the other."""
        self.assertIn('Fields', self.obs['cols_btn_label'])
        self.assertNotIn('Columns', self.obs['cols_btn_label'])

    def test_the_fields_sheet_offers_the_same_nine_fields_as_the_columns_picker(self):
        """One list for both widths, so the two agree about what a channel is made of
        and disagree only about how it is drawn.

        `status` is in neither list since dev/changelog/860: the badges it stood for are
        drawn unconditionally now, on the card's badge line and in the table's name cell,
        alongside DUP and KEPT which have never been switchable either."""
        self.assertEqual(len(self.obs['fields_sheet_keys']), 9)
        self.assertIn('health', self.obs['fields_sheet_keys'])
        self.assertNotIn('status', self.obs['fields_sheet_keys'])

    def test_hiding_a_field_redraws_and_does_not_re_run_the_search(self):
        """Visibility only: it changes what is DRAWN and nothing about the query, so
        the rows already in hand are redrawn. The only request is the pref write, and
        it is the CARD pref row rather than the desktop column setup - a phone and a
        desktop legitimately want different things."""
        self.assertNotIn('Health', self.obs['labels_after_hiding_health'])
        self.assertEqual(self.obs['requests_after_hiding_a_field'],
                         ['POST /api/user-prefs/channel_search_card_fields_v2'])


class MobileSheetTests(_PageJs, unittest.TestCase):
    """Every sheet, driven. A sheet here is a buildModal() panel, which style.css's own
    <=768px block turns into a bottom sheet (DESIGN.md 9.6) - there is deliberately no
    second overlay component on this page."""

    SCENARIO = 'mobile_sheets'

    def test_the_filters_sheet_is_the_rails_replacement_and_drills_two_levels(self):
        """One sheet does both jobs the desktop splits between a rail and a popover,
        and it takes the POPOVER's shape: a list of dimensions, then that dimension's
        values, with a way back. Six expanded facets in an 86vh sheet is ~1250px of
        scrolling to reach the last one."""
        self.assertIn('acct', self.obs['filters_dims'])
        self.assertIn('Filters', self.obs['filters_title'])
        self.assertTrue(self.obs['after_drill_has_back'])
        self.assertGreater(self.obs['after_drill_value_rows'], 0)
        self.assertEqual(self.obs['after_back_dims'], self.obs['filters_dims'])

    def test_the_standing_options_are_the_sheets_footer_not_a_card_of_their_own(self):
        """Round 7's rail order re-expressed: what you touch every search comes first,
        what you set once comes last. They are on the sheet's TOP level, so they are
        reachable without drilling into a dimension."""
        self.assertTrue(self.obs['filters_standing_at_top_level'])
        self.assertIn('showdup', self.obs['filters_standing_at_top_level'])

    def test_a_standing_option_toggled_from_the_sheet_reaches_the_url(self):
        """They live in the URL and nowhere else (DESIGN-channel-search.md 5), so a
        toggle that only moved a local flag would give the address bar and the screen
        two different result sets."""
        self.assertIn('showdup', self.obs['standing_after_toggle'])

    def test_the_three_state_survives_into_the_sheet(self):
        """Excluding a value is what the one-tap suggestion row down here deliberately
        cannot do, so this is the only place at phone width that can say "is not"."""
        self.assertGreater(self.obs['after_drill_tri_pairs'], 0)
        self.assertTrue([k for k in self.obs['request_after_exclude'] if k.startswith('x.')],
                        'excluding from the sheet did not reach the request')

    def test_the_sheet_stays_open_while_you_pick_values(self):
        """It applies live, which is why it carries its own match count - a sheet that
        shut on every pick would make a two-value filter two round trips."""
        self.assertTrue(self.obs['sheet_still_open_after_picking'])
        self.assertTrue(self.obs['chip_count_after_exclude'])

    def test_tapping_an_open_sheets_own_chip_closes_it(self):
        """So a chip is never a no-op. A CHIP, specifically: the chip row stays above the
        sheet, so a second tap reaches it. The Filters sheet's entry point moved into the
        well in dev/changelog/811 and is in the page body, under the open sheet - a browser
        pass confirmed a tap at those coordinates reaches the sheet's own content, so that
        one closes by its X (below) rather than by a toggle that could never fire."""
        self.assertTrue(self.obs['sheet_closed_by_its_own_chip'])

    def test_the_filters_sheet_closes_by_its_own_x(self):
        self.assertTrue(self.obs['filters_sheet_closed_by_its_x'])

    def test_the_scope_sheet_carries_the_fields_and_the_match_mode(self):
        """The desktop popover's two panes split by FUNCTION rather than stacking:
        stacking would push every suggestion below the fold on each keystroke. The
        all-words/any-word segment comes with the scope because it is a property of
        the search in exactly the same way."""
        self.assertIn('name', self.obs['scope_fields'])
        self.assertIn('epg-desc', self.obs['scope_fields'])
        self.assertEqual(sorted(self.obs['scope_has_match_segment']), ['all', 'any'])
        self.assertIn('epg-desc', self.obs['fields_after_tick'])
        self.assertEqual(self.obs['match_after_any'], ['any'])

    def test_ticking_a_scope_switch_does_not_put_the_caret_back_in_the_box(self):
        """The refocus is the other half of the desktop popover's blur guard and has
        no counterpart in a sheet, which is not focus-dependent - and on a phone it
        would raise the keyboard over the sheet being used."""
        self.assertFalse(self.obs['box_focused_after_tick'])

    def test_the_sort_sheet_offers_only_the_registrys_own_keys(self):
        """Which is what keeps the four columns that look sortable and are not - Now
        airing, Status, Groups, Tags - off it for free
        (DESIGN-channel-search.md 7)."""
        for absent in ('airing', 'status', 'groups', 'tags'):
            self.assertNotIn(absent, self.obs['sort_options'])
        self.assertIn('name', self.obs['sort_options'])

    def test_re_picking_the_active_sort_field_reverses_it(self):
        """One rule for both surfaces: re-picking flips, a different field starts
        ascending, exactly as clicking a column header does."""
        self.assertEqual(self.obs['sort_after_pick'], ['category'])
        self.assertEqual(self.obs['sort_after_repick'], ['-category'])
        self.assertEqual(self.obs['sort_after_switching_field'], ['health'],
                         'a new field inherited the previous one\'s direction')

    def test_the_suggestion_menu_is_one_pane_with_a_scope_footer(self):
        """The scope pane became a sheet, so this footer is the only thing left
        linking what you are typing to what it matches."""
        self.assertTrue(self.obs['sugg_open'])
        self.assertFalse(self.obs['sugg_has_scope_pane'])
        self.assertTrue(self.obs['sugg_has_scope_footer'])

    def test_a_suggestion_row_is_one_tap_and_carries_no_three_state(self):
        """A +/- pair on every row leaves about 150px for the label at 375. Excluding
        is the -word syntax the footer teaches, or the Filters sheet, where the row is
        full width."""
        self.assertEqual(self.obs['sugg_tri_buttons'], 0)

    def test_opening_a_sheet_closes_the_suggestion_menu(self):
        """dev/changelog/394: #sugg is not a `.menu`, so nothing else closes it, and a
        sheet opening in front of it handed back a page still covered in stale
        suggestions when that sheet was dismissed."""
        self.assertFalse(self.obs['sugg_open_after_a_sheet_opened'])

    def test_the_row_sheet_holds_every_single_channel_action(self):
        self.assertIn('open-channel', self.obs['row_sheet_actions'])
        self.assertTrue(set(self.obs['row_sheet_actions']) & {'add-guide', 'in-guide'})
        self.assertTrue(self.obs['row_sheet_has_test'])
        self.assertTrue(self.obs['row_sheet_has_select'])

    def test_the_sheet_is_where_the_single_channel_guide_remove_now_lives(self):
        """dev/changelog/860 took the guide control off every desktop row. The phone kebab
        is not that control - it is behind a tap, not sitting on all 136,130 rows - so it
        keeps it, and it is the last caller of the single-channel remove path. That path
        used to synthesize a form POST to /channels/<id>/toggle and reload the whole page,
        which threw away the selection and the scroll position."""
        self.assertTrue(self.obs['sheet_remove_offered'])
        self.assertIn('/api/guide/channels/remove', self.obs['sheet_remove_post_url'])
        self.assertEqual(len(self.obs['sheet_remove_post_body']['channel_ids']), 1)

    def test_selecting_from_the_row_sheet_writes_the_same_selection_as_the_checkbox(self):
        """One writer, so the card's tick, the sheet's row and the bottom bar cannot
        disagree about what is selected."""
        self.assertTrue(self.obs['card_ticked_after_selecting_in_the_sheet'])
        self.assertTrue(self.obs['row_sheet_says_deselect_now'])

    def test_the_dup_badge_opens_a_sheet_rather_than_drilling_straight_in(self):
        """Touch has no hover, and the hover tooltip is what said what the cluster was
        - DESIGN.md 13.1 forbids information reachable only that way. So the sheet says
        it, and the drill-in is a button inside it."""
        self.assertTrue(self.obs['card_badge_opened_a_sheet'])
        self.assertTrue(self.obs['card_badge_did_not_drill_in'],
                        'the card badge drilled straight in, so the cluster was never explained')
        self.assertEqual(self.obs['dup_sheet_title'], 'Duplicated stream URL')
        self.assertTrue(self.obs['dup_sheet_lists_others'])
        self.assertTrue(self.obs['dup_sheet_says_kept_reason'])
        self.assertTrue(self.obs['dup_sheet_has_drill_button'])

    def test_the_drill_in_is_by_channel_id_and_turns_hide_duplicates_off(self):
        """The row payload MASKS stream URLs, so searching the text on screen matches
        nothing - `dup.ids` is the whole cluster. And Show duplicates has to come on,
        or the copies it just went to look at are the very rows that option hides."""
        params = self.obs['after_drill_params']
        self.assertEqual(sorted(params.get('f.chan', [])), sorted(self.obs['dup_cluster_ids']))
        self.assertNotIn('dup', params.get('standing', []))
        self.assertTrue(self.obs['dup_sheet_closed_after_drill'])


class AiringGrainTests(_PageJs, unittest.TestCase):
    """The second grain, drawn (dev/changelog/414).

    The engine has answered `grain=airings` since dev/changelog/412; these assert that the
    PAGE renders it - the toggle, the airing registry's columns, the per-showing Record
    button, and what a flip does to state that cannot cross.
    """

    SCENARIO = 'airing'

    def test_the_toggle_names_both_grains_and_starts_on_channels(self):
        """The pills name the two MODES, in the approved wording: `Search Channels`
        and `Search Programs (EPG)` (dev/changelog/807). Both are title case and both carry
        a pictorial glyph, which DESIGN.md §4 otherwise forbids and now carries two
        page-scoped exceptions for - the rule governs controls that ACT, and a pill naming a
        mode is a label rather than a verb (dev/changelog/809, 810). The catalog's own `tab`
        and `label` strings are still what PROSE elsewhere on the page uses."""
        self.assertEqual(self.obs['tabs'],
                         ['📺Search Channels', '📅Search Programs (EPG)'])
        self.assertEqual(self.obs['tab_active_at_boot'], '📺Search Channels')

    def test_the_default_grain_is_absent_from_the_url(self):
        """So every link already stored against this page opens the search it named -
        the channel grain's URLs are byte-identical to what they were before `grain`
        existed."""
        self.assertTrue(self.obs['grain_absent_from_default_request'])

    def test_flipping_carries_the_text_the_scope_and_the_match_mode(self):
        """Only the RESULT SHAPE changes. Everything about what is being searched is
        shared verbatim, which is the whole reason this is a toggle on one page."""
        self.assertEqual(self.obs['grain_after_flip'], 'airings')
        self.assertEqual(self.obs['tab_active_after_flip'], '📅Search Programs (EPG)')
        self.assertEqual(self.obs['q_after_flip'], self.obs['q_before_flip'])
        self.assertTrue(self.obs['fields_after_flip'])

    def test_flipping_applies_the_target_grains_standing_defaults(self):
        """The two airings-only hiders are on by default. Arriving with them off would
        list every showing since the EPG began without ever saying so.

        The two are asserted in OPPOSITE directions because the inversion did not touch
        `grpdedup` (dev/changelog/778): it is a mode, so present still means it collapses,
        while `showpast` is a `show*` key and hides the past by being ABSENT."""
        standing = self.obs['standing_after_flip']
        self.assertNotIn('showpast', standing)
        self.assertIn('grpdedup', standing)

    def test_flipping_to_airings_lands_on_the_airing_grains_own_default_sort(self):
        """The channel grain boots on `category`, which IS a valid airing sort - so it used
        to follow the flip and `when` (soonest first, this grain's default) was unreachable
        by clicking the tab. It is not a cosmetic difference: on the production database the
        default airings page costs 1.67s sorted by category against 0.11s sorted by when,
        because only `when` can walk ix_epg_entries_start_stop and stop at 100 rows
        (dev/changelog/692)."""
        self.assertEqual(self.obs['sort_after_flip'], 'when')

    def test_a_sort_the_user_chose_still_crosses_the_flip(self):
        """The half that must NOT change: `health` means the same thing on both grains, so
        a chosen sort carries. Only a sort nobody picked gets replaced."""
        self.assertEqual(self.obs['sort_chosen_on_channels'], 'health')
        self.assertEqual(self.obs['sort_after_flip_when_chosen'], 'health')

    def test_a_sort_that_cannot_cross_is_remapped_and_the_page_says_so(self):
        """`when` orders programs and means nothing for a row that is a channel. The
        ENGINE stays strict (a cross-grain sort is still a 400); the page never generates
        one, and discloses the fallback rather than silently reordering the list."""
        # `-when`, not `when`: the flip now lands on this grain's default, so clicking the
        # When header re-picks the ACTIVE sort and flips its direction. The direction is
        # beside the point here - what matters is that the key cannot cross back.
        self.assertEqual(self.obs['sort_before_back'].lstrip('-'), 'when')
        self.assertEqual(self.obs['sort_after_back'], 'name')
        self.assertIn('sorted by', self.obs['count_line_after_back'])

    def test_a_default_sort_that_cannot_cross_is_remapped_without_a_note(self):
        """The other side of the rule above, and the reason it needed restating.

        The channel grain's default is `name` (dev/changelog/699), and no airing can be
        ordered by a channel's name - so a plain "Guide (EPG)" click now takes the same
        "cannot cross" branch that the When-sorted flip above takes. The remap is right; the
        NOTE is not. It would explain a fallback from a sort the user never picked, on every
        flip, forever. Only a sort somebody actually chose is worth a sentence.
        """
        self.assertEqual(self.obs['sort_after_flip'], 'when')
        self.assertNotIn('sorted by', self.obs['count_line_after_flip'])

    def test_the_header_is_the_airing_registry_with_the_program_pinned_first(self):
        """The thing the row IS goes first and is not in the picker - Program here,
        exactly as Channel is on the other grain."""
        labels = self.obs['head_labels']
        self.assertIn('Program', labels)
        self.assertIn('When', labels)
        self.assertIn('Channel', labels)
        self.assertNotIn('Now airing', labels, 'that is a CHANNEL column')
        self.assertIn('Channel', self.obs['head_labels_after_back'])
        self.assertNotIn('Program', self.obs['head_labels_after_back'])

    def test_only_the_airing_registrys_sorts_get_a_sortable_header(self):
        """Anything not in the grain's SORTS renders unsortable rather than guessing -
        paging is server-side, so a sort that cannot be expressed in SQL cannot be
        honoured at all."""
        offered = set(self.obs['sortable_heads'])
        allowed = set(_observations()['catalog_by_grain']['airings']['sorts'])
        self.assertTrue(offered)
        self.assertTrue(offered <= allowed, f'{offered - allowed} are not airing sorts')

    def test_every_row_is_an_airing_row_carrying_a_time(self):
        self.assertTrue(self.obs['row_count'])
        self.assertTrue(self.obs['rows_are_airing_rows'])
        self.assertTrue(self.obs['first_row_has_when'])
        self.assertTrue(self.obs['first_row_title'])

    def test_the_checkbox_carries_both_ids(self):
        """A selection is always a set of CHANNELS, on both grains - but the row is a
        showing, so both ids have to be on the element."""
        self.assertTrue(self.obs['checkbox_has_channel_id'])
        self.assertTrue(self.obs['checkbox_has_airing_id'])

    def test_selecting_showings_selects_their_channels_deduped(self):
        """Several showings on one channel are ONE selection. A count that reported the
        number of ROWS ticked would be the lie the count line exists to prevent."""
        n = self.obs['distinct_channels_in_rows']
        self.assertIn(f'({n})', self.obs['selected_count_text'])

    def test_the_record_button_is_per_showing_and_draws_every_state(self):
        """The same channel can have one showing scheduled and one neither, so a button
        reading a channel-level flag would say the same wrong thing on all its rows."""
        states = self.obs['record_states']
        buttons = self.obs['record_buttons']
        self.assertIn('scheduled', states)
        self.assertIn('past', states)
        labels = [b['label'] for b in buttons]
        self.assertIn('Record', labels)
        self.assertIn('Edit recording', labels)
        self.assertIn('Ended', labels)
        ended = next(b for b in buttons if b['label'] == 'Ended')
        self.assertTrue(ended['disabled'],
                        'an ended showing must be a disabled control that says why, not an '
                        'enabled one that fails on click')

    def test_the_record_click_fetches_its_context_rather_than_using_the_masked_row(self):
        """The row payload masks the stream URL and carries no profile or group id, so
        the modal is opened from a per-click fetch - never from the search response. A
        masked URL reaching the modal would schedule a recording of a URL that does not
        exist."""
        self.assertTrue(self.obs['record_click_fetched_context'])
        self.assertEqual(self.obs['record_click_opened_modal'], 1)
        self.assertTrue(self.obs['record_modal_got_raw_url'])


class WhenDimensionTests(_PageJs, unittest.TestCase):
    """`when`: three fixed values from the catalog, two the page spells itself."""

    SCENARIO = 'when'

    def test_the_grain_is_read_from_the_url(self):
        self.assertEqual(self.obs['grain_from_url'], 'airings')

    def test_the_rail_leads_with_the_dimension_that_exists_only_on_this_grain(self):
        """A dimension that exists only on the grain you just entered is the reason you
        entered it, so it leads rather than trailing (mockup 25, P9)."""
        self.assertEqual(self.obs['rail_first_facet'], 'when')
        self.assertEqual(self.obs['rail_facets'],
                         _observations()['catalog_by_grain']['airings']['dimensions'][:len(
                             self.obs['rail_facets'])])

    def test_the_three_fixed_values_come_from_the_catalog(self):
        """The page must not re-type a registry. The two parametrized values are
        deliberately absent from the catalog - there are infinitely many of them."""
        served = [w['value'] for w in _observations()['catalog_when_values']]
        self.assertEqual(self.obs['when_values'][:len(served)], served)

    def test_both_fill_in_controls_are_drawn_and_start_inactive(self):
        """Until a control holds a value its row counts n/a and cannot be included or
        excluded - an empty control is not a window."""
        self.assertEqual(self.obs['when_fill_controls'], 2)
        self.assertTrue(self.obs['rel_row_off_before_typing'])

    def test_typing_a_number_spells_one_next_value(self):
        self.assertEqual(self.obs['when_after_rel'], ['next:3:hours'])

    def test_the_number_box_keeps_the_caret_across_the_rail_rebuild(self):
        """Applying a filter rewrites the whole rail, and this control is inside it. Without
        the focus restore the box loses the caret after ONE digit, which makes any window
        of more than nine units unreachable."""
        self.assertTrue(self.obs['rel_box_kept_focus'])
        self.assertEqual(self.obs['rel_box_value_after_apply'], '3')

    def test_retyping_replaces_the_window_rather_than_adding_a_second(self):
        """There is one "Next ..." window. Leaving the old one behind would mean typing a
        new number silently widened the filter instead of changing it."""
        self.assertEqual(self.obs['when_after_retype'], ['next:5:hours'])

    def test_an_unreadable_caret_is_restored_to_the_END_not_the_front(self):
        """Chrome raises InvalidStateError on `selectionStart` for `type=number`, so the
        caret cannot be read across the rail rebuild. Defaulting it to 0 puts the cursor
        at the FRONT, and the next digit lands before the previous one - typing "36" gives
        "63" and applies a 63-hour window. Measured in a real browser (dev/changelog/414);
        this scenario makes the getter throw, because jsdom implements selectionStart on a
        number input where Chrome does not and would otherwise never reach that branch."""
        self.assertTrue(self.obs['caret_box_still_focused'])
        self.assertIsNotNone(self.obs['caret_set_at'], 'the caret was never restored at all')
        self.assertEqual(self.obs['caret_set_at'], self.obs['caret_set_on_value_len'],
                         'the caret was put somewhere other than the end, so the next digit '
                         'typed lands before the ones already there')

    def test_a_filter_the_other_grain_cannot_express_is_parked_not_dropped(self):
        """It leaves the REQUEST but stays on SCREEN, struck through. Hiding it while the
        URL still carried it is the silent behavior this project forbids; dropping it is
        the defect found in round 1."""
        self.assertEqual(self.obs['when_sent_on_channels'], [])
        self.assertTrue(self.obs['parked_chip_visible'])
        self.assertIn('When', self.obs['parked_chip_text'])

    def test_parking_is_not_announced_because_it_is_visible(self):
        """A state you can see does not have to be narrated, and two toasts on every flip
        is a lot of talking about something you did on purpose (round 4)."""
        self.assertEqual([t for t in self.obs['no_toast_on_flip'] if 'Switched' in t], [])

    def test_flipping_back_restores_both_the_chip_and_its_control(self):
        """Restoring only the chip would leave a filter you can see and cannot edit."""
        self.assertEqual(self.obs['when_after_flip_back'], ['next:5:hours'])
        self.assertTrue(self.obs['parked_chip_gone'])
        self.assertEqual(self.obs['rel_control_value'], '5')


class CountsSplitTests(_PageJs, unittest.TestCase):
    """The airing grain's row/counts split (dev/changelog/598): rows must never wait on
    the standing-breakdown total, and nothing on the page may claim a wrong number - a
    fabricated zero, "No matches", "Page 1 of 1" - while the real one is still pending.

    `holdCounts` parks the trailing `GET /api/channels/search/counts` request alone, so
    the pending window is an observable state here rather than something that exists for
    one microtask under the harness's synchronous stub.
    """

    SCENARIO = 'counts_split'

    def test_the_row_request_asks_to_skip_the_breakdown(self):
        self.assertTrue(self.obs['row_request_asked_to_skip_counts'])

    def test_a_separate_counts_request_is_made(self):
        self.assertTrue(self.obs['counts_request_made'])

    def test_the_counts_request_was_genuinely_held_back(self):
        """Vacuous otherwise: if nothing was actually parked, every "while pending"
        assertion below would trivially pass because counts already landed."""
        self.assertEqual(self.obs['counts_held_count'], 1)

    def test_rows_render_while_the_count_is_still_pending(self):
        self.assertGreater(self.obs['rows_shown_while_counts_pending'], 0)
        self.assertFalse(self.obs['empty_shown_while_pending'])

    def test_the_header_shows_a_real_partial_count_not_a_wrong_one(self):
        """Product Principle 1: a number the user cannot explain is worse than no number.
        The rows already on screen are a real, honest lower bound - "3+ airings" - never
        blank-as-if-nothing-loaded and never a bare "3 airings" claiming the exact total
        is already known."""
        text = self.obs['head_count_while_pending']
        self.assertIn('+', text)
        self.assertIn('airing', text)

    def test_the_pager_stays_empty_rather_than_claiming_one_page(self):
        self.assertEqual(self.obs['pager_html_while_pending'], '')

    def test_the_count_line_says_counting_rather_than_no_matches(self):
        line = self.obs['count_line_while_pending']
        self.assertNotIn('No</strong> matches', line)
        self.assertIn('counting', line)

    def test_the_in_box_count_stays_blank_rather_than_printing_zero(self):
        self.assertEqual(self.obs['in_box_count_while_pending'], '')

    def test_once_released_the_real_total_replaces_the_partial_one(self):
        self.assertNotIn('+', self.obs['head_count_after'])
        self.assertIn('airing', self.obs['head_count_after'])
        self.assertNotIn('counting', self.obs['count_line_after'])


class RequestIdentityTests(_PageJs, unittest.TestCase):
    """The page tells the server which of its own requests each one is.

    An abort is invisible to a WSGI server - Werkzeug runs the handler thread to completion
    and notices the dead peer only when it writes - so during a degraded window the single
    scan slot was being spent, in full, on searches the page had already thrown away.
    Measured live with an index rebuild running: three searches 0.25s apart, and the FIRST
    one, already abandoned, returned the only complete answer while the live one 503'd
    (dev/changelog/678). `sid` plus `seq` are how the page says so out loud; the server side
    is tests/test_search_supersession.py.

    Reuses the counts-split scenario because it is the one that drives both aggregate
    endpoints as well as the rows.
    """

    SCENARIO = 'counts_split'

    def test_the_row_request_carries_the_page_id_and_its_own_sequence(self):
        self.assertTrue(self.obs['row_request_sid'])
        self.assertTrue(self.obs['row_request_seq'])

    def test_every_request_from_one_page_load_carries_the_same_id(self):
        """Two ids would read as two different pages, and neither could supersede the
        other's work."""
        self.assertTrue(self.obs['row_request_sid'], 'no request carried an id at all')
        self.assertTrue(self.obs['every_request_shares_one_sid'])

    def test_the_counts_request_reuses_the_row_requests_sequence(self):
        """Those numbers describe one specific row response, so a newer ROW request is what
        invalidates them - which is also why the server puts both on one lane. Given its own
        counter it would never be cancelled by the search that replaced it."""
        self.assertTrue(self.obs['counts_request_seq'])
        self.assertEqual(self.obs['counts_request_seq'], self.obs['row_request_seq'])
        self.assertEqual(self.obs['counts_request_sid'], self.obs['row_request_sid'])

    def test_the_facet_rail_has_its_own_sequence(self):
        """A separate lane, because rows and the rail run concurrently by design: sharing
        one counter would make each cancel the other on every keystroke."""
        self.assertTrue(self.obs['facets_request_seq'])

    def test_row_sequences_only_ever_go_up(self):
        """A seq that repeated or went backwards would either cancel nothing or cancel the
        live request instead of the dead one."""
        self.assertTrue(self.obs['row_seqs_in_order'])


class CountsSplitChannelGrainTests(_PageJs, unittest.TestCase):
    """The channel grain's own breakdown is already cheap (DESIGN-channel-search.md §10),
    so it stays bundled with the row response - the split above is airing-grain only, not
    a blanket change to how every request is shaped."""

    SCENARIO = 'counts_split_channels'

    def test_the_channel_grains_row_request_keeps_counts_inline(self):
        self.assertTrue(self.obs['row_request_did_not_skip_counts'])

    def test_no_separate_counts_request_is_made(self):
        self.assertFalse(self.obs['counts_request_made'])

    def test_the_header_count_is_the_real_total_immediately(self):
        # "N+ channels" is the PENDING wording (dev/changelog/598). "N channels + M
        # groups" is a real, complete answer that happens to contain the same character,
        # so the assertion names the pending shape rather than the character.
        self.assertNotRegex(self.obs['head_count'], r'\d\+')


class AiringMobileTests(_PageJs, unittest.TestCase):
    """The airing grain at 375px: 25's rows in 22's phone arrangement."""

    SCENARIO = 'airing_mobile'

    def test_the_toggle_is_the_segmented_strip_not_the_tab_strip(self):
        """A different spelling on purpose: this page already carries a tab strip at this
        width, and that one navigates between PAGES."""
        self.assertEqual(self.obs['strip_tabs'],
                         ['📺Search Channels', '📅Search Programs (EPG)'])

    def test_the_table_becomes_cards(self):
        self.assertTrue(self.obs['cards'])
        self.assertTrue(self.obs['no_table_rows'])
        self.assertTrue(self.obs['card_title'])

    def test_the_card_has_no_record_button_only_a_kebab(self):
        """"P4 - kebab only" (round 8). The card still shows the STATE as a badge,
        because a state readable only behind a tap is not a state the list shows."""
        self.assertTrue(self.obs['card_has_no_record_button'])
        self.assertTrue(self.obs['card_kebabs'])
        self.assertTrue(self.obs['card_kebab_carries_airing'],
                        'the kebab must carry the airing id, or three showings on one '
                        'channel would open the same sheet')

    def test_the_kebab_sheet_is_about_the_showing_and_leads_with_recording_it(self):
        self.assertTrue(self.obs['sheet_opened'])
        self.assertTrue(self.obs['sheet_title'])
        rows = self.obs['sheet_rows']
        self.assertTrue(rows)
        self.assertTrue(any('showing' in r or 'recording' in r for r in rows),
                        f'no record action in the sheet: {rows}')
        self.assertTrue(any('Open channel detail' == r for r in rows))


class ResultsLoadingStateTests(_PageJs, unittest.TestCase):
    """The results area while a request is out - first paint, and a slow SAME-grain
    request. dev/docs/BUGS.md 2026-07-31 10:58 PM; the rule is
    dev/docs/DESIGN-channel-search.md 12.2."""

    SCENARIO = 'loading'

    def test_the_first_paint_is_a_spinner_and_not_a_blank_card(self):
        """Nothing has been fetched, so there is no honest list to draw. Before this the
        results area was empty until the first response, which on the airing grain is
        seconds."""
        # Two, in fact - the rows and the facet counts both go to this endpoint - but
        # the assertion is on "at least one", so a change to how the facets are fetched
        # does not make this read like a loading-state regression.
        self.assertGreaterEqual(self.obs['first_paint_held'], 1,
                                'the rows request must still be in flight for this '
                                'observation to mean anything')
        self.assertTrue(self.obs['first_paint_loading'])
        self.assertEqual(self.obs['first_paint_rows'], 0)
        self.assertIn('Loading', self.obs['first_paint_label'])

    def test_the_first_paint_does_not_claim_there_are_no_matches(self):
        """"No matches" and "0 of N" are claims about data nobody has yet."""
        self.assertFalse(self.obs['first_paint_empty_shown'])
        self.assertIn('Searching', self.obs['first_paint_count_line'])
        self.assertNotIn('No matches', self.obs['first_paint_count_line'])
        self.assertEqual(self.obs['first_paint_pager'], '')
        self.assertEqual(self.obs['first_paint_head_count'], '')

    def test_the_grain_tabs_are_drawn_before_any_rows_exist(self):
        """The spinner has to say WHICH grain is loading, and the tab strip is what says
        it - so it cannot wait for the rows the way it used to."""
        self.assertEqual(self.obs['first_paint_tabs'],
                         ['📺Search Channels', '📅Search Programs (EPG)'])

    def test_the_rows_replace_the_spinner_when_they_land(self):
        self.assertFalse(self.obs['after_release_loading'])
        self.assertGreater(self.obs['after_release_rows'], 0)

    def test_a_same_grain_request_keeps_its_rows_for_the_first_half_second(self):
        """THE FLICKER GUARD. The rows in hand are the right SHAPE - only stale - so
        blanking them on every keystroke at a 250ms debounce would be noise, not
        information. This is what proves SLOW_RESULTS_MS is not zero."""
        self.assertGreater(self.obs['slow_early_rows'], 0)
        self.assertFalse(self.obs['slow_early_loading'])

    def test_a_same_grain_request_gives_up_on_them_once_it_is_slow(self):
        """Rows 200ms behind read as a responsive page; rows four seconds behind read as
        a page that did not react, and the airing grain's unfiltered list is 4-5s.

        Polled, not sampled at a fixed moment: the state arrives on the page's own timers
        (250ms debounce + 600ms SLOW_RESULTS_MS), and a flat sleep to 900ms left 50ms of
        margin that a loaded box eats, so this failed under a sharded run while passing
        standalone (dev/changelog/724). The harness caps the poll, so "gave up late" is
        still distinguishable from "never gave up"."""
        self.assertGreaterEqual(self.obs['slow_late_waited_ms'], 0,
                                'the page never gave up on the stale rows within the cap')
        self.assertTrue(self.obs['slow_late_loading'])
        self.assertEqual(self.obs['slow_late_rows'], 0)
        self.assertIn('Searching', self.obs['slow_late_count_line'])
        self.assertEqual(self.obs['slow_late_scount'], '',
                         'the in-box match count sits where the typing happens, so a '
                         'stale number there reads as an answer to what was typed')

    def test_the_rows_come_back_when_the_slow_request_finally_answers(self):
        self.assertFalse(self.obs['slow_after_release_loading'])
        self.assertGreater(self.obs['slow_after_release_rows'], 0)


class GrainFlipLoadingTests(_PageJs, unittest.TestCase):
    """The reported defect: clicking Guide (EPG) left CHANNEL rows under AIRING column
    headers for the 4-5s the request took. dev/docs/BUGS.md 2026-07-31 10:58 PM."""

    SCENARIO = 'loading_flip'

    def test_the_page_starts_with_channel_rows_and_no_spinner(self):
        self.assertGreater(self.obs['before_rows'], 0)
        self.assertFalse(self.obs['before_loading'])
        self.assertEqual(self.obs['before_head_labels'][2], 'Channel')

    def test_the_other_grains_rows_are_gone_immediately_not_after_600ms(self):
        """Observed 20ms after the click, nowhere near SLOW_RESULTS_MS: this is the
        wrong-SHAPE case, which never goes through the timer. Rows of one grain under
        the other grain's column labels are a mismatch, not staleness."""
        self.assertEqual(self.obs['during_rows'], 0)
        self.assertTrue(self.obs['during_loading'])
        self.assertIn('airing', self.obs['during_label'])

    def test_the_header_is_already_the_airing_grains_which_is_why_it_mattered(self):
        """applyColumns() swaps the registry, the grid tracks and the header
        synchronously. That is exactly what the leftover channel rows did not match."""
        self.assertEqual(self.obs['during_head_labels'][2], 'Program')
        self.assertEqual(self.obs['during_active_tab'], '📅Search Programs (EPG)')

    def test_no_number_from_the_previous_grain_survives_the_flip(self):
        """A count, a page range or a pager about a list that is no longer on screen."""
        self.assertFalse(self.obs['during_empty_shown'])
        self.assertEqual(self.obs['during_pager'], '')
        self.assertEqual(self.obs['during_head_count'], '')
        self.assertIn('Searching', self.obs['during_count_line'])
        self.assertNotIn('channels</strong>', self.obs['during_count_line'])

    def test_the_airing_rows_replace_the_spinner_when_they_land(self):
        self.assertFalse(self.obs['after_loading'])
        self.assertGreater(self.obs['after_airing_rows'], 0)
        self.assertEqual(self.obs['after_channel_rows'], 0)


class LoadingFailureTests(_PageJs, unittest.TestCase):
    """A rows request that never answers must not leave a spinner claiming work is still
    happening - and must not claim nothing matched either."""

    SCENARIO = 'loading_fail'

    def test_the_spinner_comes_down_on_a_failure(self):
        self.assertTrue(self.obs['loading_before_reject'])
        self.assertFalse(self.obs['loading_after_reject'])

    def test_the_empty_state_names_the_failure_instead_of_claiming_no_matches(self):
        """A grain flip DISCARDS its rows, so there is nothing to put back - and "No
        airings match this search" would be a claim about data nobody has."""
        self.assertEqual(self.obs['rows_after_reject'], 0)
        self.assertTrue(self.obs['empty_shown'])
        self.assertIn('could not be run', self.obs['empty_text'])
        self.assertIn('the engine said no', self.obs['empty_text'])
        self.assertNotIn('match this search', self.obs['empty_text'])

    def test_it_is_said_out_loud_as_well(self):
        self.assertTrue(any('the engine said no' in t for t in self.obs['toasts']))


class SupersededRequestTests(_PageJs, unittest.TestCase):
    """A keystroke that supersedes an in-flight search must CANCEL it, not merely ignore
    its answer.

    dev/docs/BUGS.md 2026-07-31 10:03 PM - the sequence guard drops the stale response while the
    request behind it runs to completion, and during a sync every search is an unindexed
    scan of 1.9M rows. Ten of those at once pegged both cores for 16 minutes and
    exhausted the connection pool, whose casualty was the sync itself.

    Scope, so a green run is not read as more than it is: this proves the browser calls
    the request off. It does not - and cannot - prove the server stops scanning, because
    Werkzeug runs the handler thread to completion regardless. Bounding that is a
    separate change (dev/changelog/417)."""

    SCENARIO = 'superseded'

    def _rows(self):
        return [r for r in self.obs['requests'] if r['kind'] == 'rows']

    def _facets(self):
        return [r for r in self.obs['requests'] if r['kind'] == 'facets']

    def test_three_generations_really_were_in_flight_at_once(self):
        """Without this the rest is vacuous: aborting nothing also aborts nothing
        wrongly. Two typed queries on top of the first paint, rows and facets each."""
        self.assertEqual(len(self._rows()), 3)
        self.assertEqual(len(self._facets()), 3)
        self.assertEqual([r['q'] for r in self._rows()], ['', 'sup', 'supe'])

    def test_every_search_request_carries_a_signal(self):
        """Both call sites, not just the rows one - the facet request runs the same scan
        once per dimension, so it is the costlier half of each pair."""
        self.assertTrue(all(r['has_signal'] for r in self.obs['requests']),
                        self.obs['requests'])

    def test_the_superseded_rows_requests_were_aborted(self):
        self.assertEqual([r['aborted'] for r in self._rows()], [True, True, False])

    def test_the_superseded_facet_requests_were_aborted(self):
        self.assertEqual([r['aborted'] for r in self._facets()], [True, True, False])

    def test_the_request_the_user_is_waiting_on_is_left_alone(self):
        """The newest generation is still parked and still wanted. A companion to the two
        above rather than an independent guard - it also passes when nothing aborts at
        all, and what it really rules out is an over-eager abort that cancels the request
        it just issued."""
        self.assertFalse(self._rows()[-1]['aborted'])
        self.assertFalse(self._facets()[-1]['aborted'])
        self.assertTrue(self.obs['loading'])

    def test_a_cancelled_request_is_not_reported_as_a_failure(self):
        """Characterization, not a regression guard: the seq guard already swallowed
        this before the abort existed. It is here so that a future editor who reorders
        the abort ahead of the sequence bump - which WOULD toast - is told about it."""
        self.assertEqual(self.obs['toasts'], [])
        self.assertFalse(self.obs['empty_shown'])
        self.assertEqual(self.obs['errors'], [])


class SearchReadinessNoticeTests(_PageJs, unittest.TestCase):
    """The PROACTIVE degraded-search notice: search is slow right now, said before the user
    types rather than after a slow request comes back.

    dev/changelog/427. The reactive half - the `unindexed` badge on the results meta line -
    shipped with dev/changelog/418 and stays; this is the standing condition above the
    results, fed by the /api/nav-status poll so it appears and clears across a sync's
    lifetime on its own.

    jsdom computes no layout, so nothing here speaks to the 375px arrangement; that needed a
    browser and was checked separately."""

    SCENARIO = 'readiness'

    def test_the_hook_is_registered(self):
        """base.html's poll calls window.__applySearchReadiness behind a truthiness check,
        so a page that never registers it degrades to silence rather than to an error - the
        exact failure mode nothing else here would catch."""
        self.assertTrue(self.obs['hook_registered'])

    def test_served_hidden_and_left_hidden_until_told_otherwise(self):
        """CLAUDE.md §Frontend rendering: the server-rendered state must equal the
        nothing-active state. JS may turn the notice on; it must never be what turns it off."""
        self.assertTrue(self.obs['hidden_before_any_payload'])

    def test_a_healthy_payload_shows_nothing(self):
        self.assertTrue(self.obs['hidden_when_ready'])

    def test_a_degraded_payload_raises_the_notice(self):
        self.assertTrue(self.obs['shown_when_degraded'])

    def test_the_notice_carries_the_engines_own_reason(self):
        """One speller. The server sentence is shown verbatim rather than re-worded here -
        tests/test_search_readiness_notice.py guards the other end of that."""
        html = self.obs['degraded_html']
        self.assertIn('the programs search index is stale', html)
        self.assertIn('complete and correct', html,
                      'the rows a degraded search returns are still correct and must say so')

    def test_it_clears_itself_when_readiness_comes_back(self):
        """The reason it can piggyback the poll at all: no reload, no user action."""
        self.assertTrue(self.obs['hidden_after_flip_back'])

    def test_a_channel_only_search_is_not_warned_about_the_program_index(self):
        """Mirrors channel_search._index_names(). Warning about an index the current field
        set never touches is the false alarm that teaches people to ignore the notice.

        Since dev/changelog/860 the channel grain's DEFAULT scope is the channel's name
        alone, so this is the state the page opens in - a stale programs index has to be
        silent here, and only start speaking when a program field is switched on."""
        self.assertTrue(self.obs['found_epg_switch'])
        self.assertEqual(self.obs['fields_at_default'], ['name'])
        self.assertTrue(self.obs['hidden_for_default_channel_scope'],
                        'the default channel scope touches no program index')
        self.assertEqual(self.obs['fields_after_tick'], ['name', 'epg-title'])
        self.assertTrue(self.obs['shown_with_a_program_field'])
        self.assertEqual(self.obs['fields_after_untick'], ['name'])
        self.assertTrue(self.obs['hidden_for_channels_only_search'])

    def test_turning_a_program_field_back_on_warns_without_waiting_for_a_poll(self):
        """No nav-status response happens between these two states - only the field change.
        Re-rendering on poll alone would leave the page silent until the next tick."""
        self.assertTrue(self.obs['shown_again_after_reticking'])

    def test_a_bad_channels_index_warns_whatever_is_being_searched(self):
        self.assertTrue(self.obs['shown_when_channels_index_bad'])

    def test_the_unindexed_badge_region_is_untouched(self):
        """Two regions, two triggers, one updater each. The badge belongs to renderCount()
        and reports on a response that already returned; nothing here may write it."""
        self.assertNotIn('cs-degraded', self.obs['count_html'])
        self.assertNotIn('running without their index', self.obs['count_html'])

    def test_nothing_was_toasted(self):
        """A standing condition is not an event, and 8 syncs a day would be 8 toasts."""
        self.assertEqual(self.obs['toasts'], [])


class DeclinedNumbersTests(_PageJs, unittest.TestCase):
    """When the server declines the numbers, the page says so IN THE SERVER'S WORDS.

    dev/changelog/681. Two different conditions make the totals and the facet rail too
    expensive to compute - the index is temporarily unusable, or the index cannot answer this
    search at all (an airing search with "Show airings that have ended" ticked, which puts
    1.35M ended showings back in scope with nothing able to narrow them). They read
    differently because the remedies differ: wait, versus untick a checkbox.

    Both surfaces used to hardcode the first, so the second one's copy told the reader to wait
    out something that was never going to clear. The server sends one sentence and the page
    renders it - the same one-speller rule the readiness notice above already follows.
    """

    SCENARIO = 'declined_numbers'

    def test_the_rows_are_still_there(self):
        """The whole point: completeness of the answer is what is traded, never its
        existence."""
        self.assertTrue(self.obs['row_count'])

    def test_the_count_line_says_the_total_was_not_counted(self):
        self.assertIn('total not counted', self.obs['count_line_html'])
        self.assertNotIn('counting the total', self.obs['count_line_html'],
                         'a declined total is not coming - saying "counting" promises work '
                         'nobody is doing')

    def test_the_rail_says_its_counts_are_missing_and_the_filters_still_work(self):
        note = self.obs['rail_note_text']
        self.assertIn('Counts not available', note)
        self.assertIn('filters still work', note)

    def test_neither_surface_claims_the_index_is_being_rebuilt(self):
        """The regression this class exists for. Both strings were hardcoded to the degraded
        case, so a healthy search the index simply could not answer was labeled as an
        unindexed one - naming the wrong cause and pointing at the wrong fix."""
        for where, text in (('rail note', self.obs['rail_note_text']),
                            ('rail tooltip', self.obs['rail_note_tip']),
                            ('count tooltip', self.obs['count_tip'])):
            with self.subTest(where=where):
                self.assertNotIn('unindexed', text)
                self.assertNotIn('without its index', text)

    def test_both_tooltips_carry_the_servers_own_reason(self):
        why = _observations()['decline_why']
        self.assertIn(why, self.obs['rail_note_tip'])
        self.assertIn(why, self.obs['count_tip'])

    def test_nothing_was_toasted(self):
        """An incomplete answer over correct rows is not a failure, and an error toast over a
        working page is what a 503 used to produce here."""
        self.assertEqual(self.obs['toasts'], [])


class ServedNumbersTests(_PageJs, unittest.TestCase):
    """The control for the class above: neither decline surface may be permanently on."""

    SCENARIO = 'served_numbers'

    def test_no_rail_note_when_the_counts_were_served(self):
        self.assertFalse(self.obs['rail_note_present'])

    def test_no_uncounted_marker_when_the_total_was_served(self):
        self.assertFalse(self.obs['count_uncounted_present'])


class StandingInversionTests(_PageJs, unittest.TestCase):
    """The standing options after the inversion (dev/changelog/778).

    Every assertion here is about a DIRECTION, because getting one backwards is invisible:
    the page still renders, the request still succeeds, and the list is simply the
    complement of what the reader asked for. Six of the eight options are keyed `show*` and
    remove rows while their key is ABSENT, which pulls three statements apart that used to
    be one - which toggle is lit, which option is hiding, and what the count line says.
    """

    SCENARIO = 'standing_inversion'

    def test_the_disclosure_line_names_each_options_own_noun(self):
        """The reported defect, and the reason the `noun` field exists: at four options on,
        the line read `849 hidden - 752 hidden - 101,916 hidden - 33,401 hidden`. Every
        entry has to be distinguishable without hovering it."""
        entries = self.obs['disclosure_entries']
        self.assertTrue(entries, 'the corpus should have something hidden to disclose')
        nouns = [e.split(' ', 1)[1] for e in entries]
        self.assertEqual(len(set(nouns)), len(nouns), entries)
        self.assertNotIn('hidden', nouns[:len(nouns)] if len(nouns) > 1 else [])

    def test_clicking_a_disclosure_entry_puts_those_rows_back(self):
        """It ADDS the `show*` key. The pre-inversion spelling deleted the key, which after
        the rename is a no-op on exactly the state this entry renders in - the click would
        look live and do nothing at all."""
        key = self.obs['clicked_disclosure_key']
        self.assertNotIn(key, self.obs['standing_before_disclosure_click'])
        self.assertIn(key, self.obs['standing_after_disclosure_click'])

    def test_the_count_badge_sits_on_the_option_that_is_hiding_not_the_lit_one(self):
        """`state.standing.has(key)` is what lights a toggle; `standingApplied()` is what
        decides it has anything to report. For a `show*` option those are opposites, so
        using one for the other puts every count on the wrong control."""
        lit = set(self.obs['lit_toggles'])
        counted = set(self.obs['toggles_with_a_count'])
        self.assertTrue(counted, 'the corpus should have an option hiding something')
        self.assertFalse(lit & counted, f'lit={sorted(lit)} counted={sorted(counted)}')

    def test_the_card_is_no_longer_titled_for_exclusions(self):
        """A card called Fixed Exclusions full of "Show ..." toggles contradicts itself."""
        self.assertEqual(self.obs['card_title'], 'Standing options')

    def test_the_card_badge_counts_what_is_being_removed_not_what_is_ticked(self):
        """Those were the same number while every label said "Hide ..."; they are now
        opposites. Every option this grain offers is a `show*` one, so "hiding" is exactly
        "not lit" here - and a badge counting ticks would report how much is getting
        through, beside a list of what is being kept out."""
        hiding = len(self.obs['all_toggles']) - len(self.obs['lit_toggles'])
        self.assertEqual(self.obs['card_badge'], str(hiding))
        self.assertNotEqual(hiding, len(self.obs['lit_toggles']),
                            'the fixture must not have the two halves equal, or this '
                            'assertion cannot tell them apart')


if __name__ == '__main__':
    unittest.main()
