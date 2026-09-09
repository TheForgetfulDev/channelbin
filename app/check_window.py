"""Maintenance window for recurring health checks (dev/changelog/497).

A recurring OnDemandTestJob with recur_use_window=True has no APScheduler CronTrigger of
its own - it is dispatcher-owned. hc_window_dispatch (an interval job, see app/scheduler.py)
calls dispatch_tick() every channel_testing.window.dispatch_interval_minutes to start the
next due window job, one at a time, so window checks can never collide with each other.
hc_window_close (a cron job firing at the window's end time) calls window_close() to hard-stop
whatever is still running and report any leftover work.

Everything window-shaped lives here so there is exactly one home for the arithmetic - see
CLAUDE.md's Coding Standards canonical-homes table.
"""
import logging
from datetime import datetime, time, timedelta

from .tz_utils import to_local, to_naive_utc, parse_hhmm, format_clock

log = logging.getLogger(__name__)

_DEFAULT_START = '02:00'
_DEFAULT_END = '06:00'


def window_bounds(ct_cfg: dict):
    """(start, end) as datetime.time, from ct_cfg['window']['start'/'end']. Garbage config
    falls back to the defaults and logs a warning - this runs inside a scheduler job and
    must never raise."""
    window_cfg = (ct_cfg or {}).get('window', {})
    start_str = window_cfg.get('start', _DEFAULT_START)
    end_str = window_cfg.get('end', _DEFAULT_END)
    try:
        start_h, start_m = parse_hhmm(start_str)
        end_h, end_m = parse_hhmm(end_str)
    except ValueError:
        log.warning('Invalid channel_testing.window start/end (%r/%r) - falling back to %s-%s',
                    start_str, end_str, _DEFAULT_START, _DEFAULT_END)
        start_h, start_m = parse_hhmm(_DEFAULT_START)
        end_h, end_m = parse_hhmm(_DEFAULT_END)
    return time(start_h, start_m), time(end_h, end_m)


def format_window_label(ct_cfg: dict) -> str:
    """The window's bounds as one display string ("2:00 AM-6:00 AM").

    Every surface that names the window renders it identically, so the f-string lives
    here rather than at each caller - it had been written out four separate times before
    a fifth page needed it (dev/changelog/830).
    """
    start_t, end_t = window_bounds(ct_cfg)
    return (f'{format_clock(start_t.hour, start_t.minute)}-'
            f'{format_clock(end_t.hour, end_t.minute)}')


def _window_length_seconds(start_t: time, end_t: time) -> int:
    start_min = start_t.hour * 60 + start_t.minute
    end_min = end_t.hour * 60 + end_t.minute
    if end_min > start_min:
        return (end_min - start_min) * 60
    return (24 * 60 - start_min + end_min) * 60


def _recur_day_for_date(d) -> int:
    """The channel_testing.test_days / OnDemandTestJob.recur_day encoding (1=Sun...7=Sat)
    for a date - the inverse of scheduler.py's _DAY_MAP. date.isoweekday() is 1=Mon...7=Sun."""
    return d.isoweekday() % 7 + 1


def occurrence_containing(ct_cfg: dict, now_utc: datetime):
    """(start_utc, end_utc) of the window occurrence containing now_utc, or None. end<=start
    means the window crosses midnight, in which case the occurrence spans two calendar
    dates (belongs to the day it opened - see due_jobs())."""
    start_t, end_t = window_bounds(ct_cfg)
    local_now = to_local(now_utc)
    today = local_now.date()
    now_time = local_now.time()

    if end_t > start_t:
        if start_t <= now_time < end_t:
            return (to_naive_utc(datetime.combine(today, start_t)),
                    to_naive_utc(datetime.combine(today, end_t)))
        return None

    # Crosses midnight: either the tail of yesterday's occurrence, or the start of
    # tonight's.
    if now_time < end_t:
        return (to_naive_utc(datetime.combine(today - timedelta(days=1), start_t)),
                to_naive_utc(datetime.combine(today, end_t)))
    if now_time >= start_t:
        return (to_naive_utc(datetime.combine(today, start_t)),
                to_naive_utc(datetime.combine(today + timedelta(days=1), end_t)))
    return None


def _occurrence_bounds_for_closing(ct_cfg: dict, now_utc: datetime):
    """(start_utc, end_utc) of the occurrence that is closing right now. window_close()
    runs on a cron trigger fired at the window's end time (display tz), so 'now' is
    (approximately) the occurrence's end moment - unlike occurrence_containing(), which
    answers "does now fall inside some occurrence" and would return None right at the
    boundary this function is designed to be called at."""
    start_t, end_t = window_bounds(ct_cfg)
    local_now = to_local(now_utc)
    end_date = local_now.date()
    start_date = end_date - timedelta(days=1) if end_t <= start_t else end_date
    return (to_naive_utc(datetime.combine(start_date, start_t)),
            to_naive_utc(datetime.combine(end_date, end_t)))


