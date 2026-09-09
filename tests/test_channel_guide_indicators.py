"""The channel detail page states its guide relationship as TWO separate facts.

`Channel.in_guide` means "this channel is its own guide row" and nothing else since
dev/changelog/751 - joining a group stopped suppressing a member's own row. That made the
column honest and made a second question visible: a member of an in-guide group has its
listings on screen through the group's row whether or not it holds one of its own.

Nothing said so. The page read `Channel.in_guide` alone and therefore told a grouped
channel "Not in the TV Guide" while its listings were in the TV Guide - the defect logged
at dev/docs/BUGS.md 2026-08-20 @ 07:31 AM. These pin the replacement: both facts stated,
independently, in every combination, and the group chips marked so which group puts it
there is readable without hovering (dev/changelog/759).

The four states are enumerated on purpose rather than tested as a pair of booleans - a
trailing `else` rendering a real state is the defect class CLAUDE.md names, and "own row
AND via a group" is a real state now, not an impossible one.
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tests.support.app import make_test_app  # noqa: E402
from tests.support import seed  # noqa: E402
from app import db  # noqa: E402


class _DetailPage(unittest.TestCase):

    def setUp(self):
        self.t = make_test_app()
        self.ctx = self.t.app.app_context()
        self.ctx.push()
        self.acct = seed.make_account()

    def tearDown(self):
        self.ctx.pop()
        self.t.cleanup()

    def channel(self, name='FS1', in_guide=False):
        ch = seed.make_channel(self.acct, name=name, in_guide=in_guide, health_score=80.0)
        db.session.commit()
        return ch

    def page(self, channel):
        resp = self.t.client.get(f'/channels/{channel.id}')
        self.assertEqual(resp.status_code, 200)
        return resp.get_data(as_text=True)

    def healthy(self, channel):
        """A passing test, which is the branch of the status bar that carries the guide
        sentence. Without one the bar renders "Never tested" and says nothing about the
        guide at all."""
        seed.make_channel_test(channel, all_null=False, status='COMPLETED')
        db.session.commit()


class GuideBadgeTests(_DetailPage):

    def test_a_grouped_channel_with_no_row_of_its_own_names_the_group(self):
        ch = self.channel()
        seed.make_group(name='Fox Sports 1', members=[ch], in_guide=True)
        db.session.commit()
        body = self.page(ch)
        self.assertIn('In guide via Fox Sports 1', body)
        # The "own row" badge must NOT appear - it would claim a row that does not exist.
        self.assertNotIn('>In Guide<', body)

    def test_both_facts_render_together_when_both_are_true(self):
        """Joining a group no longer hides a channel's own row, so this combination is an
        ordinary state. A page that rendered one badge or the other would have to be
        wrong in one direction."""
        ch = self.channel(in_guide=True)
        seed.make_group(name='Fox Sports 1', members=[ch], in_guide=True)
        db.session.commit()
        body = self.page(ch)
        self.assertIn('>In Guide<', body)
        self.assertIn('In guide via Fox Sports 1', body)

    def test_a_group_that_is_not_in_the_guide_puts_nothing_on_screen(self):
        """A health-check-only group renders no guide row, so naming it as a route into
        the guide would invent an entry the guide does not have."""
        ch = self.channel()
        seed.make_group(name='Nightly checks', members=[ch], in_guide=False,
                        recording=False)
        db.session.commit()
        body = self.page(ch)
        self.assertNotIn('In guide via', body)

    def test_a_channel_with_only_its_own_row_gets_only_the_own_row_badge(self):
        ch = self.channel(in_guide=True)
        body = self.page(ch)
        self.assertIn('>In Guide<', body)
        self.assertNotIn('In guide via', body)

    def test_several_in_guide_groups_name_the_first_and_count_the_rest(self):
        ch = self.channel()
        seed.make_group(name='Alpha', members=[ch], in_guide=True)
        seed.make_group(name='Beta', members=[ch], in_guide=True)
        seed.make_group(name='Gamma', members=[ch], in_guide=False, recording=False)
        db.session.commit()
        body = self.page(ch)
        # Two of the three are in the guide, so the badge counts one extra, not two.
        self.assertIn('+1', body)
        self.assertIn('In guide via ', body)


class GuideSentenceTests(_DetailPage):
    """The status bar's guide sentence - the line that literally said "Not in the TV
    Guide" about a channel whose listings were in the TV Guide."""

    def test_a_grouped_channel_is_not_told_it_is_absent_from_the_guide(self):
        ch = self.channel()
        seed.make_group(name='Fox Sports 1', members=[ch], in_guide=True)
        self.healthy(ch)
        body = self.page(ch)
        self.assertNotIn('Not in the TV Guide.', body)
        self.assertIn('No guide row of its own', body)
        self.assertIn('through Fox Sports 1', body)

    def test_a_channel_in_the_guide_only_by_its_own_row_reads_as_before(self):
        ch = self.channel(in_guide=True)
        self.healthy(ch)
        self.assertIn('In the guide and recordable.', self.page(ch))

    def test_both_at_once_says_both(self):
        ch = self.channel(in_guide=True)
        seed.make_group(name='Fox Sports 1', members=[ch], in_guide=True)
        self.healthy(ch)
        body = self.page(ch)
        self.assertIn('In the guide as its own row and through Fox Sports 1', body)

    def test_a_channel_in_neither_still_says_so(self):
        """The fourth state has to survive: widening the sentence must not make "not in
        the guide" unsayable."""
        ch = self.channel()
        self.healthy(ch)
        self.assertIn('Not in the TV Guide.', self.page(ch))


