"""What the Rules pages show: the list (templates/rules.html) and a rule's own page
(templates/rule_detail.html), built from the rules, their ledger and the recordings it names
(app/auto_rules.py, DESIGN-autoschedule.md §8, mockup 42, dev/changelog/1189).

Three things to keep in mind:

* **A rule has exactly one state, and the order of rule_state()'s checks is the precedence.**
  Paused wins over everything (a paused rule is not checked, whatever else is true); a rule
  that cannot run says so before anything about its cap. `last_error` is "could not run" and
  nothing else - a check that ran but could not place a showing writes `last_problems`, which
  is a notice on a running rule, never "Not running".
* **A pick's badge is its recording's status, in the words the Recordings page uses**
  (fmt_utils.rec_status_display), because that is the question once a pick is made. A skip's
  badge is its verdict. Every action and every removed-recording stamp is named.
* **Everything here is a fixed number of queries per page**, however many rules or ledger
  rows there are - the list is a case in tests/test_scaling_pages.py, and so is a rule's page.
  Formatting reads `tz`/`h24` once and hands them down.
"""
import logging
import re
from datetime import datetime, timedelta

from sqlalchemy import func

from . import admission, auto_rules, db
from .database import (
    Account, AutoRule, CANCEL_BEFORE_START, CANCEL_DURING_CAPTURE, CANCEL_GROUP_CHANGE,
    Channel, ChannelGroup, EPGEntry, REC_STATUS_ABORTED, REC_STATUS_COMPLETED,
    REC_STATUS_FAILED, REC_STATUS_SCHEDULED, RULE_MATCH_SCHEDULED, RULE_MATCH_SKIPPED_CAP,
    RULE_MATCH_SKIPPED_EXISTING, RULE_MATCH_SKIPPED_LIMIT, RULE_MATCH_SKIPPED_REPEAT,
    RULE_MATCH_USER_MODIFIED, Recording, RecordingProfile, RuleMatch,
)

log = logging.getLogger(__name__)

STATE_PAUSED = 'paused'
STATE_BROKEN = 'broken'
STATE_NEW = 'new'
STATE_CAP = 'cap'
STATE_ON = 'on'

#: (badge class, badge label, row edge class) for each state. The edge colors the list row's
#: left border the way the recordings list colors a recording's.
STATE_UI = {
    STATE_ON: ('b-done', 'On', 'rs-on'),
    STATE_CAP: ('b-warn', 'At its cap', 'rs-cap'),
    STATE_BROKEN: ('b-fail', 'Not running', 'rs-broken'),
    STATE_PAUSED: ('b-paused', 'Paused', 'rs-paused'),
    STATE_NEW: ('b-sched', 'Not checked', 'rs-new'),
}

#: The cards of a rule's page, in their default order, and what Customize sections calls
#: them. The status bar above them is not one: it is the rule's state and never moves.
SECTIONS = ('coming', 'search', 'checks', 'settings', 'history')
SECTION_NAMES = {'coming': 'Coming up', 'search': 'What it searches for', 'checks': 'Checks',
                 'settings': 'Settings', 'history': 'History'}
#: The user-prefs row holding that order - one layout for every rule.
SECTIONS_PREF_KEY = 'rule_detail_sections'

#: History's filter chips: (key, label). The bucket each row falls in is ledger_entry()'s.
HISTORY_FILTERS = (
    ('all', 'All'),
    ('rec', 'Recorded'),
    ('bad', 'Failed or stopped'),
    ('gone', 'Cancelled or deleted'),
    ('skip', 'Skipped'),
)
HISTORY_FILTER_KEYS = tuple(k for k, _label in HISTORY_FILTERS)
#: How many History rows a page shows at first, and how many more each "Show more" adds.
HISTORY_PAGE = 12

_WARNED = 'Scheduled with a warning:'
#: The engine's retrack sentences (auto_rules._move_recording and its two siblings), picked
#: out of a row's detail so the page can set them apart.
_MOVED = re.compile(r'(Moved with the program[^.]*\.|The guide moved the program to [^.]*\.)')


def rule_state(rule, cap_waiting: int) -> str:
    """The rule's one state. Each check names a state, in precedence order."""
    if not rule.enabled:
        return STATE_PAUSED
    if rule.last_error:
        return STATE_BROKEN
    if rule.last_evaluated_at is None:
        return STATE_NEW
    if cap_waiting:
        return STATE_CAP
    return STATE_ON


