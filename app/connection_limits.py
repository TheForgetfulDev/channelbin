"""
Per-account "live connection slot" tracking.

Shared by recorder.py (recordings), channel_tester.py (channel tests) and preview.py
(live previews) to enforce Account.max_connections / accounts.default_max_connections - the
number of concurrent outbound stream connections this app will open to a single IPTV
provider account at once. Account sync does NOT participate: its own per-account
threading.Lock registry (app/accounts.py _sync_locks) already prevents a given account
from syncing more than once concurrently, and that stays completely separate - sync is
brief HTTP fetches, not a held streaming connection.

Seats are counted per POOL (DESIGN-account-providers.md §5.2, dev/changelog/1169). An
account with no listed logins has one pool, keyed by its account id and sized by its
limit - exactly the counter this was before logins existed. An account with logins has
one pool per login, keyed `('login', login_id)` and sized by that login's seats;
try_acquire() walks them in list order and takes the first free seat, and the launch site
renders the capture with that login's credentials (account_links.render_held_login).
Which pool a holder was counted in is written down at acquire and read back at release,
so a login list edited while a seat is held releases from the pool it was taken in and
never re-resolves.

In-memory only (module-level dicts), not persisted - correct because a process restart
already kills every ffmpeg process it owned (see recorder.kill_all_active), so there's
nothing to recover across a restart.
"""
import logging
import threading
from collections import defaultdict
from datetime import datetime
from typing import List, Optional

log = logging.getLogger(__name__)

_lock = threading.Lock()
# pool -> list of (holder_kind, holder_id) currently holding a seat in it.
#   pool: the account id itself (int) for an account with no logins - its one pool, as
#         it always was - or ('login', login_id) for each login the account holds.
#   holder_kind: 'recording' | 'test' | 'preview'
_holders: dict = defaultdict(list)
# (holder_kind, holder_id) -> (account_id, pool): where each holder was counted, written at
# acquire and read at release, so the pool never has to be re-derived from a list that may
# have been edited since.
_seat_of: dict = {}

#: What a holder of each kind is called when a refusal names it to the user.
HOLDER_LABELS = {
    'recording': 'a recording',
    'test': 'a channel test',
    'preview': 'a live preview',
}


def login_pool(login_id: int) -> tuple:
    return ('login', login_id)


def limit_for_account(account, default_max_connections: int, seats: Optional[list] = None) -> int:
    """The account's configured limit, before any block (app/account_blocks.py) is taken
    out of it: the sum of its logins' seats when it holds any (`seats`, the rows from
    account_links.login_seats_for_accounts), else its own max_connections or the global
    default. What a slot decision compares against is _usable_limits()."""
    if seats:
        return sum(s for _, s, _ in seats)
    return account.max_connections or default_max_connections


def limits_for_accounts(accounts, default_max_connections: int) -> dict:
    """{account_id: configured limit} for `accounts` (rows), batched: one query for every
    account's logins rather than one per account (CLAUDE.md, no hidden I/O in loops)."""
    from .account_links import login_seats_for_accounts
    rows = list(accounts)
    seats = login_seats_for_accounts(a.id for a in rows)
    return {a.id: limit_for_account(a, default_max_connections, seats.get(a.id)) for a in rows}


def _pool_taken_now(account_ids, seats: dict) -> dict:
    """{pool: slots a block takes right now} for the pools of `account_ids` (`seats`, their
    login rows) - read BEFORE _lock is taken, since it is a query and the lock guards only
    the in-memory holder list. A block on an account sharing one of their logins counts on
    that login's pool too (dev/changelog/1170); the sibling lookup runs only when one of
    them lists a login, so an account with none asks exactly what it always did."""
    from .account_blocks import blocked_slots, blocks_at, pool_taken
    from .account_links import login_seats_for_accounts, login_siblings
    ids = set(account_ids)
    wider = set(ids)
    if any(seats.get(a) for a in ids):
        wider |= {other for found in login_siblings(ids).values() for other in found}
    merged = blocked_slots(blocks_at(account_ids=wider))
    if not merged:
        return {}
    missing = set(merged) - set(seats)
    all_seats = dict(seats)
    if missing:
        all_seats.update(login_seats_for_accounts(missing))
    return pool_taken(merged, all_seats)


