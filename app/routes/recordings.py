import json
import logging
import os
import threading
import time
import uuid
from datetime import datetime, timedelta

from flask import (
    Blueprint, render_template, request, redirect, url_for, flash, jsonify,
    send_file, abort, make_response,
)

from .. import db
from ..database import (
    Recording, Channel, ChannelGroup, Account, EPGEntry, RecordingEvent, RecordingSegment,
    RECORDING_CREATED_AFTER_EVENT_START, RECORDING_EDITED, RECORDING_STOP_TIME_ADJUSTED,
    RECORDING_REPLACED_OTHER, RESTART_BLOCKING_STATUSES, RECORDING_ABORTED,
    DIAGNOSTICS, RESTART_ATTEMPTED, RESTART_FAILED, RESTART_SUCCEEDED, STALL_DETECTED,
    FAST_DELIVERY_DETECTED, RECORDING_RESUMED, GROUP_FAILOVER, SEGMENT_ENDED,
    add_recording_event, detach_recording_references,
    group_event_channel_links,
    REC_STATUS_SCHEDULED, REC_STATUS_IN_PROGRESS, REC_STATUS_PAUSED, REC_STATUS_RETRYING,
    REC_STATUS_CONCATENATING, REC_STATUS_ANALYZING, REC_STATUS_CONVERTING,
    REC_STATUS_COMPLETED, REC_STATUS_FAILED, REC_STATUS_ABORTED, WINDOW_OPEN_STATUSES,
    FAILURE_ALL_SEGMENTS_PLACEHOLDER,
    CANCEL_DURING_ANALYSIS, CANCEL_DURING_CONVERSION,
)
from ..channel_groups import (pick_best_member, recording_members,
                              format_eligible_members, recording_formats,
                              format_label, effective_score, DEFAULT_FAILING_STREAK_THRESHOLD)
from ..health_score import dismiss_recording_failing_alerts, evaluate_and_alert_recording
from ..metadata_sidecar import globally_enabled
from ..recording_metadata import (
    EDITABLE_FIELDS, MAX_LENGTHS, UNSET, apply_user_edit, validate_edit,
)
from ..db_utils import retry_on_locked
from ..scheduler import schedule_recording, unschedule_recording
from ..recorder import (
    abort_recording, get_state, stop_recording, pause_recording, resume_recording,
    get_live_segment_path, recording_disk_paths, recording_image_paths, delete_files, end_capture_wait,
    finished_image_dirs, finished_image_path, MoveRequest, switch_choices, block_move_preview,
)
from ..tz_utils import parse_local_to_utc, local_input_value, format_local
from ..account_blocks import recording_blocks, set_recording_blocks, rearm_waiting_starts
from ..accounts import normalize_url, normalize_url_loose
from ..config import load_config, resolve_ffmpeg_path
from ..logo_cache import resolve_logo_url
from ..screenshot import capture_screenshot
from ..storage_dirs import THUMBNAILS, image_dir
from ..url_utils import mask_creds as _mask_creds_str
from .. import fmt_utils, recording_story

recordings_bp = Blueprint('recordings', __name__)
log = logging.getLogger(__name__)

# Sentinel for _apply_edit_and_reschedule: "leave profile_id untouched" (the
# form-based edit page has no profile field; None is a real value meaning
# "no profile", so it can't double as the sentinel).
_UNSET = object()


def _parse_recording_form(form):
    """Shared validation for the recording create/edit form fields.

    Returns (name, url, start_utc, stop_utc, errors); callers differ only in
    how they report a non-empty errors list (flash vs. JSON envelope).
    """
    name = form.get('name', '').strip()
    url = form.get('url', '').strip()
    start_str = form.get('start_time', '').strip()
    stop_str = form.get('stop_time', '').strip()

    errors = []
    if not name:
        errors.append('Name is required.')
    if not url:
        errors.append('IPTV URL is required.')

    start_utc = stop_utc = None
    try:
        start_utc = parse_local_to_utc(start_str)
    except ValueError:
        errors.append('Invalid start time.')

    try:
        stop_utc = parse_local_to_utc(stop_str)
    except ValueError:
        errors.append('Invalid stop time.')

    if start_utc and stop_utc and stop_utc <= start_utc:
        errors.append('Stop time must be after start time.')

    return name, url, start_utc, stop_utc, errors


def _apply_edit_and_reschedule(recording_id, name, url, start_utc, stop_utc,
                               profile_id=_UNSET, channel_id=_UNSET, member_reason=None):
    """Persist an edit to a SCHEDULED recording, then re-register its jobs.

    Only the mutate+commit is retried; the scheduler side effects must run
    exactly once, after the write has durably succeeded.

    `channel_id` (with `member_reason`, why the old member was passed over) restamps a
    group recording onto a different member, pointing `url` at that member's feed the
    way record start does. It is written only while the row is still SCHEDULED: once
    record start has chosen a member, that choice is the one ffmpeg is capturing, and
    an edit decided against the stale row must not overwrite it.

    The scheduled_* pair is re-baselined here, not preserved. It is an audit trail
    of *execution* drift (started late / stopped early / aborted), and the only
    caller is SCHEDULED-only, so nothing has run yet and there is no drift to
    record - an edit is a new plan, not a deviation from the old one. The
    RECORDING_EDITED event below is what preserves the previous times
    (dev/changelog/471). Auto-record retrack is the one other writer that
    re-baselines them, on the same reasoning (app/auto_rules.py::_move_recording,
    dev/changelog/1188).
    """
    @retry_on_locked()
    def _apply_edit_and_commit():
        r = db.session.get(Recording, recording_id)
        if profile_id is not _UNSET:
            r.profile_id = profile_id
        old_start, old_stop = r.start_time, r.stop_time
        r.name = name
        r.url = url
        r.start_time = start_utc
        r.stop_time = stop_utc
        r.scheduled_start_time = start_utc
        r.scheduled_stop_time = stop_utc
        if old_start != start_utc or old_stop != stop_utc:
            db.session.add(RecordingEvent(
                recording_id=r.id,
                event_type=RECORDING_EDITED,
                detail=f'Start/stop time edited: {old_start} → {start_utc}, {old_stop} → {stop_utc}',
            ))
        if (channel_id is not _UNSET and channel_id != r.channel_id
                and r.status == REC_STATUS_SCHEDULED):
            new_ch = db.session.get(Channel, channel_id)
            if new_ch is not None:
                old_ch = r.channel
                old_name = old_ch.name if old_ch else 'no member'
                r.channel_id = new_ch.id
                r.url = normalize_url(new_ch.stream_url, new_ch.account)
                add_recording_event(
                    r.id, RECORDING_EDITED,
                    detail=(f'Group member changed for the edited window: "{old_name}" → '
                            f'"{new_ch.name}" - {member_reason}.'),
                    extra={'from_channel_id': old_ch.id if old_ch else None,
                           'to_channel_id': new_ch.id})
        db.session.commit()
        return old_start != start_utc

    start_moved = _apply_edit_and_commit()
    _rearm_replanned(recording_id, start_utc, stop_utc, start_moved)


def _rearm_replanned(recording_id, start_utc, stop_utc, start_moved):
    """The tail of every re-plan of a SCHEDULED recording, after its commit - a user edit
    above, and auto-record retrack moving a recording with its program
    (app/auto_rules.py::_rearm_moved). The old jobs come off before the new ones go on, so
    a re-arm that fails part way never leaves a start armed at the old time."""
    unschedule_recording(recording_id)
    from flask import current_app
    schedule_recording(current_app._get_current_object(), recording_id, start_utc, stop_utc)
    if start_moved:
        # A new start time is a new plan: a hold on the old one is over, and one that
        # carried over would date the next wait from before the edit (dev/changelog/1141).
        end_capture_wait(recording_id)


def _abort_and_delete_files(recording_id):
    """Abort an active recording, then delete its on-disk files (segments + live
    thumbnail; no concatenated output exists yet). Paths are collected before the
    abort so the segment rows are still queryable."""
    from flask import current_app
    paths = recording_disk_paths(recording_id)
    abort_recording(current_app._get_current_object(), recording_id)
    # abort_recording commits ABORTED inside its own nested app-context/session; expire
    # the outer session so the Recording row (loaded above by recording_disk_paths) is
    # re-read fresh rather than served stale from this session's identity map.
    db.session.expire_all()
    delete_files(paths)


def _unschedule_and_delete_row(recording_id):
    """Permanently remove a Recording row: cancel any pending APScheduler job, unlink
    everything still naming the row, then delete it. Shared by the full-delete route and
    by cancelling a SCHEDULED recording (which has captured nothing, so there is no
    ABORTED history worth keeping instead - dev/changelog/814).

    detach_recording_references subsumes the failing-health dismiss the other callers
    still make on their own: those alerts carry the recording_id too, and here they have
    to come off the row inside the delete's own commit rather than in one before it."""
    unschedule_recording(recording_id)

    @retry_on_locked()
    def _delete_row():
        r = db.session.get(Recording, recording_id)
        detach_recording_references(recording_id)
        if r is not None:
            db.session.delete(r)
        db.session.commit()

    _delete_row()


def _wants_file_deletion(payload) -> bool:
    """Whether a delete request also wants the on-disk files removed.

    Deleting the files is the default, so an omitted key keeps the behavior every existing
    caller (the guide's unschedule paths, any external POST) already relies on. Only an
    explicit opt-out keeps them: accepts the JSON booleans and the strings an HTML form can
    send, since the same flag arrives as `false` on `/delete-json` and as an absent-or-`on`
    checkbox on the form-POST `/delete` (dev/changelog/587).
    """
    raw = payload.get('delete_files', True)
    if isinstance(raw, bool):
        return raw
    return str(raw).strip().lower() not in ('0', 'false', 'no', 'off', '')


def _log_recording_deleted(recording_id, name, remove_files, removed):
    """One log line per delete naming what happened to the files. The Recording row and its
    events are gone by now, so this log line is the only remaining record that the files
    were deliberately kept - without it, a file left on disk is indistinguishable from a
    failed unlink."""
    if remove_files:
        log.info('Recording "%s" (#%d) deleted; %d file(s) removed from disk',
                 name, recording_id, removed)
    else:
        log.info('Recording "%s" (#%d) deleted; files kept on disk by request',
                 name, recording_id)


def _match_channel_by_url(url: str):
    """Return a Channel whose stream URL matches url, or None."""
    if not url:
        return None
    # 1. Exact match on stream_url (normalized) or raw_stream_url
    ch = Channel.query.filter(
        (Channel.stream_url == url) | (Channel.raw_stream_url == url)
    ).first()
    if ch:
        return ch
    # 2. Loose match: strip /live/ and extensions from both sides. Bare columns, not Channel
    # rows - the comparison needs two strings per channel, and hydrating 137k ORM objects for
    # it is the shape _upsert_channels was measured paying 2.40s for (dev/changelog/673), on
    # a box with no swap.
    norm = normalize_url_loose(url)
    rows = db.session.query(Channel.id, Channel.stream_url, Channel.raw_stream_url)
    for channel_id, stream_url, raw_stream_url in rows.yield_per(5000):
        if (normalize_url_loose(stream_url or '') == norm
                or normalize_url_loose(raw_stream_url or '') == norm):
            return db.session.get(Channel, channel_id)
    return None


def _check_account_connection_limit(account_id, new_channel_id, start_utc, stop_utc,
                                     exclude_recording_id=None, pending=None):
    """Return a dict {'message': str, 'conflicts': [...]} if scheduling new_channel_id
    from start_utc to stop_utc would push this account's concurrent distinct-channel
    commitments above its connection limit; None if OK.

    Each entry in 'conflicts' describes one overlapping recording (id, channel_name,
    title, start_time/stop_time already formatted for display) so the caller can render
    a clickable link to it.

    Counts overlapping SCHEDULED/IN_PROGRESS/PAUSED recordings on OTHER channels of
    the same account as distinct commitments. Same-channel overlap is intentionally
    excluded here - that's a handoff (old recording gracefully stopped when the new
    one starts), not a conflict. Recordings with channel_id=None (ad-hoc URL) can't
    be attributed to an account and are invisible to this check - an accepted gap.

    `pending` is recordings a bulk schedule is about to create alongside this one
    (_pending_overlapping()); they count exactly as a stored one would, or eight showings
    picked at once on a one-connection account would each pass alone (dev/changelog/1157).

    A non-None result used to be a hard 400 refusal; since dev/changelog/854 made the
    connection limit a hard ceiling enforced at record-start time, the schedule-time
    check has nothing left to protect by refusing, so callers now fold this into a
    soft, proceedable warning instead (dev/changelog/858;
    _pending_recording_warnings() below).
    """
    account = db.session.get(Account, account_id)
    if account is None:
        return None
    limit = account.max_connections
    if not limit:
        limit = load_config().get('accounts', {}).get('default_max_connections', 1)

    q = (
        Recording.query
        .join(Channel, Recording.channel_id == Channel.id)
        .filter(
            Channel.account_id == account_id,
            Recording.status.in_([REC_STATUS_SCHEDULED, REC_STATUS_IN_PROGRESS, REC_STATUS_PAUSED]),
            Recording.start_time < stop_utc,
            Recording.stop_time > start_utc,
        )
    )
    if exclude_recording_id is not None:
        q = q.filter(Recording.id != exclude_recording_id)

    overlapping = q.all()
    other_channel_recs = [r for r in overlapping if r.channel_id != new_channel_id]
    other_pending = [p for p in _pending_overlapping(pending, start_utc, stop_utc)
                     if p['account_id'] == account_id and p['channel_id'] != new_channel_id]
    distinct_channels = {r.channel_id for r in other_channel_recs}
    distinct_channels |= {p['channel_id'] for p in other_pending}
    distinct_channels.add(new_channel_id)

    if len(distinct_channels) > limit:
        conflicts = [
            {
                'recording_id': r.id,
                'channel_name': r.channel.name if r.channel else 'Unknown channel',
                'title': r.name,
                'start_time': local_time_filter(r.start_time),
                'stop_time': local_time_filter(r.stop_time),
            }
            for r in sorted(
                other_channel_recs,
                key=lambda r: (r.channel.name if r.channel else '', r.start_time),
            )
        ] + [_serialize_pending(p) for p in other_pending]
        names = sorted({c['channel_name'] for c in conflicts})
        return {
            'message': (f'This would exceed "{account.name}"\'s connection limit ({limit}). '
                        f'Overlaps with recordings on: {", ".join(names)}.'),
            'conflicts': conflicts,
        }
    return None


def _overlap_conflicts(channel_id, group_id, start_utc, stop_utc, exclude_recording_id=None):
    """Every SCHEDULED/IN_PROGRESS/PAUSED recording whose window overlaps
    [start_utc, stop_utc), across every account - the general schedule-time overlap
    warning (dev/changelog/858). Unlike _check_account_connection_limit, this is not
    scoped to one account: two overlapping recordings on different accounts are
    exactly the case that used to pass in complete silence.

    Same-channel and same-group overlap is excluded: that is a graceful handoff (the
    earlier recording gracefully stops when the new one starts, or the group's serving
    member changes), not a conflict, and warning about it as one would misrepresent
    normal behavior. A recording with no channel of its own (an ad-hoc URL, or a group
    row that failed to resolve to any member) is never excluded by that rule and always
    counts as a conflict if its window overlaps.
    """
    q = Recording.query.filter(
        Recording.status.in_([REC_STATUS_SCHEDULED, REC_STATUS_IN_PROGRESS, REC_STATUS_PAUSED]),
        Recording.start_time < stop_utc,
        Recording.stop_time > start_utc,
    )
    if exclude_recording_id is not None:
        q = q.filter(Recording.id != exclude_recording_id)
    if channel_id is not None:
        q = q.filter(db.or_(Recording.channel_id.is_(None), Recording.channel_id != channel_id))
    if group_id is not None:
        q = q.filter(db.or_(Recording.group_id.is_(None), Recording.group_id != group_id))
    return q.all()


def _pending_overlapping(pending, start_utc, stop_utc):
    """The not-yet-created recordings of a bulk schedule whose window overlaps this one.

    Each is a dict of plain values ('channel_id', 'group_id', 'account_id', 'start',
    'stop', 'name', 'channel_name') - never a Recording, since none exists yet."""
    return [p for p in (pending or []) if p['start'] < stop_utc and p['stop'] > start_utc]


def _serialize_pending(p):
    """A pending bulk-schedule recording in the shape _serialize_overlap_conflicts() gives a
    stored one, with no recording id to link to and `in_batch` saying why."""
    return {
        'recording_id': None,
        'in_batch': True,
        'channel_name': p['channel_name'],
        'title': p['name'],
        'start_time': local_time_filter(p['start']),
        'stop_time': local_time_filter(p['stop']),
    }


def _serialize_overlap_conflicts(conflicts):
    def label(r):
        if r.channel:
            return r.channel.name
        if r.group:
            return r.group.name
        return 'Manual URL'

    return [
        {
            'recording_id': r.id,
            'channel_name': label(r),
            'title': r.name,
            'start_time': local_time_filter(r.start_time),
            'stop_time': local_time_filter(r.stop_time),
        }
        for r in sorted(conflicts, key=lambda r: (r.start_time, r.id))
    ]


def _pending_recording_warnings(channel_id, group_id, start_utc, stop_utc,
                                exclude_recording_id=None, pending_blocks=None,
                                pending=None):
    """Every soft, proceedable schedule-time warning new_recording_json and
    edit_recording_json raise before committing a create/edit, in one place - the
    recording-scheduling counterpart to routes/channel_groups.py::_pending_warnings().
    Empty (proceed) when there is nothing to warn about; the caller skips calling this
    at all once the user has pressed Proceed anyway (`force`).

    Two independent warnings, both keyed so a caller can render either or both:
      - 'overlap_warning': any other overlapping recording, any account - see
        _overlap_conflicts() above.
      - 'connection_limit_warning': the narrower, same-account provider-connection-limit
        conflict _check_account_connection_limit() computes. Kept distinct rather than
        merged into the general list because it names a specific, different consequence
        (the provider connection ceiling) rather than just "something else is running
        then too" - a recording that trips it is very likely also present in
        'overlap_warning', and that is fine, the two answer different questions.

    `pending` is the rest of a bulk schedule (_pending_overlapping()), counted in both
    warnings under the same same-channel / same-group handoff rule as a stored recording.
    """
    if channel_id is None:
        return {}
    warnings = {}

    overlaps = _overlap_conflicts(channel_id, group_id, start_utc, stop_utc, exclude_recording_id)
    batch = [p for p in _pending_overlapping(pending, start_utc, stop_utc)
             if p['channel_id'] != channel_id
             and (group_id is None or p['group_id'] != group_id)]
    if overlaps or batch:
        n = len(overlaps) + len(batch)
        warnings['overlap_warning'] = {
            'message': f'Overlaps {n} other scheduled recording{"s" if n != 1 else ""}.',
            'conflicts': (_serialize_overlap_conflicts(overlaps)
                          + [_serialize_pending(p) for p in batch]),
        }

    ch = db.session.get(Channel, channel_id)
    if ch is not None:
        limit_conflict = _check_account_connection_limit(
            ch.account_id, channel_id, start_utc, stop_utc,
            exclude_recording_id=exclude_recording_id, pending=pending)
        if limit_conflict:
            warnings['connection_limit_warning'] = limit_conflict
        block_warning = _account_block_warning(ch, group_id, start_utc, stop_utc,
                                               exclude_recording_id, pending_blocks)
        if block_warning:
            warnings['account_block_warning'] = block_warning

    return warnings


def _account_block_warning(ch, group_id, start_utc, stop_utc, exclude_recording_id,
                           pending_blocks):
    """The account this recording would record from is blocked for some of its window
    (app/account_blocks.py). Asked only when there is nowhere else to go - a single channel,
    or a group whose every recording member is blocked then - since a group with an unblocked
    member simply records from that one. Counts the blocks this very request is setting, so
    "block the account I am recording from" is caught before it is saved."""
    from ..account_blocks import blocked_account_ids, blocks_overlapping
    if group_id is not None:
        group = db.session.get(ChannelGroup, group_id)
        accounts = {m.account_id for m in recording_members(group.memberships)} if group else set()
    else:
        accounts = {ch.account_id}
    accounts.discard(None)
    if not accounts:
        return None
    blocked = blocked_account_ids(accounts, start=start_utc, stop=stop_utc,
                                  pending=pending_blocks,
                                  exclude_recording_id=exclude_recording_id)
    if blocked != accounts:
        return None
    windows = blocks_overlapping(start_utc, stop_utc, accounts, exclude_recording_id)
    names = sorted(a.name for a in Account.query.filter(Account.id.in_(accounts)).all())
    whole = (bool(pending_blocks) and set(pending_blocks) >= accounts) or any(
        b.start <= start_utc and b.stop >= stop_utc for b in windows)
    subject = ('this channel\'s account' if group_id is None
               else 'every account this group can record from')
    return {'message': (
        f'{subject[0].upper()}{subject[1:]} ({", ".join(names)}) is blocked for '
        + ('all' if whole else 'part') + ' of this window. The recording waits for the '
        'block to end and records what is left'
        + (', so as things stand it will not record anything.' if whole else '.'))}


def _parse_block_form(form):
    """The record modal's account-block picker, as {account_id: slots} (None = every slot),
    or None when the picker never loaded - a failed lookup must leave a recording's blocks
    alone rather than read as "block nothing"."""
    if form.get('block_accounts_loaded') != '1':
        return None
    wanted = {}
    for raw in form.getlist('block_account_ids'):
        raw = raw.strip()
        if not raw.isdigit():
            continue
        slots_raw = (form.get(f'block_slots_{raw}') or '').strip()
        wanted[int(raw)] = int(slots_raw) if slots_raw.isdigit() and int(slots_raw) > 0 else None
    return wanted


