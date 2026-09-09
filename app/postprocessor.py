"""Post-processing after concatenation: health data, format conversion and/or file move."""
import glob
import logging
import os
import re
import shutil
import subprocess
import threading
import time
import uuid
from datetime import datetime

from .db_utils import retry_on_locked
from .fmt_utils import fmt_bytes as _fmt_bytes, fmt_duration
from .fs_utils import ensure_dir
from .proc_utils import GrowthMonitor, read_stderr_tail, terminate_or_kill

log = logging.getLogger(__name__)

# ── Live conversion registry ──────────────────────────────────────────────────
# recording_id -> the ffmpeg Popen currently converting it. Mirrors recorder._active
# so a shutdown hook (kill_active_conversions) can terminate a live conversion and a
# double-run guard can refuse a second ffmpeg for the same id. Every child must be
# terminated on every terminal path (success/fail/stall/timeout/shutdown), per CLAUDE.md
# subprocess-discipline.
_active_conversions: dict = {}
# recording_ids whose conversion the user asked to cancel. The restart loop consumes the
# flag (check-and-clear) and takes the abort path instead of restarting, so a cancel is not
# mistaken for a crash-and-restart. Guarded by _active_lock.
_cancel_requested: set = set()
_active_lock = threading.Lock()


def is_conversion_active(recording_id: int) -> bool:
    with _active_lock:
        return recording_id in _active_conversions


def has_active_conversion() -> bool:
    """Is ANY recording's mp4 conversion currently running? Used by
    recorder.start_recording under recording.post_process.collision_policy 'wait' to decide
    whether a freshly-due recording must wait for local resources to free up."""
    with _active_lock:
        return bool(_active_conversions)


def request_cancel_conversion(recording_id: int) -> bool:
    """Ask a live conversion to stop: flag it so the restart loop aborts instead of
    restarting, and kill the current ffmpeg. Returns True if a live conversion was found and
    signalled (the loop will set the terminal status), False if none was live (the caller
    marks the row aborted itself - there is no loop to do it)."""
    with _active_lock:
        proc = _active_conversions.get(recording_id)
        if proc is None:
            return False
        _cancel_requested.add(recording_id)
    log.info('Cancel requested for recording %d conversion - killing ffmpeg', recording_id)
    terminate_or_kill(proc, hard=True)
    return True


def _consume_cancel(recording_id: int) -> bool:
    """Atomically check-and-clear the cancel flag for this id."""
    with _active_lock:
        if recording_id in _cancel_requested:
            _cancel_requested.discard(recording_id)
            return True
        return False


def cancelled_meanwhile(recording_id: int) -> bool:
    """Has the user cancelled this recording since this chain started working on it?

    Re-reads the row's status from the database rather than the identity map: the cancel
    is committed by a request thread in its own session, so a plain db.session.get() here
    can serve a stale in-memory row. Checked at phase boundaries so a cancelled recording
    stops being worked on instead of running to completion (dev/changelog/667).
    """
    from . import db
    from .database import Recording, REC_STATUS_ABORTED
    db.session.expire_all()
    r = db.session.get(Recording, recording_id)
    return r is not None and r.status == REC_STATUS_ABORTED


def kill_active_conversions():
    """Terminate every live conversion ffmpeg (called from run.py on SIGTERM). A killed
    attempt is honestly counted: the row stays CONVERTING with its current
    conversion_attempts, and the startup CONVERTING-resume path increments+relaunches it."""
    with _active_lock:
        procs = list(_active_conversions.items())
    for rid, proc in procs:
        log.info('Shutdown: killing live conversion for recording %d', rid)
        terminate_or_kill(proc, hard=True)
    with _active_lock:
        _active_conversions.clear()


def _collision_window_seconds(expected_duration, multiplier) -> float:
    """Pre-start/resume lookahead window: the recording-being-converted's own duration
    divided by the configured 'runs at Nx realtime' assumption
    (recording.post_process.collision_lookahead_multiplier). 0 when the duration is
    unknown - degrades to reacting only to a recording that is already IN_PROGRESS or
    already overdue, never a future one. The only place the >= 0.1 floor is enforced -
    None means "not set" (falls back to 1.0); anything else, including 0 or negative, is
    clamped up rather than silently swapped for the default. Pure - no I/O."""
    if not expected_duration or expected_duration <= 0:
        return 0.0
    m = multiplier if multiplier is not None else 1.0
    return expected_duration / max(0.1, m)


def _conversion_collision_conflict(within_seconds):
    """A Recording that argues against starting/continuing an mp4 conversion right now -
    one that is IN_PROGRESS, or SCHEDULED to start within within_seconds. None = clear.

    Mirrors channel_tester.imminent_recording_conflict() (DESIGN-concurrency.md 5.5),
    extended to also cover IN_PROGRESS: conversion CPU/disk contention with a recording
    matters for the recording's whole run, not just its run-up
    (recording.post_process.collision_policy).
    """
    from datetime import timedelta
    from .database import Recording, REC_STATUS_SCHEDULED, REC_STATUS_IN_PROGRESS
    from . import db as _db

    cutoff = datetime.utcnow() + timedelta(seconds=max(0.0, within_seconds))
    return Recording.query.filter(
        _db.or_(
            Recording.status == REC_STATUS_IN_PROGRESS,
            _db.and_(Recording.status == REC_STATUS_SCHEDULED, Recording.start_time <= cutoff),
        )
    ).order_by(Recording.start_time).first()


def _wait_for_conversion_clear(recording_id, within_seconds, poll_seconds=5, log_every=12):
    """Block (polling) until _conversion_collision_conflict is clear, or this recording is
    cancelled meanwhile. Mirrors concatenator._wait_for_no_active_recording, with a
    lookahead window instead of a bare IN_PROGRESS check
    (recording.post_process.collision_policy)."""
    from .database import REC_STATUS_IN_PROGRESS
    i = 0
    while True:
        conflict = _conversion_collision_conflict(within_seconds)
        if conflict is None:
            return
        if cancelled_meanwhile(recording_id):
            return
        if i % log_every == 0:
            where = ('in progress' if conflict.status == REC_STATUS_IN_PROGRESS
                     else f'starting at {conflict.start_time}')
            log.info('Recording %d conversion waiting on recording "%s" (%s) - '
                     'recording.post_process.collision_policy', recording_id, conflict.name, where)
        time.sleep(poll_seconds)
        i += 1


def _round_eta(secs: float) -> int:
    """Round an ETA to a human bucket so the displayed value doesn't jitter by the second:
    nearest 15s under 2min, nearest 30s under 10min, nearest minute above."""
    secs = max(0.0, secs)
    if secs < 120:
        return int(round(secs / 15) * 15)
    if secs < 600:
        return int(round(secs / 30) * 30)
    return int(round(secs / 60) * 60)


class EtaSmoother:
    """Debounced ETA for a conversion (the 'no yo-yo' requirement).

    Fed cumulative (wall_elapsed, out_time) samples - out_time is media-seconds already
    converted, from ffmpeg's -progress out_time_us. Returns a smoothed ETA in seconds, or
    None while still 'estimating' (too little signal to trust).

    Why it stays stable where a naive ETA swings 1h→8h→5min:
    - The base rate is *cumulative* (out_time / wall_elapsed), which is inherently smoothed
      because its denominator only grows - one slow interval can't move it much.
    - A slow EWMA of per-interval rates is blended in (small alpha) so a genuine sustained
      slowdown is still reflected, without overreacting to a single sample.
    - The emitted ETA is finally rate-limited to at most ±max_step relative change per
      update, which is a hard guarantee against yo-yo regardless of input noise.
    """

    def __init__(self, expected_duration, *, min_wall=15.0, min_frac=0.01,
                 alpha=0.2, blend=0.3, max_step=0.20):
        self.expected = expected_duration or 0.0
        self.min_wall = min_wall
        self.min_frac = min_frac
        self.alpha = alpha        # EWMA weight on the newest interval rate
        self.blend = blend        # weight of the EWMA vs. the cumulative rate
        self.max_step = max_step  # max relative ETA change allowed per update
        self._ewma_rate = None
        self._prev_wall = 0.0
        self._prev_out = 0.0
        self._last_eta = None     # last raw (unrounded, unclamped-source) ETA

    def update(self, wall_elapsed, out_time):
        """Return a smoothed ETA in seconds, or None to signal 'estimating…'."""
        expected = self.expected
        frac = (out_time / expected) if expected > 0 else 0.0

        # Update the interval-rate EWMA regardless (so it's warm once we start emitting).
        dw = wall_elapsed - self._prev_wall
        do = out_time - self._prev_out
        if dw > 0 and do >= 0:
            interval_rate = do / dw
            self._ewma_rate = (interval_rate if self._ewma_rate is None
                               else self.alpha * interval_rate + (1 - self.alpha) * self._ewma_rate)
        self._prev_wall, self._prev_out = wall_elapsed, out_time

        # Suppress an estimate until there's enough signal to trust one.
        if expected <= 0 or wall_elapsed < self.min_wall or frac < self.min_frac or out_time <= 0:
            return None

        cumulative_rate = out_time / wall_elapsed
        ewma = self._ewma_rate if self._ewma_rate and self._ewma_rate > 0 else cumulative_rate
        effective = (1 - self.blend) * cumulative_rate + self.blend * ewma
        if effective <= 0:
            return _round_eta(self._last_eta) if self._last_eta is not None else None

        raw_eta = max(0.0, expected - out_time) / effective

        # Hard debounce: clamp the raw ETA to ±max_step of the previous raw ETA.
        if self._last_eta is not None:
            hi = self._last_eta * (1 + self.max_step)
            lo = self._last_eta * (1 - self.max_step)
            raw_eta = min(hi, max(lo, raw_eta))
        self._last_eta = raw_eta
        return _round_eta(raw_eta)


