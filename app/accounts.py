"""Generic account sync orchestration - shared by M3U and Xtream accounts.

Handles channel/EPG sync dispatch, M3U playlist parsing, XMLTV import,
URL normalization, and filename templating. Code specific to the Xtream API
(the HTTP client, dump/debug tooling) lives in app/xtream_client.py and is
imported here lazily to avoid a circular import.
"""
import bisect
import functools
import gzip
import hashlib
import math
import os
import re
import logging
import threading
import time
import traceback
import zlib
import defusedxml.ElementTree as ET
from defusedxml.common import DefusedXmlException
from datetime import datetime, timedelta, timezone
from io import BytesIO
from typing import NamedTuple

import requests
from sqlalchemy import func, select, update
from sqlalchemy.exc import SQLAlchemyError

from .config import load_config, config_default
from . import admission, db
from .channel_groups import touch_group
from .database import (
    Account, Alert, Channel, ChannelEvent, ChannelGroupMember, EPGEntry, AccountSyncLog,
    EpgAlternateEntry, EpgSource, EPG_SOURCE_PROVIDER, EPG_SOURCE_URL,
    EPG_STATUS_FAILED, EPG_STATUS_OK, EPG_STATUS_REFUSED, EPG_STATUS_TRUNCATED,
    Recording, add_recording_event,
    CHANNEL_URL_CHANGED, CHANNEL_ADDED_TO_GUIDE, CHANNEL_REMOVED_FROM_GUIDE,
    RECORDING_REPOINTED, REC_STATUS_IN_PROGRESS, REC_STATUS_SCHEDULED,
)
from .db_utils import current_wal_size_bytes, retry_on_locked
from .epg_sources import (
    DISTINCT_TITLE_CAP, REASON_NONE, UPCOMING_TITLES, DirectoryRow, accepted_keys,
    add_held_coverage, apply_winners, channel_key, demote, directory_coverage, drop_alternates,
    ensure_provider_source, held_coverage, norm_key, promote, provider_xmltv_url,
    refresh_source_counts, resolve_active_source, sources_refreshed_by_sync, subscriber_ids,
    subscriptions_for, sources_without_directory, write_directory,
)
from .fmt_utils import fmt_bytes
from .tz_utils import format_local, parse_epoch_utc, to_naive_utc
from .url_utils import mask_account_urls_in_text, mask_creds, mask_creds_in_text, mask_url_path

log = logging.getLogger(__name__)

_sync_locks: dict[int, threading.Lock] = {}
_sync_threads: dict[int, threading.Thread] = {}
_sync_stop_events: dict[int, threading.Event] = {}
# Why a cancel was requested, set by cancel_sync and consumed by _mark_sync_cancelled.
# The cancelling caller and the thread that writes the cancelled state are different
# threads, so the reason has to travel out-of-band or the stored message lies about
# who cancelled (a recording preempting a sync is not "cancelled by user").
_sync_cancel_reasons: dict[int, str] = {}
# Live progress for an in-flight sync - {'phase': 'channels'|'epg', 'done': int,
# 'total': int|None}. 'total' is None for the EPG phase: the XMLTV parse is a streaming
# iterparse with no upfront count. Read by dashboard.py's nav-status chip (Product
# Principle 1 - a sync that's hung looks identical to one that's healthy without this).
_sync_progress: dict[int, dict] = {}
_sync_locks_mutex = threading.Lock()
# One refresh of a source at a time, whichever path started it: its owner's sync, its own
# job, or Refresh now. Admission keeps two syncs apart, but a manual Sync now is forced past
# admission, so it can meet a source refresh that started first (dev/changelog/1104).
_source_locks: dict[int, threading.Lock] = {}

_DEFAULT_CANCEL_REASON = 'Cancelled by user'


class SyncCancelled(Exception):
    """A sync phase saw its stop event and stopped where it stood.

    Raised rather than returned so a cancel can unwind out of the middle of a phase
    without every layer between it and _do_sync having to carry a sentinel back up.
    Safe inside a retry_on_locked closure: that decorator only catches OperationalError,
    so this propagates untouched and the closure's own uncommitted work is discarded by
    the rollback _mark_sync_cancelled does first.

    The message is the phase's own account of what it left behind, which _do_sync appends
    to the stored cancellation reason. It differs per phase and the difference matters to
    the user: stopping a channel upsert saves nothing at all, stopping the EPG import
    before its delete keeps the previous guide, and stopping it after that delete does not.
    """


# What a stopped phase left behind, appended to the stored cancellation reason. Named
# constants because both channel-sync branches of _do_sync would otherwise spell the same
# sentence twice, and because the wording is the user-facing half of the cancel.
_CANCEL_LEFT_NOTHING = 'no changes were saved.'
_CANCEL_KEPT_EPG = ('the channel list was updated; the EPG import stopped before it changed '
                    'anything, so the previous guide data was kept.')
# How many <programme> elements the collapse guard's count pass reads between cancel polls.
# It matches the import loop's own 500-entry checkpoint so both phases answer a cancel with
# the same responsiveness, and it is a count of elements read rather than of entries matched
# so a feed whose channels mostly do not match still polls.
_CANCEL_POLL_PROGRAMS = 500


def _raise_if_cancelled(stop_event: threading.Event | None, detail: str = '') -> None:
    """Stop this sync here if it has been cancelled. `detail` describes what was left behind.

    Event.is_set() is a plain attribute read with no lock and no I/O, which is what makes
    it safe to call from inside the per-row loops (CLAUDE.md "no hidden I/O in per-row
    loops") - though every caller in this module still throttles it to a checkpoint that
    already exists rather than paying it 80,000 times.
    """
    if stop_event is not None and stop_event.is_set():
        raise SyncCancelled(detail)


def _get_source_lock(source_id: int) -> threading.Lock:
    with _sync_locks_mutex:
        return _source_locks.setdefault(source_id, threading.Lock())


def _get_sync_lock(account_id: int) -> threading.Lock:
    with _sync_locks_mutex:
        if account_id not in _sync_locks:
            _sync_locks[account_id] = threading.Lock()
        return _sync_locks[account_id]


def _set_sync_progress(account_id: int, phase: str, done: int, total: int | None) -> None:
    with _sync_locks_mutex:
        _sync_progress[account_id] = {'phase': phase, 'done': done, 'total': total}


def get_sync_progress(account_id: int) -> dict | None:
    """Live progress for account_id's in-flight sync, or None if there's nothing tracked
    yet (not syncing, or too early in the sync to have reached a checkpoint)."""
    with _sync_locks_mutex:
        return _sync_progress.get(account_id)


def sync_signature() -> str:
    """A short string that changes whenever any account's sync starts, finishes, fails or is
    cancelled. /api/nav-status carries it and /accounts renders the one its rows were read
    at, so the list page can tell it has gone stale without polling a page of its own
    (dev/changelog/921).

    Both halves are needed. The syncing set moves when a sync starts or ends; the newest
    sync-log id moves when one starts, which is the only trace left by a sync that starts
    and fails inside a single poll interval - the syncing set is the same at both polls."""
    syncing = (db.session.query(Account.id)
               .filter(Account.status == 'SYNCING')
               .order_by(Account.id).all())
    latest_log_id = db.session.query(func.max(AccountSyncLog.id)).scalar() or 0
    return f"{','.join(str(account_id) for (account_id,) in syncing)}|{latest_log_id}"


def _request_headers(cfg: dict | None = None) -> dict:
    if cfg is None:
        cfg = load_config()
    ua = cfg.get('http', {}).get('user_agent', 'VLC/3.0.18 LibVLC/3.0.18')
    return {'User-Agent': ua}


# ── M3U parser (also used as a fallback for non-standard Xtream servers) ─────

def _parse_m3u_as_streams(content: str) -> list:
    """Parse an M3U playlist into stream dicts, excluding unambiguous VOD.

    The only filtering done here is the conservative, NEGATIVE-ONLY VOD-path exclusion
    (DESIGN-live-vod.md §5): a /movie/ or /series/ path segment is VOD, host-independent.
    There is deliberately no positive "this is live" rule - requiring /live/ silently wipes
    the live channels of any provider serving rootless or HLS URLs, and one real account
    (Account 2) mixes /live/ and rootless shapes in a single playlist.

    Authoritative live-vs-VOD classification does NOT happen here: for Xtream accounts it is
    the provider's own get_live_streams response, applied by
    xtream_client.classify_live_streams(). This parser is only the playlist reader.

    Each entry records `_id_source` ('cuid' | 'url' | 'hash') describing how stream_id was
    derived. The classifier needs it: a synthesized hash id must never be accepted as a
    match against a real live stream_id.
    """
    from urllib.parse import urlparse

    streams = []
    skipped_vod = 0
    lines = content.splitlines()
    i = 0
    while i < len(lines):
        line = lines[i].strip()
        if line.startswith('#EXTINF:'):
            attrs = {}
            for m in re.finditer(r'([\w-]+)="([^"]*)"', line):
                attrs[m.group(1)] = m.group(2)
            display_name = line.rsplit(',', 1)[-1].strip() if ',' in line else ''

            # Advance to the next non-empty, non-comment line (the URL)
            i += 1
            while i < len(lines) and (not lines[i].strip() or lines[i].strip().startswith('#')):
                i += 1

            if i < len(lines):
                url = lines[i].strip()
                if url:
                    url_path = urlparse(url).path.lower()

                    if '/movie/' in url_path or '/series/' in url_path:
                        skipped_vod += 1
                        i += 1
                        continue

                    # CUID attribute (present on some providers) maps to the JSON API stream_id.
                    # Prefer it so stream_ids stay consistent across M3U and JSON API syncs.
                    # This precedence is load-bearing for classification, not cosmetic: one
                    # real account's URL numeric tails do NOT equal its stream_ids, so
                    # checking the URL first drops all but 157 of its 13,106 channels
                    # (DESIGN-live-vod.md §3.1). Do not reorder.
                    cuid = attrs.get('CUID', '')
                    if cuid.isdigit():
                        stream_id, id_source = int(cuid), 'cuid'
                    else:
                        sid_match = re.search(r'/(\d+)(?:\.ts|\.m3u8)?(?:\?.*)?$', url)
                        if sid_match:
                            stream_id, id_source = int(sid_match.group(1)), 'url'
                        else:
                            stream_id = int(hashlib.md5(url.encode()).hexdigest()[:8], 16)
                            id_source = 'hash'

                    streams.append({
                        'stream_id': stream_id,
                        'name': attrs.get('tvg-name', display_name) or display_name,
                        'stream_icon': attrs.get('tvg-logo', ''),
                        'epg_channel_id': attrs.get('tvg-id', ''),
                        'category_name': attrs.get('group-title', ''),
                        'category_id': '',
                        '_stream_url': url,
                        '_id_source': id_source,
                    })
        i += 1

    if skipped_vod:
        log.info('M3U parse: excluded %d entry(s) on an unambiguous /movie/ or /series/ path',
                 skipped_vod)
    return streams


# ── XMLTV datetime parsing ────────────────────────────────────────────────────

@functools.lru_cache(maxsize=65536)
def _parse_xmltv_dt(s: str) -> datetime:
    """Parse XMLTV datetime like '20250621143000 -0500' to naive UTC datetime.

    Memoized: a feed repeats the same few thousand grid timestamps across every channel,
    and strptime was over a third of an import's wall clock - 1.15M calls for 498,666
    programs, since the scan pass and the import loop each parse every listing
    (dev/changelog/1101). A pure function of an immutable string, returning an immutable
    value, so the cache cannot change an answer."""
    m = re.match(r'(\d{14})\s*([+-]\d{4})', s)
    if not m:
        # Try without timezone offset - treat as UTC
        m2 = re.match(r'(\d{14})', s)
        if m2:
            return datetime.strptime(m2.group(1), '%Y%m%d%H%M%S')
        raise ValueError(f'Cannot parse XMLTV datetime: {s!r}')
    naive = datetime.strptime(m.group(1), '%Y%m%d%H%M%S')
    sign = 1 if m.group(2)[0] == '+' else -1
    h = int(m.group(2)[1:3])
    mi = int(m.group(2)[3:5])
    offset = timezone(timedelta(hours=sign * h, minutes=sign * mi))
    aware = naive.replace(tzinfo=offset)
    return to_naive_utc(aware)


# ── URL normalization ─────────────────────────────────────────────────────────

def normalize_url_loose(url: str) -> str:
    """Strip the /live/ path prefix and .ts/.m3u8 extension. Unconditional - used for
    loose recording↔channel URL MATCHING regardless of per-account normalization.

    Deliberately left as the old blind regex: it produces a comparison *key*, never a URL
    that gets stored or played, and both sides of every comparison go through it. Do not
    "fix" it to use parse_stream_url_parts - a key only has to be consistent, and changing
    it would fail to match recordings whose url was stored under the old behavior."""
    url = re.sub(r'/live/', '/', url)
    url = re.sub(r'\.(ts|m3u8)$', '', url)
    return url


# ── URL normalization modes (spec: changelog/258 "Spec") ─────────────────────

NORM_DISABLED = 'disabled'
NORM_MPEGTS = 'mpegts'            # http://host/AAA/BBB/1        (recommended - see below)
NORM_MPEGTS_LIVE = 'mpegts_live'  # http://host/live/AAA/BBB/1.ts
NORM_HLS = 'hls'                  # http://host/live/AAA/BBB/1.m3u8

# Label shown in the UI; the example is rendered live under the dropdown.
NORM_MODES = [
    (NORM_DISABLED,    'Disabled',              ''),
    (NORM_MPEGTS,      'MPEG-TS without live',  'http://example.com/AAA/BBB/1'),
    (NORM_MPEGTS_LIVE, 'MPEG-TS with live',     'http://example.com/live/AAA/BBB/1.ts'),
    (NORM_HLS,         'HLS',                   'http://example.com/live/AAA/BBB/1.m3u8'),
]
_VALID_MODES = {m for m, _l, _e in NORM_MODES}


def norm_mode_label(mode: str) -> str:
    """Human label for a mode, for UI text and alert bodies. Unknown/empty returns '' so
    callers can treat "no mode selected" as a blank rather than special-casing it."""
    return next((label for value, label, _e in NORM_MODES if value == mode and mode), '')


def norm_mode_example(mode: str) -> str:
    """Example URL for a mode. Same contract as norm_mode_label - NORM_MODES stays the
    single source for both, so adding a mode never needs a second table edited."""
    return next((ex for value, _l, ex in NORM_MODES if value == mode and mode), '')

# [/live/]<user>/<pass>/<numeric id>[.ts|.m3u8] - the shape shared by all three spellings.
# The stream id is always numeric (confirmed against real accounts: never seen otherwise), and that is
# what makes the match safe: a path ending in a non-numeric segment is some other kind of
# stream URL entirely and must not be touched.
_STREAM_URL_RE = re.compile(
    r'^/(?:live/)?(?P<user>[^/]+)/(?P<password>[^/]+)/(?P<sid>\d+)(?:\.(?:ts|m3u8))?$'
)


# A legacy boolean survives a round-trip through the (now String) url_normalization column
# as 0/1 or '0'/'1' - SQLite's dynamic typing stores whatever it is handed. Mapping only the
# Python bools would give one value two meanings depending on whether it had been to the DB
# yet: `False` in memory reads as "disabled", the same row re-read reads as 0 -> "defer to
# the global default", which is a different mode entirely (CLAUDE.md "one flag, one meaning").
_LEGACY_TRUE = {'true', '1'}
_LEGACY_FALSE = {'false', '0'}


def coerce_normalization_mode(value) -> str | None:
    """Accept a mode string, or a legacy boolean from a pre-dropdown config/DB row.

    True was the old "enabled", whose only behavior was today's MPEG-TS-without-live form,
    so it maps there and an existing install keeps the exact URLs it already has."""
    if value is None or value == '':
        return None
    # str() first, deliberately: every legacy spelling is compared in one place. A bool that
    # has been to the DB and back comes out as 0/1, not True/False, so matching only the
    # Python bools would give one stored value two meanings.
    value = str(value).strip().lower()
    if value in _VALID_MODES:
        return value
    if value in _LEGACY_TRUE:
        return NORM_MPEGTS
    if value in _LEGACY_FALSE:
        return NORM_DISABLED
    return None


def parse_stream_url_parts(url: str) -> tuple[str, str, str, str] | None:
    """(origin, user, password, stream_id) for a URL in any of the three known spellings,
    or None when it carries no user/pass/id triplet to rebuild from.

    Returning None is the whole safety mechanism. Roughly 1% of real channels are
    third-party streams the provider aggregated into its lineup (Pluto TV, CloudFront,
    standalone radio) which have no triplet at all - there is literally nothing to rebuild
    them from, so they must be handed back untouched rather than mangled. The pre-dropdown
    normalizer had no such check and broke 9 real channels by stripping a meaningful
    .m3u8/.ts from them."""
    if not url or '://' not in url:
        return None
    from urllib.parse import urlsplit
    parts = urlsplit(url)
    if not parts.scheme or not parts.netloc:
        return None
    m = _STREAM_URL_RE.match(parts.path)
    if not m:
        return None
    return (f'{parts.scheme}://{parts.netloc}',
            m.group('user'), m.group('password'), m.group('sid'))


def build_stream_url(parts: tuple[str, str, str, str], mode: str) -> str:
    """Render parsed parts in the requested spelling.

    NOTE: when `parts` came from parse_stream_url_parts, it carries the origin and
    credentials the PROVIDER put in the stream URL, and they are reproduced verbatim.
    Never rebuild those from Account.base_url / username / password - on two of four real
    test accounts ZERO stream URLs sit on the account's configured host (account 2's base
    is one domain while its streams are served from a CDN on another; account 1 is an M3U
    account with no base_url at all), so substituting account credentials would break every
    channel on them.

    The one sanctioned exception is construct_stream_url() below, which builds from account
    settings precisely because the provider supplied no URL to preserve."""
    origin, user, password, sid = parts
    if mode == NORM_MPEGTS:
        return f'{origin}/{user}/{password}/{sid}'
    if mode == NORM_MPEGTS_LIVE:
        return f'{origin}/live/{user}/{password}/{sid}.ts'
    if mode == NORM_HLS:
        return f'{origin}/live/{user}/{password}/{sid}.m3u8'
    raise ValueError(f'unknown normalization mode: {mode!r}')


def resolve_normalization_mode(account: Account, cfg: dict = None) -> str:
    """The effective mode for an account: its own setting, else the global default.

    Pass a pre-loaded `cfg` at any call site inside a per-row loop - otherwise this
    reads+parses config.yaml from disk on every call when the account defers to the
    global default, reintroducing O(rows) disk I/O (dev/docs/BUGS.md 2026-07-15 10:34, originally
    via render_filename_template on the same routes)."""
    mode = coerce_normalization_mode(getattr(account, 'url_normalization', None))
    if mode is None:
        if cfg is None:
            cfg = load_config()
        mode = coerce_normalization_mode(
            cfg.get('sync', {}).get('url_normalization')) or NORM_DISABLED
    return mode


