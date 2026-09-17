import os
import threading
from datetime import datetime

from flask import (
    Blueprint, request, jsonify, redirect,
    send_from_directory, current_app, url_for,
)
from sqlalchemy import func

from .. import db
from ..database import (
    Channel, ChannelTest, OnDemandTestJob, HealthCheckProfile,
    ChannelGroup, ChannelGroupMember, Recording,
    TEST_STATUS_COMPLETED, TEST_STATUS_CANCELLED, TEST_STATUS_FAILED,
)
from ..accounts import (
    duplicates_within, duplicate_groups_within, lifecycle_states_for_channels,
    transfer_channel_state,
)
from ..channel_groups import (effective_score, check_target_channels, teardown_test_job,
                              set_participation, build_group_with_members,
                              group_name_conflict, evaluate_and_reconcile_group,
                              group_live_recordings, group_scheduled_recordings,
                              cancel_scheduled_recordings, member_channels,
                              deregister_cancelled_recordings)
from .. import channel_hiding
from ..config import load_config, config_default
from ..db_utils import retry_on_locked
from ..logo_cache import resolve_logo_url
from ..tz_utils import to_naive_utc, parse_local_to_utc, format_local, parse_hhmm, format_clock

channel_tests_bp = Blueprint('channel_tests', __name__)


def _test_status_label(t) -> str:
    """PASS/WARN/FAIL/CANCELLED/WAITING for a ChannelTest - the single derivation shared by
    every surface. Exposed to Jinja as the `test_status_label` filter and mirrored in JS by
    util.js::testStatusLabel(); the three template copies and two JS copies this replaced
    all rendered a CANCELLED test as "FAIL". Callers layer their own context-only states
    (DISABLED, TESTING) on top of the result.

    Pure function of its argument - no config, disk, or ORM access - so it is safe inside a
    `{% for %}` loop (CLAUDE.md, no hidden I/O in per-row loops)."""
    if t is None:
        return 'WAITING'
    if t.status == TEST_STATUS_COMPLETED:
        return 'WARN' if t.error_detail else 'PASS'
    if t.status == TEST_STATUS_CANCELLED:
        return 'CANCELLED'
    return 'FAIL'


def _suggested_keep_id(group, lifecycle_by_channel):
    """Which member of a duplicate set to pre-select as the keeper: never one the provider
    has removed (lifecycle 'missing') while a non-removed member exists in the group - a
    channel the provider dropped is never the right default to keep. Within whatever's
    left, one that's already in the TV Guide wins outright; otherwise the highest effective
    health score, tie-broken by oldest id (groups arrive id-ascending from
    duplicate_groups_within)."""
    pool = [ch for ch in group
            if lifecycle_by_channel.get(ch.id, (None, None))[0] != 'missing'] or group
    in_guide = [ch for ch in pool if ch.in_guide]
    candidates = in_guide or pool
    return min(candidates, key=lambda ch: (-effective_score(ch), ch.id)).id


def _serialize_dup_groups(groups, cfg, tests_by_channel=None, disabled_ids=frozenset(),
                           lifecycle_by_channel=None):
    """Serialize duplicate_groups_within() output into JSON-friendly dicts for the
    Remove Duplicates modal.

    tests_by_channel/disabled_ids are optional because only the health-check callers have
    them; without them the modal simply omits the per-row test line and DISABLED badge.

    Lifecycle state (provider-removed / new) is computed once over every channel across
    every group, never per-row, matching lifecycle_states_for_channels' own batching
    contract. A caller that already computed it for the same request (group_detail_rows,
    which also needs it for the row table and the missing-channels notice) can hand it
    over via `lifecycle_by_channel` instead of paying for a second identical query.
    """
    tests_by_channel = tests_by_channel or {}
    if lifecycle_by_channel is None:
        all_channels = [ch for group in groups for ch in group]
        lifecycle_by_channel = lifecycle_states_for_channels(all_channels, cfg)
    return [
        {
            'suggested_keep_id': _suggested_keep_id(group, lifecycle_by_channel),
            'channels': [
                {
                    'channel_id': ch.id,
                    'channel_name': ch.name,
                    'account_name': ch.account.name,
                    'in_guide': ch.in_guide,
                    'category_name': ch.category_name or '',
                    'score': effective_score(ch),
                    'health_score_sample_count': ch.health_score_sample_count,
                    'disabled': ch.id in disabled_ids,
                    # Groups are id-ascending, so index 0 is the longest-standing entry.
                    'is_oldest': ci == 0,
                    'test_status': _test_status_label(tests_by_channel.get(ch.id)),
                    'tested_et': format_local(
                        getattr(tests_by_channel.get(ch.id), 'test_started_at', None),
                        'monthday_time', none_value=None,
                    ),
                    'lifecycle': (lc := lifecycle_by_channel.get(ch.id, (None, None)))[0],
                    'lifecycle_date': lc[1].strftime('%Y-%m-%d') if lc[1] else '',
                }
                for ci, ch in enumerate(group)
            ],
        }
        for group in groups
    ]


def _fmt_audio(t) -> str | None:
    """Format audio metadata as compact string e.g. 'AAC 2ch 48kHz'."""
    if not t or not t.audio_codec:
        return None
    parts = [t.audio_codec.upper()]
    if t.audio_channels:
        parts.append(f'{t.audio_channels}ch')
    if t.audio_sample_rate:
        parts.append(f'{t.audio_sample_rate // 1000}kHz')
    return ' '.join(parts)


# Sentinel for _latest_tests_by_channel: latest test regardless of job -
# used by the TV Guide health dots and the Browse tab.
ANY_JOB = object()


def _latest_tests_by_channel(channel_ids, for_job_id=ANY_JOB):
    """Return dict of channel_id → ChannelTest (latest per channel).

    for_job_id=<id>     → tests for this specific job (incl. the system guide job)
    for_job_id=ANY_JOB  → latest test regardless of job

    The _m004 backfill closed the old NULL scope (pre-unification guide tests), but
    job_id NULL did NOT stop existing: a recording's pre-check writes its tests with no
    job, identified by `pre_check_recording_id` instead. Those rows are reachable only
    through ANY_JOB, so they feed the format lock and every any-job surface while being
    invisible to every job-scoped one. That is deliberate - a pre-check is a real, and
    usually the freshest, measurement of the feed - but it means `for_job_id=None` is
    never the same thing as "no job selected": it filters on `job_id IS NULL` and returns
    only pre-check rows. Callers with no job in hand pass ANY_JOB.
    """
    if not channel_ids:
        return {}
    max_id_q = (
        db.session.query(func.max(ChannelTest.id).label('max_id'))
        .filter(ChannelTest.channel_id.in_(channel_ids))
    )
    if for_job_id is not ANY_JOB:
        max_id_q = max_id_q.filter(ChannelTest.job_id == for_job_id)
    sub = max_id_q.group_by(ChannelTest.channel_id).subquery()
    tests = ChannelTest.query.filter(
        ChannelTest.id.in_(db.session.query(sub.c.max_id))
    ).all()
    return {t.channel_id: t for t in tests}


