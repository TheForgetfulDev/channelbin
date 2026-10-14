"""Day of week (`dow`) and Time of day (`tod`) on the airing grain (dev/changelog/1187).

Both read a showing's START on the display timezone's wall clock. The whole risk is in that
sentence: storage is naive UTC, so a Sunday 8 PM ET showing is stored on Monday, and a single
"current offset" reads every showing across a DST change an hour wrong. Every seed below is an
absolute instant chosen to sit on one of those edges, and every assertion names the local
reading it expects - a test written against UTC would pass on a broken engine.

  - TimeOfDayParseTests: the `tod:` spelling, strict about an end that is present but is not
    a time (a half-read range would be a wider search than anyone asked for).
  - OffsetSegmentTests: app/tz_utils.py::utc_offset_segments, the offsets SQL adds per row -
    New York's two 2026 changes to the second, a 30-minute DST zone, a zone with none.
  - DayOfWeekTests / TimeOfDayTests: the predicates against the edges - the UTC midnight
    boundary, local midnight exactly, both sides of the November change, the repeated hour
    at fall-back, the spring-forward gap, past midnight, one end alone, an exact minute,
    seconds inside the minute, exclusion, the two ANDing with each other, and the same
    answers from the `strftime('%s')` spelling an SQLite older than 3.38 gets.
  - DayTimeFacetTests: the weekday counts on both shapes of the shared facet scan and on the
    filtered path, seeded at zero for a day the guide does not reach; the time range counts
    only its own value; under "One row per channel" no facet about the showing is counted,
    because its count would disagree with its own list.
  - DayTimeContractTests: the URL round trip, the grain scoping, the 400s.
"""
import os
import sys
import unittest
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from werkzeug.datastructures import MultiDict  # noqa: E402

from tests.support.app import make_test_app  # noqa: E402
from tests.support import seed  # noqa: E402
from tests.support.search import only_hiding  # noqa: E402
from app import channel_search as cs, db  # noqa: E402
from app.channel_search import (  # noqa: E402
    DOW_VALUES, GRAIN_AIRINGS, GRAIN_CHANNELS, DimensionFilter, SearchContext, SearchState,
    SearchStateError, compute_facets, parse_time_of_day, search, split_predicates,
    visible_dimensions_for)
from app.database import EPGEntry  # noqa: E402
from app.tz_utils import utc_offset_segments  # noqa: E402

NY = ZoneInfo('America/New_York')
TEST_CFG = {'sync': {'channel_missing_after_days': 7}}

#: title -> naive-UTC start. Each name says the LOCAL (America/New_York) reading.
SHOWINGS = {
    # The UTC midnight boundary: 8 PM Sunday ET is 00:00 Monday UTC.
    'Sun 20:00 (Mon UTC)': datetime(2026, 10, 19, 0, 0),
    'Sun 23:59': datetime(2026, 10, 19, 3, 59),
    # Local midnight exactly - the instant a floating-point day fraction misplaces.
    'Mon 00:00': datetime(2026, 10, 19, 4, 0),
    'Tue 01:00': datetime(2026, 10, 20, 5, 0),
    'Tue 02:01': datetime(2026, 10, 20, 6, 1),
    'Tue 21:59': datetime(2026, 10, 21, 1, 59),
    'Tue 22:00': datetime(2026, 10, 21, 2, 0),
    # Seconds inside the minute: still a 9:00 PM showing.
    'Sat 21:00:30': datetime(2026, 10, 18, 1, 0, 30),
    # Both sides of the November change (2026-11-01 06:00 UTC), same wall clock.
    'Sat 19:00 EDT': datetime(2026, 10, 31, 23, 0),
    'Sun 19:00 EST': datetime(2026, 11, 2, 0, 0),
    # The hour that happens twice at fall-back: both are 1:30 AM Sunday on the clock.
    'Sun 01:30 EDT': datetime(2026, 11, 1, 5, 30),
    'Sun 01:30 EST': datetime(2026, 11, 1, 6, 30),
    # Spring forward (2026-03-08 07:00 UTC): the last minute before and the first after.
    'Sun 01:59 EST': datetime(2026, 3, 8, 6, 59),
    'Sun 03:00 EDT': datetime(2026, 3, 8, 7, 0),
}


