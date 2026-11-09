"""Reading an event channel's name for its status and clock (app/event_names.py, design 6.8).

Every name below is a shape seen on prod's accounts (snapshots of 2026-11-04 and 2026-11-07), with
made-up teams where a team is named. Pure functions: no app, no database.
  python3 -m unittest tests.test_event_names
"""
import os
import sys
import unittest
from datetime import date, datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import event_names  # noqa: E402
from app.event_names import LEVEL_DATE, LEVEL_NONE, LEVEL_TZ  # noqa: E402

NOW = datetime(2026, 11, 7, 12, 0)


def clock(name, now=NOW):
    return event_names.read(name, now).clock


class TimezoneTests(unittest.TestCase):
    def test_viaplay_day_month_clock_and_abbreviation(self):
        c = clock('NEXT | PHL HARBORVIEW - IRONVALE (ENG) | Sat 07 Nov 13:05 EST (US) | 8K EXCLUSIVE | US: PPV 2')
        self.assertEqual((c.level, c.at, c.day), (LEVEL_TZ, datetime(2026, 11, 7, 18, 5), date(2026, 11, 7)))
        self.assertEqual(c.text, '07 Nov 13:05 EST')

    def test_a_european_offset_crosses_midnight_backwards(self):
        c = clock('NEXT | PHL NORTHGATE - METRO CITY | Sun 08 Nov 01:05 CET (DK) | 8K EXCLUSIVE | DK: PPV 59')
        self.assertEqual(c.at, datetime(2026, 11, 8, 0, 5))

    def test_iso_date_then_a_bracketed_gmt_clock(self):
        c = clock('Next | Harborview FC vs. Ironvale Anchors | Pretend Division | 2026-11-04 | 12:30 (GMT) | 8K')
        self.assertEqual((c.level, c.at), (LEVEL_TZ, datetime(2026, 11, 4, 12, 30)))

    def test_a_fixed_daylight_abbreviation(self):
        self.assertEqual(clock('Gulls at Bears Jul 8 7:00 PM EDT', now=datetime(2026, 7, 1)).at,
                         datetime(2026, 7, 8, 23, 0))

    def test_a_generic_us_zone_is_no_timezone(self):
        """ET is -5 or -4 by daylight time, and no zone is named in app code outside tz_utils and config."""
        c = clock('US (ESPN+ 182) | Gulls vs. Bears Nov 07 2:00PM ET (2026-11-07 14:03:20)')
        self.assertEqual((c.level, c.day), (LEVEL_DATE, date(2026, 11, 7)))

    def test_an_abbreviation_that_means_two_places_is_no_timezone(self):
        """CST is US Central and China; IST is India, Ireland and Israel."""
        for name in ('Gulls at Bears Nov 8 7:00 PM CST', 'Gulls at Bears Nov 8 19:00 IST'):
            with self.subTest(name=name):
                self.assertEqual(clock(name).level, LEVEL_DATE)

    def test_an_ordinary_word_after_the_clock_is_not_a_timezone(self):
        """Providers write timezones in capitals; `et` is French for "and", `West` a feed's name."""
        for name in ('Gulls at Bears Nov 8 7:00 PM West Coast Feed', 'Gulls - Bears 08 Nov 19:00 et 21:00',
                     'Gulls at Bears Nov 8 7:00 PM WEST'):
            with self.subTest(name=name):
                self.assertEqual(clock(name).level, LEVEL_DATE)

    def test_a_lower_case_meridiem_still_reads(self):
        self.assertEqual(clock('Gulls at Bears Nov 8 7:00 p.m. EST').at, datetime(2026, 11, 9, 0, 0))

    def test_an_abbreviation_not_right_after_the_clock_is_not_read(self):
        self.assertEqual(clock('Gulls at Bears Nov 8 19:00 | CT Network').level, LEVEL_DATE)


