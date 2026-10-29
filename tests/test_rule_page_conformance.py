"""An auto-record rule's own page (/rules/<id>, templates/rule_detail.html +
static/js/rule-detail.js, app/auto_rules_view.py) against the design that produced it:
mockup 42 and DESIGN-autoschedule.md §8 for what it says, DESIGN.md 3.3, 3.6, 3.12, 4 and 14
for how (dev/changelog/1189). What is asserted:

  * The group page's shell: .page-head with "← All Rules", .gd-head with the name and the
    state badge, then .gd-statezone holding the banners and the .gd-actionbar - Edit search
    (primary), Pause or Resume, and the kebab, all .btn-sm. The bar is the rule's status and
    is NOT a section.
  * The cards are [data-section]s in .gd-sections, in the approved default order: Coming up,
    What it searches for, Checks, Settings, History; Customize sections reorders them, one
    layout for every rule.
  * One state per rule, with its banner: Not running for `last_error`, a running rule's
    problem for `last_problems` (never "Not running"), At its cap, Paused, Not checked.
  * The ledger: Coming up is every judged showing ahead, soonest first; History is what aired,
    newest first, filterable by outcome and paged. A pick's badge is its recording's status
    in the Recordings page's words; a skip's is its verdict; what became of a pick is said.
  * What it searches for marks a name that no longer exists where it is listed.
  * Check now runs the rule's check (POST /api/auto-rules/<id>/check) and is refused on a
    paused rule. A recording a rule made says so, linking the rule.

The page's actions are driven in jsdom over the page as the app rendered it
(tests/support/rule_page.mjs). Throwaway temp SQLite DB - never the live dvr.db.
  python3 -m unittest tests.test_rule_page_conformance
"""
import html as html_lib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timedelta
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tests.support.app import make_test_app  # noqa: E402
from tests.support import seed  # noqa: E402
from app import auto_rules, auto_rules_view, db  # noqa: E402
from app.database import (  # noqa: E402
    AutoRule, CANCEL_BEFORE_START, CANCEL_DURING_CAPTURE, CANCEL_GROUP_CHANGE,
    RULE_MATCH_SCHEDULED, RULE_MATCH_SKIPPED_CAP, RULE_MATCH_SKIPPED_EXISTING,
    RULE_MATCH_SKIPPED_LIMIT, RULE_MATCH_SKIPPED_REPEAT, RULE_MATCH_USER_MODIFIED,
    RuleMatch, Tag, UserPref,
)

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HARNESS = os.path.join(REPO, 'tests', 'support', 'rule_page.mjs')
JSDOM = os.path.join(REPO, 'node_modules', 'jsdom')


def _read(rel):
    with open(os.path.join(REPO, rel), encoding='utf-8') as fh:
        return fh.read()


def _unescaped(text):
    return html_lib.unescape(text)


def _text(fragment):
    return ' '.join(_unescaped(re.sub(r'<[^>]+>', ' ', fragment)).split())


def _section(html, key):
    start = html.index(f'data-section="{key}"')
    nxt = html.find('data-section="', start + 20)
    return html[start:nxt if nxt != -1 else len(html)]


def _tbody_rows(fragment):
    rows = []
    for body in fragment.split('<tbody>')[1:]:
        rows += re.findall(r'<tr>.*?</tr>', body.split('</tbody>')[0], re.S)
    return rows


def make_rule(name='Cup', search='grain=airings&q=nascar', **settings):
    return auto_rules.create_rule(auto_rules.read_rule_values(
        dict({'name': name, 'search': search}, **settings)))


def checked(rule_id, minutes=5, reach_days=3, error=None, problems=None):
    rule = db.session.get(AutoRule, rule_id)
    rule.last_evaluated_at = datetime.utcnow() - timedelta(minutes=minutes)
    rule.last_guide_reach = (datetime.utcnow() + timedelta(days=reach_days)
                             if reach_days is not None else None)
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

    def get(self, path):
        resp = self.client.get(path)
        self.assertEqual(resp.status_code, 200, path)
        return resp.get_data(as_text=True)


