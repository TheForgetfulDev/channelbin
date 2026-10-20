"""Live streams: every channel of a backend as MPEG-TS, HLS or an MP3 radio mount.

A channel runs whether or not anyone watches it. Its stream is a pure function of channel time
- which pass of its loop, where in the pass, how far its timestamps have moved on - so nothing
runs for a channel nobody watches, and each connection cuts its own bytes from the one shared
copy of the loop (sim/ts.py). What a client sees is what a headend with a ring buffer per
channel would send, and two things real providers do fall out of it (DESIGN-simulator.md §3.3):
a client that reconnects 45 seconds later lands 45 seconds later in the content, and a new
connection gets the channel's last `burst_seconds` at once and then real time - 13 to 29 s on
real accounts, about 25 s typically, replayed on every reconnect (dev/changelog/942, 997).

Channel time is the simulator's clock as of the moment it was last set, moving on in real time
from there, so a test that sets the clock moves every channel with it and a stream always plays
at real speed. Each channel has its own content offset and its own timestamp base, fixed by the
seed, so two channels on one loop show different pictures and two group members never share a
clock.

Seats are counted per login at the backend: one login used through two panels has one set of
seats, and two accounts on one backend share streams but not seats. What a backend does when a
login asks for one more connection than `max_connections` allows is its `over_limit` policy.
The default is what a real provider did, seen once (DESIGN-simulator.md §3.10): the new
connection plays and the login's oldest one ends a few seconds later, cleanly, as if the
stream had finished. Refusing it, and allowing it then banning the login, are assumed.

How a request is answered follows the same check. An Xtream stream URL is answered with a 302
to a play URL carrying a token, `/live/play/<token>/<id>`, on one of the backend's stream hosts
in turn (`stream_redirect`; off, MPEG-TS is served where it was asked for). The play URL serves
MPEG-TS, or for a `.m3u8` request the media playlist itself, which rolls `hls_window` segments
cut at keyframes, named `/hls/<token>/<id>_<seq>.ts`. A connection, and each segment, starts at
a cut point: the PAT and PMT, then a keyframe. An HLS client holds a seat until it has fetched
nothing for `hls_idle_seconds`. A play token stays valid, so a client reconnecting to it is
served again (assumed). A third-party HLS channel answers a master playlist with two variants
and holds no seat (assumed). A radio mount is MP3 at its own bitrate, opening with a burst the
size of Icecast's default.

Stream faults (sim/faults.py, sim/stream_faults.py) act at two moments. When a request is
answered, the faults it took may refuse it, hold it without an answer, delay it, give it the
placeholder clip or another loop, or arm a silence or a close on its connection. While a
connection plays, it reads its channel's faults on every piece and applies them by channel
time: video withheld, timestamps moved, another pace, lumps at the live edge. A 302 to a play
URL is never faulted.
"""
import datetime as _dt
import math
import re
import threading
import time
from dataclasses import dataclass, field

from .content import PROFILES, RADIO_PROFILE, ContentError, profile_for_format
from .faults import (FORM_HLS_MASTER, FORM_HLS_PLAYLIST, FORM_HLS_SEGMENT, FORM_RADIO, FORM_TS,
                     STAGE_REDIRECT, STAGE_SERVE, drifted_credentials)
from .lineup import KIND_NO_ID, KIND_RADIO, channel_label
from .mp3 import SAMPLES_PER_FRAME
from .rand import derive
from .stream_faults import OpenPlan, LiveFaults, epoch_ns, plan_open, shape
from .ts import CLOCK_HZ, PTS_HZ, PTS_WRAP, TS
from .world import CONTENT_CALIBRATED

TICK_SECONDS = 0.02
CHUNK_PACKETS = 1394                # about 256 KiB a write
RADIO_BURST_BYTES = 65536           # Icecast's default burst-size; assumed for these providers
HLS_KEEP_BEHIND = 3                 # segments still served after they leave the window
PLAY_TOKENS_KEPT = 4096             # the oldest play tokens are forgotten past this many

TS_TYPE = 'video/mp2t'
HLS_TYPE = 'application/vnd.apple.mpegurl'
XTREAM_HLS_TYPE = 'application/x-mpegurl'
MP3_TYPE = 'audio/mpeg'

KIND_TS = 'ts'
KIND_HLS = 'hls'

_CRED_RX = re.compile(r'^/(?:live/)?(?P<user>[^/]+)/(?P<password>[^/]+)/(?P<sid>\d+)'
                      r'(?:\.(?P<ext>ts|m3u8))?$')
_PLAY_RX = re.compile(r'^/live/play/(?P<token>[0-9a-f]{16})/(?P<sid>\d+)$')
_SEGMENT_RX = re.compile(r'^/hls/(?P<token>[0-9a-f]{16})/(?P<sid>\d+)_(?P<seq>\d+)\.ts$')
_RADIO_RX = re.compile(r'^/radio/(?P<slug>[a-z0-9-]+)\.mp3$')
_TP_HLS_RX = re.compile(r'^/hls/(?P<slug>[a-z0-9-]+)/(?:index\.m3u8|(?P<variant>v\d)/'
                        r'(?:index\.m3u8|(?P<seq>\d+)\.ts))$')
_VOD_RX = re.compile(r'^/(?:movie|series)/')
_RANGE_RX = re.compile(r'^\s*bytes=(?P<start>\d+)-\d*\s*$')


class ChannelClock:
    """The simulator's clock as of the last `set()`, moving on in real time."""

    def __init__(self, when: _dt.datetime):
        self._lock = threading.Lock()
        self.set(when)

    def set(self, when: _dt.datetime) -> None:
        with self._lock:
            self._anchor = epoch_ns(when)
            self._mono = time.monotonic_ns()

    def now_ns(self) -> int:
        with self._lock:
            return self._anchor + (time.monotonic_ns() - self._mono)


