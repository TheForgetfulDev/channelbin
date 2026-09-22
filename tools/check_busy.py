#!/usr/bin/env python3
"""Report whether any background work is in flight, for restart.sh's busy guard.

Exit 1 (and print one line per blocker) if anything a restart would interrupt is running;
exit 0 if the app is safe to restart. A missing DB file is exit 0 - a fresh install must
still be startable.

**What counts, and why each one is here.** The guard's job is that nobody destroys work
they did not know was running, so "recoverable" is not the same as "free" and does not by
itself earn an exemption (dev/changelog/732):

- **Recordings** in a status that owns a live ffmpeg child or an in-flight background
  thread (RESTART_BLOCKING_STATUSES) - **except one that is parked**, waiting on another
  recording to finish with the machine (Recording.postprocess_waiting_since). A parked
  recording is doing nothing: either its conversion ffmpeg is SIGSTOPped or it has not
  spawned one yet, and it polls once every few seconds until the other recording is done.
  Blocking on it refuses restarts for as long as that other recording runs - hours, on
  2026-09-13 - to protect work nobody is doing. It is still printed, with what a restart
  would cost it (dev/changelog/952).
- **A search index mid-rebuild**: killing one mid-transaction strands it at
  STATUS_BUILDING with nobody left to finish it - self-healing
  (reconcile_interrupted_builds() fails it at the next startup) but costly: search falls
  back to the unindexed LIKE scan until the next rebuild, and the rebuild itself (up to
  ~90s) has to start over. dev/changelog/461 is the incident that added this.
- **A health check run**, which spawns a real ffmpeg probe per channel and can run for
  hours across hundreds of them. A restart kills the live probe and abandons the rest of
  the run.
- **A single-channel test** with no job behind it - a pre-record check or a "Test now"
  click. Same live probe, one channel.
- **An account mid-sync.** Advisory-only until dev/changelog/732: a sync is safely
  cancellable, which is an argument about corruption rather than about waste. Measured on
  four real accounts a sync takes 63-135s and runs every 12 hours, so blocking on one
  refuses roughly 1 restart in 90 - cheap against a restart that costs the sync's own
  provider fetch plus up to 25 minutes of degraded search before the index janitor
  repairs it.

Every signal here is a database row that reconciles itself at the next startup, which is
what makes it safe to block on: a row left behind by a hard kill cannot wedge restarts
forever. The in-memory admission registry (app/admission.py) is deliberately NOT consulted
- this runs as a CLI in a separate process and cannot see it - so maintenance jobs and the
logo-cache batch are outside the guard. Neither owns a child process and both re-run on
their own schedule.

The queries live here rather than inlined in restart.sh so they are testable against a temp
DB in the suite (restart.sh itself can't be run from a test - it would kill the live app).

**Output contract.** One indented line per blocker, then a single unindented
`blocking-kinds: ...` line naming the kinds present. restart.sh parses that line rather
than the prose: its "no ffmpeg under the DVR output dir, so this row may be stale" hint is
true of recordings only, since a health check's probe writes to the system temp dir.

Usage:
    python3 tools/check_busy.py [--db PATH]
"""
import argparse
import os
import sqlite3
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from app.database import (RESTART_BLOCKING_STATUSES, parked_restart_phrase,  # noqa: E402
                          OD_JOB_STATUS_RUNNING)
from app.search_index import STATUS_BUILDING  # noqa: E402


def busy_rows(db_path):
    """Return ``(rows, note)`` for the recordings currently in a blocking status.

    ``rows`` is a list of ``(id, status, name)`` tuples, empty when the app is idle.
    ``note`` is a human-readable reason the database could not be consulted (missing
    or unreadable), or None on a successful read - in both of those cases ``rows`` is
    empty and the caller should treat the app as idle, because neither a fresh install
    nor an unmigrated DB may wedge a restart.

    A recording whose post-processing is parked waiting on another recording is NOT here -
    see parked_rows() below.

    Callers other than main(): dev/tools/search_bench.py, which refuses to benchmark
    while work is in flight (dev/changelog/362).
    """
    rows, note = _recording_rows(db_path)
    return [(rid, status, name) for rid, status, name, waiting, _on, _pct in rows
            if waiting is None], note


def parked_rows(db_path):
    """Return ``(rows, note)`` for recordings in a blocking status that are parked - doing
    no work, waiting on another recording to finish with the machine.

    ``rows`` is a list of ``(id, status, name, waiting_on_name, progress_pct)`` tuples,
    ``waiting_on_name`` being the recording it parked for. **These do not block
    a restart**, which is the whole reason they are told apart from busy_rows(): a process
    that is paused is not, technically, running. On 2026-09-13 two recordings parked in
    ANALYZING, each polling once a minute and doing nothing at all, refused every restart
    for hours.

    They are still PRINTED, and that half is not decoration. A parked row before the
    conversion has started has an ffmpeg nowhere and costs nothing to interrupt; one parked
    mid-conversion holds a suspended encode, and restarting discards whatever percentage it
    had reached. The operator is told which they are looking at instead of being quietly
    allowed through (dev/changelog/952).
    """
    rows, note = _recording_rows(db_path)
    return [(rid, status, name, waiting_on, pct)
            for rid, status, name, waiting, waiting_on, pct in rows
            if waiting is not None], note


