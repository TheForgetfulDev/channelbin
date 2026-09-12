"""
WatchdogThread: polls segment file growth, detects stalls, handles restarts.
One thread per active recording. Publishes SSE events via app.events.
"""
import logging
import os
import time
import threading
from datetime import datetime, timedelta
from typing import NamedTuple

from .database import add_recording_event, REC_STATUS_FAILED, REC_STATUS_RETRYING
from .db_utils import retry_on_locked
from .probe import parse_ffprobe
from .proc_utils import GrowthMonitor, terminate_or_kill, wait_for_file_data

log = logging.getLogger(__name__)

# Capture-time format probe (DESIGN.md section 5): wait for this much data
# before the header read, and give up after this many empty results.
_PROBE_MIN_BYTES = 2 * 1024 * 1024
_PROBE_MAX_ATTEMPTS = 3

# Dead-stream retry backoff (Product Principle 2), deliberately hardcoded (2026-08-12) - only
# the total attempt cap (watchdog.dead_stream_max_retry_attempts) is configurable, not this
# cadence.
_DEAD_STREAM_RETRY_DELAYS_MINUTES = [1, 2, 5, 15]  # then 60 for every attempt after the 4th


def _dead_stream_retry_delay_minutes(attempt_number: int) -> int:
    """1-indexed attempt number -> minutes to wait before that attempt fires."""
    idx = attempt_number - 1
    if idx < len(_DEAD_STREAM_RETRY_DELAYS_MINUTES):
        return _DEAD_STREAM_RETRY_DELAYS_MINUTES[idx]
    return 60


def stalls_within_window(times, now_mono: float, window_seconds: float) -> list:
    """The stall timestamps in `times` still inside a rolling window ending at `now_mono`.

    The stall-rate demotion trigger (dev/changelog/889) is a RATE, not a total: a flat
    total cannot tell a bad feed from a long recording, since 5 stalls is a broken feed in
    an hour and a healthy one across four. Note which way the window moves the trigger -
    widening it LOOSENS it, because the count is unchanged and there is more time to reach
    it. Measured against recording 14's real stall times, 3-in-30 first fires at +24.2 min
    and 3-in-10 not until +65.0.
    """
    return [t for t in times if t >= now_mono - window_seconds]


def _first_line(tail: str) -> str:
    """The first line of a (possibly multi-line, already credential-masked) stderr
    tail - the single most specific thing ffmpeg said, for folding into a one-line
    event/alert message. '' for an empty tail."""
    return tail.split('\n', 1)[0] if tail else ''


class WatchdogThresholds(NamedTuple):
    """The tunables one recording's watchdog runs on. A NamedTuple rather than a bare
    tuple because the arity is now past what a reader can keep straight positionally, and
    because a field added here should not silently break an existing unpack - both callers
    read what they need by name."""
    stall_timeout: int
    restart_delay: int
    max_failures: int
    stall_move_count: int
    stall_move_window_minutes: int


def _resolve_watchdog_thresholds(cfg, recording_id):
    """stall/restart/failure thresholds: recording's profile overrides first, global
    config.yaml second - same fallback convention as Account.max_connections
    (see app/connection_limits.py)."""
    from . import db
    from .database import Recording

    # This thread holds one long-lived session for its whole lifetime (single
    # app_context wrapping the entire run() loop) - without expiring, a Recording/
    # RecordingProfile row loaded on an earlier iteration would stay cached in the
    # identity map and never pick up an edit committed from a web request's separate
    # session, breaking the "profile edits apply live" behavior promised in the
    # profile edit page's banner.
    db.session.expire_all()

    stall_timeout = cfg['watchdog']['stall_timeout_seconds']
    restart_delay = cfg['watchdog']['restart_delay_seconds']
    max_failures = cfg['watchdog']['max_consecutive_failures']
    stall_move_count = cfg['watchdog']['stall_move_count']
    stall_move_window = cfg['watchdog']['stall_move_window_minutes']

    rec = db.session.get(Recording, recording_id)
    profile = rec.profile if rec else None
    if profile is not None:
        if profile.stall_timeout_seconds is not None:
            stall_timeout = profile.stall_timeout_seconds
        if profile.restart_delay_seconds is not None:
            restart_delay = profile.restart_delay_seconds
        if profile.max_consecutive_failures is not None:
            max_failures = profile.max_consecutive_failures
        if profile.stall_move_count is not None:
            stall_move_count = profile.stall_move_count
        if profile.stall_move_window_minutes is not None:
            stall_move_window = profile.stall_move_window_minutes
    return WatchdogThresholds(stall_timeout, restart_delay, max_failures,
                              stall_move_count, stall_move_window)


