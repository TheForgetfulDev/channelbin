"""
Shared JPEG frame-grab via ffmpeg - HDR-aware scale/tonemap, seek point configurable.

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


def capture_screenshot(filepath: str, output_path: str, ffmpeg_path: str,
                        probe: Optional[dict] = None,
                        seek_args: Optional[list] = None,
                        timeout: int = 30) -> bool:
    """Extract a single JPEG frame. Returns True on success.

    Applies HDR→SDR tonemapping for PQ/HLG content and always caps output at
    1920x1080 to avoid giant 4K JPEGs. Falls back to a simple scale if tonemapping
    fails (e.g. unsupported codec path).

    seek_args: ffmpeg input-seek options placed before -i. Defaults to ['-ss', '5'].
               Pass seek_args_for_clip(duration) for a finished clip whose length is
               known, or ['-sseof', '-3'] to grab from ~3s before EOF of a growing file.
    """
    seek_args = seek_args if seek_args is not None else ['-ss', '5']
    try:
        os.makedirs(os.path.dirname(output_path), exist_ok=True)

        # Determine whether HDR tonemapping is needed.
        ct = (probe or {}).get('color_transfer') or ''
        w = (probe or {}).get('vid_width') or 0
        is_hlg = ct == 'arib-std-b67'
        is_hdr = ct == 'smpte2084' or is_hlg or w >= 3840

        def _run(vf: str) -> bool:
            cmd = [ffmpeg_path, *seek_args, '-i', filepath,
                   '-vf', vf, '-vframes', '1', '-q:v', '3', '-y', output_path]
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
            if _run(tonemap_vf):
                return True
            # Tonemap failed - fall back to plain scale so we still get a screenshot.
            log.warning('HDR tonemapping failed for %s, falling back to plain scale', filepath)
            return _run('scale=1920:1080,format=yuv420p')
        else:
            # SDR: just cap at 1080p while preserving aspect ratio.
            return _run('scale=min(iw\\,1920):min(ih\\,1080):force_original_aspect_ratio=decrease')

    except Exception as exc:
        log.debug('Screenshot capture failed for %s: %s', filepath, exc)
        return False
