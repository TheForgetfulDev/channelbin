"""Tier 2 - joining a channel group costs a channel nothing.

Guards the subtraction `dev/changelog/751` made. Until it landed, holding one membership in
any non-system group silently removed that channel's own TV Guide row while leaving
`Channel.in_guide` set to True, and four separate code paths were built on top of that:

  * the guide grid excluded every member from its channel rows,
  * `guide_scope_channel_ids()` excluded them from search scope,
  * `add_channel_to_guide` redirected the write onto the member's GROUP, so clicking
    "+ Add to Guide" on a channel added a row for something else,
  * `toggle_channel` refused outright and flashed "add the group on the Groups tab instead",
  * and `create_group` inherited any member's flag onto the new group, to hand back the rows
    it had just taken away.

The column consequently answered three questions and only one correctly. Measured on the live
database at the time: the guide painted 8 rows fed by 19 channels while the flag was true on
6, one of which had no guide row at all (`dev/changelog/734`).

Each test here fails against the pre-deletion tree, and the file is deliberately organized by
entry point rather than by assertion, because the defect's shape was that every one of these
paths had its own copy of the suppression.

The inherited-flag case is the one that had become a live defect rather than dead weight.
Members are created Recording-off (`dev/docs/DESIGN-channel-groups-model.md` §14), so
inheriting a member's flag produced a guide row with no eligible member behind it - a row that
looks fine and cannot produce a file, which is what §15's guide invariant forbids.
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tests.support.app import make_test_app  # noqa: E402
from tests.support import seed  # noqa: E402
from app import db  # noqa: E402
from app.channel_groups import guide_scope_channel_ids  # noqa: E402
from app.channel_search import SearchContext, SearchState, search  # noqa: E402
from app.database import Channel, ChannelGroup  # noqa: E402
from app.routes.guide import _guide_row_entries  # noqa: E402


class _MemberWithItsOwnRow(unittest.TestCase):
    """One channel that is both a guide row of its own and a member of a group.

    That combination was unreachable before the deletion, which is why every test below
    builds it the same way.
    """

    def setUp(self):
        self.t = make_test_app()
        self.t.app.config['WTF_CSRF_ENABLED'] = False
        self.ctx = self.t.app.app_context()
        self.ctx.push()
        self.acct = seed.make_account()
        self.member = seed.make_channel(self.acct, name='FS1 East', in_guide=True)
        self.sibling = seed.make_channel(self.acct, name='FS1 West')
        self.loner = seed.make_channel(self.acct, name='Ungrouped', in_guide=True)
        self.group = seed.make_group(name='Fox Sports 1',
                                     members=[self.member, self.sibling], in_guide=True)
        db.session.commit()

    def tearDown(self):
        self.ctx.pop()
        self.t.cleanup()

    def scope_ids(self):
        return {ch.id for ch in
                Channel.query.filter(Channel.id.in_(guide_scope_channel_ids())).all()}

    def channel_rows(self):
        return {obj.id for kind, obj, _active in _guide_row_entries() if kind == 'channel'}


class TheGuideGridTests(_MemberWithItsOwnRow):

    def test_a_member_with_its_own_flag_gets_its_own_channel_row(self):
        self.assertIn(self.member.id, self.channel_rows())

    def test_it_gets_that_row_alongside_its_groups_row(self):
        """Two rows for one feed is the outcome the auto-hide was clumsily preventing. It is
        allowed, and saying so is the model's answer - a row the user asked for twice is not
        something to silently take away."""
        rows = _guide_row_entries()
        self.assertIn(self.member.id, {o.id for k, o, _ in rows if k == 'channel'})
        self.assertIn(self.group.id, {o.id for k, o, _ in rows if k == 'group'})

    def test_a_member_with_no_flag_of_its_own_still_gets_no_channel_row(self):
        """The subtraction removes the suppression, not the meaning of the column."""
        self.assertNotIn(self.sibling.id, self.channel_rows())


class GuideScopeTests(_MemberWithItsOwnRow):

    def test_a_flagged_member_is_in_scope_even_when_no_group_of_its_is(self):
        self.group.in_guide = False
        db.session.commit()
        self.assertIn(self.member.id, self.scope_ids())

    def test_scope_still_covers_a_members_listings_through_its_group(self):
        """The half of scope that survives: `sibling` has no flag and reaches the guide only
        through the group's row."""
        self.assertIn(self.sibling.id, self.scope_ids())


