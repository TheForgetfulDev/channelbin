"""The stream tests' shared footing: the small test loops, a world that serves them, and a way
to read a stream for a set time (DESIGN-simulator.md §3.3).

The loops render once per machine into the simulator's default content cache (about 1.5 s
each, under 3 MB together) and are reused after that. A run without ffmpeg skips, as every
other ffmpeg test does; `run_tests.sh` refuses to start without it.
"""
import copy
import http.client
import shutil
import socket
import threading
import time
import unittest
from urllib.parse import urlsplit

import requests

from sim import Library, build_world
from sim import ts as sim_ts

TEST_PROFILES = ('test_tiny', 'test_tiny_low', 'radio_mp3')

_library = None
_library_lock = threading.Lock()


def library(*extra) -> Library:
    """The default content cache with the test loops in it, rendered the first time, plus any
    `extra` profiles a test needs (rendered once per machine like the rest)."""
    global _library
    with _library_lock:
        if _library is None:
            if not shutil.which('ffmpeg') or not shutil.which('ffprobe'):
                raise unittest.SkipTest('ffmpeg and ffprobe are needed to render the test loops')
            lib = Library()
            lib.build(TEST_PROFILES)
            _library = lib
        if extra:
            _library.build(extra)
        return _library


# One backend behind two Xtream panels (the second listing its streams on its own API host),
# with radio and third-party HLS channels, every channel on the tiny loop.
STREAM_WORLD = {
    'seed': 5,
    'backends': [{'name': 'cove', 'channels': 60, 'stream_hosts': 2, 'content': 'test_tiny',
                  'hls_variant': 'test_tiny_low', 'burst_seconds': 4.0,
                  'hls_segment_seconds': 4.0, 'radio_share': 0.05, 'no_id_share': 0.05}],
    'panels': [
        {'name': 'cove-tv', 'backend': 'cove', 'url_shape': {'bare': 1.0}},
        {'name': 'cove-plus', 'backend': 'cove', 'streams_on_api_host': True,
         'url_shape': {'live_ts': 1.0}},
    ],
    'logins': [{'backend': 'cove', 'username': 'reeduser', 'password': 'reedpass1',
                'panels': ['cove-tv', 'cove-plus'], 'max_connections': 1},
               {'backend': 'cove', 'username': 'fernuser', 'password': 'fernpass1',
                'panels': ['cove-tv'], 'max_connections': 2}],
}


def stream_world(**backend):
    """STREAM_WORLD with its backend's settings changed."""
    data = copy.deepcopy(STREAM_WORLD)
    data['backends'][0].update(backend)
    return build_world(data)


def live_ids(world, panel='cove-tv') -> list:
    """Stream ids of the panel's channels that carry a stream id, in listing order."""
    return [row.channel.stream_id for row in world.panel_lineups[panel].rows
            if row.channel.kind in ('live', 'event', 'separator')]


def origin(url: str) -> str:
    p = urlsplit(url)
    return f'{p.scheme}://{p.netloc}'


def read_for(url: str, seconds: float, *, first_piece_by: float | None = None) -> tuple:
    """(status, bytes) of a GET read for `seconds` of wall time. With `first_piece_by`, only
    what arrived within that many seconds of the request is returned."""
    out = bytearray()
    start = time.monotonic()
    with requests.get(url, stream=True, timeout=10) as r:
        if r.status_code != 200:
            return r.status_code, b''
        for piece in r.iter_content(4096):
            if first_piece_by is not None and time.monotonic() - start > first_piece_by:
                break
            out += piece
            if time.monotonic() - start > seconds:
                break
        return r.status_code, bytes(out)


