"""The answer key: what a recording holds, checked against what the simulator sent.

A client reports a lot about a recording - content lost, stalls, restarts, which group member
served which stretch. This module works out what is true, from the recording's own files and
the simulator's ledger, so a test can tell whether those numbers are right rather than only
whether they exist (DESIGN-simulator.md §3.5, §3.12).

A recording's timestamps cannot be read as channel time: a capture moves them to start near
1.4 s, and an in-process reconnect smooths over the jump. Its content can be. A capture
copies the stream, so every packet in a segment is byte for byte a frame of the loop the
simulator cut it from, and in a loop with motion and a burned-in clock no two frames are
alike. So `check()`:

1. lists each segment's packets with ffprobe, with an MD5 of each, and looks each one up in
   the loops the ledger says were served - a run of consecutive frames is a stretch of one
   channel;
2. places each run on the ledger: the connection that sent it and which pass of the loop,
   from the send times in that connection's `offered` range. Connections are used in the
   order they were answered, and a segment never uses an earlier segment's connection - each
   capture process opens its own;
3. turns the placed runs into time on the simulator's clock (which is every channel's clock):
   what the recording holds, what it holds twice, and what it lost, each loss with a reason
   taken from the ledger.

`compare()` then checks figures someone else reported against it. Every figure names which
kind of duration it is (`RECORDING_FIELDS`, `SEGMENT_FIELDS`): content held, content lost, a
file's own timeline and wall clock are different numbers, and comparing one with another is
the mistake this exists to catch.

Placement needs a loop whose frames are all different. Frames of a loop with repeats (the
black, frozen and uniform pictures) are counted and never placed; the placeholder is counted
as placeholder; audio frames are counted, not placed. MP3 radio mounts and HLS recordings
are not read.

This module never shares code with the client it checks: an oracle that parses recordings the
way the client does agrees with its bugs.
"""
import datetime as _dt
import os
import subprocess
import threading
import time
from dataclasses import asdict, dataclass, field
from decimal import Decimal

from .ts import CLOCK_HZ, PTS_HZ, TS

# Why a stretch of the channel is missing from a recording.
REASON_NOT_SENT = 'not_sent'            # no connection was sending it
REASON_REFUSED = 'refused'              # the stream was asked for and not served
REASON_PLACEHOLDER = 'placeholder'      # the offline clip was served in its place
REASON_WITHHELD = 'withheld'            # a connection sent it without its video
REASON_NOT_RECORDED = 'not_recorded'    # sent, and the recording does not hold it
REASONS = (REASON_NOT_SENT, REASON_REFUSED, REASON_PLACEHOLDER, REASON_WITHHELD,
           REASON_NOT_RECORDED)

WITHHOLDING_FAULTS = ('video_stops',)   # stream faults that send a stretch without its video
PLACEHOLDER_FAULT = 'placeholder'
SERVED = (200, 206)

# What each reported figure measures. A comparison is only meaningful between two figures of
# the same kind.
KIND_CONTENT = 'content: seconds of channel, on the simulator clock'
KIND_FILE = "file timeline: the span the file's own timestamps cover"
KIND_WALL = 'wall clock'
KIND_COUNT = 'count'
KIND_NAMES = 'names'

