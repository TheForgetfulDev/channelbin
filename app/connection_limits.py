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

from .fmt_utils import join_labels

log = logging.getLogger(__name__)

_lock = threading.Lock()
# pool -> list of (holder_kind, holder_id) currently holding a seat in it.
#   pool: the account id itself (int) for an account with no logins - its one pool, as
#         it always was - or ('login', login_id) for each login the account holds.
#   holder_kind: 'recording' | 'test' | 'preview' | 'viewer'
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
    'viewer': 'an IPTV player',
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


def seated_holder_ids(holder_kind: str) -> set:
    """The ids of every `holder_kind` holder with a seat right now. Advisory, like at_limit()."""
    with _lock:
        return {holder_id for kind, holder_id in _seat_of if kind == holder_kind}


def _viewer_seats_locked() -> dict:
    """{account_id: seats 'viewer' holders have on it} - the IPTV output's relayed plays.
    The caller holds _lock."""
    counts: dict = defaultdict(int)
    for (kind, _holder_id), (account_id, _pool) in _seat_of.items():
        if kind == 'viewer':
            counts[account_id] += 1
    return dict(counts)


def _nothing_taken() -> dict:
    return {}


def _pool_taken_plan(account_ids, seats: dict, cfg=None, holder_kind: Optional[str] = None,
                     login_blocks: bool = True):
    """A function of no arguments answering {pool: slots taken} for the pools of
    `account_ids` (`seats`, their login rows), to be called with _lock held. The queries run
    here, BEFORE the lock: a block and the login rows are database reads, and the lock guards
    only the in-memory holder list. A block on an account sharing one of their logins counts on
    that login's pool too (dev/changelog/1170); the sibling lookup runs only when one of them
    lists a login, so an account with none asks exactly what it always did.

    The output feed's held seats are the IPTV players' own (dev/changelog/1536): `holder_kind`
    'viewer' (a relayed play) is never kept off them, and for every other kind a relayed play
    already on the account fills one, so only the held seats no viewer holds are taken. That
    count is the holder list itself, so the function reads it under the lock, in the same
    critical section as the decision it feeds.

    A block on one of their logins takes that login's whole pool (dev/changelog/1672);
    `login_blocks=False` leaves those out, for must_yield_to_block(), since a login block
    never moves a running recording. Asked only when one of them lists a login."""
    from .account_blocks import blocked_slots, blocks_at, login_blocks_at, pool_taken
    from .account_links import login_seats_for_accounts, login_siblings
    ids = set(account_ids)
    wider = set(ids)
    if any(seats.get(a) for a in ids):
        wider |= {other for found in login_siblings(ids).values() for other in found}
    blocked = blocked_slots(blocks_at(account_ids=wider))
    login_ids = {lid for a in ids for lid, _, _ in seats.get(a) or ()}
    lblocks = login_blocks_at(login_ids=login_ids) if login_blocks and login_ids else []
    # Seats the output feed keeps free for an IPTV player are subtracted exactly where a block's
    # are: ChannelBin cannot see a redirected play's connection, so the recorder must never plan
    # on a seat the IPTV player may be using (DESIGN-output-feed.md 5.3, 5.5).
    from .output_feed import held_seats_now
    held_now = held_seats_now(wider, cfg) if holder_kind != 'viewer' else {}
    if not blocked and not held_now and not lblocks:
        return _nothing_taken
    missing = (set(blocked) | set(held_now)) - set(seats)
    all_seats = dict(seats)
    if missing:
        all_seats.update(login_seats_for_accounts(missing))

    def taken_locked() -> dict:
        merged = dict(blocked)
        viewers = _viewer_seats_locked() if held_now else {}
        for account_id, held in held_now.items():
            held = max(0, held - viewers.get(account_id, 0))
            if not held or merged.get(account_id, 0) is None:
                continue
            merged[account_id] = merged.get(account_id, 0) + held
        return pool_taken(merged, all_seats, lblocks) if merged or lblocks else {}
    return taken_locked


