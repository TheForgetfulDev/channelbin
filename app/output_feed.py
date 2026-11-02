"""The output feed: ChannelBin as an Xtream/M3U/XMLTV source an IPTV app adds as an account.

What the IPTV app sees is the TV Guide - every in-guide channel, and every in-guide group as ONE
channel - with the guide's own listings, and nothing else (dev/docs/DESIGN-output-feed.md).
This module is the model: the served set and its feed ids, the catalog the Xtream actions
answer with, the XMLTV writer, and the relay session that plays a channel through
ChannelBin in `proxy` mode. The blueprint is app/routes/output_feed.py.

The first optional module (design doc section 12): off by default, one switch, nothing
here runs, holds or writes while it is off. Rules this module enforces, each guarded in
tests/test_output_feed.py:

* The served set is the guide scope and the feed ids are ChannelBin's own: `2 * Channel.id`
  for a channel, `2 * ChannelGroup.id + 1` for a group - stable, decodable, never a
  provider's id. A group's member is chosen at play time by channel_groups.serving_member,
  the same ranking the recorder uses; the accounts allowlist and a blocked account are
  dropped before the format lock, exactly as a recording drops them.
* A relay session holds a provider seat under the 'viewer' holder kind, lowest in the
  recorder's preemption order, and yields it to a recording by name: the session ends
  REASON_PREEMPTED, a VIEWER_PREEMPTED alert names the recording, and the recording's own
  timeline gets a diagnostics event saying what it displaced (section 4.4).
* The relay is one ffmpeg stream-copying into a fixed-size in-memory ring - no disk, no
  per-client queue, memory a function of config and never of watch time. The reader thread
  is the one drain of ffmpeg's stdout, so a slow IPTV app can never stall the process; an IPTV app that
  falls behind by more than the ring is cut REASON_CLIENT_TOO_SLOW (section 4.2).
* The viewer plays `reserve_seconds` behind live: each chunk is released that long after it
  arrived, so a stall or a failover shorter than the reserve plays through. What the
  provider never sent is still missing - the picture skips, it does not freeze.
* A member that dies is replaced inside the same response (section 4.5, decided by the
  capture of 2026-10-31: TiviMate played through a resolution-and-frame-rate switch with
  nothing visible), the event is named on the group's timeline, and a group with nothing
  left ends REASON_NO_MEMBER.
* Every end has a name (section 4.6) and every one is torn down the same way: ffmpeg
  killed, seat released, ring freed, stderr spool removed. Session state is process-local,
  like app/preview.py's: a restart kills what this process owned and there is nothing to
  recover. kill_all_viewers runs on shutdown beside preview.kill_all_previews.
* In `redirect` mode nothing flows through ChannelBin: a play is a 302 to the member's
  provider URL, re-picked on every tune-in, and ChannelBin never learns the play ended. The
  seats `held_seats` names are subtracted from the recorder's limit where a connection is
  taken (connection_limits._pool_taken_now), so it never plans on a seat the IPTV app may hold.

Nothing here feeds a health score or writes a row for a session: a viewer is not an
observation.
"""
import hmac
import logging
import os
import secrets
import subprocess
import tempfile
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Optional
from xml.sax.saxutils import escape as xml_escape

from . import connection_limits as connlim
from .fmt_utils import fmt_bytes, iso_or_none
from .proc_utils import build_relay_cmd, read_stderr_tail, terminate_or_kill
from .url_utils import mask_creds_in_text

log = logging.getLogger(__name__)

MODE_PROXY = 'proxy'
MODE_REDIRECT = 'redirect'
MODES = (MODE_PROXY, MODE_REDIRECT)

STATE_STARTING = 'STARTING'     # ffmpeg launched, nothing relayed yet
STATE_STREAMING = 'STREAMING'   # bytes have reached the ring
STATE_STOPPED = 'STOPPED'       # ended; `reason` says why
STATES = (STATE_STARTING, STATE_STREAMING, STATE_STOPPED)

# Why a session ended. Every terminal path names one, and REASON_TEXT is what the dashboard
# shows for it - a stream that stops says why, or it is a number nobody can explain
# (CLAUDE.md principle 1). FAILED_OVER is the one non-terminal entry: it is recorded in the
# session's history while the session goes on.
REASON_CLIENT_GONE = 'client_gone'
REASON_CLIENT_TOO_SLOW = 'client_too_slow'
REASON_BUFFER_TOO_SMALL = 'buffer_too_small'
REASON_IDLE = 'idle'
REASON_MAX_DURATION = 'max_duration'
REASON_PREEMPTED = 'preempted'
REASON_FFMPEG_EXITED = 'ffmpeg_exited'
REASON_CONNECT_TIMEOUT = 'connect_timeout'
REASON_LAUNCH_FAILED = 'launch_failed'
REASON_FAILED_OVER = 'failed_over'
REASON_NO_MEMBER = 'no_member'
REASON_SHUTDOWN = 'shutdown'
REASON_DISABLED = 'disabled'

REASON_TEXT = {
    REASON_CLIENT_GONE: 'The IPTV app stopped watching.',
    REASON_CLIENT_TOO_SLOW: 'Cut - the IPTV app fell further behind than the buffer holds.',
    REASON_BUFFER_TOO_SMALL: ('Cut - the stream arrived faster than the buffer can hold for this '
                              'reserve. Lower reserve_seconds or raise buffer_mb in Settings.'),
    REASON_IDLE: 'Cut - the IPTV app read nothing for a while.',
    REASON_MAX_DURATION: 'Cut - a session runs for a limited time. The IPTV app can tune in again.',
    REASON_PREEMPTED: 'Cut - a recording needed this connection.',
    REASON_FFMPEG_EXITED: 'The stream ended or dropped.',
    REASON_CONNECT_TIMEOUT: 'Could not connect - the stream sent nothing in time.',
    REASON_LAUNCH_FAILED: 'Could not start.',
    REASON_FAILED_OVER: 'Moved to another channel in the group.',
    REASON_NO_MEMBER: 'Nothing left to play - every channel in the group failed or is unavailable.',
    REASON_SHUTDOWN: 'Cut - the app is restarting.',
    REASON_DISABLED: 'Cut - the output feed was turned off.',
}

#: Reads from ffmpeg's stdout are this big; the ring holds whole chunks.
READ_CHUNK = 64 * 1024
#: How long a session may take to its first byte before it ends REASON_CONNECT_TIMEOUT -
#: the same window the preview gives a channel (preview.connect_timeout_seconds' default).
CONNECT_TIMEOUT_SECONDS = 20.0
#: Bytes per second of reserve the settings validation assumes, a typical HD stream at
#: 8 Mbps (design doc section 4.3). The ring must hold the reserve with room to spare.
RESERVE_BYTES_PER_SECOND = 1_000_000
#: How much of the ring the reserve may fill before the validation refuses it.
RESERVE_RING_SHARE = 0.75
#: How many ended sessions and redirects are kept for the dashboard and the bundle.
_KEEP_ENDED = 20
_REAP_INTERVAL = 0.5

