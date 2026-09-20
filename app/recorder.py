"""
Core recording management.

Module-level _active: dict[int, RecordingState] holds live state for each
in-progress recording. All mutations are protected by _lock.
"""
import glob
import json
import logging
import os
import subprocess
import threading
import time
import uuid
from collections import namedtuple
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Optional

from sqlalchemy.exc import SQLAlchemyError

from . import db
from .config import load_config
from .database import (
    Channel, Recording, RecordingSegment, RecordingEvent, add_recording_event,
    SEGMENT_STARTED, SEGMENT_ENDED, RECORDING_HANDOFF, DIAGNOSTICS, RESTART_ATTEMPTED,
    GROUP_MEMBER_SELECTED, GROUP_FAILOVER, RECORDING_FORMAT_OVERRIDE,
    RECORDING_URL_RERESOLVED, RECORDING_RESUME_REFUSED,
    RECORDING_FAILED, RECORDING_START_DEFERRED, RECORDING_ABORTED, RECORDING_PAUSED,
    REC_STATUS_SCHEDULED, REC_STATUS_IN_PROGRESS, REC_STATUS_PAUSED, REC_STATUS_RETRYING,
    REC_STATUS_FAILED, REC_STATUS_ABORTED,
    FAILURE_CONVERSION_COLLISION, FAILURE_CONNECTION_SLOT_TIMEOUT, FAILURE_DVR_DIR_UNUSABLE,
    FAILURE_LAUNCH_FAILED,
    CANCEL_DURING_CAPTURE,
)
from .channel_groups import (effective_score, pick_best_member, recording_members,
                             format_eligible_members, format_key, format_label,
                             segment_format_key, DEFAULT_FAILING_STREAK_THRESHOLD)
from .db_utils import retry_on_locked
from .fs_utils import PATH_OK, describe_dir_problem, probe_dir
from .proc_utils import (build_capture_cmd, count_stderr_matches, read_stderr_tail,
                         terminate_or_kill)
from .url_utils import mask_creds, mask_creds_in_text
from . import events as ev

log = logging.getLogger(__name__)

_lock = threading.Lock()

# How long a recording waiting for a free connection slot sleeps between attempts.
# Deliberately shorter than the conversion-collision defer in start_recording: a slot
# frees the instant another recording ends, and every second spent waiting is content
# this recording will never capture. Not a config key - it is a retry cadence, not a
# policy the user has any basis to tune.
SLOT_WAIT_POLL_SECONDS = 10

# What _launch_segment did, for the one caller that has to tell them apart. Named states
# rather than a bool because they carry different obligations, and collapsing any two of
# them is what the defect was: LAUNCH_FAILED means _handle_launch_failure has ALREADY
# counted the failure and now owns either the give-up or the relaunch, so a caller that
# re-counts it charges one hiccup twice (dev/changelog/984). LAUNCH_ABANDONED means a child
# was spawned and a concurrent teardown immediately claimed it - nothing is running, but
# nothing needs accounting either. LAUNCH_REFUSED means no child was spawned at all because
# the recording is no longer IN_PROGRESS: deliberately not folded into LAUNCH_ABANDONED,
# whose whole meaning is that a process existed and was reclaimed (dev/changelog/987). A
# caller that only needs "is a capture running now" tests against LAUNCH_SPAWNED, so a
# state added later cannot land in a trailing else.
LAUNCH_SPAWNED = 'SPAWNED'
LAUNCH_FAILED = 'LAUNCH_FAILED'
LAUNCH_ABANDONED = 'ABANDONED'
LAUNCH_REFUSED = 'REFUSED'


@dataclass
class RecordingState:
    process: Optional[subprocess.Popen] = None
    stop_event: threading.Event = field(default_factory=threading.Event)
    watchdog: Optional[threading.Thread] = None
    current_segment_num: int = 0
    # Open spool file (and its path) receiving the current segment's ffmpeg stderr, so
    # every terminal path can read the tail and unlink it. Held here rather than passed
    # around because the paths that end a segment - watchdog stall, manual stop, failover,
    # give-up - reach the process the same way. Both are None between segments and
    # whenever spooling could not be set up (dev/changelog/430).
    stderr_fh: Optional[object] = None
    stderr_path: Optional[str] = None
    # Pending relaunch after a Popen that failed below the give-up threshold. Held here so
    # the thread is reachable from tracked state; it waits on stop_event, so every teardown
    # path cancels it (dev/changelog/644).
    launch_retry: Optional[threading.Thread] = None
    # Group failover (group-backed recordings only): members whose feed already died
    # during THIS recording - excluded when picking the next member. In-memory by
    # design: a service restart forgets the set, worst case costing one wasted retry
    # cycle per previously-failed member.
    failed_member_ids: set = field(default_factory=set)
    # Members this recording moved off because they stalled repeatedly while every
    # restart succeeded (dev/changelog/889). Deliberately NOT failed_member_ids, which is
    # permanent for the run: nothing here died, so a demoted member merely ranks below the
    # ones not yet demoted and stays selectable. A three-member group therefore cycles
    # A -> B -> C -> A rather than running out of candidates, and a member whose feed
    # settles down is reachable again without any un-demotion machinery.
    demoted_member_ids: set = field(default_factory=set)
    # How many stall-driven moves this recording has made. Reported, never enforced - a
    # cap was designed and dropped once the cost was measured at zero: a stall closes the
    # current segment and opens the next one whether or not the member changes, so the
    # move rides a segment boundary that was happening anyway (and skips the restart
    # delay, making it ~30s cheaper than staying put).
    stall_moves: int = 0
    # Channels this recording has switched to real-time pacing on its own, because a segment
    # on them was stopped for fast delivery after in-process reconnects (dev/changelog/997).
    # Keyed on the member, so a failover does not carry it to a feed that never showed the
    # problem. In-memory by design: it is a recovery for this run, never a stored setting -
    # Channel.pace_realtime is the user's answer and nothing here writes it. A pause/resume
    # or service restart forgets it, costing at most one more fast-delivery kill to re-learn.
    auto_paced_channel_ids: set = field(default_factory=set)
    # Whether the segment now running was launched with -re, so the watchdog can tell a
    # paced capture that still misbehaved (pacing is not the fix) from an unpaced one.
    current_segment_paced: bool = False


# Where a segment's real-time pacing decision came from. Every value is named, and the
# segment-start event says which one applied (dev/changelog/997).
PACING_BOUNDED = 'bounded'           # a segment length is set, so -re is mandatory
PACING_CHANNEL_ON = 'channel_on'     # Channel.pace_realtime is True
PACING_CHANNEL_OFF = 'channel_off'   # Channel.pace_realtime is False
PACING_DEFAULT_ON = 'default_on'     # the channel follows ffmpeg.pace_realtime, which is on
PACING_DEFAULT_OFF = 'default_off'   # the channel follows ffmpeg.pace_realtime, which is off
PACING_AUTOMATIC = 'automatic'       # the watchdog turned it on for this channel this run

_PACING_NOTES = {
    PACING_BOUNDED: 'required by the configured segment length',
    PACING_CHANNEL_ON: "this channel's setting",
    PACING_DEFAULT_ON: 'the default in Settings',
    PACING_AUTOMATIC: 'turned on automatically earlier in this recording',
}


def resolve_capture_pacing(cfg: dict, channel_pace, segment_duration, auto_paced: bool):
    """(pace, source) for one recording segment's -re flag.

    Order matters. A bounded segment is always paced, because -t without -re is satisfied
    from a provider's backlog in seconds (dev/changelog/437). An explicit per-channel answer
    beats everything else, including the watchdog's automatic pacing - the user said no.
    Automatic pacing only ever turns pacing ON for a channel following the default.
    """
    if segment_duration:
        return True, PACING_BOUNDED
    if channel_pace is True:
        return True, PACING_CHANNEL_ON
    if channel_pace is False:
        return False, PACING_CHANNEL_OFF
    if cfg.get('ffmpeg', {}).get('pace_realtime', False):
        return True, PACING_DEFAULT_ON
    if auto_paced:
        return True, PACING_AUTOMATIC
    return False, PACING_DEFAULT_OFF


# recording_id → RecordingState
_active: dict = {}


def recording_disk_paths(recording_id: int, cfg: dict = None) -> list:
    """Every on-disk file a recording owns, for teardown: every file its output stem can
    occupy, its metadata sidecar and poster, all segment files, the conversion scratch
    files, the partly-encoded parts a resumable conversion checkpoints into, and its images
    (recording_image_paths()).
    Paths only - no deletion. Requires an active app context. Missing/None paths are
    omitted; the thumbnail and scratch paths are always included (delete_files() guards on
    existence).

    The stem is expanded through the WHOLE output-extension family rather than the one
    sibling of whatever `output_path` currently names, and the direction that matters is
    .ts -> .<fmt>: `output_path` is only repointed at the converted file on success, so a
    recording whose conversion gave up still names its .ts while a multi-GB partial .mp4
    sits beside it. Enumerating only the row's own extension deleted the .ts and the
    segments and stranded that partial forever, with no row left pointing at it
    (dev/changelog/888). The family comes from output_extension_family(), the same answer
    reserve_concat_output_path() uses to stake the stem - so every path listed here is one
    that reservation already proved belongs to this recording and not to a _2-suffixed
    neighbour.

    A move on completion repoints `output_path` at the destination, so the stem the file
    was moved FROM is expanded the same way: a .ts kept by post_process.delete_source off
    (or one whose unlink failed) and any conversion scratch stay behind in that folder, and
    the FILE_MOVED event's from_path is the only record of it (dev/changelog/1041). Moves
    made before that event carried from_path are not covered.

    Its one blind spot is deliberate: the family is read from the CURRENT config, so a
    partial left by an attempt that ran under a different post_process.format is not
    listed. Nothing records the format an attempt actually used, and globbing the stem
    would delete on a guess."""
    from .database import FILE_MOVED
    from .metadata_sidecar import sidecar_paths
    from .postprocessor import all_part_paths_on_disk, output_extension_family

    paths = []
    if cfg is None:
        cfg = load_config()
    family = output_extension_family(cfg)
    rec = db.session.get(Recording, recording_id)
    stems = []
    if rec is not None and rec.output_path:
        paths.append(rec.output_path)
        stems.append(os.path.splitext(rec.output_path)[0])
    for ev_row in (RecordingEvent.query
                   .filter_by(recording_id=recording_id, event_type=FILE_MOVED).all()):
        try:
            from_path = json.loads(ev_row.extra_data).get('from_path') if ev_row.extra_data else None
        except (ValueError, TypeError, AttributeError) as exc:
            log.warning('Recording %d: unreadable FILE_MOVED extra_data: %s', recording_id, exc)
            from_path = None
        if from_path:
            stems.append(os.path.splitext(from_path)[0])
    for stem in dict.fromkeys(stems):
        for ext in family:
            sibling = stem + ext
            if sibling not in paths:
                paths.append(sibling)
        # The .nfo and poster that describe this recording to a media server. Enumerated
        # here and NOT in recording_image_paths(), which is what a keep-the-files delete
        # removes: these describe the video the user is choosing to keep, and a library
        # left holding the video without them would silently lose its title and synopsis
        # (dev/changelog/1057). They are stem siblings but not members of the extension
        # family, because that family also decides which stems a concat may claim.
        for sidecar in sidecar_paths(stem + family[0]):
            if sidecar not in paths:
                paths.append(sidecar)
        # Supervised-run scratch (progress + stderr tail) is written alongside the output
        # file and unlinked in proc_utils.supervise_ffmpeg's finally, so these only survive
        # a shutdown that skipped it - after which a delete is the last thing that will ever
        # look at them. All four prefixes: 'conv' is the mp4/mkv conversion, 'concat' the
        # segment join (dev/changelog/947), 'part' the re-mux that salvages a killed part and
        # 'join' the assembly of those parts (dev/changelog/955). Patterns are copied from
        # that function's own stale-reap list
        # and listed per id for the same reason: a bare f'{recording_id}*' glob would let
        # id 6 match id 64's files.
        scratch_dir = os.path.dirname(stem) or '.'
        for prefix in ('conv', 'concat', 'part', 'join'):
            paths.extend(glob.glob(os.path.join(scratch_dir, f'.{prefix}-progress-{recording_id}-*.txt')))
            paths.extend(glob.glob(os.path.join(scratch_dir, f'.{prefix}-stderr-{recording_id}-*.log')))
            paths.append(os.path.join(scratch_dir, f'.{prefix}-progress-{recording_id}.txt'))
            paths.append(os.path.join(scratch_dir, f'.{prefix}-stderr-{recording_id}.log'))
        # The partly-encoded parts a resumable conversion checkpoints into
        # (dev/changelog/955). Enumerated off the SAME extension family as the stem above,
        # for the same reason: a killed re-encode leaves the row naming its .ts with parts
        # beside it under the converted extension, and they are multi-gigabyte. They are
        # found on disk rather than read from conversion_parts_done, because the count is
        # what a crash between ffmpeg and the commit gets wrong - and a part no row knows
        # about is precisely the file nothing else will ever delete.
        for ext in family:
            paths.extend(all_part_paths_on_disk(stem + ext))
    for seg in RecordingSegment.query.filter_by(recording_id=recording_id).all():
        if seg.file_path:
            paths.append(seg.file_path)
    paths.extend(recording_image_paths(recording_id, cfg))
    return list(dict.fromkeys(paths))


def recording_image_paths(recording_id: int, cfg: dict = None) -> list:
    """The images ChannelBin itself made for a recording: the thumbnail and the poster
    frame, plus any temp capture a failed or interrupted grab left beside either.

    Removed on EVERY delete, including one that keeps the recording's files: both are named
    by the row's id and only the row's pages ever show them, so once the row is gone nothing
    will display or delete them again - and the keep-files dialog promises that only what
    ChannelBin holds is removed (dev/changelog/1041). The COPY of the poster written beside
    the video is a different file and deliberately not here: it describes the video the user
    is choosing to keep, and recording_disk_paths() is what enumerates it.

    The temp glob is anchored on f'{id}.' so recording 6 never matches recording 64's
    files."""
    from .storage_dirs import POSTER_FRAMES, THUMBNAILS, image_dir

    cfg = cfg if cfg is not None else load_config()
    paths = []
    for kind in (THUMBNAILS, POSTER_FRAMES):
        folder = image_dir(cfg, kind)
        paths.append(os.path.join(folder, f'{recording_id}.jpg'))
        paths.extend(glob.glob(os.path.join(folder, f'{recording_id}.*.tmp.jpg')))
    return paths


#: Which segment the poster frame ended up coming from, in the words the log uses. The
#: three are real states and each is named, rather than one of them being the trailing
#: else: "the moment we asked for" and "the nearest content we had" are different answers
#: and a cover that silently came from the wrong place is not explicable later.
POSTER_FRAME_AT_TARGET = 'at_target'
POSTER_FRAME_AFTER_TARGET = 'after_target'
POSTER_FRAME_PAST_END = 'past_end'

#: The two images a finished recording can show, and the only two values
#: recording.live_thumbnail.finished_image accepts. Spelled here rather than in the route
#: that validates it, so the reader and the gate cannot drift apart.
FINISHED_IMAGE_POSTER = 'poster'
FINISHED_IMAGE_LAST_FRAME = 'last_frame'
FINISHED_IMAGE_CHOICES = (FINISHED_IMAGE_POSTER, FINISHED_IMAGE_LAST_FRAME)

