"""Manual channel grouping routes: Groups tab list, per-group detail page, group
CRUD, and the suggest-duplicates helper. Group semantics live in app/channel_groups.py."""
import collections
import json
import logging
import re

from flask import Blueprint, render_template, request, jsonify, url_for, abort
from sqlalchemy.orm import selectinload

from .. import db, health_bands, channel_hiding
from ..database import (
    Channel, ChannelGroup, ChannelGroupMember, ChannelEvent, ChannelTest, Tag,
    Recording, RecordingEvent, HealthCheckProfile, OnDemandTestJob,
    CHANNEL_GROUPED, CHANNEL_UNGROUPED, GROUP_MEMBER_SELECTED, GROUP_FAILOVER,
    CHANNEL_FAILOVER_HEALTH_OBSERVATION, CHANNEL_STALL_DEMOTION_HEALTH_OBSERVATION,
    CHANNEL_PLACEHOLDER_HEALTH_OBSERVATION, CHANNEL_FAST_DELIVERY_HEALTH_OBSERVATION,
    ChannelGroupEvent,
    GROUP_FORMAT_STRATEGIES,
    GROUP_FORMAT_HEALTH_CHECK_ONLY, GROUP_FORMAT_MANUAL,
    GROUP_WARNING_KINDS,
    group_event_channel_links,
    REC_STATUS_COMPLETED, REC_STATUS_FAILED, REC_STATUS_ABORTED,
)
from ..accounts import (
    duplicate_groups_within, duplicates_within, lifecycle_states_for_channels,
    tags_matching, transfer_channel_state,
)
from ..channel_groups import (
    effective_score, rank_members, suggest_candidates, group_reference_key,
    derived_reference_key, lock_ranking_ids,
    classify_group_formats, group_format_outliers, format_key, format_label,
    member_channels, recording_members, test_member_ids, pick_best_member,
    participation_is_recording, participating_member_ids, group_manages_format,
    PARTICIPATION_FIELDS, set_participation, format_eligible_members,
    pinned_format_offenders, set_warning_muted,
    guide_invariant_check, demote_group_from_guide, log_guide_change,
    group_scheduled_recordings,
    group_live_recordings, cancel_scheduled_recordings,
    deregister_cancelled_recordings,
    report_orphaned_guide_groups, resolve_broken_guide_row,
    evaluate_and_reconcile_group, check_target_channels, teardown_test_job,
    apply_lock_and_log, apply_format_strategy, strategy_lock_plan,
    FORMAT_STRATEGY_LABELS,
    DEFAULT_FAILING_STREAK_THRESHOLD, plan_format_selection, FORMAT_STRATEGIES,
    MATCH_REASON_STRENGTH, FORMAT_STATUS_STRENGTH,
    build_group_with_members, group_name_conflict, serving_member, touch_group,
    schedule_is_live,
)
from ..config import load_config
from ..db_utils import retry_on_locked
from ..logo_cache import resolve_logo_url
from ..tz_utils import local_input_value, format_local
from .channel_tests import (
    _serialize_dup_groups, _latest_tests_by_channel, _test_status_label,
    _channel_result_row, _tally, get_job_result_counts, get_job_result_counts_batch,
    tally_tests, _recur_label, _fmt_et,
    ANY_JOB,
)

log = logging.getLogger(__name__)

channel_groups_bp = Blueprint('channel_groups', __name__)


def _next_guide_sort_order():
    """Groups and channels share one guide ordering space - next slot after both."""
    max_ch = db.session.query(db.func.max(Channel.guide_sort_order))\
        .filter_by(in_guide=True).scalar() or 0
    max_gr = db.session.query(db.func.max(ChannelGroup.guide_sort_order))\
        .filter_by(in_guide=True).scalar() or 0
    return max(max_ch, max_gr) + 1


def _format_warnings(existing_members, new_channels):
    """The format questions for a pending create / add / clone.

    Returns (mismatch, unverified_new):

      - `mismatch` is a JSON-able {'groups': [...], 'untested': [...]} when >= 2 distinct
        KNOWN formats would coexist in the group, else None. **It is a warning, and adding
        a member is never refused over its format** (dev/changelog/762). A member whose
        format does not match is skipped where members are CHOSEN and stays exactly as the
        user left it (DESIGN-channel-groups-model.md 4.1, DECIDED 11), so a group holding a
        mix is a legal, recoverable, self-healing setup rather than a state to keep the user
        out of - the same call 4.3 already made for recording from an unmonitored member.
        The rationale the earlier hard refusal shipped with, that a mixed concat/remux
        breaks, was measured false on this machine: concat, stream-copy and re-encode all
        exit 0 on a 720p30 + 1080p60 pair (dev/changelog/754).
      - `unverified_new` is the NEW channels with no usable test, for the caller's soft
        `unverified_format` warning. Existing untested members don't warn - they were
        already accepted when they were added.

    Mismatch is decided only on channels with a KNOWN format: an untested channel is
    unknown, not proven-different, which is the same call format_eligible_members() makes
    where it matters."""
    all_members = list(existing_members) + list(new_channels)
    latest = _latest_tests_by_channel([ch.id for ch in all_members])
    cls = classify_group_formats(all_members, latest)

    mismatch = None
    if cls['distinct_known'] >= 2:
        mismatch = {
            'groups': [
                {'format': format_label(key),
                 'channels': [{'channel_id': ch.id, 'channel_name': ch.name} for ch in chans]}
                for key, chans in sorted(cls['buckets'].items(), key=lambda kv: kv[0])
            ],
            'untested': [{'channel_id': ch.id, 'channel_name': ch.name}
                         for ch in cls['untested']],
        }

    new_ids = {ch.id for ch in new_channels}
    unverified_new = [ch for ch in cls['untested'] if ch.id in new_ids]
    return mismatch, unverified_new


def _pending_warnings(existing_members, new_channels, force, ask_format):
    """Every pre-commit warning create_group and add_members raise, in one place so the
    two cannot give different answers to the same question - which they did until
    dev/changelog/762, where create refused a mixed selection outright while adding the
    identical channels to the identical group one request later succeeded.

    Empty (proceed) when `force` is set - that flag is the user's press of Proceed anyway.

    `ask_format` is the caller's strategy gate: a group whose strategy is still
    health_check_only is not a recording source, so it accepts any mix of channels and the
    format questions are asked when it is promoted (DESIGN-channel-groups-model.md 14.1),
    not while it is being assembled. The duplicate-stream warning rides the same gate
    because it says the same thing - duplicate feeds "add no failover redundancy", which is
    a sentence about a group that records.

    Warnings, all soft:
      - format_mismatch / unverified_format: _format_warnings() above
      - duplicate_warning + dup_groups: exact-stream_url duplicate sets within the would-be
        member list that involve at least one NEW channel (sets purely among existing
        members were already accepted when they were added)
    """
    if force or not ask_format:
        return {}
    warnings = {}

    new_ids = {ch.id for ch in new_channels}
    dup_sets = [
        g for g in duplicate_groups_within(list(existing_members) + list(new_channels))
        if any(ch.id in new_ids for ch in g)
    ]
    if dup_sets:
        warnings['duplicate_warning'] = True
        warnings['dup_groups'] = _serialize_dup_groups(dup_sets, load_config())

    mismatch, unverified_new = _format_warnings(existing_members, new_channels)
    if mismatch:
        warnings['format_mismatch'] = mismatch
    if unverified_new:
        warnings['unverified_format'] = [
            {'channel_id': ch.id, 'channel_name': ch.name} for ch in unverified_new]

    return warnings


def _load_channels_or_error(channel_ids):
    if not channel_ids or not isinstance(channel_ids, list):
        return None, (jsonify({'error': 'No channels specified'}), 400)
    channels = Channel.query.filter(Channel.id.in_(channel_ids)).all()
    if len(channels) != len(set(channel_ids)):
        return None, (jsonify({'error': 'One or more channels not found'}), 404)
    return channels, None


def _grouping_events(channels, group, event_type, detail):
    for ch in channels:
        db.session.add(ChannelEvent(
            channel_id=ch.id, event_type=event_type,
            detail=detail.format(group_name=group.name),
        ))


# ── Groups tab list + per-group detail ──────────────────────────────────────

def _group_view(g, latest_by_channel=None, monitored_ids=None, memberships=None,
                streak_threshold=DEFAULT_FAILING_STREAK_THRESHOLD, cfg=None):
    """Ranked members + dup info + format state for one group - shared by the group
    list, the detail page, and the Browse "In Your Guide → Groups" sub-list.

    `cfg`: pass the request's already-loaded config on any call site inside a per-group
    loop (the caller of _channel_group_row is one) - never left to fall back to
    load_config() there, per CLAUDE.md's no-hidden-I/O-in-per-row-loops rule.

    `latest_by_channel`: pass a pre-batched channel_id→latest-ChannelTest map spanning
    (at least) this group's members to avoid a per-group query on list views - it's sliced
    down to this group's members before use, so a map covering many groups is fine. When
    None (the detail page's single-group call), this runs its own scoped query.

    `monitored_ids`: pass the batched `monitored_channel_ids()` set to populate
    `unmonitored_count` (Part H drift-coverage warning) on list views without a per-group
    query. When None, `unmonitored_count` is None (the detail page computes its own).

    `memberships`: pass a pre-batched list of this group's ChannelGroupMember rows to
    avoid the per-group `g.memberships` query on list views (that relationship is
    `lazy=True`, so N groups in a loop is N queries otherwise). When None (the detail
    page's single-group call), falls back to `g.memberships`.

    Format state (lock-aware, Part F): `latest_by_channel` for this group's members,
    the effective `reference_key`/`reference_label` (locked or derived),
    `format_offenders` (the recording-enabled members off a hand-pinned format, in rank
    order - the only thing a group format warning is about, dev/changelog/925) with
    `format_override` (the pin filtered every one of them, so a recording bypasses it),
    and the empty-group `no_active_match` flag (a reference exists but no *active* member
    reports it - e.g. a locked format no live feed conforms to)."""
    cfg = cfg if cfg is not None else load_config()
    memberships = list(memberships) if memberships is not None else list(g.memberships)
    unranked = member_channels(memberships)
    member_ids = [ch.id for ch in unranked]
    if latest_by_channel is None:
        latest_by_channel = _latest_tests_by_channel(member_ids)
    else:
        # Scope the batched map to this group's members so the returned view (and its
        # stored latest_by_channel) stays per-group, same as the un-batched path.
        latest_by_channel = {cid: latest_by_channel[cid]
                             for cid in member_ids if cid in latest_by_channel}
    members = rank_members(unranked, latest_by_channel, streak_threshold=streak_threshold)

    ref_key = group_reference_key(g, memberships, latest_by_channel, streak_threshold)
    recording_ids = {m.channel_id for m in memberships if m.recording_enabled}
    tested_ids = test_member_ids(memberships)
    participating_ids = participating_member_ids(g, memberships)
    active = recording_members(memberships)
    active_matches_reference = ref_key is not None and any(
        format_key(latest_by_channel.get(ch.id)) == ref_key for ch in active)
    no_active_match = ref_key is not None and not active_matches_reference
    format_offenders = pinned_format_offenders(
        g, [ch for ch in members if ch.id in recording_ids], latest_by_channel)
    format_override = bool(format_offenders) and format_eligible_members(
        g, active, latest_by_channel).override

    # List-view summary stats: the best recording-enabled member (where a group
    # recording actually starts) and the most recent test across all members.
    disabled = [ch for ch in members if ch.id not in participating_ids]
    best_active = pick_best_member(active, latest_by_channel, streak_threshold=streak_threshold)
    tested_at = [t.test_started_at for t in latest_by_channel.values()
                 if t and t.test_started_at]
    last_tested = max(tested_at) if tested_at else None

    # Part H drift-coverage: members not in any active recurring health check (their
    # format could drift without the group's mismatch state being re-verified).
    unmonitored_count = (sum(1 for ch in members if ch.id not in monitored_ids)
                         if monitored_ids is not None else None)

    return {
        'group': g,
        'members': members,
        # Per-member participation, read by templates rather than off the channel row:
        # the two switches as id sets, so a template asks `ch.id in ...` for either,
        # plus the one that answers "taking part in what this group is FOR" - the set
        # every dimmed/disabled display reads (app/channel_groups.py).
        'recording_ids': recording_ids,
        'tested_ids': tested_ids,
        'participating_ids': participating_ids,
        'latest_by_channel': latest_by_channel,
        'scores': {ch.id: effective_score(ch) for ch in members},
        'dup_titles': duplicates_within(members),
        'dup_groups': _serialize_dup_groups(duplicate_groups_within(members), cfg),
        'reference_key': ref_key,
        'reference_label': format_label(ref_key),
        'locked': g.locked_format_key is not None,
        'format_offenders': format_offenders,
        'format_override': format_override,
        'no_active_match': no_active_match,
        'active_count': len(active),
        'disabled_count': len(disabled),
        'best_active': best_active,
        'best_active_id': best_active.id if best_active else None,
        'best_active_score': effective_score(best_active) if best_active else None,
        'last_tested': last_tested,
        'unmonitored_count': unmonitored_count,
    }


_TERMINAL_RECORDING_STATUSES = (REC_STATUS_COMPLETED, REC_STATUS_FAILED, REC_STATUS_ABORTED)

# Tooltip shown on a member whose Recording switch is off. One reason now, because
# nothing but a human turns it off (DESIGN-channel-groups-model.md 4.1) - a format
# mismatch no longer disables anything, it filters at selection time and carries its own
# per-row warning instead.
_RECORDING_OFF_TOOLTIP = ('Recording is off for this member. Excluded from this group only - '
    'still usable everywhere else, including its other groups.')
_TEST_DISABLED_TOOLTIP = ('Testing is turned off for this channel (Channels &rsaquo; toggle). '
    'It still appears in the TV Guide, but the automatic health check skips it.')


def _channel_initials(name):
    # duplicated from app/routes/recordings.py::_channel_initials - same tiny
    # letters-only-first-3 rule, not worth a shared import for two lines.
    letters = ''.join(c for c in (name or '') if c.isalnum())
    return (letters[:3] or '?').upper()


def _member_ctx(ch, latest_by_channel, disabled_tooltip, is_best):
    """One member row's worth of display fields - the same shape for every group, so the
    page renders them all with one template block."""
    t = latest_by_channel.get(ch.id)
    return {
        'id': ch.id,
        'name': ch.name,
        'url': url_for('channels.channel_detail', channel_id=ch.id),
        'logo_url': resolve_logo_url(ch) or None,
        'initials': _channel_initials(ch.name),
        'account_name': ch.account.name,
        'account_color': ch.account.color,
        'score': effective_score(ch) if ch.health_score is not None else None,
        'resolution': t.resolution if t else None,
        'fps': round(t.fps) if t and t.fps else None,
        'bitrate_kbps': round(t.bitrate_kbps, 1) if t and t.bitrate_kbps else None,
        'tested_et': _fmt_et(t.test_started_at) if t and t.test_started_at else None,
        'tested_iso': t.test_started_at.isoformat() if t and t.test_started_at else '',
        'status': _test_status_label(t),
        'disabled': disabled_tooltip,
        'is_best': is_best,
    }


def _check_ctx(job, channel_ids, inherited, tests_by_job=None, ct_cfg=None):
    """One health-check chip's worth of display fields for a group row - a job
    attached to this group (inherited=False) or the system job covering this
    channel-kind group's members incidentally (inherited=True, §14.5).

    `tests_by_job`: the batched {job_id: {channel_id: test}} map from
    `get_job_result_counts_batch()`. List views MUST pass it - without it this runs one
    query per check, which is an N+1 across the page (guarded by
    tests/test_scaling_pages.py::test_groups_page_with_attached_checks).

    `ct_cfg`: the channel_testing config sub-dict, forwarded to `_recur_label()` - any
    per-row caller must hoist it once rather than let `_recur_label` call load_config()
    itself (CLAUDE.md no-hidden-I/O-in-per-row-loops)."""
    if tests_by_job is None:
        counts = get_job_result_counts(job.id, channel_ids)
    else:
        by_channel = tests_by_job.get(job.id, {})
        counts = tally_tests([t for t in (by_channel.get(cid) for cid in channel_ids)
                              if t is not None])
    return {
        'job_id': job.id,
        'name': job.name,
        'status': job.status,
        'recurring': job.recurring,
        'recur_paused': job.recur_paused,
        'recur_description': _recur_label(job, ct_cfg),
        'scheduled_et': _fmt_et(job.scheduled_start_time),
        'completed_et': _fmt_et(job.completed_at),
        'completed_iso': job.completed_at.isoformat() if job.completed_at else '',
        'has_schedule': bool(job.recurring or job.status == 'SCHEDULED'),
        # Narrower than has_schedule, which a paused recurring job still satisfies:
        # this one is "will it actually fire", and is what a coverage claim reads.
        'schedule_live': schedule_is_live(job),
        'inherited': inherited,
        'profile_name': job.profile.name if job.profile else None,
        'detail_url': url_for('channels.health_check_detail', job_id=job.id),
        'channel_count': len(channel_ids),
        **counts,
    }


