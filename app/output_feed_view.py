"""What the IPTV Output page shows (templates/iptv_output.html, mockup 53, dev/changelog/1526).

One function, page_context(), and a fixed number of queries whatever the guide holds: the
catalog's own, the accounts, the sports channels' games, the listings on now for the channels
being watched, one page of Recent plays with its counts, and which version of the address
notice was hidden. Nothing here writes; the page's actions are app/routes/iptv_output.py.

The password never reaches this module's output. The Xtream card shows it through the settings
reveal, and the two playlist addresses are handed over as templates with PASSWORD_SLOT where
the password goes, so the page source holds no secret.
"""
import json
import logging
from datetime import datetime, timedelta
from urllib.parse import quote, urlsplit

from flask import current_app, url_for

from . import connection_limits as connlim
from . import output_feed, pip_channels, pip_engine, proxy_trust, public_listener
from . import db
from .config import MASK_SENTINEL, PROXY_FIX_ARGS, public_url
from .database import (FEED_PICTURE_BACK, FEED_PICTURE_STOPPED, FEED_PLAY_ENDED, FEED_PLAY_PICTURE, FEED_PLAY_REDIRECTED, FEED_PLAY_REFUSED, Account,
                       EPGEntry, OutputFeedLogin, OutputFeedPlay, UserPref)
from .auth import lockout_words, steps_words
from .fmt_utils import counted, fmt_bytes, fmt_duration, join_labels, length_words
from .tz_utils import day_label, format_local, get_display_tz, is_24h

log = logging.getLogger(__name__)

STATE_ON = 'on'
STATE_OFF = 'off'
STATE_SETUP = 'setup'       # no username and password yet, so nothing can be turned on

ADDRESS_SET = 'set'
ADDRESS_UNSET = 'unset'
ADDRESS_LOCALHOST = 'localhost'   # unset, and the page was opened on an address only this computer has

#: Where the password goes in a playlist address the page is handed.
PASSWORD_SLOT = '{password}'
#: Lines of Recent plays to a page.
PLAYS_PER_PAGE = 10
#: Rows of the Channels card to a page. The page is cut by static/js/iptv-output.js, over the
#: rows its search and filters leave, so the server draws every row and hides all but the first.
CHANNELS_PER_PAGE = 25

#: The user pref holding which version of the "ChannelBin's address is not set" notice was
#: hidden. A version, not a yes/no, so a release that changes what the notice says raises
#: ADDRESS_NOTICE_VERSION and shows it once more (the group-create intro's pattern,
#: dev/changelog/831). The page is handed both, so this is the one spelling of each.
ADDRESS_NOTICE_PREF = 'iptv_address_notice_hidden_version'
ADDRESS_NOTICE_VERSION = 1

PROXY_UNSEEN = 'unseen'       # a reverse proxy's headers arrive and flask.behind_proxy is off
PROXY_RESTART = 'restart'     # it was turned on and waits for a restart
PROXY_UNLISTED = 'unlisted'   # it is on, and the proxy is not in flask.trusted_proxies
#: Whose word the public port takes for a player's address (_public_proxy).
PUBLIC_PROXY_OFF = 'off'        # nobody's: every player is the proxy's address
PUBLIC_PROXY_ANY = 'any'        # any caller that sends a proxy's headers
PUBLIC_PROXY_LISTED = 'listed'  # only a peer in flask.trusted_proxies
#: The headers a reverse proxy adds to what it passes on.
_PROXY_HEADERS = ('X-Forwarded-For', 'X-Forwarded-Proto', 'X-Forwarded-Host', 'X-Real-IP',
                  'Forwarded')
#: What the Settings page's proxy check plants in X-Forwarded-For: a documentation address
#: (RFC 5737) no visitor can really have, so finding it again says what the proxy did with it.
PROXY_PROBE_ADDRESS = '203.0.113.7'
PROBE_APPENDED = 'appended'   # the proxy added the visitor after the planted value
PROBE_REPLACED = 'replaced'   # the proxy wrote its own value over it
PROBE_PASSED = 'passed'       # the planted value came through as the last one: the proxy passed it on
PROBE_DROPPED = 'dropped'     # no X-Forwarded-For at all: the proxy removed it and sends none
PLAYS_ALL = 'all'

#: What the IPTV Output page's mode setting says under each choice.
MODE_HELP = {
    output_feed.MODE_PROXY: ('ChannelBin plays the channel through itself. It can move a group to another '
                             'channel mid-stream and say why a play stopped.'),
    output_feed.MODE_MIXED: ('Channel groups and sports channels play through ChannelBin, so a group can '
                             'move to another channel mid-stream and a sports channel\'s waiting screen '
                             'turns into the game by itself. Every other channel is sent straight to your '
                             'IPTV provider\'s own stream, which ChannelBin never sees end. An Up next channel '
                             'is ChannelBin\'s own picture and plays through it in every mode.'),
    output_feed.MODE_REDIRECT: ('The IPTV player is sent to your IPTV provider\'s own stream. No move to '
                                'another channel, your provider\'s login reaches the player, and '
                                'ChannelBin never sees the play end.'),
}

#: The mode choice as the IPTV Output page and Settings offer it, in the order shown.
MODE_LABELS = [(output_feed.MODE_PROXY, 'Proxy, everything through ChannelBin'),
               (output_feed.MODE_MIXED, 'Mixed, groups and sports channels through ChannelBin'),
               (output_feed.MODE_REDIRECT, 'Redirect, everything straight from the provider')]

KIND_TEXT = {output_feed.KIND_CHANNEL: 'Channel', output_feed.KIND_GROUP: 'Channel group',
             output_feed.KIND_SPORTS: 'Sports channel', output_feed.KIND_UP_NEXT: 'Up next channel',
             output_feed.KIND_PIP: 'Picture-in-picture channel'}
_PLAY_CHIPS = {FEED_PLAY_ENDED: 'Ended', FEED_PLAY_REFUSED: 'Refused',
               FEED_PLAY_REDIRECTED: 'Tuned in', FEED_PLAY_PICTURE: 'Small picture'}
#: What the Logins card says about the public port (_public()), one per case, each drawn by its
#: own branch in templates/iptv_output.html.
PUBLIC_OPEN = 'open'
PUBLIC_OPEN_NO_URL = 'open-no-url'
PUBLIC_FAILED = 'failed'
PUBLIC_WAITING = 'waiting'
PUBLIC_UNSET = 'unset'
#: Added to the mode's help when the public port is open and the mode is not proxy.
PUBLIC_MODE_NOTE = (' A play on the public port always goes through ChannelBin, whatever this says, '
                    'so your IPTV provider\'s login never reaches a player outside your home network.')
_LOCAL_HOSTS = frozenset({'localhost', '127.0.0.1', '::1'})


def feed_state(fcfg: dict) -> str:
    if not output_feed.credentials_set(fcfg):
        return STATE_SETUP
    return STATE_ON if output_feed.is_enabled(fcfg) else STATE_OFF


def _times(n: int) -> str:
    return 'once' if n == 1 else f'{n} times'


def _address(cfg: dict, host_url: str) -> dict:
    stored = public_url(cfg)
    base = stored or host_url.rstrip('/')
    if stored:
        state = ADDRESS_SET
    elif (urlsplit(base).hostname or '') in _LOCAL_HOSTS:
        state = ADDRESS_LOCALHOST
    else:
        state = ADDRESS_UNSET
    return {'state': state, 'base': base}