class ConversionResult:
    """Outcome of one supervised conversion attempt.

    `out_time` is how far into the source the attempt had encoded when it ended. The
    restart loop compares it across attempts: a defect in the source file stops every
    attempt at the same offset, and restarting from the top cannot get past it.
    `decode_errors` is how many frames ffmpeg failed to decode, counted so a conversion
    that finished by concealing damage can still say the damage happened.
    """
    def __init__(self, success, reason=None, error_msg=None, out_time=None,
                 decode_errors=0):
        self.success = success
        # 'success' | 'died' | 'stalled' | 'no_output' | 'timeout' | 'preempted'.
        # 'no_output' is the pre-output budget expiring; 'timeout' survives only for the
        # stall-detection-disabled fallback, and is no longer reachable in the default
        # configuration (dev/changelog/865).
        self.reason = reason
        self.error_msg = error_msg
        self.out_time = out_time
        self.decode_errors = decode_errors


# A ConversionResult.reason is an internal token; the event log is read by a person. Every
# reason a restart can follow is named here, so a new one that is not named renders as the
# bare token rather than being silently mistranslated as an existing phrase.
_RESTART_REASON_PHRASE = {
    'died': 'stopped',
    'stalled': 'stalled',
    'no_output': 'produced no output',
    'timeout': 'ran past its limit with stall detection disabled',
}


def _read_progress_tail(progress_path):
    """Parse ffmpeg's -progress file, returning the latest value of each key it emits in
    repeating key=value blocks. Returns (out_time_us:int|None, total_size:int|None,
    done:bool). Missing/unreadable file yields (None, None, False)."""
    out_time_us = None
    total_size = None
    done = False
    try:
        with open(progress_path, 'r') as fh:
            for line in fh:
                line = line.strip()
                if '=' not in line:
                    continue
                key, _, val = line.partition('=')
                if key == 'out_time_us':
                    try:
                        out_time_us = int(val)
                    except ValueError:
                        pass
                elif key == 'total_size':
                    try:
                        total_size = int(val)
                    except ValueError:
                        pass
                elif key == 'progress':
                    done = (val == 'end')
    except OSError:
        return None, None, False
    return out_time_us, total_size, done


def _persist_conversion_snapshot(recording_id, pct, size, eta):
    """Write one progress tick to the recording row. Its own re-fetch→mutate→commit
    closure so a lock-retry can't drop the write; extracted so it's directly testable
    without a real ffmpeg."""
    from . import db
    from .database import Recording

    @retry_on_locked()
    def _do():
        r = db.session.get(Recording, recording_id)
        if r is None:
            return
        r.conversion_progress_pct = pct
        r.conversion_out_size = size or None
        r.conversion_eta_seconds = eta
        r.conversion_updated_at = datetime.utcnow()
        db.session.commit()

    _do()


def run_conversion_supervised(app, recording_id, cmd, output_path, *,
                              expected_duration, pre_output_timeout, interval, stall_seconds,
                              collision_policy='off', collision_window_seconds=0):
    """Spawn one conversion ffmpeg and supervise it: poll -progress, publish/persist a
    progress snapshot, and detect death, stall, or (collision_policy='cancel') a colliding
    recording. Returns a ConversionResult for the caller's restart loop to act on.

    A CONVERSION THAT IS STILL ADVANCING IS NEVER KILLED. There is no whole-job deadline:
    `pre_output_timeout` bounds only the phase before ffmpeg muxes its first frame, and
    after that `stall_seconds` is the sole authority (dev/changelog/865). The deadline this
    replaced fired on healthy work whenever a job was simply longer than its budget - it
    killed a 3.6h 1080p59.94 re-encode at elapsed 14,404s against a 14,400s number
    calibrated on a machine that encoded twice as fast as this one, and the retry then
    re-ran the identical command from 0% twice more.

    The two rules are not interchangeable and both are needed. Stall detection is gated on
    `out_time_us > 0` because a badly-damaged source makes ffmpeg seek and analyze for
    minutes before muxing anything (dev/docs/BUGS.md 2026-07-24), so it cannot see a
    conversion that never starts - that is what the pre-output budget is for.

    The ffmpeg spawn is a non-idempotent side effect and lives OUTSIDE every
    retry_on_locked closure (CLAUDE.md). Each progress-snapshot write is its own small
    re-fetch→mutate→commit closure. The child is terminated on every exit path and
    unregistered in finally.
    """
    from . import db
    from .database import Recording, REC_STATUS_CONVERTING, REC_STATUS_IN_PROGRESS
    from . import events as ev

    # Scratch (-progress + stderr tail) sits alongside the output file - both are tiny
    # (KB), and this keeps them inside whatever dir the output lives in rather than a
    # hardcoded /dvr/tmp (which would escape a test sandbox).
    #
    # The filename carries a per-attempt token, and leftovers from earlier attempts are
    # reaped below, because the poll loop reads the progress file on its FIRST pass -
    # milliseconds after Popen, inside the ~0.25s window before ffmpeg truncates it. On a
    # fixed filename that read returns the previous attempt's out_time, GrowthMonitor
    # latches it as a high-water mark the new attempt can never beat, and the conversion is
    # killed as stalled at exactly stall_seconds. A shutdown mid-conversion skips the
    # finally-block unlink, so stale files are routine. See dev/docs/BUGS.md 2026-07-23.
    scratch_dir = os.path.dirname(output_path) or '.'
    # Both patterns are listed explicitly per id: a bare f'{recording_id}*' glob would let
    # id 6 match id 64's scratch files.
    stale_paths = (glob.glob(os.path.join(scratch_dir, f'.conv-progress-{recording_id}-*.txt'))
                   + glob.glob(os.path.join(scratch_dir, f'.conv-stderr-{recording_id}-*.log'))
                   + [os.path.join(scratch_dir, f'.conv-progress-{recording_id}.txt'),
                      os.path.join(scratch_dir, f'.conv-stderr-{recording_id}.log')])
    for stale in stale_paths:
        try:
            os.unlink(stale)
        except OSError:
            pass  # best-effort reap; a unique token below is what actually guarantees safety
    token = uuid.uuid4().hex[:8]
    progress_path = os.path.join(scratch_dir, f'.conv-progress-{recording_id}-{token}.txt')
    stderr_path = os.path.join(scratch_dir, f'.conv-stderr-{recording_id}-{token}.log')

    # -nostdin: never block on a tty. -progress: machine-readable stats to a file. stderr is
    # redirected to a real file below (never an undrained pipe, which would deadlock the
    # child - CLAUDE.md subprocess-discipline); -stats_period sets how often stats are written.
    full_cmd = cmd[:1] + ['-nostdin'] + cmd[1:]
    # Insert -progress/-stats_period just before the output path (last arg).
    full_cmd = full_cmd[:-1] + ['-progress', progress_path, '-stats_period', str(interval)] + full_cmd[-1:]

    started = time.monotonic()

    @retry_on_locked()
    def _mark_attempt_started():
        r = db.session.get(Recording, recording_id)
        r.conversion_started_at = datetime.utcnow()
        r.conversion_progress_pct = None
        r.conversion_eta_seconds = None
        r.conversion_out_size = None
        r.conversion_updated_at = datetime.utcnow()
        db.session.commit()

    _mark_attempt_started()

    stderr_fh = open(stderr_path, 'wb')
    proc = subprocess.Popen(full_cmd, stdin=subprocess.DEVNULL,
                            stdout=subprocess.DEVNULL, stderr=stderr_fh)
    with _active_lock:
        _active_conversions[recording_id] = proc

    smoother = EtaSmoother(expected_duration)
    growth = GrowthMonitor()
    # Latched the first time out_time advances past 0, and never cleared: the pre-output
    # budget is spent once and does not come back if ffmpeg later pauses. A pause after
    # output has started is a stall, and stall_seconds is what judges it.
    output_started = False
    last_out_time_us = 0
    stall_watching = bool(stall_seconds and stall_seconds > 0)
    # Only reached while stall detection is disabled - see the check itself below.
    fallback_deadline = started + pre_output_timeout
    long_run_noted = False
    result = None
    try:
        while True:
            finished = proc.poll() is not None
            now = time.monotonic()
            wall = now - started

            out_time_us, total_size, done = _read_progress_tail(progress_path)
            try:
                size = os.path.getsize(output_path)
            except OSError:
                size = 0
            if total_size:
                size = max(size, total_size)

            out_time = (out_time_us / 1e6) if out_time_us else 0.0
            pct = None
            if expected_duration and expected_duration > 0 and out_time > 0:
                pct = max(0.0, min(100.0, out_time / expected_duration * 100.0))
            eta = smoother.update(wall, out_time)

            _persist_conversion_snapshot(recording_id, pct, size, eta)
            ev.publish(recording_id, 'CONVERSION_PROGRESS', {
                'status': REC_STATUS_CONVERTING, 'pct': pct, 'out_size': size or None,
                'eta_seconds': eta,
            })

            if finished:
                rc = proc.returncode
                decode_errors = _count_decode_errors(stderr_path)
                if rc == 0:
                    result = ConversionResult(True, reason='success', out_time=out_time,
                                              decode_errors=decode_errors)
                else:
                    result = ConversionResult(False, reason='died',
                                              error_msg=_read_stderr_tail(stderr_path, rc),
                                              out_time=out_time, decode_errors=decode_errors)
                break

            if collision_policy == 'cancel':
                conflict = _conversion_collision_conflict(collision_window_seconds)
                if conflict is not None:
                    where = ('is in progress' if conflict.status == REC_STATUS_IN_PROGRESS
                             else 'starts soon')
                    log.info('Recording %d conversion yielding to recording "%s" (%s) - '
                             'killing, will resume once clear (collision_policy: cancel)',
                             recording_id, conflict.name, where)
                    terminate_or_kill(proc, hard=True)
                    result = ConversionResult(
                        False, reason='preempted',
                        error_msg=f'yielding local resources to recording "{conflict.name}" ({where})',
                        out_time=out_time)
                    break

            if out_time_us and out_time_us > 0:
                output_started = True
                last_out_time_us = out_time_us

            # Stall detection tracks the encoded OUTPUT timestamp (out_time), and only once
            # output has actually started. A heavily-damaged source (e.g. 79% timeline gaps)
            # makes ffmpeg seek/analyze for minutes before muxing its first frame - out_time
            # legitimately sits at 0 that whole time, which is NOT a stall (verified live on
            # recording #64: a healthy re-encode was false-killed at 301s). The pre-output
            # budget below is the backstop for a conversion that never produces output.
            # stall_seconds<=0 disables stall detection.
            if stall_watching and output_started:
                # last_out_time_us, not out_time_us: a poll can legitimately read nothing
                # back while ffmpeg truncates and rewrites its -progress file, and the
                # monitor must see the high-water mark rather than a None. Feeding the last
                # known value (not skipping the update) is deliberate - it keeps the stall
                # clock running through an unreadable stretch, so a hung ffmpeg whose
                # progress file has gone quiet is still caught rather than running forever.
                stalled_for = growth.update(last_out_time_us)
                if stalled_for >= stall_seconds:
                    log.warning('Recording %d conversion stalled for %.0fs (output stopped advancing) - killing',
                                recording_id, stalled_for)
                    terminate_or_kill(proc, hard=True)
                    result = ConversionResult(False, reason='stalled',
                                              error_msg=f'No conversion progress for {int(stalled_for)}s',
                                              out_time=out_time)
                    break

            if not output_started and wall >= pre_output_timeout:
                log.warning('Recording %d conversion produced no output in %ds - killing',
                            recording_id, pre_output_timeout)
                terminate_or_kill(proc, hard=True)
                result = ConversionResult(
                    False, reason='no_output',
                    error_msg=f'Conversion produced no output in {pre_output_timeout}s',
                    out_time=out_time)
                break

            # Once output is advancing there is no upper bound - EXCEPT when the operator
            # has turned stall detection off, which leaves nothing watching liveness at all.
            # The old wall clock stands in for it there, the same way run_probe_until_stalled
            # falls back to one when /proc offers no progress signal. Never widen this to the
            # stall_seconds>0 case: that reinstates the deadline this function exists to
            # remove.
            if output_started and not stall_watching and now >= fallback_deadline:
                log.warning('Recording %d conversion exceeded %ds with stall detection disabled '
                            '(stall_seconds: 0) - killing', recording_id, pre_output_timeout)
                terminate_or_kill(proc, hard=True)
                result = ConversionResult(
                    False, reason='timeout',
                    error_msg=f'Conversion ran {pre_output_timeout}s with stall detection disabled',
                    out_time=out_time)
                break

            # A conversion outliving what used to be its whole budget is now routine, so it
            # is said out loud once rather than left as an unexplained multi-hour gap in the
            # log - the same disclosure run_probe_until_stalled makes for a long probe.
            if output_started and stall_watching and not long_run_noted and wall >= pre_output_timeout:
                long_run_noted = True
                log.info('Recording %d conversion has run %.0fs and is still advancing '
                         '(%s done, ETA %s) - no deadline applies while it progresses',
                         recording_id, wall,
                         f'{pct:.1f}%' if pct is not None else 'unknown',
                         f'{int(eta)}s' if eta else 'unknown')

            time.sleep(interval)
    finally:
        terminate_or_kill(proc)
        stderr_fh.close()
        with _active_lock:
            _active_conversions.pop(recording_id, None)
        for p in (progress_path, stderr_path):
            try:
                os.unlink(p)
            except OSError:
                pass  # best-effort scratch cleanup

    return result


