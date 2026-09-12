"""The channel-group detail page's member list against the design that produced it.

`dev/changelog/755` ported the member list out of `dev/mockups/33-group-detail-desktop.html`
under the porting contract in `dev/docs/DESIGN-channel-groups-model.md` §17. Before it, no UI
anywhere wrote `ChannelGroupMember.recording_enabled`, so no group created since
`dev/changelog/741` could ever be promoted out of health-check-only.

Each case here is a decision a careless edit would quietly undo:

  * **The two participation columns exist and are sortable** (§4.2, DECIDED 6). Two
    checkboxes were chosen over a multiselect *precisely* so each can be sorted and
    filtered on its own; a list that cannot do both has missed the point of the columns.
  * **A stored group's rows carry the two switches, and nothing is "disabled"** (DECIDED 3:
    membership is indicators, not a status). The derived flag is gone rather than renamed -
    keeping it would also have left every member unselectable for the bulk switches, since
    a disabled row carries no checkbox.
  * **The system group is the opposite case** and keeps `disabled`: its membership is
    computed, so it has no participation columns to show and its rows carry
    `Channel.test_enabled` instead.
  * **`format_blocked` comes from `format_eligible_members()`**, the one helper every
    selection site asks - never re-derived here. Two answers to "which members may serve"
    is a disagreement the user sees as a row claiming a member is skipped while the
    recorder picks it (`CLAUDE.md` "Format lock filters, health score ranks").
  * **No participation control is ever disabled** (§4.1). The lock filters where members
    are chosen and writes nothing, so a member the lock currently skips keeps a live
    control and the pill beside its name explains it.
  * **Both write paths go through the one route, and therefore the one writer.** The bulk
    endpoint validates its field server-side and reports what actually moved - enforcement
    never lives in whichever control happened to post it.

The pills and the dim are rendered by `static/js/group-detail.js` from these payload
fields, so what is asserted here is the payload and the markup that carries it; the browser
half (the fixed-width grid tracks) is what the Playwright pass covers, because jsdom
computes no layout.

Runs against a throwaway temp SQLite DB - never the live dvr.db.
  python3 -m unittest tests.test_group_detail_page_conformance
"""
import os
import re
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tests.support.app import make_test_app  # noqa: E402
from tests.support.seed import (make_account, make_channel, make_group,  # noqa: E402
                                make_channel_test, make_test_job)
from app import db  # noqa: E402
from app.database import ChannelGroupMember, ChannelGroupEvent  # noqa: E402

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _read(rel):
    with open(os.path.join(REPO, rel), encoding='utf-8') as fh:
        return fh.read()


class GroupDetailPayloadTests(unittest.TestCase):
    """What the row payload must carry for the member list to render at all."""

    def setUp(self):
        self.t = make_test_app()
        self.app = self.t.app
        self.app.config['WTF_CSRF_ENABLED'] = False
        self.client = self.app.test_client()
        with self.app.app_context():
            acct = make_account()
            self.a = make_channel(acct, name='FS1 A')
            self.b = make_channel(acct, name='FS1 B')
            grp = make_group('FS1', members=[self.a, self.b], recording=False)
            self.gid = grp.id
            self.aid, self.bid = self.a.id, self.b.id
            db.session.commit()

    def tearDown(self):
        self.t.cleanup()

    def _rows(self):
        r = self.client.get(f'/api/channel-groups/{self.gid}/detail-rows')
        self.assertEqual(r.status_code, 200)
        data = r.get_json()
        return {row['channel_name']: row for row in data['rows']}, data

    def _lock_to(self, resolution, fps):
        """Set the strategy and the lock through the app's own endpoints.

        Deliberately not a direct model write: the test client reuses one scoped session
        across requests, so a group mutated in a test app_context is still served from the
        request session's identity map and the next request reads the pre-lock values.
        Going through the routes is both the honest write path and the one that cannot go
        stale."""
        r = self.client.post(f'/api/channel-groups/{self.gid}/format-strategy',
                             json={'strategy': 'manual'})
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        r = self.client.post(f'/api/channel-groups/{self.gid}/format',
                             json={'resolution': resolution, 'fps': fps})
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))

    def test_rows_carry_both_participation_switches(self):
        """Without these two fields the switch columns have nothing to render."""
        rows, _ = self._rows()
        for row in rows.values():
            self.assertIn('recording_enabled', row)
            self.assertIn('test_enabled', row)
        # make_group(recording=False) is the production default: created as a health check.
        self.assertFalse(rows['FS1 A']['recording_enabled'])
        self.assertTrue(rows['FS1 A']['test_enabled'])

    def test_stored_group_marks_nothing_disabled(self):
        """DECIDED 3. A recording-off member is a choice, not a status - and a disabled row
        carries no select checkbox, which would make it unreachable by the bulk switches."""
        with self.app.app_context():
            m = ChannelGroupMember.query.filter_by(group_id=self.gid, channel_id=self.aid).first()
            m.recording_enabled = False  # participation-write-ok: fixture setup
            db.session.commit()
        rows, _ = self._rows()
        self.assertFalse(any(row['disabled'] for row in rows.values()))
        self.assertIsNone(rows['FS1 A']['disabled_tip'])

    def test_format_blocked_follows_the_lock_and_only_the_lock(self):
        """§16.1's pill names the LOCK, not the derived reference: an unlocked group still
        auto-derives one from its best member, and nothing enforces that."""
        with self.app.app_context():
            from app.database import Channel
            make_channel_test(db.session.get(Channel, self.aid), all_null=False,
                              status='COMPLETED', resolution='1280x720', fps=60.0)
            make_channel_test(db.session.get(Channel, self.bid), all_null=False,
                              status='COMPLETED', resolution='1920x1080', fps=60.0)
            for m in ChannelGroupMember.query.filter_by(group_id=self.gid).all():
                m.recording_enabled = True  # participation-write-ok: fixture setup
            db.session.commit()

        # No strategy set yet (health_check_only manages no format), so nothing is blocked
        # even though the two members genuinely differ.
        rows, data = self._rows()
        self.assertIsNone(data['lock_label'])
        self.assertFalse(any(row['format_blocked'] for row in rows.values()))

        self._lock_to('1920x1080', 60)

        rows, data = self._rows()
        self.assertEqual(data['lock_label'], '1920x1080 @ 60')
        self.assertTrue(rows['FS1 A']['format_blocked'])
        self.assertFalse(rows['FS1 B']['format_blocked'])

    def test_the_duplicates_modal_keeps_the_sitting_out_flag(self):
        """DECIDED 3 reaches the ROW, not every consumer. The duplicates modal is picking
        which of several identical feeds to keep, renders no status pill and no select
        checkbox, and "this one is sitting out" is a fact worth having in that decision."""
        with self.app.app_context():
            from app.database import Channel
            shared = 'http://example.test/live/dup'
            for cid in (self.aid, self.bid):
                db.session.get(Channel, cid).stream_url = shared
            m = ChannelGroupMember.query.filter_by(group_id=self.gid, channel_id=self.bid).first()
            m.test_enabled = False  # participation-write-ok: fixture setup
            db.session.commit()
        _, data = self._rows()
        by_id = {c['channel_id']: c for c in data['dup_groups'][0]['channels']}
        self.assertFalse(by_id[self.aid]['disabled'])
        self.assertTrue(by_id[self.bid]['disabled'])

    def test_zero_survivors_blocks_nobody(self):
        """A lock that would leave nothing is bypassed (§15.2), so no row may claim it was
        skipped - the recording is about to run from one of them."""
        with self.app.app_context():
            from app.database import Channel
            for cid in (self.aid, self.bid):
                make_channel_test(db.session.get(Channel, cid), all_null=False,
                                  status='COMPLETED', resolution='1280x720', fps=60.0)
            for m in ChannelGroupMember.query.filter_by(group_id=self.gid).all():
                m.recording_enabled = True  # participation-write-ok: fixture setup
            db.session.commit()
        self._lock_to('1920x1080', 60)
        rows, _ = self._rows()
        self.assertFalse(any(row['format_blocked'] for row in rows.values()))


class BulkParticipationRouteTests(unittest.TestCase):
    """The bulk half of the switch. Enforcement lives server-side."""

    def setUp(self):
        self.t = make_test_app()
        self.app = self.t.app
        self.app.config['WTF_CSRF_ENABLED'] = False
        self.client = self.app.test_client()
        with self.app.app_context():
            acct = make_account()
            self.chans = [make_channel(acct, name=f'FS1 {i}') for i in range(3)]
            grp = make_group('FS1', members=self.chans, recording=False)
            self.gid = grp.id
            self.ids = [c.id for c in self.chans]
            db.session.commit()

    def tearDown(self):
        self.t.cleanup()

    def _post(self, **body):
        return self.client.post(f'/api/channel-groups/{self.gid}/members/participation/bulk',
                                json=body)

    def test_turns_the_switch_on_for_every_named_member(self):
        r = self._post(field='recording_enabled', channel_ids=self.ids, enabled=True)
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.get_json()['moved'], 3)
        with self.app.app_context():
            self.assertTrue(all(m.recording_enabled for m in
                                ChannelGroupMember.query.filter_by(group_id=self.gid).all()))

    def test_moved_counts_what_changed_not_what_was_submitted(self):
        """A switch already where it was asked to go is not something that happened, and
        the toast reads off this number."""
        self._post(field='recording_enabled', channel_ids=self.ids[:2], enabled=True)
        r = self._post(field='recording_enabled', channel_ids=self.ids, enabled=True)
        self.assertEqual(r.get_json()['moved'], 1)

    def test_every_move_lands_in_the_group_event_log(self):
        """§4.5. A bulk path that wrote the columns itself would move three switches with
        no trace on any surface - which is the defect dev/changelog/748 fixed for the
        single-member path."""
        self._post(field='test_enabled', channel_ids=self.ids, enabled=False)
        with self.app.app_context():
            evs = ChannelGroupEvent.query.filter_by(
                group_id=self.gid, event_type='GROUP_MEMBER_PARTICIPATION').all()
            self.assertEqual(len(evs), 3)
            self.assertTrue(all('Health check turned off' in e.detail for e in evs))

    def test_unknown_field_is_refused(self):
        r = self._post(field='in_guide', channel_ids=self.ids, enabled=True)
        self.assertEqual(r.status_code, 400)
        self.assertIn('in_guide', r.get_json()['error'])

    def test_empty_or_missing_channel_ids_is_refused(self):
        for body in ({'field': 'recording_enabled', 'enabled': True},
                     {'field': 'recording_enabled', 'channel_ids': [], 'enabled': True},
                     {'field': 'recording_enabled', 'channel_ids': 'all', 'enabled': True}):
            with self.subTest(body=body):
                r = self.client.post(
                    f'/api/channel-groups/{self.gid}/members/participation/bulk', json=body)
                self.assertEqual(r.status_code, 400)

    def test_channels_outside_the_group_are_a_404(self):
        r = self._post(field='recording_enabled', channel_ids=[999999], enabled=True)
        self.assertEqual(r.status_code, 404)

    def test_missing_group_is_a_404(self):
        r = self.client.post('/api/channel-groups/999999/members/participation/bulk',
                             json={'field': 'recording_enabled', 'channel_ids': [1],
                                   'enabled': True})
        self.assertEqual(r.status_code, 404)


