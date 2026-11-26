"""The output feed: ChannelBin as an Xtream/M3U/XMLTV source an IPTV player adds as an account.

What the IPTV player sees is the TV Guide - every in-guide channel, and every in-guide group as ONE
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
  recorder's preemption order, and yields it to a recording by name. A group's or sports
  channel's play moves to another of its channels with a connection free, once the recording
  holds its seat; one with nowhere to go, and any other play, ends REASON_PREEMPTED with a
  VIEWER_PREEMPTED alert naming the recording. Either way the recording's own timeline gets a
  diagnostics event saying what it displaced (section 4.4, dev/changelog/1642).
* The relay is one ffmpeg stream-copying into a fixed-size in-memory ring - no disk, no
  per-client queue, memory a function of config and never of watch time. The reader thread
  is the one drain of ffmpeg's stdout, so a slow IPTV player can never stall the process; an IPTV player that
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
  taken (connection_limits._pool_taken_now), so it never plans on a seat the IPTV player may hold.
* `mixed` mode relays what needs ChannelBin during a play - a group (failover) and a sports
  channel (its waiting screen handing over) - and redirects a plain channel; relays() is the
  one answer. A relayed play sits on one of its account's held seats before the recorder
  loses another (dev/changelog/1536).

* A third kind of feed channel is ChannelBin's own: a followed team, or one of a followed
  league's numbered channels (app/sports_feed.py, dev/changelog/1517). It is not in the TV
  Guide, its guide is TheSportsDB's schedule, and it plays whichever channel's guide lists
  the game within the sports window of now (sports_window(), dev/changelog/1529), ranked like
  a group's members, or, for a game no guide lists, the channel whose name mentions it that
  Record would take (dev/changelog/1643). Its ids start at SPORTS_FEED_BASE.
  A play with no game on, or a game nothing lists, is shown the channel's waiting screen
  (start_waiting(), dev/changelog/1534): ChannelBin's own looped clip of what is next, with no
  provider seat and not counted against max_viewers. A relayed play (proxy mode, and mixed
  mode for a sports channel) has the game put on in its place when it comes on; a game that is on with no slot or seat free waits on the screen too.
  A screen that cannot be made is refused by name, with no alert: neither is a fault.

* A fourth kind is an Up next channel (app/sports_up_next.py, dev/changelog/1614): ChannelBin's
  own picture of the coming games and nothing else, so it is streamed through ChannelBin in every
  mode, takes no provider seat and is not counted against max_viewers (STATE_SHOWING). Its clip
  is made on tune-in, as a waiting screen's is, and a play swaps in the new one when what it
  shows changes. Its ids start at
  UP_NEXT_FEED_BASE; the one for everything followed is that number itself.

* A fifth kind is a picture-in-picture channel (app/pip_channels.py,
  dev/docs/DESIGN-pip-channels.md): two pictures a person chose, each a game, a channel, a
  group or a search, resolved as the matching plain play would be. Its ids start at
  PIP_FEED_BASE, two to a row: the channel and its swapped copy. It is streamed through
  ChannelBin in every mode. A tune-in plays its two pictures combined into one
  (app/pip_engine.py, _start_combined): the play's own ffmpeg is the combiner, and each
  picture is a session of its own in `_internal`, seated, reaped, failed over and preempted
  as a play is, listed and counted nowhere, belonging to the channel's row so two plays of
  it share them. On a box whose GPU cannot combine two it plays its big picture alone
  (section 7.3, `combine_off` says why), and with neither picture able to play, the
  channel's card (STATE_CARD): ChannelBin's own clip saying why, no provider seat, not
  counted, asked again every SCREEN_RECHECK_SECONDS and replaced by the pictures when one
  comes on.

* A channel or a group a person took off the output (`on_output`, written only by
  set_on_output with its event) is in the TV Guide and not in the feed: gone from every
  catalog answer, the numbers after it one lower, its feed id unchanged, and a play of it
  refused by name (dev/changelog/1525).
* Every play that ended, was refused or was redirected is a line in output_feed_plays, the
  page's Recent plays. The line is queued in memory and written by one thread of this
  module's own, never inline: a play ends on threads that may sit inside another writer's
  transaction, and in the shutdown signal handler, where a database write has no place. A
  play cut by a restart is therefore in the app log and not in the table.

Nothing here feeds a health score: a viewer is not an observation.
"""
import contextvars
import hmac
import json
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
from .database import FEED_PLAY_ENDED, FEED_PLAY_REDIRECTED, FEED_PLAY_REFUSED
from .fmt_utils import counted, fmt_bytes, fmt_duration, iso_or_none
from .proc_utils import build_relay_cmd, build_screen_play_cmd, read_stderr_tail, terminate_or_kill
from .url_utils import mask_creds_in_text

log = logging.getLogger(__name__)

MODE_PROXY = 'proxy'
#: Groups and sports channels through ChannelBin, plain channels redirected (dev/changelog/1536).
MODE_MIXED = 'mixed'
MODE_REDIRECT = 'redirect'
MODES = (MODE_PROXY, MODE_MIXED, MODE_REDIRECT)

STATE_STARTING = 'STARTING'     # ffmpeg launched, nothing relayed yet
STATE_STREAMING = 'STREAMING'   # bytes have reached the ring
STATE_STOPPED = 'STOPPED'       # ended; `reason` says why
#: On a sports channel's waiting screen, ChannelBin's own looped clip: no game to play yet,
#: no provider seat, and not counted against max_viewers (dev/changelog/1534).
STATE_WAITING = 'WAITING'
#: On an Up next channel: ChannelBin's own looped picture of the coming games, which is all the
#: channel ever plays. No provider seat, not counted against max_viewers (dev/changelog/1614).
STATE_SHOWING = 'SHOWING'
#: A picture-in-picture channel's card: its big picture has nothing to play right now, and the
#: card says why. Never a second meaning of STATE_WAITING or STATE_SHOWING.
STATE_CARD = 'CARD'
STATES = (STATE_STARTING, STATE_STREAMING, STATE_WAITING, STATE_SHOWING, STATE_CARD, STATE_STOPPED)
#: The states in which a play is on ChannelBin's own clip and holds no provider connection.
OWN_PICTURE_STATES = (STATE_WAITING, STATE_SHOWING, STATE_CARD)

# Why a session ended. Every terminal path names one, and REASON_TEXT is what the dashboard
# shows for it - a stream that stops says why, or it is a number nobody can explain
# (CLAUDE.md principle 1). FAILED_OVER, MOVED_FOR_RECORDING and MOVED_BY_HAND are the non-terminal
# entries: each is recorded in the session's history (its moves) while the session goes on.
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
REASON_MOVED_FOR_RECORDING = 'moved_for_recording'
REASON_MOVED_BY_HAND = 'moved_by_hand'
REASON_NO_MEMBER = 'no_member'
REASON_SHUTDOWN = 'shutdown'
REASON_DISABLED = 'disabled'
REASON_TAKEN_OFF = 'taken_off'
REASON_STOPPED = 'stopped'
REASON_ACCOUNT_OFF = 'account_off'
REASON_SCREEN_FAILED = 'screen_failed'
REASON_SCREEN_GONE = 'screen_gone'
REASON_WINDOW_OPENED = 'window_opened'
REASON_PICTURE_FAILED = 'picture_failed'
REASON_CARD_FAILED = 'card_failed'
REASON_REPLACED = 'replaced'
REASON_LOGIN_OFF = 'login_off'

#: What two of the reasons read as on a sports channel, which has no group.
SPORTS_REASON_TEXT = {
    REASON_FAILED_OVER: 'Moved to another channel carrying the game.',
    REASON_NO_MEMBER: 'Nothing left to play - every channel carrying the game failed or is unavailable.',
}

REASON_TEXT = {
    REASON_CLIENT_GONE: 'The IPTV player stopped watching.',
    REASON_CLIENT_TOO_SLOW: 'Cut - the IPTV player fell further behind than the buffer holds.',
    REASON_BUFFER_TOO_SMALL: ('Cut - more of the stream arrived at once than the buffer holds. Raise '
                              '"Memory held for each player" in the IPTV output\'s settings.'),
    REASON_IDLE: 'Cut - the IPTV player read nothing for a while.',
    REASON_MAX_DURATION: 'Cut - a session runs for a limited time. The IPTV player can tune in again.',
    REASON_PREEMPTED: 'Cut - a recording needed this connection.',
    REASON_FFMPEG_EXITED: 'The stream ended or dropped.',
    REASON_CONNECT_TIMEOUT: 'Could not connect - the stream sent nothing in time.',
    REASON_LAUNCH_FAILED: 'Could not start.',
    REASON_FAILED_OVER: 'Moved to another channel in the group.',
    REASON_MOVED_FOR_RECORDING: 'Moved to another channel - a recording needed the connection.',
    REASON_MOVED_BY_HAND: 'Moved to another channel by hand, from Watching now.',
    REASON_NO_MEMBER: 'Nothing left to play - every channel in the group failed or is unavailable.',
    REASON_SHUTDOWN: 'Cut - the app is restarting.',
    REASON_DISABLED: 'Cut - the IPTV output was turned off.',
    REASON_TAKEN_OFF: 'Cut - the channel was taken off the IPTV output.',
    REASON_STOPPED: 'Stopped from the IPTV Output page.',
    REASON_ACCOUNT_OFF: 'Cut - its account was switched off for the IPTV output.',
    REASON_SCREEN_FAILED: 'Cut - the waiting screen stopped playing.',
    REASON_PICTURE_FAILED: 'Cut - the Up next picture stopped playing.',
    REASON_CARD_FAILED: 'Cut - the picture-in-picture card stopped playing.',
    REASON_REPLACED: 'Ended - the same IPTV player tuned in again, to another channel or this one.',
    REASON_LOGIN_OFF: 'Cut - its IPTV output login was turned off, changed or deleted.',
    REASON_SCREEN_GONE: 'Cut - the sports channel is no longer on the IPTV output.',
    REASON_WINDOW_OPENED: ('Ended - the game came on. In redirect mode ChannelBin cannot tune a player in, '
                           'so the waiting screen stops and the next tune-in gets the game.'),
}

#: Reads from ffmpeg's stdout are this big; the ring holds whole chunks.
READ_CHUNK = 64 * 1024


class _SourceMark(bytes):
    """The zero-length chunk Ring.mark() puts where one ffmpeg's bytes end and the next one's
    begin (a failover, a move, the waiting screen handing over to the game, a new clip). A player
    never sees it: stream() drops it. The browser preview asks for it (stream(marks=True)) because
    its HLS segmenter cannot follow a change of codec inside one input - measured with ffmpeg 7.1,
    an AAC track followed by an AC3 one was decoded as AAC and came out as garbage, so the preview
    starts a new segmenter at each mark (app/preview.py, dev/changelog/1645)."""


SOURCE_MARK = _SourceMark()
#: The client a browser preview's play is listed under (app/preview.py::start_feed_preview).
PREVIEW_CLIENT = 'ChannelBin preview'
#: How long a session may take to its first byte before it ends REASON_CONNECT_TIMEOUT -
#: the same window the preview gives a channel (preview.connect_timeout_seconds' default).
CONNECT_TIMEOUT_SECONDS = 20.0
#: Bytes per second of reserve the settings validation assumes, a typical HD stream at
#: 8 Mbps (design doc section 4.3). The ring must hold the reserve with room to spare.
RESERVE_BYTES_PER_SECOND = 1_000_000
#: How much of the ring the reserve may fill before the validation refuses it, and how full
#: the ring gets before a chunk is released ahead of its reserve (Ring.take).
RESERVE_RING_SHARE = 0.75
#: How long a tune-in at the viewer cap, or at a seat another play of the output holds, waits
#: for that play to end before it is refused. An IPTV player changing channel asks for the new
#: channel before ChannelBin has seen the old connection close: prod measured the old play
#: ending 2.0 to 3.1 s after the first request for the new one, and every change of channel
#: was refused three or four times first (dev/docs/BUGS.md 2026-11-12). A play that is really
#: still being watched is still there after the wait, and the refusal is the same one, this
#: much later. Waited for, never cut: two players behind one address look like one player
#: changing channel, and each would end the other's play in turn.
SWITCH_GRACE_SECONDS = 5.0
_SWITCH_POLL = 0.05
#: How many ended sessions and redirects are kept for the dashboard and the bundle.
_KEEP_ENDED = 20
#: How long a line of Recent plays is kept, and how often the writer looks for older ones.
PLAY_LOG_DAYS = 30
_PRUNE_EVERY_SECONDS = 3600.0
_LOG_INTERVAL = 0.5
#: A refusal asked again within this long of the last time is the same line, counted.
REFUSAL_RUN_GAP = timedelta(hours=1)
_REAP_INTERVAL = 0.5
#: How often a play on a sports channel's waiting screen asks again whether its game is on,
#: and whether what the screen says has changed. Start broadcast asks at once (recheck_waiting).
SCREEN_RECHECK_SECONDS = 30.0
#: What a play's history calls the waiting screen, as the channel it moved from.
SCREEN_NAME = 'the waiting screen'
#: What a play's line in Recent plays names as what an Up next channel played.
PICTURE_NAME = 'the Up next picture'
#: And what a play on a picture-in-picture channel's card is on.
CARD_NAME = 'the picture-in-picture card'

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


#: A stream address is built by the player as /live/<username>/<password>/<id>.ts, so neither
#: half of the login may hold what ends or splits a path.
LOGIN_BREAKERS = '/?#%\\'
LOGIN_MAX = 60


def login_problem(label: str, value: str) -> str:
    """Why `value` cannot be the feed's username or password (`label` says which), or ''.
    The one check, for every route that saves either half."""
    if len(value) > LOGIN_MAX:
        return f'The {label} can be at most {LOGIN_MAX} characters.'
    if any(c.isspace() for c in value) or any(c in LOGIN_BREAKERS for c in value):
        return (f'The {label} cannot hold a space or any of {" ".join(LOGIN_BREAKERS)} - an IPTV '
                f'player puts it inside the address of every stream it plays.')
    return ''


# ── Logins ─────────────────────────────────────────────────────────────────────────────────

#: The most streams one login for another user may hold at once, and the fewest.
MAX_STREAMS_BOUNDS = (1, 20)
#: Characters a generated password is made of: no LOGIN_BREAKERS, and none that read alike on
#: a TV keyboard (0/O, 1/l/I).
_PASSWORD_ALPHABET = 'abcdefghijkmnpqrstuvwxyzABCDEFGHJKLMNPQRSTUVWXYZ23456789'
GENERATED_PASSWORD_LENGTH = 12


@dataclass(frozen=True)
class FeedLogin:
    """Who an IPTV player logged in as: the main login in Settings (`feed_login_id` None) or one
    OutputFeedLogin row (dev/changelog/1760). Every URL the output serves this player is built
    from these credentials, never another login's."""
    username: str
    password: str
    feed_login_id: Optional[int] = None
    name: str = ''
    # How many plays it may have at once; None is no limit of its own (the main login, which
    # only the output's own "IPTV players at once" holds).
    max_streams: Optional[int] = None
    # How many times stop_login() had run for this login when the gate read it: a play started
    # on an older count is refused when it registers (_LoginRoom.still_good_locked).
    generation: int = 0

    @property
    def is_main(self) -> bool:
        return self.feed_login_id is None

    @property
    def log_name(self) -> Optional[str]:
        """What a line of Recent plays stores in its `feed_login`: the username, None for the main
        login."""
        return None if self.is_main else self.username


def main_login(fcfg: dict) -> FeedLogin:
    return FeedLogin(feed_username(fcfg), feed_password(fcfg))


def _feed_login_of(row, generation: int = 0) -> FeedLogin:
    return FeedLogin(row.username, row.password, feed_login_id=row.id, name=row.name or '',
                     max_streams=max(1, int(row.max_streams or 1)), generation=generation)


def _login_generation() -> dict:
    with _lock:
        return dict(_login_changes)


def feed_login_for(fcfg: dict, username, password) -> Optional[FeedLogin]:
    """The login an IPTV player's credentials name, or None. None for every cause - the output
    off, an unknown username, a login turned off, a wrong password - so the caller answers them
    all alike (section 6.1). A login for another user works whether or not the main login is
    set. Every path compares a password in constant time; an unknown username still costs the
    same database lookup a known one does."""
    if not is_enabled(fcfg):
        return None
    if not isinstance(username, str) or not isinstance(password, str):
        return None
    if credentials_ok(fcfg, username, password):
        return main_login(fcfg)
    from .database import OutputFeedLogin
    # Read BEFORE the row: a change committed after the row was read moves the count after
    # this read too, so a play this login starts is refused when it registers (_LoginRoom).
    generation = _login_generation()
    row = OutputFeedLogin.query.filter_by(username=username).first()
    stored = row.password if row is not None and row.enabled else ''
    if not hmac.compare_digest(stored.encode(), password.encode()) or not stored:
        return None
    return _feed_login_of(row, generation.get(row.id, 0))


def login_taken(username: str, fcfg: dict, except_login_id: Optional[int] = None) -> bool:
    """Whether `username` is already the main login's or another login's. Two logins with one
    username could not be told apart by the gate."""
    from .database import OutputFeedLogin
    if username == feed_username(fcfg):
        return True
    q = OutputFeedLogin.query.filter_by(username=username)
    if except_login_id is not None:
        q = q.filter(OutputFeedLogin.id != except_login_id)
    return q.first() is not None


def generated_password() -> str:
    return ''.join(secrets.choice(_PASSWORD_ALPHABET) for _ in range(GENERATED_PASSWORD_LENGTH))


def max_streams_problem(value) -> str:
    """Why `value` cannot be a login's stream limit, or ''."""
    low, high = MAX_STREAMS_BOUNDS
    try:
        n = int(value)
    except (TypeError, ValueError):
        return 'The number of streams at once must be a whole number.'
    if n < low or n > high:
        return f'The number of streams at once must be between {low} and {high}.'
    return ''


#: The login the tune-in on this thread came in on, for the lines a refusal or a redirect
#: writes to Recent plays: every one of those is written inside the tune-in's own call
#: (start_session, redirect_play), which sets this for its duration.
_tuning_login: contextvars.ContextVar = contextvars.ContextVar('output_feed_tuning_login',
                                                              default=None)


def _tuning_log_name() -> Optional[str]:
    login = _tuning_login.get()
    return login.log_name if login is not None else None


#: The lowest and highest value each numeric setting takes. A number outside is moved to the
#: nearest end by the save, and the page's fields carry the same pair.
SETTING_BOUNDS = {
    'xmltv_days': (1, 14),
    'max_viewers': (1, 99),
    'reserve_seconds': (0, 120),
    'idle_seconds': (1, 3600),
    'max_session_hours': (1, 168),
    'buffer_mb': (1, 1024),
    'sports_before_minutes': (0, 720),
    'sports_after_minutes': (0, 720),
}


def guide_days(fcfg: dict) -> int:
    """How many days of guide the output sends (xmltv_days, 3 when unset), held to SETTING_BOUNDS.
    A numbered sports channel with no game inside them is left out of the channel list too
    (dev/changelog/1621)."""
    low, high = SETTING_BOUNDS['xmltv_days']
    try:
        days = int(fcfg.get('xmltv_days') or 3)
    except (TypeError, ValueError):
        log.warning('integrations.output_feed.xmltv_days is not a whole number (%r); using 3', fcfg.get('xmltv_days'))
        days = 3
    return min(high, max(low, days))


def guide_horizon(cfg=None) -> timedelta:
    """How far past now the guide the output sends reaches: a game starting past it is in no IPTV
    player's cached guide, so a league's layout may still move it (sports_feed.place_league_events,
    dev/changelog/1684)."""
    return timedelta(days=guide_days(feed_config(cfg)))


def sports_window(fcfg: dict) -> tuple:
    """(before, after): how far ahead of a game's listing and past its end a sports channel plays
    the channel that lists it (sports_feed.playing_now, dev/changelog/1529). A missing key is 0,
    which is the listing's own time; a hand-edited value is held to SETTING_BOUNDS."""
    out = []
    for key in ('sports_before_minutes', 'sports_after_minutes'):
        low, high = SETTING_BOUNDS[key]
        try:
            minutes = int(fcfg.get(key) or 0)
        except (TypeError, ValueError):
            log.warning('integrations.output_feed.%s is not a whole number (%r); using 0', key, fcfg.get(key))
            minutes = 0
        out.append(timedelta(minutes=min(high, max(low, minutes))))
    return tuple(out)


def mode_of(fcfg: dict) -> str:
    mode = fcfg.get('mode') or MODE_PROXY
    return mode if mode in MODES else MODE_PROXY


def relays(fcfg: dict, feed_id: int) -> bool:
    """Whether a play of `feed_id` goes through ChannelBin (a relay session) rather than a 302
    to the provider. Decided by the feed id's kind alone, so it costs no query: in mixed mode
    a group and a sports channel are relayed, because failover and a waiting screen handing
    over happen during a play, where a redirected TV is out of ChannelBin's reach."""
    mode = mode_of(fcfg)
    if decode_feed_id(feed_id)[0] in (KIND_UP_NEXT, KIND_PIP):
        # ChannelBin's own picture is the only thing an Up next channel plays, and a
        # picture-in-picture channel has no one provider stream to send a player to.
        return True
    if mode == MODE_PROXY:
        return True
    if mode == MODE_REDIRECT:
        return False
    if mode == MODE_MIXED:
        return decode_feed_id(feed_id)[0] != KIND_CHANNEL
    raise ValueError(f'Unknown feed mode "{mode}"')


def relays_for(fcfg: dict, feed_id: int, login: FeedLogin) -> bool:
    """relays() for one login's tune-in. A login for another user is always relayed, whatever
    the mode: a redirect sends the player to the provider's own stream URL, which carries the
    owner's provider username and password, and puts the provider account on that user's
    address (dev/changelog/1760)."""
    return not login.is_main or relays(fcfg, feed_id)


def seen_in_mode(session: 'RelaySession', mode: str) -> bool:
    """Whether Watching now lists `session` in `mode`. In redirect mode ChannelBin's own
    pictures and a login for another user's plays are its own stream; anything else live then
    is from before a change of mode."""
    if mode in (MODE_PROXY, MODE_MIXED):
        return True
    if mode == MODE_REDIRECT:
        return session.own_picture or session.feed_login_id is not None
    raise ValueError(f'Unknown feed mode "{mode}"')


def relays_some(fcfg: dict) -> bool:
    """Whether any play goes through ChannelBin in this mode (proxy, mixed)."""
    return mode_of(fcfg) != MODE_REDIRECT


def redirects_some(fcfg: dict) -> bool:
    """Whether any play is sent straight to the provider in this mode (redirect, mixed)."""
    return mode_of(fcfg) != MODE_PROXY


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
    """{account_id: seats the IPTV player may be using} for `account_ids` (None: every account),
    non-empty only while the feed is on in a mode that redirects (redirect, mixed) - the one
    reader connection_limits subtracts from, which nets out the seats relayed plays already
    hold. `cfg` is the caller's already-loaded config; a caller in a loop hands it in."""
    fcfg = feed_config(cfg)
    if not is_enabled(fcfg) or not redirects_some(fcfg):
        return {}
    held = held_seats_map(fcfg)
    if account_ids is None:
        return held
    wanted = {a for a in account_ids if a is not None}
    return {a: n for a, n in held.items() if a in wanted}