def _read_stderr_tail(stderr_path, rc):
    """Conversion-flavored wrapper over proc_utils.read_stderr_tail: this caller needs a
    non-empty string for a user-facing error message, so an empty tail becomes the exit code."""
    return read_stderr_tail(stderr_path) or f'ffmpeg exited {rc}'


# ffmpeg's own wording for a frame it could not decode. Both spellings occur: the first
# when the packet is rejected on submission, the second when the decoder accepts it and
# then fails. Counted rather than tailed because the tail holds only the last few KB and
# the interesting damage is usually thousands of lines back.
_DECODE_ERROR_MARKERS = ('Error submitting packet to decoder',
                         'Decoding error:')


# ffmpeg collapses a run of identical lines into one of these instead of repeating it, so
# a naive per-line count reports 1 where the source lost 200 frames. The number reaches the
# user, and a number that is quietly a floor is worse than no number at all.
_REPEATED_RE = re.compile(r'Last message repeated (\d+) times?')


def _count_decode_errors(stderr_path):
    """How many frames ffmpeg reported it could not decode, over the whole stderr spool.

    Conversion runs with -max_error_rate 1.0, which stops ffmpeg aborting a conversion
    over decode errors alone; without this count a file whose audio was silently patched
    over would finish looking indistinguishable from a clean one.

    A "Last message repeated N times" line is attributed to whatever it repeats, so it
    counts N more only when the line it collapsed was itself a decode error.
    """
    count = 0
    last_was_error = False
    try:
        with open(stderr_path, 'r', errors='replace') as fh:
            for line in fh:
                if any(marker in line for marker in _DECODE_ERROR_MARKERS):
                    count += 1
                    last_was_error = True
                    continue
                repeated = _REPEATED_RE.search(line)
                if repeated:
                    # Consecutive repeat lines all collapse the same original message,
                    # so last_was_error is deliberately left alone here.
                    if last_was_error:
                        count += int(repeated.group(1))
                    continue
                last_was_error = False
    except OSError:
        return 0  # best-effort: a missing spool must never fail the conversion
    return count


def collision_safe_dest(destination: str, source_path: str) -> str:
    """Where `source_path` should land inside `destination`, suffixed `_2`, `_3`, ... when
    something is already sitting on the name.

    shutil.move() silently clobbers an existing destination file (os.rename on the same
    filesystem, copy2-then-unlink across one), so without this a second recording that
    renders to the same filename - a re-record of the same program on the same day, a
    "Record again" - destroys the first one's file. Worse than losing the file: the first
    recording's row still points at that path, so the UI would show it as present and hand
    back the *second* recording's content.

    Returns `source_path` unchanged when it is already the file at the destination name, so
    a destination equal to the file's current directory is a no-op rather than a rename to
    `_2` (dev/changelog/587).
    """
    filename = os.path.basename(source_path)
    dest_path = os.path.join(destination, filename)
    if not os.path.exists(dest_path):
        return dest_path
    if os.path.samefile(dest_path, source_path):
        return source_path
    stem, ext = os.path.splitext(filename)
    n = 2
    while True:
        candidate = os.path.join(destination, f'{stem}_{n}{ext}')
        if not os.path.exists(candidate):
            return candidate
        n += 1


def output_extension_family(cfg) -> list:
    """Every extension one recording's output stem can end up occupying: the concat's own
    `.ts`, plus whatever post-processing converts it to. The `.ts` is always first - it is
    the file concat actually writes.

    Lives here rather than in the concatenator because the second entry is a fact about
    post-processing, and the concatenator should not have to know what conversion produces.
    """
    exts = ['.ts']
    pp = cfg['recording']['post_process']
    if pp.get('enabled', True):
        fmt = str(pp.get('format', 'mp4')).lower().lstrip('.')
        if fmt and f'.{fmt}' not in exts:
            exts.append(f'.{fmt}')
    return exts


def reserve_concat_output_path(directory: str, safe_name: str, extensions) -> str:
    """Claim a free output path in `directory` for a concat about to run, suffixed `_2`,
    `_3`, ... when the plain name is taken. Returns the path carrying `extensions[0]`.

    A sibling of collision_safe_dest() above rather than a caller, because it answers a
    different question in two ways that matter: it names a file that does not exist yet, and
    it judges a stem by the whole family of files that stem will occupy rather than by one
    filename.

    The family check is the load-bearing part. Conversion writes `X.<fmt>` from `X.ts` with
    ffmpeg -y and post_process.delete_source then removes the source, so under the shipped
    defaults a finished recording leaves only `X.<fmt>` behind. Checking `.ts` alone would
    therefore find `X.ts` free, hand that stem to the next same-named recording, and let its
    conversion silently overwrite the first recording's finished file - while the first
    recording's row still pointed at it. That is the same "worse than losing the file"
    outcome collision_safe_dest() exists to prevent, one step further down the pipeline
    (dev/changelog/643).

    The winning stem is staked with O_CREAT|O_EXCL so two concurrent concats cannot both
    clear the existence check and pick the same name. The zero-byte placeholder is replaced
    by the single-segment rename or overwritten by ffmpeg's own -y.
    """
    exts = list(extensions) or ['.ts']
    n = 1
    while True:
        stem = safe_name if n == 1 else f'{safe_name}_{n}'
        candidate = os.path.join(directory, f'{stem}{exts[0]}')
        if any(os.path.exists(os.path.join(directory, f'{stem}{e}')) for e in exts):
            n += 1
            continue
        try:
            os.close(os.open(candidate, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644))
        except FileExistsError:
            n += 1
            continue
        except OSError as exc:
            # Could not stake the claim (permissions, no space, a CIFS hiccup). The
            # existence check above already passed, so returning it unreserved is no worse
            # than not reserving at all, and the concat's own write reports the real error.
            log.warning('Could not reserve concat output %s: %s', candidate, exc)
        return candidate


