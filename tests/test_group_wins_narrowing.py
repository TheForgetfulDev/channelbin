"""Tier 2 - labelling a page's group winners never scans the showings of ungrouped channels.

Guards `dev/changelog/905`. `group_wins_by_entry()` answers "which groups did each of this
page's showings win", and it used to answer it by materializing the whole four-table ranking
window over every showing on every grouped channel and then filtering that down to the
hundred rows on screen. The window's own docstring called that free, on a measurement taken
when 8 memberships put 636 rows in its scope.

Membership is data, and it grew: at 207 memberships the same window spanned 10,348 showings
and cost the airing landing page ~43ms of ~245ms to return, in the overwhelmingly common
case, an empty dict - because group membership is a hand-curated handful against six figures
of channels, so a page of showings sorted by start time usually contains none of them at all.

That is the whole shape of the slowdown three investigations failed to pin on a commit: no
commit caused it, the data grew into a cost the code had assumed away. The fix is a
subtraction - a showing on a channel that is in no group can have won no group, so those
entries are dropped before the window runs and a page with none of them asks nothing.

Two things are asserted here, and they have to be asserted together: that the query is
skipped when it cannot find anything (the performance claim), and that the winners are
unchanged when it is not (the correctness claim). Either alone would pass on a broken
implementation - returning `{}` unconditionally satisfies the first, and the original
full-scan satisfies the second.
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tests.support.app import make_test_app  # noqa: E402
from tests.support import seed  # noqa: E402
from tests.support.iocount import IOCounter, all_engines  # noqa: E402
from app import db  # noqa: E402
from app.channel_search import (SearchContext, SearchState,  # noqa: E402
                                group_wins_by_entry, search)
from app.database import EPGEntry  # noqa: E402


class _GroupedAndUngrouped(unittest.TestCase):
    """One group of two members carrying the same program, plus ungrouped channels."""

    def setUp(self):
        self.t = make_test_app()
        self.ctx = self.t.app.app_context()
        self.ctx.push()
        acct = seed.make_account()
        # The group's two members carry a byte-identical listing, which is what puts them in
        # one ranking partition and gives the collapse something to actually decide.
        self.best = seed.make_channel(acct, name='CW East', health_score=90)
        self.worst = seed.make_channel(acct, name='CW West', health_score=10)
        self.group = seed.make_group(name='CW', members=[self.best, self.worst])
        self.loners = [seed.make_channel(acct, name='Ungrouped %d' % i)
                       for i in range(4)]
        db.session.commit()

        self.grouped_entries = [seed.make_epg_entry(ch, title='The Program', offset_minutes=5)
                                for ch in (self.best, self.worst)]
        # The seed helper stamps each showing from its own `now`, so two calls land
        # microseconds apart - which is four distinct partitions, not one, and every entry
        # then trivially "wins" a partition of size 1. The collapse is only exercised when
        # the members' listings agree to the second, which is what real member EPG looks like.
        pinned = self.grouped_entries[0]
        for entry in self.grouped_entries[1:]:
            entry.start_time, entry.stop_time = pinned.start_time, pinned.stop_time
        self.loner_entries = [seed.make_epg_entry(ch, title='Something Else',
                                                  offset_minutes=5)
                              for ch in self.loners]
        db.session.commit()
        self.member_ids = tuple(ch.id for ch in (self.best, self.worst))
        # `commit()` expires every loaded instance, so the FIRST touch of `.channel_id` inside
        # a measured block would be a lazy refresh SELECT and would be counted as though the
        # narrowing had queried. Load them here instead. The real call site never has this
        # problem - it hands over rows `_page_rows()` has just returned in the same session -
        # so forcing it in the fixture measures the code rather than the fixture.
        for entry in self.grouped_entries + self.loner_entries:
            entry.id, entry.channel_id

    def tearDown(self):
        self.ctx.pop()
        self.t.cleanup()

    def counting(self):
        """`IOCounter(all_engines())` - the canonical counter, and `all_engines()` is not
        optional here. `db.session` routes to the BACKGROUND pool whenever there is no request
        context, which is exactly what a test body is, so watching `db.engine` alone counts
        zero however many statements run and the guard passes while measuring nothing
        (tests/support/iocount.py)."""
        return IOCounter(all_engines())


class ThePageThatCannotWinAnythingAsksNothingTests(_GroupedAndUngrouped):

    def test_a_page_of_only_ungrouped_showings_issues_no_query(self):
        """The performance claim, stated as a fact a test can hold: the scan is not made
        cheaper, it is not made at all."""
        with self.counting() as c:
            wins = group_wins_by_entry(self.loner_entries, self.member_ids)
        self.assertEqual({}, wins)
        self.assertEqual(0, c.queries, c.statements)

    def test_an_empty_page_issues_no_query(self):
        with self.counting() as c:
            self.assertEqual({}, group_wins_by_entry([], self.member_ids))
        self.assertEqual(0, c.queries, c.statements)

    def test_no_memberships_at_all_issues_no_query(self):
        """Empty is a real answer, not a missing one - and it is the state a fresh install
        is in, where this must not cost a scan either."""
        with self.counting() as c:
            self.assertEqual({}, group_wins_by_entry(self.grouped_entries, ()))
        self.assertEqual(0, c.queries, c.statements)


class TheWinnersAreUnchangedTests(_GroupedAndUngrouped):

    def test_the_healthier_member_still_wins_its_partition(self):
        """The correctness claim. Without it, `return {}` would pass the file above."""
        wins = group_wins_by_entry(self.grouped_entries, self.member_ids)
        winner = self.grouped_entries[0]
        self.assertEqual({winner.id: (self.group.id,)}, wins)

    def test_a_mixed_page_still_answers_for_its_grouped_rows(self):
        """The narrowing drops ungrouped entries from the INPUT, never from the answer's
        meaning - a page carrying both kinds must still label the grouped one."""
        mixed = self.loner_entries + self.grouped_entries
        wins = group_wins_by_entry(mixed, self.member_ids)
        self.assertEqual({self.grouped_entries[0].id: (self.group.id,)}, wins)

    def test_it_matches_the_unnarrowed_answer_for_every_entry(self):
        """Reference check against the shape this replaced: ask for every showing at once and
        confirm the narrowed answer is the same mapping, not merely a subset of it."""
        every = EPGEntry.query.all()
        narrowed = group_wins_by_entry(every, self.member_ids)
        reference = {e.id: v for e, v in
                     ((e, group_wins_by_entry([e], self.member_ids).get(e.id))
                      for e in every) if v}
        self.assertEqual(reference, narrowed)


class TheAiringPageStillLabelsItsGroupRowsTests(_GroupedAndUngrouped):
    """End to end through `search()`, because the call site changed too - it now hands over
    `ctx.group_member_channel_ids` rather than a list of ids, and a wrong argument there
    would un-label every group row on the page with nothing raised."""

    def test_the_surviving_row_is_labelled_with_its_group(self):
        state = SearchState(grain='airings')
        result = search(state, SearchContext.build())
        self.assertEqual({self.grouped_entries[0].id: (self.group.id,)},
                         result.airing_group_ids)

    def test_the_losing_member_is_collapsed_away(self):
        state = SearchState(grain='airings')
        result = search(state, SearchContext.build())
        ids = {r.id for r in result.rows}
        self.assertIn(self.grouped_entries[0].id, ids)
        self.assertNotIn(self.grouped_entries[1].id, ids)


if __name__ == '__main__':
    unittest.main()
