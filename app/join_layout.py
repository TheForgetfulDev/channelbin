"""Which of a recording's segments can be joined track by track, before the join runs.

ffmpeg's concat demuxer takes its stream layout from the first file and matches every later
file's streams by position. A segment that declares no video has its sound at position 0, and
the join writes that sound into the video track: H.264 packets holding ADTS audio, and a file
short of that segment's sound (measured on 7.1, dev/changelog/1210). Keeping the declared track
at capture removed the case that was found. This handles one a capture cannot: a stream that
declares no video at all, or one that gains a track after the first segment.

Told which packet id each track carries (`exact_stream_id` in an ffconcat list), the demuxer
matches on that instead, and a track a segment lacks simply has no packets for its stretch. Three
segments of video + sound, sound alone, video + sound joined that way hold 500 frames and all
1,296 audio packets, on one continuous timeline with a hole in the video; by position they hold
864 (dev/changelog/1410). So a segment whose ids do not fit is remuxed to fit, and the plain list
stays the path for every recording whose segments agree, which is nearly all of them.
"""
from typing import NamedTuple, Optional

from .fmt_utils import join_labels
from .proc_utils import TIMELINE_GAP_KEEP_SECONDS

VIDEO = 'video'
AUDIO = 'audio'
#: The packet ids `-map 0:v? -map 0:a?` gives a capture holding one of each.
TRACK_PIDS = {VIDEO: 0x100, AUDIO: 0x101}

#: Beside the segment it rewrites, for the length of the join.
LAYOUT_REMUX_SUFFIX = '.layout.ts'

#: How much of the join ffmpeg may hold in memory to learn a track the first segment lacks. It
#: reads ahead to the first packet of that track, and everything it reads stays in memory until
#: output starts, so this is a cap on a real allocation on a box that may have no swap.
MAX_LEAD_BYTES = 256 * 1024 * 1024
#: Added to the lead so ffmpeg reads far enough into the next file to parse the new track.
_PROBE_MARGIN_BYTES = 32 * 1024 * 1024


class LayoutPlan(NamedTuple):
    """What the join does about the segments' layouts. `tracks` empty means the plain list, as
    ever, and `missing` may still name a segment that lacks a track the others have. Indexes are
    into the list given to plan_layouts()."""
    tracks: tuple = ()
    remux: dict = {}            # index -> the kinds it holds, for a segment to rewrite
    missing: dict = {}          # index -> the kinds it lacks
    set_aside: tuple = ()       # indexes kept out of the join and left on disk
    probe_bytes: Optional[int] = None   # -probesize when the first segment lacks a track


def _kinds(layout):
    return [kind for kind, _pid in layout if kind in TRACK_PIDS]


def _first_pid(layout, kind):
    return next((pid for k, pid in layout if k == kind), None)


def _positions_agree(layouts) -> bool:
    known = [_kinds(lay) for lay in layouts if lay is not None]
    if not known:
        return True
    first = known[0]
    return all(all(a == b for a, b in zip(first, kinds)) and set(kinds) <= set(first)
               for kinds in known[1:])