class _Ledger(_Base):
    """A rule whose ledger holds every kind of row: picks whose recordings are scheduled,
    recording, finished, failed, aborted by a person and called off by a group change; picks
    whose recordings were deleted at each stamp; every skip verdict on each side of now; and
    a showing the user edited."""

    def setUp(self):
        super().setUp()
        self.now = datetime.utcnow()
        acc = seed.make_account(name='Acct')
        self.nbc = seed.make_channel(acc, name='NBC East')
        self.fox = seed.make_channel(acc, name='Fox One')
        self.group = seed.make_group(name='NBC', members=[self.nbc])
        db.session.commit()
        self.rid = make_rule()
        checked(self.rid)
        self.recs = {}
        n = [0]

        def row(action, hours, status=None, removed=None, removed_reason=None,
                cancel_reason=None, detail='Scheduled on NBC (via NBC East).', group=True):
            n[0] += 1
            start = self.now + timedelta(hours=hours)
            rec_id = None
            if status is not None:
                rec = seed.make_recording(status=status, name=f'rec {n[0]}', rule_id=self.rid,
                                          start_time=start, stop_time=start + timedelta(hours=1),
                                          cancel_reason=cancel_reason)
                rec_id = rec.id
                self.recs[n[0]] = rec
            db.session.add(RuleMatch(
                rule_id=self.rid, title_key=f't{n[0]}', episode_key='',
                program_title=f'Race {n[0]}', program_sub_title=f'Round {n[0]}',
                program_start_time=start, program_stop_time=start + timedelta(hours=1),
                action_taken=action, recording_id=rec_id, removed_status=removed,
                removed_cancel_reason=removed_reason,
                channel_id=self.nbc.id if group else self.fox.id,
                group_id=self.group.id if group else None, detail=detail))
            db.session.commit()
            return n[0]

        S = RULE_MATCH_SCHEDULED
        # Coming up.
        self.next_pick = row(S, 24, status='SCHEDULED', detail=(
            'Scheduled on NBC (via NBC East). Moved with the program from Sun Oct 18, 2:00 PM '
            'EDT to Sun Oct 18, 2:30 PM EDT. Scheduled with a warning: the account is full.'))
        self.cap_ahead = row(RULE_MATCH_SKIPPED_CAP, 48, group=False)
        row(RULE_MATCH_SKIPPED_EXISTING, 30)
        # History.
        self.recorded = row(S, -24, status='COMPLETED')
        self.recording = row(S, -0.5, status='IN_PROGRESS')
        self.failed = row(S, -48, status='FAILED')
        self.aborted = row(S, -72, status='ABORTED', cancel_reason=CANCEL_DURING_CAPTURE)
        self.called_off = row(S, -96, status='ABORTED', cancel_reason=CANCEL_GROUP_CHANGE)
        self.cancelled = row(S, -120, removed='SCHEDULED')
        self.deleted = row(S, -144, removed='COMPLETED')
        self.deleted_failed = row(S, -168, removed='FAILED')
        self.deleted_never_started = row(S, -192, removed='ABORTED',
                                         removed_reason=CANCEL_BEFORE_START)
        self.repeat = row(RULE_MATCH_SKIPPED_REPEAT, -200)
        self.cap_past = row(RULE_MATCH_SKIPPED_CAP, -210)
        self.limit = row(RULE_MATCH_SKIPPED_LIMIT, -220)
        self.edited = row(RULE_MATCH_USER_MODIFIED, -230, status='COMPLETED')
        self.html = self.get(f'/rules/{self.rid}')

    def ledger_row(self, html, n):
        for tr in _tbody_rows(html):
            if f'Race {n}<' in tr:
                return tr
        self.fail(f'no ledger row for Race {n}')

    def badge(self, tr):
        return re.search(r'<span class="badge ([^" ]+)">(?:<span class="pulse"></span>)?([^<]+)</span>',
                         tr).groups()


