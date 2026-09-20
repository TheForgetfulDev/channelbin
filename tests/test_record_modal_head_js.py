"""Tier 0 - the record modal's program header (static/js/guide.js::progHeadHtml,
templates/_record_modal.html, dev/changelog/1050).

Clicking a program in the TV Guide opened the scheduling modal, and the only thing that
modal said about the program was `prog.description.slice(0, 120)` in the grey hint line
under the Recording Name box - cut off mid-sentence, with no subtitle, airtime, length or
tags anywhere. Phones got a different flow entirely: a bottom sheet that carried all of
that, and a button to reach the same modal. The header moved that block into the modal, and
the sheet was deleted so a tap goes straight there on every width.

What it covers: that the header renders the WHOLE description rather than a slice, that it
carries the title/subtitle/When/Day/Channel/tags the deleted sheet carried, that it renders
nothing at all for a target with no program behind it (the dashboard's and the recording
detail page's "edit a scheduled recording" callers, and a dummy filler slot), that the
modal is still what opens at phone width, and that the channel-group disclosure is the one
shortened sentence while still naming the member it would record from.

Python cannot reach any of it: openModal() is handed a plain object and writes innerHTML,
so this runs the shipped static/js/util.js + static/js/guide.js against the markup the real
/guide route rendered, the same arrangement tests/test_guide_modal_padding_js.py uses.
tests/support/record_modal_head.mjs drives it and reports observations; every assertion
lives here.

What it cannot cover: jsdom computes no layout, so this says nothing about how the header
looks - only about what text and elements it contains. The grid cell click itself is not
driven either (it needs a rendered grid); `ProgramSheetIsGoneTests` below covers that half
by reading the shipped file, which is what makes the phone path structural rather than
conditional.

Runs against a throwaway temp SQLite DB - never the live one.
  python3 -m unittest tests.test_record_modal_head_js
"""
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tests.support.app import make_test_app  # noqa: E402
from tests.support import seed  # noqa: E402

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HARNESS = os.path.join(REPO, 'tests', 'support', 'record_modal_head.mjs')
JSDOM = os.path.join(REPO, 'node_modules', 'jsdom')
GUIDE_JS = os.path.join(REPO, 'static', 'js', 'guide.js')

# The description the harness feeds in, verbatim - the assertions compare against it whole.
LONG_DESC = (
    'A documentary crew follows three lighthouse keepers through a winter on the north '
    'Atlantic coast, from the first storm of the season to the day the supply boat finally '
    'reaches them again.')

_RESULT = None


def _observe():
    """Boot the /guide page once in node and return every scenario's observations."""
    global _RESULT
    if _RESULT is not None:
        return _RESULT
    t = make_test_app()
    tmp = tempfile.mkdtemp(prefix='record_modal_head_js_')
    try:
        with t.app.app_context():
            acc = seed.make_account()
            seed.make_channel(acc, name='Documentary HD', in_guide=True)
            page = t.client.get('/guide').get_data(as_text=True)
        with open(os.path.join(tmp, 'page.html'), 'w', encoding='utf-8') as f:
            f.write(page)
        proc = subprocess.run(
            ['node', HARNESS, tmp, REPO],
            capture_output=True, text=True, timeout=120, cwd=REPO)
        if proc.returncode != 0:
            raise AssertionError(f'harness failed:\n{proc.stderr[-4000:]}')
        _RESULT = json.loads(proc.stdout)
        return _RESULT
    finally:
        t.cleanup()
        shutil.rmtree(tmp, ignore_errors=True)


@unittest.skipIf(shutil.which('node') is None, 'node not installed')
@unittest.skipIf(not os.path.isdir(JSDOM), 'jsdom not installed (npm install)')
class _Base(unittest.TestCase):
    SCENARIO = ''

    @classmethod
    def setUpClass(cls):
        if not cls.SCENARIO:
            raise unittest.SkipTest('base class - carries no cases')
        cls.obs = _observe()[cls.SCENARIO]
        if isinstance(cls.obs, dict) and 'error' in cls.obs:
            raise AssertionError(f'{cls.SCENARIO} threw in the page:\n{cls.obs["error"]}')

    def test_the_page_booted_without_errors(self):
        self.assertEqual(self.obs['errors'], [])


class FullProgramHeaderTests(_Base):
    """A real EPG program: everything the deleted sheet showed, in the modal."""
    SCENARIO = 'full_program'

    def test_the_header_is_shown(self):
        self.assertTrue(self.obs['modalOpen'])
        self.assertTrue(self.obs['headVisible'])

    def test_the_whole_description_is_shown(self):
        """The defect this replaced: the old hint line was `description.slice(0, 120)`, so
        the sentence stopped mid-word with nothing saying it had been cut."""
        self.assertEqual(self.obs['desc'], LONG_DESC)
        self.assertGreater(len(LONG_DESC), 120, 'fixture no longer exercises the truncation')

    def test_title_and_subtitle_are_shown(self):
        self.assertEqual(self.obs['title'], 'The Long Watch')
        self.assertEqual(self.obs['sub'], 'Part Two: The Supply Boat')

    def test_when_day_and_channel_lines_are_shown(self):
        self.assertEqual(self.obs['labels'], ['When', 'Day', 'Channel'])

    def test_the_when_line_carries_the_length(self):
        """The program's own length is nowhere else in the modal: the Start/Stop fields
        hold the PADDED times a profile computed, not the program's own."""
        when = self.obs['values'][0]
        self.assertIn('1h 30m', when)
        # 20:00-21:30 UTC in America/New_York (UTC-4 in August).
        self.assertIn('4:00', when)
        self.assertIn('5:30', when)

    def test_the_day_line_reads_in_the_display_timezone(self):
        self.assertEqual(self.obs['values'][1], 'Monday, Aug 10')

    def test_the_channel_line_names_the_channel(self):
        self.assertEqual(self.obs['values'][2], 'Documentary HD')

    def test_matched_tags_are_shown(self):
        self.assertEqual(self.obs['tags'], ['Documentary', 'Nature'])

    def test_tag_swatches_use_the_shared_dot(self):
        """.tag-badge-dot lives in guide.css, and this modal also renders on the channel
        search page, which loads no guide.css - the swatch would be invisible there."""
        self.assertIn('color-dot', self.obs['html'])
        self.assertNotIn('tag-badge-dot', self.obs['html'])

    def test_the_truncated_hint_line_is_gone(self):
        self.assertFalse(self.obs['nameHintExists'],
                         '#modal-name-hint is what the header replaced')


