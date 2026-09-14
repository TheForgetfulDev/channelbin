import json
import logging
import os
import queue
from datetime import datetime, timedelta

from flask import Blueprint, render_template, Response, stream_with_context, jsonify, url_for

from sqlalchemy.orm import selectinload

from .. import db
from ..database import (
    Recording, Account,
    REC_STATUS_SCHEDULED, REC_STATUS_IN_PROGRESS, REC_STATUS_PAUSED, REC_STATUS_RETRYING,
    REC_STATUS_CONCATENATING, REC_STATUS_ANALYZING, REC_STATUS_CONVERTING,
    WINDOW_OPEN_STATUSES,
)
from .. import events as ev
from ..tz_utils import UTC, to_naive_utc, format_local, relative
from ..accounts import get_sync_progress, next_sync_map, sync_signature
from .channel_tests import get_active_run_summary
from ..fmt_utils import REC_STATUS_DISPLAY, rec_status_display

dashboard_bp = Blueprint('dashboard', __name__)
log = logging.getLogger(__name__)

# The dashboard's recording rows reuse the one status vocabulary rather than a second copy,
# so the two surfaces cannot silently color - or name - the same status two different ways
# (CLAUDE.md: "one flag, one meaning; states are enumerated"). The label half was left
# behind here for as long as the table existed, which is why the template printed the raw
# enum and the Dashboard said CONCATENATING where every other page said JOINING
# (dev/changelog/961).
REC_ROW_STATUS_CLASS = {status: row[1] for status, row in REC_STATUS_DISPLAY.items()}

# For dashboard.js, which relabels a badge from the status on an SSE frame. Rendered into
# the page as JSON rather than hand-written in the .js file, so there is one table, not two.
REC_ROW_STATUS_LABEL = {status: row[3] for status, row in REC_STATUS_DISPLAY.items()}


# ── DESIGN.md 16.1/16.2: the sections, their order, and their one jump-off ───
# The order and the enabled set are USER state, stored in one /api/user-prefs row and read
# back here so the first paint is already correct - JS may upgrade the server's markup, it
# must never be required to calm it down (CLAUDE.md, Frontend rendering).
#
# Each section carries exactly one action and it is a forward jump-off naming its page
# (16.2). A section with no entry renders no control, so a section added later cannot
# silently inherit somebody else's page.
DASHBOARD_SECTIONS_PREF = 'dashboard_sections'

DASHBOARD_SECTIONS = {
    'timeline': {'label': 'Timeline',
                 'hint': 'Recordings and scheduled jobs on one time axis.',
                 'to': 'Jobs', 'endpoint': 'jobs.jobs_page'},
    'live': {'label': 'Recordings in progress',
             'hint': 'Capturing or converting right now.',
             'to': 'Recordings', 'endpoint': 'recordings.index'},
    'upcoming': {'label': 'Upcoming recordings',
                 'hint': 'Scheduled, not started yet.',
                 'to': 'Recordings', 'endpoint': 'recordings.index'},
    'accounts': {'label': 'Account sync status',
                 'hint': 'Channel counts, last and next sync, sync errors.',
                 'to': 'Accounts', 'endpoint': 'accounts.accounts_list'},
    'health': {'label': 'Channel health check',
               'hint': 'Progress of a running health check run.',
               'to': 'Groups', 'endpoint': 'channel_groups.groups_page'},
}

# `upcoming` starts OFF because the timeline above it already answers "what is booked and
# when" - leaving both on ships the same recordings twice. One toggle in Customize brings
# it back. Everything else is on.
DASHBOARD_SECTION_ORDER = ['timeline', 'live', 'upcoming', 'accounts', 'health']
DASHBOARD_SECTION_ON = {'timeline': True, 'live': True, 'upcoming': False,
                        'accounts': True, 'health': True}


def _section_state():
    """(order, enabled_map) for the dashboard's sections, from the user's stored prefs.

    One /api/user-prefs row holds both. It is user-written JSON, so anything that is not the
    shape this expects falls back to the defaults rather than being allowed to 500 a page it
    only arranges. Unknown keys are dropped and missing ones appended in their default
    position, so adding a section later cannot strand a user on a stored order that predates
    it - the new section simply appears.
    """
    from ..database import UserPref

    pref = db.session.get(UserPref, DASHBOARD_SECTIONS_PREF)
    try:
        stored = json.loads(pref.value) if pref and pref.value else None
    except ValueError:
        log.warning('Stored dashboard section prefs are not valid JSON - using defaults.')
        stored = None
    if not isinstance(stored, dict):
        return list(DASHBOARD_SECTION_ORDER), dict(DASHBOARD_SECTION_ON)

    raw_order = stored.get('order')
    order = [k for k in raw_order if k in DASHBOARD_SECTIONS] if isinstance(raw_order, list) else []
    order += [k for k in DASHBOARD_SECTION_ORDER if k not in order]

    raw_on = stored.get('on')
    on = dict(DASHBOARD_SECTION_ON)
    if isinstance(raw_on, dict):
        for key in DASHBOARD_SECTIONS:
            if key in raw_on:
                on[key] = bool(raw_on[key])
    return order, on