def _check_dimension(checks):
    """Group-level 'Health check' filter dimension (§14.6): sched | oneoff | none."""
    if not checks:
        return 'none'
    if any(c['has_schedule'] for c in checks):
        return 'sched'
    return 'oneoff'


def _check_health(check):
    """(class, label) for one health check's own state. Every state is named - a check
    with no run is 'Never run', not a fall-through. `check` is None when nothing is
    attached at all."""
    if check is None:
        return 'none', 'No health check attached'
    if check['status'] == 'RUNNING':
        return 'run', 'Running now'
    if check['status'] == 'CANCELLED':
        return 'none', 'Cancelled'
    if check['fail_count']:
        return 'bad', f"{check['fail_count']} failing"
    if check['warn_count']:
        return 'warn', f"{check['warn_count']} with warnings"
    if check['pass_count']:
        return 'ok', 'All passing'
    return 'none', 'Never run'


# Worst-first, so a merged pair item shows the half that needs attention. A green pair
# header sitting on top of a failing check hides the one thing the row exists to surface.
# 'run' (a check running now) is not an alarm - it ranks below 'bad'/'warn' but above the
# neutral states, distinct from 'live' (an actual in-progress recording, which this map never
# sees - dev/changelog/816).
_HEALTH_RANK = {'bad': 0, 'run': 1, 'warn': 2, 'none': 3, 'ok': 4}


def _worst_health(states):
    """`states` is a list of (class, label). Returns the worst by _HEALTH_RANK."""
    return min(states, key=lambda s: _HEALTH_RANK[s[0]])


def _last_activity(members, checks):
    stamps = [m['tested_iso'] for m in members if m['tested_iso']]
    stamps += [c['completed_iso'] for c in checks if c['completed_iso']]
    return max(stamps) if stamps else ''


def _channel_group_row(g, latest_by_channel, monitored_ids, system_job, system_channel_ids,
                       memberships, jobs, blocking_recordings, tests_by_job,
                       streak_threshold=DEFAULT_FAILING_STREAK_THRESHOLD, ct_cfg=None, cfg=None):
    view = _group_view(g, latest_by_channel, monitored_ids, memberships, streak_threshold, cfg)
    off_tooltip = (_RECORDING_OFF_TOOLTIP if participation_is_recording(g)
                   else _TEST_DISABLED_TOOLTIP)
    members = [
        _member_ctx(ch, latest_by_channel,
                    None if ch.id in view['participating_ids'] else off_tooltip,
                    ch.id == view['best_active_id'])
        for ch in view['members']
    ]
    member_ids = [ch.id for ch in view['members']]

    checks = [_check_ctx(job, member_ids, inherited=False, tests_by_job=tests_by_job, ct_cfg=ct_cfg)
              for job in jobs]
    if system_job is not None:
        covered = [cid for cid in member_ids if cid in system_channel_ids]
        if covered:
            checks.append(_check_ctx(system_job, covered, inherited=True,
                                     tests_by_job=tests_by_job, ct_cfg=ct_cfg))

    recordings = [{'id': r.id, 'name': r.name, 'status': r.status,
                  'when': _fmt_et(r.start_time)} for r in blocking_recordings]

    unmon = view['unmonitored_count'] or 0
    hscore = round(g.health_score) if g.health_score is not None else None
    band = health_bands.band_for(hscore, health_bands.resolve_bands(cfg or {}))
    offenders = view['format_offenders']
    if offenders:
        health_cls, health_label = 'bad', 'Mixed format'
    elif not members:
        health_cls, health_label = 'none', 'No channels'
    elif band == health_bands.POOR:
        health_cls, health_label = 'bad', f'Poor health ({hscore})'
    elif band == health_bands.FAIR:
        health_cls, health_label = 'warn', f'Fair health ({hscore})'
    # "Not monitored" outranks a healthy score but not an unhealthy one: a group nothing
    # tests has a score that is merely old, so it must not paint the row green - but it is
    # still less urgent than a score that is actively bad.
    elif unmon:
        health_cls, health_label = 'warn', 'Not monitored'
    elif band == health_bands.UNTESTED:
        health_cls, health_label = 'none', 'No health data'
    elif band == health_bands.GOOD:
        health_cls, health_label = 'ok', f'Good health ({hscore})'
    elif band == health_bands.GREAT:
        health_cls, health_label = 'ok', f'Great health ({hscore})'
    else:
        # Not the rendering of any real band - every one is named above. Reachable only if a
        # band is added to health_bands.BAND_KEYS without a branch here, which is exactly the
        # silent-swallow this shape exists to make loud.
        log.warning('Group %s has health band %r with no branch - showing it unbanded', g.id, band)
        health_cls, health_label = 'none', f'Health {hscore}'

    issues = []
    # The token keeps its old spelling so a saved `?issue=mismatch` link still filters;
    # what it matches is the badge's condition, not "any member differs".
    if offenders:
        issues.append('mismatch')
    if unmon:
        issues.append('unmon')
    if not members:
        issues.append('empty')
    if any(m['status'] == 'WAITING' for m in members):
        issues.append('untested')

    # An attached (never inherited) schedule on this group. The inherited system check is
    # the automatic TV Guide check, which has its own row.
    attached = [c for c in checks if not c['inherited']]
    # The group's own health class and its schedule's, both `st-` prefixed for the list's
    # health filter: one row has to match if EITHER matches. `health_cls` below is the
    # worse of the two - that is what colours the row and drives the sort.
    health_set = [f'st-{health_cls}']
    if attached:
        check_cls, check_label = _check_health(attached[-1])
        health_set.append(f'st-{check_cls}')
        health_cls, health_label = _worst_health([(health_cls, health_label),
                                                  (check_cls, check_label)])

    return {
        'id': g.id, 'name': g.name, 'system': False,
        'in_guide': g.in_guide, 'format_strategy': g.format_strategy,
        'health_score': hscore, 'health_cls': health_cls, 'health_label': health_label,
        'health_set': health_set, 'health_n': g.health_score_sample_count,
        'attached_checks': attached,
        'members': members, 'member_count': len(members),
        'recording_count': view['active_count'], 'disabled_count': view['disabled_count'],
        # What the Mixed format badge's tooltip names: the pin, and each member off it.
        'format_warning': {
            'lock_label': format_label(g.locked_format_key),
            'override': view['format_override'],
            'offenders': [{'name': ch.name,
                           'format': format_label(format_key(view['latest_by_channel'].get(ch.id)))}
                          for ch in offenders],
        } if offenders else None,
        'unmonitored_count': unmon,
        'reference_label': view['reference_label'] if view['reference_key'] else None,
        'locked': view['locked'],
        'best_active': next((m for m in members if m['is_best']), None),
        'checks': checks, 'check_dim': _check_dimension(checks),
        'recordings': recordings,
        'accounts': sorted({m['account_name'] for m in members}),
        'issues': issues,
        'last_activity': _last_activity(members, checks),
        'detail_url': url_for('channel_groups.group_detail', group_id=g.id),
    }


def _system_group_row(g, latest_by_channel, channels, disabled_ids, jobs, tests_by_job,
                      ct_cfg=None):
    """The pinned "TV Guide Channels" row. Its membership is computed rather than stored
    (channel_groups.check_target_channels), so it cannot go through _channel_group_row's
    membership path; `channels`/`disabled_ids` are that already-resolved target set,
    built once in groups_page() rather than per-row."""
    ranked = sorted(channels, key=lambda ch: (
        0 if ch.health_score is not None else 1,
        -(effective_score(ch) if ch.health_score is not None else 0),
        ch.name.lower(),
    ))
    tooltip = _TEST_DISABLED_TOOLTIP
    members = [
        _member_ctx(ch, latest_by_channel, tooltip if ch.id in disabled_ids else None, is_best=False)
        for ch in ranked
    ]
    channel_ids = [ch.id for ch in channels]
    checks = [_check_ctx(job, channel_ids, inherited=False, tests_by_job=tests_by_job, ct_cfg=ct_cfg)
              for job in jobs]

    health_cls, health_label = _check_health(checks[-1] if checks else None)

    issues = []
    if not members:
        issues.append('empty')
    if any(m['status'] == 'WAITING' for m in members):
        issues.append('untested')

    return {
        'id': g.id, 'name': g.name, 'system': True,
        'in_guide': False, 'format_strategy': g.format_strategy,
        'health_score': None, 'health_cls': health_cls, 'health_label': health_label,
        'health_set': [f'st-{health_cls}'], 'health_n': 0,
        'attached_checks': checks,
        'members': members, 'member_count': len(members),
        'recording_count': 0, 'disabled_count': len(disabled_ids),
        'format_warning': None, 'unmonitored_count': 0,
        'reference_label': None, 'locked': False, 'best_active': None,
        'checks': checks, 'check_dim': _check_dimension(checks),
        'recordings': [],
        'accounts': sorted({m['account_name'] for m in members}),
        'issues': issues,
        'last_activity': _last_activity(members, checks),
        # The system group has no detail page of its own - "Details" opens its schedule's
        # results page. Ambiguous only for the rare 2-checks case (e.g. a nightly quick +
        # weekly deep check on one group); first job wins.
        'detail_url': checks[0]['detail_url'] if checks else None,
    }


@channel_groups_bp.route('/channel-groups')
def groups_page():
    """The Groups tab (DESIGN.md §14): every ChannelGroup, one list.

    There is one kind of group. A group used only for health checking is one whose
    format_strategy is health_check_only and whose members are all recording-disabled,
    which is a configuration rather than a separate type - so this page no longer
    sections by kind and no longer merges a group with its schedule into a "pair"
    (DESIGN-channel-groups-model.md DECIDED 2).

    Every per-group lookup below is batched once across all groups rather than
    queried inside the per-group loop (CLAUDE.md's no-N+1 rule) - both
    `ChannelGroup.memberships` and `OnDemandTestJob.group`'s `test_jobs` backref are
    `lazy=True`, so touching either per group in the loop is one query per group."""
    from ..channel_tester import (monitored_channel_ids, get_status,
                                  health_check_profile_payload)

    cfg = load_config()
    ct_cfg = cfg.get('channel_testing', {})
    streak_threshold = ct_cfg.get('failing_streak_threshold', DEFAULT_FAILING_STREAK_THRESHOLD)

    groups = ChannelGroup.query.order_by(db.func.lower(ChannelGroup.name)).all()
    system_group = next((g for g in groups if g.is_system), None)
    stored_group_ids = [g.id for g in groups if not g.is_system]

    memberships_by_group = {}
    if stored_group_ids:
        rows = (ChannelGroupMember.query
                .filter(ChannelGroupMember.group_id.in_(stored_group_ids))
                .order_by(ChannelGroupMember.position).all())
        for m in rows:
            memberships_by_group.setdefault(m.group_id, []).append(m)

    jobs_by_group = {}
    if groups:
        for job in OnDemandTestJob.query.filter(OnDemandTestJob.group_id.in_([g.id for g in groups])).all():
            jobs_by_group.setdefault(job.group_id, []).append(job)

    all_member_ids = set()
    for gid, memberships in memberships_by_group.items():
        all_member_ids.update(m.channel_id for m in memberships)
    system_channels = []
    system_disabled_ids = set()
    if system_group is not None:
        system_channels, system_disabled_ids = check_target_channels(system_group)
        all_member_ids.update(ch.id for ch in system_channels)
    latest = _latest_tests_by_channel(list(all_member_ids))
    monitored_ids = monitored_channel_ids()
    # Every check's per-channel results in ONE query. Per-check is an N+1 across the page
    # and each linked pair carries at least one check.
    all_job_ids = [job.id for jobs in jobs_by_group.values() for job in jobs]
    tests_by_job = get_job_result_counts_batch(all_job_ids, list(all_member_ids))

    system_jobs = jobs_by_group.get(system_group.id, []) if system_group else []
    system_job = system_jobs[0] if system_jobs else None
    system_channel_ids = {ch.id for ch in system_channels}

    blocking_by_group = {}
    if stored_group_ids:
        recs = (Recording.query
                .filter(Recording.group_id.in_(stored_group_ids),
                        Recording.status.notin_(_TERMINAL_RECORDING_STATUSES))
                .order_by(Recording.start_time).all())
        for r in recs:
            blocking_by_group.setdefault(r.group_id, []).append(r)

    group_rows = []
    for g in groups:
        jobs = jobs_by_group.get(g.id, [])
        if g.is_system:
            group_rows.append(_system_group_row(g, latest, system_channels,
                                                system_disabled_ids, jobs, tests_by_job, ct_cfg))
        else:
            group_rows.append(_channel_group_row(g, latest, monitored_ids,
                                                 system_job, system_channel_ids,
                                                 memberships_by_group.get(g.id, []), jobs,
                                                 blocking_by_group.get(g.id, []),
                                                 tests_by_job, streak_threshold, ct_cfg, cfg))
    # The pinned system group stays first under every sort (DESIGN.md §14.1).
    group_rows.sort(key=lambda r: (not r['system'], r['name'].lower()))

    # The Create-health-check modal's profile readout, folded once for the whole page.
    # Building it per group row is the /guide-11s defect class - hand the resolver the
    # config dict that is already in hand (dev/changelog/321).
    check_profiles = health_check_profile_payload(
        load_config().get('channel_testing', {}),
        HealthCheckProfile.query.order_by(HealthCheckProfile.name).all())
    # For the Create-group modal's client-side name-conflict check and its Manual format
    # mode. Both are per-request, never per row.
    resolution_options, fps_options = _format_option_sets({'resolution': '', 'fps': None})
    from ..check_window import format_window_label
    window_label = format_window_label(ct_cfg)
    return render_template('channels/groups.html',
        group_rows=group_rows,
        group_names=[g.name for g in groups],
        resolution_options=resolution_options, fps_options=fps_options,
        check_profiles=check_profiles, tester_status=get_status(),
        window_label=window_label)


def _group_tests(job_names, recordings):
    """The ChannelTests a group's own work produced: every test run by a check in
    `job_names`, plus the pre-recording checks run for `recordings`.

    Two disjoint scopes rather than one, because a pre-check carries no job_id at all -
    it is named by the recording it protected (DESIGN-prerecord-checks.md 3), and that
    recording is what makes it this group's. One query whatever the member count, per
    CLAUDE.md's no-hidden-I/O rule; an empty scope runs none."""
    rec_ids = [r.id for r in recordings]
    scopes = []
    if job_names:
        scopes.append(ChannelTest.job_id.in_(list(job_names)))
    if rec_ids:
        scopes.append(ChannelTest.pre_check_recording_id.in_(rec_ids))
    if not scopes:
        return []
    return ChannelTest.query.filter(db.or_(*scopes)).all()


