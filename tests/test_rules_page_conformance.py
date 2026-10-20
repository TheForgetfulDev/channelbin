"""The Rules list (/rules, templates/rules.html + static/js/rules.js) against the design that
produced it: mockup 42 and DESIGN-autoschedule.md §8 for what it must say, DESIGN.md 3.1,
3.6, 3.12, 3.14 and 4 for how (dev/changelog/1189). What is asserted:

  * It is reached from the nav as "Auto-record", right after Recordings, in both shells, and
    is active there and on a rule's own page.
  * The list is the recordings list's card-table hybrid (.list-head + .rows .row) inside the
    named .rl-list container, one row per rule, each opening its rule's page.
  * Each row says what the rule is doing: one state badge (On, At its cap, Not running,
    Paused, Not checked) with the matching row edge, the next recording or why there is none,
    how many recordings it holds scheduled and when it was last checked. A check that ran but
    could not place a showing is a warning line on a running rule, never "Not running".
  * The kebab offers the same actions as the rule's page: Pause or Resume, Edit search (the
    Programs search in the rule's errand), Edit settings, Open search (the stored spelling,
    the rule's exact preview), Check now (not on a paused rule) and Delete rule.
  * + New rule is the primary action, in the head and in the empty state, and opens the
    Programs search in the new-rule errand.
  * A strip above the list says how far the guide reaches and what refreshes it.
  * Delete names only the rule; calling off its scheduled recordings is Cancel, offered as a
    choice that defaults to keeping them. Pause, Resume and Check now say what they did.

The row actions are driven in jsdom over the page as the app rendered it
(tests/support/rules_page.mjs). Runs against a throwaway temp SQLite DB - never the live
dvr.db.
  python3 -m unittest tests.test_rules_page_conformance
"""
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tests.support.app import make_test_app  # noqa: E402
from tests.support import seed  # noqa: E402
from app import auto_rules, db  # noqa: E402
from app.database import (  # noqa: E402
    AutoRule, RULE_MATCH_ACTIONS, RULE_MATCH_SCHEDULED, RULE_MATCH_SKIPPED_CAP,
    RecordingProfile, RuleMatch,
)
from app.routes.auto_rules import (  # noqa: E402
    VERDICT_PHRASES, verdict_phrases,
)

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HARNESS = os.path.join(REPO, 'tests', 'support', 'rules_page.mjs')
JSDOM = os.path.join(REPO, 'node_modules', 'jsdom')

SEARCH = 'grain=airings&q=nascar'


def _read(rel):
    with open(os.path.join(REPO, rel), encoding='utf-8') as fh:
        return fh.read()


def _unescaped(html):
    return (html.replace('&#34;', '"').replace('&quot;', '"').replace('&#39;', "'")
            .replace('&amp;', '&'))


def _text(fragment):
    return ' '.join(_unescaped(re.sub(r'<[^>]+>', ' ', fragment)).split())


def _rows(html):
    """{rule id: the row's markup}, each row cut at the next row or the list's end."""
    body = html.split('<div class="rows">')[1]
    parts = re.split(r'(?=<div class="row )', body)
    return {int(re.search(r'data-rule="(\d+)"', p).group(1)): p
            for p in parts if p.startswith('<div class="row ')}


def make_rule(name, search=SEARCH, **settings):
    values = auto_rules.read_rule_values(dict({'name': name, 'search': search}, **settings))
    return auto_rules.create_rule(values)


def add_row(rule_id, action, start, recording_id=None, detail=None, title='Show'):
    db.session.add(RuleMatch(
        rule_id=rule_id, title_key=f'{title}-{start.isoformat()}', episode_key='',
        program_title=title, program_start_time=start,
        program_stop_time=start + timedelta(minutes=30), action_taken=action,
        recording_id=recording_id, detail=detail))
    db.session.commit()


def add_recording(rule_id, status, start, name):
    rec = seed.make_recording(status=status, name=name, rule_id=rule_id, start_time=start,
                              stop_time=start + timedelta(minutes=30))
    db.session.commit()
    return rec