class StreamUrlConstructionBlocked(Exception):
    """The provider supplied no stream URLs and no normalization mode is set to build one in.

    There is no safe default to fall back on. The three spellings are NOT interchangeable
    across providers - a trailing `.ts` is frequently blocked by Cloudflare - so guessing
    would hand back an entire account's worth of channels that look imported but cannot
    record. The sync stops and says so instead (DESIGN-live-vod.md §4.3)."""


def stream_origin_from_server_info(auth_payload: dict) -> str | None:
    """Where the provider says its STREAMS live, from the auth response's `server_info`.

    **This is not the API base and must never be used as one.** An Xtream account has two
    independent endpoints, and they routinely differ:

      * `Account.base_url` - where the API/playlist/EPG are fetched from (`player_api.php`,
        `get.php`, `xmltv.php`). Whatever the user typed when adding the account.
      * `server_info.{url,port,https_port,server_protocol}` - where to tune in the channels.

    Real example: account 2's API is served over **https** while every one of its 12,696
    stream URLs is plain **http**. Neither is wrong; they are different services. Feeding
    `server_info` back into the API base would downgrade that account's credential-bearing
    API calls from TLS to cleartext, which is why this returns a stream origin only.

    Returns None when the provider declares nothing usable (one real dump carries no
    `server_info` key at all), leaving the caller to fall back.

    The port is reproduced verbatim, including a default one: when we are generating URLs
    rather than being given them, the provider's own statement of where to connect is the
    only evidence there is, so it is copied rather than second-guessed."""
    server_info = (auth_payload or {}).get('server_info') or {}
    host = str(server_info.get('url') or '').strip().rstrip('/')
    if not host:
        return None
    if '://' in host:
        host = host.split('://', 1)[1]
    protocol = str(server_info.get('server_protocol') or 'http').strip().lower()
    if protocol not in ('http', 'https'):
        # Panels have been seen echoing junk here; an unusable scheme means fall back
        # rather than emit a URL nothing can open.
        return None
    port = server_info.get('https_port' if protocol == 'https' else 'port')
    port = str(port or '').strip()
    if port and ':' not in host:
        host = f'{host}:{port}'
    return f'{protocol}://{host}'


def _parse_bool_flag(value) -> bool | None:
    """Xtream sends is_trial as the string "1"/"0" (occasionally a real bool). None if
    absent or unrecognized - never guess a value the provider didn't actually send."""
    if isinstance(value, bool):
        return value
    if value is None:
        return None
    s = str(value).strip()
    if s in ('1', 'true', 'True'):
        return True
    if s in ('0', 'false', 'False'):
        return False
    return None


def _parse_int_flag(value) -> int | None:
    """Xtream sends max_connections/active_cons as numeric strings ("1", "0"), not native
    ints. None if absent/unparseable."""
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def parse_provider_account_info(auth_payload: dict) -> dict:
    """The account-level facts in the auth response's `user_info` block that this app used
    to read and discard (dev/changelog/534) - subscription status, expiry, trial flag,
    connection entitlement, and allowed output formats. `.get()` throughout: providers
    disagree on which fields they send, and the M3U-for-auth path synthesizes a bare
    `{'user_info': {'auth': 1}}` with nothing else in it.

    Returns a dict of `provider_*` Account column values (minus the prefix), all None
    where the provider didn't say. `allowed_output_formats` is informational only - it is
    never a rule for building stream URLs (DESIGN-live-vod.md §4.3)."""
    user_info = (auth_payload or {}).get('user_info') or {}
    status = user_info.get('status')
    formats = user_info.get('allowed_output_formats')
    return {
        'status': str(status) if status is not None else None,
        'exp_date': parse_epoch_utc(user_info.get('exp_date')),
        'is_trial': _parse_bool_flag(user_info.get('is_trial')),
        'max_connections': _parse_int_flag(user_info.get('max_connections')),
        'active_connections': _parse_int_flag(user_info.get('active_cons')),
        'allowed_output_formats': ','.join(formats) if isinstance(formats, list) else None,
    }


def construct_stream_url(account: Account, stream_id, mode: str,
                         origin: str | None = None) -> str:
    """Build a stream URL from scratch for a provider that supplied none.

    This is the ONLY sanctioned use of the account's own host and credentials inside a
    stream URL (DESIGN-live-vod.md §4.3). It applies when the provider's playlist
    endpoint is unavailable so only its catalog could be read, leaving no provider-supplied
    URL to preserve. Never call it to "fix up" a URL the provider did supply.

    `origin` is the provider's own declaration of where its streams live
    (stream_origin_from_server_info). It is preferred over `base_url` because base_url is
    the API endpoint, which is a different service and may legitimately use a different
    scheme, host, or port. base_url is the fallback for a provider that declares nothing -
    it is a guess, and the sync says so.

    A base_url fallback is passed to the builder verbatim (minus a trailing slash): some
    panels are hosted under a path, and that path is part of where the stream lives."""
    if mode == NORM_DISABLED:
        raise StreamUrlConstructionBlocked(
            'This provider supplied no stream URLs, so ChannelBin has to build them, but '
            'URL Normalization is set to Disabled - there is no form to build them in. Set '
            'URL Normalization on this account (or as the global default) and sync again. '
            'Choose "MPEG-TS without live" if you are unsure.'
        )
    origin = (origin or account.base_url or '').rstrip('/')
    return build_stream_url(
        (origin, account.username, account.password, str(stream_id)), mode)


def normalize_url_with_mode(url: str, mode: str) -> str:
    """normalize_url's inner half, for callers that already resolved the mode.

    A row loop MUST use this and resolve the mode once outside the loop - resolving it per
    row falls through to load_config() on every account that defers to the global default
    (CLAUDE.md "no hidden I/O in per-row loops")."""
    if mode == NORM_DISABLED:
        return url
    parts = parse_stream_url_parts(url)
    if parts is None:
        return url
    return build_stream_url(parts, mode)


def normalize_url(url: str, account: Account, cfg: dict = None) -> str:
    """Rewrite a stream URL into the account's chosen spelling.

    Only the SHAPE changes. A URL with no user/pass/id triplet is returned untouched in
    every mode - see parse_stream_url_parts."""
    return normalize_url_with_mode(url, resolve_normalization_mode(account, cfg))


def url_is_normalizable(url: str) -> bool:
    """Whether normalization can act on this URL at all. Pure - no I/O, safe in a row loop.

    Drives the "couldn't be normalized" badge: a channel whose account has a mode selected
    but whose URL has no triplet is silently left alone, and the user should be able to see
    that rather than wonder why it kept its old form.

    A shipped migration step calls this to backfill channels.url_normalizable, so editing it
    changes what a not-yet-migrated database gets stamped - deliberate, since the column is
    defined as this function's answer, but see migrations.py's module docstring before
    assuming an edit here stays inside this module (dev/changelog/688)."""
    return parse_stream_url_parts(url) is not None


# ── Filename template ─────────────────────────────────────────────────────────

TEMPLATE_VARIABLES = [
    ('{date}',        'Recording date (YYYY-MM-DD)'),
    ('{title}',       'Program title'),
    ('{sub_title}',   'Program sub-title / episode title'),
    ('{description}', 'Program description (truncated to 80 chars)'),
    ('{channel}',     'Channel name'),
    ('{category}',    'Channel or program category'),
    ('{start_time}',  'Program start time (HHMM, 24-hour)'),
    ('{end_time}',    'Program end time (HHMM, 24-hour)'),
]


def tag_template_variables() -> list[tuple[str, str]]:
    """Per-tag chip for the template editor: {tag:name} for every Tag row."""
    from .database import Tag
    return [
        (
            '{tag:' + tag.name + '}',
            f'Insert "{tag.name}" if the "{tag.name}" tag matched, else nothing',
        )
        for tag in Tag.query.order_by(Tag.name).all()
    ]


_TAG_TOKEN_RE = re.compile(r'\{tag:([\w-]+)\}')


def tags_matching(tags, *texts) -> list:
    """The tags whose pattern(s) appear (case-insensitive substring) across `texts`.

    One definition, because a tag has to mean the same thing wherever it is shown: the TV
    Guide's program cells, the EPG deep search and the channel search's airing rows all badge
    programs with it. `app/channel_search.py::_tag_predicate` is the SQL form of the same
    question for the CHANNEL grain ("does this channel air anything carrying the tag") and is
    deliberately separate - it answers about a channel, this answers about one program.
    """
    combined = ' '.join(t for t in texts if t).lower()
    if not combined:
        return []
    return [{'id': tag.id, 'name': tag.name, 'color': tag.color} for tag in tags
            if any(p.pattern.lower() in combined for p in tag.patterns if p.pattern)]


def filename_tag_cleanup(cfg) -> list:
    """The (tag_name, mode) cleanup list for `render_filename_template`, from config.

    `render_filename_template(tag_cleanup=None)` does this lookup itself via `load_config()`,
    which is a per-call config read - so every caller that renders more than one name computes
    it ONCE and passes it in. Up to 1,000 program rows in a single guide or search response
    made that the difference between a cheap lookup and an O(programs) cost.
    """
    rec_cfg = cfg.get('recording', {})
    return ([(name, 'remove') for name in rec_cfg.get('filename_tags_remove', [])]
            + [(name, 'replace') for name in rec_cfg.get('filename_tags_replace', [])])


def effective_filename_template(cfg, channel) -> str:
    """The template a recording of this channel would be named with: the channel's default
    profile's own template if it sets one, else the global recording.filename_template."""
    global_template = cfg.get('recording', {}).get(
        'filename_template', config_default('recording.filename_template'))
    profile = getattr(channel, 'default_profile', None)
    if profile is not None and profile.filename_template:
        return profile.filename_template
    return global_template


def render_filename_template(template: str, program: dict, tag_cleanup: list = None,
                             tags_by_name: dict = None, tz=None) -> str:
    """Substitute EPG program data into a filename template string.

    program dict keys: title, description, channel_name, category,
                       start_time (datetime UTC naive), stop_time (datetime UTC naive)

    {tag:name} placeholders are also supported, resolved against Tag/TagPattern rows by
    name: conditional insert - renders as the tag's plain name if any of its patterns is
    found in title/sub_title/description, else empty string.

    tag_cleanup: optional list of (tag_name, mode) pairs, mode is 'remove' or 'replace',
    applied as a final pass over the fully-rendered result (catching the tag's pattern
    anywhere in the output, not just in title/sub_title/description). If omitted, falls
    back to the persisted recording.filename_tags_remove / filename_tags_replace config.

    tags_by_name: MANDATORY at any call site inside a per-row loop. Both the {tag:...}
    resolution and the cleanup pass otherwise run their own Tag query per call, so
    rendering a page of 100 showings issues up to 200 of them - the no-hidden-I/O rule in
    CLAUDE.md, and not hypothetical: /api/channels/search's scaling test goes red the
    moment a cleanup list is non-empty, which the filename designer makes a one-modal
    change (dev/changelog/441). Pass the already-loaded {name: Tag} map and this function
    issues no queries at all.

    tz: the zoneinfo `{date}`/`{start_time}`/`{end_time}` are rendered in. MANDATORY at any
    call site inside a per-row loop, for the same reason as tags_by_name - omitting it
    calls tz_utils.get_display_tz() (a load_config()) on every call. If omitted, falls back
    to the display timezone (dev/docs/BUGS.md 2026-08-05).
    """
    def _tags(names):
        if tags_by_name is not None:
            return {n: tags_by_name[n] for n in names if n in tags_by_name}
        from .database import Tag
        return {t.name: t for t in Tag.query.filter(Tag.name.in_(names)).all()}

    from .tz_utils import UTC, get_display_tz
    if tz is None:
        tz = get_display_tz()

    def _local(dt):
        if dt is None:
            return None
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=UTC)
        return dt.astimezone(tz)

    start = _local(program.get('start_time'))
    stop = _local(program.get('stop_time'))
    title = program.get('title', '') or ''
    sub_title = program.get('sub_title', '') or ''
    description = program.get('description', '') or ''

    tag_names: set[str] = {m.group(1) for m in _TAG_TOKEN_RE.finditer(template)}

    tag_subs: dict[str, str] = {}
    if tag_names:
        token_tags = _tags(tag_names)
        for name in tag_names:
            tag = token_tags.get(name)
            patterns = [p.pattern for p in tag.patterns] if tag else []
            matched = any(p in title or p in sub_title or p in description for p in patterns)
            tag_subs['{tag:' + name + '}'] = tag.name if (tag and matched) else ''

    subs = {
        'date': start.strftime('%Y-%m-%d') if start else '',
        'title': title,
        'sub_title': sub_title,
        'description': description[:80],
        'channel': program.get('channel_name', ''),
        'category': program.get('category', ''),
        'start_time': start.strftime('%H%M') if start else '',
        'end_time': stop.strftime('%H%M') if stop else '',
    }
    result = template
    for key, val in subs.items():
        safe_val = re.sub(r'[\\/:*?"<>|]', '_', str(val)).strip()
        result = result.replace('{' + key + '}', safe_val)
    for token, val in tag_subs.items():
        result = result.replace(token, val)

    if tag_cleanup is None:
        cfg = load_config().get('recording', {})
        tag_cleanup = (
            [(name, 'remove') for name in cfg.get('filename_tags_remove', [])] +
            [(name, 'replace') for name in cfg.get('filename_tags_replace', [])]
        )
    if tag_cleanup:
        cleanup_tags_by_name = _tags({name for name, _ in tag_cleanup})
        # ONE alternation pass over every pattern, never a str.replace loop per tag. A
        # sequential loop rescans what it just wrote, so a tag whose NAME contains one of
        # its own PATTERNS rewrites its own output: `UHD 4K` with patterns 2160p/UHD/4K
        # turned "Show 2160p" into "Show UHD UHD 4K UHD 4K". Longest pattern first, so an
        # overlapping shorter pattern cannot win the alternation and leave a tail behind.
        # `remove` mode was never affected - replacing with '' cannot re-match.
        # dev/docs/BUGS.md 2026-08-03; dev/changelog/441.
        replacements: dict[str, str] = {}
        for name, mode in tag_cleanup:
            tag = cleanup_tags_by_name.get(name)
            if not tag:
                continue
            for p in tag.patterns:
                if p.pattern:
                    replacements[p.pattern] = '' if mode == 'remove' else tag.name
            # In `replace` mode the tag's own NAME maps to itself. One alternation pass
            # alone stops the cascade from growing without bound, but text that ALREADY
            # reads as the tag name still gets torn apart by the patterns inside it -
            # "Show UHD 4K" becoming "Show UHD 4K UHD 4K", which is the same defect
            # arriving from the other direction. Registering the name means the longest
            # alternative matches it first and consumes it, so `2160p` still normalizes to
            # `UHD 4K` and an already-normalized name is left alone.
            if mode != 'remove' and tag.name:
                replacements.setdefault(tag.name, tag.name)
        if replacements:
            alternation = '|'.join(
                re.escape(p) for p in sorted(replacements, key=len, reverse=True))
            result = re.sub(alternation, lambda m: replacements[m.group(0)], result)

    # Collapse repeated " - "-style separators left behind by empty substitutions
    # (e.g. a non-matching {tag:x} producing "Show -  - Channel") and trim stray
    # leading/trailing separators.
    result = re.sub(r'(?:\s*-\s*){2,}', ' - ', result)
    result = re.sub(r'^\s*-\s*|\s*-\s*$', '', result)
    return result.strip()


# ── Account sync ──────────────────────────────────────────────────────────────

def sync_account(app, account_id: int, use_dump: bool = False, force_epg_resync: bool = False,
                 force_admission: bool = False):
    """Sync channels and EPG data for one account (M3U or Xtream).

    Safe to call from a background thread or APScheduler job.
    Uses a per-account lock so concurrent sync calls don't overlap.
    Pass use_dump=True to read from dump files instead of the live API (Xtream debug only).
    Pass force_epg_resync=True to bypass the EPG collapse guard for this one sync
    (DESIGN-sync-resilience.md §4 "Force EPG Resync" override) - manual-action only, the
    scheduled sync job never sets it.

    **Admission** (app/admission.py, dev/changelog/679). The decision to start lives here
    rather than in the caller, because it has to be made in the same locked breath as the
    registration - a caller that asks "is anything running?" and then calls this has rebuilt
    the check-then-act race the registry exists to close. A sync yields to an active test
    run (the polarity settled in DESIGN-concurrency.md §5.2) and to another in-flight sync.

    Returns an `admission.Refusal` when the sync did not run because something else holds
    the axis - the caller decides the surface (the scheduled job defers and retries) -
    and None otherwise, including when the per-account lock was already held.
    `force_admission=True` registers without asking: manual sync is a present user's call
    after the warn-and-override flow (§5.4), and it still registers so everything that
    yields to a sync can see it.
    """
    lock = _get_sync_lock(account_id)
    if not lock.acquire(blocking=False):
        log.info('Sync for account %d already in progress, skipping', account_id)
        return None

    ticket = None
    try:
        ticket = admission.try_start(admission.KIND_SYNC, f'account {account_id}',
                                     force=force_admission)
        if not ticket.granted:
            return ticket

        stop_event = threading.Event()
        with _sync_locks_mutex:
            _sync_threads[account_id] = threading.current_thread()
            _sync_stop_events[account_id] = stop_event
        try:
            with app.app_context():
                _do_sync(account_id, stop_event, use_dump=use_dump,
                         force_epg_resync=force_epg_resync)
        finally:
            with _sync_locks_mutex:
                _sync_threads.pop(account_id, None)
                _sync_stop_events.pop(account_id, None)
                # A cancel signalled after the sync's last stop-check leaves a reason behind;
                # drop it so it can't outlive the run it described.
                _sync_cancel_reasons.pop(account_id, None)
                _sync_progress.pop(account_id, None)
        return None
    finally:
        admission.release(ticket)
        lock.release()


def sync_conflicts(account_id: int) -> list[str]:
    """Reasons a manual sync for this account is a bad idea right now, as user-facing prose.

    Empty list = no conflict. This is the ONE implementation of the check shared by both
    manual entry points (routes/accounts.py::sync_account_now and routes/jobs.py::run_job_now)
    per DESIGN-concurrency.md 5.4 - manual sync warns and lets the user override, where the
    scheduled job (scheduler.py::_account_sync_job) hard-skips the same three conditions
    because nobody is present to decide. Both honor the same config toggles, so turning a
    guard off does not leave manual stricter than scheduled.

    Each string names the conflict AND its consequence: the user is being asked to decide,
    so "a recording is in progress" alone does not tell them what they are risking.
    """
    # Re-imported locally, not taken from the module-top binding, so tests can patch
    # app.config.load_config and have it reach this function (CLAUDE.md Testing).
    from .config import load_config
    from . import channel_tester

    sync_cfg = load_config().get('sync', {})
    conflicts = []

    if sync_cfg.get('skip_sync_if_recording_active', True):
        active = Recording.query.filter_by(status=REC_STATUS_IN_PROGRESS).first()
        if active:
            conflicts.append(
                f'A recording is in progress ("{active.name}") - syncing now may count '
                "against this account's connection limit."
            )

    within_minutes = sync_cfg.get('skip_sync_if_recording_within_minutes', 5)
    if within_minutes and within_minutes > 0:
        cutoff = datetime.utcnow() + timedelta(minutes=within_minutes)
        upcoming = Recording.query.filter(
            Recording.status == REC_STATUS_SCHEDULED,
            Recording.start_time <= cutoff,
        ).order_by(Recording.start_time).first()
        if upcoming:
            conflicts.append(
                f'A recording starts within {within_minutes} minutes ("{upcoming.name}") - '
                'a sync running into its start may count against the connection limit.'
            )

    if channel_tester.is_running():
        conflicts.append(
            'A channel test run is in progress - both open provider connections, and '
            'syncing now may cause tests to fail.'
        )

    # Advisory only, which is what active_kinds() is for - the manual sync goes ahead
    # regardless once the user confirms, so there is no decision here to race.
    if admission.KIND_SYNC in admission.active_kinds():
        conflicts.append(
            'Another account is already syncing - two syncs write the same database at '
            'once, so both will be slower.'
        )

    return conflicts


