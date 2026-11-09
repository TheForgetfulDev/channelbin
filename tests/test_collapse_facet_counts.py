"""Programs facet counts under "Collapse channel groups" say what picking the value gives.

The collapse keeps a group's best member over the rows in the search, and a facet is counted
with its own filter off, so picking a value re-ranks inside it: a member that lost to a member on
another account wins once that account is filtered out. On the production database the rail read
261,832 for an account whose filter returned 264,449, and 0 for a health band whose filter
returned 127 (dev/docs/BUGS.md 2026-11-05).

No network, no ffmpeg. Run standalone:
  python3 -m unittest tests.test_collapse_facet_counts
"""
import os
import sys
import unittest
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import db  # noqa: E402
from app.channel_search import (  # noqa: E402
    DEFAULT_STANDING, GRAIN_AIRINGS, DimensionFilter, SearchContext, SearchState,
    default_standing_for, search, search_facets)
from app.database import Account, Channel, ChannelGroupMember, Tag, TagPattern  # noqa: E402
from tests.support import make_test_app  # noqa: E402
from tests.support import seed  # noqa: E402

COUNTED = ('acct', 'cat', 'health', 'other', 'group', 'tag', 'mark', 'when', 'dow')


class _Case(unittest.TestCase):
    """Two groups sharing a member, members on two accounts in different categories and health
    bands, so the best member of a showing differs with almost every value picked."""

    def setUp(self):
        self.t = make_test_app(csrf=False)
        now = datetime.utcnow()
        harbor = seed.make_account(name='harbor', last_sync_at=now)
        skyline = seed.make_account(name='skyline', last_sync_at=now)
        db.session.commit()

        def make(account, name, category, score):
            ch = seed.make_channel(account, name=name, category_name=category,
                                   health_score=score, last_seen_at=now)
            ch.stream_url = ch.raw_stream_url = f'http://{account.name}.example/{name}.ts'
            return ch

        best = make(harbor, 'Arena', 'Sports', 95.0)
        shared = make(skyline, 'Hockey Local', 'Local', 75.0)
        untested = make(skyline, 'Arena Backup', 'Sports', None)
        weak = make(harbor, 'Newsroom', 'News', 30.0)
        # Gone from its feed, so the Other facet has a value the winning member lacks.
        weak.last_seen_at = now - timedelta(days=30)
        spare = make(harbor, 'Spare', 'Sports', 90.0)
        alone = make(skyline, 'Standalone', 'Local', 60.0)
        db.session.flush()
        seed.make_group(name='Sports', members=[best, shared, untested])
        seed.make_group(name='News', members=[shared, weak])
        # The backup loses in both of its groups, to a channel the other group lacks - so
        # picking either group lets it win the other one's showing.
        seed.make_group(name='Backups', members=[untested, spare])
        tag = Tag(name='backup-tag')
        db.session.add(tag)
        db.session.flush()
        db.session.add(TagPattern(tag_id=tag.id, pattern='Backup'))
        db.session.commit()

        # One clock for every row: the collapse's partition is the title and both times, so
        # showings a microsecond apart would be different programs and never collapse.
        start = now.replace(second=0, microsecond=0)
        for ch in (best, shared, untested, spare):
            # A tag reads the showing's subtitle too, which is not part of the partition.
            sub = 'Backup feed' if ch is untested else None
            # A guide mark is each member's own guide's, like the subtitle: the backup's
            # guide marks the game live and the winning member's does not.
            seed.make_epg_entry(ch, title='The game', sub_title=sub, is_live=ch is untested,
                                is_new=ch in (best, untested),
                                start_time=start + timedelta(minutes=30))
            seed.make_epg_entry(ch, title='The game', sub_title=sub, is_live=ch is untested,
                                start_time=start + timedelta(days=1))
        for ch in (shared, weak):
            seed.make_epg_entry(ch, title='The news', start_time=start + timedelta(minutes=90))
        for ch in (best, shared, untested, weak, spare, alone):
            seed.make_epg_entry(ch, title=f'Only on {ch.name}',
                                start_time=start + timedelta(minutes=150))
        db.session.commit()

    def tearDown(self):
        self.t.cleanup()

    def total_for(self, state, key, value):
        narrowed = [f for f in state.filters if f.key != key] + [DimensionFilter(key, (value,))]
        picked = SearchState(grain=state.grain, q=state.q, standing=state.standing,
                             filters=tuple(narrowed), facets=())
        return search(picked, SearchContext.build()).channel_total

    def assert_facets_agree(self, **kw):
        kw.setdefault('grain', GRAIN_AIRINGS)
        kw.setdefault('standing', default_standing_for(GRAIN_AIRINGS))
        state = SearchState(**kw)
        self.assertIn('grpdedup', state.standing)
        facets = search_facets(state, SearchContext.build())
        checked = 0
        for key in COUNTED:
            for value, count in facets.get(key, {}).items():
                with self.subTest(dimension=key, value=value):
                    self.assertEqual(count, self.total_for(state, key, value))
                checked += 1
        self.assertGreater(checked, 15)
        return facets


