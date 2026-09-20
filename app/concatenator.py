"""Concatenate all segment files into a single final .ts output."""
import bisect
import contextlib
import json
import logging
import os
import shutil
import tempfile
import threading
import time
from collections import namedtuple
from datetime import datetime

from .fmt_utils import fmt_bytes as _fmt_bytes, fmt_duration
from .proc_utils import delivery_ratio, supervise_ffmpeg, terminate_or_kill

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


# ── Live join child registry ──────────────────────────────────────────────────
# recording_id -> (the ffmpeg Popen joining its segments, the path it is writing). The
# join's own registry rather than an entry in postprocessor._active_conversions, because
# has_active_conversion() drives start_recording's collision-wait policy and a join is not
# a conversion - and rather than a field on _active_concats above, whose answer is "a chain
# owns this recording" and would then be answering two questions at once.
#
# It exists so the shutdown handler can reach the child: without it the join lives only in
# _run_concatenation's stack frame, and a SIGTERM mid-join orphans an ffmpeg that keeps
# writing while the next process's CONCATENATING sweep starts a second join beside it
# (dev/changelog/986).
_active_join_procs: dict = {}
_active_join_procs_lock = threading.Lock()


# ── Live join progress ────────────────────────────────────────────────────────
# recording_id -> what the join has written so far, for the surfaces that report it while
# it runs. In memory rather than on the Recording row, and the difference from the
# conversion's persisted conversion_* columns is not inconsistency: a conversion survives a
# restart and resumes where it stopped, so its progress has to outlive the process, while a
# join always re-runs from the top (the CONCATENATING sweep in scheduler.py), so a stored
# number would only ever describe an attempt that no longer exists. The process that owns
# the join is the one serving the page.
#
# Absent means "no ffmpeg is joining right now", which is a real state with its own wording:
# a row can sit at CONCATENATING for as long as serialize_concat makes it queue.
_concat_progress: dict = {}
_concat_progress_lock = threading.Lock()


def is_concat_active(recording_id: int) -> bool:
    with _active_concats_lock:
        return recording_id in _active_concats


def _start_concat_progress(recording_id: int, *, total_bytes: int, sizes):
    """Seed the entry before ffmpeg spawns, so the strip reads `0 of 16` from the moment the
    status flips rather than sitting blank for the first poll interval.

    `sizes` is the joinable segments' byte sizes in join order, already stat'ed for the
    disk-space check - the concat demuxer reads them in that order, so their running total
    is what turns bytes-written into "joined x of y" without asking the filesystem again.
    The denominator is taken from that same list rather than passed alongside it, so "x of
    y" cannot report a y the offsets disagree with - one list, one count.
    """
    running = []
    total = 0
    for size in sizes:
        total += size
        running.append(total)
    with _concat_progress_lock:
        _concat_progress[recording_id] = {
            'of': len(sizes), 'total_bytes': total_bytes, 'bytes': 0, 'joined': 0,
            'pct': None, 'eta_seconds': None,
            'started': time.monotonic(), '_offsets': running,
        }


def _publish_concat_progress(recording_id: int, *, written: int, eta_seconds):
    """One tick. Percent is bytes written against bytes to be read, which for a `-c copy`
    join differ only by muxing overhead - so it is clamped at 99 and the status leaving
    CONCATENATING is what reports completion, rather than a bar that reaches 100 and sits
    there through the analysis pass that follows."""
    with _concat_progress_lock:
        entry = _concat_progress.get(recording_id)
        if entry is None:
            return
        offsets = entry['_offsets']
        total = entry['total_bytes']
        entry['bytes'] = written
        # Overhead pushes the output past the last cumulative offset before ffmpeg exits;
        # bisect over that same list is what bounds this at the segment count, which is why
        # the count is derived from the list rather than carried beside it.
        entry['joined'] = bisect.bisect_right(offsets, written)
        entry['pct'] = min(99.0, written / total * 100.0) if total > 0 else None
        entry['eta_seconds'] = eta_seconds


