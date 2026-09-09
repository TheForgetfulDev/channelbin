"""group-detail.js's run-now action against a real confirm/cancel choice.

dev/changelog/555. jobs.js got this fix in dev/changelog/446 (a confirm()
overloading OK/Cancel to decide the fate of the *next scheduled run*, not
whether to run at all - Cancel meant "run it, and drop the schedule"). This
guards the equivalent run-now case in group-detail.js, which still used the
same overloaded confirm() until now.

    python3 -m unittest tests.test_group_detail_run_now_modal
"""
import os
import re
import unittest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class RunNowModalTests(unittest.TestCase):
    def setUp(self):
        with open(os.path.join(REPO, 'static/js/group-detail.js'), encoding='utf-8') as fh:
            self.js = fh.read()
        case_match = re.search(
            r"case 'run-now': \{(.*?)\n      \}", self.js, re.S)
        self.assertIsNotNone(case_match, "run-now case not found in group-detail.js")
        self.case_body = case_match.group(1)

    def test_no_blocking_browser_dialog(self):
        """The run-now case must not fall back to a native confirm() - Cancel
        in the old dialog silently ran the job anyway."""
        code = re.sub(r'//[^\n]*', '', self.case_body)
        self.assertNotIn('confirm(', code)
        self.assertIn('buildModal(', self.case_body)

    def test_cancel_is_a_real_option_that_does_not_run_the_job(self):
        """Three explicit buttons - Cancel must not appear alongside an
        onClick that posts to the start endpoint."""
        footer_match = re.search(r'footer:\s*\[(.*?)\],\s*\}\);', self.case_body, re.S)
        self.assertIsNotNone(footer_match)
        footer = footer_match.group(1)
        cancel_entry = re.search(r"\{\s*label:\s*'Cancel'[^}]*\}", footer)
        self.assertIsNotNone(cancel_entry)
        self.assertNotIn('jobApi', cancel_entry.group(0))
        self.assertNotIn('postAndReload', cancel_entry.group(0))

    def test_both_run_outcomes_are_named_verbs(self):
        for label in ('Run and drop the schedule', 'Run and keep it', 'Cancel'):
            self.assertIn(label, self.case_body)

    def test_both_run_outcomes_hit_the_start_endpoint_with_the_right_flag(self):
        self.assertIn("postAndReload(jobApi('start'), 'POST', { keep_schedule: false })", self.case_body)
        self.assertIn("postAndReload(jobApi('start'), 'POST', { keep_schedule: true })", self.case_body)


if __name__ == '__main__':
    unittest.main()