def _build_group_timeline(group, pinned_job=None):
    """Merge this group's ChannelTests + group-backed Recordings + those recordings'
    GROUP_* RecordingEvents + members' grouping ChannelEvents + the group's own
    ChannelGroupEvents into one chronological (newest-first) list of dicts, for the group
    detail Activity Timeline. Each entry carries a `source` label (which member/recording
    it came from), since a group aggregates across members - mirrors
    _build_channel_timeline in routes/channels.py.

    A test entry is here because THIS GROUP'S OWN WORK produced it, never because of who
    is a member today (dev/changelog/790): the tests any check attached to this group ran,
    plus the pre-recording checks run for this group's own recordings. Scoping them by
    current membership instead put another group's check on a shared feed, and a one-off
    "Test now", on this page as if this check had run them - while a channel that WAS in a
    run and has since been removed vanished from its own history. `pinned_job` narrows the
    test half to one check, which is what /channels/health-checks/<id> is: the same page
    entered by check rather than by group, so the timeline it shows is that check's runs.
    Everything else below is a fact about the group and stays group-scoped either way.

    Membership still comes from check_target_channels(), not `group.memberships`, for the
    ChannelEvent pull: the system group stores no membership rows (it is computed from the
    guide), so reading the relationship directly gave "TV Guide Channels" a permanently
    empty timeline.

    The members' ChannelEvent pull is deliberately narrow - grouping and failover only.
    The CHANNEL_GROUP_FORMAT_* pair is per-membership and lives on ChannelGroupEvent, so
    it arrives already scoped to this group by the query below; reading it off
    ChannelEvent showed a group format events raised by a *different* group that happened
    to share a member (dev/changelog/789).

    Every query below is batched across all members - the per-member loop this used to
    run was 2 queries per channel, which on the system group is 2 per guide channel."""
    entries = []
    members, _ = check_target_channels(group)
    member_ids = [ch.id for ch in members]
    urls = {ch.id: url_for('channels.channel_detail', channel_id=ch.id) for ch in members}
    names = {ch.id: ch.name for ch in members}

    recordings = Recording.query.filter_by(group_id=group.id).all()
    job_names = ({pinned_job.id: pinned_job.name} if pinned_job is not None
                 else {j.id: j.name for j in group.test_jobs})
    tests = _group_tests(job_names, [] if pinned_job is not None else recordings)

    # A test may sit on a channel that has since left the group, so its name is resolved
    # with the same batched extra lookup the group's own events below use rather than
    # assumed present in the members map.
    extra_ids = {t.channel_id for t in tests if t.channel_id not in names}

    if member_ids:
        for e in (ChannelEvent.query
                  .filter(ChannelEvent.channel_id.in_(member_ids))
                  .filter(ChannelEvent.event_type.in_(
                      [CHANNEL_GROUPED, CHANNEL_UNGROUPED,
                       CHANNEL_FAILOVER_HEALTH_OBSERVATION,
                       CHANNEL_STALL_DEMOTION_HEALTH_OBSERVATION,
                       CHANNEL_PLACEHOLDER_HEALTH_OBSERVATION,
                       CHANNEL_FAST_DELIVERY_HEALTH_OBSERVATION])).all()):
            extra = json.loads(e.extra_data) if e.extra_data else {}
            entries.append({'kind': 'channel_event', 'ts': e.timestamp, 'obj': e,
                            'source': names[e.channel_id], 'source_url': urls[e.channel_id],
                            'blend_breakdown': extra.get('blend_breakdown')})

    # The group's own facts (DESIGN-channel-groups-model.md 4.5). channel_id is nullable -
    # a fact about the group as a whole carries no source - and when it is set the channel
    # may no longer be a member, so its name is resolved with one extra batched query
    # rather than assumed present in the members map. That query is shared with the tests
    # above, which can name a departed member for the same reason.
    group_events = ChannelGroupEvent.query.filter_by(group_id=group.id).all()
    extra_ids |= {e.channel_id for e in group_events
                  if e.channel_id is not None and e.channel_id not in names}
    ge_names = ({c.id: c.name for c in Channel.query.filter(Channel.id.in_(extra_ids)).all()}
                if extra_ids else {})
    for e in group_events:
        src = names.get(e.channel_id) or ge_names.get(e.channel_id)
        entries.append({
            'kind': 'group_event', 'ts': e.timestamp, 'obj': e,
            'source': src,
            'source_url': (url_for('channels.channel_detail', channel_id=e.channel_id)
                           if src else None),
        })

    for t in tests:
        src = names.get(t.channel_id) or ge_names.get(t.channel_id)
        entries.append({
            'kind': 'test', 'ts': t.test_started_at, 'obj': t,
            'source': src,
            'source_url': (url_for('channels.channel_detail', channel_id=t.channel_id)
                           if src else None),
            'run_label': (job_names.get(t.job_id) if t.job_id is not None
                          else 'Pre-recording check'),
            'quality_breakdown': json.loads(t.quality_breakdown) if t.quality_breakdown else None,
            'blend_breakdown': json.loads(t.blend_breakdown) if t.blend_breakdown else None,
        })

    rec_names = {r.id: r.name for r in recordings}
    rec_urls = {r.id: url_for('recordings.recording_detail', recording_id=r.id) for r in recordings}
    # The channel a recording ran on (its FINAL member, if it failed over) - not necessarily
    # a current group member, so looked up separately from the members-only names/urls above.
    rec_channel_ids = {r.channel_id for r in recordings if r.channel_id is not None}
    rec_channels = ({c.id: c for c in Channel.query.filter(Channel.id.in_(rec_channel_ids)).all()}
                     if rec_channel_ids else {})
    for r in recordings:
        rec_ch = rec_channels.get(r.channel_id)
        entries.append({
            'kind': 'recording', 'ts': r.completed_at or r.start_time, 'obj': r,
            'source': rec_ch.name if rec_ch else None,
            'source_url': url_for('channels.channel_detail', channel_id=rec_ch.id) if rec_ch else None,
            'quality_breakdown': json.loads(r.health_quality_breakdown) if r.health_quality_breakdown else None,
            'blend_breakdown': json.loads(r.health_blend_breakdown) if r.health_blend_breakdown else None,
            'correction_breakdown': json.loads(r.capture_quality_breakdown) if r.capture_quality_breakdown else None,
        })
    if recordings:
        rec_events = (RecordingEvent.query
                      .filter(RecordingEvent.recording_id.in_(list(rec_names)))
                      .filter(RecordingEvent.event_type.in_([GROUP_MEMBER_SELECTED, GROUP_FAILOVER])).all())
        event_links = group_event_channel_links(rec_events)
        for ev in rec_events:
            entries.append({'kind': 'recording_event', 'ts': ev.timestamp, 'obj': ev,
                            'source': rec_names[ev.recording_id], 'source_url': rec_urls[ev.recording_id],
                            'channel_links': event_links.get(ev.id)})

    entries.sort(key=lambda e: e['ts'], reverse=True)
    return entries


def _resolution_area(res):
    """Pixel area of a "WxH" resolution string for biggest-first sorting; 0 if unparseable."""
    try:
        w, h = res.lower().split('x')
        return int(w) * int(h)
    except (ValueError, AttributeError):
        return 0


def _format_option_sets(group_format):
    """(resolution_options, fps_options) for the Part J Group Format dropdowns.

    resolution_options: distinct probed resolutions ∪ {1920x1080, 1280x720, 640x480}
    ∪ current locked value, sorted by pixel area descending.
    fps_options: [{value:int, label:str}] - distinct rounded probed FPS ∪ {30, 60}
    ∪ current locked value, deduped on the rounded int, ascending; 30→"29.97/30",
    60→"59.97/60", every other value its plain number.
    """
    res_set = {(r[0] or '').strip() for r in
               db.session.query(ChannelTest.resolution)
               .filter(ChannelTest.resolution.isnot(None)).distinct().all()}
    res_set.discard('')
    res_set.update(['1920x1080', '1280x720', '640x480'])
    if group_format['resolution']:
        res_set.add(group_format['resolution'])
    resolution_options = sorted(res_set, key=_resolution_area, reverse=True)

    fps_set = {round(f[0]) for f in
               db.session.query(ChannelTest.fps)
               .filter(ChannelTest.fps.isnot(None)).distinct().all() if f[0]}
    fps_set.update([30, 60])
    if group_format['fps']:
        fps_set.add(int(group_format['fps']))
    _fps_labels = {30: '29.97/30', 60: '59.97/60'}
    fps_options = [{'value': v, 'label': _fps_labels.get(v, str(v))}
                   for v in sorted(fps_set)]
    return resolution_options, fps_options


# ── Unified group / health-check detail page ────────────────────────────────
#
# ONE page and ONE builder back both `/channel-groups/<id>` and
# `/channels/health-checks/<job_id>` (dev/changelog/273). A ChannelGroup is the
# primary object; an attached OnDemandTestJob is a schedule it may or may not have, so
# the page's structure is driven by three booleans:
#   is_stored = an ordinary group, with stored membership - everything but the pinned
#               system group, whose membership is computed at display time
#   records   = at least one member has Recording on, so a recording (and a guide row)
#               can start from this group at all - what the page says about itself
#   has_check = a health check schedule is attached (job is not None)
# Every section keys off those. Both routes funnel through
# build_group_detail_context() so a group opened from either URL renders identically.

# `linked` sits between summary and settings (dev/changelog/323): an attached schedule
# is a thing you can see and act on, not a chip in the title bar.
GROUP_DETAIL_SECTIONS = ('summary', 'linked', 'settings', 'channels', 'activity')

# Optional Channels-table columns, per facet. Structural columns (select, caret,
# Channel, actions) are never user-hideable and are not listed here.
# `rec` and `test` lead on a stored group's list: they are what the page exists to set,
# and DECIDED 6 chose two checkboxes over a multiselect precisely so each can be sorted
# and filtered on its own (dev/changelog/755). The system group has no memberships, so it
# has neither column.
#
# `account` is a column of its own carrying the account's colour dot and its name
# (dev/changelog/1064). It was a dot inside the Channel cell until then, which made it the
# one entry the Columns popover could not reorder. A user with a single account still turns
# it off here, which is why it was a hideable entry in the first place (dev/changelog/758);
# the phone card draws it under the name rather than in a track.
GROUP_DETAIL_COLUMNS = {
    'check': ('rec', 'test', 'status', 'score', 'res', 'fps', 'audio', 'framePct', 'bitrate', 'drops', 'shot', 'epg', 'account'),
    'channel': ('rec', 'test', 'score', 'res', 'fps', 'audio', 'bitrate', 'epg', 'account'),
    'system': ('status', 'score', 'res', 'fps', 'audio', 'framePct', 'bitrate', 'drops', 'shot', 'epg', 'account'),
}
# FPS is off by default because the Format column repeats it as a subtitle; Frames is
# off by default too - available, just not shown until asked for. Audio joins them: a health
# check measures five audio facts on every member and the list could show none of them
# (dev/changelog/769), but a column that is on for everybody would push the video stats right
# on the many groups whose members all carry the same stereo AAC. EPG id joins them for the
# same reason and is turned on for you by the mismatch banner's own Review members button,
# which is the one moment it answers a question (dev/changelog/904). Drops joins them
# (dev/changelog/1064): a drop count is a detail you go looking for after the health score
# and the frame percentage have already told you a feed is unwell, and on the overwhelming
# majority of members it is a column of zeroes.
#
# A browser that already stored a layout keeps whatever it stored - a default only ever
# decides what a key starts as, and overriding a visibility the user is already storing is
# the app answering a question that belongs to them.
GROUP_DETAIL_COLUMNS_OFF = ('fps', 'framePct', 'audio', 'epg', 'drops')


def _group_detail_job(group):
    """The health check this group's detail page shows, or None.

    Several jobs may attach to one group (a nightly quick check plus a weekly deep
    one), but the page renders exactly one - the newest, which is the one whose
    results the table is showing. Opening a specific check by its own URL pins that
    one instead."""
    jobs = sorted(group.test_jobs, key=lambda j: j.id)
    return jobs[-1] if jobs else None


def _format_source_map(latest, latest_any):
    """{channel_id: {...}} describing the test a member's format verdict was decided on,
    for the members where that is NOT the test the table renders.

    The detail table is scoped to the attached health check while every lock-derived fact
    reads each channel's newest test whatever ran it - two maps, deliberately, because the
    table's job scope is a feature and the lock must agree with the recorder
    (dev/changelog/890). What was missing is that the row then states one format in its
    Format column and judges on another in its mismatch pill, with nothing on screen
    saying so: group 5's member 4137 rendered "1920x1080 @ 60" beside a Format mismatch
    pill whose own tooltip read "this member is 1920x1080 @ 60 and the group is pinned by
    hand to 1920x1080 @ 60" (dev/docs/BUGS.md 2026-09-14). Neither map may move, so the
    disagreement has to be named instead - principle 1, a number the user cannot explain
    is worse than no number.

    Empty for every member whose two tests are the same row, which is the normal case, so
    the client renders nothing extra unless there is genuinely something to disclose. One
    batched query for the job names; nothing per row."""
    # Narrowed to the members where the newer check produced a competing READING - its own
    # resolution and frame rate, differing from the rendered check's. Two narrowings, both
    # about noise:
    #   - A newer check measuring the same thing contradicts nothing, and flagging those
    #     would put a tooltip on most of the table for no information.
    #   - A newer check that measured nothing (it failed before ffprobe got a usable
    #     answer) has not disagreed with anybody, so it is not what this says. That row is
    #     already untested-for-format everywhere, and "unknown is not proven-different."
    # Deliberately raw readings rather than format_key(): a failed check's numbers do not
    # count as a format (that is the point of the status gate), but they are still a real
    # number the user can see on the test, so a row showing a different one has to say so.
    def _reading(t):
        return (t.resolution, round(t.fps)) if t is not None and t.resolution and t.fps else None

    differing = {cid: t for cid, t in latest_any.items()
                 if t is not None and latest.get(cid) is not t
                 and _reading(t) is not None
                 and _reading(t) != _reading(latest.get(cid))}
    if not differing:
        return {}
    job_ids = {t.job_id for t in differing.values() if t.job_id is not None}
    job_names = dict(db.session.query(OnDemandTestJob.id, OnDemandTestJob.name)
                     .filter(OnDemandTestJob.id.in_(job_ids)).all()) if job_ids else {}
    return {
        cid: {
            'label': format_label(format_key(t)) if format_key(t) else None,
            # What the newer check measured even when it does not count as a format -
            # "it failed, and these were the numbers" is the honest sentence, and a bare
            # "unknown" would hide the black-placeholder reading that raised the question.
            'measured': f'{t.resolution} @ {round(t.fps)}',
            'status': _test_status_label(t),
            # A test with no job is a recording's pre-check, identified by
            # pre_check_recording_id instead (routes/channel_tests.py::
            # _latest_tests_by_channel). Naming it beats "another check": it is the
            # freshest measurement there is, and it is not something the user scheduled.
            'pre_check': t.job_id is None,
            'tested_at': _fmt_et(t.test_started_at),
            'job_name': job_names.get(t.job_id),
        }
        for cid, t in differing.items()
    }