def _converting_detail(r):
    """Dashboard bg-task detail for a CONVERTING row: enrich with the persisted progress
    snapshot when it's fresh (updated within ~30s), else just the name. Reads only fields
    already on the row - no extra query. All fields nullable-guarded (Jinja/py rule)."""
    name = r.name
    updated = r.conversion_updated_at
    if not updated or (datetime.utcnow() - updated).total_seconds() > 30:
        return name
    pct = r.conversion_progress_pct
    eta = r.conversion_eta_seconds
    bits = []
    if pct is not None:
        bits.append(f'{pct:.0f}%')
    if eta is not None:
        mins = eta // 60
        bits.append(f'~{mins} min left' if mins >= 1 else '~<1 min left')
    return f'{name} - {", ".join(bits)}' if bits else name


def _joining_detail(r):
    """Dashboard bg-task detail for a CONCATENATING row: the live join's own numbers when an
    ffmpeg is running, else just the name.

    The registry read is a dict lookup, not a query - the join's progress is in memory
    because a join always re-runs from the top rather than resuming (see
    concatenator._concat_progress). No entry means the row is queued behind another job,
    which is a real state and reads as the bare name rather than a fabricated 0%."""
    from ..concatenator import concat_progress
    prog = concat_progress(r.id)
    if prog is None:
        return r.name
    bits = []
    if prog['pct'] is not None:
        bits.append(f"{prog['pct']:.0f}%")
    bits.append(f"{prog['joined']} of {prog['of']} segments")
    return f'{r.name} - {", ".join(bits)}'


def _analyzing_detail(r):
    """Dashboard bg-task detail for an ANALYZING row: which whole-file read is running and
    how far through it is, else just the name.

    Same registry read and the same reasoning as _joining_detail above
    (postprocessor._analysis_progress). No entry means no pass is reading - between the two
    passes, or before the first one - which reads as the bare name rather than a 0% that
    describes nothing."""
    from ..postprocessor import analysis_progress
    prog = analysis_progress(r.id)
    if prog is None:
        return r.name
    label = prog['pass_label']
    if prog['of_passes'] > 1:
        label += f" ({prog['pass_number']} of {prog['of_passes']})"
    bits = [label]
    if prog['pct'] is not None:
        bits.append(f"{prog['pct']:.0f}%")
    return f'{r.name} - {", ".join(bits)}'


# ── DESIGN.md 16.1: the metric strip ────────────────────────────────────────
# The strip is why this page is not just a list. Every tile is DERIVED from state the page
# already holds - nothing here is hardcoded, and nothing is measured specially for it. That
# is deliberate and load-bearing: what the tiles show is expected to change once someone has
# lived with them, and it stays a one-function edit only while every value is derived.

# How far back "errors, last hour" looks, and how much of the log it is willing to read to
# answer. The cap is what keeps this a bounded read on a page with a scaling test: a busy
# hour can outrun it, in which case the tile undercounts rather than reading a 200MB file.
_ERROR_WINDOW = timedelta(hours=1)
_ERROR_SCAN_LINES = 4000


def _recent_error_counts(now):
    """(errors, criticals, lines_seen) in the last hour, from the tail of the log file.

    Returns (None, None, 0) when there is no readable log - the tile then says so rather
    than claiming zero errors, which is the one wrong answer here (a missing log is not a
    quiet hour).
    """
    from .logs import _log_file_path, _parse_lines, _tail_lines

    path = _log_file_path()
    if not path or not os.path.isfile(path):
        return None, None, 0
    try:
        records = _parse_lines(_tail_lines(path, _ERROR_SCAN_LINES))
    except OSError as exc:
        log.warning('Could not read the log file for the dashboard error tile: %s', exc)
        return None, None, 0

    cutoff = (now - _ERROR_WINDOW).strftime('%Y-%m-%d %H:%M:%S')
    errors = crits = seen = 0
    for rec in records:
        # String comparison, not parsing: the format is fixed-width and lexically ordered,
        # so this avoids building 4000 datetimes to answer one count. A record's ts carries
        # a ',mmm' the cutoff does not, which makes a record ON the cutoff second sort after
        # it - i.e. in-window, which is the right side to round toward.
        if rec['ts'] < cutoff:
            continue
        seen += 1
        if rec['level'] == 'CRITICAL':
            crits += 1
            errors += 1
        elif rec['level'] == 'ERROR':
            errors += 1
    return errors, crits, seen


