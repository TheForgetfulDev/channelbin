"""Xtream API client + debug/dump tooling.

Everything here is specific to account_type == 'xtream' accounts that talk to
the Xtream Codes API (player_api.php / get.php / xmltv.php). Generic account
sync orchestration (shared by M3U and Xtream accounts) lives in app/accounts.py.
"""
import json
import os
import re
import logging
from datetime import datetime

import requests

from .config import load_config, resolve_app_path, ensure_private_dir, DEFAULT_XTREAM_DUMP_DIR
from . import db
from .accounts import _request_headers, _parse_m3u_as_streams

log = logging.getLogger(__name__)


# ── Live-vs-VOD classification (DESIGN-live-vod.md) ──────────────────────────

# The provider's own get_live_streams response is the authority for which streams are
# live; we never re-derive that from URL shape. See DESIGN-live-vod.md §2 for the measured
# reason (one real account mixes /live/ and rootless URLs in a single playlist, so no
# URL-shape rule can classify both real accounts correctly).

CLASSIFY_APPLIED = 'applied'
CLASSIFY_UNAVAILABLE = 'unavailable'
CLASSIFY_REFUSED = 'refused'


def _live_keys_from_json(rows: list) -> tuple[set, set]:
    """(live stream_ids as str, live direct_source URLs) from a get_live_streams payload."""
    ids = set()
    direct = set()
    for row in rows:
        if not isinstance(row, dict):
            continue
        sid = row.get('stream_id')
        if sid is not None and str(sid):
            ids.add(str(sid))
        ds = row.get('direct_source')
        if ds:
            direct.add(ds)
    return ids, direct


def _tag_api_sourced(rows: list) -> list:
    """Mark rows that came straight from the live catalog, so classification skips them."""
    for row in rows:
        if isinstance(row, dict):
            row['_id_source'] = 'api'
    return rows


def _entry_is_live(entry: dict, live_ids: set, live_direct: set) -> bool:
    """Match one parsed playlist entry against the authoritative live set.

    Precedence (DESIGN-live-vod.md §3.1): stream_id when it came from the provider (a CUID
    attribute or a real numeric URL tail), else exact direct_source URL equality.

    A 'hash' id is a value we synthesized for a URL with no derivable id - accepting it
    could match a real live stream_id by coincidence and admit a VOD row, so it is never a
    valid key. Such entries can still match via direct_source, which is how CDN-fronted
    live channels (1,176 of them on one real account) are kept.

    '_id_source' == 'api' means the entry came from the live catalog itself rather than
    from a playlist, so it is live by construction. Without this, a sync that fell back to
    the JSON API would filter the catalog against itself, match nothing (API rows carry no
    playlist-derived id) and drop every channel.
    """
    if entry.get('_id_source') == 'api':
        return True
    if entry.get('_id_source') in ('cuid', 'url'):
        if str(entry.get('stream_id')) in live_ids:
            return True
    return entry.get('_stream_url') in live_direct


def classify_live_streams(entries: list, live_keys, channel_count_baseline: int,
                          threshold_percent: int) -> tuple[list, str, dict]:
    """Filter parsed playlist entries down to the provider's declared live set.

    Returns (kept_entries, outcome, stats) where outcome is one of CLASSIFY_APPLIED /
    CLASSIFY_UNAVAILABLE / CLASSIFY_REFUSED. Every non-applied outcome returns the entries
    UNFILTERED and is reported by the caller as a visible degradation - the app imports more
    than it would like and says so, rather than silently shipping a decimated channel list
    (DESIGN-live-vod.md §4).

    channel_count_baseline is the account's prior channel_count, NOT a share of the
    playlist: a legitimate m3u_plus playlist can be overwhelmingly VOD, so a
    percent-of-playlist guard would refuse correct classifications (§4.1).
    """
    stats = {'parsed': len(entries), 'kept': len(entries), 'dropped': 0}
    if live_keys is None:
        return entries, CLASSIFY_UNAVAILABLE, stats

    live_ids, live_direct = live_keys
    kept = [e for e in entries if _entry_is_live(e, live_ids, live_direct)]
    stats['kept'] = len(kept)
    stats['dropped'] = len(entries) - len(kept)

    # A classification that keeps nothing out of a non-empty playlist is never right - it
    # means the catalog and the playlist could not be correlated at all (a stale dump, an
    # unfamiliar id scheme), not that the provider has no live channels. Refused
    # unconditionally, because the percentage guard below cannot catch it on a first-ever
    # sync where there is no baseline to be a fraction of.
    floor = 0.0
    if threshold_percent and channel_count_baseline:
        floor = channel_count_baseline * threshold_percent / 100.0
    if entries and (not kept or len(kept) < floor):
        stats['kept'] = len(entries)
        stats['dropped'] = 0
        stats['refused_kept'] = len(kept)
        stats['floor'] = floor
        return entries, CLASSIFY_REFUSED, stats

    return kept, CLASSIFY_APPLIED, stats


