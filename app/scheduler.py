"""
APScheduler integration.
- One DateTrigger job fires start_recording at start_time.
- One DateTrigger job fires stop_recording at stop_time.
- Job store persists jobs to dvr.db so they survive restarts.
- resume_in_progress_recordings() runs at startup to handle crashed sessions.
- Per-account account_sync_<id> IntervalTrigger jobs for EPG sync.
"""
import logging
import os
import re
from datetime import datetime, timedelta

from sqlalchemy import create_engine
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.jobstores.sqlalchemy import SQLAlchemyJobStore
from apscheduler.jobstores.base import JobLookupError

from . import admission
from .config import config_default
from .db_utils import configure_sqlite_pragmas, retry_on_locked
from .tz_utils import to_naive_utc

log = logging.getLogger(__name__)

_scheduler: BackgroundScheduler = None
_app = None


# All jobstore writes must go through these two wrappers, never _scheduler.add_job/
# remove_job directly: the jobstore's own engine can hit "database is locked" past
# busy_timeout, and an unretried failure 500s the calling route (seen live 2026-07-16).
# Safe to retry - a locked error means the write never happened, and add_job is only
# ever called with replace_existing=True. rollback_session=False because these never
# touch db.session.

@retry_on_locked(rollback_session=False)
def _add_job(**kwargs):
    return _scheduler.add_job(**kwargs)


@retry_on_locked(rollback_session=False)
def _remove_job(job_id):
    _scheduler.remove_job(job_id)


def remove_job_if_exists(job_id) -> bool:
    """Remove a jobstore job, retrying on lock contention; False if it wasn't there.

    "If exists" covers the scheduler not existing either. A job cannot be present when
    nothing is running, and callers on a scheduler-less app (a test app, an early-startup
    path) were getting an AttributeError out of a function whose whole contract is that a
    missing job is fine."""
    if _scheduler is None:
        return False
    try:
        _remove_job(job_id)
        return True
    except JobLookupError:
        return False


def _pid_is_alive(pid: int) -> bool:
    """Best-effort liveness check via signal 0 (sends nothing, just probes the pid)."""
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # exists, just owned by another user
    return True


def _claim_singleton_pidfile(pidfile_path: str):
    """Single-writer guard for the live scheduler + startup recovery pass.

    Returns the pid of another live process already holding pidfile_path, or None after
    claiming it for this process (including reclaiming a stale file left by a process
    that is no longer running - the normal case on every clean restart). Not a hard lock
    (no flock/O_EXCL): the hazard this guards against is a second create_app(
    start_scheduler=True) call made long after the first is already running - e.g. an
    ad-hoc diagnostic script against production (dev/docs/BUGS.md 2026-08-14) - not two
    processes racing to start at the same instant.
    """
    try:
        with open(pidfile_path, 'r') as f:
            other_pid = int(f.read().strip())
    except (OSError, ValueError):
        other_pid = None

    if other_pid and other_pid != os.getpid() and _pid_is_alive(other_pid):
        return other_pid

    os.makedirs(os.path.dirname(pidfile_path), exist_ok=True)
    with open(pidfile_path, 'w') as f:
        f.write(str(os.getpid()))
    return None


def release_pidfile():
    """Best-effort cleanup on graceful shutdown. Not required for correctness - a stale
    pidfile from a crash is already handled by the liveness check in
    _claim_singleton_pidfile - just keeps the file from lingering after a clean stop."""
    if _app is None:
        return
    pidfile_path = _app.config.get('PIDFILE_PATH')
    if not pidfile_path:
        return
    try:
        with open(pidfile_path, 'r') as f:
            if int(f.read().strip()) == os.getpid():
                os.remove(pidfile_path)
    except (OSError, ValueError):
        pass


def init_scheduler(app):
    global _scheduler, _app
    _app = app

    # Refuse loudly rather than silently double-running startup recovery: a second live
    # process (this same guard's motivating incident was an ad-hoc diagnostic script's bare
    # create_app() call against production - dev/docs/BUGS.md 2026-08-14) would otherwise
    # get its own BackgroundScheduler + resume_in_progress_recordings() pass against the
    # SAME database, which can steal an actively-recording segment and would double-fire
    # every future scheduled job. This process still serves requests - it just doesn't run
    # background jobs.
    pidfile_path = app.config.get('PIDFILE_PATH')
    other_pid = _claim_singleton_pidfile(pidfile_path) if pidfile_path else None
    if other_pid is not None:
        log.error(
            'init_scheduler: refusing to start - pid %d already holds %s. This process will '
            'keep serving requests but will not start a scheduler or run startup recovery.',
            other_pid, pidfile_path, extra={'already_alerted': True})
        with app.app_context():
            from .alerts import create_alert
            create_alert(
                'SECOND_INSTANCE_DETECTED',
                title='A second live instance was refused startup recovery',
                body=(f'This process detected another live instance (pid {other_pid}) already '
                      f'holding {pidfile_path} and refused to start its own scheduler or run '
                      'startup recovery, to avoid duplicating in-progress recordings and '
                      'scheduled jobs against the same database.'),
                source='startup:second-instance-detected',
            )
        return

    # Past the pidfile claim, so this process is the one that owns startup recovery - which
    # is exactly the licence the spool sweep needs, since it deletes by directory listing and
    # cannot tell a dead process's leftover from a live process's open spool. It ran from
    # create_app() until dev/changelog/967, where every second app build against the real
    # config destroyed a running capture's diagnostics. Before resume_in_progress_recordings
    # below, which opens the spools this must not touch.
    from .recorder import sweep_stale_stderr_spools
    sweep_stale_stderr_spools(app)

    # The URI must come from app.config, never a fresh load_config(): create_app()
    # already resolved it (from config.yaml, or from config_overrides under a test app),
    # and reading config.yaml again here would point the jobstore at the production DB
    # while the ORM used the test's temp one - which is exactly how test runs came to
    # write jobs into the live dvr.db (dev/docs/BUGS.md 2026-07-18).
    #
    # Built as an explicit engine (not url=) so we can attach the same WAL + busy_timeout +
    # cache_size + journal_size_limit pragmas as the main Flask-SQLAlchemy engine - otherwise
    # this jobstore's connections use SQLite's fail-fast defaults (busy_timeout=0)
    # and this near-constant writer becomes the main source of lock errors. journal_size_limit
    # matters here for the same reason: it is per-connection, so a WAL bounded on the other
    # two engines would still be unbounded whenever this one happened to do the commit.
    jobstore_engine = create_engine(app.config['SQLALCHEMY_DATABASE_URI'])
    configure_sqlite_pragmas(
        jobstore_engine,
        cache_size_mb=app.config['SQLITE_CACHE_SIZE_MB'],
        wal_size_limit_mb=app.config['SQLITE_WAL_SIZE_LIMIT_MB'])
    job_store = SQLAlchemyJobStore(engine=jobstore_engine)

    _scheduler = BackgroundScheduler(
        jobstores={'default': job_store},
        timezone='UTC',
        # misfire_grace_time: None = always fire missed jobs, no ceiling
        job_defaults={'misfire_grace_time': None},
    )
    _scheduler.start()
    log.info('APScheduler started')

    resume_in_progress_recordings(app)
    schedule_all_account_syncs(app)
    schedule_config_backup(app)
    schedule_recording_retention(app)
    schedule_db_maintenance(app)
    schedule_check_window_jobs(app)
    schedule_logo_cache_job(app)
    schedule_index_janitor(app)
    schedule_storage_dirs_check(app)
    schedule_account_stats_fold(app)

    # One-shot reconcile of every group's format state (auto-disable mismatched members /
    # re-enable conforming ones). Runs after migrations so schema-v6 columns exist.
    from .channel_groups import sweep_all_groups
    sweep_all_groups(app)


def schedule_recording(app, recording_id: int, start_time: datetime, stop_time: datetime):
    """Register start and stop jobs for a recording."""
    _add_job(
        func=_start_job,
        trigger='date',
        run_date=start_time,
        args=[recording_id],
        id=f'start_{recording_id}',
        replace_existing=True,
    )
    _add_job(
        func=_stop_job,
        trigger='date',
        run_date=stop_time,
        args=[recording_id],
        id=f'stop_{recording_id}',
        replace_existing=True,
    )
    _schedule_precheck_job(recording_id, start_time)
    log.info('Scheduled recording %d: start=%s stop=%s', recording_id, start_time, stop_time)


def unschedule_recording(recording_id: int):
    for job_id in (f'start_{recording_id}', f'stop_{recording_id}', f'precheck_{recording_id}',
                   f'retry_{recording_id}', f'resume_{recording_id}'):
        remove_job_if_exists(job_id)


def schedule_dead_stream_retry(recording_id: int, run_date: datetime):
    """Register/replace the one-shot retry_<id> job: dead-stream fast-fail backoff
    (CLAUDE.md Product Principle 2, app/watchdog.py::WatchdogThread._schedule_dead_stream_retry).
    Persists in the same jobstore as every other per-recording job, so it survives an app
    restart on its own (misfire_grace_time=None fires it immediately if overdue) - RETRYING is
    still in RESTART_BLOCKING_STATUSES as a belt-and-suspenders default regardless. Cancelled by
    unschedule_recording like every other per-recording job - no bespoke cancel path needed.

    No-ops (with a warning) if the scheduler isn't running - same defensive shape as
    remove_job_if_exists. A live production app always has one by the time any recording can
    reach RETRYING; this guard exists for test apps built with start_scheduler=False."""
    if _scheduler is None:
        log.warning('schedule_dead_stream_retry: no scheduler running, retry_%d not registered',
                    recording_id)
        return
    _add_job(
        func=_dead_stream_retry_job,
        trigger='date',
        run_date=run_date,
        args=[recording_id],
        id=f'retry_{recording_id}',
        replace_existing=True,
    )


def _dead_stream_retry_job(recording_id):
    with _app.app_context():
        from .recorder import fire_dead_stream_retry
        fire_dead_stream_retry(_app, recording_id)


def _schedule_precheck_job(recording_id: int, start_time: datetime):
    """Register precheck_<id> at start_time - pre_check.lead_minutes (DESIGN-prerecord-
    checks.md §3). Registered unconditionally, enabled-or-not - the enable decision happens
    at fire time from a fresh load_config(), same runtime-read pattern as the retention job,
    so toggling the feature applies to already-scheduled recordings with no job surgery. If
    the lead has already passed, register at now instead - the margin guard inside
    run_pre_check decides at fire time whether it's still viable."""
    from .config import load_config
    lead = load_config().get('channel_testing', {}).get('pre_check', {}).get('lead_minutes', 15)
    run_date = start_time - timedelta(minutes=lead)
    now = datetime.utcnow()
    if run_date < now:
        run_date = now
    _add_job(
        func=_precheck_job,
        trigger='date',
        run_date=run_date,
        args=[recording_id],
        id=f'precheck_{recording_id}',
        replace_existing=True,
    )


def _precheck_job(recording_id):
    with _app.app_context():
        from .channel_tester import run_pre_check
        run_pre_check(_app, recording_id)


def reschedule_precheck(recording_id: int, run_date: datetime):
    """Re-register precheck_<id> at a new run_date - used by run_pre_check's tester-busy
    retry (DESIGN-prerecord-checks.md §3 step 4). replace_existing=True keeps one pending
    retry job under the same teardown id, so unschedule_recording still removes it."""
    _add_job(
        func=_precheck_job,
        trigger='date',
        run_date=run_date,
        args=[recording_id],
        id=f'precheck_{recording_id}',
        replace_existing=True,
    )


def reschedule_recording_start(recording_id: int, run_date: datetime):
    """Re-register start_<id> at a new run_date - used when start_recording defers itself
    because a live mp4 conversion is running (recording.post_process.collision_policy ==
    'wait'; DESIGN-concurrency.md's precedence doctrine extended to conversion CPU/disk
    contention). Same shape as reschedule_precheck above: replace_existing=True keeps this
    under the id unschedule_recording already tears down, and each retry re-enters
    start_recording, so the collision check re-runs fresh."""
    _add_job(
        func=_start_job,
        trigger='date',
        run_date=run_date,
        args=[recording_id],
        id=f'start_{recording_id}',
        replace_existing=True,
    )