class ShellTests(_Ledger):

    def test_the_page_head_and_the_way_back(self):
        """DESIGN.md 4: a back link is `← DestinationName`."""
        h1s = re.findall(r'<h1[^>]*>(.*?)</h1>', self.html, re.S)
        self.assertEqual([_text(h) for h in h1s], ['Auto-record rules'])
        head = self.html.split('<div class="page-head">')[1].split('</div>')[0]
        self.assertIn('<a href="/rules" class="btn"><svg class="icon" data-icon="back"', head)
        self.assertIn('#i-back"></use></svg> All Rules</a>', head)

    def test_the_header_names_the_rule_and_its_state(self):
        head = self.html.split('<div class="gd-head">')[1].split('<div class="gd-statezone">')[0]
        self.assertIn('<h2>Cup</h2>', head)
        self.assertRegex(head, r'<span class="badge b-warn tip-plain" data-tip="[^"]*">At its cap</span>')
        self.assertIn('class="gd-sub-lead"', head)
        self.assertIn('Added ', _text(head.split('class="gd-dates"')[1]))

    def test_the_status_bar_is_in_the_state_zone_and_is_not_a_section(self):
        zone = self.html.split('<div class="gd-statezone">')[1].split('<div class="gd-sections"')[0]
        self.assertIn('<div class="gd-actionbar" id="rule-actionbar"', zone)
        sections = self.html.split('id="rule-sections"')[1]
        self.assertNotIn('gd-actionbar', sections)

    def test_the_bar_controls_are_all_btn_sm_with_edit_search_primary(self):
        """Round 1 of mockup 42: the kebab was shorter than the buttons beside it."""
        bar = self.html.split('id="rule-actionbar"')[1].split('<div class="gd-sections"')[0]
        actions = bar.split('class="gd-ab-actions"')[1]
        controls = re.findall(r'<(?:a|button) class="(btn[^"]*)"', actions)
        self.assertEqual(controls, ['btn btn-primary btn-sm', 'btn btn-sm', 'btn btn-sm btn-icon'])
        edit = re.search(r'<a class="btn btn-primary btn-sm" href="([^"]+)"', actions).group(1)
        self.assertIn(f'rule={self.rid}', _unescaped(edit))
        self.assertIn('>Edit search</a>', actions)
        self.assertIn('data-act="pause">Pause</button>', actions)

    def test_the_kebab(self):
        menu = self.html.split('id="rule-actionbar"')[1].split('<div class="menu pop-left">')[1]
        menu = menu.split('</div>\n      </span>')[0]
        items = [_text(x) for x in re.findall(r'class="menu-item[^"]*"[^>]*>(.*?)</', menu)]
        self.assertEqual(items, ['Edit settings…', 'Open search', 'Customize sections…',
                                 'Delete rule'])
        self.assertIn('<button class="menu-item danger" data-act="delete">Delete rule</button>', menu)

    def test_the_sections_are_in_the_approved_default_order(self):
        order = re.findall(r'<div class="card" data-section="(\w+)"', self.html)
        self.assertEqual(order, ['coming', 'search', 'checks', 'settings', 'history'])
        self.assertEqual(list(auto_rules_view.SECTIONS), order)
        start = self.html.index('window.RULE_CONFIG = Object.assign(') + len(
            'window.RULE_CONFIG = Object.assign(')
        cfg, _end = json.JSONDecoder().raw_decode(self.html[start:])
        self.assertEqual(cfg['apiBase'], '/api/auto-rules/')
        tail = self.html.split('window.RULE_CONFIG')[1]
        self.assertIn('sections: ["coming", "search", "checks", "settings", "history"]', tail)
        self.assertIn('sectionsPrefKey: "rule_detail_sections"', tail)
        self.assertIn('sectionPref: null', tail)

    def test_card_heads_carry_their_own_actions(self):
        search = _section(self.html, 'search')
        self.assertIn('Open search <span class="arw"><svg class="icon" data-icon="forward"', search)
        self.assertIn('>Edit search</a>', search)
        self.assertIn('data-act="check">Check now</button>', _section(self.html, 'checks'))
        self.assertIn('data-act="edit">Edit settings</button>', _section(self.html, 'settings'))

    def test_open_search_is_the_stored_spelling(self):
        stored = db.session.get(AutoRule, self.rid).search_params
        self.assertIn(f'href="/channels?{stored}">Open search', _unescaped(_section(self.html, 'search')))

    def test_a_missing_rule_is_404(self):
        self.assertEqual(self.client.get('/rules/99999').status_code, 404)