class MemberListMarkupTests(unittest.TestCase):
    """The controls DECIDED 6 exists for, in the page and in its renderer."""

    def setUp(self):
        self.t = make_test_app()
        self.app = self.t.app
        self.app.config['WTF_CSRF_ENABLED'] = False
        self.client = self.app.test_client()
        with self.app.app_context():
            acct = make_account()
            ch = make_channel(acct, name='FS1 A')
            self.gid = make_group('FS1', members=[ch], recording=False).id
            db.session.commit()

    def tearDown(self):
        self.t.cleanup()

    def test_page_carries_the_filter_bar_and_both_bulk_menus(self):
        """One "+ Filter" chip, not a control per dimension (DESIGN.md 3.11,
        dev/changelog/767) - and it ships unconditionally, because which dimensions are
        offered is the registry's `available()` rather than a Jinja conditional."""
        html = self.client.get(f'/channel-groups/{self.gid}').get_data(as_text=True)
        self.assertIn('id="gd-filter-chips"', html)
        self.assertIn('id="gd-filter-menu"', html)
        self.assertIn('js/filter-bar.js', html)
        for gone in ('id="gd-filter-part"', 'id="gd-filter-status"',
                     'id="gd-filter-account"', 'id="gd-filter-tested"'):
            self.assertNotIn(gone, html, 'a standing filter control is back on the bar')
        self.assertIn('id="gd-bulk-rec"', html)
        self.assertIn('id="gd-bulk-test"', html)
        for verb in ('data-bulk="rec:on"', 'data-bulk="rec:off"',
                     'data-bulk="test:on"', 'data-bulk="test:off"'):
            self.assertIn(verb, html)

    def test_a_group_without_a_check_still_gets_the_selection_bar(self):
        """The bulk switches are a stored group's verbs whether or not it can test."""
        html = self.client.get(f'/channel-groups/{self.gid}').get_data(as_text=True)
        self.assertIn('id="gd-select-all"', html)

    def test_both_columns_are_offered_and_sortable(self):
        from app.routes.channel_groups import GROUP_DETAIL_COLUMNS
        for key in ('check', 'channel'):
            self.assertIn('rec', GROUP_DETAIL_COLUMNS[key])
            self.assertIn('test', GROUP_DETAIL_COLUMNS[key])
        # The system group has no memberships, so it must not advertise either column.
        self.assertNotIn('rec', GROUP_DETAIL_COLUMNS['system'])
        self.assertNotIn('test', GROUP_DETAIL_COLUMNS['system'])

        js = _read('static/js/group-detail.js')
        self.assertIn("rec: true, test: true", js)          # COL_SORTABLE
        self.assertIn("case 'rec': return r.recording_enabled", js)
        self.assertIn("case 'test': return r.test_enabled", js)

    def test_no_participation_control_is_ever_disabled(self):
        """§4.1. Round 2's disabled control is the shape the sixth pass deleted: the lock
        filters at selection time, so the switch stays the user's and the pill explains."""
        js = _read('static/js/group-detail.js')
        cell = js[js.index('function partCell('):js.index('function cell(')]
        self.assertNotIn('disabled', cell)
        css = _read('static/css/style.css')
        self.assertNotIn('.gd-part .switch input[disabled]', css)

    def test_the_row_expand_fallback_does_not_swallow_a_switch_click(self):
        """A .switch paints its .knob over its own input, so a real click lands on the
        knob - a SIBLING of the input, which `closest('input')` misses. The fallback then
        called renderTable() and replaced the tbody before the label's forwarded activation
        reached the live input, so the row expanded and nothing was ever posted
        (dev/docs/BUGS.md 2026-08-19 @ 06:41:09 PM ET).

        This is a SOURCE assertion, not a behavioral one: the defect is a hit-testing and
        event-ordering interaction that jsdom cannot reproduce, and it was found in a real
        browser. It guards the selector against being trimmed back, and nothing more."""
        js = _read('static/js/group-detail.js')
        self.assertIn("closest('a, button, input, label.switch')", js)

    def test_a_full_width_phone_search_needs_its_parent_to_stretch(self):
        """`.card-head-actions .search-wrap { width: 100% }` resolved against a
        shrink-to-fit parent, so the phone search box stayed a ~190px stub floating in a
        full-width card head. Measured at 375px in a browser; jsdom computes no layout, so
        this asserts the rule exists rather than the width it produces."""
        css = _read('static/css/style.css')
        phone = css[css.index('@media (max-width: 768px)'):]
        idx = phone.index('.card-head-actions .search-wrap')
        before = phone[max(0, idx - 700):idx]
        # BOTH are needed: the head must wrap so the actions get their own line, and the
        # actions box must then stretch across it. Either alone leaves the ~191px stub.
        self.assertIn('.card-head { flex-wrap: wrap; }', before)
        self.assertIn('.card-head-actions { width: 100%; margin-left: 0; }', before)

    def test_the_dead_member_toggle_endpoints_are_gone_from_the_client(self):
        """dev/changelog/741 replaced members/enable + members/disable with one
        participation route, and the row kebab kept calling the deleted pair."""
        js = _read('static/js/group-detail.js')
        self.assertNotIn("members/enable", js)
        self.assertNotIn("members/disable", js)


class ParticipationFilterTests(unittest.TestCase):
    """DECIDED 6's deciding factor: filterable by each switch, independently. Since
    dev/changelog/767 the four standing controls are one "+ Filter" chip over the shared
    static/js/filter-bar.js, so each of these is a FILTER_DIMS entry."""

    def test_every_dimension_reaches_the_one_predicate(self):
        """Adding a filter dimension means an entry in the ONE registry - a second
        consumer can shadow rows the first one showed (the guide's health and tag filters
        were each missed by computeVisibleSegments in turn)."""
        js = _read('static/js/group-detail.js')
        matches = js[js.index('function matches(r)'):js.index('function visibleRows(')]
        self.assertIn('filterBar.matches(r)', matches)
        # The search term is the one thing that is NOT a dimension (it is a text box, not
        # a chip), so it stays here - and no row field may be read alongside it.
        for field in ('r.recording_enabled', 'r.test_enabled', 'r.account_id', 'r.last_test'):
            self.assertNotIn(field, matches, f'{field} is filtered outside the registry')

    def test_recording_and_health_check_are_two_dimensions_so_they_keep_anding(self):
        """"Recording on AND not tested" is the setup 16.1 warns about, so it has to be a
        question the list can answer. One "Participation" dimension would OR them."""
        js = _read('static/js/group-detail.js')
        dims = js[js.index('const FILTER_DIMS = ['):js.index('const filterBar = createFilterBar(')]
        self.assertIn("k: 'rec'", dims)
        self.assertIn("k: 'test'", dims)
        self.assertIn('r.recording_enabled', dims)
        self.assertIn('r.test_enabled', dims)

    def test_each_dimension_is_gated_on_the_facet_that_gives_it_meaning(self):
        """The participation switches only exist on a stored group and the status/test-age
        questions only on one with a health check. A dimension offered anyway is a menu row
        that can only ever narrow the list to nothing."""
        js = _read('static/js/group-detail.js')
        dims = js[js.index('const FILTER_DIMS = ['):js.index('const filterBar = createFilterBar(')]
        self.assertEqual(dims.count('available: () => G.hasChannel'), 2)
        self.assertEqual(dims.count('available: () => G.hasCheck'), 2)

    def test_initial_state_is_nothing_active(self):
        """Server-rendered initial state must equal the "nothing active" state - JS may
        upgrade it, never be required to calm it down. The chip row ships holding only the
        + Filter control, and the popover ships empty."""
        tpl = _read('templates/channels/group_detail.html')
        block = tpl[tpl.index('id="gd-filter-chips"'):tpl.index('id="gd-col-menu"')]
        self.assertNotIn('active-filter', block)
        self.assertIn('<div class="menu pop-left" id="gd-filter-menu"></div>', block)

    def test_the_status_filter_ignores_the_transient_testing_overlay(self):
        """A row being re-measured keeps the status it is being re-measured FROM, so a
        filtered list does not blink a row out for the seconds a test sits on it - and so
        the popover's per-value counts stay honest, which they could not be if a row under
        test matched every status at once."""
        js = _read('static/js/group-detail.js')
        fn = js[js.index('function filterStatus(r)'):js.index('function canExpand(')]
        self.assertNotIn('TESTING', fn)
        dims = js[js.index('const FILTER_DIMS = ['):js.index('const filterBar = createFilterBar(')]
        self.assertIn('filterStatus(r) === v', dims)


# ── dev/changelog/756 - the format strategy control and the banner stack ─────
#
# §4.4's eight-value setting had no dropdown, so every group was stuck on the
# health_check_only default and nothing exercised the engine #7 built. §16's banners are
# the whole of what the app says about a questionable setup, since nothing is
# auto-corrected (§4.1) and almost nothing is refused (§4.3, §15) - which makes their
# gating a behavior, not decoration.