def reschedule_recording_resume(recording_id: int, run_date: datetime):
    """Re-arm resume_<id> - used when resume_recording defers itself because every
    connection slot on the account is held by another recording (dev/changelog/854).

    Its own job id rather than a reuse of start_<id>: that one targets start_recording,
    which refuses anything not SCHEDULED, and a resuming recording is PAUSED, RETRYING or
    IN_PROGRESS. Torn down by unschedule_recording alongside every other per-recording
    job, so a cancel during the wait leaves nothing armed.

    No-ops (with a warning) if no scheduler is running - same defensive shape as
    schedule_dead_stream_retry, for test apps built with start_scheduler=False."""
    if _scheduler is None:
        log.warning('reschedule_recording_resume: no scheduler running, resume_%d not registered',
                    recording_id)
        return
    _add_job(
        func=_resume_job,
        trigger='date',
        run_date=run_date,
        args=[recording_id],
        id=f'resume_{recording_id}',
        replace_existing=True,
    )


def _resume_job(recording_id):
    with _app.app_context():
        from .recorder import resume_recording
        resume_recording(_app, recording_id)


def _register_stop_job(rec):
    """Register/replace the date-triggered job that fires _stop_job at rec.stop_time."""
    _add_job(
        func=_stop_job,
        trigger='date',
        run_date=rec.stop_time,
        args=[rec.id],
        id=f'stop_{rec.id}',
        replace_existing=True,
    )


# A shortfall under this is the ordinary cost of restarting the service within a minute
# of a recording's stop time, not an outage worth waking anyone for. Same threshold and
# same reasoning as RECORDING_STARTED_LATE / RECORDING_STOPPED_EARLY in app/recorder.py.
# The event is written either way - only the alert is gated.
_OUTAGE_ALERT_MIN_SECONDS = 60


def _report_capture_lost_to_outage(rec, stopped_at):
    """Say out loud that the service was down when this recording's stop time passed.

    Reaching the past-stop-time branch of resume_in_progress_recordings() *is* the proof:
    had the app been running, the stop job would have fired at stop_time and finished the
    recording normally. So capture ended when the app died, and everything between then
    and stop_time was never recorded. That used to be reported as a bare "missing 26%"
    percentage with nothing anywhere naming the cause, which is the silent-degradation
    case Product Principle 1 exists for (dev/docs/BUGS.md 2026-08-17).

    Deliberately does NOT touch the missing-content figure itself - the DIAGNOSTICS
    capture_health measurement is correct and stays exactly as it is. This adds the "why"
    beside it, nothing else. Caller must hold an app context.
    """
    from . import db
    from .database import CAPTURE_LOST_TO_OUTAGE, RecordingSegment, add_recording_event
    from .fmt_utils import fmt_duration_phrase

    if stopped_at is None:
        # Nothing was left open (the crash landed between segments), so the last closed
        # segment's own end is when capture stopped.
        stopped_at = db.session.query(db.func.max(RecordingSegment.ended_at)).filter(
            RecordingSegment.recording_id == rec.id).scalar()
    if stopped_at is None or rec.stop_time is None:
        return

    uncaptured = (rec.stop_time - stopped_at).total_seconds()
    if uncaptured <= 0:
        return   # capture reached the scheduled end; the outage cost this recording nothing

    noticed_after = (datetime.utcnow() - stopped_at).total_seconds()
    lost_phrase = fmt_duration_phrase(uncaptured)
    detail = (f'The service was not running when this recording\'s stop time passed, so '
              f'capture ended early: the last {lost_phrase} of the scheduled window was '
              f'never recorded. Capture stopped {fmt_duration_phrase(noticed_after)} '
              f'before the service came back up and found this recording still open.')

    log.warning('Recording %d: %.0fs of the scheduled window was never captured - the '
                'service was down from %s until now', rec.id, uncaptured, stopped_at)

    rec_id = rec.id   # never read the ORM object inside the retry unit: a rollback expires it

    @retry_on_locked()
    def _log_outage_and_commit():
        add_recording_event(
            rec_id, CAPTURE_LOST_TO_OUTAGE, detail=detail,
            extra={'uncaptured_seconds': round(uncaptured, 1),
                   'capture_stopped_at': stopped_at.isoformat(),
                   'noticed_after_seconds': round(noticed_after, 1)})
        db.session.commit()

    _log_outage_and_commit()

    if uncaptured >= _OUTAGE_ALERT_MIN_SECONDS:
        from .alerts import create_alert
        create_alert(
            'CAPTURE_LOST_TO_OUTAGE',
            title=f'Recording #{rec.id} lost {lost_phrase} - service was down',
            body=(f'"{rec.name}" was still recording when the service stopped running, and '
                  f'nothing was back up by the time its stop time passed. {detail}'),
            source=f'recording:{rec.id}:capture-lost-to-outage',
            recording_id=rec.id,
        )


