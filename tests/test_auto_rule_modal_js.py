"""The "Create rule from this search" dialog (static/js/auto-rule-modal.js,
DESIGN-autoschedule.md §8), driven in jsdom.

What it must do: send the search on screen and every setting the API reads, in the shape
app/auto_rules.py::read_rule_values accepts; keep a refusal on screen inside the dialog,
because the sentence says what to change on the search behind it; refuse half of "at most X
within Y hours" before asking; and after a save, say what the rule's first check did - or
what it is waiting for, or that the guide had nothing to search - rather than closing on a
rule that silently scheduled nothing (principle 1).

Its verdict wording comes from one table (routes/auto_rules.py::VERDICT_PHRASES), which
every save, resume and check toast reads; the JS twin is held to the Python one here. The
dialog's edit mode is driven over a real rule's page in tests/test_rule_page_conformance.py.

`tests/support/auto_rule_modal.mjs` drives it and reports observations; every assertion
lives here. jsdom computes no layout, so the limit row's width and 375px are browser work.
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.database import RULE_MATCH_ACTIONS  # noqa: E402
from app.routes.auto_rules import VERDICT_PHRASES, verdict_phrases  # noqa: E402

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HARNESS = os.path.join(REPO, 'tests', 'support', 'auto_rule_modal.mjs')
JSDOM = os.path.join(REPO, 'node_modules', 'jsdom')

COUNTS = [
    {},
    {'scheduled': 1, 'skipped_repeat': 2},
    {action: 1 for action in RULE_MATCH_ACTIONS},
    {action: n + 2 for n, action in enumerate(RULE_MATCH_ACTIONS)},
    {'skipped_cap': 0, 'user_modified': 1},
]

_RESULT = None


def _observe():
    global _RESULT
    if _RESULT is None:
        with tempfile.TemporaryDirectory() as tmp:
            fixture = os.path.join(tmp, 'fixture.json')
            with open(fixture, 'w', encoding='utf-8') as fh:
                json.dump({'phrases': verdict_phrases(), 'counts': COUNTS}, fh)
            proc = subprocess.run([shutil.which('node'), HARNESS, REPO, fixture],
                                  capture_output=True, text=True, cwd=REPO, timeout=120)
        if proc.returncode != 0:
            raise AssertionError(f'harness failed:\n{proc.stdout}\n{proc.stderr[-4000:]}')
        _RESULT = json.loads(proc.stdout)
    return _RESULT


@unittest.skipIf(shutil.which('node') is None, 'node not installed')
@unittest.skipIf(not os.path.isdir(JSDOM), 'jsdom not installed (npm install)')
class AutoRuleModalTests(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.obs = _observe()

    def test_no_script_errors(self):
        for name, run in self.obs.items():
            if isinstance(run, dict) and 'errors' in run:
                self.assertEqual(run['errors'], [], name)

    def test_the_dialog_opens_seeded_from_the_search(self):
        first = self.obs['clean']['first']
        self.assertEqual(first['title'], 'Create rule from this search')
        self.assertEqual(first['name'], 'nascar')
        self.assertEqual(first['profile_options'],
                         [['', "Each showing's default profile"], ['5', 'Padded']])
        self.assertTrue(first['repeats_checked'], 'skip repeats is on by default (§3.4)')
        self.assertEqual(first['cap_placeholder'], 'Default (10)')
        self.assertIn('Search: "nascar".', first['intro'])
        self.assertTrue(first['error_hidden_at_open'])

    def test_the_footer_is_cancel_then_the_action_verb(self):
        """DESIGN.md 3.12 / 4: Cancel dismisses, the primary names the action - not a bare
        Save+noun, which section 4 rules out."""
        self.assertEqual(self.obs['clean']['first']['footer'],
                         [['Cancel', 'btn'], ['Create rule', 'btn btn-primary']])

    def test_the_hints_say_what_skip_repeats_and_the_limit_actually_do(self):
        text = self.obs['clean']['first']['text']
        self.assertIn('episode title', text)
        self.assertIn('Showings without an episode title are never skipped as repeats', text)
        self.assertIn('shorter than the shortest real gap between two events', text)
        self.assertIn('a weekly event does not start at the same hour every week', text)
        self.assertIn('let Skip repeats handle the replays', text)
        self.assertIn('0 means no cap', text)

    def test_save_posts_every_setting_the_api_reads(self):
        sent = self.obs['clean']['sent']
        self.assertEqual(len(sent), 1)
        self.assertEqual(sent[0]['url'], '/api/auto-rules')
        self.assertEqual(sent[0]['method'], 'POST')
        self.assertEqual(sent[0]['body'], {
            'name': 'NASCAR Cup', 'search': 'q=nascar&grain=airings', 'profile_id': 5,
            'skip_repeats': True, 'limit_count': 1, 'limit_window_hours': 120,
            'max_upcoming': None,
        })

    def test_untouched_fields_send_the_defaults(self):
        self.assertEqual(self.obs['defaults']['sent'][0]['body'], {
            'name': 'nascar', 'search': 'q=nascar&grain=airings', 'profile_id': None,
            'skip_repeats': True, 'limit_count': None, 'limit_window_hours': None,
            'max_upcoming': None,
        })

    def test_a_clean_save_closes_and_says_what_the_first_check_did(self):
        run = self.obs['clean']
        self.assertFalse(run['open'])
        self.assertEqual(run['saved'], 1)
        toast = run['toasts'][-1]
        self.assertTrue(toast['html'])
        self.assertEqual(toast['type'], 'success')
        self.assertIn('Rule saved. 2 scheduled, 1 repeat skipped.', toast['msg'])
        self.assertIn('<a href="/rules">Rules &rarr;</a>', toast['msg'])

    def test_the_toast_links_the_new_rules_page_when_the_answer_names_it(self):
        msg = self.obs['with_page']['toasts'][-1]['msg']
        self.assertIn('<a href="/rules/4">Cup &lt;Series&gt; &rarr;</a>', msg)
        self.assertNotIn('Rules &rarr;', msg)

    def test_a_refusal_stays_in_the_dialog_with_the_servers_sentence(self):
        run = self.obs['refused']
        self.assertTrue(run['open'])
        self.assertTrue(run['error_shown'])
        self.assertIn('A rule cannot use "On now"', run['error'])
        self.assertEqual(run['toasts'], [])
        self.assertEqual(run['saved'], 0)
        self.assertFalse(run['button_disabled'], 'the user can fix the search and try again')
        self.assertEqual(run['button_label'], 'Create rule')
        body = run['sent'][0]['body']
        self.assertFalse(body['skip_repeats'])
        self.assertEqual(body['max_upcoming'], 3)

    def test_half_a_limit_is_refused_before_asking(self):
        run = self.obs['half_limit']
        self.assertEqual(run['sent'], [])
        self.assertTrue(run['open'])
        self.assertTrue(run['error_shown'])
        self.assertIn('both halves', run['error'])

    def test_a_blank_name_is_refused_before_asking(self):
        run = self.obs['no_name']
        self.assertEqual(run['sent'], [])
        self.assertTrue(run['error_shown'])
        self.assertIn('name', run['error'])

    def test_a_queued_check_says_what_it_waits_for(self):
        run = self.obs['queued']
        self.assertFalse(run['open'])
        toast = run['toasts'][-1]
        self.assertEqual(toast['type'], 'info')
        self.assertIn('Rule saved.', toast['msg'])
        self.assertIn('It has not been checked yet: an account sync is already running (Acct).',
                      toast['msg'])

    def test_an_empty_guide_is_not_reported_as_nothing_matched(self):
        """§4.5: a null guide reach after a check means the rule searched nothing at all,
        which must not read like a search that simply found nothing."""
        empty = self.obs['empty_guide']['toasts'][-1]
        self.assertEqual(empty['type'], 'warning')
        self.assertIn('the guide holds no upcoming listings', empty['msg'])
        nothing = self.obs['nothing_matched']['toasts'][-1]
        self.assertEqual(nothing['type'], 'success')
        self.assertIn('Nothing upcoming matches it yet.', nothing['msg'])

    def test_a_check_that_stopped_says_why(self):
        toast = self.obs['check_error']['toasts'][-1]
        self.assertEqual(toast['type'], 'error')
        self.assertIn('Its check stopped: The search names a tag that is gone', toast['msg'])

    def test_a_check_that_ran_reports_what_it_scheduled_before_its_problems(self):
        """A showing the check could not place is not a stopped check: the toast says what it
        DID schedule, then the problem, as a warning - never "Its check stopped" in red
        (dev/changelog/1186)."""
        toast = self.obs['placed_with_problems']['toasts'][-1]
        self.assertEqual(toast['type'], 'warning')
        self.assertIn('2 scheduled', toast['msg'])
        self.assertIn('could not be placed', toast['msg'])
        self.assertNotIn('stopped', toast['msg'])

    def test_a_recheck_of_decided_matches_does_not_say_nothing_matches(self):
        """A rule whose upcoming matches were all scheduled by an earlier check matches them
        still; "Nothing upcoming matches it" would read as a broken rule (dev/changelog/1186)."""
        toast = self.obs['all_already_scheduled']['toasts'][-1]
        self.assertIn('3 already scheduled', toast['msg'])
        self.assertNotIn('Nothing upcoming matches', toast['msg'])

    def test_a_check_that_moved_a_recording_says_so(self):
        """A check that followed a program the guide moved says it moved a recording -
        retrack is never silent (dev/changelog/1188). The two counts are separate: a moved
        recording is not also counted as already scheduled."""
        toast = self.obs['moved_with_its_program']['toasts'][-1]
        self.assertEqual(toast['type'], 'success')
        self.assertIn('1 already scheduled, 1 moved to a new time.', toast['msg'])

    def test_the_js_wording_is_the_phrase_tables(self):
        """The toasts word counts exactly as VERDICT_PHRASES says, in its order."""
        def words(counts):
            return ', '.join((one if counts[a] == 1 else many).format(n=counts[a])
                             for a, one, many in VERDICT_PHRASES if counts.get(a))
        self.assertEqual(self.obs['counts_text'], [words(c) for c in COUNTS])
        self.assertEqual(self.obs['counts_text'][1], '1 scheduled, 2 repeats skipped')

    def test_number_boxes_never_round_what_was_typed(self):
        """A non-integer goes to the server as typed, so its own sentence refuses it -
        Python's int(1.5) would otherwise quietly store 1."""
        self.assertEqual(self.obs['numbers'], [None, None, 7, 0, '1.5', '-2', None])


if __name__ == '__main__':
    unittest.main()