def group_detail_rows(group, job):
    """Member rows + tallies + duplicate sets for the unified detail page.

    Feeds both the first paint (embedded in the template) and the live-refresh
    endpoint, so the table can never be handed two different row shapes. Every
    per-member lookup is batched before the loop, never inside it."""
    from ..channel_tester import monitored_channel_ids, resolve_health_check_settings

    cfg = load_config()
    streak_threshold = cfg.get('channel_testing', {}).get(
        'failing_streak_threshold', DEFAULT_FAILING_STREAK_THRESHOLD)

    # The system group's membership is computed rather than stored, so it is the one
    # group whose rows do not come from ChannelGroupMember.
    stored = not group.is_system
    memberships = list(group.memberships)

    # Scoped to this job when there is one, so the table shows THAT check's results
    # rather than whatever test happened to run most recently on the channel. With no
    # check attached the scope is ANY_JOB, never None - `for_job_id=None` filters on
    # `job_id IS NULL`, and no ChannelTest has had a null job_id since the _m004
    # backfill, so it would silently return zero results for every group.
    if stored:
        unranked = member_channels(memberships)
        channel_ids = [ch.id for ch in unranked]
        latest = _latest_tests_by_channel(channel_ids, for_job_id=job.id if job else ANY_JOB)
        channels = rank_members(unranked, latest, streak_threshold=streak_threshold)
        # A stored group's ROWS carry their two participation switches directly, so no row
        # is "disabled" any more: an unticked member is a choice the user made and the
        # switch one column over already says so. DECIDED 3 (membership is indicators, not
        # a status) is what deletes the derived flag rather than renaming it - keeping it
        # would also have left every member unselectable for the bulk switches, since a
        # disabled row carries no checkbox (dev/changelog/755).
        disabled_ids = set()
        # The duplicates modal is a DIFFERENT consumer and keeps the old meaning: it is
        # picking which of several identical feeds to keep, and "this one is sitting out"
        # is a fact worth having in that decision. It renders no red status pill and no
        # select checkbox, so DECIDED 3 does not reach it.
        sitting_out_ids = {ch.id for ch in channels
                           if ch.id not in participating_member_ids(group, memberships)}
    else:
        channels, disabled_ids = check_target_channels(group)
        sitting_out_ids = disabled_ids
        channel_ids = [ch.id for ch in channels]
        latest = _latest_tests_by_channel(channel_ids, for_job_id=job.id if job else ANY_JOB)

    # Two maps, deliberately. `latest` is scoped to the attached check and is what the
    # ROWS render - the table's job scope is a real feature. `latest_any` is each
    # channel's own newest test whatever produced it, and every lock-derived fact below
    # reads it, because that is what the recorder, evaluate_and_reconcile_group() and
    # apply_format_strategy() read. Deriving them from the job-scoped map made this page
    # answer "which members may serve" differently from the code that actually serves
    # them - a disagreement the user sees as a page claiming nobody is blocked while
    # every recording falls through the zero-survivor override (dev/changelog/890,
    # CLAUDE.md "Format lock filters, health score ranks"). Reuses `latest` outright when
    # there is no job to scope to, so the second query only exists when the two differ.
    latest_any = latest if job is None else _latest_tests_by_channel(channel_ids,
                                                                    for_job_id=ANY_JOB)

    ref_key = (group_reference_key(group, memberships, latest_any, streak_threshold)
               if stored else None)
    derived_ref_key = (derived_reference_key(memberships, latest_any, streak_threshold)
                       if stored else None)
    _, outliers = (group_format_outliers(channels, latest_any, reference_key=ref_key,
                                         streak_threshold=streak_threshold)
                   if stored else (None, []))
    outlier_ids = {ch.id for ch in outliers}
    best_active_id = None
    if stored:
        best = pick_best_member(recording_members(memberships), latest_any,
                                streak_threshold=streak_threshold)
        best_active_id = best.id if best else None

    # Which members the format lock would drop at selection time, asked of the one helper
    # every selection site asks so the row pill and the recorder cannot disagree about who
    # is servable (CLAUDE.md "Format lock filters, health score ranks"). `filtered` is
    # empty when `override` is True, which is right: a lock that left nothing is bypassed,
    # so it blocks nobody and no row should claim otherwise.
    format_blocked_ids = set()
    format_override = False
    if stored:
        sel = format_eligible_members(group, recording_members(memberships), latest_any)
        format_blocked_ids = {ch.id for ch in sel.filtered}
        format_override = sel.override
    recording_ids = {m.channel_id for m in memberships if m.recording_enabled}
    test_ids = test_member_ids(memberships)

    # The measurement the lock-derived facts above were decided on, whenever it is NOT the
    # one this table renders. Both maps are already loaded, so this costs one query for the
    # job names and nothing per row.
    format_source_by_channel = _format_source_map(latest, latest_any) if stored else {}

    monitored_ids = monitored_channel_ids() if stored else set()
    dup_titles = duplicates_within(channels)
    # Computed once here and handed to _serialize_dup_groups (never recomputed there) -
    # the row table and the missing-channels notice below both need it too.
    lifecycle_by_channel = lifecycle_states_for_channels(channels, cfg)
    dup_groups = _serialize_dup_groups(duplicate_groups_within(channels), cfg, latest,
                                       sitting_out_ids,
                                       lifecycle_by_channel=lifecycle_by_channel)

    test_duration = resolve_health_check_settings(
        cfg.get('channel_testing', {}), job.profile if job else None)['test_duration_seconds']

    # Every tag once, patterns eagerly, ABOVE the row loop - the member list's Tag filter
    # asks tags_matching() per row and a lazy `tag.patterns` inside the comprehension would
    # be a query per row (CLAUDE.md "No hidden I/O in per-row loops"; guarded by
    # tests/test_scaling_pages.py's two group cases).
    all_tags = Tag.query.options(selectinload(Tag.patterns)).all()

    rows = [
        _channel_result_row(
            ch, latest.get(ch.id), test_duration, dup_titles,
            in_guide=ch.in_guide,
            disabled=ch.id in disabled_ids,
            disabled_tip=(_TEST_DISABLED_TOOLTIP if ch.id in disabled_ids else None),
            is_best=ch.id == best_active_id,
            mismatch=ch.id in outlier_ids,
            monitored=ch.id in monitored_ids,
            # The two switches, and what the lock does to this member right now. A
            # non-stored group has no memberships at all, so its rows carry neither.
            recording_enabled=stored and ch.id in recording_ids,
            test_enabled=stored and ch.id in test_ids,
            # The id the section 8 mismatch banner tallies, carried per row so the member
            # list can be filtered to the members that banner names rather than only being
            # told how many there are (dev/changelog/904). Already loaded on `ch` - reading
            # it here costs no query. Empty string rather than None: it is a filter value
            # and a column, and "no EPG id" is a bucket of its own on both.
            epg_channel_id=ch.epg_channel_id or '',
            format_blocked=ch.id in format_blocked_ids,
            format_source=format_source_by_channel.get(ch.id),
            lifecycle=(lc := lifecycle_by_channel.get(ch.id, (None, None)))[0],
            lifecycle_date=lc[1].strftime('%Y-%m-%d') if lc[1] else '',
            # Which tags this member's own NAME carries, through the one definition of
            # "carries this tag" (accounts.py::tags_matching). Deliberately the channel-name
            # question and not channel_search.py's wider channel-grain one ("airs something
            # matching"): on a member list the tag is being read as a label on the feed - a
            # 4K variant, a backup - and the two tags that live in program titles rather than
            # channel names correctly offer nothing here.
            tags=tags_matching(all_tags, ch.name),
        )
        for ch in channels
    ]
    counts = _tally([t for t in (latest.get(cid) for cid in channel_ids) if t is not None])
    missing_channels = [
        {'channel_id': ch.id, 'channel_name': ch.name, 'account_name': ch.account.name,
         'lifecycle_date': lifecycle_by_channel[ch.id][1].strftime('%Y-%m-%d')}
        for ch in channels
        if lifecycle_by_channel.get(ch.id, (None, None))[0] == 'missing'
    ]
    return {
        'rows': rows,
        'dup_groups': dup_groups,
        'missing_channels': missing_channels,
        'counts': counts,
        'total': len(channel_ids),
        'reference_key': ref_key,
        'reference_label': format_label(ref_key) if ref_key else None,
        # The LOCK, which is not always the effective reference: an unlocked group still
        # derives one from its best member, and 16.1's pill has to name the thing that
        # actually filtered this member rather than a value nothing enforces.
        'lock_label': (format_label(group.locked_format_key)
                       if stored and group.locked_format_key else None),
        # What the data alone points at, with any lock ignored - the format the
        # `highest_score` strategy would follow. Separate from both of the above because
        # the settings picker labelled that strategy from `lock_label or reference_label`
        # and so named the group's existing lock back at it (dev/changelog/890).
        'derived_reference_label': format_label(derived_ref_key) if derived_ref_key else None,
        'mismatch_count': len(outlier_ids),
        'unmonitored_count': sum(1 for ch in channels if ch.id not in monitored_ids) if stored else 0,
        'warnings': _banner_facts(group, channels, latest_any, recording_ids,
                                  format_blocked_ids, format_override, best_active_id,
                                  monitored_ids, job is not None, ref_key,
                                  lock_ranking_ids(memberships) if stored else None)
        if stored else None,
    }


def _banner_facts(group, channels, latest, recording_ids, format_blocked_ids,
                  format_override, best_active_id, monitored_ids, has_check, ref_key,
                  rank_ids=None):
    """Everything section 16's warning banners need, decided server-side.

    The banners are gated and counted here rather than in group-detail.js because every
    input is already on this side and two answers to "do these members span more than one
    format" is a disagreement the user reads as the page arguing with itself. The client
    renders and words them; it decides nothing.

    Scoped to the RECORDING-ENABLED members throughout. Section 16.1: "If record is
    disabled then no need to show the same warnings, because it isn't set to record
    anyways" - a member sitting out is not part of what this group would record, so it is
    not part of what a warning about that recording describes.

    `latest` is the ANY_JOB map, not the job-scoped one the rows render: every banner here
    describes what the lock and the recorder would do, and those read each member's own
    newest test whatever job produced it (dev/changelog/890). `rank_ids` narrows the
    strategy ranking the same way apply_format_strategy() narrows it.

    Every value is derived from data the caller already loaded; nothing here queries."""
    recording = [ch for ch in channels if ch.id in recording_ids]

    # The EPG tally behind the section 8 banner. Members with no id at all are counted
    # separately: unknown is not mismatched, and saying so is what keeps the banner from
    # reading as an accusation against a feed that simply has no listings.
    epg_ids = collections.Counter(
        ch.epg_channel_id for ch in recording if ch.epg_channel_id)
    best = next((ch for ch in channels if ch.id == best_active_id), None)

    # The no-winner state made visible while it is true, rather than only in the one
    # GROUP_FORMAT_STRATEGY_BLOCKED event written on the way into it. Right after a
    # database wipe every group is here, so a banner that only appeared at the transition
    # would be missing for exactly the users who need it. Same pure engine call
    # apply_format_strategy() makes, over the same member list AND the same ranking
    # population, so the two agree - passing everything here while the engine ranked over
    # the recording-enabled members is how this banner came to describe a different
    # group's lock than the one on the row (dev/changelog/890).
    plan = strategy_lock_plan(group, channels, latest, rank_ids=rank_ids)
    entry = plan['entry'] or {}
    no_winner = plan['manages_lock'] and entry.get('key') is None

    return {
        'strategy': group.format_strategy,
        'strategy_label': FORMAT_STRATEGY_LABELS.get(group.format_strategy),
        'no_winner': no_winner,
        'no_winner_rationale': entry.get('rationale') if no_winner else None,
        'manages_format': group_manages_format(group),
        'is_source': group.format_strategy != GROUP_FORMAT_HEALTH_CHECK_ONLY,
        'muted': [k for k in GROUP_WARNING_KINDS if k in group.muted_warning_set()],
        'recording_count': len(recording),
        'in_guide': bool(group.in_guide),
        # 15's breach state made visible. Not one of 16's mutable warnings and not gated
        # on the strategy the way they are: a group holding a guide row has necessarily
        # been promoted, and this is the explanation for why the row cannot produce a
        # file - which 16.2 says is exactly the kind of warning nobody may hide.
        'guide_broken': bool(group.in_guide and not recording_ids),
        # How many willing members the lock is currently skipping, and whether it skipped
        # every one of them (15.2's override, where the lock is bypassed rather than the
        # recording abandoned). `format_blocked_ids` is empty under an override by design,
        # so the two are read together, never one from the other.
        'format_blocked_count': len(format_blocked_ids & recording_ids),
        'format_override': bool(format_override),
        # The gate on every format warning this page shows - both banners and the loud
        # per-member pill: a hand-pinned format with a recording-enabled member off it. The
        # same helper answers the groups list's badge, so the two pages cannot disagree
        # (dev/changelog/925). Distinct from `manages_format`, which says whether a lock
        # FILTERS, a question an automatic strategy also answers yes to.
        'format_warns': bool(pinned_format_offenders(group, recording, latest)),
        # Who the recording would actually run from under an override, so the banner can
        # name it rather than saying "some member". Same pick_best_member answer the star
        # in the table renders, so the two cannot name different feeds.
        'override_member_name': best.name if best is not None else None,
        'override_member_format': (format_label(format_key(latest.get(best.id)))
                                   if best is not None and format_key(latest.get(best.id))
                                   else None),
        # Whether the member a recording would actually START on has a measured format.
        # Everything the page says about who would be used if a recording ran right now
        # hangs on this: with no format for the lead there is nothing for section 5.1's
        # pin to be, so no row may be dimmed as proven-different and the reference the
        # page displays is a lower-ranked member's rather than the one it would open at
        # (dev/changelog/765).
        'best_format_known': best is not None and format_key(latest.get(best.id)) is not None,
        # How many willing members share the format the group would record as. Stated once
        # on the Group format chip so the count and the dimmed rows cannot disagree, and
        # counted the same way both of them are decided - against the effective reference,
        # locked or derived. None when there is no reference to count against.
        'format_match_count': (sum(1 for ch in recording
                                   if format_key(latest.get(ch.id)) == ref_key)
                               if ref_key else None),
        'epg_ids': [{'epg_channel_id': eid, 'count': n}
                    for eid, n in epg_ids.most_common()],
        'epg_missing_count': sum(1 for ch in recording if not ch.epg_channel_id),
        # The drift-coverage warning, moved off the template so it follows a Health check
        # switch instead of waiting for a reload (handed forward by dev/changelog/756).
        # Every other banner on this page is already decided here for the same reason.
        'unmonitored_count': sum(1 for ch in channels if ch.id not in monitored_ids),
        'has_check': bool(has_check),
    }


def _schedule_ctx(job, ct_cfg=None):
    """The Settings modal's schedule block: a mode plus only the fields that mode
    needs. `mode` is derived, never stored - the DB carries recurring/
    scheduled_start_time and this renders them (dev/changelog/272 round 4).

    `ct_cfg`: forwarded to `_recur_label()`, see `_check_ctx()`'s docstring."""
    if job is None:
        return None
    if job.recurring:
        mode = 'recur'
    elif job.status == 'SCHEDULED' and job.scheduled_start_time:
        mode = 'once'
    else:
        mode = 'manual'
    if mode == 'manual':
        label = 'None'
    elif mode == 'once':
        label = f'One time, {_fmt_et(job.scheduled_start_time)}'
    else:
        label = _recur_label(job, ct_cfg)
    return {
        'mode': mode,
        'label': label,
        'recur_day': job.recur_day if job.recur_day is not None else 0,
        'recur_time': f'{job.recur_hour or 0:02d}:{job.recur_minute or 0:02d}',
        'use_window': bool(job.recur_use_window),
        'oneoff_value': (local_input_value(job.scheduled_start_time)
                         if job.scheduled_start_time else ''),
        'paused': job.recur_paused,
        'next_run_et': (_fmt_et(job.scheduled_start_time)
                        if mode != 'manual' and not job.recur_paused else None),
    }


def build_group_detail_context(group, job, pinned_check=False):
    """Everything the unified detail template renders, for one (group, check) pair.

    `pinned_check` says the page was entered by check URL rather than by group, which
    both entry points otherwise look identical from here: /channel-groups/<id> pins the
    newest attached check for the table too. It is what scopes the Activity Timeline's
    test entries to that one check's runs (dev/changelog/790)."""
    from .channels import _ListPagination, CHANNEL_HEALTH_PAGE_SIZES
    from ..channel_tester import get_status, health_check_profile_payload

    ct_cfg = load_config().get('channel_testing', {})
    from ..check_window import format_window_label
    window_label = format_window_label(ct_cfg)
    is_stored = not group.is_system
    payload = group_detail_rows(group, job)

    # What the page may SAY it is, as opposed to what it structurally is. A recording and
    # a guide row both start from recording_members(), so a group with none records
    # nothing whatever its format strategy says, and must not describe itself as a
    # recording source - "a group with no member enabled for recording is simply a health
    # check" is the Groups list page's own sentence for that state.
    # Deliberately not participation_is_recording(): that gate answers which switch a
    # member ROW is drawn by, and is keyed on format_strategy - a standing setting rather
    # than a fact about what would happen if a recording started now (dev/changelog/750).
    records = bool(recording_members(group.memberships))

    # The Channels table's own status pills are corrected live by group-detail.js
    # (rowStatus() vs. testerStatus), but the Activity Timeline below is server-rendered
    # only - so its "test" entries need the same in-progress override _timeline.html
    # applies for the channel detail page (CLAUDE.md "one flag, one meaning; states are
    # enumerated" - a ChannelTest row is created with status='FAILED' as a placeholder
    # and only gets its real terminal status once the test finishes).
    tester_status = get_status()
    in_progress_test_id = None
    if tester_status['is_running'] and tester_status['current_channel_id'] is not None:
        running_test = _latest_tests_by_channel(
            [tester_status['current_channel_id']], for_job_id=ANY_JOB
        ).get(tester_status['current_channel_id'])
        if running_test is not None:
            in_progress_test_id = running_test.id

    group_format = None
    if is_stored:
        # The EFFECTIVE reference (the locked value when set, else the one derived from
        # the best member) - not the stored lock columns. Switching the Settings modal to
        # Manual must pre-select the format the group is actually using today, which for
        # an unlocked group is the derived one and for a locked one is the pinned one.
        ref_key = payload['reference_key']
        group_format = {
            'locked': group.locked_format_key is not None,
            'reference_label': payload['reference_label'],
            'resolution': ref_key[0] if ref_key else '',
            'fps': ref_key[1] if ref_key else '',
            'strategy': group.format_strategy,
        }
    # Built even for the system group's page: the Create group modal opens from there
    # too, and its Manual format mode needs the same option sets (dev/changelog/322).
    resolution_options, fps_options = _format_option_sets(
        group_format or {'resolution': '', 'fps': None})

    timeline_job = job if (pinned_check and job is not None) else None
    all_entries = _build_group_timeline(group, pinned_job=timeline_job)
    # What the timeline is scoped to, said rather than left to be inferred - the whole
    # defect it replaces was a reader taking these entries for one check's own work
    # (dev/changelog/790).
    timeline_scope = (
        f'Health tests from "{timeline_job.name}" only. The recordings and group changes '
        f'below are the group\'s.' if timeline_job is not None else
        "Health tests this group's own checks and pre-recording checks ran, plus its "
        'recordings and changes.')
    per_page = request.args.get('timeline_per_page', '25')
    if per_page not in CHANNEL_HEALTH_PAGE_SIZES:
        per_page = '25'
    page = request.args.get('timeline_page', 1, type=int)
    if per_page == 'all':
        entries, pagination = all_entries, None
    else:
        per = int(per_page)
        pagination = _ListPagination(page, per, len(all_entries))
        start = (page - 1) * per
        entries = all_entries[start:start + per]

    # Every attached check, not just the one the table is pinned to: the `linked` section
    # lists them all, and a group carrying more than one has to say which run the Summary
    # bar and each Settings chip belong to.
    channel_ids = [r['channel_id'] for r in payload['rows']]
    checks = []
    for attached in sorted(group.test_jobs, key=lambda j: j.id):
        ctx = _check_ctx(attached, channel_ids, inherited=False, ct_cfg=ct_cfg)
        ctx['health_cls'], ctx['health_label'] = _check_health(ctx)
        ctx['is_primary'] = job is not None and attached.id == job.id
        ctx['schedule_label'] = _schedule_ctx(attached, ct_cfg)['label']
        checks.append(ctx)

    # Notice #2 of the Create-health-check modal (dev/changelog/321): this group is
    # already covered incidentally by the automatic TV Guide check. Asked of the check's
    # own target set, exactly as the groups list asks it of its batched copy
    # (_channel_group_row) - never re-derived from Channel.in_guide, which since
    # dev/changelog/752 answers a different question: the automatic check probes one
    # member per guide row plus one per scheduleless group, and whether a member also
    # holds its own guide row has nothing to do with whether it is one of them. The
    # group's own in_guide is not a gate either, because the scheduleless fallback covers
    # groups that are not in the guide at all. One resolve per page render, not per row.
    inherited_check = None
    if is_stored and channel_ids:
        system_group = ChannelGroup.query.filter_by(is_system=True).first()
        system_job = (sorted(system_group.test_jobs, key=lambda j: j.id)[0]
                      if system_group is not None and system_group.test_jobs else None)
        if system_job is not None:
            system_ids = {ch.id for ch in check_target_channels(system_group)[0]}
            covered = sum(1 for cid in channel_ids if cid in system_ids)
            if covered:
                inherited_check = {
                    'job_id': system_job.id,
                    'name': system_job.name,
                    'recur_description': _recur_label(system_job, ct_cfg),
                    # Its schedule is the user's to remove (dev/changelog/1068), and an
                    # unscheduled check covers nothing - so the two surfaces that
                    # advertise this coverage say which it is rather than assuming.
                    'schedule_live': schedule_is_live(system_job),
                    'recur_paused': system_job.recur_paused,
                    'channel_count': covered,
                    'detail_url': url_for('channels.health_check_detail', job_id=system_job.id),
                }

    from ..database import UserPref
    profiles = HealthCheckProfile.query.order_by(HealthCheckProfile.name).all()
    sec_key = 'both' if (is_stored and job) else ('channel' if is_stored else 'check')
    # The system group gets its own key rather than borrowing 'check': its membership is
    # computed, so it has no participation switches to show and must not inherit a saved
    # column order that names them.
    col_key = 'system' if not is_stored else ('check' if job else 'channel')
    sec_pref = db.session.get(UserPref, f'group_detail_sections_{sec_key}')
    col_pref = db.session.get(UserPref, f'group_detail_columns_{col_key}')

    if not checks:
        linked_title = 'Health checks'
    else:
        linked_title = f"Health check{'s' if len(checks) != 1 else ''}"

    # Clone-provenance note (dev/changelog/501): a one-time "created
    # from" pointer, not a live pairing - see `checks`/`inherited_check` above for
    # that. The source may since have been deleted, converted or renamed, so the
    # link is offered only while it still resolves; the name always renders from the
    # snapshot taken at clone time either way.
    cloned_from = None
    if group.cloned_from_group_id:
        src_group = db.session.get(ChannelGroup, group.cloned_from_group_id)
        cloned_from = {
            'name': group.cloned_from_name or (src_group.name if src_group else 'a deleted group'),
            'detail_url': (url_for('channel_groups.group_detail', group_id=src_group.id)
                           if src_group is not None else None),
        }

    return {
        'group': group,
        'job': job,
        'checks': checks,
        'linked_title': linked_title,
        'is_stored': is_stored,
        'records': records,
        'has_check': job is not None,
        'is_system': group.is_system,
        'payload': payload,
        'group_format': group_format,
        'resolution_options': resolution_options,
        'fps_options': fps_options,
        'schedule': _schedule_ctx(job, ct_cfg),
        'window_label': window_label,
        'health_check_profiles': profiles,
        # Folded once for the page - see the note in groups_page().
        'check_profiles': health_check_profile_payload(
            load_config().get('channel_testing', {}), profiles),
        'inherited_check': inherited_check,
        'cloned_from': cloned_from,
        # Client-side name-conflict check for the Create-channel-group modal. One query,
        # names only - never hydrated per row.
        'group_names': [n for (n,) in db.session.query(ChannelGroup.name).all()],
        'tester_status': tester_status,
        'in_progress_test_id': in_progress_test_id,
        'created_et': _fmt_et(group.created_at),
        'updated_et': _fmt_et(group.updated_at),
        'last_run_et': _fmt_et(job.completed_at) if job and job.completed_at else None,
        'sec_key': sec_key,
        'col_key': col_key,
        'section_pref': json.loads(sec_pref.value) if sec_pref and sec_pref.value else None,
        'column_pref': json.loads(col_pref.value) if col_pref and col_pref.value else None,
        'sections': list(GROUP_DETAIL_SECTIONS),
        'columns': list(GROUP_DETAIL_COLUMNS[col_key]),
        'columns_off': list(GROUP_DETAIL_COLUMNS_OFF),
        'timeline_entries': entries,
        'timeline_scope': timeline_scope,
        'timeline_per_page': per_page,
        'timeline_pagination': pagination,
        'page_sizes': CHANNEL_HEALTH_PAGE_SIZES,
    }


