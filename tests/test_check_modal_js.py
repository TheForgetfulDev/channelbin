"""Tier 0 - the pure client-side math behind the "Create a health check" modal
(static/js/check-modal.js, dev/changelog/321 spec / 325 implementation).

The readout's run-time estimate, the "First run:" line and the POST body are computed in
the browser, so the Python suite cannot reach them - but they are the parts most likely to
be quietly wrong, and the mockup round proved it by catching three of these with its own
throwaway verify script (dev/changelog/321 "Outcome"). That script was gitignored and is
gone; this is its assertion list, kept.

How it works: the helpers are declared at file top level in check-modal.js precisely so
this can evaluate the file in node and call them directly. Nothing test-only lives in the
shipped file - no module.exports tail, no injected globals. The file's DOM-touching parts
are never invoked, and defining them costs nothing.

Two things the estimate must get right, both spec'd in 321 and both easy to get wrong:
the tester waits BETWEEN channels (N-1 waits, not N), and "already passed" is evaluated
against the DISPLAY timezone's wall clock, never the browser's - which is why every
helper here takes `nowWall` as an argument instead of reading a clock.
"""
import json
import os
import shutil
import subprocess
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MODAL_JS = os.path.join(REPO, 'static', 'js', 'check-modal.js')

_EXPORTS = ('ccRunSeconds, ccDurationPhrase, ccNextRun, ccClock, ccWhenLabel, ccPayload, '
            'ccWindowRecommendLine')

_HARNESS = f"""
const fs = require('fs');
const src = fs.readFileSync(process.argv[1], 'utf8');
// check-modal.js relies on util.js::escHtml being a global (loaded via <script> on the
// real page) - a minimal stand-in here, since ccWindowCapacityLine is the first of these
// top-level pure helpers to call it and `new Function(...)` bodies only see real globals,
// not this script's own top-level const/let.
global.escHtml = (s) => String(s).replace(/[&<>"']/g, (c) => ({{
  '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;',
}}[c]));
const api = new Function(src + '\\nreturn {{{_EXPORTS}}};')();
const {{{_EXPORTS}}} = api;
console.log(JSON.stringify(eval(process.argv[2])));
"""

# A fixed Saturday afternoon in the display timezone, as util.js::dateToTzInputValue
# renders it. Every next-run case below is relative to this, so none of them depend on
# when the suite happens to run or on the machine's own zone.
NOW = '2026-07-25T14:30'


@unittest.skipIf(shutil.which('node') is None, 'node not installed')
class _Base(unittest.TestCase):

    def evaluate(self, expr):
        proc = subprocess.run(['node', '-e', _HARNESS, MODAL_JS, expr],
                              capture_output=True, text=True, cwd=REPO, timeout=60)
        if proc.returncode != 0:
            self.fail(f'node failed evaluating `{expr}`:\n{proc.stderr}')
        return json.loads(proc.stdout)


class RunTimeEstimateTests(_Base):
    """"One run over N channels takes about X." Wrong here and the modal misstates how
    long the user's group is tied up."""

    def test_waits_happen_between_channels_not_after_the_last_one(self):
        self.assertEqual(self.evaluate('ccRunSeconds(3, 120, 180)'), 3 * 120 + 2 * 180)

    def test_single_channel_has_no_wait_at_all(self):
        self.assertEqual(self.evaluate('ccRunSeconds(1, 120, 180)'), 120)

    def test_empty_group_is_zero(self):
        self.assertEqual(self.evaluate('ccRunSeconds(0, 120, 180)'), 0)

    def test_under_a_minute(self):
        self.assertEqual(self.evaluate('ccDurationPhrase(45)'), 'under a minute')
        self.assertEqual(self.evaluate('ccDurationPhrase(59)'), 'under a minute')

    def test_minutes_are_singular_at_one(self):
        self.assertEqual(self.evaluate('ccDurationPhrase(60)'), 'about 1 minute')

    def test_minutes_up_to_ninety(self):
        self.assertEqual(self.evaluate('ccDurationPhrase(720)'), 'about 12 minutes')
        self.assertEqual(self.evaluate('ccDurationPhrase(89 * 60)'), 'about 89 minutes')

    def test_hours_take_over_at_ninety_minutes(self):
        self.assertEqual(self.evaluate('ccDurationPhrase(90 * 60)'), 'about 1 hour 30 minutes')

    def test_whole_hours_omit_the_minutes(self):
        self.assertEqual(self.evaluate('ccDurationPhrase(7200)'), 'about 2 hours')