# ── Xtream API client ─────────────────────────────────────────────────────────

class XtreamClient:
    def __init__(self, base_url: str, username: str, password: str, timeout: int = 30,
                 cfg: dict | None = None):
        self.base_url = base_url.rstrip('/')
        self.username = username
        self.password = password
        self.timeout = timeout
        self._base_params = {'username': username, 'password': password}
        self._headers = _request_headers(cfg)
        # The live catalog, kept for the lifetime of this client so one sync never asks the
        # provider the same question twice. Every provider seen so far allows exactly ONE
        # connection at a time, so a redundant round trip is not merely wasteful - it is one
        # more chance to be refused.
        #
        # `_catalog_attempted` is separate from the value on purpose: a FAILED attempt must
        # be remembered too. Caching only successes would leave the failure paths re-asking
        # a provider that just refused us - the exact case where a second knock is least
        # welcome and least likely to be answered.
        self._catalog_cache: list | None = None
        self._catalog_attempted: bool = False
        self._catalog_m3u_text: str | None = None

    def _api_url(self) -> str:
        return f'{self.base_url}/player_api.php'

    def _fetch_live_catalog(self) -> list | None:
        """Fetch player_api's live catalog once per client, then serve it from memory.

        Returns the raw rows, or None if the provider could not answer with a usable JSON
        catalog. Both get_live_streams() (as its no-playlist fallback) and
        get_live_stream_ids() (for classification) go through here, which is what stops the
        two of them issuing back-to-back identical requests.
        """
        if self._catalog_attempted:
            return self._catalog_cache
        self._catalog_attempted = True
        try:
            resp = self._fetch(self._api_url(),
                               dict(self._base_params, action='get_live_streams'))
        except Exception as exc:
            log.warning('Live catalog unavailable (fetch failed): %s', exc)
            return None
        text = resp.text.strip()
        if text.startswith('#EXTM3U'):
            # A server answering the JSON endpoint with a playlist has no separate live
            # catalog to consult, so it cannot act as a classification authority.
            log.info('Live catalog endpoint returned M3U (%d bytes), not JSON', len(resp.content))
            self._catalog_m3u_text = text
            return None
        try:
            rows = resp.json()
        except Exception:
            log.warning('Live catalog unavailable: non-JSON response (%s)',
                        text[:120].replace('\n', ' '))
            return None
        if not isinstance(rows, list):
            log.warning('Live catalog unavailable: unexpected payload type %s', type(rows).__name__)
            return None
        self._catalog_cache = rows
        return rows

    def _fetch(self, url: str, params: dict) -> requests.Response:
        resp = requests.get(url, params=params, timeout=self.timeout, headers=self._headers)
        resp.raise_for_status()
        if not resp.text.strip():
            raise ValueError(
                f'Server returned HTTP {resp.status_code} with empty body. '
                'Check that the base URL is correct (including port if needed, '
                'e.g. http://server.com:8080) and that credentials are valid.'
            )
        return resp

    def check_auth(self) -> dict:
        resp = self._fetch(self._api_url(), self._base_params)
        text = resp.text.strip()
        if text.startswith('#EXTM3U'):
            # Non-standard server: returns M3U for the auth endpoint - treat as auth success
            log.info('Server returned M3U for auth endpoint (non-standard but accepted as auth OK)')
            return {'user_info': {'auth': 1}}
        try:
            data = resp.json()
        except Exception:
            preview = text[:300].replace('\n', ' ')
            raise ValueError(
                f'Server returned non-JSON, non-M3U response (HTTP {resp.status_code}). '
                f'First 300 chars: {preview!r}. '
                'The base URL may be wrong or this is not an Xtream API server.'
            )
        if not data.get('user_info', {}).get('auth'):
            raise ValueError('Authentication failed - check username and password')
        return data

    def get_live_streams(self) -> list:
        """Fetch the playlist (real stream URLs). Live-vs-VOD classification is separate.

        Kept URL-bearing on purpose: the /get.php playlist is the only source that always
        carries the actual stream URL, which for a CDN-fronted provider cannot be
        reconstructed from account credentials. Which of those entries are LIVE is answered
        by get_live_stream_ids() instead - see DESIGN-live-vod.md §3.
        """
        # Primary: M3U from /get.php - the only source that always has stream URLs.
        try:
            streams = self._get_live_streams_from_m3u()
            if streams:
                return streams
            log.warning('M3U from /get.php returned 0 streams - falling back to JSON API')
        except Exception as exc:
            log.warning('M3U fetch failed (%s) - falling back to JSON API', exc)

        # Fallback: the live catalog. Stream URLs are absent and will be constructed from
        # account credentials in _upsert_channels, which the sync reports as a degradation
        # (DESIGN-live-vod.md §4.3). Goes through the cache so the classification step
        # below does not re-ask for the identical payload.
        rows = self._fetch_live_catalog()
        if rows is not None:
            log.info('Live catalog returned %d live streams (no URLs)', len(rows))
            return _tag_api_sourced(rows)
        # A server that answers player_api with a playlist: parse what it gave us.
        m3u_text = getattr(self, '_catalog_m3u_text', None)
        if m3u_text:
            streams = _parse_m3u_as_streams(m3u_text)
            log.info('Parsed %d streams from the playlist that player_api returned', len(streams))
            return streams
        return []

    def get_live_stream_ids(self) -> tuple[set, set] | None:
        """The provider's own declaration of which streams are live (the authority).

        Returns (live_stream_ids, live_direct_source_urls), or None when the API could not
        answer - None means "unclassifiable", which the caller must treat as a visible
        degradation, never as an empty live set (that would wipe the channel list).

        Served from the per-client cache, so calling this after get_live_streams() has
        already fallen back to the catalog costs no extra request.
        """
        rows = self._fetch_live_catalog()
        if not rows:
            return None
        return _live_keys_from_json(rows)

    def _get_live_streams_from_m3u(self) -> list:
        """Fetch the full m3u_plus playlist from /get.php."""
        url = f'{self.base_url}/get.php'
        params = dict(self._base_params, type='m3u_plus', output='ts')
        try:
            resp = self._fetch(url, params)
        except Exception as exc:
            raise ValueError(f'Could not fetch M3U from /get.php: {exc}') from exc
        text = resp.content.decode('utf-8', errors='replace').strip()
        log.info('/get.php returned %d bytes', len(resp.content))
        if not text.startswith('#EXTM3U'):
            raise ValueError(f'Expected M3U from /get.php, got: {text[:100]!r}')
        streams = _parse_m3u_as_streams(text)
        log.info('Parsed %d playlist entries from /get.php M3U', len(streams))
        return streams

    def get_live_categories(self) -> dict:
        """Return {category_id_str: category_name} mapping for live TV."""
        params = dict(self._base_params, action='get_live_categories')
        try:
            resp = self._fetch(self._api_url(), params)
        except Exception as exc:
            log.warning('get_live_categories failed: %s', exc)
            return {}
        text = resp.text.strip()
        if text.startswith('#EXTM3U'):
            return {}
        try:
            cats = resp.json()
            return {str(c.get('category_id', '')): c.get('category_name', '') for c in cats}
        except Exception:
            return {}

    def get_xmltv(self) -> bytes:
        url = f'{self.base_url}/xmltv.php'
        resp = requests.get(url, params=self._base_params, timeout=self.timeout,
                            stream=True, headers=self._headers)
        resp.raise_for_status()
        return resp.content


