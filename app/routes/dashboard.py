import hashlib
import json
import logging
import os
import queue
from datetime import datetime, timedelta

from flask import Blueprint, render_template, Response, stream_with_context, jsonify, url_for

from sqlalchemy import func
from sqlalchemy.orm import selectinload

from .. import db
from ..database import (
    Recording, RecordingEvent, Account, EpgSource, STALL_DETECTED,
    REC_STATUS_COMPLETED, REC_STATUS_FAILED, REC_STATUS_ABORTED,
    REC_STATUS_SCHEDULED, REC_STATUS_IN_PROGRESS, REC_STATUS_PAUSED, REC_STATUS_RETRYING,
    REC_STATUS_CONCATENATING, REC_STATUS_ANALYZING, REC_STATUS_CONVERTING,
    WINDOW_OPEN_STATUSES, ACCOUNT_STATUS_ERROR, ACCOUNT_STATUS_SYNCING,
)
from .. import events as ev
from .. import account_stats_view, setup_steps
from ..config import load_config
from ..tz_utils import UTC, to_naive_utc, format_local, relative
from ..accounts import get_sync_progress, next_sync_map, sync_signature
from .channel_tests import get_active_run_summary, schedule_phrase
from ..fmt_utils import REC_STATUS_DISPLAY, counted, rec_is_waiting, rec_status_display
from ..recording_story import partial_note, stopped_early_note

dashboard_bp = Blueprint('dashboard', __name__)
log = logging.getLogger(__name__)

# The dashboard's recording rows reuse the one status vocabulary rather than a second copy,
# so the two surfaces cannot silently color - or name - the same status two different ways
# (CLAUDE.md: "one flag, one meaning; states are enumerated"). The label half was left
# behind here for as long as the table existed, which is why the template printed the raw
# enum and the Dashboard said CONCATENATING where every other page said JOINING
# (dev/changelog/961).
REC_ROW_STATUS_CLASS = {status: row[1] for status, row in REC_STATUS_DISPLAY.items()}

# ── The machine-readable half of a background-task row ───────────────────────
# `label` and `detail` on a task are display text: they are reworded whenever the wording
# is wrong, and the Dashboard's own history has two entries doing exactly that. `kind` is
# what a client keys a DECISION off, so that a reword cannot silently switch a behavior
# off. dashboard.js watches for BG_KIND_HEALTH_CHECK to know a health check has started or
# ended; nothing else reads one yet, and every row carries one anyway so the next reader
# does not have to add the field to four call sites first (dev/changelog/1082).
BG_KIND_HEALTH_CHECK = 'health_check'
BG_KIND_ACCOUNT_SYNC = 'account_sync'
BG_KIND_EPG_SOURCE_REFRESH = 'epg_source_refresh'
BG_KIND_POST_CAPTURE = 'post_capture'
BG_KIND_SEARCH_INDEX = 'search_index'
BG_KIND_MAINTENANCE = 'maintenance'


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
             'hint': 'Capturing or finishing right now.',
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

# Every status the Dashboard's two recording sections render - which is every status that is
# not an outcome. recording_signature() reads the same set, so the page and the signature it
# re-renders on cannot disagree about which rows exist.
DASHBOARD_REC_STATUSES = (*WINDOW_OPEN_STATUSES, REC_STATUS_CONCATENATING,
                          REC_STATUS_ANALYZING, REC_STATUS_CONVERTING, REC_STATUS_SCHEDULED)