def proxy_state(cfg: dict, headers, wired: bool, trusted: bool) -> str:
    """'' when ChannelBin sees this page's visitor as they are, else why it does not. The page
    is on the web UI's port, so this is about that port only. `headers` are the page request's
    own, `wired` is whether this process believes a proxy on the web UI's port
    (proxy_trust.wired(app).web) and `trusted` whether this request's headers were believed
    (proxy_trust.is_trusted). With a proxy in front and not believed, every request looks like
    the proxy's: http where the visitor typed https, and the proxy's address for every IPTV
    player. The headers are only looked for here, never believed."""
    if trusted or not any(headers.get(name) for name in _PROXY_HEADERS):
        return ''
    if wired:
        return PROXY_UNLISTED
    return PROXY_RESTART if (cfg.get('flask') or {}).get('behind_proxy') else PROXY_UNSEEN


def _forwarded_for(value) -> list:
    return [part.strip() for part in str(value or '').split(',') if part.strip()]


def proxy_check(environ, probe: bool) -> dict:
    """What a request arrived with and what believing its proxy would make of it, for the
    Settings page's proxy check, which is a check of the web UI's port. Only the proxy headers are reported, never any other.

    Read from the request as it arrived: with the setting already on, ProxyFix has rewritten
    the environ and kept the originals under 'werkzeug.proxy_fix.orig', which are put back
    before ProxyFix is run once more over a copy. Nothing here is applied to the request.

    `probe` says the browser planted PROXY_PROBE_ADDRESS in X-Forwarded-For, and the answer
    says what the proxy did with it: added the visitor after it (appended), wrote over it
    (replaced), passed it on as the last value (passed), which is what lets a caller pick its
    own address past the login lockout, or sent no X-Forwarded-For at all (dropped), which
    leaves every visitor looking like the proxy even with the setting on. A request with no proxy in front also reads as
    passed, so the page asks without the probe first and believes the probe only when that
    first answer saw a proxy."""
    from werkzeug.middleware.proxy_fix import ProxyFix
    from werkzeug.wrappers import Request

    arrived = dict(environ)
    arrived.update(environ.get('werkzeug.proxy_fix.orig') or {})
    arrived.pop('werkzeug.proxy_fix.orig', None)
    headers = Request(arrived).headers
    found = {name: headers.get(name) for name in _PROXY_HEADERS if headers.get(name)}
    seen = {}
    ProxyFix(lambda env, start: seen.update(env) or [], **PROXY_FIX_ARGS)(dict(arrived), None)
    direct, would = Request(arrived), Request(seen)
    chain = _forwarded_for(found.get('X-Forwarded-For'))
    if not probe:
        planted = ''
    elif not chain:
        planted = PROBE_DROPPED
    elif PROXY_PROBE_ADDRESS not in chain:
        planted = PROBE_REPLACED
    elif chain[-1] == PROXY_PROBE_ADDRESS:
        planted = PROBE_PASSED
    else:
        planted = PROBE_APPENDED
    return {
        'proxied': bool(found), 'headers': found, 'forwarded_for': chain,
        'direct': {'client': direct.remote_addr, 'origin': f'{direct.scheme}://{direct.host}'},
        'would': {'client': would.remote_addr, 'origin': f'{would.scheme}://{would.host}'},
        'probe': planted,
    }


def address_notice_hidden() -> bool:
    """Whether this version of the address notice was hidden."""
    pref = db.session.get(UserPref, ADDRESS_NOTICE_PREF)
    try:
        stored = int(json.loads(pref.value)) if pref and pref.value else 0
    except (TypeError, ValueError, OverflowError):
        log.warning('Stored %s %r is not a version number - showing the notice.',
                    ADDRESS_NOTICE_PREF, pref.value)
        stored = 0
    return stored >= ADDRESS_NOTICE_VERSION


def _connect(fcfg: dict, base: str) -> dict:
    user = output_feed.feed_username(fcfg)
    query = f'username={quote(user, safe="")}&password={PASSWORD_SLOT}'
    return {'server': base, 'username': user,
            'm3u': f'{base}/get.php?{query}&type=m3u_plus',
            'xmltv': f'{base}/xmltv.php?{query}'}


_NO_SPORTS = (None, None, None, None)


def _sports_programs(rows, now: datetime, days: int, after, blocked=frozenset(), allowed=None) -> tuple:
    """({sports row id: (the game on now, the last one that ended no more than `after` ago, the
    next one, the first one started early with Start broadcast), each or None}, {event id: [names of the
    sports channels carrying it]}), from guide_programs' two queries. Never playing_now() per row.
    `blocked` and `allowed` are the account ids a play drops, so a game's description counts the
    channels a tune-in could really use, as the guide the feed sends does."""
    from . import sports_feed
    sports = [r for r in rows if r.kind == output_feed.KIND_SPORTS]
    names = {r.row.row.id: r.name for r in sports}
    out, carrying = {}, {}
    for program in sports_feed.guide_programs([r.row for r in sports], now - after, days,
                                               blocked=blocked, allowed=allowed, asked_at=now):
        on_now, ended, following, opened = out.get(program.row_id, _NO_SPORTS)
        if program.event is not None:
            carrying.setdefault(program.event.id, []).append(names[program.row_id])
            if opened is None and sports_feed.live_now_on(program.event, now):
                opened = program
        if program.start <= now < program.stop:
            on_now = on_now or program
        elif program.stop <= now:
            ended = program     # ordered by start, so the last one wins
        elif following is None:
            following = program
        out[program.row_id] = (on_now, ended, following, opened)
    return out, carrying


def _sports_tuned(entry: tuple, now: datetime, window: tuple) -> tuple:
    """(program, phase) a tune-in to a sports channel plays now by its guide's times, the same
    order sports_feed.playing_now() takes listings in: a game started early with Start broadcast, then on
    now, then a game that may be running long, then the next game's early start; a game outside
    its own time counts only when a guide lists it or a channel's name gives it (Program.playable),
    since playing_now() has nothing to play otherwise, and runs long only when a guide lists it,
    since a game taken from a name ends at its pick's end. (None, '') when the channel has nothing
    near. The guide's times stand in for the
    listings' here, so this is what the row says, not a play."""
    from .sports_feed import PHASE_AFTER, PHASE_BEFORE, PHASE_LIVE_NOW, PHASE_ON
    before, after = window
    on_now, ended, following, opened = entry
    if opened is not None and opened.playable:
        # Opened early, it holds the channel; once its time comes it reads as any game does, as
        # the play's own label does (playing_now relabels only the time before its listing).
        if now < opened.start:
            return opened, PHASE_LIVE_NOW
        return opened, PHASE_ON if now < opened.stop else PHASE_AFTER
    if on_now is not None:
        return on_now, PHASE_ON
    if ended is not None and ended.listed and now < ended.stop + after:
        return ended, PHASE_AFTER
    if following is not None and following.playable and following.start - before <= now:
        return following, PHASE_BEFORE
    return None, ''


