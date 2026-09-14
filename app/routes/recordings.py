import json
import logging
import os
import threading
import time
import uuid
from datetime import datetime

from flask import (
    Blueprint, render_template, request, redirect, url_for, flash, jsonify,
    send_file, abort,
)

from .. import db
from ..database import (
    Recording, Channel, ChannelGroup, Account, EPGEntry, RecordingEvent,
    RECORDING_CREATED_AFTER_EVENT_START, RECORDING_EDITED, RECORDING_STOP_TIME_ADJUSTED,
    RECORDING_REPLACED_OTHER, RESTART_BLOCKING_STATUSES, RECORDING_ABORTED,
    DIAGNOSTICS, add_recording_event, detach_recording_references,
    group_event_channel_links,
    REC_STATUS_SCHEDULED, REC_STATUS_IN_PROGRESS, REC_STATUS_PAUSED, REC_STATUS_RETRYING,
    REC_STATUS_CONCATENATING, REC_STATUS_ANALYZING, REC_STATUS_CONVERTING,
    REC_STATUS_COMPLETED, REC_STATUS_FAILED, REC_STATUS_ABORTED,
)
from ..channel_groups import (pick_best_member, recording_members,
                              format_eligible_members,
                              DEFAULT_FAILING_STREAK_THRESHOLD)
from ..health_score import dismiss_recording_failing_alerts, evaluate_and_alert_recording
from ..db_utils import retry_on_locked
from ..scheduler import schedule_recording, unschedule_recording
from ..recorder import (
    abort_recording, get_state, stop_recording, pause_recording, resume_recording,
    get_live_segment_path, recording_disk_paths, delete_files, end_slot_wait,
)
from ..tz_utils import parse_local_to_utc, local_input_value, format_local
from ..accounts import normalize_url_loose
from ..config import load_config, resolve_ffmpeg_path
from ..logo_cache import resolve_logo_url
from ..screenshot import capture_screenshot
from ..url_utils import mask_creds as _mask_creds_str
from .. import fmt_utils

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
                               profile_id=_UNSET):
    """Persist an edit to a SCHEDULED recording, then re-register its jobs.

    Only the mutate+commit is retried; the scheduler side effects must run
    exactly once, after the write has durably succeeded.

    The scheduled_* pair is re-baselined here, not preserved. It is an audit trail
    of *execution* drift (started late / stopped early / aborted), and the only
    caller is SCHEDULED-only, so nothing has run yet and there is no drift to
    record - an edit is a new plan, not a deviation from the old one. The
    RECORDING_EDITED event below is what preserves the previous times
    (dev/changelog/471).
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
        db.session.commit()

    _apply_edit_and_commit()

    unschedule_recording(recording_id)
    from flask import current_app
    schedule_recording(current_app._get_current_object(), recording_id, start_utc, stop_utc)


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
    # 2. Loose match: strip /live/ and extensions from both sides
    norm = normalize_url_loose(url)
    for channel in Channel.query.all():
        if normalize_url_loose(channel.stream_url or '') == norm:
            return channel
        if normalize_url_loose(channel.raw_stream_url or '') == norm:
            return channel
    return None


def _check_account_connection_limit(account_id, new_channel_id, start_utc, stop_utc,
                                     exclude_recording_id=None):
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
    distinct_channels = {r.channel_id for r in other_channel_recs}
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
        ]
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
                                exclude_recording_id=None):
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
    """
    if channel_id is None:
        return {}
    warnings = {}

    overlaps = _overlap_conflicts(channel_id, group_id, start_utc, stop_utc, exclude_recording_id)
    if overlaps:
        n = len(overlaps)
        warnings['overlap_warning'] = {
            'message': f'Overlaps {n} other scheduled recording{"s" if n != 1 else ""}.',
            'conflicts': _serialize_overlap_conflicts(overlaps),
        }

    ch = db.session.get(Channel, channel_id)
    if ch is not None:
        limit_conflict = _check_account_connection_limit(
            ch.account_id, channel_id, start_utc, stop_utc,
            exclude_recording_id=exclude_recording_id)
        if limit_conflict:
            warnings['connection_limit_warning'] = limit_conflict

    return warnings


def _members_committed_in_window(members, start_utc, stop_utc, exclude_recording_id=None):
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

    blocked = set()
    for ch in members:
        limit = limits.get(ch.account_id)
        if limit is None:
            continue
        others = committed_channels.get(ch.account_id, set()) - {ch.id}
        if len(others) >= limit:
            blocked.add(ch.id)
    return blocked


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
    for s in reversed(rec.segments):
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
        if s.probe_video_codec:
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
        rows.append(_profile_row('Efficiency', f'{bpp:.4f}',
                                 tip=fmt_utils.EFFICIENCY_TIP + '\n' + _OUTPUT_NOTE))
    return {'rows': rows, 'source': src, 'coded': coded, 'audio_note': audio_note}


