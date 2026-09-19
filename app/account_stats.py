"""Per-account usage and health numbers for the Accounts pages (dev/changelog/1028).

Two kinds, and the split is the design:

- **Current state** (`current_stats`) describes each account as it is right now - channels,
  group membership, guide reach, EPG coverage, health bands, what is failing. It ignores the
  time window and is read straight from the live tables in a fixed number of grouped
  queries, however many accounts there are.
- **Windowed** (`windowed_stats`, `trend`) comes from the ledger, `account_stat_days`: one
  row per account per local day holding what that day produced. Computing these from the
  raw tables was measured at seconds per page on a multi-year install, and would also
  shrink silently as channel tests are pruned and recordings deleted. The ledger costs
  milliseconds forever and keeps a deleted row's contribution, the same way
  `health_recompute.py` leaves it baked into a stored score.

How the ledger is filled (`refresh_ledger`): each source table - recording segments,
channel tests, channel events - is read above its watermark on `AccountStatState`, bucketed
by the local day it started, ADDED to the day rows, and the watermark moved, all in one
commit. Adding is safe precisely because the watermark moves in the same transaction as the
counts: a crash loses both or neither. Nothing ever subtracts.

A watermark only passes a FINISHED row, and stops before the first unfinished one, so a
row that is still running is folded on a later pass once it has its final numbers:

- a segment is finished when `ended_at` is set. Its `excluded_reason` is written in that
  same commit (app/watchdog.py), so exclusion is final by then too.
- a test is finished when `test_ended_at` is set. Tests a restart interrupted are closed as
  CANCELLED at startup (app/scheduler.py), so none stays open forever.
- an event is final when it is written.

What each source contributes:

- **segment** -> `segments` and `stalls` always; `capture_seconds` (its wall clock,
  credited to the account of the channel that captured it - never the recording's final
  channel, which failover rewrites) and `recordings` (on the recording's first joined
  segment on that account) only when it was not excluded. An excluded segment is the
  provider's placeholder clip: the stalls around it were real, the "capture" was not.
- **test** -> `checks_passed` (COMPLETED) or `checks_failed` (FAILED). A CANCELLED test
  says nothing about the channel and counts in neither.
- **event** -> `failovers_away`, for the four health observations a recording writes when
  it has to leave a channel (`FAILOVER_EVENT_TYPES`).

Rows whose channel is gone (deleted before they were folded) are passed with no account to
credit. A **rebuild** (`rebuild_ledger`, run automatically when the display timezone
changes, since every day boundary moved) truncates and refolds from the rows that still
exist, so it is the one path that forgets pruned history.
"""
import logging
import threading
from bisect import bisect_right
from collections import defaultdict
from datetime import date, datetime, timedelta

from sqlalchemy import case, func
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from . import admission, db
from .channel_groups import DEFAULT_FAILING_STREAK_THRESHOLD, guide_scope_channel_ids
from .channel_search import effective_health, health_band_expr
from .database import (Account, AccountStatDay, AccountStatState, Channel, ChannelEvent,
                       ChannelGroup, ChannelGroupMember, ChannelTest, EPGEntry,
                       RecordingSegment, CHANNEL_FAILOVER_HEALTH_OBSERVATION,
                       CHANNEL_FAST_DELIVERY_HEALTH_OBSERVATION,
                       CHANNEL_PLACEHOLDER_HEALTH_OBSERVATION,
                       CHANNEL_STALL_DEMOTION_HEALTH_OBSERVATION, TEST_STATUS_COMPLETED,
                       TEST_STATUS_FAILED)
from .db_utils import retry_on_locked
from . import health_bands, tz_utils

log = logging.getLogger(__name__)

WINDOWS = ('7d', '30d', '90d', 'all')
DEFAULT_WINDOW = 'all'
_WINDOW_DAYS = {'7d': 7, '30d': 30, '90d': 90}

# The four ways a recording leaves a channel mid-run: the feed died, it kept stalling, it
# served the provider's offline placeholder, or it delivered faster than real time.
FAILOVER_EVENT_TYPES = (CHANNEL_FAILOVER_HEALTH_OBSERVATION,
                        CHANNEL_STALL_DEMOTION_HEALTH_OBSERVATION,
                        CHANNEL_PLACEHOLDER_HEALTH_OBSERVATION,
                        CHANNEL_FAST_DELIVERY_HEALTH_OBSERVATION)

