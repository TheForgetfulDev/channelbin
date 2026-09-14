"""Concatenate all segment files into a single final .ts output."""
import contextlib
import logging
import os
import shutil
import tempfile
import threading
import time
from datetime import datetime

from .fmt_utils import fmt_bytes as _fmt_bytes
from .proc_utils import supervise_ffmpeg

log = logging.getLogger(__name__)

# Global (not per-account) - gates the concat+conversion step when
# recording.serialize_concat is enabled. Concatenation is local CPU/disk work,
# unrelated to the per-account provider connection limits in connection_limits.py.
_concat_lock = threading.Lock()

# How often the supervised join is polled, and how often ffmpeg writes -progress.
# Not a config key: it is a poll cadence, not a budget, and the two budgets that do
# bound the join (ffmpeg.concat_pre_output_timeout_seconds / concat_stall_seconds)
# are both minutes-scale, so nothing about this number is worth tuning.
CONCAT_POLL_INTERVAL_SECONDS = 5

# ── Live concat registry ──────────────────────────────────────────────────────
# recording_id -> time.monotonic() when its chain claimed it. Mirrors
# postprocessor._active_conversions, and exists because CONCATENATING is not evidence
# that a thread is dead: a row sits there for as long as serialize_concat makes it wait,
# and the same status is what a crash leaves behind. Without this, "Retry concat" cannot
# tell queued from stranded and starts a second do_concatenation - two ffmpegs writing one
# output_path, both deleting the same segments (dev/changelog/668).
#
# The claim spans the whole chain, post-processing tail included, because that is the
# window in which a second concat would do damage: the flag means "a do_concatenation
# chain is working this recording", not "ffmpeg is running right now".
_active_concats: dict = {}
_active_concats_lock = threading.Lock()


def is_concat_active(recording_id: int) -> bool:
    with _active_concats_lock:
        return recording_id in _active_concats


def _claim_concat(recording_id: int):
    """Test-and-set under one lock. Returns None if the claim succeeded, otherwise the
    seconds the chain that already holds it has been running (for the refusal message).

    Atomic because the callers race: a stop job and a manual Stop can both reach
    stop_recording() while the row still reads IN_PROGRESS, and a double-clicked Retry
    beats its own status write. A check-then-set would let both through.
    """
    with _active_concats_lock:
        started = _active_concats.get(recording_id)
        if started is not None:
            return max(0.0, time.monotonic() - started)
        _active_concats[recording_id] = time.monotonic()
        return None


def _release_concat(recording_id: int):
    with _active_concats_lock:
        _active_concats.pop(recording_id, None)


def _wait_for_no_active_recording(recording_id: int, poll_seconds: float = 5, log_every: int = 12):
    """Block (polling) until no Recording is IN_PROGRESS. Only called while holding
    _concat_lock, so this also naturally serializes against other queued concats.
    Segments are already safely on disk, so waiting here is harmless."""
    from .database import Recording, REC_STATUS_IN_PROGRESS
    i = 0
    while True:
        active = Recording.query.filter(Recording.status == REC_STATUS_IN_PROGRESS).count()
        if active == 0:
            return
        if i % log_every == 0:
            log.info('Concat for recording %d waiting on %d active recording(s) to finish '
                      '(recording.serialize_concat enabled)', recording_id, active)
        time.sleep(poll_seconds)
        i += 1


def committed_concat_output(rec):
    """The finished concat's .ts path if this recording already has one, else None.

    Recording.output_path is written in exactly three places - concat success, conversion
    success and the move step - so a non-NULL value is a *recorded* fact that the concat
    already committed, set in the same closure as its final_file_size and its
    CONCATENATION_DONE event. Status is not that fact and must never be read as one. A
    resume that inferred "not concatenated yet" from the status re-ran the concat, found no
    segments and failed a complete 17.9 GB recording (dev/docs/BUGS.md 2026-08-24) - back
    when CONCATENATING covered the post-processing tail as well. ANALYZING now names that
    tail (dev/changelog/867), which makes this check belt-and-braces rather than the only
    signal, but it stays the primary one: a row can still be CONCATENATING with a committed
    output for the instant between the concat's commit and do_postprocess's own.

    Existence on disk is part of the answer: a Retry after the output was deleted has a
    stale path on the row and genuinely does need a fresh concat.
    """
    path = rec.output_path
    if path and path.endswith('.ts') and os.path.exists(path):
        return path
    return None


