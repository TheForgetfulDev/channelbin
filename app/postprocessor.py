"""Post-processing after concatenation: health data, format conversion and/or file move."""
import logging
import os
import re
import shutil
import subprocess
import threading
import time
from contextlib import contextmanager
from datetime import datetime

from .db_utils import retry_on_locked
from .fmt_utils import fmt_bytes as _fmt_bytes, fmt_duration
from .fs_utils import ensure_dir
from .proc_utils import supervise_ffmpeg, terminate_or_kill

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


# ── Live post-capture analysis progress ───────────────────────────────────────
# recording_id -> which whole-file read is running and how far through it is, for the
# surfaces that report the ANALYZING phase while it runs. Same shape and the same reasoning
# as concatenator._concat_progress (dev/changelog/959): in memory rather than on the row,
# because an interrupted analysis re-runs from the top rather than resuming, so a stored
# number could only ever describe an attempt that no longer exists.
#
# Absent means "no pass is reading right now", which is a real state with its own wording
# and not a 0%: an ANALYZING row can be parked behind another recording, or between passes.
_analysis_progress: dict = {}
_analysis_progress_lock = threading.Lock()

# The passes are named for what they answer, not for the function that runs them - this is
# the text a user reads while waiting on one.
ANALYSIS_PASS_HEALTH = 'capture health'
ANALYSIS_PASS_TIMELINE = 'damage scan'


def analysis_pass_plan(cfg, pp) -> list:
    """The whole-file reads this recording's ANALYZING phase will actually run, in order.

    "Pass 1 of 2" is a promise, so the denominator is derived from the same two settings the
    phase branches on rather than hardcoded: with gather_health_data off there is no health
    pass, and the damage scan then runs only because the re-encode decision needs it - which
    requires mp4 output and reencode_mode 'damaged' (do_postprocess's own conditions, kept
    in step with them). A plan of zero passes is the honest answer for a configuration whose
    analysis reads nothing, and the surfaces show no progress rather than an empty bar.
    """
    if cfg['recording'].get('gather_health_data', True):
        return [ANALYSIS_PASS_HEALTH, ANALYSIS_PASS_TIMELINE]
    if (pp.get('enabled') and pp.get('format', 'mp4').lower().lstrip('.') == 'mp4'
            and pp.get('reencode_mode', 'damaged') == 'damaged'):
        return [ANALYSIS_PASS_TIMELINE]
    return []


def _start_analysis_pass(recording_id: int, label: str, plan: list, total_bytes: int):
    """Seed (or re-seed) the entry as a pass begins, so the strip names the pass from the
    moment it starts rather than staying blank until the first sample arrives.

    Every pass reads the same joined file, so one size serves all of them; percent is per
    pass, which is what makes "pass 2 of 2, 43%" mean something a reader can act on - a
    blended figure across passes of unequal cost would move at a rate nothing explains.
    """
    number = (plan.index(label) + 1) if label in plan else 1
    with _analysis_progress_lock:
        _analysis_progress[recording_id] = {
            'pass_label': label, 'pass_number': number, 'of_passes': max(len(plan), number),
            'pct': None, 'bytes': 0, 'total_bytes': total_bytes,
            'started': time.monotonic(),
        }


def _publish_analysis_progress(recording_id: int, read_bytes: int):
    """One sample. Clamped at 99 because the counter is bytes the process has read, not
    bytes of the file: ffprobe seeks and re-reads, so it can pass the file's size before it
    is done, and a bar that sits at 100% through the rest of a pass is the thing this
    replaced."""
    with _analysis_progress_lock:
        entry = _analysis_progress.get(recording_id)
        if entry is None:
            return
        total = entry['total_bytes']
        entry['bytes'] = read_bytes
        entry['pct'] = min(99.0, read_bytes / total * 100.0) if total > 0 else None


def _clear_analysis_progress(recording_id: int):
    with _analysis_progress_lock:
        _analysis_progress.pop(recording_id, None)


def analysis_progress(recording_id: int):
    """Which analysis pass is reading this recording's file and how far it has got, or None
    when none is.

    A plain dict lookup with no I/O of its own, so the recordings list can call it per row
    (CLAUDE.md - no hidden I/O in per-row loops). Elapsed is derived here rather than
    stored, so it is current at the moment it is read.
    """
    with _analysis_progress_lock:
        entry = _analysis_progress.get(recording_id)
        if entry is None:
            return None
        out = {k: v for k, v in entry.items() if not k.startswith('_')}
    out['elapsed_seconds'] = max(0.0, time.monotonic() - out.pop('started'))
    return out


@contextmanager
def _analysis_pass(recording_id: int, label: str, ts_path: str, plan):
    """Publish one whole-file analysis read for as long as it is running, and yield the hook
    that feeds it.

    The entry is cleared in a `finally`, so a pass that raises leaves no progress behind
    claiming to be running - and because each pass owns its own entry, the gap between two
    passes reports as "nothing reading" rather than as a stalled percentage.

    plan falsy (a caller outside the ANALYZING phase, or a configuration whose analysis
    reads nothing) publishes nothing at all and yields None, which every probe treats as
    "no progress hook".
    """
    if not plan:
        yield None
        return
    try:
        total = os.path.getsize(ts_path)
    except OSError:
        # Only the denominator is lost: the pass still runs and still names itself, and
        # _publish_analysis_progress reports pct None rather than dividing by zero.
        total = 0
    _start_analysis_pass(recording_id, label, plan, total)
    try:
        yield lambda read_bytes: _publish_analysis_progress(recording_id, read_bytes)
    finally:
        _clear_analysis_progress(recording_id)


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


def _collision_window_seconds(remaining_duration, multiplier) -> float:
    """Lookahead window: how much source is still LEFT to encode, divided by the configured
    'runs at Nx realtime' assumption (recording.post_process.collision_lookahead_multiplier).
    0 when that is unknown or already exhausted - degrades to reacting only to a recording
    that is already IN_PROGRESS or already overdue, never a future one. The only place the
    >= 0.1 floor is enforced - None means "not set" (falls back to 1.0); anything else,
    including 0 or negative, is clamped up rather than silently swapped for the default.
    Pure - no I/O, and the remaining duration is passed in rather than read here.

    The caller decides what "remaining" means, and the two callers differ: the pre-start
    check has no progress to read and passes the whole duration, while the in-run check
    passes what is left. A window that never shrinks is a window sized on work that is
    already done - recording 17 held a 5.06h lookahead with 1.7h of source left and stepped
    aside five hours before the recording it stepped aside for began (dev/changelog/953)."""
    if not remaining_duration or remaining_duration <= 0:
        return 0.0
    m = multiplier if multiplier is not None else 1.0
    return remaining_duration / max(0.1, m)


# What each conflicting status is actually doing to the machine, in the user's words. Every
# status _conversion_collision_conflict() can return has an entry; a status with none is a
# defect, not a case to render (CLAUDE.md "states are enumerated").
def _conflict_phrase(conflict) -> str:
    from .database import (REC_STATUS_SCHEDULED, REC_STATUS_IN_PROGRESS,
                           REC_STATUS_CONCATENATING, REC_STATUS_ANALYZING)
    phrases = {
        REC_STATUS_IN_PROGRESS: 'is in progress',
        REC_STATUS_SCHEDULED: 'starts soon',
        REC_STATUS_CONCATENATING: 'is joining its segments',
        REC_STATUS_ANALYZING: 'is being checked for damage',
    }
    phrase = phrases.get(conflict.status)
    if phrase is None:
        log.warning('Recording %d is a conversion conflict in unhandled status %s',
                    conflict.id, conflict.status)
        return f'is busy ({conflict.status})'
    return phrase


def _conversion_collision_conflict(within_seconds, exclude_recording_id):
    """A Recording that argues against starting/continuing an mp4 conversion right now, or
    None when nothing does.

    Four statuses count, and they are the four in which another recording is using this
    machine: IN_PROGRESS (capturing), SCHEDULED to start within within_seconds (about to),
    and CONCATENATING or ANALYZING - a whole-file join and a whole-file ffprobe, which want
    the same two vCPUs and the same CIFS mount an mp4 conversion does. The post-capture pair
    was invisible here until dev/changelog/953, so a conversion would wait politely for a
    capture and then land on top of that capture's own post-processing: recording 17's
    conversion started 09:30:05 and recording 19's 42.6 GB concat started 09:30:18, and the
    concat was killed shortly after.

    Two guards, and neither is optional:

    - **exclude_recording_id is required**, because the caller's OWN row is one of the rows
      this query matches. do_postprocess runs its pre-start check while the row is still
      ANALYZING, so without the exclusion every conversion would wait forever on itself.
    - **A parked row does not count.** postprocess_waiting_since is the recorded fact that a
      row has stopped and is waiting on someone else (dev/changelog/952), so it is provably
      consuming nothing. Two parked rows that each counted the other would deadlock, and
      that is the exact state recordings 17 and 19 were in on 2026-09-13 - both ANALYZING,
      both yielding, both doing no work.

    Mirrors channel_tester.imminent_recording_conflict() (DESIGN-concurrency.md 5.5),
    extended because conversion CPU/disk contention with a recording matters for the
    recording's whole run, not just its run-up (recording.post_process.collision_policy).
    """
    from datetime import timedelta
    from .database import (Recording, REC_STATUS_SCHEDULED, REC_STATUS_IN_PROGRESS,
                           REC_STATUS_CONCATENATING, REC_STATUS_ANALYZING)
    from . import db as _db

    cutoff = datetime.utcnow() + timedelta(seconds=max(0.0, within_seconds))
    return Recording.query.filter(
        Recording.id != exclude_recording_id,
        _db.or_(
            Recording.status == REC_STATUS_IN_PROGRESS,
            _db.and_(Recording.status == REC_STATUS_SCHEDULED, Recording.start_time <= cutoff),
            _db.and_(Recording.status.in_((REC_STATUS_CONCATENATING, REC_STATUS_ANALYZING)),
                     Recording.postprocess_waiting_since.is_(None)),
        )
    ).order_by(Recording.start_time).first()