def mark_checked(rule_id, reach=None, error=None, problems=None):
    rule = db.session.get(AutoRule, rule_id)
    rule.last_evaluated_at = datetime.utcnow() - timedelta(minutes=5)
    rule.last_guide_reach = reach
    rule.last_error = error
    rule.last_problems = problems
    db.session.commit()


class _Base(unittest.TestCase):
    def setUp(self):
        self.t = make_test_app(csrf=False)
        self.client = self.t.app.test_client()
        self.ctx = self.t.app.app_context()
        self.ctx.push()

    def tearDown(self):
        self.ctx.pop()
        self.t.cleanup()

    def get(self, path='/rules'):
        resp = self.client.get(path)
        self.assertEqual(resp.status_code, 200)
        return resp.get_data(as_text=True)

    def search_url(self, params):
        with self.t.app.test_request_context():
            from flask import url_for
            return url_for('channels.channel_browser') + '?' + params

    def new_rule_url(self):
        with self.t.app.test_request_context():
            from app.routes.channels import airing_search_url
            return airing_search_url(rule='new')


class EmptyPageTests(_Base):

    def setUp(self):
        super().setUp()
        self.html = self.get()

    def test_one_h1_with_the_count(self):
        h1s = re.findall(r'<h1[^>]*>(.*?)</h1>', self.html, re.S)
        self.assertEqual(len(h1s), 1)
        self.assertIn('Auto-record rules', h1s[0])
        self.assertIn('0 rules', h1s[0])

    def test_new_rule_is_the_primary_action_and_opens_the_new_rule_errand(self):
        """DESIGN.md 4: a list page's primary action sits top-right of the header. The URL
        comes from airing_search_url(), never a hand-typed query string."""
        url = self.new_rule_url()
        self.assertIn('grain=airings', url)
        self.assertIn('rule=new', url)
        head = self.html.split('<div class="page-head">')[1].split('</div>')[0]
        self.assertIn(f'<a class="btn btn-primary" href="{url}">+ New rule</a>', _unescaped(head))

    def test_the_empty_state_carries_the_same_action(self):
        """DESIGN.md 3.14: an empty state with an action carries it."""
        state = self.html.split('<div class="empty-state">')[1].split('</div>')[0]
        self.assertIn('No rules yet', state)
        self.assertIn('Programs search', state)
        self.assertIn(f'href="{self.new_rule_url()}">+ New rule</a>', _unescaped(state))
        self.assertNotIn('class="rl-list"', self.html)

    def test_the_guide_strip_shows_with_no_rules(self):
        self.assertIn('id="rules-guide"', self.html)
        self.assertIn('no rule can schedule anything', self.html)


