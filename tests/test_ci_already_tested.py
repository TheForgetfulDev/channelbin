"""The suite runs once per tree on GitHub (dev/changelog/1701).

`.github/scripts/already-tested.sh` decides whether a push to main needs the suite, and runs
here against a stand-in `gh` that answers from canned API replies through the real `jq`, so its
queries are checked along with its logic. The workflow itself only runs on GitHub.
"""
import json
import os
import shutil
import subprocess
import tempfile
import unittest

import yaml

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPT = os.path.join(ROOT, '.github', 'scripts', 'already-tested.sh')
WORKFLOW = os.path.join(ROOT, '.github', 'workflows', 'tests.yml')

REPO = 'owner/app'
MAIN = 'a' * 40
HEAD = 'b' * 40
OTHER = 'c' * 40
RUN_URL = 'https://github.com/owner/app/actions/runs/7'

# A stand-in for `gh api PATH --jq EXPR`: answers PATH from the JSON file of replies, through
# jq -r as gh's own --jq prints, and exits 1 like gh does on a path it has no answer for.
FAKE_GH = r'''#!/usr/bin/env bash
[[ "$1" == api ]] || exit 2
path="$2"; expr="."
[[ "${3-}" == --jq ]] && expr="$4"
reply="$(jq -c --arg p "$path" '.[$p] // empty' "$FAKE_GH_REPLIES")"
if [[ -z "$reply" ]]; then echo "gh: Not Found (HTTP 404)" >&2; exit 1; fi
printf '%s' "$reply" | jq -r "$expr"
'''


def runs_path(head):
    return ('repos/%s/actions/workflows/tests.yml/runs?head_sha=%s'
            '&event=pull_request&status=success' % (REPO, head))


@unittest.skipUnless(os.path.exists(SCRIPT), 'no .github/scripts in this checkout')
@unittest.skipUnless(shutil.which('bash') and shutil.which('jq'), 'bash or jq is not on PATH')
class AlreadyTestedScriptTests(unittest.TestCase):

    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dir, True)
        gh = os.path.join(self.dir, 'gh')
        with open(gh, 'w', encoding='utf-8') as handle:
            handle.write(FAKE_GH)
        os.chmod(gh, 0o755)

    def answer(self, replies):
        path = os.path.join(self.dir, 'replies.json')
        with open(path, 'w', encoding='utf-8') as handle:
            json.dump(replies, handle)
        env = dict(os.environ, GITHUB_REPOSITORY=REPO, GITHUB_SHA=MAIN, FAKE_GH_REPLIES=path,
                   PATH=self.dir + os.pathsep + os.environ['PATH'])
        done = subprocess.run(['bash', SCRIPT], env=env, capture_output=True, text=True,
                              timeout=30)
        self.assertEqual(0, done.returncode, done.stderr)
        return done.stdout.strip(), done.stderr

    def replies(self, head_tree='t1', merged=True, run=True):
        return {
            'repos/%s/git/commits/%s' % (REPO, MAIN): {'tree': {'sha': 't1'}},
            'repos/%s/git/commits/%s' % (REPO, HEAD): {'tree': {'sha': head_tree}},
            'repos/%s/commits/%s/pulls' % (REPO, MAIN): [
                {'number': 5, 'head': {'sha': HEAD},
                 'merged_at': '2026-11-23T21:15:46Z' if merged else None}],
            runs_path(HEAD): {'total_count': int(run),
                              'workflow_runs': [{'html_url': RUN_URL}] if run else []},
        }

    def test_a_tree_its_pull_request_run_passed_names_that_run(self):
        self.assertEqual(RUN_URL, self.answer(self.replies())[0])

    def test_a_tree_that_differs_from_the_pull_requests_runs_the_suite(self):
        """Another pull request landed in between, so main holds a tree no run saw."""
        self.assertEqual('', self.answer(self.replies(head_tree='t2'))[0])

    def test_no_passing_pull_request_run_runs_the_suite(self):
        self.assertEqual('', self.answer(self.replies(run=False))[0])

    def test_a_pull_request_that_was_never_merged_does_not_count(self):
        self.assertEqual('', self.answer(self.replies(merged=False))[0])

    def test_a_commit_no_pull_request_brought_runs_the_suite(self):
        replies = self.replies()
        replies['repos/%s/commits/%s/pulls' % (REPO, MAIN)] = []
        self.assertEqual('', self.answer(replies)[0])

    def test_the_matching_pull_request_is_found_among_several(self):
        replies = self.replies()
        replies['repos/%s/commits/%s/pulls' % (REPO, MAIN)].insert(
            0, {'number': 4, 'head': {'sha': OTHER}, 'merged_at': '2026-11-22T10:00:00Z'})
        replies['repos/%s/git/commits/%s' % (REPO, OTHER)] = {'tree': {'sha': 't9'}}
        self.assertEqual(RUN_URL, self.answer(replies)[0])

    def test_github_not_answering_runs_the_suite_and_says_why(self):
        """A failed lookup is never read as "already tested"."""
        out, err = self.answer({})
        self.assertEqual('', out)
        self.assertIn('so the suite runs', err)
        replies = self.replies()
        del replies['repos/%s/commits/%s/pulls' % (REPO, MAIN)]
        out, err = self.answer(replies)
        self.assertEqual('', out)
        self.assertIn('so the suite runs', err)


@unittest.skipUnless(os.path.exists(WORKFLOW), 'no .github/workflows in this checkout')
class RunsOncePerTreeWorkflowTests(unittest.TestCase):

    def setUp(self):
        with open(WORKFLOW, encoding='utf-8') as handle:
            self.doc = yaml.safe_load(handle)
        # YAML 1.1 reads a bare `on:` key as true.
        self.on = self.doc.get('on', self.doc.get(True))
        self.jobs = self.doc['jobs']

    def test_a_push_runs_only_on_main_and_never_for_a_tag(self):
        """A push to any other branch repeats its pull request's run, and a tag sits on a
        main commit that was already checked."""
        self.assertEqual({'branches': ['main']}, self.on['push'])
        self.assertIn('pull_request', self.on)

    def test_the_required_check_keeps_its_name_and_runs_unless_the_tree_passed(self):
        """Branch protection requires a check named run_tests on every pull request."""
        job = self.jobs['run_tests']
        self.assertEqual('already_tested', job['needs'])
        self.assertIn('!cancelled()', job['if'])
        self.assertIn("needs.already_tested.outputs.run == ''", job['if'])

    def test_the_lookup_runs_only_on_a_push_and_only_reads(self):
        job = self.jobs['already_tested']
        self.assertEqual("github.event_name == 'push'", job['if'])
        self.assertEqual({'read'}, set(job['permissions'].values()))
        self.assertIn('already-tested.sh', json.dumps(job['steps']))

    def test_only_a_pull_requests_superseded_run_is_cancelled(self):
        group = self.doc['concurrency']['group']
        self.assertIn('github.event.pull_request.number', group)
        self.assertIn('github.run_id', group)


if __name__ == '__main__':
    unittest.main()
