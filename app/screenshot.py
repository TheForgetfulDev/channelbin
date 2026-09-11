"""
Shared JPEG frame-grab via ffmpeg - keyframe-only decode, transfer-driven scale/tonemap,
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

# A PQ or HLG transfer function is the only thing that proves a source is HDR. Frame width
# is not, and the 3840-wide trigger this replaced is what made the whole HDR path wrong: it
# pulled 4K *SDR* into the tonemap chain, where a bar pattern whose mean luma is 102 came
# back at 66 - every 4K SDR screenshot darkened by a third, and pushed that much closer to
# the tester's uniformity check. A source that declares no transfer at all is left alone for
# the same reason, in the other direction: unknown is not a proven HDR (dev/changelog/915).
HDR_TRANSFERS = ('smpte2084', 'arib-std-b67')

# What ffprobe prints for a colorspace field a stream never stated. It emits the key either
# way, so these read as absent rather than as a value.
UNSPECIFIED_COLOR = ('', 'unknown', 'unspecified', 'reserved')

# zscale will not construct a conversion path unless transfer, matrix, primaries and range
# are ALL known, and fails the entire filter graph with "code 3074: no path between
# colorspaces" when any one is missing - which is the shape of a real broadcast that tags
# its transfer and nothing else. These are what a PQ or HLG transfer implies: both are
# defined on BT.2020 primaries, and neither is carried full-range in practice.
#
# zscale's own tin=/min=/pin=/rin= options are NOT a substitute for stating this ahead of
# the chain. Measured on 6.1.1 here and on 7.1.5 in the container, a chain carrying
# tin=smpte2084:min=bt2020nc:pin=bt2020:rin=tv returns 3074 exactly as it did without them;
# the identical values via setparams produce a byte-identical JPEG to the same source
# tagged properly at the encoder.
HDR_DEFAULT_PRIMARIES = 'bt2020'
HDR_DEFAULT_MATRIX = 'bt2020nc'
HDR_DEFAULT_RANGE = 'tv'


def _stated(value: Optional[str]) -> Optional[str]:
    """The probed colorspace value, or None if the stream stated nothing."""
    value = (value or '').strip().lower()
    return None if value in UNSPECIFIED_COLOR else value


def hdr_input_params(probe: Optional[dict]) -> Optional[str]:
    """A `setparams` filter declaring the input's colorspace, or None if it is not HDR.

    Returns None for everything but a PQ/HLG transfer, so SDR content of any size never
    reaches the tonemap chain. For HDR, the filter states all four fields: whatever the
    probe actually read, and the BT.2020 defaults above only for the ones it did not.
    Declaring them is what makes the chain work at all - see HDR_DEFAULT_PRIMARIES.
    """
    transfer = _stated((probe or {}).get('color_transfer'))
    if transfer not in HDR_TRANSFERS:
        return None
    primaries = _stated((probe or {}).get('color_primaries')) or HDR_DEFAULT_PRIMARIES
    matrix = _stated((probe or {}).get('color_space')) or HDR_DEFAULT_MATRIX
    rng = _stated((probe or {}).get('color_range')) or HDR_DEFAULT_RANGE
    return (f'setparams=color_trc={transfer}:color_primaries={primaries}'
            f':colorspace={matrix}:range={rng}')


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


def _log_untonemapped(filepath: str, probe: Optional[dict]) -> None:
    """Say why a source that looks like it could be HDR was not tonemapped.

    Only for content whose shape invites the question - UHD width or a 10-bit depth -
    and only when the stream stated no transfer at all, which is the one case where the
    honest answer is "it did not say". A source that states an SDR transfer answered the
    question and needs no line.
    """
    probe = probe or {}
    if _stated(probe.get('color_transfer')) is not None:
        return
    if (probe.get('vid_width') or 0) < 3840 and (probe.get('bit_depth') or 0) < 10:
        return
    log.info('%s: %sx%s %s-bit source declares no color transfer, so no HDR tonemapping '
             'was attempted - the frame is scaled as-is',
             filepath, probe.get('vid_width'), probe.get('vid_height'),
             probe.get('bit_depth'))


def capture_screenshot(filepath: str, output_path: str, ffmpeg_path: str,
                        probe: Optional[dict] = None,
                        seek_args: Optional[list] = None,
                        timeout: int = 30) -> bool:
    """Extract a single JPEG frame. Returns True on success.

    Decodes keyframes only, so the frame is reconstructed from a complete reference
    instead of from a mid-GOP guess - see KEYFRAME_ONLY_ARGS for why that is not
    optional. Applies HDR→SDR tonemapping to content whose probe reports a PQ or HLG
    transfer - and to nothing else, whatever its size - and always caps output at
    1920x1080 to avoid giant 4K JPEGs. Falls back to an untonemapped scale if the chain
    fails, saying so.

    probe: the parse_ffprobe() dict for this file, or None. Without one there is no
           transfer to read, so no tonemapping is attempted - which is why the two
           live-thumbnail callers, neither of which probes, have never taken that path.

    seek_args: ffmpeg input-seek options placed before -i. Defaults to ['-ss', '5'].
               Pass seek_args_for_clip(duration) for a finished clip whose length is
               known, or ['-sseof', '-3'] to grab from ~3s before EOF of a growing file.
               Each is tried in turn against _capture_attempts()'s ladder.
    """
    seek_args = seek_args if seek_args is not None else ['-ss', '5']
    try:
        os.makedirs(os.path.dirname(output_path), exist_ok=True)

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

        input_params = hdr_input_params(probe)
        if input_params:
            # Full HDR→SDR pipeline via zscale + hable tonemap, in front of it the
            # declaration of what is coming in. The first zscale step converts from PQ
            # (or HLG) to linear light.
            tonemap_vf = (
                f'{input_params},'
                f'scale=1920:1080,'
                f'zscale=t=linear:npl=100,'
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
            _log_untonemapped(filepath, probe)

        # Every rung gets its own tonemap attempt. A failure on an earlier one is not
        # evidence the chain is unusable: the common way to fail is to find no frame at
        # that seek at all, which is exactly what the later, wider rungs exist to fix, and
        # latching the chain off there delivered an untonemapped grab of HDR content that
        # would have tonemapped fine one rung down. The latch that used to sit here
        # reasoned from a build missing zscale, which no build checked actually is
        # (dev/changelog/915). It costs a doomed run per rung on a clip that yields no
        # frame anywhere - measured at 0.31s, the same as the plain run beside it, because
        # both abort at the same point.
        for seek, keyframe_only in _capture_attempts(seek_args):
            tonemap_failed_here = False
            if tonemap_vf:
                if _run(tonemap_vf, seek, keyframe_only):
                    return True
                tonemap_failed_here = True
            if _run(plain_vf, seek, keyframe_only):
                if tonemap_failed_here:
                    # Only sayable here: the plain run just proved a frame was available
                    # at this seek, so the chain itself is what failed. Logged before the
                    # frame count was known, the same sentence was routinely false.
                    log.warning('HDR tonemapping failed for %s (a frame was available at '
                                'this seek), falling back to an untonemapped scale',
                                filepath)
                return True
        return False

    except Exception as exc:
        log.debug('Screenshot capture failed for %s: %s', filepath, exc)
        return False