class ListPageTests(_Base):

    def setUp(self):
        super().setUp()
        profile = RecordingProfile(name='Padded', pre_padding_minutes=5, post_padding_minutes=60)
        db.session.add(profile)
        db.session.commit()
        now = datetime.utcnow()
        self.reach = now + timedelta(days=3)
        self.on = make_rule('Busy', profile_id=profile.id)
        rec = add_recording(self.on, 'SCHEDULED', now + timedelta(days=1), 'Busy rec')
        add_recording(self.on, 'SCHEDULED', now + timedelta(days=2), 'Busy rec two')
        add_row(self.on, RULE_MATCH_SCHEDULED, now + timedelta(days=1), rec.id,
                detail='Scheduled on NBC. Scheduled with a warning: the account is full.',
                title='Cup Race')
        mark_checked(self.on, reach=self.reach)
        self.capped = make_rule('Capped', search='grain=airings&q=race')
        add_row(self.capped, RULE_MATCH_SKIPPED_CAP, now + timedelta(days=1))
        mark_checked(self.capped, reach=self.reach)
        self.broken = make_rule('Broken', search='grain=airings&q=cup')
        mark_checked(self.broken, error='Its search names the tag "live", which no longer exists.')
        self.problem = make_rule('Problem', search='grain=airings&q=show')
        mark_checked(self.problem, reach=self.reach, problems='"Show" could not be placed.')
        self.paused = make_rule('Paused one', search='grain=airings&q=indy')
        auto_rules.set_rule_enabled(self.paused, False)
        self.fresh = make_rule('Never checked', search='grain=airings&q=moto')
        self.rules = {r.id: r for r in AutoRule.query.all()}
        self.html = self.get()
        self.rows = _rows(self.html)

    def test_one_h1_with_the_counts(self):
        h1s = re.findall(r'<h1[^>]*>(.*?)</h1>', self.html, re.S)
        self.assertEqual(len(h1s), 1)
        self.assertIn('6 rules', h1s[0])
        self.assertIn('2 recordings scheduled', _text(h1s[0]))

    def test_the_list_is_the_card_table_hybrid_in_its_named_container(self):
        """DESIGN.md 3.1: .list-head + .rows .row, and CLAUDE.md's named-container pattern
        so the row restacks on the list's own width."""
        self.assertRegex(self.html, r'<div class="rl-list">\s*<div class="list-head">')
        head = self.html.split('<div class="list-head">')[1].split('</div>')[0]
        labels = [_text(x) for x in re.findall(r'<span[^>]*>(.*?)</span>', head, re.S)]
        self.assertEqual(labels, ['Rule', 'Status', 'Next recording', 'Scheduled',
                                  'Last checked', ''])
        self.assertEqual(sorted(self.rows), sorted(self.rules))
        css = _read('static/css/style.css')
        self.assertIn('.rl-list { container-type: inline-size; container-name: rl-list; }', css)
        tracks = re.search(r'\.rl-list \.rows \.row \{ grid-template-columns: ([^;]+);', css)
        self.assertNotRegex(tracks.group(1), r'auto|content|min-content|max-content',
                            'CLAUDE.md CSS: fixed lengths and fr only')

    def test_each_row_opens_its_rule_page(self):
        for rid, row in self.rows.items():
            self.assertIn(f'data-href="/rules/{rid}"', row)
            self.assertIn(f'<a class="rl-name" href="/rules/{rid}">', row)

    def badge(self, rid):
        return re.search(r'<span class="rl-status"><span class="badge ([^" ]+)[^>]*>([^<]+)</span>',
                         self.rows[rid]).groups()

    def test_one_state_per_rule_with_its_edge(self):
        want = {self.on: ('b-done', 'On', 'rs-on'), self.capped: ('b-warn', 'At its cap', 'rs-cap'),
                self.broken: ('b-fail', 'Not running', 'rs-broken'),
                self.problem: ('b-done', 'On', 'rs-on'),
                self.paused: ('b-paused', 'Paused', 'rs-paused'),
                self.fresh: ('b-sched', 'Not checked', 'rs-new')}
        for rid, (cls, label, edge) in want.items():
            self.assertEqual(self.badge(rid), (cls, label), self.rules[rid].name)
            self.assertTrue(self.rows[rid].startswith(f'<div class="row {edge}"'),
                            self.rules[rid].name)

    def test_a_check_that_ran_with_a_problem_is_a_warning_not_not_running(self):
        self.assertIn('Its last check could not schedule everything', self.rows[self.problem])
        self.assertNotIn('Its last check could not schedule everything', self.rows[self.broken])

    def test_the_next_recording_cell(self):
        on = _text(self.rows[self.on].split('rl-next')[1].split('rl-meta')[0])
        self.assertIn('Tomorrow', on)
        self.assertIn('Cup Race', on)
        self.assertIn('Scheduled with a warning', on)
        self.assertIn('Nothing scheduled', _text(self.rows[self.paused]))
        self.assertIn('Paused, so nothing new is added', _text(self.rows[self.paused]))
        self.assertIn('Not running until it is fixed', _text(self.rows[self.broken]))
        self.assertIn('Waiting for its first check', _text(self.rows[self.fresh]))
        self.assertIn('Nothing in the guide yet', _text(self.rows[self.problem]))

    def test_scheduled_counts_only_scheduled_recordings(self):
        num = lambda rid: _text(re.search(r'<span class="rl-num">(.*?)</span>\s*<span class="rl-cell"',
                                          self.rows[rid], re.S).group(1))
        self.assertEqual(num(self.on), 'Scheduled 2')
        self.assertEqual(num(self.paused), 'Scheduled 0')
        self.assertIn('data-scheduled="2"', self.rows[self.on])

    def test_last_checked(self):
        self.assertIn('5 minutes ago', _text(self.rows[self.on]))
        self.assertIn('Checked Never', _text(self.rows[self.fresh]))

    def test_the_kebab_offers_the_rule_pages_actions(self):
        """DESIGN.md 3.6 and 4: Pause or Resume, whichever applies; Check now is not offered
        on a paused rule, which is never checked; Delete names the rule."""
        for rid, row in self.rows.items():
            self.assertIn('data-menu', row)
            self.assertIn('<button class="menu-item" data-act="edit">Edit settings&hellip;</button>', row)
            self.assertIn('<button class="menu-item danger" data-act="delete">Delete rule</button>', row)
        self.assertIn('data-act="pause">Pause<', self.rows[self.on])
        self.assertNotIn('data-act="resume"', self.rows[self.on])
        self.assertIn('data-act="resume">Resume<', self.rows[self.paused])
        self.assertIn('data-act="check" disabled>Check now<', self.rows[self.paused])
        self.assertIn('data-act="check">Check now<', self.rows[self.on])

    def test_open_search_is_the_stored_spelling_and_edit_search_adds_the_errand(self):
        """The search IS the rule's preview (§8): Open search is the stored canonical query
        string as written. Edit search is that search plus the `rule=<id>` action context."""
        for rid in self.rows:
            stored = self.rules[rid].search_params
            row = _unescaped(self.rows[rid])
            self.assertIn(f'<a class="menu-item" href="{self.search_url(stored)}">Open search</a>', row)
            edit = re.search(r'<a class="menu-item" href="([^"]+)">Edit search</a>', row).group(1)
            self.assertIn(f'rule={rid}', edit)
            for part in stored.split('&'):
                self.assertIn(part, edit)

    def test_each_row_carries_what_the_edit_dialog_starts_from(self):
        dialog = json.loads(_unescaped(re.search(r'data-dialog="([^"]+)"',
                                                 self.rows[self.on]).group(1)))
        self.assertEqual(dialog['id'], self.on)
        self.assertEqual(dialog['name'], 'Busy')
        self.assertEqual(dialog['search_lines'][0], ['Text', 'nascar'])
        self.assertIn('rule=', dialog['edit_search_url'])

    def test_the_summary_and_its_tooltip_say_what_the_rule_searches_for(self):
        self.assertIn('<span class="rl-sum tip-plain"', self.rows[self.on])
        self.assertIn('nascar', _text(self.rows[self.on].split('rl-sum')[1].split('</span>')[0]))
        self.assertIn('What it searches for:', _unescaped(self.rows[self.on]))

    def test_the_guide_strip(self):
        strip = self.html.split('id="rules-guide"')[1].split('<div class="rl-list">')[0]
        self.assertIn('notice-banner-body', strip)

    def test_the_page_config_carries_what_the_actions_need(self):
        cfg = json.loads(re.search(r'window\.RULES_CONFIG = (\{.*?\});\n', self.html, re.S).group(1))
        self.assertEqual(cfg['phrases'], verdict_phrases())
        self.assertEqual(cfg['apiBase'], '/api/auto-rules/')
        self.assertIn('rule=new', cfg['newRuleUrl'])
        self.assertIn('js/auto-rule-modal.js', self.html)
        self.assertIn('js/rules.js', self.html)

    def test_no_native_confirm(self):
        """DESIGN.md 3.12 - the delete dialog carries a choice, so it is a buildModal()."""
        for rel in ('static/js/rules.js', 'static/js/rule-detail.js',
                    'static/js/auto-rule-modal.js', 'templates/rules.html',
                    'templates/rule_detail.html'):
            code = re.sub(r'/\*.*?\*/|\{#.*?#\}|(?<![:\'"])//[^\n]*', '', _read(rel), flags=re.S)
            self.assertNotRegex(code, r'(?<![\w$.])confirm\s*\(', rel)