def first_check_wait():
    """What a rule's first check is waiting behind right now, as "an account sync is
    running (Account 3)", or None when nothing holds it back. Read from admission's registry
    for display only - the check itself asks admission when it runs."""
    blockers = admission.BLOCKED_BY[admission.KIND_RULES]
    for kind, label, _age in admission.describe_active():
        if kind in blockers:
            return f'{admission.KIND_LABELS[kind]} is running' + (f' ({label})' if label else '')
    return None


def _plural(n, one, many=None):
    return f'{n:,} {one if n == 1 else (many or one + "s")}'


def _cap_of(rule, default_cap):
    return default_cap if rule.max_upcoming is None else rule.max_upcoming


# ---------------------------------------------------------------------------
# The ledger, row by row
# ---------------------------------------------------------------------------

def _never_started(cancel_reason) -> bool:
    return cancel_reason in (CANCEL_BEFORE_START, CANCEL_GROUP_CHANGE)


def ledger_entry(m, rec, names, *, now, tz, h24) -> dict:
    """One ledger row as the page shows it. `m` is a RuleMatch row (columns only), `rec` the
    recording it names - (id, name, status, cancel_reason) - or None, `names` is
    {'channels': {id: name}, 'groups': {id: name}}.

    `bucket` is which History filter the row falls in, or None for a row only All shows (a
    pick still being recorded, one you edited)."""
    from .fmt_utils import rec_status_display
    from .tz_utils import day_label, format_local
    past = m.program_start_time <= now
    action = m.action_taken
    badge, pulse, bucket, fate = None, False, None, ''
    if action == RULE_MATCH_SCHEDULED:
        if rec is not None:
            _section, _edge, cls, label, pulse = rec_status_display(rec.status)
            badge = (cls, label)
            if rec.status == REC_STATUS_COMPLETED:
                bucket = 'rec'
            elif rec.status == REC_STATUS_FAILED:
                bucket = 'bad'
                fate = 'The recording failed, so a later airing of it is not a repeat.'
            elif rec.status == REC_STATUS_ABORTED:
                if rec.cancel_reason == CANCEL_GROUP_CHANGE:
                    bucket, fate = 'gone', 'A change to its channel group called it off.'
                elif _never_started(rec.cancel_reason):
                    bucket, fate = 'gone', 'It was cancelled before it started.'
                elif rec.cancel_reason == CANCEL_DURING_CAPTURE:
                    bucket, fate = 'bad', 'You aborted it while it was recording.'
                else:
                    bucket, fate = 'bad', 'It was cancelled after it started.'
        elif m.removed_status == REC_STATUS_SCHEDULED:
            badge, bucket = ('b-abort', 'Cancelled'), 'gone'
            fate = 'You cancelled it before it started, so this rule will not schedule it again.'
        elif m.removed_status == REC_STATUS_COMPLETED:
            badge, bucket = ('b-abort', 'Deleted'), 'gone'
            fate = 'It was recorded, and later deleted.'
        elif m.removed_status == REC_STATUS_FAILED:
            badge, bucket = ('b-fail', 'Failed'), 'bad'
            fate = 'The recording failed, and was later deleted.'
        elif m.removed_status == REC_STATUS_ABORTED:
            badge = ('b-abort', rec_status_display(REC_STATUS_ABORTED)[3])
            bucket = 'gone' if _never_started(m.removed_cancel_reason) else 'bad'
            fate = 'It was cancelled, and its recording was later deleted.'
        elif m.removed_status is not None:
            badge, bucket = ('b-abort', rec_status_display(m.removed_status)[3]), 'gone'
            fate = 'Its recording was deleted.'
        else:
            # A pick with no recording and no stamp of what happened to it - not something the
            # engine writes. Shown as what is known rather than guessed at.
            log.warning('Auto-record rule #%s: ledger row %s is a pick with no recording and '
                        'no removal stamp', m.rule_id, m.id)
            badge, bucket = ('b-abort', 'Removed'), 'gone'
            fate = 'Its recording is gone, and nothing recorded why.'
    elif action == RULE_MATCH_SKIPPED_EXISTING:
        badge, bucket = ('b-abort', 'Has a recording'), 'skip'
    elif action == RULE_MATCH_SKIPPED_REPEAT:
        badge, bucket = ('b-abort', 'Repeat'), 'skip'
    elif action == RULE_MATCH_SKIPPED_CAP:
        # Upcoming, it is still waiting for room; once aired, it never got any.
        badge, bucket = ('b-warn', 'Cap reached' if past else 'Waiting: cap'), 'skip'
    elif action == RULE_MATCH_SKIPPED_LIMIT:
        badge, bucket = ('b-abort', 'Over the limit'), 'skip'
    elif action == RULE_MATCH_USER_MODIFIED:
        badge = ('b-warn', 'You edited it')
        fate = 'Your edited recording:' if rec is not None else 'Its recording was deleted.'
    else:
        log.error('Auto-record rule #%s: ledger row %s has unknown action %r',
                  m.rule_id, m.id, action)
        badge = ('b-abort', action)

    detail = m.detail or ''
    parts = []
    for i, chunk in enumerate(_MOVED.split(detail)):
        if not chunk:
            continue
        if i % 2:
            parts.append((chunk, 'moved'))
        elif _WARNED in chunk:
            before, _sep, after = chunk.partition(_WARNED)
            if before:
                parts.append((before, ''))
            parts.append((_WARNED + after, 'warned'))
        else:
            parts.append((chunk, ''))

    group = names['groups'].get(m.group_id) if m.group_id is not None else None
    channel = names['channels'].get(m.channel_id) if m.channel_id is not None else None
    return {
        'id': m.id,
        'action': action,
        'start': m.program_start_time,
        'day': day_label(m.program_start_time, now, tz),
        'time': format_local(m.program_start_time, 'clock', tz=tz, h24=h24),
        'title': m.program_title or '',
        'sub': m.program_sub_title or '',
        'where': group or channel or '-',
        'via': channel if group else '',
        'badge': badge,
        'pulse': pulse,
        'detail': parts,
        'warned': _WARNED in detail,
        'moved': bool(_MOVED.search(detail)),
        'fate': fate,
        'rec': {'id': rec.id, 'name': rec.name} if rec is not None else None,
        'bucket': bucket,
        'scheduled': (action == RULE_MATCH_SCHEDULED and rec is not None
                      and rec.status == REC_STATUS_SCHEDULED),
    }


