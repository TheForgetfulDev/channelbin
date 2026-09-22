"""Tier 0 - the TV Guide's recording chip names a status with the server's word
(static/js/guide.js::recStatusBadge, dev/docs/BUGS.md 2026-09-22).

guide.js used to spell its own status -> label table. CONCATENATING and ANALYZING both
rendered "✓ Recorded" while CONVERTING - the phase that runs after both of them - rendered
"✓ Converting", so the guide told the user a program was recorded during the two phases that
can still fail, and then changed its mind for the one that follows them. The Recordings list
said JOINING / ANALYZING / CONVERTING for the same three rows.

The word now comes from app/fmt_utils.py's one vocabulary, served to every page by
base.html's `rec-status-labels` meta tag and read by static/js/util.js::recStatusLabel. That
whole path is browser-side - Python can assert the meta tag exists but not what guide.js
does with it - so this runs the shipped util.js + guide.js against the markup the real
/guide route rendered, the same arrangement tests/test_guide_modal_padding_js.py uses.
tests/support/guide_rec_chip.mjs drives it and reports observations; every assertion is here.

What it covers: that every status the server names is drawn with that server word (one
documented exception, COMPLETED, which the guide calls "Recorded" on purpose), that no two
statuses share a chip word, that the chip's tooltip is headed by its own word rather than a
separately-typed one, that the two statuses the EPG route never sends draw nothing, and that
a status the guide has no entry for draws nothing and complains rather than borrowing a
neighbour's colour and word.

What it cannot cover: jsdom computes no layout, so this says nothing about how a chip looks
on the grid - only about its text, tooltip and classes.

Runs against a throwaway temp SQLite DB - never the live one.
  python3 -m unittest tests.test_guide_rec_chip_js
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
from app.fmt_utils import REC_STATUS_DISPLAY, REC_STATUS_LABELS  # noqa: E402
from app.database import (  # noqa: E402
    REC_STATUS_CONCATENATING, REC_STATUS_ANALYZING, REC_STATUS_CONVERTING,
    REC_STATUS_COMPLETED, REC_STATUS_FAILED, REC_STATUS_ABORTED,
)

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HARNESS = os.path.join(REPO, 'tests', 'support', 'guide_rec_chip.mjs')
JSDOM = os.path.join(REPO, 'node_modules', 'jsdom')

# The guide draws no chip for these: app/routes/guide.py excludes them from the recordings
# matched to guide cells, so a chip would have no data behind it.
NO_CHIP = (REC_STATUS_FAILED, REC_STATUS_ABORTED)

_RESULT = None


def _observe():
    """Boot the /guide page once in node and return what it drew for each status."""
    global _RESULT
    if _RESULT is not None:
        return _RESULT
    t = make_test_app()
    tmp = tempfile.mkdtemp(prefix='guide_rec_chip_js_')
    try:
        with t.app.app_context():
            acc = seed.make_account()
            seed.make_channel(acc, name='Test Channel', in_guide=True)
            page = t.client.get('/guide').get_data(as_text=True)
        with open(os.path.join(tmp, 'page.html'), 'w', encoding='utf-8') as f:
            f.write(page)
        # The status list crosses into node as data, never re-typed there: a status added to
        # app/fmt_utils.py has to reach this test on its own or the coverage is a fiction.
        with open(os.path.join(tmp, 'statuses.json'), 'w', encoding='utf-8') as f:
            json.dump(sorted(REC_STATUS_DISPLAY), f)
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
    @classmethod
    def setUpClass(cls):
        cls.obs = _observe()

    def chip(self, status):
        return self.obs['chips'][status]


class ChipVocabularyTests(_Base):
    def test_the_page_booted_without_errors(self):
        self.assertEqual(self.obs['errors'], [])

    def test_the_browser_received_the_servers_own_table(self):
        """base.html carries app/fmt_utils.py's vocabulary into every page. If this is
        empty, guide.js has no word to use and every assertion below is vacuous."""
        self.assertEqual(REC_STATUS_LABELS, self.obs['vocab']['labels'])

    def test_every_post_capture_phase_is_named_its_own_phase(self):
        """The defect itself: a join is not a finished recording, and neither is an
        analysis. Each of the three post-capture phases gets the word the Recordings list
        uses for it, not the word for the state after them."""
        self.assertEqual('✓ Joining', self.chip(REC_STATUS_CONCATENATING)['text'])
        self.assertEqual('✓ Analyzing', self.chip(REC_STATUS_ANALYZING)['text'])
        self.assertEqual('✓ Converting', self.chip(REC_STATUS_CONVERTING)['text'])

    def test_every_chip_word_is_the_servers_word_for_that_status(self):
        """The general form of the rule, so a status added later cannot drift. The guide
        writes chips in sentence case where the Recordings list badges in upper case, so the
        comparison is case-insensitive - the WORD has to match, not its casing."""
        for status in REC_STATUS_DISPLAY:
            if status in NO_CHIP or status == REC_STATUS_COMPLETED:
                continue
            with self.subTest(status=status):
                text = self.chip(status)['text']
                self.assertIsNotNone(text, f'{status} draws no chip at all')
                # The chip may lead with a glyph; the word is what follows it.
                word = text.split(' ')[-1]
                self.assertEqual(REC_STATUS_LABELS[status].lower(), word.lower())

    def test_completed_is_the_one_deliberate_difference(self):
        """The guide calls a finished recording "Recorded", not "Completed". Whether that
        is the right word is a naming call nobody has made - this pins the exception so it
        stays the only one rather than becoming a second table again."""
        self.assertEqual('✓ Recorded', self.chip(REC_STATUS_COMPLETED)['text'])

    def test_no_two_statuses_share_a_chip_word(self):
        """What the user actually saw: two different states wearing one label, with no way
        to tell from the guide which of them a program was in."""
        words = {}
        for status in REC_STATUS_DISPLAY:
            if status in NO_CHIP:
                continue
            words.setdefault(self.chip(status)['text'], []).append(status)
        collisions = {w: s for w, s in words.items() if len(s) > 1}
        self.assertEqual({}, collisions, f'statuses sharing one chip word: {collisions}')

    def test_each_tooltip_is_headed_by_its_own_chips_word(self):
        """The heading used to be typed separately from the chip, which is how "Recorded"
        came to sit over a sentence describing a join in progress."""
        for status in REC_STATUS_DISPLAY:
            if status in NO_CHIP:
                continue
            with self.subTest(status=status):
                chip = self.chip(status)
                # The newline travels as &#10; in the attribute; reading it back through the
                # DOM decodes it, so accept either spelling rather than pin the encoding.
                head = re.split(r'&#10;|\n', chip['tip'])[0]
                self.assertEqual(chip['text'].split(' ')[-1], head)


class NoChipStatusTests(_Base):
    def test_the_statuses_the_guide_never_receives_draw_nothing(self):
        """app/routes/guide.py excludes FAILED and ABORTED from the recordings matched to
        cells. They are in the guide's table as an explicit "no chip" rather than absent, so
        they draw nothing quietly instead of tripping the unknown-status complaint."""
        for status in NO_CHIP:
            with self.subTest(status=status):
                self.assertEqual('', self.chip(status)['html'])
                self.assertEqual('', self.chip(status)['cls'])
                self.assertFalse(self.chip(status)['complained'])

    def test_an_unknown_status_draws_nothing_and_says_so(self):
        """A status added to app/fmt_utils.py and not to the guide's table must not inherit
        a trailing branch's colour and word - that is exactly how this defect existed."""
        self.assertEqual('', self.obs['unknown']['html'])
        self.assertEqual('', self.obs['unknown']['cls'])
        self.assertTrue(self.obs['unknown']['complained'],
                        'an unrecognized status was swallowed silently')

    def test_every_drawn_status_is_silent(self):
        """The flip side: a status the guide does know about must not complain."""
        for status in REC_STATUS_DISPLAY:
            with self.subTest(status=status):
                self.assertFalse(self.chip(status)['complained'])


if __name__ == '__main__':
    unittest.main()