def _usable_limits(account, default_max_connections: int, taken: dict,
                   seats: list) -> list:
    """[(pool, usable limit, last_refused_at), ...] for `account`, in the order seats are
    taken. `taken` is _pool_taken_now(): a block takes its slots from the blocked account's
    pools in order (None = every slot), so a fully blocked account answers 0 on every pool,
    which is what turns every acquire on it into the refusal its caller already handles
    (dev/changelog/1151) - and a login it shares answers 0 for the sibling too."""
    from .account_blocks import effective_limit
    if seats:
        pools = [(login_pool(lid), s, refused) for lid, s, refused in seats]
    else:
        pools = [(account.id, limit_for_account(account, default_max_connections), None)]
    if not taken:
        return pools
    return [(pool, effective_limit(limit, taken[pool]) if pool in taken else limit, refused)
            for pool, limit, refused in pools]


def _seats_now(account_ids) -> dict:
    from .account_links import login_seats_for_accounts
    return login_seats_for_accounts(account_ids)


def _refused(refused_at, now, cooldown) -> bool:
    from .account_links import is_refused_now
    return is_refused_now(refused_at, now, cooldown)


def _held_in(pool) -> list:
    """The holders of `pool` without creating an entry for an empty one."""
    return _holders.get(pool, [])


def try_acquire(account_id: int, holder_kind: str, holder_id) -> bool:
    """Register a seat for (holder_kind, holder_id) on one of the account's pools if any
    has one free.

    Returns False without registering if every pool is at its limit. Idempotent: if this
    exact holder is already registered on the account, returns True without double-counting
    (so callers like watchdog-triggered segment restarts, which don't re-acquire, can't
    accidentally leak or duplicate a slot even if they did call this again).

    With logins listed, the pools are walked in list order and a login refused inside the
    refusal cooldown is passed over when another has a free seat; when none other has one,
    the refused login is tried again (DESIGN-account-providers.md §5.4).
    """
    from . import db
    from .account_links import refusal_cooldown
    from .config import load_config
    from .database import Account
    # Config read happens before the lock is taken; the account row (which may
    # override with its own max_connections) is still read under the lock, since
    # that must see the current value.
    cfg = load_config()
    default_max_connections = cfg.get('accounts', {}).get('default_max_connections', 1)
    cooldown = refusal_cooldown(cfg)
    all_seats = _seats_now([account_id])
    seats = all_seats.get(account_id, [])
    taken = _pool_taken_now([account_id], all_seats)
    with _lock:
        account = db.session.get(Account, account_id)
        if account is None:
            return False
        pools = _usable_limits(account, default_max_connections, taken, seats)
        key = (holder_kind, holder_id)
        if any(key in _held_in(pool) for pool, _, _ in pools):
            return True
        seated = _seat_of.get(key)
        if seated is not None and seated[0] == account_id and key in _held_in(seated[1]):
            # Already counted on this account, in a pool its list no longer names (a login
            # removed while the seat was held). Still one seat, still this account's.
            return True
        free = [(pool, refused) for pool, limit, refused in pools if len(_held_in(pool)) < limit]
        if not free:
            return False
        now = datetime.utcnow()
        preferred = [pool for pool, refused in free if not _refused(refused, now, cooldown)]
        pool = preferred[0] if preferred else free[0][0]
        _holders[pool].append(key)
        _seat_of[key] = (account_id, pool)
        return True