#: The one-line body every refused credential gets, whatever the cause (section 6.1).
AUTH_FAILED_MESSAGE = 'Invalid or missing credentials'
UNCATEGORIZED = 'Uncategorized'


# ── Config ─────────────────────────────────────────────────────────────────────────────────

def feed_config(cfg=None) -> dict:
    """The `integrations.output_feed` block, merged over the defaults."""
    if cfg is None:
        from .config import load_config
        cfg = load_config()
    return (cfg.get('integrations') or {}).get('output_feed') or {}


def is_enabled(fcfg: dict) -> bool:
    return bool(fcfg.get('enabled'))


def _cred(value) -> str:
    """A stored credential as text. A hand-edited config.yaml with `username: 1` parses it
    as an int, and a short login is exactly what the help text invites."""
    return '' if value is None else str(value).strip()


def feed_username(fcfg: dict) -> str:
    return _cred(fcfg.get('username'))


def feed_password(fcfg: dict) -> str:
    return '' if fcfg.get('password') is None else str(fcfg.get('password'))


def credentials_set(fcfg: dict) -> bool:
    return bool(feed_username(fcfg)) and bool(feed_password(fcfg))


def credentials_ok(fcfg: dict, username, password) -> bool:
    """Enabled, a credential is stored, and the pair matches - each checked, none inferred
    from another (the shape app/routes/ha.py::_ha_key_ok sets). Constant-time on the pair:
    the client sends it on every request, and a hash check measured 147 ms per call
    (design doc section 6.2)."""
    if not is_enabled(fcfg) or not credentials_set(fcfg):
        return False
    if not isinstance(username, str) or not isinstance(password, str):
        return False
    want = f'{feed_username(fcfg)}\0{feed_password(fcfg)}'.encode()
    got = f'{username}\0{password}'.encode()
    return hmac.compare_digest(want, got)


def mode_of(fcfg: dict) -> str:
    mode = fcfg.get('mode') or MODE_PROXY
    return mode if mode in MODES else MODE_PROXY


def allowed_account_ids(fcfg: dict) -> Optional[frozenset]:
    """The accounts the feed may serve from, or None for all (section 5.3)."""
    ids = fcfg.get('accounts') or []
    out = set()
    for raw in ids:
        try:
            out.add(int(raw))
        except (TypeError, ValueError):
            continue
    return frozenset(out) or None


def held_seats_map(fcfg: dict) -> dict:
    """{account_id: seats} from the config's `held_seats`, tolerant of the string keys a
    YAML or JSON round trip leaves behind."""
    out = {}
    for raw_id, raw_n in (fcfg.get('held_seats') or {}).items():
        try:
            account_id, n = int(raw_id), int(raw_n)
        except (TypeError, ValueError):
            continue
        if n > 0:
            out[account_id] = n
    return out


def held_seats_now(account_ids, cfg=None) -> dict:
    """{account_id: seats the IPTV app may be using} for `account_ids`, non-empty only while the
    feed is on in redirect mode - the one reader connection_limits subtracts from. `cfg` is
    the caller's already-loaded config; a caller in a loop hands it in."""
    fcfg = feed_config(cfg)
    if not is_enabled(fcfg) or mode_of(fcfg) != MODE_REDIRECT:
        return {}
    held = held_seats_map(fcfg)
    wanted = {a for a in account_ids if a is not None}
    return {a: n for a, n in held.items() if a in wanted}


def reserve_fits(fcfg: dict) -> bool:
    """Whether `buffer_mb` can hold `reserve_seconds` at RESERVE_BYTES_PER_SECOND with
    RESERVE_RING_SHARE to spare - the settings validation and the readiness check."""
    try:
        buffer_mb = float(fcfg.get('buffer_mb') or 0)
        reserve = float(fcfg.get('reserve_seconds') or 0)
    except (TypeError, ValueError):
        return False
    if buffer_mb <= 0:
        return False
    return reserve * RESERVE_BYTES_PER_SECOND <= buffer_mb * 1024 * 1024 * RESERVE_RING_SHARE