class BareProgramHeaderTests(_Base):
    """A program the provider gave nothing but a title and a time. Every absent field
    renders NOTHING rather than an empty element - an empty `.info-desc` is a blank gap
    the user cannot explain."""
    SCENARIO = 'bare_program'

    def test_the_header_is_still_shown(self):
        self.assertTrue(self.obs['headVisible'])
        self.assertEqual(self.obs['title'], 'The Long Watch')

    def test_no_empty_subtitle_description_or_tags(self):
        self.assertEqual(self.obs['sub'], '')
        self.assertEqual(self.obs['desc'], '')
        self.assertEqual(self.obs['tags'], [])

    def test_the_lines_still_render(self):
        self.assertEqual(self.obs['labels'], ['When', 'Day', 'Channel'])


class NoProgramHeaderTests(_Base):
    """The dashboard's and the recording detail page's "edit a scheduled recording"
    callers pass a recording, not a program - there is nothing to describe, and an empty
    box above the form is worse than no box."""
    SCENARIO = 'no_program'

    def test_the_modal_still_opens(self):
        self.assertTrue(self.obs['modalOpen'])
        self.assertEqual(self.obs['nameValue'], 'Saved Recording')

    def test_the_header_is_hidden_and_empty(self):
        self.assertFalse(self.obs['headVisible'])
        self.assertEqual(self.obs['html'], '')


class DummySlotHeaderTests(_Base):
    """A filler slot on a channel with no EPG data. Scheduling off it is allowed - that
    is how you record a channel the provider publishes no listings for - but there is no
    program to describe."""
    SCENARIO = 'dummy_slot'

    def test_the_modal_opens_with_no_header(self):
        self.assertTrue(self.obs['modalOpen'])
        self.assertFalse(self.obs['headVisible'])


class MobileProgramTests(_Base):
    """Phone width goes straight to the record modal, like desktop - the program sheet
    that used to stand in front of it is gone, and the details it carried are here."""
    SCENARIO = 'mobile_program'

    def test_the_phone_width_path_was_taken(self):
        self.assertTrue(self.obs['isMobile'], 'the harness did not boot at phone width')

    def test_the_record_modal_is_what_opens(self):
        self.assertTrue(self.obs['modalOpen'])
        self.assertEqual(self.obs['sheetPanels'], 0, 'a bottom sheet was built')

    def test_the_description_reaches_the_phone(self):
        """13.9 forbids a description in a phone-width grid cell, so with the sheet gone
        this modal is the ONLY place a phone can read one."""
        self.assertTrue(self.obs['headVisible'])
        self.assertEqual(self.obs['desc'], LONG_DESC)


class GroupNoteTests(_Base):
    """The channel-group disclosure, shortened to one sentence (dev/changelog/1050) while
    still naming the member it would record from (dev/changelog/904)."""
    SCENARIO = 'group_note'

    def test_the_note_is_shown(self):
        self.assertTrue(self.obs['visible'])

    def test_it_names_the_group_and_the_serving_member(self):
        self.assertIn('Movie Channels', self.obs['text'])
        self.assertIn('Cinema One FHD', self.obs['text'])

    def test_it_says_the_choice_is_made_at_record_time(self):
        self.assertIn('picked just before the recording starts', self.obs['text'])

    def test_it_is_one_sentence_and_drops_the_account(self):
        self.assertEqual(self.obs['text'].count('. '), 0)
        self.assertTrue(self.obs['text'].endswith('.'))
        self.assertNotIn('Provider A', self.obs['text'])


class ProgramSheetIsGoneTests(unittest.TestCase):
    """The other half of "mobile goes straight to the record modal": it is structural, not
    conditional. The sheet's three functions are deleted, so there is no branch left that
    could route a program tap anywhere else - which is why the jsdom half above does not
    need to drive a grid cell click to prove it.

    Reads the shipped file rather than the DOM because what is being asserted is an
    absence, and an absence has no element to query."""

    @classmethod
    def setUpClass(cls):
        with open(GUIDE_JS, encoding='utf-8') as f:
            cls.src = f.read()

    def test_the_sheet_functions_are_deleted(self):
        for name in ('openProgramSheet', 'programSheetActions', 'openProgramTarget'):
            self.assertNotIn(name, self.src, f'{name} survived the sheet removal')

    def test_every_program_cell_opens_the_record_modal(self):
        """Two render paths build program cells - the normal grid and the collapsed-gap
        one - and they have to agree. Either one left on a sheet would be a phone-only
        divergence nothing else in the suite can see."""
        handlers = re.findall(r"el\.addEventListener\('click', \(\) => (\w+)\(prog, ch\)\);",
                              self.src)
        self.assertEqual(handlers, ['openModal', 'openModal'])


if __name__ == '__main__':
    unittest.main()
