"""Playing and downloading a recording's own file in the browser (dev/docs/DESIGN-playback.md).

Two halves with opposite rules about the disk:

- `offer()` decides what the recording page shows - Play, Download, whether Play uses the
  browser's own player, the preview pipeline over the finished file, or the pipeline over the
  segments still being captured, and why - from the row's columns ALONE. It never stats, opens or reads the
  file: a finished recording usually sits on storage that spins down when idle, and a page
  view that woke it for nothing is a cost every visit pays (dev/changelog/1312).
- `open_for_playback()` runs only when the user clicks. It is the one place the file is
  touched, it names every way that can fail, and it measures how long the storage took to
  answer, because a sleeping disk can take ten seconds or more and a wait nobody explains
  reads as a hang.

Video is never re-encoded for playback (DESIGN-playback.md, the measured cost of a re-encode).
"""
import errno
import logging
import os
import time
from dataclasses import dataclass
from datetime import datetime
from typing import Optional

from .database import (
    REC_STATUS_ABORTED, REC_STATUS_ANALYZING, REC_STATUS_COMPLETED, REC_STATUS_CONCATENATING,
    REC_STATUS_CONVERTING, REC_STATUS_FAILED, REC_STATUS_IN_PROGRESS, REC_STATUS_PAUSED,
    REC_STATUS_RETRYING, REC_STATUS_SCHEDULED,
)
from .fs_utils import (
    PATH_DENIED, PATH_MISSING, PATH_NOT_A_DIR, PATH_OK, PATH_UNREACHABLE, probe_dir,
)

log = logging.getLogger(__name__)

MIMETYPES = {
    '.mp4': 'video/mp4',
    '.m4v': 'video/mp4',
    '.mkv': 'video/x-matroska',
    '.ts': 'video/mp2t',
}

# What every current browser decodes inside an mp4. HEVC is offered but caveated: Safari and
# Chrome/Edge on hardware with an HEVC decoder play it, Firefox and others do not.
_NATIVE_CONTAINERS = ('.mp4', '.m4v')
_NATIVE_VIDEO = ('h264',)
_MAYBE_VIDEO = ('hevc',)
_NATIVE_AUDIO = ('aac', 'mp3')

# Containers the preview pipeline repackages for a browser (dev/changelog/1314).
_PIPELINE_CONTAINERS = ('.ts', '.mkv')
_CONTAINER_NAMES = {'.ts': 'an MPEG-TS (.ts) file', '.mkv': 'a Matroska (.mkv) file'}

# Statuses with a finished file to hand over, and which file. Every status is named: a new one
# must be placed here on purpose rather than fall into a default (CLAUDE.md, states are
# enumerated).
_FINISHED = (REC_STATUS_COMPLETED,)
_KEPT_SOURCE_ONLY = (REC_STATUS_FAILED, REC_STATUS_ABORTED)
# The capture window: the segments captured so far play through the preview pipeline, and
# while IN_PROGRESS the one being written is followed (dev/changelog/1316). PAUSED and
# RETRYING play what was captured, with no live edge until the capture runs again.
_CAPTURING = (REC_STATUS_IN_PROGRESS, REC_STATUS_PAUSED, REC_STATUS_RETRYING)
# Joining, analyzing or converting: the segments are being read or replaced and the final
# file does not exist yet.
_NOTHING_YET = (REC_STATUS_SCHEDULED, REC_STATUS_CONCATENATING, REC_STATUS_ANALYZING,
                REC_STATUS_CONVERTING)


@dataclass(frozen=True)
class Offer:
    """What the recording page offers for one recording's file.

    `play` and `download` say whether the controls appear, and Play takes one of three routes:
    `native` - the browser's own <video> takes the file as it is - `pipeline` - the file
    plays through the preview pipeline (app/preview.py file sessions), repackaged as it plays
    with its video untouched - or `capture` - the recording is still in its capture window,
    and the segments captured so far play through the same pipeline, following the one being
    written. At most one. When `native` is False, `reason` says in the user's
    terms why the browser's own player cannot take the file. `caveat` is set when a browser
    may still refuse it (HEVC video, or audio a browser lacks on the native route).
    """
    play: bool = False
    download: bool = False
    native: bool = False
    pipeline: bool = False
    capture: bool = False
    reason: Optional[str] = None
    caveat: Optional[str] = None
    video_codec: Optional[str] = None
    audio_codec: Optional[str] = None