def _build_test_dict(t, test_duration_seconds=config_default('channel_testing.test_duration_seconds')):
    """Serialize a ChannelTest to a JSON-safe dict for API responses."""
    if t is None:
        return None
    return {
        'id': t.id,
        'status': t.status,
        'started_et': format_local(t.test_started_at, 'monthday_time', none_value=None),
        # Naive-UTC ISO string so the client can compute test age (the "not tested in 24h"
        # filter). JS appends 'Z' when parsing, matching the rest of this page's timestamps.
        'tested_at': t.test_started_at.isoformat() if t.test_started_at else None,
        'resolution': t.resolution,
        'fps': round(t.fps, 1) if t.fps else None,
        'frame_count': t.frame_count,
        'frame_pct': round(t.frame_pct, 1) if t.frame_pct is not None else None,
        'bitrate_kbps': round(t.bitrate_kbps, 1) if t.bitrate_kbps else None,
        'drop_count': t.drop_count,
        'duration_seconds': round(t.duration_seconds, 0) if t.duration_seconds else None,
        'connect_attempts': t.connect_attempts or 1,
        'screenshot_filename': os.path.basename(t.screenshot_path) if t.screenshot_path else None,
        'screenshot_pruned': bool(t.screenshot_pruned),
        'error_detail': t.error_detail,
        'audio_codec': t.audio_codec,
        'audio_channels': t.audio_channels,
        'audio_sample_rate': t.audio_sample_rate,
        'audio_bitrate_kbps': round(t.audio_bitrate_kbps, 1) if t.audio_bitrate_kbps else None,
        'audio_language': t.audio_language,
        'audio_summary': _fmt_audio(t),
        # Stream quality profile (DESIGN-stream-quality-profile.md) - informational only.
        'video_codec': t.video_codec,
        'pix_fmt': t.pix_fmt,
        'bit_depth': t.bit_depth,
        'chroma_subsampling': t.chroma_subsampling,
        'interlaced': t.interlaced,
        'coded_resolution': t.coded_resolution,
        'is_vfr': t.is_vfr,
        'bits_per_pixel_frame': round(t.bits_per_pixel_frame, 4) if t.bits_per_pixel_frame else None,
        'timeline_gap_count': t.timeline_gap_count,
        'timeline_gap_seconds': round(t.timeline_gap_seconds, 1) if t.timeline_gap_seconds else None,
        'expected_duration_seconds': test_duration_seconds,
    }


def _channel_result_row(ch, t, test_duration_seconds, dup_titles, **extra):
    """Common per-channel row for test-results JSON endpoints; extras are caller-specific keys."""
    row = {
        'channel_id': ch.id,
        'channel_name': ch.name,
        'category_name': ch.category_name or '',
        'logo_url': resolve_logo_url(ch),
        'account_id': ch.account_id,
        'account_color': ch.account.color,
        'account_name': ch.account.name,
        'last_test': _build_test_dict(t, test_duration_seconds),
        'test_duration_seconds': test_duration_seconds,
        'health_score': ch.health_score,
        'manual_health_adjustment': ch.manual_health_adjustment,
        'health_score_sample_count': ch.health_score_sample_count,
        'duplicate_title': dup_titles.get(ch.id),
    }
    row.update(extra)
    return row


def get_job_result_counts_batch(job_ids, channel_ids):
    """{job_id: {channel_id: latest ChannelTest}} for every (job, channel) pair at once.

    The per-job `get_job_result_counts()` below is one query per job, which is an N+1 on
    any page listing several checks - the Groups tab reaches it once per attached check
    (caught by tests/test_scaling_pages.py::test_groups_page_with_attached_checks). Grouping the
    max-id subquery by (job_id, channel_id) instead of channel_id alone answers all of
    them in one round trip; the per-job function stays for single-job callers.
    """
    if not job_ids or not channel_ids:
        return {}
    sub = (db.session.query(func.max(ChannelTest.id).label('max_id'))
           .filter(ChannelTest.channel_id.in_(channel_ids),
                   ChannelTest.job_id.in_(job_ids))
           .group_by(ChannelTest.job_id, ChannelTest.channel_id).subquery())
    out = {}
    for t in ChannelTest.query.filter(ChannelTest.id.in_(db.session.query(sub.c.max_id))).all():
        out.setdefault(t.job_id, {})[t.channel_id] = t
    return out


def tally_tests(tests):
    """Public name for the pass/warn/fail tally, for callers that already hold the tests
    (the batched path above) and must not re-query for them."""
    return _tally(tests)


def get_job_result_counts(job_id, channel_ids):
    """Pass/warn/fail counts for an on-demand job, one tally per channel (latest test only -
    a channel re-tested via Test Again/Resume can have multiple ChannelTest rows for the
    same job)."""
    tests = _latest_tests_by_channel(channel_ids, for_job_id=job_id).values()
    return _tally(tests)


def _tally(tests):
    # An unfinished row is the test running RIGHT NOW and is not a result yet. The row is
    # inserted with status=FAILED as a placeholder before ffmpeg spawns and only gets its
    # real status from _finalize_test(), so counting it reported the channel under test as
    # a failure while it was still connecting - the summary bar drew a red segment that
    # turned green seconds later (dev/docs/BUGS.md 2026-08-28 06:14 pm). test_ended_at is the
    # authority for "in progress" the same way it is for scheduler.py's startup sweep, which
    # closes as CANCELLED any row that outlived the process that opened it - so a row cannot
    # sit here excluded forever.
    tests = [t for t in tests if t.test_ended_at is not None]
    # CANCELLED tests are excluded entirely, tested_count included: they produced no result
    # about the channel. Counting them in tested_count would leave pass+warn+fail short of
    # the total and silently under-fill the percentage bars in _macros.html.
    passed = sum(1 for t in tests if t.status == TEST_STATUS_COMPLETED and not t.error_detail)
    warned = sum(1 for t in tests if t.status == TEST_STATUS_COMPLETED and t.error_detail)
    failed = sum(1 for t in tests if t.status == TEST_STATUS_FAILED)
    cancelled = sum(1 for t in tests if t.status == TEST_STATUS_CANCELLED)
    return {'tested_count': len(tests) - cancelled, 'pass_count': passed,
            'warn_count': warned, 'fail_count': failed}


def get_active_run_summary():
    """Summary of the currently active channel-test run, for the Live Dashboard widget.
    Returns None if no run is active. run_kind names which kind: an OnDemandTestJob run
    (the automatic guide run is the is_system job) or a pre-recording health check
    (DESIGN-prerecord-checks.md §3), which belongs to no job at all."""
    from ..channel_tester import get_status
    status = get_status()
    if not status['is_running']:
        return None

    if status['run_kind'] == 'pre_check':
        rec = db.session.get(Recording, status.get('pre_check_recording_id') or 0)
        if rec is None:
            return None
        return {
            'active': True,
            'kind': 'pre_check',
            'name': f'Pre-check: {rec.name}',
            'detail_url': url_for('recordings.recording_detail', recording_id=rec.id),
            'completed_channels': status['completed_channels'],
            'total_channels': status['total_channels'] or 1,
            'run_started_at': status['run_started_at'],
            'tested_count': 0, 'pass_count': 0, 'warn_count': 0, 'fail_count': 0,
        }

    job = db.session.get(OnDemandTestJob, status.get('current_job_id') or 0)
    if job is None:
        return None
    channels, _disabled = job_channel_lists(job)

    return {
        'active': True,
        'kind': 'guide' if job.is_system else 'on_demand',
        'name': job.name,
        'detail_url': url_for('channels.health_check_detail', job_id=job.id),
        'completed_channels': status['completed_channels'],
        'total_channels': status['total_channels'],
        'run_started_at': status['run_started_at'],
        **get_job_result_counts(job.id, [ch.id for ch in channels]),
    }


def _fmt_et(dt_utc):
    return format_local(dt_utc, 'short_datetime', none_value=None)


_RECUR_DAY_LABELS = {
    0: 'Every day', 1: 'Sunday', 2: 'Monday', 3: 'Tuesday',
    4: 'Wednesday', 5: 'Thursday', 6: 'Friday', 7: 'Saturday',
}


def _recur_label(job, ct_cfg=None):
    """Human-readable recurrence description, e.g. 'Every day at 2:00 AM' or (for a
    maintenance-window job) 'Every day in the maintenance window (2:00-6:00 AM)'; None if
    the job isn't recurring.

    ct_cfg (the channel_testing config sub-dict) is only consulted for a window job's
    start/end times - pass the precomputed value from any per-row loop caller (CLAUDE.md
    no-hidden-I/O-in-per-row-loops); single-job callers may omit it and pay one load_config()."""
    if not job.recurring:
        return None
    day = _RECUR_DAY_LABELS.get(job.recur_day, 'Every day')
    if job.recur_use_window:
        if ct_cfg is None:
            ct_cfg = load_config().get('channel_testing', {})
        from ..check_window import window_bounds
        start_t, end_t = window_bounds(ct_cfg)
        window_str = f'{format_clock(start_t.hour, start_t.minute)}-{format_clock(end_t.hour, end_t.minute)}'
        label = f'{day} in the maintenance window ({window_str})'
    else:
        h = job.recur_hour or 0
        mnt = job.recur_minute or 0
        label = f'{day} at {format_clock(h, mnt)}'
    if job.recur_paused:
        label += ' (Paused)'
    return label