def held_seats_problem(held: dict, cfg: dict) -> str:
    """Why `held` ({account id: connections recordings leave alone}) cannot be saved, or ''.
    A number above what the account allows would leave recordings nothing and still not
    give the IPTV player more than the provider does; an id that names no account is a typo.
    The one check, for the Settings page and the IPTV Output page alike."""
    from .database import Account
    wanted = {}
    for raw_id, raw_n in (held or {}).items():
        account_id, n = int(raw_id), int(raw_n)
        if n > 0:
            wanted[account_id] = n
    if not wanted:
        return ''
    accounts = Account.query.filter(Account.id.in_(list(wanted))).all()
    limits = connlim.limits_for_accounts(
        accounts, (cfg.get('accounts') or {}).get('default_max_connections', 1))
    names = {a.id: a.name for a in accounts}
    for account_id, n in sorted(wanted.items()):
        if account_id not in names:
            return f'There is no account with id {account_id}.'
        if n > limits[account_id]:
            return (f'{names[account_id]} allows {counted(limits[account_id], "connection")} at once, '
                    f'so recordings cannot leave {n} alone.')
    return ''


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
KIND_SPORTS = 'sports'
KIND_UP_NEXT = 'up_next'
KIND_PIP = 'pip'

#: Where sports channels' feed ids start. Channels and groups between them use every integer
#: from 0 up and may never be renumbered (an IPTV player caches them), so the third kind takes a
#: range they cannot reach in practice: the first channel id that would land here is
#: 500,000,000. Below 2**31, since an Xtream client reads a stream id as a 32-bit integer.
SPORTS_FEED_BASE = 1_000_000_000
#: Where Up next channels' feed ids start (dev/changelog/1614): the one for everything followed
#: is this number, a league's is this plus its row id. Leaves sports channels 500,000,000 ids,
#: and stays below 2**31.
UP_NEXT_FEED_BASE = 1_500_000_000
#: Where picture-in-picture channels' feed ids start (dev/docs/DESIGN-pip-channels.md 4.2): a
#: row's channel is this plus twice its id, its swapped copy one more. Leaves Up next channels
#: 500,000,000 ids, and stays below 2**31 for 73 million rows.
PIP_FEED_BASE = 2_000_000_000


def feed_id_for_channel(channel_id: int) -> int:
    return 2 * channel_id


def feed_id_for_group(group_id: int) -> int:
    return 2 * group_id + 1


def feed_id_for_sports(row_id: int) -> int:
    return SPORTS_FEED_BASE + row_id


def feed_id_for_up_next(number: int) -> int:
    """`number` is sports_up_next.UpNext.number: 0 for everything followed, else a row id."""
    return UP_NEXT_FEED_BASE + number


def feed_id_for_pip(row_id: int, swapped: bool = False) -> int:
    return PIP_FEED_BASE + 2 * row_id + (1 if swapped else 0)


def pip_swapped(feed_id: int) -> bool:
    """Whether a picture-in-picture feed id is the row's swapped copy."""
    return feed_id >= PIP_FEED_BASE and bool((feed_id - PIP_FEED_BASE) % 2)


def decode_feed_id(feed_id: int):
    """(kind, row id) for a feed id - no table needed, which is the point (section 3.3). An Up
    next channel's "row id" is its number: 0 for everything followed. A picture-in-picture
    channel and its swapped copy decode to the same row; pip_swapped() says which it is."""
    if feed_id >= PIP_FEED_BASE:
        return KIND_PIP, (feed_id - PIP_FEED_BASE) // 2
    if feed_id >= UP_NEXT_FEED_BASE:
        return KIND_UP_NEXT, feed_id - UP_NEXT_FEED_BASE
    if feed_id >= SPORTS_FEED_BASE:
        return KIND_SPORTS, feed_id - SPORTS_FEED_BASE
    if feed_id % 2:
        return KIND_GROUP, (feed_id - 1) // 2
    return KIND_CHANNEL, feed_id // 2


# ── The catalog ────────────────────────────────────────────────────────────────────────────

@dataclass
class FeedRow:
    """One channel as the IPTV player sees it. `row` is the Channel, ChannelGroup or
    sports_feed.Served behind it, `listings_channel_id` the channel whose EPG rows fill it
    (None for a sports channel, whose guide is its schedule), `logo_channel_id` the channel
    whose logo it shows (None when it has none, and for ChannelBin's own channels, which
    show ChannelBin's) - see logo_url()."""
    feed_id: int
    kind: str
    num: int
    name: str
    category: str
    logo_channel_id: Optional[int]
    row: object = field(repr=False)
    listings_channel_id: Optional[int]
    members: list = field(default_factory=list, repr=False)
    # False when a person took the row off the output (set_on_output).
    on_output: bool = True
    # Why the row is not offered at all though its switch is on, '' when it is: its account
    # is one the output may not play from.
    left_out: str = ''
    # Why a play of it would be refused right now, '' when one would start.
    why_none: str = ''
    # The channel a play would start from right now, for the page's view only (`everything`):
    # the channel itself, or the member a group would pick. None when nothing would play.
    plays: object = field(default=None, repr=False)

    @property
    def offered(self) -> bool:
        return self.on_output and not self.left_out


#: Why a channel whose switch is on is still not offered (FeedRow.left_out).
LEFT_OUT_ACCOUNT = 'its account is one the IPTV output is not allowed to play from'
#: Why a play of a row a person took off the output is refused (Play.why_none).
WHY_TAKEN_OFF = 'it was taken off the IPTV output'


#: Why a candidate was passed over, for the sentence a play with none left gives (_sports_member).
_DROP_RECORDING = 'recording'   # a group with no member that has Recording on
_DROP_BLOCKED = 'blocked'       # on an account blocked right now
_DROP_ALLOWED = 'allowed'       # on an account the output may not serve from
_DROP_FAILED = 'failed'         # already failed this play
_DROP_GONE = 'gone'             # its channel or group was removed while the play was worked out
_DROP_OTHER_GAME = 'other_game' # a group whose members with Recording on all show something else

_GROUP_WHY = {
    _DROP_RECORDING: 'no channel in the group has Recording on',
    _DROP_BLOCKED: 'every channel in the group is on a blocked account',
    _DROP_ALLOWED: 'every channel in the group is on an account the IPTV output is not allowed to serve from',
}


def _members_for_feed(group, blocked, allowed, game=None):
    """A group's members the feed may play, in the order a recording drops them: blocked
    accounts first (account_blocks.split_blocked), then the allowlist. `game` is, for a
    sports play, the members that may carry its game (auto_rules._Showing.members), and the
    rest go before anything else is asked (dev/changelog/1690). Returns (members, why none) -
    `why` is a _DROP_* naming the reason when the list is empty, else ''."""
    from .account_blocks import split_blocked
    from .channel_groups import recording_members
    members = recording_members(group.memberships)
    if not members:
        return [], _DROP_RECORDING
    if game is not None:
        members = [m for m in members if m.id in game]
        if not members:
            return [], _DROP_OTHER_GAME
    kept, _skipped = split_blocked(members, blocked)
    if not kept:
        return [], _DROP_BLOCKED
    if allowed is not None:
        kept = [m for m in kept if m.account_id in allowed]
        if not kept:
            return [], _DROP_ALLOWED
    return kept, ''


def _logo_channel_id(channel) -> Optional[int]:
    if channel is None or not (channel.logo_url or channel.logo_cache_path):
        return None
    return channel.id


#: The picture ChannelBin's own channels (Up next, sports) carry. A static file, so an IPTV
#: player fetches it with no login whether or not the password gate is on.
OWN_LOGO_PATH = '/static/img/logo/channelbin-favicon/android-chrome-512x512.png'


def logo_url(row: 'FeedRow', base_url: str, login: FeedLogin) -> str:
    """The logo a row lists: ChannelBin's own for its own channels, else the feed's logo route
    for the row's channel (routes/output_feed.py::logo), which serves the cached copy or fetches
    the provider's for it. Never the provider's URL itself, which may carry credentials, and
    never /api/channels/<id>/logo, which the password gate keeps from an IPTV player
    (dev/changelog/1618). The route's address carries `login`'s own credentials (dev/changelog/1760)."""
    if row.kind in (KIND_UP_NEXT, KIND_SPORTS, KIND_PIP):
        return f'{base_url}{OWN_LOGO_PATH}'
    if row.kind in (KIND_CHANNEL, KIND_GROUP):
        if row.logo_channel_id is None:
            return ''
        return f'{base_url}/logo/{login.username}/{login.password}/{row.logo_channel_id}'
    raise ValueError(f'unknown feed row kind {row.kind!r}')


def logo_channel(channel_id: int):
    """The Channel whose logo the feed's logo route may serve: one some feed row could carry,
    a channel in the TV Guide or a member of a group that is. None otherwise, so the route
    is never a way to make ChannelBin fetch any logo in a 100k-channel catalog."""
    from . import db
    from .database import Channel, ChannelGroup, ChannelGroupMember
    channel = db.session.get(Channel, channel_id)
    if channel is None:
        return None
    if channel.in_guide:  # guide-flag-read-ok: the same own-row test catalog() makes
        return channel
    in_guide_group = (db.session.query(ChannelGroupMember.id)
                      .join(ChannelGroup, ChannelGroup.id == ChannelGroupMember.group_id)
                      .filter(ChannelGroupMember.channel_id == channel_id,
                              ChannelGroup.in_guide.is_(True))
                      .first())
    return channel if in_guide_group is not None else None


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


def catalog(cfg=None, everything: bool = False) -> list:
    """Every feed row in the guide's order: the in-guide channels and the in-guide groups,
    interleaved by their shared guide_sort_order (channel_groups.guide_row_sort_key), then
    ChannelBin's own channels, Up next and sports, by league (_own_rows). A fixed number
    of queries whatever the guide holds: the channels, the groups with their members and
    accounts, the members' latest health checks, the blocked accounts, the Up next channels, the
    sports channels.

    A channel on an account the allowlist excludes is not served; a group is served even
    when nothing in it is playable right now - the play is what refuses, by name
    (FEED_NO_MEMBER), so the IPTV player's list does not silently shrink. A group's members are
    never listed individually: they are its failover set. A row a person took off the output
    is not served either, and takes no number, so the rows after it move up.

    `everything` is the IPTV Output page's view: the rows that are not offered come back too,
    with `num` 0 and the reason on the row (`on_output`, `left_out`), from the same queries."""
    fcfg = feed_config(cfg)
    rows = guide_rows(fcfg, plays=everything) + _own_rows(everything, fcfg)
    offered = [row for row in rows if row.offered]
    for n, row in enumerate(offered, start=1):
        row.num = n
    return rows if everything else offered


def guide_rows(fcfg: dict, plays: bool = False) -> list:
    """The TV Guide's own feed rows, in its order, unnumbered and whether or not each is on the
    output: catalog()'s first half, and what a picture-in-picture channel's search is matched
    against (app/pip_channels.py), so the page and a tune-in cannot disagree about which
    channel a search names. `plays` fills each group's `plays`. Four queries."""
    from .channel_groups import (blocked_account_ids_now, guide_listings_member,
                                 guide_row_sort_key, member_eager_options, serving_member)
    from .database import Channel, ChannelGroup
    from .routes.channel_tests import _latest_tests_by_channel

    allowed = allowed_account_ids(fcfg)
    blocked = blocked_account_ids_now()
    everything = plays
    channels = Channel.query.filter(Channel.in_guide.is_(True)).all()  # guide-flag-read-ok: the feed's own-row half; the grouped half is the groups below
    groups = (ChannelGroup.query.filter(ChannelGroup.in_guide.is_(True))
              .options(*member_eager_options(with_accounts=True)).all())
    member_ids = {m.channel_id for g in groups for m in g.memberships}
    latest = _latest_tests_by_channel(list(member_ids)) if member_ids else {}

    entries = []
    for ch in channels:
        left_out = (LEFT_OUT_ACCOUNT if allowed is not None and ch.account_id not in allowed
                    else '')
        entries.append((guide_row_sort_key(ch), FeedRow(
            feed_id=feed_id_for_channel(ch.id), kind=KIND_CHANNEL, num=0, name=ch.name,
            category=(ch.category_name or '').strip() or UNCATEGORIZED,
            logo_channel_id=_logo_channel_id(ch), row=ch, listings_channel_id=ch.id,
            on_output=bool(ch.on_output), left_out=left_out,
            plays=None if left_out else ch)))
    for g in groups:
        members, why = _members_for_feed(g, blocked, allowed)
        lead = _group_lead(g, members, latest, blocked)
        listings = guide_listings_member(g) or lead
        plays = None
        if everything and members:
            plays = serving_member(g, latest, blocked_account_ids=blocked, members=members).member
        entries.append((guide_row_sort_key(g), FeedRow(
            feed_id=feed_id_for_group(g.id), kind=KIND_GROUP, num=0, name=g.name,
            category=((lead.category_name or '').strip() if lead else '') or UNCATEGORIZED,
            logo_channel_id=_logo_channel_id(lead), row=g,
            listings_channel_id=listings.id if listings else None, members=members,
            on_output=bool(g.on_output), why_none=_GROUP_WHY[why] if why else '', plays=plays)))
    entries.sort(key=lambda e: e[0])
    return [e[1] for e in entries]


#: The category an IPTV player files the Up next channel for everything followed under.
UP_NEXT_CATEGORY = 'ChannelBin'


#: Why a league's numbered sports channel is not in the channel list right now (FeedRow.left_out).
LEFT_OUT_NO_GAME = 'it has no {one} in the {days} of guide the output sends'


def _own_rows(everything: bool, fcfg: dict) -> list:
    """ChannelBin's own channels, after the guide's: the Up next channel for everything followed,
    then each league's Up next channel ahead of its sports channels (dev/changelog/1614), leagues
    by name. With `everything`, the Up next channels whose switch is off come too, not offered.
    A league's numbered channel with no game in the days of guide the output sends is left out
    (dev/changelog/1621): it is a slot for a busy day, and an IPTV player listing 31 of them with
    nothing in their guide is a list to scroll past. Its feed id stays, so it comes back as the
    same channel; a team's channel is always listed."""
    from . import sports_feed, sports_up_next
    up_next = sports_up_next.channels(everything=everything)
    served = sports_feed.served_channels()
    days = guide_days(fcfg)
    with_games = sports_feed.numbered_with_games(served, datetime.utcnow(), days, sports_window(fcfg)[1])
    out = []
    for ch in up_next:
        if ch.everything:
            out.append(FeedRow(
                feed_id=feed_id_for_up_next(ch.number), kind=KIND_UP_NEXT, num=0, name=ch.name,
                category=UP_NEXT_CATEGORY, logo_channel_id=None, row=ch, listings_channel_id=None,
                on_output=ch.enabled))
    by_league = {}
    for ch in up_next:
        if not ch.everything:
            by_league.setdefault(ch.league.id, (ch.league, []))[1].append(FeedRow(
                feed_id=feed_id_for_up_next(ch.number), kind=KIND_UP_NEXT, num=0, name=ch.name,
                category=ch.league.name, logo_channel_id=None, row=ch, listings_channel_id=None,
                on_output=ch.enabled, left_out=ch.left_out))
    for one in served:
        left_out = ('' if one.is_team or one.row.id in with_games
                    else LEFT_OUT_NO_GAME.format(one=sports_feed.event_word(one.league), days=counted(days, 'day')))
        by_league.setdefault(one.league.id, (one.league, []))[1].append(FeedRow(
            feed_id=feed_id_for_sports(one.row.id), kind=KIND_SPORTS, num=0, name=one.name,
            category=one.league.name, logo_channel_id=None, row=one, listings_channel_id=None,
            left_out=left_out))
    for _key, (_league, league_rows) in sorted(by_league.items(),
                                               key=lambda kv: (kv[1][0].name.lower(), kv[0])):
        out += league_rows
    # Picture-in-picture channels last, in the order they were made, each followed by its
    # swapped copy (dev/docs/DESIGN-pip-channels.md 4.3). One with nothing set up is listed
    # too: a player keeps the list it was given.
    from . import pip_channels
    for one in pip_channels.served():
        out.append(FeedRow(
            feed_id=feed_id_for_pip(one.row.id, one.swapped), kind=KIND_PIP, num=0, name=one.name,
            category=UP_NEXT_CATEGORY, logo_channel_id=None, row=one, listings_channel_id=None,
            on_output=bool(one.row.on_output)))
    return out


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


def live_streams_payload(rows, base_url: str, login: FeedLogin) -> list:
    """get_live_streams: what ChannelBin's own XtreamClient reads plus what the measured
    client needs (section 3.4). `direct_source` is empty: the capture showed the client
    ignores it. `epg_channel_id` is the feed id's string, which is what xmltv.php names."""
    ids = {name: cid for cid, name in categories(rows)}
    out = []
    for row in rows:
        out.append({
            'num': row.num, 'name': row.name, 'stream_type': 'live', 'stream_id': row.feed_id,
            'stream_icon': logo_url(row, base_url, login),
            'epg_channel_id': str(row.feed_id), 'added': '0', 'is_adult': '0',
            'category_id': ids[row.category], 'category_ids': [int(ids[row.category])],
            'custom_sid': '', 'tv_archive': 0, 'direct_source': '', 'tv_archive_duration': 0,
        })
    return out


def auth_payload(fcfg: dict, login: FeedLogin, host: str, port: str, scheme: str) -> dict:
    """The bare player_api.php answer: what accounts.check_xtream_auth() needs (`auth`,
    `status`, `exp_date`) and a server_info naming the host the request arrived on. Never
    an account's base_url - the feed's stream endpoint is ChannelBin itself (section 3.5).
    The login and its connection counts are `login`'s: a login for another user is told its
    own limit and its own plays, the main login the output's (dev/changelog/1760)."""
    now = datetime.utcnow()
    if login.is_main:
        active, most = len(live_sessions()), int(fcfg.get('max_viewers') or 1)
    else:
        active, most = len(login_plays(login.feed_login_id)), login.max_streams
    return {
        'user_info': {
            'username': login.username, 'password': login.password,
            'message': 'ChannelBin IPTV output', 'auth': 1, 'status': 'Active',
            'exp_date': str(int((now + timedelta(days=3650)).timestamp())),
            'is_trial': '0', 'active_cons': str(active),
            'created_at': '0', 'max_connections': str(most),
            'allowed_output_formats': ['ts'],
        },
        'server_info': {
            'url': host, 'port': port, 'https_port': port if scheme == 'https' else '443',
            'server_protocol': scheme, 'rtmp_port': '', 'timezone': 'UTC',
            'timestamp_now': int(now.timestamp()), 'time_now': now.strftime('%Y-%m-%d %H:%M:%S'),
            'process': True,
        },
    }


def stream_url(base_url: str, login: FeedLogin, feed_id: int) -> str:
    return f'{base_url}/live/{login.username}/{login.password}/{feed_id}.ts'


def m3u_text(rows, base_url: str, login: FeedLogin) -> str:
    """get.php as m3u_plus: tvg-id is the feed id string, the URL is the feed's own stream
    URL in every mode - a redirect happens on play, so no provider URL is ever listed."""
    lines = ['#EXTM3U']
    for row in rows:
        logo = logo_url(row, base_url, login)
        name = row.name.replace('\n', ' ').replace('"', "'")
        category = row.category.replace('\n', ' ').replace('"', "'")
        lines.append(f'#EXTINF:-1 tvg-id="{row.feed_id}" tvg-name="{name}" tvg-logo="{logo}" '
                     f'group-title="{category}",{name}')
        lines.append(stream_url(base_url, login, row.feed_id))
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
    days = guide_days(fcfg)
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
    sports = [row.row for row in rows if row.kind == KIND_SPORTS]
    if sports:
        from . import sports_feed
        from .channel_groups import blocked_account_ids_now
        from .tz_utils import get_display_tz, is_24h
        programs = sports_feed.guide_programs(sports, now, days, blocked=blocked_account_ids_now(),
                                              allowed=allowed_account_ids(fcfg))
        for program in programs:
            yield (f'  <programme start="{_xmltv_time(program.start)}" '
                   f'stop="{_xmltv_time(program.stop)}" '
                   f'channel="{feed_id_for_sports(program.row_id)}">'
                   f'<title>{xml_escape(program.title)}</title>'
                   f'<desc>{xml_escape(program.description)}</desc>'
                   f'<category>{xml_escape(program.category)}</category></programme>\n')
        # Between games the guide names the next one instead of leaving the player to show
        # "No information" (dev/changelog/1621). No category: it is not a game.
        for filler in sports_feed.guide_fillers(sports, programs, now, days, get_display_tz(), is_24h()):
            yield (f'  <programme start="{_xmltv_time(filler.start)}" '
                   f'stop="{_xmltv_time(filler.stop)}" '
                   f'channel="{feed_id_for_sports(filler.row_id)}">'
                   f'<title>{xml_escape(filler.title)}</title>'
                   f'<desc>{xml_escape(filler.description)}</desc></programme>\n')
    for row in rows:
        if row.kind == KIND_UP_NEXT:
            yield from _up_next_programs(row, now, days)
    pips = [row for row in rows if row.kind == KIND_PIP]
    if pips:
        from . import pip_channels
        described = pip_channels.describe([row.row.row for row in pips], now)
        for row in pips:
            for start, stop, title, desc in pip_channels.guide_blocks(row.row, described, now, days,
                                                                      UP_NEXT_BLOCK):
                yield (f'  <programme start="{_xmltv_time(start)}" stop="{_xmltv_time(stop)}" '
                       f'channel="{row.feed_id}"><title>{xml_escape(title)}</title>'
                       f'<desc>{xml_escape(desc)}</desc></programme>\n')
    yield '</tv>\n'


#: An Up next channel's guide is one program after another of this length, saying what it is.
UP_NEXT_BLOCK = timedelta(hours=2)


