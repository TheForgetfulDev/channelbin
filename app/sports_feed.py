"""ChannelBin's own sports channels on the output feed (dev/changelog/1504, 1517, 1518).

A followed team is a channel ("Philadelphia Flyers" in an "NHL" category) and a league followed
whole is a set of numbered ones ("NHL 1" to "NHL N"). Each has a guide of its games from
TheSportsDB's schedule and plays whichever provider channel's guide lists the game near now.
app/output_feed.py serves them as its third kind of feed channel; this module is what a sports
channel is: which ones are served, what their guide says, what one plays at a given moment, the
switches, and where a league's games are laid out.

Rules this module holds, each guarded in tests/test_sports_feed.py:

* Watch only and feed only. A sports channel is never a channel group, never in the TV Guide, and
  nothing records from it.
* It plays only from a channel whose guide lists the game near that moment: a strong guide match
  still in the guide, not marked wrong, not a replay, on a channel that is not hidden, whose
  listing is on or within the output's sports window of it (sports_match.airing_now,
  dev/changelog/1529). Never an event channel's name.
* A row of sports_feed_channels is an identity, and its id is the feed id an IPTV player caches. A
  switch going off or a follow ending stops the channel being served and keeps the row.
* A team's channel is served while its switch is on and the team is followed; a league's numbered
  channels while the league's switch is on and the league is followed whole. Both are read where
  the channel is served, so no path that changes a follow has to remember them.
* Go live now is a fact about a game, not a channel (dev/changelog/1530): a person said it is on
  early, so every sports channel carrying it plays it ahead of its window until the end that was
  fixed when the button was pressed. Written by set_live_now() and nothing else.
* A league's layout is stored. SportsEvent.feed_number is written where the schedule is written
  and a game keeps it, with one exception: a game whose time has changed so that it lands on
  another's on the same channel is placed again, since the guide an IPTV player cached for it was
  already wrong about its time. The number of channels only grows.
"""
import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Optional

from sqlalchemy import func, or_

from . import db, sports_match
from .database import Channel, SportsEvent, SportsFeedChannel, SportsFollow, SportsLeague, SportsTeam
from .db_utils import retry_on_locked
from .fmt_utils import counted
from .sports_data import event_game_name, event_shape_is_matchup

log = logging.getLogger(__name__)

#: How long a game is taken to run when no guide listing says. TheSportsDB gives a start and no end.
MATCHUP_LENGTH = timedelta(hours=3, minutes=30)
FIELD_LENGTH = timedelta(hours=4)
MIN_LENGTH = timedelta(hours=1)
#: The longest a game's listing is believed, and how far back a game that may still be on is
#: looked for: a golf round or an endurance race is listed for most of a day.
MAX_LENGTH = timedelta(hours=12)
#: Two games on one numbered channel may run into each other by this much before one of them is
#: placed again. Less than this is one game running long, which the guide shows by ending it
#: where the next starts.
COLLISION = timedelta(minutes=30)
#: A listing that starts within this of the game's start is the game itself, so its end is the
#: game's end; one further off is a pregame show or a later airing.
START_AGREE = timedelta(minutes=90)
#: How many of a channel's coming events a play looks at to find the one on now or the next.
_PLAY_EVENTS = 24


class NotFollowed(Exception):
    """A switch was turned on for something that is not followed. The message is user-facing."""


@dataclass
class Served:
    """One sports channel the feed serves now."""
    row: SportsFeedChannel = field(repr=False)
    league: SportsLeague = field(repr=False)
    name: str

    @property
    def is_team(self) -> bool:
        return bool(self.row.team_tsdb_id)


@dataclass
class Program:
    """One game in a sports channel's guide."""
    row_id: int
    start: datetime
    stop: datetime
    title: str
    description: str
    category: str
    #: How many channels list the game at its time, on any account: 0 when no guide lists it.
    listed: int = 0
    #: The game, and its listings a play may take [(channel id, account id, start, stop)], for a
    #: page that says what Go live now would do without asking again.
    event: Optional[SportsEvent] = field(default=None, repr=False)
    listings: tuple = field(default=(), repr=False)


#: Where a play's moment falls against the listing it plays from (playing_now). Every value is
#: named wherever a phase is read.
PHASE_ON = 'on'          # the listing is on now
PHASE_AFTER = 'after'    # the listing ended no more than the window's `after` ago
PHASE_BEFORE = 'before'  # the listing starts no more than the window's `before` from now
PHASE_LIVE_NOW = 'live_now'  # a person opened the game early with Go live now (set_live_now)