@channel_groups_bp.route('/api/channel-groups/<int:group_id>/detail-rows')
def group_detail_rows_api(group_id):
    """Live refresh for the unified detail page's Channels table. `job_id` pins which
    attached check's results the rows carry; omitted means the group's newest."""
    from ..channel_tester import get_status

    group = db.session.get(ChannelGroup, group_id)
    if group is None:
        return jsonify({'error': 'Group not found'}), 404

    job_id = request.args.get('job_id', type=int)
    if job_id is not None:
        job = db.session.get(OnDemandTestJob, job_id)
        if job is None or job.group_id != group.id:
            return jsonify({'error': 'Health check not found on this group'}), 404
    else:
        job = _group_detail_job(group)

    payload = group_detail_rows(group, job)
    return jsonify({
        'success': True,
        'job_status': job.status if job else None,
        'tester_status': get_status(),
        **payload,
    })


@channel_groups_bp.route('/api/channel-groups/<int:group_id>/format-plan')
def group_format_plan(group_id):
    """Read-only: for each auto-select-format strategy, which video format wins and
    why, over the group's current members (or a check-only group's computed target
    channels).

    **Always scoped ANY_JOB, and always ranked over the lock population.** This endpoint
    exists to answer "what would applying this strategy do", and apply_format_strategy()
    is the thing that would do it - so it reads what that reads or it is previewing a
    different app. It used to take a `job_id` and narrow to that health check's results,
    which is right for the member TABLE and wrong here: on group 5 the job-scoped view
    ranked 1080p60 first at a median 3957 kbps while the engine's any-job view ranked
    720p30 first, and refreshing never reconciled them because they were not stale copies
    of one answer (dev/changelog/890).

    `rank_scope=all` opts out of the recording-enabled narrowing, for the clone preview:
    a clone's members are all Recording-off by model default, so the new group's own
    engine will rank over everyone and a preview narrowed to the SOURCE's recording-enabled
    members would describe neither group."""
    group = db.session.get(ChannelGroup, group_id)
    if group is None:
        return jsonify({'error': 'Group not found'}), 404

    memberships = list(group.memberships)
    if group.is_system:
        channels, _excluded_ids = check_target_channels(group)
        rank_ids = None
    else:
        channels = member_channels(memberships)
        rank_ids = (None if request.args.get('rank_scope') == 'all'
                    else lock_ranking_ids(memberships))

    latest = _latest_tests_by_channel([ch.id for ch in channels], for_job_id=ANY_JOB)
    plan = plan_format_selection(channels, latest, rank_ids=rank_ids)
    return jsonify({'success': True, **plan})


@channel_groups_bp.route('/channel-groups/<int:group_id>')
def group_detail(group_id):
    group = db.session.get(ChannelGroup, group_id)
    if group is None:
        abort(404)
    return render_template('channels/group_detail.html',
                           **build_group_detail_context(group, _group_detail_job(group)))


# ── Group CRUD ──────────────────────────────────────────────────────────────

@channel_groups_bp.route('/api/channel-groups')
def list_groups():
    """Lightweight list for the group modal's "add to existing group" dropdown."""
    # The pinned system group is excluded: its membership is computed, not stored, so
    # adding to it would silently do nothing.
    groups = (ChannelGroup.query
              .filter_by(is_system=False)
              .order_by(db.func.lower(ChannelGroup.name)).all())
    return jsonify({'groups': [
        {'id': g.id, 'name': g.name, 'format_strategy': g.format_strategy,
         'member_count': len(g.memberships), 'in_guide': g.in_guide}
        for g in groups
    ]})


@channel_groups_bp.route('/api/channel-groups', methods=['POST'])
def create_group():
    data = request.get_json(silent=True) or {}
    name = (data.get('name') or '').strip()
    if not name or len(name) > 255:
        return jsonify({'error': 'Group name is required (max 255 characters)'}), 400
    if group_name_conflict(name):
        return jsonify({'error': f'A group named "{name}" already exists'}), 409
    # A group is created as a health check and promoted deliberately
    # (DESIGN-channel-groups-model.md 14): every member arrives with Health check on and
    # Recording off, and the five format questions are not asked here because at creation
    # time no member has been tested and every strategy would return "run a check first".
    strategy = data.get('format_strategy') or GROUP_FORMAT_HEALTH_CHECK_ONLY
    if strategy not in GROUP_FORMAT_STRATEGIES:
        return jsonify({'error': f'Unknown format strategy "{strategy}"'}), 400

    # 'channel_ids' entirely omitted (as opposed to present-but-empty, still an
    # error below) means "create empty, add members later" - the Groups tab's
    # "+ New Group" modal (DESIGN.md §14) creates a bare group by name only;
    # every other caller (Browse tab's "Group Selected") always sends the array.
    if 'channel_ids' not in data:
        channels = []
    else:
        channels, err = _load_channels_or_error(data.get('channel_ids'))
        if err:
            return err

    if channels:
        # Gated on the REQUESTED strategy exactly as add_members and clone_group gate it on
        # the stored one. A group defaults to health_check_only, and a sweep - every member
        # health checked, nothing recording-enabled - is the default shape of a new group
        # rather than a special case (DESIGN-channel-groups-model.md 7), so asking the
        # format question here would put a warning in front of the most ordinary thing a
        # user does with this app.
        warnings = _pending_warnings([], channels, bool(data.get('force')),
                                     strategy != GROUP_FORMAT_HEALTH_CHECK_ONLY)
        if warnings:
            return jsonify({'success': False, **warnings})

    channel_ids = [ch.id for ch in channels]

    # The create+commit is its own retry_on_locked unit (re-fetch inside): a locked
    # retry re-runs only this, and the prior rolled-back INSERT leaves no duplicate.
    # Reconcile runs AFTER, as its own unit - never a second commit inside this closure.
    @retry_on_locked()
    def _create():
        group = build_group_with_members(name, channel_ids, strategy)
        db.session.commit()
        return group.id
    group_id = _create()

    group = db.session.get(ChannelGroup, group_id)
    evaluate_and_reconcile_group(group)
    return jsonify({
        'success': True,
        'group_id': group.id,
        'group_name': group.name,
        'detail_url': url_for('channel_groups.group_detail', group_id=group.id),
    })


@channel_groups_bp.route('/api/channel-groups/<int:group_id>/members', methods=['POST'])
def add_members(group_id):
    group = db.session.get(ChannelGroup, group_id)
    if group is None:
        return jsonify({'error': 'Group not found'}), 404

    data = request.get_json(silent=True) or {}
    channels, err = _load_channels_or_error(data.get('channel_ids'))
    if err:
        return err

    existing_ids = {m.channel_id for m in group.memberships}
    new_channels = [ch for ch in channels if ch.id not in existing_ids]
    existing_channels = member_channels(group.memberships)

    warnings = _pending_warnings(
        existing_channels, new_channels, bool(data.get('force')),
        group.format_strategy != GROUP_FORMAT_HEALTH_CHECK_ONLY)
    if warnings:
        return jsonify({'success': False, **warnings})

    new_ids = [ch.id for ch in new_channels]

    @retry_on_locked()
    def _add():
        g = db.session.get(ChannelGroup, group_id)
        chans = Channel.query.filter(Channel.id.in_(new_ids)).all()
        next_pos = max((m.position for m in g.memberships), default=-1) + 1
        by_id = {ch.id: ch for ch in chans}
        for idx, cid in enumerate(new_ids):
            db.session.add(ChannelGroupMember(group_id=g.id, channel_id=cid,
                                              position=next_pos + idx))
        _grouping_events([by_id[cid] for cid in new_ids], g,
                         CHANNEL_GROUPED, 'Added to channel group "{group_name}"')
        channel_hiding.recompute(new_ids)
        touch_group(g)
        db.session.commit()
    _add()

    group = db.session.get(ChannelGroup, group_id)
    evaluate_and_reconcile_group(group)
    return jsonify({'success': True, 'group_id': group.id, 'group_name': group.name,
                    'added': new_ids})


@channel_groups_bp.route('/api/channel-groups/<int:group_id>/members/remove', methods=['POST'])
def remove_members(group_id):
    """Remove members from a group - the plain "remove this channel" action, and
    (via `removals`/`transfer`) the Remove Duplicate Channels modal's dedup path, which
    can optionally transfer each removed channel's guide listing/group membership/
    scheduled recordings/health-check enrollment to the channel kept in its place
    (transfer_channel_state(), the same transfer Re-point uses).

    Removing the last recording-enabled member is 15's breach path 3, and it takes the
    same _invariant_gate the two participation routes take - a member leaving empties the
    recording-enabled set exactly as unticking it does, and the user is standing right
    here, so it is confirmed rather than merely alerted.

    No view-level retry_on_locked any more: the removal now shares its commit with the
    demotion, and transfer_channel_state() is a side effect that must not be re-run by a
    lock retry, so the unit is the closure below rather than the whole view."""
    group = db.session.get(ChannelGroup, group_id)
    if group is None:
        return jsonify({'error': 'Group not found'}), 404

    data = request.get_json(silent=True) or {}
    # `removals` (dedup path) carries a keep_channel_id per entry; the plain removal path
    # only ever sends bare ids, so it's normalized into the same shape here.
    removals = data.get('removals')
    if removals is None:
        channel_ids = data.get('channel_ids') or ([data['channel_id']] if data.get('channel_id') else [])
        removals = [{'channel_id': cid} for cid in channel_ids]
    transfer = bool(data.get('transfer'))

    # Every id the request could take out of this group, whether by removal or by a
    # transfer onto a keeper that is already a member (which deletes the source's own
    # membership). Both empty the recording-enabled set the same way, so 15's gate is
    # asked about the whole set before any of it happens.
    losing_ids = [r.get('channel_id') for r in removals if r.get('channel_id')]
    err, state = _invariant_gate(group, losing_ids, bool(data.get('confirm')))
    if err is not None:
        return err
    demoting = bool(state['breaches'] and state['in_guide'])

    # transfer_channel_state() reassigns rows across channels and is not safe to re-run,
    # so the retried unit is this closure and everything it touches is re-fetched inside.
    @retry_on_locked()
    def _remove():
        g = db.session.get(ChannelGroup, group_id)
        by_channel = {m.channel_id: m for m in g.memberships}
        cfg = load_config() if transfer else None
        removed = []
        transferred = []
        transfer_skipped = []
        # Groups other than this one whose membership a transfer may have deleted - a
        # source channel can belong to several. Checked after the commit, never here.
        touched_group_ids = set()
        for r in removals:
            m = by_channel.get(r.get('channel_id'))
            if m is None:
                continue
            ch = m.channel
            did_transfer = False
            if transfer:
                keep_id = r.get('keep_channel_id')
                keeper = db.session.get(Channel, keep_id) if keep_id else None
                # Server re-validates the keeper from DB ids - never trust the client
                # blindly (same "server re-validates everything" pattern as repoint).
                if keeper is None or keeper.id == ch.id or keeper.id not in by_channel:
                    transfer_skipped.append({'channel_id': ch.id,
                                             'reason': 'No valid channel to keep in this group'})
                elif keeper.stream_url != ch.stream_url:
                    transfer_skipped.append({'channel_id': ch.id,
                                             'reason': 'Kept channel no longer shares the stream URL'})
                else:
                    touched_group_ids.update(mm.group_id for mm in ch.group_memberships)
                    transfer_channel_state(ch, keeper, cfg)
                    transferred.append(ch.id)
                    did_transfer = True
            if not did_transfer:
                removed.append(ch)
                db.session.delete(m)
        # Transferred channels get their own event trail from transfer_channel_state()
        # (guide/scheduled-recording events) - a plain "Removed from channel group" event
        # here would misdescribe what happened to them, so only genuine removals count.
        _grouping_events(removed, g, CHANNEL_UNGROUPED,
                         'Removed from channel group "{group_name}"')
        cancelled = _apply_demotion(g, state) if demoting else []
        # Transferred channels are already covered - transfer_channel_state() recomputes
        # both ends itself, because it moves guide rows as well as memberships.
        channel_hiding.recompute([ch.id for ch in removed])
        # A transfer changes this group's member set too - the source's membership is
        # reassigned to the keeper or deleted - so the date moves for either outcome,
        # not only for a plain removal. transfer_channel_state() also touches every
        # group it moves a membership in, which covers the OTHER groups the source
        # belonged to; this covers the one the request was aimed at.
        if removed or transferred:
            touch_group(g)
        db.session.commit()
        return ([ch.id for ch in removed], transferred, transfer_skipped, cancelled,
                touched_group_ids - {group_id})

    removed_ids, transferred, transfer_skipped, cancelled, touched = _remove()
    deregister_cancelled_recordings(cancelled)
    # A transfer can also strip the last recording-enabled member out of some OTHER group
    # the source channel belonged to - one nobody is looking at and cannot be prompted
    # about. That is 15's breach path 3 with no human present, so it is alerted rather
    # than acted on: those groups keep their guide rows and say they are broken.
    report_orphaned_guide_groups(touched)
    return jsonify({'success': True, 'removed': removed_ids,
                    'transferred': transferred, 'transfer_skipped': transfer_skipped,
                    'left_guide': bool(demoting and removed_ids),
                    'cancelled_recordings': len(cancelled)})