RECORDING_FIELDS = {
    'held_seconds': (KIND_CONTENT, 'channel the recording holds, a stretch held twice '
                                   'counted twice'),
    'distinct_seconds': (KIND_CONTENT, 'channel the recording holds at least once'),
    'repeated_seconds': (KIND_CONTENT, 'channel held more than once, each extra copy counted'),
    'lost_seconds': (KIND_CONTENT, 'channel inside the extent that no segment holds'),
    'lost_count': (KIND_COUNT, 'separate stretches lost'),
    'max_lost_seconds': (KIND_CONTENT, 'the longest stretch lost'),
    'placeholder_seconds': (KIND_CONTENT, "the offline clip's content the recording holds"),
    'segment_count': (KIND_COUNT, 'segment files read'),
    'channels': (KIND_NAMES, 'channels the recording holds content of, as the ledger names '
                             'them'),
    'profiles': (KIND_NAMES, 'loops the recording holds frames of'),
}
SEGMENT_FIELDS = {
    'held_seconds': RECORDING_FIELDS['held_seconds'],
    'distinct_seconds': RECORDING_FIELDS['distinct_seconds'],
    'repeated_seconds': RECORDING_FIELDS['repeated_seconds'],
    'placeholder_seconds': RECORDING_FIELDS['placeholder_seconds'],
    'audio_seconds': (KIND_CONTENT, 'audio frames the segment holds, counted, not placed'),
    'file_video_seconds': (KIND_FILE, "the span of the file's video timestamps, first frame "
                                      'to the end of the last'),
    'connection_seconds': (KIND_WALL, 'from the first connection the segment used being '
                                      'answered to the last one ending'),
    'unidentified': (KIND_COUNT, 'video packets that are no frame of any loop - a frame cut '
                                 'short where a connection ended, for one'),
    'channels': RECORDING_FIELDS['channels'],
    'profiles': RECORDING_FIELDS['profiles'],
}

DEFAULT_TOLERANCE = 0.1     # seconds; a frame or two at the rates the loops play
_EPS27 = 100                # 27 MHz ticks of slack for `offered`'s rounding to the microsecond


class AnswerKeyError(RuntimeError):
    """A recording the checker could not read, with the reason in words."""


# --- what each loop holds ------------------------------------------------------------------------

@dataclass
class _Frames:
    """One stream of one loop, in decode order: each packet's hash and PTS, and for video the
    TS packet its PES starts in."""
    profile: str
    hashes: list
    pts: list
    packet: list
    seconds: float              # one frame's nominal duration
    unique: bool

    @property
    def n(self) -> int:
        return len(self.hashes)


_INDEX: dict = {}               # (path, sha256) -> {'video': _Frames, 'audio': _Frames}
_INDEX_LOCK = threading.Lock()


def _int(v):
    return None if v in (None, '', 'N/A') else int(v)


def probe_packets(ffprobe: str, path: str, timeout: float = 600) -> dict:
    """{'video': [...], 'audio': [...]}: every packet of the file's first video and first audio
    stream, in file order, as dicts of pts, dts, pos, key and hash (an MD5 of its payload)."""
    argv = [ffprobe, '-v', 'error', '-show_entries',
            'stream=index,codec_type:packet=stream_index,pts,dts,pos,flags,data_hash',
            '-show_data_hash', 'MD5', '-of', 'compact=p=1:nk=0', path]
    try:
        # run() drains both pipes as it waits, so neither can fill and block ffprobe.
        out = subprocess.run(argv, stdin=subprocess.DEVNULL, capture_output=True, text=True,
                             timeout=timeout)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise AnswerKeyError(f'ffprobe could not read {path}: {exc}') from exc
    if out.returncode != 0:
        raise AnswerKeyError(f'ffprobe could not read {path}: {out.stderr.strip()[-300:]}')
    kinds, packets = {}, []
    for line in out.stdout.splitlines():
        head, _, rest = line.partition('|')
        kv = dict(x.split('=', 1) for x in rest.split('|') if '=' in x)
        if head == 'stream' and 'index' in kv:
            kinds[int(kv['index'])] = kv.get('codec_type')
        elif head == 'packet':
            packets.append(kv)
    first = {}
    for index in sorted(kinds):
        first.setdefault(kinds[index], index)
    streams = {'video': [], 'audio': []}
    for kv in packets:
        index = _int(kv.get('stream_index'))
        for kind in streams:
            if first.get(kind) == index:
                streams[kind].append({'pts': _int(kv.get('pts')), 'dts': _int(kv.get('dts')),
                                      'pos': _int(kv.get('pos')),
                                      'key': kv.get('flags', '').startswith('K'),
                                      'hash': kv.get('data_hash', '')})
    return streams