class NextRunTests(_Base):
    """The "First run:" line. recur_day is 0 = every day, 1 = Sunday ... 7 = Saturday -
    off by one here and the readout promises a different day than the job will run."""

    def test_every_day_picks_today_when_the_time_is_still_ahead(self):
        got = self.evaluate(f'ccNextRun(0, "15:00", "{NOW}")')
        self.assertEqual((got['month'], got['day']), (7, 25))

    def test_every_day_rolls_to_tomorrow_once_the_time_has_passed(self):
        got = self.evaluate(f'ccNextRun(0, "10:00", "{NOW}")')
        self.assertEqual((got['month'], got['day']), (7, 26))

    def test_named_day_finds_the_next_one(self):
        """recur_day 1 = Sunday, from a Saturday, is tomorrow."""
        got = self.evaluate(f'ccNextRun(1, "03:00", "{NOW}")')
        self.assertEqual((got['month'], got['day'], got['dow']), (7, 26, 0))

    def test_today_still_counts_when_the_time_is_ahead(self):
        got = self.evaluate(f'ccNextRun(7, "15:00", "{NOW}")')
        self.assertEqual((got['month'], got['day']), (7, 25))

    def test_today_rolls_a_full_week_once_the_time_has_passed(self):
        """Not "later today" and not "tomorrow" - the same weekday next week, across a
        month boundary."""
        got = self.evaluate(f'ccNextRun(7, "10:00", "{NOW}")')
        self.assertEqual((got['month'], got['day'], got['dow']), (8, 1, 6))

    def test_no_time_yields_nothing_rather_than_a_guess(self):
        self.assertIsNone(self.evaluate(f'ccNextRun(1, "", "{NOW}")'))

    def test_midnight_reads_as_twelve_am_not_zero(self):
        self.assertEqual(self.evaluate('ccClock("00:00", true)'), '12:00 AM')
        self.assertEqual(self.evaluate('ccClock("12:00", true)'), '12:00 PM')

    def test_twenty_four_hour_format_is_honoured(self):
        self.assertEqual(self.evaluate('ccClock("13:05", false)'), '13:05')
        self.assertEqual(self.evaluate('ccClock("13:05", true)'), '1:05 PM')

    def test_the_label_reads_as_the_spec_writes_it(self):
        got = self.evaluate(f'ccWhenLabel(ccNextRun(1, "03:00", "{NOW}"), true)')
        self.assertEqual(got, 'Sunday, Jul 26 at 3:00 AM')


class PayloadTests(_Base):
    """The POST body per action (dev/changelog/321 "POST body"). The endpoint is
    unchanged, so this is what has to match it."""

    _BASE = '{name: "G - health check", channelIds: [7], profileId: 2, action: "%s"%s}'

    def _payload(self, action, extra=''):
        return self.evaluate(f'ccPayload({self._BASE % (action, extra)})')

    def test_queue_sends_no_schedule_keys(self):
        body = self._payload('queue')
        self.assertEqual(body, {'name': 'G - health check', 'action': 'queue',
                                'channel_ids': [7], 'profile_id': 2})

    def test_start_sends_no_schedule_keys(self):
        self.assertNotIn('recurring', self._payload('start'))

    def test_schedule_recurring_carries_day_and_time(self):
        body = self._payload('schedule', ', schedule: {recurring: true, recur_day: 1, recur_time: "03:00"}')
        self.assertTrue(body['recurring'])
        self.assertEqual((body['recur_day'], body['recur_time']), (1, '03:00'))
        self.assertNotIn('scheduled_time', body)

    def test_schedule_one_off_carries_the_moment(self):
        body = self._payload('schedule', ', schedule: {recurring: false, scheduled_time: "2026-08-01T03:00"}')
        self.assertFalse(body['recurring'])
        self.assertEqual(body['scheduled_time'], '2026-08-01T03:00')
        self.assertNotIn('recur_day', body)

    def test_a_schedule_is_ignored_unless_the_action_is_schedule(self):
        """Switching to Run now after filling the schedule fields must not smuggle them
        into the body - the route would then register a recurrence for a started job."""
        body = self._payload('start', ', schedule: {recurring: true, recur_day: 1, recur_time: "03:00"}')
        self.assertNotIn('recurring', body)

    def test_empty_profile_id_becomes_null_never_an_empty_string(self):
        body = self.evaluate('ccPayload({name: "n", channelIds: [1], profileId: "", action: "queue"})')
        self.assertIsNone(body['profile_id'])

    def test_missing_profile_id_becomes_null(self):
        body = self.evaluate('ccPayload({name: "n", channelIds: [1], action: "queue"})')
        self.assertIsNone(body['profile_id'])

    def test_the_selection_is_sent_as_channel_ids_and_never_as_a_group(self):
        """This modal only ever creates, so the body always describes a selection the
        route will mint a group around. `attach_group_id` must never appear: every group
        already carries its one check and the route answers that shape with a 409
        (dev/changelog/1077, `1078`)."""
        body = self.evaluate('ccPayload({name: "n", channelIds: [5, 6], action: "queue"})')
        self.assertEqual(body['channel_ids'], [5, 6])
        self.assertNotIn('attach_group_id', body)

    def test_nameless_omits_name_entirely_rather_than_sending_an_empty_one(self):
        """A health check has no name of its own - the route derives one from the group
        (dev/changelog/831). The key must be ABSENT, not '': the route treats a present
        but empty name as a 400 the user cannot act on, since there is no field to fill."""
        body = self.evaluate('ccPayload({channelIds: [1], action: "queue"})')
        self.assertNotIn('name', body)  # short-needle-ok: body is the payload dict, a key check
        body = self.evaluate('ccPayload({name: "", channelIds: [1], action: "queue"})')
        self.assertNotIn('name', body)  # short-needle-ok: body is the payload dict, a key check

    def test_the_group_name_is_its_own_key_not_the_jobs_name(self):
        """The ad hoc create path names the GROUP it mints and the JOB from one string. A
        nameless caller creating a group has to be able to say which is which, or the
        group ends up called "<whatever> - health check"."""
        body = self.evaluate('ccPayload({channelIds: [1, 2], groupName: "Fox Sports 1", action: "queue"})')
        self.assertEqual(body['group_name'], 'Fox Sports 1')
        self.assertNotIn('name', body)  # short-needle-ok: body is the payload dict, a key check


