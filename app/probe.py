"""Shared ffprobe utility used by channel tester and post-processor."""
import heapq
import json
import logging
import math
import os
import queue
import re
import subprocess
import threading
import time

from .config import resolve_ffprobe_path

log = logging.getLogger(__name__)

# Latched, so a machine with no ffprobe pays for the report once rather than on every
# probe: every capture segment, every health check and every conversion reaches one of the
# three spawn sites below, and re-running the toolchain probe (two process spawns) for each
# of them would turn a configuration error into a performance one.
_missing_lock = threading.Lock()
_missing_reported = False


def _report_ffprobe_unusable(ffprobe_path: str, exc: OSError, context: str) -> None:
    """Record that ffprobe itself could not be RUN.

    This is the one probe failure that is a configuration error rather than a fact about
    the file, and keeping the two apart is the whole point of it having a function. Every
    other failure below - a non-zero exit, unparseable JSON, no video stream - describes
    what was probed, and {} is a correct answer for it. A spawn that fails on argv[0]
    describes this machine: ffprobe is absent, or is there and not executable. Until
    dev/changelog/911 both arrived as the same empty dict, and three surfaces rendered a
    verdict off it that blamed a provider's stream for ChannelBin's own install.

    Conclusive rather than inferred, per CLAUDE.md's "already done is a fact you recorded":
    the OSError is proof that the binary at that path could not be executed just now, so
    this never has to consult a cache that might predate the tool being removed. It resets
    that cache instead, which is what lets the standing alert appear for an ffprobe that
    went missing AFTER startup - toolchain.report_tool_state() otherwise only ever runs at
    create_app() and on a settings save.

    Never raises. It runs inside the exception handling of a probe that has already failed,
    and a diagnostic that can mask the failure it is describing is worse than no diagnostic.
    """
    global _missing_reported
    with _missing_lock:
        if _missing_reported:
            return
        _missing_reported = True
    try:
        from . import toolchain
        log.warning(
            'Could not run ffprobe (looked for it at: %s): %s. %s Probe requested by: %s',
            ffprobe_path, exc, toolchain.CONSEQUENCE['ffprobe'], context)
        # Outside the latch lock: this spawns two processes, and holding a module lock
        # across them would serialize every thread that hits the same failure.
        toolchain.reset_cache()
        toolchain.report_tool_state(f'a probe of {context}')
    except Exception:
        log.exception('Could not report the missing ffprobe')


# How often scan_video_timeline samples its child's read counter for a progress hook.
# Matched to run_probe_until_stalled's 1s poll rather than to anything the scan does, so
# the two passes of one analysis advance at the same visible cadence; the surfaces reading
# them redraw every 15s, so a finer sample would be thrown away.
_SCAN_PROGRESS_INTERVAL = 1.0


def _bytes_read(pid: int):
    """Bytes this process has read via syscalls, or None when /proc can't answer.

    rchar, not read_bytes: read_bytes counts block-device I/O and stays at 0 for a file
    on a network mount, which is exactly where the long probes in this app run. Measured
    on the CIFS /dvr mount 2026-08-24 - rchar advances at ~155 MB/s there, read_bytes
    never leaves 0.
    """
    try:
        with open(f'/proc/{pid}/io') as fh:
            for line in fh:
                if line.startswith('rchar:'):
                    return int(line.split(':', 1)[1])
    except (OSError, ValueError):
        return None
    return None


def _report_progress(on_progress, read_bytes):
    """Hand one bytes-read sample to a caller's progress hook, never raising.

    A progress report is a diagnostic, and a diagnostic that can kill the pass it is
    describing is worse than no diagnostic (CLAUDE.md). The hook is supplied by a surface
    that only wants to draw a percentage, so anything it throws is swallowed here rather
    than propagating into a probe the recording depends on.
    """
    if on_progress is None or read_bytes is None:
        return
    try:
        on_progress(read_bytes)
    except Exception:
        log.exception('A probe progress hook raised; the probe itself is unaffected')


def run_probe_until_stalled(cmd, stall_timeout: int, fallback_timeout: int,
                            poll_interval: float = 1.0, on_progress=None):
    """Run a probe that reads a whole file, bounded by progress rather than wall clock.

    A fixed deadline is the wrong tool for a probe whose runtime scales with file size:
    -count_packets on a 17.9 GB capture reads the entire file, which takes ~124s on this
    machine's mount and so could never finish inside the 60s deadline it used to be given.
    Every recording of that size silently lost its health data (dev/docs/BUGS.md
    2026-08-24). What actually distinguishes a working probe from a hung one is whether it
    is still reading, so that is what is measured: the process is killed only after
    stall_timeout seconds with no advance in bytes read.

    fallback_timeout is a plain wall-clock deadline used only when /proc gives no progress
    signal - without one, an unreadable /proc would turn every probe unbounded.

    on_progress(read_bytes) is called with each new sample of that same counter, so a
    caller that wants to report how far the read has got pays nothing extra: the sample is
    already being taken to decide whether the probe has stalled. It is not called at all on
    a machine whose /proc gives no signal, which is the same condition that drops this
    function back to the wall-clock deadline - absent progress reads as absent, never 0%.

    Returns (returncode, stdout_text). Raises subprocess.TimeoutExpired on a stall, so
    callers can treat it exactly as they treat subprocess.run's timeout.
    """
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
    # A reader thread, never an undrained pipe: ffprobe's JSON is small today, but an
    # undrained PIPE buffer is the deadlock that once froze every recording in this app
    # at ~7.8 minutes (dev/changelog/430), and this process can run for minutes.
    chunks = []
    reader = threading.Thread(target=lambda: chunks.append(proc.stdout.read()), daemon=True)
    reader.start()

    started = time.monotonic()
    last_progress = started
    last_read = _bytes_read(proc.pid)
    have_signal = last_read is not None
    _report_progress(on_progress, last_read)
    try:
        while True:
            try:
                proc.wait(timeout=poll_interval)
                break
            except subprocess.TimeoutExpired:
                pass

            now = time.monotonic()
            if not have_signal:
                if now - started > fallback_timeout:
                    raise subprocess.TimeoutExpired(cmd, fallback_timeout)
                continue

            current = _bytes_read(proc.pid)
            _report_progress(on_progress, current)
            if current is not None and current > last_read:
                last_read = current
                last_progress = now
            elif now - last_progress > stall_timeout:
                raise subprocess.TimeoutExpired(cmd, stall_timeout)
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait()
        reader.join(timeout=5)
        proc.stdout.close()  # probes run per recording and per channel test; don't leak fds

    elapsed = time.monotonic() - started
    if elapsed > fallback_timeout:
        # The whole point of this function is that a long probe is legitimate, so say how
        # long it took rather than leaving an unexplained multi-minute gap in the log.
        log.info('Probe read the file for %.0fs (still making progress throughout): %s',
                 elapsed, cmd[-1])
    return proc.returncode, (chunks[0] if chunks else '')


def parse_pix_fmt(pix_fmt):
    """(bit_depth, chroma_subsampling) parsed from an ffprobe pix_fmt string.

    'yuv420p'->(8,'420'), 'yuv420p10le'->(10,'420'), 'yuv444p12le'->(12,'444'),
    'yuvj420p'->(8,'420'). Either element is None when it can't be determined
    (e.g. rgb24, gray). Standard 8-bit planar formats carry no depth digits, so a
    recognized chroma with no 'p<digits>' token means 8-bit."""
    if not pix_fmt:
        return None, None
    chroma = None
    for sub in ('444', '440', '422', '420', '411', '410'):
        if sub in pix_fmt:
            chroma = sub
            break
    m = re.search(r'p(\d+)', pix_fmt)
    bit_depth = int(m.group(1)) if m else (8 if chroma else None)
    return bit_depth, chroma


