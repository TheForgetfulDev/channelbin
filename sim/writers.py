"""The bytes on the wire: JSON the way a PHP panel encodes it, M3U playlists, XMLTV and PNG.

Nothing here is shared with the application. The simulator writes these formats itself, so an
oracle never agrees with a reader's bug because the two were built from the same code.
"""
import datetime as _dt
import gzip
import json
import struct
import zlib
from xml.sax.saxutils import escape


def php_json(obj) -> bytes:
    """JSON as PHP's json_encode writes it by default: no spaces, ASCII only (anything else
    becomes a \\u escape, emoji as surrogate pairs), and every forward slash escaped."""
    # Safe as a blind replace: outside strings, JSON's grammar has no slash at all.
    return json.dumps(obj, separators=(',', ':')).replace('/', '\\/').encode('ascii')


def gzip_bytes(data: bytes) -> bytes:
    """gzip with a zero timestamp, so the same input always compresses to the same bytes."""
    return gzip.compress(data, compresslevel=6, mtime=0)


def _attr(value) -> str:
    return escape(str(value), {'"': '&quot;'})


def m3u(entries, duration: int = -1, header: str = '#EXTM3U') -> bytes:
    """A playlist from (attrs, display name, url) triples; attrs is a list of (key, value)
    pairs written in order, as `key="value"`."""
    lines = [header]
    for attrs, name, url in entries:
        attr_text = ' '.join(f'{k}="{v}"' for k, v in attrs)
        lines.append(f'#EXTINF:{duration} {attr_text},{name}' if attr_text
                     else f'#EXTINF:{duration},{name}')
        lines.append(url)
    return ('\n'.join(lines) + '\n').encode('utf-8')


def xmltv_time(when: _dt.datetime, offset: str) -> str:
    """`when` is naive UTC; written as local time at `offset` (e.g. '+0000', '-0500')."""
    sign = -1 if offset[0] == '-' else 1
    delta = _dt.timedelta(hours=int(offset[1:3]), minutes=int(offset[3:5])) * sign
    return f'{(when + delta):%Y%m%d%H%M%S} {offset}'


def xmltv(channels, programs, offset: str, generator: str) -> bytes:
    """An XMLTV document. `channels` is (id, display name, icon url or None); `programs` is
    (channel id, guide.Program, icon url or None), already in the order to write them.

    The element names are XMLTV's own, `programme` included: that is the format's spelling,
    not this project's."""
    out = ['<?xml version="1.0" encoding="UTF-8"?>',
           '<!DOCTYPE tv SYSTEM "xmltv.dtd">',
           f'<tv generator-info-name="{_attr(generator)}">']
    for cid, name, icon in channels:
        out.append(f'  <channel id="{_attr(cid)}">')
        out.append(f'    <display-name>{escape(name)}</display-name>')
        if icon:
            out.append(f'    <icon src="{_attr(icon)}"/>')
        out.append('  </channel>')
    for cid, p, icon in programs:
        out.append(f'  <programme start="{xmltv_time(p.start, offset)}" '
                   f'stop="{xmltv_time(p.stop, offset)}" channel="{_attr(cid)}">')
        out.append(f'    <title lang="en">{escape(p.title)}</title>')
        if p.sub_title:
            out.append(f'    <sub-title lang="en">{escape(p.sub_title)}</sub-title>')
        if p.desc:
            out.append(f'    <desc lang="en">{escape(p.desc)}</desc>')
        if p.category:
            out.append(f'    <category lang="en">{escape(p.category)}</category>')
        if p.episode:
            season, episode = p.episode
            out.append(f'    <episode-num system="xmltv_ns">{season - 1}.{episode - 1}.</episode-num>')
            out.append(f'    <episode-num system="onscreen">S{season:02d}E{episode:02d}</episode-num>')
        if icon:
            out.append(f'    <icon src="{_attr(icon)}"/>')
        if p.new:
            out.append('    <new/>')
        if p.rating:
            out.append(f'    <rating system="VCHIP"><value>{escape(p.rating)}</value></rating>')
        out.append('  </programme>')
    out.append('</tv>')
    return ('\n'.join(out) + '\n').encode('utf-8')


def _png_chunk(kind: bytes, data: bytes) -> bytes:
    return (struct.pack('>I', len(data)) + kind + data
            + struct.pack('>I', zlib.crc32(kind + data) & 0xffffffff))


def png(key: str, size: int = 64) -> bytes:
    """A small logo: two colors and a diagonal band, all fixed by `key` (hex)."""
    n = int(key[:12] or '0', 16)
    bg = ((n >> 16) & 0xff, (n >> 8) & 0xff, n & 0xff)
    fg = (255 - bg[0], 255 - bg[1], 255 - bg[2])
    width = 4 + (n >> 24) % (size // 3)
    rows = bytearray()
    for y in range(size):
        rows.append(0)
        for x in range(size):
            rows.extend(fg if abs(x - y) < width else bg)
    header = struct.pack('>IIBBBBB', size, size, 8, 2, 0, 0, 0)
    return (b'\x89PNG\r\n\x1a\n' + _png_chunk(b'IHDR', header)
            + _png_chunk(b'IDAT', zlib.compress(bytes(rows), 9)) + _png_chunk(b'IEND', b''))