_ELSEWHERE = ' Download it and play it in VLC or another media player.'
_HEVC_CAVEAT = ('The video is HEVC (H.265). Safari plays it, and Chrome or Edge do on hardware '
                'with an HEVC decoder; Firefox does not.')


def _ext(path: str) -> str:
    return os.path.splitext(path or '')[1].lower()


def _not_native(path: str, vcodec, acodec) -> Offer:
    """A file the browser's own player cannot take: through the pipeline when it can carry
    the video as it is, otherwise Download alone with the reason."""
    ext = _ext(path)
    if vcodec and vcodec not in _NATIVE_VIDEO + _MAYBE_VIDEO:
        return Offer(play=True, download=True, video_codec=vcodec, audio_codec=acodec,
                     reason=f'The video in this file is {vcodec}, which browsers do not play.'
                            + _ELSEWHERE)
    if ext in _PIPELINE_CONTAINERS:
        what = _CONTAINER_NAMES[ext]
        return Offer(play=True, download=True, pipeline=True, video_codec=vcodec,
                     audio_codec=acodec,
                     caveat=_HEVC_CAVEAT if vcodec in _MAYBE_VIDEO else None,
                     reason=f'This recording was kept as {what}, which a browser cannot play '
                            'by itself.')
    what = f'a {ext or "extensionless"} file'
    return Offer(play=True, download=True, video_codec=vcodec, audio_codec=acodec,
                 reason=f'This recording was kept as {what}, which a browser cannot play by '
                        'itself.' + _ELSEWHERE)


def offer(rec, kept_source_on_disk: bool = False,
          capture_video_codec: Optional[str] = None) -> Offer:
    """What to offer for `rec`, from its columns alone - never a filesystem call.

    `kept_source_on_disk` is the caller's own answer to "is a FAILED or ABORTED row's joined
    .ts still there" (the recording page already works it out for Retry conversion); a
    finished row is never checked here, by design - the click finds out.
    `capture_video_codec` is what the capture's own segment probe measured, for a recording
    still in its capture window (capture_codecs()); the recorded_* columns are written only
    once the file is finished.
    """
    status = rec.status
    vcodec = (rec.recorded_video_codec or '').lower() or None
    acodec = (rec.recorded_audio_codec or '').lower() or None
    if status in _NOTHING_YET:
        return Offer()
    if status in _CAPTURING:
        cv = (capture_video_codec or '').lower() or None
        if cv and cv not in _NATIVE_VIDEO + _MAYBE_VIDEO:
            return Offer(play=True, video_codec=cv,
                         reason=f'The video being captured is {cv}, which browsers do not '
                                'play. Once the recording has finished, download it and play '
                                'it in VLC or another media player.')
        return Offer(play=True, capture=True, video_codec=cv,
                     caveat=_HEVC_CAVEAT if cv in _MAYBE_VIDEO else None)
    if status in _KEPT_SOURCE_ONLY:
        if not (rec.output_path and kept_source_on_disk):
            return Offer()
        return _not_native(rec.output_path, vcodec, acodec)
    if status not in _FINISHED:
        log.warning('Recording %s: no playback branch for status %r', rec.id, status)
        return Offer()
    if not rec.output_path:
        return Offer()
    if _ext(rec.output_path) not in _NATIVE_CONTAINERS:
        return _not_native(rec.output_path, vcodec, acodec)
    if vcodec and vcodec not in _NATIVE_VIDEO + _MAYBE_VIDEO:
        return _not_native(rec.output_path, vcodec, acodec)
    caveat = None
    if vcodec in _MAYBE_VIDEO:
        caveat = _HEVC_CAVEAT
    elif acodec and acodec not in _NATIVE_AUDIO:
        caveat = (f'The audio is {acodec}, which most browsers cannot decode, so the picture '
                  'may play without sound.')
    return Offer(play=True, download=True, native=True, caveat=caveat,
                 video_codec=vcodec, audio_codec=acodec)


def allowed_roots(cfg: dict) -> list:
    """The folders a recording's file may be served from: the recording folder and the
    move-on-complete destination.

    The destination counts even while moving is switched off - files moved there before it
    was switched off are still this install's recordings. Not configured_write_dirs(), which
    lists the directories of features that are ON and adds image, backup and log folders
    that no recording's output_path should ever point into.
    """
    rec_cfg = cfg.get('recording', {}) or {}
    roots = [rec_cfg.get('dvr_output_dir')]
    roots.append(((rec_cfg.get('move_on_complete') or {}).get('destination') or '').strip())
    return [r for r in roots if r]


