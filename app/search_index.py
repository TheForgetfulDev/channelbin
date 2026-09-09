"""FTS5 trigram search indexes: their schema, their rebuild, and whether they can be trusted.

The one home for "build/rebuild the search indexes" - the migration that first creates them,
the fresh-database path in create_app(), and the sync close-out that keeps them current all
call in here rather than carrying their own copy of the SQL.

Why trigram FTS5 and not a normal tokenizer: today's search is `LIKE '%q%'`, and the trigram
tokenizer is an exact drop-in for it rather than an approximation - it preserves mid-word
substring matching, which matters because channel names look like `US| ESPN2 HD`. A
word-based tokenizer would silently stop matching those. Measured on the real database:
channel search 89ms -> 4-10ms, EPG deep search 1.3-1.6s -> 15-250ms (dev/changelog/364).

Two things about these indexes that are easy to get wrong:

**They are rebuilt wholesale, never maintained with triggers.** EPG sync mass-deletes and
bulk re-inserts 270k-410k rows about six times a day. Trigger-maintained FTS turns that
1.16M-row rewrite from 31.2s into 314.8s on insert and 6.1s into 89.1s on delete - a 10x
write penalty holding SQLite's single write lock the whole time. A wholesale rebuild after
the sync costs ~40s instead. Do not add triggers here later "for freshness".

**The programs rebuild is chunked, and that is a correctness constraint, not a tuning
choice.** SQLite has one write lock and a 10s busy_timeout; populating chan_prog_fts in a
single statement holds that lock for 39.5s, stalling every recorder and watchdog write.
Chunking releases it between batches, which in turn means the index spends the rebuild
half-populated - so `STATUS_BUILDING` exists to stop readers trusting it in that window.
The three pieces (FTS_CHUNK_ROWS, the unit structure of REBUILD_SQL, STATUS_BUILDING) only
make sense together; do not remove one of them.

**Nothing here is allowed to be the reason a search returns wrong results.** The index is
an optimization; `LIKE` is the fallback, and every path that cannot use the index takes it
and logs that it did. Readiness is therefore checked against the *source data*, not against
whether the last rebuild reported success - see `_SOURCE_WATERMARK_SQL`.

**`db.create_all()` cannot create them.** They are raw SQLite virtual tables with no ORM
model, so nothing in SQLAlchemy knows about them, and `run_migrations(fresh_db=True)` stamps
the version and returns without running a step. That leaves exactly two paths that must both
create them: the migration (for an existing database) and `ensure_search_index_schema()`
called unconditionally from `create_app()` (for a brand-new one, and for the test suite's
schema template). Only `SearchIndexState` - the "is this index safe to query" row - is an
ORM model, so that table alone comes from create_all().
"""
import hashlib
import logging
import threading
import time
from datetime import datetime

from sqlalchemy import Integer, column, func, or_, select, table, text

from . import admission, db
from .database import Channel, EPGEntry, SearchIndexState
from .db_utils import current_wal_size_bytes, retry_on_locked
from .fmt_utils import fmt_bytes

log = logging.getLogger(__name__)


SEARCH_INDEX_CHANNELS = 'channels'
SEARCH_INDEX_PROGRAMS = 'programs'
SEARCH_INDEX_NAMES = (SEARCH_INDEX_CHANNELS, SEARCH_INDEX_PROGRAMS)

STATUS_OK = 'OK'
STATUS_FAILED = 'FAILED'
# Recorded as the `error` of an index that was left BUILDING by a process that no longer
# exists. A constant rather than a literal because both reconcile_interrupted_builds() and its
# regression tests have to mean the same string.
BUILD_INTERRUPTED_ERROR = ('the rebuild was interrupted - the app stopped while this index was '
                           'still being built, so it never finished')

# Set for the duration of a rebuild, cleared to OK or FAILED when it ends. Required because
# the programs rebuild is chunked across many transactions (see REBUILD_SQL): mid-rebuild the
# index is genuinely incomplete, and the watermark cannot be relied on to notice - a rebuild
# triggered when the source has NOT moved (a migration, an EPG cleanup that deleted nothing,
# a manual rebuild) leaves the recorded watermark matching, so readiness would report OK and
# serve partial results. DESIGN-search-indexes.md section 6.
STATUS_BUILDING = 'BUILDING'

# Rows per transaction when populating chan_prog_fts. The whole population is ~33s of work
# against production-scale EPG (344k deduped rows) and SQLite has one write lock, so doing it
# in a single statement holds that lock for 39.5s against a 10s busy_timeout - measured, and
# it stalls every recorder and watchdog write for the duration. Measured alternatives, local
# disk, WAL + synchronous=NORMAL + 64MB cache:
#
#     one-shot 'rebuild'   1 txn     39.5s total   39.5s held  <- what shipped before
#     chunked, 25,000     14 txns    35.1s total    6.16s held
#     chunked, 10,000     35 txns    32.8s total    1.62s held  <- this
#     chunked,  5,000     69 txns    36.0s total    0.97s held
#
# 10,000 is both the fastest overall and ~6x under the timeout. DESIGN-search-indexes.md
# section 5 recommends 25,000 for the (larger, unbuilt) epg_fts index; on this table that
# measures 6.16s, thinner headroom for no gain.
FTS_CHUNK_ROWS = 10_000

# Minimum query length the trigram tokenizer can answer at all. Below three characters an
# FTS MATCH returns *zero rows* rather than erroring, so every caller must fall back to LIKE
# instead of trusting the empty result.
TRIGRAM_MIN_CHARS = 3


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------

# One source of truth for the DDL, executed both through the ORM (create_app) and through a
# raw sqlite3 cursor (the migration). Plain strings with no bind parameters so the same text
# works on either - sqlite3 and SQLAlchemy spell placeholders differently.
#
# ch_fts indexes channels.stream_url, which carries the provider credentials in its path.
# That is not new exposure - the column itself already stores them in plaintext in this same
# file, and the pre-migration snapshot already lands in a 0700 directory for that reason -
# but it does mean the FTS shadow tables inherit the same handling as the rest of dvr.db.
SEARCH_INDEX_DDL = (
    """
    CREATE VIRTUAL TABLE IF NOT EXISTS ch_fts USING fts5(
        name, stream_url, epg_channel_id, category_name,
        content='channels', content_rowid='id', tokenize='trigram'
    )
    """,
    # chan_prog is a derived cache, not a source of truth: DISTINCT (channel_id, title,
    # sub_title, description) over *future* EPG entries. The point is size - 1M EPG rows
    # collapse to ~344k distinct rows, and "which channels air something matching q" needs
    # nothing else, so the index answers that 12x faster off this table with an identical
    # result set.
    #
    # description is here because it is where this app's searches actually match: an EPG
    # title is often just "Live Sport", with the teams named only in the description
    # (DESIGN-search-indexes.md section 3). Including it costs +15% rows, ~315MB of disk and
    # ~33s of the daily rebuild, and keeps queries at 17-86ms - the same league as title-only
    # search. The alternative that was costed and rejected, epg_fts over raw epg_entries, is
    # +517MB and ~132s for a different question (per-airing, not per-channel); section 4.
    """
    CREATE TABLE IF NOT EXISTS chan_prog (
        id          INTEGER PRIMARY KEY,
        channel_id  INTEGER NOT NULL,
        title       TEXT,
        sub_title   TEXT,
        description TEXT
    )
    """,
    'CREATE INDEX IF NOT EXISTS ix_chan_prog_channel_id ON chan_prog (channel_id)',
    """
    CREATE VIRTUAL TABLE IF NOT EXISTS chan_prog_fts USING fts5(
        title, sub_title, description,
        content='chan_prog', content_rowid='id', tokenize='trigram'
    )
    """,
)

