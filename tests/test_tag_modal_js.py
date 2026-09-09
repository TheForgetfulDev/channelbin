"""Tier 0 - the pure client-side rules behind the tag create/edit modal
(static/js/tag-modal.js, dev/changelog/448).

The modal turns three controls into the JSON body the API stores. Python cannot reach that
translation, so this evaluates the file in node and calls the helpers directly - the same
arrangement tests/test_profile_modal_js.py and tests/test_check_modal_js.py use, and the
reason those helpers are declared at file top level. Nothing test-only lives in the shipped
file.

The server-side half of the same rules is tests/test_tags_page_conformance.py::ApiTests, and
the two MUST agree in one direction specifically: the client may not be STRICTER than the
server, or the modal refuses to submit something the API would happily have stored and the
user has no way to find out why. The Unicode-name cases below are that pairing - Python's
str.isalnum() accepts a non-ASCII name, so an /^[a-z0-9]+$/ check here would have been a
silently unfixable form.

  python3 -m unittest tests.test_tag_modal_js
"""
import json
import os
import shutil
import subprocess
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MODAL_JS = os.path.join(REPO, 'static', 'js', 'tag-modal.js')

_EXPORTS = 'tagCleanPatterns, tagNormalizeName, tagValidate, tagPayload, TAG_DEFAULT_COLOR'

_HARNESS = f"""
const fs = require('fs');
const src = fs.readFileSync(process.argv[1], 'utf8');
const api = new Function(src + '\\nreturn {{{_EXPORTS}}};')();
const {{{_EXPORTS}}} = api;
console.log(JSON.stringify(eval(process.argv[2])));
"""

OK = "{name:'live', color:'#f85149', patterns:['LIVE']}"


@unittest.skipIf(shutil.which('node') is None, 'node not installed')
class _Base(unittest.TestCase):

    def evaluate(self, expr):
        proc = subprocess.run(['node', '-e', _HARNESS, MODAL_JS, expr],
                              capture_output=True, text=True, cwd=REPO, timeout=60)
        if proc.returncode != 0:
            self.fail(f'node failed evaluating `{expr}`:\n{proc.stderr}')
        return json.loads(proc.stdout)


class LoadsWithoutADomTests(_Base):
    """The file must have no side effects at load - openTagModal touches `document`, but
    only when called. A top-level DOM reference would make it unloadable here AND would run
    before DOMContentLoaded on the real page."""

    def test_the_file_evaluates_outside_a_browser(self):
        self.assertEqual(self.evaluate('TAG_DEFAULT_COLOR'), '#58a6ff')


class PatternCleaningTests(_Base):
    """Mirrors _clean_patterns in app/routes/tags.py."""

    def test_blank_rows_are_dropped(self):
        self.assertEqual(self.evaluate("tagCleanPatterns(['LIVE', '', '   '])"), ['LIVE'])

    def test_values_are_trimmed(self):
        self.assertEqual(self.evaluate("tagCleanPatterns(['  LIVE  '])"), ['LIVE'])

    def test_duplicates_are_dropped_keeping_the_first(self):
        self.assertEqual(self.evaluate("tagCleanPatterns(['LIVE', 'NEW', 'LIVE'])"),
                         ['LIVE', 'NEW'])

    def test_case_variants_are_not_duplicates(self):
        """Matching is case-insensitive at USE time, but the stored strings are literal -
        collapsing them here would silently discard a pattern the user typed."""
        self.assertEqual(self.evaluate("tagCleanPatterns(['LIVE', 'live'])"),
                         ['LIVE', 'live'])

    def test_a_missing_list_is_not_a_crash(self):
        self.assertEqual(self.evaluate('tagCleanPatterns(null)'), [])


class NameNormalizationTests(_Base):

    def test_the_name_is_lowercased_and_trimmed(self):
        self.assertEqual(self.evaluate("tagNormalizeName('  LiVe ')"), 'live')

    def test_a_null_name_becomes_the_empty_string(self):
        self.assertEqual(self.evaluate('tagNormalizeName(null)'), '')


