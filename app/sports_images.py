"""TheSportsDB's poster and background for a recording matched to one of its events.

A recording's cover is otherwise a frame from the program, and for a game that is whatever graphic
it landed on. TheSportsDB has a designed portrait poster for many events and a landscape thumb for
all of them. Both are downloaded once, to this install's own images folder, and written beside the
video by app/metadata_sidecar.py: the poster as `-poster`, the thumb as `-fanart`, which Plex, Kodi
and Jellyfin all read as the background. With no poster the thumb stands in as the poster too, since
the frame it would otherwise fall back to is landscape as well (dev/changelog/1465).

- **ChannelBin is a connector.** It ships no TheSportsDB image; each install fetches them through
  the public API's addresses for its own user (DESIGN-sports-data.md 8). Credit is on every surface
  that names one.
- **One download per image, at the worker's pace.** The images come from a Cloudflare cache on a
  different host from the API, so they most likely do not count against its limit, but that could
  not be proven without pushing the limit; at a few a day the shared pace costs nothing. A 429 backs
  the pace off exactly as one from the API does.
- **Only for a recording that has not finished.** A finished one has already written its sidecar,
  and a sidecar is never rewritten behind the user's back: the video's storage may be asleep.
- **A downloaded file is untrusted:** only an https address on thesportsdb.com is fetched, a
  redirect is not followed, the body is capped, and the bytes must be a JPEG or PNG
  (app/profile_posters.py's checks). The stored name is chosen here.
- **Teardown:** a file is shared by every recording naming its address, so it goes only with the
  last of them: recorder.recording_image_paths() lists it then, database.detach_recording_references()
  forgets its row, and the worker's daily prune catches a link that moved or was dropped.
"""
import hashlib
import logging
import os
import re
import time
from datetime import datetime, timedelta
from urllib.parse import urlsplit

import requests
from sqlalchemy import or_
from urllib3.exceptions import LocationParseError
from urllib3.util import parse_url

from . import db
from .database import (
    REC_ENDED_STATUSES, SPORTS_FETCH_OK, SPORTS_FETCH_RATE_LIMITED, SPORTS_FETCH_REFUSED, Recording,
    RecordingSports, SportsImage,
)
from .db_utils import retry_on_locked
from .profile_posters import EXTENSIONS, MAX_BYTES, sniff_image, write_image_file
from .storage_dirs import SPORTS_IMAGES, image_dir

log = logging.getLogger(__name__)

#: The shape of every stored name: a hash of the address, so one address is one file.
FILENAME_RE = re.compile(r'^[0-9a-f]{20}\.(jpg|png)$')
_HOST = 'thesportsdb.com'
#: The characters a host name may have. Anything else (a backslash, a percent sign) is where two URL
#: parsers can disagree about the host, which is how a check passes and the fetch goes elsewhere.
_HOST_RE = re.compile(r'^[a-z0-9.-]+$')
_CHUNK = 64 * 1024
#: The whole download, not each read: a server sending a byte every few seconds would otherwise hold
#: the worker's client lock, and every person's lookup with it, for as long as it liked.
DOWNLOAD_DEADLINE_SECONDS = 60

#: What each image is to a recording, by its role. `label` starts a sentence on the recording page.
POSTER = 'poster'
BACKGROUND = 'background'
LABELS = {POSTER: 'Poster', BACKGROUND: 'Background'}

#: The state of one of a recording's images, as image_state() names it.
READY = 'ready'          # downloaded, and the file is there
WAITING = 'waiting'      # not tried yet, or will be tried again
REFUSED = 'refused'      # the address is not one this fetches; never tried
MISSED = 'missed'        # the recording finished before it was downloaded
OFF = 'off'              # downloading is switched off in Settings
UNUSED = 'unused'        # this recording writes no metadata file, the one place they are used


def enabled(cfg: dict) -> bool:
    return bool((cfg.get('sports_data') or {}).get('download_images', True))


