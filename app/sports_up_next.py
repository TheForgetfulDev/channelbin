"""The Up next channels on the output feed (dev/changelog/1614).

An Up next channel is always ChannelBin's own picture: the coming games, a page every 10 s, each
page drawn as a sports channel's waiting screen is (dev/mockups/55's look 7: the next game big in
its two teams' colors and badges, the later ones as stripes), with where each game is on. There
is one for everything followed and one per league. app/output_feed.py serves them as its fourth
kind of feed channel; this module is which ones are served, their switch and what their pages
show. Their clips are made on tune-in, never ahead (dev/changelog/1615).

Rules this module holds, each guarded in tests/test_sports_up_next.py:

* Watch only and feed only, like a sports channel: never in the TV Guide, nothing records from it,
  and it never plays a provider's stream, so it takes no connection in any feed mode.
* A row of sports_up_next_channels is an identity: a league's row id is its feed id, so a switch
  going off or a follow ending keeps the row. The one for everything followed is league 0 and its
  feed id is fixed (output_feed.UP_NEXT_FEED_BASE), so it has one before its row exists.
* The switch is a person's, written by set_up_next() and nothing else, and off until turned on.
  A league's channel is served while its switch is on and something in the league is followed,
  read where it is served, as a sports channel's is.
* Every page credits TheSportsDB and says where "on" comes from, and a league with nothing in the
  next UP_NEXT_DAYS is named on the page for everything rather than silently missing from it.
"""
import logging
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from typing import Optional

from . import db, sports_feed, sports_match
from .database import Channel, SportsEvent, SportsFollow, SportsLeague, SportsTeam, SportsUpNextChannel
from .db_utils import retry_on_locked
from .fmt_utils import counted, join_labels

log = logging.getLogger(__name__)

#: The league id of the Up next channel for everything followed.
EVERYTHING = 0
NAME = 'Up next'
#: How far ahead an Up next channel looks: a week, the depth of a full guide, so a game it shows
#: has had its chance to be found in the guide.
UP_NEXT_DAYS = 7
#: A league's own channel pages through up to this many pages of its coming games.
LEAGUE_PAGES = 3
#: The words at the foot of every page: where its facts come from.
CREDIT = 'Schedule from TheSportsDB. Where it is on: your guide.'
#: Why a league's switched-on channel is not served (output_feed.FeedRow.left_out).
LEFT_OUT_UNFOLLOWED = 'nothing in its league is followed any more'
NOT_IN_GUIDE = 'Not in your guide yet'
NOT_IN_GUIDE_SHORT = 'Not in guide yet'


class NotFollowed(Exception):
    """A league's channel was turned on with nothing in the league followed. User-facing."""


@dataclass
class UpNext:
    """One Up next channel. `number` is its feed id less output_feed.UP_NEXT_FEED_BASE: 0 for
    everything followed, else its row's id. `enabled` is the switch; `left_out` says why a channel
    whose switch is on is not served, '' when it is."""
    number: int
    league: Optional[SportsLeague]
    name: str
    enabled: bool
    left_out: str = ''

    @property
    def everything(self) -> bool:
        return self.league is None

    @property
    def served(self) -> bool:
        return self.enabled and not self.left_out


def name_for(league) -> str:
    return NAME if league is None else f'{league.name} up next'


def _followed() -> set:
    return {i for (i,) in db.session.query(SportsFollow.league_id).distinct().all()}


def channels(everything: bool = False, number: int = None) -> list:
    """The Up next channels served now, the one for everything first, then each league's by the
    league's name; or with `everything`, every one that has a row, switched on or off, for the
    IPTV Output page, and the one for everything once anything is followed or its row exists.
    `number` narrows it to that channel. Three queries whatever is stored."""
    q = SportsUpNextChannel.query
    if number is not None:
        q = (q.filter(SportsUpNextChannel.league_id == EVERYTHING) if number == EVERYTHING
             else q.filter(SportsUpNextChannel.id == number, SportsUpNextChannel.league_id != EVERYTHING))
    rows = q.all()
    league_ids = {r.league_id for r in rows if r.league_id != EVERYTHING}
    leagues = ({lg.id: lg for lg in SportsLeague.query.filter(SportsLeague.id.in_(league_ids)).all()}
               if league_ids else {})
    followed = _followed() if league_ids or everything else set()
    out = []
    if number in (None, EVERYTHING):
        row = next((r for r in rows if r.league_id == EVERYTHING), None)
        if row is not None or followed:
            out.append(UpNext(EVERYTHING, None, NAME, bool(row is not None and row.enabled)))
    for row in rows:
        league = leagues.get(row.league_id)
        if league is None:
            continue
        out.append(UpNext(row.id, league, name_for(league), bool(row.enabled),
                          '' if league.id in followed else LEFT_OUT_UNFOLLOWED))
    first = 1 if out and out[0].everything else 0
    out[first:] = sorted(out[first:], key=lambda c: (c.league.name.lower(), c.league.id))
    return out if everything else [c for c in out if c.served]


