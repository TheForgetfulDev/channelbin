"""MPEG-TS as the stream engine needs it: one rendered loop, read once and cut forever.

A loop is a rendered MPEG-TS file whose last frame is followed seamlessly by its first
(sim/content.py renders them that way). `Loop` indexes it once - each packet's send time from
the PCR, every PCR, PTS and DTS field, the continuity counters, and the cut point before each
video keyframe - and can then cut any stretch of an endless stream out of it: pass `k` of the
loop with its timestamps moved on by `k` loop lengths and its continuity counters carried on
from pass `k - 1`. Packets are numbered across passes (`g = k * packets + i`), so a stretch of
stream is a range of packet numbers and the same range always gives the same bytes.

`check()` reads a stretch back and reports what a seamless stream must not have: lost sync, a
continuity-counter jump, a PCR gap over the 100 ms the standard allows, a decode timestamp
running backwards.
"""
import bisect
from array import array
from dataclasses import dataclass, field

TS = 188
SYNC = 0x47
PTS_HZ = 90_000
CLOCK_HZ = 27_000_000
PTS_WRAP = 1 << 33
# ISO/IEC 13818-1 2.7.2: a PCR at least every 100 ms.
PCR_MAX_GAP_SECONDS = 0.1

VIDEO_TYPES = {0x01: 'mpeg1video', 0x02: 'mpeg2video', 0x1B: 'h264', 0x24: 'hevc'}
AUDIO_TYPES = {0x03: 'mp2', 0x04: 'mp2', 0x0F: 'aac', 0x11: 'aac_latm', 0x81: 'ac3',
               0x87: 'eac3'}


class TsError(ValueError):
    """Bytes that are not the MPEG-TS a loop has to be."""


def _read_ts(b, o) -> int:
    return (((b[o] >> 1) & 0x07) << 30) | (b[o + 1] << 22) | ((b[o + 2] >> 1) << 15) | \
        (b[o + 3] << 7) | (b[o + 4] >> 1)


def _ts_bytes(prefix: int, v: int) -> bytes:
    """A 5-byte PTS or DTS field; `prefix` keeps the field's own marker nibble."""
    return bytes(((prefix & 0xF0) | (((v >> 30) & 0x07) << 1) | 1,
                  (v >> 22) & 0xFF,
                  (((v >> 15) & 0x7F) << 1) | 1,
                  (v >> 7) & 0xFF,
                  ((v & 0x7F) << 1) | 1))


def _pcr_at(b, q) -> tuple[int, int]:
    base = (b[q] << 25) | (b[q + 1] << 17) | (b[q + 2] << 9) | (b[q + 3] << 1) | (b[q + 4] >> 7)
    return base, ((b[q + 4] & 1) << 8) | b[q + 5]


def _section(data, o):
    """The PSI section starting in the packet at `o` (pointer field honored), or None."""
    afc = (data[o + 3] >> 4) & 3
    p = o + 4
    if afc & 2:
        p += 1 + data[p]
    if not afc & 1 or p >= o + TS:
        return None
    p += 1 + data[p]
    if p + 3 > o + TS:
        return None
    length = ((data[p + 1] & 0x0F) << 8) | data[p + 2]
    end = min(o + TS, p + 3 + length)
    return data[p], data[p + 3:end]


def programs(data) -> tuple[dict, int | None, int | None]:
    """({pid: stream_type}, pcr_pid, pmt_pid) from the first PAT and PMT in `data`."""
    pmt_pid = None
    for o in range(0, len(data) - TS + 1, TS):
        pid = ((data[o + 1] & 0x1F) << 8) | data[o + 2]
        if pid == 0 and pmt_pid is None and data[o + 1] & 0x40:
            sec = _section(data, o)
            if sec and sec[0] == 0x00:
                body = sec[1]
                for e in range(5, len(body) - 4, 4):
                    number = (body[e] << 8) | body[e + 1]
                    if number:
                        pmt_pid = ((body[e + 2] & 0x1F) << 8) | body[e + 3]
                        break
        elif pmt_pid is not None and pid == pmt_pid and data[o + 1] & 0x40:
            sec = _section(data, o)
            if not sec or sec[0] != 0x02:
                continue
            body = sec[1]
            pcr_pid = ((body[5] & 0x1F) << 8) | body[6]
            info = ((body[7] & 0x0F) << 8) | body[8]
            e = 9 + info
            streams = {}
            while e + 5 <= len(body) - 4:
                stype = body[e]
                epid = ((body[e + 1] & 0x1F) << 8) | body[e + 2]
                es_info = ((body[e + 3] & 0x0F) << 8) | body[e + 4]
                streams[epid] = stype
                e += 5 + es_info
            return streams, pcr_pid, pmt_pid
    return {}, None, pmt_pid


