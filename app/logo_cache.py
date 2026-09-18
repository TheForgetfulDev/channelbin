"""Local caching of Channel.logo_url images instead of hotlinking the provider/CDN on
every page view (dev/changelog/601).

Design decisions:
- Off by default (`recording.logo_cache.enabled`) - an install that doesn't turn this on
  gets no background job and no cache directory.
- Only channels in the TV Guide (`Channel.in_guide`) or a member of any channel group are
  eligible - never the whole catalog, which can be 100k+ channels on a real account.
- Change detection is "the provider's logo_url string changed since we last cached it" -
  no ETag/conditional-GET polling, so a re-sync's channels that keep the same logo_url
  never generate extra provider traffic.
- The actual download runs from a scheduled background job (app/scheduler.py), never
  inline in the per-channel sync loop (`_upsert_channels` already runs at 100k+ row scale,
  and CLAUDE.md forbids hidden I/O in a per-row loop) and never hammers the provider (a
  small batch per run, one request at a time).
"""
import logging
import os

import requests

from . import db
from .config import load_config
from .database import Channel, ChannelGroupMember
from .db_utils import retry_on_locked
from .storage_dirs import LOGOS, image_dir
from .url_utils import mask_creds

log = logging.getLogger(__name__)

# Sanity cap on a single logo download - a provider serving something enormous at a
# stream_icon URL is not a logo, and streaming an unbounded response into memory would be
# a self-inflicted resource problem.
_MAX_LOGO_BYTES = 5 * 1024 * 1024
_FETCH_TIMEOUT_SECONDS = 15

# Raster-only allowlist, not a bare `image/*` check: `channel_logo` serves cached files
# with a Content-Type derived from this extension, so accepting image/svg+xml let a
# provider-supplied SVG containing <script> execute on the app's own origin.
_ALLOWED_LOGO_CONTENT_TYPES = {
    'image/png': '.png',
    'image/jpeg': '.jpg',
    'image/gif': '.gif',
    'image/webp': '.webp',
}


def get_logo_cache_dir(cfg: dict | None = None) -> str:
    """The logo subfolder of recording.images_dir, absolutized. Pass `cfg` when the caller
    already has a loaded config - a bare load_config() call would re-read the real
    config.yaml and escape a test's sandboxed override (CLAUDE.md config-read rule)."""
    if cfg is None:
        cfg = load_config()
    return image_dir(cfg, LOGOS)


def resolve_logo_url(channel: Channel) -> str:
    """The URL a page should put in an <img src> for this channel: the local cache
    route if a logo has been cached, else the provider's own URL unchanged (today's
    behavior). Pure function of already-loaded columns - no I/O - so it is safe to call
    from a per-row loop building a search/guide/group row."""
    if channel.logo_cache_path:
        return f'/api/channels/{channel.id}/logo'
    return channel.logo_url or ''


def _eligible_channel_ids_query():
    """Channels that qualify for logo caching: in the TV Guide, or a member of any
    channel group."""
    member_ids = db.session.query(ChannelGroupMember.channel_id).distinct()
    return Channel.query.filter(
        db.or_(Channel.in_guide.is_(True), Channel.id.in_(member_ids)))


def _channels_needing_fetch(limit: int):
    """Up to `limit` eligible channels whose logo_url is set and has never been attempted,
    or has changed since the last attempt. Deliberately keyed on logo_cache_source_url
    alone, not logo_cache_path: a channel whose last fetch failed (cache_path left NULL,
    source_url stamped with the URL that failed) must NOT be retried every run against
    the same known-bad URL - that would hammer the provider forever on a dead logo link.
    It only becomes eligible again once the provider serves a different logo_url."""
    return (
        _eligible_channel_ids_query()
        .filter(Channel.logo_url.isnot(None), Channel.logo_url != '')
        .filter(db.or_(
            Channel.logo_cache_source_url.is_(None),
            Channel.logo_cache_source_url != Channel.logo_url,
        ))
        .order_by(Channel.id)
        .limit(limit)
        .all()
    )


