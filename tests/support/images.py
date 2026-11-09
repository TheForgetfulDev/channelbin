"""Image bytes for tests: a PNG or JPEG of a given size, built here so no real image is ever
shipped or fetched. Shared by the profile-poster and TheSportsDB-image tests."""
import struct
import zlib


def png_bytes(width, height):
    """A structurally valid PNG of the given size: signature, IHDR, one IDAT, IEND."""
    def chunk(kind, body):
        return (struct.pack('>I', len(body)) + kind + body
                + struct.pack('>I', zlib.crc32(kind + body) & 0xFFFFFFFF))
    ihdr = struct.pack('>IIBBBBB', width, height, 8, 2, 0, 0, 0)
    raw = b''.join(b'\x00' + b'\x00\x00\x00' * width for _ in range(height))
    return (b'\x89PNG\r\n\x1a\n' + chunk(b'IHDR', ihdr)
            + chunk(b'IDAT', zlib.compress(raw)) + chunk(b'IEND', b''))


def jpeg_bytes(width, height, app1_padding=0, sof=0xC0):
    """A JPEG header carrying the size in an SOF marker, optionally behind a large APP1
    segment the way a camera's EXIF block sits. The scan data is absent - the sniffer
    reads headers only, and that is what these cases pin."""
    out = b'\xff\xd8'
    out += b'\xff\xe0' + struct.pack('>H', 16) + b'JFIF\x00\x01\x01\x00\x00\x01\x00\x01\x00\x00'
    if app1_padding:
        body = b'Exif\x00\x00' + b'\x00' * app1_padding
        out += b'\xff\xe1' + struct.pack('>H', len(body) + 2) + body
    frame = struct.pack('>BHHB', 8, height, width, 3) + b'\x01\x22\x00\x02\x11\x01\x03\x11\x01'
    out += bytes([0xFF, sof]) + struct.pack('>H', len(frame) + 2) + frame
    return out + b'\xff\xd9'