def _members_committed_in_window(members, start_utc, stop_utc, exclude_recording_id=None,
                                 pending=None):
    """Of `members`, the channel ids _check_account_connection_limit() would refuse.

    The schedule-time counterpart to recorder._busy_account_channel_ids(). Live slot
    occupancy answers "can this start right now"; a recording scheduled for tomorrow
    evening needs the same question asked of *its own window*, which is the overlap the
    limit check above already computes. Same rules, so the two cannot disagree: only
    SCHEDULED/IN_PROGRESS/PAUSED recordings count, distinct channels are the unit, and a
    member's own channel is excluded because same-channel overlap is a handoff.

    Batched over every member account in one query - the limit check runs per candidate
    and would otherwise re-read the config and re-query per member
    (CLAUDE.md, no hidden I/O in per-row loops).
    """
    account_ids = {ch.account_id for ch in members if ch.account_id is not None}
    if not account_ids:
        return set()
    default_max = load_config().get('accounts', {}).get('default_max_connections', 1)
    limits = {a.id: (a.max_connections or default_max)
              for a in Account.query.filter(Account.id.in_(account_ids)).all()}

    q = (db.session.query(Channel.account_id, Recording.channel_id)
         .join(Channel, Recording.channel_id == Channel.id)
         .filter(Channel.account_id.in_(account_ids),
                 Recording.status.in_([REC_STATUS_SCHEDULED, REC_STATUS_IN_PROGRESS,
                                       REC_STATUS_PAUSED]),
                 Recording.start_time < stop_utc,
                 Recording.stop_time > start_utc))
    if exclude_recording_id is not None:
        q = q.filter(Recording.id != exclude_recording_id)
    committed_channels = {}
    for account_id, channel_id in q.all():
        committed_channels.setdefault(account_id, set()).add(channel_id)
    for p in _pending_overlapping(pending, start_utc, stop_utc):
        committed_channels.setdefault(p['account_id'], set()).add(p['channel_id'])

    blocked = set()
    for ch in members:
        limit = limits.get(ch.account_id)
        if limit is None:
            continue
        others = committed_channels.get(ch.account_id, set()) - {ch.id}
        if len(others) >= limit:
            blocked.add(ch.id)
    return blocked


def _resolve_group_member(group, channel_id, start_utc, stop_utc, streak_threshold,
                          exclude_recording_id=None, pending_blocks=None, pending=None):
    """The member a group-backed recording should be stamped with for [start_utc, stop_utc).

    Returns (channel_id, reason). The supplied `channel_id` is kept (reason None) unless it
    is genuinely unusable in this window - not a recording-enabled member, outside the
    format lock, or on an account already committed then - so a schedule that would have
    gone through is never quietly moved to a different feed. Otherwise the best member on
    an uncommitted account wins, and `reason` says why the supplied one was passed over.
    Every account committed is an override, never a skip: the supplied member is kept if
    it is one, else the plain best member is taken. (None, None) only when the group has
    no member to stamp and nothing usable was supplied.

    Shared by create and edit so the two cannot disagree (dev/changelog/855, 1025).
    """
    from .channel_tests import _latest_tests_by_channel
    from ..account_blocks import blocked_account_ids
    enabled = recording_members(group.memberships)
    latest_by_channel = _latest_tests_by_channel([ch.id for ch in enabled])
    # Format lock filters, health score ranks (DESIGN-channel-groups-model.md 5).
    active = format_eligible_members(group, enabled, latest_by_channel).members
    committed = _members_committed_in_window(
        active, start_utc, stop_utc, exclude_recording_id=exclude_recording_id,
        pending=pending)
    # A member on an account blocked in this window is as unusable as one on a full
    # account - including a block this same request is setting (dev/changelog/1151). Only
    # the stamp: record start re-decides against the blocks as they stand then.
    blocked = blocked_account_ids({ch.account_id for ch in active}, start=start_utc,
                                  stop=stop_utc, pending=pending_blocks,
                                  exclude_recording_id=exclude_recording_id)
    committed |= {ch.id for ch in active if ch.account_id in blocked}

    supplied_is_member = (channel_id is not None
                          and channel_id in {m.channel_id for m in group.memberships})
    active_ids = {ch.id for ch in active}
    if supplied_is_member and channel_id in active_ids and channel_id not in committed:
        return channel_id, None

    if not supplied_is_member:
        reason = 'it is no longer a member of this group'
    elif channel_id not in {ch.id for ch in enabled}:
        reason = 'it is no longer enabled for recording in this group'
    elif channel_id not in active_ids:
        reason = "it does not match the group's format lock"
    elif _account_of(active, channel_id) in blocked:
        reason = 'its account is blocked in this window'
    else:
        reason = 'its account has no free connection in this window'

    member = pick_best_member(active, latest_by_channel, exclude_ids=committed,
                              streak_threshold=streak_threshold)
    if member is None and supplied_is_member:
        return channel_id, None
    if member is None:
        # Nothing on an uncommitted account and nothing supplied to fall back on: stamp
        # the plain best member and let the limit check speak to it.
        member = pick_best_member(active, latest_by_channel, streak_threshold=streak_threshold)
    if member is None:
        return None, None
    return member.id, reason


def _account_of(members, channel_id):
    return next((ch.account_id for ch in members if ch.id == channel_id), None)


# ── Recordings list row model (design-system reference page 1) ──────────────

_AUDIO_CODEC_LABELS = {
    'aac': 'AAC', 'ac3': 'AC-3', 'eac3': 'E-AC-3', 'mp2': 'MP2', 'mp3': 'MP3',
    'dts': 'DTS', 'opus': 'Opus', 'flac': 'FLAC', 'pcm_s16le': 'PCM',
}
_AUDIO_CH_LABELS = {1: '1.0', 2: '2.0', 6: '5.1', 8: '7.1'}


def _fmt_video(resolution, fps):
    """'1920x1080' + 59.94 -> '1080p60'; None-safe."""
    if not resolution or 'x' not in str(resolution):
        return None
    height = str(resolution).split('x')[-1]
    if fps:
        return f'{height}p{round(fps)}'
    return f'{height}p'


def _fmt_audio(codec, channels):
    if not codec:
        return None
    label = _AUDIO_CODEC_LABELS.get(str(codec).lower(), str(codec).upper())
    if channels:
        label += ' ' + _AUDIO_CH_LABELS.get(channels, f'{channels}ch')
    return label


def _tech_parts(rec):
    """(video_str, audio_str, source) for a recording's stream tech info.

    Source order per DESIGN.md section 5: capture-time segment probe first
    (describes the ORIGINAL capture); recording-level recorded_* values as the
    explicit fallback for pre-probe rows (those describe the final/converted
    file - the 'output' source label keeps that honest). (None, None, None)
    when nothing is known."""
    # An excluded segment was never joined into the file, and a discarded placeholder has
    # its own resolution and frame rate (dev/changelog/957).
    for s in reversed(rec.segments):
        if s.excluded:
            continue
        if s.probe_resolution or s.probe_audio_codec:
            return (_fmt_video(s.probe_resolution, s.probe_fps),
                    _fmt_audio(s.probe_audio_codec, s.probe_audio_channels),
                    'capture')
    if rec.recorded_resolution or rec.recorded_audio_codec:
        return (_fmt_video(rec.recorded_resolution, rec.recorded_fps),
                _fmt_audio(rec.recorded_audio_codec, rec.recorded_audio_channels),
                'output')
    return (None, None, None)


# ── Stream format profile (dev/changelog/336, shared vocabulary 351) ────────
# Labels, tri-state handling and tooltip wording live in app/fmt_utils.py so this
# page, the channel detail page and the group page's drawer
# (static/js/group-detail.js) read identically for the same measurement.

_NONE = fmt_utils.PROFILE_NONE
# True since dev/changelog/1247: the recorded_* format columns are re-read from the converted
# file once a conversion finishes (postprocessor._output_format_fields), so a re-encoded
# audio track reads at the mp4's bitrate and layout, not the joined .ts's.
_OUTPUT_NOTE = 'Measured on the final output file, not the original capture.'


def _profile_row(label, value, cls=None, tip=None):
    return {'label': label, 'value': value or _NONE, 'cls': cls, 'tip': tip or None}


def _format_profile(rec):
    """{'rows', 'source', 'coded'} for the recording's stream format profile.

    Pure; reads only the already-loaded rec.segments collection, never the DB.

    Source order matches _tech_parts (capture-time segment probe first, per
    DESIGN.md section 5), resolved independently of it: a segment probed before
    the format columns existed has probe_resolution but no probe_video_codec, so
    the tech rows can legitimately describe the capture while these describe the
    output. Each labels its own source, which is what keeps that honest.

    'source' is None when nothing is known - a pre-feature recording. The caller
    renders one explanatory row in that case rather than six empty ones.

    Efficiency is always output-derived: bits-per-pixel-frame needs a reliable
    bitrate, which a still-growing segment cannot give (dev/changelog/335), so
    there is no probe_* counterpart and its tip says which file it describes.
    """
    src = None
    codec = pix_fmt = chroma = coded = None
    depth = None
    interlaced = is_vfr = None
    fps = None
    for s in reversed(rec.segments):
        if s.probe_video_codec and not s.excluded:
            src = 'capture'
            codec, pix_fmt, depth = s.probe_video_codec, s.probe_pix_fmt, s.probe_bit_depth
            chroma, coded = s.probe_chroma_subsampling, s.probe_coded_resolution
            interlaced, is_vfr, fps = s.probe_interlaced, s.probe_is_vfr, s.probe_fps
            break
    if src is None and rec.recorded_video_codec:
        src = 'output'
        codec, pix_fmt = rec.recorded_video_codec, rec.recorded_pix_fmt
        depth, chroma = rec.recorded_bit_depth, rec.recorded_chroma_subsampling
        coded, interlaced = rec.recorded_coded_resolution, rec.recorded_interlaced
        is_vfr, fps = rec.recorded_is_vfr, rec.recorded_fps

    bpp = rec.recorded_bits_per_pixel_frame
    audio_note = _audio_profile_note(rec)
    if src is None and not bpp:
        return {'rows': [], 'source': None, 'coded': None, 'audio_note': audio_note}

    # Appended to every row that came from recorded_* so a reader is never left
    # guessing whether a codec describes what the provider sent or what we wrote.
    note = ('\n' + _OUTPUT_NOTE) if src == 'output' else ''

    rows = [
        _profile_row('Codec', codec, tip=(pix_fmt and f'ffprobe pixel format: {pix_fmt}.{note}')),
        _profile_row('Bit depth', depth and f'{depth}-bit', tip=note.strip() or None),
        _profile_row('Chroma', fmt_utils.fmt_chroma(chroma), tip=fmt_utils.CHROMA_TIP + note),
    ]
    if interlaced is None:
        rows.append(_profile_row('Scan', 'Unknown', tip=fmt_utils.SCAN_UNKNOWN_TIP))
    elif interlaced:
        rows.append(_profile_row('Scan', 'Interlaced', cls='warn',
                                 tip=fmt_utils.INTERLACED_TIP + note))
    else:
        rows.append(_profile_row('Scan', 'Progressive',
                                 tip=fmt_utils.PROGRESSIVE_TIP + note))
    if is_vfr is None:
        rows.append(_profile_row('Frame rate', fps and f'{fps:.1f} fps',
                                 tip=fmt_utils.VFR_UNKNOWN_TIP))
    elif is_vfr:
        rows.append(_profile_row('Frame rate', 'Variable', cls='warn',
                                 tip=fmt_utils.VFR_TIP + note))
    else:
        rows.append(_profile_row('Frame rate', f'{fps:.1f} fps constant' if fps else 'Constant',
                                 tip=note.strip() or None))
    if bpp:
        rows.append(_profile_row(fmt_utils.EFFICIENCY_LABEL, f'{bpp:.4f}',
                                 tip=fmt_utils.EFFICIENCY_TIP + '\n' + _OUTPUT_NOTE))
    return {'rows': rows, 'source': src, 'coded': coded, 'audio_note': audio_note}


def _audio_profile_note(rec):
    """One tooltip line for the audio detail the Audio row's 'AAC 2.0' label drops.

    Always output-derived (there are no probe_* counterparts for these three), so
    it says 'Output audio' rather than silently reading as the capture's - and the
    columns do describe the output, re-read from the converted file when the conversion
    finished (dev/changelog/1247).
    """
    parts = []
    if rec.recorded_audio_sample_rate:
        parts.append(f'{rec.recorded_audio_sample_rate / 1000:g} kHz')
    if rec.recorded_audio_bitrate_kbps:
        parts.append(f'{rec.recorded_audio_bitrate_kbps:.0f} kb/s')
    if rec.recorded_audio_language:
        parts.append(rec.recorded_audio_language)
    return ('Output audio: ' + ' · '.join(parts)) if parts else None


# duplicated from tz_utils.relative - different display register: this one is loose/
# approximate ('3.5 hours', '2 weeks') for the recordings list, where many rows render at
# once and exact seconds don't matter; tz_utils.relative is precise single/double-unit
# wording for the Dashboard/Jobs "next event" countdown (dev/changelog/623)
def _humanize_secs(seconds):
    s = abs(int(seconds))
    if s < 60:
        return 'moments'
    if s < 3600:
        return f'{s // 60} min'
    if s < 86400:
        hours = s / 3600
        return f'{hours:.1f} hours'.replace('.0 ', ' ') if hours < 10 else f'{round(hours)} hours'
    days = s / 86400
    if days < 14:
        return f'{days:.1f} days'.replace('.0 ', ' ') if days < 3 else f'{round(days)} days'
    return f'{round(days / 7)} weeks'


def _size_parts(n):
    """bytes -> ('6.9', 'GB') for the num/unit split in the size cell."""
    if not n:
        return (None, None)
    units = ['B', 'KB', 'MB', 'GB', 'TB']
    v = float(n)
    i = 0
    while v >= 1024 and i < len(units) - 1:
        v /= 1024
        i += 1
    return (f'{v:.1f}' if i >= 3 else f'{v:.0f}', units[i])


# The status -> (section, edge class, badge class, label, pulse) table lives in
# app/fmt_utils.py: four surfaces render a recording's status as text and two of them used
# to print the stored enum instead of the label (dev/changelog/961).


def _join_strip_context(rec):
    """(live progress or None, the segment count the join is about) for the detail page.

    The count is the whole point of the second half of this: the strip used to read
    `rec.segments | length`, which is every row - zero-byte ones and the ones the app
    deliberately discarded included - so a join of 16 announced itself as 19. Three
    different questions, and each has exactly one answer already written:

    - joining right now: the registry's own `of`, which IS the list ffmpeg was handed, so
      the strip and the join cannot disagree even if a file disappeared since;
    - queued or stranded at CONCATENATING with no ffmpeg: concatenator.joinable_segments(),
      the one definition of joinable, re-asked because nothing has been handed to ffmpeg yet;
    - finished: postprocessor._joined_segment_count(), which counts rows - a successful
      concat deletes the segment files, so a filesystem test answers 0 for every completed
      recording.

    The file tests cost one stat per segment and run on a single recording's detail page,
    never in a per-row loop.
    """
    from ..concatenator import concat_progress, joinable_segments
    from ..postprocessor import _joined_segment_count

    if rec.status == REC_STATUS_CONCATENATING:
        prog = concat_progress(rec.id)
        if prog is not None:
            return prog, prog['of']
        return None, len(joinable_segments(rec.segments))
    if rec.status in (REC_STATUS_COMPLETED, REC_STATUS_ANALYZING, REC_STATUS_CONVERTING):
        return None, _joined_segment_count(rec.id)
    return None, len(joinable_segments(rec.segments))


def _channel_initials(name):
    letters = ''.join(c for c in (name or '') if c.isalnum())
    return (letters[:3] or '?').upper()


def _row_times(rec, now, tz):
    """(day label, relative line) for one row of the recordings list - the two cells of it
    that change with the clock alone. _index_row renders them into the page, and
    /api/recordings/times re-asks them once a minute so the open page's "in 5 min" and
    "Today" stay true without re-rendering every row (dev/changelog/1144). One function, so
    the page and the tick cannot word the same row two ways. Reads only the row's own
    columns and the in-memory join/analysis registries: no query, no disk."""
    from ..tz_utils import day_label
    from ..concatenator import concat_progress, concat_progress_parts
    from ..postprocessor import analysis_progress, analysis_progress_parts
    # A dict lookup under a lock, no I/O - safe in this per-row builder, which is what the
    # /recordings case in tests/test_scaling_pages.py exists to keep true. Only a row that
    # is actually joining can have an entry, so the lookup is skipped for every other status
    # rather than paying it once per row on a page of hundreds.
    concat_prog = (concat_progress(rec.id)
                   if rec.status == REC_STATUS_CONCATENATING else None)
    analysis_prog = (analysis_progress(rec.id)
                     if rec.status == REC_STATUS_ANALYZING else None)

    day = day_label(rec.start_time, now, tz)

    # relative line - explicit per section/status, anchored on the honest time
    if rec.status in WINDOW_OPEN_STATUSES:
        secs = (rec.stop_time - now).total_seconds()
        rel = f'ends in {_humanize_secs(secs)}' if secs > 0 else 'past stop time'
        if rec.status == REC_STATUS_PAUSED:
            rel = 'paused - ' + rel
        elif rec.status == REC_STATUS_RETRYING:
            if rec.next_retry_at and rec.next_retry_at > now:
                retry_in = _humanize_secs((rec.next_retry_at - now).total_seconds())
                rel = f'retrying in {retry_in} (attempt {rec.dead_stream_retry_count}) - ' + rel
            else:
                rel = 'retrying - ' + rel
    elif rec.postprocess_waiting_since and rec.status in (REC_STATUS_ANALYZING,
                                                          REC_STATUS_CONVERTING):
        # Parked, not working. Reported before the two phase branches below so neither can
        # describe work that has stopped - a converting row would otherwise show a frozen
        # ETA, and an analyzing one a damage scan that already finished (dev/changelog/954).
        if rec.status == REC_STATUS_CONVERTING and rec.conversion_progress_pct is not None:
            rel = (f'paused at {rec.conversion_progress_pct:.0f}% · '
                   f'waiting on "{rec.postprocess_waiting_on_name}"')
        else:
            rel = f'waiting on "{rec.postprocess_waiting_on_name}" · resumes by itself'
    elif rec.status == REC_STATUS_CONVERTING:
        parts = ['converting']
        if rec.conversion_progress_pct is not None:
            parts.append(f'{rec.conversion_progress_pct:.0f}%')
        if rec.conversion_eta_seconds is not None:
            parts.append(f'~{_humanize_secs(rec.conversion_eta_seconds)} left')
        elif rec.conversion_progress_pct is None:
            parts.append('starting')
        rel = ' · '.join(parts)
    elif rec.status == REC_STATUS_CONCATENATING and concat_prog:
        # A join that is running says how far it has got; one that is only queued behind
        # another (serialize_concat) falls through to the branch below, because "0%" and
        # "has not started" are different facts and the strip that reads 0% forever is the
        # thing this replaced.
        parts = ['joining'] + concat_progress_parts(concat_prog)
        if concat_prog['eta_seconds'] is not None:
            parts.append(f"~{_humanize_secs(concat_prog['eta_seconds'])} left")
        rel = ' · '.join(parts)
    elif rec.status == REC_STATUS_ANALYZING and analysis_prog:
        # Same split as the join above: a pass that is reading says which one and how far,
        # and an ANALYZING row with no pass running (between passes, or queued) falls
        # through to "ended Nm ago" rather than reporting a percentage nothing is moving.
        label, *rest = analysis_progress_parts(analysis_prog)
        parts = [f'checking - {label}'] + rest
        parts.append(f"{_humanize_secs(analysis_prog['elapsed_seconds'])} elapsed")
        rel = ' · '.join(parts)
    elif rec.status in (REC_STATUS_CONCATENATING, REC_STATUS_ANALYZING):
        rel = f'ended {_humanize_secs((now - rec.stop_time).total_seconds())} ago'
    elif rec.status == REC_STATUS_SCHEDULED:
        secs = (rec.start_time - now).total_seconds()
        if secs > 0:
            rel = f'in {_humanize_secs(secs)}'
        elif rec.start_deferred_since:
            # A held start names what it is waiting for, read off the row rather than the
            # RECORDING_START_DEFERRED event, which would be a query per row
            # (dev/changelog/1141). "starting" is left for the seconds a start really takes.
            rel = (f'waiting for {rec.start_deferred_for}' if rec.start_deferred_for
                   else 'waiting to start')
            if -secs >= 60:
                rel += f' · {_humanize_secs(secs)} late'
        else:
            rel = 'starting'
    else:
        anchor = rec.completed_at or rec.stop_time
        rel = f'{_humanize_secs((now - anchor).total_seconds())} ago'

    return day, rel


#: Seconds with no new byte before the live page stops calling a capture live. The page's
#: script reads it off the strip, so the strip and the screenshot's tag change their answer
#: at the same moment.
LIVE_STALE_SECONDS = 15

# The watchdog's last word on a capture that is not getting data: it is reconnecting (a
# stall, a restart under way or one that got nothing, a resume), or a restart got the stream
# back. The page's script keeps the same flag from the same events as they arrive.
_RECONNECTING_AFTER = {
    STALL_DETECTED: True, FAST_DELIVERY_DETECTED: True, RESTART_ATTEMPTED: True,
    RESTART_FAILED: True, RECORDING_RESUMED: True, RESTART_SUCCEEDED: False,
}


def _live_capture(rec, live_seg, live_seg_bytes, now):
    """The live strip's first render: its numbers, whether data is arriving, whether the
    watchdog is reconnecting, whether this recording has captured anything yet. It used to
    render "Recording" with dashes and wait for the first SSE frame, which could be half a
    minute away while a restart was waited out (dev/changelog/1285). The numbers are the
    ones watchdog._publish_snapshot sends, worked out the same way. Whether data is arriving
    comes from the open segment's size, already stat'ed, and the events the page holds:
    nothing reads a time off the file, because /dvr's mtimes come from the share's clock.

    The page's script re-derives `label` from the same three facts on every frame; the two
    must stay in step."""
    capturing = live_seg is not None and live_seg_bytes > 0
    reconnecting = False
    for e in reversed(rec.events):
        if e.event_type in _RECONNECTING_AFTER:
            reconnecting = _RECONNECTING_AFTER[e.event_type]
            break
    had_data = capturing or any(s.bytes_recorded and not s.excluded for s in rec.segments)
    if capturing:
        label = 'Recording'
    elif not had_data and not reconnecting:
        label = 'Connecting'
    else:
        label = 'Not capturing'
    return {'capturing': capturing, 'reconnecting': reconnecting and not capturing,
            'had_data': had_data, 'label': label,
            'segment_number': live_seg.segment_number if live_seg is not None else None,
            'segment_bytes': live_seg_bytes,
            'elapsed': (now - rec.started_at).total_seconds() if rec.started_at else 0,
            'remaining': max(0, (rec.stop_time - now).total_seconds()),
            'total_bytes': sum(s.bytes_recorded or 0 for s in rec.segments
                               if s.ended_at is not None and not s.excluded) + live_seg_bytes}