@dataclass
class NowPlaying:
    """What a sports channel has on at one moment: the game and the channels whose guide lists it
    within the window, or `why_none`, a clause that finishes "Nothing to play for <channel>: ...".
    `note` says where now falls against the listings played from when that is not "on now"."""
    event: Optional[SportsEvent] = field(default=None, repr=False)
    channels: list = field(default_factory=list, repr=False)
    why_none: str = ''
    note: str = ''


# ---------------------------------------------------------------------------
# Which channels are served
# ---------------------------------------------------------------------------

def _follow_scope() -> dict:
    """{league id: (followed whole, {team ids})} over every follow, a person's and a rule's alike."""
    out = {}
    for league_id, team_id in db.session.query(SportsFollow.league_id, SportsFollow.team_tsdb_id).all():
        whole, teams = out.get(league_id, (False, set()))
        if team_id:
            teams.add(team_id)
        out[league_id] = (whole or not team_id, teams)
    return out


def served_channels(row_id: int = None) -> list:
    """Every sports channel the feed serves now, or just `row_id` when it is one. At most three
    queries whatever is stored, and one when there is no sports channel at all. Ordered by league,
    its team channels by name, then its numbered ones."""
    q = db.session.query(SportsFeedChannel, SportsLeague).join(
        SportsLeague, SportsLeague.id == SportsFeedChannel.league_id)
    if row_id is not None:
        q = q.filter(SportsFeedChannel.id == row_id)
    rows = q.all()
    if not rows:
        return []
    scope = _follow_scope()
    team_ids = {r.team_tsdb_id for r, _l in rows if r.team_tsdb_id}
    names = dict(db.session.query(SportsTeam.tsdb_id, SportsTeam.full_name)
                 .filter(SportsTeam.tsdb_id.in_(team_ids)).all()) if team_ids else {}
    out = []
    for row, league in rows:
        whole, teams = scope.get(league.id, (False, frozenset()))
        if row.team_tsdb_id:
            if row.enabled and (whole or row.team_tsdb_id in teams):
                out.append(Served(row, league, names.get(row.team_tsdb_id) or f'Team {row.team_tsdb_id}'))
        elif league.feed_league_channels and whole:
            out.append(Served(row, league, f'{league.name} {row.number}'))
    out.sort(key=lambda s: (s.league.name.lower(), s.league.id, not s.is_team,
                            s.name.lower() if s.is_team else '', s.row.number))
    return out


# ---------------------------------------------------------------------------
# A game's time on a sports channel
# ---------------------------------------------------------------------------

def event_window(event, listings=()) -> Optional[tuple]:
    """(start, stop) of a game on a sports channel, or None when it has no time to show. The start
    is the guide's own when the matcher has found the game there (SportsEvent.guide_start), else
    TheSportsDB's. The stop is the latest end among `listings` [(channel id, account id, start,
    stop)] that start with the game, else a length by the event's shape. A postponed game has no time, and
    neither does a field event still at TheSportsDB's 00:00 placeholder (DESIGN-sports-data.md 7)."""
    start = event.guide_start or event.start_at
    if start is None or event.postponed:
        return None
    matchup = event_shape_is_matchup(event)
    if not matchup and event.guide_start is None and (start.hour, start.minute) == (0, 0):
        return None
    ends = [stop for _c, _a, _begin, stop in _with_the_game(listings, start) if stop is not None]
    stop = max(ends) if ends else start + (MATCHUP_LENGTH if matchup else FIELD_LENGTH)
    return start, min(max(stop, start + MIN_LENGTH), start + MAX_LENGTH)


def _with_the_game(listings, start: datetime) -> list:
    """The listings that start with the game, which is what "a channel lists it" means for its end
    time and for how many channels carry it. One further off is a pregame show or a later airing."""
    return [row for row in listings if row[2] is not None and abs(row[2] - start) <= START_AGREE]


def placement_window(event) -> Optional[tuple]:
    """(start, stop) of a game for laying a league out. TheSportsDB's own start and a length by
    shape, so it moves only when TheSportsDB moves the game: the guide's start shifts by minutes as
    listings come and go, and a layout must not. A field event at the 00:00 placeholder has only
    the guide's start to go on."""
    start = event.start_at
    matchup = event_shape_is_matchup(event)
    if start is not None and not matchup and (start.hour, start.minute) == (0, 0):
        start = event.guide_start
    if start is None or event.postponed:
        return None
    return start, start + (MATCHUP_LENGTH if matchup else FIELD_LENGTH)


def _event_start():
    return func.coalesce(SportsEvent.guide_start, SportsEvent.start_at)


def _events_of(served: Served):
    if served.is_team:
        team = served.row.team_tsdb_id
        return SportsEvent.query.filter(SportsEvent.league_id == served.league.id,
                                        or_(SportsEvent.home_team_id == team, SportsEvent.away_team_id == team))
    return SportsEvent.query.filter(SportsEvent.league_id == served.league.id,
                                    SportsEvent.feed_number == served.row.number)