class BannerFactsTests(unittest.TestCase):
    """The payload §16's banners are gated and counted on - decided server-side, because
    two answers to "do these members span more than one format" is a disagreement the user
    reads as the page arguing with itself."""

    def setUp(self):
        self.t = make_test_app()
        self.app = self.t.app
        self.app.config['WTF_CSRF_ENABLED'] = False
        self.client = self.app.test_client()
        with self.app.app_context():
            acct = make_account()
            self.a = make_channel(acct, name='FS1 A')
            self.b = make_channel(acct, name='FS1 B')
            self.a.epg_channel_id, self.b.epg_channel_id = 'fs1.us', 'fox1.us'
            grp = make_group('FS1', members=[self.a, self.b], recording=True, in_guide=False)
            self.gid, self.aid, self.bid = grp.id, self.a.id, self.b.id
            db.session.commit()

    def tearDown(self):
        self.t.cleanup()

    def _warn(self):
        r = self.client.get(f'/api/channel-groups/{self.gid}/detail-rows')
        self.assertEqual(r.status_code, 200)
        return r.get_json()['warnings']

    def _measure(self, differing=True):
        from app.database import Channel
        with self.app.app_context():
            make_channel_test(db.session.get(Channel, self.aid), all_null=False,
                              status='COMPLETED', resolution='1920x1080', fps=60.0)
            make_channel_test(db.session.get(Channel, self.bid), all_null=False,
                              status='COMPLETED',
                              resolution='1280x720' if differing else '1920x1080', fps=60.0)
            db.session.commit()

    def _strategy(self, value):
        r = self.client.post(f'/api/channel-groups/{self.gid}/format-strategy',
                             json={'strategy': value})
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        return r.get_json()

    def test_payload_carries_the_banner_facts(self):
        """Without these the banner region renders nothing at all."""
        w = self._warn()
        for key in ('strategy', 'manages_format', 'is_source', 'muted', 'recording_count',
                    'format_blocked_count', 'format_override', 'format_warns',
                    'no_winner', 'epg_ids', 'epg_missing_count'):
            self.assertIn(key, w)

    def test_health_check_only_is_not_a_recording_source(self):
        """§16 gates every warning on the STRATEGY, not on in_guide: a group made purely
        for health checking should not be warned at all."""
        w = self._warn()
        self.assertEqual(w['strategy'], 'health_check_only')
        self.assertFalse(w['is_source'])
        self.assertFalse(w['manages_format'])

    def test_unmanaged_is_a_source_that_manages_no_format(self):
        """The two flags are not the same question, and `unmanaged` is the value that
        separates them - it records, and it enforces nothing."""
        self._strategy('unmanaged')
        w = self._warn()
        self.assertTrue(w['is_source'])
        self.assertFalse(w['manages_format'])

    def test_format_warns_counts_only_recording_enabled_members(self):
        """§16.1: "If record is disabled then no need to show the same warnings, because it
        isn't set to record anyways" - a member sitting out is not part of what this group
        would record, so it is not part of what the warning describes (dev/changelog/925)."""
        self._measure(differing=True)
        self._strategy('manual')
        r = self.client.post(f'/api/channel-groups/{self.gid}/format',
                             json={'resolution': '1920x1080', 'fps': 60})
        self.assertEqual(r.status_code, 200)
        self.assertTrue(self._warn()['format_warns'])

        r = self.client.post(f'/api/channel-groups/{self.gid}/members/participation',
                             json={'channel_id': self.bid, 'field': 'recording_enabled',
                                   'enabled': False})
        self.assertEqual(r.status_code, 200)
        w = self._warn()
        self.assertFalse(w['format_warns'])
        self.assertEqual(w['recording_count'], 1)

    def test_blocked_count_and_override_are_read_together(self):
        """§15.2. `format_blocked_count` is empty under an override by design - the lock
        that left nothing is bypassed, so it blocks nobody - which is exactly why the
        banner reads the two facts rather than deriving one from the other."""
        self._measure(differing=True)
        self._strategy('manual')
        r = self.client.post(f'/api/channel-groups/{self.gid}/format',
                             json={'resolution': '1920x1080', 'fps': 60})
        self.assertEqual(r.status_code, 200)
        w = self._warn()
        self.assertEqual(w['format_blocked_count'], 1)
        self.assertFalse(w['format_override'])

        # Pin a format NO member reports: every willing member is filtered out, so the
        # lock is bypassed rather than the recording abandoned.
        r = self.client.post(f'/api/channel-groups/{self.gid}/format',
                             json={'resolution': '3840x2160', 'fps': 60})
        self.assertEqual(r.status_code, 200)
        w = self._warn()
        self.assertTrue(w['format_override'])
        self.assertEqual(w['format_blocked_count'], 0)
        self.assertIsNotNone(w['override_member_name'])

    def test_epg_tally_separates_missing_from_mismatched(self):
        """§8. A member with no EPG id is unknown, not mismatched - and the banner says so
        rather than reading as an accusation against a feed that simply has no listings."""
        w = self._warn()
        self.assertEqual(sorted(e['epg_channel_id'] for e in w['epg_ids']),
                         ['fox1.us', 'fs1.us'])
        self.assertEqual(w['epg_missing_count'], 0)

        # A third feed with no listings at all. Added through the route rather than by
        # mutating a row in an app_context: the test client reuses one scoped session, so
        # a model write here is served stale by the next request (see _lock_to above).
        from app.database import Account, Channel
        with self.app.app_context():
            acct = db.session.get(Account, Channel.query.get(self.aid).account_id)
            bare = make_channel(acct, name='FS1 C')
            bare.epg_channel_id = None
            db.session.commit()
            bare_id = bare.id
        r = self.client.post(f'/api/channel-groups/{self.gid}/members',
                             json={'channel_ids': [bare_id]})
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        r = self.client.post(f'/api/channel-groups/{self.gid}/members/participation',
                             json={'channel_id': bare_id, 'field': 'recording_enabled',
                                   'enabled': True})
        self.assertEqual(r.status_code, 200)

        w = self._warn()
        self.assertEqual(len(w['epg_ids']), 2, 'the bare member adds no id to the tally')
        self.assertEqual(w['epg_missing_count'], 1)

    def test_no_winner_is_reported_while_it_is_true(self):
        """Right after a database wipe every group is here, so a banner that appeared only
        at the GROUP_FORMAT_STRATEGY_BLOCKED transition would be missing for exactly the
        people who need it."""
        self._strategy('highest_bitrate')
        w = self._warn()
        self.assertTrue(w['no_winner'])
        self.assertTrue(w['no_winner_rationale'])

        self._measure(differing=False)
        self._strategy('highest_bitrate')
        self.assertFalse(self._warn()['no_winner'])

    def test_the_system_group_has_no_banner_facts(self):
        """It has no memberships, so there is nothing about it to warn about."""
        from app.database import ChannelGroup
        with self.app.app_context():
            sid = ChannelGroup.query.filter_by(is_system=True).first().id
        r = self.client.get(f'/api/channel-groups/{sid}/detail-rows')
        self.assertIsNone(r.get_json()['warnings'])


class FloatingFormatDisclosureTests(unittest.TestCase):
    """§16.1's no-lock case (`dev/changelog/765`): a group whose format FLOATS still says
    which members a recording starting right now would leave behind.

    A group under `highest_score` filters nobody, so no row is `format_blocked` and the page
    used to say nothing at all - while §5.1's pin made the divergence real anyway, because a
    run keeps the format of its first segment and cannot fail over to a member reporting
    another one. The server already computed the per-row `mismatch` flag and serialized it;
    nothing rendered it.

    Two facts carry the surface, and both are decided here rather than in the client so the
    dimmed rows and the Group format chip's tally cannot disagree.
    """

    def setUp(self):
        self.t = make_test_app()
        self.app = self.t.app
        self.app.config['WTF_CSRF_ENABLED'] = False
        self.client = self.app.test_client()
        with self.app.app_context():
            acct = make_account()
            self.lead = make_channel(acct, name='FS1 lead')
            self.other = make_channel(acct, name='FS1 1080p')
            self.untested = make_channel(acct, name='FS1 unknown')
            grp = make_group('FS1', members=[self.lead, self.other, self.untested],
                             recording=True, in_guide=False)
            self.gid = grp.id
            self.lead_id, self.other_id = self.lead.id, self.other.id
            self.untested_id = self.untested.id
            db.session.commit()

    def tearDown(self):
        self.t.cleanup()

    def _measure(self, lead_res='1280x720', other_res='1920x1080'):
        """Give the lead the higher bitrate so rank_members puts it on top: the three
        members tie at the unscored neutral, and bitrate is the tie-break before id."""
        from app.database import Channel
        with self.app.app_context():
            make_channel_test(db.session.get(Channel, self.lead_id), all_null=False,
                              status='COMPLETED', resolution=lead_res, fps=59.94,
                              bitrate_kbps=6000.0)
            make_channel_test(db.session.get(Channel, self.other_id), all_null=False,
                              status='COMPLETED', resolution=other_res, fps=59.94,
                              bitrate_kbps=3000.0)
            db.session.commit()

    def _payload(self):
        r = self.client.get(f'/api/channel-groups/{self.gid}/detail-rows')
        self.assertEqual(r.status_code, 200)
        return r.get_json()

    def _rows(self):
        return {row['channel_id']: row for row in self._payload()['rows']}

    def _strategy(self, value):
        r = self.client.post(f'/api/channel-groups/{self.gid}/format-strategy',
                             json={'strategy': value})
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))

    def test_the_diverging_member_is_flagged_with_no_lock_in_force(self):
        """The whole finding: `highest_score` writes no lock, so nothing is filtered and
        `format_blocked` is false on every row - but the 1080p member still cannot be
        failed over to once a run opens at the lead's 720p (§5.1)."""
        self._measure()
        self._strategy('highest_score')
        rows = self._rows()
        self.assertFalse(rows[self.other_id]['format_blocked'],
                         'no lock exists, so nothing may claim the lock filtered it')
        self.assertTrue(rows[self.other_id]['mismatch'])
        self.assertFalse(rows[self.lead_id]['mismatch'],
                         'the reference IS the lead format, so the lead is never an outlier')

    def test_an_untested_member_is_never_flagged(self):
        """Unknown is not proven-different (§5.1) - it stays eligible for failover, so
        dimming it would claim something the app does not know."""
        self._measure()
        self._strategy('highest_score')
        self.assertFalse(self._rows()[self.untested_id]['mismatch'])

    def test_best_format_known_gates_the_whole_surface(self):
        """`effective_score()` gives an untested channel UNSCORED_NEUTRAL, so an untested
        member can lead the ranking - and a run starting there pins to whatever it turns out
        to deliver, which may be neither the displayed reference nor anything the page
        predicted. With no format for the lead there is nothing for the pin to be."""
        self._strategy('highest_score')
        w = self._payload()['warnings']
        self.assertFalse(w['best_format_known'], 'nothing is measured yet')

        self._measure()
        self.assertTrue(self._payload()['warnings']['best_format_known'])

    def test_the_match_count_is_counted_against_the_same_reference(self):
        """The Group format chip's tally and the dimmed rows read one number, so they
        cannot disagree about who is under the format the page displays."""
        self._measure()
        self._strategy('highest_score')
        payload = self._payload()
        w = payload['warnings']
        self.assertEqual(payload['reference_label'], '1280x720 @ 60')
        self.assertEqual(w['recording_count'], 3)
        # The lead matches; the 1080p member does not; the untested one has no format to
        # match with, so it is outside the count without being an outlier.
        self.assertEqual(w['format_match_count'], 1)

    def test_the_match_count_is_none_with_no_reference(self):
        """Nothing measured means no reference, and a tally against nothing is a number the
        user cannot explain."""
        self._strategy('highest_score')
        self.assertIsNone(self._payload()['warnings']['format_match_count'])

    def test_a_locked_group_still_counts_and_still_blocks(self):
        """The loud path is untouched: with a lock the row carries `format_blocked` and
        16.1's amber pill, and the tally counts against the lock."""
        self._measure()
        self._strategy('manual')
        r = self.client.post(f'/api/channel-groups/{self.gid}/format',
                             json={'resolution': '1280x720', 'fps': 60})
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        rows = self._rows()
        self.assertTrue(rows[self.other_id]['format_blocked'])
        self.assertEqual(self._payload()['warnings']['format_match_count'], 1)