# Source rows folded per commit. Bounds one write transaction, and is the resume unit of an
# interrupted rebuild.
FOLD_CHUNK = 5_000
# Above this many unfolded source rows a page load does not fold inline - it starts the
# background catch-up and says so. Folding runs at roughly 30,000 rows/s on the reference
# box, so this caps an inline fold near a third of a second (dev/changelog/1028). The hourly
# job keeps the routine backlog far below it.
INLINE_FOLD_LIMIT = 10_000
# How long a page load waits for a fold another thread is already running (the hourly job,
# a concurrent page load) before rendering what the ledger holds.
_INLINE_LOCK_WAIT_SECONDS = 5

_LEDGER_COLUMNS = ('capture_seconds', 'segments', 'recordings', 'stalls',
                   'checks_passed', 'checks_failed', 'failovers_away')

# Held across read-watermark -> fold -> commit. The watermark comparison inside one
# transaction makes a fold idempotent, but two folders reading the same watermark before
# either commits would both add the same rows.
_fold_lock = threading.Lock()
# Guards the check-and-start of the background catch-up within this process; admission's
# KIND_LEDGER entry is what refuses it against a sync or maintenance.
_catch_up_start_lock = threading.Lock()
# The running (or last failed) background catch-up: {'done', 'total', 'error', 'thread'}.
_catch_up = {}


# ---------------------------------------------------------------------------
# The fold
# ---------------------------------------------------------------------------

def _state():
    return db.session.get(AccountStatState, 1)


def _new_state(tz_name):
    state = AccountStatState(id=1, segment_watermark=0, test_watermark=0, event_watermark=0,
                             tz_name=tz_name)
    db.session.add(state)
    return state


def _first_unfinished_id(model, finished_col, watermark):
    return (db.session.query(func.min(model.id))
            .filter(model.id > watermark, finished_col.is_(None)).scalar())


def _local_day(dt, tz):
    return tz_utils.to_local(dt, tz).date().isoformat()


def _fold_segments(state, tz, inc, limit):
    """Add segments above the watermark to `inc`. Returns (new watermark, more pending)."""
    wm = state.segment_watermark
    stop = _first_unfinished_id(RecordingSegment, RecordingSegment.ended_at, wm)
    q = (db.session.query(RecordingSegment.id, RecordingSegment.recording_id,
                          Channel.account_id, RecordingSegment.started_at,
                          RecordingSegment.ended_at, RecordingSegment.stall_count,
                          RecordingSegment.excluded_reason)
         .outerjoin(Channel, Channel.id == RecordingSegment.channel_id)
         .filter(RecordingSegment.id > wm))
    if stop is not None:
        q = q.filter(RecordingSegment.id < stop)
    rows = q.order_by(RecordingSegment.id).limit(limit).all()
    if not rows:
        return wm, False

    # The first joined segment of each recording on each account. Every lower id is already
    # finished (the watermark never passes an unfinished row), so "the minimum id among
    # joined segments" is final by the time any segment is folded.
    rec_ids = {r.recording_id for r in rows}
    firsts = {sid for (sid,) in (
        db.session.query(func.min(RecordingSegment.id))
        .join(Channel, Channel.id == RecordingSegment.channel_id)
        .filter(RecordingSegment.recording_id.in_(rec_ids),
                RecordingSegment.excluded_reason.is_(None))
        .group_by(RecordingSegment.recording_id, Channel.account_id))}

    for r in rows:
        if r.account_id is None:
            continue
        day = inc[(r.account_id, _local_day(r.started_at, tz))]
        day['segments'] += 1
        day['stalls'] += r.stall_count or 0
        if r.excluded_reason is None:
            day['capture_seconds'] += max(0.0, (r.ended_at - r.started_at).total_seconds())
            if r.id in firsts:
                day['recordings'] += 1
    return rows[-1].id, len(rows) == limit