def _up_next_programs(row, now: datetime, days: int):
    """An Up next channel's <programme> lines: blocks of UP_NEXT_BLOCK on the clock's even hours
    across the window, each naming what the channel shows. No query."""
    ch = row.row
    what = ('the coming games in every league you follow' if ch.everything
            else f'the coming {ch.league.name} games')
    desc = xml_escape(f'ChannelBin\'s own picture of {what}, with when and where each is on: a new page '
                      'every 10 seconds. Schedule from TheSportsDB.')
    title = xml_escape(row.name)
    start = now.replace(minute=0, second=0, microsecond=0)
    start -= timedelta(hours=start.hour % 2)
    end = now + timedelta(days=days)
    while start < end:
        stop = start + UP_NEXT_BLOCK
        yield (f'  <programme start="{_xmltv_time(start)}" stop="{_xmltv_time(stop)}" '
               f'channel="{row.feed_id}"><title>{title}</title><desc>{desc}</desc>'
               f'<category>Sports</category></programme>\n')
        start = stop


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
    from when it is one. `why_none` names the reason when `member` is None. A sports
    channel's play carries the game it is (`event_id`), and `fault` is False when having
    nothing to play is an ordinary state - no game on, or one no guide lists - that is
    refused by name and raises no alert."""
    feed_id: int
    label: str
    member: object = field(default=None, repr=False)
    group: object = field(default=None, repr=False)
    why_none: str = ''
    sports: bool = False
    # Why a sports play took its game from this channel (auto_rules.PickReason), for its log line.
    picked: str = ''
    event_id: Optional[int] = None
    fault: bool = True
    #: A sports play taken from outside its listing's own time says so here (sports window,
    #: dev/changelog/1529). Kept off `label`, which a proxy session keeps for its whole life.
    note: str = ''
    #: A sports play's channel (sports_feed.Served) and what it has on (sports_feed.NowPlaying),
    #: which its waiting screen is drawn from.
    served: object = field(default=None, repr=False)
    found: object = field(default=None, repr=False)
    #: An Up next channel's play: its sports_up_next.UpNext. It plays ChannelBin's picture and
    #: nothing else, so `member` stays None and the play is never refused for it.
    up_next: object = field(default=None, repr=False)
    #: Resolved for a browser preview (resolve_play(preview=True)): a group plays whether or not
    #: the output offers it, and an Up next channel whether or not it is switched on.
    preview: bool = False
    #: A picture-in-picture channel's play: its pip_channels.Tuned, the row and what its big
    #: picture is right now. With `member` None the play is on the channel's card.
    pip: object = field(default=None, repr=False)


def _resolve_sports_play(feed_id: int, row_id: int, allowed, exclude_ids, event_id, now,
                         window: tuple, cfg=None, viewer_id=None) -> Optional[Play]:
    """A sports channel's play: the game on now, and the channel a recording of it would be
    taken from (auto_rules.game_sources(), the one answer for Record, a rule and this play,
    dev/changelog/1637), over the listings the play holds. Down that order, a group plays its
    member as the group's own row would (_members_for_feed, then channel_groups.serving_member),
    preferring one on an account with a connection free as a recording's start does, and a
    channel is passed over when its account is blocked, is one the output may not serve from, or
    it already failed this play."""
    from . import sports_feed

    served = sports_feed.served_channels(row_id)
    if not served:
        return None
    play = Play(feed_id, f'sports channel {row_id} ({served[0].name})', sports=True)
    before, after = window
    now = now or datetime.utcnow()
    # resolve_play()'s event_id is only ever a play already on that game (its docstring).
    found = sports_feed.playing_now(served[0], now, event_id=event_id, before=before, after=after,
                                    held=event_id is not None)
    play.served, play.found = served[0], found
    return _play_found(play, found, allowed, exclude_ids, now, cfg, viewer_id)


def _play_found(play: Play, found, allowed, exclude_ids, now, cfg, viewer_id) -> Play:
    """Fill `play` from `found` (sports_feed.NowPlaying): the game, and the channel a recording
    of it would be taken from. The one walk for a sports channel's play and for a
    picture-in-picture channel's picture that is a game (resolve_game_play)."""
    from .auto_rules import PICK_EVENT, game_sources
    from .channel_groups import blocked_account_ids_now
    from .fmt_utils import counted
    from .sports_data import event_game_name
    play.found = found
    if not found.channels:
        play.why_none = found.why_none
        play.fault = False
        return play
    game = event_game_name(found.event)
    play.event_id = found.event.id
    play.note = found.note
    if found.named is not None:
        # No guide lists it: the channels naming it, in the order Record takes them, never a copy
        # of that order (dev/changelog/1643).
        play.label = f"{play.label}: {game}, from a channel's name"
        picked = game_sources(found.event.id, now, cfg)
        sources = picked.sources if picked is not None and picked.pick.kind == PICK_EVENT else []
        if not sources:
            play.why_none = (f"a channel's name mentions {game}, but Record would no longer take it from one: "
                             'a guide may have listed it since, or its time is over')
            return play
    else:
        play.label = f'{play.label}: {game}'
        picked = game_sources(found.event.id, now, cfg, listings=found.listings)
        sources = picked.sources if picked is not None else []
        if not sources and not found.ended:
            play.why_none = (f'your guide lists {game} on {counted(len(found.channels), "channel")} now, but '
                             f'none of those listings is one Record would pick for it')
            return play
    blocked = blocked_account_ids_now()
    play.member, why, source = (_sports_member(sources, blocked, allowed, exclude_ids,
                                               live=True, viewer_id=viewer_id)
                                if sources else (None, None, None))
    if play.member is None and found.ended:
        # Every channel listing the game now failed or cannot be used: a channel whose listing of
        # it ended inside the window may still carry it running long, in Record's order over
        # those listings (dev/changelog/1745).
        later = game_sources(found.event.id, now, cfg, listings=found.ended)
        if later is not None and later.sources:
            member, later_why, _source = _sports_member(later.sources, blocked, allowed, exclude_ids,
                                                        live=True, viewer_id=viewer_id)
            if member is not None:
                play.member = member
                play.picked = (f'No channel whose guide lists {game} now could be used, so this is one '
                               f'whose listing of it has ended: the game may be running long.')
                return play
            if not sources:
                why = later_why
        elif not sources:
            play.why_none = (f'your guide lists {game} on {counted(len(found.channels), "channel")} now, but '
                             f'none of those listings, or of the ones that ended a little earlier, is one '
                             f'Record would pick for it')
            return play
    if play.member is None:
        play.why_none = sports_drop_words(why, game, named=found.named is not None)
    elif picked.why is not None:
        # The reason is the first choice's; a play that walked past it says that instead.
        play.picked = (picked.why.text if source is sources[0]
                       else 'Not the first choice: an earlier channel in the order could not be used.')
    return play


def resolve_game_play(event, league, feed_id: int, label: str, cfg: dict, exclude_ids=frozenset(),
                      held: bool = False, now: Optional[datetime] = None, viewer_id=None) -> Play:
    """What one game plays right now as a picture-in-picture channel's picture: what a sports
    channel carrying it would (dev/docs/DESIGN-pip-channels.md 5), over the same window, the
    same order between channels and the same walk inside a group. `held` is a play already on
    the game, which the clock never ends (dev/changelog/1685). `member` None with `found.channels`
    empty is a game not on here now; the caller says which side of its window that is."""
    from . import sports_feed
    fcfg = feed_config(cfg)
    before, after = sports_window(fcfg)
    now = now or datetime.utcnow()
    play = Play(feed_id, label, sports=True)
    found = sports_feed.playing_now(sports_feed.OneGame(league, event.id), now, event_id=event.id,
                                    before=before, after=after, held=held)
    return _play_found(play, found, allowed_account_ids(fcfg), exclude_ids, now, cfg, viewer_id)


def _viewer_standing(account_ids, viewer_id=None) -> dict:
    """connection_limits.seat_standing() for a play: which of `account_ids` it could take a seat
    on right now, its own seat (`viewer_id`, when it holds one) not counted against it."""
    return connlim.seat_standing(account_ids, 'viewer',
                                 exclude_holder=('viewer', viewer_id) if viewer_id else None)


def sports_drop_words(why: str, game: str, named: bool = False) -> str:
    """Why a sports play has no channel to take `game` from, for each _DROP_* _sports_member()
    answers. A clause, not a sentence: the play's refusal and "Channel it would use" each wrap it.
    `named` is a game no guide lists, played from the channels whose names mention it."""
    which = f'whose name mentions {game}' if named else f'whose guide lists {game}'
    return {
        _DROP_FAILED: f'every channel {which} already failed this play',
        _DROP_ALLOWED: (f'every channel {which} is on an account the IPTV '
                        f'output is not allowed to serve from'),
        _DROP_BLOCKED: f'every channel {which} is on a blocked account',
        _DROP_RECORDING: f'every channel group {which} has no channel with Recording on',
        _DROP_OTHER_GAME: (f'every channel group {which} has its channels with Recording on showing '
                           f'something else at that time'),
        _DROP_GONE: f'every channel or group {which} was removed while it was being worked out',
    }[why]


def sports_member_now(sources, blocked, allowed) -> tuple:
    """(the channel a sports channel's play would take right now from `sources` - game_sources()'
    order - or None, the _DROP_* that says why none, and the showing it came from - a group's when
    the channel is a group's member). The tune-in's own walk with nothing yet
    failed, so a page can ask "what would play" without a play (dev/changelog/1639). `allowed` None
    leaves the IPTV output's account allowlist out, which is the member a recording of a group
    would start on. Reads only."""
    return _sports_member(sources, blocked, allowed, frozenset())


def _sports_member(sources, blocked, allowed, exclude_ids, live: bool = False,
                   viewer_id=None) -> tuple:
    """(the first channel down `sources` - auto_rules._Showing in game_sources()' order - that
    this play may use, or None; the _DROP_* that says why none could, else ''; and the showing it
    came from, or None). A group counts once, at its first showing. A fixed handful of queries,
    whatever the length.

    `live` is a play's own pick: inside a group it prefers a member on an account with a
    connection free for the play (_viewer_standing), as a recording's start does. Off, a page
    asking what would play is told the member a recording would start on with every account
    free. The order between sources is game_sources()' either way - the order Record takes."""
    from .channel_groups import member_eager_options, serving_member
    from .database import Channel, ChannelGroup
    from .routes.channel_tests import _latest_tests_by_channel
    group_ids = sorted({s.group_id for s in sources if s.group_id is not None})
    groups = ({g.id: g for g in ChannelGroup.query.options(*member_eager_options())
               .filter(ChannelGroup.id.in_(group_ids))} if group_ids else {})
    single_ids = sorted({s.channel_id for s in sources if s.group_id is None})
    singles = {c.id: c for c in Channel.query.filter(Channel.id.in_(single_ids))} if single_ids else {}
    latest = _latest_tests_by_channel(sorted(set(singles) | {m.channel_id for g in groups.values()
                                                             for m in g.memberships}))
    standing = (_viewer_standing({m.channel.account_id for g in groups.values() for m in g.memberships
                                  if m.channel is not None}, viewer_id)
                if live and groups else None)
    drops, seen = set(), set()
    for s in sources:
        key = ('group', s.group_id) if s.group_id is not None else ('channel', s.channel_id)
        if key in seen:
            continue
        seen.add(key)
        if s.group_id is not None:
            group = groups.get(s.group_id)
            if group is None:
                drops.add(_DROP_GONE)
                continue
            game, carry = getattr(s, 'members', None), getattr(s, 'carriers', None)
            members, why = _members_for_feed(group, blocked, allowed, game=game)
            if not members:
                drops.add(why)
                continue
            # The members whose guide shows the game go first, then those that cannot be told
            # apart (sports_match.members_for_game(), dev/changelog/1690).
            carriers = [m for m in members if carry is not None and m.id in carry]
            member = None
            for tier in ([carriers, members] if carriers and len(carriers) < len(members) else [members]):
                member = serving_member(group, latest, blocked_account_ids=blocked, members=tier,
                                        exclude_ids=exclude_ids, seat_standing=standing).member
                if member is not None:
                    break
            if member is not None:
                return member, '', s
            drops.add(_DROP_FAILED)
            continue
        ch = singles.get(s.channel_id)
        if ch is None:
            drops.add(_DROP_GONE)
            continue
        if ch.account_id in blocked:
            drops.add(_DROP_BLOCKED)
        elif allowed is not None and ch.account_id not in allowed:
            drops.add(_DROP_ALLOWED)
        elif ch.id in exclude_ids:
            drops.add(_DROP_FAILED)
        else:
            return ch, '', s
    for why in (_DROP_FAILED, _DROP_ALLOWED, _DROP_BLOCKED, _DROP_OTHER_GAME, _DROP_RECORDING, _DROP_GONE):
        if why in drops:
            return None, why, None
    raise ValueError('_sports_member was handed no sources')


def resolve_play(feed_id: int, cfg=None, exclude_ids=frozenset(), event_id: int = None,
                 now: Optional[datetime] = None, viewer_id=None, preview: bool = False,
                 any_switch: bool = False) -> Optional[Play]:
    """The member `feed_id` plays right now, or None when the id names nothing served.
    A group is re-asked here on every tune-in and every failover (`exclude_ids` drops the
    members that already failed this play), so the IPTV player always gets the member a recording
    would start from (channel_groups.serving_member, the one spelling) - and, like a recording's
    start, one on an account with a connection free over one on a full account
    (connection_limits.seat_standing(), dev/changelog/1642). `viewer_id` is the play asking,
    whose own seat is not somebody else's. `event_id` holds a sports channel's failover to the
    game its play started on, which the clock never ends (dev/changelog/1685). `preview` resolves for a browser preview (Play.preview): a group the
    output does not offer - not in the TV Guide, or taken off the output - plays as its row would,
    so a group can be watched before it is offered, and an Up next channel plays switched off. A
    channel and a sports channel are asked exactly as for a player. `any_switch` plays a channel
    or a group in the TV Guide whether or not its On the output switch is on: a
    picture-in-picture channel's picture may be a guide row kept off the player's list
    (dev/docs/DESIGN-pip-channels.md 5). It never plays a row that left the guide."""
    from .channel_groups import blocked_account_ids_now, serving_member
    from .database import Channel, ChannelGroup
    from .routes.channel_tests import _latest_tests_by_channel
    from . import db

    if cfg is None:
        from .config import load_config
        cfg = load_config()
    fcfg = feed_config(cfg)
    allowed = allowed_account_ids(fcfg)
    kind, row_id = decode_feed_id(feed_id)
    if kind == KIND_PIP:
        from . import pip_channels
        return pip_channels.resolve_feed(feed_id, cfg, exclude_ids=exclude_ids, event_id=event_id,
                                         now=now, viewer_id=viewer_id, preview=preview)
    if kind == KIND_UP_NEXT:
        from .sports_up_next import channels
        found = channels(everything=preview, number=row_id)
        if not found:
            return None
        return Play(feed_id, f'{found[0].name} (Up next channel {row_id})', fault=False, up_next=found[0],
                    preview=preview)
    if kind == KIND_SPORTS:
        play = _resolve_sports_play(feed_id, row_id, allowed, exclude_ids, event_id, now,
                                    sports_window(fcfg), cfg, viewer_id)
        if play is not None:
            play.preview = preview
        return play
    if kind == KIND_CHANNEL:
        ch = db.session.get(Channel, row_id)
        if ch is None or not ch.in_guide:  # guide-flag-read-ok: a feed id for a channel is its own guide row and nothing else
            return None
        play = Play(feed_id, f'channel {ch.id} ({ch.name})', preview=preview)
        if not ch.on_output and not any_switch:
            play.why_none, play.fault = WHY_TAKEN_OFF, False
            return play
        if allowed is not None and ch.account_id not in allowed:
            play.why_none = 'its account is one the IPTV output is not allowed to serve from'
            return play
        if ch.id in exclude_ids:
            play.why_none = 'it already failed this play'
            return play
        play.member = ch
        return play
    group = db.session.get(ChannelGroup, row_id)
    if group is None or (not group.in_guide and not preview):
        return None
    play = Play(feed_id, f'group {group.id} ({group.name})', group=group, preview=preview)
    if not group.on_output and not preview and not any_switch:
        play.why_none, play.fault = WHY_TAKEN_OFF, False
        return play
    blocked = blocked_account_ids_now()
    members, why = _members_for_feed(group, blocked, allowed)
    if not members:
        play.why_none = _GROUP_WHY[why]
        return play
    latest = _latest_tests_by_channel([m.id for m in members])
    choice = serving_member(group, latest, blocked_account_ids=blocked, members=members,
                            exclude_ids=exclude_ids,
                            seat_standing=_viewer_standing({m.account_id for m in members}, viewer_id))
    if choice.member is None:
        play.why_none = 'every channel in the group already failed this play'
        return play
    play.member = choice.member
    return play


# ── The relay ──────────────────────────────────────────────────────────────────────────────

class Ring:
    """A fixed-size queue of arrival-stamped chunks between the reader thread and the one
    HTTP response. The reader appends; when the total passes `capacity` the oldest chunks
    are dropped and `dropped` counts the bytes, which is how an IPTV player that stopped reading is
    noticed (REASON_CLIENT_TOO_SLOW) rather than queued for. `take()` hands the consumer
    the oldest chunk once it has aged `reserve` seconds, so the viewer plays that far behind
    live and a gap shorter than it plays through (section 4.3).

    The reserve gives way to the ring, never the other way round: once the ring is fuller
    than `pressure`, the oldest chunk is released whatever its age, and `released_early`
    counts those bytes. A provider opens a connection with 13 to 29 seconds of the channel at
    once (dev/changelog/942, 997), and holding all of that for the reserve in a ring it does
    not fit lost the head of the stream before the viewer could be given any of it - every
    play of an HD channel ended at nothing sent (dev/docs/BUGS.md 2026-11-12). A viewer on
    less reserve than was asked for is still watching.

    `dropped_unripe` is the part of `dropped` that was evicted before it had aged the
    reserve: more arrived inside the reserve than the ring holds, faster than the viewer took
    what the pressure released (REASON_BUFFER_TOO_SMALL). The rest of `dropped` had been the
    viewer's to take for a while (REASON_CLIENT_TOO_SLOW)."""

    def __init__(self, capacity: int, reserve: float):
        self.capacity = max(int(capacity), READ_CHUNK)
        self.reserve = max(float(reserve), 0.0)
        self.pressure = int(self.capacity * RESERVE_RING_SHARE)
        self._chunks: deque = deque()
        self.size = 0
        self.dropped = 0
        self.dropped_unripe = 0
        self.released_early = 0
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
        """The oldest chunk once it is `reserve` old, or sooner while the ring is fuller
        than `pressure`; None after `timeout` with nothing releasable."""
        deadline = (time.monotonic() if now is None else now) + timeout
        with self._cv:
            while True:
                now = time.monotonic()
                if self._chunks:
                    arrived, data = self._chunks[0]
                    ripe = arrived + self.reserve <= now
                    if ripe or self.size > self.pressure:
                        self._chunks.popleft()
                        self.size -= len(data)
                        if not ripe:
                            self.released_early += len(data)
                        return data
                    wait = min(arrived + self.reserve - now, deadline - now)
                else:
                    wait = deadline - now
                if wait <= 0:
                    return None
                self._cv.wait(wait)

    def untaken_age(self, now: Optional[float] = None) -> float:
        """How long the oldest chunk has been releasable and still not taken - the one
        measure of an IPTV player that has stopped reading. Zero while nothing is releasable, so a
        provider that stopped sending never reads as an idle IPTV player."""
        now = time.monotonic() if now is None else now
        with self._cv:
            if not self._chunks:
                return 0.0
            return max(0.0, now - (self._chunks[0][0] + self.reserve))

    def wake(self) -> None:
        with self._cv:
            self._cv.notify_all()

    def mark(self) -> None:
        """Put SOURCE_MARK in the queue: the bytes after it come from another ffmpeg. It is
        zero-length, so it changes no count and is released in order with the bytes around it."""
        self.append(SOURCE_MARK)

    def set_reserve(self, reserve: float) -> None:
        """A new reserve from the next chunk on: a waiting screen plays on none, and the game
        it moves to on the configured one."""
        with self._cv:
            self.reserve = max(float(reserve), 0.0)
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
    label: str                       # what the IPTV player asked for: 'channel 5 (CNN)' or 'group 2 (...)'
    client: str                      # the IPTV player's address
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
    # {account_id: seconds} each account's ffmpeg ran for this play, and the one running now as
    # (proc, account_id, started_mono). Closed per ffmpeg, so a play that moved credits each
    # account its own part; a waiting screen or Up next picture opens none (_close_phase_locked).
    account_seconds: dict = field(default_factory=dict)
    _phase: Optional[tuple] = field(default=None, repr=False)
    # Members that died this play, so a failover never returns to one (section 4.5).
    failed_ids: set = field(default_factory=set)
    failovers: list = field(default_factory=list)    # [(from name, to name, why)]
    # Things that went less than right on this play and did not end it, each said once, as
    # sentences: Watching now and the play's line in Recent plays show them (_note_once).
    notes: list = field(default_factory=list)
    # Set by preempt_for_account() in the acquire-to-Popen window; the launch re-reads it
    # after Popen and never runs on a seat a recording already took.
    preempted: bool = False
    preempted_by: Optional[int] = None
    login_id: Optional[int] = None
    # The game a sports channel's play is, which a failover is held to; None for anything else.
    event_id: Optional[int] = None
    # Whether another channel can take over when the one playing dies: a group, a sports channel.
    fails_over: bool = False
    app: Optional[object] = field(default=None, repr=False)
    # A sports channel's waiting screen (dev/changelog/1534). The row it is drawn for, the clip
    # playing and its cache key, whether that clip is the quick still, what it shows
    # (sports_screen.STATE_*, and HELD_* when a game that is on cannot start), the game it waits
    # for, and when the play went to the game.
    sports_row_id: Optional[int] = None
    reserve_seconds: float = 0.0
    screen_path: str = ''
    screen_key: str = ''
    screen_still: bool = False
    screen_state: str = ''
    screen_held: Optional[str] = None
    waiting_for: str = ''
    left_screen_at: Optional[datetime] = None
    screen_seconds: float = 0.0
    # An Up next channel's number (sports_up_next.UpNext.number) on a play of one: its clips'
    # owner. None for anything else.
    up_next_number: Optional[int] = None
    # A picture-in-picture channel's row id on a play of one, whichever of its two feed ids it
    # asked for; None for anything else. `card_state` is the pip_channels.PANE_* its card shows
    # while it is on one (STATE_CARD) and `card_text` what Watching now says of it.
    pip_row_id: Optional[int] = None
    card_state: str = ''
    card_text: str = ''
    # Counted against max_viewers while the play takes a slot to leave the screen for the game.
    slot_claimed: bool = False
    # Ask again now, not at the next SCREEN_RECHECK_SECONDS (recheck_waiting, Start broadcast).
    recheck_due: bool = False
    # A browser preview's play (app/preview.py): a real play, counted and seated as any is, that
    # resolves with resolve_play(preview=True), relays in every mode because it cannot tune in
    # again, and raises no alert when refused - the preview names the refusal itself.
    preview: bool = False
    # The IPTV player's address when it names one player - flask.behind_proxy is on, so the
    # address is the player's and not a reverse proxy's - else ''. Only ever compared with
    # another play's, to tell the play a player is changing channel from (_take_over).
    player: str = ''
    # The feed id of the play this one ended when it took over from its player's last one
    # (_take_over), None if it never did.
    replaced_feed_id: Optional[int] = None
    # The IPTV output login the play came in on (FeedLogin.feed_login_id): None for the main
    # login, a browser preview and a picture of ChannelBin's own. Never read as the player's
    # address - two users behind one address are two logins - and never the seat holder id
    # some functions call `viewer_id` (dev/changelog/1760).
    feed_login_id: Optional[int] = None
    # That login's username as it was, for Watching now and Recent plays; '' for the main one.
    feed_login: str = ''
    last_recheck_mono: float = 0.0
    _reserve_short_logged: bool = False
    _finishing: bool = False
    _teardown_lock: threading.Lock = field(default_factory=threading.Lock, repr=False)
    _failing_over: bool = False
    # Between channels after a recording took its seat (preempt_for_account): no ffmpeg and no
    # seat anywhere until the move names its new account. Never a second meaning of _failing_over,
    # whose play still holds its seat.
    _moving: bool = False
    # Leaving a picture for its card because its picture-in-picture channel was cleared
    # (_to_card_now): no ffmpeg until the card's clip starts. Never a second meaning of
    # _failing_over, whose play is on its way to another channel.
    _carding: bool = False
    # One of a picture-in-picture row's two pictures (app/pip_engine.py): a session kept in
    # _internal and listed nowhere, never counted, whose `ring` is the picture's buffer and
    # whose `picture` is the pip_engine.Picture it plays for. Its seat, launch, stall
    # detection, failover and move for a recording are a plain play's.
    internal: bool = False
    picture: Optional[object] = field(default=None, repr=False)
    # A play of a picture-in-picture channel on its two pictures combined: the
    # pip_engine.Combined beside it. Its own ffmpeg is the combiner; it holds no seat and has
    # no channel or account of its own, its pictures hold those.
    combined: Optional[object] = field(default=None, repr=False)
    # Why a play of a set-up picture-in-picture channel plays its big picture alone, a
    # sentence; '' while it does not.
    combine_off: str = ''
    # Held by whatever swaps what the play is playing: one swap at a time.
    _swap_lock: threading.Lock = field(default_factory=threading.Lock, repr=False)
    _rechecking: bool = False
    # When a play that left a picture for its card went onto it (monotonic), 0.0 otherwise: a
    # play that started on its card or a waiting screen counts from `started_mono`.
    _screen_since: float = 0.0

    @property
    def live(self) -> bool:
        return self.state != STATE_STOPPED

    @property
    def waiting(self) -> bool:
        return self.state == STATE_WAITING

    @property
    def showing(self) -> bool:
        return self.state == STATE_SHOWING

    @property
    def on_card(self) -> bool:
        return self.state == STATE_CARD

    @property
    def own_picture(self) -> bool:
        """Playing ChannelBin's own clip, a waiting screen, an Up next channel or a
        picture-in-picture channel's card: no provider bytes, so a play ChannelBin streams in
        every mode."""
        return self.state in OWN_PICTURE_STATES

    @property
    def own_picture_name(self) -> str:
        """What ChannelBin's own clip is called on this play: a sports channel's waiting screen,
        an Up next picture or a picture-in-picture card. Read from what kind of play it is,
        which never changes, and not from its state, which another thread may move between a
        caller's check and its read. '' for a play that has no clip of ChannelBin's own."""
        if self.up_next_number is not None:
            return PICTURE_NAME
        if self.pip_row_id is not None:
            return CARD_NAME
        if self.sports_row_id is not None:
            return SCREEN_NAME
        return ''

    @property
    def counted(self) -> bool:
        """Whether the play counts against max_viewers: every live one but a play of ChannelBin's
        own picture, unless it is taking a slot to leave a waiting screen for its game."""
        return self.live and not self.internal and (not self.own_picture or self.slot_claimed)

    def reason_text(self) -> str:
        if not self.reason:
            return ''
        if self.event_id is not None and self.reason in SPORTS_REASON_TEXT:
            return SPORTS_REASON_TEXT[self.reason]
        return REASON_TEXT.get(self.reason, '')

    def to_dict(self) -> dict:
        now = time.monotonic()
        return {
            'session_id': self.id, 'feed_id': self.feed_id, 'label': self.label,
            'client': self.client, 'feed_login': self.feed_login,
            'group_id': self.group_id, 'channel_id': self.channel_id,
            'event_id': self.event_id,
            'channel_name': self.channel_name, 'account_id': self.account_id,
            'state': self.state, 'reason': self.reason, 'reason_text': self.reason_text(),
            'detail': self.detail, 'bytes_sent': self.bytes_sent,
            'elapsed_seconds': round(now - self.started_mono, 1) if self.started_mono else 0,
            'started_at': self.started_at, 'ended_at': self.ended_at,
            'failovers': list(self.failovers),
            'waiting': self.waiting, 'waiting_for': self.waiting_for, 'showing': self.showing,
            'on_card': self.on_card, 'card_state': self.card_state, 'card_text': self.card_text,
            'screen_state': self.screen_state, 'screen_held': self.screen_held,
            'left_screen_at': self.left_screen_at,
            'screen_seconds': round(self._screen_seconds(now), 1),
            'move_offered': move_offer(self) == MOVE_OFFERED,
            'move_why': {MOVE_NOWHERE: MOVE_NOWHERE_TEXT,
                         MOVE_TWO_PICTURES: MOVE_TWO_PICTURES_TEXT}.get(move_offer(self), ''),
        }

    def _screen_seconds(self, now: float) -> float:
        """How long the play has been on the waiting screen, or on its card over every time it
        went onto it, up to `now`."""
        if self.state == STATE_CARD and self._screen_since:
            return self.screen_seconds + max(0.0, now - self._screen_since)
        if self.state == STATE_WAITING and self.started_mono:
            return max(0.0, now - self.started_mono)
        return self.screen_seconds