# ── Legacy URL redirects ─────────────────────────────────────────────────────

@channel_tests_bp.route('/channel-tests')
def channel_tests():
    """Old URL - moved to the consolidated Channels hub."""
    return redirect(url_for('channels.channels_health_checks'))


@channel_tests_bp.route('/channel-tests/guide')
def guide_channel_tests():
    """Old URL - moved to the consolidated Channels hub."""
    return redirect(url_for('channels.channels_health_checks_guide'))


@channel_tests_bp.route('/api/channel-tests/status')
def channel_tests_status():
    """Live tester-module state - polled by the health-check detail page while a run is
    active (lighter than the per-job /results endpoint)."""
    from ..channel_tester import get_status
    return jsonify(get_status())


@channel_tests_bp.route('/api/channel-tests/active-run')
def channel_tests_active_run():
    """Summary of the currently active run for the Live Dashboard."""
    return jsonify(get_active_run_summary() or {'active': False})


@channel_tests_bp.route('/api/channel-tests/formats')
def channel_measured_formats():
    """The measured format of each requested channel: {id: {resolution, fps}}.

    A channel's format is its latest health test's resolution + fps and nothing else - no
    test, no format, never a guess, the same rule app/channel_groups.py applies. A channel
    with no measurement is simply absent from the map, which is what lets a caller tell
    "unknown" from "proven different"; those are not the same claim, and only the second
    one is ever filtered on (DESIGN-channel-groups-model.md 5).

    Exists for the group-create flow, whose picked-channel list names each channel's
    format and whose one true warning has to bucket the selection against a group's stored
    lock before anything is written (dev/changelog/831). The channel search's own row
    payload deliberately carries neither field, and widening it for one modal would put
    two joins on every row of a 136,130-channel search.
    """
    raw = (request.args.get('channel_ids') or '').split(',')
    ids = []
    for part in raw:
        part = part.strip()
        if part.isdigit():
            ids.append(int(part))
    # The id list arrives from the client, so it is bounded here the same way the delete
    # scopes are - this is one modal's selection, not a sweep.
    if len(ids) > 5000:
        return jsonify({'error': 'Too many channels requested'}), 400
    formats = {}
    for cid, t in _latest_tests_by_channel(ids).items():
        if t.resolution:
            formats[str(cid)] = {'resolution': t.resolution, 'fps': t.fps}
    return jsonify({'success': True, 'formats': formats})


@channel_tests_bp.route('/api/channel-tests/window-plan')
def channel_tests_window_plan():
    """The maintenance window's capacity payload (app/check_window.py::window_plan) plus
    its bounds - feeds both the create/edit check modal's over-capacity warning and the
    settings page's "is my window big enough" line, so the capacity math has one home."""
    from ..check_window import window_bounds, window_plan
    from ..tz_utils import format_clock

    ct_cfg = load_config().get('channel_testing', {})
    start_t, end_t = window_bounds(ct_cfg)
    plan = window_plan(ct_cfg)
    return jsonify({
        'success': True,
        'start': f'{start_t.hour:02d}:{start_t.minute:02d}',
        'end': f'{end_t.hour:02d}:{end_t.minute:02d}',
        'start_label': format_clock(start_t.hour, start_t.minute),
        'end_label': format_clock(end_t.hour, end_t.minute),
        **plan,
    })


@channel_tests_bp.route('/channel-tests/screenshots/<path:filename>')
def serve_screenshot(filename):
    cfg = load_config()
    screenshot_dir = cfg.get('channel_testing', {}).get(
        'screenshot_dir', '/dvr/channel_test_screenshots'
    )
    return send_from_directory(screenshot_dir, filename)


def job_channel_lists(job):
    """(ordered channel list, disabled-id set) for a job's display/run bookkeeping -
    the job's attached group's membership (channel_groups.check_target_channels;
    computed live for the system group, stored rows otherwise). A job left with no
    group (shouldn't happen post-_m011) lists empty."""
    if job.group is None:
        return [], set()
    return check_target_channels(job.group)


def _editable_job_group_or_error(job):
    """(group, None) when a job's channel list may be edited through the job routes,
    else (None, (json, status)). The system group is the one exclusion: its membership is
    computed from the guide rather than stored, so there is no row here to edit."""
    group = job.group
    if group is None:
        return None, (jsonify({'error': 'This check has no attached group'}), 409)
    if group.is_system:
        return None, (jsonify({'error': 'The TV Guide Channels check tests whatever is in the guide - manage channels from the guide instead'}), 400)
    return group, None


# ── On-demand job routes ──────────────────────────────────────────────────────

def _start_job_run(job_id, subset=None, force=False):
    """Mark an on-demand job RUNNING and spawn its background test thread.

    Shared tail of create/start/resume/test-selected/restart: each of those routes
    checks its own already-running/status preconditions first, then calls this. subset
    is the channel_id_subset passed through to run_on_demand_test_job (resume and
    test-selected only)."""
    from ..channel_tester import run_on_demand_test_job

    @retry_on_locked()
    def _mark_running_and_commit():
        j = db.session.get(OnDemandTestJob, job_id)
        j.status = 'RUNNING'
        j.completed_at = None
        db.session.commit()

    _mark_running_and_commit()

    app = current_app._get_current_object()
    threading.Thread(
        target=run_on_demand_test_job,
        args=(app, job_id),
        kwargs={'channel_id_subset': subset, 'force': force},
        daemon=True,
        name=f'od-test-job-{job_id}',
    ).start()


