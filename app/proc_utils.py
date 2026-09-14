"""Process/ffmpeg helpers - canonical home per CLAUDE.md's coding-standards table."""
import glob
import logging
import os
import signal
import subprocess
import time
import uuid
from urllib.parse import urlsplit

from .config import resolve_ffmpeg_path

log = logging.getLogger(__name__)


def is_hls_url(url: str) -> bool:
    """True if url is an HLS playlist (path ends in .m3u8, ignoring any query string).

    HLS inputs must NOT get the raw-stream reconnect flags below: an HLS media-playlist
    GET returns EOF on every fetch (that's how the format works), so -reconnect_at_eof
    makes ffmpeg spin re-fetching the playlist forever instead of downloading segments -
    it falls behind the live edge and captures zero bytes (dev/docs/BUGS.md 2026-07-18). HLS
    resilience is instead handled by the demuxer's own segment retry plus the watchdog's
    stall-restart, so dropping the flags loses nothing."""
    return urlsplit(url).path.lower().endswith('.m3u8')


def build_capture_cmd(cfg: dict, url: str, output_path: str, duration_seconds: int = 0,
                      *, pace_realtime: bool = True) -> list:
    """ffmpeg "connect + stream-copy to file" command, shared by the recorder,
    channel tester, and manual URL test. Every http(s) input is identified with
    http.user_agent; continuous ones also get reconnect flags so all three behave the
    same on briefly-dropping streams, while HLS (.m3u8) inputs are excluded from those
    (see is_hls_url). duration_seconds > 0 adds -t (segment duration for recordings,
    capture length for tests).

    pace_realtime controls -re and defaults to True because that is the safe value -
    it is what every caller received before dev/changelog/437, so a caller that has
    not reasoned about pacing keeps today's behavior. Read the comment at the flag
    before changing what a call site passes."""
    cmd = [resolve_ffmpeg_path(cfg['ffmpeg']['path'])]
    # Identify as http.user_agent, the same string account sync sends (accounts.py
    # _request_headers) - one setting, so a provider that filters on user agent sees this
    # app the same way whether it is fetching a playlist or capturing a stream. Without it
    # ffmpeg announces its own default, measured on this box as 'Lavf/60.16.100' (ffmpeg
    # 6.1.1), which is among the most commonly blocked strings there is - and a provider
    # that throttles or refuses it produces a channel that "just doesn't work" with nothing
    # naming the reason. Verified on a loopback listener that one flag covers HLS too: the
    # value is sent on the segment GETs, not only the playlist GET (dev/changelog/523).
    #
    # Emitted BEFORE extra_input_args, deliberately: ffmpeg takes the last occurrence of a
    # repeated option, so a user who sets their own -user_agent there still overrides this.
    if url.startswith(('http://', 'https://')):
        cmd += ['-user_agent', cfg.get('http', {}).get('user_agent',
                                                       'VLC/3.0.18 LibVLC/3.0.18')]
    cmd += cfg['ffmpeg'].get('extra_input_args', [])
    if url.startswith(('http://', 'https://')) and not is_hls_url(url):
        cmd += ['-reconnect', '1', '-reconnect_streamed', '1',
                '-reconnect_at_eof', '1', '-reconnect_delay_max', '2']
    # -re throttles input reading to the stream's own native rate, and it is MANDATORY
    # wherever duration_seconds bounds the capture: -t is a content-time limit, so
    # without pacing a provider that pre-buffers satisfies "-t 30" out of its backlog
    # almost instantly. Measured on this box (dev/changelog/437): a 30s bounded capture
    # off a bursting source took 29.8s wall with -re and 0.35s without - 205x, which is
    # exactly the channel-tester defect of dev/docs/BUGS.md 2026-06-28 04:40 pm.
    #
    # On an UNBOUNDED capture it is inert, which is why the recorder no longer asks for
    # it. The long-standing suspicion was that -re forbids catching up after a reconnect,
    # pinning the connection behind the live edge until the provider drops it. That was
    # measured and is false on this build: ffmpeg throttles only while AHEAD of schedule
    # and bursts to catch up when behind. Ten minutes against a feed hiccupping 6s out of
    # every 30 produced content durations identical to the microsecond with and without
    # the flag, and four real 85-minute captures sat 1.0-1.7s behind wall clock with no
    # growth over the segment. So it is dropped as an unexplained day-one hardcode that
    # ffmpeg's own docs advise against on live input - NOT because it was doing damage.
    # It is specifically not the cause of the stall-and-restart mode in changelog 429/436.
    if pace_realtime:
        cmd += ['-re']
    cmd += ['-i', url]
    if duration_seconds and duration_seconds > 0:
        cmd += ['-t', str(duration_seconds)]
    cmd += cfg['ffmpeg'].get('extra_output_args', [])
    cmd += ['-c', 'copy', '-y', output_path]
    return cmd