# What Watching now offers a play for "Move to another channel" (move_by_hand, dev/changelog/1675).
MOVE_OFFERED = 'offered'          # a group or a sports channel playing a channel: there is somewhere to go
MOVE_NOWHERE = 'nowhere'          # a single channel: nothing else plays it, and the menu says so
MOVE_NO_CHANNEL = 'no_channel'    # ChannelBin's own picture, or ended: no channel to move off
MOVE_TWO_PICTURES = 'two_pictures'    # two pictures combined: moving one by hand is not offered
MOVE_NOWHERE_TEXT = 'A single channel, so there is nowhere to move it'
MOVE_TWO_PICTURES_TEXT = 'It plays two pictures, and each moves to another channel by itself when it drops'


def move_offer(session: 'RelaySession') -> str:
    """One of the MOVE_* answers for `session` as it stands. Whether a move can start this
    instant (another swap in flight) is move_by_hand()'s to say."""
    if not session.live or session.own_picture:
        return MOVE_NO_CHANNEL
    if session.combined is not None:
        # Its own ffmpeg is the combiner, not a channel's: a move would take that
        # (dev/docs/DESIGN-pip-channels.md 15).
        return MOVE_TWO_PICTURES
    if session.state not in (STATE_STARTING, STATE_STREAMING):
        raise ValueError(f'Unknown play state "{session.state}"')
    return MOVE_OFFERED if session.fails_over else MOVE_NOWHERE


_lock = threading.Lock()
_sessions: dict = {}          # session id -> RelaySession, live ones and the last few ended
#: The picture-in-picture pictures' sessions (RelaySession.internal), live ones only: seated,
#: reaped, preempted and stopped as plays are, listed and counted nowhere.
_internal: dict = {}
_redirects: list = []         # the last _KEEP_ENDED redirect-mode plays, oldest first
_refused: dict = {}           # (feed id, message, login) -> the last _KEEP_ENDED refused plays
#: Login id -> its tune-ins between their stream-limit check and their play's registration
#: (_LoginRoom), counted with its live plays so two at once cannot both pass the check.
_login_starting: dict = {}
#: Login id -> how many times stop_login() has run for it (FeedLogin.generation).
_login_changes: dict = {}
_reaper: Optional[threading.Thread] = None
_play_queue: list = []        # lines of Recent plays waiting for the writer thread
_log_writer: Optional[threading.Thread] = None
_log_kick = threading.Event()   # ends the writer's wait early (discard_play_log)
_last_prune_mono: Optional[float] = None


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


def recent_refused() -> list:
    """The refused plays, newest first: what was asked for, why it was refused, when it last
    was and how many times. An IPTV player retries a refused play every half minute, so a
    standing reason is one row that counts, not a row per try."""
    with _lock:
        return sorted(_refused.values(), key=lambda r: r['at'], reverse=True)


def _note_refused(feed_id: int, label: str, client: str, message: str) -> None:
    login = _tuning_log_name()
    with _lock:
        row = _refused.get((feed_id, message, login))
        if row is None:
            row = _refused[(feed_id, message, login)] = {
                'feed_id': feed_id, 'label': label, 'message': message, 'count': 0,
                'feed_login': login or '', 'first_at': datetime.utcnow()}
        row.update(at=datetime.utcnow(), client=client, count=row['count'] + 1)
        for key in sorted(_refused, key=lambda k: _refused[k]['at'])[:-_KEEP_ENDED]:
            del _refused[key]
    _queue_play(None, {'kind': FEED_PLAY_REFUSED, 'at': datetime.utcnow(), 'feed_id': feed_id,
                       'label': label, 'client': client, 'message': message, 'feed_login': login})


def _refuse_nothing_on(play: Play, client: str):
    """Refuse a play that has nothing to start and is not a fault - a sports channel between
    games, a row a person took off the output: named in the refused plays and the log, and
    no alert."""
    message = f'Nothing to play for {play.label}: {play.why_none}.'
    _note_refused(play.feed_id, play.label, client, message)
    log.info('Viewer from %s refused: %s', client, message)
    raise FeedRefused(message, 409)


def get_session(session_id: str) -> Optional[RelaySession]:
    with _lock:
        return _sessions.get(session_id)


def _prune_ended_locked():
    ended = [s for s in _sessions.values() if not s.live]
    if len(ended) > _KEEP_ENDED:
        ended.sort(key=lambda s: s.ended_at or s.started_at)
        for s in ended[:-_KEEP_ENDED]:
            _sessions.pop(s.id, None)


def _refuse(feed_id: int, label: str, message: str, status: int, source_tail: str,
            client: str = '', alert: bool = True):
    """Raise FeedRefused after an INFO alert, once per standing condition: the measured
    client reconnects every 27 s, so one open alert per feed id and cause, not one per try.
    The refusal is also kept in the refused plays. A browser preview passes `alert=False`: it
    shows the refusal to the person who asked, so an alert would say it a second time."""
    from .alerts import create_alert, has_open_alert
    _note_refused(feed_id, label, client, message)
    if not alert:
        raise FeedRefused(message, status)
    source = f'feed:{feed_id}:{source_tail}'
    alert_type = 'FEED_NO_MEMBER' if source_tail == 'no-member' else 'VIEWER_REFUSED'
    if not has_open_alert(alert_type, source):
        title = (f'IPTV output: nothing to play for {label}' if alert_type == 'FEED_NO_MEMBER'
                 else f'IPTV output: an IPTV player was refused {label}')
        create_alert(alert_type, title, body=message, source=source)
    raise FeedRefused(message, status)


def start_session(feed_id: int, client: str, cfg=None, preview: bool = False,
                  player: str = '', login: Optional[FeedLogin] = None) -> RelaySession:
    """Start relaying `feed_id` to one IPTV player. Raises FeedRefused with a user-facing message:
    404 for an id the feed does not serve, 503 at the viewer cap or the login's own stream
    limit, 409 when nothing is playable (a blocked account, the allowlist, the connection
    limit) and 500 when ffmpeg could not start. Requires an app context; the returned session
    is registered and reaped. `preview` is a browser preview's play (RelaySession.preview), in
    every mode. `player` is RelaySession.player: the address when it names one IPTV player,
    else ''. `login` is the login the player came in on, None for the main one.
    """
    room = _LoginRoom(login) if login is not None and not login.is_main else None
    token = _tuning_login.set(login)
    try:
        session = _start_session(feed_id, client, cfg, preview, player, login, room)
        gone = False
        with _lock:
            if player and not session.player:
                # A waiting screen, card or Up next picture: built without it, and the play a
                # later channel change from this player leaves all the same.
                session.player = player
            if room is not None:
                if session.feed_login_id is None:
                    session.feed_login_id, session.feed_login = login.feed_login_id, login.username
                if session.replaced_feed_id is None:
                    session.replaced_feed_id = room.replaced_feed_id
                gone = not room.still_good_locked()
        if gone:
            # The login was turned off, changed or deleted while this play was starting.
            _finish(session, REASON_LOGIN_OFF)
            raise FeedRefused(AUTH_FAILED_MESSAGE, 401)
    finally:
        # After the play carries its login, so its count never dips between the two.
        if room is not None:
            room.release()
        _tuning_login.reset(token)
    return session


class _LoginRoom:
    """One tune-in's place under its login's stream limit (FeedLogin.max_streams), taken under
    the one lock with the count of the login's live plays and given back once the play is
    registered or refused - the check-then-act shape .claude/rules/background-jobs.md names,
    closed the way _claim_slot closes it for the output's own cap."""

    def __init__(self, login: FeedLogin):
        self.login = login
        self.held = False
        # The feed id of the play this tune-in ended to take its place, for the new play's
        # replaced_feed_id: the guard _leaving_play_of reads against two players behind one
        # address cutting each other on every retry (dev/changelog/1744).
        self.replaced_feed_id: Optional[int] = None

    def still_good_locked(self) -> bool:
        """Whether the login is still what the gate read: no stop_login() has run for it since.
        Under _lock, at the moment the play takes the login's name, which is the moment
        stop_login() could start finding it."""
        return _login_changes.get(self.login.feed_login_id, 0) == self.login.generation

    def claim(self, play: 'Play', client: str, player: str, alert: bool) -> None:
        """Take the place, or raise FeedRefused (503) naming the login's limit. A full limit
        is asked again until SWITCH_GRACE_SECONDS, after ending the play this player is
        changing channel from when it can be told (_leaving_play_of), as the output's own cap
        is: an IPTV player asks for the new channel before it closes the old one."""
        login_id, most = self.login.feed_login_id, self.login.max_streams
        deadline = time.monotonic() + SWITCH_GRACE_SECONDS
        asked = False
        while True:
            with _lock:
                if len(_login_plays_locked(login_id)) + _login_starting.get(login_id, 0) < most:
                    _login_starting[login_id] = _login_starting.get(login_id, 0) + 1
                    self.held = True
                    return
            if time.monotonic() >= deadline:
                break
            if not asked:
                asked = True
                # Every live play counts against the limit, a waiting screen's included, so any
                # of them is the one a player can be leaving.
                old = _leaving_play_of(player, login_id, play.feed_id, counted_only=False)
                if old is not None and _finish(old, REASON_REPLACED):
                    self.replaced_feed_id = old.feed_id
                    log.info('Viewer from %s: %s ended %s, the play the same IPTV player on login '
                             '%s is changing channel from', client, play.label, old.id,
                             self.login.username)
                    continue
            time.sleep(_SWITCH_POLL)
        _refuse(play.feed_id, play.label,
                f'The IPTV output login "{self.login.username}" is already playing as many streams '
                f'as it may at once ({counted(most, "stream")}). Stop one on that login\'s IPTV player, '
                f'or raise its limit under Logins for other users on the IPTV Output page.',
                503, f'login-{login_id}', client, alert=alert)

    def release(self) -> None:
        if not self.held:
            return
        with _lock:
            left = _login_starting.get(self.login.feed_login_id, 0) - 1
            if left > 0:
                _login_starting[self.login.feed_login_id] = left
            else:
                _login_starting.pop(self.login.feed_login_id, None)
        self.held = False


def _login_plays_locked(login_id: int) -> list:
    return [s for s in _sessions.values()
            if s.live and not s.internal and not s.preview and s.feed_login_id == login_id]


def login_plays(login_id: int) -> list:
    """The live plays that came in on login `login_id`: what its stream limit counts and what
    player_api.php reports as its active connections."""
    with _lock:
        return _login_plays_locked(login_id)


def stop_login(login_id: int, reason: str = REASON_LOGIN_OFF) -> int:
    """End every live play that came in on login `login_id`, by name. How many it ended.
    Called after the commit that turned the login off, changed it or deleted it, never
    inside it. The count it moves, in the same lock as the list it takes, refuses a play the
    gate let in before the commit and that registers after this list (_LoginRoom)."""
    with _lock:
        _login_changes[login_id] = _login_changes.get(login_id, 0) + 1
        live = _login_plays_locked(login_id)
    return sum(1 for s in live if _finish(s, reason))


def _start_session(feed_id: int, client: str, cfg, preview: bool, player: str,
                   login: Optional[FeedLogin] = None, room: Optional[_LoginRoom] = None) -> RelaySession:
    from flask import current_app
    from .config import load_config

    cfg = cfg or load_config()
    fcfg = feed_config(cfg)
    play = resolve_play(feed_id, cfg, preview=preview)
    if play is None:
        raise FeedRefused('No such channel on the IPTV output', 404)
    if room is not None:
        room.claim(play, client, player, alert=not preview)
    login_id = login.feed_login_id if login is not None else None
    login_name = login.username if login is not None and not login.is_main else ''
    if play.up_next is not None:
        return start_showing(play, client, cfg)
    combine_off = ''
    if play.pip is not None and play.pip.set_up:
        device, combine_off = _pip_engine().gpu_state(cfg)
        if device:
            session = _start_combined(play, client, cfg, device, player, login_id, login_name)
            if session is not None:
                return session
            # Neither picture can play right now, or no slot is free: the card says which.
            held = None
            if play.member is not None:
                with _lock:
                    full = sum(1 for s in _sessions.values() if s.counted) >= int(fcfg.get('max_viewers') or 1)
                held = _screen_mod().HELD_NO_SLOT if full else _screen_mod().HELD_NO_CONNECTION
            return start_card(play, client, cfg, held=held)
    if play.pip is not None and play.member is None:
        return start_card(play, client, cfg)
    if play.sports and play.member is None and not play.fault:
        return start_waiting(play, client, cfg)
    if play.member is None and not play.fault:
        _refuse_nothing_on(play, client)
    if play.member is None:
        _refuse(feed_id, play.label, f'Nothing to play for {play.label}: {play.why_none}.',
                409, 'no-member', client, alert=not preview)

    member = play.member
    cap = int(fcfg.get('max_viewers') or 1)
    session = RelaySession(
        id=secrets.token_urlsafe(12), feed_id=feed_id, label=play.label, client=client,
        group_id=play.group.id if play.group is not None else None,
        channel_id=member.id, channel_name=member.name, account_id=member.account_id,
        ring=Ring(int(fcfg.get('buffer_mb') or 64) * 1024 * 1024,
                  float(fcfg.get('reserve_seconds') or 0)),
        **_session_limits(cfg, fcfg),
        started_mono=time.monotonic(),
        event_id=play.event_id,
        fails_over=play.group is not None or play.sports or play.pip is not None,
        app=current_app._get_current_object(), preview=preview,
        pip_row_id=play.pip.row_id if play.pip is not None else None,
        # A set-up picture-in-picture channel reaching here plays its big picture alone, a
        # plain stream copy on one connection: nothing can combine the two (section 7.3).
        combine_off=combine_off, player=player, feed_login_id=login_id, feed_login=login_name,
    )
    deadline = time.monotonic() + SWITCH_GRACE_SECONDS
    if not _claim_slot(session, cap, deadline):
        if play.sports:
            # A game that is on and cannot start here yet waits on the screen, which says
            # why and starts the game by itself when a slot is free.
            return start_waiting(play, client, cfg, held=_screen_mod().HELD_NO_SLOT)
        if play.pip is not None:
            # As a sports channel does: the card says why, and the picture comes on when a
            # slot frees.
            return start_card(play, client, cfg, held=_screen_mod().HELD_NO_SLOT)
        _refuse(feed_id, play.label,
                f'The IPTV output is already playing to as many IPTV players as it allows at once '
                f'({cap}). Stop one, or raise "IPTV players at once" in the IPTV output\'s settings.',
                503, 'cap', client, alert=not preview)
    try:
        _launch(session, member, cfg, first=True, seat_deadline=deadline,
                hold=play.sports or play.pip is not None)
    except _SeatHeld:
        with _lock:
            _sessions.pop(session.id, None)
        if play.pip is not None:
            # Every account its picture plays from is busy: the card says so, and the picture
            # comes on by itself (dev/docs/DESIGN-pip-channels.md 8.2).
            return start_card(play, client, cfg, held=_screen_mod().HELD_NO_CONNECTION)
        return start_waiting(play, client, cfg, held=_screen_mod().HELD_NO_CONNECTION)
    _ensure_reaper()
    log.info('Viewer %s from %s%s: %s playing channel %d (%s) on account %d%s%s', session.id,
             client, f' on login {session.feed_login}' if session.feed_login else '', session.label, session.channel_id, session.channel_name, session.account_id,
             f' ({play.note})' if play.note else '', f' Why this channel: {play.picked}' if play.picked else '')
    return session


def _claim_slot(session: RelaySession, cap: int, deadline: float) -> bool:
    """Register `session` under the viewer cap. True once it is registered; False, with nothing
    registered, when the cap is still full at `deadline`.

    The cap is counted and the session registered under the ONE lock, so two tune-ins in the
    same instant cannot both pass the count (the check-then-act shape
    .claude/rules/background-jobs.md names). Registered BEFORE the seat is taken, so a
    recording preempting in the window between acquire and Popen finds something to flag. A
    play on a waiting screen is not counted: it holds no seat and plays ChannelBin's own clip
    (dev/changelog/1534). A full cap is asked again until `deadline` (SWITCH_GRACE_SECONDS),
    after the play this player is changing channel from is ended, when it can be told
    (_take_over)."""
    asked = False
    while True:
        with _lock:
            live = sum(1 for s in _sessions.values() if s.counted)
            if live < cap:
                _sessions[session.id] = session
                _prune_ended_locked()
                return True
        if time.monotonic() >= deadline:
            return False
        if not asked:
            asked = True
            if _take_over(session):
                continue
        time.sleep(_SWITCH_POLL)


def _leaving_play(session: RelaySession) -> Optional[RelaySession]:
    """The play `session`'s IPTV player is changing channel from: its one other live play on
    the output. None when the player cannot be told from others (RelaySession.player is ''), it
    has no other play, or more than one (a multi-view TV, and which it is leaving cannot be
    told), when that play is not counted against the cap, or when `session` asks for the very
    channel that play took over from. That last is what a second player behind one address
    does when it tunes back in to the channel it was cut from, and ending the play that cut it
    would start the two cutting each other on every retry; a person flipping back to the last
    channel waits as any tune-in did before (dev/changelog/1744). Only a play on the same login
    is the same player's: two users behind one address are two logins (dev/changelog/1760)."""
    if session.preview or session.internal:
        return None
    return _leaving_play_of(session.player, session.feed_login_id, session.feed_id, exclude=session)


def _leaving_play_of(player: str, login_id: Optional[int], feed_id: int,
                     exclude: Optional[RelaySession] = None,
                     counted_only: bool = True) -> Optional[RelaySession]:
    """_leaving_play for a tune-in that may not have a session yet: the one other live play of
    `player` on login `login_id`, under the same conditions. `counted_only` False also hands
    over a play the output's cap does not count (a waiting screen, a card, an Up next
    picture), for a login's own limit, which counts every play."""
    if not player:
        return None
    with _lock:
        mine = [s for s in _sessions.values()
                if s is not exclude and s.live and not s.internal and not s.preview
                and s.player == player and s.feed_login_id == login_id]
    if len(mine) != 1 or (counted_only and not mine[0].counted):
        return None
    old = mine[0]
    if old.replaced_feed_id is not None and old.replaced_feed_id == feed_id:
        return None
    return old


def _take_over(session: RelaySession, account_id: Optional[int] = None) -> bool:
    """End the play `session`'s IPTV player is changing channel from (_leaving_play) so the new
    one can start now: an IPTV player asks for the new channel before it closes the old one,
    and prod measured the old one going 2 to 27 s later (dev/docs/BUGS.md 2026-11-12,
    dev/changelog/1744). With `account_id`, only when the old play's seat is the one that
    account needs. True when it ended one."""
    old = _leaving_play(session)
    if old is None:
        return False
    if account_id is not None:
        standing = connlim.seat_standing([account_id], 'viewer', exclude_holder=('viewer', old.id))
        if standing.get(account_id) != connlim.SEAT_FREE:
            return False
    if not _finish(old, REASON_REPLACED):
        return False
    with _lock:
        session.replaced_feed_id = old.feed_id
    log.info('Viewer %s from %s: %s ended %s, the play the same IPTV player is changing channel from',
             session.id, session.client, session.label, old.id)
    return True


def _start_combined(play: Play, client: str, cfg: dict, device: str, player: str = '',
                    login_id: Optional[int] = None, login: str = '') -> Optional[RelaySession]:
    """Start `play`, a set-up picture-in-picture channel's, on its two pictures combined
    (app/pip_engine.py). The session holds no seat and names no channel: its pictures do. None,
    with nothing registered or held, when no slot is free or neither picture can play right
    now; the caller starts the channel's card. Requires an app context."""
    from flask import current_app
    fcfg = feed_config(cfg)
    tuned = play.pip
    session = RelaySession(
        id=secrets.token_urlsafe(12), feed_id=play.feed_id,
        label=f'picture-in-picture channel {tuned.row_id} ({tuned.name})', client=client,
        group_id=None, channel_id=None, channel_name='', account_id=None,
        ring=Ring(int(fcfg.get('buffer_mb') or 64) * 1024 * 1024, float(fcfg.get('reserve_seconds') or 0)),
        **_session_limits(cfg, fcfg), started_mono=time.monotonic(), fails_over=True,
        app=current_app._get_current_object(), preview=play.preview, pip_row_id=tuned.row_id,
        player=player, feed_login_id=login_id, feed_login=login)
    cap = int(fcfg.get('max_viewers') or 1)
    if not _claim_slot(session, cap, time.monotonic() + SWITCH_GRACE_SECONDS):
        return None
    try:
        began = _pip_engine().begin(session, tuned, cfg, device)
    except Exception:
        with _lock:
            _sessions.pop(session.id, None)
        raise
    if not began:
        with _lock:
            _sessions.pop(session.id, None)
        return None
    log.info('Viewer %s from %s: %s playing its two pictures combined', session.id, client, session.label)
    return session