def _open_segment_bytes(seg):
    """The open segment's size on disk. Its bytes_recorded is written only when it ends, so
    without this a live recording's size lags by the whole open segment - the list read 42
    MB beside its own page's 76.8 MB (dev/docs/BUGS.md 2026-10-22 08:13:56 PM). One stat per live
    recording, so a page pays for the recordings under way, never for its row count."""
    if seg is None or not seg.file_path:
        return 0
    try:
        return os.path.getsize(seg.file_path)
    except OSError:  # ffmpeg has not created the file yet
        return 0


def _dur_size_cells(rec, now, live_seg_bytes=0):
    """A row's duration and size cells - the two that grow while a recording runs, so the
    list's minute tick re-asks them for its live rows (recording_times) from this same
    function the page renders them with."""
    sched_secs = rec.scheduled_duration_seconds
    # Discarded segments are out: this list stands in for the file, and both numbers it
    # feeds - captured-so-far and the size shown before a final file exists - would
    # otherwise credit the recording with content the app deliberately threw away.
    data_segs = [s for s in rec.segments if s.bytes_recorded and not s.excluded]

    # duration cell: scheduled window for SCHEDULED rows; captured-so-far while
    # live; actual (file, else segment span) once terminal - four different
    # values, each labeled by its tooltip
    dur_flag = None
    dur_tip = None
    if rec.status == REC_STATUS_SCHEDULED:
        dur_secs = sched_secs
    elif rec.status in WINDOW_OPEN_STATUSES:
        captured = sum((s.ended_at - s.started_at).total_seconds()
                       for s in data_segs if s.ended_at)
        # The open segment counts once it has written a byte. A restart still waiting for
        # its first one is not capture, and counting it made the number fall when that
        # restart closed with nothing: "Captured 53s", then 45s (dev/changelog/1285).
        active = rec.active_segment
        if active and live_seg_bytes:
            captured += (now - active.started_at).total_seconds()
        dur_secs = captured
        dur_flag = 'live'
        verb = 'Waiting to retry' if rec.status == REC_STATUS_RETRYING else 'Recording'
        dur_tip = (f'{verb} - {fmt_utils.fmt_duration(captured)} captured so far '
                   f'of {fmt_utils.fmt_duration(sched_secs)} scheduled.')
    elif (rec.status in (REC_STATUS_CONCATENATING, REC_STATUS_ANALYZING, REC_STATUS_CONVERTING)
          and rec.actual_duration_seconds is None):
        # Capture is over and the file's own length is not measured until the join has
        # probed it, so this stretch read "-" where the page had just shown a number
        # (dev/changelog/1285). The segments' span is what was captured.
        dur_secs = rec.captured_duration_seconds
        dur_tip = ('Measured from the segments that captured data. The file\'s own length '
                   'replaces it once the segments are joined.')
    else:
        dur_secs = rec.actual_duration_seconds
        # The user's own Stop gets a quiet mark of its own, not the amber partial one
        # (dev/changelog/1287).
        stopped = recording_story.stopped_early_note(rec)
        if stopped:
            dur_flag = 'stopped'
            dur_tip = stopped
        elif recording_story.short_capture(rec):
            dur_flag = 'partial'
            dur_tip = (f'Partial - {fmt_utils.fmt_duration(dur_secs)} captured of '
                       f'{fmt_utils.fmt_duration(sched_secs)} scheduled.')

    # size: final file once it exists, else data written to segments so far plus the open
    # segment's size, which the caller stat'ed (_open_segment_bytes).
    size_bytes = (rec.final_file_size
                  or sum(s.bytes_recorded or 0 for s in data_segs) + live_seg_bytes or None)
    # The rate's basis is whichever duration the cell above settled on, and the tooltip
    # names it: every other duration-derived stat on the page says which of the four it
    # divides by, and this one did not (dev/changelog/1247).
    rate = None
    rate_tip = None
    if size_bytes and dur_secs and dur_secs > 60:
        rate = f'{size_bytes * 8 / dur_secs / 1e6:.1f} Mb/s'
        if rec.status in WINDOW_OPEN_STATUSES:
            rate_tip = ('Bytes captured so far over the wall clock the capture has run for. '
                        'A live average - a stall lowers it, a replayed buffer raises it.')
        elif rec.actual_duration_source == 'file':
            rate_tip = ('File size over the content length ffprobe measured in the finished '
                        'file, not over the recording window.')
        else:
            rate_tip = ('File size over the span of the segments that captured data - the '
                        'finished file was not probed for its content length.')
    elif rec.status in WINDOW_OPEN_STATUSES:
        rate_tip = 'Shown once a minute has been captured.'
    size_num, size_unit = _size_parts(size_bytes)
    return {'dur_secs': dur_secs, 'dur_str': fmt_utils.fmt_duration(dur_secs),
            'dur_flag': dur_flag, 'dur_tip': dur_tip, 'size_bytes': size_bytes,
            'size_num': size_num, 'size_unit': size_unit, 'rate': rate, 'rate_tip': rate_tip}


def _index_row(rec, now, tz, thumb_ids, live_seg_bytes=0, file_formats=None,
               thumb_enabled=True):
    """file_formats is channel_groups.file_format_stretches() over the recording's format
    survey, when the caller has it in hand - the detail page does, from events it loaded
    anyway. Without it the format clause falls back on what each segment opened at, which
    is all a list row can read without a query per row."""
    # The WAITING derivation is inside rec_status_display, so this row and the Dashboard's
    # row and chip cannot name the same parked recording three different ways.
    section, st_class, badge_class, badge_label, pulse = fmt_utils.rec_status_display(
        rec.status, waiting=bool(rec.postprocess_waiting_since))

    day, rel = _row_times(rec, now, tz)
    cells = _dur_size_cells(rec, now, live_seg_bytes)
    dur_secs, size_bytes = cells['dur_secs'], cells['size_bytes']
    data_segs = [s for s in rec.segments if s.bytes_recorded and not s.excluded]
    stalls = rec.total_stall_count or 0

    # health pill. Three different quantities and the tooltip keeps them apart: downtime is
    # the capture-time gap counter, the gap figure is wall clock with no segment running at
    # all (measured after the fact from the segment clocks), and the content figure compares
    # the delivered length against that capture time (dev/changelog/432, 942).
    # Gated on the same 2% the duration cell's 'partial' flag uses, so the two agree: every
    # clean capture is a few seconds off its window and saying so on every row would be
    # noise. The detail page shows both figures unconditionally. The clauses are
    # recording_story's, so this tooltip and the recording page word them once
    # (dev/changelog/1270).
    missing = ''.join(f' {c}' for c in recording_story.trouble_clauses(rec))
    # Distinct channels this recording's segments actually used - free to compute, rec.segments
    # is already loaded/iterated above for data_segs/stalls. >1 means a channel-group recording
    # had to change channels at least once (same-account restart never changes channel_id).
    distinct_channels = len({s.channel_id for s in rec.segments if s.channel_id is not None})
    spanned = f' Spanned {distinct_channels} channels.' if distinct_channels > 1 else ''
    # A mixed-format file plays (dev/changelog/754), so this adds a sentence rather than moving
    # the pill - what it corrects is a tooltip calling the file a clean, uniform capture.
    # The segments' probes read only each opening, so that fallback says "opened at" and
    # never how long; the survey's stretches are the measured answer (dev/changelog/1201).
    format_change = None
    if file_formats is not None:
        if file_formats['others']:
            from ..postprocessor import describe_format_stretches
            format_change = (f'Format changed mid-recording: '
                             f'{describe_format_stretches(file_formats)}. The file plays, '
                             f'but its header describes only '
                             f'{format_label(file_formats["first"])}.')
    elif rec.format_survey_main is not None:
        # The survey's verdict, stored on the row for exactly this reader (dev/changelog/1307):
        # the same whole-file answer the page gives, without the survey's stretches, so a
        # list row and the page never disagree about whether the file changes format.
        if rec.format_survey_other_seconds:
            format_change = (f'Format changed mid-recording: mostly {rec.format_survey_main}, '
                             f'with {fmt_utils.fmt_duration(rec.format_survey_other_seconds, with_seconds=True)} '
                             f'in other formats. The file plays, but its header describes '
                             f'only {rec.format_survey_first or rec.format_survey_main}.')
    else:
        formats = recording_formats(rec.segments)
        if len(formats) > 1:
            format_change = (f'Format changed mid-recording: segments opened at '
                             f'{", then ".join(format_label(k) for k in formats)}. The file '
                             f'plays, but its header describes only {format_label(formats[0])}.')
    format_tail = f' {format_change}' if format_change else ''
    # In-process reconnects (dev/changelog/1306): a segment that dropped and reopened its
    # connection eight times is not a clean capture, whatever the stall count says.
    reconnects = rec.reconnect_count
    if reconnects:
        howmany = f'at least {reconnects}' if rec.reconnects_truncated else f'{reconnects}'
        reconnect_tail = (f' The stream dropped and ffmpeg reconnected {howmany} '
                          f'time{"" if reconnects == 1 and not rec.reconnects_truncated else "s"} '
                          f'inside a segment, without restarting the capture.')
    else:
        reconnect_tail = ''
    if not rec.segments:
        health = {'cls': 'na', 'label': '-', 'tip': None}
    elif rec.status == REC_STATUS_FAILED:
        # A failed recording is never joined, so what is on disk is its segments - and when
        # every one was discarded there is nothing of the channel there at all.
        kept = ('Segments kept on disk.' if data_segs
                else 'Nothing of the channel was captured.')
        health = {'cls': 'bad', 'label': f'⚠ {stalls + (rec.consecutive_failures or 0)}',
                  'text': f'⚠ {fmt_utils.counted(stalls, "stall")}',
                  'tip': (f'{len(rec.segments)} segments · {stalls} stalls · '
                          f'{fmt_utils.counted(rec.consecutive_failures or 0, "consecutive failed restart")}. '
                          f'{kept}{missing}{reconnect_tail}{spanned}{format_tail}')}
    elif stalls > 0:
        downtime = (f' {fmt_utils.fmt_duration(rec.total_downtime_seconds, with_seconds=True)} '
                    f'with nothing being written.') if rec.total_downtime_seconds else ''
        health = {'cls': 'warn', 'label': f'⚠ {stalls}',
                  'text': f'⚠ {fmt_utils.counted(stalls, "stall")}',
                  'tip': (f'{len(rec.segments)} segments · {stalls} stalls, recovered '
                          f'automatically.{downtime}{missing}{reconnect_tail}{spanned}{format_tail}')}
    elif rec.fast_delivery_segment_count:
        # A frozen or looping feed writes bytes at full rate and keeps ffmpeg's frame counter
        # moving, so it never stalls and never restarts - which meant this row read a green
        # "clean capture" tick on the worst recording in the set. The pill carries no number
        # because the number beside a ⚠ everywhere else on this page is a stall count, and
        # this state has none (dev/changelog/966).
        health = {'cls': 'warn', 'label': '⚠', 'text': '⚠ not clean',
                  'tip': (f'{len(rec.segments)} segment'
                          f'{"s" if len(rec.segments) != 1 else ""} · 0 stalls, but the '
                          f'capture was not clean.{missing}{reconnect_tail}{spanned}{format_tail}')}
    else:
        # A reconnect is ffmpeg's own repair of a dropped feed, kept inside the segment, so
        # the pill stays green like a mixed-format file's does - but the words "clean
        # capture" are not true of it (dev/changelog/1306).
        clean = '' if (format_change or reconnects) else ' - clean capture'
        health = {'cls': 'ok', 'label': '✓', 'text': '✓ no stalls',
                  'tip': (f'{len(rec.segments)} segment{"s" if len(rec.segments) != 1 else ""} '
                          f'· 0 stalls{clean}.{missing}{reconnect_tail}{spanned}{format_tail}')}

    video, audio, tech_source = _tech_parts(rec)
    tech_line = ' · '.join(p for p in (video, audio) if p) or None

    # edit-modal payload (SCHEDULED only) - feeds guide.js's openModal() so "Edit schedule"
    # opens the same modal the TV Guide uses, instead of the old standalone edit page.
    # edit_recording_json only reads name/url/start_time/stop_time/profile_id from the
    # posted form, so channel_id/group_id here only need to be roughly right.
    edit = None
    if rec.status == REC_STATUS_SCHEDULED:
        edit = {
            'url': rec.url,
            'start_iso': rec.start_time.isoformat(),
            'stop_iso': rec.stop_time.isoformat(),
            'channel_id': rec.channel_id,
            'group_id': rec.group_id,
            'profile_id': rec.profile_id,
        }

    # channel/group pill (channel/group relationships are lazy='joined')
    pill = None
    member_pill = None
    group = rec.group
    if group is not None:
        # A group recording's channel_id is stamped to its serving member at creation
        # (new_recording_json) and re-stamped at record start / failover (recorder.py,
        # watchdog.py) - never left unset for a real group recording - so rec.channel is
        # the same "who is actually serving this" answer the TV Guide's group row shows.
        # It gets a chip of its own beside the group's (dev/changelog/1252): the group
        # chip carries the group's initials and the member chip carries the member's logo
        # and account, so neither chip wears the other's identity. The row's logo fallback
        # reads the member chip for the same reason.
        pill = {'label': group.name,
                'url': url_for('channel_groups.group_detail', group_id=group.id),
                'logo_url': None,
                'initials': _channel_initials(group.name),
                'acct_color': None}
        if rec.channel is not None:
            if rec.status == REC_STATUS_SCHEDULED:
                member_tip = f'The member of "{group.name}" this recording is set to start on.'
            elif rec.status in WINDOW_OPEN_STATUSES:
                member_tip = (f'The member of "{group.name}" this recording is capturing from '
                              f'right now - it changes when the recording fails over.')
            else:
                member_tip = f'The member of "{group.name}" this recording ended on.'
            member_pill = {
                'label': rec.channel.name,
                'url': url_for('channels.channel_detail', channel_id=rec.channel.id),
                'logo_url': resolve_logo_url(rec.channel),
                'initials': _channel_initials(rec.channel.name),
                'acct_color': rec.channel.account.color if rec.channel.account else None,
                'tip': member_tip}
    elif rec.channel is not None:
        pill = {'label': rec.channel.name,
                'url': url_for('channels.channel_detail', channel_id=rec.channel.id),
                'logo_url': resolve_logo_url(rec.channel),
                'initials': _channel_initials(rec.channel.name),
                'acct_color': rec.channel.account.color if rec.channel.account else None}

    acct = rec.channel.account if rec.channel else None
    # A SCHEDULED recording has never captured a frame, so a file at its thumbnail
    # path can only be a leftover from a deleted recording whose id SQLite reused -
    # showing it would present another recording's screenshot as this one's.
    # The rest mirrors what live_thumbnail() serves, so a row never links an image the
    # route answers 404: nothing while the feature is off, a fresh grab only while
    # IN_PROGRESS, and otherwise a persisted image (dev/docs/BUGS.md 2026-10-19 07:28 AM).
    has_thumb = thumb_enabled and (
        rec.status == REC_STATUS_IN_PROGRESS
        or (rec.status != REC_STATUS_SCHEDULED and rec.id in thumb_ids))
    progress_pct = None
    if rec.status == REC_STATUS_IN_PROGRESS:
        total = (rec.stop_time - rec.start_time).total_seconds()
        if total > 0:
            progress_pct = max(0, min(100, (now - rec.start_time).total_seconds() / total * 100))
    elif rec.status == REC_STATUS_CONVERTING and rec.conversion_progress_pct is not None:
        progress_pct = rec.conversion_progress_pct

    return {
        'id': rec.id, 'name': rec.name, 'status': rec.status,
        'section': section, 'st_class': st_class,
        'badge_class': badge_class, 'badge_label': badge_label, 'badge_pulse': pulse,
        'thumb_url': url_for('recordings.live_thumbnail', recording_id=rec.id) if has_thumb else None,
        'tech_line': tech_line, 'tech_source': tech_source,
        'pill': pill, 'member_pill': member_pill,
        'acct_name': acct.name if acct else None,
        'day': day, 'start': rec.start_time, 'stop': rec.stop_time, 'rel': rel,
        'dur_str': cells['dur_str'], 'dur_flag': cells['dur_flag'], 'dur_tip': cells['dur_tip'],
        # The recording page's precise form; the list keeps the short one (dev/changelog/1287).
        'dur_long': fmt_utils.fmt_duration(cells['dur_secs'], with_seconds=True),
        'size_num': cells['size_num'], 'size_unit': cells['size_unit'],
        'rate': cells['rate'], 'rate_tip': cells['rate_tip'],
        'health': health, 'format_change': format_change,
        'progress_pct': progress_pct,
        'output_file': os.path.basename(rec.output_path) if rec.output_path else None,
        'converted_gaps': (fmt_utils.fmt_duration(rec.converted_gap_seconds, with_seconds=True)
                           if (rec.converted_gap_seconds or 0) > 0 else None),
        'edit': edit,
        # sort keys
        'sort_start': int(rec.start_time.timestamp()),
        'sort_dur': int(dur_secs or 0), 'sort_size': size_bytes or 0,
        'sort_health': stalls,
    }


# The /api/user-prefs key holding the recordings list's saved filters. Written into the page
# with the list so the script posts back to the key the route read - one spelling
# (dev/changelog/1156).
RECORDINGS_SAVED_FILTERS_PREF = 'recordings_saved_filters'


def _saved_filters():
    """The recordings list's saved filters as stored, or [] - never an exception.

    One /api/user-prefs row holds the whole list. It is user-written JSON, so a value that
    is not a list is treated as "nothing saved" rather than allowed to 500 the page it only
    decorates; filter-bar.js skips any record inside it that is not shaped like one.
    """
    from ..database import UserPref

    pref = db.session.get(UserPref, RECORDINGS_SAVED_FILTERS_PREF)
    try:
        rows = json.loads(pref.value) if pref and pref.value else []
    except ValueError:
        log.warning('Saved recordings-list filters are not valid JSON - ignoring them.')
        return []
    return rows if isinstance(rows, list) else []


@recordings_bp.route('/recordings')
def index():
    from sqlalchemy.orm import selectinload, joinedload
    from ..tz_utils import get_display_tz
    from .dashboard import recording_signature
    # Read before the rows: a change landing between the two then makes the next poll
    # re-render once more, where the other order would leave that change unseen.
    rec_sig = recording_signature()
    recordings = (Recording.query
                  .options(selectinload(Recording.segments),
                           joinedload(Recording.channel).joinedload(Channel.account))
                  .order_by(Recording.start_time.desc()).all())

    # persisted images: one directory listing PER FOLDER, never a per-row stat. Either
    # folder alone answers the question a row asks - "is there an image to show" - because
    # finished_image_path() falls back to whichever one exists (dev/changelog/1060).
    cfg = load_config()
    thumb_enabled = cfg.get('recording', {}).get('live_thumbnail', {}).get('enabled', True)
    thumb_ids = set()
    for folder in finished_image_dirs(cfg):
        try:
            for fn in os.listdir(folder):
                stem, ext = os.path.splitext(fn)
                if ext == '.jpg' and stem.isdigit():
                    thumb_ids.add(int(stem))
        except OSError:
            pass  # image dir missing/unreadable -> those rows just show placeholders

    now = datetime.utcnow()
    tz = get_display_tz()
    rows = [_index_row(r, now, tz, thumb_ids, thumb_enabled=thumb_enabled,
                       live_seg_bytes=(_open_segment_bytes(r.active_segment)
                                       if r.status == REC_STATUS_IN_PROGRESS else 0))
            for r in recordings]
    sections = [
        ('In Progress', 'live', [r for r in rows if r['section'] == 'live']),
        ('Scheduled', 'sched', [r for r in rows if r['section'] == 'sched']),
        ('Completed', 'done', [r for r in rows if r['section'] == 'done']),
    ]
    live_now = sum(1 for r in rows if r['status'] == REC_STATUS_IN_PROGRESS)

    from ..database import UserPref, RecordingProfile
    pref = db.session.get(UserPref, 'recordings_columns')
    col_prefs = json.loads(pref.value) if pref and pref.value else None
    saved_filters = {'key': RECORDINGS_SAVED_FILTERS_PREF, 'list': _saved_filters()}
    profiles = RecordingProfile.query.order_by(RecordingProfile.name).all()

    return render_template('index.html', sections=sections, total=len(rows),
                           live_now=live_now, col_prefs=col_prefs, rec_sig=rec_sig,
                           saved_filters=saved_filters,
                           profiles=profiles)  # _record_modal.html + GUIDE_CONFIG.profiles expect this name


