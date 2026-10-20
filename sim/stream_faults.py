"""Stream faults as the headend applies them. The record - each kind's evidence, basis,
settings and the requests it acts on - is sim/faults.py's; this module is what they do.

Two halves, met in this order by a stream request:

- **When it is answered** (`plan_open`): the open-layer faults the request took decide whether
  it is refused, held without an answer, delayed, given the placeholder clip or another loop,
  and which connection faults - silence or a close after so many bytes - ride on the
  connection it opens.
- **While it plays** (`LiveFaults`): a connection reads its channel's stream-layer faults on
  every piece and applies each by channel time - the window's start and end are moments on the
  channel clock. Bytes stay a pure function of which packets are sent; a fault that changes the
  pace changes only when they are sent.

What a fault does to the bytes is `shape()`, shared by an MPEG-TS connection and an HLS segment
so the two always agree.
"""
import datetime as _dt
import select
import socket
from dataclasses import dataclass, field

from .faults import LAYER_OPEN, LAYER_STREAM, nginx_page
from .ts import PTS_HZ, PTS_WRAP, TS

_EPOCH = _dt.datetime(1970, 1, 1)
_NEVER_BEFORE = -(1 << 62)
_NEVER_AFTER = 1 << 62
_REASONS = {400: 'Bad Request', 403: 'Forbidden', 404: 'Not Found',
            405: 'Method Not Allowed', 410: 'Gone', 451: 'Unavailable For Legal Reasons',
            500: 'Internal Server Error', 502: 'Bad Gateway',
            503: 'Service Temporarily Unavailable'}
_SWAPS = ('format_change', 'audio_layout_change', 'picture', 'low_bitrate')


def epoch_ns(when: _dt.datetime) -> int:
    d = when - _EPOCH
    return (d.days * 86400 + d.seconds) * 1_000_000_000 + d.microseconds * 1000


def peer_gone(sock) -> bool:
    """Whether the client on `sock` has hung up, without reading anything it sent."""
    try:
        ready, _, _ = select.select([sock], [], [], 0)
        if not ready:
            return False
        return sock.recv(1, socket.MSG_PEEK) == b''
    except OSError:
        return True


# --- when a stream request is answered ----------------------------------------------------------

@dataclass
class OpenPlan:
    """What the open-layer faults a request took make of its answer."""
    hold: float | None = None       # accept and never answer, for up to this many seconds
    refuse: tuple | None = None     # (status, body)
    delay: float = 0.0              # seconds before the first byte
    placeholder: object = None      # the placeholder fault, when the clip is served
    profile: str | None = None      # another loop for the channel
    silent_after: int | None = None
    hold_silence: float = 0.0
    close_after: int | None = None
    range_start: int = 0            # the request's Range, which only the placeholder honors
    names: list = field(default_factory=list)

    def gate(self):
        """(status, body, note) when the request is not to be served at all, else None."""
        if self.hold is not None:
            return 0, b'', f'held without an answer by no_answer for up to {self.hold:g} s'
        if self.refuse is not None:
            return self.refuse[0], self.refuse[1], f'refused with {self.refuse[0]} by refuse_at_open'
        return None


def plan_open(taken) -> OpenPlan:
    """The plan for one stream request from the faults it took, in the order they were added;
    where two would do the same thing, the first one decides."""
    plan = OpenPlan()
    for a in taken:
        if a.kind.layer not in (LAYER_OPEN, LAYER_STREAM):
            continue
        name, p = a.kind.name, a.params
        plan.names.append(name)
        if name == 'no_answer' and plan.hold is None:
            plan.hold = p['seconds']
        elif name == 'refuse_at_open' and plan.refuse is None:
            s = p['status']
            plan.refuse = (s, nginx_page(s, _REASONS.get(s, 'Error')))
        elif name == 'slow_first_byte':
            plan.delay = max(plan.delay, p['seconds'])
        elif name == 'placeholder' and plan.placeholder is None:
            plan.placeholder = a
        elif name in _SWAPS and plan.profile is None:
            plan.profile = p['profile']
        elif name == 'silent_socket' and plan.silent_after is None:
            plan.silent_after, plan.hold_silence = p['after_bytes'], p['hold_seconds']
        elif name == 'close_mid_stream' and plan.close_after is None:
            plan.close_after = p['after_bytes']
    return plan


# --- while a stream plays -----------------------------------------------------------------------

def _window(a) -> tuple[int, int]:
    t = a.trigger
    return (epoch_ns(t.start) if t.start else _NEVER_BEFORE,
            epoch_ns(t.end) if t.end else _NEVER_AFTER)