def recording_signature() -> str:
    """A short string that changes whenever the set of recordings still under way changes.
    /api/nav-status carries it and the Dashboard renders the one its rows were read at, so
    the page knows a recording started, finished, was scheduled elsewhere or was edited
    without reloading (dev/changelog/1143). Built for more readers than the Dashboard: the
    Recordings list and the TV Guide's recording chips need the same trigger.

    Hashes, per row in DASHBOARD_REC_STATUSES: id and status, which move on every
    transition - a finished, failed or aborted row got there FROM one of these statuses, so
    no outcome is reached without the set changing; the parked and held-start fields, which
    change a row's cells with no status change and no SSE frame; and name, channel and the
    two times, which an edit from another tab moves. count(*) over the whole table adds the
    one change that touches no unfinished row: deleting a finished recording.

    Deliberately NOT hashed: anything SSE already keeps current inside a row (bytes, progress,
    ETA, stalls). Those move every second, and a signature that moved with them would re-render
    the page on every poll. updated_at is left out for the same reason - capture and conversion
    rewrite it constantly. A scan of a small table on every nav poll: ~0.04 ms on the live
    database."""
    rows = (db.session.query(
                Recording.id, Recording.status, Recording.name, Recording.channel_id,
                Recording.start_time, Recording.stop_time,
                Recording.postprocess_waiting_since, Recording.postprocess_waiting_on_name,
                Recording.start_deferred_since, Recording.start_deferred_for)
            .filter(Recording.status.in_(DASHBOARD_REC_STATUSES))
            .order_by(Recording.id).all())
    total = db.session.query(func.count(Recording.id)).scalar() or 0
    digest = hashlib.blake2s(repr([tuple(r) for r in rows]).encode(), digest_size=8).hexdigest()
    return f'{total}|{len(rows)}|{digest}'


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
    from ..concatenator import concat_progress, concat_progress_parts
    prog = concat_progress(r.id)
    if prog is None:
        return r.name
    return f'{r.name} - {", ".join(concat_progress_parts(prog))}'


def _analyzing_detail(r):
    """Dashboard bg-task detail for an ANALYZING row: which whole-file read is running and
    how far through it is, else just the name.

    Same registry read and the same reasoning as _joining_detail above
    (postprocessor._analysis_progress). No entry means no pass is reading - between the two
    passes, or before the first one - which reads as the bare name rather than a 0% that
    describes nothing."""
    from ..postprocessor import analysis_progress, analysis_progress_parts
    prog = analysis_progress(r.id)
    if prog is None:
        return r.name
    return f'{r.name} - {", ".join(analysis_progress_parts(prog))}'


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

# How far back the timeline reaches for recordings that have already finished, so one that
# stalled and ended a few minutes ago is still on the axis rather than gone from the page the
# moment it completed (dev/changelog/1270).
# duplicated from static/js/dashboard.js TL_BACK_HOURS - the server picks the finished rows
# the axis's back half can show, the browser draws the axis.
TIMELINE_BACK = timedelta(hours=2)
FINISHED_STATUSES = (REC_STATUS_COMPLETED, REC_STATUS_FAILED, REC_STATUS_ABORTED)


def recently_finished(now):
    """Recordings that captured something and whose window ended inside TIMELINE_BACK.
    One query; segments come with them for partial_note()."""
    return (Recording.query.options(selectinload(Recording.segments))
            .filter(Recording.status.in_(FINISHED_STATUSES),
                    Recording.started_at.isnot(None),
                    Recording.stop_time > now - TIMELINE_BACK,
                    Recording.start_time < now)
            .order_by(Recording.start_time).all())


def _stalls_last_hour(recordings, now):
    """(stalls, recordings that had one) in the last hour, from the STALL_DETECTED events of
    the recordings handed in - the ones under way or finished inside TIMELINE_BACK, which is
    every recording that can have a stall that recent. Counted from the events rather than
    from a recording's running total, so a stall from three hours into a long capture is not
    counted as this hour's, and a recording that finished a minute ago still is."""
    ids = [r.id for r in recordings if r.started_at]
    if not ids:
        return 0, 0
    stalls, recs = (db.session.query(func.count(RecordingEvent.id),
                                     func.count(func.distinct(RecordingEvent.recording_id)))
                    .filter(RecordingEvent.recording_id.in_(ids),
                            RecordingEvent.event_type == STALL_DETECTED,
                            RecordingEvent.timestamp >= now - _ERROR_WINDOW)
                    .one())
    return stalls or 0, recs or 0


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