def _recording_rows(db_path):
    """Every recording in a blocking status, with the columns that say whether it is
    actually working and, if parked, on what:
    ``(id, status, name, postprocess_waiting_since, postprocess_waiting_on_name,
    progress_pct)``.

    Same "missing or unreadable DB reads as idle" contract as the checks below. The waiting
    columns are read defensively - a database older than their migrations does not have
    them, and an upgrade in progress must not be told a fresh install is unreadable.
    """
    if not os.path.exists(db_path):
        return [], f'no database at {db_path}, assuming idle'

    # Read-only so this can never write, and never blocks behind the app's writer.
    conn = sqlite3.connect(f'file:{db_path}?mode=ro', uri=True)
    try:
        cols = {r[1] for r in conn.execute('PRAGMA table_info(recordings)').fetchall()}
        waiting_col, waiting_on_col = (
            c if c in cols else 'NULL'
            for c in ('postprocess_waiting_since', 'postprocess_waiting_on_name'))
        placeholders = ','.join('?' * len(RESTART_BLOCKING_STATUSES))
        rows = conn.execute(
            f'SELECT id, status, name, {waiting_col}, {waiting_on_col}, '
            f'conversion_progress_pct '
            f'FROM recordings WHERE status IN ({placeholders}) ORDER BY id',
            RESTART_BLOCKING_STATUSES,
        ).fetchall()
    except sqlite3.DatabaseError as e:
        return [], f'cannot read {db_path} ({e}), assuming idle'
    finally:
        conn.close()
    return rows, None


def rebuilding_indexes(db_path):
    """Return ``(names, note)`` for search indexes currently STATUS_BUILDING.

    Same shape and same "missing/unreadable DB reads as idle" contract as busy_rows() - a
    fresh install or a not-yet-migrated database has no search_index_state table either, and
    that must not block a restart. Runs over a separate read-only sqlite3 connection (this
    process has no Flask app, so it cannot use app.search_index.rebuilding_index_names(),
    which is an ORM query) - the two share only the STATUS_BUILDING string, imported above
    rather than retyped, so they cannot silently mean different things.
    """
    if not os.path.exists(db_path):
        return [], f'no database at {db_path}, assuming idle'

    conn = sqlite3.connect(f'file:{db_path}?mode=ro', uri=True)
    try:
        rows = conn.execute(
            'SELECT name FROM search_index_state WHERE status = ? ORDER BY name',
            (STATUS_BUILDING,),
        ).fetchall()
    except sqlite3.DatabaseError as e:
        return [], f'cannot read {db_path} ({e}), assuming idle'
    finally:
        conn.close()
    return [r[0] for r in rows], None


def running_health_checks(db_path):
    """Return ``(rows, note)`` for health check runs currently RUNNING.

    ``rows`` is a list of ``(id, name)`` tuples. A run holds this status for its whole
    length, including the configured wait between channels where no probe is live, so this
    is the signal that covers a run as a whole; in_flight_channel_tests() below covers the
    probes a run has no row for.

    Safe to block on because scheduler.py::resume_in_progress_recordings finalizes every
    job caught RUNNING at startup, so a row left by a hard kill is gone by the time the
    next process is up and cannot wedge restarts forever.

    The status comes from app/database.py's OD_JOB_STATUS_* block, imported rather than
    retyped like RESTART_BLOCKING_STATUSES and STATUS_BUILDING above. Same "missing or
    unreadable DB reads as idle" contract as busy_rows().
    """
    if not os.path.exists(db_path):
        return [], f'no database at {db_path}, assuming idle'

    conn = sqlite3.connect(f'file:{db_path}?mode=ro', uri=True)
    try:
        rows = conn.execute(
            'SELECT id, name FROM on_demand_test_jobs WHERE status = ? ORDER BY id',
            (OD_JOB_STATUS_RUNNING,)).fetchall()
    except sqlite3.DatabaseError as e:
        return [], f'cannot read {db_path} ({e}), assuming idle'
    finally:
        conn.close()
    return rows, None