def bits_per_pixel_frame(bitrate_bps, width, height, fps):
    """Normalized compression-efficiency stat: bits the encoder spends per pixel per
    frame (bitrate / (w*h*fps)). A standard efficiency metric, NOT a better/worse
    verdict (DESIGN-stream-quality-profile.md §5). Pure; None if any input is missing
    or non-positive."""
    if not bitrate_bps or not width or not height or not fps:
        return None
    px = width * height * fps
    if px <= 0:
        return None
    return round(bitrate_bps / px, 4)


def expected_frame_count(fps, dts_span_seconds=None, fallback_duration=None):
    """How many video frames a clip of this length should hold. None if unknowable.

    Measured over the DECODE span (scan_video_timeline's dts_span_seconds) whenever one is
    available, and only over a duration as a fallback. Both alternatives to it are biased
    low in a way that reads as dropped frames on a complete capture, and the bias is a
    fixed number of frames per clip - so it is invisible on a long recording and dominates
    a short test (dev/changelog/896):

    - The CONTAINER duration spans every stream, so audio that starts before the first
      video frame lengthens it while contributing no frames.
    - The PTS span overshoots the capture window by the reorder depth. A stream copy bounded
      by -t cuts on DTS, so the last packets written are anchor frames presenting several
      frames into the future while the B-frames between them fall past the cut. Measured on
      a 4K50 HEVC feed with reorder depth 9: a 5s capture read 96.9% complete and a 20s
      capture of the same feed in the same minute read 99.8%, with zero DTS gaps in either.

    The +1 is not a fudge: N frames span N-1 intervals, so a clean 5s/50fps capture holds
    251 frames across a 5.000s decode span. Omitting it reads 100.4%.
    """
    if not fps or fps <= 0:
        return None
    if dts_span_seconds is not None and dts_span_seconds >= 0:
        return dts_span_seconds * fps + 1
    if fallback_duration and fallback_duration > 0:
        return fps * fallback_duration
    return None


def _rate_to_float(raw):
    """ffprobe rational string ('30000/1001') to float, or None if unparseable/zero."""
    if not raw or '/' not in raw:
        return None
    try:
        num, den = raw.split('/')
        num, den = int(num), int(den)
        if den <= 0 or num <= 0:
            return None
        return num / den
    except (ValueError, ZeroDivisionError):
        return None


def detect_vfr(r_frame_rate, avg_frame_rate):
    """True when the stream looks variable-frame-rate: r_frame_rate (nominal) and
    avg_frame_rate (actual average) disagree by more than 2%. False when they agree
    (CFR). None when either rate can't be parsed. avg is legitimately lower than r on
    a feed with timeline gaps, which is exactly the VFR signal we want to surface."""
    r = _rate_to_float(r_frame_rate)
    a = _rate_to_float(avg_frame_rate)
    if not r or not a:
        return None
    return abs(r - a) / r > 0.02

# A rate no real feed has is no rate. ffprobe reports r_frame_rate as the stream's timebase
# (90000/1 on MPEG-TS) when a capture holds too few frames to infer one - a segment that
# opened while the video was out - and that number reached a conversion as `-r 90000
# -fps_mode:v cfr`, which duplicates every frame about 3,000 times and turns a minute into
# hours (dev/changelog/1247). Broadcast tops out at 120 fps; the margin is for the odd
# high-rate feed, not for a timebase.
MAX_PLAUSIBLE_FPS = 240.0


def plausible_fps(value):
    """The float rate when it is one a video feed could run at, else None."""
    if value is None:
        return None
    try:
        value = float(value)
    except (TypeError, ValueError):
        return None
    return value if 0 < value <= MAX_PLAUSIBLE_FPS else None


# Seek-damage thresholds (see assess_seek_damage): a recording is "damaged" when its
# video timeline is missing more than this many seconds, or more than this fraction of
# the total span, whichever is larger. Calibrated against real captures 2026-07-17:
# clean recordings measure ~0s missing; the known-bad one measured ~1,259s (11.6%).
DAMAGE_MIN_MISSING_SECONDS = 10.0
DAMAGE_MIN_MISSING_FRACTION = 0.01

# Two capture frame rates count as the same rate below this relative spread. Matches
# detect_vfr()'s tolerance above, and for the same reason: 59.94 arrives as 60000/1001 from
# one probe and 59.94 from another, and a rate that only differs in rounding must not be
# reported as a rate change (dev/changelog/866).
RATE_SAME_TOLERANCE = 0.02