def _pool_taken_now(account_ids, seats: dict, cfg=None, holder_kind: Optional[str] = None,
                    login_blocks: bool = True) -> dict:
    """_pool_taken_plan()'s answer now, for an advisory reader that decides nothing under the
    lock (at_limit(), accounts_without_free_recording_slot(), must_yield_to_block())."""
    taken = _pool_taken_plan(account_ids, seats, cfg, holder_kind, login_blocks)
    with _lock:
        return taken()


def _usable_limits(account, default_max_connections: int, taken: dict,
                   seats: list, hosts_off: frozenset = frozenset()) -> list:
    """[(pool, usable limit, last_refused_at), ...] for `account`, in the order seats are
    taken. `taken` is _pool_taken_now(): a block takes its slots from the blocked account's
    pools in order (None = every slot), so a fully blocked account answers 0 on every pool,
    which is what turns every acquire on it into the refusal its caller already handles
    (dev/changelog/1151) - and a login it shares answers 0 for the sibling too.

    A login turned off on the account answers 0 already (its seats are 0 in `seats`), and an
    account in `hosts_off` - every host it lists turned off - answers 0 on every pool: no
    stream may go out on a host the user turned off (dev/changelog/1670). A holder already
    seated keeps its seat either way; only a new one is refused."""
    from .account_blocks import effective_limit
    if seats:
        pools = [(login_pool(lid), s, refused) for lid, s, refused in seats]
    else:
        pools = [(account.id, limit_for_account(account, default_max_connections), None)]
    if account.id in hosts_off:
        return [(pool, 0, refused) for pool, _limit, refused in pools]
    if not taken:
        return pools
    return [(pool, effective_limit(limit, taken[pool]) if pool in taken else limit, refused)
            for pool, limit, refused in pools]


def _seats_now(account_ids, cfg=None) -> dict:
    from .account_links import login_seats_for_accounts
    return login_seats_for_accounts(account_ids, cfg)


def _hosts_off(account_ids) -> frozenset:
    from .account_links import hosts_all_off
    return frozenset(hosts_all_off(account_ids))


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
    all_seats = _seats_now([account_id], cfg)
    seats = all_seats.get(account_id, [])
    hosts_off = _hosts_off([account_id])
    taken = _pool_taken_plan([account_id], all_seats, cfg, holder_kind)
    with _lock:
        account = db.session.get(Account, account_id)
        if account is None:
            return False
        pools = _usable_limits(account, default_max_connections, taken(), seats, hosts_off)
        key = (holder_kind, holder_id)
        if any(key in _held_in(pool) for pool, _, _ in pools):
            return True
        seated = _seat_of.get(key)
        if seated is not None and seated[0] == account_id and key in _held_in(seated[1]):
            # Already counted on this account, in a pool its list no longer names (a login
            # removed while the seat was held). Still one seat, still this account's.
            return True
        pool = _pick_free_locked(pools, datetime.utcnow(), cooldown)
        if pool is None:
            return False
        _holders[pool].append(key)
        _seat_of[key] = (account_id, pool)
        return True


def _pick_free_locked(pools, now, cooldown):
    """The pool a new seat goes in: the first with a free seat, passing over a login refused
    inside the cooldown while another has one. None when every pool is full. The caller holds
    _lock. try_acquire() decides with it and next_seat() predicts with it, so the page's "the
    next stream will use" cannot name a different login from the one the next seat takes."""
    free = [(pool, refused) for pool, limit, refused in pools if len(_held_in(pool)) < limit]
    if not free:
        return None
    preferred = [pool for pool, refused in free if not _refused(refused, now, cooldown)]
    return preferred[0] if preferred else free[0][0]


def next_seat(account_id: int, cfg=None) -> tuple:
    """(whether a new stream could take a seat on the account right now, the login id it would
    be seated on - None for an account with no logins). Advisory and racy exactly as at_limit()
    is: for the account and channel pages' "the next stream will use" line, never a decision.
    A recording that would have to stop a preview or a test for its seat reads as no free seat
    here; the recorder still preempts them."""
    from . import db
    from .account_links import refusal_cooldown
    from .config import load_config
    from .database import Account
    cfg = cfg if cfg is not None else load_config()
    default_max_connections = cfg.get('accounts', {}).get('default_max_connections', 1)
    cooldown = refusal_cooldown(cfg)
    all_seats = _seats_now([account_id], cfg)
    seats = all_seats.get(account_id, [])
    hosts_off = _hosts_off([account_id])
    taken = _pool_taken_now([account_id], all_seats, cfg)
    account = db.session.get(Account, account_id)
    if account is None:
        return False, None
    pools = _usable_limits(account, default_max_connections, taken, seats, hosts_off)
    with _lock:
        pool = _pick_free_locked(pools, datetime.utcnow(), cooldown)
    if pool is None:
        return False, None
    return True, (pool[1] if isinstance(pool, tuple) else None)


