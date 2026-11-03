"""Closing the holes ffmpeg's reconnect repair leaves in a segment's timeline.

ffmpeg reading an MPEG-TS closes every backward timestamp jump it sees, with one offset
shared by every stream (fftools/ffmpeg_demux.c, ts_discontinuity_detect). A provider answers
a reconnected capture from its buffer, so the content comes back from before the drop and
the jump is closed once - the replay. But the H.264 parser hands out a frame only once the
next frame's bytes have arrived, and the demuxer hands the parser whole PES packets, so the
last old video frame leaves the parser only after the new connection's first video PES is
complete. By then the new audio has usually closed the jump: the late old frame lands a
replay's length in the future, under -dts_delta_threshold and so accepted in silence, and
the first new video frame is read as a second backward jump and closed again. The offset
moves twice the replay; every stream's next packet lands a replay later; the file's
timeline has a hole as long as the replay with no content missing on either side
(dev/changelog/1237, measured again on loopback and offline in dev/changelog/1414).

No capture flag stops it. `-fflags +discardcorrupt` drops the truncated frame but the
complete one before it is still in the parser, measured 9 and 7 holes in 28 reconnects
against 10 without; `+noparse` stops the lag but a stream copy then writes nothing, since
no packet carries a keyframe flag; a threshold small enough to close the late frame's
forward jump would close every real gap too. So the capture keeps the file it wrote and the
join repairs it: the segment's own stderr says how many holes there are and how long each
is (proc_utils.read_reconnect_jumps), a scan of the file finds the jump each one left on
every stream, and a stream-copy remux moves everything after each hole back by exactly the
hole, per stream, through ffmpeg's setts bitstream filter under -copyts so nothing else in
the timeline is touched. Only a jump the stderr accounts for is closed: a forward jump with
no reconnect behind it is content the provider never sent, and that stays a gap the
timeline scan reports.
"""
import logging
import os
from typing import NamedTuple

log = logging.getLogger(__name__)

#: How far a jump in the file may sit from the hole the stderr reported, in seconds. The
#: late packet carries the first closure's offset and the stream's next packet the second,
#: and the two closures differ by a frame or two (0.12 s measured); a real gap near a hole
#: inside this margin is the one ambiguity the matcher accepts.
HOLE_MATCH_TOLERANCE = 0.5

#: Forward jumps under this are a stream's ordinary cadence or a reorder, never a hole: the
#: smallest hole read_reconnect_jumps reports is REPLAY_MIN_SECONDS (1 s).
MIN_JUMP_SECONDS = 0.5

#: How far apart, in seconds of the file's timeline, the streams' jumps for one hole may
#: sit: the lead one stream has over the other in the mux, 0.18 s on the fixture and under a
#: couple of seconds on an HD interleave, never the length of a replay.
HOLE_PLACE_WINDOW = 3.0

#: The remux lands beside the segment until it is verified, then replaces it.
HOLE_REMUX_SUFFIX = '.holes.ts'

#: The DIAGNOSTICS kind of the event a join writes per segment it looked at.
DIAGNOSTIC_KIND = 'reconnect_holes_closed'

#: How far the repaired file's duration may sit from the expected one, in seconds, before the
#: remux is refused: the holes closed, less a frame per stream.
DURATION_TOLERANCE = 1.5

#: A stream copy writes the same bytes into fresh packets; a size outside this band means
#: the remux lost or invented content.
SIZE_RATIO = (0.85, 1.15)


class HoleMatch(NamedTuple):
    """One reported hole found on every stream: `reported` is what the stderr said, `shifts`
    is {stream index: (boundary_seconds, shift_seconds)} - every packet of that stream at or
    after the boundary moves back by the shift, which leaves the stream stepping as it did
    before the hole."""
    reported: float
    shifts: dict