# The rebuild statements, likewise shared between the ORM and raw-cursor callers.
#
# **Each entry is a tuple of UNITS, and each unit is a tuple of statements that run inside
# one transaction.** The caller commits between units and may retry a unit in full, so every
# unit has to be independently re-runnable - which is why the temp table is dropped and
# recreated inside the unit that uses it rather than shared across units. Do not flatten this
# back into one statement list: the whole point is that the write lock is released between
# units. `rebuild_units()` is what drives it, and it appends the chunked units this table
# cannot express statically.
#
# The DISTINCT scan over ~1M EPG rows lands in a TEMP table first, and only then is copied
# into chan_prog. That ordering is the entire reason it is written this way: SQLite takes the
# write lock at the first statement that writes the *main* database, so doing the scan into
# temp keeps it out of the locked window - the unit runs ~28s but holds the lock for ~1.9s of
# it (measured: DELETE 0.66s + INSERT 1.20s at 344k rows).
#
# `datetime('now')` rather than a bound cutoff so this stays parameter-free. It returns UTC
# to the second, and stop_time is stored as naive-UTC text with microseconds, so the string
# comparison is correct (a same-second row sorts after the cutoff and is kept).
REBUILD_SQL = {
    # One statement, and no equivalent trick is available: this holds the write lock for its
    # whole duration, measured 5.7s warm and 9.8s on a cold page cache against 136k channels.
    # That is inside busy_timeout, and retry_on_locked covers the cold tail. It is not
    # chunked like the programs index because it does not need to be.
    SEARCH_INDEX_CHANNELS: (
        ("INSERT INTO ch_fts(ch_fts) VALUES('rebuild')",),
    ),
    SEARCH_INDEX_PROGRAMS: (
        # Clearing is its own unit because it is the one part that cannot be chunked - but
        # unlike epg_fts, where clearing 517MB costs ~30s and is the whole problem
        # (DESIGN-search-indexes.md section 5), delete-all on this 260MB index measures
        # 2.71s, comfortably inside busy_timeout. Measured 2026-07-30, so section 5's
        # chunked-clear machinery is genuinely not needed here.
        ("INSERT INTO chan_prog_fts(chan_prog_fts) VALUES('delete-all')",),
        (
            # IF EXISTS, not a bare DROP: a retry re-enters here with the temp table
            # possibly still present on this connection.
            'DROP TABLE IF EXISTS temp.chan_prog_stage',
            'CREATE TEMP TABLE chan_prog_stage AS '
            'SELECT DISTINCT channel_id, title, sub_title, description FROM epg_entries '
            "WHERE stop_time >= datetime('now')",
            'DELETE FROM chan_prog',
            'INSERT INTO chan_prog (channel_id, title, sub_title, description) '
            'SELECT channel_id, title, sub_title, description FROM chan_prog_stage',
            # Dropped inside this unit, not after the chunks: the temp table lives on
            # whichever connection ran it, and the ORM driver releases its connection back to
            # the pool at every commit. Nothing after this unit may assume it still exists.
            'DROP TABLE IF EXISTS temp.chan_prog_stage',
        ),
    ),
}

def rebuild_units(name: str, scalar):
    """Yield the rebuild for `name` as units of SQL, one unit per transaction.

    **The caller must commit each unit before asking for the next.** This is a generator on
    purpose: the chunk bounds below are read from chan_prog *after* the static units have
    repopulated it, and that read only returns the right answer if the caller has already
    committed them. `scalar(sql)` runs a SELECT and returns its first column - passed in
    rather than assumed, because the two callers drive this through different plumbing (the
    ORM session, and a raw sqlite3 cursor in a migration).

    Chunk bounds are interpolated as literal integers rather than bound parameters, for the
    same reason every other statement here is parameter-free: sqlite3 and SQLAlchemy spell
    placeholders differently, and these strings have to work on both. They are ints this
    function computed, never user input.

    The chunks walk id ranges rather than LIMIT/OFFSET so each one is an index seek on the
    primary key instead of a re-scan of everything before it. chan_prog was emptied and
    repopulated by the unit above, so its ids start at 1 and are contiguous.
    """
    for unit in REBUILD_SQL[name]:
        yield unit
    if name != SEARCH_INDEX_PROGRAMS:
        return
    max_id = scalar('SELECT MAX(id) FROM chan_prog') or 0
    for lo in range(0, max_id, FTS_CHUNK_ROWS):
        yield (
            'INSERT INTO chan_prog_fts(rowid, title, sub_title, description) '
            'SELECT id, title, sub_title, description FROM chan_prog '
            f'WHERE id > {lo} AND id <= {lo + FTS_CHUNK_ROWS}',
        )


_ROW_COUNT_SQL = {
    SEARCH_INDEX_CHANNELS: 'SELECT COUNT(*) FROM channels',
    SEARCH_INDEX_PROGRAMS: 'SELECT COUNT(*) FROM chan_prog',
}

