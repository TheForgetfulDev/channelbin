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
from typing import NamedTuple, Optional

from sqlalchemy.exc import SQLAlchemyError

from . import db
from .config import load_config
from .version import __version__
from .database import (
    RECORDING_JOB_ERROR,
    Channel, Recording, RecordingSegment, RecordingEvent, add_recording_event,
    SEGMENT_STARTED, SEGMENT_ENDED, RECORDING_HANDOFF, DIAGNOSTICS, RESTART_ATTEMPTED,
    GROUP_MEMBER_SELECTED, GROUP_FAILOVER, RECORDING_FORMAT_OVERRIDE,
    RECORDING_MOVED_FOR_CONNECTION,
    RECORDING_URL_RERESOLVED, RECORDING_RESUME_REFUSED,
    RECORDING_FAILED, RECORDING_START_DEFERRED, RECORDING_ABORTED, RECORDING_PAUSED,
    REC_STATUS_SCHEDULED, REC_STATUS_IN_PROGRESS, REC_STATUS_PAUSED, REC_STATUS_RETRYING,
    REC_STATUS_FAILED, REC_STATUS_ABORTED, WINDOW_OPEN_STATUSES, REC_ENDED_STATUSES,
    FAILURE_CONVERSION_COLLISION, FAILURE_CONNECTION_SLOT_TIMEOUT, FAILURE_DVR_DIR_UNUSABLE,
    FAILURE_LAUNCH_FAILED, FAILURE_ACCOUNT_BLOCKED, FAILURE_SWITCHED_OFF,
    CANCEL_DURING_CAPTURE,
)
from .channel_groups import (effective_score, is_streaking, pick_best_member,
                             recording_members,
                             format_eligible_members, format_key, format_label,
                             segment_pin_format_key, DEFAULT_FAILING_STREAK_THRESHOLD)
from .db_utils import retry_on_locked
from .fmt_utils import SECONDS_LIST_SHOWN, counted, seconds_list
from .fs_utils import PATH_OK, describe_dir_problem, probe_dir
from .hole_repair import HOLE_REMUX_SUFFIX
from .join_layout import LAYOUT_REMUX_SUFFIX
from .proc_utils import (SCRATCH_PREFIXES, build_capture_cmd, count_stderr_matches,
                         is_hls_url, pick_hls_variant, read_reconnect_jumps, read_stderr_tail,
                         REPEAT_CLOSURES_STUCK, scratch_file_paths, terminate_or_kill)
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
    # The quality each master playlist's first read chose, by URL, so a restart stays on it: a
    # read that failed at the restart would otherwise drop the rest of the run to the first
    # one listed, and a recording does not change format mid-run (dev/changelog/1408).
    hls_variants: dict = field(default_factory=dict)
    # Whether the segment now running was launched with -re, so the watchdog can tell a
    # paced capture that still misbehaved (pacing is not the fix) from an unpaced one.
    current_segment_paced: bool = False
    # A user's request to move this recording, left here by a route and carried out by the
    # recording's own watchdog at the top of its next poll (app/watchdog.py::
    # _check_move_request). The request thread records intent; the one thread that drives
    # the capture is the only one that stops an ffmpeg or launches the next segment, so a
    # switch cannot race the watchdog's own failover or block check (dev/changelog/1254).
    move_request: Optional['MoveRequest'] = None
    # The login whose own host list the running segment was launched on, or None for the
    # account's hosts (account_links.render_held_login()). The watchdog's roll reads it, so a
    # resolution failure rolls the list the failed launch used (DESIGN-account-providers.md
    # §14.6).
    host_login_id: Optional[int] = None


@dataclass
class MoveRequest:
    """What a user asked of a running group recording, for its watchdog to carry out.

    `target_channel_id` names the member to switch to; None asks for the account-block
    check to run now rather than at its next 15 s cadence - a block the user just set from
    the recording's own page. The watchdog sets `done` once it has acted, with `moved`
    saying whether the recording changed member; the route that asked reads the recording's
    newest event for what happened, so the user is told in the same words the recording
    keeps."""
    target_channel_id: Optional[int] = None
    done: threading.Event = field(default_factory=threading.Event)
    moved: bool = False


# Where a segment's real-time pacing decision came from. Every value is named, and the
# segment-start event says which one applied (dev/changelog/997).
PACING_BOUNDED = 'bounded'           # a segment length is set, so -re is mandatory
PACING_CHANNEL_ON = 'channel_on'     # Channel.pace_realtime is True
PACING_CHANNEL_OFF = 'channel_off'   # Channel.pace_realtime is False
PACING_DEFAULT_ON = 'default_on'     # the channel follows ffmpeg.pace_realtime, which is on
PACING_DEFAULT_OFF = 'default_off'   # the channel follows ffmpeg.pace_realtime, which is off

_PACING_NOTES = {
    PACING_BOUNDED: 'required by the configured segment length',
    PACING_CHANNEL_ON: "this channel's setting",
    PACING_DEFAULT_ON: 'the default in Settings',
}


def resolve_capture_pacing(cfg: dict, channel_pace, segment_duration):
    """(pace, source) for one recording segment's -re flag.

    Order matters. A bounded segment is always paced, because -t without -re is satisfied
    from a provider's backlog in seconds (dev/changelog/437). An explicit per-channel answer
    beats the default. Nothing turns it on by itself: on a feed that keeps dropping it costs
    video (dev/changelog/1423).
    """
    if segment_duration:
        return True, PACING_BOUNDED
    if channel_pace is True:
        return True, PACING_CHANNEL_ON
    if channel_pace is False:
        return False, PACING_CHANNEL_OFF
    if cfg.get('ffmpeg', {}).get('pace_realtime', False):
        return True, PACING_DEFAULT_ON
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
    scratch_dirs = set()
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
        # look at them. The prefixes and the filename shapes are proc_utils' own
        # (SCRATCH_PREFIXES, scratch_file_paths), so a new supervised caller is reaped here
        # the moment it exists.
        scratch_dir = os.path.dirname(stem) or '.'
        scratch_dirs.add(scratch_dir)
        for prefix in SCRATCH_PREFIXES:
            paths.extend(scratch_file_paths(scratch_dir, prefix, recording_id))
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
            # A rewrite for the join that a killed process left behind: every other exit from
            # the join deletes its own (concatenator._remux_for_join, hole_repair).
            paths.append(f'{seg.file_path}{LAYOUT_REMUX_SUFFIX}')
            paths.append(f'{seg.file_path}{HOLE_REMUX_SUFFIX}')
            # A join killed before it committed an output has no stem above, and its scratch
            # (the join's own, a layout rewrite's, a hole repair's) sits beside the segments.
            scratch_dir = os.path.dirname(seg.file_path) or '.'
            if scratch_dir not in scratch_dirs:
                scratch_dirs.add(scratch_dir)
                for prefix in SCRATCH_PREFIXES:
                    paths.extend(scratch_file_paths(scratch_dir, prefix, recording_id))
    paths.extend(recording_image_paths(recording_id, cfg))
    return list(dict.fromkeys(paths))


def recording_image_paths(recording_id: int, cfg: dict = None) -> list:
    """The images ChannelBin itself holds for a recording: the thumbnail and the poster
    frame, plus any temp capture a failed or interrupted grab left beside either, and the
    TheSportsDB images no other recording shares.

    Removed on EVERY delete, including one that keeps the recording's files: the thumbnail and
    poster frame are named by the row's id, the TheSportsDB images are listed only when no other
    recording names them, and only the row's pages ever show them, so once the row is gone nothing
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
    # The images downloaded from TheSportsDB for its event, when no other recording names them
    # (app/sports_images.py, dev/changelog/1465). Read while its link row still exists.
    from .sports_images import owned_paths
    paths.extend(owned_paths(recording_id, cfg))
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

#: How many frames one recording's poster will look at, the first included. It has to have a
#: limit because every try happens inside the join: 5 tries bound the wait at about 3m50s on a
#: wedged disk (three 12 s grabs and a 10 s check per try), and a normal try takes well under
#: a second.
POSTER_FRAME_TRIES = 5
POSTER_FRAME_RETRY_STEP_SECONDS = 60.0

#: extra_data['kind'] of the DIAGNOSTICS event written when a poster frame was skipped.
DIAGNOSTIC_KIND_POSTER_FRAME = 'poster_frame'
#: extra_data['kind'] of the DIAGNOSTICS event written when a group recording of a game starts on
#: a member whose guide shows something else, since none showed the game (game_members()).
DIAGNOSTIC_KIND_OTHER_PROGRAM = 'other_program_override'

#: What is known about the final-frame thumbnail when every poster try was black. Each is
#: named because each changes what the recording's cover turns out to be.
FINAL_FRAME_PICTURE = 'picture'
FINAL_FRAME_BLACK = 'black'
FINAL_FRAME_MISSING = 'missing'
FINAL_FRAME_UNCHECKED = 'unchecked'


def poster_frame_path(recording_id: int, cfg: dict = None) -> str:
    """Where this recording's poster frame lives. The one spelling of that name, so the
    writer, the teardown and the two readers cannot disagree about it."""
    from .storage_dirs import POSTER_FRAMES, image_dir

    return os.path.join(image_dir(cfg if cfg is not None else load_config(), POSTER_FRAMES),
                        f'{recording_id}.jpg')


def final_frame_path(recording_id: int, cfg: dict) -> str:
    """Where this recording's final-frame thumbnail lives."""
    from .storage_dirs import THUMBNAILS, image_dir

    return os.path.join(image_dir(cfg, THUMBNAILS), f'{recording_id}.jpg')


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


def _poster_retry_step(thumb_cfg: dict, recording_id: int) -> float:
    """The configured retry step in seconds. A step of zero would grab the same frame on every
    try, so anything that is not a positive number takes the default."""
    step = thumb_cfg.get('poster_frame_retry_step_seconds', POSTER_FRAME_RETRY_STEP_SECONDS)
    try:
        step = float(step)
    except (TypeError, ValueError):
        step = 0.0
    if step <= 0:
        log.warning('Recording %d: poster_frame_retry_step_seconds is not a positive number '
                    '(%r) - using %.0f', recording_id,
                    thumb_cfg.get('poster_frame_retry_step_seconds'),
                    POSTER_FRAME_RETRY_STEP_SECONDS)
        return POSTER_FRAME_RETRY_STEP_SECONDS
    return step


def _find_poster_frame(recording_id, candidates, target, step, tmp_path, ffmpeg_path, timeout):
    """Grab frames until one is not black. Returns `(found, skipped)`.

    `found` is `(segment, seek, how, moment)` with the frame sitting at `tmp_path`, or None
    when no try produced a usable frame. `skipped` names every black frame passed over.

    Each new target is one step after the moment actually looked at, not after the moment
    asked for: a target in a gap lands on the top of the next segment, and stepping from the
    target would land there again until the gap ran out.
    """
    from .screenshot import capture_screenshot, frame_bright_percent, is_black_frame

    skipped = []
    for _try in range(POSTER_FRAME_TRIES):
        seg, seek, how = poster_frame_seek(candidates, target)
        # The past-end answer is the top of the last segment, which is earlier than the frame
        # that was just rejected. A retry exists to look later, and earlier could be front
        # padding or the previous show.
        if skipped and how == POSTER_FRAME_PAST_END:
            log.info('Recording %d: the next poster frame try would be past everything '
                     'captured, so it stops at %s', recording_id,
                     counted(len(skipped), 'black frame'))
            return None, skipped
        if not capture_screenshot(seg.file_path, tmp_path, ffmpeg_path,
                                  seek_args=['-ss', f'{seek:.2f}'], timeout=timeout):
            log.info('Recording %d: poster frame capture failed (segment %d at %.2fs) - it '
                     'falls back to the final-frame thumbnail',
                     recording_id, seg.segment_number, seek)
            return None, skipped
        bright = frame_bright_percent(tmp_path, ffmpeg_path)
        if bright is None:
            log.warning('Recording %d: the poster frame (segment %d at %.2fs) could not be '
                        'checked for black, so it is kept unchecked',
                        recording_id, seg.segment_number, seek)
        if not is_black_frame(bright):
            return (seg, seek, how, target), skipped
        where = f'segment {seg.segment_number} at {seek:.2f}s'
        skipped.append(where)
        log.info('Recording %d: skipped a black poster frame, %s (%.2f%% of its pixels are '
                 'above black)', recording_id, where, bright)
        looked_at = seg.started_at if how == POSTER_FRAME_AFTER_TARGET else target
        target = looked_at + timedelta(seconds=step)
    return None, skipped


def _final_frame_state(recording_id, cfg, ffmpeg_path):
    """Is the final-frame thumbnail, which a recording with no poster frame falls back to, any
    better? It is grabbed first, so it is already on disk when this asks."""
    from .screenshot import frame_bright_percent, is_black_frame

    path = final_frame_path(recording_id, cfg)
    if not os.path.exists(path):
        return FINAL_FRAME_MISSING
    bright = frame_bright_percent(path, ffmpeg_path)
    if bright is None:
        return FINAL_FRAME_UNCHECKED
    return FINAL_FRAME_BLACK if is_black_frame(bright) else FINAL_FRAME_PICTURE


