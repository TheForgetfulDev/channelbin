"""Both profile list pages against the design that produced them (dev/changelog/864).

/profiles and /health-check-profiles used to be nine-column tables whose cells mostly read
"Default (N)" - 731px and 677px wider than a portrait phone respectively, the two worst
horizontal-scroll offenders in the app (dev/changelog/859). They are now the card-row list
DESIGN.md 3.1/9.4 describes, under their own .prof-head/.prow class names, and a row
carries only what the profile OVERRIDES.

Each case here is a decision a careless edit would quietly undo:

  * The row is the hybrid card list (3.1), not a `.tbl` in a `.table-scroll`. Restoring the
    table restores the sideways scroll, and jsdom cannot see layout, so the class names are
    the only thing a test can hold onto.
  * An inherited setting is ABSENT from the row. That is the whole redesign - a row that
    lists inheritance is the page this replaced.
  * A stored 0 or False is a value the user chose and must still render. This is where
    app/profile_forms.py's blank-is-not-zero rule becomes visible, and truthiness in a
    Jinja conditional is the exact way it gets collapsed: retention_days=0 ("never delete,
    even if a global window is set") and screenshots_enabled=False both have to survive.
  * The globals appear ONCE, under the list, and are rendered through the same functions as
    an override cell - a fallback spelled a second time in a template is how the list, the
    line and the modal drift apart.
  * Add and Edit stay one modal over a JSON API (3.12) - the redesign is the list only.

Runs against a throwaway temp SQLite DB - never the live dvr.db.
  python3 -m unittest tests.test_profiles_page_conformance
"""
import os
import re
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tests.support.app import make_test_app  # noqa: E402
from app import db  # noqa: E402
from app.database import HealthCheckProfile, RecordingProfile  # noqa: E402

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# Everything the page renders for its own list, with base.html's shell excluded - the nav
# and footer carry markup of their own and a whole-document scan answers about the shell.
_PAGE_START = '<div class="page-head">'
_PAGE_END = '<footer class="app-footer">'


def _page(html):
    return html.split(_PAGE_START)[1].split(_PAGE_END)[0]


def _rows(html):
    """Just the .prow elements - the defaults line under the list uses .p-ov too, so a
    whole-page search for an override pair would pass on the globals alone.

    Raises rather than returning [] when the list markup is missing: every caller here
    reads a row, so an empty result is the page being wrong, and silently returning it
    turns "the rows are gone" into a vacuous pass on any case that loops over them.
    """
    body = _page(html)
    if '<div class="prof-rows">' not in body:
        raise AssertionError('the page rendered no .prof-rows list at all')
    rows = body.split('<div class="prof-rows">')[1].split('</div>\n</div>')[0]
    found = re.findall(r'<div class="prow">.*?(?=<div class="prow">|$)', rows, re.S)
    if not found:
        raise AssertionError('.prof-rows is present but contains no .prow rows')
    return found


def _defaults_line(html):
    body = _page(html)
    return body.split('<p class="prof-defaults">')[1].split('</p>')[0]


def _pairs(fragment):
    """[(label, value)] for every .p-ov override pill in the fragment."""
    return [(m.group(1).strip(), m.group(2).strip()) for m in re.finditer(
        r'<span class="p-ov"><span class="p-ok">(.*?)</span>(.*?)</span>', fragment, re.S)]


class _Base(unittest.TestCase):
    URL = None

    def setUp(self):
        self.t = make_test_app()
        self.client = self.t.client
        self.ctx = self.t.app.app_context()
        self.ctx.push()

    def tearDown(self):
        self.ctx.pop()
        self.t.cleanup()

    def get(self):
        resp = self.client.get(self.URL)
        self.assertEqual(resp.status_code, 200)
        return resp.get_data(as_text=True)