def cancel_sync(account_id: int, reason: str = _DEFAULT_CANCEL_REASON) -> str:
    """Signal a running sync to stop, or report orphaned state for the caller to reset.

    `reason` is what the account row and the sync log will show; it must describe who
    actually cancelled (see _sync_cancel_reasons).

    Returns 'cancelled' if a live thread was signalled, 'reset' if state was orphaned.
    """
    with _sync_locks_mutex:
        stop_event = _sync_stop_events.get(account_id)
        thread = _sync_threads.get(account_id)

    if stop_event is not None and thread is not None and thread.is_alive():
        with _sync_locks_mutex:
            _sync_cancel_reasons[account_id] = reason
        stop_event.set()
        log.info('Cancel signal sent to sync thread for account %d', account_id)
        return 'cancelled'

    log.info('No live sync thread for account %d - orphaned SYNCING state', account_id)
    return 'reset'


def stop_sync_and_wait(account_id: int, reason: str, timeout: float) -> bool:
    """Signal any running sync for this account to stop, then wait for its thread to exit.

    True when no sync thread is running afterward - either none was, or it stopped inside
    `timeout` seconds. False means one is still alive, and a caller about to delete this
    account's rows must not proceed: the thread holds no reference to the row it writes
    and SQLite foreign keys are off, so it would go on inserting channels for an account
    that no longer exists.

    A False is not a failure to escalate past. A sync now honors its stop event inside the
    channel-upsert and EPG-import loops as well as between phases (dev/changelog/720), so
    most cancels land in well under `timeout` - but a checkpoint can still be a batch commit
    or a provider fetch away, and the caller is holding a delete that must not race a live
    writer. Refusing and letting the caller retry stays the answer, not a longer wait.
    """
    with _sync_locks_mutex:
        thread = _sync_threads.get(account_id)

    # No live thread means nothing to stop. Checked here rather than leaving it to
    # cancel_sync so a routine call doesn't log its 'orphaned SYNCING state' line.
    if thread is None or not thread.is_alive():
        return True

    cancel_sync(account_id, reason)
    thread.join(timeout)
    return not thread.is_alive()


def record_skipped_sync(account_id: int, reason: str) -> None:
    """Record that a scheduled sync for this account did not run, and why.

    A skipped occurrence is part of an account's sync history, so it lands where the rest of
    that history already lives rather than in a surface of its own: the account page's
    Activity renders AccountSyncLog rows directly, so writing one here IS the surface
    (dev/changelog/928). Without it a sync that yields to a recording or to database
    contention leaves no trace on the account at all.

    `completed_at` is set at insert, so this row can never be mistaken for the open
    IN_PROGRESS row finalize_sync_state() closes, and the status is outside the
    SUCCESS/PARTIAL pair every "a sync that actually ran" aggregate filters on - so a skip
    moves no count, no duration estimate and no first-sync-era decision.
    """
    @retry_on_locked()
    def _add_and_commit():
        now = datetime.utcnow()
        db.session.add(AccountSyncLog(
            account_id=account_id,
            started_at=now,
            completed_at=now,
            status='SKIPPED',
            channels_synced=0,
            epg_entries_synced=0,
            error_message=reason,
        ))
        db.session.commit()

    _add_and_commit()


def finalize_sync_state(account_id: int, account_status: str, log_status: str,
                         account_message: str, log_message: str,
                         sync_log_id: int | None = None) -> None:
    """Set an account's terminal sync status and close its open AccountSyncLog.

    Mutates only - the caller owns its own retry_on_locked/rollback/commit, since the
    four call sites differ on whether a rollback is needed first and whether several
    accounts share one commit (the startup reset loop).

    account_message and log_message are intentionally separate params, not one shared
    `message`: three of the four call sites store different text on the account than on
    the log (e.g. the account gets "Sync interrupted by service restart", the log gets
    "Interrupted by service restart").

    sync_log_id targets the log row this sync itself opened, when the caller has one.
    Left None for the startup/reset paths, which have no log object of their own - the
    most recently-opened IN_PROGRESS log for the account is closed instead.
    """
    account = db.session.get(Account, account_id)
    if account:
        account.status = account_status
        account.last_error = account_message

    if sync_log_id is not None:
        log_row = db.session.get(AccountSyncLog, sync_log_id)
    else:
        log_row = (
            AccountSyncLog.query
            .filter_by(account_id=account_id, status='IN_PROGRESS')
            .order_by(AccountSyncLog.started_at.desc())
            .first()
        )
    if log_row:
        log_row.status = log_status
        log_row.completed_at = datetime.utcnow()
        log_row.error_message = log_message


def _mark_sync_cancelled(account_id: int, sync_log, detail: str | None = None) -> None:
    """Persist the cancelled terminal state, naming what the stopped phase left behind.

    `detail` is the phase's own sentence, carried up by SyncCancelled. Without it a cancel
    reads the same whether it saved nothing, kept the previous EPG, or left a guide holding
    only part of an import - three outcomes the user cannot tell apart from the outside,
    which is exactly what Product Principle 1 rules out.
    """
    with _sync_locks_mutex:
        reason = _sync_cancel_reasons.pop(account_id, _DEFAULT_CANCEL_REASON)
    if detail:
        reason = f'{reason} - {detail}'

    @retry_on_locked()
    def _do():
        db.session.rollback()
        finalize_sync_state(
            account_id, 'UNSYNCED', 'CANCELLED',
            # Slicing, not [0], so an empty reason can't IndexError inside a commit closure.
            account_message=f'Sync {reason[:1].lower()}{reason[1:]}',
            log_message=reason,
            sync_log_id=getattr(sync_log, 'id', None),
        )
        db.session.commit()

    try:
        _do()
    except Exception:
        log.exception('Failed to persist cancelled state for account %d', account_id)


def _apply_hide_rules_after_upsert(account_id: int, account_name: str) -> None:
    """Re-apply the channel hide rules to this account, between the channel upsert and the
    EPG import.

    The ordering is load-bearing rather than incidental: a channel that arrived in THIS sync
    has to be matched against the rules before anything downstream reads the hidden set, or
    the saving it buys arrives a whole sync late. It runs after the upsert has committed, so
    the rows it is matching are on disk.

    `force=True` on admission - this is the tail of an already-admitted sync, not a
    competitor for the database axis, and being refused by its own sync's ticket would leave
    the account permanently one sync behind.

    Never fatal. A sync that fetched a playlist successfully has done the irreplaceable half
    of its job; failing it here would throw that away over a cache that the next rule edit or
    the next sync recomputes anyway.
    """
    from . import channel_hiding
    try:
        channel_hiding.materialize(f'sync of {account_name}', account_id=account_id,
                                   force=True, stats=False)
    except SQLAlchemyError:
        log.warning('Could not apply hide rules for account %d after sync; the next pass '
                    'will pick it up', account_id, exc_info=True)
        db.session.rollback()