def _clear_concat_progress(recording_id: int):
    with _concat_progress_lock:
        _concat_progress.pop(recording_id, None)


def concat_progress(recording_id: int):
    """What the join has written so far, or None when no ffmpeg is joining this recording.

    A plain dict lookup with no I/O of its own, which is what lets the recordings list call
    it per row (CLAUDE.md - no hidden I/O in per-row loops). Elapsed is derived here rather
    than stored so it is current at the moment it is read.
    """
    with _concat_progress_lock:
        entry = _concat_progress.get(recording_id)
        if entry is None:
            return None
        out = {k: v for k, v in entry.items() if not k.startswith('_')}
    out['elapsed_seconds'] = max(0.0, time.monotonic() - out.pop('started'))
    return out


def _track_join_child(recording_id: int, proc, output_path: str):
    """Register the join's ffmpeg so a terminal path outside this thread can reach it."""
    with _active_join_procs_lock:
        _active_join_procs[recording_id] = (proc, output_path)


def _untrack_join_child(recording_id: int):
    with _active_join_procs_lock:
        _active_join_procs.pop(recording_id, None)


def kill_active_joins():
    """Terminate every live join ffmpeg and remove the partial it was writing (called from
    run.py on SIGTERM, beside kill_all_active and kill_active_conversions).

    The partial goes too, which is where this differs from kill_active_conversions: a
    conversion is checkpointed and the startup resume picks up its parts, while a join always
    re-runs from the top, so its half-written output is referenced by nothing. Leaving it
    behind is not merely untidy - reserve_concat_output_path() stakes a stem by whether any
    file in its extension family exists, so the partial would push every future attempt at
    this recording onto a `_2` name permanently, and nothing lists it for cleanup
    (recorder.recording_disk_paths knows only committed paths).

    Deleting it is safe for the same reason the failed-concat path a few hundred lines below
    gives: output_path is committed only after supervise_ffmpeg returns, and the segments are
    deleted only after that commit, so a child still running means the capture is intact on
    disk and this file is the sole unreachable artifact. Deliberately does no DB work - a
    signal handler cannot - and the join threads are daemons, so their own finally blocks do
    not run once the interpreter starts shutting down.
    """
    with _active_join_procs_lock:
        entries = list(_active_join_procs.items())
        _active_join_procs.clear()
    for rid, (proc, output_path) in entries:
        log.info('Shutdown: killing live concat for recording %d', rid)
        terminate_or_kill(proc, hard=True)
        try:
            if output_path and os.path.exists(output_path):
                partial = os.path.getsize(output_path)
                os.unlink(output_path)
                log.info('Shutdown: removed recording %d\'s partial join output %s (%s)',
                         rid, output_path, _fmt_bytes(partial))
        except OSError as exc:
            log.warning('Shutdown: could not remove partial join output %s: %s',
                        output_path, exc)


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


def do_concatenation(app, recording_id: int, *, reason: str = 'Stop time reached; beginning the join'):
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


def joinable_segments(segments):
    """The segments that go into the final file, in the order they were captured.

    The one definition of "joinable", so the count a page shows and the list the concat
    actually builds cannot disagree - they did, and a strip reading "19 segments being
    joined" over a join of 16 is the kind of number nobody can explain. Two tests, and they
    answer different questions: the file has to be on disk with bytes in it (a segment whose
    capture never produced one, or whose file went missing), and the row must not be
    excluded (the app decided the content was not the channel - see
    RecordingSegment.excluded_reason).
    """
    return [
        s for s in segments
        if not s.excluded
        and s.file_path and os.path.exists(s.file_path) and os.path.getsize(s.file_path) > 0
    ]