def _row_view(row, accounts: dict, blocked, allowed, sports: dict, carrying: dict, watching: dict, now, tz,
              h24, window: tuple, fcfg: dict, described: dict) -> dict:
    """One feed row as the Channels card shows it. `state` is exactly one of on / hidden /
    left_out; `problem` says why a play would not start, and is set on a left-out row too.
    `how` is how a play of it is delivered, said only in mixed mode, where rows differ."""
    view = {'feed_id': row.feed_id, 'kind': row.kind, 'kind_text': KIND_TEXT[row.kind],
            'name': row.name, 'category': row.category, 'num': row.num or '-',
            'is_sports': row.kind == output_feed.KIND_SPORTS,
            'is_up_next': row.kind == output_feed.KIND_UP_NEXT,
            'is_pip': row.kind == output_feed.KIND_PIP, 'pip': None,
            'on': row.offered, 'switchable': True, 'switch_tip': '', 'problem': '',
            'plays': None, 'note': '', 'sub': KIND_TEXT[row.kind], 'live': None, 'channel_now': None,
            'watching': watching.get(row.feed_id, 0), 'how': '', 'how_tip': ''}
    if output_feed.mode_of(fcfg) == output_feed.MODE_MIXED:
        if row.kind == output_feed.KIND_UP_NEXT:
            view['how'] = 'Through ChannelBin'
            view['how_tip'] = ('ChannelBin\'s own picture, so it always plays through ChannelBin and uses no '
                               'connection to your IPTV provider.')
        elif row.kind == output_feed.KIND_PIP:
            view['how'] = 'Through ChannelBin'
            view['how_tip'] = ('ChannelBin makes its picture, so it plays through ChannelBin in every mode.')
        elif output_feed.relays(fcfg, row.feed_id):
            view['how'] = 'Through ChannelBin'
            view['how_tip'] = ('ChannelBin plays it through itself, so it can move to another channel '
                               'when a stream drops, and a sports channel\'s waiting screen turns into '
                               'the game by itself.')
        else:
            view['how'] = 'Straight from your IPTV provider'
            view['how_tip'] = ('The IPTV player is sent to the channel\'s own stream, so ChannelBin '
                               'sees it tune in and never sees it stop.')
    if row.left_out:
        view['state'] = 'left_out'
    elif not row.on_output:
        view['state'] = 'hidden'
    else:
        view['state'] = 'on'

    def plays_from(channel):
        account = accounts.get(channel.account_id)
        return {'name': channel.name,
                'url': url_for('channels.channel_detail', channel_id=channel.id),
                'account': account}

    if row.kind == output_feed.KIND_CHANNEL:
        view['url'] = url_for('channels.channel_detail', channel_id=row.row.id)
        if row.left_out:
            view['problem'] = 'Not on the output: its account is switched off below.'
            view['switchable'] = False
            view['switch_tip'] = ('Its account is switched off for the output, below. Switch the '
                                  'account on to put this channel back.')
        elif row.row.account_id in blocked:
            # The play itself is what refuses this (the seat is never given), so the row
            # says it before a player finds out.
            view['problem'] = 'Nothing to play: its account is blocked right now.'
        else:
            # A channel plays from itself, so only its account is worth saying.
            view['plays'] = dict(plays_from(row.row), name='')
    elif row.kind == output_feed.KIND_GROUP:
        view['url'] = url_for('channel_groups.group_detail', group_id=row.row.id)
        if row.plays is not None:
            view['plays'] = plays_from(row.plays)
            view['note'] = (f'The best of {counted(len(row.members), "channel")} it can play right '
                            f'now, of {len(row.row.memberships)} in the group')
        else:
            view['problem'] = f'Nothing to play: {row.why_none or "no channel in the group can play right now"}.'
    elif row.kind == output_feed.KIND_SPORTS:
        served = row.row
        view['url'] = url_for('sports.league_page', league_id=served.league.id)
        view['sub'] = (f'{KIND_TEXT[row.kind]} - every {served.name} game' if served.is_team
                       else f'{KIND_TEXT[row.kind]} - {served.league.name}')
        # A sports channel's switch is its league's own, and one that is off is not a feed row
        # at all, so this page could never offer to turn it back on.
        view['switchable'] = False
        view['switch_tip'] = 'Turned on and off on its league\'s page in Sports Schedules.'
        from . import sports_words
        from .sports_feed import (PHASE_AFTER, PHASE_BEFORE, PHASE_LIVE_NOW, PHASE_ON, event_word, live_now_offer,
                                  live_now_on)
        entry = sports.get(served.row.id, _NO_SPORTS)
        tuned, phase = _sports_tuned(entry, now, window)
        following = entry[2]
        view['sports_now'] = tuned.title if tuned is not None else ''

        def offer(program):
            return live_now_offer(program.event, program.listings, carrying.get(program.event.id, ()),
                                  now, window[0], window[1], tz, h24, blocked=blocked, allowed=allowed,
                                  named=program.named)

        # Stop broadcast stays on the row while the game it started is what the row shows, until
        # the game's own time on air comes (live_now_offer() is None from then).
        if tuned is not None and tuned.event is not None and live_now_on(tuned.event, now):
            view['live'] = offer(tuned)
        if phase == PHASE_LIVE_NOW:
            view['sports_when'] = ('playing ahead of its start' if tuned.named is not None
                                   else 'playing ahead of its listing')
            view['note'] = tuned.description
        elif phase == PHASE_ON:
            view['sports_when'] = 'on now'
            view['note'] = tuned.description
        elif phase == PHASE_AFTER:
            view['sports_when'] = (f'listed until {day_label(tuned.stop, now, tz).lower()} '
                                   f'{format_local(tuned.stop, "clock", tz=tz, h24=h24)} and may be running long')
            view['note'] = tuned.description
        elif phase == PHASE_BEFORE:
            view['sports_when'] = (f'starts {day_label(tuned.start, now, tz).lower()} at '
                                   f'{format_local(tuned.start, "clock", tz=tz, h24=h24)}')
            view['note'] = tuned.description
        elif phase == '':
            view['sports_when'] = ''
            view['note'] = (f'Next: {following.title}, {day_label(following.start, now, tz)} at '
                            f'{format_local(following.start, "clock", tz=tz, h24=h24)}'
                            if following is not None else f'No {event_word(served.league)} is waiting for it')
            if following is not None and following.event is not None:
                view['live'] = offer(following)
        else:
            raise ValueError(f'Unknown play phase "{phase}"')
        # Channel it would use (dev/changelog/1639), for the game the row names: asked on click,
        # and only once a guide lists the game or a channel's name gives it (dev/changelog/1643).
        shown = tuned if tuned is not None else following
        if shown is not None and shown.event is not None:
            view['channel_now'] = {'event': shown.event.id, 'why': '' if shown.playable else (
                f"No channel's guide lists {shown.title} yet, and it is not one Record would take from a "
                "channel's name either, so there is no channel to show. ChannelBin looks again after each "
                'guide update.')}
        if row.left_out:
            # Not a fault: a tune-in from a player that kept it still plays (dev/changelog/1621).
            view['note'] = (f'Not in the channel list right now: {row.left_out}. It comes back as the same '
                            f'channel when one of its {sports_words.many(event_word(served.league))} is that '
                            f'close. {view["note"]}')
    elif row.kind == output_feed.KIND_UP_NEXT:
        ch = row.row
        view['url'] = (url_for('sports.sports_page') if ch.everything
                       else url_for('sports.league_page', league_id=ch.league.id))
        view['sub'] = (f'{KIND_TEXT[row.kind]} - every league you follow' if ch.everything
                       else f'{KIND_TEXT[row.kind]} - {ch.league.name}')
        view['note'] = ('ChannelBin\'s own picture of the coming games, a new page every 10 seconds. It '
                        'uses no connection to your IPTV provider.')
        if row.left_out:
            view['problem'] = f'Not on the output: {row.left_out}.'
            view['switch_tip'] = 'Follow something in its league again in Sports Schedules to bring it back.'
    elif row.kind == output_feed.KIND_PIP:
        one = row.row
        view['url'] = '#of-pip'
        view['sub'] = 'Picture in picture, swapped' if one.swapped else 'Picture in picture'
        roles = (pip_channels.ROLE_INSET, pip_channels.ROLE_MAIN) if one.swapped else pip_channels.ROLES
        if pip_channels.is_set(one.row):
            view['pip'] = [_pip_plays(described[(one.row.id, role)], label, accounts)
                           for role, label in zip(roles, ('Big', 'Small'))]
        else:
            view['note'] = 'Nothing set up: a card saying so'
        if one.swapped:
            # It follows its channel's switch and has none of its own (DESIGN-pip-channels.md 4.3).
            view['switchable'] = False
            view['switch_tip'] = ('A swapped copy follows its channel. Turn it off with Swapped copy, under '
                                  'Picture-in-picture channels.')
    else:
        raise ValueError(f'Unknown feed row kind "{row.kind}"')
    # Preview (dev/changelog/1645): a provider's channel previews as itself; ChannelBin's own channels
    # play as a player gets them, a group whether or not it is on the output. A group or an Up next
    # channel that cannot play says why instead; a sports channel always has its waiting screen.
    if row.kind == output_feed.KIND_CHANNEL:
        view['preview'] = {'channel': row.row.id, 'feed': None, 'why': ''}
    else:
        why = view['problem'] if row.kind in (output_feed.KIND_GROUP, output_feed.KIND_UP_NEXT) else ''
        view['preview'] = {'channel': None, 'feed': row.feed_id, 'why': why}
    view['hay'] = ' '.join(x for x in (row.name, row.category,
                                        (view['plays'] or {}).get('name', '')) if x).lower()
    return view