#: Where the offset was measured from. The program's own start time is the point of the
#: feature; a manual URL-only recording carries none, exactly as it carries no
#: program_title, and falls back to the start of its own first segment.
POSTER_ANCHOR_PROGRAM = 'program start'
POSTER_ANCHOR_CAPTURE = 'the start of the capture'


def poster_frame_path(recording_id: int, cfg: dict = None) -> str:
    """Where this recording's poster frame lives. The one spelling of that name, so the
    writer, the teardown and the two readers cannot disagree about it."""
    from .storage_dirs import POSTER_FRAMES, image_dir

    return os.path.join(image_dir(cfg if cfg is not None else load_config(), POSTER_FRAMES),
                        f'{recording_id}.jpg')


def _segment_span_end(seg):
    """The wall-clock moment this segment stopped covering, or None when it cannot be told.

    content_duration_seconds is the fallback rather than the first choice because it
    measures CONTENT, and a provider that replays its buffer on reconnect delivers more
    content than wall clock (see the column's own comment). ended_at is the wall clock and
    is what a target instant has to be compared against.
    """
    if seg.ended_at:
        return seg.ended_at
    if seg.content_duration_seconds:
        return seg.started_at + timedelta(seconds=seg.content_duration_seconds)
    return None


def poster_frame_seek(candidates, target):
    """Which captured segment holds `target`, and how far into it to seek.

    Returns `(segment, seconds, how)`, or `(None, 0.0, None)` when there is nothing to
    grab. `candidates` is concatenator.joinable_segments() output, in capture order - this
    function is pure so the rule can be tested without a file or an ffmpeg, and it is the
    part of this feature most likely to be wrong.

    A recording is many segments with gaps between them, so the moment asked for may not be
    inside any of them. One rule covers every shape: the first segment whose captured span
    reaches past `target`, seeking to the distance from that segment's own start.

    - The target sits inside a segment: seek to it. POSTER_FRAME_AT_TARGET.
    - The target sits in a gap between two segments, or before the capture joined the feed
      at all: the next segment starts after it, so the earliest content that exists after
      the moment asked for is the top of that segment. POSTER_FRAME_AFTER_TARGET.
    - The target is past everything captured, which is what a program that aired later than
      its listing said looks like: the last segment, POSTER_FRAME_PAST_END. Reaching for
      the newest content rather than giving up, because a frame from the wrong minute is
      still a frame from this recording.

    A segment whose end cannot be told is treated as covering nothing beyond its own start,
    so an unknown falls through to the next segment rather than swallowing every target
    after it.
    """
    for seg in candidates:
        if target < seg.started_at:
            return seg, 0.0, POSTER_FRAME_AFTER_TARGET
        end = _segment_span_end(seg)
        if end is not None and target < end:
            return seg, max(0.0, (target - seg.started_at).total_seconds()), POSTER_FRAME_AT_TARGET
    if candidates:
        return candidates[-1], 0.0, POSTER_FRAME_PAST_END
    return None, 0.0, None


def persist_poster_frame(recording_id: int):
    """Capture the recording's poster: a frame from inside the program itself.

    Called from the concatenator in the same moment as persist_final_thumbnail(), and for
    the same reason - a successful join deletes the segment files, so this is the last
    point at which the frame can be taken from a file already on disk. It must never come
    from a second pull on the stream URL: that spends a provider connection against a live
    capture, which is what a diagnostic is forbidden from doing to the thing it describes.

    Deliberately NOT the same image as the final-frame thumbnail. That one is grabbed from
    the END of the last segment with data, which is an honest "what did this end on" and a
    poor cover - a ball game ends on a postgame graphic, a commercial or the provider's
    slate.

    Best-effort: a failure only means the recording falls back to its thumbnail. Spawns
    ffmpeg, so callers must keep it OUTSIDE any retry_on_locked closure. Requires an app
    context.
    """
    from .concatenator import joinable_segments
    from .config import resolve_ffmpeg_path
    from .screenshot import capture_screenshot

    cfg = load_config()
    thumb_cfg = cfg.get('recording', {}).get('live_thumbnail', {})
    if not thumb_cfg.get('enabled', True):
        return
    rec = db.session.get(Recording, recording_id)
    if rec is None:
        return

    segments = (RecordingSegment.query.filter_by(recording_id=recording_id)
                .order_by(RecordingSegment.segment_number).all())
    candidates = joinable_segments(segments)
    if not candidates:
        log.info('Recording %d: no poster frame - nothing was captured to take one from',
                 recording_id)
        return

    # Four different times are in play here and only one of them is the right anchor:
    # the recording's scheduled start, its actual start, the program's start and the
    # program's end. It is program_start_time, which is an immutable creation snapshot, so
    # no new state is needed to compute the offset.
    if rec.program_start_time:
        anchor, anchor_kind = rec.program_start_time, POSTER_ANCHOR_PROGRAM
    else:
        anchor, anchor_kind = candidates[0].started_at, POSTER_ANCHOR_CAPTURE
    offset = thumb_cfg.get('poster_frame_offset_seconds', 60)
    try:
        offset = max(0.0, float(offset))
    except (TypeError, ValueError):
        log.warning('Recording %d: poster_frame_offset_seconds is not a number (%r) - '
                    'taking the frame at the program start instead', recording_id, offset)
        offset = 0.0
    target = anchor + timedelta(seconds=offset)

    seg, seek, how = poster_frame_seek(candidates, target)
    poster_path = poster_frame_path(recording_id, cfg)
    poster_dir = os.path.dirname(poster_path)
    try:
        os.makedirs(poster_dir, exist_ok=True)
    except OSError as exc:
        log.warning('Recording %d: cannot create poster frame dir %s: %s',
                    recording_id, poster_dir, exc)
        return
    tmp_path = os.path.join(poster_dir, f'{recording_id}.{uuid.uuid4().hex}.tmp.jpg')
    ffmpeg_path = resolve_ffmpeg_path(cfg.get('ffmpeg', {}).get('path', 'ffmpeg'))
    ok = capture_screenshot(
        seg.file_path, tmp_path, ffmpeg_path,
        seek_args=['-ss', f'{seek:.2f}'],
        timeout=thumb_cfg.get('capture_timeout_seconds', 12),
    )
    if not ok:
        log.info('Recording %d: poster frame capture failed (segment %d at %.2fs) - it '
                 'falls back to the final-frame thumbnail',
                 recording_id, seg.segment_number, seek)
        return
    try:
        os.replace(tmp_path, poster_path)
    except OSError as exc:
        log.warning('Recording %d: poster frame replace failed: %s', recording_id, exc)
        return
    if how == POSTER_FRAME_AT_TARGET:
        log.info('Recording %d: poster frame taken %.0fs after %s, %.2fs into segment %d',
                 recording_id, offset, anchor_kind, seek, seg.segment_number)
    elif how == POSTER_FRAME_AFTER_TARGET:
        log.info('Recording %d: nothing was captured %.0fs after %s, so the poster frame '
                 'is the first content after it, at the top of segment %d',
                 recording_id, offset, anchor_kind, seg.segment_number)
    else:
        log.warning('Recording %d: %.0fs after %s is past everything this recording '
                    'captured, so the poster frame is the top of its last segment (%d)',
                    recording_id, offset, anchor_kind, seg.segment_number)


def finished_image_path(recording_id: int, cfg: dict = None) -> str:
    """The image a FINISHED recording shows, honoring recording.live_thumbnail
    .finished_image, or None when it has neither.

    The one place that preference is resolved, so the list page, the detail page and the
    route that serves the bytes cannot disagree about which file a row is showing.

    The unchosen image is the fallback rather than a hard miss, in both directions: every
    recording made before the poster frame existed has only a thumbnail, and a recording
    whose poster capture failed has one too. A row showing a placeholder because the
    preferred image happens to be absent would be a regression dressed up as a setting.
    """
    cfg = cfg if cfg is not None else load_config()
    for path in finished_image_candidates(recording_id, cfg):
        if os.path.exists(path):
            return path
    return None


def finished_image_candidates(recording_id: int, cfg: dict) -> list:
    """Both images a finished recording could show, preferred one first."""
    from .storage_dirs import THUMBNAILS, image_dir

    thumb = os.path.join(image_dir(cfg, THUMBNAILS), f'{recording_id}.jpg')
    poster = poster_frame_path(recording_id, cfg)
    prefers_poster = (cfg.get('recording', {}).get('live_thumbnail', {})
                      .get('finished_image', FINISHED_IMAGE_POSTER) != FINISHED_IMAGE_LAST_FRAME)
    return [poster, thumb] if prefers_poster else [thumb, poster]


def finished_image_dirs(cfg: dict) -> list:
    """The folders finished_image_path() reads, for a page that needs the ids of every
    recording that HAS an image - one directory listing each, never a stat per row."""
    from .storage_dirs import POSTER_FRAMES, THUMBNAILS, image_dir

    return [image_dir(cfg, THUMBNAILS), image_dir(cfg, POSTER_FRAMES)]


def persist_final_thumbnail(recording_id: int):
    """Refresh the recording's thumbnail from the last segment that has data, so a
    finished recording keeps a final-frame screenshot at the live-thumbnail path
    (DESIGN.md: list-row thumbs + detail hero for completed/failed rows).

    Best-effort - failure only means the row shows a placeholder. Requires an app
    context. Spawns ffmpeg, so callers must keep it OUTSIDE any retry_on_locked
    closure."""
    from .config import resolve_ffmpeg_path
    from .screenshot import capture_screenshot

    cfg = load_config()
    thumb_cfg = cfg.get('recording', {}).get('live_thumbnail', {})
    if not thumb_cfg.get('enabled', True):
        return
    seg = None
    for cand in RecordingSegment.query.filter_by(recording_id=recording_id)\
            .order_by(RecordingSegment.segment_number.desc()).all():
        if cand.file_path and os.path.exists(cand.file_path) and os.path.getsize(cand.file_path) > 0:
            seg = cand
            break
    if seg is None:
        return
    from .storage_dirs import THUMBNAILS, image_dir
    thumb_dir = image_dir(cfg, THUMBNAILS)
    try:
        os.makedirs(thumb_dir, exist_ok=True)
    except OSError as exc:
        log.warning('Recording %d: cannot create thumbnail dir %s: %s', recording_id, thumb_dir, exc)
        return
    thumb_path = os.path.join(thumb_dir, f'{recording_id}.jpg')
    tmp_path = os.path.join(thumb_dir, f'{recording_id}.{uuid.uuid4().hex}.tmp.jpg')
    ffmpeg_path = resolve_ffmpeg_path(cfg.get('ffmpeg', {}).get('path', 'ffmpeg'))
    ok = capture_screenshot(
        seg.file_path, tmp_path, ffmpeg_path,
        seek_args=['-sseof', '-3'],
        timeout=thumb_cfg.get('capture_timeout_seconds', 12),
    )
    if ok:
        try:
            os.replace(tmp_path, thumb_path)
        except OSError as exc:
            log.warning('Recording %d: final thumbnail replace failed: %s', recording_id, exc)
    else:
        log.info('Recording %d: final thumbnail capture failed (seg %d)',
                 recording_id, seg.segment_number)


def delete_files(paths) -> int:
    """Unlink each existing path, best-effort. A failure on one path is logged and
    skipped (never aborts the rest). Returns the number of files actually removed.
    Callers must run this OUTSIDE any retry_on_locked closure - it's a non-idempotent
    side effect that must fire only after the DB delete has durably committed."""
    removed = 0
    for path in paths:
        try:
            if path and os.path.exists(path):
                os.remove(path)
                removed += 1
        except OSError as exc:
            log.warning('Could not delete file %s: %s', path, exc)
    return removed


def kill_all_active():
    """Terminate every ffmpeg process this process currently owns.

    Called from the shutdown signal handler so a killed/restarted run.py
    process never leaves an orphaned ffmpeg child writing to a segment file
    the next process doesn't know about. Deliberately does no DB work (unsafe
    from a signal handler) - the next process's resume_recording() already
    closes out any segment row left with ended_at IS NULL.

    stop_event is set first, before any kill: it is the app-wide "this death was
    deliberate" flag, and the watchdog now treats an exited capture process as a
    dead feed within one poll (app/watchdog.py). Without it, shutting down would
    look like a stall and the watchdog would spawn a replacement ffmpeg on the way
    out - precisely the orphan this function exists to prevent. Setting an Event
    takes no DB and no app context, so it is as signal-handler-safe as the _lock
    this function already takes.
    """
    with _lock:
        states = list(_active.values())
    for state in states:
        state.stop_event.set()
    for state in states:
        proc = state.process
        if proc and proc.poll() is None:
            log.info('Shutdown: terminating ffmpeg pid=%d', proc.pid)
            proc.terminate()
    for state in states:
        proc = state.process
        if proc and proc.poll() is None:
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                log.warning('Shutdown: ffmpeg pid=%d did not exit, killing', proc.pid)
                proc.kill()


def get_state(recording_id: int) -> Optional[RecordingState]:
    with _lock:
        return _active.get(recording_id)


def _claim_live_state(recording_id: int, state: RecordingState) -> bool:
    """Register `state` as this recording's one live state, or refuse because another
    caller already registered one. Decides and registers under a single lock.

    The in-memory half of "a start has exactly one owner" (dev/changelog/987). A plain
    assignment here is a silent overwrite: when two callers start the same recording, the
    second one's entry displaced the first, leaving the first caller's ffmpeg and watchdog
    reachable from nothing at all - no teardown path, no kill at shutdown, and the
    connection slot held for the life of the process.
    """
    with _lock:
        if recording_id in _active:
            return False
        _active[recording_id] = state
        return True


def _release_live_state(recording_id: int, state: RecordingState) -> None:
    """Drop `state` from _active, but only while it is still the registered one.

    The identity test is what makes this safe on a loser's unwind path: popping by id alone
    would delete whichever state the WINNER registered, which is the exact damage
    _claim_live_state exists to prevent.
    """
    with _lock:
        if _active.get(recording_id) is state:
            del _active[recording_id]


def _claim_status_row(recording_id: int, from_statuses, to_status: str) -> bool:
    """Compare-and-swap Recording.status: move the row to `to_status` only if it is still
    one of `from_statuses`, and report whether this caller is the one that moved it.

    A conditional UPDATE rather than a read-then-write, because a read-then-write is not a
    claim: two sessions can both read SCHEDULED and both write IN_PROGRESS, and neither
    learns it lost. SQLite serializes the writes, so the second UPDATE here sees the first
    one's committed status and matches zero rows.

    Must be called from inside a retry_on_locked closure that owns the commit - this
    function deliberately does not commit, so the claim and everything the caller writes
    alongside it land in one transaction.
    """
    claimed = Recording.query.filter(
        Recording.id == recording_id,
        Recording.status.in_(tuple(from_statuses)),
    ).update({Recording.status: to_status}, synchronize_session='fetch')
    return bool(claimed)


def get_live_segment_path(recording_id: int) -> Optional[str]:
    """Return the file path of the currently-open segment for an active recording.

    Returns None if the recording isn't in _active (not IN_PROGRESS - e.g. also
    None while PAUSED, since pause_recording() pops it from _active), or if the
    current segment's DB row doesn't exist yet (e.g. the instant after a fresh
    ffmpeg launch, before RecordingSegment is committed).
    """
    state = get_state(recording_id)
    if state is None:
        return None
    seg = (RecordingSegment.query
           .filter_by(recording_id=recording_id, segment_number=state.current_segment_num)
           .first())
    if seg is None or not seg.file_path:
        return None
    return seg.file_path


