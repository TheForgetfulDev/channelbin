"""The first-run setup steps: what a new install still needs, read from what is set up.

Four steps take an install from empty to its first recording - an account that has synced,
a machine that can record, something in the TV Guide, a recording. The dashboard's banner
leads with the first one that needs the user and goes away by itself once all four are
done; the Get started page lists them with what each is for, adds the optional steps worth
doing next, explains the two kinds of search, and hands off to the Readiness check. Design
record: `dev/mockups/44-first-run-setup.html`, `dev/changelog/1278`, `1279`, `1280`.

**Nothing here is ever stored.** A step is done because the thing exists right now, never
because a flag says it was done once: a stored "setup complete" goes stale the moment
someone deletes their only account, and this answer simply comes back. The one thing kept
in memory is the menu link's "N left", cached the way Readiness caches its own nav counts
and thrown away whenever a fact it was read from moves.

**The facts are Readiness's.** One `readiness.Context` and one evaluation over it answer
every question the steps ask, plus one recording count, so this card and the Readiness
check cannot disagree about the same fact. The account step deliberately does not take the
`account_sync` verdict, though: that one turns red whenever any account is overdue, and the
setup question is whether anything has synced at all. A stale sync on a working install is
Readiness's to report, not a reason for the setup banner to come back.
"""
import logging
import threading
from datetime import datetime

from . import examples, readiness
from .fmt_utils import join_labels, plural
from .tz_utils import ago

log = logging.getLogger(__name__)

#: The step is there now.
DONE = 'done'
#: Something is wrong that the user has to fix; the banner leads with it.
PROBLEM = 'problem'
#: Under way with nothing for the user to do (an account's first sync).
WAITING = 'waiting'
#: Not done, and nothing stands in its way.
TODO = 'todo'
#: Comes after an earlier step that is not done yet.
BLOCKED = 'blocked'

STEP_STATES = (DONE, PROBLEM, WAITING, TODO, BLOCKED)

#: What the banner's title starts with, per state it can lead with. DONE and BLOCKED are
#: absent on purpose: the banner never leads with either (see `_lead`).
LEAD_WORD = {PROBLEM: 'Needs you', WAITING: 'In progress', TODO: 'Next'}

#: How the Get started page draws a step, per state. TODO is two looks: the one core step
#: the user should do now is accented, every other TODO is plain (see `_display`).
DISPLAY = {DONE: 'done', PROBLEM: 'problem', WAITING: 'wait', BLOCKED: 'later'}
NOW = 'now'
PLAIN = 'todo'

#: The Readiness checks behind "This machine can record": everything the Record and
#: Schedule capabilities need from the box itself, plus ffprobe, without which a recording
#: cannot be measured.
MACHINE_CHECKS = ('ffmpeg', 'ffprobe', 'storage_dvr', 'storage_space', 'db_write',
                  'scheduler', 'timezone')

#: The menu link's count is cached as long as Readiness's own nav counts, and keyed on the
#: facts that can move it from another page (see `state_key`).
NAV_CACHE_SECONDS = readiness.NAV_CACHE_SECONDS

_ERROR_CHARS = 140

_nav_lock = threading.Lock()
_nav_cache: dict = {}


def _step(step_id, title, status, state, short=None, link=None, body=None, more=(),
          defer=False, keep_examples=False):
    """`defer`: a TODO the banner and the accent take only once no other core step is left
    to do (see `_lead`). `keep_examples`: the step carries Keep the examples."""
    return {'id': step_id, 'title': title, 'status': status, 'state': state,
            'short': short or state, 'link': link, 'body': body, 'more': list(more),
            'optional': False, 'defer': defer, 'keep_examples': keep_examples}


def _link(label, endpoint, **values):
    from flask import url_for
    return {'label': label, 'url': url_for(endpoint, **values)}


def _first_line(text):
    line = (text or '').strip().splitlines()[0] if (text or '').strip() else 'no reason given'
    return line if len(line) <= _ERROR_CHARS else line[:_ERROR_CHARS - 3].rstrip() + '...'


def _lower_first(text):
    return text[:1].lower() + text[1:]