def max_reserve_seconds(buffer_mb) -> int:
    """The largest `reserve_seconds` `buffer_mb` holds, for the validation's message."""
    try:
        return int(float(buffer_mb) * 1024 * 1024 * RESERVE_RING_SHARE // RESERVE_BYTES_PER_SECOND)
    except (TypeError, ValueError):
        return 0


# ── Feed ids ───────────────────────────────────────────────────────────────────────────────

KIND_CHANNEL = 'channel'
KIND_GROUP = 'group'


def feed_id_for_channel(channel_id: int) -> int:
    return 2 * channel_id


def feed_id_for_group(group_id: int) -> int:
    return 2 * group_id + 1


def decode_feed_id(feed_id: int):
    """(kind, row id) for a feed id - no table needed, which is the point (section 3.3)."""
    if feed_id % 2:
        return KIND_GROUP, (feed_id - 1) // 2
    return KIND_CHANNEL, feed_id // 2


# ── The catalog ────────────────────────────────────────────────────────────────────────────

@dataclass
class FeedRow:
    """One channel as the IPTV app sees it. `row` is the Channel or ChannelGroup behind it,
    `listings_channel_id` the channel whose EPG rows fill it, `logo_path` the app's own
    logo route or '' (never a provider URL, which may carry credentials)."""
    feed_id: int
    kind: str
    num: int
    name: str
    category: str
    logo_path: str
    row: object = field(repr=False)
    listings_channel_id: Optional[int]
    members: list = field(default_factory=list, repr=False)


def _members_for_feed(group, blocked, allowed):
    """A group's members the feed may play, in the order a recording drops them: blocked
    accounts first (account_blocks.split_blocked), then the allowlist. Returns (members,
    why none) - `why` names the reason when the list is empty."""
    from .account_blocks import split_blocked
    from .channel_groups import recording_members
    members = recording_members(group.memberships)
    if not members:
        return [], 'no channel in the group has Recording on'
    kept, _skipped = split_blocked(members, blocked)
    if not kept:
        return [], 'every channel in the group is on a blocked account'
    if allowed is not None:
        kept = [m for m in kept if m.account_id in allowed]
        if not kept:
            return [], ('every channel in the group is on an account the output feed is '
                        'not allowed to serve from')
    return kept, ''


def _logo_path(channel) -> str:
    from .logo_cache import resolve_logo_url
    url = resolve_logo_url(channel) if channel is not None else ''
    return url if url.startswith('/') else ''


def _group_lead(group, members, latest_by_channel, blocked):
    """The member whose category and logo a group's feed row carries: the pinned listings
    member, else the member the group would play now, else its first member."""
    from .channel_groups import guide_listings_member, serving_member
    pinned = guide_listings_member(group)
    if pinned is not None:
        return pinned
    if members:
        serving = serving_member(group, latest_by_channel, blocked_account_ids=blocked,
                                 members=members).member
        if serving is not None:
            return serving
    return next((m.channel for m in group.memberships), None)


def catalog(cfg=None) -> list:
    """Every feed row in the guide's order: the in-guide channels and the in-guide groups,
    interleaved by their shared guide_sort_order (channel_groups.guide_row_sort_key). A
    fixed number of queries whatever the guide holds: the channels, the groups with their
    members and accounts, the members' latest health checks, the blocked accounts.

    A channel on an account the allowlist excludes is not served; a group is served even
    when nothing in it is playable right now - the play is what refuses, by name
    (FEED_NO_MEMBER), so the IPTV app's list does not silently shrink. A group's members are
    never listed individually: they are its failover set."""
    from .channel_groups import (blocked_account_ids_now, guide_listings_member,
                                 guide_row_sort_key, member_eager_options)
    from .database import Channel, ChannelGroup
    from .routes.channel_tests import _latest_tests_by_channel

    fcfg = feed_config(cfg)
    allowed = allowed_account_ids(fcfg)
    blocked = blocked_account_ids_now()
    channels = Channel.query.filter(Channel.in_guide.is_(True)).all()  # guide-flag-read-ok: the feed's own-row half; the grouped half is the groups below
    groups = (ChannelGroup.query.filter(ChannelGroup.in_guide.is_(True))
              .options(*member_eager_options(with_accounts=True)).all())
    member_ids = {m.channel_id for g in groups for m in g.memberships}
    latest = _latest_tests_by_channel(list(member_ids)) if member_ids else {}

    entries = []
    for ch in channels:
        if allowed is not None and ch.account_id not in allowed:
            continue
        entries.append((guide_row_sort_key(ch), FeedRow(
            feed_id=feed_id_for_channel(ch.id), kind=KIND_CHANNEL, num=0, name=ch.name,
            category=(ch.category_name or '').strip() or UNCATEGORIZED,
            logo_path=_logo_path(ch), row=ch, listings_channel_id=ch.id)))
    for g in groups:
        members, _why = _members_for_feed(g, blocked, allowed)
        lead = _group_lead(g, members, latest, blocked)
        listings = guide_listings_member(g) or lead
        entries.append((guide_row_sort_key(g), FeedRow(
            feed_id=feed_id_for_group(g.id), kind=KIND_GROUP, num=0, name=g.name,
            category=((lead.category_name or '').strip() if lead else '') or UNCATEGORIZED,
            logo_path=_logo_path(lead), row=g,
            listings_channel_id=listings.id if listings else None, members=members)))
    entries.sort(key=lambda e: e[0])
    rows = [e[1] for e in entries]
    for n, row in enumerate(rows, start=1):
        row.num = n
    return rows


def categories(rows) -> list:
    """[(category_id, name)] over `rows`, deduplicated by name in first-seen order, so the
    same id answers get_live_categories and the streams' category_id from one catalog."""
    names = []
    for row in rows:
        if row.category not in names:
            names.append(row.category)
    return [(str(i), name) for i, name in enumerate(names, start=1)]


def category_payload(rows) -> list:
    return [{'category_id': cid, 'category_name': name, 'parent_id': 0}
            for cid, name in categories(rows)]


def live_streams_payload(rows, base_url: str) -> list:
    """get_live_streams: what ChannelBin's own XtreamClient reads plus what the measured
    client needs (section 3.4). `direct_source` is empty: the capture showed the client
    ignores it. `epg_channel_id` is the feed id's string, which is what xmltv.php names."""
    ids = {name: cid for cid, name in categories(rows)}
    out = []
    for row in rows:
        out.append({
            'num': row.num, 'name': row.name, 'stream_type': 'live', 'stream_id': row.feed_id,
            'stream_icon': f'{base_url}{row.logo_path}' if row.logo_path else '',
            'epg_channel_id': str(row.feed_id), 'added': '0', 'is_adult': '0',
            'category_id': ids[row.category], 'category_ids': [int(ids[row.category])],
            'custom_sid': '', 'tv_archive': 0, 'direct_source': '', 'tv_archive_duration': 0,
        })
    return out


def auth_payload(fcfg: dict, host: str, port: str, scheme: str) -> dict:
    """The bare player_api.php answer: what accounts.check_xtream_auth() needs (`auth`,
    `status`, `exp_date`) and a server_info naming the host the request arrived on. Never
    an account's base_url - the feed's stream endpoint is ChannelBin itself (section 3.5)."""
    now = datetime.utcnow()
    return {
        'user_info': {
            'username': feed_username(fcfg), 'password': feed_password(fcfg),
            'message': 'ChannelBin output feed', 'auth': 1, 'status': 'Active',
            'exp_date': str(int((now + timedelta(days=3650)).timestamp())),
            'is_trial': '0', 'active_cons': str(len(live_sessions())),
            'created_at': '0', 'max_connections': str(int(fcfg.get('max_viewers') or 1)),
            'allowed_output_formats': ['ts'],
        },
        'server_info': {
            'url': host, 'port': port, 'https_port': port if scheme == 'https' else '443',
            'server_protocol': scheme, 'rtmp_port': '', 'timezone': 'UTC',
            'timestamp_now': int(now.timestamp()), 'time_now': now.strftime('%Y-%m-%d %H:%M:%S'),
            'process': True,
        },
    }


def stream_url(base_url: str, fcfg: dict, feed_id: int) -> str:
    return f'{base_url}/live/{feed_username(fcfg)}/{feed_password(fcfg)}/{feed_id}.ts'


def m3u_text(rows, base_url: str, fcfg: dict) -> str:
    """get.php as m3u_plus: tvg-id is the feed id string, the URL is the feed's own stream
    URL in both modes - a redirect happens on play, so no provider URL is ever listed."""
    lines = ['#EXTM3U']
    for row in rows:
        logo = f'{base_url}{row.logo_path}' if row.logo_path else ''
        name = row.name.replace('\n', ' ').replace('"', "'")
        category = row.category.replace('\n', ' ').replace('"', "'")
        lines.append(f'#EXTINF:-1 tvg-id="{row.feed_id}" tvg-name="{name}" tvg-logo="{logo}" '
                     f'group-title="{category}",{name}')
        lines.append(stream_url(base_url, fcfg, row.feed_id))
    return '\n'.join(lines) + '\n'


def _xmltv_time(dt: datetime) -> str:
    # Storage is naive UTC; the wire carries the offset so no client guesses a timezone.
    return dt.strftime('%Y%m%d%H%M%S') + ' +0000'


def xmltv_lines(rows, cfg=None, now: Optional[datetime] = None):
    """xmltv.php, streamed as it is built: the served rows as <channel> elements and their
    listings as <programme> elements, from ONE query over EPGEntry for the listings
    channels in the configured window. A channel that fills several feed rows (itself and a
    group) has its programs written under each feed id. The element is spelled `programme`
    because it is the wire format's identifier; everything ChannelBin names is `program`."""
    from . import db
    from .database import EPGEntry

    fcfg = feed_config(cfg)
    now = now or datetime.utcnow()
    days = int(fcfg.get('xmltv_days') or 3)
    yield '<?xml version="1.0" encoding="UTF-8"?>\n'
    yield '<!DOCTYPE tv SYSTEM "xmltv.dtd">\n'
    yield '<tv generator-info-name="ChannelBin">\n'
    feed_ids_by_channel: dict = {}
    for row in rows:
        yield (f'  <channel id="{row.feed_id}"><display-name>{xml_escape(row.name)}'
               f'</display-name></channel>\n')
        if row.listings_channel_id is not None:
            feed_ids_by_channel.setdefault(row.listings_channel_id, []).append(row.feed_id)
    if feed_ids_by_channel:
        q = (db.session.query(EPGEntry.channel_id, EPGEntry.title, EPGEntry.sub_title,
                              EPGEntry.description, EPGEntry.category, EPGEntry.rating,
                              EPGEntry.start_time, EPGEntry.stop_time)
             .filter(EPGEntry.channel_id.in_(list(feed_ids_by_channel)),
                     EPGEntry.stop_time > now,
                     EPGEntry.start_time < now + timedelta(days=days))
             .order_by(EPGEntry.channel_id, EPGEntry.start_time))
        for (channel_id, title, sub_title, description, category, rating,
             start, stop) in q.yield_per(500):
            body = f'<title>{xml_escape(title or "")}</title>'
            if sub_title:
                body += f'<sub-title>{xml_escape(sub_title)}</sub-title>'
            if description:
                body += f'<desc>{xml_escape(description)}</desc>'
            if category:
                body += f'<category>{xml_escape(category)}</category>'
            if rating:
                body += f'<rating><value>{xml_escape(rating)}</value></rating>'
            for feed_id in feed_ids_by_channel[channel_id]:
                yield (f'  <programme start="{_xmltv_time(start)}" stop="{_xmltv_time(stop)}" '
                       f'channel="{feed_id}">{body}</programme>\n')
    yield '</tv>\n'


# ── Picking what a feed id plays ───────────────────────────────────────────────────────────

class FeedRefused(Exception):
    """A play could not start; `message` is user-facing prose, `status` the HTTP code."""

    def __init__(self, message: str, status: int = 409):
        super().__init__(message)
        self.message = message
        self.status = status


@dataclass
class Play:
    """What one feed id resolves to right now: the member to play, and the group it came
    from when it is one. `why_none` names the reason when `member` is None."""
    feed_id: int
    label: str
    member: object = field(default=None, repr=False)
    group: object = field(default=None, repr=False)
    why_none: str = ''


def resolve_play(feed_id: int, cfg=None, exclude_ids=frozenset()) -> Optional[Play]:
    """The member `feed_id` plays right now, or None when the id names nothing served.
    A group is re-asked here on every tune-in and every failover (`exclude_ids` drops the
    members that already failed this play), so the IPTV app always gets the member a recording
    would start from (channel_groups.serving_member, the one spelling)."""
    from .channel_groups import blocked_account_ids_now, serving_member
    from .database import Channel, ChannelGroup
    from .routes.channel_tests import _latest_tests_by_channel
    from . import db

    fcfg = feed_config(cfg)
    allowed = allowed_account_ids(fcfg)
    kind, row_id = decode_feed_id(feed_id)
    if kind == KIND_CHANNEL:
        ch = db.session.get(Channel, row_id)
        if ch is None or not ch.in_guide:  # guide-flag-read-ok: a feed id for a channel is its own guide row and nothing else
            return None
        play = Play(feed_id, f'channel {ch.id} ({ch.name})')
        if allowed is not None and ch.account_id not in allowed:
            play.why_none = 'its account is one the output feed is not allowed to serve from'
            return play
        if ch.id in exclude_ids:
            play.why_none = 'it already failed this play'
            return play
        play.member = ch
        return play
    group = db.session.get(ChannelGroup, row_id)
    if group is None or not group.in_guide:
        return None
    play = Play(feed_id, f'group {group.id} ({group.name})', group=group)
    blocked = blocked_account_ids_now()
    members, why = _members_for_feed(group, blocked, allowed)
    if not members:
        play.why_none = why
        return play
    latest = _latest_tests_by_channel([m.id for m in members])
    choice = serving_member(group, latest, blocked_account_ids=blocked, members=members,
                            exclude_ids=exclude_ids)
    if choice.member is None:
        play.why_none = 'every channel in the group already failed this play'
        return play
    play.member = choice.member
    return play


# ── The relay ──────────────────────────────────────────────────────────────────────────────

class Ring:
    """A fixed-size queue of arrival-stamped chunks between the reader thread and the one
    HTTP response. The reader appends; when the total passes `capacity` the oldest chunks
    are dropped and `dropped` counts the bytes, which is how an IPTV app that stopped reading is
    noticed (REASON_CLIENT_TOO_SLOW) rather than queued for. `take()` hands the consumer
    the oldest chunk once it has aged `reserve` seconds, so the viewer plays that far behind
    live and a gap shorter than it plays through (section 4.3).

    `dropped_unripe` is the part of `dropped` that was evicted before it had aged the
    reserve - bytes the viewer could not have taken yet. Those say the ring is too small for
    the reserve at the stream's real bitrate (REASON_BUFFER_TOO_SMALL), not that the viewer
    is slow: the settings validation assumes RESERVE_BYTES_PER_SECOND, and a stream above it
    fills the ring before its head is ever releasable, so the viewer gets nothing at all."""

    def __init__(self, capacity: int, reserve: float):
        self.capacity = max(int(capacity), READ_CHUNK)
        self.reserve = max(float(reserve), 0.0)
        self._chunks: deque = deque()
        self.size = 0
        self.dropped = 0
        self.dropped_unripe = 0
        self.total_in = 0
        self._cv = threading.Condition()

    def append(self, data: bytes, now: Optional[float] = None) -> None:
        now = time.monotonic() if now is None else now
        with self._cv:
            self._chunks.append((now, data))
            self.size += len(data)
            self.total_in += len(data)
            while self.size > self.capacity and len(self._chunks) > 1:
                arrived, old = self._chunks.popleft()
                self.size -= len(old)
                self.dropped += len(old)
                if arrived + self.reserve > now:
                    self.dropped_unripe += len(old)
            self._cv.notify_all()

    def take(self, timeout: float, now: Optional[float] = None) -> Optional[bytes]:
        """The oldest chunk once it is `reserve` old, or None after `timeout` with
        nothing releasable."""
        deadline = (time.monotonic() if now is None else now) + timeout
        with self._cv:
            while True:
                now = time.monotonic()
                if self._chunks:
                    arrived, data = self._chunks[0]
                    if arrived + self.reserve <= now:
                        self._chunks.popleft()
                        self.size -= len(data)
                        return data
                    wait = min(arrived + self.reserve - now, deadline - now)
                else:
                    wait = deadline - now
                if wait <= 0:
                    return None
                self._cv.wait(wait)

    def untaken_age(self, now: Optional[float] = None) -> float:
        """How long the oldest chunk has been releasable and still not taken - the one
        measure of an IPTV app that has stopped reading. Zero while nothing is releasable, so a
        provider that stopped sending never reads as an idle IPTV app."""
        now = time.monotonic() if now is None else now
        with self._cv:
            if not self._chunks:
                return 0.0
            return max(0.0, now - (self._chunks[0][0] + self.reserve))

    def wake(self) -> None:
        with self._cv:
            self._cv.notify_all()

    def clear(self) -> None:
        with self._cv:
            self._chunks.clear()
            self.size = 0
            self._cv.notify_all()


@dataclass
class RelaySession:
    id: str
    feed_id: int
    label: str                       # what the IPTV app asked for: 'channel 5 (CNN)' or 'group 2 (...)'
    client: str                      # the IPTV app's address
    group_id: Optional[int]
    channel_id: int
    channel_name: str
    account_id: int
    ring: Ring = field(repr=False)
    idle_timeout: float = 30.0
    max_seconds: float = 12 * 3600
    stall_timeout: float = 30.0
    pace_realtime: bool = False
    proc: Optional[subprocess.Popen] = field(default=None, repr=False)
    stderr_path: str = ''
    reader: Optional[threading.Thread] = field(default=None, repr=False)
    state: str = STATE_STARTING
    reason: Optional[str] = None
    detail: str = ''
    started_mono: float = 0.0
    started_at: datetime = field(default_factory=datetime.utcnow)
    ended_at: Optional[datetime] = None
    launched_mono: float = 0.0       # when the current member's ffmpeg was started
    last_data_mono: Optional[float] = None
    last_read_mono: Optional[float] = None
    bytes_sent: int = 0
    # Members that died this play, so a failover never returns to one (section 4.5).
    failed_ids: set = field(default_factory=set)
    failovers: list = field(default_factory=list)    # [(from name, to name, why)]
    # Set by preempt_for_account() in the acquire-to-Popen window; the launch re-reads it
    # after Popen and never runs on a seat a recording already took.
    preempted: bool = False
    preempted_by: Optional[int] = None
    login_id: Optional[int] = None
    app: Optional[object] = field(default=None, repr=False)
    _finishing: bool = False
    _teardown_lock: threading.Lock = field(default_factory=threading.Lock, repr=False)
    _failing_over: bool = False

    @property
    def live(self) -> bool:
        return self.state != STATE_STOPPED

    def reason_text(self) -> str:
        return REASON_TEXT.get(self.reason, '') if self.reason else ''

    def to_dict(self) -> dict:
        now = time.monotonic()
        return {
            'session_id': self.id, 'feed_id': self.feed_id, 'label': self.label,
            'client': self.client, 'group_id': self.group_id, 'channel_id': self.channel_id,
            'channel_name': self.channel_name, 'account_id': self.account_id,
            'state': self.state, 'reason': self.reason, 'reason_text': self.reason_text(),
            'detail': self.detail, 'bytes_sent': self.bytes_sent,
            'elapsed_seconds': round(now - self.started_mono, 1) if self.started_mono else 0,
            'started_at': self.started_at, 'ended_at': self.ended_at,
            'failovers': list(self.failovers),
        }


_lock = threading.Lock()
_sessions: dict = {}          # session id -> RelaySession, live ones and the last few ended
_redirects: list = []         # the last _KEEP_ENDED redirect-mode plays, oldest first
_reaper: Optional[threading.Thread] = None


def live_sessions() -> list:
    with _lock:
        return [s for s in _sessions.values() if s.live]


def recent_ended() -> list:
    with _lock:
        ended = [s for s in _sessions.values() if not s.live]
    return sorted(ended, key=lambda s: s.ended_at or s.started_at, reverse=True)


def recent_redirects() -> list:
    with _lock:
        return list(reversed(_redirects))


def get_session(session_id: str) -> Optional[RelaySession]:
    with _lock:
        return _sessions.get(session_id)


def _prune_ended_locked():
    ended = [s for s in _sessions.values() if not s.live]
    if len(ended) > _KEEP_ENDED:
        ended.sort(key=lambda s: s.ended_at or s.started_at)
        for s in ended[:-_KEEP_ENDED]:
            _sessions.pop(s.id, None)


def _refuse(feed_id: int, label: str, message: str, status: int, source_tail: str):
    """Raise FeedRefused after an INFO alert, once per standing condition: the measured
    client reconnects every 27 s, so one open alert per feed id and cause, not one per try."""
    from .alerts import create_alert, has_open_alert
    source = f'feed:{feed_id}:{source_tail}'
    alert_type = 'FEED_NO_MEMBER' if source_tail == 'no-member' else 'VIEWER_REFUSED'
    if not has_open_alert(alert_type, source):
        title = (f'Output feed: nothing to play for {label}' if alert_type == 'FEED_NO_MEMBER'
                 else f'Output feed: an IPTV app was refused {label}')
        create_alert(alert_type, title, body=message, source=source)
    raise FeedRefused(message, status)


def start_session(feed_id: int, client: str, cfg=None) -> RelaySession:
    """Start relaying `feed_id` to one IPTV app. Raises FeedRefused with a user-facing message:
    404 for an id the feed does not serve, 503 at the viewer cap, 409 when nothing is
    playable (a blocked account, the allowlist, the connection limit) and 500 when ffmpeg
    could not start. Requires an app context; the returned session is registered and reaped.
    """
    from flask import current_app
    from .config import load_config
    from .recorder import resolve_capture_pacing

    cfg = cfg or load_config()
    fcfg = feed_config(cfg)
    play = resolve_play(feed_id, cfg)
    if play is None:
        raise FeedRefused('No such channel on this feed', 404)
    if play.member is None:
        _refuse(feed_id, play.label, f'Nothing to play for {play.label}: {play.why_none}.',
                409, 'no-member')

    member = play.member
    cap = int(fcfg.get('max_viewers') or 1)
    pace, _source = resolve_capture_pacing(cfg, member.pace_realtime, 0, False)
    session = RelaySession(
        id=secrets.token_urlsafe(12), feed_id=feed_id, label=play.label, client=client,
        group_id=play.group.id if play.group is not None else None,
        channel_id=member.id, channel_name=member.name, account_id=member.account_id,
        ring=Ring(int(fcfg.get('buffer_mb') or 16) * 1024 * 1024,
                  float(fcfg.get('reserve_seconds') or 0)),
        idle_timeout=float(fcfg.get('idle_seconds') or 30),
        max_seconds=float(fcfg.get('max_session_hours') or 12) * 3600,
        stall_timeout=float((cfg.get('watchdog') or {}).get('stall_timeout_seconds') or 30),
        pace_realtime=pace, started_mono=time.monotonic(),
        app=current_app._get_current_object(),
    )
    # The cap is counted and the session registered under the ONE lock, so two tune-ins in
    # the same instant cannot both pass the count (the check-then-act shape
    # .claude/rules/background-jobs.md names). Registered BEFORE the seat is taken, so a
    # recording preempting in the window between acquire and Popen finds something to flag.
    with _lock:
        live = sum(1 for s in _sessions.values() if s.live)
        if live < cap:
            _sessions[session.id] = session
            _prune_ended_locked()
    if live >= cap:
        _refuse(feed_id, play.label,
                f'The output feed is at its viewer cap ({cap} at once). Stop another IPTV app '
                f'or raise the cap in Settings.', 503, 'cap')
    _launch(session, member, cfg, first=True)
    _ensure_reaper()
    log.info('Viewer %s from %s: %s playing channel %d (%s) on account %d', session.id,
             client, session.label, member.id, member.name, member.account_id)
    return session


def _take_seat(session: RelaySession, member, first: bool) -> bool:
    """A seat for `member`'s account: a fresh one on the first play, the seat already held
    on a failover within the account, or that seat moved across accounts in one step
    (connection_limits.move_seat - never release-then-acquire, which can leave the session
    holding nothing)."""
    if first:
        return connlim.try_acquire(member.account_id, 'viewer', session.id)
    if member.account_id == session.account_id:
        return True
    return connlim.move_seat('viewer', session.id, session.account_id, member.account_id)


def _launch(session: RelaySession, member, cfg, first: bool = True) -> None:
    """Take the seat and start ffmpeg on `member`, wiring its stdout to the ring through a
    reader thread. Raises FeedRefused; on the first play the session is finished first and
    the refusal alert raised, on a failover the caller decides what the refusal means."""
    from .account_blocks import blocked_reason
    from .account_links import render_held_login
    from .config import load_config

    if not _take_seat(session, member, first):
        account_name = member.account.name if member.account else f'account {member.account_id}'
        blocked = blocked_reason(member.account_id, account_name, capital=True)
        if blocked:
            detail = f'{blocked}.'
        else:
            who = connlim.describe_holders(member.account_id)
            detail = (f'{account_name} is at its connection limit'
                      + (f' ({who} is using it)' if who else '') + '.')
        if first:
            _finish(session, REASON_LAUNCH_FAILED, detail=detail)
            _refuse(session.feed_id, session.label, detail, 409, 'seat')
        raise FeedRefused(detail, 409)

    session.channel_id = member.id
    session.channel_name = member.name
    session.account_id = member.account_id
    launch_url, session.login_id = render_held_login(member.stream_url, member.account_id,
                                                     'viewer', session.id)
    cfg = cfg or load_config()
    cmd = build_relay_cmd(cfg, launch_url, pace_realtime=session.pace_realtime)
    log.info('Viewer %s: launching %s', session.id, mask_creds_in_text(' '.join(cmd)))
    try:
        fd, session.stderr_path = tempfile.mkstemp(prefix='channelbin-relay-', suffix='.stderr')
        os.close(fd)
        # stderr to a file, never a pipe (dev/changelog/430); stdout IS a pipe, and the
        # reader thread below is its one continuous drain.
        with open(session.stderr_path, 'wb') as err:
            proc = subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                    stderr=err)
    except OSError as exc:
        detail = mask_creds_in_text(str(exc))
        log.error('Viewer %s: could not start ffmpeg: %s', session.id, detail)
        _remove_stderr(session)
        if first:
            _finish(session, REASON_LAUNCH_FAILED, detail=detail)
        raise FeedRefused(f'Could not start ffmpeg: {detail}', 500)

    reader = threading.Thread(target=_read_loop, args=(session, proc), daemon=True,
                              name=f'relay-{session.id}')
    with _lock:
        # The session may have ended while ffmpeg was launching: a recording took the seat,
        # or (on a failover) the IPTV app closed the response and _finish() already released
        # everything. Either way the new ffmpeg is never adopted - a process nothing tracks
        # is the one shape the teardown rule forbids.
        adopt = session.live and not session.preempted
        if adopt:
            session.proc = proc
            session.reader = reader
            session.launched_mono = time.monotonic()
        preempted = session.preempted
    if not adopt:
        terminate_or_kill(proc)
        _remove_stderr(session)
        if preempted:
            _finish(session, REASON_PREEMPTED)
            raise FeedRefused(REASON_TEXT[REASON_PREEMPTED], 409)
        raise FeedRefused(REASON_TEXT[REASON_CLIENT_GONE], 409)
    reader.start()


