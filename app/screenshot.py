"""
Shared JPEG frame-grab via ffmpeg - keyframe-only decode, HDR-aware scale/tonemap,
seek point configurable.

Used by channel_tester.py (grabs a frame ~5s into a short test clip) and by the
live-thumbnail route in routes/recordings.py (grabs a frame near the end of a
still-growing live recording segment via -sseof).
"""
import logging
import os
import subprocess
from typing import Optional

log = logging.getLogger(__name__)

DEFAULT_SEEK_SECONDS = 5.0

# Decode only keyframes, so the frame handed back was reconstructed from a complete
# reference rather than from whatever the decoder happened to hold at the seek point.
# An input seek into an MPEG-TS lands at a byte offset, not a keyframe - the container
# carries no index - so decoding starts mid-GOP. ffmpeg's h264 decoder suppresses output
# until it reaches a recovery point and hides this; its hevc decoder does not, and emits
# a flat gray canvas with inter-prediction residuals painted on it. That frame then reads
# as "solid color" to the tester's uniformity check, so every HEVC channel carried an
# unearned blank-screenshot warning and a depressed health score. Measured on this box:
# 64x64 gray std_dev 1.24 (720p) and 4.99 (3840x2160 10-bit) without this flag, 63.5 with
# it, and the grab is faster because far fewer frames are decoded (dev/changelog/894).
KEYFRAME_ONLY_ARGS = ['-skip_frame', 'nokey']

# How much further back the widened retry reaches when seeking from the end of a file.
WIDEN_SSEOF_SECONDS = 9.0


def seek_args_for_clip(duration: Optional[float]) -> list:
    """Input-seek args for a frame grab from a finished clip of known length.

    5s in, except on a clip too short to hold that - then the midpoint, because a seek
    at or past the final moment of a file returns no frame at all and the caller reads
    that as "screenshot capture failed". Unchanged for any clip of 10s or more.
    duration None/0 (never probed) keeps the 5s default.
    """
    if not duration or duration <= 0:
        return ['-ss', str(int(DEFAULT_SEEK_SECONDS))]
    return ['-ss', f'{min(DEFAULT_SEEK_SECONDS, duration / 2):.2f}']


def widened_seek_args(seek_args: list) -> Optional[list]:
    """A wider seek covering the same file, or None if the given one cannot be widened.

    Keyframe-only decoding scans forward from the seek point, so it finds nothing when
    every keyframe in the file sits *before* it - a real shape for a capture that joined
    its stream mid-GOP and holds one keyframe near the top. Reaching further back is what
    recovers a real frame there: measured on a clip whose only keyframe preceded the seek,
    the widened grab scores std_dev 63.6 where the un-widened one returns no frame at all
    and the pre-2026-09-09 behavior returned a gray one at 3.84 (dev/changelog/894).

    Only the two seek shapes this module documents are widened; anything else declines
    rather than guessing at semantics it does not own.
    """
    if len(seek_args) != 2:
        return None
    flag, value = seek_args
    try:
        seconds = float(value)
    except (TypeError, ValueError):
        return None
    if flag == '-ss':
        # From the top of the file - every keyframe it has is then in scope.
        return ['-ss', '0'] if seconds > 0 else None
    if flag == '-sseof':
        # A longer tail. Bounded rather than proportional, so a thumbnail that falls back
        # here is a known few seconds staler instead of arbitrarily older than requested.
        return ['-sseof', f'{seconds - WIDEN_SSEOF_SECONDS:.2f}']
    return None


def _capture_attempts(seek_args: list) -> list:
    """(seek_args, keyframe_only) pairs to try, in order, until one yields a frame.

    The last pair is deliberately the pre-2026-09-09 command: a file with no decodable
    keyframe anywhere still produces whatever it produced before, so this can only ever
    add screenshots, never remove one. A frame that reaches the caller that way is a
    genuinely damaged one and the tester's uniformity check still warns about it.
    """
    attempts = [(seek_args, True)]
    wider = widened_seek_args(seek_args)
    if wider is not None:
        attempts.append((wider, True))
    attempts.append((seek_args, False))
    return attempts