# ── The four core steps ─────────────────────────────────────────────────────────

def _account_step(ctx):
    """Done once an account of the user's own has synced. The example account opens the later
    steps (they can be tried on it) but never finishes this one: it is a trial until the user
    adds their own account or keeps the examples, which makes them an ordinary account
    (`examples.keep()`, dev/changelog/1288). Until then the step is deferred, so the banner
    leads with the trial's own next step first."""
    title = 'Add your IPTV account(s)'
    accounts = ctx.accounts
    if not accounts:
        return _step('account', title, TODO, 'No account added yet.',
                     'Add the server, username and password your provider sent you. '
                     'ChannelBin fetches the channels and listings from there. No subscription '
                     'to hand? Get started has free example channels to try it with.',
                     _link('Accounts', 'accounts.accounts_list'),
                     body='Your provider sent you either a server address with a username and '
                          'password, or a playlist link. Add it on Accounts with + Add account, '
                          'and any other accounts you have the same way. ChannelBin then fetches '
                          "each one's channel list and program listings, which takes a minute or "
                          'two.')
    own = [a for a in accounts if not a.is_example]
    if own:
        synced = [a for a in own if a.last_sync_at is not None]
        if not synced:
            return _first_sync_step(title, own)
        channels = sum(a.channel_count or 0 for a in own)
        also = (' The example channels are still loaded beside them.'
                if len(own) < len(accounts) else '')
        return _step('account', title, DONE,
                     f'{len(own)} {plural(len(own), "account")}, '
                     f'{channels:,} channels between them.{also}',
                     link=_link('Accounts', 'accounts.accounts_list'))
    if not any(a.last_sync_at is not None for a in accounts):
        return _first_sync_step(title, accounts)
    channels = sum(a.channel_count or 0 for a in accounts)
    return _step('account', title, TODO,
                 f'Only the example channels so far, {channels:,} of them from M3UPT. Add your '
                 'own account on Accounts, or keep the examples if they are all you want.',
                 'Only the example channels so far. Add your own account, or keep the examples '
                 'if they are all you want.',
                 _link('Accounts', 'accounts.accounts_list'),
                 body='The example channels are a trial, and Delete the examples takes them out '
                      'again in one step. Add the account your provider sent you on Accounts '
                      'with + Add account, and this step ticks itself off once it has synced. '
                      'If the example channels are all you want, Keep the examples '
                      'makes them an ordinary account instead.',
                 defer=True, keep_examples=True)


def _first_sync_step(title, accounts):
    """The account step while none of `accounts` has synced yet: failed, running, or waiting
    for its first sync."""
    from .accounts import next_sync_map
    from .database import (ACCOUNT_STATUS_ERROR, ACCOUNT_STATUS_OK, ACCOUNT_STATUS_SYNCING,
                           ACCOUNT_STATUS_UNSYNCED)
    from .routes.recordings import time_until_filter
    failed = [a for a in accounts if a.status == ACCOUNT_STATUS_ERROR]
    if failed:
        a = failed[0]
        return _step('account', title, PROBLEM,
                     f"{a.name}'s first sync failed: {_first_line(a.last_error)}",
                     link=_link(a.name, 'accounts.account_detail', account_id=a.id),
                     body="Check the server, username and password on the account page, where "
                          "the provider's own answer is shown too. ChannelBin tries again at "
                          'the next scheduled sync, or straight away if you use Sync now there.')
    running = [a for a in accounts if a.status == ACCOUNT_STATUS_SYNCING]
    if running:
        a = running[0]
        return _step('account', title, WAITING,
                     f"{a.name}'s first sync is running now. This moves on by itself when "
                     'it finishes.',
                     link=_link(a.name, 'accounts.account_detail', account_id=a.id),
                     body='The sync fetches the channel list and the program listings. Nothing '
                          'needs doing until it finishes.')
    # Never synced and neither failing nor running: added and waiting for its first sync.
    # OK is listed because a status with no sync time is still not a sync that happened.
    pending = [a for a in accounts if a.status in (ACCOUNT_STATUS_UNSYNCED, ACCOUNT_STATUS_OK)]
    if not pending:
        raise ValueError(f'account step: unhandled statuses {sorted({a.status for a in accounts})}')
    a = pending[0]
    when = next_sync_map([a]).get(a.id)
    if when is None:
        state = (f"{a.name} has not synced yet, and its scheduled sync is off. Use Sync now on "
                 'the account page.')
    else:
        state = (f"{a.name}'s first sync starts {time_until_filter(when)}. Nothing to do "
                 'until then, or use Sync now on the account page to start it straight away.')
    return _step('account', title, WAITING, state,
                 link=_link(a.name, 'accounts.account_detail', account_id=a.id),
                 body='The sync fetches the channel list and the program listings. This page '
                      'moves on by itself when it finishes.')