class Loop:
    """One rendered loop, indexed once and shared by every connection that serves it.

    `length90` is the loop's length in 90 kHz ticks as recorded when it was rendered. It is
    never read back from the file: at 59.94 fps frames alternate 1,501 and 1,502 ticks, so a
    length inferred from PES timestamps comes out a tick long and drifts a frame a day.
    """

    def __init__(self, data: bytes, length90: int, name: str = ''):
        if len(data) < TS or len(data) % TS:
            raise TsError(f'{name or "loop"}: {len(data)} bytes is not a whole number of packets')
        self.name = name
        self.L90 = int(length90)
        self.L27 = self.L90 * 300
        n = len(data) // TS
        self.n = n
        buf = bytearray(data)
        streams, pcr_pid, pmt_pid = programs(buf)
        video = [p for p, t in streams.items() if t in VIDEO_TYPES]
        if not video:
            raise TsError(f'{name or "loop"}: no video stream in its PMT')
        self.streams = streams
        self.video_pid = video[0]
        self.pmt_pid = pmt_pid
        cc_count = {}
        pcr_sites = []      # (packet, byte offset of the 6 PCR bytes, base, extension)
        ts_sites = []       # (packet, byte offset of a 5-byte PTS or DTS field, value)
        keyframes = []      # (packet, PTS) of each video PES that starts a keyframe
        last_cc_pid = {}
        for i in range(n):
            o = i * TS
            if buf[o] != SYNC:
                raise TsError(f'{name or "loop"}: lost sync at packet {i}')
            pid = ((buf[o + 1] & 0x1F) << 8) | buf[o + 2]
            pusi = buf[o + 1] & 0x40
            afc = (buf[o + 3] >> 4) & 3
            p = o + 4
            rai = False
            if afc & 2:
                af_len = buf[p]
                if af_len:
                    flags = buf[p + 1]
                    rai = bool(flags & 0x40)
                    if flags & 0x10:
                        base, ext = _pcr_at(buf, p + 2)
                        pcr_sites.append((i, p + 2, base, ext))
                p += 1 + af_len
            if afc & 1:
                # Renumber so each PID counts 0, 1, 2 ... across the loop; pass k then adds
                # k times the PID's payload packets per pass, mod 16.
                c = cc_count.get(pid, 0)
                buf[o + 3] = (buf[o + 3] & 0xF0) | (c & 0x0F)
                cc_count[pid] = c + 1
                last_cc_pid[pid] = c
                if pusi and buf[p:p + 3] == b'\x00\x00\x01' and p + 14 <= o + TS:
                    pts_flags = buf[p + 7] >> 6
                    pts = None
                    if pts_flags & 2:
                        pts = _read_ts(buf, p + 9)
                        ts_sites.append((i, p + 9, pts))
                    if pts_flags == 3:
                        ts_sites.append((i, p + 14, _read_ts(buf, p + 14)))
                    if pid == self.video_pid and rai and pts is not None:
                        keyframes.append((i, pts))
            else:
                buf[o + 3] = (buf[o + 3] & 0xF0) | ((cc_count.get(pid, 0) - 1) & 0x0F)
        if len(pcr_sites) < 2:
            raise TsError(f'{name or "loop"}: fewer than two PCRs')
        if not keyframes or keyframes[0][0] > pcr_sites[-1][0]:
            raise TsError(f'{name or "loop"}: no video keyframe')
        self.data = bytes(buf)
        self.cc_count = cc_count
        self.pcr_sites = pcr_sites
        self.pcr_idx = [s[0] for s in pcr_sites]
        self.ts_sites = ts_sites
        self.ts_idx = [s[0] for s in ts_sites]
        first_i, _, b0, e0 = pcr_sites[0]
        self.pcr0 = b0                  # the first PCR's base, 90 kHz
        p0 = b0 * 300 + e0
        # Each packet's send time, 27 MHz from the first PCR: exact at a PCR, interpolated by
        # packet count between two, and the next pass's first PCR is the loop length on.
        anchors = [(i, b * 300 + e - p0) for i, _, b, e in pcr_sites]
        anchors.append((n + first_i, self.L27))
        rel = [0] * n
        a = 0
        for i in range(n):
            while a + 1 < len(anchors) - 1 and anchors[a + 1][0] <= i:
                a += 1
            (ia, ta), (ib, tb) = anchors[a], anchors[a + 1]
            rel[i] = ta + (i - ia) * (tb - ta) // (ib - ia)
        # The packets ahead of the first PCR are extrapolated; keep them after the previous
        # pass's last packet so send times rise across the seam.
        for i in range(first_i):
            rel[i] = max(rel[i], rel[n - 1] - self.L27)
        self.rel = rel
        # A cut point is where an HLS segment may start: the PAT and PMT the muxer writes just
        # ahead of each keyframe, or the keyframe itself if they are not there.
        si = {pmt_pid, 0}
        cuts = []
        for i, pts in keyframes:
            j = i
            while j > 0 and i - j < 4 and (((buf[(j - 1) * TS + 1] & 0x1F) << 8)
                                           | buf[(j - 1) * TS + 2]) in si:
                j -= 1
            cuts.append((j, pts))
        self.cuts = cuts
        self.cut_rel = [rel[j] for j, _ in cuts]
        # Byte 3 of every packet rewritten for each of the 16 possible pass offsets, so a
        # chunk's continuity counters are one strided slice assignment.
        pids = [((self.data[i * TS + 1] & 0x1F) << 8) | self.data[i * TS + 2] for i in range(n)]
        self.pids = array('H', pids)
        col = self.data[3::TS]
        self.cc_cols = []
        for r in range(16):
            out = bytearray(col)
            if r:
                for i in range(n):
                    add = (r * cc_count.get(pids[i], 0)) & 0x0F
                    if add:
                        out[i] = (out[i] & 0xF0) | ((out[i] + add) & 0x0F)
            self.cc_cols.append(bytes(out))

    @property
    def gop90(self) -> int:
        return self.L90 // len(self.cuts)

    # --- packet numbers and send times ------------------------------------------------------

    def send_time(self, g: int) -> int:
        """Packet g's send time, 27 MHz from pass 0's first PCR."""
        k, i = divmod(g, self.n)
        return k * self.L27 + self.rel[i]

    def packets_through(self, s: int) -> int:
        """How many packets, counted from packet 0 of pass 0, are due by send time `s`."""
        k, r = divmod(s, self.L27)
        j = bisect.bisect_right(self.rel, r)
        if j == self.n:
            j += bisect.bisect_right(self.rel, r - self.L27)
        return k * self.n + j

    def cut_packet(self, c: int) -> int:
        """Global packet number of global cut point c (cuts counted across passes)."""
        k, j = divmod(c, len(self.cuts))
        return k * self.n + self.cuts[j][0]

    def cut_pts(self, c: int) -> int:
        """Unwrapped PTS of the keyframe at global cut point c, before any channel shift."""
        k, j = divmod(c, len(self.cuts))
        return k * self.L90 + self.cuts[j][1]

    def cuts_through(self, s: int) -> int:
        """How many global cut points have been sent by send time `s`."""
        k, r = divmod(s, self.L27)
        return k * len(self.cuts) + bisect.bisect_right(self.cut_rel, r)

    # --- bytes ------------------------------------------------------------------------------

    def cut(self, g0: int, g1: int, base90: int) -> bytes:
        """Packets g0 to g1 (exclusive), every timestamp moved on by base90 + k * L90 in the
        pass k each packet belongs to, wrapped at 33 bits."""
        out = []
        n = self.n
        while g0 < g1:
            k, i0 = divmod(g0, n)
            i1 = min(n, i0 + (g1 - g0))
            out.append(self._slice(k, i0, i1, base90))
            g0 += i1 - i0
        return b''.join(out)

    def _slice(self, k, i0, i1, base90) -> bytes:
        buf = bytearray(self.data[i0 * TS:i1 * TS])
        buf[3::TS] = self.cc_cols[k & 0x0F][i0:i1]
        shift = base90 + k * self.L90
        off = i0 * TS
        for j in range(bisect.bisect_left(self.pcr_idx, i0), bisect.bisect_left(self.pcr_idx, i1)):
            _, q, base, ext = self.pcr_sites[j]
            nb = (base + shift) % PTS_WRAP
            q -= off
            buf[q] = (nb >> 25) & 0xFF
            buf[q + 1] = (nb >> 17) & 0xFF
            buf[q + 2] = (nb >> 9) & 0xFF
            buf[q + 3] = (nb >> 1) & 0xFF
            buf[q + 4] = ((nb & 1) << 7) | 0x7E | ((ext >> 8) & 1)
            buf[q + 5] = ext & 0xFF
        for j in range(bisect.bisect_left(self.ts_idx, i0), bisect.bisect_left(self.ts_idx, i1)):
            _, q, v = self.ts_sites[j]
            q -= off
            buf[q:q + 5] = _ts_bytes(buf[q], (v + shift) % PTS_WRAP)
        return bytes(buf)


