"""
Account blocks: stretches of time during which ChannelBin keeps off an account.

The case this exists for is a TV watching live on a provider account that ChannelBin also
records from. With an account limited to one connection, anything ChannelBin opens there while
the TV is on it either kicks the TV off or is refused by the provider - and a refused
connection is scored as a failure. So for the length of a block, nothing in ChannelBin opens a
stream on the account: not a recording's member choice, not a failover, not a pre-check, not a
health check, not a live preview. Account sync is untouched; it makes API calls, not a stream
connection (dev/changelog/1151).

Two halves, and every consumer goes through one of them:

- **Where a member is CHOSEN** (record start, failover, the pre-check's target, the guide row's
  serving member, the member a new recording is stamped with), a member on a fully blocked
  account is dropped before the format lock and the recording's format pin, the same way an
  already-failed member is - so a blocked member can never be the one survivor that suppresses
  a zero-survivors override. `blocked_account_ids()` + `split_blocked()`.
- **Where a connection is TAKEN**, `app/connection_limits.py` subtracts the blocked slots from
  the account's limit, so a test, a preview or a single-channel recording is refused through
  the refusal paths that already exist: a recording defers and retries, a test is skipped and
  never scored.

A skip for a block is never an observation. Nothing here writes a health score, a failing
streak or the account ledger - the user asked for the account to be left alone, not judged.

A block is the user's answer to a judgment call and only the explicit user action writes one
(CLAUDE.md, participation-switch rule). Nothing clears one early on the user's behalf; it stops
mattering once its window has passed.
"""
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Dict, Iterable, List, Optional

from . import db
from .db_utils import retry_on_locked

log = logging.getLogger(__name__)

#: The longest block the account page may set. A block exists so the user does not have to
#: remember to turn something back on; one that could run for a week is that switch again.
MAX_ACCOUNT_BLOCK_HOURS = 24

_POINT = timedelta(microseconds=1)


def _live_recording_statuses():
    """A block set on a recording counts only while the recording can still open a stream.
    Once it has stopped capturing (concatenating, finished, failed, aborted) the account is
    free again, even if the recording's scheduled stop is still ahead."""
    from .database import REC_STATUS_SCHEDULED, WINDOW_OPEN_STATUSES
    return (REC_STATUS_SCHEDULED,) + tuple(WINDOW_OPEN_STATUSES)


@dataclass(frozen=True)
class BlockWindow:
    """One block with its window resolved - a recording's block takes the recording's."""
    id: int
    account_id: int
    recording_id: Optional[int]
    start: datetime
    stop: datetime
    slots: Optional[int]   # None = every slot


def blocks_overlapping(start: datetime, stop: datetime,
                       account_ids: Optional[Iterable[int]] = None,
                       exclude_recording_id: Optional[int] = None) -> List[BlockWindow]:
    """Every block whose window overlaps [start, stop), in one query. `exclude_recording_id`
    leaves out one recording's own blocks - an edit that is replacing them."""
    from .database import AccountBlock, Recording
    q = (db.session.query(AccountBlock.id, AccountBlock.account_id, AccountBlock.recording_id,
                          AccountBlock.start_time, AccountBlock.stop_time, AccountBlock.slots,
                          Recording.start_time, Recording.stop_time)
         .outerjoin(Recording, AccountBlock.recording_id == Recording.id)
         .filter(db.or_(
             db.and_(AccountBlock.recording_id.is_(None),
                     AccountBlock.start_time < stop, AccountBlock.stop_time > start),
             db.and_(AccountBlock.recording_id.isnot(None),
                     Recording.status.in_(_live_recording_statuses()),
                     Recording.start_time < stop, Recording.stop_time > start))))
    if account_ids is not None:
        ids = {a for a in account_ids if a is not None}
        if not ids:
            return []
        q = q.filter(AccountBlock.account_id.in_(ids))
    if exclude_recording_id is not None:
        q = q.filter(db.or_(AccountBlock.recording_id.is_(None),
                            AccountBlock.recording_id != exclude_recording_id))
    out = []
    for (bid, account_id, recording_id, own_start, own_stop, slots,
         rec_start, rec_stop) in q.all():
        tied = recording_id is not None
        out.append(BlockWindow(bid, account_id, recording_id,
                               rec_start if tied else own_start,
                               rec_stop if tied else own_stop, slots))
    return out