@recordings_bp.route('/api/recordings/times')
def recording_times():
    """Each recording's day label and relative line, for the open list page's minute tick.

    Re-rendering the whole list to move "in 5 min" to "in 4 min" costs about 1.3 ms and
    1.7 KB per recording - 2.7 s and 3.3 MB at 2,000 recordings, every minute, per open tab.
    This answers the two cells that move with the clock alone, from the same _row_times()
    the page itself renders them with, in one query and no per-row I/O - plus, for the few
    rows still recording, their duration and size cells. What a row IS
    (status, section, menu) is the full re-render's job, driven by recording_signature()
    (dev/changelog/1144)."""
    from sqlalchemy.orm import selectinload
    from ..tz_utils import get_display_tz
    now = datetime.utcnow()
    tz = get_display_tz()
    rows = {}
    for rec in Recording.query.all():
        day, rel = _row_times(rec, now, tz)
        rows[rec.id] = {'day': day, 'rel': rel}
    # A row whose window is open also has two cells that grow with the clock - captured so
    # far and size - and without this they froze at whatever the last full render read,
    # minutes behind the recording's own page. Its own query, with the segments, so the
    # finished rows never load theirs; one stat per live recording.
    for rec in (Recording.query.options(selectinload(Recording.segments))
                .filter(Recording.status.in_(WINDOW_OPEN_STATUSES)).all()):
        live_bytes = (_open_segment_bytes(rec.active_segment)
                      if rec.status == REC_STATUS_IN_PROGRESS else 0)
        cells = _dur_size_cells(rec, now, live_bytes)
        rows[rec.id]['cells'] = {k: cells[k] for k in ('dur_str', 'dur_flag', 'dur_tip',
                                                       'size_num', 'size_unit', 'rate',
                                                       'rate_tip')}
    return jsonify({'success': True, 'rows': rows})


def read_section_pref(key):
    """The stored {order, hidden} a page's Customize sections saved under `key`, or None.

    It is user-written JSON, so anything that is not the expected shape falls back to the
    page's defaults rather than being allowed to 500 a page it only arranges. util.js
    initSectionLayout() reads the answer."""
    from ..database import UserPref
    pref = db.session.get(UserPref, key)
    try:
        stored = json.loads(pref.value) if pref and pref.value else None
    except ValueError:
        log.warning('Stored section layout %r is not valid JSON - using the defaults.', key)
        return None
    return stored if isinstance(stored, dict) and isinstance(stored.get('order'), list) else None


@recordings_bp.route('/api/user-prefs/<key>', methods=['GET', 'POST'])
def user_prefs(key):
    """Server-side per-user UI config (DESIGN.md 3.11: column setup follows the
    user across browsers - never localStorage). Single-user app: one row per key."""
    from ..database import UserPref
    if not key.replace('-', '').replace('_', '').isalnum() or len(key) > 64:
        return jsonify({'error': 'invalid key'}), 400
    if request.method == 'GET':
        pref = db.session.get(UserPref, key)
        return jsonify({'success': True,
                        'value': json.loads(pref.value) if pref and pref.value else None})
    payload = request.get_json(silent=True) or {}

    @retry_on_locked()
    def _save_pref_and_commit():
        pref = db.session.get(UserPref, key)
        if pref is None:
            pref = UserPref(key=key)
            db.session.add(pref)
        pref.value = json.dumps(payload.get('value'))
        db.session.commit()

    _save_pref_and_commit()
    return jsonify({'success': True})


@recordings_bp.route('/recordings/new', methods=['GET'])
def new_recording():
    """Manual one-off scheduling now lives in a modal on the TV Guide page."""
    return redirect(url_for('guide.guide', new=1))


def _segment_stderr_tails(rec) -> dict:
    """{segment_number: ffmpeg stderr tail} for the Segments table's inline "why" disclosure.

    Read off the already-loaded rec.events collection (the event log renders it on the same
    page), so this costs no extra query and no per-row I/O - CLAUDE.md's no-hidden-I/O rule.

    segment_number is NOT unique among these events: a segment can be written on more than
    one terminal path in odd teardown orders. Last-in-id-order wins rather than an arbitrary
    row, which is the honest choice - the newest tail describes the most recent exit
    (CLAUDE.md's keyed-lookup rule). Tails are already credential-masked at write time
    (recorder.collect_segment_diagnostics); this only reads them back.
    """
    tails = {}
    for evt in rec.events:  # ordered by id
        if evt.event_type != DIAGNOSTICS or evt.segment_number is None or not evt.extra_data:
            continue
        try:
            data = json.loads(evt.extra_data)
        except (ValueError, TypeError):  # malformed blob - skip, never 500 the page
            continue
        if isinstance(data, dict) and data.get('kind') == 'capture_stderr' and data.get('stderr_tail'):
            tails[evt.segment_number] = data['stderr_tail']
    return tails


def _segment_channels(rec) -> dict:
    """{channel_id: Channel} for every channel referenced by rec.segments, batch-fetched once
    so the Segments table's Channel column costs no per-row query (CLAUDE.md no-hidden-I/O
    rule). Legacy segments predating recording_segments.channel_id are simply absent.
    Channel.account is preloaded too, so showing each segment's account (and building the
    header's distinct-accounts pills) costs no additional query either."""
    from sqlalchemy.orm import joinedload
    ids = {s.channel_id for s in rec.segments if s.channel_id is not None}
    if not ids:
        return {}
    return {c.id: c for c in Channel.query.options(joinedload(Channel.account))
            .filter(Channel.id.in_(ids)).all()}


def _distinct_accounts(rec, seg_channels: dict) -> list:
    """Accounts this recording's segments actually used, in first-seen (chronological)
    order - the order a group recording moved through them, not arbitrary DB order."""
    seen = set()
    accounts = []
    for seg in rec.segments:
        ch = seg_channels.get(seg.channel_id)
        acct = ch.account if ch else None
        if acct and acct.id not in seen:
            seen.add(acct.id)
            accounts.append(acct)
    return accounts


def _kept_ts_on_disk(rec) -> bool:
    """The recording's output is a joined .ts that is still on disk - the source a Retry
    conversion works from, and on a FAILED or ABORTED row the one file there is to download.
    A filesystem call, so never per row."""
    return bool(
        rec.output_path and rec.output_path.endswith('.ts') and os.path.exists(rec.output_path)
    )


@recordings_bp.route('/recordings/<int:recording_id>')
def recording_detail(recording_id):
    rec = db.session.get(Recording, recording_id)
    if rec is None:
        flash('Recording not found.', 'error')
        return redirect(url_for('recordings.index'))
    state = get_state(recording_id)
    is_active = state is not None
    ts_source_available = _kept_ts_on_disk(rec)
    # What a failed recording still has to work with. The two are not the same thing and a
    # FAILED page used to claim both at once: a successful concat consumes its segments, so
    # "segments kept on disk" is false for exactly the recordings that do have a recoverable
    # .ts. Stat'ed here rather than in the template, which cannot reach the filesystem.
    segment_files_on_disk = any(
        s.file_path and os.path.exists(s.file_path) and os.path.getsize(s.file_path) > 0
        for s in rec.segments
    )
    # Which rows' files are gone, said on the Segments table once capture is over: a join
    # deletes them, and a table still listing "segment_001.ts" under an event log saying the
    # .ts was deleted read as the log being wrong (dev/changelog/1287). Not asked while
    # capturing, when every file is there and the page is swapped every 15 s.
    seg_files_gone = (set() if rec.status in WINDOW_OPEN_STATUSES else
                      {s.id for s in rec.segments
                       if s.file_path and not os.path.exists(s.file_path)})
    # The one answer to "what recovers this finished-badly recording", read by the header, the
    # mobile action bar and the status strip alike. Placeholder segments are on disk but a Retry
    # join refuses them again for the same reason, so they recover nothing (dev/changelog/990).
    #
    # ABORTED gets only the conversion half, and that asymmetry is what the routes actually
    # accept rather than a judgment call: retry_convert takes a cancelled row (the two cancels
    # that keep the joined .ts exist so it can), while retry_concat refuses anything but FAILED
    # or a stranded JOINING. Offering Retry join on a cancelled row would name a control that
    # bounces the click straight back with an error (dev/changelog/1054).
    recover_act = None
    if rec.status == REC_STATUS_FAILED:
        if ts_source_available:
            recover_act = 'retry-convert'
        elif segment_files_on_disk and rec.failure_reason != FAILURE_ALL_SEGMENTS_PLACEHOLDER:
            recover_act = 'retry-concat'
    elif rec.status == REC_STATUS_ABORTED and ts_source_available:
        recover_act = 'retry-convert'
    # A finished recording whose converted file is missing video, and whose source was kept
    # because of it, can be converted again too (dev/changelog/1200). Its own flag, because
    # the retry deletes the converted file first and the confirm has to say so.
    gaps_retry = False
    if rec.status == REC_STATUS_COMPLETED and (rec.converted_gap_seconds or 0) > 0:
        from ..postprocessor import kept_source_path
        gaps_retry = kept_source_path(rec.id) is not None
    # Play and Download, decided from the row's columns alone - a finished file is touched
    # only when the user clicks one of them (app/playback.py, dev/changelog/1312).
    play_offer = _play_offer(rec, ts_source_available)
    cfg = load_config()
    thumb_cfg = cfg.get('recording', {}).get('live_thumbnail', {})

    # "Find another airing" prefill: stored program title snapshot, else a live EPG
    # lookup by (channel, program air time) for pre-snapshot recordings whose entry
    # still exists, else the recording name (template-rendered, so may carry
    # date/channel noise - the search box on the other end is editable).
    airing_query = rec.program_title
    if not airing_query and rec.channel_id and rec.program_start_time:
        entry = EPGEntry.query.filter_by(
            channel_id=rec.channel_id, start_time=rec.program_start_time).first()
        if entry:
            airing_query = entry.title
    airing_query = airing_query or rec.name

    # The link that replaced the Extended Search modal (dev/changelog/416). Built through
    # airing_search_url(), i.e. through SearchState.to_params(), so this page and the TV
    # Guide's "Search all..." spell the search's parameters exactly once between them.
    #
    # Two deliberate choices. `in=epg-title` narrows the search to program titles: the
    # question here is "where else is THIS program on", and the page's default field set
    # also matches channel names, which would answer a different one. And a SCHEDULED
    # recording carries the replace action context, so recording another showing deletes
    # this one in the same click - which is what the modal's Replace button did. The old
    # modal's guide-only default is deliberately NOT carried over: it hid airings on
    # channels not in the guide, which is the opposite of what this action is for.
    from .channels import airing_search_url
    find_airing_url = airing_search_url(
        query=airing_query, fields=('epg-title',),
        replace_rec=rec.id if rec.status == REC_STATUS_SCHEDULED else None)

    from ..database import RecordingProfile
    profiles = RecordingProfile.query.order_by(RecordingProfile.name).all()

    from ..tz_utils import get_display_tz
    now = datetime.utcnow()
    # Whichever image this recording actually shows - the poster frame or the final-frame
    # thumbnail, per the setting (dev/changelog/1060). Resolved through the one helper the
    # serving route uses, so the page cannot claim an image the route then 404s.
    # SCHEDULED excluded for the same reason as the list row's has_thumb: a file
    # at a not-yet-started recording's path belongs to a deleted, id-reused row.
    # And none at all while the feature is off: the route answers 404 then, persisted
    # image or not.
    thumb_enabled = thumb_cfg.get('enabled', True)
    shot_path = (finished_image_path(rec.id, cfg)
                 if thumb_enabled
                 and rec.status not in (REC_STATUS_SCHEDULED, REC_STATUS_IN_PROGRESS) else None)
    has_shot = thumb_enabled and (rec.status == REC_STATUS_IN_PROGRESS or shot_path is not None)
    shot_mtime = None
    if shot_path:
        shot_mtime = datetime.utcfromtimestamp(os.path.getmtime(shot_path))
    # 1-based segment display: new recordings' rows are already 1-based; legacy
    # 0-based recordings get +1 applied to every displayed number (files keep
    # their names in the path column) - DESIGN.md section 5
    seg_offset = 1 if any(s.segment_number == 0 for s in rec.segments) else 0
    seg_stderr = _segment_stderr_tails(rec)
    seg_channels = _segment_channels(rec)
    # Which rows get the "fast" badge, decided here by the same function the join uses, so
    # the badge and the event that explains it cannot disagree about what counts. Config is
    # read once for the page, never per row.
    from ..concatenator import fast_delivery_findings, segment_wall_seconds
    fast_delivery_segs = {
        f.segment_number for f in fast_delivery_findings(
            [(s.id, s.segment_number, s.content_duration_seconds, segment_wall_seconds(s))
             for s in rec.segments],
            load_config()['watchdog']['fast_delivery_surplus_seconds'])
    }
    distinct_accounts = _distinct_accounts(rec, seg_channels)
    event_channel_links = group_event_channel_links(rec.events)
    video, audio, tech_source = _tech_parts(rec)
    # The whole-file format survey, once the analysis phase has run it, is the measured
    # answer to which format the file holds and where; the segment probes behind
    # _tech_parts read only each segment's opening (dev/changelog/1201).
    from ..channel_groups import file_format_stretches
    from ..postprocessor import _recorded_survey
    file_formats = file_format_stretches(_recorded_survey(rec.id))
    video_basis = 'segment'
    if file_formats is not None and tech_source == 'capture':
        video = _fmt_video(*file_formats['main'])
        video_basis = 'survey'
    # The open segment's bytes_recorded is only written when it ends, so its row, the Size
    # stat and the banner would disagree for the whole segment. The banner's number is this
    # same file's size (watchdog._publish_snapshot); one stat for the one open segment.
    live_seg = rec.active_segment if is_active else None
    live_seg_id = live_seg.id if live_seg is not None and live_seg.file_path else None
    live_seg_bytes = _open_segment_bytes(live_seg)
    # Counted from the events the page already holds, so the Restarts row and the activity
    # log below it cannot disagree: total_restart_count counts only the restarts that got
    # the stream back, and "Restarts 0" beside six RESTART_ATTEMPTED read as a contradiction.
    restart_attempts = sum(1 for e in rec.events if e.event_type == RESTART_ATTEMPTED)
    # A "Recording not capturing" alert outlives the stream's return by the dead-stream window
    # (dev/changelog/1264), so the page that says the stream is back also says when the red
    # banner above it goes. Asked only while it can be true: one query, live pages only.
    retry_alert_clears_after = None
    if rec.status == REC_STATUS_IN_PROGRESS:
        from ..alerts import recording_retrying_alert_open
        if recording_retrying_alert_open(rec.id):
            retry_alert_clears_after = cfg['watchdog']['early_fail_window_seconds']
    story = recording_story.what_happened(rec, restart_attempts, seg_stderr,
                                          open_segment_bytes=live_seg_bytes,
                                          retry_alert_clears_after=retry_alert_clears_after)
    row = _index_row(rec, now, get_display_tz(), {rec.id} if has_shot else set(),
                     live_seg_bytes=live_seg_bytes, file_formats=file_formats,
                     thumb_enabled=thumb_enabled)
    live_capture = (_live_capture(rec, live_seg, live_seg_bytes, now)
                    if rec.status == REC_STATUS_IN_PROGRESS else None)
    concat_prog, joinable_count = _join_strip_context(rec)
    # Why the join's count is smaller than the Segments row, said beside it: "Joined 2
    # segments" on a page listing 7, five of them empty, was left for the reader to work out.
    # A NULL byte count is a segment closed on an error path that never measured its file;
    # the finished join counts only measured bytes (postprocessor._joined_segment_count), so
    # those are named as their own reason rather than folded into "no data".
    join_left_out = {
        'empty': sum(1 for s in rec.segments if not s.excluded and s.bytes_recorded == 0),
        'unmeasured': sum(1 for s in rec.segments
                          if not s.excluded and s.bytes_recorded is None and s.ended_at),
        'discarded': sum(1 for s in rec.segments if s.excluded),
    }
    # None for every status but ANALYZING, and also for an ANALYZING row whose passes are
    # done or have not started - the strip's generic wording covers that, per
    # postprocessor.analysis_progress.
    from ..postprocessor import analysis_progress
    analysis_prog = (analysis_progress(rec.id)
                     if rec.status == REC_STATUS_ANALYZING else None)
    pp_cfg = cfg['recording']['post_process']
    conversion_max_attempts = pp_cfg.get('max_restart_attempts', 3)
    # What happens to the file once the join is done, so a conversion nobody asked for on
    # this page does not arrive unannounced (one global setting; no profile overrides it).
    after_join = {
        'convert': bool(pp_cfg.get('enabled', True)),
        'format': str(pp_cfg.get('format', 'mp4')).lstrip('.').upper(),
        'delete_source': bool(pp_cfg.get('delete_source', True)),
        'settings_url': url_for('settings.settings', q='recording.post_process.enabled'),
    }
    max_dead_stream_retry_attempts = load_config()['watchdog'].get('dead_stream_max_retry_attempts', 10)
    # The auto-record rule that made this recording, if it still owns it. Deleting a rule
    # clears rule_id on every recording it keeps, so a set rule_id always names a live rule
    # (dev/changelog/1186); the RECORDING_RULE_SCHEDULED event keeps the name either way.
    made_by_rule = None
    if rec.rule_id is not None:
        from ..database import AutoRule
        rule_name = db.session.query(AutoRule.name).filter(AutoRule.id == rec.rule_id).scalar()
        if rule_name is not None:
            made_by_rule = {'name': rule_name,
                            'url': url_for('auto_rules.rule_page', rule_id=rec.rule_id)}

    page = render_template(
        'recording_detail.html', rec=rec, is_active=is_active,
        made_by_rule=made_by_rule,
        live_capture=live_capture, live_stale_seconds=LIVE_STALE_SECONDS,
        ts_source_available=ts_source_available,
        segment_files_on_disk=segment_files_on_disk, seg_files_gone=seg_files_gone,
        recover_act=recover_act,
        gaps_retry=gaps_retry,
        play_offer=play_offer,
        conversion_max_attempts=conversion_max_attempts,
        after_join=after_join,
        max_dead_stream_retry_attempts=max_dead_stream_retry_attempts,
        now=now,
        concat_prog=concat_prog, joinable_count=joinable_count, join_left_out=join_left_out,
        restart_attempts=restart_attempts, story=story,
        analysis_prog=analysis_prog,
        row=row, video=video, video_basis=video_basis, audio=audio, tech_source=tech_source,
        fmt=_format_profile(rec),
        seg_offset=seg_offset, seg_stderr=seg_stderr, seg_channels=seg_channels,
        fast_delivery_segs=fast_delivery_segs,
        live_seg_id=live_seg_id, live_seg_bytes=live_seg_bytes,
        distinct_accounts=distinct_accounts,
        event_channel_links=event_channel_links,
        shot_mtime=shot_mtime, has_shot=has_shot,
        stop_local_input=local_input_value(rec.stop_time),
        thumb_enabled=thumb_enabled,
        thumb_auto_refresh_seconds=thumb_cfg.get('auto_refresh_seconds', 60),
        find_airing_url=find_airing_url,
        meta_panel=_metadata_panel(rec, cfg),
        profiles=profiles,  # _record_modal.html + GUIDE_CONFIG.profiles expect this name
        blocked_accounts=_recording_block_labels(rec.id),
    )
    # The page keeps itself current by swapping regions in place, so the document a browser
    # cached is only what it looked like at its last full load. Back served that copy: the
    # What happened of a recording still joining, over one that had finished
    # (dev/changelog/1285). Never stored, so Back asks again.
    resp = make_response(page)
    resp.headers['Cache-Control'] = 'no-store'
    return resp


def _recording_block_labels(recording_id):
    """'"Provider A"', '"Provider B" (1 connection)' - the accounts this recording blocks,
    for its detail page. Two statements whatever the count."""
    wanted = recording_blocks(recording_id)
    if not wanted:
        return []
    names = {a.id: a.name for a in Account.query.filter(Account.id.in_(wanted)).all()}
    return [f'"{names[aid]}"' + ('' if slots is None else
                                 f' ({slots} connection{"s" if slots != 1 else ""})')
            for aid, slots in sorted(wanted.items(), key=lambda kv: names.get(kv[0], ''))
            if aid in names]


#: Where a recording is in its life, for copy that has to be honest about what a save will
#: actually do. 'before' is the only phase where the record-start refresh is still ahead of
#: the user, and 'after' is the only one where a sidecar may already be on disk.
def _metadata_phase(rec):
    if rec.status == REC_STATUS_SCHEDULED:
        return 'before'
    if rec.status in (REC_STATUS_COMPLETED, REC_STATUS_FAILED, REC_STATUS_ABORTED):
        return 'after'
    return 'during'


def _metadata_panel(rec, cfg):
    """Everything the Program card shows and the edit modal opens with.

    Rendered into the card as one `data-meta` payload rather than served by a GET endpoint,
    for two reasons. The card is in the detail page's 15-second `swapFromServer` list, so a
    payload carried ON the card stays fresh through every swap while one baked into the
    page's script block would go stale. And a new non-`/api/` GET rule would owe a
    `tests/test_scaling_pages.py` decision, for a route that is a single row by construction
    (dev/changelog/1058).

    One row, no loop, and one `stat()` for the whole page - nothing here scales with row
    count.
    """
    from ..metadata_sidecar import (
        derived_title, globally_enabled, sidecar_paths, sidecar_source,
    )

    level, effective = sidecar_source(cfg, rec.profile, rec)
    nfo_path = sidecar_paths(rec.output_path)[0] if rec.output_path else None
    return {
        'recording_id': rec.id,
        'title': rec.metadata_title,          # None means "derive it"
        'derived_title': derived_title(rec),
        'description': rec.metadata_description,
        'category': rec.metadata_category,
        'rating': rec.metadata_rating,
        'locked': bool(rec.metadata_locked),
        'sidecar_enabled': rec.metadata_sidecar_enabled,   # tri-state: None = inherit
        'sidecar_effective': effective,
        'sidecar_level': level,
        # The global switch decides whether the feature's UI exists at all; the resolved
        # answer above decides whether a file gets written. Two different questions, and
        # hiding the per-recording control whenever the resolved answer is Off would leave
        # a recording that had been switched off with no way to switch it back on.
        'sidecar_global': globally_enabled(cfg),
        'profile_name': rec.profile.name if rec.profile else None,
        'nfo_name': os.path.basename(nfo_path) if nfo_path else None,
        'nfo_exists': bool(nfo_path and os.path.exists(nfo_path)),
        'phase': _metadata_phase(rec),
        'limits': dict(MAX_LENGTHS),
    }


