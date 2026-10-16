"""What a re-encoded recording is normalized TO, decided from what it actually contains.

A recording can change format part-way through - a failover from a 720p30 member to a
1080p60 one, a provider switching feeds, a stereo pre-roll before a 5.1 program. The stream
copy leaves that file mixed, and the re-encode used to squeeze the whole of it to whatever
the FIRST stretch was. Every re-encode now brings a recording to its OWN biggest format: the
largest picture, the highest frame rate and the widest audio layout found anywhere in it,
optionally lowered (never raised) by a max-resolution setting. Normalization is per
recording and never across recordings: a 720p-only recording stays 720p (dev/changelog/1184,
measurements dev/changelog/1183).

Pure. Everything here is a function of what the caller measured and the settings it read,
so the whole decision table is unit-testable without ffmpeg, a database or a GPU. The
measuring is app/probe.py's (survey_formats, measure_loudness); the acting is
app/postprocessor.py's.
"""
import math
from dataclasses import dataclass, field

# recording.post_process.video_codec. Compared against these names, never a literal;
# app/routes/settings.py whitelists a save against the same pair.
VIDEO_CODEC_H264 = 'h264'
VIDEO_CODEC_HEVC = 'hevc'
VIDEO_CODECS = (VIDEO_CODEC_H264, VIDEO_CODEC_HEVC)

# recording.post_process.audio_channels.
AUDIO_CHANNELS_SOURCE = 'source'
AUDIO_CHANNELS_STEREO = 'stereo'
AUDIO_CHANNELS_CHOICES = (AUDIO_CHANNELS_SOURCE, AUDIO_CHANNELS_STEREO)

# recording.post_process.max_height. 0 is "no cap"; the select offers exactly these.
MAX_HEIGHT_CHOICES = (0, 2160, 1080, 720)

# Loudness leveling (recording.post_process.loudness_normalize). -24 LUFS is the ATSC A/85
# broadcast target; measured channels ran -19.8 to -24.5 (dev/changelog/1183), so leveling
# mostly turns loud channels down. The boost is capped because a recording far below the
# target is near-silent or deliberately quiet, and raising it further lifts the noise floor
# toward program level: 12 dB lets a channel 12 LU quiet reach the target and stops there.
# The limiter ceiling is -2 dBTP as a linear amplitude, which keeps a boosted peak from
# clipping the AAC encode.
LOUDNESS_TARGET_LUFS = -24.0
LOUDNESS_MAX_BOOST_DB = 12.0
LIMITER_CEILING = 0.79

# Two capture rates closer than this are one rate spelled two ways (59.94 vs 60.0 from two
# probes of one feed). Same tolerance probe.effective_capture_fps collapses rates with.
_RATE_SAME = 0.02

# The broadcast rates ffprobe reports as rationals; a float probe_fps is mapped back to its
# exact rational so -r never drifts by a rounding error over hours.
_KNOWN_RATES = (
    (24000 / 1001, '24000/1001'), (30000 / 1001, '30000/1001'),
    (60000 / 1001, '60000/1001'), (120000 / 1001, '120000/1001'),
)

# Reasons a recording is not normalized - each one its own trigger for the `damaged` mode,
# named on the recording under FORMAT_NORMALIZE_NEEDED. A mixed frame rate is the fourth
# reason and keeps its own older event, MIXED_FRAME_RATE_DETECTED (dev/changelog/866); a
# mixed audio layout is deliberately NOT one, because mp4 audio is re-encoded on every
# conversion and -ac fixes it without touching the video.
REASON_MIXED_RESOLUTION = 'mixed_resolution'
REASON_ABOVE_MAX_RESOLUTION = 'above_max_resolution'


def even(x) -> int:
    """Nearest even integer - 4:2:0 video cannot have an odd dimension."""
    return max(2, int(round(x / 2.0)) * 2)


def parse_resolution(raw):
    """'1920x1080' -> (1920, 1080); None for anything else."""
    try:
        w, h = (int(v) for v in str(raw or '').lower().split('x'))
    except ValueError:
        return None
    return (w, h) if w > 0 and h > 0 else None


