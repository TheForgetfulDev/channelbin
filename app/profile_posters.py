"""The poster image a Recording Profile can pin, and the one upload surface this app has.

A finished recording's poster is otherwise a frame captured from the recording itself, and
for a ball game that is whatever graphic the capture ended on. A profile that carries its
own image gives every recording made under it the same cover - the series logo, the league
badge - which is what a library of sports recordings actually wants (dev/changelog/1059).

This is the first place the app accepts a file from the browser, so the rules that keep an
upload from becoming an attack surface are all here and all enforced on the server:

- **The file is what its bytes say it is.** JPEG and PNG are recognized by their signatures,
  never by the extension or the browser's content type, and the pixel size comes out of the
  header - PNG's IHDR, JPEG's SOF marker - without decoding the image. Nothing is resized,
  cropped or converted; the file is stored exactly as uploaded, and the form says what size
  a media server expects so the user can supply that.
- **The stored name is chosen here, never taken from the upload** - `profile-<id>-<token>.<ext>`
  inside images_dir's posters subfolder - so there is nothing to sanitize and no path a
  name could walk out of. `FILENAME_RE` is the shape every stored name has, and the serving
  route refuses a row that does not match it.
- **The size cap is on the request, not only the file.** CSRFProtect parses the form body
  before any view runs, so a per-view cap would arrive after the bytes were already read;
  the app-wide MAX_CONTENT_LENGTH is `MAX_REQUEST_BYTES` and the route re-checks the image
  itself against `MAX_BYTES`.
- **A half-written file never becomes the poster**: it is written to a temp name in the
  same folder and moved into place with os.replace().

The profile's file is shared by every recording made under it, so a recording's teardown
never touches it (recorder.recording_disk_paths lists the COPY beside the video, not the
source). Only deleting the profile, replacing its poster or removing it discards the file.
"""
import logging
import os
import re
import secrets
import struct
from typing import NamedTuple, Optional

from .storage_dirs import POSTERS, image_dir

log = logging.getLogger(__name__)

#: What a media server's poster slot is drawn for: Plex, Jellyfin and Kodi all document a
#: 2:3 portrait poster at this size. The form states it and the upload response compares
#: against it; nothing here enforces it, because a logo that is 999 x 1499 is not a mistake
#: worth refusing when the file is used as-is either way.
POSTER_WIDTH = 1000
POSTER_HEIGHT = 1500

#: A 1000 x 1500 PNG is under 3 MB; this leaves room for a generous one without letting
#: the upload endpoint become a way to fill the images folder.
MAX_BYTES = 10 * 1024 * 1024
#: Multipart framing around the image - the boundary lines, the part headers and the small
#: form fields beside it. The app-wide request cap is the image cap plus this.
MAX_REQUEST_BYTES = MAX_BYTES + 64 * 1024

#: Image kind (as sniff_image() names it) -> the extension the stored file gets.
EXTENSIONS = {'jpeg': '.jpg', 'png': '.png'}

#: The one shape a stored poster name can have. Anything else in `poster_file` did not
#: come from store_poster() and is never served or deleted.
FILENAME_RE = re.compile(r'^profile-(\d+)-([0-9a-f]{12})\.(jpg|png)$')

_PNG_SIGNATURE = b'\x89PNG\r\n\x1a\n'
#: The JPEG frame-header markers that carry the image size (baseline, progressive,
#: lossless, and their differential/arithmetic variants). Not C4 (huffman tables), C8
#: (reserved) or CC (arithmetic conditioning), which share the range but hold no size.
_JPEG_SOF_MARKERS = frozenset(
    [0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7, 0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF])
#: Markers with no length field after them.
_JPEG_BARE_MARKERS = frozenset([0x01, 0xD8] + list(range(0xD0, 0xD8)))


class ImageInfo(NamedTuple):
    """What sniff_image() learned: the kind ('jpeg' or 'png') and the pixel size."""
    kind: str
    width: int
    height: int


def _png_info(data: bytes) -> Optional[ImageInfo]:
    # Signature, then the IHDR chunk is required to come first: 4-byte length, 'IHDR',
    # then width and height as big-endian 32-bit integers.
    if len(data) < 24 or not data.startswith(_PNG_SIGNATURE) or data[12:16] != b'IHDR':
        return None
    width, height = struct.unpack('>II', data[16:24])
    if width <= 0 or height <= 0:
        return None
    return ImageInfo('png', width, height)