def _metric_tiles(recordings, accounts, now):
    """The six tiles of DESIGN.md 16.1's metric strip, each derived from data already read.

    `cls` is one of '', 'ok', 'warn', 'bad' and drives the tile's left rule and value color.
    """
    from ..config import load_config
    from ..fmt_utils import fmt_bytes
    from .recordings import time_ago_filter
    from .system import DVR_DIR_ROLE, _disk_bytes

    capturing = [r for r in recordings if r.status == REC_STATUS_IN_PROGRESS]
    converting = [r for r in recordings
                  if r.status in (REC_STATUS_CONVERTING, REC_STATUS_CONCATENATING,
                                  REC_STATUS_ANALYZING)]
    scheduled = [r for r in recordings if r.status == REC_STATUS_SCHEDULED]

    dvr_dir = load_config()['recording']['dvr_output_dir']
    disk_total, disk_free = _disk_bytes(dvr_dir, DVR_DIR_ROLE)
    used_pct = round((disk_total - disk_free) / disk_total * 100) if disk_total else None

    err_accounts = [a for a in accounts if a.status == 'ERROR']
    synced = [a.last_sync_at for a in accounts if a.last_sync_at]
    oldest_sync = min(synced) if synced else None

    # logging's %(asctime)s writes SYSTEM-local wall clock, not UTC and not the configured
    # display timezone, so the cutoff is datetime.now() rather than utcnow().
    errors, crits, log_lines = _recent_error_counts(datetime.now())

    stalls = sum((r.total_stall_count or 0) for r in capturing)
    downtime = sum((r.total_downtime_seconds or 0) for r in capturing)
    nxt = scheduled[0] if scheduled else None

    return [
        {'key': 'Capturing now', 'value': str(len(capturing)),
         'sub': f'{len(converting)} converting' if converting else 'nothing converting',
         'cls': 'ok' if capturing else ''},
        {'key': 'Next recording',
         'value': relative(nxt.start_time, style='compact', now=now) if nxt else None,
         'sub': (nxt.channel.name if nxt and nxt.channel else nxt.name) if nxt else 'nothing scheduled',
         'cls': '', 'tip': nxt.name if nxt else None,
         # Deliberate exception to "never fill a tile with a per-record number" above -
         # this tile already names one specific recording, so linking it through is a
         # click-target fix (dev/changelog/666), not a new per-record fact.
         'href': url_for('recordings.recording_detail', recording_id=nxt.id) if nxt else None},
        {'key': 'Disk free', 'value': fmt_bytes(disk_free) if disk_free is not None else None,
         'sub': f'{used_pct}% used on {dvr_dir}' if used_pct is not None else f'{dvr_dir} is not mounted',
         'cls': '' if used_pct is None else 'bad' if used_pct >= 90 else 'warn' if used_pct >= 75 else 'ok'},
        {'key': 'Errors, last hour',
         'value': None if errors is None else str(errors),
         'sub': ('no readable log file' if errors is None
                 else f'{crits} critical' if crits else f'{log_lines} log lines'),
         'cls': '' if errors is None else 'bad' if crits else 'warn' if errors else 'ok'},
        {'key': 'Stalls, live', 'value': str(stalls),
         'sub': f'{int(downtime)}s lost' if downtime else 'no lost time',
         'cls': 'bad' if stalls > 5 else 'warn' if stalls else 'ok'},
        {'key': 'Accounts', 'value': f'{len(accounts) - len(err_accounts)} of {len(accounts)}',
         'sub': 'never synced' if oldest_sync is None else f'oldest sync {time_ago_filter(oldest_sync)}',
         'cls': 'bad' if err_accounts else 'ok'},
    ]


# ── DESIGN.md 16.3/16.4: what the timeline is drawn from ────────────────────

