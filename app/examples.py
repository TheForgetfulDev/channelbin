"""Example channels: "Try it with example channels" on the Get started page, Delete the
examples, and Keep the examples.

For someone with no IPTV provider yet, or who wants to see ChannelBin work before giving
it their own: one click adds an account reading M3UPT's public playlist and listings, puts a
few English-language channels in the TV Guide, groups two Bloomberg TV feeds that share one
listing, and runs one health check. M3UPT is a Portuguese list of channels the broadcasters
themselves stream publicly; its playlist and listings are plain public links, so the example
account syncs through the same path as any other M3U account. Design record:
`dev/changelog/1281`.

**What is an example is recorded, never inferred.** `Account.is_example` and
`ChannelGroup.is_example` are set when the rows are created here and nowhere else. Everything
else the load creates hangs off the example account - channels, listings, the listings
source, health checks and their screenshots, cached logos, guide rows, group memberships - and
the account delete (`routes/accounts.py::_delete_account_and_jobs`) already takes all of it.
So Delete the examples is: dissolve the example groups, then delete the example accounts.

**What goes is everything on an example channel, and nothing on the user's own.** An example
channel the user put in the guide or tested goes with the examples, and the copy says so
(dev/changelog/1288). A recording made from an example channel stays (an account delete never
touches recordings); a group of the user's own that holds an example channel keeps its other
channels; an example group the user added a channel of their own to is kept, its marker
cleared, and its timeline says so. A recording still under way or still scheduled on an
example channel refuses the removal and is named, rather than being cancelled for the user.

**Keeping them** (`keep()`) is for a user who wants M3UPT as their provider: it clears both
markers, after which the account and group are ordinary ones that Delete the examples no longer
touches and that finish Get started's account step (dev/changelog/1288).

**The load is shown, not hidden** (dev/changelog/1396). Each of its five steps records its
state and a one-line result as it goes, and `progress()` turns that into Get started's progress
card: live while the load runs, then its report until the examples are deleted, a new load
starts, or the process restarts. A step that did not happen says why on its own line, in
addition to the EXAMPLES_INCOMPLETE alert.
"""
import copy
import logging
import threading
import time
from datetime import datetime

from . import db
from .db_utils import retry_on_locked
from .fmt_utils import counted, first_line, join_labels

log = logging.getLogger(__name__)

SITE_URL = 'https://m3upt.com/'
PLAYLIST_URL = 'https://m3upt.com/iptv'
LISTINGS_URL = 'https://m3upt.com/epg'
ACCOUNT_NAME = 'Example channels (M3UPT)'
#: Nothing measured says M3UPT allows more than one, so the account assumes one. The load
#: needs no more: its one health check is a single channel, and nothing else streams then.
MAX_CONNECTIONS = 1

#: Picked from M3UPT's list on 2026-10-24: English-language, carried with listings, and each
#: opened by ffprobe with no extra headers. Matched by name; one that is gone is named in the
#: EXAMPLES_INCOMPLETE alert rather than replaced.
GUIDE_CHANNELS = ('BBC News', 'France 24 English', 'Al Jazeera English', 'Deutsche Welle',
                  'PBS America')
GROUP_NAME = 'Bloomberg TV (example)'
#: Two feeds sharing one listings id (BloombergTV.us): the nearest thing M3UPT carries to the
#: same channel twice, which is what a channel group is for.
GROUP_CHANNELS = ('Bloomberg TV Europe', 'Bloomberg TV Brasil')
TEST_CHANNEL = 'BBC News'

#: How long the load waits on a first sync another thread is running (the scheduled one can
#: fire while the load's own sync is starting) before it gives up and says so.
SYNC_WAIT_SECONDS = 15 * 60
_SYNC_POLL_SECONDS = 5

#: How many blocking recordings a refusal names.
_BLOCKERS_SHOWN = 5