def _invariant_gate(group, losing_channel_ids, confirmed):
    """DESIGN-channel-groups-model.md 15, enforced. Returns (error_response, state) -
    `error_response` is non-None when the caller must stop.

    Every path that can empty a group's recording-enabled set goes through this one
    function: the single switch, the bulk switch, member removal, and apply-format-plan's
    remove option. That is the point. A bulk action that counted members its own way would
    be a guard the user can walk around by selecting two rows instead of one, and 15 is the
    only thing in this model that is enforced rather than warned about. All four paths are
    held down by tests/test_group_guide_invariant.py.

    Two different answers, and the difference is 15.1's:

    - **A capture is under way: refused, full stop.** No `confirm` overrides it. Killing
      a live recording as a side effect of a checkbox is what product principle 2
      forbids; aborting one is its own deliberate verb and the user has to reach for it.
    - **Otherwise: confirmed, then carried out.** 409 the first time with the facts the
      dialog has to name, and go ahead once the client says `confirm`. The client cannot
      skip this by never asking - it is the server that refuses, and the confirm flag
      only says the sentence was shown.
    """
    state = guide_invariant_check(group, losing_channel_ids)
    if not state['breaches']:
        return None, state
    if state['live']:
        rec = state['live'][0]
        until = format_local(rec.stop_time, style='clock', none_value='an unknown time')
        return (jsonify({
            'error': (f'"{group.name}" is recording until {until}. Turning off the last '
                      'member it can record from would leave that recording with nowhere '
                      'to fail over to. Let it finish, or abort it deliberately, then '
                      'come back to this.'),
            'recording_in_progress': {'recording_id': rec.id, 'name': rec.name,
                                      'until': until},
        }), 409), state
    if not confirmed:
        return (jsonify({
            'error': 'This would leave the group with no member to record from.',
            'confirm_required': {
                'group_name': group.name,
                'in_guide': state['in_guide'],
                'losing': [{'channel_id': ch.id, 'channel_name': ch.name}
                           for ch in state['losing']],
                'scheduled_count': len(state['scheduled']),
            },
        }), 409), state
    return None, state


def _apply_demotion(group, state):
    """The write half of the gate above, for a breach the user has confirmed.

    Called INSIDE the caller's retry_on_locked closure, so the participation write and
    the demotion commit together - a half-applied breach is a guide row that cannot
    record, which is the state this whole item exists to refuse. Returns the recording
    ids the caller deregisters after that commit lands.

    A group that is not in the guide has nothing to demote: 15's confirm still happens
    (the user is told they are turning off the last one) but there is no row to remove
    and no schedule counting on it, so this is correctly a no-op.

    The scheduled list is re-read here rather than taken from `state`, which was built
    before the closure: a lock-retry rolls the session back and expires everything it
    held, so the pre-gate list is exactly the stale read retry_on_locked's whole-unit
    rule exists to avoid. `state` is consulted only for the guide flag, which the gate
    and this call cannot disagree about within one request."""
    if not state['in_guide']:
        return []
    scheduled = group_scheduled_recordings(group)
    detail = (f'Removed from the TV Guide - no member of "{group.name}" is switched on '
              'for recording.')
    if scheduled:
        n = len(scheduled)
        detail += f" {n} scheduled recording{'' if n == 1 else 's'} cancelled."
    return demote_group_from_guide(group, scheduled, detail)


@channel_groups_bp.route('/api/channel-groups/<int:group_id>/members/participation',
                         methods=['POST'])
def set_member_participation(group_id):
    """Set one membership's Recording or Health check switch, from the group page.

    The write itself goes through channel_groups.py::set_participation(), the only
    writer of either column and the one place the change is logged; this route is one of
    its two callers (the other is the health check's own channel list,
    channel_tests.py::toggle_job_channel_enabled). An unknown field is a 400 here rather
    than a ValueError from the helper - enforcement lives server-side, never in whichever
    control happened to post it.

    Turning Recording off does NOT remove the member from anything else - the guide, its
    standalone recordings and its health checks are untouched, and the member stays
    listed on the group page. The one exception is 15's invariant: turning off the LAST
    recording-enabled member of a group that holds a guide row takes the row with it,
    which is why that case is gated (_invariant_gate) rather than simply done.
    """
    group = db.session.get(ChannelGroup, group_id)
    if group is None:
        return jsonify({'error': 'Group not found'}), 404
    data = request.get_json(silent=True) or {}
    cid = data.get('channel_id')
    field = data.get('field')
    if not cid:
        return jsonify({'error': 'channel_id is required'}), 400
    if field not in PARTICIPATION_FIELDS:
        return jsonify({'error': f'Unknown participation field "{field}"'}), 400
    enabled = bool(data.get('enabled'))

    # Only Recording-off can breach 15 - the health-check switch has no bearing on
    # whether the group can produce a file, and Recording-on only ever adds to the set.
    breaching = field == 'recording_enabled' and not enabled
    err, state = ((None, None) if not breaching else
                  _invariant_gate(group, [cid], bool(data.get('confirm'))))
    if err is not None:
        return err

    demoting = bool(breaching and state['breaches'] and state['in_guide'])

    @retry_on_locked()
    def _set():
        m = ChannelGroupMember.query.filter_by(group_id=group_id, channel_id=cid).first()
        if m is None:
            return None, []
        changed = set_participation(m, field, enabled)
        cancelled = []
        if changed:
            # Inside the same closure and the same commit as the switch itself: a guide
            # row that outlived the member holding it up is precisely the state 15 exists
            # to refuse, so the two facts must land together or not at all.
            if demoting:
                cancelled = _apply_demotion(db.session.get(ChannelGroup, group_id), state)
            db.session.commit()
        return changed, cancelled

    changed, cancelled = _set()
    if changed is None:
        return jsonify({'error': 'Channel is not in this group'}), 404
    deregister_cancelled_recordings(cancelled)
    if changed:
        # The recording-enabled set defines the group's derived format reference, so
        # moving it can change which members read as outliers - re-evaluate so the
        # mismatch log and alert stay honest. Detection only; it writes no membership.
        evaluate_and_reconcile_group(db.session.get(ChannelGroup, group_id))
        # Either direction of this switch can end the broken-guide-row state: Recording
        # back on gives the row something to record from, and a confirmed demotion took
        # the row out of the guide. The helper re-asks the invariant rather than inferring
        # it from which branch ran (dev/changelog/933).
        resolve_broken_guide_row(group_id)
    # `left_guide` is what the client turns into its own sentence, and it is reported
    # rather than inferred: the client's copy of group.in_guide can be a refresh out of
    # date, and a switch that did not actually move demotes nothing.
    return jsonify({'success': True, 'field': field, 'enabled': enabled,
                    'left_guide': bool(demoting and changed),
                    'cancelled_recordings': len(cancelled)})


@channel_groups_bp.route('/api/channel-groups/<int:group_id>/members/participation/bulk',
                         methods=['POST'])
def set_members_participation_bulk(group_id):
    """Set one participation switch across several members at once.

    The bulk half of the switch above, for the group page's selection. It writes through
    the same set_participation() the single-member route does - one call per membership,
    so every move gets its own GROUP_MEMBER_PARTICIPATION event and the Activity Timeline
    reads the same whichever control was used. A bulk path that wrote the columns itself
    would be a second writer, which is exactly what
    tests/test_static_invariants.py::ParticipationWriteBypassTests exists to refuse.

    `moved` is the count that actually changed, not the count submitted: a switch that was
    already where it was asked to go is not something that happened, and the caller says
    so rather than claiming work it did not do.

    It goes through the SAME 15 gate the single switch does (_invariant_gate). A guard a
    bulk action can walk around is not a guard, and selecting every row is the easiest
    way there is to empty a group's recording-enabled set.
    """
    group = db.session.get(ChannelGroup, group_id)
    if group is None:
        return jsonify({'error': 'Group not found'}), 404
    data = request.get_json(silent=True) or {}
    field = data.get('field')
    cids = data.get('channel_ids')
    if field not in PARTICIPATION_FIELDS:
        return jsonify({'error': f'Unknown participation field "{field}"'}), 400
    if not isinstance(cids, list) or not cids:
        return jsonify({'error': 'channel_ids must be a non-empty list'}), 400
    enabled = bool(data.get('enabled'))

    breaching = field == 'recording_enabled' and not enabled
    err, state = ((None, None) if not breaching else
                  _invariant_gate(group, cids, bool(data.get('confirm'))))
    if err is not None:
        return err
    demoting = bool(breaching and state['breaches'] and state['in_guide'])

    @retry_on_locked()
    def _set():
        members = ChannelGroupMember.query.filter(
            ChannelGroupMember.group_id == group_id,
            ChannelGroupMember.channel_id.in_(cids)).all()
        if not members:
            return None, []
        moved = sum(1 for m in members if set_participation(m, field, enabled))
        cancelled = []
        if moved:
            if demoting:
                cancelled = _apply_demotion(db.session.get(ChannelGroup, group_id), state)
            db.session.commit()
        return moved, cancelled

    moved, cancelled = _set()
    if moved is None:
        return jsonify({'error': 'None of those channels are in this group'}), 404
    deregister_cancelled_recordings(cancelled)
    if moved:
        # Same reason as the single-member route: the recording-enabled set defines the
        # derived format reference, so moving it can change who reads as an outlier.
        evaluate_and_reconcile_group(db.session.get(ChannelGroup, group_id))
        resolve_broken_guide_row(group_id)
    return jsonify({'success': True, 'field': field, 'enabled': enabled, 'moved': moved,
                    'left_guide': bool(demoting and moved),
                    'cancelled_recordings': len(cancelled)})


@channel_groups_bp.route('/api/channel-groups/<int:group_id>/rename', methods=['POST'])
@retry_on_locked()
def rename_group(group_id):
    group = db.session.get(ChannelGroup, group_id)
    if group is None:
        return jsonify({'error': 'Group not found'}), 404
    name = ((request.get_json(silent=True) or {}).get('name') or '').strip()
    if not name or len(name) > 255:
        return jsonify({'error': 'Group name is required (max 255 characters)'}), 400
    if group_name_conflict(name, exclude_group_id=group_id):
        return jsonify({'error': f'A group named "{name}" already exists'}), 409
    group.name = name
    db.session.commit()
    return jsonify({'success': True, 'group_name': group.name})


@channel_groups_bp.route('/api/channel-groups/<int:group_id>/guide-toggle', methods=['POST'])
@retry_on_locked()
def toggle_group_guide(group_id):
    """Put the group in the TV Guide, or take it out.

    Adding it is 15's breach path 1, and the ONE place in this model where blocking is
    the right answer rather than warning: a guide row exists to be recorded from, and the
    fix is a single switch away - the promotion walkthrough (14.1) offers it in the same
    click. Refusing costs the user one dialog; allowing it produces a row that looks
    perfectly normal and cannot produce a file.

    Taking it OUT is never refused. Nothing is lost by it and the group keeps its members,
    their switches and its schedule.

    Either direction leaves a trace, through the one writer log_guide_change() - the guide
    row is the group's most visible state and the timeline has to be able to say when it
    appeared or went (dev/changelog/764). The flag and its event are written here, inside
    the route's own retry_on_locked unit, so neither can land without the other."""
    group = db.session.get(ChannelGroup, group_id)
    if group is None:
        return jsonify({'error': 'Group not found'}), 404
    if group.is_system:
        return jsonify({'error': 'The TV Guide Channels group cannot appear in the guide.'}), 400
    if group.in_guide:
        # hidden-recompute-ok: ChannelGroup.in_guide. A member is protected from being
        # hidden by its MEMBERSHIP row, not by whether its group is a guide row, so
        # neither branch of this route can change any channel's hidden state.
        group.in_guide = False
        log_guide_change(group, False, 'Taken out of the TV Guide.')
    else:
        if not any(m.recording_enabled for m in group.memberships):
            return jsonify({
                'error': (f'No member of "{group.name}" is switched on for recording, so '
                          'a guide row for it could not produce a file. Turn Recording on '
                          'for at least one member first.'),
                'needs_recording_member': True,
            }), 409
        group.in_guide = True  # hidden-recompute-ok: ChannelGroup.in_guide, as above
        group.guide_sort_order = _next_guide_sort_order()
        log_guide_change(group, True, 'Added to the TV Guide.')
    db.session.commit()
    # Taking the group out of the guide ends the broken-row state outright: there is no
    # row left to be unable to record from. Called after the commit above so the helper
    # reads the flag this request just wrote (dev/changelog/933).
    resolve_broken_guide_row(group_id)
    return jsonify({'success': True, 'in_guide': group.in_guide})


_RESOLUTION_RE = re.compile(r'^\d{2,5}x\d{2,5}$')


@channel_groups_bp.route('/api/channel-groups/<int:group_id>/format', methods=['POST'])
def set_group_format(group_id):
    """Lock the group format to a resolution+fps, or clear the lock (back to derived).

    Body: {'resolution': '1920x1080', 'fps': 60} to lock; {} or {'clear': true} to
    unlock. On success re-evaluates the group so a new lock's mismatches are logged and
    alerted. The lock filters members where they are chosen; it unticks nothing.

    **Writing a pin by hand IS choosing the `manual` strategy**, so this sets the strategy
    in the same commit (dev/changelog/762). `manual` is the only strategy whose lock belongs
    to the user (DESIGN-channel-groups-model.md 16.2) - and the symmetric half of that rule,
    that setting a non-manual strategy clears the lock, is already enforced in
    set_group_format_strategy and promote. Doing it here is what makes pinning ONE request:
    the settings modal used to post /format and then /format-strategy, so a failed second
    request stranded the pin under the old strategy, where it went on filtering members
    forever while the page said "Healthiest member's format". A client that issues two
    requests is exactly the case that proves the server has to own the invariant."""
    group = db.session.get(ChannelGroup, group_id)
    if group is None:
        return jsonify({'error': 'Group not found'}), 404
    if group.is_system:
        return jsonify({'error': 'The TV Guide Channels group has no group format.'}), 400

    data = request.get_json(silent=True) or {}
    clear = bool(data.get('clear')) or (data.get('resolution') in (None, '') and data.get('fps') in (None, ''))

    resolution = fps = None
    if not clear:
        resolution = (data.get('resolution') or '').strip()
        if not _RESOLUTION_RE.match(resolution):
            return jsonify({'error': 'Resolution must look like "1920x1080".'}), 400
        try:
            fps = int(data.get('fps'))
        except (TypeError, ValueError):
            return jsonify({'error': 'Frame rate must be a whole number.'}), 400
        if fps <= 0:
            return jsonify({'error': 'Frame rate must be a positive whole number.'}), 400

    @retry_on_locked()
    def _save():
        g = db.session.get(ChannelGroup, group_id)
        g.set_locked_format(resolution, fps)
        # Clearing is left alone: a group on `manual` with no pin filters nothing, which is
        # a legal resting state and the repair action for a pin somebody regrets. It is
        # storing one that has to imply the strategy.
        if not clear:
            g.format_strategy = GROUP_FORMAT_MANUAL
        db.session.commit()
        return g
    group = _save()

    evaluate_and_reconcile_group(group)
    return jsonify({'success': True, 'locked': group.locked_format_key is not None,
                    'format': format_label(group.locked_format_key),
                    'format_strategy': group.format_strategy})


@channel_groups_bp.route('/api/channel-groups/<int:group_id>/format-strategy', methods=['POST'])
def set_group_format_strategy(group_id):
    """Set the group's standing format strategy - which format the group should be
    (DESIGN-channel-groups-model.md 4.4).

    It replaces the old auto-disable toggle, which no longer has anything to toggle: a
    strategy writes the format LOCK, and the lock filters members at selection time
    rather than unticking them.

    The choice is **applied as well as stored** (dev/changelog/753). A standing setting
    that visibly does nothing until the next health check run reads as broken, and the
    engine is the same one that runs nightly, so the two can never disagree about what
    the setting means. It only ever moves the lock - removing the members that do not
    match stays the explicit apply-format-plan press, because destroying membership rows
    is not something a dropdown should do on change.
    """
    group = db.session.get(ChannelGroup, group_id)
    if group is None:
        return jsonify({'error': 'Group not found'}), 404
    if group.is_system:
        return jsonify({'error': 'The TV Guide Channels group has no format strategy.'}), 400

    strategy = (request.get_json(silent=True) or {}).get('strategy')
    if strategy not in GROUP_FORMAT_STRATEGIES:
        return jsonify({'error': f'Unknown format strategy "{strategy}"'}), 400

    @retry_on_locked()
    def _save():
        g = db.session.get(ChannelGroup, group_id)
        g.format_strategy = strategy
        # `manual` is the ONLY strategy whose lock belongs to the user. Every other value
        # either writes the lock itself (the four bucket-ranking ones, below) or does not
        # use it at all, so a pin left behind by a previous `manual` has no standing under
        # the new choice - and leaving it would make the choice a lie: group_reference_key
        # returns the lock ahead of anything derived, so "Healthiest member's format"
        # would go on enforcing yesterday's pin instead of following the healthiest
        # member. Clearing here also leaves the four engine strategies a clean slate: they
        # write their winner immediately below, and a no-winner run correctly leaves the
        # group filtering nothing rather than filtering to a format nothing chose.
        if strategy != GROUP_FORMAT_MANUAL:
            g.set_locked_format(None, None)
        db.session.commit()
        return g
    group = _save()

    plan = apply_format_strategy(group) or {}
    # apply_format_strategy only reconciles when it actually moved the lock, so the
    # no-move path still needs this: the strategy gate itself changes which members are
    # filtered, and a mismatch that just became relevant has to reach its surfaces.
    if not plan.get('moved'):
        evaluate_and_reconcile_group(group)
    return jsonify({'success': True, 'format_strategy': group.format_strategy,
                    'format': format_label(group.locked_format_key),
                    'lock_moved': bool(plan.get('moved'))})