def _pip_plays(d, label: str, accounts: dict) -> dict:
    """One picture of a picture-in-picture channel for a "plays from" cell: which window it is
    (`label`), and either the channel a play would start from now with its account, or what the
    picture is and when it is on. A game never names a channel here: which one carries it is
    worked out at the tune-in."""
    view = {'label': label, 'text': '', 'warn': d.gone, 'channel': None, 'account': None}
    if d.member is not None:
        view['channel'] = {'name': d.member.name,
                           'url': url_for('channels.channel_detail', channel_id=d.member.id)}
        view['account'] = accounts.get(d.member.account_id)
    elif d.kind == pip_channels.PIP_SOURCE_GAME or d.gone:
        view['text'] = f'{d.short}: {d.sub[:1].lower()}{d.sub[1:]}' if d.sub else d.title
    elif d.kind == pip_channels.PIP_SOURCE_SEARCH:
        view['text'] = f'no channel is named {d.short} right now' if not d.matches else f'{d.short}: nothing it matches can play right now'
        view['warn'] = True
    elif d.kind in (pip_channels.PIP_SOURCE_CHANNEL, pip_channels.PIP_SOURCE_GROUP):
        view['text'] = f'{d.short} cannot play right now'
        view['warn'] = True
    else:
        raise ValueError(f'Unknown kind of picture "{d.kind}"')
    return view


def pip_boxes() -> dict:
    """Where the small picture sits in the stand-in picture, as a CSS box in percent of the
    frame, for every layout a channel can store: 'side', and each corner with each size
    ('brm'). From proc_utils.pip_geometry(), so the stand-in is the real arrangement."""
    from .proc_utils import (PIP_CORNER_SIZES, PIP_CORNERS, PIP_LAYOUT_CORNER, PIP_LAYOUT_SIDE,
                             REENCODE_HEIGHT, REENCODE_WIDTH, pip_geometry)

    def box(g):
        w, h = g.small_w + 2 * g.border, g.small_h + 2 * g.border
        return (f'left:{100 * g.small_x / REENCODE_WIDTH:.2f}%;top:{100 * g.small_y / REENCODE_HEIGHT:.2f}%;'
                f'width:{100 * w / REENCODE_WIDTH:.2f}%;height:{100 * h / REENCODE_HEIGHT:.2f}%')
    side = pip_geometry(PIP_LAYOUT_SIDE)
    out = {'side': box(side),
           'side_main': (f'left:0;top:{100 * side.big_y / REENCODE_HEIGHT:.2f}%;'
                         f'width:{100 * side.big_w / REENCODE_WIDTH:.2f}%;height:{100 * side.big_h / REENCODE_HEIGHT:.2f}%')}
    for corner in PIP_CORNERS:
        for size in PIP_CORNER_SIZES:
            out[corner + size] = box(pip_geometry(PIP_LAYOUT_CORNER, corner, size))
    return out


def _pip_card(rows, described: dict, watching: dict) -> list:
    """The Picture-in-picture card's rows, one per channel a person made, from the catalog's
    rows and describe()'s answers: no query."""
    by_row = {}
    for row in rows:
        if row.kind == output_feed.KIND_PIP:
            by_row.setdefault(row.row.row.id, []).append(row)
    out = []
    boxes = pip_boxes()
    for row_id, feed_rows in by_row.items():
        main = next(r for r in feed_rows if not r.row.swapped)
        twin = next((r for r in feed_rows if r.row.swapped), None)
        row = main.row.row
        is_set = pip_channels.is_set(row)
        pics = {role: described.get((row.id, role)) for role in pip_channels.ROLES}
        games = [d for d in pics.values() if d is not None and d.kind == pip_channels.PIP_SOURCE_GAME]
        sub = f'{pip_channels.layout_words(row)}.' if is_set else pip_channels.NOTHING_SET_UP
        if is_set and len(games) == 2 and all(d.over for d in games):
            sub += ' Its games are over, and it shows that until you change it.'
        if not row.on_output:
            sub += ' Hidden from the IPTV output.'
        spec = {'id': row.id, 'name': row.name, 'layout': row.layout, 'corner': row.corner,
                'size': row.size, 'swapped_copy': bool(row.swapped_copy),
                'main': None, 'inset': None}
        for role in pip_channels.ROLES:
            source, d = pip_channels.source_of(row, role), pics[role]
            if source is not None:
                spec[role] = dict(source.to_dict(), title=d.title, short=d.short, sub=d.sub)
        out.append({
            'id': row.id, 'name': row.name, 'feed_id': main.feed_id, 'num': main.num or '-',
            'twin_num': (twin.num or '-') if twin is not None else '', 'twin_name': pip_channels.swapped_name(row.name),
            'set': is_set, 'on': bool(row.on_output), 'swapped_copy': bool(row.swapped_copy), 'sub': sub,
            'big': pics[pip_channels.ROLE_MAIN], 'small': pics[pip_channels.ROLE_INSET],
            'side': row.layout == pip_channels.PIP_LAYOUT_SIDE,
            'inset_box': boxes['side'] if row.layout == pip_channels.PIP_LAYOUT_SIDE else boxes[row.corner + row.size],
            'main_box': boxes['side_main'] if row.layout == pip_channels.PIP_LAYOUT_SIDE else '',
            'watching': watching.get(main.feed_id, 0) + (watching.get(twin.feed_id, 0) if twin else 0),
            'spec': spec,
        })
    return out