def play_alone(session: RelaySession) -> None:
    """Put a picture-in-picture play whose two pictures cannot be combined on its big picture
    alone, in place, as a plain stream copy on one connection, or on its card when that has no
    connection either (dev/docs/DESIGN-pip-channels.md 7.3). The caller has stopped its ffmpeg
    and let go of its pictures, and holds `_swap_lock`. Requires an app context."""
    from .config import load_config
    cfg = load_config()
    play = resolve_play(session.feed_id, cfg, viewer_id=session.id, preview=session.preview)
    if play is None or play.pip is None:
        _finish(session, REASON_TAKEN_OFF)
        return
    member = _seat_for(session, play.member, play.event_id, cfg) if play.member is not None else None
    if member is None or not _own_seat(session, member):
        if session.live:
            _put_on_card(session, play, cfg,
                         held=_screen_mod().HELD_NO_CONNECTION if play.member is not None else None)
        return
    came_from = session.left_screen_at, session.screen_seconds
    _put_on_game(session, play, member, cfg)
    with _lock:
        # It left no card: _put_on_game's "came from its card" does not apply.
        session.left_screen_at, session.screen_seconds = came_from


def _session_limits(cfg: dict, fcfg: dict) -> dict:
    """The RelaySession fields every play takes from the settings."""
    return {'idle_timeout': float(fcfg.get('idle_seconds') or 30),
            'max_seconds': float(fcfg.get('max_session_hours') or 12) * 3600,
            'stall_timeout': float((cfg.get('watchdog') or {}).get('stall_timeout_seconds') or 30),
            'reserve_seconds': float(fcfg.get('reserve_seconds') or 0)}


class _SeatHeld(Exception):
    """A sports play's first seat was refused: the play waits on the screen instead of being
    refused. Raised by _launch(hold=True) with nothing to tear down."""


def _take_seat(session: RelaySession, member, first: bool, deadline: float = 0.0) -> bool:
    """A seat for `member`'s account: a fresh one on the first play, the seat already held
    on a failover within the account, or that seat moved across accounts in one step
    (connection_limits.move_seat - never release-then-acquire, which can leave the session
    holding nothing). A first play refused the seat asks again until `deadline` while another
    play of the output is live: that is the channel an IPTV player is changing from, about
    to let its seat go (SWITCH_GRACE_SECONDS), and first ends it when it is this player's own
    and holds the seat needed (_take_over). With no other play the refusal is at once."""
    if first:
        # A picture's own plays are not a channel being left: they are who it plays for.
        mine = session.picture.pair.plays if session.picture is not None else ()
        asked = False
        while True:
            if connlim.try_acquire(member.account_id, 'viewer', session.id):
                return True
            # A picture-in-picture picture no play is on any more gives its seat up the moment
            # anything asks (dev/docs/DESIGN-pip-channels.md 9).
            if _pip_engine().drop_lingering() and connlim.try_acquire(member.account_id, 'viewer', session.id):
                return True
            if not asked:
                asked = True
                if _take_over(session, member.account_id):
                    continue
            # Any other play, not only one on this account: two accounts can be one login
            # and so one seat (app/account_links.py).
            with _lock:
                leaving = any(s.counted and s is not session and s.id not in mine
                              for s in _sessions.values())
            if not leaving or time.monotonic() >= deadline:
                return False
            time.sleep(_SWITCH_POLL)
    if member.account_id == session.account_id:
        return True
    return connlim.move_seat('viewer', session.id, session.account_id, member.account_id)


def _launch(session: RelaySession, member, cfg, first: bool = True,
            seat_deadline: float = 0.0, hold: bool = False) -> None:
    """Take the seat and start ffmpeg on `member`, wiring its stdout to the ring through a
    reader thread. Raises FeedRefused; on the first play the session is finished first and
    the refusal alert raised, on a failover the caller decides what the refusal means. With
    `hold`, a first play refused its seat raises _SeatHeld and touches nothing.

    A first play refused its seat on `member` asks every other channel that could play it, in
    rank order, before it is refused or held (dev/changelog/1585): the next one down may be on
    an account with a connection free. Only what this play starts on changes; a recording's
    pick is channel_groups' and is not asked here."""
    from .account_blocks import blocked_reason
    from .account_links import render_held_login
    from .config import load_config
    from .recorder import resolve_capture_pacing

    walked = False
    got = _take_seat(session, member, first, seat_deadline)
    if not got:
        other = _seat_for(session, member, session.event_id, cfg, refused=(member,)) if first else None
        if other is not None:
            busy = member.account.name if member.account else f'account {member.account_id}'
            # The same account can answer yes a moment later, so the account is blamed only
            # when the play really starts on another one.
            why = (f'{busy}, which "{member.name}" is on, had no connection free'
                   if other.account_id != member.account_id
                   else f'no connection was free for "{member.name}" when it was asked')
            _note_once(session, f'Started on "{other.name}": {why}.')
            log.info('Viewer %s: no connection free on account %d for "%s", so %s starts on "%s" '
                     'on account %d', session.id, member.account_id, member.name, session.label,
                     other.name, other.account_id)
            member, walked, got = other, True, True
    if not got:
        if first and hold:
            raise _SeatHeld()
        account_name = member.account.name if member.account else f'account {member.account_id}'
        from .account_links import unusable_reason
        blocked = (unusable_reason(member.account_id, account_name, capital=True)
                   or blocked_reason(member.account_id, account_name, capital=True))
        if blocked:
            detail = f'{blocked}.'
        else:
            who = connlim.describe_holders(member.account_id)
            detail = (f'{account_name} is at its connection limit'
                      + (f' ({who} is using it)' if who else '') + '.')
        if first:
            _finish(session, REASON_LAUNCH_FAILED, detail=detail)
            _refuse(session.feed_id, session.label, detail, 409, 'seat', session.client,
                    alert=not session.preview)
        raise FeedRefused(detail, 409)

    # The account is named and the seat confirmed in one hold of the lock: a recording finds
    # the plays it preempts by account (preempt_for_account), so a seat it stripped before the
    # account was named here would otherwise go unnoticed and the play would relay on a
    # connection it does not hold (dev/docs/BUGS.md 2026-11-15 01:36 PM). A first play is
    # registered under its account before its seat is asked for and needs no second look.
    with _lock:
        session.channel_id = member.id
        session.channel_name = member.name
        session.account_id = member.account_id
        seated = (first and not walked) or session.id in connlim.seated_holder_ids('viewer')
        ending = walked and (not session.live or session._finishing)
        if walked and seated and not ending:
            # A recording that started on the account the play was registered under flagged
            # it while it held no seat there (preempt_for_account matches by account). The
            # seat it holds now is on another account, where nothing has asked for it.
            session.preempted, session.preempted_by = False, None
    if ending:
        # Stopped while the walk was asking (shutdown, its row taken off): the stop released
        # whatever the walk had taken, and the play is neither held nor preempted.
        connlim.release(member.account_id, 'viewer', session.id)
        raise FeedRefused(session.reason_text() or REASON_TEXT[REASON_STOPPED], 409)
    if not seated and first and hold:
        # A recording took the seat the walk found before the play was named on its account:
        # nothing is held, and a sports play waits on the screen as it does for any seat.
        raise _SeatHeld()
    if not seated:
        _finish(session, REASON_PREEMPTED,
                detail=f'a recording took the connection as the play was moving to "{member.name}"')
        raise FeedRefused(REASON_TEXT[REASON_PREEMPTED], 409)
    launch_url, session.login_id = render_held_login(member.stream_url, member.account_id,
                                                     'viewer', session.id)
    cfg = cfg or load_config()
    # Asked of the member being launched, never carried over from the play's first one: a
    # failover and a game put on from the waiting screen get that channel's own answer.
    session.pace_realtime, _source = resolve_capture_pacing(cfg, member.pace_realtime, 0)
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

    reader = _adopt(session, proc, account_id=member.account_id)
    if reader is None:
        # The seat taken above is this play's whatever _finish() released before it.
        connlim.release(member.account_id, 'viewer', session.id)
        if session.preempted:
            _finish(session, REASON_PREEMPTED)
            raise FeedRefused(REASON_TEXT[REASON_PREEMPTED], 409)
        raise FeedRefused(REASON_TEXT[REASON_CLIENT_GONE], 409)


def _close_phase_locked(session: RelaySession, proc: Optional[subprocess.Popen] = None) -> None:
    """Credit the provider ffmpeg the play is timing to its account, and stop timing it. With
    `proc`, only when that is the ffmpeg being timed: a swapped-out ffmpeg's reader can drain
    after the next one was adopted, and must not close the new one's time. Caller holds _lock."""
    phase = session._phase
    if phase is None or (proc is not None and phase[0] is not proc):
        return
    session._phase = None
    _proc, account_id, started = phase
    session.account_seconds[account_id] = (session.account_seconds.get(account_id, 0.0)
                                           + max(0.0, time.monotonic() - started))


def _adopt(session: RelaySession, proc: subprocess.Popen,
           account_id: Optional[int] = None) -> Optional[threading.Thread]:
    """Make a just-started ffmpeg the session's and start its reader, or kill it and return
    None. The session may have ended while ffmpeg was launching: a recording took the seat, or
    the IPTV player closed the response and _finish() is tearing down or has torn down. A
    session _finish() has claimed is still live until its teardown is done, so the claim is
    what is tested: an ffmpeg adopted after _finish() read the session's process would be one
    nothing ever kills (dev/docs/BUGS.md 2026-11-13).

    `account_id` is the account a provider ffmpeg plays from, and its run is timed against it;
    ChannelBin's own clip passes None and is timed against nothing."""
    reader = threading.Thread(target=_read_loop, args=(session, proc), daemon=True,
                              name=f'relay-{session.id}')
    with _lock:
        adopt = session.live and not session._finishing and not session.preempted
        if adopt:
            session.proc = proc
            session.reader = reader
            session.launched_mono = time.monotonic()
            _close_phase_locked(session)
            if account_id is not None:
                session._phase = (proc, account_id, session.launched_mono)
            # Before the reader starts, and after the last one was joined (every swap stops and
            # drains the old ffmpeg first), so the mark sits exactly between the two sources.
            session.ring.mark()
            # Started before the lock goes: a reader _finish() or a swap can read is one it
            # can join. Joining one not yet started raises, and a _finish() that raised left
            # the play live and unstoppable (dev/docs/BUGS.md 2026-11-17).
            reader.start()
    if not adopt:
        terminate_or_kill(proc, hard=True)
        _remove_stderr(session)
        return None
    return reader


def _read_loop(session: RelaySession, proc: subprocess.Popen) -> None:
    """Drain ffmpeg's stdout into the ring until it ends. The drain is unconditional: the
    IPTV player's speed never reaches this loop, so a frozen client cannot stall ffmpeg."""
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
        # The pipe's end is the ffmpeg's end: killed at a swap or a stop, or the feed ran out.
        with _lock:
            _close_phase_locked(session, proc)
        try:
            out.close()
        except OSError:
            pass   # the pipe is already gone with the process


def stream(session: RelaySession, marks: bool = False):
    """The HTTP body: chunks from the ring on the reserve's pacing until the session ends.
    Runs outside any request or app context - everything it needs is on the session - and
    its close (the IPTV player disconnected, changed channel, was turned off) ends the session
    REASON_CLIENT_GONE. A session the reaper ended stops the body on its next turn. `marks`
    also yields SOURCE_MARK where one source's bytes give way to the next (the browser preview);
    a player is never sent one."""
    try:
        while session.live:
            chunk = session.ring.take(timeout=1.0)
            if chunk is None:
                continue
            if chunk is SOURCE_MARK:
                if marks:
                    yield chunk
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
        # On a waiting screen or a card: no seat to free for anyone, and time on it to close.
        on_screen = session.state in (STATE_WAITING, STATE_CARD)
    pictures = ''
    if not claimed:
        with session._teardown_lock:
            return False
    try:
        try:
            if proc is not None:
                # Killed outright, here and wherever a relay's ffmpeg is stopped: it writes
                # to a pipe, so there is no trailer or file a graceful stop would finish,
                # and the wait for one is time the next channel cannot start in at the
                # viewer cap (measured 0.52 s graceful, 0.01 s killed - dev/changelog/1527).
                terminate_or_kill(proc, hard=True)
            # The reader ends on the pipe's EOF, which the kill produces; joined before the
            # ring is cleared so ffmpeg's drained tail cannot land in a cleared ring.
            if reader is not None and reader is not threading.current_thread():
                reader.join(timeout=5.0)
        finally:
            try:
                connlim.release(session.account_id, 'viewer', session.id)
            finally:
                try:
                    if session.combined is not None:
                        # The combiner is dead: its decoder, pacer and writers go, its pictures
                        # are let go of, and the provider time they ran up is this play's.
                        pictures = session.combined.title()
                        for account_id, secs in _pip_engine().detach(session).items():
                            session.account_seconds[account_id] = (
                                session.account_seconds.get(account_id, 0.0) + secs)
                finally:
                    _remove_stderr(session)
                    session.ring.clear()
        with _lock:
            if on_screen:
                session.screen_seconds = session._screen_seconds(time.monotonic())
            session.state = STATE_STOPPED
            session.slot_claimed = False
            session.reason = reason
            session.detail = detail
            session.ended_at = datetime.utcnow()
            # Its reader may still be draining past the join's timeout, or be this thread.
            _close_phase_locked(session)
    finally:
        session._teardown_lock.release()
    if not on_screen and reason != REASON_SHUTDOWN:
        # A slot or a seat may have come free for a game waiting on the screen for one.
        recheck_waiting(held_only=True)
    level = logging.INFO if reason in (REASON_CLIENT_GONE, REASON_SHUTDOWN, REASON_REPLACED) else logging.WARNING
    if session.internal:
        level = logging.INFO   # the engine says what became of the picture (pip_engine.Picture.ended)
    log.log(level, 'Viewer %s: %s stopped after %s - %s%s', session.id, session.label,
            fmt_bytes(session.bytes_sent), reason, f' ({detail})' if detail else '')
    # Not on shutdown: this runs in the signal handler then, where starting a thread that
    # writes the database has no place. The log line above is that play's record.
    if reason != REASON_SHUTDOWN and not session.internal:
        _queue_play(session.app, {
            'kind': FEED_PLAY_ENDED, 'at': session.ended_at, 'first_at': session.started_at,
            'feed_id': session.feed_id, 'label': session.label, 'client': session.client,
            'feed_login': session.feed_login or None,
            # A play that only ever watched a sports channel's waiting screen played that, and
            # an Up next channel's play only ever plays its picture.
            'channel_id': session.channel_id, 'channel_name': pictures or session.channel_name or (
                SCREEN_NAME if session.sports_row_id else PICTURE_NAME
                if session.up_next_number is not None else CARD_NAME
                if session.pip_row_id is not None else ''),
            'account_id': session.account_id, 'reason': reason,
            'message': ' '.join(x for x in (session.reason_text(), detail, _screen_note(session),
                                            _moves_note(session), play_notes(session),
                                            reserve_short_note(session.ring)) if x),
            'seconds': int(max(0.0, time.monotonic() - session.started_mono)) if session.started_mono else 0,
            'bytes_sent': session.bytes_sent, 'moves': len(session.failovers),
            'account_seconds': json.dumps({str(aid): round(secs, 1) for aid, secs
                                           in sorted(session.account_seconds.items())})})
    return True


def _screen_note(session: RelaySession) -> str:
    """What a play's line in Recent plays says about the waiting screen, or '' for a play
    that never saw it."""
    if session.pip_row_id is not None:
        if not session.screen_seconds:
            return ''
        waited = fmt_duration(session.screen_seconds)
        # No account was ever timed: the play never left its card for a picture.
        return (f'Showed only its card, for {waited}.' if not session.account_seconds
                else f'Was on its card for {waited} of that.')
    if not session.sports_row_id:
        return ''
    waited = fmt_duration(session.screen_seconds)
    if session.left_screen_at is None:
        what = f', waiting for {session.waiting_for}' if session.waiting_for else ''
        return f'Watched only the waiting screen, for {waited}{what}.'
    return f'Was on the waiting screen for {waited} before it came on.'


def reserve_short_note(ring: Ring) -> str:
    """What Watching now and a play's line in Recent plays say when the ring gave part of the
    stream out ahead of its reserve, or ''."""
    if not ring.released_early:
        return ''
    return (f'{fmt_bytes(ring.released_early)} went out sooner than {ring.reserve:g}s behind live, '
            f'because the {fmt_bytes(ring.capacity)} buffer was nearly full.')


def _remove_stderr(session: RelaySession) -> None:
    if session.stderr_path:
        try:
            os.unlink(session.stderr_path)
        except OSError:
            pass   # already gone, or never created


def stop_all(reason: str = REASON_SHUTDOWN) -> int:
    """End every live play by name, and stop any waiting-screen clip being made for one: a
    make outlives the play that asked for it otherwise. No database, signal-handler safe."""
    with _lock:
        live = [s for s in _sessions.values() if s.live]
    for s in live:
        _finish(s, reason)
    # After the plays: a pair's pictures close with no play left on them to rebuild for.
    _pip_engine().close_all(reason)
    _screen_mod().stop_makes()
    return len(live)


def stop_feed_id(feed_id: int, reason: str) -> int:
    """End every live play of `feed_id`, by name. How many it ended."""
    with _lock:
        live = [s for s in _sessions.values() if s.live and s.feed_id == feed_id]
    return sum(1 for s in live if _finish(s, reason))


def stop_account(account_id: int, reason: str = REASON_ACCOUNT_OFF) -> int:
    """End every live play coming from `account_id`, by name, and every picture-in-picture
    picture on it, whose window then says why. How many it ended."""
    with _lock:
        live = [s for s in _all_live_locked() if s.account_id == account_id]
    # A picture is not a play: it is ended here and counted nowhere.
    return sum(1 for s in live if _finish(s, reason) and not s.internal)


def _live_pip_plays(row_id: int) -> list:
    with _lock:
        return [s for s in _sessions.values() if s.live and s.pip_row_id == row_id]


def stop_pip(row_id: int, reason: str = REASON_TAKEN_OFF, swapped_only: bool = False) -> int:
    """End every live play of picture-in-picture channel `row_id`, by name: of both its feed
    ids, or of its swapped copy alone when that is what stopped being offered. How many it
    ended. Called after the commit that took it off, never inside it."""
    ended = sum(1 for s in _live_pip_plays(row_id)
                if (not swapped_only or pip_swapped(s.feed_id)) and _finish(s, reason))
    if not swapped_only:
        # Its pictures do not linger for a channel that is no longer offered.
        _pip_engine().close_row(row_id, reason)
    return ended


def refresh_pip(row_id: int) -> tuple:
    """A picture-in-picture channel's pictures or layout were changed. (plays on its card that
    were asked again, plays left on a picture, plays that change in place). A play on its card
    asks again within _REAP_INTERVAL. One on its two pictures combined changes in place: a
    picture whose source changed is replaced, one that did not keeps its connection, and a new
    layout is the next combiner's (pip_engine.row_changed). One on its big picture alone
    keeps it until it tunes in again. Requires an app context; called after the commit."""
    asked = kept = 0
    changing = _pip_engine().row_changed(row_id)
    for s in _live_pip_plays(row_id):
        with _lock:
            on_card = s.on_card
            alone = not on_card and s.combined is None
            if on_card:
                s.recheck_due = True
        asked, kept = asked + on_card, kept + alone
    return asked, kept, changing


def clear_pip(row_id: int) -> int:
    """Move every live play of a picture-in-picture channel that was just cleared onto the card
    saying it has nothing on it, giving its connection back. How many were on a picture.
    Requires an app context; each move runs on a thread of its own, since it stops an ffmpeg and
    makes a clip."""
    moved = 0
    for s in _live_pip_plays(row_id):
        with _lock:
            on_card = s.on_card
            if on_card:
                s.recheck_due = True
        if on_card:
            continue
        moved += 1
        threading.Thread(target=_to_card_now, args=(s,), daemon=True,
                         name=f'relay-card-{s.id}').start()
    return moved


def _live_sports_plays() -> list:
    """The live plays of a sports channel or an Up next channel: the two kinds whether a follow
    serves."""
    with _lock:
        return [s for s in _sessions.values()
                if s.live and decode_feed_id(s.feed_id)[0] in (KIND_SPORTS, KIND_UP_NEXT)]


def _sports_served_ids() -> frozenset:
    from . import sports_feed, sports_up_next
    return frozenset([feed_id_for_sports(c.row.id) for c in sports_feed.served_channels()]
                     + [feed_id_for_up_next(c.number) for c in sports_up_next.channels()])


def sports_served_now() -> Optional[frozenset]:
    """The feed ids of the sports channels and Up next channels served right now, for
    stop_unserved_sports(): read before a person's act that may stop one being served. None, and
    no query, when neither kind is being played. Requires an app context."""
    if not _live_sports_plays():
        return None
    return _sports_served_ids()


def stop_unserved_sports(before: Optional[frozenset], reason: str = REASON_TAKEN_OFF) -> int:
    """End every live play of a sports channel or an Up next channel that was served `before`
    (sports_served_now(), read before the act) and is not now, on a game, on the waiting screen or
    on its picture, by name. How many it ended. The one place a person's act that stops a sports channel being served cuts its plays:
    its switch going off, a follow a person changed or removed, its league deleted
    (dev/changelog/1586). Called after that act's commit, never inside it.

    Only what THIS act stopped being served is cut. A rule's follow ending is not a person
    turning the channel off and does not come here, and a game still playing after one is not in
    `before`, so a later act about something else leaves it alone: it plays out, and a play on
    the waiting screen ends REASON_SCREEN_GONE at its next re-check. One read of what is served
    whatever the number of plays; requires an app context."""
    if not before:
        return 0
    live = [s for s in _live_sports_plays() if s.feed_id in before]
    if not live:
        return 0
    served = _sports_served_ids()
    cut = sum(1 for s in live if s.feed_id not in served and _finish(s, reason))
    if cut:
        log.info('IPTV output: %s of a sports or Up next channel that is no longer served cut',
                 counted(cut, 'play'))
    return cut


def stop_session(session_id: str, reason: str = REASON_STOPPED, detail: str = '') -> bool:
    """End one live play. False when it is unknown or already over. No database and no app
    context, so the browser preview's teardown can call it."""
    session = get_session(session_id)
    return bool(session is not None and session.live and _finish(session, reason, detail=detail))


def live_count_for(feed_id: int) -> int:
    with _lock:
        return sum(1 for s in _sessions.values() if s.live and s.feed_id == feed_id)


# ── The switch ─────────────────────────────────────────────────────────────────────────────

def set_on_output(kind: str, row_id: int, on: bool):
    """Take a channel, a group or a picture-in-picture channel off the IPTV output, or put it back: THE only writer of
    `on_output`, column and event together (the shape of channel_groups.set_participation).
    Mutates and adds the event without committing, so the caller owns the commit and keeps
    the read-modify-write in one retry_on_locked unit; ending the plays of a row that went
    off is the caller's too, after the commit (stop_feed_id), never inside it.

    Returns (row, moved), or (None, False) for an id that is not a guide row of that kind. A
    switch that did not move logs nothing. The feed id is the row's own and does not move,
    so an IPTV player that cached it finds the same channel when it comes back. A sports
    channel has its own switch (app/sports_feed.py) and is not handled here; a
    picture-in-picture channel's is its row's, with no event (it has no timeline)."""
    from . import db
    from .database import (Channel, ChannelEvent, ChannelGroup, ChannelGroupEvent,
                           OUTPUT_FEED_SWITCHED, PipChannel)
    if kind == KIND_PIP:
        # ChannelBin's own channel, in no TV Guide and with no timeline to carry an event: the
        # route that asked logs the move. Its swapped copy follows it and has no switch.
        row = db.session.get(PipChannel, row_id)
        if row is None:
            return None, False
        on = bool(on)
        if bool(row.on_output) == on:
            return row, False
        row.on_output = on
        return row, True
    if kind == KIND_CHANNEL:
        row = db.session.get(Channel, row_id)
    elif kind == KIND_GROUP:
        row = db.session.get(ChannelGroup, row_id)
    else:
        raise ValueError(f'No output switch for a row of kind "{kind}"')
    if row is None or not row.in_guide:  # guide-flag-read-ok: only a guide row is on the feed, so only one has the switch
        return None, False
    on = bool(on)
    if bool(row.on_output) == on:
        return row, False
    row.on_output = on
    detail = ('Put back on the IPTV output by hand' if on
              else 'Taken off the IPTV output by hand. It stays in the TV Guide')
    extra = json.dumps({'on_output': on, 'source': 'user'})
    if kind == KIND_CHANNEL:
        db.session.add(ChannelEvent(channel_id=row.id, event_type=OUTPUT_FEED_SWITCHED,
                                    detail=detail, extra_data=extra))
    else:
        db.session.add(ChannelGroupEvent(group_id=row.id, event_type=OUTPUT_FEED_SWITCHED,
                                         detail=detail, extra_data=extra))
    return row, True