def do_concatenation(app, recording_id: int, *, reason: str = 'Stop time reached; beginning concatenation'):
    """Concatenate one recording's segments, then post-process, at most once at a time.

    The double-run guard lives here rather than only in the callers because every launch
    site is a plausible duplicate: the Retry route, the startup CONCATENATING sweep, and
    stop_recording() when a scheduled stop job races a manual one.
    """
    running_for = _claim_concat(recording_id)
    if running_for is not None:
        log.warning('Concatenation for recording %d is already in progress (%.0fs) - refusing '
                    'to start a second one', recording_id, running_for,
                    extra={'recording_id': recording_id})
        return
    try:
        _run_concatenation(app, recording_id, reason=reason)
    finally:
        _release_concat(recording_id)


def run_postprocess_claimed(app, recording_id: int, ts_path: str):
    """do_postprocess() under the live-chain claim, for callers that reach post-processing
    without going through do_concatenation - today only the Retry conversion route.

    The claim is the concat registry's, not a second one, because that registry's answer is
    already "a chain is working this recording" and it already spans the post-processing
    tail. Without this a retry-launched run registers nowhere: is_conversion_active() is
    False for the whole analysis phase (no ffmpeg has been spawned yet), so a second Retry
    in that window starts a duplicate chain that re-probes the same file and races the first
    one to spawn a conversion.
    """
    running_for = _claim_concat(recording_id)
    if running_for is not None:
        log.warning('Post-processing for recording %d is already in progress (%.0fs) - '
                    'refusing to start a second one', recording_id, running_for,
                    extra={'recording_id': recording_id})
        return
    try:
        from .postprocessor import do_postprocess
        do_postprocess(app, recording_id, ts_path)
    finally:
        _release_concat(recording_id)


def _measure_segment_content_durations(recording_id: int, valid_segments):
    """ffprobe each segment's finished file and store its content length on the row.

    Runs here rather than in the watchdog because the watchdog probes a file that is still
    growing: its duration would be whatever had arrived by then, not what the segment ended
    up holding. Here every file is closed and final, and this is post-capture work, so it
    cannot delay a restart or otherwise touch the capture it is measuring (CLAUDE.md
    Product Principles - a diagnostic must never harm the capture it is diagnosing).

    Header-only (count_packets=False), which on MPEG-TS seeks rather than scans - measured
    at 0.04s per file on this machine, flat from 10 MB to 317 MB, so 28 segments cost about
    a second against a concat that runs for minutes.

    Never raises and never fails the concat: a segment it could not read keeps its NULL,
    which means "not measured" and is honest. Probing happens first, in full; the database
    write is one decorated closure with one commit, so no ffprobe re-runs on a retry.
    """
    from . import db
    from .database import RecordingSegment
    from .db_utils import retry_on_locked
    from .probe import parse_ffprobe

    measured = []
    for seg in valid_segments:
        try:
            info = parse_ffprobe(seg.file_path, count_packets=False, timeout=30)
        except Exception as exc:
            log.warning('Recording %d seg %s: content-duration probe failed: %s',
                        recording_id, seg.segment_number, exc)
            continue
        duration = info.get('duration')
        if duration is not None and duration > 0:
            measured.append((seg.id, float(duration)))

    if not measured:
        return

    @retry_on_locked()
    def _store_content_durations_and_commit():
        for seg_id, duration in measured:
            row = db.session.get(RecordingSegment, seg_id)
            if row is not None:
                row.content_duration_seconds = duration
        db.session.commit()

    _store_content_durations_and_commit()
    log.info('Recording %d: measured content duration on %d of %d segment(s)',
             recording_id, len(measured), len(valid_segments))


