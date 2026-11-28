"""The account page's Activity card: what happened on this account, in one list.

Derived from rows the app already holds and never invented (DESIGN.md §17.3): the account's
sync runs, the alerts raised about it - its own standing alerts (`account:<id>:*`), a refused
login of its (`login:<id>:refused`) and the provider it is on (`provider:<id>:*`) - and the
blocks set on it. Until dev/changelog/1475 the card listed only syncs, which the Sync history
table beside it already listed run by run; the table stays the full sync ledger, and this is
the recent story of everything, cut down by the history-filter chips (app/history_filter.py).
Since dev/changelog/1665 it also lists the account's finished stream checks, and since
dev/changelog/1671 the last time the user turned each of its hosts and logins on or off - the
row's own `switched_at`, so a switch moved twice shows its latest move only. Since
dev/changelog/1672 it lists the blocks set on a login the account holds, too, and since
DESIGN-account-providers.md §14.9 the switches of a host on such a login's own list.

Nine queries however long the account's history - the syncs, the account's login ids, the
alerts, the blocks, the login blocks, the stream checks and the host, login and login-host
switches, all
over the last
`ACTIVITY_DAYS` days - plus one for the recordings a block was tied to. Every entry is
`{'kind', 'ts', 'obj', ...}` with `kind` one of `KINDS`; the template branches on it
and names every kind explicitly.
"""
from __future__ import annotations

from datetime import datetime, timedelta

from sqlalchemy import or_

from . import db
from .account_blocks import NO_END
from .database import (Account, AccountBlock, AccountHost, AccountLogin, AccountStreamCheck,
                       AccountSyncLog, Alert, Login, LoginBlock, LoginHost, Recording,
                       STREAM_CHECK_DONE)
from .history_filter import ALL

#: How far back the card reaches. Beyond it the Sync history table has the syncs and the
#: Alerts page the alerts; a card that reached back forever would count thousands of syncs on
#: a chip and show twelve of them.
ACTIVITY_DAYS = 30
#: Entries shown at first, and added by each "Show more".
ACTIVITY_PAGE = 12

KIND_SYNC, KIND_ALERT, KIND_BLOCK, KIND_STREAM_CHECK = 'sync', 'alert', 'block', 'stream_check'
KIND_SWITCH, KIND_LOGIN_BLOCK = 'switch', 'login_block'
KINDS = (KIND_SYNC, KIND_ALERT, KIND_BLOCK, KIND_STREAM_CHECK, KIND_SWITCH, KIND_LOGIN_BLOCK)

ACTIVITY_FILTERS = (
    (ALL, 'All'),
    ('syncs', 'Syncs'),
    ('alerts', 'Alerts'),
    ('blocks', 'Blocks'),
    ('checks', 'Stream checks'),
    ('switches', 'Hosts and logins'),
)
BUCKET_OF_KIND = {KIND_SYNC: 'syncs', KIND_ALERT: 'alerts', KIND_BLOCK: 'blocks',
                  KIND_STREAM_CHECK: 'checks', KIND_SWITCH: 'switches',
                  KIND_LOGIN_BLOCK: 'blocks'}

#: An alert's dot on the card, by severity (app/alerts.py's four). A severity this table does
#: not know reads as a warning, never as fine.
_DOT_BY_SEVERITY = {'ERROR': 'bad', 'CRIT': 'bad', 'WARN': 'warn', 'INFO': 'ok'}


def bucket_of(entry: dict):
    return BUCKET_OF_KIND.get(entry['kind'])


def alert_sources(account: Account) -> list:
    """The `Alert.source` prefixes that are about this account: its own standing alerts, a
    refusal of one of its listed logins, and the provider it is on. Exact prefixes, so
    `account:1:` never matches `account:12:`."""
    prefixes = [f'account:{account.id}:']
    login_ids = [lid for (lid,) in db.session.query(AccountLogin.login_id)
                 .filter(AccountLogin.account_id == account.id)]
    prefixes += [f'login:{lid}:' for lid in login_ids]
    if account.provider_id is not None:
        prefixes.append(f'provider:{account.provider_id}:')
    return prefixes