def _timeline_payload(recordings, accounts, now):
    """Everything the timeline draws, as one JSON blob on the page (the GUIDE_CONFIG
    pattern) rather than a second endpoint - it is the same data this render already read.

    Times are epoch milliseconds UTC so the client does no timezone arithmetic; the labels
    on the axis are formatted by the browser in the user's own zone.

    Jobs carry `est_seconds: None` because the app has NO runtime estimates yet. DESIGN.md
    16.3 is explicit about what that must render: an outline with no length, never a number
    invented to make the bar legible. That consequence was accepted when the section was
    approved ("So we'll have to add runtime estimates as a feature").

    CALL ORDER IS LOAD-BEARING. `_build_job_list()` does a `db.session.get()` per scheduled
    recording and per sync job. Those cost no SQL here only because the caller has already
    loaded the same Recording and Account rows into this session, so every one of them is an
    identity-map hit. Moving this call ahead of those queries turns it into per-row I/O on a
    page that renders a row-scaling list.
    """
    from ..config import load_config
    from .jobs import _build_job_list

    # Same section app/scheduler.py and app/accounts.py read the guards from - the
    # mark on the axis has to describe what those two will actually do.
    sync_cfg = load_config().get('sync', {})
    account_color = {a.id: a.color for a in accounts}

    def _ms(dt):
        return int(dt.replace(tzinfo=UTC).timestamp() * 1000)

    rows = []
    for r in recordings:
        rows.append({
            'id': r.id,
            'name': r.name,
            'status': r.status,
            'channel': r.channel.name if r.channel else None,
            'color': account_color.get(r.channel.account_id) if r.channel else None,
            'start': _ms(r.start_time),
            'end': _ms(r.stop_time),
            'url': f'/recordings/{r.id}',
        })

    jobs = []
    for job in _build_job_list():
        if not job.get('next_run_utc'):
            continue
        # The recording start/stop jobs are dropped, and ONLY those: they are the same
        # recordings the lanes above already draw, so keeping them would put every
        # scheduled capture on the page twice - once as a bar and once as a pip on the
        # rail beneath it. Everything else is kept by default, so a job type added to
        # _build_job_list later appears here rather than being silently missing.
        if job['id'].startswith(('start_', 'active_')):
            continue
        jobs.append({
            'id': job['id'],
            'name': job['display_name'],
            'start': _ms(datetime.fromisoformat(job['next_run_utc'])),
            # No estimate exists anywhere in the app yet, so this is None on every job and
            # the client draws the unknown state. See this function's docstring.
            'est_seconds': None,
            'runs': 0,
            # Only an account sync is subject to the guards below, so only it is eligible
            # to be marked. Matched on the id, not on display_name, which is user data.
            'is_sync': job['id'].startswith('account_sync_'),
            'url': '/jobs',
        })

    return {
        'now': _ms(now),
        'recordings': rows,
        'jobs': jobs,
        # 16.4: the thresholds come from config, never from a typed number, because the
        # warning has to describe what scheduler.py will ACTUALLY do.
        'syncGuards': {
            'skipIfRecordingActive': bool(sync_cfg.get('skip_sync_if_recording_active', True)),
            'skipWithinMinutes': int(sync_cfg.get('skip_sync_if_recording_within_minutes', 5)),
        },
    }


def _live_row_stats(rows, now):
    """Elapsed/remaining/bytes for each live row, so the page paints real numbers.

    The three stat cells used to render a literal '-' and wait for an SSE STATS_SNAPSHOT to
    fill them. Only a capturing recording emits those, so a PAUSED, RETRYING or
    CONCATENATING row sat on dashes indefinitely - and CLAUDE.md's frontend rule is that
    server-rendered initial state must already be right, with JS upgrading it rather than
    being required to calm it down.

    Reads only fields the caller has already loaded (`selectinload`-ed segments included):
    no query and no disk access per row, which `test_scaling_pages::test_dashboard_page`
    enforces. An in-flight segment's not-yet-recorded bytes are therefore missing here; SSE
    supplies them within a tick on the one status that has an in-flight segment at all.
    """
    stats = {}
    for r in rows:
        elapsed = (now - r.started_at).total_seconds() if r.started_at else 0
        stats[r.id] = (
            max(0, elapsed),
            max(0, (r.stop_time - now).total_seconds()),
            sum((s.bytes_recorded or 0) for s in r.segments if s.ended_at),
        )
    return stats