@channel_tests_bp.route('/api/channel-tests/on-demand', methods=['POST'])
def create_on_demand_job():
    """Create and optionally start an on-demand test job."""
    from ..channel_tester import get_status
    from ..scheduler import schedule_on_demand_job

    data = request.get_json(force=True, silent=True) or {}
    name = (data.get('name') or '').strip()
    # A health check is a schedule its group carries, so it has no name of its own to ask
    # for (DESIGN-channel-groups-model.md DECIDED 2) - the client sends `group_name`
    # instead and the job's name is derived below. `name` stays accepted for the one
    # caller that has no group to derive from: the ad hoc "Test this channel" on a
    # channel's own page (dev/changelog/831).
    group_name = (data.get('group_name') or '').strip()
    channel_ids = data.get('channel_ids') or []
    action = data.get('action', 'start')   # 'start' | 'schedule' | 'queue'
    scheduled_time_str = data.get('scheduled_time')  # ISO datetime in display tz (one-off)
    recurring = bool(data.get('recurring'))
    recur_day = data.get('recur_day')            # 0=every day, 1=Sun...7=Sat
    recur_time_str = data.get('recur_time')       # 'HH:MM' in display tz (recurring)
    use_window = bool(data.get('use_window'))     # dispatcher-owned maintenance window instead
    profile_id_raw = data.get('profile_id')
    profile_id = int(profile_id_raw) if profile_id_raw not in (None, '') else None
    if profile_id is not None and db.session.get(HealthCheckProfile, profile_id) is None:
        profile_id = None

    # Groups unification 4/4 (DESIGN.md §14.5 "Create a health check"): attaching a new
    # check to an EXISTING group re-uses that group's own membership as the job's channel
    # list, rather than spawning a duplicate group that would drift out of sync with it
    # the moment a member is added or removed later.
    attach_group_id = data.get('attach_group_id')
    attach_group = None
    if attach_group_id is not None:
        attach_group = db.session.get(ChannelGroup, attach_group_id)
        if attach_group is None:
            return jsonify({'error': 'Group not found'}), 404
        unique_ids = [ch.id for ch in check_target_channels(attach_group)[0]]
        duplicate_count = 0
        if not unique_ids:
            return jsonify({'error': 'This group has no channels to test'}), 400
    else:
        if not channel_ids:
            return jsonify({'error': 'At least one channel is required'}), 400

        # Deduplicate while preserving order
        seen = set()
        unique_ids = []
        for cid in channel_ids:
            if cid not in seen:
                seen.add(cid)
                unique_ids.append(cid)
        duplicate_count = len(channel_ids) - len(unique_ids)

        # Validate all channel IDs exist
        valid_channels = Channel.query.filter(Channel.id.in_(unique_ids)).all()
        valid_ids = {ch.id for ch in valid_channels}
        unique_ids = [cid for cid in unique_ids if cid in valid_ids]
        if not unique_ids:
            return jsonify({'error': 'None of the specified channels exist'}), 400

    # Derived, not demanded. The nameless client sends no `name` at all, and there are
    # only two shapes a job can arrive in - attached to a group, or carrying a group_name
    # to create one - so both have something to derive from. Only a request with neither
    # is unanswerable, and that is the one that still 400s.
    if not name:
        if attach_group is not None:
            name = f'{attach_group.name} - health check'
        elif group_name:
            name = f'{group_name} - health check'
        else:
            return jsonify({'error': 'Job name is required'}), 400

    # The group this request is about to mint is a real user group, named by the user on
    # the create flow's own screen, so a collision is refused here the same way
    # POST /api/channel-groups refuses one - not silently allowed to make a second group
    # with a name that already means something else.
    if attach_group is None and group_name and group_name_conflict(group_name):
        return jsonify({'error': f'A group named "{group_name}" already exists'}), 409

    if action == 'start':
        status = get_status()
        if status['is_running']:
            return jsonify({'error': 'A health check is already in progress - please wait'}), 409

    scheduled_start_time = None
    recur_hour = recur_minute = None
    if action == 'schedule':
        if recurring:
            try:
                recur_day = int(recur_day)
                if recur_day not in range(0, 8):
                    raise ValueError
                if not use_window:
                    recur_hour, recur_minute = parse_hhmm(recur_time_str)
            except (ValueError, TypeError):
                return jsonify({'error': 'Invalid recurring schedule - pick a test day and time'}), 400
        else:
            if not scheduled_time_str:
                return jsonify({'error': 'scheduled_time is required for schedule action'}), 400
            try:
                scheduled_start_time = parse_local_to_utc(scheduled_time_str)
                if scheduled_start_time <= datetime.utcnow():
                    return jsonify({'error': 'Scheduled time must be in the future'}), 400
            except ValueError:
                return jsonify({'error': 'Invalid scheduled_time format'}), 400

    # Each step below is its own retry unit rather than the whole function, so a
    # retried commit can never re-run db.session.add(job) and create a duplicate row.
    @retry_on_locked()
    def _create_job_and_commit():
        # One commit for group + membership + job: a locked-retry rolls all three
        # back together, so re-running the adds can't duplicate rows.
        if attach_group_id is not None:
            group_id_for_job = attach_group_id
        else:
            # The one builder every group-with-members goes through, so a group minted
            # here is the same object POST /api/channel-groups mints - CHANNEL_GROUPED
            # events on each member and the hiding recompute included, which this path
            # used to skip (dev/changelog/831).
            g = build_group_with_members(group_name or name, unique_ids)
            group_id_for_job = g.id
        j = OnDemandTestJob(
            name=name,
            status='QUEUED',
            group_id=group_id_for_job,
            profile_id=profile_id,
        )
        db.session.add(j)
        db.session.commit()
        return j

    job = _create_job_and_commit()
    group = db.session.get(ChannelGroup, job.group_id) if job.group_id else None
    # Same follow-up POST /api/channel-groups runs after its own create, as its own unit
    # rather than a second commit inside the closure above.
    if group is not None and attach_group_id is None:
        evaluate_and_reconcile_group(group)

    if action == 'start':
        _start_job_run(job.id)

    elif action == 'schedule':
        # Commit the recurrence fields *before* calling schedule_on_demand_job(),
        # so the jobstore write never registers a job whose recurrence the DB
        # doesn't durably have yet.
        @retry_on_locked()
        def _set_recurrence_and_commit():
            job.recurring = recurring
            job.recur_day = recur_day if recurring else None
            job.recur_use_window = use_window if recurring else False
            if recurring and not use_window:
                job.recur_hour = recur_hour
                job.recur_minute = recur_minute
            elif not recurring:
                job.recur_hour = None
                job.recur_minute = None
            job.recur_paused = False
            job.status_before_schedule = 'QUEUED'
            if not recurring:
                job.scheduled_start_time = scheduled_start_time
            db.session.commit()

        _set_recurrence_and_commit()
        aps_job_id, next_run = schedule_on_demand_job(job)

        @retry_on_locked()
        def _mark_scheduled_and_commit():
            job.status = 'SCHEDULED'
            job.scheduled_start_time = next_run
            job.scheduler_job_id = aps_job_id
            db.session.commit()

        _mark_scheduled_and_commit()

    # action == 'queue' → job stays QUEUED, nothing else to do

    scheduled_et = None
    if job.scheduled_start_time:
        scheduled_et = _fmt_et(job.scheduled_start_time)

    return jsonify({
        'success': True,
        'job_id': job.id,
        # The group this request minted (or attached to). The create flow's closing toast
        # links at it, and the id is only knowable here - the same request made it.
        'group_id': job.group_id,
        'group_name': group.name if group is not None else None,
        'detail_url': (url_for('channel_groups.group_detail', group_id=job.group_id)
                       if job.group_id else None),
        'name': job.name,
        'status': job.status,
        'channel_count': len(unique_ids),
        'duplicate_count': duplicate_count,
        'scheduled_start_time_et': scheduled_et,
        'recurring': job.recurring,
        'recur_description': _recur_label(job),
    })


@channel_tests_bp.route('/api/channel-tests/on-demand', methods=['GET'])
def list_on_demand_jobs():
    """List all health-check jobs with summary stats - the pinned system row first,
    then custom jobs newest-first."""
    jobs = OnDemandTestJob.query.order_by(
        OnDemandTestJob.is_system.desc(), OnDemandTestJob.created_at.desc()
    ).all()
    ct_cfg = load_config().get('channel_testing', {})
    result = []
    for job in jobs:
        channels, _disabled = job_channel_lists(job)
        channel_ids = [ch.id for ch in channels]
        counts = get_job_result_counts(job.id, channel_ids)

        result.append({
            'id': job.id,
            'name': job.name,
            'is_system': job.is_system,
            'status': job.status,
            'created_et': _fmt_et(job.created_at),
            'completed_et': _fmt_et(job.completed_at),
            'scheduled_et': _fmt_et(job.scheduled_start_time),
            'channel_count': len(channel_ids),
            'recurring': job.recurring,
            'recur_paused': job.recur_paused,
            'recur_description': _recur_label(job, ct_cfg),
            **counts,
        })
    return jsonify({'jobs': result})


@channel_tests_bp.route('/channel-tests/on-demand/<int:job_id>')
def on_demand_job_detail(job_id):
    """Old URL - moved to the consolidated Channels hub."""
    return redirect(url_for('channels.health_check_detail', job_id=job_id))


@channel_tests_bp.route('/api/channel-tests/on-demand/<int:job_id>/results')
def on_demand_job_results(job_id):
    """Per-channel results for an on-demand job (for live refresh)."""
    from ..channel_tester import get_status

    job = db.session.get(OnDemandTestJob, job_id)
    if job is None:
        return jsonify({'error': 'Job not found'}), 404

    cfg = load_config()
    test_duration_seconds = cfg.get('channel_testing', {}).get('test_duration_seconds', config_default('channel_testing.test_duration_seconds'))

    channels, disabled_ids = job_channel_lists(job)
    channel_ids = [ch.id for ch in channels]
    tests_by_channel = _latest_tests_by_channel(channel_ids, for_job_id=job_id)
    dup_titles = duplicates_within(channels)
    dup_groups = _serialize_dup_groups(duplicate_groups_within(channels), cfg,
                                       tests_by_channel, set(disabled_ids))

    rows = []
    for ch in channels:
        t = tests_by_channel.get(ch.id)
        rows.append(_channel_result_row(ch, t, test_duration_seconds, dup_titles,
                                        in_guide=ch.in_guide, disabled=ch.id in disabled_ids))

    tester_status = get_status()
    return jsonify({
        'results': rows,
        'dup_groups': dup_groups,
        'job_status': job.status,
        'is_this_job_running': tester_status['is_running'] and tester_status['current_job_id'] == job_id,
        'tester_status': tester_status,
    })


