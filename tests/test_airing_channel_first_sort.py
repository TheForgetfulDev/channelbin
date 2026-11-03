"""Reading `channels` first is a query plan, so the rows must not be able to tell.

Three airing sorts take that plan because a full sort of every surviving showing cost five times
the page (dev/changelog/1419). ORDER BY decides the answer either way, and this holds that to be
true over the shapes that could break it: empty and tied sort keys, both directions, a page
boundary. The seeds are small, so the plan itself can only be pinned by its shape.
"""
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import event  # noqa: E402
from sqlalchemy.engine import Engine  # noqa: E402

from app import channel_search as cs, db  # noqa: E402
from app.channel_search import GRAIN_AIRINGS, search  # noqa: E402
from app.search_index import rebuild_search_indexes  # noqa: E402
from tests.support import seed  # noqa: E402
from tests.support.search import only_hiding  # noqa: E402
from tests.test_airing_search import _AiringTestCase  # noqa: E402


class ChannelFirstSortTests(_AiringTestCase):
    SORTS = sorted(cs._CHANNEL_LED_SORTS)

    def _seed(self):
        super()._seed()
        # A channel with no category and no EPG id, so the sort key is NULL on real rows, and
        # three channels tied on one category so the tiebreak decides between them.
        blank = seed.make_channel(self.acct, name='Blank', category_name=None,
                                  epg_channel_id=None, last_seen_at=self.now)
        self._epg(blank, 'Blank Show', 90)
        for n in range(3):
            tied = seed.make_channel(self.acct, name=f'Tied {n}', category_name='Tied',
                                     epg_channel_id='tied.id', last_seen_at=self.now)
            self._epg(tied, 'Same Time', 120)
            self._epg(tied, f'Later {n}', 240 + n)
        db.session.commit()

    def _ids(self, **kw):
        kw.setdefault('standing', only_hiding())
        return [r.id for r in search(self.state(**kw), self.context(),
                                     want_counts=False, want_facets=False).rows]

    def _both_ways(self, **kw):
        driven = self._ids(**kw)
        with mock.patch.object(cs, '_CHANNEL_LED_SORTS', frozenset()):
            return driven, self._ids(**kw)

    def test_the_list_is_what_was_measured(self):
        self.assertEqual(cs._CHANNEL_LED_SORTS, frozenset({'category', 'channel', 'tvg'}))

    def test_the_same_rows_in_the_same_order(self):
        for sort in self.SORTS:
            for desc in (False, True):
                with self.subTest(sort=sort, desc=desc):
                    driven, joined = self._both_ways(sort=sort, sort_desc=desc)
                    self.assertTrue(joined)
                    self.assertEqual(driven, joined)

    def test_the_same_rows_across_a_page_boundary(self):
        for sort in self.SORTS:
            for page in (1, 2, 3):
                with self.subTest(sort=sort, page=page):
                    driven, joined = self._both_ways(sort=sort, page=page, page_size=2)
                    self.assertEqual(len(joined), 2)
                    self.assertEqual(driven, joined)

    def test_the_same_rows_under_a_filter_and_a_standing_option(self):
        narrow = (cs.DimensionFilter('cat', ('Tied',), ()),)
        for sort in self.SORTS:
            with self.subTest(sort=sort):
                driven, joined = self._both_ways(sort=sort, filters=narrow)
                self.assertEqual(driven, joined)
                self.assertEqual(len(joined), 6)
                driven, joined = self._both_ways(
                    sort=sort, standing=cs.default_standing_for(GRAIN_AIRINGS))
                self.assertTrue(joined)
                self.assertEqual(driven, joined)

    def _statements(self, **kw):
        statements = []

        def capture(_conn, _cursor, statement, *_rest):
            statements.append(statement)

        event.listen(Engine, 'before_cursor_execute', capture)
        try:
            self._ids(**kw)
        finally:
            event.remove(Engine, 'before_cursor_execute', capture)
        return '\n'.join(statements)

    def test_a_listed_sort_reads_channels_first(self):
        for sort in ('category', 'channel', 'tvg'):
            with self.subTest(sort):
                self.assertIn('+channels.id', self._statements(sort=sort))

    def test_an_unlisted_sort_does_not(self):
        for sort in ('when', 'title', 'account', 'health', 'sid', 'url'):
            with self.subTest(sort):
                self.assertNotIn('+channels.id', self._statements(sort=sort))

    def test_a_typed_query_does_not(self):
        rebuild_search_indexes('test')
        self.assertNotIn('+channels.id', self._statements(
            sort='category', q='wembley', fields=('name', 'epg-title')))

    def test_a_tag_filter_does_not(self):
        self.assertNotIn('+channels.id', self._statements(
            sort='category', filters=(cs.DimensionFilter('tag', ('sports',), ()),)))


if __name__ == '__main__':
    unittest.main()