def _busy_channel_ids(exclude_recording_id: int) -> set:
    """Channel ids that another IN_PROGRESS recording is currently using - the
    busy-member skip rule (2026-07-20): group member selection prefers a
    free member over one that would preempt/handoff a live recording, but will
    knowingly take a busy member when no alternative active member exists."""
    rows = (db.session.query(Recording.channel_id)
            .filter(Recording.status == REC_STATUS_IN_PROGRESS,
                    Recording.id != exclude_recording_id,
                    Recording.channel_id.isnot(None)).all())
    return {r[0] for r in rows}


def _busy_account_channel_ids(members, exclude_recording_id: int) -> set:
    """Of `members`, the channel ids whose account has no free connection slot right now.

    The account-level sibling of the busy-channel rule above, and the same shape: prefer
    a member the recording can start on immediately, fall back to one it cannot when
    nothing else survives. Since the connection limit became a hard ceiling
    (dev/changelog/854) a member on a full account no longer risks the provider account -
    it costs capture time, because the start waits for a slot instead of connecting. So a
    group spanning several accounts rolls over to an idle one rather than queueing behind
    a busy one, and waiting is what is left when every eligible member is on a full
    account (dev/changelog/855).

    The recording's own slot is discounted: at failover it already holds one on its
    current account, and a same-account swap keeps that slot rather than competing for it.
    """
    from . import connection_limits as connlim
    full = connlim.accounts_without_free_recording_slot(
        {ch.account_id for ch in members},
        exclude_holder=('recording', exclude_recording_id))
    return {ch.id for ch in members if ch.account_id in full}


def start_recording(app, recording_id: int):
    """Transition a SCHEDULED recording to IN_PROGRESS and launch ffmpeg."""
    with app.app_context():
        rec = db.session.get(Recording, recording_id)
        if rec is None:
            log.error('start_recording: recording %d not found', recording_id)
            return
        if rec.status not in (REC_STATUS_SCHEDULED,):
            log.warning('start_recording: recording %d already %s, skipping', recording_id, rec.status)
            return

        # mp4 conversion collision avoidance, 'wait' policy only (recording.post_process.
        # collision_policy). 'cancel' needs no check here - a live conversion yields to
        # this recording on its own (app/postprocessor.py's own poll loop) without ever
        # delaying the start; DESIGN-concurrency.md's "recordings always win" doctrine
        # predates conversion as an actor, so this extends it rather than reopening it.
        # Checked before any of the group-member-resolution work below, so a defer/fail
        # doesn't waste it - a retry re-enters this function and redoes it fresh.
        collision_policy = load_config()['recording']['post_process'].get('collision_policy', 'cancel')
        if collision_policy == 'wait':
            from . import postprocessor
            if postprocessor.has_active_conversion():
                now = datetime.utcnow()
                if rec.stop_time <= now:
                    log.error('Recording "%s" (#%d): stop time already passed while waiting '
                              'for a live mp4 conversion to finish - giving up', rec.name, recording_id,
                              extra={'recording_id': recording_id, 'already_alerted': True})

                    @retry_on_locked()
                    def _fail_conversion_collision_and_commit():
                        r = db.session.get(Recording, recording_id)
                        r.status = REC_STATUS_FAILED
                        r.completed_at = now
                        r.failure_reason = FAILURE_CONVERSION_COLLISION
                        add_recording_event(
                            recording_id, RECORDING_FAILED,
                            detail="Never started: waited for a live mp4 conversion to finish "
                                   "(recording.post_process.collision_policy: wait), but the "
                                   "recording's own stop time passed first")
                        db.session.commit()

                    _fail_conversion_collision_and_commit()
                    from .health_score import dismiss_recording_failing_alerts
                    dismiss_recording_failing_alerts(recording_id)
                    end_slot_wait(recording_id)
                    from .alerts import create_alert
                    create_alert(
                        'RECORDING_FAILED_CONVERSION_COLLISION',
                        f'Recording failed: {rec.name}',
                        body="Waited for a live mp4 conversion to finish (collision_policy: "
                             "wait), but the recording's own window ended first.",
                        source='recorder', recording_id=recording_id,
                    )
                    return

                if not RecordingEvent.query.filter_by(
                        recording_id=recording_id, event_type=RECORDING_START_DEFERRED).first():
                    @retry_on_locked()
                    def _mark_deferred_and_commit():
                        add_recording_event(
                            recording_id, RECORDING_START_DEFERRED,
                            detail="Start deferred: a live mp4 conversion is using local "
                                   "resources (recording.post_process.collision_policy: "
                                   "wait) - will retry once it's clear.")
                        db.session.commit()

                    _mark_deferred_and_commit()

                log.info('Recording %d: deferring start - a live mp4 conversion is running '
                         '(collision_policy: wait)', recording_id)
                from .scheduler import reschedule_recording_start
                reschedule_recording_start(recording_id, now + timedelta(seconds=30))
                return

        # Group-backed recording: re-resolve the best member NOW (scores may have
        # shifted since creation) and point channel_id/url at it before anything
        # else reads them (handoff check, slot acquisition, ffmpeg launch).
        if rec.group_id is not None and rec.group is not None:
            cfg = load_config()
            streak_threshold = cfg.get('channel_testing', {}).get(
                'failing_streak_threshold', DEFAULT_FAILING_STREAK_THRESHOLD)
            # Recording-enabled members only - a member the user has not enabled for
            # recording is not a failover candidate
            # (app/channel_groups.py::recording_members).
            members = recording_members(rec.group.memberships)
            from .routes.channel_tests import _latest_tests_by_channel
            latest_by_channel = _latest_tests_by_channel([ch.id for ch in members])
            # Format lock filters, health score ranks (DESIGN-channel-groups-model.md 5).
            # A member whose measured format differs from the lock is skipped HERE, where
            # the member is chosen - never by unticking its Recording switch, which is
            # user intent and is written by a human alone (4.1).
            selection = format_eligible_members(rec.group, members, latest_by_channel)
            members = selection.members
            busy = _busy_channel_ids(recording_id)
            # Two preferences, relaxed one at a time and both applied HERE, after the
            # format filter - so neither can empty the candidate list ahead of the
            # format lock's own zero-survivors override (dev/changelog/754).
            busy_account = _busy_account_channel_ids(members, recording_id)
            best = pick_best_member(members, latest_by_channel,
                                    exclude_ids=busy | busy_account,
                                    streak_threshold=streak_threshold)
            if best is None:
                # Nothing on an account with a free slot. Take a free *channel* on a
                # full account and wait for the slot rather than handing off a live
                # recording - the pre-rollover behavior, unchanged.
                best = pick_best_member(members, latest_by_channel, exclude_ids=busy,
                                        streak_threshold=streak_threshold)
            if best is None:
                # No free active member - knowingly take the busy one (the handoff
                # check below gracefully stops the recording occupying it).
                best = pick_best_member(members, latest_by_channel, streak_threshold=streak_threshold)
            # Derived from what was actually picked, not from which branch ran: one fact,
            # read the same way by the note and by the event's extra_data.
            took_busy = best is not None and best.id in busy
            took_busy_account = best is not None and best.id in busy_account
            if best is not None:
                from .accounts import normalize_url
                skipped_busy = [ch for ch in members if ch.id in busy and ch.id != best.id]
                skipped_busy_account = [ch for ch in members if ch.id in busy_account
                                        and ch.id not in busy and ch.id != best.id]
                if took_busy:
                    busy_note = (' - channel is busy with another recording but no free '
                                 'active channel exists; taking it (same-channel handoff)')
                elif skipped_busy:
                    busy_note = (' - skipped busy channel(s): '
                                 + ', '.join(f'"{ch.name}"' for ch in skipped_busy))
                else:
                    busy_note = ''
                if took_busy_account:
                    busy_note += (f' - no member on an account with a free connection slot, '
                                  f'so the start waits for one on "{best.account.name}"')
                elif skipped_busy_account:
                    busy_note += (' - rolled over to a free account, past: '
                                  + ', '.join(f'"{ch.name}"' for ch in skipped_busy_account))

                # The group's lock left nothing eligible and we are recording anyway
                # (DESIGN-channel-groups-model.md 15.2). Principle 2 says complete the
                # recording; principle 1 says never hide the damage - so the fact rides
                # on the recording itself, not only on an alert the user may never open.
                override_detail = None
                if selection.override:
                    got = format_label(format_key(latest_by_channel.get(best.id)))
                    override_detail = (
                        f'Recorded from "{best.name}" at {got}. This group is locked to '
                        f'{format_label(selection.reference)} and no member matched.')

                @retry_on_locked()
                def _select_group_member_and_commit():
                    rec.channel_id = best.id
                    rec.url = normalize_url(best.stream_url, best.account)
                    add_recording_event(recording_id, GROUP_MEMBER_SELECTED,
                        detail=(f'Group "{rec.group.name}": recording from "{best.name}" '
                                f'({best.account.name}, score {effective_score(best)})'
                                f'{busy_note}'),
                        extra={'group_id': rec.group_id, 'channel_id': best.id,
                               'effective_score': effective_score(best),
                               'took_busy': took_busy,
                               'skipped_busy_channel_ids': [ch.id for ch in skipped_busy],
                               'took_busy_account': took_busy_account,
                               'skipped_busy_account_channel_ids': [
                                   ch.id for ch in skipped_busy_account]})
                    if override_detail:
                        add_recording_event(recording_id, RECORDING_FORMAT_OVERRIDE,
                            detail=override_detail,
                            extra={'group_id': rec.group_id, 'channel_id': best.id,
                                   'locked_format': list(selection.reference),
                                   'recorded_format': list(
                                       format_key(latest_by_channel.get(best.id)) or [])})
                    db.session.commit()

                _select_group_member_and_commit()

        # Case 2: if a recording on this same channel - or the same channel group
        # (two group recordings may have resolved to different members) - is already
        # running, this is a planned handoff, not a conflict - gracefully stop it.
        handoff_from_id = None
        if rec.channel_id is not None or rec.group_id is not None:
            same_source = []
            if rec.channel_id is not None:
                same_source.append(Recording.channel_id == rec.channel_id)
            if rec.group_id is not None:
                same_source.append(Recording.group_id == rec.group_id)
            old = Recording.query.filter(
                db.or_(*same_source),
                Recording.status == REC_STATUS_IN_PROGRESS,
                Recording.id != recording_id,
            ).first()
            if old is not None:
                log.info('Recording %d: same-channel handoff from recording %d', recording_id, old.id)
                _handoff_stop_old_recording(app, old.id, recording_id)
                handoff_from_id = old.id

        cfg = load_config()
        dvr_dir = cfg['recording']['dvr_output_dir']
        # Not os.path.isdir(): it answers False for a stale mount too, so a dead NAS
        # used to fail the recording with "does not exist" and send the operator off to
        # mkdir a directory that was already there (dev/changelog/723).
        dvr_probe = probe_dir(dvr_dir)
        if dvr_probe.outcome != PATH_OK:
            dvr_reason = describe_dir_problem(dvr_dir, dvr_probe)
            log.error('Cannot start recording "%s" (#%d): DVR output directory %s', rec.name, recording_id, dvr_reason,
                      extra={'recording_id': recording_id})

            @retry_on_locked()
            def _mark_missing_dvr_dir_and_commit():
                rec.status = REC_STATUS_FAILED
                rec.completed_at = datetime.utcnow()
                rec.failure_reason = FAILURE_DVR_DIR_UNUSABLE
                add_recording_event(recording_id, RECORDING_FAILED,
                                    detail=f'DVR output directory {dvr_reason}')
                db.session.commit()

            _mark_missing_dvr_dir_and_commit()
            from .health_score import dismiss_recording_failing_alerts
            dismiss_recording_failing_alerts(recording_id)
            end_slot_wait(recording_id)
            return

        account_id = rec.channel.account_id if (rec.channel_id and rec.channel) else None
        if account_id is not None:
            if not _try_acquire_slot_with_preemption(app, recording_id, account_id):
                _defer_start_for_slot(app, recording_id, account_id)
                return
            # Fairness, checked only now that a slot is actually held: yielding on the
            # mere existence of an earlier waiter would defer this recording even when
            # the account has slots to spare. at_limit() is advisory and racy by its own
            # docstring, which is exactly what it is used for here - it orders two
            # waiters, it never decides whether this recording may connect. The acquire
            # above already decided that.
            #
            # A handoff is exempt: its predecessor released this very slot moments ago at
            # :404, and yielding it to a third recording would leave the gap the handoff
            # exists to avoid.
            if handoff_from_id is None:
                from . import connection_limits as connlim
                ahead = _earlier_waiter_for_slot(recording_id, account_id, rec.start_time)
                if ahead is not None and connlim.at_limit(account_id):
                    connlim.release(account_id, 'recording', recording_id)
                    _defer_start_for_slot(app, recording_id, account_id, waiting_on=ahead)
                    return

        # Registration comes BEFORE the status claim, and abort_recording commits ABORTED
        # before ITS teardown. The two orderings interlock, and that pairing is what closes
        # the window rather than either edit on its own: either the abort's status write
        # lands first, in which case the claim below refuses and nothing spawns, or the
        # claim wins, in which case this registration is already visible to the teardown
        # that necessarily follows the abort's write (dev/changelog/987).
        #
        # Nothing is released on this refusal: the caller holding the live state holds the
        # slot too, under the same idempotent (recording, id) key this one just re-acquired.
        state = RecordingState()
        if not _claim_live_state(recording_id, state):
            log.warning('start_recording: recording %d already has live state - another '
                        'caller owns this capture, so this start does nothing', recording_id)
            return

        @retry_on_locked()
        def _claim_in_progress_and_commit():
            """Claim the row SCHEDULED -> IN_PROGRESS, and return whether this caller won.

            The status check at the top of this function is not the claim: everything
            between it and here - group member resolution, the handoff query, the DVR probe,
            the slot acquire - runs while the row is still SCHEDULED, so a second caller
            passes that same check and arrives here too. Measured, not theorized: an overdue
            start_<id> job dispatched by APScheduler's first pass after a restart and the
            startup sweep's own case 2b both reached a launch on every run
            (dev/changelog/987).
            """
            if not _claim_status_row(recording_id, (REC_STATUS_SCHEDULED,),
                                     REC_STATUS_IN_PROGRESS):
                db.session.rollback()
                return False
            r = db.session.get(Recording, recording_id)
            now = datetime.utcnow()
            r.started_at = now
            if r.start_time < now and (now - r.start_time).total_seconds() > 60:
                late_secs = (now - r.start_time).total_seconds()
                log.warning('Recording %d: starting %.0fs after scheduled start (system delay); adjusting start_time',
                            recording_id, late_secs)
                from .database import RECORDING_STARTED_LATE
                add_recording_event(recording_id, RECORDING_STARTED_LATE,
                                    detail=f'Started {late_secs/60:.1f} min after scheduled time due to system delay; start_time adjusted to actual start')
                r.start_time = now
            if handoff_from_id is not None:
                add_recording_event(recording_id, RECORDING_HANDOFF,
                                    detail=f'Started via handoff from recording #{handoff_from_id} on the same channel')
            _snapshot_channel_health(r)
            db.session.commit()
            return True

        if not _claim_in_progress_and_commit():
            _abandon_unclaimed_start(recording_id, state, account_id)
            return

        from .health_score import dismiss_recording_failing_alerts
        dismiss_recording_failing_alerts(recording_id)
        end_slot_wait(recording_id)

        # Re-read the program's synopsis/genre/rating from the guide now that the channel
        # is settled: a recording scheduled days ago may be describing a program the
        # provider has since revised (dev/changelog/1055). Its own DB unit, deliberately
        # kept out of everything around it - and it never raises into the start path,
        # because a description is a convenience and Product Principle 2 does not let one
        # cost a capture.
        try:
            from .recording_metadata import refresh_from_guide
            refresh_from_guide(recording_id)
        except SQLAlchemyError:
            log.warning('Recording %d: could not refresh the program details at record '
                        'start - keeping what was captured when it was scheduled',
                        recording_id, exc_info=True)
            # The launch below shares this session, so a half-failed unit has to be put
            # back rather than carried into it.
            db.session.rollback()

        # 1-based segment numbering for new recordings (DESIGN.md section 5 -
        # display matches filenames); pre-existing recordings keep 0-based files.
        _launch_segment(app, recording_id, seg_num=1)