def capture_screenshot(filepath: str, output_path: str, ffmpeg_path: str,
                        probe: Optional[dict] = None,
                        seek_args: Optional[list] = None,
                        timeout: int = 30) -> bool:
    """Extract a single JPEG frame. Returns True on success.

    Decodes keyframes only, so the frame is reconstructed from a complete reference
    instead of from a mid-GOP guess - see KEYFRAME_ONLY_ARGS for why that is not
    optional. Applies HDR→SDR tonemapping for PQ/HLG content and always caps output at
    1920x1080 to avoid giant 4K JPEGs. Falls back to a simple scale if tonemapping
    fails (e.g. unsupported codec path).

    seek_args: ffmpeg input-seek options placed before -i. Defaults to ['-ss', '5'].
               Pass seek_args_for_clip(duration) for a finished clip whose length is
               known, or ['-sseof', '-3'] to grab from ~3s before EOF of a growing file.
               Each is tried in turn against _capture_attempts()'s ladder.
    """
    seek_args = seek_args if seek_args is not None else ['-ss', '5']
    try:
        os.makedirs(os.path.dirname(output_path), exist_ok=True)

        # Determine whether HDR tonemapping is needed.
        ct = (probe or {}).get('color_transfer') or ''
        w = (probe or {}).get('vid_width') or 0
        is_hlg = ct == 'arib-std-b67'
        is_hdr = ct == 'smpte2084' or is_hlg or w >= 3840

        def _run(vf: str, seek: list, keyframe_only: bool) -> bool:
            cmd = [ffmpeg_path, *seek]
            if keyframe_only:
                cmd += KEYFRAME_ONLY_ARGS
            cmd += ['-i', filepath, '-vf', vf, '-vframes', '1', '-q:v', '3',
                    '-y', output_path]
            r = subprocess.run(cmd, capture_output=True, timeout=timeout)
            return (r.returncode == 0
                    and os.path.exists(output_path)
                    and os.path.getsize(output_path) > 0)

        if is_hdr:
            # Full HDR→SDR pipeline via zscale + hable tonemap.
            # The first zscale step converts from PQ (or HLG) to linear light.
            tin_arg = 'tin=arib-std-b67:' if is_hlg else ''
            tonemap_vf = (
                f'scale=1920:1080,'
                f'zscale={tin_arg}t=linear:npl=100,'
                f'format=gbrpf32le,'
                f'zscale=p=bt709,'
                f'tonemap=hable,'
                f'zscale=t=bt709:m=bt709:r=tv,'
                f'format=yuv420p'
            )
            plain_vf = 'scale=1920:1080,format=yuv420p'
        else:
            tonemap_vf = None
            # SDR: just cap at 1080p while preserving aspect ratio.
            plain_vf = 'scale=min(iw\\,1920):min(ih\\,1080):force_original_aspect_ratio=decrease'

        # A tonemap chain that failed once is not retried on the later seeks. Whether it
        # failed for want of a filter or for want of a frame is not distinguishable from
        # the exit code, and a build missing zscale would otherwise pay a doomed run on
        # every rung. The cost of guessing wrong is one untonemapped screenshot, which is
        # already what the plain-scale fallback has always produced on such a build.
        tonemap_ruled_out = False
        for seek, keyframe_only in _capture_attempts(seek_args):
            if tonemap_vf and not tonemap_ruled_out:
                if _run(tonemap_vf, seek, keyframe_only):
                    return True
                tonemap_ruled_out = True
                log.warning('HDR tonemapping failed for %s, falling back to plain scale',
                            filepath)
            if _run(plain_vf, seek, keyframe_only):
                return True
        return False

    except Exception as exc:
        log.debug('Screenshot capture failed for %s: %s', filepath, exc)
        return False