def read_stream(url: str, seconds: float, *, read_timeout: float = 1.0, headers=None) -> dict:
    """A GET read for up to `seconds` of wall time, saying how it ended: 'time' (still
    flowing when the time ran out), 'eof' (the server closed it) or 'silent' (nothing arrived
    for `read_timeout`). `pieces` is (seconds since the request, bytes) per piece received."""
    # http.client rather than requests: read1() hands back whatever has arrived, where
    # requests' iter_content blocks to fill its block and so never returns the bytes sent just
    # before a silence.
    out = bytearray()
    pieces = []
    start = time.monotonic()
    ended = 'time'
    for _ in range(5):
        p = urlsplit(url)
        conn = http.client.HTTPConnection(p.hostname, p.port, timeout=read_timeout)
        try:
            conn.request('GET', p.path + (f'?{p.query}' if p.query else ''),
                         headers=headers or {})
            resp = conn.getresponse()
            if resp.status in (301, 302, 303, 307, 308):
                url = resp.getheader('Location')
                if url.startswith('/'):
                    url = f'{p.scheme}://{p.netloc}{url}'
                continue
            status, resp_headers = resp.status, dict(resp.getheaders())
            try:
                while True:
                    piece = resp.read1(65536)
                    if not piece:
                        ended = 'eof'
                        break
                    pieces.append((time.monotonic() - start, len(piece)))
                    out += piece
                    if time.monotonic() - start > seconds:
                        break
            except (TimeoutError, socket.timeout):
                ended = 'silent'
            break
        finally:
            conn.close()
    else:
        raise AssertionError(f'more than five redirects from {url}')
    return {'status': status, 'headers': resp_headers, 'data': bytes(out), 'ended': ended,
            'pieces': pieces, 'wall': time.monotonic() - start}


def pes_dts(data: bytes, pid: int) -> list:
    """The decode timestamp (the PTS where there is no DTS), in 90 kHz ticks as sent and
    wrapping at 33 bits, of every PES of `pid`, in the order sent. Decode order, not
    presentation order: B-frames send their PTS out of order."""
    out = []
    ts = sim_ts.TS
    for o in range(0, len(data) - ts + 1, ts):
        if data[o] != 0x47 or not data[o + 1] & 0x40:
            continue
        if ((data[o + 1] & 0x1F) << 8) | data[o + 2] != pid:
            continue
        p = o + 4
        if (data[o + 3] >> 4) & 2:
            p += 1 + data[p]
        if data[p:p + 3] == b'\x00\x00\x01' and data[p + 7] >> 6 & 2:
            out.append(sim_ts._read_ts(data, p + 14 if data[p + 7] >> 6 == 3 else p + 9))
    return out


def pcrs(data: bytes) -> list:
    """Every PCR base in a stretch of stream, 90 kHz ticks, in order."""
    out = []
    ts = sim_ts.TS
    for o in range(0, len(data) - ts + 1, ts):
        if data[o] == 0x47 and (data[o + 3] >> 4) & 2 and data[o + 4] and data[o + 5] & 0x10:
            out.append(sim_ts._pcr_at(data, o + 6)[0])
    return out


VIDEO_PID, AUDIO_PID = 0x100, 0x101     # where every rendered loop puts them


def wait_until(predicate, seconds: float = 3.0) -> bool:
    """Poll `predicate` until it holds or `seconds` pass; a connection's seat is released once
    the server's next write finds the client gone, a few ticks after it hangs up."""
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        if predicate():
            return True
        time.sleep(0.02)
    return predicate()


def first_pcr_seconds(data: bytes) -> float:
    return sim_ts.check(whole_packets(data)).first_pcr / sim_ts.CLOCK_HZ


def whole_packets(data: bytes) -> bytes:
    return data[:len(data) // sim_ts.TS * sim_ts.TS]


def video_seconds(data: bytes) -> float:
    """Seconds of video in a stretch of stream, from its video PES count at the loop's rate."""
    streams, _, _ = sim_ts.programs(data)
    vpid = next(p for p, t in streams.items() if t in sim_ts.VIDEO_TYPES)
    return sim_ts.check(whole_packets(data)).pes.get(vpid, 0) * 1001 / 30000