def _note_skipped_poster_frames(recording_id, skipped, used, final_frame):
    """Say on the recording itself why its cover is not the frame the settings describe.

    `used` names the frame kept, or is None when there is no poster frame, and `final_frame`
    is a FINAL_FRAME_* state in that case. Runs after every ffmpeg, in its own retry closure,
    and a database failure here is only a log line: the join is what matters at this point.
    """
    black = f'{counted(len(skipped), "black frame")} ({", ".join(skipped)})'
    extra = {'kind': DIAGNOSTIC_KIND_POSTER_FRAME, 'black_frames': skipped}
    if used is not None:
        extra['frame_used'] = used
        detail = f'Poster frame: skipped {black} and used {used}'
    else:
        extra['final_frame'] = final_frame
        detail = f'No poster frame: found {black} and no usable frame, '
        if final_frame == FINAL_FRAME_PICTURE:
            detail += 'so the cover is the final frame'
        elif final_frame == FINAL_FRAME_BLACK:
            detail += 'and the final frame is black too, so the cover is black'
        elif final_frame == FINAL_FRAME_MISSING:
            detail += 'and there is no final frame either, so there is no cover'
        elif final_frame == FINAL_FRAME_UNCHECKED:
            detail += 'so the cover is the final frame, which could not be checked for black'
        else:
            log.error('Recording %d: unknown final frame state %r', recording_id, final_frame)
            detail += f'and the final frame is {final_frame}'
    if final_frame in (FINAL_FRAME_BLACK, FINAL_FRAME_MISSING, FINAL_FRAME_UNCHECKED):
        log.warning('Recording %d: %s', recording_id, detail)
    else:
        log.info('Recording %d: %s', recording_id, detail)

    @retry_on_locked()
    def _note_and_commit():
        add_recording_event(recording_id, DIAGNOSTICS, detail=detail, extra=extra)
        db.session.commit()

    try:
        _note_and_commit()
    except SQLAlchemyError as exc:
        db.session.rollback()
        log.warning('Recording %d: could not record the skipped poster frames: %s',
                    recording_id, exc)


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

    A black frame is not used: the target moves forward by the retry step and it grabs again,
    up to POSTER_FRAME_TRIES (dev/changelog/1433).

    Best-effort: a failure only means the recording falls back to its thumbnail. Spawns
    ffmpeg, so callers must keep it OUTSIDE any retry_on_locked closure. Requires an app
    context.
    """
    from .concatenator import joinable_segments
    from .config import resolve_ffmpeg_path

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
    # program's end. It is program_start_time, the creation snapshot of the air time (moved
    # only by auto-record retrack, along with the program and the window - dev/changelog/1188),
    # so no new state is needed to compute the offset.
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
    try:
        found, skipped = _find_poster_frame(
            recording_id, candidates, target, _poster_retry_step(thumb_cfg, recording_id),
            tmp_path, ffmpeg_path, thumb_cfg.get('capture_timeout_seconds', 12))
        if found is None:
            if skipped:
                _note_skipped_poster_frames(recording_id, skipped, None,
                                            _final_frame_state(recording_id, cfg, ffmpeg_path))
            return
        try:
            os.replace(tmp_path, poster_path)
        except OSError as exc:
            log.warning('Recording %d: poster frame replace failed: %s', recording_id, exc)
            return
    finally:
        # A rejected frame is still sitting here, and so is a grab whose replace failed.
        try:
            os.remove(tmp_path)
        except FileNotFoundError:
            pass
        except OSError as exc:
            log.warning('Recording %d: could not remove %s: %s', recording_id, tmp_path, exc)

    seg, seek, how, moment = found
    after_anchor = (moment - anchor).total_seconds()
    if how == POSTER_FRAME_AT_TARGET:
        log.info('Recording %d: poster frame taken %.0fs after %s, %.2fs into segment %d',
                 recording_id, after_anchor, anchor_kind, seek, seg.segment_number)
    elif how == POSTER_FRAME_AFTER_TARGET:
        log.info('Recording %d: nothing was captured %.0fs after %s, so the poster frame '
                 'is the first content after it, at the top of segment %d',
                 recording_id, after_anchor, anchor_kind, seg.segment_number)
    else:
        log.warning('Recording %d: %.0fs after %s is past everything this recording '
                    'captured, so the poster frame is the top of its last segment (%d)',
                    recording_id, after_anchor, anchor_kind, seg.segment_number)
    if skipped:
        _note_skipped_poster_frames(recording_id, skipped,
                                    f'segment {seg.segment_number} at {seek:.2f}s', None)


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
    thumb = final_frame_path(recording_id, cfg)
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


def has_live_state(recording_id: int) -> bool:
    """Whether a start or resume has registered this recording's live state."""
    with _lock:
        return recording_id in _active


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


def _busy_account_channel_ids(members, exclude_recording_id: int) -> tuple:
    """(the channel ids of `members` whose account has no connection slot a recording could
    take right now, the ones whose account has one only by cutting off an IPTV player on the
    output feed) - connection_limits.seat_standing(), the answer the output's own pick reads.

    The account-level sibling of the busy-channel rule above, and the same shape: prefer
    a member the recording can start on immediately, fall back to one it cannot when
    nothing else survives. Since the connection limit became a hard ceiling
    (dev/changelog/854) a member on a full account no longer risks the provider account -
    it costs capture time, because the start waits for a slot instead of connecting. So a
    group spanning several accounts rolls over to an idle one rather than queueing behind
    a busy one, and waiting is what is left when every eligible member is on a full
    account (dev/changelog/855). An account an IPTV player holds sits between the two: the
    recording connects at once there, but cuts the player off, so an empty account is taken
    first (dev/changelog/1642).

    The recording's own slot is discounted: at failover it already holds one on its
    current account, and a same-account swap keeps that slot rather than competing for it.
    """
    from . import connection_limits as connlim
    standing = connlim.seat_standing({ch.account_id for ch in members}, 'recording',
                                     exclude_holder=('recording', exclude_recording_id))
    full = {ch.id for ch in members if standing.get(ch.account_id) == connlim.SEAT_NONE}
    watched = {ch.id for ch in members
               if standing.get(ch.account_id) == connlim.SEAT_BY_CUTTING_VIEWER}
    paused = {ch.id for ch in members
              if standing.get(ch.account_id) == connlim.SEAT_BY_PAUSING_PICTURE}
    return full, ViewerAccounts(watched | paused, watched)


class ViewerAccounts(frozenset):
    """The channels a recording reaches only by taking a connection the IPTV output holds.
    `watched` are the ones where that cuts off a play or a picture-in-picture channel's big
    picture; on the rest it only pauses a picture no TV has as its big one, so those are
    taken first (dev/docs/DESIGN-pip-channels.md 8.2). A plain set handed where one of these
    is read counts as all watched."""

    def __new__(cls, ids=(), watched=()):
        self = super().__new__(cls, ids)
        self.watched = frozenset(watched)
        return self


def _watched(viewer_account) -> frozenset:
    return getattr(viewer_account, 'watched', frozenset(viewer_account))