def next_occurrence_start(ct_cfg: dict, job, now_utc: datetime) -> datetime:
    """Naive UTC start of the next window occurrence eligible for `job` (honoring
    job.recur_day), strictly after now_utc. Written to scheduled_start_time so the
    existing list/sort surfaces keep working without an APScheduler job to ask."""
    start_t, _end_t = window_bounds(ct_cfg)
    recur_day = job.recur_day if job.recur_day is not None else 0
    local_now = to_local(now_utc)
    candidate_date = local_now.date()
    if local_now.time() >= start_t:
        candidate_date += timedelta(days=1)

    for _ in range(8):
        if recur_day == 0 or _recur_day_for_date(candidate_date) == recur_day:
            return to_naive_utc(datetime.combine(candidate_date, start_t))
        candidate_date += timedelta(days=1)
    # Unreachable (recur_day is always 0-7, so one of the 8 candidates always matches),
    # but this runs inside a scheduler job and must never raise.
    return to_naive_utc(datetime.combine(candidate_date, start_t))


def estimate_job_seconds(job, ct_cfg: dict) -> int:
    """n*duration + (n-1)*wait for testing job's group, honoring any profile override.
    duplicated from static/js/check-modal.js::ccRunSeconds - the modal needs a live
    estimate without a round trip."""
    from .channel_tester import resolve_health_check_settings
    from .channel_groups import check_run_channels

    if job.group is None:
        return 0
    n = len(check_run_channels(job.group))
    if not n:
        return 0
    settings = resolve_health_check_settings(ct_cfg, job.profile)
    duration = settings['test_duration_seconds']
    wait = settings['wait_between_channels_seconds']
    return n * duration + (n - 1) * wait


def _day_matches(job, day_num: int) -> bool:
    return job.recur_day in (0, None) or job.recur_day == day_num


def _window_eligible_for_day(occurrence_start_utc: datetime, statuses):
    """Window-dispatched recurring checks whose recur_day matches this occurrence's local
    day and are not paused, in `statuses`. Ordered (0 if is_system else 1, last_full_run_at
    ASC nulls first, id) - the system 'TV Guide Channels' check always first, then a
    self-stabilizing least-recently-fully-run rotation (see due_jobs() for the reasoning)."""
    from .database import OnDemandTestJob

    day_num = _recur_day_for_date(to_local(occurrence_start_utc).date())
    candidates = (
        OnDemandTestJob.query
        .filter(
            OnDemandTestJob.recurring.is_(True),
            OnDemandTestJob.recur_use_window.is_(True),
            OnDemandTestJob.status.in_(statuses),
            OnDemandTestJob.recur_paused.is_(False),
        )
        .order_by(OnDemandTestJob.is_system.desc(),
                  OnDemandTestJob.last_full_run_at.asc(),
                  OnDemandTestJob.id.asc())
        .all()
    )
    return [j for j in candidates if _day_matches(j, day_num)]


def due_jobs(occurrence_start_utc: datetime):
    """Ordered list of OnDemandTestJob eligible to start right now for the window
    occurrence beginning at occurrence_start_utc. Eligible iff: recurring, recur_use_window,
    status == SCHEDULED, not paused, recur_day matches the occurrence's local day (0 = every
    day), has not already run this occurrence (completed_at unset or before this occurrence
    started), and any "skip next run" window has passed."""
    now = datetime.utcnow()
    result = []
    for job in _window_eligible_for_day(occurrence_start_utc, ('SCHEDULED',)):
        if job.completed_at is not None and job.completed_at >= occurrence_start_utc:
            continue
        if job.window_skip_until is not None and job.window_skip_until > now:
            continue
        result.append(job)
    return result


def dispatch_tick(app):
    """hc_window_dispatch's interval-job body: start the next due window job, one at a
    time. Never passes force=True to run_on_demand_test_job - imminent_recording_conflict()
    must still apply, so a recording starting soon still wins."""
    with app.app_context():
        from .config import load_config
        from . import channel_tester

        if channel_tester.is_running():
            return

        ct_cfg = load_config().get('channel_testing', {})
        occ = occurrence_containing(ct_cfg, datetime.utcnow())
        if occ is None:
            return
        occurrence_start_utc, _occurrence_end_utc = occ

        jobs = due_jobs(occurrence_start_utc)
        if not jobs:
            return
        job_id = jobs[0].id

    import threading
    threading.Thread(
        target=channel_tester.run_on_demand_test_job,
        args=(app, job_id),
        daemon=True,
        name=f'od-window-job-{job_id}',
    ).start()