def reseat_if_refused(account_id: int, holder_kind: str, holder_id) -> Optional[int]:
    """Before a relaunch: if the holder's seat is on a login the server refused inside the
    cooldown and another login on the account has a free seat that was not, move the seat
    there, so the next segment launches on a working login (design §5.4). Returns the login
    id the holder is seated on afterward, or None when it holds no login seat."""
    from . import db
    from .account_links import refusal_cooldown
    from .config import load_config
    from .database import Account
    key = (holder_kind, holder_id)
    with _lock:
        seated = _seat_of.get(key)
    if seated is None or not isinstance(seated[1], tuple):
        return None
    cfg = load_config()
    default_max_connections = cfg.get('accounts', {}).get('default_max_connections', 1)
    cooldown = refusal_cooldown(cfg)
    all_seats = _seats_now([account_id])
    seats = all_seats.get(account_id, [])
    taken = _pool_taken_now([account_id], all_seats)
    with _lock:
        seated = _seat_of.get(key)
        if seated is None or not isinstance(seated[1], tuple):
            return None
        current = seated[1]
        account = db.session.get(Account, account_id)
        if account is None:
            return current[1]
        pools = _usable_limits(account, default_max_connections, taken, seats)
        now = datetime.utcnow()
        mine = next(((pool, refused) for pool, _, refused in pools if pool == current), None)
        if mine is None or not _refused(mine[1], now, cooldown):
            return current[1]
        for pool, limit, refused in pools:
            if pool == current or _refused(refused, now, cooldown):
                continue
            if len(_held_in(pool)) < limit:
                _held_in(current).remove(key)
                if not _held_in(current):
                    _holders.pop(current, None)
                _holders[pool].append(key)
                _seat_of[key] = (account_id, pool)
                log.info('%s %s: moved from refused login %d to login %d on account %d',
                         holder_kind, holder_id, current[1], pool[1], account_id)
                return pool[1]
        return current[1]


def held_login_id(holder_kind: str, holder_id) -> Optional[int]:
    """The login the holder's seat is on, or None (no seat, or the account's own pool).
    What a launch site asks before rendering the capture URL."""
    with _lock:
        seated = _seat_of.get((holder_kind, holder_id))
    if seated is None or not isinstance(seated[1], tuple):
        return None
    return seated[1][1]


def at_limit(account_id: int) -> bool:
    """Read-only peek: is this account already at its connection limit on every pool?

    For telling a user up front that an action cannot run (the channel detail page's
    "Test now"), never as a substitute for try_acquire() - it registers nothing, so a
    slot can still be taken between the peek and the acquire. The acquire remains the
    only thing that decides.
    """
    from . import db
    from .config import load_config
    from .database import Account
    default_max_connections = load_config().get('accounts', {}).get('default_max_connections', 1)
    all_seats = _seats_now([account_id])
    seats = all_seats.get(account_id, [])
    taken = _pool_taken_now([account_id], all_seats)
    with _lock:
        account = db.session.get(Account, account_id)
        if account is None:
            return True
        pools = _usable_limits(account, default_max_connections, taken, seats)
        return all(len(_held_in(pool)) >= limit for pool, limit, _ in pools)


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
    seats = _seats_now(ids)
    taken = _pool_taken_now(ids, seats)
    pools_by_account = {a.id: _usable_limits(a, default_max_connections, taken, seats.get(a.id, []))
                        for a in Account.query.filter(Account.id.in_(ids)).all()}
    full = set()
    with _lock:
        for account_id in ids:
            pools = pools_by_account.get(account_id)
            if pools is None:
                # No account row: try_acquire() refuses outright, so this is as
                # unavailable as a full account. Same answer at_limit() gives.
                full.add(account_id)
                continue
            for pool, limit, _ in pools:
                held = sum(1 for h in _held_in(pool)
                           if h[0] == 'recording' and h != exclude_holder)
                if held < limit:
                    break
            else:
                full.add(account_id)
    return full


def must_yield_to_block(account_id: int, recording_id: int) -> bool:
    """True when a block now leaves the pool `recording_id`'s seat is in fewer usable slots
    than the recordings holding one - so it should move to another member if it has one.
    Advisory, like at_limit(): two recordings on one partly blocked account may both answer
    True and both try to move, and a move that finds nowhere to go stays put."""
    from . import db
    from .config import load_config
    from .database import Account
    all_seats = _seats_now([account_id])
    seats = all_seats.get(account_id, [])
    taken = _pool_taken_now([account_id], all_seats)
    if not taken:
        return False
    default_max_connections = load_config().get('accounts', {}).get('default_max_connections', 1)
    account = db.session.get(Account, account_id)
    if account is None:
        return False
    pools = _usable_limits(account, default_max_connections, taken, seats)
    key = ('recording', recording_id)
    with _lock:
        seated = _seat_of.get(key)
        pool = seated[1] if seated is not None else account_id
        usable = next((limit for p, limit, _ in pools if p == pool), None)
        if usable is None:
            return False
        recordings = [h for h in _held_in(pool) if h[0] == 'recording']
    return key in recordings and len(recordings) > usable