def _pip_live(session, accounts: dict):
    """A combined play's two pictures for Watching now: each the channel and account it plays
    from, or why it is not playing. None for a play that is not on its two pictures."""
    from . import pip_engine
    both = pip_engine.views(session)
    if both is None:
        return None
    out = []
    for label, one in (('Big', both['big']), ('Small', both['small'])):
        text = one['text']
        out.append({'label': label, 'warn': not one['live'],
                    'channel': ({'name': one['channel_name'],
                                 'url': url_for('channels.channel_detail', channel_id=one['channel_id'])}
                                if one['live'] else None),
                    'account': accounts.get(one['account_id']),
                    'text': f'{text[:1].upper()}{text[1:]}' if text else 'Not playing'})
    return out


def _watching(sessions, by_feed: dict, accounts: dict, sports: dict, now, tz, h24, window: tuple,
              described: dict) -> list:
    """The live plays, each with the program its row's guide lists now: ONE listings query
    for every watched channel together."""
    from . import db
    listing_ids = {by_feed[s.feed_id].listings_channel_id for s in sessions
                   if s.feed_id in by_feed and by_feed[s.feed_id].listings_channel_id}
    programs = {}
    if listing_ids:
        found = (db.session.query(EPGEntry.channel_id, EPGEntry.title, EPGEntry.start_time,
                                  EPGEntry.stop_time)
                 .filter(EPGEntry.channel_id.in_(list(listing_ids)),
                         EPGEntry.start_time <= now, EPGEntry.stop_time > now)
                 .order_by(EPGEntry.start_time).all())
        for channel_id, title, start, stop in found:
            programs[channel_id] = (title, start, stop)   # the latest start wins an overlap
    out = []
    for s in sorted(sessions, key=lambda x: x.started_at):
        row = by_feed.get(s.feed_id)
        d = s.to_dict()
        view = {'id': s.id, 'client': s.client, 'login': s.feed_login, 'name': row.name if row else s.label,
                'kind_text': KIND_TEXT[row.kind] if row else '', 'row_url': '',
                'channel_name': s.channel_name,
                'channel_url': (url_for('channels.channel_detail', channel_id=s.channel_id)
                                if s.channel_id is not None else ''),
                'account': accounts.get(s.account_id),
                'starting': s.state == output_feed.STATE_STARTING,
                'waiting': s.state == output_feed.STATE_WAITING,
                'showing': s.state == output_feed.STATE_SHOWING,
                'on_card': s.state == output_feed.STATE_CARD,
                'card_text': f'{s.card_text[:1].upper()}{s.card_text[1:]}' if s.card_text else '',
                'pip': None, 'pip_live': None, 'combine_off': '',
                'waiting_text': _waiting_text(s),
                'from_screen': ('' if s.left_screen_at is None
                                else f'Came from its card after {fmt_duration(s.screen_seconds)}'
                                if s.pip_row_id is not None
                                else f'Came from the waiting screen after {fmt_duration(s.screen_seconds)}'),
                'elapsed': fmt_duration(d['elapsed_seconds']),
                'sent': fmt_bytes(s.bytes_sent) if s.bytes_sent else '-',
                'moves': len(s.failovers), 'moves_text': _times(len(s.failovers)),
                'moves_tip': '\n'.join(output_feed.move_text(a, b, why) for a, b, why in s.failovers),
                'reserve_note': output_feed.reserve_short_note(s.ring),
                'notes': output_feed.play_notes(s),
                'move_offered': d['move_offered'], 'move_why': d['move_why'],
                'program': '', 'program_times': ''}
        if row is not None and row.kind == output_feed.KIND_UP_NEXT:
            view['row_url'] = (url_for('sports.sports_page') if row.row.everything
                               else url_for('sports.league_page', league_id=row.row.league.id))
        elif row is not None and row.kind == output_feed.KIND_SPORTS:
            view['row_url'] = url_for('sports.league_page', league_id=row.row.league.id)
            tuned, _phase = _sports_tuned(sports.get(row.row.row.id, _NO_SPORTS), now, window)
            view['program'] = tuned.title if tuned is not None else ''
        elif row is not None and row.kind == output_feed.KIND_PIP:
            one = row.row
            view['row_url'] = '#of-pip'
            roles = (pip_channels.ROLE_INSET, pip_channels.ROLE_MAIN) if one.swapped else pip_channels.ROLES
            big, small = (described.get((one.row.id, role)) for role in roles)
            if big is not None and small is not None:
                view['pip'] = {'big': big.title, 'small': small.title}
            view['pip_live'] = _pip_live(s, accounts)
            view['combine_off'] = s.combine_off
        elif row is not None and row.kind in (output_feed.KIND_CHANNEL, output_feed.KIND_GROUP):
            view['row_url'] = (url_for('channels.channel_detail', channel_id=row.row.id)
                               if row.kind == output_feed.KIND_CHANNEL
                               else url_for('channel_groups.group_detail', group_id=row.row.id))
            hit = programs.get(row.listings_channel_id)
            if hit is not None:
                view['program'] = hit[0] or ''
                view['program_times'] = (f'{format_local(hit[1], "clock", tz=tz, h24=h24)} - '
                                         f'{format_local(hit[2], "clock", tz=tz, h24=h24)}')
        elif row is not None:
            raise ValueError(f'Unknown feed row kind "{row.kind}"')
        out.append(view)
    return out


def play_names(cfg: dict, plays) -> tuple:
    """({feed id: {'name', 'kind_text'}}, {event id: game}) for `plays` (dicts with a
    `feed_id`, and an `event_id` for a sports play): the guide row's name and its kind, as
    Watching now and Recent plays on the IPTV Output page name a play (_watching, _recent), and
    the game a sports play is, for a page that lists plays without the rest of this one (the
    dashboard). A row no longer in the catalog is left out, and its play keeps its label. One
    catalog read, and none with no play to name: 0.2 s measured on a guide of 105 rows
    (dev/changelog/1746)."""
    from .database import SportsEvent
    from .sports_data import event_game_name
    plays = list(plays)
    ids = {p['feed_id'] for p in plays}
    if not ids:
        return {}, {}
    names = {r.feed_id: {'name': r.name, 'kind_text': KIND_TEXT[r.kind]}
             for r in output_feed.catalog(cfg, everything=True) if r.feed_id in ids}
    event_ids = {p.get('event_id') for p in plays} - {None}
    games = ({e.id: event_game_name(e) for e in SportsEvent.query.filter(SportsEvent.id.in_(event_ids))}
             if event_ids else {})
    return names, games


def _waiting_text(s) -> str:
    """What Watching now says a play on the waiting screen is waiting for, or ''."""
    if s.state != output_feed.STATE_WAITING:
        return ''
    from .sports_screen import HELD_NO_CONNECTION, HELD_NO_SLOT
    if s.screen_held == HELD_NO_SLOT:
        why = 'On now, waiting for one of the "IPTV players at once" slots'
    elif s.screen_held == HELD_NO_CONNECTION:
        why = 'On now, waiting for a free connection on an account that lists it'
    elif s.screen_held is None:
        why = ''
    else:
        raise ValueError(f'Unknown reason a game is held "{s.screen_held}"')
    game = f'Waiting for {s.waiting_for}' if s.waiting_for else 'Nothing scheduled'
    return f'{game}. {why}' if why else game


