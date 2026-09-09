"""Tier 0 - the client-side display-time helpers in static/js/util.js.

`displayTz`, `displayHour12`, `tzFormatter`, `fmtTimeTz`, `fmtDateTz`, `tzDayKey`,
`tzDayLabel` and `utcIsoToDate` are the one home for "render this instant in the user's
display timezone and clock format" (dev/changelog/654). Before they existed, eleven
templates injected the timezone and the 12h/24h flag into their own JS config globals and
about twenty-five sites re-typed the Intl option object - and the surface that never got
the plumbing at all, the Live dashboard's timeline, silently rendered the BROWSER's
timezone instead (dev/docs/BUGS.md 2026-08-14).

Same technique as tests/test_format_plan_js.py: the helpers are top-level functions in
util.js precisely so this can evaluate the file in node and call them directly. util.js
reads the two settings from base.html's <meta> tags, so the harness stubs a minimal
`document.querySelector` returning whichever pair a scenario wants - that stub IS the
thing under test as far as the plumbing is concerned.

Node is required. It ships an ICU build with full timezone data, which is what makes
America/New_York and Europe/London real here rather than aliases for UTC.
"""
import json
import os
import shutil
import subprocess
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
UTIL_JS = os.path.join(REPO, 'static', 'js', 'util.js')

_EXPORTS = ('displayTz, displayHour12, tzFormatter, fmtTimeTz, fmtDateTz, '
            'tzDayKey, tzDayLabel, utcIsoToDate')

# argv: [util.js path, tz meta content or '', hour12 meta content or '', expression]
# A '' content stands for "the tag is absent", which is what a page outside base.html
# would see and what the fallbacks exist for.
_HARNESS = f"""
const fs = require('fs');
const metas = {{ 'display-tz': process.argv[2], 'display-hour12': process.argv[3] }};
global.document = {{
  querySelector(sel) {{
    const m = /^meta\\[name="(.+)"\\]$/.exec(sel);
    if (!m) return null;
    const content = metas[m[1]];
    return content ? {{ content }} : null;
  }},
  addEventListener() {{}},
  createElement: () => ({{ style: {{}}, classList: {{ add() {{}}, remove() {{}} }} }}),
}};
// util.js installs a global fetch wrapper and a few delegated listeners at load. None of
// it is under test here, but all of it has to not throw for the file to evaluate.
// `location` is referenced only inside the wrapper's body, which nothing here calls, so
// an empty object is enough - and it deliberately holds no URL string, because
// tests/support/netguard.py refuses to spawn a child whose argv contains one.
global.window = {{ fetch: () => Promise.resolve(), addEventListener() {{}}, matchMedia: () => ({{ matches: false, addEventListener() {{}} }}) }};
global.location = {{}};
const src = fs.readFileSync(process.argv[1], 'utf8');
const api = new Function(src + '\\nreturn {{{_EXPORTS}}};')();
const {{{_EXPORTS}}} = api;
console.log(JSON.stringify(eval(process.argv[4])));
"""

ET = 'America/New_York'
LONDON = 'Europe/London'

# 2026-01-15 05:30 UTC = 00:30 ET the same day, and 05:30 in London. Chosen so the two
# zones disagree about both the clock AND (at midnight) the calendar day, which is what
# makes a wrong-timezone render visible instead of plausible.
WINTER = "new Date(Date.UTC(2026, 0, 15, 5, 30, 0))"
# 2026-07-04 23:45 UTC = 19:45 ET, still July 4th in New York but already the 5th in London.
SUMMER = "new Date(Date.UTC(2026, 6, 4, 23, 45, 0))"
# Midnight ET exactly - the value that used to render as '24:00' under older ICU with
# hour12:false, and the reason the helper is the one place that spelling lives.
MIDNIGHT_ET = "new Date(Date.UTC(2026, 0, 15, 5, 0, 0))"