def address_refused(url: str) -> str:
    """Why this address is not fetched, or '' when it is. The address comes from TheSportsDB's
    answer, so this is where it is checked, not trusted."""
    try:
        parts = urlsplit(url)
        port = parts.port
        # What requests will actually connect to, which must be the host checked here.
        fetched = (parse_url(url).host or '').lower()
    except (ValueError, LocationParseError):
        return 'Its address could not be read.'
    host = (parts.hostname or '').lower()
    if parts.scheme != 'https' or parts.username or parts.password or port not in (None, 443):
        return 'Its address is not a plain https address, so it was not downloaded.'
    if not _HOST_RE.match(host) or fetched != host or any(c.isspace() or c == '\\' for c in url):
        return 'Its address has characters a web address does not, so it was not downloaded.'
    if host != _HOST and not host.endswith('.' + _HOST):
        return f'Its address is on {host}, not thesportsdb.com, so it was not downloaded.'
    return ''


def stored_name(url: str, kind: str) -> str:
    return hashlib.sha256(url.encode('utf-8')).hexdigest()[:20] + EXTENSIONS[kind]


def image_path(cfg: dict, file_name: str) -> str:
    """Where a stored image is. Refuses a name this module could not have chosen."""
    if not FILENAME_RE.match(file_name or ''):
        raise ValueError(f'not a stored TheSportsDB image name: {file_name!r}')
    return os.path.join(image_dir(cfg, SPORTS_IMAGES), file_name)


def role_urls(link) -> dict:
    """{role: address} for what a link has: the thumb is the background, and the poster too when
    the event has no poster."""
    out = {}
    if link.poster_url or link.thumb_url:
        out[POSTER] = link.poster_url or link.thumb_url
    if link.thumb_url:
        out[BACKGROUND] = link.thumb_url
    return out


# ---------------------------------------------------------------------------
# Downloading, one image per worker turn
# ---------------------------------------------------------------------------

def _wanting():
    """The links of recordings that have not finished and have an address, oldest first, with their
    recordings and profiles in the same query."""
    return (db.session.query(RecordingSports, Recording)
            .join(Recording, Recording.id == RecordingSports.recording_id)
            .filter(RecordingSports.removed_at.is_(None), Recording.status.notin_(REC_ENDED_STATUSES),
                    or_(RecordingSports.poster_url.isnot(None), RecordingSports.thumb_url.isnot(None)))
            .order_by(RecordingSports.linked_at, RecordingSports.recording_id).all())


def needed() -> bool:
    """An unfinished recording has an address to download, so the worker has work even with nothing
    followed (sports_data.worker_needed())."""
    return bool(_wanting())


def _file_present(cfg: dict, img) -> bool:
    try:
        return bool(img.file_name) and os.path.exists(image_path(cfg, img.file_name))
    except ValueError:
        return False


def next_due(cfg: dict, now: datetime):
    """The address to download next, or None. Only for a recording whose metadata file is on, since
    that is the only place an image is used: nothing is fetched that nobody gets. A file that went
    missing is downloaded again. Two queries, whatever the count."""
    from .metadata_sidecar import sidecar_enabled
    urls = list(dict.fromkeys(u for link, rec in _wanting() if sidecar_enabled(cfg, rec.profile, rec)
                              for u in (link.poster_url, link.thumb_url) if u))
    if not urls:
        return None
    known = {i.url: i for i in SportsImage.query.filter(SportsImage.url.in_(urls))}
    for url in urls:
        img = known.get(url)
        if img is None:
            return url
        if img.file_name:
            if not _file_present(cfg, img):
                return url
        elif img.next_try_at is not None and img.next_try_at <= now:
            return url
    return None