#: The load's steps, in the order it runs them. Get started shows them as a checklist that
#: moves while the load runs and stays as its report once it is done (see `progress()`).
STEP_ACCOUNT = 'account'
STEP_SYNC = 'sync'
STEP_GUIDE = 'guide'
STEP_GROUP = 'group'
STEP_TEST = 'test'
STEPS = (STEP_ACCOUNT, STEP_SYNC, STEP_GUIDE, STEP_GROUP, STEP_TEST)
STEP_LABELS = {
    STEP_ACCOUNT: 'Add the example account',
    STEP_SYNC: "Sync M3UPT's channels and program listings",
    STEP_GUIDE: f'Add {len(GUIDE_CHANNELS)} channels to the TV Guide',
    STEP_GROUP: 'Make an example channel group',
    STEP_TEST: f'Run a health check on {TEST_CHANNEL}',
}

#: A step's state. SKIPPED is a step an earlier failure stopped.
STEP_PENDING = 'pending'
STEP_RUNNING = 'running'
STEP_DONE = 'done'
STEP_PROBLEM = 'problem'
STEP_SKIPPED = 'skipped'
#: The mark each state draws (an `app/icons.py` meaning); RUNNING draws the spinner instead.
STEP_ICONS = {STEP_PENDING: 'inactive', STEP_RUNNING: None, STEP_DONE: 'ok',
              STEP_PROBLEM: 'warning', STEP_SKIPPED: 'not-set-up'}
_FINISHED_STATES = (STEP_DONE, STEP_PROBLEM, STEP_SKIPPED)

_ERROR_CHARS = 160

_lock = threading.Lock()
#: The load in progress, if any: {'account_id', 'started_at', 'steps', 'refs'}. In memory on
#: purpose - it describes a thread in this process, and a restart ends the thread.
_loading: dict = {}
#: The last load's record once it has finished, the same shape plus 'finished_at': what the
#: progress card reports until the examples are deleted, a new load starts, or a restart.
_last: dict = {}


def loading() -> dict | None:
    with _lock:
        return dict(_loading) if _loading else None


def _new_steps() -> dict:
    return {s: {'state': STEP_PENDING, 'detail': ''} for s in STEPS}


def _set(step, state, detail=''):
    with _lock:
        if _loading:
            _loading.setdefault('steps', _new_steps())[step] = {'state': state,
                                                                 'detail': detail}


def _skip_rest(why):
    with _lock:
        steps = _loading.get('steps') if _loading else None
        for s in STEPS if steps else ():
            if steps[s]['state'] in (STEP_PENDING, STEP_RUNNING):
                steps[s] = {'state': STEP_SKIPPED, 'detail': why}


def _ref(**refs):
    with _lock:
        if _loading:
            _loading.setdefault('refs', {}).update(refs)


def reset_for_tests():
    with _lock:
        _loading.clear()
        _last.clear()


# ── Loading ──────────────────────────────────────────────────────────────────

def start(app) -> tuple[bool, str]:
    """Add the example account and start the thread that syncs it and sets up the rest.
    Returns (started, message). Refused while a load runs or while examples are loaded."""
    from .database import Account
    from .epg_sources import add_url_source, clean_url_source_fields
    from .scheduler import schedule_account_sync

    with _lock:
        if _loading:
            return False, 'The example channels are already loading.'
        steps = _new_steps()
        steps[STEP_ACCOUNT]['state'] = STEP_RUNNING
        _loading.update({'account_id': None, 'started_at': datetime.utcnow(),
                         'steps': steps, 'refs': {}})
    try:
        if Account.query.filter_by(is_example=True).first() is not None:
            with _lock:
                _loading.clear()
            return False, ('The example channels are already loaded. Delete the examples '
                           'first to load them again.')

        @retry_on_locked()
        def _create():
            account = Account(name=ACCOUNT_NAME, account_type='m3u', m3u_url=PLAYLIST_URL,
                              base_url='', username='', password='', url_normalization='disabled',
                              sync_enabled=True, max_connections=MAX_CONNECTIONS,
                              is_example=True)
            db.session.add(account)
            db.session.flush()
            _errors, fields = clean_url_source_fields({'url': LISTINGS_URL}, ACCOUNT_NAME)
            add_url_source(account, fields)
            db.session.commit()
            return account.id

        account_id = _create()
    except Exception:
        with _lock:
            _loading.clear()
        raise
    with _lock:
        _loading['account_id'] = account_id
        _last.clear()
    _set(STEP_ACCOUNT, STEP_DONE, f'Added "{ACCOUNT_NAME}".')
    _set(STEP_SYNC, STEP_RUNNING)
    schedule_account_sync(app, account_id)
    threading.Thread(target=_load, args=(app, account_id), daemon=True,
                     name='examples-load').start()
    log.info('Example channels: account %d added, first sync starting', account_id)
    return True, ('Loading the example channels. The first sync fetches about a thousand '
                  'channels and their listings from M3UPT, which takes a minute or two; this '
                  'page moves on by itself.')