# ── Dump helpers ─────────────────────────────────────────────────────────────

def _write_json_file(path: str, data) -> None:
    with open(path, 'w', encoding='utf-8') as f:
        json.dump(data, f, indent=2, ensure_ascii=False, default=str)


def _dump_base(cfg: dict) -> str:
    """Resolve the configured (or default) dump base, same shape as the two instance/
    backup dirs in app/config.py - a relative path is anchored to the app root, never the
    process CWD (resolve_app_path()); an absolute override passes through unchanged."""
    return resolve_app_path(cfg.get('debug', {}).get('xtream_dump_dir') or DEFAULT_XTREAM_DUMP_DIR)


def _make_new_dump_dir(cfg: dict, account_id: int) -> str:
    """Create and return a new versioned dump directory: {base}/{id}/{yyyy-mm-dd_NN}/"""
    # Dumped playlists embed the account's plaintext username/password in every stream
    # URL, so the account dir is private from birth - same treatment as the config/DB
    # backup dirs (DESIGN-secrets.md §5). ensure_private_dir() only chmods a dir it just
    # created, so an existing dir's permissions are left alone.
    account_dir = ensure_private_dir(os.path.join(_dump_base(cfg), str(account_id)))
    today = datetime.utcnow().strftime('%Y-%m-%d')
    n = 1
    existing = [
        d for d in os.listdir(account_dir)
        if d.startswith(today + '_') and os.path.isdir(os.path.join(account_dir, d))
    ]
    if existing:
        nums = [int(d.split('_')[-1]) for d in existing if d.split('_')[-1].isdigit()]
        if nums:
            n = max(nums) + 1
    dump_dir = os.path.join(account_dir, f'{today}_{n:02d}')
    os.makedirs(dump_dir, exist_ok=True)
    return dump_dir