def _do_sync(account_id: int, stop_event: threading.Event, use_dump: bool = False,
             force_epg_resync: bool = False) -> None:
    cfg = load_config()
    sync_cfg = cfg.get('sync', {})
    timeout = sync_cfg.get('request_timeout_seconds', 30)
    epg_days = sync_cfg.get('epg_days_ahead', 3)

    account = db.session.get(Account, account_id)
    if account is None:
        log.error('sync_account: account %d not found', account_id)
        return

    # Captured before this sync's own writes overwrite them - the channel-lifecycle
    # alerts (DESIGN-sync-resilience.md §5) need the PRIOR sync's baseline to detect a
    # shrink/missing transition, same pattern as the EPG collapse guard's baseline read.
    # sync_time anchors "seen by this sync" - it must predate _upsert_channels' own
    # stamping, never a fresh datetime.utcnow() read after the fact (see
    # _raise_channel_lifecycle_alerts's docstring).
    sync_time = datetime.utcnow()
    previous_last_sync_at = account.last_sync_at
    channel_count_baseline = account.channel_count or 0

    # Read here, not in the except block below: a failed flush leaves the session needing
    # a rollback, so touching `account` down there could raise instead of masking. These
    # are the account-owned URLs a requests exception will have stringified in full
    # (DESIGN-secrets.md §4.2).
    account_urls = (account.m3u_url, account.epg_url, account.base_url,
                    *(u for (u,) in db.session.query(EpgSource.url).filter(
                        EpgSource.owner_account_id == account_id, EpgSource.url.isnot(None))))
    # Same reason: the failure-path alert below titles itself with the account name, and by
    # then the session has been rolled back and this instance expired.
    account_name = account.name

    # Mark syncing and create log entry
    @retry_on_locked()
    def _mark_syncing_and_commit():
        account.status = 'SYNCING'
        account.last_error = None
        log_entry = AccountSyncLog(
            account_id=account_id,
            started_at=datetime.utcnow(),
            status='IN_PROGRESS',
        )
        db.session.add(log_entry)
        db.session.commit()
        return log_entry

    sync_log = _mark_syncing_and_commit()

    if stop_event.is_set():
        _mark_sync_cancelled(account_id, sync_log, detail=_CANCEL_LEFT_NOTHING)
        return

    # Clean up old sync logs. Own closure+commit: previously this delete had no commit
    # of its own and rode on the next closure's commit - a lock-retry rollback there would
    # silently drop the delete without re-running it.
    sync_log_keep_days = sync_cfg.get('sync_log_keep_days', 30)
    if sync_log_keep_days and sync_log_keep_days > 0:
        @retry_on_locked()
        def _prune_old_sync_logs_and_commit():
            cutoff = datetime.utcnow() - timedelta(days=sync_log_keep_days)
            AccountSyncLog.query.filter(
                AccountSyncLog.account_id == account_id,
                AccountSyncLog.started_at < cutoff,
            ).delete(synchronize_session=False)
            db.session.commit()

        _prune_old_sync_logs_and_commit()

    # Drives the search-index rebuild in the `finally` below. Set once the channel upserts
    # have COMMITTED, which happens early on purpose (see the m3u branch's comment) - from
    # that moment the indexes describe data that no longer exists, whether or not the rest of
    # this sync succeeds, so the rebuild cannot live only on the success path.
    rebuild_needed = False
    try:
        account_type = (account.account_type or 'm3u')
        drifted: list[tuple] = []   # set by whichever channel-sync branch runs; read after both
        # None = EPG fetch/import was healthy (or skipped); a string means the sync should
        # finish PARTIAL (DESIGN-sync-resilience.md §2) - set by whichever EPG branch runs.
        epg_degradation_reason: str | None = None
        # Live-vs-VOD classification outcome (DESIGN-live-vod.md §4). Only the Xtream branch
        # can classify authoritatively; a plain M3U account has no provider catalog to
        # consult, so it stays None and raises no alert - that is normal operation for the
        # account type, not a degradation (§4.2).
        classify_outcome: str | None = None
        # How many channels ended up with a stream URL we constructed rather than one the
        # provider supplied (DESIGN-live-vod.md §4.3). M3U accounts always get real URLs
        # from their playlist, so this stays 0 for them.
        constructed_urls: int = 0
        # Where constructed URLs were built on: the provider's own declared stream location
        # (server_info) or, when it declares none, this account's base_url as a fallback.
        # base_url is the API endpoint, so using it for streams is a guess the alert names.
        stream_origin_used: str | None = None
        # The provider account facts from the auth response's user_info block (see
        # parse_provider_account_info). Stays all-None for M3U accounts, which never
        # authenticate this way.
        provider_info: dict = {}

        if account_type == 'm3u':
            # Fetched OUTSIDE the closure below: retry_on_locked re-runs its whole body, so
            # a playlist download in there is re-issued on every lock retry against a
            # provider that allows one connection at a time (dev/changelog/683).
            m3u_streams = _fetch_m3u_streams(account, timeout, cfg)
            # The download above is one uninterruptible blocking call, so a cancel issued
            # during it is honored the moment it returns rather than after the upsert.
            _raise_if_cancelled(stop_event, _CANCEL_LEFT_NOTHING)

            @retry_on_locked()
            def _upsert_m3u_channels_and_commit():
                n = _upsert_channels(account, m3u_streams, cfg, stop_event=stop_event)
                db.session.commit()
                return n

            # Commit channel upserts now (rather than leaving them pending through
            # the EPG fetch+import below) so the write lock is only held for this
            # batch, not for the whole sync - see dev/docs/BUGS.md for the incident this fixes.
            (channels_synced, skipped_malformed, skipped_duplicate, drifted,
             new_channel_ids) = _upsert_m3u_channels_and_commit()
            rebuild_needed = True
            _apply_hide_rules_after_upsert(account_id, account.name)
            _raise_if_cancelled(stop_event, _CANCEL_KEPT_EPG)
        else:
            # Xtream path
            from .xtream_client import (XtreamClient, FileXtreamClient, _get_latest_dump_dir,
                                        _fetch_and_classify_xtream_streams)

            if use_dump:
                dump_dir = _get_latest_dump_dir(cfg, account_id)
                log.info('Sync from dump: using %s', dump_dir)
                client = FileXtreamClient(
                    dump_dir, account.base_url, account.username, account.password
                )
            else:
                client = XtreamClient(account.base_url, account.username, account.password,
                                      timeout=timeout, cfg=cfg)
            # Kept, not discarded: server_info carries the provider's own statement of
            # where its streams live, which any constructed URL is built on. Re-fetching
            # auth later to get it would be a second provider connection for data we
            # already hold, and these accounts run at max_connections: 1.
            auth_data = client.check_auth()
            log.info('Xtream auth OK for account %d (%s)', account_id, account.name)

            stream_origin_used = stream_origin_from_server_info(auth_data)
            provider_info = parse_provider_account_info(auth_data)

            # Fetched and classified OUTSIDE the closure below - three provider API calls
            # that must not be re-issued on a lock retry; see the m3u branch above.
            xtream_streams, classify_outcome, constructed_urls = (
                _fetch_and_classify_xtream_streams(account, client, cfg, stream_origin_used))
            _raise_if_cancelled(stop_event, _CANCEL_LEFT_NOTHING)   # see the m3u branch

            @retry_on_locked()
            def _upsert_xtream_channels_and_commit():
                n = _upsert_channels(account, xtream_streams, cfg, stream_origin_used,
                                     stop_event=stop_event)
                db.session.commit()
                return n

            # Commit now - see the m3u branch above for why.
            (channels_synced, skipped_malformed, skipped_duplicate, drifted,
             new_channel_ids) = _upsert_xtream_channels_and_commit()
            rebuild_needed = True
            _apply_hide_rules_after_upsert(account_id, account.name)
            _raise_if_cancelled(stop_event, _CANCEL_KEPT_EPG)

            # Every Xtream account has exactly one provider source (DESIGN-epg-sources.md
            # §3). Created here as well as at account creation so an account whose type was
            # changed to Xtream, or that predates sources, is never left without one.
            @retry_on_locked()
            def _ensure_provider_source_and_commit():
                ensure_provider_source(db.session.get(Account, account_id))
                db.session.commit()

            _ensure_provider_source_and_commit()

        # One refresh per source this sync carries (DESIGN-epg-sources.md §8.1). Each writes
        # its own status and alerts; the sync finishes PARTIAL naming every source that
        # degraded (§9.1), so the sync log keeps the meaning DESIGN-sync-resilience.md §2
        # gave it.
        epg_synced = 0
        degraded: list[str] = []
        for source in sources_refreshed_by_sync(account_id):
            if use_dump and source.kind == EPG_SOURCE_PROVIDER:
                xmltv_path = os.path.join(dump_dir, 'xmltv.xml')
                if not os.path.exists(xmltv_path):
                    log.warning('No xmltv.xml in dump dir, skipping EPG source %d', source.id)
                    continue
                case_sensitive = sync_cfg.get('epg_case_sensitive_matching', False)
                with open(xmltv_path, 'rb') as f:
                    synced, reason = import_source(
                        source, f.read(), epg_days, case_sensitive, cfg, force_epg_resync,
                        stop_event=stop_event)
            else:
                synced, reason = refresh_source(
                    source, timeout, epg_days, cfg, force_epg_resync=force_epg_resync,
                    stop_event=stop_event)
            epg_synced += synced
            if reason:
                degraded.append(f'EPG source "{source.name}": {reason}')
        epg_degradation_reason = ' '.join(degraded) or None

        # "Removed this sync" = the immediate per-sync diff (channels the previous sync
        # touched that this one didn't) - not _raise_channel_lifecycle_alerts's delayed
        # channel_missing_after_days threshold, confirmed deliberately. A first sync has
        # no previous sync to diff against, so that's a true zero.
        #
        # The lower bound must be the previous sync's own started_at, NOT
        # previous_last_sync_at (= account.last_sync_at from before this sync, which is
        # stamped by _mark_success_and_commit's "now" - captured AFTER that previous
        # sync's own channel upsert already ran and stamped last_seen_at on every channel
        # it touched). Using previous_last_sync_at directly as the lower bound would always
        # come out empty: every channel the previous sync touched has a last_seen_at that
        # predates that sync's own completion timestamp, by construction, so none of them
        # would ever satisfy ">= previous_last_sync_at" (measured empirically while
        # implementing this - the naive query always returned 0). The previous sync's own
        # AccountSyncLog.started_at is captured before that run's upsert (same
        # _mark_syncing_and_commit pattern as this run's own sync_log), so it correctly
        # predates every last_seen_at stamp that run wrote. Found by matching completed_at
        # to previous_last_sync_at - both are the exact same `now` value assigned together
        # in that prior run's _mark_success_and_commit closure, so this is an exact match,
        # not a fuzzy one.
        if previous_last_sync_at is not None:
            previous_log = AccountSyncLog.query.filter_by(
                account_id=account_id, completed_at=previous_last_sync_at,
            ).first()
            removed_lower_bound = previous_log.started_at if previous_log else previous_last_sync_at
            channels_removed_count = Channel.query.filter_by(account_id=account_id).filter(
                Channel.last_seen_at < sync_time,
                Channel.last_seen_at >= removed_lower_bound,
            ).count()
        else:
            channels_removed_count = 0

        # Both counts and the duplicate recompute run OUTSIDE the success closure below, and
        # must stay out of it. The closure's first assignment dirties the account row, so the
        # first query after it autoflushes and takes the single write lock - which the EPG
        # count and the recompute would then hold for the rest of the read pass while the
        # recorder and watchdog writers can only wait on busy_timeout. They are also reads of
        # work that committed long before (the channel upserts above, the EPG batches inside
        # import_source), so nothing here depends on the closure's own writes and hoisting
        # them changes no value. See dev/changelog/685.
        channel_count_total = Channel.query.filter_by(account_id=account_id).count()
        # Deliberately unfiltered by `hidden`, and it needs no filter: hidden channels are
        # excluded from the import and their entries are deleted when they are hidden, so
        # this count IS the visible one. Adding a hidden filter here would hide leftover rows
        # from a purge that failed, which is exactly the discrepancy worth being able to see
        # (dev/changelog/781).
        epg_entry_count_total = EPGEntry.query.join(Channel).filter(
            Channel.account_id == account_id
        ).count()

        recompute_duplicate_stream_urls_and_commit()

        @retry_on_locked()
        def _mark_success_and_commit():
            now = datetime.utcnow()
            interval_hours = account.sync_interval_hours or sync_cfg.get('sync_interval_hours', config_default('sync.sync_interval_hours'))
            account.status = 'OK'
            account.last_sync_at = now
            account.next_sync_at = now + timedelta(hours=interval_hours)
            account.channel_count = channel_count_total
            account.epg_entry_count = epg_entry_count_total
            account.constructed_stream_url_count = constructed_urls
            account.provider_status = provider_info.get('status')
            account.provider_exp_date = provider_info.get('exp_date')
            account.provider_is_trial = provider_info.get('is_trial')
            account.provider_max_connections = provider_info.get('max_connections')
            account.provider_active_connections = provider_info.get('active_connections')
            account.provider_allowed_output_formats = provider_info.get('allowed_output_formats')
            account.provider_stream_origin = stream_origin_used

            # PARTIAL (not plain SUCCESS) whenever the EPG fetch/import degraded - a sync
            # that "succeeded" with a silently-empty EPG must be distinguishable from a
            # fully-healthy one (DESIGN-sync-resilience.md §2). account.status stays OK:
            # it's a control value the scheduler/route guards key off, not a display concern.
            sync_log.status = 'PARTIAL' if epg_degradation_reason else 'SUCCESS'
            sync_log.completed_at = now
            sync_log.channels_synced = channels_synced
            sync_log.epg_entries_synced = epg_synced
            sync_log.channels_added = len(new_channel_ids)
            sync_log.channels_removed = channels_removed_count
            sync_log.skipped_malformed_urls = skipped_malformed
            sync_log.skipped_duplicate_stream_ids = skipped_duplicate
            sync_log.error_message = epg_degradation_reason
            db.session.commit()

        _mark_success_and_commit()

        # The WAL size rides along because a sync is the other operation that can plausibly
        # grow dvr.db-wal by gigabytes - one mass DELETE of this account's future EPG rows
        # followed by ~200 insert batches, and any reader holding a snapshot across that run
        # stops every checkpoint from rewinding the file. The daily maintenance reading says
        # which DAY the WAL grew; this says whether it was a sync. See dev/changelog/424.
        log.info(
            'Sync complete for account %d: %d channels, %d EPG entries - WAL now %s',
            account_id, channels_synced, epg_synced,
            fmt_bytes(current_wal_size_bytes()),
        )

        # The two skip counts are not alerted: they are written to this sync's own
        # AccountSyncLog row and shown on the account page, which is where a fact about what
        # one sync imported belongs (dev/changelog/926, 928).
        _alert_url_drift(account, drifted, sync_cfg)
        _write_channel_url_drift_events(drifted)
        # EPG alerts are raised per source, inside each refresh (DESIGN-epg-sources.md §9.1).

        # Live-vs-VOD classification degradations (DESIGN-live-vod.md §4). Both are
        # evaluated every sync so a standing one auto-resolves once the provider recovers.
        # An M3U account leaves classify_outcome None, which resolves both and alerts on
        # neither - it has no catalog to consult and that is not a fault (§4.2).
        from .xtream_client import CLASSIFY_REFUSED, CLASSIFY_UNAVAILABLE
        _raise_or_resolve_standing_alert(
            'SYNC_LIVE_CLASSIFY_UNAVAILABLE',
            source=f'account:{account_id}:live-classify',
            active=classify_outcome == CLASSIFY_UNAVAILABLE,
            title=f'{account.name}: could not tell live channels from VOD',
            body=(
                'The provider\'s live-channel catalog could not be read this sync, so the '
                'playlist was imported without it. Entries on an unambiguous movie or '
                'series path were still excluded, but other on-demand content may have '
                'been imported as channels. No channels were removed.'
            ),
        )
        _raise_or_resolve_standing_alert(
            'SYNC_LIVE_CLASSIFY_REFUSED',
            source=f'account:{account_id}:live-classify-collapse',
            active=classify_outcome == CLASSIFY_REFUSED,
            title=f'{account.name}: live-channel filtering refused (collapse guard)',
            body=(
                'The provider\'s live-channel catalog would have cut this account to a small '
                'fraction of its previous channel count, which usually means the catalog came '
                'back truncated rather than that the channels are gone. The playlist was '
                'imported unfiltered instead, so some on-demand content may have been '
                'imported as channels. No channels were removed.'
            ),
        )

        # Constructed stream URLs are not alerted: the count is on the account row
        # (`constructed_stream_url_count`), and the account page carries the full
        # explanation as a standing banner for exactly as long as it is true
        # (templates/account_detail.html, dev/changelog/928).

        # This sync got far enough to import channels, so whatever blocked a previous one
        # is fixed - clear the standing alert. A sync that had to build its own URLs is
        # still SUCCESS, not PARTIAL, by design: for a provider whose playlist endpoint
        # is permanently disabled that is the normal steady state, and a status that is
        # always degraded stops carrying information. The alert above is the surface.
        _raise_or_resolve_standing_alert(
            'SYNC_URL_CONSTRUCTION_BLOCKED',
            source=f'account:{account_id}:url-construction-blocked',
            active=False,
        )

        # This sync reached the end, so a SYNC_FAILED standing from an earlier attempt is
        # describing a state that no longer exists. PARTIAL counts as finished here: the
        # sync itself completed, and its EPG degradation has its own standing alert above.
        _raise_or_resolve_standing_alert(
            'SYNC_FAILED',
            source=f'account:{account_id}:sync-failed',
            active=False,
        )

        # The account just synced, so it is by definition no longer overdue. This is the
        # self-clearing half the overdue alert promises (dev/changelog/923 decision 8) and it
        # belongs on the success path rather than on a sweep: nothing else knows the account
        # caught up.
        update_overdue_alert(account_id, sync_cfg)

        _raise_channel_lifecycle_alerts(account, cfg, sync_time, previous_last_sync_at,
                                        channel_count_baseline, new_channel_ids)

    except SyncCancelled as exc:
        # Ahead of the broad handler below on purpose: a cancel reaching that one would be
        # persisted as ERROR, which is the opposite of what happened.
        log.info('Sync cancelled for account %d: %s', account_id, str(exc) or 'no detail')
        # Cancel means stop now: a rebuild here would answer a cancellation with ~12s of
        # held write lock. The staleness is safe - the watermark check in
        # search_index_readiness() sends search back to LIKE until a sync finishes.
        rebuild_needed = False
        _mark_sync_cancelled(account_id, sync_log, detail=str(exc) or None)

    except Exception as exc:
        # Captured into a plain local because Python deletes `exc` when the except
        # block exits - a closure reading `exc` directly only works while still
        # inside the block, which is too fragile to rely on.
        # Masked because requests exceptions stringify with the full credentialed URL, and
        # this string is persisted, rendered in the UI, and can be pushed off-box as an alert.
        # The account's own URLs lose their whole path (they may BE the credential); anything
        # else in the message still gets the generic heuristics.
        # Built here, above the log line, because the SYNC_FAILED alert raised below carries
        # this same string as its body - masking the one message twice is two chances for the
        # log and the alert to disagree about what was redacted.
        error_message = mask_account_urls_in_text(str(exc), *account_urls)
        # log.error with a pre-rendered traceback, not log.exception: the handler-level
        # CredentialMaskingFilter masks a traceback with the generic heuristics only, and
        # a path-token account URL matches none of them. Marked already_alerted because
        # SYNC_FAILED below is this condition's own typed alert (dev/changelog/930).
        log.error('Sync failed for account %d: %s\n%s', account_id, error_message,
                  mask_account_urls_in_text(traceback.format_exc(), *account_urls),
                  extra={'already_alerted': True})

        # Rollback is required before touching the session again - a failed flush
        # leaves the transaction in a "needs rollback" state; any subsequent commit
        # without rollback raises InvalidRequestError and the error status never saves.
        @retry_on_locked()
        def _mark_error_and_commit():
            db.session.rollback()
            finalize_sync_state(
                account_id, 'ERROR', 'ERROR',
                account_message=error_message,
                log_message=error_message,
                sync_log_id=getattr(sync_log, 'id', None),
            )
            db.session.commit()

        try:
            _mark_error_and_commit()
        except Exception:
            log.exception('Failed to persist error state for account %d', account_id)

        # Raised only AFTER the rollback above, for the reason spelled out on the blocked-
        # construction alert below: create_alert commits, and until _mark_error_and_commit
        # has run the session can be in a needs-rollback state. Standing rather than one row
        # per attempt, and resolved by the next sync that finishes, because a failure that
        # has since recovered must not go on standing - "Sync failed for account 2" sat open
        # through eight days of successful syncs (dev/changelog/930).
        _raise_or_resolve_standing_alert(
            'SYNC_FAILED',
            source=f'account:{account_id}:sync-failed',
            active=True,
            title=f'{account_name}: sync failed',
            body=error_message,
        )

        # Raised only AFTER the rollback above: the blocked construction aborts the upsert
        # loop mid-flight, so the session is in a needs-rollback state until then and any
        # write here (create_alert commits) would fail with InvalidRequestError.
        if isinstance(exc, StreamUrlConstructionBlocked):
            _raise_or_resolve_standing_alert(
                'SYNC_URL_CONSTRUCTION_BLOCKED',
                source=f'account:{account_id}:url-construction-blocked',
                active=True,
                title=f'{account_name}: no URL format set, so no channels could be imported',
                body=(
                    'This provider does not supply stream URLs - only its channel catalog '
                    'could be read - so ChannelBin has to build the URLs itself. It cannot, '
                    'because URL Normalization is set to Disabled, which leaves no format to '
                    'build them in. No channels were imported and nothing was changed.\n\n'
                    'Set URL Normalization on this account, or as the global default in '
                    'Settings, and sync again. If you are unsure which to pick, choose '
                    f'"{norm_mode_label(NORM_MPEGTS)}" ({norm_mode_example(NORM_MPEGTS)}) - '
                    'it is the most widely accepted form, and the trailing ".ts" of the '
                    'other MPEG-TS spelling is often blocked by Cloudflare.'
                ),
            )

    finally:
        # Both search indexes are derived from what this sync rewrote, so they are stale the
        # moment the channel upserts commit. In a `finally` rather than on the success path
        # because the upserts commit BEFORE the EPG fetch, and a provider timeout there is the
        # single most common way a sync fails - leaving committed channels the index has never
        # seen, and renamed channels still matching their old names.
        #
        # Deliberately outside every decorated closure above: this is a bulk rebuild, not part
        # of any of their read-modify-write units, and a lock retry on one of them must never
        # drag a multi-second rebuild along with it. It never raises - a failed rebuild alerts
        # and leaves search on its LIKE fallback rather than failing a sync whose own work is
        # already committed and correct, and raising from a `finally` would replace the real
        # exception the except block above just handled.
        if rebuild_needed:
            from .search_index import rebuild_search_indexes
            rebuild_search_indexes(f'account {account_id} sync')


# How far into a playlist to look for the #EXTM3U header before giving up. Real providers
# put licence text or a branding line above it - m3upt.com opens with a "# M3UPT.com - IPTV
# playlist ... Public and official streams only." comment - and rejecting those cost the
# entire sync, all channels, on a playlist every other player reads fine. Bounded rather
# than unlimited so the check keeps doing its actual job: an HTML error page or a login
# redirect served in place of a playlist still has no #EXTM3U anywhere near the top and is
# still refused (dev/docs/BUGS.md 2026-08-09, dev/changelog/523).
_M3U_HEADER_SCAN_LINES = 10


def _looks_like_m3u(text: str) -> bool:
    """True if #EXTM3U appears within the first few non-blank lines."""
    seen = 0
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        if stripped.startswith('#EXTM3U'):
            return True
        seen += 1
        if seen >= _M3U_HEADER_SCAN_LINES:
            break
    return False


def _fetch_m3u_streams(account: Account, timeout: int, cfg: dict | None = None) -> list:
    """Fetch account.m3u_url and parse it into a list of stream dicts.

    Deliberately does no database work. The caller upserts these streams inside its own
    retry_on_locked closure, so a lock retry re-runs the write and never re-downloads the
    playlist - a multi-MB fetch against a provider that allows one connection at a time,
    possibly while a recording is live on that same account (dev/changelog/683).
    """
    log.info('Fetching M3U for account %d from %s', account.id, mask_url_path(account.m3u_url))
    resp = requests.get(account.m3u_url, timeout=timeout, headers=_request_headers(cfg))
    resp.raise_for_status()
    text = resp.content.decode('utf-8-sig', errors='replace').strip()
    if not _looks_like_m3u(text):
        # Masked here, not just at the log site: this message is persisted verbatim into
        # account.last_error / AccountSyncLog.error_message and rendered in the UI.
        raise ValueError(
            f'Expected M3U playlist from {mask_url_path(account.m3u_url)!r}, got: {text[:200]!r}')
    # Empty server_base_url skips the /live/-only filter - all channels are kept
    streams = _parse_m3u_as_streams(text)
    log.info('Parsed %d streams from M3U for account %d', len(streams), account.id)
    return streams


def _alert_url_drift(account: Account, drifted: list, sync_cfg: dict) -> None:
    """Raise/refresh the informational mass-URL-rewrite alert (DESIGN-url-drift.md 3/3).

    Purely observational: the sync has already succeeded by the time this runs and this
    must never block or fail it (a sync-blocking confirm gate was explicitly rejected).
    """
    threshold = sync_cfg.get('url_drift_alert_min_channels', 50)
    if not threshold or threshold <= 0 or len(drifted) < threshold:
        return

    source = f'account:{account.id}:url-drift'
    title = f'{account.name}: provider stream URLs changed on {len(drifted)} channel(s)'
    _old, _new = drifted[0][1], drifted[0][2]
    body = (
        f'The last sync of account "{account.name}" rewrote the stream URL of '
        f'{len(drifted)} existing channel(s) - typically the provider moving its stream '
        'domain or rotating the credentials embedded in every URL. Channel identity, guide '
        'placement, groups and history are unaffected (channels are matched by stream id, '
        'not URL), and scheduled recordings re-resolve their URL at record start. No action '
        f'is normally needed. Example: {mask_creds(_old)} -> {mask_creds(_new)}'
    )

    # Refresh the standing alert for this account rather than stacking one per sync while
    # the provider keeps drifting; read_at is cleared because a fresh mass rewrite is new
    # news even if the previous one was already read.
    @retry_on_locked()
    def _refresh_existing():
        existing = Alert.query.filter(
            Alert.alert_type == 'PROVIDER_URLS_CHANGED',
            Alert.source == source,
            Alert.dismissed_at.is_(None),
        ).all()
        if not existing:
            return False
        for a in existing:
            a.title = title[:255]
            a.body = body
            a.created_at = datetime.utcnow()
            a.read_at = None
        db.session.commit()
        return True

    if _refresh_existing():
        return

    from .alerts import create_alert
    create_alert('PROVIDER_URLS_CHANGED', title=title, body=body, source=source)


def _write_channel_url_drift_events(drifted: list) -> None:
    """Write a CHANNEL_URL_CHANGED ChannelEvent for every channel in `drifted`
    (DESIGN-url-drift.md 4/3), so the drift surfaces on that channel's own Activity
    Timeline - not just the account-level PROVIDER_URLS_CHANGED alert above, which only
    fires once the drift count crosses a threshold. Written for every drifted channel
    regardless of that threshold, by design (a mass rewrite is still a real, if repeated,
    fact about each individual channel's own history).

    One retry_on_locked closure batch-inserts every row and commits once - a mass domain
    move can drift thousands of channels in one sync, so this must never be one
    add()+commit per row (CLAUDE.md commit rule).
    """
    if not drifted:
        return

    @retry_on_locked()
    def _write_events_and_commit():
        for channel_id, old_url, new_url in drifted:
            db.session.add(ChannelEvent(
                channel_id=channel_id, event_type=CHANNEL_URL_CHANGED,
                detail=f'Provider stream URL changed: {mask_creds(old_url)} -> {mask_creds(new_url)}'))
        db.session.commit()

    _write_events_and_commit()


# Every degradation reason refresh_source / import_source can return starts with one of
# these prefixes, and each maps to exactly one alert type. A map rather than a chain of
# startswith() branches because the chain's trailing else silently absorbed any prefix
# added later - CLAUDE.md "a trailing else must never be the rendering of a real state".
# The prefixes are literals this module owns at both write sites; nothing outside parses
# them (they are also the user-facing text of AccountSyncLog.error_message).
EPG_DEGRADATION_ALERT_TYPES = {
    'fetch failed:': 'EPG_SOURCE_FETCH_FAILED',
    'import refused:': 'EPG_SOURCE_COLLAPSE_REFUSED',
    'import truncated:': 'EPG_SOURCE_IMPORT_TRUNCATED',
}


def _epg_degradation_alert_type(reason: str | None) -> str | None:
    """Which alert type a degradation reason belongs to, or None when the EPG was healthy.

    An unrecognized prefix is a defect in whoever added a reason without registering it,
    so it is logged and routed to the generic fetch-failure type - never dropped, which
    would leave a degraded sync with no surface at all.
    """
    if not reason:
        return None
    for prefix, alert_type in EPG_DEGRADATION_ALERT_TYPES.items():
        if reason.startswith(prefix):
            return alert_type
    log.error('EPG degradation reason has no registered prefix, routing it to the generic '
              'fetch-failure alert: %r', reason)
    return 'EPG_SOURCE_FETCH_FAILED'


def _raise_or_resolve_standing_alert(alert_type: str, source: str, active: bool,
                                     title: str = '', body: str = '') -> None:
    """The shared shape behind every SYNC_* degradation alert (DESIGN-sync-resilience.md
    §3): while `active`, raise or refresh a WARN alert for (alert_type, source) rather than
    stacking one per sync; once the condition clears, auto-dismiss the standing one. Mirrors
    the GROUP_FORMAT_MISMATCH auto-resolve precedent (app/channel_groups.py:405-417) and
    generalizes `_alert_url_drift`'s refresh-only pattern above with a dismiss branch.

    Canonical home for this pattern - the EPG_SOURCE_* alerts and SYNC_FEED_SHRUNK need the
    identical shape; reuse this instead of another copy.
    """
    @retry_on_locked()
    def _refresh_or_dismiss_existing():
        existing = Alert.query.filter(
            Alert.alert_type == alert_type,
            Alert.source == source,
            Alert.dismissed_at.is_(None),
        ).all()
        if not existing:
            return False
        if active:
            for a in existing:
                a.title = title[:255]
                a.body = body
                a.created_at = datetime.utcnow()
                a.read_at = None
        else:
            for a in existing:
                a.dismissed_at = datetime.utcnow()
        db.session.commit()
        return True

    if _refresh_or_dismiss_existing() or not active:
        return

    from .alerts import create_alert
    create_alert(alert_type, title=title, body=body, source=source)


