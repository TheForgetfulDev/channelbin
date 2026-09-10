"""Shared ffprobe utility used by channel tester and post-processor."""
import json
import logging
import os
import queue
import re
import shutil
import subprocess
import threading
import time

log = logging.getLogger(__name__)


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


def run_probe_until_stalled(cmd, stall_timeout: int, fallback_timeout: int,
                            poll_interval: float = 1.0):
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
                  stall_timeout: int = 60) -> dict:
    """Return dict with video/audio stream metadata and format info.

    Keys: resolution, fps, duration, bitrate_bps, frame_count, vid_width,
          vid_height, color_transfer, color_primaries, video_codec, pix_fmt, bit_depth,
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
    """
    ffprobe_path = shutil.which('ffprobe') or 'ffprobe'
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
        if count_packets:
            returncode, stdout = run_probe_until_stalled(
                cmd, stall_timeout=stall_timeout, fallback_timeout=timeout)
        else:
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
            returncode, stdout = result.returncode, result.stdout
        if returncode != 0:
            return {}
        data = json.loads(stdout)

        resolution = None
        fps = None
        vid_width = None
        vid_height = None
        color_transfer = None
        color_primaries = None
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
                                val = round(int(num) / int(den), 2)
                                if val > 0:
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
    damaged captures, which is exactly when callers need the true rate."""
    ffprobe_path = shutil.which('ffprobe') or 'ffprobe'
    try:
        cmd = [ffprobe_path, '-v', 'quiet', '-print_format', 'json',
               '-select_streams', 'v:0', '-show_streams', filepath]
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        if result.returncode != 0:
            return None
        streams = json.loads(result.stdout).get('streams', [])
        raw = streams[0].get('r_frame_rate', '') if streams else ''
        if '/' in raw:
            num, den = raw.split('/')
            if int(den) > 0 and int(num) > 0:
                return raw
    except (subprocess.SubprocessError, OSError, ValueError, json.JSONDecodeError) as exc:
        log.debug('nominal_video_rate failed for %s: %s', filepath, exc)
    return None


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


def scan_video_timeline(filepath: str, gap_threshold: float = 0.25, timeout: int = 600,
                        expected_fps: float | None = None) -> dict:
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
        deficit_fps      the rate deficit_seconds was actually divided by - `fps` unless
                         the caller passed expected_fps. The two differ only on a file
                         whose capture rate changed partway through, and keeping both is
                         what makes the deficit number explainable after the fact
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
        deficit_seconds  span - packet_count/deficit_fps - catches micro-gaps below the
                         threshold
        missing_seconds  max(gap_seconds, deficit_seconds) - the headline damage number

    expected_fps overrides the header rate for the deficit division ONLY. The header rate
    is whatever the file's first stretch was captured at, so on a concatenation whose
    segments ran at different rates it turns every frame captured at the lower rate into a
    missing frame: recording 2 measured 1,200s missing from a 12,949s timeline against 0.41s
    of real damage (dev/changelog/866). This function measures a file and cannot know it was
    concatenated, let alone from what - so the corrected rate is a parameter, and
    assess_seek_damage() is what derives it from the segment rows.

    Returns {} on any error (missing file, no video stream, ffprobe failure).
    """
    if not filepath or not os.path.exists(filepath):
        return {}
    ffprobe_path = shutil.which('ffprobe') or 'ffprobe'
    try:
        fps = _nominal_fps(filepath)

        cmd = [ffprobe_path, '-v', 'error', '-select_streams', 'v:0',
               '-show_entries', 'packet=pts_time,dts_time', '-of', 'csv=p=0', filepath]
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
        low = None
        highwater = None
        count = 0
        prev_dts = None
        dts_low = None
        dts_high = None
        saw_dts = False
        gap_count = 0
        gap_seconds = 0.0
        max_gap = 0.0
        backward_count = 0

        # timeout bounds inactivity, not total wall time: a probe reading a large file
        # legitimately runs for minutes, so the deadline must reset on every line rather
        # than cap the read loop outright (dev/changelog/797's run_probe_until_stalled is
        # the same shape, over bytes read; a CSV line here is the cheaper, more direct
        # progress signal). A reader thread is required to make that inactivity actually
        # observable - reading proc.stdout directly in this loop, as before, blocks the
        # whole thread on a stalled ffprobe with no way to notice no line has arrived.
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

                fields = line.strip().rstrip(',').split(',')
                pts = _packet_time(fields[0] if fields else '')
                dts = _packet_time(fields[1] if len(fields) > 1 else '')
                # A packet with neither timestamp tells us nothing about the timeline.
                if pts is None and dts is None:
                    continue

                # Presentation timeline: span and packet count only, no gap accounting.
                # min/max rather than first/last because with B-frames the first packet in
                # storage order is not the earliest-presented one.
                stamp = pts if pts is not None else dts
                count += 1
                if low is None or stamp < low:
                    low = stamp
                if highwater is None or stamp > highwater:
                    highwater = stamp

                # Decode timeline: the only basis gap accounting runs on. Packets missing a
                # DTS are skipped rather than substituted with PTS - mixing the two timelines
                # would manufacture a gap at every switch between them.
                if dts is not None:
                    saw_dts = True
                    if dts_low is None or dts < dts_low:
                        dts_low = dts
                    if dts_high is None or dts > dts_high:
                        dts_high = dts
                    if prev_dts is not None:
                        step = dts - prev_dts
                        if step < 0:
                            backward_count += 1
                        elif step > gap_threshold:
                            gap_count += 1
                            gap_seconds += step
                            if step > max_gap:
                                max_gap = step
                    prev_dts = dts
            proc.wait(timeout=timeout)
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.wait()
            reader.join(timeout=5)
            proc.stdout.close()  # scanner runs per recording and per channel test; don't leak fds

        if proc.returncode != 0 or count < 2 or low is None:
            return {}

        span = highwater - low
        # Withheld on a file whose decode timeline restarts: max-min then measures the
        # widest timestamp in the file rather than the capture window, and None makes that
        # misuse impossible instead of merely documented.
        dts_span = (dts_high - dts_low) if (saw_dts and not backward_count) else None
        deficit_fps = expected_fps if expected_fps and expected_fps > 0 else fps
        deficit = (span - count / deficit_fps) if deficit_fps else 0.0
        return {
            'span_seconds': span,
            'dts_span_seconds': dts_span,
            'packet_count': count,
            'fps': fps,
            'deficit_fps': deficit_fps,
            'gap_basis': 'dts' if saw_dts else 'none',
            'gap_threshold': gap_threshold,
            'gap_count': gap_count,
            'gap_seconds': gap_seconds,
            'max_gap_seconds': max_gap,
            'backward_count': backward_count,
            'deficit_seconds': max(deficit, 0.0),
            'missing_seconds': max(gap_seconds, deficit, 0.0),
        }
    except Exception as exc:
        log.warning('Timeline scan failed for %s: %s', filepath, exc)
        return {}


