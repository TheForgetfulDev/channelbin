"""Channel groups as first-class search results (dev/changelog/811).

The design is `dev/docs/DESIGN-group-search-rows.md` §5.1-§5.5, and its §5.2 lists four
rules that keep the shape's cost bounded. Three of them are correctness claims a test can
make, and this file makes them; the fourth (nothing resolved per row) is a query COUNT and
therefore lives in `tests/test_scaling_pages.py`, where the count is what makes it
deterministic.

What is guarded here:

* **The merge is exact.** Groups are fetched by their own query and spliced into a page the
  database ordered, so the two orders have to agree at every offset, in both directions and
  at every page size. A disagreement does not error - it serves a row on two pages, or on
  none, which is invisible until someone counts. `channel_sort_key` reproducing SQLite's
  ORDER BY exactly (including that `lower()` is ASCII-only) is what makes that possible, so
  it is asserted against the database rather than against a second Python spelling.
* **The fold.** A channel in a group is represented by its group's row; a channel holding
  its own guide row never is; `showmembers` puts them back and the count line names how
  many went.
* **Two numbers, never one.** `total` is what the pager pages, `channel_total` is what the
  standing options and the facet rail are defined over, and they must not be conflated.
* **The five sorts a group cannot answer** put group rows first rather than sorting them by
  a value they do not have.

No network, no real ffmpeg - see CLAUDE.md §Testing.
Run standalone:
  python3 -m unittest tests.test_group_search_rows
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tests.support.app import make_test_app  # noqa: E402
from tests.support import seed  # noqa: E402
from tests.support.search import only_hiding  # noqa: E402
from app import db  # noqa: E402
from app.channel_search import (  # noqa: E402
    GROUP_SORTS, SORTS, DimensionFilter, SearchContext, SearchState,
    _sqlite_lower, channel_sort_key, search, search_counts)
from app.database import Channel, ChannelGroup  # noqa: E402


class _GroupSearchTestCase(unittest.TestCase):
    """A corpus shaped like the real thing: two groups, a channel that holds its own guide
    row AND sits in a group, a channel in no group at all, and names that interleave the two
    kinds under the default sort."""

    def setUp(self):
        self.t = make_test_app()
        self.ctx_mgr = self.t.app.app_context()
        self.ctx_mgr.push()
        self._ctx = None
        self.acct = seed.make_account(name='Alpha')
        self.own_row = self._channel('Beta Own Row', in_guide=True, health_score=70.0)
        self.member_a = self._channel('Alpha Feed A', health_score=90.0)
        self.member_b = self._channel('Alpha Feed B', health_score=10.0)
        self.member_c = self._channel('Delta Feed C', health_score=55.0)
        self.loner = self._channel('Echo Loner', health_score=40.0)
        self.shown = seed.make_group(name='Charlie Group',
                                     members=[self.member_a, self.member_b], in_guide=True)
        self.other = seed.make_group(name='Zulu Group',
                                     members=[self.member_c, self.own_row], in_guide=False)
        self.shown.health_score = 77.0
        db.session.commit()

    def tearDown(self):
        self.ctx_mgr.pop()
        self.t.cleanup()

    def _channel(self, name, **kw):
        return seed.make_channel(self.acct, name=name, **kw)

    # -- helpers ---------------------------------------------------------

    def ctx(self):
        """One context per test, not one per search.

        `SearchContext.build()` reads the tags, the accounts and the index readiness, so a
        test that runs a hundred searches would pay all of it a hundred times - and the
        context is per-REQUEST state that nothing here mutates. This is the same hoist
        `CLAUDE.md`'s no-hidden-I/O rule asks of production code, applied to the harness so
        the paging matrix below stays affordable.
        """
        if getattr(self, '_ctx', None) is None:
            self._ctx = SearchContext.build({})
        return self._ctx

    def run_search(self, **kw):
        kw.setdefault('facets', ())
        return search(SearchState(**kw), self.ctx())

    def labels(self, **kw):
        return [('G:' if isinstance(r, ChannelGroup) else '') + r.name
                for r in self.run_search(**kw).rows]

    def unfolded(self, **kw):
        kw.setdefault('standing', only_hiding())
        return kw

    def folded(self, **kw):
        """The fold turned ON - which since dev/changelog/860 is no longer the default.

        A group member keeps a row of its own now, because this page is where you go to FIND
        a channel and a search that answers "no such channel" because it is in a group is the
        hidden behavior this project refuses. The fold is still a real, switchable option, so
        the tests about what it DOES have to ask for it rather than assume it.
        """
        kw.setdefault('standing', only_hiding('showmembers'))
        return kw


class GroupRowTests(_GroupSearchTestCase):

    def test_a_group_is_a_row_of_its_own(self):
        rows = self.run_search().rows
        names = {r.name for r in rows if isinstance(r, ChannelGroup)}
        self.assertEqual(names, {'Charlie Group', 'Zulu Group'})

    def test_a_group_is_returned_when_its_own_name_matches(self):
        """Its name, even when nothing about its members does - which is the half a search
        over the channels table alone can never answer."""
        self.assertEqual(self.labels(q='zulu'), ['G:Zulu Group'])

    def test_a_group_is_returned_when_a_member_matches(self):
        """The same question its members answer, which is what makes "why is this group
        here" explainable rather than a guess."""
        self.assertIn('G:Charlie Group', self.labels(q='alpha feed'))

    def test_the_system_group_is_never_a_row(self):
        """Its membership is computed rather than stored, it is not a recording target, and
        as a row it would fold away every channel in the guide."""
        db.session.add(ChannelGroup(name='TV Guide Channels', is_system=True, in_guide=True))
        db.session.commit()
        self.assertNotIn('G:TV Guide Channels', self.labels())

    def test_a_group_with_no_members_is_not_reached_by_a_member_search(self):
        seed.make_group(name='Empty Group', members=[], in_guide=False)
        db.session.commit()
        self.assertNotIn('G:Empty Group', self.labels())
        # ... but its own name still finds it, because the user made it and can look for it.
        self.assertIn('G:Empty Group', self.labels(q='empty'))


