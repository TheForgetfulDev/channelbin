"""The `listings` filter behind the Programs notice: which channels have nothing scheduled, and
that the notice's number is the rows its button lands on (dev/changelog/1437). The page's side
of it is NameNoticeTests in tests/test_channel_search_page_js.py.

No network, no ffmpeg. Run standalone:
  python3 -m unittest tests.test_search_listings_filter
"""
import os
import sys
import unittest
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import db, duplicate_streams  # noqa: E402
from app.channel_search import (  # noqa: E402
    GRAIN_AIRINGS, GRAIN_CHANNELS, SearchContext, SearchState, describe_state, describe_value,
    dimensions_for, search, visible_dimensions_for)
from app.database import EPGEntry  # noqa: E402
from tests.support import make_test_app  # noqa: E402
from tests.support import seed  # noqa: E402


class _Case(unittest.TestCase):
    def setUp(self):
        self.t = make_test_app(csrf=False)
        self.client = self.t.app.test_client()
        self.now = datetime.utcnow()
        self.acct = seed.make_account(name='Alpha', last_sync_at=self.now)
        self.never = self.channel('NHL TEAM| PHILADELPHIA FLYERS')
        self.ended = self.channel('NHL 05: 7pm Wild @ Flyers')
        self.listing(self.ended, -3, -1, title='NHL 05: 7pm Wild @ Flyers')
        self.on_now = self.channel('Flyers Classics')
        self.listing(self.on_now, -1, 1)
        self.later = self.channel('Flyers Tonight')
        self.listing(self.later, 5, 6)
        self.other = self.channel('Discovery')
        db.session.commit()

    def tearDown(self):
        self.t.cleanup()

    def channel(self, name, **kw):
        kw.setdefault('last_seen_at', self.now)
        return seed.make_channel(self.acct, name=name, **kw)

    def listing(self, channel, start_hours, stop_hours, title='A Program'):
        db.session.add(EPGEntry(channel_id=channel.id, title=title,
                                start_time=self.now + timedelta(hours=start_hours),
                                stop_time=self.now + timedelta(hours=stop_hours)))

    def rows(self, query):
        resp = self.client.get(f'/api/channels/search?{query}')
        self.assertEqual(resp.status_code, 200, resp.get_data(as_text=True))
        return resp.get_json()

    def counts(self, query):
        resp = self.client.get(f'/api/channels/search/counts?{query}')
        self.assertEqual(resp.status_code, 200, resp.get_data(as_text=True))
        return resp.get_json()

    def names(self, query):
        return sorted(r['name'] for r in self.rows(query)['rows'])


class WhichChannelsTests(_Case):
    def test_none_scheduled_is_no_listing_that_has_not_ended(self):
        self.assertEqual(self.names('q=Flyers&f.listings=none'),
                         ['NHL 05: 7pm Wild @ Flyers', 'NHL TEAM| PHILADELPHIA FLYERS'])

    def test_excluding_it_leaves_the_channels_with_something_scheduled(self):
        self.assertEqual(self.names('q=Flyers&x.listings=none'),
                         ['Flyers Classics', 'Flyers Tonight'])

    def test_an_unknown_value_is_refused(self):
        resp = self.client.get('/api/channels/search?f.listings=some')
        self.assertEqual(resp.status_code, 400)
        self.assertIn('Listings', resp.get_json()['error'])


class TheNumberIsTheRowsTests(_Case):
    def test_the_count_equals_the_rows(self):
        query = 'q=Flyers&f.listings=none'
        answer = self.counts(query)
        self.assertEqual(answer['total'], 2)
        self.assertEqual(answer['total'], len(self.rows(query)['rows']))

    def test_a_group_named_for_the_search_is_not_one_of_them(self):
        seed.make_group(name='Flyers', members=[self.on_now])
        db.session.commit()
        self.assertEqual(self.counts('q=Flyers')['total'], 5)
        self.assertEqual(self.counts('q=Flyers&f.listings=none')['total'], 2)
        self.assertEqual(self.counts('q=Flyers&f.listings=none')['group_total'], 0)
        self.assertNotIn('group', [r.get('kind') for r in
                                   self.rows('q=Flyers&f.listings=none')['rows']])

    def test_hidden_channels_are_counted_the_way_the_channels_tab_hides_them(self):
        self.never.hidden = True
        db.session.commit()
        self.assertEqual(self.counts('q=Flyers&f.listings=none')['total'], 1)

    def test_it_composes_with_the_other_filters(self):
        self.never.category_name = 'NHL Package'
        db.session.commit()
        self.assertEqual(self.names('q=Flyers&f.listings=none&f.cat=NHL+Package'),
                         ['NHL TEAM| PHILADELPHIA FLYERS'])

    def test_ticking_the_ended_option_on_programs_does_not_move_the_number(self):
        # What the tab switch makes of the set the page sends with the ended option ticked.
        crossed = 'q=Flyers&f.listings=none&standing=shownoepg&standing=showuntested'
        self.assertEqual(self.counts(crossed)['total'], self.counts('q=Flyers&f.listings=none')['total'])

    def test_channels_with_no_listings_turned_off_leaves_nothing_to_count(self):
        # The notice goes quiet rather than linking to an empty page.
        self.assertEqual(self.counts('q=Flyers&f.listings=none&standing=showuntested')['total'], 0)

    def test_the_rail_still_answers_under_the_filter(self):
        resp = self.client.get('/api/channels/search/facets?q=Flyers&f.listings=none&facets=acct')
        self.assertEqual(resp.status_code, 200, resp.get_data(as_text=True))
        self.assertEqual(resp.get_json()['facets']['acct'][str(self.acct.id)], 2)