def window_close(app):
    """hc_window_close's cron-job body: stop whatever window job is still running and
    report leftover work in a WARNING alert. Silent when the window drained cleanly -
    a nightly INFO would be noise when every run is already visible on the check's own
    history and the jobs page."""
    with app.app_context():
        from . import db
        from .config import load_config
        from . import channel_tester
        from .database import OnDemandTestJob, Alert
        from .alerts import create_alert
        from .fmt_utils import fmt_duration_hm, fmt_duration_phrase

        ct_cfg = load_config().get('channel_testing', {})
        now_utc = datetime.utcnow()
        occurrence_start_utc, occurrence_end_utc = _occurrence_bounds_for_closing(ct_cfg, now_utc)

        status = channel_tester.get_status()
        stopped_job = None
        stopped_tested = stopped_total = 0
        if status.get('is_running') and status.get('run_kind') == 'job':
            running_job_id = status.get('current_job_id')
            running_job = db.session.get(OnDemandTestJob, running_job_id) if running_job_id else None
            if running_job is not None and running_job.recur_use_window:
                stopped_job = running_job
                stopped_tested = status.get('completed_channels', 0)
                stopped_total = status.get('total_channels', 0)
                channel_tester.request_stop()

        # due_jobs() naturally excludes the job just stopped (its status is still RUNNING
        # at this instant - its own finally block hasn't landed yet) and anything that
        # already fully completed tonight, leaving exactly "never started".
        never_started = due_jobs(occurrence_start_utc)

        if stopped_job is None and not never_started:
            return

        already = Alert.query.filter(
            Alert.alert_type == 'HEALTH_CHECK_WINDOW',
            Alert.created_at >= occurrence_start_utc,
        ).first()
        if already is not None:
            return

        # The reporting cohort ("due tonight") includes the job just stopped (RUNNING at
        # query time) and anything already completed, which due_jobs()'s SCHEDULED-only /
        # completed_at filtering deliberately excludes for its own (start-able-now) purpose.
        cohort = _window_eligible_for_day(occurrence_start_utc, ('SCHEDULED', 'RUNNING'))
        cohort_total = sum(estimate_job_seconds(j, ct_cfg) for j in cohort)

        window_label = format_window_label(ct_cfg)
        window_seconds = (occurrence_end_utc - occurrence_start_utc).total_seconds()

        lines = [
            f'Window: {window_label} ({fmt_duration_hm(window_seconds)}). '
            f'Due tonight: {len(cohort)} check{"" if len(cohort) == 1 else "s"}, '
            f'{fmt_duration_phrase(cohort_total)}.',
            '',
        ]
        if stopped_job is not None:
            lines.append('Stopped mid-run:')
            lines.append(f'  - {stopped_job.name}: tested {stopped_tested} of {stopped_total} channels')
            lines.append('')
        if never_started:
            lines.append('Never started:')
            for j in never_started:
                lines.append(f'  - {j.name} ({fmt_duration_phrase(estimate_job_seconds(j, ct_cfg))})')
            lines.append('')
        lines.append('To fix this, do one of:')
        lines.append('  - Widen the maintenance window (Settings > Channel Testing)')
        lines.append('  - Lower Test duration / Wait between channels, or use a faster health check profile')
        lines.append('  - Move one of these checks to a different day')

        create_alert(
            'HEALTH_CHECK_WINDOW',
            title='Maintenance window closed with work left over',
            body='\n'.join(lines).strip(),
            source='check_window',
        )


def window_plan(ct_cfg: dict) -> dict:
    """The capacity payload both item 3 surfaces (the create/edit check modal's
    over-capacity warning and the settings page's "is my window big enough" line) consume.

    'days' is keyed 0-7 in the same encoding as OnDemandTestJob.recur_day: 0 lists the
    every-day checks on their own (informational), and 1-7 each roll every-day checks into
    that weekday's total too, since an every-day check occupies every calendar day's
    capacity even though it is not filed under any single weekday key."""
    from sqlalchemy.orm import selectinload
    from .database import OnDemandTestJob, ChannelGroup

    start_t, end_t = window_bounds(ct_cfg)
    window_seconds = _window_length_seconds(start_t, end_t)

    jobs = (
        OnDemandTestJob.query
        .options(selectinload(OnDemandTestJob.group).selectinload(ChannelGroup.memberships))
        .filter(
            OnDemandTestJob.recurring.is_(True),
            OnDemandTestJob.recur_use_window.is_(True),
            OnDemandTestJob.recur_paused.is_(False),
        )
        .order_by(OnDemandTestJob.name)
        .all()
    )

    def entry(job):
        return {'id': job.id, 'name': job.name, 'seconds': estimate_job_seconds(job, ct_cfg)}

    every_day_entries = [entry(j) for j in jobs if not j.recur_day]
    days = {0: {'checks': every_day_entries,
                'total_seconds': sum(e['seconds'] for e in every_day_entries)}}
    for d in range(1, 8):
        day_entries = [entry(j) for j in jobs if j.recur_day == d] + every_day_entries
        days[d] = {'checks': day_entries, 'total_seconds': sum(e['seconds'] for e in day_entries)}

    return {'window_seconds': window_seconds, 'days': days}
