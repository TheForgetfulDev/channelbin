"""What an auto-record rule that names a league or team reads from TheSportsDB's local tables
(DESIGN-sports-data.md 10, dev/changelog/1490).

A rule is still a Programs search. Naming a league or team adds three things on top of it, each
of which can only add a recording or add information, never take one away on a time it cannot
stand behind:

* **One recording per game.** Among a game's first airings the listing near the game's believed
  start, then the one most channels carry, is judged first, and another listing of the same game is `skipped_existing` only while a
  recording of the game covers that listing's air time. A listing outside it - a wrong time, a
  game that runs past it - is scheduled as well, saying why.
* **Skip game replays** (opt-in). A listing every one of whose matches says replay is skipped
  only while a first airing of the game is actually recorded or scheduled. A failed or
  cancelled first airing frees it, and doubt is never skipped.
* **Expected games** on the rule's page, and one standing alert when a game the rule is about
  starts within hours with nothing anywhere to record it from.

Everything is read once per run, or once per page, into plain values (GameIndex): judging a
showing does no query, the engine's own rule (DESIGN-autoschedule.md 4.3). TheSportsDB being
down leaves a rule judging exactly as a rule that names nothing does, because no match means
no game.
"""
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from sqlalchemy import or_

from . import db, sports_words
from .database import (
    SPORTS_AIRING_FIRST, SPORTS_AIRING_REPLAY, SPORTS_AIRING_UNKNOWN, SPORTS_MATCH_KIND_CHANNEL_NAME,
    SPORTS_MATCH_KIND_GUIDE, SPORTS_MATCH_STRONG, Channel, ChannelGroup, RecordingSports, SportsEvent, SportsLeague,
    SportsMatch, SportsTeam,
)
from .fmt_utils import counted
from .sports_match import NOT_RACES, _offered, combined_airing, event_session, normalize, session_of

#: How much of a listing may fall before a recording of the same game and still count as
#: covered: the pregame slot a channel lists at 8:00 for an 8:15 kickoff. A listing starting
#: earlier than that, or ending after the recording, is not covered and is recorded as well.
PREGAME_SLACK = timedelta(minutes=30)
#: A listing starting within this of the start the matcher believes for a game is picked over a
#: listing further off, however many channels carry that one: the pregame slot still counts as
#: the game, and a listing 95 minutes off is the guide still listing a moved game's old time
#: (dev/changelog/1625).
PICK_NEAR = timedelta(minutes=30)
#: How far ahead the alert looks: a game this close with nothing to record it from will be
#: missed unless a person does something (dev/changelog/923: alerts are for real problems).
ALERT_AHEAD = timedelta(hours=6)
#: How far back a game is still read, so one that started a little ago keeps its line.
LOOKBACK = timedelta(hours=6)
#: A game recorded from an event channel has no listing to read its length from. Unless the league's
#: own strong listings say longer, it is given this long: a short recording of a long game is the
#: worse failure (design 10).
DEFAULT_GAME_LENGTH = timedelta(hours=4)

#: Where an event pick's start came from, every value named where it is read.
START_FROM_NAME = 'name'          # the clock in the channel's name, which carries a time zone
START_FROM_TSDB = 'tsdb'          # TheSportsDB's start, in a league that goes by it (app/sports_times.py)


@dataclass
class Game:
    """One TheSportsDB event, as a rule reads it."""
    id: int
    tsdb_id: int
    league_id: int
    title: str
    name: str
    home_id: int
    away_id: int
    start: datetime | None
    local_date: object
    previous_start: datetime | None
    start_changed_at: datetime | None
    venue: str
    city: str
    season: str
    round: int | None
    postponed: bool
    matchup: bool
    # The start the matcher believes (SportsEvent.guide_start): which listing is the game when the
    # guide lists it at more than one time (keepers()).
    guide_start: datetime | None = None
    # The old time the guide still lists for a game TheSportsDB moved (SportsEvent.guide_behind_start).
    guide_behind_start: datetime | None = None
    # Practice, qualifying or a shootout: recorded by a rule only when it asks (Scope.sessions,
    # dev/changelog/1633). A sprint is a race.
    session: bool = False