def _abandon_unclaimed_start(recording_id: int, state: RecordingState, account_id):
    """Unwind a start (or resume) that lost its status claim: drop the live state this
    caller registered and, when nothing else is running the recording, give the connection
    slot back.

    The slot release is conditional on purpose. connection_limits.try_acquire is idempotent
    on (holder_kind, holder_id), so two callers starting ONE recording share a single slot
    entry - a loser releasing unconditionally would strip the WINNER's slot while the
    winner's ffmpeg is still connected, and the account would then oversubscribe. So the
    states are told apart rather than lumped: IN_PROGRESS means another caller owns both the
    capture and the slot and this one touches neither; anything else - ABORTED by a cancel
    that raced this start, FAILED, or a row deleted underneath it - means no other teardown
    will ever run and the slot is this caller's to release.
    """
    _release_live_state(recording_id, state)
    rec = db.session.get(Recording, recording_id)
    status = rec.status if rec is not None else None
    log.warning('Recording %d: the start claim was lost (row is %s) - abandoning this '
                'start without launching', recording_id, status or 'gone')
    if status != REC_STATUS_IN_PROGRESS and account_id is not None:
        from . import connection_limits as connlim
        connlim.release(account_id, 'recording', recording_id)


def _segment_file_is_growing(path: str, wait_seconds: float = 2.0) -> bool:
    """Read-only liveness probe: True if `path` grew between two size samples taken
    wait_seconds apart. Pure stat() reads - never opens or touches the file - so it
    cannot interfere with whatever process might be writing it (CLAUDE.md: a diagnostic
    must never be able to harm the capture it is diagnosing). False (not growing) for a
    missing path or a file that can't be stat'd, which keeps this a no-op for the
    ordinary post-crash case where nothing is writing any more."""
    if not path:
        return False
    try:
        before = os.path.getsize(path)
    except OSError:
        return False
    time.sleep(wait_seconds)
    try:
        after = os.path.getsize(path)
    except OSError:
        return False
    return after > before


def _find_open_segment(recording_id: int):
    """The highest-numbered segment row still left open (ended_at IS NULL), or None.

    An open row means the process that owned the capture never got to close it - either
    it is still running right now, or it died without a graceful shutdown. Only the
    caller's own liveness probe can tell those apart (_segment_file_is_growing)."""
    return RecordingSegment.query.filter_by(
        recording_id=recording_id, ended_at=None
    ).order_by(RecordingSegment.segment_number.desc()).first()


def _capture_stopped_at(seg) -> datetime:
    """Best evidence for when this segment's capture actually stopped.

    The file's mtime is ffmpeg's own last write; utcnow() is only when *we* noticed. The
    two agree when the app was restarted promptly, and they can be an hour apart when the
    host itself went down and nothing came back up until well after the recording's stop
    time (dev/docs/BUGS.md 2026-08-17). Writing "when we noticed" into ended_at inflates
    the segment's rendered duration by exactly the length of the outage, which is the
    "number the user cannot explain" case Product Principle 1 exists to prevent.

    Clamped to [started_at, now]: an mtime outside that window is describing something
    other than this capture (a restored file, a clock that moved), so fall back to now."""
    now = datetime.utcnow()
    if not seg.file_path:
        return now
    try:
        mtime = datetime.utcfromtimestamp(os.path.getmtime(seg.file_path))
    except OSError:
        return now
    if seg.started_at and mtime < seg.started_at:
        return now
    return min(mtime, now)


def _refuse_open_segment_still_growing(recording_id: int, open_seg, action: str):
    """Shared refusal for "another live process is still writing this segment".

    Two independent create_app() calls against the same DB (e.g. an ad-hoc diagnostic
    script run against production - dev/docs/BUGS.md 2026-08-14) each start with an empty
    in-process _active dict, so nothing in memory can tell that another process owns this
    recording. The file's own growth is the only signal visible across processes.

    `action` names what was refused ('resume', 'concatenate'). The condition and the
    remedy are identical either way: touching these rows or these files while another
    process is mid-capture corrupts what it is doing, so nothing is changed here."""
    log.error(
        'Recording %d: segment %d is still growing on disk - refusing to %s (another '
        'process is almost certainly already recording it). No state was changed.',
        recording_id, open_seg.segment_number, action,
        extra={'recording_id': recording_id, 'already_alerted': True})
    from .alerts import create_alert
    create_alert(
        'RECORDING_RESUME_REFUSED',
        title=f'Resume refused - recording #{recording_id} looks already active',
        body=(f'Startup recovery wanted to {action} recording #{recording_id}, but its '
              f'open segment ({open_seg.segment_number}) is still growing on disk, so '
              'another process is almost certainly already recording it. Refused to '
              'close the segment or launch a second capture; no state was changed. '
              'This usually means two app instances are running against the same '
              'database.'),
        source=f'recording:{recording_id}:resume-refused',
        recording_id=recording_id,
    )

    seg_num = open_seg.segment_number

    @retry_on_locked()
    def _log_resume_refused_and_commit():
        add_recording_event(
            recording_id, RECORDING_RESUME_REFUSED,
            detail=f'Segment {seg_num} is still growing on disk - '
                   f'refused to {action} (likely a second live process)',
            segment_number=seg_num)
        db.session.commit()

    _log_resume_refused_and_commit()


def _close_open_segment(recording_id: int, seg_id: int, seg_num: int, detail: str) -> datetime:
    """Finalize a segment row the crash left open, and return its ended_at.

    Caller must already have proven the file is not still growing."""
    @retry_on_locked()
    def _close_open_segment_and_commit():
        seg = db.session.get(RecordingSegment, seg_id)
        seg.ended_at = _capture_stopped_at(seg)
        seg.exit_reason = 'SERVICE_RESTART'
        if seg.file_path and os.path.exists(seg.file_path):
            seg.bytes_recorded = os.path.getsize(seg.file_path)
        add_recording_event(recording_id, SEGMENT_ENDED, detail=detail,
                            segment_number=seg_num)
        db.session.commit()
        return seg.ended_at

    return _close_open_segment_and_commit()


def _abandon_raced_segment(recording_id: int, seg_id: int, seg_num: int, seg_path: str):
    """Release a segment row created after its recording was already torn down.

    Only _launch_segment's `state is None` branch reaches this: the row was committed a moment
    after a concurrent abort/stop had already closed every open segment, so no other path knows
    it exists. Left alone it keeps ended_at NULL forever and the detail page's
    `(seg.ended_at or now)` fallback renders an ever-growing duration for a segment that never
    captured anything, next to a partial .ts the abort's own delete pass had already walked past.

    The caller has already killed ffmpeg, so the file is not still being written when it goes.
    """
    @retry_on_locked()
    def _close_raced_segment_and_commit():
        seg = db.session.get(RecordingSegment, seg_id)
        if seg is None:
            return
        seg.ended_at = datetime.utcnow()
        seg.exit_reason = 'TEARDOWN_RACE'
        add_recording_event(
            recording_id, SEGMENT_ENDED,
            detail=(f'Segment {seg_num} was discarded - the recording was torn down while this '
                    f'segment was launching, so it captured nothing and its file was deleted'),
            segment_number=seg_num)
        db.session.commit()

    _close_raced_segment_and_commit()

    try:
        os.unlink(seg_path)
    except FileNotFoundError:
        pass  # ffmpeg may never have created it - nothing to release
    except OSError as exc:
        log.warning('Recording %d seg %d: could not delete the raced segment file %s: %s',
                    recording_id, seg_num, seg_path, exc)


# Outcome of close_open_segment_after_unclean_stop(). `outcome` is one of the three
# states below and is never inferred from the other fields being None.
OpenSegmentClose = namedtuple('OpenSegmentClose', 'outcome segment_number stopped_at')
OPEN_SEG_NOTHING_OPEN = 'nothing_open'   # no row left open; nothing to do
OPEN_SEG_CLOSED = 'closed'               # row finalized; stopped_at is when capture ended
OPEN_SEG_REFUSED = 'refused'             # file still growing; nothing was changed


def close_open_segment_after_unclean_stop(recording_id: int) -> OpenSegmentClose:
    """Close a segment row left open because the app died mid-capture, without resuming.

    The recovery half of resume_recording() for callers that are NOT going to record any
    more (app/scheduler.py's past-stop-time branch, which goes straight to concatenation).
    Same liveness probe, same exit_reason, same SEGMENT_ENDED event - a segment left open
    by an unclean stop must not stay open just because the recording's window has already
    closed (dev/docs/BUGS.md 2026-08-17).

    Requires an app context. A `refused` outcome means another process owns this
    recording and the caller must not finalize it either."""
    open_seg = _find_open_segment(recording_id)
    if open_seg is None:
        return OpenSegmentClose(OPEN_SEG_NOTHING_OPEN, None, None)

    seg_id, seg_num = open_seg.id, open_seg.segment_number
    if _segment_file_is_growing(open_seg.file_path):
        _refuse_open_segment_still_growing(recording_id, open_seg, 'concatenate')
        return OpenSegmentClose(OPEN_SEG_REFUSED, seg_num, None)

    stopped_at = _close_open_segment(
        recording_id, seg_id, seg_num,
        detail=f'Segment {seg_num} closed at service restart (capture had already stopped)')
    return OpenSegmentClose(OPEN_SEG_CLOSED, seg_num, stopped_at)


def resume_recording(app, recording_id: int):
    """Resume an IN_PROGRESS, PAUSED, or RETRYING recording."""
    with app.app_context():
        rec = db.session.get(Recording, recording_id)
        if rec is None:
            return

        # Look for a segment left "open" (no ended_at) BEFORE touching status or launching
        # anything, and probe it read-only before assuming this is a genuine post-crash
        # resume - see _refuse_open_segment_still_growing() for why growth is the only
        # signal available here.
        open_seg = _find_open_segment(recording_id)
        if open_seg and _segment_file_is_growing(open_seg.file_path):
            _refuse_open_segment_still_growing(recording_id, open_seg, 'resume')
            return

        # The slot is taken BEFORE any status write, not after it as this did until
        # dev/changelog/854. A refusal now has nothing to unwind: the recording stays
        # PAUSED / RETRYING / IN_PROGRESS-awaiting-resume exactly as it was, and the
        # re-armed resume job re-enters here. Acquiring after the flip would leave a
        # recording marked IN_PROGRESS with no slot and no ffmpeg.
        account_id = rec.channel.account_id if (rec.channel_id and rec.channel) else None
        if account_id is not None:
            if not _try_acquire_slot_with_preemption(app, recording_id, account_id):
                _defer_resume_for_slot(app, recording_id, account_id)
                return

        # Same ownership pairing as start_recording: the live state is registered BEFORE any
        # status write, so an abort - which commits ABORTED before its own teardown - either
        # loses the claim below or finds this state and stops it (dev/changelog/987). The
        # segment number is filled in further down, once the recording's own segment rows
        # have been read; nothing reads it before _launch_segment sets it for real.
        #
        # Nothing is released on this refusal: the caller holding the live state holds the
        # slot too, under the same idempotent (recording, id) key this one just re-acquired.
        state = RecordingState()
        if not _claim_live_state(recording_id, state):
            log.warning('resume_recording: recording %d already has live state - another '
                        'caller owns this capture, so this resume does nothing', recording_id)
            return

        if rec.status == REC_STATUS_PAUSED:
            @retry_on_locked()
            def _claim_resumed_and_commit():
                from .database import RECORDING_RESUMED
                if not _claim_status_row(recording_id, (REC_STATUS_PAUSED,),
                                         REC_STATUS_IN_PROGRESS):
                    db.session.rollback()
                    return False
                add_recording_event(recording_id, RECORDING_RESUMED, detail='Recording manually resumed from pause')
                db.session.commit()
                return True

            if not _claim_resumed_and_commit():
                _abandon_unclaimed_start(recording_id, state, account_id)
                return
        elif rec.status == REC_STATUS_RETRYING:
            @retry_on_locked()
            def _claim_retry_resumed_and_commit():
                from .database import RECORDING_RESUMED
                if not _claim_status_row(recording_id, (REC_STATUS_RETRYING,),
                                         REC_STATUS_IN_PROGRESS):
                    db.session.rollback()
                    return False
                r = db.session.get(Recording, recording_id)
                r.next_retry_at = None
                add_recording_event(recording_id, RECORDING_RESUMED,
                                    detail=f'Retry attempt {r.dead_stream_retry_count} - reconnecting')
                db.session.commit()
                return True

            if not _claim_retry_resumed_and_commit():
                _abandon_unclaimed_start(recording_id, state, account_id)
                return

        end_slot_wait(recording_id)

        # Next segment number comes from this recording's OWN segment rows. Deriving it
        # from a directory listing of `{safe_name}_seg_*` instead - as this did until
        # dev/changelog/643 - counted a same-named sibling recording's segment files into
        # this recording's numbering, because Recording.name is not unique and nothing at
        # create time makes it so.
        highest = db.session.query(db.func.max(RecordingSegment.segment_number)).filter(
            RecordingSegment.recording_id == recording_id).scalar()
        next_seg = (highest or 0) + 1

        # Close out the segment found open above, if any (left that way by the crash).
        if open_seg:
            _close_open_segment(
                recording_id, open_seg.id, open_seg.segment_number,
                detail=f'Segment {open_seg.segment_number} closed at service restart')

        state.current_segment_num = next_seg
        _launch_segment(app, recording_id, seg_num=next_seg)


def fire_dead_stream_retry(app, recording_id: int):
    """Target of the retry_<id> APScheduler job (app/scheduler.py::schedule_dead_stream_retry) -
    fires after a dead-stream backoff wait. No-ops if the recording moved on in the meantime
    (aborted/deleted, or somehow already resumed by another path) - status is re-checked fresh
    rather than assumed from when the job was scheduled, since a wait can be up to an hour.

    Does not relaunch if the recording's own scheduled window ended during the wait - the retry
    budget having attempts left does not mean there is still anything left to record. That case
    goes through stop_recording(), the same place the stop job lands, so what was captured
    before the stream died is joined rather than failed. Otherwise reuses resume_recording(), the same "bring a non-running recording back to
    IN_PROGRESS" path already used for a PAUSED resume and for crash recovery.
    """
    with app.app_context():
        rec = db.session.get(Recording, recording_id)
        if rec is None or rec.status != REC_STATUS_RETRYING:
            return
        if rec.stop_time <= datetime.utcnow():
            stop_recording(app, recording_id)
            return
        resume_recording(app, recording_id)


