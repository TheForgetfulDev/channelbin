"""Tier 0 - the pure client-side parsing behind the profile create/edit modal
(static/js/profile-modal.js, dev/changelog/356).

The modal turns form inputs into the JSON body the API stores, and that translation is
where "unset" and "zero" can quietly become the same thing. Python cannot reach it, so
this evaluates the file in node and calls the helpers directly - the same arrangement
tests/test_check_modal_js.py uses, and the reason those helpers are declared at file top
level. Nothing test-only lives in the shipped file.

The server-side half of the same invariant is tests/test_health_check_profile_api.py;
the two must agree, because the modal is only presentation of rules the API enforces.
"""
import json
import os
import shutil
import subprocess
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MODAL_JS = os.path.join(REPO, 'static', 'js', 'profile-modal.js')

_EXPORTS = ('pmFlatFields, pmHintText, pmValidate, pmPayload, '
            'HEALTH_CHECK_PROFILE_SECTIONS, RECORDING_PROFILE_SECTIONS')

_HARNESS = f"""
const fs = require('fs');
const src = fs.readFileSync(process.argv[1], 'utf8');
const api = new Function(src + '\\nreturn {{{_EXPORTS}}};')();
const {{{_EXPORTS}}} = api;
console.log(JSON.stringify(eval(process.argv[2])));
"""

# A minimal spec covering all three field types, so these cases do not move when the
# real health-check spec's copy changes.
FIELDS = ("[{key:'name',label:'Name',type:'text',required:true},"
          "{key:'n',label:'Count',type:'int',inheritable:true},"
          "{key:'b',label:'Shots',type:'tristate',inheritable:true,"
          "trueLabel:'Always capture',falseLabel:'Never capture'}]")


@unittest.skipIf(shutil.which('node') is None, 'node not installed')
class _Base(unittest.TestCase):

    def evaluate(self, expr):
        proc = subprocess.run(['node', '-e', _HARNESS, MODAL_JS, expr],
                              capture_output=True, text=True, cwd=REPO, timeout=60)
        if proc.returncode != 0:
            self.fail(f'node failed evaluating `{expr}`:\n{proc.stderr}')
        return json.loads(proc.stdout)


class PayloadTests(_Base):
    """The request body. Blank means inherit (null); 0 means the user chose zero."""

    def test_blank_number_becomes_null_not_zero(self):
        body = self.evaluate(f"pmPayload({FIELDS}, {{name:'x', n:'', b:''}})")
        self.assertIsNone(body['n'])

    def test_zero_stays_zero(self):
        body = self.evaluate(f"pmPayload({FIELDS}, {{name:'x', n:'0', b:''}})")
        self.assertEqual(body['n'], 0)

    def test_number_is_sent_as_a_number_not_a_string(self):
        body = self.evaluate(f"pmPayload({FIELDS}, {{name:'x', n:'45', b:''}})")
        self.assertEqual(body['n'], 45)
        self.assertIsInstance(body['n'], int)

    def test_tristate_unset_is_null(self):
        body = self.evaluate(f"pmPayload({FIELDS}, {{name:'x', n:'', b:''}})")
        self.assertIsNone(body['b'])

    def test_tristate_false_is_false_not_null(self):
        body = self.evaluate(f"pmPayload({FIELDS}, {{name:'x', n:'', b:'false'}})")
        self.assertIs(body['b'], False)

    def test_tristate_true_is_true(self):
        body = self.evaluate(f"pmPayload({FIELDS}, {{name:'x', n:'', b:'true'}})")
        self.assertIs(body['b'], True)

    def test_name_is_trimmed(self):
        body = self.evaluate(f"pmPayload({FIELDS}, {{name:'  Padded  ', n:'', b:''}})")
        self.assertEqual(body['name'], 'Padded')

    def test_every_field_is_always_present_so_an_edit_can_clear_one(self):
        """An omitted key would leave the old value in place on the server; sending an
        explicit null is what makes "clear this back to inherited" work."""
        body = self.evaluate(f"pmPayload({FIELDS}, {{name:'x'}})")
        self.assertEqual(sorted(body.keys()), ['b', 'n', 'name'])


class ValidateTests(_Base):
    """Mirrors the API's _read_profile_body, including that a blank number is valid."""

    def test_missing_name_is_an_error(self):
        self.assertIsNotNone(self.evaluate(f"pmValidate({FIELDS}, {{name:'', n:'', b:''}})"))

    def test_whitespace_name_is_an_error(self):
        self.assertIsNotNone(self.evaluate(f"pmValidate({FIELDS}, {{name:'   ', n:'', b:''}})"))

    def test_blank_number_is_valid_because_it_means_inherit(self):
        self.assertIsNone(self.evaluate(f"pmValidate({FIELDS}, {{name:'x', n:'', b:''}})"))

    def test_zero_is_valid(self):
        self.assertIsNone(self.evaluate(f"pmValidate({FIELDS}, {{name:'x', n:'0', b:''}})"))

    def test_negative_is_rejected(self):
        self.assertIsNotNone(self.evaluate(f"pmValidate({FIELDS}, {{name:'x', n:'-1', b:''}})"))

    def test_fractional_is_rejected(self):
        self.assertIsNotNone(self.evaluate(f"pmValidate({FIELDS}, {{name:'x', n:'1.5', b:''}})"))

    def test_error_names_the_field_label(self):
        msg = self.evaluate(f"pmValidate({FIELDS}, {{name:'x', n:'-1', b:''}})")
        self.assertIn('Count', msg)