def _audio_profile_note(rec):
    """One tooltip line for the audio detail the Audio row's 'AAC 2.0' label drops.

    Always output-derived (there are no probe_* counterparts for these three), so
    it says 'Output audio' rather than silently reading as the capture's.
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


# Every Recording.status, explicitly (no fallthrough rendering - an unknown
# status still renders visibly via the .get default, never lands in a real
# state's branch): (section, row edge class, badge class, badge label, pulse)
_STATUS_ROW = {
    REC_STATUS_SCHEDULED:     ('sched', 'st-sched',  'b-sched',  'SCHEDULED',     False),
    REC_STATUS_IN_PROGRESS:   ('live',  'st-live',   'b-live',   'RECORDING',     True),
    REC_STATUS_PAUSED:        ('live',  'st-paused', 'b-paused', 'PAUSED',        False),
    REC_STATUS_RETRYING:      ('live',  'st-retry',  'b-retry',  'RETRYING',      False),
    # Three post-capture phases, each naming its own. CONCATENATING used to read
    # "PROCESSING", which was tolerable while it was the only one; next to a second phase
    # that is also processing it says nothing (dev/changelog/867).
    REC_STATUS_CONCATENATING: ('live',  'st-concat', 'b-concat', 'JOINING',       True),
    REC_STATUS_ANALYZING:     ('live',  'st-concat', 'b-concat', 'ANALYZING',     True),
    REC_STATUS_CONVERTING:    ('live',  'st-concat', 'b-concat', 'CONVERTING',    True),
    REC_STATUS_COMPLETED:     ('done',  'st-done',   'b-done',   'COMPLETED',     False),
    REC_STATUS_FAILED:        ('done',  'st-fail',   'b-fail',   'FAILED',        False),
    REC_STATUS_ABORTED:       ('done',  'st-abort',  'b-abort',  'CANCELLED',     False),
}


def _channel_initials(name):
    letters = ''.join(c for c in (name or '') if c.isalnum())
    return (letters[:3] or '?').upper()


def _index_row(rec, now, tz, thumb_ids):
    from ..tz_utils import UTC
    section, st_class, badge_class, badge_label, pulse = _STATUS_ROW.get(
        rec.status, ('done', 'st-abort', 'b-abort', rec.status, False))

    # A parked post-processing chain badges as WAITING and stops pulsing. This is a display
    # derivation from two stored facts, NOT a status: Recording.status stays ANALYZING or
    # CONVERTING because the startup sweep, the collision query and the cancel route all
    # branch on it, and a row that fell out of those would be stranded by the next restart
    # rather than resumed (dev/changelog/954).
    if rec.postprocess_waiting_since and rec.status in (REC_STATUS_ANALYZING,
                                                        REC_STATUS_CONVERTING):
        badge_label, pulse = 'WAITING', False

    start_l = rec.start_time.replace(tzinfo=UTC).astimezone(tz)
    today = now.replace(tzinfo=UTC).astimezone(tz).date()
    d = start_l.date()
    delta_days = (d - today).days
    if delta_days == 0:
        day = 'Today'
    elif delta_days == 1:
        day = 'Tomorrow'
    elif delta_days == -1:
        day = 'Yesterday'
    elif d.year == today.year:
        day = start_l.strftime('%a %b ') + str(start_l.day)
    else:
        day = start_l.strftime('%b ') + f'{start_l.day}, {start_l.year}'

    # relative line - explicit per section/status, anchored on the honest time
    if rec.status in (REC_STATUS_IN_PROGRESS, REC_STATUS_PAUSED, REC_STATUS_RETRYING):
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
    elif rec.status in (REC_STATUS_CONCATENATING, REC_STATUS_ANALYZING):
        rel = f'ended {_humanize_secs((now - rec.stop_time).total_seconds())} ago'
    elif rec.status == REC_STATUS_SCHEDULED:
        secs = (rec.start_time - now).total_seconds()
        rel = f'in {_humanize_secs(secs)}' if secs > 0 else 'starting'
    else:
        anchor = rec.completed_at or rec.stop_time
        rel = f'{_humanize_secs((now - anchor).total_seconds())} ago'

    sched_secs = rec.scheduled_duration_seconds
    data_segs = [s for s in rec.segments if s.bytes_recorded]
    stalls = rec.total_stall_count or 0

    # duration cell: scheduled window for SCHEDULED rows; captured-so-far while
    # live; actual (file, else segment span) once terminal - four different
    # values, each labeled by its tooltip
    dur_flag = None
    dur_tip = None
    if rec.status == REC_STATUS_SCHEDULED:
        dur_secs = sched_secs
    elif rec.status in (REC_STATUS_IN_PROGRESS, REC_STATUS_PAUSED, REC_STATUS_RETRYING):
        captured = sum((s.ended_at - s.started_at).total_seconds()
                       for s in data_segs if s.ended_at)
        active = rec.active_segment
        if active:
            captured += (now - active.started_at).total_seconds()
        dur_secs = captured
        dur_flag = 'live'
        verb = 'Waiting to retry' if rec.status == REC_STATUS_RETRYING else 'Recording'
        dur_tip = (f'{verb} - {fmt_utils.fmt_duration(captured)} captured so far '
                   f'of {fmt_utils.fmt_duration(sched_secs)} scheduled.')
    else:
        dur_secs = rec.actual_duration_seconds
        if dur_secs and sched_secs and dur_secs < sched_secs * 0.98:
            dur_flag = 'partial'
            dur_tip = (f'Partial - {fmt_utils.fmt_duration(dur_secs)} captured of '
                       f'{fmt_utils.fmt_duration(sched_secs)} scheduled.')

    # size: final file once it exists, else data written to segments so far
    size_bytes = rec.final_file_size or sum(s.bytes_recorded or 0 for s in data_segs) or None
    rate = None
    if size_bytes and dur_secs and dur_secs > 60:
        rate = f'{size_bytes * 8 / dur_secs / 1e6:.1f} Mb/s'
    size_num, size_unit = _size_parts(size_bytes)

    # health pill. Three different quantities and the tooltip keeps them apart: downtime is
    # the capture-time gap counter, the gap figure is wall clock with no segment running at
    # all (measured after the fact from the segment clocks), and the content figure compares
    # the delivered length against that capture time (dev/changelog/432, 942).
    # Gated on the same 2% the duration cell's 'partial' flag uses, so the two agree: every
    # clean capture is a few seconds off its window (ffmpeg start latency, and a connect-time
    # buffer on the other side) and saying so on every row would be noise. The detail page
    # shows both figures unconditionally.
    window = rec.duration_seconds
    gap_secs = rec.capture_gap_seconds
    vs_capture = rec.content_vs_capture_seconds
    missing = (f' {fmt_utils.fmt_duration(gap_secs, with_seconds=True)} of the recording '
               f'window with nothing capturing at all.') if (
                   gap_secs and window and gap_secs >= window * 0.02) else ''
    if vs_capture and window and abs(vs_capture) >= window * 0.02:
        missing += (
            f' The file holds {fmt_utils.fmt_duration(abs(vs_capture), with_seconds=True)} '
            f'{"more" if vs_capture > 0 else "less"} content than the time the capture ran.')
    # Distinct channels this recording's segments actually used - free to compute, rec.segments
    # is already loaded/iterated above for data_segs/stalls. >1 means a channel-group recording
    # had to change channels at least once (same-account restart never changes channel_id).
    distinct_channels = len({s.channel_id for s in rec.segments if s.channel_id is not None})
    spanned = f' Spanned {distinct_channels} channels.' if distinct_channels > 1 else ''
    if not rec.segments:
        health = {'cls': 'na', 'label': '-', 'tip': None}
    elif rec.status == REC_STATUS_FAILED:
        health = {'cls': 'bad', 'label': f'⚠ {stalls + (rec.consecutive_failures or 0)}',
                  'tip': (f'{len(rec.segments)} segments · {stalls} stalls · '
                          f'{rec.consecutive_failures or 0} consecutive failed restarts. '
                          f'Partial file kept on disk.{missing}{spanned}')}
    elif stalls > 0:
        downtime = (f' {fmt_utils.fmt_duration(rec.total_downtime_seconds, with_seconds=True)} '
                    f'with nothing being written.') if rec.total_downtime_seconds else ''
        health = {'cls': 'warn', 'label': f'⚠ {stalls}',
                  'tip': (f'{len(rec.segments)} segments · {stalls} stalls, recovered '
                          f'automatically.{downtime}{missing}{spanned}')}
    else:
        health = {'cls': 'ok', 'label': '✓',
                  'tip': (f'{len(rec.segments)} segment{"s" if len(rec.segments) != 1 else ""} '
                          f'· 0 stalls - clean capture.{missing}{spanned}')}

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
    group = rec.group
    if group is not None:
        # A group recording's channel_id is stamped to its serving member at creation
        # (new_recording_json) and re-stamped at record start / failover (recorder.py,
        # watchdog.py) - never left unset for a real group recording - so rec.channel is
        # the same "who is actually serving this" answer the TV Guide's group row shows.
        pill = {'label': group.name,
                'url': url_for('channel_groups.group_detail', group_id=group.id),
                'logo_url': resolve_logo_url(rec.channel) if rec.channel else None,
                'initials': _channel_initials(group.name),
                'acct_color': rec.channel.account.color if (rec.channel and rec.channel.account) else None}
    elif rec.channel is not None:
        pill = {'label': rec.channel.name,
                'url': url_for('channels.channel_detail', channel_id=rec.channel.id),
                'logo_url': resolve_logo_url(rec.channel),
                'initials': _channel_initials(rec.channel.name),
                'acct_color': rec.channel.account.color if rec.channel.account else None}

    acct = rec.channel.account if rec.channel else None
    live = rec.status in (REC_STATUS_IN_PROGRESS, REC_STATUS_PAUSED, REC_STATUS_RETRYING)
    # A SCHEDULED recording has never captured a frame, so a file at its thumbnail
    # path can only be a leftover from a deleted recording whose id SQLite reused -
    # showing it would present another recording's screenshot as this one's.
    has_thumb = live or (rec.status != REC_STATUS_SCHEDULED and rec.id in thumb_ids)
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
        'pill': pill,
        'acct_name': acct.name if acct else None,
        'day': day, 'start': rec.start_time, 'stop': rec.stop_time, 'rel': rel,
        'dur_str': fmt_utils.fmt_duration(dur_secs), 'dur_flag': dur_flag, 'dur_tip': dur_tip,
        'size_num': size_num, 'size_unit': size_unit, 'rate': rate,
        'health': health,
        'progress_pct': progress_pct,
        'output_file': os.path.basename(rec.output_path) if rec.output_path else None,
        'edit': edit,
        # sort keys
        'sort_start': int(rec.start_time.timestamp()),
        'sort_dur': int(dur_secs or 0), 'sort_size': size_bytes or 0,
        'sort_health': stalls,
    }


@recordings_bp.route('/recordings')
def index():
    from sqlalchemy.orm import selectinload, joinedload
    from ..tz_utils import get_display_tz
    recordings = (Recording.query
                  .options(selectinload(Recording.segments),
                           joinedload(Recording.channel).joinedload(Channel.account))
                  .order_by(Recording.start_time.desc()).all())

    # persisted screenshots: one directory listing, never a per-row stat
    thumb_cfg = load_config().get('recording', {}).get('live_thumbnail', {})
    thumb_ids = set()
    try:
        for fn in os.listdir(thumb_cfg.get('dir', '/dvr/live_thumbnails')):
            stem, ext = os.path.splitext(fn)
            if ext == '.jpg' and stem.isdigit():
                thumb_ids.add(int(stem))
    except OSError:
        pass  # thumbnail dir missing/unreadable -> rows just show placeholders

    now = datetime.utcnow()
    tz = get_display_tz()
    rows = [_index_row(r, now, tz, thumb_ids) for r in recordings]
    sections = [
        ('In Progress', 'live', [r for r in rows if r['section'] == 'live']),
        ('Scheduled', 'sched', [r for r in rows if r['section'] == 'sched']),
        ('Completed', 'done', [r for r in rows if r['section'] == 'done']),
    ]
    live_now = sum(1 for r in rows if r['status'] == REC_STATUS_IN_PROGRESS)

    from ..database import UserPref, RecordingProfile
    pref = db.session.get(UserPref, 'recordings_columns')
    col_prefs = json.loads(pref.value) if pref and pref.value else None
    profiles = RecordingProfile.query.order_by(RecordingProfile.name).all()

    return render_template('index.html', sections=sections, total=len(rows),
                           live_now=live_now, col_prefs=col_prefs,
                           profiles=profiles)  # _record_modal.html + GUIDE_CONFIG.profiles expect this name


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


@recordings_bp.route('/recordings/<int:recording_id>')
def recording_detail(recording_id):
    rec = db.session.get(Recording, recording_id)
    if rec is None:
        flash('Recording not found.', 'error')
        return redirect(url_for('recordings.index'))
    state = get_state(recording_id)
    is_active = state is not None
    ts_source_available = bool(
        rec.output_path and rec.output_path.endswith('.ts') and os.path.exists(rec.output_path)
    )
    # What a failed recording still has to work with. The two are not the same thing and a
    # FAILED page used to claim both at once: a successful concat consumes its segments, so
    # "segments kept on disk" is false for exactly the recordings that do have a recoverable
    # .ts. Stat'ed here rather than in the template, which cannot reach the filesystem.
    segment_files_on_disk = any(
        s.file_path and os.path.exists(s.file_path) and os.path.getsize(s.file_path) > 0
        for s in rec.segments
    )
    thumb_cfg = load_config().get('recording', {}).get('live_thumbnail', {})

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
    thumb_path = os.path.join(thumb_cfg.get('dir', '/dvr/live_thumbnails'), f'{rec.id}.jpg')
    # SCHEDULED excluded for the same reason as the list row's has_thumb: a file
    # at a not-yet-started recording's path belongs to a deleted, id-reused row.
    has_shot = rec.status == REC_STATUS_IN_PROGRESS or (
        rec.status != REC_STATUS_SCHEDULED and os.path.exists(thumb_path))
    shot_mtime = None
    if has_shot and rec.status != REC_STATUS_IN_PROGRESS and os.path.exists(thumb_path):
        shot_mtime = datetime.utcfromtimestamp(os.path.getmtime(thumb_path))
    # 1-based segment display: new recordings' rows are already 1-based; legacy
    # 0-based recordings get +1 applied to every displayed number (files keep
    # their names in the path column) - DESIGN.md section 5
    seg_offset = 1 if any(s.segment_number == 0 for s in rec.segments) else 0
    seg_stderr = _segment_stderr_tails(rec)
    seg_channels = _segment_channels(rec)
    distinct_accounts = _distinct_accounts(rec, seg_channels)
    event_channel_links = group_event_channel_links(rec.events)
    video, audio, tech_source = _tech_parts(rec)
    row = _index_row(rec, now, get_display_tz(), {rec.id} if has_shot else set())
    conversion_max_attempts = load_config()['recording']['post_process'].get('max_restart_attempts', 3)
    max_dead_stream_retry_attempts = load_config()['watchdog'].get('dead_stream_max_retry_attempts', 10)

    return render_template(
        'recording_detail.html', rec=rec, is_active=is_active,
        ts_source_available=ts_source_available,
        segment_files_on_disk=segment_files_on_disk,
        conversion_max_attempts=conversion_max_attempts,
        max_dead_stream_retry_attempts=max_dead_stream_retry_attempts,
        now=now,
        row=row, video=video, audio=audio, tech_source=tech_source, fmt=_format_profile(rec),
        seg_offset=seg_offset, seg_stderr=seg_stderr, seg_channels=seg_channels,
        distinct_accounts=distinct_accounts,
        event_channel_links=event_channel_links,
        shot_mtime=shot_mtime, has_shot=has_shot,
        stop_local_input=local_input_value(rec.stop_time),
        thumb_enabled=thumb_cfg.get('enabled', True),
        thumb_auto_refresh_seconds=thumb_cfg.get('auto_refresh_seconds', 60),
        find_airing_url=find_airing_url,
        profiles=profiles,  # _record_modal.html + GUIDE_CONFIG.profiles expect this name
    )


@recordings_bp.route('/recordings/<int:recording_id>/live-thumbnail.jpg')
def live_thumbnail(recording_id):
    """Recording screenshot: regenerated from the live segment while IN_PROGRESS;
    for finished recordings, serves the final frame persisted at capture end
    (recorder.persist_final_thumbnail) if one exists."""
    rec = db.session.get(Recording, recording_id)
    if rec is None:
        abort(404)

    cfg = load_config()
    thumb_cfg = cfg.get('recording', {}).get('live_thumbnail', {})
    if not thumb_cfg.get('enabled', True):
        abort(404)

    thumb_dir = thumb_cfg.get('dir', '/dvr/live_thumbnails')
    thumb_path = os.path.join(thumb_dir, f'{recording_id}.jpg')

    if rec.status != REC_STATUS_IN_PROGRESS:
        if not os.path.exists(thumb_path):
            abort(404)
        resp = send_file(thumb_path, mimetype='image/jpeg')
        resp.headers['X-Thumb-Captured-At'] = str(int(os.path.getmtime(thumb_path)))
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


# Refusing is the honest answer for a CONCATENATING row: the concat ffmpeg is spawned
# through a blocking subprocess.run and is reachable from no registry, so nothing here can
# stop it, and it is the step that produces the final file. Marking the row ABORTED while
# it keeps running is what this replaced (dev/changelog/667).
CONCAT_CANCEL_REFUSED = (
    "Concatenation is already running and can't be interrupted. It is the step that joins "
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
    # a lock-retry would re-run abort_recording + file deletion. Only the SCHEDULED
    # branch's status write is retried, in its own closure below.
    rec = db.session.get(Recording, recording_id)
    if rec is None:
        flash('Recording not found.', 'error')
        return redirect(url_for('recordings.index'))

    if rec.status in (REC_STATUS_COMPLETED, REC_STATUS_FAILED, REC_STATUS_ABORTED):
        flash('Recording is already finished.', 'error')
        return redirect(url_for('recordings.recording_detail', recording_id=recording_id))

    if rec.status == REC_STATUS_SCHEDULED:
        unschedule_recording(recording_id)

        @retry_on_locked()
        def _mark_aborted_and_commit():
            r = db.session.get(Recording, recording_id)
            add_recording_event(recording_id, RECORDING_ABORTED, detail='Recording cancelled while scheduled')
            r.status = REC_STATUS_ABORTED
            r.completed_at = datetime.utcnow()
            db.session.commit()

        _mark_aborted_and_commit()
        dismiss_recording_failing_alerts(recording_id)
        end_slot_wait(recording_id)
        flash(f'Recording "{rec.name}" cancelled.', 'success')
    elif rec.status in (REC_STATUS_IN_PROGRESS, REC_STATUS_PAUSED, REC_STATUS_RETRYING):
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

    if rec.status not in (REC_STATUS_IN_PROGRESS, REC_STATUS_PAUSED, REC_STATUS_RETRYING):
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
    from ..scheduler import schedule_recording as _sched
    resume_recording(current_app._get_current_object(), recording_id)
    # Re-register the stop job in case it was removed
    _sched(current_app._get_current_object(), recording_id, rec.start_time, rec.stop_time)
    flash(f'Recording "{rec.name}" resumed.', 'success')
    return redirect(url_for('recordings.recording_detail', recording_id=recording_id))


@recordings_bp.route('/recordings/<int:recording_id>/adjust-stop', methods=['POST'])
@retry_on_locked()
def adjust_stop_time(recording_id):
    """JSON endpoint - change stop_time of an active recording from the TV Guide."""
    rec = db.session.get(Recording, recording_id)
    if rec is None:
        return jsonify({'error': 'Recording not found'}), 404
    if rec.status not in (REC_STATUS_IN_PROGRESS, REC_STATUS_PAUSED, REC_STATUS_RETRYING):
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
    if rec.status not in (REC_STATUS_IN_PROGRESS, REC_STATUS_PAUSED, REC_STATUS_RETRYING):
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
    from ..scheduler import schedule_recording as _sched
    resume_recording(current_app._get_current_object(), recording_id)
    _sched(current_app._get_current_object(), rec.id, rec.start_time, rec.stop_time)
    return jsonify({'success': True})


@recordings_bp.route('/recordings/<int:recording_id>/cancel-json', methods=['POST'])
def cancel_recording_json(recording_id):
    """JSON endpoint for cancelling an active recording (used from TV Guide)."""
    rec = db.session.get(Recording, recording_id)
    if rec is None:
        return jsonify({'error': 'Recording not found'}), 404

    if rec.status in (REC_STATUS_COMPLETED, REC_STATUS_FAILED, REC_STATUS_ABORTED):
        return jsonify({'error': 'Recording is already finished'}), 400

    if rec.status in (REC_STATUS_IN_PROGRESS, REC_STATUS_PAUSED, REC_STATUS_RETRYING):
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
    paths = recording_disk_paths(recording_id) if remove_files else []
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

    # Group-backed recording (created from a channel-group guide row): channel_id must
    # be a member; the best member is re-resolved at record start (app/recorder.py).
    group_id_raw = request.form.get('group_id', '').strip()
    group_id = int(group_id_raw) if group_id_raw.isdigit() else None
    if group_id is not None:
        group = db.session.get(ChannelGroup, group_id)
        if group is None:
            group_id = None
        elif group.is_system:
            # The pinned TV Guide Channels group has computed membership and is not a
            # recording source (server-side enforcement, not just UI).
            return jsonify({'error': 'The TV Guide Channels group cannot back a '
                                     'recording.'}), 400
        else:
            from .channel_tests import _latest_tests_by_channel
            streak_threshold = cfg.get('channel_testing', {}).get(
                'failing_streak_threshold', DEFAULT_FAILING_STREAK_THRESHOLD)
            active = recording_members(group.memberships)
            latest_by_channel = _latest_tests_by_channel([ch.id for ch in active])
            # Format lock filters, health score ranks - the member stamped on the
            # recording now must be the one record start would resolve to, or the
            # scheduled recording names a feed that will not be used
            # (DESIGN-channel-groups-model.md 5).
            active = format_eligible_members(group, active, latest_by_channel).members
            # Then the account preference, the schedule-time half of dev/changelog/855.
            # The channel_id the modal supplies for a group row is the guide's serving
            # member - the app's own ranking, not a feed the user picked by hand - so a
            # multi-account group rolls over to an account with room in this window
            # rather than stamping a member the limit check below would refuse. Only
            # when the supplied member is genuinely unusable, so a schedule that would
            # have gone through is never quietly moved to a different feed.
            committed = _members_committed_in_window(
                active, start_utc, stop_utc, exclude_recording_id=replace_recording_id)
            supplied_is_member = (channel_id is not None
                                  and channel_id in {m.channel_id for m in group.memberships})
            keep_supplied = (supplied_is_member
                             and channel_id in {ch.id for ch in active}
                             and channel_id not in committed)
            if not keep_supplied:
                member = pick_best_member(active, latest_by_channel, exclude_ids=committed,
                                          streak_threshold=streak_threshold)
                if member is None and not supplied_is_member:
                    # Nothing on an uncommitted account and nothing supplied to fall back
                    # on: stamp the plain best member and let the limit check speak to it.
                    # Rolling over cannot help when every account is committed.
                    member = pick_best_member(active, latest_by_channel,
                                              streak_threshold=streak_threshold)
                    if member is None:
                        group_id = None
                if member is not None:
                    channel_id = member.id

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
            exclude_recording_id=replace_recording_id)
        if warnings:
            return jsonify({'success': False, **warnings})

    now_utc = datetime.utcnow()
    created_after_start = start_utc < now_utc
    if created_after_start:
        start_utc = now_utc

    # Snapshot the EPG program's own air time (independent of what the user
    # typed into the form) at creation, so it stays immutable even if the
    # program later shifts in the guide or the recording is edited/adjusted.
    # A plain read - not wrapped in retry_on_locked - whose result is captured
    # into plain local variables (not an ORM object reference) before the
    # write closure below, so nothing here is affected by a rollback+retry
    # inside that closure.
    source_epg_id_raw = request.form.get('source_epg_id', '').strip()
    program_start_time = program_stop_time = None
    program_title = program_sub_title = None
    if source_epg_id_raw.isdigit():
        entry = db.session.get(EPGEntry, int(source_epg_id_raw))
        if entry is not None:
            program_start_time = entry.start_time
            program_stop_time = entry.stop_time
            program_title = entry.title
            program_sub_title = entry.sub_title

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
            status=REC_STATUS_SCHEDULED,
            channel_id=channel_id,
            group_id=group_id,
            profile_id=profile_id,
        )
        db.session.add(r)
        db.session.commit()
        return r

    rec = _create_recording_and_commit()

    if created_after_start:
        @retry_on_locked()
        def _log_created_after_start_and_commit():
            db.session.add(RecordingEvent(
                recording_id=rec.id,
                event_type=RECORDING_CREATED_AFTER_EVENT_START,
                detail='Recording created after the program\'s scheduled start time; start_time set to now',
            ))
            db.session.commit()

        _log_created_after_start_and_commit()

    from flask import current_app
    schedule_recording(current_app._get_current_object(), rec.id, start_utc, stop_utc)

    # Schedule-time half of DESIGN-prerecord-checks.md §2 (dev/changelog/478): warn now
    # if the channel/group this recording just landed on is already failing, rather than
    # waiting for the next scheduled test to happen to run assess_scheduled_recording_impact.
    evaluate_and_alert_recording(rec, cfg)

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
                db.session.delete(old)
            db.session.add(RecordingEvent(
                recording_id=rec.id,
                event_type=RECORDING_REPLACED_OTHER,
                detail=detail,
            ))
            db.session.commit()

        _delete_replaced_and_commit()

    return jsonify({'success': True, 'id': rec.id})


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

    force = request.form.get('force') == '1'
    if not force:
        warnings = _pending_recording_warnings(
            rec.channel_id, rec.group_id, start_utc, stop_utc,
            exclude_recording_id=recording_id)
        if warnings:
            return jsonify({'success': False, **warnings})

    profile_id_raw = request.form.get('profile_id', '').strip()
    profile_id = int(profile_id_raw) if profile_id_raw.isdigit() else None

    _apply_edit_and_reschedule(recording_id, name, url, start_utc, stop_utc,
                               profile_id=profile_id)

    return jsonify({'success': True})


@recordings_bp.route('/recordings/<int:recording_id>/delete-json', methods=['POST'])
def delete_recording_json(recording_id):
    rec = db.session.get(Recording, recording_id)
    if rec is None:
        return jsonify({'error': 'Recording not found.'}), 404

    if rec.status in RESTART_BLOCKING_STATUSES:
        return jsonify({'error': 'Cannot delete an active recording. Cancel it first.'}), 400

    name = rec.name
    remove_files = _wants_file_deletion(request.get_json(silent=True) or {})
    paths = recording_disk_paths(recording_id) if remove_files else []
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
        flash('Concat retry is only available for FAILED or stuck CONCATENATING recordings.', 'error')
        return redirect(url_for('recordings.recording_detail', recording_id=recording_id))

    # CONCATENATING is accepted above to rescue a row stranded by a crash, but the same
    # status covers a live chain that is merely queued (waiting on serialize_concat). Only
    # the registry can tell them apart; without this a retry starts a second ffmpeg on the
    # same output_path and both delete the same segments (dev/changelog/668).
    if is_concat_active(recording_id):
        flash('Concatenation is already running for this recording - it may be queued behind '
              'another recording or concat. Retry is not needed.', 'error')
        return redirect(url_for('recordings.recording_detail', recording_id=recording_id))

    segments = RecordingSegment.query.filter_by(recording_id=recording_id).all()
    valid = [s for s in segments
             if s.file_path and os.path.exists(s.file_path) and os.path.getsize(s.file_path) > 0]
    if not valid:
        flash('No segment files found on disk - cannot retry concat.', 'error')
        return redirect(url_for('recordings.recording_detail', recording_id=recording_id))

    # Commit in its own closure; the thread launch stays outside so a lock-retry
    # can never spawn a second concat thread.
    @retry_on_locked()
    def _mark_concatenating_and_commit():
        r = db.session.get(Recording, recording_id)
        r.status = REC_STATUS_CONCATENATING
        db.session.commit()

    _mark_concatenating_and_commit()

    app_obj = current_app._get_current_object()
    threading.Thread(
        target=do_concatenation,
        args=(app_obj, recording_id),
        kwargs={'reason': 'Manual retry of concatenation'},
        daemon=True,
    ).start()

    flash(f'Concat retry started for "{rec.name}".', 'success')
    return redirect(url_for('recordings.recording_detail', recording_id=recording_id))


@recordings_bp.route('/recordings/<int:recording_id>/retry-convert', methods=['POST'])
def retry_convert(recording_id):
    from flask import current_app
    from ..concatenator import is_concat_active, run_postprocess_claimed
    from ..postprocessor import is_conversion_active

    rec = db.session.get(Recording, recording_id)
    if rec is None:
        flash('Recording not found.', 'error')
        return redirect(url_for('recordings.index'))

    # FAILED (retry a given-up conversion), CONVERTING or ANALYZING (a row stranded by a
    # restart/crash that nothing is working), or ABORTED (post-processing the user
    # cancelled). Refuse if anything is genuinely live for this id so two chains don't fight
    # over the same output.
    if rec.status not in (REC_STATUS_FAILED, REC_STATUS_ANALYZING, REC_STATUS_CONVERTING,
                          REC_STATUS_ABORTED):
        flash('Conversion retry is only available for failed, cancelled, or stalled recordings.', 'error')
        return redirect(url_for('recordings.recording_detail', recording_id=recording_id))

    # Two registries, because they answer at different times: is_conversion_active() only
    # holds once ffmpeg has been spawned, which is the back half of the chain. The analysis
    # phase ahead of it spawns nothing, so a live ANALYZING row is invisible to it and the
    # live-chain claim is what covers that window.
    if is_conversion_active(recording_id) or is_concat_active(recording_id):
        flash('Post-processing is already running for this recording.', 'error')
        return redirect(url_for('recordings.recording_detail', recording_id=recording_id))

    ts_path = rec.output_path
    if not ts_path or not ts_path.endswith('.ts') or not os.path.exists(ts_path):
        flash('No concatenated .ts source file found on disk - cannot retry conversion.', 'error')
        return redirect(url_for('recordings.recording_detail', recording_id=recording_id))

    # ANALYZING, not CONVERTING: do_postprocess re-reads the whole .ts before it spawns any
    # ffmpeg, so a row marked CONVERTING here would be back to naming a phase the app is not
    # in yet - the defect this status was added to end (dev/changelog/867). do_postprocess
    # sets it again itself; this write is what the redirect below renders.
    #
    # A manual Retry is an operator action that buys a fresh restart budget: reset the
    # attempt counter to 0.
    @retry_on_locked()
    def _mark_analyzing_and_commit():
        r = db.session.get(Recording, recording_id)
        r.status = REC_STATUS_ANALYZING
        r.conversion_attempts = 0
        db.session.commit()

    _mark_analyzing_and_commit()

    app_obj = current_app._get_current_object()
    threading.Thread(
        target=run_postprocess_claimed,
        args=(app_obj, recording_id, ts_path),
        daemon=True,
    ).start()

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
    from ..database import RecordingEvent, CONVERSION_DONE

    # A live conversion: signal its loop to abort (it sets the terminal status + deletes the
    # partial output). A stranded CONVERTING row with no live ffmpeg: mark it here, since no
    # loop exists to do it.
    if request_cancel_conversion(recording_id):
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
            event_type=CONVERSION_DONE,
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
def time_until_filter(dt):
    """How long until a scheduled time - and, when it has already passed, how far overdue.

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
    return f'in {s}s'


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