def _get_latest_dump_dir(cfg: dict, account_id: int) -> str:
    """Return the path to the most recently created dump directory for an account."""
    account_dir = os.path.join(_dump_base(cfg), str(account_id))
    if not os.path.isdir(account_dir):
        raise FileNotFoundError(
            f'No dump found for account {account_id} at {account_dir}. '
            'Run "Fetch & Dump" first.'
        )
    subdirs = sorted([
        d for d in os.listdir(account_dir)
        if os.path.isdir(os.path.join(account_dir, d))
    ])
    if not subdirs:
        raise FileNotFoundError(f'No dump directories found in {account_dir}')
    return os.path.join(account_dir, subdirs[-1])


class FileXtreamClient:
    """Reads Xtream API responses from dump files. Same interface as XtreamClient."""

    def __init__(self, dump_dir: str, base_url: str, username: str, password: str):
        self.dump_dir = dump_dir
        self.base_url = base_url.rstrip('/')
        self.username = username
        self.password = password

    def check_auth(self) -> dict:
        with open(os.path.join(self.dump_dir, 'auth.json'), encoding='utf-8') as f:
            return json.load(f)

    def get_live_streams(self) -> list:
        # The raw playlist is preferred over the pre-parsed JSON beside it: re-parsing with
        # the CURRENT parser keeps an old dump usable after a parser change, where the
        # pre-parsed file is frozen at whatever the parser emitted the day it was written
        # (dumps taken before 2026-07-22 carry no `_id_source` at all).
        raw = os.path.join(self.dump_dir, 'live_streams_m3u.m3u')
        if os.path.exists(raw):
            log.info('FileXtreamClient: parsing streams from live_streams_m3u.m3u')
            with open(raw, encoding='utf-8', errors='replace') as f:
                return _parse_m3u_as_streams(f.read().strip())
        for name in ('live_streams_m3u_parsed.json', 'live_streams_json_api.json'):
            p = os.path.join(self.dump_dir, name)
            if os.path.exists(p):
                log.info('FileXtreamClient: loading streams from %s', name)
                with open(p, encoding='utf-8') as f:
                    rows = json.load(f)
                if name == 'live_streams_json_api.json':
                    return _tag_api_sourced(rows)
                return rows
        raise FileNotFoundError(f'No stream dump files found in {self.dump_dir}')

    def get_live_stream_ids(self) -> tuple[set, set] | None:
        p = os.path.join(self.dump_dir, 'live_streams_json_api.json')
        if not os.path.exists(p):
            return None
        with open(p, encoding='utf-8') as f:
            rows = json.load(f)
        if not isinstance(rows, list) or not rows:
            return None
        return _live_keys_from_json(rows)

    def get_live_categories(self) -> dict:
        p = os.path.join(self.dump_dir, 'live_categories.json')
        if not os.path.exists(p):
            return {}
        with open(p, encoding='utf-8') as f:
            cats = json.load(f)
        return {str(c.get('category_id', '')): c.get('category_name', '') for c in cats}