def _load(app, account_id):
    """The thread body: sync, then place the guide channels and the group, then one health
    check. Each part that does not happen is named in one EXAMPLES_INCOMPLETE alert."""
    from .accounts import run_manual_sync
    from .channel_tester import run_single_channel_test

    problems = []
    # A load whose account was deleted under it leaves no report: nothing it made is left.
    keep_record = True
    try:
        run_manual_sync(app, account_id, restart_schedule=True)
        with app.app_context():
            synced, why = _wait_for_sync(account_id)
            if not synced:
                if why is None:
                    keep_record = False
                    return
                problems.append(why)
                _set(STEP_SYNC, STEP_PROBLEM, why)
                _skip_rest('Not done, because the sync did not succeed.')
                _report(problems)
                return
            _set(STEP_SYNC, STEP_DONE, _synced_detail(account_id))
            _set(STEP_GUIDE, STEP_RUNNING)
            test = _place(account_id, problems)
        if test is not None:
            channel_id, test_name = test
            _ref(test_channel_id=channel_id)
            _set(STEP_TEST, STEP_RUNNING, 'Watching the stream and measuring what arrives: '
                                          'picture, sound, bitrate and stalls.')
            test_id = run_single_channel_test(app, channel_id)
            if test_id is None:
                why = (f'The health check on {test_name} did not run: another health check '
                       "or a recording was using the tester or the account's connections. "
                       "Test now on the channel's page runs it.")
                problems.append(why)
                _set(STEP_TEST, STEP_PROBLEM, why)
            else:
                _ref(test_id=test_id)
                with app.app_context():
                    _set(STEP_TEST, *_test_outcome(test_id))
        with app.app_context():
            _report(problems)
    except Exception as exc:
        log.exception('Example channels: the load failed')
        why = f'The load stopped with an error: {exc}'
        problems.append(why)
        _fail_running(why)
        with app.app_context():
            _report(problems)
    finally:
        with _lock:
            if keep_record and _loading:
                _last.clear()
                _last.update(_loading, finished_at=datetime.utcnow())
            _loading.clear()


def _fail_running(why):
    """The step under way when the load raised is the problem; the rest did not happen."""
    with _lock:
        steps = _loading.get('steps') if _loading else None
        if steps:
            stopped = next((s for s in STEPS if steps[s]['state'] == STEP_RUNNING),
                           next((s for s in STEPS if steps[s]['state'] == STEP_PENDING), None))
            if stopped is not None:
                steps[stopped] = {'state': STEP_PROBLEM, 'detail': why}
    _skip_rest('Not done, because the load stopped.')


def _synced_detail(account_id):
    """The sync's result line. The live line counted playlist entries, so the entries the
    sync left out are named here, or the two numbers would disagree with no reason given."""
    from .database import Account, AccountSyncLog
    account = db.session.get(Account, account_id)
    line = (f'Added {account.channel_count or 0:,} channels and synced '
            f'{account.epg_entry_count or 0:,} EPG entries.')
    log_row = (AccountSyncLog.query.filter_by(account_id=account_id)
               .order_by(AccountSyncLog.id.desc()).first())
    dupes = (log_row.skipped_duplicate_stream_ids or 0) if log_row else 0
    unusable = (log_row.skipped_malformed_urls or 0) if log_row else 0
    left_out = []
    if dupes:
        left_out.append(counted(dupes, 'duplicate'))
    if unusable:
        left_out.append(f'{unusable:,} with no usable stream address')
    if left_out:
        line += (f" {counted(dupes + unusable, 'playlist entry', 'playlist entries')} "
                 f'left out: {join_labels(left_out)}.')
    return line


