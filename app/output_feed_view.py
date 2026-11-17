"""What the IPTV Output page shows (templates/iptv_output.html, mockup 53, dev/changelog/1526).

One function, page_context(), and a fixed number of queries whatever the guide holds: the
catalog's own, the accounts, the sports channels' games, the listings on now for the channels
being watched, and one page of Recent plays with its counts. Nothing here writes; the page's
actions are app/routes/iptv_output.py.

The password never reaches this module's output. The Xtream card shows it through the settings
reveal, and the two playlist addresses are handed over as templates with PASSWORD_SLOT where
the password goes, so the page source holds no secret.
"""
from datetime import datetime, timedelta
from urllib.parse import quote, urlsplit

from flask import url_for

from . import connection_limits as connlim
from . import output_feed
from .config import MASK_SENTINEL, public_url
from .database import (FEED_PLAY_ENDED, FEED_PLAY_REDIRECTED, FEED_PLAY_REFUSED, Account,
                       EPGEntry, OutputFeedPlay)
from .fmt_utils import counted, fmt_bytes, fmt_duration
from .tz_utils import day_label, format_local, get_display_tz, is_24h

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

PROXY_UNSEEN = 'unseen'       # a reverse proxy's headers arrive and flask.behind_proxy is off
PROXY_RESTART = 'restart'     # it was turned on in config.yaml and waits for a restart
#: The headers a reverse proxy adds to what it passes on.
_PROXY_HEADERS = ('X-Forwarded-For', 'X-Forwarded-Proto', 'X-Forwarded-Host', 'X-Real-IP',
                  'Forwarded')
PLAYS_ALL = 'all'

#: What the IPTV Output page's mode setting says under each choice.
MODE_HELP = {
    output_feed.MODE_PROXY: ('ChannelBin plays the channel through itself. It can move a group to another '
                             'channel mid-stream and say why a play stopped.'),
    output_feed.MODE_MIXED: ('Channel groups and sports channels play through ChannelBin, so a group can '
                             'move to another channel mid-stream and a sports channel\'s waiting screen '
                             'turns into the game by itself. Every other channel is sent straight to your '
                             'IPTV provider\'s own stream, which ChannelBin never sees end.'),
    output_feed.MODE_REDIRECT: ('The IPTV player is sent to your IPTV provider\'s own stream. No move to '
                                'another channel, your provider\'s login reaches the player, and '
                                'ChannelBin never sees the play end.'),
}

#: The mode choice as the IPTV Output page and Settings offer it, in the order shown.
MODE_LABELS = [(output_feed.MODE_PROXY, 'Proxy, everything through ChannelBin'),
               (output_feed.MODE_MIXED, 'Mixed, groups and sports channels through ChannelBin'),
               (output_feed.MODE_REDIRECT, 'Redirect, everything straight from the provider')]

KIND_TEXT = {output_feed.KIND_CHANNEL: 'Channel', output_feed.KIND_GROUP: 'Channel group',
             output_feed.KIND_SPORTS: 'Sports channel'}
_PLAY_CHIPS = {FEED_PLAY_ENDED: 'Ended', FEED_PLAY_REFUSED: 'Refused',
               FEED_PLAY_REDIRECTED: 'Tuned in'}
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


def proxy_state(cfg: dict, headers, wired: bool) -> str:
    """'' when ChannelBin sees its visitors as they are, else why it does not. `headers` are
    the page request's own and `wired` is whether this process trusts a proxy's headers
    (app.config['BEHIND_PROXY']). With a proxy in front and the setting off, every request
    looks like the proxy's: http where the visitor typed https, and the proxy's address for
    every IPTV player. The headers are only looked for here, never believed: believing them
    is what the setting turns on (app/config.py)."""
    if wired or not any(headers.get(name) for name in _PROXY_HEADERS):
        return ''
    return PROXY_RESTART if (cfg.get('flask') or {}).get('behind_proxy') else PROXY_UNSEEN


def _connect(fcfg: dict, base: str) -> dict:
    user = output_feed.feed_username(fcfg)
    query = f'username={quote(user, safe="")}&password={PASSWORD_SLOT}'
    return {'server': base, 'username': user,
            'm3u': f'{base}/get.php?{query}&type=m3u_plus',
            'xmltv': f'{base}/xmltv.php?{query}'}


_NO_SPORTS = (None, None, None, None)