@channel_groups_bp.route('/api/channel-groups/<int:group_id>/promote', methods=['POST'])
def promote_group(group_id):
    """The promotion walkthrough's one write (DESIGN-channel-groups-model.md 14.1).

    A group is created as a health check and promoted deliberately (14), and promoting it
    is four decisions the user makes in one dialog: the format strategy (plus a pinned
    format when that strategy is `manual`), which members may record, whether the members
    that do not match keep being health checked, and whether the group joins the TV Guide.

    They land as ONE request rather than as four calls from the client. Two reasons, both
    learned here: a client-side Promise.all over several endpoints discards every later
    write when an earlier one fails (dev/changelog/756), and a partial promotion is a
    genuinely bad state - a guide row whose strategy was set but whose members were not,
    or members switched on for a lock that never got written.

    Body: {'strategy', 'resolution'/'fps' (manual only), 'enable': 'matching'|'all'|'none',
    'enable_channel_ids' (what the click that opened the walkthrough was going to switch
    on), 'unmatched_checks': 'keep'|'stop', 'add_to_guide': bool}.

    Ordering inside the closure is load bearing: the pin is written BEFORE the strategy is
    applied, because under `manual` the reconcile reads the stored lock; and the guide row
    is added LAST, after the switches, so 15's invariant is already satisfied by the time
    anything is in the guide.
    """
    group = db.session.get(ChannelGroup, group_id)
    if group is None:
        return jsonify({'error': 'Group not found'}), 404
    if group.is_system:
        return jsonify({'error': 'The TV Guide Channels group cannot be promoted.'}), 400

    data = request.get_json(silent=True) or {}
    strategy = data.get('strategy')
    if strategy not in GROUP_FORMAT_STRATEGIES:
        return jsonify({'error': f'Unknown format strategy "{strategy}"'}), 400
    if strategy == GROUP_FORMAT_HEALTH_CHECK_ONLY:
        return jsonify({'error': 'Promoting a group means choosing a format strategy '
                                 'other than health checks only.'}), 400
    enable = data.get('enable', 'none')
    if enable not in ('matching', 'all', 'none'):
        return jsonify({'error': "enable must be 'matching', 'all' or 'none'"}), 400
    unmatched_checks = data.get('unmatched_checks', 'keep')
    if unmatched_checks not in ('keep', 'stop'):
        return jsonify({'error': "unmatched_checks must be 'keep' or 'stop'"}), 400
    add_to_guide = bool(data.get('add_to_guide'))

    resolution = fps = None
    if strategy == GROUP_FORMAT_MANUAL:
        resolution = (data.get('resolution') or '').strip()
        if not _RESOLUTION_RE.match(resolution):
            return jsonify({'error': 'Pick a format to pin, like "1920x1080".'}), 400
        try:
            fps = int(data.get('fps'))
        except (TypeError, ValueError):
            return jsonify({'error': 'Frame rate must be a whole number.'}), 400
        if fps <= 0:
            return jsonify({'error': 'Frame rate must be a positive whole number.'}), 400

    pending_ids = {cid for cid in (data.get('enable_channel_ids') or []) if cid}

    @retry_on_locked()
    def _promote():
        g = db.session.get(ChannelGroup, group_id)
        g.format_strategy = strategy
        # Same rule set_group_format_strategy() enforces: `manual` is the only strategy
        # whose lock belongs to the user, so every other value starts from a clean slate
        # rather than inheriting a pin nothing chose.
        if strategy == GROUP_FORMAT_MANUAL:
            g.set_locked_format(resolution, fps)
        else:
            g.set_locked_format(None, None)
        db.session.commit()
        return g
    group = _promote()

    # Outside the closure: its own read-modify-write unit, and it must not be re-run by a
    # retry of the write above. This is what turns the four bucket-ranking strategies into
    # an actual lock, so it has to happen before "which members match" is asked.
    apply_format_strategy(group)

    group = db.session.get(ChannelGroup, group_id)
    members = member_channels(group.memberships)
    latest = _latest_tests_by_channel([ch.id for ch in members])
    reference = group_reference_key(group, group.memberships, latest)
    matching_ids = {ch.id for ch in members if format_key(latest.get(ch.id)) == reference}
    # An unmeasured member is never counted as non-matching: unknown is not
    # proven-different, and turning its health check off for failing a comparison nothing
    # could make is how a member ends up recordable with no data behind it forever.
    unmatched_ids = {ch.id for ch in members
                     if format_key(latest.get(ch.id)) and ch.id not in matching_ids}

    if enable == 'all':
        turn_on = {ch.id for ch in members}
    elif enable == 'matching':
        turn_on = set(matching_ids)
    else:
        turn_on = set()
    turn_on |= pending_ids

    @retry_on_locked()
    def _switches():
        rows = ChannelGroupMember.query.filter_by(group_id=group_id).all()
        on = sum(1 for m in rows
                 if m.channel_id in turn_on and set_participation(m, 'recording_enabled', True))
        off = 0
        if unmatched_checks == 'stop':
            off = sum(1 for m in rows
                      if m.channel_id in unmatched_ids
                      and set_participation(m, 'test_enabled', False))
        g = db.session.get(ChannelGroup, group_id)
        joined = False
        # 15's invariant, satisfied by construction rather than checked afterwards: the
        # guide flag is only written once the switches above have gone in, and only if
        # something is actually switched on.
        if add_to_guide and not g.in_guide and any(m.recording_enabled for m in rows):
            # hidden-recompute-ok: ChannelGroup.in_guide - promotion adds no memberships,
            # and a membership is what protects a channel from being hidden.
            g.in_guide = True
            g.guide_sort_order = _next_guide_sort_order()
            log_guide_change(g, True, 'Added to the TV Guide when the group was promoted.')
            joined = True
        if on or off or joined:
            db.session.commit()
        return on, off, joined
    enabled_count, unchecked_count, joined_guide = _switches()

    group = db.session.get(ChannelGroup, group_id)
    evaluate_and_reconcile_group(group)
    # The walkthrough's whole job is turning Recording on, so it is the most likely path
    # of all to end a broken guide row (dev/changelog/933).
    resolve_broken_guide_row(group_id)
    return jsonify({
        'success': True,
        'format_strategy': group.format_strategy,
        'format': format_label(group.locked_format_key),
        'enabled': enabled_count,
        'unchecked': unchecked_count,
        'in_guide': group.in_guide,
        'joined_guide': joined_guide,
        'recording_members': sum(1 for m in group.memberships if m.recording_enabled),
    })


@channel_groups_bp.route('/api/channel-groups/<int:group_id>/warnings', methods=['POST'])
def set_group_warnings(group_id):
    """Hide or re-arm this group's warning banners
    (DESIGN-channel-groups-model.md 16.2).

    Body: {'warnings': {kind: shown_bool, ...}} - the Settings modal's Warnings block
    posts the whole set it rendered, and a kind it did not send is left alone rather than
    treated as re-armed. The value is what the user sees on the switch (**shown**), which
    is the inverse of what is stored, so the flip happens here rather than in the client:
    a UI that has to remember to invert a boolean will eventually forget.

    The banner's own "Hide this warning" button posts a single-entry map through the same
    route. There is no second endpoint for it, because there is no second decision.

    Every move writes its own GROUP_WARNING_MUTED event through set_warning_muted(), the
    one writer of the column - so a warning cannot go quiet without the group's Activity
    Timeline saying who silenced it and when.
    """
    group = db.session.get(ChannelGroup, group_id)
    if group is None:
        return jsonify({'error': 'Group not found'}), 404
    if group.is_system:
        return jsonify({'error': 'The TV Guide Channels group has no warnings to hide.'}), 400

    shown = (request.get_json(silent=True) or {}).get('warnings')
    if not isinstance(shown, dict) or not shown:
        return jsonify({'error': 'warnings must be a non-empty object'}), 400
    unknown = [k for k in shown if k not in GROUP_WARNING_KINDS]
    if unknown:
        return jsonify({'error': f'Unknown warning kind "{unknown[0]}"'}), 400

    @retry_on_locked()
    def _save():
        g = db.session.get(ChannelGroup, group_id)
        moved = sum(1 for kind, is_shown in shown.items()
                    if set_warning_muted(g, kind, not is_shown))
        if moved:
            db.session.commit()
        return g, moved
    group, moved = _save()

    muted = group.muted_warning_set()
    return jsonify({'success': True, 'moved': moved,
                    'muted': [k for k in GROUP_WARNING_KINDS if k in muted]})


@channel_groups_bp.route('/api/channel-groups/<int:group_id>/apply-format-plan', methods=['POST'])
def apply_format_plan(group_id):
    """Apply an auto-select-format strategy (app/channel_groups.py::plan_format_selection)
    to an EXISTING group's current members: lock the group format to the winner, then
    either disable or remove the members that don't match it. Recomputes the plan from
    the group's live members and their latest tests server-side - a client-supplied
    channel list is never trusted (CLAUDE.md: enforcement lives server-side).

    Body: {'strategy': one of FORMAT_STRATEGIES, 'non_matching': 'keep' | 'remove'}.
    'keep' only sets the lock: the members that do not match it are filtered out wherever
    members are chosen, and are left in the group untouched, so one that starts matching
    again is eligible again on its own (DESIGN-channel-groups-model.md 4.1). 'remove'
    deletes their ChannelGroupMember rows outright (mirrors remove_members, including its
    ChannelEvent), then locks.

    Uses each member's own latest test regardless of job (the same ANY_JOB default
    evaluate_and_reconcile_group and apply_format_strategy use), never a specific job_id -
    every surface that decides or describes a lock reads the same data or it is deciding
    about a different app (dev/changelog/890)."""
    group = db.session.get(ChannelGroup, group_id)
    if group is None:
        return jsonify({'error': 'Group not found'}), 404
    if group.is_system:
        return jsonify({'error': 'The TV Guide Channels group has no group format.'}), 400

    data = request.get_json(silent=True) or {}
    strategy = data.get('strategy')
    if strategy not in FORMAT_STRATEGIES:
        return jsonify({'error': f'strategy must be one of {", ".join(FORMAT_STRATEGIES)}'}), 400
    non_matching = data.get('non_matching')
    if non_matching not in ('keep', 'remove'):
        return jsonify({'error': "non_matching must be 'keep' or 'remove'"}), 400

    memberships = list(group.memberships)
    members = member_channels(memberships)
    latest = _latest_tests_by_channel([ch.id for ch in members])
    # Ranked over the lock population, exactly as apply_format_strategy() ranks it, but
    # the winning entry's `channel_ids` still covers every member measuring that format.
    # That split is load-bearing here and nowhere else: `non_matching_ids` below can
    # DELETE members, so narrowing the membership half would delete every health-check-only
    # member as a side effect of a ranking change (dev/changelog/890).
    plan = plan_format_selection(members, latest, rank_ids=lock_ranking_ids(memberships))
    entry = plan['strategies'][strategy]
    if entry['key'] is None:
        return jsonify({'error': entry['rationale']}), 400

    winning_ids = {cid for cid in entry['channel_ids']}
    non_matching_ids = [ch.id for ch in members if ch.id not in winning_ids]
    if len(non_matching_ids) >= len(members):
        return jsonify({'error': 'This would remove every member of the group.'}), 400

    resolution, fps = entry['resolution'], entry['fps']
    removed_ids = []
    cancelled = []
    demoting = False

    if non_matching == 'remove':
        # The fourth path that can empty the recording-enabled set, and it takes the same
        # gate as the other three. "This would remove every member" above is a different
        # and weaker check: a group can keep members and still lose every one it was
        # willing to record from.
        err, state = _invariant_gate(group, non_matching_ids, bool(data.get('confirm')))
        if err is not None:
            return err
        demoting = bool(state['breaches'] and state['in_guide'])

        @retry_on_locked()
        def _remove_and_lock():
            g = db.session.get(ChannelGroup, group_id)
            removed = []
            for cid in non_matching_ids:
                m = ChannelGroupMember.query.filter_by(group_id=group_id, channel_id=cid).first()
                if m is not None:
                    removed.append(m.channel)
                    db.session.delete(m)
            _grouping_events(removed, g, CHANNEL_UNGROUPED,
                             'Removed from channel group "{group_name}"')
            apply_lock_and_log(g, strategy, entry, non_matching, len(removed))
            dropped = _apply_demotion(g, state) if demoting else []
            channel_hiding.recompute([ch.id for ch in removed])
            # The lock write above already moves the date through `onupdate`; saying so
            # here anyway keeps the membership change itself responsible for it, rather
            # than leaving it to a side effect of the write beside it.
            touch_group(g)
            db.session.commit()
            return [ch.id for ch in removed], dropped
        removed_ids, cancelled = _remove_and_lock()
        deregister_cancelled_recordings(cancelled)
    else:
        @retry_on_locked()
        def _lock():
            g = db.session.get(ChannelGroup, group_id)
            apply_lock_and_log(g, strategy, entry, non_matching, 0)
            db.session.commit()
        _lock()

    group = db.session.get(ChannelGroup, group_id)
    diff = evaluate_and_reconcile_group(group) or {}
    return jsonify({
        'success': True,
        'format': {'resolution': resolution, 'fps': fps, 'label': entry['label']},
        'kept': entry['count'],
        'filtered': len(diff.get('outliers') or []),
        'removed': len(removed_ids),
        'left_guide': bool(demoting and removed_ids),
        'cancelled_recordings': len(cancelled),
    })


@channel_groups_bp.route('/api/channel-groups/<int:group_id>/delete', methods=['POST'])
def delete_group(group_id):
    """Dissolve the group. Its members are untouched: each keeps whatever guide row it
    had of its own, because grouping never took one away (dev/changelog/751). The group's
    own row goes with the group. This used to be a restore contract - Channel.in_guide
    doubled as "put this back as a row if the group dissolves" - and that second meaning
    is what made the column unreadable.

    A group is a health check's channel list (groups unification 3/4), so any
    attached OnDemandTestJob(s) are cascade-deleted first - scheduler entry and
    ChannelTest rows via teardown_test_job(), same teardown delete_on_demand_job()
    does for a standalone job delete. Blocked only when a job is actively RUNNING,
    since cascading through a live test run isn't safe.

    Recordings take DESIGN-channel-groups-model.md 15.1's split, the same one the
    participation switch takes, because dissolving the group is strictly the more
    destructive action of the two (dev/changelog/763):

    - **A capture under way refuses the delete outright**, with no confirm that
      overrides it. Foreign keys are off, so the delete would otherwise succeed and
      leave Recording.group_id dangling - and failover_group_member() reads that as
      "no group" and stops failing over, silently, mid-recording.
    - **Scheduled recordings are named, then cancelled** once the user confirms. They
      cannot survive the group: a SCHEDULED row pointing at a deleted group fires later
      and records with no member to resolve.

    The gate runs before the closure below and the scheduler work after it, so the
    retried unit is the database write alone."""
    group = db.session.get(ChannelGroup, group_id)
    if group is None:
        return jsonify({'error': 'Group not found'}), 404
    if group.is_system:
        return jsonify({'error': 'The TV Guide Channels group cannot be deleted'}), 400
    jobs = list(group.test_jobs)
    running = [j for j in jobs if j.status == 'RUNNING']
    if running:
        names = ', '.join(f'"{j.name}"' for j in running)
        plural = 's' if len(running) != 1 else ''
        return jsonify({'error': f'Health check{plural} {names} still running - stop before deleting this group'}), 409

    live = group_live_recordings(group)
    if live:
        rec = live[0]
        until = format_local(rec.stop_time, style='clock', none_value='an unknown time')
        return jsonify({
            'error': (f'"{group.name}" is recording until {until}. Deleting the group '
                      'would leave that recording with nowhere to fail over to. Let it '
                      'finish, or abort it deliberately, then come back to this.'),
            'recording_in_progress': {'recording_id': rec.id, 'name': rec.name,
                                      'until': until},
        }), 409
    scheduled = group_scheduled_recordings(group)
    if scheduled and not (request.get_json(silent=True) or {}).get('confirm'):
        return jsonify({
            'error': 'This group has scheduled recordings that would be cancelled.',
            'confirm_required': {
                'group_name': group.name,
                'scheduled_count': len(scheduled),
                'scheduled': [{'recording_id': r.id, 'name': r.name,
                               'start': format_local(r.start_time, style='clock',
                                                     none_value='an unknown time')}
                              for r in scheduled[:5]],
            },
        }), 409

    @retry_on_locked()
    def _delete():
        g = db.session.get(ChannelGroup, group_id)
        detail = f'Channel group "{g.name}" was deleted.'
        cancelled = cancel_scheduled_recordings(group_scheduled_recordings(g), detail)
        screenshot_paths = []
        for job in list(g.test_jobs):
            screenshot_paths.extend(teardown_test_job(job))
            db.session.delete(job)
        members = member_channels(g.memberships)
        _grouping_events(members, g, CHANNEL_UNGROUPED,
                         'Channel group "{group_name}" dissolved')
        db.session.delete(g)  # membership rows go via the delete-orphan cascade
        # After the group delete, not before it: the memberships that protect these
        # channels only stop existing when that cascade flushes, and a recompute run one
        # line earlier would still see them and leave every deferred hide deferred.
        channel_hiding.recompute([ch.id for ch in members])
        db.session.commit()
        return cancelled, screenshot_paths

    cancelled, screenshot_paths = _delete()
    from ..recorder import delete_files
    delete_files(screenshot_paths)
    deregister_cancelled_recordings(cancelled)
    # The group is gone, so an alert saying its guide row cannot record names a row that
    # no longer exists - and its deep link would 404 (dev/changelog/933).
    resolve_broken_guide_row(group_id)
    return jsonify({'success': True, 'cancelled_recordings': len(cancelled)})