class WindowRecommendLineTests(_Base):
    """ccWindowRecommendLine() - the maintenance-window line under a recurring check set to
    "Run at: Maintenance window", built from GET /api/channel-tests/window-plan's response
    shape. It recommends the window and says where its hours live; the capacity arithmetic
    it replaced stated three numbers unconditionally, which made a healthy window read like
    a problem (dev/changelog/831). Non-blocking (CLAUDE.md "enforcement lives server-side"):
    this only ever adds a sentence, never disables the create button."""

    _PLAN = ('{window_seconds: 14400, start_label: "2:00 AM", end_label: "6:00 AM", days: {'
             '0: {checks: [], total_seconds: 0}, '
             '3: {checks: [{id: 1, name: "A", seconds: 6000}, {id: 2, name: "B", seconds: 5400}], '
             '   total_seconds: 11400}}}')

    def test_under_capacity_says_only_the_recommendation(self):
        line = self.evaluate(f'ccWindowRecommendLine({self._PLAN}, 3, 2880, "/settings?q=x")')
        self.assertIn('Recommended.', line)
        self.assertIn('avoid overlapping tasks', line)
        self.assertNotIn('color:var(--warn)', line)
        self.assertNotIn('may not all finish', line)
        # The numbers are the exception, not the body of the line.
        self.assertNotIn('already booked', line)

    def test_the_settings_link_points_where_the_caller_said(self):
        line = self.evaluate(f'ccWindowRecommendLine({self._PLAN}, 3, 2880, "/settings?q=Maintenance+window")')
        self.assertIn('href="/settings?q=Maintenance+window"', line)
        self.assertIn('target="_blank"', line)

    def test_a_missing_settings_url_does_not_render_undefined(self):
        """A caller that forgot the option must not ship the word `undefined` into an href."""
        line = self.evaluate(f'ccWindowRecommendLine({self._PLAN}, 3, 2880)')
        self.assertIn('href="#"', line)
        self.assertNotIn('undefined', line)

    def test_over_capacity_is_the_one_number_that_is_news(self):
        line = self.evaluate(f'ccWindowRecommendLine({self._PLAN}, 3, 12000, "/s")')
        self.assertIn('color:var(--warn)', line)
        self.assertIn('may not all finish', line)
        self.assertIn('about 4 hours', line)          # the window
        self.assertIn('about 6 hours 30 minutes', line)  # what the day would hold

    def test_a_day_with_nothing_booked_is_judged_on_this_check_alone(self):
        """recurDay 5 has no entry in the plan at all - that is "nothing booked", not a
        crash, and it is under capacity."""
        line = self.evaluate(f'ccWindowRecommendLine({self._PLAN}, 5, 300, "/s")')
        self.assertIn('Recommended.', line)
        self.assertNotIn('may not all finish', line)


if __name__ == '__main__':
    unittest.main()