class FloatingFormatRenderingTests(unittest.TestCase):
    """The client half of `dev/changelog/765`. Source assertions, in this file's existing
    style: jsdom is not wired up for group-detail.js, and the dim is a CSS class whose
    effect only exists in a real browser."""

    def test_the_quiet_pill_is_not_the_loud_one(self):
        """16.1's amber pill promises "It will not be used until it matches" - an
        enforcement that is not happening without a lock. One label on two meanings is the
        one-flag-one-meaning defect class."""
        js = _read('static/js/group-detail.js')
        warn = js[js.index('function rowWarnings(r)'):js.index('const rowDims =')]
        self.assertIn("pill: 'different format'", warn)
        self.assertIn('note: true', warn)
        # The loud pill stays a b-warn badge and the quiet one never becomes one.
        self.assertEqual(warn.count("cls: 'b-warn'"), 3)

    def test_the_quiet_pill_renders_as_a_note_not_a_badge(self):
        js = _read('static/js/group-detail.js')
        flags = js[js.index('function rowFlags(r)'):js.index('function nameCell(r)')]
        self.assertIn('gd-note tip-plain', flags)
        self.assertIn('badge ${w.cls} gd-offpill', flags)

    def test_both_renderers_dim_from_one_reader(self):
        """The desktop row and the phone card must not dim different members - the phone
        card is a second renderer and 375px is where a drift would live unseen."""
        js = _read('static/js/group-detail.js')
        self.assertEqual(js.count('const dim = rowDims(r);'), 2)
        self.assertEqual(js.count('const rowDims = (r) =>'), 1)

    def test_both_renderers_build_their_flags_from_one_authority(self):
        js = _read('static/js/group-detail.js')
        self.assertEqual(js.count('function rowFlags(r)'), 1)
        self.assertIn('h += rowFlags(r).join', js)
        self.assertIn('flags.push(...rowFlags(r))', js)

    def test_the_predicate_carries_all_three_honesty_gates(self):
        """Each is a case where the page would otherwise state something it cannot know:
        an unmeasured lead, a lock that owns the row, and a bypassed lock (§15.2)."""
        js = _read('static/js/group-detail.js')
        fn = js[js.index('function floatingMismatch(r)'):js.index('// Everything questionable')]
        self.assertIn('WARN.best_format_known', fn)
        self.assertIn('!r.format_blocked', fn)
        self.assertIn('!WARN.format_override', fn)
        self.assertIn('WARN.is_source', fn)

    def test_no_red_banner_fires_for_a_floating_reference(self):
        """§16.2: the banners "clear on their own once the group is set up cleanly", and a
        group assembled from every FS1 feed a provider carries is already clean - §7 calls
        that the default shape of a new group. A banner it could never satisfy would be
        crying wolf. Since dev/changelog/925 the region fires only for a hand-pinned format,
        and the `unmanaged` branch is gone."""
        js = _read('static/js/group-detail.js')
        banners = js[js.index('function renderBanners()'):js.index('function muteWarning(')]
        mixed = banners[banners.index('let mixed ='):banners.index("set('gd-format-banner'")]
        self.assertIn('WARN.format_warns && n', mixed)
        self.assertNotIn('format_spans', mixed)
        self.assertNotIn('manages_format', mixed)
        self.assertNotIn('floatingMismatch', mixed)

    def test_the_chip_states_how_many_members_match(self):
        """The rows dim one at a time and a group this size is scrolled, so without the
        count the page states a format and leaves the reader to tally who is under it."""
        js = _read('static/js/group-detail.js')
        self.assertIn('WARN.format_match_count', js)
        self.assertIn('of ${WARN.recording_count} match', js)


class WarningMuteTests(unittest.TestCase):
    """§16.2: every banner is mutable per group, recorded, and re-armable."""

    def setUp(self):
        self.t = make_test_app()
        self.app = self.t.app
        self.app.config['WTF_CSRF_ENABLED'] = False
        self.client = self.app.test_client()
        with self.app.app_context():
            acct = make_account()
            ch = make_channel(acct, name='FS1 A')
            self.gid = make_group('FS1', members=[ch], recording=True).id
            db.session.commit()

    def tearDown(self):
        self.t.cleanup()

    def _post(self, warnings):
        return self.client.post(f'/api/channel-groups/{self.gid}/warnings',
                                json={'warnings': warnings})

    def _muted(self):
        return self.client.get(
            f'/api/channel-groups/{self.gid}/detail-rows').get_json()['warnings']['muted']

    def test_hiding_and_re_arming_round_trip(self):
        """"Hide this warning" must not be a one-way door - Settings > Warnings is the
        re-entry point, and it posts through this same route."""
        self.assertEqual(self._muted(), [])
        r = self._post({'format': False})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.get_json()['muted'], ['format'])
        self.assertEqual(self._muted(), ['format'])

        r = self._post({'format': True})
        self.assertEqual(r.get_json()['muted'], [])
        self.assertEqual(self._muted(), [])

    def test_a_kind_not_sent_is_left_alone(self):
        """The banner's own Hide button posts a single-entry map. Treating an absent kind
        as re-armed would let hiding one warning silently un-hide another."""
        self._post({'format': False, 'epg': False})
        self._post({'override': False})
        self.assertEqual(self._muted(), ['format', 'epg', 'override'])

    def test_the_value_is_what_the_user_sees_not_what_is_stored(self):
        """The switch reads "shown"; the column stores "muted". The flip happens in the
        route, because a UI that has to remember to invert a boolean will forget."""
        self._post({'epg': False})
        self.assertEqual(self._muted(), ['epg'])

    def test_an_unknown_kind_is_a_400(self):
        """Enforcement lives server-side, never in whichever control happened to post."""
        self.assertEqual(self._post({'bogus': False}).status_code, 400)
        r = self.client.post(f'/api/channel-groups/{self.gid}/warnings', json={})
        self.assertEqual(r.status_code, 400)

    def test_every_move_writes_its_own_event(self):
        """§4.5: a warning cannot go quiet without the group's Activity Timeline saying who
        silenced it and when. A no-op writes nothing - a switch that did not move is not
        something that happened."""
        from app.database import GROUP_WARNING_MUTED
        self._post({'format': False})
        self._post({'format': False})       # no-op
        self._post({'format': True})
        with self.app.app_context():
            evs = (ChannelGroupEvent.query
                   .filter_by(group_id=self.gid, event_type=GROUP_WARNING_MUTED)
                   .order_by(ChannelGroupEvent.id).all())
            self.assertEqual(len(evs), 2)
            self.assertIn('hidden', evs[0].detail)
            self.assertIn('turned back on', evs[1].detail)

    def test_the_mute_dies_with_the_group(self):
        """It is a column, not a UserPref keyed on group id - so teardown is free rather
        than something delete_group has to remember (CLAUDE.md teardown rule)."""
        from app.database import ChannelGroup
        self._post({'format': False})
        r = self.client.post(f'/api/channel-groups/{self.gid}/delete')
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        with self.app.app_context():
            self.assertIsNone(db.session.get(ChannelGroup, self.gid))

    def test_the_system_group_has_nothing_to_hide(self):
        from app.database import ChannelGroup
        with self.app.app_context():
            sid = ChannelGroup.query.filter_by(is_system=True).first().id
        r = self.client.post(f'/api/channel-groups/{sid}/warnings',
                             json={'warnings': {'format': False}})
        self.assertEqual(r.status_code, 400)