# ── The play log ───────────────────────────────────────────────────────────────────────────

def _queue_play(app, entry: dict) -> None:
    """Hand one line of Recent plays to the writer thread. `app` is the Flask app when the
    caller has one in hand (a session carries its own); None means the caller is inside a
    request, and with neither the line is dropped with a log line saying so."""
    global _log_writer
    if app is None:
        from flask import current_app, has_app_context
        if not has_app_context():
            log.warning('Output feed: a %s play of feed id %s was not written to Recent plays '
                        '(no app to write it with)', entry.get('kind'), entry.get('feed_id'))
            return
        app = current_app._get_current_object()
    with _lock:
        _play_queue.append(entry)
        if _log_writer is None:
            # A kick meant for a writer that had already exited would end this one's first
            # wait at once, writing the line before a caller's own flush_play_log() can.
            _log_kick.clear()
            _log_writer = threading.Thread(target=_log_loop, args=(app,), name='relay-play-log',
                                           daemon=True)
            _log_writer.start()


def _log_loop(app) -> None:
    global _log_writer
    while True:
        _log_kick.wait(_LOG_INTERVAL)
        _log_kick.clear()
        with _lock:
            batch = list(_play_queue)
            _play_queue.clear()
            if not batch:
                _log_writer = None
                return
        try:
            with app.app_context():
                _write_plays(batch)
        except Exception:                               # noqa: BLE001 - the log must never take the feed down
            log.exception('Output feed: %d line(s) of Recent plays could not be written and '
                          'are lost', len(batch))


def flush_play_log() -> int:
    """Write what is queued now, in the caller's app context. For a caller that needs the
    lines in the table before it reads them (the page after its own action, the tests)."""
    with _lock:
        batch = list(_play_queue)
        _play_queue.clear()
    if batch:
        _write_plays(batch)
    return len(batch)


def discard_play_log(timeout: float = 5.0) -> bool:
    """Forget what is queued and wait for the writer thread to exit. tests/support/app.py:
    a writer left running would write one test's lines into the next test's database."""
    with _lock:
        del _play_queue[:]
        thread = _log_writer
    if thread is None:
        return True
    _log_kick.set()
    thread.join(timeout)
    return not thread.is_alive()


def _write_plays(batch: list) -> None:
    from . import db
    from .database import OutputFeedPlay
    from .db_utils import retry_on_locked

    @retry_on_locked()
    def _write():
        for entry in batch:
            at = entry['at'] or datetime.utcnow()
            if entry['kind'] == FEED_PLAY_REFUSED:
                # Re-read inside the retried unit: a rolled-back session forgets the bump.
                last = (OutputFeedPlay.query
                        .filter_by(kind=FEED_PLAY_REFUSED, feed_id=entry['feed_id'],
                                   message=entry['message'], feed_login=entry.get('feed_login'))
                        .filter(OutputFeedPlay.at >= at - REFUSAL_RUN_GAP)
                        .order_by(OutputFeedPlay.at.desc()).first())
                if last is not None:
                    last.at = max(last.at, at)
                    last.count += 1
                    last.client = entry.get('client') or last.client
                    db.session.flush()
                    continue
            fields = {k: v for k, v in entry.items() if v is not None}
            fields.setdefault('first_at', at)
            fields['at'] = at
            db.session.add(OutputFeedPlay(**fields))
            db.session.flush()
        db.session.commit()

    _write()
    _prune_plays()


def _prune_plays() -> None:
    """Drop lines older than PLAY_LOG_DAYS, at most once an hour and only on the back of a
    write, so an output nobody uses runs nothing."""
    global _last_prune_mono
    from . import db
    from .database import OutputFeedPlay
    from .db_utils import retry_on_locked
    now = time.monotonic()
    if _last_prune_mono is not None and now - _last_prune_mono < _PRUNE_EVERY_SECONDS:
        return
    _last_prune_mono = now

    @retry_on_locked()
    def _prune():
        # Never the highest id: SQLite hands the next row max(id) + 1 (no AUTOINCREMENT), so
        # deleting it would re-issue an id account_stats' play watermark has already passed,
        # and that play would never be counted (dev/changelog/1676).
        newest = db.session.query(db.func.max(OutputFeedPlay.id)).scalar_subquery()
        gone = OutputFeedPlay.query.filter(
            OutputFeedPlay.at < datetime.utcnow() - timedelta(days=PLAY_LOG_DAYS),
            OutputFeedPlay.id < newest).delete(synchronize_session=False)
        db.session.commit()
        return gone

    gone = _prune()
    if gone:
        log.info('Output feed: %d line(s) of Recent plays older than %d days removed', gone,
                 PLAY_LOG_DAYS)


def kill_all_viewers():
    """Shutdown hook (run.py): end every relay so a restarted process never leaves an
    orphaned ffmpeg pulling a provider stream for an IPTV player it cannot serve. No DB, signal-handler
    safe."""
    stop_all(REASON_SHUTDOWN)


def preempt_for_account(app, account_id: int, recording_id: int, movers: Optional[list] = None) -> bool:
    """A recording on `account_id` needs the seat: end every live session on that account
    that no longer holds one REASON_PREEMPTED, and tell the user twice - a VIEWER_PREEMPTED
    alert naming the recording, and a diagnostics event on the recording's own timeline
    saying what it displaced (section 4.4). Called by
    recorder._try_acquire_slot_with_preemption after connection_limits.preempt_viewers_for_slot
    stripped the seats; a play it left seated (one on a held seat, dev/changelog/1536) plays on.
    A session with proc=None is inside its launch window and is flagged so it never runs on
    the stripped seat.

    With `movers`, a play that can move to another channel (a group, a sports channel) is not
    ended: its ffmpeg is stopped here, so the provider connection is closed before the
    recording opens its own, and the play is appended to `movers` as (session, account_id) for
    move_preempted() to move once the recording holds its seat (dev/changelog/1642). Until the
    move names its new account the play is `_moving`: it holds no seat anywhere, so the reaper,
    a failover and another recording's preemption all leave it alone."""
    seated = connlim.seated_holder_ids('viewer')
    moving, detached = [], []
    with _lock:
        matched = [s for s in _all_live_locked()
                   if not s._moving and s.account_id == account_id and s.id not in seated]
        for s in matched:
            if movers is not None and _can_move(s):
                s._moving = True
                detached.append((s.proc, s.reader))
                s.proc = s.reader = None
                moving.append(s)
                # Handed over here, under the lock that set the flag: whatever raises below,
                # the caller's move_preempted() still sees the play and ends or moves it.
                movers.append((s, account_id))
                continue
            s.preempted = True
            s.preempted_by = recording_id
        launched = [s for s in matched if s.proc is not None and s not in moving]
    for s, (proc, reader) in zip(moving, detached):
        _stop_detached(s, proc, reader)
        log.warning('Viewer %s: recording %d took the connection on account %d - %s moves to '
                    'another channel if one is free', s.id, recording_id, account_id, s.label)
    for s in launched:
        _finish(s, REASON_PREEMPTED, detail=f'recording {recording_id} took the connection')
    cut = [s for s in matched if s not in moving]
    if cut:
        # A picture-in-picture picture a recording cut raises no alert: the TV kept its other
        # picture, and the window says why (dev/docs/DESIGN-pip-channels.md 8.2).
        _announce_quietly(app, [(s, s.channel_name, s.channel_id, None, not s.internal) for s in cut],
                          recording_id, account_id)
    return bool(matched)


def _can_move(session: RelaySession) -> bool:
    """Whether a play a recording is preempting can move to another channel instead of ending:
    one that fails over (a group, a sports channel), relaying a channel now, and not already
    between channels or ending. The caller holds _lock."""
    return (session.fails_over and session.app is not None and session.proc is not None
            and not session._failing_over and not session._carding and not session._finishing
            and session.state in (STATE_STARTING, STATE_STREAMING))


def _stop_detached(session: RelaySession, proc, reader) -> None:
    """Stop the ffmpeg and reader a move or a failover took off `session`, and remove its stderr
    spool. The old reader drains the old channel's tail before the new one starts, so the two
    channels' bytes never interleave in the ring at the switch."""
    if proc is not None:
        terminate_or_kill(proc, hard=True)
    if reader is not None and reader is not threading.current_thread():
        reader.join(timeout=5.0)
    _remove_stderr(session)


def move_preempted(app, movers: list, recording_id: int) -> None:
    """Move each play preempt_for_account() stopped for `recording_id` to another channel on an
    account with a connection free, on its own thread. Called once the recording's acquire is
    done, so a move can never take back the seat the recording was freeing, and the recording's
    start never waits on one. Never raises: it runs in the recorder's acquire path, and a play
    whose move cannot even start is ended by name instead."""
    for session, account_id in movers:
        try:
            threading.Thread(target=_move_for_recording,
                             args=(app, session, recording_id, account_id),
                             daemon=True, name=f'relay-move-{session.id}').start()
        except RuntimeError:
            # can't start new thread: the play holds no seat and has no ffmpeg, so it ends here.
            log.exception('Viewer %s: could not start the move off the connection recording %d '
                          'took', session.id, recording_id)
            with _lock:
                session._moving = False
            _finish(session, REASON_PREEMPTED, detail=f'recording {recording_id} took the connection')


def _move_for_recording(app, session: RelaySession, recording_id: int, account_id: int) -> None:
    from_name, from_id = session.channel_name, session.channel_id
    moved, why = None, ''
    try:
        with app.app_context():
            moved, why = _move_play(session, recording_id)
            if moved is None and session.live:
                _finish(session, REASON_PREEMPTED,
                        detail=f'recording {recording_id} took the connection, and {why}')
            # The recording's timeline says what it took whatever became of the play - a player
            # that gave up mid-move was still cut by it; only a play this cut raises the alert.
            _announce_quietly(app, [(session, from_name, from_id, moved,
                                     moved is None and session.reason == REASON_PREEMPTED
                                     and not session.internal)],
                              recording_id, account_id)
    except Exception:                                   # noqa: BLE001 - one play, never the reaper
        # A play left live with no ffmpeg and no seat would never be reaped (_check skips a
        # moving play), so whatever went wrong ends it by name.
        log.exception('Viewer %s: moving %s off the connection recording %d took failed',
                      session.id, session.label, recording_id)
        _finish(session, REASON_PREEMPTED, detail=f'recording {recording_id} took the connection')
    finally:
        with _lock:
            session._moving = False


def _move_play(session: RelaySession, recording_id: int) -> tuple:
    """Put `session` on the best channel that can play what it asked for whose account has a
    connection free, with the seat taken, in place - the TV sees an MPEG-TS discontinuity, as at
    a failover. (the channel, '') or (None, why not as a clause); None leaves the caller to end
    the play. A channel that will not start is passed over for the next, as at a failover."""
    from .config import load_config
    cfg = load_config()
    from_name = session.channel_name
    why = f'no other channel that can play {session.label} has a connection free'
    refused = []
    while True:
        if not session.live or session._finishing:
            return None, why
        play = _resolve_again(session, cfg, frozenset(session.failed_ids), session.event_id)
        if play is None or play.member is None:
            return None, why
        member = _seat_for(session, play.member, session.event_id, cfg, refused=tuple(refused))
        if member is None:
            return None, why
        with _lock:
            ending = not session.live or session._finishing
            seated = not ending and session.id in connlim.seated_holder_ids('viewer')
            if seated:
                # Named with the seat confirmed in one hold of the lock, and no longer moving: a
                # recording that starts on the new account from here on finds the play and ends
                # it (preempt_for_account matches on the account).
                session.account_id = member.account_id
                session.preempted, session.preempted_by = False, None
                session._moving = False
        if ending:
            connlim.release(member.account_id, 'viewer', session.id)
            return None, why
        if not seated:
            return None, f'a recording took the connection on "{member.name}" as the play was moving to it'
        try:
            _launch(session, member, cfg, first=False)
        except FeedRefused as exc:
            log.warning('Viewer %s: cannot move to "%s": %s', session.id, member.name, exc.message)
            connlim.release(member.account_id, 'viewer', session.id)
            why = f'"{member.name}" could not start: {exc.message}'
            refused.append(member)
            with _lock:
                session._moving = True
            continue
        break
    with _lock:
        if not session.live:
            return None, why
        session.state = STATE_STARTING
        session.last_data_mono = None
        session.failovers.append((from_name, member.name, REASON_MOVED_FOR_RECORDING))
    log.warning('Viewer %s: recording %d took the connection - %s moved from "%s" to "%s" on '
                'account %d', session.id, recording_id, session.label, from_name, member.name,
                member.account_id)
    if session.group_id is not None:
        _log_viewer_move(session.group_id, member.id,
                         f'An IPTV player watching through the IPTV output was moved from "{from_name}" '
                         f'to "{member.name}": recording {recording_id} needed the connection '
                         f'"{from_name}" was using.')
    return member, ''


def _log_viewer_move(group_id: int, channel_id: int, detail: str) -> None:
    """A GROUP_VIEWER_FAILOVER event on the group's timeline. Needs an app context. A write that
    fails is logged and never ends the play it describes: the move has already happened."""
    from sqlalchemy.exc import SQLAlchemyError
    from . import db
    from .database import ChannelGroupEvent, GROUP_VIEWER_FAILOVER
    from .db_utils import retry_on_locked

    @retry_on_locked()
    def _log_group_event_and_commit():
        db.session.add(ChannelGroupEvent(group_id=group_id, channel_id=channel_id,
                                         event_type=GROUP_VIEWER_FAILOVER, detail=detail))
        db.session.commit()

    try:
        _log_group_event_and_commit()
    except SQLAlchemyError:
        db.session.rollback()
        log.exception('Group %d: could not write the IPTV player move to its timeline: %s',
                      group_id, detail)


def _announce_quietly(app, plays, recording_id: int, account_id: int) -> None:
    try:
        _announce_preemption(app, plays, recording_id, account_id)
    except Exception:                               # noqa: BLE001 - a note, never the recording
        # This runs inside the recorder's own acquire path: a viewer's note that cannot be
        # written must never cost the recording its start (CLAUDE.md principle 2).
        log.exception('Viewer preemption by recording %d: could not write the alert or '
                      'the recording event', recording_id)


def _announce_preemption(app, plays, recording_id: int, account_id: int) -> None:
    """`plays` are (session, the name and id of the channel it was on, the channel it moved to or
    None, whether to raise the alert). A play the recording cut raises the VIEWER_PREEMPTED alert;
    one that moved raises none - it kept playing - and its move is in its own history and its
    group's timeline. Either way the recording's timeline says what it took the connection from,
    and what became of the play."""
    from . import db
    from .alerts import create_alert
    from .database import DIAGNOSTICS, Account, Recording, add_recording_event
    from .db_utils import retry_on_locked

    with app.app_context():
        rec = db.session.get(Recording, recording_id)
        account = db.session.get(Account, account_id)
        rec_name = rec.name if rec else f'#{recording_id}'
        account_name = account.name if account else f'account {account_id}'
        for s, was_on, _was_id, _moved, alert in plays:
            if not alert:
                continue
            create_alert(
                'VIEWER_PREEMPTED',
                f'IPTV player cut for recording "{rec_name}"',
                body=(f'An IPTV player watching {s.label} on the IPTV output (channel '
                      f'"{was_on}" on {account_name}) was cut because recording '
                      f'"{rec_name}" needed the connection. A recording always wins; the '
                      f'IPTV player reconnects on its own if another connection is free.'),
                source=f'feed:{s.feed_id}:preempted', recording_id=recording_id)

        @retry_on_locked()
        def _note_on_recording_and_commit():
            if db.session.get(Recording, recording_id) is None:
                return
            for s, was_on, was_id, moved, _alert in plays:
                then = (f' The play moved to "{moved.name}" on '
                        f'{moved.account.name if moved.account else f"account {moved.account_id}"}.'
                        if moved is not None else '')
                add_recording_event(
                    recording_id, DIAGNOSTICS,
                    detail=(f'Took the connection from an IPTV player watching {s.label} '
                            f'(channel "{was_on}") on {account_name}.{then}'),
                    extra={'kind': 'viewer_preempted', 'feed_id': s.feed_id,
                           'channel_id': was_id,
                           'account_id': account_id, 'client': s.client,
                           'moved_to_channel_id': moved.id if moved is not None else None})
            db.session.commit()

        _note_on_recording_and_commit()


# ── Failover and the reaper ────────────────────────────────────────────────────────────────

def _check(session: RelaySession) -> None:
    now = time.monotonic()
    with _lock:
        proc = session.proc
        preempted = session.preempted
        state = session.state
        failing_over = session._failing_over or session._moving or session._carding
    if preempted and proc is not None:
        _finish(session, REASON_PREEMPTED,
                detail=f'recording {session.preempted_by} took the connection')
        return
    if proc is None or failing_over:
        # No process to judge: the session is inside its launch window (start_session()
        # registers it before _launch() adopts the ffmpeg, so a reaper another viewer keeps
        # running sees it with launched_mono still 0.0), between members on a failover, or
        # moving off a connection a recording took. The launch or the move ends it itself
        # when the seat or the spawn fails.
        return
    if session.ring.dropped_unripe:
        _finish(session, REASON_BUFFER_TOO_SMALL,
                detail=f'{fmt_bytes(session.ring.dropped_unripe)} of the stream was lost: more '
                       f'than the {fmt_bytes(session.ring.capacity)} buffer holds arrived '
                       f'inside {session.ring.reserve:g}s.')
        return
    if session.ring.released_early and not session._reserve_short_logged:
        session._reserve_short_logged = True
        log.warning('Viewer %s: %s is playing on less than its %gs reserve - the %s buffer '
                    'filled, so the stream is going out early instead of being lost',
                    session.id, session.label, session.ring.reserve,
                    fmt_bytes(session.ring.capacity))
    if session.ring.dropped:
        _finish(session, REASON_CLIENT_TOO_SLOW,
                detail=f'{fmt_bytes(session.ring.dropped)} dropped unread')
        return
    # Idle is a fact about the IPTV player, so it is measured on the IPTV player's side of the ring: a chunk
    # the IPTV player could have taken and has not. A provider that stopped sending leaves nothing
    # releasable and is the stall branch's business below, never this one.
    untaken = session.ring.untaken_age(now)
    if untaken > session.idle_timeout:
        _finish(session, REASON_IDLE, detail=f'nothing read for {untaken:.0f}s')
        return
    if now - session.started_mono > session.max_seconds:
        _finish(session, REASON_MAX_DURATION)
        return
    if state in OWN_PICTURE_STATES:
        _check_waiting(session, proc, now)
        return
    if session.combined is not None:
        _pip_engine().check(session, proc, now, state)
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
    the play is a group or a sports channel with somebody left, else end the session by the
    member's reason."""
    if not session.fails_over or session.app is None:
        _finish(session, reason, detail=detail)
        return
    with _lock:
        # The reaper judged an ffmpeg it read a moment ago. A play that has since gone to
        # ChannelBin's own clip, or is on its way to its card, has no channel left to fail over
        # from, and a failover beside that move would start a second clip nothing stops.
        if session._failing_over or session._carding or session.own_picture or not session.live:
            return
        session._failing_over = True
    try:
        _failover(session, reason, detail)
    finally:
        with _lock:
            session._failing_over = False


def _failover(session: RelaySession, reason: str, detail: str):
    """Take `session` off the channel it is on and onto the best one left, in place. The channel
    it lands on, or None when nothing would start: the play then ended REASON_NO_MEMBER, or, for
    a picture-in-picture channel, went to its card and is still live (`session.on_card`)."""
    from .config import load_config

    from .alerts import create_alert, has_open_alert

    failed_id, failed_name = session.channel_id, session.channel_name
    session.failed_ids.add(failed_id)
    with _lock:
        old_proc, old_reader = session.proc, session.reader
        session.proc = session.reader = None
    with session.app.app_context():
        cfg = load_config()
        _stop_detached(session, old_proc, old_reader)
        while True:
            play = _resolve_again(session, cfg, frozenset(session.failed_ids), session.event_id)
            if play is not None and play.pip is not None and play.member is None:
                # Zero channels left is a card, never the end of the play
                # (dev/docs/DESIGN-pip-channels.md 8.3): it tries again by itself.
                log.warning('Viewer %s: "%s" %s and nothing else can play %s right now, so it shows '
                            'its card', session.id, failed_name, _died_phrase(reason), session.label)
                _note_once(session, f'"{failed_name}" {_died_phrase(reason)} and nothing else could '
                                    f'play, so it showed its card.')
                _put_on_card(session, play, cfg)
                return None
            if play is None or play.member is None:
                gone = 'the group' if session.group_id is not None else 'the channel'
                why = play.why_none if play is not None else f'{gone} is no longer served'
                detail = f'"{failed_name}" {_died_phrase(reason)}; {why}'
                if _finish(session, REASON_NO_MEMBER, detail=detail) and (play is None or play.fault):
                    source = f'feed:{session.feed_id}:no-member'
                    if not has_open_alert('FEED_NO_MEMBER', source):
                        create_alert('FEED_NO_MEMBER',
                                     f'IPTV output: nothing left to play for {session.label}',
                                     body=f'An IPTV player watching {session.label} was cut: {detail}.',
                                     source=source)
                return None
            member = play.member
            try:
                _launch(session, member, cfg, first=False)
            except FeedRefused as exc:
                log.warning('Viewer %s: cannot fail over to "%s": %s', session.id, member.name,
                            exc.message)
                if not session.live:
                    return None
                session.failed_ids.add(member.id)
                continue
            break
        with _lock:
            if not session.live:
                return None
            session.state = STATE_STARTING
            session.last_data_mono = None
            session.failovers.append((failed_name, member.name, reason))
        why = _died_phrase(reason) + (f' ({detail})' if detail else '')
        log.warning('Viewer %s: "%s" %s - moved to "%s"', session.id, failed_name, why,
                    member.name)

        if session.group_id is not None:
            # A sports channel has no group whose timeline could carry this. The move is in
            # the session's own history, which the dashboard shows, and in the log above.
            _log_viewer_move(session.group_id, member.id,
                             f'An IPTV player watching through the IPTV output was moved from '
                             f'"{failed_name}" to "{member.name}": "{failed_name}" {why}.')
        return member


def _died_phrase(reason: str) -> str:
    """Why a play left a channel, as the words after the channel's name."""
    if reason == REASON_CONNECT_TIMEOUT:
        return 'sent nothing in time'
    if reason == REASON_FFMPEG_EXITED:
        return 'stopped delivering'
    if reason == REASON_MOVED_BY_HAND:
        return 'was left by hand, from Watching now'
    raise ValueError(f'No words for why a play left a channel: "{reason}"')


