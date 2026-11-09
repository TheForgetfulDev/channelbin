"""Several accounts of one provider stalling together is the provider's outage, said once.

A recording's stall is logged on the recording (STALL_DETECTED), and until dev/changelog/1476
that was the whole record: three recordings on three accounts stalling in the same minutes
read as three independent faults, and finding out that two of the accounts were one backend
took an investigation (2026-09-13, DESIGN-account-providers.md §10's follow-up). A provider
row (`providers`, dev/changelog/1170) is the user saying which accounts reach the same
backend, so the correlation is now a fact the app can state: when stalls land on two or more
of a provider's accounts inside `WINDOW`, one WARN alert is raised per provider (refreshed,
not stacked, while the window keeps filling) and each recording in the window gets one
PROVIDER_STALLS_CORRELATED event saying the stall is most likely the provider's.

Two or more ACCOUNTS, not two recordings: one recording failing over across the provider's
accounts and dying on each is the same evidence, and two recordings stalling on one account
is not - that can be the account's own connection. A provider with one account can never
qualify, and a channel on an account with no provider costs one query and nothing else.

The window is in memory, like account_links' roll cooldown: a restart forgets it, which can
only miss a correlation, never invent one. The alert is NOT self-clearing, for the reason
ACCOUNT_LOGIN_REFUSED is not: "the provider recovered" has no cheap signal, and a
self-clearing alert cannot be dismissed by hand. Called from the watchdog's segment-end path
after the stall's own commit, outside its retry closure, and guarded there: a diagnostic
must never harm the capture it describes.
"""
from __future__ import annotations

import logging
import threading
from datetime import datetime, timedelta

from . import db
from .database import (PROVIDER_STALLS_CORRELATED, Account, Alert, Channel, Provider, Recording,
                       add_recording_event)
from .db_utils import retry_on_locked

log = logging.getLogger(__name__)

#: How close together stalls on different accounts have to be to read as one outage.
WINDOW = timedelta(minutes=5)

_lock = threading.Lock()
#: provider_id -> [{'at', 'account_id', 'recording_id', 'noted'}] for stalls inside WINDOW;
#: `noted` is whether that recording already carries the window's event.
_recent: dict[int, list[dict]] = {}
#: Serializes the database half of a correlation. Two recordings stalling in the same poll
#: are the feature's own case, and two watchdog threads both finding no open alert and both
#: creating one is the stacking this module promises not to do. Separate from `_lock` so the
#: in-memory window stays cheap to update.
_record_lock = threading.Lock()


def alert_source(provider_id: int) -> str:
    return f'provider:{provider_id}:stalls'


def note_stall(recording_id: int, channel_id, now: datetime | None = None) -> bool:
    """Record a stall of `recording_id` on `channel_id`; True when it made (or refreshed)
    the provider's correlation alert. One query for the channel's account and provider;
    nothing else happens for a channel on no provider."""
    if channel_id is None:
        return False
    now = now or datetime.utcnow()
    row = (db.session.query(Channel.account_id, Account.provider_id)
           .join(Account, Account.id == Channel.account_id)
           .filter(Channel.id == channel_id).first())
    if row is None or row.provider_id is None:
        return False
    account_id, provider_id = row
    with _lock:
        hits = [h for h in _recent.get(provider_id, []) if h['at'] >= now - WINDOW]
        # A recording already carrying this window's event does not get a second one for
        # its next stall; it joins the window again with that fact remembered.
        already = {h['recording_id'] for h in hits if h['noted']}
        hits.append({'at': now, 'account_id': account_id, 'recording_id': recording_id,
                     'noted': recording_id in already})
        _recent[provider_id] = hits
        if len({h['account_id'] for h in hits}) < 2:
            return False
        to_note = sorted({h['recording_id'] for h in hits if not h['noted']})
        for h in hits:
            h['noted'] = True
        account_ids = sorted({h['account_id'] for h in hits})
        recording_ids = sorted({h['recording_id'] for h in hits})
    with _record_lock:
        _record(provider_id, account_ids, recording_ids, to_note, now)
    return True


def _record(provider_id, account_ids, recording_ids, to_note, now):
    provider = db.session.get(Provider, provider_id)
    pname = provider.name if provider else f'provider {provider_id}'
    accounts = {a.id: a.name for a in Account.query.filter(Account.id.in_(account_ids)).all()}
    recordings = {r.id: r.name for r in Recording.query.filter(Recording.id.in_(recording_ids)).all()}
    minutes = int(WINDOW.total_seconds() // 60)
    rec_list = ', '.join(f'"{recordings.get(rid, f"recording {rid}")}"' for rid in recording_ids)
    acct_list = ', '.join(accounts.get(aid, f'account {aid}') for aid in account_ids)
    detail = (f'Stalls landed on {len(account_ids)} accounts of {pname} ({acct_list}) within '
              f'{minutes} minutes of each other, so this one is most likely the provider\'s, '
              f'not the channel\'s or the account\'s. One alert covers them.')

    @retry_on_locked()
    def _add_events_and_commit():
        for rid in to_note:
            add_recording_event(rid, PROVIDER_STALLS_CORRELATED, detail=detail,
                                extra={'provider_id': provider_id, 'account_ids': account_ids,
                                       'recording_ids': recording_ids})
        db.session.commit()

    if to_note:
        _add_events_and_commit()
    _raise_or_refresh(
        source=alert_source(provider_id),
        title=f'Several accounts on {pname} stalled together',
        body=(f'Within {minutes} minutes, recordings on {len(account_ids)} of the accounts on '
              f'{pname} ({acct_list}) stalled: {rec_list}. That points at the provider, not at '
              f'one channel or account. Each recording restarts and fails over on its own as '
              f'usual; this is so {len(recording_ids)} stalls read as one outage. Dismiss it '
              f'once the provider is back.'))
    log.warning('Provider %s: stalls on %d accounts within %d minutes (recordings %s)',
                pname, len(account_ids), minutes, recording_ids)


def _raise_or_refresh(source: str, title: str, body: str) -> None:
    """One open alert per provider: refresh the open row's text and time while the window
    keeps filling, create one when there is none (or the person dismissed the last).

    # duplicated from app/accounts.py::_raise_or_resolve_standing_alert - the refresh half
    # only. That helper is for alerts the app clears by itself and carries a dismiss branch;
    # this alert is dismissed by a person and never by the app, and
    # tests/test_self_clearing_alerts.py reads a call to that helper as a clearing path, so
    # using it here would either lie about the flag or leave an undismissable alert."""
    from .alerts import create_alert

    @retry_on_locked()
    def _refresh_open_and_commit():
        rows = Alert.query.filter(Alert.alert_type == PROVIDER_STALLS_CORRELATED,
                                  Alert.source == source, Alert.dismissed_at.is_(None)).all()
        if not rows:
            return False
        for a in rows:
            a.title = title[:255]
            a.body = body
            a.created_at = datetime.utcnow()
            a.read_at = None
        db.session.commit()
        return True

    if not _refresh_open_and_commit():
        create_alert(PROVIDER_STALLS_CORRELATED, title=title, body=body, source=source)