@recordings_bp.route('/recordings/<int:recording_id>/metadata-json', methods=['POST'])
def save_recording_metadata(recording_id):
    """Save what this recording says about itself, and rewrite its sidecar if it has one.

    POST only. The read half is rendered into the page (see `_metadata_panel`), so there is
    no GET rule here to keep in step with it.

    Three things happen in a fixed order, and the order is the point. The columns move and
    their event is written inside ONE `retry_on_locked` closure, because a rolled-back
    session expires the row and a retry that re-ran only `commit()` would persist nothing.
    The sidecar is rewritten AFTER that closure returns, because writing a file is exactly
    the non-idempotent side effect the retry rule says may not sit inside one. And the
    sidecar's own event commits separately, in `write_sidecar`, which is why it cannot share
    the closure above - two commits under one decorator is how a retry duplicates a row.
    """
    rec = db.session.get(Recording, recording_id)
    if rec is None:
        return jsonify({'error': 'Recording not found'}), 404

    data = request.get_json(silent=True) or {}
    cfg = load_config()

    values = {col: data[col] for col, _label in EDITABLE_FIELDS if col in data}
    problem = validate_edit(values)
    if problem:
        return jsonify({'error': problem}), 400

    lock = data.get('locked', UNSET)
    if lock is not UNSET and not isinstance(lock, bool):
        return jsonify({'error': 'The lock must be true or false.'}), 400

    sidecar = data.get('sidecar_enabled', UNSET)
    if sidecar is not UNSET and sidecar is not None and not isinstance(sidecar, bool):
        return jsonify(
            {'error': 'The metadata file switch must be true, false, or null to follow '
                      'the profile.'}), 400
    # The UI hides this control when the feature is off globally. That is presentation, and
    # presentation is never the gate - a payload that skipped the form gets the same answer
    # here (CLAUDE.md, "enforcement lives server-side").
    if sidecar is not UNSET and not globally_enabled(cfg):
        return jsonify(
            {'error': 'Metadata files for media servers are turned off in Settings, so '
                      'there is nothing to override for this recording.'}), 400

    @retry_on_locked()
    def _save_and_commit():
        r = db.session.get(Recording, recording_id)
        if r is None:
            return None
        changes = apply_user_edit(r, values, lock=lock, sidecar_enabled=sidecar)
        if changes:
            db.session.commit()
        return changes

    changed = _save_and_commit()
    if changed is None:
        return jsonify({'error': 'Recording not found'}), 404

    # Rewrite the file on disk when there already is one, so a correction shows up in the
    # library rather than only in this app. Skipped when nothing moved, when the recording
    # has not finished, or when its video is gone - a sidecar describing a file that is no
    # longer there is worse than none, since a media server would read it onto whatever
    # else it matches.
    rewrote = False
    if (changed and _metadata_phase(rec) == 'after' and rec.output_path
            and os.path.exists(rec.output_path)):
        from ..metadata_sidecar import write_sidecar
        rewrote = write_sidecar(recording_id, rec.output_path, cfg)

    db.session.expire_all()
    rec = db.session.get(Recording, recording_id)
    return jsonify({
        'success': True,
        'changed': changed,
        'sidecar_rewritten': rewrote,
        'meta': _metadata_panel(rec, cfg),
    })


@recordings_bp.route('/recordings/<int:recording_id>/live-thumbnail.jpg')
def live_thumbnail(recording_id):
    """Recording screenshot: regenerated from the live segment while IN_PROGRESS; for a
    finished recording, whichever of its two persisted images the
    recording.live_thumbnail.finished_image setting names - the poster frame taken from
    inside the program (recorder.persist_poster_frame) or the final frame captured at
    capture end (recorder.persist_final_thumbnail). The path stays live-thumbnail.jpg
    because every page already links it and the file it serves is the page's business,
    not the URL's."""
    rec = db.session.get(Recording, recording_id)
    if rec is None:
        abort(404)

    cfg = load_config()
    thumb_cfg = cfg.get('recording', {}).get('live_thumbnail', {})
    if not thumb_cfg.get('enabled', True):
        abort(404)

    thumb_dir = image_dir(cfg, THUMBNAILS)
    thumb_path = os.path.join(thumb_dir, f'{recording_id}.jpg')

    if rec.status != REC_STATUS_IN_PROGRESS:
        finished_path = finished_image_path(recording_id, cfg)
        if finished_path is None:
            abort(404)
        resp = send_file(finished_path, mimetype='image/jpeg')
        resp.headers['X-Thumb-Captured-At'] = str(int(os.path.getmtime(finished_path)))
        return resp
    # Must keep a .jpg extension - ffmpeg infers the output muxer from the
    # filename extension, so a plain ".tmp" suffix fails with "Unable to
    # choose an output format". Suffixed with a per-request uuid (rather than
    # a shared name) so two concurrent requests (e.g. two tabs, or the
    # initial page load racing its own JS refresh) each write their own tmp
    # file instead of one request's os.replace() finding the other's tmp
    # file already consumed - see dev/docs/BUGS.md 2026-07-13 entry.
    tmp_path = os.path.join(thumb_dir, f'{recording_id}.{uuid.uuid4().hex}.tmp.jpg')
    min_interval = thumb_cfg.get('min_regen_interval_seconds', 10)

    fresh_enough = (os.path.exists(thumb_path)
                     and (time.time() - os.path.getmtime(thumb_path)) < min_interval)

    if not fresh_enough:
        seg_path = get_live_segment_path(recording_id)
        if seg_path and os.path.exists(seg_path) and os.path.getsize(seg_path) > 0:
            # images_dir can change in Settings without a restart, so the folder the
            # startup pass created may not be this one.
            try:
                os.makedirs(thumb_dir, exist_ok=True)
            except OSError as exc:
                log.warning('Recording %d: cannot create thumbnail dir %s: %s',
                            recording_id, thumb_dir, exc)
            ffmpeg_path = resolve_ffmpeg_path(cfg.get('ffmpeg', {}).get('path', 'ffmpeg'))
            ok = capture_screenshot(
                seg_path, tmp_path, ffmpeg_path,
                seek_args=['-sseof', '-3'],
                timeout=thumb_cfg.get('capture_timeout_seconds', 12),
            )
            if ok:
                os.replace(tmp_path, thumb_path)

    if not os.path.exists(thumb_path):
        abort(503)

    resp = send_file(thumb_path, mimetype='image/jpeg')
    resp.headers['Cache-Control'] = 'no-store'
    resp.headers['X-Thumb-Captured-At'] = str(int(os.path.getmtime(thumb_path)))
    return resp


def _play_offer(rec, kept_source_on_disk: bool):
    """playback.offer() with the capture's own codec when the recording is still in its capture
    window - read from segment rows the page has loaded, never from a file."""
    from .. import playback
    vcodec = (playback.capture_codecs(rec.segments)[0]
              if rec.status in WINDOW_OPEN_STATUSES else None)
    return playback.offer(rec, kept_source_on_disk=kept_source_on_disk,
                          capture_video_codec=vcodec)


def _file_urls(recording_id: int) -> dict:
    # Relative URLs: an absolute one built behind a TLS-terminating proxy can come out as
    # http:// and be blocked as mixed content, while a relative one is right on any origin -
    # bare IP, hostname, http or https (dev/changelog/1312).
    return {
        'media_url': url_for('recordings.recording_file', recording_id=recording_id),
        'download_url': url_for('recordings.recording_file', recording_id=recording_id,
                                download=1),
        'playback_url': url_for('recordings.start_recording_playback',
                                recording_id=recording_id),
    }


@recordings_bp.route('/recordings/<int:recording_id>/file/check')
def recording_file_check(recording_id):
    """Run when the user clicks Play or Download, and only then: wakes the storage, proves
    the file is still where ChannelBin left it, and says how long that took. A recording
    page never touches a finished file on load (app/playback.py)."""
    from .. import playback
    rec = db.session.get(Recording, recording_id)
    if rec is None:
        return jsonify({'error': 'Recording not found.'}), 404
    if rec.status in WINDOW_OPEN_STATUSES:
        # Still capturing: there is no finished file to wake, and the storage the capture
        # writes to is awake. The start route reads the segment rows.
        o = _play_offer(rec, False)
        return jsonify({
            'success': True,
            'name': rec.name,
            'opened_seconds': 0,
            'native': False,
            'pipeline': False,
            'capture': o.capture,
            'download': False,
            # Start at the live edge: something is being written right now.
            'live': rec.status == REC_STATUS_IN_PROGRESS and get_state(rec.id) is not None,
            'reason': o.reason,
            'caveat': o.caveat,
            'video_codec': o.video_codec,
            'audio_codec': o.audio_codec,
            **_file_urls(recording_id),
        })
    try:
        opened = playback.open_for_playback(rec, load_config(),
                                            kept_source_on_disk=_kept_ts_on_disk(rec))
    except playback.PlaybackRefused as exc:
        return jsonify({'error': exc.message}), exc.status
    o = playback.offer(rec, kept_source_on_disk=True)
    return jsonify({
        'success': True,
        'name': os.path.basename(opened['path']),
        'size': opened['size'],
        'opened_seconds': round(opened['seconds'], 1),
        'native': o.native,
        'pipeline': o.pipeline,
        'capture': False,
        'download': True,
        # The position bar's whole length: the content duration ffprobe measured on the file,
        # not the scheduled or requested one. None when it was never measured.
        'duration': rec.recorded_duration_seconds,
        'reason': o.reason,
        'caveat': o.caveat,
        'video_codec': o.video_codec,
        'audio_codec': o.audio_codec,
        **_file_urls(recording_id),
    })


@recordings_bp.route('/recordings/<int:recording_id>/file')
def recording_file(recording_id):
    """The recording's own file, by id - never by a path from the request. Range requests are
    answered (send_file conditional=True), which is what lets the browser's player seek
    without reading the file from the start; `?download=1` makes it an attachment under the
    file's own name."""
    from .. import playback
    rec = db.session.get(Recording, recording_id)
    if rec is None:
        return jsonify({'error': 'Recording not found.'}), 404
    try:
        path = playback.resolve_file(rec, load_config(), kept_source_on_disk=_kept_ts_on_disk(rec))
    except playback.PlaybackRefused as exc:
        return jsonify({'error': exc.message}), exc.status
    download = request.args.get('download') == '1'
    try:
        resp = send_file(path, mimetype=playback.mimetype_for(path), conditional=True,
                         as_attachment=download, download_name=os.path.basename(path))
    except FileNotFoundError as exc:
        refusal = playback.missing_file_refusal(recording_id, path, exc)
        return jsonify({'error': refusal.message}), refusal.status
    except OSError as exc:
        log.warning('Recording %d: storage error serving %s: %s', recording_id, path, exc)
        refusal = playback.storage_refusal(path, exc)
        return jsonify({'error': refusal.message}), refusal.status
    # The file is the recording's and does not change once finished, but a Retry conversion
    # replaces it under the same URL; revalidating is a cheap stat on a disk that is awake.
    resp.headers['Cache-Control'] = 'no-cache'
    return resp


@recordings_bp.route('/api/recordings/<int:recording_id>/playback', methods=['POST'])
def start_recording_playback(recording_id):
    """Play a file the browser cannot take by itself through the preview pipeline, from
    `offset` seconds (JSON body). A seek is another call with a new offset: it replaces the
    session before the new one starts (app/preview.py). The player calls this after the
    click's check has woken the storage; the session is then driven through the preview's
    own status, playlist and stop routes."""
    import math

    from .. import playback, preview
    from .preview import session_urls
    rec = db.session.get(Recording, recording_id)
    if rec is None:
        return jsonify({'error': 'Recording not found.'}), 404
    body = request.get_json(silent=True) or {}
    try:
        offset = float(body.get('offset') or 0)
    except (TypeError, ValueError):
        return jsonify({'error': 'The position to play from must be a number of seconds.'}), 400
    if not math.isfinite(offset) or offset < 0:
        return jsonify({'error': 'The position to play from must be 0 or more seconds.'}), 400
    if rec.status in WINDOW_OPEN_STATUSES:
        o = _play_offer(rec, False)
        if not o.capture:
            return jsonify({'error': o.reason or 'This recording has nothing to play.'}), 409
        return _start_capture_playback(rec, body, offset)
    if rec.status in (REC_STATUS_CONCATENATING, REC_STATUS_ANALYZING, REC_STATUS_CONVERTING):
        # Reached from a player that was following the capture when it stopped.
        return jsonify({'error': 'Its final file is being made from the captured segments '
                                 'now.'}), 409
    kept = _kept_ts_on_disk(rec)
    o = playback.offer(rec, kept_source_on_disk=kept)
    if not o.pipeline:
        return jsonify({'error': (
            'This recording plays in the browser\'s own player.' if o.native
            else (o.reason or 'This recording has no finished file to play.'))}), 409
    duration = rec.recorded_duration_seconds
    if duration and offset >= duration:
        return jsonify({'error': (
            f'{fmt_utils.fmt_duration(offset, with_seconds=True)} is past the end of the '
            f'recording ({fmt_utils.fmt_duration(duration, with_seconds=True)}).')}), 400
    try:
        path = playback.resolve_file(rec, load_config(), kept_source_on_disk=kept)
        session = preview.start_file_playback(
            rec.id, rec.name, path, offset,
            transcode_audio=preview.audio_needs_transcode(rec.recorded_audio_codec))
    except playback.PlaybackRefused as exc:
        return jsonify({'error': exc.message}), exc.status
    except preview.PreviewRefused as exc:
        return jsonify({'error': exc.message}), exc.status
    return jsonify({'success': True, **session.to_dict(), **session_urls(session.id)})


def _start_capture_playback(rec, body: dict, offset: float):
    """Play a recording still in its capture window from `offset` seconds of what has been
    captured, or from the live edge when the body says `live` (dev/changelog/1316).

    The timeline is the joinable segments laid end to end (playback.capture_timeline). A
    position in a finished segment plays it from there and ends with it; one in the segment
    being written follows that file, which ends when the recorder leaves it - the player then
    asks again, with `moved_from` naming the segment it was on so the answer can say why the
    recording moved, in the recording's own events."""
    from .. import playback, preview
    from ..concatenator import joinable_segments
    from .preview import session_urls

    state = get_state(rec.id) if rec.status == REC_STATUS_IN_PROGRESS else None
    segments = sorted(rec.segments, key=lambda s: s.segment_number)
    spans = playback.capture_timeline(joinable_segments(segments), datetime.utcnow(),
                                      state.current_segment_num if state else None)
    # 409 for both "not yet" answers: the player asks again while the recording is capturing.
    if not spans:
        return jsonify({'error': 'Nothing has been captured yet.'}), 409
    captured = spans[-1].end
    position = offset
    if body.get('live'):
        # Nothing being written (paused, retrying, or the player's follower outlived the
        # capture): "live" is the last few seconds that were captured.
        position = captured if state is not None else max(0.0, captured - playback.LIVE_SNAP_SECONDS)
    resolved = playback.resolve_capture_position(spans, position)
    if resolved is None and state is not None:
        return jsonify({'error': (
            f'The capture is starting segment {state.current_segment_num}, which has not '
            'written anything yet.')}), 409
    if resolved is None:
        return jsonify({'error': (
            f'{fmt_utils.fmt_duration(position, with_seconds=True)} is past what has been '
            f'captured so far ({fmt_utils.fmt_duration(captured, with_seconds=True)}).')}), 400
    span, at, live = resolved
    if live:
        # A few seconds before what is written so far; the follower's burst reads up to it.
        at = max(0.0, span.length - playback.FOLLOW_FROM_END_SECONDS)
    seg = next(s for s in segments if s.segment_number == span.segment_number)
    acodec = seg.probe_audio_codec or playback.capture_codecs(segments)[1]
    place = preview.CapturePlace(
        segment_number=span.segment_number, base=span.base,
        span_end=None if span.growing else span.end,
        follow_state=state if span.growing else None, live=live)
    try:
        path = playback.resolve_segment_file(rec, span.file_path, load_config())
        session = preview.start_file_playback(
            rec.id, rec.name, path, at,
            transcode_audio=preview.audio_needs_transcode(acodec),
            capture=place)
    except playback.PlaybackRefused as exc:
        return jsonify({'error': exc.message}), exc.status
    except preview.PreviewRefused as exc:
        return jsonify({'error': exc.message}), exc.status
    return jsonify({
        'success': True, **session.to_dict(), **session_urls(session.id),
        # What has been captured so far, by the segments' clocks; it grows while capturing.
        'captured_seconds': round(captured, 1),
        'capturing': state is not None,
        'capture_note': _capture_note(rec.status, state is not None),
        'moved_because': _why_segment_ended(rec.id, body.get('moved_from')),
    })


def _capture_note(status: str, capturing: bool) -> str:
    """What the player says about a recording in its capture window that is not writing
    anything right now. Every window status is named (CLAUDE.md, states are enumerated)."""
    if capturing:
        return ''
    if status == REC_STATUS_PAUSED:
        return 'The recording is paused, so this plays what was captured before the pause.'
    if status == REC_STATUS_RETRYING:
        return ('The stream dropped and the recording is waiting to try it again, so this plays '
                'what was captured before the drop.')
    if status == REC_STATUS_IN_PROGRESS:
        return 'Nothing is being written right now, so this plays what has been captured.'
    log.warning('No capture note for status %r', status)
    return ''


def _why_segment_ended(recording_id: int, segment_number) -> str:
    """The recording's own account of why `segment_number` ended: its stall and failover
    events, then the segment's end, as written. '' when nothing names it."""
    try:
        segment_number = int(segment_number)
    except (TypeError, ValueError):
        return ''
    seg = (RecordingSegment.query
           .filter_by(recording_id=recording_id, segment_number=segment_number).first())
    if seg is None:
        return ''
    nxt = (RecordingSegment.query
           .filter(RecordingSegment.recording_id == recording_id,
                   RecordingSegment.segment_number > segment_number)
           .order_by(RecordingSegment.segment_number).first())
    window = [RecordingEvent.recording_id == recording_id,
              RecordingEvent.timestamp >= seg.started_at]
    if nxt is not None:
        window.append(RecordingEvent.timestamp <= nxt.started_at)
    events = (RecordingEvent.query
              .filter(*window,
                      db.or_(db.and_(RecordingEvent.segment_number == segment_number,
                                     RecordingEvent.event_type.in_((STALL_DETECTED,
                                                                    SEGMENT_ENDED))),
                             RecordingEvent.event_type == GROUP_FAILOVER))
              .order_by(RecordingEvent.id).all())
    order = {STALL_DETECTED: 0, GROUP_FAILOVER: 1, SEGMENT_ENDED: 2}
    picked = sorted(events, key=lambda e: order[e.event_type])
    return ' '.join(e.detail.rstrip('.') + '.' for e in picked if e.detail)


# Refusing is the honest answer for a CONCATENATING row. The join is the step that produces
# the final file, and it runs inside a blocking supervise_ffmpeg() call on the concat thread;
# nothing registers its child, so no route holds a handle that could stop it. Marking the row
# ABORTED while it keeps running is what this replaced (dev/changelog/667).
#
# The registries next door answer different questions and neither is a way in:
# concatenator._active_concats says a chain owns this recording (so a second one cannot
# start) and _concat_progress says how far the join has got (dev/changelog/959). Adding an
# on_spawn hook would make the process reachable, and would still not make cancelling it
# right here - the output is committed only on success, so a killed join leaves the segments
# and no file, which is what Retry concatenation is for.
CONCAT_CANCEL_REFUSED = (
    "The join is already running and can't be interrupted. It is the step that joins "
    'the captured segments into the final file; wait for it to finish, then delete the '
    "recording if you don't want it."
)


def _cancel_analysis(recording_id, rec):
    """Stop an ANALYZING recording's post-processing and return the message to surface.

    ANALYZING is cancellable where CONCATENATING is refused, and the difference is what is
    already on disk: the concat has committed its output, so the recorded file is whole and
    the work still running is read-only ffprobe over it. Nothing has to be killed - marking
    the row ABORTED is enough, because do_postprocess() checks at every phase boundary and
    preserve_cancelled_status() guards each of its terminal writes, so the chain stops
    instead of converting and reporting COMPLETED over the user's decision.

    The stop is therefore not instant, and the message says so rather than implying it:
    a whole-file scan already under way runs to its end first (dev/changelog/867).
    """
    @retry_on_locked()
    def _mark_aborted_and_commit():
        r = db.session.get(Recording, recording_id)
        add_recording_event(
            recording_id, RECORDING_ABORTED,
            detail='Cancelled during post-capture analysis - the recorded .ts file is kept.')
        r.status = REC_STATUS_ABORTED
        r.cancel_reason = CANCEL_DURING_ANALYSIS
        r.completed_at = datetime.utcnow()
        db.session.commit()

    _mark_aborted_and_commit()
    dismiss_recording_failing_alerts(recording_id)
    log.info('Recording %d cancelled during post-capture analysis', recording_id)
    return (f'Cancelling "{rec.name}". Post-processing stops at its next checkpoint - a '
            'file check already under way finishes first - and the recorded .ts file is kept.')