def _fold_tests(state, tz, inc, limit):
    wm = state.test_watermark
    stop = _first_unfinished_id(ChannelTest, ChannelTest.test_ended_at, wm)
    q = (db.session.query(ChannelTest.id, Channel.account_id, ChannelTest.test_started_at,
                          ChannelTest.status)
         .outerjoin(Channel, Channel.id == ChannelTest.channel_id)
         .filter(ChannelTest.id > wm))
    if stop is not None:
        q = q.filter(ChannelTest.id < stop)
    rows = q.order_by(ChannelTest.id).limit(limit).all()
    if not rows:
        return wm, False
    for r in rows:
        if r.account_id is None:
            continue
        if r.status == TEST_STATUS_COMPLETED:
            inc[(r.account_id, _local_day(r.test_started_at, tz))]['checks_passed'] += 1
        elif r.status == TEST_STATUS_FAILED:
            inc[(r.account_id, _local_day(r.test_started_at, tz))]['checks_failed'] += 1
        # TEST_STATUS_CANCELLED: passed by the watermark, counted nowhere.
    return rows[-1].id, len(rows) == limit


def _fold_events(state, tz, inc, limit):
    wm = state.event_watermark
    # Read the ceiling first: a row committed after it has a higher id, so it cannot be
    # skipped by moving the watermark to the ceiling.
    ceiling = db.session.query(func.max(ChannelEvent.id)).scalar() or 0
    if ceiling <= wm:
        return wm, False
    rows = (db.session.query(ChannelEvent.id, Channel.account_id, ChannelEvent.timestamp)
            .outerjoin(Channel, Channel.id == ChannelEvent.channel_id)
            .filter(ChannelEvent.id > wm, ChannelEvent.id <= ceiling,
                    ChannelEvent.event_type.in_(FAILOVER_EVENT_TYPES))
            .order_by(ChannelEvent.id).limit(limit).all())
    for r in rows:
        if r.account_id is not None:
            inc[(r.account_id, _local_day(r.timestamp, tz))]['failovers_away'] += 1
    if len(rows) == limit:
        return rows[-1].id, True
    return ceiling, False


_INSERT_DAY = sqlite_insert(AccountStatDay.__table__)
_UPSERT = _INSERT_DAY.on_conflict_do_update(
    index_elements=['account_id', 'day'],
    set_={c: AccountStatDay.__table__.c[c] + _INSERT_DAY.excluded[c] for c in _LEDGER_COLUMNS})


def _fold_chunk(tz, tz_name, limit):
    """Fold up to `limit` rows of each source and commit them with their watermarks.

    Returns (rows passed, more pending). The whole read-modify-write is one retried unit and
    re-reads the state row inside it, so a retry after a lock refolds from the committed
    watermark rather than adding a half-built increment twice."""

    @retry_on_locked()
    def _fold_and_commit():
        state = _state() or _new_state(tz_name)
        inc = defaultdict(lambda: defaultdict(float))
        seg_wm, seg_more = _fold_segments(state, tz, inc, limit)
        test_wm, test_more = _fold_tests(state, tz, inc, limit)
        event_wm, event_more = _fold_events(state, tz, inc, limit)
        passed = ((seg_wm - state.segment_watermark) + (test_wm - state.test_watermark)
                  + (event_wm - state.event_watermark))

        if inc:
            # One executemany upsert on uq_account_stat_day, adding to whatever the day
            # already holds - a statement per day row would scale with the history folded.
            db.session.execute(_UPSERT, [
                {'account_id': account_id, 'day': day,
                 **{c: (counts.get(c, 0) if c == 'capture_seconds' else int(counts.get(c, 0)))
                    for c in _LEDGER_COLUMNS}}
                for (account_id, day), counts in inc.items()])

        # Anchors only move forward.
        state.segment_watermark = max(state.segment_watermark, seg_wm)
        state.test_watermark = max(state.test_watermark, test_wm)
        state.event_watermark = max(state.event_watermark, event_wm)
        more = seg_more or test_more or event_more
        now = datetime.utcnow()
        state.refreshed_at = now
        if not more and state.rebuild_started_at is not None and (
                state.rebuilt_at is None or state.rebuilt_at < state.rebuild_started_at):
            state.rebuilt_at = now
            log.info('Account stats ledger rebuild finished')
        db.session.commit()
        return passed, more

    return _fold_and_commit()


