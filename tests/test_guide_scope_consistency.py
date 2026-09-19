"""Every surface that answers "is this in the TV Guide" must answer it the same way.

`Channel.in_guide` used to carry two facts at once - "this channel is a guide row" and
"restore this channel as a guide row if its group is dissolved" - because group membership
silently suppressed a member's own row. Reading the raw column got the question wrong in BOTH
directions: measured on the live database, the flag was true on 6 channels while 19 fed the
guide's 8 rows, and one of the 6 had no guide row at all (`dev/changelog/734`).

The suppression is gone (`dev/changelog/751`) and the column is now honest - it means "this
channel is its own guide row" and nothing else, whatever groups it belongs to. So one whole
direction of that disagreement is now structurally impossible, and these tests assert it is:
a flagged channel is in scope no matter what group it sits in.

`channel_groups.guide_scope_channel_ids()` remains the single answer, because scope is still
the WIDER question in the other direction - a group member with its own flag off has its
listings on screen through the group's row. These tests pin the two things that make it
trustworthy: it agrees with what the guide grid actually paints, and every consumer of it
agrees with every other one. The `f.other=guide` search filter and the account page's "In the
TV Guide" count are the two consumers today, and they are rendered next to each other as a
number and the link that opens it - so a disagreement is visible to the user.
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tests.support.app import make_test_app  # noqa: E402
from tests.support import seed  # noqa: E402
from tests.support.search import only_hiding  # noqa: E402
from app import db  # noqa: E402
from app.channel_groups import guide_scope_channel_ids  # noqa: E402
from app.channel_search import (  # noqa: E402
    DimensionFilter, SearchContext, SearchState, search)
from app.database import Channel  # noqa: E402
from app.account_stats import guide_counts  # noqa: E402
from app.routes.guide import _guide_row_entries  # noqa: E402


class _GuideScopeTestCase(unittest.TestCase):
    """One account carrying every shape the two definitions can still disagree on.

    `paints` is a channel with its own guide row - the uncontroversial case. `grouped_row` is
    the shape that used to expose the auto-hide: its flag is on AND it sits in a group that is
    not in the guide. It is now simply a guide row, which is what its flag says.
    `member_a`/`member_b` are in an in-guide group and carry no flag of their own; the group
    row is what shows their listings, and they are the only remaining reason scope is wider
    than the column.
    """

    def setUp(self):
        self.t = make_test_app()
        self.ctx_mgr = self.t.app.app_context()
        self.ctx_mgr.push()
        self.acct = seed.make_account(name='Alpha')
        self.paints = self._channel('Plain Guide Channel', in_guide=True)
        self.absent = self._channel('Not In The Guide')
        self.grouped_row = self._channel('Grouped And Its Own Row', in_guide=True)
        self.member_a = self._channel('Feed A')
        self.member_b = self._channel('Feed B')
        self.shown_group = seed.make_group(name='Shown', members=[self.member_a, self.member_b],
                                           in_guide=True)
        self.hidden_group = seed.make_group(name='Hidden', members=[self.grouped_row],
                                            in_guide=False)
        db.session.commit()

    def tearDown(self):
        self.ctx_mgr.pop()
        self.t.cleanup()

    def _channel(self, name, **kw):
        return seed.make_channel(self.acct, name=name, **kw)

    def scope_names(self):
        rows = Channel.query.filter(Channel.id.in_(guide_scope_channel_ids())).all()
        return sorted(ch.name for ch in rows)

    def search_names(self):
        """The CHANNELS `f.other=guide` returns.

        Since dev/changelog/811 that search also returns the in-guide GROUP as a row of its
        own - a group answers `guide` because it holds a guide row - and that is a different
        kind of answer to the same question, not a disagreement with the scope. Guide scope
        is a set of channel ids, so this compares channels to channels.
        """
        state = SearchState(standing=only_hiding(), facets=(),
                            filters=(DimensionFilter('other', ('guide',)),))
        return sorted(r.name for r in search(state, SearchContext.build({})).rows
                      if isinstance(r, Channel))


class ScopeDefinitionTests(_GuideScopeTestCase):

    def test_scope_is_guide_rows_plus_group_members(self):
        self.assertEqual(self.scope_names(),
                         ['Feed A', 'Feed B', 'Grouped And Its Own Row',
                          'Plain Guide Channel'])

    def test_a_flagged_channel_is_in_scope_whatever_group_it_is_in(self):
        """The auto-hide, asserted gone. `Grouped And Its Own Row` carries its own flag and
        sits in a group that is not in the guide; before dev/changelog/751 the membership
        suppressed it and the guide showed it nowhere while the flag stayed true, which is
        the defect that made the column unreadable."""
        self.assertTrue(self.grouped_row.in_guide)
        self.assertIn('Grouped And Its Own Row', self.scope_names())

    def test_dissolving_the_group_changes_nothing_for_its_members(self):
        """The dissolve-restore contract, asserted gone. A member never lost its row, so
        losing the group gives nothing back - the flag reads the same before and after."""
        before = self.scope_names()
        for m in list(self.hidden_group.memberships):
            db.session.delete(m)
        db.session.delete(self.hidden_group)
        db.session.commit()
        self.assertEqual(self.scope_names(), before)
        self.assertIn('Grouped And Its Own Row', self.scope_names())

    def test_a_group_out_of_the_guide_neither_adds_nor_removes(self):
        """`ChannelGroup.in_guide` is the whole test of whether a group is a guide row.

        This used to read `kind='check_only'`, which no longer exists: a group used only
        for health checking is one whose members are all recording-disabled, and that is a
        configuration rather than a type (dev/changelog/741). What survives is the
        `in_guide` half - a group nobody put in the guide contributes nothing to scope
        whatever its members' switches say.

        Both directions are asserted. The REMOVE half - that grouping `paints` does not cost
        it the row it already had - could not be asserted while the auto-hide existed, and
        asserting it is the point of dev/changelog/751.
        """
        seed.make_group(name='Nightly', members=[self.absent, self.paints],
                        recording=False, in_guide=False)
        db.session.commit()
        self.assertEqual(self.scope_names(),
                         ['Feed A', 'Feed B', 'Grouped And Its Own Row',
                          'Plain Guide Channel'])


class ScopeMatchesTheRenderedGuideTests(_GuideScopeTestCase):

    def test_every_channel_the_guide_paints_is_in_scope(self):
        """The guide grid resolves each group row to ONE active member; scope keeps all of
        them, because which member fills a given cell is decided per program at render time.
        So the painted set is a subset - but it must never contain something scope omits, or
        the filter hides a row the user is looking straight at."""
        painted = set()
        for kind, obj, active in _guide_row_entries():
            painted.add(active.id if kind == 'group' else obj.id)
        self.assertTrue(painted)
        self.assertLessEqual(painted, set(ch.id for ch in Channel.query.filter(
            Channel.id.in_(guide_scope_channel_ids())).all()))

    def test_scope_adds_only_the_groups_other_members(self):
        painted = set()
        for kind, obj, active in _guide_row_entries():
            painted.add(active.id if kind == 'group' else obj.id)
        scope = {ch.id for ch in Channel.query.filter(
            Channel.id.in_(guide_scope_channel_ids())).all()}
        self.assertEqual({self.member_a.id, self.member_b.id} & (scope - painted),
                         (scope - painted))


class ConsumersAgreeTests(_GuideScopeTestCase):

    def test_the_account_count_matches_its_own_link(self):
        """The account page renders this number as a link into `f.other=guide`. Reading the
        raw flag here made the two disagree by more than 3x on the live database."""
        self.assertEqual(guide_counts()[self.acct.id], len(self.search_names()))

    def test_the_account_count_is_the_scope_count(self):
        self.assertEqual(guide_counts()[self.acct.id], len(self.scope_names()))

    def test_the_search_filter_returns_the_scope(self):
        self.assertEqual(self.search_names(), self.scope_names())

    def test_a_second_account_does_not_borrow_the_first_ones_rows(self):
        """`guide_counts` groups by account, and the scope subquery is account-blind - so
        the two have to compose rather than one overwriting the other."""
        other = seed.make_account(name='Beta')
        seed.make_channel(other, name='Beta Guide Channel', in_guide=True)
        db.session.commit()
        counts = guide_counts()
        self.assertEqual(counts[self.acct.id], 4)
        self.assertEqual(counts[other.id], 1)


if __name__ == '__main__':
    unittest.main()