#: How far past due an account has to fall before the overdue alert fires, in multiples of its
#: own sync interval. A sync is due at last_sync_at + interval, so 2 means "a whole interval has
#: gone by since it should have run" - the threshold decided in dev/changelog/923 (decision 8).
#: One interval would fire on every ordinary deferral, which is exactly the noise that decision
#: was written to avoid; an individual deferred sync is shown on the account, not alerted.
OVERDUE_INTERVAL_MULTIPLE = 2


def update_overdue_alert(account_id: int, sync_cfg: dict) -> None:
    """Raise or clear this account's standing "sync is overdue" alert.

    Called from both ends of the condition: scheduler.py when a sync is deferred past a
    recording (the only path that can let an account fall behind) and from the success path
    above (the only path that can bring it back). Nothing polls - a condition alert that has to
    be swept for is a condition alert that can go stale.

    An account with automatic sync switched off is never overdue: there is nothing it is late
    for. Neither is one that has never synced - that is UNSYNCED, a different state with its own
    surfaces, and calling a brand-new account "overdue" would be wrong on its first day.
    """
    account = db.session.get(Account, account_id)
    if account is None:
        return

    interval_hours = account.sync_interval_hours or sync_cfg.get('sync_interval_hours', config_default('sync.sync_interval_hours'))
    overdue_by = timedelta(hours=interval_hours * OVERDUE_INTERVAL_MULTIPLE)
    active = bool(
        account.sync_enabled
        and account.last_sync_at is not None
        and datetime.utcnow() - account.last_sync_at >= overdue_by
    )

    body = ''
    if active:
        body = (
            f'This account syncs every {interval_hours}h, but its last successful sync '
            f'finished {format_local(account.last_sync_at)} - more than '
            f'{interval_hours * OVERDUE_INTERVAL_MULTIPLE}h ago. Scheduled syncs are being '
            'deferred past recordings and have not yet found a gap long enough to run in, so '
            'this account\'s channel list and guide data are getting stale. Sync it by hand '
            'from the account page, or widen the gap by turning off "Skip sync during '
            'recording" in Settings. This alert clears itself on the next successful sync.'
        )

    _raise_or_resolve_standing_alert(
        'SYNC_ACCOUNT_OVERDUE',
        source=f'account:{account_id}:overdue',
        active=active,
        title=f'{account.name}: sync is overdue',
        body=body,
    )


def next_sync_map(accounts) -> dict:
    """{account_id: the next sync attempt, naive UTC or None} for a batch of accounts.

    The answer comes from the scheduler, because that is what decides it: the earliest of the
    account's interval job and any deferred-retry one-shot. `Account.next_sync_at` is only the
    fallback for an app with no scheduler running, and it is deliberately not preferred - it is
    written just when a sync succeeds and when the interval job is re-registered, so an account
    whose sync was deferred past a recording kept showing a time that had already gone by. Every
    "next sync" on the dashboard, the accounts list, the account page and the channel page read
    "overdue" while the real attempt was hours away (dev/changelog/941).

    Batched deliberately: one jobstore read serves a whole page, and the returned dict is what a
    per-row template loop indexes into. Never ask per row.
    """
    from .scheduler import next_sync_attempts

    scheduled = next_sync_attempts()
    return {
        account.id: (scheduled.get(account.id, account.next_sync_at)
                     if account.sync_enabled else None)
        for account in accounts
    }


def channel_lifecycle_state(channel: Channel, account: Account, cfg: dict,
                            completed_sync_count: int, earliest_sync_at) -> tuple[str | None, object]:
    """('missing' | 'new' | None, since_datetime) - the derived display state from
    DESIGN-sync-resilience.md §5, never stored. Pure function of its inputs: the caller
    MUST precompute completed_sync_count and earliest_sync_at once per account (a
    per-channel-row loop calling this must never trigger a query - CLAUDE.md "no hidden
    I/O in per-row loops").

    completed_sync_count: this account's AccountSyncLog rows with status SUCCESS/PARTIAL.
    earliest_sync_at: this account's earliest AccountSyncLog.started_at, or None.
    """
    sync_cfg = cfg.get('sync', {})
    missing_days = sync_cfg.get('channel_missing_after_days', 7)
    new_days = sync_cfg.get('channel_new_within_days', 3)
    now = datetime.utcnow()

    if (missing_days > 0 and channel.last_seen_at is not None
            and account.last_sync_at is not None
            and channel.last_seen_at < now - timedelta(days=missing_days)
            and account.last_sync_at > channel.last_seen_at):
        return 'missing', channel.last_seen_at

    if new_days > 0 and channel.first_seen_at is not None \
            and channel.first_seen_at > now - timedelta(days=new_days):
        first_sync_marker = earliest_sync_at or account.created_at
        in_first_sync_era = (
            completed_sync_count < 2
            or (first_sync_marker is not None
                and first_sync_marker > now - timedelta(days=new_days))
        )
        if not in_first_sync_era:
            return 'new', channel.first_seen_at

    return None, None


def get_sync_duration_estimate(account_id: int):
    """(avg_seconds, run_count) over this account's most recent completed syncs, capped
    at database.JOB_RUN_HISTORY_LIMIT rows - the account-sync half of the "estimated job
    runtime" feature (dev/changelog/592). Keyed per account, never averaged across
    accounts: a 34,012-channel sync and an 8,804-channel one are not the same job with
    the same average.

    Reuses AccountSyncLog rather than the generic JobRun table in database.py - this data
    already exists here, so a second copy would be exactly the duplication CLAUDE.md's
    "search before you write" rule forbids. PARTIAL counts as completed (a degraded-but-
    finished sync still took real wall-clock time); IN_PROGRESS/ERROR/CANCELLED don't -
    an aborted run's elapsed time isn't a useful estimate of how long the job takes to
    actually finish. (None, 0) when there is no completed sync yet.
    """
    from .database import JOB_RUN_HISTORY_LIMIT

    rows = (
        AccountSyncLog.query
        .filter(AccountSyncLog.account_id == account_id,
                AccountSyncLog.status.in_(['SUCCESS', 'PARTIAL']),
                AccountSyncLog.completed_at.isnot(None))
        .order_by(AccountSyncLog.started_at.desc(), AccountSyncLog.id.desc())
        .limit(JOB_RUN_HISTORY_LIMIT).all()
    )
    if not rows:
        return None, 0
    total = sum((row.completed_at - row.started_at).total_seconds() for row in rows)
    return total / len(rows), len(rows)


def lifecycle_states_for_channels(channels, cfg, accounts_by_id=None) -> dict:
    """{channel.id: (state, since)} - channel_lifecycle_state() for a batch of channels,
    with the two per-account aggregates it needs fetched ONCE for the whole batch.

    Lives here, beside the function it batches, because more than one surface renders these
    states now (the Channels hub, the channel detail page, the channel search's row payload)
    and each of them would otherwise re-derive the batching - and getting it wrong means a
    per-row query, which is the defect class CLAUDE.md names.

    `accounts_by_id` lets a caller that has already loaded its accounts hand them over; when
    it is omitted the channels' own `account` relationship is used, which is a per-row lookup
    unless the accounts are already in the session's identity map.
    """
    if not channels:
        return {}

    account_ids = {ch.account_id for ch in channels}
    completed_counts = dict(
        db.session.query(AccountSyncLog.account_id, func.count(AccountSyncLog.id))
        .filter(AccountSyncLog.account_id.in_(account_ids),
                AccountSyncLog.status.in_(['SUCCESS', 'PARTIAL']))
        .group_by(AccountSyncLog.account_id)
        .all()
    )
    earliest_by_account = dict(
        db.session.query(AccountSyncLog.account_id, func.min(AccountSyncLog.started_at))
        # A SKIPPED row is an occurrence that never ran, so it is not this account's first
        # sync however early it sits - counting one would move the first-sync era off a
        # date nothing was ever imported on (dev/changelog/928).
        .filter(AccountSyncLog.account_id.in_(account_ids),
                db.or_(AccountSyncLog.status.is_(None),
                       AccountSyncLog.status != 'SKIPPED'))
        .group_by(AccountSyncLog.account_id)
        .all()
    )

    out = {}
    for ch in channels:
        account = (accounts_by_id or {}).get(ch.account_id) if accounts_by_id else ch.account
        if account is None:
            continue
        out[ch.id] = channel_lifecycle_state(
            ch, account, cfg,
            completed_counts.get(ch.account_id, 0),
            earliest_by_account.get(ch.account_id),
        )
    return out


def repoint_candidates_for_channels(channels, cfg, lifecycle_by_channel):
    """{missing_channel.id: survivor_channel} - the duplicate-repoint recovery target
    (DESIGN-sync-resilience.md §6) for every channel in `channels` that is 'missing' and
    duplicate-flagged. `is_duplicate_stream_url` is the cheap prefilter (skip the lookup
    entirely when nothing in the batch is flagged); the survivor query itself is one batched
    exact-URL lookup across ALL accounts, never per-row. Picks the oldest (lowest id)
    non-missing channel sharing the URL, matching duplicate_groups_within's "oldest first"
    convention. Nothing here is stored - recomputed at render/action time, same as
    channel_lifecycle_state itself.

    Moved here from app/routes/channels.py (dev/changelog/627) beside lifecycle_states_for_channels
    when the TV Guide's channel column became its second caller, matching the DRY canonical-home
    pattern already used for lifecycle_states_for_channels itself.
    """
    missing_channels = [
        ch for ch in channels
        if ch.is_duplicate_stream_url
        and lifecycle_by_channel.get(ch.id, (None, None))[0] == 'missing'
    ]
    if not missing_channels:
        return {}

    urls = {ch.stream_url for ch in missing_channels}
    candidates = Channel.query.filter(Channel.stream_url.in_(urls)).all()
    candidate_lifecycle = lifecycle_states_for_channels(candidates, cfg)

    by_url: dict[str, list] = {}
    for c in candidates:
        by_url.setdefault(c.stream_url, []).append(c)

    result = {}
    for ch in missing_channels:
        survivors = sorted(
            (c for c in by_url.get(ch.stream_url, [])
             if c.id != ch.id and candidate_lifecycle.get(c.id, (None, None))[0] != 'missing'),
            key=lambda c: c.id,
        )
        if survivors:
            result[ch.id] = survivors[0]
    return result


def transfer_channel_state(source: Channel, dest: Channel, cfg: dict) -> str:
    """Transfer guide/group/schedule/test-enrollment state from `source` to `dest`
    (DESIGN-sync-resilience.md §6). `source` is kept (soft state) - it just no longer
    holds guide/group/schedule state. Returns a plain-English summary of what moved, for
    the caller to show as a toast/response message.

    Shared by two callers with different validation needs: the repoint recovery route
    (`app/routes/channels.py::repoint_channel`, which additionally requires `source` to be
    lifecycle-'missing' and `dest` not to be) and "Remove Duplicate Channels" removal with
    transfer (`app/routes/channel_tests.py`, `channel_groups.py`, `guide.py`, no lifecycle
    requirement). This function itself only assumes `source` and `dest` are two distinct
    Channel rows sharing the same stream_url - callers own their own validation and are
    expected to call this only from inside their own retry_on_locked commit closure, after
    re-fetching fresh rows.
    """
    moved = []

    # 1. Guide membership - only meaningful if the source channel was actually a guide
    # row; a dest that's already in the guide is left alone (nothing to move).
    if source.in_guide:
        if not dest.in_guide:
            dest.in_guide = True
            dest.guide_sort_order = source.guide_sort_order
            db.session.add(ChannelEvent(
                channel_id=dest.id, event_type=CHANNEL_ADDED_TO_GUIDE,
                detail=f'Added to guide via transfer from "{source.name}"',
            ))
            moved.append('guide listing')
        source.in_guide = False
        db.session.add(ChannelEvent(
            channel_id=source.id, event_type=CHANNEL_REMOVED_FROM_GUIDE,
            detail=f'Removed from guide - transferred to "{dest.name}"',
        ))

    # 2. Group membership - M:N since Groups unification, so this transfers EVERY group
    # the source channel belongs to, one at a time.
    # A group the dest already belongs to is skipped and named in the summary - no
    # silent partial transfer.
    skipped_groups = []
    transferred_groups = 0
    for m in list(source.group_memberships):
        already_member = ChannelGroupMember.query.filter_by(
            group_id=m.group_id, channel_id=dest.id).first()
        # Either branch changes which channels that group holds, and the group may not
        # be the one the user is looking at - a source channel can belong to several.
        touch_group(m.group)
        if already_member is not None:
            skipped_groups.append(m.group.name)
            db.session.delete(m)
        else:
            m.channel_id = dest.id
            transferred_groups += 1
    if transferred_groups:
        moved.append('group membership')
    if skipped_groups:
        moved.append(f'group membership skipped (survivor already in '
                      f'{", ".join(skipped_groups)})')

    # 3. SCHEDULED recordings only - never IN_PROGRESS or terminal rows (never rewrite
    # history). Group-backed recordings are excluded: their channel_id is owned by the
    # group's own member-selection/failover logic (app/recorder.py), not this helper.
    scheduled_recs = Recording.query.filter_by(
        channel_id=source.id, status=REC_STATUS_SCHEDULED, group_id=None).all()
    fresh_url = normalize_url(dest.stream_url, dest.account, cfg)
    for rec in scheduled_recs:
        old_channel_name, old_url = source.name, rec.url
        rec.channel_id = dest.id
        rec.url = fresh_url
        add_recording_event(
            rec.id, RECORDING_REPOINTED,
            detail=(f'Channel "{old_channel_name}" ({source.account.name}) removed as a '
                    f'duplicate; recording transferred to "{dest.name}" '
                    f'({dest.account.name}) ({mask_creds(old_url)} -> {mask_creds(fresh_url)})'),
            extra={'old_channel_id': source.id, 'new_channel_id': dest.id,
                   'old_url': mask_creds(old_url), 'new_url': mask_creds(fresh_url)})
    if scheduled_recs:
        moved.append(f'{len(scheduled_recs)} scheduled recording'
                      f'{"s" if len(scheduled_recs) != 1 else ""}')

    # 4. Channel-test enrollment - dest keeps True if already enabled.
    if source.test_enabled and not dest.test_enabled:
        # participation-write-ok: Channel.test_enabled, the channel-wide off switch, not
        # the group-scoped ChannelGroupMember column.
        dest.test_enabled = True
        moved.append('channel-test enrollment')

    # 5. Both channels' hidden state, because steps 1 and 2 just moved the two things that
    # protect a channel from being hidden. The source can have given up its last protection
    # (a deferred hide now takes effect) and the dest can have gained its first. Recomputed
    # rather than reasoned about: this runs inside the caller's own retry_on_locked commit
    # closure, which is exactly where a recompute belongs.
    from . import channel_hiding
    channel_hiding.recompute([source.id, dest.id])

    if not moved:
        return f'"{source.name}": nothing to transfer to "{dest.name}".'
    return f'"{source.name}" -> "{dest.name}": moved {", ".join(moved)}.'


def missing_channels_query(account_id, cfg, channel_ids=None):
    """Channel.query filtered to exactly the 'missing' branch of channel_lifecycle_state()
    above - re-expressed as SQL (joined against Account) so a bulk operation over
    thousands of rows (see the Channels hub bulk-delete action) never has to hydrate every
    channel and call the Python function per row. The 'new' branch isn't needed here since
    it depends on per-account aggregates that don't apply to "missing" at all.

    `channel_ids` scopes to an explicit set of channels (a group/health check's own
    members) instead of - or in addition to - an account. Kept deliberately parallel to
    channel_lifecycle_state's missing condition - if that condition changes, this must
    change with it.
    """
    missing_days = cfg.get('sync', {}).get('channel_missing_after_days', 7)
    if missing_days <= 0:
        return Channel.query.filter(db.false())

    cutoff = datetime.utcnow() - timedelta(days=missing_days)
    q = (
        Channel.query.join(Account, Channel.account_id == Account.id)
        .filter(Channel.last_seen_at.isnot(None))
        .filter(Account.last_sync_at.isnot(None))
        .filter(Channel.last_seen_at < cutoff)
        .filter(Account.last_sync_at > Channel.last_seen_at)
    )
    if account_id:
        q = q.filter(Channel.account_id == account_id)
    if channel_ids is not None:
        q = q.filter(Channel.id.in_(channel_ids))
    return q


def _raise_channel_lifecycle_alerts(account: Account, cfg: dict, sync_time: datetime,
                                    previous_last_sync_at, channel_count_baseline: int,
                                    new_channel_ids: list) -> None:
    """Feed-shrink (WARN, standing) + missing/new digest alerts (INFO, per-transition) -
    DESIGN-sync-resilience.md §5. Called after _mark_success_and_commit, so
    account.channel_count already reflects THIS sync; sync_time, channel_count_baseline,
    and previous_last_sync_at must all be captured by the CALLER before that commit (same
    before/after-the-overwrite pattern as the EPG collapse guard's baseline read).

    sync_time MUST be captured before _upsert_channels ran, not a fresh datetime.utcnow()
    taken here - every channel's last_seen_at is "in the past" relative to a timestamp
    taken after the sync already stamped it, so comparing against a freshly-captured
    "now" here would misclassify every touched channel as unseen too.

    Never deletes or disables a channel - display/alert only, per §5.
    """
    sync_cfg = cfg.get('sync', {})
    now = sync_time

    # Channels _upsert_channels did NOT just stamp this sync (it always stamps
    # last_seen_at to a moment at or after sync_time).
    not_seen = db.session.query(Channel.name, Channel.last_seen_at).filter_by(
        account_id=account.id).filter(Channel.last_seen_at < now).all()

    shrink_pct = sync_cfg.get('feed_shrink_percent', 50)
    shrunk = (shrink_pct > 0 and channel_count_baseline > 0
             and len(not_seen) >= channel_count_baseline * shrink_pct / 100)
    _raise_or_resolve_standing_alert(
        'SYNC_FEED_SHRUNK',
        source=f'account:{account.id}:feed-shrunk',
        active=shrunk,
        title=f'{account.name}: provider feed shrunk',
        body=(
            f'{len(not_seen)} of {channel_count_baseline} channels were absent from this '
            f'sync ({shrink_pct}% threshold) - channels are never deleted or disabled, but '
            'this may indicate a provider outage.' if shrunk else ''
        ),
    )

    # Neither the newly-missing nor the newly-new set is announced. Both are derived
    # display state (channel_lifecycle_state), so each channel carries its own new/missing
    # badge wherever it is listed, the account page links straight to the filtered lists,
    # and channel search filters on them - which answers "what changed" far better than a
    # once-a-day digest naming five of them could (dev/changelog/928).