def do_postprocess(app, recording_id: int, ts_path: str):
    """Run conversion and/or move after a successful concat.

    Always sets rec.status = 'COMPLETED' (or 'FAILED' on conversion error).
    Caller must NOT set COMPLETED before calling this.

    Runs in a background thread. Every commit is its own retry_on_locked closure
    (re-fetch → mutate → commit); subprocess/file side effects stay outside the
    closures so a lock-retry can never re-run them.
    """
    from . import db
    from .config import load_config, resolve_ffmpeg_path
    from .database import (
        Recording, RecordingEvent, add_recording_event, preserve_cancelled_status,
        DIAGNOSTICS, SEEK_DAMAGE_DETECTED, MIXED_FRAME_RATE_DETECTED,
        POSTCAPTURE_ANALYSIS_STARTED, CONVERSION_STARTED, CONVERSION_RESTARTED,
        CONVERSION_YIELDED, CONVERSION_DONE, FILE_MOVED, CONCATENATION_DONE, SCRIPT_EXECUTED,
        REC_STATUS_ANALYZING, REC_STATUS_CONVERTING, REC_STATUS_IN_PROGRESS,
        REC_STATUS_ABORTED, REC_STATUS_FAILED, REC_STATUS_COMPLETED,
    )
    from . import events as ev
    from . import alerts

    # Double-run guard: if an ffmpeg is already converting this recording, a second
    # do_postprocess (from a retry click or a startup resume racing a live one) would have
    # two ffmpegs fighting over the same output. Refuse.
    if is_conversion_active(recording_id):
        log.warning('Postprocessor: recording %d already has a live conversion - refusing duplicate run',
                    recording_id)
        return

    with app.app_context():
        cfg = load_config()
        pp = cfg['recording']['post_process']
        mv = cfg['recording']['move_on_complete']

        rec = db.session.get(Recording, recording_id)
        if rec is None:
            log.error('Postprocessor: recording %d not found', recording_id)
            return

        rec_name = rec.name
        current_path = ts_path

        # Post-capture cancel: the user can abort while this chain is still working, so
        # the chain stops at the next phase boundary instead of running to completion and
        # writing its own terminal status over the ABORTED row. Every terminal write below
        # is additionally guarded by preserve_cancelled_status, for a cancel that lands
        # after the last boundary check (dev/changelog/667).
        def _stop_if_cancelled(detail):
            if not cancelled_meanwhile(recording_id):
                return False

            @retry_on_locked()
            def _commit_cancel_note():
                r = db.session.get(Recording, recording_id)
                preserve_cancelled_status(r, detail)
                db.session.commit()

            _commit_cancel_note()
            log.info('Recording %d cancelled - %s', recording_id, detail)
            return True

        # ── Analysis phase ────────────────────────────────────────────────────
        # The row moves off CONCATENATING here, at the top, because the concat is over: its
        # output is committed and the segments it consumed are gone. Everything below this
        # line reads that finished file back, and on a large capture that is hours, not a
        # moment. Leaving the row on CONCATENATING through it is what produced an activity
        # log reading "concat complete" -> "resuming concatenation" -> "concat failed" for a
        # recording sitting whole on disk (dev/changelog/867).
        #
        # preserve_cancelled_status: a cancel can already have landed (this runs on a
        # background thread), and a status write here must never resurrect an ABORTED row.
        # Its RECORDING_ABORTED event is what explains why the chain stopped.
        @retry_on_locked()
        def _commit_analysis_started():
            r = db.session.get(Recording, recording_id)
            if preserve_cancelled_status(
                    r, 'Post-processing did not start - the recording was cancelled first.'):
                db.session.commit()
                return False
            r.status = REC_STATUS_ANALYZING
            add_recording_event(
                recording_id, POSTCAPTURE_ANALYSIS_STARTED,
                detail=f'Checking the joined file before conversion: '
                       f'{os.path.basename(ts_path)}')
            db.session.commit()
            return True

        if not _commit_analysis_started():
            log.info('Recording %d was cancelled before post-processing started', recording_id)
            return
        ev.publish(recording_id, POSTCAPTURE_ANALYSIS_STARTED, {'status': REC_STATUS_ANALYZING})

        # ── Recording health data (ffprobe on the .ts file) ───────────────────
        health_fields, health_diag = _gather_recording_health(recording_id, ts_path, rec, cfg)
        if health_fields or health_diag:
            # One closure, one commit: add_recording_event inserts without committing, so
            # the event joins the field write as a single unit of work rather than adding
            # a second commit under the same decorator (dev/changelog/332).
            @retry_on_locked()
            def _commit_health_fields():
                r = db.session.get(Recording, recording_id)
                if r is not None and health_fields:
                    for k, v in health_fields.items():
                        setattr(r, k, v)
                if health_diag:
                    add_recording_event(recording_id, DIAGNOSTICS,
                                        detail=health_diag['detail'],
                                        extra=health_diag['extra'])
                db.session.commit()

            _commit_health_fields()

        # ── Timeline scan (MEASURE only - see the re-encode branch below) ─────
        # Gated on gather_health_data alone, so the stats and the DIAGNOSTICS event exist
        # for every format and every reencode_mode, not just the one combination that
        # happens to act on the verdict. Memoized: the conversion phase reads this rather
        # than re-running a full-file ffprobe.
        timeline_scan = None
        if cfg['recording'].get('gather_health_data', True):
            timeline_scan = _scan_recording_timeline(recording_id, ts_path)
            # Near-empty/slate detection (compute-only, no extra ffprobe) and the
            # capture-quality score correction both depend on what the timeline scan just
            # committed (timeline_deficit_seconds), so they run right after it, in order.
            _detect_near_empty_segments(recording_id)
            from .health_score import apply_capture_quality_correction
            apply_capture_quality_correction(app, recording_id)

        # ── Conversion ────────────────────────────────────────────────────────
        if _stop_if_cancelled('Post-processing stopped before conversion started - '
                              'the concatenated .ts file was kept.'):
            return

        if pp.get('enabled'):
            fmt = pp.get('format', 'mp4').lower().lstrip('.')
            # Bounds only the phase before ffmpeg muxes its first frame. The same number
            # covers a remux and a re-encode because the phase it bounds is identical -
            # ffmpeg opening and analyzing the same .ts - and nothing about it scales with
            # the codec work that follows (dev/changelog/865).
            pre_output_timeout = max(1, int(pp.get('pre_output_timeout_seconds', 1800) or 1800))
            auto_restart = pp.get('auto_restart', True)
            max_attempts = max(0, int(pp.get('max_restart_attempts', 3) or 0))
            stall_seconds = max(0, int(pp.get('stall_seconds', 300) or 0))
            interval = max(1, min(60, int(pp.get('progress_interval_seconds', 5) or 5)))

            stem = os.path.splitext(ts_path)[0]
            output_path = f'{stem}.{fmt}'

            # Expected media duration, probed ONCE (health-gather above usually already set
            # recorded_duration_seconds). Drives % complete/ETA below AND the collision
            # lookahead window right after - computed here rather than nearer the restart
            # loop so both have it.
            expected_duration = rec.recorded_duration_seconds
            if not expected_duration:
                try:
                    from .probe import parse_ffprobe
                    probe = parse_ffprobe(ts_path, count_packets=False)
                    expected_duration = probe.get('duration') if probe else None
                except Exception as exc:
                    log.warning('Recording %d: could not probe duration for conversion ETA: %s',
                                recording_id, exc)
                    expected_duration = None

            # mp4 conversion vs. an imminent/active recording (recording.post_process.
            # collision_policy). Checked before the re-encode decision below - that runs its
            # own ffprobe, no sense paying for it if we're about to wait anyway.
            collision_policy = pp.get('collision_policy', 'cancel')
            collision_multiplier = float(pp.get('collision_lookahead_multiplier', 1.0))
            collision_window_seconds = _collision_window_seconds(expected_duration, collision_multiplier)
            if collision_policy != 'off':
                conflict = _conversion_collision_conflict(collision_window_seconds)
                if conflict is not None:
                    where = ('is in progress' if conflict.status == REC_STATUS_IN_PROGRESS
                             else 'starts soon')
                    log.info('Recording %d: postponing conversion start - recording "%s" %s '
                             '(collision_policy: %s)', recording_id, conflict.name, where, collision_policy)

                    @retry_on_locked()
                    def _commit_collision_yield_event(name=conflict.name, where_=where):
                        add_recording_event(
                            recording_id, CONVERSION_YIELDED,
                            detail=f'Conversion not started yet: recording "{name}" {where_} - '
                                   f'will start once local resources are free.')
                        db.session.commit()

                    _commit_collision_yield_event()
                    _wait_for_conversion_clear(recording_id, collision_window_seconds)
                    if _stop_if_cancelled('Post-processing stopped while waiting for local '
                                          'resources to free up before converting.'):
                        return

            # Re-encode decision (mp4 only). Stream drops during capture leave the .ts
            # with a gappy video timeline (missing frames, continuous audio); a straight
            # -c:v copy carries that into the mp4 and Plex FF/RW freezes when a seek
            # lands in a damaged stretch. Re-encoding at CFR fills the gaps with
            # duplicated frames and writes clean 2s IDRs, making the file fully seekable.
            # Decided ONCE here (not per restart) - the source .ts is static, and
            # assess_seek_damage runs ffprobe, which the no-hidden-I/O rule forbids looping.
            #
            # MEASURING AND ACTING ARE TWO SEPARATE CONDITIONS AND MUST STAY THAT WAY.
            # The scan above runs on gather_health_data; only this branch turns a verdict
            # into a re-encode, and it still requires mp4 + reencode_mode == 'damaged'.
            # Collapsing them back together would silently start re-encoding files for
            # users who explicitly set reencode_mode: never (dev/changelog/331).
            reencode = False
            damage_summary = None
            mixed_rate_summary = None
            reencode_mode = pp.get('reencode_mode', 'damaged')
            if fmt == 'mp4' and reencode_mode == 'always':
                reencode = True
            elif fmt == 'mp4' and reencode_mode == 'damaged':
                # Scan only if the health phase did not already do it - this branch has
                # always run the scan regardless of gather_health_data, and it still must,
                # or turning health data off would silently disable damage repair.
                if timeline_scan is None:
                    timeline_scan = _scan_recording_timeline(recording_id, ts_path)
                damaged, _metrics, summary = timeline_scan
                if damaged:
                    reencode = True
                    damage_summary = summary
                elif _metrics.get('capture_fps_values'):
                    # A capture whose frame rate changed mid-recording is not damaged, and
                    # must never be reported as damaged - that misreading is what sent a
                    # 12,949s recording through a needless re-encode over 0.41s of real
                    # damage (dev/changelog/866). But it IS a file worth re-encoding: the
                    # -fps_mode:v cfr normalization gives it one constant rate, which is
                    # what makes it seek predictably. Its own trigger and its own event, so
                    # the treatment survives with the reason stated - "one flag, one
                    # meaning" is the rule this whole defect was an instance of.
                    reencode = True
                    mixed_rate_summary = summary

            if damage_summary:
                @retry_on_locked()
                def _commit_damage_detected():
                    db.session.add(RecordingEvent(
                        recording_id=recording_id,
                        event_type=SEEK_DAMAGE_DETECTED,
                        detail=f'Timeline damage in capture: {damage_summary}. '
                               f'Converting with full video re-encode so the file seeks cleanly.',
                    ))
                    db.session.commit()

                _commit_damage_detected()
            elif mixed_rate_summary:
                @retry_on_locked()
                def _commit_mixed_rate_detected():
                    db.session.add(RecordingEvent(
                        recording_id=recording_id,
                        event_type=MIXED_FRAME_RATE_DETECTED,
                        detail=f'Capture frame rate changed mid-recording: '
                               f'{mixed_rate_summary}. The timeline is intact - converting '
                               f'with full video re-encode to normalize it to one constant '
                               f'frame rate, not to repair damage.',
                    ))
                    db.session.commit()

                _commit_mixed_rate_detected()

            mode_note = ''
            if reencode:
                if damage_summary:
                    mode_note = ' (video re-encode: damage detected)'
                elif mixed_rate_summary:
                    mode_note = ' (video re-encode: mixed capture frame rate)'
                else:
                    mode_note = ' (video re-encode: always)'

            @retry_on_locked()
            def _commit_conversion_started():
                r = db.session.get(Recording, recording_id)
                if preserve_cancelled_status(
                        r, 'Conversion not started - the recording was cancelled first.'):
                    db.session.commit()
                    return False
                r.status = REC_STATUS_CONVERTING
                db.session.add(RecordingEvent(
                    recording_id=recording_id,
                    event_type=CONVERSION_STARTED,
                    detail=f'Converting {os.path.basename(ts_path)} → '
                           f'{os.path.basename(output_path)}{mode_note}',
                ))
                db.session.commit()
                return True

            if not _commit_conversion_started():
                return
            ev.publish(recording_id, CONVERSION_STARTED, {'status': REC_STATUS_CONVERTING})

            ffmpeg_path = resolve_ffmpeg_path(cfg['ffmpeg']['path'])
            video_crf = int(pp.get('video_crf', 20) or 20)
            audio_kbps = int(pp.get('audio_bitrate_kbps', 192) or 192)
            rate = None
            if fmt == 'mp4' and reencode:
                from .probe import nominal_video_rate
                rate = nominal_video_rate(ts_path)

            def _build_cmd(audio_copy=False):
                """The conversion command, optionally with audio stream-copied instead of
                re-encoded (the fallback below).

                -max_error_rate 1.0 raises ffmpeg's default abort, which fires once 2/3 of
                the frames in its window fail to decode: a burst of garbage audio trips it
                even though the video either side is perfect. Concealed damage is counted
                and disclosed rather than left silent.

                What is deliberately NOT here is -reinit_filter:a 0. It does stop a corrupt
                frame's nonsense channel count from tearing down the resampler, but it also
                stops a LEGITIMATE mid-stream layout change (stereo -> 5.1 at a program
                boundary, routine on IPTV) from reconfiguring it - measured, that truncates
                the audio at the change and fails the conversion outright
                (dev/changelog/800). Trading a common working case for a rare broken one is
                the wrong direction; the fallback handles the rare one instead.
                """
                c = [ffmpeg_path, '-err_detect', 'ignore_err', '-fflags', '+genpts+discardcorrupt',
                     '-max_error_rate', '1.0', '-i', ts_path]
                if fmt == 'mp4' and reencode:
                    # yuv420p keeps hardware players (Roku/TV apps) happy; 2s forced IDRs give
                    # dense, guaranteed-clean seek points. CFR at the stream's nominal rate
                    # (r_frame_rate - avg is skewed by the very gaps being repaired) fills
                    # timeline holes with duplicated frames instead of leaving PTS jumps.
                    c += ['-c:v', 'libx264', '-preset', 'veryfast', '-crf', str(video_crf),
                          '-pix_fmt', 'yuv420p',
                          '-force_key_frames', 'expr:gte(t,n_forced*2)']
                    if rate:
                        c += ['-r', rate, '-fps_mode:v', 'cfr']
                elif fmt == 'mp4':
                    c += ['-c:v', 'copy']
                else:
                    return c + ['-c', 'copy', '-y', output_path]
                if audio_copy:
                    # MP4/MOV needs ADTS AAC converted to raw AAC by this bitstream filter.
                    # Copying never decodes, so no bad frame can reach a decoder or a filter
                    # graph - which is why it survives a source the re-encode cannot. The
                    # cost is that one codec config covers the whole track, so a source that
                    # really does change layout mid-stream leaves that stretch undecodable
                    # in players. Fallback only, never the default.
                    c += ['-c:a', 'copy', '-bsf:a', 'aac_adtstoasc']
                else:
                    c += ['-c:a', 'aac', '-b:a', f'{audio_kbps}k']
                return c + ['-movflags', '+faststart', '-y', output_path]

            cmd = _build_cmd()
            log.info('Conversion command: %s', ' '.join(cmd))

            # Supervised restart loop. `attempt` is the number of restarts already performed
            # (persisted across a service restart via conversion_attempts). max_restart_attempts
            # is a count of RESTARTS after the initial spawn - so 3 means up to 4 total spawns.
            attempt = rec.conversion_attempts or 0
            conversion_ok = False
            cancelled = False
            last_error = None
            decode_errors = 0
            prev_death_out_time = None
            repeated_at = None
            audio_copy_fallback = False
            _consume_cancel(recording_id)  # drop any stale flag from a prior run
            while True:
                result = run_conversion_supervised(
                    app, recording_id, cmd, output_path,
                    expected_duration=expected_duration, pre_output_timeout=pre_output_timeout,
                    interval=interval, stall_seconds=stall_seconds,
                    collision_policy=collision_policy,
                    collision_window_seconds=collision_window_seconds,
                )
                # A user cancel (request_cancel_conversion killed the ffmpeg) reads as a
                # death; the flag distinguishes it so we abort instead of restarting.
                if _consume_cancel(recording_id):
                    cancelled = True
                    break

                if result.reason == 'preempted':
                    # Not a failure - never counts against max_restart_attempts. Wait
                    # (blocking this chain's own thread) until clear, then restart from
                    # scratch; -y in cmd handles the leftover partial output.
                    log.info('Recording %d conversion paused: %s', recording_id, result.error_msg)

                    @retry_on_locked()
                    def _commit_yielded_event(msg=result.error_msg):
                        add_recording_event(recording_id, CONVERSION_YIELDED,
                                            detail=f'Conversion paused: {msg}')
                        db.session.commit()

                    _commit_yielded_event()
                    ev.publish(recording_id, CONVERSION_YIELDED, {'status': REC_STATUS_CONVERTING})
                    _wait_for_conversion_clear(recording_id, collision_window_seconds)
                    if _stop_if_cancelled('Post-processing stopped while waiting for local '
                                          'resources to free up before resuming conversion.'):
                        return
                    continue

                decode_errors = max(decode_errors, result.decode_errors or 0)

                if result.success:
                    conversion_ok = True
                    break

                last_error = result.error_msg or result.reason

                # A restart re-reads the same static .ts from byte zero, so a defect IN
                # THAT FILE stops every attempt at the same output timestamp. Recording 4
                # died four times at 02:53:02.06 with byte-identical output sizes, burning
                # ~40 minutes of CPU to fail the same way (dev/changelog/799). Retrying is
                # only ever worth it for a transient cause - a killed process, a blip on
                # the storage mount - which lands somewhere new each time. The tolerance is
                # one poll interval: two attempts that stop within a single progress sample
                # of each other are the same stop, not a coincidence.
                repeat = (result.reason == 'died' and result.out_time and prev_death_out_time
                          and abs(result.out_time - prev_death_out_time) <= max(interval, 1.0))
                if repeat and fmt == 'mp4' and not audio_copy_fallback:
                    # The source is damaged HERE, and re-encoding audio is what cannot get
                    # past it: a corrupt frame reaches the decoder, decodes to a nonsense
                    # channel layout, and the resampler rebuild that follows is fatal.
                    # Copying the audio never decodes anything, so the same bytes pass
                    # straight through - measured to salvage 59 of 60 seconds where the
                    # re-encode salvaged none (dev/changelog/800). Tried once, and only
                    # after the file itself has been shown to be the cause, because it
                    # costs a source that legitimately changes layout mid-stream.
                    audio_copy_fallback = True
                    cmd = _build_cmd(audio_copy=True)
                    # prev_death_out_time is deliberately kept: if a run that decodes
                    # nothing still stops at the same offset, nothing will get past it.
                    attempt += 1
                    log.warning('Recording %d conversion died twice at %.2fs into the source - '
                                'retrying with audio stream-copied instead of re-encoded',
                                recording_id, result.out_time)

                    @retry_on_locked()
                    def _commit_audio_fallback_event(where=result.out_time, n=attempt):
                        r = db.session.get(Recording, recording_id)
                        r.conversion_attempts = n
                        add_recording_event(
                            recording_id, CONVERSION_RESTARTED,
                            detail=f'Conversion stopped twice at the same point '
                                   f'({fmt_duration(where, with_seconds=True)} in) - the source '
                                   f'is damaged there. Retrying with the audio copied instead '
                                   f'of re-encoded, which does not decode it.')
                        db.session.commit()

                    _commit_audio_fallback_event()
                    ev.publish(recording_id, CONVERSION_RESTARTED,
                               {'status': REC_STATUS_CONVERTING, 'attempt': attempt,
                                'max': max_attempts})
                    continue
                if repeat:
                    repeated_at = result.out_time
                    log.warning('Recording %d conversion died at the same point as the previous '
                                'attempt (%.2fs into the source) - the source file is the cause, '
                                'not a transient failure; not restarting again',
                                recording_id, repeated_at)
                    break
                if result.reason == 'died':
                    prev_death_out_time = result.out_time

                give_up = (not auto_restart) or max_attempts <= 0 or attempt >= max_attempts
                if give_up:
                    break

                attempt += 1

                @retry_on_locked()
                def _commit_attempt_increment(n=attempt):
                    r = db.session.get(Recording, recording_id)
                    r.conversion_attempts = n
                    db.session.commit()

                _commit_attempt_increment()
                log.info('Recording %d conversion restarting (attempt %d of %d) after %s: %s',
                         recording_id, attempt, max_attempts, result.reason, last_error)

                @retry_on_locked()
                def _commit_restart_event(n=attempt, reason=result.reason):
                    db.session.add(RecordingEvent(
                        recording_id=recording_id,
                        event_type=CONVERSION_RESTARTED,
                        detail=f'Conversion {_RESTART_REASON_PHRASE.get(reason, reason)}; '
                               f'restarting (attempt {n} of {max_attempts}).',
                    ))
                    db.session.commit()

                _commit_restart_event()
                ev.publish(recording_id, CONVERSION_RESTARTED,
                           {'status': REC_STATUS_CONVERTING, 'attempt': attempt, 'max': max_attempts})

            if cancelled:
                # User cancelled: keep the source .ts (Retry conversion works from CANCELLED),
                # delete the partial .mp4 (killed mid-write, no moov atom - unreadable garbage).
                if output_path != ts_path and os.path.exists(output_path):
                    try:
                        os.unlink(output_path)
                    except OSError as exc:
                        log.warning('Could not delete partial conversion output %s: %s', output_path, exc)

                @retry_on_locked()
                def _commit_conversion_cancelled():
                    r = db.session.get(Recording, recording_id)
                    r.status = REC_STATUS_ABORTED
                    r.completed_at = datetime.utcnow()
                    db.session.add(RecordingEvent(
                        recording_id=recording_id,
                        event_type=CONVERSION_DONE,
                        detail='Conversion cancelled by user - source .ts kept for retry.',
                    ))
                    db.session.commit()

                _commit_conversion_cancelled()
                ev.publish(recording_id, CONVERSION_DONE,
                           {'success': False, 'cancelled': True, 'status': REC_STATUS_ABORTED})
                log.info('Recording %d conversion cancelled by user', recording_id)
                return

            if conversion_ok:
                converted_size = os.path.getsize(output_path) if os.path.exists(output_path) else 0

                @retry_on_locked()
                def _commit_conversion_done():
                    r = db.session.get(Recording, recording_id)
                    r.output_path = output_path
                    r.final_file_size = converted_size
                    db.session.add(RecordingEvent(
                        recording_id=recording_id,
                        event_type=CONVERSION_DONE,
                        detail=f'Conversion complete: {os.path.basename(output_path)} ({_fmt_bytes(converted_size)})',
                    ))
                    db.session.commit()

                _commit_conversion_done()
                log.info('Recording %d conversion complete: %s', recording_id, output_path)

                if audio_copy_fallback:
                    # The file only exists because the audio was copied rather than
                    # re-encoded, and that has its own cost worth naming: one codec config
                    # covers the whole track, so any stretch where the source genuinely
                    # changed audio layout will not decode in a player.
                    @retry_on_locked()
                    def _commit_audio_copy_note():
                        add_recording_event(
                            recording_id, DIAGNOSTICS,
                            detail='Audio was copied rather than re-encoded, because the '
                                   'source was damaged in a way the decoder could not get '
                                   'past. If the source changed audio format mid-recording, '
                                   'that stretch may not play.',
                            extra={'kind': 'conversion_audio_copy_fallback'},
                        )
                        db.session.commit()

                    _commit_audio_copy_note()

                if decode_errors:
                    # The conversion succeeded only because -max_error_rate let it run past
                    # frames it could not decode. Those frames are missing from the output,
                    # so the file is not the clean success its size and status suggest.
                    @retry_on_locked()
                    def _commit_decode_errors_event(n=decode_errors):
                        add_recording_event(
                            recording_id, DIAGNOSTICS,
                            detail=f'Conversion concealed damage: {n} frame(s) in the source '
                                   f'could not be decoded and were dropped from the output.',
                            extra={'kind': 'conversion_decode_errors', 'decode_errors': n},
                        )
                        db.session.commit()

                    _commit_decode_errors_event()
                    log.warning('Recording %d conversion dropped %d undecodable frame(s)',
                                recording_id, decode_errors)

                if pp.get('delete_source', True) and ts_path != output_path:
                    try:
                        os.unlink(ts_path)
                    except Exception as exc:
                        log.warning('Could not delete source .ts file %s: %s', ts_path, exc)

                current_path = output_path
            else:
                # Give-up: keep the source .ts so a manual Retry still works. The counter is
                # left at its final value (an operator Retry resets it). attempt = restarts done.
                if repeated_at is not None:
                    where = fmt_duration(repeated_at, with_seconds=True)
                    give_up_msg = (f'FAILED after {attempt + 1} attempt(s) - every attempt '
                                   f'stopped at the same point ({where} into the recording), '
                                   f'so the source file is damaged there and restarting cannot '
                                   f'get past it (gave up: {last_error})')
                else:
                    give_up_msg = (f'FAILED after {attempt + 1} attempt(s) '
                                   f'(gave up: {last_error})')

                @retry_on_locked()
                def _commit_conversion_failed():
                    r = db.session.get(Recording, recording_id)
                    if preserve_cancelled_status(
                            r, f'Conversion gave up after the recording was cancelled '
                               f'- status left ABORTED. {give_up_msg}'):
                        db.session.commit()
                        return False
                    r.status = REC_STATUS_FAILED
                    r.completed_at = datetime.utcnow()
                    db.session.add(RecordingEvent(
                        recording_id=recording_id,
                        event_type=CONVERSION_DONE,
                        detail=give_up_msg,
                    ))
                    db.session.commit()
                    return True

                if _commit_conversion_failed():
                    ev.publish(recording_id, CONVERSION_DONE,
                               {'success': False, 'error': last_error, 'status': REC_STATUS_FAILED})
                    alerts.create_alert(
                        'CONVERSION_FAILED',
                        f'Conversion failed: {rec_name}',
                        body=give_up_msg,
                        source='postprocessor',
                        recording_id=recording_id,
                    )
                    log.error('Recording "%s" (#%d) conversion failed: %s', rec_name, recording_id, give_up_msg,
                              extra={'recording_id': recording_id})
                else:
                    # Cancelled while this attempt was giving up: the row stays ABORTED, so
                    # neither the FAILED alert nor the FAILED SSE frame is honest here.
                    ev.publish(recording_id, CONVERSION_DONE,
                               {'success': False, 'cancelled': True, 'status': REC_STATUS_ABORTED})
                return

        # ── Move ──────────────────────────────────────────────────────────────
        if _stop_if_cancelled('Post-processing stopped after the conversion step - the file '
                              'was not moved and the recording was not marked complete.'):
            return

        if mv.get('enabled') and mv.get('destination', '').strip():
            destination = mv['destination'].strip()
            try:
                # Not a bare makedirs(exist_ok=True): on a stale mount root mkdir returns
                # EEXIST and makedirs' own isdir() recheck swallows the ESTALE, so it
                # re-raises "[Errno 17] File exists" and the FILE_MOVED event below sends
                # the reader hunting a filename collision that does not exist.
                ensure_dir(destination)
                dest_path = collision_safe_dest(destination, current_path)
                if dest_path != current_path:
                    shutil.move(current_path, dest_path)
            except Exception as exc:
                log.error('Recording "%s" (#%d) move failed: %s', rec_name, recording_id, exc,
                          extra={'recording_id': recording_id})
                # Plain local, not `exc` itself - Python deletes `exc` when the except
                # block exits, so a closure must not capture it directly.
                move_error = str(exc)

                @retry_on_locked()
                def _commit_move_failed():
                    db.session.add(RecordingEvent(
                        recording_id=recording_id,
                        event_type=FILE_MOVED,
                        detail=f'Move FAILED: {move_error} - file remains at {current_path}',
                    ))
                    db.session.commit()

                _commit_move_failed()
            else:
                # Name the rename when one happened - a file that quietly landed under a
                # different name than the recording is called is exactly the kind of thing
                # the user has to be able to explain later.
                if os.path.basename(dest_path) != os.path.basename(current_path):
                    move_detail = (f'Moved to {dest_path} - renamed, '
                                   f'{os.path.basename(current_path)} already existed there')
                elif dest_path == current_path:
                    move_detail = f'Already at the destination: {dest_path}'
                else:
                    move_detail = f'Moved to {dest_path}'

                @retry_on_locked()
                def _commit_moved():
                    r = db.session.get(Recording, recording_id)
                    r.output_path = dest_path
                    db.session.add(RecordingEvent(
                        recording_id=recording_id,
                        event_type=FILE_MOVED,
                        detail=move_detail,
                    ))
                    db.session.commit()

                _commit_moved()
                log.info('Recording %d: %s', recording_id, move_detail)
                current_path = dest_path

        # ── Post-completion script ────────────────────────────────────────────
        ps = cfg['recording'].get('post_script', {})
        if ps.get('enabled') and ps.get('path', '').strip():
            script_path = ps['path'].strip()
            script_timeout = ps.get('timeout_seconds', 300)
            script_cwd = os.path.dirname(os.path.abspath(script_path)) or None
            try:
                script_result = subprocess.run(
                    [script_path],
                    cwd=script_cwd,
                    capture_output=True,
                    timeout=script_timeout,
                )
                if script_result.returncode == 0:
                    stdout = (script_result.stdout.decode(errors='replace') or '').strip()
                    detail = f'Script succeeded: {script_path}'
                    if stdout:
                        detail += f' - {stdout[:200]}'
                else:
                    stderr = script_result.stderr.decode(errors='replace').strip()[-400:]
                    detail = f'Script failed (exit {script_result.returncode}): {script_path} - {stderr}'
                log.info('Recording %d post-script result: %s', recording_id, detail[:120])
            except subprocess.TimeoutExpired:
                detail = f'Script timed out after {script_timeout}s: {script_path}'
                log.error('Recording "%s" (#%d) %s', rec_name, recording_id, detail,
                          extra={'recording_id': recording_id})
            except Exception as exc:
                detail = f'Script error: {script_path} - {exc}'
                log.error('Recording "%s" (#%d) %s', rec_name, recording_id, detail,
                          extra={'recording_id': recording_id})

            @retry_on_locked()
            def _commit_script_event():
                db.session.add(RecordingEvent(
                    recording_id=recording_id,
                    event_type=SCRIPT_EXECUTED,
                    detail=detail,
                ))
                db.session.commit()

            _commit_script_event()

        # ── Complete ──────────────────────────────────────────────────────────
        @retry_on_locked()
        def _commit_completed():
            r = db.session.get(Recording, recording_id)
            if preserve_cancelled_status(
                    r, 'Post-processing finished after the recording was cancelled - status '
                       'left ABORTED rather than COMPLETED.'):
                db.session.commit()
                return None
            r.status = REC_STATUS_COMPLETED
            r.completed_at = datetime.utcnow()
            if not r.output_path:
                r.output_path = current_path
            db.session.commit()
            return r.output_path, r.final_file_size

        completed = _commit_completed()
        if completed is None:
            log.info('Recording %d was cancelled while post-processing ran - left ABORTED', recording_id)
            return
        final_output_path, final_size = completed

        # Normally a no-op: the concat step already emitted this recording's capture-phase
        # observation. Kept as the backstop for a row whose concat ran before changelog 279
        # shipped and is only now being retried through here.
        from .health_score import apply_capture_phase_health_observation
        apply_capture_phase_health_observation(app, recording_id)

        ev.publish(recording_id, CONCATENATION_DONE, {
            'success': True,
            'output_path': final_output_path,
            'final_size': final_size,
            'status': REC_STATUS_COMPLETED,
        })
        log.info('Recording %d post-processing complete: %s', recording_id, final_output_path)