def _carrier_keys(served: list) -> tuple:
    numbered, teams = {}, {}
    for s in served:
        if s.is_team:
            teams[(s.league.id, s.row.team_tsdb_id)] = s
        else:
            numbered[(s.league.id, s.row.number)] = s
    return numbered, teams


def _carriers_of(event, numbered: dict, teams: dict) -> list:
    """The served channels that carry `event`: its league's numbered channel and each team's."""
    rows = [numbered.get((event.league_id, event.feed_number)),
            teams.get((event.league_id, event.home_team_id)),
            teams.get((event.league_id, event.away_team_id))]
    return [s for s in rows if s is not None]


def carriers(served: list, events) -> dict:
    """{event id: [Served]} for each of `events` some served channel carries. No query."""
    numbered, teams = _carrier_keys(served)
    out = {}
    for event in events:
        rows = _carriers_of(event, numbered, teams)
        if rows:
            out[event.id] = rows
    return out


#: How many of a sports channel's coming games its waiting screen draws: the next and three later
#: (dev/mockups/55, look 7).
SCREEN_GAMES = 4


def screen_events(served: list, now: datetime) -> dict:
    """{channel row id: [SportsEvent]}: each of `served`'s next SCREEN_GAMES games not yet over, in
    start order, which is what its waiting screen draws (app/sports_screen.py). One query."""
    if not served:
        return {}
    numbered, teams = _carrier_keys(served)
    start = _event_start()
    events = (SportsEvent.query.filter(SportsEvent.league_id.in_({s.league.id for s in served}),
                                       start > now - MAX_LENGTH, SportsEvent.postponed.is_(False))
              .order_by(start, SportsEvent.id).all())
    out = {}
    for event in events:
        window = event_window(event)
        if window is None or window[1] <= now:
            continue
        for s in _carriers_of(event, numbered, teams):
            games = out.setdefault(s.row.id, [])
            if len(games) < SCREEN_GAMES:
                games.append(event)
    return out


def screen_team_ids(now: datetime) -> set:
    """The TheSportsDB ids of the teams in the games a served channel's waiting screen would draw
    now (screen_events()). What a badge is downloaded for (app/sports_images.py). Four queries
    whatever is stored, one with no sports channel."""
    return {i for games in screen_events(served_channels(), now).values()
            for event in games for i in (event.home_team_id, event.away_team_id) if i}


# ---------------------------------------------------------------------------
# The guide
# ---------------------------------------------------------------------------

def _description(event, channels: int, unusable: int) -> str:
    """A game's guide description. Says where the stream comes from, and says so when there is
    none: an IPTV player shows this before anyone presses play. `channels` list the game and can be
    played from; `unusable` list it on an account the feed cannot use right now."""
    bits = []
    where = ', '.join(x for x in (event.venue, event.city) if x)
    if where:
        bits.append(f'{where}.')
    if channels:
        bits.append(f'ChannelBin plays this from one of the {counted(channels, "channel")} whose guide lists it.'
                    if channels > 1 else 'ChannelBin plays this from the 1 channel whose guide lists it.')
    elif unusable:
        bits.append(f'No stream it can play: {counted(unusable, "channel")} '
                    f"{'lists' if unusable == 1 else 'list'} this, on an account that is blocked or that "
                    'the IPTV output is not allowed to use.')
    else:
        bits.append("No stream found yet: none of your channels' guides lists this. "
                    'ChannelBin looks again after each guide update.')
    if event.missing_since is not None:
        bits.append("TheSportsDB's latest schedule no longer lists it.")
    return ' '.join(bits)


