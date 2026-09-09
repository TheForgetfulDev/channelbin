"""
Scheduled Jobs page and API.

GET /jobs              - HTML page listing all APScheduler jobs
GET /api/jobs/scheduled - JSON list of all jobs with metadata
"""
import logging
import os
import re
import subprocess
import threading
from datetime import datetime, timedelta

from flask import Blueprint, render_template, jsonify, current_app, request

log = logging.getLogger(__name__)

from ..tz_utils import to_naive_utc, is_24h, format_local, relative

jobs_bp = Blueprint('jobs', __name__)

# Recurring jobs (other than the account_sync_<id> and od_job_<id> families, which are
# matched and handled separately) that run_job_now() actually implements. Anything not in
# this set gets a greyed-out, still-clickable Run Now item explaining why - see
# _RUN_NOW_REASONS in _build_job_list() - instead of a button that promises an action the
# backend will 400 on (dev/changelog/613).
_RUN_NOW_SUPPORTED_JOBS = frozenset({'config_backup_daily'})


def _fmt_et(dt_utc: datetime) -> str:
    return format_local(dt_utc, 'day_datetime')


def _relative(dt_utc: datetime) -> str:
    return relative(dt_utc, style='long')


def _trigger_description(job) -> str:
    """Return a human-readable schedule description from an APScheduler trigger."""
    t = job.trigger
    tname = type(t).__name__

    if tname == 'IntervalTrigger':
        # APScheduler 3.x stores interval as a timedelta on .interval
        td = getattr(t, 'interval', None)
        if td is None:
            return 'interval'
        secs = int(td.total_seconds())
        if secs % 3600 == 0:
            h = secs // 3600
            return f'every {h} hour{"s" if h != 1 else ""}'
        if secs % 60 == 0:
            m = secs // 60
            return f'every {m} minute{"s" if m != 1 else ""}'
        return f'every {secs}s'

    if tname == 'CronTrigger':
        # Inspect the fields list to build a description
        fields = {f.name: str(f) for f in t.fields}
        hour = fields.get('hour', '*')
        minute = fields.get('minute', '0')
        dow = fields.get('day_of_week', '*')

        try:
            h = int(hour)
            m = int(minute)
            if is_24h():
                time_str = f'{h:02d}:{m:02d}'
            else:
                suffix = 'AM' if h < 12 else 'PM'
                disp_h = h % 12 or 12
                time_str = f'{disp_h}:{m:02d} {suffix}'
        except (ValueError, TypeError):
            time_str = f'{hour}:{minute}'

        _DOW_NAMES = {
            'mon': 'Mondays', 'tue': 'Tuesdays', 'wed': 'Wednesdays',
            'thu': 'Thursdays', 'fri': 'Fridays', 'sat': 'Saturdays', 'sun': 'Sundays',
        }
        if dow == '*' or dow == '0-6':
            return f'daily at {time_str}'
        return f'{_DOW_NAMES.get(dow, dow)} at {time_str}'

    if tname == 'DateTrigger':
        return None  # one-off - no schedule description

    return tname


# Job kinds this page has run-history for (dev/changelog/592) - hc_window_dispatch/
# hc_window_close are deliberately excluded, matching the approved dev/mockups/29 rail
# contract, which doesn't track them either.
_DURATION_TRACKED_SYSTEM_JOBS = {
    'config_backup_daily', 'recording_retention_daily', 'db_maintenance_daily',
    'logo_cache_fetch', 'search_index_janitor',
}


def _job_duration_info(job_id: str, account_id: int = None):
    """(avg_seconds, runs, duration_line) for a _build_job_list item - avg_seconds/runs
    None/0 and duration_line None when this job kind has no run history tracked at all
    (an untracked kind, distinct from a tracked kind with zero completed runs yet, which
    returns a real "Expected runtime unknown" line via fmt_job_duration_line)."""
    from ..fmt_utils import fmt_job_duration_line

    if account_id is not None:
        from ..accounts import get_sync_duration_estimate
        avg_seconds, runs = get_sync_duration_estimate(account_id)
    elif job_id in _DURATION_TRACKED_SYSTEM_JOBS:
        from ..database import get_job_duration_estimate
        avg_seconds, runs = get_job_duration_estimate(job_id)
    else:
        return None, 0, None
    return avg_seconds, runs, fmt_job_duration_line(avg_seconds, runs)


_OVERLAP_WARN_SECONDS = 300  # 5 minutes