@recordings_bp.route('/recordings/<int:recording_id>/cancel', methods=['POST'])
def cancel_recording(recording_id):
    # No view-level retry_on_locked: the IN_PROGRESS/PAUSED branch owns no commit and
    # a lock-retry would re-run abort_recording + file deletion. The SCHEDULED branch's
    # row delete is retried inside _unschedule_and_delete_row's own closure.
    rec = db.session.get(Recording, recording_id)
    if rec is None:
        flash('Recording not found.', 'error')
        return redirect(url_for('recordings.index'))

    if rec.status in (REC_STATUS_COMPLETED, REC_STATUS_FAILED, REC_STATUS_ABORTED):
        flash('Recording is already finished.', 'error')
        return redirect(url_for('recordings.recording_detail', recording_id=recording_id))

    if rec.status == REC_STATUS_SCHEDULED:
        # The same answer the JSON route gives: a SCHEDULED recording never captured
        # anything, so cancelling it deletes the row (dev/changelog/814). This branch used
        # to mark it ABORTED instead, so the outcome depended on which endpoint the caller
        # reached (dev/changelog/1247).
        name = rec.name
        end_capture_wait(recording_id)
        _unschedule_and_delete_row(recording_id)
        log.info('Recording "%s" (#%d) cancelled before it started', name, recording_id)
        flash(f'Recording "{name}" cancelled.', 'success')
    elif rec.status in WINDOW_OPEN_STATUSES:
        _abort_and_delete_files(recording_id)
        flash(f'Recording "{rec.name}" cancelled and files deleted.', 'success')
    elif rec.status == REC_STATUS_CONVERTING:
        flash(_cancel_conversion(recording_id, rec), 'success')
        return redirect(url_for('recordings.recording_detail', recording_id=recording_id))
    elif rec.status == REC_STATUS_ANALYZING:
        flash(_cancel_analysis(recording_id, rec), 'success')
        return redirect(url_for('recordings.recording_detail', recording_id=recording_id))
    elif rec.status == REC_STATUS_CONCATENATING:
        flash(CONCAT_CANCEL_REFUSED, 'error')
        return redirect(url_for('recordings.recording_detail', recording_id=recording_id))
    else:
        # Every real status is named above. Reaching here means the row holds a value the
        # status vocabulary does not define, which is a bug to see rather than to absorb
        # into an abort.
        log.error('cancel_recording: recording %d has unrecognized status %r', recording_id, rec.status)
        flash(f'Recording is in an unrecognized state ({rec.status}) and was not cancelled.', 'error')
        return redirect(url_for('recordings.recording_detail', recording_id=recording_id))

    return redirect(url_for('recordings.index'))


@recordings_bp.route('/recordings/<int:recording_id>/stop', methods=['POST'])
def stop_recording_manual(recording_id):
    rec = db.session.get(Recording, recording_id)
    if rec is None:
        flash('Recording not found.', 'error')
        return redirect(url_for('recordings.index'))

    if rec.status not in WINDOW_OPEN_STATUSES:
        flash('Recording is not active.', 'error')
        return redirect(url_for('recordings.recording_detail', recording_id=recording_id))

    from flask import current_app
    stop_recording(current_app._get_current_object(), recording_id, reason='MANUAL_STOP')
    flash(f'Recording "{rec.name}" stopped - processing segments…', 'success')
    return redirect(url_for('recordings.recording_detail', recording_id=recording_id))


@recordings_bp.route('/recordings/<int:recording_id>/pause', methods=['POST'])
def pause_recording_route(recording_id):
    rec = db.session.get(Recording, recording_id)
    if rec is None:
        flash('Recording not found.', 'error')
        return redirect(url_for('recordings.index'))

    if rec.status != REC_STATUS_IN_PROGRESS:
        flash('Recording is not in progress.', 'error')
        return redirect(url_for('recordings.recording_detail', recording_id=recording_id))

    from flask import current_app
    pause_recording(current_app._get_current_object(), recording_id)
    flash(f'Recording "{rec.name}" paused.', 'success')
    return redirect(url_for('recordings.recording_detail', recording_id=recording_id))


@recordings_bp.route('/recordings/<int:recording_id>/resume', methods=['POST'])
def resume_recording_route(recording_id):
    rec = db.session.get(Recording, recording_id)
    if rec is None:
        flash('Recording not found.', 'error')
        return redirect(url_for('recordings.index'))

    if rec.status != REC_STATUS_PAUSED:
        flash('Recording is not paused.', 'error')
        return redirect(url_for('recordings.recording_detail', recording_id=recording_id))

    if rec.stop_time <= datetime.utcnow():
        flash("Cannot resume - the recording's stop time has already passed.", 'error')
        return redirect(url_for('recordings.recording_detail', recording_id=recording_id))

    from flask import current_app
    from ..scheduler import reschedule_recording_stop
    resume_recording(current_app._get_current_object(), recording_id)
    # Re-register the stop job in case it was removed - the stop job only: the start has
    # already happened (dev/changelog/1242).
    reschedule_recording_stop(recording_id, rec.stop_time)
    flash(f'Recording "{rec.name}" resumed.', 'success')
    return redirect(url_for('recordings.recording_detail', recording_id=recording_id))


@recordings_bp.route('/recordings/<int:recording_id>/adjust-stop', methods=['POST'])
@retry_on_locked()
def adjust_stop_time(recording_id):
    """JSON endpoint - change stop_time of an active recording from the TV Guide."""
    rec = db.session.get(Recording, recording_id)
    if rec is None:
        return jsonify({'error': 'Recording not found'}), 404
    if rec.status not in WINDOW_OPEN_STATUSES:
        return jsonify({'error': 'Recording is not active'}), 400

    data = request.get_json(silent=True) or {}
    stop_str = data.get('stop_time', '').strip()
    try:
        stop_utc = parse_local_to_utc(stop_str)
    except ValueError:
        return jsonify({'error': 'Invalid stop time format'}), 400

    if stop_utc <= datetime.utcnow():
        return jsonify({'error': 'Stop time must be in the future'}), 400
    if stop_utc <= rec.start_time:
        return jsonify({'error': 'Stop time must be after start time'}), 400

    old_stop = rec.stop_time
    rec.stop_time = stop_utc
    if old_stop != stop_utc:
        db.session.add(RecordingEvent(
            recording_id=rec.id,
            event_type=RECORDING_STOP_TIME_ADJUSTED,
            detail=f'Stop time adjusted: {old_stop} → {stop_utc}',
        ))
    db.session.commit()

    # Re-register the stop job with the new time
    unschedule_recording(recording_id)
    from flask import current_app
    schedule_recording(current_app._get_current_object(), rec.id, rec.start_time, stop_utc)

    return jsonify({'success': True, 'stop_time': stop_utc.strftime('%Y-%m-%dT%H:%M:%S')})


@recordings_bp.route('/recordings/<int:recording_id>/stop-json', methods=['POST'])
def stop_recording_json(recording_id):
    """JSON endpoint for stopping an active recording (used from TV Guide)."""
    rec = db.session.get(Recording, recording_id)
    if rec is None:
        return jsonify({'error': 'Recording not found'}), 404
    if rec.status not in WINDOW_OPEN_STATUSES:
        return jsonify({'error': 'Recording is not active'}), 400
    from flask import current_app
    stop_recording(current_app._get_current_object(), recording_id, reason='MANUAL_STOP')
    return jsonify({'success': True})


@recordings_bp.route('/recordings/<int:recording_id>/pause-json', methods=['POST'])
def pause_recording_json(recording_id):
    """JSON endpoint for pausing an active recording (used from TV Guide)."""
    rec = db.session.get(Recording, recording_id)
    if rec is None:
        return jsonify({'error': 'Recording not found'}), 404
    if rec.status != REC_STATUS_IN_PROGRESS:
        return jsonify({'error': 'Recording is not in progress'}), 400
    from flask import current_app
    pause_recording(current_app._get_current_object(), recording_id)
    return jsonify({'success': True})


@recordings_bp.route('/recordings/<int:recording_id>/resume-json', methods=['POST'])
def resume_recording_json(recording_id):
    """JSON endpoint for resuming a paused recording (used from TV Guide)."""
    rec = db.session.get(Recording, recording_id)
    if rec is None:
        return jsonify({'error': 'Recording not found'}), 404
    if rec.status != REC_STATUS_PAUSED:
        return jsonify({'error': 'Recording is not paused'}), 400
    if rec.stop_time <= datetime.utcnow():
        return jsonify({'error': 'Stop time has already passed'}), 400
    from flask import current_app
    from ..scheduler import reschedule_recording_stop
    resume_recording(current_app._get_current_object(), recording_id)
    reschedule_recording_stop(rec.id, rec.stop_time)
    return jsonify({'success': True})


@recordings_bp.route('/recordings/<int:recording_id>/cancel-json', methods=['POST'])
def cancel_recording_json(recording_id):
    """JSON endpoint for cancelling an active recording (used from TV Guide)."""
    rec = db.session.get(Recording, recording_id)
    if rec is None:
        return jsonify({'error': 'Recording not found'}), 404

    if rec.status in (REC_STATUS_COMPLETED, REC_STATUS_FAILED, REC_STATUS_ABORTED):
        return jsonify({'error': 'Recording is already finished'}), 400

    if rec.status in WINDOW_OPEN_STATUSES:
        _abort_and_delete_files(recording_id)
    elif rec.status == REC_STATUS_CONVERTING:
        _cancel_conversion(recording_id, rec)
    elif rec.status == REC_STATUS_ANALYZING:
        _cancel_analysis(recording_id, rec)
    elif rec.status == REC_STATUS_CONCATENATING:
        return jsonify({'error': CONCAT_CANCEL_REFUSED}), 409
    elif rec.status != REC_STATUS_SCHEDULED:
        log.error('cancel_recording_json: recording %d has unrecognized status %r',
                  recording_id, rec.status)
        return jsonify({'error': f'Recording is in an unrecognized state ({rec.status}).'}), 400
    else:
        # A SCHEDULED recording never started, so there is nothing captured to preserve -
        # cancelling it deletes the row rather than marking it ABORTED (dev/changelog/814).
        name = rec.name
        _unschedule_and_delete_row(recording_id)
        log.info('Recording "%s" (#%d) cancelled before it started', name, recording_id)

    return jsonify({'success': True})


@recordings_bp.route('/recordings/<int:recording_id>/delete', methods=['POST'])
def delete_recording(recording_id):
    rec = db.session.get(Recording, recording_id)
    if rec is None:
        flash('Recording not found.', 'error')
        return redirect(url_for('recordings.index'))

    if rec.status in RESTART_BLOCKING_STATUSES:
        flash('Cannot delete an active recording. Cancel it first.', 'error')
        return redirect(url_for('recordings.recording_detail', recording_id=recording_id))

    name = rec.name
    remove_files = _wants_file_deletion(request.form)
    # Collect file paths before the row is gone; unlink only after the delete
    # durably commits (file removal is a non-idempotent side effect - keep it
    # out of the retry_on_locked closure per CLAUDE.md).
    paths = (recording_disk_paths(recording_id) if remove_files
             else recording_image_paths(recording_id))
    from ..scheduler import unschedule_recording
    unschedule_recording(recording_id)

    @retry_on_locked()
    def _delete_row():
        r = db.session.get(Recording, recording_id)
        detach_recording_references(recording_id)
        if r is not None:
            db.session.delete(r)
        db.session.commit()

    _delete_row()
    removed = delete_files(paths)
    _log_recording_deleted(recording_id, name, remove_files, removed)
    if remove_files:
        flash(f'Recording "{name}" deleted.', 'success')
    else:
        flash(f'Recording "{name}" deleted. Its files were left on disk.', 'success')
    return redirect(url_for('recordings.index'))


def _create_scheduled_recording(cfg, *, name, url, start_utc, stop_utc, channel_id,
                                 group_id, profile_id, entry=None, blocks=None, link=None):
    """Create one SCHEDULED recording and arm its start - the one create path, shared by
    the record modal (new_recording_json), the bulk schedule (bulk_schedule_json) and
    auto-record rules (app/auto_rules.py), so none of them can drift in what a new
    recording carries (dev/changelog/1157).

    `entry` is the EPGEntry being recorded, or None for a manual URL. `blocks` is the
    account-block picker's {account_id: slots}, or None to set none. A start already in
    the past is moved to now and logged as such.

    `link(recording)` runs inside the create's own commit unit, after the row is flushed
    (so it has an id) and before the commit, so whatever it writes lands with the recording
    or not at all - a rule's provenance, its event and its ledger row (DESIGN-autoschedule.md
    §4.4). It returns False to call the create off, and then nothing is written and this
    returns None.

    Returns the committed Recording, or None when `link` called it off.
    """
    now_utc = datetime.utcnow()
    created_after_start = start_utc < now_utc
    if created_after_start:
        start_utc = now_utc

    # Snapshot the EPG program's own air time (independent of what the user
    # typed into the form) at creation, so it stays put even if the program
    # later shifts in the guide or the recording is edited/adjusted. The one
    # exception is an auto-record rule's recording, which retrack moves with
    # its program while nobody has edited its times (dev/changelog/1188).
    # A plain read - not wrapped in retry_on_locked - whose result is captured
    # into plain local variables (not an ORM object reference) before the
    # write closure below, so nothing here is affected by a rollback+retry
    # inside that closure.
    #
    # The same read also seeds the metadata_* family - the program's synopsis, genre and
    # rating. Those are NOT an immutable snapshot: recorder.start_recording() refreshes
    # them from the program's listing as it stands at air time. Seeding them here anyway
    # is what makes the data survive at all, because epg_keep_days prunes the listing a
    # day after it airs and nothing can recover it afterwards (dev/changelog/1055).
    program_start_time = program_stop_time = None
    program_title = program_sub_title = None
    metadata_description = metadata_category = metadata_rating = None
    if entry is not None:
        program_start_time = entry.start_time
        program_stop_time = entry.stop_time
        program_title = entry.title
        program_sub_title = entry.sub_title
        metadata_description = entry.description
        metadata_category = entry.category
        metadata_rating = entry.rating

    # Each step below is its own retry unit rather than the whole route, so a
    # retried second commit can never re-run db.session.add(rec) and create a
    # duplicate Recording row (see dev/docs/BUGS.md for the same class of bug fixed here
    # after being caught in create_on_demand_job).
    @retry_on_locked()
    def _create_recording_and_commit():
        r = Recording(
            name=name,
            url=url,
            start_time=start_utc,
            stop_time=stop_utc,
            scheduled_start_time=start_utc,
            scheduled_stop_time=stop_utc,
            program_start_time=program_start_time,
            program_stop_time=program_stop_time,
            program_title=program_title,
            program_sub_title=program_sub_title,
            metadata_description=metadata_description,
            metadata_category=metadata_category,
            metadata_rating=metadata_rating,
            status=REC_STATUS_SCHEDULED,
            channel_id=channel_id,
            group_id=group_id,
            profile_id=profile_id,
        )
        db.session.add(r)
        if link is not None:
            db.session.flush()
            if not link(r):
                db.session.rollback()
                return None
        db.session.commit()
        return r

    rec = _create_recording_and_commit()
    if rec is None:
        return None

    # Saved before the start is armed, so a recording created after its own start time
    # never opens a stream on an account it was asked to keep off.
    if blocks:
        set_recording_blocks(rec.id, blocks)

    if created_after_start:
        @retry_on_locked()
        def _log_created_after_start_and_commit():
            db.session.add(RecordingEvent(
                recording_id=rec.id,
                event_type=RECORDING_CREATED_AFTER_EVENT_START,
                detail='The program had already started, so the recording started right away',
            ))
            db.session.commit()

        _log_created_after_start_and_commit()

    from flask import current_app
    schedule_recording(current_app._get_current_object(), rec.id, start_utc, stop_utc)

    # Schedule-time half of DESIGN-prerecord-checks.md §2 (dev/changelog/478): warn now
    # if the channel/group this recording just landed on is already failing, rather than
    # waiting for the next scheduled test to happen to run assess_scheduled_recording_impact.
    evaluate_and_alert_recording(rec, cfg)

    return rec


@recordings_bp.route('/recordings/new-json', methods=['POST'])
def new_recording_json():
    name, url, start_utc, stop_utc, errors = _parse_recording_form(request.form)
    if errors:
        return jsonify({'error': ' '.join(errors)}), 400

    cfg = load_config()

    channel_id_raw = request.form.get('channel_id', '').strip()
    channel_id = int(channel_id_raw) if channel_id_raw.isdigit() else None
    if channel_id is None:
        # URL matching is correct here and deliberately left alone by the URL-drift work
        # (DESIGN-url-drift.md 2/3): this branch only runs when the form supplied no
        # channel_id, so there is no channel identity to prefer over the URL.
        ch = _match_channel_by_url(url)
        if ch:
            channel_id = ch.id

    # "Find Another Airing" replace mode: create this recording, then delete the
    # SCHEDULED one it replaces (create-first, so a failure never loses the original).
    # Parsed before the group block below, which needs it to discount the recording
    # being replaced when it reads the window's existing commitments.
    replace_raw = request.form.get('replace_recording_id', '').strip()
    replace_recording_id = int(replace_raw) if replace_raw.isdigit() else None
    wanted_blocks = _parse_block_form(request.form)

    # Group-backed recording (created from a channel-group guide row): channel_id must
    # be a member; the best member is re-resolved at record start (app/recorder.py).
    group_id_raw = request.form.get('group_id', '').strip()
    group_id = int(group_id_raw) if group_id_raw.isdigit() else None
    if group_id is not None:
        group = db.session.get(ChannelGroup, group_id)
        if group is None:
            group_id = None
        elif group.is_system:
            # The pinned TV Guide health check's group has computed membership and is not a
            # recording source (server-side enforcement, not just UI).
            return jsonify({'error': 'The TV Guide health check cannot back a '
                                     'recording.'}), 400
        else:
            # The channel_id the modal supplies for a group row is the guide's serving
            # member - the app's own ranking, not a feed the user picked by hand - so a
            # multi-account group rolls over to an account with room in this window
            # rather than stamping a member the limit check below would refuse.
            streak_threshold = cfg.get('channel_testing', {}).get(
                'failing_streak_threshold', DEFAULT_FAILING_STREAK_THRESHOLD)
            resolved, _reason = _resolve_group_member(
                group, channel_id, start_utc, stop_utc, streak_threshold,
                exclude_recording_id=replace_recording_id, pending_blocks=wanted_blocks)
            if resolved is None:
                group_id = None
            else:
                channel_id = resolved

    profile_id_raw = request.form.get('profile_id', '').strip()
    profile_id = int(profile_id_raw) if profile_id_raw.isdigit() else None

    if replace_recording_id is not None:
        old = db.session.get(Recording, replace_recording_id)
        if old is None:
            return jsonify({'error': 'Recording to replace not found.'}), 404
        if old.status != REC_STATUS_SCHEDULED:
            return jsonify({'error': 'Only SCHEDULED recordings can be replaced.'}), 400

    force = request.form.get('force') == '1'
    if not force:
        warnings = _pending_recording_warnings(
            channel_id, group_id, start_utc, stop_utc,
            exclude_recording_id=replace_recording_id, pending_blocks=wanted_blocks)
        if warnings:
            return jsonify({'success': False, **warnings})

    source_epg_id_raw = request.form.get('source_epg_id', '').strip()
    entry = (db.session.get(EPGEntry, int(source_epg_id_raw))
             if source_epg_id_raw.isdigit() else None)
    rec = _create_scheduled_recording(
        cfg, name=name, url=url, start_utc=start_utc, stop_utc=stop_utc,
        channel_id=channel_id, group_id=group_id, profile_id=profile_id,
        entry=entry, blocks=wanted_blocks)

    if replace_recording_id is not None:
        unschedule_recording(replace_recording_id)

        @retry_on_locked()
        def _delete_replaced_and_commit():
            old = db.session.get(Recording, replace_recording_id)
            detach_recording_references(replace_recording_id)
            detail = 'Created via Find Another Airing'
            if old is not None:
                detail += (f', replacing scheduled recording #{old.id} "{old.name}" '
                           f'({old.start_time} – {old.stop_time} UTC), which was deleted')
                db.session.delete(old)  # hidden-recompute-ok: a Recording, not a group or membership
            db.session.add(RecordingEvent(
                recording_id=rec.id,
                event_type=RECORDING_REPLACED_OTHER,
                detail=detail,
            ))
            db.session.commit()

        _delete_replaced_and_commit()

    return jsonify({'success': True, 'id': rec.id})


# ── Bulk schedule from the airing search (dev/changelog/1157) ───────────────
#
# Several showings ticked on the EPG search's airings list, one set of settings, each
# showing its own ordinary scheduled recording. Preview and create run the SAME planner,
# so what the preview promised is what the create does; create re-plans rather than
# trusting a plan the client sends back, since the guide may have moved in between.

#: A generous ceiling on one request - three pages of 100 showings. The planner's own
#: queries are batched, but every created recording arms its own scheduler jobs.
BULK_SCHEDULE_MAX = 300

BULK_OK = 'ok'
BULK_WARN = 'warn'      # overlaps something in time - proceedable
BULK_HARD = 'hard'      # overlaps on one account with too few connections - proceedable, loud
BULK_SKIP = 'skip'      # not created at all; `reason` says why


def _parse_bulk_schedule(data):
    """(items, profile_choice, error). `items` is [(epg_id, group_id or None)] in request
    order; `profile_choice` is 'default' (each showing's own default, as its Record button
    would pick), None (no profile - the global defaults) or a RecordingProfile."""
    from ..database import RecordingProfile
    raw_items = data.get('items')
    if not isinstance(raw_items, list) or not raw_items:
        return None, None, 'Select at least one showing to schedule.'
    if len(raw_items) > BULK_SCHEDULE_MAX:
        return None, None, (f'At most {BULK_SCHEDULE_MAX} showings can be scheduled at '
                            f'once; {len(raw_items)} were selected.')

    def _id(v):
        return v if isinstance(v, int) and not isinstance(v, bool) and v > 0 else None

    items = []
    for raw in raw_items:
        if not isinstance(raw, dict) or _id(raw.get('epg_id')) is None:
            return None, None, 'Every showing needs a numeric epg_id.'
        group_raw = raw.get('group_id')
        if group_raw is not None and _id(group_raw) is None:
            return None, None, 'group_id must be a numeric id when given.'
        items.append((raw['epg_id'], group_raw))

    choice = data.get('profile', 'default')
    if choice == 'default':
        return items, 'default', None
    if choice in (None, 'none'):
        return items, None, None
    if _id(choice) is None:
        return None, None, 'profile must be "default", "none" or a profile id.'
    profile = db.session.get(RecordingProfile, choice)
    if profile is None:
        return None, None, 'That recording profile no longer exists.'
    return items, profile, None


