"""
Per-account "live connection slot" tracking.

Shared by recorder.py (recordings) and channel_tester.py (channel tests) to
enforce Account.max_connections / accounts.default_max_connections - the number
of concurrent outbound stream connections this app will open to a single IPTV
provider account at once. Account sync does NOT participate: its own per-account
threading.Lock registry (app/accounts.py _sync_locks) already prevents a given
account from syncing more than once concurrently, and that stays completely
separate - sync is brief HTTP fetches, not a held streaming connection.

In-memory only (module-level dict), not persisted - correct because a process
restart already kills every ffmpeg process it owned (see
recorder.kill_all_active), so there's nothing to recover across a restart.
"""
import logging
import threading
from collections import defaultdict
from typing import List

log = logging.getLogger(__name__)

_lock = threading.Lock()
# account_id -> list of (holder_kind, holder_id) currently holding a slot.
# holder_kind: 'recording' | 'test' | 'preview'
_holders: dict = defaultdict(list)

#: What a holder of each kind is called when a refusal names it to the user.
HOLDER_LABELS = {
    'recording': 'a recording',
    'test': 'a channel test',
    'preview': 'a live preview',
}


def _limit_for_account(account, default_max_connections: int) -> int:
    return account.max_connections or default_max_connections


def try_acquire(account_id: int, holder_kind: str, holder_id) -> bool:
    """Register a slot for (holder_kind, holder_id) if under the account's limit.

    Returns False without registering if already at the limit. Idempotent: if
    this exact holder is already registered, returns True without double-counting
    (so callers like watchdog-triggered segment restarts, which don't re-acquire,
    can't accidentally leak or duplicate a slot even if they did call this again).
    """
    from . import db
    from .config import load_config
    from .database import Account
    # Config read happens before the lock is taken; the account row (which may
    # override with its own max_connections) is still read under the lock, since
    # that must see the current value.
    default_max_connections = load_config().get('accounts', {}).get('default_max_connections', 1)
    with _lock:
        account = db.session.get(Account, account_id)
        if account is None:
            return False
        holders = _holders[account_id]
        key = (holder_kind, holder_id)
        if key in holders:
            return True
        if len(holders) >= _limit_for_account(account, default_max_connections):
            return False
        holders.append(key)
        return True


def at_limit(account_id: int) -> bool:
    """Read-only peek: is this account already at its connection limit?

    For telling a user up front that an action cannot run (the channel detail page's
    "Test now"), never as a substitute for try_acquire() - it registers nothing, so a
    slot can still be taken between the peek and the acquire. The acquire remains the
    only thing that decides.
    """
    from . import db
    from .config import load_config
    from .database import Account
    default_max_connections = load_config().get('accounts', {}).get('default_max_connections', 1)
    with _lock:
        account = db.session.get(Account, account_id)
        if account is None:
            return True
        return len(_holders[account_id]) >= _limit_for_account(account, default_max_connections)


def accounts_without_free_recording_slot(account_ids, exclude_holder=None) -> set:
    """Of `account_ids`, the ones a recording could not take a slot on right now.

    "Free for a recording" is a narrower question than at_limit(): a slot held by a
    channel test is preempted rather than waited on
    (recorder._try_acquire_slot_with_preemption), so a test holder never delays a
    recording and never makes an account look full here. Only 'recording'-kind holders
    count. `exclude_holder` is a (holder_kind, holder_id) pair to discount - a live
    recording asking where it could fail over to must not see its own slot as somebody
    else's.

    Advisory and racy in exactly the way at_limit() documents: this informs *which
    member to prefer*, never whether a recording may connect. try_acquire() remains the
    only thing that decides, and a member on a "full" account is still selectable when
    no alternative exists (dev/changelog/855).

    Batched deliberately. One selection pass asks this once for a group whose members
    may span many accounts, where at_limit() would re-read the config and one Account
    row per member (CLAUDE.md, no hidden I/O in per-row loops).
    """
    from .config import load_config
    from .database import Account
    ids = {a for a in account_ids if a is not None}
    if not ids:
        return set()
    default_max_connections = load_config().get('accounts', {}).get('default_max_connections', 1)
    limits = {a.id: _limit_for_account(a, default_max_connections)
              for a in Account.query.filter(Account.id.in_(ids)).all()}
    full = set()
    with _lock:
        for account_id in ids:
            limit = limits.get(account_id)
            if limit is None:
                # No account row: try_acquire() refuses outright, so this is as
                # unavailable as a full account. Same answer at_limit() gives.
                full.add(account_id)
                continue
            held = sum(1 for h in _holders.get(account_id, ())
                       if h[0] == 'recording' and h != exclude_holder)
            if held >= limit:
                full.add(account_id)
    return full


def describe_holders(account_id: int) -> str:
    """Prose naming what holds this account's slots right now, for a refusal message -
    'a recording', 'a recording and a live preview'. Empty string when nothing does.
    Advisory, like at_limit(): read under the lock, but stale the moment it returns."""
    with _lock:
        kinds = [h[0] for h in _holders.get(account_id, ())]
    labels = [HOLDER_LABELS.get(k, k) for k in dict.fromkeys(kinds)]
    if not labels:
        return ''
    if len(labels) == 1:
        return labels[0]
    return ', '.join(labels[:-1]) + ' and ' + labels[-1]


def release(account_id: int, holder_kind: str, holder_id):
    with _lock:
        holders = _holders.get(account_id)
        if not holders:
            return
        try:
            holders.remove((holder_kind, holder_id))
        except ValueError:
            pass
        if not holders:
            _holders.pop(account_id, None)


def _strip_kind(account_id: int, holder_kind: str) -> List:
    """Release every holder of one kind on account_id and return their holder_ids."""
    with _lock:
        holders = _holders.get(account_id, [])
        stripped = [h for h in holders if h[0] == holder_kind]
        for h in stripped:
            holders.remove(h)
        if not holders:
            _holders.pop(account_id, None)
        return [h[1] for h in stripped]


def preempt_tests_for_slot(account_id: int) -> List[int]:
    """Release every 'test'-kind holder for account_id and return their holder_ids
    (channel_ids), so the caller (recorder.py, when a recording needs the slot a
    running channel test currently holds) can kill each test's ffmpeg process.

    Never releases 'recording'-kind holders - a recording must never force out
    another recording. Recordings always win over tests per product decision;
    this is the mechanism that enforces it.
    """
    return _strip_kind(account_id, 'test')


def preempt_previews_for_slot(account_id: int) -> List[str]:
    """Release every 'preview'-kind holder for account_id and return their holder_ids
    (preview session ids), so recorder.py can stop the preview's ffmpeg through
    app/preview.py. Same doctrine as preempt_tests_for_slot, and the recorder asks this
    one FIRST: a preview is a person looking, a test is a measurement feeding the health
    score, so the look yields before the measurement does (dev/changelog/1018)."""
    return _strip_kind(account_id, 'preview')