def _record_discarded_rollup(recording_id: int, excluded_segments):
    """Roll the discarded segments up onto the Recording row: how many, and how many seconds
    of content they held between them.

    Columns rather than a scan of the segment rows because both numbers are displayed, and a
    displayed number read by parsing rows on every page render is the per-row I/O this app
    has a mandatory regression test for. Written even when nothing was discarded: 0 is a real
    answer ("this recording was checked and kept everything") and NULL means the recording
    never got here, which is a different fact (dev/changelog/957).

    Content seconds, not wall clock - the question a reader is asking is how much of the
    finished file's missing airtime went to placeholder video, and a 600s clip that arrived
    in 5s cost the recording 600s of program, not 5.
    """
    from . import db
    from .database import Recording
    from .db_utils import retry_on_locked

    count = len(excluded_segments)
    seconds = sum(s.content_duration_seconds or 0.0 for s in excluded_segments)

    @retry_on_locked()
    def _store_rollup_and_commit():
        r = db.session.get(Recording, recording_id)
        if r is not None:
            r.discarded_segment_count = count
            r.discarded_seconds = seconds
        db.session.commit()

    _store_rollup_and_commit()
    if count:
        log.warning('Recording %d: %d segment(s) discarded before the join, holding %.0fs of '
                    'placeholder video', recording_id, count, seconds)


def segment_wall_seconds(segment):
    """How long a finished segment's capture actually ran, or None while it is still open.

    Its own function because "wall clock" is one of four durations a recording carries
    (scheduled, requested, wall, content) and the delivery surplus below is meaningless
    unless it is measured against this one.
    """
    if segment.started_at is None or segment.ended_at is None:
        return None
    return (segment.ended_at - segment.started_at).total_seconds()


def delivery_surplus_seconds(content_seconds, wall_seconds):
    """Seconds of content a segment holds beyond the seconds it ran for, or None when the
    pair cannot say.

    SURPLUS rather than the ratio proc_utils.delivery_ratio() reports, and the difference is
    the measurement rather than a preference. The only legitimate source of content arriving
    early is the provider's per-connect back-buffer, which is a fixed 13-29s
    (dev/changelog/942) - a CONSTANT, so a constant threshold separates it identically at
    every segment length. A ratio threshold cannot: the same 29s reads 1.48x on a one-minute
    segment and 1.01x on a long one, so it is simultaneously too tight to trust on short
    segments and too loose to catch an hours-long segment running quietly 10% fast.

    The ratio is still what explains a finding to a reader ("3.76x real time"), which is why
    both numbers reach the event. Only this one decides.
    """
    if content_seconds is None or wall_seconds is None:
        return None
    if wall_seconds <= 0:
        return None
    return content_seconds - wall_seconds


FastDeliveryFinding = namedtuple(
    'FastDeliveryFinding',
    'segment_number content_seconds wall_seconds surplus_seconds ratio')


def fast_delivery_findings(measured, surplus_threshold):
    """The segments that delivered more content than their wall clock allows, as findings.

    Pure, and takes plain numbers rather than rows, so the threshold's behavior at its
    boundaries is testable without a recording. `measured` is
    (segment id, segment number, content seconds, wall seconds or None).

    A non-positive threshold disables the flag outright, matching every other trip-wire in
    the watchdog config block.
    """
    if not surplus_threshold or surplus_threshold <= 0:
        return []
    findings = []
    for _seg_id, seg_num, content, wall in measured:
        surplus = delivery_surplus_seconds(content, wall)
        if surplus is None or surplus <= surplus_threshold:
            continue
        findings.append(FastDeliveryFinding(
            segment_number=seg_num, content_seconds=content, wall_seconds=wall,
            surplus_seconds=surplus,
            # The one home for this measurement (dev/changelog/964) - the live detector and
            # the sentence describing a finished segment must never be able to disagree
            # about what a delivery rate is.
            ratio=delivery_ratio(content, wall)))
    return findings