@retry_on_locked()
def set_up_next(league_id: int, on: bool) -> Optional[int]:
    """A person's switch for an Up next channel: `league_id` EVERYTHING for the one for everything
    followed. Returns its feed number, or None when the league is gone. Raises NotFollowed when a
    league's is turned on with nothing in it followed. Turning it off keeps the row, and with it
    the feed id. Ending a play of one that went off is the caller's, after this commit."""
    if league_id != EVERYTHING:
        if db.session.get(SportsLeague, league_id) is None:
            db.session.rollback()
            return None
        if on and not SportsFollow.query.filter_by(league_id=league_id).first():
            db.session.rollback()
            raise NotFollowed('Follow the league or one of its teams first: its Up next channel shows '
                              'the games you follow.')
    row = SportsUpNextChannel.query.filter_by(league_id=league_id).first()
    if row is None:
        row = SportsUpNextChannel(league_id=league_id, enabled=bool(on), created_at=datetime.utcnow())
        db.session.add(row)
    else:
        row.enabled = bool(on)
    db.session.commit()
    return EVERYTHING if league_id == EVERYTHING else row.id


def league_of(number: int) -> Optional[int]:
    """The league id an Up next channel's number is for (EVERYTHING for 0), or None for a number no
    row holds."""
    if number == EVERYTHING:
        return EVERYTHING
    row = db.session.get(SportsUpNextChannel, number)
    return row.league_id if row is not None and row.league_id != EVERYTHING else None


def switch(league_id: int, on: bool) -> Optional[dict]:
    """A person's switch, end to end: set_up_next(), then, after its commit, every play of a
    channel turned off ended by name. {name, number, on, cut}, or None when the league is gone.
    Raises NotFollowed. Requires an app context."""
    from . import output_feed
    number = set_up_next(league_id, on)
    if number is None:
        return None
    league = db.session.get(SportsLeague, league_id) if league_id != EVERYTHING else None
    cut = 0 if on else output_feed.stop_feed_id(output_feed.feed_id_for_up_next(number),
                                                output_feed.REASON_TAKEN_OFF)
    log.info('Up next: %s turned %s%s', name_for(league), 'on' if on else 'off',
             f', {counted(cut, "play")} cut' if cut else '')
    return {'name': name_for(league), 'number': number, 'on': bool(on), 'cut': cut}


def delete_for_league(league_id: int):
    """Inside sports_data.delete_league()'s unit (through sports_feed.delete_for_league): the
    league's Up next channel goes with it, its id never issued again (AUTOINCREMENT)."""
    if league_id != EVERYTHING:
        SportsUpNextChannel.query.filter_by(league_id=league_id).delete(synchronize_session=False)


# ---------------------------------------------------------------------------
# What a channel's pages show
# ---------------------------------------------------------------------------

def _games_by_league(league_ids, now: datetime) -> dict:
    """{league id: [(SportsEvent, its window)]}: each league's followed games not over, starting
    within UP_NEXT_DAYS, in start order, each at the time its sports channel's block gives it
    (sports_feed.event_window over its listings). A league followed whole shows all its games, one
    where only teams are followed shows those teams' games, as a sports channel and
    sports_feed.followed_coming() do: the switch says the channel "shows the games you follow".
    Four queries."""
    if not league_ids:
        return {}
    scope = sports_feed.follow_scope()
    start = sports_feed.event_start()
    events = (SportsEvent.query.filter(SportsEvent.league_id.in_(league_ids),
                                       start > now - sports_feed.MAX_LENGTH,
                                       start < now + timedelta(days=UP_NEXT_DAYS),
                                       SportsEvent.postponed.is_(False))
              .order_by(start, SportsEvent.id).all())
    events = [e for e in events if _followed_game(e, scope)]
    listings = sports_match.listing_windows([e.id for e in events], now) if events else {}
    timed = []
    for event in events:
        window = sports_feed.event_window(event, listings.get(event.id, ()))
        if window is not None and window[1] > now:
            timed.append((event, window))
    out = {}
    for event, window in sorted(timed, key=lambda p: (p[1][0], p[0].id)):
        out.setdefault(event.league_id, []).append((event, window))
    return out