class PlaybackRefused(Exception):
    """The file cannot be handed over. `message` is the sentence the page shows."""

    def __init__(self, message: str, status: int):
        super().__init__(message)
        self.message = message
        self.status = status


def _within(path: str, roots: list) -> bool:
    real = os.path.realpath(path)
    for root in roots:
        real_root = os.path.realpath(root)
        try:
            if os.path.commonpath([real, real_root]) == real_root:
                return True
        except ValueError:
            continue   # different drives (Windows) - not inside this root
    return False


def resolve_file(rec, cfg: dict, kept_source_on_disk: bool = False) -> str:
    """The path of `rec`'s file once it has been checked as servable, or PlaybackRefused.

    Touches the filesystem - only on a request the user made. Never takes a path from the
    request: the row's output_path is the only source.
    """
    o = offer(rec, kept_source_on_disk=kept_source_on_disk)
    if not o.download:
        raise PlaybackRefused('This recording has no finished file to play or download.', 409)
    path = rec.output_path
    if not _within(path, allowed_roots(cfg)):
        log.warning('Recording %d: refused to serve %s - outside the recording folder and the '
                    'move destination', rec.id, path)
        raise PlaybackRefused(
            f'The file is at {path}, which is outside the recording folder and the move '
            'destination ChannelBin is set up with, so it is not served.', 403)
    return path


def _error_text(err_no, strerror) -> str:
    """The OS's own words for an error, with its symbolic name: "Stale file handle (ESTALE)"."""
    name = errno.errorcode.get(err_no) if err_no is not None else None
    return f'{strerror} ({name})' if name else str(strerror)


def storage_refusal(path: str, exc: OSError) -> PlaybackRefused:
    """The open of `path` failed with an error other than "no such file".

    The message reports the error and does not guess at its cause: a stale handle, a dropped
    share and a failing disk each have more than one explanation, and only the user knows
    their setup (dev/changelog/1313).
    """
    return PlaybackRefused(
        f'Could not open {path}: {_error_text(exc.errno, exc.strerror or str(exc))}.', 503)


def missing_file_refusal(recording_id: int, path: str, exc: OSError) -> PlaybackRefused:
    """The open of `path` said "no such file"; report that and what its folder answered.

    "No such file" alone is not the whole story: a file under a CIFS mount whose handle has
    gone stale opens as ENOENT while its folder answers ESTALE (measured on this app's own
    share, dev/changelog/1312). So the folder is asked too, and both answers are shown as the
    OS gave them. Neither is interpreted - a file someone deleted after watching it and a
    share that dropped are both possible, and the errors are what tell them apart.
    """
    opened = f'Could not open {path}: {_error_text(exc.errno, exc.strerror or str(exc))}.'
    folder = os.path.dirname(path)
    probe = probe_dir(folder)
    if probe.outcome == PATH_OK:
        log.warning('Recording %d: file not found for playback at %s (folder %s is readable)',
                    recording_id, path, folder)
        return PlaybackRefused(f'{opened} Its folder {folder} is readable, and the file is not '
                               'in it.', 404)
    if probe.outcome in (PATH_MISSING, PATH_NOT_A_DIR, PATH_DENIED, PATH_UNREACHABLE):
        folder_said = _error_text(probe.errno, probe.strerror)
        log.warning('Recording %d: file not found for playback at %s; folder %s: %s',
                    recording_id, path, folder, folder_said)
        return PlaybackRefused(f'{opened} Its folder {folder} could not be read either: '
                               f'{folder_said}.',
                               404 if probe.outcome == PATH_MISSING else 503)
    log.warning('Recording %d: no branch for folder outcome %r', recording_id, probe.outcome)
    return PlaybackRefused(opened, 404)


