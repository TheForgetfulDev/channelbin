"""Tier 0 - util.js::confirmModal(), the app's one confirm dialog, driven in a real DOM.

dev/changelog/1162 replaced every native browser confirm() with this helper, and
tests/test_static_invariants.py::NativeConfirmDialogTests keeps a bare confirm() from coming
back. What this file holds down is that the replacement is a real confirm: it resolves true
only on the action button and false on every way of backing out, it follows DESIGN.md 4's
anatomy, and - the case the native dialog never had to think about - an Escape pressed over
a confirm that sits on top of another overlay closes the confirm and nothing else. The guide
opens its Stop, Abort and Cancel recording confirms over its own record modal, whose Escape
handler would otherwise close that modal too.

jsdom computes no layout, so the mobile bottom sheet (DESIGN.md 9.6) is browser work.

  python3 -m unittest tests.test_confirm_modal_js
"""
import json
import os
import shutil
import subprocess
import unittest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HARNESS = os.path.join(REPO, 'tests', 'support', 'confirm_modal.mjs')
JSDOM = os.path.join(REPO, 'node_modules', 'jsdom')

_RESULT = None


def _observe():
    global _RESULT
    if _RESULT is None:
        proc = subprocess.run([shutil.which('node'), HARNESS, REPO],
                              capture_output=True, text=True, cwd=REPO, timeout=120)
        if proc.returncode != 0:
            raise AssertionError(f'harness failed:\n{proc.stdout}\n{proc.stderr}')
        _RESULT = json.loads(proc.stdout)
    return _RESULT


@unittest.skipIf(shutil.which('node') is None, 'node not installed')
@unittest.skipIf(not os.path.isdir(JSDOM), 'jsdom not installed (npm install)')
class ConfirmModalTests(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.obs = _observe()

    def test_no_script_error(self):
        self.assertEqual(self.obs['errors'], [])

    def test_only_the_action_button_confirms(self):
        self.assertIs(self.obs['confirm']['result'], True)
        for way in ('cancel', 'close', 'backdrop', 'escape'):
            self.assertIs(self.obs[way]['result'], False, f'{way} must resolve false')

    def test_every_path_removes_the_dialog_and_leaves_the_host_open(self):
        for way in ('confirm', 'cancel', 'close', 'backdrop', 'escape', 'rich'):
            self.assertEqual(self.obs[way]['overlaysAfter'], 0, way)
            self.assertTrue(self.obs[way]['hostStillOpen'], way)

    def test_escape_closes_the_confirm_and_never_reaches_the_page(self):
        """The guide's own Escape handler closes its record modal; a confirm opened over
        that modal must take Escape for itself."""
        self.assertEqual(self.obs['escape']['hostSawEscape'], 0)

    def test_no_escape_listener_outlives_its_dialog(self):
        self.assertEqual(self.obs['escapeAfterAllClosed'], 1)

    def test_anatomy_is_cancel_then_the_named_verb(self):
        """DESIGN.md 4: the confirm button is the action verb, never OK or Yes."""
        self.assertEqual(self.obs['confirm']['buttons'], ['Cancel', 'Stop'])
        self.assertEqual(self.obs['confirm']['title'], 'Stop recording')
        self.assertEqual(self.obs['rich']['paras'], ['Capture of <b>X</b> stops.',
                                                     'Segments are deleted.'])

    def test_text_is_escaped(self):
        self.assertNotIn('<b>', self.obs['rich']['paraHtml'][0])
        self.assertEqual(self.obs['rich']['list'], ['<i>A</i>', 'B'])

    def test_danger_styles_the_verb_and_focuses_cancel(self):
        """Enter on a destructive confirm must not destroy anything."""
        self.assertIn('btn-danger', self.obs['rich']['confirmClass'])
        self.assertEqual(self.obs['rich']['focused'], 'Cancel')
        self.assertIn('btn-primary', self.obs['confirm']['confirmClass'])
        self.assertEqual(self.obs['confirm']['focused'], 'Stop')


if __name__ == '__main__':
    unittest.main()
