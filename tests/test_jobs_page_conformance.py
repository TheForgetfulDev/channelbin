"""The Scheduled Jobs page against the design that produced it.

Chunk 5 part 2b of the fableUI rollout (dev/changelog/446) converted /jobs onto
the generic components - it has no DESIGN.md section of its own, so what it must
obey is sections 3.2, 3.4, 3.6, 3.10 and 3.14. Each case here is a decision a
careless edit would quietly undo:

  * The list is `.tbl` inside a `.card` inside `.table-scroll` (3.2), not the
    bespoke `.jobs-table` this page carried.
  * Overlap is a dot plus a left EDGE, never a row tint (3.4). The edge is an
    inset shadow rather than a border, because a border on a `td` moves that
    cell's content while its neighbours stay put.
  * Every job type gets an explicit branch. A type added to _build_job_list must
    be named here rather than landing in a silent `else` - CLAUDE.md's
    "states are enumerated" rule, which this page's old chain violated by
    rendering "One-off" for anything unrecognized.
  * Row actions live behind a kebab (3.6), and a job with no actions renders no
    empty menu.
  * Nothing on the page hides data at a breakpoint. The version this replaced
    display:none'd columns 2 and 4 below 600px, so a phone was shown a table
    with two columns silently missing.

The page's JavaScript moved to static/js/jobs.js in the same change, and the
three confirm() prompts became modals - see tests below for why that mattered:
two of them meant "run the job" when you pressed Cancel.

Runs against a throwaway temp SQLite DB - never the live dvr.db.
  python3 -m unittest tests.test_jobs_page_conformance
"""
import os
import re
import unittest
from unittest import mock

from tests.support.app import make_test_app

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

JOBS = [
    {'id': 'start_12', 'display_name': 'Record: The Bear', 'type': 'one_off',
     'next_run_et': 'Aug 03, 2026 09:00 PM', 'next_run_relative': 'in 2 hours',
     'stop_run_et': 'Aug 03, 2026 10:00 PM', 'schedule_description': None,
     'edit_url': '/recordings/12', 'cancel_url': '/api/jobs/cancel/12',
     'overlap': 'red'},
    {'id': 'sync_1', 'display_name': 'Account sync: provider-one', 'type': 'recurring',
     'next_run_et': 'Aug 04, 2026 03:00 AM', 'next_run_relative': 'in 8 hours',
     'stop_run_et': None, 'schedule_description': 'every 12 hours',
     'edit_url': '/settings?q=sync', 'run_url': '/api/accounts/1/sync',
     'skip_url': '/api/jobs/skip/sync_1', 'overlap': 'yellow'},
    {'id': 'active_9', 'display_name': 'Health check: Sports HD', 'type': 'active',
     'next_run_et': 'running', 'next_run_relative': 'now', 'stop_run_et': None,
     'schedule_description': 'running now', 'edit_url': None, 'overlap': 'green'},
    {'id': 'post_script', 'display_name': 'Post-processing script', 'type': 'on_event',
     'next_run_et': 'after each recording', 'next_run_relative': '', 'stop_run_et': None,
     'schedule_description': 'Runs after each recording finishes post-processing',
     'edit_url': '/settings?q=post_script', 'run_url': '/api/jobs/run-post-script',
     'run_keep_schedule_prompt': True, 'overlap': 'green'},
]


def _tr_rows(html):
    body = html.split('<tbody>')[1].split('</tbody>')[0]
    return re.findall(r'<tr\b.*?</tr>', body, re.S)


class JobsPageConformanceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # One app and one render for the whole class: every case reads the markup and
        # none rewrites the state it was rendered from (dev/changelog/979).
        cls.t = make_test_app()
        cls.client = cls.t.app.test_client()
        cls.patcher = mock.patch('app.routes.jobs._build_job_list', return_value=JOBS)
        cls.patcher.start()
        cls.html = cls.client.get('/jobs').get_data(as_text=True)

    @classmethod
    def tearDownClass(cls):
        cls.patcher.stop()
        cls.t.cleanup()

    def test_the_page_renders(self):
        self.assertEqual(self.client.get('/jobs').status_code, 200)

    def test_page_has_exactly_one_h1(self):
        """DESIGN.md 3.10."""
        h1s = re.findall(r'<h1[^>]*>(.*?)</h1>', self.html, re.S)
        self.assertEqual(len(h1s), 1)
        self.assertIn('Scheduled Jobs', h1s[0])

    def test_the_table_is_the_shared_component_in_a_card(self):
        """DESIGN.md 3.2 - the bespoke .jobs-table and .jobs-table-wrap are gone."""
        self.assertIn('<table class="tbl">', self.html)
        self.assertRegex(self.html, r'<div class="card-body table-scroll">\s*<table class="tbl">')
        self.assertNotIn('jobs-table', self.html)

    def test_overlap_is_an_edge_and_a_dot_not_a_row_tint(self):
        """DESIGN.md 3.4. The page previously set a tinted `background` on the
        `tr` for red and yellow, which is the status tint the section rules out
        and which also fights `.tbl tbody tr:hover`."""
        style = re.search(r'<style>(.*?)</style>', self.html, re.S).group(1)
        rules = re.findall(r'tr\[data-overlap="(\w+)"\] td:first-child\s*{([^}]*)}', style)
        self.assertEqual(sorted(k for k, _ in rules), ['red', 'yellow'])
        for key, decl in rules:
            self.assertIn('inset', decl, key)
            self.assertNotIn('background', decl, key)
        for row in _tr_rows(self.html):
            self.assertRegex(row, r'data-overlap="(red|yellow|green)"')
            self.assertRegex(row, r'<span class="jb-dot (red|yellow|green) tip-plain"')

    def test_every_overlap_state_explains_itself(self):
        """CLAUDE.md principle 1 - a colored dot the user cannot decode is a
        number they cannot explain. Each dot carries its own tooltip AND the
        legend stays on the page."""
        for row in _tr_rows(self.html):
            tip = re.search(r'<span class="jb-dot \w+ tip-plain"\s*data-tip="([^"]*)"', row)
            self.assertIsNotNone(tip, row[:120])
            self.assertTrue(tip.group(1).strip())
        self.assertIn('jb-legend', self.html)

    def test_every_job_type_has_its_own_branch(self):
        """CLAUDE.md: a branch chain over an enum names every state. The old
        chain ended in a bare `else` that rendered "One-off", so an unrecognized
        type was displayed as a real one."""
        rows = _tr_rows(self.html)
        badges = [re.search(r'<span class="badge (b-\w+)">([^<]*)</span>', r).groups()
                  for r in rows]
        self.assertEqual(badges, [('b-done', 'One-off'), ('b-sched', 'Recurring'),
                                  ('b-warn', 'Active'), ('b-paused', 'Per Recording')])

    def test_an_unknown_job_type_is_not_rendered_as_a_real_one(self):
        odd = dict(JOBS[0], id='mystery_1', type='not_a_real_type',
                   display_name='Something new')
        with mock.patch('app.routes.jobs._build_job_list', return_value=[odd]):
            html = self.client.get('/jobs').get_data(as_text=True)
        badge = re.search(r'<span class="badge (b-\w+)">([^<]*)</span>', html).groups()
        self.assertEqual(badge, ('b-abort', 'not_a_real_type'))

    def test_row_actions_are_behind_a_kebab(self):
        """DESIGN.md 3.6 - the actions cell used to hold up to four loose buttons."""
        rows = _tr_rows(self.html)
        with_actions = [r for r in rows if 'data-menu' in r]
        self.assertEqual(len(with_actions), 3)
        for row in with_actions:
            self.assertEqual(row.count('data-menu'), 1)
            self.assertIn('<div class="menu pop-left">', row)

    def test_a_job_with_no_actions_renders_no_menu(self):
        """An empty popover is a control that does nothing when clicked."""
        row = [r for r in _tr_rows(self.html) if 'Health check: Sports HD' in r][0]
        self.assertNotIn('data-menu', row)
        self.assertIn('<span class="muted">-</span>', row)

    def test_the_run_action_carries_what_the_prompt_needs(self):
        """The Run modal names the next scheduled run and offers keeping or
        dropping it. Both facts travel on the trigger, so the modal cannot be
        built from a job whose data attributes went missing."""
        sync = [r for r in _tr_rows(self.html) if 'Account sync' in r][0]
        self.assertIn('data-act="run"', sync)
        self.assertIn('data-skip-url="/api/jobs/skip/sync_1"', sync)
        self.assertIn('data-next-run="Aug 04, 2026 03:00 AM (in 8 hours)"', sync)
        self.assertIn('data-name="Account sync: provider-one"', sync)

        post = [r for r in _tr_rows(self.html) if 'Post-processing' in r][0]
        self.assertIn('data-keep-prompt="1"', post)

    def test_no_blocking_browser_dialog_survives(self):
        """DESIGN.md 3.12/4: confirm() and alert() are gone, and the modal's
        buttons are verb-named. The two prompts this replaced overloaded Cancel
        to mean "run it, and drop the schedule", which is the opposite of what a
        Cancel button says."""
        with open(os.path.join(REPO, 'static/js/jobs.js'), encoding='utf-8') as fh:
            js = fh.read()
        # The comments explain what was removed and name it, so they are stripped
        # before the assertion - otherwise the file documenting the fix fails it.
        code = re.sub(r'//[^\n]*', '', re.sub(r'/\*.*?\*/', '', js, flags=re.S))
        self.assertNotIn('confirm(', code)
        self.assertNotIn('alert(', code)
        self.assertIn('buildModal(', js)
        for label in ('Run and keep it', 'Run and drop the schedule',
                      'Run and skip the next one', 'Run and keep the next one',
                      'Skip it', 'Cancel the run'):
            self.assertIn(label, js)

    def test_the_page_script_is_a_file_not_an_inline_block(self):
        self.assertIn('js/jobs.js', self.html)
        self.assertNotIn('function runJob', self.html)

    def test_nothing_is_hidden_at_a_breakpoint(self):
        """The old media query display:none'd the Type and Schedule columns below
        600px. .table-scroll is how a wide table meets a phone (3.2); hiding two
        columns of a five-column table is data the user is never told is missing."""
        style = re.search(r'<style>(.*?)</style>', self.html, re.S).group(1)
        self.assertNotIn('display: none', style)
        self.assertNotIn('nth-child', style)

    def test_the_empty_state_is_the_shared_component_with_a_way_out(self):
        """DESIGN.md 3.14 - one line of text plus a CTA when an action exists."""
        with mock.patch('app.routes.jobs._build_job_list', return_value=[]):
            html = self.client.get('/jobs').get_data(as_text=True)
        self.assertIn('<div class="empty-state">', html)
        self.assertIn('btn btn-primary', html.split('<div class="empty-state">')[1])

    def test_no_em_dash_in_the_template_or_its_script(self):
        """CLAUDE.md: no em dashes. The old actions cell rendered two."""
        for path in ('templates/jobs.html', 'static/js/jobs.js'):
            with open(os.path.join(REPO, path), encoding='utf-8') as fh:
                self.assertNotIn('—', fh.read(), path)
        self.assertNotIn('—', self.html.split('<tbody>')[1].split('</tbody>')[0])