def assess_seek_damage(filepath: str, *, joined_segments: int = 1, segment_rates=None) -> tuple:
    """Decide whether a recording's video timeline is damaged enough to break seeking.

    segment_rates is [(duration_seconds, fps), ...] for the segments that were joined, from
    the capture-time probe already stored on recording_segments. It is used ONLY when those
    rates disagree, in which case the frame deficit is measured against their duration-
    weighted mean instead of the file header's single nominal rate - see
    scan_video_timeline's expected_fps. A recording that held one rate throughout is
    measured exactly as before, whether or not its rates were supplied.

    Two facts come back in the metrics when the rates disagree, because the verdict changing
    on a fact the caller cannot see is the failure this replaced: capture_fps_values (the
    rates the segments actually ran at) and deficit_fps (what the deficit was divided by).
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
    metrics = scan_video_timeline(filepath, expected_fps=effective_fps if mixed else None)
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
    if mixed:
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
        # on its own. Names "Content missing" on purpose - that stat sits on the same
        # recording detail page (dev/changelog/432) and reports a much larger number,
        # because the concatenated span excludes the time between segments entirely.
        # Saying so is what stops the two from reading as a contradiction.
        summary += (f'. Gaps are not measurable across {joined_segments} concatenated '
                    f'segments: concat re-timestamps every join, so that count is only '
                    f'what survived inside a segment, and time lost between segments is '
                    f'not part of this timeline at all - that loss is reported as '
                    f'Content missing')
    return damaged, metrics, summary