class _DayTimeCase(unittest.TestCase):

    def setUp(self):
        self.t = make_test_app()
        self.ctx_mgr = self.t.app.app_context()
        self.ctx_mgr.push()
        acct = seed.make_account(name='Clock')
        self.channel = seed.make_channel(acct, name='Clock Channel', category_name='Sports',
                                         last_seen_at=datetime.utcnow())
        for title, start in SHOWINGS.items():
            db.session.add(EPGEntry(channel_id=self.channel.id, title=title, start_time=start,
                                    stop_time=start + timedelta(hours=1)))
        db.session.commit()

    def tearDown(self):
        self.ctx_mgr.pop()
        self.t.cleanup()

    def context(self, tz=NY):
        """The display timezone pinned, not read from config: the assertions are written
        for New York, and a sandbox whose default moved would otherwise fail them all for a
        reason that has nothing to do with the engine."""
        ctx = SearchContext.build(TEST_CFG)
        ctx.display_tz = tz
        ctx.tz_segments = None
        return ctx

    def state(self, include=(), exclude=(), **kw):
        """Nothing hidden - these showings are mostly in the past - and rows only."""
        filters = {}
        for key, value in include:
            filters.setdefault(key, ([], []))[0].append(value)
        for key, value in exclude:
            filters.setdefault(key, ([], []))[1].append(value)
        kw.setdefault('facets', ())
        kw.setdefault('page_size', 500)
        return SearchState(grain=GRAIN_AIRINGS, standing=only_hiding(),
                           filters=tuple(DimensionFilter(k, tuple(v), tuple(x))
                                         for k, (v, x) in filters.items()), **kw)

    def titles(self, include=(), exclude=(), tz=NY):
        result = search(self.state(include, exclude), self.context(tz), want_counts=False)
        return sorted(r.title for r in result.rows)


class TimeOfDayParseTests(unittest.TestCase):

    def test_both_ends_one_end_and_minutes_after_midnight(self):
        self.assertEqual(parse_time_of_day('tod:18:00..23:59'), (1080, 1439))
        self.assertEqual(parse_time_of_day('tod:22:00..02:00'), (1320, 120))
        self.assertEqual(parse_time_of_day('tod:18:00..'), (1080, None))
        self.assertEqual(parse_time_of_day('tod:..09:00'), (None, 540))
        self.assertEqual(parse_time_of_day('tod:00:00..00:00'), (0, 0))

    def test_no_range_is_no_value(self):
        for value in ('tod:..', 'tod:', 'tod:18:00', 'dur:18:00..19:00', ''):
            with self.subTest(value=value):
                self.assertIsNone(parse_time_of_day(value))

    def test_an_end_that_is_present_but_is_not_a_time_spoils_the_whole_value(self):
        """`18:00..25:00` read as "at or after 6 PM" would search wider than was asked, and a
        rule saved with it would record that without anyone noticing."""
        for value in ('tod:18:00..25:00', 'tod:6pm..', 'tod:18:00..19:60', 'tod:..x',
                      'tod:18:00..19:00..20:00', 'tod:18:00:00..'):
            with self.subTest(value=value):
                self.assertIsNone(parse_time_of_day(value))