def _run_concatenation(app, recording_id: int, *, reason: str):
    from . import db
    from .config import load_config
    from .database import (
        Recording, RecordingSegment, add_recording_event, preserve_cancelled_status,
        CAPTURE_COMPLETE, CONCATENATION_STARTED, CONCATENATION_DONE,
        REC_STATUS_ANALYZING, REC_STATUS_CONCATENATING, REC_STATUS_FAILED,
    )
    from . import events as ev
    from .recorder import _safe_name
    from .postprocessor import output_extension_family, reserve_concat_output_path
    from .db_utils import retry_on_locked

    with app.app_context():
        cfg = load_config()
        dvr_dir = cfg['recording']['dvr_output_dir']
        concat_pre_output_timeout = cfg['ffmpeg']['concat_pre_output_timeout_seconds']
        concat_stall_seconds = cfg['ffmpeg']['concat_stall_seconds']

        rec = db.session.get(Recording, recording_id)
        if rec is None:
            log.error('Concatenation: recording %d not found', recording_id)
            return

        # A concat that already committed its output must never be re-run: it deleted the
        # segments it consumed, so a second pass finds nothing and fails a recording that
        # is sitting complete on disk. Pick the chain back up where it actually stopped -
        # in post-processing, which is re-runnable from the top by design.
        finished_output = committed_concat_output(rec)
        if finished_output:
            log.info('Recording %d already has a concatenated output (%s) - resuming '
                     'post-processing instead of re-concatenating', recording_id, finished_output)

            # ANALYZING, not CONCATENATING: this branch exists precisely because the concat
            # is already done, so putting the row back on the concat status here is the
            # sentence that made the activity log unreadable - "concat complete" followed by
            # "resuming concatenation" for a file that never needed re-joining
            # (dev/changelog/867). do_postprocess() sets it again at its own entry; setting
            # it here too is what makes the SSE publish below tell the truth.
            @retry_on_locked()
            def _mark_resuming_postprocess_and_commit():
                r = db.session.get(Recording, recording_id)
                add_recording_event(
                    recording_id, CAPTURE_COMPLETE,
                    detail=f'Concatenation was already complete ({os.path.basename(finished_output)}) - '
                           f'resuming post-processing')
                r.status = REC_STATUS_ANALYZING
                db.session.commit()

            _mark_resuming_postprocess_and_commit()
            ev.publish(recording_id, CAPTURE_COMPLETE, {'status': REC_STATUS_ANALYZING})

            from .postprocessor import do_postprocess
            do_postprocess(app, recording_id, finished_output)
            return

        log.info('Starting concatenation for recording %d (%s)', recording_id, rec.name)

        @retry_on_locked()
        def _mark_concatenating_and_commit():
            add_recording_event(recording_id, CAPTURE_COMPLETE, detail=reason)
            rec.status = REC_STATUS_CONCATENATING
            db.session.commit()

        _mark_concatenating_and_commit()

        ev.publish(recording_id, CAPTURE_COMPLETE, {'status': REC_STATUS_CONCATENATING})

        # Final-frame screenshot for the finished recording, taken now while the
        # segment files still exist (a successful concat deletes them). Subprocess
        # side effect - deliberately outside any retry_on_locked closure.
        from .recorder import persist_final_thumbnail
        persist_final_thumbnail(recording_id)

        # serialize_concat: only one concat+conversion job runs at a time, and none
        # run while any recording is IN_PROGRESS. Marking CONCATENATING above happens
        # unconditionally so the UI reflects the true state immediately even while
        # queued here. do_postprocess() (the mp4-conversion step) runs synchronously
        # inside this same guarded section further below, so both "concat" and
        # "convert" are covered by one lock.
        serialize = cfg['recording'].get('serialize_concat', False)
        lock_ctx = _concat_lock if serialize else contextlib.nullcontext()
        with lock_ctx:
            if serialize:
                _wait_for_no_active_recording(recording_id)

            # Collect segment files in order
            segments = RecordingSegment.query.filter_by(recording_id=recording_id)\
                .order_by(RecordingSegment.segment_number).all()

            valid_segments = [
                s for s in segments
                if s.file_path and os.path.exists(s.file_path) and os.path.getsize(s.file_path) > 0
            ]

            if not valid_segments:
                # "No files on disk" and "the feed never delivered" are two different
                # failures that reach this branch, and only the second one is channel
                # signal. bytes_recorded is what separates them: the watchdog wrote it
                # from the capture itself, so a positive total means ffmpeg really did
                # pull those bytes down and the files went missing afterwards, locally.
                # Blaming the channel for that is what apply_recording_health_observation's
                # own contract forbids, and it cost a healthy feed 100 -> 22 on a capture
                # that had succeeded (dev/docs/BUGS.md 2026-08-24).
                captured_bytes = sum(s.bytes_recorded or 0 for s in segments)
                stream_delivered = captured_bytes > 0
                if stream_delivered:
                    why = (f'FAILED: no segment files on disk, but the capture recorded '
                           f'{_fmt_bytes(captured_bytes)} across {len(segments)} segment(s) - '
                           f'the files went missing after capture, so this is not a stream fault')
                else:
                    why = 'FAILED: no valid segments found - the capture never recorded any data'
                log.error('Recording "%s" (#%d): no valid segments to concatenate (%s)',
                          rec.name, recording_id,
                          'files missing after capture' if stream_delivered else 'nothing was captured',
                          extra={'recording_id': recording_id, 'already_alerted': True})

                @retry_on_locked()
                def _mark_no_segments_failed_and_commit():
                    r = db.session.get(Recording, recording_id)
                    if preserve_cancelled_status(
                            r, 'Concatenation found no valid segments, but the recording had '
                               'already been cancelled - status left ABORTED.'):
                        db.session.commit()
                        return False
                    r.status = REC_STATUS_FAILED
                    r.completed_at = datetime.utcnow()
                    add_recording_event(recording_id, CONCATENATION_DONE, detail=why)
                    db.session.commit()
                    return True

                if _mark_no_segments_failed_and_commit():
                    if not stream_delivered:
                        from .health_score import apply_recording_health_observation
                        apply_recording_health_observation(app, recording_id, 'failed')
                    ev.publish(recording_id, CONCATENATION_DONE, {'success': False, 'error': 'no valid segments'})
                    # Typed rather than left to the log->alert catch-all, which gave this
                    # an "Application Error (log)" label and no deep link to the recording
                    # it is about (dev/changelog/930). Nothing clears it: there is no file
                    # to recover and nothing re-runs this concatenation.
                    from .alerts import create_alert
                    create_alert(
                        'CONCATENATION_FAILED',
                        f'Concatenation failed: {rec.name}',
                        body=why,
                        source='concatenator',
                        recording_id=recording_id,
                    )
                return

            # The capture is over and left usable segments: score it now, once, so a later
            # local failure (no disk space, concat error, conversion give-up) cannot discard
            # the stalls and restarts that actually happened on the feed. Idempotent, so the
            # postprocessor's own call and any Retry re-entry are no-ops. See changelog 279.
            from .health_score import apply_capture_phase_health_observation
            apply_capture_phase_health_observation(app, recording_id)

            # Before concat, while each segment is still its own closed file - afterwards
            # the joined .ts reports one duration and the per-segment answer is gone.
            _measure_segment_content_durations(recording_id, valid_segments)

            total_seg_bytes = sum(os.path.getsize(s.file_path) for s in valid_segments)
            free_bytes = shutil.disk_usage(dvr_dir).free
            if free_bytes < total_seg_bytes:
                log.error('Recording "%s" (#%d): insufficient disk space for concat - need %s, have %s',
                          rec.name, recording_id, _fmt_bytes(total_seg_bytes), _fmt_bytes(free_bytes),
                          extra={'recording_id': recording_id})

                @retry_on_locked()
                def _mark_disk_space_failed_and_commit():
                    r = db.session.get(Recording, recording_id)
                    if preserve_cancelled_status(
                            r, 'Concatenation ran out of disk space, but the recording had '
                               'already been cancelled - status left ABORTED.'):
                        db.session.commit()
                        return False
                    r.status = REC_STATUS_FAILED
                    r.completed_at = datetime.utcnow()
                    add_recording_event(recording_id, CONCATENATION_DONE,
                                        detail=f'FAILED: not enough disk space - need {_fmt_bytes(total_seg_bytes)}, '
                                               f'only {_fmt_bytes(free_bytes)} free. Segments preserved. Free space and retry.')
                    db.session.commit()
                    return True

                if _mark_disk_space_failed_and_commit():
                    ev.publish(recording_id, CONCATENATION_DONE, {
                        'success': False, 'error': 'insufficient disk space', 'status': REC_STATUS_FAILED,
                    })
                return

            # Chosen once, above the single-vs-multi split, so the collision check and its
            # reservation happen exactly once per concat. Recording.name is not unique, so
            # the plain `{safe_name}.ts` can already be occupied by an earlier same-named
            # recording - directly, or via the converted sibling it left behind
            # (dev/changelog/643).
            safe_name = _safe_name(rec.name)
            output_path = reserve_concat_output_path(
                dvr_dir, safe_name, output_extension_family(cfg))
            renamed_from = (f'{safe_name}.ts'
                            if os.path.basename(output_path) != f'{safe_name}.ts' else None)
            if renamed_from:
                log.info('Recording %d: concat output renamed to %s - %s was already taken',
                         recording_id, os.path.basename(output_path), renamed_from)

            if len(valid_segments) == 1:
                # Only one segment - just rename it
                seg_path = valid_segments[0].file_path
                try:
                    os.rename(seg_path, output_path)
                    success = True
                    error_msg = None
                except Exception as exc:
                    success = False
                    error_msg = str(exc)
            else:
                # Write concat.txt and run ffmpeg concat
                concat_txt = os.path.join(tempfile.gettempdir(), f'dvr_{recording_id}_concat.txt')
                with open(concat_txt, 'w') as f:
                    for seg in valid_segments:
                        f.write(f"file '{seg.file_path}'\n")

                @retry_on_locked()
                def _mark_concat_started_and_commit():
                    add_recording_event(recording_id, CONCATENATION_STARTED,
                                        detail=f'Concatenating {len(valid_segments)} segments → {output_path}')
                    db.session.commit()

                _mark_concat_started_and_commit()

                from .config import resolve_ffmpeg_path
                ffmpeg_path = resolve_ffmpeg_path(cfg['ffmpeg']['path'])
                cmd = [
                    ffmpeg_path,
                    '-err_detect', 'ignore_err',
                    '-fflags', '+genpts+discardcorrupt',
                    '-f', 'concat',
                    '-safe', '0',
                    '-i', concat_txt,
                    '-c', 'copy',
                    '-y', output_path,
                ]
                log.info('Concat command: %s', ' '.join(cmd))

                try:
                    # Supervised, not deadlined: the join reads and writes however many
                    # bytes the capture produced, so a fixed budget is a bet on file size
                    # and a large recording loses it. A join that is still writing is
                    # working (dev/changelog/947). Progress is measured as bytes landing in
                    # the output file rather than ffmpeg's out_time, because writing bytes
                    # is the entire job of a `-c copy` and a byte count cannot go backwards
                    # the way a concat-demuxer timestamp can under +genpts.
                    run = supervise_ffmpeg(
                        cmd, output_path,
                        scratch_prefix='concat', scratch_key=recording_id,
                        interval=CONCAT_POLL_INTERVAL_SECONDS,
                        pre_output_timeout=concat_pre_output_timeout,
                        stall_seconds=concat_stall_seconds,
                        progress_signal='size', noun='concat',
                        label=f'Recording {recording_id} concat')
                    success = run.success
                    error_msg = None if success else run.error_msg
                except Exception as exc:
                    success = False
                    error_msg = str(exc)
                finally:
                    try:
                        os.unlink(concat_txt)
                    except OSError:
                        pass  # best-effort concat.txt cleanup

            if success:
                final_size = os.path.getsize(output_path) if os.path.exists(output_path) else 0
                pp_enabled = cfg['recording']['post_process'].get('enabled', True)
                mv_enabled = cfg['recording']['move_on_complete'].get('enabled', False)
                doing_postprocess = pp_enabled or mv_enabled

                @retry_on_locked()
                def _mark_concat_done_and_commit():
                    rec.output_path = output_path
                    rec.final_file_size = final_size
                    # A file that landed under a different name than the recording is called
                    # has to be explainable afterwards, so the rename is named here rather
                    # than left for the user to notice in the output path.
                    add_recording_event(recording_id, CONCATENATION_DONE,
                                        detail=f'Concat complete: {output_path} ({_fmt_bytes(final_size)})'
                                               + (f' - renamed, {renamed_from} was already taken'
                                                  if renamed_from else '')
                                               + (' - starting post-processing' if doing_postprocess else ''))
                    db.session.commit()

                _mark_concat_done_and_commit()
                log.info('Recording %d concat complete: %s (%s)', recording_id, output_path, _fmt_bytes(final_size))

                # Clean up segment files
                for seg in valid_segments:
                    try:
                        if os.path.exists(seg.file_path) and seg.file_path != output_path:
                            os.unlink(seg.file_path)
                    except Exception as exc:
                        log.warning('Could not delete segment %s: %s', seg.file_path, exc)

                from .postprocessor import do_postprocess
                do_postprocess(app, recording_id, output_path)
            else:
                # Remove whatever this attempt wrote, empty placeholder or multi-gigabyte
                # partial alike. Nothing references it: output_path is committed only on
                # success, so a failed attempt's file is unreachable from every row, every
                # teardown path and every retry - the 21.4 GB a killed join stranded on
                # 2026-09-13 would have sat there forever, and reserve_concat_output_path()
                # judges a stem by whether any file in its extension family exists, so it
                # would also have pushed every future attempt at this recording onto a `_2`
                # name and stepped around the garbage permanently (CLAUDE.md
                # teardown-releases-everything, dev/changelog/947).
                #
                # THIS IS ONLY SAFE BECAUSE THE SEGMENTS SURVIVE A FAILED CONCAT - they are
                # deleted on success only, a few lines up. If that ever changes, the partial
                # becomes the sole copy of the capture and this rule inverts.
                partial_size = 0
                try:
                    if os.path.exists(output_path):
                        partial_size = os.path.getsize(output_path)
                        os.unlink(output_path)
                except OSError as exc:
                    log.warning('Could not clean up failed concat output %s: %s', output_path, exc)
                else:
                    if partial_size > 0:
                        log.info('Recording %d: removed the %s partial the failed concat left '
                                 'at %s - the segments it was built from are still on disk',
                                 recording_id, _fmt_bytes(partial_size), output_path)

                @retry_on_locked()
                def _mark_concat_failed_and_commit():
                    r = db.session.get(Recording, recording_id)
                    if preserve_cancelled_status(
                            r, f'Concatenation failed after the recording was cancelled - '
                               f'status left ABORTED. {error_msg}'):
                        db.session.commit()
                        return False
                    r.status = REC_STATUS_FAILED
                    r.completed_at = datetime.utcnow()
                    add_recording_event(recording_id, CONCATENATION_DONE,
                                        detail=f'FAILED: {error_msg}')
                    db.session.commit()
                    return True

                if _mark_concat_failed_and_commit():
                    ev.publish(recording_id, CONCATENATION_DONE, {
                        'success': False,
                        'error': error_msg,
                        'status': REC_STATUS_FAILED,
                    })
                    log.error('Recording "%s" (#%d) concat failed: %s', rec.name, recording_id, error_msg,
                              extra={'recording_id': recording_id})