def _read_loop(session: RelaySession, proc: subprocess.Popen) -> None:
    """Drain ffmpeg's stdout into the ring until it ends. The drain is unconditional: the
    IPTV app's speed never reaches this loop, so a frozen client cannot stall ffmpeg."""
    out = proc.stdout
    try:
        while True:
            try:
                chunk = out.read1(READ_CHUNK)
            except (ValueError, OSError):
                return
            if not chunk:
                return
            session.ring.append(chunk)
            now = time.monotonic()
            with _lock:
                session.last_data_mono = now
                if session.state == STATE_STARTING and session.live:
                    session.state = STATE_STREAMING
    finally:
        try:
            out.close()
        except OSError:
            pass   # the pipe is already gone with the process


def stream(session: RelaySession):
    """The HTTP body: chunks from the ring on the reserve's pacing until the session ends.
    Runs outside any request or app context - everything it needs is on the session - and
    its close (the IPTV app disconnected, changed channel, was turned off) ends the session
    REASON_CLIENT_GONE. A session the reaper ended stops the body on its next turn."""
    try:
        while session.live:
            chunk = session.ring.take(timeout=1.0)
            if chunk is None:
                continue
            with _lock:
                session.last_read_mono = time.monotonic()
                session.bytes_sent += len(chunk)
            yield chunk
    finally:
        _finish(session, REASON_CLIENT_GONE)