def _reresolve_channel_url(recording_id: int, cfg: dict) -> None:
    """Repoint a channel-backed recording's frozen url at its channel's current stream_url.

    Providers rewrite their stream domain and/or the creds embedded in every stream URL with
    no notice. Sync repairs the Channel row in place (identity is matched on stream_id, not
    URL), but Recording.url is snapshotted at creation, so without this a SCHEDULED recording
    made before the drift records from the dead old URL and stall-loops to FAILED.

    Called from _launch_segment because that is the ONLY consumer of rec.url - which makes
    this one call site cover record start, service-restart resume, and every watchdog segment
    relaunch (so a sync landing mid-recording rescues a stalling one). Group-backed recordings
    are excluded: they re-resolve their member at start and have failover_group_member() for
    the mid-recording case, and both write rec.url themselves. Manual URL-only recordings
    (channel_id None) have no channel to resolve against and are left alone.
    """
    rec = db.session.get(Recording, recording_id)
    if rec is None or rec.group_id is not None or rec.channel_id is None:
        return
    channel = rec.channel
    if channel is None or not channel.stream_url:
        return

    from .accounts import normalize_url
    fresh = normalize_url(channel.stream_url, channel.account, cfg)
    if fresh == rec.url:
        return

    stale = rec.url

    @retry_on_locked()
    def _apply_resolved_url_and_commit():
        r = db.session.get(Recording, recording_id)
        r.url = fresh
        add_recording_event(recording_id, RECORDING_URL_RERESOLVED,
            detail=(f'Stream URL for channel "{channel.name}" changed since this recording '
                    f'was created; recording from the channel\'s current URL '
                    f'({mask_creds(stale)} -> {mask_creds(fresh)})'),
            extra={'channel_id': channel.id,
                   'old_url': mask_creds(stale),
                   'new_url': mask_creds(fresh)})
        db.session.commit()

    _apply_resolved_url_and_commit()
    log.info('Recording %d: re-resolved stale stream URL %s -> %s',
             recording_id, mask_creds(stale), mask_creds(fresh))


#: Filename shape of a capture's stderr spool. Spelled once, here, because two different
#: reapers match it and a pattern that drifts between them either misses live spools or
#: deletes files in a directory the user also keeps their own things in.
STDERR_SPOOL_PREFIX = '.cap-stderr-'


def _stderr_spool_glob(log_dir: str, recording_id=None) -> str:
    """Glob matching this recording's stderr spools, or every recording's when given None."""
    who = f'{recording_id}-' if recording_id is not None else ''
    return os.path.join(log_dir, f'{STDERR_SPOOL_PREFIX}{who}*.log')


def sweep_stale_stderr_spools(app):
    """Delete every capture stderr spool left behind by a process that is gone.

    Called from init_scheduler(), and ONLY from there: it deletes by directory listing, so
    it cannot tell a dead process's leftover from a live process's open spool, and the
    pidfile claim just above its call site is what establishes that no other process owns
    any of them. It lived in create_app() until dev/changelog/967, where "no capture can be
    running yet" was true for the serving process and false for every other caller - a
    second app build against the real config (an ad-hoc read-only check while the service
    is up) deleted a live recording's spool with no error anywhere, and cost that segment
    its capture-ended diagnostics for good.

    Without it they accumulate forever for any recording that never resumes: kill_all_active
    runs in a signal handler and deliberately does no cleanup.

    Never raises. Losing the sweep costs disk, not a recording.
    """
    log_dir = app.config.get('CAPTURE_LOG_DIR')
    if not log_dir:
        return
    for stale in glob.glob(_stderr_spool_glob(log_dir)):
        try:
            os.unlink(stale)
        except OSError:
            pass  # best-effort; a stale spool is inert, and per-attempt tokens make it unreadable as a live one


def _open_segment_stderr_spool(app, recording_id: int, seg_num: int):
    """Open the file this segment's ffmpeg stderr is spooled to; (path, handle) or (None, None).

    The directory comes from app.config, resolved once in create_app() where config_overrides
    are honored - never from a runtime load_config(), which reads the real config.yaml and
    would spool a test's capture stderr into the production directory (BUGS.md 2026-07-18).

    The filename carries a per-attempt token and stale files for this recording are reaped
    first, both copied from postprocessor.py's conversion spool: on a fixed filename a
    shutdown that skips the unlink leaves a file the NEXT attempt reads as its own
    (BUGS.md 2026-07-23). The reap is what covers kill_all_active, which deliberately does no
    cleanup because it runs in a signal handler.

    Never raises. Returning (None, None) costs the diagnostic, not the recording.
    """
    log_dir = app.config.get('CAPTURE_LOG_DIR')
    if not log_dir:
        return None, None
    try:
        # Listed per id, not as a bare f'{recording_id}*' glob - that would let id 6 reap
        # id 64's live spool.
        for stale in glob.glob(_stderr_spool_glob(log_dir, recording_id)):
            try:
                os.unlink(stale)
            except OSError:
                pass  # best-effort reap; the token below is what actually guarantees safety
        path = os.path.join(
            log_dir,
            f'{STDERR_SPOOL_PREFIX}{recording_id}-{seg_num}-{uuid.uuid4().hex[:8]}.log')
        return path, open(path, 'wb')
    except OSError as exc:
        log.warning('Recording %d seg %d: could not open stderr spool in %s: %s - '
                    'capture continues without diagnostics', recording_id, seg_num, log_dir, exc)
        return None, None


def _discard_stderr_spool(path, fh):
    """Close and delete a spool whose contents will never be read. Never raises."""
    if fh is not None:
        try:
            fh.close()
        except OSError:
            pass  # nothing further to do; the unlink below is what matters
    if path:
        try:
            os.unlink(path)
        except OSError:
            pass  # best-effort; the glob reap in _open_segment_stderr_spool collects strays


def collect_segment_diagnostics(recording_id: int):
    """(ffmpeg_exit_code, stderr_tail, reconnects, spool_missing) for the segment that just
    ended; releases its spool.

    Call AFTER terminating the process. poll() is read rather than wait()ed, so a child that
    is somehow still alive yields None - honest, since we genuinely do not know how it ended.
    A negative code is the signal that killed it (-15 = our own SIGTERM), a positive one is
    ffmpeg's own error status; that distinction is the whole point, because it separates
    "we gave up on it" from "it died on us".

    `reconnects` is (count, complete) for the times ffmpeg dropped the connection and opened
    a new one WITHOUT the segment ending - the repair that ffmpeg.read_timeout_seconds exists
    to make possible (dev/changelog/958). It is counted over the whole spool rather than the
    tail because a capture's last 20 stderr lines are almost always 20 progress lines, so a
    segment that reconnected eight times would otherwise be indistinguishable from a clean
    one - which is the opposite of what an in-process repair should cost the user in
    visibility.

    `spool_missing` says the spool this segment was writing to had been deleted by the time
    we came to read it - not that ffmpeg said nothing. The distinction is load-bearing: an
    empty tail reads identically either way, which is how recording 18's segment 1 came to
    have no capture-ended event at all and no trace of why (dev/changelog/967). It is
    deliberately NOT set when no spool was ever opened - that path already logs its own
    warning from _open_segment_stderr_spool and would otherwise raise one event per segment
    for as long as capture_log_dir stays unwritable.

    Idempotent: the spool is forgotten here, so a second call returns ('' for the tail).
    The tail is credential-masked - ffmpeg echoes its input URL on error and those URLs
    carry the provider username and password in the path.

    Never raises. A diagnostic that can break a teardown path is worse than no diagnostic
    (CLAUDE.md Product Principle 2).
    """
    state = get_state(recording_id)
    if state is None:
        return None, '', (0, True), False
    exit_code = state.process.poll() if state.process is not None else None
    path, fh = state.stderr_path, state.stderr_fh
    state.stderr_path, state.stderr_fh = None, None
    if fh is not None:
        try:
            fh.close()
        except OSError:
            pass  # the tail read below still tries; a partial spool is better than none
    tail, reconnects, spool_missing = '', (0, True), False
    if path and not os.path.exists(path):
        # Tested before the read rather than inferred from an empty tail, because
        # read_stderr_tail returns '' for both. Nothing in this process deletes a spool
        # between opening it and here, so this is always another process reaching into
        # capture_log_dir.
        spool_missing = True
        log.warning('Recording %d: stderr spool %s was gone before the segment ended - '
                    'another process deleted it, and this segment has no record of what '
                    'ffmpeg said', recording_id, path)
    elif path:
        tail = mask_creds_in_text(read_stderr_tail(path))
        # Before the unlink, and before the relaunch: the process is already dead, so this
        # cannot touch the capture it is describing, and count_stderr_matches is bounded so
        # it cannot delay the restart.
        reconnects = count_stderr_matches(path)
        try:
            os.unlink(path)
        except OSError:
            pass  # best-effort; the glob reap in _open_segment_stderr_spool collects strays
    return exit_code, tail, reconnects, spool_missing


def record_segment_diagnostics(recording_id: int, seg, exit_code, tail, reconnects=(0, True),
                               spool_missing=False):
    """Write exit_code onto seg and, when there is something to say, add a DIAGNOSTICS event.

    Inserts only - the CALLER commits. This joins the caller's existing retry_on_locked unit
    rather than opening a second one: two commits inside one decorated closure can duplicate
    rows when the first succeeds and the second retries (CLAUDE.md § Agent Behavior).

    The exit code goes in the column and in the event's detail string, NEVER in extra_data -
    a stat with a column does not also live there (CLAUDE.md § Measurements). extra_data
    carries only the stderr tail, which has no column because nothing sorts on a blob. The
    reconnect count has no column either and nothing sorts on it, so it rides the detail
    string where a human reads it.

    Stays quiet when the exit is uninformative: a negative code is a signal we sent, which
    seg.exit_reason already names, so with no stderr to show there is nothing an event would
    add that the segment row does not already say. A segment that reconnected in-process is
    NOT uninformative, however it ended - that is the whole reason the count is collected.

    A LOST spool (spool_missing) is never uninformative either, whatever the exit code: the
    absence of an event is what made recording 18's segment 1 unexplainable, because nothing
    on any surface distinguished "ffmpeg had nothing to say" from "its output was destroyed"
    (dev/changelog/967). Saying so is Product Principle 1 applied to the diagnostic itself.
    """
    if seg is not None:
        seg.ffmpeg_exit_code = exit_code
    reconnect_count, reconnects_complete = reconnects
    if (not tail and not reconnect_count and not spool_missing
            and not (exit_code is not None and exit_code > 0)):
        return
    seg_num = seg.segment_number if seg is not None else None
    if exit_code is None:
        code_str = 'ffmpeg exit code unknown'
    elif exit_code < 0:
        code_str = f'ffmpeg killed by signal {-exit_code}'
    else:
        code_str = f'ffmpeg exited {exit_code}'
    detail = f'Segment {seg_num} capture ended: {code_str}' if seg_num is not None \
        else f'Capture ended: {code_str}'
    if reconnect_count:
        # "at least" only when the scan was truncated, so the ordinary case reads as the
        # fact it is rather than hedging about a bound nobody hit.
        howmany = f'at least {reconnect_count}' if not reconnects_complete else f'{reconnect_count}'
        times = 'time' if reconnect_count == 1 and reconnects_complete else 'times'
        detail += (f'; the stream dropped and ffmpeg reconnected {howmany} {times} during '
                   f'the segment, without restarting the capture')
    if spool_missing:
        detail += ('; ffmpeg\'s output was not captured for this segment - another process '
                   'deleted the file it was being written to, so there is no record of what '
                   'it reported')
    add_recording_event(recording_id, DIAGNOSTICS, detail=detail, segment_number=seg_num,
                        extra={'kind': 'capture_stderr', 'stderr_tail': tail} if tail
                              else {'kind': 'capture_stderr'})