def _sports_programs(rows, now: datetime, days: int, after, blocked=frozenset(), allowed=None) -> tuple:
    """({sports row id: (the game on now, the last one that ended no more than `after` ago, the
    next one, the first one opened with Go live now), each or None}, {event id: [names of the
    sports channels carrying it]}), from guide_programs' two queries. Never playing_now() per row.
    `blocked` and `allowed` are the account ids a play drops, so a game's description counts the
    channels a tune-in could really use, as the guide the feed sends does."""
    from . import sports_feed
    sports = [r for r in rows if r.kind == output_feed.KIND_SPORTS]
    names = {r.row.row.id: r.name for r in sports}
    out, carrying = {}, {}
    for program in sports_feed.guide_programs([r.row for r in sports], now - after, days,
                                               blocked=blocked, allowed=allowed):
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
    order sports_feed.playing_now() takes listings in: a game opened with Go live now, then on
    now, then a game that may be running long, then the next game's early start; a game outside
    its own time counts only when a guide lists it, since playing_now() has nothing to play
    otherwise. (None, '') when the channel has nothing near. The guide's times stand in for the
    listings' here, so this is what the row says, not a play."""
    from .sports_feed import PHASE_AFTER, PHASE_BEFORE, PHASE_LIVE_NOW, PHASE_ON
    before, after = window
    on_now, ended, following, opened = entry
    if opened is not None and opened.listed:
        # Opened early, it holds the channel; once its time comes it reads as any game does, as
        # the play's own label does (playing_now relabels only the time before its listing).
        if now < opened.start:
            return opened, PHASE_LIVE_NOW
        return opened, PHASE_ON if now < opened.stop else PHASE_AFTER
    if on_now is not None:
        return on_now, PHASE_ON
    if ended is not None and ended.listed and now < ended.stop + after:
        return ended, PHASE_AFTER
    if following is not None and following.listed and following.start - before <= now:
        return following, PHASE_BEFORE
    return None, ''


def _row_view(row, accounts: dict, blocked, allowed, sports: dict, carrying: dict, watching: dict, now, tz,
              h24, window: tuple, fcfg: dict) -> dict:
    """One feed row as the Channels card shows it. `state` is exactly one of on / hidden /
    left_out; `problem` says why a play would not start, and is set on a left-out row too.
    `how` is how a play of it is delivered, said only in mixed mode, where rows differ."""
    view = {'feed_id': row.feed_id, 'kind': row.kind, 'kind_text': KIND_TEXT[row.kind],
            'name': row.name, 'category': row.category, 'num': row.num or '-',
            'is_sports': row.kind == output_feed.KIND_SPORTS,
            'on': row.offered, 'switchable': True, 'switch_tip': '', 'problem': '',
            'plays': None, 'note': '', 'sub': KIND_TEXT[row.kind], 'live': None,
            'watching': watching.get(row.feed_id, 0), 'how': '', 'how_tip': ''}
    if output_feed.mode_of(fcfg) == output_feed.MODE_MIXED:
        if output_feed.relays(fcfg, row.feed_id):
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
        from .sports_feed import (PHASE_AFTER, PHASE_BEFORE, PHASE_LIVE_NOW, PHASE_ON, live_now_offer,
                                  live_now_on)
        entry = sports.get(served.row.id, _NO_SPORTS)
        tuned, phase = _sports_tuned(entry, now, window)
        following = entry[2]
        view['sports_now'] = tuned.title if tuned is not None else ''

        def offer(program):
            return live_now_offer(program.event, program.listings, carrying.get(program.event.id, ()),
                                  now, window[1], tz, h24, blocked=blocked, allowed=allowed)

        # Turn off stays on the row for as long as the game it opened is what the row shows.
        if tuned is not None and tuned.event is not None and live_now_on(tuned.event, now):
            view['live'] = offer(tuned)
        if phase == PHASE_LIVE_NOW:
            view['sports_when'] = 'playing ahead of its listing'
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
                            if following is not None else 'No game is waiting for it')
            if following is not None and following.event is not None:
                view['live'] = offer(following)
        else:
            raise ValueError(f'Unknown play phase "{phase}"')
    else:
        raise ValueError(f'Unknown feed row kind "{row.kind}"')
    view['hay'] = ' '.join(x for x in (row.name, row.category,
                                        (view['plays'] or {}).get('name', '')) if x).lower()
    return view


def _watching(sessions, by_feed: dict, accounts: dict, sports: dict, now, tz, h24, window: tuple) -> list:
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
        view = {'id': s.id, 'client': s.client, 'name': row.name if row else s.label,
                'kind_text': KIND_TEXT[row.kind] if row else '', 'row_url': '',
                'channel_name': s.channel_name,
                'channel_url': (url_for('channels.channel_detail', channel_id=s.channel_id)
                                if s.channel_id is not None else ''),
                'account': accounts.get(s.account_id),
                'starting': s.state == output_feed.STATE_STARTING,
                'waiting': s.state == output_feed.STATE_WAITING,
                'waiting_text': _waiting_text(s),
                'from_screen': (f'Came from the waiting screen after {fmt_duration(s.screen_seconds)}'
                                if s.left_screen_at is not None else ''),
                'elapsed': fmt_duration(d['elapsed_seconds']),
                'sent': fmt_bytes(s.bytes_sent) if s.bytes_sent else '-',
                'moves': len(s.failovers), 'moves_text': _times(len(s.failovers)),
                'moves_tip': '\n'.join(output_feed.move_text(a, b, why) for a, b, why in s.failovers),
                'reserve_note': output_feed.reserve_short_note(s.ring),
                'notes': output_feed.play_notes(s),
                'program': '', 'program_times': ''}
        if row is not None and row.kind == output_feed.KIND_SPORTS:
            view['row_url'] = url_for('sports.league_page', league_id=row.row.league.id)
            tuned, _phase = _sports_tuned(sports.get(row.row.row.id, _NO_SPORTS), now, window)
            view['program'] = tuned.title if tuned is not None else ''
        elif row is not None:
            view['row_url'] = (url_for('channels.channel_detail', channel_id=row.row.id)
                               if row.kind == output_feed.KIND_CHANNEL
                               else url_for('channel_groups.group_detail', group_id=row.row.id))
            hit = programs.get(row.listings_channel_id)
            if hit is not None:
                view['program'] = hit[0] or ''
                view['program_times'] = (f'{format_local(hit[1], "clock", tz=tz, h24=h24)} - '
                                         f'{format_local(hit[2], "clock", tz=tz, h24=h24)}')
        out.append(view)
    return out


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
    game = f'Waiting for {s.waiting_for}' if s.waiting_for else 'No game scheduled'
    return f'{game}. {why}' if why else game