def parse_ffprobe(filepath: str, count_packets: bool = True, timeout: int = 60,
                  stall_timeout: int = 60, on_progress=None) -> dict:
    """Return dict with video/audio stream metadata and format info.

    Keys: resolution, fps, duration, bitrate_bps, frame_count, vid_width,
          vid_height, color_transfer, color_primaries, color_space, color_range,
          video_codec, pix_fmt, bit_depth,
          chroma_subsampling, interlaced, coded_resolution, is_vfr, audio_codec,
          audio_channels, audio_sample_rate, audio_bitrate_kbps, audio_language,
          video_tracks, audio_tracks.

    video_tracks/audio_tracks are lists of every stream of that type (always present,
    length 1 for the common single-track case) - {codec, resolution, language} for
    video, {codec, channels, sample_rate, bitrate_kbps, language} for audio. The
    scalar fields above (resolution, audio_codec, ...) always describe tracks[0], for
    every existing caller that only wants "the" video/audio stream.
    Returns {} on any error.

    count_packets=False skips -count_packets (which reads the WHOLE file) for
    a fast header-only probe - use it on live/large segment files where only
    stream format info is needed (frame_count comes back None).

    The two deadlines answer the two shapes of probe this function covers, and are not
    interchangeable. A header-only probe should finish in about a second, so `timeout` is
    a plain wall clock and a slow one is a hung one. A count_packets probe reads the whole
    file, so its runtime scales with size and no fixed clock can be right for both a 25 MB
    and a 19 GB capture; it is bounded by `stall_timeout` instead - seconds of no reading
    at all - with `timeout` left as the fallback for when /proc offers no progress signal.

    on_progress(read_bytes) reports how far that whole-file read has got, for a caller
    reporting a long pass to a user. It applies to the count_packets shape only: a
    header-only probe finishes in about a second and has nothing to report.
    """
    ffprobe_path = resolve_ffprobe_path()
    cmd = [
        ffprobe_path,
        '-v', 'quiet',
        '-print_format', 'json',
    ]
    if count_packets:
        cmd.append('-count_packets')
    cmd += [
        '-show_streams',
        '-show_format',
        filepath,
    ]
    try:
        # Nested deliberately, and only around the spawn: an OSError from these two lines
        # is the binary failing to execute, which is a fact about this machine, while
        # everything after them can only fail over what was probed. Catching both in one
        # handler is what let "there is no ffprobe here" arrive as the same empty dict as
        # "this file has no video stream" (dev/changelog/911).
        try:
            if count_packets:
                returncode, stdout = run_probe_until_stalled(
                    cmd, stall_timeout=stall_timeout, fallback_timeout=timeout,
                    on_progress=on_progress)
            else:
                result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
                returncode, stdout = result.returncode, result.stdout
        except OSError as exc:
            _report_ffprobe_unusable(ffprobe_path, exc, os.path.basename(filepath))
            return {}
        if returncode != 0:
            return {}
        data = json.loads(stdout)

        resolution = None
        fps = None
        vid_width = None
        vid_height = None
        color_transfer = None
        color_primaries = None
        color_space = None
        color_range = None
        video_codec = None
        pix_fmt = None
        bit_depth = None
        chroma_subsampling = None
        interlaced = None
        coded_resolution = None
        is_vfr = None
        frame_count = None
        audio_codec = None
        audio_channels = None
        audio_sample_rate = None
        audio_bitrate_kbps = None
        audio_language = None
        video_tracks = []
        audio_tracks = []

        for stream in data.get('streams', []):
            codec_type = stream.get('codec_type')
            if codec_type == 'video':
                w = stream.get('width')
                h = stream.get('height')
                track_resolution = f'{w}x{h}' if w and h else None
                video_tracks.append({
                    'codec': stream.get('codec_name'),
                    'resolution': track_resolution,
                    'language': (stream.get('tags') or {}).get('language') or None,
                })
                if resolution is not None:
                    continue

                resolution = track_resolution
                vid_width = w
                vid_height = h
                color_transfer = stream.get('color_transfer')
                color_primaries = stream.get('color_primaries')
                # The matrix and range halves of the same answer. zscale needs all four
                # before it will build a conversion path, and refuses the whole filter
                # graph when any one is missing (screenshot.py::hdr_input_params,
                # dev/changelog/915). ffprobe prints 'unknown' rather than omitting the
                # key when a stream states nothing, so a caller reading these has to
                # treat that string as absent.
                color_space = stream.get('color_space')
                color_range = stream.get('color_range')

                video_codec = stream.get('codec_name')
                pix_fmt = stream.get('pix_fmt')
                bit_depth, chroma_subsampling = parse_pix_fmt(pix_fmt)

                field_order = stream.get('field_order')
                if field_order == 'progressive':
                    interlaced = False
                elif field_order in ('tt', 'bb', 'tb', 'bt'):
                    interlaced = True
                # else: absent/unknown - leave None rather than guessing interlaced

                cw = stream.get('coded_width')
                ch = stream.get('coded_height')
                # Only meaningful when the coded frame differs from the displayed one
                # (e.g. 1080 encoded as 1088); otherwise it's noise, so leave None.
                if cw and ch and (cw != w or ch != h):
                    coded_resolution = f'{cw}x{ch}'

                is_vfr = detect_vfr(stream.get('r_frame_rate'), stream.get('avg_frame_rate'))

                raw_fc = stream.get('nb_read_packets')
                if raw_fc:
                    try:
                        frame_count = int(raw_fc)
                    except (ValueError, TypeError):
                        pass

                for fps_key in ('avg_frame_rate', 'r_frame_rate'):
                    raw = stream.get(fps_key, '')
                    if '/' in raw:
                        try:
                            num, den = raw.split('/')
                            if int(den) > 0:
                                # A timebase reported as a rate (90000/1) is skipped, not
                                # stored: see plausible_fps.
                                val = plausible_fps(round(int(num) / int(den), 2))
                                if val:
                                    fps = val
                                    break
                        except (ValueError, ZeroDivisionError):
                            pass

            elif codec_type == 'audio':
                track_sample_rate = None
                sr = stream.get('sample_rate')
                if sr:
                    try:
                        track_sample_rate = int(sr)
                    except (ValueError, TypeError):
                        pass
                track_bitrate_kbps = None
                raw_abr = stream.get('bit_rate')
                if raw_abr:
                    try:
                        track_bitrate_kbps = int(raw_abr) / 1000
                    except (ValueError, TypeError):
                        pass
                audio_tracks.append({
                    'codec': stream.get('codec_name'),
                    'channels': stream.get('channels'),
                    'sample_rate': track_sample_rate,
                    'bitrate_kbps': track_bitrate_kbps,
                    'language': (stream.get('tags') or {}).get('language') or None,
                })
                if audio_codec is not None:
                    continue

                audio_codec = stream.get('codec_name')
                audio_channels = stream.get('channels')
                audio_sample_rate = track_sample_rate
                audio_bitrate_kbps = track_bitrate_kbps
                audio_language = (stream.get('tags') or {}).get('language') or None

        fmt = data.get('format', {})
        duration = None
        raw_dur = fmt.get('duration')
        if raw_dur:
            try:
                duration = float(raw_dur)
            except (ValueError, TypeError):
                pass

        bitrate_bps = None
        raw_br = fmt.get('bit_rate')
        if raw_br:
            try:
                bitrate_bps = int(raw_br)
            except (ValueError, TypeError):
                pass

        return {
            'resolution': resolution,
            'fps': fps,
            'duration': duration,
            'bitrate_bps': bitrate_bps,
            'vid_width': vid_width,
            'vid_height': vid_height,
            'color_transfer': color_transfer,
            'color_primaries': color_primaries,
            'color_space': color_space,
            'color_range': color_range,
            'video_codec': video_codec,
            'pix_fmt': pix_fmt,
            'bit_depth': bit_depth,
            'chroma_subsampling': chroma_subsampling,
            'interlaced': interlaced,
            'coded_resolution': coded_resolution,
            'is_vfr': is_vfr,
            'frame_count': frame_count,
            'audio_codec': audio_codec,
            'audio_channels': audio_channels,
            'audio_sample_rate': audio_sample_rate,
            'audio_bitrate_kbps': audio_bitrate_kbps,
            'audio_language': audio_language,
            'video_tracks': video_tracks,
            'audio_tracks': audio_tracks,
        }
    except Exception as exc:
        log.debug('ffprobe failed for %s: %s', filepath, exc)
    return {}


def nominal_video_rate(filepath: str, timeout: int = 60):
    """Nominal video frame rate as ffprobe's raw r_frame_rate rational ('60000/1001'),
    or None. r_frame_rate, not avg_frame_rate - avg is skewed low by timeline gaps in
    damaged captures, which is exactly when callers need the true rate.

    An r_frame_rate no feed could run at (the 90000/1 timebase a frameless stretch
    reports) is not returned: avg_frame_rate stands in when it is plausible, else None,
    so the caller falls back on the rates the segments were probed at rather than
    converting at 90,000 fps (dev/changelog/1247)."""
    ffprobe_path = resolve_ffprobe_path()
    try:
        cmd = [ffprobe_path, '-v', 'quiet', '-print_format', 'json',
               '-select_streams', 'v:0', '-show_streams', filepath]
        try:
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        except OSError as exc:
            # See parse_ffprobe: a spawn that fails on argv[0] is this machine's problem,
            # not the file's, and must not be reported as "no nominal rate".
            _report_ffprobe_unusable(ffprobe_path, exc, os.path.basename(filepath))
            return None
        if result.returncode != 0:
            return None
        streams = json.loads(result.stdout).get('streams', [])
        return plausible_nominal_rate(streams[0] if streams else {})
    except (subprocess.SubprocessError, OSError, ValueError, json.JSONDecodeError) as exc:
        log.debug('nominal_video_rate failed for %s: %s', filepath, exc)
    return None