def resume_in_progress_recordings(app):
    """Called once at startup. Resumes crashed recordings and re-registers future ones.

    Each DB mutate+commit unit below is wrapped in a small retry_on_locked-decorated
    helper so a transient lock (very unlikely at startup, but possible if a jobstore
    write races in) is retried by redoing the mutation - not just the bare commit,
    since a rolled-back session expires pending attribute changes. Side effects
    (thread starts, scheduler.add_job) always happen after their commit succeeds,
    never inside a retried block.
    """
    with app.app_context():
        from . import db
        from .database import (
            Recording, RECORDING_RESUMED, RECORDING_FAILED, Account,
            add_recording_event,
            REC_STATUS_SCHEDULED, REC_STATUS_IN_PROGRESS, REC_STATUS_PAUSED,
            REC_STATUS_RETRYING,
            REC_STATUS_CONCATENATING, REC_STATUS_ANALYZING, REC_STATUS_CONVERTING,
            REC_STATUS_FAILED,
            FAILURE_SOURCE_MISSING, FAILURE_CONVERSION_FAILED, FAILURE_MISSED_AT_STARTUP,
        )
        from .recorder import (start_recording, resume_recording, stop_recording,
                               close_open_segment_after_unclean_stop, OPEN_SEG_REFUSED)
        from .concatenator import do_concatenation, run_postprocess_claimed
        from .accounts import finalize_sync_state
        import threading

        @retry_on_locked()
        def _reset_stuck_accounts():
            # Reset any accounts stuck in SYNCING from a previous run - no sync
            # thread is alive at startup so these states are always orphaned.
            # UNSYNCED means "never synced" (account_detail.html renders it as
            # "Never synced. Nothing has been imported from this account yet.");
            # an account that has synced before goes to ERROR instead, so the
            # interruption is loud but the previously-imported data isn't
            # misreported as never having existed (CLAUDE.md "one flag, one
            # meaning"). last_sync_at is set only on a genuine successful sync
            # (app/accounts.py::_mark_success_and_commit), so it is a recorded
            # fact, not an inference.
            stuck_accounts = Account.query.filter_by(status='SYNCING').all()
            for acc in stuck_accounts:
                resumed_status = 'ERROR' if acc.last_sync_at is not None else 'UNSYNCED'
                log.info('Account %d (%s) was SYNCING at startup - resetting to %s',
                         acc.id, acc.name, resumed_status)
                finalize_sync_state(
                    acc.id, resumed_status, 'CANCELLED',
                    account_message='Sync interrupted by service restart',
                    log_message='Interrupted by service restart',
                )
            if stuck_accounts:
                db.session.commit()

        _reset_stuck_accounts()

        @retry_on_locked()
        def _clear_stale_postprocess_waits():
            # postprocess_waiting_since says a post-processing chain is parked waiting on
            # another recording, and it is read by a process that cannot see this one
            # (tools/check_busy.py). No such chain survives a restart - the thread is gone
            # and any suspended conversion ffmpeg died with it - so every stamp present at
            # startup is orphaned, and one left set would tell the restart guard a working
            # recording is idle for the rest of its life (dev/changelog/952). Cleared before
            # any resume below re-parks a row and legitimately sets it again. All three
            # columns of the park go together through the one writer (dev/changelog/954).
            from .postprocessor import set_postprocess_wait
            stale = Recording.query.filter(
                Recording.postprocess_waiting_since.isnot(None)).all()
            for r in stale:
                log.info('Recording %d was parked waiting on recording "%s" at restart - '
                         'clearing the wait', r.id, r.postprocess_waiting_on_name)
                set_postprocess_wait(r, None)
            if stale:
                db.session.commit()

        _clear_stale_postprocess_waits()

        @retry_on_locked()
        def _record_event_and_commit(recording_id, event_type, detail=None):
            add_recording_event(recording_id, event_type, detail)
            db.session.commit()

        now = datetime.utcnow()

        # Case 1: was IN_PROGRESS when Flask died
        for rec in Recording.query.filter_by(status=REC_STATUS_IN_PROGRESS).all():
            if rec.stop_time <= now:
                log.warning(
                    'Recording %d was IN_PROGRESS and past stop_time → concatenating; the '
                    'service was not running when its stop time passed', rec.id)
                _record_event_and_commit(rec.id, RECORDING_RESUMED, 'Resuming to join the segments (past stop time)')
                # This branch used to go straight to concatenation, so a segment row left
                # open by an unclean stop stayed open forever - the recording detail page
                # then rendered it as still capturing, counting up, hours after the file
                # had been delivered (dev/docs/BUGS.md 2026-08-17). The window being over
                # is exactly why nothing else will ever close it.
                closed = close_open_segment_after_unclean_stop(rec.id)
                if closed.outcome == OPEN_SEG_REFUSED:
                    # Another live process owns this recording. Concatenating a file it is
                    # still writing would truncate the output and race its own finalize,
                    # so leave the row alone - the process that owns it will finish it.
                    # ERROR, marked already_alerted: the helper above raised the typed
                    # RECORDING_RESUME_REFUSED alert for this same condition, so the marker
                    # is what stops the second LOG_ERROR row. This used to be logged at
                    # WARNING to dodge that, which downgraded the log line to fix an alert
                    # problem - the level describes the log (dev/changelog/930).
                    log.error('Recording %d: not concatenating - segment %d is still being '
                              'written by another process', rec.id, closed.segment_number,
                              extra={'recording_id': rec.id, 'already_alerted': True})
                    continue
                _report_capture_lost_to_outage(rec, closed.stopped_at)
                threading.Thread(target=do_concatenation, args=(app, rec.id), daemon=True).start()
            else:
                log.info('Recording %d was IN_PROGRESS → resuming', rec.id)
                _record_event_and_commit(rec.id, RECORDING_RESUMED, 'Resuming mid-recording after service restart')
                resume_recording(app, rec.id)
                _register_stop_job(rec)

        # Case 1b: was PAUSED when Flask died
        for rec in Recording.query.filter_by(status=REC_STATUS_PAUSED).all():
            if rec.stop_time <= now:
                log.info('Recording %d was PAUSED and past stop_time → concatenating', rec.id)
                _record_event_and_commit(
                    rec.id, RECORDING_RESUMED,
                    'Joining a paused recording\'s segments (stop time passed at restart)',
                )
                threading.Thread(target=do_concatenation, args=(app, rec.id), daemon=True).start()
            else:
                log.info('Recording %d was PAUSED at restart → leaving as PAUSED', rec.id)
                _register_stop_job(rec)

        # Case 1b': was RETRYING (waiting out a dead-stream backoff) when Flask died. Without
        # this case the row's two persisted jobs both misfired on scheduler start and whichever
        # committed first decided the recording: stop_<id> joined what was captured, retry_<id>
        # failed it with no join (dev/changelog/988). stop_recording() is where both jobs now
        # land and where the join-or-give-up decision lives, so the sweep goes there too.
        for rec in Recording.query.filter_by(status=REC_STATUS_RETRYING).all():
            if rec.stop_time <= now:
                log.warning('Recording %d was RETRYING and past stop_time → joining what was '
                            'captured; the service was not running when its stop time passed',
                            rec.id)
                remove_job_if_exists(f'retry_{rec.id}')
                remove_job_if_exists(f'stop_{rec.id}')
                _record_event_and_commit(
                    rec.id, RECORDING_RESUMED,
                    'Ending a recording that was waiting to retry (stop time passed at restart)',
                )
                stop_recording(app, rec.id)
            else:
                # Re-registered rather than trusted to have survived in the jobstore: a row
                # left RETRYING with no retry job waits for nothing until its window closes.
                log.info('Recording %d was RETRYING at restart → leaving it waiting to retry',
                         rec.id)
                schedule_dead_stream_retry(rec.id, rec.next_retry_at or now)
                _register_stop_job(rec)

        # Case 1c: was CONVERTING when Flask died (e.g. restart.sh killed the conversion
        # ffmpeg - the origin of this whole feature). Nothing else resumes a CONVERTING row,
        # so this is the safety net. The restart-kill counts against the budget: increment
        # conversion_attempts first, and if that exhausts the budget mark FAILED (same
        # give-up path as the supervised loop) rather than resurrecting it forever.
        from .config import load_config as _load_config
        from .postprocessor import is_conversion_active
        from .database import CONVERSION_DONE
        from . import alerts as _alerts
        _pp = _load_config()['recording']['post_process']
        _auto = _pp.get('auto_restart', True)
        _max_attempts = max(0, int(_pp.get('max_restart_attempts', 3) or 0))
        # Handed to case 1d below so it cannot pick the same row up a second time. The first
        # thing a relaunched do_postprocess() does is write ANALYZING, which is exactly what
        # 1d selects on - so a row resumed here reappears in 1d's query milliseconds later and
        # gets a second chain. Both then wait out the same collision window and spawn ffmpeg
        # on the same output, which is what this case's own is_conversion_active() guard is
        # for: that guard is checked once at entry and cannot see across a wait that lasts as
        # long as the recording being yielded to. Observed live on recording 17
        # (dev/changelog/951). Not a check-then-act race - both loops run in this one thread.
        resumed_here = set()
        for rec in Recording.query.filter_by(status=REC_STATUS_CONVERTING).all():
            ts_path = rec.output_path
            if is_conversion_active(rec.id):
                # A live conversion is already running for this id (shouldn't happen at
                # startup, but never launch a second ffmpeg on the same output).
                continue
            if not ts_path or not ts_path.endswith('.ts') or not os.path.exists(ts_path):
                log.warning('Recording %d was CONVERTING but source .ts is missing → marking FAILED', rec.id)

                @retry_on_locked()
                def _fail_missing_ts(rid=rec.id):
                    r = db.session.get(Recording, rid)
                    r.status = REC_STATUS_FAILED
                    r.completed_at = datetime.utcnow()
                    r.failure_reason = FAILURE_SOURCE_MISSING
                    add_recording_event(rid, CONVERSION_DONE,
                                        'FAILED: source .ts missing at restart - cannot resume conversion')
                    db.session.commit()

                _fail_missing_ts()
                _alerts.create_alert('CONVERSION_FAILED', f'Conversion failed: {rec.name}',
                                     body='Source .ts missing at restart - cannot resume conversion.',
                                     source='scheduler', recording_id=rec.id)
                continue

            new_attempts = (rec.conversion_attempts or 0) + 1
            if _auto and _max_attempts > 0 and new_attempts > _max_attempts:
                log.info('Recording %d was CONVERTING but has exhausted its restart budget → marking FAILED', rec.id)

                @retry_on_locked()
                def _fail_exhausted(rid=rec.id, n=new_attempts):
                    r = db.session.get(Recording, rid)
                    r.status = REC_STATUS_FAILED
                    r.completed_at = datetime.utcnow()
                    r.conversion_attempts = n
                    r.failure_reason = FAILURE_CONVERSION_FAILED
                    add_recording_event(rid, CONVERSION_DONE,
                                        f'FAILED: conversion budget exhausted after {n} attempt(s) '
                                        f'(interrupted by service restart)')
                    db.session.commit()

                _fail_exhausted()
                _alerts.create_alert('CONVERSION_FAILED', f'Conversion failed: {rec.name}',
                                     body=f'Restart budget exhausted after {new_attempts} attempts.',
                                     source='scheduler', recording_id=rec.id)
                continue

            log.info('Recording %d was CONVERTING at restart → resuming conversion (attempt %d)',
                     rec.id, new_attempts)

            @retry_on_locked()
            def _count_restart_kill(rid=rec.id, n=new_attempts):
                r = db.session.get(Recording, rid)
                r.conversion_attempts = n
                db.session.commit()

            _count_restart_kill()
            resumed_here.add(rec.id)
            # Under the live-chain claim, like every other launch of this chain. A resumed run
            # can park in the conversion collision wait for hours reading ANALYZING, and the
            # Retry-conversion route refuses only on that claim or a spawned ffmpeg - neither of
            # which existed for a bare do_postprocess() thread, so a Retry during the wait
            # started a second chain onto the same output (dev/changelog/988).
            threading.Thread(target=run_postprocess_claimed, args=(app, rec.id, ts_path),
                             daemon=True).start()

        # Case 1d: was CONCATENATING or ANALYZING when Flask died (crash, or a restart
        # killing the process mid-phase). Same safety net as CONVERTING above, and both
        # statuses take the same route because do_concatenation() is the one entry point
        # that can tell them apart: it asks committed_concat_output() and resumes at
        # post-processing when the output is already there, rather than re-running a
        # finished concat against segments that are gone.
        #
        # ANALYZING almost always has a committed output - that is what the status means -
        # but it is still asked rather than assumed, because a Retry after the output was
        # deleted genuinely does need a fresh concat. Unlike CONVERTING there is no
        # restart-attempts budget to exhaust in either case.
        from .concatenator import is_concat_active, committed_concat_output
        for rec in Recording.query.filter(
                Recording.status.in_((REC_STATUS_CONCATENATING, REC_STATUS_ANALYZING))).all():
            if rec.id in resumed_here:
                # Case 1c launched this one moments ago and its chain wrote ANALYZING on the
                # way past; this is that write, not a stranded row.
                continue
            if is_concat_active(rec.id):
                # Same guard as the CONVERTING case above: a live chain already owns this
                # id, so relaunching would put a second ffmpeg on the same output.
                continue
            resuming_postprocess = committed_concat_output(rec) is not None
            phase = ('post-processing (the join is already complete)' if resuming_postprocess
                     else 'the join')
            log.info('Recording %d was %s at restart → resuming %s', rec.id, rec.status, phase)
            threading.Thread(
                target=do_concatenation, args=(app, rec.id),
                kwargs={'reason': f'Resuming {phase} after service restart'},
                daemon=True,
            ).start()

        # Post-processing alerts left standing over a recording that has since recovered.
        # Runs after the cases above so the rows they just re-launched are CONVERTING rather
        # than COMPLETED, and so the two that give up above keep the alert they just raised.
        from .postprocessor import reconcile_failure_alerts
        reconcile_failure_alerts()

        # Case 2a: SCHEDULED but entirely missed (past stop_time) → mark FAILED
        import threading as _t

        @retry_on_locked()
        def _mark_expired_failed(expired_recs):
            for rec in expired_recs:
                rec.status = REC_STATUS_FAILED
                rec.completed_at = now
                rec.failure_reason = FAILURE_MISSED_AT_STARTUP
                add_recording_event(rec.id, RECORDING_FAILED,
                                    'Recording missed entirely - stop time already passed at startup')
            if expired_recs:
                db.session.commit()

        expired = Recording.query.filter(
            Recording.status == REC_STATUS_SCHEDULED,
            Recording.stop_time <= now,
        ).all()
        for rec in expired:
            log.info('Recording %d is SCHEDULED but past stop_time → marking FAILED', rec.id)
        _mark_expired_failed(expired)

        # Case 2b: SCHEDULED, past start_time but stop_time still in future → start now
        missed = Recording.query.filter(
            Recording.status == REC_STATUS_SCHEDULED,
            Recording.start_time <= now,
            Recording.stop_time > now,
        ).all()
        for rec in missed:
            missed_by = (now - rec.start_time).total_seconds()
            log.info('Recording %d missed start by %.0fs → starting now', rec.id, missed_by)
            # The persisted start_<id> job is overdue and misfire_grace_time is None, so
            # APScheduler dispatches it on its first pass - which init_scheduler triggers
            # by starting the scheduler immediately before this sweep runs. Dropping it here
            # means this sweep is the only starter in the ordinary case rather than one of
            # two; the claim inside start_recording is what covers the case where the job
            # already fired (dev/changelog/987).
            remove_job_if_exists(f'start_{rec.id}')
            _record_event_and_commit(
                rec.id, RECORDING_RESUMED,
                f'Started late by {missed_by:.0f}s after service restart',
            )
            _t.Thread(target=start_recording, args=(app, rec.id), daemon=True).start()
            _register_stop_job(rec)

        # Case 3: future SCHEDULED recordings - re-register with scheduler
        future = Recording.query.filter(
            Recording.status == REC_STATUS_SCHEDULED,
            Recording.start_time > now,
        ).all()
        for rec in future:
            schedule_recording(app, rec.id, rec.start_time, rec.stop_time)

        # On-demand jobs: handle any that were RUNNING (mark CANCELLED, or auto-heal back to
        # SCHEDULED for a recurring job) or SCHEDULED
        from .database import OnDemandTestJob, ChannelTest, TEST_STATUS_CANCELLED
        from .channel_tester import run_on_demand_test_job
        from .config import load_config
        ct_cfg = load_config().get('channel_testing', {})

        @retry_on_locked()
        def _close_orphaned_channel_tests():
            # A channel_tests row is inserted before its ffmpeg probe spawns and closed when
            # the probe finishes, so a row still open at startup can only be one this restart
            # killed - no test thread survives a process exit. Nothing used to close them:
            # the row stayed open forever, shadowing the channel's real latest result and
            # rendering as a bare "Failed" with no reason, and it left the restart guard no
            # trustworthy signal to block on (dev/docs/BUGS.md 2026-08-18, dev/changelog/732).
            #
            # CANCELLED rather than FAILED, deliberately: health_score.py does not score a
            # cancelled test, so an interrupted probe must not apply the fail floor to a
            # channel nothing is wrong with - the same call the preempted-test path already
            # makes (dev/docs/BUGS.md 2026-07-20).
            orphaned = ChannelTest.query.filter(ChannelTest.test_ended_at.is_(None)).all()
            for test in orphaned:
                log.warning('ChannelTest %d (channel %d) was still running at restart - '
                            'closing as CANCELLED; its probe died with the previous process',
                            test.id, test.channel_id)
                test.test_ended_at = datetime.utcnow()
                test.status = TEST_STATUS_CANCELLED
                test.error_detail = 'Interrupted by service restart'
            if orphaned:
                db.session.commit()

        _close_orphaned_channel_tests()

        @retry_on_locked()
        def _cancel_running_ondemand():
            for job in OnDemandTestJob.query.filter_by(status='RUNNING').all():
                # completed=False: a job caught RUNNING at startup was interrupted by the
                # restart, never actually finished, so it can only land on SCHEDULED (recurring
                # or a kept one-off) or CANCELLED - never COMPLETED.
                kind = finalize_on_demand_job_status(job, False, ct_cfg)
                if kind == 'recurring':
                    log.info('OnDemandTestJob %d was RUNNING at restart - recurring, reverting to SCHEDULED', job.id)
                elif kind == 'kept':
                    log.info('OnDemandTestJob %d was RUNNING at restart - one-off schedule was kept, reverting to SCHEDULED', job.id)
                else:
                    log.info('OnDemandTestJob %d was RUNNING at restart - marking CANCELLED', job.id)
            db.session.commit()

        _cancel_running_ondemand()

        _unset = object()

        @retry_on_locked()
        def _update_ondemand_schedule(job_id, aps_job_id=_unset, next_run=_unset):
            job = db.session.get(OnDemandTestJob, job_id)
            if aps_job_id is not _unset:
                job.scheduler_job_id = aps_job_id
            if next_run is not _unset:
                job.scheduled_start_time = next_run
            db.session.commit()

        to_start_immediately = []
        for job in OnDemandTestJob.query.filter_by(status='SCHEDULED').all():
            if job.recurring and job.recur_paused:
                # Paused - no APScheduler job should exist; leave it that way across restarts.
                continue
            if job.recurring and job.recur_use_window:
                # Dispatcher-owned - never register a CronTrigger for these, just refresh
                # the displayed next-run time. Must be checked before the plain `recurring`
                # branch below, or it falls through and registers a CronTrigger at
                # recur_hour:recur_minute, running the check twice.
                #
                # Defensively cancel any stray APScheduler job first (a no-op when
                # scheduler_job_id is already unset, as it normally is): a row that had
                # recur_use_window flipped on outside the normal reschedule route - a
                # direct DB edit, a future migration - can otherwise leave its old
                # CronTrigger alive in the jobstore, firing independently of the
                # dispatcher and running the check twice.
                if job.scheduler_job_id:
                    cancel_on_demand_job_schedule(job)
                next_run = next_on_demand_run_for_job(job, ct_cfg)
                _update_ondemand_schedule(job.id, aps_job_id=None, next_run=next_run)
                continue
            if job.recurring:
                # The CronTrigger job persists in the jobstore across restarts - just refresh
                # the displayed next-run time, re-registering only if it somehow went missing.
                next_run = get_next_on_demand_run(job) if job.scheduler_job_id else None
                if next_run:
                    _update_ondemand_schedule(job.id, next_run=next_run)
                else:
                    aps_job_id, next_run = schedule_on_demand_job(job)
                    _update_ondemand_schedule(job.id, aps_job_id, next_run)
            elif job.scheduled_start_time and job.scheduled_start_time > now:
                aps_job_id, next_run = schedule_on_demand_job(job)
                _update_ondemand_schedule(job.id, aps_job_id, next_run)
            else:
                # Missed scheduled time - run immediately
                log.info('OnDemandTestJob %d missed scheduled time - starting now', job.id)
                _update_ondemand_schedule(job.id, aps_job_id=None)
                to_start_immediately.append(job.id)

        for job_id in to_start_immediately:
            import threading as _od_t
            _od_t.Thread(
                target=run_on_demand_test_job,
                args=(_app, job_id),
                daemon=True,
            ).start()

        # Sweep any od_job_<id> jobstore rows with no matching OnDemandTestJob (dev/docs/BUGS.md
        # 2026-08-10): every code path that deletes a job cancels its own scheduler entry first,
        # but a jobstore row can still outlive its DB row by other means (a since-fixed test
        # isolation bug wrote real od_job_1 into this exact production dvr.db on/before
        # 2026-07-18) - a stray recurring CronTrigger then fires forever, logging "job N not
        # found" on every occurrence. Self-heal at every startup rather than leaving it to repeat.
        existing_ids = {jid for (jid,) in db.session.query(OnDemandTestJob.id).all()}
        for aps_job in get_scheduler().get_jobs():
            m = re.fullmatch(r'od_job_(\d+)', aps_job.id)
            if m and int(m.group(1)) not in existing_ids:
                remove_job_if_exists(aps_job.id)
                log.warning('Removed orphaned APScheduler job %s - no matching OnDemandTestJob row',
                            aps_job.id)


