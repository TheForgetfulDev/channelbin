"""The shared color picker's preview dot (util.js colorPickerHtml/wireColorPicker, used by the
tag and account modals), driven in a real DOM (dev/changelog/1395).

The dot sits right of the Custom box, so it reads as previewing what was typed. It followed
the selected swatch instead, which made a typed color look ignored. It shows the custom color
only, and only once that is a hex value the server accepts.

  python3 -m unittest tests.test_color_picker_js
"""
import json
import os
import shutil
import subprocess
import unittest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HARNESS = os.path.join(REPO, 'tests', 'support', 'color_picker.mjs')
JSDOM = os.path.join(REPO, 'node_modules', 'jsdom')


@unittest.skipIf(shutil.which('node') is None, 'node not installed')
@unittest.skipIf(not os.path.isdir(JSDOM), 'jsdom not installed (npm install)')
class PreviewDotTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        proc = subprocess.run([shutil.which('node'), HARNESS, REPO],
                              capture_output=True, text=True, cwd=REPO, timeout=120)
        if proc.returncode != 0:
            raise AssertionError(f'harness failed:\n{proc.stdout}\n{proc.stderr}')
        result = json.loads(proc.stdout)
        cls.errors, cls.out = result['errors'], result['out']

    def test_no_script_error(self):
        self.assertEqual(self.errors, [])

    def test_hidden_while_a_swatch_is_the_answer(self):
        for step in ('presetOpen', 'afterPick', 'cleared', 'pickAfterCustom'):
            with self.subTest(step=step):
                self.assertTrue(self.out[step]['hidden'], self.out[step])
                self.assertEqual(self.out[step]['bg'], '')

    def test_shows_the_typed_color(self):
        self.assertFalse(self.out['afterTyping']['hidden'])
        self.assertEqual(self.out['afterTyping']['bg'], 'rgb(255, 0, 0)')

    def test_hidden_until_the_typed_value_is_a_hex_color(self):
        self.assertTrue(self.out['halfTyped']['hidden'])

    def test_an_existing_custom_color_shows_on_open(self):
        self.assertFalse(self.out['customOpen']['hidden'])
        self.assertEqual(self.out['customOpen']['bg'], 'rgb(232, 180, 251)')

    def test_the_value_read_is_unchanged(self):
        """The dot is presentation only - what the modal submits still follows the swatch
        or the custom box, whichever was touched last."""
        self.assertEqual(self.out['afterPick']['value'], '#2bb3a6')
        self.assertEqual(self.out['afterTyping']['value'], '#ff0000')
        self.assertEqual(self.out['cleared']['value'], '#bc8cff')
        self.assertEqual(self.out['pickAfterCustom']['value'], '#bc8cff')


if __name__ == '__main__':
    unittest.main()