@channel_tests_bp.route('/api/channel-tests/on-demand/<int:job_id>/start', methods=['POST'])
def start_on_demand_job(job_id):
    """Manually start a QUEUED or SCHEDULED job immediately."""
    from ..channel_tester import get_status, imminent_recording_conflict
    from ..scheduler import cancel_on_demand_job_schedule

    job = db.session.get(OnDemandTestJob, job_id)
    if job is None:
        return jsonify({'error': 'Job not found'}), 404
    if job.status not in ('QUEUED', 'SCHEDULED'):
        return jsonify({'error': f'Job is {job.status} and cannot be started'}), 409

    status = get_status()
    if status['is_running']:
        return jsonify({'error': 'A health check is already in progress - please wait'}), 409

    data = request.get_json(force=True, silent=True) or {}
    keep_schedule = bool(data.get('keep_schedule', False))

    # Same warn + explicit override shape as manual sync (DESIGN-concurrency.md 5.4/5.5) -
    # the user is present, so they decide, but never silently.
    force = bool(data.get('force'))
    if not force:
        conflict = imminent_recording_conflict()
        if conflict:
            return jsonify({'error': f'Health check not started: {conflict}', 'conflicts': [conflict]}), 409

    # Recurring jobs keep their CronTrigger - "Start Now" is just an extra ad hoc run, the
    # regular cadence is untouched. A one-off job's schedule is cancelled here too, unless
    # the caller explicitly asked to keep it ("Run Now" with keep_schedule=True) - in that
    # case job.scheduler_job_id is left pointing at the still-registered DateTrigger, and
    # run_on_demand_test_job()'s finally block reverts back to SCHEDULED once this ad hoc
    # run finishes instead of going terminal.
    if not job.recurring and not keep_schedule:
        cancel_on_demand_job_schedule(job)
    _start_job_run(job_id, force=force)
    return jsonify({'success': True, 'job_id': job_id})


@channel_tests_bp.route('/api/channel-tests/on-demand/<int:job_id>/reschedule', methods=['POST'])
def reschedule_on_demand_job(job_id):
    """Set or update scheduled_start_time on a job - from QUEUED/COMPLETED/CANCELLED (first
    schedule, or re-scheduling a job that already ran) or SCHEDULED (editing an existing
    schedule, including flipping recurring on/off)."""
    from ..scheduler import schedule_on_demand_job, cancel_on_demand_job_schedule

    job = db.session.get(OnDemandTestJob, job_id)
    if job is None:
        return jsonify({'error': 'Job not found'}), 404
    if job.status not in ('QUEUED', 'SCHEDULED', 'COMPLETED', 'CANCELLED'):
        return jsonify({'error': f'Job is {job.status} and cannot be rescheduled'}), 409
    prior_status = job.status

    data = request.get_json(force=True, silent=True) or {}
    recurring = bool(data.get('recurring'))
    use_window = bool(data.get('use_window'))
    scheduled_start_time = None
    recur_day = recur_hour = recur_minute = None

    if recurring:
        try:
            recur_day = int(data.get('recur_day'))
            if recur_day not in range(0, 8):
                raise ValueError
            if not use_window:
                recur_hour, recur_minute = parse_hhmm(data.get('recur_time'))
        except (ValueError, TypeError):
            return jsonify({'error': 'Invalid recurring schedule - pick a test day and time'}), 400
    else:
        scheduled_time_str = data.get('scheduled_time')
        if not scheduled_time_str:
            return jsonify({'error': 'scheduled_time is required'}), 400
        try:
            scheduled_start_time = parse_local_to_utc(scheduled_time_str)
            if scheduled_start_time <= datetime.utcnow():
                return jsonify({'error': 'Scheduled time must be in the future'}), 400
        except ValueError:
            return jsonify({'error': 'Invalid scheduled_time format'}), 400

    cancel_on_demand_job_schedule(job)

    # Commit the recurrence fields *before* calling schedule_on_demand_job(), so the
    # jobstore write never registers a job whose recurrence the DB doesn't durably have yet.
    @retry_on_locked()
    def _set_recurrence_and_commit():
        job.recurring = recurring
        job.recur_day = recur_day
        job.recur_use_window = use_window if recurring else False
        # A toggle into window mode leaves recur_hour/recur_minute exactly as they were -
        # they are not meaningful while use_window is set, but restoring exact-time mode
        # later should bring back the last time the user actually picked, not 00:00.
        if recurring and not use_window:
            job.recur_hour = recur_hour
            job.recur_minute = recur_minute
        elif not recurring:
            job.recur_hour = None
            job.recur_minute = None
        job.recur_paused = False
        if not recurring:
            job.scheduled_start_time = scheduled_start_time
        if prior_status != 'SCHEDULED':
            job.status_before_schedule = prior_status
        db.session.commit()

    _set_recurrence_and_commit()
    aps_job_id, next_run = schedule_on_demand_job(job)

    @retry_on_locked()
    def _mark_scheduled_and_commit():
        job.status = 'SCHEDULED'
        job.scheduled_start_time = next_run
        job.scheduler_job_id = aps_job_id
        db.session.commit()

    _mark_scheduled_and_commit()

    return jsonify({
        'job_id': job_id,
        'status': job.status,
        'scheduled_et': _fmt_et(next_run),
        'recurring': job.recurring,
        'recur_description': _recur_label(job),
    })


@channel_tests_bp.route('/api/channel-tests/on-demand/<int:job_id>/profile', methods=['POST'])
@retry_on_locked()
def update_on_demand_job_profile(job_id):
    job = db.session.get(OnDemandTestJob, job_id)
    if job is None:
        return jsonify({'error': 'Job not found'}), 404
    raw = (request.get_json(silent=True) or {}).get('profile_id')
    profile_id = int(raw) if raw not in (None, '') else None
    if profile_id is not None and db.session.get(HealthCheckProfile, profile_id) is None:
        return jsonify({'error': 'Profile not found'}), 404
    job.profile_id = profile_id
    db.session.commit()
    return jsonify({'success': True, 'profile_id': job.profile_id})


@channel_tests_bp.route('/api/channel-tests/on-demand/<int:job_id>/unschedule', methods=['POST'])
@retry_on_locked()
def unschedule_on_demand_job(job_id):
    """Remove a job's schedule (one-off or recurring) entirely, reverting it to whatever
    status it was in before it was scheduled. Does not touch existing test results."""
    from ..scheduler import cancel_on_demand_job_schedule

    job = db.session.get(OnDemandTestJob, job_id)
    if job is None:
        return jsonify({'error': 'Job not found'}), 404
    if job.is_system:
        return jsonify({'error': 'The TV Guide Channels check is always scheduled - pause its schedule instead'}), 400
    if job.status != 'SCHEDULED':
        return jsonify({'error': f'Job is {job.status}, not SCHEDULED'}), 409

    cancel_on_demand_job_schedule(job)
    job.status = job.status_before_schedule or 'QUEUED'
    job.status_before_schedule = None
    job.recurring = False
    job.recur_day = None
    job.recur_hour = None
    job.recur_minute = None
    job.recur_paused = False
    job.recur_use_window = False
    job.window_skip_until = None
    job.scheduled_start_time = None
    db.session.commit()
    return jsonify({'success': True, 'job_id': job_id, 'status': job.status})


@channel_tests_bp.route('/api/channel-tests/on-demand/<int:job_id>/pause-schedule', methods=['POST'])
@retry_on_locked()
def pause_on_demand_schedule(job_id):
    """Pause a recurring job's schedule - keeps the recurrence settings intact but removes
    the active APScheduler job so it won't fire automatically until resumed."""
    from ..scheduler import cancel_on_demand_job_schedule

    job = db.session.get(OnDemandTestJob, job_id)
    if job is None:
        return jsonify({'error': 'Job not found'}), 404
    if job.status != 'SCHEDULED' or not job.recurring:
        return jsonify({'error': 'Only a recurring, scheduled job can be paused'}), 409
    if job.recur_paused:
        return jsonify({'error': 'Job schedule is already paused'}), 409

    cancel_on_demand_job_schedule(job)
    job.recur_paused = True
    db.session.commit()
    return jsonify({'success': True, 'job_id': job_id, 'recur_paused': True, 'recur_description': _recur_label(job)})