def _joined_segment_count(recording_id):
    """How many capture segments were fed to the concat that produced this recording's .ts.

    Counted from rows that recorded bytes, which is the same test concatenator.py applies
    when it picks its valid_segments (file present and non-empty) - and rows are all that
    survives, because a successful concat deletes the segment files. Above 1 means the
    timestamps were regenerated at every join, which is what blinds the timeline scan's
    gap count (dev/changelog/433).

    Under-counting is the safe direction: it claims less blindness than there is, rather
    than explaining away a gap count that was in fact honest.
    """
    from .database import RecordingSegment
    return RecordingSegment.query.filter(
        RecordingSegment.recording_id == recording_id,
        RecordingSegment.bytes_recorded > 0,
    ).count()


def _segment_capture_rates(recording_id):
    """[(duration_seconds, probe_fps), ...] for the segments that were joined - what
    probe.assess_seek_damage() needs to measure the frame deficit against the rate the
    footage was actually captured at rather than the one in the concatenated file's header.

    Same row test as _joined_segment_count (bytes recorded), plus the two fields the weight
    needs. A segment with no probe_fps contributes nothing: the watchdog probes a segment
    that is still growing and leaves a field it could not read NULL rather than guessing, and
    inventing a rate for it would put the corrected number back where the wrong one was.

    Wall clock, not content duration - there is no per-segment content-duration column, so
    this is an approximation, and worth being precise about which way it errs. A segment
    whose wall clock exceeds the content it produced pulls the weighted rate toward its own
    rate more than it should. On a LOWER-rate segment that drags the effective rate down and
    shrinks the deficit, which could in principle mask loss. That is acceptable here because
    it is not the deficit's job to catch gross loss: a dropout inside a segment is measured
    by gap_seconds, which is rate-independent, and loss BETWEEN segments is invisible to this
    scan either way and is reported as Content missing (dev/changelog/433). What the deficit
    uniquely catches is micro-gaps under the 0.25s gap threshold, and those barely move a
    segment's wall-clock-to-content ratio at all. Weighting by wall clock is wrong by a
    start-up second or two per segment; using one rate for the whole file was wrong by 1,200
    seconds on the recording that prompted this (dev/changelog/866).
    """
    from . import db
    from .database import RecordingSegment
    rows = db.session.query(
        RecordingSegment.started_at, RecordingSegment.ended_at, RecordingSegment.probe_fps
    ).filter(
        RecordingSegment.recording_id == recording_id,
        RecordingSegment.bytes_recorded > 0,
    ).all()
    rates = []
    for started_at, ended_at, probe_fps in rows:
        if not started_at or not ended_at or not probe_fps:
            continue
        span = (ended_at - started_at).total_seconds()
        if span > 0:
            rates.append((span, probe_fps))
    return rates