def game_windows(game, start, stop, rule, now) -> list:
    """sports_times.windows() for a listing of `game`, which is a Game, not a SportsEvent: the times
    the league's answer to which time a game goes by gives the listing (dev/changelog/1630)."""
    from types import SimpleNamespace
    from .sports_times import event_windows
    as_event = SimpleNamespace(start_at=game.start, guide_start=game.guide_start,
                               guide_behind_start=game.guide_behind_start, previous_start=game.previous_start,
                               start_changed_at=game.start_changed_at, home_team_id=game.home_id,
                               away_team_id=game.away_id, postponed=game.postponed)
    return event_windows(start, stop, as_event, rule, now)


@dataclass
class Held:
    """A recording that is a game, read from its recording_sports link or made by this run."""
    recording_id: int
    airing: str
    where: str = ''
    title: str = ''
    # As a rule reads it (database.rule_status); '' for one this run made, which the run's own
    # library knows.
    status: str = ''
    rule_id: int | None = None


@dataclass
class Named:
    """A strong channel-name match: a channel whose name names the game (design 6.8). `start` is the
    clock its name gives, and only when the name carries a time zone (dev/changelog/1467)."""
    channel_id: int
    account_id: int
    channel_name: str
    category: str
    start: datetime | None
    score: int
    reason: str
    # The duplicate copy whose name was read, when this channel is the kept copy of that stream and
    # its own name does not name the game (design 6.8, dev/changelog/1579). '' when the name read is
    # this channel's own.
    source_name: str = ''

    @property
    def said_by(self) -> str:
        """The channel whose name names the game."""
        return self.source_name or self.channel_name


@dataclass
class Scope:
    """What one rule is about: a league, and a team in it or None for all its games."""
    league_id: int
    team_id: int | None = None
    skip_replays: bool = False
    # Records from event channels (DESIGN-sports-data.md 10, dev/changelog/1491). Without it, a game
    # only an event channel names is one this rule cannot record, and the alert says so.
    event_channels: bool = False
    # Practice, qualifying and shootouts are its games too (dev/changelog/1633). Without it a session
    # is not one of the rule's games: no expected line, no alert, and a listing of one its search
    # finds is judged as a listing of no game and still recorded. No channel's name is read for a
    # session, so it is never recorded from an event channel either way.
    sessions: bool = False

    def covers(self, game: Game) -> bool:
        if game.league_id != self.league_id or (game.session and not self.sessions):
            return False
        return not self.team_id or self.team_id in (game.home_id, game.away_id)


def game_title(event) -> str:
    """`Away at Home` for a matchup, the way guides list it, and the event's name otherwise."""
    if (event.home_team_id or event.away_team_id) and event.home_name and event.away_name:
        return f'{event.away_name} at {event.home_name}'
    return event.name