def _test_outcome(test_id) -> tuple[str, str]:
    """(state, detail) for the load's health check, read from its ChannelTest row: the
    verdict, the score it left the channel at, and what the stream measured."""
    from .database import ChannelTest
    from .routes.channel_tests import _test_status_label
    t = db.session.get(ChannelTest, test_id)
    if t is None:
        return STEP_PROBLEM, 'The health check ran, but its result is gone.'
    label = _test_status_label(t)
    score = t.lifetime_score_after if t.lifetime_score_after is not None else t.quality_score
    scored = f' with a health score of {score}' if score is not None else ''
    measured = []
    if t.resolution:
        measured.append(t.resolution + (f' at {round(t.fps, 2):g} fps' if t.fps else ''))
    if t.bitrate_kbps:
        measured.append(f'{t.bitrate_kbps:,.0f} kbps')
    if t.video_codec:
        measured.append(f'{t.video_codec.upper()} video')
    if t.audio_codec:
        measured.append(f'{t.audio_codec.upper()} audio')
    stream = f' {", ".join(measured)}.' if measured else ''
    if label == 'PASS':
        return STEP_DONE, f'Passed{scored}.{stream}'
    if label == 'WARN':
        return STEP_PROBLEM, (f'Played{scored}, with a warning: '
                              f'{first_line(t.error_detail, _ERROR_CHARS).rstrip(".")}.{stream}')
    if label in ('FAIL', 'CANCELLED'):
        return STEP_PROBLEM, f'Did not pass{scored}: {first_line(t.error_detail, _ERROR_CHARS)}'
    raise ValueError(f'health check {test_id}: unhandled label {label}')


def _wait_for_sync(account_id) -> tuple[bool, str | None]:
    """(synced, why not). Waits while another thread holds the account's first sync. A
    deleted account is (False, None): the user removed the examples mid-load, and that is
    not a problem to report."""
    from .database import ACCOUNT_STATUS_OK, ACCOUNT_STATUS_SYNCING, Account
    deadline = time.monotonic() + SYNC_WAIT_SECONDS
    while True:
        db.session.expire_all()
        account = db.session.get(Account, account_id)
        if account is None:
            return False, None
        if account.status != ACCOUNT_STATUS_SYNCING:
            break
        if time.monotonic() >= deadline:
            return False, ('The first sync was still running after '
                           f'{SYNC_WAIT_SECONDS // 60} minutes, so the guide channels, the '
                           'group and the health check were not set up.')
        time.sleep(_SYNC_POLL_SECONDS)
    if account.status == ACCOUNT_STATUS_OK and account.last_sync_at is not None:
        return True, None
    reason = (account.last_error or '').strip().splitlines()
    return False, ('The first sync of the example account did not succeed'
                   + (f': {reason[0]}' if reason else '.')
                   + ' So the guide channels, the group and the health check were not set '
                     'up. Delete the examples and try again later.')


def _by_name(account_id, names):
    """{name: channel} among the example account's channels, the lowest id winning when the
    playlist carries one name twice."""
    from .database import Channel
    rows = (Channel.query.filter(Channel.account_id == account_id, Channel.name.in_(names))
            .order_by(Channel.id).all())
    found = {}
    for ch in rows:
        found.setdefault(ch.name, ch)
    return found