def plausible_nominal_rate(stream: dict):
    """The rational rate to call a stream's nominal rate, from its ffprobe stream dict:
    r_frame_rate when it is a rate a feed could run at, else avg_frame_rate when that is,
    else None. Pure - split out of nominal_video_rate so the timebase case is testable
    without a file."""
    for key in ('r_frame_rate', 'avg_frame_rate'):
        raw = stream.get(key, '') or ''
        if '/' not in raw:
            continue
        num, den = raw.split('/')
        if int(den) > 0 and int(num) > 0 and plausible_fps(int(num) / int(den)):
            return raw
    return None


def stream_ends(filepath: str, timeout: int = 120) -> dict:
    """Where each video and audio stream of a file ends, in seconds from the file's own start.

    Returns {'duration': <format duration or None>, 'video': [ends], 'audio': [ends]}, or {}
    when ffprobe cannot read the file. A stream that states no duration is left out of its
    list rather than guessed at.

    The format duration is the LONGEST stream, so it overstates how far a file reaches
    whenever one track runs on past the other. A killed conversion part whose audio ran 7
    minutes past its video was measured that way, and the resume skipped the video in between
    (dev/changelog/1194). Header-only: no packet is read.
    """
    ffprobe_path = resolve_ffprobe_path()
    cmd = [ffprobe_path, '-v', 'quiet', '-print_format', 'json',
           '-show_entries', 'format=start_time,duration:stream=codec_type,start_time,duration',
           filepath]
    try:
        try:
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        except OSError as exc:
            # See parse_ffprobe: a spawn that fails on argv[0] is this machine's problem.
            _report_ffprobe_unusable(ffprobe_path, exc, os.path.basename(filepath))
            return {}
        if result.returncode != 0:
            return {}
        data = json.loads(result.stdout)
    except (subprocess.SubprocessError, ValueError) as exc:
        log.debug('stream_ends failed for %s: %s', filepath, exc)
        return {}

    def _num(raw):
        try:
            return float(raw)
        except (TypeError, ValueError):
            return None

    fmt = data.get('format') or {}
    origin = _num(fmt.get('start_time')) or 0.0
    out = {'duration': _num(fmt.get('duration')), 'video': [], 'audio': []}
    for stream in data.get('streams') or []:
        kind = stream.get('codec_type')
        if kind not in ('video', 'audio'):
            continue
        dur = _num(stream.get('duration'))
        if dur is None:
            continue
        start = _num(stream.get('start_time')) or 0.0
        out[kind].append(start + dur - origin)
    return out


def tail_track_ends(filepath: str, window: float = 30.0, timeout: int = 120) -> dict:
    """Where the first video and the first audio track end, in seconds from the file's own
    start, read from the packets at the end of the file. Returns {'video': end, 'audio': end}
    with a key left out when that track has no packet in the tail read, or {} when ffprobe
    cannot read the file.

    stream_ends() cannot answer this on an MPEG-TS whose video stops well before its audio:
    ffprobe estimates a TS stream's duration from the file's last few hundred KB, so a video
    track that ended a minute before the audio states no duration at all (measured on ffmpeg
    7.1 here). This seeks `window` seconds before the end instead - the seek lands on the
    last video packet at or before that point, however far back it is - and reads to EOF.
    The cost is the tail itself: about 22 MB for a 15-minute audio-only overhang.
    """
    ffprobe_path = resolve_ffprobe_path()
    head = [ffprobe_path, '-v', 'quiet', '-print_format', 'json',
            '-show_entries', 'format=start_time,duration:stream=index,codec_type', filepath]
    try:
        try:
            result = subprocess.run(head, capture_output=True, text=True, timeout=timeout)
        except OSError as exc:
            # See parse_ffprobe: a spawn that fails on argv[0] is this machine's problem.
            _report_ffprobe_unusable(ffprobe_path, exc, os.path.basename(filepath))
            return {}
        if result.returncode != 0:
            return {}
        data = json.loads(result.stdout)
    except (subprocess.SubprocessError, ValueError) as exc:
        log.debug('tail_track_ends failed for %s: %s', filepath, exc)
        return {}

    fmt = data.get('format') or {}
    origin = _packet_time(str(fmt.get('start_time', ''))) or 0.0
    duration = _packet_time(str(fmt.get('duration', '')))
    kinds = {}
    for stream in data.get('streams') or []:
        kind = stream.get('codec_type')
        if kind in ('video', 'audio') and kind not in kinds.values():
            kinds[stream.get('index')] = kind
    if not duration or not kinds:
        return {}

    target = max(origin, origin + duration - window)
    ends = {}

    def _on_line(line):
        fields = line.strip().rstrip(',').split(',')
        if len(fields) < 2:
            return
        try:
            kind = kinds.get(int(fields[0]))
        except ValueError:
            return
        pts = _packet_time(fields[1])
        if kind is None or pts is None:
            return
        dur = _packet_time(fields[2]) if len(fields) > 2 else None
        end = pts + (dur or 0.0)
        if end > ends.get(kind, float('-inf')):
            ends[kind] = end

    tail = [ffprobe_path, '-v', 'quiet', '-read_intervals', f'{target:.3f}%',
            '-show_entries', 'packet=stream_index,pts_time,duration_time', '-of', 'csv=p=0',
            filepath]
    try:
        returncode = _stream_probe_lines(tail, _on_line, timeout=timeout)
    except OSError as exc:
        _report_ffprobe_unusable(ffprobe_path, exc, os.path.basename(filepath))
        return {}
    except subprocess.TimeoutExpired:
        log.warning('Reading the end of %s stalled for %ds - track ends unknown',
                    os.path.basename(filepath), timeout)
        return {}
    if returncode != 0:
        return {}
    return {kind: end - origin for kind, end in ends.items()}


def _nominal_fps(filepath: str) -> float | None:
    raw = nominal_video_rate(filepath)
    if raw:
        num, den = raw.split('/')
        return int(num) / int(den)
    return None


def _packet_time(raw: str):
    """One ffprobe packet timestamp field to float, or None for 'N/A'/blank/garbage."""
    raw = raw.strip()
    if not raw or raw == 'N/A':
        return None
    try:
        return float(raw)
    except ValueError:
        return None


def effective_capture_fps(segment_rates) -> tuple:
    """Reduce [(duration_seconds, fps), ...] to (effective_fps, distinct_rates).

    effective_fps is the duration-weighted arithmetic mean, which is the rate that makes
    `packet_count / fps` mean "seconds of content" on a file assembled from stretches
    captured at different rates: expected frames over a span that ran fraction p at rate A
    and the rest at rate B is span * (p*A + (1-p)*B). A harmonic or unweighted mean is not
    the same number and is not the right one.

    distinct_rates is the sorted set of rates the segments actually ran at, collapsed with
    RATE_SAME_TOLERANCE - so a recording that never changed rate returns exactly one entry
    however its rate was spelled. Returns (None, []) when no pair carries both a positive
    duration and a positive rate; unknown must stay distinguishable from measured.
    """
    usable = [(float(d), float(f)) for d, f in (segment_rates or [])
              if d and f and d > 0 and f > 0]
    if not usable:
        return None, []
    total = sum(d for d, _f in usable)
    effective = sum(d * f for d, f in usable) / total

    distinct = []
    for _d, f in sorted(usable, key=lambda p: p[1]):
        if not distinct or abs(f - distinct[-1]) / distinct[-1] > RATE_SAME_TOLERANCE:
            distinct.append(f)
    return effective, distinct


