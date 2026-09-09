"""The channel search PAGE route: the saved-search default, and the link vocabulary.

Everything else about this search is JSON (tests/test_channel_search.py for the engine,
tests/test_channel_search_api.py for the endpoints). What is left in the route is small and
all of it is a thing that fails silently:

* **The default saved search is applied by a REDIRECT**, not by the page's JS, so the
  address bar and the screen say the same thing and a copied link reproduces what was on
  screen. A redirect that fires on a request that already carries parameters would override
  a search the visitor stated and could not be got rid of; one that fires on its own output
  is an infinite loop.
* **The stored value is user text in a JSON blob.** A saved search naming a dimension that
  has since been removed, or a pref row holding something that is not a list at all, must
  open the page unfiltered - not 400 or 500 every bare visit to Channels.
* **The saved query string is re-serialized through `SearchState.to_params()`**, never
  re-emitted as stored. That function is the URL contract, so a link into this search has
  exactly one speller.
* **The inbound links spell `f.acct` / `f.cat`** (DESIGN-channel-search.md 2.4). The parser
  IGNORES an unknown non-`f.`/`x.` key rather than raising - the deep links the old Browse
  tab shipped (`account_id=`, `category=`) are therefore dropped in silence, so a link left
  unrepointed lists every channel with no error anywhere. The links themselves are asserted
  on here, not just what the route accepts.
"""
import json
import os
import re
import sys
import unittest
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tests.support.app import make_test_app  # noqa: E402
from tests.support import seed  # noqa: E402
from app import db  # noqa: E402
from app.database import UserPref  # noqa: E402
from app.routes.channels import CHANNEL_SEARCH_SAVED_PREF  # noqa: E402

PAGE_URL = '/channels'


class _PageTestCase(unittest.TestCase):

    def setUp(self):
        self.t = make_test_app()
        self.ctx_mgr = self.t.app.app_context()
        self.ctx_mgr.push()
        now = datetime.utcnow()
        self.acct_a = seed.make_account(name='Alpha', last_sync_at=now)
        self.acct_b = seed.make_account(name='Beta', last_sync_at=now)
        self.a_sports = seed.make_channel(self.acct_a, name='Alpha Sports',
                                          category_name='Sports', last_seen_at=now)
        self.a_docs = seed.make_channel(self.acct_a, name='Alpha Docs',
                                        category_name='Docs', last_seen_at=now)
        self.b_sports = seed.make_channel(self.acct_b, name='Beta Sports',
                                          category_name='Sports', last_seen_at=now)
        db.session.commit()

    def tearDown(self):
        self.ctx_mgr.pop()
        self.t.cleanup()

    def save(self, records):
        db.session.add(UserPref(key=CHANNEL_SEARCH_SAVED_PREF, value=json.dumps(records)))
        db.session.commit()

    def save_raw(self, value):
        db.session.add(UserPref(key=CHANNEL_SEARCH_SAVED_PREF, value=value))
        db.session.commit()