def move_seat(holder_kind: str, holder_id, from_account_id: int, to_account_id: int) -> bool:
    """Move the holder's seat from one account to another in one step, or leave it exactly
    where it is.

    For a live recording failing over across accounts. A release followed by a try_acquire
    on the target leaves the recording holding no seat at all when the target refuses, and
    the re-acquire on the account it just left can refuse too - the account may be the one
    the user just blocked, whose usable limit is 0 - so the recording goes on capturing
    with a connection nothing counts, and every later block check reads "no seat here" as
    "nothing to move" (dev/changelog/1242). Decided and done under the one lock: the target
    pool is found with the old seat still held, and only then is the old seat given up.

    Returns False with nothing changed when no pool on the target has a free seat. When
    the two accounts share the login the seat is already in, the seat stays and only its
    attribution moves - one connection, one seat, now counted to the account the recording
    is on.
    """
    from . import db
    from .account_links import refusal_cooldown
    from .config import load_config
    from .database import Account
    cfg = load_config()
    default_max_connections = cfg.get('accounts', {}).get('default_max_connections', 1)
    cooldown = refusal_cooldown(cfg)
    all_seats = _seats_now([to_account_id], cfg)
    seats = all_seats.get(to_account_id, [])
    hosts_off = _hosts_off([to_account_id])
    taken = _pool_taken_plan([to_account_id], all_seats, cfg, holder_kind)
    key = (holder_kind, holder_id)
    with _lock:
        account = db.session.get(Account, to_account_id)
        if account is None:
            return False
        pools = _usable_limits(account, default_max_connections, taken(), seats, hosts_off)
        seated = _seat_of.get(key)
        current = seated[1] if seated is not None else from_account_id
        if key in _held_in(current) and any(pool == current for pool, _, _ in pools):
            _seat_of[key] = (to_account_id, current)
            return True
        free = [(pool, refused) for pool, limit, refused in pools if len(_held_in(pool)) < limit]
        if not free:
            return False
        now = datetime.utcnow()
        preferred = [pool for pool, refused in free if not _refused(refused, now, cooldown)]
        pool = preferred[0] if preferred else free[0][0]
        old = _held_in(current)
        if key in old:
            old.remove(key)
            if not old:
                _holders.pop(current, None)
        _holders[pool].append(key)
        _seat_of[key] = (to_account_id, pool)
        return True


def reseat_before_relaunch(account_id: int, holder_kind: str, holder_id) -> Optional[int]:
    """Before a relaunch: move the holder's seat off a login it should not launch on again,
    when another login on the account has a free seat that is fine - so the next segment
    carries working credentials. Two reasons to move: the server refused the login inside the
    cooldown (design §5.4), or the user turned the login off for this account since the seat
    was taken (dev/changelog/1670). With nowhere to move, the seat stays where it is: a
    refusal is sometimes a panel hiccup, and a stream already running keeps going rather than
    being cut off by a switch. Returns the login id the holder is seated on afterward, or None
    when it holds no login seat. A third reason, on the same terms: the user blocked the
    login since (dev/changelog/1672) - the TV it was blocked for wants the seat, and a stream
    already running keeps going when no other login has one."""
    from . import db
    from .account_blocks import login_blocks_at
    from .account_links import login_enabled, refusal_cooldown
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
    all_seats = _seats_now([account_id], cfg)
    seats = all_seats.get(account_id, [])
    hosts_off = _hosts_off([account_id])
    current_off = not login_enabled(account_id, seated[1][1])
    current_blocked = bool(login_blocks_at(login_ids=[seated[1][1]]))
    taken = _pool_taken_plan([account_id], all_seats, cfg, holder_kind)
    with _lock:
        seated = _seat_of.get(key)
        if seated is None or not isinstance(seated[1], tuple):
            return None
        current = seated[1]
        account = db.session.get(Account, account_id)
        if account is None:
            return current[1]
        pools = _usable_limits(account, default_max_connections, taken(), seats, hosts_off)
        now = datetime.utcnow()
        mine = next(((pool, refused) for pool, _, refused in pools if pool == current), None)
        refused_now = mine is not None and _refused(mine[1], now, cooldown)
        if not refused_now and not current_off and not current_blocked:
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
                why = ('refused' if refused_now else 'turned-off' if current_off
                       else 'blocked')
                log.info('%s %s: moved from %s login %d to login %d on account %d',
                         holder_kind, holder_id, why,
                         current[1], pool[1], account_id)
                return pool[1]
        return current[1]