def _viewer_account_note(best, took: bool, skipped, viewer_account=frozenset()) -> str:
    """The member-selection event's clause about IPTV players on the output feed: the
    connection it takes from one, or the members it passed over so as not to, or ''."""
    if took and best.id not in _watched(viewer_account):
        return (f' - no member on an account with a connection nobody is using, so it takes '
                f'one a picture-in-picture channel can spare on "{best.account.name}": a TV '
                f'watching it keeps its big picture')
    if took:
        return (f' - no member on an account with a connection nobody is using, so it takes '
                f'the one an IPTV player is watching on "{best.account.name}"')
    if skipped:
        return (' - passed over member(s) on an account an IPTV player is watching on, so '
                'as not to cut it off: ' + ', '.join(f'"{ch.name}"' for ch in skipped))
    return ''


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
                    end_capture_wait(recording_id)
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

                converting = [db.session.get(Recording, cid)
                              for cid in postprocessor.active_conversion_ids()]
                converting = [c for c in converting if c is not None]
                _note_start_deferred(
                    recording_id,
                    f'the mp4 conversion of "{converting[0].name}" to finish' if converting
                    else 'a live mp4 conversion to finish')

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
            # A recording of one game takes only members that may carry it (dev/changelog/1690).
            # When none can, the start goes ahead on the whole list and says so, as the format
            # lock's zero-survivors override does: a member's guide can be wrong, and a
            # recording that never starts cannot be checked.
            by_game = game_members(rec, members)
            game_override = bool(members) and not by_game.members
            if not game_override:
                members = by_game.members
            # Members on an account the user has blocked are dropped first, before the
            # format lock, like a member that already failed - a blocked one must never be
            # the lone survivor that suppresses the lock's zero-survivors override. When
            # every member is blocked they all stay, and the slot acquire below refuses and
            # defers: the recording waits for a block to end (dev/changelog/1151).
            from .account_blocks import blocked_account_ids, split_blocked
            blocked_ids = blocked_account_ids({ch.account_id for ch in members})
            unblocked, skipped_blocked = split_blocked(members, blocked_ids)
            if unblocked:
                members = unblocked
            else:
                skipped_blocked = []
            if not game_override:
                members = by_game.preferred(members)
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
            busy_account, viewer_account = _busy_account_channel_ids(members, recording_id)
            best = pick_best_member(members, latest_by_channel,
                                    exclude_ids=busy | busy_account | viewer_account,
                                    streak_threshold=streak_threshold)
            if best is not None and viewer_account and is_streaking(best, streak_threshold):
                # Sparing an IPTV player never sends a recording to a feed the health checks
                # say is down: the recording always wins (DESIGN-output-feed.md 4.4).
                best = None
            if best is None and viewer_account:
                # Nothing on an empty account. A picture-in-picture channel's small picture
                # gives way before anything a TV would lose its picture without
                # (DESIGN-pip-channels.md 8.2), and never for a feed on a failing streak.
                best = pick_best_member(members, latest_by_channel,
                                        exclude_ids=busy | busy_account | _watched(viewer_account),
                                        streak_threshold=streak_threshold)
                if best is not None and is_streaking(best, streak_threshold):
                    best = None
            if best is None:
                # Cut off an IPTV player before waiting: the recording is what the app is
                # for (DESIGN-output-feed.md 4.4).
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
            took_viewer_account = best is not None and best.id in viewer_account
            if best is not None:
                from .accounts import normalize_url
                skipped_busy = [ch for ch in members if ch.id in busy and ch.id != best.id]
                skipped_busy_account = [ch for ch in members if ch.id in busy_account
                                        and ch.id not in busy and ch.id != best.id]
                skipped_viewer_account = [] if took_busy_account else [
                    ch for ch in members if ch.id in viewer_account
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
                busy_note += _viewer_account_note(best, took_viewer_account,
                                                  skipped_viewer_account, viewer_account)
                if not game_override:
                    busy_note += _skipped_game_note(by_game.skipped, by_game.game)
                if skipped_blocked:
                    busy_note += (' - skipped member(s) on a blocked account: '
                                  + ', '.join(f'"{ch.name}" ({ch.account.name})'
                                              for ch in skipped_blocked))
                elif best.account_id in blocked_ids:
                    busy_note += (f' - every member is on a blocked account, so the start '
                                  f'waits for the block on "{best.account.name}" to end')

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

                game_detail = None
                if game_override:
                    shows = dict((ch.id, what) for ch, what in by_game.skipped).get(best.id, 'something else')
                    game_detail = (f'Recording from "{best.name}", whose guide shows {shows} at this time, not '
                                   f'{by_game.game}. No channel in group "{rec.group.name}" with Recording on '
                                   f'shows {by_game.game} then, so the recording starts on the best of them '
                                   'rather than not at all.')

                selected_detail = (f'Group "{rec.group.name}": recording from "{best.name}" '
                                   f'({best.account.name}, score {effective_score(best)})'
                                   f'{busy_note}')
                # A start that is waiting re-enters here on every retry. The member it names
                # is re-decided each time and written to the row, but the event is written
                # only when what it says has changed - otherwise a two-hour wait on a
                # blocked account buries the recording's history under identical rows.
                last_selected = (RecordingEvent.query
                                 .filter_by(recording_id=recording_id,
                                            event_type=GROUP_MEMBER_SELECTED)
                                 .order_by(RecordingEvent.id.desc()).first())
                repeat_selection = (last_selected is not None
                                    and last_selected.detail == selected_detail)

                @retry_on_locked()
                def _select_group_member_and_commit():
                    rec.channel_id = best.id
                    rec.url = normalize_url(best.stream_url, best.account)
                    if not repeat_selection:
                        add_recording_event(recording_id, GROUP_MEMBER_SELECTED,
                            detail=selected_detail,
                            extra={'group_id': rec.group_id, 'channel_id': best.id,
                                   'effective_score': effective_score(best),
                                   'took_busy': took_busy,
                                   'skipped_busy_channel_ids': [ch.id for ch in skipped_busy],
                                   'took_busy_account': took_busy_account,
                                   'skipped_busy_account_channel_ids': [
                                       ch.id for ch in skipped_busy_account],
                                   'took_viewer_account': took_viewer_account,
                                   'skipped_viewer_account_channel_ids': [
                                       ch.id for ch in skipped_viewer_account],
                                   'skipped_blocked_channel_ids': [
                                       ch.id for ch in skipped_blocked]})
                    if override_detail and not repeat_selection:
                        add_recording_event(recording_id, RECORDING_FORMAT_OVERRIDE,
                            detail=override_detail,
                            extra={'group_id': rec.group_id, 'channel_id': best.id,
                                   'locked_format': list(selection.reference),
                                   'recorded_format': list(
                                       format_key(latest_by_channel.get(best.id)) or [])})
                    if game_detail and not repeat_selection:
                        add_recording_event(recording_id, DIAGNOSTICS, detail=game_detail,
                            extra={'kind': DIAGNOSTIC_KIND_OTHER_PROGRAM, 'group_id': rec.group_id,
                                   'channel_id': best.id,
                                   'skipped_channel_ids': [ch.id for ch, _ in by_game.skipped]})
                    db.session.commit()

                if game_detail and not repeat_selection:
                    log.warning('Recording %d: %s', recording_id, game_detail,
                                extra={'recording_id': recording_id})
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
            end_capture_wait(recording_id)
            return

        account_id = rec.channel.account_id if (rec.channel_id and rec.channel) else None
        if account_id is not None:
            if not _try_acquire_slot_with_preemption(app, recording_id, account_id):
                move = _move_off_full_account(app, recording_id, account_id)
                if move.lost:
                    return
                if move.account_id is None:
                    _defer_start_for_slot(app, recording_id, account_id, move_note=move.note)
                    return
                account_id = move.account_id
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
                    if _yield_start_seat(recording_id, account_id):
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
        # slot too, under the same (recording, id) key this one just re-acquired. The seat
        # is only put back on that caller's account, when this one's acquire was on another
        # (a row the owner moved after this start read it).
        state = RecordingState()
        if not _claim_live_state(recording_id, state):
            log.warning('start_recording: recording %d already has live state - another '
                        'caller owns this capture, so this start does nothing', recording_id)
            if account_id is not None:
                _settle_shared_seat(recording_id, account_id, scheduled_counts=True)
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

        try:
            claimed = _claim_in_progress_and_commit()
        except Exception:
            # A claim that raised (the database, usually) decided nothing, and nothing has
            # been spawned. Leaving the live state registered would make the scheduler's
            # next try of this start stop at "already has live state" and return as if it
            # had worked, with the row still SCHEDULED and the account's seat held
            # (dev/changelog/1590). This caller registered the state, so no other caller
            # owns the capture or the seat: both are this one's to give back.
            _release_live_state(recording_id, state)
            if account_id is not None:
                from . import connection_limits as connlim
                connlim.release(account_id, 'recording', recording_id)
            raise
        if not claimed:
            _abandon_unclaimed_start(recording_id, state, account_id)
            return
        if account_id is not None:
            _hold_seat_after_claim(recording_id, account_id)

        # From here the row is claimed, so a raise can no longer be answered by the
        # scheduler trying the start again: start_recording() refuses a row that is not
        # SCHEDULED. Whatever raises below is unwound here and the capture brought back
        # through the resume path (_recover_claimed_launch, dev/changelog/1601).
        try:
            _start_claimed_capture(app, recording_id)
        except Exception as exc:
            _recover_claimed_launch(app, recording_id, state, account_id, exc, 'start')


def _start_claimed_capture(app, recording_id: int):
    """The rest of start_recording() once its claim is won: clear what the wait left
    standing, refresh the program details, launch segment 1."""
    # Neither is worth a minute of the recording: a standing alert or wait note is
    # cleared again by the next thing that ends them.
    try:
        from .health_score import dismiss_recording_failing_alerts
        dismiss_recording_failing_alerts(recording_id)
        end_capture_wait(recording_id)
    except SQLAlchemyError:
        log.warning('Recording %d: could not clear its waiting alert or wait note at '
                    'record start - starting the capture anyway', recording_id,
                    exc_info=True)
        db.session.rollback()

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


# How long after a start or a resume raised, with the recording already claimed, its capture
# is tried again. The scheduler's own pace for a start that raised before its claim
# (scheduler.JOB_ERROR_RETRY_SECONDS).
CLAIMED_LAUNCH_RETRY_SECONDS = 60


def _recover_claimed_launch(app, recording_id: int, state: RecordingState, account_id,
                            exc: Exception, what: str) -> str:
    """Unwind a start or resume (`what`) that raised after it registered its live state,
    and arm resume_<id> to bring the capture back while the window is open. Returns the
    error as it was written down.

    Without it the row sits IN_PROGRESS (or PAUSED / RETRYING) with live state and a seat
    held and nothing capturing: every later resume stops at "already has live state", and
    the stop job fails the recording for having no segments (dev/docs/BUGS.md 2026-11-16).
    The same state after a crash is what startup recovery hands to resume_recording(), so
    that is the path taken here too, a minute later.

    Everything the launch may have acquired is given back first: an ffmpeg already attached
    to the state, its open segment row and spool, the live state, the seat. An ffmpeg
    spawned but not yet attached is killed by _launch_segment itself before it raises.
    Never raises: the database is the likeliest cause of the raise, so each step is tried
    on its own and a step that fails is logged and passed over."""
    error = mask_creds_in_text(f'{type(exc).__name__}: {exc}')[:300]

    def _step(name, fn):
        try:
            return fn()
        except Exception:
            log.warning('Recording %d: after its %s raised, could not %s', recording_id, what,
                        name, exc_info=True)
            return None

    _step('roll the session back', db.session.rollback)
    # The full teardown only when a process was attached, since that is when this launch
    # has an open segment of its own to close. With none attached, an open row is one a
    # crash left, and the next resume owes it its real end time and SERVICE_RESTART.
    if state.process is not None and get_state(recording_id) is state:
        if _step('finish the teardown', lambda: _teardown_active_ffmpeg(
                app, recording_id, exit_reason='ERROR', hard_kill=True)) is None:
            _step('roll the session back', db.session.rollback)
    # Whatever the teardown did not reach. Each is a no-op when it did.
    state.stop_event.set()
    proc = state.process
    if proc is not None and proc.poll() is None:
        _step('stop its ffmpeg', lambda: terminate_or_kill(proc, hard=True))
    if state.stderr_fh is not None or state.stderr_path:
        _discard_stderr_spool(state.stderr_path, state.stderr_fh)
        state.stderr_fh, state.stderr_path = None, None
    _release_live_state(recording_id, state)
    if account_id is not None:
        from . import connection_limits as connlim
        _step('give its seat back',
              lambda: connlim.release(account_id, 'recording', recording_id))

    retry_at = datetime.utcnow() + timedelta(seconds=CLAIMED_LAUNCH_RETRY_SECONDS)
    TRY_AGAIN, WINDOW_ENDS, MOVED_ON = 'again', 'window', 'moved_on'

    @retry_on_locked()
    def _note_and_commit():
        rec = db.session.get(Recording, recording_id)
        if rec is None or rec.status not in WINDOW_OPEN_STATUSES:
            # Aborted, failed or deleted while the launch was raising: its own event says
            # how it ended, and there is nothing to bring back.
            return MOVED_ON, False
        outcome = TRY_AGAIN if rec.stop_time > retry_at else WINDOW_ENDS
        detail = (f'The {what} hit an error inside ChannelBin after the recording was claimed '
                  f'({error}). Nothing is left capturing from that try. '
                  + (f'The capture is started again every {CLAIMED_LAUNCH_RETRY_SECONDS} '
                     f'seconds until it works or the stop time passes.'
                     if outcome == TRY_AGAIN else
                     'Its window ends before another try, so whatever was already captured '
                     'is finalized by its stop job.'))
        last = (RecordingEvent.query
                .filter_by(recording_id=recording_id, event_type=RECORDING_JOB_ERROR)
                .order_by(RecordingEvent.id.desc()).first())
        try:
            told = json.loads(last.extra_data or '{}') if last is not None else {}
        except ValueError:
            told = {}
        # One event per distinct error, not one a minute for as long as the cause lasts.
        # A segment started since then means that run of tries ended: the same error
        # later is a new failure and is said again.
        same = (isinstance(told, dict) and told.get('after_claim') is True
                and told.get('error') == error and outcome == TRY_AGAIN
                and RecordingEvent.query.filter(
                    RecordingEvent.recording_id == recording_id,
                    RecordingEvent.event_type == SEGMENT_STARTED,
                    RecordingEvent.id > last.id).first() is None)
        if not same:
            add_recording_event(recording_id, RECORDING_JOB_ERROR, detail=detail,
                                extra={'job': what, 'error': error, 'after_claim': True})
            db.session.commit()
        return outcome, same

    try:
        outcome, repeat = _note_and_commit()
    except Exception:
        log.warning('Recording %d: could not write the event for its %s error; trying the '
                    'capture again anyway', recording_id, what, exc_info=True)
        _step('roll the session back', db.session.rollback)
        # The row could not be read, so whether it is still open is unknown. One more try:
        # resume_recording() does nothing for a row that has ended.
        outcome, repeat = TRY_AGAIN, False
    if outcome == MOVED_ON:
        log.warning('Recording %d: its %s raised (%s), and the recording has ended or gone '
                    'since - nothing to bring back', recording_id, what, error)
        return error
    log.log(logging.WARNING if repeat else logging.ERROR,
            'Recording %d: its %s raised after the recording was claimed (%s); %s',
            recording_id, what, error,
            f'trying the capture again in {CLAIMED_LAUNCH_RETRY_SECONDS}s'
            if outcome == TRY_AGAIN else 'its window ends before another try',
            exc_info=None if repeat else exc, extra={'recording_id': recording_id})
    if outcome == TRY_AGAIN:
        from .scheduler import reschedule_recording_resume
        _step('arm the next try', lambda: reschedule_recording_resume(recording_id, retry_at))
    return error


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
    will ever run and the slot is this caller's to release. When the winner records on
    another account than this caller acquired on, the seat is put back there
    (_settle_shared_seat).
    """
    _release_live_state(recording_id, state)
    status, _ = _start_row_now(recording_id)
    log.warning('Recording %d: the start claim was lost (row is %s) - abandoning this '
                'start without launching', recording_id, status or 'gone')
    if account_id is not None:
        _settle_shared_seat(recording_id, account_id, scheduled_counts=False)


def _start_row_now(recording_id: int) -> tuple:
    """(status, account id) of the recording as the database has it now, (None, None) when
    the row is gone. For a start deciding whose a seat is: two starts of one recording share
    one ('recording', id) seat, so what the other has written since is the whole question."""
    r = db.session.get(Recording, recording_id, populate_existing=True)
    if r is None:
        return None, None
    return r.status, (r.channel.account_id if (r.channel_id and r.channel) else None)


def _settle_shared_seat(recording_id: int, took_account_id: int, scheduled_counts: bool):
    """This start took (or re-took) the recording's seat on `took_account_id` and is now
    stopping because the recording is not its own: another start moved or claimed it, or a
    cancel or an edit landed. Leave the one seat where the recording is, and give it back
    only when nothing will record.

    Every start of one recording holds the same ('recording', id) seat, so a plain release
    here strips the seat the winner captures on (dev/docs/BUGS.md 2026-11-26). And an acquire
    on another account than the winner's carries the seat there (connection_limits.try_acquire
    moves a holder, it never seats one twice), so it is put back on the row's account.

    `scheduled_counts` says whether a row still SCHEDULED has an owner: yes for a start that
    lost to another start which has not claimed yet, no for one that lost the claim itself.
    A row an edit repointed keeps the seat until its start or a cancel, which both find it.
    PAUSED and RETRYING hold no seat, so only IN_PROGRESS counts as recording."""
    from . import connection_limits as connlim
    status, row_account_id = _start_row_now(recording_id)
    owned = status == REC_STATUS_IN_PROGRESS or (scheduled_counts
                                                 and status == REC_STATUS_SCHEDULED)
    if not owned or row_account_id is None:
        connlim.release(took_account_id, 'recording', recording_id)
        return
    if row_account_id == took_account_id:
        return
    if not connlim.try_acquire(row_account_id, 'recording', recording_id):
        # Never left counted on an account the recording is not on: that would keep a
        # free connection from everything else for as long as it records.
        connlim.release(took_account_id, 'recording', recording_id)
        log.warning('Recording %d: another start has it on account %d while this one held '
                    'its seat on account %d, and account %d has no seat free to put it back '
                    'on - it is recording with no seat counted', recording_id, row_account_id,
                    took_account_id, row_account_id, extra={'recording_id': recording_id})


def _yield_start_seat(recording_id: int, account_id: int) -> bool:
    """Give this start's seat to a recording ahead of it in the queue. True when the seat
    was given up and the start should wait; False when the start must simply stop because
    the recording is no longer this caller's to hold.

    The seat is shared with any other start of the same recording, which may claim the row
    at any moment (dev/changelog/987). So the row is read before the release and again after
    it: a start that claimed in between gets the seat straight back, and the claimer asserts
    its seat once more after its claim (_hold_seat_after_claim), which covers a claim that
    lands after the second read."""
    from . import connection_limits as connlim
    status, _ = _start_row_now(recording_id)
    if status != REC_STATUS_SCHEDULED:
        log.info('Recording %d: not waiting behind an earlier recording - the row is %s now, '
                 'so this start stops', recording_id, status or 'gone',
                 extra={'recording_id': recording_id})
        _settle_shared_seat(recording_id, account_id, scheduled_counts=False)
        return False
    connlim.release(account_id, 'recording', recording_id)
    status, row_account_id = _start_row_now(recording_id)
    if status == REC_STATUS_IN_PROGRESS:
        if row_account_id is None or not connlim.try_acquire(row_account_id, 'recording',
                                                             recording_id):
            log.warning('Recording %d: another start claimed it while this one was giving '
                        'its seat to an earlier recording, and the seat could not be taken '
                        'back - it is recording with no seat counted', recording_id,
                        extra={'recording_id': recording_id})
        return False
    return status == REC_STATUS_SCHEDULED


def _hold_seat_after_claim(recording_id: int, account_id: int):
    """Assert the claimed recording's seat once more. A no-op when it is held, as it is
    unless another start of the same recording gave the shared seat away between this
    start's acquire and its claim (_yield_start_seat). Never preempts and never stops the
    capture: a seat that cannot be retaken is said in the log."""
    from . import connection_limits as connlim
    try:
        held = connlim.try_acquire(account_id, 'recording', recording_id)
    except Exception:
        # Broad on purpose: the row is claimed and nothing is capturing yet, so a raise
        # from here would leave it IN_PROGRESS with nothing armed to bring it back.
        log.warning('Recording %d: could not confirm its connection seat after the claim',
                    recording_id, exc_info=True, extra={'recording_id': recording_id})
        db.session.rollback()
        return
    if not held:
        log.warning('Recording %d: its seat on account %d was given away by another start '
                    'before the claim and the account is full now - recording with no seat '
                    'counted', recording_id, account_id, extra={'recording_id': recording_id})


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

    Only _launch_segment's torn-down branches reach this: the row was committed a moment
    after a concurrent abort/stop had already closed every open segment, or while one was
    closing them, so no other path knows it exists. Left alone it keeps ended_at NULL forever
    and the detail page's `(seg.ended_at or now)` fallback renders an ever-growing duration
    for a segment that never captured anything, next to a partial .ts the abort's own delete
    pass had already walked past.

    A row the teardown did reach - closed by its _close_active_segment because the commit
    landed before that pass - is left as the teardown wrote it, file included: the join that
    follows a stop takes every closed segment with bytes, and deleting one from under it
    would be a second teardown disagreeing with the first.

    The caller has already killed ffmpeg, so the file is not still being written when it goes.
    """
    @retry_on_locked()
    def _close_raced_segment_and_commit():
        seg = db.session.get(RecordingSegment, seg_id)
        if seg is None or seg.ended_at is not None:
            return False
        seg.ended_at = datetime.utcnow()
        seg.exit_reason = 'TEARDOWN_RACE'
        add_recording_event(
            recording_id, SEGMENT_ENDED,
            detail=(f'Segment {seg_num} was discarded - the recording was torn down while this '
                    f'segment was launching, so it captured nothing and its file was deleted'),
            segment_number=seg_num)
        db.session.commit()
        return True

    if not _close_raced_segment_and_commit():
        return

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


def close_segments_left_open_on_ended_recordings() -> list:
    """Close every segment row left open on a recording whose capture is over, at startup.

    Every terminal path closes its segment, in a commit after the one that ends the
    recording; this is the net for a process that died between the two, or a path that
    someday forgets: nothing else would ever close it, the detail page would count it up as
    still capturing, and the usage ledger would be held behind it for an hour. The file is not
    read - a finished recording's storage may be asleep, and its segments are usually gone
    after the join - so when capture stopped is unknown: the row is closed at its own start,
    credits no capture time, and its event and the WARNING say so (dev/changelog/1374).
    Returns the (recording id, segment number) pairs it closed. Requires an app context."""
    @retry_on_locked()
    def _close_and_commit():
        rows = (RecordingSegment.query
                .join(Recording, Recording.id == RecordingSegment.recording_id)
                .filter(RecordingSegment.ended_at.is_(None),
                        Recording.status.in_(REC_ENDED_STATUSES))
                .order_by(RecordingSegment.id).all())
        for seg in rows:
            seg.ended_at = seg.started_at
            seg.exit_reason = 'LEFT_OPEN'
            add_recording_event(
                seg.recording_id, SEGMENT_ENDED, segment_number=seg.segment_number,
                detail=(f'Segment {seg.segment_number} was still marked as capturing after the '
                        f'recording had ended, so it was closed at service restart. When it '
                        f'really stopped is unknown, so it shows no duration and adds no '
                        f'recorded time'))
        closed = [(seg.recording_id, seg.segment_number) for seg in rows]
        if rows:
            db.session.commit()
        return closed

    closed = _close_and_commit()
    for recording_id, seg_num in closed:
        log.warning('Recording %d: segment %d was left open after the recording ended - '
                    'closed at startup with no duration', recording_id, seg_num,
                    extra={'recording_id': recording_id})
    return closed


