"""Process-wide admission control for database-heavy background work.

**The defect this closes.** Every mutual-avoidance rule between background jobs used to be
shaped `if other_actor.is_running(): defer` - a check, then a start, with nothing holding
the two together. Two jobs that check within the same millisecond both see "clear" and both
proceed. Observed for real while restoring a 1.5 GB database into a fresh container: the
boot log reads `02:23:45,298 Fetching M3U for account 1` then `02:23:45,302 Starting channel
test run: 17 channels`, because the sync's tester check ran in the 4 ms window before the
tester registered itself, so the documented deferral never fired. Startup is the worst case,
since APScheduler fires every missed catch-up job at once on separate worker threads.

**The mechanism.** One lock spans "read who is running" and "register me", so the decision
and the start cannot be pulled apart. A caller asks `try_start(kind, label)` and gets either
a `Ticket` (it is registered and may proceed) or a `Refusal` naming what blocked it. There
is no other correct way to ask - a caller that reads `active_kinds()` and then starts has
rebuilt the original race.

**This is the database/CPU axis, not the provider-connection axis.** `DESIGN-concurrency.md`
models contention over provider connections, deliberately keeps sync out of its slot system,
and is settled - nothing here re-decides it. What had no model at all was contention over
the database: several large writers arriving at one SQLite file at once. Reasoning and the
yield-order argument: `dev/docs/DESIGN-db-admission.md`.

**Recordings are deliberately not clients of this registry.** A recording never waits and is
never refused, so a ticket buys the recorder nothing; what every other actor needs is "is a
recording running or imminent", and the authority for that is the database
(`REC_STATUS_IN_PROGRESS` plus the SCHEDULED lookahead), which is correct across a restart
and across processes as an in-memory flag would not be. Those guards stay where they are and
run *before* the admission call. A leaked recording ticket would also silently block all
background work for the length of a recording.

**Two rules for anyone extending this module:**

1. **The admission lock is a leaf.** Never hold it across a database query, a `load_config()`,
   a log call, or any call into another module. Everything under `_lock` is dict and tuple
   work on in-memory state. This is what keeps the registry out of any lock-ordering cycle
   (`DESIGN-concurrency.md` §2 verified none exists; this module must not introduce one).
   Callers may take the lock while holding their own module lock - the tester does - but
   never the reverse.
2. **Every ticket is released in a `finally`, on every terminal path.** A ticket that leaks
   blocks its kind's dependents for the life of the process. `release()` is idempotent so a
   defensive second call is free, and `describe_active()` exists so a leak is diagnosable
   rather than mysterious.
"""
import logging
import threading
from dataclasses import dataclass
from datetime import datetime

log = logging.getLogger(__name__)

KIND_TESTER = 'tester'
KIND_SYNC = 'sync'
KIND_REBUILD = 'rebuild'
KIND_MAINTENANCE = 'maintenance'
KIND_HIDING = 'hiding'

# Human prose for a refusal message, so a refused caller can hand the reason straight to a
# user-facing surface without restating it. Keep these lowercase noun phrases - they are
# composed into "<label> is already running".
KIND_LABELS = {
    KIND_TESTER: 'a channel test run',
    KIND_SYNC: 'an account sync',
    KIND_REBUILD: 'a search index rebuild',
    KIND_MAINTENANCE: 'database maintenance',
    KIND_HIDING: 'a channel hide rule pass',
}

# The yield order, as a table rather than as branches scattered across four modules.
# `BLOCKED_BY[kind]` names every kind whose presence refuses `kind`. Reading down:
#
# - The **tester** yields to nothing on this axis. It is the lightest database user of the
#   four (a handful of rows per channel) and the longest-running, so making anything defer
#   it would starve that thing for hours. Its own single-run-globally check
#   (`channel_tester._state.is_running` under `channel_tester._lock`) remains the authority
#   for tester-vs-tester, and is already race-free; an empty tuple here means the tester is
#   *registering* its presence for everyone else to see, not asking permission.
# - **Sync** yields to the tester (the polarity settled in `DESIGN-concurrency.md` §5.2, kept
#   verbatim) and to another sync. Cross-account syncs were only ever kept apart by a
#   5-minute start stagger, which a big playlist outlasts.
# - **Rebuild** yields to sync: a rebuild reads the tables a sync is still writing, and the
#   sync's own close-out rebuild will cover that work anyway. The sync close-out's own
#   rebuild passes force=True - it is the tail of an already-admitted sync, not a competitor.
#   It also yields to another rebuild, which matters only to the one caller that asks rather
#   than forces (the index janitor): _rebuild_lock in app/search_index.py would otherwise
#   make it *wait out* a manual rebuild - up to ~76s parked on a scheduler thread - to then
#   rebuild an index that was just rebuilt. The forcing callers are unaffected, so this entry
#   costs them nothing.
# - **Hiding** - the channel-hide-rule materializer (app/channel_hiding.py) - yields to sync
#   and to itself, the same shape as rebuild and for the first of the same reasons: it
#   rewrites `channels` rows a sync is still writing. It deliberately does NOT yield to the
#   tester (hours long, and hiding would starve behind it) or to a rebuild (up to ~76s, and
#   the caller here is a person who just saved a rule and is waiting on the answer). A
#   refusal is never silent: the rule is saved either way and a retry is queued, so the only
#   cost of yielding is that the new answer lands late.
# - **Maintenance** yields to everything, itself included. It is the most deferrable work in
#   the app (pruning, retention, WAL reporting) and the heaviest per run, because EPG cleanup
#   drags a full `programs` rebuild behind it.
#
# Recordings appear in no tuple by design - see the module docstring.
BLOCKED_BY = {
    KIND_TESTER: (),
    KIND_SYNC: (KIND_TESTER, KIND_SYNC),
    KIND_REBUILD: (KIND_SYNC, KIND_REBUILD),
    KIND_HIDING: (KIND_SYNC, KIND_HIDING),
    KIND_MAINTENANCE: (KIND_TESTER, KIND_SYNC, KIND_REBUILD, KIND_HIDING, KIND_MAINTENANCE),
}