def _machine_step(rows):
    title = 'This machine can record'
    counted = [rows[cid] for cid in MACHINE_CHECKS if not rows[cid]['ignored']]
    bad = [r for r in counted if r['status'] in (readiness.PROBLEM, readiness.UNKNOWN)]
    if bad:
        first = bad[0]
        verb = 'Could not check' if first['status'] == readiness.UNKNOWN else 'Problem with'
        more = f' {len(bad) - 1} more on the Readiness check.' if len(bad) > 1 else ''
        link = first['link'] or _link('Maintenance', 'system.maintenance')
        return _step('machine', title, PROBLEM,
                     f"{verb} {first['short']}: {first['found']}.{more}", link=link,
                     body=f"{first['without']} This step is checked again every time the page "
                          'loads, so it ticks itself off once that is fixed.')
    return _step('machine', title, DONE,
                 f"ffmpeg {_lower_first(rows['ffmpeg']['found'])}. Recording folder: "
                 f"{rows['storage_dvr']['found']}. Times show in {rows['timezone']['found']}.",
                 link=_link('Settings', 'settings.settings'))


def _guide_step(ctx, rows, account_done):
    title = 'Put the channels you watch in the TV Guide'
    guide = rows['guide_content']
    if guide['status'] == readiness.READY:
        return _step('guide', title, DONE, f"{guide['found']} in the guide.",
                     link=_link('Channel Search', 'channels.channel_browser'))
    if not account_done:
        return _step('guide', title, BLOCKED,
                     'Comes after adding an account: there are no channels yet.')
    channels = sum(a.channel_count or 0 for a in ctx.accounts)
    return _step('guide', title, TODO,
                 f'{channels:,} channels came in, none in the guide yet.',
                 f'{channels:,} channels came in and none are in the guide yet. Pick the ones '
                 'you watch in Channel Search.',
                 _link('Channel Search', 'channels.channel_browser'),
                 body="ChannelBin does not put every channel in the guide for you. A provider's "
                      'list is often thousands of channels, most of them nothing you would '
                      'watch, and each channel in the guide is also health-checked on a '
                      'schedule. Find the ones you want in Channel Search with Search Channels, '
                      'and use Add to Guide on each.')


def _record_step(recordings, guide_done):
    title = 'Record something'
    if recordings:
        return _step('record', title, DONE,
                     f'{recordings} {plural(recordings, "recording")} so far, scheduled or '
                     'finished.',
                     link=_link('Recordings', 'recordings.index'))
    if not guide_done:
        return _step('record', title, BLOCKED, 'Comes after the guide has a channel in it.')
    return _step('record', title, TODO, 'Nothing recorded or scheduled yet.',
                 'Open the TV Guide and click a program to record it.',
                 _link('TV Guide', 'guide.guide'),
                 body='Open the TV Guide and click a program to record it, or find it with '
                      'Search Programs (EPG) on Channel Search, where every showing has a Record '
                      'button. If the stream drops partway through, ChannelBin reconnects by '
                      "itself, and the recording's page says what happened and how much was "
                      'lost.')


# ── The optional steps, on the Get started page only ─────────────────────────────