@channel_groups_bp.route('/api/channel-groups/<int:group_id>/clone', methods=['POST'])
def clone_group(group_id):
    """Clone a group's membership into a new group (DESIGN.md §14.5). `channel_ids` lets
    the caller narrow the copy, so a prune flow clones only the surviving set the user
    picked rather than cloning-then-pruning."""
    src = db.session.get(ChannelGroup, group_id)
    if src is None:
        return jsonify({'error': 'Group not found'}), 404

    data = request.get_json(silent=True) or {}
    name = (data.get('name') or '').strip()
    if not name or len(name) > 255:
        return jsonify({'error': 'Group name is required (max 255 characters)'}), 400
    if group_name_conflict(name):
        return jsonify({'error': f'A group named "{name}" already exists'}), 409
    strategy = data.get('format_strategy') or src.format_strategy
    if strategy not in GROUP_FORMAT_STRATEGIES:
        return jsonify({'error': f'Unknown format strategy "{strategy}"'}), 400

    if src.is_system:
        src_channels, _disabled = check_target_channels(src)
    else:
        src_channels = member_channels(src.memberships)
    requested_ids = data.get('channel_ids')
    if requested_ids is not None:
        keep = set(requested_ids)
        src_channels = [ch for ch in src_channels if ch.id in keep]
    if not src_channels:
        return jsonify({'error': 'A group needs at least one channel'}), 400

    # Group settings the Create-group modal sets at creation time (dev/changelog/322).
    # All optional - a plain clone passes none of them.
    allow_mismatch = bool(data.get('allow_format_mismatch'))
    in_guide = bool(data.get('in_guide'))
    fmt_res = (data.get('format_resolution') or '').strip() or None
    fmt_fps = data.get('format_fps')
    fmt_fps = int(fmt_fps) if str(fmt_fps or '').strip().isdigit() else None
    if (fmt_res is None) != (fmt_fps is None):
        return jsonify({'error': 'A manual group format needs both a resolution and a frame rate'}), 400

    # Warned, not refused (dev/changelog/762). `allow_format_mismatch` is the caller's
    # explicit press of the confirm, and it is checked HERE rather than only in the modal
    # because enforcement lives server-side - a client that pre-empts the warning with its
    # own count is a convenience, never the thing that decides.
    if strategy != GROUP_FORMAT_HEALTH_CHECK_ONLY and not allow_mismatch:
        mismatch, _unverified = _format_warnings([], src_channels)
        if mismatch:
            return jsonify({'success': False, 'format_mismatch': mismatch})

    channel_ids = [ch.id for ch in src_channels]
    # A stored format lock belongs to `manual` and to nothing else
    # (DESIGN-channel-groups-model.md 16.2). Every other strategy either writes the lock
    # itself or does not read one, so a pin carried in under one of them would filter
    # members on a format nothing chose while the group's own settings card named a
    # different rule - and nothing would ever clear it. Refused rather than stored, and
    # reported rather than silently dropped. health_check_only falls out of the same rule:
    # it is not a recording source until it is promoted (15), so a lock on it is a setting
    # nothing reads.
    pin_refused = False
    if (fmt_res is not None or fmt_fps is not None) and strategy != GROUP_FORMAT_MANUAL:
        fmt_res, fmt_fps, pin_refused = None, None, True
    # 15's invariant applies to a clone exactly as it does to a switch. A clone's members
    # take the model defaults - Recording OFF on every one of them (14) - so a clone that
    # honored in_guide would land a guide row with nothing behind it at the moment of
    # creation. The copy is made out of the guide and the caller is told to promote it,
    # which is the walkthrough's job and asks the format question properly.
    guide_refused = in_guide
    in_guide = False
    guide_order = None

    @retry_on_locked()
    def _clone():
        chans = Channel.query.filter(Channel.id.in_(channel_ids)).all()
        by_id = {ch.id: ch for ch in chans}
        # A clone starts out of the TV Guide unless the caller asked otherwise - only one
        # group can be the guide row for a given set of channels, so this is opt-in.
        group = ChannelGroup(name=name, format_strategy=strategy,
                            cloned_from_group_id=src.id, cloned_from_name=src.name)
        group.in_guide = in_guide
        if guide_order is not None:
            group.guide_sort_order = guide_order
        group.format_resolution = fmt_res
        group.format_fps = fmt_fps
        db.session.add(group)
        db.session.flush()
        # Membership participation takes the model defaults, exactly as a fresh create
        # does - a clone is not a promotion, so Recording starts off on every member.
        for idx, cid in enumerate(channel_ids):
            db.session.add(ChannelGroupMember(group_id=group.id, channel_id=cid, position=idx))
        _grouping_events([by_id[cid] for cid in channel_ids], group,
                         CHANNEL_GROUPED, 'Added to channel group "{group_name}"')
        channel_hiding.recompute(channel_ids)
        db.session.commit()
        return group.id
    new_id = _clone()

    group = db.session.get(ChannelGroup, new_id)
    # Outside the commit closure on purpose: it is its own read-modify-write unit and
    # must not be re-run by a retry of the create above.
    evaluate_and_reconcile_group(group)
    return jsonify({'success': True, 'group_id': group.id, 'group_name': group.name,
                    'format_strategy': strategy,
                    # Reported, not silently dropped: the caller asked for a guide row and
                    # did not get one, and product principle 1 says the reason belongs on
                    # a surface rather than in a log line nobody reads. Same for a format
                    # pin the clone's strategy has no standing to hold.
                    'guide_refused': guide_refused,
                    'format_pin_refused': pin_refused,
                    'detail_url': url_for('channel_groups.group_detail', group_id=group.id)})


@channel_groups_bp.route('/api/channel-groups/<int:group_id>/clone-info', methods=['GET'])
def clone_info(group_id):
    """Seed data for the unified Clone modal (clone-modal.js) - one canonical shape so
    the list page and every detail page's kebab feed the same modal identically instead
    of each hand-building its own ad hoc payload (dev/changelog/542).

    `has_schedule` is true when a health check is attached to this group, and is what
    decides whether the modal offers the group/check/both picker at all. An inherited
    automatic TV Guide check does not count: it is attached to the system group rather
    than to this one, so it is not this group's to clone. `channel_settings` is always
    present - every group has guide and format settings (dev/changelog/741) - while
    `check` is there only when a check really is attached, so the client can tell
    "nothing to carry over" apart from "carry over these empty/default values".

    A backticked lowercase name in this docstring is a field of the response, and
    tests/test_clone_modal.py checks that it still is one."""
    group = db.session.get(ChannelGroup, group_id)
    if group is None:
        return jsonify({'error': 'Group not found'}), 404
    if group.is_system:
        return jsonify({'error': 'Cannot clone the pinned TV Guide Channels check'}), 400

    memberships = list(group.memberships)
    channels = member_channels(memberships)
    channel_ids = [ch.id for ch in channels]
    latest = _latest_tests_by_channel(channel_ids, for_job_id=ANY_JOB)
    participating_ids = participating_member_ids(group, memberships)
    off_tooltip = (_RECORDING_OFF_TOOLTIP if participation_is_recording(group)
                   else _TEST_DISABLED_TOOLTIP)
    channel_payload = [
        _member_ctx(ch, latest,
                    None if ch.id in participating_ids else off_tooltip,
                    is_best=False)
        for ch in channels
    ]

    channel_settings = {
        'in_guide': group.in_guide,
        'format_mode': 'manual' if (group.format_resolution and group.format_fps) else 'auto',
        'format_resolution': group.format_resolution,
        'format_fps': group.format_fps,
        'format_strategy': group.format_strategy,
    }

    job = _group_detail_job(group)
    check_payload = None
    if job is not None:
        ct_cfg = load_config().get('channel_testing', {})
        check_payload = {
            'job_id': job.id,
            'profile_id': job.profile_id,
            'schedule': _schedule_ctx(job, ct_cfg),
        }

    return jsonify({
        'id': group.id, 'name': group.name,
        'channels': channel_payload,
        'has_schedule': job is not None,
        'channel_settings': channel_settings,
        'check': check_payload,
    })


@channel_groups_bp.route('/api/channel-groups/<int:group_id>/record-context', methods=['GET'])
def group_record_context(group_id):
    """What the shared scheduling modal has to say out loud about a GROUP target
    (`templates/_record_modal.html`, `static/js/guide.js::openModal`, dev/changelog/904).

    A group-backed recording does not record "the group" - it records one member, chosen
    at record-start time by the format lock and the health score, and it can hand off to
    another member mid-run. Every one of those decisions was already made and logged; the
    modal was simply the one surface that never mentioned them, so scheduling against a
    group looked identical to scheduling a single channel while quietly meaning something
    else. Product principle 1 - the app says what it is about to do on your behalf.

    Answered fresh per Record click rather than carried on the row payload, and that is the
    point: `Recording.channel_id` is stamped when the recording is created and RE-RESOLVED
    at start (`app/recorder.py::start_recording`), so the stamped member is provisional and
    an Edit opened days later would otherwise name a channel the recorder has already
    stopped preferring.

    `serving` is null when nothing is eligible - a group with no recording-enabled member,
    which is what a health_check_only group is by construction. The modal says so rather
    than falling silent; a group that cannot produce a file is exactly the state the user
    must not have to infer.

    `format_override` is the lock's zero-survivors case (DESIGN-channel-groups-model.md
    15.2): the recording will run anyway, off the group's locked format. It has always been
    disclosed three times, but every one of them lands at or after record start - this is
    the only one the user sees while the choice to schedule is still theirs.

    Read-only, one group, no per-row work. Refuses the pinned system group, which is a
    health-check container and not a recording source.
    """
    group = db.session.get(ChannelGroup, group_id)
    if group is None:
        return jsonify({'error': 'Group not found'}), 404
    if group.is_system:
        return jsonify({'error': 'The pinned TV Guide Channels check is not a '
                                 'recording source'}), 400

    memberships = list(group.memberships)
    cfg = load_config()
    streak_threshold = cfg.get('channel_testing', {}).get(
        'failing_streak_threshold', DEFAULT_FAILING_STREAK_THRESHOLD)
    latest = _latest_tests_by_channel([m.channel_id for m in memberships], for_job_id=ANY_JOB)
    choice = serving_member(group, latest, streak_threshold=streak_threshold)
    serving = choice.member

    return jsonify({
        'success': True,
        'group': {'id': group.id, 'name': group.name},
        'serving': None if serving is None else {
            'id': serving.id,
            'name': serving.name,
            'account_name': serving.account.name if serving.account else '',
        },
        'member_count': len(memberships),
        'recording_member_count': sum(1 for m in memberships if m.recording_enabled),
        'format_override': choice.selection.override,
        # Named only when the lock actually filtered. A strategy that follows the data has
        # no lock to show, and showing the last key it derived would read as a setting the
        # user made (dev/changelog/762).
        'locked_format': (format_label(choice.selection.reference)
                          if choice.selection.reference else ''),
    })


# ── Suggest-duplicates helper ───────────────────────────────────────────────

@channel_groups_bp.route('/api/channel-groups/suggest')
def suggest():
    """Candidates likely to be the same logical channel as the seed. Seed with
    ?channel_id=N, or ?group_id=N (union of suggestions across all members)."""
    channel_id = request.args.get('channel_id', type=int)
    group_id = request.args.get('group_id', type=int)

    group = None
    if channel_id:
        seed = db.session.get(Channel, channel_id)
        if seed is None:
            return jsonify({'error': 'Channel not found'}), 404
        seed_channels = [seed]
    elif group_id:
        group = db.session.get(ChannelGroup, group_id)
        if group is None:
            return jsonify({'error': 'Group not found'}), 404
        seed_channels = member_channels(group.memberships)
        if not seed_channels:
            return jsonify({'results': []})
    else:
        return jsonify({'error': 'channel_id or group_id required'}), 400

    all_channels = Channel.query.all()
    latest_by_channel = _latest_tests_by_channel([c.id for c in all_channels])

    # The reference format every candidate is classified against - lock-aware for a group
    # (group_reference_key: locked format, else best active member), or the single seed's
    # own format for the Browse-tab channel seed. Classifying against ONE reference is what
    # keeps a 720p candidate from reading "same format" just because it matches one of a
    # mixed group's 720p members.
    if group is not None:
        streak_threshold = load_config().get('channel_testing', {}).get(
            'failing_streak_threshold', DEFAULT_FAILING_STREAK_THRESHOLD)
        ref_key = group_reference_key(group, list(group.memberships), latest_by_channel,
                                      streak_threshold)
    else:
        ref_key = format_key(latest_by_channel.get(seed_channels[0].id))

    # Union candidates across all seeds, keeping the strongest match reason AND the
    # best (most-confident) format status seen from any seed.
    best = {}   # channel_id -> {'ch', 'reason', 'status'}
    for seed in seed_channels:
        for ch, reason, status in suggest_candidates(seed, all_channels, latest_by_channel,
                                                     reference_key=ref_key):
            cur = best.get(ch.id)
            if cur is None:
                best[ch.id] = {'ch': ch, 'reason': reason, 'status': status}
            else:
                if MATCH_REASON_STRENGTH[reason] < MATCH_REASON_STRENGTH[cur['reason']]:
                    cur['reason'] = reason
                if FORMAT_STATUS_STRENGTH[status] < FORMAT_STATUS_STRENGTH[cur['status']]:
                    cur['status'] = status

    seed_ids = {c.id for c in seed_channels}
    # 'different' candidates are known-incompatible (the create/add guard hard-blocks them),
    # but we still list them as red, non-selectable "mismatch" rows for visibility.
    results = []
    for entry in best.values():
        ch, reason, status = entry['ch'], entry['reason'], entry['status']
        if ch.id in seed_ids:
            continue
        test = latest_by_channel.get(ch.id)
        results.append({
            'channel_id': ch.id,
            'channel_name': ch.name,
            'account_name': ch.account.name,
            'account_color': ch.account.color,
            'reason': reason,
            'format_status': status,
            'selectable': status != 'different',
            'format': format_label(format_key(test)),
            'resolution': (test.resolution if test else None),
            'fps': (round(test.fps) if test and test.fps else None),
            'bitrate_kbps': (test.bitrate_kbps if test else None),
            'effective_score': effective_score(ch),
            'health_score': ch.health_score,
            'in_guide': ch.in_guide,
            'stream_url': ch.stream_url,
        })
    results.sort(key=lambda r: (FORMAT_STATUS_STRENGTH[r['format_status']],
                                MATCH_REASON_STRENGTH[r['reason']], -r['effective_score']))
    results = results[:100]
    # Provider-removed state for just the returned rows, batched - the same signal the
    # Remove Duplicates modal and the member table badge (dev/changelog/652, 1042). Marked,
    # never filtered or demoted: the user decides whether a dropped feed is still wanted.
    lifecycle_by_channel = lifecycle_states_for_channels(
        [best[r['channel_id']]['ch'] for r in results], load_config())
    for r in results:
        state, since = lifecycle_by_channel.get(r['channel_id'], (None, None))
        r['lifecycle'] = state
        r['lifecycle_date'] = since.strftime('%Y-%m-%d') if since else ''
    # Existing memberships (any kind) for just the returned rows, one batched query -
    # never a per-channel lazy load over the whole candidate set.
    result_ids = [r['channel_id'] for r in results]
    groups_by_channel = {}
    if result_ids:
        rows = (db.session.query(ChannelGroupMember.channel_id, ChannelGroup.name)
                .join(ChannelGroup, ChannelGroupMember.group_id == ChannelGroup.id)
                .filter(ChannelGroupMember.channel_id.in_(result_ids))
                .order_by(db.func.lower(ChannelGroup.name)).all())
        for cid, gname in rows:
            groups_by_channel.setdefault(cid, []).append(gname)
    for r in results:
        r['current_groups'] = groups_by_channel.get(r['channel_id'], [])
    different_count = sum(1 for r in results if r['format_status'] == 'different')
    return jsonify({
        'results': results,
        'seed_format': format_label(ref_key),
        'seed_format_known': ref_key is not None,
        'different_count': different_count,
    })