class VerdictPhraseTests(unittest.TestCase):

    def test_the_phrase_table_names_all_six_verdicts_in_order(self):
        """database.py: every branch over action_taken names all six. Its wording is driven
        through the toasts in tests/test_auto_rule_modal_js.py."""
        self.assertEqual([a for a, _one, _many in VERDICT_PHRASES], list(RULE_MATCH_ACTIONS))
        self.assertEqual([p['action'] for p in verdict_phrases() if p['pick']],
                         [RULE_MATCH_SCHEDULED])


class NavTests(_Base):

    def _links(self, html):
        return re.findall(r'<a class="nav-link([^"]*)" href="/rules">', html)

    def test_both_shells_link_it_and_mark_it_active_here_and_on_a_rule(self):
        rid = make_rule('Cup')
        for path in ('/rules', f'/rules/{rid}'):
            links = self._links(self.get(path))
            self.assertEqual(len(links), 2, links)
            for cls in links:
                self.assertIn('active', cls)

    def test_it_is_not_active_on_another_page(self):
        for cls in self._links(self.get('/tags')):
            self.assertNotIn('active', cls)

    def test_it_is_called_auto_record_right_after_recordings(self):
        html = self.get()
        sidebar = html.split('<aside class="sidebar">')[1].split('</aside>')[0]
        labels = re.findall(r'<span class="nav-t">([^<]+)</span>', sidebar)
        i = labels.index('Recordings')
        self.assertEqual(labels[i - 1], 'Library')
        self.assertEqual(labels[i + 1], 'Auto-record')
        self.assertNotIn('Rules', labels)

    def test_it_has_its_own_glyph(self):
        html = self.get()
        sidebar = html.split('<aside class="sidebar">')[1].split('</aside>')[0]
        icons = re.findall(r'<svg class="nav-i"[^>]*>(.*?)</svg>\s*<span class="nav-t">([^<]+)<',
                           sidebar, re.S)
        by_label = {label: svg for svg, label in icons}
        self.assertIn('Auto-record', by_label)
        others = [svg for label, svg in by_label.items() if label != 'Auto-record']
        self.assertNotIn(by_label['Auto-record'], others)