class LedgerTests(_Ledger):

    def test_coming_up_is_every_judged_showing_ahead_soonest_first(self):
        coming = _section(self.html, 'coming')
        titles = re.findall(r'<span class="t">([^<]+)</span>', coming)
        self.assertEqual(titles, [f'Race {self.next_pick}', 'Race 3', f'Race {self.cap_ahead}'])
        self.assertIn('Coming up <span class="cnt">3</span>', coming)

    def test_history_is_what_aired_newest_first(self):
        hist = _section(self.html, 'history')
        titles = re.findall(r'<span class="t">([^<]+)</span>', hist)
        self.assertEqual(titles[:3], [f'Race {self.recording}', f'Race {self.recorded}',
                                      f'Race {self.failed}'])
        self.assertEqual(len(titles), auto_rules_view.HISTORY_PAGE)
        self.assertIn('History <span class="cnt">13</span>', hist)

    def test_a_pick_wears_its_recordings_status_in_the_recordings_pages_words(self):
        """fmt_utils.REC_STATUS_DISPLAY - one word per status app-wide (dev/changelog/961)."""
        html = self.get(f'/rules/{self.rid}?shown=48')
        want = {self.next_pick: ('b-sched', 'SCHEDULED'), self.recording: ('b-live', 'RECORDING'),
                self.recorded: ('b-done', 'COMPLETED'), self.failed: ('b-fail', 'FAILED'),
                self.aborted: ('b-abort', 'CANCELLED'), self.called_off: ('b-abort', 'CANCELLED')}
        for n, badge in want.items():
            self.assertEqual(self.badge(self.ledger_row(html, n)), badge, n)
        self.assertIn('<span class="pulse"></span>', self.ledger_row(html, self.recording))

    def test_a_deleted_picks_stamp_is_said(self):
        html = self.get(f'/rules/{self.rid}?shown=48')
        cases = {
            self.cancelled: (('b-abort', 'Cancelled'), 'You cancelled it before it started, so '
                             'this rule will not schedule it again.'),
            self.deleted: (('b-abort', 'Deleted'), 'It was recorded, and later deleted.'),
            self.deleted_failed: (('b-fail', 'Failed'), 'The recording failed, and was later deleted.'),
            self.deleted_never_started: (('b-abort', 'CANCELLED'),
                                         'It was cancelled, and its recording was later deleted.'),
        }
        for n, (badge, fate) in cases.items():
            tr = self.ledger_row(html, n)
            self.assertEqual(self.badge(tr), badge, n)
            self.assertIn(fate, _text(tr), n)

    def test_each_skip_wears_its_verdict(self):
        html = self.get(f'/rules/{self.rid}?shown=48')
        self.assertEqual(self.badge(self.ledger_row(html, self.cap_ahead)), ('b-warn', 'Waiting: cap'))
        self.assertEqual(self.badge(self.ledger_row(html, self.cap_past)), ('b-warn', 'Cap reached'))
        self.assertEqual(self.badge(self.ledger_row(html, self.repeat)), ('b-abort', 'Repeat'))
        self.assertEqual(self.badge(self.ledger_row(html, self.limit)), ('b-abort', 'Over the limit'))
        self.assertEqual(self.badge(self.ledger_row(html, 3)), ('b-abort', 'Has a recording'))
        self.assertEqual(self.badge(self.ledger_row(html, self.edited)), ('b-warn', 'You edited it'))

    def test_what_became_of_a_pick_links_its_recording(self):
        html = self.get(f'/rules/{self.rid}?shown=48')
        rec = self.recs[self.failed]
        tr = self.ledger_row(html, self.failed)
        self.assertIn('The recording failed, so a later airing of it is not a repeat.', _text(tr))
        self.assertIn(f'<a href="/recordings/{rec.id}">{rec.name} <svg class="icon" data-icon="forward"', tr)
        self.assertIn('You aborted it while it was recording.', _text(self.ledger_row(html, self.aborted)))
        self.assertIn('A change to its channel group called it off.',
                      _text(self.ledger_row(html, self.called_off)))
        self.assertIn('Your edited recording:', _text(self.ledger_row(html, self.edited)))

    def test_where_names_the_group_and_the_member_it_came_through(self):
        tr = self.ledger_row(self.html, self.next_pick)
        self.assertIn('NBC<span class="via">via NBC East</span>', tr)
        tr = self.ledger_row(self.html, self.cap_ahead)
        self.assertIn('Fox One', tr)
        self.assertNotIn('class="via"', tr)

    def test_a_move_and_a_warning_are_set_apart_in_the_engines_own_words(self):
        tr = self.ledger_row(self.html, self.next_pick)
        self.assertIn('<span class="moved">Moved with the program from Sun Oct 18, 2:00 PM EDT '
                      'to Sun Oct 18, 2:30 PM EDT.</span>', tr)
        self.assertIn('<span class="warned">Scheduled with a warning: the account is full.</span>', tr)

    def test_every_value_cell_carries_its_column_header(self):
        """DESIGN.md 3.2: the tables are cards below 960px."""
        for key, labels in (('coming', ['When', 'Program', 'Channel', 'Decision', 'Why']),
                            ('history', ['Aired', 'Program', 'Channel', 'Outcome', 'Why'])):
            section = _section(self.html, key)
            self.assertIn('<div class="table-scroll">', section)
            self.assertIn('<table class="tbl tbl-cards rl-tbl">', section)
            for tr in _tbody_rows(section):
                self.assertEqual(re.findall(r'<td data-label="([^"]+)"', tr), labels)