def _intervals_overlap(a_start, a_end, b_start, b_end) -> bool:
    """Return True if [a_start, a_end) and [b_start, b_end) overlap."""
    return a_start < b_end and b_start < a_end


def _interval_gap_seconds(a_start, a_end, b_start, b_end) -> float:
    """Return gap in seconds between non-overlapping intervals (0 if they overlap)."""
    if _intervals_overlap(a_start, a_end, b_start, b_end):
        return 0.0
    if a_end <= b_start:
        return (b_start - a_end).total_seconds()
    return (a_start - b_end).total_seconds()


def _build_job_list():
    """Return a list of job dicts for the jobs page/API."""
    from ..scheduler import get_scheduler
    from ..database import (
        Recording, OnDemandTestJob, Account,
        REC_STATUS_SCHEDULED, REC_STATUS_IN_PROGRESS, REC_STATUS_PAUSED, REC_STATUS_RETRYING,
    )

    scheduler = get_scheduler()
    if scheduler is None:
        return []

    raw_jobs = scheduler.get_jobs()
    now = datetime.utcnow()

    # Separate recording start/stop jobs from others
    start_ids: dict[int, object] = {}  # recording_id → job
    stop_ids: dict[int, object] = {}
    other_jobs: list = []

    for job in raw_jobs:
        m = re.match(r'^start_(\d+)$', job.id)
        if m:
            start_ids[int(m.group(1))] = job
            continue
        m = re.match(r'^stop_(\d+)$', job.id)
        if m:
            stop_ids[int(m.group(1))] = job
            continue
        other_jobs.append(job)

    # Batch-fetch every row the loops below need, keyed by id - CLAUDE.md's no-hidden-I/O
    # rule. Resolving these per job made the page cost one SELECT per scheduled recording
    # and one per account sync job (dev/docs/BUGS.md 2026-08-18). A missing id is a normal
    # state here, not an error: an APScheduler job can outlive the row it names, which is
    # exactly what the `Recording #<id>` / `Account <id>` fallback labels below are for.
    rec_ids = set(start_ids) | set(stop_ids)
    recordings = ({r.id: r for r in Recording.query.filter(Recording.id.in_(rec_ids)).all()}
                  if rec_ids else {})
    account_ids = {int(m.group(1)) for m in
                   (re.match(r'^account_sync(?:_retry)?_(\d+)$', j.id) for j in other_jobs)
                   if m}
    accounts = ({a.id: a for a in Account.query.filter(Account.id.in_(account_ids)).all()}
                if account_ids else {})
    od_ids = {int(m.group(1)) for m in
              (re.match(r'^od_job_(\d+)$', j.id) for j in other_jobs) if m}
    od_jobs = ({o.id: o for o in
                OnDemandTestJob.query.filter(OnDemandTestJob.id.in_(od_ids)).all()}
               if od_ids else {})

    items = []

    # Recording rows - scheduled (has start job)
    for rec_id, start_job in start_ids.items():
        stop_job = stop_ids.get(rec_id)
        next_run = start_job.next_run_time
        if next_run is None:
            continue
        next_run_utc = to_naive_utc(next_run)

        stop_run_et = None
        stop_run_utc = None
        if stop_job and stop_job.next_run_time:
            stop_run_utc = to_naive_utc(stop_job.next_run_time)
            stop_run_et = _fmt_et(stop_run_utc)

        rec = recordings.get(rec_id)
        if rec and rec.status != REC_STATUS_SCHEDULED:
            continue  # orphaned start job for a recording that already progressed/finished
        name = rec.name if rec else f'Recording #{rec_id}'
        edit_url = f'/recordings/{rec_id}'

        items.append({
            '_start_utc': next_run_utc,
            '_end_utc': stop_run_utc or next_run_utc,
            '_next_run_utc': next_run_utc,
            'id': f'start_{rec_id}',
            'display_name': f'Recording: {name}',
            'type': 'one_off',
            'next_run_utc': next_run_utc.isoformat(),
            'next_run_et': _fmt_et(next_run_utc),
            'next_run_relative': _relative(next_run_utc),
            'stop_run_et': stop_run_et,
            'schedule_description': None,
            'edit_url': edit_url,
            'overlap': 'green',
        })

    # Recording rows - active/paused (only stop job remains; start already fired)
    for rec_id, stop_job in stop_ids.items():
        if rec_id in start_ids:
            continue  # already handled above
        next_run = stop_job.next_run_time
        if next_run is None:
            continue
        stop_run_utc = to_naive_utc(next_run)

        rec = recordings.get(rec_id)
        if rec and rec.status not in (REC_STATUS_IN_PROGRESS, REC_STATUS_PAUSED, REC_STATUS_RETRYING):
            continue  # orphaned stop job for a finished/aborted recording
        name = rec.name if rec else f'Recording #{rec_id}'
        started = (rec.started_at or rec.start_time) if rec else now

        items.append({
            '_start_utc': started,
            '_end_utc': stop_run_utc,
            '_next_run_utc': started,
            'id': f'active_{rec_id}',
            'display_name': f'Recording: {name}',
            'type': 'active',
            'next_run_utc': started.isoformat(),
            'next_run_et': _fmt_et(started),
            'next_run_relative': 'in progress',
            'stop_run_et': _fmt_et(stop_run_utc),
            'schedule_description': None,
            'edit_url': f'/recordings/{rec_id}',
            'overlap': 'green',
        })

    # On-demand test job rows
    for job in other_jobs[:]:
        m = re.match(r'^od_job_(\d+)$', job.id)
        if not m:
            continue
        other_jobs.remove(job)
        job_id = int(m.group(1))
        next_run = job.next_run_time
        if next_run is None:
            continue
        next_run_utc = to_naive_utc(next_run)

        od = od_jobs.get(job_id)
        if od and od.status != 'SCHEDULED':
            continue  # orphaned APScheduler job for a job that already finished
        label = f'Health check #{job_id}'
        if od and od.name:
            label = f'Health check: {od.name}'
        is_recurring = bool(od and od.recurring)

        od_item = {
            '_start_utc': next_run_utc,
            '_end_utc': next_run_utc,
            '_next_run_utc': next_run_utc,
            'id': job.id,
            'display_name': label,
            'type': 'recurring' if is_recurring else 'one_off',
            'next_run_utc': next_run_utc.isoformat(),
            'next_run_et': _fmt_et(next_run_utc),
            'next_run_relative': _relative(next_run_utc),
            'stop_run_et': None,
            'schedule_description': _trigger_description(job) if is_recurring else None,
            'edit_url': f'/channels/health-checks/{job_id}',
            'overlap': 'green',
        }
        if is_recurring:
            od_item['skip_url'] = f'/api/channel-tests/on-demand/{job_id}/skip-next'
        else:
            od_item['run_url'] = f'/api/channel-tests/on-demand/{job_id}/start'
            od_item['run_keep_schedule_prompt'] = True
            od_item['cancel_url'] = f'/api/channel-tests/on-demand/{job_id}/unschedule'
        items.append(od_item)

    # Recurring maintenance-window checks (recur_use_window=True) have no APScheduler job
    # at all - hc_window_dispatch owns starting them (app/check_window.py) - so they never
    # match the od_job_<id> regex above and would otherwise vanish from this page entirely.
    from ..config import load_config
    from ..scheduler import next_on_demand_run_for_job
    from .channel_tests import _recur_label

    ct_cfg = load_config().get('channel_testing', {})
    # recur_paused=False matches the exact-time branch above: pausing a schedule calls
    # cancel_on_demand_job_schedule(), which removes the APScheduler row and so drops it
    # off this page too - a paused window job (no row to begin with) gets the same result
    # by being excluded here instead.
    window_jobs = OnDemandTestJob.query.filter_by(
        recurring=True, recur_use_window=True, status='SCHEDULED', recur_paused=False).all()
    for od in window_jobs:
        next_run_utc = next_on_demand_run_for_job(od, ct_cfg)
        if next_run_utc is None:
            continue
        label = f'Health check: {od.name}' if od.name else f'Health check #{od.id}'
        items.append({
            '_start_utc': next_run_utc,
            '_end_utc': next_run_utc,
            '_next_run_utc': next_run_utc,
            'id': f'od_window_{od.id}',
            'display_name': label,
            'type': 'recurring',
            'next_run_utc': next_run_utc.isoformat(),
            'next_run_et': _fmt_et(next_run_utc),
            'next_run_relative': _relative(next_run_utc),
            'stop_run_et': None,
            'schedule_description': _recur_label(od, ct_cfg),
            'edit_url': f'/channels/health-checks/{od.id}',
            'skip_url': f'/api/channel-tests/on-demand/{od.id}/skip-next',
            'overlap': 'green',
        })

    # Known recurring jobs (non-sync)
    _RECURRING_META = {
        'config_backup_daily': ('Config Backup', '/settings?q=config_backup'),
        'recording_retention_daily': ('Recording Retention', '/settings?q=retention_days'),
        'db_maintenance_daily': ('Database Maintenance', '/settings?q=keep_days'),
        'hc_window_dispatch': ('Maintenance Window Dispatch', '/settings?q=channel_testing.window'),
        'hc_window_close': ('Maintenance Window Close', '/settings?q=channel_testing.window'),
        'logo_cache_fetch': ('Logo Cache Fetch', '/settings?q=logo_cache'),
        'search_index_janitor': ('Search Index Janitor',
                                 '/settings?q=index_janitor_grace_minutes'),
    }

    # Explains, per job, what it does and why Run Now isn't offered - shown in the greyed
    # Run Now item's toast for anything not in _RUN_NOW_SUPPORTED_JOBS. dispatch_minutes is
    # read from the same ct_cfg already loaded above rather than a second load_config() call.
    dispatch_minutes = ct_cfg.get('window', {}).get('dispatch_interval_minutes', 5)
    _RUN_NOW_REASONS = {
        'hc_window_dispatch': (
            'Maintenance Window Dispatch starts the next due health check inside your '
            f'configured maintenance window, checking every {dispatch_minutes} minute'
            f'{"s" if dispatch_minutes != 1 else ""} '
            '(channel_testing.window.dispatch_interval_minutes). Running it on demand is '
            'not supported yet.'
        ),
        'hc_window_close': (
            'Maintenance Window Close hard-stops any health checks still running when the '
            'maintenance window ends, and reports what was left over. Running it on demand '
            'is not supported yet.'
        ),
        'recording_retention_daily': (
            'Recording Retention deletes recordings (and, if enabled, their files) past '
            'their configured retention window. Running it on demand is not supported yet.'
        ),
        'db_maintenance_daily': (
            'Database Maintenance prunes dismissed alerts and old EPG entries, and checks '
            'the write-ahead log. Running it on demand is not supported yet.'
        ),
        'logo_cache_fetch': (
            'Logo Cache Fetch refreshes cached channel logos in small batches. Running it '
            'on demand is not supported yet.'
        ),
        'search_index_janitor': (
            'Search Index Janitor rebuilds a search index that has been unusable for longer '
            'than its grace window with nothing else repairing it. To rebuild right now, use '
            'Maintenance -> Search index -> Rebuild now.'
        ),
    }

    for job in other_jobs:
        next_run = job.next_run_time
        if next_run is None:
            continue
        next_run_utc = to_naive_utc(next_run)

        # Per-account sync jobs: account_sync_<id>
        m = re.match(r'^account_sync_(\d+)$', job.id)
        if m:
            account_id = int(m.group(1))
            account = accounts.get(account_id)
            account_name = account.name if account else f'Account {account_id}'
            td = getattr(job.trigger, 'interval', None)
            hours = int(td.total_seconds() / 3600) if td else '?'
            avg_seconds, runs, duration_line = _job_duration_info(job.id, account_id=account_id)
            items.append({
                '_start_utc': next_run_utc,
                '_end_utc': next_run_utc + timedelta(seconds=avg_seconds) if avg_seconds else next_run_utc,
                '_next_run_utc': next_run_utc,
                'id': job.id,
                'display_name': f'Sync: {account_name}',
                'type': 'recurring',
                'next_run_utc': next_run_utc.isoformat(),
                'next_run_et': _fmt_et(next_run_utc),
                'next_run_relative': _relative(next_run_utc),
                'stop_run_et': None,
                'schedule_description': f'every {hours} hour{"s" if hours != 1 else ""}',
                'edit_url': f'/accounts/{account_id}/edit',
                'run_url': f'/api/jobs/{job.id}/run-now',
                'skip_url': f'/api/jobs/{job.id}/skip-next',
                'overlap': 'green',
                'runs': runs,
                'duration_line': duration_line,
            })
            continue

        # One-shot deferred-sync retries: account_sync_retry_<id>. Own branch because
        # they don't match the account_sync_<id> regex above and would otherwise render
        # as a raw job id, with a Run Now button that run_job_now() answers 400 to.
        m = re.match(r'^account_sync_retry_(\d+)$', job.id)
        if m:
            account_id = int(m.group(1))
            account = accounts.get(account_id)
            account_name = account.name if account else f'Account {account_id}'
            avg_seconds, runs, duration_line = _job_duration_info(job.id, account_id=account_id)
            items.append({
                '_start_utc': next_run_utc,
                '_end_utc': next_run_utc + timedelta(seconds=avg_seconds) if avg_seconds else next_run_utc,
                '_next_run_utc': next_run_utc,
                'id': job.id,
                'display_name': f'Sync retry: {account_name}',
                'type': 'one_off',
                'next_run_utc': next_run_utc.isoformat(),
                'next_run_et': _fmt_et(next_run_utc),
                'next_run_relative': _relative(next_run_utc),
                'stop_run_et': None,
                'schedule_description': 'deferred past a channel test run or another sync',
                'edit_url': f'/accounts/{account_id}/edit',
                'run_url': None,
                'skip_url': None,
                'overlap': 'green',
                'runs': runs,
                'duration_line': duration_line,
            })
            continue

        # One-shot deferred-maintenance retries: <base>_retry, queued when admission
        # refused a maintenance job (app/scheduler.py::_defer_job_for_contention). Same
        # shape as the sync retry above, named off the base job so the two read as a pair
        # rather than as an unexplained raw job id.
        if job.id.endswith('_retry') and job.id[:-len('_retry')] in _RECURRING_META:
            base_name, edit_url = _RECURRING_META[job.id[:-len('_retry')]]
            avg_seconds, runs, duration_line = _job_duration_info(job.id)
            items.append({
                '_start_utc': next_run_utc,
                '_end_utc': next_run_utc + timedelta(seconds=avg_seconds) if avg_seconds else next_run_utc,
                '_next_run_utc': next_run_utc,
                'id': job.id,
                'display_name': f'{base_name} retry',
                'type': 'one_off',
                'next_run_utc': next_run_utc.isoformat(),
                'next_run_et': _fmt_et(next_run_utc),
                'next_run_relative': _relative(next_run_utc),
                'stop_run_et': None,
                'schedule_description': 'deferred past heavier database work',
                'edit_url': edit_url,
                'run_url': None,
                'skip_url': None,
                'overlap': 'green',
                'runs': runs,
                'duration_line': duration_line,
            })
            continue

        display_name, edit_url = _RECURRING_META.get(job.id, (job.id, None))
        avg_seconds, runs, duration_line = _job_duration_info(job.id)
        item = {
            '_start_utc': next_run_utc,
            '_end_utc': next_run_utc + timedelta(seconds=avg_seconds) if avg_seconds else next_run_utc,
            '_next_run_utc': next_run_utc,
            'id': job.id,
            'display_name': display_name,
            'type': 'recurring',
            'next_run_utc': next_run_utc.isoformat(),
            'next_run_et': _fmt_et(next_run_utc),
            'next_run_relative': _relative(next_run_utc),
            'stop_run_et': None,
            'schedule_description': _trigger_description(job),
            'edit_url': edit_url,
            'skip_url': f'/api/jobs/{job.id}/skip-next',
            'overlap': 'green',
            'runs': runs,
            'duration_line': duration_line,
        }
        if job.id in _RUN_NOW_SUPPORTED_JOBS:
            item['run_url'] = f'/api/jobs/{job.id}/run-now'
        else:
            item['run_disabled_reason'] = _RUN_NOW_REASONS.get(
                job.id, f'Running "{display_name}" on demand is not supported yet.')
        items.append(item)

    # Sort by _next_run_utc
    items.sort(key=lambda x: x['_next_run_utc'])

    # Overlap detection: interval-based
    for i, a in enumerate(items):
        for j, b in enumerate(items):
            if i == j:
                continue
            if _intervals_overlap(a['_start_utc'], a['_end_utc'], b['_start_utc'], b['_end_utc']):
                a['overlap'] = 'red'
            elif (a['overlap'] != 'red' and
                  _interval_gap_seconds(a['_start_utc'], a['_end_utc'],
                                        b['_start_utc'], b['_end_utc']) < _OVERLAP_WARN_SECONDS):
                a['overlap'] = 'yellow'

    # Strip internal keys before returning
    for item in items:
        del item['_start_utc']
        del item['_end_utc']
        del item['_next_run_utc']

    # Event-triggered jobs (not APScheduler-scheduled; run after each recording's pipeline)
    from ..config import load_config as _load_config
    _cfg = _load_config()
    _ps = _cfg['recording'].get('post_script', {})
    if _ps.get('enabled', True):
        items.append({
            'id': 'post_complete_script',
            'display_name': 'Post-completion script: ' + (os.path.basename(_ps.get('path', '')) or '(not set)'),
            'type': 'on_event',
            'next_run_utc': None,
            'next_run_et': '-',
            'next_run_relative': 'after each recording completes',
            'stop_run_et': None,
            'schedule_description': 'Runs after each recording finishes post-processing',
            'edit_url': '/settings?q=post_script',
            'run_url': '/api/jobs/run-post-script',
            'overlap': 'green',
        })

    return items