# The node child runs in a timezone that matches NEITHER the display setting under test nor
# UTC, standing in for a browser whose machine disagrees with the app's setting - which is
# the only condition under which any of these bugs is visible.
#
# This is load-bearing, not decoration. This build machine is UTC, so with the child left on
# the ambient zone `new Date('2026-01-15T05:30:00')` (parsed as LOCAL) and the correct
# UTC reading are the same instant, and a helper that forgot to append the Z passes every
# assertion. Verified by mutation: dropping the Z from utcIsoToDate is caught here and was
# NOT caught before this was added. Auckland is +12/+13, so it is on the far side of the
# date line from America/New_York as well - a dropped timeZone option shows up as the wrong
# DAY, not just the wrong hour.
_CHILD_TZ = 'Pacific/Auckland'


@unittest.skipIf(shutil.which('node') is None, 'node not installed')
class _Base(unittest.TestCase):
    tz = ET
    hour12 = 'true'

    def evaluate(self, expr, tz=None, hour12=None):
        args = ['node', '-e', _HARNESS, UTIL_JS,
                self.tz if tz is None else tz,
                self.hour12 if hour12 is None else hour12,
                expr]
        env = dict(os.environ, TZ=_CHILD_TZ)
        proc = subprocess.run(args, capture_output=True, text=True, cwd=REPO,
                              timeout=60, env=env)
        if proc.returncode != 0:
            self.fail(f'node failed evaluating `{expr}`:\n{proc.stderr}')
        return json.loads(proc.stdout)

    def test_the_harness_itself_runs_outside_the_display_timezone(self):
        """Meta-assertion: if the child ever ran in UTC or in the display zone, most of the
        assertions below would pass against a helper that ignored the setting entirely."""
        ambient = self.evaluate('Intl.DateTimeFormat().resolvedOptions().timeZone')
        self.assertEqual(ambient, _CHILD_TZ)
        self.assertNotIn(ambient, (ET, LONDON, 'UTC'))


class DisplaySettingSourceTests(_Base):
    """The two settings come from the meta tags and nowhere else."""

    def test_timezone_comes_from_the_meta_tag(self):
        self.assertEqual(self.evaluate('displayTz()'), ET)
        self.assertEqual(self.evaluate('displayTz()', tz=LONDON), LONDON)

    def test_missing_timezone_tag_falls_back_to_utc(self):
        """A page outside base.html has no tag. UTC is the honest answer - it is what the
        app stores - rather than the browser's zone, which is a different user's setting."""
        self.assertEqual(self.evaluate('displayTz()', tz=''), 'UTC')

    def test_hour12_reads_the_flag_both_ways(self):
        self.assertIs(self.evaluate('displayHour12()', hour12='true'), True)
        self.assertIs(self.evaluate('displayHour12()', hour12='false'), False)

    def test_hour12_defaults_to_twelve_hour_when_absent_or_unrecognized(self):
        """Only the literal string 'false' means 24h, matching app/routes/settings.py's
        `!= '24h'`. The two Jinja spellings this replaced disagreed for any third value:
        one produced 12h, the other 24h, on different pages of the same app."""
        self.assertIs(self.evaluate('displayHour12()', hour12=''), True)
        self.assertIs(self.evaluate('displayHour12()', hour12='gibberish'), True)


