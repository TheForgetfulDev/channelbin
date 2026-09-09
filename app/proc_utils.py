"""Process/ffmpeg helpers - canonical home per CLAUDE.md's coding-standards table."""
import os
import subprocess
import time
from urllib.parse import urlsplit

from .config import resolve_ffmpeg_path


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


def terminate_or_kill(proc, timeout: float = 5.0, hard: bool = False):
    """Stop a child process: SIGTERM, escalating to SIGKILL if it hasn't exited
    within timeout. hard=True skips straight to SIGKILL. No-op if proc is None or
    already exited; always reaps after SIGKILL so no zombie is left."""
    if proc is None or proc.poll() is not None:
        return
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