class StrategyControlMarkupTests(unittest.TestCase):
    """The control itself, and the regions its banners render into."""

    def setUp(self):
        self.t = make_test_app()
        self.app = self.t.app
        self.app.config['WTF_CSRF_ENABLED'] = False
        self.client = self.app.test_client()
        with self.app.app_context():
            acct = make_account()
            ch = make_channel(acct, name='FS1 A')
            self.gid = make_group('FS1', members=[ch], recording=False).id
            db.session.commit()

    def tearDown(self):
        self.t.cleanup()

    def test_the_page_carries_all_five_banner_regions(self):
        html = self.client.get(f'/channel-groups/{self.gid}').get_data(as_text=True)
        for region in ('gd-banner-explainer', 'gd-format-banner', 'gd-override-banner',
                       'gd-epg-banner', 'gd-nowinner-banner'):
            self.assertIn(f'id="{region}"', html)

    def test_banner_regions_start_empty(self):
        """Server-rendered initial state must equal the "nothing active" state. The gating
        reads live participation switches and a live lock, so a Jinja copy would go stale
        the moment a switch is clicked."""
        html = self.client.get(f'/channel-groups/{self.gid}').get_data(as_text=True)
        self.assertIn('id="gd-banner-explainer" style="display:none;"></div>', html)
        self.assertIn('id="gd-format-banner" style="display:none;"></div>', html)
        self.assertIn('id="gd-nowinner-banner" style="display:none;"></div>', html)

    def test_the_explainer_sits_above_the_stack_it_explains(self):
        """DESIGN-channel-groups-model.md:1108: the explainer is one region above the
        banner stack, not repeated inside each mutable banner - regression for the
        group-model-rebuild-review finding that it was appended into three banners at
        once (dev/changelog/850)."""
        html = self.client.get(f'/channel-groups/{self.gid}').get_data(as_text=True)
        self.assertLess(html.index('id="gd-banner-explainer"'), html.index('id="gd-format-banner"'))
        js = _read('static/js/group-detail.js')
        fn = js[js.index('function renderBanners()'):js.index('function muteWarning(')]
        self.assertEqual(fn.count('bannerNote()'), 1,
                          'bannerNote() must be called exactly once, into the shared region')
        self.assertIn("set('gd-banner-explainer'", fn)

    def test_the_page_ships_the_banner_facts_to_the_renderer(self):
        html = self.client.get(f'/channel-groups/{self.gid}').get_data(as_text=True)
        self.assertIn('warnings:', html)

    def test_all_eight_strategies_are_offered_with_help_text(self):
        """§4.4. Every value ships with one plain sentence, not just `balanced` - a setting
        whose owner cannot say what it does is a number the user cannot explain."""
        js = _read('static/js/format-plan.js')
        block = js[js.index('const GROUP_FORMAT_STRATEGIES'):js.index('function groupStrategyLabel')]
        for value in ('health_check_only', 'highest_score', 'highest_bitrate',
                      'highest_resolution', 'most_channels', 'balanced', 'manual', 'unmanaged'):
            self.assertIn(f"'{value}'", block)
        # Three columns per entry: key, dropdown label, one sentence of help.
        self.assertEqual(block.count('],\n'), 8)

    def test_the_strategy_list_matches_the_server(self):
        """A value the client offers that the server refuses is a dropdown entry that 400s."""
        from app.database import GROUP_FORMAT_STRATEGIES
        js = _read('static/js/format-plan.js')
        block = js[js.index('const GROUP_FORMAT_STRATEGIES'):js.index('function groupStrategyLabel')]
        for value in GROUP_FORMAT_STRATEGIES:
            self.assertIn(f"['{value}'", block)

    def test_the_dead_auto_disable_control_is_gone(self):
        """It read a payload key deleted in dev/changelog/741 (so it always painted Off)
        and posted to a route deleted in the same change, which 404'd the whole Save."""
        js = _read('static/js/group-detail.js')
        self.assertNotIn('gd-autodis', js)
        self.assertNotIn("api('auto-disable')", js)
        self.assertNotIn('auto_disable', js)

    def test_the_settings_chip_says_where_the_format_came_from(self):
        """A format the strategy chose overnight and a format the user pinned look
        identical otherwise, and only one of them will still be there tomorrow."""
        js = _read('static/js/group-detail.js')
        for origin in ('pinned by you', 'not enforced', 'selected automatically by',
                       'follows the healthiest member'):
            self.assertIn(origin, js)

    def test_the_banners_render_from_one_updater_called_on_refresh(self):
        """One updater per DOM region: moving a Recording switch changes who is
        format-blocked, so the banners are re-decided on the same refresh the table is."""
        js = _read('static/js/group-detail.js')
        refresh = js[js.index('function refreshRows()'):js.index('function updateHero(')]
        self.assertIn('WARN = data.warnings', refresh)
        self.assertIn('renderBanners()', refresh)
        self.assertEqual(js.count('function renderBanners()'), 1)

    def test_every_banner_button_resolves_to_a_handler(self):
        """A banner exists to be acted on, so an action it offers that nothing implements is
        worse than one it does not offer - §16.1 refuses a dead knob one control down, and the
        approved mockup left "Review members" as a placeholder toast because a mockup has no
        member list to filter. Found by the browser pass, which noticed the button was missing
        from the shipped banner entirely (dev/changelog/756)."""
        js = _read('static/js/group-detail.js')
        banners = js[js.index('function renderBanners()'):js.index('function muteWarning(')]
        offered = set(re.findall(r'data-act="([a-z-]+)"', banners))
        self.assertIn('review-members', offered, 'the members-naming banners must open them')
        dispatch = js[js.index('function pageAction(act, el)'):]
        for act in offered:
            self.assertIn(f"case '{act}':", dispatch, f'banner offers {act} with no handler')

    def test_review_members_filters_to_the_set_the_banners_count(self):
        """Every §16 banner counts the RECORDING-ENABLED members, so the button that puts them
        in front of you has to show that same set - a different one would answer a question the
        banner did not ask."""
        js = _read('static/js/group-detail.js')
        fn = js[js.index('function reviewMembers('):js.index('function muteWarning(')]
        self.assertIn("filterBar.toggle('rec', 'on')", fn)
        # Replaces what was filtered rather than intersecting with it: the banner names one
        # set, so the list has to end up showing that set and not what is left after some
        # filter from earlier also applies.
        self.assertIn('filterBar.clear()', fn)
        self.assertLess(fn.index('filterBar.clear()'), fn.index("filterBar.toggle('rec', 'on')"))
        # apply(), so the chip row, the phone's count and the list are all redrawn: since
        # dev/changelog/758 the drawing is chosen by the breakpoint, and moving the state
        # without redrawing would leave a phone showing the unfiltered card list under a
        # banner claiming it had been filtered.
        self.assertIn('filterBar.apply()', fn)
        self.assertIn('scrollIntoView', fn)

    def test_a_running_health_check_is_not_painted_the_recording_red(self):
        """§17.1 group C. `.b-live` is the recording red; a check doing its job is not an
        alarm."""
        css = _read('static/css/style.css')
        self.assertIn('.b-running {', css)
        for tpl in ('templates/channels/group_detail.html', 'templates/channels/groups.html',
                    'templates/channels/detail.html'):
            html = _read(tpl)
            self.assertNotIn("b-live'", html)
            self.assertNotIn('badge b-live"', html)

    def test_every_class_the_port_authored_has_a_rule(self):
        """An undefined custom property or class fails silently, which has shipped two
        invisible-UI bugs. `.cg-auto-details` had callers in two shipped JS files and a
        rule in none."""
        css = _read('static/css/style.css')
        for cls in ('.b-running', '.gd-strategy-help', '.gd-bannote', '.gd-ban-acts',
                    '.gd-epg-list', '.cg-auto-details'):
            self.assertIn(f'{cls} ', css, f'{cls} is used but has no rule')


class GuideInvariantPageTests(unittest.TestCase):
    """§15 and §14.1's client half (`dev/changelog/757`). The routes enforce; these hold
    the page to naming what happened rather than letting a knob slide silently."""

    def test_every_gated_path_reads_the_one_error_handler(self):
        """The single switch, the bulk switch, the removal and the dedup submit are four
        ways into the same refusal, and a fifth wording of it is a fifth chance to describe
        the same action differently. All four go through handleInvariantError()."""
        js = _read('static/js/group-detail.js')
        self.assertEqual(1, js.count('function handleInvariantError('))
        self.assertEqual(1, js.count('function confirmLastMember('))
        for fn, end in (('function changePart(', 'function bulkPart('),
                        ('function bulkPart(', '── Bulk selection'),
                        ('function removeMember(', 'function pageAction('),
                        ('function submitDedup(', 'function openDedup()')):
            body = js[js.index(fn):js.index(end)]
            self.assertIn('handleInvariantError(', body,
                          f'{fn} can empty the recording-enabled set without the dialog')

    def test_the_retry_carries_the_confirm_the_server_asked_for(self):
        """A retry that dropped `confirm` would loop on the same 409 forever, which reads
        to the user as a control that does nothing."""
        js = _read('static/js/group-detail.js')
        for call in ('changePart(cid, which, on, true)', 'bulkPart(which, on, true)',
                     'removeMember(r, true)', 'submitDedup(removals, transfer, true)'):
            self.assertIn(call, js, f'{call} is how that path re-submits after the confirm')

    def test_the_guide_button_opens_the_walkthrough_rather_than_refusing(self):
        """§14.1: the guide button on a health-check-only group asks the format question
        and then does what was clicked. It is a trigger, not a veto."""
        js = _read('static/js/group-detail.js')
        case = js[js.index("case 'guide-toggle':"):js.index("case 'walkthrough':")]
        self.assertIn('needsPromotion()', case)
        self.assertIn("openWalkthrough('guide')", case)
        self.assertIn('handleInvariantError(', case,
                      'and the server refusal still has to reach a dialog')

    def test_the_first_recording_switch_triggers_the_walkthrough(self):
        js = _read('static/js/group-detail.js')
        change = js[js.index('function changePart('):js.index('function bulkPart(')]
        self.assertIn("openWalkthrough('member', [cid])", change)
        bulk = js[js.index('function bulkPart('):js.index('── Bulk selection')]
        self.assertIn("openWalkthrough('bulk', ids)", bulk,
                      'the bulk action is the same trigger reached from a selection')

    def test_the_walkthrough_submits_one_request(self):
        """Four endpoints behind a Promise.all is how dev/changelog/756's Save silently
        discarded three writes, and a half-promoted group is a genuinely bad state."""
        # The file's own header explains why, so the scan is of the CODE only - a comment
        # naming the rejected shape is not the rejected shape.
        js = _read('static/js/group-promote-modal.js')
        code = js[js.index('function openPromoteModal('):]
        self.assertEqual(1, code.count('jsonFetch('),
                         'the promotion is one call, not one per decision')
        self.assertIn('/promote`', code)
        self.assertNotIn('Promise.all', code)

    def test_the_walkthrough_reads_the_servers_bucket_table(self):
        """A mockup with no server had to reimplement the bucket engine in JS. Here that
        would be a second answer to "which format wins" for the user to catch disagreeing
        with the first."""
        js = _read('static/js/group-promote-modal.js')
        for helper in ('fetchFormatPlan(', 'formatPlanEntry(', 'formatPlanTable(',
                       'formatPlanSummary(', 'GROUP_FORMAT_STRATEGIES'):
            self.assertIn(helper, js, f'{helper} is format-plan.js\'s, not a local copy')

    def test_the_walkthrough_never_offers_health_check_only(self):
        """This dialog is open because the user reached for a recording action; offering
        "not a recording source" would be offering to do nothing."""
        js = _read('static/js/group-promote-modal.js')
        self.assertIn("filter(([k]) => k !== 'health_check_only')", js)

    def test_the_walkthrough_names_the_combination_not_each_half(self):
        """§14.1: "record from all" plus "stop checking the unmatched" is one bad outcome,
        and a user reading either half alone cannot see it."""
        js = _read('static/js/group-promote-modal.js')
        self.assertIn("state.enable === 'all' && state.unmatched === 'stop'", js)
        self.assertIn('without being monitored', js)

    def test_the_walkthrough_is_loaded_by_the_page(self):
        html = _read('templates/channels/group_detail.html')
        self.assertIn('js/group-promote-modal.js', html)
        self.assertLess(html.index('js/format-plan.js'), html.index('js/group-promote-modal.js'),
                        'it renders with format-plan.js\'s helpers')

    def test_the_broken_guide_banner_is_not_mutable(self):
        """§16.2: hiding a warning about a configuration you accepted is reasonable;
        hiding the explanation for why the thing is not working is not."""
        js = _read('static/js/group-detail.js')
        banners = js[js.index('function renderBanners()'):js.index('function reviewMembers(')]
        broken = banners[banners.index("if (WARN.guide_broken)"):banners.index("set('gd-broken-banner'")]
        self.assertNotIn('muteBtn(', broken)
        self.assertIn("data-act=\"walkthrough\"", broken, 'it offers the fix')
        self.assertIn("data-act=\"guide-toggle\"", broken, 'and the other way out')

    def test_the_unmonitored_banner_moved_off_the_template(self):
        """It was server-rendered, so it went on claiming a member was unmonitored after
        its Health check switch was turned back on, until the page was reloaded
        (handed forward by dev/changelog/756)."""
        html = _read('templates/channels/group_detail.html')
        self.assertNotIn('payload.unmonitored_count', html,
                         'a Jinja copy goes stale the moment a switch is clicked')
        self.assertIn('id="gd-unmonitored-banner"', html)
        js = _read('static/js/group-detail.js')
        self.assertIn("set('gd-unmonitored-banner'", js)
        self.assertIn('WARN.unmonitored_count', js)

    def test_the_clone_screen_never_offers_the_guide(self):
        """A copy's members take the model defaults, so Recording is off on every one of
        them and a guide row would have nothing behind it at the moment of creation."""
        js = _read('static/js/create-group-modal.js')
        self.assertNotIn('cg-guide', js, 'the switch is gone, not merely defaulted off')
        self.assertIn('in_guide: false', js)

    def test_creating_a_group_from_a_selection_offers_its_health_check(self):
        """§14: a group is created as a health check. Every format strategy answers "run a
        check first" until one has, so the schedule is offered in the same flow rather than
        left as a second trip to the group page.

        That chain moved out of group-modal.js and into the group-create flow, which is now
        the channel search's create path (dev/changelog/831) - the check is its last screen
        rather than something offered after the group already exists."""
        js = _read('static/js/group-create-flow.js')
        self.assertIn('openCreateCheckModal(', js)
        search = _read('static/js/channel-search.js')
        self.assertIn('openGroupCreateFlow(', search, 'the Browse tab opens it')
        self.assertIn('checkOpts:', search, 'and supplies what that last screen needs')

    def test_the_group_modal_no_longer_carries_a_second_create_then_check_chain(self):
        """It had one for the channel search's old create path. That path is the flow now,
        and a second implementation of "create, then offer a check" is exactly the pair of
        diverging copies the shared modals exist to prevent."""
        js = _read('static/js/group-modal.js')
        self.assertNotIn('checkOpts', js)
        self.assertNotIn('openCreateCheckModal', js)