def fold_account_pool(account_id: int) -> Optional[int]:
    """Move every holder counted in the account's own pool onto its first login that is on,
    once the account has started holding logins. Returns the login id they moved to, or None.

    The first listing of a login - the playlist's own at a sync, or one the user adds - turns
    an account that counted one pool keyed by its id into one counted per login. A recording
    already seated in the old pool would otherwise count against nothing: every new holder
    would see the login's seat as free and a second stream would open on a one-connection
    login, and the recording itself would never be reseated, so it could not move off a login
    the user later turns off (dev/changelog/1670)."""
    from .account_links import login_seats_for_accounts
    seats = login_seats_for_accounts([account_id]).get(account_id, [])
    if not seats:
        return None
    target = login_pool(next((lid for lid, n, _ in seats if n), seats[0][0]))
    with _lock:
        held = _holders.pop(account_id, [])
        for key in held:
            _holders[target].append(key)
            _seat_of[key] = (account_id, target)
    if held:
        log.info('Account %d now holds logins: %d seat(s) in its own pool counted on login %d',
                 account_id, len(held), target[1])
    return target[1]


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
    cfg = load_config()
    default_max_connections = cfg.get('accounts', {}).get('default_max_connections', 1)
    all_seats = _seats_now([account_id], cfg)
    seats = all_seats.get(account_id, [])
    hosts_off = _hosts_off([account_id])
    taken = _pool_taken_now([account_id], all_seats, cfg)
    with _lock:
        account = db.session.get(Account, account_id)
        if account is None:
            return True
        pools = _usable_limits(account, default_max_connections, taken, seats, hosts_off)
        return all(len(_held_in(pool)) >= limit for pool, limit, _ in pools)


#: seat_standing()'s answers, best first: a seat nobody is in; a seat only by cutting off an
#: IPTV player on the output feed (a recording's question only - a viewer cuts nobody); none.
SEAT_FREE = 'free'
SEAT_BY_CUTTING_VIEWER = 'by_cutting_viewer'
SEAT_NONE = 'none'
SEAT_STANDINGS = (SEAT_FREE, SEAT_BY_CUTTING_VIEWER, SEAT_NONE)