def _test_step(latest, latest_example, account_done, now):
    """Done by a health check on a channel of the user's own. One on an example channel is
    named but does not finish the step: the examples run one themselves, and nothing records
    who started a check."""
    title = 'Check that a channel works'
    if latest is not None:
        name, when = latest
        return _step('test', title, DONE,
                     f'The latest health check was on {name}, {ago(when, now)}.',
                     link=_link('Channel Search', 'channels.channel_browser'))
    if not account_done:
        return _step('test', title, BLOCKED, 'Comes after adding an account.')
    if latest_example is not None:
        name, when = latest_example
        state = (f'Only an example channel has been checked so far: {name}, '
                 f'{ago(when, now)}.')
    else:
        state = 'No channel has been checked yet.'
    return _step('test', title, TODO, state,
                 link=_link('Channel Search', 'channels.channel_browser'),
                 more=[_link('Channel Groups', 'channel_groups.groups_page')],
                 body='A health check opens a channel for a short while and measures what it '
                      'sends: whether it plays, its resolution and frame rate, and whether it '
                      'stalls. Channels in your guide get one on a schedule anyway. To check one '
                      'now, find it with Search Channels, open it, and use Test now at the top of '
                      'its page. Channel Groups lists the health checks that run on a schedule, '
                      'with Test now on each.')


def _group_step(groups, example_groups, account_done):
    """Done by a group of the user's own: never the automatic TV Guide health check, and
    never an example group, which `example_groups` names instead."""
    title = 'Group channels that show the same thing'
    if groups:
        return _step('group', title, DONE,
                     f'{groups} channel {plural(groups, "group")} of your own.',
                     link=_link('Channel Groups', 'channel_groups.groups_page'))
    if not account_done:
        return _step('group', title, BLOCKED, 'Comes after adding an account.')
    state = 'No channel group of your own yet.'
    if example_groups:
        names = join_labels(f'"{n}"' for n in example_groups)
        state += (f' The example {plural(len(example_groups), "group")} {names} '
                  f'{plural(len(example_groups), "shows", "show")} what one is.')
    return _step('group', title, TODO, state,
                 link=_link('Channel Groups', 'channel_groups.groups_page'),
                 body='Providers often carry the same channel more than once: an HD and an SD '
                      'feed, a backup feed, or the same channel on a second account. Put those '
                      'in one channel group and the TV Guide shows them as one row. A recording '
                      'from the group starts on the channel with the best health score and '
                      'moves to another channel in the group if that feed dies partway through. '
                      'Use + New Group on Channel Groups, then + Add channels on the group.')


def _rule_step(rules, account_done):
    title = 'Record a show every time it is on'
    if rules:
        return _step('rule', title, DONE,
                     f'{rules} auto-record {plural(rules, "rule")}.',
                     link=_link('Auto-record', 'auto_rules.rules_page'))
    if not account_done:
        return _step('rule', title, BLOCKED, 'Comes after adding an account.')
    return _step('rule', title, TODO, 'No auto-record rule yet.',
                 link=_link('Auto-record', 'auto_rules.rules_page'),
                 body='An auto-record rule is a Search Programs (EPG) search that ChannelBin keeps '
                      'running for you: every showing it finds is scheduled by itself, including '
                      'ones that only appear in the listings later. Use + New rule on '
                      'Auto-record, search for what you want recorded, then Create rule from '
                      'this search.')


def _notify_step(ctx):
    from .notifications import SERVICE_LABELS
    title = 'Get told when something breaks'
    names = [SERVICE_LABELS.get(s, s) for s in ctx.notify_enabled]
    if names:
        return _step('notify', title, DONE,
                     f'{join_labels(names)} {"is" if len(names) == 1 else "are"} on.',
                     link=_link('Notifications', 'settings.notifications_settings'))
    services = list(SERVICE_LABELS.values())
    return _step('notify', title, TODO, 'No notification service is on.',
                 link=_link('Notifications', 'settings.notifications_settings'),
                 body='Without one, you find out about a failed recording only by opening '
                      'ChannelBin and looking. Add a service on Notifications: '
                      f'{", ".join(services[:-1])} or {services[-1]}.')


# ── Putting it together ─────────────────────────────────────────────────────────