# "What did the source data look like when this index was built?" - recorded on the state
# row at rebuild time and re-read on every readiness check. If the two differ, the source
# has moved since the rebuild and the index is stale.
#
# This is what makes correctness independent of the rebuild ever running. A rebuild that is
# skipped (sync failed after the channel upserts already committed - they commit early on
# purpose, so the write lock is not held across the EPG fetch), killed mid-flight, or undone
# by a restore from backup all land here as "stale" and send search back to LIKE, instead of
# quietly matching against the previous sync's text. That last case is the nasty one: a
# renamed channel keeps matching its OLD name until the index is rebuilt.
#
# Both are O(log n) index lookups, not scans - measured 0.02-0.10ms on the production
# database (136k channels), which is what makes it affordable once per request.
#   channels: MAX(id) and MAX(search_text_updated_at), together. Neither is sufficient alone -
#             the stamp catches a rename (the id does not change) but is only moved by code
#             that remembers to stamp it, and MAX(id) catches any insertion whether or not it
#             was stamped. Covered by the PK and ix_channels_search_text_updated_at.
#
#             The stamp is deliberately NOT last_seen_at (what this read until
#             dev/changelog/674) and deliberately NOT updated_at. last_seen_at is stamped on
#             every matched row of every sync whether the provider changed anything or not, so
#             it declared the index stale after every no-change sync and forced a full rebuild;
#             worse, the degraded window it opened ran from the channel upserts committing
#             (early, on purpose, to release the write lock before the EPG fetch) all the way
#             to the rebuild in the sync's finally - the whole EPG import phase, measured at
#             2.5 minutes on this machine's 21:50 sync, roughly 24 times a day. updated_at is
#             honest about "did this row change" but answers a different question than this
#             index asks: it moves for a health score, an in_guide toggle, a default-profile
#             change, none of which alter a token in ch_fts. Since nothing repairs staleness
#             except a rebuild, and rebuilds only fire from a sync, keying off updated_at
#             would have turned a 3am channel test into hours of degraded search. It is also
#             unindexed and a full scan - 87ms against 138k channels, versus 0.014ms for an
#             indexed max, on a query that runs once per request.
#   programs: MAX(id) - epg_entries has no written-at timestamp and EPG rows are immutable
#             once written, so a rising PK max is both available and sufficient.
#
# COUNT(*) is deliberately not in here: it is a full scan (measured 8ms vs 0.02ms for a max)
# and it would only add deletion detection, which is not needed. A deleted channel is already
# safe - _fts_rowids returns bare rowids, and the caller's IN drops any whose row is gone.
# Deletion can only ever cost a match, never invent one.
#
# **One MAX() per statement, and it has to be.** SQLite's index-max shortcut applies only to a
# lone `SELECT MAX(col)`; the moment two aggregates share a statement the planner falls back to
# scanning. Measured on production (against the previous stamp column, but the planner behavior
# is about the two aggregates sharing a statement, not about which column): the two maxes as
# separate statements are 0.02ms each and plan as SEARCH, while `SELECT MAX(id) || MAX(stamp)`
# plans as SCAN and costs 17-30ms - roughly 1000x, on a query that runs once per request. Do
# not "tidy" these into one SELECT.
_SOURCE_WATERMARK_SQL = {
    SEARCH_INDEX_CHANNELS: ('SELECT MAX(id) FROM channels',
                            'SELECT MAX(search_text_updated_at) FROM channels'),
    SEARCH_INDEX_PROGRAMS: ('SELECT MAX(id) FROM epg_entries',),
}


def source_watermark(name: str) -> str:
    """The current watermark for an index's source table, as a string.

    Stringified rather than typed because the same value is written by the migration
    through a raw sqlite3 cursor and by the rebuild through the ORM - comparing text keeps
    those two paths from disagreeing over datetime-vs-str. An empty source reads as '',
    which is a perfectly good watermark: it only ever has to match itself.
    """
    parts = [db.session.execute(text(sql)).scalar() for sql in _SOURCE_WATERMARK_SQL[name]]
    return '/'.join('' if p is None else str(p) for p in parts)


def ensure_search_index_schema():
    """Create the FTS tables if they are missing. Idempotent; safe on every startup.

    Called unconditionally from create_app() after run_migrations(), which is what covers
    the brand-new-database path that neither create_all() nor the migration runner reaches.
    On an already-migrated database every statement is a no-op.

    Startup is single-threaded with no concurrent writer, so this commit deliberately skips
    retry_on_locked - the same exemption app/migrations.py and _seed_default_tags() take.
    """
    for stmt in SEARCH_INDEX_DDL:
        db.session.execute(text(stmt))
    db.session.commit()


def reconcile_interrupted_builds():
    """Fail any index still marked BUILDING at startup. Called once, from create_app().

    STATUS_BUILDING is only ever cleared by the rebuild that set it, so a process that dies
    mid-rebuild - ./restart.sh, kill -9, an OOM kill - leaves it set with nobody left to
    clear it. search_index_readiness() then reports that index unusable *forever*, every
    search silently takes the unindexed scan over 1.9M rows, and the only thing that repairs
    it is a later account sync happening to rebuild. Restarting the app for CPU relief during
    the 2026-08-01 search stampede would have caused exactly that (dev/changelog/425).

    Two races this deliberately does not guard against, because neither needs it:

    * **A rebuild in this process.** There cannot be one. This runs from create_app() before
      init_scheduler() and before a single blueprint is registered, so nothing has yet had
      the chance to start a rebuild.
    * **A rebuild in another process** (a tools/ script, a second app build). The worst case
      is a spurious FAILED, which sends search to LIKE - correct but slow - until that
      rebuild's own _record_state(STATUS_OK) overwrites it and auto-dismisses the alert with
      it. The only failure direction is the safe one, which is why no cross-process lock is
      warranted here.
    """
    stranded = [(s.name, s.rebuilt_at)
                for s in SearchIndexState.query.filter_by(status=STATUS_BUILDING).all()]
    for name, since in stranded:
        log.warning('Search index %r was left BUILDING at %s by a process that is gone - '
                    'marking it FAILED so the degradation is visible instead of permanent',
                    name, since)
        # Empty watermark, exactly as the rebuild-failure path writes: whatever the
        # interrupted rebuild recorded describes rows this index does not actually contain,
        # and leaving it in place would let a later readiness check read a half-built index
        # as fresh the moment something set the status back to OK.
        _record_state(name, STATUS_FAILED, None, None, BUILD_INTERRUPTED_ERROR, '')
        _alert_rebuild_failed(name, BUILD_INTERRUPTED_ERROR, active=True)


# ---------------------------------------------------------------------------
# Rebuild
# ---------------------------------------------------------------------------

# Serializes every rebuild in this process. Two concurrent rebuilds of `programs` do not
# merely duplicate work, they corrupt: REBUILD_SQL empties chan_prog and stages into
# temp.chan_prog_stage, and rebuild_units() reads its chunk bounds from MAX(id) FROM chan_prog
# between commits - so an interleaved second run moves the bounds out from under the first and
# leaves a half-populated index that _record_state(STATUS_OK) then declares fresh. There has
# been exactly one caller (the sync close-out) until now; a manual rebuild trigger makes a
# second caller real (dev/changelog/426).
#
# Per-process only, deliberately. A rebuild in *another* process is already handled the safe
# way by STATUS_BUILDING plus the watermark - the worst case is search falling back to LIKE,
# correct but slow - which is the same argument reconcile_interrupted_builds() makes for not
# taking a cross-process lock. Do not add one here.
_rebuild_lock = threading.Lock()
_rebuilding = False