class DuplicatesTests(_Case):
    """A copy with no listings whose kept copy has them: with the filter on, the kept copy is
    out of the results, so the copy shows (dev/changelog/1435). The notice counts it the same."""

    def test_the_count_and_the_rows_agree_about_a_copy(self):
        kept = self.channel('Same Stream Flyers', health_score=99.0)
        copy = self.channel('Same Stream Flyers HD', health_score=10.0)
        self.listing(kept, 1, 2)
        kept.stream_url = copy.stream_url = 'http://example.test/live/u/p/77'
        duplicate_streams.recompute()
        db.session.commit()
        self.assertTrue(copy.is_duplicate_loser)
        query = 'q=Same+Stream&f.listings=none'
        self.assertEqual(self.names(query), ['Same Stream Flyers HD'])
        self.assertEqual(self.counts(query)['total'], 1)


class OnlyOnTheChannelsTabTests(_Case):
    def test_it_is_a_channels_dimension_and_never_in_the_rail(self):
        keys = [d.key for d in dimensions_for(GRAIN_CHANNELS)]
        self.assertIn('listings', keys)
        self.assertNotIn('listings', [d.key for d in dimensions_for(GRAIN_AIRINGS)])
        self.assertNotIn('listings', [d.key for d in visible_dimensions_for(GRAIN_CHANNELS)])

    def test_the_programs_tab_ignores_it_like_any_parked_filter(self):
        with_it = self.counts('grain=airings&q=Flyers&f.listings=none')['total']
        self.assertEqual(with_it, self.counts('grain=airings&q=Flyers')['total'])
        self.assertGreater(with_it, 0)

    def test_a_stored_programs_search_naming_it_matches_what_it_matches_without(self):
        ctx = SearchContext.build()
        state = SearchState.from_params({'grain': GRAIN_AIRINGS, 'q': 'Flyers', 'f.listings': 'none'})
        plain = SearchState.from_params({'grain': GRAIN_AIRINGS, 'q': 'Flyers'})
        self.assertEqual({r.id for r in search(state, ctx).rows},
                         {r.id for r in search(plain, ctx).rows})

    def test_the_catalog_serves_the_chips_wording(self):
        catalog = self.client.get('/api/channels/search/catalog').get_json()
        self.assertEqual(catalog['listings_values'], [{
            'value': 'none', 'label': 'No scheduled listings', 'label_ex': 'Has scheduled listings',
            'tip': 'Channels with nothing in the guide that has not ended yet.',
            'tip_ex': 'Channels with something in the guide that has not ended yet.'}])
        self.assertEqual(describe_value('listings', 'none'), 'No scheduled listings')

    def test_a_described_search_says_the_phrase_both_ways_round(self):
        """Never "listings none scheduled" or "not listings ..." (dev/changelog/1450)."""
        picked = describe_state(SearchState.from_params({'f.listings': 'none'}))
        self.assertEqual(picked['summary'], 'no scheduled listings')
        self.assertEqual(picked['lines'][0]['values'][0]['text'], 'No scheduled listings')
        self.assertFalse(picked['lines'][0]['neg'])
        excluded = describe_state(SearchState.from_params({'x.listings': 'none'}))
        self.assertEqual(excluded['summary'], 'has scheduled listings')
        self.assertFalse(excluded['lines'][0]['neg'])


if __name__ == '__main__':
    unittest.main()