@jobs_bp.route('/jobs')
def jobs_page():
    from .. import db
    with db.engine.connect():
        job_list = _build_job_list()
    return render_template('jobs.html', jobs=job_list)


@jobs_bp.route('/api/jobs/scheduled')
def jobs_api():
    from .. import db
    with db.engine.connect():
        job_list = _build_job_list()
    return jsonify(job_list)


@jobs_bp.route('/api/jobs/run-post-script', methods=['POST'])
def run_post_script():
    from ..config import load_config
    cfg = load_config()
    ps = cfg['recording'].get('post_script', {})

    if not ps.get('enabled', True):
        return jsonify({'error': 'Post-completion script is disabled'}), 400

    path = ps.get('path', '').strip()
    if not path:
        return jsonify({'error': 'No script path configured'}), 400

    timeout = ps.get('timeout_seconds', 300)
    cwd = os.path.dirname(os.path.abspath(path)) or None

    def _run():
        log.info('Manual post-script run: %s (cwd=%s)', path, cwd)
        try:
            result = subprocess.run([path], cwd=cwd, capture_output=True, timeout=timeout)
            if result.returncode == 0:
                log.info('Manual post-script succeeded: %s', path)
            else:
                stderr = result.stderr.decode(errors='replace').strip()[-500:]
                log.error('Manual post-script failed (exit %d): %s - %s', result.returncode, path, stderr)
        except subprocess.TimeoutExpired:
            log.error('Manual post-script timed out after %ds: %s', timeout, path)
        except Exception as exc:
            log.exception('Manual post-script error (%s): %s', path, exc)

    thread = threading.Thread(target=_run, daemon=True, name='manual_post_script')
    thread.start()
    return jsonify({'success': True, 'message': f'Script started: {path}'})