def entries(account: Account, now: datetime | None = None) -> list:
    """Every Activity entry for `account` inside the window, newest first."""
    now = now or datetime.utcnow()
    since = now - timedelta(days=ACTIVITY_DAYS)
    out = []

    syncs = (AccountSyncLog.query
             .filter(AccountSyncLog.account_id == account.id,
                     AccountSyncLog.started_at >= since)
             .order_by(AccountSyncLog.started_at.desc()).all())
    out += [{'kind': KIND_SYNC, 'ts': log.started_at, 'obj': log} for log in syncs]

    prefixes = alert_sources(account)
    alerts = (Alert.query
              .filter(Alert.created_at >= since,
                      or_(*[Alert.source.like(p.replace('%', r'\%') + '%', escape='\\')
                            for p in prefixes]))
              .order_by(Alert.created_at.desc()).all())
    out += [{'kind': KIND_ALERT, 'ts': a.created_at, 'obj': a,
             'dot': _DOT_BY_SEVERITY.get(a.severity, 'warn'),
             'cleared': a.dismissed_at is not None and a.cleared_by_app,
             'dismissed': a.dismissed_at is not None and not a.cleared_by_app}
            for a in alerts]

    blocks = (AccountBlock.query
              .filter(AccountBlock.account_id == account.id, AccountBlock.created_at >= since)
              .order_by(AccountBlock.created_at.desc()).all())
    tied = {b.recording_id for b in blocks if b.recording_id is not None}
    recs = ({r.id: r for r in Recording.query.filter(Recording.id.in_(tied)).all()}
            if tied else {})
    for b in blocks:
        rec = recs.get(b.recording_id) if b.recording_id is not None else None
        # A block tied to a recording reads that recording's window live (app/account_blocks.py);
        # one the recording no longer exists for has no window to show and says so.
        # An account block with no stop holds until the user unblocks it (dev/changelog/1667).
        start = b.start_time if b.recording_id is None else (rec.start_time if rec else None)
        stop = b.stop_time if b.recording_id is None else (rec.stop_time if rec else None)
        no_end = b.recording_id is None and b.stop_time is None
        out.append({'kind': KIND_BLOCK, 'ts': b.created_at, 'obj': b, 'recording': rec,
                    'start': start, 'stop': stop, 'no_end': no_end,
                    'active': bool(start and start <= now and (no_end or (stop and now < stop))),
                    'over': bool(stop and stop <= now)})

    # A block on a login this account holds (dev/changelog/1672): every holder lists it, since
    # it keeps ChannelBin off the login on each of them. An Unblock moves its stop to the
    # moment it was ended, so the line says how long it really held.
    login_blocks = (db.session.query(LoginBlock, Login.name)
                    .join(Login, Login.id == LoginBlock.login_id)
                    .join(AccountLogin, AccountLogin.login_id == LoginBlock.login_id)
                    .filter(AccountLogin.account_id == account.id,
                            LoginBlock.created_at >= since).all())
    for b, name in login_blocks:
        stop = b.stop_time or NO_END
        out.append({'kind': KIND_LOGIN_BLOCK, 'ts': b.created_at, 'obj': b, 'login': name,
                    'start': b.start_time, 'stop': b.stop_time, 'no_end': b.stop_time is None,
                    'active': b.start_time <= now < stop})

    # Finished checks only: an open one is the page's banner, and has no result to list yet.
    from .stream_check import view as stream_check_view
    checks = (AccountStreamCheck.query
              .filter(AccountStreamCheck.account_id == account.id,
                      AccountStreamCheck.status == STREAM_CHECK_DONE,
                      AccountStreamCheck.finished_at >= since)
              .order_by(AccountStreamCheck.finished_at.desc()).all())
    for c in checks:
        v = stream_check_view(c, now)
        out.append({'kind': KIND_STREAM_CHECK, 'ts': c.finished_at, 'obj': c, 'view': v,
                    'dot': v['summary_tone'] if v['summary_tone'] != 'faint' else 'warn'})

    # The user's own switch moves: a faint dot, like a block, since the user chose it. A
    # login's switch is this account's only. A sync that makes a row name another host or
    # login clears its time (account_links.list_playlist_rows), so no line names one the user
    # never switched.
    hosts = (AccountHost.query
             .filter(AccountHost.account_id == account.id, AccountHost.switched_at >= since)
             .all())
    out += [{'kind': KIND_SWITCH, 'ts': h.switched_at, 'what': 'host', 'name': h.host,
             'on': h.enabled} for h in hosts]
    logins = (db.session.query(AccountLogin.switched_at, AccountLogin.enabled, Login.name)
              .join(Login, Login.id == AccountLogin.login_id)
              .filter(AccountLogin.account_id == account.id, AccountLogin.switched_at >= since)
              .all())
    out += [{'kind': KIND_SWITCH, 'ts': at, 'what': 'login', 'name': name, 'on': on}
            for at, on, name in logins]
    # A host on a login's own list is switched per login, so every account holding the login
    # lists the move (DESIGN-account-providers.md §14.9).
    login_hosts = (db.session.query(LoginHost.switched_at, LoginHost.enabled, LoginHost.host,
                                    Login.name)
                   .join(Login, Login.id == LoginHost.login_id)
                   .join(AccountLogin, AccountLogin.login_id == LoginHost.login_id)
                   .filter(AccountLogin.account_id == account.id, LoginHost.switched_at >= since)
                   .all())
    out += [{'kind': KIND_SWITCH, 'ts': at, 'what': 'login_host', 'name': host, 'login': login,
             'on': on} for at, on, host, login in login_hosts]

    out.sort(key=lambda e: e['ts'], reverse=True)
    return out