def guide_programs(served: list, now: datetime, days: int, blocked=frozenset(), allowed=None) -> list:
    """Every game in the served channels' guides from `now` for `days`, as Programs ordered by
    channel then start. Two queries whatever is served: the leagues' events in the window, and the
    listings matched to them. A game ends where the next one on its channel starts. `blocked` and
    `allowed` are the account ids a play would drop (output_feed._resolve_sports_play), so the
    description counts the channels a play could really use."""
    if not served:
        return []
    numbered, teams = _carrier_keys(served)
    start = _event_start()
    events = (SportsEvent.query.filter(SportsEvent.league_id.in_({s.league.id for s in served}),
                                       start > now - MAX_LENGTH, start < now + timedelta(days=days))
              .order_by(start, SportsEvent.id).all())
    listings = sports_match.listing_windows([e.id for e in events])
    by_row = {}
    for event in events:
        rows = _carriers_of(event, numbered, teams)
        if not rows:
            continue
        mine = listings.get(event.id, ())
        window = event_window(event, mine)
        if window is None or window[1] <= now:
            continue
        carrying = _with_the_game(mine, window[0])
        usable = {c for c, account, _b, _e in carrying
                  if account not in blocked and (allowed is None or account in allowed)}
        listed = {c for c, _a, _b, _e in carrying}
        description = _description(event, len(usable), len(listed - usable))
        for s in rows:
            by_row.setdefault(s.row.id, []).append(Program(
                s.row.id, window[0], window[1], event_game_name(event), description,
                s.league.sport or 'Sports', len(listed), event, tuple(mine)))
    out = []
    for s in served:
        programs = sorted(by_row.get(s.row.id, ()), key=lambda p: p.start)
        for program, following in zip(programs, programs[1:] + [None]):
            if following is not None and following.start < program.stop:
                program.stop = following.start
            if program.stop > program.start:
                out.append(program)
    return out


# ---------------------------------------------------------------------------
# What a channel plays now
# ---------------------------------------------------------------------------

def _when(start: datetime, now: datetime) -> str:
    from .tz_utils import day_label, format_local, get_display_tz, is_24h
    tz = get_display_tz()
    return f'{day_label(start, now, tz)} {format_local(start, "clock", tz=tz, h24=is_24h())}'


def _pick(by_id: dict, listings: list, now: datetime) -> Optional[tuple]:
    """(event, {channel ids}, phase, listing start, listing stop) for the game a play takes from
    `listings` [(event id, channel id, start, stop)], or None when there are none. A listing on now
    wins; else the one that ended last, since its game may be running long; else the one that
    starts soonest. So a game's tail holds the channel until the next game's listing starts."""
    on, ended, coming = {}, {}, {}
    for found, channel_id, begin, stop in listings:
        if found not in by_id:
            continue
        bucket = on if begin <= now < stop else ended if stop <= now else coming
        channels, lo, hi = bucket.get(found, (set(), begin, stop))
        channels.add(channel_id)
        bucket[found] = (channels, min(lo, begin), max(hi, stop))
    if on:
        # Two games listed at once on one channel: the one the guide shows now is the latest that
        # has started (guide_programs ends a game where the next starts). A listing can run from
        # before its game, as a pregame block, so a game not yet started is taken only when
        # nothing that has started is listed.
        def begins(e):
            return e.guide_start or e.start_at or datetime.min
        listed = [by_id[i] for i in on]
        started = [e for e in listed if begins(e) <= now]
        event = (max(started, key=lambda e: (begins(e), e.id)) if started
                 else min(listed, key=lambda e: (begins(e), e.id)))
        channels, begin, stop = on[event.id]
        return event, channels, PHASE_ON, begin, stop
    if ended:
        found = max(ended, key=lambda i: (ended[i][2], i))
        return (by_id[found], ended[found][0], PHASE_AFTER, ended[found][1], ended[found][2])
    if coming:
        found = min(coming, key=lambda i: (coming[i][1], i))
        return (by_id[found], coming[found][0], PHASE_BEFORE, coming[found][1], coming[found][2])
    return None


def _phase_note(phase: str, begin: datetime, stop: datetime, now: datetime) -> str:
    if phase == PHASE_ON:
        return ''
    if phase == PHASE_AFTER:
        return f'its listing ended {_when(stop, now)}; the game may be running long'
    if phase == PHASE_BEFORE:
        return f'its listing starts {_when(begin, now)}'
    if phase == PHASE_LIVE_NOW:
        return f'Go live now is on; its listing starts {_when(begin, now)}'
    raise ValueError(f'Unknown play phase "{phase}"')