class HoleRepair(NamedTuple):
    """What close_reconnect_holes did. `closed` is the seconds taken out per matched hole,
    `left` is [(reported seconds, why)] for the holes that stayed, `error` is why nothing
    was changed at all, `scanned` says the file was read."""
    closed: list
    left: list
    error: str | None
    scanned: bool


def match_reconnect_holes(scan: dict, reported, tolerance: float = HOLE_MATCH_TOLERANCE,
                          window: float = HOLE_PLACE_WINDOW):
    """(matches, left) for the holes the stderr reported against the jumps a timeline scan
    found (probe.scan_timeline_jumps).

    A hole is one movement of the shared offset, so every stream carrying packets shows a
    forward jump of about the hole over its own cadence at the same place in the file: the
    first stream's jump anchors the place, and every other stream's must sit within `window`
    of it, or a real gap of about the hole's length elsewhere on one stream would be closed
    in its stead and the streams moved against each other. The holes are matched in order
    on each stream, and a hole that one stream does not show is left alone on all of them,
    with no stream's cursor moved. Streams with a single packet have no jumps and no say."""
    cursors = {idx: 0 for idx, st in scan.items() if st.get('packets', 0) > 1}
    matches, left = [], []
    for hole in reported:
        shifts, advanced, why = {}, {}, None
        anchor = None
        for idx in sorted(cursors):
            jumps = scan[idx]['jumps']
            pos = cursors[idx]
            found = None
            while pos < len(jumps):
                prev_dts, dts, cadence = jumps[pos]
                excess = dts - prev_dts - (cadence or 0.0)
                pos += 1
                if abs(excess - hole) > tolerance:
                    continue
                place = (prev_dts + dts) / 2.0
                if anchor is not None and abs(place - anchor) > window:
                    continue
                found = (place, excess)
                break
            if found is None:
                why = (f'stream {idx} shows no jump of about {hole:.1f} s'
                       + ('' if anchor is None else f' near {anchor:.1f} s'))
                break
            if anchor is None:
                anchor = found[0]
            shifts[idx] = found
            advanced[idx] = pos
        if why is not None:
            left.append((hole, why))
            continue
        if not shifts:
            left.append((hole, 'the file holds no stream with more than one packet'))
            continue
        cursors.update(advanced)
        matches.append(HoleMatch(hole, shifts))
    return matches, left


def setts_args(matches) -> list:
    """The -bsf arguments that close `matches` on every stream they name: each packet's
    timestamps move back by the sum of the holes before it, keyed on its own decode time in
    the file as written (-copyts keeps that the timeline the filter sees). TB is the
    stream's time base, so the expression needs no knowledge of it."""
    per_stream = {}
    for m in matches:
        for idx, (boundary, shift) in m.shifts.items():
            per_stream.setdefault(idx, []).append((boundary, shift))
    args = []
    for idx in sorted(per_stream):
        terms = ''.join(f'-{shift:.6f}/TB*gte(DTS*TB\\,{boundary:.6f})'
                        for boundary, shift in per_stream[idx])
        args += [f'-bsf:{idx}', f'setts=pts=PTS{terms}:dts=DTS{terms}']
    return args


def remux_args(ffmpeg_path: str, source: str, dest: str, matches) -> list:
    """The stream-copy remux that writes `source` to `dest` with `matches` closed. -copyts
    keeps the timestamps the file was written with, so the filter's boundaries mean what the
    scan measured and ffmpeg's own repair closes nothing on the way through. No
    +discardcorrupt, unlike the join's own read: every packet the segment holds goes
    through, a truncated one included, so the count can be checked against the scan."""
    return [ffmpeg_path, '-err_detect', 'ignore_err', '-fflags', '+genpts',
            '-copyts', '-i', source, '-map', '0', '-c', 'copy',
            *setts_args(matches), '-f', 'mpegts', '-y', dest]