def _start_job(recording_id):
    with _app.app_context():
        from .recorder import start_recording
        start_recording(_app, recording_id)


def _stop_job(recording_id):
    with _app.app_context():
        from .recorder import stop_recording
        stop_recording(_app, recording_id)


def schedule_on_demand_job(job):
    """Register an APScheduler job for an on-demand test job - a CronTrigger for a
    recurring job (job.recurring), or a one-shot DateTrigger using job.scheduled_start_time
    otherwise. Returns (aps_job_id, next_run_utc); next_run_utc is read off the registered
    APScheduler job so callers always get an accurate "next run" moment for display/sorting
    regardless of trigger type.

    A recurring job with recur_use_window=True (app/check_window.py) gets no APScheduler
    job at all - hc_window_dispatch owns starting it - so this returns (None, <next window
    occurrence>) instead.
    """
    if job.recurring and job.recur_use_window:
        from .check_window import next_occurrence_start
        from .config import load_config
        ct_cfg = load_config().get('channel_testing', {})
        return None, next_occurrence_start(ct_cfg, job, datetime.utcnow())

    aps_job_id = f'od_job_{job.id}'

    if job.recurring:
        from .tz_utils import get_display_tz_name
        tz_name = get_display_tz_name()
        kwargs = dict(
            func=_on_demand_job_trigger,
            trigger='cron',
            hour=job.recur_hour,
            minute=job.recur_minute,
            timezone=tz_name,
            args=[job.id],
            id=aps_job_id,
            replace_existing=True,
        )
        if job.recur_day in _DAY_MAP:
            kwargs['day_of_week'] = _DAY_MAP[job.recur_day]
        aps_job = _add_job(**kwargs)
        log.info('Scheduled recurring on-demand test job %d (aps_id=%s, next=%s)',
                 job.id, aps_job_id, aps_job.next_run_time)
    else:
        aps_job = _add_job(
            func=_on_demand_job_trigger,
            trigger='date',
            run_date=job.scheduled_start_time,
            args=[job.id],
            id=aps_job_id,
            replace_existing=True,
        )
        log.info('Scheduled on-demand test job %d at %s (aps_id=%s)',
                 job.id, job.scheduled_start_time, aps_job_id)

    next_run_utc = (
        to_naive_utc(aps_job.next_run_time)
        if aps_job.next_run_time else job.scheduled_start_time
    )
    return aps_job_id, next_run_utc


def get_next_on_demand_run(job):
    """Return the next naive-UTC fire time for a job's registered APScheduler job, or None.

    Returns None for a window job (recur_use_window=True) - it has no registered
    APScheduler job by design. Callers that need an accurate next-run for a window job too
    must use next_on_demand_run_for_job() instead."""
    if not job.scheduler_job_id:
        return None
    aps_job = _scheduler.get_job(job.scheduler_job_id)
    if aps_job is None or aps_job.next_run_time is None:
        return None
    return to_naive_utc(aps_job.next_run_time)


def next_on_demand_run_for_job(job, ct_cfg=None):
    """The next run time for a recurring on-demand job regardless of schedule mode -
    get_next_on_demand_run() for a normal job (reads its registered APScheduler job), or
    check_window.next_occurrence_start() for a window job (which has none).

    ct_cfg (the channel_testing config sub-dict): pass the precomputed value from any
    per-row loop caller rather than let this call load_config() itself (CLAUDE.md
    no-hidden-I/O-in-per-row-loops); single-job callers may omit it."""
    if job.recur_use_window:
        from .check_window import next_occurrence_start
        if ct_cfg is None:
            from .config import load_config
            ct_cfg = load_config().get('channel_testing', {})
        return next_occurrence_start(ct_cfg, job, datetime.utcnow())
    return get_next_on_demand_run(job)


def get_active_one_off_next_run(job):
    """For a non-recurring on-demand job, return its still-pending next-run time if the
    original one-off DateTrigger is still registered, or None otherwise.

    This is how "Run Now" (with keep_schedule=True) is distinguished from a normal ad hoc
    run afterward: keeping the schedule means cancel_on_demand_job_schedule() was never
    called, so job.scheduler_job_id still points at a live DateTrigger job. Used by every
    place that finalizes a RUNNING on-demand job's status, so a kept one-off schedule reverts
    to SCHEDULED instead of being wiped to COMPLETED/CANCELLED.
    """
    if job.recurring or not job.scheduler_job_id:
        return None
    return get_next_on_demand_run(job)


def finalize_on_demand_job_status(job, completed, ct_cfg=None):
    """Apply the "on-demand job stopped RUNNING" terminal-status decision tree to `job`,
    mutating its status/schedule fields in place. Does not commit - the caller owns its own
    retry_on_locked commit closure, and does not call cancel_on_demand_job_schedule() or raise
    any alert either, since those are call-site-specific (only a genuine test-loop finish
    alerts; only channel_tester's finish path clears the scheduler_job_id today).

    The tree, shared by every place that finalizes a RUNNING on-demand job (a normal finish,
    a crash-recovery sweep at startup, and the "recover a job whose background thread died"
    API route):
    - A recurring job never goes terminal - its CronTrigger persists and keeps firing on its
      own - so it always reverts to SCHEDULED with its next run computed.
      last_full_run_at only advances on an untruncated finish (completed=True) - it is
      due_jobs()'s ordering key, and a hard-stopped window run should leave it at its stale
      value so the next window occurrence floats back to the front of the queue.
    - A one-off "Run Now" with keep_schedule=True never cancelled its original DateTrigger -
      if it's still registered, this was just an extra ad hoc run, so it also reverts to
      SCHEDULED at the kept time rather than going terminal.
    - Otherwise the job is genuinely done: COMPLETED if `completed`, CANCELLED otherwise.

    ct_cfg (the channel_testing config sub-dict): pass the precomputed value from any per-row
    loop caller rather than let this call load_config() itself (CLAUDE.md
    no-hidden-I/O-in-per-row-loops); single-job callers may omit it.

    Returns 'recurring', 'kept', or 'finished' naming which branch was taken, so callers can
    layer their own side effects (an alert, a dispatch-thread kick, logging) on top.
    """
    job.completed_at = datetime.utcnow()
    if job.recurring:
        if completed:
            job.last_full_run_at = job.completed_at
        job.status = 'SCHEDULED'
        next_run = next_on_demand_run_for_job(job, ct_cfg)
        if next_run:
            job.scheduled_start_time = next_run
        return 'recurring'

    kept_next_run = get_active_one_off_next_run(job)
    if kept_next_run:
        job.status = 'SCHEDULED'
        job.scheduled_start_time = kept_next_run
        return 'kept'

    job.status = 'COMPLETED' if completed else 'CANCELLED'
    return 'finished'


def cancel_on_demand_job_schedule(job):
    """Remove the APScheduler job for an on-demand test job, if any.

    The pending deferred retry goes with it: a run that was deferred past a recording has a
    one-shot DateTrigger of its own, and leaving it behind would fire a cancelled, paused or
    deleted job's run hours later (CLAUDE.md "teardown releases everything the create path
    acquired"). Unconditional, because the retry exists independently of scheduler_job_id -
    a window job has no scheduler_job_id at all and can still have been deferred.
    """
    remove_job_if_exists(health_check_retry_job_id(job.id))
    if job.scheduler_job_id:
        if remove_job_if_exists(job.scheduler_job_id):
            log.info('Cancelled APScheduler job %s for on-demand job %d', job.scheduler_job_id, job.id)
        job.scheduler_job_id = None


def _on_demand_job_trigger(job_id):
    from .channel_tester import run_on_demand_test_job
    run_on_demand_test_job(_app, job_id)


def _get_account_interval(account, cfg) -> int:
    """Return the effective sync interval hours for an account."""
    if account.sync_interval_hours:
        return account.sync_interval_hours
    return cfg.get('sync', {}).get('sync_interval_hours', config_default('sync.sync_interval_hours'))


def _find_safe_next_run(candidate: datetime, existing_times: list, gap_minutes: int = 5) -> datetime:
    """Push candidate forward until it is gap_minutes clear of every existing time."""
    gap = timedelta(minutes=gap_minutes)
    max_iterations = len(existing_times) * 10 + 10
    for _ in range(max_iterations):
        conflict = False
        for t in existing_times:
            if t is None:
                continue
            if abs((candidate - t).total_seconds()) < gap.total_seconds():
                candidate += gap
                conflict = True
                break
        if not conflict:
            break
    return candidate


