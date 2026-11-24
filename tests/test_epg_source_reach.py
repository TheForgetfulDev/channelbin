"""How far ahead a guide reaches: per source from its directory, per account from the sources
that guide its channels, and the words the Accounts pages show (dev/changelog/1714).

  python3 -m unittest tests.test_epg_source_reach
"""
import os
import re
import sys
import unittest
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tests.support.app import make_test_app  # noqa: E402
from tests.support import seed  # noqa: E402
from app import db  # noqa: E402
from app import epg_sources  # noqa: E402
from app.database import EpgSourceChannel  # noqa: E402
from app.epg_sources import (account_reach, reach_breakdown, reach_from_horizons,  # noqa: E402
                             reach_words, source_reach)

ET = ZoneInfo('America/New_York')
# 10:00 AM Eastern on a Tuesday.
NOW = datetime(2026, 11, 24, 15, 0)


def _utc(local_day, hour=12):
    """A naive-UTC instant at `hour` o'clock Eastern on `local_day`."""
    return (datetime.combine(local_day, datetime.min.time()).replace(hour=hour, tzinfo=ET)
            .astimezone(ZoneInfo('UTC')).replace(tzinfo=None))


class ReachFromHorizonsTests(unittest.TestCase):
    def test_the_day_is_the_median_channels_not_the_furthest_ones(self):
        """One channel running far past the rest moves `latest`, never `day`: on prod 3,570
        of a provider's channels ended Nov 25 and one ran to Nov 29."""
        r = reach_from_horizons([(_utc(date(2026, 11, 25)), 3570),
                                 (_utc(date(2026, 11, 29)), 1)], ET)
        self.assertEqual(r.day, date(2026, 11, 25))
        self.assertEqual(r.latest, date(2026, 11, 29))
        self.assertEqual(r.later, 1)
        self.assertEqual(r.channels, 3571)
        self.assertTrue(r.from_directory)

    def test_at_least_half_the_channels_reach_the_day(self):
        r = reach_from_horizons([(_utc(date(2026, 11, 26)), 4), (_utc(date(2026, 11, 27)), 3),
                                 (_utc(date(2026, 11, 28)), 3)], ET)
        self.assertEqual(r.day, date(2026, 11, 27))
        self.assertEqual(r.later, 3)

    def test_a_listing_ending_at_local_midnight_runs_through_the_day_before(self):
        midnight = _utc(date(2026, 11, 28), hour=0)
        r = reach_from_horizons([(midnight, 5)], ET)
        self.assertEqual(r.day, date(2026, 11, 27))

    def test_days_are_local_and_latest_first(self):
        # 03:00 UTC on Nov 28 is still Nov 27 in Eastern.
        r = reach_from_horizons([(datetime(2026, 11, 28, 3, 0), 2), (_utc(date(2026, 11, 25)), 1),
                                 (datetime(2026, 11, 27, 20, 0), 4)], ET)
        self.assertEqual(r.days, ((date(2026, 11, 27), 6), (date(2026, 11, 25), 1)))

    def test_nothing_is_none(self):
        self.assertIsNone(reach_from_horizons([], ET))
        self.assertIsNone(reach_from_horizons([(None, 3), (_utc(date(2026, 11, 25)), 0)], ET))