def resume_recording(app, recording_id: int):
    """Resume an IN_PROGRESS, PAUSED, or RETRYING recording. Returns None, or the error as
    text when the resume raised with the live state held and was unwound and armed again
    (_recover_claimed_launch), so a caller a person is waiting on can say so."""
    with app.app_context():
        rec = db.session.get(Recording, recording_id)
        if rec is None:
            return
        # Before anything is acquired. The resume_<id> job that reaches here is re-armed
        # by _defer_resume_for_slot and survives a restart, so it can fire on a row the
        # stop job has since joined or failed; a resume that took a seat and registered
        # live state first, and only then found _launch_segment refusing the status, held
        # both for the life of the process (dev/changelog/1242).
        if rec.status not in WINDOW_OPEN_STATUSES:
            log.warning('resume_recording: recording %d is %s - nothing to resume',
                        recording_id, rec.status)
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

        # Live state and the seat are held from here, so a raise below is unwound and the
        # resume armed again rather than left to hold both with nothing capturing
        # (_recover_claimed_launch, dev/changelog/1601).
        try:
            _resume_claimed_capture(app, recording_id, rec, state, account_id, open_seg)
        except Exception as exc:
            return _recover_claimed_launch(app, recording_id, state, account_id, exc, 'resume')
        return None


def _resume_claimed_capture(app, recording_id: int, rec, state: RecordingState, account_id,
                            open_seg):
    """The rest of resume_recording() once it holds the live state: claim the status where
    one is owed, close the segment a crash left open, launch the next one."""
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
            from .watchdog import close_retry_wait
            r = db.session.get(Recording, recording_id)
            close_retry_wait(r, datetime.utcnow())
            if not _claim_status_row(recording_id, (REC_STATUS_RETRYING,),
                                     REC_STATUS_IN_PROGRESS):
                db.session.rollback()
                return False
            add_recording_event(recording_id, RECORDING_RESUMED,
                                detail=f'Retry attempt {r.dead_stream_retry_count} - reconnecting')
            db.session.commit()
            return True

        if not _claim_retry_resumed_and_commit():
            _abandon_unclaimed_start(recording_id, state, account_id)
            return

    end_capture_wait(recording_id)

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
    relaunch (so a sync landing mid-recording rescues a stalling one). A group-backed
    recording is covered too: rec.channel is its serving member, and what selection and
    failover write into rec.url is exactly normalize_url(member.stream_url, member.account),
    so re-reading it here changes nothing unless the member's Channel row moved under the
    run - a host roll, a sync that found the provider's URLs drifted - which is the case
    that used to reconnect every relaunch to the dead host until the member was burned
    (dev/changelog/1243). Manual URL-only recordings (channel_id None) have no channel to
    resolve against and are left alone.
    """
    rec = db.session.get(Recording, recording_id)
    if rec is None or rec.channel_id is None:
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
            detail=(f'Stream URL for channel "{channel.name}" changed since it was last '
                    f'written to this recording; recording from the channel\'s current URL '
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
    """(ffmpeg_exit_code, stderr_tail, reconnects, spool_missing, jumps) for the segment that
    just ended; releases its spool.

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

    `jumps` is what those reconnects did to the file, a `proc_utils.ReconnectJumps` read from
    the same spool, or None when there was no reconnect: the seconds the provider sent again
    after each one, which the file holds twice, and any hole ffmpeg's repair left
    (dev/changelog/1237).

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
        return None, '', (0, True), False, None
    exit_code = state.process.poll() if state.process is not None else None
    path, fh = state.stderr_path, state.stderr_fh
    state.stderr_path, state.stderr_fh = None, None
    if fh is not None:
        try:
            fh.close()
        except OSError:
            pass  # the tail read below still tries; a partial spool is better than none
    tail, reconnects, spool_missing, jumps = '', (0, True), False, None
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
        # Read whether or not it reconnected: a closed gap or a clock reset is logged with
        # no reconnect behind it (dev/changelog/1415).
        jumps = read_reconnect_jumps(path)
        if not any(jumps[:2] + jumps[3:]):
            jumps = None
        try:
            os.unlink(path)
        except OSError:
            pass  # best-effort; the glob reap in _open_segment_stderr_spool collects strays
    return exit_code, tail, reconnects, spool_missing, jumps


def record_segment_diagnostics(recording_id: int, seg, exit_code, tail, reconnects=(0, True),
                               spool_missing=False, jumps=None, placeholder=False):
    """Write exit_code onto seg and, when there is something to say, add a DIAGNOSTICS event.

    Inserts only - the CALLER commits. This joins the caller's existing retry_on_locked unit
    rather than opening a second one: two commits inside one decorated closure can duplicate
    rows when the first succeeds and the second retries (CLAUDE.md § Agent Behavior).

    The exit code goes in the column and in the event's detail string, NEVER in extra_data -
    a stat with a column does not also live there (CLAUDE.md § Measurements). extra_data
    carries only the stderr tail, which has no column because nothing sorts on a blob. The
    reconnect count goes in the segment's own column too (the Stats card and the list row
    sum it) and in the detail string where a human reads it. What the reconnects did to the file (`jumps`, a
    `proc_utils.ReconnectJumps`) rides the detail string too, and extra_data carries the
    per-reconnect seconds, which have no column either (dev/changelog/1237): a file that
    plays 86 s for a 60 s window, 142% of its expected frames, is a number nobody can
    explain unless the event says the provider's buffer was captured again after each drop.

    Stays quiet when the exit is uninformative: a negative code is a signal we sent, which
    seg.exit_reason already names, so with no stderr to show there is nothing an event would
    add that the segment row does not already say. A segment that reconnected in-process is
    NOT uninformative, however it ended - that is the whole reason the count is collected.

    A LOST spool (spool_missing) is never uninformative either, whatever the exit code: the
    absence of an event is what made recording 18's segment 1 unexplainable, because nothing
    on any surface distinguished "ffmpeg had nothing to say" from "its output was destroyed"
    (dev/changelog/967). Saying so is Product Principle 1 applied to the diagnostic itself.
    """
    reconnect_count, reconnects_complete = reconnects
    if seg is not None:
        seg.ffmpeg_exit_code = exit_code
        # Written before the quiet-exit return below: a segment that reconnected zero
        # times is a measured 0, not an unknown, and the columns are what the Stats card
        # and the list row read (dev/changelog/1306).
        seg.reconnect_count = reconnect_count
        seg.reconnects_truncated = not reconnects_complete
    if (not tail and not reconnect_count and not spool_missing and jumps is None
            and not (exit_code is not None and exit_code > 0)):
        return
    seg_num = seg.segment_number if seg is not None else None
    if exit_code is None:
        code_str = 'ffmpeg exit code unknown'
    elif exit_code < 0:
        code_str = f'ffmpeg killed by signal {-exit_code}'
    elif (exit_code == 255 and seg is not None and seg.exit_reason
          and seg.exit_reason != 'PROCESS_EXITED'):
        # ffmpeg 7.1's code for the SIGTERM ChannelBin stops it with, so beside a stop of
        # ChannelBin's own it is not a failure - the Segments table reads it the same way
        # (dev/changelog/1287, 1293).
        code_str = 'ffmpeg stopped when ChannelBin told it to (exit code 255)'
    else:
        code_str = f'ffmpeg exited {exit_code}'
    detail = f'Segment {seg_num} capture ended: {code_str}' if seg_num is not None \
        else f'Capture ended: {code_str}'
    if reconnect_count:
        # "at least" only when the scan was truncated, so the ordinary case reads as the
        # fact it is rather than hedging about a bound nobody hit.
        howmany = f'at least {reconnect_count}' if not reconnects_complete else f'{reconnect_count}'
        times = 'time' if reconnect_count == 1 and reconnects_complete else 'times'
        if placeholder:
            # ffmpeg reads a finite clip's end at open, for its duration, and again when the
            # clip runs out; both log a reconnect at the clip's last byte that finds nothing.
            # None of them is a drop, and "the stream dropped" beside the discard said there
            # was a live stream to drop (dev/docs/BUGS.md 2026-10-21 10:45 PM).
            word = 'reconnect' if times == 'time' else 'reconnects'
            detail += (f'; ffmpeg logged {howmany} {word} '
                       f'at the end of the placeholder clip, which found nothing more - '
                       f'the clip is a file with an end, not a live stream that dropped')
        else:
            detail += (f'; the stream dropped and ffmpeg reconnected {howmany} {times} during '
                       f'the segment, without restarting the capture')
    extra = {'kind': 'capture_stderr'}
    if reconnect_count and jumps is not None and jumps.replays:
        # The provider answers each new connection from its buffer, so the content comes
        # back from before the drop and ffmpeg closes the backward jump: the file holds
        # those seconds twice on one continuous timeline, which is where a file longer than
        # its window comes from. A jump closed once per stream leaves a hole as long again
        # in the timeline, with the content on both sides of it intact (dev/changelog/1237).
        detail += (f'; each reconnect was answered from the provider\'s buffer again, so the '
                   f'file holds the seconds before each drop twice: '
                   f'{seconds_list(jumps.replays, limit=SECONDS_LIST_SHOWN)}, '
                   f'{sum(jumps.replays):.1f} s in all')
        if jumps.repeats >= REPEAT_CLOSURES_STUCK:
            # The replay figures above are then the streams' distance as much as any replay,
            # and a hole sentence would promise a repair that cannot match one stream's jump.
            detail += (f'; ffmpeg then corrected the video and audio against each other '
                       f'{jumps.repeats:,} times without settling, which means the picture and '
                       f'sound were about {jumps.repeat_seconds:.0f} s apart in this segment by '
                       f'its end')
            extra['timestamp_repeat_closures'] = jumps.repeats
            extra['streams_apart_seconds'] = jumps.repeat_seconds
        elif jumps.holes:
            n = len(jumps.holes)
            detail += (f'; at {n} of them ffmpeg closed the timestamp jump once per stream, '
                       f'which leaves a hole of about '
                       f'{seconds_list(jumps.holes, limit=SECONDS_LIST_SHOWN)} in the '
                       f'file\'s timeline, which the join tries to close')
        extra['reconnect_replay_seconds'] = list(jumps.replays)
        extra['reconnect_hole_seconds'] = list(jumps.holes)
    if jumps is not None and jumps.closed_gaps:
        # A forward jump past what the capture keeps as a gap (TIMELINE_GAP_KEEP_SECONDS):
        # the stretch is not in the file, and its timeline runs on as if it never was.
        n = len(jumps.closed_gaps)
        detail += (f'; ffmpeg closed up {counted(n, "forward timestamp jump")} of '
                   f'{seconds_list(jumps.closed_gaps[:5])}'
                   f'{" and more" if n > 5 else ""} - content the provider never sent, or '
                   f'its clock moving ahead - so the file\'s timeline runs on as if that '
                   f'stretch were not there')
        extra['closed_gap_seconds'] = list(jumps.closed_gaps)
    if jumps is not None and jumps.resets:
        n = len(jumps.resets)
        detail += (f'; ffmpeg closed {counted(n, "backward timestamp jump")} of '
                   f'{seconds_list(jumps.resets[:5])}{" and more" if n > 5 else ""} with no '
                   f'reconnect behind it - the provider\'s clock went back - so the file\'s '
                   f'timeline runs on from where it was')
        extra['reset_seconds'] = list(jumps.resets)
        if jumps.reset_holes:
            # The same once-per-stream closure as a reconnect's hole, and closed by the
            # same repair at the join, which reads the holes in file order (dev/changelog/1604).
            if jumps.repeats < REPEAT_CLOSURES_STUCK:
                n = len(jumps.reset_holes)
                detail += (f'; at {n} of them ffmpeg closed the jump once per stream, which '
                           f'leaves a hole of about '
                           f'{seconds_list(jumps.reset_holes, limit=SECONDS_LIST_SHOWN)} in '
                           f'the file\'s timeline, which the join tries to close')
            extra['reset_hole_seconds'] = list(jumps.reset_holes)
            extra['timeline_hole_seconds'] = list(jumps.timeline_holes)
    if spool_missing:
        detail += ('; ffmpeg\'s output was not captured for this segment - another process '
                   'deleted the file it was being written to, so there is no record of what '
                   'it reported')
    if tail:
        extra['stderr_tail'] = tail
    add_recording_event(recording_id, DIAGNOSTICS, detail=detail, segment_number=seg_num,
                        extra=extra)


def _has_recorded_bytes(recording_id) -> bool:
    """Whether a segment of this recording already holds content the file will keep (a
    discarded placeholder is not the channel's picture). Requires an app context."""
    if recording_id is None:
        return False
    return db.session.query(
        RecordingSegment.query.filter(RecordingSegment.recording_id == recording_id,
                                      RecordingSegment.bytes_recorded > 0,
                                      RecordingSegment.excluded_reason.is_(None))
        .exists()).scalar()