class SearchPageEntryTests(_Base):
    """The other half of §8's IA: "Create rule from this search" on the Programs search, in
    the Saved searches menu and on the results bar. The dialog itself is driven in
    tests/test_auto_rule_modal_js.py, the rule errand in tests/test_rule_page_conformance.py."""

    def setUp(self):
        super().setUp()
        self.html = self.get('/channels?grain=airings')

    def test_the_page_loads_the_dialog_and_hands_it_what_it_needs(self):
        from app.config import load_config
        self.assertIn('js/auto-rule-modal.js', self.html)
        cfg = self.html.split('autoRule: {')[1].split('\n  },')[0]
        self.assertIn('createUrl: "/api/auto-rules"', cfg)
        self.assertIn("apiBase: '/api/auto-rules/'", cfg)
        self.assertIn('rulesUrl: "/rules"', cfg)
        self.assertIn(f'defaultCap: {auto_rules.default_cap(load_config())},', cfg)
        phrases = json.loads(re.search(r'phrases: (\[.*?\]),', cfg).group(1))
        self.assertEqual(phrases, verdict_phrases())

    def test_both_entries_are_served_and_offered_on_the_programs_grain_only(self):
        """A rule records showings, and a channel has no start time (§3.1). The results bar's
        button is served hidden - the nothing-active state - and drawn by renderRuleBar()."""
        self.assertRegex(self.html, r'<button class="btn btn-sm" id="sel-mkrule" type="button" '
                                    r'style="display:none"')
        js = _read('static/js/channel-search.js')
        self.assertRegex(js, r"isAirings\(\) && ruleMode\(\) !== 'edit' \? `<div class=\"psep\"></div>\s*"
                             r"<div class=\"pfoot\"><button class=\"btn btn-sm\" data-mkrule=\"1\">"
                             r"Create rule from this search&#8230;</button>")
        self.assertIn("if (e.target.closest('[data-mkrule]'))", js)
        self.assertRegex(js, r'function openRuleFromSearch\(\) \{\s*if \(!isAirings\(\)\) return;')
        self.assertRegex(js, r"mk\.style\.display = isAirings\(\) && mode !== 'edit' \? '' : 'none';")


