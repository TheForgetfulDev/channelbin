"""Tier 1 pure units for timezone conversion/formatting (app/tz_utils.py).

tz_utils reads the display timezone + time_format from config via a *local*
`from .config import load_config` in each function, so a pure test controls them by
patching `app.config.load_config` - no app, no DB. The zone is pinned NON-Eastern
(America/Los_Angeles) on purpose: the whole point of the migration away from hardcoded
Eastern (CLAUDE.md Timezone Rules) is that display honors config, not a wired-in zone.
Storage is naive UTC; round-trips must be lossless.
"""
import os
import sys
import unittest
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import app.config as config_mod  # noqa: E402
from app import tz_utils  # noqa: E402


class _PatchDisplayConfig:
    """Patch app.config.load_config to return a fixed display config (tz_utils imports
    it locally per call, so this binding is what it resolves)."""

    def __init__(self, timezone='America/Los_Angeles', time_format='24h'):
        self._cfg = {'display': {'timezone': timezone, 'time_format': time_format}}

    def __enter__(self):
        self._orig = config_mod.load_config
        config_mod.load_config = lambda *a, **k: self._cfg
        return self

    def __exit__(self, *exc):
        config_mod.load_config = self._orig
        return False


class DisplayTzTests(unittest.TestCase):
    def setUp(self):
        self._patch = _PatchDisplayConfig().__enter__()

    def tearDown(self):
        self._patch.__exit__(None, None, None)

    def test_display_tz_is_configured_non_eastern(self):
        self.assertEqual(tz_utils.get_display_tz_name(), 'America/Los_Angeles')

    def test_to_local_converts_utc_to_pacific(self):
        # 2026-07-17 20:00 UTC = 13:00 PDT (UTC-7 in July)
        local = tz_utils.to_local(datetime(2026, 7, 17, 20, 0))
        self.assertEqual((local.hour, local.minute), (13, 0))

    def test_naive_utc_round_trip(self):
        utc = datetime(2026, 7, 17, 20, 0)
        back = tz_utils.to_naive_utc(tz_utils.to_local(utc))
        self.assertEqual(back, utc)

    def test_parse_local_to_utc(self):
        # 13:00 Pacific (PDT, UTC-7) → 20:00 naive UTC
        self.assertEqual(tz_utils.parse_local_to_utc('2026-07-17T13:00'),
                         datetime(2026, 7, 17, 20, 0))

    def test_parse_local_input_round_trip(self):
        utc = datetime(2026, 7, 17, 20, 0)
        self.assertEqual(tz_utils.parse_local_to_utc(tz_utils.local_input_value(utc)), utc)

    def test_parse_local_to_utc_rejects_bad_input(self):
        for bad in (None, '', 'not-a-date', 12345):
            with self.assertRaises(ValueError):
                tz_utils.parse_local_to_utc(bad)

    def test_invalid_timezone_falls_back(self):
        with _PatchDisplayConfig(timezone='Not/AZone'):
            # falls back to Eastern rather than raising
            self.assertEqual(tz_utils.get_display_tz().key, 'America/New_York')


class TimeFormatTests(unittest.TestCase):
    def test_24h_clock_style(self):
        with _PatchDisplayConfig(time_format='24h'):
            self.assertTrue(tz_utils.is_24h())
            # 20:00 UTC → 13:00 PDT, 24h clock
            self.assertEqual(tz_utils.format_local(datetime(2026, 7, 17, 20, 0), 'clock'),
                             '13:00')

    def test_12h_clock_style(self):
        with _PatchDisplayConfig(time_format='12h'):
            self.assertFalse(tz_utils.is_24h())
            self.assertEqual(tz_utils.format_local(datetime(2026, 7, 17, 20, 0), 'clock'),
                             '1:00 PM')

    def test_24h_datetime_sec_style(self):
        """Guards BUGS.md 2026-07-18 - the recording-detail event log needs seconds so a
        stall/restart burst inside one minute stays readable."""
        with _PatchDisplayConfig(time_format='24h'):
            self.assertEqual(
                tz_utils.format_local(datetime(2026, 7, 17, 20, 4, 7), 'datetime_sec'),
                'Jul 17, 2026 13:04:07 PDT')

    def test_12h_datetime_sec_style(self):
        with _PatchDisplayConfig(time_format='12h'):
            self.assertEqual(
                tz_utils.format_local(datetime(2026, 7, 17, 20, 4, 7), 'datetime_sec'),
                'Jul 17, 2026 01:04:07 PM PDT')

    def test_datetime_sec_distinguishes_same_minute_events(self):
        """Two events one second apart must not render identically (the actual defect)."""
        with _PatchDisplayConfig(time_format='12h'):
            a = tz_utils.format_local(datetime(2026, 7, 17, 20, 4, 7), 'datetime_sec')
            b = tz_utils.format_local(datetime(2026, 7, 17, 20, 4, 8), 'datetime_sec')
            self.assertNotEqual(a, b)
            # ...while the seconds-less style they replaced still collides.
            self.assertEqual(tz_utils.format_local(datetime(2026, 7, 17, 20, 4, 7), 'datetime'),
                             tz_utils.format_local(datetime(2026, 7, 17, 20, 4, 8), 'datetime'))

    def test_none_datetime_returns_placeholder(self):
        with _PatchDisplayConfig():
            self.assertEqual(tz_utils.format_local(None), '-')
            self.assertIsNone(tz_utils.format_local(None, none_value=None))


if __name__ == '__main__':
    unittest.main(verbosity=2)
