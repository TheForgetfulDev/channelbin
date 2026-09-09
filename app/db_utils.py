"""SQLite concurrency helpers: connection pools, pragma setup, commit-retry-on-lock,
query deadlines, scan caps."""
import contextlib
import functools
import logging
import os
import threading
import time

from flask import has_request_context
from flask_sqlalchemy.session import Session as FlaskSQLAlchemySession
from sqlalchemy import event as sa_event
from sqlalchemy import text
from sqlalchemy.exc import DatabaseError, OperationalError

log = logging.getLogger(__name__)


DEFAULT_CACHE_SIZE_MB = 64

#: Default for database.wal_size_limit_mb. See _wal_size_limit_pragma_value for what the
#: setting does and, more importantly, what it does not do.
DEFAULT_WAL_SIZE_LIMIT_MB = 256

# `synchronous` IS DELIBERATELY NOT SET HERE, AND THAT IS A MEASURED DECISION rather than an
# oversight. dvr.db therefore runs at SQLite's default of FULL (2), which waits for the
# physical disk on every commit. The obvious change is NORMAL (1), which WAL mode is designed
# for and which is the usual recommendation for a WAL database - so the reason it was rejected
# belongs here, where the next person to notice will be standing.
#
# It buys nothing here, because this app does not commit the way that setting rewards.
# Measured 2026-08-01 on a copy of the real 1.68GB database, three alternated reps per arm
# (dev/changelog/428; re-runnable with `python3 dev/tools/write_bench.py`):
#
#     400 bare single-row commits    1.485s FULL   0.019s NORMAL   <- 3.71ms per fsync, 78x
#     EPG import shape (100 x 2000)  18.639s       18.923s         <- -1.5%, i.e. noise
#     real chan_prog_fts rebuild     70.204s       71.644s         <- -2.1%, i.e. noise
#
# The first line is the mechanism and it is real. The other two are why it does not matter:
# the heavy writers commit in 2000-row batches (accounts.py) and ~40 transactions
# (search_index.py), so the arithmetic ceiling on NORMAL is 0.37s of an 18.6s import and 0.15s
# of a 70s rebuild, against a run-to-run spread of 18s on the rebuild alone. Both measured
# deltas came out negative. Steady state is ~310 commits a DAY, about 1.1 seconds of fsync.
#
# So there is no throughput to buy, and FULL is what keeps the last few committed
# transactions across an OS crash or a power cut. Two things that would change the answer, and
# only these two: a per-row commit loop appearing on a hot path (fix the loop, not the
# pragma), or storage with a much slower fsync than this box's 3.71ms - an SD card or a slow
# USB disk, where the same 200-commit import would spend 6-20s waiting. If ChannelBin ever
# ships to that hardware, this becomes a config key rather than a constant.
#
# tests/support/sqlitespeed.py sets synchronous=OFF on the SUITE's throwaway databases. That
# is test-only and unrelated: it never touches an engine this module configures.

#: Bind key of the second engine on dvr.db, the one background work uses. See
#: WorkloadRoutedSession below for what "background" means and why it gets its own pool.
BACKGROUND_BIND = 'background'