def close_reconnect_holes(path: str, reported, *, ffmpeg_path: str, pre_output_timeout,
                          stall_seconds, on_spawn=None, scan_timeout: int = 600,
                          label: str = 'hole repair') -> HoleRepair:
    """Close the `reported` holes in the segment at `path`, in place.

    Scans the file, matches the holes, remuxes into a sibling file and replaces the
    original only once the result reads as the same content: ffmpeg exited 0, the size is
    the same to within SIZE_RATIO, and the duration is the old one less the holes. A remux
    that fails any of those is removed and the segment is left exactly as it was - a
    repair must never be able to cost the recording the content it is repairing.
    Never raises.
    """
    from .probe import parse_ffprobe, scan_timeline_jumps
    from .proc_utils import supervise_ffmpeg

    reported = [float(h) for h in reported if h]
    if not reported:
        return HoleRepair([], [], None, False)
    scan = scan_timeline_jumps(path, timeout=scan_timeout)
    if not scan:
        return HoleRepair([], [(h, 'the file could not be scanned') for h in reported],
                          'the file could not be scanned', False)
    matches, left = match_reconnect_holes(scan, reported)
    if not matches:
        return HoleRepair([], left, None, True)
    try:
        before = parse_ffprobe(path, count_packets=False, timeout=30).get('duration')
    except Exception as exc:
        log.warning('%s: duration probe of %s failed: %s', label, path, exc)
        before = None
    dest = f'{path}{HOLE_REMUX_SUFFIX}'
    error = None
    try:
        run = supervise_ffmpeg(
            remux_args(ffmpeg_path, path, dest, matches), dest,
            scratch_prefix='concat', scratch_key=os.path.basename(path),
            interval=5, pre_output_timeout=pre_output_timeout, stall_seconds=stall_seconds,
            progress_signal='size', noun='concat', on_spawn=on_spawn, label=label)
        if not run.success:
            error = run.error_msg or run.reason
        else:
            error = _verify(path, dest, before, matches)
    except Exception as exc:
        error = str(exc)
    if error is not None:
        try:
            os.unlink(dest)
        except OSError:
            pass  # a remux that never started wrote nothing to remove
        return HoleRepair([], left + [(m.reported, 'the remux was refused') for m in matches],
                          error, True)
    try:
        os.replace(dest, path)
    except OSError as exc:
        try:
            os.unlink(dest)
        except OSError:
            pass  # the original is untouched either way
        return HoleRepair([], left + [(m.reported, 'the remux was refused') for m in matches],
                          f'could not replace the segment: {exc}', True)
    # The holes as the capture reported them, the figure the segment's own event carries;
    # per stream the shift differs from it by the lead one stream had over the other at
    # the cut, which is what keeps them in step.
    return HoleRepair([m.reported for m in matches], left, None, True)


def _verify(path: str, dest: str, before, matches) -> str | None:
    """Why `dest` is not `path` with the holes closed, or None when it is."""
    from .probe import parse_ffprobe
    try:
        src_size, dst_size = os.path.getsize(path), os.path.getsize(dest)
    except OSError as exc:
        return f'could not size the remux: {exc}'
    if not src_size or not SIZE_RATIO[0] <= dst_size / src_size <= SIZE_RATIO[1]:
        return (f'the remux is {dst_size:,} bytes against the segment\'s {src_size:,}, '
                f'outside the band a stream copy lands in')
    if before is None:
        return 'the segment\'s own duration could not be read, so the remux cannot be checked'
    try:
        after = parse_ffprobe(dest, count_packets=False, timeout=30).get('duration')
    except Exception as exc:
        return f'could not read the remux\'s duration: {exc}'
    if after is None:
        return 'the remux has no readable duration'
    expected = float(before) - sum(max(s for _b, s in m.shifts.values()) for m in matches)
    if abs(float(after) - expected) > DURATION_TOLERANCE:
        return (f'the remux runs {float(after):.1f} s where {expected:.1f} s was expected '
                f'({float(before):.1f} s less the holes)')
    return None
