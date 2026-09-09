"""Tier 0 - the maintenance-window "Run at" radio in static/js/schedule-fields.js
(mountScheduleFields), added alongside the server-side maintenance window feature
(app/check_window.py). Python cannot reach this - it is pure client-side DOM wiring - so
this drives the real shipped file in jsdom via tests/support/schedule_fields.mjs against a
hand-built fragment matching what templates/_macros.html::recur_schedule_fields renders,
and asserts on what it reports back.

What it covers: payload() omits recur_time and sets use_window:true when the window radio
is selected, the time input hides/shows with the radio, and prefill() (used by both the
create-check modal and the group detail Settings modal) lands on the right radio and value
for both directions. What it cannot cover: how the radio actually looks - jsdom computes no
layout - or the check-modal.js capacity-line fetch, which is exercised manually (dev/
changelog - maintenance window UI) rather than mocked here.

Runs against no app/DB at all - this widget has none.
  python3 -m unittest tests.test_check_window_js
"""
import json
import os
import shutil
import subprocess
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HARNESS = os.path.join(REPO, 'tests', 'support', 'schedule_fields.mjs')
# The harness imports jsdom, so `node` on PATH is not enough - node_modules/ is gitignored
# and never ships, so a checkout that has not run `npm install` has node but no jsdom. Every
# other jsdom-backed test carries both guards; this one carried only the first and therefore
# raised in setUpClass instead of skipping (found by the clean-room run, dev/changelog/520).
JSDOM = os.path.join(REPO, 'node_modules', 'jsdom')


@unittest.skipIf(shutil.which('node') is None, 'node not installed')
@unittest.skipIf(not os.path.isdir(JSDOM), 'jsdom not installed (npm install)')
class ScheduleFieldsWindowModeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        proc = subprocess.run(['node', HARNESS, REPO], capture_output=True, text=True,
                              timeout=60, cwd=REPO)
        if proc.returncode != 0:
            raise AssertionError(f'node harness failed:\n{proc.stderr}')
        cls.r = json.loads(proc.stdout)

    def test_no_javascript_errors(self):
        self.assertEqual(self.r['errors'], [])

    def test_default_mode_is_the_maintenance_window(self):
        """The macro's own `checked` attribute defaults to the maintenance window
        (dev/changelog/830), so an untouched mount produces a valid window payload with no
        time typed - and the irrelevant time input is already hidden on first paint rather
        than after a JS pass."""
        self.assertEqual(self.r['defaultPayload'],
                         {'recurring': True, 'recur_day': 0, 'use_window': True})
        self.assertTrue(self.r['defaultTimeFieldHidden'],
                        'the time input must be hidden on first paint in window mode')

    def test_choosing_a_specific_time_still_requires_one(self):
        """The old default's behavior, now reached by choosing the other radio: exact-time
        mode with nothing typed is still an error rather than a silent 00:00."""
        self.assertEqual(self.r['exactTimeNoTimePayload'],
                         {'error': 'Pick a time for the recurring run.'})

    def test_selecting_window_radio_omits_recur_time(self):
        self.assertEqual(self.r['windowModePayload'],
                         {'recurring': True, 'recur_day': 0, 'use_window': True})
        self.assertTrue(self.r['windowModeValue']['useWindow'])
        self.assertTrue(self.r['timeFieldHiddenInWindowMode'],
                        'the time input must hide while window mode is selected')

    def test_switching_back_to_exact_time_restores_the_time_field(self):
        self.assertTrue(self.r['timeFieldVisibleAfterSwitchBack'])
        self.assertEqual(self.r['exactTimePayload'],
                         {'recurring': True, 'recur_day': 0, 'recur_time': '04:15', 'use_window': False})

    def test_prefill_use_window_true_selects_the_window_radio(self):
        p = self.r['prefillWindow']
        self.assertEqual(p['day'], '3')
        self.assertTrue(p['windowChecked'])
        self.assertTrue(p['timeFieldHidden'])
        self.assertEqual(p['payload'], {'recurring': True, 'recur_day': 3, 'use_window': True})

    def test_prefill_use_window_false_selects_the_exact_time_radio(self):
        p = self.r['prefillExactTime']
        self.assertEqual(p['day'], '2')
        self.assertTrue(p['timeChecked'])
        self.assertTrue(p['timeFieldVisible'])
        self.assertEqual(p['payload'],
                         {'recurring': True, 'recur_day': 2, 'recur_time': '05:30', 'use_window': False})

    def test_prefill_without_use_window_leaves_the_radio_where_it_is(self):
        """Absent is not false (dev/changelog/830). A caller that states no use_window has
        no opinion, so whatever the radio shows survives - which is what lets the macro's
        rendered default actually BE the default instead of being overridden on mount.

        Asserted in both directions so a pass cannot be "prefill always prefers time":
        from exact-time mode it stays on time, from window mode it stays on window.
        """
        from_time = self.r['prefillNoOpinionFromTime']
        self.assertEqual(from_time['day'], '5')
        self.assertTrue(from_time['timeChecked'])
        self.assertFalse(from_time['windowChecked'])
        self.assertEqual(from_time['payload'],
                         {'recurring': True, 'recur_day': 5, 'recur_time': '06:45',
                          'use_window': False})

        from_window = self.r['prefillNoOpinionFromWindow']
        self.assertEqual(from_window['day'], '4')
        self.assertTrue(from_window['windowChecked'],
                        'an omitted use_window must not knock the radio off the window')
        self.assertTrue(from_window['timeFieldHidden'])
        self.assertEqual(from_window['payload'],
                         {'recurring': True, 'recur_day': 4, 'use_window': True})


if __name__ == '__main__':
    unittest.main(verbosity=2)