@dataclass
class GameIndex:
    """The games of some leagues and what the guide says about them, read once."""
    games: dict = field(default_factory=dict)          # {event id: Game}
    by_tsdb: dict = field(default_factory=dict)        # {TheSportsDB event id: Game}
    by_showing: dict = field(default_factory=dict)     # {(channel, start, title key): {event id: [airing]}}
    by_listing: dict = field(default_factory=dict)     # {(title key, episode key, start): {event id: [airing]}}
    by_title: dict = field(default_factory=dict)       # {(title key, start): {event id: [airing]}}
    reasons: dict = field(default_factory=dict)        # {event id: a sentence from one strong match}
    listed: set = field(default_factory=set)           # events with a live guide listing on a shown channel
    named: set = field(default_factory=set)            # events an event channel's name names
    named_by: dict = field(default_factory=dict)       # {event id: [Named]} strong, recordable
    held: dict = field(default_factory=dict)           # {event id: [Held]}
    rules: dict = field(default_factory=dict)          # {league id: sports_times.Rule, which time it goes by}
    sports: dict = field(default_factory=dict)         # {league id: TheSportsDB's sport, for its word}
    now: datetime = None
    longest: dict = field(default_factory=dict)        # {league id: its longest strong listing}

    @classmethod
    def load(cls, league_ids, now, horizon_days=21, lookback=LOOKBACK, held=True):
        """The games of `league_ids` from `lookback` ago to `horizon_days` ahead, their live guide
        and channel-name matches, and the recordings linked to them (`held` False leaves those out,
        for a reader that only asks what a game would be taken from). A fixed handful of queries.
        A league page's Record reads back a day, since that page keeps a game all its day."""
        out = cls(now=now)
        league_ids = sorted({i for i in league_ids if i})
        if not league_ids:
            return out
        lo, hi = now - lookback, now + timedelta(days=horizon_days)
        events = SportsEvent.query.filter(
            SportsEvent.league_id.in_(league_ids),
            or_(SportsEvent.start_at.between(lo, hi),
                SportsEvent.start_at.is_(None) & SportsEvent.local_date.between(lo.date(), hi.date())),
        ).all()
        for e in events:
            out.games[e.id] = Game(
                id=e.id, tsdb_id=e.tsdb_id, league_id=e.league_id, title=game_title(e), name=e.name,
                home_id=e.home_team_id or 0, away_id=e.away_team_id or 0, start=e.start_at,
                local_date=e.local_date, previous_start=e.previous_start,
                start_changed_at=e.start_changed_at, venue=e.venue or '', city=e.city or '',
                season=e.season or '', round=e.round, postponed=bool(e.postponed),
                matchup=bool(e.home_team_id or e.away_team_id), guide_start=e.guide_start,
                guide_behind_start=e.guide_behind_start, session=event_session(e).kind in NOT_RACES)
        out.by_tsdb = {g.tsdb_id: g for g in out.games.values()}
        if not out.games:
            return out
        from .sports_times import Rules
        rules = Rules()
        for lg_id, tsdb_id, sport in db.session.query(SportsLeague.id, SportsLeague.tsdb_id, SportsLeague.sport).filter(
                SportsLeague.id.in_(league_ids)):
            out.rules[lg_id] = rules.of(tsdb_id)
            out.sports[lg_id] = sport
        ids = sorted(out.games)
        rows = db.session.query(
            SportsMatch.event_id, SportsMatch.channel_id, SportsMatch.kind, SportsMatch.program_start_time,
            SportsMatch.program_stop_time, SportsMatch.title_key, SportsMatch.episode_key, SportsMatch.airing, SportsMatch.strength,
            SportsMatch.reason, SportsMatch.gone_since, Channel.account_id, Channel.name.label('channel_name'),
            Channel.category_name, Channel.is_duplicate_loser, Channel.duplicate_cluster_id,
            Channel.health_score, Channel.manual_health_adjustment,
        ).join(Channel, Channel.id == SportsMatch.channel_id).filter(
            SportsMatch.event_id.in_(ids), SportsMatch.rejected_at.is_(None), _offered(),
        ).all()
        on_copies = []
        for m in rows:
            if m.gone_since is not None:
                # A listing the guide no longer holds still says which game a ledger row was, so a
                # person's cancellation of it keeps covering the game (R6). It says nothing else.
                if (m.kind == SPORTS_MATCH_KIND_GUIDE and m.strength == SPORTS_MATCH_STRONG
                        and m.program_start_time is not None):
                    key_l = (m.title_key or '', m.episode_key or '', m.program_start_time)
                    out.by_listing.setdefault(key_l, {}).setdefault(m.event_id, []).append(m.airing)
                    out.by_title.setdefault(key_l[0::2], {}).setdefault(m.event_id, []).append(m.airing)
                continue
            if m.kind == SPORTS_MATCH_KIND_CHANNEL_NAME:
                out.named.add(m.event_id)
                # Only the kept copy of a stream is one a rule may record from (design 10). A strong
                # name on a duplicate copy is still a name for that stream, so it is carried over to
                # the kept copy below.
                if m.strength == SPORTS_MATCH_STRONG and m.is_duplicate_loser:
                    on_copies.append(m)
                elif m.strength == SPORTS_MATCH_STRONG:
                    from .channel_groups import effective_score
                    out.named_by.setdefault(m.event_id, []).append(Named(
                        channel_id=m.channel_id, account_id=m.account_id, channel_name=m.channel_name,
                        category=m.category_name or '', start=m.program_start_time,
                        score=effective_score(m), reason=m.reason))
                continue
            if m.kind != SPORTS_MATCH_KIND_GUIDE or m.program_start_time is None:
                continue
            # Any guide listing means the game is listed, which only holds the alert back. Only a
            # strong one says which game a listing is: a weak match must never be what skips a
            # recording (design 10).
            out.listed.add(m.event_id)
            if m.strength != SPORTS_MATCH_STRONG:
                continue
            tkey, ep = m.title_key or '', m.episode_key or ''
            out.by_showing.setdefault((m.channel_id, m.program_start_time, tkey), {}) \
                .setdefault(m.event_id, []).append(m.airing)
            out.by_listing.setdefault((tkey, ep, m.program_start_time), {}) \
                .setdefault(m.event_id, []).append(m.airing)
            out.by_title.setdefault((tkey, m.program_start_time), {}) \
                .setdefault(m.event_id, []).append(m.airing)
            if m.program_stop_time is not None:
                league = out.games[m.event_id].league_id
                out.longest[league] = max(out.longest.get(league, timedelta(0)),
                                          m.program_stop_time - m.program_start_time)
            if m.event_id not in out.reasons:
                out.reasons[m.event_id] = m.reason
        out._name_kept_copies(on_copies)
        if held:
            out._load_held()
        return out

    def _name_kept_copies(self, on_copies: list):
        """Give each strong name match on a duplicate copy to the kept copy of its stream, unless
        that copy's own name already names the game. The kept copy's guide can keep repeating the
        names it had on earlier days, so its own name is never read: on 2026-11-13, 16 of prod's 43
        strong name matches were on duplicate copies and none of the 16 kept copies had a match, so
        those games had nothing to record from (dev/changelog/1579). Two queries, and none when no
        copy names a game."""
        clusters = sorted({m.duplicate_cluster_id for m in on_copies if m.duplicate_cluster_id is not None})
        if not clusters:
            return
        from .channel_groups import effective_score
        # A cluster keeps one copy; a list all the same, since that is the duplicate module's rule
        # and not a constraint on the table. The best-scored kept copy is the one named.
        kept = {}
        for ch in db.session.query(
                Channel.id, Channel.account_id, Channel.name, Channel.category_name, Channel.duplicate_cluster_id,
                Channel.health_score, Channel.manual_health_adjustment).filter(
                Channel.duplicate_cluster_id.in_(clusters), Channel.is_duplicate_loser.is_(False), _offered()):
            kept.setdefault(ch.duplicate_cluster_id, []).append(ch)
        # A kept copy whose own name match for the game a person marked wrong stays out: the
        # copy's name is the same claim about the same stream.
        kept_ids = sorted({c.id for copies in kept.values() for c in copies})
        wrong = set(db.session.query(SportsMatch.event_id, SportsMatch.channel_id).filter(
            SportsMatch.event_id.in_(sorted({m.event_id for m in on_copies})),
            SportsMatch.channel_id.in_(kept_ids), SportsMatch.kind == SPORTS_MATCH_KIND_CHANNEL_NAME,
            SportsMatch.rejected_at.isnot(None))) if kept_ids else set()
        for m in sorted(on_copies, key=lambda m: (m.event_id, m.channel_id)):
            copies = kept.get(m.duplicate_cluster_id)
            if not copies:
                continue
            ch = max(copies, key=lambda c: (effective_score(c), -c.id))
            if (m.event_id, ch.id) in wrong:
                continue
            have = self.named_by.setdefault(m.event_id, [])
            if any(n.channel_id == ch.id for n in have):
                continue
            have.append(Named(
                channel_id=ch.id, account_id=ch.account_id, channel_name=ch.name, category=ch.category_name or '',
                start=m.program_start_time, score=effective_score(ch), reason=m.reason,
                source_name=m.channel_name))

    def _load_held(self):
        by_tsdb = {g.tsdb_id: g.id for g in self.games.values()}
        links = db.session.query(RecordingSports.recording_id, RecordingSports.tsdb_event_id,
                                 RecordingSports.airing).filter(
            RecordingSports.tsdb_event_id.in_(sorted(by_tsdb)), RecordingSports.removed_at.is_(None)).all()
        if not links:
            return
        from .database import Recording, rule_status
        recs = {r.id: r for r in db.session.query(
            Recording.id, Recording.channel_id, Recording.group_id, Recording.program_title,
            Recording.status, Recording.gave_up_reason, Recording.rule_id).filter(
            Recording.id.in_(sorted({link.recording_id for link in links})))}
        chans = {r.channel_id for r in recs.values() if r.channel_id}
        groups = {r.group_id for r in recs.values() if r.group_id}
        cnames = dict(db.session.query(Channel.id, Channel.name).filter(Channel.id.in_(sorted(chans)))) \
            if chans else {}
        gnames = dict(db.session.query(ChannelGroup.id, ChannelGroup.name).filter(
            ChannelGroup.id.in_(sorted(groups)))) if groups else {}
        for link in links:
            rec = recs.get(link.recording_id)
            if rec is None:
                continue
            where = gnames.get(rec.group_id) or cnames.get(rec.channel_id) or ''
            self.held.setdefault(by_tsdb[link.tsdb_event_id], []).append(
                Held(recording_id=rec.id, airing=link.airing, where=where, title=rec.program_title or '',
                     status=rule_status(rec.status, rec.gave_up_reason), rule_id=rec.rule_id))

    # -- what a game is called ------------------------------------------------------------------

    def one(self, game) -> str:
        """The word for `game`: game, match, race or event (app/sports_words.py)."""
        return sports_words.noun(self.sports.get(game.league_id), game.matchup)[0]

    def words(self, league_id) -> tuple:
        """(one, many) for a league's events as this index holds them."""
        mine = [g for g in self.games.values() if g.league_id == league_id]
        return sports_words.noun(self.sports.get(league_id), any(g.matchup for g in mine) if mine else None)

    # -- which game a listing is --------------------------------------------------------------

    def _one(self, found: dict, scope: Scope):
        """(Game, airing) when exactly one game in the rule's scope is matched, else (None, None).
        Two games at one listing is a question this does not answer for anyone."""
        mine = {eid: airings for eid, airings in found.items()
                if eid in self.games and scope.covers(self.games[eid])}
        if len(mine) != 1:
            return None, None
        eid, airings = next(iter(mine.items()))
        return self.games[eid], combined_airing(airings)

    def program_game(self, program, scope: Scope):
        """(Game, airing) for an engine program, from the matches of the showings it was built
        from, each looked up on its own channel."""
        found = {}
        for s in program.showings:
            for eid, airings in self.by_showing.get((s.channel_id, s.start, s.title_key), {}).items():
                found.setdefault(eid, []).extend(airings)
        return self._one(found, scope)

    def row_game(self, row, scope: Scope):
        """(Game, airing) for a ledger row: by its game for one recorded from an event channel, else
        by its listing."""
        tsdb = getattr(row, 'sports_event_tsdb_id', None)
        if tsdb:
            game = self.by_tsdb.get(tsdb)
            return (game, SPORTS_AIRING_UNKNOWN) if game is not None and scope.covers(game) else (None, None)
        return self.listing_game(row.title_key, row.episode_key, getattr(row, 'start', None)
                                 or row.program_start_time, scope)

    def listing_game(self, tkey, episode, start, scope: Scope):
        """(Game, airing) for a ledger row, which has no channel of its listing: by title, episode and
        start, and by title and start when its episode was never told apart. Gone listings count here,
        and only here."""
        game, airing = self._one(self.by_listing.get((tkey, episode or '', start), {}), scope)
        if game is None and not episode:
            game, airing = self._one(self.by_title.get((tkey, start), {}), scope)
        return game, airing

    def in_scope(self, scope: Scope) -> list:
        """The rule's games, soonest first; one with no time sorts by its day."""
        def key(g):
            return (g.start or datetime.combine(g.local_date, datetime.min.time()), g.id)
        return sorted((g for g in self.games.values() if scope.covers(g)), key=key)