class WatchdogThread(threading.Thread):
    def __init__(self, recording_id: int, state, app):
        super().__init__(name=f'watchdog-{recording_id}', daemon=True)
        self.recording_id = recording_id
        self.state = state        # RecordingState from recorder.py
        self.app = app

    def run(self):
        from .config import load_config
        from . import db
        from .database import (
            Recording, RecordingSegment,
            STALL_DETECTED, RESTART_ATTEMPTED, RESTART_SUCCEEDED, RESTART_FAILED,
        )
        from . import events as ev

        with self.app.app_context():
            # Consecutive-early-failure tracking (dead-stream fast-fail), independent of
            # consecutive_failures - not reset by a "successful" restart that merely produced
            # a few bytes before dying again. Fast-fail detection for dead streams:
            # dev/changelog/105.
            early_fail_times = []  # monotonic timestamps
            # Segment number that a failed restart has ALREADY established is dead, handed
            # from the restart branch to the next outer-loop pass. See where it is consumed
            # below for why re-deriving it instead cost ~11s per dead segment.
            restart_no_data_segment = None
            # Stall-rate demotion (dev/changelog/889): monotonic timestamps of the stalls
            # charged to the member currently being recorded, and which member that is.
            # Keyed on the member rather than cleared at each failover site: a move that
            # arrives from any of the three give-up points below must not carry the old
            # feed's stalls onto the new one, and a .clear() at each of them would be four
            # places for the fifth site to forget.
            member_stall_times = []
            member_stall_channel_id = None

            log.info('Watchdog started for recording %d', self.recording_id)

            while not self.state.stop_event.is_set():
                # Refresh config each outer loop in case settings were changed
                cfg = load_config()
                poll = cfg['watchdog']['poll_interval_seconds']
                thresholds = _resolve_watchdog_thresholds(cfg, self.recording_id)
                stall_timeout = thresholds.stall_timeout
                restart_delay = thresholds.restart_delay
                max_failures = thresholds.max_failures
                early_fail_window = cfg['watchdog']['early_fail_window_seconds']
                early_fail_min_bytes = cfg['watchdog']['early_fail_min_bytes']
                early_fail_abort_count = cfg['watchdog']['early_fail_abort_count']
                early_fail_abort_window = cfg['watchdog']['early_fail_abort_window_seconds']

                seg_num = self.state.current_segment_num
                seg = RecordingSegment.query.filter_by(
                    recording_id=self.recording_id,
                    segment_number=seg_num,
                ).first()

                if seg is None or seg.ended_at is not None:
                    # Segment not ready yet; wait
                    self.state.stop_event.wait(timeout=1)
                    continue

                seg_path = seg.file_path
                monitor = GrowthMonitor()
                # Consumed once, for the one segment it was recorded against: a later
                # segment must be judged on its own growth, not on a stale verdict.
                restart_produced_no_data = (restart_no_data_segment == seg_num)
                restart_no_data_segment = None
                # Capture-time format probe: once per segment, as soon as it has
                # enough data for a stable header read (DESIGN.md section 5 - tech
                # info describes the original capture). Bounded attempts so a
                # header ffprobe can't return empty forever and hammer every poll.
                probe_attempts = 0 if seg.probe_resolution is None else _PROBE_MAX_ATTEMPTS

                # ── Inner poll loop: watch this segment ──────────────────────
                while not self.state.stop_event.is_set():
                    # Publish stats snapshot
                    self._publish_snapshot(cfg)

                    try:
                        current_size = os.path.getsize(seg_path)
                    except FileNotFoundError:
                        current_size = 0

                    if probe_attempts < _PROBE_MAX_ATTEMPTS and current_size >= _PROBE_MIN_BYTES:
                        probe_attempts += 1
                        # subprocess side effect - runs OUTSIDE the retry closure
                        info = parse_ffprobe(seg_path, count_packets=False, timeout=15)
                        if info.get('resolution') or info.get('audio_codec'):
                            seg_id = seg.id

                            @retry_on_locked()
                            def _record_probe_and_commit():
                                s = db.session.get(RecordingSegment, seg_id)
                                s.probe_resolution = info.get('resolution')
                                s.probe_fps = info.get('fps')
                                s.probe_audio_codec = info.get('audio_codec')
                                s.probe_audio_channels = info.get('audio_channels')
                                # Format profile of the ORIGINAL capture, from the same
                                # single probe - no extra ffprobe per segment. Verified on
                                # this machine that all seven read identically off a 2 MB
                                # partial file and the finished one, progressive H.264 and
                                # interlaced MPEG-2 alike, so a mid-capture read is not a
                                # guess (dev/changelog/335). NULL still means unknown.
                                s.probe_video_codec = info.get('video_codec')
                                s.probe_pix_fmt = info.get('pix_fmt')
                                s.probe_bit_depth = info.get('bit_depth')
                                s.probe_chroma_subsampling = info.get('chroma_subsampling')
                                s.probe_interlaced = info.get('interlaced')
                                s.probe_coded_resolution = info.get('coded_resolution')
                                s.probe_is_vfr = info.get('is_vfr')
                                s.probed_at = datetime.utcnow()
                                db.session.commit()

                            _record_probe_and_commit()
                            probe_attempts = _PROBE_MAX_ATTEMPTS
                            self._check_format_pin(seg_id, seg_num)

                    stalled_for = monitor.update(current_size)

                    # wait_for_file_data already proved this segment produced nothing, and
                    # GrowthMonitor started from scratch here needs another stall_timeout to
                    # reach the identical conclusion - the ~11s that put 17 of the 24
                    # zero-byte segment rows in the database at 21.1-21.2s (dev/changelog/435).
                    # Act on the verdict instead of re-deriving it.
                    #
                    # current_size is what makes this safe rather than a shortcut:
                    # wait_for_file_data polls every 2s, so a reconnect whose first byte
                    # landed just after it gave up is a LIVE segment, and killing it on a
                    # verdict that is now stale would break a capture that recovered.
                    restart_dead = restart_produced_no_data and current_size == 0
                    restart_produced_no_data = False

                    # A capture process that has already exited will never write another
                    # byte, so waiting out stall_timeout to "discover" that only throws the
                    # difference away. On recording 71 it cost ~15s x 75 stalls = 18.8 of
                    # the ~30 minutes lost (dev/changelog/429).
                    #
                    # stop_event - not poll() alone - is what separates "the feed died" from
                    # "we killed it": every deliberate teardown sets it before terminating
                    # (recorder.py::_teardown_active_ffmpeg, ::kill_all_active). Overloading
                    # a dead process into meaning "the stream died" would make every manual
                    # stop, pause, abort and service shutdown look like a stall and relaunch
                    # ffmpeg on the way out.
                    proc_exited = (
                        self.state.process is not None
                        and self.state.process.poll() is not None
                        and not self.state.stop_event.is_set()
                    )
                    if proc_exited:
                        # ffmpeg can flush bytes between the getsize above and its exit;
                        # re-read once so they still count toward bytes_recorded, matching
                        # the ordering wait_for_file_data documents for the same race.
                        try:
                            monitor.update(os.path.getsize(seg_path))
                        except OSError:
                            pass  # the size read above stands; a missing file is 0 either way

                    if stalled_for > stall_timeout or proc_exited or restart_dead:
                        if proc_exited:
                            log.warning(
                                'Recording %d: capture process for seg %d exited on its own '
                                '(%d bytes)', self.recording_id, seg_num, monitor.last_size,
                            )
                        elif restart_dead:
                            log.warning(
                                'Recording %d: seg %d was the restart that produced no data '
                                'within %ss - closing it without re-deriving that',
                                self.recording_id, seg_num, stall_timeout,
                            )
                        else:
                            log.warning(
                                'Recording %d: stall detected on seg %d (%.1fs, %d bytes)',
                                self.recording_id, seg_num, stalled_for, monitor.last_size,
                            )
                        # ── STALL HANDLING ──────────────────────────────

                        # Dead-stream classification: a segment that stalled quickly
                        # and produced very little data is a candidate "early failure" -
                        # independent of consecutive_failures, and NOT reset just because
                        # the next restart briefly produces a few bytes (see below).
                        seg_duration = (datetime.utcnow() - seg.started_at).total_seconds()
                        is_early_failure = (
                            seg_duration <= early_fail_window
                            and monitor.last_size < early_fail_min_bytes
                        )
                        if is_early_failure:
                            now_mono = time.monotonic()
                            early_fail_times.append(now_mono)
                            cutoff = now_mono - early_fail_abort_window
                            early_fail_times[:] = [t for t in early_fail_times if t >= cutoff]
                        else:
                            early_fail_times.clear()

                        # Kill current ffmpeg
                        terminate_or_kill(self.state.process)

                        # Read the exit code and stderr tail now that the process is
                        # confirmed dead, and OUTSIDE the retry closure below - collecting
                        # consumes the spool, so a lock-retry would re-read nothing and
                        # record an empty tail. A negative code here is our own SIGTERM
                        # above; a positive one means ffmpeg had already died on its own
                        # before we ever declared the stall (dev/changelog/430).
                        from .recorder import collect_segment_diagnostics, record_segment_diagnostics
                        exit_code, stderr_tail = collect_segment_diagnostics(self.recording_id)

                        # Update segment + recording stats. Wrapped as one retry unit
                        # so a retry redoes the read-modify-write together - the
                        # counters below rely on rec being re-fetched fresh each
                        # attempt, not incremented against a stale in-memory value.
                        # Two different things happened and the row says which: STALL_KILLED
                        # means we killed a process that had stopped writing, PROCESS_EXITED
                        # means ffmpeg was already gone when we looked. Same restart from
                        # here on, but a row that claims we killed a process we did not is
                        # exactly the kind of unexplainable number Product Principle 1 exists
                        # to prevent - and the exit code beside it will disagree.
                        # Three shapes reach here and the row says which. proc_exited wins a
                        # tie with restart_dead: it is the more specific fact and the exit
                        # code beside it is ffmpeg's own answer, which "produced no data"
                        # cannot give. Every state is named - a trailing else that rendered
                        # one of them would swallow the next one added.
                        if proc_exited:
                            exit_reason = 'PROCESS_EXITED'
                            stall_reason = 'process_exited'
                            stall_detail = (f'Capture process exited on its own at '
                                            f'{monitor.last_size} bytes - restarting')
                        elif restart_dead:
                            exit_reason = 'RESTART_NO_DATA'
                            stall_reason = 'restart_no_data'
                            stall_detail = (f'Restarted segment produced no data within '
                                            f'{stall_timeout}s - closed on that verdict '
                                            f'rather than waiting to re-derive it')
                        else:
                            exit_reason = 'STALL_KILLED'
                            stall_reason = 'no_growth'
                            stall_detail = (f'File stalled at {monitor.last_size} bytes '
                                            f'for {stalled_for:.1f}s')

                        @retry_on_locked()
                        def _record_stall_and_commit():
                            db.session.refresh(seg)
                            seg.ended_at = datetime.utcnow()
                            seg.exit_reason = exit_reason
                            seg.bytes_recorded = monitor.last_size
                            seg.stall_count = (seg.stall_count or 0) + 1
                            record_segment_diagnostics(self.recording_id, seg,
                                                       exit_code, stderr_tail)

                            r = db.session.get(Recording, self.recording_id)
                            r.total_stall_count += 1
                            # Downtime is wall-clock time with nothing being written, and
                            # this is the first of its two measurable halves: the window
                            # between the last byte and this stall being declared. It used
                            # to be `+= restart_delay`, which counted only the deliberate
                            # wait and so understated recording 71's loss 5x
                            # (dev/changelog/429). The other half - restart delay plus the
                            # reconnect wait - is banked once the restart resolves, below.
                            # Banking it here rather than at the end is what keeps every
                            # give-up and failover branch from silently dropping its share.
                            # Granular to one poll interval: GrowthMonitor starts its clock
                            # at the first poll that OBSERVED no growth, not at the last byte.
                            #
                            # On the restart_dead branch this is 0.0 and that is correct, not
                            # a dropped charge: those seconds were spent inside
                            # wait_for_file_data and are already banked as gap_seconds on the
                            # RESTART_FAILED row above. Charging stalled_for again would count
                            # the same window twice.
                            r.total_downtime_seconds += stalled_for
                            r.consecutive_failures += 1
                            if r.consecutive_failures > r.consecutive_failures_peak:
                                r.consecutive_failures_peak = r.consecutive_failures

                            add_recording_event(self.recording_id, STALL_DETECTED,
                                detail=stall_detail,
                                segment_number=seg_num,
                                extra={'bytes': monitor.last_size,
                                       'stall_duration': stalled_for,
                                       'stall_reason': stall_reason})

                            db.session.commit()
                            return r

                        rec = _record_stall_and_commit()

                        ev.publish(self.recording_id, STALL_DETECTED, {
                            'segment_number': seg_num,
                            'bytes_at_stall': monitor.last_size,
                            'stall_duration': stalled_for,
                            'stall_count': rec.total_stall_count,
                            'consecutive_failures': rec.consecutive_failures,
                        })

                        # Dead-stream fast-fail: independent trip-wire, checked before the
                        # normal max_consecutive_failures path since it's meant to catch
                        # exactly the case that path never accumulates for (each restart
                        # looks "successful" because the file briefly grew).
                        # Group-backed recordings try their next-best member at each
                        # give-up point instead of aborting; the abort paths run only
                        # once every member has failed (ffmpeg is already dead here -
                        # killed in the stall handling above).
                        if is_early_failure and len(early_fail_times) >= early_fail_abort_count:
                            from .recorder import failover_group_member, _launch_segment
                            if failover_group_member(self.app, self.recording_id, 'dead-stream fast-fail'):
                                early_fail_times.clear()
                                _launch_segment(self.app, self.recording_id, seg_num + 1)
                                break
                            # exit_code/stderr_tail were collected above (this block's own
                            # collect_segment_diagnostics call) for the STALL_DETECTED/
                            # DIAGNOSTICS pair on this segment. _give_up() below collects
                            # again for self._give_up_diagnostics, but that spool was already
                            # consumed here - collect_segment_diagnostics is idempotent and
                            # returns an empty tail on a second call - so the real cause has
                            # to be passed through explicitly rather than left for _give_up
                            # to (fail to) find on its own.
                            cause = _first_line(stderr_tail)
                            max_retry_attempts = cfg['watchdog']['dead_stream_max_retry_attempts']
                            if self._schedule_dead_stream_retry(
                                    rec, len(early_fail_times), early_fail_abort_window,
                                    max_retry_attempts, cause):
                                return
                            self._give_up(lambda: self._fail_recording_dead_stream(
                                rec, len(early_fail_times), early_fail_abort_window,
                                cause=cause))
                            return

                        # Check failure limit before attempting restart
                        if rec.consecutive_failures >= max_failures:
                            from .recorder import failover_group_member, _launch_segment
                            if failover_group_member(self.app, self.recording_id, 'max consecutive failures'):
                                early_fail_times.clear()
                                _launch_segment(self.app, self.recording_id, seg_num + 1)
                                break
                            # Same reasoning as the dead-stream give-up above: pass the
                            # already-collected cause through rather than rely on _give_up's
                            # redundant (and by now empty) re-collection.
                            self._give_up(lambda: self._fail_recording(
                                rec, max_failures, cause=_first_line(stderr_tail)))
                            return

                        # ── Stall-rate demotion (dev/changelog/889) ─────────────
                        # The three trip-wires above are all "the feed is dead" shaped, and
                        # a member that stalls constantly but always comes back reaches
                        # none of them: the successful restart below zeroes the very
                        # counter that would trip max_consecutive_failures. Checked here,
                        # after both of them, because they burn the member for the run and
                        # a dead feed deserves that; this one only demotes.
                        #
                        # A stall closes this segment and opens the next one either way, so
                        # the move rides a boundary that was happening anyway - and skips
                        # the restart delay below, making it cheaper than staying put.
                        if rec.channel_id != member_stall_channel_id:
                            member_stall_times = []
                            member_stall_channel_id = rec.channel_id
                        now_mono = time.monotonic()
                        member_stall_times.append(now_mono)
                        member_stall_times = stalls_within_window(
                            member_stall_times, now_mono,
                            thresholds.stall_move_window_minutes * 60)
                        if (rec.group_id is not None
                                and thresholds.stall_move_count > 0
                                and len(member_stall_times) >= thresholds.stall_move_count):
                            from .recorder import failover_group_member, _launch_segment
                            move_reason = (f'{len(member_stall_times)} stalls in '
                                           f'{thresholds.stall_move_window_minutes} minutes')
                            if failover_group_member(self.app, self.recording_id,
                                                     move_reason, demote=True):
                                self.state.stall_moves += 1
                                member_stall_times = []
                                member_stall_channel_id = None
                                early_fail_times.clear()
                                _launch_segment(self.app, self.recording_id, seg_num + 1)
                                break
                            # Nowhere better to go - a one-member group, or every other
                            # member held back by its account's connection limit.
                            # Deliberately NOT a give-up: nothing died, so the recording
                            # stays where it is and takes its normal restart below, exactly
                            # as it would have without this trip-wire.
                            #
                            # The stall clock is reset on a refusal too, so the member has
                            # to re-earn the whole window before asking again. Leaving it
                            # armed would re-ask on every single stall from here on, and
                            # the connection-limit refusal writes an event each time it is
                            # asked - a recording riding a stalling feed would fill its own
                            # timeline with them.
                            member_stall_times = []

                        # Second half of the downtime gap (see the stall commit above):
                        # everything from here until the replacement segment writes its
                        # first byte is time no data is being captured. Measured rather
                        # than assumed - restart_delay is only part of it, and the
                        # reconnect can take up to stall_timeout more.
                        gap_start = time.monotonic()

                        # Wait restart delay (interruptible by stop_event)
                        @retry_on_locked()
                        def _record_restart_attempted_and_commit():
                            add_recording_event(self.recording_id, RESTART_ATTEMPTED,
                                detail=f'Waiting {restart_delay}s before restart',
                                segment_number=seg_num)
                            db.session.commit()

                        _record_restart_attempted_and_commit()
                        ev.publish(self.recording_id, RESTART_ATTEMPTED, {
                            'delay': restart_delay,
                            'consecutive_failures': rec.consecutive_failures,
                        })

                        self.state.stop_event.wait(timeout=restart_delay)
                        if self.state.stop_event.is_set():
                            return

                        # Launch next segment
                        next_seg_num = seg_num + 1
                        from .recorder import _launch_segment
                        _launch_segment(self.app, self.recording_id, next_seg_num)

                        # Wait for new segment to start growing. path_fn queries the
                        # row each poll - it may not exist yet right after launch.
                        def _next_seg_path():
                            s = RecordingSegment.query.filter_by(
                                recording_id=self.recording_id, segment_number=next_seg_num
                            ).first()
                            return s.file_path if s else None

                        # proc= is read after _launch_segment, so it is the NEW process (or
                        # the old dead one if the spawn itself failed, which is equally a
                        # restart that will never produce data). Without it a relaunch that
                        # dies on connect costs the full stall_timeout to notice, exactly the
                        # latency the proc= check exists to remove.
                        restart_ok = wait_for_file_data(
                            _next_seg_path, stall_timeout,
                            stop_check=self.state.stop_event.is_set,
                            proc=self.state.process,
                            poll_interval=2,
                        )

                        # Read once, outside both closures below: a lock-retry re-runs the
                        # closure and would otherwise charge the recording a longer gap
                        # each attempt.
                        gap_seconds = time.monotonic() - gap_start

                        if restart_ok:
                            @retry_on_locked()
                            def _record_restart_succeeded_and_commit():
                                r = db.session.get(Recording, self.recording_id)
                                r.consecutive_failures = 0
                                r.total_restart_count += 1
                                r.total_downtime_seconds += gap_seconds
                                add_recording_event(self.recording_id, RESTART_SUCCEEDED,
                                    detail=f'Segment {next_seg_num} is growing',
                                    segment_number=next_seg_num)
                                db.session.commit()
                                return r

                            rec2 = _record_restart_succeeded_and_commit()
                            ev.publish(self.recording_id, RESTART_SUCCEEDED, {
                                'segment_number': next_seg_num,
                                'consecutive_failures': 0,
                            })
                        else:
                            @retry_on_locked()
                            def _record_restart_failed_and_commit():
                                r = db.session.get(Recording, self.recording_id)
                                r.consecutive_failures += 1
                                # A restart that produced nothing is entirely lost time,
                                # so it counts the same as a successful one - dropping it
                                # would make the worst captures look like the best ones.
                                r.total_downtime_seconds += gap_seconds
                                if r.consecutive_failures > r.consecutive_failures_peak:
                                    r.consecutive_failures_peak = r.consecutive_failures
                                add_recording_event(self.recording_id, RESTART_FAILED,
                                    detail=f'Segment {next_seg_num} produced no data within {stall_timeout}s',
                                    segment_number=next_seg_num)
                                db.session.commit()
                                return r

                            rec2 = _record_restart_failed_and_commit()
                            ev.publish(self.recording_id, RESTART_FAILED, {
                                'segment_number': next_seg_num,
                                'consecutive_failures': rec2.consecutive_failures,
                            })
                            # A restart that produced no data = this feed is dead;
                            # group-backed recordings switch members right away.
                            from .recorder import failover_group_member
                            if failover_group_member(self.app, self.recording_id, 'restart produced no data'):
                                # The failed restart's ffmpeg is still running (it
                                # just never produced data) - kill it before the
                                # new member's segment launches, or it leaks and
                                # keeps holding the old account's connection.
                                terminate_or_kill(self.state.process)
                                # Close out the abandoned segment's row the same way the
                                # stall-detected path above does (ended_at/exit_reason/
                                # bytes_recorded/stderr diagnostics), or it stays open
                                # forever and recording_detail.html's `(seg.ended_at or now)`
                                # fallback renders an ever-growing nonsense duration once the
                                # recording is long since terminal - CLAUDE.md "teardown
                                # releases everything the create path acquired"
                                # (dev/changelog/463).
                                from .recorder import _close_active_segment
                                _close_active_segment(self.app, self.recording_id, exit_reason='ERROR')
                                early_fail_times.clear()
                                _launch_segment(self.app, self.recording_id, next_seg_num + 1)
                                break
                            if rec2.consecutive_failures >= max_failures:
                                self._give_up(lambda: self._fail_recording(rec2, max_failures))
                                return
                            # Hand the verdict to the next outer-loop pass rather than
                            # letting it start a fresh GrowthMonitor and spend another
                            # stall_timeout arriving at the same answer. Only this
                            # fall-through path needs it: the two branches above have
                            # already closed the segment out or ended the recording.
                            restart_no_data_segment = next_seg_num

                        # Break inner loop → outer loop picks up new segment
                        break

                    self.state.stop_event.wait(timeout=poll)
                # ── End inner loop ───────────────────────────────────────────

            log.info('Watchdog exiting for recording %d (stop_event set)', self.recording_id)

    def _check_format_pin(self, seg_id: int, seg_num: int):
        """Say so when this segment was captured at a different format than the one the
        recording opened with, so the finished file changes format part-way through
        (DECIDED 12, DESIGN-channel-groups-model.md 5.1).

        Fired here, off the segment's own capture-time probe, rather than at the failover
        that usually causes it. Two reasons, and the second is why it has to be here:
        a probe is what actually happened where a selection is only what was intended,
        and the SAME member's feed can change format under us with no failover at all -
        a provider swapping an upstream mid-event - which no selection-time check can
        see. The pin filter in recorder.py makes this rare; it does not make it
        impossible, and principle 1 says the rare case is exactly the one that must not
        be silent.

        Best-effort diagnostics: a failure here must never touch the capture it is
        describing, so it logs and returns."""
        from . import db
        from .database import RecordingSegment, RECORDING_FORMAT_CHANGED
        from .channel_groups import format_label, segment_format_key
        from .recorder import recording_format_pin

        try:
            seg = db.session.get(RecordingSegment, seg_id)
            got = segment_format_key(seg)
            pin = recording_format_pin(self.recording_id)
            # An unprobed segment leaves the pin None, and the segment that SET the pin
            # matches it - both are the no-op case, not a change.
            if got is None or pin is None or got == pin:
                return

            # Every divergent segment gets its own event - each is a distinct fact about a
            # distinct part of the file, and the recording's own detail page is where a
            # mixed-format file is disclosed (dev/changelog/928).
            detail = (f'Segment {seg_num} was captured at {format_label(got)}, but this '
                      f'recording opened at {format_label(pin)}. The finished file '
                      f'changes format part-way through - it plays, but its header '
                      f'describes only {format_label(pin)}.')

            @retry_on_locked()
            def _log_format_change_and_commit():
                add_recording_event(self.recording_id, RECORDING_FORMAT_CHANGED,
                                    detail=detail,
                                    extra={'segment_number': seg_num,
                                           'opened_format': list(pin),
                                           'segment_format': list(got)})
                db.session.commit()

            _log_format_change_and_commit()
            log.warning('Recording %d: %s', self.recording_id, detail)
        except Exception:
            log.exception('Recording %d: format-pin check failed on segment %d',
                          self.recording_id, seg_num)

    def _publish_snapshot(self, cfg):
        """Publish a STATS_SNAPSHOT SSE event with current recording metrics."""
        from . import db
        from .database import Recording, RecordingSegment
        from . import events as ev
        try:
            rec = db.session.get(Recording, self.recording_id)
            if rec is None:
                return
            now = datetime.utcnow()
            elapsed = (now - rec.started_at).total_seconds() if rec.started_at else 0
            remaining = max(0, (rec.stop_time - now).total_seconds())

            # Current segment size
            seg = RecordingSegment.query.filter_by(
                recording_id=self.recording_id,
                segment_number=self.state.current_segment_num,
            ).first()
            current_bytes = 0
            if seg and os.path.exists(seg.file_path):
                current_bytes = os.path.getsize(seg.file_path)

            # Rough total bytes: sum all completed segments + current
            total_bytes = sum(
                (s.bytes_recorded or 0)
                for s in rec.segments
                if s.ended_at is not None
            ) + current_bytes

            ev.publish(self.recording_id, 'STATS_SNAPSHOT', {
                'status': rec.status,
                'elapsed_seconds': elapsed,
                'remaining_seconds': remaining,
                'current_segment_number': self.state.current_segment_num,
                'current_segment_bytes': current_bytes,
                'total_bytes': total_bytes,
                'stall_count': rec.total_stall_count,
                'restart_count': rec.total_restart_count,
                'consecutive_failures': rec.consecutive_failures,
                'downtime_seconds': rec.total_downtime_seconds,
            })
        except Exception as exc:
            log.debug('Snapshot publish failed: %s', exc)

    @retry_on_locked()
    def _mark_recording_failed(self, rec, event_type, failure_reason, log_msg, detail, sse_extra,
                               cause=None):
        """Shared tail for _fail_recording / _fail_recording_dead_stream: kill the
        process, set the terminal status, log the event, and drop from _active.

        cause: the first line of whatever ffmpeg said, if the caller already had it in
        scope (see the two call sites that pass one). Falls back to self._give_up_diagnostics
        (set by _give_up) when the caller has none - true for the one give-up path whose
        process's stderr spool _give_up is the FIRST thing to read, not a second, already-
        empty read of one this same stall iteration already consumed.
        """
        from . import db
        from . import events as ev
        from .recorder import _active, _lock

        exit_code, stderr_tail = getattr(self, '_give_up_diagnostics', (None, ''))
        if cause is None:
            cause = _first_line(stderr_tail)
        # Fold the reason ffmpeg actually gave into the terminal event's detail and the
        # alert body (built from log_msg by the _AlertHandler in app/__init__.py) - not
        # just the DIAGNOSTICS row one scroll away. Already credential-masked upstream by
        # collect_segment_diagnostics. Product Principle 1: this is the moment a recording
        # is abandoned, so it's where the reason is worth the most.
        if cause:
            log_msg = f'{log_msg} - {cause}'
            detail = f'{detail} - {cause}'

        log.error(log_msg, extra={'recording_id': self.recording_id})

        # The last restart attempt's ffmpeg process is still running (it just hasn't
        # produced data yet) - kill it now. Otherwise it's abandoned here (removed
        # from _active below) but keeps running untracked, potentially still holding
        # the provider's one allowed connection for this account and blocking any
        # other recording on the same account from connecting. terminate_or_kill's
        # poll() guard makes a retry of the whole function (see retry_on_locked)
        # safe: it's a no-op once the process is confirmed dead.
        terminate_or_kill(self.state.process)

        rec.status = REC_STATUS_FAILED
        rec.completed_at = datetime.utcnow()
        rec.failure_reason = failure_reason
        add_recording_event(self.recording_id, event_type, detail=detail)
        # Whatever ffmpeg said on its way out, attached to the segment that was live when we
        # gave up. Collected in _give_up (see there for why it cannot happen inside this
        # retried closure); the default covers the paths that reach here without it.
        # ended_at/exit_reason are deliberately NOT set here: that row is left open by a
        # separate known defect tracked in the backlog, and closing it is out of scope here.
        from .database import RecordingSegment
        from .recorder import record_segment_diagnostics
        open_seg = RecordingSegment.query.filter_by(
            recording_id=self.recording_id, ended_at=None
        ).order_by(RecordingSegment.segment_number.desc()).first()
        record_segment_diagnostics(self.recording_id, open_seg, exit_code, stderr_tail)
        db.session.commit()
        ev.publish(self.recording_id, event_type, sse_extra)
        with _lock:
            _active.pop(self.recording_id, None)

    def _fail_recording(self, rec, max_failures, cause=None):
        from .database import RECORDING_FAILED
        self._mark_recording_failed(
            rec,
            event_type=RECORDING_FAILED,
            failure_reason='MAX_CONSECUTIVE_FAILURES',
            log_msg=('Recording "%s" (#%d): max consecutive failures (%d) reached, aborting'
                      % (rec.name, self.recording_id, max_failures)),
            detail=f'Aborting: {max_failures} consecutive failures',
            sse_extra={
                'consecutive_failures': rec.consecutive_failures,
                'max_failures': max_failures,
            },
            cause=cause,
        )
        # Outside the retry_on_locked closure above: this is runtime connection
        # accounting, not a DB write. Without it the account's slot stays "held"
        # by this dead recording until the app restarts (dev/docs/BUGS.md). A manual
        # URL-only recording has no channel, so there's no account slot to release -
        # same guard _schedule_dead_stream_retry already uses.
        from . import connection_limits as connlim
        if rec.channel_id and rec.channel:
            connlim.release(rec.channel.account_id, 'recording', self.recording_id)

    def _fail_recording_dead_stream(self, rec, streak, window_seconds, cause=None):
        from .database import RECORDING_FAILED_DEAD_STREAM
        # dead_stream_retry_count > 0 means this recording already retried and died again -
        # say so, so the terminal event reads honestly as "gave up after trying" rather than
        # indistinguishable from the very first trip (Product Principle 1).
        retried_note = (f' after {rec.dead_stream_retry_count} retry attempt(s)'
                        if rec.dead_stream_retry_count else '')
        self._mark_recording_failed(
            rec,
            event_type=RECORDING_FAILED_DEAD_STREAM,
            failure_reason='DEAD_STREAM_DETECTED',
            log_msg=(
                'Recording "%s" (#%d): %d consecutive early-failure segments within %ds - '
                'dead stream, aborting%s'
                % (rec.name, self.recording_id, streak, window_seconds, retried_note)),
            detail=(f'Aborting: {streak} consecutive early failures within {window_seconds}s '
                    f'- stream appears to reconnect but immediately dies{retried_note}'),
            sse_extra={
                'early_fail_streak': streak,
                'window_seconds': window_seconds,
                'retry_attempts': rec.dead_stream_retry_count,
            },
            cause=cause,
        )
        from . import connection_limits as connlim
        if rec.channel_id and rec.channel:
            connlim.release(rec.channel.account_id, 'recording', self.recording_id)

    def _schedule_dead_stream_retry(self, rec, streak, window_seconds, max_attempts, cause) -> bool:
        """Instead of giving up on a dead-stream trip immediately, back off and try again
        later (Product Principle 2) - unless the retry budget or the recording's own window
        is already exhausted, in which case the caller falls through to the normal give-up.

        Deliberately does NOT go through _give_up(): that call is for a TERMINAL abandonment
        (it dings the channel's health score and persists a "final" thumbnail), and entering
        RETRYING is provisional, not terminal - the recording may yet finish cleanly.
        """
        from . import db
        from .database import Recording, RECORDING_RETRY_SCHEDULED
        from . import events as ev
        from . import connection_limits as connlim
        from .recorder import _active, _lock
        from .scheduler import schedule_dead_stream_retry

        now = datetime.utcnow()
        if max_attempts <= 0 or rec.dead_stream_retry_count >= max_attempts or rec.stop_time <= now:
            return False

        delay_minutes = _dead_stream_retry_delay_minutes(rec.dead_stream_retry_count + 1)
        next_at = now + timedelta(minutes=delay_minutes)

        @retry_on_locked()
        def _mark_retrying_and_commit():
            r = db.session.get(Recording, self.recording_id)
            r.status = REC_STATUS_RETRYING
            r.dead_stream_retry_count += 1
            r.next_retry_at = next_at
            detail = (f'{streak} consecutive early failures within {window_seconds}s - stream '
                      f'appears dead. Retrying (attempt {r.dead_stream_retry_count}/{max_attempts}) '
                      f'in {delay_minutes} min' + (f' - {cause}' if cause else ''))
            add_recording_event(self.recording_id, RECORDING_RETRY_SCHEDULED, detail=detail,
                                extra={'attempt': r.dead_stream_retry_count,
                                       'max_attempts': max_attempts,
                                       'delay_minutes': delay_minutes})
            db.session.commit()
            return r

        r = _mark_retrying_and_commit()
        log.warning('Recording "%s" (#%d): dead-stream trip, retrying (attempt %d/%d) in %d min',
                    r.name, self.recording_id, r.dead_stream_retry_count, max_attempts, delay_minutes)
        if r.channel_id and r.channel:
            connlim.release(r.channel.account_id, 'recording', self.recording_id)
        schedule_dead_stream_retry(self.recording_id, next_at)
        with _lock:
            _active.pop(self.recording_id, None)
        ev.publish(self.recording_id, RECORDING_RETRY_SCHEDULED, {
            # Every consumer of this event reads `status` to relabel the row (dashboard.js's
            # handleTerminal); without it the badge rendered the event NAME and reached for a
            # `badge-recording_retry_scheduled` rule that does not exist (dev/changelog/663).
            'status': REC_STATUS_RETRYING,
            'attempt': r.dead_stream_retry_count,
            'max_attempts': max_attempts,
            'next_retry_at': next_at.strftime('%Y-%m-%dT%H:%M:%S'),
        })
        return True

    def _give_up(self, fail_fn):
        """Run a watchdog give-up: the terminal fail call, then the health-score
        observation + final-thumbnail persist that every give-up point needs."""
        # Kill and read the diagnostics BEFORE the terminal fail call. Collecting unlinks
        # the stderr spool - a non-idempotent side effect, which CLAUDE.md forbids inside a
        # retry_on_locked closure, and _mark_recording_failed is decorated as a whole.
        # terminate_or_kill is a no-op on an already-dead process (the stall path killed it
        # already), so doing it here costs nothing and makes poll() meaningful.
        # This is the moment a recording is abandoned, so it is exactly where the reason
        # ffmpeg gave is worth the most (Product Principle 1).
        from .recorder import collect_segment_diagnostics
        terminate_or_kill(self.state.process)
        self._give_up_diagnostics = collect_segment_diagnostics(self.recording_id)
        fail_fn()
        from .health_score import apply_recording_health_observation
        apply_recording_health_observation(self.app, self.recording_id, 'failed')
        from .recorder import persist_final_thumbnail
        persist_final_thumbnail(self.recording_id)