def _launch_segment(app, recording_id: int, seg_num: int) -> str:
    """Spawn a new ffmpeg process for segment seg_num and start the watchdog.

    Returns LAUNCH_SPAWNED, LAUNCH_FAILED, LAUNCH_ABANDONED or LAUNCH_REFUSED - see those
    constants for what each obliges the caller to do. Most callers relaunch and immediately
    hand control back to a loop, so they can ignore it; the watchdog's restart branch
    cannot, because it goes on to judge the launch it just asked for.
    """
    with app.app_context():
        cfg = load_config()
        _reresolve_channel_url(recording_id, cfg)
        rec = db.session.get(Recording, recording_id)
        # IN_PROGRESS is the only status under which a capture may exist, so it is re-read
        # here rather than inherited from whatever the caller saw. A cancel that lands after
        # a start has claimed the row but before this point used to spawn ffmpeg anyway,
        # leaving a watchdog supervising an ABORTED recording with no stop job and no
        # teardown that would ever run (dev/changelog/987). The retry thread in
        # _schedule_launch_retry has always re-read it for the same reason.
        if rec is None or rec.status != REC_STATUS_IN_PROGRESS:
            log.warning('Recording %d: refusing to launch segment %d - the recording is %s, '
                        'not IN_PROGRESS', recording_id, seg_num,
                        rec.status if rec is not None else 'gone')
            return LAUNCH_REFUSED
        dvr_dir = cfg['recording']['dvr_output_dir']
        safe_name = _safe_name(rec.name)
        # The recording id is in the filename because Recording.name is not unique: two
        # same-named recordings capturing at once would otherwise be handed the identical
        # segment path and run two ffmpegs writing the same file (dev/changelog/643). It is
        # safe to expose here in a way it is not for the final output - segments are
        # internal, and a successful concat deletes them.
        seg_path = os.path.join(dvr_dir, f'{safe_name}_{recording_id}_seg_{seg_num:03d}.ts')

        # -re is mandatory when segment_duration_seconds bounds this segment (-t is a
        # content-time limit, dev/changelog/437); otherwise it is the channel's setting, the
        # Settings default, or the watchdog's automatic pacing for this run - see
        # resolve_capture_pacing and dev/changelog/997.
        segment_duration = cfg['recording']['segment_duration_seconds']
        pace_state = get_state(recording_id)
        channel = rec.channel if rec.channel_id else None
        pace, pace_source = resolve_capture_pacing(
            cfg, channel.pace_realtime if channel is not None else None, segment_duration,
            pace_state is not None and rec.channel_id in pace_state.auto_paced_channel_ids)
        cmd = build_capture_cmd(cfg, rec.url, seg_path, segment_duration,
                                pace_realtime=pace)
        log.info('Recording %d seg %d: %s', recording_id, seg_num,
                 mask_creds_in_text(' '.join(cmd)))

        stderr_path, stderr_fh = _open_segment_stderr_spool(app, recording_id, seg_num)

        try:
            proc = subprocess.Popen(
                cmd,
                stdout=subprocess.DEVNULL,
                # Never a pipe: an undrained 64KB pipe buffer deadlocked every recording in
                # this app at ~7.8 minutes (CLAUDE.md subprocess discipline). A plain file
                # needs no reader thread and cannot block the child. DEVNULL when the spool
                # could not be opened - capture always outranks its own diagnostics.
                stderr=(stderr_fh or subprocess.DEVNULL),
            )
        except Exception as exc:
            log.error('Recording "%s" (#%d): failed to launch ffmpeg: %s', rec.name, recording_id, exc,
                      extra={'recording_id': recording_id})
            # No child was spawned, so nothing will ever be written to the spool and no
            # terminal path below will run to clean it up - release it here.
            _discard_stderr_spool(stderr_path, stderr_fh)
            # Same thresholds the watchdog resolves for its own restarts, so a recording
            # profile's overrides apply to a failed spawn exactly as they do to a stall.
            from .watchdog import _resolve_watchdog_thresholds
            thresholds = _resolve_watchdog_thresholds(cfg, recording_id)
            restart_delay = thresholds.restart_delay
            max_failures = thresholds.max_failures
            if _handle_launch_failure(app, recording_id, seg_num, str(exc),
                                      max_failures, restart_delay):
                # A give-up here is terminal, so it owes the same releases every other
                # terminal path performs. Outside _handle_launch_failure's retried
                # closure, mirroring how _fail_recording releases after
                # _mark_recording_failed commits (dev/changelog/731).
                if rec.channel_id and rec.channel:
                    from . import connection_limits as connlim
                    connlim.release(rec.channel.account_id, 'recording', recording_id)
                from .health_score import apply_recording_health_observation
                apply_recording_health_observation(app, recording_id, 'failed')
            else:
                # Non-idempotent side effects, so they follow the commit rather than
                # sitting inside _handle_launch_failure's retried closure.
                ev.publish(recording_id, RESTART_ATTEMPTED, {
                    'delay': restart_delay,
                    'segment_number': seg_num,
                })
                _schedule_launch_retry(app, recording_id, seg_num, restart_delay)
            return LAUNCH_FAILED

        # Create segment row. Only this DB tail is wrapped in retry_on_locked - proc
        # is already spawned above, so retrying must never re-run Popen (that would
        # launch a second ffmpeg for the same segment).
        @retry_on_locked()
        def _record_segment_started_and_commit():
            seg = RecordingSegment(
                recording_id=recording_id,
                channel_id=rec.channel_id,
                segment_number=seg_num,
                file_path=seg_path,
                started_at=datetime.utcnow(),
                stall_count=0,
            )
            db.session.add(seg)
            detail = f'Segment {seg_num} started, pid={proc.pid}'
            if pace:
                detail += (f', reading the stream at real-time speed '
                           f'({_PACING_NOTES[pace_source]})')
            add_recording_event(recording_id, SEGMENT_STARTED, detail=detail,
                                segment_number=seg_num)
            db.session.commit()
            return seg.id

        seg_id = _record_segment_started_and_commit()

        state = get_state(recording_id)
        if state is None:
            # A teardown ran concurrently: it closed every open segment and popped _active
            # BEFORE the row above was committed, so nothing else will ever close this one.
            # Releasing it here is the create path's own teardown obligation - the kill and
            # the unlink stay outside the retried closure, since neither is idempotent.
            terminate_or_kill(proc, hard=True)
            _discard_stderr_spool(stderr_path, stderr_fh)
            _abandon_raced_segment(recording_id, seg_id, seg_num, seg_path)
            return LAUNCH_ABANDONED

        state.process = proc
        state.current_segment_num = seg_num
        state.current_segment_paced = pace
        # The previous segment's spool is closed and unlinked by whichever terminal path
        # ended it, which always runs before the next _launch_segment. Overwriting a
        # non-None value here would therefore leak a file - assert the invariant loudly
        # rather than silently dropping the handle.
        if state.stderr_fh is not None or state.stderr_path:
            log.warning('Recording %d seg %d: previous stderr spool was not released (%s) - '
                        'discarding it now', recording_id, seg_num, state.stderr_path)
            _discard_stderr_spool(state.stderr_path, state.stderr_fh)
        state.stderr_fh = stderr_fh
        state.stderr_path = stderr_path

        ev.publish(recording_id, SEGMENT_STARTED, {
            'segment_number': seg_num,
            'file_path': seg_path,
            'pid': proc.pid,
        })

        # Start (or restart) watchdog
        if state.watchdog is None or not state.watchdog.is_alive():
            from .watchdog import WatchdogThread
            state.watchdog = WatchdogThread(recording_id, state, app)
            state.watchdog.daemon = True
            state.watchdog.start()

        return LAUNCH_SPAWNED


def _teardown_active_ffmpeg(app, recording_id: int, exit_reason: str, hard_kill: bool = False) -> bool:
    """Terminate ffmpeg (if running; hard_kill skips straight to SIGKILL), close the
    active segment, pop from _active, and release any held connection-limit slot.
    Does NOT touch Recording.status and does NOT start concatenation - callers do
    both afterward. Returns True if there was live state to tear down, False if
    _active had no entry."""
    state = get_state(recording_id)
    if state is None:
        return False
    state.stop_event.set()

    proc = state.process
    if proc and proc.poll() is None:
        log.info('Recording %d: stopping ffmpeg (pid=%d) reason=%s', recording_id, proc.pid, exit_reason)
        terminate_or_kill(proc, hard=hard_kill)

    # Finalise the active segment
    _close_active_segment(app, recording_id, exit_reason=exit_reason)

    with _lock:
        _active.pop(recording_id, None)

    rec = db.session.get(Recording, recording_id)
    if rec is not None and rec.channel_id and rec.channel:
        from . import connection_limits as connlim
        connlim.release(rec.channel.account_id, 'recording', recording_id)

    return True


def _handoff_stop_old_recording(app, old_recording_id: int, new_recording_id: int):
    """Case 2: gracefully stop an old same-channel recording so a new one can
    start immediately. Reuses the synchronous teardown from stop_recording();
    concatenation continues in the background exactly as a normal clean stop -
    old_rec.status flows through CONCATENATING -> COMPLETED, never FAILED/ABORTED.
    """
    _teardown_active_ffmpeg(app, old_recording_id, exit_reason='RECORDING_HANDOFF')

    @retry_on_locked()
    def _log_old_handoff_and_commit():
        add_recording_event(old_recording_id, RECORDING_HANDOFF,
                            detail=f'Handed off to new recording #{new_recording_id} on the same channel')
        db.session.commit()

    _log_old_handoff_and_commit()

    from .concatenator import do_concatenation
    threading.Thread(target=do_concatenation, args=(app, old_recording_id), daemon=True).start()
    ev.publish(old_recording_id, RECORDING_HANDOFF, {'handed_off_to': new_recording_id})


def recording_format_pin(recording_id: int):
    """The format this recording is committed to for the rest of its run - the
    format_key of the EARLIEST segment that carries a capture-time probe
    (DESIGN-channel-groups-model.md 5.1, DECIDED 12). None when no segment has been
    probed yet, which reads as "not pinned": unknown is never treated as different.

    Derived, never stored. The segment rows are the record, so the pin costs no column
    and survives a service restart - a restart mid-recording that forgot the pin would
    silently drop the constraint on exactly the long recording that needs it most.

    Earliest *probed*, not literally segment 1: a segment that never carried enough data
    to probe also carries no data into the concatenated file, so what the output actually
    opens as is the first segment that did. A discarded segment is excluded for the same
    reason and it is not a fine point - a provider placeholder is 1080p30, so letting one
    set the pin locks a 59.94 fps recording to 30 fps and filters out every real member for
    the rest of the run (dev/changelog/957). Requires an app context."""
    seg = (RecordingSegment.query
           .filter(RecordingSegment.recording_id == recording_id,
                   RecordingSegment.probe_resolution.isnot(None),
                   RecordingSegment.probe_fps.isnot(None),
                   RecordingSegment.excluded_reason.is_(None))
           .order_by(RecordingSegment.segment_number.asc())
           .first())
    return segment_format_key(seg)


def _pin_eligible_members(members, pin, latest_by_channel):
    """Filter failover candidates down to the recording's format pin, returning
    (members, pin_override). Composes with format_eligible_members() rather than
    replacing it: the lock is the group's standing rule, the pin is this run's, and a
    recording can be constrained by the pin while its group manages no format at all.

    Same three rules as the lock filter, for the same reasons (channel_groups.py::
    format_eligible_members): no pin filters nothing, an untested candidate is never
    filtered out, and zero survivors is an override rather than a skip. That last one is
    measured rather than assumed - a mid-file format change concatenates and converts
    without error on this machine (dev/changelog/754), so refusing a failover over it
    would trade a complete recording for an incomplete one to avoid damage ffmpeg
    absorbs. Principle 2 wins; principle 1 is served by naming the change, which
    app/watchdog.py does off the next segment's own probe."""
    if pin is None:
        return list(members), False
    keep = []
    for ch in members:
        key = format_key(latest_by_channel.get(ch.id))
        if key is None or key == pin:
            keep.append(ch)
    if not keep and members:
        return list(members), True
    return keep, False