def _finish(session: RelaySession, reason: str, detail: str = '') -> bool:
    """Tear down everything the session acquired - ffmpeg, the seat, the stderr spool, the
    ring - and only THEN mark it STOPPED with `reason`. Idempotent and serialized, as
    preview._finish is: the first caller does the work, later ones wait and return False.
    No database and no app context, so the shutdown signal handler can call it."""
    with _lock:
        claimed = session.live and not session._finishing
        if claimed:
            session._finishing = True
            session._teardown_lock.acquire()
        proc, reader = session.proc, session.reader
    if not claimed:
        with session._teardown_lock:
            return False
    try:
        try:
            if proc is not None:
                terminate_or_kill(proc)
            # The reader ends on the pipe's EOF, which the kill produces; joined before the
            # ring is cleared so ffmpeg's drained tail cannot land in a cleared ring.
            if reader is not None and reader is not threading.current_thread():
                reader.join(timeout=5.0)
        finally:
            try:
                connlim.release(session.account_id, 'viewer', session.id)
            finally:
                _remove_stderr(session)
                session.ring.clear()
        with _lock:
            session.state = STATE_STOPPED
            session.reason = reason
            session.detail = detail
            session.ended_at = datetime.utcnow()
    finally:
        session._teardown_lock.release()
    level = logging.INFO if reason in (REASON_CLIENT_GONE, REASON_SHUTDOWN) else logging.WARNING
    log.log(level, 'Viewer %s: %s stopped after %s - %s%s', session.id, session.label,
            fmt_bytes(session.bytes_sent), reason, f' ({detail})' if detail else '')
    return True