class CollapseFacetsAgreeWithTheirFilterTests(_Case):

    def test_unfiltered(self):
        facets = self.assert_facets_agree()
        # The backup's two games, which the collapse hides behind two better members, and the
        # showing only it carries.
        self.assertEqual(facets['tag']['backup-tag'], 3)
        # Only the backup's guide marks the game live, and the collapse hides both showings.
        # New is on the winner's copy in one group and on the backup's in the other.
        self.assertEqual(facets['mark'], {'live': 2, 'new': 2})

    def test_with_a_filter_on_the_counted_dimension(self):
        self.assert_facets_agree(filters=(DimensionFilter('acct', (str(self.skyline_id()),)),))

    def test_with_each_dimension_filtered_on_one_of_its_own_values(self):
        """A filtered dimension is counted by its own pass, with its own filter off."""
        unfiltered = search_facets(SearchState(grain=GRAIN_AIRINGS), SearchContext.build())
        for key in ('cat', 'health', 'other', 'group', 'tag', 'mark'):
            value = next(v for v, n in unfiltered[key].items() if n)
            with self.subTest(filtered=key, value=value):
                self.assert_facets_agree(filters=(DimensionFilter(key, (value,)),))

    def test_a_member_that_does_not_record_ranks_last(self):
        """The collapse's first sort key: a recording-disabled member loses to any that records,
        whatever its health."""
        best = Channel.query.filter_by(name='Arena').one()
        ChannelGroupMember.query.filter_by(channel_id=best.id).update(
            {'recording_enabled': False})
        db.session.commit()
        self.assert_facets_agree()
        self.assert_facets_agree(filters=(DimensionFilter('health', ('great',)),))

    def test_with_a_filter_on_another_dimension(self):
        self.assert_facets_agree(filters=(DimensionFilter('cat', ('Sports', 'Local')),))

    def test_with_typed_text(self):
        self.assert_facets_agree(q='game')

    def test_with_one_row_per_channel(self):
        standing = default_standing_for(GRAIN_AIRINGS) | {'firstonly'}
        self.assert_facets_agree(standing=standing)

    def test_collapse_off_is_untouched(self):
        standing = default_standing_for(GRAIN_AIRINGS) - {'grpdedup'}
        state = SearchState(grain=GRAIN_AIRINGS, standing=standing)
        facets = search_facets(state, SearchContext.build())
        for key in COUNTED:
            for value, count in facets.get(key, {}).items():
                with self.subTest(dimension=key, value=value):
                    self.assertEqual(count, self.total_for(state, key, value))

    def skyline_id(self):
        return Account.query.filter_by(name='skyline').one().id


class ChannelGrainIsUntouchedTests(_Case):

    def test_channel_grain_has_no_collapse(self):
        state = SearchState(standing=DEFAULT_STANDING)
        facets = search_facets(state, SearchContext.build())
        self.assertEqual(sum(facets['acct'].values()), 6)


if __name__ == '__main__':
    unittest.main()