class DefaultSavedSearchTests(_PageTestCase):

    def test_bare_visit_renders_when_nothing_is_saved(self):
        resp = self.t.client.get(PAGE_URL)
        self.assertEqual(resp.status_code, 200)

    def test_bare_visit_renders_when_a_saved_search_is_not_the_default(self):
        self.save([{'name': 'Sports', 'params': 'q=sports', 'is_default': False}])
        resp = self.t.client.get(PAGE_URL)
        self.assertEqual(resp.status_code, 200)

    def test_bare_visit_redirects_to_the_default_search(self):
        # `category`, which must stay a NON-default sort for this to test anything:
        # to_params() omits a sort at its default, so spelling the default here would
        # assert that the redirect drops it (that is the next test's job).
        self.save([{'name': 'Sports', 'params': 'q=sports&f.cat=Sports&sort=category',
                    'is_default': True}])
        resp = self.t.client.get(PAGE_URL)
        self.assertEqual(resp.status_code, 302)
        self.assertIn('q=sports', resp.headers['Location'])
        self.assertIn('f.cat=Sports', resp.headers['Location'])
        self.assertIn('sort=category', resp.headers['Location'])

    def test_the_redirect_target_renders_and_does_not_redirect_again(self):
        """The one loop this route could have: a redirect whose own output is a bare visit."""
        self.save([{'name': 'Sports', 'params': 'q=sports', 'is_default': True}])
        first = self.t.client.get(PAGE_URL)
        self.assertEqual(first.status_code, 302)
        second = self.t.client.get(first.headers['Location'])
        self.assertEqual(second.status_code, 200)

    def test_a_stated_search_is_never_overridden_by_the_default(self):
        self.save([{'name': 'Sports', 'params': 'q=sports', 'is_default': True}])
        resp = self.t.client.get(PAGE_URL + '?q=docs')
        self.assertEqual(resp.status_code, 200)

    def test_the_redirect_carries_an_empty_standing_when_the_search_has_none(self):
        """The case the whole "carry the standing options" rule exists for: a saved search
        that turns the default-on hiders OFF must not come back with them on. Absent means
        the defaults, so "none" is only expressible as one empty value."""
        self.save([{'name': 'Duplicate cleanup', 'params': 'f.other=dupurl&standing=',
                    'is_default': True}])
        resp = self.t.client.get(PAGE_URL)
        self.assertEqual(resp.status_code, 302)
        self.assertIn('standing=', resp.headers['Location'])
        # ...and it survives a round trip back through the parser, which is what the page
        # actually does with it.
        follow = self.t.client.get(resp.headers['Location'])
        self.assertEqual(follow.status_code, 200)

    def test_only_the_first_default_is_honoured(self):
        self.save([{'name': 'One', 'params': 'q=one', 'is_default': True},
                   {'name': 'Two', 'params': 'q=two', 'is_default': True}])
        resp = self.t.client.get(PAGE_URL)
        self.assertIn('q=one', resp.headers['Location'])

    def test_the_target_is_the_engines_own_spelling_not_the_stored_text(self):
        """Stored params that are merely equivalent (defaults spelled out, a redundant
        page=1) come back minimised, because the link is built from to_params()."""
        # `sort=name` is DEFAULT_SORT spelled out and `in=name` is this grain's default
        # scope (dev/changelog/860) - both are the thing being minimised away here. The sort
        # moved from `category` in dev/changelog/699; these strings track those constants.
        self.save([{'name': 'Verbose',
                    'params': 'q=sports&in=name&sort=name&page=1&per_page=100',
                    'is_default': True}])
        resp = self.t.client.get(PAGE_URL)
        self.assertEqual(resp.headers['Location'].split('?', 1)[1], 'q=sports')

    def test_a_default_that_says_nothing_does_not_redirect(self):
        """An empty search serializes to an empty query string, which would redirect to the
        same bare URL forever."""
        self.save([{'name': 'Everything', 'params': '', 'is_default': True}])
        self.assertEqual(self.t.client.get(PAGE_URL).status_code, 200)


class UnusableSavedSearchTests(_PageTestCase):
    """None of these may take the page down: it is a decoration on a page whose job is to
    search, and the failure has to be recoverable without editing the database."""

    def test_an_unknown_filter_dimension_opens_the_page_unfiltered(self):
        self.save([{'name': 'Stale', 'params': 'f.nosuchdim=x', 'is_default': True}])
        self.assertEqual(self.t.client.get(PAGE_URL).status_code, 200)

    def test_an_unknown_sort_opens_the_page_unfiltered(self):
        self.save([{'name': 'Stale', 'params': 'sort=airing', 'is_default': True}])
        self.assertEqual(self.t.client.get(PAGE_URL).status_code, 200)

    def test_a_pref_row_that_is_not_json_opens_the_page(self):
        self.save_raw('{not json at all')
        self.assertEqual(self.t.client.get(PAGE_URL).status_code, 200)

    def test_a_pref_row_that_is_not_a_list_opens_the_page(self):
        self.save_raw(json.dumps({'name': 'Sports', 'is_default': True}))
        self.assertEqual(self.t.client.get(PAGE_URL).status_code, 200)

    def test_records_that_are_not_objects_are_skipped(self):
        self.save_raw(json.dumps(['nonsense', {'name': 'Ok', 'params': 'q=sports',
                                               'is_default': True}]))
        resp = self.t.client.get(PAGE_URL)
        self.assertEqual(resp.status_code, 302)
        self.assertIn('q=sports', resp.headers['Location'])