def plan_layouts(layouts, sizes, max_lead_bytes=MAX_LEAD_BYTES) -> LayoutPlan:
    """The plan for segments with these `layouts` ([(kind, pid), ...], None when one could not
    be read) and file sizes.

    A second audio language beyond the first track is not a mismatch: the join carries one
    audio track either way, chosen by ffmpeg. A later segment lacking a trailing track is not
    one either: nothing lands anywhere wrong, and that stretch simply has none, which the plan
    still names.

    A segment that could not be read is left in the list as it is. The capture command is what
    gave it its ids, so it most likely fits, and the plain join would have taken it too."""
    if _positions_agree(layouts):
        present = {k for lay in layouts if lay is not None for k in _kinds(lay)}
        return LayoutPlan(missing={
            i: lacks for i, lay in enumerate(layouts) if lay is not None
            for lacks in [[k for k in (VIDEO, AUDIO) if k in present and k not in _kinds(lay)]]
            if lacks})

    set_aside = []
    while True:
        kept = [i for i in range(len(layouts)) if i not in set_aside]
        present = {k for i in kept if layouts[i] is not None for k in _kinds(layouts[i])}
        tracks = tuple(k for k in (VIDEO, AUDIO) if k in present)
        lead = 0
        need = set(tracks)
        for i in kept:
            need -= set(tracks if layouts[i] is None else _kinds(layouts[i]))
            if not need:
                break
            lead += sizes[i]
        if lead <= max_lead_bytes or len(kept) == 1:
            break
        set_aside.append(kept[0])

    remux, missing = {}, {}
    for i in kept:
        if layouts[i] is None:
            continue
        lacks = [k for k in tracks if k not in _kinds(layouts[i])]
        if lacks:
            missing[i] = lacks
        if any(_first_pid(layouts[i], k) not in (None, TRACK_PIDS[k]) for k in tracks):
            remux[i] = [k for k in tracks if k not in lacks]
    return LayoutPlan(tracks=tracks, remux=remux, missing=missing, set_aside=tuple(set_aside),
                      probe_bytes=lead + _PROBE_MARGIN_BYTES if lead else None)


def concat_stream_lines(tracks) -> str:
    """The ffconcat header that has the demuxer match `tracks` by packet id."""
    return 'ffconcat version 1.0\n' + ''.join(
        f'stream\nexact_stream_id {TRACK_PIDS[kind]:#x}\n' for kind in tracks)


def remux_args(kinds, source, dest) -> list:
    """ffmpeg arguments that rewrite `source` so each of `kinds` carries its track's packet id.
    A stream copy: nothing is decoded, and only the ids and the container change."""
    # The capture's own gap bound, or this rewrite closes up a kept gap in one stream out
    # of step with the other, exactly as the capture used to (dev/changelog/1415).
    args = ['-v', 'error', '-dts_delta_threshold', str(TIMELINE_GAP_KEEP_SECONDS), '-i', source]
    for kind in kinds:
        args += ['-map', f'0:{kind[0]}:0']
    for out_index, kind in enumerate(kinds):
        args += ['-streamid', f'{out_index}:{TRACK_PIDS[kind]}']
    return args + ['-c', 'copy', '-f', 'mpegts', '-y', dest]


def join_input_args(plan: LayoutPlan) -> list:
    """Options for the concat input. ffmpeg stops looking for a track's format at 5 MB by
    default, and with no format the track is dropped, video included, measured on a
    sound-first join. The duration is set out of the way so the size is what bounds it."""
    if plan.probe_bytes is None:
        return []
    return ['-analyzeduration', str(86400 * 1000000), '-probesize', str(plan.probe_bytes)]


def describe(plan: LayoutPlan, segment_numbers, kept_paths) -> Optional[str]:
    """The sentence the event log carries, or None. `segment_numbers` and `kept_paths` are keyed
    by plan index; the latter names the files left out of the join."""
    parts = []
    for kind in (VIDEO, AUDIO):
        lacking = [segment_numbers[i] for i, kinds in sorted(plan.missing.items())
                   if kind in kinds and i not in kept_paths]
        if lacking:
            what = 'no picture' if kind == VIDEO else 'no sound'
            parts.append(f'{"Segment" if len(lacking) == 1 else "Segments"} '
                         f'{join_labels(str(n) for n in lacking)} had no {kind} track, so the '
                         f'finished file has {what} for that stretch; everything else in '
                         f'{"it" if len(lacking) == 1 else "them"} is in place.')
    if kept_paths:
        parts.append('Kept out of the join and left on disk: '
                     + ', '.join(f'segment {segment_numbers[i]} ({path})'
                                 for i, path in sorted(kept_paths.items()))
                     + '.')
    return ' '.join(parts) or None