class GroupChipTests(_DetailPage):
    """Marking the chips is what makes WHICH group readable without hovering the badge -
    deliberate, see dev/changelog/759."""

    def test_an_in_guide_group_chip_is_marked(self):
        ch = self.channel()
        seed.make_group(name='Fox Sports 1', members=[ch], in_guide=True)
        db.session.commit()
        self.assertIn('grp-guided', self.page(ch))

    def test_a_chip_for_a_group_with_no_guide_row_is_not_marked(self):
        ch = self.channel()
        seed.make_group(name='Nightly checks', members=[ch], in_guide=False,
                        recording=False)
        db.session.commit()
        body = self.page(ch)
        self.assertIn('mem-chip grp ', body)
        self.assertNotIn('grp-guided', body)

    def test_the_mark_follows_the_group_flag_rather_than_the_membership(self):
        ch = self.channel()
        grp = seed.make_group(name='Fox Sports 1', members=[ch], in_guide=True)
        db.session.commit()
        self.assertIn('grp-guided', self.page(ch))
        grp.in_guide = False
        db.session.commit()
        self.assertNotIn('grp-guided', self.page(ch))


class GuideToggleCopyTests(_DetailPage):
    """UI text describing backend behavior is part of the change surface (CLAUDE.md). The
    button is only ever about this channel's OWN row, so its label does not change - but
    it must stop implying the guide has never heard of the channel."""

    def test_add_to_guide_keeps_its_label_and_explains_the_group(self):
        ch = self.channel()
        seed.make_group(name='Fox Sports 1', members=[ch], in_guide=True)
        db.session.commit()
        body = self.page(ch)
        self.assertIn('+ Add to Guide', body)
        self.assertIn('already in the guide through Fox Sports 1', body)

    def test_remove_from_guide_says_the_listings_survive_through_the_group(self):
        """Removing the channel's own row does not take its listings off screen while a
        group still carries them, and a dialog that implied otherwise would be describing
        a deletion the backend does not perform."""
        ch = self.channel(in_guide=True)
        seed.make_group(name='Fox Sports 1', members=[ch], in_guide=True)
        db.session.commit()
        body = self.page(ch)
        self.assertIn('its listings stay in the guide through Fox Sports 1', body)