class CollapseChannelGroupsIsUnaffectedTests(unittest.TestCase):
    """`grpdedup` reads membership, `recording_enabled` and health score - never `in_guide` -
    so deleting the suppression changes none of its inputs and none of its output.

    It was the one behavioral unknown in this change, because a member holding its own guide
    row alongside its group's was previously unreachable. Measured rather than assumed: with
    the option on, a losing member is still collapsed away even when it carries its own row.

    That is deliberate and stays. The survivor is chosen by the rule record resolution and
    failover use (`_group_collapse_losers`), so that clicking Record on the surviving row
    records the feed the recorder would actually pick. Ranking a member up for holding its own
    guide row would break exactly that, and the user who wants both showings has the standing
    option: "Collapse channel groups" is what they turned on.
    """

    def setUp(self):
        self.t = make_test_app()
        self.ctx = self.t.app.app_context()
        self.ctx.push()
        acct = seed.make_account()
        self.winner = seed.make_channel(acct, name='Winner Feed', health_score=90)
        self.loser = seed.make_channel(acct, name='Loser With Own Row',
                                       in_guide=True, health_score=10)
        seed.make_group(name='Grp', members=[self.winner, self.loser], in_guide=True)
        first = seed.make_epg_entry(self.winner, title='The Match', offset_minutes=60)
        second = seed.make_epg_entry(self.loser, title='The Match', offset_minutes=60)
        # The partition is (group, title, start, stop), so the two showings have to be the
        # same showing to the minute or they never meet in it.
        second.start_time, second.stop_time = first.start_time, first.stop_time
        db.session.commit()

    def tearDown(self):
        self.ctx.pop()
        self.t.cleanup()

    def _names(self, standing):
        state = SearchState(grain='airings', standing=frozenset(standing), facets=())
        return sorted(r.channel.name for r in search(state, SearchContext.build({})).rows)

    def test_both_showings_are_listed_with_the_option_off(self):
        self.assertEqual(self._names({'past'}),
                         ['Loser With Own Row', 'Winner Feed'])

    def test_the_losing_member_is_collapsed_even_though_it_has_its_own_row(self):
        self.assertEqual(self._names({'past', 'grpdedup'}), ['Winner Feed'])


class AddToGuideEntryPointTests(_MemberWithItsOwnRow):
    """Both add-to-guide entry points now do what their label says: this channel, its own
    row. One used to flip the group's flag instead; the other refused."""

    def test_the_api_adds_the_channel_not_its_group(self):
        self.sibling.in_guide = False
        self.group.in_guide = False
        db.session.commit()
        r = self.t.app.test_client().post(
            f'/api/guide/channels/{self.sibling.id}/add', json={'force': True})
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.get_json()['success'])
        db.session.expire_all()
        self.assertTrue(db.session.get(Channel, self.sibling.id).in_guide)
        self.assertFalse(db.session.get(ChannelGroup, self.group.id).in_guide)

    def test_the_api_names_the_channel_it_added(self):
        """It used to answer "Fox Sports 1 (group)" to a request naming a channel, so the
        toast told the user something they had not asked for had happened."""
        self.sibling.in_guide = False
        db.session.commit()
        r = self.t.app.test_client().post(
            f'/api/guide/channels/{self.sibling.id}/add', json={'force': True})
        self.assertEqual(r.get_json()['channel_name'], 'FS1 West')

    def test_the_api_does_not_refuse_a_channel_in_several_groups(self):
        """Two memberships used to be a 409 - the route could not decide which group's flag
        to flip. There is no choice to make now; the channel is what gets the row."""
        seed.make_group(name='Second', members=[self.sibling], in_guide=False)
        self.sibling.in_guide = False
        db.session.commit()
        r = self.t.app.test_client().post(
            f'/api/guide/channels/{self.sibling.id}/add', json={'force': True})
        self.assertEqual(r.status_code, 200)
        db.session.expire_all()
        self.assertTrue(db.session.get(Channel, self.sibling.id).in_guide)

    def test_the_form_post_toggles_a_member_instead_of_refusing(self):
        """`channels.toggle_channel` flashed "add the group on the Groups tab instead" and
        returned without writing anything."""
        c = self.t.app.test_client()
        r = c.post(f'/channels/{self.member.id}/toggle',
                   data={'next': '/channels', 'confirm': '1'}, follow_redirects=False)
        self.assertEqual(r.status_code, 302)
        db.session.expire_all()
        self.assertFalse(db.session.get(Channel, self.member.id).in_guide)

    def test_the_form_post_adds_a_member_that_has_no_row(self):
        c = self.t.app.test_client()
        r = c.post(f'/channels/{self.sibling.id}/toggle',
                   data={'next': '/channels', 'confirm': '1'}, follow_redirects=False)
        self.assertEqual(r.status_code, 302)
        db.session.expire_all()
        self.assertTrue(db.session.get(Channel, self.sibling.id).in_guide)