class PhoneLayoutTests(unittest.TestCase):
    """The member list at 375px, ported from `dev/mockups/34-group-detail-mobile.html`
    (approved round 2.1) under §17's porting contract - `dev/changelog/758`.

    Before it, the nine-column table was the only drawing: at 375px the two participation
    switches this whole rebuild exists to expose sat off the right edge of a sideways
    scroll, which DESIGN.md §9.5 makes the exception rather than the answer.

    **jsdom computes no layout and answers `matchMedia` with `matches: false` always**, so
    everything geometric here - the ~44px tap target, the band, the reveal, the card
    arrangement - is the browser pass's job and not assertable in this suite. What IS
    assertable is the structure that makes the geometry reachable, and every case below is
    a decision a careless edit would quietly undo.
    """

    def setUp(self):
        self.js = _read('static/js/group-detail.js')
        self.css = _read('static/css/style.css')
        self.tpl = _read('templates/channels/group_detail.html')

    # ── One module, one state, two drawings ──────────────────────────────────

    def test_one_module_draws_both_widths(self):
        """A second template would be a second copy of the state, the filters, the sort and
        every write path, and the two would disagree. The table and the card list are in one
        template and one module, exactly as `static/js/channel-search.js` settled it."""
        self.assertIn('id="gd-tbody"', self.tpl)
        self.assertIn('id="gd-list"', self.tpl)
        self.assertEqual(self.tpl.count("filename='js/group-detail.js'"), 1)

    def test_the_breakpoint_is_spelled_once_and_matches_the_stylesheet(self):
        """Two spellings of 768 is how the JS comes to draw cards while the CSS is still
        showing the table."""
        self.assertEqual(self.js.count("matchMedia("), 1)
        self.assertIn("matchMedia('(max-width: 768px)')", self.js)
        self.assertIn('.gm-list, .gd-phonebar, .gm-botpad { display: none; }', self.css)

    def test_render_list_is_the_only_entry_point(self):
        """Every caller goes through the dispatcher. A caller reaching `renderTable()`
        directly would redraw the half the user is not looking at - the phone would keep
        showing a stale card list under a control that claimed to have changed it."""
        lines = self.js.split('\n')
        start = next(i for i, l in enumerate(lines) if l.startswith('  function renderList()'))
        end = next(i for i, l in enumerate(lines) if i > start and l == '  }')
        inside = range(start, end + 1)
        self.assertIn('renderTable();', '\n'.join(lines[start:end + 1]))
        stray = [f'{i + 1}: {l.strip()}' for i, l in enumerate(lines)
                 if i not in inside
                 and ('renderTable()' in l or 'renderCards()' in l)
                 and not l.startswith('  function ')]
        self.assertEqual(stray, [], 'call renderList(); the breakpoint picks the drawing')

    def test_the_drawing_not_in_use_is_emptied(self):
        """Not merely hidden. A populated-but-hidden table is a second copy of every row in
        the accessibility tree, and it scales with the member count."""
        fn = self.js[self.js.index('function renderList()'):self.js.index('// ── The phone')]
        self.assertIn("byId('gd-tbody').innerHTML = ''", fn)
        self.assertIn("byId('gd-list').innerHTML = ''", fn)

    def test_crossing_the_breakpoint_redraws(self):
        """A phone rotated to landscape, or a window dragged narrow, would otherwise keep
        whichever drawing it booted with while the stylesheet moved under it."""
        self.assertIn("MOBILE_MQ.addEventListener('change', onBreakpointChange)", self.js)
        self.assertIn('if (!isPhone() && selecting)', self.js,
                      'selection mode is a phone concept and must not survive leaving it')

    # ── The three measured decisions (§17.1) ─────────────────────────────────

    def test_the_reveal_is_a_transform_and_never_display_none(self):
        """An unmounted element cannot animate back, and this one moves on every scroll past
        the inline bar."""
        rule = self.css[self.css.index('.actbar-reveal {'):self.css.index('.actbar-reveal.on')]
        self.assertIn('transform: translateY(', rule)
        self.assertNotIn('display:', rule)
        self.assertIn('.actbar-reveal.on { transform: translateY(0); }', self.css)

    def test_the_bottom_padding_is_reserved_unconditionally(self):
        """Coupling the page's height to the bar's visibility lets showing the bar change
        the scroll height underneath the very observer deciding whether to show it."""
        self.assertNotIn('gm-botpad', self.js,
                         'no code path may toggle it - it is CSS-only on purpose')
        self.assertIn('.gm-botpad { display: block; height: 76px; }', self.css)

    def test_the_switch_row_is_padded_for_a_thumb(self):
        """11px, not 7px: measured at 375px the row came out ~35px tall at 7px, and these two
        switches are the controls the whole page exists to set. 11px puts it at ~44px."""
        rule = self.css[self.css.index('  .gm-part {'):self.css.index('  .gm-part:last-child')]
        self.assertIn('padding: 11px 0;', rule)

    def test_the_cards_buttons_do_not_match_the_surface_under_them(self):
        """`.btn`'s background is `--bg2` and so is `.gm-card`'s, so only the border said
        they were controls at all."""
        rule = self.css[self.css.index('  .gm-acts .btn {'):self.css.index('  .gm-acts .btn:hover')]
        self.assertIn('background: var(--bg3);', rule)
        self.assertIn('justify-content: center;', rule)

    # ── One renderer per bar, one action list ────────────────────────────────

    def test_the_sticky_bar_clones_the_inline_one(self):
        """The guide button's label changes with state ("+ Add to Guide" / "Remove from
        Guide"), and two renderers of a control that changes is two controls that can
        disagree. The sticky bar is built from the inline bar's own nodes."""
        fn = self.js[self.js.index('function renderBottomBar()'):self.js.index('function setBar(')]
        self.assertIn("byId('gd-inline-actions')", fn)
        self.assertIn('cloneNode(true)', fn)
        self.assertNotIn('Add to Guide', fn, 'the label is cloned, never re-typed here')

    def test_the_bar_carries_only_what_fits_it(self):
        """Found in the browser at 375px: cloning every inline action put five buttons in the
        bar, which then scrolled sideways with two reachable and three discoverable only by
        swiping a strip nothing says is scrollable. `.mobile-actbar`'s `flex: 1` cannot
        divide a fixed width among an unbounded number of buttons, so what is bounded is the
        number. All four dropped ones stay one tap away in the overflow sheet."""
        fn = self.js[self.js.index('function renderBottomBar()'):self.js.index('function setBar(')]
        self.assertIn("el.classList.contains('gd-phone-hide')", fn)
        bar = self.tpl[self.tpl.index('class="gd-ab-actions"'):self.tpl.index('<div class="menu" id="gd-kebab">')]
        for act in ('data-act="suggest"', 'data-act="create-check"', 'data-act="create-group"'):
            block = bar[bar.index(act) - 200:bar.index(act)]
            self.assertIn('gd-phone-hide', block, f'{act} is still on the phone bar')
        self.assertIn('gd-phone-hide', bar[bar.index("add_to_group=group.id") - 200:],
                      '+ Add Channels is still on the phone bar')
        self.assertIn('data-act="guide-toggle"', bar)
        self.assertNotIn('gd-phone-hide', bar[bar.index('data-act="guide-toggle"') - 120:
                                              bar.index('data-act="guide-toggle"')],
                         'the guide toggle is one of the three the bar keeps')

    def test_the_kebab_is_not_cloned_into_the_bar(self):
        """It would put a second `id="gd-kebab"` in the document, and a menu anchored to a
        bottom-pinned button opens downward off screen."""
        fn = self.js[self.js.index('function renderBottomBar()'):self.js.index('function setBar(')]
        self.assertIn("classList.contains('menu-wrap')", fn)
        self.assertIn("more.dataset.act = 'overflow'", fn)

    def test_the_overflow_sheet_reads_the_desktop_kebab(self):
        """So a phone cannot be missing an action the desktop offers. Re-listing them here
        is how the two come to differ by one item nobody notices."""
        fn = self.js[self.js.index('function openOverflow()'):self.js.index('function openBulkSheet()')]
        self.assertIn("byId('gd-kebab')", fn)
        self.assertIn('Array.from(kebab.children)', fn)
        self.assertIn('closeSheet();', fn, 'the sheet closes before its action runs')

    def test_selection_forces_the_bar_visible(self):
        """Its verbs exist nowhere else on the page, so it must not hide itself just because
        the user happens to be scrolled to the top."""
        self.assertIn('function syncBar() { setBar(selecting || inlineOffScreen); }', self.js)

    def test_both_reveal_fallbacks_resolve_toward_showing(self):
        """A fallback that hid the bar could strand the page's primary action off screen with
        no way to reach it."""
        fn = self.js[self.js.index('function initBarReveal()'):self.js.index('function renderSettingsBar()')]
        self.assertIn("if (!inline || typeof IntersectionObserver === 'undefined')", fn)
        head = fn[fn.index('undefined'):fn.index('const observer')]
        self.assertIn('inlineOffScreen = true;', head)
        self.assertIn("window.addEventListener('pagehide'", fn,
                      'a live observer toggling a bar on a page you have left is a floating control')

    # ── The card itself ──────────────────────────────────────────────────────

    def test_the_band_uses_the_same_five_tokens_the_group_rows_use(self):
        """A third spelling of one convention is how the three drift. `.grp-item` and the
        recordings rows already key a left border off `data-health`. The running state is
        `st-run`/`--accent` here, not `st-live`/`--live` - a health check running now is not
        a failure, and the recording-alarm red made it read as one (dev/docs/BUGS.md
        2026-08-26, dev/changelog/816)."""
        for state, token in (('st-ok', '--ok'), ('st-warn', '--warn'), ('st-bad', '--bad'),
                             ('st-run', '--accent'), ('st-none', '--text-faint')):
            self.assertIn(f'.gm-card[data-health="{state}"]', self.css)
            grp = self.css[self.css.index(f'.grp-item[data-health="{state}"]'):]
            self.assertIn(token, grp[:grp.index('\n')])
            card = self.css[self.css.index(f'.gm-card[data-health="{state}"]'):]
            self.assertIn(token, card[:card.index('\n')])

    def test_a_channel_being_tested_does_not_read_as_failed(self):
        """dev/docs/BUGS.md 2026-08-26: the TESTING state's badge and left-edge band both
        used to be the recording-alarm red (`.b-concat`'s tone and `st-live`/`--live`), so a
        channel simply being tested by a health check looked exactly like one that had
        failed. Both must use the blue `.b-running` treatment (dev/changelog/816), which
        `.gm-card`'s CSS (asserted above) resolves `st-run` to."""
        js = self.js
        badge_fn = js[js.index('function statusBadge('):js.index('function scoreCell(')]
        self.assertIn("if (st === 'TESTING') return '<span class=\"badge b-running\">", badge_fn)
        self.assertNotIn('b-concat', badge_fn)
        band_fn = js[js.index('function healthBand('):js.index('function partRow(')]
        self.assertIn("case 'TESTING': return 'st-run';", band_fn)

    def test_no_participation_control_on_a_card_is_ever_disabled(self):
        """§4.1, same as the desktop column: the lock filters where members are chosen and
        writes nothing, so a member it skips keeps a live control and a pill explains it."""
        fn = self.js[self.js.index('function partRow('):self.js.index('function memberCard(')]
        self.assertNotIn('disabled', fn)
        self.assertIn('class="switch"', fn, 'the shared component, not a hand-rolled toggle')

    def test_each_switch_row_carries_its_own_label(self):
        """There is no column header above it, so a bare switch is unanswerable."""
        fn = self.js[self.js.index('function partRow('):self.js.index('function memberCard(')]
        self.assertIn('gm-part-lbl', fn)
        self.assertIn('COL_LABEL[which]', fn, 'the same two words the column header uses')

    def test_the_card_carries_the_same_pills_as_the_desktop_row(self):
        """Built from `rowFlags()` over `rowWarnings()`, so there is one authority for what a
        member is flagged for and the two drawings cannot warn about different things. The
        dim reads `rowDims()` for the same reason - it covers both the locked and the
        floating case since dev/changelog/765, and the phone card is where a drift between
        the two renderers would live unseen."""
        fn = self.js[self.js.index('function memberCard('):self.js.index('function renderCards()')]
        self.assertIn('rowFlags(r)', fn)
        self.assertIn('const dim = rowDims(r);', fn,
                      'the unusable name still dims, and only the switched-on ones')
        dims = self.js[self.js.index('const rowDims = (r) ='):self.js.index('function rowFlags(r)')]
        self.assertIn('r.recording_enabled', dims)
        self.assertIn('r.format_blocked', dims)

    def test_the_card_never_hand_rolls_a_component_the_app_ships(self):
        """Eleven of the twelve mockup revisions were this defect. The card holds the app's
        own switch, badge, button and empty state."""
        fn = self.js[self.js.index('function memberCard('):self.js.index('// The single entry point')]
        self.assertIn('class="badge ', fn)
        self.assertIn('class="btn', fn)
        self.assertIn("'<div class=\"empty-state\">No channels match.</div>'", fn)

    # ── The Account field, on both widths ────────────────────────────────────

    def test_the_account_field_is_hideable_on_every_facet(self):
        """"Imagine a scenario where a user only has 1 account (will be true for many). Not
        being able to hide that field is a waste of space." So it is a real entry in the
        shared visibility set, not a phone-only extra."""
        from app.routes.channel_groups import GROUP_DETAIL_COLUMNS, GROUP_DETAIL_COLUMNS_OFF
        for facet, cols in GROUP_DETAIL_COLUMNS.items():
            self.assertIn('account', cols, f'{facet} cannot hide the account')
        self.assertNotIn('account', GROUP_DETAIL_COLUMNS_OFF, 'shown until it is turned off')

    def test_one_visibility_store_serves_both_drawings(self):
        """A second store would let a field be on in the table and off on the card, which is
        two answers to one question. Both read `colState` through `fieldOn()`."""
        self.assertEqual(self.js.count("jsonFetch(`/api/user-prefs/group_detail_columns_"), 1)
        self.assertIn('const fieldOn = (k) => !colState.hidden.includes(k);', self.js)
        name = self.js[self.js.index('function nameCell('):self.js.index('// One participation switch')]
        self.assertIn("fieldOn('account')", name, 'the desktop account dot is gated too')
        card = self.js[self.js.index('function memberCard('):self.js.index('function renderCards()')]
        self.assertIn("fieldOn('account')", card)

    def test_a_field_only_entry_is_not_a_column_and_is_not_draggable(self):
        """It renders inside the Channel cell, so it has no column position - a grip that
        moved nothing would be a control that lies."""
        self.assertIn('const FIELD_ONLY = { account: true };', self.js)
        cols = self.js[self.js.index('function columns()'):self.js.index('function buildColMenu()')]
        self.assertIn('!FIELD_ONLY[k]', cols)
        menu = self.js[self.js.index('function buildColMenu()'):self.js.index('const FILTER_DIMS = [')]
        self.assertIn('item.draggable = !FIELD_ONLY[key];', menu)

    def test_the_phone_field_picker_offers_the_same_set(self):
        """The Filters sheet's field list is DESIGN.md §9.4's amendment - visibility only,
        never the desktop Columns popover, because a card line is not a table track."""
        self.assertIn('data-fieldpick=', self.js)
        fn = self.js[self.js.index('function openFilters()'):self.js.index('function afterFilterChange(')]
        self.assertIn('PICKABLE_FIELDS()', fn)
        self.assertNotIn('draggable', fn, 'there is nothing to reorder on a card')
        handler = self.js[self.js.index("const fieldPick = e.target.closest('[data-fieldpick]')"):]
        self.assertIn('buildColMenu();', handler[:handler.index('return;')],
                      'the desktop popover reads the same set and must be rebuilt with it')

    # ── The chips row and the filter sheet ───────────────────────────────────

    def test_the_filters_chip_counts_what_the_desktop_draws_as_chips(self):
        """One state, two drawings: the desktop renders a chip per active filter, the
        phone renders the number of them. A count derived separately is how the two come
        to disagree - and an "every value checked means no filter" reading would light the
        chip on every page load, which is what the shared bar's empty-means-off avoids."""
        fn = self.js[self.js.index('function renderChipsRow()'):self.js.index('function setSelecting(')]
        self.assertIn('filterBar.count()', fn)

    def test_the_sort_chip_names_the_field_and_the_direction(self):
        """The sortable column headers go away with the table, so this chip is the only thing
        left saying how the list is ordered (DESIGN.md §9.4)."""
        fn = self.js[self.js.index('function renderChipsRow()'):self.js.index('function setSelecting(')]
        self.assertIn('Sort: ${escHtml(sortLabel())}', fn)
        self.assertIn('gm-dir', fn)

    def test_the_sheet_is_drawn_from_the_same_registry_as_the_desktop_popover(self):
        """One registry, two drawings. A sheet holding its own hand-written list of
        dimensions is how the phone comes to offer a filter the desktop dropped - or to
        miss one it gained - a new dimension is a registry entry and nothing else."""
        fn = self.js[self.js.index('function openFilters()'):self.js.index('function afterFilterChange(')]
        self.assertIn('FILTER_DIMS.forEach', fn)
        self.assertIn('d.available()', fn)
        self.assertIn('filterBar.has(d.k, o.v)', fn)

    def test_every_sheet_filter_reaches_the_same_state_as_the_desktop_bar(self):
        """One state, two controls. A sheet writing a parallel copy is how a filter comes
        to be on in one drawing and off in the other. One handler covers every dimension,
        so a new one cannot arrive without its sheet row working."""
        handler = self.js[self.js.index("const fPick = e.target.closest('[data-fpick]')"):]
        self.assertIn('filterBar.toggle(', handler[:handler.index('return;')])

    def test_the_desktop_bar_is_rebuilt_when_a_sheet_moves_a_filter(self):
        """Otherwise crossing the breakpoint lands on a control still showing the state it
        had before the sheet touched it."""
        fn = self.js[self.js.index('function afterFilterChange(fromBar)'):self.js.index('// The sort sheet redraws')]
        for call in ('sheetRedraw()', 'filterBar.render();', 'renderList();'):
            self.assertIn(call, fn)

    def test_server_rendered_initial_state_is_the_nothing_active_state(self):
        """JS may upgrade it; it must never be required to calm it down. The chips ship
        empty and the card list ships empty."""
        self.assertIn('<div class="gm-list" id="gd-list"></div>', self.tpl)
        for chip in ('gd-chip-sort', 'gd-chip-filter', 'gd-chip-select'):
            block = self.tpl[self.tpl.index(f'id="{chip}"'):]
            self.assertIn('></button>', block[:block.index('</button>') + 9])

    def test_the_phone_chrome_and_the_desktop_chrome_are_mutually_exclusive(self):
        """Two live copies of one control is how the two come to disagree - the desktop's
        four filter controls and its Columns popover leave, and the chips row arrives."""
        phone_block = self.css[self.css.index('  .gd-statezone { display: flex;'):]
        phone_block = phone_block[:phone_block.index('  .gm-card {')]
        self.assertIn('[data-section="channels"] .table-scroll,', phone_block)
        self.assertIn('[data-section="channels"] .gd-bulkbar,', phone_block)
        self.assertIn('.gd-phone-hide { display: none; }', phone_block)
        self.assertIn('.gd-phonebar { display: flex; }', phone_block)
        # The desktop's chip row leaves at this width and the Filters sheet arrives: a
        # popover that has to be drilled into is a bad control at 375px, and two live
        # copies of one filter is how the two come to disagree.
        chips = self.tpl[self.tpl.index('id="gd-filter-chips"'):]
        self.assertIn('gd-phone-hide', self.tpl[:self.tpl.index('id="gd-filter-chips"')][-200:],
                      'the + Filter chip row is still on screen at 375px')
        self.assertIn('id="gd-filter-menu"', chips[:chips.index('</div>')+400])

    def test_the_search_box_is_not_duplicated(self):
        """One input, one `searchTerm`. `.card-head` already wraps at this width, so the
        shipped box is full width on its own line above the chips."""
        self.assertEqual(self.tpl.count('id="gd-search"'), 1)
        phonebar = self.tpl[self.tpl.index('class="toolbar gd-phonebar"'):self.tpl.index('id="gd-list"')]
        self.assertNotIn('class="search"', phonebar)
        self.assertNotIn('search-wrap', phonebar)

    # ── R5: the status bar reads before the advisories ───────────────────────

    def test_the_status_bar_comes_before_the_banners_at_phone_width_only(self):
        """Measured at 375px with two banners firing, the bar started 1,022px down the
        document - the page opened on advisories with every one of its controls below the
        fold. `display: contents` is what keeps the desktop order byte-identical."""
        self.assertIn('.gd-statezone { display: contents; }', self.css)
        self.assertIn('.gd-statezone > .gd-actionbar, .gd-statezone > .gd-action-error { order: -1; }',
                      self.css)
        zone = self.tpl[self.tpl.index('<div class="gd-statezone">'):self.tpl.index('/.gd-statezone')]
        self.assertLess(zone.index('id="gd-format-banner"'), zone.index('class="gd-actionbar"'),
                        'the source order is the desktop order and the CSS moves it')

    # ── Enforcement and the action chain ─────────────────────────────────────

    def test_every_phone_action_resolves_to_a_handler(self):
        """A control the markup offers and the module does not handle is a button that does
        nothing, which must never be silent."""
        chain = self.js[self.js.index('function pageAction(act, el)'):]
        for act in ('overflow', 'bulk-sheet', 'select-done', 'select-all-visible', 'test-selected'):
            self.assertIn(f"case '{act}':", chain, f'{act} is emitted but unhandled')

    def test_the_bulk_sheet_posts_the_same_values_as_the_desktop_menu(self):
        """Both reach `members/participation/bulk`, so both go through `set_participation()`
        - a phone must not be a second, unvalidated write path."""
        fn = self.js[self.js.index('function openBulkSheet()'):self.js.index('// ── The sticky bottom bar')]
        for value in ('rec:on', 'rec:off', 'test:on', 'test:off'):
            self.assertIn(f'data-bulk="{value}"', fn)
        self.assertNotIn('jsonFetch', fn, 'the sheet emits the action, it does not post it')

    def test_leaving_selection_mode_clears_the_selection(self):
        """Rather than leaving it armed and invisible: the checkboxes go with the mode."""
        fn = self.js[self.js.index('function setSelecting(on)'):self.js.index('// A sheet row.')]
        self.assertIn('if (!on) selected.clear();', fn)