def _stream_probe_lines(cmd, on_line, *, timeout, on_progress=None, merge_stderr=False) -> int:
    """Run a whole-file ffprobe whose stdout is one record per line, hand each line to
    on_line as it arrives, and return the exit code. OSError from the spawn propagates for
    the caller to report; subprocess.TimeoutExpired is raised on inactivity.

    timeout bounds inactivity, not total wall time: a probe reading a large file
    legitimately runs for minutes, so the deadline must reset on every line rather than cap
    the read loop outright (dev/changelog/797's run_probe_until_stalled is the same shape,
    over bytes read; a line here is the cheaper, more direct progress signal). A reader
    thread is required to make that inactivity actually observable - reading proc.stdout
    directly in the loop blocks the whole thread on a stalled ffprobe with no way to notice
    no line has arrived.

    on_progress(read_bytes) is sampled on a clock rather than per line: a 6-hour capture is
    millions of lines, and an unthrottled read would turn one /proc open per second into one
    per line.
    """
    # merge_stderr: ffmpeg's filters log to stderr, and that log IS the record a measurement
    # reads. It is drained by the same reader as stdout, so it can never fill a pipe buffer.
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT if merge_stderr else subprocess.DEVNULL,
                            text=True, errors='replace')
    lines: queue.Queue = queue.Queue()

    def _reader():
        try:
            for line in proc.stdout:
                lines.put(line)
        finally:
            lines.put(None)

    reader = threading.Thread(target=_reader, daemon=True)
    reader.start()

    try:
        last_progress = time.monotonic()
        last_report = last_progress
        _report_progress(on_progress, _bytes_read(proc.pid))
        while True:
            try:
                line = lines.get(timeout=1.0)
            except queue.Empty:
                if time.monotonic() - last_progress > timeout:
                    raise subprocess.TimeoutExpired(cmd, timeout)
                continue
            if line is None:
                break
            last_progress = time.monotonic()
            if on_progress is not None and last_progress - last_report >= _SCAN_PROGRESS_INTERVAL:
                last_report = last_progress
                _report_progress(on_progress, _bytes_read(proc.pid))
            on_line(line)
        proc.wait(timeout=timeout)
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait()
        reader.join(timeout=5)
        proc.stdout.close()  # runs per recording and per channel test; don't leak fds
    return proc.returncode


#: A PQ or HLG transfer function is the only thing that proves a source is HDR. Frame width
#: is not, and the 3840-wide trigger this replaced is what made the screenshot HDR path
#: wrong: it pulled 4K *SDR* into the tonemap chain, where a bar pattern whose mean luma is
#: 102 came back at 66. A source that declares no transfer at all is left alone for the same
#: reason, in the other direction: unknown is not a proven HDR (dev/changelog/915).
HDR_TRANSFERS = ('smpte2084', 'arib-std-b67')


def _parse_sar(raw):
    """'4:3' -> 4/3; None for absent, '0:1' or 'N/A' - an unstated aspect is square."""
    try:
        num, den = (int(x) for x in (raw or '').split(':'))
    except ValueError:
        return None
    return num / den if num > 0 and den > 0 else None


#: How many format stretches survey_formats() lists. A feed that flaps between two formats
#: all night must not grow a DIAGNOSTICS row without bound; format_seconds still totals
#: every stretch and format_run_count says how many there were.
FORMAT_RUNS_LISTED = 50


class _FormatRuns:
    """survey_formats()' stretch accumulator: one keyframe at a time, in file order.

    A keyframe with no readable rate keeps the stretch's rate rather than starting a new
    one - unknown is not different. Rates within RATE_SAME_TOLERANCE are one rate, the same
    rule video_rates uses, so the two answers cannot disagree about how many rates there
    were."""

    def __init__(self):
        self.listed = []
        self.count = 0
        self._totals = {}
        self._t0 = None
        self._run = None
        self._last_t = None
        self._last_step = 0.0

    def add(self, t, width, height, rate):
        if t is None:
            return
        if self._t0 is None:
            self._t0 = t
        if self._last_t is not None and t > self._last_t:
            self._last_step = t - self._last_t
        self._last_t = t if self._last_t is None else max(t, self._last_t)
        run = self._run
        if run is not None:
            same_rate = (rate is None or run['rate'] is None
                         or abs(rate - run['rate']) / run['rate'] <= RATE_SAME_TOLERANCE)
            if (width, height) == (run['w'], run['h']) and same_rate:
                if run['rate'] is None:
                    run['rate'] = rate
                return
            self._close(t)
        self._run = {'start': t, 'w': width, 'h': height, 'rate': rate}

    def _close(self, end):
        run = self._run
        seconds = max(0.0, end - run['start'])
        key = (run['w'], run['h'], run['rate'])
        self._totals[key] = self._totals.get(key, 0.0) + seconds
        self.count += 1
        if len(self.listed) < FORMAT_RUNS_LISTED:
            self.listed.append([round(run['start'] - self._t0, 2), round(end - self._t0, 2),
                                run['w'], run['h'], run['rate']])

    def finish(self):
        if self._run is not None:
            self._close(self._last_t + self._last_step)
            self._run = None

    def totals(self):
        return [[w, h, rate, round(seconds, 2)]
                for (w, h, rate), seconds in self._totals.items()]