def _place(account_id, problems) -> tuple[int, str] | None:
    """Give the guide channels their rows and make the example group. Returns the id and
    name of the channel to health check, or None."""
    from .channel_groups import (build_group_with_members, evaluate_and_reconcile_group,
                                 group_name_conflict)
    from .database import ChannelGroup
    from .routes.guide import give_own_guide_row

    @retry_on_locked()
    def _place_and_commit():
        found = _by_name(account_id, GUIDE_CHANNELS + GROUP_CHANNELS)
        for name in GUIDE_CHANNELS:
            ch = found.get(name)
            if ch is not None and not ch.in_guide:
                give_own_guide_row(ch)
        members = [found[n].id for n in GROUP_CHANNELS if n in found]
        group_id = None
        if len(members) == len(GROUP_CHANNELS) and not group_name_conflict(GROUP_NAME):
            group_id = build_group_with_members(GROUP_NAME, members, is_example=True).id
        db.session.commit()
        missing = [n for n in GUIDE_CHANNELS + GROUP_CHANNELS if n not in found]
        test = found.get(TEST_CHANNEL) or next(
            (found[n] for n in GUIDE_CHANNELS if n in found), None)
        return missing, group_id, ((test.id, test.name) if test is not None else None)

    missing, group_id, test = _place_and_commit()
    if missing:
        problems.append(f"M3UPT's list no longer carries {join_labels(missing)}, so "
                        f"{'it was' if len(missing) == 1 else 'they were'} left out.")
    placed = [n for n in GUIDE_CHANNELS if n not in missing]
    gone = [n for n in GUIDE_CHANNELS if n in missing]
    if not gone:
        _set(STEP_GUIDE, STEP_DONE, f'Added {join_labels(placed)}.')
    else:
        added = f'Added {join_labels(placed)}. ' if placed else ''
        _set(STEP_GUIDE, STEP_PROBLEM,
             f"{added}M3UPT's list no longer carries {join_labels(gone)}, so "
             f"{'it was' if len(gone) == 1 else 'they were'} left out.")
    _set(STEP_GROUP, STEP_RUNNING)
    if group_id is not None:
        _ref(group_id=group_id)
        evaluate_and_reconcile_group(db.session.get(ChannelGroup, group_id))
        _set(STEP_GROUP, STEP_DONE, f'Made "{GROUP_NAME}": two Bloomberg TV feeds that share '
                                    'one program listing.')
    elif not any(n in missing for n in GROUP_CHANNELS):
        why = (f'A channel group named "{GROUP_NAME}" already exists, so the example group was '
               'not made.')
        problems.append(why)
        _set(STEP_GROUP, STEP_PROBLEM, why)
    else:
        why = 'The example channel group was not made, since it needs both Bloomberg TV feeds.'
        # The list carrying none of the channels is already named above.
        if len(missing) < len(GUIDE_CHANNELS + GROUP_CHANNELS):
            problems.append(why)
        _set(STEP_GROUP, STEP_PROBLEM, why)
    if test is None:
        why = 'No example channel was left to run the health check on.'
        problems.append(why)
        _set(STEP_TEST, STEP_PROBLEM, why)
    return test


def _report(problems):
    from .alerts import create_alert
    if not problems:
        log.info('Example channels: loaded')
        return
    log.warning('Example channels: loaded in part - %s', ' '.join(problems))
    create_alert('EXAMPLES_INCOMPLETE', 'The example channels loaded only in part',
                 '\n'.join(problems), source='examples')


def _sync_detail(account_id) -> tuple[str, float]:
    """What the running sync is doing right now, from its own live progress, and how far
    through the sync step that is (0 to 1) for the progress bar."""
    from .accounts import get_sync_progress
    from .search_index import rebuild_in_progress
    p = get_sync_progress(account_id) if account_id is not None else None
    # The sync's closing index rebuild runs while its progress still reads the listings
    # phase it finished on.
    if (p is None or p['phase'] == 'epg') and rebuild_in_progress():
        return 'Indexing the channels and listings for search.', .9
    if p and p['phase'] == 'channels':
        total = p['total']
        # Playlist entries, not channels: the sync leaves some out, which the done line says.
        if total:
            return (f"Reading M3UPT's playlist: {p['done']:,} of {total:,} entries.",
                    .1 + .4 * p['done'] / total)
        return f"Reading M3UPT's playlist: {p['done']:,} entries so far.", .3
    if p and p['phase'] == 'epg':
        return f"Reading program listings: {p['done']:,} so far.", .7
    return "Downloading M3UPT's playlist. The whole sync takes a minute or two.", .05