#: The channel columns ch_fts actually indexes (app/search_index.py::SEARCH_INDEX_DDL). A
#: change to one of these is the only thing that can leave the channels search index matching
#: text the row no longer has, so they are tracked apart from the rest of the field set below:
#: they alone stamp search_text_updated_at, the index's staleness watermark. Keep in step with
#: the ch_fts column list; adding a column there without adding it here means renames of that
#: column are silently missing from search until something else happens to stale the index.
_SEARCH_TEXT_COLUMNS = frozenset({'name', 'stream_url', 'epg_channel_id', 'category_name'})


def _upsert_channels(account: Account, streams: list, cfg: dict | None = None,
                     stream_origin: str | None = None,
                     stop_event: threading.Event | None = None) -> tuple[int, int, int, list, list]:
    """Insert or update Channel rows from a list of stream dicts.

    Returns (synced_count, skipped_malformed_count, skipped_duplicate_count, drifted,
    new_channel_ids).

    skipped_duplicate_count is stream_ids repeated within this one playlist/catalog - a
    collision that can be innocuous (the URL-derived id takes the last numeric path segment,
    so /u/p/123.ts and /u/p/123.m3u8 variants collide) or a genuine loss (the synthesized
    hash id is only 32 bits, which across a large playlist has a real chance of colliding two
    unrelated URLs). Either way the second occurrence is dropped, so this is logged and
    alerted rather than silently discarded (dev/docs/BUGS.md 2026-08-30).

    `stop_event` makes this loop cancellable. On the largest real account it is minutes of
    work, and before it was polled here a cancel issued during it was silently ignored until
    the phase ended (dev/changelog/720). Stopping mid-loop loses nothing: every write this
    function makes is pending in one transaction that the caller commits only on return, so
    an abort leaves the rollback in _mark_sync_cancelled with nothing of consequence to undo.
    drifted is a list of (channel_id, old_raw_url, new_raw_url) for existing channels
    whose provider URL changed in this sync - provider stream-URL drift
    (DESIGN-url-drift.md 3/3). It is a list rather than a bare count on purpose: the
    per-channel CHANNEL_URL_CHANGED event (URL drift 4/3, still in the backlog) needs the
    same rows and must not walk this loop a second time to get them.
    new_channel_ids is the ids of channels created (not updated) this sync - feeds the
    SYNC_CHANNELS_NEW digest alert (DESIGN-sync-resilience.md §5); populated after flush
    since a new row has no id until then.

    One `sync_time`, captured once here (not per row), stamps every touched channel's
    last_seen_at (existing and newly-created alike) and, for new channels only,
    first_seen_at - the raw data behind the channel-lifecycle tracking in §5.

    A matched channel is UPDATEd only when a provider field actually moved. The unchanged
    majority - normally the entire account, since a provider feed rarely differs between
    two syncs four hours apart - gets its last_seen_at advanced by a chunked bulk UPDATE
    instead, and its updated_at left alone. See _stamp_last_seen for why that matters and
    why the statement is shaped the way it is (dev/changelog/673).

    Of those changed rows, only the ones where a column the channels search index actually
    contains moved (_SEARCH_TEXT_COLUMNS) also stamp search_text_updated_at, which is that
    index's staleness watermark - so a sync that only shuffled logos costs no rebuild
    (dev/changelog/674).
    """
    # Columns only, and only the ones compared or carried below: hydrating full ORM
    # objects here costs 57k instances plus the LEFT JOIN that Channel.default_profile's
    # lazy='joined' drags in, for a lookup dict that never reads a profile and never
    # mutates an object. Measured on this account: 2.40s -> 0.54s (dev/changelog/673).
    existing = {
        row.stream_id: row
        for row in db.session.query(
            Channel.id, Channel.stream_id, Channel.name, Channel.logo_url,
            Channel.category_name, Channel.category_id, Channel.stream_url,
            Channel.raw_stream_url, Channel.url_normalizable, Channel.epg_channel_id,
        ).filter(Channel.account_id == account.id).all()
    }

    # Resolved once, never per row: resolve_normalization_mode() falls through to
    # load_config() whenever the account defers to the global default, which is the
    # default state of every account, and this loop runs once per channel - tens of
    # thousands of times on a real provider (CLAUDE.md "no hidden I/O in per-row loops").
    norm_mode = resolve_normalization_mode(account, cfg)

    sync_time = datetime.utcnow()
    synced = 0
    skipped_malformed = 0
    skipped_duplicate = 0
    # Bounded regardless of how many duplicates a bad feed produces - the log line and the
    # alert body both want a few real stream_ids, not a multi-thousand-entry dump.
    duplicate_sample: list = []
    _DUPLICATE_SAMPLE_CAP = 5
    drifted: list[tuple] = []
    new_channels: list = []
    # Matched existing rows, split by whether the provider actually changed anything.
    changed: list[dict] = []      # -> one UPDATE each, updated_at moves
    unchanged_ids: list[int] = []  # -> bulk last_seen_at stamp, updated_at untouched
    seen_ids: set[int] = set()  # guard against duplicate stream_ids within one M3U
    total_streams = len(streams)
    for i, stream in enumerate(streams):
        # Throttled and unconditional (ahead of every `continue` below), not per-row: an
        # in-memory dict write under a lock is cheap, but no reason to pay it 80,000 times
        # on the largest real account, and a run of skipped rows must not silently widen
        # the gap between checkpoints.
        if i % 250 == 0 or i == total_streams - 1:
            _set_sync_progress(account.id, 'channels', i + 1, total_streams)
            # Polled on the checkpoint that already exists rather than per row - the cancel
            # lands within 250 rows either way, and this loop runs 80,000 times on the
            # largest real account.
            _raise_if_cancelled(stop_event, _CANCEL_LEFT_NOTHING)

        sid = stream.get('stream_id')
        if not sid:
            continue
        if sid in seen_ids:
            skipped_duplicate += 1
            if len(duplicate_sample) < _DUPLICATE_SAMPLE_CAP:
                duplicate_sample.append(sid)
            continue
        seen_ids.add(sid)

        # M3U-sourced entries carry _stream_url; Xtream catalog entries have none, so the
        # URL is built from this account's own settings in the form the user selected.
        # It is deliberately NOT hardcoded: the shape a provider accepts varies, and the
        # form this used to hardcode (/live/<user>/<pass>/<id>.ts) is the one most often
        # blocked by Cloudflare (DESIGN-live-vod.md §4.3).
        raw_url = stream.get('_stream_url')
        if not raw_url:
            raw_url = construct_stream_url(account, sid, norm_mode, stream_origin)
        if '://' not in raw_url:
            # Some providers list placeholder/template catalog entries (e.g. per-team
            # "NFL Sunday Ticket" or per-state "Local Affiliates" rows) with a garbage
            # URL like the literal string "http" instead of a real stream - not a
            # parsing defect on our side, confirmed against the raw feed. These aren't
            # usable channels, so skip them entirely rather than importing dead rows.
            skipped_malformed += 1
            continue
        stream_url = normalize_url_with_mode(raw_url, norm_mode)
        # Stamped here, next to the URL it describes, so the stored flag can never disagree
        # with the raw_stream_url in the same row. Deliberately a property of the URL alone
        # and not of the account's mode: a channel whose account defers normalization is
        # still "has a triplet" or "has none", and the mode is applied by whoever reads it.
        normalizable = url_is_normalizable(raw_url)
        epg_id = stream.get('epg_channel_id') or ''
        cat_id = str(stream.get('category_id', ''))
        cat_name = stream.get('category_name') or ''

        if sid in existing:
            ch = existing[sid]
            # Captured before the comparison below - this is the only moment both the old
            # and new provider URL exist.
            if ch.raw_stream_url and ch.raw_stream_url != raw_url:
                drifted.append((ch.id, ch.raw_stream_url, raw_url))
            # Resolve the effective new value first, THEN compare - the four fallbacks
            # below are load-bearing and carried over unchanged from when these were
            # direct assignments. name/logo_url/category_name/category_id keep the stored
            # value when the provider omits or empties them, so "the provider said
            # nothing" must not read as a change. The other four have no fallback and
            # never did: an omitted epg_channel_id genuinely clears the stored one, and
            # the URL trio is recomputed from this sync's own feed every time.
            fields = {
                'name': stream.get('name', ch.name),
                'logo_url': stream.get('stream_icon') or ch.logo_url,
                'category_name': cat_name or ch.category_name,
                'category_id': cat_id or ch.category_id,
                'stream_url': stream_url,
                'raw_stream_url': raw_url,
                'url_normalizable': normalizable,
                'epg_channel_id': epg_id,
            }
            moved = [col for col, val in fields.items() if getattr(ch, col) != val]
            if moved:
                # updated_at is stamped explicitly rather than left to the column's
                # onupdate, so it is one value for the whole sync instead of a fresh
                # utcnow() per row - "when did this sync change this channel", not a
                # 57k-way tiebreak.
                row = {'id': ch.id, 'updated_at': sync_time,
                       'last_seen_at': sync_time, **fields}
                if not _SEARCH_TEXT_COLUMNS.isdisjoint(moved):
                    # Only a column ch_fts contains stales the channels search index. A
                    # provider that moved a logo or a category id changed this row, but
                    # changed nothing the index matches on, and must not cost a rebuild.
                    row['search_text_updated_at'] = sync_time
                changed.append(row)
            else:
                unchanged_ids.append(ch.id)
        else:
            ch = Channel(
                account_id=account.id,
                stream_id=sid,
                name=stream.get('name', f'Stream {sid}'),
                logo_url=stream.get('stream_icon'),
                category_name=cat_name,
                category_id=cat_id,
                stream_url=stream_url,
                raw_stream_url=raw_url,
                url_normalizable=normalizable,
                epg_channel_id=epg_id,
                in_guide=False,
                guide_sort_order=0,
                first_seen_at=sync_time,
                last_seen_at=sync_time,
                # MAX(id) already moves the watermark for an insert, so this is belt and
                # braces - but a row whose indexed text has never been stamped is exactly
                # the shape the stamp is supposed to rule out, so stamp it.
                search_text_updated_at=sync_time,
            )
            db.session.add(ch)
            new_channels.append(ch)
        synced += 1

    if skipped_duplicate:
        log.info(
            'Account %d sync: skipped %d channel(s) with a stream_id already seen this '
            'sync (sample: %s)', account.id, skipped_duplicate,
            ', '.join(str(s) for s in duplicate_sample))

    if changed:
        db.session.bulk_update_mappings(Channel, changed)
    _stamp_last_seen(unchanged_ids, sync_time)
    db.session.flush()
    return synced, skipped_malformed, skipped_duplicate, drifted, [ch.id for ch in new_channels]


# One UPDATE per this many ids. Well inside SQLite's bind-parameter ceiling, and measured
# flat across plausible sizes - 64 statements for the largest real account cost 0.60s
# against a 0.48s floor for a single unconditional whole-account UPDATE, so there is no
# reason to carry a second "the feed covered everything" code path for the last 0.12s.
_LAST_SEEN_CHUNK = 900


def _stamp_last_seen(channel_ids: list[int], sync_time: datetime) -> None:
    """Advance last_seen_at on channels this sync matched but did not change.

    Two details here are measured on this machine, not assumed, and both are silent
    corruption if reversed (dev/changelog/673):

    `updated_at=Channel.updated_at` is a deliberate self-assignment. A Core UPDATE fires
    the column's own `onupdate=datetime.utcnow`, which would move updated_at on every row
    and put back the exact lie this function exists to remove - naming the column in the
    SET clause is what suppresses that default. It is not redundant; deleting it is the
    bug.

    This is a Core UPDATE rather than raw text() SQL because text() bypasses SQLAlchemy's
    DateTime type and stores whatever the driver's adapter produces, which drops
    microseconds ('22:55:35' for '22:55:35.650439'). last_seen_at is compared with `<`
    against the sync's own timestamp in _raise_channel_lifecycle_alerts, so a truncated
    stamp can sort BELOW the sync that just wrote it and report every channel in the
    account as absent from its own feed - a spurious SYNC_FEED_SHRUNK alert.

    synchronize_session=False is safe because the rows are addressed by id and nothing
    reads them back from this session: _upsert_channels holds plain column tuples, not
    ORM instances, and both callers commit immediately (which expires the identity map
    anyway).
    """
    for i in range(0, len(channel_ids), _LAST_SEEN_CHUNK):
        db.session.execute(
            update(Channel)
            .where(Channel.id.in_(channel_ids[i:i + _LAST_SEEN_CHUNK]))
            .values(last_seen_at=sync_time, updated_at=Channel.updated_at),
            execution_options={'synchronize_session': False},
        )


def _recompute_duplicate_stream_urls() -> None:
    """Flag every Channel whose stream_url is shared by >1 channel, across all accounts.

    Runs after every sync (not on-demand) since a newly-synced account's channels can
    create or resolve duplicates involving channels from other accounts too.

    Two Core UPDATEs, one per direction, rather than a Python scan-and-flip over
    `Channel.query.all()`: this is a whole-database pass on every sync of any account, and
    hydrating every channel to compare one string cost 5.80s warm / 7.56s cold against the
    real 138,415-row database while changing zero rows, versus 0.59s for the two statements
    (dev/changelog/684). It also allocated 138k ORM objects on a box with no swap.

    It writes, so it holds the single write lock for as long as its enclosing closure runs,
    and a lock retry re-runs everything else in that closure with it: prefer
    `recompute_duplicate_stream_urls_and_commit()` below, which is that closure and holds
    nothing else (dev/changelog/685). Re-running the recompute itself is always safe - it
    derives every flag from the current table rather than from a diff.

    The duplicate set stays a subquery instead of a Python set so no URL ever crosses back
    into the process: both statements plan as a `SEARCH channels USING INDEX
    ix_channels_is_duplicate_stream_url` with the group-by materialized once as a list
    subquery. `stream_url` is NOT NULL, and the `://` filter cannot admit a NULL either, so
    the NOT IN below has no three-valued-logic hole to fall through.

    `updated_at` is deliberately NOT self-assigned here (contrast `_stamp_last_seen` above,
    which must suppress it): a row whose flag actually flips did change, and letting the
    column's `onupdate` fire keeps this identical to the ORM flush it replaces.

    synchronize_session=False is safe for the same reason it is in `_stamp_last_seen` -
    all four callers commit immediately, which expires the identity map anyway.
    """
    dup_urls = (
        select(Channel.stream_url)
        .where(Channel.stream_url.contains('://'))  # skip malformed/truncated URLs - not a real duplicate signal
        .group_by(Channel.stream_url)
        .having(func.count(Channel.id) > 1)
    )
    db.session.execute(
        update(Channel)
        .where(Channel.is_duplicate_stream_url.is_(False),
               Channel.stream_url.in_(dup_urls))
        .values(is_duplicate_stream_url=True),
        execution_options={'synchronize_session': False},
    )
    db.session.execute(
        update(Channel)
        .where(Channel.is_duplicate_stream_url.is_(True),
               Channel.stream_url.not_in(dup_urls))
        .values(is_duplicate_stream_url=False),
        execution_options={'synchronize_session': False},
    )


@retry_on_locked()
def recompute_duplicate_stream_urls_and_commit() -> None:
    """The recompute above as its own retried commit unit - the way callers should run it.

    Kept deliberately bare: whatever else shares a `retry_on_locked` closure with these two
    write statements is redone on every lock retry, and is holding the write lock while they
    run (dev/changelog/685).
    """
    _recompute_duplicate_stream_urls()
    db.session.commit()


def duplicate_groups_within(channels) -> list[list]:
    """Group an already-fetched list of Channel objects by stream_url. Returns only
    groups with 2+ members, each sorted by id ascending (oldest first)."""
    by_url: dict[str, list] = {}
    for ch in channels:
        by_url.setdefault(ch.stream_url, []).append(ch)
    return [sorted(group, key=lambda c: c.id) for group in by_url.values() if len(group) > 1]


def duplicates_within(channels) -> dict[int, str]:
    """For any channel in this list whose stream_url is shared by another channel in the
    same list, map its id to a tooltip string naming the other(s). Channels with no
    in-list duplicate are omitted."""
    titles: dict[int, str] = {}
    for group in duplicate_groups_within(channels):
        for ch in group:
            others = [c for c in group if c.id != ch.id]
            names = ', '.join(f'{c.name} ({c.account.name})' for c in others)
            titles[ch.id] = f'Duplicate of: {names}'
    return titles


def _cancel_kept_source(source_name: str) -> str:
    return (f'the channel list was updated; the import of EPG source "{source_name}" stopped '
            'before it changed anything, so its previous listings were kept.')


def _source_fetch_url(source: EpgSource) -> str:
    if source.kind == EPG_SOURCE_PROVIDER:
        return provider_xmltv_url(source.owner)
    if source.kind == EPG_SOURCE_URL:
        return source.url or ''
    raise ValueError(f'EPG source {source.id} has unknown kind {source.kind!r}')


def refresh_source(source: EpgSource, timeout: int, epg_days: int, cfg: dict | None = None,
                   force_epg_resync: bool = False,
                   stop_event: threading.Event | None = None,
                   track_progress: bool = True) -> tuple[int, str | None]:
    """Refresh one source, one at a time per source, with `refresh_started_at` set for as
    long as it runs so the restart guard can see it. A source already being refreshed by
    another path is skipped, not queued behind it: that refresh is fetching the same file.
    See _refresh_source for the return value."""
    source_id = source.id
    lock = _get_source_lock(source_id)
    if not lock.acquire(blocking=False):
        log.info('EPG source %d (%s) is already being refreshed - skipped', source_id,
                 source.name)
        return 0, None
    try:
        _set_refreshing(source_id, True)
        try:
            return _refresh_source(source, timeout, epg_days, cfg, force_epg_resync,
                                   stop_event, track_progress)
        finally:
            _set_refreshing(source_id, False)
    finally:
        lock.release()


def _set_refreshing(source_id: int, on: bool) -> None:
    @retry_on_locked()
    def _stamp_and_commit():
        src = db.session.get(EpgSource, source_id)
        if src is not None:
            src.refresh_started_at = datetime.utcnow() if on else None
            db.session.commit()

    _stamp_and_commit()


#: Every standing alert a source can hold, by the key suffix it is raised under.
_SOURCE_ALERTS = (('EPG_SOURCE_FETCH_FAILED', 'fetch'), ('EPG_SOURCE_COLLAPSE_REFUSED', 'collapse'),
                  ('EPG_SOURCE_IMPORT_TRUNCATED', 'truncated'),
                  ('EPG_SOURCE_COVERAGE_LOST', 'coverage'), ('EPG_SOURCE_STALE', 'stale'))