class InboundLinkVocabularyTests(_PageTestCase):
    """The repointed links. `f.acct` / `f.cat` is what the search parses; the old
    `account_id=` / `category=` spelling is silently ignored, so a link left unrepointed
    lists every channel and nothing anywhere goes red."""

    def test_a_stated_filter_survives_the_page_load(self):
        """The page is a shell - the rows arrive over JSON - so what the route owes an
        inbound link is that it renders the URL rather than redirecting it away."""
        resp = self.t.client.get(PAGE_URL + f'?f.acct={self.acct_a.id}')
        self.assertEqual(resp.status_code, 200)

    def test_the_old_spelling_is_dropped_rather_than_erroring(self):
        """Not an endorsement - the reason every link had to be repointed. A stray old
        link opens the page unfiltered instead of 400ing, and the address bar shows it."""
        self.assertEqual(self.t.client.get(PAGE_URL + '?account_id=1').status_code, 200)
        resp = self.t.client.get('/api/channels/search?account_id=1&facets=')
        self.assertEqual(resp.status_code, 200)
        names = {r['name'] for r in resp.get_json()['rows']}
        self.assertEqual(names, {'Alpha Sports', 'Alpha Docs', 'Beta Sports'})

    def test_the_new_search_answers_the_new_spelling(self):
        resp = self.t.client.get(f'/api/channels/search?f.acct={self.acct_a.id}&facets=')
        self.assertEqual(resp.status_code, 200)
        names = {r['name'] for r in resp.get_json()['rows']}
        self.assertEqual(names, {'Alpha Sports', 'Alpha Docs'})

    def test_the_account_link_is_rendered_with_the_new_spelling(self):
        """The link itself, not just what the routes accept: repointing one side only is the
        whole failure mode, and nothing goes red when it happens."""
        body = self.t.client.get('/accounts').get_data(as_text=True)
        hrefs = re.findall(r'href="([^"]*)"[^>]*>Browse channels<', body)
        self.assertTrue(hrefs, 'no Browse channels link on the accounts page')
        for href in hrefs:
            self.assertIn('f.acct=', href)
            self.assertNotIn('account_id=', href)

    def test_the_add_channels_link_carries_the_group_id_and_not_its_name(self):
        """`group_name` is not part of the contract - the page resolves the name from the id
        through the catalog, so it never has to trust a display name from a URL."""
        group = seed.make_group(name='Fox', members=[self.a_sports])
        db.session.commit()
        body = self.t.client.get(f'/channel-groups/{group.id}').get_data(as_text=True)
        self.assertIn(f'add_to_group={group.id}', body)
        self.assertNotIn('group_name=', body)