def _lead(steps):
    """The step the banner leads with: one that needs the user beats the next one in order,
    so a missing ffmpeg is said even though the account step comes first. A deferred step
    waits until no other is left to do. Never a BLOCKED step - one of those always has an
    earlier step that is not done."""
    for step in steps:
        if step['status'] == PROBLEM:
            return step
    deferred = None
    for step in steps:
        if step['status'] in (WAITING, TODO):
            if not step['defer']:
                return step
            deferred = deferred or step
        elif step['status'] == BLOCKED:
            raise ValueError(f"step {step['id']} is blocked with nothing before it to do")
    return deferred


def _display(core, optional):
    """Each step's look on the page. The first core TODO is the accented one, unless a core
    step already says it needs the user (a problem) or is under way - those carry their own
    color, and two accented steps would be two answers to "what now". A deferred step is
    accented only once it is the last one to do; an optional step never is."""
    todo = [s for s in core if s['status'] == TODO]
    accent = None
    if not any(s['status'] in (PROBLEM, WAITING) for s in core):
        accent = next((s for s in todo if not s['defer']), None) or next(iter(todo), None)
    for step in core + optional:
        if step['status'] == TODO:
            step['display'] = NOW if step is accent else PLAIN
        elif step['status'] in DISPLAY:
            step['display'] = DISPLAY[step['status']]
        else:
            raise ValueError(f"step {step['id']}: unknown state {step['status']}")


def state_key(sync_sig=None, recordings=None):
    """The facts that move a step from another page or a background job: a sync starting
    or ending, an account added or deleted, a recording scheduled, a guide row added. The
    menu's cached count is thrown away when any of them changes, including the examples
    being kept. The machine step is not
    here - nothing on a page moves it - and is covered by the cache's age instead.

    A caller that already holds the sync signature or the recording count passes it in."""
    from sqlalchemy import func
    from . import db
    from .accounts import sync_signature
    from .database import Account, Channel, ChannelGroup, Recording
    if sync_sig is None:
        sync_sig = sync_signature()
    if recordings is None:
        recordings = db.session.query(func.count(Recording.id)).scalar() or 0
    # Keeping the examples moves the account step without changing how many accounts exist.
    accounts = tuple(db.session.query(func.count(Account.id),
                                      func.coalesce(func.sum(Account.is_example), 0)).one())
    guide = ((db.session.query(func.count(Channel.id))
              .filter(Channel.in_guide.is_(True)).scalar() or 0)  # guide-flag-read-ok: the same own-row count Readiness's guide_content reads
             + (db.session.query(func.count(ChannelGroup.id))
                .filter(ChannelGroup.in_guide.is_(True), ChannelGroup.is_system.is_(False))
                .scalar() or 0))
    return (sync_sig, accounts, recordings, guide)


def _store_nav(result, key):
    with _nav_lock:
        _nav_cache.update({'at': datetime.utcnow(), 'key': key,
                           'value': {'left': result['total'] - result['done'],
                                     'total': result['total']}})


def cached_nav(key):
    """The menu's count if one is cached, fresh and read at `key`; otherwise None. Never
    evaluates - the context processor that renders the menu runs after a page has loaded its
    rows, and an evaluation there would roll every one of them back."""
    with _nav_lock:
        cached = dict(_nav_cache)
    if (cached.get('at') and cached.get('key') == key
            and (datetime.utcnow() - cached['at']).total_seconds() < NAV_CACHE_SECONDS):
        return cached['value']
    return None


def nav_summary(sync_sig=None, recordings=None) -> dict:
    """{'left': n, 'total': 4} for the /api/nav-status poll: from the cache while it is fresh
    and its facts have not moved, otherwise from a new evaluation. Never breaks the poll."""
    key = state_key(sync_sig, recordings)
    value = cached_nav(key)
    if value is not None:
        return value
    try:
        result = evaluate(key=key)
    except Exception as exc:                              # noqa: BLE001 - never break the poll
        log.warning('setup steps: nav summary failed: %s', exc)
        return {'left': 0, 'total': 0}
    return {'left': result['total'] - result['done'], 'total': result['total']}


def reset_for_tests():
    """Drop the cached menu count. Called by tests/support/app.py."""
    with _nav_lock:
        _nav_cache.clear()