def start_source_refresh(app, source_id: int) -> tuple[bool, str]:
    """Refresh one source now, in the background, outside any account sync - Refresh now,
    and the refresh a new or re-pointed source gets at once (its directory is empty until
    then, so it covers nothing). Returns (started, message for the user).

    Admission is asked here, in the request, so a refusal can be answered: it takes the
    sync ticket (DESIGN-epg-sources.md §8.1), so it never overlaps an account sync or
    another source's refresh. A refusal queues a retry rather than dropping the refresh,
    and the message names what it is waiting behind.
    """
    source = db.session.get(EpgSource, source_id)
    if source is None:
        return False, 'That EPG source no longer exists.'
    name = source.name
    ticket = admission.try_start(admission.KIND_SYNC, f'EPG source {name}')
    if not ticket.granted:
        from .scheduler import defer_source_refresh
        when = defer_source_refresh(source_id, ticket.reason)
        later = (f'It will start at {format_local(when)}.' if when
                 else 'Use Refresh now again once that has finished.')
        return False, f'{ticket.reason[0].upper()}{ticket.reason[1:]}, so the refresh of "{name}" waits. {later}'
    threading.Thread(target=refresh_source_standalone, args=(app, source_id),
                     kwargs={'ticket': ticket}, daemon=True,
                     name=f'epg-source-refresh-{source_id}').start()
    return True, f'Refreshing "{name}" now.'


def refresh_source_standalone(app, source_id: int, ticket=None):
    """One source's refresh on its own - its interval job, Refresh now, or a deferred
    retry. Returns the admission Refusal when it could not start (the caller decides where
    that goes), else None. A `ticket` already granted is used and released here."""
    if ticket is None:
        with app.app_context():
            source = db.session.get(EpgSource, source_id)
            if source is None:
                return None
            label = f'EPG source {source.name}'
        ticket = admission.try_start(admission.KIND_SYNC, label)
        if not ticket.granted:
            return ticket
    try:
        with app.app_context():
            source = db.session.get(EpgSource, source_id)
            if source is None:
                return None
            cfg = load_config()
            sync_cfg = cfg.get('sync', {})
            try:
                refresh_source(source, sync_cfg.get('request_timeout_seconds', 30),
                               sync_cfg.get('epg_days_ahead', 3), cfg, track_progress=False)
            finally:
                update_source_stale_alert(source_id)
    finally:
        admission.release(ticket)
    return None


def update_source_stale_alert(source_id: int) -> None:
    """Raise or clear EPG_SOURCE_STALE: a source on its own interval with no good refresh in
    OVERDUE_INTERVAL_MULTIPLE intervals. Called at each end of the condition - after every
    refresh attempt, when a refresh is deferred, and when the source is edited - so nothing
    has to poll, the same rule update_overdue_alert follows. A never-refreshed source is
    measured from when it was added: one that has never worked is the stalest of all."""
    source = db.session.get(EpgSource, source_id)
    if source is None:
        return
    since = source.last_success_at or source.created_at
    interval = source.refresh_interval_hours
    active = bool(source.enabled and interval and since
                  and datetime.utcnow() - since >= timedelta(hours=interval * OVERDUE_INTERVAL_MULTIPLE))
    body = ''
    if active:
        last = (f'its last good refresh was {format_local(source.last_success_at)}'
                if source.last_success_at else 'it has not had a good refresh since it was added')
        body = (f'EPG source "{source.name}" refreshes every {interval}h, but {last} - more '
                f'than {interval * OVERDUE_INTERVAL_MULTIPLE}h ago. Its listings are still the '
                'ones from its last good refresh. The source\'s row on its account page says '
                'why the refreshes since have not worked.')
    _raise_or_resolve_standing_alert(
        'EPG_SOURCE_STALE', source=f'epg-source:{source_id}:stale', active=active,
        title=f'EPG source "{source.name}" is stale', body=body)


def resolve_source_alerts(source_id: int) -> None:
    """Clear every standing alert a deleted source held - nothing will ever refresh it to
    clear them itself."""
    for alert_type, suffix in _SOURCE_ALERTS:
        _raise_or_resolve_standing_alert(alert_type, source=f'epg-source:{source_id}:{suffix}',
                                         active=False)


def report_source_removed(source_id: int, source_name: str, cause: str, readers,
                          guided: dict[int, int], outcome: dict[int, int | None]) -> None:
    """Raise EPG_SOURCE_REMOVED for a deleted source that other accounts read (§9.5):
    which accounts, how many of their channels took their guide from it, and what became
    of them. `guided` is {channel_id: source_id} collected before the delete
    (foreign_guided_channels); `outcome` is reresolve_channels()' {channel_id: new winner}.
    Nothing to say when no other account read it."""
    readers = [r for r in readers if r.source_id == source_id]
    if not readers:
        return
    mine = [cid for cid, sid in guided.items() if sid == source_id]
    lost = [cid for cid in mine if not outcome.get(cid)]
    who = ', '.join(f'"{r.account_name}" ({r.guided:,} channel(s) took their guide from it)'
                    for r in readers)
    body = f'EPG source "{source_name}" was deleted {cause}. Accounts reading it: {who}.'
    if mine:
        body += f' {len(mine) - len(lost):,} of those channel(s) now take their guide from another source'
        if lost:
            sample = [n for (n,) in db.session.query(Channel.name).filter(
                Channel.id.in_(lost[:_LOST_CHANNELS_NAMED])).order_by(Channel.name)]
            more = len(lost) - len(sample)
            body += (f'; {len(lost):,} have no guide from any source now: {", ".join(sample)}'
                     + (f', and {more:,} more' if more > 0 else ''))
        body += '.'
    from .alerts import create_alert
    create_alert('EPG_SOURCE_REMOVED', f'EPG source "{source_name}" was removed', body,
                 source=f'epg-source:{source_id}:removed')


def _refresh_source(source: EpgSource, timeout: int, epg_days: int, cfg: dict | None,
                    force_epg_resync: bool, stop_event: threading.Event | None,
                    track_progress: bool) -> tuple[int, str | None]:
    """Fetch one EPG source's XMLTV and import it (DESIGN-epg-sources.md §8).

    Returns (entries_imported, degradation_reason). `degradation_reason` is None on a
    healthy refresh; a fetch exception is a value the caller must handle, finishing the sync
    PARTIAL instead of plain SUCCESS with a silently-stale guide (DESIGN-sync-resilience.md
    §2). 'import refused:' means the collapse guard (§4) refused the import -
    force_epg_resync bypasses that guard for this one call - and 'import truncated:' means
    the payload stopped parsing after the delete had committed. Every prefix this returns
    must have an entry in EPG_DEGRADATION_ALERT_TYPES. The source's own alerts and status
    are written before this returns.
    """
    source_id, source_name = source.id, source.name
    url = _source_fetch_url(source)
    log.info('Fetching XMLTV for EPG source %d (%s) from %s', source_id, source_name,
             mask_url_path(url))
    reason = None
    try:
        resp = requests.get(url, timeout=timeout, stream=True, headers=_request_headers(cfg))
        resp.raise_for_status()
        xml_bytes = resp.content
    except Exception as exc:
        # Masked: this reason is persisted into AccountSyncLog.error_message and rendered
        # in the UI, and requests exceptions stringify with the full URL that was fetched.
        # Account-owned, so the whole path goes, not just the heuristic shapes.
        reason = f'fetch failed: {mask_account_urls_in_text(str(exc), url)}'
    else:
        # raise_for_status() only covers 4xx and 5xx. These providers answer their playlist
        # endpoint with a made-up HTTP 884 and an empty body, which it lets through.
        if not 200 <= resp.status_code < 300:
            reason = (f'fetch failed: the provider answered HTTP {resp.status_code} '
                      f'({fmt_bytes(len(xml_bytes))}), not a success status')
    if reason:
        log.warning('XMLTV fetch failed for EPG source %d (%s): %s - listings kept',
                    source_id, source_name, reason)
        _report_source_outcome(source_id, reason, None)
        return 0, reason
    # The download is one blocking call, so this is the first moment a cancel issued during
    # it can land - and it lands before the import deletes anything.
    _raise_if_cancelled(stop_event, _cancel_kept_source(source_name))
    case_sensitive = (cfg or {}).get('sync', {}).get('epg_case_sensitive_matching', False)
    return import_source(source, xml_bytes, epg_days, case_sensitive, cfg, force_epg_resync,
                         stop_event=stop_event, track_progress=track_progress)


def _match_program(elem, channel_map: dict, case_sensitive: bool,
                     window_start: datetime, window_end: datetime):
    """(channel_ids, start_dt, stop_dt) this <programme> element would import into, or
    None if out of scope (unmatched channel id, unparseable timestamps, or outside the
    import window). Shared by the collapse guard's count pass
    (_count_projected_epg_entries) and the real import loop below
    (DESIGN-sync-resilience.md §4) so the two can never drift on what counts as a match.
    """
    epg_channel_id = elem.get('channel', '')
    key = epg_channel_id if case_sensitive else epg_channel_id.lower()
    channel_ids = channel_map.get(key)
    if not channel_ids:
        return None
    try:
        start_dt = _parse_xmltv_dt(elem.get('start', ''))
        stop_dt = _parse_xmltv_dt(elem.get('stop', ''))
    except ValueError:
        return None
    if stop_dt < window_start or start_dt > window_end:
        return None
    return channel_ids, start_dt, stop_dt


class ProjectedEpgCount(NamedTuple):
    """What the scan pass saw. `programs_seen` and `channels_seen` count every <programme> /
    <channel> element in the feed, matched or not, which is what lets a refusal tell "the
    feed had no listings" from "it had listings for other channels" (dev/changelog/1100).
    `directory` is the source directory the pass built (DESIGN-epg-sources.md §7.4)."""
    projected: int
    parse_error: str | None
    programs_seen: int
    channels_seen: int
    directory: list | None = None


def _count_projected_epg_entries(xml_bytes: bytes, channel_map: dict, case_sensitive: bool,
                                  window_start: datetime, window_end: datetime,
                                  stop_event: threading.Event | None = None,
                                  cancel_detail: str = _CANCEL_KEPT_EPG
                                  ) -> ProjectedEpgCount:
    """One pass over the feed that counts the entries the real import would create - the
    collapse guard's projected count (DESIGN-sync-resilience.md §4) - and builds the source
    directory: every <channel>'s display names and, per channel id, its in-window program
    count, distinct titles (capped) and horizon (DESIGN-epg-sources.md §7.4). CPU only, over
    bytes already in memory, and no ORM objects.

    Runs on every import, not only when the guard is armed: the directory is what decides
    which source wins a channel, and that has to be known before any row is written.

    `stop_event` makes this pass cancellable: it is a full extra walk of the payload, which
    on a large feed is long enough that ignoring a cancel through it is exactly the wait
    dev/changelog/720 removed. Stopping here is free - nothing has been written.

    A malformed or truncated payload is reported as a value rather than raised. Letting it
    propagate failed the whole sync as ERROR - misattributing an EPG-only fault to a channel
    sync that had already committed successfully (dev/changelog/719). The counts returned
    alongside the error are what parsed before the break, and are not comparable against a
    threshold: the caller refuses on the error itself.
    """
    total = 0
    seen = 0
    channels = 0
    names: dict[str, list] = {}
    counts: dict[str, int] = {}
    titles: dict[str, set] = {}
    horizon: dict[str, datetime] = {}
    # Per channel, the UPCOMING_TITLES programs starting soonest among those not over yet.
    # Kept as a small sorted list rather than every program, so memory stays per channel.
    upcoming: dict[str, list] = {}
    now = datetime.utcnow()
    error = None
    try:
        # Only the two top-level elements are cleared. Clearing every element on its end
        # event would empty <title> and <display-name> before their parent's end event
        # reads them.
        for _event, elem in ET.iterparse(BytesIO(xml_bytes), events=('end',)):
            if elem.tag == 'programme':
                seen += 1
                if seen % _CANCEL_POLL_PROGRAMS == 0:
                    _raise_if_cancelled(stop_event, cancel_detail)
                xml_id = elem.get('channel', '')
                try:
                    start_dt = _parse_xmltv_dt(elem.get('start', ''))
                    stop_dt = _parse_xmltv_dt(elem.get('stop', ''))
                except ValueError:
                    elem.clear()
                    continue
                if stop_dt >= window_start and start_dt <= window_end:
                    counts[xml_id] = counts.get(xml_id, 0) + 1
                    title_el = elem.find('title')
                    title = (title_el.text or '').strip() if title_el is not None else ''
                    seen_titles = titles.setdefault(xml_id, set())
                    if len(seen_titles) < DISTINCT_TITLE_CAP:
                        seen_titles.add(title)
                    if stop_dt > now:
                        soon = upcoming.setdefault(xml_id, [])
                        if len(soon) < UPCOMING_TITLES or start_dt < soon[-1][0]:
                            bisect.insort(soon, (start_dt, title))
                            del soon[UPCOMING_TITLES:]
                    if xml_id not in horizon or stop_dt > horizon[xml_id]:
                        horizon[xml_id] = stop_dt
                    match = _match_program(elem, channel_map, case_sensitive,
                                           window_start, window_end)
                    if match is not None:
                        total += len(match[0])
                elem.clear()
            elif elem.tag == 'channel':
                channels += 1
                xml_id = elem.get('id', '')
                if xml_id:
                    names[xml_id] = [d.text.strip() for d in elem.findall('display-name')
                                     if d.text and d.text.strip()]
                elem.clear()
    except (ET.ParseError, DefusedXmlException) as exc:
        # Unknown provenance - this text comes from the provider's payload, not from an
        # account-owned URL, so the generic masker is the right one (DESIGN-secrets.md §4.2).
        error = mask_creds_in_text(str(exc))
    directory = []
    for xml_id in sorted(set(names) | set(counts)):
        if not xml_id:
            continue
        t = titles.get(xml_id, set())
        directory.append(DirectoryRow(
            xml_id=xml_id, display_names=names.get(xml_id, []),
            entry_count=counts.get(xml_id, 0), distinct_titles=len(t),
            sole_title=next(iter(t)) if len(t) == 1 else None,
            horizon_until=horizon.get(xml_id),
            upcoming=tuple(upcoming.get(xml_id, ()))))
    return ProjectedEpgCount(total, error, seen, channels, directory)


def _empty_feed_reason(xml_bytes: bytes, channels_seen: int | None, baseline: int) -> str:
    """The refusal for a well-formed feed with no <programme> elements at all. Worded apart
    from a threshold refusal because the cause is the provider's, not a matching problem:
    the channel list can arrive intact with every listing missing, which is what account 3
    received on 2026-09-23 (dev/changelog/1100). `channels_seen` is None when no count pass
    ran."""
    shape = f'{fmt_bytes(len(xml_bytes))}'
    if channels_seen is not None:
        shape += f', {channels_seen} channel entries'
    kept = (f'previous sync had {baseline}. Old EPG data was kept'
            if baseline else 'there was no previous EPG to keep')
    return (f'import refused: the provider\'s XMLTV feed contained no program listings at '
            f'all ({shape}, 0 programs) - {kept}; use "Force EPG Resync" to import it anyway.')


# gzip's magic number. A provider that serves its XMLTV as a .xml.gz FILE sends it with a
# file content type (application/octet-stream or application/gzip) and no Content-Encoding
# header, so requests hands back the compressed bytes untouched - unlike a response merely
# transfer-compressed with Content-Encoding: gzip, which requests decodes for us. Sniffing
# the bytes covers both without having to trust any header.
_GZIP_MAGIC = b'\x1f\x8b'


def _maybe_gunzip(xml_bytes: bytes) -> tuple[bytes, str | None]:
    """(xml_bytes, degradation_reason) - decompress a gzipped XMLTV payload, or pass
    plain bytes through untouched.

    A great many public and provider EPG endpoints are .xml.gz. Before this existed the
    compressed bytes went straight to the parser, which found no <programme> elements and
    reported a healthy import of 0 entries - a silent, permanently-empty guide with nothing
    anywhere naming the cause (dev/docs/BUGS.md 2026-08-09, dev/changelog/523).
    """
    if not xml_bytes.startswith(_GZIP_MAGIC):
        return xml_bytes, None
    try:
        plain = gzip.decompress(xml_bytes)
    except (OSError, EOFError, zlib.error) as exc:
        # Named rather than swallowed: the payload announced itself as gzip and then failed
        # to decompress, which is a truncated or corrupt download, not an empty guide.
        log.warning('XMLTV payload has gzip magic bytes but would not decompress: %s', exc)
        return xml_bytes, f'fetch failed: XMLTV looked gzipped but would not decompress: {exc}'
    log.info('XMLTV payload was gzipped: %s -> %s', fmt_bytes(len(xml_bytes)),
             fmt_bytes(len(plain)))
    return plain, None


def _visible_source_baseline(source_id: int) -> int:
    """The collapse guard's baseline for one source: the rows it holds, in both tables, on
    channels that are NOT hidden (DESIGN-epg-sources.md §8.2).

    Per source, not per account: an account-wide count would read a second, smaller feed
    imported after the first as a 90% collapse and refuse it forever.

    Counted live rather than read from a cached total, because hiding moves the
    comparison's other side. `projected` counts only what the import would create, and since
    dev/changelog/781 that excludes hidden channels - so a total taken before a large hide
    reads a legitimate drop as a provider returning garbage and freezes the guide. A live
    count is right whatever route did the hiding, and excludes rows sitting on a hidden
    channel even if the purge in `channel_hiding.recompute()` never ran.

    `baseline == 0` still means "no prior successful import to compare against" and leaves
    the guard unarmed (DESIGN-sync-resilience.md §4).
    """
    total = 0
    for model in (EPGEntry, EpgAlternateEntry):
        total += db.session.query(func.count(model.id)).join(
            Channel, Channel.id == model.channel_id).filter(
                model.source_id == source_id, Channel.hidden.is_(False)).scalar() or 0
    return total


class _ImportResult(NamedTuple):
    synced: int
    reason: str | None
    # None = the import never reached resolution (refused, undecodable), so coverage was
    # not re-evaluated and a standing coverage alert must be left as it is.
    lost_channel_ids: list | None


def import_source(source: EpgSource, xml_bytes: bytes, epg_days: int,
                  case_sensitive: bool = False, cfg: dict | None = None,
                  force_epg_resync: bool = False,
                  stop_event: threading.Event | None = None,
                  track_progress: bool = True) -> tuple[int, str | None]:
    """Import one EPG source's XMLTV payload for every account subscribed to it
    (DESIGN-epg-sources.md §8.2), write the source's status and raise or clear its alerts.

    Returns (entries_imported, degradation_reason), None = healthy. A reason starting
    'import refused:' means the collapse guard (DESIGN-sync-resilience.md §4) skipped the
    delete and import entirely, keeping the source's listings in both tables untouched;
    force_epg_resync bypasses the guard. One starting 'import truncated:' means the delete
    had already committed when the payload stopped parsing, so the source holds only what
    was imported. The two need opposite wording in front of the user (dev/changelog/719).

    Accepts gzipped XMLTV, decompressed here so the Xtream dump path gets it too and the
    scan pass reads the same bytes the import loop does.
    """
    began_at = datetime.utcnow()
    result = _import_source(source, xml_bytes, epg_days, case_sensitive, cfg,
                            force_epg_resync, stop_event, track_progress)
    _report_source_outcome(source.id, result.reason, result.lost_channel_ids, began_at)
    return result.synced, result.reason