def _metric_tiles(recordings, accounts, now, finished=()):
    """The six tiles of DESIGN.md 16.1's metric strip, each derived from data already read.

    `cls` is one of '', 'ok', 'warn', 'bad' and drives the tile's left rule and value color.
    """
    from ..fmt_utils import fmt_bytes
    from .recordings import time_ago_filter
    from .system import DVR_DIR_ROLE, _disk_bytes, disk_level, disk_used_pct

    capturing = [r for r in recordings if r.status == REC_STATUS_IN_PROGRESS]
    # "Finishing" is the umbrella for the three post-capture phases - joining, checking,
    # converting. It used to be "converting", which is also the badge for the third phase
    # alone, so a tile reading "1 converting" sat over a row badged JOINING
    # (dev/changelog/1247). The same word labels the timeline legend and the HA sensor.
    finishing = [r for r in recordings
                 if r.status in (REC_STATUS_CONVERTING, REC_STATUS_CONCATENATING,
                                 REC_STATUS_ANALYZING)]
    scheduled = [r for r in recordings if r.status == REC_STATUS_SCHEDULED]

    dvr_dir = load_config()['recording']['dvr_output_dir']
    disk_total, disk_free = _disk_bytes(dvr_dir, DVR_DIR_ROLE)
    used_pct = disk_used_pct(disk_total, disk_free)

    err_accounts = [a for a in accounts if a.status == ACCOUNT_STATUS_ERROR]
    synced = [a.last_sync_at for a in accounts if a.last_sync_at]
    oldest_sync = min(synced) if synced else None

    # logging's %(asctime)s writes SYSTEM-local wall clock, not UTC and not the configured
    # display timezone, so the cutoff is datetime.now() rather than utcnow().
    errors, crits, log_lines = _recent_error_counts(datetime.now())

    stalls, stalled_recs = _stalls_last_hour(list(recordings) + list(finished), now)
    nxt = scheduled[0] if scheduled else None

    return [
        {'key': 'Capturing now', 'value': str(len(capturing)),
         'sub': f'{len(finishing)} finishing' if finishing else 'nothing finishing',
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
         'cls': disk_level(used_pct) or ''},
        # Both tiles name what they count. "Errors 0" beside a recording that had just
        # stalled six times read as the app saying nothing went wrong: a stall is logged as
        # a warning, not an error, and the stall tile counted only what was capturing at
        # that moment (dev/changelog/1270). "60 min" rather than "last hour" because the
        # longer key was cut to "LOG ERRORS, LAS..." at 1280 (dev/changelog/1292).
        {'key': 'Errors, 60 min',
         'value': None if errors is None else str(errors),
         'sub': ('no readable log file' if errors is None
                 else f'{crits} critical' if crits else f'{log_lines} log lines'),
         'cls': '' if errors is None else 'bad' if crits else 'warn' if errors else 'ok',
         'tip': ('ERROR and CRITICAL lines ChannelBin wrote to its log in the last hour.\n'
                 'A stall is a warning, not an error - the stall tile counts those.')},
        {'key': 'Stalls, 60 min', 'value': str(stalls),
         'sub': f'on {counted(stalled_recs, "recording")}' if stalls else 'none',
         'cls': 'bad' if stalls > 5 else 'warn' if stalls else 'ok',
         'tip': ('Times a stream stopped delivering video in the last hour, on any recording - '
                 'capturing now or already finished. Each recording\'s page says what '
                 'happened and what ChannelBin did about it.')},
        {'key': 'Accounts', 'value': f'{len(accounts) - len(err_accounts)} of {len(accounts)}',
         'sub': 'never synced' if oldest_sync is None else f'oldest sync {time_ago_filter(oldest_sync)}',
         'cls': 'bad' if err_accounts else 'ok'},
    ]