def _scan_recording_timeline(recording_id, ts_path):
    """Scan the concatenated .ts for timeline damage, persist what was measured, and
    return assess_seek_damage()'s (damaged, metrics, summary).

    Emits a DIAGNOSTICS event on BOTH verdicts - a healthy recording saying "checked, 0
    gaps" is the point (dev/changelog/331); silence used to mean either "clean" or "never
    scanned" with no way to tell them apart. SEEK_DAMAGE_DETECTED is unaffected and still
    fires only on damage.

    Called at most once per post-process: assess_seek_damage runs a full-file ffprobe
    (measured 14s for a 3.0 GB file over the CIFS mount), so the caller memoizes the result
    rather than re-scanning for the re-encode decision.
    """
    from . import db
    from .database import Recording, DIAGNOSTICS, add_recording_event
    from .probe import assess_seek_damage

    if not ts_path or not os.path.exists(ts_path):
        log.warning('Recording %d: timeline scan skipped, no file at %s', recording_id, ts_path)
        return False, {}, None

    # ffprobe is a non-idempotent side effect and stays OUTSIDE the retry closure below.
    joined = _joined_segment_count(recording_id)
    damaged, metrics, summary = assess_seek_damage(
        ts_path, joined_segments=joined, segment_rates=_segment_capture_rates(recording_id))
    log.info('Recording %d seek-damage scan: %s', recording_id, summary)

    # Columns get the five stats worth sorting/filtering/displaying on; extra_data carries the
    # rest. span_seconds/packet_count/fps are deliberately extra_data-only - they duplicate
    # recorded_duration_seconds/recorded_frame_count/recorded_fps on the same row, and one
    # fact must not have two sources of truth.
    # joined_segments has no column and is not rec.segments|length: that counts every row,
    # including ones that never wrote a byte. This is the number of segments actually joined,
    # and it is what makes gap_basis 'dts-post-concat' readable after the fact.
    extra = {'kind': 'timeline_scan', 'joined_segments': joined}
    if metrics:
        extra.update({
            'gap_basis':       metrics['gap_basis'],
            'gap_threshold':   metrics['gap_threshold'],
            'backward_count':  metrics['backward_count'],
            'missing_seconds': round(metrics['missing_seconds'], 2),
            'span_seconds':    round(metrics['span_seconds'], 2),
            'packet_count':    metrics['packet_count'],
            'fps':             round(metrics['fps'], 3) if metrics['fps'] else metrics['fps'],
        })
        # Only present when the capture rate actually changed, and then both are needed to
        # reproduce deficit_seconds: which rates ran, and which single rate it was divided
        # by. Omitted entirely on the ordinary case rather than written as a null, so their
        # presence is itself the signal that this recording was rate-corrected.
        if metrics.get('capture_fps_values'):
            extra['capture_fps_values'] = metrics['capture_fps_values']
            extra['deficit_fps'] = round(metrics['deficit_fps'], 3)
    else:
        # Observable failure path: the columns stay NULL, so the event has to say why or the
        # recording renders as "never scanned" with no explanation.
        extra['scan_failed'] = True

    @retry_on_locked()
    def _commit_timeline_diagnostics():
        r = db.session.get(Recording, recording_id)
        if r is not None and metrics:
            r.timeline_gap_count       = metrics['gap_count']
            r.timeline_gap_seconds     = metrics['gap_seconds']
            r.timeline_max_gap_seconds = metrics['max_gap_seconds']
            r.timeline_deficit_seconds = metrics['deficit_seconds']
            r.timeline_damaged         = damaged
        add_recording_event(recording_id, DIAGNOSTICS,
                            detail=f'Timeline scan: {summary}', extra=extra)
        db.session.commit()

    _commit_timeline_diagnostics()
    return damaged, metrics, summary