def progress() -> dict | None:
    """The load's checklist for Get started's progress card: the load under way, or the
    last one's report once it has finished, or None. The running sync's line is read live;
    each step that made something links to it. A fixed handful of lookups, never per row,
    and it builds URLs, so it runs inside a request."""
    from flask import url_for
    from .database import Account, Channel, ChannelGroup
    from .fmt_utils import fmt_duration
    from .routes.channels import account_channels_url, airing_search_url
    with _lock:
        run = copy.deepcopy(_loading or _last)
    if not run:
        return None
    account_id = run.get('account_id')
    if account_id is not None and db.session.get(Account, account_id) is None:
        return None
    finished = 'finished_at' in run
    steps = run.get('steps') or _new_steps()
    refs = run.get('refs') or {}

    links = {STEP_GUIDE: ('Open the TV Guide', url_for('guide.guide'))}
    if account_id is not None:
        links[STEP_ACCOUNT] = ('View the account',
                               url_for('accounts.account_detail', account_id=account_id))
        links[STEP_SYNC] = ('View the channels', account_channels_url(account_id))
    if refs.get('group_id') and db.session.get(ChannelGroup, refs['group_id']) is not None:
        links[STEP_GROUP] = ('View the group',
                             url_for('channel_groups.group_detail', group_id=refs['group_id']))
    if (refs.get('test_id') and refs.get('test_channel_id')
            and db.session.get(Channel, refs['test_channel_id']) is not None):
        links[STEP_TEST] = ('View the result', url_for(
            'channels.channel_detail', channel_id=refs['test_channel_id']) + '#cd-tests')

    view, partial = [], 0.0
    for s in STEPS:
        state, detail = steps[s]['state'], steps[s]['detail']
        if s == STEP_SYNC and state == STEP_RUNNING and not finished:
            detail, partial = _sync_detail(account_id)
        link = links.get(s) if state in (STEP_DONE, STEP_PROBLEM) else None
        view.append({'id': s, 'label': STEP_LABELS[s], 'state': state,
                     'icon': STEP_ICONS[state], 'spinning': state == STEP_RUNNING,
                     'detail': detail,
                     'link': {'label': link[0], 'url': link[1]} if link else None})
    over = sum(1 for s in STEPS if steps[s]['state'] in _FINISHED_STATES)
    clean = all(steps[s]['state'] == STEP_DONE for s in STEPS)
    end = run.get('finished_at') or datetime.utcnow()
    return {
        'steps': view,
        'finished': finished,
        'clean': finished and clean,
        'elapsed': fmt_duration((end - run['started_at']).total_seconds(), with_seconds=True),
        'pct': 100 if finished else round(100 * (over + partial) / len(STEPS)),
        'sig': ' '.join(f"{s}:{steps[s]['state']}" for s in STEPS) + (' finished' if finished else ''),
        'search_url': airing_search_url(),
    }


def offer() -> dict:
    """What the Get started page says the button will add, from the same constants the load
    reads, so the promise and the load cannot drift apart."""
    return {'site_url': SITE_URL, 'account_name': ACCOUNT_NAME,
            'guide': join_labels(GUIDE_CHANNELS), 'guide_count': len(GUIDE_CHANNELS),
            'group_name': GROUP_NAME, 'test_channel': TEST_CHANNEL}


# ── What is loaded, and removing it ─────────────────────────────────────────

def summary() -> dict | None:
    """What the examples are right now, for the Get started page and the account page, or
    None when there are none and none are loading. A few counting queries, never per row."""
    from sqlalchemy import func
    from .database import Account, Channel, ChannelGroup
    accounts = Account.query.filter_by(is_example=True).order_by(Account.id).all()
    groups = ChannelGroup.query.filter_by(is_example=True).order_by(ChannelGroup.id).all()
    load = loading()
    if not accounts and not groups and load is None:
        return None
    ids = [a.id for a in accounts]
    guide = (db.session.query(func.count(Channel.id))
             .filter(Channel.account_id.in_(ids), Channel.in_guide.is_(True))  # guide-flag-read-ok: the example channels holding their own row, which the load gave them
             .scalar() or 0) if ids else 0
    return {
        'accounts': [{'id': a.id, 'name': a.name, 'channels': a.channel_count or 0}
                     for a in accounts],
        'channels': sum(a.channel_count or 0 for a in accounts),
        'guide': guide,
        'groups': [{'id': g.id, 'name': g.name} for g in groups],
        'loading': load,
        'site_url': SITE_URL,
    }