def _remove_stderr(session: RelaySession) -> None:
    if session.stderr_path:
        try:
            os.unlink(session.stderr_path)
        except OSError:
            pass   # already gone, or never created


def stop_all(reason: str = REASON_SHUTDOWN) -> int:
    with _lock:
        live = [s for s in _sessions.values() if s.live]
    for s in live:
        _finish(s, reason)
    return len(live)


def kill_all_viewers():
    """Shutdown hook (run.py): end every relay so a restarted process never leaves an
    orphaned ffmpeg pulling a provider stream for an IPTV app it cannot serve. No DB, signal-handler
    safe."""
    stop_all(REASON_SHUTDOWN)


def preempt_for_account(app, account_id: int, recording_id: int) -> bool:
    """A recording on `account_id` needs the seat: end every live session on that account
    REASON_PREEMPTED, and tell the user twice - a VIEWER_PREEMPTED alert naming the
    recording, and a diagnostics event on the recording's own timeline saying what it
    displaced (section 4.4). Called by recorder._try_acquire_slot_with_preemption after
    connection_limits.preempt_viewers_for_slot stripped the seats; a session with proc=None
    is inside its launch window and is flagged so it never runs on the stripped seat."""
    with _lock:
        matched = [s for s in _sessions.values() if s.live and s.account_id == account_id]
        for s in matched:
            s.preempted = True
            s.preempted_by = recording_id
        launched = [s for s in matched if s.proc is not None]
    for s in launched:
        _finish(s, REASON_PREEMPTED, detail=f'recording {recording_id} took the connection')
    if matched:
        try:
            _announce_preemption(app, matched, recording_id, account_id)
        except Exception:                               # noqa: BLE001 - a note, never the recording
            # This runs inside the recorder's own acquire path: a viewer's note that cannot be
            # written must never cost the recording its start (CLAUDE.md principle 2).
            log.exception('Viewer preemption by recording %d: could not write the alert or '
                          'the recording event', recording_id)
    return bool(matched)


