"""The issue-title workflow: an issue's title starts with its kind, from its label.

`.github/scripts/issue-title.sh` is the whole decision, kept out of the workflow file so it
can be run here. The workflow itself only runs on GitHub (dev/changelog/1650).
"""
import os
import re
import shutil
import subprocess
import unittest

import yaml

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPT = os.path.join(ROOT, '.github', 'scripts', 'issue-title.sh')
WORKFLOW = os.path.join(ROOT, '.github', 'workflows', 'issue-title.yml')
TEMPLATES = os.path.join(ROOT, '.github', 'ISSUE_TEMPLATE')


@unittest.skipUnless(os.path.exists(SCRIPT), 'no .github/scripts in this checkout')
@unittest.skipUnless(shutil.which('bash'), 'bash is not on PATH')
class IssueTitleScriptTests(unittest.TestCase):

    def want(self, title, labels):
        env = dict(os.environ, ISSUE_TITLE=title, ISSUE_LABELS=labels)
        done = subprocess.run(['bash', SCRIPT], env=env, capture_output=True, text=True,
                              timeout=30, check=True)
        self.assertEqual('', done.stderr)
        return done.stdout[:-1]

    def test_a_missing_prefix_is_added_from_the_label(self):
        self.assertEqual('[bug]: Guide is blank', self.want('Guide is blank', 'bug'))
        self.assertEqual('[feature]: Dark mode', self.want('Dark mode', 'enhancement'))
        self.assertEqual('[bug]: x', self.want('x', 'accessibility,bug'))

    def test_a_correct_title_is_left_exactly_as_it_is(self):
        for title, labels in (('[bug]: Guide is blank', 'bug'),
                              ('[feature]: Dark mode', 'enhancement')):
            self.assertEqual(title, self.want(title, labels))

    def test_another_spelling_of_the_prefix_is_replaced_not_stacked(self):
        for typed in ('[Bug]: Guide is blank', '[BUG] Guide is blank', '[bug]Guide is blank',
                      '  [bug] :  Guide is blank', '[Feature]: Guide is blank'):
            self.assertEqual('[bug]: Guide is blank', self.want(typed, 'bug'), typed)

    def test_an_issue_with_no_known_label_keeps_its_title(self):
        for labels in ('', 'question', 'debug', 'bugs'):
            self.assertEqual('[Bug] whatever', self.want('[Bug] whatever', labels), labels)

    def test_the_title_is_text_and_is_never_run_or_expanded(self):
        for title in ('$(touch /nonexistent/x) `id` $HOME', '-n -e \\n * ?', '"; exit 3; "',
                      'a [bug] in the middle: stays'):
            self.assertEqual('[bug]: ' + title, self.want(title, 'bug'), title)


@unittest.skipUnless(os.path.exists(WORKFLOW), 'no .github/workflows in this checkout')
class IssueTitleWorkflowTests(unittest.TestCase):

    def setUp(self):
        with open(WORKFLOW, encoding='utf-8') as handle:
            self.text = handle.read()
        self.doc = yaml.safe_load(self.text)

    def test_the_title_reaches_the_shell_only_through_the_environment(self):
        """An expression inside `run:` is pasted into the script before the shell reads it,
        so a title there is a command a stranger wrote."""
        for step in self.doc['jobs']['prefix']['steps']:
            self.assertNotIn('${{', step.get('run', ''))

    def test_it_can_write_issues_and_nothing_else(self):
        self.assertEqual({'contents': 'read', 'issues': 'write'}, self.doc['permissions'])

    def test_every_form_prefills_the_prefix_its_label_maps_to(self):
        """The form's default title and the script's map are two spellings of one fact."""
        with open(SCRIPT, encoding='utf-8') as handle:
            kinds = dict(re.findall(r'^\s+"([a-z ]+):([a-z]+)"$', handle.read(), re.M))
        self.assertTrue(kinds)
        seen = set()
        for name in sorted(os.listdir(TEMPLATES)):
            if name == 'config.yml':
                continue
            with open(os.path.join(TEMPLATES, name), encoding='utf-8') as handle:
                form = yaml.safe_load(handle)
            label, = form['labels']
            self.assertIn(label, kinds, '%s has a label the workflow does not know' % name)
            self.assertEqual('[%s]: ' % kinds[label], form['title'], name)
            seen.add(label)
        self.assertEqual(set(kinds), seen, 'a kind in the script has no form')


if __name__ == '__main__':
    unittest.main()