def loop_frames(library, profile: str) -> dict:
    """{'video': _Frames, 'audio': _Frames} for a rendered loop, read once per process."""
    m = library.find(profile)
    path = library.path(profile)
    if m is None or path is None:
        raise AnswerKeyError(f'loop {profile!r} is not rendered in {library.directory}')
    key = (path, m.get('sha256'))
    with _INDEX_LOCK:
        hit = _INDEX.get(key)
    if hit is not None:
        return hit
    if not library.ffprobe:
        raise AnswerKeyError('the answer key needs ffprobe: put it on PATH or set SIM_FFMPEG')
    streams = probe_packets(library.ffprobe, path)
    loop = library.load(profile)
    out = {}
    for kind, packets in streams.items():
        hashes = [p['hash'] for p in packets]
        seconds = (loop.L90 / len(packets) / PTS_HZ) if packets else 0.0
        out[kind] = _Frames(profile, hashes, [p['pts'] for p in packets],
                            [None if p['pos'] is None else p['pos'] // TS for p in packets],
                            seconds, len(set(hashes)) == len(hashes))
    with _INDEX_LOCK:
        _INDEX[key] = out
    return out


# --- what the checker reports ----------------------------------------------------------------

@dataclass(frozen=True)
class Stretch:
    """A run of consecutive frames of one channel in one segment, placed on the ledger."""
    start: float        # the simulator clock, epoch seconds, when its first frame is presented
    end: float          # the end of its last frame
    channel: str
    profile: str
    row: int            # the ledger row of the connection that sent it
    segment: int
    frames: int
    # The time its frames are shown, as (start, end) pieces: a run of frames in decode order
    # can leave a hole a frame or two wide at either end, where a B-frame shown earlier than
    # the frame decoded before it was not in the run.
    pieces: tuple = ()

    @property
    def seconds(self) -> float:
        return _length(self.pieces)


@dataclass(frozen=True)
class Lost:
    """A stretch of the simulator clock inside the recording's extent that it does not hold."""
    start: float
    end: float
    reason: str         # one of REASONS
    detail: str
    rows: tuple         # the ledger rows the reason was read from

    @property
    def seconds(self) -> float:
        return self.end - self.start


@dataclass
class SegmentAnswer:
    index: int
    path: str
    stretches: list = field(default_factory=list)
    counted: dict = field(default_factory=dict)     # profile -> frames of a loop with repeats
    placeholder_seconds: float = 0.0
    audio_seconds: float = 0.0
    file_video_seconds: float = 0.0
    connection_seconds: float = 0.0
    unidentified: int = 0
    unplaced: int = 0
    rows: tuple = ()
    # A file the client created and never wrote to, which a capture that connected to
    # nothing leaves behind. It holds nothing and that is not a problem with the ledger.
    empty: bool = False

    @property
    def held_seconds(self) -> float:
        return sum(s.seconds for s in self.stretches)

    @property
    def distinct_seconds(self) -> float:
        return _length(_union(p for s in self.stretches for p in s.pieces))

    @property
    def repeated_seconds(self) -> float:
        return self.held_seconds - self.distinct_seconds

    @property
    def channels(self) -> tuple:
        return tuple(dict.fromkeys(s.channel for s in self.stretches))

    @property
    def profiles(self) -> tuple:
        return tuple(dict.fromkeys([s.profile for s in self.stretches] + list(self.counted)))

    def figures(self) -> dict:
        return {name: getattr(self, name) for name in SEGMENT_FIELDS}


@dataclass
class Answer:
    segments: list
    extent: tuple | None        # (start, end) on the simulator clock, or None if nothing held
    held: list                  # the time held, merged: [(start, end)]
    lost: list
    problems: list

    @property
    def stretches(self) -> list:
        return [s for seg in self.segments for s in seg.stretches]

    @property
    def held_seconds(self) -> float:
        return sum(seg.held_seconds for seg in self.segments)

    @property
    def distinct_seconds(self) -> float:
        return _length(self.held)

    @property
    def repeated_seconds(self) -> float:
        return self.held_seconds - self.distinct_seconds

    @property
    def lost_seconds(self) -> float:
        return sum(x.seconds for x in self.lost)

    @property
    def lost_count(self) -> int:
        return len(self.lost)

    @property
    def max_lost_seconds(self) -> float:
        return max((x.seconds for x in self.lost), default=0.0)

    @property
    def placeholder_seconds(self) -> float:
        return sum(seg.placeholder_seconds for seg in self.segments)

    @property
    def segment_count(self) -> int:
        return len(self.segments)

    @property
    def channels(self) -> tuple:
        return tuple(dict.fromkeys(c for seg in self.segments for c in seg.channels))

    @property
    def profiles(self) -> tuple:
        return tuple(dict.fromkeys(p for seg in self.segments for p in seg.profiles))

    def figures(self) -> dict:
        out = {name: getattr(self, name) for name in RECORDING_FIELDS}
        out['segments'] = [seg.figures() for seg in self.segments]
        return out

    def as_dict(self) -> dict:
        out = self.figures()
        out.update(extent=self.extent, held=self.held, problems=list(self.problems),
                   lost=[asdict(x) for x in self.lost],
                   stretches=[asdict(s) for s in self.stretches])
        return out

    def compare(self, claims: dict, tolerance: float = DEFAULT_TOLERANCE) -> list:
        return compare(self, claims, tolerance)

    def describe(self) -> str:
        """The answer in words, for a failing test's message."""
        lines = [f'{self.segment_count} segment(s); {self.distinct_seconds:.3f} s of channel '
                 f'held ({self.repeated_seconds:.3f} s of it twice), {self.lost_seconds:.3f} s '
                 f'lost in {self.lost_count} stretch(es)']
        for s in self.stretches:
            lines.append(f'  segment {s.segment}: {s.channel} {s.start:.3f} to {s.end:.3f} '
                         f'({s.seconds:.3f} s, {s.frames} frames) from ledger row {s.row}')
        for x in self.lost:
            lines.append(f'  lost {x.start:.3f} to {x.end:.3f} ({x.seconds:.3f} s): '
                         f'{x.reason} - {x.detail}')
        lines += [f'  problem: {p}' for p in self.problems]
        return '\n'.join(lines)


# --- interval arithmetic on the simulator clock ----------------------------------------------

def _union(spans) -> list:
    spans = sorted(spans)
    out = []
    for a, b in spans:
        if out and a <= out[-1][1]:
            out[-1] = (out[-1][0], max(out[-1][1], b))
        else:
            out.append((a, b))
    return out


def _length(spans) -> float:
    return sum(b - a for a, b in spans)


def _minus(span, spans) -> list:
    a, b = span
    out = []
    for c, d in spans:
        if d <= a or c >= b:
            continue
        if c > a:
            out.append((a, c))
        a = max(a, d)
    if a < b:
        out.append((a, b))
    return out


def _to27(seconds: float) -> int:
    return int(Decimal(repr(seconds)) * CLOCK_HZ)


def _epoch(t) -> float:
    if isinstance(t, _dt.datetime):
        if t.tzinfo is None:
            t = t.replace(tzinfo=_dt.timezone.utc)
        return t.timestamp()
    return float(t)


# --- the check -------------------------------------------------------------------------------

@dataclass
class _Run:
    profile: str
    i0: int
    count: int = 1
    last: int = 0
    first_pts: int | None = None
    last_pts: int | None = None


class _Placer:
    """Places runs on the ledger in the order rule the module docstring states."""

    def __init__(self, sim, rows, frames):
        self.sim = sim
        self.rows = rows
        self.frames = frames
        self.timings = {}
        self.floor = 0              # a new segment uses only rows after this one
        self.prev = None            # (row seq, send time of the last frame placed on it)
        self.used = []

    def timing(self, row):
        key = (row.channel, row.profile)
        if key not in self.timings:
            try:
                self.timings[key] = self.sim.headend.timing(row.channel, row.profile)
            except KeyError:
                self.timings[key] = None
        return self.timings[key]

    def new_segment(self):
        if self.used:
            self.floor = max(self.used)
        self.prev = None

    def place(self, run: _Run, segment: int) -> Stretch | None:
        fr = self.frames[run.profile]['video']
        for row in self.rows:
            if row.seq <= self.floor or row.profile != run.profile or row.offered is None:
                continue
            if self.prev is not None and row.seq < self.prev[0]:
                continue
            tm = self.timing(row)
            if tm is None:
                continue
            found = self._fit(run, row, tm, fr)
            if found is None:
                continue
            k, start_send, last_send = found
            if self.prev is not None and row.seq == self.prev[0] and start_send < self.prev[1]:
                # Inside one connection the content only moves forward.
                k = -(-(self.prev[1] - start_send) // tm.loop.L27) + k
                found = self._fit(run, row, tm, fr, k_from=k)
                if found is None:
                    continue
                k, start_send, last_send = found
            self.prev = (row.seq, last_send)
            self.used.append(row.seq)
            return self._stretch(run, row, tm, fr, k, segment)
        return None

    def _fit(self, run, row, tm, fr, k_from=None):
        """The first pass k of the loop at which the run lies inside the row's offered range,
        as (k, send time of its first frame, send time of its last), or None."""
        lp = tm.loop
        lo = _to27(row.offered[0]) + tm.off27 - _EPS27
        hi = _to27(row.offered[1]) + tm.off27 + _EPS27
        first_rel = lp.rel[fr.packet[run.i0]]
        j = run.i0 + run.count - 1
        wraps = j // fr.n
        last_rel = lp.rel[fr.packet[j % fr.n]]
        k_min = -(-(lo - first_rel) // lp.L27)
        k_max = (hi - wraps * lp.L27 - last_rel) // lp.L27
        if k_from is not None:
            k_min = max(k_min, k_from)
        if k_min > k_max:
            return None
        return k_min, k_min * lp.L27 + first_rel, (k_min + wraps) * lp.L27 + last_rel

    def _stretch(self, run, row, tm, fr, k, segment) -> Stretch:
        lp = tm.loop
        step = round(fr.seconds * CLOCK_HZ)
        shown = sorted((k + j // fr.n) * lp.L27 + (fr.pts[j % fr.n] - lp.pcr0) * 300 - tm.off27
                       for j in range(run.i0, run.i0 + run.count))
        pieces = []
        for p in shown:
            if pieces and p - pieces[-1][1] <= step // 2:
                pieces[-1][1] = p + step
            else:
                pieces.append([p, p + step])
        pieces = tuple((round(a / CLOCK_HZ, 6), round(b / CLOCK_HZ, 6)) for a, b in pieces)
        return Stretch(pieces[0][0], pieces[-1][1], row.channel, run.profile, row.seq, segment,
                       run.count, pieces)


def _runs(packets, index, frames) -> tuple:
    """Consecutive frames of one loop, as (runs, unidentified packet count)."""
    runs, cur, unidentified = [], None, 0
    for p in packets:
        cands = index.get(p['hash'])
        if not cands:
            unidentified += 1
            cur = None
            continue
        if cur is not None:
            want = (cur.profile, (cur.last + 1) % frames[cur.profile]['video'].n)
            if want in cands:
                cur.count += 1
                cur.last = want[1]
                cur.last_pts = p['pts']
                continue
        profile, i = cands[0]
        cur = _Run(profile, i, last=i, first_pts=p['pts'], last_pts=p['pts'])
        runs.append(cur)
    return runs, unidentified


def _wait_closed(sim, settle: float) -> list:
    end = time.monotonic() + settle
    while True:
        still = [r for r in sim.ledger.entries(endpoint='stream') if r.open]
        if not still or time.monotonic() >= end:
            return still
        time.sleep(0.05)


def check(sim, segments, *, window=None, channels=None, serving=None, min_gap: float = 0.0,
          settle: float = 3.0) -> Answer:
    """What the recording made of `segments` (paths, in recording order) holds of what `sim`
    sent. `window` is the stretch of the simulator clock the recording was meant to cover, as
    epoch seconds or naive-UTC datetimes; without it the extent is the first frame held to the
    end of the last. `channels` names the channels to explain losses on when the recording
    holds none of them. `serving` names the channels the recording could have been served
    from, and only their connections are placed on: two channels cut from one loop show the
    same frames at different times, so while two recordings capture at once, a run from one
    can fit the other's connection too. Without it every connection is a candidate, which is
    right for one recording at a time. A lost stretch shorter than `min_gap` seconds is left out of `lost`
    (and so of its seconds and count): at 0, the default, a B-frame missing where a segment
    was cut is a loss like any other. Waits up to `settle` seconds for connections still being written to
    end, since a row's `offered` is filled in only then."""
    problems = []
    for r in _wait_closed(sim, settle):
        problems.append(f'ledger row {r.seq} ({r.channel}) was still open after {settle:g} s')
    rows = sorted((r for r in sim.ledger.entries(endpoint='stream')), key=lambda r: r.seq)
    profiles = list(dict.fromkeys(r.profile for r in rows if r.profile))
    placeholder = {r.profile for r in rows if r.profile and PLACEHOLDER_FAULT in r.faults}
    frames = {p: loop_frames(sim.library, p) for p in profiles}
    index, audio = {}, {}
    for p in profiles:
        for i, h in enumerate(frames[p]['video'].hashes):
            index.setdefault(h, []).append((p, i))
        for h in frames[p]['audio'].hashes:
            audio.setdefault(h, frames[p]['audio'].seconds)
    ffprobe = sim.library.ffprobe
    if not ffprobe:
        raise AnswerKeyError('the answer key needs ffprobe: put it on PATH or set SIM_FFMPEG')
    serving = set(serving) if serving is not None else None
    placer = _Placer(sim, [r for r in rows if serving is None or r.channel in serving], frames)
    out = []
    for n, path in enumerate(segments):
        if os.path.getsize(path) == 0:
            placer.new_segment()
            out.append(SegmentAnswer(n, path, empty=True))
            continue
        streams = probe_packets(ffprobe, path)
        seg = SegmentAnswer(n, path)
        placer.new_segment()
        start_used = len(placer.used)
        video = streams['video']
        runs, seg.unidentified = _runs(video, index, frames)
        # A frame cut short where a connection ended is one packet per join; more than that
        # is content the ledger cannot account for, which is never a quiet number.
        if seg.unidentified > max(3, len(video) // 10):
            problems.append(f'segment {n}: {seg.unidentified} of {len(video)} video packets '
                            'are no frame of any loop the ledger says was served')
        for run in runs:
            fr = frames[run.profile]['video']
            if run.profile in placeholder:
                seg.placeholder_seconds += run.count * fr.seconds
                clip = next((r for r in placer.rows if r.seq > placer.floor
                             and r.profile == run.profile
                             and PLACEHOLDER_FAULT in r.faults
                             and r.seq not in placer.used), None)
                if clip is not None:
                    placer.used.append(clip.seq)
                continue
            if not fr.unique:
                seg.counted[run.profile] = seg.counted.get(run.profile, 0) + run.count
                continue
            st = placer.place(run, n)
            if st is None:
                seg.unplaced += run.count
                problems.append(f'segment {n}: {run.count} frame(s) of {run.profile} from frame '
                                f'{run.i0} fit no connection in the ledger')
            else:
                seg.stretches.append(st)
        seg.audio_seconds = sum(audio.get(p['hash'], 0.0) for p in streams['audio'])
        pts = [p['pts'] for p in video if p['pts'] is not None]
        if pts:
            step = frames[runs[0].profile]['video'].seconds if runs else 0.0
            seg.file_video_seconds = (max(pts) - min(pts)) / PTS_HZ + step
        else:
            problems.append(f'segment {n}: no video packets')
        used = sorted(set(placer.used[start_used:]))
        seg.rows = tuple(used)
        if used:
            by_seq = {r.seq: r for r in rows}
            seg.connection_seconds = max(by_seq[s].at + by_seq[s].seconds for s in used) - \
                min(by_seq[s].at for s in used)
        out.append(seg)
    stretches = [s for seg in out for s in seg.stretches]
    held = _union(p for s in stretches for p in s.pieces)
    if window is not None:
        extent = (_epoch(window[0]), _epoch(window[1]))
    elif held:
        extent = (held[0][0], held[-1][1])
    else:
        extent = None
    names = set(channels or ()) | set(serving or ()) | {s.channel for s in stretches}
    lost = _explain(rows, frames, held, extent, names, placer, min_gap) if extent else []
    return Answer(out, extent, held, lost, problems)


def _explain(rows, frames, held, extent, names, placer, min_gap) -> list:
    """Each hole in what is held, split by what the ledger says happened there."""
    first_used = min(placer.used, default=None)
    # A served row is in the extent if what it SENT starts before the extent ends, never by
    # when it was answered: the connect burst sends content from before `at`, so a recording
    # that stops inside the burst has its one row answered after its last frame is shown, and
    # judging that row by `at` dropped it and read every hole as not_sent (dev/changelog/1236).
    # A row that sent nothing has only `at` to be judged by.
    mine = [r for r in rows if r.channel in names
            and (r.offered[0] if r.offered else r.at) <= extent[1]
            and ((first_used is not None and r.seq >= first_used) or r.at >= extent[0] - 1)]
    windows, r_lead = [], {}
    for r in mine:
        if r.offered is None or r.status not in SERVED or r.profile not in frames:
            continue
        fr, tm = frames[r.profile]['video'], placer.timing(r)
        if tm is None or not fr.n:
            continue
        leads = [((fr.pts[i] - tm.loop.pcr0) * 300 - tm.loop.rel[fr.packet[i]]) / CLOCK_HZ
                 for i in range(fr.n)]
        windows.append((r.offered[0] + min(leads), r.offered[1] + max(leads) + fr.seconds, r))
        r_lead[r.seq] = (min(leads), max(leads) + fr.seconds)
    out = []
    for hole in _minus(extent, held):
        if hole[1] - hole[0] < max(min_gap, 1e-6):
            continue
        covering = [(a, b, r) for a, b, r in windows if a < hole[1] and b > hole[0]]
        # Split at every window edge - a connection's and each stretch it withheld video
        # for, as the time those frames would have been shown - so each piece is read from one
        # connection: the one that withheld it if any did, else the first that sent it.
        edges = [x for a, b, _ in covering for x in (a, b)]
        edges += [x for r in (r for _, _, r in covering) for _, lo, hi in r.withheld
                  for x in (lo + r_lead[r.seq][0], hi + r_lead[r.seq][1])]
        cuts = sorted({hole[0], hole[1]} | {x for x in edges if hole[0] < x < hole[1]})
        pieces = []
        for lo, hi in zip(cuts, cuts[1:]):
            over = [r for a, b, r in covering if a < hi and b > lo]
            if not over:
                continue
            held_back = [(r, why) for r in over
                         for why in [_withheld_by(r, lo, hi, r_lead)] if why]
            if held_back:
                r, why = held_back[0]
                pieces.append((lo, hi, REASON_WITHHELD,
                               f'connection {r.seq} sent it without its video ({why})', r))
            else:
                r = over[0]
                pieces.append((lo, hi, REASON_NOT_RECORDED,
                               f'connection {r.seq} sent it; the recording does not hold it '
                               f'({r.note or "no note"})', r))
        for lo, hi in _minus(hole, _union((a, b) for a, b, _ in covering)):
            pieces.append(_unsent(lo, hi, mine, windows))
        out += _merge(sorted(pieces, key=lambda p: (p[0], p[1])))
    return out


def _withheld_by(row, lo, hi, r_lead) -> str | None:
    """The withholding fault that explains [lo, hi) on this connection, or None. Read from
    the row's own record of the channel time it sent without video (`Request.withheld`),
    shifted to when those frames would have been shown - never from the fault book, so a
    fault cleared since the connection played explains exactly what it withheld."""
    first, last = r_lead[row.seq]
    for k, a, b in row.withheld:
        if k in WITHHOLDING_FAULTS and a + first < hi and b + last > lo:
            return k
    return None


def _unsent(lo, hi, mine, windows):
    """A piece no connection was sending: what was asked for then, and what ended before it."""
    asked = [r for r in mine if lo - 1 <= r.at <= hi]
    before = [r for a, b, r in windows if b <= lo + 1e-6]
    after = f'; connection {before[-1].seq} ended before it: {before[-1].note}' \
        if before and before[-1].note else ''
    clip = [r for r in asked if PLACEHOLDER_FAULT in r.faults]
    if clip:
        return (lo, hi, REASON_PLACEHOLDER,
                f'connection {clip[0].seq} was answered with the placeholder clip{after}',
                clip[0])
    refused = [r for r in asked if r.status not in SERVED and r.status != 302]
    if refused:
        r = refused[0]
        return (lo, hi, REASON_REFUSED,
                f'connection {r.seq} was answered {r.status} ({r.note or "no note"}){after}', r)
    return (lo, hi, REASON_NOT_SENT, f'no connection was sending{after}', None)


def _merge(pieces) -> list:
    out = []
    for lo, hi, reason, detail, row in pieces:
        if out and out[-1].reason == reason and out[-1].detail == detail \
                and lo <= out[-1].end + 1e-6:
            prev = out[-1]
            out[-1] = Lost(prev.start, max(prev.end, hi), reason, detail, prev.rows)
            continue
        out.append(Lost(round(lo, 6), round(hi, 6), reason, detail,
                        (row.seq,) if row is not None else ()))
    return out


# --- comparing reported figures with the answer --------------------------------------------

@dataclass(frozen=True)
class Mismatch:
    field: str          # 'lost_seconds', or 'segments[1].held_seconds'
    claimed: object
    truth: object
    kind: str
    tolerance: float | None

    def __str__(self):
        within = f', within {self.tolerance:g} s' if self.tolerance is not None else ''
        return (f'{self.field}: reported {self.claimed!r}, the answer key says '
                f'{self.truth!r} ({self.kind}{within})')


def _check_field(name, label, kind, claimed, truth, tolerance):
    if kind == KIND_NAMES:
        if set(claimed) != set(truth):
            return Mismatch(label, tuple(claimed), tuple(truth), kind, None)
        return None
    if kind == KIND_COUNT:
        if claimed != truth:
            return Mismatch(label, claimed, truth, kind, None)
        return None
    if claimed is None or abs(float(claimed) - truth) > tolerance:
        return Mismatch(label, claimed, round(truth, 6), kind, tolerance)
    return None


def compare(answer: Answer, claims: dict, tolerance: float = DEFAULT_TOLERANCE) -> list:
    """Every figure in `claims` the answer disagrees with, as a list of Mismatch - empty when
    they all agree. `claims` holds RECORDING_FIELDS names, plus `segments`, a list with one
    dict of SEGMENT_FIELDS names (or None to skip it) per segment. A name in neither is an
    error rather than a figure quietly not checked."""
    out = []
    figures = answer.figures()
    for name, claimed in claims.items():
        if name == 'segments':
            if len(claimed) != len(answer.segments):
                out.append(Mismatch('segment_count', len(claimed), len(answer.segments),
                                    KIND_COUNT, None))
                continue
            for i, seg_claims in enumerate(claimed):
                for sname, sclaimed in (seg_claims or {}).items():
                    if sname not in SEGMENT_FIELDS:
                        raise ValueError(f'no segment figure called {sname!r}; the answer key '
                                         f'reports {", ".join(SEGMENT_FIELDS)}')
                    m = _check_field(sname, f'segments[{i}].{sname}', SEGMENT_FIELDS[sname][0],
                                     sclaimed, figures['segments'][i][sname], tolerance)
                    if m is not None:
                        out.append(m)
            continue
        if name not in RECORDING_FIELDS:
            raise ValueError(f'no figure called {name!r}; the answer key reports '
                             f'{", ".join(RECORDING_FIELDS)} and segments')
        m = _check_field(name, name, RECORDING_FIELDS[name][0], claimed, figures[name],
                         tolerance)
        if m is not None:
            out.append(m)
    return out


__all__ = ['Answer', 'AnswerKeyError', 'Lost', 'Mismatch', 'RECORDING_FIELDS', 'REASONS',
           'REASON_NOT_RECORDED', 'REASON_NOT_SENT', 'REASON_PLACEHOLDER', 'REASON_REFUSED',
           'REASON_WITHHELD', 'SEGMENT_FIELDS', 'SegmentAnswer', 'Stretch', 'check', 'compare',
           'loop_frames', 'probe_packets']