class OffsetSegmentTests(unittest.TestCase):

    def test_new_york_2026_changes_at_the_exact_second(self):
        segments = utc_offset_segments(NY, datetime(2026, 1, 1), datetime(2026, 12, 31))
        self.assertEqual(segments, (
            (datetime(2026, 3, 8, 7, 0), -5 * 3600),
            (datetime(2026, 11, 1, 6, 0), -4 * 3600),
            (None, -5 * 3600),
        ))

    def test_a_span_with_no_change_is_one_segment(self):
        self.assertEqual(
            utc_offset_segments(NY, datetime(2026, 10, 12), datetime(2026, 10, 16)),
            ((None, -4 * 3600),))
        self.assertEqual(
            utc_offset_segments(ZoneInfo('UTC'), datetime(2026, 1, 1), datetime(2026, 12, 31)),
            ((None, 0),))

    def test_a_change_on_the_spans_own_first_or_last_day_is_found(self):
        """The sampling starts a day before the span and ends a day after it, so a change
        inside the span's first or last day is never missed."""
        self.assertEqual(len(utc_offset_segments(NY, datetime(2026, 11, 1, 12),
                                                 datetime(2026, 11, 1, 12))), 2)
        self.assertEqual(len(utc_offset_segments(NY, datetime(2026, 3, 8, 1),
                                                 datetime(2026, 3, 8, 1))), 2)

    def test_a_half_hour_daylight_saving_zone(self):
        segments = utc_offset_segments(ZoneInfo('Australia/Lord_Howe'),
                                       datetime(2026, 1, 1), datetime(2026, 12, 31))
        self.assertEqual([s for _until, s in segments], [39600, 37800, 39600])


class DayOfWeekTests(_DayTimeCase):

    def test_a_showing_is_on_the_local_day_it_starts_not_the_utc_one(self):
        mondays = self.titles(include=[('dow', 'mon')])
        self.assertEqual(mondays, ['Mon 00:00'])
        self.assertIn('Sun 20:00 (Mon UTC)', self.titles(include=[('dow', 'sun')]))

    def test_local_midnight_exactly_starts_the_new_day(self):
        self.assertNotIn('Mon 00:00', self.titles(include=[('dow', 'sun')]))
        self.assertIn('Sun 23:59', self.titles(include=[('dow', 'sun')]))

    def test_every_showing_lands_on_exactly_one_day(self):
        seen = []
        for day in DOW_VALUES:
            seen += self.titles(include=[('dow', day)])
        self.assertEqual(sorted(seen), sorted(SHOWINGS))

    def test_the_days_of_one_filter_mean_any_of_them(self):
        weekend = self.titles(include=[('dow', 'sat'), ('dow', 'sun')])
        self.assertEqual(weekend, sorted(t for t in SHOWINGS if t.startswith(('Sat', 'Sun'))))

    def test_excluding_a_day_keeps_every_other_day(self):
        self.assertEqual(self.titles(exclude=[('dow', 'sun')]),
                         sorted(t for t in SHOWINGS if not t.startswith('Sun')))

    def test_the_change_of_offset_moves_no_showing_to_another_day(self):
        """Both 7 PM showings either side of the November change are where the clock says,
        and so are both 1:30 AM showings in the hour that happens twice."""
        sundays = self.titles(include=[('dow', 'sun')])
        for title in ('Sun 19:00 EST', 'Sun 01:30 EDT', 'Sun 01:30 EST', 'Sun 01:59 EST',
                      'Sun 03:00 EDT'):
            self.assertIn(title, sundays)
        self.assertIn('Sat 19:00 EDT', self.titles(include=[('dow', 'sat')]))

    def test_the_display_timezone_decides_not_the_server(self):
        """Read on UTC's clock instead, 8 PM Sunday ET is Monday - which is exactly the
        answer the engine must NOT give a New York user."""
        utc_mondays = self.titles(include=[('dow', 'mon')], tz=ZoneInfo('UTC'))
        self.assertIn('Sun 20:00 (Mon UTC)', utc_mondays)

    def test_an_sqlite_without_unixepoch_reads_the_same_days_and_times(self):
        """`unixepoch()` is SQLite 3.38+; below that the same whole seconds come from
        `strftime('%s')`. Both spellings must give every showing the same day and minute."""
        from unittest import mock
        queries = [[('dow', d)] for d in DOW_VALUES] + [
            [('tod', 'tod:22:00..01:00')], [('tod', 'tod:19:00..19:00')],
            [('tod', 'tod:01:30..01:30')]]
        fast = [self.titles(include=q) for q in queries]
        with mock.patch.object(cs, '_HAS_UNIXEPOCH', False):
            self.assertIn('strftime', str(cs._epoch_seconds(EPGEntry.start_time).compile()))
            slow = [self.titles(include=q) for q in queries]
        self.assertEqual(slow, fast)

    def test_an_unknown_day_is_refused(self):
        with self.assertRaises(SearchStateError):
            self.titles(include=[('dow', 'funday')])