def _hls_variant_for(state, url: str, cfg: dict, recording_id=None):
    """The HlsVariant a segment of `url` should map, or None for a stream with no choice to
    make. A URL with a provider login in it never gets the playlist read (pick_hls_variant).

    The first answer stands for the run, a failed read's first-quality-listed included once
    a segment recorded with it: a later read that worked would map the best quality and
    change the picture size mid-recording (dev/docs/BUGS.md 2026-11-16). While nothing has
    been recorded there is no format to keep, so a failed read is asked again."""
    if not is_hls_url(url):
        return None
    from .accounts import parse_stream_url_parts
    if parse_stream_url_parts(url) is not None:
        return None
    known = state.hls_variants.get(url) if state is not None else None
    if known is not None and (known.offered or _has_recorded_bytes(recording_id)):
        return known
    variant = pick_hls_variant(cfg, url)
    if state is not None and variant is not None:
        state.hls_variants[url] = variant
    return variant


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
        # Settings default - see resolve_capture_pacing.
        segment_duration = cfg['recording']['segment_duration_seconds']
        channel = rec.channel if rec.channel_id else None
        pace, pace_source = resolve_capture_pacing(
            cfg, channel.pace_realtime if channel is not None else None, segment_duration)
        # The recording's seat may be on one of the account's listed logins: if the server
        # refused that login since the last segment, or the user turned it off, the seat
        # moves to another login with a free one, and either way this segment is launched
        # with the seated login's credentials rendered into the URL (app/account_links.py).
        # Recording.url keeps the channel's URL; the substitution lives on the command
        # only. An account with no login list pays nothing here: the holder is in its own
        # pool and the URL passes through untouched.
        launch_url = rec.url
        host_login_id = None
        if channel is not None:
            from . import connection_limits as connlim
            from .account_links import render_held_login
            connlim.reseat_before_relaunch(channel.account_id, 'recording', recording_id)
            held = render_held_login(rec.url, channel.account_id, 'recording', recording_id)
            launch_url, host_login_id = held.url, held.host_login_id
        # Said on the segment's start event when the launch went out on a login's own host,
        # so the recording's page answers "which host was this on" (§14.4).
        launched_on = None
        if host_login_id is not None:
            from .account_links import host_of
            if host_of(launch_url) != host_of(rec.url):
                from .database import Login
                lg = db.session.get(Login, host_login_id)
                launched_on = (host_of(launch_url), lg.name if lg is not None else str(host_login_id))
        # Whose host list this segment went out on, for the watchdog's roll (§14.6): kept
        # beside the segment it launched, never re-derived from the seat afterward.
        live = get_state(recording_id)
        if live is not None:
            live.host_login_id = host_login_id
        hls_variant = _hls_variant_for(get_state(recording_id), launch_url, cfg, recording_id)
        cmd = build_capture_cmd(cfg, launch_url, seg_path, segment_duration,
                                pace_realtime=pace, keep_video_track=True,
                                hls_program=hls_variant.program if hls_variant else 0)
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
                from .alerts import clear_recording_retrying_alert
                clear_recording_retrying_alert(recording_id)
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
            if hls_variant and hls_variant.offered > 1:
                detail += (f', recording {hls_variant.label or "the best"} of the '
                           f'{hls_variant.offered} qualities its playlist offers')
            elif hls_variant and not hls_variant.offered:
                detail += (', recording the first quality its playlist lists, since the '
                           'playlist could not be read to choose one')
            if launched_on is not None:
                detail += f', on host {launched_on[0]}, login "{launched_on[1]}"\'s own'
            # The version that captured it, which a support bundle built after an upgrade
            # cannot tell from its own. Segment 1 only: every extra_data renders as a
            # disclosure in the event log, and one per reconnect is clutter
            # (dev/changelog/1193).
            add_recording_event(recording_id, SEGMENT_STARTED, detail=detail,
                                segment_number=seg_num,
                                extra={'app_version': __version__} if seg_num == 1 else None)
            db.session.commit()
            return seg.id

        try:
            seg_id = _record_segment_started_and_commit()
        except Exception:
            # ffmpeg is running and nothing can reach it: no segment row names its file and
            # the live state does not hold the process yet. It is this launch's to end
            # before the error leaves here (dev/docs/BUGS.md 2026-11-16).
            # A warning: the raise goes on to the caller, which reports it.
            log.warning('Recording %d seg %d: could not record that the segment started; '
                        'stopping the ffmpeg just launched (pid=%d)', recording_id, seg_num,
                        proc.pid)
            terminate_or_kill(proc, hard=True)
            _discard_stderr_spool(stderr_path, stderr_fh)
            try:
                os.unlink(seg_path)
            except OSError:
                pass  # ffmpeg had written nothing yet, or the folder is the problem
            raise

        state = get_state(recording_id)
        if state is None or state.stop_event.is_set():
            # A teardown ran concurrently. Either it has finished - it closed every open
            # segment and popped _active BEFORE the row above was committed - or it has
            # begun: stop_event is the first thing every teardown sets, and a launch that
            # passes this point after it would hand the teardown's kill a process it has
            # already looked at and leave this one reachable from nothing, holding a
            # connection whose seat is about to be released (dev/changelog/1242). Either
            # way nothing else will ever close this row or kill this child. Releasing them
            # here is the create path's own teardown obligation - the kill and the unlink
            # stay outside the retried closure, since neither is idempotent.
            terminate_or_kill(proc, hard=True)
            _discard_stderr_spool(stderr_path, stderr_fh)
            _abandon_raced_segment(recording_id, seg_id, seg_num, seg_path)
            return LAUNCH_ABANDONED

        state.process = proc
        if state.stop_event.is_set():
            # The teardown set the event between the check above and the assignment. Its
            # kill read state.process before this write, so it reaped the old child or
            # nothing; this one is ours to end, and the teardown's own close of the open
            # row is honored rather than repeated (_abandon_raced_segment).
            terminate_or_kill(proc, hard=True)
            _discard_stderr_spool(stderr_path, stderr_fh)
            _abandon_raced_segment(recording_id, seg_id, seg_num, seg_path)
            return LAUNCH_ABANDONED
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
    threading.Thread(
        target=do_concatenation, args=(app, old_recording_id), daemon=True,
        kwargs={'reason': f'Handed off to recording #{new_recording_id}; beginning the join'},
    ).start()
    ev.publish(old_recording_id, RECORDING_HANDOFF, {'handed_off_to': new_recording_id})