def _gather():
    from sqlalchemy import func
    from . import db
    from .database import Recording
    ctx = readiness.Context()
    payload = readiness.evaluate(ctx=ctx)
    rows = {r['id']: r for r in payload['checks']}
    recordings = db.session.query(func.count(Recording.id)).scalar() or 0
    return ctx, payload, rows, recordings


def _summarize(steps):
    for n, step in enumerate(steps, 1):
        if step['status'] not in STEP_STATES:
            raise ValueError(f"step {step['id']}: unknown state {step['status']}")
        step['n'] = n
    done = sum(1 for s in steps if s['status'] == DONE)
    lead = _lead(steps)
    return {
        'steps': steps,
        'done': done,
        'total': len(steps),
        'finished': lead is None,
        'lead': lead,
        'lead_word': LEAD_WORD[lead['status']] if lead else None,
    }


def _has_channels(ctx):
    """Whether any account, the examples included, has synced: what opens the later steps."""
    return any(a.last_sync_at is not None for a in ctx.accounts)


def _core(ctx, rows, recordings):
    account = _account_step(ctx)
    guide = _guide_step(ctx, rows, _has_channels(ctx))
    return [account, _machine_step(rows), guide,
            _record_step(recordings, guide['status'] == DONE)]


def evaluate(key=None) -> dict:
    """Every core step's state, the count done and the step to lead with.

    Builds its own Readiness Context, whose database write probe rolls back the whole
    session transaction - so a route calls this before it loads any ORM row it means to
    render, or every one of them is expired and reloaded lazily.
    """
    if key is None:
        key = state_key()
    ctx, _payload, rows, recordings = _gather()
    result = _summarize(_core(ctx, rows, recordings))
    _store_nav(result, key)
    return result


def page() -> dict:
    """`evaluate()`'s answer plus what only the Get started page shows: the optional steps,
    each step's look, and which capabilities the Maintenance numbers are counting right now.
    The same rule about loading rows first applies."""
    from sqlalchemy import func
    from . import db
    from .database import Account, AutoRule, Channel, ChannelGroup, ChannelTest
    key = state_key()
    ctx, payload, rows, recordings = _gather()
    result = _summarize(_core(ctx, rows, recordings))
    _store_nav(result, key)

    account_done = _has_channels(ctx)

    def latest_test(example):
        row = (db.session.query(Channel.name, ChannelTest.test_started_at)
               .join(Channel, Channel.id == ChannelTest.channel_id)
               .join(Account, Account.id == Channel.account_id)
               .filter(Account.is_example.is_(example))
               .order_by(ChannelTest.id.desc()).first())
        return tuple(row) if row else None

    groups = (db.session.query(func.count(ChannelGroup.id))
              .filter(ChannelGroup.is_system.is_(False), ChannelGroup.is_example.is_(False))
              .scalar() or 0)
    example_groups = [n for (n,) in db.session.query(ChannelGroup.name)
                      .filter(ChannelGroup.is_example.is_(True)).order_by(ChannelGroup.id)]
    rules = db.session.query(func.count(AutoRule.id)).scalar() or 0
    optional = [_test_step(latest_test(False), latest_test(True), account_done, ctx.now),
                _group_step(groups, example_groups, account_done),
                _rule_step(rules, account_done),
                _notify_step(ctx)]
    for n, step in enumerate(optional, len(result['steps']) + 1):
        step['optional'] = True
        step['n'] = n
    _display(result['steps'], optional)
    result['optional'] = optional
    result['examples'] = examples.summary()
    # "Try it with example channels" is offered only on an install with no account at all:
    # once there is one, examples would only clutter it.
    result['offer_examples'] = (examples.offer() if not ctx.accounts and result['examples'] is None
                                else None)

    caps = payload['headline_capabilities']
    blocked = [c for c in caps if c['state'] == readiness.CANNOT]
    degraded = [c for c in caps if c['state'] == readiness.DEGRADED]
    result['readiness'] = {
        'blocked': [c['label'] for c in blocked],
        'degraded': [c['label'] for c in degraded],
        # Whether some of the numbers are the guide being empty, which the steps clear.
        'clears_some': any('guide_content' in c['blockers'] + c['degraded']
                           for c in blocked + degraded),
    }
    return result