class WorkloadRoutedSession(FlaskSQLAlchemySession):
    """Route a session to the background connection pool when no request is being served.

    **The rule is one sentence: no request context means the background pool.** That covers
    the account sync, the search index rebuild, every APScheduler job, the recorder watchdogs,
    the concatenator and the post-processor - with nothing to remember to opt into, which is
    the whole reason the discriminator is request context rather than a list of entry points
    somebody has to keep current. A path this fails to recognize falls back to the UI pool,
    i.e. exactly today's behavior, so a miss is never worse than not having done this.

    **Why two pools at all.** One pool served every consumer first-come-first-served, so a
    burst of UI requests could take the last connection out from under work the user cannot
    see. That is not hypothetical: on 2026-08-01 unindexed search scans held every connection
    long enough that the account sync died with `QueuePool limit of size 5 overflow 10
    reached`, and the alert write that would have reported it failed the same way
    (dev/changelog/423). Separate pools make that structurally impossible rather than
    unlikely - browsing cannot consume a connection a recording or a sync needs, because it
    is not drawing from the same set.

    Three things a future editor must not break:

    * **One transaction never spans both engines.** The decision is taken on the first
      operation of a transaction and reused until that transaction ends, rather than being
      re-taken per statement. It has to be: a session's pending, un-flushed and
      flushed-but-uncommitted rows live on the connection it opened, and a second connection
      cannot see them. Production would not notice - an app context belongs either to a
      request or to a background thread, never to both - but a *test* shares one app context
      between its body and `client.get()`, so re-deciding mid-transaction made rows seeded
      with `tests/support/seed.py` (which flushes, deliberately, without committing) invisible
      to the request that was meant to render them. Two tests caught that; nothing would have
      caught it in a third.
    * **Outside a transaction the decision is taken fresh**, so a scoped session that outlives
      one request is not still pinned to that request's pool on its next use.
    * **`bind is not None` means the caller already chose**, and an explicit choice always
      wins - that branch is what keeps `db.session.execute(..., bind_arguments={'bind': ...})`
      and SQLAlchemy's own internals honest.

    One consequence worth knowing when writing tests: because a test body's session may hold a
    background-engine transaction open across a `client.get()`, `tests/support/iocount.py`
    counts statements across every engine rather than just `db.engine`.
    """

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        #: None = undecided. True/False = this transaction's answer, held until it ends.
        self._is_background_workload = None

    def get_bind(self, mapper=None, clause=None, bind=None, **kwargs):
        engine = super().get_bind(mapper=mapper, clause=clause, bind=bind, **kwargs)
        if bind is not None:
            return engine
        engines = self._db.engines
        # Only the DEFAULT engine is rerouted. A model that declares its own __bind_key__ has
        # asked for a specific database and must keep getting it; today none do, and this is
        # what keeps that true if one ever does.
        if engine is not engines.get(None) or BACKGROUND_BIND not in engines:
            return engine
        if self._is_background_workload is None or not self.in_transaction():
            self._is_background_workload = not has_request_context()
        return engines[BACKGROUND_BIND] if self._is_background_workload else engine


def _cache_size_pragma_value(cache_size_mb):
    """Turn a config MB figure into SQLite's own cache_size units.

    The sign carries the unit and there is no way to say "MB" directly: a NEGATIVE value
    is KiB, a POSITIVE value is a page count. -65536 is 64MiB; 65536 would be 65,536 pages,
    which at the 4096-byte page_size this database uses is 256MB - four times what was
    asked for. So the negation here is load-bearing, not a style choice.

    A non-positive or unparseable setting falls back to the default rather than being
    passed through: PRAGMA cache_size=0 disables the page cache outright, which would turn
    a typo into a silent order-of-magnitude slowdown.
    """
    try:
        mb = int(cache_size_mb)
    except (TypeError, ValueError):
        mb = 0
    if mb <= 0:
        log.warning(
            'database.cache_size_mb=%r is not a positive number - using %dMB instead',
            cache_size_mb, DEFAULT_CACHE_SIZE_MB,
        )
        mb = DEFAULT_CACHE_SIZE_MB
    return -(mb * 1024)


def _wal_size_limit_pragma_value(wal_size_limit_mb):
    """Turn a config MB figure into SQLite's own journal_size_limit units (bytes; -1 = none).

    **This is a retention limit, not a quota, and the difference is the whole point.** It
    never blocks, throttles or shrinks a transaction: SQLite grows dvr.db-wal to whatever a
    write needs and commits it, however far past this number that goes. Measured on this box
    at 20x and 32x the limit, both committing every row (dev/changelog/424). What the setting
    controls is only how much of the file is KEPT once the data in it is no longer needed.

    Two behaviors a future editor has to know, both measured rather than assumed:

    * **The truncation lands on the first commit AFTER a checkpoint rewinds the WAL**, not at
      checkpoint time - SQLite's walRestartLog() sets truncateOnCommit and the next
      sqlite3WalFrames() applies the limit. A fully backfilled checkpoint on its own leaves
      the file at full size, so "I checkpointed and nothing shrank" is correct behavior.
    * **A WAL that stays under the limit is never touched at all** - no truncate, no
      re-extend, zero churn. That is why the default is generous: the cost of a low limit is
      paid by every cycle that exceeds it, while the cost of a high one is only disk.

    0 means "no limit" (SQLite's -1), the behavior this app shipped with. It is a real choice,
    so it is passed through rather than treated as a typo - unlike cache_size, where 0 would
    silently disable the page cache. Anything unparseable still falls back to the default.
    """
    try:
        mb = int(wal_size_limit_mb)
    except (TypeError, ValueError):
        log.warning(
            'database.wal_size_limit_mb=%r is not a number - using %dMB instead',
            wal_size_limit_mb, DEFAULT_WAL_SIZE_LIMIT_MB,
        )
        return DEFAULT_WAL_SIZE_LIMIT_MB * 1024 * 1024
    if mb < 0:
        log.warning(
            'database.wal_size_limit_mb=%r is negative - use 0 for no limit; '
            'using %dMB instead', wal_size_limit_mb, DEFAULT_WAL_SIZE_LIMIT_MB,
        )
        return DEFAULT_WAL_SIZE_LIMIT_MB * 1024 * 1024
    if mb == 0:
        return -1
    return mb * 1024 * 1024