def survey_formats(filepath: str, timeout: int = 600, on_progress=None) -> dict:
    """Every picture size, audio layout, interlacing and transfer function the file carries,
    from ONE whole-file read: video keyframes only (`-skip_frame nokey`) and every audio
    frame.

    The segment rows' capture-time probes see only each segment's first frames, so a format
    change inside one segment - a stereo pre-roll before a 5.1 program with no restart, a
    provider switching feeds under one connection - is invisible to them. This is what sees
    it. Measured on this box (dev/changelog/1184): 12.8 s wall / 12.7 CPU-s for a 25 min
    1080p recording, about 2 minutes for 4 hours; 4K HEVC Main10 keyframes decode at about
    3 CPU-s per minute. Keyframes are enough for video because a picture size, an
    interlacing mode or a transfer function can only change at one.

    Returns {} on any failure (unknown must never read as "one format"), else:
        video_sizes     [[width, height, sar], ...] distinct, sar a float (1.0 when unstated)
        interlaced      True when any keyframe was coded interlaced
        video_rates     distinct frame rates, from each keyframe's own duration
        transfers       distinct color_transfer values a keyframe stated
        audio_channels  distinct channel counts across every audio stream
        keyframes, audio_frames  how much was seen, so an empty answer is explainable
        format_runs     [[start, end, width, height, rate], ...] - WHEN each picture size
                        and rate held, in seconds from the first keyframe, one entry per
                        unbroken stretch; the first FORMAT_RUNS_LISTED only
        format_run_count  how many stretches there were, listed or not
        format_seconds  [[width, height, rate, seconds], ...] - the total time at each,
                        over every stretch

    The runs are what tells a short stretch from a long one. The segment probes cannot:
    recording 16's two restarts each opened on a minute or two of 720p29.97 inside four
    hours of 1080p59.94, and recording 17's feed dropped to that rate 25s into a segment
    whose probe had read 1080p59.94 (dev/changelog/1201). A stretch ends at the keyframe
    that starts the next one, and the last one a keyframe interval after its last
    keyframe, so a boundary is placed to within one GOP.
    """
    if not filepath or not os.path.exists(filepath):
        return {}
    ffprobe_path = resolve_ffprobe_path()
    cmd = [ffprobe_path, '-v', 'error', '-skip_frame', 'nokey', '-show_entries',
           'frame=media_type,stream_index,pts_time,best_effort_timestamp_time,width,height,'
           'sample_aspect_ratio,interlaced_frame,color_transfer,duration_time,channels',
           '-of', 'compact=p=0', filepath]
    st = {'sizes': set(), 'interlaced': False, 'transfers': set(), 'channels': set(),
          'rates': [], 'video_stream': None, 'keyframes': 0, 'audio_frames': 0}
    runs = _FormatRuns()

    def _on_line(line):
        fields = dict(part.split('=', 1) for part in line.strip().split('|') if '=' in part)
        kind = fields.get('media_type')
        if kind == 'video':
            # The first video stream only: it is the one the conversion encodes.
            if st['video_stream'] is None:
                st['video_stream'] = fields.get('stream_index')
            if fields.get('stream_index') != st['video_stream']:
                return
            try:
                w, h = int(fields.get('width', 0)), int(fields.get('height', 0))
            except ValueError:
                return
            if w <= 0 or h <= 0:
                return
            st['keyframes'] += 1
            st['sizes'].add((w, h, round(_parse_sar(fields.get('sample_aspect_ratio')) or 1.0, 4)))
            if fields.get('interlaced_frame') == '1':
                st['interlaced'] = True
            # A keyframe's duration is one frame at the rate in force there, so a rate change
            # inside one segment shows up here as it does nowhere else.
            duration = _packet_time(fields.get('duration_time', ''))
            rate = None
            if duration and 0.001 < duration < 1.0:
                rate = round(1.0 / duration, 3)
                if not any(abs(rate - r) / r <= RATE_SAME_TOLERANCE for r in st['rates']):
                    st['rates'].append(rate)
            t = _packet_time(fields.get('pts_time', ''))
            if t is None:
                t = _packet_time(fields.get('best_effort_timestamp_time', ''))
            runs.add(t, w, h, rate)
            transfer = fields.get('color_transfer') or ''
            if transfer and transfer not in ('unknown', 'unspecified', 'reserved'):
                st['transfers'].add(transfer)
        elif kind == 'audio':
            try:
                n = int(fields.get('channels', 0))
            except ValueError:
                return
            if n > 0:
                st['audio_frames'] += 1
                st['channels'].add(n)

    try:
        returncode = _stream_probe_lines(cmd, _on_line, timeout=timeout, on_progress=on_progress)
    except OSError as exc:
        _report_ffprobe_unusable(ffprobe_path, exc, os.path.basename(filepath))
        return {}
    except subprocess.SubprocessError as exc:
        log.warning('Format survey failed for %s: %s', filepath, exc)
        return {}
    if returncode != 0 or not st['keyframes']:
        return {}
    runs.finish()
    return {
        'video_sizes': [list(s) for s in sorted(st['sizes'])],
        'interlaced': st['interlaced'],
        'video_rates': sorted(st['rates']),
        'transfers': sorted(st['transfers']),
        'audio_channels': sorted(st['channels']),
        'keyframes': st['keyframes'],
        'audio_frames': st['audio_frames'],
        'format_runs': runs.listed,
        'format_run_count': runs.count,
        'format_seconds': runs.totals(),
    }


#: What ebur128 reports for audio with nothing above its absolute gate - silence, or close
#: enough that there is no program loudness to measure.
LOUDNESS_SILENT_LUFS = -70.0

_EBUR128_LINE_RE = re.compile(r'^\[Parsed_ebur128_\d+ @ (0x[0-9a-f]+)\] t: *([0-9.]+).*?'
                              r'\bI: *(-?[0-9.]+) LUFS')


def combine_loudness(instances) -> float | None:
    """One integrated loudness from [(seconds, lufs), ...], one pair per ebur128 instance.

    ffmpeg rebuilds the audio filter graph when the decoded layout changes (stereo -> 5.1 at
    a program boundary), and each rebuild is a fresh ebur128 with its own integrated
    figure - measured on a stereo-then-5.1 file, two summaries (dev/changelog/1184). They
    are combined by duration-weighted ENERGY, which is how integrated loudness itself
    averages; a mean of the LUFS figures would weight a quiet stretch far above its share.
    The gate is per instance, so the result is an approximation across a layout change and
    exact for a file that never changed. None when nothing was measured.
    """
    usable = [(float(d), float(lufs)) for d, lufs in instances if d and d > 0]
    if not usable:
        return None
    total = sum(d for d, _ in usable)
    energy = sum(d * 10 ** (lufs / 10.0) for d, lufs in usable) / total
    if energy <= 10 ** (LOUDNESS_SILENT_LUFS / 10.0):
        return LOUDNESS_SILENT_LUFS
    return round(10.0 * math.log10(energy), 2)


def measure_loudness(filepath: str, ffmpeg_path: str, timeout: int = 600,
                     on_progress=None) -> float | None:
    """Integrated loudness (LUFS) of the audio the conversion would encode, or None.

    One ebur128 pass over the audio alone (-vn, ffmpeg's own default audio pick, the same
    stream the conversion takes). Measured at 116 CPU-s for 4 hours of audio, against ~11x
    that for a two-pass loudnorm (dev/changelog/1183). The per-100 ms log lines are read
    live rather than the closing summary, because they carry which ebur128 instance spoke
    and when, and a layout change splits the file across several (combine_loudness).
    """
    if not filepath or not os.path.exists(filepath):
        return None
    cmd = [ffmpeg_path, '-hide_banner', '-nostats', '-nostdin', '-loglevel', 'info',
           '-i', filepath, '-vn', '-sn', '-dn', '-af', 'ebur128', '-f', 'null', '-']
    instances = {}

    def _on_line(line):
        m = _EBUR128_LINE_RE.match(line)
        if not m:
            return
        key, t, lufs = m.group(1), float(m.group(2)), float(m.group(3))
        entry = instances.setdefault(key, [t, t, lufs])
        entry[1] = t
        entry[2] = lufs

    try:
        returncode = _stream_probe_lines(cmd, _on_line, timeout=timeout,
                                         on_progress=on_progress, merge_stderr=True)
    except (OSError, subprocess.SubprocessError) as exc:
        log.warning('Loudness measurement failed for %s: %s', filepath, exc)
        return None
    if returncode != 0:
        log.warning('Loudness measurement for %s exited %s', filepath, returncode)
        return None
    # Each log line is 100 ms of audio, so an instance's span is last - first plus one line.
    return combine_loudness([(last - first + 0.1, lufs)
                             for first, last, lufs in instances.values()])