class DissolveTests(_MemberWithItsOwnRow):

    def test_dissolving_a_group_leaves_every_members_flag_exactly_as_it_was(self):
        """The restore contract, asserted gone. Nothing is given back because nothing was
        taken - and the flags must not be rewritten on the way out either."""
        before = {ch.id: ch.in_guide for ch in
                  (self.member, self.sibling, self.loner)}
        r = self.t.app.test_client().post(f'/api/channel-groups/{self.group.id}/delete')
        self.assertEqual(r.status_code, 200)
        db.session.expire_all()
        for cid, was in before.items():
            self.assertEqual(db.session.get(Channel, cid).in_guide, was)

    def test_dissolving_a_group_removes_only_the_groups_own_row(self):
        self.t.app.test_client().post(f'/api/channel-groups/{self.group.id}/delete')
        db.session.expire_all()
        self.assertIsNone(db.session.get(ChannelGroup, self.group.id))
        self.assertEqual(self.channel_rows(), {self.member.id, self.loner.id})


class CreateGroupInheritanceTests(unittest.TestCase):
    """A new group is never put in the guide, however many of its members already are.

    That inheritance existed to hand back the rows grouping took away. Nothing is taken away
    now, and keeping it would breach `DESIGN-channel-groups-model.md` §15 outright: members
    are created Recording-off, so the inherited row has no eligible member behind it.
    """

    def setUp(self):
        self.t = make_test_app()
        self.t.app.config['WTF_CSRF_ENABLED'] = False
        self.ctx = self.t.app.app_context()
        self.ctx.push()
        self.acct = seed.make_account()
        self.a = seed.make_channel(self.acct, name='Feed A', in_guide=True)
        self.b = seed.make_channel(self.acct, name='Feed B')
        db.session.commit()

    def tearDown(self):
        self.ctx.pop()
        self.t.cleanup()

    def _create(self):
        r = self.t.app.test_client().post('/api/channel-groups', json={
            'name': 'New Group', 'channel_ids': [self.a.id, self.b.id], 'force': True})
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        db.session.expire_all()
        return ChannelGroup.query.filter_by(name='New Group').one()

    def test_a_new_group_is_not_in_the_guide_even_when_a_member_is(self):
        self.assertFalse(self._create().in_guide)

    def test_the_new_group_takes_no_guide_sort_order(self):
        """The inherited row also claimed a slot in the ordering space channels and groups
        share, so it displaced whatever the user had arranged. A group that is not in the
        guide takes the column's own default and reserves nothing."""
        self.assertFalse(self._create().guide_sort_order)

    def test_the_members_keep_their_own_flags(self):
        self._create()
        self.assertTrue(db.session.get(Channel, self.a.id).in_guide)
        self.assertFalse(db.session.get(Channel, self.b.id).in_guide)

    def test_the_inherited_row_would_have_had_no_eligible_member(self):
        """Why the inheritance is a §15 breach and not merely redundant: every member of a
        new group is Recording-off, so the row it used to create could never have recorded."""
        group = self._create()
        self.assertFalse(any(m.recording_enabled for m in group.memberships))