class ValidationTests(_Base):
    """Presentation only - app/routes/tags.py enforces the same rules. What matters here is
    that it never rejects something the server accepts."""

    def test_a_valid_tag_passes(self):
        self.assertIsNone(self.evaluate(f'tagValidate({OK})'))

    def test_an_empty_name_is_caught(self):
        self.assertIn('required', self.evaluate(
            "tagValidate({name:'  ', color:'#f85149', patterns:['LIVE']})"))

    def test_a_name_of_only_separators_is_caught(self):
        """Stripping the hyphens and underscores leaves nothing, and "nothing" is not a
        valid name - the same edge Python's ''.isalnum() == False covers."""
        self.assertIsNotNone(self.evaluate(
            "tagValidate({name:'-_', color:'#f85149', patterns:['LIVE']})"))

    def test_punctuation_in_a_name_is_caught(self):
        self.assertIn('letters, numbers, hyphens', self.evaluate(
            "tagValidate({name:'live!', color:'#f85149', patterns:['LIVE']})"))

    def test_a_space_in_a_name_is_caught(self):
        self.assertIsNotNone(self.evaluate(
            "tagValidate({name:'live now', color:'#f85149', patterns:['LIVE']})"))

    def test_hyphens_and_underscores_are_allowed(self):
        self.assertIsNone(self.evaluate(
            "tagValidate({name:'live_now-2', color:'#f85149', patterns:['LIVE']})"))

    def test_a_non_ascii_name_is_allowed(self):
        """Paired with ApiTests::test_a_non_ascii_name_is_accepted. An ASCII-only class
        here would block a name the server stores without complaint."""
        self.assertIsNone(self.evaluate(
            "tagValidate({name:'\\u00f1o\\u00f1o', color:'#f85149', patterns:['LIVE']})"))

    def test_a_pattern_list_of_only_blanks_is_caught(self):
        self.assertIn('match pattern', self.evaluate(
            "tagValidate({name:'live', color:'#f85149', patterns:['', '  ']})"))

    def test_a_junk_colour_is_caught(self):
        self.assertIn('hex', self.evaluate(
            "tagValidate({name:'live', color:'red', patterns:['LIVE']})"))

    def test_a_three_digit_hex_is_allowed(self):
        self.assertIsNone(self.evaluate(
            "tagValidate({name:'live', color:'#f00', patterns:['LIVE']})"))

    def test_the_name_rule_is_checked_before_the_pattern_rule(self):
        """One message at a time, and it names the field the user has to go fix first."""
        self.assertIn('required', self.evaluate(
            "tagValidate({name:'', color:'#f85149', patterns:[]})"))


class PayloadTests(_Base):

    def test_the_payload_carries_the_normalized_name(self):
        self.assertEqual(self.evaluate(
            "tagPayload({name:'  LIVE ', color:'#f85149', patterns:['X']})")['name'],
            'live')

    def test_the_payload_carries_the_cleaned_patterns(self):
        self.assertEqual(self.evaluate(
            "tagPayload({name:'live', color:'#f85149', patterns:[' X ', '', 'X', 'Y']})"
        )['patterns'], ['X', 'Y'])

    def test_a_missing_colour_falls_back_to_the_default(self):
        """Paired with ApiTests::test_a_missing_colour_falls_back_to_the_default - the two
        defaults have to be the same value or a tag's colour changes on save."""
        self.assertEqual(self.evaluate(
            "tagPayload({name:'live', color:'', patterns:['X']})")['color'], '#58a6ff')

    def test_the_body_carries_exactly_the_three_stored_fields(self):
        """No id in the body: the row being edited is identified by the URL. Sending one
        would give the API a second, contradictable source for which row to write."""
        body = self.evaluate(f'tagPayload({OK})')
        self.assertEqual(sorted(body), ['color', 'name', 'patterns'])


if __name__ == '__main__':
    unittest.main()
