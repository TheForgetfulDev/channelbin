"""Do a recording's picture and sound end together?

Each stream of a capture can read as continuous while the two sit minutes apart: recording 32
played with its sound 1:52 ahead of its picture, and every check it passed looked at one track
at a time (dev/changelog/1422). Where the tracks end is the cheap reading that sees it, about a
tail's worth of packets per file. It is not proof of a desync: a segment whose video stopped
before a restart also ends apart, so the sentences say what was measured.
"""
import logging
import time

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


def note_track_ends(recording_id, files, where):
    """Measure `files` [(segment number or None, path)], keep the worst on the recording, and
    say so once: one DIAGNOSTICS event per call, one alert when the recording first goes past
    the bound. `where` names the files in the event ('segments', 'joined file', 'converted
    file'). Returns the worst distance read, or None.

    Reads the files before opening a write: the probe takes seconds and SQLite has one writer.
    Runs inside a join or a conversion, so a caller guards it: a diagnostic must never be able
    to harm what it is diagnosing."""
    from . import db
    from .database import (DIAGNOSTICS, Recording, TRACK_END_GAP_OUT_OF_STEP_SECONDS,
                           add_recording_event)
    from .fmt_utils import fmt_duration

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

    if not readings:
        worst, worst_at, detail = None, None, (
            f'Could not compare where the picture and sound end in the {where}.')
    else:
        worst_at = max(readings, key=lambda r: abs(r[1] - r[2]))
        worst = round(abs(worst_at[1] - worst_at[2]), 3)
        place = (f'segment {worst_at[0]}' if worst_at[0] is not None and len(files) > 1
                 else f'the {where}')
        if worst >= TRACK_END_GAP_OUT_OF_STEP_SECONDS:
            ahead = 'video' if worst_at[1] > worst_at[2] else 'audio'
            detail = (f'The picture and sound end {fmt_duration(worst, with_seconds=True)} apart '
                      f'in {place}: the {ahead} runs on past the other track. Expect them out '
                      f'of step from wherever they separated.')
        else:
            detail = f'The picture and sound end together in the {where} ({worst:.2f} s apart).'
    extra = {'kind': 'track_ends', 'where': where,
             'ends': [{'segment': s, 'video': v, 'audio': a} for s, v, a in readings]}

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