# ── DESIGN.md 16.3/16.4: what the timeline is drawn from ────────────────────

def _timeline_payload(recordings, accounts, now, finished=()):
    """Everything the timeline draws, as one JSON blob on the page (the GUIDE_CONFIG
    pattern) rather than a second endpoint - it is the same data this render already read.

    Times are epoch milliseconds UTC so the client does no timezone arithmetic; the labels
    on the axis are formatted by the browser in the user's own zone.

    Every job's runtime estimate, and the recording guard the scheduler will apply to it,
    are `_build_job_list()`'s - the same numbers /jobs shows, so the two pages cannot
    disagree about how long a job runs or whether it will yield (DESIGN.md 16.3, 16.4). A
    job with no run history arrives with `est_seconds: None` and draws as an outline with
    no length, never a number invented to make the bar legible.
    """
    from .jobs import _build_job_list

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
    # A finished row says how it went, since its bar no longer carries a live state: a
    # partial file, stalls, a failure or a cancel each read differently from a clean one.
    for r in finished:
        if r.status == REC_STATUS_FAILED:
            note, trouble = 'Failed - open the recording for why.', True
        elif r.status == REC_STATUS_ABORTED:
            note, trouble = 'Cancelled.', False
        else:
            partial = partial_note(r)
            # The user's own Stop is said, and is not trouble (dev/changelog/1287).
            stopped = stopped_early_note(r)
            stalls = r.total_stall_count or 0
            parts = ([f'Partial: {partial}'] if partial else []) + (
                [stopped] if stopped else []) + (
                [f'Finished after {counted(stalls, "stall")}.'] if stalls else [])
            note = ' '.join(parts) or 'Finished cleanly.'
            trouble = bool(partial or stalls)
        rows.append({
            'id': r.id,
            'name': r.name,
            'status': r.status,
            'channel': r.channel.name if r.channel else None,
            'color': account_color.get(r.channel.account_id) if r.channel else None,
            'start': _ms(r.started_at or r.start_time),
            'end': _ms(r.stop_time),
            'url': f'/recordings/{r.id}',
            'note': note,
            'trouble': trouble,
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
            'est_seconds': job['est_seconds'],
            # /jobs' own sentence, so the tooltip and the table word it once.
            'duration_line': job.get('duration_line'),
            # None for a job no recording can defer - only those with one are eligible to
            # be marked (16.4).
            'guard': job.get('recording_guard'),
            'url': '/jobs',
        })

    return {
        'now': _ms(now),
        'recordings': rows,
        'jobs': jobs,
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


def _postcapture_row_cells(rows):
    """The three stat cells of a JOINING or ANALYZING row: what the phase is doing and how
    far it has got, from the same in-memory progress registries the Recordings list reads,
    plus the bytes the capture left. {id: [(key, value, tip), ...]}.

    These rows are past their stop time, so the Elapsed/Remaining pair the capture rows
    show would read "Remaining 0s" under an Elapsed still counting from the capture start -
    capture-era numbers under labels that read as current (dev/changelog/1247). Both
    registry reads are dict lookups under a lock, never a query, so
    test_scaling_pages::test_dashboard_page holds.
    """
    from ..concatenator import concat_progress, concat_progress_parts
    from ..postprocessor import analysis_progress, analysis_progress_parts

    cells = {}
    for r in rows:
        captured = sum((s.bytes_recorded or 0) for s in r.segments if s.ended_at)
        recorded = ('Captured', captured,
                    'Bytes the capture wrote to its segments. The finished file is what the '
                    'join produces from them.')
        # The words come from the two shared builders, never spelled here, so the Dashboard
        # and the Recordings list cannot describe one join two ways (tests/test_progress_parts).
        if r.status == REC_STATUS_CONCATENATING:
            prog = concat_progress(r.id)
            if prog is None:
                cells[r.id] = [('Joining', 'queued',
                                'Waiting for another join to finish before this one starts.'),
                               ('Segments', '-', None), recorded]
            else:
                parts = concat_progress_parts(prog)   # [pct?, 'N of M segments']
                cells[r.id] = [('Joining', parts[0] if len(parts) > 1 else 'running',
                                'How far the join of the segments has got.'),
                               ('Segments', parts[-1],
                                'Segments joined so far, of the segments being joined.'),
                               recorded]
        elif r.status == REC_STATUS_ANALYZING:
            prog = analysis_progress(r.id)
            if prog is None:
                cells[r.id] = [('Checking', 'between passes',
                                'No whole-file read is running right now: between two passes, '
                                'or before the first one.'),
                               ('Progress', '-', None), recorded]
            else:
                parts = analysis_progress_parts(prog)   # ['label (n of m)', pct?]
                cells[r.id] = [('Checking', parts[0],
                                'Which whole-file read of the joined file is running.'),
                               ('Progress', parts[1] if len(parts) > 1 else '-',
                                'How far through the joined file this pass has read.'),
                               recorded]
    return cells


@dashboard_bp.route('/')
def dashboard():
    # Read before the rows: a change landing between the two then makes the next poll
    # re-render once more, where the other order would leave that change unseen.
    rec_sig = recording_signature()
    sync_sig = sync_signature()
    # Before any row this page renders: its Readiness write probe rolls back the session,
    # which would expire every loaded row and reload each one lazily (app/setup_steps.py).
    # After the two signatures, so a change landing in between re-renders the banner too.
    try:
        setup = setup_steps.evaluate()
    except Exception as exc:                              # noqa: BLE001 - logged; never break the page
        # The banner is a pointer, not the page: a Readiness context that cannot be built
        # costs the banner, never the dashboard under it. nav_summary() makes the same call.
        log.warning('dashboard: setup steps could not be read, banner left out: %s', exc)
        setup = None
    # selectinload segments: the active-card template counts `rec.segments | length` per
    # non-scheduled row, which is a lazy per-row load (N+1) without this - one batched query
    # instead. Guarded by test_scaling_pages::test_dashboard_page.
    active = Recording.query.options(selectinload(Recording.segments)).filter(
        Recording.status.in_(DASHBOARD_REC_STATUSES)
    ).order_by(Recording.start_time).all()
    accounts = Account.query.order_by(Account.created_at).all()
    finished = recently_finished(datetime.utcnow())
    # The account rows' second line (DESIGN.md 16.1): the same macros /accounts draws, from
    # the same numbers, so the two pages cannot disagree about an account's health. Rows
    # only - the windowed Usage section stays on /accounts (dev/changelog/1063). Unlike
    # section_context() this commits nothing, so it does not have to run before the queries
    # above; see its docstring.
    cfg = load_config()
    acct_stats = account_stats_view.rows_context(cfg, accounts)
    health_check = get_active_run_summary()
    # What the Channel health section says while nothing runs: when the guide's own check
    # next tests anything. It used to send a new user to Channel Groups to start one, beside
    # a health check that already existed (dev/changelog/1289).
    guide_check = None
    if health_check is None:
        from ..database import OnDemandTestJob
        sys_job = OnDemandTestJob.query.filter_by(is_system=True).first()
        if sys_job is not None:
            guide_check = (sys_job.name,
                           schedule_phrase(sys_job, cfg.get('channel_testing', {})))
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
        guide_check=guide_check,
        acct_stats=acct_stats,
        next_sync=next_sync_map(accounts),
        live=live, upcoming=upcoming, now=now, live_stats=_live_row_stats(live, now),
        postcapture_cells=_postcapture_row_cells(live),
        rec_status_class=REC_ROW_STATUS_CLASS,
        # Per row rather than through the rec_status_label filter, because the parked
        # derivation needs a second column off the row. Built from the rows already
        # loaded - a dict build, no query (tests/test_scaling_pages.py::test_dashboard_page).
        rec_status_text={r.id: rec_status_display(
            r.status, waiting=bool(r.postprocess_waiting_since))[3] for r in active},
        rec_waiting_ids={r.id for r in active
                         if rec_is_waiting(r.status, r.postprocess_waiting_since)},
        section_defs=DASHBOARD_SECTIONS, section_order=order, section_on=enabled,
        section_pref_key=DASHBOARD_SECTIONS_PREF,
        # The signals dashboard.js swaps its self-refreshing regions on. Each signature is the
        # same one /api/nav-status carries, so the comparison is between two readings of one
        # function rather than between a page and a lookalike. The health kind is handed over
        # for the same reason the status labels above are: a client decision keyed on a
        # string the server owns.
        sync_sig=sync_sig,
        rec_sig=rec_sig,
        setup=setup,
        bg_health_kind=BG_KIND_HEALTH_CHECK,
        metrics=_metric_tiles(active, accounts, now, finished),
        timeline=_timeline_payload(active, accounts, now, finished),
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
        # The first yield sits inside the try: a client that leaves before the first event
        # or keepalive closes the generator right here, and only the finally unsubscribes
        # its queue (dev/docs/BUGS.md 2026-10-19 @ 04:39:29 PM).
        try:
            yield ': connected\n\n'
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
    dashboard_bg_tasks = []

    # Channel health test
    if ct['is_running']:
        total = ct.get('total_channels') or 0
        done = ct.get('completed_channels') or 0
        ch_name = ct.get('current_channel_name', '')
        if ch_name:
            detail = f'Testing {ch_name} ({done + 1} of {total})' if total else f'Testing {ch_name}'
        else:
            detail = f'{done} of {total} channels tested' if total else 'Starting…'
        bg_tasks.append({'kind': BG_KIND_HEALTH_CHECK,
                         'label': 'Channel health test', 'detail': detail,
                         'href': url_for('jobs.jobs_page')})
        dashboard_bg_tasks.append(bg_tasks[-1])

    # Account sync
    syncing_accounts = Account.query.filter_by(status=ACCOUNT_STATUS_SYNCING).all()
    for acc in syncing_accounts:
        progress = get_sync_progress(acc.id)
        if progress and progress['phase'] == 'channels' and progress['total']:
            detail = f"Syncing {acc.name} - {progress['done']:,} of {progress['total']:,} channels"
        elif progress and progress['phase'] == 'epg':
            detail = f"Syncing {acc.name} - {progress['done']:,} EPG entries"
        else:
            detail = f'Syncing {acc.name}'
        bg_tasks.append({'kind': BG_KIND_ACCOUNT_SYNC,
                         'label': 'Account sync', 'detail': detail,
                         'href': url_for('accounts.account_detail', account_id=acc.id)})
        dashboard_bg_tasks.append(bg_tasks[-1])

    # An EPG source refreshing on its own - its interval job or Refresh now. One inside an
    # account's sync is that sync's work and already shown above.
    syncing_ids = {acc.id for acc in syncing_accounts}
    for src in EpgSource.query.filter(EpgSource.refresh_started_at.isnot(None)).all():
        if src.owner_account_id in syncing_ids:
            continue
        bg_tasks.append({'kind': BG_KIND_EPG_SOURCE_REFRESH,
                         'label': 'EPG source refresh', 'detail': f'Refreshing {src.name}',
                         'href': url_for('accounts.account_detail',
                                         account_id=src.owner_account_id)})
        dashboard_bg_tasks.append(bg_tasks[-1])

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
            if rec_is_waiting(status_val, r.postprocess_waiting_since):
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
            bg_tasks.append({'kind': BG_KIND_POST_CAPTURE,
                             'label': row_label, 'detail': detail,
                             'href': url_for('recordings.recording_detail', recording_id=r.id)})
            dashboard_bg_tasks.append(bg_tasks[-1])

    # Search index rebuild - the window between "sync complete" and "index rebuilt" (up to
    # ~90s) used to have no indicator at all, even though real work was still happening
    # (dev/changelog/461). Not part of dashboard_bg_tasks: no Dashboard page section shows it.
    from ..search_index import rebuilding_index_names
    for name in rebuilding_index_names():
        bg_tasks.append({'kind': BG_KIND_SEARCH_INDEX,
                         'label': 'Search index rebuild', 'detail': f'Rebuilding {name} index',
                         'href': url_for('system.maintenance') + '#m-index'})

    # The admission registry (app/admission.py) is the only record of a maintenance job -
    # retention sweeps and the daily database maintenance leave no row anywhere else, so
    # this chip was silent for their whole duration while they held the database axis. The
    # other three kinds are deliberately NOT read from here: tester, sync and rebuild each
    # already have a row above built from the database, with progress detail the registry
    # does not carry, and reporting them twice would say the same work is happening twice.
    # Not part of dashboard_bg_tasks - maintenance has no Dashboard page section, same as
    # the rebuild entry above.
    from ..admission import KIND_MAINTENANCE, describe_active
    admission_held = describe_active()
    for kind, label, _age in admission_held:
        if kind == KIND_MAINTENANCE:
            bg_tasks.append({'kind': BG_KIND_MAINTENANCE,
                             'label': 'Database maintenance', 'detail': label or 'Running',
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
        from ..scheduler import get_scheduler, scheduler_is_live
        scheduler = get_scheduler()
        # A dead loop's registered jobs are not upcoming; readiness says why the chip is
        # gone (dev/changelog/1114).
        if scheduler and scheduler_is_live():
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
                    sys_job.name, url_for('jobs.jobs_page'))

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
        'dashboard_activity_count': len(live_recs) + len(dashboard_bg_tasks),
        # What that count is counting, one line each, for the badge's tooltip: a "1" that
        # came and went during a first sync with nothing to say it was the sync is the
        # defect dev/changelog/1292 fixed.
        'dashboard_activity_labels': (
            [f'{_WINDOW_OPEN_LABEL[r.status]}: {r.name}' for r in live_recs]
            + [f"{t['label']}: {t['detail']}" for t in dashboard_bg_tasks]),
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
    its rows were rendered at to know when to re-render itself. `alert_signature` does the
    same job for /alerts (dev/changelog/1131), and `recording_signature` for the Dashboard's
    recording regions (dev/changelog/1143). `health_check` is
    `channel_tester.health_check_signature()`, which the Groups list and the Channel page
    follow a test run on (dev/changelog/1158).
    """
    from .system import _system_stats_dict
    from ..channel_tester import health_check_signature
    from .alerts import _unread_alert_summary, alert_signature
    from ..readiness import nav_summary
    sync_sig = sync_signature()
    # The sidebar's Recordings count, the same COUNT inject_globals renders it from.
    recordings_total = db.session.query(func.count(Recording.id)).scalar() or 0
    payload = dict(
        stats=_system_stats_dict(),
        alerts=_unread_alert_summary(),
        activity=_activity_status_dict(),
        search=_search_readiness_dict(),
        account_sync=sync_sig,
        alert_signature=alert_signature(),
        recording_signature=recording_signature(),
        recordings_total=recordings_total,
        health_check=health_check_signature(),
    )
    # The Get started link's count. Cached like Readiness's, and keyed on the sync signature
    # and the recording count read just above, so a first sync or a first recording moves it
    # at the next poll. Before Readiness's own: an evaluation here refreshes that cache too,
    # so the two never cost two evaluations in one poll.
    payload['setup'] = setup_steps.nav_summary(sync_sig, recordings_total)
    # Cached for 30s inside nav_summary(), and never able to run an on-demand check - a poll
    # that spawned a process or opened a provider connection would be the exact thing the
    # Readiness card refuses to do on page load (dev/changelog/950).
    payload['readiness'] = nav_summary()
    return jsonify(**payload)