def blocks_at(at: Optional[datetime] = None,
              account_ids: Optional[Iterable[int]] = None) -> List[BlockWindow]:
    at = at or datetime.utcnow()
    return blocks_overlapping(at, at + _POINT, account_ids)


def blocked_slots(blocks: Iterable[BlockWindow], pending: Optional[Dict[int, Optional[int]]] = None
                  ) -> Dict[int, Optional[int]]:
    """{account_id: slots taken} across `blocks` (plus `pending`, blocks not yet saved).

    None means every slot. Two partial blocks on one account add up - two TVs on one account
    are two connections - and any whole-account block wins outright."""
    merged: Dict[int, Optional[int]] = {}
    items = [(b.account_id, b.slots) for b in blocks]
    items += list((pending or {}).items())
    for account_id, slots in items:
        if account_id in merged and merged[account_id] is None:
            continue
        if slots is None:
            merged[account_id] = None
        else:
            merged[account_id] = merged.get(account_id, 0) + slots
    return merged


def effective_limit(limit: int, taken) -> int:
    """The account's connection limit with a block's share taken out. `taken` is one value
    of blocked_slots(): None = every slot. Pass 0 when the account is not blocked."""
    if taken is None:
        return 0
    return max(limit - taken, 0)


def account_limits(account_ids: Iterable[int], cfg: Optional[dict] = None) -> Dict[int, int]:
    """{account_id: configured connection limit}, batched - one config read, one query."""
    from .connection_limits import limits_for_accounts
    from .database import Account
    ids = {a for a in account_ids if a is not None}
    if not ids:
        return {}
    if cfg is None:
        from .config import load_config
        cfg = load_config()
    default_max = cfg.get('accounts', {}).get('default_max_connections', 1)
    return limits_for_accounts(Account.query.filter(Account.id.in_(ids)).all(), default_max)


def account_pools(account_id: int, seats: Optional[list], limit: Optional[int]) -> list:
    """[(pool, size), ...] for an account, in the order seats are taken: one pool per listed
    login (`seats` from account_links.login_seats_for_accounts), or the account's own pool
    sized by `limit` when it lists none. The same keys app/connection_limits.py counts on."""
    if seats:
        return [(('login', login_id), size) for login_id, size, _ in seats]
    return [(account_id, limit)]


def pool_taken(slots_by_account: Dict[int, Optional[int]], seats_by_account: Dict[int, list]
               ) -> dict:
    """{pool: slots taken} - each blocked account's slots spread over its pools in list
    order, summed per pool (None = every slot).

    A block is written against an account, because that is what the user is keeping a TV
    on; the seats it takes are the account's logins'. When one of them is a login another
    account on the same provider shares, that account loses those seats too - the provider
    sees one login (DESIGN-account-providers.md §5.3, dev/changelog/1170). An account with
    no logins has one pool and takes its block whole, which is the rule before logins."""
    out: dict = {}
    for account_id, taken in slots_by_account.items():
        left = taken
        for pool, size in account_pools(account_id, seats_by_account.get(account_id), None):
            if left is None:
                share = None
            elif size is None:
                share = left
            else:
                share = min(left, size)
                left -= share
            if pool in out and out[pool] is None:
                continue
            out[pool] = None if share is None else out.get(pool, 0) + share
    return out


def _fully_blocked_pools(candidates, taken: dict, seats_by_account: Dict[int, list],
                         limits: Dict[int, int]) -> set:
    """Of `candidates`, the accounts left with no usable seat on any pool."""
    out = set()
    for account_id in candidates:
        pools = account_pools(account_id, seats_by_account.get(account_id),
                              limits.get(account_id, 0))
        if all(effective_limit(size, taken.get(pool, 0)) == 0 for pool, size in pools):
            out.add(account_id)
    return out


def _with_siblings(ids) -> tuple[set, dict]:
    """(`ids` plus every account sharing a login with one of them, {account_id: {sibling:
    [login_id, ...]}}) - the accounts whose blocks can reach `ids`."""
    from .account_links import login_siblings
    siblings = login_siblings(ids)
    wider = set(ids) | {other for found in siblings.values() for other in found}
    return wider, siblings