def _ledger_rows(rule_ids):
    """Every ledger row of these rules (columns only), with the recordings and names they
    point at, in four queries however many rows there are."""
    if not rule_ids:
        return [], {}, {'channels': {}, 'groups': {}}
    rows = db.session.query(
        RuleMatch.id, RuleMatch.rule_id, RuleMatch.channel_id, RuleMatch.group_id,
        RuleMatch.program_title, RuleMatch.program_sub_title, RuleMatch.program_start_time,
        RuleMatch.action_taken, RuleMatch.recording_id, RuleMatch.removed_status,
        RuleMatch.removed_cancel_reason, RuleMatch.detail,
    ).filter(RuleMatch.rule_id.in_(sorted(rule_ids))).all()
    rec_ids = sorted({r.recording_id for r in rows if r.recording_id is not None})
    recs = {r.id: r for r in db.session.query(
        Recording.id, Recording.name, Recording.status, Recording.cancel_reason
    ).filter(Recording.id.in_(rec_ids))} if rec_ids else {}
    channel_ids = sorted({r.channel_id for r in rows if r.channel_id is not None})
    group_ids = sorted({r.group_id for r in rows if r.group_id is not None})
    names = {
        'channels': dict(db.session.query(Channel.id, Channel.name).filter(
            Channel.id.in_(channel_ids))) if channel_ids else {},
        'groups': dict(db.session.query(ChannelGroup.id, ChannelGroup.name).filter(
            ChannelGroup.id.in_(group_ids))) if group_ids else {},
    }
    return rows, recs, names


def _cap_waiting(rule_ids, now) -> dict:
    if not rule_ids:
        return {}
    return dict(db.session.query(RuleMatch.rule_id, func.count(RuleMatch.id)).filter(
        RuleMatch.rule_id.in_(sorted(rule_ids)),
        RuleMatch.action_taken == RULE_MATCH_SKIPPED_CAP,
        RuleMatch.program_start_time > now,
    ).group_by(RuleMatch.rule_id))


# ---------------------------------------------------------------------------
# A search in words, for every rule at once
# ---------------------------------------------------------------------------