def playing_now(served: Served, now: datetime, event_id: int = None, before: timedelta = timedelta(0),
                after: timedelta = timedelta(0)) -> NowPlaying:
    """The game `served` has on at `now` and the channels whose guide lists it, from `before` ahead
    of a listing's start to `after` past its end (integrations.output_feed's sports window,
    dev/changelog/1529): a viewer tuning in for the pregame or to a game running long expects
    something on. Zero for both is the listings on at this moment. `event_id` holds the answer to
    that one game, which is how a failover stays on the game it started on. A game opened with Go
    live now plays from its listings that start with the game, whenever they start, until its
    stored end (set_live_now).
    With nothing to play, `why_none` says which of four things is true: no game is near, a game is
    near that no guide lists, the game this play was held to is over, or a game opened early has no
    listing any more."""
    start = _event_start()
    q = _events_of(served).filter(start > now - MAX_LENGTH - after)
    if event_id is not None:
        q = q.filter(SportsEvent.id == event_id)
    events = q.order_by(start, SportsEvent.id).limit(_PLAY_EVENTS).all()
    by_id = {e.id: e for e in events}
    opened = {i: e for i, e in by_id.items() if live_now_on(e, now)}
    picked = None
    if opened:
        # A game a person opened early wins: they said it is on. Its listings count up to the end
        # fixed when the button was pressed, and no further.
        reach = max(e.live_now_until for e in opened.values()) - now
        rows = [row for row in sports_match.airing_now(list(opened), now, reach, after)
                if _starts_with_game(opened[row[0]], row[2])]
        picked = _pick(opened, rows, now)
        if picked is not None and picked[2] == PHASE_BEFORE:
            picked = picked[:2] + (PHASE_LIVE_NOW,) + picked[3:]
    if picked is None:
        picked = _pick(by_id, sports_match.airing_now(list(by_id), now, before, after), now)
    if picked is not None:
        event, channel_ids, phase, begin, stop = picked
        channels = Channel.query.filter(Channel.id.in_(channel_ids)).all()
        return NowPlaying(event=event, channels=channels, note=_phase_note(phase, begin, stop, now))
    if opened:
        name = event_game_name(min(opened.values(), key=lambda e: (e.live_now_until, e.id)))
        return NowPlaying(why_none=f"Go live now is on for {name}, but no channel's guide lists it any more")
    if event_id is not None:
        name = event_game_name(events[0]) if events else 'the game'
        return NowPlaying(why_none=f"no channel's guide lists {name} any more")
    windows = [(e, event_window(e)) for e in events]
    for event, window in windows:
        if window is not None and window[0] - before <= now < window[1]:
            when = '' if window[0] <= now else f', {_when(window[0], now)}'
            return NowPlaying(event=event, why_none=(
                f"no stream is found yet for {event_game_name(event)}{when}: none of your channels' "
                f'guides lists it right now'))
    for event, window in windows:
        if window is not None and window[0] > now:
            return NowPlaying(why_none=f'no game is on right now. Next: {event_game_name(event)}, '
                                       f'{_when(window[0], now)}{_plays_from(event, window[0], before, now)}')
    return NowPlaying(why_none='no game is on right now, and none is in the schedule')


def _plays_from(event, shown: datetime, before: timedelta, now: datetime) -> str:
    """When the channel starts playing `event`, as a clause after its time: from `before` ahead of
    its earliest listing, said only when that is not the time already shown. One query, on the
    refusal path only."""
    starts = [begin for _c, _a, begin, _stop in sports_match.listing_windows([event.id]).get(event.id, ())
              if begin is not None and begin > now]
    if not starts:
        return ''
    opens = min(starts) - before
    return '' if opens == shown else f'. This channel plays it from {_when(opens, now)}'


# ---------------------------------------------------------------------------
# Go live now
# ---------------------------------------------------------------------------

class LiveNowRefused(Exception):
    """Go live now has nothing to open for a game. The message is user-facing."""


def live_now_on(event, now: datetime) -> bool:
    return event.live_now_until is not None and now < event.live_now_until


def _starts_with_game(event, begin) -> bool:
    """Whether a listing starting at `begin` is the game itself, the rule _with_the_game() applies
    for the guide's own count, so a page's "listed" and what Go live now plays never disagree."""
    window = event_window(event)
    return window is not None and begin is not None and abs(begin - window[0]) <= START_AGREE


def _game_listings(event, listings) -> list:
    return [row for row in listings if row[3] is not None and _starts_with_game(event, row[2])]


def live_now_end(event, listings, now: datetime, after: timedelta) -> Optional[datetime]:
    """When Go live now for `event` turns itself off: `after` past the end of its listings that
    start with the game and are still to end. That is where the game's own window ends, so the
    switch cannot outlive the game. None when no such listing is left: there is nothing to play.
    `listings` are [(channel id, account id, start, stop)], already narrowed to usable accounts."""
    ahead = [row for row in _game_listings(event, listings) if row[3] > now]
    return max(row[3] for row in ahead) + after if ahead else None


def at_clause(dt: datetime, now: datetime, tz, h24: bool) -> str:
    """"today at 7:00 PM", "Sun Nov 15 at 7:00 PM": a time inside a sentence."""
    from .tz_utils import day_label, format_local
    day = day_label(dt, now, tz)
    day = day.lower() if day in ('Today', 'Tomorrow', 'Yesterday') else day
    return f'{day} at {format_local(dt, "clock", tz=tz, h24=h24)}'


def _usable(listings, blocked, allowed) -> list:
    return [row for row in listings if row[1] not in blocked and (allowed is None or row[1] in allowed)]