def is_stream_path(path: str) -> bool:
    """A path a panel's own host answers as a stream rather than as a panel endpoint."""
    return bool(_CRED_RX.match(path) or _PLAY_RX.match(path) or _SEGMENT_RX.match(path)
                or _VOD_RX.match(path))


# --- one channel's timing on one loop ---------------------------------------------------------

@dataclass(frozen=True)
class Feed:
    """One channel of a backend: which loop it plays and where its clock sits on it."""
    backend: str
    key: str            # the stream id, or radio/<slug>, or hls/<slug>
    profile: str
    seed: int

    def content_offset(self, length: int) -> int:
        return derive(self.seed, 'content-offset', self.backend, self.key) % length

    @property
    def pts_offset(self) -> int:
        return derive(self.seed, 'pts-offset', self.backend, self.key) % PTS_WRAP


class TsTiming:
    """A feed laid on its loop: channel time to the loop's send time and back, and the
    timestamp shift that makes the stream's PCR the channel's clock."""

    def __init__(self, loop, feed: Feed):
        self.loop = loop
        off90 = feed.content_offset(loop.L90)
        self.off27 = off90 * 300
        self.pts_offset = feed.pts_offset
        self.base90 = (feed.pts_offset - loop.pcr0 - off90) % PTS_WRAP

    def send_time(self, now_ns: int) -> int:
        return now_ns * 27 // 1000 + self.off27

    def channel_seconds(self, s: int) -> float:
        return (s - self.off27) / CLOCK_HZ