def _recent(args, mode: str, by_feed: dict, accounts: dict, now, tz, h24) -> dict:
    """One page of Recent plays and the chips' counts: two queries. A line that names the channel it
    played links it and its account from the ids the line kept: a channel deleted since has had its
    id cleared (detach_channel_references) and reads as text, as does an account no longer there."""
    from . import db
    since = now - timedelta(days=output_feed.PLAY_LOG_DAYS)
    counts = dict(db.session.query(OutputFeedPlay.kind, db.func.count(OutputFeedPlay.id))
                  .filter(OutputFeedPlay.at >= since).group_by(OutputFeedPlay.kind).all())
    total = sum(counts.values())
    # A kind the current mode never writes still gets its chip while the log holds some, so
    # nothing kept from before a change of mode is unreachable.
    if mode == output_feed.MODE_PROXY:
        kinds = [FEED_PLAY_ENDED, FEED_PLAY_REFUSED]
    elif mode == output_feed.MODE_MIXED:
        kinds = [FEED_PLAY_ENDED, FEED_PLAY_REDIRECTED, FEED_PLAY_REFUSED]
    elif mode == output_feed.MODE_REDIRECT:
        kinds = [FEED_PLAY_REDIRECTED, FEED_PLAY_REFUSED]
    else:
        raise ValueError(f'Unknown feed mode "{mode}"')
    # A picture-in-picture play's small-picture lines get a chip only while there are some.
    kinds += [k for k in (FEED_PLAY_ENDED, FEED_PLAY_REDIRECTED, FEED_PLAY_PICTURE)
              if k not in kinds and counts.get(k)]
    picked = args.get('plays', PLAYS_ALL)
    if picked not in kinds:
        picked = PLAYS_ALL
    shown = total if picked == PLAYS_ALL else counts.get(picked, 0)
    pages = max(1, -(-shown // PLAYS_PER_PAGE))
    try:
        page = int(args.get('page', 1))
    except (TypeError, ValueError):
        page = 1
    page = min(max(1, page), pages)
    q = OutputFeedPlay.query.filter(OutputFeedPlay.at >= since)
    if picked != PLAYS_ALL:
        q = q.filter(OutputFeedPlay.kind == picked)
    found = (q.order_by(OutputFeedPlay.at.desc(), OutputFeedPlay.id.desc())
             .offset((page - 1) * PLAYS_PER_PAGE).limit(PLAYS_PER_PAGE).all())

    def clock(dt):
        return format_local(dt, 'clock', tz=tz, h24=h24)

    def played(play, lead, tail):
        return {'lead': lead, 'tail': tail,
                'channel': {'name': play.channel_name or '-',
                            'url': (url_for('channels.channel_detail', channel_id=play.channel_id)
                                    if play.channel_id is not None else '')},
                'account': accounts.get(play.account_id) if play.account_id is not None else None}

    lines = []
    for play in found:
        row = by_feed.get(play.feed_id)
        # The stored message names the row the way the log does ("group 2 (USA)"); the
        # column beside it already says which row, in the name the user knows.
        text = (play.message or '').replace(f' for {play.label}', '')
        line = {'kind': play.kind, 'when': f'{day_label(play.at, now, tz)}, {clock(play.at)}',
                'client': play.client or '-', 'login': play.feed_login or '',
                'name': row.name if row else play.label,
                'text': text, 'detail': '', 'played': None}
        if play.kind == FEED_PLAY_ENDED:
            line['badge'] = ('b-abort', 'Ended')
            line['played'] = played(play, 'Played', f' for {fmt_duration(play.seconds)}'
                                    + (f', moved to another channel {_times(play.moves)}' if play.moves else ''))
        elif play.kind == FEED_PLAY_REFUSED:
            line['badge'] = ('b-warn', 'Refused')
            line['detail'] = (f'Asked {play.count} times, last at {clock(play.at)}'
                              if play.count > 1 else 'Asked once')
        elif play.kind == FEED_PLAY_REDIRECTED:
            line['badge'] = ('b-sched', 'Sent to the provider')
            line['played'] = played(play, 'To', '. ChannelBin cannot see when it stopped.')
        elif play.kind == FEED_PLAY_PICTURE:
            # A picture-in-picture play's small picture: back on a channel, or stopped and why.
            if play.reason == FEED_PICTURE_BACK:
                line['badge'] = ('b-sched', 'Small picture back')
                line['played'] = played(play, 'On', '')
            elif play.reason == FEED_PICTURE_STOPPED:
                line['badge'] = ('b-warn', 'Small picture stopped')
            else:
                raise ValueError(f'Unknown small-picture line "{play.reason}"')
        else:
            raise ValueError(f'Unknown play kind "{play.kind}"')
        lines.append(line)

    def link(kind, to_page=1):
        params = {}
        if kind != PLAYS_ALL:
            params['plays'] = kind
        if to_page > 1:
            params['page'] = to_page
        return url_for('iptv_output.page', **params)

    chips = [{'key': PLAYS_ALL, 'label': 'All', 'n': total, 'url': link(PLAYS_ALL)}]
    chips += [{'key': k, 'label': _PLAY_CHIPS[k], 'n': counts.get(k, 0), 'url': link(k)}
              for k in kinds]
    first = (page - 1) * PLAYS_PER_PAGE
    return {'lines': lines, 'chips': chips, 'picked': picked, 'days': output_feed.PLAY_LOG_DAYS,
            'pager': None if pages == 1 else {
                'text': f'{first + 1}-{first + len(lines)} of {shown}',
                'newer': link(picked, page - 1) if page > 1 else '',
                'older': link(picked, page + 1) if page < pages else ''}}


def _held_choices(limit: int, held: int) -> list:
    """The "Recordings leave alone" choices for an account with `limit` connections. Short
    on purpose: what choosing all of them costs is said under the select (`held_all`), since
    inside the choice it made the column wider than a card beside the sidebar holds."""
    out = []
    for n in range(limit + 1):
        if n == 0:
            label = 'None'
        elif n == limit:
            label = 'The only one' if limit == 1 else f'All {limit}'
        else:
            label = f'{n} of {limit}'
        out.append({'n': n, 'label': label, 'selected': n == min(held, limit)})
    return out


def _accounts(rows, sessions, accounts: dict, cfg: dict, fcfg: dict) -> list:
    """The Accounts card's rows. "Recordings leave alone" is offered in every mode that
    redirects a play, since only a redirected play is out of ChannelBin's sight."""
    allowed = output_feed.allowed_account_ids(fcfg)
    held = output_feed.held_seats_map(fcfg)
    redirects = output_feed.redirects_some(fcfg)
    limits = (connlim.limits_for_accounts(
        accounts.values(), (cfg.get('accounts') or {}).get('default_max_connections', 1))
        if redirects else {})
    included = {a: 0 for a in accounts}
    for row in rows:
        if not row.on_output:
            continue
        if row.kind == output_feed.KIND_CHANNEL:
            on = {row.row.account_id}
        elif row.kind == output_feed.KIND_GROUP:
            on = {m.channel.account_id for m in row.row.memberships if m.recording_enabled}
        elif row.kind == output_feed.KIND_SPORTS:
            on = set()      # a sports channel plays from whichever account lists the game then
        elif row.kind == output_feed.KIND_UP_NEXT:
            on = set()      # ChannelBin's own picture: no account at all
        elif row.kind == output_feed.KIND_PIP:
            on = set()      # each picture plays from whichever account has it then
        else:
            raise ValueError(f'Unknown feed row kind "{row.kind}"')
        for account_id in on:
            if account_id in included:
                included[account_id] += 1
    streams = {}
    # A picture-in-picture play names no account itself: its two pictures each hold a
    # connection, shared by every play of the row (DESIGN-pip-channels.md 8.1).
    for account_id in [s.account_id for s in sessions] + pip_engine.picture_accounts():
        streams[account_id] = streams.get(account_id, 0) + 1
    out = []
    for account in sorted(accounts.values(), key=lambda a: (a.name.lower(), a.id)):
        view = {'account': account, 'on': allowed is None or account.id in allowed,
                'included': included[account.id], 'streams': streams.get(account.id, 0)}
        if redirects:
            view['connections'] = limits[account.id]
            view['held'] = _held_choices(limits[account.id], held.get(account.id, 0))
            view['held_all'] = held.get(account.id, 0) >= limits[account.id] > 0
        out.append(view)
    return out


def _logins(sessions, rows, accounts: dict) -> list:
    """The Logins for other users card: one query, each login's live plays counted from the
    plays already in hand (dev/changelog/1760), its channels from the catalog already in hand
    (dev/changelog/1764), and its own accounts (dev/changelog/1765) named from `accounts`
    ({id: Account}, already loaded). A login's own accounts are applied to that catalog in
    memory (output_feed.left_out_for_login), never by asking for it again."""
    playing = {}
    for s in sessions:
        if s.feed_login_id is not None:
            playing[s.feed_login_id] = playing.get(s.feed_login_id, 0) + 1
    existing = {output_feed.lineup_key(r.feed_id) for r in rows}
    out = []
    for row in OutputFeedLogin.query.order_by(db.func.lower(OutputFeedLogin.name),
                                              OutputFeedLogin.id).all():
        lineup, own = output_feed.lineup_of(row), output_feed.accounts_of(row)
        view = {'id': row.id, 'name': row.name, 'username': row.username, 'on': row.enabled,
                'max_streams': row.max_streams, 'playing': playing.get(row.id, 0),
                'everything': lineup is None, 'own_accounts': own is not None}
        if own is not None:
            names = sorted((accounts[a].name for a in own if a in accounts), key=str.lower)
            view['accounts_text'] = join_labels(names) if names else 'None'
            gone = sum(1 for a in own if a not in accounts)
            view['accounts_gone_text'] = f'{counted(gone, "picked account")} deleted since' if gone else ''
        if lineup is not None:
            # What its IPTV player lists (a swapped copy is a channel of its own there), the
            # picks it does not list because the output does not offer them right now, and
            # those its own accounts leave out.
            listed, by_accounts = set(), set()
            for r in rows:
                why = output_feed.left_out_for_login(r, own) if r.on_output else None
                if why == '':
                    listed.add(output_feed.lineup_key(r.feed_id))
                elif why in output_feed.LOGIN_LEFT_OUT:
                    by_accounts.add(output_feed.lineup_key(r.feed_id))
            view['channels_text'] = counted(len(lineup & listed), 'channel')
            view['channels_off_accounts'] = len(lineup & by_accounts)
            view['channels_off'] = len((lineup & existing) - listed - by_accounts)
        out.append(view)
    return out


#: What the Choose channels dialog says of a row the output does not offer right now.
LINEUP_TAKEN_OFF = 'Taken off the output'


def lineup_choices(cfg: dict, lineup, accounts=None) -> dict:
    """The Choose channels dialog's list (routes/iptv_output.py::login_channels): every row the
    output has, in its order, offered right now or not, a picture-in-picture channel's swapped
    copy folded into its channel, and which of them `lineup` picks (None for everything). A
    pick of a row the output no longer has at all is not listed, so the next save drops it.
    `accounts` is the login's own account list (None for the output's), which says which rows
    its player would leave out (output_feed.left_out_for_login, dev/changelog/1765)."""
    choices, by_key = [], {}
    for r in output_feed.catalog(cfg, everything=True):
        key = output_feed.lineup_key(r.feed_id)
        if key in by_key:
            by_key[key]['swapped'] = True     # the swapped copy, right after its channel
            continue
        why = output_feed.left_out_for_login(r, accounts)
        if not r.on_output:
            off = LINEUP_TAKEN_OFF
        elif why in output_feed.LOGIN_LEFT_OUT:
            off = f'Not for this login: {why}'
        elif why:
            off = f'Not on the output now: {why}'
        else:
            off = ''
        by_key[key] = {'id': key, 'name': r.name, 'category': r.category, 'num': r.num,
                       'off': off, 'swapped': False}
        choices.append(by_key[key])
    return {'everything': lineup is None,
            'picked': sorted(k for k in (lineup or ()) if k in by_key),
            'rows': choices}


def account_choices(cfg: dict, accounts) -> dict:
    """The Choose accounts dialog's list (routes/iptv_output.py::login_accounts): every account
    by name, whether the output's own list has it (`on_output`), and which `accounts` (a login's
    own list, None for the output's) picks. A pick of an account since deleted is not listed,
    so the next save drops it. One query."""
    allowed = output_feed.allowed_account_ids(output_feed.feed_config(cfg))
    rows = sorted(Account.query.all(), key=lambda a: (a.name or '').lower())
    ids = {a.id for a in rows}
    return {'own': accounts is not None,
            'picked': sorted(a for a in (accounts or ()) if a in ids),
            'accounts': [{'id': a.id, 'name': a.name,
                          'on_output': allowed is None or a.id in allowed} for a in rows]}


def _public(fcfg: dict, connect_server: str, tz, h24) -> dict:
    """The public port, the address people outside use and the addresses locked out after
    wrong logins, for the Logins for other users card (dev/changelog/1761). No query: the
    listener's status and the lockout counts are in memory."""
    listener = public_listener.status()
    url = output_feed.feed_public_url(fcfg)
    state = listener['state']
    now = datetime.utcnow()
    locked = output_feed.login_failures.locked_out()
    set_port = public_listener.public_port(fcfg)
    serving = state == public_listener.STATE_SERVING
    if serving:
        say = PUBLIC_OPEN if url else PUBLIC_OPEN_NO_URL
    elif state == public_listener.STATE_FAILED:
        say = PUBLIC_FAILED
    elif state == public_listener.STATE_OFF:
        say = PUBLIC_WAITING if set_port else PUBLIC_UNSET
    else:
        raise ValueError(f'Unknown public port state "{state}"')
    return {
        'say': say, 'serving': serving, 'port': listener['port'] or set_port,
        'set_port': set_port, 'error': listener['error'], 'url': url,
        # What the Logins card copies as the server: outside people's address once the public
        # port is open and that address is set, else the one in the Connect card.
        'server': url if url and serving else connect_server,
        'suggested': output_feed.SUGGESTED_PUBLIC_PORT,
        'lockouts': [{'address': found.address, 'until': _until(found.until, now, tz, h24),
                      'words': lockout_words(found)} for found in locked[:LOCKOUTS_SHOWN]],
        'lockouts_more': max(0, len(locked) - LOCKOUTS_SHOWN),
        'lockout_steps': steps_words(output_feed.login_failures.steps),
        'lockout_forget': length_words(output_feed.login_failures.forget),
        'lockout_tries': output_feed.LOGIN_MAX_FAILURES,
        'alarm': _login_alarm(tz, h24),
        'proxy': _public_proxy(),
    }


#: Locked-out addresses the Logins card lists, soonest to end first. The tracker can hold
#: auth.MAX_TRACKED_IPS of them under a flood, and the card says how many it left out.
LOCKOUTS_SHOWN = 20


def _until(when, now, tz, h24) -> str:
    """'9:40 PM', or '9:40 PM tomorrow' when a lockout ends on another day."""
    clock = format_local(when, 'clock', tz=tz, h24=h24)
    day = day_label(when, now, tz)
    return clock if day == 'Today' else f'{clock} {day.lower() if day == "Tomorrow" else day}'


def _login_alarm(tz, h24) -> dict:
    """The wrong-login alarm for the Logins card (auth.FailureTracker.alarm): going on now, or
    when it last was. Memory only."""
    alarm = output_feed.login_failures.alarm()
    return {
        'active': alarm['active'], 'count': alarm['count'], 'addresses': alarm['addresses'],
        'peak': alarm['peak'], 'per_minute': output_feed.LOGIN_ALARM_PER_MINUTE,
        'first': format_local(alarm['first'], 'clock', tz=tz, h24=h24) if alarm['first'] else '',
        'last': format_local(alarm['last'], 'clock', tz=tz, h24=h24) if alarm['last'] else '',
    }


def _public_proxy() -> dict:
    """Whose word the public port takes for a player's address, and who it last heard from
    (app/proxy_trust.py): 'say' is one of PUBLIC_PROXY_*. Memory only."""
    policy = proxy_trust.wired(current_app)
    seen = proxy_trust.seen(proxy_trust.PORT_PUBLIC)
    if not policy.public:
        say = PUBLIC_PROXY_OFF
    elif policy.networks:
        say = PUBLIC_PROXY_LISTED
    else:
        say = PUBLIC_PROXY_ANY
    return {'say': say, 'listed': len(policy.networks), 'believed': seen['believed_peer'],
            'direct': seen['direct_peer'], 'ignored': seen['ignored_recent']}


def page_context(cfg: dict, host_url: str, args, proxy: str = '') -> dict:
    """Everything templates/iptv_output.html draws. `host_url` is the address the page was
    opened on, used only when ChannelBin's own address is not set; `proxy` is proxy_state()."""
    fcfg = output_feed.feed_config(cfg)
    state = feed_state(fcfg)
    mode = output_feed.mode_of(fcfg)
    redirect = mode == output_feed.MODE_REDIRECT
    now = datetime.utcnow()
    tz, h24 = get_display_tz(), is_24h()
    accounts = {a.id: a for a in Account.query.all()}
    rows = output_feed.catalog(cfg, everything=True)
    by_feed = {r.feed_id: r for r in rows}
    window = output_feed.sports_window(fcfg)
    from .channel_groups import blocked_account_ids_now
    blocked = blocked_account_ids_now()
    allowed = output_feed.allowed_account_ids(fcfg)
    sports, carrying = _sports_programs(rows, now, output_feed.guide_days(fcfg), window[1],
                                        blocked=blocked, allowed=allowed)
    # In redirect mode the plays ChannelBin can see are its own pictures, a sports channel's
    # waiting screen and an Up next channel, its own stream there too (dev/changelog/1534, 1614),
    # and every play on a login for another user, which is always relayed (dev/changelog/1760).
    sessions = ([s for s in output_feed.live_sessions() if output_feed.seen_in_mode(s, mode)]
                if state == STATE_ON else [])
    watching = {}
    for s in sessions:
        watching[s.feed_id] = watching.get(s.feed_id, 0) + 1
    pip_rows = list({r.row.row.id: r.row.row for r in rows if r.kind == output_feed.KIND_PIP}.values())
    described = (pip_channels.describe(
        pip_rows, now, guide=[r for r in rows if r.kind in (output_feed.KIND_CHANNEL, output_feed.KIND_GROUP)],
        after=window[1]) if pip_rows else {})
    views = [_row_view(r, accounts, blocked, allowed, sports, carrying, watching, now, tz, h24, window, fcfg,
                       described)
             for r in rows]
    address = _address(cfg, host_url)
    max_viewers = int(fcfg.get('max_viewers') or 1)
    logins = _logins(output_feed.live_sessions() if state == STATE_ON else [], rows, accounts)
    connect = _connect(fcfg, address['base'])
    public = _public(fcfg, connect['server'], tz, h24)
    # The public port relays every play on it, whatever the mode (dev/changelog/1761).
    public_relays = public['serving'] and mode != output_feed.MODE_PROXY
    return {
        'state': state, 'is_on': state == STATE_ON, 'is_off': state == STATE_OFF,
        'needs_login': state == STATE_SETUP, 'mode': mode, 'redirect': redirect,
        'mixed': mode == output_feed.MODE_MIXED,
        # A login for another user is relayed in every mode, so with one there is always
        # something the relay's settings and counts apply to (dev/changelog/1760).
        'relays_some': output_feed.relays_some(fcfg) or bool(logins) or public_relays,
        'redirects_some': output_feed.redirects_some(fcfg),
        'dormant': state != STATE_ON, 'mask': MASK_SENTINEL, 'bounds': output_feed.SETTING_BOUNDS,
        'address': address, 'proxy': proxy, 'channels_per_page': CHANNELS_PER_PAGE,
        'address_notice': {'hidden': address['state'] == ADDRESS_UNSET and address_notice_hidden(),
                           'pref': ADDRESS_NOTICE_PREF, 'version': ADDRESS_NOTICE_VERSION},
        'connect': connect,
        'public': public,
        'logins': logins,
        'login_password': output_feed.generated_password(),
        'login_streams': output_feed.MAX_STREAMS_BOUNDS,
        'password_slot': PASSWORD_SLOT,
        'rows': views,
        'categories': sorted({v['category'] for v in views}, key=str.lower),
        'counts': {'all': len(views), 'on': sum(1 for v in views if v['on']),
                   'hidden': sum(1 for v in views if v['state'] == 'hidden'),
                   'problem': sum(1 for v in views if v['problem'])},
        'watching': _watching(sessions, by_feed, accounts, sports, now, tz, h24, window, described),
        'pips': _pip_card(rows, described, watching),
        'pip_gpu_why': pip_engine.gpu_notice(cfg) if pip_rows else '',
        'pip_boot': {'boxes': pip_boxes(), 'corners': pip_channels.CORNER_WORDS, 'sizes': pip_channels.SIZE_WORDS,
                     'name_max': pip_channels.NAME_MAX, 'query_max': pip_channels.QUERY_MAX},
        'at_limit': sum(1 for s in sessions if s.counted) >= max_viewers,
        'max_viewers': max_viewers,
        'recent': _recent(args, mode, by_feed, accounts, now, tz, h24),
        'accounts': _accounts(rows, sessions, accounts, cfg, fcfg),
        'feedcfg': fcfg,
        'modes': MODE_LABELS,
        'mode_help': MODE_HELP[mode] + (PUBLIC_MODE_NOTE if public_relays else ''),
    }