class HistoryFilterTests(_Ledger):

    def chips(self, html):
        hist = _section(html, 'history')
        return [(k, _text(lbl), 'active' in cls) for cls, k, lbl in re.findall(
            r'<a class="chip([^"]*)" data-hist="(\w+)"\s+href="[^"]+">(.*?)</a>', hist, re.S)]

    def test_the_chips_count_each_outcome_and_all_is_active(self):
        self.assertEqual(self.chips(self.html), [
            ('all', 'All 13', True), ('rec', 'Recorded 1', False),
            ('bad', 'Failed or stopped 3', False), ('gone', 'Cancelled or deleted 4', False),
            ('skip', 'Skipped 3', False)])

    def test_a_filter_narrows_history_and_is_a_link(self):
        html = self.get(f'/rules/{self.rid}?hist=gone')
        hist = _section(html, 'history')
        titles = re.findall(r'<span class="t">([^<]+)</span>', hist)
        self.assertEqual(titles, [f'Race {n}' for n in (self.called_off, self.cancelled,
                                                        self.deleted, self.deleted_never_started)])
        self.assertIn(('gone', 'Cancelled or deleted 4', True), self.chips(html))
        self.assertIn(f'href="/rules/{self.rid}?hist=skip"', hist)

    def test_an_unknown_filter_is_all(self):
        html = self.get(f'/rules/{self.rid}?hist=nonsense')
        self.assertIn(('all', 'All 13', True), self.chips(html))

    def test_show_more_pages_the_rows(self):
        hist = _section(self.html, 'history')
        # Everything left fits on the next page, so it says how many rather than "1 of 1".
        self.assertIn(f'href="/rules/{self.rid}?hist=all&amp;shown=24">Show 1 more</a>', hist)
        more = _section(self.get(f'/rules/{self.rid}?shown=24'), 'history')
        self.assertEqual(len(re.findall(r'<span class="t">', more)), 13)
        self.assertNotIn('data-hist-more', more)


class LongHistoryTests(_Base):

    def test_show_more_says_how_many_are_left_when_more_than_a_page_is(self):
        rid = make_rule()
        checked(rid)
        for i in range(30):
            at = datetime.utcnow() - timedelta(days=1, hours=i)
            db.session.add(RuleMatch(rule_id=rid, title_key=f't{i}', episode_key='',
                                     program_title=f'Race {i}', program_start_time=at,
                                     program_stop_time=at + timedelta(hours=1),
                                     action_taken=RULE_MATCH_SKIPPED_REPEAT))
        db.session.commit()
        hist = _section(self.get(f'/rules/{rid}'), 'history')
        self.assertIn(f'href="/rules/{rid}?hist=all&amp;shown=24">Show 12 more of 18</a>', hist)
        hist = _section(self.get(f'/rules/{rid}?shown=24'), 'history')
        self.assertIn(f'href="/rules/{rid}?hist=all&amp;shown=36">Show 6 more</a>', hist)


class StateTests(_Base):

    def page(self, rid):
        html = self.get(f'/rules/{rid}')
        zone = html.split('<div class="gd-statezone">')[1].split('<div class="gd-sections"')[0]
        badge = re.search(r'<span class="badge ([^" ]+) tip-plain"[^>]*>([^<]+)</span>',
                          zone.split('id="rule-actionbar"')[1]).groups()
        notices = [_text(n) for n in re.findall(r'<div class="notice [^"]+">(.*?)</div>\s*(?:<a|<button|</div>|$)',
                                               zone, re.S)]
        msg = _text(zone.split('class="gd-ab-msg">')[1].split('</span>')[0])
        return badge, notices, msg, html

    def test_on(self):
        rid = make_rule()
        checked(rid)
        badge, notices, msg, _html = self.page(rid)
        self.assertEqual(badge, ('b-done', 'On'))
        self.assertEqual(notices, [])
        self.assertEqual(msg, 'Nothing scheduled: nothing it matches is in the guide yet.')

    def test_not_running_says_why_and_offers_edit_search(self):
        rid = make_rule()
        checked(rid, error='Its search names the tag "live", which no longer exists.')
        badge, notices, msg, html = self.page(rid)
        self.assertEqual(badge, ('b-fail', 'Not running'))
        self.assertIn('This rule is not running. Its search names the tag "live", which no '
                      'longer exists.', notices[0])
        self.assertIn('Not running.', msg)
        zone = html.split('<div class="gd-statezone">')[1]
        self.assertRegex(zone, r'notice-warn notice-act">\s*<div>.*?</div>\s*<a class="btn btn-sm" '
                               r'href="[^"]*rule=' + str(rid))

    def test_a_running_rule_with_a_problem_is_never_not_running(self):
        """dev/changelog/1189: last_problems is a check that ran."""
        rid = make_rule()
        checked(rid, problems='"Cup Race" on Sun Oct 18, 3:00 PM EDT could not be placed.')
        badge, notices, _msg, html = self.page(rid)
        self.assertEqual(badge, ('b-done', 'On'))
        self.assertIn('Its last check could not schedule everything it found. "Cup Race" on Sun '
                      'Oct 18, 3:00 PM EDT could not be placed. The next check tries again.',
                      notices[0])
        self.assertNotIn('Not running', html.split('<div class="gd-statezone">')[1].split('gd-sections')[0])

    def test_paused_offers_resume_and_checks_say_none(self):
        rid = make_rule()
        checked(rid)
        auto_rules.set_rule_enabled(rid, False)
        badge, notices, msg, html = self.page(rid)
        self.assertEqual(badge, ('b-paused', 'Paused'))
        self.assertEqual(msg, 'Paused. It schedules nothing new.')
        self.assertIn('Resuming checks the guide at once.', notices[0])
        self.assertIn('data-act="resume">Resume</button>', html.split('id="rule-actionbar"')[1])
        checks = _section(html, 'checks')
        self.assertIn('None while paused', _text(checks))
        self.assertIn('data-act="check" disabled>Check now</button>', checks)

    def test_not_checked_yet(self):
        rid = make_rule()
        badge, notices, msg, html = self.page(rid)
        self.assertEqual(badge, ('b-sched', 'Not checked'))
        self.assertEqual(msg, 'Waiting for its first check.')
        self.assertIn('Not checked yet.', notices[0])
        self.assertIn('Not measured yet', _text(_section(html, 'checks')))
        self.assertIn('Nothing yet - the first check has not run.', _text(_section(html, 'coming')))

    def test_a_short_guide_is_a_banner_on_a_running_rule(self):
        rid = make_rule()
        checked(rid, reach_days=None)
        _badge, notices, _msg, html = self.page(rid)
        self.assertIn('The guide this rule searches is empty.', notices[0])
        self.assertIn('Nothing listed', _text(_section(html, 'checks')))

    def test_rule_state_precedence(self):
        """Paused wins over everything; a rule that cannot run says so before its cap."""
        class R:
            enabled, last_error, last_evaluated_at = True, None, datetime.utcnow()
        r = R()
        self.assertEqual(auto_rules_view.rule_state(r, 2), 'cap')
        r.last_error = 'broken'
        self.assertEqual(auto_rules_view.rule_state(r, 2), 'broken')
        r.enabled = False
        self.assertEqual(auto_rules_view.rule_state(r, 2), 'paused')
        r.enabled, r.last_error, r.last_evaluated_at = True, None, None
        self.assertEqual(auto_rules_view.rule_state(r, 0), 'new')
        r.last_evaluated_at = datetime.utcnow()
        self.assertEqual(auto_rules_view.rule_state(r, 0), 'on')