def _near_empty_flags(spans, ratio_threshold, min_span_seconds):
    """Pure: given [(segment_number, span_seconds, bytes_per_sec), ...], return
    (flagged_segment_numbers set, recording_avg_bytes_per_sec). No I/O - split out of
    _detect_near_empty_segments so the threshold math is unit-testable without a DB.

    A segment is near-empty when its own bytes/sec sits below `ratio_threshold` of the
    recording's own average AND its span clears `min_span_seconds` (avoids flagging
    tiny/noisy segments). Calibrated against recording #64's real segments: the two known
    blank/slate segments measure ~10.6%/12.1% of the recording's average bitrate vs. ~104%
    for the two normal segments - a 20% threshold has a wide margin on both sides.
    """
    total_bytes = sum(bps * span for _n, span, bps in spans)
    total_span = sum(span for _n, span, _bps in spans)
    if total_span <= 0:
        return set(), 0.0
    avg_bps = total_bytes / total_span
    flagged = {
        seg_number for seg_number, span, bps in spans
        if span >= min_span_seconds and avg_bps > 0 and bps < ratio_threshold * avg_bps
    }
    return flagged, avg_bps


def _detect_near_empty_segments(recording_id):
    """Flag segments whose bytes-per-second sits far below the recording's own average -
    a timeline-clean segment (frames present, evenly spaced, nothing for a re-encode to
    repair) that is nonetheless a blank/slate/error-card screen. Persists per-segment flags
    plus a Recording-level rollup, and emits a DIAGNOSTICS event on every verdict (mirrors
    _scan_recording_timeline's shape - a clean verdict is itself a real answer, not silence).

    Pure compute - no ffprobe, no I/O beyond the DB read/write already happening in this
    function's caller. bytes_recorded/started_at/ended_at are already stored per segment;
    this is a compute-only addition. The actual threshold math is _near_empty_flags() above.
    """
    from . import db
    from .config import load_config
    from .database import Recording, RecordingSegment, DIAGNOSTICS, add_recording_event

    cfg = load_config()
    rs_cfg = cfg.get('channel_testing', {}).get('recording_score', {})
    ratio_threshold = rs_cfg.get('near_empty_bitrate_ratio', 0.20)
    min_span = rs_cfg.get('near_empty_min_span_seconds', 30)

    # Read-only pass: pure numbers only (segment_number -> span/bytes-per-sec), never an ORM
    # attribute mutation, so this whole block can safely sit outside the retry closure below
    # (CLAUDE.md: a rolled-back session expires pending attribute changes on retry, so the
    # mutate step must happen on a fresh fetch inside the decorated closure, not out here).
    rows = db.session.query(
        RecordingSegment.segment_number, RecordingSegment.bytes_recorded,
        RecordingSegment.started_at, RecordingSegment.ended_at
    ).filter_by(recording_id=recording_id).all()

    spans = []  # (segment_number, span_seconds, bytes_per_sec)
    for seg_number, bytes_recorded, started_at, ended_at in rows:
        if not bytes_recorded or not started_at or not ended_at:
            continue
        span = (ended_at - started_at).total_seconds()
        if span <= 0:
            continue
        spans.append((seg_number, span, bytes_recorded / span))

    if not spans:
        @retry_on_locked()
        def _commit_no_spans_event():
            add_recording_event(recording_id, DIAGNOSTICS,
                                 detail='Near-empty segment scan: no data-bearing segments to evaluate',
                                 extra={'kind': 'near_empty_scan', 'scan_failed': True})
            db.session.commit()

        _commit_no_spans_event()
        return

    flagged_numbers, avg_bps = _near_empty_flags(spans, ratio_threshold, min_span)
    flagged_count = len(flagged_numbers)
    flagged_seconds = sum(span for seg_number, span, _bps in spans if seg_number in flagged_numbers)
    summary = (f'{flagged_count} segment(s) near-empty/slate, {flagged_seconds:.0f}s total'
               if flagged_count else 'no near-empty segments detected')

    @retry_on_locked()
    def _commit_near_empty():
        for seg in RecordingSegment.query.filter_by(recording_id=recording_id).all():
            seg.near_empty = seg.segment_number in flagged_numbers
        r = db.session.get(Recording, recording_id)
        if r is not None:
            r.near_empty_segment_count = flagged_count
            r.near_empty_seconds = flagged_seconds
        add_recording_event(recording_id, DIAGNOSTICS, detail=f'Near-empty segment scan: {summary}',
                             extra={'kind': 'near_empty_scan', 'avg_bitrate_bps': round(avg_bps),
                                    'threshold_ratio': ratio_threshold,
                                    'flagged_segments': sorted(flagged_numbers)})
        db.session.commit()

    _commit_near_empty()