class FmtTimeTzTests(_Base):
    def test_renders_in_the_display_timezone_not_utc(self):
        self.assertEqual(self.evaluate(f'fmtTimeTz({WINTER})'), '12:30 AM')
        self.assertEqual(self.evaluate(f'fmtTimeTz({WINTER})', tz=LONDON), '5:30 AM')

    def test_twenty_four_hour_setting_is_honored(self):
        self.assertEqual(self.evaluate(f'fmtTimeTz({WINTER})', hour12='false'), '00:30')
        self.assertEqual(self.evaluate(f'fmtTimeTz({SUMMER})', hour12='false'), '19:45')

    def test_midnight_is_zero_not_twenty_four_under_24h(self):
        """Older ICU rendered en-US + hour12:false as hourCycle h24, giving '24:00' for
        midnight. Asserted here because the shared helper is the one place a workaround
        would go if a browser ever regresses."""
        self.assertEqual(self.evaluate(f'fmtTimeTz({MIDNIGHT_ET})', hour12='false'), '00:00')
        self.assertEqual(self.evaluate(f'fmtTimeTz({MIDNIGHT_ET})', hour12='true'), '12:00 AM')

    def test_seconds_option_adds_seconds(self):
        self.assertEqual(self.evaluate(f'fmtTimeTz({WINTER}, {{seconds: true}})'), '12:30:00 AM')
        self.assertEqual(
            self.evaluate(f'fmtTimeTz({WINTER}, {{seconds: true}})', hour12='false'), '00:30:00')

    def test_dst_offset_is_applied_not_a_fixed_one(self):
        """July is EDT (UTC-4), January is EST (UTC-5). A hardcoded offset would get one
        of these wrong; Intl with a real zone name gets both."""
        self.assertEqual(self.evaluate(f'fmtTimeTz({SUMMER})'), '7:45 PM')
        self.assertEqual(self.evaluate(f'fmtTimeTz({WINTER})'), '12:30 AM')


class FmtDateTzTests(_Base):
    def test_default_shape_is_the_long_day_form(self):
        self.assertEqual(self.evaluate(f'fmtDateTz({WINTER})'), 'Thursday, Jan 15')

    def test_day_follows_the_display_timezone_across_the_date_line(self):
        """23:45 UTC on July 4th is still the 4th in New York and already the 5th in
        London. This is the failure a wrong timezone produces that a wrong clock does
        not: the wrong DAY."""
        self.assertEqual(self.evaluate(f'fmtDateTz({SUMMER})'), 'Saturday, Jul 4')
        self.assertEqual(self.evaluate(f'fmtDateTz({SUMMER})', tz=LONDON), 'Sunday, Jul 5')

    def test_custom_options_pass_through(self):
        opts = "{weekday: 'short', month: 'short', day: 'numeric'}"
        self.assertEqual(self.evaluate(f'fmtDateTz({WINTER}, {opts})'), 'Thu, Jan 15')

    def test_combined_date_and_time_picks_up_the_clock_setting(self):
        """The accounts page's 'last synced' shape. hour12 is filled in from the setting
        because the caller asked for an hour without pinning it - an ad-hoc option object
        is exactly where that used to get forgotten."""
        opts = "{month: 'short', day: 'numeric', hour: 'numeric', minute: '2-digit'}"
        self.assertEqual(self.evaluate(f'fmtDateTz({SUMMER}, {opts})'), 'Jul 4, 7:45 PM')
        self.assertEqual(
            self.evaluate(f'fmtDateTz({SUMMER}, {opts})', hour12='false'), 'Jul 4, 19:45')

    def test_locale_is_en_us_regardless_of_the_host(self):
        """Month and weekday names are English everywhere. The app's own strings are
        English by construction - 'Today'/'Tomorrow' render right beside these - so a
        browser-locale month name produces half-translated output. channel-search.js was
        the one site that did that before this consolidated them."""
        self.assertEqual(self.evaluate(f'fmtDateTz({WINTER})'), 'Thursday, Jan 15')


class TzDayKeyTests(_Base):
    def test_key_is_iso_ordered_and_string_sortable(self):
        self.assertEqual(self.evaluate(f'tzDayKey({WINTER})'), '2026-01-15')

    def test_key_follows_the_display_timezone(self):
        self.assertEqual(self.evaluate(f'tzDayKey({SUMMER})'), '2026-07-04')
        self.assertEqual(self.evaluate(f'tzDayKey({SUMMER})', tz=LONDON), '2026-07-05')

    def test_key_is_unaffected_by_the_clock_setting(self):
        """A day key is a calendar fact. If hour12 leaked into it, the 12h and 24h halves
        of the app would bucket airings into different days."""
        self.assertEqual(self.evaluate(f'tzDayKey({WINTER})', hour12='false'), '2026-01-15')