def _pool_blocked(merged: Dict[int, Optional[int]], candidates: Optional[set]) -> set:
    """The accounts among `candidates` (None = every account a block can reach) that
    `merged` (blocked_slots() over the right block set) leaves with no seat at all.

    The question is per pool: a block on one account reaches every account sharing one of
    its logins. Seats and limits are read only for the accounts involved."""
    from .account_links import login_seats_for_accounts, login_siblings
    if not merged:
        return set()
    reached = set(merged) | {other for found in login_siblings(merged).values()
                             for other in found}
    if candidates is not None:
        reached &= candidates
    if not reached:
        return set()
    seats = login_seats_for_accounts(reached | set(merged))
    plain = {aid for aid in reached if not seats.get(aid)}
    limits = account_limits(plain) if plain else {}
    return _fully_blocked_pools(reached, pool_taken(merged, seats), seats, limits)


def blocked_account_ids(account_ids: Optional[Iterable[int]], at: Optional[datetime] = None,
                        start: Optional[datetime] = None, stop: Optional[datetime] = None,
                        pending: Optional[Dict[int, Optional[int]]] = None,
                        exclude_recording_id: Optional[int] = None) -> set:
    """Of `account_ids` (None = every account), the ones fully blocked at `at` (default
    now), or at any point of [start, stop) when a window is given. One query for the blocks;
    the account limits are read only when a partial block needs them."""
    ids = None if account_ids is None else {a for a in account_ids if a is not None}
    if ids is not None and not ids:
        return set()
    # A block on an account sharing a login with one of `ids` takes that login's seats from
    # it too (dev/changelog/1170), so those accounts' blocks are read as well.
    wider = None if ids is None else _with_siblings(ids)[0]
    if start is not None and stop is not None:
        blocks = blocks_overlapping(start, stop, wider, exclude_recording_id)
    else:
        blocks = blocks_at(at, wider)
    pending = {a: s for a, s in (pending or {}).items() if wider is None or a in wider}
    return _pool_blocked(blocked_slots(blocks, pending), ids)


def split_blocked(members, blocked_ids):
    """(kept, skipped): `members` whose account is not in `blocked_ids`, and the rest.

    A member on a blocked account is unavailable in the way a member that already failed this
    run is - so this runs BEFORE either format filter, which judge "zero survivors" over the
    list they are handed."""
    if not blocked_ids:
        return list(members), []
    kept = [m for m in members if m.account_id not in blocked_ids]
    skipped = [m for m in members if m.account_id in blocked_ids]
    return kept, skipped


def free_at(account_id: int, at: Optional[datetime] = None) -> Optional[datetime]:
    """When `account_id` next has a connection it may use, if it is fully blocked at `at`;
    None when it is not fully blocked. The earliest block end after which the blocks still
    in force leave a slot. A block that starts at that same moment is not looked ahead to -
    the next retry re-asks."""
    from .account_links import login_seats_for_accounts
    at = at or datetime.utcnow()
    wider, _ = _with_siblings([account_id])
    blocks = sorted(blocks_at(at, wider), key=lambda b: b.stop)
    if not blocks:
        return None
    seats = login_seats_for_accounts(wider)
    limits = account_limits([account_id]) if not seats.get(account_id) else {}
    for i in range(len(blocks)):
        taken = pool_taken(blocked_slots(blocks[i:]), seats)
        if not _fully_blocked_pools([account_id], taken, seats, limits):
            return None if i == 0 else blocks[i - 1].stop
    return blocks[-1].stop


def until_label(dt: datetime, tz=None, h24=None) -> str:
    """'10:00 PM' for a time later today, 'Sat 9/28 10:00 PM' for one on another day."""
    from .tz_utils import format_local, get_display_tz, to_local
    tz = tz or get_display_tz()
    same_day = to_local(dt, tz).date() == to_local(datetime.utcnow(), tz).date()
    return format_local(dt, 'clock' if same_day else 'monthday_time', tz=tz, h24=h24)


def describe(account_name: str, until: Optional[datetime], capital: bool = False) -> str:
    """The one phrase every surface uses for a blocked account. `capital` for the start of
    a sentence."""
    head = 'Account' if capital else 'account'
    if until is None:
        return f'{head} "{account_name}" is blocked'
    return f'{head} "{account_name}" is blocked until {until_label(until)}'