def _searches(rules, tz, h24, *, with_gone=False) -> dict:
    """{rule id: describe_state() answer, or None for a spelling that no longer parses}.
    The account and channel names every search names are read once."""
    from .channel_search import SearchStateError, describe_state
    states = {}
    for r in rules:
        try:
            states[r.id] = auto_rules.parse_search(r.search_params)
        except SearchStateError:
            states[r.id] = None
    acct_ids, chan_ids = set(), set()
    for st in states.values():
        if st is None:
            continue
        for f in st.filters:
            if f.key == 'acct':
                acct_ids |= {int(v) for v in f.values + f.ex if str(v).isdigit()}
            elif f.key == 'chan':
                chan_ids |= {int(v) for v in f.values + f.ex if str(v).isdigit()}
    accounts = dict(db.session.query(Account.id, Account.name).filter(
        Account.id.in_(sorted(acct_ids)))) if acct_ids else {}
    channels = dict(db.session.query(Channel.id, Channel.name).filter(
        Channel.id.in_(sorted(chan_ids)))) if chan_ids else {}
    known = auto_rules.known_names([s for s in states.values() if s is not None]) \
        if with_gone else None
    out = {}
    for rid, st in states.items():
        if st is None:
            out[rid] = None
            continue
        out[rid] = describe_state(
            st, account_names=accounts, channel_names=channels,
            gone=auto_rules.gone_values(st, known) if known else None, tz=tz, h24=h24)
    return out


def search_urls(rule_id, search_params):
    """(Open search, Edit search) links for a rule. Open search is the stored spelling as
    written - the rule's exact preview; Edit search adds the `rule=<id>` action context
    through to_params(), never by editing the string. A spelling that no longer parses opens
    a blank Programs search in that context, which is how it gets fixed."""
    from dataclasses import replace
    from urllib.parse import urlencode

    from flask import url_for

    from .channel_search import GRAIN_AIRINGS, SearchState, SearchStateError
    base = url_for('channels.channel_browser')
    try:
        state = auto_rules.parse_search(search_params)
    except SearchStateError:
        state = SearchState(grain=GRAIN_AIRINGS)
    return (f'{base}?{search_params}',
            f'{base}?{urlencode(replace(state, rule=str(rule_id)).to_params())}')


def dialog_data(rule, described, edit_search_url) -> dict:
    """What the edit dialog (auto-rule-modal.js openAutoRuleModal, edit mode) starts from:
    the rule's settings, its search as lines of text, and where Edit search goes."""
    if described is None:
        lines = [['Search', 'Its search can no longer be read.']]
    else:
        lines = [[ln['label'], ('not ' if ln['neg'] else '') + ' or '.join(
            v['text'] + (' (no longer exists)' if v['gone'] else '') for v in ln['values'])]
            for ln in described['lines']]
    return {'id': rule.id, 'name': rule.name, 'profile_id': rule.profile_id,
            'skip_repeats': bool(rule.skip_repeats), 'limit_count': rule.limit_count,
            'limit_window_hours': rule.limit_window_hours, 'max_upcoming': rule.max_upcoming,
            'search_lines': lines, 'edit_search_url': edit_search_url}


def _search_tip(described) -> str:
    if described is None:
        return 'What it searches for:\nIts search can no longer be read.'
    lines = [f'{ln["label"]}: {"not " if ln["neg"] else ""}'
             f'{" or ".join(v["text"] for v in ln["values"])}' for ln in described['lines']]
    return 'What it searches for:\n' + '\n'.join(lines)


# ---------------------------------------------------------------------------
# The guide strip on the list
# ---------------------------------------------------------------------------

def guide_strip(now, tz, h24) -> dict:
    """{tone, title, sub} for the line above the list: how far the guide reaches and what
    will refresh it. 'warn' when a rule cannot see a day ahead, or nothing will refresh it."""
    from .tz_utils import ago, format_local
    reach = db.session.query(func.max(EPGEntry.start_time)).scalar()
    total, sync_on, next_sync, last_sync = db.session.query(
        func.count(Account.id),
        func.sum(db.case((Account.sync_enabled.is_(True), 1), else_=0)),
        func.min(db.case((Account.sync_enabled.is_(True), Account.next_sync_at))),
        func.max(Account.last_sync_at),
    ).one()
    sync_on = int(sync_on or 0)
    at = (lambda dt: format_local(dt, 'day_datetime', tz=tz, h24=h24))
    if not total:
        return {'tone': 'warn', 'title': 'There are no accounts yet, so the guide is empty and '
                                         'no rule can schedule anything.',
                'sub': 'Rules can only record what the guide lists. Add an account, and the '
                       'guide fills on its first sync.'}
    if sync_on == 0:
        sync = (f'automatic sync is off on {"the only account" if total == 1 else f"all {total:,} accounts"}')
    else:
        sync = f'{sync_on:,} of {_plural(total, "account")} sync automatically'
    last = f' The last refresh was {ago(last_sync, now)}.' if last_sync else ''
    if reach is None:
        return {'tone': 'warn', 'title': 'The guide is empty, so no rule can schedule anything.',
                'sub': f'Rules can only record what the guide lists, and {sync}.{last}'}
    when = at(reach) if reach > now else f'{at(reach)}, which has passed'
    if reach - now < auto_rules.GUIDE_REACH_MIN:
        nothing = ', and nothing will refresh it' if sync_on == 0 else ''
        nxt = (f' The next refresh is {at(next_sync)}.' if sync_on and next_sync else '')
        return {'tone': 'warn', 'title': f'The guide ends {when}{nothing}.',
                'sub': f'Rules can only schedule what the guide lists, and {sync}. Nothing '
                       f'after that time is scheduled until an account syncs.{nxt}{last}'}
    if sync_on and next_sync:
        tail = f'; the next refresh is {at(next_sync)}.'
    elif sync_on:
        tail = '.'
    else:
        tail = f', but {sync}, so nothing will refresh it.'
    return {'tone': 'muted',
            'title': f'The guide reaches {when}. Rules check it every hour and after every '
                     f'guide refresh{tail}',
            'sub': ''}