class AiringShellTests(_PageTestCase):
    """What the SHELL has to carry for the airing grain (dev/changelog/414).

    The page is written by JS, so nothing here asserts on rendering - only on the four
    things the server has to put in the first paint for that JS to have anything to work
    with, each of which fails silently if it is missing.
    """

    def test_the_grain_slot_is_served_and_starts_empty(self):
        """ONE slot at both widths since dev/changelog/811 - the desktop tab strip and the
        phone segmented strip were replaced by one pair of pills above the search box.
        Empty IS the nothing-active state: a page whose catalog never lands must show no
        selector rather than a wrong one (CLAUDE.md, frontend rendering rules)."""
        body = self.t.client.get(PAGE_URL).get_data(as_text=True)
        self.assertIn('id="grainslot-panel"', body)
        self.assertRegex(body, r'id="grainslot-panel"[^>]*>\s*</div>')
        # The two it replaced are gone, not merely unused - a stale empty slot would keep
        # its margin and push the search zone down by a row's worth of nothing.
        self.assertNotIn('id="grainslot-tabs"', body)
        self.assertNotIn('id="grainstrip"', body)

    def test_the_heading_is_the_count_alone(self):
        """The grain pills above the search box name the grain, so the heading saying it
        again said it twice - deleted in mockup 25 round 3."""
        body = self.t.client.get(PAGE_URL).get_data(as_text=True)
        self.assertIn('id="all-head-count"', body)
        self.assertNotIn('<h2>All Channels', body)

    def test_the_airing_pref_keys_are_their_own_rows(self):
        """One shared row would have each grain overwrite the other's column setup every
        time the toggle was flipped."""
        body = self.t.client.get(PAGE_URL).get_data(as_text=True)
        self.assertIn('channel_search_airing_columns', body)
        self.assertIn('channel_search_airing_card_fields', body)
        # ...and they are DIFFERENT rows from the channel grain's.
        self.assertIn('channel_search_columns', body)
        self.assertIn('channel_search_card_fields', body)

    def test_the_loading_placeholder_is_served_hidden(self):
        """Same rule as the toggle slots above: the served markup IS the nothing-active
        state, so the spinner ships hidden and JS only ever turns it on. It sits OUTSIDE
        #atable so the column header stays visible above it and so it works unchanged at
        375, where .arow-head is hidden but the table still holds the card list
        (dev/changelog/415, dev/docs/DESIGN-channel-search.md 12.2)."""
        body = self.t.client.get(PAGE_URL).get_data(as_text=True)
        self.assertIn('id="all-loading"', body)
        self.assertIn('id="all-loading-lbl"', body)
        self.assertRegex(body, r'id="all-loading"[^>]*style="display:none"')
        self.assertLess(body.index('id="atable"'), body.index('id="all-loading"'),
                        'the placeholder must follow the table, not live inside it')

    def test_the_record_modal_and_its_config_are_on_the_page(self):
        """The Record action opens the SAME modal the TV Guide opens, through the same
        guide.js - not a second schedule modal. Its element ids are a contract with
        openModal(), so a missing one is a modal that opens blank."""
        body = self.t.client.get(PAGE_URL).get_data(as_text=True)
        for el in ('record-modal', 'modal-form', 'modal-url', 'modal-start', 'modal-stop',
                   'modal-channel-id', 'modal-group-id', 'modal-profile', 'modal-epg-id'):
            self.assertIn(f'id="{el}"', body, f'{el} is missing, so openModal would throw')
        self.assertIn('GUIDE_CONFIG', body)
        self.assertIn('js/guide.js', body)
        self.assertIn('recordContextUrlBase', body)


class RecordContextEndpointTests(_PageTestCase):
    """`GET /api/channels/airings/<epg_id>/record-context`.

    The one thing on this page that deliberately serves an UNMASKED stream URL, because
    the modal posts it - so what it answers for, and what it refuses, both matter.
    """

    def _airing(self):
        from app.database import EPGEntry
        from datetime import timedelta
        now = datetime.utcnow()
        entry = EPGEntry(channel_id=self.a_sports.id, title='Wembley Cup Final',
                         sub_title='Semi Final', description='at Wembley',
                         start_time=now + timedelta(hours=1),
                         stop_time=now + timedelta(hours=3))
        db.session.add(entry)
        db.session.commit()
        return entry

    def test_it_answers_with_everything_open_modal_reads(self):
        entry = self._airing()
        data = self.t.client.get(
            f'/api/channels/airings/{entry.id}/record-context').get_json()
        self.assertTrue(data['success'])
        prog = data['program']
        for key in ('id', 'title', 'start_time', 'stop_time', 'stream_url', 'suggested_name',
                    'channel_id', 'group_id', 'has_recording', 'recording_id',
                    'recording_status', 'recording_profile_id', 'description'):
            self.assertIn(key, prog, f'openModal reads {key}')
        self.assertEqual(prog['channel_id'], self.a_sports.id)
        self.assertEqual(data['channel']['id'], self.a_sports.id)
        self.assertIn('default_profile_id', data['channel'])

    def test_the_url_it_serves_is_the_real_one_not_the_masked_one(self):
        """This is the whole reason the endpoint exists. A masked URL would post fine and
        then record nothing."""
        entry = self._airing()
        prog = self.t.client.get(
            f'/api/channels/airings/{entry.id}/record-context').get_json()['program']
        self.assertNotIn('***', prog['stream_url'])
        self.assertTrue(prog['stream_url'])

    def test_it_finds_a_recording_that_already_covers_the_showing(self):
        """Identity first: the recording carries channel_id, which survives the provider
        rewriting its stream URLs."""
        entry = self._airing()
        rec = seed.make_recording(status='SCHEDULED', channel_id=self.a_sports.id,
                                  start_time=entry.start_time, stop_time=entry.stop_time)
        db.session.commit()
        prog = self.t.client.get(
            f'/api/channels/airings/{entry.id}/record-context').get_json()['program']
        self.assertTrue(prog['has_recording'])
        self.assertEqual(prog['recording_id'], rec.id)
        self.assertEqual(prog['recording_status'], 'SCHEDULED')

    def test_it_finds_a_group_backed_recording_covering_the_showing(self):
        """dev/docs/BUGS.md 2026-08-23: a group-backed Recording carries group_id and no
        channel_id, so a channel-only match missed it entirely and the modal opened in
        Schedule (not Edit) mode, creating a duplicate of a showing already covered."""
        entry = self._airing()
        group = seed.make_group(name='A Sports Group', members=[self.a_sports])
        rec = seed.make_recording(status='SCHEDULED', group_id=group.id,
                                  start_time=entry.start_time, stop_time=entry.stop_time)
        db.session.commit()
        prog = self.t.client.get(
            f'/api/channels/airings/{entry.id}/record-context').get_json()['program']
        self.assertTrue(prog['has_recording'])
        self.assertEqual(prog['recording_id'], rec.id)
        self.assertEqual(prog['group_id'], group.id)

    def test_a_fresh_showing_on_a_grouped_channel_stays_channel_pinned(self):
        """group_id is only ever carried for a showing that already
        has a group-backed recording - never invented for a plain Record click, even when
        the channel belongs to an in-guide group. Auto-upgrading a click on one specific
        feed into a failover-capable recording would schedule more than what was clicked."""
        entry = self._airing()
        seed.make_group(name='A Sports Group', members=[self.a_sports])
        db.session.commit()
        prog = self.t.client.get(
            f'/api/channels/airings/{entry.id}/record-context').get_json()['program']
        self.assertFalse(prog['has_recording'])
        self.assertIsNone(prog['group_id'])

    def test_an_unknown_showing_is_a_404_naming_the_problem(self):
        """A showing can vanish between the search response and the click - an EPG sync
        deletes past entries. The honest answer is a 404 the page can say out loud."""
        resp = self.t.client.get('/api/channels/airings/99999999/record-context')
        self.assertEqual(resp.status_code, 404)
        self.assertIn('error', resp.get_json())