def display_size(width, height, sar=1.0):
    """The picture as it is meant to be seen: height kept, width stretched by the sample
    aspect ratio. A 1440x1080 anamorphic frame with SAR 4:3 displays at 1920x1080."""
    return even(width * (sar or 1.0)), even(height)


def rate_rational(fps):
    """A probed float rate as the rational -r should be given, e.g. 59.94 -> '60000/1001'."""
    if not fps or fps <= 0:
        return None
    for value, rational in _KNOWN_RATES:
        if abs(fps - value) / value < 0.001:
            return rational
    if abs(fps - round(fps)) < 0.001:
        return str(int(round(fps)))
    return f'{int(round(fps * 1000))}/1000'


def rate_value(rational):
    """'60000/1001' -> 59.94..., '50' -> 50.0, None -> None."""
    if not rational:
        return None
    try:
        if '/' in str(rational):
            num, den = (int(v) for v in str(rational).split('/'))
            return num / den if num > 0 and den > 0 else None
        value = float(rational)
        return value if value > 0 else None
    except ValueError:
        return None


def format_label(width, height, rate=None, interlaced=False) -> str:
    """'1080p59.94', '720p30', '1080i29.97'. Unlike channel_groups.format_label (a rounded
    grouping key), this keeps the exact nominal rate, because it describes one file."""
    if not width or not height:
        return 'unknown'
    label = f'{height}{"i" if interlaced else "p"}'
    if rate:
        label += f'{math.floor(rate * 100 + 0.5) / 100:g}'
    return label


def channels_label(n) -> str:
    return {1: 'mono', 2: 'stereo', 6: '5.1', 8: '7.1'}.get(n, f'{n} channels') if n else 'unknown'


def _distinct_rates(rates):
    out = []
    for r in sorted(r for r in rates if r and r > 0):
        if not out or abs(r - out[-1]) / out[-1] > _RATE_SAME:
            out.append(r)
    return out


@dataclass(frozen=True)
class SourceFormats:
    """Everything a recording contains, merged from the segment rows (capture-time probes,
    one per segment) and the whole-file survey (every keyframe). Unknown is never treated as
    different: a field nobody could measure simply contributes nothing."""
    sizes: tuple = ()          # distinct (display_w, display_h), largest last
    stored_sizes: tuple = ()   # distinct (w, h, sar) as coded
    rates: tuple = ()          # distinct capture rates (frames/s), ascending
    header_rate: str = None    # the joined file's r_frame_rate rational
    channels: tuple = ()       # distinct audio channel counts, ascending
    interlaced: bool = False
    hdr_transfer: str = None   # 'smpte2084' / 'arib-std-b67', or None
    surveyed: bool = False     # the whole-file survey ran and answered

    @property
    def mixed_resolution(self):
        return len(self.sizes) > 1

    @property
    def mixed_rate(self):
        return len(self.rates) > 1

    @property
    def mixed_audio_layout(self):
        return len(self.channels) > 1

    @property
    def non_square(self):
        return any(abs((sar or 1.0) - 1.0) > 0.001 for _w, _h, sar in self.stored_sizes)

    @property
    def single_format(self):
        """One picture size and one rate, as far as anything measured - what GPU decode
        requires (the hardware decoder dies on a mid-file size change, dev/changelog/1183)."""
        return self.surveyed and len(self.sizes) == 1 and len(self.rates) <= 1