# ---------------------------------------------------------------------------
# The list
# ---------------------------------------------------------------------------

def _state_tip(state, rule, scheduled, cap, waiting, wait, now):
    from .tz_utils import ago
    if state == STATE_ON:
        return ('On.\nChecks the guide every hour and after every guide refresh. Last checked '
                f'{ago(rule.last_evaluated_at, now)}.')
    if state == STATE_CAP:
        return (f'At its cap.\n{_plural(scheduled, "recording")} scheduled, the most this rule '
                f'may have at once ({cap:,}). {_plural(waiting, "showing waits", "showings wait")} '
                'until one of them is over.')
    if state == STATE_BROKEN:
        return (f'Not running.\n{rule.last_error}\nIt schedules nothing new until this is fixed. '
                'What it already scheduled still records.')
    if state == STATE_PAUSED:
        return 'Paused.\nIt schedules nothing new. What it already scheduled still records.'
    if state == STATE_NEW:
        return ('Not checked yet.\nIts first check is waiting'
                + (f': {wait}. It runs as soon as that finishes.' if wait
                   else ' to run, at the latest within the hour.'))
    raise ValueError(f'unknown rule state {state!r}')


def list_context(now=None) -> dict:
    """Everything templates/rules.html renders. A fixed number of queries however many
    rules and ledger rows there are."""
    from .config import load_config
    from .tz_utils import ago, day_label, format_local, get_display_tz, is_24h
    now = now or datetime.utcnow()
    tz, h24 = get_display_tz(), is_24h()
    default_cap = auto_rules.default_cap(load_config())
    rules = AutoRule.query.order_by(func.lower(AutoRule.name), AutoRule.id).all()
    ids = [r.id for r in rules]
    scheduled = auto_rules.scheduled_counts(ids)
    waiting = _cap_waiting(ids, now)
    searches = _searches(rules, tz, h24)
    wait = first_check_wait() if any(r.enabled and r.last_evaluated_at is None for r in rules) \
        else None

    # The next recording of each rule: its earliest upcoming pick whose recording is still
    # SCHEDULED. Only the picks are read - a rule holds at most its cap of them.
    picks = db.session.query(
        RuleMatch.id, RuleMatch.rule_id, RuleMatch.channel_id, RuleMatch.group_id,
        RuleMatch.program_title, RuleMatch.program_sub_title, RuleMatch.program_start_time,
        RuleMatch.detail,
    ).join(Recording, Recording.id == RuleMatch.recording_id).filter(
        RuleMatch.rule_id.in_(ids), RuleMatch.action_taken == RULE_MATCH_SCHEDULED,
        RuleMatch.program_start_time > now, Recording.status == REC_STATUS_SCHEDULED,
    ).order_by(RuleMatch.program_start_time, RuleMatch.id).all() if ids else []
    nxt = {}
    for p in picks:
        nxt.setdefault(p.rule_id, p)
    channel_ids = sorted({p.channel_id for p in nxt.values() if p.channel_id is not None})
    group_ids = sorted({p.group_id for p in nxt.values() if p.group_id is not None})
    chan_names = dict(db.session.query(Channel.id, Channel.name).filter(
        Channel.id.in_(channel_ids))) if channel_ids else {}
    group_names = dict(db.session.query(ChannelGroup.id, ChannelGroup.name).filter(
        ChannelGroup.id.in_(group_ids))) if group_ids else {}

    out = []
    for r in rules:
        n = scheduled.get(r.id, 0)
        state = rule_state(r, waiting.get(r.id, 0))
        cls, label, edge = STATE_UI[state]
        described = searches.get(r.id)
        urls = search_urls(r.id, r.search_params)
        p = nxt.get(r.id)
        if state == STATE_NEW:
            next_cell = {'head': 'Waiting for its first check',
                         'sub': wait or 'It runs within the hour'}
        elif p is not None:
            where = group_names.get(p.group_id) or chan_names.get(p.channel_id) or ''
            what = p.program_sub_title or p.program_title or ''
            next_cell = {'day': day_label(p.program_start_time, now, tz),
                         'time': format_local(p.program_start_time, 'clock', tz=tz, h24=h24),
                         'sub': ' · '.join(x for x in (what, where) if x),
                         'warned': _WARNED in (p.detail or '')}
        elif state == STATE_PAUSED:
            next_cell = {'head': 'Nothing scheduled', 'sub': 'Paused, so nothing new is added'}
        elif state == STATE_BROKEN:
            next_cell = {'head': 'Nothing scheduled', 'sub': 'Not running until it is fixed'}
        else:
            next_cell = {'head': 'Nothing in the guide yet',
                         'sub': (f'The guide reaches {day_label(r.last_guide_reach, now, tz)}'
                                 if r.last_guide_reach else 'The guide holds nothing for it')}
        out.append({
            'id': r.id, 'name': r.name, 'enabled': bool(r.enabled), 'state': state,
            'badge': (cls, label), 'edge': edge,
            'badge_tip': _state_tip(state, r, n, _cap_of(r, default_cap), waiting.get(r.id, 0),
                                    wait, now),
            'summary': described['summary'] if described else 'Its search can no longer be read',
            'search_tip': _search_tip(described),
            'search_url': urls[0],
            'edit_search_url': urls[1],
            'dialog': dialog_data(r, described, urls[1]),
            'next': next_cell,
            'problems': bool(r.last_problems) and state != STATE_BROKEN,
            'scheduled': n,
            'checked': ago(r.last_evaluated_at, now, none_value='Never'),
            'checked_at': format_local(r.last_evaluated_at, 'day_datetime', none_value='',
                                       tz=tz, h24=h24),
        })
    return {'rules': out, 'scheduled_total': sum(scheduled.values()),
            'guide': guide_strip(now, tz, h24)}