def failover_group_member(app, recording_id: int, reason: str, demote: bool = False,
                          score_departure: bool = True) -> bool:
    """Switch a group-backed recording to its next-best untried member after its
    active feed died (failed restart / dead-stream trip / max consecutive
    failures - the watchdog's three give-up points). Returns True if switched -
    the caller relaunches a segment and keeps going - or False when the recording
    isn't group-backed or every member has already failed, in which case the
    caller falls through to its normal abort path.

    `demote=True` is the fourth caller, the watchdog's stall-rate trip-wire
    (dev/changelog/889), and it is a VOLUNTARY move off a member that is still
    delivering content - nothing died. Three things change and nothing else does, so the
    format lock, the recording's format pin, the busy-member handling and the
    connection-slot swap are shared rather than reimplemented in a second selection path:

      1. the departing member is demoted (state.demoted_member_ids) instead of burned
         (state.failed_member_ids), so it stays selectable and merely ranks last;
      2. the current member is excluded outright - a voluntary move to yourself is not a
         move, and returning False for it is what makes a one-member group a no-op rather
         than an abort;
      3. the departing member is scored on its own measured share rather than the
         recording fail floor, which would be a lie about a feed that was delivering.

    A False from this function is never an abort on the demote path: the caller stays put
    and takes its normal restart, exactly as it would have without the trip-wire.

    `score_departure=False` says the caller has ALREADY written a more specific health
    observation on the departing member, so this function must not add a second one for the
    same departure. The placeholder discard is the one caller that does
    (app/watchdog.py, dev/changelog/957): it scores the member at the fail floor because the
    feed served no content at all, and the demotion's own scoring - which reads the member's
    measured share - would otherwise blend a near-perfect score in beside it for a member
    that delivered five seconds of black. Two observations for one departure is the defect;
    which of the two is right is not in question.

    Only rewrites channel_id/url and swaps connection slots; the caller owns
    killing/launching ffmpeg (this keeps every non-idempotent side effect out of
    the retry-wrapped DB closure below).
    """
    with app.app_context():
        rec = db.session.get(Recording, recording_id)
        if rec is None or rec.group_id is None or rec.group is None:
            return False
        state = get_state(recording_id)
        if state is None:
            return False

        old_channel = rec.channel
        if rec.channel_id is not None:
            if demote:
                state.demoted_member_ids.add(rec.channel_id)
            else:
                state.failed_member_ids.add(rec.channel_id)

        cfg = load_config()
        streak_threshold = cfg.get('channel_testing', {}).get(
            'failing_streak_threshold', DEFAULT_FAILING_STREAK_THRESHOLD)
        # Members whose feed already died this run are dropped BEFORE either format
        # filter, not merely excluded from the ranking afterwards. Both filters treat
        # "zero survivors" as the override that keeps the recording alive (15.2), and a
        # survivor that has already failed is not one - counting it suppresses the
        # override and aborts a recording that had a working member left
        # (dev/docs/BUGS.md 2026-08-19, dev/changelog/754).
        members = [ch for ch in recording_members(rec.group.memberships)
                   if ch.id not in state.failed_member_ids]
        from .routes.channel_tests import _latest_tests_by_channel
        latest_by_channel = _latest_tests_by_channel([ch.id for ch in members])
        # Same filter as record start, so a failover cannot land on a format the group
        # would have refused to start on. When the lock leaves nothing, it hands back the
        # unfiltered list rather than an empty one - a live capture is never abandoned to
        # a format rule (15.2, principle 2).
        selection = format_eligible_members(rec.group, members, latest_by_channel)
        members = selection.members
        # Then this run's own constraint: a recording does not change format mid-run
        # (DECIDED 12). Independent of the lock - it holds even when the group manages no
        # format - and applied after it, so the lock's survivors are what the pin ranks
        # over rather than the other way round.
        pin = recording_format_pin(recording_id)
        members, pin_override = _pin_eligible_members(members, pin, latest_by_channel)
        busy = _busy_channel_ids(recording_id)
        # Prefer a member whose account can actually take the swap. The cross-account
        # branch below refuses rather than exceeding the target's limit
        # (dev/changelog/854), so without this the ranking sends the failover straight
        # into that refusal while an idle account sits a row lower (dev/changelog/855).
        # Applied after both format filters, so it cannot pre-empt their own
        # zero-survivors overrides.
        busy_account = _busy_account_channel_ids(members, recording_id)
        # Preference tiers, best-first: the first that yields a member wins. The inner
        # three are the long-standing busy cascade - prefer a member whose account has a
        # free slot, then any free member, then knowingly take the best busy one rather
        # than abort (busy-member skip rule; no handoff fires mid-recording, so both
        # recordings share the feed's provider connection).
        #
        # A demote pass wraps a second round outside them: every tier is tried against the
        # not-yet-demoted members first, and only then re-tried with the demoted ones
        # allowed back in. That second round is what keeps a small group cycling instead
        # of running dry - a three-member group goes A -> B -> C -> A - and it ranks by
        # health score, which each demotion has just made more honest about this run.
        demoted = state.demoted_member_ids if demote else frozenset()
        # Excluded from every tier on the demote path: moving to the member we are already
        # on is not a move. Nothing needs this off the demote path, where the current
        # member is in failed_member_ids and `members` already dropped it.
        #
        # Excluded HERE and not before the format filters, which is the deliberate
        # difference from the burn path: a stalling member is still a survivor of the lock
        # and of the pin, so leaving it in is what stops it from being the sole survivor
        # that a zero-survivors override would otherwise fire on. Staying put is the right
        # answer to "only the member you are on matches the locked format" - the override
        # exists to keep a dying recording alive, and this one is not dying.
        stay_put = {rec.channel_id} if (demote and rec.channel_id is not None) else frozenset()
        best = None
        for preferred in ([demoted, frozenset()] if demoted else [frozenset()]):
            base = stay_put | set(preferred)
            for tier in (base | busy | busy_account, base | busy, base):
                best = pick_best_member(members, latest_by_channel, exclude_ids=tier,
                                        streak_threshold=streak_threshold)
                if best is not None:
                    break
            if best is not None:
                break
        took_busy = best is not None and best.id in busy
        took_busy_account = best is not None and best.id in busy_account
        took_demoted = best is not None and best.id in demoted
        if best is None:
            if demote:
                # Not an abort and not a failure: a one-member group, or a group whose
                # every other member is unavailable, simply stays where it is. The caller
                # takes its normal restart from here.
                log.info('Recording %d: group "%s" has nowhere better to move (%s) - '
                         'staying on the current member',
                         recording_id, rec.group.name, reason)
                return False
            log.warning('Recording %d: group "%s" has no untried members left (%s)',
                        recording_id, rec.group.name, reason)
            return False
        # `members` already excludes the failed ones (see above), so this is only the
        # busy-and-not-chosen filter.
        skipped_busy = [ch for ch in members if ch.id in busy and ch.id != best.id]
        skipped_busy_account = [ch for ch in members if ch.id in busy_account
                                and ch.id not in busy and ch.id != best.id]

        # Cross-account switch: swap connection slots. The acquire can now refuse rather
        # than exceed the target account's limit (dev/changelog/854), so the release is
        # undone when it does - re-acquiring the slot this same recording released a line
        # earlier cannot fail, which is what keeps a live recording from ever being left
        # holding no slot at all.
        #
        # Refusing the swap is the whole answer here, and deliberately not a wait: this
        # runs on the watchdog thread with the capture already dead, and blocking it would
        # stall the recording's own supervision. Returning False drops the caller into its
        # existing dead-stream retry/abort path, which is itself a wait-and-retry loop and
        # re-enters this function with the accounts re-read.
        old_account_id = old_channel.account_id if old_channel else None
        if old_account_id is not None and best.account_id != old_account_id:
            from . import connection_limits as connlim
            connlim.release(old_account_id, 'recording', recording_id)
            if not _try_acquire_slot_with_preemption(app, recording_id, best.account_id):
                connlim.try_acquire(old_account_id, 'recording', recording_id)
                log.warning('Recording %d: cannot fail over to "%s" - account "%s" is at its '
                            'connection limit and every slot is held by another recording; '
                            'not exceeding it (%s)',
                            recording_id, best.name, best.account.name, reason)

                @retry_on_locked()
                def _log_failover_blocked_and_commit():
                    add_recording_event(
                        recording_id, DIAGNOSTICS,
                        detail=(f'Failover to "{best.name}" was held back: account '
                                f'"{best.account.name}" is at its connection limit and every '
                                f'slot is held by another recording. The recording was not '
                                f'moved rather than opening a connection over the limit.'),
                        extra={'kind': 'failover_blocked_by_connection_limit',
                               'group_id': rec.group_id, 'to_channel_id': best.id,
                               'to_account_id': best.account_id})
                    db.session.commit()

                _log_failover_blocked_and_commit()
                return False

        from .accounts import normalize_url
        old_name = old_channel.name if old_channel else '?'
        if took_busy:
            busy_note = (' - channel is busy with another recording but no free '
                         'untried channel exists; taking it anyway')
        elif skipped_busy:
            busy_note = (' - skipped busy channel(s): '
                         + ', '.join(f'"{ch.name}"' for ch in skipped_busy))
        else:
            busy_note = ''
        if took_busy_account:
            busy_note += (' - no untried member sits on an account with a free '
                          'connection slot')
        elif skipped_busy_account:
            busy_note += (' - rolled over to a free account, past: '
                          + ', '.join(f'"{ch.name}"' for ch in skipped_busy_account))
        override_note = ''
        if selection.override:
            override_note = (f' - no member matches the locked format '
                             f'{format_label(selection.reference)}, so the lock was '
                             f'overridden to keep the recording going')
        if pin_override:
            override_note += (f' - no member is still recording at {format_label(pin)}, '
                              f'so this recording changes format part-way through rather '
                              f'than stopping here')
        if took_demoted:
            override_note += (' - every other member has already been moved off during '
                              'this recording, so this is the best of them')
        if demote:
            # "Died" would be false here and the distinction is the whole point of this
            # path: the feed is still delivering, it is just costing too much to keep.
            detail = (f'Group "{rec.group.name}": feed "{old_name}" kept stalling '
                      f'({reason}) - moving to "{best.name}" ({best.account.name}, '
                      f'score {effective_score(best)}). "{old_name}" is demoted for the '
                      f'rest of this recording, not dropped - it is still selectable if '
                      f'the alternatives turn out worse'
                      f'{busy_note}{override_note}')
        else:
            detail = (f'Group "{rec.group.name}": feed "{old_name}" died ({reason}) - '
                      f'failing over to "{best.name}" ({best.account.name}, '
                      f'score {effective_score(best)}){busy_note}{override_note}')
        log.warning('Recording %d: %s', recording_id, detail)

        @retry_on_locked()
        def _switch_member_and_commit():
            rec.channel_id = best.id
            rec.url = normalize_url(best.stream_url, best.account)
            rec.consecutive_failures = 0
            add_recording_event(recording_id, GROUP_FAILOVER, detail=detail,
                extra={'group_id': rec.group_id, 'reason': reason,
                       'from_channel_id': old_channel.id if old_channel else None,
                       'to_channel_id': best.id,
                       'to_effective_score': effective_score(best),
                       'took_busy': took_busy,
                       'skipped_busy_channel_ids': [ch.id for ch in skipped_busy],
                       'took_busy_account': took_busy_account,
                       'skipped_busy_account_channel_ids': [
                           ch.id for ch in skipped_busy_account],
                       'format_pin': list(pin) if pin else None,
                       'pin_override': pin_override,
                       # True = a voluntary stall-rate move, so from_channel_id was
                       # demoted rather than burned and can be selected again this run.
                       'demoted': demote,
                       'took_demoted': took_demoted,
                       # Cumulative counters through the just-abandoned member - lets the
                       # terminal observation score the final member from only its own share
                       # (health_score.py::apply_recording_health_observation). Snapshotted
                       # before the consecutive_failures reset below.
                       'counters_at_failover': {
                           'total_stall_count': rec.total_stall_count or 0,
                           'total_restart_count': rec.total_restart_count or 0,
                           'consecutive_failures_peak': rec.consecutive_failures_peak or 0,
                           'total_downtime_seconds': rec.total_downtime_seconds or 0,
                       }})
            db.session.commit()

        _switch_member_and_commit()

        # The abandoned feed demonstrably died mid-recording - real quality signal
        # for that member, independent of the recording's own terminal observation.
        # A demoted member did NOT die, so it is scored on what it actually measured
        # instead of the fail floor (see apply_stall_demotion_health_observation).
        if old_channel is not None and score_departure:
            if demote:
                from .health_score import apply_stall_demotion_health_observation
                apply_stall_demotion_health_observation(
                    app, old_channel.id, recording_id, reason=reason)
            else:
                from .health_score import apply_failover_health_observation
                apply_failover_health_observation(app, old_channel.id, recording_id,
                                                  reason=reason)

        ev.publish(recording_id, GROUP_FAILOVER, {
            'group_id': rec.group_id,
            'from_channel': old_name,
            'to_channel': best.name,
            'reason': reason,
            'demoted': demote,
        })
        return True


def _preempt_sync_for_recording(recording_id: int, account_id: int):
    """Cancel an in-flight sync on this account (DESIGN-concurrency.md 5.3, gap G11).

    A sync's HTTP fetch can count against the account's provider connection limit, and
    recordings always win. Deliberately unconditional rather than gated on the slot
    acquire failing: sync holds no connection slot, so a successful acquire proves
    nothing about a sync being in flight.

    Never waits on the sync thread - the stop event makes it abandon promptly, and a
    recording must not block on anything. The cancelled sync self-heals at its next
    interval fire.
    """
    from .database import Account
    account = db.session.get(Account, account_id)
    if account is None or account.status != 'SYNCING':
        return

    from .accounts import cancel_sync
    result = cancel_sync(account_id, reason='Cancelled - a recording needed this account')
    if result == 'cancelled':
        log.warning('Recording %d: cancelled in-flight sync on account %d (%s) - recordings win',
                    recording_id, account_id, account.name)
    else:
        # 'reset': SYNCING status with no live thread, i.e. stale state from a crash.
        # Nothing to preempt; the next sync clears it.
        log.info('Recording %d: account %d (%s) was marked SYNCING with no live sync thread',
                 recording_id, account_id, account.name)


def _try_acquire_slot_with_preemption(app, recording_id: int, account_id: int) -> bool:
    """Acquire a connection slot on account_id for this recording, or return False.

    Recordings still always win over the two actors that do not hold a stream open on
    the user's behalf: an in-flight sync on this account is cancelled, and a running
    channel test is preempted so the recording can take its slot. What this will NOT do
    is exceed the account's limit. When every slot is held by another *recording*, it
    returns False and the caller waits for one to free
    (dev/docs/DESIGN-concurrency.md 1 doctrine 1, amended - dev/changelog/854).

    The limit is a provider-side ceiling, so crossing it risks the whole account rather
    than this one recording: a provider that throttles or bans over concurrent-connection
    abuse takes down every channel on it, including the recording already running
    legitimately. That is the clause CLAUDE.md principle 2 already carried - complete the
    recording, "never by hammering a provider hard enough to risk the account".
    """
    _preempt_sync_for_recording(recording_id, account_id)

    from . import connection_limits as connlim
    if connlim.try_acquire(account_id, 'recording', recording_id):
        return True
    # A live preview yields first: someone looking at a channel is worth less than a
    # measurement feeding its health score, and far less than the recording itself.
    if connlim.preempt_previews_for_slot(account_id):
        from . import preview
        preview.preempt_for_account(account_id)
        log.warning('Recording %d: preempted a live preview on account %d',
                    recording_id, account_id)
        if connlim.try_acquire(account_id, 'recording', recording_id):
            return True
    preempted = connlim.preempt_tests_for_slot(account_id)
    for _channel_id in preempted:
        from . import channel_tester
        channel_tester.kill_active_test_for_account(app, account_id)
        log.warning('Recording %d: preempted a running channel test on account %d',
                    recording_id, account_id)
    return connlim.try_acquire(account_id, 'recording', recording_id)


def _earlier_waiter_for_slot(recording_id: int, account_id: int, start_time: datetime):
    """The recording that has been waiting longer than this one for a slot on account_id.

    Fairness for the wait loop: without an ordering rule, whichever waiter happens to
    poll first when a slot frees takes it, and the recording that has been waiting
    longest can be starved indefinitely by later arrivals. A waiter is a SCHEDULED
    recording on this account whose own start time has passed and whose window is still
    open - i.e. its start job has fired at least once and deferred. Ordered on
    (start_time, id) so two recordings scheduled for the same instant still have a total
    order rather than each yielding to the other forever.

    Bounded by construction: the recording ahead can only hold this one back until its
    own stop_time passes, at which point it stops matching.
    """
    now = datetime.utcnow()
    return (Recording.query
            .join(Channel, Recording.channel_id == Channel.id)
            .filter(Channel.account_id == account_id,
                    Recording.status == REC_STATUS_SCHEDULED,
                    Recording.id != recording_id,
                    Recording.start_time <= now,
                    Recording.stop_time > now,
                    db.or_(Recording.start_time < start_time,
                           db.and_(Recording.start_time == start_time,
                                   Recording.id < recording_id)))
            .order_by(Recording.start_time, Recording.id)
            .first())


def _slot_wait_reason(account_id: int, waiting_on=None) -> str:
    """Plain-language why-this-recording-is-waiting, shared by every surface that says so.

    Written once here rather than at each site so the event, the alert and the log line
    cannot drift into describing the wait three different ways.
    """
    from .database import Account
    account = db.session.get(Account, account_id)
    account_name = account.name if account else f'#{account_id}'
    if waiting_on is not None:
        return (f'recording "{waiting_on.name}" (#{waiting_on.id}) has been waiting longer '
                f'for a connection slot on account "{account_name}"')
    return (f'every connection slot on account "{account_name}" is in use by another '
            f'recording')


def _note_slot_wait_once(recording_id: int, account_id: int, why: str, waiting_on=None):
    """Write the deferral event and its alert the FIRST time this recording waits.

    The wait re-enters every SLOT_WAIT_POLL_SECONDS, so writing these per attempt would
    bury the recording's real history under identical rows. Keyed on the reason rather
    than the event type alone: a recording can legitimately defer once for a conversion
    collision and once for a connection slot, and both facts are worth having.
    """
    already = [e for e in RecordingEvent.query.filter_by(
        recording_id=recording_id, event_type=RECORDING_START_DEFERRED).all()
        if 'connection_limit' in (e.extra_data or '')]
    if already:
        return

    @retry_on_locked()
    def _mark_slot_deferred_and_commit():
        add_recording_event(
            recording_id, RECORDING_START_DEFERRED,
            detail=(f'Start deferred: {why}. Waiting for a slot rather than exceeding the '
                    f'account\'s connection limit - will start as soon as one frees, if '
                    f'this recording\'s window is still open.'),
            extra={'kind': 'connection_limit', 'account_id': account_id,
                   'waiting_on_recording_id': waiting_on.id if waiting_on else None})
        db.session.commit()

    _mark_slot_deferred_and_commit()
    rec = db.session.get(Recording, recording_id)
    rec_name = rec.name if rec else f'#{recording_id}'
    from .alerts import create_alert
    create_alert(
        'RECORDING_WAITING_FOR_CONNECTION_SLOT',
        f'Recording waiting for a connection slot: {rec_name}',
        body=(f'Recording "{rec_name}" (#{recording_id}) has not started because {why}. It '
              f'will start as soon as a slot frees, provided its own scheduled window has '
              f'not ended by then. Nothing is being recorded in the meantime.'),
        source=f'account:{account_id}:slot-wait',
        recording_id=recording_id,
    )


def end_slot_wait(recording_id: int):
    """Clear the standing slot-wait alert once this recording is no longer waiting.

    The other half of _note_slot_wait_once above. RECORDING_WAITING_FOR_CONNECTION_SLOT
    describes a condition that is still true while the row stands, so the Alerts page
    lists it under "Active alerts" and offers no Dismiss (dev/changelog/933) - a promise
    that only holds if EVERY way out of the wait clears it, not just the one where the
    recording starts.

    Keyed on the recording id rather than the (type, source) pair: the source names the
    account, and a second recording still queued on that same account has to keep its own
    row. A no-op when nothing stands, so it is safe on the paths that never waited at all
    - which is most of them, and is why it sits beside dismiss_recording_failing_alerts at
    each site rather than behind a "did this one wait" test of its own.
    """
    from .alerts import dismiss_open_alerts_for_recording
    dismiss_open_alerts_for_recording(
        recording_id, 'RECORDING_WAITING_FOR_CONNECTION_SLOT')