def _reset_for_rebuild(tz_name, reason):
    """Truncate the ledger and zero the watermarks, in one commit, so the only crash states
    are "not started" and "resumable": the refold that follows is an ordinary refresh."""

    @retry_on_locked()
    def _reset_and_commit():
        AccountStatDay.query.delete(synchronize_session=False)
        state = _state() or _new_state(tz_name)
        state.segment_watermark = 0
        state.test_watermark = 0
        state.event_watermark = 0
        state.tz_name = tz_name
        state.rebuild_started_at = datetime.utcnow()
        db.session.commit()

    log.warning('Rebuilding the account stats ledger: %s. Rows pruned since they were first '
                'counted will no longer be included.', reason)
    _reset_and_commit()


def _rebuild_reason(state, tz_name):
    """Why the ledger must be rebuilt before folding, or None."""
    if state is not None and state.tz_name and state.tz_name != tz_name:
        return (f'the display timezone changed from {state.tz_name} to {tz_name}, '
                f'so every day boundary moved')
    return None


def _fold_all(tz, tz_name, progress=None):
    """Fold until nothing is pending. Caller holds `_fold_lock`."""
    state = _state()
    reason = _rebuild_reason(state, tz_name)
    if reason:
        _reset_for_rebuild(tz_name, reason)
    elif state is not None and state.rebuild_started_at is not None and (
            state.rebuilt_at is None or state.rebuilt_at < state.rebuild_started_at):
        log.warning('Resuming an interrupted account stats ledger rebuild (started %s UTC) '
                    'from segment %d, test %d, event %d', state.rebuild_started_at,
                    state.segment_watermark, state.test_watermark, state.event_watermark)
    total = 0
    while True:
        passed, more = _fold_chunk(tz, tz_name, FOLD_CHUNK)
        total += passed
        if progress is not None:
            progress(passed)
        if not more:
            return total


def refresh_ledger():
    """Bring the ledger up to date, synchronously. Returns how many source rows it passed.

    Rebuilds first when the display timezone differs from the one the days were bucketed
    in. Blocks on a fold already running elsewhere; page routes use `ensure_fresh()`,
    which does not."""
    tz_name = tz_utils.get_display_tz_name()
    tz = tz_utils.get_display_tz()
    with _fold_lock:
        return _fold_all(tz, tz_name)


def rebuild_ledger(reason='requested'):
    """Truncate and refold the ledger from the source rows that still exist.

    Forgets every contribution whose source row has since been pruned or deleted - that is
    what a rebuild means, and why nothing runs it except a timezone change."""
    tz_name = tz_utils.get_display_tz_name()
    tz = tz_utils.get_display_tz()
    with _fold_lock:
        _reset_for_rebuild(tz_name, reason)
        return _fold_all(tz, tz_name)


def pending_rows():
    """Source rows above the watermarks - an upper bound on what the next fold reads."""
    state = _state()
    seg_wm = state.segment_watermark if state else 0
    test_wm = state.test_watermark if state else 0
    event_wm = state.event_watermark if state else 0
    return (db.session.query(func.count(RecordingSegment.id))
            .filter(RecordingSegment.id > seg_wm).scalar()
            + db.session.query(func.count(ChannelTest.id))
            .filter(ChannelTest.id > test_wm).scalar()
            + db.session.query(func.count(ChannelEvent.id))
            .filter(ChannelEvent.id > event_wm).scalar())


def _catch_up_notice():
    if _catch_up.get('error'):
        return {'text': 'Usage numbers may be behind: the last attempt to bring them up to '
                        f'date failed ({_catch_up["error"]}). It is retried on the next '
                        'page load.', 'level': 'warn'}
    return {'text': 'Usage numbers are being brought up to date '
                    f'({_catch_up.get("done", 0):,} of {_catch_up.get("total", 0):,} rows '
                    'read).', 'level': 'info'}