def _blocking_recordings(account_ids, group_ids):
    """Recordings that refuse the removal: under way or scheduled, on an example channel or
    an example group that would be dissolved."""
    from sqlalchemy import or_
    from .database import (Channel, REC_STATUS_SCHEDULED, RESTART_BLOCKING_STATUSES,
                           Recording)
    scopes = []
    if account_ids:
        scopes.append(Recording.channel_id.in_(
            db.session.query(Channel.id).filter(Channel.account_id.in_(account_ids))))
    if group_ids:
        scopes.append(Recording.group_id.in_(group_ids))
    if not scopes:
        return []
    return (Recording.query
            .filter(Recording.status.in_((REC_STATUS_SCHEDULED,) + tuple(RESTART_BLOCKING_STATUSES)),
                    or_(*scopes))
            .order_by(Recording.start_time).all())


def plan() -> dict:
    """What Delete the examples would do right now, and what stops it. Read by the confirm
    dialog and again by the removal itself, so the dialog cannot promise one thing while the
    removal does another."""
    from .database import (ACCOUNT_STATUS_SYNCING, Account, Channel, ChannelGroup,
                           ChannelGroupMember, ChannelTest, OD_JOB_STATUS_RUNNING,
                           REC_STATUS_SCHEDULED)
    from .tz_utils import format_local
    accounts = Account.query.filter_by(is_example=True).order_by(Account.id).all()
    account_ids = [a.id for a in accounts]
    example_groups = ChannelGroup.query.filter_by(is_example=True).order_by(ChannelGroup.id).all()

    dissolve, keep = [], []
    for g in example_groups:
        theirs = [m for m in g.memberships
                  if m.channel is not None and m.channel.account_id not in account_ids]
        (keep if theirs else dissolve).append(g)

    touched = []
    if account_ids:
        touched = (ChannelGroup.query
                   .join(ChannelGroupMember, ChannelGroupMember.group_id == ChannelGroup.id)
                   .join(Channel, Channel.id == ChannelGroupMember.channel_id)
                   .filter(Channel.account_id.in_(account_ids),
                           ChannelGroup.is_example.is_(False))
                   .distinct().order_by(ChannelGroup.name).all())

    blockers = []
    if loading() is not None:
        blockers.append('The example channels are still loading. Wait for that to finish.')
    for a in accounts:
        if a.status == ACCOUNT_STATUS_SYNCING:
            blockers.append(f'"{a.name}" is syncing. Wait for the sync to finish.')
    for g in dissolve:
        if g.check is not None and g.check.status == OD_JOB_STATUS_RUNNING:
            blockers.append(f'The health check on "{g.name}" is running. Wait for it to finish.')
    recs = _blocking_recordings(account_ids, [g.id for g in dissolve])
    for rec in recs[:_BLOCKERS_SHOWN]:
        if rec.status == REC_STATUS_SCHEDULED:
            blockers.append(f'"{rec.name}" is scheduled on an example channel for '
                            f'{format_local(rec.start_time, style="datetime")}. Cancel it '
                            'first, or record it from a channel of your own.')
        else:
            blockers.append(f'"{rec.name}" is still recording, joining or converting on an '
                            'example channel. Wait for it to finish, or abort it.')
    if len(recs) > _BLOCKERS_SHOWN:
        blockers.append(f'And {counted(len(recs) - _BLOCKERS_SHOWN, "more recording")}.')

    lines = []
    for a in accounts:
        lines.append(f'The account "{a.name}" and its {a.channel_count or 0:,} channels, with '
                     'their listings.')
    if account_ids:
        # Counted rather than listed by kind: nothing records whether the load or the user put
        # a channel in the guide or ran a check, and both go (dev/changelog/1288).
        example_channels = db.session.query(Channel.id).filter(Channel.account_id.in_(account_ids))
        guide = (Channel.query.filter(Channel.account_id.in_(account_ids),
                                      Channel.in_guide.is_(True))  # guide-flag-read-ok: the example channels' own guide rows, which the delete takes
                 .count())
        tests = ChannelTest.query.filter(ChannelTest.channel_id.in_(example_channels)).count()
        if guide:
            lines.append(f'{counted(guide, "example channel")} in your TV Guide, including any '
                         'you put there yourself.')
        if tests:
            lines.append(f'{counted(tests, "health check result")} on example channels, '
                         'including any you ran yourself.')
    for g in dissolve:
        lines.append(f'The channel group "{g.name}".')
    for g in keep:
        lines.append(f'"{g.name}" stays, because you added a channel of your own to it. It '
                     'loses its example channels and is no longer marked as an example.')
    for g in touched:
        lines.append(f'Your group "{g.name}" keeps its own channels and loses the example '
                     'ones.')
    if account_ids:
        lines.append('Recordings you made from example channels stay where they are.')
    return {
        'accounts': [{'id': a.id, 'name': a.name} for a in accounts],
        'dissolve': [{'id': g.id, 'name': g.name} for g in dissolve],
        'keep': [{'id': g.id, 'name': g.name} for g in keep],
        'touched': [g.name for g in touched],
        'lines': lines,
        'blockers': blockers,
        'nothing': not accounts and not example_groups,
    }