def wal_size_bytes(db_path):
    """Size of `db_path`'s -wal companion file, or 0 if there is none.

    Reading the file size is safe against a live database - it opens no connection and takes
    no lock - which is what makes it usable from a scheduled job while syncs and recordings
    are running.
    """
    try:
        return os.path.getsize(str(db_path) + '-wal')
    except OSError:
        # No WAL file yet (nothing has been written since the last clean close), or the
        # path is not readable. Neither is worth a log line from a size probe.
        return 0


#: SQLAlchemy URI prefix for a filesystem SQLite database.
_SQLITE_URI_PREFIX = 'sqlite:///'


def current_wal_size_bytes():
    """wal_size_bytes() for the database the CURRENT app is bound to.

    Resolved from app.config, never from a fresh load_config(): a runtime load_config()
    reads the real config.yaml, so under a test app this would report on the production
    dvr.db while everything around it used the temp one (CLAUDE.md, BUGS.md 2026-07-18).

    Returns 0 outside an app context, or for a non-file SQLite URI (`:memory:`), so a
    logging call site never has to guard it.
    """
    from flask import current_app, has_app_context
    if not has_app_context():
        return 0
    uri = current_app.config.get('SQLALCHEMY_DATABASE_URI') or ''
    if not uri.startswith(_SQLITE_URI_PREFIX):
        return 0
    return wal_size_bytes(uri[len(_SQLITE_URI_PREFIX):])


def register_sqlite_pragmas(engine, cache_size_mb=DEFAULT_CACHE_SIZE_MB,
                            wal_size_limit_mb=DEFAULT_WAL_SIZE_LIMIT_MB):
    """Arm WAL, a busy timeout, a real page cache and the WAL size limit on every connection.

    WAL lets readers run concurrently with a writer. busy_timeout tells SQLite to
    retry for up to 10s on lock conflicts instead of raising immediately. Any engine
    talking to dvr.db needs this applied - both Flask-SQLAlchemy engines and
    APScheduler's separate jobstore engine alike - otherwise the unconfigured one
    fails fast on any contention.

    cache_size_mb is SQLite's per-connection page cache. Its 2MB stock default was never a
    considered choice for a database this size, and raising it to 64MB measured channel
    search 89ms -> 38ms and EPG deep search 1.4s -> 0.6s with no other change
    (dev/changelog/363). Callers pass the configured value; every engine gets the same one,
    since they all read the same file and benefit identically.

    wal_size_limit_mb bounds how much of dvr.db-wal is kept once it is no longer needed;
    see _wal_size_limit_pragma_value for why it cannot constrain a write. It is a
    per-CONNECTION setting rather than something stored in the database file, which is
    exactly why it belongs in this listener - a connection that misses it is unbounded for
    its whole pooled life.

    **Registration only - this opens no connection**, which is what lets create_app() call it
    the moment the engines exist and before any startup query runs. A connection opened
    before its listener is registered keeps SQLite's fail-fast `busy_timeout=0` for its whole
    pooled life, and startup queries happen early enough to create exactly that connection.
    """
    cache_size = _cache_size_pragma_value(cache_size_mb)
    wal_limit = _wal_size_limit_pragma_value(wal_size_limit_mb)

    @sa_event.listens_for(engine, 'connect')
    def _set_pragmas(dbapi_conn, connection_record):
        cursor = dbapi_conn.cursor()
        cursor.execute('PRAGMA journal_mode=WAL')
        cursor.execute('PRAGMA busy_timeout=10000')
        cursor.execute(f'PRAGMA cache_size={cache_size}')
        cursor.execute(f'PRAGMA journal_size_limit={wal_limit}')
        cursor.close()