class ReachWordsTests(unittest.TestCase):
    def test_one_day_says_how_many_days_ahead(self):
        w = reach_words((date(2026, 11, 28), date(2026, 11, 28)), NOW, ET)
        self.assertEqual(w, {'ran_out': False, 'text': 'Sat Nov 28 (4 days)',
                             'inline': 'Sat Nov 28 (4 days)'})

    def test_a_range_names_both_ends(self):
        w = reach_words((date(2026, 11, 25), date(2026, 11, 27)), NOW, ET)
        self.assertEqual(w['text'], 'Tomorrow - Fri Nov 27 (up to 3 days)')
        self.assertEqual(w['inline'], 'tomorrow - Fri Nov 27 (up to 3 days)')

    def test_a_date_only_form_for_a_narrow_cell(self):
        w = reach_words((date(2026, 11, 28),) * 2, NOW, ET, with_days=False)
        self.assertEqual(w['text'], 'Sat Nov 28')

    def test_today_and_tomorrow_need_no_count(self):
        self.assertEqual(reach_words((date(2026, 11, 25),) * 2, NOW, ET)['text'], 'Tomorrow')
        self.assertEqual(reach_words((date(2026, 11, 24),) * 2, NOW, ET)['inline'], 'today')

    def test_a_guide_behind_today_ran_out(self):
        w = reach_words((date(2026, 11, 22), date(2026, 11, 23)), NOW, ET)
        self.assertEqual(w, {'ran_out': True, 'text': 'Yesterday', 'inline': 'yesterday'})

    def test_no_span_is_none(self):
        self.assertIsNone(reach_words(None, NOW, ET))


class ReachBreakdownTests(unittest.TestCase):
    def test_counts_and_shares_by_day_with_past_days_together(self):
        r = reach_from_horizons([(_utc(date(2026, 11, 28)), 1), (_utc(date(2026, 11, 27)), 897),
                                 (_utc(date(2026, 11, 23)), 100), (_utc(date(2026, 11, 22)), 2)],
                                ET)
        self.assertEqual(reach_breakdown(r, NOW, ET).split('\n'), [
            '1,000 channels in this guide, by the last day their listings run through.',
            'Sat Nov 28 (4 days): 1 (<1%)',
            'Fri Nov 27 (3 days): 897 (90%)',
            'Already ran out: 102 (10%)',
        ])

    def test_a_source_measured_from_held_listings_says_so(self):
        r = epg_sources.Reach(date(2026, 11, 26), date(2026, 11, 26), 0, 0, (), False)
        self.assertIn('no directory', reach_breakdown(r, NOW, ET))


class AccountReachTests(unittest.TestCase):
    def test_only_sources_guiding_a_channel_here_count(self):
        """A source the account reads that guides none of its channels (johnpulse on
        account 5) says nothing about the account's guide, however far it reaches."""
        days = {1: date(2026, 11, 25), 2: date(2026, 11, 27), 3: date(2026, 12, 1)}
        self.assertEqual(account_reach({1: 372, 2: 2965, 3: 0}, days),
                         (date(2026, 11, 25), date(2026, 11, 27), date(2026, 11, 27)))
        self.assertEqual(account_reach({2: 5}, days), (date(2026, 11, 27),) * 3)
        self.assertIsNone(account_reach({3: 0}, days))
        self.assertIsNone(account_reach({9: 4}, days))

    def test_most_is_the_day_at_least_half_its_channels_reach(self):
        """The list row's one date: weighted by how many channels each source guides
        here, not the longest source's day."""
        days = {1: date(2026, 11, 25), 2: date(2026, 11, 27)}
        self.assertEqual(account_reach({1: 3000, 2: 400}, days).most, date(2026, 11, 25))
        self.assertEqual(account_reach({1: 400, 2: 3000}, days).most, date(2026, 11, 27))
        self.assertEqual(account_reach({1: 1, 2: 1}, days).most, date(2026, 11, 25))