# Twin caps on any stderr tail read back off disk. Bytes bound the read, lines bound what a
# human is shown; 4KB/20 lines comfortably holds an ffmpeg error plus the context above it.
#
# Measured on this machine (dev/changelog/430), because ffmpeg's stderr shape is build- and
# invocation-specific and the documented behavior is not what happens here:
#   * Redirected to a FILE (which is how the recorder spawns it), ffmpeg 6.x on this box
#     emits progress with newlines and essentially no '\r' - an 8s `-re -c copy` run produced
#     CR=0, LF=17. So the byte cap is the load-bearing one; a long capture's spool is large
#     because there are many lines, not one enormous one.
#   * '\r' is still normalized rather than assumed absent: a libx264 encode on the same box
#     did emit one, the flag set differs per call site, and the cost of handling it is a
#     str.replace. It is defensive, not the primary reason for the byte cap.
STDERR_TAIL_MAX_BYTES = 4096
STDERR_TAIL_MAX_LINES = 20


def read_stderr_tail(path, max_bytes: int = STDERR_TAIL_MAX_BYTES,
                     max_lines: int = STDERR_TAIL_MAX_LINES) -> str:
    """Last few meaningful lines of a child process's stderr file; '' if unreadable.

    Only ever called once the child has exited, so there is no partial-write race. Seeks
    rather than reading the whole file: a multi-hour capture's stderr is unbounded on disk
    even though what we keep is not. '\\r' is normalized to '\\n' so a progress-stat run
    splits into lines instead of forming one; blank lines are dropped.

    Returns '' - not a placeholder string - when there is nothing to say, so callers can
    distinguish "ffmpeg said nothing" from "ffmpeg said something" and stay quiet accordingly.
    """
    try:
        with open(path, 'rb') as fh:
            try:
                fh.seek(-max_bytes, os.SEEK_END)
            except OSError:
                fh.seek(0)  # file shorter than max_bytes - seek would land before byte 0
            raw = fh.read()
    except OSError:
        return ''
    lines = [ln.strip() for ln in raw.decode('utf-8', errors='replace').replace('\r', '\n').split('\n')]
    lines = [ln for ln in lines if ln]
    if max_lines and len(lines) > max_lines:
        lines = lines[-max_lines:]
    return '\n'.join(lines)


def suspend_process(proc) -> bool:
    """SIGSTOP a child so it releases the CPU without losing what it has already done.

    The whole point is that this is not a kill: an encode holds its state and picks up
    exactly where it left off on resume_process(), with no splice point and no quality
    question (dev/changelog/952). Measured on this box with the real conversion flags: a
    1080p30 libx264 run stopped mid-encode held 340,680 kB RSS unchanged across the pause
    (no swap here, so nothing is reclaimed), its output file froze to the byte, and the
    resumed run exited 0 with a complete, correct file.

    False if there was no live process to stop, so a caller can tell "suspended" from
    "already gone" rather than assuming.
    """
    if proc is None or proc.poll() is not None:
        return False
    try:
        proc.send_signal(signal.SIGSTOP)
    except OSError:
        return False  # raced with its own exit; nothing to stop
    return True


def resume_process(proc) -> bool:
    """SIGCONT a child suspended by suspend_process(). Harmless on one that is already
    running - SIGCONT's default action on a non-stopped process is to do nothing - which is
    what lets terminate_or_kill() send it unconditionally."""
    if proc is None or proc.poll() is not None:
        return False
    try:
        proc.send_signal(signal.SIGCONT)
    except OSError:
        return False
    return True