def _recent(args, mode: str, by_feed: dict, now, tz, h24) -> dict:
    """One page of Recent plays and the chips' counts: two queries."""
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
    kinds += [k for k in (FEED_PLAY_ENDED, FEED_PLAY_REDIRECTED) if k not in kinds and counts.get(k)]
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

    lines = []
    for play in found:
        row = by_feed.get(play.feed_id)
        # The stored message names the row the way the log does ("group 2 (USA)"); the
        # column beside it already says which row, in the name the user knows.
        text = (play.message or '').replace(f' for {play.label}', '')
        line = {'kind': play.kind, 'when': f'{day_label(play.at, now, tz)}, {clock(play.at)}',
                'client': play.client or '-', 'name': row.name if row else play.label,
                'text': text}
        if play.kind == FEED_PLAY_ENDED:
            line['badge'] = ('b-abort', 'Ended')
            line['detail'] = (f'Played {play.channel_name or "-"} for {fmt_duration(play.seconds)}'
                              + (f', moved to another channel {_times(play.moves)}' if play.moves else ''))
        elif play.kind == FEED_PLAY_REFUSED:
            line['badge'] = ('b-warn', 'Refused')
            line['detail'] = (f'Asked {play.count} times, last at {clock(play.at)}'
                              if play.count > 1 else 'Asked once')
        elif play.kind == FEED_PLAY_REDIRECTED:
            line['badge'] = ('b-sched', 'Sent to the provider')
            line['detail'] = f'To {play.channel_name or "-"}. ChannelBin cannot see when it stopped.'
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
        else:
            on = set()      # a sports channel plays from whichever account lists the game then
        for account_id in on:
            if account_id in included:
                included[account_id] += 1
    streams = {}
    for s in sessions:
        streams[s.account_id] = streams.get(s.account_id, 0) + 1
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
    sports, carrying = _sports_programs(rows, now, int(fcfg.get('xmltv_days') or 3), window[1],
                                        blocked=blocked, allowed=allowed)
    # In redirect mode the one play ChannelBin can see is a sports channel's waiting screen,
    # which is its own stream there too (dev/changelog/1534).
    sessions = ([s for s in output_feed.live_sessions() if not redirect or s.waiting]
                if state == STATE_ON else [])
    watching = {}
    for s in sessions:
        watching[s.feed_id] = watching.get(s.feed_id, 0) + 1
    views = [_row_view(r, accounts, blocked, allowed, sports, carrying, watching, now, tz, h24, window, fcfg)
             for r in rows]
    address = _address(cfg, host_url)
    max_viewers = int(fcfg.get('max_viewers') or 1)
    return {
        'state': state, 'is_on': state == STATE_ON, 'is_off': state == STATE_OFF,
        'needs_login': state == STATE_SETUP, 'mode': mode, 'redirect': redirect,
        'mixed': mode == output_feed.MODE_MIXED, 'relays_some': output_feed.relays_some(fcfg),
        'redirects_some': output_feed.redirects_some(fcfg),
        'dormant': state != STATE_ON, 'mask': MASK_SENTINEL, 'bounds': output_feed.SETTING_BOUNDS,
        'address': address, 'proxy': proxy, 'channels_per_page': CHANNELS_PER_PAGE,
        'connect': _connect(fcfg, address['base']),
        'password_slot': PASSWORD_SLOT,
        'rows': views,
        'categories': sorted({v['category'] for v in views}, key=str.lower),
        'counts': {'all': len(views), 'on': sum(1 for v in views if v['on']),
                   'hidden': sum(1 for v in views if v['state'] == 'hidden'),
                   'problem': sum(1 for v in views if v['problem'])},
        'watching': _watching(sessions, by_feed, accounts, sports, now, tz, h24, window),
        'at_limit': sum(1 for s in sessions if s.counted) >= max_viewers,
        'max_viewers': max_viewers,
        'recent': _recent(args, mode, by_feed, now, tz, h24),
        'accounts': _accounts(rows, sessions, accounts, cfg, fcfg),
        'feedcfg': fcfg,
        'modes': MODE_LABELS, 'mode_help': MODE_HELP[mode],
    }