@dataclass
class EventPick:
    """A game a rule would record from an event channel: the channel, and the start it can stand
    behind and where that came from. `confirmed` is False for TheSportsDB's own time."""
    game: Game
    named: Named
    start: datetime
    stop: datetime
    source: str

    @property
    def confirmed(self) -> bool:
        return self.source == START_FROM_NAME


def event_pick(games, game):
    """(EventPick or None, why not). The channel is the healthiest that names the game, preferring
    one whose name gives a time zone; the start is that clock, else TheSportsDB's start in a league
    that goes by it or covers both (app/sports_times.py, dev/changelog/1630). The league's wait
    holds TheSportsDB's time back only while the guide lists the game: the wait exists so a far-off
    game goes by its listing, and a game with no listing has nothing else to go by
    (dev/changelog/1660). A recording made on TheSportsDB's time moves with it until it starts
    (sports_follow). With neither there is no start to stand behind (design 10): a league that goes
    by the guide has no listing of this game to go by, and NASCAR's 00:00 placeholders and a name
    with a bare clock land there too."""
    from .sports_times import GO_GUIDE
    named = named_order(games, game)
    if not named:
        return None, 'No event channel names it.'
    best = named[0]
    rule = games.rules.get(game.league_id)
    if best.start is not None:
        start, source = best.start, START_FROM_NAME
    elif game.start is None or not (game.matchup or (game.start.hour, game.start.minute) != (0, 0)):
        return None, ('An event channel names it, but its name gives no time zone and TheSportsDB gives no '
                      'time for it, so there is no start to stand behind.')
    elif rule is None or rule.go_by == GO_GUIDE:
        return None, ('An event channel names it, but its name gives no time zone, and this league goes by '
                      'your guide, which has no listing of it, so there is no start to stand behind.')
    elif game.id in games.listed and games.now is not None and games.now < game.start - rule.wait:
        return None, (f"An event channel names it, but its name gives no time zone, and while your guide lists "
                      f"it this league goes by TheSportsDB's time only from {counted(rule.wait_hours, 'hour')} "
                      f"before it, so there is no start to stand behind yet.")
    else:
        start, source = game.start, START_FROM_TSDB
    length = max(games.longest.get(game.league_id, timedelta(0)), DEFAULT_GAME_LENGTH)
    return EventPick(game=game, named=best, start=start, stop=start + length, source=source), ''