def finalize_dead_stream_retry_exhausted(app, recording_id: int, cause: str = None):
    """Give up on a RETRYING recording whose scheduled window ended before its next retry
    attempt could fire - see recorder.py::fire_dead_stream_retry, the retry_<id> job's target.

    No-ops if the recording moved on in the meantime (aborted/deleted/somehow resumed) -
    status is checked fresh here, not assumed from the caller. Must be called from within an
    app context (both call sites already have one - this is not a bare-thread entry point like
    WatchdogThread.run()). Mirrors what WatchdogThread._give_up() does for every other give-up
    path (health-score observation, final-thumbnail persist) even though there is no
    WatchdogThread instance here - the recording already released its live state when it
    entered RETRYING, so there is no process left to kill.
    """
    from . import db
    from .database import Recording, RECORDING_FAILED_DEAD_STREAM

    @retry_on_locked()
    def _mark_and_commit():
        r = db.session.get(Recording, recording_id)
        if r is None or r.status != REC_STATUS_RETRYING:
            return None
        r.status = REC_STATUS_FAILED
        r.completed_at = datetime.utcnow()
        r.failure_reason = 'DEAD_STREAM_DETECTED'
        r.next_retry_at = None
        detail = (f'Aborting: exhausted {r.dead_stream_retry_count} retry attempt(s) - '
                  f'scheduled window ended before the stream came back'
                  + (f' - {cause}' if cause else ''))
        add_recording_event(recording_id, RECORDING_FAILED_DEAD_STREAM, detail=detail,
                            extra={'retry_attempts': r.dead_stream_retry_count})
        db.session.commit()
        return r

    rec = _mark_and_commit()
    if rec is None:
        return
    log.error('Recording "%s" (#%d): window ended during dead-stream retry wait '
              '(attempt %d) - aborting', rec.name, recording_id, rec.dead_stream_retry_count)
    from . import events as ev
    ev.publish(recording_id, RECORDING_FAILED_DEAD_STREAM, {
        'retry_attempts': rec.dead_stream_retry_count,
    })
    from .health_score import apply_recording_health_observation
    apply_recording_health_observation(app, recording_id, 'failed')
    from .recorder import persist_final_thumbnail
    persist_final_thumbnail(recording_id)