def _followed_game(event, scope: dict) -> bool:
    whole, teams = scope.get(event.league_id, (False, frozenset()))
    return whole or bool({event.home_team_id, event.away_team_id} & teams)


def _drawn(chans: list, by_league: dict, followed: list) -> dict:
    """{channel number: [(league, [(event, window)] for each page)]}: what each channel draws."""
    from .sports_screen import SCREEN_GAMES_DRAWN as PAGE
    out = {}
    for ch in chans:
        if ch.everything:
            firsts = [(lg, by_league[lg.id][:PAGE]) for lg in followed if by_league.get(lg.id)]
            out[ch.number] = sorted(firsts, key=lambda p: (p[1][0][1][0], p[0].name.lower()))
        else:
            games = by_league.get(ch.league.id, [])[:PAGE * LEAGUE_PAGES]
            out[ch.number] = [(ch.league, games[i:i + PAGE]) for i in range(0, len(games), PAGE)]
    return out


def _where(event, start: datetime, carrying: list, listed: list, names: dict, named=None) -> tuple:
    """(long, short): where a game is on. ChannelBin's own sports channel carrying it, else the
    channel whose guide lists it nearest its start and how many more, else, for a game no guide
    lists, the channel whose name mentions it that Record would take (`named`, a NameSource,
    dev/changelog/1643), else not in the guide."""
    if carrying:
        first, more = carrying[0].name, len(carrying) - 1
        return (f'On {first}' + (f' and {counted(more, "other channel")}' if more else ''),
                f'{first} +{more}' if more else first)
    nearest = {}
    for channel_id, _a, begin, _e, _listed in sports_feed.with_the_game(listed, event):
        if channel_id in names:
            off = abs(begin - start)
            nearest[channel_id] = min(off, nearest.get(channel_id, off))
    found = sorted(nearest, key=lambda c: (nearest[c], names[c].lower()))
    if not found and named is not None:
        first, more = named.pick.named.said_by, len({n.channel_id for n in named.names}) - 1
        return (f'On {first}' + (f' and {counted(more, "more channel")}' if more else ''),
                f'{first} +{more}' if more else first)
    if not found:
        return NOT_IN_GUIDE, NOT_IN_GUIDE_SHORT
    first, more = names[found[0]], len(found) - 1
    return (f'On {first}' + (f' and {counted(more, "more channel")}' if more else ''),
            f'{first} +{more}' if more else first)


class _Pages:
    """What every page of one pass draws from: the teams, the badges, the sports channels and the
    guide listings, each read once for every game drawn."""

    def __init__(self, cfg: dict, now: datetime, events: list):
        from .sports_images import badge_paths
        from .tz_utils import get_display_tz, is_24h
        self.cfg, self.now = cfg, now
        self.tz, self.h24 = get_display_tz(), is_24h()
        team_ids = {i for e in events for i in (e.home_team_id, e.away_team_id) if i}
        self.teams = ({t.tsdb_id: t for t in SportsTeam.query.filter(SportsTeam.tsdb_id.in_(team_ids)).all()}
                      if team_ids else {})
        self.badges = badge_paths(cfg, [t.badge_url for t in self.teams.values()])
        served = sports_feed.served_channels()
        self.carriers = sports_feed.carriers(served, events)
        self.listings = sports_match.listing_windows([e.id for e in events], now)
        self.named = sports_feed.name_sources({e.league_id for e in events}, now) if events else {}
        channel_ids = {row[0] for rows in self.listings.values() for row in rows}
        self.names = (dict(db.session.query(Channel.id, Channel.name).filter(Channel.id.in_(channel_ids)).all())
                      if channel_ids else {})

    def game(self, event, window) -> tuple:
        """(Game, when): the game as a page draws it, a stripe's "On now" included, and the
        hero's time line."""
        from .sports_screen import game_of
        game = game_of(event, self.teams, self.cfg, self.now, self.tz, self.h24, self.badges)
        where, short = _where(event, window[0], self.carriers.get(event.id, []),
                              self.listings.get(event.id, []), self.names,
                              None if self.listings.get(event.id) else self.named.get(event.id))
        live = window[0] <= self.now
        when = ((f'Started {game.clock}' if game.clock else 'On now') if live
                else f'{game.day} {game.clock}'.strip())
        drawn = replace(game, where=where, where_short=short, live=live)
        if live:
            drawn = replace(drawn, day='On now', clock='')
        return drawn, when