def scan_video_timeline(filepath: str, gap_threshold: float = 0.25, timeout: int = 600,
                        expected_fps: float | None = None, on_progress=None,
                        max_gaps_listed: int = 20) -> dict:
    """Scan every video packet's timestamps and measure timeline damage.

    Gaps are counted on the DECODE timeline (dts_time), never the presentation timeline.
    DTS is monotonic and reflects the capture cadence, so a real dropout shows up as a DTS
    gap; PTS is reordered relative to decode order whenever the stream has B-frames, and a
    monotonic high-water mark over PTS counts every reorder excursion as missing time. That
    is not a theoretical concern: a healthy 25fps capture with reorder depth 3 (~0.32s
    excursions, over the 0.25s threshold) measured 39,155 gaps totalling 12,325s of "missing"
    video out of a 15,553s timeline - see dev/docs/BUGS.md 2026-07-23. Never reinstate
    PTS-based gap counting.

        span_seconds     max PTS - min PTS (what players treat as the duration)
        dts_span_seconds max DTS - min DTS: the window the capture actually covered, and
                         the only span an expected-frame count may be derived from. The
                         PTS span overshoots it by the reorder depth whenever a capture is
                         cut at a DTS boundary - the trailing anchor frames present several
                         frames into the future while the B-frames between them fall past
                         the cut - which reads as missing frames on a complete clip
                         (dev/changelog/896). None when DTS is absent, or when the decode
                         timeline steps backwards (a concatenation restarts it, so max-min
                         would span the joins rather than the capture)
        packet_count     video packets seen
        fps              nominal frame rate (r_frame_rate)
        deficit_basis    how deficit_seconds counted the packets' content time:
                         'frame_durations' when every packet carried its own duration,
                         'nominal_rate' when none did (each counts 1/deficit_fps), and
                         'partial_durations' when some did and the rest fell back
        deficit_fps      the rate a packet WITHOUT a duration was counted at - `fps` unless
                         the caller passed expected_fps. Unused on 'frame_durations'
        gap_basis        'dts'; 'none' on the rare stream carrying no DTS at all, where
                         gap_count is 0 and deficit_seconds alone carries the verdict.
                         assess_seek_damage() rewrites it to 'dts-post-concat' when it
                         knows the file is a join of several segments - this function
                         measures a file and cannot know where the file came from
        gap_threshold    the threshold gaps were counted against
        gap_count        gaps in decode time > gap_threshold
        gap_seconds      total time inside those gaps
        max_gap_seconds  largest single gap
        backward_count   DTS steps that went backwards (concat-boundary diagnostic)
        deficit_seconds  span minus the content time the packets account for - catches
                         micro-gaps below the threshold
        missing_seconds  max(gap_seconds, deficit_seconds) - the headline damage number
        gaps             [(start, end), ...] in seconds from the file's earliest presented
                         frame, in file order: the largest max_gaps_listed gaps only, so
                         gap_count may be larger than len(gaps)

    Each packet counts as its OWN duration, which the demuxer derives from the stream's
    timing at that point in the file, so a stretch at 29.97 fps counts 1/29.97 a frame and
    a stretch at 59.94 counts 1/59.94 - exact across any number of rate changes, including
    one inside a segment. Dividing the packet count by one rate is what this replaced: the
    header rate turns every frame of a lower-rate stretch into a missing frame (recording 2,
    1,200s missing against 0.41s of real damage, dev/changelog/866), and a rate blended from
    the segments' opening probes was blind when a probe caught only a short opening
    (recording 16 read 0.0s missing beside 96.3% of expected frames, dev/changelog/1201).
    Measured on every real capture in the GPU lab's clips and through the concat join:
    every packet carries a duration at its stretch's rate.

    expected_fps is the fallback for a packet that carries no duration, and only for that.
    The header rate is whatever the file's first stretch was captured at, so this function -
    which cannot know the file was concatenated, let alone from what - takes the corrected
    rate as a parameter, and assess_seek_damage() derives it from the segment rows.

    on_progress(read_bytes) reports how far the scan's own ffprobe has read, sampled from
    the same /proc counter run_probe_until_stalled uses. Sampled on a clock rather than per
    packet: this loop turns once per CSV line and a 6-hour capture is millions of them, so
    an unthrottled read would turn one /proc open per second into one per packet.

    Returns {} on any error (missing file, no video stream, ffprobe failure).
    """
    if not filepath or not os.path.exists(filepath):
        return {}
    ffprobe_path = resolve_ffprobe_path()
    try:
        fps = _nominal_fps(filepath)

        # ffprobe prints a section's fields in its own order, not show_entries' order; for a
        # packet that is pts_time, dts_time, duration_time.
        cmd = [ffprobe_path, '-v', 'error', '-select_streams', 'v:0',
               '-show_entries', 'packet=pts_time,dts_time,duration_time', '-of', 'csv=p=0',
               filepath]
        st = {'low': None, 'highwater': None, 'count': 0, 'prev_dts': None,
              'dts_low': None, 'dts_high': None, 'saw_dts': False, 'gap_count': 0,
              'gap_seconds': 0.0, 'max_gap': 0.0, 'backward_count': 0,
              'dur_seconds': 0.0, 'dur_count': 0}
        # A min-heap of the largest gaps, so a file with thousands of small ones costs a
        # fixed amount of memory and still names the ones worth reading about.
        largest = []

        def _on_line(line):
            fields = line.strip().rstrip(',').split(',')
            pts = _packet_time(fields[0] if fields else '')
            dts = _packet_time(fields[1] if len(fields) > 1 else '')
            # A packet with neither timestamp tells us nothing about the timeline.
            if pts is None and dts is None:
                return

            # Presentation timeline: span and packet count only, no gap accounting.
            # min/max rather than first/last because with B-frames the first packet in
            # storage order is not the earliest-presented one.
            stamp = pts if pts is not None else dts
            st['count'] += 1
            # Bounded like survey_formats' keyframe rate: a duration of a second or more is
            # not one frame of video, and must not be allowed to cover a real gap.
            duration = _packet_time(fields[2] if len(fields) > 2 else '')
            if duration is not None and 0.0 < duration < 1.0:
                st['dur_seconds'] += duration
                st['dur_count'] += 1
            if st['low'] is None or stamp < st['low']:
                st['low'] = stamp
            if st['highwater'] is None or stamp > st['highwater']:
                st['highwater'] = stamp

            # Decode timeline: the only basis gap accounting runs on. Packets missing a
            # DTS are skipped rather than substituted with PTS - mixing the two timelines
            # would manufacture a gap at every switch between them.
            if dts is not None:
                st['saw_dts'] = True
                if st['dts_low'] is None or dts < st['dts_low']:
                    st['dts_low'] = dts
                if st['dts_high'] is None or dts > st['dts_high']:
                    st['dts_high'] = dts
                if st['prev_dts'] is not None:
                    step = dts - st['prev_dts']
                    if step < 0:
                        st['backward_count'] += 1
                    elif step > gap_threshold:
                        st['gap_count'] += 1
                        st['gap_seconds'] += step
                        if step > st['max_gap']:
                            st['max_gap'] = step
                        entry = (step, st['prev_dts'], dts)
                        if len(largest) < max_gaps_listed:
                            heapq.heappush(largest, entry)
                        elif step > largest[0][0]:
                            heapq.heapreplace(largest, entry)
                st['prev_dts'] = dts

        try:
            returncode = _stream_probe_lines(cmd, _on_line, timeout=timeout,
                                             on_progress=on_progress)
        except OSError as exc:
            # See parse_ffprobe: this is the binary failing to execute, not a file with no
            # measurable timeline, and the two must not share one empty answer.
            _report_ffprobe_unusable(ffprobe_path, exc, os.path.basename(filepath))
            return {}

        low, highwater, count = st['low'], st['highwater'], st['count']
        saw_dts, backward_count = st['saw_dts'], st['backward_count']
        dts_low, dts_high = st['dts_low'], st['dts_high']
        gap_count, gap_seconds, max_gap = st['gap_count'], st['gap_seconds'], st['max_gap']
        if returncode != 0 or count < 2 or low is None:
            return {}

        span = highwater - low
        # Withheld on a file whose decode timeline restarts: max-min then measures the
        # widest timestamp in the file rather than the capture window, and None makes that
        # misuse impossible instead of merely documented.
        dts_span = (dts_high - dts_low) if (saw_dts and not backward_count) else None
        deficit_fps = expected_fps if expected_fps and expected_fps > 0 else fps
        dur_seconds, dur_count = st['dur_seconds'], st['dur_count']
        undated = count - dur_count
        if not undated:
            deficit_basis = 'frame_durations'
            deficit = span - dur_seconds
        else:
            deficit_basis = 'nominal_rate' if not dur_count else 'partial_durations'
            deficit = (span - dur_seconds - undated / deficit_fps) if deficit_fps else 0.0
        return {
            'span_seconds': span,
            'dts_span_seconds': dts_span,
            'packet_count': count,
            'fps': fps,
            'deficit_basis': deficit_basis,
            'deficit_fps': deficit_fps,
            'gap_basis': 'dts' if saw_dts else 'none',
            'gap_threshold': gap_threshold,
            'gap_count': gap_count,
            'gap_seconds': gap_seconds,
            'max_gap_seconds': max_gap,
            'backward_count': backward_count,
            'deficit_seconds': max(deficit, 0.0),
            'missing_seconds': max(gap_seconds, deficit, 0.0),
            'gaps': [(prev - low, cur - low)
                     for _step, prev, cur in sorted(largest, key=lambda g: g[1])],
        }
    except Exception as exc:
        log.warning('Timeline scan failed for %s: %s', filepath, exc)
        return {}