def dump_xtream_account(app, account_id: int) -> str:
    """Fetch all Xtream API responses and write to files for troubleshooting.

    Does NOT write to the database. Returns the dump directory path.
    Each API call is attempted independently so a failure in one step
    doesn't abort the rest.
    """
    with app.app_context():
        from .database import XtreamAccount
        cfg = load_config()
        account = db.session.get(XtreamAccount, account_id)
        if account is None or (account.account_type or 'm3u') != 'xtream':
            raise ValueError('Account not found or not an Xtream account')

        dump_dir = _make_new_dump_dir(cfg, account_id)
        log.info('Dumping Xtream API data for account %d to %s', account_id, dump_dir)

        timeout = cfg.get('sync', {}).get('request_timeout_seconds', 30)
        client = XtreamClient(account.base_url, account.username, account.password,
                              timeout=timeout, cfg=cfg)
        meta: dict = {
            'account_id': account_id,
            'account_name': account.name,
            'base_url': account.base_url,
            'timestamp': datetime.utcnow().isoformat(),
        }

        # 1. Auth
        try:
            auth_data = client.check_auth()
            _write_json_file(os.path.join(dump_dir, 'auth.json'), auth_data)
            meta['auth'] = 'ok'
        except Exception as exc:
            meta['auth'] = f'error: {exc}'
            log.warning('Dump: auth failed for account %d: %s', account_id, exc)

        # 2. Live categories (raw API list)
        try:
            resp = client._fetch(client._api_url(), dict(client._base_params, action='get_live_categories'))
            text = resp.text.strip()
            if not text.startswith('#EXTM3U'):
                cats_raw = resp.json()
                _write_json_file(os.path.join(dump_dir, 'live_categories.json'), cats_raw)
                meta['categories'] = len(cats_raw)
            else:
                meta['categories'] = 'server returned M3U (no categories)'
        except Exception as exc:
            meta['categories'] = f'error: {exc}'
            log.warning('Dump: categories failed for account %d: %s', account_id, exc)

        # 3. Live streams - JSON API endpoint
        try:
            resp = client._fetch(client._api_url(), dict(client._base_params, action='get_live_streams'))
            text = resp.text.strip()
            if text.startswith('#EXTM3U'):
                with open(os.path.join(dump_dir, 'live_streams_json_api.m3u'), 'w', encoding='utf-8') as f:
                    f.write(text)
                meta['streams_json_api'] = f'server returned M3U ({len(resp.content)} bytes)'
            else:
                json_streams = resp.json()
                _write_json_file(os.path.join(dump_dir, 'live_streams_json_api.json'), json_streams)
                meta['streams_json_api'] = len(json_streams)
        except Exception as exc:
            meta['streams_json_api'] = f'error: {exc}'
            log.warning('Dump: JSON stream list failed for account %d: %s', account_id, exc)

        # 4. Live streams - full M3U from /get.php
        try:
            m3u_resp = client._fetch(
                f'{client.base_url}/get.php',
                dict(client._base_params, type='m3u_plus', output='ts'),
            )
            m3u_text = m3u_resp.content.decode('utf-8', errors='replace')
            with open(os.path.join(dump_dir, 'live_streams_m3u.m3u'), 'w', encoding='utf-8') as f:
                f.write(m3u_text)
            parsed = _parse_m3u_as_streams(m3u_text.strip())
            _write_json_file(os.path.join(dump_dir, 'live_streams_m3u_parsed.json'), parsed)
            meta['streams_m3u'] = len(parsed)
        except Exception as exc:
            meta['streams_m3u'] = f'error: {exc}'
            log.warning('Dump: M3U stream list failed for account %d: %s', account_id, exc)

        # 5. XMLTV EPG
        try:
            epg_url = f'{client.base_url}/xmltv.php'
            epg_resp = requests.get(epg_url, params=client._base_params, timeout=timeout,
                                    stream=True, headers=client._headers)
            epg_resp.raise_for_status()
            xml_text = epg_resp.content.decode('utf-8', errors='replace')
            # Add line breaks between elements - guaranteed not single-line, no DOM load needed
            xml_pretty = re.sub(r'>\s*<', '>\n<', xml_text)
            with open(os.path.join(dump_dir, 'xmltv.xml'), 'w', encoding='utf-8') as f:
                f.write(xml_pretty)
            meta['xmltv_bytes'] = len(epg_resp.content)
        except Exception as exc:
            meta['xmltv'] = f'error: {exc}'
            log.warning('Dump: XMLTV fetch failed for account %d: %s', account_id, exc)

        _write_json_file(os.path.join(dump_dir, 'dump_meta.json'), meta)
        log.info('Dump complete for account %d: %s', account_id, meta)
        return dump_dir