def rebuild_in_progress() -> bool:
    """Whether a rebuild is running in this process. The cheap cross-module read, for callers
    deciding whether to offer or refuse a manual rebuild; mirrors channel_tester.is_running()."""
    return _rebuilding


def rebuilding_index_names() -> list:
    """Names of indexes with a SearchIndexState row currently STATUS_BUILDING.

    Unlike rebuild_in_progress(), this is a DB read, not an in-memory flag - it answers
    correctly from any process, including a second app build or a read-only connection from
    tools/check_busy.py (which cannot import this function, since it runs with no Flask app;
    it queries the same status column directly and imports only the STATUS_BUILDING string
    from here, so the two callers cannot drift on what "rebuilding" means). Same query
    reconcile_interrupted_builds() already runs at startup, factored out so the dashboard's
    background-task indicator (app/routes/dashboard.py) and this module's own callers share it.
    """
    return [s.name for s in SearchIndexState.query.filter_by(status=STATUS_BUILDING).all()]


def rebuild_search_indexes(reason: str, names=SEARCH_INDEX_NAMES, refusable: bool = False) -> dict:
    """Rebuild the named indexes and record the outcome. Returns {name: bool succeeded}.

    Never raises: a rebuild failure must not turn an otherwise-successful sync into an
    ERROR, because the sync's own work is already committed and correct. Instead the index
    is marked FAILED, an ERROR alert names the failure, and search falls back to LIKE - slow
    but right - until a later rebuild succeeds. Silently serving an empty index is the one
    outcome that is not allowed.

    `reason` is what the log line says triggered this ("account 3 sync", "EPG cleanup") so a
    rebuild that starts failing can be traced to the path that asked for it.

    Blocks on _rebuild_lock rather than skipping, so a sync close-out that arrives while a
    manual rebuild is running still rebuilds instead of leaving its own writes unindexed.
    Both callers are already on background threads, so waiting out the ~76s is free; only a
    request thread must never call this directly.

    Registers on the database-contention axis (app/admission.py) for the whole rebuild, so
    the actors that yield to a rebuild can see it. `refusable=False` - the default, and what
    every caller today passes - registers without asking, because each of them is either a
    present user's manual rebuild or the tail of work that was already admitted (the sync
    close-out, EPG cleanup). A caller that is genuinely optional and should stand down while
    a sync is running passes `refusable=True` and gets `{}` back when refused; the search
    index janitor (run_index_janitor) is the one such caller. A refused rebuild rebuilds
    nothing and says so - it must never look like a completed one, since STATUS_OK on an
    unbuilt index is the single outcome this module does not allow.
    """
    global _rebuilding
    ticket = admission.try_start(admission.KIND_REBUILD, reason, force=not refusable)
    if not ticket.granted:
        log.info('Search index rebuild (%s) declined: %s', reason, ticket.reason)
        return {}
    try:
        with _rebuild_lock:
            _rebuilding = True
            try:
                results = {}
                for name in names:
                    results[name] = _rebuild_one(name, reason)
                return results
            finally:
                _rebuilding = False
    finally:
        admission.release(ticket)


def _rebuild_one(name: str, reason: str) -> bool:
    @retry_on_locked()
    def _run_unit_and_commit(statements):
        for stmt in statements:
            db.session.execute(text(stmt))
        db.session.commit()

    def _scalar(sql):
        return db.session.execute(text(sql)).scalar()

    started = time.monotonic()
    try:
        # Read BEFORE the rebuild, never after. If the source moves while the rebuild is
        # running, a watermark read afterwards would describe data this index does not
        # actually contain and would read as fresh forever. Reading first can only err the
        # safe way - the index looks stale one rebuild longer than it strictly had to.
        watermark = source_watermark(name)
        # BUILDING before the first unit, not after it. The very first unit empties the FTS
        # index while its content table still holds the old rows, so from that commit until
        # the last chunk lands, any reader that trusted this index would get zero matches
        # with no error. That window is the reason this status exists; a rebuild that dies
        # mid-way leaves it set, which reads as "not usable" and sends search to LIKE.
        _record_state(name, STATUS_BUILDING, None, None, None, watermark)
        for statements in rebuild_units(name, _scalar):
            _run_unit_and_commit(statements)
        row_count = _scalar(_ROW_COUNT_SQL[name])
    except Exception as exc:
        duration_ms = int((time.monotonic() - started) * 1000)
        db.session.rollback()
        log.error('Search index %r rebuild failed after %dms (%s): %s',
                  name, duration_ms, reason, exc)
        _record_state(name, STATUS_FAILED, duration_ms, None, str(exc), '')
        _alert_rebuild_failed(name, str(exc), active=True)
        return False

    duration_ms = int((time.monotonic() - started) * 1000)
    # The WAL size rides along on this line because a rebuild is one of the two operations
    # that can plausibly grow dvr.db-wal by gigabytes - it writes hundreds of thousands of
    # rows across ~40 transactions, and any reader holding a snapshot across that window
    # stops every checkpoint from rewinding the file. The daily maintenance reading says
    # which DAY the WAL grew; this says whether it was this. See dev/changelog/424.
    log.info('Search index %r rebuilt in %dms (%d rows, %s) - WAL now %s',
             name, duration_ms, row_count or 0, reason,
             fmt_bytes(current_wal_size_bytes()))
    _record_state(name, STATUS_OK, duration_ms, row_count, None, watermark)
    _alert_rebuild_failed(name, '', active=False)
    return True


def rebuild_through_cursor(name: str, conn, cur) -> tuple:
    """Run a rebuild on a raw sqlite3 cursor. Returns (duration_ms, row_count, watermark).

    For migrations only, which is why it neither records state nor alerts: it runs at
    startup, before the app serves anything, with no other writer to lock out and no ORM
    session available (opening one against an in-flight migration transaction deadlocks).
    The caller writes the state row itself, because which columns exist depends on which
    migration is running.

    Three shipped migration steps call this, so editing the rebuild SQL or SEARCH_INDEX_DDL
    also edits what those steps produce - deliberate, since an index is defined as whatever
    this module currently builds, but it has to stay runnable against the older-shaped
    chan_prog those steps meet. migrations.py's module docstring carries the contract and
    _ensure_search_index_shape the mitigation (dev/changelog/688).

    Commits between units, same as the ORM driver - the chunk bounds are only correct once
    the units before them are committed.
    """
    started = time.monotonic()
    # Read before the rebuild, same ordering rule as _rebuild_one - a watermark read
    # afterwards can describe rows the index does not contain.
    parts = [cur.execute(sql).fetchone()[0] for sql in _SOURCE_WATERMARK_SQL[name]]
    watermark = '/'.join('' if p is None else str(p) for p in parts)
    for statements in rebuild_units(name, lambda sql: cur.execute(sql).fetchone()[0]):
        for stmt in statements:
            cur.execute(stmt)
        conn.commit()
    duration_ms = int((time.monotonic() - started) * 1000)
    row_count = cur.execute(_ROW_COUNT_SQL[name]).fetchone()[0]
    return duration_ms, row_count, watermark