# ---------------------------------------------------------------------------
# A rule's page
# ---------------------------------------------------------------------------

def _profile_name(rule):
    if rule.profile_id is None:
        return None
    return db.session.query(RecordingProfile.name).filter(
        RecordingProfile.id == rule.profile_id).scalar()


def _lead(rule, profile):
    prof = f'the {profile} profile' if profile else "each showing's own default profile"
    out = f'Records each upcoming showing its search finds, once, with {prof}. '
    out += ('A showing that repeats something already recorded is skipped. ' if rule.skip_repeats
            else 'Repeats are recorded too. ')
    if rule.limit_count and rule.limit_window_hours:
        out += (f'It takes at most {rule.limit_count:,} within any '
                f'{_plural(rule.limit_window_hours, "hour")}. ')
    return out + ('Recordings it makes are ordinary recordings: they fail over, wait for a '
                  'connection and show up in Recordings like any other.')


def settings_items(rule, profile, default_cap) -> list:
    """The Settings card's chips (util.js settingChipsHtml), each opening the edit dialog on
    its own field."""
    cap = _cap_of(rule, default_cap)
    cap_text = (f'{cap:,} scheduled at once' if cap else 'No cap') \
        + (' (the default)' if rule.max_upcoming is None else '')
    limit = (f'At most {rule.limit_count:,} within {_plural(rule.limit_window_hours, "hour")}'
             if rule.limit_count and rule.limit_window_hours else 'None')
    return [
        {'focus': 'profile', 'label': 'Recording profile',
         'value': profile or "Each showing's default profile",
         'tip': "Its padding is what keeps a program that runs long. Each showing's default is "
                'the profile its own Record button would pick.'},
        {'focus': 'repeats', 'label': 'Skip repeats', 'value': 'On' if rule.skip_repeats else 'Off',
         'tip': 'A showing whose title AND episode title both match an earlier airing that was '
                'already recorded is skipped. Showings without an episode title are never '
                'skipped as repeats.'},
        {'focus': 'limit', 'label': 'Limit', 'value': limit,
         'tip': 'At most X within Y hours, measured on start times. Optional; most series '
                'need none.'},
        {'focus': 'cap', 'label': 'Cap', 'value': cap_text,
         'tip': 'The most recordings this rule may have scheduled at once, a guard against a '
                'rule broader than meant.'},
    ]