def blocked_reason(account_id: int, account_name: str, capital: bool = False,
                   at: Optional[datetime] = None, partial: bool = False) -> Optional[str]:
    """describe() for an account fully blocked at `at` (default now), else None - for a
    refusal that has to say why, rather than blaming the connection limit. `partial` also
    answers for a block that leaves some seats (the watchdog's move off a partly blocked
    pool), without an end time.

    When the seats are gone because a block on ANOTHER account takes a login the two share,
    it says that instead: an account the user never blocked must not read as blocked with
    no way to see why (dev/changelog/1170)."""
    at = at or datetime.utcnow()
    until = free_at(account_id, at)
    if until is None and not partial:
        return None
    own = blocks_at(at, [account_id])
    if own:
        return describe(account_name, until, capital)
    sibling = _sibling_block_phrase(account_id, at, until)
    if sibling is None:
        return describe(account_name, until, capital) if until is not None else None
    head = 'Account' if capital else 'account'
    return f'{head} "{account_name}" {sibling}'


def _sibling_block_phrase(account_id: int, at: datetime, until: Optional[datetime]) -> Optional[str]:
    """'shares login "main" with "skyline-curated", which is blocked until 10:00 PM' for the
    first sibling whose block reaches this account at `at`, else None."""
    from .database import Account, Login
    _, siblings = _with_siblings([account_id])
    found = siblings.get(account_id) or {}
    if not found:
        return None
    blocking = {b.account_id for b in blocks_at(at, found)}
    if not blocking:
        return None
    other = min(blocking)
    login_ids = found[other]
    names = dict(db.session.query(Account.id, Account.name).filter(Account.id == other).all())
    logins = [n for (n,) in db.session.query(Login.name).filter(Login.id.in_(login_ids))
              .order_by(Login.id).all()]
    login_text = (f'login "{logins[0]}"' if len(logins) == 1
                  else 'logins ' + ', '.join(f'"{n}"' for n in logins))
    tail = f' until {until_label(until)}' if until is not None else ''
    return (f'shares {login_text} with "{names.get(other, f"account {other}")}", which is '
            f'blocked{tail}')


def upcoming_by_account(account_ids: Optional[Iterable[int]] = None
                        ) -> Dict[int, List[BlockWindow]]:
    """{account_id: [blocks in force now or still to come]}, soonest first - what the account
    pages list. One query however many accounts are asked about."""
    now = datetime.utcnow()
    out: Dict[int, List[BlockWindow]] = {}
    for b in blocks_overlapping(now, datetime.max, account_ids):
        out.setdefault(b.account_id, []).append(b)
    for rows in out.values():
        rows.sort(key=lambda b: (b.start, b.id))
    return out


def block_views(account_ids: Optional[Iterable[int]] = None) -> Dict[int, List[dict]]:
    """{account_id: [one dict per block in force or still to come]} for the account pages:
    the badge, the banner and its Unblock buttons. Three statements however many accounts
    and blocks - the blocks, the recording names, and the display settings."""
    from .database import Recording
    from .tz_utils import get_display_tz, is_24h
    by_account = upcoming_by_account(account_ids)
    if not by_account:
        return {}
    tied = {b.recording_id for rows in by_account.values() for b in rows if b.recording_id}
    names = ({rid: name for rid, name in db.session.query(Recording.id, Recording.name)
              .filter(Recording.id.in_(tied))} if tied else {})
    tz, h24 = get_display_tz(), is_24h()
    now = datetime.utcnow()
    out: Dict[int, List[dict]] = {}
    for account_id, rows in by_account.items():
        out[account_id] = [{
            'id': b.id,
            'active': b.start <= now,
            'stop': b.stop,
            'start_label': until_label(b.start, tz, h24),
            'stop_label': until_label(b.stop, tz, h24),
            'slots': b.slots,
            'recording_id': b.recording_id,
            'recording_name': names.get(b.recording_id),
        } for b in rows]
    return out


# ── Writers. The explicit user action is the only caller of each. ──────────────