# --- reading a stream back ------------------------------------------------------------------

@dataclass
class Report:
    packets: int = 0
    sync_errors: int = 0
    cc_errors: dict = field(default_factory=dict)       # pid -> count
    pcr_count: int = 0
    first_pcr: int | None = None                        # 27 MHz
    pcr_max_gap: float = 0.0                            # seconds
    pcr_backwards: int = 0
    dts_backwards: dict = field(default_factory=dict)   # pid -> count
    dts_steps: dict = field(default_factory=dict)       # pid -> {step in ticks: count}
    first_pts: dict = field(default_factory=dict)       # pid -> first PTS seen
    pes: dict = field(default_factory=dict)             # pid -> PES packets started

    @property
    def clean(self) -> bool:
        return (not self.sync_errors and not any(self.cc_errors.values())
                and not self.pcr_backwards and self.pcr_max_gap <= PCR_MAX_GAP_SECONDS
                and not any(self.dts_backwards.values()))

    def problems(self) -> list[str]:
        out = []
        if self.sync_errors:
            out.append(f'{self.sync_errors} packet(s) without the sync byte')
        for pid, c in sorted(self.cc_errors.items()):
            if c:
                out.append(f'PID {pid:#x}: {c} continuity jump(s)')
        if self.pcr_backwards:
            out.append(f'{self.pcr_backwards} PCR(s) running backwards')
        if self.pcr_max_gap > PCR_MAX_GAP_SECONDS:
            out.append(f'a {self.pcr_max_gap * 1000:.1f} ms gap between PCRs')
        for pid, c in sorted(self.dts_backwards.items()):
            if c:
                out.append(f'PID {pid:#x}: {c} decode timestamp(s) running backwards')
        return out