def _account_of(pool, holder) -> Optional[int]:
    if not isinstance(pool, tuple):
        return pool
    seated = _seat_of.get(holder)
    return seated[0] if seated is not None else None


def holder_counts() -> dict:
    """{account_id: slots held right now, every holder kind, every pool} for every account
    holding one. Advisory, like at_limit(), and taken in one pass under the lock so a caller
    reporting on many accounts never asks once per account."""
    counts: dict = defaultdict(int)
    with _lock:
        for pool, holders in _holders.items():
            for h in holders:
                account_id = _account_of(pool, h)
                if account_id is not None:
                    counts[account_id] += 1
    return dict(counts)


def pool_holder_counts(account_id: int) -> dict:
    """{login_id: seats held right now} across the account's login pools - the Logins
    card's per-login count. Advisory, like at_limit()."""
    counts: dict = {}
    with _lock:
        for pool, holders in _holders.items():
            if not isinstance(pool, tuple):
                continue
            n = sum(1 for h in holders if _account_of(pool, h) == account_id)
            if n:
                counts[pool[1]] = n
    return counts


def _kinds_label(kinds) -> str:
    return _join_labels(HOLDER_LABELS.get(k, k) for k in kinds)


def _join_labels(labels) -> str:
    labels = list(dict.fromkeys(labels))
    if not labels:
        return ''
    if len(labels) == 1:
        return labels[0]
    return ', '.join(labels[:-1]) + ' and ' + labels[-1]


def describe_holders(account_id: int) -> str:
    """Prose naming what holds this account's slots right now, across its pools, for a
    refusal message - 'a recording', 'a recording and a live preview'. Empty string when
    nothing does. Advisory, like at_limit(): read under the lock, but stale the moment it
    returns."""
    with _lock:
        kinds = [h[0] for pool, holders in _holders.items() for h in holders
                 if _account_of(pool, h) == account_id]
    return _kinds_label(kinds)


def login_pool_counts(login_ids) -> dict:
    """{login_id: seats held right now} for each login's whole pool, whichever account the
    holder came through - a login shared by two accounts is one pool, and its card says how
    full the pool is (dev/changelog/1170). Advisory, like at_limit()."""
    with _lock:
        return {lid: len(_held_in(login_pool(lid))) for lid in login_ids
                if _held_in(login_pool(lid))}


def describe_pool_holders(login_id: int, account_id: Optional[int] = None,
                          names: Optional[dict] = None) -> str:
    """describe_holders() for one login's pool - the Logins card's holder line. With
    `account_id` and `names` ({account_id: name}), a holder that came through another
    account sharing the login is named with it: 'a recording on skyline-curated'."""
    with _lock:
        held = [(h[0], _account_of(login_pool(login_id), h)) for h in _held_in(login_pool(login_id))]
    labels = []
    for kind, owner in held:
        label = HOLDER_LABELS.get(kind, kind)
        if account_id is not None and names is not None and owner not in (None, account_id):
            label += f' on {names.get(owner, f"account {owner}")}'
        labels.append(label)
    return _join_labels(labels)


def release(account_id: int, holder_kind: str, holder_id):
    """Release the holder's seat from the pool it was counted in - never re-derived from
    the account's current list."""
    key = (holder_kind, holder_id)
    with _lock:
        seated = _seat_of.pop(key, None)
        pool = seated[1] if seated is not None else account_id
        holders = _holders.get(pool)
        if not holders:
            return
        try:
            holders.remove(key)
        except ValueError:
            pass
        if not holders:
            _holders.pop(pool, None)


def _strip_kind(account_id: int, holder_kind: str) -> List:
    """Release every holder of one kind on account_id, across its pools, and return their
    holder_ids."""
    stripped = []
    with _lock:
        for pool in list(_holders):
            holders = _holders[pool]
            mine = [h for h in holders if h[0] == holder_kind
                    and _account_of(pool, h) == account_id]
            for h in mine:
                holders.remove(h)
                _seat_of.pop(h, None)
            stripped.extend(mine)
            if not holders:
                _holders.pop(pool, None)
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
