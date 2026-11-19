"""static/js/channel-now.js::channelNow(), Channel it would use's one handler on a league page and
the IPTV Output page (dev/changelog/1639), driven in jsdom over the shipped util.js. One answer
opens that channel and leaves the sentence for the page it lands on, and a new-tab click gets the
dialog instead; two answers open a dialog
with each channel and its account linked; nothing to show, a refusal and a greyed-out button each
say why in a toast and open nothing. No dev/docs/BUGS.md entry: a feature.

  python3 -m unittest tests.test_channel_now_js
"""
import json
import os
import shutil
import subprocess
import unittest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HARNESS = os.path.join(REPO, 'tests', 'support', 'channel_now.mjs')
JSDOM = os.path.join(REPO, 'node_modules', 'jsdom')
URL = '/api/sports/events/{}/channel-now'


@unittest.skipIf(shutil.which('node') is None or not os.path.isdir(JSDOM), 'node or jsdom not installed')
class ChannelNowScriptTests(unittest.TestCase):
    observed = None

    def setUp(self):
        if ChannelNowScriptTests.observed is None:
            proc = subprocess.run([shutil.which('node'), HARNESS, REPO], capture_output=True,
                                  text=True, cwd=REPO, timeout=120)
            if proc.returncode != 0:
                raise AssertionError(f'harness failed:\n{proc.stdout}\n{proc.stderr[-4000:]}')
            ChannelNowScriptTests.observed = json.loads(proc.stdout)
        self.obs = ChannelNowScriptTests.observed

    def test_no_script_errors(self):
        self.assertEqual(self.obs['errors'], [])

    def test_one_answer_opens_the_channel_and_says_so_there(self):
        one = self.obs['one']
        self.assertEqual(one['calls'], [{'url': URL.format(1), 'method': 'GET'}])
        self.assertEqual(one['navs'], ['/channels/11'])
        self.assertIsNone(one['modal'])
        self.assertIn('would record it from this channel', one['pending']['message'])

    def test_a_new_tab_click_gets_the_dialog_and_leaves_no_sentence_behind(self):
        new_tab = self.obs['newTab']
        self.assertEqual(new_tab['navs'], [])
        self.assertIsNone(new_tab['pending'])
        self.assertEqual(new_tab['modal']['links'][0], ['Local 1', '/channels/11'])

    def test_two_answers_show_both_linked_with_their_accounts(self):
        two = self.obs['two']
        self.assertEqual(two['navs'], [])
        m = two['modal']
        self.assertEqual(m['title'], 'Gulls at Bears: channel it would use')
        self.assertEqual(m['heads'], ['A recording', 'Its sports channel on the IPTV output'])
        self.assertEqual(m['links'], [['Local <1>', '/channels/11'], ['Acct One<b>', '/accounts/1'],
                                      ['Locals', '/groups/5'], ['Local 2', '/channels/12'],
                                      ['Acct Two<b>', '/accounts/2']])
        self.assertIn('A recording of a group picks its channel as it starts.', m['text'])
        self.assertEqual(m['buttons'], ['Close'])

    def test_each_channel_has_a_preview_that_closes_the_dialog_first(self):
        """dev/changelog/1645: the channel a recording and a sports channel would use can be watched."""
        self.assertEqual(self.obs['two']['modal']['previews'],
                         [{'channel': '11', 'name': 'Local <1>', 'text': 'Preview'},
                          {'channel': '12', 'name': 'Local 2', 'text': 'Preview'}])
        previewed = self.obs['previewed']
        self.assertEqual(previewed['calls'], [{'url': '/api/channels/12/preview', 'method': 'POST'}])
        self.assertEqual(previewed['titles'], ['Preview - Local 2'])

    def test_names_are_escaped(self):
        html = self.obs['two']['modal']['html']
        self.assertIn('Local &lt;1&gt;', html)
        self.assertNotIn('<b>', html)

    def test_nothing_to_show_says_why_and_opens_nothing(self):
        none = self.obs['none']
        self.assertEqual(none['navs'], [])
        self.assertIsNone(none['modal'])
        self.assertIn('not in your guide yet', none['toast']['text'])

    def test_a_refusal_is_said(self):
        gone = self.obs['gone']
        self.assertEqual(gone['navs'], [])
        self.assertIn('no longer stored', gone['toast']['text'])

    def test_a_greyed_out_button_says_why_and_asks_nothing(self):
        why = self.obs['why']
        self.assertEqual(why['calls'], [])
        self.assertIn('does not list this game yet', why['toast']['text'])


if __name__ == '__main__':
    unittest.main()