def _wait_for_conversion_clear(recording_id, within_seconds, poll_seconds=5, log_every=12):
    """Block (polling) until _conversion_collision_conflict is clear, or this recording is
    cancelled meanwhile. Mirrors concatenator._wait_for_no_active_recording, with a
    lookahead window instead of a bare IN_PROGRESS check
    (recording.post_process.collision_policy).

    Returns 'clear' or 'cancelled', naming which way out it took. The production caller
    re-checks the cancel itself and ignores this; tests assert the exit by name rather than
    by how fast it came (dev/docs/BUGS.md 2026-09-23 @ 09:08:47 PM)."""
    i = 0
    while True:
        conflict = _conversion_collision_conflict(within_seconds, recording_id)
        if conflict is None:
            return 'clear'
        if cancelled_meanwhile(recording_id):
            return 'cancelled'
        if i % log_every == 0:
            log.info('Recording %d conversion waiting on recording "%s" (status %s, '
                     'starts %s) - recording.post_process.collision_policy',
                     recording_id, conflict.name, conflict.status, conflict.start_time)
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

    `out_time` is how far into the source the attempt had encoded when it ended, as an
    ABSOLUTE source position - a resumed attempt's `-ss` offset is already added back, so the
    figure means the same thing whatever the attempt started from. The restart loop compares
    it across attempts: a defect in the source file stops every attempt at the same offset,
    and restarting from the top cannot get past it.
    `decode_errors` is how many frames ffmpeg failed to decode, counted so a conversion
    that finished by concealing damage can still say the damage happened.
    """
    def __init__(self, success, reason=None, error_msg=None, out_time=None,
                 decode_errors=0):
        self.success = success
        # 'success' | 'died' | 'stalled' | 'no_output' | 'timeout'. 'no_output' is the
        # pre-output budget expiring; 'timeout' survives only for the stall-detection-
        # disabled fallback, and is no longer reachable in the default configuration
        # (dev/changelog/865). There is no 'preempted': yielding to a recording suspends the
        # ffmpeg and continues it, so an attempt that yields still ends exactly once, on its
        # own terms (dev/changelog/952).
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


def set_postprocess_wait(rec, conflict=None):
    """Move a recording's whole park record together: parked-since, who it is waiting on,
    and what that recording is doing. `conflict` is the blocking Recording, or None to clear.

    THE THREE COLUMNS ARE ONE FACT AND THIS IS THEIR ONLY WRITER IN app/. Never assign any
    of them directly. A stamp without a blocker renders as a wait naming nobody, and a blocker
    without a stamp is a finished recording still claiming to be waiting - the same defect
    class as the participation switch, where two writers for one user-visible fact meant it
    could move with nothing on any surface saying so (CLAUDE.md).

    Mutates without committing, so the caller owns the whole read-modify-write unit and can
    join it to whatever else it is writing under one retry_on_locked (the same shape as
    channel_groups.set_participation).
    """
    rec.postprocess_waiting_since = datetime.utcnow() if conflict is not None else None
    rec.postprocess_waiting_on_name = conflict.name if conflict is not None else None
    rec.postprocess_waiting_on_state = _conflict_phrase(conflict) if conflict is not None else None


def _persist_postprocess_waiting(recording_id, conflict=None):
    """Record - or clear - the fact that this recording's post-processing is parked waiting
    on another recording. Its own re-fetch→mutate→commit closure, per CLAUDE.md.

    Written at both yield sites and cleared on every way out of either, because the one
    consumer that cannot see this process is the one that matters: tools/check_busy.py reads
    the row to decide whether a restart is interrupting real work, and a stamp left behind
    by a wait that ended would tell it a working recording is idle.

    Returns the row's status (None if the row is gone), so a caller can publish the
    parked/unparked frame without a second read.
    """
    from . import db
    from .database import Recording

    @retry_on_locked()
    def _do():
        r = db.session.get(Recording, recording_id)
        if r is None:
            return None
        set_postprocess_wait(r, conflict)
        status = r.status
        db.session.commit()
        return status

    return _do()


# ── Re-encode checkpointing ───────────────────────────────────────────────────────────
# A KILLED RE-ENCODE KEEPS WHAT IT ENCODED. The re-encode writes numbered part files rather
# than the final container, and a final concat joins them; a stall, a crash or a service
# restart therefore costs one stretch of encoding instead of the whole job. Recording 17 lost
# 4h26m at 66.4% to the old behavior, and its partial was not even readable
# (dev/changelog/955).
#
# Three findings measured on this box on 2026-09-13 shape all of it, and none is optional:
#   1. +faststart writes moov LAST, so a killed file has none and probes as garbage. The same
#      encode under +frag_keyframe+empty_moov+default_base_moof, killed at the same instant,
#      probes clean. That one flag is the whole difference between a discarded partial and a
#      usable checkpoint, which is why the final container's +faststart moves to the join.
#   2. -ss on the source plus the concat demuxer does reassemble correctly: 120.370370s
#      single-pass against 120.370370s of video across a two-part join, zero decode errors,
#      about two frames of splice drift at 59.94 fps.
#   3. THE SPLICE POINT COMES FROM THE LAST DECODABLE FRAME, NEVER THE DECLARED DURATION. A
#      killed part claimed 6.039373s while its last clean frame was at 5.739072s, the final
#      fragment truncated mid-NAL. Resuming at the declared duration silently lost exactly
#      1.0s of video. Re-muxing the survivor with -c copy drops the broken tail, and the
#      resume point is read from THAT - which is also what makes the part safe to join.
#
# Scope is the re-encode only. A stream copy finishes in minutes (3m to 15m47s measured on
# real recordings), so resuming one saves nothing and it keeps writing the final file directly.

# Fragmented-MP4 flags: moov up front and self-contained fragments, so a part killed mid-write
# is still readable up to its last complete fragment. Finding 1 above.
PART_MOVFLAGS = '+frag_keyframe+empty_moov+default_base_moof'


def part_path(output_path: str, index: int) -> str:
    """Where part `index` (1-based) of a resumable conversion lives.

    DERIVED from the output path rather than stored, so the row and the filesystem cannot
    disagree about which file a part number names. Dot-prefixed because recording.
    dvr_output_dir defaults to the same directory a single-directory install points its media
    scanner at, and a multi-gigabyte `Show.part1.mp4` sitting there is something Plex will
    happily index as an episode. The existing .conv-progress-* scratch is hidden for the same
    reason.
    """
    directory = os.path.dirname(output_path)
    stem, ext = os.path.splitext(os.path.basename(output_path))
    return os.path.join(directory, f'.{stem}.part{index}{ext}')


def clean_part_path(part_file: str) -> str:
    """Where finalize_part() re-muxes a part before replacing it.

    The extension is preserved, not appended to: ffmpeg infers the muxer from the output
    extension, so a plain `<part>.clean` gives it nothing to infer and it exits -EINVAL
    without writing a byte - measured on this box, which is the only way anyone finds out
    (CLAUDE.md external-tools-are-verified-empirically).
    """
    root, ext = os.path.splitext(part_file)
    return f'{root}.clean{ext}'


def existing_part_paths(output_path: str, count: int) -> list:
    """The part files 1..count, whether or not they exist on disk."""
    return [part_path(output_path, i) for i in range(1, count + 1)]


def all_part_paths_on_disk(output_path: str, limit: int = 64) -> list:
    """Every part file that actually exists for this output, for teardown.

    Scans a bounded range rather than the recorded count: teardown has to remove parts the
    row does not know about - one written by an attempt that died before its commit, or left
    by a run whose signature was later invalidated - and a file nothing references is exactly
    what teardown exists to catch (CLAUDE.md teardown-releases-everything). The limit is a
    sanity bound on the scan, not a cap on how many parts may exist; a conversion reaching 64
    restarts has been given up on long before.
    """
    found = []
    for i in range(1, limit + 1):
        p = part_path(output_path, i)
        for candidate in (p, clean_part_path(p)):
            # The .clean sibling is finalize_part()'s re-mux, os.replace()d over the part on
            # success. It only outlives that call when a shutdown lands mid-re-mux, and it is
            # the same size as the part, so teardown has to know about it too.
            if os.path.exists(candidate):
                found.append(candidate)
    return found


def parts_signature(cmd, output_path: str) -> str:
    """A fingerprint of the encode-relevant arguments behind a set of parts.

    PARTS MADE UNDER DIFFERENT SETTINGS MUST NEVER BE JOINED, and this is what notices. Two
    real cases reach it: the audio-copy fallback swaps the audio codec mid-conversion, so a
    joined file would carry one codec config over a track encoded two ways; and a crf or
    bitrate edited in settings between a crash and its resume would leave a quality seam in
    the middle of the file that nothing on any surface could explain. A mismatch discards the
    parts and says so, which costs the re-encode this feature would have saved and is still
    the right trade - principle 1 outranks principle 2 whenever they pull apart.

    The output path and the -ss offset are excluded because they are what legitimately differs
    BETWEEN parts of one set. Everything else is included, hashed rather than enumerated, so a
    future flag is covered without anyone remembering to add it here.
    """
    import hashlib

    meaningful = []
    skip_next = False
    for i, arg in enumerate(cmd):
        if skip_next:
            skip_next = False
            continue
        if arg == '-ss':
            skip_next = True
            continue
        if i == len(cmd) - 1 and arg == output_path:
            continue
        meaningful.append(str(arg))
    return hashlib.sha256('\x00'.join(meaningful).encode('utf-8')).hexdigest()[:32]


def set_conversion_parts(rec, *, parts_done=0, source_covered=None, source_complete=False,
                         signature=None):
    """Move a recording's whole conversion checkpoint together: how many parts exist, how much
    source they cover, whether the encode reached the end, and what settings made them.

    THE FOUR COLUMNS ARE ONE FACT AND THIS IS THEIR ONLY WRITER IN app/. Never assign any of
    them directly. A part count without a covered offset resumes from the wrong place; a
    covered offset without a signature joins parts that do not belong together; and a
    source_complete left behind by an abandoned run skips the encode entirely. The same shape
    as set_postprocess_wait() above, and for the same reason.

    Called with no arguments it clears the checkpoint, which is what a discard is.

    Mutates without committing, so the caller owns the whole read-modify-write unit
    (CLAUDE.md).
    """
    rec.conversion_parts_done = parts_done or 0
    rec.conversion_source_covered_seconds = source_covered
    rec.conversion_source_complete = bool(source_complete)
    rec.conversion_parts_signature = signature


def _persist_conversion_parts(recording_id, **kwargs):
    """Record - or clear - the conversion checkpoint. Its own re-fetch→mutate→commit closure,
    per CLAUDE.md.

    THE COMMIT IS THE POINT. A part file that exists but is not committed here is not a
    checkpoint, and the next attempt verifies it from scratch before believing anything about
    it. That ordering is what makes the crash window safe: the worst case is re-encoding a
    stretch that was already done, never splicing at an offset nothing measured.
    """
    from . import db
    from .database import Recording

    @retry_on_locked()
    def _do():
        r = db.session.get(Recording, recording_id)
        if r is None:
            return
        set_conversion_parts(r, **kwargs)
        db.session.commit()

    _do()


def finalize_part(part_file: str, ffmpeg_path: str, *, scratch_key, interval=5,
                  pre_output_timeout=300, stall_seconds=300, label='part', on_spawn=None):
    """Make a killed part joinable and measure where it really ends. Returns its content
    duration in seconds, or None if there is nothing usable in it.

    A part killed mid-write ends in a truncated fragment. Re-muxing it with -c copy drops that
    tail, and the duration of the RESULT is the last decodable frame - which is both the only
    honest splice point (finding 3: the declared duration overshot by exactly 1.0s) and what
    makes the file safe to hand to the concat demuxer. The two needs are the same operation,
    which is why this is not a probe with a repair bolted on.

    Idempotent: running it against an already-clean part re-muxes it again and returns the
    same duration, so the caller may call it without first knowing whether an earlier attempt
    got to. That is what lets a service restart adopt a part no commit describes.

    Never raises. A part too short to hold a keyframe, or damaged past re-muxing, returns None
    and is discarded by the caller - honest degradation to "re-encode that stretch".
    """
    if not part_file or not os.path.exists(part_file):
        return None
    try:
        if os.path.getsize(part_file) <= 0:
            return None
    except OSError:
        return None

    clean = clean_part_path(part_file)
    # -fflags +discardcorrupt is what actually drops the tail, and it is not optional:
    # measured on this box, a plain -c copy re-mux reports the right duration while copying
    # the half-written packet through, so the file ends with "Invalid NAL unit size" and the
    # join inherits it. The duration was honest and the bytes were not - which is the exact
    # shape of damage this app exists to refuse to ship silently.
    cmd = [ffmpeg_path, '-err_detect', 'ignore_err', '-fflags', '+discardcorrupt',
           '-i', part_file, '-c', 'copy', '-movflags', PART_MOVFLAGS, '-y', clean]
    try:
        run = supervise_ffmpeg(
            cmd, clean, scratch_prefix='part', scratch_key=scratch_key,
            interval=interval, pre_output_timeout=pre_output_timeout,
            stall_seconds=stall_seconds, progress_signal='size', noun='part remux',
            label=label, on_spawn=on_spawn)
    except Exception as exc:
        log.warning('%s: could not re-mux %s: %s', label, part_file, exc)
        run = None

    if run is None or not run.success or not os.path.exists(clean) or os.path.getsize(clean) <= 0:
        log.warning('%s: %s holds nothing that survives a re-mux - discarding it',
                    label, os.path.basename(part_file))
        try:
            os.unlink(clean)
        except OSError:
            pass  # best-effort scratch cleanup
        return None

    duration = None
    try:
        from .probe import parse_ffprobe
        info = parse_ffprobe(clean, count_packets=False, timeout=120)
        if info:
            duration = info.get('duration')
    except Exception as exc:
        log.warning('%s: could not probe the re-muxed %s: %s', label, part_file, exc)

    if not duration or duration <= 0:
        try:
            os.unlink(clean)
        except OSError:
            pass  # best-effort scratch cleanup
        return None

    try:
        os.replace(clean, part_file)
    except OSError as exc:
        log.warning('%s: could not put the re-muxed part back as %s: %s', label, part_file, exc)
        try:
            os.unlink(clean)
        except OSError:
            pass  # best-effort scratch cleanup
        return None
    return float(duration)


class PartJoinResult:
    """Outcome of assembling encoded parts into the final container.

    `reason` is 'success', 'no_parts', 'missing_part', 'no_space' or an ffmpeg failure
    reason. `error_msg` is already written for a person - it goes straight into the event
    that explains a failed conversion, so a join that fails names what it needed.
    """

    def __init__(self, success, reason, *, error_msg=None, parts=0, bytes_in=0):
        self.success = success
        self.reason = reason
        self.error_msg = error_msg
        self.parts = parts
        self.bytes_in = bytes_in


def join_conversion_parts(parts, output_path, ffmpeg_path, *, scratch_key, interval=5,
                          pre_output_timeout=1800, stall_seconds=300, label='join',
                          on_progress=None, on_spawn=None) -> PartJoinResult:
    """Assemble encoded parts into the final container with the concat demuxer.

    THIS REPLACES +faststart's REWRITE RATHER THAN ADDING A PASS. A faststart mux already
    walks the whole file a second time to move moov to the front; this walk does that and the
    join at once, so the steady-state cost of checkpointing is wall-clock-neutral even when
    there is only one part. What it is NOT neutral on is disk: the parts and the output exist
    together for the length of the join, which is why the space check below is not optional.

    -c copy throughout: the parts were encoded to identical settings (parts_signature() is
    what guarantees it), so there is nothing to transcode and nothing to decide.

    Supervised through proc_utils.supervise_ffmpeg() like every other long ffmpeg in this app
    - watching bytes, because writing bytes is the entire job of a stream copy.
    """
    import tempfile

    if not parts:
        return PartJoinResult(False, 'no_parts',
                              error_msg='No encoded parts to assemble.')
    missing = [p for p in parts if not os.path.exists(p)]
    if missing:
        return PartJoinResult(
            False, 'missing_part', parts=len(parts),
            error_msg=f'{len(missing)} of {len(parts)} encoded part(s) are gone from disk - '
                      f'the conversion has to start over.')

    bytes_in = 0
    for p in parts:
        try:
            bytes_in += os.path.getsize(p)
        except OSError:
            pass  # counted only to size the space check and the event text

    directory = os.path.dirname(output_path) or '.'
    try:
        free_bytes = shutil.disk_usage(directory).free
    except OSError:
        free_bytes = None
    # The output is a stream copy of the parts, so it lands within rounding of their combined
    # size. Checked BEFORE the join rather than discovered as a truncated output halfway
    # through it: the parts are the only copy of hours of encoding at this point, and an
    # ENOSPC that leaves them in place with a clear message is recoverable where one that
    # reads as an ffmpeg failure is not.
    if free_bytes is not None and free_bytes < bytes_in:
        return PartJoinResult(
            False, 'no_space', parts=len(parts), bytes_in=bytes_in,
            error_msg=f'Not enough disk space to assemble the converted file - need '
                      f'{_fmt_bytes(bytes_in)}, only {_fmt_bytes(free_bytes)} free. The '
                      f'encoded parts are kept; free space and retry.')

    fd, list_path = tempfile.mkstemp(prefix='dvr_partjoin_', suffix='.txt')
    try:
        with os.fdopen(fd, 'w') as fh:
            for p in parts:
                fh.write(f"file '{p}'\n")
        cmd = [ffmpeg_path, '-f', 'concat', '-safe', '0', '-i', list_path,
               '-c', 'copy', '-movflags', '+faststart', '-y', output_path]
        log.info('%s: assembling %d part(s), %s: %s', label, len(parts),
                 _fmt_bytes(bytes_in), ' '.join(cmd))
        run = supervise_ffmpeg(
            cmd, output_path, scratch_prefix='join',
            scratch_key=scratch_key, interval=interval,
            pre_output_timeout=pre_output_timeout, stall_seconds=stall_seconds,
            progress_signal='size', noun='assembly', label=label,
            on_progress=on_progress, on_spawn=on_spawn)
    finally:
        try:
            os.unlink(list_path)
        except OSError:
            pass  # best-effort list-file cleanup

    if not run.success:
        return PartJoinResult(False, run.reason, error_msg=run.error_msg,
                              parts=len(parts), bytes_in=bytes_in)
    return PartJoinResult(True, 'success', parts=len(parts), bytes_in=bytes_in)


def discard_conversion_parts(output_path):
    """Remove every part file for this output. Returns how many were deleted.

    Used wherever the parts stop being a valid checkpoint - a settings change, a signature
    mismatch, a cancel, a success that no longer needs them - and by teardown. Best-effort per
    file: one undeletable part must not stop the rest from going.
    """
    removed = 0
    for p in all_part_paths_on_disk(output_path):
        try:
            os.unlink(p)
            removed += 1
        except OSError as exc:
            log.warning('Could not delete conversion part %s: %s', p, exc)
    return removed


# How far the assembled file's duration may sit from the span it should cover before it stops
# being recognisable as this conversion's output. Generous deliberately: each join seam loses
# about two frames, and the covered span is itself read from ffmpeg's progress output, so a
# tight bound would refuse healthy files - while what this has to exclude (a half-written
# assembly, a stale file left by an earlier format) is unreadable or wrong by minutes.
_ADOPT_SLACK_SECONDS = 5.0
_ADOPT_SLACK_FRACTION = 0.02


def _adoptable_assembly(output_path: str, expected_seconds: float) -> bool:
    """Is the file already sitting at `output_path` the finished assembly of an encode whose
    parts have since been deleted?

    Answered from the file, never from the row - the row is the thing in doubt wherever this
    gets asked. A header-only probe settles the case that actually arises: +faststart writes
    moov LAST and only relocates it once the write finishes, so an assembly killed mid-write
    has no moov atom and ffprobe reports nothing about it at all (measured on this box, and
    what the original incident saw as "moov atom not found"). Reading the whole file with
    -count_packets would walk up to 19 GB to learn what the header already says.

    ITS ONE BLIND SPOT, STATED RATHER THAN IMPLIED: a file truncated AFTER a successful
    faststart write still carries a valid moov at the front and probes clean, and ffprobe
    derives the format bitrate from the actual size, so no header field separates it from the
    complete original. Only a whole-file read would. That shape is not what the crash this
    guards against produces - the parts are deleted only once the assembly finished - so the
    trade is deliberate.

    Returns False for anything it cannot positively confirm - unreadable, zero-length, no
    video stream, no duration, a duration that does not match, or no span to match it against
    - so every unconfirmed case falls through to the caller's existing failure path rather
    than adopting a file on a guess.
    """
    from .probe import parse_ffprobe

    if not output_path or not os.path.exists(output_path):
        return False
    try:
        if os.path.getsize(output_path) <= 0:
            return False
    except OSError:
        return False
    if not expected_seconds or expected_seconds <= 0:
        log.warning('Cannot judge whether %s is a finished assembly: nothing recorded how '
                    'much of the source it should cover', output_path)
        return False

    probe = parse_ffprobe(output_path, count_packets=False)
    if not probe or not probe.get('video_codec'):
        return False
    duration = probe.get('duration')
    if not duration or duration <= 0:
        return False

    slack = max(_ADOPT_SLACK_SECONDS, expected_seconds * _ADOPT_SLACK_FRACTION)
    if abs(duration - expected_seconds) > slack:
        log.warning('%s is readable but runs %.1fs against the %.1fs it should cover, so it '
                    'is not the finished assembly of this conversion',
                    output_path, duration, expected_seconds)
        return False
    return True


def run_conversion_supervised(app, recording_id, cmd, output_path, *,
                              expected_duration, pre_output_timeout, interval, stall_seconds,
                              collision_policy='off', collision_multiplier=1.0,
                              source_offset=0.0):
    """Spawn one conversion ffmpeg and supervise it: poll -progress, publish/persist a
    progress snapshot, and detect death, stall, or (collision_policy='cancel') a colliding
    recording. Returns a ConversionResult for the caller's restart loop to act on.

    A YIELD IS SIZED ON THE WORK LEFT, NOT THE WHOLE JOB. `collision_multiplier` is turned
    into a lookahead window against the source still to encode on every poll, so a
    conversion that is nearly done steps aside for almost nothing while one that has barely
    started still steps aside early (dev/changelog/953).

    A CONVERSION THAT YIELDS IS SUSPENDED, NEVER KILLED. `collision_policy='cancel'`
    SIGSTOPs the ffmpeg while a recording needs the machine and SIGCONTs it afterwards, so
    stepping aside costs wall clock instead of every hour already encoded - recording 17 lost
    4h26m at 66.4% to the kill this replaced, and its partial had no moov atom, so none of it
    was recoverable (dev/changelog/952). The attempt is not ended, so nothing about a yield
    reaches the caller's restart loop or its budget.

    AN ATTEMPT MAY START PART-WAY INTO THE SOURCE. `source_offset` is how far in this
    attempt's `-ss` puts it, and ffmpeg's out_time is relative to that - so every figure
    derived from progress adds it back: the percentage, the ETA's target and the collision
    window's work-remaining. Without that a resumed attempt's progress bar falls back to 0%
    and stops at the fraction it re-encoded, and its collision window is sized on source that
    is already on disk (dev/changelog/955).

    A CONVERSION THAT IS STILL ADVANCING IS NEVER KILLED. There is no whole-job deadline:
    `pre_output_timeout` bounds only the phase before ffmpeg muxes its first frame, and
    after that `stall_seconds` is the sole authority (dev/changelog/865). The deadline this
    replaced fired on healthy work whenever a job was simply longer than its budget - it
    killed a 3.6h 1080p59.94 re-encode at elapsed 14,404s against a 14,400s number
    calibrated on a machine that encoded twice as fast as this one, and the retry then
    re-ran the identical command from 0% twice more.

    Both rules, the poll loop and the scratch/stderr handling now live in
    proc_utils.supervise_ffmpeg(), which the concat shares (dev/changelog/947). What stays
    here is what is specific to a conversion: the progress snapshot, the ETA, the live
    registry, the collision yield and the decode-error count. Stall detection watches
    `out_time` rather than bytes because an encode's output file can sit still while the
    muxer buffers.

    The ffmpeg spawn is a non-idempotent side effect and lives OUTSIDE every
    retry_on_locked closure (CLAUDE.md). Each progress-snapshot write is its own small
    re-fetch→mutate→commit closure. The child is terminated on every exit path and
    unregistered in finally.
    """
    from . import db
    from .database import (Recording, RecordingEvent, CONVERSION_YIELDED, CONVERSION_RESUMED,
                           REC_STATUS_CONVERTING)
    from . import events as ev

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

    # The ETA is for the work THIS attempt has left, so it is projected against the source
    # still to encode rather than the whole recording - the part already on disk is not
    # waiting on anything.
    remaining_duration = (max(0.0, expected_duration - source_offset)
                          if expected_duration else expected_duration)
    smoother = EtaSmoother(remaining_duration)
    decode_errors = 0
    # The conflict _check_collision most recently decided to yield to, so _on_suspend can
    # record WHO the pause is for without asking the database a second question.
    suspending_for = None

    def _register(proc):
        with _active_lock:
            _active_conversions[recording_id] = proc

    def _publish(wall, out_time, size):
        pct = None
        # Against the SOURCE position, not this attempt's own progress: a resume that showed
        # 0% after two hours of encoding already on disk is a number the user cannot explain.
        source_at = out_time + source_offset
        if expected_duration and expected_duration > 0 and source_at > 0:
            pct = max(0.0, min(100.0, source_at / expected_duration * 100.0))
        eta = smoother.update(wall, out_time)
        _persist_conversion_snapshot(recording_id, pct, size, eta)
        ev.publish(recording_id, 'CONVERSION_PROGRESS', {
            'status': REC_STATUS_CONVERTING, 'pct': pct, 'out_size': size or None,
            'eta_seconds': eta,
        })

    def _check_collision(wall, out_time, size):
        """None to keep encoding; a reason string to have the ffmpeg suspended until clear.

        The window is sized on the source LEFT to encode, recomputed each poll, because a
        lookahead is a guess at how much longer this conversion needs and a job that is
        nearly finished needs almost none. out_time is already in hand from the poll that
        called this, so nothing is re-read (CLAUDE.md no-hidden-I/O - this is a per-poll
        loop). While the child is suspended out_time is frozen, so the window correctly
        stops shrinking during a pause rather than counting wall clock as progress."""
        nonlocal suspending_for
        if collision_policy != 'cancel':
            return None
        # source_offset is already encoded and on disk, so it is not work left - a resumed
        # attempt that counted it would yield on a window sized for a job it is most of the
        # way through.
        remaining = (expected_duration - out_time - source_offset) if expected_duration else None
        window = _collision_window_seconds(remaining, collision_multiplier)
        conflict = _conversion_collision_conflict(window, recording_id)
        if conflict is None:
            return None
        # Handed to _on_suspend through here rather than re-queried there: the answer is
        # already in hand, and a second query could name a different recording than the one
        # the pause is actually for.
        suspending_for = conflict
        return (f'yielding local resources to recording "{conflict.name}" '
                f'({_conflict_phrase(conflict)})')

    def _on_suspend(reason):
        # The row is stamped BEFORE the event is written, so the window in which a restart
        # guard could see a parked conversion as working is as short as one commit rather
        # than as long as whatever the wait turns out to be.
        _persist_postprocess_waiting(recording_id, suspending_for)

        @retry_on_locked()
        def _commit_yielded_event():
            db.session.add(RecordingEvent(
                recording_id=recording_id, event_type=CONVERSION_YIELDED,
                detail=f'Conversion paused: {reason}. It keeps everything encoded so far '
                       f'and continues by itself once the recording is done.'))
            db.session.commit()

        _commit_yielded_event()
        # `waiting` is what lets the Dashboard badge the row WAITING live, the same
        # derivation fmt_utils.rec_status_display makes from the stored stamp.
        ev.publish(recording_id, CONVERSION_YIELDED, {'status': REC_STATUS_CONVERTING, 'waiting': True})

    def _on_resume():
        nonlocal suspending_for
        suspending_for = None
        _persist_postprocess_waiting(recording_id, None)

        @retry_on_locked()
        def _commit_resumed_event():
            db.session.add(RecordingEvent(
                recording_id=recording_id, event_type=CONVERSION_RESUMED,
                detail='Conversion resumed where it left off - local resources are free again.'))
            db.session.commit()

        _commit_resumed_event()
        ev.publish(recording_id, CONVERSION_RESUMED, {'status': REC_STATUS_CONVERTING, 'waiting': False})

    def _tally_decode_errors(stderr_path, returncode):
        # Read while the spool still exists - supervise_ffmpeg unlinks it right after this.
        nonlocal decode_errors
        decode_errors = _count_decode_errors(stderr_path)

    try:
        run = supervise_ffmpeg(
            cmd, output_path,
            scratch_prefix='conv', scratch_key=recording_id,
            interval=interval, pre_output_timeout=pre_output_timeout,
            stall_seconds=stall_seconds, progress_signal='out_time',
            noun='conversion', label=f'Recording {recording_id} conversion',
            on_spawn=_register, on_progress=_publish,
            suspend_check=_check_collision, on_suspend=_on_suspend, on_resume=_on_resume,
            on_exit=_tally_decode_errors)
    finally:
        with _active_lock:
            _active_conversions.pop(recording_id, None)
        # A run killed while suspended (a cancel, a shutdown) never reaches _on_resume, so
        # the stamp is cleared here too - the attempt is over either way, and a row left
        # claiming to be waiting outlives everything that could clear it.
        _persist_postprocess_waiting(recording_id, None)

    # decode_errors is attributed only to a run that reached the end of the source; a
    # killed attempt's count describes the part it got through, not the file.
    #
    # out_time is returned as a SOURCE position, offset included, because every consumer
    # reasons about the source: the restart loop compares where two attempts stopped to tell a
    # damaged file from a transient failure, and that comparison is meaningless between two
    # attempts that started at different places.
    return ConversionResult(
        run.success, reason=run.reason, error_msg=run.error_msg,
        out_time=(run.out_time or 0.0) + source_offset,
        decode_errors=decode_errors if run.reason in ('success', 'died') else 0)


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


#: The two post-processing failure alerts this module raises inline and clears inline, and
#: the observation that proves each one is over. Both are self_clearing, so an open row sits
#: under the Alerts page's "Active alerts" card, which offers no Dismiss (dev/changelog/932)
#: - which is only safe while something actually clears them.
_RECONCILABLE_ALERTS = ('CONVERSION_FAILED', 'RECORDING_MOVE_FAILED')


def reconcile_failure_alerts():
    """Dismiss open conversion/move alerts whose recording has since recovered.

    Called at startup. The inline clears in do_postprocess run one call AFTER the commit
    that records the success, so a process death in that gap - `restart.sh --force` during
    a conversion is the ordinary way to get there - leaves the alert standing over a
    recording that is fine, in a card with no Dismiss, forever. Deriving each row's
    liveness from what the recording itself recorded closes that gap permanently, and is
    the same demand CLAUDE.md's "already done is a fact you recorded" rule makes of the
    migration ledger: do not infer state from whether one call happened to run.

    Both tests are POSITIVE observations of recovery, never "no evidence of failure":

    - `CONVERSION_FAILED` - the recording is COMPLETED. Every site that raises this type
      puts the row in FAILED first (here and the two startup-recovery sites in
      app/scheduler.py), so COMPLETED can only mean a later attempt finished.
    - `RECORDING_MOVE_FAILED` - the file is in the configured move destination. A failed
      move leaves output_path where it was and the successful one rewrites it, so the
      column answers this directly. A move that is now disabled, or pointed somewhere
      else, is not evidence either way and the row is left alone.

    A recording that no longer exists is also left alone: deleting one already dismisses
    and unlinks its alerts (dev/changelog/929), so a dangling id is not a recovery.
    """
    from . import db
    from .config import load_config
    from .database import Alert, Recording, REC_STATUS_COMPLETED

    open_rows = (Alert.query
                 .filter(Alert.dismissed_at.is_(None),
                         Alert.alert_type.in_(_RECONCILABLE_ALERTS),
                         Alert.recording_id.isnot(None))
                 .all())
    if not open_rows:
        return 0

    # Hoisted above the loop, and the recordings fetched in one keyed query rather than a
    # db.session.get per row (CLAUDE.md, no hidden I/O in per-row loops).
    mv = load_config()['recording']['post_process'].get('move') or {}
    move_dest = (os.path.normpath(mv['destination'].strip())
                 if mv.get('enabled') and mv.get('destination', '').strip() else None)
    recs = {r.id: r for r in Recording.query.filter(
        Recording.id.in_({a.recording_id for a in open_rows})).all()}

    stale = []
    for a in open_rows:
        rec = recs.get(a.recording_id)
        if rec is None:
            continue
        if a.alert_type == 'CONVERSION_FAILED':
            recovered = rec.status == REC_STATUS_COMPLETED
            observed = f'recording is {rec.status}'
        else:
            recovered = bool(move_dest) and bool(rec.output_path) and \
                os.path.dirname(os.path.normpath(rec.output_path)) == move_dest
            observed = f'file is at {rec.output_path}'
        if recovered:
            # Loud rather than quiet: a self-clearing alert that needed reconciling is one
            # whose inline clear did not run, and that is worth being able to read later.
            log.warning('Alert %d (%s, recording %d) describes a condition that is over '
                        '(%s) - dismissing it at startup', a.id, a.alert_type, rec.id, observed)
            stale.append(a.id)

    if not stale:
        return 0

    @retry_on_locked()
    def _dismiss():
        rows = Alert.query.filter(Alert.id.in_(stale), Alert.dismissed_at.is_(None)).all()
        now = datetime.utcnow()
        for row in rows:
            row.dismissed_at = now
        db.session.commit()
        return len(rows)

    return _dismiss()


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
        POSTCAPTURE_ANALYSIS_STARTED, POSTCAPTURE_ANALYSIS_SKIPPED,
        CONVERSION_STARTED, CONVERSION_RESTARTED, CONVERSION_YIELDED, CONVERSION_RESUMED,
        CONVERSION_DONE, FILE_MOVED, CONCATENATION_DONE, SCRIPT_EXECUTED,
        REC_STATUS_ANALYZING, REC_STATUS_CONVERTING,
        REC_STATUS_ABORTED, REC_STATUS_FAILED, REC_STATUS_COMPLETED,
        FAILURE_CONVERSION_FAILED, CANCEL_DURING_CONVERSION,
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
        # Resolved once, from the same settings the phase branches on below, so every pass
        # reports the same denominator - a plan recomputed per pass could name "1 of 2" and
        # then "1 of 1" on one recording.
        analysis_plan = analysis_pass_plan(cfg, pp)

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
        #
        # analysis_completed_at is the recorded fact that this phase already finished, and
        # the ONLY thing consulted - never the status, which reads ANALYZING both while the
        # phase runs and while a finished one sits parked yielding to a live recording, i.e.
        # exactly the state a restart finds. Before this record existed, every resume re-read
        # the whole file and blended a SECOND capture-quality observation into the channel's
        # health score: recording 17 did it three times and left channel 16047 holding a
        # number observation_ledger() could not reproduce (dev/changelog/951).
        analysis_done_at = rec.analysis_completed_at

        if analysis_done_at is not None:
            from .tz_utils import format_local

            @retry_on_locked()
            def _commit_analysis_skipped():
                r = db.session.get(Recording, recording_id)
                if preserve_cancelled_status(
                        r, 'Post-processing did not start - the recording was cancelled first.'):
                    db.session.commit()
                    return False
                r.status = REC_STATUS_ANALYZING
                add_recording_event(
                    recording_id, POSTCAPTURE_ANALYSIS_SKIPPED,
                    detail=f'The joined file was already checked on '
                           f'{format_local(analysis_done_at)} - picking back up at conversion '
                           f'without re-reading it.')
                db.session.commit()
                return True

            if not _commit_analysis_skipped():
                log.info('Recording %d was cancelled before post-processing started', recording_id)
                return
            log.info('Recording %d: post-capture analysis already completed at %s - '
                     'resuming at conversion', recording_id, analysis_done_at)
            ev.publish(recording_id, POSTCAPTURE_ANALYSIS_SKIPPED,
                       {'status': REC_STATUS_ANALYZING})
            timeline_scan = _recorded_timeline_scan(recording_id)
        else:
            # An earlier attempt that started this phase and never recorded finishing it is
            # being redone, which an operator must not have to infer - same warning the
            # migration backfill ledger prints for the same reason.
            redo = _analysis_attempt_count(recording_id) > 0
            if redo:
                log.warning('Recording %d: an earlier post-capture analysis did not finish - '
                            'reading the joined file again from the top', recording_id)

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
                    detail=(f'Checking the joined file before conversion: '
                            f'{os.path.basename(ts_path)}')
                           + (' - an earlier check did not finish, so it runs again'
                              if redo else ''))
                db.session.commit()
                return True

            if not _commit_analysis_started():
                log.info('Recording %d was cancelled before post-processing started', recording_id)
                return
            ev.publish(recording_id, POSTCAPTURE_ANALYSIS_STARTED, {'status': REC_STATUS_ANALYZING})

            # ── Recording health data (ffprobe on the .ts file) ───────────────
            health_fields, health_diag = _gather_recording_health(
                recording_id, ts_path, rec, cfg, analysis_plan=analysis_plan)
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

            # ── Timeline scan (MEASURE only - see the re-encode branch below) ─
            # Gated on gather_health_data alone, so the stats and the DIAGNOSTICS event exist
            # for every format and every reencode_mode, not just the one combination that
            # happens to act on the verdict. Memoized: the conversion phase reads this rather
            # than re-running a full-file ffprobe.
            timeline_scan = None
            analysis_finished_at = datetime.utcnow()
            if cfg['recording'].get('gather_health_data', True):
                timeline_scan = _scan_recording_timeline(recording_id, ts_path,
                                                         analysis_plan=analysis_plan)
                # Near-empty/slate detection (compute-only, no extra ffprobe) and the
                # capture-quality score correction both depend on what the timeline scan just
                # committed (timeline_deficit_seconds), so they run right after it, in order.
                _detect_near_empty_segments(recording_id)
                from .health_score import apply_capture_quality_correction
                # The completion stamp rides this call's own commit, and it has to: the blend
                # is the one step of this phase that is an increment rather than a recompute,
                # so a stamp written in a LATER commit leaves a crash window in which the
                # resume re-blends. Everything above it overwrites columns and is safe to
                # redo (CLAUDE.md, "anything reached through such a gate must be re-runnable
                # from the top").
                apply_capture_quality_correction(
                    app, recording_id, analysis_completed_at=analysis_finished_at)
            else:
                # No blend ran, so nothing here is an increment and the stamp is free to be
                # its own commit.
                @retry_on_locked()
                def _commit_analysis_completed():
                    r = db.session.get(Recording, recording_id)
                    if r is not None:
                        r.analysis_completed_at = analysis_finished_at
                    db.session.commit()

                _commit_analysis_completed()

        # ── Conversion ────────────────────────────────────────────────────────
        if _stop_if_cancelled('Post-processing stopped before conversion started - '
                              'the joined .ts file was kept.'):
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
            # Nothing has been encoded yet on this path, so the whole duration IS the work
            # remaining - the in-run check inside run_conversion_supervised is the one that
            # narrows it as the encode advances (dev/changelog/953).
            collision_window_seconds = _collision_window_seconds(expected_duration, collision_multiplier)
            if collision_policy != 'off':
                conflict = _conversion_collision_conflict(collision_window_seconds, recording_id)
                if conflict is not None:
                    where = _conflict_phrase(conflict)
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
                    # No ffmpeg exists yet on this path - the chain just polls - so there is
                    # nothing to suspend and nothing at risk from a restart. The stamp is
                    # what lets the restart guard tell that apart from a row that is working
                    # (dev/changelog/952); it is cleared on BOTH ways out of the wait, the
                    # cancelled-meanwhile one included.
                    parked_status = _persist_postprocess_waiting(recording_id, conflict)
                    ev.publish(recording_id, CONVERSION_YIELDED,
                               {'status': parked_status, 'waiting': True})
                    try:
                        _wait_for_conversion_clear(recording_id, collision_window_seconds)
                    finally:
                        unparked_status = _persist_postprocess_waiting(recording_id, None)
                        ev.publish(recording_id, CONVERSION_RESUMED,
                                   {'status': unparked_status, 'waiting': False})
                    if _stop_if_cancelled('Post-processing stopped while waiting for local '
                                          'resources to free up before converting.'):
                        return

                    @retry_on_locked()
                    def _commit_collision_resumed_event():
                        add_recording_event(
                            recording_id, CONVERSION_RESUMED,
                            detail='Local resources are free - starting the conversion now.')
                        db.session.commit()

                    _commit_collision_resumed_event()

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
                    timeline_scan = _scan_recording_timeline(
                        recording_id, ts_path, analysis_plan=analysis_plan)
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

            # Only a re-encode checkpoints. A stream copy finishes in minutes on files this
            # size (3m to 15m47s measured on real recordings), so parts would buy nothing and
            # cost a join pass; it keeps writing the final container directly, +faststart and
            # all (dev/changelog/955).
            resumable = (fmt == 'mp4' and reencode)

            def _build_cmd(audio_copy=False, dest=None, start_at=0.0):
                """The conversion command, optionally with audio stream-copied instead of
                re-encoded (the fallback below).

                `dest` and `start_at` are the checkpointing half: a resumable conversion writes
                each part to its own file, seeks into the source with -ss for parts after the
                first, and swaps +faststart for fragmented flags so a part killed mid-write is
                still readable. The seek is an INPUT seek, before -i, which decodes from the
                preceding keyframe and discards - accurate, and the shape finding 2 measured.

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
                target = dest or output_path
                c = [ffmpeg_path, '-err_detect', 'ignore_err', '-fflags', '+genpts+discardcorrupt',
                     '-max_error_rate', '1.0']
                if start_at and start_at > 0:
                    c += ['-ss', f'{start_at:.6f}']
                c += ['-i', ts_path]
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
                    return c + ['-c', 'copy', '-y', target]
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
                # A resumable run's parts are fragmented so a kill leaves a readable
                # checkpoint; +faststart moves to the join that assembles them, so the final
                # file is byte-for-byte the shape it has always been (dev/changelog/955).
                movflags = PART_MOVFLAGS if resumable else '+faststart'
                return c + ['-movflags', movflags, '-y', target]

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
            # Dropped here, before anything in this phase spawns an ffmpeg, rather than just
            # before the loop: the salvage below is a real child that a Cancel can kill, so a
            # flag set from this point on is a live cancel and must not be mistaken for a
            # stale one left by a previous run.
            _consume_cancel(recording_id)

            # ── Checkpoint state ─────────────────────────────────────────────────────
            # What a previous attempt got through, read from the row rather than inferred
            # from the files beside it. `signature` is this run's settings; parts recorded
            # under any other settings are not a checkpoint for this run and are discarded
            # rather than joined into a file with a seam in it.
            def _track_child(proc):
                """Register a conversion-phase ffmpeg that is not the encode itself - the
                re-mux that salvages a killed part, and the assembly that joins them.

                Both are minutes-long stream copies over multi-gigabyte files, so both are
                long-lived children and both must be reachable from tracked state: without
                this, a Cancel during the assembly sets the flag and kills nothing, and a
                shutdown orphans the child (CLAUDE.md subprocess-discipline). The registry is
                the conversion's own, because from every consumer's point of view these ARE
                the conversion - it is what kill_active_conversions() and
                request_cancel_conversion() already reach for.
                """
                with _active_lock:
                    _active_conversions[recording_id] = proc

            def _untrack_child():
                with _active_lock:
                    _active_conversions.pop(recording_id, None)

            signature = parts_signature(_build_cmd(), output_path) if resumable else None
            parts_done = 0
            source_covered = 0.0
            source_complete = False
            if not resumable:
                # A format or mode change since the last attempt (mp4 re-encode -> mkv copy,
                # say) leaves parts nothing will ever join. Cleared here rather than left to
                # sit: this path writes the final file directly, so nothing downstream looks
                # at them again.
                if discard_conversion_parts(output_path):
                    _persist_conversion_parts(recording_id)
            elif rec.conversion_parts_signature == signature:
                parts_done = rec.conversion_parts_done or 0
                source_covered = rec.conversion_source_covered_seconds or 0.0
                source_complete = bool(rec.conversion_source_complete)
            elif rec.conversion_parts_signature:
                n = discard_conversion_parts(output_path)
                log.warning('Recording %d: conversion settings changed since its %d encoded '
                            'part(s) were made - discarding them and re-encoding from the '
                            'start', recording_id, n)

                @retry_on_locked()
                def _commit_parts_invalidated(count=n):
                    r = db.session.get(Recording, recording_id)
                    set_conversion_parts(r)
                    add_recording_event(
                        recording_id, DIAGNOSTICS,
                        detail=f'Discarded {count} partly-encoded file(s) from an earlier '
                               f'attempt: the conversion settings have changed since they were '
                               f'made, and joining them would leave the finished recording '
                               f'inconsistent part-way through. Re-encoding from the start.',
                        extra={'kind': 'conversion_parts_invalidated', 'parts': count})
                    db.session.commit()

                _commit_parts_invalidated()

            # THE SIGNATURE IS REGISTERED BEFORE THE FIRST PART IS WRITTEN, and the order is
            # the whole point (CLAUDE.md "register the obligation in a commit that precedes the
            # phase-1 write"). It makes the only crash state a recorded signature with no part
            # yet - which the next attempt handles - instead of a part with nothing recorded
            # about what made it, which is unanswerable: adopting it would splice settings
            # nobody can check, and refusing it would throw away the first attempt's work in
            # the most common interruption there is. In the resumed case this re-writes what
            # was just read, which costs one commit and keeps the rule in one place.
            if resumable:
                _persist_conversion_parts(
                    recording_id, parts_done=parts_done,
                    source_covered=source_covered or None,
                    source_complete=source_complete, signature=signature)

            # A part file the row does not know about is what a service restart leaves: ffmpeg
            # wrote it, nothing got to commit. It is ADOPTED only after being verified from
            # scratch - re-muxed to drop its truncated tail and probed for where its last
            # decodable frame actually is - so the checkpoint is measured here and now rather
            # than assumed from the file's existence. Everything it establishes is committed
            # before any ffmpeg runs.
            if resumable and not source_complete:
                stray = part_path(output_path, parts_done + 1)
                if os.path.exists(stray):
                    log.warning('Recording %d: found a partly-encoded file no attempt recorded '
                                '(%s) - checking what of it survives',
                                recording_id, os.path.basename(stray))
                    try:
                        kept = finalize_part(stray, ffmpeg_path, scratch_key=recording_id,
                                             interval=interval,
                                             pre_output_timeout=pre_output_timeout,
                                             stall_seconds=stall_seconds,
                                             label=f'Recording {recording_id} part',
                                             on_spawn=_track_child)
                    finally:
                        _untrack_child()
                    if kept:
                        parts_done += 1
                        source_covered += kept
                        _persist_conversion_parts(
                            recording_id, parts_done=parts_done, source_covered=source_covered,
                            source_complete=False, signature=signature)

                        @retry_on_locked()
                        def _commit_part_adopted(where=source_covered, n=parts_done):
                            add_recording_event(
                                recording_id, DIAGNOSTICS,
                                detail=f'Resuming the conversion rather than restarting it: '
                                       f'{n} part(s) already encoded, covering the first '
                                       f'{fmt_duration(where, with_seconds=True)} of the '
                                       f'recording. Only the rest will be encoded.',
                                extra={'kind': 'conversion_part_adopted', 'parts': n,
                                       'source_covered_seconds': round(where, 3)})
                            db.session.commit()

                        _commit_part_adopted()
                    else:
                        try:
                            os.unlink(stray)
                        except OSError as exc:
                            log.warning('Could not delete unusable conversion part %s: %s',
                                        stray, exc)

            while True:
                # A cancel that landed between attempts - during the salvage re-mux, or while
                # the restart event was being written - has no attempt of its own to be
                # noticed by, so it is caught here before another encode starts.
                if _consume_cancel(recording_id):
                    cancelled = True
                    break

                if source_complete:
                    # Every frame is already encoded and only the assembly is left - which is
                    # the state a service restart during the join leaves behind. Re-encoding
                    # here would throw away the whole job to redo a stream copy.
                    conversion_ok = True
                    break

                part_file = part_path(output_path, parts_done + 1) if resumable else output_path
                start_at = source_covered if resumable else 0.0
                cmd = _build_cmd(audio_copy=audio_copy_fallback, dest=part_file,
                                 start_at=start_at)
                if start_at:
                    log.info('Recording %d conversion resuming at %s into the source '
                             '(part %d): %s', recording_id,
                             fmt_duration(start_at, with_seconds=True), parts_done + 1,
                             ' '.join(cmd))
                else:
                    log.info('Conversion command: %s', ' '.join(cmd))

                result = run_conversion_supervised(
                    app, recording_id, cmd, part_file,
                    expected_duration=expected_duration, pre_output_timeout=pre_output_timeout,
                    interval=interval, stall_seconds=stall_seconds,
                    collision_policy=collision_policy,
                    collision_multiplier=collision_multiplier,
                    source_offset=start_at,
                )
                # A user cancel (request_cancel_conversion killed the ffmpeg) reads as a
                # death; the flag distinguishes it so we abort instead of restarting.
                if _consume_cancel(recording_id):
                    cancelled = True
                    break

                # A mid-run collision never gets here: run_conversion_supervised suspends the
                # ffmpeg and continues it in place, so the attempt does not end and the loop
                # is not re-entered (dev/changelog/952). What reaches this point is only ever
                # a real outcome - success, death, stall - which is why the restart budget
                # below can be spent without checking whether the attempt merely yielded.
                decode_errors = max(decode_errors, result.decode_errors or 0)

                if result.success:
                    conversion_ok = True
                    if resumable:
                        # A part ffmpeg closed itself needs no repair - it has a proper
                        # trailer and every fragment is complete - so it is recorded as-is.
                        # source_complete is the fact that stops a restart during the join
                        # below from re-encoding a sliver off the end of the source.
                        parts_done += 1
                        source_covered = max(source_covered, result.out_time or source_covered)
                        source_complete = True
                        _persist_conversion_parts(
                            recording_id, parts_done=parts_done,
                            source_covered=source_covered, source_complete=True,
                            signature=signature)
                    break

                last_error = result.error_msg or result.reason

                if resumable:
                    # THE KILLED PART IS SALVAGED BEFORE ANYTHING ELSE HAPPENS. Re-muxing it
                    # drops the fragment that was mid-write when it died and hands back where
                    # its last decodable frame really is - the only honest splice point, and
                    # the reason the next attempt's -ss is trustworthy. A part with nothing
                    # usable in it is deleted and the offset is left where it was, so the
                    # worst case is re-encoding that stretch rather than skipping it.
                    try:
                        kept = finalize_part(part_file, ffmpeg_path, scratch_key=recording_id,
                                             interval=interval,
                                             pre_output_timeout=pre_output_timeout,
                                             stall_seconds=stall_seconds,
                                             label=f'Recording {recording_id} part',
                                             on_spawn=_track_child)
                    finally:
                        _untrack_child()
                    if kept:
                        parts_done += 1
                        source_covered += kept
                        _persist_conversion_parts(
                            recording_id, parts_done=parts_done,
                            source_covered=source_covered, source_complete=False,
                            signature=signature)
                        log.info('Recording %d: kept %s of encoding from the attempt that '
                                 '%s - %s of the source is now encoded across %d part(s)',
                                 recording_id, fmt_duration(kept, with_seconds=True),
                                 result.reason, fmt_duration(source_covered, with_seconds=True),
                                 parts_done)
                    else:
                        try:
                            os.unlink(part_file)
                        except OSError:
                            pass  # nothing usable in it; best-effort removal
                        log.warning('Recording %d: the attempt that %s left nothing that can '
                                    'be kept - the next one re-encodes from %s',
                                    recording_id, result.reason,
                                    fmt_duration(source_covered, with_seconds=True))

                # A restart re-reads the same static .ts, so a defect IN THAT FILE stops every
                # attempt at the same source position. Recording 4 died four times at
                # 02:53:02.06 with byte-identical output sizes, burning ~40 minutes of CPU to
                # fail the same way (dev/changelog/799). Retrying is only ever worth it for a
                # transient cause - a killed process, a blip on the storage mount - which
                # lands somewhere new each time. The tolerance is one poll interval: two
                # attempts that stop within a single progress sample of each other are the
                # same stop, not a coincidence.
                #
                # ConversionResult.out_time is an absolute source position (a resumed
                # attempt's -ss is added back), which is what keeps this comparison meaningful
                # once two attempts no longer start from the same place.
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
                    # THE FALLBACK INVALIDATES EVERY PART ALREADY ENCODED, and there is no way
                    # around it: the parts hold an AAC track this attempt would copy the
                    # source's own bytes into, so one codec config would have to cover two
                    # differently-encoded halves and the seam is undecodable in players. The
                    # signature is what expresses that - recomputing it here is the same
                    # judgment the resume path above makes, applied the moment the settings
                    # change rather than at the next restart. The cost is the re-encoding that
                    # checkpointing had saved, which is a worse outcome than resuming and a
                    # better one than a file with a hole in it.
                    parts_discarded = discard_conversion_parts(output_path) if resumable else 0
                    parts_done = 0
                    source_covered = 0.0
                    source_complete = False
                    if resumable:
                        signature = parts_signature(_build_cmd(audio_copy=True), output_path)
                    # prev_death_out_time is deliberately kept: if a run that decodes
                    # nothing still stops at the same offset, nothing will get past it.
                    attempt += 1
                    log.warning('Recording %d conversion died twice at %.2fs into the source - '
                                'retrying with audio stream-copied instead of re-encoded '
                                '(%d encoded part(s) discarded)',
                                recording_id, result.out_time, parts_discarded)

                    @retry_on_locked()
                    def _commit_audio_fallback_event(where=result.out_time, n=attempt,
                                                     dropped=parts_discarded, sig=signature):
                        r = db.session.get(Recording, recording_id)
                        r.conversion_attempts = n
                        # The NEW signature is registered here, not merely cleared: this is
                        # the same registration the phase does before its first part, and it
                        # has to happen before this attempt writes one. Clearing alone would
                        # leave a copied-audio part on disk with nothing recorded about it,
                        # and a service restart would then adopt it under the re-encoded-audio
                        # signature it recomputes - the precise splice this signature exists
                        # to prevent.
                        set_conversion_parts(r, signature=sig)
                        add_recording_event(
                            recording_id, CONVERSION_RESTARTED,
                            detail=f'Conversion stopped twice at the same point '
                                   f'({fmt_duration(where, with_seconds=True)} in) - the source '
                                   f'is damaged there. Retrying with the audio copied instead '
                                   f'of re-encoded, which does not decode it.'
                                   + (f' The {dropped} part(s) already encoded cannot be joined '
                                      f'onto audio copied this way, so they were discarded and '
                                      f'this attempt starts from the beginning.'
                                      if dropped else ''))
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
                def _commit_restart_event(n=attempt, reason=result.reason,
                                          covered=source_covered, nparts=parts_done):
                    # What the next attempt will actually do, not a generic "restarting": a
                    # resume that says "restarting" reads as the hours-thrown-away behavior
                    # this replaced, and the difference is the whole point of the feature.
                    where = (f' Resuming from {fmt_duration(covered, with_seconds=True)} in - '
                             f'the {nparts} part(s) already encoded are kept.'
                             if covered > 0 else '')
                    db.session.add(RecordingEvent(
                        recording_id=recording_id,
                        event_type=CONVERSION_RESTARTED,
                        detail=f'Conversion {_RESTART_REASON_PHRASE.get(reason, reason)}; '
                               f'restarting (attempt {n} of {max_attempts}).{where}',
                    ))
                    db.session.commit()

                _commit_restart_event()
                ev.publish(recording_id, CONVERSION_RESTARTED,
                           {'status': REC_STATUS_CONVERTING, 'attempt': attempt, 'max': max_attempts})

            if conversion_ok and resumable:
                # ── Assembly ─────────────────────────────────────────────────────────
                # Every frame is encoded; what exists on disk is N fragmented parts, and this
                # turns them into the one +faststart file the rest of the app expects. It is
                # not an extra pass - it is the pass +faststart was already doing at the end
                # of every conversion, now doing the join as well.
                parts = existing_part_paths(output_path, parts_done)
                joined_bytes_in = 0
                for _p in parts:
                    try:
                        joined_bytes_in += os.path.getsize(_p)
                    except OSError:
                        pass  # sizes only drive the progress percentage and the event text

                def _publish_join(wall, out_time, size):
                    # Byte progress against the parts' combined size. The same field the
                    # encode publishes and the same question it answers - how far through is
                    # this conversion - measured the way the assembly phase can measure it.
                    # A percentage frozen at 100 for the ten minutes a multi-gigabyte join
                    # takes is exactly the number nobody can explain.
                    pct = None
                    if joined_bytes_in and size:
                        pct = max(0.0, min(100.0, size / joined_bytes_in * 100.0))
                    _persist_conversion_snapshot(recording_id, pct, size, None)
                    ev.publish(recording_id, 'CONVERSION_PROGRESS', {
                        'status': REC_STATUS_CONVERTING, 'pct': pct, 'out_size': size or None,
                        'eta_seconds': None,
                    })

                @retry_on_locked()
                def _commit_join_started(n=len(parts), total=joined_bytes_in):
                    # THE SPLICE COST IS NAMED, not left for someone to find by comparing
                    # durations. Measured on this box over a real 1080p59.94 capture: a join
                    # loses two frames and leaves one duplicated timestamp at the seam, so the
                    # finished file runs 0.033s different from an uninterrupted conversion of
                    # the same source. That is a thirtieth of a second against the hours of
                    # encoding the parts represent, which is why it is the right trade - and
                    # saying so is what makes it a disclosed trade rather than a silent one.
                    add_recording_event(
                        recording_id, DIAGNOSTICS,
                        detail=f'Assembling the converted file from {n} encoded part(s) '
                               f'({_fmt_bytes(total)}) - the conversion was interrupted and '
                               f'resumed rather than restarted. Each of the {n - 1} join(s) '
                               f'costs about two frames at the seam, so the finished file may '
                               f'run a few hundredths of a second short.',
                        extra={'kind': 'conversion_parts_join', 'parts': n,
                               'splices': n - 1, 'bytes_in': total})
                    db.session.commit()

                if parts_done > 1:
                    # Said out loud only when the conversion actually was interrupted. A
                    # single-part join is the ordinary ending of every conversion and needs
                    # no event of its own.
                    _commit_join_started()

                try:
                    join = join_conversion_parts(
                        parts, output_path, ffmpeg_path, scratch_key=recording_id,
                        interval=interval,
                        pre_output_timeout=pre_output_timeout, stall_seconds=stall_seconds,
                        label=f'Recording {recording_id} assembly',
                        on_progress=_publish_join, on_spawn=_track_child)
                finally:
                    _untrack_child()

                # A Cancel during the assembly kills its ffmpeg through the registry above,
                # which reads here as a failed join. The flag is what tells the two apart, and
                # it is consumed on the same terms the restart loop consumes it on. This whole
                # block sits ABOVE the cancelled handler so that one terminal path serves both
                # a cancel during the encode and a cancel during the assembly.
                if _consume_cancel(recording_id):
                    cancelled = True
                    conversion_ok = False
                elif not join.success:
                    # What a finished assembly of this conversion would have to run to: the
                    # span the parts covered, or the source's own duration when an attempt
                    # finished without the supervisor ever reporting an out_time.
                    adopt_span = source_covered or expected_duration or 0.0
                    if (join.reason == 'missing_part'
                            and _adoptable_assembly(output_path, adopt_span)):
                        # THE FINISHED FILE IS ADOPTED, NOT DELETED. Reaching here means the
                        # row records a complete encode whose parts are gone while a
                        # probe-clean file of the right length sits at the output path -
                        # which is exactly what a crash between the discard and the
                        # completion commit used to leave, and is not a state the deletion
                        # below can improve. Destroying an hours-long re-encode's finished
                        # output to satisfy a bookkeeping check is the opposite of completing
                        # the recording at almost all costs. Gated on missing_part alone:
                        # 'no_parts' cannot co-occur with a recorded complete encode (the two
                        # move in one commit), and 'no_space' means the parts are still on
                        # disk and the file at the output path is a half-written assembly.
                        log.warning('Recording %d: the encoded parts are gone but the '
                                    'assembled file is already at %s and probes clean - '
                                    'adopting it rather than re-encoding',
                                    recording_id, output_path)

                        @retry_on_locked()
                        def _commit_output_adopted(covered=adopt_span):
                            add_recording_event(
                                recording_id, DIAGNOSTICS,
                                detail=f'Adopted the converted file already on disk: the '
                                       f'encoded parts it was assembled from are gone, but '
                                       f'the finished file is there and covers the expected '
                                       f'{fmt_duration(covered, with_seconds=True)}. An '
                                       f'earlier attempt was interrupted between deleting '
                                       f'the parts and recording that the conversion was '
                                       f'done.',
                                extra={'kind': 'conversion_output_adopted',
                                       'source_covered_seconds': round(covered, 3)})
                            db.session.commit()

                        _commit_output_adopted()
                    else:
                        # THE PARTS ARE KEPT. They are the only copy of the encoding, the
                        # assembly is a stream copy that costs minutes rather than hours, and
                        # a Retry re-enters this function, finds source_complete still
                        # recorded and goes straight back to the join. Deleting them here
                        # would turn a recoverable failure into the hours-lost outcome this
                        # whole feature exists to end.
                        conversion_ok = False
                        last_error = join.error_msg or join.reason
                        log.error('Recording %d: converted every frame but could not '
                                  'assemble the final file: %s', recording_id, last_error,
                                  extra={'recording_id': recording_id})
                        if os.path.exists(output_path):
                            try:
                                os.unlink(output_path)
                            except OSError as exc:
                                log.warning('Could not delete the partial assembly %s: %s',
                                            output_path, exc)

            if cancelled:
                # User cancelled: keep the source .ts (Retry conversion works from CANCELLED),
                # delete the partial .mp4 (killed mid-write, no moov atom - unreadable garbage).
                if output_path != ts_path and os.path.exists(output_path):
                    try:
                        os.unlink(output_path)
                    except OSError as exc:
                        log.warning('Could not delete partial conversion output %s: %s', output_path, exc)
                # The parts go with it. A cancel is the user saying they do not want this
                # conversion, not asking for it to be paused - and a Retry after one resets
                # the attempt budget, so leaving multi-gigabyte parts behind to resume from
                # would be keeping work nobody asked to keep (CLAUDE.md teardown).
                discard_conversion_parts(output_path)

                @retry_on_locked()
                def _commit_conversion_cancelled():
                    r = db.session.get(Recording, recording_id)
                    r.status = REC_STATUS_ABORTED
                    r.cancel_reason = CANCEL_DURING_CONVERSION
                    r.completed_at = datetime.utcnow()
                    set_conversion_parts(r)
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
                    # THE CHECKPOINT IS CLEARED IN THE SAME COMMIT THAT NAMES THE OUTPUT, and
                    # the parts are deleted only after it returns. Clearing it separately -
                    # or, as this did, unlinking the parts first and recording it afterwards -
                    # leaves a window where the row describes N parts that no longer exist,
                    # and the next attempt believes that checkpoint and deletes the finished
                    # file to satisfy it (dev/docs/BUGS.md 2026-09-15 @ 09:23:59 PM ET). One
                    # commit for one fact means the only crash state left is an orphan part
                    # beside a correctly-finished recording, which recording_disk_paths()
                    # already enumerates for teardown.
                    set_conversion_parts(r)
                    db.session.add(RecordingEvent(
                        recording_id=recording_id,
                        event_type=CONVERSION_DONE,
                        detail=f'Conversion complete: {os.path.basename(output_path)} ({_fmt_bytes(converted_size)})',
                    ))
                    db.session.commit()

                _commit_conversion_done()
                # Costs no extra disk headroom over the old ordering: the parts and the
                # finished output already coexisted for the whole length of the join.
                discard_conversion_parts(output_path)
                # The conversion that had given up is over, so its alert is describing a
                # state that no longer exists. Keyed on the recording rather than the
                # (type, source) pair because CONVERSION_FAILED is raised under two
                # different sources - 'postprocessor' here and 'scheduler' from startup
                # recovery - and only the id names them both (dev/changelog/930).
                alerts.dismiss_open_alerts_for_recording(recording_id, 'CONVERSION_FAILED')
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
                # The encoding that did succeed is still on disk and a Retry picks up from it,
                # so say so: an operator deciding whether retrying is worth hours of CPU is
                # entitled to know it is not (dev/changelog/955).
                if resumable and parts_done and not repeated_at:
                    if source_complete:
                        give_up_msg += (' - every frame was converted and only the final '
                                        'assembly failed, so a retry picks up from there')
                    else:
                        give_up_msg += (f' - the first '
                                        f'{fmt_duration(source_covered, with_seconds=True)} is '
                                        f'already converted and kept, so a retry resumes from '
                                        f'there rather than starting over')

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
                    r.failure_reason = FAILURE_CONVERSION_FAILED
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
                              extra={'recording_id': recording_id, 'already_alerted': True})
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
                          extra={'recording_id': recording_id, 'already_alerted': True})
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
                alerts.create_alert(
                    'RECORDING_MOVE_FAILED',
                    f'Move failed: {rec_name}',
                    body=(f'The recording finished and was converted, but could not be moved '
                          f'to its destination: {move_error}. The file is still at '
                          f'{current_path}, so nothing was lost. Nothing retries a move on '
                          f'its own, so this clears when the recording is post-processed '
                          f'again or deleted.'),
                    source='postprocessor',
                    recording_id=recording_id,
                )
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

                # from_path is what teardown reads: once output_path names the destination,
                # nothing else remembers the folder a kept .ts source and any conversion
                # scratch were left in (recorder.recording_disk_paths, dev/changelog/1041).
                moved_from = current_path if dest_path != current_path else None

                @retry_on_locked()
                def _commit_moved():
                    r = db.session.get(Recording, recording_id)
                    r.output_path = dest_path
                    add_recording_event(
                        recording_id, FILE_MOVED, detail=move_detail,
                        extra={'from_path': moved_from} if moved_from else None)
                    db.session.commit()

                _commit_moved()
                # The file is where it belongs now, so an earlier move failure for this
                # recording is over. This is the only path that can clear it - there is no
                # move retry, so it takes a fresh post-process run to get here.
                alerts.dismiss_open_alerts_for_recording(recording_id, 'RECORDING_MOVE_FAILED')
                log.info('Recording %d: %s', recording_id, move_detail)
                current_path = dest_path

        # ── Metadata sidecar ──────────────────────────────────────────────────
        # After the move and before the post-script: collision_safe_dest() can rename the
        # video, and the sidecar has to carry the name the file actually ended up with or a
        # media server reads it onto the wrong item. Running before the script also means a
        # user's own script finds the sidecar already there.
        #
        # Deliberately not inside a try that swallows - write_sidecar() handles its own
        # failures, logs an event naming the reason, and never raises at a caller that is
        # one step from marking this recording COMPLETED.
        from .metadata_sidecar import write_sidecar
        write_sidecar(recording_id, current_path, cfg)

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

    Counted from rows that recorded bytes and were not excluded, which is the same test
    concatenator.joinable_segments() applies - and rows are all that survives, because a
    successful concat deletes the segment files. Above 1 means the timestamps were
    regenerated at every join, which is what blinds the timeline scan's gap count
    (dev/changelog/433).

    Under-counting is the safe direction: it claims less blindness than there is, rather
    than explaining away a gap count that was in fact honest.
    """
    from .database import RecordingSegment
    return RecordingSegment.query.filter(
        RecordingSegment.recording_id == recording_id,
        RecordingSegment.bytes_recorded > 0,
        RecordingSegment.excluded_reason.is_(None),
    ).count()


def _segment_capture_rates(recording_id):
    """[(content_seconds, probe_fps), ...] for the segments that were joined - what
    probe.assess_seek_damage() needs to measure the frame deficit against the rate the
    footage was actually captured at rather than the one in the concatenated file's header.

    Same row test as _joined_segment_count (bytes recorded, not excluded), plus the two
    fields the weight needs. A segment with no probe_fps contributes nothing: the watchdog probes a segment
    that is still growing and leaves a field it could not read NULL rather than guessing, and
    inventing a rate for it would put the corrected number back where the wrong one was.

    THE WEIGHT IS CONTENT DURATION, NOT WALL CLOCK, and the difference is not academic. The
    deficit counts frames against content time, so the blended rate has to be the rate that
    each of the file's own seconds was captured at - how long a segment took to arrive is a
    fact about the network, not about the footage. The two diverge whenever a provider sends
    faster than real time: a placeholder clip is 600s of 30 fps video that arrives in 5s, so
    by wall clock it weighs 5 against a 59.94 fps recording's thousands and moves the blend
    from 59.94 to 59.87 while contributing 18,000 frames at half that rate. Everything those
    frames are short by is then reported as missing video. Measured on the two recordings
    that prompted this: 1800.9s of "missing" video became 23.9s, and 2354.1s became 1.3s,
    both flipping DAMAGED to OK and both having been sent into a full re-encode by the wrong
    number (dev/changelog/962).

    recording_segments.content_duration_seconds is the column (migration 56), filled by
    concatenator._measure_segment_content_durations() from a header-only ffprobe of each
    closed segment file immediately before the join - so it is already on the row by the time
    the analysis phase asks, on the single-segment rename path as well as the concat.

    The wall span is the fallback, for the one case the column cannot answer: a segment whose
    file the probe could not read keeps its NULL, which honestly means "not measured", and
    that includes every segment captured before migration 56 existed. Falling back is wrong
    by a start-up second or two per segment on a normal feed, which is the error this function
    shipped with and lived with (dev/changelog/866); it is only the fast-delivery case that
    made it catastrophic, and a segment fast enough to matter is a segment the probe read.
    """
    from . import db
    from .database import RecordingSegment
    rows = db.session.query(
        RecordingSegment.started_at, RecordingSegment.ended_at, RecordingSegment.probe_fps,
        RecordingSegment.content_duration_seconds,
    ).filter(
        RecordingSegment.recording_id == recording_id,
        RecordingSegment.bytes_recorded > 0,
        RecordingSegment.excluded_reason.is_(None),
    ).all()
    rates = []
    for started_at, ended_at, probe_fps, content_seconds in rows:
        if not probe_fps:
            continue
        if content_seconds and content_seconds > 0:
            rates.append((content_seconds, probe_fps))
            continue
        if not started_at or not ended_at:
            continue
        span = (ended_at - started_at).total_seconds()
        if span > 0:
            rates.append((span, probe_fps))
    return rates


#: The one spelling of the timeline scan's event detail, so the memo below can strip it back
#: off rather than a second literal drifting from the one _scan_recording_timeline writes.
_TIMELINE_SCAN_DETAIL_PREFIX = 'Timeline scan: '


def _analysis_attempt_count(recording_id) -> int:
    """How many times the post-capture analysis phase has announced itself for this
    recording. Read to tell a first run from a redo, never to decide whether the phase may be
    skipped - that is analysis_completed_at's job and nothing else's (dev/changelog/951)."""
    from .database import RecordingEvent, POSTCAPTURE_ANALYSIS_STARTED
    return (RecordingEvent.query
            .filter_by(recording_id=recording_id, event_type=POSTCAPTURE_ANALYSIS_STARTED)
            .count())


def _recorded_timeline_scan(recording_id):
    """_scan_recording_timeline's (damaged, metrics, summary) rebuilt from what that scan
    recorded, or None when no scan was ever recorded for this recording.

    The memo has to survive a process restart, not just the call chain: without this, gating
    the analysis phase would simply move the full-file ffprobe from the analysis phase into
    the re-encode decision two hundred lines below, and a resumed recording would still pay
    the 266s-434s scan this gate exists to stop paying (dev/changelog/951).

    Rebuilt, not re-measured. Every value the scan produced is already durable - the five
    stats it promotes to columns, and the rest in its DIAGNOSTICS event's extra_data, which
    is a strict partition (CLAUDE.md, "a stat with a column does not also go in extra_data").
    `timeline_damaged` is deliberately the stored verdict rather than a fresh evaluation: it
    records what the app decided and acted on, which is the question a resume is asking.
    """
    import json
    from . import db
    from .database import Recording, RecordingEvent, DIAGNOSTICS

    events = (RecordingEvent.query
              .filter_by(recording_id=recording_id, event_type=DIAGNOSTICS)
              .order_by(RecordingEvent.id.desc())
              .all())
    for evt in events:
        try:
            extra = json.loads(evt.extra_data) if evt.extra_data else {}
        except (ValueError, TypeError):
            continue
        if not isinstance(extra, dict) or extra.get('kind') != 'timeline_scan':
            continue

        detail = evt.detail or ''
        summary = (detail[len(_TIMELINE_SCAN_DETAIL_PREFIX):]
                   if detail.startswith(_TIMELINE_SCAN_DETAIL_PREFIX) else detail) or None
        if extra.get('scan_failed'):
            # The scan ran and produced nothing. Recorded as such, and replayed as such -
            # re-probing would be guessing that a failure was transient.
            return False, {}, summary

        rec = db.session.get(Recording, recording_id)
        metrics = {
            'gap_count':        rec.timeline_gap_count if rec else None,
            'gap_seconds':      rec.timeline_gap_seconds if rec else None,
            'max_gap_seconds':  rec.timeline_max_gap_seconds if rec else None,
            'deficit_seconds':  rec.timeline_deficit_seconds if rec else None,
        }
        for k in ('gap_basis', 'gap_threshold', 'backward_count', 'missing_seconds',
                  'span_seconds', 'packet_count', 'fps', 'capture_fps_values', 'deficit_fps'):
            if k in extra:
                metrics[k] = extra[k]
        damaged = bool(rec.timeline_damaged) if rec is not None else False
        return damaged, metrics, summary
    return None


def _scan_recording_timeline(recording_id, ts_path, analysis_plan=None):
    """Scan the concatenated .ts for timeline damage, persist what was measured, and
    return assess_seek_damage()'s (damaged, metrics, summary).

    analysis_plan is analysis_pass_plan()'s list when this runs as part of the ANALYZING
    phase, which is what makes the read publish its progress; omitted, the scan behaves
    exactly as before and reports nothing.

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
    with _analysis_pass(recording_id, ANALYSIS_PASS_TIMELINE, ts_path, analysis_plan) as hook:
        damaged, metrics, summary = assess_seek_damage(
            ts_path, joined_segments=joined,
            segment_rates=_segment_capture_rates(recording_id), on_progress=hook)
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
                            detail=f'{_TIMELINE_SCAN_DETAIL_PREFIX}{summary}', extra=extra)
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
    # Excluded rows are out of the average AND out of the flagging. They are not in the file
    # this scan describes, and a placeholder in particular reads as a 23 Mbps segment here -
    # 14MB over 5 wall seconds - which both pulls the recording's own average up and means
    # the clip itself is never flagged. That is why the near-empty scan reported "no
    # near-empty segments detected" on two recordings that were an hour of black
    # (dev/changelog/957). Distinct feature from the discard, and the two must not read as
    # the same thing: this one finds a slate inside a segment that was kept.
    rows = db.session.query(
        RecordingSegment.segment_number, RecordingSegment.bytes_recorded,
        RecordingSegment.started_at, RecordingSegment.ended_at
    ).filter(
        RecordingSegment.recording_id == recording_id,
        RecordingSegment.excluded_reason.is_(None),
    ).all()

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
            # An excluded segment keeps its NULL - it was never evaluated, and writing False
            # would claim this scan had looked at it and cleared it.
            if seg.excluded:
                continue
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