class SourceReachDbTests(unittest.TestCase):
    def setUp(self):
        self.t = make_test_app()
        self.acc = seed.make_account(name='Acct A')
        self.src = seed.make_epg_source(self.acc)

    def tearDown(self):
        self.t.cleanup()

    def _dir(self, source, xml_id, until, entry_count=10):
        db.session.add(EpgSourceChannel(source_id=source.id, xml_id=xml_id,
                                        entry_count=entry_count, horizon_until=until))

    def test_directory_rows_are_counted_and_run_out_rows_are_not(self):
        far = datetime.utcnow() + timedelta(days=4)
        near = datetime.utcnow() + timedelta(days=1)
        for i in range(3):
            self._dir(self.src, f'a{i}', far - timedelta(minutes=i))
        self._dir(self.src, 'b', near)
        # retire_expired_listings() zeroes a run-out row's count and keeps its horizon.
        self._dir(self.src, 'gone', far + timedelta(days=2), entry_count=0)
        db.session.commit()
        r = source_reach([self.src.id], ET)[self.src.id]
        self.assertEqual(r.channels, 4)
        self.assertEqual(r.day, epg_sources.last_local_day(far, ET))
        self.assertEqual(r.latest, r.day)
        self.assertEqual(sum(n for _d, n in r.days), 4)

    def test_a_source_with_no_directory_is_measured_from_its_held_listings(self):
        ch = seed.make_channel(self.acc)
        seed.make_epg_entry(ch, offset_minutes=60 * 50, source_id=self.src.id)
        db.session.commit()
        r = source_reach([self.src.id], ET)[self.src.id]
        self.assertFalse(r.from_directory)
        self.assertEqual(r.days, ())

    def test_a_source_with_a_directory_of_run_out_rows_has_no_reach(self):
        self._dir(self.src, 'gone', datetime.utcnow() + timedelta(days=3), entry_count=0)
        db.session.commit()
        self.assertEqual(source_reach([self.src.id], ET), {})


class AccountPagesTests(unittest.TestCase):
    def setUp(self):
        self.t = make_test_app()
        self.client = self.t.app.test_client()
        self.guided = seed.make_account(name='Guided')
        self.bare = seed.make_account(name='Bare')
        self.src = seed.make_epg_source(self.guided)
        self.ch = seed.make_channel(self.guided, epg_source_id=self.src.id)
        self.until = datetime.utcnow() + timedelta(days=4)
        for i in range(5):
            db.session.add(EpgSourceChannel(source_id=self.src.id, xml_id=f'x{i}',
                                            entry_count=10, horizon_until=self.until))
        db.session.add(EpgSourceChannel(source_id=self.src.id, xml_id='far', entry_count=10,
                                        horizon_until=self.until + timedelta(days=3)))
        db.session.commit()

    def tearDown(self):
        self.t.cleanup()

    def _expected(self):
        from app.tz_utils import get_display_tz
        tz = get_display_tz()
        day = epg_sources.last_local_day(self.until, tz)
        return reach_words((day, day), datetime.utcnow(), tz)

    def test_the_list_row_says_how_far_the_guide_reaches(self):
        """Under the Programs count, the date only - the column has no room for more - and
        the count of days in its tooltip."""
        from app.tz_utils import date_label, get_display_tz
        tz = get_display_tz()
        day = epg_sources.last_local_day(self.until, tz)
        html = self.client.get('/accounts').get_data(as_text=True)
        notes = re.findall(r'<span class="a-note tip-plain" data-tip="Guide reaches\.([^"]*)">'
                           r'([^<]*)</span>', html)
        self.assertEqual(len(notes), 1, 'only the account with a guided channel')
        tip, text = notes[0]
        self.assertEqual(text, 'to ' + date_label(day, datetime.utcnow(), tz))
        self.assertIn(f'listings through {self._expected()["inline"]}.', tip)

    def test_the_account_page_names_it_on_the_content_card_and_each_source(self):
        html = self.client.get(f'/accounts/{self.guided.id}').get_data(as_text=True)
        self.assertRegex(html, r'>Guide reaches</span><span class="sv">'
                         + re.escape(self._expected()['text']) + '<')
        cell = re.search(r'<td data-label="Reaches">(.*?)</td>', html, re.S).group(1)
        self.assertIn(self._expected()['text'], cell)
        self.assertIn('6 channels in this guide', cell)
        self.assertIn('1 channel runs later, to ', cell)

    def test_an_account_guiding_nothing_shows_no_reach(self):
        html = self.client.get(f'/accounts/{self.bare.id}').get_data(as_text=True)
        self.assertNotIn('Guide reaches', html)


if __name__ == '__main__':
    unittest.main()