# ── dev/changelog/853 - what this page says WHILE its check is running ───────
#
# Two halves of one complaint (dev/docs/BUGS.md 2026-08-28 06:14 pm). The summary bar
# counted the channel being measured right now as a failure, so a 104-channel run drew a
# red segment that turned green seconds later; and the status bar's "Testing channel N of
# M" was server-rendered with no updater at all, so it sat on channel 1 for the whole run.


class RunProgressCountsTests(unittest.TestCase):
    """The tally is the shared authority behind the summary bar, the Groups list's check
    chip and the dashboard widget, so the in-progress row has to be excluded once, there."""

    def setUp(self):
        self.t = make_test_app()
        self.app = self.t.app
        with self.app.app_context():
            acct = make_account()
            self.chans = [make_channel(acct, name=f'CW {i}') for i in range(3)]
            job = make_test_job('Quick test', channels=self.chans, status='RUNNING')
            self.job_id = job.id
            self.cids = [c.id for c in self.chans]
            # Two finished results plus the row the run is sitting on: inserted with the
            # placeholder status the tester gives every test before ffmpeg spawns.
            make_channel_test(self.chans[0], status='COMPLETED', job_id=self.job_id)
            make_channel_test(self.chans[1], status='FAILED', job_id=self.job_id,
                              error_detail='no data received')
            self.open_id = make_channel_test(self.chans[2], status='FAILED',
                                             job_id=self.job_id, test_ended_at=None).id
            db.session.commit()

    def tearDown(self):
        self.t.cleanup()

    def _counts(self):
        from app.routes.channel_tests import get_job_result_counts
        with self.app.app_context():
            return get_job_result_counts(self.job_id, self.cids)

    def test_the_channel_under_test_is_not_counted_as_a_failure(self):
        """It has produced no result yet - counting its placeholder status reported a
        failure the run had not observed, on the one surface the user watches during a run."""
        c = self._counts()
        self.assertEqual(c['fail_count'], 1)
        self.assertEqual(c['pass_count'], 1)
        self.assertEqual(c['tested_count'], 2)

    def test_it_counts_the_moment_the_test_finishes(self):
        """Excluded means "not yet", never "dropped" - the row lands in the tally as soon
        as _finalize_test stamps it."""
        from app.database import ChannelTest
        with self.app.app_context():
            row = db.session.get(ChannelTest, self.open_id)
            row.status = 'COMPLETED'
            row.test_ended_at = row.test_started_at
            db.session.commit()
        c = self._counts()
        self.assertEqual(c['pass_count'], 2)
        self.assertEqual(c['fail_count'], 1)
        self.assertEqual(c['tested_count'], 3)

    def test_a_warned_result_still_reads_as_tested(self):
        """Guards the exclusion against being written as a status filter: a finished row
        carrying an error_detail is a warn, and warns are results."""
        from app.database import ChannelTest
        with self.app.app_context():
            row = db.session.get(ChannelTest, self.open_id)
            row.status = 'COMPLETED'
            row.error_detail = 'Low bitrate'
            row.test_ended_at = row.test_started_at
            db.session.commit()
        c = self._counts()
        self.assertEqual(c['warn_count'], 1)
        self.assertEqual(c['tested_count'], 3)