def _plan_bulk_schedule(items, profile_choice, cfg, skip_overlapped=True):
    """One plan per requested showing, in request order - what would be created and what
    each one would be warned about.

    Every showing is judged against the recordings already stored AND the showings before
    it in this same request (`pending`), so eight overlapping picks on a one-connection
    account are seven hard warnings rather than eight clean rows. Each plan carries its
    private create arguments under '_create'; bulk_plan_payload() strips them.

    `skip_overlapped=False` turns off the "Already scheduled / Already being recorded" skip,
    for auto-record rules only (DESIGN-autoschedule.md §4.3). That state comes from
    match_recording(), an overlap test against a recording's PADDED window, so a padded
    neighbor on the same channel reads as this program and a race starting inside the
    previous race's post-padding would never be recorded. A rule judges identity and full
    coverage itself, and a partial overlap is a handoff, exactly as for a person's
    recordings. The person-facing bulk scheduler keeps the skip."""
    from types import SimpleNamespace
    from ..accounts import default_profile_for, filename_naming_for
    from ..channel_search_rows import (
        REC_STATE_PAST, REC_STATE_RECORDING, REC_STATE_SCHEDULED,
        _record_state, _recordings_for, _suggested_name)
    from ..database import Tag
    from ..tz_utils import get_display_tz

    now = datetime.utcnow()
    epg_ids = [epg_id for epg_id, _ in items]
    entries = {e.id: e for e in EPGEntry.query.filter(EPGEntry.id.in_(epg_ids)).all()}
    channels = {c.id: c for c in Channel.query.filter(
        Channel.id.in_({e.channel_id for e in entries.values()})).all()}
    recordings = _recordings_for(list(entries.values()), channels) if skip_overlapped else {}
    group_ids = {g for _, g in items if g is not None}
    groups = ({g.id: g for g in ChannelGroup.query.filter(ChannelGroup.id.in_(group_ids)).all()}
              if group_ids else {})
    streak_threshold = cfg.get('channel_testing', {}).get(
        'failing_streak_threshold', DEFAULT_FAILING_STREAK_THRESHOLD)
    tags_by_name = {t.name: t for t in Tag.query.all()}
    tz = get_display_tz()
    ctx = SimpleNamespace(now=now)

    plans, pending, seen = [], [], set()
    for epg_id, group_id in items:
        plan = {'epg_id': epg_id, 'group_id': group_id, 'verdict': BULK_SKIP,
                'reason': None, 'warnings': {}}
        plans.append(plan)
        if epg_id in seen:
            plan['reason'] = 'Listed twice - scheduled once.'
            continue
        seen.add(epg_id)
        entry = entries.get(epg_id)
        ch = channels.get(entry.channel_id) if entry is not None else None
        if entry is None or ch is None:
            plan['reason'] = 'This showing is no longer in the guide data.'
            continue
        plan.update(title=entry.title or ch.name, sub_title=entry.sub_title or '',
                    channel_name=ch.name, program_start=_iso_utc(entry.start_time),
                    program_stop=_iso_utc(entry.stop_time))

        state = _record_state(entry, recordings.get(epg_id), ctx)
        if state == REC_STATE_RECORDING:
            plan['reason'] = 'Already being recorded.'
            continue
        if state == REC_STATE_SCHEDULED:
            plan['reason'] = 'Already scheduled.'
            continue
        if state == REC_STATE_PAST:
            plan['reason'] = 'Already over.'
            continue

        group = None
        if group_id is not None:
            group = groups.get(group_id)
            if (group is None or group.is_system
                    or ch.id not in {m.channel_id for m in group.memberships}):
                plan['reason'] = 'This showing is not on a member of that channel group.'
                continue
            plan['group_name'] = group.name

        profile = (default_profile_for(ch, group) if profile_choice == 'default'
                   else profile_choice)
        pre = (profile.pre_padding_minutes or 0) if profile is not None else 0
        post = (profile.post_padding_minutes or 0) if profile is not None else 0
        start = entry.start_time - timedelta(minutes=pre)
        stop = entry.stop_time + timedelta(minutes=post)

        target_id = ch.id
        if group is not None:
            # The same resolver the record modal's group path uses, counting this request's
            # earlier picks, so a multi-account group rolls onto an account with room.
            resolved, _reason = _resolve_group_member(
                group, ch.id, start, stop, streak_threshold, pending=pending)
            if resolved is None:
                group = None
                plan['group_id'] = None
                plan.pop('group_name', None)
            else:
                target_id = resolved
        target = channels.get(target_id) or db.session.get(Channel, target_id)

        # Named by the profile the showing is actually scheduled with, which is the chosen
        # one when a profile was picked - not the channel's default (dev/docs/BUGS.md
        # 2026-09-29 @ 08:56).
        name = _suggested_name(ch, entry, filename_naming_for(cfg, profile), tags_by_name, tz)
        warnings = _pending_recording_warnings(
            target.id, group.id if group else None, start, stop, pending=pending)
        plan.update(
            name=name,
            member_name=target.name if group is not None else None,
            start_time=_iso_utc(start), stop_time=_iso_utc(stop),
            started=start < now,
            profile_name=profile.name if profile is not None else None,
            warnings=warnings,
            verdict=(BULK_HARD if 'connection_limit_warning' in warnings
                     else BULK_WARN if warnings else BULK_OK),
        )
        plan['_create'] = dict(
            name=name, url=normalize_url(target.stream_url, target.account, cfg),
            start_utc=start, stop_utc=stop, channel_id=target.id,
            group_id=group.id if group else None,
            profile_id=profile.id if profile is not None else None, entry=entry)
        pending.append({'channel_id': target.id, 'group_id': group.id if group else None,
                        'account_id': target.account_id, 'start': start, 'stop': stop,
                        'name': name, 'channel_name': group.name if group else target.name})
    return plans


def _iso_utc(dt):
    return dt.strftime('%Y-%m-%dT%H:%M:%S') if dt else None


def bulk_plan_payload(plan):
    return {k: v for k, v in plan.items() if not k.startswith('_')}


@recordings_bp.route('/api/recordings/bulk-preview', methods=['POST'])
def bulk_schedule_preview_json():
    """What bulk_schedule_json would create for these showings, and every warning each one
    would carry. Writes nothing."""
    items, profile_choice, error = _parse_bulk_schedule(request.get_json(silent=True) or {})
    if error:
        return jsonify({'error': error}), 400
    plans = _plan_bulk_schedule(items, profile_choice, load_config())
    return jsonify({'success': True, 'plans': [bulk_plan_payload(p) for p in plans]})


@recordings_bp.route('/api/recordings/bulk-schedule', methods=['POST'])
def bulk_schedule_json():
    """Schedule each showing as its own ordinary recording. Warnings do not stop a showing
    - the preview already showed them and the user pressed Schedule - so only a skip does.

    Each recording is created through _create_scheduled_recording(), one commit unit per
    recording, so a failure part way leaves the ones before it intact and is reported by
    name rather than failing the whole request (dev/changelog/1157)."""
    items, profile_choice, error = _parse_bulk_schedule(request.get_json(silent=True) or {})
    if error:
        return jsonify({'error': error}), 400
    cfg = load_config()
    plans = _plan_bulk_schedule(items, profile_choice, cfg)

    created, failed = [], []
    for plan in plans:
        args = plan.get('_create')
        if plan['verdict'] == BULK_SKIP or args is None:
            continue
        # Broad on purpose, and logged with its traceback: whatever stops one showing, the
        # ones already created stay created and this one is named in the response.
        try:
            rec = _create_scheduled_recording(cfg, **args)
        except Exception as exc:
            db.session.rollback()
            log.exception('Bulk schedule: could not create "%s" (showing %s)',
                          plan.get('name'), plan['epg_id'])
            failed.append({**bulk_plan_payload(plan), 'error': str(exc) or type(exc).__name__})
            continue
        created.append({**bulk_plan_payload(plan), 'recording_id': rec.id})

    skipped = [bulk_plan_payload(p) for p in plans if p['verdict'] == BULK_SKIP]
    log.info('Bulk schedule: %d created, %d skipped, %d failed',
             len(created), len(skipped), len(failed))
    return jsonify({'success': True, 'created': created, 'skipped': skipped,
                    'failed': failed})


@recordings_bp.route('/api/recordings/test-url', methods=['POST'])
def test_recording_url():
    """Quick, ephemeral connectivity test for a manually-entered stream URL.

    Connects, captures a few seconds of data, probes it with ffprobe, then
    disconnects. Nothing is persisted - used by the manual "Add Recording"
    modal on the TV Guide page to sanity-check a URL before scheduling.
    """
    data = request.get_json(silent=True) or {}
    url = (data.get('url') or '').strip()
    if not url:
        return jsonify({'error': 'URL is required.'}), 400

    import subprocess
    import tempfile
    from ..config import load_config
    from ..probe import parse_ffprobe
    from ..channel_tester import _extract_stderr_error

    cfg = load_config()
    connect_timeout = cfg.get('channel_testing', {}).get('connect_timeout_seconds', 15)
    capture_seconds = 5

    tmp_fd, tmp_path = tempfile.mkstemp(suffix='.ts', prefix='url_test_')
    os.close(tmp_fd)

    from ..proc_utils import build_capture_cmd
    # Paced for the same reason as the channel tester: capture_seconds is a content-time
    # bound and -re is what makes it a wall-clock one (dev/changelog/437).
    cmd = build_capture_cmd(cfg, url, tmp_path, capture_seconds, pace_realtime=True)

    try:
        try:
            result = subprocess.run(
                cmd, capture_output=True, text=True,
                timeout=connect_timeout + capture_seconds + 10,
            )
            stderr_lines = (result.stderr or '').splitlines()
        except subprocess.TimeoutExpired:
            return jsonify({'error': 'Connection timed out - server did not respond.'}), 502

        bytes_received = os.path.getsize(tmp_path) if os.path.exists(tmp_path) else 0
        if bytes_received == 0:
            err = _extract_stderr_error(stderr_lines)
            detail = f': {err}' if err else ''
            return jsonify({'error': f'No data received{detail}'}), 502

        probe = parse_ffprobe(tmp_path)
        if not probe.get('resolution') and not probe.get('audio_codec'):
            # Which of the two empty answers this is decides who gets blamed. Bytes did
            # arrive - the check above proved it - so with no ffprobe on the machine the
            # stream verdict below would be ChannelBin reporting its own missing binary as
            # a fault in the provider's feed (dev/changelog/911).
            from ..toolchain import ffprobe_missing
            if ffprobe_missing():
                return jsonify({'error': f'Connected and received {bytes_received:,} bytes, '
                                         'but ffprobe is not installed, so the stream could '
                                         'not be inspected. See Maintenance > External '
                                         'tools.'}), 503
            return jsonify({'error': 'Connected, but received no usable audio/video stream.'}), 502

        duration = probe.get('duration') or capture_seconds
        bitrate_kbps = (bytes_received * 8) / duration / 1000 if duration else None

        return jsonify({
            'success': True,
            'resolution': probe.get('resolution'),
            'fps': probe.get('fps'),
            'bitrate_kbps': round(bitrate_kbps, 0) if bitrate_kbps else None,
            'audio_codec': probe.get('audio_codec'),
            'audio_channels': probe.get('audio_channels'),
            'audio_sample_rate': probe.get('audio_sample_rate'),
            'duration_seconds': round(duration, 1),
        })
    finally:
        try:
            if os.path.exists(tmp_path):
                os.unlink(tmp_path)
        except OSError:
            pass


@recordings_bp.route('/recordings/<int:recording_id>/edit-json', methods=['POST'])
def edit_recording_json(recording_id):
    rec = db.session.get(Recording, recording_id)
    if rec is None:
        return jsonify({'error': 'Recording not found.'}), 404
    if rec.status != REC_STATUS_SCHEDULED:
        return jsonify({'error': 'Only SCHEDULED recordings can be edited.'}), 400

    name, url, start_utc, stop_utc, errors = _parse_recording_form(request.form)
    if errors:
        return jsonify({'error': ' '.join(errors)}), 400

    wanted_blocks = _parse_block_form(request.form)
    # The blocks this edit will leave in place, for the member stamp and the warning. The
    # recording's current ones are excluded from both reads by its own id.
    pending_blocks = (wanted_blocks if wanted_blocks is not None
                      else recording_blocks(recording_id))

    # A group recording's member was chosen for its old window; ask the same question
    # of the new one, exactly as create does (dev/changelog/1025).
    channel_id, member_reason = rec.channel_id, None
    if rec.group is not None and not rec.group.is_system:
        streak_threshold = load_config().get('channel_testing', {}).get(
            'failing_streak_threshold', DEFAULT_FAILING_STREAK_THRESHOLD)
        resolved, reason = _resolve_group_member(
            rec.group, rec.channel_id, start_utc, stop_utc, streak_threshold,
            exclude_recording_id=recording_id, pending_blocks=pending_blocks)
        if resolved is not None and resolved != rec.channel_id:
            channel_id, member_reason = resolved, reason

    force = request.form.get('force') == '1'
    if not force:
        warnings = _pending_recording_warnings(
            channel_id, rec.group_id, start_utc, stop_utc,
            exclude_recording_id=recording_id, pending_blocks=pending_blocks)
        if warnings:
            return jsonify({'success': False, **warnings})

    profile_id_raw = request.form.get('profile_id', '').strip()
    profile_id = int(profile_id_raw) if profile_id_raw.isdigit() else None

    if wanted_blocks is not None and set_recording_blocks(recording_id, wanted_blocks):
        from ..account_blocks import rearm_waiting_starts
        rearm_waiting_starts()

    _apply_edit_and_reschedule(
        recording_id, name, url, start_utc, stop_utc, profile_id=profile_id,
        channel_id=channel_id if member_reason is not None else _UNSET,
        member_reason=member_reason)

    return jsonify({'success': True})


@recordings_bp.route('/api/account-blocks/options')
def account_block_options():
    """What the record modal's "Block account use during this recording" picker offers: every
    account, its connection limit (a slot count is only asked for above one), and - with
    `recording_id` - which of them that recording already blocks."""
    from ..account_blocks import account_limits
    rid = request.args.get('recording_id', '').strip()
    current = recording_blocks(int(rid)) if rid.isdigit() else {}
    accounts = Account.query.order_by(Account.name).all()
    limits = account_limits([a.id for a in accounts])
    return jsonify({'success': True, 'accounts': [{
        'id': a.id,
        'name': a.name,
        'color': a.color,
        'limit': limits.get(a.id, 1),
        'blocked': a.id in current,
        'slots': current.get(a.id),
    } for a in accounts]})


# How long a route waits for the recording's watchdog to answer a move request. The
# watchdog polls every watchdog.poll_interval_seconds (5 by default) and may be inside a
# restart delay; a request it has not answered by then is still on the state and is
# carried out at its next poll - the response says so rather than guessing.
_MOVE_WAIT_SECONDS = 12


def _parse_blocks(items):
    """The picker's payload as {account_id: slots}, or (None, error message)."""
    wanted = {}
    for item in items or []:
        try:
            aid = int(item.get('account_id'))
            slots = item.get('slots')
            slots = None if slots in (None, '') else int(slots)
        except (AttributeError, TypeError, ValueError):
            return None, 'Each block needs an account_id and optional slots.'
        if slots is not None and slots < 1:
            return None, 'Slots must be at least 1.'
        wanted[aid] = slots
    return wanted, None


def _latest_event_detail(recording_id):
    ev = (RecordingEvent.query.filter_by(recording_id=recording_id)
          .order_by(RecordingEvent.id.desc()).first())
    return ev.detail if ev is not None else ''


def _ask_watchdog(rec, state, request_):
    """Leave `request_` for the recording's watchdog and wait for its answer. Returns the
    JSON fields that say what happened: `moved`, `from`, `to` and `message`, the message
    being the recording's own newest event so the user reads the same words the recording
    keeps. `moved` is None when the watchdog had not answered within the wait."""
    from_name = rec.channel.name if rec.channel else None
    state.move_request = request_
    done = request_.done.wait(timeout=_MOVE_WAIT_SECONDS)
    db.session.expire_all()
    rec = db.session.get(Recording, rec.id)
    if not done:
        return {'moved': None, 'from': from_name, 'to': None,
                'message': ("Asked. The recording's watchdog carries it out at its next check; "
                            "the event log says what it did.")}
    return {'moved': request_.moved, 'from': from_name,
            'to': rec.channel.name if request_.moved and rec.channel else None,
            'message': _latest_event_detail(rec.id)}


@recordings_bp.route('/api/recordings/<int:recording_id>/account-blocks', methods=['POST'])
def set_recording_account_blocks(recording_id):
    """Replace the accounts a recording blocks, for as long as it can still open a stream -
    the in-progress modal's picker and the recording page's Block accounts dialog. Body:
    {"blocks": [{"account_id": 1, "slots": null}]}. A live recording on an account this
    blocks moves off it at once: the watchdog is asked to run its block check now rather
    than at its next 15 s cadence, and the answer (`moved`, `from`, `to`, `message`) rides
    on the response so the page can say what the block did (dev/changelog/1254)."""
    rec = db.session.get(Recording, recording_id)
    if rec is None:
        return jsonify({'error': 'Recording not found.'}), 404
    if rec.status not in (REC_STATUS_SCHEDULED,) + tuple(WINDOW_OPEN_STATUSES):
        return jsonify({'error': 'Only a scheduled or running recording can block accounts.'}), 400
    data = request.get_json(silent=True) or {}
    wanted, problem = _parse_blocks(data.get('blocks'))
    if problem:
        return jsonify({'error': problem}), 400
    if not set_recording_blocks(recording_id, wanted):
        return jsonify({'success': True, 'message': 'Nothing to change.'})
    rearm_waiting_starts()
    state = get_state(recording_id)
    answer = {}
    if state is not None and rec.status == REC_STATUS_IN_PROGRESS and rec.group_id is not None:
        answer = _ask_watchdog(rec, state, MoveRequest())
    return jsonify({'success': True, 'message': 'Blocked accounts updated.', **answer})


@recordings_bp.route('/api/recordings/<int:recording_id>/block-preview')
def recording_block_preview(recording_id):
    """What the Block accounts dialog says before a block is saved: whether the blocks in
    `blocks` (the picker's payload, JSON in the query string) take the account the
    recording is on, and if so where the recording would move (dev/changelog/1254)."""
    rec = db.session.get(Recording, recording_id)
    if rec is None:
        return jsonify({'error': 'Recording not found.'}), 404
    try:
        items = json.loads(request.args.get('blocks') or '[]')
    except ValueError:
        return jsonify({'error': 'blocks must be JSON.'}), 400
    wanted, problem = _parse_blocks(items if isinstance(items, list) else None)
    if problem:
        return jsonify({'error': problem}), 400
    if rec.status != REC_STATUS_IN_PROGRESS or rec.group_id is None or rec.channel is None:
        return jsonify({'success': True, 'affects': False})
    preview = block_move_preview(recording_id, wanted)
    if preview is None:
        return jsonify({'success': True, 'affects': False})
    to = preview['to']
    return jsonify({
        'success': True, 'affects': True,
        'current': {'name': rec.channel.name, 'account': rec.channel.account.name},
        'to': ({'name': to.name, 'account': to.account.name, 'score': effective_score(to)}
               if to is not None else None),
        'held': preview['held'],
    })


@recordings_bp.route('/api/recordings/<int:recording_id>/switch-options')
def recording_switch_options(recording_id):
    """What the Switch channel dialog lists: every channel the group records from, with
    whether it can be picked right now and why (recorder.switch_choices)."""
    rec = db.session.get(Recording, recording_id)
    if rec is None:
        return jsonify({'error': 'Recording not found.'}), 404
    if rec.status != REC_STATUS_IN_PROGRESS or rec.group_id is None:
        return jsonify({'error': 'Only a group recording capturing right now can switch channels.'}), 400
    if get_state(recording_id) is None:
        return jsonify({'error': 'This recording is not being captured by this process.'}), 400
    since = (RecordingSegment.query
             .filter_by(recording_id=recording_id, channel_id=rec.channel_id)
             .order_by(RecordingSegment.segment_number.desc()).first())
    return jsonify({
        'success': True,
        'group': rec.group.name,
        'current': ({'channel_id': rec.channel.id, 'name': rec.channel.name,
                     'account': rec.channel.account.name} if rec.channel else None),
        'since': fmt_utils.iso_or_none(since.started_at) if since is not None else None,
        'choices': [{
            'channel_id': c.channel.id,
            'name': c.channel.name,
            'account': {'id': c.channel.account.id, 'name': c.channel.account.name,
                        'color': c.channel.account.color},
            # The chip's whole number, as every health chip shows it; None = never scored.
            'score': None if c.channel.health_score is None else effective_score(c.channel),
            'ok': c.ok, 'why': c.why, 'warn': c.warn,
        } for c in switch_choices(recording_id)],
    })


# What the Switch channel dialog may ask of the channel it leaves. `off` goes through the
# one participation writer (channel_groups.set_participation - this IS the explicit user
# action), `block` through the one block writer; `keep` writes nothing.
SWITCH_LEAVE_CHOICES = ('keep', 'off', 'block')


