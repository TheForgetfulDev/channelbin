"""Do a recording's picture and sound end together?

Each stream of a capture can read as continuous while the two sit minutes apart: recording 32
played with its sound 1:52 ahead of its picture, and every check it passed looked at one track
at a time (dev/changelog/1422). Where the tracks end is the cheap reading that sees it, about a
tail's worth of packets per file. It is not proof of a desync, so the sentences say what was
measured. One case is told apart, because the app already knows it: a capture whose picture
stopped under its sound, which the watchdog restarted about 35 s in or left running for the
sound. That file's sound ends past its picture by construction, and nothing in it is out of
step (dev/changelog/1599).
"""
import logging
import time

from sqlalchemy.exc import SQLAlchemyError

from .db_utils import retry_on_locked

log = logging.getLogger(__name__)

# What the readings may cost the join they run ahead of: a tail read is under a second on a
# healthy disk, and one that hangs must not hold the recording up.
READ_TIMEOUT_SECONDS = 30
TOTAL_READ_BUDGET_SECONDS = 120


def read_track_ends(path):
    """(video end, audio end) in seconds, or None when either cannot be read."""
    from .probe import tail_track_ends
    ends = tail_track_ends(path, timeout=READ_TIMEOUT_SECONDS)
    video, audio = ends.get('video'), ends.get('audio')
    return None if video is None or audio is None else (video, audio)


def sound_only_endings(recording_id):
    """(segment numbers that ended with sound and no picture, whether the recording's last
    segment is one of them).

    Read from what the watchdog wrote, never from the file: a segment it closed as
    SEGMENT_EXIT_VIDEO_STOPPED, or one whose last video event is CAPTURE_VIDEO_STOPPED, the
    capture left running for its sound. A picture that came back on the same segment wrote
    CAPTURE_VIDEO_RESUMED there after it; one that came back on a restart wrote it on the
    next segment."""
    from .database import (CAPTURE_VIDEO_RESUMED, CAPTURE_VIDEO_STOPPED, RecordingEvent,
                           RecordingSegment, SEGMENT_EXIT_VIDEO_STOPPED)
    # A discarded segment is not in the joined file, so it is not where that file ends.
    rows = (RecordingSegment.query
            .filter(RecordingSegment.recording_id == recording_id,
                    RecordingSegment.excluded_reason.is_(None))
            .with_entities(RecordingSegment.segment_number, RecordingSegment.exit_reason).all())
    sound_only = {n for n, reason in rows if reason == SEGMENT_EXIT_VIDEO_STOPPED}
    last_event = {}
    events = (RecordingEvent.query
              .filter(RecordingEvent.recording_id == recording_id,
                      RecordingEvent.event_type.in_([CAPTURE_VIDEO_STOPPED,
                                                     CAPTURE_VIDEO_RESUMED]))
              .order_by(RecordingEvent.id)
              .with_entities(RecordingEvent.segment_number, RecordingEvent.event_type).all())
    for number, event_type in events:
        last_event[number] = event_type
    sound_only |= {n for n, event_type in last_event.items()
                   if n is not None and event_type == CAPTURE_VIDEO_STOPPED}
    last = max((n for n, _reason in rows), default=None)
    return sound_only, last is not None and last in sound_only