def _page(ch: UpNext, league, games: list, first: bool, note: str, pages: _Pages):
    from .sports_screen import MODE_PROXY, STATE_NEXT, Screen, Words, league_color
    from .sports_view import _noun
    drawn = [pages.game(e, w) for e, w in games]
    hero, when = drawn[0]
    one = _noun(league, [e for e, _w in games])[0]
    if ch.everything:
        kicker = f'{league.name} - ' + ('on now' if hero.live else f'next {one}')
    elif hero.live:
        kicker = 'On now'
    else:
        kicker = f'Next {one}' if first else 'Coming up'
    color = league_color(league) if hero.field else hero.home.colors[0]
    words = Words(kicker=kicker, headline=hero.name, when=when, note=note, live=hero.live)
    return Screen(ch.name, color, STATE_NEXT, MODE_PROXY, words, hero, tuple(g for g, _w in drawn[1:]),
                  hero.field, later_label=f'Later in {league.name}')


def _nothing(ch: UpNext, followed: list):
    from .sports_screen import ACCENT, MODE_PROXY, STATE_NONE, Screen, Words, league_color
    from .sports_view import _noun
    if ch.everything:
        note = ('Follow a league or a team in Sports Schedules and its games show here.' if not followed
                else f'None of the leagues you follow has a game in the next {UP_NEXT_DAYS} days on '
                     'TheSportsDB.')
        return Screen(ch.name, ACCENT, STATE_NONE, MODE_PROXY,
                      Words(kicker='Nothing scheduled', headline=NAME, when='', note=note))
    league = ch.league
    many = _noun(league)[1]
    note = (f'TheSportsDB lists no {many} for {league.name} in the next {UP_NEXT_DAYS} days. The season '
            f'may be over, or the next ones are not listed yet.')
    return Screen(ch.name, league_color(league), STATE_NONE, MODE_PROXY,
                  Words(kicker=f'No {many} scheduled', headline=league.name, when='', note=note),
                  field=league.sport == 'Motorsport')


def rotations(chans: list, now: datetime, cfg: dict) -> dict:
    """{channel number: sports_screen.Rotation} for each of `chans` (channels()), as it draws at
    `now`. A fixed number of queries whatever is followed or drawn."""
    from .sports_screen import Rotation
    if not chans:
        return {}
    want_all = any(ch.everything for ch in chans)
    followed_ids = _followed() if want_all else set()
    followed = (sorted(SportsLeague.query.filter(SportsLeague.id.in_(followed_ids)).all(),
                       key=lambda lg: (lg.name.lower(), lg.id)) if followed_ids else [])
    league_ids = followed_ids | {ch.league.id for ch in chans if not ch.everything}
    by_league = _games_by_league(league_ids, now)
    drawn = _drawn(chans, by_league, followed)
    events = list({e.id: e for pages in drawn.values() for _lg, games in pages for e, _w in games}.values())
    pages = _Pages(cfg, now, events)
    out = {}
    for ch in chans:
        note = CREDIT
        if ch.everything:
            quiet = [lg.name for lg in followed if not by_league.get(lg.id)]
            if quiet:
                note += f' Nothing in the next {UP_NEXT_DAYS} days: {join_labels(quiet)}.'
        shown = [_page(ch, league, games, i == 0, note, pages) for i, (league, games) in enumerate(drawn[ch.number])]
        out[ch.number] = Rotation(ch.name, tuple(shown) or (_nothing(ch, followed),))
    return out


def team_ids(now: datetime) -> set:
    """The TheSportsDB ids of the teams an Up next channel that is on would draw now, for the
    badges the image worker downloads (app/sports_images.py). A few queries; one with none on."""
    chans = channels()
    if not chans:
        return set()
    want_all = any(ch.everything for ch in chans)
    followed_ids = _followed() if want_all else set()
    followed = (sorted(SportsLeague.query.filter(SportsLeague.id.in_(followed_ids)).all(),
                       key=lambda lg: (lg.name.lower(), lg.id)) if followed_ids else [])
    by_league = _games_by_league(followed_ids | {ch.league.id for ch in chans if not ch.everything}, now)
    drawn = _drawn(chans, by_league, followed)
    return {i for pages in drawn.values() for _lg, games in pages for e, _w in games
            for i in (e.home_team_id, e.away_team_id) if i}