def _import_source(source: EpgSource, xml_bytes: bytes, epg_days: int, case_sensitive: bool,
                   cfg: dict | None, force_epg_resync: bool,
                   stop_event: threading.Event | None,
                   track_progress: bool = True) -> _ImportResult:
    source_id, source_name = source.id, source.name
    # A standalone refresh has no sync progress entry to write, and one written here would
    # outlive it: only sync_account's own teardown clears them.
    progress_id = source.owner_account_id if track_progress else None
    kept_detail = _cancel_kept_source(source_name)

    xml_bytes, gzip_reason = _maybe_gunzip(xml_bytes)
    if gzip_reason:
        return _ImportResult(0, gzip_reason, None)

    now = datetime.utcnow()
    window_start = now - timedelta(hours=1)
    window_end = now + timedelta(days=epg_days)

    # Every channel this import can say anything about: the subscribers' channels, plus any
    # channel elsewhere still pointing at this source (its account unsubscribed since).
    subscribers = subscriber_ids(source_id)
    scope = Channel.epg_source_id == source_id
    if subscribers:
        scope = db.or_(Channel.account_id.in_(subscribers), scope)
    channels = db.session.query(
        Channel.id, Channel.account_id, Channel.epg_channel_id, Channel.hidden,
        Channel.epg_source_id, Channel.epg_source_override_id).filter(scope).all()
    order = subscriptions_for({c.account_id for c in channels})
    candidate_sources = {sid for sids in order.values() for sid in sids}
    candidate_sources |= {c.epg_source_override_id for c in channels
                          if c.epg_source_override_id}
    candidate_sources.add(source_id)
    user_keys = accepted_keys(candidate_sources)

    # {normalized key: [channel ids]} over this source's key for each channel (§7.1).
    # HIDDEN CHANNELS ARE EXCLUDED, and this is the whole EPG saving of dev/changelog/781:
    # the map decides which <programme> elements become rows. The scan pass is handed this
    # same map, so the guard's count follows without a second filter - the two share
    # `_match_program` precisely so they cannot drift on what counts as a match.
    subscribed = set(subscribers)
    channel_map: dict[str, list[int]] = {}
    for c in channels:
        if c.hidden or c.account_id not in subscribed:
            continue
        k = channel_key(c.epg_channel_id, user_keys.get((c.id, source_id)))
        if k:
            channel_map.setdefault(norm_key(k, case_sensitive), []).append(c.id)

    if not channel_map:
        # The directory is still written: it is what the name-match review page proposes
        # from, and an account whose channels carry no ids at all is exactly the one that
        # needs it (DESIGN-epg-sources.md §7.5, dev/changelog/1105).
        count = _count_projected_epg_entries(
            xml_bytes, {}, case_sensitive, window_start, window_end,
            stop_event=stop_event, cancel_detail=kept_detail)
        if count.parse_error is None:
            write_directory(source_id, count.directory or [])
        log.info('No visible channels with an EPG key for EPG source %d (%s), skipping import',
                 source_id, source_name)
        return _ImportResult(0, None, None)

    threshold_pct = (cfg or {}).get('sync', {}).get('epg_collapse_threshold_percent', 20)
    baseline = _visible_source_baseline(source_id)
    count = _count_projected_epg_entries(
        xml_bytes, channel_map, case_sensitive, window_start, window_end,
        stop_event=stop_event, cancel_detail=kept_detail)
    projected = count.projected
    reason = None
    if threshold_pct > 0 and baseline == 0 and b'<programme' not in xml_bytes:
        # Unarmed guard, empty feed. Nothing would be lost by importing it, but it would be
        # a SUCCESS with 0 entries and no word on any surface - the state a source lands in
        # one refresh after a real empty-feed refusal, once pruning has emptied its baseline.
        # A byte search rather than the scan's count: a malformed payload's count stops at
        # the break, and a feed lacking the literal tag cannot hold an element the import
        # would read.
        reason = _empty_feed_reason(xml_bytes, count.channels_seen, baseline)
    elif threshold_pct > 0 and baseline > 0:
        min_required = baseline * threshold_pct / 100
        if count.parse_error:
            # A payload that will not parse all the way through is refused on that fact
            # alone, never on the threshold: `projected` is only what parsed before the
            # break. Refusing keeps the old listings - the only half where nothing is lost.
            # Unarmed (no baseline), the import goes ahead and reports itself truncated.
            reason = (f'import refused: the XMLTV feed is truncated or malformed - parsing '
                      f'stopped after {projected} projected entries ({count.parse_error}). '
                      'Old EPG data was kept; use "Force EPG Resync" to import whatever '
                      'does parse.')
        elif count.programs_seen == 0:
            reason = _empty_feed_reason(xml_bytes, count.channels_seen, baseline)
        elif projected < min_required:
            # Rounded up: `projected` is a whole number, so "below 0.2" is "below 1".
            required = math.ceil(min_required)
            if projected == 0:
                what = (f'the feed had {count.programs_seen} programs, but none matched the '
                        'EPG id of a visible channel reading this source inside the import '
                        'window')
            else:
                what = f'provider returned {projected} matching EPG entries'
            reason = (f'import refused: {what}; previous sync had {baseline} - below the '
                      f'{threshold_pct}% collapse threshold (at least {required} required). '
                      'Old EPG data was kept; use "Force EPG Resync" to override.')
    if reason:
        if force_epg_resync:
            log.info('EPG collapse guard bypassed for EPG source %d (Force EPG Resync): %s',
                     source_id, reason)
        else:
            log.warning('EPG collapse guard refused import for EPG source %d (%s): %s',
                        source_id, source_name, reason)
            return _ImportResult(0, reason, None)

    # The last point at which stopping costs the user nothing: the directory rewrite and
    # the row moves below change what the guide shows.
    _raise_if_cancelled(stop_event, kept_detail)

    write_directory(source_id, count.directory or [])

    # Who wins each channel (DESIGN-epg-sources.md §5), decided before a row is written so
    # every row lands in the table it belongs in.
    coverage = directory_coverage(candidate_sources, case_sensitive)
    # Another source with no directory yet covers what it holds rows for - read as "covers
    # nothing", this import would take its channels from it (dev/changelog/1107).
    unrefreshed = sources_without_directory(candidate_sources - {source_id})
    held = (held_coverage(unrefreshed, [c.id for c in channels if not c.hidden])
            if unrefreshed else {})
    changes: dict[int, tuple] = {}
    reasons: dict[int, str] = {}
    winners: dict[int, int | None] = {}
    lost: list[int] = []
    covered_here = 0
    for c in channels:
        if c.hidden:
            new, why = None, REASON_NONE
        else:
            ordered = order.get(c.account_id, [])
            cov = {}
            allowed = set(ordered) | ({c.epg_source_override_id}
                                      if c.epg_source_override_id else set())
            for sid in allowed:
                k = channel_key(c.epg_channel_id, user_keys.get((c.id, sid)))
                hit = coverage.get(sid, {}).get(norm_key(k, case_sensitive)) if k else None
                if hit is not None:
                    cov[sid] = hit
            if held:
                add_held_coverage(cov, c.id, held, allowed)
            if source_id in cov and c.account_id in subscribed:
                covered_here += 1
            new, why = resolve_active_source(c.epg_source_override_id, ordered, cov)
        winners[c.id] = new
        if new != c.epg_source_id:
            changes[c.id] = (c.epg_source_id, new)
            reasons[c.id] = why
            if c.epg_source_id is not None and new is None and not c.hidden:
                lost.append(c.id)

    # A change of winner is a move between the two tables, never a re-fetch (§5.4). Done
    # before the delete and the inserts, so a cancel partway through the inserts leaves no
    # channel with two sources' listings in epg_entries at once. A channel that lost its
    # last source keeps what it has: this source's in-window rows go in the delete below.
    touched_sources = {source_id}
    by_old: dict[int, list[int]] = {}
    by_new: dict[int, list[int]] = {}
    for ch_id, (old, new) in changes.items():
        if new is None:
            continue
        if old is not None:
            by_old.setdefault(old, []).append(ch_id)
        by_new.setdefault(new, []).append(ch_id)
    for sid, ids in by_old.items():
        demote(ids, sid)
        touched_sources.add(sid)
    for sid, ids in by_new.items():
        promote(ids, sid)
        touched_sources.add(sid)
    if changes:
        names = dict(db.session.query(EpgSource.id, EpgSource.name).filter(
            EpgSource.id.in_({s for pair in changes.values() for s in pair if s})))
        apply_winners(changes, names, reasons)

    # Delete scope = this source's rows in the window, both tables (§8.2). No channel join:
    # a channel hidden since the last refresh has its rows cleared by the same statement.
    @retry_on_locked()
    def _delete_old_epg_and_commit():
        for model in (EPGEntry, EpgAlternateEntry):
            model.query.filter(model.source_id == source_id,
                               model.stop_time >= window_start).delete(synchronize_session=False)
        db.session.commit()

    _delete_old_epg_and_commit()

    @retry_on_locked()
    def _save_epg_batch_and_commit(active_rows, alternate_rows):
        if active_rows:
            db.session.bulk_insert_mappings(EPGEntry, active_rows)
        if alternate_rows:
            db.session.bulk_insert_mappings(EpgAlternateEntry, alternate_rows)
        db.session.commit()

    synced = 0
    handled = 0
    last_checkpoint = 0
    active_batch: list = []
    alternate_batch: list = []
    batch_size = 2000
    truncated_reason: str | None = None

    def _flush():
        nonlocal active_batch, alternate_batch
        if active_batch or alternate_batch:
            _save_epg_batch_and_commit(active_batch, alternate_batch)
            active_batch, alternate_batch = [], []

    try:
        # start+end events so a <programme>'s children are not cleared before its own end
        # event reads them.
        context = ET.iterparse(BytesIO(xml_bytes), events=('start', 'end'))
        in_program = False
        for event, elem in context:
            if event == 'start':
                if elem.tag == 'programme':
                    in_program = True
                continue
            if elem.tag != 'programme':
                if not in_program:
                    elem.clear()  # safe to clear top-level non-program elements
                continue
            in_program = False

            match = _match_program(elem, channel_map, case_sensitive, window_start, window_end)
            if match is None:
                elem.clear()
                continue
            channel_ids, start_dt, stop_dt = match

            title_el = elem.find('title')
            desc_el = elem.find('desc')
            sub_title_el = elem.find('sub-title')
            cat_el = elem.find('category')
            rating_el = elem.find('rating/value')

            title = (title_el.text or '').strip() if title_el is not None else ''
            description = (desc_el.text or '').strip() if desc_el is not None else None
            sub_title = (sub_title_el.text or '').strip() if sub_title_el is not None else None
            category = (cat_el.text or '').strip() if cat_el is not None else None
            rating = (rating_el.text or '').strip() if rating_el is not None else None

            for channel_id in channel_ids:
                winner = winners.get(channel_id)
                if winner is None:
                    continue
                row = {'channel_id': channel_id, 'source_id': source_id, 'title': title,
                       'description': description, 'sub_title': sub_title,
                       'start_time': start_dt, 'stop_time': stop_dt,
                       'category': category, 'rating': rating}
                if winner == source_id:
                    active_batch.append(row)
                    synced += 1
                else:
                    alternate_batch.append(row)
                handled += 1
            elem.clear()

            if handled - last_checkpoint >= 500:
                time.sleep(0)  # yield GIL so Flask request threads can run
                if progress_id is not None:
                    _set_sync_progress(progress_id, 'epg', synced, None)
                last_checkpoint = handled
                if stop_event is not None and stop_event.is_set():
                    # Salvage before stopping: `synced` counts rows appended to the batch,
                    # so anything still buffered would be reported as imported without
                    # ever having been inserted.
                    _flush()
                    raise SyncCancelled(
                        f'the import of EPG source "{source_name}" had already cleared its '
                        f'previous listings, so it holds only the {synced} entries imported '
                        'before the cancel, until its next successful refresh.')

            if len(active_batch) + len(alternate_batch) >= batch_size:
                _flush()

        _flush()

    except (ET.ParseError, DefusedXmlException) as exc:
        _flush()
        detail = mask_creds_in_text(str(exc))
        log.warning('XMLTV parse error for EPG source %d (%s): %s - partial EPG imported',
                    source_id, source_name, detail)
        # Not None, and deliberately not silent: the delete above has already committed,
        # so the source now holds only what parsed (dev/changelog/719).
        truncated_reason = (f'import truncated: the XMLTV feed stopped parsing after {synced} '
                            f'entries were imported ({detail}). The source\'s old EPG data '
                            'had already been cleared, so the guide holds only those entries '
                            'until the next successful sync.')

    # Alternates are kept only for channels with an active source (§4).
    orphaned = [ch for ch, (old, new) in changes.items() if new is None and old is not None]
    for sid in {changes[ch][0] for ch in orphaned}:
        drop_alternates([ch for ch in orphaned if changes[ch][0] == sid], sid)

    @retry_on_locked()
    def _refresh_counts_and_commit():
        for sid in touched_sources:
            refresh_source_counts(sid, covered_here if sid == source_id else None)
        db.session.commit()

    _refresh_counts_and_commit()

    if progress_id is not None:
        _set_sync_progress(progress_id, 'epg', synced, None)
    log.info('Imported %d EPG entries from EPG source %d (%s)', synced, source_id, source_name)
    return _ImportResult(synced, truncated_reason, lost)


def _report_source_outcome(source_id: int, reason: str | None,
                           lost_channel_ids: list | None,
                           began_at: datetime | None = None) -> None:
    """Write one refresh's outcome onto the source and raise or clear its alerts
    (DESIGN-epg-sources.md §9.1). The three refresh types are evaluated on every refresh, so
    whichever one is not this refresh's reason is auto-resolved if it was standing from an
    earlier one. The coverage alert is evaluated only when the import got as far as
    resolving winners (`lost_channel_ids` is not None): a refused or failed refresh changed
    no channel's guide, so it can neither raise nor clear it."""
    alert_type = _epg_degradation_alert_type(reason)
    now = datetime.utcnow()

    @retry_on_locked()
    def _record_status_and_commit():
        source = db.session.get(EpgSource, source_id)
        if source is None:
            return None
        source.last_refresh_at = max(filter(None, (source.last_refresh_at, now)))
        source.last_status = _EPG_STATUS_BY_ALERT.get(alert_type, EPG_STATUS_OK)
        source.last_error = reason
        if alert_type is None:
            # When the import STARTED, i.e. read the users' keys: a key saved while it ran
            # was not in its channel map, and the channel page's "waiting for the next
            # refresh" compares against this (epg_sources._pending_keys).
            source.last_success_at = began_at or now
        db.session.commit()
        return source.name

    name = _record_status_and_commit()
    if name is None:
        return

    fetch_failed = alert_type == 'EPG_SOURCE_FETCH_FAILED'
    refused = alert_type == 'EPG_SOURCE_COLLAPSE_REFUSED'
    truncated = alert_type == 'EPG_SOURCE_IMPORT_TRUNCATED'
    _raise_or_resolve_standing_alert(
        'EPG_SOURCE_FETCH_FAILED', source=f'epg-source:{source_id}:fetch',
        active=fetch_failed, title=f'EPG source "{name}": fetch failed',
        body=(f'{reason} - the source\'s previous listings were kept.' if fetch_failed else ''))
    _raise_or_resolve_standing_alert(
        'EPG_SOURCE_COLLAPSE_REFUSED', source=f'epg-source:{source_id}:collapse',
        active=refused, title=f'EPG source "{name}": import refused',
        body=reason if refused else '')
    # Deliberately not folded into the fetch-failure alert: its body promises the previous
    # listings were kept, and on this path they were already deleted.
    _raise_or_resolve_standing_alert(
        'EPG_SOURCE_IMPORT_TRUNCATED', source=f'epg-source:{source_id}:truncated',
        active=truncated, title=f'EPG source "{name}": import was cut short',
        body=reason if truncated else '')

    if lost_channel_ids is None:
        return
    lost_body = ''
    if lost_channel_ids:
        sample = [n for (n,) in db.session.query(Channel.name).filter(
            Channel.id.in_(list(lost_channel_ids)[:_LOST_CHANNELS_NAMED]))
            .order_by(Channel.name)]
        more = len(lost_channel_ids) - len(sample)
        lost_body = (f'{len(lost_channel_ids):,} channel(s) that had a guide from EPG source '
                     f'"{name}" have none from any source now: {", ".join(sample)}'
                     + (f', and {more:,} more' if more > 0 else '') + '.')
    _raise_or_resolve_standing_alert(
        'EPG_SOURCE_COVERAGE_LOST', source=f'epg-source:{source_id}:coverage',
        active=bool(lost_channel_ids),
        title=f'EPG source "{name}": {len(lost_channel_ids):,} channel(s) lost their guide',
        body=lost_body)


#: How many channel names EPG_SOURCE_COVERAGE_LOST spells out before "and N more".
_LOST_CHANNELS_NAMED = 10
_EPG_STATUS_BY_ALERT = {
    'EPG_SOURCE_FETCH_FAILED': EPG_STATUS_FAILED,
    'EPG_SOURCE_COLLAPSE_REFUSED': EPG_STATUS_REFUSED,
    'EPG_SOURCE_IMPORT_TRUNCATED': EPG_STATUS_TRUNCATED,
}


def cleanup_old_epg_entries(app):
    """Delete EPG entries, active and alternate, older than sync.epg_keep_days. 0 = keep
    forever."""
    with app.app_context():
        cfg = load_config()
        keep_days = cfg.get('sync', {}).get('epg_keep_days', 1)
        if keep_days <= 0:
            return
        cutoff = datetime.utcnow() - timedelta(days=keep_days)

        @retry_on_locked()
        def _delete_and_commit():
            n = EPGEntry.query.filter(EPGEntry.stop_time < cutoff).delete(synchronize_session=False)
            # Same cutoff for the alternates (DESIGN-epg-sources.md §4), so the comparison
            # view never shows a source's past that the guide itself has already dropped.
            n_alt = EpgAlternateEntry.query.filter(
                EpgAlternateEntry.stop_time < cutoff).delete(synchronize_session=False)
            if n or n_alt:
                # The delete is global, so any stored count may now be stale. Recompute in
                # the same transaction (COUNT sees the pending delete) so the stored value
                # doesn't drift until the next full sync. Only a handful of accounts and
                # sources, once daily - not a per-row hot loop.
                for account in Account.query.all():
                    account.epg_entry_count = EPGEntry.query.join(Channel).filter(
                        Channel.account_id == account.id
                    ).count()
                for (source_id,) in db.session.query(EpgSource.id).all():
                    refresh_source_counts(source_id)
            db.session.commit()
            return n

        deleted = _delete_and_commit()
        if deleted:
            log.info('EPG cleanup: deleted %d entries older than %d day(s)', deleted, keep_days)
            # chan_prog holds what was in the *future* when it was last built, and this
            # delete takes rows out from under exactly those that have since aged into the
            # past. Rebuilding only the program index - channels are untouched here, and a
            # channel rebuild is several seconds of held write lock for nothing.
            from .search_index import rebuild_search_indexes, SEARCH_INDEX_PROGRAMS
            rebuild_search_indexes('EPG cleanup', names=(SEARCH_INDEX_PROGRAMS,))