def _defer_start_for_slot(app, recording_id: int, account_id: int, waiting_on=None):
    """Hold a SCHEDULED recording that cannot have a connection slot yet, or fail it loudly.

    Same shape as the mp4-conversion collision defer in start_recording, and for the same
    reason: re-arming the start job costs no thread and no new status, and each retry
    re-enters start_recording from the top so the group member, the handoff check and the
    slot acquire are all re-decided against current state rather than replayed.

    `waiting_on` is the recording ahead of this one in the queue, when the wait is the
    fairness yield rather than a full account.
    """
    now = datetime.utcnow()
    rec = db.session.get(Recording, recording_id)
    if rec is None:
        return
    why = _slot_wait_reason(account_id, waiting_on)

    if rec.stop_time <= now:
        log.error('Recording "%s" (#%d): %s, and this recording\'s own stop time passed '
                  'while it waited - giving up', rec.name, recording_id, why,
                  extra={'recording_id': recording_id, 'already_alerted': True})

        @retry_on_locked()
        def _fail_slot_wait_and_commit():
            r = db.session.get(Recording, recording_id)
            r.status = REC_STATUS_FAILED
            r.completed_at = now
            r.failure_reason = FAILURE_CONNECTION_SLOT_TIMEOUT
            add_recording_event(
                recording_id, RECORDING_FAILED,
                detail=(f'Never started: {why}, so starting would have exceeded the '
                        f'account\'s connection limit. Waited for a free slot instead, and '
                        f'this recording\'s own window ended first'))
            db.session.commit()

        _fail_slot_wait_and_commit()
        from .health_score import dismiss_recording_failing_alerts
        dismiss_recording_failing_alerts(recording_id)
        # The wait is over, badly: RECORDING_FAILED_CONNECTION_LIMIT below is what the
        # user is owed now, and leaving "waiting for a slot" standing beside it would
        # claim a recording is still queued when it has already given up.
        end_slot_wait(recording_id)
        from .alerts import create_alert
        create_alert(
            'RECORDING_FAILED_CONNECTION_LIMIT',
            f'Recording failed: {rec.name}',
            body=(f'Recording "{rec.name}" (#{recording_id}) never started: {why}. It waited '
                  f'for a slot rather than opening a connection the account is not entitled '
                  f'to, and its own scheduled window ended first.'),
            source='recorder', recording_id=recording_id,
        )
        return

    _note_slot_wait_once(recording_id, account_id, why, waiting_on)
    log.info('Recording %d: deferring start - %s', recording_id, why)
    from .scheduler import reschedule_recording_start
    reschedule_recording_start(
        recording_id, now + timedelta(seconds=SLOT_WAIT_POLL_SECONDS))


def _defer_resume_for_slot(app, recording_id: int, account_id: int) -> None:
    """Hold a recording that is resuming (crash recovery, unpause, dead-stream retry) but
    cannot have a connection slot yet.

    Unlike the SCHEDULED path this writes no terminal state when the window has passed:
    the recording's own stop_<id> job fires at stop_time independently and concatenates
    whatever was already captured, which is the right answer for a recording that has
    segments on disk (principle 2 - salvage what exists). All this owes the user is to
    stop re-arming and say why.
    """
    now = datetime.utcnow()
    rec = db.session.get(Recording, recording_id)
    if rec is None:
        return
    why = _slot_wait_reason(account_id)

    if rec.stop_time <= now:
        log.error('Recording "%s" (#%d): %s, and its window ended before a slot freed - '
                  'not resuming; whatever was already captured is finalized by its stop job',
                  rec.name, recording_id, why, extra={'recording_id': recording_id})
        _note_slot_wait_once(recording_id, account_id, why)
        # Noted, then cleared: the deferral event is the durable record of why the resume
        # never happened, and it stays on the recording. The alert says the recording is
        # waiting, which stopped being true the moment its window closed.
        end_slot_wait(recording_id)
        return

    _note_slot_wait_once(recording_id, account_id, why)
    log.info('Recording %d: deferring resume - %s', recording_id, why)
    from .scheduler import reschedule_recording_resume
    reschedule_recording_resume(
        recording_id, now + timedelta(seconds=SLOT_WAIT_POLL_SECONDS))


@retry_on_locked()
def _log_manual_stop_and_commit(recording_id: int):
    """MANUAL_STOP bookkeeping: pull stop_time back to now when stopping early, and
    log the manual-stop event. Re-fetches the row inside the retry unit."""
    from .database import RECORDING_MANUALLY_STOPPED, RECORDING_STOPPED_EARLY
    rec = db.session.get(Recording, recording_id)
    if rec is None:
        return
    now = datetime.utcnow()
    if rec.stop_time > now and (rec.stop_time - now).total_seconds() > 60:
        early_secs = (rec.stop_time - now).total_seconds()
        log.info('Recording %d: stopped %.0fs before scheduled end; adjusting stop_time',
                 recording_id, early_secs)
        add_recording_event(recording_id, RECORDING_STOPPED_EARLY,
                            detail=f'Stopped {early_secs/60:.1f} min before scheduled end; stop_time updated to actual stop')
        rec.stop_time = now
    add_recording_event(recording_id, RECORDING_MANUALLY_STOPPED,
                        detail='Recording manually stopped by user')
    db.session.commit()


def _capture_produced_data(recording_id: int) -> bool:
    """Whether any of this recording's segments ever held data.

    Wider than concatenator.joinable_segments on purpose: a placeholder-only capture or one
    whose files went missing still produced something, and the join is the path that names
    each of those precisely. Only a capture that never pulled a byte is a plain dead stream.
    """
    for seg in RecordingSegment.query.filter_by(recording_id=recording_id).all():
        if (seg.bytes_recorded or 0) > 0:
            return True
        if seg.file_path and os.path.exists(seg.file_path) and os.path.getsize(seg.file_path) > 0:
            return True
    return False


def stop_recording(app, recording_id: int, reason: str = 'STOP_TIME_REACHED'):
    """Signal the watchdog to stop and kill the current ffmpeg process."""
    with app.app_context():
        had_state = _teardown_active_ffmpeg(app, recording_id, exit_reason=reason)

        if not had_state:
            # May already be done, paused, or waiting on a dead-stream retry; concat only
            # applies while the recording is still alive in one of those senses. RETRYING has
            # no active ffmpeg either (same as PAUSED), so it reaches this branch too.
            rec = db.session.get(Recording, recording_id)
            if not (rec and rec.status in (REC_STATUS_IN_PROGRESS, REC_STATUS_PAUSED, REC_STATUS_RETRYING)):
                return

            # A RETRYING window that ended is reached by two persisted jobs - stop_<id> and
            # retry_<id> both misfire at startup, and at runtime the retry can land after
            # stop_time but before the stop job's join flips the status. Both come through
            # here, so both reach the same answer in either order: join what was captured,
            # and give up as a dead stream only when there is nothing to join. Deciding it in
            # two places made the outcome whichever job committed first (dev/changelog/988).
            if (rec.status == REC_STATUS_RETRYING and reason == 'STOP_TIME_REACHED'
                    and not _capture_produced_data(recording_id)):
                from .watchdog import finalize_dead_stream_retry_exhausted
                finalize_dead_stream_retry_exhausted(
                    app, recording_id, cause='scheduled window ended during the retry wait')
                return

        if reason == 'MANUAL_STOP':
            _log_manual_stop_and_commit(recording_id)

        from .concatenator import do_concatenation
        threading.Thread(
            target=do_concatenation, args=(app, recording_id), daemon=True
        ).start()


def abort_recording(app, recording_id: int):
    """Cancel an in-progress or scheduled recording.

    The ABORTED commit comes FIRST and the teardown follows it. That order is load-bearing,
    not cosmetic: start_recording registers its live state before claiming the row, so
    writing the status first means either this write lands before that claim - which then
    refuses, and no ffmpeg is ever spawned - or the claim got there first, in which case its
    registration already happened and the teardown below is guaranteed to find it. Tearing
    down first, as this did until dev/changelog/987, lost that pairing: a cancel in the first
    second of a recording found no state to stop, marked the row ABORTED, removed the stop
    job, and then watched start_recording spawn ffmpeg onto it anyway - a capture with no
    stop job, no teardown path that would ever run, and the account's connection slot held
    for the life of the process.
    """
    with app.app_context():
        rec = db.session.get(Recording, recording_id)
        if rec:
            @retry_on_locked()
            def _mark_aborted_and_commit():
                r = db.session.get(Recording, recording_id)
                add_recording_event(recording_id, RECORDING_ABORTED, detail='Recording manually cancelled')
                now = datetime.utcnow()
                r.status = REC_STATUS_ABORTED
                r.cancel_reason = CANCEL_DURING_CAPTURE
                r.stop_time = now
                r.completed_at = now
                db.session.commit()

            _mark_aborted_and_commit()

        _teardown_active_ffmpeg(app, recording_id, exit_reason='MANUAL_CANCEL', hard_kill=True)

        if rec:
            from .scheduler import unschedule_recording
            unschedule_recording(recording_id)
            from .health_score import dismiss_recording_failing_alerts
            dismiss_recording_failing_alerts(recording_id)
            end_slot_wait(recording_id)

        ev.publish(recording_id, RECORDING_ABORTED, {'status': REC_STATUS_ABORTED})


def pause_recording(app, recording_id: int):
    """Pause an in-progress recording: stop ffmpeg, keep files, no concatenation."""
    with app.app_context():
        _teardown_active_ffmpeg(app, recording_id, exit_reason='MANUAL_PAUSE')

        rec = db.session.get(Recording, recording_id)
        if rec:
            @retry_on_locked()
            def _mark_paused_and_commit():
                add_recording_event(recording_id, RECORDING_PAUSED, detail='Recording manually paused')
                rec.status = REC_STATUS_PAUSED
                db.session.commit()

            _mark_paused_and_commit()

        ev.publish(recording_id, RECORDING_PAUSED, {'status': REC_STATUS_PAUSED})


@retry_on_locked()
def _handle_launch_failure(app, recording_id, seg_num, error_msg, max_failures, restart_delay):
    """Called when Popen itself fails (ffmpeg not found, etc.).

    Returns True if this call caused a terminal FAILED status, so the caller can decide
    whether to feed a health-score observation (never done inside this closure itself -
    that's a second commit and would duplicate the RESTART_FAILED/RECORDING_FAILED events
    on a retry, see CLAUDE.md's commit-discipline rule).

    Both outcomes are named in the same commit - the give-up, and the retry the caller is
    about to schedule. A recording left IN_PROGRESS with no process and no event saying a
    relaunch is coming is the unexplainable state Product Principle 1 exists to prevent.
    """
    from .database import RESTART_FAILED
    with app.app_context():
        rec = db.session.get(Recording, recording_id)
        if rec is None:
            return False
        rec.consecutive_failures += 1
        if rec.consecutive_failures > rec.consecutive_failures_peak:
            rec.consecutive_failures_peak = rec.consecutive_failures
        add_recording_event(recording_id, RESTART_FAILED,
                            detail=f'Launch failed: {error_msg}', segment_number=seg_num)
        failed = rec.consecutive_failures >= max_failures
        if failed:
            rec.status = REC_STATUS_FAILED
            rec.completed_at = datetime.utcnow()
            rec.failure_reason = FAILURE_LAUNCH_FAILED
            add_recording_event(recording_id, RECORDING_FAILED,
                                detail=(f'Max consecutive failures reached ({rec.consecutive_failures}) - '
                                        f'the last attempt could not start ffmpeg: {error_msg}'))
            with _lock:
                dead_state = _active.pop(recording_id, None)
            if dead_state is not None:
                # Cancels a launch-retry still waiting out its delay and winds down a
                # watchdog that reached here through its own restart branch - both hold
                # this state object directly, so popping _active alone does not reach
                # them. Idempotent, so it is safe inside this retried closure.
                dead_state.stop_event.set()
        else:
            add_recording_event(recording_id, RESTART_ATTEMPTED,
                                detail=(f'Waiting {restart_delay}s before retrying the launch '
                                        f'of segment {seg_num} '
                                        f'(attempt {rec.consecutive_failures} of {max_failures})'),
                                segment_number=seg_num)
        db.session.commit()
        return failed


def _schedule_launch_retry(app, recording_id: int, seg_num: int, delay):
    """Relaunch seg_num after `delay` seconds when Popen itself failed.

    A launch failure below the give-up threshold leaves no process and no segment row, so
    the watchdog cannot own the retry the way it owns a stall: its loop keys off
    state.current_segment_num, which _launch_segment only advances after a successful
    spawn, so it either waits forever for a row that will never appear or re-reads the
    previous, already-ended segment. On the very first launch there is no watchdog at all.
    Without this the recording sits IN_PROGRESS with nothing capturing until its stop-time
    job fires and concatenation runs over zero segments (dev/changelog/644).

    Distinct from the dead-stream retry in app/watchdog.py: that one answers "the stream
    itself is gone", parks the recording in RETRYING for minutes and releases its
    connection slot. This is a spawn that failed while the recording is still live and
    holding everything it acquired, so it stays IN_PROGRESS and retries on the watchdog's
    own restart cadence.

    The thread waits on stop_event rather than sleeping, so every teardown path cancels it
    (_teardown_active_ffmpeg and kill_all_active both set it before anything else).
    """
    state = get_state(recording_id)
    if state is None or state.stop_event.is_set():
        return

    def _retry():
        if state.stop_event.wait(timeout=delay):
            return  # torn down while waiting - deliberate, not a failure to report
        # A recording that ended and was resumed under us has a different state object;
        # relaunching against the old one would attach the process where nothing reads it.
        if get_state(recording_id) is not state:
            return
        with app.app_context():
            rec = db.session.get(Recording, recording_id)
            if rec is None or rec.status != REC_STATUS_IN_PROGRESS:
                return
        _launch_segment(app, recording_id, seg_num)

    thread = threading.Thread(target=_retry, name=f'launch-retry-{recording_id}-{seg_num}',
                              daemon=True)
    state.launch_retry = thread
    thread.start()


def _close_active_segment(app, recording_id: int, exit_reason: str):
    # Read outside the retried closure: it consumes the spool (closes and unlinks it), so a
    # lock-retry of the closure would find it already gone and record an empty tail. Reading
    # first makes the values plain locals the retry can safely reuse.
    exit_code, tail, reconnects, spool_missing = collect_segment_diagnostics(recording_id)

    @retry_on_locked()
    def _close_and_commit():
        with app.app_context():
            seg = RecordingSegment.query.filter_by(
                recording_id=recording_id, ended_at=None
            ).order_by(RecordingSegment.segment_number.desc()).first()
            if seg:
                seg.ended_at = datetime.utcnow()
                seg.exit_reason = exit_reason
                if os.path.exists(seg.file_path):
                    seg.bytes_recorded = os.path.getsize(seg.file_path)
                add_recording_event(recording_id, SEGMENT_ENDED,
                                    detail=f'Segment {seg.segment_number} ended: {exit_reason}',
                                    segment_number=seg.segment_number)
                record_segment_diagnostics(recording_id, seg, exit_code, tail,
                                           reconnects, spool_missing)
                db.session.commit()

    _close_and_commit()


def _snapshot_channel_health(rec):
    """Store the most recent ChannelTest for rec.channel_id as a JSON snapshot.

    Called right before db.session.commit() in start_recording, so the caller
    handles the commit.
    """
    import json
    if not rec.channel_id:
        return
    try:
        from .database import ChannelTest
        latest = (
            ChannelTest.query
            .filter_by(channel_id=rec.channel_id, job_id=None)
            .order_by(ChannelTest.id.desc())
            .first()
        )
        if latest is None:
            return
        from .tz_utils import UTC
        rec.channel_health_snapshot = json.dumps({
            'resolution':      latest.resolution,
            'fps':             latest.fps,
            'bitrate_kbps':    latest.bitrate_kbps,
            'frame_pct':       latest.frame_pct,
            'drop_count':      latest.drop_count,
            'audio_codec':     latest.audio_codec,
            'audio_channels':  latest.audio_channels,
            'connected':       latest.connected,
            'test_started_at': (
                latest.test_started_at.replace(tzinfo=UTC).isoformat()
                if latest.test_started_at else None
            ),
        })
    except Exception as exc:
        log.warning('Could not snapshot channel health for recording %d: %s', rec.id, exc)


def _safe_name(name: str) -> str:
    """Sanitize a recording name for use as a filename component."""
    import re
    safe = re.sub(r'[^\w\-.]', '_', name)
    return safe.strip('._') or 'recording'