class TimeOfDayTests(_DayTimeCase):

    def tod(self, value, exclude=False):
        return self.titles(**({'exclude': [('tod', value)]} if exclude
                              else {'include': [('tod', value)]}))

    def test_both_ends_are_included(self):
        self.assertEqual(self.tod('tod:21:59..22:00'), ['Tue 21:59', 'Tue 22:00'])

    def test_an_exact_minute_and_the_seconds_inside_it(self):
        self.assertEqual(self.tod('tod:21:00..21:00'), ['Sat 21:00:30'])

    def test_wall_clock_on_both_sides_of_a_daylight_saving_change(self):
        self.assertEqual(self.tod('tod:19:00..19:00'), ['Sat 19:00 EDT', 'Sun 19:00 EST'])

    def test_the_repeated_hour_at_fall_back_matches_twice(self):
        self.assertEqual(self.tod('tod:01:30..01:30'), ['Sun 01:30 EDT', 'Sun 01:30 EST'])

    def test_the_spring_forward_gap_has_no_showings_in_it(self):
        """1:59 AM EST and 3:00 AM EDT are one minute apart, and the clock skips the hour
        between them."""
        def sunday(value):
            return self.titles(include=[('dow', 'sun'), ('tod', value)])
        self.assertEqual(sunday('tod:02:00..02:59'), [])
        self.assertEqual(sunday('tod:01:59..03:00'), ['Sun 01:59 EST', 'Sun 03:00 EDT'])
        self.assertEqual(sunday('tod:03:00..03:00'), ['Sun 03:00 EDT'])

    def test_an_end_earlier_than_the_start_runs_past_midnight(self):
        self.assertEqual(self.tod('tod:22:00..01:00'),
                         ['Mon 00:00', 'Sun 23:59', 'Tue 01:00', 'Tue 22:00'])

    def test_a_range_one_minute_short_of_a_day_still_runs_past_midnight(self):
        """10:01 PM to 10:00 PM is every minute of the day - the complement is empty."""
        self.assertEqual(self.tod('tod:22:01..22:00'), sorted(SHOWINGS))

    def test_one_end_alone(self):
        self.assertEqual(self.tod('tod:21:59..'),
                         ['Sun 23:59', 'Tue 21:59', 'Tue 22:00'])
        self.assertEqual(self.tod('tod:..01:00'), ['Mon 00:00', 'Tue 01:00'])

    def test_excluding_a_range(self):
        self.assertEqual(self.tod('tod:00:00..19:59', exclude=True),
                         ['Sat 21:00:30', 'Sun 20:00 (Mon UTC)', 'Sun 23:59', 'Tue 21:59',
                          'Tue 22:00'])

    def test_day_and_time_narrow_each_other(self):
        """Two dimensions AND - which is the reason they are not `when` values."""
        self.assertEqual(self.titles(include=[('dow', 'sun'), ('tod', 'tod:18:00..23:59')]),
                         ['Sun 19:00 EST', 'Sun 20:00 (Mon UTC)', 'Sun 23:59'])

    def test_an_unparseable_range_matches_nothing_and_a_foreign_value_is_refused(self):
        self.assertEqual(self.tod('tod:18:00..25:00'), [])
        with self.assertRaises(SearchStateError):
            self.tod('6pm')


