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
* It plays from a channel whose guide lists the game near that moment: a strong guide match
  still in the guide, not marked wrong, not a replay, on a channel that is not hidden, whose
  listing is on or within the output's sports window of it (sports_match.airing_now,
  dev/changelog/1529). Which of those listings and channels plays is auto_rules.game_sources()'s
  answer, the one Record and a rule read (dev/changelog/1637).
* A game no guide lists at all plays from an event channel whose name mentions it, exactly when
  Record would record it from one (auto_rules_sports.name_pick(), dev/changelog/1643): from the
  window's `before` ahead of the pick's start to the pick's end, which is where game_sources()
  calls it over. Any listing, even a weak one, means no name is read. Its guide lists it at the
  pick's times, and its play, its guide description and its note say it came from a name.
* A row of sports_feed_channels is an identity, and its id is the feed id an IPTV player caches. A
  switch going off or a follow ending stops the channel being served and keeps the row.
* A team's channel is served while its switch is on and the team is followed; a league's numbered
  channels while the league's switch is on and the league is followed whole. Both are read where
  the channel is served, so no path that changes a follow has to remember them.
* Start broadcast is a fact about a game, not a channel (dev/changelog/1530, named so since
  dev/changelog/1644): a person said it is on early, so every sports channel carrying it plays it
  ahead of its window until the end that was fixed when the button was pressed. Written by
  set_live_now() and nothing else; the code keeps the feature's first name, live now.
* A league's layout is stored, one game per numbered channel per day (dev/changelog/1684).
  SportsEvent.feed_number is written where the schedule is written and a game keeps it, with two
  exceptions: a game whose time has changed so that it lands on another's on the same channel is
  placed again, since the guide an IPTV player cached for it was already wrong about its time; and
  a game past the guide the output sends that does not fit its channel's day is placed again,
  since no player has cached it. The number of channels only grows.
"""
import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Optional

from sqlalchemy import and_, func, or_

from . import db, sports_match, sports_words
from .database import Channel, SportsEvent, SportsFeedChannel, SportsFollow, SportsLeague, SportsTeam
from .db_utils import retry_on_locked
from .fmt_utils import counted, length_words
from .sports_data import event_game_name, event_shape_is_matchup
from .sports_times import Rules, kickoff as times_kickoff

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
#: A game is placed on a numbered channel only when no other game there starts on the same day in
#: the display timezone and this much clears its window from every other one's, end to start
#: (dev/changelog/1684). A viewer tuning back to a game in overtime, or in early for the next one,
#: gets the game they asked for; the gap is for a game just after midnight, after a late one.
DAY_GAP = timedelta(hours=3)
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
    #: The game, and its listings a play may take [(channel id, account id, start, stop, guide start)], for a
    #: page that says what Start broadcast would do without asking again.
    event: Optional[SportsEvent] = field(default=None, repr=False)
    listings: tuple = field(default=(), repr=False)
    #: For a game no guide lists, the event channels it plays from (auto_rules_sports.NameSource);
    #: None when it has a listing or nothing names it.
    named: object = field(default=None, repr=False)

    @property
    def playable(self) -> bool:
        """A tune-in has something to take the game from: a guide lists it, or a channel's name
        gives it (dev/changelog/1643). Whether the account allows it is the description's to say."""
        return bool(self.listed) or self.named is not None


#: Where a play's moment falls against the listing it plays from (playing_now). Every value is
#: named wherever a phase is read.
PHASE_ON = 'on'          # the listing is on now
PHASE_AFTER = 'after'    # the listing ended no more than the window's `after` ago
PHASE_BEFORE = 'before'  # the listing starts no more than the window's `before` from now
PHASE_LIVE_NOW = 'live_now'  # a person started the broadcast early with Start broadcast (set_live_now)


@dataclass
class NowPlaying:
    """What a sports channel has on at one moment: the game and the channels whose guide lists it
    within the window, or `why_none`, a clause that finishes "Nothing to play for <channel>: ...".
    `note` says where now falls against the listings played from when that is not "on now".
    `listings` names those listings, (channel id, the listing's start in the guide), for
    auto_rules.game_sources() to pick the channel from (dev/changelog/1637). A game no guide lists
    that an event channel's name gives has no listings and its `named` instead (a NameSource);
    `channels` are then the channels naming it (dev/changelog/1643)."""
    event: Optional[SportsEvent] = field(default=None, repr=False)
    channels: list = field(default_factory=list, repr=False)
    listings: frozenset = frozenset()
    why_none: str = ''
    note: str = ''
    named: object = field(default=None, repr=False)
    # The same game's listings that ended inside the window, when `listings` are the ones on now
    # and so the only ones played from: a play whose every channel listing it now cannot play
    # takes one of these next, since its game may be running long (dev/changelog/1745).
    ended: frozenset = frozenset()


# ---------------------------------------------------------------------------
# Which channels are served
# ---------------------------------------------------------------------------

def follow_scope() -> dict:
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
    scope = follow_scope()
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

def _anchor(event) -> Optional[datetime]:
    """The start a game's own listings are told apart by: the guide's when the matcher has found
    the game there (SportsEvent.guide_start), else TheSportsDB's. None for a postponed game, and
    for a field event still at TheSportsDB's 00:00 placeholder (DESIGN-sports-data.md 7)."""
    if event.postponed:
        return None
    start = event.guide_start or event.start_at
    if start is None:
        return None
    if not event_shape_is_matchup(event) and event.guide_start is None and (start.hour, start.minute) == (0, 0):
        return None
    return start


def event_window(event, listings=(), named=None) -> Optional[tuple]:
    """(start, stop) of a game on a sports channel, or None when it has no time to show. `listings`
    are listing_windows() rows [(channel id, account id, start, stop, guide start)], each at the
    times its league's answer gives it. The start is where the answer puts the game's own listing
    the guide starts nearest the game (_anchor), and the listings it counts are told apart by the
    guide's start, so a block, its count, Start broadcast and the play read a listing the answer
    moved the same way (dev/docs/BUGS.md 2026-11-25 @ 10:54:34 PM); with no listing it is the
    anchor itself. The stop is the latest end among the game's own listings (with_the_game), else a
    length by the event's shape. A postponed game has no time, and neither does a field event still
    at TheSportsDB's 00:00 placeholder. A game no guide lists that an event channel's name gives
    (`named`, a NameSource) is at its pick's start and end, the times Record would record it at
    (dev/changelog/1643)."""
    if event.postponed:
        return None
    if named is not None and not listings:
        begin, end = named.pick.start, named.pick.stop
        return begin, min(max(end, begin + MIN_LENGTH), begin + MAX_LENGTH)
    anchor = _anchor(event)
    if anchor is None:
        return None
    mine = [row for row in with_the_game(listings, event) if row[2] is not None]
    start = anchor
    if mine:
        nearest = min(abs(row[4] - anchor) for row in mine)
        start = min(row[2] for row in mine if abs(row[4] - anchor) == nearest)
    ends = [row[3] for row in mine if row[3] is not None]
    stop = max(ends) if ends else start + (MATCHUP_LENGTH if event_shape_is_matchup(event) else FIELD_LENGTH)
    return start, min(max(stop, start + MIN_LENGTH), start + MAX_LENGTH)