class FoldTests(_GroupSearchTestCase):

    def test_a_member_keeps_its_own_row_by_default(self):
        """dev/changelog/860. The fold mirrored the TV Guide, and the guide is the wrong
        model for this page: a search that silently answers "no such channel" because the
        channel is in a group is the hidden behavior this project exists to refuse. Both
        rows are on screen - the member's and its group's - which is what the member's
        "In guide via X" badge explains."""
        names = self.labels()
        for name in ('Alpha Feed A', 'Alpha Feed B', 'Delta Feed C'):
            self.assertIn(name, names)
        self.assertIn('G:Charlie Group', names)
        self.assertIn('Echo Loner', names)
        self.assertNotIn('showmembers', self.run_search().standing_hidden)

    def test_a_grouped_channel_is_represented_by_its_group_when_the_fold_is_on(self):
        names = self.labels(**self.folded())
        self.assertNotIn('Alpha Feed A', names)
        self.assertNotIn('Alpha Feed B', names)
        self.assertNotIn('Delta Feed C', names)
        self.assertIn('G:Charlie Group', names)
        self.assertIn('Echo Loner', names)

    def test_a_channel_with_its_own_guide_row_is_never_folded(self):
        """Even with the fold asked for: it has a TV Guide row beside its group's, so
        folding it would stop mirroring the guide at the one channel that proves both can
        hold a row."""
        self.assertIn('Beta Own Row', self.labels(**self.folded()))

    def test_showmembers_puts_them_back(self):
        names = self.labels(**self.unfolded())
        for name in ('Alpha Feed A', 'Alpha Feed B', 'Delta Feed C'):
            self.assertIn(name, names)

    def test_what_the_fold_took_is_counted_and_named(self):
        """The disclosure rule: an option that hides rows is only acceptable because the
        count line says by name what went, and clicking it puts them back."""
        result = self.run_search(**self.folded())
        self.assertEqual(result.standing_hidden.get('showmembers'), 3)
        self.assertEqual(self.run_search(**self.unfolded()).standing_hidden.get('showmembers'),
                         None)


class TwoNumbersTests(_GroupSearchTestCase):

    def test_total_is_what_the_pager_pages_and_channel_total_is_the_channels(self):
        result = self.run_search(**self.unfolded())
        self.assertEqual(result.channel_total, Channel.query.count())
        self.assertEqual(result.group_total, 2)
        self.assertEqual(result.total, result.channel_total + result.group_total)

    def test_the_counts_endpoint_agrees_with_the_row_search(self):
        """They are two requests answering one question (dev/changelog/598 split them), so
        a group counted by one and not the other is a pager that disagrees with its own
        count line."""
        state = SearchState(facets=())
        hidden, total, pages, groups, _matched = search_counts(state, self.ctx())
        result = search(state, self.ctx())
        self.assertEqual((hidden, total, pages), (result.standing_hidden, result.total,
                                                  result.pages))
        self.assertEqual(groups, result.group_total)

    def test_the_standing_counts_are_defined_over_the_channels(self):
        result = self.run_search()
        self.assertEqual(result.channel_total + sum(result.standing_hidden.values()),
                         Channel.query.count())


class SortPlacementTests(_GroupSearchTestCase):

    def test_name_and_health_interleave(self):
        """Asserted with the fold ON, so the list is the four rows this is about rather than
        those four plus every member - the placement of a GROUP row among channel rows is
        the question, and three more channels only make it harder to read."""
        self.assertEqual(GROUP_SORTS, frozenset({'name', 'health'}))
        by_name = self.labels(sort='name', **self.folded())
        self.assertEqual(by_name, ['Beta Own Row', 'G:Charlie Group', 'Echo Loner',
                                   'G:Zulu Group'])
        # Zulu has no score at all, and SQLite sorts NULL first ascending.
        self.assertEqual(self.labels(sort='health', **self.folded()),
                         ['G:Zulu Group', 'Echo Loner', 'Beta Own Row', 'G:Charlie Group'])

    def test_a_sort_a_group_cannot_answer_puts_the_groups_first(self):
        """`category`, `account`, `sid`, `tvg` and `url` describe a stream or a provider,
        and a group has none of them. First, in name order - and the count line says so."""
        for sort in sorted(set(SORTS) - set(GROUP_SORTS)):
            with self.subTest(sort=sort):
                names = self.labels(sort=sort)
                self.assertEqual(names[:2], ['G:Charlie Group', 'G:Zulu Group'])
                self.assertFalse([n for n in names[2:] if n.startswith('G:')])

    def test_the_placement_does_not_flip_with_the_direction(self):
        """Reversing a sort reverses a VALUE. A group has none of these values, so
        reversing would move its row for a reason the UI cannot explain."""
        names = self.labels(sort='category', sort_desc=True)
        self.assertEqual(names[:2], ['G:Charlie Group', 'G:Zulu Group'])


