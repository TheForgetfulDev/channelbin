"""The account page's Activity card: what happened on this account, in one list.

Derived from rows the app already holds and never invented (DESIGN.md §17.3): the account's
sync runs, the alerts raised about it - its own standing alerts (`account:<id>:*`), a refused
login of its (`login:<id>:refused`) and the provider it is on (`provider:<id>:*`) - and the
blocks set on it. Until dev/changelog/1475 the card listed only syncs, which the Sync history
table beside it already listed run by run; the table stays the full sync ledger, and this is
the recent story of everything, cut down by the history-filter chips (app/history_filter.py).

Four queries however long the account's history - the syncs, the account's login ids, the
alerts and the blocks, all over the last `ACTIVITY_DAYS` days - plus one for the recordings
a block was tied to. Every entry is
`{'kind', 'ts', 'obj', ...}` with `kind` one of `KINDS`; the template branches on it
and names every kind explicitly.
"""
from __future__ import annotations

from datetime import datetime, timedelta

from sqlalchemy import or_

from . import db
from .database import Account, AccountBlock, AccountLogin, AccountSyncLog, Alert, Recording
from .history_filter import ALL

#: How far back the card reaches. Beyond it the Sync history table has the syncs and the
#: Alerts page the alerts; a card that reached back forever would count thousands of syncs on
#: a chip and show twelve of them.
ACTIVITY_DAYS = 30
#: Entries shown at first, and added by each "Show more".
ACTIVITY_PAGE = 12

KIND_SYNC, KIND_ALERT, KIND_BLOCK = 'sync', 'alert', 'block'
KINDS = (KIND_SYNC, KIND_ALERT, KIND_BLOCK)

ACTIVITY_FILTERS = (
    (ALL, 'All'),
    ('syncs', 'Syncs'),
    ('alerts', 'Alerts'),
    ('blocks', 'Blocks'),
)
BUCKET_OF_KIND = {KIND_SYNC: 'syncs', KIND_ALERT: 'alerts', KIND_BLOCK: 'blocks'}

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
        start = b.start_time if b.recording_id is None else (rec.start_time if rec else None)
        stop = b.stop_time if b.recording_id is None else (rec.stop_time if rec else None)
        out.append({'kind': KIND_BLOCK, 'ts': b.created_at, 'obj': b, 'recording': rec,
                    'start': start, 'stop': stop,
                    'active': bool(start and stop and start <= now < stop),
                    'over': bool(stop and stop <= now)})

    out.sort(key=lambda e: e['ts'], reverse=True)
    return out