def with_the_game(listings, event) -> list:
    """The listings that start with the game, which is what "a channel lists it" means for its end
    time and for how many channels carry it: the guide starts them within START_AGREE of the
    game's anchor (_anchor). One further off is a pregame show or a later airing. Told by the
    guide's start (row[4]), never the times the league's answer moved it to, which can be hours
    from the anchor."""
    anchor = _anchor(event)
    if anchor is None:
        return []
    return [row for row in listings if row[4] is not None and abs(row[4] - anchor) <= START_AGREE]


def placement_window(event, named=None) -> Optional[tuple]:
    """(start, stop) of a game for laying a league out. TheSportsDB's own start and a length by
    shape, so it moves only when TheSportsDB moves the game: the guide's start shifts by minutes as
    listings come and go, and a layout must not. A field event at the 00:00 placeholder has only
    the guide's start to go on, or, when no guide lists it, the start an event channel's name gives
    (`named`, a NameSource, dev/changelog/1643), so a race only a name gives has a channel too."""
    start = event.start_at
    matchup = event_shape_is_matchup(event)
    if start is not None and not matchup and (start.hour, start.minute) == (0, 0):
        start = event.guide_start or (named.pick.start if named is not None else None)
    if start is None or event.postponed:
        return None
    return start, start + (MATCHUP_LENGTH if matchup else FIELD_LENGTH)


def game_windows(events, now: datetime) -> dict:
    """{event id: (start, stop) or None}: each game's time on a sports channel (event_window),
    one only a channel's name gives at that name's times. At most one read of the names."""
    return _timed(events, now)


def followed_coming(now: datetime, after: timedelta, limit: int) -> list:
    """The games of everything followed that are not long over, soonest first, at most `limit`:
    a followed league's all, a followed team's own. Postponed games are left out, having no time.
    Two queries, one with nothing followed."""
    scope = follow_scope()
    if not scope:
        return []
    events = (SportsEvent.query.filter(SportsEvent.league_id.in_(list(scope)), SportsEvent.postponed.is_(False),
                                       _since(now - MAX_LENGTH - after))
              .order_by(event_start(), SportsEvent.id).limit(limit).all())
    return [e for e in events
            if scope[e.league_id][0] or {e.home_team_id, e.away_team_id} & scope[e.league_id][1]]


def event_start():
    return func.coalesce(SportsEvent.guide_start, SportsEvent.start_at)


def _since(lo: datetime):
    """Games whose start is after `lo`, and the games no guide has given a start that TheSportsDB
    has up to a day before it: a field event at the 00:00 placeholder whose real time is a
    channel's name's, later that day (dev/changelog/1643). Their windows decide the rest."""
    return or_(event_start() > lo,
               and_(SportsEvent.guide_start.is_(None), SportsEvent.start_at > lo - timedelta(days=1)))


def _timed(events, now: datetime) -> dict:
    """{event id: event_window()} for `events`, each over its listings, so a page's time for a game
    is its sports channel's block; a game with no time of its own (a field event at TheSportsDB's
    00:00 placeholder that no guide lists) at the times an event channel's name gives it. The
    listings are two queries; the names are read only when such a game is there: one
    name_sources() load then, none otherwise."""
    listings = sports_match.listing_windows([e.id for e in events], now) if events else {}
    out = {e.id: event_window(e, listings.get(e.id, ())) for e in events}
    untimed = [e for e in events if out[e.id] is None and not e.postponed and e.guide_start is None]
    if untimed:
        names = name_sources({e.league_id for e in untimed}, now)
        for e in untimed:
            out[e.id] = event_window(e, named=names.get(e.id))
    return out


def name_sources(league_ids, now: datetime) -> dict:
    """{event id: NameSource} for the games of `league_ids` no guide lists that an event channel's
    name gives: auto_rules_sports.name_sources(), read over the same days and at the same `now` as
    auto_rules.game_sources() reads them, since a pick's length is the longest listing among the
    games read. A fixed handful of queries, none with no league."""
    from .auto_rules import PICK_LOOKBACK
    from .auto_rules_sports import name_sources as read
    league_ids = sorted({i for i in league_ids if i})
    return read(league_ids, now, lookback=PICK_LOOKBACK) if league_ids else {}


def _usable_names(named, blocked, allowed) -> list:
    """The channels naming a game that are on an account a play may use."""
    return [n for n in named.names if n.account_id not in blocked and (allowed is None or n.account_id in allowed)]


@dataclass
class OneGame:
    """One game and nothing else, where playing_now() is asked about it rather than about a
    sports channel: a picture-in-picture channel's picture (app/pip_channels.py)."""
    league: SportsLeague = field(repr=False)
    event_id: int