def _jpeg_info(data: bytes) -> Optional[ImageInfo]:
    """Walk the marker segments to the first frame header. The size lives in SOFn, which
    can sit behind arbitrarily large APPn (EXIF, ICC) and comment segments, so this reads
    marker lengths rather than assuming an offset."""
    if len(data) < 4 or data[0:2] != b'\xff\xd8':
        return None
    pos = 2
    while pos + 4 <= len(data):
        if data[pos] != 0xFF:
            return None
        marker = data[pos + 1]
        if marker == 0xFF:
            pos += 1          # fill byte
            continue
        if marker in _JPEG_BARE_MARKERS:
            pos += 2
            continue
        if marker in (0xD9, 0xDA):
            return None       # end of image / start of scan with no frame header seen
        (length,) = struct.unpack('>H', data[pos + 2:pos + 4])
        if length < 2:
            return None
        if marker in _JPEG_SOF_MARKERS:
            if pos + 9 > len(data):
                return None
            height, width = struct.unpack('>HH', data[pos + 5:pos + 9])
            if width <= 0 or height <= 0:
                return None
            return ImageInfo('jpeg', width, height)
        pos += 2 + length
    return None


def sniff_image(data: bytes) -> Optional[ImageInfo]:
    """The kind and pixel size of `data` when it is a JPEG or PNG, else None.

    Reads the header only. A file whose header is intact but whose body is truncated or
    corrupt passes here and shows up broken in the library - the alternative is decoding
    every upload, which is the image processing this feature deliberately does not do.
    """
    return _png_info(data) or _jpeg_info(data)


def size_advice(width: int, height: int) -> Optional[str]:
    """One sentence saying how an image differs from what a media server expects, or None
    when it is exactly that. Mirrored by pmSizeAdvice in static/js/profile-modal.js so the
    modal can say it before the upload and the response can say it after."""
    if width == POSTER_WIDTH and height == POSTER_HEIGHT:
        return None
    expected = f'{POSTER_WIDTH} x {POSTER_HEIGHT}'
    if width * POSTER_HEIGHT == height * POSTER_WIDTH:
        return (f'This image is {width} x {height}. It has the right 2:3 shape but is '
                f'{"smaller" if width < POSTER_WIDTH else "larger"} than the {expected} '
                f'media servers expect, so it will be scaled by the server.')
    return (f'This image is {width} x {height}. Media servers expect a portrait poster of '
            f'{expected} (2:3), so this one will be cropped or letterboxed to fit.')


def posters_dir(cfg: dict) -> str:
    """The folder pinned posters live in: images_dir's posters subfolder."""
    return image_dir(cfg, POSTERS)


def poster_path(cfg: dict, filename: str) -> str:
    """Where a stored poster is on disk. Every path here goes through this, and it refuses
    a name that store_poster() could not have produced rather than joining it."""
    if not FILENAME_RE.match(filename or ''):
        raise ValueError(f'not a stored poster name: {filename!r}')
    return os.path.join(posters_dir(cfg), filename)


def poster_payload(profile) -> Optional[dict]:
    """What the profiles page and the modal show for a profile's poster, or None."""
    if not profile.poster_file:
        return None
    match = FILENAME_RE.match(profile.poster_file)
    return {
        # The token is part of the name and changes on every upload, so the browser
        # cannot keep showing the previous image after a replace.
        'url': f'/api/profiles/{profile.id}/poster?v={match.group(2) if match else ""}',
        'width': profile.poster_width,
        'height': profile.poster_height,
        'kind': 'PNG' if profile.poster_file.endswith('.png') else 'JPG',
        'advice': size_advice(profile.poster_width or 0, profile.poster_height or 0),
    }


def store_poster(cfg: dict, profile_id: int, data: bytes, info: ImageInfo) -> str:
    """Write `data` into the posters folder under a fresh server-chosen name and return
    that name. Raises OSError when the folder cannot be written.

    The name carries a random token rather than overwriting `profile-<id>.<ext>` in place:
    the previous file stays intact until the row points at the new one, so a crash between
    the write and the commit leaves the old poster serving, not a torn one.
    """
    folder = posters_dir(cfg)
    os.makedirs(folder, exist_ok=True)
    filename = f'profile-{profile_id}-{secrets.token_hex(6)}{EXTENSIONS[info.kind]}'
    final = os.path.join(folder, filename)
    tmp = final + '.tmp'
    try:
        with open(tmp, 'wb') as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, final)
    except OSError:
        try:
            os.remove(tmp)
        except OSError:
            pass  # best-effort cleanup of the temp file; the original error is what matters
        raise
    return filename


def discard_poster_file(cfg: dict, filename: Optional[str]):
    """Remove a stored poster from disk, best-effort. A name that is not one of ours is
    left alone and logged - deleting on the strength of a column value that something
    else wrote is how a bad row turns into a missing file elsewhere."""
    if not filename:
        return
    try:
        path = poster_path(cfg, filename)
    except ValueError:
        log.warning('Not deleting %r: it is not a stored poster name', filename)
        return
    try:
        os.remove(path)
    except FileNotFoundError:
        pass  # already gone, which is the state being asked for
    except OSError as exc:
        log.warning('Could not remove poster %s: %s', path, exc)