def configure_sqlite_pragmas(engine, cache_size_mb=DEFAULT_CACHE_SIZE_MB,
                             wal_size_limit_mb=DEFAULT_WAL_SIZE_LIMIT_MB):
    """register_sqlite_pragmas(), plus an immediate apply to one pooled connection.

    The apply-now half exists so an engine whose pool already holds a connection - opened
    before registration, or by a caller that got in first - is not left running on SQLite's
    defaults until that connection happens to be recycled.

    **Every pragma has to be set in both halves.** Setting one here and not in the listener
    (or the reverse) means which behavior a connection gets depends on pool state, which is
    the silent half-fix tests/test_sqlite_cache_size.py and tests/test_wal_policy.py exist
    to catch.
    """
    cache_size = _cache_size_pragma_value(cache_size_mb)
    wal_limit = _wal_size_limit_pragma_value(wal_size_limit_mb)
    register_sqlite_pragmas(engine, cache_size_mb=cache_size_mb,
                            wal_size_limit_mb=wal_size_limit_mb)

    with engine.connect() as conn:
        conn.execute(text('PRAGMA journal_mode=WAL'))
        conn.execute(text('PRAGMA busy_timeout=10000'))
        conn.execute(text(f'PRAGMA cache_size={cache_size}'))
        conn.execute(text(f'PRAGMA journal_size_limit={wal_limit}'))


#: Seconds between repeats of one pool's exhaustion warning. The condition persists for as
#: long as the pool stays full, and a per-checkout log line would be its own denial of service.
POOL_WARN_INTERVAL_SECONDS = 60


def warn_when_pool_is_exhausted(engine, label, ceiling):
    """Log a WARNING naming the pool the moment a checkout takes its last connection.

    The diagnostic that was missing on 2026-08-01: the pool ran dry and the only trace was a
    generic `Sync failed for account 3: QueuePool limit of size 5 overflow 10 reached` several
    layers up, at the one moment when knowing *which* pool and *how full* would have shortened
    the investigation by hours. Per CLAUDE.md's failure-paths-must-be-observable rule this
    fires **before** anything fails, on the checkout that reaches the ceiling rather than on
    the one that gives up waiting - by then the damage is already done.

    `ceiling` is passed in rather than read off the pool because `_max_overflow` is private and
    the configured number is right here in the caller's hand anyway.

    Costs one integer comparison per connection checkout - not per statement, since a session
    checks out once and runs many.
    """
    state = {'last': 0.0}

    @sa_event.listens_for(engine, 'checkout')
    def _warn(dbapi_conn, connection_record, connection_proxy):
        pool = engine.pool
        if pool.checkedout() < ceiling:
            return
        now = time.monotonic()
        if now - state['last'] < POOL_WARN_INTERVAL_SECONDS:
            return
        state['last'] = now
        log.warning(
            'The %s database connection pool is full: all %d connections are checked out. '
            'Anything asking for one now waits, and fails if it waits too long.',
            label, ceiling,
        )


def _is_locked_error(exc: OperationalError) -> bool:
    msg = str(exc).lower()
    return 'database is locked' in msg or 'database table is locked' in msg


def retry_on_locked(max_attempts=5, base_delay=0.15, rollback_session=True):
    """Retry a whole function on SQLite lock contention that outlasts busy_timeout.

    On OperationalError('database is locked'), rolls back the session and re-runs
    the decorated function from scratch with exponential backoff. Only wrap
    functions whose entire body is safe to re-run in full - i.e. pure DB
    read/mutate/commit work. Never wrap code with non-idempotent side effects
    (spawning ffmpeg, calling an external API) - extract just the DB tail instead.

    rollback_session=False skips the db.session.rollback() between attempts - for
    callables that never touch the Flask-SQLAlchemy session, e.g. APScheduler
    jobstore add_job/remove_job, which write through the jobstore's own engine
    (also sqlalchemy.exc.OperationalError on lock) and must not disturb whatever
    session state the calling request has pending.
    """
    def decorator(func):
        @functools.wraps(func)
        def wrapper(*args, **kwargs):
            delay = base_delay
            for attempt in range(1, max_attempts + 1):
                try:
                    return func(*args, **kwargs)
                except OperationalError as exc:
                    if not _is_locked_error(exc) or attempt == max_attempts:
                        raise
                    if rollback_session:
                        from . import db
                        db.session.rollback()
                    log.warning(
                        'DB locked in %s (attempt %d/%d), retrying in %.2fs',
                        func.__qualname__, attempt, max_attempts, delay,
                    )
                    time.sleep(delay)
                    delay *= 2
        return wrapper
    return decorator