class CardTests(_Base):

    def test_what_it_searches_for_marks_a_name_that_is_gone(self):
        db.session.add(Tag(name='race-live'))
        db.session.commit()
        rid = make_rule(search='grain=airings&q=Supercars&in=epg-title&f.tag=race-live&x.tag=gone')
        html = self.get(f'/rules/{rid}')
        rows = re.findall(r'<div class="srow"><span class="sk">([^<]+)</span><span class="sv">(.*?)</span></div>',
                          _section(html, 'search'), re.S)
        lines = [(k, _text(v)) for k, v in rows]
        self.assertEqual(lines, [('Text', 'Supercars'), ('Searched in', 'Program title'),
                                 ('Tag', 'race-live'), ('Tag', 'not gone (no longer exists)')])
        self.assertIn('<span class="gone">(no longer exists)</span>', _section(html, 'search'))

    def test_settings_chips_are_the_rules_settings(self):
        rid = make_rule(limit_count=1, limit_window_hours=72, max_upcoming=0, skip_repeats=False)
        html = self.get(f'/rules/{rid}')
        items = json.loads(_unescaped(re.search(r'id="rule-setbar" data-items="([^"]+)"', html).group(1)))
        self.assertEqual([(i['focus'], i['label'], i['value']) for i in items], [
            ('profile', 'Recording profile', "Each showing's default profile"),
            ('repeats', 'Skip repeats', 'Off'),
            ('limit', 'Limit', 'At most 1 within 72 hours'),
            ('cap', 'Cap', 'No cap')])
        for i in items:
            self.assertTrue(i['tip'])

    def test_the_checks_card(self):
        rid = make_rule()
        checked(rid, minutes=90)
        text = _text(_section(self.get(f'/rules/{rid}'), 'checks'))
        self.assertIn('Last checked 2 hours ago', text)
        self.assertIn('History kept 90 days', text)

    def test_a_saved_layout_reaches_the_page_and_a_broken_one_does_not(self):
        rid = make_rule()
        db.session.add(UserPref(key='rule_detail_sections',
                                value=json.dumps({'order': ['history', 'coming'], 'hidden': ['checks']})))
        db.session.commit()
        tail = self.get(f'/rules/{rid}').split('window.RULE_CONFIG')[1]
        self.assertIn('sectionPref: {"hidden": ["checks"], "order": ["history", "coming"]}', tail)
        db.session.get(UserPref, 'rule_detail_sections').value = '{not json'
        db.session.commit()
        self.assertIn('sectionPref: null', self.get(f'/rules/{rid}').split('window.RULE_CONFIG')[1])


