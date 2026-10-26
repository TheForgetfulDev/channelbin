"""Tier 0 - static/js/examples.js, "Try it with example channels" and Delete the examples,
driven in a real DOM (dev/changelog/1281).

The delete's confirm dialog is filled from GET /api/examples/removal, the plan the delete
itself follows. What is held down here:

  (a) The dialog lists every line of that plan, and only its action button deletes.
  (b) When the plan names something standing in the way, the user is told that instead, and
      the delete is never sent - a button that would only be refused is not offered.
  (c) A refusal from the delete itself reaches the user as the server's own sentence.
  (d) The load asks first, and sends one request when confirmed. It leaves no toast for the
      reloaded page, which shows the load's progress itself (dev/changelog/1288).
  (e) Keep the examples asks first and sends one request; backing out sends nothing.
  (f) A menu that dispatches by data-act reaches the same delete through
      window.removeExamples, with no button to hold.

tests/support/examples_buttons.mjs replays each scenario; every assertion lives here.

  python3 -m unittest tests.test_examples_js
"""
import json
import os
import shutil
import subprocess
import unittest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HARNESS = os.path.join(REPO, 'tests', 'support', 'examples_buttons.mjs')
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
class ExamplesButtonsTests(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.obs = _observe()

    def test_no_script_error_in_any_scenario(self):
        for name, sc in self.obs.items():
            self.assertEqual(sc['errors'], [], name)

    def test_the_delete_dialog_lists_the_plan_and_its_button_deletes(self):
        sc = self.obs['removeConfirmed']
        self.assertEqual(sc['shown']['title'], 'Delete the examples?')
        self.assertEqual(sc['shown']['items'],
                         ['The account "Example channels (M3UPT)"', 'The group'])
        self.assertEqual(sc['shown']['buttons'][-1], 'Delete the examples')
        self.assertEqual(sc['calls'], ['GET /api/examples/removal', 'POST /api/examples/remove'])

    def test_backing_out_sends_nothing(self):
        self.assertEqual(self.obs['removeBacked']['calls'], ['GET /api/examples/removal'])

    def test_a_blocker_is_told_and_the_delete_never_sent(self):
        sc = self.obs['removeBlocked']
        self.assertEqual(sc['shown']['title'], 'The examples cannot be deleted yet')
        self.assertEqual(sc['shown']['items'], ['"bbc_tonight" is scheduled.'])
        self.assertEqual(sc['shown']['buttons'], ['Close'])
        self.assertEqual(sc['calls'], ['GET /api/examples/removal'])

    def test_a_refused_delete_shows_the_servers_sentence(self):
        self.assertEqual(self.obs['removeRefused']['toast'],
                         'The examples were not removed. Still syncing.')

    def test_the_load_asks_first_and_sends_one_request(self):
        sc = self.obs['loadConfirmed']
        self.assertEqual(sc['shown']['title'], 'Load the example channels?')
        self.assertEqual(sc['shown']['buttons'][-1], 'Load the examples')
        self.assertEqual(sc['calls'], ['POST /api/examples'])

    def test_a_successful_load_leaves_no_toast_over_its_own_progress(self):
        """The toast sat over the box's own progress text (dev/changelog/1288)."""
        self.assertEqual(self.obs['loadConfirmed']['pending'], [])
        self.assertIsNone(self.obs['loadConfirmed']['toast'])

    def test_keep_asks_first_and_sends_one_request(self):
        sc = self.obs['keepConfirmed']
        self.assertEqual(sc['shown']['title'], 'Keep the examples?')
        self.assertEqual(sc['shown']['buttons'][-1], 'Keep the examples')
        self.assertEqual(sc['calls'], ['POST /api/examples/keep'])
        self.assertTrue(any('Yours now.' in p for p in sc['pending']))

    def test_backing_out_of_keep_sends_nothing(self):
        self.assertEqual(self.obs['keepBacked']['calls'], [])

    def test_a_menu_reaches_the_delete_through_the_global(self):
        sc = self.obs['removeFromMenu']
        self.assertEqual(sc['shown']['title'], 'Delete the examples?')
        self.assertEqual(sc['calls'], ['GET /api/examples/removal', 'POST /api/examples/remove'])


if __name__ == '__main__':
    unittest.main()