def _live_now_problem(event, listings, usable: list, channels: list, now: datetime) -> str:
    """Why Go live now has nothing to open for `event`, or ''. Every case is named: a game over
    never reads as one not listed yet."""
    game = event_game_name(event)
    if event.postponed:
        return f'{game} is postponed, so there is no time to open it for.'
    mine = _game_listings(event, listings)
    if not mine:
        return (f"No channel's guide lists {game} yet, so there is nothing to play. ChannelBin looks "
                'again after each guide update.')
    if not any(row[3] > now for row in mine):
        return f"Every listing of {game} has ended, so there is nothing left to open."
    if not any(row[3] > now for row in _game_listings(event, usable)):
        return (f'Every channel whose guide lists {game} is on an account that is blocked right now or '
                'that the IPTV output does not play from.')
    if not channels:
        return (f'No sports channel on your IPTV output carries {game}. Switch one on for it on its '
                "league's page first.")
    return ''


def live_now_offer(event, listings, channels: list, now: datetime, after: timedelta, tz, h24: bool,
                   blocked=frozenset(), allowed=None) -> dict:
    """What a page shows for Go live now on one game, from what it has already read: the game's
    listings [(channel id, account id, start, stop)], the names of the sports channels carrying it,
    and the account ids a play drops (output_feed._resolve_sports_play). No query. `why` is set when
    there is nothing to open, and `message` is the confirm's text."""
    game = event_game_name(event)
    on = live_now_on(event, now)
    usable = _usable(listings, blocked, allowed)
    until = event.live_now_until if on else live_now_end(event, usable, now, after)
    out = {'event': event.id, 'game': game, 'on': on, 'channels': list(channels),
           'until': at_clause(until, now, tz, h24) if until is not None else '',
           'why': '' if on else _live_now_problem(event, listings, usable, channels, now), 'message': ''}
    if out['why'] or on:
        return out
    start = event.start_at
    if start is None or (not event_shape_is_matchup(event) and (start.hour, start.minute) == (0, 0)):
        scheduled = f'TheSportsDB has no start time for {game}'
    else:
        scheduled = f'{game} is scheduled to start {at_clause(start, now, tz, h24)}'
    starts = [row[2] for row in _game_listings(event, usable) if row[3] > now]
    listed = min(starts) if starts else None
    if listed is not None and listed != start:
        scheduled += f', and your guide lists it from {at_clause(listed, now, tz, h24)}'
    where = 'the sports channel' if len(channels) == 1 else 'any of the sports channels'
    out['message'] = (f'{scheduled}. From now on, tuning in to {where} below plays it, and Go live now '
                      f'turns itself off {out["until"]}. A play already running is not cut.')
    return out


@retry_on_locked()
def set_live_now(event_id: int, on: bool, after: timedelta, now: datetime = None, blocked=frozenset(),
                 allowed=None) -> Optional[dict]:
    """A person's Go live now for one game, the only writer of SportsEvent.live_now_at and
    live_now_until. Turning it on fixes its end with live_now_end(), so the switch dies with the
    game's own window whatever later happens to the game, and is refused (LiveNowRefused) when
    there is nothing to open (_live_now_problem names which). `blocked` and `allowed` are the
    account ids a play drops, so a game listed only where the feed cannot play is refused here
    rather than at every tune-in. Turning it off clears both. None when the game is gone. Returns {game, on, until,
    channels}."""
    now = now or datetime.utcnow()
    event = db.session.get(SportsEvent, event_id)
    if event is None:
        return None
    game = event_game_name(event)
    if not on:
        was = live_now_on(event, now)
        event.live_now_at = event.live_now_until = None
        db.session.commit()
        return {'game': game, 'on': False, 'was_on': was, 'until': None, 'channels': []}
    listings = sports_match.listing_windows([event.id]).get(event.id, ())
    usable = _usable(listings, blocked, allowed)
    until = live_now_end(event, usable, now, after)
    channels = [s.name for s in carriers(served_channels(), [event]).get(event.id, ())]
    problem = _live_now_problem(event, listings, usable, channels, now)
    if problem:
        db.session.rollback()
        raise LiveNowRefused(problem)
    event.live_now_at = now
    event.live_now_until = until
    db.session.commit()
    return {'game': game, 'on': True, 'was_on': False, 'until': until, 'channels': channels}


# ---------------------------------------------------------------------------
# A league's layout
# ---------------------------------------------------------------------------