def gather_source_formats(segments, survey, header, hdr_transfers):
    """Merge what was measured into one SourceFormats.

    segments: iterable of (probe_resolution, probe_fps, probe_audio_channels,
        probe_interlaced) for the segments the concat joined.
    survey: probe.survey_formats()'s dict, or {} / None when it did not run or failed.
    header: probe.parse_ffprobe()'s header-only dict of the joined file, or {}.
    hdr_transfers: probe.HDR_TRANSFERS, passed in so this module stays import-free.
    """
    survey = survey or {}
    header = header or {}
    stored = set()
    rates = []
    channels = set()
    interlaced = bool(survey.get('interlaced'))
    for resolution, fps, audio_channels, seg_interlaced in segments or ():
        size = parse_resolution(resolution)
        if size:
            stored.add((size[0], size[1], 1.0))
        if fps:
            rates.append(float(fps))
        if audio_channels:
            channels.add(int(audio_channels))
        if seg_interlaced:
            interlaced = True
    for w, h, sar in survey.get('video_sizes') or ():
        # The survey knows the aspect ratio and the rows do not, so a surveyed size
        # replaces the row's square-pixel guess at the same coded size.
        stored.discard((int(w), int(h), 1.0))
        stored.add((int(w), int(h), float(sar or 1.0)))
    for n in survey.get('audio_channels') or ():
        channels.add(int(n))
    for r in survey.get('video_rates') or ():
        rates.append(float(r))
    if not stored:
        size = parse_resolution(header.get('resolution'))
        if size:
            stored.add((size[0], size[1], 1.0))
    if header.get('interlaced'):
        interlaced = True
    header_rate = header.get('r_frame_rate')
    if not rates and header.get('fps'):
        rates.append(float(header['fps']))
    if not channels and header.get('audio_channels'):
        channels.add(int(header['audio_channels']))

    transfer = header.get('color_transfer')
    hdr = transfer if transfer in hdr_transfers else None
    for t in survey.get('transfers') or ():
        if t in hdr_transfers:
            hdr = hdr or t

    sizes = sorted({display_size(w, h, sar) for w, h, sar in stored},
                   key=lambda s: (s[1], s[0]))
    return SourceFormats(
        sizes=tuple(sizes), stored_sizes=tuple(sorted(stored)),
        rates=tuple(_distinct_rates(rates)), header_rate=header_rate,
        channels=tuple(sorted(channels)), interlaced=interlaced, hdr_transfer=hdr,
        surveyed=bool(survey))


@dataclass(frozen=True)
class NormalizePlan:
    """What the re-encode produces. width/height None = keep the picture as it is."""
    width: int = None
    height: int = None
    scale: bool = False        # emit the fit-and-pad chain
    rate: str = None           # -r rational
    deinterlace: bool = False
    channels: int = None       # -ac; None = leave it to the encoder (nothing was measured)
    reasons: tuple = ()        # REASON_* this recording is not normalized
    hdr: bool = False
    normalize_video: bool = True   # False = today's command (software + HDR source)
    notes: tuple = field(default_factory=tuple)

    @property
    def rate_value(self):
        return rate_value(self.rate)

    def target_label(self):
        return format_label(self.width, self.height, self.rate_value)


def target_size(sizes, max_height=0):
    """The biggest display size in the recording, lowered (never raised) by max_height."""
    if not sizes:
        return None
    w, h = max(sizes, key=lambda s: (s[1], s[0]))
    if max_height and h > max_height:
        w, h = even(w * max_height / h), even(max_height)
    return w, h


def target_rate(src: SourceFormats):
    """The highest capture rate in the recording, as a rational. An interlaced source is
    output at its FIELD rate - deinterlacing a 1080i29.97 feed to 59.94p keeps every field's
    motion, which is what a frame-rate cap was rejected for losing (dev/changelog/1184).
    A rate at or below 30.5 is a frame rate of interlaced material; anything above it is
    already progressive at field rate and is not doubled."""
    rates = list(src.rates)
    header = rate_value(src.header_rate)
    if not rates and header:
        rates = [header]
    if not rates:
        return src.header_rate
    if src.interlaced:
        rates = [r * 2 if r <= 30.5 else r for r in rates]
    top = max(rates)
    # Prefer the header's own spelling when it IS the top rate - it is ffprobe's exact
    # rational rather than a float mapped back to one.
    if header and abs(header - top) / top <= _RATE_SAME and not (src.interlaced and header <= 30.5):
        return src.header_rate
    return rate_rational(top)