# How many SQLite VM operations pass between deadline checks. Measured on dvr.db: 20,000
# is ~10ms of resolution on a 1.2s scan and costs nothing detectable (an unbounded scan
# ran 1.17s, the same scan with a never-firing handler installed ran 1.09s). Read inside
# query_deadline() rather than captured, so a test can drop it to 1 and get a
# deterministic trip on a small database - a query over ten rows finishes in far fewer
# than 20,000 operations and would otherwise never invoke the handler at all.
QUERY_DEADLINE_CHECK_OPS = 20_000


class QueryDeadlineExceeded(Exception):
    """A statement was aborted because its caller's time budget ran out.

    Raised in place of the OperationalError SQLite reports, so a caller can tell "this took
    too long" apart from "this failed", and answer the two differently.
    """

    def __init__(self, seconds, label=''):
        self.seconds = seconds
        self.label = label
        super().__init__(
            f'{label or "query"} exceeded its {seconds:g}s time budget')


class QueryAbandoned(Exception):
    """A statement was aborted because nobody is waiting for its answer any more.

    Distinct from both siblings above, and the distinction is the point: this request did
    not run too long and did not fail to get a turn - it was still correct and still
    running when the only caller that wanted it went away. Reporting it as either of the
    other two would put a scary sentence in the log for the one outcome that is working as
    designed (dev/changelog/678).
    """

    def __init__(self, label=''):
        self.label = label
        super().__init__(f'{label or "query"} was abandoned by its caller')


@contextlib.contextmanager
def query_deadline(seconds, label='', abandoned=None):
    """Abort whatever SQL runs in this block once `seconds` of wall clock has elapsed.

    Enforced with SQLite's own progress handler, which is the only thing that can stop a
    statement **already running** - a Python-side check between statements cannot, and
    neither can the client hanging up (Werkzeug runs a handler thread to completion and
    only notices a dead peer when it writes the response). That distinction is the whole
    reason this exists: on 2026-08-01 a single search request scanned 1.9M rows for about
    nine minutes with nothing able to stop it (dev/changelog/418).

    `seconds` falsy or non-positive turns the deadline off and yields straight through -
    the documented escape hatch for a caller that must be allowed to run long.

    `abandoned` is an optional zero-arg predicate polled on the same callback as the clock:
    True means nothing is waiting for this answer any more, and the statement is aborted at
    once with QueryAbandoned. It rides the progress handler rather than a check between
    statements for exactly the reason the handler exists at all - the work being cancelled
    is one long-running statement, and a Python-side check never gets a turn while it runs.
    Supplying it keeps the handler installed even with the deadline switched off, so
    "cancellable but unbounded" is expressible.

    Three constraints a future editor must not break:

    * **The handler MUST be cleared in a finally.** It lives on the pooled DBAPI
      connection, not on this block, so one left installed keeps aborting every later
      request that checks that connection out.
    * **It is per-connection, so it cannot leak across threads** - each request thread has
      its own session and therefore its own connection. Do not hoist the handle.
    * **The block must not roll back for its own reasons.** A rollback releases the
      connection, so `raw` would no longer be the session's connection and the handler
      would be cleared off the wrong one. Every caller today is a read-only query path.
    """
    from . import db
    timed = bool(seconds) and seconds > 0
    if not timed and abandoned is None:
        yield
        return

    raw = db.session.connection().connection.dbapi_connection
    deadline = time.monotonic() + seconds if timed else None
    fired = []

    def _check():
        # Abandonment first: when both are true at once, "nobody wanted this" is the more
        # accurate of the two answers, and it is the one that must not be reported as a
        # user-facing timeout.
        if abandoned is not None and abandoned():
            fired.append(QueryAbandoned)
            return 1
        if deadline is None or time.monotonic() < deadline:
            return 0
        fired.append(QueryDeadlineExceeded)
        return 1

    try:
        try:
            raw.set_progress_handler(_check, QUERY_DEADLINE_CHECK_OPS)
            yield
        finally:
            raw.set_progress_handler(None, 0)
    except DatabaseError as exc:
        # `fired` IS the discriminator, and the error text deliberately is not. Measured on
        # SQLite 3.45.1: aborting during sqlite3_step reports `interrupted`, but aborting
        # while a statement is still being PREPARED reports
        # `expected 0 columns for '' but got 5` from the subquery column-name resolver
        # instead. Same abort, same handler, two unrelated messages - so a substring test
        # on the message silently turns a bounded request into a 500 for whichever half of
        # the requests happened to abort during prepare. Nothing else in the block can set
        # `fired`, and no further statement runs after an abort, so `fired` means ours.
        if not fired:
            raise
        # Hand the connection back to the pool clean. Without this it stays checked out
        # mid-transaction, which is the pool exhaustion this whole change exists to stop.
        db.session.rollback()
        if fired[0] is QueryAbandoned:
            raise QueryAbandoned(label) from exc
        raise QueryDeadlineExceeded(seconds, label) from exc