class DayTimeFacetTests(_DayTimeCase):

    def facets(self, include=(), keys=('dow', 'tod')):
        state = self.state(include, facets=keys)
        return compute_facets(state, self.context())

    def expected_days(self, titles):
        counts = dict.fromkeys(DOW_VALUES, 0)
        for t in titles:
            counts[{'Mon': 'mon', 'Tue': 'tue', 'Sat': 'sat', 'Sun': 'sun'}[t[:3]]] += 1
        return counts

    def test_every_day_is_counted_and_an_empty_day_is_a_zero(self):
        counts = self.facets()['dow']
        self.assertEqual(counts, self.expected_days(SHOWINGS))
        self.assertEqual(counts['wed'], 0)

    def test_both_shapes_of_the_shared_scan_agree(self):
        """The deferred-join tally carries the weekday as a key of its own; the joined shape
        groups by it directly. A weekday read after the tally collapsed to one row per
        channel would count every showing on one day."""
        state = SearchState(grain=GRAIN_AIRINGS, standing=only_hiding(), facets=('dow',))
        ctx = self.context()
        preds = cs.base_predicates(state, ctx, text_preds=[])
        split = split_predicates(state, ctx, [])
        self.assertIsNotNone(split)
        tallied = cs._combined_facet_scan(('dow', 'acct'), state, ctx, preds, split=split)
        joined = cs._combined_facet_scan(('dow', 'acct'), state, ctx, preds, split=None)
        self.assertEqual(tallied, joined)
        self.assertEqual(tallied['dow'], self.expected_days(SHOWINGS))
        self.assertEqual(sum(tallied['acct'].values()), len(SHOWINGS))

    def test_a_filtered_day_is_counted_without_its_own_filter(self):
        """Picking Sunday must not zero the other six, or the facet is unusable once used."""
        counts = self.facets(include=[('dow', 'sun')])['dow']
        self.assertEqual(counts, self.expected_days(SHOWINGS))

    def test_days_are_counted_under_a_time_filter(self):
        counts = self.facets(include=[('tod', 'tod:18:00..23:59')])['dow']
        self.assertEqual(counts, self.expected_days(
            ['Sat 21:00:30', 'Sat 19:00 EDT', 'Sun 19:00 EST', 'Sun 20:00 (Mon UTC)',
             'Sun 23:59', 'Tue 21:59', 'Tue 22:00']))

    def test_one_row_per_channel_leaves_every_showing_facet_uncounted(self):
        """"One row per channel" keeps each channel's earliest showing of what the search
        matches. Counted with its own filter removed, a showing-side facet counts each
        channel's earliest showing on ANY day - here the one channel's March showing, so the
        rail read Sunday 1 and every other day 0 while filtering to Tuesday lists a Tuesday
        showing. A count that disagrees with its own list is not given; channel-side facets
        still are (dev/docs/BUGS.md 2026-10-13 03:38 PM)."""
        state = SearchState(grain=GRAIN_AIRINGS, standing=only_hiding('firstonly'),
                            facets=None)
        counted = compute_facets(state, self.context())
        for key in ('when', 'dow', 'tod', 'duration', 'tag'):
            self.assertNotIn(key, counted)
        self.assertEqual(counted['acct'], {str(self.channel.account_id): 1})

        tuesday = search(SearchState(grain=GRAIN_AIRINGS, standing=only_hiding('firstonly'),
                                     facets=(), filters=(DimensionFilter('dow', ('tue',), ()),)),
                         self.context(), want_counts=False)
        self.assertEqual([r.title for r in tuesday.rows], ['Tue 01:00'])

    def test_what_is_left_uncounted_is_named_with_its_reason(self):
        """Exactly the facets compute_facets() leaves out, each with the sentence the rail
        shows - and nothing at all without One row per channel."""
        on = SearchState(grain=GRAIN_AIRINGS, standing=only_hiding('firstonly'), facets=None)
        uncounted = cs.uncounted_facets(on)
        self.assertEqual(sorted(uncounted), ['dow', 'duration', 'tag', 'tod', 'when'])
        self.assertTrue(all('One row per channel' in why for why in uncounted.values()))
        counted = compute_facets(on, self.context())
        self.assertEqual(set(uncounted) & set(counted), set())
        asked = SearchState(grain=GRAIN_AIRINGS, standing=only_hiding('firstonly'),
                            facets=('dow', 'acct'))
        self.assertEqual(sorted(cs.uncounted_facets(asked)), ['dow'])
        off = SearchState(grain=GRAIN_AIRINGS, standing=only_hiding(), facets=None)
        self.assertEqual(cs.uncounted_facets(off), {})

    def test_the_facets_endpoint_says_what_it_left_out(self):
        standing = '&'.join(f'standing={k}' for k in sorted(only_hiding('firstonly')))
        data = self.t.client.get(
            f'/api/channels/search/facets?grain=airings&{standing}').get_json()
        self.assertIn('dow', data['uncounted'])
        self.assertNotIn('dow', data['facets_counted'])
        plain = self.t.client.get('/api/channels/search/facets?grain=airings').get_json()
        self.assertEqual(plain['uncounted'], {})

    def test_a_time_range_counts_only_its_own_value(self):
        self.assertEqual(self.facets()['tod'], {})
        counts = self.facets(include=[('tod', 'tod:21:59..22:00')])['tod']
        self.assertEqual(counts, {'tod:21:59..22:00': 2})