def add_account_block(account_id: int, stop: datetime, start: Optional[datetime] = None,
                      slots: Optional[int] = None) -> int:
    """Block `account_id` from `start` (default now) to `stop`. Returns the new block's id."""
    from .database import AccountBlock
    start = start or datetime.utcnow()

    @retry_on_locked()
    def _add_and_commit():
        row = AccountBlock(account_id=account_id, start_time=start, stop_time=stop, slots=slots)
        db.session.add(row)
        db.session.commit()
        return row.id

    block_id = _add_and_commit()
    log.info('Account %d blocked from %s to %s (%s)', account_id, start, stop,
             'every slot' if slots is None else f'{slots} slot(s)')
    return block_id


def end_account_block(block_id: int) -> Optional[int]:
    """Take an account's block away. Returns the account id, or None when there was no such
    block. A recording's blocks are changed through set_recording_blocks(), which says so on
    the recording."""
    from .database import AccountBlock

    @retry_on_locked()
    def _end_and_commit():
        row = db.session.get(AccountBlock, block_id)
        if row is None or row.recording_id is not None:
            return None
        account_id = row.account_id
        db.session.delete(row)
        db.session.commit()
        return account_id

    account_id = _end_and_commit()
    if account_id is not None:
        log.info('Account block %d on account %d ended by the user', block_id, account_id)
    return account_id


def recording_blocks(recording_id: int) -> Dict[int, Optional[int]]:
    """{account_id: slots} for the blocks set on one recording."""
    from .database import AccountBlock
    return {b.account_id: b.slots
            for b in AccountBlock.query.filter_by(recording_id=recording_id).all()}


def set_recording_blocks(recording_id: int, wanted: Dict[int, Optional[int]]) -> bool:
    """Make the recording's blocks exactly `wanted` ({account_id: slots}). Writes a
    RECORDING_EDITED event naming what moved, in the same commit - a block changes what the
    recording may record from, so it never moves without the recording saying so. Returns
    True when anything changed."""
    from .database import Account, AccountBlock, RECORDING_EDITED, add_recording_event

    names = {a.id: a.name for a in Account.query.filter(
        Account.id.in_(set(wanted) | set(recording_blocks(recording_id)))).all()}
    wanted = {aid: slots for aid, slots in wanted.items() if aid in names}

    def _label(aid):
        slots = wanted.get(aid)
        share = '' if slots is None else f' ({slots} connection{"s" if slots != 1 else ""})'
        return f'"{names[aid]}"{share}'

    def _detail(added, changed, removed):
        parts = []
        if added or changed:
            parts.append('Blocks account use during this recording: '
                         + ', '.join(_label(aid) for aid in added + changed))
        if removed:
            parts.append('No longer blocks: ' + ', '.join(f'"{names[aid]}"' for aid in removed))
        return '. '.join(parts)

    @retry_on_locked()
    def _replace_and_commit():
        current = {b.account_id: b for b in
                   AccountBlock.query.filter_by(recording_id=recording_id).all()}
        added = [aid for aid in wanted if aid not in current]
        removed = [aid for aid in current if aid not in wanted]
        changed = [aid for aid in wanted if aid in current and current[aid].slots != wanted[aid]]
        if not (added or removed or changed):
            return False
        for aid in removed:
            db.session.delete(current[aid])
        for aid in changed:
            current[aid].slots = wanted[aid]
        for aid in added:
            db.session.add(AccountBlock(account_id=aid, recording_id=recording_id,
                                        slots=wanted[aid]))
        add_recording_event(recording_id, RECORDING_EDITED,
                            detail=_detail(added, changed, removed),
                            extra={'kind': 'account_blocks',
                                   'blocked_account_ids': sorted(wanted),
                                   'removed_account_ids': sorted(removed)})
        db.session.commit()
        return True

    return _replace_and_commit()


def rearm_waiting_starts():
    """Re-arm every recording whose start is being held, so one waiting on a block that was
    just ended starts now rather than at its next scheduled retry. Harmless for one waiting
    on something else: a start re-decides everything from the top and defers again."""
    from .database import Recording, REC_STATUS_SCHEDULED
    from .scheduler import get_scheduler, reschedule_recording_start
    if not get_scheduler():
        return
    now = datetime.utcnow()
    waiting = [rid for (rid,) in db.session.query(Recording.id).filter(
        Recording.status == REC_STATUS_SCHEDULED,
        Recording.start_deferred_since.isnot(None),
        Recording.start_time <= now, Recording.stop_time > now)]
    for rid in waiting:
        reschedule_recording_start(rid, now)