def plan_normalize(src: SourceFormats, *, max_height=0, audio_channels=AUDIO_CHANNELS_SOURCE,
                   hdr_ok=True) -> NormalizePlan:
    """The normalized format for one recording.

    hdr_ok is False on the software path: tone-mapping in software costs ~46 CPU-s per
    second of 4K (dev/changelog/1183), so an HDR source is not normalized there - its video
    command is the one it always was, and the caller says so on the recording.
    """
    if audio_channels == AUDIO_CHANNELS_STEREO:
        channels = 2
    else:
        channels = max(src.channels) if src.channels else None

    hdr = bool(src.hdr_transfer)
    if hdr and not hdr_ok:
        return NormalizePlan(channels=channels, hdr=True, normalize_video=False,
                             rate=src.header_rate,
                             notes=('hdr_software',))

    size = target_size(src.sizes, max_height)
    reasons = []
    if src.mixed_resolution:
        reasons.append(REASON_MIXED_RESOLUTION)
    biggest = target_size(src.sizes)
    if size and biggest and size != biggest:
        reasons.append(REASON_ABOVE_MAX_RESOLUTION)
    scale = bool(size) and (src.mixed_resolution or size != biggest or src.non_square)
    w, h = size if size else (None, None)
    return NormalizePlan(width=w, height=h, scale=scale, rate=target_rate(src),
                         deinterlace=src.interlaced, channels=channels,
                         reasons=tuple(reasons), hdr=hdr)


def audio_bitrate_kbps(per_pair_kbps, channels):
    """recording.post_process.audio_bitrate_kbps is a rate per stereo pair: 192 -> 576 for
    5.1. Treated as a total it gave a 5.1 source about 32 kb/s a channel, measurably duller
    than the AC-3 it came from (dev/changelog/1163). Unknown channels keep the plain rate."""
    pairs = max(1, math.ceil(channels / 2)) if channels else 1
    return int(per_pair_kbps) * pairs


def loudness_gain_db(measured_lufs, target=LOUDNESS_TARGET_LUFS,
                     max_boost=LOUDNESS_MAX_BOOST_DB):
    """The linear gain that brings a recording to the target, or None when there is
    nothing to level (unmeasured, or silent - ebur128's -70 floor). Cuts are never capped;
    a boost is, at max_boost."""
    if measured_lufs is None or measured_lufs <= -70.0:
        return None
    return round(min(target - measured_lufs, max_boost), 2)


def audio_filter(gain_db):
    """The leveling chain for a measured gain, or None. A plain gain plus a peak limiter -
    linear, not loudnorm's dynamic compression, at ~1/10 of its CPU (dev/changelog/1183).
    level=false stops alimiter re-normalizing its own output, which would undo the gain."""
    if gain_db is None:
        return None
    return f'volume={gain_db:+.2f}dB,alimiter=limit={LIMITER_CEILING}:level=false'


# ── Video filter chains ──────────────────────────────────────────────────────────────────

def fit_filter(width, height):
    """Scale every frame to fit width x height at its true aspect ratio, pad the rest, and
    mark the result square-pixel.

    Evaluated PER FRAME, and that is the whole point: under -reinit_filter:v 0 (which the GPU
    path needs) the graph is never rebuilt when the source changes size, and a plain
    `scale=iw*sar:ih` keeps its first frame's answer - measured on this box, a 1080p stretch
    after a 4:3 SD one came out squeezed into the SD's 1440-wide pillarbox. One scale whose
    expressions read the current frame, plus a pad that does too, gives an SD stretch its
    pillarbox and the HD stretch the full frame within one fixed output size
    (dev/changelog/1184)."""
    W, H = int(width), int(height)
    ratio = f'{W}/{H}'
    w_expr = f"if(gt(iw*sar/ih,{ratio}),{W},trunc({H}*iw*sar/ih/2)*2)"
    h_expr = f"if(gt(iw*sar/ih,{ratio}),trunc({W}*ih/(iw*sar)/2)*2,{H})"
    return (f"scale=w='{w_expr}':h='{h_expr}':eval=frame,"
            f"pad=w={W}:h={H}:x=(ow-iw)/2:y=(oh-ih)/2:eval=frame,setsar=1")