@channel_tests_bp.route('/api/channel-tests/on-demand/<int:job_id>/resume-schedule', methods=['POST'])
def resume_on_demand_schedule(job_id):
    """Re-enable a paused recurring job's schedule."""
    from ..scheduler import schedule_on_demand_job

    job = db.session.get(OnDemandTestJob, job_id)
    if job is None:
        return jsonify({'error': 'Job not found'}), 404
    if job.status != 'SCHEDULED' or not job.recurring or not job.recur_paused:
        return jsonify({'error': 'Job schedule is not paused'}), 409

    aps_job_id, next_run = schedule_on_demand_job(job)

    @retry_on_locked()
    def _mark_resumed_and_commit():
        job.recur_paused = False
        job.scheduler_job_id = aps_job_id
        job.scheduled_start_time = next_run
        db.session.commit()

    _mark_resumed_and_commit()
    return jsonify({
        'job_id': job_id,
        'recur_paused': False,
        'scheduled_et': _fmt_et(next_run),
        'recur_description': _recur_label(job),
    })


@channel_tests_bp.route('/api/channel-tests/on-demand/<int:job_id>/skip-next', methods=['POST'])
def skip_next_on_demand_job(job_id):
    """Skip a recurring job's next scheduled occurrence, advancing to the one after it -
    the skipped run never fires. Reuses the generic APScheduler skip_next_run() helper (also
    used by the /jobs page's other recurring jobs) and mirrors its result into
    scheduled_start_time, since that DB column (not a live scheduler lookup) is what every
    on-demand-job view displays."""
    from ..scheduler import skip_next_run

    job = db.session.get(OnDemandTestJob, job_id)
    if job is None:
        return jsonify({'error': 'Job not found'}), 404
    if (job.status != 'SCHEDULED' or not job.recurring or job.recur_paused
            or not (job.recur_use_window or job.scheduler_job_id)):
        return jsonify({'error': 'Only an active recurring schedule can skip its next run'}), 409

    if job.recur_use_window:
        # A window job has no APScheduler job to modify - window_skip_until is what
        # due_jobs() (app/check_window.py) checks instead. scheduled_start_time is always
        # exactly an occurrence start (written by next_occurrence_start), so
        # occurrence_containing resolves that occurrence's end correctly, including the
        # midnight-crossing case.
        from ..check_window import occurrence_containing, next_occurrence_start

        ct_cfg = load_config().get('channel_testing', {})
        occ = occurrence_containing(ct_cfg, job.scheduled_start_time)
        if occ is None:
            return jsonify({'error': 'Could not determine the next run'}), 500
        _occurrence_start, occurrence_end = occ
        new_next_utc = next_occurrence_start(ct_cfg, job, occurrence_end)

        @retry_on_locked()
        def _commit_window():
            job.window_skip_until = occurrence_end
            job.scheduled_start_time = new_next_utc
            db.session.commit()

        _commit_window()
        return jsonify({'success': True, 'job_id': job_id, 'scheduled_et': _fmt_et(new_next_utc)})

    new_next = skip_next_run(job.scheduler_job_id)
    if new_next is None:
        return jsonify({'error': 'Could not determine the next run'}), 500
    new_next_utc = to_naive_utc(new_next)

    @retry_on_locked()
    def _commit():
        job.scheduled_start_time = new_next_utc
        db.session.commit()

    _commit()
    return jsonify({'success': True, 'job_id': job_id, 'scheduled_et': _fmt_et(new_next_utc)})


@channel_tests_bp.route('/api/channel-tests/on-demand/<int:job_id>/force-cancel', methods=['POST'])
@retry_on_locked()
def force_cancel_on_demand_job(job_id):
    """Recover a RUNNING job when the background thread has died unexpectedly - reverts a
    recurring job back to SCHEDULED (its cadence keeps going), or CANCELLED otherwise (unless
    it was a "Run Now, keep schedule" one-off job, which also reverts to SCHEDULED)."""
    from ..channel_tester import get_status
    from ..scheduler import finalize_on_demand_job_status

    job = db.session.get(OnDemandTestJob, job_id)
    if job is None:
        return jsonify({'error': 'Job not found'}), 404
    if job.status != 'RUNNING':
        return jsonify({'error': f'Job is {job.status}, not RUNNING'}), 400

    status = get_status()
    if status['is_running'] and status['current_job_id'] == job_id:
        return jsonify({'error': 'Job is actively running - use Stop instead'}), 409

    # completed=False: the background thread died mid-run, so this was never a genuine
    # finish - the only terminal outcome here is CANCELLED (never COMPLETED).
    finalize_on_demand_job_status(job, False)
    db.session.commit()
    return jsonify({'success': True, 'job_id': job_id, 'status': job.status})


@channel_tests_bp.route('/api/channel-tests/on-demand/<int:job_id>/stop', methods=['POST'])
def stop_on_demand_job(job_id):
    """Request cancellation of a running on-demand job."""
    from ..channel_tester import request_stop, get_status

    job = db.session.get(OnDemandTestJob, job_id)
    if job is None:
        return jsonify({'error': 'Job not found'}), 404

    status = get_status()
    if not (status['is_running'] and status['current_job_id'] == job_id):
        return jsonify({'error': 'This job is not currently running'}), 409

    request_stop()
    return jsonify({'success': True})


@channel_tests_bp.route('/api/channel-tests/on-demand/<int:job_id>/resume', methods=['POST'])
def resume_on_demand_job(job_id):
    """Resume a CANCELLED job, re-testing only channels that didn't complete."""
    from ..channel_tester import get_status, imminent_recording_conflict

    job = db.session.get(OnDemandTestJob, job_id)
    if job is None:
        return jsonify({'error': 'Job not found'}), 404
    if job.status != 'CANCELLED':
        return jsonify({'error': f'Job is {job.status} - only CANCELLED jobs can be resumed'}), 400

    status = get_status()
    if status['is_running']:
        return jsonify({'error': 'A health check is already in progress - please wait'}), 409

    data = request.get_json(silent=True) or {}
    force = bool(data.get('force'))
    if not force:
        conflict = imminent_recording_conflict()
        if conflict:
            return jsonify({'error': f'Health check not started: {conflict}', 'conflicts': [conflict]}), 409

    channels, disabled_ids = job_channel_lists(job)
    all_ids = [ch.id for ch in channels]
    # COMPLETED only: both FAILED and CANCELLED are re-tested on resume. A CANCELLED test
    # was aborted before it measured anything (recording took the slot, or the run was
    # stopped), so it is exactly the case resume exists to pick back up.
    #
    # The LATEST test per channel for this job, not any-completed-across-all-history: restart
    # deliberately keeps prior rows, so a history-wide set meant that once a job had completed
    # once, every later cancelled re-run resumed to an empty subset and the route answered
    # "All channels are already completed" for a run that had barely started.
    completed_ids = {
        cid for cid, t in _latest_tests_by_channel(all_ids, for_job_id=job_id).items()
        if t.status == TEST_STATUS_COMPLETED
    }
    subset = [cid for cid in all_ids if cid not in completed_ids and cid not in disabled_ids]

    if not subset:
        return jsonify({'error': 'All channels are already completed'}), 400

    _start_job_run(job_id, subset=subset, force=force)
    return jsonify({'success': True, 'job_id': job_id, 'channels_to_test': len(subset)})