def schedule_account_sync(app, account_id: int, *, force_reschedule: bool = False):
    """Register or update the IntervalTrigger sync job for a single account."""
    job_id = f'account_sync_{account_id}'

    # Nothing to register against. Named rather than crashed on: every write path that
    # edits an account calls this, and on a scheduler-less app (a test app, or a startup
    # ordering that puts a write before init_scheduler) an AttributeError here would 500 a
    # route whose actual work - saving the account - had already succeeded.
    if _scheduler is None:
        log.warning('No scheduler running - sync job for account %d was not (re)scheduled.',
                    account_id)
        return

    with app.app_context():
        from . import db
        from .database import Account
        from .config import load_config

        account = db.session.get(Account, account_id)
        if account is None:
            remove_job_if_exists(job_id)
            remove_job_if_exists(sync_retry_job_id(account_id))
            return

        if not account.sync_enabled:
            remove_job_if_exists(sync_retry_job_id(account_id))
            if remove_job_if_exists(job_id):
                log.info('Removed sync job for account %d (%s) - sync disabled', account_id, account.name)
            return

        cfg = load_config()
        interval = _get_account_interval(account, cfg)

        existing = _scheduler.get_job(job_id)
        if existing is not None and existing.next_run_time is not None and not force_reschedule:
            stored_td = getattr(existing.trigger, 'interval', None)
            stored_hours = int(stored_td.total_seconds() / 3600) if stored_td else None
            if stored_hours == interval:
                log.debug('Sync job for account %d already scheduled correctly (every %dh)', account_id, interval)
                return
            log.info('Sync interval changed for account %d: %dh→%dh', account_id, stored_hours, interval)

        now = datetime.utcnow()
        if account.next_sync_at is not None and account.next_sync_at > now:
            candidate = account.next_sync_at
        elif account.status == 'UNSYNCED' or account.next_sync_at is None:
            candidate = now + timedelta(minutes=1)
        else:
            candidate = now + timedelta(hours=interval)

        # Gather next_run_time of all OTHER account sync jobs for conflict avoidance
        other_times = []
        for job in _scheduler.get_jobs():
            if job.id == job_id:
                continue
            if re.match(r'^account_sync_\d+$', job.id) and job.next_run_time is not None:
                other_times.append(to_naive_utc(job.next_run_time))

        candidate = _find_safe_next_run(candidate, other_times)

        # Keep the displayed next_sync_at in sync with the job actually being
        # registered below - otherwise a reschedule that recomputes candidate (e.g.
        # after an interrupted sync resets status to UNSYNCED, or conflict avoidance
        # pushes the time forward) leaves the DB column stale while the real job runs
        # on time, and the dashboard shows a wrong/past "NEXT SYNC" value.
        if account.next_sync_at != candidate:
            @retry_on_locked()
            def _update_next_sync_at_and_commit(aid=account_id, when=candidate):
                acc = db.session.get(Account, aid)
                acc.next_sync_at = when
                db.session.commit()

            _update_next_sync_at_and_commit()

        _add_job(
            func=_account_sync_job,
            trigger='interval',
            hours=interval,
            id=job_id,
            replace_existing=True,
            next_run_time=candidate,
            kwargs={'account_id': account_id},
        )
        log.info('Sync job scheduled for account %d (%s) every %dh, next run at %s',
                 account_id, account.name, interval, candidate)


def schedule_all_account_syncs(app, *, force_reschedule_defaults: bool = False):
    """Register sync jobs for all accounts. Called at startup and on global config change."""
    # Remove legacy global xtream_sync job if it's still in the store
    if remove_job_if_exists('xtream_sync'):
        log.info('Removed legacy global xtream_sync job from store')

    # Remove orphaned per-account jobs from the old xtream_sync_<id> naming -
    # schedule_account_sync below re-adds each one under account_sync_<id>.
    for job in _scheduler.get_jobs():
        if re.match(r'^xtream_sync_\d+$', job.id):
            if remove_job_if_exists(job.id):
                log.info('Removed legacy job %s from store', job.id)

    with app.app_context():
        from .database import Account
        accounts = Account.query.all()

    for acc in accounts:
        force = force_reschedule_defaults and acc.sync_interval_hours is None
        schedule_account_sync(app, acc.id, force_reschedule=force)


# ── Deferred occurrences ────────────────────────────────────────────────────────────
#
# A background job that yields to a recording used to simply return, dropping the occurrence
# entirely: the next attempt was a whole interval away, and nothing said so. One 30-minute
# recording on 2026-09-10 landed on all four accounts' staggered sync slots at once and turned
# a 24h interval into a 48h gap (dev/changelog/941).
#
# These helpers are the shared half of deferring instead of dropping - they answer "when could
# this job actually run", so a caller can queue one one-shot retry there rather than polling
# and raising a skip record every time round. Deliberately generic: account sync and the
# scheduled health-check jobs have the identical skip-and-drop shape and both call in here.

#: What to assume an occurrence needs when nothing has ever measured it. A real account sync
#: took 2-4 minutes across the 2026-09-09 logs, and a job with no history gets the top of that
#: range rather than zero - a zero-length window makes every instant look free, which is the
#: bug the existing 5-minute lookahead already has.
DEFAULT_OCCURRENCE_SECONDS = 240

#: Multiplier over a measured estimate. A gap sized to the exact average is a coin flip, since
#: half of all runs are longer than their own average.
OCCURRENCE_SAFETY_FACTOR = 1.5

#: Stop looking for a slot past this. Beyond it the honest answer is "it cannot catch up",
#: which is what the overdue alert says - a retry queued two days out would be a worse lie
#: than the one this whole change removes.
MAX_DEFERRAL_HORIZON_HOURS = 48


def occurrence_seconds(estimate_seconds) -> float:
    """How much clear time a job needs, from its measured average runtime."""
    if not estimate_seconds or estimate_seconds <= 0:
        return float(DEFAULT_OCCURRENCE_SECONDS)
    return estimate_seconds * OCCURRENCE_SAFETY_FACTOR


def recording_windows(*, include_active: bool = True, lead_minutes: int = 0,
                      now: datetime = None) -> list:
    """[(start, end)] naive-UTC intervals a recording guard would refuse to start inside.

    An IN_PROGRESS recording occupies from `now` to its stop time, floored at `now` so an
    overrunning recording (stop time already past) still blocks the present. A SCHEDULED one
    occupies from `lead_minutes` BEFORE its start - the window the guard actually refuses in -
    through its stop time.

    The two parameters mirror the caller's own guard settings rather than re-deciding them: a
    window here that disagreed with the guard would aim a retry straight back into a skip.
    `include_active=False` is a caller whose in-progress guard is switched off; `lead_minutes=0`
    is one whose lookahead guard is off, and a scheduled recording then blocks only from its
    actual start.

    Must be called inside an app context.
    """
    from .database import Recording, REC_STATUS_SCHEDULED, REC_STATUS_IN_PROGRESS

    now = now or datetime.utcnow()
    windows = []

    if include_active:
        for rec in Recording.query.filter_by(status=REC_STATUS_IN_PROGRESS).all():
            windows.append((now, max(rec.stop_time or now, now)))

    for rec in Recording.query.filter(
        Recording.status == REC_STATUS_SCHEDULED,
        Recording.stop_time > now,
    ).all():
        start = rec.start_time - timedelta(minutes=lead_minutes)
        windows.append((start, max(rec.stop_time, start)))

    return windows


def _merge_windows(windows: list) -> list:
    """Sorted, non-overlapping copy of [(start, end)]. Touching intervals merge."""
    ordered = sorted((w for w in windows if w[1] > w[0]), key=lambda w: w[0])
    merged = []
    for start, end in ordered:
        if merged and start <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((start, end))
    return merged


def first_free_slot(needed_seconds: float, windows: list, *, now: datetime = None,
                    horizon_hours: int = MAX_DEFERRAL_HORIZON_HOURS):
    """First moment from `now` where `needed_seconds` clears every window, or None.

    None means "no such gap within the horizon" - a real answer the caller reports, not a
    failure. Queueing a retry anyway would just re-run the guard and defer again.

    The gap is sized to the whole run, not to its start: a job that begins in a five-minute
    hole and needs four minutes still has to FINISH before the next recording's guard window
    opens, which is what the existing lookahead never checked.
    """
    now = now or datetime.utcnow()
    deadline = now + timedelta(hours=horizon_hours)
    needed = timedelta(seconds=needed_seconds)

    candidate = now
    for start, end in _merge_windows(windows):
        if end <= candidate:
            continue
        if start - candidate >= needed:
            break
        candidate = max(candidate, end)

    return None if candidate > deadline else candidate


def _sync_job_times(pattern: str) -> dict:
    """{account_id: earliest naive-UTC next_run_time} over the sync jobs matching `pattern`.

    One get_jobs() call per caller. Never reach for this per row - the two public wrappers
    below are batched for exactly that reason.
    """
    if _scheduler is None:
        return {}
    times = {}
    for job in _scheduler.get_jobs():
        m = re.match(pattern, job.id)
        if not m or job.next_run_time is None:
            continue
        account_id = int(m.group(1))
        when = to_naive_utc(job.next_run_time)
        if account_id not in times or when < times[account_id]:
            times[account_id] = when
    return times


def pending_sync_retries() -> dict:
    """{account_id: naive-UTC run time} for every queued deferred-sync retry.

    Derived from the jobstore, never stored: the queued retry's existence IS the fact that this
    account owes a sync, so no second copy can go stale and no teardown path has to remember to
    clear it. Same argument as pending_hide_materialize() below, and the reason this change adds
    no column - Account.next_sync_at could not carry a catch-up time anyway, because
    schedule_account_sync() seeds the INTERVAL trigger from it and would re-anchor the regular
    schedule on the catch-up.
    """
    return _sync_job_times(r'^account_sync_retry_(\d+)$')


def next_sync_attempts() -> dict:
    """{account_id: when a sync will next actually be attempted}, naive UTC.

    The earliest of an account's regular interval job and any deferred-retry one-shot, read off
    the jobs that will really fire. Account.next_sync_at cannot answer this: it is written only
    when a sync succeeds and when the interval job is re-registered, so it goes stale the moment
    an occurrence is deferred - and it was already capable of sitting in the past, which is the
    "overdue" every surface displayed while the real job was hours away (dev/changelog/941).

    Empty when no scheduler is running, which is a real state (a test app, early startup) and
    not an error - accounts.next_sync_map() falls back to the stored column there.
    """
    return _sync_job_times(r'^account_sync(?:_retry)?_(\d+)$')


def _reserved_sync_windows(exclude_account_id: int) -> list:
    """Windows other accounts' syncs already claim, so a catch-up lands in a gap of its own.

    Admission refuses sync-while-sync, so four accounts released into one gap would produce one
    sync and three refusals - three more deferrals, three more skip records, and no catch-up.
    Spacing them is the cheaper half of the "one at a time" rule; the alternative was chaining
    each release off the previous sync's completion, which needs a completion hook and a queue
    that a restart could strand.

    Each window is sized by that account's own runtime estimate, not a shared constant: a
    34,012-channel sync and an 8,804-channel one are not the same job.
    """
    from .accounts import get_sync_duration_estimate

    if _scheduler is None:
        return []

    reserved = []
    for job in _scheduler.get_jobs():
        m = re.match(r'^account_sync(?:_retry)?_(\d+)$', job.id)
        if not m or job.next_run_time is None:
            continue
        other_id = int(m.group(1))
        if other_id == exclude_account_id:
            continue
        start = to_naive_utc(job.next_run_time)
        estimate, _ = get_sync_duration_estimate(other_id)
        reserved.append((start, start + timedelta(seconds=occurrence_seconds(estimate))))
    return reserved