class CheckNowTests(_Base):

    def test_it_runs_the_rules_check_and_answers_what_it_did(self):
        rid = make_rule()
        with patch('app.auto_rules.evaluate_now', return_value={'queued': False, 'scheduled': 2}) as ev:
            resp = self.client.post(f'/api/auto-rules/{rid}/check')
        self.assertEqual(resp.status_code, 200)
        ev.assert_called_once_with(rid)
        body = resp.get_json()
        self.assertTrue(body['success'])
        self.assertEqual(body['evaluation'], {'queued': False, 'scheduled': 2})
        self.assertEqual(body['rule']['id'], rid)

    def test_a_paused_rule_is_not_checked(self):
        rid = make_rule()
        auto_rules.set_rule_enabled(rid, False)
        with patch('app.auto_rules.evaluate_now') as ev:
            resp = self.client.post(f'/api/auto-rules/{rid}/check')
        self.assertEqual(resp.status_code, 409)
        self.assertIn('Resume it to check it', resp.get_json()['error'])
        ev.assert_not_called()

    def test_a_missing_rule(self):
        self.assertEqual(self.client.post('/api/auto-rules/999/check').status_code, 404)

    def test_a_check_that_raises_is_an_answer_not_a_500(self):
        rid = make_rule()
        with patch('app.auto_rules.evaluate_now', side_effect=RuntimeError('boom')):
            resp = self.client.post(f'/api/auto-rules/{rid}/check')
        self.assertEqual(resp.status_code, 200)
        error = resp.get_json()['evaluation']['error']
        self.assertIn('boom', error)
        self.assertIn('The check runs again shortly.', error)
        self.assertNotIn('saved', error)

    def test_creating_a_rule_answers_its_page(self):
        with patch('app.auto_rules.evaluate_now', return_value={'queued': False}):
            resp = self.client.post('/api/auto-rules', json={'name': 'Cup', 'search': 'grain=airings&q=cup'})
        body = resp.get_json()
        self.assertEqual(body['url'], f'/rules/{body["rule"]["id"]}')


class RecordingPageTests(_Base):

    def test_a_recording_a_rule_made_names_the_rule(self):
        rid = make_rule(name='NASCAR Cup Series')
        start = datetime.utcnow() + timedelta(days=1)
        rec = seed.make_recording(status='SCHEDULED', name='Race', rule_id=rid, start_time=start,
                                  stop_time=start + timedelta(hours=3))
        db.session.commit()
        html = self.get(f'/recordings/{rec.id}')
        meta = html.split('<div class="dh-meta">')[1].split('<div class="dh-actions">')[0]
        self.assertIn(f'href="/rules/{rid}"', meta)
        self.assertIn('Scheduled by rule NASCAR Cup Series <svg class="icon" data-icon="forward"', meta)

    def test_one_a_person_made_does_not(self):
        start = datetime.utcnow() + timedelta(days=1)
        rec = seed.make_recording(status='SCHEDULED', name='Race', start_time=start,
                                  stop_time=start + timedelta(hours=3))
        db.session.commit()
        self.assertNotIn('Scheduled by rule', self.get(f'/recordings/{rec.id}'))


def _observe_page_js():
    t = make_test_app()
    try:
        with t.app.app_context():
            rid = make_rule(name='Cup', limit_count=1, limit_window_hours=72)
            checked(rid)
            start = datetime.utcnow() - timedelta(days=1)
            for i in range(14):
                s = start - timedelta(hours=i)
                db.session.add(RuleMatch(
                    rule_id=rid, title_key=f't{i}', episode_key='', program_title=f'Race {i}',
                    program_start_time=s, program_stop_time=s + timedelta(hours=1),
                    action_taken=RULE_MATCH_SKIPPED_REPEAT if i % 2 else RULE_MATCH_SKIPPED_LIMIT))
            db.session.commit()
            page = t.client.get(f'/rules/{rid}').get_data(as_text=True)
            skip = t.client.get(f'/rules/{rid}?hist=skip').get_data(as_text=True)
    finally:
        t.cleanup()
    with tempfile.TemporaryDirectory() as tmp:
        for name, text in (('page.html', page), ('skip.html', skip),
                           ('ids.json', json.dumps({'rule': rid}))):
            with open(os.path.join(tmp, name), 'w', encoding='utf-8') as fh:
                fh.write(text)
        proc = subprocess.run([shutil.which('node'), HARNESS, tmp, REPO],
                              capture_output=True, text=True, cwd=REPO, timeout=120)
    if proc.returncode != 0:
        raise AssertionError(f'harness failed:\n{proc.stdout}\n{proc.stderr[-4000:]}')
    obs = json.loads(proc.stdout)
    obs['rid'] = rid
    return obs