def _gather_recording_health(recording_id, ts_path, rec, cfg, analysis_plan=None):
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
        with _analysis_pass(recording_id, ANALYSIS_PASS_HEALTH, ts_path, analysis_plan) as hook:
            probe = parse_ffprobe(ts_path, on_progress=hook)
        if not probe:
            # One empty dict, two meanings, and this event is the only record of the
            # recording's missing numbers that survives on the artifact - so it has to say
            # which. probe_failed described the file; probe_unavailable describes the
            # install, and nothing about the capture can be concluded from it
            # (dev/changelog/911).
            from .toolchain import ffprobe_missing
            if ffprobe_missing():
                log.warning('Recording %d: no ffprobe on this machine, so %s has no health '
                            'data', recording_id, ts_path)
                return None, {
                    'detail': 'Capture health check skipped: ffprobe is not installed, so '
                              'the recorded file could not be inspected. This says nothing '
                              'about the recording itself - see Maintenance > External '
                              'tools.',
                    'extra': {'kind': 'capture_health', 'probe_unavailable': True},
                }
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
        # The two facts this string exists to make unmissable, reported side by side and
        # never netted: gap time is wall clock when no segment was capturing, and the
        # content figure is how the delivered length compares against the wall clock the
        # capture actually ran for. They used to be one subtraction clamped at zero, which
        # let a buffered feed's replay cancel real gap time - recording 14 printed
        # "missing 0s (0%)" over 135.7s of gaps (dev/changelog/942). Same quantities as
        # Recording.capture_gap_seconds / content_vs_capture_seconds; they ride in the
        # detail string, never in extra_data, because every input already has a column
        # (CLAUDE.md §Measurements).
        covered = rec.covered_capture_seconds
        gap_secs = max(0.0, adjusted_secs - covered)
        gap_txt = (
            f", {gap_secs:.0f}s not capturing"
            f"{'' if adjusted_secs <= 0 else f' ({gap_secs / adjusted_secs * 100:.0f}%)'}"
        )
        vs_capture = None if duration is None else duration - covered
        accounting_txt = gap_txt + (
            '' if vs_capture is None else
            f", content {vs_capture:+.0f}s against {covered:.0f}s of capture time"
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
            f"{accounting_txt}); "
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