def seat_standing(account_ids, holder_kind: str, exclude_holder=None) -> dict:
    """{account_id: SEAT_*} - whether a `holder_kind` holder could take a seat on each account
    right now, and whom it would have to cut off to get one. The ONE answer to "which accounts
    are free, and free of whom" that a recording's member pick and the IPTV output's ask
    (dev/changelog/1642), so the two cannot disagree about which account is idle.

    For a 'recording': a channel test or a live preview is preempted rather than waited on
    (recorder._try_acquire_slot_with_preemption) and never makes an account less free. An IPTV
    player is preempted too, but cutting one off is a cost a viewer sees, so an account whose
    only free seats are a viewer's answers SEAT_BY_CUTTING_VIEWER and an empty one is preferred
    over it. Only other recordings make an account SEAT_NONE - and a viewer the preemption would
    leave seated, one on its account's held seats (dev/changelog/1536), which counts as one.

    For a 'viewer': the question try_acquire() would answer - a viewer preempts nobody, so every
    holder counts and its account's held seats are its own. SEAT_FREE or SEAT_NONE.

    `exclude_holder` is a (holder_kind, holder_id) pair to discount: a live recording or play
    asking where it could move to must not see its own seat as somebody else's.

    Advisory and racy in exactly the way at_limit() documents: this informs *which member to
    prefer*, never whether anything may connect. try_acquire() remains the only thing that
    decides, and a member on a SEAT_NONE account is still selectable when no alternative exists
    (dev/changelog/855).

    Batched deliberately. One selection pass asks this once for a group whose members may span
    many accounts, where at_limit() would re-read the config and one Account row per member
    (CLAUDE.md, no hidden I/O in per-row loops)."""
    from .config import load_config
    from .database import Account
    from .output_feed import held_seats_now
    ids = {a for a in account_ids if a is not None}
    if not ids:
        return {}
    cfg = load_config()
    default_max_connections = cfg.get('accounts', {}).get('default_max_connections', 1)
    seats = _seats_now(ids, cfg)
    hosts_off = _hosts_off(ids)
    taken = _pool_taken_now(ids, seats, cfg, holder_kind)
    pools_by_account = {a.id: _usable_limits(a, default_max_connections, taken, seats.get(a.id, []),
                                             hosts_off)
                        for a in Account.query.filter(Account.id.in_(ids)).all()}
    # The viewers a recording's preemption leaves seated (preempt_viewers_for_slot keeps up to
    # an account's held seats, dev/changelog/1536) are in its way as surely as a recording.
    held = {} if holder_kind == 'viewer' else held_seats_now(None, cfg)
    out = {}
    with _lock:
        for account_id in ids:
            pools = pools_by_account.get(account_id)
            if pools is None:
                # No account row: try_acquire() refuses outright, so this is as
                # unavailable as a full account. Same answer at_limit() gives.
                out[account_id] = SEAT_NONE
                continue
            best = SEAT_NONE
            # One count across the account's pools, as the preemption keeps its held seats
            # across all of them (_strip_kind's `keep`), not up to that many in each.
            kept: dict = defaultdict(int)
            for pool, limit, _ in pools:
                hard, soft = _in_the_way(pool, holder_kind, exclude_holder, held, kept)
                if hard + soft < limit:
                    best = SEAT_FREE
                    break
                if hard < limit:
                    best = SEAT_BY_CUTTING_VIEWER
            out[account_id] = best
    return out


def _in_the_way(pool, holder_kind: str, exclude_holder, held: dict, kept: dict) -> tuple:
    """(holders a `holder_kind` holder would have to wait on in `pool`, viewers it could cut off
    instead). `kept` counts, per account, the viewers already found on its held seats in the
    pools before this one. The caller holds _lock."""
    others = [h for h in _held_in(pool) if h != exclude_holder]
    if holder_kind == 'viewer':
        return len(others), 0
    hard = soft = 0
    for h in others:
        if h[0] == 'recording':
            hard += 1
        elif h[0] == 'viewer':
            owner = _account_of(pool, h)
            if kept[owner] < held.get(owner, 0):
                kept[owner] += 1
                hard += 1
            else:
                soft += 1
    return hard, soft


def accounts_without_free_recording_slot(account_ids, exclude_holder=None) -> set:
    """Of `account_ids`, the ones a recording could not take a slot on right now, even by
    cutting off an IPTV player: seat_standing()'s SEAT_NONE for a 'recording'. Only other
    recordings make an account full here; the rule scheduler's seat moves read this
    (app/seat_moves.py)."""
    return {a for a, s in seat_standing(account_ids, 'recording', exclude_holder).items()
            if s == SEAT_NONE}


def must_yield_to_block(account_id: int, recording_id: int) -> bool:
    """True when an ACCOUNT block now leaves the pool `recording_id`'s seat is in fewer usable
    slots than the recordings holding one - so it should move to another member if it has one.
    Advisory, like at_limit(): two recordings on one partly blocked account may both answer
    True and both try to move, and a move that finds nowhere to go stays put. A block on a
    login is not counted: streams already running on a blocked login keep going
    (dev/changelog/1672)."""
    from . import db
    from .config import load_config
    from .database import Account
    cfg = load_config()
    all_seats = _seats_now([account_id])
    seats = all_seats.get(account_id, [])
    taken = _pool_taken_now([account_id], all_seats, cfg, login_blocks=False)
    if not taken:
        return False
    default_max_connections = cfg.get('accounts', {}).get('default_max_connections', 1)
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
    return join_labels(HOLDER_LABELS.get(k, k) for k in kinds)


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
    return join_labels(labels)


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