class ConcurrencyGateTimeout(Exception):
    """A caller gave up waiting for a slot in a ConcurrencyGate.

    Distinct from QueryDeadlineExceeded on purpose: this request never started work, so
    "the box is busy, come back" is a different sentence from "your query ran too long",
    and a caller answering them identically would be lying about one of them.
    """

    def __init__(self, seconds, limit, label=''):
        self.seconds = seconds
        self.limit = limit
        self.label = label
        super().__init__(
            f'waited {seconds:g}s for one of {limit} {label or "slot"} slots')


class ConcurrencyGate:
    """Cap how many threads run one kind of expensive read at once, with a bounded wait.

    `query_deadline` above bounds how long ONE request runs; this bounds how many run
    together, which is the other half of the same incident. On 2026-08-01 about ten
    unindexed search scans piled onto two cores: each was individually survivable, and
    together they saturated the box, exhausted the connection pool, killed the account sync
    that had opened the degraded window, and starved the index rebuild that would have
    closed it (608s against a normal 65-176s). Nothing bounded the multiplier
    (dev/changelog/422).

    **The caller must hold no database connection when it enters `slot()`.** A thread that
    waits here while holding a pooled connection has not fixed the exhaustion, it has moved
    it: the queue is then made of connections rather than of threads, and the background
    work that ends the degraded window still cannot get one. Every caller today releases the
    session (`db.session.rollback()`) before entering. Nothing here can enforce that, which
    is why it is stated first.

    A `threading.Condition` and a counter rather than a `BoundedSemaphore`, because the limit
    is read on every acquire: a semaphore fixes its size at construction, so changing the
    setting would need a restart to take effect, and this is a knob an operator reaches for
    precisely while something is going wrong.
    """

    def __init__(self, label=''):
        self.label = label
        self._cv = threading.Condition()
        self._held = 0

    @property
    def held(self) -> int:
        """How many slots are in use right now. For logging and tests, not for deciding -
        anything that branched on this would be racing the next acquire."""
        with self._cv:
            return self._held

    @contextlib.contextmanager
    def slot(self, limit, timeout=None):
        """Hold one of `limit` slots for the duration of the block.

        `limit` falsy or non-positive yields straight through with no bookkeeping - the same
        escape hatch shape `query_deadline(0)` documents, and what a caller passes when this
        particular request is not the expensive kind.

        `timeout=None` waits indefinitely. Every caller that has a time budget passes it, so
        that waiting is bounded by the same number the work itself is; unbounded waiting is
        only for a caller whose budget is switched off.
        """
        if not limit or limit <= 0:
            yield
            return

        with self._cv:
            if not self._cv.wait_for(lambda: self._held < limit, timeout=timeout):
                raise ConcurrencyGateTimeout(timeout, limit, self.label)
            self._held += 1
        try:
            yield
        finally:
            # In a finally, always: a slot leaked by an exception inside the block is
            # permanent for the life of the process, and at limit=1 that is the whole
            # feature failing closed forever.
            with self._cv:
                self._held -= 1
                self._cv.notify()