def _action_message(state, scheduled, cap, cap_waiting, nxt):
    if state == STATE_ON:
        if nxt is not None:
            return f'{_plural(scheduled, "recording")} scheduled. The next is {nxt}.'
        return 'Nothing scheduled: nothing it matches is in the guide yet.'
    if state == STATE_CAP:
        return (f'{_plural(scheduled, "recording")} scheduled; its cap is {cap:,}. '
                f'{_plural(cap_waiting, "showing is", "showings are")} waiting for room.')
    if state == STATE_BROKEN:
        return 'Not running. It schedules nothing new until this is fixed.'
    if state == STATE_PAUSED:
        return 'Paused. It schedules nothing new.'
    if state == STATE_NEW:
        return 'Waiting for its first check.'
    raise ValueError(f'unknown rule state {state!r}')


def _notices(state, rule, scheduled, cap, cap_waiting, wait, now, tz, h24, sync_on):
    """[(tone, strong, text, action)] above the status bar - the rule's problems in words.
    `action` is the data-act of the one button a notice carries, or ''."""
    from .tz_utils import format_local
    out = []
    if state == STATE_BROKEN:
        out.append(('warn', 'This rule is not running.',
                    f'{rule.last_error} It schedules nothing new until this is fixed. The '
                    'recordings it already scheduled still record.', 'edit-search'))
    if rule.last_problems and state != STATE_BROKEN:
        out.append(('warn', 'Its last check could not schedule everything it found.',
                    f'{rule.last_problems} The next check tries again.', ''))
    if state == STATE_CAP:
        out.append(('warn', f'{_plural(cap_waiting, "showing is", "showings are")} waiting for '
                            'room under the cap.',
                    f'This rule may have {cap:,} recordings scheduled at once and has '
                    f'{scheduled:,}. The rest are scheduled as earlier ones finish, if they have '
                    'not started by then. A rule that keeps hitting its cap is usually broader '
                    'than meant - Coming up shows what it picked.', 'edit-search'))
    if state == STATE_PAUSED:
        out.append(('info', 'Paused.',
                    'It schedules nothing new.'
                    + (f' The {_plural(scheduled, "recording")} it already scheduled still '
                       'record.' if scheduled else '')
                    + ' Resuming checks the guide at once.', 'resume'))
    if state == STATE_NEW:
        out.append(('info', 'Not checked yet.',
                    ('Its first check is waiting: ' + wait + '. It runs as soon as that '
                     'finishes, and this page shows what it scheduled.') if wait else
                    'Its first check runs at the latest within the hour, and this page shows '
                    'what it scheduled.', ''))
    if state in (STATE_ON, STATE_CAP):
        reach = rule.last_guide_reach
        if reach is None or reach - now < auto_rules.GUIDE_REACH_MIN:
            head = ('The guide this rule searches is empty.' if reach is None else
                    'The guide this rule searches ends '
                    f'{format_local(reach, "day_datetime", tz=tz, h24=h24)}.')
            out.append(('warn', head,
                        'It cannot schedule anything the guide does not list yet'
                        + (', and automatic sync is off on every account.' if not sync_on
                           else '. The next account sync adds more.'), ''))
    return out