def move_text(from_name: str, to_name: str, reason: str) -> str:
    """One of a play's moves as a sentence. RelaySession.failovers keeps each move's reason as a
    REASON_* code, which is never shown as it is (dev/docs/BUGS.md 2026-11-15 @ 03:51:40 PM)."""
    if reason == REASON_MOVED_FOR_RECORDING:
        return (f'Moved from "{from_name}" to "{to_name}": a recording needed the connection '
                f'"{from_name}" was using.')
    if reason == REASON_MOVED_BY_HAND:
        return f'Moved from "{from_name}" to "{to_name}" by hand, from Watching now.'
    return f'Moved from "{from_name}" to "{to_name}": "{from_name}" {_died_phrase(reason)}.'


# What a move by hand did with the channel it left (MoveResult.outcome).
MOVE_ENDED = 'ended'              # nothing else would start, so the play ended; nothing written
MOVE_TO_CARD = 'to_card'          # nothing else would start, so a picture-in-picture play shows its card
MOVE_ADJUSTED = 'adjusted'        # moved, and the channel left now has HAND_MOVE_ADJUSTMENT
MOVE_ALREADY = 'already'          # moved; the channel left already had it, so nothing was written
MOVE_CHANNEL_GONE = 'channel_gone'    # moved; the channel left is no longer in the database
MOVE_UNSAVED = 'unsaved'          # moved, but the adjustment could not be written (`error`)


class MoveResult:
    """What move_by_hand() did: the channel the play left, the name of the one it is on now ('' for
    MOVE_ENDED), and one of the MOVE_* outcomes above."""

    def __init__(self, from_id: int, from_name: str, to_name: str, outcome: str, error: str = ''):
        self.from_id, self.from_name, self.to_name = from_id, from_name, to_name
        self.outcome, self.error = outcome, error


#: The score adjustment a move by hand writes on the channel it leaves: the lowest there is, so
#: every ranking that reads the score tries it last until a person sets it back on its page. A
#: ranking, not a filter: with nothing else left it is still used.
HAND_MOVE_ADJUSTMENT = -100
HAND_MOVE_NOTE = ('An IPTV player was moved off this channel by hand, from Watching now. Set the '
                  'score adjustment back to 0 in Settings on this page to rank it normally again.')


def move_by_hand(session_id: str) -> MoveResult:
    """Take a live play off the channel it is playing and onto the next best one that plays what
    it asked for, as a failover does, at a person's word from Watching now (dev/changelog/1675).
    The channel it leaves is never returned to for the rest of the play (failed_ids), and gets
    HAND_MOVE_ADJUSTMENT, so a new play, a recording and a group's ranking all try it last
    until the adjustment is set back. Needs an app context.

    Raises FeedRefused, with the play left exactly as it was, when there is nowhere to go: a
    single channel, ChannelBin's own picture, a play already changing what it plays, or no
    other channel that could take it right now. Only once another channel is found is the
    current one stopped - the provider allows one connection, so the two cannot overlap."""
    session = get_session(session_id)
    if session is None or not session.live:
        raise FeedRefused('That play has already ended.', 404)
    offer = move_offer(session)
    if offer == MOVE_NO_CHANNEL:
        raise FeedRefused('That play is showing ChannelBin\'s own picture, not a channel.', 409)
    if offer == MOVE_NOWHERE:
        raise FeedRefused(f'{session.label} is a single channel, so there is nowhere to move it.', 409)
    if offer == MOVE_TWO_PICTURES:
        raise FeedRefused(f'{session.label} plays two pictures, and each moves to another channel by '
                          f'itself when it drops.', 409)
    if offer != MOVE_OFFERED:
        raise ValueError(f'Unknown move offer "{offer}"')
    busy = FeedRefused('That play is changing channel right now. Try again in a moment.', 409)
    if session.app is None or not session._swap_lock.acquire(blocking=False):
        raise busy
    try:
        with _lock:
            free = (session.live and not session._failing_over and not session._moving
                    and not session._carding and not session._finishing and session.proc is not None
                    and session.state in (STATE_STARTING, STATE_STREAMING))
            if free:
                # The failover guard: the reaper and a dying member leave the play alone while
                # it changes channel, as they do for any failover.
                session._failing_over = True
        if not free:
            raise busy
        from_id, from_name = session.channel_id, session.channel_name
        try:
            _next_channel_or_refuse(session)
            # The channel _failover() returns belongs to a session it has closed: the play's
            # own fields carry the name.
            moved = _failover(session, REASON_MOVED_BY_HAND, '') is not None
        except FeedRefused:
            raise
        except Exception as exc:                        # noqa: BLE001 - one play, never left half-moved
            if session.proc is not None:
                raise
            # _failover() had already stopped the channel: a play left live with no ffmpeg
            # is skipped by the reaper and would hold its seat forever, so it ends by name.
            log.exception('Viewer %s: moving %s off "%s" by hand failed', session.id,
                          session.label, from_name)
            _finish(session, REASON_LAUNCH_FAILED,
                    detail=f'moving it off "{from_name}" failed: {mask_creds_in_text(str(exc))}')
            moved = False
        finally:
            with _lock:
                session._failing_over = False
    finally:
        session._swap_lock.release()
    if not moved:
        return MoveResult(from_id, from_name, '', MOVE_TO_CARD if session.live and session.on_card else MOVE_ENDED)
    return _avoid_left_channel(from_id, from_name, session.channel_name)


def _next_channel_or_refuse(session: RelaySession) -> None:
    """Raise FeedRefused unless another channel could take the play right now: one that plays
    what it asked for, on the play's own account or one with a connection free. Asked before
    anything is stopped, so a refusal leaves the play on the channel it is on. resolve_play()
    already prefers a channel whose account has a connection free over a better-ranked one on a
    full account (seat_standing, dev/changelog/1642), so a full account at the top of its answer
    means every one is full."""
    from .config import load_config
    exclude = frozenset(session.failed_ids | {session.channel_id})
    play = resolve_play(session.feed_id, load_config(), exclude_ids=exclude,
                        event_id=session.event_id, viewer_id=session.id, preview=session.preview)
    stays = f'It stays on "{session.channel_name}".'
    if play is None or play.member is None:
        raise FeedRefused(f'No other channel can play {session.label} right now. {stays}', 409)
    member = play.member
    if member.account_id == session.account_id:
        return
    standing = _viewer_standing({member.account_id}, session.id).get(member.account_id)
    if standing != connlim.SEAT_FREE:
        account = member.account.name if member.account else f'account {member.account_id}'
        raise FeedRefused(f'The next channel that plays it, "{member.name}", is on {account}, '
                          f'which has no connection free right now. {stays}', 409)


def _avoid_left_channel(from_id: int, from_name: str, to_name: str) -> MoveResult:
    """Write HAND_MOVE_ADJUSTMENT on the channel a move by hand left. The play has already
    moved, so a write that fails is said in the result and the log, never raised."""
    from sqlalchemy.exc import SQLAlchemyError
    from . import db
    from .database import Channel
    from .db_utils import retry_on_locked
    from .health_score import set_manual_adjustment

    @retry_on_locked()
    def _adjust_and_commit():
        channel = db.session.get(Channel, from_id)
        if channel is None:
            return MOVE_CHANNEL_GONE
        if (channel.manual_health_adjustment or 0) == HAND_MOVE_ADJUSTMENT:
            return MOVE_ALREADY
        set_manual_adjustment(channel, HAND_MOVE_ADJUSTMENT, HAND_MOVE_NOTE,
                              why='an IPTV player was moved off it by hand, from Watching now',
                              extra={'cause': 'hand_move'})
        db.session.commit()
        return MOVE_ADJUSTED

    try:
        outcome = _adjust_and_commit()
    except SQLAlchemyError as exc:
        db.session.rollback()
        log.exception('Channel %d: the play moved off it, but its score adjustment could not be '
                      'written', from_id)
        return MoveResult(from_id, from_name, to_name, MOVE_UNSAVED, error=str(exc))
    return MoveResult(from_id, from_name, to_name, outcome)


def _moves_note(session: 'RelaySession') -> str:
    """What a play's line in Recent plays says about its moves, or ''. A sports channel has no
    group timeline to keep them in, so the line is where each move's reason outlives the play."""
    return ' '.join(move_text(a, b, why) for a, b, why in session.failovers)


def _note_once(session: 'RelaySession', sentence: str) -> None:
    """Keep `sentence` on the play, once however often its cause repeats (a waiting screen is
    redrawn at every re-check)."""
    with _lock:
        if sentence not in session.notes:
            session.notes.append(sentence)


def play_notes(session: 'RelaySession') -> str:
    with _lock:
        return ' '.join(session.notes)


def _reap_loop():
    global _reaper
    while True:
        time.sleep(_REAP_INTERVAL)
        engine = _pip_engine()
        with _lock:
            live = _all_live_locked()
            # A pair with no session left is still the engine's to close after its linger.
            if not live and engine.idle_locked():
                _reaper = None
                return
        for s in live:
            try:
                _check(s)
            except Exception:                           # noqa: BLE001 - the loop must outlive one session's failure
                log.exception('Viewer %s: reaper check failed', s.id)
        try:
            engine.tick()
        except Exception:                               # noqa: BLE001 - the loop must outlive the engine's failure
            log.exception('Picture in picture: the engine\'s turn failed')


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


# ── The waiting screen ─────────────────────────────────────────────────────────────────────
#
# A sports channel with no game to play shows its waiting screen instead of refusing the play
# (dev/changelog/1534, the picture app/sports_screen.py): ChannelBin's own clip, looped by an
# ffmpeg that writes into the same ring through the same reader, so idle, max duration, the
# IPTV player leaving and every other end work as they do for a relay. It takes no provider
# seat and is not counted against max_viewers (the play still shows on Watching now). Every
# SCREEN_RECHECK_SECONDS it asks again: in proxy mode a game that has come on takes a slot and a
# seat and is put on in place of the clip, as a failover puts on a member; a game that cannot
# get one, or words that changed, swap the clip in place. One swap at a time per play
# (`_swap_lock`), never under `_lock`, never inside a database unit.

def _screen_mod():
    from . import sports_screen
    return sports_screen


def _screen_of(play: Play, cfg: dict, held: str = None, other_login: bool = False):
    """The waiting screen `play` shows now. A few queries (sports_screen.screen_for).
    `other_login` is a play on a login for another user, which is always relayed (relays_for)."""
    fcfg = feed_config(cfg)
    before, _after = sports_window(fcfg)
    # The screen's words follow how this play is delivered, so mixed mode's sports channel,
    # which is relayed, says the game starts by itself.
    mode = (MODE_PROXY if play.preview or other_login or relays(fcfg, play.feed_id)
            else MODE_REDIRECT)
    return _screen_mod().screen_for(play.served, play.found, datetime.utcnow(), cfg, mode=mode,
                                    before_minutes=int(before.total_seconds() // 60), held=held,
                                    slots=int(fcfg.get('max_viewers') or 1))


def _playing_clips() -> set:
    with _lock:
        return {s.screen_path for s in _sessions.values() if s.live and s.screen_path}


def _prune_clips(cfg: dict) -> None:
    """Prune the clip folder before a make, the one thing that adds to it, keeping every clip a
    live play is on. So the folder needs no job of its own."""
    try:
        _screen_mod().prune_clips(cfg, keep=_playing_clips())
    except OSError as exc:
        log.warning('Could not prune the waiting-screen clips: %s', exc)


def _clip_for(cfg: dict, row_id: int, screen) -> tuple:
    """(path, still) for `screen`: its moving clip when it is made, else the quick still, made
    now (under a second, dev/changelog/1533) for the play to start on while the moving one is
    made behind it. Raises sports_screen.ClipFailed."""
    ss = _screen_mod()
    path = ss.ready_clip(cfg, row_id, screen)
    if path:
        return path, False
    _prune_clips(cfg)
    return ss.make_clip(cfg, row_id, screen, still=True), True


def _set_screen(session: RelaySession, screen, path: str, still: bool, held: str = None) -> None:
    ss = _screen_mod()
    key = ss.clip_key(screen)
    with _lock:
        session.screen_path, session.screen_key, session.screen_still = path, key, still
        if isinstance(screen, ss.Rotation):
            return
        session.screen_state, session.screen_held = screen.state, held
        session.waiting_for = screen.game.name if screen.game is not None else ''


def _note_badges(session: RelaySession, screen) -> None:
    """Say on the play, once, each badge its screen draws as the team's code (the draw warns in
    the log at every clip made, dev/changelog/1587). Called only once the clip is playing, and
    nothing it raises leaves here: it decodes a downloaded file, and a diagnostic must never be
    able to cost the play its screen."""
    try:
        unread = _screen_mod().unread_badges(screen)
    except Exception as exc:                            # noqa: BLE001 - a note, never the play
        log.warning('Viewer %s: could not ask whether the badges of %s can be read: %s', session.id,
                    session.label, exc)
        return
    what = (session.own_picture_name or 'the picture').removeprefix('the ')
    for name, code in unread:
        _note_once(session, f'The badge for {name} could not be read as a picture, so the {what} '
                            f'shows "{code}" in its place.')


def start_waiting(play: Play, client: str, cfg: dict, held: str = None) -> RelaySession:
    """Start `play`, a sports play, on its channel's waiting screen. `held` (a
    sports_screen.HELD_* reason) is a game that is on and cannot start here yet. Requires an app
    context. Raises FeedRefused, naming why, when the screen cannot be made or played: the play
    is then refused as it was before the screen existed (storage_dirs.SPORTS_SCREEN_ROLE)."""
    from flask import current_app
    ss = _screen_mod()
    fcfg = feed_config(cfg)
    row_id = play.served.row.id
    try:
        tuning = _tuning_login.get()
        screen = _screen_of(play, cfg, held, other_login=tuning is not None and not tuning.is_main)
        path, still = _clip_for(cfg, row_id, screen)
    except ss.ClipFailed as exc:
        from .sports_feed import event_word
        why = play.why_none or f'the {event_word(play.served.league)} cannot start here yet'
        message = f'Nothing to play for {play.label}: {why}. Its waiting screen could not be shown: {exc}.'
        _note_refused(play.feed_id, play.label, client, message)
        log.warning('Viewer from %s refused: %s', client, message)
        raise FeedRefused(message, 409)
    session = RelaySession(
        id=secrets.token_urlsafe(12), feed_id=play.feed_id, label=play.label, client=client,
        group_id=None, channel_id=None, channel_name='', account_id=None,
        ring=Ring(int(fcfg.get('buffer_mb') or 64) * 1024 * 1024, 0.0),
        **_session_limits(cfg, fcfg), started_mono=time.monotonic(), state=STATE_WAITING,
        fails_over=True, app=current_app._get_current_object(), sports_row_id=row_id,
        preview=play.preview)
    session.last_recheck_mono = session.started_mono
    _set_screen(session, screen, path, still, held)
    with _lock:
        _sessions[session.id] = session
        _prune_ended_locked()
    try:
        _launch_clip(session, path, cfg)
    except FeedRefused as exc:
        _finish(session, REASON_LAUNCH_FAILED, detail=exc.message)
        raise
    _note_badges(session, screen)
    if still:
        _make_moving_later(session, cfg, screen, held)
    _ensure_reaper()
    log.info('Viewer %s from %s: %s on the waiting screen (%s%s)%s', session.id, client, session.label,
             screen.state, f', {held}' if held else '', f', waiting for {session.waiting_for}'
             if session.waiting_for else '')
    return session


def _launch_clip(session: RelaySession, path: str, cfg: dict) -> None:
    """Start the ffmpeg that loops `path` into the session's ring. Raises FeedRefused."""
    cmd = build_screen_play_cmd(cfg, path)
    try:
        fd, session.stderr_path = tempfile.mkstemp(prefix='channelbin-relay-', suffix='.stderr')
        os.close(fd)
        # stderr to a file, never a pipe (dev/changelog/430).
        with open(session.stderr_path, 'wb') as err:
            proc = subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=err)
    except OSError as exc:
        _remove_stderr(session)
        what = session.own_picture_name or 'the picture'
        log.error('Viewer %s: could not start %s: %s', session.id, what, exc)
        raise FeedRefused(f'Could not start {what}: {exc}', 500)
    if _adopt(session, proc) is None:
        raise FeedRefused(REASON_TEXT[REASON_CLIENT_GONE], 409)


def _stop_current(session: RelaySession) -> None:
    """Kill what the session is playing and drain its tail into the ring, so two sources' bytes
    never interleave at a swap (the failover's order)."""
    with _lock:
        old_proc, old_reader = session.proc, session.reader
        session.proc = session.reader = None
        session.last_data_mono = None
    if old_proc is not None:
        terminate_or_kill(old_proc, hard=True)
    if old_reader is not None and old_reader is not threading.current_thread():
        old_reader.join(timeout=5.0)
    _remove_stderr(session)


def _discard_unplayed(path: str) -> None:
    """Delete a clip the play just left because something newer replaced it, unless another live
    play is on it. A clip that is still a channel's current screen is never passed here."""
    if not path:
        return
    with _lock:
        playing = any(s.live and s.screen_path == path for s in _sessions.values())
    if not playing:
        _screen_mod().discard_clip(path)


def _swap_clip(session: RelaySession, screen, path: str, still: bool, cfg: dict, held: str = None) -> bool:
    """Put `path` on in place of the clip playing. The caller holds `_swap_lock`. False when the
    play ended instead."""
    old = session.screen_path
    _stop_current(session)
    _set_screen(session, screen, path, still, held)
    try:
        _launch_clip(session, path, cfg)
    except FeedRefused as exc:
        if session.live:
            _finish(session, _clip_failed_reason(session), detail=exc.message)
        return False
    if old != path:
        _discard_unplayed(old)
    _note_badges(session, screen)
    if session.showing:
        log.info('Viewer %s: %s now shows its new picture%s', session.id, session.label,
                 ', the quick still' if still else '')
    elif session.on_card:
        log.info('Viewer %s: the card for %s now says "%s"%s', session.id, session.label,
                 screen.words.headline, ', the quick still' if still else '')
    elif session.waiting:
        log.info('Viewer %s: the waiting screen for %s now shows %s%s%s', session.id, session.label,
                 screen.state, f' ({held})' if held else '', ', the quick still' if still else '')
    return True


def _make_moving_later(session: RelaySession, cfg: dict, screen, held: str = None) -> None:
    """Make the moving clip of `screen` behind the quick still the play is on, at a low priority,
    and swap it in when it is made, if the play still shows that screen. The maker's ffmpeg is
    sports_screen's to stop (stop_makes(), from stop_all())."""
    def _make():
        ss = _screen_mod()
        what = (session.own_picture_name or 'the picture').removeprefix('the ')
        try:
            path = ss.make_clip(cfg, _clip_owner(session), screen, background=True)
        except ss.ClipFailed as exc:
            log.warning('Viewer %s: the moving %s for %s could not be made, so the still one stays on: '
                        '%s', session.id, what, session.label, exc)
            _note_once(session, f'The moving {what} could not be made, so the still one stayed on ({exc}).')
            return
        key = ss.clip_key(screen)
        with session._swap_lock:
            with _lock:
                current = session.own_picture and session.screen_still and session.screen_key == key
            if current:
                _swap_clip(session, screen, path, False, cfg, held)
    threading.Thread(target=_make, daemon=True, name=f'relay-screen-{session.id}').start()


def _clip_owner(session: RelaySession):
    """Whose clips a play of ChannelBin's own picture plays: its Up next channel's, its
    picture-in-picture channel's, or its sports channel's row."""
    if session.up_next_number is not None:
        return _screen_mod().up_next_owner(session.up_next_number)
    if session.pip_row_id is not None:
        return _screen_mod().pip_owner(session.pip_row_id)
    return session.sports_row_id


# ── An Up next channel ─────────────────────────────────────────────────────────────────────
#
# Always ChannelBin's own picture (app/sports_up_next.py, dev/changelog/1614), looped by the
# same ffmpeg shape and reader as a waiting screen, so every end works as it does for a relay.
# Its clip is made on tune-in, never ahead (dev/changelog/1615): the play starts on the quick
# still of its first page while the moving clip is made behind it. Every SCREEN_RECHECK_SECONDS the play asks again,
# and swaps in the new picture when what it shows has changed.

def _rotation_of(play_or_ch, cfg: dict):
    from .sports_up_next import rotations
    ch = play_or_ch.up_next if isinstance(play_or_ch, Play) else play_or_ch
    return rotations([ch], datetime.utcnow(), cfg)[ch.number]


def start_showing(play: Play, client: str, cfg: dict) -> RelaySession:
    """Start `play`, an Up next channel's, on its picture. Requires an app context. Raises
    FeedRefused, naming why, when the picture cannot be made or played."""
    from flask import current_app
    ss = _screen_mod()
    fcfg = feed_config(cfg)
    number = play.up_next.number
    owner = ss.up_next_owner(number)
    try:
        rotation = _rotation_of(play, cfg)
        path, still = _clip_for(cfg, owner, rotation)
    except ss.ClipFailed as exc:
        message = f'{play.up_next.name} could not be shown: {exc}.'
        _note_refused(play.feed_id, play.label, client, message)
        log.warning('Viewer from %s refused: %s', client, message)
        raise FeedRefused(message, 409)
    session = RelaySession(
        id=secrets.token_urlsafe(12), feed_id=play.feed_id, label=play.label, client=client,
        group_id=None, channel_id=None, channel_name='', account_id=None,
        ring=Ring(int(fcfg.get('buffer_mb') or 64) * 1024 * 1024, 0.0),
        **_session_limits(cfg, fcfg), started_mono=time.monotonic(), state=STATE_SHOWING,
        app=current_app._get_current_object(), up_next_number=number, preview=play.preview)
    session.last_recheck_mono = session.started_mono
    _set_screen(session, rotation, path, still)
    with _lock:
        _sessions[session.id] = session
        _prune_ended_locked()
    try:
        _launch_clip(session, path, cfg)
    except FeedRefused as exc:
        _finish(session, REASON_LAUNCH_FAILED, detail=exc.message)
        raise
    _note_badges(session, rotation)
    if still:
        _make_moving_later(session, cfg, rotation)
    _ensure_reaper()
    log.info('Viewer %s from %s: %s on its picture (%s)%s', session.id, client, session.label,
             counted(len(rotation.pages), 'page'), ', the quick still while the moving one is made' if still else '')
    return session


def _recheck_showing(session: RelaySession) -> None:
    """An Up next play's turn to ask again: ended by name when the channel is no longer served,
    else the new picture swapped in when what it shows has changed."""
    from .config import load_config
    ss = _screen_mod()
    cfg = load_config()
    play = resolve_play(session.feed_id, cfg, preview=session.preview)
    if play is None or play.up_next is None:
        _finish(session, REASON_TAKEN_OFF)
        return
    rotation = _rotation_of(play, cfg)
    if ss.clip_key(rotation) == session.screen_key:
        return
    try:
        path, still = _clip_for(cfg, ss.up_next_owner(session.up_next_number), rotation)
    except ss.ClipFailed as exc:
        log.warning('Viewer %s: %s keeps its picture, its new one could not be made: %s', session.id,
                    session.label, exc)
        _note_once(session, f'The Up next picture could not be redrawn, so it kept what it showed ({exc}).')
        return
    if _swap_clip(session, rotation, path, still, cfg) and still:
        _make_moving_later(session, cfg, rotation)


def recheck_waiting(held_only: bool = False) -> int:
    """Ask every play on a waiting screen or a picture-in-picture card again now rather than at its next turn: Start broadcast
    opened a game (app/routes/sports.py), or a play that held a slot ended (`held_only`, the
    plays whose game is on and waiting for one). No database; the reaper asks within
    _REAP_INTERVAL. How many plays were asked."""
    with _lock:
        waiting = [s for s in _sessions.values() if s.live and (s.waiting or s.on_card)
                   and (s.screen_held or not held_only)]
        for s in waiting:
            s.recheck_due = True
    _pip_engine().ask_all()
    return len(waiting)