class TzDayLabelTests(_Base):
    def test_today_tomorrow_yesterday_are_relative_to_now(self):
        self.assertEqual(self.evaluate('tzDayLabel(new Date())'), 'Today')
        self.assertEqual(
            self.evaluate('tzDayLabel(new Date(Date.now() + 86400000))'), 'Tomorrow')
        self.assertEqual(
            self.evaluate('tzDayLabel(new Date(Date.now() - 86400000))'), 'Yesterday')

    def test_a_distant_day_gets_the_short_date(self):
        self.assertEqual(self.evaluate(f'tzDayLabel({WINTER})'), 'Thu, Jan 15')

    def test_null_is_empty_not_a_crash(self):
        self.assertEqual(self.evaluate('tzDayLabel(null)'), '')

    def test_the_relative_window_follows_the_display_timezone(self):
        """Two zones can disagree about which calendar day 'now' is, so the label has to
        bucket against the display timezone rather than the browser's."""
        got = self.evaluate('tzDayLabel(new Date())', tz=LONDON)
        self.assertEqual(got, 'Today')


class UtcIsoToDateTests(_Base):
    def test_naive_string_is_read_as_utc_not_browser_local(self):
        """The API sends naive UTC. JavaScript reads an unsuffixed string as LOCAL, so
        without the appended Z every timestamp in the app would be off by the browser's
        offset - the single most-repeated line in the code this replaced."""
        self.assertEqual(
            self.evaluate("utcIsoToDate('2026-01-15T05:30:00').toISOString()"),
            '2026-01-15T05:30:00.000Z')

    def test_string_that_already_carries_an_offset_is_left_alone(self):
        self.assertEqual(
            self.evaluate("utcIsoToDate('2026-01-15T05:30:00Z').toISOString()"),
            '2026-01-15T05:30:00.000Z')
        self.assertEqual(
            self.evaluate("utcIsoToDate('2026-01-15T00:30:00-05:00').toISOString()"),
            '2026-01-15T05:30:00.000Z')

    def test_empty_and_unparseable_give_null_not_an_invalid_date(self):
        """An Invalid Date formats as the string 'Invalid Date' and renders straight into
        the page; null lets the caller decide what to show instead."""
        self.assertIsNone(self.evaluate("utcIsoToDate('')"))
        self.assertIsNone(self.evaluate('utcIsoToDate(null)'))
        self.assertIsNone(self.evaluate("utcIsoToDate('not a date')"))


class FormatterCacheTests(_Base):
    def test_same_options_return_the_identical_formatter_object(self):
        """The cache is load-bearing, not an optimization: channel-search.js formats a
        time and a day label for every row of a result page over 136,130 channels, and
        every hand-rolled site this replaced hoisted its formatter to module scope for
        that reason. A per-call construction would put Intl setup in a per-row loop."""
        expr = ("(() => { const a = tzFormatter({hour: 'numeric'}); "
                "const b = tzFormatter({hour: 'numeric'}); return a === b; })()")
        self.assertIs(self.evaluate(expr), True)

    def test_different_options_return_different_formatters(self):
        expr = ("(() => { const a = tzFormatter({hour: 'numeric'}); "
                "const b = tzFormatter({weekday: 'long'}); return a === b; })()")
        self.assertIs(self.evaluate(expr), False)

    def test_repeated_formatting_is_stable(self):
        """Guards against a cache keyed on something mutable: the same instant must format
        identically on the thousandth row as on the first."""
        expr = (f"(() => {{ const d = {WINTER}; const first = fmtTimeTz(d); "
                "for (let i = 0; i < 1000; i++) fmtTimeTz(d); "
                "return first === fmtTimeTz(d); })()")
        self.assertIs(self.evaluate(expr), True)


if __name__ == '__main__':
    unittest.main()