def _strip_kind(account_id: int, holder_kind: str, keep: Optional[dict] = None) -> List[tuple]:
    """Release every holder of one kind that a recording on account_id would need the seat
    of, and return (holder_id, account the holder came through) pairs.

    That is every such holder in any of the account's CURRENT pools - whichever account it
    came through, since a login shared across accounts is one seat set and a preview on the
    sibling holds the very seat the recording needs (dev/changelog/1243) - plus any holder
    counted to this account in a pool its list no longer names (a login removed while the
    seat was held), which is still this account's connection on the provider.

    `keep` ({account the holder came through: how many}) leaves that many of them seated,
    the earliest first: the ones whose release would free nothing a recording may use."""
    seats = _seats_now([account_id]).get(account_id, [])
    pools = {login_pool(lid) for lid, _, _ in seats} if seats else {account_id}
    left = dict(keep or {})
    stripped = []

    def kept(owner) -> bool:
        if left.get(owner, 0) > 0:
            left[owner] -= 1
            return True
        return False

    with _lock:
        # A kept holder that is not in the way (another pool of its own account) uses up its
        # account's keep first, so the ones in the way are the ones released.
        for pool, holders in _holders.items():
            for h in holders:
                owner = _account_of(pool, h)
                if h[0] == holder_kind and not (pool in pools or owner == account_id):
                    kept(owner)
        for pool in list(_holders):
            holders = _holders[pool]
            mine = [(h, _account_of(pool, h)) for h in holders if h[0] == holder_kind
                    and (pool in pools or _account_of(pool, h) == account_id)]
            mine = [(h, owner) for h, owner in mine if not kept(owner)]
            for h, _ in mine:
                holders.remove(h)
                _seat_of.pop(h, None)
            stripped.extend(mine)
            if not holders:
                _holders.pop(pool, None)
    return [(h[1], owner if owner is not None else account_id) for h, owner in stripped]


def preempt_tests_for_slot(account_id: int) -> List[tuple]:
    """Release every 'test'-kind holder a recording on account_id needs the seat of and
    return (channel_id, account the test came through) pairs, so the caller (recorder.py,
    when a recording needs the slot a running channel test currently holds) can kill each
    test's ffmpeg process through its own account.

    Never releases 'recording'-kind holders - a recording must never force out
    another recording. Recordings always win over tests per product decision;
    this is the mechanism that enforces it.
    """
    return _strip_kind(account_id, 'test')


def preempt_viewers_for_slot(account_id: int) -> List[tuple]:
    """Release every 'viewer'-kind holder a recording on account_id needs the seat of and
    return (relay session id, account the viewer came through) pairs, so recorder.py can
    end the session through app/output_feed.py. An IPTV player is lowest in the order: the
    recorder asks this before the preview strip, since a viewer can resume on its own and
    a recording is what the app is for (DESIGN-output-feed.md 4.4).

    A viewer sitting on one of its account's held seats is left alone: recordings never get
    those seats, so ending the play would free nothing they may use (dev/changelog/1536)."""
    from .output_feed import held_seats_now
    return _strip_kind(account_id, 'viewer', held_seats_now(None))


def preempt_previews_for_slot(account_id: int) -> List[tuple]:
    """Release every 'preview'-kind holder a recording on account_id needs the seat of and
    return (preview session id, account the preview came through) pairs, so recorder.py
    can stop the preview's ffmpeg through app/preview.py. Same doctrine as
    preempt_tests_for_slot, and the recorder asks this one FIRST: a preview is a person
    looking, a test is a measurement feeding the health score, so the look yields before
    the measurement does (dev/changelog/1018)."""
    return _strip_kind(account_id, 'preview')