@unittest.skipIf(shutil.which('node') is None, 'node not installed')
@unittest.skipIf(not os.path.isdir(JSDOM), 'jsdom not installed (npm install)')
class RulePageJsTests(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.obs = _observe_page_js()
        cls.rid = cls.obs['rid']

    def test_no_script_errors(self):
        for name, run in self.obs.items():
            if isinstance(run, dict) and 'errors' in run:
                self.assertEqual(run['errors'], [], name)

    def test_the_settings_are_chips_on_a_desktop_and_a_list_on_a_phone(self):
        """The group page's Settings card, through the one renderer they share
        (util.js renderSettingChips)."""
        desk = self.obs['desktop']
        self.assertEqual(desk['setbar_class'], 'gd-setbar')
        self.assertEqual(desk['chips'], [
            ['Recording profile', "Each showing's default profile", 'profile'],
            ['Skip repeats', 'On', 'repeats'], ['Limit', 'At most 1 within 72 hours', 'limit'],
            ['Cap', '10 scheduled at once (the default)', 'cap']])
        phone = self.obs['phone']
        self.assertEqual(phone['setbar_class'], '')
        self.assertEqual(phone['rows'], 4)

    def test_a_chip_opens_the_edit_dialog_on_its_own_field(self):
        for focus, field in (('profile', 'ar-profile'), ('repeats', 'ar-repeats'),
                             ('limit', 'ar-limit-count'), ('cap', 'ar-cap')):
            run = self.obs[f'chip_{focus}']
            self.assertEqual(run['title'], 'Edit rule')
            self.assertEqual(run['focused'], field, focus)

    def test_the_edit_dialog_starts_from_the_rule_and_saves_settings_only(self):
        run = self.obs['edit_save']
        self.assertEqual(run['prefill'], {'name': 'Cup', 'limit_count': '1', 'limit_hours': '72',
                                          'cap': '', 'repeats': True})
        self.assertEqual(run['footer'], [['Cancel', 'btn'], ['Save', 'btn btn-primary']])
        self.assertEqual(run['sent'][0]['url'], f'/api/auto-rules/{self.rid}')
        self.assertEqual(run['sent'][0]['method'], 'PUT')
        self.assertNotIn('search', run['sent'][0]['body'])
        self.assertEqual(run['sent'][0]['body']['max_upcoming'], 4)
        self.assertEqual(run['toasts'][-1], {'msg': 'Rule saved. 1 scheduled.', 'type': 'success'})
        self.assertEqual(run['swapped'], ['.gd-head', '.gd-statezone', '#rule-sections'])

    def test_saving_a_paused_rule_says_nothing_was_checked(self):
        self.assertEqual(self.obs['edit_paused']['toasts'][-1]['msg'],
                         'Rule saved. It is paused, so nothing was checked.')

    def test_the_bar_and_card_actions(self):
        self.assertEqual(self.obs['pause']['sent'][0]['url'], f'/api/auto-rules/{self.rid}/pause')
        self.assertEqual(self.obs['pause']['swapped'], ['.gd-head', '.gd-statezone', '#rule-sections'])
        self.assertEqual(self.obs['check']['sent'][0]['url'], f'/api/auto-rules/{self.rid}/check')
        self.assertIn('Checked "Cup".', self.obs['check']['toasts'][-1]['msg'])

    def test_delete_goes_back_to_the_list(self):
        run = self.obs['delete']
        self.assertEqual(run['sent'][0], {'url': f'/api/auto-rules/{self.rid}', 'method': 'DELETE',
                                          'body': {'cancel_scheduled': False}})
        self.assertTrue(run['navigated'])

    def test_a_history_filter_swaps_only_its_card_and_keeps_the_url(self):
        run = self.obs['hist_filter']
        self.assertEqual(run['fetched'], [f'http://localhost:5000/rules/{self.rid}?hist=skip'])
        self.assertEqual(run['swapped'], ['#rule-history'])
        self.assertEqual(run['address_bar'], f'/rules/{self.rid}?hist=skip')
        self.assertIn('active', run['active_chip'])

    def test_a_new_tab_click_on_a_filter_is_left_to_the_browser(self):
        self.assertEqual(self.obs['hist_newtab']['fetched'], [])
        self.assertFalse(self.obs['hist_newtab']['prevented'])

    def test_customize_sections_lists_the_cards_in_order(self):
        self.assertEqual(self.obs['sections']['title'], 'Customize sections')
        self.assertEqual(self.obs['sections']['rows'], ['Coming up', 'What it searches for',
                                                        'Checks', 'Settings', 'History'])
        self.assertIn('The same layout applies to every rule.', self.obs['sections']['text'])


if __name__ == '__main__':
    unittest.main()
