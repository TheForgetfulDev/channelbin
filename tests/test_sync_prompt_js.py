"""Tier 0 - the Sync now dialog asks whether a manual sync replaces the next scheduled one.

A manual sync used to leave the account's schedule where it was, so a Sync now could be
followed minutes later by an automatic sync doing the same work again. Sync now now opens a
dialog with an "And skip the scheduled sync in ..." switch, which starts where
`sync.manual_sync_restarts_schedule` says, links to that setting, and sends the user's answer
as `restart_schedule` (dev/changelog/1134). None of that exists until the browser builds it.

tests/support/sync_prompt.mjs runs the shipped util.js and account-actions.js in jsdom with
jsonFetch captured; every assertion lives here.

  python3 -m unittest tests.test_sync_prompt_js
"""
import json
import os
import shutil
import subprocess
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HARNESS = os.path.join(REPO, 'tests', 'support', 'sync_prompt.mjs')
JSDOM = os.path.join(REPO, 'node_modules', 'jsdom')

_RESULT = None


def _observe():
    global _RESULT
    if _RESULT is None:
        proc = subprocess.run([shutil.which('node'), HARNESS, REPO], capture_output=True,
                              text=True, timeout=120, cwd=REPO)
        if proc.returncode != 0:
            raise AssertionError(f'harness failed:\n{proc.stdout[-2000:]}\n{proc.stderr[-4000:]}')
        _RESULT = json.loads(proc.stdout)
    return _RESULT


@unittest.skipIf(shutil.which('node') is None, 'node not installed')
@unittest.skipIf(not os.path.isdir(JSDOM), 'jsdom not installed (npm install)')
class SyncPromptTests(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.obs = _observe()

    def test_no_scenario_threw(self):
        for name, seen in self.obs.items():
            self.assertEqual(seen['errors'], [], name)

    def test_nothing_to_skip_means_no_dialog(self):
        """Automatic sync off, or nothing scheduled: the sync starts straight away, and the
        server's default decides, because nobody was asked."""
        for name in ('auto_off', 'nothing_scheduled', 'no_prompt'):
            seen = self.obs[name]
            self.assertFalse(seen['modal'], name)
            self.assertEqual(len(seen['calls']), 1, name)
            self.assertNotIn('restart_schedule', seen['calls'][0]['body'], name)

    def test_the_dialog_names_the_scheduled_sync_it_would_skip(self):
        seen = self.obs['default_on']
        self.assertTrue(seen['modal'])
        self.assertEqual(seen['title'], 'Sync now')
        self.assertIn('And skip the scheduled sync in 3h 12m', seen['body_text'])
        self.assertEqual(seen['buttons'], ['Cancel', 'Sync now'])

    def test_the_account_name_is_escaped(self):
        self.assertIn('Account &lt;b&gt;2&lt;/b&gt;', self.obs['default_on']['body_html'])

    def test_the_switch_starts_where_the_setting_says(self):
        self.assertTrue(self.obs['default_on']['switch_checked'])
        self.assertIn('starts on', self.obs['default_on']['body_text'])
        self.assertFalse(self.obs['default_off']['switch_checked'])
        self.assertIn('starts off', self.obs['default_off']['body_text'])

    def test_the_dialog_links_to_the_setting(self):
        self.assertEqual(self.obs['default_on']['link'],
                         '/settings?q=sync.manual_sync_restarts_schedule')
        self.assertIn('Change it there', self.obs['default_on']['body_text'])

    def test_the_answer_is_sent(self):
        self.assertEqual(self.obs['default_on']['calls'][0]['body']['restart_schedule'], True)
        self.assertEqual(self.obs['default_off']['calls'][0]['body']['restart_schedule'], False)
        self.assertEqual(self.obs['toggled_off']['calls'][0]['body']['restart_schedule'], False)

    def test_the_note_says_what_each_position_does(self):
        seen = self.obs['toggled_off']
        self.assertIn('12 hours after this one', seen['note'])
        self.assertIn('If this sync fails, the scheduled one still runs', seen['note'])
        self.assertIn('still runs', seen['note_after_toggle'])
        self.assertNotIn('hours after this one', seen['note_after_toggle'])
        self.assertIn('1 hour after this one', self.obs['one_hour']['note'])

    def test_an_overdue_sync_gets_no_countdown(self):
        text = self.obs['overdue']['body_text']
        self.assertIn('And skip the next scheduled sync', text)
        self.assertNotIn('ago', text)

    def test_cancel_sends_nothing(self):
        self.assertEqual(self.obs['cancelled']['calls'], [])
        self.assertEqual(self.obs['cancelled']['modals_left'], 0)

    def test_the_answer_survives_sync_anyway(self):
        """The conflict override resubmits with force; the switch the user set must ride
        along, not fall back to the default."""
        calls = self.obs['conflict']['calls']
        self.assertEqual(len(calls), 2)
        self.assertEqual(calls[1]['body']['force'], True)
        self.assertEqual(calls[1]['body']['restart_schedule'], False)


if __name__ == '__main__':
    unittest.main()