def in_flight_channel_tests(db_path):
    """Return ``(rows, note)`` for single-channel tests whose probe is still running.

    ``rows`` is a list of ``(id, channel_name)`` tuples. A channel_tests row is inserted
    before its ffmpeg probe spawns and closed when the probe finishes, so an open row means
    a live probe.

    ``job_id IS NULL`` is what makes this the *single-channel* signal rather than a second
    count of the same work: every test a health check run performs carries that run's
    job_id, and running_health_checks() already reports it. What is left with no job behind
    it is a pre-record check or a "Test now" click - the two run kinds that would otherwise
    be invisible to this guard entirely.

    Safe to block on for the same reason as above: resume_in_progress_recordings closes any
    row still open at startup, so a probe killed by a hard restart cannot leave a row that
    refuses every future restart. Same "missing or unreadable DB reads as idle" contract as
    busy_rows().
    """
    if not os.path.exists(db_path):
        return [], f'no database at {db_path}, assuming idle'

    conn = sqlite3.connect(f'file:{db_path}?mode=ro', uri=True)
    try:
        rows = conn.execute(
            'SELECT t.id, c.name FROM channel_tests t '
            'LEFT JOIN channels c ON c.id = t.channel_id '
            'WHERE t.test_ended_at IS NULL AND t.job_id IS NULL ORDER BY t.id').fetchall()
    except sqlite3.DatabaseError as e:
        return [], f'cannot read {db_path} ({e}), assuming idle'
    finally:
        conn.close()
    return rows, None


def syncing_accounts(db_path):
    """Return ``(names, note)`` for accounts currently mid-sync.

    Blocking since dev/changelog/732. It was advisory-only when it was added
    (dev/changelog/680) on the grounds that a sync is safely cancellable - it commits in
    phases and the next run redoes what it lost - but that reasoning is about corruption,
    not about waste, and the guard exists to stop an operator destroying work they did not
    know was running. On 2026-08-15 a restart landed between an account's channel-upsert
    commit and its EPG fetch, killing the close-out rebuild that thread was going to run,
    and both search indexes then sat stale for 6.5 hours. The index janitor bounds that at
    ~25 minutes now, which makes it survivable rather than free.

    Blocking is cheap here, which is the other half of the argument: measured across four
    real accounts a sync runs 63-135s against a 12-hour interval, so this refuses on the
    order of 1 restart in 90. The message says what is running and leaves the call to the
    operator - --force and the in-app "Restart anyway" both go through.

    Same "missing or unreadable DB reads as idle" contract as the functions above.
    """
    if not os.path.exists(db_path):
        return [], f'no database at {db_path}, assuming idle'

    conn = sqlite3.connect(f'file:{db_path}?mode=ro', uri=True)
    try:
        rows = conn.execute(
            "SELECT name FROM accounts WHERE status = 'SYNCING' ORDER BY name").fetchall()
    except sqlite3.DatabaseError as e:
        return [], f'cannot read {db_path} ({e}), assuming idle'
    finally:
        conn.close()
    return [r[0] for r in rows], None


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--db', help='path to dvr.db (default: database.path from config.yaml)')
    args = ap.parse_args()

    db_path = args.db
    if not db_path:
        from app.config import load_config
        db_path = load_config()['database']['path']

    rows, note = busy_rows(db_path)
    if note:
        # A missing or unreadable database fails every check below identically, so say it
        # once and treat the app as idle - a fresh install must still be startable.
        print(f'check_busy: {note}')
        return 0
    parked, _parked_note = parked_rows(db_path)

    # Each check below reports its own note instead of aborting the run: an older database
    # can be missing one of these tables while the others read fine, and a table that isn't
    # there yet is idle rather than a reason to stop asking. Identical notes are said once.
    health_jobs, hc_note = running_health_checks(db_path)
    tests, test_note = in_flight_channel_tests(db_path)
    rebuilding, idx_note = rebuilding_indexes(db_path)
    syncing, sync_note = syncing_accounts(db_path)

    for rid, status, name in rows:
        print(f'  #{rid}  {status}  {name}')
    for rid, status, name, waiting_on, pct in parked:
        # Named, never counted: these are printed whether or not anything else blocks, and
        # they never reach the `kinds` list below. The cost is the point - a parked
        # conversion is holding a real encode that a restart throws away.
        print(f'  #{rid}  {status}  {name} - '
              f'{parked_restart_phrase(status, waiting_on, pct)}')
    for jid, name in health_jobs:
        print(f'  health check #{jid} {name!r} is running')
    for tid, channel_name in tests:
        print(f'  channel test #{tid} on {channel_name or "a deleted channel"!r} is running')
    for name in rebuilding:
        print(f'  search index {name!r} is rebuilding')
    for name in syncing:
        print(f'  account {name!r} is syncing')

    for seen in dict.fromkeys(n for n in (hc_note, test_note, idx_note, sync_note) if n):
        print(f'check_busy: {seen}')

    # The machine-readable half of the output. restart.sh keys its recordings-only stale-row
    # hint off this rather than off the prose above - see the module docstring.
    kinds = [name for name, present in (
        ('recordings', rows), ('health-checks', health_jobs), ('channel-tests', tests),
        ('search-indexes', rebuilding), ('account-syncs', syncing),
    ) if present]
    if kinds:
        print('blocking-kinds: ' + ' '.join(kinds))
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())
