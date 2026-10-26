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
"""
import logging
import threading
import time
from datetime import datetime

from . import db
from .db_utils import retry_on_locked
from .fmt_utils import counted, join_labels

log = logging.getLogger(__name__)

SITE_URL = 'https://m3upt.com/'
PLAYLIST_URL = 'https://m3upt.com/iptv'
LISTINGS_URL = 'https://m3upt.com/epg'
ACCOUNT_NAME = 'Example channels (M3UPT)'
#: Free streams have no seat limit, and two lets a recording and a health check run at once.
MAX_CONNECTIONS = 2

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

_lock = threading.Lock()
#: The load in progress, if any: {'account_id', 'started_at', 'step'}. In memory on purpose -
#: it describes a thread in this process, and a restart ends the thread.
_loading: dict = {}


def loading() -> dict | None:
    with _lock:
        return dict(_loading) if _loading else None


def _set_step(step):
    with _lock:
        if _loading:
            _loading['step'] = step


def reset_for_tests():
    with _lock:
        _loading.clear()


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
        _loading.update({'account_id': None, 'started_at': datetime.utcnow(),
                         'step': 'Adding the example account.'})
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
    try:
        _set_step("Fetching M3UPT's channels and listings.")
        run_manual_sync(app, account_id, restart_schedule=True)
        with app.app_context():
            synced, why = _wait_for_sync(account_id)
            if not synced:
                if why is not None:
                    problems.append(why)
                    _report(problems)
                return
            _set_step('Putting the example channels in the TV Guide.')
            test = _place(account_id, problems)
        if test is not None:
            test_id, test_name = test
            _set_step(f'Running a health check on {test_name}.')
            if run_single_channel_test(app, test_id) is None:
                problems.append(f'The health check on {test_name} did not run: another '
                                'health check or a recording was using the tester or the '
                                "account's connections. Test now on the channel's page runs "
                                'it.')
        with app.app_context():
            _report(problems)
    except Exception as exc:
        log.exception('Example channels: the load failed')
        problems.append(f'The load stopped with an error: {exc}')
        with app.app_context():
            _report(problems)
    finally:
        with _lock:
            _loading.clear()


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
    if group_id is not None:
        evaluate_and_reconcile_group(db.session.get(ChannelGroup, group_id))
    elif not any(n in missing for n in GROUP_CHANNELS):
        problems.append(f'A channel group named "{GROUP_NAME}" already exists, so the example '
                        'group was not made.')
    elif len(missing) < len(GUIDE_CHANNELS + GROUP_CHANNELS):
        problems.append('The example channel group was not made, since it needs both Bloomberg '
                        'TV feeds.')
    if test is None:
        problems.append('No example channel was left to run the health check on.')
    return test


def _report(problems):
    from .alerts import create_alert
    if not problems:
        log.info('Example channels: loaded')
        return
    log.warning('Example channels: loaded in part - %s', ' '.join(problems))
    create_alert('EXAMPLES_INCOMPLETE', 'The example channels loaded only in part',
                 '\n'.join(problems), source='examples')


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