def fast_delivery_detail(finding):
    """The event sentence for one finding.

    Claims ONLY what was measured. An earlier draft of this feature said the provider had
    "served buffered content faster than live, so the finished file holds more than the
    recording window" - which was inferred from the ratio alone and is false on the very
    recording that motivated the work: sampled across all 8h38m, every frame read the same
    lap of the same race, so the file held one short stretch re-served, not hours of
    earlier coverage. Both causes are named, neither is chosen, and the app says plainly
    that it did not look at the picture (dev/changelog/966).
    """
    return (
        f'Segment {finding.segment_number} holds '
        f'{fmt_duration(finding.content_seconds, with_seconds=True)} of video but ran for '
        f'{fmt_duration(finding.wall_seconds, with_seconds=True)} of capture - '
        f'{fmt_duration(finding.surplus_seconds, with_seconds=True)} more than the clock '
        f'({finding.ratio:.2f}x real time). Video arriving faster than the clock is video '
        f'the provider already had: it may be buffered content served after a reconnect, '
        f'or the same stretch re-served over and over. Nothing here compares frames, so '
        f'this does not say which. The segment was kept and joined into the final file - '
        f'check it before you rely on it.')


def _delivery_rate_reported_segments(recording_id: int):
    """Segment numbers that already carry a delivery-rate event on this recording.

    One query for the recording, never one per segment: this runs inside the concat, but
    the same shape in a request handler is the per-row I/O the scaling tests guard.
    """
    from .database import RecordingEvent, DIAGNOSTICS

    reported = set()
    rows = RecordingEvent.query.filter_by(
        recording_id=recording_id, event_type=DIAGNOSTICS).all()
    for row in rows:
        if not row.extra_data or row.segment_number is None:
            continue
        try:
            extra = json.loads(row.extra_data)
        except (ValueError, TypeError):
            continue  # a malformed row is not evidence the finding was reported
        if isinstance(extra, dict) and extra.get('kind') == 'delivery_rate':
            reported.add(row.segment_number)
    return reported


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

    The same pass flags the segments whose content ran ahead of the clock by more than
    watchdog.fast_delivery_surplus_seconds - see _fast_delivery_disclosure() below, which
    shares this closure so a measurement and the sentence describing it are one commit.
    """
    from . import db
    from .config import load_config
    from .database import Recording, RecordingSegment, add_recording_event, DIAGNOSTICS
    from .db_utils import retry_on_locked
    from .probe import parse_ffprobe

    # Hoisted out of the probe loop: config is read once for the whole pass, never per
    # segment (CLAUDE.md - no hidden I/O in per-row loops).
    surplus_threshold = load_config()['watchdog']['fast_delivery_surplus_seconds']

    measured = []  # (seg id, segment number, content seconds, wall seconds or None)
    for seg in valid_segments:
        try:
            info = parse_ffprobe(seg.file_path, count_packets=False, timeout=30)
        except Exception as exc:
            log.warning('Recording %d seg %s: content-duration probe failed: %s',
                        recording_id, seg.segment_number, exc)
            continue
        duration = info.get('duration')
        if duration is not None and duration > 0:
            measured.append((seg.id, seg.segment_number, float(duration),
                             segment_wall_seconds(seg)))

    if not measured:
        return

    flagged = fast_delivery_findings(measured, surplus_threshold)
    # Read committed state, not inferred state: a segment already carrying its event has
    # been reported, and "Retry join" re-enters this function from the top. Deriving it
    # from the stored duration instead would be the "already done is a fact you recorded"
    # trap - the duration is written by the phase BEFORE the event, so a crash between the
    # two would silence the finding permanently.
    already_reported = _delivery_rate_reported_segments(recording_id)

    @retry_on_locked()
    def _store_content_durations_and_commit():
        for seg_id, _seg_num, duration, _wall in measured:
            row = db.session.get(RecordingSegment, seg_id)
            if row is not None:
                row.content_duration_seconds = duration
        rec = db.session.get(Recording, recording_id)
        if rec is not None:
            # Written on every pass, including the one that flags nothing: 0 is the real
            # answer "this recording was checked and its video arrived on time", and NULL
            # is the different fact that nothing ever looked (dev/changelog/957's rollup
            # made the same distinction and for the same reason).
            rec.fast_delivery_segment_count = len(flagged)
            rec.fast_delivery_seconds = sum(f.content_seconds for f in flagged)
        for finding in flagged:
            if finding.segment_number in already_reported:
                continue
            add_recording_event(recording_id, DIAGNOSTICS,
                                detail=fast_delivery_detail(finding),
                                segment_number=finding.segment_number,
                                # Only the kind: content duration, started_at and ended_at
                                # are all columns on the segment row and the ratio derives
                                # from them, so storing any of them here would be the second
                                # source of truth CLAUDE.md's strict partition forbids.
                                extra={'kind': 'delivery_rate'})
        db.session.commit()

    _store_content_durations_and_commit()
    log.info('Recording %d: measured content duration on %d of %d segment(s)',
             recording_id, len(measured), len(valid_segments))
    for finding in flagged:
        log.warning('Recording %d seg %d: %.0fs of content in %.0fs of capture '
                    '(+%.0fs, %.2fx real time) - flagged for review',
                    recording_id, finding.segment_number, finding.content_seconds,
                    finding.wall_seconds, finding.surplus_seconds, finding.ratio)


def _run_concatenation(app, recording_id: int, *, reason: str):
    from . import db
    from .config import load_config
    from .database import (
        Recording, RecordingSegment, add_recording_event, preserve_cancelled_status,
        CAPTURE_COMPLETE, CONCATENATION_STARTED, CONCATENATION_DONE,
        REC_STATUS_ANALYZING, REC_STATUS_CONCATENATING, REC_STATUS_FAILED, REC_STATUS_PAUSED,
        FAILURE_ALL_SEGMENTS_PLACEHOLDER, FAILURE_SEGMENT_FILES_MISSING, FAILURE_NO_VALID_SEGMENTS,
        FAILURE_PAUSED_NOTHING_CAPTURED, FAILURE_INSUFFICIENT_DISK_SPACE, FAILURE_CONCAT_ERROR,
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
                    detail=f'The join was already complete ({os.path.basename(finished_output)}) - '
                           f'resuming post-processing')
                r.status = REC_STATUS_ANALYZING
                db.session.commit()

            _mark_resuming_postprocess_and_commit()
            ev.publish(recording_id, CAPTURE_COMPLETE, {'status': REC_STATUS_ANALYZING})

            from .postprocessor import do_postprocess
            do_postprocess(app, recording_id, finished_output)
            return

        log.info('Starting concatenation for recording %d (%s)', recording_id, rec.name)
        # Read before the flip below erases it: a pause taken before any segment held data
        # reaches the no-valid-segments branch, and "never recorded any data" alone does not
        # say the recording was paused the whole time it could have been capturing.
        was_paused = rec.status == REC_STATUS_PAUSED

        @retry_on_locked()
        def _mark_concatenating_and_commit():
            add_recording_event(recording_id, CAPTURE_COMPLETE, detail=reason)
            rec.status = REC_STATUS_CONCATENATING
            db.session.commit()

        _mark_concatenating_and_commit()

        ev.publish(recording_id, CAPTURE_COMPLETE, {'status': REC_STATUS_CONCATENATING})

        # Both of the recording's own images, taken now while the segment files still
        # exist (a successful concat deletes them). Subprocess side effects - deliberately
        # outside any retry_on_locked closure. They answer different questions and neither
        # replaces the other: the thumbnail is the final frame, the poster is a frame from
        # inside the program itself (dev/changelog/1060).
        from .recorder import persist_final_thumbnail, persist_poster_frame
        persist_final_thumbnail(recording_id)
        persist_poster_frame(recording_id)

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

            valid_segments = joinable_segments(segments)
            # Kept out of the join but NOT out of the teardown: these still have files on
            # disk and they are deleted alongside the joined ones below, so a discarded
            # placeholder cannot quietly accumulate gigabytes under /dvr (CLAUDE.md,
            # teardown releases everything the create path acquired). They stay on disk
            # until the join succeeds so a failed concat's "segments preserved" path still
            # holds the complete picture of what the capture produced.
            excluded_segments = [s for s in segments if s.excluded]
            _record_discarded_rollup(recording_id, excluded_segments)

            if not valid_segments:
                # Three different failures reach this branch and only two of them are channel
                # signal, so the message has to name which one happened. bytes_recorded
                # separates the first two: the watchdog wrote it from the capture itself, so a
                # positive total means ffmpeg really did pull those bytes down and the files
                # went missing afterwards, locally. Blaming the channel for that is what
                # apply_recording_health_observation's own contract forbids, and it cost a
                # healthy feed 100 -> 22 on a capture that had succeeded (dev/docs/BUGS.md
                # 2026-08-24). The third is checked first because it is the specific case:
                # the files are present and the app itself refused them, which neither of the
                # other two sentences describes (dev/changelog/957).
                captured_bytes = sum(s.bytes_recorded or 0 for s in segments)
                stream_delivered = captured_bytes > 0
                all_discarded = bool(excluded_segments) and len(excluded_segments) == len(segments)
                if all_discarded:
                    why = (f'FAILED: every one of the {len(segments)} captured segment(s) was '
                           f'the provider\'s placeholder clip rather than the channel, so there '
                           f'was nothing to join - the channel was down for the whole window')
                    log_why = 'every segment was a provider placeholder'
                    failure_reason = FAILURE_ALL_SEGMENTS_PLACEHOLDER
                elif stream_delivered:
                    why = (f'FAILED: no segment files on disk, but the capture recorded '
                           f'{_fmt_bytes(captured_bytes)} across {len(segments)} segment(s) - '
                           f'the files went missing after capture, so this is not a stream fault')
                    log_why = 'files missing after capture'
                    failure_reason = FAILURE_SEGMENT_FILES_MISSING
                elif was_paused:
                    why = ('FAILED: no valid segments found - the recording was paused before '
                           'the capture recorded any data, and its window ended while paused')
                    log_why = 'paused before anything was captured'
                    failure_reason = FAILURE_PAUSED_NOTHING_CAPTURED
                else:
                    why = 'FAILED: no valid segments found - the capture never recorded any data'
                    log_why = 'nothing was captured'
                    failure_reason = FAILURE_NO_VALID_SEGMENTS
                log.error('Recording "%s" (#%d): no valid segments to concatenate (%s)',
                          rec.name, recording_id, log_why,
                          extra={'recording_id': recording_id, 'already_alerted': True})

                @retry_on_locked()
                def _mark_no_segments_failed_and_commit():
                    r = db.session.get(Recording, recording_id)
                    if preserve_cancelled_status(
                            r, 'The join found no valid segments, but the recording had '
                               'already been cancelled - status left ABORTED.'):
                        db.session.commit()
                        return False
                    r.status = REC_STATUS_FAILED
                    r.completed_at = datetime.utcnow()
                    r.failure_reason = failure_reason
                    add_recording_event(recording_id, CONCATENATION_DONE, detail=why)
                    db.session.commit()
                    return True

                if _mark_no_segments_failed_and_commit():
                    if not stream_delivered:
                        from .health_score import apply_recording_health_observation
                        apply_recording_health_observation(app, recording_id, 'failed')
                    # `status` is not optional on a frame that moves a row: dashboard.js
                    # relabels from it and ignores a frame without one, so this row would
                    # sit on its old badge (dev/docs/BUGS.md 2026-09-14, dev/changelog/1023).
                    ev.publish(recording_id, CONCATENATION_DONE, {
                        'success': False, 'error': 'no valid segments',
                        'status': REC_STATUS_FAILED,
                    })
                    # Typed rather than left to the log->alert catch-all, which gave this
                    # an "Application Error (log)" label and no deep link to the recording
                    # it is about (dev/changelog/930). Nothing clears it: there is no file
                    # to recover and nothing re-runs this concatenation.
                    from .alerts import create_alert
                    create_alert(
                        'CONCATENATION_FAILED',
                        f'Join failed: {rec.name}',
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

            # Stat'ed once and kept: the total is the disk-space check's, and the individual
            # sizes are what the progress registry turns into "joined x of y".
            seg_sizes = [os.path.getsize(s.file_path) for s in valid_segments]
            total_seg_bytes = sum(seg_sizes)
            free_bytes = shutil.disk_usage(dvr_dir).free
            if free_bytes < total_seg_bytes:
                log.error('Recording "%s" (#%d): insufficient disk space for concat - need %s, have %s',
                          rec.name, recording_id, _fmt_bytes(total_seg_bytes), _fmt_bytes(free_bytes),
                          extra={'recording_id': recording_id})

                @retry_on_locked()
                def _mark_disk_space_failed_and_commit():
                    r = db.session.get(Recording, recording_id)
                    if preserve_cancelled_status(
                            r, 'The join ran out of disk space, but the recording had '
                               'already been cancelled - status left ABORTED.'):
                        db.session.commit()
                        return False
                    r.status = REC_STATUS_FAILED
                    r.completed_at = datetime.utcnow()
                    r.failure_reason = FAILURE_INSUFFICIENT_DISK_SPACE
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

            # Seeded here, cleared in the finally below, so every path out of the join -
            # success, ffmpeg failure, an exception, a Retry that re-enters this function -
            # leaves nothing behind claiming a join is running (CLAUDE.md, teardown releases
            # everything the create path acquired). A queued concat has not reached this
            # point and correctly has no entry.
            _start_concat_progress(recording_id, total_bytes=total_seg_bytes,
                                   sizes=seg_sizes)
            try:
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
                                            detail=f'Joining {len(valid_segments)} segments → {output_path}')
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

                    # Fed bytes rather than the media-seconds it was written for, which is
                    # sound because it only ever divides one cumulative quantity by another -
                    # the units cancel. Reused rather than re-derived so the join's ETA
                    # behaves like the conversion's: debounced, and silent until there is
                    # enough signal to trust one.
                    from .postprocessor import EtaSmoother
                    eta = EtaSmoother(total_seg_bytes)

                    def _publish_join(wall, out_time, size):
                        _publish_concat_progress(recording_id, written=size,
                                                 eta_seconds=eta.update(wall, size))

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
                            on_progress=_publish_join,
                            on_spawn=lambda proc: _track_join_child(
                                recording_id, proc, output_path),
                            label=f'Recording {recording_id} concat')
                        success = run.success
                        error_msg = None if success else run.error_msg
                    except Exception as exc:
                        success = False
                        error_msg = str(exc)
                    finally:
                        # Unregistered on every exit - success, ffmpeg failure, an exception -
                        # so nothing outside this thread can reach a dead process or unlink an
                        # output the success path is about to commit.
                        _untrack_join_child(recording_id)
                        try:
                            os.unlink(concat_txt)
                        except OSError:
                            pass  # best-effort concat.txt cleanup
            finally:
                _clear_concat_progress(recording_id)

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
                for seg in valid_segments + excluded_segments:
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
                            r, f'The join failed after the recording was cancelled - '
                               f'status left ABORTED. {error_msg}'):
                        db.session.commit()
                        return False
                    r.status = REC_STATUS_FAILED
                    r.completed_at = datetime.utcnow()
                    r.failure_reason = FAILURE_CONCAT_ERROR
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