@recordings_bp.route('/api/recordings/<int:recording_id>/switch-channel', methods=['POST'])
def switch_recording_channel(recording_id):
    """Move a running group recording to the member the user picked, now. Body:
    {"channel_id": 123, "leave": "keep" | "off" | "block"}.

    The route validates the pick against switch_choices() - the same reading the dialog
    drew - and then only records intent: the move itself is made by the recording's
    watchdog (app/watchdog.py::_check_move_request), the one thread that stops an ffmpeg
    and launches the next segment, so a switch cannot race the watchdog's own failover or
    block check. The leave-behind answer is applied here, after the watchdog reports the
    move, so a switch that did not happen changes nothing else (dev/changelog/1254)."""
    from ..channel_groups import set_participation
    rec = db.session.get(Recording, recording_id)
    if rec is None:
        return jsonify({'error': 'Recording not found.'}), 404
    if rec.status != REC_STATUS_IN_PROGRESS or rec.group_id is None:
        return jsonify({'error': 'Only a group recording capturing right now can switch channels.'}), 400
    state = get_state(recording_id)
    if state is None:
        return jsonify({'error': 'This recording is not being captured by this process.'}), 400
    data = request.get_json(silent=True) or {}
    try:
        channel_id = int(data.get('channel_id'))
    except (TypeError, ValueError):
        return jsonify({'error': 'Pick the channel to switch to.'}), 400
    leave = data.get('leave') or 'keep'
    if leave not in SWITCH_LEAVE_CHOICES:
        return jsonify({'error': 'leave must be keep, off or block.'}), 400
    choice = next((c for c in switch_choices(recording_id) if c.channel.id == channel_id), None)
    if choice is None:
        return jsonify({'error': 'That channel is not one this group records from.'}), 400
    if not choice.ok:
        return jsonify({'error': f'Not switched to "{choice.channel.name}": {choice.why}'}), 400
    old = rec.channel
    old_id, old_account_id = (old.id, old.account_id) if old else (None, None)

    answer = _ask_watchdog(rec, state, MoveRequest(target_channel_id=channel_id))
    left = ''
    if answer['moved'] and old_id is not None:
        # The move landed, so the target is now the member recording - and it has Recording
        # on (switch_choices offers nothing else), so turning the old one off cannot empty
        # the group's recording-enabled set and needs no guide-invariant gate.
        if leave == 'off':
            @retry_on_locked()
            def _turn_off_and_commit():
                from ..database import ChannelGroupMember
                membership = ChannelGroupMember.query.filter_by(
                    group_id=rec.group_id, channel_id=old_id).first()
                if membership is not None and set_participation(
                        membership, 'recording_enabled', False, surface='recording_page'):
                    db.session.commit()
                    return True
                return False

            if _turn_off_and_commit():
                left = (f'Recording is now off for "{old.name}" in "{rec.group.name}" - turn it '
                        f'back on from the group page.')
        elif leave == 'block':
            blocks = dict(recording_blocks(recording_id))
            blocks[old_account_id] = None
            if set_recording_blocks(recording_id, blocks):
                rearm_waiting_starts()
            left = f'"{old.account.name}" is blocked for the rest of this recording.'
    return jsonify({'success': True, 'left': left, **answer})


@recordings_bp.route('/recordings/<int:recording_id>/delete-json', methods=['POST'])
def delete_recording_json(recording_id):
    rec = db.session.get(Recording, recording_id)
    if rec is None:
        return jsonify({'error': 'Recording not found.'}), 404

    if rec.status in RESTART_BLOCKING_STATUSES:
        return jsonify({'error': 'Cannot delete an active recording. Cancel it first.'}), 400

    name = rec.name
    remove_files = _wants_file_deletion(request.get_json(silent=True) or {})
    paths = (recording_disk_paths(recording_id) if remove_files
             else recording_image_paths(recording_id))
    _unschedule_and_delete_row(recording_id)
    removed = delete_files(paths)
    _log_recording_deleted(recording_id, name, remove_files, removed)
    return jsonify({'success': True, 'files_deleted': removed, 'kept_files': not remove_files})


@recordings_bp.route('/recordings/<int:recording_id>/retry-concat', methods=['POST'])
def retry_concat(recording_id):
    from flask import current_app
    from ..concatenator import do_concatenation, is_concat_active
    from ..database import RecordingSegment

    rec = db.session.get(Recording, recording_id)
    if rec is None:
        flash('Recording not found.', 'error')
        return redirect(url_for('recordings.index'))

    if rec.status not in (REC_STATUS_FAILED, REC_STATUS_CONCATENATING):
        flash('Retrying the join is only available for FAILED or stuck JOINING recordings.', 'error')
        return redirect(url_for('recordings.recording_detail', recording_id=recording_id))

    # CONCATENATING is accepted above to rescue a row stranded by a crash, but the same
    # status covers a live chain that is merely queued (waiting on serialize_concat). Only
    # the registry can tell them apart; without this a retry starts a second ffmpeg on the
    # same output_path and both delete the same segments (dev/changelog/668).
    if is_concat_active(recording_id):
        flash('The join is already running for this recording - it may be queued behind '
              'another recording or join. Retry is not needed.', 'error')
        return redirect(url_for('recordings.recording_detail', recording_id=recording_id))

    segments = RecordingSegment.query.filter_by(recording_id=recording_id).all()
    valid = [s for s in segments
             if s.file_path and os.path.exists(s.file_path) and os.path.getsize(s.file_path) > 0]
    if not valid:
        flash('No segment files found on disk - cannot retry the join.', 'error')
        return redirect(url_for('recordings.recording_detail', recording_id=recording_id))

    # Commit in its own closure; the thread launch stays outside so a lock-retry
    # can never spawn a second concat thread.
    @retry_on_locked()
    def _mark_concatenating_and_commit():
        r = db.session.get(Recording, recording_id)
        r.status = REC_STATUS_CONCATENATING
        # Holds a value only while the row is FAILED; the next terminal writer names its own.
        r.failure_reason = None
        db.session.commit()

    _mark_concatenating_and_commit()

    app_obj = current_app._get_current_object()
    threading.Thread(
        target=do_concatenation,
        args=(app_obj, recording_id),
        kwargs={'reason': 'Manual retry of the join'},
        daemon=True,
    ).start()

    flash(f'Join retry started for "{rec.name}".', 'success')
    return redirect(url_for('recordings.recording_detail', recording_id=recording_id))


@recordings_bp.route('/recordings/<int:recording_id>/retry-convert', methods=['POST'])
def retry_convert(recording_id):
    from flask import current_app
    from ..concatenator import (claim_postprocess, release_postprocess,
                                run_postprocess_claimed)
    from ..postprocessor import is_conversion_active

    rec = db.session.get(Recording, recording_id)
    if rec is None:
        flash('Recording not found.', 'error')
        return redirect(url_for('recordings.index'))

    # FAILED (retry a given-up conversion), CONVERTING or ANALYZING (a row stranded by a
    # restart/crash that nothing is working), or ABORTED (post-processing the user
    # cancelled). Refuse if anything is genuinely live for this id so two chains don't fight
    # over the same output. Also a COMPLETED recording whose converted file is missing video
    # its source has: the source was kept for exactly this (dev/changelog/1200).
    gaps_source = None
    if rec.status == REC_STATUS_COMPLETED and (rec.converted_gap_seconds or 0) > 0:
        from ..postprocessor import kept_source_path
        gaps_source = kept_source_path(recording_id)
    if gaps_source is None and rec.status not in (
            REC_STATUS_FAILED, REC_STATUS_ANALYZING, REC_STATUS_CONVERTING, REC_STATUS_ABORTED):
        flash('Conversion retry is only available for failed, cancelled, or stalled recordings, '
              'or a converted file with gaps whose source was kept.', 'error')
        return redirect(url_for('recordings.recording_detail', recording_id=recording_id))

    # Two registries, because they answer at different times: is_conversion_active() only
    # holds once ffmpeg has been spawned, which is the back half of the chain. The analysis
    # phase ahead of it spawns nothing, so a live ANALYZING row is invisible to it and the
    # live-chain claim is what covers that window. The claim is TAKEN here, in the request,
    # not merely read: a check here followed by a claim in the worker thread let two
    # requests both pass the check before either thread claimed, so both reset the attempt
    # budget of the run already going and both flashed "started" (dev/changelog/1242).
    # Everything from here to the thread start releases it on the way out.
    if is_conversion_active(recording_id):
        flash('Post-processing is already running for this recording.', 'error')
        return redirect(url_for('recordings.recording_detail', recording_id=recording_id))
    if claim_postprocess(recording_id) is not None:
        flash('Post-processing is already running for this recording.', 'error')
        return redirect(url_for('recordings.recording_detail', recording_id=recording_id))

    started = False
    try:
        damaged_output = None
        if gaps_source is not None:
            # The file with the gaps goes first. The new conversion is written under the
            # same name, and a move on completion would otherwise file it beside the old one
            # as a renamed copy. Refused rather than half-done when it cannot be deleted.
            ts_path = gaps_source
            damaged_output = rec.output_path
            if damaged_output and damaged_output != ts_path and os.path.exists(damaged_output):
                try:
                    os.unlink(damaged_output)
                except OSError as exc:
                    log.warning('Recording %d: could not delete the converted file with gaps '
                                '%s: %s', recording_id, damaged_output, exc)
                    flash(f'Could not delete the converted file with gaps ({exc}) - conversion '
                          f'not retried.', 'error')
                    return redirect(url_for('recordings.recording_detail',
                                            recording_id=recording_id))
        else:
            ts_path = rec.output_path
            if not ts_path or not ts_path.endswith('.ts') or not os.path.exists(ts_path):
                flash('No joined .ts source file found on disk - cannot retry conversion.',
                      'error')
                return redirect(url_for('recordings.recording_detail', recording_id=recording_id))

        # ANALYZING, not CONVERTING: do_postprocess re-reads the whole .ts before it spawns
        # any ffmpeg, so a row marked CONVERTING here would be back to naming a phase the app
        # is not in yet - the defect this status was added to end (dev/changelog/867).
        # do_postprocess sets it again itself; this write is what the redirect below renders.
        #
        # A manual Retry is an operator action that buys a fresh restart budget: reset the
        # attempt counter to 0.
        @retry_on_locked()
        def _mark_analyzing_and_commit():
            r = db.session.get(Recording, recording_id)
            r.status = REC_STATUS_ANALYZING
            r.conversion_attempts = 0
            # Both hold a value only while the row is terminal, and this one accepts an
            # ABORTED row as well as a FAILED one; the next terminal writer names its own.
            r.failure_reason = None
            r.cancel_reason = None
            if damaged_output is not None:
                # The row stops naming a file that is gone, and stops describing its gaps.
                r.output_path = ts_path
                r.converted_gap_seconds = None
                add_recording_event(
                    recording_id, DIAGNOSTICS,
                    detail=f'Deleted the converted file with gaps '
                           f'({os.path.basename(damaged_output)}) to convert again from the '
                           f'kept source {os.path.basename(ts_path)}.',
                    extra={'kind': 'converted_file_discarded', 'deleted_path': damaged_output})
            db.session.commit()

        _mark_analyzing_and_commit()

        app_obj = current_app._get_current_object()
        threading.Thread(
            target=run_postprocess_claimed,
            args=(app_obj, recording_id, ts_path),
            kwargs={'claimed': True},
            daemon=True,
        ).start()
        started = True
    finally:
        if not started:
            release_postprocess(recording_id)

    flash(f'Conversion retry started for "{rec.name}".', 'success')
    return redirect(url_for('recordings.recording_detail', recording_id=recording_id))


def _cancel_conversion(recording_id, rec):
    """Stop a CONVERTING recording's conversion and return the message to surface.

    The source .ts is kept so Retry conversion still works; the partial output is deleted
    by the runner (or here, when nothing is live). The one conversion-cancel path - the
    two generic cancel routes delegate here rather than to abort_recording, which only
    tears down recorder._active and so never reaches a conversion ffmpeg
    (dev/changelog/667).
    """
    from ..postprocessor import (request_cancel_conversion, set_postprocess_wait,
                                 set_conversion_parts, discard_conversion_parts)
    from ..concatenator import is_concat_active
    from ..database import RecordingEvent, CONVERSION_CANCELLED

    # A live conversion: signal its loop to abort (it sets the terminal status + deletes the
    # partial output). A stranded CONVERTING row with no live ffmpeg: mark it here, since no
    # loop exists to do it. "Live" is the chain's claim, not the ffmpeg registry alone: a
    # chain between two attempts - re-muxing a killed part, writing its restart event, or
    # ahead of its first spawn - has no ffmpeg registered, and reading that as stranded
    # deleted the parts and marked the row ABORTED under a loop that then spawned the next
    # attempt anyway (dev/changelog/1242).
    if request_cancel_conversion(recording_id, chain_active=is_concat_active(recording_id)):
        return f'Cancelling conversion for "{rec.name}".'

    # A stranded CONVERTING row's output_path is still the .ts; the partial converted file
    # sits next to it as <stem>.mp4/.mkv. Delete whichever partial exists (unreadable anyway).
    ts_path = rec.output_path or ''
    if ts_path.endswith('.ts'):
        stem = os.path.splitext(ts_path)[0]
        for ext in ('.mp4', '.mkv'):
            partial = stem + ext
            if os.path.exists(partial):
                try:
                    os.unlink(partial)
                except OSError as exc:
                    log.warning('cancel_convert: could not delete partial output %s: %s', partial, exc)
            # And the partly-encoded parts a resumable conversion checkpoints into. The
            # live-conversion branch above deletes its own; this branch is the stranded row
            # with no loop behind it, so nothing else ever will - and they are the size of
            # the recording (dev/changelog/955).
            discard_conversion_parts(partial)

    @retry_on_locked()
    def _mark_cancelled():
        r = db.session.get(Recording, recording_id)
        r.status = REC_STATUS_ABORTED
        r.cancel_reason = CANCEL_DURING_CONVERSION
        r.completed_at = datetime.utcnow()
        # This branch is the one with no live chain behind it, so nothing else will ever
        # clear a wait the stranded row is carrying - and a finished recording that
        # still claims to be waiting on another one is a lie with no expiry
        # (dev/changelog/952).
        set_postprocess_wait(r, None)
        # Same reasoning for the conversion checkpoint: the parts it describes have just been
        # deleted, and a checkpoint naming files that are gone would send a later Retry to a
        # splice point with nothing behind it (dev/changelog/955).
        set_conversion_parts(r)
        db.session.add(RecordingEvent(
            recording_id=recording_id,
            event_type=CONVERSION_CANCELLED,
            detail='Conversion cancelled by user - source .ts kept for retry.',
        ))
        db.session.commit()

    _mark_cancelled()
    return f'Conversion cancelled for "{rec.name}".'


@recordings_bp.route('/recordings/<int:recording_id>/cancel-convert', methods=['POST'])
def cancel_convert(recording_id):
    """Stop a running (or stranded) conversion and mark the recording CANCELLED (ABORTED
    status)."""
    rec = db.session.get(Recording, recording_id)
    if rec is None:
        flash('Recording not found.', 'error')
        return redirect(url_for('recordings.index'))

    if rec.status != REC_STATUS_CONVERTING:
        flash('Only a recording that is converting can have its conversion cancelled.', 'error')
        return redirect(url_for('recordings.recording_detail', recording_id=recording_id))

    flash(_cancel_conversion(recording_id, rec), 'success')
    return redirect(url_for('recordings.recording_detail', recording_id=recording_id))


@recordings_bp.app_template_filter('local_date')
def local_date_filter(dt):
    return format_local(dt, 'date')


@recordings_bp.app_template_filter('local_time')
def local_time_filter(dt):
    return format_local(dt, 'datetime')


@recordings_bp.app_template_filter('local_time_sec')
def local_time_sec_filter(dt):
    return format_local(dt, 'datetime_sec')


@recordings_bp.app_template_filter('local_time_only')
def local_time_only_filter(dt):
    return format_local(dt, 'time')


@recordings_bp.app_template_filter('duration')
def duration_filter(seconds, with_seconds=False):
    if seconds is None:
        return '?'
    return fmt_utils.fmt_duration(seconds, with_seconds=with_seconds)


@recordings_bp.app_template_filter('filesize')
def filesize_filter(n):
    from ..fmt_utils import fmt_bytes
    return fmt_bytes(n)


@recordings_bp.app_template_filter('rec_status_label')
def rec_status_label_filter(status, waiting=False):
    """What a human calls this Recording.status - JOINING for CONCATENATING, CANCELLED for
    ABORTED, and so on. For any template that has a raw status rather than a row context
    built by _index_row (the Dashboard's rows, the channel page's observations table).
    Pure, so it is safe inside a `{% for %}` (CLAUDE.md, no hidden I/O)."""
    return fmt_utils.rec_status_label(status, waiting=bool(waiting))


@recordings_bp.app_template_filter('rec_status_badge')
def rec_status_badge_filter(status):
    """The `.b-*` badge class for a raw Recording.status, from the same table as
    `rec_status_label` - so a page holding only the stored value colors the badge the way
    the Recordings list does (dev/changelog/1212). Pure."""
    return fmt_utils.rec_status_display(status)[2]


@recordings_bp.app_template_filter('mask_creds')
def mask_creds_filter(url, raw_url=None, account=None):
    if not url:
        return url
    # If a raw (pre-normalization) URL + its account are given, mask credentials on the
    # raw form (which reliably still has the full, un-normalized path) and re-run it
    # through the same normalize_url() the app already uses, so the masked result matches
    # the structure of `url` even when normalization has stripped the /live/ anchor.
    if raw_url and account:
        masked_raw = _mask_creds_str(raw_url)
        if masked_raw != raw_url:
            from ..accounts import normalize_url
            return normalize_url(masked_raw, account)
    return _mask_creds_str(url)


# duplicated from tz_utils.relative and _humanize_secs above - different display register:
# this pair (time_ago/time_until) is exact combined-unit wording ('1d 4h ago', 'in 3d 2h')
# for account sync timestamps, where precision is the point (dev/changelog/623)
@recordings_bp.app_template_filter('time_ago')
def time_ago_filter(dt):
    if dt is None:
        return '?'
    delta = datetime.utcnow() - dt
    s = int(delta.total_seconds())
    if s < 60:
        return f'{s}s ago'
    m, s = divmod(s, 60)
    if m < 60:
        return f'{m}m {s}s ago'
    h, m = divmod(m, 60)
    if h < 24:
        return f'{h}h {m}m ago'
    d, h = divmod(h, 24)
    return f'{d}d {h}h ago'


@recordings_bp.app_template_filter('time_until')
def time_until_filter(dt, compact=False):
    """How long until a scheduled time - and, when it has already passed, how far overdue.

    `compact` is for a narrow number column, where "in under a minute" was cut to "in under
    a m..." (dev/changelog/1291); it reads "in <1m" beside "in 11h 47m" instead.

    A past timestamp used to render as the literal word "now", which is a claim rather than
    a reading: one real account's next sync said "now" indefinitely because nothing had
    rescheduled it (dev/docs/BUGS.md 2026-08-04). Naming it as overdue is DESIGN.md §5's
    app-wide rule and applies wherever this filter is used."""
    if dt is None:
        return '?'
    delta = dt - datetime.utcnow()
    s = int(delta.total_seconds())
    if s <= 0:
        return _overdue_by(-s)
    m, s = divmod(s, 60)
    h, m = divmod(m, 60)
    d, h = divmod(h, 24)
    if d:
        return f'in {d}d {h}h'
    if h:
        return f'in {h}h {m}m'
    if m:
        return f'in {m}m {s}s'
    # Not seconds: every page that shows this is server-rendered and redraws at most once a
    # minute, so "in 59s" sat still for a minute and read as a broken countdown
    # (dev/changelog/1272).
    return 'in <1m' if compact else 'in under a minute'


def _overdue_by(seconds: int) -> str:
    """"overdue by 8h" for a scheduled time that has already passed.

    The first minute is spelled "overdue" alone - "overdue by 3s" reads as a fault when it
    is really just the clock ticking past the mark."""
    m, _s = divmod(max(0, seconds), 60)
    if not m:
        return 'overdue'
    h, m = divmod(m, 60)
    d, h = divmod(h, 24)
    if d:
        return f'overdue by {d}d {h}h'
    if h:
        return f'overdue by {h}h'
    return f'overdue by {m}m'


@recordings_bp.app_template_filter('channel_snapshot_data')
def channel_snapshot_data_filter(rec):
    """Return the channel_health_snapshot as a parsed dict, or {}."""
    try:
        return json.loads(getattr(rec, 'channel_health_snapshot', None) or '{}') or {}
    except (ValueError, TypeError):  # malformed snapshot JSON - render as empty
        return {}


# Labels that a mechanical de-underscoring gets wrong or leaves ambiguous. Everything else
# derives from the key, deliberately: DIAGNOSTICS is a generic carrier whose measurement is
# named in extra_data['kind'], so a future kind renders without touching this map or the
# template (dev/changelog/334).
_DIAG_LABELS = {
    'fps': 'FPS',
    'gap_threshold': 'Gap threshold (s)',
    'backward_count': 'Backward DTS steps',
}


def _diag_label(key):
    if key in _DIAG_LABELS:
        return _DIAG_LABELS[key]
    if key.endswith('_seconds'):
        return key[:-len('_seconds')].replace('_', ' ').capitalize() + ' (s)'
    return key.replace('_', ' ').capitalize()


@recordings_bp.app_template_filter('diag_view')
def diag_view_filter(raw):
    """Render a RecordingEvent.extra_data JSON string as {'title', 'rows'}, or None.

    Pure function of its argument - it runs per row inside the event-log {% for %} and may
    not touch disk, config or the ORM. Malformed or non-dict JSON yields None rather than
    raising, so a bad blob degrades to "no disclosure" instead of a 500.

    'kind' is dropped from the rows because it is what the title says.
    """
    # Decoded here rather than via the `parse_json` filter - duplicated from
    # app/routes/channels.py::parse_json_filter because the semantics differ: that one
    # collapses malformed JSON to {} (indistinguishable from "no extra data") and can
    # return a non-dict, and both distinctions are load-bearing here.
    try:
        data = json.loads(raw) if raw else None
    except (ValueError, TypeError):  # malformed extra_data - render no disclosure
        return None
    if not isinstance(data, dict):
        return None

    rows = []
    for key, value in data.items():
        if key == 'kind':
            continue
        if value is None:
            shown = '-'
        elif isinstance(value, bool):
            shown = 'yes' if value else 'no'
        else:
            shown = value
        rows.append((_diag_label(key), shown))
    if not rows:
        return None

    kind = data.get('kind')
    title = f"{kind.replace('_', ' ').capitalize()} details" if kind else 'Details'
    return {'title': title, 'rows': rows}


@recordings_bp.app_template_filter('channel_snapshot_stats')
def channel_snapshot_stats_filter(rec):
    """Return 'WxH · FPSfps' from the channel_health_snapshot JSON, or '-'."""
    snap = getattr(rec, 'channel_health_snapshot', None)
    if not snap:
        return '-'
    try:
        d = json.loads(snap)
        parts = []
        if d.get('resolution'):
            parts.append(d['resolution'])
        if d.get('fps'):
            parts.append(f"{round(float(d['fps']), 1)}fps")
        return ' · '.join(parts) if parts else '-'
    except (ValueError, TypeError):  # malformed snapshot JSON - render placeholder
        return '-'