def _download(url: str):
    """(outcome, data, info, reason). Never raises; every failure is a named outcome."""
    from .sports_data import TIMEOUT_SECONDS
    deadline = time.monotonic() + DOWNLOAD_DEADLINE_SECONDS
    try:
        resp = requests.get(url, timeout=TIMEOUT_SECONDS, stream=True, allow_redirects=False,
                            headers={'User-Agent': 'ChannelBin'})
    except requests.Timeout:
        return SPORTS_FETCH_REFUSED, None, None, f'No answer within {TIMEOUT_SECONDS} seconds.'
    except requests.RequestException as exc:
        return SPORTS_FETCH_REFUSED, None, None, f'Could not reach TheSportsDB\'s image server ({type(exc).__name__}).'
    with resp:
        if resp.status_code == 429:
            return SPORTS_FETCH_RATE_LIMITED, None, None, 'HTTP 429, too many requests.'
        if 300 <= resp.status_code < 400:
            return SPORTS_FETCH_REFUSED, None, None, f'HTTP {resp.status_code}, a redirect, which is not followed.'
        if resp.status_code != 200:
            return SPORTS_FETCH_REFUSED, None, None, f'HTTP {resp.status_code}.'
        cap = f'It is larger than {MAX_BYTES // (1024 * 1024)} MB, so it was not kept.'
        try:
            if int(resp.headers.get('Content-Length') or 0) > MAX_BYTES:
                return SPORTS_FETCH_REFUSED, None, None, cap
        except ValueError:
            pass  # an unreadable length is checked by counting below
        data = bytearray()
        try:
            for chunk in resp.iter_content(_CHUNK):
                data.extend(chunk)
                if len(data) > MAX_BYTES:
                    return SPORTS_FETCH_REFUSED, None, None, cap
                if time.monotonic() > deadline:
                    return (SPORTS_FETCH_REFUSED, None, None,
                            f'It took more than {DOWNLOAD_DEADLINE_SECONDS} seconds, so it was not kept.')
        except requests.RequestException as exc:
            return SPORTS_FETCH_REFUSED, None, None, f'The download broke off ({type(exc).__name__}).'
    info = sniff_image(bytes(data))
    if info is None:
        return SPORTS_FETCH_REFUSED, None, None, 'What came back was not a JPEG or PNG image.'
    return SPORTS_FETCH_OK, bytes(data), info, ''


def fetch_next(cfg: dict, settings: dict, now: datetime):
    """Download the one image that is due, if any. Called by sports_data.tick() with its client lock
    held and the pace already checked, so this is a call like any other. Returns the outcome, or
    None when nothing was due."""
    if not enabled(cfg):
        return None
    url = next_due(cfg, now)
    db.session.rollback()  # holds no read transaction across the outbound call
    if url is None:
        return None
    refused = address_refused(url)
    if refused:
        _record(url, None, SPORTS_FETCH_REFUSED, refused, settings, now, called=False)
        log.warning('TheSportsDB image %s: %s', url, refused)
        return SPORTS_FETCH_REFUSED
    outcome, data, info, reason = _download(url)
    name = None
    if outcome == SPORTS_FETCH_OK:
        name = stored_name(url, info.kind)
        try:
            folder = image_dir(cfg, SPORTS_IMAGES)
            os.makedirs(folder, exist_ok=True)
            write_image_file(os.path.join(folder, name), data)
        except OSError as exc:
            outcome, name, reason = SPORTS_FETCH_REFUSED, None, f'It could not be saved ({exc.strerror or exc}).'
    log.info('TheSportsDB image %s: %s%s', url, outcome, f' ({reason})' if reason else '')
    _record(url, name, outcome, reason, settings, now, called=True)
    return outcome


@retry_on_locked()
def _record(url, name, outcome, reason, settings, now, called):
    """The write for one try. A refused address made no call, so it leaves the pace alone and is not
    tried again; a failed call tries again after the worker's usual wait for a refusal."""
    from .sports_data import REFUSED_RETRY_HOURS, Answer, _note_call, _state
    if called:
        _note_call(_state(), settings, Answer(outcome), now)
    img = SportsImage.query.filter_by(url=url).first()
    if img is None:
        img = SportsImage(url=url, tries=0)
        db.session.add(img)
    img.tried_at = now
    img.tries = (img.tries or 0) + 1
    if name:
        img.file_name, img.fetched_at, img.reason, img.next_try_at = name, now, None, None
    else:
        # A file that went missing and failed to come back is no longer stored either.
        img.file_name, img.reason = None, reason
        if not called:
            img.next_try_at = None
        elif outcome == SPORTS_FETCH_RATE_LIMITED:
            img.next_try_at = now  # the backed-off pace is the wait
        else:
            img.next_try_at = now + timedelta(hours=REFUSED_RETRY_HOURS)
    db.session.commit()


# ---------------------------------------------------------------------------
# Reading: the sidecar and the recording page
# ---------------------------------------------------------------------------

def images_for(link) -> dict:
    """{address: SportsImage} for a link's addresses. One query."""
    urls = [u for u in (link.poster_url, link.thumb_url) if u]
    if not urls:
        return {}
    return {i.url: i for i in SportsImage.query.filter(SportsImage.url.in_(urls))}


