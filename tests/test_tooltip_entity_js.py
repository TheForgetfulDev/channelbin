"""Tier 0 - a tooltip renders its line break, whichever way the tip reached the DOM
(static/js/util.js).

Guards dev/docs/BUGS.md 2026-09-11 @ "Tooltips passed as Jinja macro arguments printed a
literal &#10;".

Authored tips write their line break as "&#10;". A tip written into markup is parsed as HTML,
so the entity is a real newline before anything reads it - but a tip handed to a macro
(stat_row's `tip=`, account_pill's) is a Jinja variable, and autoescape spells it "&amp;#10;",
so the attribute holds those six characters. util.js fills the tooltip with textContent, which
renders exactly what it is handed, so every one of those tips printed "&#10;" as text in the
middle of the sentence. 126 tips across 15 templates are passed that way, the account page's
Content rows among them.

The second class below pins the server side of the same fact: it renders the real stat_row
macro and asserts the escaped spelling is genuinely what the app sends, so the decode in
util.js cannot be deleted as dead code by someone reading only the templates.

tests/support/tooltip.mjs drives the shipped util.js in jsdom; every assertion lives here.
What it cannot cover: jsdom computes no layout, so the tooltip's position, its flip near the
viewport edge and the sticky-header clamp are browser work.

  python3 -m unittest tests.test_tooltip_entity_js
"""
import json
import os
import shutil
import subprocess
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from flask import render_template_string  # noqa: E402

from tests.support.app import make_test_app  # noqa: E402

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HARNESS = os.path.join(REPO, 'tests', 'support', 'tooltip.mjs')
JSDOM = os.path.join(REPO, 'node_modules', 'jsdom')

_RESULT = None


def _observe():
    global _RESULT
    if _RESULT is None:
        out = subprocess.run(['node', HARNESS, REPO], capture_output=True, text=True,
                             cwd=REPO, timeout=120)
        if out.returncode != 0:
            raise AssertionError(f'tooltip harness failed: {out.stderr[-2000:]}')
        _RESULT = json.loads(out.stdout)
    return _RESULT


@unittest.skipIf(shutil.which('node') is None, 'node not installed')
@unittest.skipUnless(os.path.isdir(JSDOM), 'jsdom not installed (npm install)')
class TooltipLineBreakTests(unittest.TestCase):
    def setUp(self):
        self.obs = _observe()
        self.assertEqual(self.obs['errors'], [], 'util.js must load cleanly')

    def test_a_macro_argument_tip_renders_a_real_line_break(self):
        """The defect: this attribute really does hold "&#10;" as text, because autoescape
        wrote "&amp;#10;" into the page."""
        macro = self.obs['macro']
        self.assertIn('&#10;', macro['attribute'],
                      'the harness must reproduce what the server actually sends')
        self.assertIn('\n', macro['rendered'])
        self.assertNotIn('&#10;', macro['rendered'],
                         'the tooltip printed the entity as text instead of breaking the line')
        self.assertEqual(macro['rendered'].split('\n')[0],
                         'Malformed URLs skipped (last sync).')

    def test_a_tip_written_into_markup_still_renders_its_line_break(self):
        """The path that always worked: the HTML parser decoded the entity, so the attribute
        holds a newline already. The fix must not disturb it."""
        markup = self.obs['markup']
        self.assertNotIn('&#10;', markup['attribute'])
        self.assertIn('\n', markup['rendered'])
        self.assertNotIn('&#10;', markup['rendered'])

    def test_a_one_line_tip_is_unchanged_and_still_opens(self):
        plain = self.obs['plain']
        self.assertEqual(plain['rendered'], 'One line, no break.')
        self.assertEqual(plain['displayed'], 'block')


@unittest.skipIf(shutil.which('node') is None, 'node not installed')
@unittest.skipUnless(os.path.isdir(JSDOM), 'jsdom not installed (npm install)')
class TooltipFollowsRebuiltAnchorTests(unittest.TestCase):
    """Guards dev/docs/BUGS.md 2026-10-20 "The shared tooltip kept its old coordinates when
    the node under it was rebuilt".

    The tooltip's coordinates were computed once, at hover time. The dashboard timeline
    replaces its markup every 60 seconds and slides its bars 4px a minute, so a tooltip
    left open over it drifted out of alignment for as long as the mouse stayed still.
    The fix is in the one shared renderer: while a tooltip is open it watches the DOM,
    re-measures an anchor that survived a mutation, hands itself to the node now under the
    pointer when that node carries the same tip, and closes only when what is under the
    pointer is something else. tests/support/tooltip.mjs stubs elementFromPoint and the
    anchors' rects, so each outcome is visible as the popover's `left` or `display`."""

    def setUp(self):
        self.obs = _observe()
        self.assertEqual(self.obs['errors'], [], 'util.js must load cleanly')
        self.rb = self.obs['rebuild']

    def test_a_surviving_anchor_is_re_measured_when_the_page_changes_around_it(self):
        s = self.rb['survivor']
        self.assertEqual(s['leftAtHover'], '120px')
        self.assertEqual(s['leftAfter'], '220px',
                         'the anchor moved and the page mutated, but the tooltip stayed put')
        self.assertEqual(s['displayed'], 'block')

    def test_a_replacement_carrying_the_same_tip_takes_over_without_a_flicker(self):
        same = self.rb['sameTip']
        self.assertEqual(same['displayed'], 'block', 'the tooltip closed on a rebuild')
        self.assertEqual(same['left'], '320px', 'the tooltip did not move onto the new node')
        self.assertEqual(same['text'], 'Live: Channel 5')
        # The replacement IS the anchor now, so hovering it must not re-open or re-place.
        self.assertEqual(self.rb['reHover'], {'left': '320px', 'displayed': 'block'})

    def test_a_replacement_carrying_a_different_tip_closes_the_tooltip(self):
        self.assertEqual(self.rb['otherTip']['displayed'], 'none',
                         'the tooltip kept describing a node that is no longer under the pointer')

    def test_nothing_with_a_tip_under_the_pointer_closes_it_too(self):
        self.assertEqual(self.rb['nothingUnder']['displayed'], 'none')


@unittest.skipIf(shutil.which('node') is None, 'node not installed')
@unittest.skipIf(not os.path.isdir(JSDOM), 'jsdom not installed (npm install)')
class PressClosesTipTests(unittest.TestCase):
    """dev/changelog/1292: the Readiness card's "Run 4 more checks" kept its long tip open
    over the rows the run was rewriting, because nothing but the pointer leaving closed it.
    A mouse press now closes a tip the way a native title does; a touch press does not,
    since a tap is how a phone opens one."""

    def setUp(self):
        self.obs = _observe()

    def test_a_touch_press_leaves_the_tip_open(self):
        self.assertEqual(self.obs['press']['afterTouch'], 'block')

    def test_a_mouse_press_closes_it(self):
        self.assertEqual(self.obs['press']['afterMouse'], 'none')


class MacroTipEscapingTests(unittest.TestCase):
    """Why the decode above is load-bearing, pinned on the server side."""

    def setUp(self):
        self.t = make_test_app()

    def tearDown(self):
        self.t.cleanup()

    def test_stat_row_sends_the_escaped_spelling_for_a_tip_argument(self):
        with self.t.app.test_request_context():
            html = render_template_string(
                "{% import '_macros.html' as m %}"
                "{{ m.stat_row('Key', '1', tip='Key.&#10;Body.') }}")
        self.assertIn('data-tip="Key.&amp;#10;Body."', html,
                      'a macro-argument tip is autoescaped - util.js decodes it on the way out')


if __name__ == '__main__':
    unittest.main(verbosity=2)