def recording_format_pin_segment(recording_id: int):
    """The segment the recording's format pin is read from - the EARLIEST one that
    carries a capture-time probe and was not discarded - or None. The watchdog asks this
    to know which segment to re-read once it has run a while (dev/changelog/1308)."""
    return (RecordingSegment.query
            .filter(RecordingSegment.recording_id == recording_id,
                    RecordingSegment.probe_resolution.isnot(None),
                    RecordingSegment.probe_fps.isnot(None),
                    RecordingSegment.excluded_reason.is_(None))
            .order_by(RecordingSegment.segment_number.asc())
            .first())


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
    the rest of the run (dev/changelog/957).

    That segment's SETTLED reading wins over its opening one when the watchdog has taken
    it (segment_pin_format_key): the opening probe sees the first two megabytes, and a
    provider in trouble can open on a minute of a lower format inside hours of its usual
    one (dev/changelog/1201), which would steer every failover to the lower format for
    the whole run (dev/changelog/1308). Requires an app context."""
    return segment_pin_format_key(recording_format_pin_segment(recording_id))


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


@dataclass
class GameMemberFilter:
    """game_members()' answer: `members` to choose from, `skipped` the ones left out because
    their own guide shows something else then, as [(channel, what it shows)], `carry` the ids of
    those whose guide shows the game, and `game` the game's name, or '' when the recording is of
    no game this can tell."""
    members: list
    skipped: list
    carry: frozenset = frozenset()
    game: str = ''

    def preferred(self, members) -> list:
        """`members` narrowed to those whose guide shows the game, when any is among them: a
        station with no guide may be showing the other game, so it is used only when no
        carrier can be (sports_match.members_for_game())."""
        carriers = [ch for ch in members if ch.id in self.carry]
        return carriers or list(members)


def game_members(rec, members) -> GameMemberFilter:
    """Narrow a group recording's candidate `members` to those that may carry the game its
    listing is of (sports_match.recording_game(), members_for_game()). A group of local stations
    shows each city's own game in a regional window, and a recording of one game must not be made
    from a station showing another (dev/changelog/1690). Anything not tied to one game - no source
    listing, no match there, a listing of two games - leaves `members` as they are. Applied where
    a member is chosen, before both format filters, like a member that already failed, so neither
    filter's zero-survivors override counts a member showing another game. Three queries at most,
    none for a recording with no source listing."""
    from .program_titles import title_key
    from .sports_data import event_game_name
    from .sports_match import members_for_game, recording_game
    event = recording_game(rec)
    if event is None or not members:
        return GameMemberFilter(list(members), [])
    found = members_for_game([ch.id for ch in members], event.id, rec.program_start_time,
                             title_key(rec.program_title))
    return GameMemberFilter([ch for ch in members if ch.id in found.keep],
                            [(ch, found.other[ch.id]) for ch in members if ch.id in found.other],
                            found.carry, event_game_name(event))


def _skipped_game_note(skipped, game) -> str:
    """The clause a member-selection event adds for members left out by game_members(): how many,
    and the first few by name with what each shows instead."""
    if not skipped:
        return ''
    shown = ', '.join(f'"{ch.name}" ({what})' for ch, what in skipped[:3])
    more = f' and {len(skipped) - 3} more' if len(skipped) > 3 else ''
    return (f' - skipped {counted(len(skipped), "member")} whose guide shows something other than '
            f'{game} at that time: {shown}{more}')


def _members_near(channel, members) -> tuple:
    """(avoid, same_login): the members that share a backend with `channel`. `avoid` is the
    id sets a feed-death failover keeps away from, widest first: everything on its account's
    provider or one of its logins, then only what is on one of its logins (its own account
    among them), an empty or repeated set left out. `same_login` is that second set whether
    or not it is in `avoid`, for saying which of the two a member shares. Two queries.

    One provider is one backend, so what killed a feed there may have killed its copies; one
    login is one seat pool as well (DESIGN-account-providers.md 9-10)."""
    from .account_links import login_siblings
    from .database import Account
    if channel is None or channel.account_id is None:
        return (), frozenset()
    own = channel.account_id
    logins = {own} | set(login_siblings([own]).get(own, {}))
    provider_of = dict(db.session.query(Account.id, Account.provider_id).filter(
        Account.id.in_({ch.account_id for ch in members} | {own})))
    provider = provider_of.get(own)
    same_login = frozenset(ch.id for ch in members if ch.account_id in logins)
    same_provider = same_login | frozenset(
        ch.id for ch in members
        if provider is not None and provider_of.get(ch.account_id) == provider)
    out = []
    for ids in (same_provider, same_login):
        if ids and (not out or ids != out[-1]):
            out.append(ids)
    return tuple(out), same_login


def _pick_by_tiers(members, latest_by_channel, stay_put, busy, busy_account, demoted,
                   streak_threshold, near=(), viewer_account=frozenset()):
    """The failover's preference tiers, best-first; the first that yields a member wins.
    The inner ones are the long-standing busy cascade - prefer a member whose account has
    a slot nobody is using, then one whose slot a picture-in-picture channel's small picture
    holds, then one whose slot an IPTV player on the output feed is watching through
    (`viewer_account` and its `watched` part, which the move cuts off - dev/changelog/1642,
    dev/docs/DESIGN-pip-channels.md 8.2), then any free member,
    then knowingly take the best busy one rather than abort (busy-member skip rule; no
    handoff fires mid-recording, so both recordings share the feed's provider connection). The
    tiers that spare a player never yield a member on a failing streak.

    A demote pass wraps a second round outside them: every tier is tried against the
    not-yet-demoted members first, and only then re-tried with the demoted ones allowed
    back in. That second round is what keeps a small group cycling instead of running dry
    - a three-member group goes A -> B -> C -> A - and it ranks by health score, which each
    demotion has just made more honest about this run.

    `near` (_members_near) is the innermost preference and only a feed death passes it:
    inside each busy tier, a member off the dead feed's provider is tried first, then one off
    its login, then anyone. A free connection still comes first, since a member that cannot
    take the seat is no move at all, and health score ranks inside each step. A member on a
    failing streak is never preferred for where it sits: distance from a dead backend does
    not outrank a feed the health checks say is down (dev/changelog/1603)."""
    best = None
    for preferred in ([demoted, frozenset()] if demoted else [frozenset()]):
        base = set(stay_put) | set(preferred)
        watched = _watched(viewer_account)
        tiers = [(base | busy | busy_account | viewer_account, bool(viewer_account)),
                 (base | busy | busy_account | watched, bool(watched)),
                 (base | busy | busy_account, False), (base | busy, False), (base, False)]
        seen = []
        for tier, spares_viewer in tiers:
            # A tier that spares an IPTV player never prefers a feed on a failing streak, as
            # the backend preference never does: the recording always wins.
            if tier in seen:
                continue
            seen.append(tier)
            for avoid in (*near, frozenset()):
                best = pick_best_member(members, latest_by_channel, exclude_ids=tier | avoid,
                                        streak_threshold=streak_threshold)
                if (best is not None and (avoid or spares_viewer)
                        and is_streaking(best, streak_threshold)):
                    best = None
                if best is not None:
                    break
            if best is not None:
                break
        if best is not None:
            break
    return best


SwitchChoice = namedtuple('SwitchChoice', 'channel ok why warn')


def switch_choices(recording_id: int) -> list:
    """Every channel the Switch channel dialog offers for a running group recording, with
    whether it can be picked right now and why, in the order the manual move reads them
    (failover_group_member with a target): the one on screen, a feed that died this run,
    a blocked account, an account at its limit, then the format against this recording's.
    One reading for the dialog and the route, so they cannot disagree (dev/changelog/1254).

    A member whose Recording switch is off is not offered at all (dev/changelog/1254): the
    switch is the user's own answer to "would I record from this feed", and a dialog on the
    recording page is not where that answer changes. A format that differs from the recording's is
    offered and marked `warn` - the lock filters, it never refuses. Requires an app context;
    [] for anything that is not a group recording running in this process."""
    from .account_blocks import blocked_account_ids
    from .routes.channel_tests import _latest_tests_by_channel
    from . import connection_limits as connlim
    rec = db.session.get(Recording, recording_id)
    state = get_state(recording_id)
    if rec is None or rec.group is None or state is None:
        return []
    members = recording_members(rec.group.memberships)
    account_ids = {ch.account_id for ch in members}
    blocked = blocked_account_ids(account_ids)
    standing = connlim.seat_standing(account_ids, 'recording',
                                     exclude_holder=('recording', recording_id))
    full = {a for a, s in standing.items() if s == connlim.SEAT_NONE}
    from .account_links import describe_unusable, unusable_reasons
    switched_off = unusable_reasons(account_ids)
    latest = _latest_tests_by_channel([ch.id for ch in members])
    pin = recording_format_pin(recording_id)
    by_game = game_members(rec, members)
    shows_other = dict((ch.id, what) for ch, what in by_game.skipped)
    current = rec.channel
    out = []
    for ch in members:
        key = format_key(latest.get(ch.id))
        warn = False
        if ch.id == rec.channel_id:
            ok, why = False, 'Recording from it now.'
        elif ch.id in state.failed_member_ids:
            ok, why = False, ('Its feed died earlier in this recording - not offered again '
                              'this run.')
        elif ch.account_id in blocked:
            ok, why = False, f'"{ch.account.name}" is blocked for the rest of this recording.'
        elif ch.account_id in switched_off and (current is None
                                                or ch.account_id != current.account_id):
            ok, why = False, describe_unusable(ch.account.name, switched_off[ch.account_id],
                                               capital=True) + '.'
        elif ch.account_id in full and (current is None or ch.account_id != current.account_id):
            ok, why = False, (f'"{ch.account.name}" is at its connection limit - every slot '
                              f'is held by another recording.')
        elif ch.id in shows_other:
            # Offered, as a format that differs is: the pick is the user's (dev/changelog/1690).
            ok, why, warn = True, (f'Its guide shows {shows_other[ch.id]} at this time, not '
                                   f'{by_game.game}.'), True
        elif key is None:
            ok, why = True, ('Never tested - its format is unknown, so it is not filtered '
                             'on format.')
        elif pin is not None and key != pin:
            ok, why, warn = True, (f'Records at {format_label(key)}; this recording is at '
                                   f'{format_label(pin)}, so the file changes format where '
                                   f'you switch.'), True
        elif pin is not None:
            ok, why = True, f'Same format as this recording ({format_label(key)}).'
        else:
            ok, why = True, f'Records at {format_label(key)}.'
        if ok and ch.id in state.demoted_member_ids:
            why += ' Moved off earlier in this recording for stalling.'
        seat = standing.get(ch.account_id)
        other_account = current is None or ch.account_id != current.account_id
        if ok and other_account and seat == connlim.SEAT_BY_CUTTING_VIEWER:
            why += (f' An IPTV player is watching on "{ch.account.name}"; switching cuts it off '
                    f'unless it can move to another channel.')
        elif ok and other_account and seat == connlim.SEAT_BY_PAUSING_PICTURE:
            why += (f' A picture-in-picture channel is using "{ch.account.name}"; switching pauses '
                    f'one of its pictures, and a TV watching it keeps its big picture.')
        out.append(SwitchChoice(ch, ok, why, warn))
    return out


def block_move_preview(recording_id: int, pending: dict):
    """What blocking `pending` ({account_id: slots}) would do to a running group recording,
    answered before the block is saved rather than discovered by the watchdog 15 s later
    (dev/changelog/1254). None when the blocks leave the account the recording is on with
    a usable slot; otherwise {'to': Channel or None, 'held': bool} - the member the block
    move would land on, read with the move's own filters and tiers, and whether that member
    sits on an account at its connection limit, where the seat move is refused and the
    recording stays put. Advisory: a partial block that still leaves the account a slot
    reads as no move here even where the watchdog's own seat count would say otherwise."""
    from .account_blocks import blocked_account_ids, split_blocked
    from .routes.channel_tests import _latest_tests_by_channel
    rec = db.session.get(Recording, recording_id)
    state = get_state(recording_id)
    if rec is None or rec.group is None or state is None or rec.channel is None:
        return None
    current = rec.channel
    members = [ch for ch in recording_members(rec.group.memberships)
               if ch.id not in state.failed_member_ids]
    blocked = blocked_account_ids({ch.account_id for ch in members} | {current.account_id},
                                  pending=pending)
    if current.account_id not in blocked:
        return None
    members, _ = split_blocked(members, blocked)
    by_game = game_members(rec, members)
    members = by_game.preferred(by_game.members)
    latest = _latest_tests_by_channel([ch.id for ch in members])
    selection = format_eligible_members(rec.group, members, latest)
    members, _ = _pin_eligible_members(selection.members, recording_format_pin(recording_id),
                                       latest)
    busy = _busy_channel_ids(recording_id)
    busy_account, viewer_account = _busy_account_channel_ids(members, recording_id)
    stay_put = {current.id} | {ch.id for ch in members if ch.account_id == current.account_id}
    streak = load_config().get('channel_testing', {}).get(
        'failing_streak_threshold', DEFAULT_FAILING_STREAK_THRESHOLD)
    best = _pick_by_tiers(members, latest, stay_put, busy, busy_account, frozenset(), streak,
                          viewer_account=viewer_account)
    return {'to': best, 'held': best is not None and best.id in busy_account}


def failover_group_member(app, recording_id: int, reason: str, demote: bool = False,
                          score_departure: bool = True, block_move: bool = False,
                          target_channel_id: Optional[int] = None) -> bool:
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

    `block_move=True` is the watchdog moving a recording off an account the user has just
    blocked (dev/changelog/1151). Voluntary like a demotion - the current member is excluded
    and a False means stay put - but the departing member is neither demoted nor burned and
    is never scored: nothing about the feed was wrong, and a block must not count against
    anything. Its account being blocked is what keeps it from being chosen again.

    `target_channel_id` is the user's own pick - the recording page's Switch channel
    (dev/changelog/1254). Like a block move the departing member is neither burned, demoted
    nor scored; unlike every other path there is no ranking, because the choice was made.
    The format filters are not applied either: a pick that differs from the recording's
    format is the user overriding the lock on purpose, and the event says so rather than
    refusing (the lock filters, it never refuses - dev/changelog/754). A target that is no
    longer available by the time the watchdog acts - its feed died, its account was blocked
    - is written on the recording as a DIAGNOSTICS event and the recording stays put. The
    connection seat moves exactly as for every other cross-account move.

    Members on a blocked account are dropped on every path, before either format filter, the
    same way a member that already failed is. Unlike record start there is no fall back to
    them: a live recording with nowhere unblocked to go keeps the member it has.

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

        manual = target_channel_id is not None
        old_channel = rec.channel
        if rec.channel_id is not None and not block_move and not manual:
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
        from .account_blocks import blocked_account_ids, split_blocked
        members, skipped_blocked = split_blocked(
            members, blocked_account_ids({ch.account_id for ch in members}))
        from .routes.channel_tests import _latest_tests_by_channel
        latest_by_channel = _latest_tests_by_channel([ch.id for ch in members])
        pin_mismatch = False
        target_key = None
        # The member health score alone would have picked, when the dead feed's backend
        # moved the choice off it, and what it shares with that feed.
        passed_near, passed_shares = None, ''
        if manual:
            # The user's pick. No ranking and no format filter - but the busy sets are
            # still read, so the event can say what the pick cost, and the pin is read so
            # a format change is named rather than discovered in the file.
            best = next((ch for ch in members if ch.id == target_channel_id), None)
            selection = None
            pin = recording_format_pin(recording_id)
            pin_override = False
            demoted = frozenset()
            busy = _busy_channel_ids(recording_id)
            busy_account, viewer_account = _busy_account_channel_ids(members, recording_id)
            if best is None:
                target = db.session.get(Channel, target_channel_id)
                target_name = target.name if target is not None else f'channel {target_channel_id}'
                why = ('its feed already died during this recording'
                       if target_channel_id in state.failed_member_ids else
                       'its account is blocked for this recording'
                       if target is not None and any(ch.account_id == target.account_id
                                                     for ch in skipped_blocked) else
                       'it is no longer a member this group records from')
                log.warning('Recording %d: not switching to "%s" - %s', recording_id,
                            target_name, why)

                @retry_on_locked()
                def _log_switch_refused_and_commit():
                    add_recording_event(
                        recording_id, DIAGNOSTICS,
                        detail=(f'Not switched to "{target_name}": {why}. The recording stays '
                                f'on "{old_channel.name if old_channel else "?"}".'),
                        extra={'kind': 'switch_refused', 'group_id': rec.group_id,
                               'to_channel_id': target_channel_id})
                    db.session.commit()

                _log_switch_refused_and_commit()
                return False
            target_key = format_key(latest_by_channel.get(best.id))
            pin_mismatch = pin is not None and target_key is not None and target_key != pin
        else:
            # Same filter as record start, so a failover cannot land on a format the group
            # would have refused to start on. When the lock leaves nothing, it hands back the
            # unfiltered list rather than an empty one - a live capture is never abandoned to
            # a format rule (15.2, principle 2).
            # Never onto a member showing another game: that would splice it into the file
            # (dev/changelog/1690). Unlike record start, no override - the recording keeps
            # what it has and ends as every other one with no member left does.
            by_game = game_members(rec, members)
            if by_game.skipped:
                log.info('Recording %d: failover leaves out %s', recording_id,
                         _skipped_game_note(by_game.skipped, by_game.game).lstrip(' -'))
            members = by_game.preferred(by_game.members)
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
            busy_account, viewer_account = _busy_account_channel_ids(members, recording_id)
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
            stay_put = ({rec.channel_id} if ((demote or block_move) and rec.channel_id is not None)
                        else frozenset())
            if block_move and old_channel is not None:
                # A partial block leaves the account some slots, so its other members survive
                # the blocked-account drop above - but moving to one of them keeps the very
                # connection the block asked back.
                stay_put = set(stay_put) | {ch.id for ch in members
                                            if ch.account_id == old_channel.account_id}
            # Only a feed that died says anything about its backend. A stall move and a
            # block move leave a working feed, so they rank as they always have.
            near, near_login = (((), frozenset()) if (demote or block_move)
                                else _members_near(old_channel, members))
            best = _pick_by_tiers(members, latest_by_channel, stay_put, busy, busy_account,
                                  demoted, streak_threshold, near=near,
                                  viewer_account=viewer_account)
            if near and best is not None:
                plain = _pick_by_tiers(members, latest_by_channel, stay_put, busy,
                                       busy_account, demoted, streak_threshold,
                                       viewer_account=viewer_account)
                if plain is not None and plain.id != best.id:
                    passed_near = plain
                    passed_shares = ('its account' if plain.account_id == old_channel.account_id
                                     else 'its login' if plain.id in near_login
                                     else 'its provider')
        took_busy = best is not None and best.id in busy
        took_busy_account = best is not None and best.id in busy_account
        took_viewer_account = best is not None and best.id in viewer_account
        took_demoted = best is not None and best.id in demoted
        if best is None:
            # Never reached on a manual move: its pick was resolved above.
            if block_move:
                log.info('Recording %d: group "%s" has no member off a blocked account (%s) - '
                         'staying on the current member', recording_id, rec.group.name, reason)
                return False
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
        # A same-account move keeps the seat the recording holds and cuts nobody off.
        cross = old_channel is None or best.account_id != old_channel.account_id
        took_viewer_account = took_viewer_account and cross
        skipped_viewer_account = [] if (manual or took_busy_account) else [
            ch for ch in members if ch.id in viewer_account and ch.id not in busy
            and ch.id != best.id and ch.id not in stay_put]

        # Cross-account switch: move the connection seat. The acquire can refuse rather
        # than exceed the target account's limit (dev/changelog/854), and the move is one
        # step - the old seat is given up only once the new one is taken - so a refusal
        # changes nothing. A release followed by a re-acquire on the old account used to be
        # how this was done, and the re-acquire was assumed unable to fail; it fails
        # whenever the old account is the one the user just blocked, whose usable limit
        # is 0, which is exactly when a block move asks (dev/changelog/1242).
        #
        # Refusing the swap is the whole answer here, and deliberately not a wait: this
        # runs on the watchdog thread with the capture already dead, and blocking it would
        # stall the recording's own supervision. Returning False drops the caller into its
        # existing dead-stream retry/abort path, which is itself a wait-and-retry loop and
        # re-enters this function with the accounts re-read; a block move is re-asked on
        # the watchdog's stay recheck, and the seat it still holds is what keeps that
        # recheck asking.
        old_account_id = old_channel.account_id if old_channel else None
        if old_account_id is not None and best.account_id != old_account_id:
            from . import connection_limits as connlim
            target_account_id = best.account_id

            def _move_seat():
                return connlim.move_seat('recording', recording_id, old_account_id,
                                         target_account_id)

            if not _try_acquire_slot_with_preemption(app, recording_id, target_account_id,
                                                     acquire=_move_seat):
                from .account_links import unusable_reason
                off = unusable_reason(target_account_id, best.account.name)
                held_why = (f'{off}, so nothing on it can stream' if off else
                            f'account "{best.account.name}" is at its connection limit and '
                            f'every slot is held by another recording')
                log.warning('Recording %d: cannot fail over to "%s" - %s; not moving (%s)',
                            recording_id, best.name, held_why, reason)

                @retry_on_locked()
                def _log_failover_blocked_and_commit():
                    add_recording_event(
                        recording_id, DIAGNOSTICS,
                        detail=(f'Failover to "{best.name}" was held back: {held_why}. The '
                                f'recording was not moved'
                                + ('.' if off else ' rather than opening a connection over '
                                   'the limit.')),
                        extra={'kind': ('failover_blocked_by_switched_off' if off else
                                        'failover_blocked_by_connection_limit'),
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
        busy_note += _viewer_account_note(best, took_viewer_account, skipped_viewer_account,
                                          viewer_account)
        override_note = ''
        if selection is not None and selection.override:
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
        if passed_near is not None:
            override_note += (f' - "{passed_near.name}" ({passed_near.account.name}, score '
                              f'{effective_score(passed_near)}) ranks higher but shares '
                              f'{passed_shares} with the feed that died, so a member that '
                              f'does not was tried first')
        if skipped_blocked and not block_move and not manual:
            override_note += (' - skipped member(s) on a blocked account: '
                              + ', '.join(f'"{ch.name}" ({ch.account.name})'
                                          for ch in skipped_blocked))
        if manual:
            if pin_mismatch:
                override_note += (f' - "{best.name}" records at {format_label(target_key)} and '
                                  f'this recording is at {format_label(pin)}, so the file '
                                  f'changes format here rather than refusing your pick')
            elif target_key is None:
                override_note += (f' - "{best.name}" has never been tested, so its format is '
                                  f'unknown until this segment is probed')
            detail = (f'Group "{rec.group.name}": you asked to move from "{old_name}" to '
                      f'"{best.name}" ({best.account.name}, score {effective_score(best)}). '
                      f'Nothing was wrong with "{old_name}"; it is not scored for the move'
                      f'{busy_note}{override_note}')
        elif block_move:
            detail = (f'Group "{rec.group.name}": {reason} - moving from "{old_name}" to '
                      f'"{best.name}" ({best.account.name}, score {effective_score(best)}). '
                      f'Nothing was wrong with "{old_name}"; it is not scored for the move'
                      f'{busy_note}{override_note}')
        elif demote:
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
                       'took_viewer_account': took_viewer_account,
                       'skipped_viewer_account_channel_ids': [
                           ch.id for ch in skipped_viewer_account],
                       'format_pin': list(pin) if pin else None,
                       'pin_override': pin_override,
                       # True = a voluntary stall-rate move, so from_channel_id was
                       # demoted rather than burned and can be selected again this run.
                       'demoted': demote,
                       'took_demoted': took_demoted,
                       'passed_near_channel_id': passed_near.id if passed_near else None,
                       'block_move': block_move,
                       # True = the user picked this member (Switch channel); from_channel_id
                       # is neither burned nor demoted, and the format filters were the
                       # user's to override - format_mismatch says whether they were.
                       'manual': manual,
                       'format_mismatch': pin_mismatch,
                       'skipped_blocked_channel_ids': [ch.id for ch in skipped_blocked],
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
        if old_channel is not None and score_departure and not block_move and not manual:
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
            'manual': manual,
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
    from .database import Account, ACCOUNT_STATUS_SYNCING
    account = db.session.get(Account, account_id)
    if account is None or account.status != ACCOUNT_STATUS_SYNCING:
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


def _try_acquire_slot_with_preemption(app, recording_id: int, account_id: int,
                                      acquire=None) -> bool:
    """Acquire a connection slot on account_id for this recording, or return False.

    `acquire` is the one call that registers the seat, tried once before any preemption
    and once after each; the default is a plain try_acquire on `account_id`. A failover
    passes connection_limits.move_seat so the seat it already holds on the old account is
    given up in the same step the new one is taken (dev/changelog/1242).

    Recordings still always win over the actors that do not hold a stream open on the
    user's behalf: an in-flight sync on this account is canceled, and an IPTV player on
    the output feed, then a live preview, then a running channel test are preempted so
    the recording can take the slot, in that order. What this will NOT do
    is exceed the account's limit. When every slot is held by another *recording*, it
    returns False and the caller waits for one to free
    (dev/docs/DESIGN-concurrency.md 1 doctrine 1, amended - dev/changelog/854).

    The limit is a provider-side ceiling, so crossing it risks the whole account rather
    than this one recording: a provider that throttles or bans over concurrent-connection
    abuse takes down every channel on it, including the recording already running
    legitimately. That is the clause CLAUDE.md principle 2 already carried - complete the
    recording, "never by hammering a provider hard enough to risk the account".

    An IPTV player it cuts off that can play from another channel (a group, a sports channel)
    is moved there rather than ended - but only once this returns, whatever it returns: the
    recording takes the seat first and never waits on the move, and a move can never take back a
    seat being freed for the recording (output_feed.move_preempted, dev/changelog/1642).
    """
    movers = []
    try:
        return _acquire_with_preemption(app, recording_id, account_id, acquire, movers)
    finally:
        if movers:
            from . import output_feed
            output_feed.move_preempted(app, movers, recording_id)


#: The most viewers one acquire takes a seat from before it moves on to previews and tests.
_MAX_VIEWER_CUTS = 32


def _acquire_with_preemption(app, recording_id: int, account_id: int, acquire, movers: list) -> bool:
    _preempt_sync_for_recording(recording_id, account_id)

    from . import connection_limits as connlim
    if acquire is None:
        def acquire():
            return connlim.try_acquire(account_id, 'recording', recording_id)
    if acquire():
        return True
    # Every host or login on the account turned off: its usable limit is 0, so cutting off what
    # it still has running would free nothing - and a stream already running keeps going when
    # a switch goes off (dev/changelog/1670).
    from .account_links import unusable_reasons
    if account_id in unusable_reasons([account_id]):
        return False
    # An IPTV player on the output feed yields first of all: it is entertainment that resumes
    # on its own, below the operator's own look and the measurement feeding the health
    # score (DESIGN-output-feed.md 4.4). The viewer is told why - the session ends by name
    # and an alert names this recording.
    #
    # One at a time, the readiest to give way first (a picture-in-picture channel's small
    # picture before a play a TV would lose its picture without), and no more once the
    # recording is seated: a recording that needs one connection cuts one viewer
    # (DESIGN-pip-channels.md 8.2). The bound is for a player that keeps retaking a seat.
    from . import output_feed
    for _ in range(_MAX_VIEWER_CUTS):
        viewers = connlim.preempt_viewers_for_slot(account_id, at_most=1)
        if not viewers:
            break
        for holder_account_id in dict.fromkeys(a for _, a in viewers):
            output_feed.preempt_for_account(app, holder_account_id, recording_id, movers=movers)
            log.warning('Recording %d: preempted an IPTV player on account %d%s',
                        recording_id, holder_account_id,
                        '' if holder_account_id == account_id
                        else f' (sharing a login with account {account_id})')
        if acquire():
            return True
    # A live preview yields next: someone looking at a channel is worth less than a
    # measurement feeding its health score, and far less than the recording itself.
    # Both strips follow the POOL, not the account: a login shared across accounts is one
    # seat set, so the preview or test holding its seat may have come through the sibling
    # account, and the kill is routed to the account it came through (dev/changelog/1243).
    previews = connlim.preempt_previews_for_slot(account_id)
    if previews:
        from . import preview
        for holder_account_id in dict.fromkeys(a for _, a in previews):
            preview.preempt_for_account(holder_account_id)
            log.warning('Recording %d: preempted a live preview on account %d%s',
                        recording_id, holder_account_id,
                        '' if holder_account_id == account_id
                        else f' (sharing a login with account {account_id})')
        if acquire():
            return True
    for _channel_id, holder_account_id in connlim.preempt_tests_for_slot(account_id):
        from . import channel_tester
        channel_tester.kill_active_test_for_account(app, holder_account_id)
        log.warning('Recording %d: preempted a running channel test on account %d%s',
                    recording_id, holder_account_id,
                    '' if holder_account_id == account_id
                    else f' (sharing a login with account {account_id})')
    return acquire()


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


def _account_label(account_id: int) -> str:
    from .database import Account
    account = db.session.get(Account, account_id)
    return account.name if account else f'#{account_id}'


def _slot_wait_for(account_id: int, waiting_on=None) -> str:
    """The short form of _slot_wait_reason below, completing "waiting for ..." on a list
    row, where the full sentence has no room. The event and the alert keep the long one."""
    phrase = f'a connection slot on "{_account_label(account_id)}"'
    if waiting_on is not None:
        phrase += f', behind "{waiting_on.name}"'
    return phrase


def _slot_wait_reason(account_id: int, waiting_on=None) -> str:
    """Plain-language why-this-recording-is-waiting, shared by every surface that says so.

    Written once here rather than at each site so the event, the alert and the log line
    cannot drift into describing the wait three different ways.
    """
    account_name = _account_label(account_id)
    if waiting_on is not None:
        return (f'recording "{waiting_on.name}" (#{waiting_on.id}) has been waiting longer '
                f'for a connection slot on account "{account_name}"')
    # Connections the IPTV output keeps for players are never a recording's, so a wait they
    # cause says so rather than blaming another recording (DESIGN-output-feed.md 5.3, 5.5).
    from .output_feed import held_seats_now
    held = held_seats_now([account_id]).get(account_id, 0)
    if held:
        return (f'every connection on account "{account_name}" that recordings may use is taken: '
                f'{counted(held, "connection")} {"is" if held == 1 else "are"} left alone for IPTV '
                f'players ("Recordings leave alone" on the IPTV Output page)')
    return (f'every connection slot on account "{account_name}" is in use by another '
            f'recording')


def _note_slot_wait_once(recording_id: int, account_id: int, why: str, waiting_on=None,
                         move_note=None):
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
                    f'this recording\'s window is still open.'
                    + (f' {move_note}' if move_note else '')),
            extra={'kind': 'connection_limit', 'account_id': account_id,
                   'waiting_on_recording_id': waiting_on.id if waiting_on else None,
                   'move_note': move_note})
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


def _note_start_deferred(recording_id: int, waiting_for: str) -> None:
    """Record on the row that this SCHEDULED recording's start is being held, and for what.

    The one writer that sets Recording.start_deferred_since/_for; end_capture_wait() below
    is the one that clears them. `since` is stamped on the first deferral of a wait and
    never moved, so the row can say how long it has really been waiting; `waiting_for` is
    rewritten when the reason changes mid-wait (a slot waiter that then meets a live
    conversion). Commits only when something changes - the wait re-enters every poll, and
    a commit per poll would be a write per recording every 30 seconds for nothing.

    Written only while the row is still SCHEDULED, checked inside the retried unit: a
    cancel that committed first has already ended the wait, and stamping the row after it
    would leave a cancelled recording claiming to wait (dev/changelog/1141).
    """
    @retry_on_locked()
    def _stamp_and_commit():
        r = db.session.get(Recording, recording_id)
        if r is None or r.status != REC_STATUS_SCHEDULED:
            return
        if r.start_deferred_since is not None and r.start_deferred_for == waiting_for:
            return
        if r.start_deferred_since is None:
            r.start_deferred_since = datetime.utcnow()
        r.start_deferred_for = waiting_for
        db.session.commit()

    _stamp_and_commit()


def end_capture_wait(recording_id: int):
    """Everything that says this recording is waiting to capture comes down with the wait.

    Two facts, one exit. The standing RECORDING_WAITING_FOR_CONNECTION_SLOT alert (the
    other half of _note_slot_wait_once above) describes a condition that is still true
    while the row stands, so the Alerts page lists it under "Active alerts" and offers no
    Dismiss (dev/changelog/933); and the row's start_deferred_since/_for pair is what the
    Recordings list and the Dashboard read to say what a late start is waiting for
    (dev/changelog/1141). Both are promises that only hold if EVERY way out of the wait
    clears them, not just the one where the recording starts - which is why they share
    this one function rather than each keeping its own list of call sites.

    The alert is keyed on the recording id rather than the (type, source) pair: the source
    names the account, and a second recording still queued on that same account has to
    keep its own row. A no-op when nothing stands, so it is safe on the paths that never
    waited at all - which is most of them, and is why it sits beside
    dismiss_recording_failing_alerts at each site rather than behind a "did this one wait"
    test of its own. Call it after the caller's own commit, never inside it.
    """
    @retry_on_locked()
    def _clear_deferral_and_commit():
        r = db.session.get(Recording, recording_id)
        if r is None or (r.start_deferred_since is None and r.start_deferred_for is None):
            return
        r.start_deferred_since = None
        r.start_deferred_for = None
        db.session.commit()

    _clear_deferral_and_commit()
    from .alerts import dismiss_open_alerts_for_recording
    dismiss_open_alerts_for_recording(
        recording_id, 'RECORDING_WAITING_FOR_CONNECTION_SLOT')


#: How many channels with the same program a start tries to take a seat on before it waits.
MAX_START_MOVE_TRIES = 6


class StartMove(NamedTuple):
    """What _move_off_full_account() did. `account_id` is the account the recording now holds
    a seat on, when it moved. `note` is why it did not. `lost` means another start of the same
    recording (or a cancel or an edit) changed the row first: this start stops, and neither
    waits nor writes anything on a recording that is no longer its own."""
    account_id: Optional[int] = None
    note: Optional[str] = None
    lost: bool = False


def _move_off_full_account(app, recording_id: int, account_id: int) -> StartMove:
    """Move a SCHEDULED single-channel recording whose account has no connection free (full or
    blocked) to another channel with the same program, holding a seat there. Returns the
    account it moved to, or why it did not move - the start then waits as before and its
    deferral event says why (app/seat_moves.py, dev/changelog/1611). A group recording returns
    an empty StartMove: its member choice above already looked for a free account.

    Asked again on every retry of a waiting start, so a seat that frees on another account later
    is taken then. The seat is taken BEFORE the row is rewritten - taking it is the in-memory
    side effect retry_on_locked must never repeat - and given back if the write fails, so a
    seat is never held for a recording still pointing at the full account. The write happens
    only while the row is still SCHEDULED on the channel the move was decided from: two starts
    of one recording can run at once (an overdue job beside the startup sweep, dev/changelog/987),
    and the one that loses must not repoint a row the other is already capturing. The loser
    does not release either: the seat is the one the winner holds (_settle_shared_seat)."""
    from . import seat_moves
    from .accounts import normalize_url
    rec = db.session.get(Recording, recording_id)
    if rec is None or rec.group_id is not None or rec.channel is None:
        return StartMove()
    cfg = load_config()
    level, allowed = seat_moves.move_source(cfg, rec.profile, rec)
    if not allowed:
        return StartMove(note=seat_moves.note_turned_off(level))
    if not seat_moves.has_program(rec):
        return StartMove(note=seat_moves.NOTE_NO_LISTING)
    old = rec.channel
    old_id, old_name = old.id, old.name
    old_account = old.account.name if old.account else f'account {account_id}'
    for ch, tier in seat_moves.start_candidates(rec, account_id, cfg)[:MAX_START_MOVE_TRIES]:
        if not _try_acquire_slot_with_preemption(app, recording_id, ch.account_id):
            continue
        new_id, new_name, new_account_id = ch.id, ch.name, ch.account_id
        new_account = ch.account.name if ch.account else f'account {new_account_id}'
        new_url = normalize_url(ch.stream_url, ch.account, cfg)
        setting = {seat_moves.LEVEL_RECORDING: 'this recording is set to move',
                   seat_moves.LEVEL_PROFILE: 'its profile is set to move',
                   seat_moves.LEVEL_GLOBAL: 'the global setting is to move'}[level]
        detail = (f'Moved from "{old_name}" ({old_account}) to "{new_name}" ({new_account}): '
                  f'{old_account} had no connection free when the recording started, and '
                  f'"{new_name}" shows the same program on an account with one free. Chosen '
                  f'because {seat_moves.TIER_WORDS[tier]}; {setting} when the account is full.')

        @retry_on_locked()
        def _move_and_commit():
            # A conditional UPDATE, as _claim_status_row is and for its reason: two starts
            # that both read the row SCHEDULED on the old channel would both write, each to
            # the channel it picked, and neither would learn it lost.
            moved_rows = Recording.query.filter(
                Recording.id == recording_id,
                Recording.status == REC_STATUS_SCHEDULED,
                Recording.channel_id == old_id,
            ).update({Recording.channel_id: new_id, Recording.url: new_url},
                     synchronize_session='fetch')
            if not moved_rows:
                db.session.rollback()
                return False
            add_recording_event(recording_id, RECORDING_MOVED_FOR_CONNECTION, detail=detail,
                                extra={'from_channel_id': old_id, 'from_account_id': account_id,
                                       'to_channel_id': new_id, 'to_account_id': new_account_id,
                                       'health_tier': tier, 'setting_level': level})
            db.session.commit()
            return True

        try:
            moved = _move_and_commit()
        except Exception:
            # The write decided nothing, so the seat goes where the row is: back to another
            # start that moved it meanwhile, or released when the row is still on the full
            # account. A settle that cannot read the row leaves the seat rather than strip
            # one a winner may hold; the scheduler's retry of this start finds it.
            try:
                db.session.rollback()
                _settle_shared_seat(recording_id, new_account_id, scheduled_counts=True)
            except SQLAlchemyError:
                log.warning('Recording %d: could not settle its seat after a move that '
                            'raised', recording_id, exc_info=True,
                            extra={'recording_id': recording_id})
                db.session.rollback()
            raise
        if not moved:
            _settle_shared_seat(recording_id, new_account_id, scheduled_counts=True)
            log.info('Recording %d: not moved to "%s" - another start already moved or claimed '
                     'it, so this start stops', recording_id, new_name,
                     extra={'recording_id': recording_id})
            return StartMove(lost=True)
        log.info('Recording %d: %s', recording_id, detail, extra={'recording_id': recording_id})
        return StartMove(account_id=new_account_id)
    return StartMove(note=seat_moves.NOTE_NONE_FREE)


def _defer_start_for_slot(app, recording_id: int, account_id: int, waiting_on=None,
                          move_note=None):
    """Hold a SCHEDULED recording that cannot have a connection slot yet, or fail it loudly.

    Same shape as the mp4-conversion collision defer in start_recording, and for the same
    reason: re-arming the start job costs no thread and no new status, and each retry
    re-enters start_recording from the top so the group member, the handoff check and the
    slot acquire are all re-decided against current state rather than replayed.

    `waiting_on` is the recording ahead of this one in the queue, when the wait is the
    fairness yield rather than a full account. `move_note` is why the start did not move to
    another channel (_move_off_full_account), said in the first deferral event.
    """
    now = datetime.utcnow()
    rec = db.session.get(Recording, recording_id, populate_existing=True)
    if rec is None:
        return
    if rec.status != REC_STATUS_SCHEDULED:
        # Another start of this recording claimed it, or a cancel landed: a wait note, a
        # waiting alert or a re-armed start on such a row would describe a recording that is
        # running or over.
        log.info('Recording %d: not deferring its start - the row is %s now', recording_id,
                 rec.status, extra={'recording_id': recording_id})
        return
    if waiting_on is None:
        from .account_blocks import free_at
        block_until = free_at(account_id, now)
        if block_until is not None:
            _defer_start_for_block(rec, account_id, block_until, now, move_note)
            return
        from .account_links import unusable_reason
        off = unusable_reason(account_id, _account_label(account_id))
        if off is not None:
            _defer_start_switched_off(rec, account_id, off, now, move_note)
            return
    why = _slot_wait_reason(account_id, waiting_on)

    if rec.stop_time <= now:
        log.error('Recording "%s" (#%d): %s, and this recording\'s own stop time passed '
                  'while it waited - giving up', rec.name, recording_id, why,
                  extra={'recording_id': recording_id, 'already_alerted': True})

        @retry_on_locked()
        def _fail_slot_wait_and_commit():
            r = db.session.get(Recording, recording_id, populate_existing=True)
            if r is None or r.status != REC_STATUS_SCHEDULED:
                db.session.rollback()
                return False
            r.status = REC_STATUS_FAILED
            r.completed_at = now
            r.failure_reason = FAILURE_CONNECTION_SLOT_TIMEOUT
            add_recording_event(
                recording_id, RECORDING_FAILED,
                detail=(f'Never started: {why}, so starting would have exceeded the '
                        f'account\'s connection limit. Waited for a free slot instead, and '
                        f'this recording\'s own window ended first'))
            db.session.commit()
            return True

        if not _fail_slot_wait_and_commit():
            log.info('Recording %d: not failed for its wait - another start or a cancel '
                     'changed it first', recording_id, extra={'recording_id': recording_id})
            return
        from .health_score import dismiss_recording_failing_alerts
        dismiss_recording_failing_alerts(recording_id)
        # The wait is over, badly: RECORDING_FAILED_CONNECTION_LIMIT below is what the
        # user is owed now, and leaving "waiting for a slot" standing beside it would
        # claim a recording is still queued when it has already given up.
        end_capture_wait(recording_id)
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

    _note_slot_wait_once(recording_id, account_id, why, waiting_on, move_note)
    _note_start_deferred(recording_id, _slot_wait_for(account_id, waiting_on))
    log.info('Recording %d: deferring start - %s', recording_id, why)
    from .scheduler import reschedule_recording_start
    reschedule_recording_start(
        recording_id, now + timedelta(seconds=SLOT_WAIT_POLL_SECONDS))


#: The longest a start waiting on a block sleeps before it re-asks. A block normally ends on
#: its own clock and the retry is armed for that moment, but a block set on a recording ends
#: early when that recording stops early, and nothing else re-arms the waiters then.
BLOCK_WAIT_MAX_POLL_SECONDS = 300


def _note_block_wait_once(recording_id: int, account_id: int, why: str, move_note=None):
    """The deferral event for a wait on a block, written once per account. No alert: the
    user set the block, so the recording waiting on it is a state they chose, shown on the
    recording, not a problem to raise (dev/changelog/1151)."""
    already = [e for e in RecordingEvent.query.filter_by(
        recording_id=recording_id, event_type=RECORDING_START_DEFERRED).all()
        if (e.extra_data or '') and json.loads(e.extra_data).get('kind') == 'account_block'
        and json.loads(e.extra_data).get('account_id') == account_id]
    if already:
        return

    @retry_on_locked()
    def _mark_block_deferred_and_commit():
        add_recording_event(
            recording_id, RECORDING_START_DEFERRED,
            detail=(f'Start deferred: {why}, and every account this recording could use is '
                    f'blocked. It starts as soon as a block ends and records what is left of '
                    f'its window.' + (f' {move_note}' if move_note else '')),
            extra={'kind': 'account_block', 'account_id': account_id, 'move_note': move_note})
        db.session.commit()

    _mark_block_deferred_and_commit()


def _defer_start_for_block(rec, account_id: int, block_until: datetime, now: datetime,
                           move_note=None):
    """The block-shaped half of _defer_start_for_slot: every account this SCHEDULED recording
    could start on is blocked by the user. It waits and records what is left once a block
    ends - labeled partial by the late start, like any other late start - or, when the block
    outlasts its window, it ends FAILED with its own reason naming the block."""
    from .account_blocks import describe
    recording_id = rec.id
    account_name = _account_label(account_id)
    why = describe(account_name, block_until)

    if rec.stop_time <= now:
        log.warning('Recording "%s" (#%d): never started - %s for the rest of its window',
                    rec.name, recording_id, why, extra={'recording_id': recording_id})

        @retry_on_locked()
        def _fail_block_wait_and_commit():
            r = db.session.get(Recording, recording_id, populate_existing=True)
            if r is None or r.status != REC_STATUS_SCHEDULED:
                db.session.rollback()
                return False
            r.status = REC_STATUS_FAILED
            r.completed_at = now
            r.failure_reason = FAILURE_ACCOUNT_BLOCKED
            add_recording_event(
                recording_id, RECORDING_FAILED,
                detail=(f'Never started: {why}, and every account this recording could use '
                        f'stayed blocked until its window ended. Nothing was captured.'))
            db.session.commit()
            return True

        if not _fail_block_wait_and_commit():
            log.info('Recording %d: not failed for its wait - another start or a cancel '
                     'changed it first', recording_id, extra={'recording_id': recording_id})
            return
        from .health_score import dismiss_recording_failing_alerts
        dismiss_recording_failing_alerts(recording_id)
        end_capture_wait(recording_id)
        return

    _note_block_wait_once(recording_id, account_id, why, move_note)
    _note_start_deferred(recording_id, f'the block on "{account_name}" to end')
    log.info('Recording %d: deferring start - %s', recording_id, why)
    retry_at = min(block_until, rec.stop_time,
                   now + timedelta(seconds=BLOCK_WAIT_MAX_POLL_SECONDS))
    retry_at = max(retry_at, now + timedelta(seconds=SLOT_WAIT_POLL_SECONDS))
    from .scheduler import reschedule_recording_start
    reschedule_recording_start(recording_id, retry_at)


def _note_switched_off_once(recording_id: int, account_id: int, why: str, move_note=None):
    """The deferral event for a wait on a switch, written once per account. No alert, as for a
    block: the user turned every host or login on the account off, so the recording waiting
    is a state they chose, shown on the recording (dev/changelog/1670)."""
    already = [e for e in RecordingEvent.query.filter_by(
        recording_id=recording_id, event_type=RECORDING_START_DEFERRED).all()
        if (e.extra_data or '') and json.loads(e.extra_data).get('kind') == 'switched_off'
        and json.loads(e.extra_data).get('account_id') == account_id]
    if already:
        return

    @retry_on_locked()
    def _mark_switched_off_and_commit():
        add_recording_event(
            recording_id, RECORDING_START_DEFERRED,
            detail=(f'Waiting: {why}, so nothing on it can stream. It starts as soon as one '
                    f'is turned on and records what is left of its window.'
                    + (f' {move_note}' if move_note else '')),
            extra={'kind': 'switched_off', 'account_id': account_id, 'move_note': move_note})
        db.session.commit()

    _mark_switched_off_and_commit()


def _defer_start_switched_off(rec, account_id: int, why: str, now: datetime, move_note=None):
    """The switch-shaped half of _defer_start_for_slot: every host or every login on the account
    is turned off. It waits, re-asking every poll, and records what is left once one is turned
    on - or, when nothing is turned on before its window ends, it ends FAILED with its own
    reason naming the switch, never the connection limit (dev/changelog/1670)."""
    recording_id = rec.id
    if rec.stop_time <= now:
        log.warning('Recording "%s" (#%d): never started - %s for the rest of its window',
                    rec.name, recording_id, why, extra={'recording_id': recording_id})

        @retry_on_locked()
        def _fail_switched_off_and_commit():
            r = db.session.get(Recording, recording_id, populate_existing=True)
            if r is None or r.status != REC_STATUS_SCHEDULED:
                db.session.rollback()
                return False
            r.status = REC_STATUS_FAILED
            r.completed_at = now
            r.failure_reason = FAILURE_SWITCHED_OFF
            add_recording_event(
                recording_id, RECORDING_FAILED,
                detail=(f'Never started: {why}, and none was turned on before its window '
                        f'ended. Nothing was captured.'))
            db.session.commit()
            return True

        if not _fail_switched_off_and_commit():
            log.info('Recording %d: not failed for its wait - another start or a cancel '
                     'changed it first', recording_id, extra={'recording_id': recording_id})
            return
        from .health_score import dismiss_recording_failing_alerts
        dismiss_recording_failing_alerts(recording_id)
        end_capture_wait(recording_id)
        return

    _note_switched_off_once(recording_id, account_id, why, move_note)
    _note_start_deferred(recording_id, f'a host or login on "{_account_label(account_id)}" '
                                       f'to be turned on')
    log.info('Recording %d: deferring start - %s', recording_id, why)
    from .scheduler import reschedule_recording_start
    reschedule_recording_start(recording_id, now + timedelta(seconds=SLOT_WAIT_POLL_SECONDS))


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
    from .account_blocks import describe, free_at
    block_until = free_at(account_id, now)
    if block_until is not None:
        # Blocked by the user: the same wait, noted without an alert (see
        # _note_block_wait_once), and re-armed for when the block ends.
        why = describe(_account_label(account_id), block_until)
        _note_block_wait_once(recording_id, account_id, why)
        if rec.stop_time <= now:
            log.warning('Recording "%s" (#%d): %s and its window ended - not resuming; '
                        'whatever was already captured is finalized by its stop job',
                        rec.name, recording_id, why, extra={'recording_id': recording_id})
            end_capture_wait(recording_id)
            return
        log.info('Recording %d: deferring resume - %s', recording_id, why)
        from .scheduler import reschedule_recording_resume
        retry_at = min(block_until, rec.stop_time,
                       now + timedelta(seconds=BLOCK_WAIT_MAX_POLL_SECONDS))
        reschedule_recording_resume(
            recording_id, max(retry_at, now + timedelta(seconds=SLOT_WAIT_POLL_SECONDS)))
        return
    from .account_links import unusable_reason
    off = unusable_reason(account_id, _account_label(account_id))
    if off is not None:
        # Every host or login turned off: the same wait as a block's, noted without an alert;
        # whatever was already captured is the stop job's to finalize (dev/changelog/1670).
        _note_switched_off_once(recording_id, account_id, off)
        if rec.stop_time <= now:
            log.warning('Recording "%s" (#%d): %s and its window ended - not resuming; '
                        'whatever was already captured is finalized by its stop job',
                        rec.name, recording_id, off, extra={'recording_id': recording_id})
            end_capture_wait(recording_id)
            return
        log.info('Recording %d: deferring resume - %s', recording_id, off)
        from .scheduler import reschedule_recording_resume
        reschedule_recording_resume(recording_id, now + timedelta(seconds=SLOT_WAIT_POLL_SECONDS))
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
        end_capture_wait(recording_id)
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


def stop_recording(app, recording_id: int, reason: str = 'STOP_TIME_REACHED',
                   join_reason: str = None):
    """Signal the watchdog to stop and kill the current ffmpeg process. `join_reason` is
    the CAPTURE_COMPLETE text when the join starts for something other than the stop
    time."""
    with app.app_context():
        had_state = _teardown_active_ffmpeg(app, recording_id, exit_reason=reason)
        # Whatever happens next, nothing more will be retried.
        from .alerts import clear_recording_retrying_alert
        clear_recording_retrying_alert(recording_id)
        # The capture is over whichever branch below finishes it, so a resume or retry
        # still armed for it - a RETRYING recording's backoff, a resume deferred for a
        # seat - must not fire on the finished row (dev/changelog/1242). abort_recording
        # removes every per-recording job itself.
        from .scheduler import unschedule_recording_resumes
        unschedule_recording_resumes(recording_id)

        if not had_state:
            # May already be done, paused, or waiting on a dead-stream retry; concat only
            # applies while the recording is still alive in one of those senses. RETRYING has
            # no active ffmpeg either (same as PAUSED), so it reaches this branch too.
            rec = db.session.get(Recording, recording_id)
            if not (rec and rec.status in WINDOW_OPEN_STATUSES):
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

            if rec.status == REC_STATUS_RETRYING:
                from .watchdog import close_retry_wait

                @retry_on_locked()
                def _close_retry_wait_and_commit():
                    r = db.session.get(Recording, recording_id)
                    close_retry_wait(r, datetime.utcnow())
                    db.session.commit()

                _close_retry_wait_and_commit()

        if reason == 'MANUAL_STOP':
            _log_manual_stop_and_commit(recording_id)
            # do_concatenation's default names the stop time, which a Stop never reached.
            join_reason = join_reason or 'Stopped by the user; beginning the join'

        from .concatenator import do_concatenation
        threading.Thread(
            target=do_concatenation, args=(app, recording_id), daemon=True,
            kwargs={'reason': join_reason} if join_reason else {},
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
                from .watchdog import close_retry_wait
                r = db.session.get(Recording, recording_id)
                add_recording_event(recording_id, RECORDING_ABORTED, detail='Recording manually cancelled')
                now = datetime.utcnow()
                close_retry_wait(r, now)
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
            from .alerts import clear_recording_retrying_alert
            clear_recording_retrying_alert(recording_id)
            end_capture_wait(recording_id)

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
            # A resume armed for later (a wait for a seat, a launch that raised) would
            # otherwise undo the pause when it fires: PAUSED is a status it resumes.
            from .scheduler import remove_job_if_exists
            remove_job_if_exists(f'resume_{recording_id}')

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


def _close_active_segment(app, recording_id: int, exit_reason: str) -> str:
    """Close the newest open segment row with `exit_reason`, recording what ffmpeg said on
    the way out. Returns that stderr tail - the spool is consumed here, so a caller that
    goes on to fail the recording passes the cause through rather than reading it again."""
    # Read outside the retried closure: it consumes the spool (closes and unlinks it), so a
    # lock-retry of the closure would find it already gone and record an empty tail. Reading
    # first makes the values plain locals the retry can safely reuse.
    exit_code, tail, reconnects, spool_missing, jumps = collect_segment_diagnostics(recording_id)

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
                                           reconnects, spool_missing, jumps)
                db.session.commit()

    _close_and_commit()
    return tail


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