class ReplaceContextTests(_PageTestCase):
    """The second action context (dev/changelog/416): the recording detail page's "Find
    another airing" hands `replace_rec=<id>` to this page, which resolves it to a name for
    the strip and hands the resolution to the JS as the authority on whether the context is
    live at all."""

    def _rec(self, status='SCHEDULED'):
        rec = seed.make_recording(status=status, name='Wembley Final',
                                  channel_id=self.a_sports.id)
        db.session.commit()
        return rec

    def test_a_scheduled_recording_is_resolved_and_named(self):
        rec = self._rec()
        body = self.t.client.get(f'{PAGE_URL}?grain=airings&replace_rec={rec.id}'
                                 ).get_data(as_text=True)
        self.assertIn('"name": "Wembley Final"', body)

    def test_a_recording_that_is_no_longer_scheduled_drops_the_context(self):
        """A stale link is expected here - the recording may have started, finished or been
        deleted between the link being copied and being followed. A decoration may not 500
        the page it decorates, and replacing a capture in progress is not a thing to offer,
        so the page opens with the context off."""
        rec = self._rec(status='IN_PROGRESS')
        resp = self.t.client.get(f'{PAGE_URL}?grain=airings&replace_rec={rec.id}')
        self.assertEqual(resp.status_code, 200)
        self.assertIn('replaceRec: null', resp.get_data(as_text=True))

    def test_an_id_that_does_not_exist_drops_the_context(self):
        resp = self.t.client.get(f'{PAGE_URL}?grain=airings&replace_rec=99999999')
        self.assertEqual(resp.status_code, 200)
        self.assertIn('replaceRec: null', resp.get_data(as_text=True))

    def test_the_strip_is_served_hidden(self):
        """Server-rendered initial state must equal the nothing-active state: JS turns the
        strip on, it is never required to calm the page down."""
        rec = self._rec()
        body = self.t.client.get(f'{PAGE_URL}?grain=airings&replace_rec={rec.id}'
                                 ).get_data(as_text=True)
        self.assertIn('id="replace-bar" style="display:none"', body)