def _catch_up_worker(app, ticket, tz, tz_name):
    try:
        with app.app_context():
            with _fold_lock:
                def _progress(n):
                    _catch_up['done'] = _catch_up.get('done', 0) + n
                _fold_all(tz, tz_name, _progress)
        _catch_up.clear()
    except Exception as exc:
        log.exception('Account stats catch-up failed')
        _catch_up['error'] = str(exc) or exc.__class__.__name__
        _catch_up.pop('thread', None)
    finally:
        admission.release(ticket)


def ensure_fresh(app):
    """Make the ledger current enough to render, without ever making a page wait long.

    Returns None when the numbers are current, or a notice dict (`text`, `level`) the page
    shows above the usage numbers when they are not: a large backlog is folded on a
    background thread under an admission ticket, and a refusal names what refused it."""
    thread = _catch_up.get('thread')
    if thread is not None and thread.is_alive():
        return _catch_up_notice()

    tz_name = tz_utils.get_display_tz_name()
    state = _state()
    rebuild = _rebuild_reason(state, tz_name) is not None
    pending = pending_rows()
    if pending == 0 and not rebuild:
        return None

    if not rebuild and pending <= INLINE_FOLD_LIMIT:
        if not _fold_lock.acquire(timeout=_INLINE_LOCK_WAIT_SECONDS):
            return {'text': 'Usage numbers are being brought up to date.', 'level': 'info'}
        try:
            _fold_all(tz_utils.get_display_tz(), tz_name)
        finally:
            _fold_lock.release()
        _catch_up.pop('error', None)
        return None

    with _catch_up_start_lock:
        thread = _catch_up.get('thread')
        if thread is not None and thread.is_alive():
            return _catch_up_notice()
        ticket = admission.try_start(admission.KIND_LEDGER, 'account stats')
        if not ticket.granted:
            return {'text': f'Usage numbers are behind ({pending:,} rows not yet read): '
                            f'{ticket.reason}. They catch up on a later page load.',
                    'level': 'info'}
        try:
            _catch_up.clear()
            _catch_up.update(done=0, total=pending)
            thread = threading.Thread(target=_catch_up_worker, name='account-stats-catch-up',
                                      args=(app, ticket, tz_utils.get_display_tz(), tz_name),
                                      daemon=True)
            _catch_up['thread'] = thread
            thread.start()
        except Exception:
            admission.release(ticket)
            raise
    return _catch_up_notice()


def wait_for_catch_up(timeout=None):
    """Join the background catch-up, if one is running. For tests and shutdown."""
    thread = _catch_up.get('thread')
    if thread is not None:
        thread.join(timeout)


def reset_for_tests():
    """Finish any catch-up and forget its progress or error. Test-support only
    (tests/support/app.py::reset_module_globals): a leftover error would put a stale notice
    on the next module's page."""
    wait_for_catch_up(30)
    _catch_up.clear()


# ---------------------------------------------------------------------------
# Windowed readers
# ---------------------------------------------------------------------------

def today_local(tz=None):
    return datetime.now(tz or tz_utils.get_display_tz()).date()


def window_bounds(window, today):
    """(first day, last day) of `window` as YYYY-MM-DD strings, both inclusive; the first is
    None for 'all'. A window is local days back from `today`, today included."""
    if window not in WINDOWS:
        raise ValueError(f'unknown window {window!r}')
    last = today.isoformat()
    if window == 'all':
        return None, last
    return (today - timedelta(days=_WINDOW_DAYS[window] - 1)).isoformat(), last


def _empty_totals():
    totals = {c: 0 for c in _LEDGER_COLUMNS}
    totals['capture_seconds'] = 0.0
    totals['pass_rate'] = None
    return totals


def _with_pass_rate(totals):
    checked = totals['checks_passed'] + totals['checks_failed']
    # None, never 0: an account with no checks has not failed any.
    totals['pass_rate'] = totals['checks_passed'] / checked if checked else None
    return totals