@dashboard_bp.route('/')
def dashboard():
    # selectinload segments: the active-card template counts `rec.segments | length` per
    # non-scheduled row, which is a lazy per-row load (N+1) without this - one batched query
    # instead. Guarded by test_scaling_pages::test_dashboard_page.
    active = Recording.query.options(selectinload(Recording.segments)).filter(
        Recording.status.in_((*WINDOW_OPEN_STATUSES, REC_STATUS_CONCATENATING,
                              REC_STATUS_ANALYZING, REC_STATUS_CONVERTING,
                              REC_STATUS_SCHEDULED))
    ).order_by(Recording.start_time).all()
    accounts = Account.query.order_by(Account.created_at).all()
    health_check = get_active_run_summary()
    now = datetime.utcnow()

    # The two record sections are cut from the ONE query above rather than re-queried, so a
    # section's head count and the rows under it cannot come from two computations
    # (DESIGN.md 16.6).
    live = [r for r in active if r.status != REC_STATUS_SCHEDULED]
    upcoming = [r for r in active if r.status == REC_STATUS_SCHEDULED]

    order, enabled = _section_state()

    return render_template(
        'dashboard.html',
        recordings=active, accounts=accounts, health_check=health_check,
        next_sync=next_sync_map(accounts),
        live=live, upcoming=upcoming, now=now, live_stats=_live_row_stats(live, now),
        rec_status_class=REC_ROW_STATUS_CLASS,
        # Per row rather than through the rec_status_label filter, because the parked
        # derivation needs a second column off the row. Built from the rows already
        # loaded - a dict build, no query (tests/test_scaling_pages.py::test_dashboard_page).
        rec_status_text={r.id: rec_status_display(
            r.status, waiting=bool(r.postprocess_waiting_since))[3] for r in active},
        rec_status_labels=REC_ROW_STATUS_LABEL,
        section_defs=DASHBOARD_SECTIONS, section_order=order, section_on=enabled,
        section_pref_key=DASHBOARD_SECTIONS_PREF,
        metrics=_metric_tiles(active, accounts, now),
        timeline=_timeline_payload(active, accounts, now),
    )


@dashboard_bp.route('/api/stats/<int:recording_id>')
def stats_snapshot(recording_id):
    """JSON snapshot of current recording stats (for initial page load)."""
    rec = db.session.get(Recording, recording_id)
    if rec is None:
        return jsonify({'error': 'not found'}), 404

    now = datetime.utcnow()
    elapsed = (now - rec.started_at).total_seconds() if rec.started_at else 0
    remaining = max(0, (rec.stop_time - now).total_seconds())

    # Current segment bytes (last segment without ended_at, or last segment)
    current_bytes = 0
    active_seg = None
    for seg in reversed(rec.segments):
        if seg.ended_at is None:
            active_seg = seg
            break
    if active_seg:
        try:
            current_bytes = os.path.getsize(active_seg.file_path)
        except OSError:  # segment file not on disk yet (or already cleaned up)
            current_bytes = 0

    total_bytes = sum((s.bytes_recorded or 0) for s in rec.segments if s.ended_at) + current_bytes

    return jsonify({
        'recording_id': recording_id,
        'name': rec.name,
        'status': rec.status,
        'elapsed_seconds': elapsed,
        'remaining_seconds': remaining,
        'total_bytes': total_bytes,
        'final_file_size': rec.final_file_size,
        'stall_count': rec.total_stall_count,
        'restart_count': rec.total_restart_count,
        'consecutive_failures': rec.consecutive_failures,
        'consecutive_failures_peak': rec.consecutive_failures_peak,
        'downtime_seconds': rec.total_downtime_seconds,
        'segment_count': len(rec.segments),
        'current_segment': active_seg.segment_number if active_seg else None,
    })


def _sse_response(recording_id=None):
    """Build an SSE Response subscribed to a single recording, or all recordings if None."""
    q = ev.subscribe(recording_id)

    def generate():
        yield ': connected\n\n'
        try:
            while True:
                try:
                    data = q.get(timeout=15)
                    yield f'data: {data}\n\n'
                except queue.Empty:
                    yield ': keepalive\n\n'
        finally:
            ev.unsubscribe(q, recording_id)

    return Response(
        stream_with_context(generate()),
        mimetype='text/event-stream',
        headers={
            'Cache-Control': 'no-cache',
            'X-Accel-Buffering': 'no',
            'Connection': 'keep-alive',
        },
    )


@dashboard_bp.route('/api/stream')
def stream_all():
    """SSE endpoint streaming events for all recordings."""
    return _sse_response()


@dashboard_bp.route('/api/stream/<int:recording_id>')
def stream_recording(recording_id):
    """SSE endpoint streaming events for a single recording."""
    return _sse_response(recording_id)


# Every WINDOW_OPEN_STATUSES member, explicitly - a status added to that tuple without a
# label here raises rather than rendering as somebody else's state.
_WINDOW_OPEN_LABEL = {
    REC_STATUS_IN_PROGRESS: 'Recording',
    REC_STATUS_PAUSED:      'Paused',
    REC_STATUS_RETRYING:    'Waiting to retry',
}