def _format_profile_summary(fields):
    """One human clause describing the format profile in a _gather_recording_health fields
    dict. Pure; every column it reads is nullable and an unknown one is omitted rather than
    rendered as a guess, so a probe that returned only a codec still says something true.

    Says "output format" on purpose: these columns describe the final/converted file, which
    a re-encode can make differ from the original capture (dev/changelog/335).
    """
    parts = []
    if fields.get('recorded_video_codec'):
        parts.append(fields['recorded_video_codec'])
    if fields.get('recorded_pix_fmt'):
        parts.append(fields['recorded_pix_fmt'])
    if fields.get('recorded_bit_depth'):
        parts.append(f"{fields['recorded_bit_depth']}-bit")
    if fields.get('recorded_chroma_subsampling'):
        parts.append(fields['recorded_chroma_subsampling'])
    if fields.get('recorded_interlaced') is not None:
        parts.append('interlaced' if fields['recorded_interlaced'] else 'progressive')
    if fields.get('recorded_is_vfr') is not None:
        parts.append('VFR' if fields['recorded_is_vfr'] else 'CFR')
    if fields.get('recorded_coded_resolution'):
        parts.append(f"coded {fields['recorded_coded_resolution']}")
    if fields.get('recorded_bits_per_pixel_frame'):
        parts.append(f"{fields['recorded_bits_per_pixel_frame']} bits/pixel/frame")
    return f"output format: {' '.join(parts) if parts else 'unknown'}"


def _gather_recording_health(recording_id, ts_path, rec, cfg):
    """Run ffprobe on the completed .ts and return (fields, diagnostics): a dict of
    Recording field values to persist (or None), and a {'detail', 'extra'} payload for
    one DIAGNOSTICS event (or None).

    Reads rec only; the caller writes the fields and inserts the event inside a single
    retry_on_locked closure, so a lock-retry rollback can't silently drop either.

    Every number in the summary already has a column, so extra_data carries only what
    nothing on the row records - expected frame count and the two window durations. The
    headline values live in the detail string instead; duplicating a column into
    extra_data would give one fact two sources of truth (dev/changelog/332).
    """
    if not cfg['recording'].get('gather_health_data', True):
        return None, None
    if not ts_path or not os.path.exists(ts_path):
        return None, None
    try:
        from .probe import parse_ffprobe
        probe = parse_ffprobe(ts_path)
        if not probe:
            log.warning('Recording %d: ffprobe returned no data for %s', recording_id, ts_path)
            return None, {
                'detail': 'Capture health check failed: ffprobe returned no data for the '
                          'recorded file.',
                'extra': {'kind': 'capture_health', 'probe_failed': True},
            }

        fields = {
            'recorded_resolution':       probe.get('resolution'),
            'recorded_fps':              probe.get('fps'),
            'recorded_frame_count':      probe.get('frame_count'),
            'recorded_duration_seconds': probe.get('duration'),
            'recorded_audio_codec':      probe.get('audio_codec'),
            'recorded_audio_channels':   probe.get('audio_channels'),
            'health_gathered_at':        datetime.utcnow(),

            # Format profile of the FINAL/converted output file - a re-encode changes codec
            # and pixel format, so this is not necessarily what the provider sent;
            # RecordingSegment.probe_* holds the original capture (dev/changelog/335).
            'recorded_video_codec':        probe.get('video_codec'),
            'recorded_pix_fmt':            probe.get('pix_fmt'),
            'recorded_bit_depth':          probe.get('bit_depth'),
            'recorded_chroma_subsampling': probe.get('chroma_subsampling'),
            'recorded_interlaced':         probe.get('interlaced'),
            'recorded_coded_resolution':   probe.get('coded_resolution'),
            'recorded_is_vfr':             probe.get('is_vfr'),
            'recorded_audio_sample_rate':  probe.get('audio_sample_rate'),
            'recorded_audio_bitrate_kbps': probe.get('audio_bitrate_kbps'),
            'recorded_audio_language':     probe.get('audio_language'),
        }

        bitrate_bps = probe.get('bitrate_bps')
        if bitrate_bps:
            fields['recorded_bitrate_kbps'] = bitrate_bps / 1000

        # Efficiency stat, not a better/worse verdict (DESIGN-stream-quality-profile.md §5).
        # Pure and None-safe, so a missing input leaves the column NULL without disturbing
        # anything already gathered. Segments get no bpp: the watchdog probes a file that is
        # still growing, where the format-level bit_rate is unreliable.
        from .probe import bits_per_pixel_frame
        fields['recorded_bits_per_pixel_frame'] = bits_per_pixel_frame(
            bitrate_bps, probe.get('vid_width'), probe.get('vid_height'), probe.get('fps'))

        # Use adjusted window (start_time/stop_time may have been updated at actual start/stop)
        adjusted_secs = (rec.stop_time - rec.start_time).total_seconds()
        fps = fields['recorded_fps']
        frame_count = fields['recorded_frame_count']
        if fps and adjusted_secs > 0 and frame_count:
            expected = fps * adjusted_secs
            if expected > 0:
                fields['recorded_frame_pct'] = round(frame_count / expected * 100, 1)

        sched_secs = rec.scheduled_duration_seconds if rec.scheduled_start_time else adjusted_secs
        expected_frames = int(fps * adjusted_secs) if fps and adjusted_secs > 0 else None

        # One string for the log line and the event detail - they say the same thing, and
        # this is a display surface now, so each duration names which of the four it is
        # (content, adjusted window, scheduled - requested is not shown here).
        pct = fields.get('recorded_frame_pct')
        kbps = fields.get('recorded_bitrate_kbps')
        duration = fields.get('recorded_duration_seconds')
        # The headline number this whole string exists to make unmissable: how much
        # content the window did not get. Same quantity as
        # Recording.content_shortfall_seconds and computed off the same two values -
        # it rides in the detail string, never in extra_data, because both inputs
        # already have columns (CLAUDE.md §Measurements).
        missing = None if duration is None else max(0.0, adjusted_secs - duration)
        missing_txt = (
            '' if missing is None else
            f", missing {missing:.0f}s"
            f"{'' if adjusted_secs <= 0 else f' ({missing / adjusted_secs * 100:.0f}%)'}"
        )
        summary = (
            f"{fields['recorded_resolution'] or '?'} @ "
            f"{f'{fps:.2f}' if fps else '?'} fps; "
            f"{frame_count if frame_count is not None else '?'} of "
            f"{expected_frames if expected_frames is not None else '?'} expected frames"
            f"{'' if pct is None else f' ({pct}%)'}; "
            f"{f'{kbps:.0f}' if kbps else '?'} kbps; "
            f"content duration {f'{duration:.1f}' if duration is not None else '?'}s "
            f"(adjusted window {adjusted_secs:.0f}s, scheduled {sched_secs:.0f}s"
            f"{missing_txt}); "
            f"{_format_profile_summary(fields)}"
        )
        log.info('Recording %d health: %s', recording_id, summary)

        diagnostics = {
            'detail': f'Capture health: {summary}',
            'extra': {
                'kind': 'capture_health',
                'expected_frame_count': expected_frames,
                'adjusted_window_seconds': round(adjusted_secs, 1),
                'scheduled_duration_seconds': round(sched_secs, 1),
            },
        }
        return fields, diagnostics
    except Exception as exc:
        log.warning('Recording %d: ffprobe health gather failed: %s', recording_id, exc)
        return None, {
            'detail': f'Capture health check failed: {exc}',
            'extra': {'kind': 'capture_health', 'probe_failed': True},
        }