class MergePagingTests(_GroupSearchTestCase):
    """The merge is exact at every offset, in both directions, at every page size.

    A wrong merge does not raise - it serves a row on two pages or on none - so this pages
    the whole list and compares it to the unpaged one rather than asserting on any single
    page.
    """

    def _whole(self, **kw):
        return self.labels(page_size=500, **kw)

    def _paged(self, size, **kw):
        out, page = [], 1
        while True:
            rows = self.labels(page=page, page_size=size, **kw)
            if not rows:
                return out
            out += rows
            page += 1
            self.assertLess(page, 60, 'paging did not terminate')

    def test_every_page_partition_matches_the_unpaged_list(self):
        for sort in sorted(SORTS):
            for desc in (False, True):
                for standing in (None, only_hiding()):
                    kw = {'sort': sort, 'sort_desc': desc, 'standing': standing}
                    whole = self._whole(**kw)
                    for size in (1, 2, 3, 7):
                        with self.subTest(sort=sort, desc=desc, size=size,
                                          members=standing is not None):
                            self.assertEqual(self._paged(size, **kw), whole)

    def test_the_page_count_matches_what_paging_actually_yields(self):
        result = self.run_search(page_size=2)
        self.assertEqual(result.pages, (result.total + 1) // 2)
        self.assertEqual(len(self._paged(2)), result.total)


class SortKeyFidelityTests(_GroupSearchTestCase):
    """`channel_sort_key` must reproduce SQLite's own ORDER BY, not approximate it.

    It is the only thing that lets a Python-side merge place a group among rows the database
    ordered. The trap it exists for is real and silent: SQLite's `lower()` is ASCII-only, so
    it leaves `Ä` alone where Python's `str.lower()` does not, and a merge built on the
    Python answer would put a group on the wrong side of any name starting with one.
    """

    def test_the_python_key_orders_channels_exactly_as_the_database_does(self):
        for name in ('Ärger TV', 'zeta', 'ZETA', 'Ångström', 'aaa', 'AAA', '  pad'):
            db.session.add(seed.make_channel(self.acct, name=name, health_score=50.0))
        db.session.commit()
        for sort in sorted(GROUP_SORTS):
            with self.subTest(sort=sort):
                order = list(SORTS[sort]()) + [Channel.id]
                from_db = Channel.query.order_by(*order).all()
                in_python = sorted(from_db, key=lambda c: channel_sort_key(c, sort))
                self.assertEqual([c.id for c in in_python], [c.id for c in from_db])

    def test_sqlite_lower_is_ascii_only(self):
        self.assertEqual(_sqlite_lower('ÄBC'), 'Äbc')
        self.assertEqual(db.session.execute(db.text("SELECT lower('ÄBC')")).scalar(), 'Äbc')


class GroupFilterTests(_GroupSearchTestCase):

    def test_a_named_group_filter_keeps_only_that_group_row(self):
        names = self.labels(filters=(DimensionFilter('group', ('Charlie Group',)),))
        self.assertEqual([n for n in names if n.startswith('G:')], ['G:Charlie Group'])

    def test_an_excluded_group_loses_its_row(self):
        names = self.labels(filters=(DimensionFilter('group', (), ('Charlie Group',)),))
        self.assertNotIn('G:Charlie Group', names)

    def test_a_channel_id_filter_returns_no_group_rows(self):
        """`f.chan=` names one channel by primary key - the duplicate drill-in builds it -
        and a group is not one. Leaving it unanswered would put every group in the app into
        a three-row cluster view."""
        names = self.labels(filters=(DimensionFilter('chan', (str(self.loner.id),)),))
        self.assertEqual(names, ['Echo Loner'])

    def test_a_group_answers_the_guide_flags_with_its_own_row(self):
        """A group in the guide answers `guide` and `guidegroup` - a group IS how listings
        reach the guide - and never `guiderow`, which is `Channel.in_guide` and is about a
        CHANNEL holding a row of its own."""
        for value, expect in (('guide', ['G:Charlie Group']),
                              ('guidegroup', ['G:Charlie Group']),
                              ('guiderow', [])):
            with self.subTest(value=value):
                names = self.labels(standing=only_hiding(),
                                    filters=(DimensionFilter('other', (value,)),))
                self.assertEqual([n for n in names if n.startswith('G:')], expect)


if __name__ == '__main__':
    unittest.main()