def named_order(games, game) -> list:
    """The strong name matches for `game` in the order they are taken: a name that gives a time
    zone first, then health. event_pick() takes the first; a sports channel's play walks on past
    one it cannot use (auto_rules.game_sources())."""
    return sorted(games.named_by.get(game.id, ()), key=lambda n: (n.start is None, -n.score, n.channel_id))


def name_pick(games, game):
    """The EventPick a game is taken from when the guide has no listing of it at all, or None:
    any listing is the search's to decide, even a weak one (design 10). The one condition
    auto_rules.game_sources()' PICK_EVENT answer and the sports channels on the IPTV output read
    (dev/changelog/1643)."""
    if game.id in games.listed:
        return None
    return event_pick(games, game)[0]


@dataclass(frozen=True)
class NameSource:
    """A game no guide lists, as the sports channels on the IPTV output read it: its event pick,
    and every channel naming it in named_order(), the first being the pick's."""
    pick: EventPick
    names: tuple


def name_sources(league_ids, now, lookback=LOOKBACK) -> dict:
    """{SportsEvent id: NameSource} for every game of `league_ids` that only an event channel's
    name gives (name_pick()), read in one GameIndex load: a fixed handful of queries whatever is
    stored. What a sports channel's guide, play and Start broadcast read for the games no guide lists."""
    games = GameIndex.load(league_ids, now, lookback=lookback, held=False)
    out = {}
    for game in games.games.values():
        pick = name_pick(games, game)
        if pick is not None:
            out[game.id] = NameSource(pick, tuple(named_order(games, game)))
    return out