@channel_tests_bp.route('/api/channel-tests/on-demand/<int:job_id>/test-selected', methods=['POST'])
def test_selected_on_demand_job(job_id):
    """Re-test only a caller-supplied subset of this job's channels (bulk-select-then-test).

    Reuses run_on_demand_test_job's channel_id_subset path (same mechanism as resume). The
    subset is intersected with the job's own enabled channels, so a stale/forged channel id
    can't test something outside this health check.
    """
    from ..channel_tester import get_status, imminent_recording_conflict

    job = db.session.get(OnDemandTestJob, job_id)
    if job is None:
        return jsonify({'error': 'Job not found'}), 404
    if job.status == 'RUNNING':
        return jsonify({'error': 'This job is already running'}), 409

    status = get_status()
    if status['is_running']:
        return jsonify({'error': 'A health check is already in progress - please wait'}), 409

    data = request.get_json(silent=True) or {}
    force = bool(data.get('force'))
    if not force:
        conflict = imminent_recording_conflict()
        if conflict:
            return jsonify({'error': f'Health check not started: {conflict}', 'conflicts': [conflict]}), 409

    requested = data.get('channel_ids')
    if not isinstance(requested, list) or not requested:
        return jsonify({'error': 'No channels selected'}), 400
    try:
        requested_ids = {int(c) for c in requested}
    except (ValueError, TypeError):
        return jsonify({'error': 'Invalid channel_ids'}), 400

    channels, disabled_ids = job_channel_lists(job)
    testable_ids = {ch.id for ch in channels} - set(disabled_ids)
    subset = [cid for cid in requested_ids if cid in testable_ids]
    if not subset:
        return jsonify({'error': 'None of the selected channels can be tested in this health check'}), 400

    _start_job_run(job_id, subset=subset, force=force)
    return jsonify({'success': True, 'job_id': job_id, 'channels_to_test': len(subset)})


@channel_tests_bp.route('/api/channel-tests/on-demand/<int:job_id>/restart', methods=['POST'])
def restart_on_demand_job(job_id):
    """Restart a CANCELLED or COMPLETED job, re-testing all channels.

    Does not delete prior ChannelTest rows - each re-test just adds a new row, and the
    job-detail views resolve "current" results via _latest_tests_by_channel(). This keeps
    per-test history intact (visible on Channel Detail) instead of destroying it on every
    restart.
    """
    from ..channel_tester import get_status, imminent_recording_conflict

    job = db.session.get(OnDemandTestJob, job_id)
    if job is None:
        return jsonify({'error': 'Job not found'}), 404
    if job.status not in ('CANCELLED', 'COMPLETED'):
        return jsonify({'error': f'Job is {job.status} - only CANCELLED or COMPLETED jobs can be restarted'}), 400

    status = get_status()
    if status['is_running']:
        return jsonify({'error': 'A health check is already in progress - please wait'}), 409

    data = request.get_json(silent=True) or {}
    force = bool(data.get('force'))
    if not force:
        conflict = imminent_recording_conflict()
        if conflict:
            return jsonify({'error': f'Health check not started: {conflict}', 'conflicts': [conflict]}), 409

    _start_job_run(job_id, force=force)
    return jsonify({'success': True, 'job_id': job_id})


def _group_dissolved_by(job):
    """The ChannelGroup deleting `job` would destroy along with it, or None.

    An auto-created 1:1 check bag has no other owner - deleting its last job would orphan
    it with no UI to remove it. A group shared with another job stays, and so does the
    system group.

    The gate in delete_on_demand_job() and the delete itself both ask this one function
    rather than each counting jobs its own way. Two answers to "does this destroy the
    group" is how a gate ends up protecting a group the delete then takes anyway."""
    group = job.group
    if group is None or group.is_system:
        return None
    shared = (OnDemandTestJob.query
              .filter(OnDemandTestJob.group_id == group.id,
                      OnDemandTestJob.id != job.id).count())
    return None if shared else group


@channel_tests_bp.route('/api/channel-tests/on-demand/<int:job_id>', methods=['DELETE'])
def delete_on_demand_job(job_id):
    """Delete a QUEUED, SCHEDULED, COMPLETED, or CANCELLED job.

    When this is its group's last job the group goes with it, which makes this a second
    door onto the damage channel_groups.py::delete_group is gated against, so it takes
    DESIGN-channel-groups-model.md 15.1's identical split (dev/changelog/869). There is
    one kind of group since the groups unification: a bag created for a health check
    appears on the Groups page like any other, and nothing stops the user turning
    Recording on for a member, putting the group in the guide and scheduling against it.

    - **A capture under way refuses the delete outright**, with no confirm that overrides
      it. Foreign keys are off, so the delete would otherwise succeed and leave
      Recording.group_id dangling - failover_group_member() reads that as "no group" and
      stops failing over, silently, mid-recording.
    - **Scheduled recordings are named, then cancelled** once the user confirms. They
      cannot survive the group: a SCHEDULED row pointing at a deleted group fires later
      and records with no member to resolve.

    Neither gate applies when the group survives this delete - the recordings keep the
    group they were counting on, and only the check schedule goes.

    The gate runs before the closure below and the scheduler work after it, so the
    retried unit is the database write alone."""
    job = db.session.get(OnDemandTestJob, job_id)
    if job is None:
        return jsonify({'error': 'Job not found'}), 404
    if job.is_system:
        return jsonify({'error': 'The TV Guide Channels check cannot be deleted'}), 400
    if job.status == 'RUNNING':
        return jsonify({'error': 'Cannot delete a running job - stop it first'}), 409

    doomed = _group_dissolved_by(job)
    if doomed is not None:
        live = group_live_recordings(doomed)
        if live:
            rec = live[0]
            until = format_local(rec.stop_time, style='clock', none_value='an unknown time')
            return jsonify({
                'error': (f'Deleting this health check would also delete the group '
                          f'"{doomed.name}", which is recording until {until}. That would '
                          'leave the recording with nowhere to fail over to. Let it '
                          'finish, or abort it deliberately, then come back to this.'),
                'recording_in_progress': {'recording_id': rec.id, 'name': rec.name,
                                          'until': until},
            }), 409
        scheduled = group_scheduled_recordings(doomed)
        if scheduled and not (request.get_json(silent=True) or {}).get('confirm'):
            return jsonify({
                'error': ('Deleting this health check would also delete its channel group, '
                          'which has scheduled recordings that would be cancelled.'),
                'confirm_required': {
                    'group_name': doomed.name,
                    'scheduled_count': len(scheduled),
                    'scheduled': [{'recording_id': r.id, 'name': r.name,
                                   'start': format_local(r.start_time, style='clock',
                                                         none_value='an unknown time')}
                                  for r in scheduled[:5]],
                },
            }), 409

    @retry_on_locked()
    def _delete():
        j = db.session.get(OnDemandTestJob, job_id)
        group = _group_dissolved_by(j)  # before the job row goes, or it counts itself out
        cancelled, members = [], []
        if group is not None:
            detail = (f'Channel group "{group.name}" was deleted with the health check '
                      'that created it.')
            cancelled = cancel_scheduled_recordings(group_scheduled_recordings(group), detail)
            members = member_channels(group.memberships)
        screenshot_paths = teardown_test_job(j)
        db.session.delete(j)
        if group is not None:
            db.session.delete(group)  # membership rows go via the delete-orphan cascade
            # After the group delete, not before it: the memberships that protect these
            # channels only stop existing when that cascade flushes, and a recompute run
            # one line earlier would still see them and leave every deferred hide deferred.
            channel_hiding.recompute([ch.id for ch in members])
        db.session.commit()
        return cancelled, screenshot_paths

    cancelled, screenshot_paths = _delete()
    from ..recorder import delete_files
    delete_files(screenshot_paths)
    deregister_cancelled_recordings(cancelled)
    return jsonify({'success': True, 'cancelled_recordings': len(cancelled)})