def _events_of(served):
    if isinstance(served, OneGame):
        return SportsEvent.query.filter(SportsEvent.id == served.event_id)
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
    start order, which is what its waiting screen draws (app/sports_screen.py). Three queries
    (the games and their listings), and the names (_timed) when a game has no time of its own."""
    if not served:
        return {}
    numbered, teams = _carrier_keys(served)
    start = event_start()
    events = (SportsEvent.query.filter(SportsEvent.league_id.in_({s.league.id for s in served}),
                                       _since(now - MAX_LENGTH), SportsEvent.postponed.is_(False))
              .order_by(start, SportsEvent.id).all())
    windows = _timed(events, now)
    out = {}
    for event in events:
        window = windows[event.id]
        if window is None or window[1] <= now:
            continue
        for s in _carriers_of(event, numbered, teams):
            games = out.setdefault(s.row.id, [])
            if len(games) < SCREEN_GAMES:
                games.append(event)
    return out


def screen_team_ids(now: datetime) -> set:
    """The TheSportsDB ids of the teams in the games a served channel's waiting screen would draw
    now (screen_events()). What a badge is downloaded for (app/sports_images.py). A fixed number
    of queries whatever is stored, one with no sports channel."""
    return {i for games in screen_events(served_channels(), now).values()
            for event in games for i in (event.home_team_id, event.away_team_id) if i}


# ---------------------------------------------------------------------------
# The guide
# ---------------------------------------------------------------------------

def _description(event, channels: int, unusable: int, named=None) -> str:
    """A game's guide description. Says where the stream comes from, and says so when there is
    none: an IPTV player shows this before anyone presses play. `channels` list the game and can be
    played from; `unusable` list it on an account the feed cannot use right now. With `named` (a
    NameSource) the two count the channels whose names mention a game no guide lists."""
    from .auto_rules_sports import START_FROM_NAME, START_FROM_TSDB
    bits = []
    where = ', '.join(x for x in (event.venue, event.city) if x)
    if where:
        bits.append(f'{where}.')
    if named is not None:
        if channels:
            bits.append("No guide lists this, so ChannelBin plays it from "
                        + ('the channel whose name mentions it.' if channels == 1
                           else f'one of the {counted(channels, "channel")} whose names mention it.'))
            if named.pick.source == START_FROM_TSDB:
                bits.append("Its time is TheSportsDB's: the channel's name gives no time zone.")
            elif named.pick.source != START_FROM_NAME:
                raise ValueError(f'unknown event start source {named.pick.source!r}')
        else:
            bits.append(f'No stream it can play: no guide lists this, and the {counted(unusable, "channel")} '
                        f"whose {'name mentions' if unusable == 1 else 'names mention'} it "
                        f"{'is' if unusable == 1 else 'are'} on an account that is blocked or that the IPTV "
                        'output is not allowed to use.')
    elif channels:
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


def guide_programs(served: list, now: datetime, days: int, blocked=frozenset(), allowed=None,
                   asked_at: datetime = None) -> list:
    """Every game in the served channels' guides from `now` for `days`, as Programs ordered by
    channel then start. A fixed number of queries whatever is served: the leagues' events in the
    window, the listings matched to them, and the games only an event channel's name gives
    (name_sources()). A game ends where the next one on its channel starts. `blocked` and
    `allowed` are the account ids a play drops (output_feed._resolve_sports_play), so the description
    counts the channels listing the game on an account a play may use. Which of them it plays is
    auto_rules.game_sources()'s answer at the play, which can also pass over a listing or a group
    member this count includes (dev/changelog/1637); asking it per game here would be a search per
    game per guide. `asked_at` is the moment the games only a channel's name gives are read at,
    when `now` is moved back to keep games just over (output_feed_view): a pick that waits on a
    league's wait is the play's answer at the real moment, never at a moved one."""
    if not served:
        return []
    numbered, teams = _carrier_keys(served)
    start = event_start()
    league_ids = {s.league.id for s in served}
    events = (SportsEvent.query.filter(SportsEvent.league_id.in_(league_ids),
                                       _since(now - MAX_LENGTH), start < now + timedelta(days=days))
              .order_by(start, SportsEvent.id).all())
    listings = sports_match.listing_windows([e.id for e in events], now)
    names = name_sources(league_ids, asked_at or now) if events else {}
    by_row = {}
    for event in events:
        rows = _carriers_of(event, numbered, teams)
        if not rows:
            continue
        mine = listings.get(event.id, ())
        named = names.get(event.id) if not mine else None
        window = event_window(event, mine, named)
        if window is None or window[1] <= now:
            continue
        if named is not None:
            usable = {n.channel_id for n in _usable_names(named, blocked, allowed)}
            naming = {n.channel_id for n in named.names}
            description = _description(event, len(usable), len(naming - usable), named)
            listed = set()
        else:
            carrying = with_the_game(mine, event)
            usable = {row[0] for row in carrying if row[1] not in blocked and (allowed is None or row[1] in allowed)}
            listed = {row[0] for row in carrying}
            description = _description(event, len(usable), len(listed - usable))
        for s in rows:
            by_row.setdefault(s.row.id, []).append(Program(
                s.row.id, window[0], window[1], event_game_name(event), description,
                s.league.sport or 'Sports', len(listed), event, tuple(mine), named))
    out = []
    for s in served:
        programs = sorted(by_row.get(s.row.id, ()), key=lambda p: p.start)
        for program, following in zip(programs, programs[1:] + [None]):
            if following is not None and following.start < program.stop:
                program.stop = following.start
            if program.stop > program.start:
                out.append(program)
    return out


def numbered_with_games(served: list, now: datetime, days: int, after: timedelta = timedelta(0)) -> set:
    """The row ids of `served`'s numbered channels that have a game in the guide the output sends:
    one not over by `after` (the output's sports window, so a game running long keeps its channel)
    that starts within `days`. A numbered channel with none is left out of the output's channel list
    (dev/changelog/1621); its row, and so its feed id, stays. Three queries (the games and their
    listings), none when no numbered channel is served, and the names (_timed) when a game has no
    time of its own."""
    numbered = {(s.league.id, s.row.number): s.row.id for s in served if not s.is_team}
    if not numbered:
        return set()
    start = event_start()
    rows = (SportsEvent.query.filter(SportsEvent.league_id.in_({league for league, _n in numbered}),
                                     SportsEvent.feed_number.isnot(None),
                                     _since(now - MAX_LENGTH - after), start < now + timedelta(days=days))
            .all())
    rows = [e for e in rows if (e.league_id, e.feed_number) in numbered]
    windows = _timed(rows, now)
    out = set()
    for event in rows:
        row_id = numbered[(event.league_id, event.feed_number)]
        window = windows[event.id]
        if window is not None and window[1] > now - after:
            out.add(row_id)
    return out


#: The mark at the start of a guide block that is not a game but says when the next one is
#: (dev/changelog/1621): an IPTV player's guide shows it where it would show "No information".
NEXT_MARK = '\N{CLOCK FACE THREE OCLOCK}'
#: The guide blocks between games start on the clock's even hours, as an Up next channel's do.
FILLER_BLOCK = timedelta(hours=2)


@dataclass
class Filler:
    """A guide block on a sports channel between games, naming the next one."""
    row_id: int
    start: datetime
    stop: datetime
    title: str
    description: str


def _next_after(served: list, when: datetime) -> dict:
    """{row id: SportsEvent}: each served channel's first game starting at or after `when`, past the
    guide's end. One query over what is stored (the schedule reaches 21 days ahead)."""
    if not served:
        return {}
    numbered, teams = _carrier_keys(served)
    start = event_start()
    out = {}
    for event in (SportsEvent.query.filter(SportsEvent.league_id.in_({s.league.id for s in served}),
                                           start >= when, SportsEvent.postponed.is_(False))
                  .order_by(start, SportsEvent.id).all()):
        if event_window(event) is None:
            continue
        for s in _carriers_of(event, numbered, teams):
            out.setdefault(s.row.id, event)
        if len(out) == len(served):
            break
    return out


def event_word(league, events=()) -> str:
    """The league's word for one of `events`, or for any of its events with none in hand
    (app/sports_words.py)."""
    events = [e for e in events if e is not None]
    return sports_words.noun(league.sport, any(event_shape_is_matchup(e) for e in events) if events else None)[0]


def next_game_words(event, start: datetime, now: datetime, tz, h24: bool) -> str:
    """'Eastern Michigan at Western Michigan, Fri 7:00 PM EST': the day by name within the coming
    week, with the date further off, always in the display timezone."""
    from .tz_utils import format_local
    style = 'weekday_time' if start - now < timedelta(days=6) else 'day_datetime'
    return f'{event_game_name(event)}, {format_local(start, style, tz=tz, h24=h24)}'


def guide_fillers(served: list, programs: list, now: datetime, days: int, tz, h24: bool) -> list:
    """The blocks that fill each served channel's guide between its games, from the even hour
    before `now` to `days` ahead, each titled with the next game and when it starts, or that none
    is scheduled. `programs` is guide_programs() for the same channels and window. One query, for
    the games past the guide's end. Never a game: no category, and the mark in front."""
    if not served:
        return []
    end = now + timedelta(days=days)
    origin = now.replace(minute=0, second=0, microsecond=0)
    origin -= timedelta(hours=origin.hour % 2)
    by_row = {}
    for program in programs:
        by_row.setdefault(program.row_id, []).append(program)
    beyond = _next_after(served, end)
    rules = Rules()
    out = []
    for s in served:
        cursor = origin
        gaps = []
        for program in by_row.get(s.row.id, ()):
            if program.start > cursor:
                gaps.append((cursor, program.start, program.event, program.start, program.named))
            cursor = max(cursor, program.stop)
        later = beyond.get(s.row.id)
        later_start = event_window(later)[0] if later is not None else None
        if cursor < end:
            gaps.append((cursor, end, later, later_start, None))
        for lo, hi, event, start, named in gaps:
            if event is not None:
                # The kickoff, by the league's answer to which time a game goes by: the game's
                # block may start earlier, with the earliest channel's coverage. A game only a
                # channel's name gives starts where its pick does, as its own block does.
                kickoff = (named.pick.start if named is not None
                           else times_kickoff(event, rules.of(s.league.tsdb_id), now, listed_fallback=start))
                words = next_game_words(event, kickoff, now, tz, h24)
                title = f'{NEXT_MARK} Next: {words}'
                source = ('a channel whose name mentions it, since no guide lists it' if named is not None
                          else 'a channel whose guide lists it')
                desc = (f'No {event_word(s.league, [event])} on {s.name} until {words}. ChannelBin plays it from {source}. '
                        'Schedule from TheSportsDB.')
            else:
                title = f'{NEXT_MARK} No {event_word(s.league)} scheduled'
                desc = f'TheSportsDB lists no coming {event_word(s.league)} for {s.name} yet.'
            block = lo
            while block < hi:
                stop = min(hi, block - timedelta(hours=block.hour % 2, minutes=block.minute,
                                                 seconds=block.second, microseconds=block.microsecond)
                           + FILLER_BLOCK)
                out.append(Filler(s.row.id, block, stop, title, desc))
                block = stop
    return out


# ---------------------------------------------------------------------------
# What a channel plays now
# ---------------------------------------------------------------------------

def _when(start: datetime, now: datetime) -> str:
    from .tz_utils import day_label, format_local, get_display_tz, is_24h
    tz = get_display_tz()
    return f'{day_label(start, now, tz)} {format_local(start, "clock", tz=tz, h24=is_24h())}'


def _pick(by_id: dict, listings: list, now: datetime) -> Optional[tuple]:
    """(event, {(channel id, guide start)}, phase, listing start, listing stop) for the game a play
    takes from `listings` [(event id, channel id, start, stop, guide start)], or None when there
    are none. A listing on now
    wins; else the one that ended last, since its game may be running long; else the one that
    starts soonest. So a game's tail holds the channel until the next game's listing starts."""
    on, ended, coming = {}, {}, {}
    for found, channel_id, begin, stop, listed_at in listings:
        if found not in by_id:
            continue
        bucket = on if begin <= now < stop else ended if stop <= now else coming
        channels, lo, hi = bucket.get(found, (set(), begin, stop))
        channels.add((channel_id, listed_at))
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
        return f'its listing ended {_when(stop, now)}; it may be running long'
    if phase == PHASE_BEFORE:
        return f'its listing starts {_when(begin, now)}'
    if phase == PHASE_LIVE_NOW:
        return f'broadcasting early; its listing starts {_when(begin, now)}'
    raise ValueError(f'Unknown play phase "{phase}"')


#: What a play's note says of a game it takes from a channel's name: the play's own label is kept
#: for a proxy session's whole life, so where the stream came from is said here as well.
FROM_NAME_NOTE = 'no guide lists it, so it plays from a channel whose name mentions it'


def _name_note(phase: str, start: datetime, now: datetime) -> str:
    if phase == PHASE_ON:
        return FROM_NAME_NOTE
    if phase == PHASE_BEFORE:
        return f'{FROM_NAME_NOTE}; it starts {_when(start, now)}'
    if phase == PHASE_LIVE_NOW:
        return f'broadcasting early; {FROM_NAME_NOTE}; it starts {_when(start, now)}'
    raise ValueError(f'Unknown play phase for a game taken from a name "{phase}"')


def _name_play(by_id: dict, names: dict, now: datetime, before: timedelta, opened: bool = False) -> Optional[tuple]:
    """(event, NameSource, phase) for the game of `by_id` a play takes from an event channel's name,
    or None. The pick's own window counts, from `before` ahead of its start to its end; a game
    broadcast early with Start broadcast counts from now to the same end. One that has started wins, the latest
    first, as with listings (_pick); else the one that starts soonest."""
    on, coming = [], []
    for event_id, event in by_id.items():
        named = names.get(event_id)
        window = event_window(event, named=named) if named is not None else None
        if window is None or now >= window[1]:
            continue
        if named.pick.start <= now:
            on.append((event, named))
        elif opened or named.pick.start - before <= now:
            coming.append((event, named))
    if on:
        event, named = max(on, key=lambda p: (p[1].pick.start, p[0].id))
        return event, named, PHASE_ON
    if coming:
        event, named = min(coming, key=lambda p: (p[1].pick.start, p[0].id))
        return event, named, PHASE_LIVE_NOW if opened else PHASE_BEFORE
    return None


def _name_first(from_name: tuple, picked) -> bool:
    """Whether a game taken from a name goes ahead of `picked`, the listing _pick() found (one not
    on now), by _pick()'s own order: a game on now beats another's tail or pregame, a game running
    long holds the channel until the next one starts, and of two still to start the sooner wins.
    So a game on the air is never cut for another's schedule."""
    if picked is None:
        return True
    _event, named, phase = from_name
    if phase == PHASE_ON:
        return True
    if phase != PHASE_BEFORE:
        raise ValueError(f'Unknown play phase for a game taken from a name "{phase}"')
    if picked[2] == PHASE_AFTER:
        return False
    if picked[2] == PHASE_BEFORE:
        return named.pick.start < picked[3]
    raise ValueError(f'Unknown play phase "{picked[2]}"')


def _named_now(found: tuple, now: datetime) -> Optional[NowPlaying]:
    event, named, phase = found
    channels = Channel.query.filter(Channel.id.in_({n.channel_id for n in named.names})).all()
    if not channels:
        return None
    return NowPlaying(event=event, channels=channels, named=named, note=_name_note(phase, named.pick.start, now))


def playing_now(served: Served, now: datetime, event_id: int = None, before: timedelta = timedelta(0),
                after: timedelta = timedelta(0), held: bool = False) -> NowPlaying:
    """The game `served` has on at `now` and the channels whose guide lists it, from `before` ahead
    of a listing's start to `after` past its end (integrations.output_feed's sports window,
    dev/changelog/1529): a viewer tuning in for the pregame or to a game running long expects
    something on. Zero for both is the listings on at this moment. `event_id` holds the answer to
    that one game, which is how a failover stays on the game it started on. `held` says a play is
    already on that game (a failover, a seat search, a move by hand) rather than asking whether a
    tune-in would get it, and a held play is never ended by the clock: its game's listings count
    up to MAX_LENGTH and `after` past their end, the reach a play already has back to a game's
    start, so a long overtime plus a provider blip moves the play rather than cutting it
    (dev/changelog/1685). A game broadcast early
    with Start broadcast plays from its listings that start with the game, whenever they start, until its
    stored end (set_live_now). A game no guide lists that an event channel's name gives plays from
    the channels naming it (_name_play, dev/changelog/1643), only when no listing has a game to
    play: a listing always comes first, as it does for Record.
    With nothing to play, `why_none` says which of four things is true: no game is near, a game is
    near that nothing lists, the game this play was held to is over, or a game opened early has no
    listing any more."""
    if held and event_id is None:
        raise ValueError('A held play needs the game it is held to')
    start = event_start()
    reach_back = MAX_LENGTH + after
    q = _events_of(served).filter(_since(now - reach_back))
    if event_id is not None:
        q = q.filter(SportsEvent.id == event_id)
    # The tail a listing's end is measured against: the configured one for a tune-in, the whole
    # reach for a play already on its game.
    tail = reach_back if held else after
    events = q.order_by(start, SportsEvent.id).limit(_PLAY_EVENTS).all()
    by_id = {e.id: e for e in events}
    opened = {i: e for i, e in by_id.items() if live_now_on(e, now)}
    names = None

    def named():
        # Read once, and only on a path that has found no listing to play.
        nonlocal names
        if names is None:
            names = name_sources([served.league.id], now) if events else {}
        return names

    picked = None
    if opened:
        # A game a person opened early wins: they said it is on. Its listings count up to the end
        # fixed when the button was pressed, and no further.
        reach = max(e.live_now_until for e in opened.values()) - now
        rows = [row for row in sports_match.airing_now(list(opened), now, reach, tail, listed=True)
                if _starts_with_game(opened[row[0]], row[4])]
        picked = _pick(opened, rows, now)
        if picked is not None and picked[2] == PHASE_BEFORE:
            picked = picked[:2] + (PHASE_LIVE_NOW,) + picked[3:]
        if picked is None:
            from_name = _name_play(opened, named(), now, before, opened=True)
            found = _named_now(from_name, now) if from_name is not None else None
            if found is not None:
                return found
    if picked is None:
        rows = sports_match.airing_now(list(by_id), now, before, tail, listed=True)
        picked = _pick(by_id, rows, now)
        if picked is None or picked[2] != PHASE_ON:
            from_name = _name_play(by_id, named(), now, before)
            found = (_named_now(from_name, now)
                     if from_name is not None and _name_first(from_name, picked) else None)
            if found is not None:
                return found
    if picked is not None:
        event, listings, phase, begin, stop = picked
        channels = Channel.query.filter(Channel.id.in_({c for c, _start in listings})).all()
        ended = (frozenset((c, listed_at) for found_id, c, _begin, end, listed_at in rows
                           if found_id == event.id and end <= now)
                 if phase == PHASE_ON else frozenset())
        return NowPlaying(event=event, channels=channels, listings=frozenset(listings),
                          note=_phase_note(phase, begin, stop, now), ended=ended)
    if opened:
        name = event_game_name(min(opened.values(), key=lambda e: (e.live_now_until, e.id)))
        return NowPlaying(why_none=(f"{name} was started early with Start broadcast, but no channel's guide lists it any more, "
                                    f'and it is not one Record would take from a channel\'s name either'))
    if event_id is not None:
        name = event_game_name(events[0]) if events else f'the {event_word(served.league)}'
        return NowPlaying(why_none=(f"no channel's guide lists {name} any more, and it is not one Record would "
                                    "take from a channel's name either"))
    # Every listing, not only those in reach: a game's block may start hours from where its listing
    # sits in the guide, and the refusal names the time the block does.
    listings = sports_match.listing_windows([e.id for e in events], now) if events else {}
    windows = [(e, event_window(e, listings.get(e.id, ()), named().get(e.id) if not listings.get(e.id) else None))
               for e in events]
    for event, window in windows:
        if window is not None and window[0] - before <= now < window[1]:
            when = '' if window[0] <= now else f', {_when(window[0], now)}'
            return NowPlaying(event=event, why_none=(
                f"no stream is found yet for {event_game_name(event)}{when}: none of your channels' "
                f'guides lists it right now'))
    for event, window in windows:
        if window is not None and window[0] > now:
            return NowPlaying(why_none=f'no {event_word(served.league, [event])} is on right now. Next: {event_game_name(event)}, '
                                       f'{_when(window[0], now)}'
                                       f'{_plays_from(event, window[0], before, now, named().get(event.id))}')
    return NowPlaying(why_none=f'no {event_word(served.league, events)} is on right now, and none is in the schedule')


def _plays_from(event, shown: datetime, before: timedelta, now: datetime, named=None) -> str:
    """When the channel starts playing `event`, as a clause after its time: from `before` ahead of
    its earliest listing, or of its pick's start for a game only a channel's name gives (`named`),
    said only when that is not the time already shown. One query, on the refusal path only."""
    if named is not None:
        starts = [named.pick.start] if named.pick.start > now else []
    else:
        starts = [row[2] for row in sports_match.listing_windows([event.id], now).get(event.id, ())
                  if row[2] is not None and row[2] > now]
    if not starts:
        return ''
    opens = min(starts) - before
    return '' if opens == shown else f'. This channel plays it from {_when(opens, now)}'


# ---------------------------------------------------------------------------
# Start broadcast (Go live now until dev/changelog/1644)
# ---------------------------------------------------------------------------

class LiveNowRefused(Exception):
    """Start broadcast has nothing to put on air for a game. The message is user-facing."""


def live_now_on(event, now: datetime) -> bool:
    return event.live_now_until is not None and now < event.live_now_until


def _starts_with_game(event, listed_at) -> bool:
    """Whether a listing the guide starts at `listed_at` is the game itself, the rule with_the_game()
    applies for the guide's own count, so a page's "listed" and what Start broadcast plays never
    disagree."""
    anchor = _anchor(event)
    return anchor is not None and listed_at is not None and abs(listed_at - anchor) <= START_AGREE


def _game_listings(event, listings) -> list:
    return [row for row in listings if row[3] is not None and _starts_with_game(event, row[4])]


def live_now_end(event, listings, now: datetime, after: timedelta, named=None) -> Optional[datetime]:
    """When Start broadcast for `event` turns itself off: `after` past the end of its listings that
    start with the game and are still to end. That is where the game's own window ends, so the
    switch cannot outlive the game. None when no such listing is left: there is nothing to play.
    `listings` are [(channel id, account id, start, stop, guide start)], already narrowed to usable accounts.
    A game no guide lists that a channel's name gives (`named`, a NameSource with a usable channel)
    turns it off at its pick's end, where its play ends (dev/changelog/1643)."""
    if named is not None and not listings:
        window = event_window(event, named=named)
        return window[1] if window is not None and window[1] > now else None
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


def _live_now_problem(event, listings, usable: list, channels: list, now: datetime, named=None,
                      named_usable=()) -> str:
    """Why Start broadcast has nothing to put on air for `event`, or ''. Every case is named: a game over
    never reads as one not listed yet. `named` is the NameSource of a game no guide lists, and
    `named_usable` its channels on an account a play may use."""
    game = event_game_name(event)
    if event.postponed:
        return f'{game} is postponed, so there is no time to broadcast it for.'
    mine = _game_listings(event, listings)
    if not mine and named is not None:
        if named.pick.stop <= now:
            return (f'{game} is over by the times the channel whose name mentions it gives, so there is '
                    'nothing left to broadcast.')
        if not named_usable:
            return (f'Every channel whose name mentions {game} is on an account that is blocked right now '
                    'or that the IPTV output does not play from.')
        if not channels:
            return (f'No sports channel on your IPTV output carries {game}. Switch one on for it on its '
                    "league's page first.")
        return ''
    if not mine:
        return (f"No channel's guide lists {game} yet, and it is not one Record would take from a channel's "
                "name either, so there is nothing to play. The league page's Record says why. ChannelBin "
                'looks again after each guide update.')
    if not any(row[3] > now for row in mine):
        return f"Every listing of {game} has ended, so there is nothing left to broadcast."
    if not any(row[3] > now for row in _game_listings(event, usable)):
        return (f'Every channel whose guide lists {game} is on an account that is blocked right now or '
                'that the IPTV output does not play from.')
    if not channels:
        return (f'No sports channel on your IPTV output carries {game}. Switch one on for it on its '
                "league's page first.")
    return ''


def _live_now_named(listings, named, blocked, allowed) -> tuple:
    """(the NameSource Start broadcast plays from, its usable channels): the game's `named` when no
    listing is there to play instead, else (None, [])."""
    if named is None or listings:
        return None, []
    return named, _usable_names(named, blocked, allowed)


def _on_air_at(event, usable: list, now: datetime, before: timedelta, named=None) -> Optional[datetime]:
    """When a sports channel puts `event` on air by itself: the window's `before` ahead of the
    first of its usable listings that start with the game, or of the name pick's start for a game
    no guide lists (`named`, with a usable channel). None when it has neither."""
    if named is not None:
        return named.pick.start - before
    starts = [row[2] for row in _game_listings(event, usable)]
    return min(starts) - before if starts else None


def broadcast_tip(game: str, before: timedelta, on: bool) -> str:
    """Start broadcast's and Stop broadcast's tooltip, with the lead the play really uses."""
    when = f'{length_words(before)} before it starts' if before > timedelta(0) else 'when it starts'
    if on:
        return (f'Stop broadcast takes the sports channel back to its own schedule, so it goes on air for '
                f'{game} {when}.')
    return f'A sports channel goes on air for {game} by itself {when}. Start broadcast puts it on air now.'


def live_now_offer(event, listings, channels: list, now: datetime, before: timedelta, after: timedelta, tz,
                   h24: bool, blocked=frozenset(), allowed=None, named=None) -> Optional[dict]:
    """What a page shows for Start broadcast on one game, from what it has already read: the game's
    listings (listing_windows() rows), the names of the sports channels carrying it,
    the account ids a play drops (output_feed._resolve_sports_play), and for a game no guide lists
    the channels naming it (`named`, a NameSource, dev/changelog/1643). No query, so `usable` is the
    listings on an account a play may use, not the channel it picks (auto_rules.game_sources()).
    `why` is set when there is nothing to broadcast, `message` is the confirm's text and `tip` the
    button's tooltip. None once the game's own time on air has come (`before` ahead of its start,
    _on_air_at): the channel plays it by itself then, so Stop broadcast would stop nothing and
    Start broadcast start nothing (dev/changelog/1644)."""
    game = event_game_name(event)
    on = live_now_on(event, now)
    usable = _usable(listings, blocked, allowed)
    named, named_usable = _live_now_named(listings, named, blocked, allowed)
    why = '' if on else _live_now_problem(event, listings, usable, channels, now, named, named_usable)
    if not why:
        opens = _on_air_at(event, usable, now, before, named if named_usable else None)
        if opens is not None and opens <= now:
            return None
    until = event.live_now_until if on else live_now_end(event, usable, now, after,
                                                         named if named_usable else None)
    out = {'event': event.id, 'game': game, 'on': on, 'channels': list(channels),
           'until': at_clause(until, now, tz, h24) if until is not None else '',
           'why': why, 'message': '', 'tip': broadcast_tip(game, before, on)}
    if out['why'] or on:
        return out
    start = event.start_at
    if start is None or (not event_shape_is_matchup(event) and (start.hour, start.minute) == (0, 0)):
        scheduled = f'TheSportsDB has no start time for {game}'
    else:
        scheduled = f'{game} is scheduled to start {at_clause(start, now, tz, h24)}'
    if named is not None:
        scheduled += (', and no guide lists it, so it plays from a channel whose name mentions it'
                      + (f', from {at_clause(named.pick.start, now, tz, h24)}' if named.pick.start != start else ''))
    starts = [row[4] for row in _game_listings(event, usable) if row[3] > now]
    listed = min(starts) if starts else None
    if listed is not None and listed != start:
        scheduled += f', and your guide lists it from {at_clause(listed, now, tz, h24)}'
    where = 'the sports channel' if len(channels) == 1 else 'any of the sports channels'
    out['message'] = (f'{scheduled}. From now on, tuning in to {where} below plays it, and the broadcast '
                      f'stops by itself {out["until"]}. A play already running is not cut.')
    return out


@retry_on_locked()
def set_live_now(event_id: int, on: bool, after: timedelta, now: datetime = None, blocked=frozenset(),
                 allowed=None) -> Optional[dict]:
    """A person's Start broadcast or Stop broadcast for one game, the only writer of SportsEvent.live_now_at and
    live_now_until. Turning it on fixes its end with live_now_end(), so the switch dies with the
    game's own window whatever later happens to the game, and is refused (LiveNowRefused) when
    there is nothing to broadcast (_live_now_problem names which). `blocked` and `allowed` are the
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
    listings = sports_match.listing_windows([event.id], now).get(event.id, ())
    usable = _usable(listings, blocked, allowed)
    named, named_usable = _live_now_named(
        listings, name_sources([event.league_id], now).get(event.id) if not listings else None, blocked, allowed)
    until = live_now_end(event, usable, now, after, named if named_usable else None)
    channels = [s.name for s in carriers(served_channels(), [event]).get(event.id, ())]
    problem = _live_now_problem(event, listings, usable, channels, now, named, named_usable)
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

def _fits(window: tuple, day, others) -> bool:
    """A game at `window` whose start falls on `day` (the display timezone's date) may join
    `others` [(start, stop, day)] on one numbered channel: no other starts that day, and DAY_GAP
    clears its window from each other's in both directions."""
    return all(day != other_day and (window[0] >= stop + DAY_GAP or window[1] + DAY_GAP <= begin)
               for begin, stop, other_day in others)


def place_league_events(league: SportsLeague, now: datetime, horizon: timedelta, tz) -> int:
    """Give each game of `league` that has a time and no channel yet the lowest-numbered channel
    it fits (_fits: one game per channel per day, dev/changelog/1684), and make sure a row exists
    for every number in use. Returns how many were placed. Mutates without committing: the
    caller's unit is the schedule write, or the switch going on. `horizon` is how far past `now`
    the guide the output sends reaches (output_feed.guide_horizon()) and `tz` the display
    timezone, read once by the caller.

    What is left to place is read from feed_number being NULL, which is the placement's own record,
    so this is re-runnable from the top. A game already placed keeps its number, with two
    exceptions:

    * A game inside the guide whose time has changed onto another game's on the same number by
      more than COLLISION: the one whose time changed last gives its number up and is placed
      again, a WARNING names both, and the move is kept on the game (feed_moved_*) for the league
      page to say. Left alone, one of the two would have no channel to be tuned on. Measured on
      the bare windows: moving a game a player has cached costs more than a tight pair.
    * A game past the guide that does not fit its number beside the games kept there (another
      starts that day, or one the day before or after is within DAY_GAP), as a game laid out
      before one game per channel per day can: it is placed again with no feed_moved_* record,
      since no player's cached guide has listed it, and one INFO line counts them. A number's
      first game past the guide is not one of these: the guide's last block names it."""
    if not league.feed_league_channels:
        return 0
    from .tz_utils import to_local
    start = event_start()
    lo = now - MAX_LENGTH
    events = (SportsEvent.query.filter(SportsEvent.league_id == league.id, _since(lo))
              .order_by(start, SportsEvent.id).all())
    windows = {e.id: placement_window(e) for e in events}
    # The day before is read only for a game with no time of its own; a game TheSportsDB timed
    # that long ago is over and is never laid out late.
    events = [e for e in events if (e.guide_start or e.start_at) > lo or windows[e.id] is None]
    untimed = [e for e in events if windows[e.id] is None and not e.postponed and e.guide_start is None]
    if untimed:
        # A race no guide lists has only an event channel's name to go on (dev/changelog/1643).
        names = name_sources([league.id], now)
        for e in untimed:
            windows[e.id] = placement_window(e, names.get(e.id))
    # The guide's own test for a game in it (guide_programs), so a game it may have sent counts as
    # cached; so does each number's first game past it, which the guide's last block names
    # ("Next: ...", guide_fillers).
    guide_end = now + horizon
    cached = {e.id for e in events if (e.guide_start or e.start_at) < guide_end}
    named_next = {}
    for event in events:
        if event.id not in cached and event.feed_number is not None and windows[event.id] is not None:
            named_next.setdefault(event.feed_number, event.id)
    cached |= set(named_next.values())
    days = {e.id: to_local(w[0], tz).date() for e in events if (w := windows[e.id]) is not None}
    placed = {}
    for event in events:
        if event.feed_number is not None and windows[event.id] is not None and event.id in cached:
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
    taken = {}
    for event in events:
        window = windows[event.id]
        if window is not None and event.feed_number is not None and event.id in cached:
            taken.setdefault(event.feed_number, []).append((window[0], window[1], days[event.id]))
    relaid = 0
    for event in events:
        window = windows[event.id]
        if window is None or event.feed_number is None or event.id in cached:
            continue
        if _fits(window, days[event.id], taken.get(event.feed_number, ())):
            taken.setdefault(event.feed_number, []).append((window[0], window[1], days[event.id]))
        else:
            event.feed_number = None
            relaid += 1
    if relaid:
        log.info('Sports channels: %s past the guide placed again to keep one game per %s channel per day',
                 counted(relaid, 'game'), league.name)
    todo = [(e, windows[e.id]) for e in events if windows[e.id] is not None and e.feed_number is None]
    for event, window in todo:
        number = 1
        while not _fits(window, days[event.id], taken.get(number, ())):
            number += 1
        event.feed_number = number
        taken.setdefault(number, []).append((window[0], window[1], days[event.id]))
    have = {n for (n,) in db.session.query(SportsFeedChannel.number).filter(
        SportsFeedChannel.league_id == league.id, SportsFeedChannel.team_tsdb_id == 0).all()}
    for number in range(1, max(taken, default=0) + 1):
        if number not in have:
            db.session.add(SportsFeedChannel(league_id=league.id, team_tsdb_id=0, number=number,
                                             enabled=False, created_at=now))
    return len(todo)


@retry_on_locked()
def place_all(now: Optional[datetime], horizon: timedelta) -> int:
    """place_league_events() for every league whose switch is on, in one commit. The matcher calls
    it after a run, since a guide match is what gives a field event its time. `horizon` is
    output_feed.guide_horizon(), the caller's to read."""
    from .tz_utils import get_display_tz
    now = now or datetime.utcnow()
    tz = get_display_tz()
    placed = 0
    for league in SportsLeague.query.filter(SportsLeague.feed_league_channels.is_(True)).all():
        placed += place_league_events(league, now, horizon, tz)
    if placed:
        db.session.commit()
        log.info('Sports channels: placed %s on league channels', counted(placed, 'game'))
    else:
        db.session.rollback()
    return placed


def place_all_guarded(now: Optional[datetime], horizon: timedelta) -> int:
    """place_all() for a caller whose own work must not depend on it: the schedule worker and the
    matcher. A failure is logged with its traceback and the games stay unplaced, which the next
    run reads from feed_number and places."""
    try:
        placed = place_all(now, horizon)
    except Exception:  # noqa: BLE001 - a watch-only layout must never stop a schedule update or a match run
        db.session.rollback()
        log.exception('Sports channels: placing games on league channels failed; the next run tries again')
        return 0
    # A league's numbered channels are made here, with no request, so this is where its item on
    # the output's arrangement can first be recorded (DESIGN-output-feed.md 16.4).
    from .output_arrangement import reconcile_guarded
    reconcile_guarded('placing games on league channels')
    return placed


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
    whole, teams = follow_scope().get(league_id, (False, frozenset()))
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
def set_league_channels(league_id: int, enabled: bool, horizon: timedelta) -> Optional[bool]:
    """A person's switch for a league's numbered channels. Turning it on lays out every game the
    schedule already holds, in the same commit (`horizon` as place_league_events() takes it).
    Turning it off stops them being served and keeps both the rows and every game's channel, so
    turning it back on brings back the same layout."""
    league = db.session.get(SportsLeague, league_id)
    if league is None:
        return None
    whole, _teams = follow_scope().get(league_id, (False, frozenset()))
    if enabled and not whole:
        db.session.rollback()
        raise NotFollowed(f'Follow the whole league first: league channels carry every {event_word(league)} in it.')
    league.feed_league_channels = enabled
    if enabled:
        from .tz_utils import get_display_tz
        place_league_events(league, datetime.utcnow(), horizon, get_display_tz())
    db.session.commit()
    return True


def delete_for_league(league_id: int):
    """Inside sports_data.delete_league()'s unit: the league's sports channels and its Up next
    channel go with it. Their ids are never issued again (AUTOINCREMENT), so an IPTV player's cached
    id cannot land on another channel. Their places on the output's arrangement go too, and the
    league's own item, since a league's id is reused (dev/changelog/1797)."""
    from .database import SportsUpNextChannel
    from .output_arrangement import forget_items
    from .output_feed import feed_id_for_sports, feed_id_for_up_next
    from .sports_up_next import delete_for_league as delete_up_next
    feed_ids = [feed_id_for_sports(i) for (i,) in db.session.query(SportsFeedChannel.id)
                .filter(SportsFeedChannel.league_id == league_id)]
    feed_ids += [feed_id_for_up_next(i) for (i,) in db.session.query(SportsUpNextChannel.id)
                 .filter(SportsUpNextChannel.league_id == league_id)]
    forget_items(feed_ids=feed_ids, league_ids=[league_id])
    SportsFeedChannel.query.filter_by(league_id=league_id).delete(synchronize_session=False)
    delete_up_next(league_id)


# ---------------------------------------------------------------------------
# The league page's card
# ---------------------------------------------------------------------------

def card_context(league: SportsLeague, follows: list, team_names: dict, events: list, cfg: dict = None) -> dict:
    """What the league page's IPTV output card shows: the league's Up next channel's switch when
    anything in it is followed, the league's switch when the league is followed whole, and one
    switch per followed team. Two queries, for the league's own rows."""
    from . import output_feed
    from .database import SportsUpNextChannel
    from .sports_up_next import name_for
    whole = any(f.team_tsdb_id == 0 for f in follows)
    rows = SportsFeedChannel.query.filter_by(league_id=league.id).all()
    up_next = SportsUpNextChannel.query.filter_by(league_id=league.id).first()
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
    # Preview (dev/changelog/1645) plays a channel's feed id, so it needs the channel switched on: a
    # sports channel that is off has nothing to play, and a team never switched on has no row yet.
    team_rows = {r.team_tsdb_id: r.id for r in rows if r.team_tsdb_id and r.enabled}
    teams = sorted(({'id': i, 'name': team_names.get(i) or names.get(i) or f'Team {i}', 'on': i in on,
                     'feed_id': output_feed.feed_id_for_sports(team_rows[i]) if i in team_rows else None}
                    for i in team_ids), key=lambda t: t['name'].lower())
    numbered_feeds = ([{'name': f'{league.name} {r.number}', 'feed_id': output_feed.feed_id_for_sports(r.id)}
                       for r in sorted((r for r in rows if not r.team_tsdb_id), key=lambda r: r.number)]
                      if league.feed_league_channels else [])
    return {'feed_on': output_feed.is_enabled(output_feed.feed_config(cfg)),
            'followed': bool(follows), 'whole': whole, 'league_on': bool(league.feed_league_channels),
            'numbered': numbered, 'teams': teams, 'numbered_feeds': numbered_feeds,
            'up_next_feed_id': output_feed.feed_id_for_up_next(up_next.id) if up_next is not None else None,
            'up_next_on': bool(up_next is not None and up_next.enabled), 'up_next_name': name_for(league),
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
        shared = f'the same time as {event_game_name(other)}' if other is not None else f"another {event_word(league, [e])}'s time"
        out.append(f'{event_game_name(e)} moved from {league.name} {e.feed_moved_from} to {league.name} '
                   f'{e.feed_number}: TheSportsDB changed a start time and it came to {shared} on one '
                   f'channel. An IPTV player that has not refreshed its guide may still list it on '
                   f'{league.name} {e.feed_moved_from}.')
    return out

