"""static/js/live-now.js::goLiveNow(), Go live now's one handler on the IPTV Output page and a
league page (dev/changelog/1530), driven in jsdom over the shipped util.js. Turning it on asks
first and names the game's start and every channel it plays on; turning it off does not ask; a
game with nothing to open says why and sends nothing. No dev/docs/BUGS.md entry: a feature.

  python3 -m unittest tests.test_live_now_js
"""
import json
import os
import shutil
import subprocess
import unittest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HARNESS = os.path.join(REPO, 'tests', 'support', 'live_now.mjs')
JSDOM = os.path.join(REPO, 'node_modules', 'jsdom')
URL = '/api/sports/events/{}/live-now'


@unittest.skipIf(shutil.which('node') is None or not os.path.isdir(JSDOM), 'node or jsdom not installed')
class GoLiveNowScriptTests(unittest.TestCase):
    observed = None

    def setUp(self):
        if GoLiveNowScriptTests.observed is None:
            proc = subprocess.run([shutil.which('node'), HARNESS, REPO], capture_output=True,
                                  text=True, cwd=REPO, timeout=120)
            if proc.returncode != 0:
                raise AssertionError(f'harness failed:\n{proc.stdout}\n{proc.stderr[-4000:]}')
            GoLiveNowScriptTests.observed = json.loads(proc.stdout)
        self.obs = GoLiveNowScriptTests.observed

    def test_no_script_errors(self):
        self.assertEqual(self.obs['errors'], [])

    def test_turning_it_on_asks_with_the_start_and_the_channels_then_puts_once(self):
        on = self.obs['on']
        self.assertEqual(on['modal']['title'], 'Go live now')
        self.assertEqual(on['modal']['paras'], ['Gulls at Bears is scheduled to start today at 7:00 PM.'])
        self.assertEqual(on['modal']['list'], ['Ironvale Bears', 'Pretend League 3'])
        self.assertEqual(on['modal']['buttons'], ['Cancel', 'Go live now'])
        self.assertEqual(on['calls'], [{'url': URL.format(7), 'method': 'PUT', 'body': {'on': True}}])
        self.assertTrue(on['result'])
        self.assertIn('until 12:30 AM', on['toast']['text'])

    def test_cancel_sends_nothing(self):
        self.assertEqual(self.obs['cancelled']['calls'], [])
        self.assertFalse(self.obs['cancelled']['result'])

    def test_turning_it_off_does_not_ask(self):
        off = self.obs['off']
        self.assertIsNone(off['modal'])
        self.assertEqual(off['calls'], [{'url': URL.format(7), 'method': 'PUT', 'body': {'on': False}}])
        self.assertTrue(off['result'])

    def test_nothing_to_open_says_why_and_sends_nothing(self):
        why = self.obs['why']
        self.assertIsNone(why['modal'])
        self.assertEqual(why['calls'], [])
        self.assertFalse(why['result'])
        self.assertEqual(why['toast']['text'], 'No sports channel carries it.')
        self.assertIn('toast-warning', why['toast']['cls'])

    def test_a_refusal_is_shown_and_the_page_is_swapped(self):
        refused = self.obs['refused']
        self.assertEqual(refused['toast']['text'], 'That game is no longer in the schedule.')
        self.assertIn('toast-error', refused['toast']['cls'])
        self.assertTrue(refused['result'], 'what the page showed was out of date')


if __name__ == '__main__':
    unittest.main()