# Field rate: every field becomes a frame (see target_rate). deint=interlaced leaves a
# progressive frame in a mixed recording untouched.
DEINTERLACE_SOFTWARE = 'bwdif=mode=send_field:deint=interlaced'
DEINTERLACE_VAAPI = 'deinterlace_vaapi=rate=field'

# HDR10 to SDR on the GPU (dev/changelog/1183, measured on the i5).
TONEMAP_VAAPI = 'tonemap_vaapi=format=nv12:t=bt709:m=bt709:p=bt709'


def end_pad_filter(seconds):
    """Hold the last picture for `seconds` after the video ends, so a recording whose
    video stops before its audio plays through to the end instead of leaving the audio
    under no picture.

    -fps_mode cfr does not do this on its own. It repeats a frame only when a LATER frame
    arrives, so it fills a hole inside one run but never pads past the last frame
    (measured, dev/changelog/1199). The length is measured from the source before the
    command is built (postprocessor.video_end_pad), never open-ended: an unbounded pad
    with -shortest would also cut the video wherever the audio ended early."""
    return f'tpad=stop_mode=clone:stop_duration={seconds:.3f}'


def software_video_filter(plan: NormalizePlan, *, end_pad=0.0):
    """The libx264 path's -vf, or None for a recording that needs none - which keeps a
    clean recording's command exactly what it always was."""
    parts = []
    if plan.normalize_video:
        if plan.deinterlace:
            parts.append(DEINTERLACE_SOFTWARE)
        if plan.scale:
            parts.append(fit_filter(plan.width, plan.height))
    if end_pad:
        parts.append(end_pad_filter(end_pad))
    return ','.join(parts) or None


def gpu_upload_filter(plan: NormalizePlan, *, hevc: bool, end_pad=0.0):
    """The VAAPI path's -vf when the CPU decodes: everything that changes the picture runs
    in software at ONE fixed output size, then the frames are uploaded.

    The fixed size is not optional. The hardware frames context is created once at the
    first frame's size, and the only shape measured to survive a mid-file size change is
    `-reinit_filter:v 0` plus an explicit software scale before hwupload; every other shape
    died with "Error reinitializing filters!" (dev/changelog/1183). So the fit chain runs
    on every GPU re-encode that knows its target, single-format ones included - on a clean
    file it is a same-size pass-through (measured: no added CPU on 1080p60).

    HDR: Main10 HEVC keeps it (p010 upload); H.264 tone-maps it to SDR on the device.
    An end pad (end_pad_filter) runs on the software frames, ahead of the upload.
    """
    parts = []
    if plan.deinterlace:
        parts.append(DEINTERLACE_SOFTWARE)
    if plan.width and plan.height:
        parts.append(fit_filter(plan.width, plan.height))
    if end_pad:
        parts.append(end_pad_filter(end_pad))
    if plan.hdr:
        parts.append('format=p010,hwupload')
        if not hevc:
            parts.append(TONEMAP_VAAPI)
    else:
        parts.append('format=nv12,hwupload')
    return ','.join(parts)


def gpu_decode_filter(plan: NormalizePlan, *, hevc: bool):
    """The VAAPI path's -vf when the GPU decodes too (frames never leave the device).
    Only for a clean, single-format recording - see SourceFormats.single_format."""
    parts = []
    if plan.deinterlace:
        parts.append(DEINTERLACE_VAAPI)
    if plan.hdr and not hevc:
        parts.append(TONEMAP_VAAPI)
    size = f'w={plan.width}:h={plan.height}:' if plan.width and plan.height else ''
    fmt = 'p010' if (plan.hdr and hevc) else 'nv12'
    parts.append(f'scale_vaapi={size}format={fmt}')
    parts.append('setsar=1')
    return ','.join(parts)