def covers(window_start, window_stop, start, stop) -> bool:
    """Does a recording of the game, `window_start` to `window_stop`, hold this listing? Up to
    PREGAME_SLACK of the listing may fall before it, and none after it."""
    if None in (window_start, window_stop, start, stop):
        return False
    return window_start <= start + PREGAME_SLACK and window_stop >= stop


def near_start(game, start) -> bool:
    """A listing starting within PICK_NEAR of the start the matcher believes for the game. False
    for a game with no believed start, which leaves every listing equal here."""
    gs = getattr(game, 'guide_start', None)
    return gs is not None and start is not None and abs(start - gs) <= PICK_NEAR


def keepers(programs, games_of, replays=False) -> set:
    """The identity of each game's keeper among `programs`: of the listings that are not a known
    replay, one starting near the game's believed start (near_start()), then the one carried on
    the most channels, then the earliest. One channel listing a moved game at its new time beats
    14 still listing the old one (dev/changelog/1625). `games_of` is {identity: (Game, airing)}.
    With `replays`, the order over the known replays only, by channels and start, since no replay
    is at the start: what a league page's Record picks for a game whose live airing is not in the
    guide or is over (dev/changelog/1539). A rule never asks for that."""
    return {ranked[0].identity for ranked in ranked_programs(programs, games_of, replays).values()}