def place_league_events(league: SportsLeague, now: datetime) -> int:
    """Give each game of `league` that has a time and no channel yet the lowest-numbered channel
    that is free for its whole length, and make sure a row exists for every number in use. Returns
    how many were placed. Mutates without committing: the caller's unit is the schedule write, or
    the switch going on.

    What is left to place is read from feed_number being NULL, which is the placement's own record,
    so this is re-runnable from the top. A game already placed keeps its number unless its time
    has changed onto another game's on the same number by more than COLLISION: then the one whose
    time changed last gives its number up and is placed again, a WARNING names both, and the move
    is kept on the game (feed_moved_*) for the league page to say. Left alone, one of the two
    would have no channel to be tuned on."""
    if not league.feed_league_channels:
        return 0
    start = _event_start()
    events = (SportsEvent.query.filter(SportsEvent.league_id == league.id, start > now - MAX_LENGTH)
              .order_by(start, SportsEvent.id).all())
    windows = {e.id: placement_window(e) for e in events}
    placed = {}
    for event in events:
        if event.feed_number is not None and windows[event.id] is not None:
            placed.setdefault(event.feed_number, []).append(event)
    for number, sharing in placed.items():
        sharing.sort(key=lambda e: (windows[e.id][0], e.id))
        kept = []
        for event in sharing:
            clash = next((k for k in kept if windows[k.id][1] - windows[event.id][0] > COLLISION), None)
            if clash is None:
                kept.append(event)
                continue
            # The one whose time changed last is the one that moved onto the other.
            mover = max((clash, event), key=lambda e: (e.start_changed_at or datetime.min, e.id))
            log.warning('Sports channels: %s and %s are now at the same time on %s %d, so %s is placed again',
                        event_game_name(clash), event_game_name(event), league.name, number,
                        event_game_name(mover))
            mover.feed_number = None
            # The first number it left is the one kept: that is where the oldest cached guide
            # still lists it, however many times it has moved since.
            mover.feed_moved_at = now
            if mover.feed_moved_from is None:
                mover.feed_moved_from = number
            mover.feed_moved_by = (event if mover is clash else clash).id
            if mover is clash:
                kept[kept.index(clash)] = event
    taken, todo = {}, []
    for event in events:
        window = windows[event.id]
        if window is None:
            continue
        if event.feed_number is None:
            todo.append((event, window))
        else:
            taken.setdefault(event.feed_number, []).append(window)
    for event, window in todo:
        number = 1
        while any(window[0] < stop and begin < window[1] for begin, stop in taken.get(number, ())):
            number += 1
        event.feed_number = number
        taken.setdefault(number, []).append(window)
    have = {n for (n,) in db.session.query(SportsFeedChannel.number).filter(
        SportsFeedChannel.league_id == league.id, SportsFeedChannel.team_tsdb_id == 0).all()}
    for number in range(1, max(taken, default=0) + 1):
        if number not in have:
            db.session.add(SportsFeedChannel(league_id=league.id, team_tsdb_id=0, number=number,
                                             enabled=False, created_at=now))
    return len(todo)


@retry_on_locked()
def place_all(now: datetime = None) -> int:
    """place_league_events() for every league whose switch is on, in one commit. The matcher calls
    it after a run, since a guide match is what gives a field event its time."""
    now = now or datetime.utcnow()
    placed = 0
    for league in SportsLeague.query.filter(SportsLeague.feed_league_channels.is_(True)).all():
        placed += place_league_events(league, now)
    if placed:
        db.session.commit()
        log.info('Sports channels: placed %s on league channels', counted(placed, 'game'))
    else:
        db.session.rollback()
    return placed


def place_all_guarded(now: datetime = None) -> int:
    """place_all() for a caller whose own work must not depend on it: the schedule worker and the
    matcher. A failure is logged with its traceback and the games stay unplaced, which the next
    run reads from feed_number and places."""
    try:
        return place_all(now)
    except Exception:  # noqa: BLE001 - a watch-only layout must never stop a schedule update or a match run
        db.session.rollback()
        log.exception('Sports channels: placing games on league channels failed; the next run tries again')
        return 0


# ---------------------------------------------------------------------------
# The switches
# ---------------------------------------------------------------------------