def note_track_ends(recording_id, files, where):
    """Measure `files` [(segment number or None, path)], keep the worst on the recording, and
    say so once: one DIAGNOSTICS event per call, one alert when the recording first goes past
    the bound. `where` names the files in the event ('segments', 'joined file', 'converted
    file'). Returns the worst distance read, or None.

    A file that ends with sound and no picture for a reason already on the recording
    (sound_only_endings) keeps its reading in the event and is named for what it is, and is
    left out of the worst: it raises no alert, keeps no source and costs the channel nothing.
    Only sound past picture is explained that way; a picture past its sound never is.

    Reads the files before opening a write: the probe takes seconds and SQLite has one writer.
    Runs inside a join or a conversion, so a caller guards it: a diagnostic must never be able
    to harm what it is diagnosing."""
    from . import db
    from .database import (DIAGNOSTICS, Recording, TRACK_END_GAP_OUT_OF_STEP_SECONDS,
                           add_recording_event)
    from .fmt_utils import fmt_duration, join_labels

    try:
        sound_only, last_is_sound_only = sound_only_endings(recording_id)
    except SQLAlchemyError as exc:
        # Unexplained is the careful reading: the alert and the kept source stand.
        log.warning('Recording %d: could not read which segments ended on sound alone: %s',
                    recording_id, exc)
        db.session.rollback()
        sound_only, last_is_sound_only = set(), False

    def _explained(reading):
        segment_number, video, audio = reading
        known = last_is_sound_only if segment_number is None else segment_number in sound_only
        return known and audio > video

    readings = []
    deadline = time.monotonic() + TOTAL_READ_BUDGET_SECONDS
    for segment_number, path in files:
        if time.monotonic() > deadline:
            log.warning('Recording %d: stopped comparing track ends after %d s; %d of %d '
                        'segments read', recording_id, TOTAL_READ_BUDGET_SECONDS, len(readings),
                        len(files))
            break
        ends = read_track_ends(path)
        if ends is not None:
            readings.append((segment_number, round(ends[0], 3), round(ends[1], 3)))

    def _place(reading):
        return (f'segment {reading[0]}' if reading[0] is not None and len(files) > 1
                else f'the {where}')

    judged = [r for r in readings if not _explained(r)]
    explained = [r for r in readings if _explained(r)
                 and r[2] - r[1] >= TRACK_END_GAP_OUT_OF_STEP_SECONDS]
    worst = None
    if not readings:
        detail = f'Could not compare where the picture and sound end in the {where}.'
    elif not judged:
        detail = ''
    else:
        worst_at = max(judged, key=lambda r: abs(r[1] - r[2]))
        worst = round(abs(worst_at[1] - worst_at[2]), 3)
        if worst >= TRACK_END_GAP_OUT_OF_STEP_SECONDS:
            ahead = 'video' if worst_at[1] > worst_at[2] else 'audio'
            detail = (f'The picture and sound end {fmt_duration(worst, with_seconds=True)} apart '
                      f'in {_place(worst_at)}: the {ahead} runs on past the other track. Expect '
                      f'them out of step from wherever they separated.')
        elif explained:
            detail = f'Everywhere else the picture and sound end together ({worst:.2f} s apart).'
        else:
            detail = f'The picture and sound end together in the {where} ({worst:.2f} s apart).'
    if explained:
        said = join_labels([f'{_place(r)} ends with '
                            f'{fmt_duration(r[2] - r[1], with_seconds=True)} of sound and no '
                            f'picture' for r in explained])
        detail = (f'{said[0].upper()}{said[1:]}: the picture had stopped arriving, as the Video '
                  f'stopped event there says. That is a stretch with no picture, not the two '
                  f'out of step. {detail}').strip()
    elif not detail:
        detail = f'The picture and sound end together in the {where}.'
    extra = {'kind': 'track_ends', 'where': where,
             'ends': [dict({'segment': r[0], 'video': r[1], 'audio': r[2]},
                           **({'sound_only': True} if _explained(r) else {}))
                      for r in readings]}

    @retry_on_locked()
    def _record_track_ends_and_commit():
        rec = db.session.get(Recording, recording_id)
        was_apart = rec.picture_sound_apart_seconds is not None
        if worst is not None:
            rec.track_end_gap_seconds = max(rec.track_end_gap_seconds or 0.0, worst)
        add_recording_event(recording_id, DIAGNOSTICS, detail=detail, extra=extra)
        now_apart = rec.picture_sound_apart_seconds is not None
        name = rec.name
        db.session.commit()
        return now_apart and not was_apart, name

    newly_apart, name = _record_track_ends_and_commit()
    if worst is not None and worst >= TRACK_END_GAP_OUT_OF_STEP_SECONDS:
        log.warning('Recording %d: %s', recording_id, detail)
    if newly_apart:
        from . import alerts
        alerts.create_alert(
            'RECORDING_OUT_OF_STEP',
            f'Picture and sound out of step: {name}',
            body=(f'{detail} The source .ts is kept rather than deleted.'),
            source='recording',
            recording_id=recording_id,
        )
    return worst