def ranked_programs(programs, games_of, replays=False) -> dict:
    """{game id: its programs among `programs`, best first} in keepers()' order, which is the
    first of each. The rest are what a sports channel's play falls back to once every channel of
    the keeper has failed it (auto_rules.game_sources())."""
    ranked = {}
    for p in programs:
        game, airing = games_of.get(p.identity, (None, None))
        if game is None or (airing == SPORTS_AIRING_REPLAY) != replays:
            continue
        near = not replays and near_start(game, p.lead.start)
        ranked.setdefault(game.id, []).append(((not near, -len(p.showings), p.lead.start, p.identity), p))
    return {gid: [p for _rank, p in sorted(found, key=lambda r: r[0])] for gid, found in ranked.items()}


def first_airing_held(held_list, spoken, lib_by_id) -> Held | None:
    """A recording of the game's first airing that is scheduled, recording or recorded."""
    for h in held_list:
        rec = lib_by_id.get(h.recording_id)
        if h.airing == SPORTS_AIRING_FIRST and rec is not None and rec.status in spoken:
            return h
    return None


def scope_of(rule) -> Scope | None:
    if not rule.sports_league_id:
        return None
    return Scope(league_id=rule.sports_league_id, team_id=rule.sports_team_tsdb_id or None,
                 skip_replays=bool(rule.sports_skip_replays),
                 event_channels=bool(rule.sports_event_channels),
                 sessions=bool(rule.sports_sessions))


def rule_words(rule) -> tuple:
    """(one, many) for the events of the league a rule names: its sport's word, read from the
    shape of its stored events unless the rule names a team, which is always a side in a game or
    a match. ('game', 'games') for a rule that names nothing. Two queries at most."""
    if not rule.sports_league_id:
        return sports_words.GAME
    sport = db.session.query(SportsLeague.sport).filter(SportsLeague.id == rule.sports_league_id).scalar()
    if rule.sports_team_tsdb_id:
        return sports_words.noun(sport, True)
    shapes = {bool(home or away) for home, away in db.session.query(
        SportsEvent.home_team_id, SportsEvent.away_team_id).filter(
        SportsEvent.league_id == rule.sports_league_id).distinct().limit(50)}
    return sports_words.noun(sport, (True in shapes) if shapes else None)