def remove() -> tuple[bool, str]:
    """Delete the examples. Returns (removed, message); a refusal names every reason."""
    from .database import ChannelGroup, ChannelGroupEvent, GROUP_EXAMPLE_KEPT
    from .routes.accounts import _delete_account_and_jobs
    from .routes.channel_groups import dissolve_group

    p = plan()
    if p['nothing']:
        return False, 'There are no example channels to remove.'
    if p['blockers']:
        return False, 'The examples were not removed. ' + ' '.join(p['blockers'])

    for g in p['keep']:
        @retry_on_locked()
        def _keep(group_id=g['id']):
            group = db.session.get(ChannelGroup, group_id)
            if group is None:
                return
            group.is_example = False
            db.session.add(ChannelGroupEvent(
                group_id=group.id, event_type=GROUP_EXAMPLE_KEPT,
                detail='Kept when the example channels were removed, because a channel of '
                       'your own is in it. Its example channels went with the examples.'))
            db.session.commit()
        _keep()
    for g in p['dissolve']:
        dissolve_group(g['id'])

    removed = []
    for a in p['accounts']:
        deleted, name_or_reason = _delete_account_and_jobs(a['id'])
        if not deleted:
            done = (f' Already removed: {join_labels(removed)}.' if removed else '')
            return False, f'The examples were removed only in part. {name_or_reason}{done}'
        if name_or_reason:
            removed.append(f'"{name_or_reason}"')
    with _lock:
        _last.clear()
    log.info('Example channels: removed %s, %d group(s) dissolved, %d kept',
             join_labels(removed) or 'no account', len(p['dissolve']), len(p['keep']))
    kept = join_labels(f'"{g["name"]}"' for g in p['keep'])
    return True, ('The example channels are removed. '
                  + (f'{kept} stayed, because a channel of your own is in it. ' if kept else '')
                  + 'Your recordings and your own accounts are untouched.')


def keep() -> tuple[bool, str]:
    """Keep the examples: every example account and group stops being an example and
    stays as an ordinary one. Returns (kept, message). Refused while a load runs, since the
    load is still adding to what it would keep."""
    from .database import Account, ChannelGroup, ChannelGroupEvent, GROUP_EXAMPLE_KEPT
    if loading() is not None:
        return False, 'The example channels are still loading. Wait for that to finish.'

    @retry_on_locked()
    def _keep_and_commit():
        accounts = Account.query.filter_by(is_example=True).order_by(Account.id).all()
        groups = ChannelGroup.query.filter_by(is_example=True).order_by(ChannelGroup.id).all()
        for a in accounts:
            a.is_example = False
        for g in groups:
            g.is_example = False
            db.session.add(ChannelGroupEvent(
                group_id=g.id, event_type=GROUP_EXAMPLE_KEPT,
                detail='Kept with Keep the examples. It is an ordinary channel group '
                       'now, and Delete the examples no longer touches it.'))
        db.session.commit()
        return [a.name for a in accounts], [g.name for g in groups]

    accounts, groups = _keep_and_commit()
    if not accounts and not groups:
        return False, 'There are no example channels to keep.'
    log.info('Example channels: kept as the user\'s own - accounts %s, groups %s',
             join_labels(accounts) or 'none', join_labels(groups) or 'none')
    return True, ('The example channels are yours now. Delete the examples no longer applies '
                  'to them; rename or delete them like any other account or channel group.')