class HintTests(_Base):
    """The "leave blank" sentence must state the default the server actually resolved,
    never a literal copied into the spec - that is how the hint and the behavior drift."""

    def test_hint_quotes_the_resolved_default_with_its_unit(self):
        hint = self.evaluate(
            "pmHintText({key:'n',type:'int',inheritable:true,unit:'s'}, {n:120})")
        self.assertIn('120s', hint)

    def test_tristate_hint_uses_the_option_label(self):
        hint = self.evaluate(
            "pmHintText({key:'b',type:'tristate',inheritable:true,"
            "trueLabel:'Always capture',falseLabel:'Never capture'}, {b:true})")
        self.assertIn('Always capture', hint)

    def test_tristate_hint_reflects_a_false_default(self):
        hint = self.evaluate(
            "pmHintText({key:'b',type:'tristate',inheritable:true,"
            "trueLabel:'Always capture',falseLabel:'Never capture'}, {b:false})")
        self.assertIn('Never capture', hint)

    def test_non_inheritable_field_gets_no_hint(self):
        self.assertEqual(self.evaluate("pmHintText({key:'name',type:'text'}, {})"), '')

    def test_missing_default_falls_back_to_generic_wording(self):
        hint = self.evaluate("pmHintText({key:'n',type:'int',inheritable:true}, {})")
        self.assertIn('global default', hint)


class BlankValueTests(_Base):
    """`blankValue` is the client-side mirror of ProfileField.blank_value: what an
    emptied control stores when the column cannot take NULL. Recording Profiles' padding
    fields are the live case (NOT NULL, default 0)."""

    PADDING = "[{key:'p',label:'Pad',type:'int',blankValue:0}]"

    def test_blank_stores_the_declared_blank_value_not_null(self):
        body = self.evaluate(f"pmPayload({self.PADDING}, {{p:''}})")
        self.assertEqual(body['p'], 0)

    def test_a_real_value_still_wins_over_the_blank_value(self):
        body = self.evaluate(f"pmPayload({self.PADDING}, {{p:'7'}})")
        self.assertEqual(body['p'], 7)

    def test_a_field_without_blank_value_still_defaults_to_null(self):
        body = self.evaluate("pmPayload([{key:'q',label:'Q',type:'int'}], {q:''})")
        self.assertIsNone(body['q'])

    def test_inheritable_text_blanks_to_null_not_empty_string(self):
        """A '' filename template would name every recording the same empty string;
        null is what the recorder reads as "use the global template"."""
        body = self.evaluate(
            "pmPayload([{key:'t',label:'T',type:'text',inheritable:true}], {t:'   '})")
        self.assertIsNone(body['t'])

    def test_non_inheritable_text_blanks_to_empty_string(self):
        """`name` inherits nothing - it is required, and the server rejects the empty
        string with a message rather than silently storing NULL."""
        body = self.evaluate(
            "pmPayload([{key:'name',label:'Name',type:'text',required:true}], {name:'  '})")
        self.assertEqual(body['name'], '')


class SpecTests(_Base):
    """Each shipped spec must cover exactly its API's editable fields - a field missing
    here is one the modal can never set, and one that only exists here would 400."""

    def test_health_check_spec_covers_every_editable_field(self):
        keys = self.evaluate(
            'pmFlatFields(HEALTH_CHECK_PROFILE_SECTIONS).map(f => f.key)')
        self.assertEqual(sorted(keys), sorted([
            'name', 'test_duration_seconds', 'wait_between_channels_seconds',
            'screenshots_enabled', 'connect_retries', 'connect_timeout_seconds',
            'connect_retry_delay_seconds',
        ]))

    def test_recording_spec_covers_every_editable_field(self):
        keys = self.evaluate(
            'pmFlatFields(RECORDING_PROFILE_SECTIONS).map(f => f.key)')
        self.assertEqual(sorted(keys), sorted([
            'name', 'filename_template', 'pre_padding_minutes', 'post_padding_minutes',
            'stall_timeout_seconds', 'restart_delay_seconds', 'max_consecutive_failures',
            'stall_move_count', 'stall_move_window_minutes',
            'retention_days', 'pre_check_enabled', 'metadata_sidecar_enabled',
        ]))

    def test_padding_fields_declare_blank_value_zero(self):
        """Their columns are NOT NULL; without this the modal would post null and the
        write would fail (or store a 0 by accident somewhere further down)."""
        spec = self.evaluate(
            "pmFlatFields(RECORDING_PROFILE_SECTIONS)"
            ".filter(f => f.key.endsWith('_padding_minutes'))"
            ".map(f => [f.key, f.blankValue === undefined ? null : f.blankValue, !!f.inheritable])")
        self.assertEqual(len(spec), 2)
        for key, blank_value, inheritable in spec:
            self.assertEqual(blank_value, 0, key)
            self.assertFalse(inheritable, key)

    def test_retention_days_inherits_rather_than_defaulting_to_zero(self):
        """The opposite of the padding fields, and the distinction is load-bearing:
        blank must inherit the global window, because 0 means never delete."""
        spec = self.evaluate(
            "pmFlatFields(RECORDING_PROFILE_SECTIONS)"
            ".filter(f => f.key === 'retention_days')"
            ".map(f => [!!f.inheritable, f.blankValue === undefined])[0]")
        self.assertEqual(spec, [True, True])


if __name__ == '__main__':
    unittest.main()
