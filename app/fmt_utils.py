"""Human-readable formatting helpers shared across backend modules - canonical home
for Python-side display formatting (the JS equivalents live in static/js/util.js).
Keep fmt_bytes' output in sync with util.js fmtBytes where they surface side by side."""


# ── Stream quality profile vocabulary (dev/changelog/351) ───────────────────
# Three surfaces render the same seven measurements: the recording detail page
# (app/routes/recordings.py), the channel detail page (app/routes/channels.py),
# and the group page's per-row drawer (static/js/group-detail.js). The wording is
# the product here - a reader who learns "4:2:2 keeps more color detail" on one
# page must not meet different prose for the same column on another - so the
# strings live here once and the two Python surfaces import them.
#
# The placeholder is '-', not an em dash, per CLAUDE.md's em-dash ban.
PROFILE_NONE = '-'

CHROMA_TIP = ('Chroma subsampling. 4:2:2 keeps more color detail than the usual 4:2:0; '
              'some players and remuxers handle it differently.')
INTERLACED_TIP = ('Interlaced source. Some players deinterlace it, some do not - the '
                  'recording may show combing.')
PROGRESSIVE_TIP = 'Progressive - one full frame at a time.'
SCAN_UNKNOWN_TIP = 'ffprobe reported no field order for this stream.'
VFR_TIP = ('Variable frame rate: the declared and average frame rates disagree. Can '
           'drift audio sync in a long recording.')
VFR_UNKNOWN_TIP = 'Constant-vs-variable frame rate was not determined for this stream.'
CFR_TIP = 'Constant frame rate: the declared and average frame rates agree.'
EFFICIENCY_TIP = ('Bits per pixel per frame = bitrate / (width x height x fps). A standard '
                  'compression-efficiency stat, not a better/worse verdict.')
TIMELINE_GAP_TIP = ('Presentation-time gaps found in the test clip - the source dropped or '
                    'paused mid-stream.')
CODED_TIP = "The encoder's padded frame size, which differs from the displayed resolution."
TRACKS_TIP = ('This test found more than one video or audio track. Only one track of each '
              'type is used for recording or health scoring - the rest are listed here for '
              'reference.')


def fmt_chroma(v):
    """'422' -> '4:2:2'; falsy -> None. The column stores the digits only."""
    return ':'.join(str(v)) if v else None


def profile_summary(video_codec, bit_depth, chroma_subsampling, interlaced, is_vfr,
                    resolution=None):
    """One compact line for a table cell: 'h264 - 10-bit - 4:2:2 - 1080i - VFR'.

    Returns None when the row carries no profile at all (a test predating capture),
    so the caller renders its own placeholder rather than an empty separator run.
    Pure - no I/O, safe inside a per-row loop (CLAUDE.md, no hidden I/O).

    The interlaced flag prefers the scan-height form ('1080i') when a resolution is
    known, because that is how a reader already recognizes an interlaced broadcast
    feed; it falls back to the bare word when it is not.
    """
    parts = [p for p in (
        video_codec,
        f'{bit_depth}-bit' if bit_depth else None,
        fmt_chroma(chroma_subsampling),
    ) if p]
    if interlaced:
        height = None
        if resolution and 'x' in str(resolution):
            tail = str(resolution).split('x')[-1].strip()
            if tail.isdigit():
                height = tail
        parts.append(f'{height}i' if height else 'Interlaced')
    if is_vfr:
        parts.append('VFR')
    return ' - '.join(parts) if parts else None


def fmt_bytes(n):
    """1234567 -> '1.2 MB'; None -> '?'."""
    if n is None:
        return '?'
    for unit in ('B', 'KB', 'MB', 'GB', 'TB'):
        if n < 1024:
            return f'{n:.1f} {unit}'
        n /= 1024
    return f'{n:.1f} PB'


def fmt_duration(seconds, with_seconds=False):
    """Canonical duration text (DESIGN.md 9.1): 'Xh Ym' / 'Xh' / 'Xm', never colon-style
    ('3:01' reads as a clock time). with_seconds=True keeps seconds where they carry
    meaning (segment durations, live elapsed/remaining/downtime tickers, an offset into
    a recording). None in, None out - the caller renders its own placeholder."""
    if seconds is None:
        return None
    s = max(0, int(seconds))
    h, rem = divmod(s, 3600)
    m, sec = divmod(rem, 60)
    if with_seconds:
        if h:
            return f'{h}h {m}m {sec}s'
        if m:
            return f'{m}m {sec}s'
        return f'{sec}s'
    if h and m:
        return f'{h}h {m}m'
    if h:
        return f'{h}h'
    return f'{m}m'


def fmt_duration_hm(seconds):
    """Exact 'Nh Mm' for a length that is itself exact (a configured window's
    start-to-end span), e.g. 14400 -> '4h 0m'. Both parts always shown, unlike
    fmt_duration_phrase's rounded/omitted form below."""
    total_min = int(round(seconds / 60))
    h, m = divmod(total_min, 60)
    return f'{h}h {m}m'


def fmt_duration_phrase(seconds):
    """1234 -> 'about 21 minutes' - a rounded, human phrase for an estimate (a health
    check's expected run time, a day's total booked time), as opposed to fmt_duration_hm's
    exact form for a window's own configured length.

    duplicated from static/js/check-modal.js::ccDurationPhrase - the modal needs the same
    phrasing client-side without a round trip to the server."""
    if seconds < 60:
        return 'under a minute'
    mins = round(seconds / 60)

    def unit(v, word):
        return f'{v} {word}' if v == 1 else f'{v} {word}s'

    if mins < 90:
        return f'about {unit(mins, "minute")}'
    h, m = divmod(mins, 60)
    return f'about {unit(h, "hour")} {unit(m, "minute")}' if m else f'about {unit(h, "hour")}'


def fmt_job_duration_line(avg_seconds, runs: int) -> str:
    """The Scheduled Jobs page's estimated-runtime line for one job, from
    (avg_seconds, runs) as returned by database.get_job_duration_estimate() /
    accounts.get_sync_duration_estimate(). Wording matches the approved
    dev/mockups/29 dashboard-rail mockup's jobDurationLine() exactly (dev/changelog/592) -
    that mockup is the contract for this string, not a draft to re-derive from. avg_seconds is None
    (never 0 - a job that has never completed a run must never borrow another job's
    number) when the job has no completed run yet."""
    if avg_seconds is None:
        return 'Expected runtime unknown - this job has never completed a run.'
    basis = f'average of the last {runs} run{"" if runs == 1 else "s"}'
    return f'Usually takes {fmt_duration_phrase(avg_seconds)} ({basis}).'