@retry_on_locked()
def set_team_channel(league_id: int, team_tsdb_id: int, enabled: bool) -> Optional[bool]:
    """A person's switch for one team's channel. None when the league is gone; NotFollowed when it
    is turned on for a team nothing follows, or one ChannelBin has never read from TheSportsDB.
    Turning it off keeps the row, and with it the feed id."""
    league = db.session.get(SportsLeague, league_id)
    if league is None:
        return None
    whole, teams = _follow_scope().get(league_id, (False, frozenset()))
    if enabled and not (whole or team_tsdb_id in teams):
        db.session.rollback()
        raise NotFollowed('Follow that team first: a sports channel is built from a followed '
                          "team's schedule.")
    row = SportsFeedChannel.query.filter_by(league_id=league_id, team_tsdb_id=team_tsdb_id, number=0).first()
    if (enabled and row is None and team_tsdb_id not in teams
            and db.session.query(SportsTeam.id).filter(SportsTeam.tsdb_id == team_tsdb_id).first() is None
            and db.session.query(SportsEvent.id).filter(
                SportsEvent.league_id == league_id,
                or_(SportsEvent.home_team_id == team_tsdb_id,
                    SportsEvent.away_team_id == team_tsdb_id)).first() is None):
        # A row is kept for good, so one is made only for a team ChannelBin has read from
        # TheSportsDB: one a schedule names, or one whose names are stored.
        db.session.rollback()
        raise NotFollowed('ChannelBin has not read that team from TheSportsDB, so it cannot have a channel.')
    if row is None:
        if enabled:
            db.session.add(SportsFeedChannel(league_id=league_id, team_tsdb_id=team_tsdb_id, number=0,
                                             enabled=True, created_at=datetime.utcnow()))
    else:
        row.enabled = enabled
    db.session.commit()
    return True


@retry_on_locked()
def set_league_channels(league_id: int, enabled: bool) -> Optional[bool]:
    """A person's switch for a league's numbered channels. Turning it on lays out every game the
    schedule already holds, in the same commit. Turning it off stops them being served and keeps
    both the rows and every game's channel, so turning it back on brings back the same layout."""
    league = db.session.get(SportsLeague, league_id)
    if league is None:
        return None
    whole, _teams = _follow_scope().get(league_id, (False, frozenset()))
    if enabled and not whole:
        db.session.rollback()
        raise NotFollowed('Follow the whole league first: league channels carry every game in it.')
    league.feed_league_channels = enabled
    if enabled:
        place_league_events(league, datetime.utcnow())
    db.session.commit()
    return True


def delete_for_league(league_id: int):
    """Inside sports_data.delete_league()'s unit: the league's sports channels go with it. Their
    ids are never issued again (AUTOINCREMENT), so an IPTV player's cached id cannot land on another
    channel."""
    SportsFeedChannel.query.filter_by(league_id=league_id).delete(synchronize_session=False)


# ---------------------------------------------------------------------------
# The league page's card
# ---------------------------------------------------------------------------

def card_context(league: SportsLeague, follows: list, team_names: dict, events: list, cfg: dict = None) -> dict:
    """What the league page's IPTV output card shows: the league's switch when the league is
    followed whole, and one switch per followed team. One query, for the league's own rows."""
    from . import output_feed
    whole = any(f.team_tsdb_id == 0 for f in follows)
    rows = SportsFeedChannel.query.filter_by(league_id=league.id).all()
    on = {r.team_tsdb_id for r in rows if r.team_tsdb_id and r.enabled}
    numbered = sum(1 for r in rows if not r.team_tsdb_id)
    if whole:
        names = {}
        for e in events:
            if e.home_team_id:
                names[e.home_team_id] = e.home_name
            if e.away_team_id:
                names[e.away_team_id] = e.away_name
        team_ids = set(names) | on
    else:
        names = {}
        team_ids = {f.team_tsdb_id for f in follows if f.team_tsdb_id}
    teams = sorted(({'id': i, 'name': team_names.get(i) or names.get(i) or f'Team {i}', 'on': i in on}
                    for i in team_ids), key=lambda t: t['name'].lower())
    return {'feed_on': output_feed.is_enabled(output_feed.feed_config(cfg)),
            'followed': bool(follows), 'whole': whole, 'league_on': bool(league.feed_league_channels),
            'numbered': numbered, 'teams': teams,
            'moved': moved_games(league, events) if whole and league.feed_league_channels else []}


def moved_games(league: SportsLeague, events: list) -> list:
    """One sentence per coming game of `events` that place_league_events() placed again after a
    collision, soonest first: which game, the channel it left and the one it is on, and the game
    it had come to share a time with. Pure: `events` is the league page's own list."""
    from .sports_data import event_game_name
    by_id = {e.id: e for e in events}
    out = []
    for e in sorted(events, key=lambda e: (e.start_at or datetime.max, e.id)):
        if e.feed_moved_at is None or e.feed_number is None or e.feed_moved_from is None:
            continue
        other = by_id.get(e.feed_moved_by)
        shared = f'the same time as {event_game_name(other)}' if other is not None else "another game's time"
        out.append(f'{event_game_name(e)} moved from {league.name} {e.feed_moved_from} to {league.name} '
                   f'{e.feed_number}: TheSportsDB changed a start time and it came to {shared} on one '
                   f'channel. An IPTV player that has not refreshed its guide may still list it on '
                   f'{league.name} {e.feed_moved_from}.')
    return out