def _observe_page_js():
    t = make_test_app()
    try:
        with t.app.app_context():
            now = datetime.utcnow()
            busy = make_rule('Busy')
            add_recording(busy, 'SCHEDULED', now + timedelta(days=1), 'Busy one')
            add_recording(busy, 'SCHEDULED', now + timedelta(days=2), 'Busy two')
            idle = make_rule('Idle', search='grain=airings&q=cup')
            auto_rules.set_rule_enabled(idle, False)
            before = t.client.get('/rules').get_data(as_text=True)
            auto_rules.delete_rule(busy, False)
            after = t.client.get('/rules').get_data(as_text=True)
    finally:
        t.cleanup()
    with tempfile.TemporaryDirectory() as tmp:
        for name, text in (('before.html', before), ('after.html', after),
                           ('ids.json', json.dumps({'busy': busy, 'idle': idle}))):
            with open(os.path.join(tmp, name), 'w', encoding='utf-8') as fh:
                fh.write(text)
        proc = subprocess.run([shutil.which('node'), HARNESS, tmp, REPO],
                              capture_output=True, text=True, cwd=REPO, timeout=120)
    if proc.returncode != 0:
        raise AssertionError(f'harness failed:\n{proc.stdout}\n{proc.stderr[-4000:]}')
    obs = json.loads(proc.stdout)
    obs['ids'] = {'busy': busy, 'idle': idle}
    return obs