def _record_state(name, status, duration_ms, row_count, error, watermark):
    """Upsert the index's SearchIndexState row. Best-effort: if this write is what failed,
    the index is still whatever the rebuild left it, and the log line above already said so -
    losing the bookkeeping row must not escalate into an exception the caller has to handle."""
    @retry_on_locked()
    def _upsert_and_commit():
        state = SearchIndexState.query.filter_by(name=name).first()
        if state is None:
            state = SearchIndexState(name=name)
            db.session.add(state)
        state.status = status
        state.rebuilt_at = datetime.utcnow()
        state.duration_ms = duration_ms
        state.row_count = row_count
        state.error = error
        state.source_watermark = watermark
        db.session.commit()

    try:
        _upsert_and_commit()
    except Exception:
        db.session.rollback()
        log.exception('Could not record search index state for %r', name)


def _alert_rebuild_failed(name: str, detail: str, active: bool):
    """Raise, refresh, or auto-dismiss the standing rebuild-failure alert for this index.

    Imported locally: app/accounts.py calls rebuild_search_indexes() at its sync close-out,
    so a module-level import here would be circular.
    """
    from .accounts import _raise_or_resolve_standing_alert
    _raise_or_resolve_standing_alert(
        'SEARCH_INDEX_REBUILD_FAILED',
        source=f'search-index:{name}',
        active=active,
        title=f'Search index "{name}" could not be rebuilt' if active else '',
        body=(
            f'The {name} search index failed to rebuild: {detail}\n\n'
            'Search still returns correct results - it falls back to the slower unindexed '
            'scan while the index is unusable - so nothing is being hidden from you. It will '
            'be retried at the end of the next account sync, or you can retry it now from '
            'Maintenance -> Search index -> Rebuild now. If it keeps failing, the database may '
            'be out of disk space or the index tables may need to be dropped and recreated.'
            if active else ''
        ),
    )


# ---------------------------------------------------------------------------
# Readiness
# ---------------------------------------------------------------------------

def search_index_readiness(*names) -> tuple[bool, str]:
    """(is every named index safe to query, why not) - the gate every search caller uses.

    False means "use LIKE instead", not "return nothing". Four ways to get there, and the
    reason string names which, because it is what gets logged when search degrades:

    * **never built** - a fresh install has the tables but no state row. Correct out of the
      box, and stays correct until its first sync populates the index.
    * **being rebuilt** - a chunked rebuild is in flight and the index is half-populated.
      See STATUS_BUILDING; the watermark check cannot catch this case on its own.
    * **last rebuild failed** - already alerted by _alert_rebuild_failed at the time.
    * **stale** - the source moved since the rebuild. See _SOURCE_WATERMARK_SQL for why this
      check exists and why it cannot be replaced by trusting the recorded status.

    One state query plus one watermark lookup per name, all of them index hits. Safe to call
    once per request; hoist it out of any per-row loop like every other DB read, and pass the
    result to apply_channel_search() rather than letting each call site re-derive it.
    """
    names = names or SEARCH_INDEX_NAMES
    rows = {r.name: r for r in SearchIndexState.query.filter(SearchIndexState.name.in_(names))}
    for name in names:
        state = rows.get(name)
        if state is None:
            return False, f'the {name} search index has never been built'
        if state.status == STATUS_BUILDING:
            return False, f'the {name} search index is being rebuilt right now'
        if state.status != STATUS_OK:
            return False, f'the {name} search index last failed to rebuild'
        current = source_watermark(name)
        if (state.source_watermark or '') != current:
            return False, (f'the {name} search index is stale - its source has changed since '
                           f'it was built (watermark {state.source_watermark!r} -> {current!r})')
    return True, ''


def search_index_ready(*names) -> bool:
    """Boolean-only form of search_index_readiness(), for callers with nothing to log."""
    return search_index_readiness(*names)[0]


#: The only two index sets any search needs, per channel_search._index_names(): a field set
#: touching a program column needs both indexes, everything else needs channels alone.
READINESS_SETS = ((SEARCH_INDEX_CHANNELS,), SEARCH_INDEX_NAMES)


def readiness_map() -> dict:
    """{index-name tuple: (ready, reason)} for both sets a search can need, in one pass.

    Exists so readiness has exactly ONE speller. SearchContext.build() answers a search
    with it and /api/nav-status warns about a search not yet typed with it, and the reason
    strings those two show the user have to be the same sentence - a second copy of the
    wording is how the badge and the notice end up disagreeing about why search is slow.
    Four state+watermark lookups, all index hits (see search_index_readiness).
    """
    return {names: search_index_readiness(*names) for names in READINESS_SETS}


# ---------------------------------------------------------------------------
# Janitor
# ---------------------------------------------------------------------------

#: {index name: when THIS PROCESS first saw it unusable with nobody rebuilding it}.
#:
#: Per-process and deliberately not persisted. A restart is one of the events that strands
#: staleness in the first place, so a clock that starts fresh at boot is the desired
#: behavior, not a limitation: it gives the natural owner first crack, because APScheduler
#: fires an account's missed sync interval immediately at startup and that sync ends with
#: its own rebuild. The janitor only steps in when no such owner turned up.
_stale_since = {}


