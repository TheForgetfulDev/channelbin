"""Tier 0 - the pure client-side half of the one dropdown component
(static/js/dropdown.js, DESIGN.md 15.3, dev/changelog/440).

Five controls across Settings and Notifications are one component precisely so their
trigger labels cannot drift apart, and that promise lives in three pure functions:
the key split, the label derivation and the trigger markup. Python cannot reach them,
so this evaluates the file in node and calls them directly - the same arrangement
tests/test_profile_modal_js.py and tests/test_check_modal_js.py use, and the reason
those helpers are declared at file top level. Nothing test-only lives in the shipped
file; the delegated listeners are wired by registerDropdown(), which this never calls.

The positioning half (viewport clamp, flip near the bottom, escaping .table-scroll)
belongs to util.js::positionMenu and needs real layout, so it is Tier 4.
"""
import json
import os
import shutil
import subprocess
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DROPDOWN_JS = os.path.join(REPO, 'static', 'js', 'dropdown.js')

_EXPORTS = 'ddKeyParts, ddPickLabel, dropdownTriggerHtml, registerDropdown'

# escHtml lives in util.js, which is loaded ahead of dropdown.js on every page. The
# stub is the same escaping, not a different one - it exists so this can evaluate one
# file rather than the whole shared bundle. `document` is stubbed only far enough for
# registerDropdown to wire its delegated listeners into nothing; every assertion below
# is about a pure function's return value, never about a dispatched event.
_HARNESS = f"""
const fs = require('fs');
function escHtml(s) {{
  return String(s == null ? '' : s).replace(/[&<>"']/g, c => (
    {{'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}}[c]));
}}
const document = {{ addEventListener() {{}}, getElementById() {{ return null; }},
                   querySelectorAll() {{ return []; }} }};
const src = fs.readFileSync(process.argv[1], 'utf8');
const api = new Function('escHtml', 'document', src + '\\nreturn {{{_EXPORTS}}};')(escHtml, document);
const {{{_EXPORTS}}} = api;
console.log(JSON.stringify(eval(process.argv[2])));
"""


@unittest.skipIf(shutil.which('node') is None, 'node not installed')
class _Base(unittest.TestCase):

    def evaluate(self, expr):
        proc = subprocess.run(['node', '-e', _HARNESS, DROPDOWN_JS, expr],
                              capture_output=True, text=True, cwd=REPO, timeout=60)
        if proc.returncode != 0:
            self.fail(f'node failed evaluating `{expr}`:\n{proc.stderr}')
        return json.loads(proc.stdout)


class KeyPartsTests(_Base):
    """`id:arg` addresses 21 routing rows with one definition."""

    def test_a_bare_id_has_an_empty_argument(self):
        self.assertEqual(self.evaluate("ddKeyParts('addsvc')"), ['addsvc', ''])

    def test_id_and_argument_split_on_the_colon(self):
        self.assertEqual(self.evaluate("ddKeyParts('push:SYNC_FAILED')"),
                         ['push', 'SYNC_FAILED'])

    def test_only_the_first_colon_splits(self):
        """An alert type or service key may itself contain a colon; splitting on all of
        them would address the wrong definition and render an empty menu."""
        self.assertEqual(self.evaluate("ddKeyParts('push:a:b')"), ['push', 'a:b'])


class PickLabelTests(_Base):
    """`None` / the one name / `N things`, re-derived from state on every sync."""

    def test_nothing_selected_reads_none(self):
        self.assertEqual(self.evaluate("ddPickLabel([], 'services')"), 'None')

    def test_one_selection_names_it(self):
        self.assertEqual(self.evaluate("ddPickLabel(['Pushover'], 'services')"), 'Pushover')

    def test_several_selections_are_counted(self):
        self.assertEqual(self.evaluate("ddPickLabel(['Pushover','Discord'], 'services')"),
                         '2 services')


class TriggerHtmlTests(_Base):
    """The trigger's markup, including the decoration rule from DESIGN.md 15.7."""

    _DEF = ("registerDropdown('t', {title: () => 'T', rows: () => [], "
            "on: () => [], label: () => 'Add a service'})")

    def test_the_label_prefix_is_carried_as_data_not_baked_into_the_text(self):
        """15.7: a trigger label rewritten by a label function must not carry decoration
        inside its text. syncDropdownTriggers() rewrites .mlbl from label(), so a `+`
        glyph baked in there is erased by the next sync and the button silently loses
        its affordance. It travels as data-mpre and is re-applied on every sync.
        """
        html = self.evaluate(f"({self._DEF}, dropdownTriggerHtml('t', 'btn-accent', '+ '))")
        self.assertIn('data-mpre="+ "', html)
        self.assertIn('>+ Add a service<', html)

    def test_no_prefix_means_no_data_attribute(self):
        html = self.evaluate(f"({self._DEF}, dropdownTriggerHtml('t'))")
        self.assertNotIn('data-mpre', html)
        self.assertIn('>Add a service<', html)

    def test_the_trigger_carries_its_key_and_the_extra_class(self):
        html = self.evaluate(f"({self._DEF}, dropdownTriggerHtml('t', 'btn-accent'))")
        self.assertIn('data-msel="t"', html)
        self.assertIn('class="btn btn-sm msel btn-accent"', html)

    def test_a_key_with_markup_in_it_is_escaped(self):
        """The key reaches an HTML attribute, and an alert type comes from config.yaml."""
        html = self.evaluate(f"({self._DEF}, dropdownTriggerHtml('t:\\\"><b>'))")
        self.assertNotIn('<b>', html)


if __name__ == '__main__':
    unittest.main()