class LiveFaults:
    """One connection's view of its channel's stream-layer faults. `refresh()` re-reads them,
    so a fault injected while the connection plays reaches it at once; `applied` names every
    one that changed what it sent, in the order they first did."""

    def __init__(self, book, channel: str, backend: str):
        self.book = book
        self.channel = channel
        self.backend = backend
        self.faults = []
        self.applied = []
        self.withheld = {}      # fault name -> [first packet, end packet) whose video went

    def refresh(self) -> 'LiveFaults':
        self.faults = self.book.live(self.channel, self.backend) if self.book else []
        return self

    def mark(self, a) -> None:
        if a.kind.name not in self.applied:
            self.applied.append(a.kind.name)
            a.note_hit()

    def withhold(self, a, lo: int, hi: int) -> None:
        """Packets lo to hi (exclusive) were sent without their video because of `a`."""
        self.mark(a)
        span = self.withheld.get(a.kind.name)
        if span is None:
            self.withheld[a.kind.name] = [lo, hi]
        else:
            span[0], span[1] = min(span[0], lo), max(span[1], hi)

    def spans(self, timing) -> tuple:
        """What this connection withheld, as the ledger records it: (fault name, first, last)
        in channel seconds, from the send times of the first withheld packet and the end."""
        loop = timing.loop
        return tuple((name, round(timing.channel_seconds(loop.send_time(lo)), 6),
                      round(timing.channel_seconds(loop.send_time(hi)), 6))
                     for name, (lo, hi) in self.withheld.items())

    def of(self, *names):
        return [a for a in self.faults if a.kind.name in names]

    def holding(self, a, now_ns: int) -> bool:
        lo, hi = _window(a)
        return lo <= now_ns < hi

    # --- pace ---------------------------------------------------------------------------------

    def rate(self, now_ns: int):
        """How many seconds of channel a second of wall clock sends right now, or None at the
        normal pace."""
        for a in self.of('fast_reserve', 'throttle'):
            if not self.holding(a, now_ns):
                continue
            self.mark(a)
            if a.kind.name == 'fast_reserve':
                return a.params['speed']
            r0, r1 = a.params['rate'], a.params['rate_end']
            lo, hi = _window(a)
            if r1 is None or lo == _NEVER_BEFORE or hi == _NEVER_AFTER:
                return r0
            return r0 + (r1 - r0) * (now_ns - lo) / (hi - lo)
        return None

    def chunk_seconds(self, now_ns: int):
        for a in self.of('live_edge_chunks'):
            if self.holding(a, now_ns):
                return a
        return None

    # --- bytes --------------------------------------------------------------------------------

    def video_windows(self, timing) -> list:
        """(first packet, end packet, fault) of each stretch whose video is withheld."""
        out = []
        for a in self.of('video_stops'):
            lo, hi = _window(a)
            out.append((_packet_at(timing, lo), _packet_at(timing, hi), a))
        return out

    def shifts(self, timing, hls_cut=None) -> tuple[int, list]:
        """(the shift every packet takes, [(first packet, shift from there on, fault)]) in
        90 kHz ticks. `hls_cut(ns)` moves a jump to the first HLS segment starting at or after
        the moment, where an HLS discontinuity falls."""
        whole = 0
        steps = []
        for a in self.of('pts_wrap'):
            wrap_s = epoch_ns(a.params['wrap_at']) / 1e9
            # The stream's PCR at channel time t is t * 90 kHz plus the channel's PTS offset
            # (sim/streams.py TsTiming); this puts that sum's 33-bit wrap at wrap_at.
            whole = (whole + (-(round(wrap_s * PTS_HZ) + timing.pts_offset)) % PTS_WRAP) % PTS_WRAP
            steps.append((_NEVER_BEFORE, 0, a))
        for a in self.of('timestamp_jump', 'hls_discontinuity'):
            hls = a.kind.name == 'hls_discontinuity'
            if hls and hls_cut is None:
                continue
            at = _window(a)[0]
            g = hls_cut(at) if hls else _packet_at(timing, at)
            steps.append((g, round(a.params['seconds'] * PTS_HZ), a))
        return whole, steps


def _packet_at(timing, ns: int) -> int:
    """The first packet a channel sends at or after channel time `ns`."""
    if ns <= _NEVER_BEFORE:
        return _NEVER_BEFORE
    if ns >= _NEVER_AFTER:
        return _NEVER_AFTER
    return timing.loop.packets_through(timing.send_time(ns) - 1)


def shape(timing, g0: int, g1: int, live: LiveFaults | None, hls_cut=None) -> bytes:
    """Packets g0 to g1 of a channel's stream with its stream-layer faults applied: timestamps
    shifted where a wrap or a jump says, video withheld where it stops."""
    loop = timing.loop
    if live is None or not live.faults:
        return loop.cut(g0, g1, timing.base90)
    whole, steps = live.shifts(timing, hls_cut)
    bounds = sorted({g for g, _, _ in steps if g0 < g < g1})
    out = []
    edges = [g0] + bounds + [g1]
    for a, b in zip(edges, edges[1:]):
        shift = whole
        for g, d, f in steps:
            if g <= a:
                shift += d
                live.mark(f)
        out.append(loop.cut(a, b, (timing.base90 + shift) % PTS_WRAP))
    data = b''.join(out)
    for ga, gb, f in live.video_windows(timing):
        lo, hi = max(g0, ga), min(g1, gb)
        if lo < hi:
            data = _withhold(loop, data, g0, lo, hi, loop.video_pid)
            live.withhold(f, lo, hi)
    return data


def _withhold(loop, data: bytes, g0: int, lo: int, hi: int, pid: int) -> bytes:
    """`data` (packets from g0 on) without the packets of `pid` numbered lo to hi."""
    n, pids = loop.n, loop.pids
    keep = []
    start = 0           # byte offset of the first packet not yet kept
    for g in range(lo, hi):
        if pids[g % n] == pid:
            o = (g - g0) * TS
            keep.append(data[start:o])
            start = o + TS
    keep.append(data[start:])
    return b''.join(keep)


__all__ = ['LiveFaults', 'OpenPlan', 'epoch_ns', 'peer_gone', 'plan_open', 'shape']
