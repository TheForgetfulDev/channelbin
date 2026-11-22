"""Watching now's "Move to another channel" in the browser (static/js/play-move.js), driven in
jsdom by tests/support/play_move.mjs: it asks first, saying what the move writes, posts once to
the play's own address, and shows the server's sentence. dev/changelog/1675. No
dev/docs/BUGS.md entry: a feature.
  python3 -m unittest tests.test_play_move_js
"""
import json
import os
import shutil
import subprocess
import unittest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HARNESS = os.path.join(REPO, 'tests', 'support', 'play_move.mjs')
JSDOM = os.path.join(REPO, 'node_modules', 'jsdom')
_OBSERVED = {}


@unittest.skipIf(shutil.which('node') is None or not os.path.isdir(JSDOM), 'node or jsdom not installed')
class PlayMoveScriptTests(unittest.TestCase):
    def setUp(self):
        if not _OBSERVED:
            proc = subprocess.run([shutil.which('node'), HARNESS, REPO], capture_output=True,
                                  text=True, cwd=REPO, timeout=60)
            if proc.returncode != 0:
                raise AssertionError(f'harness failed:\n{proc.stdout}\n{proc.stderr[-4000:]}')
            _OBSERVED.update(json.loads(proc.stdout))
        self.obs = _OBSERVED

    def test_no_script_errors(self):
        self.assertEqual(self.obs['errors'], [])

    def test_it_asks_first_and_says_what_the_move_writes(self):
        shown = self.obs['cancelled']['shown']
        self.assertEqual(shown['title'], 'Move this play to another channel?')
        self.assertIn('moves off "Feed a"', shown['paras'][0])
        self.assertIn('plays Game feeds', shown['paras'][0])
        self.assertIn('score adjustment of -100', shown['paras'][1])
        self.assertEqual(shown['confirm'], 'Move')

    def test_cancel_posts_nothing(self):
        self.assertEqual(self.obs['cancelled']['posts'], [])
        self.assertFalse(self.obs['cancelled']['result'])

    def test_a_move_posts_once_to_the_plays_address_and_shows_the_servers_sentence(self):
        moved = self.obs['moved']
        self.assertEqual(moved['posts'], ['POST /api/iptv-output/plays/s%2F1/move'])
        self.assertTrue(moved['result'])
        self.assertEqual(len(moved['toasts']), 1)
        self.assertIn('Moved to "Feed b".', moved['toasts'][0]['text'])

    def test_a_score_that_could_not_be_saved_is_a_warning(self):
        self.assertIn('warning', self.obs['unsaved']['toasts'][0]['cls'])
        self.assertNotIn('warning', self.obs['moved']['toasts'][0]['cls'])

    def test_a_refusal_says_why_and_still_asks_the_page_to_redraw(self):
        refused = self.obs['refused']
        self.assertTrue(refused['result'])
        self.assertIn('No other channel can play it right now.', refused['toasts'][0]['text'])
        self.assertIn('error', refused['toasts'][0]['cls'])


if __name__ == '__main__':
    unittest.main()