def terminate_or_kill(proc, timeout: float = 5.0, hard: bool = False):
    """Stop a child process: SIGTERM, escalating to SIGKILL if it hasn't exited
    within timeout. hard=True skips straight to SIGKILL. No-op if proc is None or
    already exited; always reaps after SIGKILL so no zombie is left."""
    if proc is None or proc.poll() is not None:
        return
    # Continue first, always. A SIGSTOPped child does not act on SIGTERM until something
    # continues it - measured on this box, a stopped ffmpeg 7.1 sat in state T five full
    # seconds after SIGTERM and only exited once SIGCONT arrived - so without this the wait
    # below is burned in its entirety before every escalation to SIGKILL. Doing it here
    # rather than at each teardown site is deliberate: shutdown, kill_active_conversions(),
    # the cancel route, FAILED and ABORTED all reach a possibly-suspended conversion, and a
    # rule spread over five call sites is a rule one of them will eventually forget
    # (dev/changelog/952).
    resume_process(proc)
    if not hard:
        proc.terminate()
        try:
            proc.wait(timeout=timeout)
            return
        except subprocess.TimeoutExpired:
            pass
    proc.kill()
    try:
        proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        pass  # uninterruptible-sleep edge case; nothing more can be done


def wait_for_file_data(path_fn, timeout, stop_check=None, proc=None, poll_interval=1.0) -> bool:
    """Poll until the file named by path_fn() (may return None while unknown) has >0
    bytes. False on timeout, stop_check() truthy, or proc exiting before data.
    Size is read before the proc-exit check so bytes written just before death count."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if stop_check is not None and stop_check():
            return False
        path = path_fn()
        size = 0
        if path:
            try:
                size = os.path.getsize(path)
            except OSError:
                size = 0
        if size > 0:
            return True
        if proc is not None and proc.poll() is not None:
            return False
        time.sleep(poll_interval)
    return False


class GrowthMonitor:
    """Feed successive file sizes via update(); reports how long the file has gone
    without growing. Only the tracking mechanics are shared between the watchdog and
    the channel tester - stall *reactions* (restart loop vs. count-and-continue) stay
    with the callers by design."""

    def __init__(self):
        self.last_size = -1
        self._stall_start = None

    def update(self, size) -> float:
        """Seconds since the file last grew (0.0 while growing)."""
        if size > self.last_size:
            self.last_size = size
            self._stall_start = None
            return 0.0
        if self._stall_start is None:
            self._stall_start = time.monotonic()
        return time.monotonic() - self._stall_start

    def reset(self):
        self._stall_start = None


# ── Supervised ffmpeg runs ────────────────────────────────────────────────────────────
# A JOB THAT IS STILL ADVANCING IS NEVER KILLED. Two rules bound an ffmpeg whose total
# work is not knowable in advance, and neither of them is a whole-job deadline:
# `pre_output_timeout` bounds only the phase before the job produces its first output, and
# once it is producing, a no-growth stall budget is the sole authority.
#
# Both halves are needed and they are not interchangeable. Stall detection is gated on the
# job having produced *something*, because a badly-damaged source makes ffmpeg seek and
# analyze for minutes before muxing anything (dev/docs/BUGS.md 2026-07-24), so it cannot
# see a job that never starts - that is what the pre-output budget is for.
#
# The rule was settled for conversions in dev/changelog/865, after a fixed deadline killed
# a healthy 3.6h re-encode at elapsed 14,404s against a 14,400s number and the retry re-ran
# it from 0% twice more. It reached the concat in dev/changelog/947, after the same shape
# killed a 42.6 GB join at roughly the halfway mark while it was writing 71 MB/s - near
# line rate for that mount. Both callers share this one implementation rather than each
# owning a poll-and-kill loop (CLAUDE.md search-before-you-write).
#
# Neither rule is a reason to stop a job that is merely in the way: a supervised run that
# has to yield local resources is SIGSTOPped and continued rather than killed and re-run
# (dev/changelog/952), and both budgets stop with it.
#
# stderr goes to a real FILE, never a pipe: nothing drains a pipe inside a poll loop, and
# an undrained 64KB pipe buffer once deadlocked every recording in this app at ~7.8 minutes
# (CLAUDE.md subprocess-discipline, dev/changelog/430).

# What a supervised run watches to decide it is still advancing.
#   'out_time' - the encoded output TIMESTAMP, from ffmpeg's own -progress file. What a
#                conversion watches: an encode's output file can sit still while the muxer
#                buffers, so bytes are the noisier signal there.
#   'size'     - bytes written to the output file. What a stream copy (the concat) watches:
#                writing bytes is the entire job, a byte count cannot go backwards the way
#                a concat-demuxer timestamp can under +genpts, and throughput is the figure
#                that proved the killed join was healthy.
PROGRESS_SIGNALS = ('out_time', 'size')

_SIGNAL_NOUN = {'out_time': 'output', 'size': 'the output file'}


class SupervisedRun:
    """Outcome of one supervised ffmpeg run.

    `reason` is 'success', 'died', 'stalled', 'no_output' or 'timeout'. 'no_output' is the
    pre-output budget expiring; 'timeout' is reachable only when stall detection has been
    switched off, which is the one case where nothing else is watching liveness at all.

    There is no reason for "yielded to something else": a supervised run that steps aside is
    SUSPENDED and continued (see suspend_check), so it ends exactly once, on its own terms.

    `out_time` is how far into the job ffmpeg had got when it ended - callers that restart
    compare it across attempts, because a defect in the source stops every attempt at the
    same offset and starting over cannot get past it.
    """

    def __init__(self, reason, *, returncode=None, error_msg=None, out_time=0.0, size=0,
                 stalled_for=None):
        self.reason = reason
        self.returncode = returncode
        self.error_msg = error_msg
        self.out_time = out_time
        self.size = size
        self.stalled_for = stalled_for

    @property
    def success(self) -> bool:
        return self.reason == 'success'


def read_progress_tail(progress_path):
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


def supervise_ffmpeg(cmd, output_path, *, scratch_prefix, scratch_key, interval,
                     pre_output_timeout, stall_seconds, progress_signal='out_time',
                     noun='job', label='ffmpeg', on_spawn=None, on_progress=None,
                     suspend_check=None, on_suspend=None, on_resume=None,
                     on_exit=None) -> SupervisedRun:
    """Spawn one ffmpeg and supervise it under the two rules above. Returns a SupervisedRun.

    `noun` ('conversion', 'concat') spells the user-facing error text; `label` prefixes the
    log lines. The hooks keep the caller's own concerns out of here: `on_spawn(proc)`
    registers the child in whatever live registry the caller owns, `on_progress(wall,
    out_time, size)` publishes a tick, and `on_exit(stderr_path, returncode)` fires after
    the loop while the stderr spool still exists, for anything the caller wants to derive
    from it.

    SUSPENSION. `suspend_check(wall, out_time, size)` returns None to keep running, or a
    short reason string to have the child SIGSTOPped until a later call returns None again -
    that is how a conversion steps aside for a recording without losing the hours it has
    already encoded (dev/changelog/952). `on_suspend(reason)` and `on_resume()` fire on the
    transitions, once each. **Every clock this function keeps stops while the child is
    stopped**: the stall budget, the pre-output budget and the elapsed `wall` handed to the
    hooks all exclude suspended time. That is not a nicety - a stopped child by definition
    stops advancing, so a running stall clock would kill it at `stall_seconds` and destroy
    exactly the work suspension exists to keep.

    The ffmpeg spawn is a non-idempotent side effect, so no caller may wrap this in a
    retry_on_locked closure (CLAUDE.md). The child is terminated on every exit path - and
    terminate_or_kill() continues a suspended child first, so a suspended run is torn down
    like any other.
    """
    if progress_signal not in PROGRESS_SIGNALS:
        raise ValueError(f'unknown progress_signal {progress_signal!r}')

    # Scratch (-progress + stderr tail) sits alongside the output file - both are tiny (KB),
    # and this keeps them inside whatever dir the output lives in rather than a hardcoded
    # /dvr/tmp (which would escape a test sandbox).
    #
    # The filename carries a per-attempt token, and leftovers from earlier attempts are
    # reaped below, because the poll loop reads the progress file on its FIRST pass -
    # milliseconds after Popen, inside the ~0.25s window before ffmpeg truncates it. On a
    # fixed filename that read returns the previous attempt's out_time, GrowthMonitor
    # latches it as a high-water mark the new attempt can never beat, and the job is killed
    # as stalled at exactly stall_seconds. A shutdown mid-run skips the finally-block
    # unlink, so stale files are routine. See dev/docs/BUGS.md 2026-07-23.
    scratch_dir = os.path.dirname(output_path) or '.'
    # Both patterns are listed explicitly per key: a bare f'{scratch_key}*' glob would let
    # id 6 match id 64's scratch files.
    stale_paths = (
        glob.glob(os.path.join(scratch_dir, f'.{scratch_prefix}-progress-{scratch_key}-*.txt'))
        + glob.glob(os.path.join(scratch_dir, f'.{scratch_prefix}-stderr-{scratch_key}-*.log'))
        + [os.path.join(scratch_dir, f'.{scratch_prefix}-progress-{scratch_key}.txt'),
           os.path.join(scratch_dir, f'.{scratch_prefix}-stderr-{scratch_key}.log')])
    for stale in stale_paths:
        try:
            os.unlink(stale)
        except OSError:
            pass  # best-effort reap; the unique token below is what actually guarantees safety
    token = uuid.uuid4().hex[:8]
    progress_path = os.path.join(scratch_dir, f'.{scratch_prefix}-progress-{scratch_key}-{token}.txt')
    stderr_path = os.path.join(scratch_dir, f'.{scratch_prefix}-stderr-{scratch_key}-{token}.log')

    # -nostdin: never block on a tty. -progress: machine-readable stats to a file.
    # -stats_period sets how often ffmpeg writes them.
    full_cmd = cmd[:1] + ['-nostdin'] + cmd[1:]
    # Insert -progress/-stats_period just before the output path (last arg).
    full_cmd = full_cmd[:-1] + ['-progress', progress_path, '-stats_period', str(interval)] + full_cmd[-1:]

    started = time.monotonic()
    stderr_fh = open(stderr_path, 'wb')
    proc = subprocess.Popen(full_cmd, stdin=subprocess.DEVNULL,
                            stdout=subprocess.DEVNULL, stderr=stderr_fh)

    growth = GrowthMonitor()
    # Latched the first time the progress signal moves past 0, and never cleared: the
    # pre-output budget is spent once and does not come back if ffmpeg later pauses. A pause
    # after output has started is a stall, and stall_seconds is what judges it.
    output_started = False
    last_mark = 0
    stall_watching = bool(stall_seconds and stall_seconds > 0)
    long_run_noted = False
    # Suspension bookkeeping. `suspended_since` is the monotonic instant the child was
    # stopped (None while it is running) and `suspended_total` is how much of the elapsed
    # wall clock has been spent stopped - every budget below reads the difference, so no
    # clock advances while the child is not.
    suspended_since = None
    suspended_total = 0.0
    result = None
    try:
        if on_spawn is not None:
            on_spawn(proc)
        while True:
            finished = proc.poll() is not None
            now = time.monotonic()
            paused_now = (now - suspended_since) if suspended_since is not None else 0.0
            wall = now - started - suspended_total - paused_now

            out_time_us, total_size, _done = read_progress_tail(progress_path)
            try:
                size = os.path.getsize(output_path)
            except OSError:
                size = 0
            if total_size:
                size = max(size, total_size)
            out_time = (out_time_us / 1e6) if out_time_us else 0.0

            # Nothing moves while the child is stopped, so a tick per poll would publish the
            # same numbers for hours. The tick taken on the way INTO suspension is the last
            # one, and it is the state the pause froze at. A suspended child that has
            # nevertheless finished was killed from outside (a cancel, a shutdown), and that
            # final tick is still published.
            if on_progress is not None and (suspended_since is None or finished):
                on_progress(wall, out_time, size)

            if finished:
                rc = proc.returncode
                if rc == 0:
                    result = SupervisedRun('success', returncode=rc, out_time=out_time, size=size)
                else:
                    result = SupervisedRun(
                        'died', returncode=rc, out_time=out_time, size=size,
                        error_msg=read_stderr_tail(stderr_path) or f'ffmpeg exited {rc}')
                break

            if suspend_check is not None:
                reason = suspend_check(wall, out_time, size)
                if reason is not None and suspended_since is None:
                    if suspend_process(proc):
                        suspended_since = now
                        log.info('%s suspended after %.0fs: %s', label, wall, reason)
                        if on_suspend is not None:
                            on_suspend(reason)
                elif reason is None and suspended_since is not None:
                    resume_process(proc)
                    paused_for = now - suspended_since
                    suspended_total += paused_for
                    suspended_since = None
                    # The stall clock restarts from now rather than counting the pause: the
                    # child has had no opportunity to advance, so anything it accumulated
                    # before being stopped would be charged against work it could not do.
                    growth.reset()
                    log.info('%s resumed after %.0fs suspended', label, paused_for)
                    if on_resume is not None:
                        on_resume()

            if suspended_since is not None:
                # Every budget below is skipped, not merely reset: a stopped child cannot
                # advance, produce first output, or exit, so each of them would be judging it
                # on work it was told not to do.
                time.sleep(interval)
                continue

            mark = (out_time_us or 0) if progress_signal == 'out_time' else (size or 0)
            if mark > 0:
                output_started = True
                last_mark = mark

            if stall_watching and output_started:
                # last_mark, not the value just read: a poll can legitimately read nothing
                # back while ffmpeg truncates and rewrites its -progress file, and the
                # monitor must see the high-water mark rather than a None. Feeding the last
                # known value (not skipping the update) is deliberate - it keeps the stall
                # clock running through an unreadable stretch, so a hung ffmpeg whose
                # progress file has gone quiet is still caught rather than running forever.
                stalled_for = growth.update(last_mark)
                if stalled_for >= stall_seconds:
                    log.warning('%s stalled for %.0fs (%s stopped advancing) - killing',
                                label, stalled_for, _SIGNAL_NOUN[progress_signal])
                    terminate_or_kill(proc, hard=True)
                    result = SupervisedRun(
                        'stalled', out_time=out_time, size=size, stalled_for=stalled_for,
                        error_msg=f'No {noun} progress for {int(stalled_for)}s')
                    break

            if not output_started and wall >= pre_output_timeout:
                log.warning('%s produced no output in %ss - killing', label, pre_output_timeout)
                terminate_or_kill(proc, hard=True)
                result = SupervisedRun(
                    'no_output', out_time=out_time, size=size,
                    error_msg=f'{noun.capitalize()} produced no output in {pre_output_timeout}s')
                break

            # Once output is advancing there is no upper bound - EXCEPT when the operator has
            # turned stall detection off, which leaves nothing watching liveness at all. The
            # old wall clock stands in for it there, the same way run_probe_until_stalled
            # falls back to one when /proc offers no progress signal. Never widen this to the
            # stall_seconds>0 case: that reinstates the deadline this function exists to remove.
            if output_started and not stall_watching and wall >= pre_output_timeout:
                log.warning('%s exceeded %ss with stall detection disabled - killing',
                            label, pre_output_timeout)
                terminate_or_kill(proc, hard=True)
                result = SupervisedRun(
                    'timeout', out_time=out_time, size=size,
                    error_msg=f'{noun.capitalize()} ran {pre_output_timeout}s with '
                              f'stall detection disabled')
                break

            # A job outliving what used to be its whole budget is now routine, so it is said
            # out loud once rather than left as an unexplained multi-hour gap in the log.
            if output_started and stall_watching and not long_run_noted and wall >= pre_output_timeout:
                long_run_noted = True
                log.info('%s has run %.0fs and is still advancing (%s written) - no deadline '
                         'applies while it progresses', label, wall, size)

            time.sleep(interval)
    finally:
        terminate_or_kill(proc)
        stderr_fh.close()
        if on_exit is not None:
            on_exit(stderr_path, proc.returncode)
        for p in (progress_path, stderr_path):
            try:
                os.unlink(p)
            except OSError:
                pass  # best-effort scratch cleanup

    return result