def about_text(rule) -> str | None:
    """`Buffalo Bills (NFL)` or `NFL`, or None for a rule that names nothing. A league since removed
    reads as its id rather than disappearing."""
    if not rule.sports_league_id:
        return None
    league = db.session.get(SportsLeague, rule.sports_league_id)
    league_name = league.name if league is not None else f'League #{rule.sports_league_id}'
    if not rule.sports_team_tsdb_id:
        return league_name
    return f'{team_names([rule.sports_team_tsdb_id])[rule.sports_team_tsdb_id]} ({league_name})'


def team_names(team_ids) -> dict:
    """{team id: name} for these teams in two queries: a looked-up row, else a stored game naming
    it, else its id - so a team whose games retention pruned still reads as itself."""
    ids = sorted({i for i in team_ids if i})
    if not ids:
        return {}
    out = dict(db.session.query(SportsTeam.tsdb_id, SportsTeam.full_name).filter(SportsTeam.tsdb_id.in_(ids)))
    missing = [i for i in ids if i not in out]
    if missing:
        for hid, hname, aid, aname in db.session.query(
                SportsEvent.home_team_id, SportsEvent.home_name, SportsEvent.away_team_id,
                SportsEvent.away_name).filter(or_(SportsEvent.home_team_id.in_(missing),
                                                  SportsEvent.away_team_id.in_(missing))):
            for tid, tname in ((hid, hname), (aid, aname)):
                if tid in missing and tname:
                    out.setdefault(tid, tname)
    return {i: out.get(i) or f'Team #{i}' for i in ids}


def _field_names(league_ids=None):
    """(league id, name) of the stored events between no teams, one query."""
    q = db.session.query(SportsEvent.league_id, SportsEvent.name).filter(
        SportsEvent.home_team_id == 0, SportsEvent.away_team_id == 0)
    if league_ids is not None:
        q = q.filter(SportsEvent.league_id.in_(sorted(league_ids)))
    return q.distinct()


def is_session_name(name) -> bool:
    return session_of(normalize(name)).kind in NOT_RACES


def league_has_sessions(league_id) -> bool:
    """Whether a league's stored events include practice, qualifying or a shootout, which is when a
    rule about it is offered the choice to record them (dev/changelog/1633)."""
    return bool(league_id) and any(is_session_name(n) for _lid, n in _field_names([league_id]))


def dialog_leagues() -> list:
    """What the rule dialog offers: every league on Sports Schedules and the teams its stored games
    name, read from the games since lookup_all_teams is unusable on the free key (design 2), and
    whether its events include sessions (league_has_sessions). Three queries."""
    leagues = SportsLeague.query.order_by(SportsLeague.name).all()
    teams = {}
    for lid, hid, hname, aid, aname in db.session.query(
            SportsEvent.league_id, SportsEvent.home_team_id, SportsEvent.home_name,
            SportsEvent.away_team_id, SportsEvent.away_name).distinct():
        for tid, tname in ((hid, hname), (aid, aname)):
            if tid and tname:
                teams.setdefault(lid, {})[tid] = tname
    with_sessions = {lid for lid, name in _field_names() if is_session_name(name)}
    return [{'id': lg.id, 'name': lg.name, 'sessions': lg.id in with_sessions,
             'teams': sorted(({'id': t, 'name': n} for t, n in teams.get(lg.id, {}).items()),
                             key=lambda t: t['name'].lower())}
            for lg in leagues]


AIRING_WORDS = {SPORTS_AIRING_FIRST: 'first airing', SPORTS_AIRING_REPLAY: 'replay',
                SPORTS_AIRING_UNKNOWN: ''}