def _clip_failed_reason(session: RelaySession) -> str:
    """The end a play of ChannelBin's own clip gets when the clip stops playing, by the kind
    of play it is (own_picture_name's reason: its state may move under the caller)."""
    if session.up_next_number is not None:
        return REASON_PICTURE_FAILED
    if session.pip_row_id is not None:
        return REASON_CARD_FAILED
    return REASON_SCREEN_FAILED


def _check_waiting(session: RelaySession, proc: subprocess.Popen, now: float) -> None:
    """The reaper's turn for a play of ChannelBin's own clip, a waiting screen or an Up next
    channel: the clip's ffmpeg is alive and sending, and every SCREEN_RECHECK_SECONDS (or when
    asked) the play asks again on a thread of its own, so a clip made on the way never holds up
    the reaper."""
    failed = _clip_failed_reason(session)
    rc = proc.poll()
    if rc is not None:
        tail = read_stderr_tail(session.stderr_path)
        _finish(session, failed, detail=f'ffmpeg exited with code {rc}' + (f': {tail}' if tail else ''))
        return
    if session.last_data_mono is None:
        if now - session.launched_mono > CONNECT_TIMEOUT_SECONDS:
            _finish(session, failed, detail='the clip sent nothing in time')
            return
    elif now - session.last_data_mono > session.stall_timeout:
        _finish(session, failed, detail=f'no data for {session.stall_timeout:.0f}s')
        return
    if session.app is None:
        return
    with _lock:
        due = session.recheck_due or now - session.last_recheck_mono >= SCREEN_RECHECK_SECONDS
        if not due or session._rechecking or not session.live:
            return
        session._rechecking = True
        session.recheck_due = False
        session.last_recheck_mono = now
    threading.Thread(target=_recheck, args=(session,), daemon=True,
                     name=f'relay-recheck-{session.id}').start()


def _recheck(session: RelaySession) -> None:
    try:
        with session.app.app_context():
            with session._swap_lock:
                if session.waiting:
                    _recheck_locked(session)
                elif session.showing:
                    _recheck_showing(session)
                elif session.on_card:
                    _recheck_card(session)
    except Exception:                                   # noqa: BLE001 - one play's re-check, never the feed
        log.exception('Viewer %s: %s could not ask again what it shows', session.id,
                      session.own_picture_name or session.label)
    finally:
        with _lock:
            session._rechecking = False


def _recheck_locked(session: RelaySession) -> None:
    from .alerts import create_alert, has_open_alert
    from .config import load_config
    ss = _screen_mod()
    cfg = load_config()
    fcfg = feed_config(cfg)
    play = resolve_play(session.feed_id, cfg, preview=session.preview)
    if play is None or not play.sports:
        _finish(session, REASON_SCREEN_GONE)
        return
    held = None
    if (play.member is not None and not session.preview and session.feed_login_id is None
            and not relays(fcfg, session.feed_id)):
        # A redirected play cannot put the game on in place: ChannelBin never sends a provider
        # stream through itself for it. The stream ends, and the player's next tune-in is the
        # 302 to the game (section 5.1).
        _finish(session, REASON_WINDOW_OPENED, detail=play.note)
        return
    if play.member is not None:
        held = _to_game(session, play, cfg, fcfg)
        if held is None:
            return
    elif play.fault:
        # The game is on, and listed only where the feed may not play from: what a tune-in
        # is refused for with FEED_NO_MEMBER, so the play ends the same way.
        detail = f'{play.why_none}'
        if _finish(session, REASON_NO_MEMBER, detail=detail):
            source = f'feed:{session.feed_id}:no-member'
            if not has_open_alert('FEED_NO_MEMBER', source):
                create_alert('FEED_NO_MEMBER', f'IPTV output: nothing to play for {play.label}',
                             body=f'An IPTV player on the waiting screen of {play.label} was cut: {detail}.',
                             source=source)
        return
    screen = _screen_of(play, cfg, held, other_login=session.feed_login_id is not None)
    if ss.clip_key(screen) == session.screen_key:
        return
    try:
        path, still = _clip_for(cfg, play.served.row.id, screen)
    except ss.ClipFailed as exc:
        log.warning('Viewer %s: the waiting screen for %s stays as it is, its new one could not be '
                    'made: %s', session.id, session.label, exc)
        _note_once(session, f'The waiting screen could not be redrawn, so it kept what it showed '
                            f'({exc}).')
        return
    if _swap_clip(session, screen, path, still, cfg, held) and still:
        _make_moving_later(session, cfg, screen, held)


def _to_game(session: RelaySession, play: Play, cfg: dict, fcfg: dict) -> Optional[str]:
    """The game is on: take a slot under the one lock (start_session()'s count), then a seat,
    then put the game on in place of the clip. None when the play moved to the game, or ended on
    the way; else the sports_screen.HELD_* reason it stays on the screen for."""
    ss = _screen_mod()
    cap = int(fcfg.get('max_viewers') or 1)
    with _lock:
        if not session.live:
            return None
        if sum(1 for s in _sessions.values() if s.counted and s is not session) >= cap:
            return ss.HELD_NO_SLOT
        session.slot_claimed = True
    try:
        member = _seat_for(session, play.member, play.event_id, cfg)
        if member is None:
            return ss.HELD_NO_CONNECTION
        if not _own_seat(session, member):
            return ss.HELD_NO_CONNECTION if session.live else None
        _put_on_game(session, play, member, cfg)
        return None
    finally:
        with _lock:
            session.slot_claimed = False


def _own_seat(session: RelaySession, member) -> bool:
    """Name the account on a waiting play if the seat _seat_for() took is still its, in one hold
    of the lock. A play on the screen has no account, so a recording that takes the seat between
    the acquire and here finds no play to end (preempt_for_account() matches on the account) and
    the play would go on to start the game on a connection the recording holds, counted nowhere
    (dev/docs/BUGS.md 2026-11-15 01:36 PM). Named and seated together, a recording from here on
    finds the play; a seat already gone leaves the play unnamed, on its screen. Nothing in
    connection_limits calls back into this module under its own lock, so taking its lock inside
    this one cannot deadlock."""
    with _lock:
        ending = not session.live or session._finishing
        seated = not ending and session.id in connlim.seated_holder_ids('viewer')
        if seated:
            session.account_id = member.account_id
    if ending:
        connlim.release(member.account_id, 'viewer', session.id)
    elif not seated:
        log.warning('Viewer %s: a recording took the connection on account %d as %s was coming on, so '
                    'the play stays on the waiting screen', session.id, member.account_id, session.label)
    return seated


def _pip_engine():
    from . import pip_engine
    return pip_engine


def viewer_give_way() -> dict:
    """{a 'viewer' seat holder's id: connection_limits.VIEWER_*} for the holders that give a
    recording their seat before a watched play does: a picture-in-picture channel's pictures
    no TV has as its big one (pip_engine.give_way, dev/docs/DESIGN-pip-channels.md 8.2)."""
    return _pip_engine().give_way()


def _resolve_again(session: RelaySession, cfg: dict, exclude_ids, event_id=None) -> Optional[Play]:
    """What `session` plays now with `exclude_ids` left out: the one re-ask of a seat walk, a
    failover and a move. A plain play asks its feed id; a picture-in-picture picture asks its
    own source (pip_engine.Picture.resolve), which a feed id does not name."""
    if session.picture is not None:
        return session.picture.resolve(cfg, exclude_ids, event_id, session.id)
    return resolve_play(session.feed_id, cfg, exclude_ids=exclude_ids, event_id=event_id,
                        viewer_id=session.id, preview=session.preview)


def _all_live_locked() -> list:
    """Every live session, the plays and the picture-in-picture pictures. Caller holds _lock."""
    return [s for s in list(_sessions.values()) + list(_internal.values()) if s.live]


def _seat_for(session: RelaySession, member, event_id, cfg: dict, refused=()):
    """The best channel that can play what the session asked for whose account has a seat free,
    with the seat taken, starting at `member` and going down the ranking; None when every one is
    out of connections. `refused` are the ones already refused a seat, which are not asked again."""
    tried = {m.id for m in refused}
    while member is not None:
        if member.id not in tried and connlim.try_acquire(member.account_id, 'viewer', session.id):
            return member
        tried.add(member.id)
        more = _resolve_again(session, cfg, frozenset(tried | session.failed_ids), event_id)
        member = more.member if more is not None else None
    return None


def _put_on_game(session: RelaySession, play: Play, member, cfg: dict) -> None:
    """Swap the clip for `member`, whose seat is already taken, in place - the TV sees an MPEG-TS
    discontinuity, as at a failover (section 4.5)."""
    old = session.screen_path
    _stop_current(session)
    now = time.monotonic()
    with _lock:
        if not session.live or session._finishing:
            alive = False
        else:
            alive = True
            session.screen_seconds = session._screen_seconds(now)
            session._screen_since = 0.0
            session.card_state = session.card_text = ''
            session.state = STATE_STARTING
            session.slot_claimed = False
            session.left_screen_at = datetime.utcnow()
            session.account_id = member.account_id   # the seat is held: _launch keeps it
            session.event_id = play.event_id
            session.group_id = play.group.id if play.group is not None else None
            session.label = play.label
            session.screen_path = ''
    if not alive:
        connlim.release(member.account_id, 'viewer', session.id)
        return
    session.ring.set_reserve(session.reserve_seconds)
    try:
        _launch(session, member, cfg, first=False)
    except FeedRefused as exc:
        if session.live:
            _finish(session, REASON_LAUNCH_FAILED, detail=exc.message)
        return
    _discard_unplayed(old)
    log.info('Viewer %s: %s came on - moved from %s to channel %d (%s) on account '
             '%d after %s%s', session.id, session.label,
             CARD_NAME if session.pip_row_id is not None else SCREEN_NAME, member.id, member.name,
             member.account_id,
             fmt_duration(session.screen_seconds), f' ({play.note})' if play.note else '')


# ── A picture-in-picture channel's card ────────────────────────────────────────────────────
#
# What a play of a picture-in-picture channel is on while its big picture has nothing to play
# (app/pip_channels.py, dev/docs/DESIGN-pip-channels.md 6.4, 8.1): nothing set up, a game not on
# yet or over, a search with no match, every account busy. The waiting screen's machinery, in a
# state of its own (STATE_CARD): a looped clip, no seat, not counted. Every
# SCREEN_RECHECK_SECONDS, and the moment a seat or a slot frees, the play asks again and puts
# the picture on in the card's place.

def _pip_mod():
    from . import pip_channels
    return pip_channels


def _card_of(play: Play, cfg: dict, held: str = None):
    """The pip_channels.Card `play` shows now. `held` (a sports_screen.HELD_* reason) is a
    picture that could play and cannot start here yet."""
    return _pip_mod().card_for(play.pip, cfg, held=held,
                               slots=int(feed_config(cfg).get('max_viewers') or 1))


def _set_card(session: RelaySession, card, path: str, still: bool, held: str = None) -> None:
    _set_screen(session, card.screen, path, still, held)
    with _lock:
        session.card_state, session.card_text = card.state, card.text


def start_card(play: Play, client: str, cfg: dict, held: str = None) -> RelaySession:
    """Start `play`, a picture-in-picture channel's, on its card. Requires an app context.
    Raises FeedRefused, naming why, when the card cannot be made or played."""
    from flask import current_app
    ss = _screen_mod()
    fcfg = feed_config(cfg)
    card = _card_of(play, cfg, held)
    try:
        path, still = _clip_for(cfg, ss.pip_owner(play.pip.row_id), card.screen)
    except ss.ClipFailed as exc:
        message = f'Nothing to play for {play.label}: {card.text}. Its card could not be shown: {exc}.'
        _note_refused(play.feed_id, play.label, client, message)
        log.warning('Viewer from %s refused: %s', client, message)
        raise FeedRefused(message, 409)
    session = RelaySession(
        id=secrets.token_urlsafe(12), feed_id=play.feed_id, label=play.label, client=client,
        group_id=None, channel_id=None, channel_name='', account_id=None,
        ring=Ring(int(fcfg.get('buffer_mb') or 64) * 1024 * 1024, 0.0),
        **_session_limits(cfg, fcfg), started_mono=time.monotonic(), state=STATE_CARD,
        fails_over=True, app=current_app._get_current_object(), pip_row_id=play.pip.row_id,
        preview=play.preview)
    session.last_recheck_mono = session._screen_since = session.started_mono
    _set_card(session, card, path, still, held)
    with _lock:
        _sessions[session.id] = session
        _prune_ended_locked()
    try:
        _launch_clip(session, path, cfg)
    except FeedRefused as exc:
        _finish(session, REASON_LAUNCH_FAILED, detail=exc.message)
        raise
    if still:
        _make_moving_later(session, cfg, card.screen, held)
    _ensure_reaper()
    log.info('Viewer %s from %s: %s on its card (%s: %s)', session.id, client, session.label, card.state,
             card.text)
    return session


def _recheck_card(session: RelaySession) -> None:
    """A card play's turn to ask again: ended by name when its channel is no longer served, the
    picture put on in the card's place when it can play now, else the card redrawn when what it
    says has changed. The caller holds `_swap_lock`."""
    from .config import load_config
    ss = _screen_mod()
    cfg = load_config()
    fcfg = feed_config(cfg)
    # A card that followed every channel failing tries them all again: the failures are at
    # least a turn old, and nothing else would ever bring the picture back.
    session.failed_ids.clear()
    play = resolve_play(session.feed_id, cfg, viewer_id=session.id, preview=session.preview)
    if play is None or play.pip is None:
        _finish(session, REASON_TAKEN_OFF)
        return
    held = None
    device = ''
    if play.pip.set_up and not session.combine_off:
        device, why = _pip_engine().gpu_state(cfg)
        if not device:
            with _lock:
                session.combine_off = why
    if device:
        moved, held = _to_combined(session, play, cfg, fcfg, device)
        if moved:
            return
    elif play.member is not None:
        held = _to_game(session, play, cfg, fcfg)
        if held is None:
            return
    card = _card_of(play, cfg, held)
    if ss.clip_key(card.screen) == session.screen_key:
        with _lock:
            session.card_state, session.card_text = card.state, card.text
        return
    try:
        path, still = _clip_for(cfg, ss.pip_owner(session.pip_row_id), card.screen)
    except ss.ClipFailed as exc:
        log.warning('Viewer %s: the card for %s stays as it is, its new one could not be made: %s',
                    session.id, session.label, exc)
        _note_once(session, f'Its card could not be redrawn, so it kept what it showed ({exc}).')
        return
    if _swap_clip(session, card.screen, path, still, cfg, held):
        with _lock:
            session.card_state, session.card_text = card.state, card.text
        if still:
            _make_moving_later(session, cfg, card.screen, held)


def _to_combined(session: RelaySession, play: Play, cfg: dict, fcfg: dict, device: str) -> tuple:
    """A play on its card asks to play its two pictures combined: a slot under the one lock
    (start_session()'s count), then the pictures. (True, None) when it left the card, or ended
    on the way; (False, the sports_screen.HELD_* reason it stays for, None when neither picture
    has anything to play). The caller holds `_swap_lock`."""
    ss = _screen_mod()
    cap = int(fcfg.get('max_viewers') or 1)
    with _lock:
        if not session.live:
            return True, None
        if sum(1 for s in _sessions.values() if s.counted and s is not session) >= cap:
            return False, ss.HELD_NO_SLOT if play.member is not None else None
        session.slot_claimed = True
    try:
        if not _pip_engine().begin(session, play.pip, cfg, device, wait=False):
            return False, ss.HELD_NO_CONNECTION if play.member is not None else None
        old = session.screen_path
        _stop_current(session)
        now = time.monotonic()
        with _lock:
            alive = session.live and not session._finishing
            if alive:
                session.screen_seconds = session._screen_seconds(now)
                session._screen_since = 0.0
                session.card_state = session.card_text = ''
                session.screen_held = None
                session.state = STATE_STARTING
                session.left_screen_at = datetime.utcnow()
                session.label = f'picture-in-picture channel {play.pip.row_id} ({play.pip.name})'
                session.screen_path = ''
        if alive:
            session.ring.set_reserve(session.reserve_seconds)
            _discard_unplayed(old)
            log.info('Viewer %s: %s left its card for its two pictures combined after %s', session.id,
                     session.label, fmt_duration(session.screen_seconds))
        return True, None
    finally:
        with _lock:
            session.slot_claimed = False


def _put_on_card(session: RelaySession, play: Play, cfg: dict, held: str = None) -> bool:
    """Put `session`, a picture-in-picture play whose picture has nothing left to play, on its
    card in place: the TV sees an MPEG-TS discontinuity, as at a failover. The caller has
    stopped its ffmpeg; its seat is given back here. False when the play ended instead."""
    ss = _screen_mod()
    # A play that was on its two pictures combined leaves them: a card is one clip, and its
    # pictures' connections go back (dev/docs/DESIGN-pip-channels.md 10).
    _pip_engine().leave(session)
    card = _card_of(play, cfg, held)
    try:
        path, still = _clip_for(cfg, ss.pip_owner(session.pip_row_id), card.screen)
    except ss.ClipFailed as exc:
        _finish(session, REASON_NO_MEMBER, detail=f'{card.text}, and its card could not be shown: {exc}')
        return False
    now = time.monotonic()
    with _lock:
        if not session.live or session._finishing:
            return False
        account_id = session.account_id
        _close_phase_locked(session)
        session.state = STATE_CARD
        session.account_id = session.channel_id = session.group_id = session.event_id = None
        session.channel_name = ''
        session.label = play.label
        session.preempted, session.preempted_by = False, None
        session.last_data_mono = None
        session._screen_since = session.last_recheck_mono = now
    connlim.release(account_id, 'viewer', session.id)
    # A card plays on no reserve, as a waiting screen does; the picture gets it back.
    session.ring.set_reserve(0.0)
    _set_card(session, card, path, still, held)
    try:
        _launch_clip(session, path, cfg)
    except FeedRefused as exc:
        if session.live:
            _finish(session, REASON_CARD_FAILED, detail=exc.message)
        return False
    if still:
        _make_moving_later(session, cfg, card.screen, held)
    # The seat it held is free for a play waiting on one.
    recheck_waiting(held_only=True)
    log.info('Viewer %s: %s now shows its card (%s: %s)', session.id, session.label, card.state, card.text)
    return True


def _to_card_now(session: RelaySession) -> None:
    """Take a play off the picture it is on and onto its channel's card: the channel was
    cleared while it was watched (clear_pip). On a thread of its own."""
    from .config import load_config
    try:
        # A play inside its launch window, or between channels, has no ffmpeg to take yet: it is
        # asked again until it has one, so it never goes on playing a picture that was cleared.
        deadline = time.monotonic() + CONNECT_TIMEOUT_SECONDS
        while True:
            with _lock:
                over = not session.live or session._finishing or session.own_picture
                free = (not over and not session._failing_over and not session._moving
                        and session.proc is not None)
            if over or free or time.monotonic() >= deadline:
                break
            time.sleep(_SWITCH_POLL)
        if over:
            # Ended, or already on its card: a card asks the channel again by itself.
            with _lock:
                session.recheck_due = True
            return
        with session.app.app_context(), session._swap_lock:
            with _lock:
                free = (session.live and not session.own_picture and not session._failing_over
                        and not session._moving and not session._finishing and session.proc is not None)
                if free:
                    session._carding = True
                    proc, reader = session.proc, session.reader
                    session.proc = session.reader = None
            if not free:
                log.warning('Viewer %s: %s was cleared and could not be moved to its card: it is still '
                            'changing what it plays', session.id, session.label)
                return
            try:
                _stop_detached(session, proc, reader)
                cfg = load_config()
                play = resolve_play(session.feed_id, cfg, preview=session.preview)
                if play is None or play.pip is None:
                    _finish(session, REASON_TAKEN_OFF)
                elif not _put_on_card(session, play, cfg) and session.live:
                    _finish(session, REASON_CARD_FAILED)
            finally:
                with _lock:
                    session._carding = False
    except Exception as exc:                            # noqa: BLE001 - one play, never left with no ffmpeg
        log.exception('Viewer %s: %s could not be moved to its card', session.id, session.label)
        _finish(session, REASON_CARD_FAILED, detail=mask_creds_in_text(str(exc)))


# ── Redirect mode ──────────────────────────────────────────────────────────────────────────

def redirect_play(feed_id: int, client: str, cfg=None, login: Optional[FeedLogin] = None) -> tuple:
    """Redirect mode's answer to a tune-in: (provider URL, None) to send the player to, or
    (None, session) for a sports channel with nothing to play, whose waiting screen is
    ChannelBin's own bytes and so is streamed in this mode too: it never takes a seat and never
    sends a provider login to the player (dev/changelog/1534). It ends when its game's window
    opens (REASON_WINDOW_OPENED), and the next tune-in is the 302. Requires an app context.
    Raises FeedRefused as redirect_target() does. Only the main login is ever redirected
    (relays_for); `login` names it on the lines this writes."""
    from .config import load_config
    cfg = cfg or load_config()
    token = _tuning_login.set(login)
    try:
        play = resolve_play(feed_id, cfg)
        if play is not None and play.sports and play.member is None and not play.fault:
            return None, start_waiting(play, client, cfg)
        return redirect_target(feed_id, client, cfg, play=play), None
    finally:
        _tuning_login.reset(token)


def redirect_target(feed_id: int, client: str, cfg=None, play: Play = None) -> str:
    """The provider URL an IPTV player is sent to for `feed_id` right now (section 5.1), recorded as
    a redirect for the dashboard. Raises FeedRefused on an id the feed does not serve (404)
    or a group with nothing playable (409, with the FEED_NO_MEMBER alert). `play` is the
    caller's resolve_play() answer when it already has one."""
    play = play or resolve_play(feed_id, cfg)
    if play is None:
        raise FeedRefused('No such channel on the IPTV output', 404)
    if play.member is None and not play.fault:
        _refuse_nothing_on(play, client)
    if play.member is None:
        _refuse(feed_id, play.label, f'Nothing to play for {play.label}: {play.why_none}.',
                409, 'no-member', client)
    member = play.member
    # A redirect is one moment, so its record may say where that moment fell (play.note).
    label = f'{play.label} ({play.note})' if play.note else play.label
    login = _tuning_log_name()
    with _lock:
        _redirects.append({'at': datetime.utcnow(), 'feed_id': feed_id, 'label': label,
                           'client': client, 'feed_login': login or '', 'channel_id': member.id,
                           'channel_name': member.name, 'account_id': member.account_id})
        del _redirects[:-_KEEP_ENDED]
    _queue_play(None, {'kind': FEED_PLAY_REDIRECTED, 'at': datetime.utcnow(), 'feed_id': feed_id,
                       'label': label, 'client': client, 'feed_login': login, 'channel_id': member.id,
                       'channel_name': member.name, 'account_id': member.account_id})
    log.info('Viewer from %s: %s redirected to channel %d (%s) on account %d', client,
             label, member.id, member.name, member.account_id)
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
    if held and redirects_some(fcfg):
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
    out['recent_refused'] = [{
        'feed_id': r['feed_id'], 'count': r['count'], 'at': iso_or_none(r['at'])}
        for r in recent_refused()]
    out['recent_redirects'] = [{
        'feed_id': r['feed_id'], 'channel_id': r['channel_id'], 'account_id': r['account_id'],
        'at': iso_or_none(r['at'])} for r in recent_redirects()]
    return out


def reset_for_tests():
    """Stop everything and forget every session. tests/support/app.py only."""
    global _last_prune_mono
    stop_all(REASON_SHUTDOWN)
    _pip_engine().reset_for_tests()
    wait_for_reaper()
    with _lock:
        _sessions.clear()
        _internal.clear()
        _refused.clear()
        _login_starting.clear()
        _login_changes.clear()
        del _redirects[:]
    discard_play_log()
    _last_prune_mono = None