def _forward(prev: int, v: int) -> int:
    """v - prev across the 33-bit wrap, negative when v is behind prev."""
    d = (v - prev) % PTS_WRAP
    return d - PTS_WRAP if d > PTS_WRAP // 2 else d


class Checker:
    """`check()` fed a piece at a time, for a stream too long to hold in memory: a piece may
    end mid-packet, and the rest of that packet starts the next one."""

    def __init__(self):
        self.report = Report()
        self._last_cc = {}
        self._last_pcr = None
        self._last_dts = {}
        self._rest = b''

    def feed(self, data: bytes) -> 'Checker':
        if self._rest:
            data = self._rest + data
        whole = len(data) // TS * TS
        self._rest = data[whole:]
        self._scan(data, whole)
        return self

    def _scan(self, data, whole):
        r, last_cc, last_dts = self.report, self._last_cc, self._last_dts
        last_pcr = self._last_pcr
        for o in range(0, whole, TS):
            r.packets += 1
            if data[o] != SYNC:
                r.sync_errors += 1
                continue
            pid = ((data[o + 1] & 0x1F) << 8) | data[o + 2]
            if pid == 0x1FFF:
                continue
            pusi = data[o + 1] & 0x40
            afc = (data[o + 3] >> 4) & 3
            cc = data[o + 3] & 0x0F
            p = o + 4
            discontinuity = False
            if afc & 2:
                af_len = data[p]
                if af_len:
                    flags = data[p + 1]
                    discontinuity = bool(flags & 0x80)
                    if flags & 0x10:
                        base, ext = _pcr_at(data, p + 2)
                        pcr = base * 300 + ext
                        r.pcr_count += 1
                        if r.first_pcr is None:
                            r.first_pcr = pcr
                        if last_pcr is not None:
                            d = (pcr - last_pcr) % (PTS_WRAP * 300)
                            if d > PTS_WRAP * 150:
                                r.pcr_backwards += 1
                            else:
                                r.pcr_max_gap = max(r.pcr_max_gap, d / CLOCK_HZ)
                        last_pcr = pcr
                p += 1 + af_len
            if afc & 1:
                if pid in last_cc and not discontinuity and cc != (last_cc[pid] + 1) & 0x0F:
                    r.cc_errors[pid] = r.cc_errors.get(pid, 0) + 1
                last_cc[pid] = cc
                if pusi and data[p:p + 3] == b'\x00\x00\x01' and p + 14 <= o + TS:
                    flags = data[p + 7] >> 6
                    if flags & 2:
                        pts = _read_ts(data, p + 9)
                        dts = _read_ts(data, p + 14) if flags == 3 else pts
                        r.pes[pid] = r.pes.get(pid, 0) + 1
                        r.first_pts.setdefault(pid, pts)
                        if pid in last_dts:
                            step = _forward(last_dts[pid], dts)
                            if step < 0:
                                r.dts_backwards[pid] = r.dts_backwards.get(pid, 0) + 1
                            steps = r.dts_steps.setdefault(pid, {})
                            steps[step] = steps.get(step, 0) + 1
                        last_dts[pid] = dts
            elif pid in last_cc and cc != last_cc[pid] and not discontinuity:
                r.cc_errors[pid] = r.cc_errors.get(pid, 0) + 1
        self._last_pcr = last_pcr


def check(data: bytes) -> Report:
    """What a stretch of stream holds, and where it is not seamless. Expects whole packets
    from the start of a packet; any trailing partial packet is ignored."""
    return Checker().feed(data).report


__all__ = ['CLOCK_HZ', 'Checker', 'Loop', 'PTS_HZ', 'PTS_WRAP', 'Report', 'TsError', 'check',
           'programs']