class _SharedListShapeTests:
    """Run against both pages - they are one design and one set of class names."""

    def test_list_is_the_card_row_not_a_scrolling_table(self):
        self.make(name='Shape')
        body = _page(self.get())
        self.assertIn('class="prof-head"', body)
        self.assertIn('class="prof-rows"', body)
        self.assertIn('<div class="prow">', body)
        # The table this replaced. `.tbl` inside `.table-scroll` is what made the page
        # 677-731px wider than a phone; bringing either back brings the drag back.
        self.assertNotIn('<table class="tbl">', body)
        self.assertNotIn('table-scroll', body)

    def test_inherited_settings_are_absent_from_the_row(self):
        """The entire point of the redesign. A profile that overrides one thing shows one
        pair, not that pair plus five statements of what it did not change."""
        self.make(name='Minimal', **self.ONE_OVERRIDE)
        row = _rows(self.get())[0]
        self.assertEqual(_pairs(row), [self.ONE_OVERRIDE_PAIR])
        self.assertNotIn('Default (', row)

    def test_a_profile_that_overrides_nothing_says_so(self):
        """An empty cell reads as a failure to render, not as "inherits everything"."""
        self.make(name='Plain')
        row = _rows(self.get())[0]
        self.assertEqual(_pairs(row), [])
        self.assertIn('Inherits every', row)

    def test_globals_are_stated_once_under_the_list_not_per_row(self):
        self.make(name='A')
        self.make(name='B')
        body = self.get()
        line = _defaults_line(body)
        self.assertTrue(_pairs(line), 'the defaults line lists no globals at all')
        # Every "Default (N)" cell the retired table repeated per row is gone from the
        # rows; the fallbacks live in this one line instead. Row count asserted so a
        # regression that drops the rows cannot pass this by looping over nothing.
        rows = _rows(body)
        self.assertEqual(len(rows), 2)
        for row in rows:
            self.assertEqual(_pairs(row), [])

    def test_add_and_edit_are_still_one_modal_over_the_json_api(self):
        """The redesign is the list only - reopening the form is out of scope, and a
        standalone form page would be a second way to write the row."""
        body = self.get()
        self.assertIn('profile-modal.js', body)
        self.assertIn(self.CONFIG_GLOBAL, body)
        for path in self.RETIRED_FORM_PATHS:
            self.assertEqual(self.client.get(path).status_code, 404)


class RecordingProfilesTests(_SharedListShapeTests, _Base):
    URL = '/profiles'
    CONFIG_GLOBAL = 'RP_CONFIG'
    RETIRED_FORM_PATHS = ('/profiles/new', '/profiles/1/edit')
    ONE_OVERRIDE = {'stall_timeout_seconds': 45}
    ONE_OVERRIDE_PAIR = ('Stall timeout', '45s')

    def make(self, **kwargs):
        p = RecordingProfile(**kwargs)
        db.session.add(p)
        db.session.commit()
        return p

    def test_zero_retention_renders_as_never_in_the_ROW(self):
        """0 means "never auto-delete even if a global window is set" - the opposite of
        unset. `if p.retention_days` in a template collapses the two and hides the setting
        that matters most. Asserted on the row, because the defaults line also renders
        Never whenever there is no global window and would pass this on its own."""
        self.make(name='Keep', retention_days=0)
        self.assertIn(('Auto-delete', 'Never'), _pairs(_rows(self.get())[0]))

    def test_pre_check_off_is_an_override_and_shows(self):
        """False is a value; only None is inheritance. The retired table had no column for
        this field at all, so a profile that turned the pre-recording check off said so
        nowhere on the page."""
        self.make(name='No check', pre_check_enabled=False)
        self.assertIn(('Pre-recording check', 'Off'), _pairs(_rows(self.get())[0]))

    def test_padding_shows_only_when_non_zero_and_reads_as_one_pair(self):
        """The padding columns are NOT NULL with a 0 default and inherit nothing, so 0 is
        the baseline rather than an override - listing it on every row is the noise this
        redesign removed. Pre and post are read together, so they render as one pair."""
        self.make(name='Padded', pre_padding_minutes=0, post_padding_minutes=60)
        self.assertIn(('Padding', '0m / 60m'), _pairs(_rows(self.get())[0]))

        self.make(name='Unpadded', pre_padding_minutes=0, post_padding_minutes=0)
        rows = {r for r in _rows(self.get()) if 'Unpadded' in r}
        self.assertEqual(len(rows), 1)
        self.assertEqual(_pairs(rows.pop()), [])

    def test_filename_template_is_shown_only_when_the_profile_sets_one(self):
        """It is the one override too long for a pill, so it gets the meta line - and the
        empty cell beside it has to say "every other default", not "every default"."""
        self.make(name='Templated', filename_template='{title}-{date}')
        row = _rows(self.get())[0]
        self.assertIn('class="p-meta"', row)
        self.assertIn('{title}-{date}', row)
        self.assertIn('Inherits every other default', row)

        self.make(name='Bare')
        bare = [r for r in _rows(self.get()) if 'Bare' in r][0]
        self.assertNotIn('class="p-meta"', bare)
        self.assertIn('Inherits every default', bare)

    def test_usage_count_keeps_its_exact_middot_spelling(self):
        """tests/test_recording_stats.py asserts on this literal string as the marker for
        the per-profile usage count, so the redesign must not respell it."""
        self.make(name='Used')
        self.assertRegex(_rows(self.get())[0], r'0 recordings · 0 channels')