def run_index_janitor(grace_minutes: int) -> dict:
    """Rebuild any search index that has been unusable for `grace_minutes` with no owner.

    **The gap this closes.** `rebuild_search_indexes()` is otherwise reachable from three
    places only - a sync's `finally`, EPG cleanup, and the Maintenance button - so every
    path that separates staleness from a live sync thread leaves the repair to nobody. A
    hard restart mid-sync is the measured case: on 2026-08-15 account 2 committed its
    channel upserts, the process was restarted four minutes later, and both indexes stayed
    stale for 6.5 hours (worst case ~22, the sync interval) with every search on the LIKE
    fallback over 2.2M rows. This bounds that window at grace + the job's interval instead
    of at "whenever the next sync happens to arrive" (dev/changelog/680).

    Returns `{'due': [...], 'results': {name: bool}, 'refused': bool}`. `due` non-empty with
    an empty `results` means admission refused the rebuild - something heavier holds the
    database axis, which is a successful outcome for a job whose whole job is to yield.

    `grace_minutes <= 0` disables the janitor. The grace exists so the owner that *should*
    rebuild gets there first: a sync in flight already refuses this through admission, but a
    sync that is about to start, or one whose close-out rebuild is seconds away, does not.
    """
    if grace_minutes <= 0:
        return {'due': [], 'results': {}, 'refused': False}

    now = datetime.utcnow()
    # One query for both names, and the only reason this reads the status column directly:
    # readiness reports BUILDING as "not ready", but an index someone is actively rebuilding
    # is the one kind of unusable that already has an owner.
    building = set(rebuilding_index_names())

    due = []
    for name in SEARCH_INDEX_NAMES:
        if name in building:
            _stale_since.pop(name, None)
            continue
        ready, reason = search_index_readiness(name)
        if ready:
            _stale_since.pop(name, None)
            continue
        since = _stale_since.get(name)
        if since is None:
            _stale_since[name] = now
            log.info('Search index janitor: %r is unusable (%s) - rebuilding in %dm if '
                     'nothing else repairs it first', name, reason, grace_minutes)
            continue
        stale_minutes = int((now - since).total_seconds() // 60)
        if stale_minutes >= grace_minutes:
            due.append((name, reason, stale_minutes))

    if not due:
        return {'due': [], 'results': {}, 'refused': False}

    names = [name for name, _, _ in due]
    detail = '; '.join(f'{name} {mins}m ({reason})' for name, reason, mins in due)
    log.info('Search index janitor: rebuilding %s - unusable with no owner: %s',
             ', '.join(names), detail)
    # refusable=True is the whole point: this is the one caller that is genuinely optional
    # and must stand down for a sync or a manual rebuild rather than compete with it. The
    # decision is made inside try_start, under the admission lock, in the same breath as the
    # registration - never by reading active_kinds() here and then starting
    # (app/admission.py, dev/changelog/679).
    results = rebuild_search_indexes(f'index janitor ({detail})', names=tuple(names),
                                     refusable=True)

    # Restart the clock for the names that actually ran, which throttles an index that keeps
    # FAILING to rebuild down to one attempt per grace window rather than one per tick - a
    # programs rebuild is ~76s of CPU against a 10-minute tick. Deliberately keyed on what
    # ran, not on what was due: a refusal did nothing at all, the blocker may be gone a tick
    # later, and charging it a fresh grace window would extend the exact degraded window this
    # job exists to cap. Timed from after the rebuild, since the rebuild itself is minutes.
    finished = datetime.utcnow()
    for name in results:
        _stale_since[name] = finished
    return {'due': names, 'results': results, 'refused': not results}


# ---------------------------------------------------------------------------
# Query
# ---------------------------------------------------------------------------

def apply_channel_search(query, q: str, *, readiness=None, include_epg: bool = False,
                         include_epg_description: bool = False):
    """Apply the channel search predicate for `q` to a Channel query, indexed when possible.

    The one place that decides FTS-vs-LIKE, so every caller degrades the same way and says
    so. Returns the query unchanged for an empty `q`.

    `readiness` is the (ready, reason) tuple from search_index_readiness(), hoisted by the
    caller so several call sites in one request share a single evaluation. Omit it and this
    derives its own, which is correct but costs a query per call.

    `include_epg=True` widens the match to "or airs a program matching q", answered off the
    deduped chan_prog index. Off by default because the surfaces that shipped before it
    search channel names only - turning it on is a visible behavior change, not a tuning knob.

    `include_epg_description=True` widens that further to the program description. It is a
    separate flag rather than part of include_epg because they are separate scope fields in
    the UI, and either can be on without the other. It costs nothing extra to answer - the
    description lives in the same chan_prog row as the title (17-86ms measured against
    production-scale data, versus ~800ms for the unindexed fallback) - so it is a normal
    scope, not a "slow mode".

    Ordering is deliberately not touched. The FTS side is spelled as `Channel.id IN (SELECT
    rowid ...)` rather than a join precisely so that the caller's own ORDER BY and pagination
    survive untouched - FTS5 returns rows in its own order, and a silently reordered list is
    wrong data rather than cosmetics. The IN spelling also makes deletion safe for free: a
    rowid whose channel row is gone simply matches nothing.
    """
    if not q:
        return query

    # Resolved once and handed to whichever predicate runs, so the two spellings of the same
    # search cannot drift apart on which program columns they cover.
    epg_cols = _epg_columns(include_epg, include_epg_description)

    if len(q) < TRIGRAM_MIN_CHARS:
        # Expected, user-driven and high-frequency (every short prefix while typing), so
        # DEBUG rather than INFO - the other two fallbacks below are the abnormal ones.
        log.debug('Channel search %r using LIKE: %d characters, below the %d-character '
                  'trigram minimum', q, len(q), TRIGRAM_MIN_CHARS)
        return query.filter(_like_predicate(q, epg_cols))

    if readiness is None:
        readiness = search_index_readiness(*_index_names(epg_cols))
    ready, reason = readiness
    if not ready:
        log.info('Channel search %r using LIKE (slower, same results): %s', q, reason)
        return query.filter(_like_predicate(q, epg_cols))

    return query.filter(_fts_predicate(q, epg_cols))


# The chan_prog / chan_prog_fts columns each scope flag turns on, in index column order.
# One list, read by both predicates and by the FTS column filter.
_EPG_TITLE_COLUMNS = ('title', 'sub_title')
_EPG_DESCRIPTION_COLUMNS = ('description',)


def _epg_columns(include_epg: bool, include_epg_description: bool) -> tuple:
    return ((_EPG_TITLE_COLUMNS if include_epg else ())
            + (_EPG_DESCRIPTION_COLUMNS if include_epg_description else ()))


def _index_names(epg_cols: tuple) -> tuple:
    return SEARCH_INDEX_NAMES if epg_cols else (SEARCH_INDEX_CHANNELS,)


def _like_predicate(q: str, epg_cols: tuple):
    """Exactly what shipped before the index existed, and the fallback for every path that
    cannot use it. Correct but unindexed: ~89ms against 136k channels for the name alone,
    ~800ms once descriptions are in scope (dev/changelog/365 and the 2026-07-30 measurements)."""
    pattern = f'%{q}%'
    name_match = Channel.name.ilike(pattern)
    if not epg_cols:
        return name_match
    # Straight off epg_entries, not chan_prog: chan_prog is only ever populated by a
    # rebuild, so in exactly the situations this fallback exists for it may be empty or
    # stale. Same future-only window as the chan_prog build, so the two agree on results.
    airing = select(EPGEntry.channel_id).where(
        EPGEntry.stop_time >= datetime.utcnow(),
        or_(*[getattr(EPGEntry, col).ilike(pattern) for col in epg_cols]),
    )
    return or_(name_match, Channel.id.in_(airing))


def _fts_predicate(q: str, epg_cols: tuple):
    term = fts_match_term(q)
    # Column-scoped, and it has to be: ch_fts also indexes stream_url, epg_channel_id and
    # category_name, so an unscoped MATCH would silently widen this to a different feature
    # (searching more than the channel name) rather than reproducing what ships today.
    matches = Channel.id.in_(_fts_rowids('ch_fts', f'{{name}} : {term}'))
    if not epg_cols:
        return matches
    # Scoped for the same reason, and this one is newer: chan_prog_fts carries description as
    # of the 2026-07-30 widening, so a bare `term` here would search descriptions even for
    # callers that only asked for titles. `{a b} : q` is FTS5's column-filter syntax.
    scoped = '{' + ' '.join(epg_cols) + '} : ' + term
    airing = select(column('channel_id')).select_from(table('chan_prog')).where(
        column('id').in_(_fts_rowids('chan_prog_fts', scoped)))
    return or_(matches, Channel.id.in_(airing))


def fts_rowid_select(fts_table: str, match_expr: str):
    """SELECT rowid FROM <fts_table> WHERE <fts_table> MATCH <expr>, as a bound subquery.

    Only rowid is selected. Reading a *column* off an external-content FTS table whose
    content row has been deleted is not merely undefined - it raises "database disk image is
    malformed" once any matching row's content row has been deleted (measured 2026-07-30).
    Rowids alone are safe: one belonging to a deleted row simply matches nothing downstream.

    **The bind name carries a digest of the expression, and must stay unique per expression.**
    One query routinely holds several of these - a two-word search is two MATCHes against
    ch_fts, plus two more against chan_prog_fts - and a shared bind name means the last value
    bound wins for all of them: every term is then answered with one term's text, silently and
    with no error (dev/docs/BUGS.md 2026-07-30 02:32 PM). Deriving the name from the
    expression also makes two identical MATCHes share one bind, which is what they should do.
    """
    bind = f'match_{fts_table}_{hashlib.sha1(match_expr.encode()).hexdigest()[:12]}'
    return (select(column('rowid', Integer))
            .select_from(table(fts_table))
            .where(text(f'{fts_table} MATCH :{bind}').bindparams(**{bind: match_expr})))


#: Historic private name, kept because `_fts_predicate` reads well with it.
_fts_rowids = fts_rowid_select


# ---------------------------------------------------------------------------
# The airing planner
# ---------------------------------------------------------------------------
#
# The AIRING question ("when is this on") is a different grain from the channel question this
# file's other helpers answer. Two ways to run it: scan `epg_entries` with LIKE, or narrow to
# the channels `chan_prog_fts` says could carry a match and apply the same LIKE to those.
#
# **The planner needs a cost branch, and the first two measurements each said it did not.**
# The original triage measurement had the narrowed path losing badly on
# common single words (`news`: 4388ms narrowed against 1759ms for LIKE), on the reasoning that
# `ORDER BY start_time LIMIT n` lets LIKE walk ix_epg_entries_start_time and stop early. Taken
# again on 2026-07-31 against the query this module actually emits, that does not reproduce.
# Best of two, warm cache, 136,182 channels / 999k EPG rows, future-only, row sets IDENTICAL
# on every term (columns: probe / chan_prog rows / COUNT both ways / first page both ways):
#
#     term             probe   rows | COUNT narrow  COUNT LIKE | PAGE narrow  PAGE LIKE
#     wembley           1.1ms    359 |        8.0ms    1046ms  |      8.1ms     1802ms
#     breaking bad     11.3ms      8 |       11.3ms    1003ms  |     11.1ms     1958ms
#     espn              0.6ms   1483 |       20.6ms    1177ms  |     22.9ms      929ms
#     liverpool         3.7ms    545 |       17.8ms    1055ms  |     18.5ms     1092ms
#     premier league   10.7ms   1802 |       27.2ms    1022ms  |     26.6ms      988ms
#     football          5.3ms   5449 |      125.6ms    1024ms  |    118.5ms      847ms
#     nfl               0.6ms   5577 |      301.5ms    1111ms  |    297.3ms      842ms
#     news              4.9ms  20000+|      615.6ms    1089ms  |    619.4ms      918ms
#     the               2.0ms  20000+|      998.5ms    1002ms  |   1040.5ms      841ms
#     live              4.9ms  20000+|      690.0ms    1060ms  |    697.4ms      836ms
#
# Two reasons the earlier figure looked pessimistic. First, LIKE's early exit only helps the
# PAGE query; this engine also runs an uncapped COUNT for the total and the standing-option
# breakdown, and that one cannot early-exit at all - which is the column where narrowing won
# 4x to 130x on every term. Second, measured by CHANNELS narrowed to, no term covers anything
# like the whole table: even `the`, the worst case in English, selected 11,343 of 136,182
# (8.3%). Channels is the wrong denominator - the channels a stopword matches are the ones
# carrying the most airings, so 8.3% of channels is 71% of rows.
#
# **That table read as "no cut point", and it was wrong - because it measured the narrowing in
# isolation.** In the query the engine composes, the narrowing sits inside an OR with the
# channel-name side, where no index can drive it; `_airing_narrowing_conjuncts()` is what
# restates it as a drivable top-level conjunct, and only then does the narrowing's real cost
# curve show. Re-measured 2026-08-16 through the actual engine (page + uncapped COUNT,
# best-of-2 warm, `mode=ro`, 1,476,074 epg_entries / 600,918 of them future, 138,338
# channels), today against the conjunct, with the share of future rows the narrowed channel
# set covers:
#
#     term         probe  |  today    conjunct  |  covers
#     wembley         24  |  5699ms      71ms   |    0.4%
#     liverpool      398  |  3488ms     143ms   |    0.7%
#     espn         1,252  |  1837ms     228ms   |    1.8%
#     nfl          3,130  |  1817ms     178ms   |      -
#     football     4,660  |  2010ms     548ms   |    7.4%
#     live         9,152  |  2177ms    1706ms   |   29.0%
#     and         16,789  |  2500ms    2532ms   |   43.2%
#     news        30,241  |  2595ms    2075ms   |   38.5%
#     the         70,767  |  3202ms    4897ms   |   71.1%
#
# **So a cut point does exist, and what actually drives it is the share of the table the
# narrowed channel set covers** - past roughly 40% the "narrowing" narrows nothing and only
# adds USE TEMP B-TREE FOR ORDER BY, while the un-narrowed plan still early-exits its
# ix_epg_entries_start_time walk because matches are that dense. The probe is a proxy for that
# share and a deliberately imperfect one (`and` has a smaller probe than `news` yet covers
# more rows), so the threshold is set where the proxy is unambiguous rather than at the true
# crossover.
#
# What the planner also still does is refuse to narrow in the five cases where the index
# cannot answer correctly - those are correctness, not tuning, and they remain the real
# content of `airing_narrowing_decision()`.
#
# This is still what made the `epg_fts` index (Option B, +517MB and ~132s per sync)
# unnecessary; DESIGN-search-indexes.md section 4.5 carries the full write-up.

#: The probe stops counting here. The question is "is this term too common to narrow", and
#: past the threshold below the answer is "yes" however far past it goes - so counting the
#: true total of a term matching 300,000 rows is work spent to learn nothing. Must stay above
#: AIRING_PROBE_MAX_ROWS or every term saturates at the cap and none can be told apart: at
#: 20,000 this capped `news` (a 1.25x win) and `the` (a 1.5x loss) at the identical number.
#: Uncapped, the worst term measured costs 19ms.
AIRING_PROBE_LIMIT = 100_000

#: Matching chan_prog rows at or above which narrowing is abandoned for the plain scan, or
#: None for "always narrow when the index can answer". Set between the two terms that bracket
#: the crossover above - `news` at 30,241 still wins, `the` at 70,767 loses - and that window
#: is empty in practice: those two are the ONLY terms clearing 20,000 in a 46-word sweep of
#: common English, so the exact value inside it is not load-bearing. **Tuned against a
#: 1.48M-row epg_entries; the crossover is a share of the table, so re-measure if that count
#: moves a lot.**
AIRING_PROBE_MAX_ROWS = 50_000


def airing_probe_count(term_text: str, columns: tuple) -> int:
    """How many chan_prog rows this term matches, capped at AIRING_PROBE_LIMIT.

    The planner's whole input. Runs a real query, so it belongs once per request per term -
    `SearchContext` memoizes it, and nothing may call this from inside a row loop.
    """
    scope = '{' + ' '.join(columns) + '} : '
    inner = (fts_rowid_select('chan_prog_fts', scope + fts_match_term(term_text))
             .limit(AIRING_PROBE_LIMIT).subquery())
    return db.session.execute(select(func.count()).select_from(inner)).scalar() or 0


def airing_narrowing_channel_ids(term_text: str, columns: tuple):
    """The channel ids the index says could carry an airing matching this term.

    **This narrows; it never decides.** The caller still applies the real LIKE to the
    `epg_entries` row, so the two paths return identical row sets - which is the property the
    whole planner rests on. It holds because `chan_prog` is DISTINCT (channel_id, title,
    sub_title, description) over *future* entries: every future entry matching a term is on a
    channel with a matching chan_prog row, so this is a proven superset per term, and being a
    per-term superset it composes under match-all and match-any alike.

    `chan_prog` has no link back to `epg_entries` - it carries its own surrogate id - which is
    why the narrowing is by channel rather than by row.
    """
    scope = '{' + ' '.join(columns) + '} : '
    return (select(column('channel_id')).select_from(table('chan_prog'))
            .where(column('id').in_(
                fts_rowid_select('chan_prog_fts', scope + fts_match_term(term_text)))))


def fts_match_term(query: str) -> str:
    """Turn a user-typed search string into an FTS5 MATCH argument.

    Wrapped in double quotes so the whole thing is one phrase and FTS5's query operators
    (`*`, `-`, `:`, `AND`, parentheses) are taken literally - the user is typing a substring
    to find, not a boolean expression. Embedded double quotes are doubled, which is FTS5's
    own escape and the reason this can't just be an f-string at the call site.

    Callers must still check the length against TRIGRAM_MIN_CHARS first: this function
    cannot tell that a two-character term will match nothing.
    """
    return '"' + query.replace('"', '""') + '"'


# ---------------------------------------------------------------------------
# Glob-style wildcards
# ---------------------------------------------------------------------------

WILDCARD_CHARS = '*?'
_LIKE_ESCAPE = '\\'


def has_wildcards(term: str) -> bool:
    """Whether a typed term asks for glob matching (`*` = any run, `?` = one character).

    Shared rather than local because the wildcard syntax is meant to spread to this app's
    other search inputs later; the channel search is only the first adopter. A term with no
    wildcard character behaves exactly as it always has - plain substring - so this is the
    switch between the two, not a mode the user has to select.
    """
    return any(c in term for c in WILDCARD_CHARS)


def glob_to_like(term: str) -> tuple[str, bool]:
    """(SQL LIKE pattern, needs_escape) for a glob term. `*` -> `%`, `?` -> `_`.

    The pattern is anchored nowhere: a glob is still a substring match, so `ESP*2` finds
    `US| ESPN2 HD`. Only `*` and `?` are special - this is explicitly not regex (F5).

    **needs_escape is not a detail, it is a performance cliff.** SQLite's trigram index can
    answer a bare `col LIKE ?` off the index (measured 21ms against 136k channels, plan
    `INDEX 0:L0`) but silently stops doing so the moment an `ESCAPE` clause is present (79ms,
    plan `INDEX 0:`). So a term carrying a literal `%` or `_` - which is the only reason to
    need an escape - is a slower search, and the caller has to know which it got. Verified on
    this machine 2026-07-30, per CLAUDE.md's "external tools are verified empirically".

    Note that neither form is safe to run against an external-content FTS table: `LIKE`/`GLOB`
    there makes fts5 read column values out of the content table, which raises
    "database disk image is malformed" for any candidate whose channel row has since been
    deleted (measured, same date). Wildcard matching therefore runs against the base table,
    optionally narrowed by literal_runs() below.
    """
    needs_escape = '%' in term or '_' in term or _LIKE_ESCAPE in term
    out = []
    for c in term:
        if c == '*':
            out.append('%')
        elif c == '?':
            out.append('_')
        elif needs_escape and c in ('%', '_', _LIKE_ESCAPE):
            out.append(_LIKE_ESCAPE + c)
        else:
            out.append(c)
    return '%' + ''.join(out) + '%', needs_escape


def literal_runs(term: str, min_chars: int = TRIGRAM_MIN_CHARS) -> list:
    """The runs of literal text in a glob term that are long enough for the trigram index.

    Every string a glob matches must contain each of these runs as a substring, so MATCHing
    all of them is a proven superset of the glob's own result - which makes it a safe
    prefilter: narrow with the index, then verify the real pattern against the base table.
    Returns [] when the term has no run long enough, meaning no prefilter is available and
    the caller has to scan.
    """
    runs, current = [], []
    for c in term:
        if c in WILDCARD_CHARS:
            runs.append(''.join(current))
            current = []
        else:
            current.append(c)
    runs.append(''.join(current))
    return [r for r in runs if len(r) >= min_chars]