@jobs_bp.route('/api/jobs/<job_id>/skip-next', methods=['POST'])
def skip_next(job_id):
    from ..scheduler import get_scheduler, skip_next_run

    scheduler = get_scheduler()
    if scheduler is None or scheduler.get_job(job_id) is None:
        return jsonify({'error': 'Job not found'}), 404

    new_next = skip_next_run(job_id)
    if new_next is None:
        return jsonify({'error': 'Could not determine next run time'}), 400

    new_next_utc = to_naive_utc(new_next)
    return jsonify({
        'success': True,
        'next_run_et': _fmt_et(new_next_utc),
        'next_run_relative': _relative(new_next_utc),
    })


@jobs_bp.route('/api/jobs/<job_id>/run-now', methods=['POST'])
def run_job_now(job_id):
    from ..scheduler import get_scheduler

    scheduler = get_scheduler()
    if scheduler is None or scheduler.get_job(job_id) is None:
        return jsonify({'error': 'Job not found'}), 404

    app_obj = current_app._get_current_object()

    m = re.match(r'^account_sync_(\d+)$', job_id)
    if m:
        from ..database import Account
        from .. import db
        from ..accounts import sync_account, sync_conflicts

        account_id = int(m.group(1))
        account = db.session.get(Account, account_id)
        if account is None:
            return jsonify({'error': 'Account not found'}), 404

        # Same shared check as the accounts page (DESIGN-concurrency.md 5.4) - refuse with
        # the reasons, and let an explicit force=true in the body override.
        body = request.get_json(silent=True) or {}
        if not body.get('force'):
            conflicts = sync_conflicts(account_id)
            if conflicts:
                return jsonify({
                    'error': f'Sync not started for "{account.name}": ' + ' '.join(conflicts),
                    'conflicts': conflicts,
                }), 409

        threading.Thread(
            target=sync_account, args=(app_obj, account_id),
            # See _start_sync in routes/accounts.py - manual sync is forced past admission
            # but still registers.
            kwargs={'force_admission': True},
            daemon=True, name=f'account-sync-{account_id}',
        ).start()
        return jsonify({'success': True, 'message': f'Sync started for "{account.name}"'})

    if job_id == 'config_backup_daily':
        from ..config_backup import do_backup

        try:
            do_backup()
            return jsonify({'success': True, 'message': 'Backup completed'})
        except Exception as exc:
            return jsonify({'error': str(exc)}), 500

    return jsonify({'error': 'Run Now is not supported for this job'}), 400