def _fetch_one_logo(channel: Channel, cache_dir: str, cfg: dict) -> None:
    """Download channel.logo_url and update its cache columns - both outcomes (cached, or
    tried-and-not-an-image) record logo_cache_source_url so this exact URL is not retried
    every run; only a URL change makes it eligible again. Never raises - a bad logo for
    one channel must not abort the batch."""
    from .accounts import _request_headers

    raw_url = channel.logo_url
    try:
        with requests.get(raw_url, timeout=_FETCH_TIMEOUT_SECONDS, stream=True,
                           headers=_request_headers(cfg)) as resp:
            resp.raise_for_status()
            content_type = resp.headers.get('Content-Type', '').split(';')[0].strip().lower()
            ext = _ALLOWED_LOGO_CONTENT_TYPES.get(content_type)
            if ext is None:
                log.warning('Logo cache: channel %d logo_url did not return an allowed '
                            'image type (Content-Type %r) - %s', channel.id, content_type,
                            mask_creds(raw_url))
                _record_logo_attempt(channel.id, raw_url, None)
                return

            chunks = []
            total = 0
            for chunk in resp.iter_content(chunk_size=65536):
                total += len(chunk)
                if total > _MAX_LOGO_BYTES:
                    log.warning('Logo cache: channel %d logo exceeded %d bytes, skipping - %s',
                                channel.id, _MAX_LOGO_BYTES, mask_creds(raw_url))
                    _record_logo_attempt(channel.id, raw_url, None)
                    return
                chunks.append(chunk)
    except Exception as exc:
        log.warning('Logo cache: fetch failed for channel %d: %s', channel.id,
                    mask_creds(str(exc)))
        _record_logo_attempt(channel.id, raw_url, None)
        return

    old_path = channel.logo_cache_path
    filename = f'{channel.id}{ext}'
    try:
        os.makedirs(cache_dir, exist_ok=True)
        with open(os.path.join(cache_dir, filename), 'wb') as f:
            f.write(b''.join(chunks))
    except OSError as exc:
        log.warning('Logo cache: could not write cache file for channel %d: %s',
                    channel.id, exc)
        _record_logo_attempt(channel.id, raw_url, None)
        return

    _record_logo_attempt(channel.id, raw_url, filename)
    if old_path and old_path != filename:
        try:
            os.remove(os.path.join(cache_dir, old_path))
        except OSError:
            pass  # stale filename from a changed content-type; not worth failing the run over


@retry_on_locked()
def _record_logo_attempt(channel_id: int, source_url: str, cache_path: str | None) -> None:
    """Stamp the outcome of one fetch attempt. cache_path=None means "tried this exact
    URL and it wasn't usable" - logo_cache_source_url is still set, so it isn't retried
    again until the provider gives a different logo_url."""
    channel = db.session.get(Channel, channel_id)
    if channel is None:
        return
    channel.logo_cache_source_url = source_url
    channel.logo_cache_path = cache_path
    db.session.commit()


def _purge_svg_logo_cache(cache_dir: str) -> None:
    """Drop any logo cached before the raster-only allowlist existed. Keyed off the DB
    (not a directory scan) since Channel.logo_cache_path is the only record of what's
    safe to remove. Runs every batch tick regardless of the enabled flag - channel_logo
    serves whatever is cached irrespective of it, so a since-disabled install would
    otherwise keep serving a stored SVG forever."""
    stale = Channel.query.filter(Channel.logo_cache_path.like('%.svg')).all()
    for channel in stale:
        old_path = channel.logo_cache_path
        _clear_logo_cache_columns(channel.id)
        try:
            os.remove(os.path.join(cache_dir, old_path))
        except OSError:
            pass


@retry_on_locked()
def _clear_logo_cache_columns(channel_id: int) -> None:
    channel = db.session.get(Channel, channel_id)
    if channel is None:
        return
    channel.logo_cache_path = None
    channel.logo_cache_source_url = None
    db.session.commit()


def run_logo_cache_batch(limit: int = 15) -> int:
    """Fetch up to `limit` pending/stale channel logos. Returns the number attempted.
    Must run inside an app context; caller (the scheduler job) owns that."""
    cfg = load_config()
    lc_cfg = cfg.get('recording', {}).get('logo_cache', {})
    cache_dir = get_logo_cache_dir(cfg)
    _purge_svg_logo_cache(cache_dir)
    if not lc_cfg.get('enabled', False):
        return 0

    channels = _channels_needing_fetch(limit)
    for channel in channels:
        _fetch_one_logo(channel, cache_dir, cfg)
    return len(channels)


def delete_cached_logos(cache_paths) -> None:
    """Best-effort delete of each named on-disk cache file. Takes plain filenames
    (Channel.logo_cache_path values), not Channel rows, because callers need this after
    the rows themselves are already gone - see account teardown in
    routes/accounts.py::_delete_account_and_jobs, which must capture the paths before
    its delete-orphan cascade removes the Channel rows they came from. CLAUDE.md's
    "teardown releases everything the create path acquired": a deleted account must not
    leave orphaned image files behind."""
    cache_dir = get_logo_cache_dir()
    for path in cache_paths:
        if not path:
            continue
        try:
            os.remove(os.path.join(cache_dir, path))
        except OSError:
            pass
