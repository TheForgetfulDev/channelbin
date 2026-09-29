"""The "Schedule selected" modal (static/js/bulk-schedule-modal.js, dev/changelog/1157),
driven in jsdom.

What it must do: list every showing the server previewed, including the ones it will skip
and why; mark a connection-limit overlap louder than a plain overlap; count only the
showings that will actually be created on the Schedule button; re-ask the server when the
profile changes (padding moves every time); and after a create, close on a clean result
but keep a failure on screen by name rather than in a toast.

`tests/support/bulk_schedule_modal.mjs` drives it and reports observations; every
assertion lives here. jsdom computes no layout, so the list's scroll height and 375px are
browser work.
"""
import json
import os
import shutil
import subprocess
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HARNESS = os.path.join(REPO, 'tests', 'support', 'bulk_schedule_modal.mjs')
JSDOM = os.path.join(REPO, 'node_modules', 'jsdom')

_RESULT = None


def _observe():
    global _RESULT
    if _RESULT is None:
        proc = subprocess.run([shutil.which('node'), HARNESS, REPO],
                              capture_output=True, text=True, cwd=REPO, timeout=120)
        if proc.returncode != 0:
            raise AssertionError(f'harness failed:\n{proc.stdout}\n{proc.stderr[-4000:]}')
        _RESULT = json.loads(proc.stdout)
    return _RESULT


@unittest.skipIf(shutil.which('node') is None, 'node not installed')
@unittest.skipIf(not os.path.isdir(JSDOM), 'jsdom not installed (npm install)')
class BulkScheduleModalTests(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.obs = _observe()
        cls.clean = cls.obs['clean']
        cls.partial = cls.obs['partial']

    def test_no_script_errors(self):
        self.assertEqual(self.clean['errors'], [])
        self.assertEqual(self.partial['errors'], [])

    def test_the_preview_asks_for_every_showing_with_each_showings_default(self):
        body = self.clean['first_preview_body']
        self.assertEqual([i['epg_id'] for i in body['items']], [1, 2, 3, 4])
        self.assertEqual(body['items'][2]['group_id'], 9)
        self.assertEqual(body['profile'], 'default')
        self.assertEqual(self.clean['profile_options'], ['default', 'none', '5'])

    def test_every_showing_is_listed_including_the_skipped_one_with_its_reason(self):
        rows = self.clean['rows']
        self.assertEqual([r['title'] for r in rows], ['Alpha', 'Beta', 'Gamma', 'Delta'])
        skip = rows[3]
        self.assertTrue(skip['skip'])
        self.assertEqual(skip['badge'], 'Skipped')
        self.assertIn('Already scheduled.', skip['text'])

    def test_a_connection_limit_warning_is_louder_than_an_overlap(self):
        ok, hard, warn, _ = self.clean['rows']
        self.assertEqual(ok['badge'], '')
        self.assertEqual(ok['warn'], 0)
        self.assertEqual(hard['badge'], 'Connection limit')
        self.assertTrue(hard['hard'])
        self.assertIn('also selected', hard['text'])
        self.assertEqual(hard['warn'], 1, 'the overlap is not repeated under the limit warning')
        self.assertEqual(warn['badge'], 'Overlaps')
        self.assertFalse(warn['hard'])

    def test_a_group_showing_names_the_group_and_its_member(self):
        self.assertIn('Sports via Three', self.clean['rows'][2]['text'])

    def test_the_button_and_summary_count_only_what_will_be_created(self):
        self.assertEqual(self.clean['submit_label'], 'Schedule 3 recordings')
        self.assertFalse(self.clean['submit_disabled'])
        summary = self.clean['summary']
        self.assertIn('3 recordings will be scheduled', summary)
        self.assertIn('1 over a connection limit', summary)
        self.assertIn('1 overlapping', summary)
        self.assertIn('1 skipped', summary)

    def test_changing_the_profile_asks_again(self):
        self.assertEqual(self.clean['profile_change_body']['profile'], 5)

    def test_schedule_posts_the_same_items_and_profile(self):
        req = self.clean['schedule_request']
        self.assertIsNotNone(req)
        self.assertEqual(len(req['body']['items']), 4)
        self.assertEqual(req['body']['profile'], 5)

    def test_a_clean_create_closes_and_toasts_the_counts(self):
        self.assertFalse(self.clean['modal_open_after'])
        self.assertEqual(self.clean['on_done'], 1)
        self.assertEqual(self.clean['toasts'][-1]['msg'], '3 recordings scheduled, 1 skipped.')

    def test_a_failure_stays_on_screen_by_name(self):
        """Principle 1: the ones that worked are scheduled, and the one that did not is
        named with its reason where it cannot scroll away."""
        self.assertTrue(self.partial['modal_open_after'])
        self.assertEqual(self.partial['on_done'], 1)
        self.assertEqual(len(self.partial['failed_rows']), 1)
        self.assertIn('Beta', self.partial['failed_rows'][0])
        self.assertIn('disk on fire', self.partial['failed_rows'][0])
        self.assertFalse(self.partial['submit_after'], 'nothing left to schedule')
        self.assertEqual(self.partial['toasts'][-1]['type'], 'error')


if __name__ == '__main__':
    unittest.main()