class DayTimeContractTests(_DayTimeCase):

    def test_the_url_spelling_round_trips(self):
        params = [('grain', 'airings'), ('f.dow', 'sat'), ('f.dow', 'sun'),
                  ('x.dow', 'mon'), ('f.tod', 'tod:18:00..23:59')]
        state = SearchState.from_params(MultiDict(params))
        self.assertEqual(state.filter_for('dow').values, ('sat', 'sun'))
        self.assertEqual(state.filter_for('dow').ex, ('mon',))
        self.assertEqual(state.filter_for('tod').values, ('tod:18:00..23:59',))
        for pair in params[1:]:
            self.assertIn(pair, state.to_params())

    def test_both_are_airing_dimensions_only(self):
        airing_keys = [d.key for d in visible_dimensions_for(GRAIN_AIRINGS)]
        channel_keys = [d.key for d in visible_dimensions_for(GRAIN_CHANNELS)]
        self.assertEqual(airing_keys[:4], ['when', 'dow', 'tod', 'duration'])
        self.assertNotIn('dow', channel_keys)
        self.assertNotIn('tod', channel_keys)

    def test_on_the_channel_grain_they_are_parked_not_applied(self):
        state = SearchState(grain=GRAIN_CHANNELS, standing=only_hiding(), facets=(),
                            filters=(DimensionFilter('dow', ('wed',), ()),
                                     DimensionFilter('tod', ('tod:03:00..03:01',), ())))
        result = search(state, self.context(), want_counts=False)
        self.assertEqual([r.id for r in result.rows], [self.channel.id])

    def test_the_offsets_cover_every_stored_start(self):
        """The span is read from the table, so the March and November changes are both in
        the CASE - a span assumed from "now" would miss the March one."""
        ctx = self.context()
        self.assertEqual([until for until, _s in ctx.offset_segments()],
                         [datetime(2026, 3, 8, 7, 0), datetime(2026, 11, 1, 6, 0), None])

    def test_the_api_answers_an_unknown_day_with_a_400(self):
        resp = self.t.client.get('/api/channels/search?grain=airings&facets=&f.dow=funday')
        self.assertEqual(resp.status_code, 400)
        self.assertIn('funday', resp.get_json()['error'])

    def test_the_catalog_serves_the_week_monday_first(self):
        data = self.t.client.get('/api/channels/search/catalog').get_json()
        self.assertEqual([d['value'] for d in data['dow_values']], list(DOW_VALUES))
        self.assertEqual(data['dow_values'][6], {'value': 'sun', 'label': 'Sunday'})


if __name__ == '__main__':
    unittest.main()