# ── Channel sync ──────────────────────────────────────────────────────────────

def _fetch_and_classify_xtream_streams(account, client: XtreamClient, cfg: dict | None = None,
                                       stream_origin: str | None = None
                                       ) -> tuple[list, str, int]:
    """Fetch channels via Xtream JSON/M3U API and classify live-vs-VOD.

    Returns (streams, classification outcome, count of channels whose stream URL will have
    to be CONSTRUCTED rather than supplied by the provider). The latter two are surfaced as
    alerts by the caller (DESIGN-live-vod.md §4, §4.3).

    Deliberately does no database work: the caller hands `streams` to `_upsert_channels`
    inside its own retry_on_locked closure, so a lock retry re-runs the write without
    re-issuing these three provider API calls (dev/changelog/683).

    `stream_origin` is where the provider says its STREAMS live, derived by the caller from
    the auth response it already holds (`accounts.stream_origin_from_server_info`). It is a
    different endpoint from the account's base_url, which is where the API is fetched from,
    and it is what any constructed URL is built on (§4.3). None means the provider declared
    nothing usable and construction falls back to base_url.
    """
    categories: dict[str, str] = {}
    try:
        categories = client.get_live_categories()
        log.info('Fetched %d live categories for account %d', len(categories), account.id)
    except Exception as exc:
        log.warning('Could not fetch live categories for account %d: %s', account.id, exc)

    streams = client.get_live_streams()

    # The playlist supplies URLs; the provider's live catalog supplies classification.
    live_keys = client.get_live_stream_ids()
    if cfg is None:
        cfg = load_config()
    threshold = cfg.get('sync', {}).get('live_classify_collapse_threshold_percent', 20)
    streams, outcome, stats = classify_live_streams(
        streams, live_keys, account.channel_count or 0, threshold)

    # An entry with no provider-supplied URL gets one CONSTRUCTED from account credentials
    # in _upsert_channels. That is the documented Xtream form, but it is still our
    # construction rather than the provider's own answer, so it is counted and reported -
    # a provider whose playlist endpoint is unavailable would otherwise produce a
    # completely ordinary-looking SUCCESS while every URL in the account was a guess.
    constructed = sum(1 for s in streams if not s.get('_stream_url'))

    if outcome == CLASSIFY_APPLIED and constructed == len(streams) and streams:
        # Nothing was filtered - the entries ARE the catalog, so there was no playlist to
        # classify. Saying "kept N of N playlist entries" here would misdescribe the run
        # for whoever is debugging exactly this situation.
        log.info('Account %d live classification: not applicable - the provider catalog was '
                 'used directly as the channel list (%d live streams); no playlist to filter',
                 account.id, len(streams))
    elif outcome == CLASSIFY_APPLIED:
        log.info('Account %d live classification: kept %d of %d playlist entries '
                 '(%d excluded as not-live by the provider catalog)',
                 account.id, stats['kept'], stats['parsed'], stats['dropped'])
    elif outcome == CLASSIFY_REFUSED:
        log.warning('Account %d live classification REFUSED: it would have kept only %d '
                    'channel(s), below %.0f (%d%% of the prior count %d). Importing the '
                    'playlist unfiltered instead.',
                    account.id, stats['refused_kept'], stats['floor'], threshold,
                    account.channel_count or 0)
    else:
        log.warning('Account %d live classification UNAVAILABLE - the provider catalog could '
                    'not be read. Importing %d playlist entries unfiltered.',
                    account.id, stats['parsed'])

    if constructed:
        log.warning('Account %d: %d of %d stream URL(s) had to be CONSTRUCTED because the '
                    'provider supplied none (its playlist endpoint was unavailable). Built '
                    'on %s, and unverified against this provider.',
                    account.id, constructed, len(streams),
                    ('the stream origin the provider declares in server_info'
                     if stream_origin else
                     "this account's base_url - the provider declared no stream origin"))

    # Resolve category names for streams that only carry category_id
    for stream in streams:
        if not stream.get('category_name') and stream.get('category_id'):
            stream['category_name'] = categories.get(str(stream['category_id']), '')

    return streams, outcome, constructed