@channel_tests_bp.route('/api/channel-tests/on-demand/<int:job_id>/channels', methods=['POST'])
@retry_on_locked()
def add_channels_to_job(job_id):
    """Add one or more channels to an existing (non-running) on-demand job's group."""
    job = db.session.get(OnDemandTestJob, job_id)
    if job is None:
        return jsonify({'error': 'Job not found'}), 404
    if job.status == 'RUNNING':
        return jsonify({'error': 'Cannot modify a running job'}), 409
    group, error = _editable_job_group_or_error(job)
    if group is None:
        return error

    data = request.get_json(force=True, silent=True) or {}
    new_ids = data.get('channel_ids') or []
    if not new_ids:
        return jsonify({'error': 'At least one channel is required'}), 400

    existing_set = {m.channel_id for m in group.memberships}
    next_pos = len(group.memberships)
    valid_ids = {ch.id for ch in Channel.query.filter(Channel.id.in_(new_ids)).all()}

    added = []
    for cid in new_ids:
        if cid in valid_ids and cid not in existing_set:
            db.session.add(ChannelGroupMember(group_id=group.id, channel_id=cid,
                                              position=next_pos + len(added)))
            existing_set.add(cid)
            added.append(cid)

    if not added:
        return jsonify({'error': 'No new valid channels to add'}), 400

    channel_hiding.recompute(added)
    db.session.commit()
    channel_count = len(existing_set)
    return jsonify({'success': True, 'job_id': job_id, 'added_count': len(added), 'channel_count': channel_count})


@channel_tests_bp.route('/api/channel-tests/on-demand/<int:job_id>/channels/<int:channel_id>', methods=['DELETE'])
def remove_channel_from_job(job_id, channel_id):
    """Remove a channel from an existing (non-running) on-demand job's group, along with
    its history."""
    job = db.session.get(OnDemandTestJob, job_id)
    if job is None:
        return jsonify({'error': 'Job not found'}), 404
    if job.status == 'RUNNING':
        return jsonify({'error': 'Cannot modify a running job'}), 409
    group, error = _editable_job_group_or_error(job)
    if group is None:
        return error

    membership = next((m for m in group.memberships if m.channel_id == channel_id), None)
    if membership is None:
        return jsonify({'error': 'Channel is not part of this job'}), 404
    if len(group.memberships) == 1:
        return jsonify({'error': 'Cannot remove the last channel from a test'}), 400

    from ..channel_tester import delete_tests_collecting_screenshots
    from ..recorder import delete_files

    @retry_on_locked()
    def _remove_and_commit():
        db.session.delete(membership)
        paths = delete_tests_collecting_screenshots(
            ChannelTest.query.filter_by(job_id=job_id, channel_id=channel_id))
        # Losing its last group membership is what lets a deferred hide finally take effect.
        channel_hiding.recompute([channel_id])
        db.session.commit()
        return paths

    delete_files(_remove_and_commit())
    return jsonify({'success': True, 'job_id': job_id, 'channel_count': len(group.memberships)})


@channel_tests_bp.route('/api/channel-tests/on-demand/<int:job_id>/remove-duplicates', methods=['POST'])
def remove_duplicate_channels(job_id):
    """Bulk-remove losing duplicate channels from a job (and its ChannelTest history),
    optionally also removing them from the TV Guide, and optionally transferring each
    removed channel's guide listing/group membership/scheduled recordings/health-check
    enrollment to the channel kept in its place (transfer_channel_state(), the same
    transfer Re-point uses)."""
    job = db.session.get(OnDemandTestJob, job_id)
    if job is None:
        return jsonify({'error': 'Job not found'}), 404
    if job.status == 'RUNNING':
        return jsonify({'error': 'Cannot modify a running job'}), 409
    group, error = _editable_job_group_or_error(job)
    if group is None:
        return error

    data = request.get_json(force=True, silent=True) or {}
    removals = data.get('removals') or []  # [{channel_id, remove_from_guide, keep_channel_id}, ...]
    transfer = bool(data.get('transfer'))
    if not removals:
        return jsonify({'error': 'No channels specified'}), 400

    by_channel = {m.channel_id: m for m in group.memberships}
    remove_ids = [r['channel_id'] for r in removals if r.get('channel_id') in by_channel]
    if not remove_ids:
        return jsonify({'error': 'None of the specified channels are in this job'}), 400
    if len(by_channel) - len(remove_ids) < 1:
        return jsonify({'error': 'Cannot remove every channel from a test'}), 400

    cfg = load_config()

    from ..channel_tester import delete_tests_collecting_screenshots
    from ..recorder import delete_files

    @retry_on_locked()
    def _remove_and_commit():
        guide_removed = []
        transferred = []
        transfer_skipped = []
        for r in removals:
            cid = r.get('channel_id')
            if cid not in remove_ids:
                continue
            ch = db.session.get(Channel, cid)
            did_transfer = False
            if transfer and ch is not None:
                keep_id = r.get('keep_channel_id')
                keeper = db.session.get(Channel, keep_id) if keep_id else None
                # Server re-validates the keeper from DB ids - never trust the client blindly
                # (same "server re-validates everything" pattern as the repoint route).
                if keeper is None or keeper.id == ch.id or keeper.id not in by_channel:
                    transfer_skipped.append({'channel_id': cid,
                                             'reason': 'No valid channel to keep in this test'})
                elif keeper.stream_url != ch.stream_url:
                    transfer_skipped.append({'channel_id': cid,
                                             'reason': 'Kept channel no longer shares the stream URL'})
                else:
                    transfer_channel_state(ch, keeper, cfg)
                    transferred.append(cid)
                    did_transfer = True
            if not did_transfer:
                db.session.delete(by_channel[cid])
                if r.get('remove_from_guide') and ch and ch.in_guide:
                    ch.in_guide = False
                    guide_removed.append(cid)
                    channel_hiding.recompute([cid])

        screenshot_paths = delete_tests_collecting_screenshots(ChannelTest.query.filter(
            ChannelTest.job_id == job_id, ChannelTest.channel_id.in_(remove_ids)
        ))
        db.session.commit()
        return guide_removed, transferred, transfer_skipped, screenshot_paths

    guide_removed, transferred, transfer_skipped, screenshot_paths = _remove_and_commit()
    delete_files(screenshot_paths)
    return jsonify({
        'success': True,
        'job_id': job_id,
        'removed': remove_ids,
        'guide_removed': guide_removed,
        'transferred': transferred,
        'transfer_skipped': transfer_skipped,
        'channel_count': len(by_channel) - len(remove_ids),
    })


@channel_tests_bp.route('/api/channel-tests/on-demand/<int:job_id>/channels/<int:channel_id>/toggle', methods=['POST'])
@retry_on_locked()
def toggle_job_channel_enabled(job_id, channel_id):
    """Enable/disable a channel within a job without removing it or its history.

    For the system TV Guide Channels job this flips Channel.test_enabled - the same flag
    the group's computed membership reads - rather than a membership row (the system
    group has none). Otherwise it flips the membership's own test_enabled, through
    channel_groups.py::set_participation() so the move reaches the group's Activity
    Timeline: this route wrote the column directly for one commit, which is the whole
    reason that helper exists (dev/changelog/748)."""
    job = db.session.get(OnDemandTestJob, job_id)
    if job is None:
        return jsonify({'error': 'Job not found'}), 404
    if job.status == 'RUNNING':
        return jsonify({'error': 'Cannot modify a running job'}), 409

    if job.group is not None and job.group.is_system:
        ch = db.session.get(Channel, channel_id)
        if ch is None or not ch.in_guide:
            return jsonify({'error': 'Channel is not part of this job'}), 404
        # participation-write-ok: Channel.test_enabled, the channel-wide off switch - a
        # different column from the group-scoped membership one below.
        ch.test_enabled = not ch.test_enabled
        db.session.commit()
        return jsonify({'success': True, 'job_id': job_id, 'channel_id': channel_id,
                        'disabled': not ch.test_enabled})

    group, error = _editable_job_group_or_error(job)
    if group is None:
        return error

    membership = next((m for m in group.memberships if m.channel_id == channel_id), None)
    if membership is None:
        return jsonify({'error': 'Channel is not part of this job'}), 404

    now_disabled = membership.test_enabled
    set_participation(membership, 'test_enabled', not membership.test_enabled,
                      surface='check_channel_list')
    db.session.commit()
    return jsonify({'success': True, 'job_id': job_id, 'channel_id': channel_id, 'disabled': now_disabled})


@channel_tests_bp.app_template_filter('test_status_label')
def test_status_label_filter(t):
    """Jinja access to the one PASS/WARN/FAIL/CANCELLED/WAITING derivation."""
    return _test_status_label(t)