def _account_sync_job(account_id: int):
    with _app.app_context():
        from . import db
        from .config import load_config
        from .database import Account, Recording, REC_STATUS_SCHEDULED, REC_STATUS_IN_PROGRESS
        from .accounts import sync_account

        account = db.session.get(Account, account_id)
        if account is None or not account.sync_enabled:
            return

        cfg = load_config()
        sync_cfg = cfg.get('sync', {})
        within_minutes = sync_cfg.get('skip_sync_if_recording_within_minutes', 5)
        guard_active = sync_cfg.get('skip_sync_if_recording_active', True)

        if guard_active:
            active = Recording.query.filter_by(status=REC_STATUS_IN_PROGRESS).first()
            if active:
                _defer_sync_past_recording(
                    account_id, account.name, sync_cfg,
                    f'Recording "{active.name}" (#{active.id}) is in progress')
                return

        if within_minutes > 0:
            cutoff = datetime.utcnow() + timedelta(minutes=within_minutes)
            upcoming = Recording.query.filter(
                Recording.status == REC_STATUS_SCHEDULED,
                Recording.start_time <= cutoff,
            ).order_by(Recording.start_time).first()
            if upcoming:
                _defer_sync_past_recording(
                    account_id, account.name, sync_cfg,
                    f'Recording "{upcoming.name}" (#{upcoming.id}) starts within '
                    f'{within_minutes} minutes')
                return

        # Sync yields to an active test run (DESIGN-concurrency.md 5.2, gap G3) and to
        # another in-flight sync. Unlike the recording guards above this one retries, so a
        # multi-hour test run can't starve sync.
        #
        # The decision is made inside sync_account(), under the admission lock, in the same
        # breath as the registration - it is deliberately NOT a channel_tester.is_running()
        # check here. That is what this used to be, and it lost the race it was written to
        # win: the check ran in the 4ms window before the tester registered itself
        # (app/admission.py, dev/changelog/679).
        try:
            outcome = sync_account(_app, account_id)
        except Exception as exc:
            log.error('Account sync failed for account %d: %s', account_id, exc)
            return
        if isinstance(outcome, admission.Refusal):
            _defer_sync_for_contention(account_id, account.name, sync_cfg, outcome.reason)

    # EPG cleanup is no longer piggybacked here - it runs from the visible daily
    # db_maintenance_daily job (schedule_db_maintenance) so it's controllable on /jobs.


def sync_retry_job_id(account_id: int) -> str:
    """Job id of an account's pending deferred-sync retry. Public so the teardown
    paths that remove account_sync_<id> can remove this one in the same breath."""
    return f'account_sync_retry_{account_id}'


def _defer_sync_past_recording(account_id: int, account_name: str, sync_cfg: dict, reason: str):
    """A recording blocks this sync: queue one retry at the first gap long enough to finish
    it, instead of dropping the occurrence for a whole sync interval (dev/changelog/941).

    Why one dated retry rather than a poll: a 20-minute poll across a 4-hour recording writes
    a dozen SKIPPED rows into the account's sync history to say the same thing twelve times.
    The slot is computed from the recordings already on the books, so one retry lands where
    the job can actually run - and because it re-enters _account_sync_job, a recording
    scheduled in the meantime simply defers it again rather than forcing a sync through.

    Same one-shot DateTrigger and same job id as the contention deferral above, for the same
    APScheduler 3.x reason (never modify_job() on the interval trigger, which would drift every
    later fire). Sharing the id means `replace_existing` collapses a contention deferral and a
    recording deferral into one pending retry, /jobs already renders it, and the teardown paths
    in schedule_account_sync() already remove it.
    """
    from .accounts import get_sync_duration_estimate, record_skipped_sync, update_overdue_alert
    from .tz_utils import format_local

    run_date = None
    if _scheduler is None:
        # A scheduler-less app (a test app, an early-startup path) has nowhere to queue the
        # retry, so the skip record is the only surface - same degenerate case the hide-rule
        # deferral below names. Production always has a scheduler.
        why = 'There was nowhere to queue a retry'
        log.warning('Sync for account %d not deferred - %s; no scheduler to queue a retry on',
                    account_id, reason)
    else:
        needed = occurrence_seconds(get_sync_duration_estimate(account_id)[0])
        windows = recording_windows(
            include_active=sync_cfg.get('skip_sync_if_recording_active', True),
            lead_minutes=sync_cfg.get('skip_sync_if_recording_within_minutes', 5),
        ) + _reserved_sync_windows(account_id)
        run_date = first_free_slot(needed, windows)
        why = (f'No gap long enough to finish a sync was found in the next '
               f'{MAX_DEFERRAL_HORIZON_HOURS} hours')

    if run_date is None:
        # Nothing within the horizon is clear enough to finish a sync in. Saying so beats
        # queueing a retry that would only defer again on arrival; update_overdue_alert()
        # below is what carries this to a surface once the account is actually behind.
        remove_job_if_exists(sync_retry_job_id(account_id))
        record_skipped_sync(
            account_id,
            f'{reason} - this sync was skipped automatically. {why}, so it will run at its '
            'next regular time.')
        log.warning('Skipped sync for account %d - %s; no retry queued (%s)',
                    account_id, reason, why)
    else:
        _add_job(
            func=_account_sync_job,
            trigger='date',
            run_date=run_date,
            id=sync_retry_job_id(account_id),
            replace_existing=True,
            kwargs={'account_id': account_id},
        )
        record_skipped_sync(
            account_id,
            f'{reason} - this sync was deferred automatically and will retry at '
            f'{format_local(run_date)}.')
        log.info('Deferred sync for account %d - %s; retry at %s', account_id, reason, run_date)

    update_overdue_alert(account_id, sync_cfg)


def _defer_sync_for_contention(account_id: int, account_name: str, sync_cfg: dict, reason: str):
    """Skip this sync occurrence because something else holds the database axis, and queue
    one retry. `reason` is admission's own prose naming the blocker, so the alert says which
    actor it actually was rather than assuming the tester - since dev/changelog/679 a sync
    also yields to another in-flight sync.

    The config key stays `sync.tester_defer_retry_minutes`: it is the same knob with the
    same meaning (how long to wait before retrying a deferred sync), and renaming a live
    config key to widen its blurb is not worth breaking every existing config.yaml."""
    job_id = f'account_sync_{account_id}'
    retry_minutes = sync_cfg.get('tester_defer_retry_minutes', 20)

    if not retry_minutes or retry_minutes <= 0:
        _emit_job_skipped(
            job_id, f'Sync: {account_name}',
            f'{reason[0].upper()}{reason[1:]} - this sync was skipped automatically. '
            'It will run again at its next regular time.',
            account_id=account_id,
        )
        log.info('Skipping sync for account %d - %s (no retry configured)', account_id, reason)
        return

    # A one-shot DateTrigger, NOT modify_job() on the interval trigger: APScheduler 3.x
    # recomputes every subsequent interval fire from the modified next_run_time, so
    # nudging it here would permanently drift the account's sync schedule.
    # replace_existing collapses repeated deferrals into a single pending retry, and the
    # retry re-enters _account_sync_job, so all guards (recordings, and admission for the
    # tester and other syncs) re-run fresh.
    run_date = datetime.utcnow() + timedelta(minutes=retry_minutes)
    _add_job(
        func=_account_sync_job,
        trigger='date',
        run_date=run_date,
        id=sync_retry_job_id(account_id),
        replace_existing=True,
        kwargs={'account_id': account_id},
    )
    _emit_job_skipped(
        job_id, f'Sync: {account_name}',
        f'{reason[0].upper()}{reason[1:]} - this sync was deferred automatically '
        f'and will retry in {retry_minutes} minutes.',
        account_id=account_id,
    )
    log.info('Deferred sync for account %d - %s; retry at %s', account_id, reason, run_date)


def health_check_retry_job_id(job_id: int) -> str:
    """Job id of a health-check job's pending deferred retry. Public so every teardown path
    that removes od_job_<id> removes this one in the same breath - a DateTrigger left behind
    would fire against a job that has been cancelled, paused or deleted."""
    return f'od_job_retry_{job_id}'


def defer_health_check_past_recording(job_id: int, ct_cfg: dict, reason: str):
    """A recording blocks this scheduled health-check run: queue one retry at the first gap
    long enough to finish it. Returns the retry time, or None when there is no such gap.

    The sync half of this is _defer_sync_past_recording above, and both exist for the same
    reason - a scheduled run that yields to a recording used to be dropped outright, so the
    next attempt was a whole schedule away with nothing saying so (dev/changelog/941).

    Sized from the run's own measured history rather than a constant: a 4-channel group and a
    400-channel one are not the same job. The retry re-enters the job through the same trigger
    the schedule uses, so every guard - the recording checks, the busy-tester check, admission -
    is asked again fresh.
    """
    from .database import get_job_duration_estimate

    if _scheduler is None:
        # Nowhere to queue the retry - a test app or an early-startup path. The caller's own
        # run-log line is then the only record, which is what the None return tells it.
        log.warning('Health check job %d not deferred - %s; no scheduler to queue a retry on',
                    job_id, reason)
        return None

    needed = occurrence_seconds(get_job_duration_estimate(f'od_job_{job_id}')[0])
    windows = recording_windows(
        include_active=ct_cfg.get('skip_if_recording_active', True),
        lead_minutes=ct_cfg.get('skip_if_recording_within_minutes', 10),
    )
    run_date = first_free_slot(needed, windows)

    if run_date is None:
        remove_job_if_exists(health_check_retry_job_id(job_id))
        log.warning('Skipped health check job %d - %s; no free slot within %dh',
                    job_id, reason, MAX_DEFERRAL_HORIZON_HOURS)
        return None

    _add_job(
        func=_on_demand_job_trigger,
        trigger='date',
        run_date=run_date,
        args=[job_id],
        id=health_check_retry_job_id(job_id),
        replace_existing=True,
    )
    log.info('Deferred health check job %d - %s; retry at %s', job_id, reason, run_date)
    return run_date


def _defer_job_for_contention(job_id: str, job_label: str, func, reason: str,
                              retry_minutes: int = 30):
    """Skip this occurrence of a deferrable maintenance job because something heavier holds
    the database axis (app/admission.py), and queue one retry.

    The retry exists so a daily job can't starve: db_maintenance_daily and
    recording_retention_daily fire at fixed times, so a sync that reliably overlaps that
    time would otherwise defer them forever, and EPG/alert pruning that never runs is how
    the database grows without bound.

    Same one-shot DateTrigger as the sync deferral above, for the same APScheduler 3.x
    reason - never modify_job() on the cron trigger, which would drift every later fire.
    replace_existing collapses repeated deferrals into one pending retry, and the retry
    re-enters the job function, so admission is asked again fresh."""
    run_date = datetime.utcnow() + timedelta(minutes=retry_minutes)
    _add_job(
        func=func,
        trigger='date',
        run_date=run_date,
        id=f'{job_id}_retry',
        replace_existing=True,
    )
    # No object of its own to record this on, and none is needed: the retry queued just
    # above is what /jobs renders, named after this job and dated (dev/changelog/928).
    log.info('Deferred %s - %s; retry at %s', job_id, reason, run_date)


HIDE_MATERIALIZE_RETRY_JOB_ID = 'channel_hide_materialize_retry'


def _hide_materialize_retry_job(reason: str = None):
    """Re-apply the channel hide rules after a pass was refused for database contention.

    `reason` is never read here - it rides in the job's own kwargs so the Hide Rules page
    can name what blocked the pass for as long as the retry is pending, which is what
    pending_hide_materialize() reads back.
    """
    from . import channel_hiding
    with _app.app_context():
        result = channel_hiding.materialize('deferred rule pass')
        if not result.granted:
            defer_hide_materialize(result.reason)


def defer_hide_materialize(reason: str, retry_minutes: int = 15) -> None:
    """A hide-rule pass was refused: say so, and queue one retry.

    The rules themselves are already committed, so nothing is lost - what is stale is
    `Channel.hidden`, the answer every browse surface reads. A person who just saved a rule
    and sees nothing change is owed the reason, which is why this alerts rather than logging
    quietly - the Hide Rules page reads that reason back off the queued retry - and owed the
    work actually happening, which is why it retries rather than waiting for the next sync to
    pick it up by accident.

    Same one-shot DateTrigger as the sync and maintenance deferrals, for the same APScheduler
    3.x reason, and `replace_existing` collapses a burst of rule edits into one pending
    retry. The retry re-enters `materialize()`, so admission is asked again fresh.
    """
    run_date = datetime.utcnow() + timedelta(minutes=retry_minutes)
    if _scheduler is None:
        # A scheduler-less app (a test app, an early-startup path) has nowhere to queue the
        # retry, so there is no pending job for the page to read and the log line is the only
        # record. Production always has a scheduler - this is the degenerate case, not the one
        # the surface is built for.
        log.warning('Hide rules not applied - %s; no scheduler to queue a retry on', reason)
    else:
        _add_job(func=_hide_materialize_retry_job, trigger='date', run_date=run_date,
                 id=HIDE_MATERIALIZE_RETRY_JOB_ID, replace_existing=True,
                 kwargs={'reason': reason})
    log.info('Deferred hide-rule materialize - %s; retry at %s', reason, run_date)