def page_context(rule_id, *, hist='all', shown=HISTORY_PAGE, now=None):
    """Everything templates/rule_detail.html renders, or None for a rule that does not
    exist. `hist` and `shown` are History's filter and how many of its rows to show."""
    from .config import load_config
    from .scheduler import auto_rule_next_check
    from .tz_utils import ago, day_label, format_local, get_display_tz, is_24h
    rule = db.session.get(AutoRule, rule_id)
    if rule is None:
        return None
    now = now or datetime.utcnow()
    tz, h24 = get_display_tz(), is_24h()
    cfg = load_config()
    default_cap = auto_rules.default_cap(cfg)
    keep_days = int(((cfg.get('auto_scheduling') or {}).get('history_keep_days')) or 0)
    scheduled = auto_rules.scheduled_counts([rule_id]).get(rule_id, 0)
    profile = _profile_name(rule)
    described = _searches([rule], tz, h24, with_gone=True).get(rule_id)
    urls = search_urls(rule.id, rule.search_params)
    sync_on = db.session.query(Account.id).filter(Account.sync_enabled.is_(True)).first()

    rows, recs, names = _ledger_rows([rule_id])
    entries = [ledger_entry(m, recs.get(m.recording_id), names, now=now, tz=tz, h24=h24)
               for m in rows]
    coming = sorted((e for e in entries if e['start'] > now), key=lambda e: (e['start'], e['id']))
    aired = sorted((e for e in entries if e['start'] <= now),
                   key=lambda e: (e['start'], e['id']), reverse=True)
    cap_waiting = sum(1 for e in coming if e['action'] == RULE_MATCH_SKIPPED_CAP)
    state = rule_state(rule, cap_waiting)
    cls, label, _edge = STATE_UI[state]
    wait = first_check_wait() if state == STATE_NEW else None
    cap = _cap_of(rule, default_cap)
    nxt = next((e for e in coming if e['scheduled']), None)

    hist = hist if hist in HISTORY_FILTER_KEYS else 'all'
    counts = {k: (len(aired) if k == 'all' else sum(1 for e in aired if e['bucket'] == k))
              for k in HISTORY_FILTER_KEYS}
    filtered = aired if hist == 'all' else [e for e in aired if e['bucket'] == hist]
    shown = max(HISTORY_PAGE, min(int(shown or HISTORY_PAGE), len(filtered) or HISTORY_PAGE))

    next_check_at = None if not rule.enabled else auto_rule_next_check()
    if not rule.enabled:
        next_check = 'None while paused'
    elif state == STATE_NEW and wait:
        next_check = f'When that finishes ({wait})'
    elif next_check_at is not None:
        next_check = (f'{day_label(next_check_at, now, tz)}, '
                      f'{format_local(next_check_at, "clock", tz=tz, h24=h24)}')
    else:
        next_check = 'Within the hour'

    if state == STATE_NEW:
        reach_text = 'Not measured yet'
    elif rule.last_guide_reach is not None:
        reach_text = format_local(rule.last_guide_reach, 'day_datetime', tz=tz, h24=h24)
    elif state == STATE_BROKEN:
        reach_text = '-'
    else:
        reach_text = 'Nothing listed'

    return {
        'rule': {
            'id': rule.id, 'name': rule.name, 'enabled': bool(rule.enabled),
            'search_url': urls[0], 'edit_search_url': urls[1], 'profile_id': rule.profile_id,
            'skip_repeats': bool(rule.skip_repeats), 'limit_count': rule.limit_count,
            'limit_window_hours': rule.limit_window_hours, 'max_upcoming': rule.max_upcoming,
            'scheduled': scheduled,
        },
        'state': state,
        'badge': (cls, label),
        'badge_tip': _state_tip(state, rule, scheduled, cap, cap_waiting, wait, now),
        'lead': _lead(rule, profile),
        'added': format_local(rule.created_at, 'date', tz=tz, h24=h24),
        'changed': (format_local(rule.updated_at, 'date', tz=tz, h24=h24)
                    if rule.updated_at and rule.created_at
                    and rule.updated_at - rule.created_at > timedelta(seconds=1) else None),
        'notices': _notices(state, rule, scheduled, cap, cap_waiting, wait, now, tz, h24,
                            sync_on is not None),
        'action_message': _action_message(
            state, scheduled, cap, cap_waiting,
            f'{nxt["day"]}, {nxt["time"]}' if nxt else None),
        'search': described,
        'dialog': dialog_data(rule, described, urls[1]),
        'settings': settings_items(rule, profile, default_cap),
        'checks': {
            'last': ago(rule.last_evaluated_at, now, none_value='Never'),
            'last_at': format_local(rule.last_evaluated_at, 'day_datetime', none_value='',
                                    tz=tz, h24=h24),
            'next': next_check,
            'reach': reach_text,
            'kept': f'{keep_days:,} days' if keep_days else 'Forever',
        },
        'coming': coming,
        'history': filtered[:shown],
        'history_total': len(aired),
        'history_more': max(0, len(filtered) - shown),
        'history_filter': hist,
        'history_shown': shown,
        'history_page': HISTORY_PAGE,
        'history_filters': [{'key': k, 'label': lbl, 'count': counts[k]}
                            for k, lbl in HISTORY_FILTERS if k == 'all' or counts[k]],
        'keep_days': keep_days,
    }