class TsConnection:
    """One client's MPEG-TS stream: the burst first, then real time. It opens at the cut point
    at or before `burst_seconds` back - the PAT, the PMT and a keyframe - so the burst can be up
    to one keyframe interval longer than asked.

    `live` is the channel's stream-layer faults (sim/stream_faults.py), read on every piece;
    `plan` carries the connection faults it took when it was answered - silence or a close
    after so many bytes, cut on a packet boundary at or before the count. `client_gone` is
    the server's way of saying the client hung up, which a silent connection cannot learn
    from a write."""

    def __init__(self, loop, feed: Feed, clock: ChannelClock, burst_seconds: float, seat=None,
                 live: LiveFaults | None = None, plan: OpenPlan | None = None):
        self.timing = TsTiming(loop, feed)
        self.loop = loop
        self.clock = clock
        self.seat = seat
        self.live = live
        plan = plan or OpenPlan()
        self.silent_after, self.hold_silence = plan.silent_after, plan.hold_silence
        self.close_after = plan.close_after
        start = self.timing.send_time(clock.now_ns()) - int(burst_seconds * CLOCK_HZ)
        self.g = self.first = loop.cut_packet(max(loop.cuts_through(start) - 1, 0))
        self.ended = ''
        self.sent = 0
        self.client_gone = None
        self._allow = None          # how far a changed pace lets it send, as a send time
        self._last_ns = None
        self._caught_up = False
        self._lumps = 0

    @property
    def applied(self) -> list:
        return list(self.live.applied) if self.live else []

    @property
    def withheld(self) -> tuple:
        return self.live.spans(self.timing) if self.live else ()

    def _connection_fault(self):
        """'silent' or 'close' once the next packet would pass that fault's byte count."""
        due = [(b, k) for b, k in ((self.silent_after, 'silent'), (self.close_after, 'close'))
               if b is not None and self.sent + TS > b]
        return min(due)[1] if due else None

    def _packets_left(self):
        caps = [b - self.sent for b in (self.silent_after, self.close_after) if b is not None]
        return max(0, min(caps)) // TS if caps else None

    def _allowance(self, now_ns: int, live) -> int:
        """The send time the connection may have sent up to. Normally the live edge; under a
        changed pace it moves at that rate from where the change found it - from the live edge
        when faster, from what was already sent when slower - and once the pace is normal again
        it waits for the clock if it ran ahead and sends the backlog if it fell behind."""
        edge = self.timing.send_time(now_ns)
        dt = 0 if self._last_ns is None else now_ns - self._last_ns
        self._last_ns = now_ns
        rate = live.rate(now_ns) if live is not None else None
        if rate is None:
            if self._allow is not None and self._allow > edge:
                return self._allow
            self._allow = None
            return edge
        if self._allow is None:
            self._allow = edge if rate > 1 else self.loop.send_time(self.g)
        else:
            self._allow += int(rate * dt * 27 // 1000)
        return self._allow

    def pieces(self, stopping: threading.Event):
        loop, timing = self.loop, self.timing
        while True:
            if stopping.is_set():
                self.ended = 'the simulator stopped'
                return
            if self.seat is not None and self.seat.is_cut():
                self.ended = self.seat.kicked_why
                return
            fault = self._connection_fault()
            if fault == 'close':
                self.ended = f'closed after {self.sent} bytes by close_mid_stream'
                return
            if fault == 'silent':
                self._silence(stopping)
                return
            now = self.clock.now_ns()
            live = self.live.refresh() if self.live is not None else None
            due = loop.packets_through(self._allowance(now, live))
            if due > self.g:
                chunk = live.chunk_seconds(now) if live is not None else None
                if chunk is not None and self._caught_up and (
                        loop.send_time(due) - loop.send_time(self.g)
                        < self._lump_seconds(chunk) * CLOCK_HZ):
                    live.mark(chunk)
                    stopping.wait(TICK_SECONDS)
                    continue
                if chunk is not None and self._caught_up:
                    self._lumps += 1
                g1 = min(due, self.g + CHUNK_PACKETS)
                left = self._packets_left()
                if left is not None:
                    g1 = min(g1, self.g + left)
                piece = shape(timing, self.g, g1, live)
                self.g = g1
                self.sent += len(piece)
                yield piece
                if g1 < due:
                    continue        # still catching up - the burst - so no pause
                self._caught_up = True
            # Caught up: wait a tick. Looping straight back would find a packet or two newly
            # due and send it on its own, which at 50 streams measured half a core of overhead.
            stopping.wait(TICK_SECONDS)

    def _lump_seconds(self, chunk) -> float:
        """How much channel the next live-edge lump waits for: `chunk_seconds`, moved early or
        late by up to `spread_seconds`, fixed per lump so a connection's lumps are repeatable."""
        p = chunk.params
        if not p['spread_seconds']:
            return p['chunk_seconds']
        f = derive(self.timing.pts_offset, 'lump', self.first, self._lumps) / 2.0 ** 64
        return max(TICK_SECONDS, p['chunk_seconds'] + p['spread_seconds'] * (2 * f - 1))

    def _silence(self, stopping: threading.Event) -> None:
        """Send nothing and keep the connection open until the client leaves, the seat is
        taken away, the simulator stops or `hold_silence` passes."""
        end = time.monotonic() + self.hold_silence
        while time.monotonic() < end:
            if stopping.wait(TICK_SECONDS):
                self.ended = 'the simulator stopped'
                return
            if self.seat is not None and self.seat.is_cut():
                self.ended = self.seat.kicked_why
                return
            if self.client_gone is not None and self.client_gone():
                self.ended = f'silent after {self.sent} bytes; the client went away'
                return
        self.ended = f'silent after {self.sent} bytes for {self.hold_silence:g} s, then closed'

    def offered(self) -> tuple | None:
        """Channel time, in seconds since the epoch, of the first and the last packet sent."""
        if self.g == self.first:
            return None
        t = self.timing
        return (round(t.channel_seconds(self.loop.send_time(self.first)), 6),
                round(t.channel_seconds(self.loop.send_time(self.g - 1)), 6))

    def close(self) -> None:
        if self.seat is not None:
            self.seat.release()


class ClipConnection:
    """The placeholder: a finite clip, the same bytes on every request, sent at up to `speed`
    times real time and then ended, as a file is (dev/changelog/957). A client reading slower
    than that - a paced capture - simply reads slower; the socket holds the rest back.

    It is answered as a file too: with its length, and from `start_byte` when a request asks
    for a range. The real clip's ffprobe read a size, and the capture's stderr shows ffmpeg
    reconnecting at the clip's last byte twice, reading End of file each time, then exiting 0 -
    so a range at or past the end gets an empty answer (Headend._ts)."""

    def __init__(self, loop, seed: int, backend: str, clip_seconds: float, speed: float,
                 seat=None, start_byte: int = 0):
        self.loop = loop
        self.seat = seat
        self.speed = speed
        self.base90 = (derive(seed, 'placeholder-pts', backend) - loop.pcr0) % PTS_WRAP
        first = loop.cut_packet(0)
        self.s0 = loop.send_time(first)
        self.g_end = loop.packets_through(self.s0 + int(clip_seconds * CLOCK_HZ))
        self.length = (self.g_end - first) * TS
        self.g = self.first = first + start_byte // TS
        self._skip = start_byte % TS
        self._s_from = loop.send_time(self.g)
        self.t0 = time.monotonic_ns()
        self.ended = ''
        self.applied = []
        self.withheld = ()
        self.client_gone = None

    def pieces(self, stopping: threading.Event):
        loop = self.loop
        while True:
            if stopping.is_set():
                self.ended = 'the simulator stopped'
                return
            if self.seat is not None and self.seat.is_cut():
                self.ended = self.seat.kicked_why
                return
            if self.g >= self.g_end:
                self.ended = 'the placeholder clip ended'
                return
            sent_by = self._s_from + int(self.speed * (time.monotonic_ns() - self.t0) * 27
                                         // 1000)
            due = min(self.g_end, loop.packets_through(sent_by))
            if due > self.g:
                g1 = min(due, self.g + CHUNK_PACKETS)
                piece = loop.cut(self.g, g1, self.base90)
                if self._skip:
                    piece, self._skip = piece[self._skip:], 0
                self.g = g1
                yield piece
                if g1 < due:
                    continue
            stopping.wait(TICK_SECONDS)

    def offered(self):
        """The clip is not the channel's content, so it offers no channel time."""
        return None

    def content_seconds(self) -> float:
        return (self.loop.send_time(self.g) - self.s0) / CLOCK_HZ

    def close(self) -> None:
        if self.seat is not None:
            self.seat.release()


class Mp3Connection:
    """One client of a radio mount: whole frames, a small burst, then real time."""

    def __init__(self, loop, feed: Feed, clock: ChannelClock):
        self.loop = loop
        self.clock = clock
        self.off = feed.content_offset(loop.frames)
        self.f = self.first = max(0, self._due() - math.ceil(
            RADIO_BURST_BYTES * loop.frames / len(loop.data)))
        self.seat = None
        self.ended = ''
        self.applied = []
        self.withheld = ()
        self.client_gone = None

    def _due(self) -> int:
        return self.clock.now_ns() * self.loop.sample_rate // (SAMPLES_PER_FRAME * 10 ** 9) \
            + self.off

    def pieces(self, stopping: threading.Event):
        while True:
            if stopping.is_set():
                self.ended = 'the simulator stopped'
                return
            due = self._due()
            if due > self.f:
                f1 = min(due, self.f + 256)
                piece = self.loop.cut(self.f, f1)
                self.f = f1
                yield piece
                if f1 < due:
                    continue
            stopping.wait(TICK_SECONDS)

    def offered(self) -> tuple | None:
        if self.f == self.first:
            return None
        sec = SAMPLES_PER_FRAME / self.loop.sample_rate
        return (round((self.first - self.off) * sec, 6), round((self.f - 1 - self.off) * sec, 6))

    def close(self) -> None:
        pass


class HlsTiming:
    """A feed's segments: cut at keyframes, `m` keyframe intervals each, numbered across
    passes so the numbering never repeats."""

    def __init__(self, loop, feed: Feed, segment_seconds: float):
        self.loop = loop
        self.timing = TsTiming(loop, feed)
        self.m = max(1, round(segment_seconds * PTS_HZ / loop.gop90))

    def live(self, now_ns: int) -> int:
        """The newest segment whose last packet is due."""
        sent = self.loop.cuts_through(self.timing.send_time(now_ns))
        return (sent - 1) // self.m - 1

    def duration(self, j: int) -> float:
        lp, m = self.loop, self.m
        return (lp.cut_pts((j + 1) * m) - lp.cut_pts(j * m)) / PTS_HZ

    def segment(self, j: int, live: LiveFaults | None = None) -> bytes:
        lp, m = self.loop, self.m
        return shape(self.timing, lp.cut_packet(j * m), lp.cut_packet((j + 1) * m), live,
                     hls_cut=self.cut_at)

    def first_at(self, ns: int) -> int:
        """The first segment starting at or after channel time `ns`."""
        c = self.loop.cuts_through(self.timing.send_time(ns) - 1)
        return -(-c // self.m)

    def cut_at(self, ns: int) -> int:
        """The first packet of that segment."""
        return self.loop.cut_packet(self.first_at(ns) * self.m)

    def span(self, j: int) -> tuple:
        lp, m, t = self.loop, self.m, self.timing
        return (round(t.channel_seconds(lp.send_time(lp.cut_packet(j * m))), 6),
                round(t.channel_seconds(lp.send_time(lp.cut_packet((j + 1) * m) - 1)), 6))

    def playlist(self, now_ns: int, window: int, uri=None, marks=()) -> bytes:
        """The media playlist. With `uri` (segment number to its URI) it is an Xtream play
        URL's, in the tag order a real one was seen in; without, a third-party one's. Each
        segment number in `marks` is preceded by EXT-X-DISCONTINUITY."""
        last = self.live(now_ns)
        first = last - window + 1
        durations = [self.duration(j) for j in range(first, last + 1)]
        target = f'#EXT-X-TARGETDURATION:{math.ceil(max(durations))}'
        if uri is None:
            lines = ['#EXTM3U', '#EXT-X-VERSION:3', target, f'#EXT-X-MEDIA-SEQUENCE:{first}']
            uri = '{}.ts'.format
        else:
            lines = ['#EXTM3U', '#EXT-X-VERSION:3', f'#EXT-X-MEDIA-SEQUENCE:{first}',
                     '#EXT-X-ALLOW-CACHE:YES', target]
        for j, d in zip(range(first, last + 1), durations):
            if j in marks:
                lines.append('#EXT-X-DISCONTINUITY')
            lines += [f'#EXTINF:{d:.6f},', uri(j)]
        return ('\n'.join(lines) + '\n').encode('ascii')

    def serves(self, j: int, now_ns: int, window: int) -> bool:
        last = self.live(now_ns)
        return last - window - HLS_KEEP_BEHIND < j <= last


# --- seats --------------------------------------------------------------------------------------

class Seat:
    def __init__(self, seats, sid, login, kind):
        self.seats = seats
        self.id = sid
        self.login = login
        self.kind = kind
        self.opened = self.last_seen = time.monotonic()
        self.kicked = threading.Event()
        self.kicked_why = ''
        self.cut_at = 0.0
        self.released = False

    def touch(self) -> None:
        self.last_seen = time.monotonic()

    def is_cut(self) -> bool:
        """Kicked, and past the moment its connection is ended."""
        return self.kicked.is_set() and time.monotonic() >= self.cut_at

    def release(self) -> None:
        self.seats.release(self)

    def __repr__(self):
        return f'<seat {self.id} {self.login} {self.kind}{" released" if self.released else ""}>'


@dataclass
class Grant:
    seat: Seat | None
    refusal: tuple | None = None        # (status, note) when no seat is given
    notes: list = field(default_factory=list)


class Seats:
    """Connections per login at its backend, and what happens over the limit."""

    def __init__(self, status: dict):
        self._lock = threading.Lock()
        self._by_login: dict = {}
        self._over: dict = {}
        self._next = 1
        self.status = status            # the provider's login statuses, for a ban

    def alive(self, s: Seat, hls_idle: float) -> bool:
        return self._alive(s, time.monotonic(), hls_idle)

    def _alive(self, s: Seat, now: float, hls_idle: float) -> bool:
        if s.released or s.kicked.is_set():
            return False
        return s.kind == KIND_TS or now - s.last_seen <= hls_idle

    def _live(self, login: str, hls_idle: float) -> list:
        now = time.monotonic()
        seats = self._by_login.get(login, [])
        alive = [s for s in seats if self._alive(s, now, hls_idle)]
        for s in seats:
            if s not in alive:
                s.released = True
        self._by_login[login] = alive
        return alive

    def acquire(self, lg, backend, kind: str, hls_idle: float) -> Grant:
        policy = backend.over_limit
        with self._lock:
            live = self._live(lg.username, hls_idle)
            notes = []
            if len(live) >= lg.max_connections:
                if policy.policy == 'refuse':
                    return Grant(None, (policy.status, f'over the connection limit of '
                                        f'{lg.max_connections}: refused (assumed)'))
                if policy.policy == 'kick_oldest':
                    oldest = min(live, key=lambda s: s.opened)
                    self._kick(oldest, 'cut off: the login opened one connection too many',
                               policy.kick_delay_seconds)
                    notes.append(f'over the limit of {lg.max_connections}: the oldest '
                                 f'connection ends in {policy.kick_delay_seconds:g} s')
                else:
                    n = self._over[lg.username] = self._over.get(lg.username, 0) + 1
                    if n >= policy.ban_after:
                        self.status[lg.username] = 'Banned'
                        for s in live:
                            self._kick(s, 'cut off: the login was banned for going over its '
                                          'limit (assumed)')
                        return Grant(None, (policy.status, f'over the limit {n} times: the '
                                            'login is banned (assumed)'))
                    notes.append(f'over the limit of {lg.max_connections} ({n} of '
                                 f'{policy.ban_after} before a ban): allowed (assumed)')
            seat = Seat(self, self._next, lg.username, kind)
            self._next += 1
            self._by_login.setdefault(lg.username, []).append(seat)
            return Grant(seat, None, notes)

    def _kick(self, seat: Seat, why: str, delay: float = 0.0) -> None:
        """Take the seat now and end its connection `delay` seconds from now."""
        seat.kicked_why = why
        seat.cut_at = time.monotonic() + delay
        seat.kicked.set()
        seat.released = True

    def release(self, seat: Seat) -> None:
        with self._lock:
            seat.released = True
            seats = self._by_login.get(seat.login, [])
            if seat in seats:
                seats.remove(seat)

    def count(self, login: str, hls_idle: float) -> int:
        with self._lock:
            return len(self._live(login, hls_idle))

    def cut(self, login: str, why: str) -> int:
        """End every connection the login holds now, as a provider dropping it does; returns
        how many."""
        with self._lock:
            seats = self._by_login.pop(login, [])
            for s in seats:
                self._kick(s, why)
        return len(seats)

    def kick_all(self, why: str) -> None:
        with self._lock:
            for seats in self._by_login.values():
                for s in seats:
                    self._kick(s, why)
            self._by_login.clear()


# --- the headend --------------------------------------------------------------------------------

@dataclass
class Target:
    """What a stream request asks for, before anything is checked."""
    kind: str           # ts, hls, play, segment, radio, tp_master, tp_media, tp_segment, vod
    backend: str
    username: str | None = None
    password: str | None = None
    sid: int | None = None
    slug: str | None = None
    token: str | None = None
    seq: int | None = None
    variant: str | None = None


@dataclass
class StreamAnswer:
    status: int
    body: bytes = b''
    content_type: str = 'text/plain; charset=utf-8'
    headers: dict = field(default_factory=dict)
    stream: object = None       # a connection with pieces(), offered() and close()
    note: str = ''
    login: str | None = None
    channel: str | None = None
    offered: tuple | None = None
    profile: str | None = None  # the loop the stream is cut from
    delay: float = 0.0          # seconds the server waits before the first byte
    hold: float | None = None   # accept, never answer, close after this many seconds at most
    faults: list = field(default_factory=list)  # stream-layer faults this answer applied
    withheld: tuple = ()        # (fault name, first, last) channel seconds sent without video


@dataclass
class _Play:
    """What a play token was minted for: one login, one channel, MPEG-TS or HLS. An HLS
    token also carries its playlist timing and the seat its client holds."""
    token: str
    kind: str
    login: str
    backend: str
    sid: int
    channel: str
    feed: Feed
    hls: HlsTiming | None = None
    seat: Seat | None = None


class Headend:
    """Every backend's channels, served to whoever asks with the right credentials."""

    def __init__(self, world, library, clock: ChannelClock, status: dict, faults, origin=None):
        """`origin` maps a host name to the origin a redirect names; without one a redirect
        names a path on the host that was asked."""
        self.world = world
        self.library = library
        self.clock = clock
        self.status = status
        self.faults = faults
        self.origin = origin
        self.seats = Seats(status)
        self._plays: dict = {}
        self._play_lock = threading.Lock()
        self._next_play = 1
        self._next_edge: dict = {}
        self._by_id = {b: {c.stream_id: c for c in lu.channels} for b, lu in world.lineups.items()}
        self._by_slug = {b: {c.slug: c for c in lu.channels if c.slug}
                         for b, lu in world.lineups.items()}
        self._host_backend = {}
        for b in world.backends.values():
            for h in b.stream_host_names():
                self._host_backend[h] = b.name
            self._host_backend[b.thirdparty_host] = b.name
        for p in world.panels.values():
            self._host_backend[p.name] = p.backend

    # --- what a request asks for --------------------------------------------------------------

    def target(self, host: str, path: str) -> Target | None:
        backend = self._host_backend.get(host)
        if backend is None:
            return None
        if host == self.world.backends[backend].thirdparty_host:
            m = _RADIO_RX.match(path)
            if m:
                return Target('radio', backend, slug=m['slug'])
            m = _TP_HLS_RX.match(path)
            if m:
                if m['variant'] is None:
                    return Target('tp_master', backend, slug=m['slug'])
                if m['seq'] is None:
                    return Target('tp_media', backend, slug=m['slug'], variant=m['variant'])
                return Target('tp_segment', backend, slug=m['slug'], variant=m['variant'],
                              seq=int(m['seq']))
            return Target('vod' if _VOD_RX.match(path) else 'unknown', backend)
        m = _PLAY_RX.match(path)
        if m:
            return Target('play', backend, token=m['token'], sid=int(m['sid']))
        m = _SEGMENT_RX.match(path)
        if m:
            return Target('segment', backend, token=m['token'], sid=int(m['sid']),
                          seq=int(m['seq']))
        m = _CRED_RX.match(path)
        if m:
            return Target(KIND_HLS if m['ext'] == 'm3u8' else KIND_TS, backend,
                          m['user'], m['password'], int(m['sid']))
        return Target('vod' if _VOD_RX.match(path) else 'unknown', backend)

    def describe(self, host: str, path: str) -> dict | None:
        """What a stream request is, as the fault layers see it: its login (a LoginSpec or
        None), channel label, backend, stage (serve, or redirect for a 302 to a play URL) and
        form (ts, hls_playlist, hls_segment, hls_master or radio)."""
        t = self.target(host, path)
        if t is None:
            return None
        out = {'login': None, 'channel': None, 'backend': t.backend, 'stage': STAGE_SERVE,
               'form': None}
        if t.kind in ('play', 'segment'):
            p = self._play(t)
            if p is not None:
                out.update(login=self.world.logins.get(p.login), channel=p.channel,
                           form=FORM_TS if p.kind == KIND_TS else (
                               FORM_HLS_PLAYLIST if t.kind == 'play' else FORM_HLS_SEGMENT))
        elif t.kind in (KIND_TS, KIND_HLS):
            ch = self._channel(t.backend, t.sid)
            redirect = t.kind == KIND_HLS or self.world.backends[t.backend].stream_redirect
            out.update(login=self.login_of(t),
                       channel=channel_label(t.backend, ch) if ch is not None else None,
                       stage=STAGE_REDIRECT if redirect else STAGE_SERVE,
                       form=FORM_TS if t.kind == KIND_TS else FORM_HLS_PLAYLIST)
        elif t.kind == 'radio' or t.kind.startswith('tp_'):
            ch = self._by_slug[t.backend].get(t.slug)
            if ch is not None:
                out['channel'] = channel_label(t.backend, ch)
            out['form'] = {'radio': FORM_RADIO, 'tp_master': FORM_HLS_MASTER,
                           'tp_media': FORM_HLS_PLAYLIST,
                           'tp_segment': FORM_HLS_SEGMENT}[t.kind]
        return out

    def login_of(self, t: Target):
        """The login a stream request's credentials name on its backend, or None. Credentials
        a `url_drift` fault put in the panel's URLs name that panel's first login."""
        if t.kind in ('play', 'segment'):
            p = self._play(t)
            return self.world.logins.get(p.login) if p else None
        if t.username is None:
            return None
        lg = self.world.logins.get(t.username)
        if lg is not None and lg.password == t.password and lg.backend == t.backend:
            return lg
        for a in self.faults.active():
            if a.kind.name != 'url_drift' or a.params.get('to') != 'credentials':
                continue
            panel = self.world.panels.get(a.target)
            if panel is None or panel.backend != t.backend:
                continue
            if drifted_credentials(self.world.seed, panel.name) == (t.username, t.password):
                on = self.world.logins_on(panel.name)
                return on[0] if on else None
        return None

    def _channel(self, backend: str, sid: int):
        ch = self._by_id[backend].get(sid)
        if ch is not None:
            return ch
        # A cuid_url_mismatch fault lists a URL whose id is the stream id plus its offset.
        for a in self.faults.active():
            if a.kind.name != 'cuid_url_mismatch':
                continue
            panel = self.world.panels.get(a.target)
            if panel is not None and panel.backend == backend:
                ch = self._by_id[backend].get(sid - a.params['offset'])
                if ch is not None:
                    return ch
        return None

    def profile_for(self, backend: str, ch) -> str:
        if ch.kind == KIND_RADIO:
            return RADIO_PROFILE
        content = self.world.backends[backend].content
        return profile_for_format(ch.format) if content == CONTENT_CALIBRATED else content

    def timing(self, label: str, profile: str) -> TsTiming:
        """Where a channel's clock sits on a loop - what turns a frame of `profile`, as this
        channel served it, back into the channel time it was sent at (sim/answer_key.py).
        `label` is the ledger's name for the channel; KeyError if no channel has it."""
        backend, _, key = label.partition(':')
        if backend not in self._by_id:
            raise KeyError(label)
        kind, _, slug = key.partition('/')
        ch = self._by_slug[backend].get(slug) if slug else (
            self._channel(backend, int(key)) if key.isdigit() else None)
        if ch is None:
            raise KeyError(label)
        return TsTiming(self.library.load(profile), self._feed(backend, ch, profile))

    def _feed(self, backend, ch, profile=None) -> Feed:
        key = str(ch.stream_id) if ch.kind not in (KIND_RADIO, KIND_NO_ID) else \
            f'{ch.kind}/{ch.slug}'
        return Feed(backend, key, profile or self.profile_for(backend, ch), self.world.seed)

    def hls_idle(self, backend: str) -> float:
        return self.world.backends[backend].hls_idle_seconds

    # --- answering ----------------------------------------------------------------------------

    def answer(self, host: str, method: str, path: str, lg, status: str | None,
               taken=(), byte_range: str | None = None) -> StreamAnswer:
        """`taken` is the stream faults this request took (sim/faults.py); the channel's
        stream-layer faults are read by the connection itself as it plays. `byte_range` is the
        request's Range header, which a live stream ignores (DESIGN-simulator.md §3.10) and the
        placeholder clip honors."""
        t = self.target(host, path)
        if t is None:
            return StreamAnswer(404, note='not a stream host')
        plan = plan_open(taken)
        m = _RANGE_RX.match(byte_range or '')
        plan.range_start = int(m['start']) if m else 0
        try:
            a = self._answer(t, method, lg, status, plan)
        except ContentError as exc:
            return StreamAnswer(503, note=f'content missing: {exc}')
        if plan.delay and a.hold is None:
            a.delay = plan.delay
        return a

    def _gate(self, plan: OpenPlan, login, label) -> StreamAnswer | None:
        """The answer when an open fault keeps the request from being served at all."""
        g = plan.gate()
        if g is None:
            return None
        status, body, note = g
        if status == 0:
            return StreamAnswer(0, note=note, login=login, channel=label, hold=plan.hold)
        return StreamAnswer(status, body, 'text/html', note=note, login=login, channel=label)

    def _answer(self, t: Target, method: str, lg, status, plan: OpenPlan) -> StreamAnswer:
        spec = self.world.backends[t.backend]
        if t.kind == 'vod':
            return StreamAnswer(404, note='VOD is not served')
        if t.kind == 'unknown':
            return StreamAnswer(404)
        if t.kind == 'radio':
            ch = self._by_slug[t.backend].get(t.slug)
            if ch is None or ch.kind != KIND_RADIO:
                return StreamAnswer(404, note='no such radio mount')
            return self._gate(plan, None, channel_label(t.backend, ch)) or \
                self._radio(t, ch, method)
        if t.kind.startswith('tp_'):
            ch = self._by_slug[t.backend].get(t.slug)
            if ch is None or ch.kind != KIND_NO_ID:
                return StreamAnswer(404, note='no such channel')
            return self._gate(plan, None, channel_label(t.backend, ch)) or \
                self._third_party_hls(t, ch, spec)
        if t.kind in ('play', 'segment'):
            return self._from_token(t, method, lg, status, spec, plan)
        who = lg.username if lg else None
        if lg is None:
            return StreamAnswer(401, note='unknown credentials (assumed)')
        if status != 'Active':
            return StreamAnswer(403, note=f'login {status} (assumed)', login=who)
        ch = self._channel(t.backend, t.sid)
        if ch is None:
            return StreamAnswer(404, note='no such stream id', login=who)
        feed = self._feed(t.backend, ch)
        label = channel_label(t.backend, ch)
        if t.kind == KIND_HLS or spec.stream_redirect:
            return self._redirect(t, lg, ch, feed, label, spec)
        gated = self._gate(plan, who, label)
        if gated is not None:
            return gated
        if method == 'HEAD':
            self.library.load(self._swapped(feed, plan).profile)
            return StreamAnswer(200, content_type=TS_TYPE, login=who, channel=label)
        return self._ts(lg, feed, label, spec, plan)

    def _swapped(self, feed: Feed, plan: OpenPlan) -> Feed:
        """The channel's feed, on another loop when a format, layout, picture or bitrate fault
        says so. The channel clock and timestamp base stay the channel's own, so a reconnect
        lands where it would have on the old loop."""
        if plan.profile is None or plan.profile == feed.profile:
            return feed
        return Feed(feed.backend, feed.key, plan.profile, feed.seed)

    def _ts(self, lg, feed, label, spec, plan: OpenPlan) -> StreamAnswer:
        if plan.placeholder is not None:
            return self._clip(lg, label, spec, plan)
        feed = self._swapped(feed, plan)
        loop = self.library.load(feed.profile)
        grant = self.seats.acquire(lg, spec, KIND_TS, self.hls_idle(spec.name))
        if grant.seat is None:
            return StreamAnswer(grant.refusal[0], note=grant.refusal[1], login=lg.username,
                                channel=label)
        conn = TsConnection(loop, feed, self.clock, spec.burst_seconds, grant.seat,
                            live=LiveFaults(self.faults, label, spec.name), plan=plan)
        return StreamAnswer(200, content_type=TS_TYPE, stream=conn, login=lg.username,
                            channel=label, note='; '.join(grant.notes), profile=feed.profile)

    def _clip(self, lg, label, spec, plan: OpenPlan) -> StreamAnswer:
        """The placeholder, answered as a file: its length, and the rest of it from a range."""
        p = plan.placeholder.params
        loop = self.library.load(p['profile'])
        probe = ClipConnection(loop, self.world.seed, spec.name, p['clip_seconds'], p['speed'])
        start = plan.range_start
        if start >= probe.length:
            # assumed: what the real provider answered past the end is unseen; an empty body
            # is what gives the End of file ffmpeg logged at each reconnect there.
            return StreamAnswer(200, b'', TS_TYPE, headers={'Accept-Ranges': 'bytes'},
                                login=lg.username, channel=label,
                                note=f'placeholder: a range from byte {start}, at or past the '
                                     'end of the clip, answered empty')
        grant = self.seats.acquire(lg, spec, KIND_TS, self.hls_idle(spec.name))
        if grant.seat is None:
            return StreamAnswer(grant.refusal[0], note=grant.refusal[1], login=lg.username,
                                channel=label)
        conn = ClipConnection(loop, self.world.seed, spec.name, p['clip_seconds'], p['speed'],
                              grant.seat, start_byte=start)
        headers = {'Accept-Ranges': 'bytes', 'Content-Length': str(probe.length - start)}
        if start:
            headers['Content-Range'] = f'bytes {start}-{probe.length - 1}/{probe.length}'
        return StreamAnswer(206 if start else 200, content_type=TS_TYPE, headers=headers,
                            stream=conn, login=lg.username, channel=label,
                            note='; '.join(['the placeholder clip'] + grant.notes),
                            profile=p['profile'])

    def _redirect(self, t: Target, lg, ch, feed, label, spec) -> StreamAnswer:
        """A play token for this login and channel, and a 302 to its URL on the backend's next
        stream host. Nothing is held until the play URL is fetched."""
        hosts = spec.stream_host_names()
        with self._play_lock:
            n = self._next_play
            self._next_play += 1
            edge = hosts[self._next_edge.get(spec.name, 0) % len(hosts)]
            self._next_edge[spec.name] = self._next_edge.get(spec.name, 0) + 1
            token = f'{derive(self.world.seed, "play-token", n):016x}'
            self._plays[token] = _Play(token, t.kind, lg.username, t.backend, ch.stream_id,
                                       label, feed)
            while len(self._plays) > PLAY_TOKENS_KEPT:
                del self._plays[next(iter(self._plays))]
        path = f'/live/play/{token}/{ch.stream_id}'
        where = self.origin(edge) + path if self.origin else path
        return StreamAnswer(302, headers={'Location': where}, login=lg.username, channel=label,
                            note=f'redirected to a play URL on {edge}')

    def _play(self, t: Target):
        with self._play_lock:
            p = self._plays.get(t.token)
        return p if p is not None and p.backend == t.backend and p.sid == t.sid else None

    def _from_token(self, t: Target, method: str, lg, status, spec, plan) -> StreamAnswer:
        p = self._play(t)
        if p is None or lg is None:
            return StreamAnswer(404, note='no such play token')
        if status != 'Active':
            return StreamAnswer(403, note=f'login {status} (assumed)', login=p.login,
                                channel=p.channel)
        gated = self._gate(plan, p.login, p.channel)
        if gated is not None:
            return gated
        if p.kind == KIND_TS:
            if t.kind != 'play':
                return StreamAnswer(404, note='an MPEG-TS play token has no segments',
                                    login=p.login, channel=p.channel)
            if method == 'HEAD':
                self.library.load(self._swapped(p.feed, plan).profile)
                return StreamAnswer(200, content_type=TS_TYPE, login=p.login, channel=p.channel)
            return self._ts(lg, p.feed, p.channel, spec, plan)
        with self._play_lock:
            hls = p.hls
        # A play token's playlist timing is fixed when its client first fetches it: a format
        # fault reaches an HLS client the way it reaches an MPEG-TS one, on its next connection.
        feed = p.feed if hls is not None else self._swapped(p.feed, plan)
        loop = self.library.load(feed.profile) if hls is None else hls.loop
        if method == 'HEAD':
            return StreamAnswer(200, content_type=XTREAM_HLS_TYPE, login=p.login,
                                channel=p.channel)
        notes = []
        with self._play_lock:
            seat = p.seat
        if seat is None or seat.is_cut() or (not seat.kicked.is_set()
                                             and not self.seats.alive(seat, self.hls_idle(
                                                 spec.name))):
            # A client fetching again after its seat lapsed or was cut off takes a new one.
            grant = self.seats.acquire(lg, spec, KIND_HLS, self.hls_idle(spec.name))
            if grant.seat is None:
                return StreamAnswer(grant.refusal[0], note=grant.refusal[1], login=p.login,
                                    channel=p.channel)
            notes = grant.notes
            with self._play_lock:
                p.seat = grant.seat
                if p.hls is None:
                    p.hls = HlsTiming(loop, feed, spec.hls_segment_seconds)
        else:
            seat.touch()
        a = self._hls_body(p.hls, t.seq, spec, p.login, p.channel,
                           uri=lambda j: f'/hls/{p.token}/{p.sid}_{j}.ts')
        if notes:
            a.note = '; '.join(([a.note] if a.note else []) + notes)
        return a

    def _hls_body(self, hls: HlsTiming, seq, spec, login, label, uri=None) -> StreamAnswer:
        now = self.clock.now_ns()
        live = LiveFaults(self.faults, label, spec.name).refresh()
        if seq is None:
            at = now
            for a in live.of('hls_stale'):
                if live.holding(a, now):
                    at = min(at, self._stale_from(a, label, now))
                    live.mark(a)
            marks = set()
            for a in live.of('hls_discontinuity'):
                marks.add(hls.first_at(epoch_ns(a.trigger.start)))
                live.mark(a)
            return StreamAnswer(200, hls.playlist(at, spec.hls_window, uri, marks),
                                HLS_TYPE if uri is None else XTREAM_HLS_TYPE, login=login,
                                channel=label, faults=live.applied)
        if not hls.serves(seq, now, spec.hls_window):
            return StreamAnswer(404, note='segment outside the window', login=login,
                                channel=label)
        start_ns = round(hls.span(seq)[0] * 1e9)
        for a in live.of('hls_missing_segment'):
            if live.holding(a, start_ns):
                live.mark(a)
                return StreamAnswer(404, note='segment withheld by hls_missing_segment',
                                    login=login, channel=label, faults=live.applied)
        body = hls.segment(seq, live)
        return StreamAnswer(200, body, TS_TYPE, login=login, channel=label,
                            offered=hls.span(seq), faults=live.applied, profile=hls.loop.name,
                            withheld=live.spans(hls.timing))

    def _stale_from(self, a, label, now_ns) -> int:
        """When an hls_stale fault froze this channel's playlist: its start, or the first
        time a playlist was asked for while it held."""
        if a.trigger.start is not None:
            return epoch_ns(a.trigger.start)
        with a.lock:
            return a.state.setdefault(('stale', label), now_ns)

    def _third_party_hls(self, t: Target, ch, spec) -> StreamAnswer:
        own = self.profile_for(t.backend, ch)
        variants = [own] + ([spec.hls_variant] if spec.hls_variant not in (None, own) else [])
        label = channel_label(t.backend, ch)
        if t.kind == 'tp_master':
            lines = ['#EXTM3U', '#EXT-X-VERSION:3']
            for i, name in enumerate(variants):
                p = PROFILES[name]
                lines += [f'#EXT-X-STREAM-INF:BANDWIDTH={p.kbps * 1100},'
                          f'RESOLUTION={p.width}x{p.height}', f'v{i}/index.m3u8']
            return StreamAnswer(200, ('\n'.join(lines) + '\n').encode('ascii'), HLS_TYPE,
                                channel=label)
        i = int(t.variant[1:])
        if i >= len(variants):
            return StreamAnswer(404, note='no such variant', channel=label)
        loop = self.library.load(variants[i])
        hls = HlsTiming(loop, self._feed(t.backend, ch, variants[i]), spec.hls_segment_seconds)
        return self._hls_body(hls, t.seq, spec, None, label)

    def _radio(self, t: Target, ch, method) -> StreamAnswer:
        loop = self.library.load(RADIO_PROFILE)
        label = channel_label(t.backend, ch)
        if method == 'HEAD':
            return StreamAnswer(200, content_type=MP3_TYPE, channel=label)
        return StreamAnswer(200, content_type=MP3_TYPE, channel=label,
                            stream=Mp3Connection(loop, self._feed(t.backend, ch), self.clock))

    # --- state a test may read ----------------------------------------------------------------

    def cut(self, username: str, why: str) -> int:
        """End every stream connection a login holds; the ledger row of each names `why`."""
        return self.seats.cut(username, why)

    def connections(self, username: str) -> int:
        lg = self.world.logins.get(username)
        if lg is None:
            return 0
        return self.seats.count(username, self.hls_idle(lg.backend))

    def profiles_in_use(self) -> list[str]:
        """Every loop some channel of this world would serve."""
        out = []
        for b, lu in self.world.lineups.items():
            spec = self.world.backends[b]
            for ch in lu.channels:
                out.append(self.profile_for(b, ch))
                if ch.kind == KIND_NO_ID and spec.hls_variant:
                    out.append(spec.hls_variant)
        return list(dict.fromkeys(out))

    def stop(self) -> None:
        self.seats.kick_all('the simulator stopped')
        with self._play_lock:
            self._plays.clear()


__all__ = ['ChannelClock', 'ClipConnection', 'Feed', 'Headend', 'HlsTiming', 'Mp3Connection',
           'Seats', 'StreamAnswer', 'TsConnection', 'epoch_ns', 'is_stream_path']