@unittest.skipIf(shutil.which('node') is None, 'node not installed')
@unittest.skipIf(not os.path.isdir(JSDOM), 'jsdom not installed (npm install)')
class RulesPageJsTests(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.obs = _observe_page_js()
        cls.busy = cls.obs['ids']['busy']
        cls.idle = cls.obs['ids']['idle']

    def test_no_script_errors_and_no_full_reloads(self):
        for name, run in self.obs.items():
            if isinstance(run, dict) and 'errors' in run and name != 'row_click':
                self.assertEqual(run['errors'], [], name)
                self.assertEqual(run['navigations'], [], name)

    def test_a_row_click_opens_the_rule_and_a_kebab_click_does_not(self):
        """bindNavClicks, so Ctrl/Cmd-click opens a tab like a real link (DESIGN.md 3.10)."""
        self.assertEqual(self.obs['row_click']['opened'], f'/rules/{self.busy}')
        self.assertIsNone(self.obs['kebab_click']['opened'])

    def test_the_delete_dialog_names_the_rule_and_offers_the_choice(self):
        shown = self.obs['delete_keep']['dialog']
        self.assertEqual(shown['title'], 'Delete rule')
        self.assertIn('"Busy" will be deleted', shown['text'])
        self.assertIn('It has 2 scheduled recordings that have not started yet.', shown['text'])
        self.assertIn('Recordings it already started or finished are never touched.',
                      shown['text'])
        self.assertEqual(shown['choices'], [
            {'value': 'keep', 'checked': True, 'label': 'Keep them as ordinary recordings'},
            {'value': 'cancel', 'checked': False, 'label': 'Cancel its 2 scheduled recordings'},
        ])

    def test_the_dialog_is_cancel_then_a_danger_delete_starting_on_cancel(self):
        shown = self.obs['delete_keep']['dialog']
        self.assertEqual(shown['footer'], [['Cancel', 'btn'], ['Delete rule', 'btn btn-danger']])
        self.assertEqual(shown['focused'], 'Cancel')
        self.assertFalse(shown['error_shown'])

    def test_keeping_is_the_default_and_is_what_is_sent(self):
        run = self.obs['delete_keep']
        self.assertEqual(run['sent'], [{'url': f'/api/auto-rules/{self.busy}', 'method': 'DELETE',
                                        'body': {'cancel_scheduled': False}}])
        self.assertIsNone(run['dialog_after'])
        self.assertEqual(run['toasts'][-1]['msg'],
                         'Rule deleted. 2 recordings kept as ordinary recordings.')

    def test_choosing_cancel_sends_it_and_says_what_was_cancelled(self):
        run = self.obs['delete_cancel']
        self.assertEqual(run['sent'][0]['body'], {'cancel_scheduled': True})
        self.assertEqual(run['toasts'][-1]['msg'],
                         'Rule deleted. 2 scheduled recordings cancelled. 1 other recording kept.')

    def test_a_rule_with_nothing_scheduled_asks_no_question(self):
        run = self.obs['delete_none']
        self.assertEqual(run['dialog']['choices'], [])
        self.assertNotIn('Cancel its', run['dialog']['text'])
        self.assertIn('never touched', run['dialog']['text'])
        self.assertEqual(run['sent'][0]['body'], {'cancel_scheduled': False})
        self.assertEqual(run['toasts'][-1]['msg'], 'Rule deleted.')

    def test_after_an_action_the_page_is_swapped_for_a_fresh_render(self):
        """One renderer - the template. The deleted rule's row goes and the count follows."""
        self.assertEqual(self.obs['first_rows'], [self.busy, self.idle])
        run = self.obs['delete_keep']
        self.assertEqual(len(run['page_fetches']), 1)
        self.assertEqual(run['rows_after'], [self.idle])
        self.assertIn('1 rule', run['head_after'])

    def test_a_failed_delete_stays_in_the_dialog(self):
        run = self.obs['delete_fail']
        self.assertIsNotNone(run['dialog_after'])
        self.assertTrue(run['dialog_after']['error_shown'])
        self.assertEqual(run['dialog_after']['error'], 'Rule not found.')
        self.assertEqual(run['toasts'], [])
        self.assertEqual(run['page_fetches'], [])
        self.assertEqual(run['rows_after'], [self.busy, self.idle])

    def test_pause_says_its_scheduled_recordings_still_record(self):
        run = self.obs['pause']
        self.assertEqual(run['sent'], [{'url': f'/api/auto-rules/{self.busy}/pause',
                                        'method': 'POST', 'body': None}])
        msg = run['toasts'][-1]['msg']
        self.assertIn('Paused "Busy". It schedules nothing new until you resume it.', msg)
        self.assertIn('The 2 recordings it already scheduled still record.', msg)
        self.assertEqual(len(run['page_fetches']), 1)

    def test_a_failed_pause_says_so_and_changes_nothing(self):
        run = self.obs['pause_fail']
        self.assertEqual(run['toasts'][-1], {'msg': 'Rule not found.', 'type': 'error'})
        self.assertEqual(run['page_fetches'], [])

    def test_resume_says_what_its_check_did(self):
        run = self.obs['resume']
        self.assertEqual(run['sent'][0]['url'], f'/api/auto-rules/{self.idle}/resume')
        self.assertEqual(run['toasts'][-1],
                         {'msg': 'Resumed "Idle". 2 scheduled, 1 repeat skipped.', 'type': 'success'})

    def test_a_queued_resume_says_what_it_waits_for(self):
        toast = self.obs['resume_queued']['toasts'][-1]
        self.assertEqual(toast['type'], 'info')
        self.assertIn('Resumed "Idle". It has not been checked yet: an account sync is already '
                      'running (Acct).', toast['msg'])

    def test_check_now_says_what_the_check_did(self):
        run = self.obs['check']
        self.assertEqual(run['sent'], [{'url': f'/api/auto-rules/{self.busy}/check',
                                        'method': 'POST', 'body': None}])
        self.assertEqual(run['toasts'][-1],
                         {'msg': 'Checked "Busy". 1 scheduled, 2 already scheduled.',
                          'type': 'success'})
        self.assertEqual(len(run['page_fetches']), 1)

    def test_edit_settings_opens_the_dialog_on_the_rule(self):
        shown = self.obs['edit']
        self.assertEqual(shown['title'], 'Edit rule')
        self.assertEqual(shown['name'], 'Busy')
        self.assertEqual(shown['search'][0], 'Text: nascar')
        self.assertIn(f'rule={self.busy}', shown['edit_search_href'])


if __name__ == '__main__':
    unittest.main()