def image_state(cfg: dict, url: str, img, finished: bool, used: bool = True) -> tuple:
    """(state, path or None, reason) for one address. `used` is whether the recording writes a
    metadata file. Stats only the images folder, never the recording's own storage."""
    if not enabled(cfg):
        return OFF, None, ''
    if img is not None and _file_present(cfg, img):
        return READY, image_path(cfg, img.file_name), ''
    if img is not None and img.file_name:
        reason = 'Its downloaded copy was deleted.' if finished else 'Its downloaded copy is missing, so it is fetched again.'
        return (MISSED if finished else WAITING), None, reason
    refused = address_refused(url)
    if refused:
        return REFUSED, None, refused
    if not finished and not used:
        return UNUSED, None, ''
    return (MISSED if finished else WAITING), None, (img.reason if img is not None else '') or ''


def artwork(cfg: dict, link, finished: bool = True, used: bool = True) -> dict:
    """{role: (state, path, reason)} for an active link, the roles it has addresses for."""
    if link is None:
        return {}
    known = images_for(link)
    return {role: image_state(cfg, url, known.get(url), finished, used) for role, url in role_urls(link).items()}


# ---------------------------------------------------------------------------
# Teardown
# ---------------------------------------------------------------------------

def _named(url_column):
    """Whether any recording_sports row names this address, as a SQL condition: a subquery, so the
    cost does not grow with a list of bound values."""
    return or_(url_column.in_(db.session.query(RecordingSports.poster_url).filter(RecordingSports.poster_url.isnot(None))),
               url_column.in_(db.session.query(RecordingSports.thumb_url).filter(RecordingSports.thumb_url.isnot(None))))


def owned_paths(recording_id: int, cfg: dict) -> list:
    """The downloaded images only this recording names, for its delete. Read before its row goes.
    Nothing to do, and no query past the first, for a recording with no addresses."""
    link = db.session.get(RecordingSports, recording_id)
    mine = [u for u in (link.poster_url, link.thumb_url) if u] if link is not None else []
    if not mine:
        return []
    shared = {u for row in db.session.query(RecordingSports.poster_url, RecordingSports.thumb_url).filter(
        RecordingSports.recording_id != recording_id,
        or_(RecordingSports.poster_url.in_(mine), RecordingSports.thumb_url.in_(mine))) for u in row if u}
    out = []
    for img in SportsImage.query.filter(SportsImage.url.in_(set(mine) - shared), SportsImage.file_name.isnot(None)):
        try:
            out.append(image_path(cfg, img.file_name))
        except ValueError:
            log.warning('Not deleting %r: it is not a stored TheSportsDB image name', img.file_name)
    return out


def _unreferenced():
    return SportsImage.query.filter(~_named(SportsImage.url))


def forget_unreferenced():
    """Delete the rows of images no recording names. Inside the caller's unit; does not commit.
    The files are the caller's: a recording's delete lists them, the prune removes them itself."""
    _unreferenced().delete(synchronize_session=False)


def prune(cfg: dict):
    """The worker's daily pass: images no recording names, and files in the folder no row names
    (a crash between writing one and storing its name). Runs with the client lock held, so no
    download is writing while it lists."""
    unreferenced = _forget_and_list()
    folder = image_dir(cfg, SPORTS_IMAGES)
    try:
        on_disk = os.listdir(folder)
    except FileNotFoundError:
        return
    except OSError as exc:
        log.warning('Could not list %s to remove unused TheSportsDB images: %s', folder, exc)
        return
    kept = {n for (n,) in db.session.query(SportsImage.file_name).filter(SportsImage.file_name.isnot(None))}
    db.session.rollback()
    for name in on_disk:
        stale = name.endswith('.tmp') or (FILENAME_RE.match(name) and name not in kept)
        if not stale and name not in unreferenced:
            continue
        try:
            os.remove(os.path.join(folder, name))
        except OSError as exc:
            log.warning('Could not remove the unused TheSportsDB image %s: %s', name, exc)


@retry_on_locked()
def _forget_and_list() -> set:
    q = _unreferenced()
    names = {i.file_name for i in q.all() if i.file_name}
    q.delete(synchronize_session=False)
    db.session.commit()
    return names