class FindAnotherAiringLinkTests(_PageTestCase):
    """The two entry points that replaced the Extended Search modal. Asserted on the LINKS
    themselves, not just on what the routes accept: repointing one side only is the whole
    failure mode, and nothing goes red when it happens."""

    def test_the_recording_detail_link_carries_the_airing_grain_and_the_title(self):
        entry = seed.make_epg_entry(self.a_sports, title='Wembley Final',
                                    offset_minutes=90)
        db.session.commit()
        rec = seed.make_recording(status='COMPLETED', name='Wembley Final',
                                  channel_id=self.a_sports.id,
                                  start_time=entry.start_time, stop_time=entry.stop_time,
                                  program_title='Wembley Final')
        db.session.commit()
        body = self.t.client.get(f'/recordings/{rec.id}').get_data(as_text=True)
        hrefs = re.findall(r'href="(/channels\?[^"]*)"', body)
        self.assertTrue(hrefs, 'no link into the channel search on the recording detail page')
        for href in hrefs:
            self.assertIn('grain=airings', href)
            self.assertIn('in=epg-title', href)
            self.assertIn('Wembley', href)
        self.assertNotIn('/api/guide/search', body)

    def test_only_a_scheduled_recording_offers_the_replace_context(self):
        """Replacing means DELETING the original, which is only a coherent thing to do to a
        recording that has not run. A completed one links to the same search undecorated."""
        for status, expect in (('SCHEDULED', True), ('COMPLETED', False)):
            with self.subTest(status=status):
                rec = seed.make_recording(status=status, name='Wembley Final',
                                          channel_id=self.a_sports.id)
                db.session.commit()
                body = self.t.client.get(f'/recordings/{rec.id}').get_data(as_text=True)
                self.assertEqual(f'replace_rec={rec.id}' in body, expect)

    def test_the_guide_button_carries_the_airing_search_url(self):
        """The TV Guide's "Search all..." is a button rather than an anchor because it takes
        the filter box's live text with it - so the base URL rides a data attribute, and
        this pins that it is a real airing-search URL and not the retired endpoint."""
        # An empty guide renders guide_empty.html, which has no toolbar at all.
        self.a_sports.in_guide = True
        db.session.commit()
        body = self.t.client.get('/guide').get_data(as_text=True)
        m = re.search(r'data-airing-search-url="([^"]*)"', body)
        self.assertIsNotNone(m, 'the guide toolbar lost its link into the airing search')
        self.assertIn('grain=airings', m.group(1))
        self.assertNotIn('/api/guide/search', body)


class ScheduleFieldsWindowOptionTests(_PageTestCase):
    """The health-check modal's schedule picker, as this page renders it into its
    <template> (dev/docs/BUGS.md 2026-08-27, dev/changelog/830).

    `recur_schedule_fields`'s third parameter defaults to '', so omitting it removes the
    maintenance-window radio from the modal ENTIRELY rather than erroring - which is how
    this page shipped for months as the only one of five call sites that could not reach
    the window. That silence is the whole reason this is asserted on the rendered page
    rather than inferred from the call site.
    """

    def setUp(self):
        super().setUp()
        self.body = self.t.client.get(f'{PAGE_URL}?q=').get_data(as_text=True)

    def test_the_maintenance_window_radio_is_rendered(self):
        self.assertIn('id="browsesched-run-window"', self.body,
                      'channel search lost the maintenance-window option again - '
                      'check that the route still supplies window_label')

    def test_the_radio_is_labelled_with_the_real_window_bounds(self):
        """A label is what makes the option choosable - "Maintenance window ()" is what an
        empty window_label would render, and it would still pass the presence check above."""
        m = re.search(r'Maintenance window \(([^)]*)\)', self.body)
        self.assertIsNotNone(m, 'the window radio rendered without its label')
        self.assertRegex(m.group(1).strip(), r'^\d.*-.*\d',
                         'the window label is empty or is not a pair of clock times')

    def test_the_window_is_the_preselected_default(self):
        """dev/changelog/830: the window is the answer this app recommends, so it is the
        RENDERED default - not something JS switches on after mount."""
        win = re.search(r'id="browsesched-run-window"[^>]*>', self.body).group(0)
        time_radio = re.search(r'id="browsesched-run-time"[^>]*>', self.body).group(0)
        self.assertIn('checked', win)
        self.assertNotIn('checked', time_radio)


if __name__ == '__main__':
    unittest.main()