@dataclass(frozen=True)
class Ticket:
    """Permission to run, and the handle that must be released when the work ends."""
    kind: str
    label: str
    seq: int
    started_at: datetime
    granted: bool = True


@dataclass(frozen=True)
class Refusal:
    """Permission denied, with the blocker named. `reason` is user-facing prose."""
    kind: str
    blocked_by: str
    reason: str
    granted: bool = False


_lock = threading.Lock()
_active = {}     # seq -> Ticket
_next_seq = 1


def _age_phrase(started_at, now):
    seconds = int((now - started_at).total_seconds())
    if seconds < 60:
        return f'{seconds}s'
    return f'{seconds // 60}m'


def try_start(kind: str, label: str = '', force: bool = False):
    """Ask permission to start `kind`, and register it in the same breath.

    Returns a `Ticket` (`.granted` True) or a `Refusal` (`.granted` False). Check
    `.granted` - never `active_kinds()` followed by a start, which is the race this module
    exists to close.

    `label` names this particular run for the log line and for a refusal message someone
    else reads ('Account 3', 'daily maintenance'). `force=True` registers unconditionally
    and never refuses: it is for actions a present user has already been warned about and
    chosen to take (the manual sync and manual rebuild override paths,
    `DESIGN-concurrency.md` §5.4), and for work that is the tail of an already-admitted run.

    Callers must apply their recording guards *before* calling this - recordings are not
    clients of this registry (see the module docstring).
    """
    if kind not in BLOCKED_BY:
        raise ValueError(f'unknown admission kind {kind!r}')

    global _next_seq
    now = datetime.utcnow()
    blockers = BLOCKED_BY[kind]

    with _lock:
        blocker = None
        if not force:
            for held in _active.values():
                if held.kind in blockers:
                    blocker = held
                    break
        if blocker is None:
            seq = _next_seq
            _next_seq += 1
            ticket = Ticket(kind=kind, label=label, seq=seq, started_at=now)
            _active[seq] = ticket
        else:
            ticket = None
            blocker_kind = blocker.kind
            blocker_label = blocker.label
            blocker_age = _age_phrase(blocker.started_at, now)

    # Logging is I/O, so it happens outside the lock - see rule 1 in the module docstring.
    if ticket is not None:
        log.info('Admission: %s started%s%s', kind,
                 f' ({label})' if label else '',
                 ' [forced]' if force else '')
        return ticket

    detail = f'{KIND_LABELS[blocker_kind]} is already running'
    if blocker_label:
        detail += f' ({blocker_label})'
    detail += f', started {blocker_age} ago'
    log.info('Admission: %s refused%s - %s', kind, f' ({label})' if label else '', detail)
    return Refusal(kind=kind, blocked_by=blocker_kind, reason=detail)


def release(ticket) -> None:
    """Give back a ticket. Idempotent, and a no-op for a Refusal, so a `finally` can call it
    unconditionally without the caller re-deriving whether permission was ever granted."""
    if ticket is None or not getattr(ticket, 'granted', False):
        return
    with _lock:
        removed = _active.pop(ticket.seq, None)
    if removed is not None:
        log.info('Admission: %s finished%s', ticket.kind,
                 f' ({ticket.label})' if ticket.label else '')


def active_kinds() -> set:
    """The kinds currently holding a ticket. Diagnostics and UI only - deciding whether to
    start from this is the check-then-act race this module replaces."""
    with _lock:
        return {t.kind for t in _active.values()}


def describe_active() -> list:
    """`[(kind, label, age_seconds)]` for every held ticket, newest last. For the logs and
    for finding a leaked ticket, which otherwise presents only as work that never runs."""
    now = datetime.utcnow()
    with _lock:
        held = sorted(_active.values(), key=lambda t: t.seq)
    return [(t.kind, t.label, int((now - t.started_at).total_seconds())) for t in held]


def reset_for_tests() -> None:
    """Drop every ticket. Test-support only: a run abandoned mid-flight would otherwise
    refuse the next test module's work (tests/support/app.py::reset_module_globals)."""
    global _next_seq
    with _lock:
        _active.clear()
        _next_seq = 1
