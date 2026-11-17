"""Tier 0 - the record and active-recording dialogs on the shared modal anatomy
(templates/_record_modal.html, static/js/guide.js, util.js::wireModal, dev/changelog/1608).

The two dialogs were the last in the app to wire their own close button, backdrop and
scroll lock by element id, with one page-wide Escape listener in guide.js that ran both
closes on every Escape pressed anywhere, whether either dialog was open or not. They now
sit on wireModal(), the same implementation buildModal() uses, in its `persistent` mode:
the server-rendered dialog is shown and hidden rather than built and removed, so its form
keeps its Jinja-rendered options, CSRF field and element ids.

What it covers: each dialog opens and locks the page, every way to close it (the head's
close button, the backdrop, Escape, its own Cancel or Close) closes it and releases the
lock without touching the other dialog; Escape is the dialog's only while it is open; a
host's escapeHandler is honored as on every buildModal() dialog; a confirm over the
active-recording dialog takes Escape for itself only.

What it cannot cover: jsdom lays nothing out (tests/support/jsdom_layout.mjs stands in for
visibility), so how either dialog looks is a browser question. The tests marked as
characterization below passed before the move too; they hold its behavior, not a defect.

Runs against a throwaway temp SQLite DB - never the live one.
  python3 -m unittest tests.test_record_modal_anatomy_js
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tests.support.app import make_test_app  # noqa: E402
from tests.support import seed  # noqa: E402

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HARNESS = os.path.join(REPO, 'tests', 'support', 'record_modal_anatomy.mjs')
JSDOM = os.path.join(REPO, 'node_modules', 'jsdom')

_RESULT = None


def _observe():
    global _RESULT
    if _RESULT is not None:
        return _RESULT
    t = make_test_app()
    tmp = tempfile.mkdtemp(prefix='record_modal_anatomy_js_')
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
class _Base:
    def obs(self, scenario):
        o = _observe()[scenario]
        if 'error' in o:
            self.fail(f'{scenario} threw in the page:\n{o["error"]}')
        self.assertEqual(o['errors'], [], scenario)
        return o


class CloseEveryWayTests(_Base, unittest.TestCase):
    """Characterization: each dialog opens locked and every closer closes it unlocked."""

    def _check(self, scenario):
        o = self.obs(scenario)
        self.assertEqual(o['before'], {'open': False, 'locked': False}, scenario)
        self.assertEqual(o['opened'], {'open': True, 'locked': True}, scenario)
        self.assertEqual(o['closed'], {'open': False, 'locked': False}, scenario)
        self.assertFalse(o['otherOpen'], scenario)

    def test_record_dialog_close_button(self):
        self._check('record_x')

    def test_record_dialog_backdrop(self):
        self._check('record_backdrop')

    def test_record_dialog_escape(self):
        self._check('record_escape')

    def test_record_dialog_cancel(self):
        self._check('record_cancel')

    def test_active_dialog_close_button(self):
        self._check('active_x')

    def test_active_dialog_backdrop(self):
        self._check('active_backdrop')

    def test_active_dialog_escape(self):
        self._check('active_escape')

    def test_active_dialog_close(self):
        self._check('active_close')

    def test_opening_twice_leaves_no_escape_listener_behind(self):
        o = self.obs('open_twice')
        self.assertFalse(o['open'])
        self.assertFalse(o['locked'])
        self.assertTrue(o['errorStillShown'])


class EscapeOwnershipTests(_Base, unittest.TestCase):

    def test_escape_does_nothing_to_a_closed_dialog(self):
        """The page-wide listener ran closeModal() on every Escape, so it hid the dialog's
        error box and re-derived the scroll lock whether the dialog was open or not."""
        self.assertTrue(self.obs('escape_after_close')['errorStillShown'])

    def test_a_hosts_escape_handler_is_honored(self):
        """wireModal's contract (dev/changelog/1161): an overlay showing a step takes Escape
        to step back. The page-wide listener closed the dialog regardless."""
        o = self.obs('escape_handler')
        self.assertEqual(o['stepped'], 1)
        self.assertTrue(o['stillOpen'])

    def test_a_step_handler_does_not_outlive_its_opening(self):
        """The dialog is hidden, not removed, so a handler left on the element would swallow
        Escape on every later opening."""
        self.assertFalse(self.obs('escape_handler_reset')['open'])

    def test_a_confirm_over_the_active_dialog_takes_escape_alone(self):
        """Characterization: confirmModal stops Escape in the capture phase."""
        o = self.obs('confirm_over_active')
        self.assertFalse(o['confirmed'])
        self.assertTrue(o['confirmGone'])
        self.assertTrue(o['activeStillOpen'])
        self.assertTrue(o['locked'])


class NoHandWiredDialogTests(unittest.TestCase):
    """The shell is wireModal's: guide.js keeps no page-wide Escape listener and no
    id-wired close button or backdrop for these dialogs."""

    def test_guide_js_has_no_hand_wired_shell(self):
        with open(os.path.join(REPO, 'static', 'js', 'guide.js'), encoding='utf-8') as fh:
            src = fh.read()
        for needle in ("getElementById('record-modal').style.display",
                       "getElementById('active-rec-modal').style.display",
                       "getElementById('modal-backdrop')", "getElementById('modal-close')",
                       "getElementById('active-rec-backdrop')",
                       "getElementById('active-rec-close-x')"):
            self.assertNotIn(needle, src)
        self.assertNotRegex(src, r"addEventListener\('keydown'[^\n]*\n[^\n]*Escape",
                            'a page-wide Escape listener is back in guide.js')
        self.assertIn("wireModal(_recordModalEl", src)
        self.assertIn("wireModal(_activeRecModalEl", src)


if __name__ == '__main__':
    unittest.main()
