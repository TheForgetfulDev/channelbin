"""util.js's wall-clock-to-instant conversion on DST change days
(dev/docs/BUGS.md 2026-10-31 @ 11:27:30 PM ET).

`tzInputValueToDate()` took the zone's offset at the wall time read as if it were UTC, which
for a US zone is hours before the real instant, so on the day DST ends 2:00-5:59 AM came out
an hour off ("2026-11-01T03:00" read as 2:00 AM EST) and on the day it starts 3:30 AM read as
4:30 AM. These hold the browser's answer to the real instant, and to the server's: the form
posts the wall text and app/tz_utils.py::parse_local_to_utc reads it, so the dialog's notes
and labels are only honest when the two agree on every minute of both change days.

  python3 -m unittest tests.test_tz_wall_js
"""
import json
import os
import shutil
import subprocess
import sys
import unittest
from datetime import datetime, timedelta
from unittest import mock
from zoneinfo import ZoneInfo

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import tz_utils  # noqa: E402

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HARNESS = os.path.join(REPO, 'tests', 'support', 'tz_wall.mjs')
JSDOM = os.path.join(REPO, 'node_modules', 'jsdom')

# Each zone's two change days: the US, a European zone and a southern-hemisphere one, whose
# fall-back is in April.
CHANGE_DAYS = {
    'America/New_York': ['2026-11-01', '2027-03-14'],
    'Europe/London': ['2026-10-25', '2027-03-28'],
    'Australia/Sydney': ['2027-04-04', '2026-10-04'],
}


def _quarter_hours(day):
    start = datetime.fromisoformat(f'{day}T00:00')
    return [(start + timedelta(minutes=15 * i)).strftime('%Y-%m-%dT%H:%M') for i in range(96)]


def _observe(tz, values):
    proc = subprocess.run([shutil.which('node'), HARNESS, REPO, tz, json.dumps(values)],
                          capture_output=True, text=True, cwd=REPO, timeout=120)
    if proc.returncode != 0:
        raise AssertionError(f'harness failed:\n{proc.stdout}\n{proc.stderr}')
    return json.loads(proc.stdout)


def _iso(dt_naive_utc):
    return dt_naive_utc.strftime('%Y-%m-%dT%H:%M:%S.000Z')


@unittest.skipIf(shutil.which('node') is None, 'node not installed')
@unittest.skipIf(not os.path.isdir(JSDOM), 'jsdom not installed (npm install)')
class TzWallJsTests(unittest.TestCase):
    def test_the_measured_cases(self):
        got = _observe('America/New_York', [
            '2026-11-01T03:00', '2027-03-14T03:30', '2026-11-01T01:30', '2027-03-14T02:30',
            '2026-07-04T12:00'])
        self.assertEqual(got['2026-11-01T03:00']['toDate'], '2026-11-01T08:00:00.000Z')
        self.assertEqual(got['2027-03-14T03:30']['toDate'], '2027-03-14T07:30:00.000Z')
        repeated = got['2026-11-01T01:30']
        self.assertEqual((repeated['first'], repeated['second'], repeated['skipped']),
                         ('2026-11-01T05:30:00.000Z', '2026-11-01T06:30:00.000Z', False))
        gap = got['2027-03-14T02:30']
        self.assertEqual((gap['first'], gap['second'], gap['skipped']),
                         ('2027-03-14T07:30:00.000Z', None, True))
        plain = got['2026-07-04T12:00']
        self.assertEqual((plain['first'], plain['second'], plain['skipped']),
                         ('2026-07-04T16:00:00.000Z', None, False))

    def test_browser_and_server_agree_on_every_change_day_minute(self):
        for tz, days in CHANGE_DAYS.items():
            values = [v for day in days for v in _quarter_hours(day)]
            got = _observe(tz, values)
            with mock.patch.object(tz_utils, 'get_display_tz', return_value=ZoneInfo(tz)):
                for v in values:
                    with self.subTest(tz=tz, value=v):
                        self.assertEqual(got[v]['toDate'], _iso(tz_utils.parse_local_to_utc(v)))

    def test_the_second_pass_is_the_other_reading_of_the_same_wall_time(self):
        for tz, days in CHANGE_DAYS.items():
            values = [v for day in days for v in _quarter_hours(day)]
            got = _observe(tz, values)
            zone = ZoneInfo(tz)
            for v in values:
                second = got[v]['second']
                with self.subTest(tz=tz, value=v):
                    later = datetime.fromisoformat(v).replace(tzinfo=zone, fold=1)
                    repeats = later.utcoffset() != later.replace(fold=0).utcoffset()
                    exists = (later.astimezone(ZoneInfo('UTC')).astimezone(zone)
                              .replace(tzinfo=None) == datetime.fromisoformat(v))
                    if repeats and exists:
                        self.assertEqual(second, later.astimezone(ZoneInfo('UTC'))
                                         .strftime('%Y-%m-%dT%H:%M:%S.000Z'))
                    else:
                        self.assertIsNone(second)
                    self.assertEqual(got[v]['skipped'], not exists)


if __name__ == '__main__':
    unittest.main()