class RunProgressBarTests(unittest.TestCase):
    """The status bar's message across a run: what the server renders as its first frame,
    and that the client has an updater to carry it from there."""

    def setUp(self):
        self.t = make_test_app()
        self.app = self.t.app
        self.client = self.app.test_client()
        with self.app.app_context():
            acct = make_account()
            chans = [make_channel(acct, name=f'CW {i}') for i in range(3)]
            job = make_test_job('Quick test', channels=chans, status='RUNNING')
            self.job_id = job.id
            self.gid = job.group_id
            self.first_cid = chans[0].id
            db.session.commit()

    def tearDown(self):
        self.t.cleanup()

    def _page_with_status(self, **overrides):
        """Render the page as it looks mid-run. Built by overriding the real idle status
        so a field added to get_status() cannot leave this fixture silently short."""
        from app import channel_tester
        status = channel_tester.get_status()
        status.update(is_running=True, current_job_id=self.job_id,
                      current_channel_id=self.first_cid, current_phase='testing',
                      completed_channels=2, total_channels=10)
        status.update(overrides)
        with patch.object(channel_tester, 'get_status', return_value=status):
            r = self.client.get(f'/channel-groups/{self.gid}')
        self.assertEqual(r.status_code, 200)
        return re.sub(r'\s+', ' ', r.get_data(as_text=True))

    def test_the_bar_names_the_channel_the_run_has_reached(self):
        self.assertIn('Testing channel 3 of 10.', self._page_with_status())

    def test_between_channels_it_says_so_rather_than_naming_the_next_one_early(self):
        """completed_channels has already moved during the wait, so the testing sentence
        would claim a channel the run has not started."""
        html = self._page_with_status(current_phase='waiting')
        self.assertIn('Between channels - 2 of 10 done.', html)
        self.assertNotIn('Testing channel 3 of 10.', html)

    def test_the_message_has_a_live_updater(self):
        """The defect was structural: the region had no updater, so every number in it was
        frozen at page load however long the run went on."""
        js = _read('static/js/group-detail.js')
        fn = js[js.index('function updateActionBarMsg(isMine)'):js.index('function updateLog(')]
        self.assertIn("#gd-inline-actionbar .gd-ab-msg", fn)
        self.assertIn('Testing channel ${s.completed_channels + 1} of ${s.total_channels}.', fn)
        self.assertIn('Between channels - ${s.completed_channels} of ${s.total_channels} done.', fn)
        poll = js[js.index('function pollStatus()'):]
        self.assertIn('updateActionBarMsg(isMine);', poll)

    def test_a_run_that_starts_under_an_open_page_refreshes_the_bar_around_it(self):
        """Only the message has an in-place updater; the badge and the action group are
        server-rendered per job status, so a run beginning under an open page needs the
        same reload the end of a run already takes - and must not take it on a page that
        was opened mid-run, or it reloads forever."""
        js = _read('static/js/group-detail.js')
        poll = js[js.index('function pollStatus()'):]
        self.assertIn("const justStarted = isMine && G.jobStatus !== 'RUNNING';", poll)
        self.assertIn('if ((justFinished || justStarted) && !reloading)', poll)

    def test_the_table_and_its_counts_still_refresh_on_every_tick(self):
        """The rows, the summary bar and the banners all come from refreshRows(), so losing
        that one call freezes the same numbers this change exists to unfreeze - and jsdom is
        not wired up for this module, so nothing else here would notice."""
        js = _read('static/js/group-detail.js')
        poll = js[js.index('function pollStatus()'):]
        self.assertIn('if (isMine || justFinished) refreshRows();', poll)


if __name__ == '__main__':
    unittest.main()