def _announce_preemption(app, sessions, recording_id: int, account_id: int) -> None:
    from . import db
    from .alerts import create_alert
    from .database import DIAGNOSTICS, Account, Recording, add_recording_event
    from .db_utils import retry_on_locked

    with app.app_context():
        rec = db.session.get(Recording, recording_id)
        account = db.session.get(Account, account_id)
        rec_name = rec.name if rec else f'#{recording_id}'
        account_name = account.name if account else f'account {account_id}'
        for s in sessions:
            create_alert(
                'VIEWER_PREEMPTED',
                f'IPTV app cut for recording "{rec_name}"',
                body=(f'An IPTV app watching {s.label} through the output feed (channel '
                      f'"{s.channel_name}" on {account_name}) was cut because recording '
                      f'"{rec_name}" needed the connection. A recording always wins the '
                      f'seat; the IPTV app reconnects on its own if another connection is free.'),
                source=f'feed:{s.feed_id}:preempted', recording_id=recording_id)

        @retry_on_locked()
        def _note_on_recording_and_commit():
            if db.session.get(Recording, recording_id) is None:
                return
            for s in sessions:
                add_recording_event(
                    recording_id, DIAGNOSTICS,
                    detail=(f'Took the connection from an IPTV app watching {s.label} '
                            f'(channel "{s.channel_name}") on {account_name}.'),
                    extra={'kind': 'viewer_preempted', 'feed_id': s.feed_id,
                           'channel_id': s.channel_id, 'account_id': account_id,
                           'client': s.client})
            db.session.commit()

        _note_on_recording_and_commit()


# ── Failover and the reaper ────────────────────────────────────────────────────────────────

def _check(session: RelaySession) -> None:
    now = time.monotonic()
    with _lock:
        proc = session.proc
        preempted = session.preempted
        state = session.state
        failing_over = session._failing_over
    if preempted and proc is not None:
        _finish(session, REASON_PREEMPTED,
                detail=f'recording {session.preempted_by} took the connection')
        return
    if proc is None or failing_over:
        # No process to judge: the session is inside its launch window (start_session()
        # registers it before _launch() adopts the ffmpeg, so a reaper another viewer keeps
        # running sees it with launched_mono still 0.0) or between members on a failover.
        # The launch ends it itself when the seat or the spawn fails.
        return
    if session.ring.dropped_unripe:
        _finish(session, REASON_BUFFER_TOO_SMALL,
                detail=f'{fmt_bytes(session.ring.dropped_unripe)} arrived faster than the '
                       f'buffer holds for a {session.ring.reserve:g}s reserve')
        return
    if session.ring.dropped:
        _finish(session, REASON_CLIENT_TOO_SLOW,
                detail=f'{fmt_bytes(session.ring.dropped)} dropped unread')
        return
    # Idle is a fact about the IPTV app, so it is measured on the IPTV app's side of the ring: a chunk
    # the IPTV app could have taken and has not. A provider that stopped sending leaves nothing
    # releasable and is the stall branch's business below, never this one.
    untaken = session.ring.untaken_age(now)
    if untaken > session.idle_timeout:
        _finish(session, REASON_IDLE, detail=f'nothing read for {untaken:.0f}s')
        return
    if now - session.started_mono > session.max_seconds:
        _finish(session, REASON_MAX_DURATION)
        return
    rc = proc.poll() if proc is not None else None
    if rc is not None:
        tail = mask_creds_in_text(read_stderr_tail(session.stderr_path))
        _member_died(session, REASON_FFMPEG_EXITED,
                     f'ffmpeg exited with code {rc}' + (f': {tail}' if tail else ''))
        return
    if state == STATE_STARTING:
        if now - session.launched_mono > CONNECT_TIMEOUT_SECONDS:
            tail = mask_creds_in_text(read_stderr_tail(session.stderr_path))
            _member_died(session, REASON_CONNECT_TIMEOUT, tail)
        return
    if session.last_data_mono is not None and now - session.last_data_mono > session.stall_timeout:
        _member_died(session, REASON_FFMPEG_EXITED,
                     f'no data for {session.stall_timeout:.0f}s')


def _member_died(session: RelaySession, reason: str, detail: str) -> None:
    """The member being relayed stopped delivering: fail over inside the same response when
    the play is a group with somebody left, else end the session by the member's reason."""
    if session.group_id is None or session.app is None:
        _finish(session, reason, detail=detail)
        return
    with _lock:
        if session._failing_over or not session.live:
            return
        session._failing_over = True
    try:
        _failover(session, reason, detail)
    finally:
        with _lock:
            session._failing_over = False