def _activity_status_dict():
    """Dict snapshot of current recording and background activity for the header indicators."""
    now = datetime.utcnow()

    def _fmt_et(dt_utc):
        return format_local(dt_utc, 'day_datetime')

    def _relative(dt_utc):
        return relative(dt_utc, style='compact', now=now)

    # ── Recording indicator ───────────────────────────────────────────────────
    # Two queries, not one. WINDOW_OPEN_STATUSES rows are already past their start, so they
    # must NOT carry the future-start_time clause - PAUSED and RETRYING were once filtered
    # alongside SCHEDULED under it and matched nothing, leaving a paused or retrying
    # recording invisible in this indicator (dev/changelog/663).
    live_recs = Recording.query.filter(
        Recording.status.in_(WINDOW_OPEN_STATUSES),
    ).order_by(Recording.start_time).all()
    scheduled_recs = Recording.query.filter(
        Recording.status == REC_STATUS_SCHEDULED,
        Recording.start_time > now,
    ).order_by(Recording.start_time).all()

    if live_recs:
        rec_state = 'active'
    elif scheduled_recs:
        rec_state = 'dim'
    else:
        rec_state = 'hidden'

    active_rec_data = []
    for r in live_recs:
        secs_remaining = max(0, (r.stop_time - now).total_seconds())
        active_rec_data.append({
            'id': r.id,
            'name': r.name,
            'href': url_for('recordings.recording_detail', recording_id=r.id),
            'status': r.status,
            # Named, not just counted: "3 recordings" is a lie when one of them is a dead
            # stream backing off toward a reconnect. Same wording the recordings list uses.
            'state_label': _WINDOW_OPEN_LABEL[r.status],
            'seconds_remaining': int(secs_remaining),
            'stop_time_et': _fmt_et(r.stop_time),
        })

    next_sched = None
    if scheduled_recs:
        r = scheduled_recs[0]
        next_sched = {
            'id': r.id,
            'name': r.name,
            'href': url_for('recordings.recording_detail', recording_id=r.id),
            'start_et': _fmt_et(r.start_time),
            'start_relative': _relative(r.start_time),
        }

    recording_payload = {
        'state': rec_state,
        'active': active_rec_data,
        'scheduled_count': len(scheduled_recs),
        'next_scheduled': next_sched,
    }

    # ── Background indicator ──────────────────────────────────────────────────
    from ..channel_tester import get_status as ct_status
    ct = ct_status()

    # Every row carries an `href`: the chip's tooltip renders each task as a link to the
    # thing it is about, so a job the user can see is a job the user can reach in one click
    # (dev/changelog/949). A task whose work has no page of its own points at the page that
    # lists its kind - /jobs or /maintenance - never at nothing.
    bg_tasks = []

    # Dashboard-visible subset of bg_tasks, i.e. items that also show up as a row in a
    # Dashboard page section (accounts, health). Search index rebuild is deliberately
    # excluded below - it has no Dashboard section of its own, only the top-bar chip.
    dashboard_bg_count = 0

    # Channel health test
    if ct['is_running']:
        total = ct.get('total_channels') or 0
        done = ct.get('completed_channels') or 0
        ch_name = ct.get('current_channel_name', '')
        if ch_name:
            detail = f'Testing {ch_name} ({done + 1} of {total})' if total else f'Testing {ch_name}'
        else:
            detail = f'{done} of {total} channels tested' if total else 'Starting…'
        bg_tasks.append({'label': 'Channel health test', 'detail': detail,
                         'href': url_for('jobs.jobs_page')})
        dashboard_bg_count += 1

    # Account sync
    syncing_accounts = Account.query.filter_by(status='SYNCING').all()
    for acc in syncing_accounts:
        progress = get_sync_progress(acc.id)
        if progress and progress['phase'] == 'channels' and progress['total']:
            detail = f"Syncing {acc.name} - {progress['done']:,} of {progress['total']:,} channels"
        elif progress and progress['phase'] == 'epg':
            detail = f"Syncing {acc.name} - {progress['done']:,} EPG entries"
        else:
            detail = f'Syncing {acc.name}'
        bg_tasks.append({'label': 'Account sync', 'detail': detail,
                         'href': url_for('accounts.account_detail', account_id=acc.id)})
        dashboard_bg_count += 1

    # The three post-capture phases - already counted via `live` on the Dashboard's own
    # recording sections (IN_PROGRESS + CONCATENATING + ANALYZING + CONVERTING all render as
    # one "Recordings in progress" list there). Each label names its own phase: the analysis
    # pass is a whole-file read that reaches hours on a large capture, so a background-task
    # row calling it "Concatenating" is the defect dev/changelog/867 removed.
    for status_val, label in [(REC_STATUS_CONCATENATING, 'Joining segments'),
                              (REC_STATUS_ANALYZING, 'Checking the joined file'),
                              (REC_STATUS_CONVERTING, 'Converting')]:
        recs_in_status = Recording.query.filter_by(status=status_val).all()
        for r in recs_in_status:
            detail = r.name
            # Per-row, never rebinding the loop's own `label` - a second recording in the
            # same status would otherwise inherit the first one's parked wording.
            row_label = label
            if r.postprocess_waiting_since and status_val in (REC_STATUS_ANALYZING,
                                                              REC_STATUS_CONVERTING):
                # Parked to let another recording have the machine, so the phase labels
                # above describe work that has stopped. Same display-only derivation the
                # recordings list makes; the row's status is untouched (dev/changelog/954).
                row_label = ('Conversion paused' if status_val == REC_STATUS_CONVERTING
                             else 'Waiting to convert')
                detail = f'{r.name} - waiting on "{r.postprocess_waiting_on_name}"'
            elif status_val == REC_STATUS_CONVERTING:
                detail = _converting_detail(r)
            elif status_val == REC_STATUS_CONCATENATING:
                detail = _joining_detail(r)
            elif status_val == REC_STATUS_ANALYZING:
                detail = _analyzing_detail(r)
            bg_tasks.append({'label': row_label, 'detail': detail,
                             'href': url_for('recordings.recording_detail', recording_id=r.id)})
            dashboard_bg_count += 1

    # Search index rebuild - the window between "sync complete" and "index rebuilt" (up to
    # ~90s) used to have no indicator at all, even though real work was still happening
    # (dev/changelog/461). Not part of dashboard_bg_count: no Dashboard page section shows it.
    from ..search_index import rebuilding_index_names
    for name in rebuilding_index_names():
        bg_tasks.append({'label': 'Search index rebuild', 'detail': f'Rebuilding {name} index',
                         'href': url_for('system.maintenance') + '#m-index'})

    # The admission registry (app/admission.py) is the only record of a maintenance job -
    # retention sweeps and the daily database maintenance leave no row anywhere else, so
    # this chip was silent for their whole duration while they held the database axis. The
    # other three kinds are deliberately NOT read from here: tester, sync and rebuild each
    # already have a row above built from the database, with progress detail the registry
    # does not carry, and reporting them twice would say the same work is happening twice.
    # Not part of dashboard_bg_count - maintenance has no Dashboard page section, same as
    # the rebuild entry above.
    from ..admission import KIND_MAINTENANCE, describe_active
    admission_held = describe_active()
    for kind, label, _age in admission_held:
        if kind == KIND_MAINTENANCE:
            bg_tasks.append({'label': 'Database maintenance', 'detail': label or 'Running',
                             'href': url_for('system.maintenance')})

    bg_active = bool(bg_tasks)

    # Dim state: a background job scheduled to start within the next hour. Bounded
    # deliberately - account sync and the daily config backup run on multi-hour/daily
    # cadences, so an unbounded "any future job" check would leave this chip dim
    # almost permanently, defeating the point of a "something's imminent" signal
    # (decided 2026-08-05).
    _BG_DIM_WINDOW = timedelta(minutes=60)
    next_bg_job = None
    future_jobs = []
    try:
        from ..scheduler import get_scheduler
        scheduler = get_scheduler()
        if scheduler:
            # label, and the page that job's kind is managed from - the tooltip row is a
            # link like every other one, so each entry owns its own destination rather
            # than every scheduled job landing on the same generic list.
            _BG_SYSTEM_NAMES = {
                'config_backup_daily': ('Config Backup', url_for('system.maintenance')),
            }
            from ..database import OnDemandTestJob
            sys_job = OnDemandTestJob.query.filter_by(is_system=True).first()
            if sys_job:
                _BG_SYSTEM_NAMES[f'od_job_{sys_job.id}'] = (
                    'TV Guide Channels Health Check', url_for('jobs.jobs_page'))

            def _bg_label(job_id):
                if job_id in _BG_SYSTEM_NAMES:
                    return _BG_SYSTEM_NAMES[job_id]
                # Real job id is per-account (`account_sync_<id>`, or its deferred-retry
                # sibling `account_sync_retry_<id>`) - there is no single global sync job.
                if job_id.startswith('account_sync_'):
                    return ('Account / EPG Sync', url_for('accounts.accounts_list'))
                return None

            cutoff = now + _BG_DIM_WINDOW
            for j in scheduler.get_jobs():
                if j.next_run_time is None:
                    continue
                named = _bg_label(j.id)
                if named is None:
                    continue
                nrt_utc = to_naive_utc(j.next_run_time)
                if nrt_utc > cutoff:
                    continue
                future_jobs.append((nrt_utc, named[0], named[1]))
            if future_jobs:
                future_jobs.sort(key=lambda t: t[0])
                nrt_utc, label, href = future_jobs[0]
                next_bg_job = {
                    'label': label,
                    'href': href,
                    'time_et': _fmt_et(nrt_utc),
                    'relative': _relative(nrt_utc),
                }
    except Exception as exc:
        # Indicator is best-effort - never let it break the nav-status poll - but a
        # failure here must not be silent (this used to swallow everything unlogged).
        log.warning('Could not read scheduler jobs for activity status: %s', exc)
        next_bg_job = None
        future_jobs = []
    bg_has_scheduled = bool(future_jobs)

    if bg_active:
        bg_state = 'active'
    elif bg_has_scheduled:
        bg_state = 'dim'
    else:
        bg_state = 'hidden'

    background_payload = {
        'state': bg_state,
        'tasks': bg_tasks,
        'next_scheduled': next_bg_job,
        # Every held admission ticket, whether or not it also earned a `tasks` row above.
        # Two readers: a leaked ticket presents only as work that never runs and this is
        # where its age shows, and it is the sole way an out-of-process reader can see the
        # registry at all - it lives in this process's memory, not in the database.
        'admission': [{'kind': kind, 'label': label, 'age_seconds': age}
                      for kind, label, age in admission_held],
    }

    return {
        'recording': recording_payload,
        'background': background_payload,
        # Dashboard sidebar nav-count badge: every Dashboard-visible in-progress
        # activity (window-open/converting/concatenating recordings, account syncs, a
        # running channel health check) - not just capturing recordings. It counts
        # live_recs rather than only IN_PROGRESS because the Dashboard page's own
        # "Recordings in progress" section renders every window-open status too; the
        # badge and that section have to describe the same set.
        'dashboard_activity_count': len(live_recs) + dashboard_bg_count,
    }