def open_for_playback(rec, cfg: dict, kept_source_on_disk: bool = False) -> dict:
    """Wake the storage and prove the file is readable, on the user's click.

    Reads one byte rather than stat()ing: a network mount can answer a stat from its attribute
    cache while the disk behind it is still asleep, and then the player's first request is the
    one that waits. Returns {path, size, seconds}; every failure is a PlaybackRefused that names
    what happened.
    """
    path = resolve_file(rec, cfg, kept_source_on_disk)
    started = time.monotonic()
    try:
        with open(path, 'rb') as f:
            f.read(1)
            size = os.fstat(f.fileno()).st_size
    except FileNotFoundError as exc:
        raise missing_file_refusal(rec.id, path, exc)
    except PermissionError as exc:
        log.warning('Recording %d: no permission to read %s for playback', rec.id, path)
        raise PlaybackRefused(
            f'Could not open {path}: {_error_text(exc.errno, exc.strerror or str(exc))}.', 403)
    except OSError as exc:
        log.warning('Recording %d: storage error opening %s for playback: %s', rec.id, path, exc)
        raise storage_refusal(path, exc)
    seconds = time.monotonic() - started
    if seconds >= 2:
        log.info('Recording %d: storage took %.1fs to open %s for playback', rec.id, seconds, path)
    return {'path': path, 'size': size, 'seconds': seconds}


def mimetype_for(path: str) -> str:
    return MIMETYPES.get(_ext(path), 'application/octet-stream')


# --- A recording still being captured (dev/changelog/1316) ---------------------------------
#
# A capture is a run of segment files with gaps between them, and the join runs them back to
# back. The player's timeline does the same, so a position while capturing is the same spot,
# give or take a few seconds per restart, in the file the join will write. Each segment's
# length is its wall-clock span: content_duration_seconds is only written by the join, and a
# span that ended in a stall counts the stalled seconds the file never got.

#: Following a growing segment starts this many seconds before what is written so far.
FOLLOW_FROM_END_SECONDS = 4.0
#: A position this close to the live edge, or past it, is played as live.
LIVE_SNAP_SECONDS = 10.0


@dataclass(frozen=True)
class CaptureSpan:
    segment_number: int
    file_path: str
    base: float
    length: float
    growing: bool

    @property
    def end(self) -> float:
        return self.base + self.length


def capture_timeline(segments, now: datetime, growing_segment: Optional[int]) -> list:
    """The captured segments laid end to end, as CaptureSpans. `segments` are the joinable
    ones in capture order (concatenator.joinable_segments()); `growing_segment` is the number
    of the segment the recorder is writing right now, or None when nothing is capturing.
    Pure, so the timeline can be tested without a recorder or a file."""
    spans = []
    base = 0.0
    for seg in segments:
        growing = growing_segment is not None and seg.segment_number == growing_segment
        end = now if growing else (seg.ended_at or now)
        length = max(0.0, (end - seg.started_at).total_seconds())
        spans.append(CaptureSpan(seg.segment_number, seg.file_path, base, length, growing))
        base += length
    return spans


def resolve_capture_position(spans: list, position: float):
    """Which segment file holds `position`, and where in it: `(span, offset, live)`, or None
    when the position is past everything captured and nothing is being written.

    `live` means play from the live edge: the position is within LIVE_SNAP_SECONDS of what
    the growing segment holds so far, or past it; the caller then starts
    FOLLOW_FROM_END_SECONDS before that edge. A position in a finished segment plays from
    its offset; one past a finished segment's end lands at the top of the next."""
    if not spans:
        return None
    for span in spans:
        if position < span.end:
            if span.growing and span.end - position <= LIVE_SNAP_SECONDS:
                return span, 0.0, True
            return span, max(0.0, position - span.base), False
    last = spans[-1]
    if last.growing:
        return last, 0.0, True
    return None


def capture_codecs(segments) -> tuple:
    """(video, audio) as the newest probed segment measured them, or (None, None)."""
    for seg in sorted(segments, key=lambda s: s.segment_number, reverse=True):
        if seg.probe_video_codec or seg.probe_audio_codec:
            return seg.probe_video_codec, seg.probe_audio_codec
    return None, None


def resolve_segment_file(rec, path: str, cfg: dict) -> str:
    """A capture segment's path once it has been checked as servable, or PlaybackRefused.
    The path comes from the segment row, never from the request."""
    if not _within(path, allowed_roots(cfg)):
        log.warning('Recording %d: refused to play segment %s - outside the recording folder '
                    'and the move destination', rec.id, path)
        raise PlaybackRefused(
            f'The segment file is at {path}, which is outside the recording folder and the '
            'move destination ChannelBin is set up with, so it is not played.', 403)
    return path