def pending_hide_materialize() -> dict | None:
    """{'reason', 'retry_at'} while a refused hide-rule pass waits to retry, else None.

    Derived from the jobstore, never stored: the queued retry's existence IS the fact that
    the rules are saved but not yet applied, and it disappears once the retry succeeds - so
    no second copy of the state can go stale, and no teardown path has to remember to clear
    it. The reason rides in the job's kwargs because admission's prose is what names the
    blocker and nothing else records it (dev/changelog/928).
    """
    if _scheduler is None:
        return None
    try:
        job = _scheduler.get_job(HIDE_MATERIALIZE_RETRY_JOB_ID)
    except JobLookupError:
        return None
    if job is None or job.next_run_time is None:
        return None
    return {'reason': (job.kwargs or {}).get('reason') or '',
            'retry_at': to_naive_utc(job.next_run_time)}


_DAY_MAP = {1: 'sun', 2: 'mon', 3: 'tue', 4: 'wed', 5: 'thu', 6: 'fri', 7: 'sat'}


def _hc_window_dispatch_job():
    from .check_window import dispatch_tick
    dispatch_tick(_app)


def _hc_window_close_job():
    from .check_window import window_close
    window_close(_app)


def _register_window_close_job(ct_cfg):
    """(Re)register hc_window_close's CronTrigger at the window's end time. Split out of
    schedule_check_window_jobs() so reschedule_window_jobs() can re-point it alone when
    channel_testing.window.end changes, without touching hc_window_dispatch's interval."""
    from .check_window import window_bounds
    from .tz_utils import get_display_tz_name
    _start_t, end_t = window_bounds(ct_cfg)
    tz_name = get_display_tz_name()
    _add_job(
        func=_hc_window_close_job,
        trigger='cron',
        hour=end_t.hour,
        minute=end_t.minute,
        timezone=tz_name,
        id='hc_window_close',
        replace_existing=True,
    )
    log.info('Maintenance window close scheduled daily at %02d:%02d %s',
             end_t.hour, end_t.minute, tz_name)


def schedule_check_window_jobs(app):
    """Register the maintenance window's two system jobs (app/check_window.py):
    hc_window_dispatch (interval - starts the next due window check) and hc_window_close
    (cron at the window's end time - hard-stops anything still running and reports leftover
    work). No 'enabled' flag to check - a window with no checks assigned to it costs
    nothing, so both are registered unconditionally."""
    from .config import load_config
    ct_cfg = load_config().get('channel_testing', {})
    dispatch_minutes = ct_cfg.get('window', {}).get('dispatch_interval_minutes', 5)

    existing_dispatch = _scheduler.get_job('hc_window_dispatch')
    if existing_dispatch is None or existing_dispatch.next_run_time is None:
        _add_job(
            func=_hc_window_dispatch_job,
            trigger='interval',
            minutes=dispatch_minutes,
            id='hc_window_dispatch',
            replace_existing=True,
        )
        log.info('Maintenance window dispatcher scheduled every %d minute(s)', dispatch_minutes)

    _register_window_close_job(ct_cfg)


def reschedule_window_jobs():
    """Re-register hc_window_close (its cron hour/minute follow channel_testing.window.end)
    and refresh every window check's displayed next-run time - called from the settings
    save path when window.start/end change, same shape as the sync.sync_interval_hours
    side effect at app/routes/settings.py's api_settings_field. start/end are re-read on
    every dispatcher tick and by hc_window_close directly, so this is purely about keeping
    hc_window_close's own CronTrigger and the DB's displayed next-run in sync - not a
    prerequisite for the new bounds to take effect."""
    from . import db
    from .config import load_config
    from .database import OnDemandTestJob

    ct_cfg = load_config().get('channel_testing', {})
    _register_window_close_job(ct_cfg)

    @retry_on_locked()
    def _refresh_window_job_times():
        for job in OnDemandTestJob.query.filter_by(
                recurring=True, recur_use_window=True, status='SCHEDULED').all():
            job.scheduled_start_time = next_on_demand_run_for_job(job, ct_cfg)
        db.session.commit()

    _refresh_window_job_times()


def schedule_config_backup(app):
    """Register a daily CronTrigger job for config backup."""
    from .config import load_config
    cfg = load_config()
    cb_cfg = cfg.get('config_backup', {})

    if not cb_cfg.get('enabled', True):
        log.info('Config backup disabled - not scheduling daily backup job')
        return

    hour = cb_cfg.get('backup_hour_et', 1)

    existing = _scheduler.get_job('config_backup_daily')
    if existing is not None and existing.next_run_time is not None:
        log.info('Config backup job already in store, next run: %s', existing.next_run_time)
        return

    from .tz_utils import get_display_tz_name
    tz_name = get_display_tz_name()
    _add_job(
        func=_config_backup_job,
        trigger='cron',
        hour=hour,
        minute=0,
        timezone=tz_name,
        id='config_backup_daily',
        replace_existing=True,
    )
    log.info('Config backup scheduled daily at %02d:00 %s', hour, tz_name)


def _config_backup_job():
    with _app.app_context():
        from .config import load_config
        from .config_backup import do_backup, get_backup_dir, prune_backups
        from .database import record_job_run, JOB_RUN_SUCCESS, JOB_RUN_FAILED
        cfg = load_config()
        cb_cfg = cfg.get('config_backup', {})
        backup_dir = get_backup_dir()
        keep_days = cb_cfg.get('backup_retention_days', 14)
        started = datetime.utcnow()
        try:
            do_backup(backup_dir=backup_dir)
            prune_backups(backup_dir=backup_dir, keep_days=keep_days)
        except Exception as exc:
            log.error('Config backup failed: %s', exc)
            record_job_run('config_backup_daily', started, datetime.utcnow(), JOB_RUN_FAILED)
        else:
            record_job_run('config_backup_daily', started, datetime.utcnow(), JOB_RUN_SUCCESS)


def schedule_recording_retention(app):
    """Register the daily recording-retention sweep. Registered unconditionally: the
    job itself reads recording.retention_days (and per-profile overrides) at run time,
    so turning retention on/off in Settings takes effect without a restart."""
    from .tz_utils import get_display_tz_name
    tz_name = get_display_tz_name()
    _add_job(
        func=_recording_retention_job,
        trigger='cron',
        hour=4,
        minute=0,
        timezone=tz_name,
        id='recording_retention_daily',
        replace_existing=True,
    )
    log.info('Recording retention sweep scheduled daily at 04:00 %s', tz_name)


def _recording_retention_job():
    """Delete terminal recordings (and their files) older than their effective
    retention window. Effective window = the recording's profile.retention_days when
    set, else the global recording.retention_days; a value <= 0 means never delete.
    Only COMPLETED/FAILED/ABORTED rows are ever eligible - active/scheduled ones are
    untouched.

    Yields the database axis to every other background actor (app/admission.py): this
    hydrates every terminal recording row and then deletes files, and nothing about it is
    urgent enough to run alongside a sync or a rebuild."""
    ticket = admission.try_start(admission.KIND_MAINTENANCE, 'recording retention')
    if not ticket.granted:
        _defer_job_for_contention('recording_retention_daily', 'Recording Retention',
                                  _recording_retention_job, ticket.reason)
        return
    try:
        _recording_retention_sweep()
    finally:
        admission.release(ticket)


def _recording_retention_sweep():
    with _app.app_context():
        from . import db
        from .config import load_config
        from .database import (
            Recording, record_job_run, detach_recording_references,
            JOB_RUN_SUCCESS, JOB_RUN_FAILED,
            REC_STATUS_COMPLETED, REC_STATUS_FAILED, REC_STATUS_ABORTED,
        )
        from .recorder import recording_disk_paths, recording_image_paths, delete_files

        started = datetime.utcnow()
        try:
            cfg = load_config()
            rec_cfg = cfg.get('recording', {})
            global_days = rec_cfg.get('retention_days', 0) or 0
            delete_file = rec_cfg.get('retention_delete_file', False)
            now = datetime.utcnow()
            candidates = Recording.query.filter(
                Recording.status.in_((REC_STATUS_COMPLETED, REC_STATUS_FAILED, REC_STATUS_ABORTED))
            ).all()

            doomed = []
            for rec in candidates:
                days = global_days
                if rec.profile is not None and rec.profile.retention_days is not None:
                    days = rec.profile.retention_days
                if not days or days <= 0:
                    continue
                # completed_at is the true "done" moment; fall back to stop_time then
                # created_at so a row missing later timestamps is still eligible.
                anchor = rec.completed_at or rec.stop_time or rec.created_at
                if anchor is None:
                    continue
                if now - anchor >= timedelta(days=days):
                    doomed.append(rec.id)

            deleted = 0
            for rid in doomed:
                paths = (recording_disk_paths(rid, cfg) if delete_file
                         else recording_image_paths(rid, cfg))
                unschedule_recording(rid)

                @retry_on_locked()
                def _delete_row(rid=rid):
                    r = db.session.get(Recording, rid)
                    detach_recording_references(rid)
                    if r is not None:
                        db.session.delete(r)
                    db.session.commit()

                _delete_row()
                delete_files(paths)
                deleted += 1

            if deleted:
                log.info(
                    'Recording retention: deleted %d recording(s) past their window (files %s)',
                    deleted, 'removed' if delete_file else 'kept on disk',
                )
        except Exception:
            record_job_run('recording_retention_daily', started, datetime.utcnow(), JOB_RUN_FAILED)
            raise
        else:
            record_job_run('recording_retention_daily', started, datetime.utcnow(), JOB_RUN_SUCCESS)


def schedule_db_maintenance(app):
    """Register the daily DB-maintenance sweep (dismissed-alert + EPG-entry retention).

    Registered unconditionally: the job reads alerts.keep_days / sync.epg_keep_days at run
    time, so retention windows change from Settings without a restart. Staggered off
    recording_retention_daily (04:00) so the /jobs overlap detector doesn't flag them."""
    from .tz_utils import get_display_tz_name
    tz_name = get_display_tz_name()
    _add_job(
        func=_db_maintenance_job,
        trigger='cron',
        hour=4,
        minute=30,
        timezone=tz_name,
        id='db_maintenance_daily',
        replace_existing=True,
    )
    log.info('DB maintenance sweep scheduled daily at 04:30 %s', tz_name)


def _db_maintenance_job():
    """Prune dismissed alerts and old EPG entries, and report on the WAL. Each sub-task is
    self-contained (its own app_context + retry_on_locked commit) so one failing doesn't
    skip the others - so this job's own recorded outcome is SUCCESS whenever it completed
    all three sub-tasks, even if one of them logged its own failure above.

    Yields the database axis to every other background actor (app/admission.py): EPG
    cleanup drags a full `programs` index rebuild behind it, which makes this the heaviest
    recurring job in the app and the one most worth keeping off a sync's back."""
    ticket = admission.try_start(admission.KIND_MAINTENANCE, 'database maintenance')
    if not ticket.granted:
        _defer_job_for_contention('db_maintenance_daily', 'Database Maintenance',
                                  _db_maintenance_job, ticket.reason)
        return
    try:
        _db_maintenance_sweep()
    finally:
        admission.release(ticket)


def _db_maintenance_sweep():
    from .alerts import cleanup_old_alerts
    from .accounts import cleanup_old_epg_entries
    from .database import record_job_run, JOB_RUN_SUCCESS

    started = datetime.utcnow()
    try:
        cleanup_old_alerts(_app)
    except Exception:
        log.exception('DB maintenance: alert cleanup failed')
    try:
        cleanup_old_epg_entries(_app)
    except Exception:
        log.exception('DB maintenance: EPG cleanup failed')
    try:
        _wal_maintenance(_app)
    except Exception:
        log.exception('DB maintenance: WAL check failed')

    with _app.app_context():
        record_job_run('db_maintenance_daily', started, datetime.utcnow(), JOB_RUN_SUCCESS)