def _failover(session: RelaySession, reason: str, detail: str) -> None:
    from . import db
    from .config import load_config
    from .database import ChannelGroupEvent, GROUP_VIEWER_FAILOVER
    from .db_utils import retry_on_locked

    from .alerts import create_alert, has_open_alert

    failed_id, failed_name = session.channel_id, session.channel_name
    session.failed_ids.add(failed_id)
    with _lock:
        old_proc, old_reader = session.proc, session.reader
        session.proc = session.reader = None
    with session.app.app_context():
        cfg = load_config()
        if old_proc is not None:
            terminate_or_kill(old_proc)
        # The old reader drains the dead member's tail before the new one starts, so the two
        # members' bytes never interleave in the ring at the switch.
        if old_reader is not None:
            old_reader.join(timeout=5.0)
        _remove_stderr(session)
        while True:
            play = resolve_play(session.feed_id, cfg, exclude_ids=frozenset(session.failed_ids))
            if play is None or play.member is None:
                why = play.why_none if play is not None else 'the group is no longer served'
                detail = f'"{failed_name}" {_died_phrase(reason)}; {why}'
                if _finish(session, REASON_NO_MEMBER, detail=detail):
                    source = f'feed:{session.feed_id}:no-member'
                    if not has_open_alert('FEED_NO_MEMBER', source):
                        create_alert('FEED_NO_MEMBER',
                                     f'Output feed: nothing left to play for {session.label}',
                                     body=f'An IPTV app watching {session.label} was cut: {detail}.',
                                     source=source)
                return
            member = play.member
            try:
                _launch(session, member, cfg, first=False)
            except FeedRefused as exc:
                log.warning('Viewer %s: cannot fail over to "%s": %s', session.id, member.name,
                            exc.message)
                if not session.live:
                    return
                session.failed_ids.add(member.id)
                continue
            break
        with _lock:
            if not session.live:
                return
            session.state = STATE_STARTING
            session.last_data_mono = None
            session.failovers.append((failed_name, member.name, reason))
        why = _died_phrase(reason) + (f' ({detail})' if detail else '')
        log.warning('Viewer %s: "%s" %s - moved to "%s"', session.id, failed_name, why,
                    member.name)

        @retry_on_locked()
        def _log_group_event_and_commit():
            db.session.add(ChannelGroupEvent(
                group_id=session.group_id, channel_id=member.id,
                event_type=GROUP_VIEWER_FAILOVER,
                detail=(f'An IPTV app watching through the output feed was moved from "{failed_name}" '
                        f'to "{member.name}": "{failed_name}" {why}.')))
            db.session.commit()

        _log_group_event_and_commit()


def _died_phrase(reason: str) -> str:
    if reason == REASON_CONNECT_TIMEOUT:
        return 'sent nothing in time'
    return 'stopped delivering'


def _reap_loop():
    global _reaper
    while True:
        time.sleep(_REAP_INTERVAL)
        with _lock:
            live = [s for s in _sessions.values() if s.live]
            if not live:
                _reaper = None
                return
        for s in live:
            try:
                _check(s)
            except Exception:                           # noqa: BLE001 - the loop must outlive one session's failure
                log.exception('Viewer %s: reaper check failed', s.id)


def _ensure_reaper():
    global _reaper
    with _lock:
        if _reaper is not None:
            return
        _reaper = threading.Thread(target=_reap_loop, name='relay-reaper', daemon=True)
        _reaper.start()


def wait_for_reaper(timeout: float = 5.0) -> bool:
    with _lock:
        thread = _reaper
    if thread is None:
        return True
    thread.join(timeout)
    return not thread.is_alive()


# ── Redirect mode ──────────────────────────────────────────────────────────────────────────

def redirect_target(feed_id: int, client: str, cfg=None) -> str:
    """The provider URL an IPTV app is sent to for `feed_id` right now (section 5.1), recorded as
    a redirect for the dashboard. Raises FeedRefused on an id the feed does not serve (404)
    or a group with nothing playable (409, with the FEED_NO_MEMBER alert)."""
    play = resolve_play(feed_id, cfg)
    if play is None:
        raise FeedRefused('No such channel on this feed', 404)
    if play.member is None:
        _refuse(feed_id, play.label, f'Nothing to play for {play.label}: {play.why_none}.',
                409, 'no-member')
    member = play.member
    with _lock:
        _redirects.append({'at': datetime.utcnow(), 'feed_id': feed_id, 'label': play.label,
                           'client': client, 'channel_id': member.id,
                           'channel_name': member.name, 'account_id': member.account_id})
        del _redirects[:-_KEEP_ENDED]
    log.info('Viewer from %s: %s redirected to channel %d (%s) on account %d', client,
             play.label, member.id, member.name, member.account_id)
    return member.stream_url


# ── Surfaces ───────────────────────────────────────────────────────────────────────────────

def readiness_facts(cfg=None) -> dict:
    """What the readiness check reads, gathered once (app/readiness.py)."""
    from .database import Account
    fcfg = feed_config(cfg)
    facts = {'enabled': is_enabled(fcfg), 'mode': mode_of(fcfg),
             'credentials_set': credentials_set(fcfg), 'reserve_fits': reserve_fits(fcfg),
             'served': 0, 'missing_held_accounts': []}
    if not facts['enabled']:
        return facts
    facts['served'] = len(catalog(cfg))
    held = held_seats_map(fcfg)
    if held and facts['mode'] == MODE_REDIRECT:
        present = {a.id for a in Account.query.filter(Account.id.in_(list(held))).all()}
        facts['missing_held_accounts'] = sorted(a for a in held if a not in present)
    return facts


def bundle_facts(cfg=None) -> dict:
    """The support bundle's view: config minus the credentials, the live count and the
    last ended sessions by reason. Ids only, never a name - the sanitizer never sees this
    module's labels."""
    fcfg = feed_config(cfg)
    out = {k: v for k, v in fcfg.items() if k not in ('username', 'password')}
    out['credentials_set'] = credentials_set(fcfg)
    out['viewers_now'] = len(live_sessions())
    out['recent_ended'] = [{
        'feed_id': s.feed_id, 'group_id': s.group_id, 'channel_id': s.channel_id,
        'account_id': s.account_id, 'reason': s.reason, 'bytes_sent': s.bytes_sent,
        'failovers': len(s.failovers),
        'started_at': iso_or_none(s.started_at), 'ended_at': iso_or_none(s.ended_at),
    } for s in recent_ended()]
    out['recent_redirects'] = [{
        'feed_id': r['feed_id'], 'channel_id': r['channel_id'], 'account_id': r['account_id'],
        'at': iso_or_none(r['at'])} for r in recent_redirects()]
    return out


def reset_for_tests():
    """Stop everything and forget every session. tests/support/app.py only."""
    stop_all(REASON_SHUTDOWN)
    wait_for_reaper()
    with _lock:
        _sessions.clear()
        del _redirects[:]