def windowed_stats(account_ids, window, today):
    """{account_id: {ledger column: sum over the window, 'pass_rate': share or None}} for
    every id asked for (zeros for an account with no ledger rows), in one query."""
    out = {aid: _empty_totals() for aid in account_ids}
    if not account_ids:
        return out
    first, last = window_bounds(window, today)
    q = (db.session.query(AccountStatDay.account_id,
                          *[func.sum(getattr(AccountStatDay, c)) for c in _LEDGER_COLUMNS])
         .filter(AccountStatDay.account_id.in_(account_ids), AccountStatDay.day <= last))
    if first is not None:
        q = q.filter(AccountStatDay.day >= first)
    for row in q.group_by(AccountStatDay.account_id):
        totals = out[row[0]]
        for col, value in zip(_LEDGER_COLUMNS, row[1:]):
            totals[col] = value or 0
    for totals in out.values():
        _with_pass_rate(totals)
    return out


def _week_start(d):
    """The Sunday on or before `d` - weeks run Sunday to Saturday."""
    return d - timedelta(days=(d.weekday() + 1) % 7)


def _add_months(d, n):
    months = d.year * 12 + d.month - 1 + n
    return date(months // 12, months % 12 + 1, 1)


# All time starts weekly and steps up to the next coarser unit whenever the finer one would
# pass this many columns, so the chart never grows past it in any realistic history.
MAX_TREND_BUCKETS = 26


def _bucket_starts(unit, first, last):
    """Bucket start dates covering first..last, in order."""
    if unit == 'day':
        n = (last - first).days + 1
        return [first + timedelta(days=i) for i in range(n)]
    if unit == 'week':
        start = _week_start(first)
        return [start + timedelta(weeks=i) for i in range((last - start).days // 7 + 1)]
    step = {'month': 1, 'quarter': 3, 'year': 12}[unit]
    if unit == 'month':
        start = date(first.year, first.month, 1)
    elif unit == 'quarter':
        start = date(first.year, (first.month - 1) // 3 * 3 + 1, 1)
    else:
        start = date(first.year, 1, 1)
    starts = []
    while start <= last:
        starts.append(start)
        start = _add_months(start, step)
    return starts


def _trend_unit(window, first, last):
    if window == '7d':
        return 'day'
    if window in ('30d', '90d'):
        return 'week'
    for unit in ('week', 'month', 'quarter'):
        if len(_bucket_starts(unit, first, last)) <= MAX_TREND_BUCKETS:
            return unit
    return 'year'


_TREND_COLUMNS = ('capture_seconds', 'checks_passed', 'checks_failed')


def trend(account_ids, window, today):
    """The windowed trend, bucketed per the approved rule (dev/changelog/1027): 7 days is
    daily, 30 and 90 days weekly, all time weekly until that would pass
    MAX_TREND_BUCKETS columns, then monthly, quarterly (calendar quarters), yearly.
    Weeks run Sunday to Saturday.

    Returns {'unit', 'buckets': [{'start', 'end', 'values': {account_id: {capture_seconds,
    checks_passed, checks_failed}}}]}, oldest bucket first. `buckets` is empty for 'all'
    when the ledger holds nothing for these accounts - there is no first day to start from.
    A bucket's `start` may fall before the window (a partial first week); only days inside
    the window are counted in it."""
    first_s, last_s = window_bounds(window, today)
    if not account_ids:
        return {'unit': _trend_unit(window, today, today), 'buckets': []}
    q = (db.session.query(AccountStatDay.account_id, AccountStatDay.day,
                          *[getattr(AccountStatDay, c) for c in _TREND_COLUMNS])
         .filter(AccountStatDay.account_id.in_(account_ids), AccountStatDay.day <= last_s))
    if first_s is not None:
        q = q.filter(AccountStatDay.day >= first_s)
    rows = q.all()

    if first_s is not None:
        first = date.fromisoformat(first_s)
    elif rows:
        first = date.fromisoformat(min(r.day for r in rows))
    else:
        return {'unit': 'week', 'buckets': []}

    unit = _trend_unit(window, first, today)
    starts = _bucket_starts(unit, first, today)
    buckets = []
    for i, start in enumerate(starts):
        end = (starts[i + 1] - timedelta(days=1)) if i + 1 < len(starts) else today
        buckets.append({'start': start, 'end': end,
                        'values': {aid: {c: 0 for c in _TREND_COLUMNS} for aid in account_ids}})
    starts_iso = [s.isoformat() for s in starts]
    for account_id, day, capture, passed, failed in rows:
        # The last bucket starting on or before the day. The first bucket starts on or before
        # the window's first day, so the index is never negative.
        values = buckets[bisect_right(starts_iso, day) - 1]['values'][account_id]
        values['capture_seconds'] += capture or 0
        values['checks_passed'] += passed or 0
        values['checks_failed'] += failed or 0
    return {'unit': unit, 'buckets': buckets}


# ---------------------------------------------------------------------------
# Current-state readers
# ---------------------------------------------------------------------------

def guide_counts(account_ids=None):
    """{account_id: channels of that account whose listings reach the guide}, in one query.

    Guide SCOPE, not `Channel.in_guide` - the number is rendered as a link into the channel
    search's `f.other=guide` filter, so reading the raw flag here made the count and the page
    it opens disagree by more than 3x (`dev/changelog/734`). The definition, and why the flag
    is not it, live on `channel_groups.guide_scope_channel_ids()`.
    """
    q = (db.session.query(Channel.account_id, func.count(Channel.id))
         .filter(Channel.id.in_(guide_scope_channel_ids())))
    if account_ids is not None:
        # `+ 0` keeps SQLite off the account_id index: with it, the planner walks every
        # channel of every account (23 ms on 139k channels) instead of looking up the few
        # hundred guide-scope ids by primary key (0.3 ms), dev/changelog/1029.
        q = q.filter((Channel.account_id + 0).in_(account_ids))
    return dict(q.group_by(Channel.account_id).all())


def epg_match_counts(account_ids):
    """{account_id: {'with_epg', 'no_match', 'no_id'}} - each account's channels split by
    whether they have confirmed EPG data: a program showing in the next 24h ('with_epg'),
    an epg_channel_id set but currently matching nothing ('no_match'), or no epg_channel_id
    at all ('no_id'). One grouped query for every account.

    The correlated EXISTS is backed by ix_epg_entries_channel_stop (migration 17)."""
    out = {aid: {'with_epg': 0, 'no_match': 0, 'no_id': 0} for aid in account_ids}
    if not account_ids:
        return out
    now = datetime.utcnow()
    window_end = now + timedelta(hours=24)
    has_id = db.and_(Channel.epg_channel_id.isnot(None), Channel.epg_channel_id != '')
    has_epg = (db.session.query(EPGEntry.id)
               .filter(EPGEntry.channel_id == Channel.id,
                       EPGEntry.stop_time >= now, EPGEntry.start_time <= window_end)
               .exists())
    rows = (db.session.query(Channel.account_id, func.count(Channel.id),
                             func.sum(case((has_id, 0), else_=1)),
                             func.sum(case((db.and_(has_id, has_epg), 1), else_=0)))
            .filter(Channel.account_id.in_(account_ids))
            .group_by(Channel.account_id))
    for account_id, total, no_id, with_epg in rows:
        no_id = no_id or 0
        with_epg = with_epg or 0
        out[account_id] = {'with_epg': with_epg, 'no_match': total - no_id - with_epg,
                           'no_id': no_id}
    return out


def current_stats(account_ids, cfg):
    """{account_id: current-state numbers} for every id asked for, in a fixed number of
    grouped queries - never one per account.

    Keys: channels_offered, in_group (distinct channels in any group),
    recording_memberships (group memberships with Recording on - one channel in two groups
    counts twice), guide_channels, guide_rows_fed / guide_rows_total, epg (see
    `epg_match_counts`), bands ({band key: count} over tested, not-hidden channels), tested,
    avg_score (mean lifetime health_score of those, or None), failing.

    `cfg` is a parameter so nothing here reads config; band edges and the failing rules are
    resolved from it once."""
    out = {aid: {'channels_offered': 0, 'in_group': 0, 'recording_memberships': 0,
                 'guide_channels': 0, 'guide_rows_fed': 0, 'guide_rows_total': 0,
                 'bands': {key: 0 for key in health_bands.BAND_KEYS}, 'tested': 0,
                 'avg_score': None, 'failing': 0}
           for aid in account_ids}
    if not account_ids:
        return out

    for aid, count, hidden in (db.session.query(Account.id, Account.channel_count,
                                                Account.hidden_channel_count)
                               .filter(Account.id.in_(account_ids))):
        out[aid]['channels_offered'] = max(0, (count or 0) - (hidden or 0))

    # The membership counts are driven FROM the membership table, with each member's
    # account looked up by primary key. Written as a join, SQLite starts from channels (the
    # account_id index) and probes memberships for every channel of every account - 24 ms
    # on 139k channels for 316 memberships, where this is 0.4 ms (dev/changelog/1029).
    member_account = (db.session.query(Channel.account_id)
                      .filter(Channel.id == ChannelGroupMember.channel_id)
                      .correlate(ChannelGroupMember).scalar_subquery())
    for aid, in_group, recording in (
            db.session.query(member_account,
                             func.count(func.distinct(ChannelGroupMember.channel_id)),
                             func.sum(case((ChannelGroupMember.recording_enabled.is_(True), 1),
                                           else_=0)))
            .group_by(member_account)
            .having(member_account.in_(account_ids))):
        out[aid]['in_group'] = in_group
        out[aid]['recording_memberships'] = recording or 0

    for aid, count in guide_counts(account_ids).items():
        out[aid]['guide_channels'] = count

    # Guide rows: every in-guide group plus every channel that is its own row. An account
    # "can feed" a group row when it has a recording-enabled member in it.
    guide_groups = db.session.query(func.count(ChannelGroup.id)).filter(
        ChannelGroup.in_guide.is_(True)).scalar() or 0
    own_rows = dict(db.session.query(Channel.account_id, func.count(Channel.id))
                    .filter(Channel.in_guide.is_(True))
                    .group_by(Channel.account_id).all())
    rows_total = guide_groups + sum(own_rows.values())
    fed_groups = dict(
        db.session.query(member_account, func.count(func.distinct(ChannelGroupMember.group_id)))
        .select_from(ChannelGroupMember)
        .join(ChannelGroup, ChannelGroup.id == ChannelGroupMember.group_id)
        .filter(ChannelGroup.in_guide.is_(True), ChannelGroupMember.recording_enabled.is_(True))
        .group_by(member_account)
        .having(member_account.in_(account_ids)).all())
    for aid in account_ids:
        out[aid]['guide_rows_total'] = rows_total
        out[aid]['guide_rows_fed'] = fed_groups.get(aid, 0) + own_rows.get(aid, 0)

    for aid, counts in epg_match_counts(account_ids).items():
        out[aid]['epg'] = counts

    # Failing, as health_score.channel_failing_reason's two channel-state rules: on a
    # losing streak, or an effective score below the failing band's ceiling.
    threshold = health_bands.failing_threshold(cfg)
    streak = cfg.get('channel_testing', {}).get('failing_streak_threshold',
                                                DEFAULT_FAILING_STREAK_THRESHOLD)
    failing_terms = []
    if threshold is not None:
        failing_terms.append(effective_health() < threshold)
    if streak > 0:
        failing_terms.append(Channel.consecutive_test_failures >= streak)
    failing = (func.sum(case((db.or_(*failing_terms), 1), else_=0)) if failing_terms
               else db.literal(0))
    band = health_band_expr(cfg)
    sums = defaultdict(float)
    # INDEXED BY: only tested channels have a score, and ix_channels_health reaches just
    # those. Left to itself SQLite picks an index that satisfies the GROUP BY and reads every
    # channel instead - 24 ms vs 0.3 ms on 139k channels with 291 tested (dev/changelog/1029).
    # The index is declared on the model and by migration 24, so it is always there.
    for aid, band_key, count, score_sum, failing_count in (
            db.session.query(Channel.account_id, band, func.count(Channel.id),
                             func.sum(Channel.health_score), failing)
            .with_hint(Channel, 'INDEXED BY ix_channels_health', 'sqlite')
            .filter(Channel.account_id.in_(account_ids), Channel.health_score.isnot(None),
                    Channel.hidden.is_(False))
            .group_by(Channel.account_id, band)):
        stats = out[aid]
        stats['bands'][band_key] = count
        stats['tested'] += count
        stats['failing'] += failing_count or 0
        sums[aid] += score_sum or 0
    for aid, stats in out.items():
        if stats['tested']:
            stats['avg_score'] = sums[aid] / stats['tested']
    return out