def _wal_maintenance(app):
    """Log the WAL size every day, and truncate it only when it is over the configured limit.

    **The log line is the point, not the truncate.** dvr.db-wal's size is a high-water mark
    with no timestamp on it, so a one-off reading cannot say what grew it - which is why
    2758.7MB of WAL against a 1598.6MB database could only be explained by inference
    (dev/changelog/424). A daily reading turns that into a series: the day a number jumps is
    the day to go looking, and the per-operation lines at the sync and index-rebuild
    close-outs then say which operation it was.

    **Truncating unconditionally would be a bug, not thoroughness.** journal_size_limit
    already gives the tail back on the first commit after a checkpoint rewinds the WAL, so in
    normal operation there is nothing here to reclaim, and a nightly wal_checkpoint(TRUNCATE)
    would take a healthy few-MB file to zero and make the next day's writes re-extend it from
    scratch - pure churn, every night, forever. Over the limit means the opposite: no
    rewinding checkpoint has managed to run at all (a reader has been pinning the WAL), which
    is the one case the pragma cannot fix on its own and the only one worth blocking writers
    briefly to repair. 04:30 is when that is cheapest.

    A partial checkpoint (busy=1) is a normal outcome under a live reader, not a failure.
    """
    from . import db
    from .db_utils import (BACKGROUND_BIND, _wal_size_limit_pragma_value,
                           current_wal_size_bytes)
    from .fmt_utils import fmt_bytes

    with app.app_context():
        limit = _wal_size_limit_pragma_value(app.config['SQLITE_WAL_SIZE_LIMIT_MB'])
        before = current_wal_size_bytes()
        if limit < 0 or before <= limit:
            log.info('DB maintenance: WAL is %s (limit %s) - nothing to reclaim',
                     fmt_bytes(before),
                     'none' if limit < 0 else fmt_bytes(limit))
            return
        # A dedicated connection rather than db.session: a checkpoint cannot run inside an
        # open transaction, and the session's may be one. Background pool by preference,
        # since that is where this job's other work already lives.
        engine = db.engines.get(BACKGROUND_BIND) or db.engine
        with engine.connect() as conn:
            # busy, log, checkpointed - see PRAGMA wal_checkpoint. busy=1 means a reader held it.
            row = conn.exec_driver_sql('PRAGMA wal_checkpoint(TRUNCATE)').fetchone()
        after = current_wal_size_bytes()
        log.warning(
            'DB maintenance: WAL was %s, over the %s limit - truncated to %s '
            '(checkpoint result %s). A WAL this size means checkpoints could not rewind it, '
            'i.e. something held a read transaction open across a long run of writes.',
            fmt_bytes(before), fmt_bytes(limit), fmt_bytes(after), tuple(row) if row else None)


_LOGO_CACHE_BATCH_SIZE = 15
_LOGO_CACHE_INTERVAL_MINUTES = 5


def schedule_logo_cache_job(app):
    """Register the channel-logo-cache fetch job at startup, if the feature is on."""
    apply_logo_cache_schedule()


def apply_logo_cache_schedule() -> bool:
    """Register or remove logo_cache_fetch to match recording.logo_cache.enabled.
    Returns True if the job is scheduled afterwards.

    Called at startup and from every path that can move the setting, so turning the
    feature on or off still takes effect with no restart - the property the previous
    unconditional registration existed for. It is the registration that now follows the
    setting rather than the tick: a disabled install used to get a poll every 5 minutes
    that scanned the whole channels table and recorded a JobRun for doing nothing
    (dev/changelog/1056).

    Idempotent, and safe to call when the job is already in the state asked for - an
    already-live job is left alone rather than re-added, so a call does not reset the
    interval and push the next tick out by a fresh 5 minutes."""
    from .config import load_config
    enabled = (load_config().get('recording', {})
               .get('logo_cache', {}).get('enabled', False))

    existing = _scheduler.get_job('logo_cache_fetch') if _scheduler else None
    if not enabled:
        if remove_job_if_exists('logo_cache_fetch'):
            log.info('Logo caching is off - unscheduled the logo cache fetch job')
        return False

    if existing is not None and existing.next_run_time is not None:
        return True
    _add_job(
        func=_logo_cache_job,
        trigger='interval',
        minutes=_LOGO_CACHE_INTERVAL_MINUTES,
        id='logo_cache_fetch',
        replace_existing=True,
    )
    log.info('Logo cache fetch scheduled every %d minutes', _LOGO_CACHE_INTERVAL_MINUTES)
    return True


def _logo_cache_job():
    with _app.app_context():
        from .database import record_job_run, JOB_RUN_SUCCESS, JOB_RUN_FAILED
        from .logo_cache import run_logo_cache_batch

        started = datetime.utcnow()
        try:
            attempted = run_logo_cache_batch(limit=_LOGO_CACHE_BATCH_SIZE)
        except Exception:
            log.exception('Logo cache fetch failed')
            record_job_run('logo_cache_fetch', started, datetime.utcnow(), JOB_RUN_FAILED)
            return
        if attempted:
            log.info('Logo cache: attempted %d channel logo(s)', attempted)
        record_job_run('logo_cache_fetch', started, datetime.utcnow(), JOB_RUN_SUCCESS)


_INDEX_JANITOR_INTERVAL_MINUTES = 10


def schedule_index_janitor(app):
    """Register the search-index janitor. Registered unconditionally, like the other
    recurring jobs: the job reads search.index_janitor_grace_minutes at run time, so
    changing (or zeroing) it in Settings takes effect on the next tick with no restart.

    Same "leave an existing live job alone" shape as apply_logo_cache_schedule, so a
    restart does not reset the interval and delay the next tick by a fresh 10 minutes."""
    existing = _scheduler.get_job('search_index_janitor')
    if existing is not None and existing.next_run_time is not None:
        return
    _add_job(
        func=_index_janitor_job,
        trigger='interval',
        minutes=_INDEX_JANITOR_INTERVAL_MINUTES,
        id='search_index_janitor',
        replace_existing=True,
    )
    log.info('Search index janitor scheduled every %d minutes', _INDEX_JANITOR_INTERVAL_MINUTES)


def _index_janitor_job():
    """Rebuild a search index that has been unusable for longer than its grace window with
    nobody rebuilding it (app/search_index.py::run_index_janitor).

    Yields the database axis to a sync or another rebuild, from inside
    rebuild_search_indexes(refusable=True) rather than from a check here - a caller that
    reads who is running and then starts has rebuilt the race app/admission.py exists to
    close (dev/changelog/679).

    Records a JobRun only on a tick that actually rebuilt something. The overwhelming
    majority of ticks are four index-hit queries and a no-op, and recording those would
    average the /jobs "expected runtime" down to milliseconds - i.e. it would answer a
    question ("how long does this take when it runs?") with the cost of it not running."""
    with _app.app_context():
        from .config import load_config
        from .database import record_job_run, JOB_RUN_SUCCESS, JOB_RUN_FAILED
        from .search_index import run_index_janitor

        grace = load_config().get('search', {}).get('index_janitor_grace_minutes', 15)
        started = datetime.utcnow()
        try:
            outcome = run_index_janitor(grace)
        except Exception:
            log.exception('Search index janitor failed')
            record_job_run('search_index_janitor', started, datetime.utcnow(), JOB_RUN_FAILED)
            return
        if not outcome['results']:
            # Nothing due, or admission refused it - rebuild_search_indexes already logged
            # which. Neither is a run worth timing.
            return
        succeeded = all(outcome['results'].values())
        record_job_run('search_index_janitor', started, datetime.utcnow(),
                       JOB_RUN_SUCCESS if succeeded else JOB_RUN_FAILED)


_STORAGE_DIRS_INTERVAL_MINUTES = 5


def schedule_storage_dirs_check(app):
    """Register the storage-directory sweep, first tick shortly after startup.

    Unlike the janitor it is deliberately re-armed at every start rather than left on its
    old interval: a folder the process cannot write to is most often a fresh deploy's
    ownership mistake, and the alert is worth most in the first minute, not five minutes
    in. Off the startup thread because a stale network mount can hang a stat() for its
    whole timeout (dev/changelog/1009)."""
    _add_job(
        func=_storage_dirs_job,
        trigger='interval',
        minutes=_STORAGE_DIRS_INTERVAL_MINUTES,
        next_run_time=datetime.utcnow() + timedelta(seconds=10),
        id='storage_dirs_check',
        replace_existing=True,
    )
    log.info('Storage directory check scheduled every %d minutes',
             _STORAGE_DIRS_INTERVAL_MINUTES)


def _storage_dirs_job():
    """Probe every configured write directory and raise or clear its standing alert
    (app/storage_dirs.py). Without it, a folder only the DVR disk readout does not cover
    stayed broken with nothing but one startup log line to say so.

    Records no JobRun: every tick is a handful of stat() calls, and timing them would tell
    the /jobs page nothing."""
    with _app.app_context():
        from .config import load_config
        from .storage_dirs import sweep_write_dirs
        try:
            sweep_write_dirs(load_config(), _app.config.get('CAPTURE_LOG_DIR'))
        except Exception:
            log.exception('Storage directory check failed')


_ACCOUNT_STATS_FOLD_INTERVAL_MINUTES = 60


def schedule_account_stats_fold(app):
    """Register the hourly account stats ledger fold (app/account_stats.py).

    The Accounts pages fold on every load, so this exists for the source rows nobody looks
    at before they are gone: a recording deleted, or a channel test pruned, before anyone
    opened an Accounts page would otherwise never reach the ledger at all. A row can still
    be lost inside the hour between finishing and this tick - the ledger's docstring and
    dev/changelog/1028 say so rather than hooking every delete path."""
    _add_job(
        func=_account_stats_fold_job,
        trigger='interval',
        minutes=_ACCOUNT_STATS_FOLD_INTERVAL_MINUTES,
        id='account_stats_fold',
        replace_existing=True,
    )
    log.info('Account stats ledger fold scheduled every %d minutes',
             _ACCOUNT_STATS_FOLD_INTERVAL_MINUTES)


def _account_stats_fold_job():
    """Fold new segments, tests and failover events into the account stats ledger.

    Through ensure_fresh(), so a large backlog goes to the admitted background catch-up
    rather than holding a scheduler thread, and a refusal is logged by name. Records no
    JobRun: a tick is a few indexed counts and usually folds nothing."""
    with _app.app_context():
        from .account_stats import ensure_fresh
        try:
            notice = ensure_fresh(_app)
        except Exception:
            log.exception('Account stats ledger fold failed')
            return
        if notice:
            log.info('Account stats ledger: %s', notice['text'])


def get_scheduler() -> BackgroundScheduler:
    """Return the running APScheduler instance."""
    return _scheduler


def skip_next_run(job_id: str):
    """Advance a recurring job's next_run_time to the following occurrence.

    Works for both IntervalTrigger and CronTrigger since both implement
    get_next_fire_time(previous_fire_time, now) on APScheduler's BaseTrigger.
    Returns the new next_run_time (tz-aware) or None if the job doesn't exist
    or has no next fire time.
    """
    job = _scheduler.get_job(job_id)
    if job is None or job.next_run_time is None:
        return None
    new_next = job.trigger.get_next_fire_time(job.next_run_time, job.next_run_time)
    if new_next is None:
        return None
    _scheduler.modify_job(job_id, next_run_time=new_next)
    log.info('Skipped next run of %s - new next run: %s', job_id, new_next)
    return new_next


def _emit_job_skipped(job_id: str, job_label: str, body: str, recording_id: int = None,
                      account_id: int = None):
    """Record that a scheduled job was skipped, on the thing it concerns.

    An account sync (`account_id` given) writes a SKIPPED row to that account's own sync
    history, which the account page renders - so a sync that yielded says so where somebody
    looking at that account will see it. A maintenance job has no such object, and is
    surfaced instead by the retry this caller queues, which /jobs names and dates
    (dev/changelog/928). Either way the skip is logged by the caller.

    Does NOT touch next_run_time: by the time a job's own callback runs,
    APScheduler has already advanced next_run_time to the next occurrence
    (it does so when dispatching the job to the executor, before the job
    function body executes) - simply returning early is enough to "skip"
    the current occurrence. Calling skip_next_run() here would advance an
    already-advanced time and skip an extra occurrence.
    """
    if account_id is None:
        return
    from .accounts import record_skipped_sync
    record_skipped_sync(account_id, body)