class DateOnlyTests(unittest.TestCase):
    def test_a_clock_with_no_timezone_counts_only_on_its_date(self):
        """`Nov 5 2:30 AM :Viaplay FI` is Finland's time, so only the date is used."""
        for name, day in (('PHL: Harborview vs Ironvale @ Nov 7 12:30 PM :Sportsnet+  08', date(2026, 11, 7)),
                          ('PHL: Harborview Gulls vs Ironvale Bears @ Nov 6 03:05 :Telia FI  02', date(2026, 11, 6)),
                          ('Nascar: Pretend Cup Series Championship Race @ Nov 7 20:30 PM :MAX ES  03',
                           date(2026, 11, 7))):
            with self.subTest(name=name):
                c = clock(name)
                self.assertEqual((c.level, c.at, c.day), (LEVEL_DATE, None, day))

    def test_an_espn_plus_stamp_is_when_the_stream_opens(self):
        c = clock('US (ESPN+ 420) | PHL: Gulls vs. Bears (2026-11-07 19:04:05)')
        self.assertEqual((c.level, c.day), (LEVEL_DATE, date(2026, 11, 7)))

    def test_month_first_numeric_date_with_a_day_past_twelve(self):
        c = clock('(JM) PLAY 34 (D): PHL | Harborview Gulls vs. Ironvale Bears | 01-18-2024 7:00pm')
        self.assertEqual(c.day, date(2024, 1, 18))

    def test_a_year_less_day_and_month_before_a_clock(self):
        self.assertEqual(clock('Flo 235: Pretend Modified Tour at Northgate Speedway - 23/10 19:15').day,
                         date(2026, 10, 23))

    def test_a_year_less_date_in_late_december_can_be_next_january(self):
        self.assertEqual(clock('Gulls at Bears Jan 2 1:00 PM', now=datetime(2026, 12, 30)).day, date(2027, 1, 2))


class AmbiguousDateTests(unittest.TestCase):
    def test_a_numeric_date_whose_other_reading_is_months_away(self):
        """`03-11-2026` on Nov 7 is Nov 3, not Mar 11."""
        c = clock('End | Rublev vs. Etcheverry | all | 03-11-2026 | 00:00 (GMT) | ...')
        self.assertEqual((c.level, c.at), (LEVEL_TZ, datetime(2026, 11, 3, 0, 0)))

    def test_a_numeric_date_that_reads_two_ways_near_now_is_no_date(self):
        """July 8 and August 7 are both within three weeks of July 20."""
        self.assertEqual(clock('x 07-08-2026 10:00 GMT', now=datetime(2026, 7, 20)).level, LEVEL_NONE)

    def test_a_date_that_does_not_exist(self):
        self.assertEqual(clock('Gulls at Bears Feb 30 1:00 PM ET').level, LEVEL_NONE)


class NoDateTests(unittest.TestCase):
    def test_a_clock_with_no_date(self):
        for name in ('PHL | 01 - 7:30pm Gulls @ Bears', 'NFL  | 03 - 1pm Cowboys at Colts', 'Game 7:00 PM ET',
                     'PHL 01: 7pm Gulls @ Bears'):
            with self.subTest(name=name):
                self.assertEqual(clock(name), event_names.NameClock(LEVEL_NONE))

    def test_a_team_name_is_not_a_month(self):
        self.assertEqual(clock('Marlins 3 at Mets').level, LEVEL_NONE)

    def test_a_score_is_not_a_clock_or_a_date(self):
        self.assertEqual(clock('Next | Gulls - Bears | Final | Game 1:7 2026 | 8K').level, LEVEL_NONE)


class StatusTests(unittest.TestCase):
    def test_status_words_at_the_front(self):
        for name, status in (('NEXT | PHL GULLS - BEARS | Sat 07 Nov 13:05 EST', 'next'),
                             ('Live | Gulls @ Bears | PHL.TV | 8K EXCLUSIVE', 'live'),
                             ('End | Rublev vs. Etcheverry | all', 'end'),
                             ('PHL: Gulls vs Bears @ Nov 7 12:30 PM', None),
                             ('Live Gulls Bears', None)):
            with self.subTest(name=name):
                self.assertEqual(event_names.read(name, NOW).status, status)

    def test_no_name(self):
        self.assertEqual(event_names.read(None, NOW), event_names.ReadName(None, event_names.NameClock(LEVEL_NONE)))


if __name__ == '__main__':
    unittest.main()