@dashboard_bp.route('/api/activity/status')
def activity_status():
    return jsonify(_activity_status_dict())


def _search_readiness_dict():
    """Search-index readiness, keyed by which index SET a field selection needs.

    THE KEY NAMES ARE NOT INDEX NAMES. `channels` is the answer for a search over channel
    columns only; `programs` is the answer for one that also touches a program column, so
    it covers BOTH indexes and is False whenever either is unusable. The client mirrors
    channel_search._index_names() to pick between them - see __applySearchReadiness in
    static/js/channel-search.js, which carries the same warning. The tuple keys
    readiness_map() returns are not JSON-serializable, which is why they are flattened
    here rather than passed through.

    This runs on EVERY page in the app, not just the search page: 4 state rows plus 4
    watermark lookups per poll, all index hits. dev/changelog/427.
    """
    from ..search_index import SEARCH_INDEX_CHANNELS, SEARCH_INDEX_NAMES, readiness_map
    ready = readiness_map()
    return {name: {'ready': ready[names][0], 'reason': ready[names][1]}
            for name, names in (('channels', (SEARCH_INDEX_CHANNELS,)),
                                ('programs', SEARCH_INDEX_NAMES))}


@dashboard_bp.route('/api/nav-status')
def nav_status():
    """Combined nav-bar payload: system stats + unread alert summary + activity indicators.

    Replaces what used to be 3 separate polling loops in base.html with a single
    endpoint polled at `display.nav_poll_interval_seconds`.

    `account_sync` is `accounts.sync_signature()`, which /accounts compares against the one
    its rows were rendered at to know when to re-render itself.
    """
    from .system import _system_stats_dict
    from .alerts import _unread_alert_summary
    from ..readiness import nav_summary
    return jsonify(
        stats=_system_stats_dict(),
        alerts=_unread_alert_summary(),
        activity=_activity_status_dict(),
        search=_search_readiness_dict(),
        account_sync=sync_signature(),
        # Cached for 30s inside nav_summary(), and never able to run an on-demand check -
        # a poll that spawned a process or opened a provider connection would be the exact
        # thing the Readiness card refuses to do on page load (dev/changelog/950).
        readiness=nav_summary(),
    )