class HealthCheckProfilesTests(_SharedListShapeTests, _Base):
    URL = '/health-check-profiles'
    CONFIG_GLOBAL = 'HCP_CONFIG'
    RETIRED_FORM_PATHS = ('/health-check-profiles/new', '/health-check-profiles/1/edit')
    ONE_OVERRIDE = {'test_duration_seconds': 5}
    ONE_OVERRIDE_PAIR = ('Test duration', '5s')

    def make(self, **kwargs):
        p = HealthCheckProfile(**kwargs)
        db.session.add(p)
        db.session.commit()
        return p

    def test_screenshots_off_is_an_override_and_shows(self):
        """False is a value the user chose; only None inherits. Truthiness here would show
        a profile that disabled screenshots as one that inherits them."""
        self.make(name='No shots', screenshots_enabled=False)
        self.assertIn(('Screenshots', 'Off'), _pairs(_rows(self.get())[0]))

    def test_zero_retries_is_an_override_and_shows(self):
        """0 connect retries means "try once and give up" - a real setting, not a blank."""
        self.make(name='One shot', connect_retries=0)
        self.assertIn(('Connect retries', '0'), _pairs(_rows(self.get())[0]))


class DefaultsLineFidelityTests(_Base):
    """The globals line and an override cell must render one setting the same way. Two
    spellings of one fallback on one page is the drift the route's shared render functions
    exist to prevent."""
    URL = '/profiles'

    def test_a_global_and_an_override_of_the_same_field_read_alike(self):
        p = RecordingProfile(name='Explicit', stall_timeout_seconds=99)
        db.session.add(p)
        db.session.commit()
        body = self.get()
        row_labels = dict(_pairs(_rows(body)[0]))
        default_labels = dict(_pairs(_defaults_line(body)))
        self.assertEqual(row_labels['Stall timeout'], '99s')
        # Same label, and a value in the same units - not "10" beside "45s".
        self.assertRegex(default_labels['Stall timeout'], r'^\d+s$')


class NoTemplateRespellsAFallbackTests(unittest.TestCase):
    """Both templates carry a comment saying never to re-spell a fallback, because the
    modal reads the same numbers. A "Default (" literal back in either file means a cell
    is once again writing what a profile inherits, per row."""

    def test_neither_template_writes_a_default_cell(self):
        for name in ('profiles.html', 'health_check_profiles.html'):
            with open(os.path.join(REPO, 'templates', name), encoding='utf-8') as fh:
                source = fh.read()
            # Jinja comments are stripped first: both files describe the retired
            # "Default (N)" table in their header comment, which is the record of why the
            # rule exists rather than a violation of it.
            markup = re.sub(r'\{#.*?#\}', '', source, flags=re.S)
            self.assertNotIn('Default (', markup, f'{name} re-spells a fallback')


if __name__ == '__main__':
    unittest.main()