def assess_seek_damage(filepath: str, *, joined_segments: int = 1, segment_rates=None,
                       on_progress=None) -> tuple:
    """Decide whether a recording's video timeline is damaged enough to break seeking.

    segment_rates is [(content_seconds, fps), ...] for the segments that were joined, from
    the capture-time probe already stored on recording_segments. It is a fallback: the scan
    counts each packet at its own duration, and only a packet carrying none is counted at
    a rate, which is the segments' duration-weighted mean when their rates disagree and the
    file header's otherwise - see scan_video_timeline's expected_fps. Those probes read
    each segment's first seconds only, so they are the weaker of the two answers.

    The weight is each segment's own CONTENT length, never how long it took to arrive: the
    deficit counts frames against content time, so a stretch that arrived faster than real
    time must still weigh what it holds. Supplying wall clock instead reported an entire rate
    change as missing video on the recordings that had one (dev/changelog/962).

    Two facts come back in the metrics when the rates disagree, because the verdict changing
    on a fact the caller cannot see is the failure this replaced: capture_fps_values (the
    rates the segments opened at) and deficit_fps (what a packet without a duration was
    counted at - deficit_basis says whether any was).
    A mixed-rate file is a real reason to re-encode - CFR normalization is work worth doing -
    but that is a different finding from timeline damage and the caller must act on it under
    its own name, never by letting a false DAMAGED verdict carry it (dev/changelog/866).

    joined_segments is how many capture segments were concatenated to produce this file.
    Above 1 the gap count is BLIND to the joins: concatenator.py runs the concat demuxer
    with -fflags +genpts, which lays one continuous timestamp run across every join, so
    the DTS discontinuity that a lost stretch would have left has been erased before this
    scan ever opens the file. gap_count then counts only discontinuities that survived
    *inside* a segment - a floor, not a total - and on this app the loss lands at the
    joins, because the watchdog ends a segment when the feed stalls. Reporting that floor
    as "0 gaps" beside a large deficit is what this parameter exists to stop
    (dev/changelog/433). Default 1 = a single-segment recording, which concatenator.py
    renames rather than concatenating, so its scan is honest.

    Gaps are still counted and still feed missing_seconds either way: the verdict must not
    weaken just because one of its two inputs went partially blind.

    Returns (damaged, metrics, summary). metrics is scan_video_timeline()'s dict ({} if
    the scan failed - treated as not-damaged so a probe hiccup never forces a re-encode),
    with gap_basis rewritten to 'dts-post-concat' when the joins are invisible; summary is
    a human-readable one-liner for logs/RecordingEvents.
    """
    effective_fps, distinct_rates = effective_capture_fps(segment_rates)
    # One rate (or none measured) leaves the scan on the header rate: the override exists to
    # correct a rate CHANGE, and applying it otherwise would silently re-target the deficit
    # of every recording in the app on the strength of a mid-capture probe.
    mixed = len(distinct_rates) > 1
    metrics = scan_video_timeline(filepath, expected_fps=effective_fps if mixed else None,
                                  on_progress=on_progress)
    if not metrics:
        return False, {}, 'timeline scan failed - assuming undamaged'
    if mixed:
        metrics['capture_fps_values'] = [round(r, 3) for r in distinct_rates]
    if joined_segments > 1 and metrics['gap_basis'] == 'dts':
        metrics['gap_basis'] = 'dts-post-concat'
    missing = metrics['missing_seconds']
    span = metrics['span_seconds']
    threshold = max(DAMAGE_MIN_MISSING_SECONDS, span * DAMAGE_MIN_MISSING_FRACTION)
    damaged = missing > threshold
    pct = (missing / span * 100) if span > 0 else 0.0
    plural = '' if metrics['gap_count'] == 1 else 's'
    summary = (f'{missing:.1f}s of video missing from a {span:.0f}s timeline ({pct:.1f}%) - '
               f'{metrics["gap_count"]} decode-timeline gap{plural} '
               f'>{metrics["gap_threshold"]}s '
               f'(largest {metrics["max_gap_seconds"]:.2f}s), frame deficit '
               f'{metrics["deficit_seconds"]:.1f}s, threshold {threshold:.1f}s -> '
               f'{"DAMAGED" if damaged else "OK"}')
    # The blended rate only did anything when some packet lacked a duration of its own;
    # otherwise the deficit never divided by it, and naming it would explain the number
    # with a figure that played no part in it.
    if mixed and metrics['deficit_basis'] == 'frame_durations':
        rates = ', '.join(f'{r:g}' for r in metrics['capture_fps_values'])
        summary += (f'. Capture frame rate changed mid-recording (segments opened at '
                    f'{rates} fps); every frame is counted at its own duration, so the '
                    f'change is not counted as missing video')
    elif mixed:
        # Stated before the concat caveat because it changes what the deficit number MEANS,
        # not merely how completely it was measured. Without it the corrected figure is as
        # unexplainable as the wrong one was - a reader comparing it against the header rate
        # cannot reproduce it (CLAUDE.md principle 1).
        rates = ', '.join(f'{r:g}' for r in metrics['capture_fps_values'])
        summary += (f'. Capture frame rate changed mid-recording ({rates} fps), so the frame '
                    f'deficit is measured against the duration-weighted rate '
                    f'{metrics["deficit_fps"]:.2f} fps rather than the {metrics["fps"]:g} fps '
                    f'in the file header - dividing by the header rate alone would report the '
                    f'rate change itself as missing video')
    if metrics['gap_basis'] == 'dts-post-concat':
        # Appended after the verdict rather than folded into the gap clause: on a clean
        # multi-segment recording this sentence IS the finding, and it has to be readable
        # on its own. Names "Capture gaps" on purpose - that stat sits on the same
        # recording detail page and is the one measurement that CAN see the time between
        # segments, because it reads the segment clocks rather than the joined file.
        # Saying so is what stops the two from reading as a contradiction. It used to
        # name "Content missing", which could not answer the question it was handed: that
        # stat netted the gaps against a buffered feed's replay and reported zero
        # (dev/changelog/942).
        summary += (f'. Gaps are not measurable across {joined_segments} concatenated '
                    f'segments: concat re-timestamps every join, so that count is only '
                    f'what survived inside a segment, and time lost between segments is '
                    f'not part of this timeline at all - that time is reported as '
                    f'Capture gaps')
    return damaged, metrics, summary
