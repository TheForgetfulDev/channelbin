"""Matching TheSportsDB's events to the guide rows that carry them (dev/docs/DESIGN-sports-data.md 6, 7).

It reads only local tables and makes no call to TheSportsDB. A match is what the provider's text
claims, not what the stream carries, and every one stores its strength and a sentence saying why.

The rules, in short. A matchup row needs the league (a match term in its text, or a category a person
tied to the league), both teams, and a start inside 13 hours of the event's listed start; the time
then sets only strength and first-or-replay, never which game it is (6.3). A field event, such as a
race, is matched on the league and its local date, and its name only tells a match strong from weak
(6.4). Only the title and sub-title are read: the description is where every wide false match came
from, and on 2026-11-07 every local station that named a game named it there (6.7).

One run takes an admission ticket and works one league at a time: one query for the league's rows,
then one event at a time, each in its own commit with a pause after it, so a big import is not
followed by a burst of writes (6.6).
"""
import json
import logging
import re
import time as time_mod
import unicodedata
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import datetime, time, timedelta

from sqlalchemy import and_, or_

from . import admission, db, event_names
from .database import (
    Channel, EPGEntry, SPORTS_AIRING_FIRST, SPORTS_AIRING_REPLAY, SPORTS_AIRING_UNKNOWN,
    SPORTS_MATCH_KIND_CHANNEL_NAME, SPORTS_MATCH_KIND_GUIDE, SPORTS_MATCH_STRONG, SPORTS_MATCH_WEAK, SportsEvent, SportsLeague,
    SportsLeagueCategory, SportsMatch, SportsState, SportsTeam,
)
from .db_utils import retry_on_locked
from .fmt_utils import counted
from .program_titles import strip_marks, title_key
from .tz_utils import format_local, get_display_tz, is_24h

log = logging.getLogger(__name__)

#: Which game a row is: league and both teams within this much of TheSportsDB's start (6.3). 13 and
#: not 12, since an AM/PM typo lands at exactly 12 and a row a few minutes late would fall outside.
WINDOW = timedelta(hours=13)
#: A row this far before the start is a placeholder (`NFL | 09 - 1pm Broncos at Panthers`, listed
#: every 2 hours for days), unless the event's time disagrees with the guide.
EARLY_LIMIT = timedelta(minutes=90)
#: Within this, a trusted league's time agrees; up to OFF_LIMIT off is weak, naming how far.
AGREE = timedelta(minutes=5)
OFF_LIMIT = timedelta(minutes=90)
#: Most channels listing the game further than this from TheSportsDB's start: its time disagrees.
DISAGREE = timedelta(minutes=90)
#: A field event's day, from its local date, wide enough for any timezone (6.4).
BAND_MARGIN = timedelta(hours=12)
MIN_GAME_MINUTES = 90
MIN_RACE_MINUTES = 120
TRUST_KEEP = 30
TRUST_MIN_SAMPLE = 10
TRUST_SHARE = 0.9
#: Between one event's write and the next, so a big run is spread out rather than a burst.
PAUSE_SECONDS = 0.5
#: How far back a run looks: the window behind a game that started 2 hours ago (6.6).
LOOKBACK = timedelta(hours=2) + WINDOW
RETRY_MINUTES = 5

_NOT_A_GAME = re.compile(r'\b(highlights?|preview|pre ?game|post ?game|betting|odds|recap|condensed|'
                         r'countdown|in game|analysis)\b')
_NOT_A_RACE = re.compile(r'\b(practice|qualifying|qualifier|pole|classics?|highlights?|best of|countdown|'
                         r'preview|pre ?race|post ?race|recap)\b')
#: Words that say nothing about which race a row is (6.4).
_RACE_FILLER = frozenset('live new race series cup championship the at from in on of and coverage motor '
                         'motorsport motorsports racing nascar'.split())
_NOT_WORD = re.compile(r"[^\w']+")
_APOSTROPHES = str.maketrans({'’': "'", '‘': "'", '`': "'"})


def normalize(text: str | None) -> str:
    """Text as matching reads it: the providers' marks off, accents folded, casefolded, and anything
    but letters, digits and apostrophes turned to spaces, so `Dallas 🏈 [NFL] Cowboys` reads as
    `dallas nfl cowboys` and `Montréal` as `montreal`. Marks come off before folding, since folding
    turns `ᴸᶦᵛᵉ` into the word `live`."""
    text = strip_marks(text).translate(_APOSTROPHES)
    text = ''.join(ch for ch in unicodedata.normalize('NFKD', text) if not unicodedata.combining(ch))
    text = _NOT_WORD.sub(' ', text.casefold()).replace('_', ' ')
    return ' '.join(text.split())


def _has(haystack: str, phrase: str) -> bool:
    """Whole words only: `nfl` is not in `unflagged`."""
    return bool(phrase) and f' {phrase} ' in f' {haystack} '


# ---------------------------------------------------------------------------
# Trust (7)
# ---------------------------------------------------------------------------

def league_trusted(league) -> bool:
    """Whether a league's start times can be believed: at least 10 events measured and 90% of them
    within 5 minutes of the guide. A league not yet measured is not trusted, the safe side."""
    sample, agreed = league.trust_sample or 0, league.trust_agreed or 0
    return sample >= TRUST_MIN_SAMPLE and agreed >= TRUST_SHARE * sample


def combined_airing(airings) -> str:
    """One listing's airing from what its matches say on each channel that carries it: a first airing
    if any channel's match says so, a replay only when every one does, and doubt otherwise. Doubt is
    never skipped (6.5), so a listing reads as a replay only when nothing argues against it."""
    seen = set(airings)
    if SPORTS_AIRING_FIRST in seen:
        return SPORTS_AIRING_FIRST
    if seen == {SPORTS_AIRING_REPLAY}:
        return SPORTS_AIRING_REPLAY
    return SPORTS_AIRING_UNKNOWN


def time_disagrees(event) -> bool:
    """Most of the event's channels list it more than 90 minutes from TheSportsDB's start (6.5)."""
    return (event.start_at is not None and event.guide_start is not None
            and abs(event.guide_start - event.start_at) > DISAGREE)


# ---------------------------------------------------------------------------
# What a league and its teams look like in text
# ---------------------------------------------------------------------------

@dataclass
class TeamText:
    names: list
    city: str = ''


@dataclass
class LeagueText:
    league: SportsLeague
    terms: list
    #: What the SQL may narrow on: LIKE compares raw text, so it is given ASCII runs that both a
    #: folded and an accented spelling contain (_like_needles).
    needles: list
    #: category name -> is_replay, for this league's tied categories.
    categories: dict
    #: Normalized terms and tied category names of every other league.
    other_terms: list
    other_categories: set
    teams: dict = field(default_factory=dict)

    def evidence(self, text: str, category: str | None) -> str | None:
        """'text' or 'category' for this league, or None. A row that names another league and not
        this one, or sits only in another league's category, is never this league's (6.3)."""
        if any(_has(text, t) for t in self.terms):
            return 'text'
        if any(_has(text, t) for t in self.other_terms):
            return None
        if category is not None and category in self.categories:
            return 'category'
        return None


_ASCII_RUN = re.compile(r'[A-Za-z0-9]+')


def _like_needles(term: str) -> set:
    """The longest plain-ASCII run of the term as typed and as folded. LIKE sees the raw title, so a
    folded `serie a` alone would miss `Série A`, and the raw `Série` alone would miss `Serie A`;
    `rie` and `serie` between them catch both. A needle only narrows the scan, so a broad one costs
    rows read, never a match (the whole-word check is in Python). The one spelling it cannot predict
    is an accent the term lacks (`Serie` against a guide's `Série`); adding that spelling as a term
    is the fix, which is what editing a league's terms is for (5.1)."""
    out = set()
    for form in (term, normalize(term)):
        runs = _ASCII_RUN.findall(form)
        if runs:
            out.add(max(runs, key=len).lower())
    return out


def _league_text(league: SportsLeague, others: list, categories: list) -> LeagueText:
    raw = [t for t in json.loads(league.match_terms or '[]') if t and normalize(t)]
    terms = [normalize(t) for t in raw]
    needles = sorted({n for t in raw for n in _like_needles(t)})
    mine = {c.category_name: c.is_replay for c in categories if c.league_id == league.id}
    other_terms = sorted({normalize(t) for o in others for t in json.loads(o.match_terms or '[]')} - set(terms))
    other_cats = {c.category_name for c in categories if c.league_id != league.id} - set(mine)
    return LeagueText(league, terms, needles, mine, [t for t in other_terms if t], other_cats)


def leagues_for_listing(title, sub_title, category) -> list:
    """The leagues a listing belongs to by the matcher's own league rule (6.3): a match term in its
    title or episode name, or a category a person tied to the league. One query per table, for one
    listing; never call it per row."""
    leagues = SportsLeague.query.all()
    categories = SportsLeagueCategory.query.all()
    text = normalize(' '.join(x for x in (title, sub_title) if x))
    return [league for league in leagues
            if _league_text(league, [o for o in leagues if o.id != league.id], categories).evidence(text, category)]


def _load_teams(lt: LeagueText, events: list):
    """Each team's names and usable city (6.2). Until a team's lookup has run only its full name from
    the event counts. A city shared by two of the league's teams, anywhere in its stored schedule and
    not only in this run's games, reads as neither."""
    ids = {i for e in events for i in (e.home_team_id, e.away_team_id) if i}
    league_ids = {i for pair in db.session.query(SportsEvent.home_team_id, SportsEvent.away_team_id)
                  .filter(SportsEvent.league_id == lt.league.id).distinct().all() for i in pair if i} | ids
    rows = {t.tsdb_id: t for t in SportsTeam.query.filter(SportsTeam.tsdb_id.in_(league_ids)).all()}
    fallback = {}
    for e in events:
        fallback.setdefault(e.home_team_id, e.home_name)
        fallback.setdefault(e.away_team_id, e.away_name)
    cities = Counter()
    for tid in league_ids:
        team = rows.get(tid)
        if team is not None and team.city and team.last_looked_up is not None:
            cities[normalize(team.city)] += 1
        elif team is not None or fallback.get(tid):
            # Not looked up yet: its city is a guess, the full name less its last word, and only
            # ever counted here. Without it, `New York` would read as the Giants' city while the
            # Jets' lookup was still queued.
            words = normalize(team.full_name if team is not None else fallback[tid]).split()
            if len(words) > 1:
                cities[' '.join(words[:-1])] += 1
    for tid in ids:
        team = rows.get(tid)
        names = [fallback.get(tid) or '']
        city = ''
        if team is not None:
            names = [team.full_name] + json.loads(team.names or '[]')
            if team.last_looked_up is None:
                names = [team.full_name]
            elif team.city and cities[normalize(team.city)] == 1:
                city = normalize(team.city)
        lt.teams[tid] = TeamText(sorted({normalize(n) for n in names if n and normalize(n)}, key=len, reverse=True),
                                 city)


def _team_in(text: str, team: TeamText) -> str | None:
    if any(_has(text, n) for n in team.names):
        return 'name'
    if team.city and _has(text, team.city):
        return 'city'
    return None


# ---------------------------------------------------------------------------
# Rows
# ---------------------------------------------------------------------------

@dataclass
class Row:
    channel_id: int
    category: str | None
    title: str
    sub_title: str | None
    start: datetime
    stop: datetime
    is_live: bool
    text: str = ''

    @property
    def minutes(self) -> float:
        return (self.stop - self.start).total_seconds() / 60

    @property
    def keys(self) -> tuple:
        return title_key(self.title), title_key(self.sub_title)


def _filler():
    """A listing that only repeats its channel's name: same title, no sub-title, no description. Some
    guide sources fill every hour of an event channel this way, in 2-hour blocks, so the block at the
    game's start read as its live airing and the blocks after it as replays (dev/changelog/1470). It
    says no more than the name does, so such a channel is read by its name (6.8)."""
    return and_(db.func.lower(db.func.trim(EPGEntry.title)) == db.func.lower(db.func.trim(Channel.name)),
                db.func.coalesce(db.func.trim(EPGEntry.sub_title), '') == '',
                db.func.coalesce(db.func.trim(EPGEntry.description), '') == '')


def _offered():
    """A channel the user is offered: never a hidden one. Hiding decides where a channel is offered,
    and a match is an offer to record it (DESIGN-channel-hiding.md §1). A hidden channel carries no
    guide rows, so without this it read as an event channel with nothing but its name: 33 of the 85
    event channels on prod's accounts for the week of 2026-11-08 were hidden (dev/changelog/1471).

    Written as `hidden + 0` so SQLite cannot use ix_channels_standing for it. Nearly every channel is
    visible, so that index narrows nothing, and with no table statistics the planner took it anyway:
    it walked all ~200,000 visible channels and probed the matches for each, where starting from the
    matches for a page's events takes under a millisecond (dev/changelog/1495)."""
    return (db.type_coerce(Channel.hidden, db.Integer) + 0).is_(0)


def _candidate_rows(lt: LeagueText, lo: datetime, hi: datetime) -> list:
    """The league's rows in [lo, hi): any whose title or sub-title carries one of its terms, or on a
    channel in one of its categories, never a filler row and never on a hidden channel. LIKE only
    narrows; the whole-word check is in Python. The start_time index bounds the scan."""
    conds = []
    for needle in lt.needles:
        conds += [EPGEntry.title.ilike(f'%{needle}%'), EPGEntry.sub_title.ilike(f'%{needle}%')]
    if lt.categories:
        conds.append(Channel.category_name.in_(list(lt.categories)))
    if not conds:
        return []
    q = (db.session.query(EPGEntry.channel_id, Channel.category_name, EPGEntry.title, EPGEntry.sub_title,
                          EPGEntry.start_time, EPGEntry.stop_time, EPGEntry.is_live)
         .join(Channel, Channel.id == EPGEntry.channel_id)
         .filter(EPGEntry.start_time >= lo, EPGEntry.start_time < hi, or_(*conds), ~_filler(), _offered()))
    out = []
    for channel_id, category, title, sub_title, start, stop, is_live in q.all():
        row = Row(channel_id, category, title, sub_title, start, stop, bool(is_live) or _marked_live(title))
        row.text = normalize(f'{title} {sub_title or ""}')
        out.append(row)
    return out


def _marked_live(title: str | None) -> bool:
    """A live mark read before strip_marks takes it off (6.2): the `ᴸᶦᵛᵉ` suffix or a `Live:` prefix."""
    raw = title or ''
    return 'ᴸᶦᵛᵉ' in raw or raw.lstrip().lower().startswith('live:')


# ---------------------------------------------------------------------------
# Matchups (6.3)
# ---------------------------------------------------------------------------

@dataclass
class Found:
    row: Row
    strength: str
    reason: str
    airing: str = SPORTS_AIRING_UNKNOWN
    #: Kept by _airing: the strong rows decide what a group's first airing is.
    evidence: str = ''
    #: A person marked it a wrong match: still stored, so it is never marked gone, but it takes no
    #: part in the guide's start, the first airing or the trust measurement.
    rejected: bool = False


def _identify_matchups(lt: LeagueText, events: list, rows: list) -> dict:
    """{event id: [(row, league evidence, home, away)]}: the rows that are each game, before strength.
    A row with two candidate games of the same pair goes to the nearer (NHL home-and-home series)."""
    pairs = defaultdict(list)
    for e in events:
        pairs[frozenset((e.home_team_id, e.away_team_id))].append(e)
    out = defaultdict(list)
    for row in rows:
        if row.minutes < MIN_GAME_MINUTES or _NOT_A_GAME.search(row.text):
            continue
        league = lt.evidence(row.text, row.category)
        if league is None:
            continue
        for pair, candidates in pairs.items():
            home_id, away_id = tuple(pair) if len(pair) == 2 else (next(iter(pair)),) * 2
            home, away = lt.teams.get(home_id), lt.teams.get(away_id)
            if home is None or away is None:
                continue
            got_home, got_away = _team_in(row.text, home), _team_in(row.text, away)
            if got_home is None or got_away is None:
                continue
            near = [e for e in candidates if abs(row.start - _anchor(e)) <= WINDOW]
            if not near:
                continue
            event = min(near, key=lambda e: abs(row.start - _anchor(e)))
            if event.home_team_id != home_id:
                got_home, got_away = got_away, got_home
            out[event.id].append((row, league, got_home, got_away))
    return out


def _anchor(event) -> datetime:
    """TheSportsDB's start, or the middle of its local date when it gave none."""
    if event.start_at is not None:
        return event.start_at
    return datetime.combine(event.local_date, time(12))


def _guide_start(rows: list, event) -> tuple:
    """(start, channels): the start the most channels list. A tie goes to the one nearest
    TheSportsDB's time. Placeholders listed every 2 hours on one channel never outvote a game."""
    by_start = defaultdict(set)
    for row in rows:
        by_start[row.start].add(row.channel_id)
    if not by_start:
        return None, 0
    anchor = _anchor(event)
    best = max(by_start, key=lambda s: (len(by_start[s]), -abs((s - anchor).total_seconds())))
    return best, len(by_start[best])


def _settled_start(event, computed, channels: int, now: datetime) -> tuple:
    """A guide start that has already aired is kept rather than measured again: an import drops
    listings that have aired, so the rows left an hour after a night game are its replays, and
    reading the start from them would call the game's time wrong and spend the league's trust."""
    if event.guide_start is not None and event.guide_start + OFF_LIMIT < now:
        return event.guide_start, event.guide_channels or 0
    return computed, channels


def _anchor_key(event_id: int, row) -> tuple:
    return event_id, row.channel_id, row.start, title_key(row.title)


def _judge_matchup(event, lt: LeagueText, hits: list, trusted: bool, now: datetime, clock, rejected=frozenset()) -> tuple:
    """([Found], guide start, channels) for one game. `clock` writes a time for a reason sentence;
    `rejected` holds the anchors a person marked a wrong match, which are left out of the guide's start."""
    counted_rows = [h[0] for h in hits if _anchor_key(event.id, h[0]) not in rejected]
    guide_start, guide_channels = _settled_start(event, *_guide_start(counted_rows, event), now)
    disagrees = (event.start_at is not None and guide_start is not None
                 and abs(guide_start - event.start_at) > DISAGREE)
    ref = guide_start if (disagrees or event.start_at is None) else event.start_at
    checked = trusted and not disagrees and event.start_at is not None
    found = []
    for row, league, home, away in hits:
        delta = row.start - ref
        if delta < -EARLY_LIMIT:
            continue
        names = home == 'name' and away == 'name'
        if league == 'category' or names:
            strength = SPORTS_MATCH_STRONG
            evidence = 'both teams by name' if names else 'both teams'
        else:
            strength = SPORTS_MATCH_WEAK
            evidence = 'the teams by city' if home == away == 'city' else 'one team by name and one by city'
        where = 'the league named in the listing' if league == 'text' else f'its category, {row.category}'
        if checked and AGREE < abs(delta) <= OFF_LIMIT:
            strength = SPORTS_MATCH_WEAK
            when = f'starting {_minutes(abs(delta))} {"early" if delta < timedelta(0) else "late"}'
        elif checked:
            when = 'at the listed time' if abs(delta) <= AGREE else 'later the same day'
        elif disagrees:
            when = (f'at {clock(guide_start)}, the time most channels list. TheSportsDB lists '
                    f'{clock(event.start_at)}')
        else:
            when = "near the listed time, which is not checked for this league yet"
        found.append(Found(row, strength, f'Matched on {where}, {evidence}, {when}.', evidence=evidence,
                           rejected=_anchor_key(event.id, row) in rejected))
    _airing(found, lt, ref, checked, floor=guide_start)
    return found, guide_start, guide_channels


def _minutes(delta: timedelta) -> str:
    return counted(int(delta.total_seconds() // 60), 'minute')


def _airing(found: list, lt: LeagueText, ref: datetime, checked: bool, floor: datetime = None):
    """first, replay or unknown (6.5). Doubt is unknown, and unknown is never skipped. With a time
    that can be believed, a row more than 90 minutes after it is a replay. Without one, the earliest
    strong row of each title and episode is the first airing, and so is a row marked live or within
    90 minutes of it. `floor` is the event's guide start, so once the live rows have aired out of
    the guide its replays are still measured from the game, not from each other."""
    groups = defaultdict(list)
    for f in found:
        groups[f.row.keys].append(f)
    for group in groups.values():
        strong = [f.row.start for f in group if f.strength == SPORTS_MATCH_STRONG and not f.rejected]
        earliest = min(strong + ([floor] if floor and strong else [])) if strong else None
        for f in group:
            if lt.categories.get(f.row.category):
                f.airing = SPORTS_AIRING_REPLAY
            elif checked:
                f.airing = SPORTS_AIRING_REPLAY if f.row.start - ref > OFF_LIMIT else SPORTS_AIRING_FIRST
            elif earliest is None:
                f.airing = SPORTS_AIRING_UNKNOWN
            elif f.row.is_live or f.row.start - earliest <= OFF_LIMIT:
                f.airing = SPORTS_AIRING_FIRST
            else:
                f.airing = SPORTS_AIRING_REPLAY


# ---------------------------------------------------------------------------
# Field events (6.4)
# ---------------------------------------------------------------------------

def _band(event) -> tuple:
    day = datetime.combine(event.local_date, time())
    return day - BAND_MARGIN, day + timedelta(days=1) + BAND_MARGIN


def _race_words(text: str, lt: LeagueText) -> set:
    for term in lt.terms:
        text = f' {text} '.replace(f' {term} ', ' ')
    return {w for w in text.split() if w not in _RACE_FILLER}


def _judge_field_events(lt: LeagueText, events: list, rows: list, trusted: bool, now: datetime,
                        rejected=frozenset()) -> dict:
    """{event id: ([Found], guide start, channels)} for a league's races."""
    hits = defaultdict(list)
    for row in rows:
        if row.minutes < MIN_RACE_MINUTES or _NOT_A_RACE.search(row.text):
            continue
        league = lt.evidence(row.text, row.category)
        if league is None:
            continue
        in_band = [e for e in events if _band(e)[0] <= row.start < _band(e)[1]]
        if not in_band:
            continue
        words = _race_words(row.text, lt)
        agreeing = [e for e in in_band if (core := _race_words(normalize(e.name), lt)) and core <= words]
        if agreeing:
            event = min(agreeing, key=lambda e: abs(row.start - _anchor(e)))
            hits[event.id].append((row, SPORTS_MATCH_STRONG,
                                   'Matched on the series and the date, and the listing names this race.'))
        elif len(in_band) == 1:
            event = in_band[0]
            if not words:
                hits[event.id].append((row, SPORTS_MATCH_STRONG,
                                       'Matched on the series and the date. The listing names no race, and this '
                                       'is the only one that day.'))
            else:
                hits[event.id].append((row, SPORTS_MATCH_WEAK,
                                       f'Matched on the series and the date, but the guide calls it '
                                       f'"{strip_marks(row.sub_title or row.title)}" and TheSportsDB calls it '
                                       f'"{event.name}".'))
    out = {}
    by_id = {e.id: e for e in events}
    for event_id, items in hits.items():
        event = by_id[event_id]
        counted_items = [i for i in items if _anchor_key(event_id, i[0]) not in rejected] or items
        seen = min(r.start for r, _, _ in counted_items)
        earliest, channels = _settled_start(
            event, seen, len({r.channel_id for r, _, _ in counted_items if r.start == seen}), now)
        found = [Found(r, s, why, rejected=_anchor_key(event_id, r) in rejected)
                 for r, s, why in items if r.start - earliest <= WINDOW]
        disagrees = event.start_at is not None and abs(earliest - event.start_at) > DISAGREE
        checked = trusted and not disagrees and event.start_at is not None
        _airing(found, lt, event.start_at if checked else earliest, checked, floor=earliest)
        out[event_id] = (found, earliest, channels)
    return out


# ---------------------------------------------------------------------------
# Event channels: a channel with no real guide listing, whose name is the only one (6.8)
# ---------------------------------------------------------------------------

#: How far ahead a name may name a game. On a Wednesday night one account already named all of
#: Sunday's NFL games (dev/docs/sports-data-investigation.md).
NAME_AHEAD = timedelta(days=7)
#: A name's clock with a timezone agrees within this: 60 of 61 were within 45 minutes (6.8).
NAME_AGREE = timedelta(minutes=45)
#: A date written with no timezone is a day either way.
NAME_DAY_SLACK = timedelta(days=1)
#: A name that stops naming a game this long after its start has simply moved on to the next one;
#: before it, the game is marked gone from that channel.
NAME_OVER = timedelta(hours=6)
_NAME_PREFIX = "Read from the channel's name, not a guide listing"


@dataclass
class NamedChannel:
    channel_id: int
    category: str | None
    name: str
    text: str
    read: event_names.ReadName


@dataclass
class NameFound:
    channel: NamedChannel
    strength: str
    reason: str
    airing: str = SPORTS_AIRING_UNKNOWN


def _candidate_names(lt: LeagueText, now: datetime) -> list:
    """Channels not hidden, with no guide rows but filler, whose name carries one of the league's terms
    or that sit in one of its tied categories. A tied category supplies the league, so `US| NHL PPV`'s
    names need not say NHL. LIKE only narrows; the whole-word check is in Python."""
    conds = [Channel.name.ilike(f'%{needle}%') for needle in lt.needles]
    if lt.categories:
        conds.append(Channel.category_name.in_(list(lt.categories)))
    if not conds:
        return []
    listed = db.session.query(EPGEntry.id).filter(EPGEntry.channel_id == Channel.id, ~_filler()).exists()
    q = db.session.query(Channel.id, Channel.account_id, Channel.category_name, Channel.name, Channel.last_seen_at
                         ).filter(or_(*conds), ~listed, _offered())
    rows = q.all()
    # Only a name the account's latest sync still listed: a channel the provider dropped keeps its last
    # name forever, and that name would match the next game of the same pair. A sync stamps every
    # channel it saw with one time, so the newest stamp on an account is its latest sync.
    accounts = {r[1] for r in rows}
    latest = dict(db.session.query(Channel.account_id, db.func.max(Channel.last_seen_at))
                  .filter(Channel.account_id.in_(accounts)).group_by(Channel.account_id).all()) if accounts else {}
    return [NamedChannel(cid, cat, name, normalize(name), event_names.read(name, now))
            for cid, account_id, cat, name, seen in rows
            if seen is None or latest.get(account_id) is None or seen >= latest[account_id]]


def _name_says(clock) -> str:
    return f' ({clock.text})' if clock.text else ''


def _match_name_matchups(lt: LeagueText, events: list, channels: list, trusted: bool, now: datetime,
                         clock) -> dict:
    """{event id: [NameFound]} for a league's games. A name naming more than one game is not
    matched: `NHL: MTL vs. TOR • NJ vs. NYI` is a schedule, not a slot."""
    pairs = defaultdict(list)
    for e in events:
        if e.home_team_id and e.away_team_id:
            pairs[(e.home_team_id, e.away_team_id) if e.home_team_id < e.away_team_id
                  else (e.away_team_id, e.home_team_id)].append(e)
    out = defaultdict(list)
    for ch in channels:
        if _NOT_A_GAME.search(ch.text):
            continue
        league = lt.evidence(ch.text, ch.category)
        if league is None:
            continue
        named = []
        folded = None
        for pair, candidates in pairs.items():
            a, b = lt.teams.get(pair[0]), lt.teams.get(pair[1])
            if a is None or b is None:
                continue
            got_a, got_b = _team_in(ch.text, a), _team_in(ch.text, b)
            if got_a and got_b:
                folded = folded if folded is not None else _fold(ch.name)
                if _faces_off(folded, a, b):
                    named.append((candidates, got_a, got_b))
        if len(named) != 1:
            continue
        candidates, got_a, got_b = named[0]
        picked = _pick_by_name(ch.read.clock, candidates, now)
        if picked is None:
            continue
        event, others = picked
        out[event.id].append(_judge_name(event, lt, ch, league, got_a, got_b, others, trusted, clock))
    return out


#: What stands between two teams in a name that is one game: `Bills @ Vikings`, `Buccaneers at Bears`,
#: `HARBORVIEW - IRONVALE`, `Falcons-Bengals`, `Bengals vs. Falcons`, `Flamengo x Palmeiras`.
_JOINER = re.compile(r"\s*(?:vs?\.?|versus|at|@|x|[-–—])\s*")


def _fold(name: str) -> str:
    """A channel name casefolded with accents and marks off, its punctuation kept, since the joiner
    between two teams is often only an `@` or a `-`, which normalize() turns to a space."""
    text = strip_marks(name).translate(_APOSTROPHES)
    return ''.join(ch for ch in unicodedata.normalize('NFKD', text) if not unicodedata.combining(ch)).casefold()


def _spans(folded: str, team: 'TeamText') -> list:
    """(start, end) of every whole-word mention of the team's names and city in a folded name."""
    out = []
    for phrase in (*team.names, team.city):
        if phrase:
            words = r'\W+'.join(re.escape(w) for w in phrase.split())
            out += [(m.start(), m.end()) for m in re.finditer(rf"(?<![\w']){words}(?![\w'])", folded)]
    return out


def _faces_off(folded: str, a: 'TeamText', b: 'TeamText') -> bool:
    """The two teams sit side by side with only a joiner between them. A provider's team channels name
    their market's teams with none (`NFL TEAMS| CBS BILLS GIANTS JETS NEW YORK NY`, `... CHIEFS BEARS ST.
    LOUIS MO`), and with only filler in their guide they are read by name (dev/changelog/1473)."""
    sa, sb = _spans(folded, a), _spans(folded, b)
    return any(_JOINER.fullmatch(folded[first[1]:second[0]])
               for x, y in ((sa, sb), (sb, sa)) for first in x for second in y if first[1] <= second[0])


def _pick_by_name(name_clock, candidates: list, now: datetime):
    """(event, how many games were in range) for the games of one pair, by what the name's clock
    allows, or None when nothing is in range. With no date, two in range go to the nearer."""
    if name_clock.level == event_names.LEVEL_TZ:
        near = [e for e in candidates if abs(name_clock.at - _anchor(e)) <= WINDOW]
        key = (lambda e: abs(name_clock.at - _anchor(e)))
    elif name_clock.level == event_names.LEVEL_DATE:
        # A date with no timezone is a day either way (`Nov 8 01:00 :Telia FI` is a North American
        # Nov 7 evening), so two games of the pair within it cannot be told apart and neither is picked.
        near = [e for e in candidates if abs(_event_day(e) - name_clock.day) <= NAME_DAY_SLACK]
        return (near[0], 1) if len(near) == 1 else None
    elif name_clock.level == event_names.LEVEL_NONE:
        near = [e for e in candidates if now - LOOKBACK <= _anchor(e) <= now + NAME_AHEAD]
        key = (lambda e: abs(_anchor(e) - now))
    else:
        raise ValueError(f'a name clock level nothing handles: {name_clock.level!r}')
    if not near:
        return None
    return min(near, key=key), len(near)


def _event_day(event):
    return event.local_date or _anchor(event).date()


def _judge_name(event, lt: LeagueText, ch: NamedChannel, league: str, got_a: str, got_b: str, in_range: int, trusted: bool,
                clock) -> NameFound:
    names = got_a == 'name' and got_b == 'name'
    if league == 'category' or names:
        strength, evidence = SPORTS_MATCH_STRONG, ('both teams by name' if names else 'both teams')
    else:
        strength = SPORTS_MATCH_WEAK
        evidence = 'the teams by city' if got_a == got_b == 'city' else 'one team by name and one by city'
    where = 'the league named in it' if league == 'text' else f'its category, {ch.category}'
    when, strength, agrees = _name_when(event, ch.read.clock, strength, in_range, trusted, clock)
    found = NameFound(ch, strength, f'{_NAME_PREFIX}: matched on {where}, {evidence}, {when}.')
    found.airing = _name_airing(lt.categories.get(ch.category), ch, agrees)
    return found


def _name_when(event, name_clock, strength: str, in_range: int, trusted: bool, clock) -> tuple:
    """(the reason's time clause, strength, whether the name's time agrees with the event's)."""
    disagrees = time_disagrees(event)
    ref = event.guide_start if disagrees else event.start_at
    checked = trusted and not disagrees and event.start_at is not None
    level = name_clock.level
    if level == event_names.LEVEL_TZ:
        said = f'the time in its name{_name_says(name_clock)}'
        if ref is None:
            return f'{said}, with no start from TheSportsDB to check it against', strength, False
        delta = name_clock.at - ref
        if abs(delta) <= NAME_AGREE:
            return said, strength, True
        off = f'{_minutes(abs(delta))} {"before" if delta < timedelta(0) else "after"} '
        if checked:
            return f'but {said} is {off}the listed start', SPORTS_MATCH_WEAK, False
        if disagrees:
            return f'and {said} is {off}the time most channels list', strength, False
        return f'and {said} is {off}the listed start, which is not checked for this league yet', strength, False
    if level == event_names.LEVEL_DATE:
        return 'on the date in its name, which gives no timezone', strength, False
    if level == event_names.LEVEL_NONE:
        if in_range > 1:
            return (f'but its name gives no date and {in_range} games between these teams are within a week, so '
                    f'this is the nearest', SPORTS_MATCH_WEAK, False)
        return 'and its name gives no date, so this is the only game between these teams within a week', strength, False
    raise ValueError(f'a name clock level nothing handles: {level!r}')


def _name_airing(replay_category, ch: NamedChannel, agrees: bool) -> str:
    """first, replay or unknown for a channel name. Doubt is unknown, and unknown is never skipped."""
    if replay_category:
        return SPORTS_AIRING_REPLAY
    if ch.read.status in (event_names.STATUS_LIVE, event_names.STATUS_NEXT) or agrees:
        return SPORTS_AIRING_FIRST
    return SPORTS_AIRING_UNKNOWN


def _match_name_races(lt: LeagueText, events: list, channels: list) -> dict:
    """{event id: [NameFound]} for a league's field events. A race is matched on its series and the
    date in the name, so a name with no date is never matched to one: a series races most weeks."""
    out = defaultdict(list)
    for ch in channels:
        level = ch.read.clock.level
        if level == event_names.LEVEL_NONE or _NOT_A_RACE.search(ch.text):
            continue
        if lt.evidence(ch.text, ch.category) is None:
            continue
        if level == event_names.LEVEL_TZ:
            in_band = [e for e in events if _band(e)[0] <= ch.read.clock.at < _band(e)[1]]
        else:
            in_band = [e for e in events if abs(_event_day(e) - ch.read.clock.day) <= NAME_DAY_SLACK]
        if not in_band:
            continue
        # A name always carries other words (`:MAX US 04`), so unlike a guide row its words cannot
        # say it names no race; only the event's own name can say there is nothing to compare.
        words = _race_words(ch.text, lt)
        cores = {e.id: _race_words(normalize(e.name), lt) for e in in_band}
        agreeing = [e for e in in_band if cores[e.id] and cores[e.id] <= words]
        if agreeing:
            event = min(agreeing, key=lambda e: abs(_event_day(e) - ch.read.clock.day))
            strength, why = SPORTS_MATCH_STRONG, 'matched on the series and the date in it, and it names this race'
        elif len(in_band) == 1 and not cores[in_band[0].id]:
            event, strength = in_band[0], SPORTS_MATCH_STRONG
            why = 'matched on the series and the date in it, the only race of the series that day'
        elif len(in_band) == 1:
            event, strength = in_band[0], SPORTS_MATCH_WEAK
            why = f'matched on the series and the date in it, but TheSportsDB calls the race "{event.name}"'
        else:
            continue
        found = NameFound(ch, strength, f'{_NAME_PREFIX}: {why}.')
        found.airing = _name_airing(lt.categories.get(ch.category), ch, False)
        out[event.id].append(found)
    return out


# ---------------------------------------------------------------------------
# Storing
# ---------------------------------------------------------------------------

@retry_on_locked()
def _store_event(event_id: int, found: list, guide_start, guide_channels: int, now: datetime) -> tuple:
    """One event's matches in one commit: found rows updated or added, rows no longer found marked
    gone (never deleted, so a provider blip does not lose a recording's reason), and rejected rows
    kept rejected. Returns (added, gone). The anchor is UNIQUE by the partial index on the table, so
    keying the existing rows on it is safe."""
    event = db.session.get(SportsEvent, event_id)
    if event is None:
        db.session.rollback()
        return 0, 0
    existing = {(m.channel_id, m.program_start_time, m.title_key): m for m in SportsMatch.query.filter_by(
        event_id=event_id, kind=SPORTS_MATCH_KIND_GUIDE).all()}
    # The rows were read before this run's pauses, and a channel deleted since then must not get a
    # match: channel ids are re-issued, so the next channel given its id would inherit it (9). One
    # hidden since then is not offered either.
    wanted = {f.row.channel_id for f in found}
    alive = ({i for (i,) in db.session.query(Channel.id).filter(Channel.id.in_(wanted), _offered())}
             if wanted else set())
    seen, added = set(), 0
    for f in found:
        if f.row.channel_id not in alive:
            continue
        key = (f.row.channel_id, f.row.start, title_key(f.row.title))
        if key in seen:
            continue
        seen.add(key)
        match = existing.get(key)
        if match is None:
            match = SportsMatch(event_id=event_id, channel_id=f.row.channel_id, kind=SPORTS_MATCH_KIND_GUIDE,
                                program_start_time=f.row.start, title_key=key[2], first_seen=now)
            db.session.add(match)
            added += 1
        match.program_stop_time = f.row.stop
        match.episode_key = title_key(f.row.sub_title)
        match.program_title = strip_marks(f.row.title)[:512]
        match.program_sub_title = strip_marks(f.row.sub_title)[:512] or None
        match.strength, match.airing, match.reason = f.strength, f.airing, f.reason
        match.last_seen = now
        match.gone_since = None
    gone = 0
    for key, match in existing.items():
        # Only a row the guide should still hold is gone; a past one simply aged out of the guide.
        if key not in seen and match.gone_since is None and (match.program_stop_time or now) > now:
            match.gone_since = now
            gone += 1
    if guide_start is not None:
        event.guide_start, event.guide_channels = guide_start, guide_channels
    db.session.commit()
    return added, gone


@retry_on_locked()
def _store_names(event_ids: list, found: dict, now: datetime) -> tuple:
    """A league's channel-name matches in one commit, for the events a name could still name: found
    rows updated or added by (event, channel), so a rename moves the same row and a rejection
    survives it; a row no longer found is marked gone while its game is still to come, and simply
    left once the game is over and the name has moved on. Returns (added, gone). The anchor is
    UNIQUE by the partial index on the table, so keying the existing rows on it is safe."""
    if not event_ids:
        db.session.rollback()
        return 0, 0
    events = {e.id: e for e in SportsEvent.query.filter(SportsEvent.id.in_(event_ids)).all()}
    existing = {(m.event_id, m.channel_id): m for m in SportsMatch.query.filter(
        SportsMatch.event_id.in_(event_ids), SportsMatch.kind == SPORTS_MATCH_KIND_CHANNEL_NAME).all()}
    wanted = {f.channel.channel_id for items in found.values() for f in items}
    # Read before this run's pauses: a channel deleted since must not get a match, since ids are
    # re-issued and the next channel given this one's id would inherit it (9). One hidden since is
    # not offered either.
    alive = ({i for (i,) in db.session.query(Channel.id).filter(Channel.id.in_(wanted), _offered())}
             if wanted else set())
    seen, added = set(), 0
    for event_id, items in found.items():
        if event_id not in events:
            continue
        for f in items:
            key = (event_id, f.channel.channel_id)
            if f.channel.channel_id not in alive or key in seen:
                continue
            seen.add(key)
            match = existing.get(key)
            if match is None:
                match = SportsMatch(event_id=event_id, channel_id=f.channel.channel_id,
                                    kind=SPORTS_MATCH_KIND_CHANNEL_NAME, first_seen=now)
                db.session.add(match)
                added += 1
            match.program_start_time = f.channel.read.clock.at
            match.program_stop_time = None
            match.title_key = title_key(f.channel.name)
            match.episode_key = ''
            match.program_title = strip_marks(f.channel.name)[:512]
            match.program_sub_title = None
            match.strength, match.airing, match.reason = f.strength, f.airing, f.reason
            match.last_seen = now
            match.gone_since = None
    gone = 0
    for key, match in existing.items():
        event = events.get(key[0])
        if key in seen or match.gone_since is not None or event is None:
            continue
        if _anchor(event) + NAME_OVER > now:
            match.gone_since = now
            gone += 1
    db.session.commit()
    return added, gone


@retry_on_locked()
def _store_trust(league_id: int, now: datetime):
    """Count events, not rows: the last 30 the guide identified, whether or not their time agreed,
    so a trusted league's wrong times are counted and it can lose its trust (7)."""
    league = db.session.get(SportsLeague, league_id)
    if league is None:
        db.session.rollback()
        return
    rows = (db.session.query(SportsEvent.start_at, SportsEvent.guide_start)
            .filter(SportsEvent.league_id == league_id, SportsEvent.guide_start.isnot(None))
            .order_by(SportsEvent.guide_start.desc()).limit(TRUST_KEEP).all())
    league.trust_sample = len(rows)
    league.trust_agreed = sum(1 for start, guide in rows if start is not None and abs(guide - start) <= AGREE)
    league.trust_measured_at = now
    db.session.commit()


@retry_on_locked()
def _store_note(note: str, now: datetime, ran: bool):
    state = db.session.get(SportsState, 1)
    if state is None:
        db.session.rollback()
        return
    state.match_note = note
    if ran:
        state.last_match_at = now
    db.session.commit()


# ---------------------------------------------------------------------------
# A run
# ---------------------------------------------------------------------------

def _events_in_range(league_id: int, lo: datetime, hi: datetime) -> list:
    return SportsEvent.query.filter(
        SportsEvent.league_id == league_id,
        or_(SportsEvent.start_at.between(lo, hi),
            (SportsEvent.start_at.is_(None) & SportsEvent.local_date.between(lo.date(), hi.date())))).all()


def match_league(league_id: int, now: datetime, pause: float = PAUSE_SECONDS, clock=None) -> dict:
    """Match one league's events that the guide can still hold, then the event channels whose names
    name one. Returns counts for the run's note. `clock` writes a time into a reason sentence, in the
    display timezone; run() builds it once."""
    clock = clock or _clock()
    empty = {'events': 0, 'matched': 0, 'rows': 0, 'named': 0, 'channels': 0}
    league = db.session.get(SportsLeague, league_id)
    if league is None:
        return empty
    # An empty guide still runs, so what it no longer lists is marked gone.
    guide_end = db.session.query(db.func.max(EPGEntry.start_time)).scalar() or now
    events = _events_in_range(league_id, now - LOOKBACK, max(guide_end, now) + WINDOW)
    name_events = _events_in_range(league_id, now - LOOKBACK, now + NAME_AHEAD)
    if not events and not name_events:
        return empty
    leagues = SportsLeague.query.all()
    lt = _league_text(league, [o for o in leagues if o.id != league.id], SportsLeagueCategory.query.all())
    _load_teams(lt, list({e.id: e for e in events + name_events}.values()))
    trusted = league_trusted(league)
    matchups = [e for e in events if e.home_team_id or e.away_team_id]
    fields = [e for e in events if not (e.home_team_id or e.away_team_id)]
    results = {}
    rows = []
    if events:
        spans = [(_anchor(e) - WINDOW, _anchor(e) + WINDOW) for e in matchups] + [_band(e) for e in fields]
        rows = _candidate_rows(lt, min(s[0] for s in spans), max(s[1] for s in spans))
        rejected = {(m.event_id, m.channel_id, m.program_start_time, m.title_key) for m in SportsMatch.query.filter(
            SportsMatch.event_id.in_([e.id for e in events]), SportsMatch.kind == SPORTS_MATCH_KIND_GUIDE,
            SportsMatch.rejected_at.isnot(None)).all()}
        hits = _identify_matchups(lt, matchups, rows)
        for event in matchups:
            results[event.id] = _judge_matchup(event, lt, hits.get(event.id, []), trusted, now, clock, rejected)
        field_results = _judge_field_events(lt, fields, rows, trusted, now, rejected)
        for event in fields:
            results[event.id] = field_results.get(event.id, ([], None, 0))
    # A name is judged against the guide start already stored, so an event whose time disagrees is
    # at worst one run behind.
    named = {}
    if name_events:
        channels = _candidate_names(lt, now)
        named = _match_name_matchups(lt, [e for e in name_events if e.home_team_id or e.away_team_id],
                                     channels, trusted, now, clock)
        named.update(_match_name_races(lt, [e for e in name_events if not (e.home_team_id or e.away_team_id)],
                                       channels))
    db.session.rollback()   # no read transaction held across the writes and pauses below

    matched = rows_matched = 0
    for event_id, (found, guide_start, channels_n) in results.items():
        _store_event(event_id, found, guide_start, channels_n, now)
        matched += bool(found)
        rows_matched += len(found)
        if pause:
            time_mod.sleep(pause)
    _store_names([e.id for e in name_events], named, now)
    _store_trust(league_id, now)
    return {'events': len(events), 'matched': matched, 'rows': rows_matched,
            'named': sum(1 for items in named.values() if items),
            'channels': len({f.channel.channel_id for items in named.values() for f in items})}


def run(now: datetime = None, pause: float = PAUSE_SECONDS) -> str:
    """One matching run over every league with events. Returns the note it stored."""
    from .scheduler import queue_sports_match
    now = now or datetime.utcnow()
    ticket = admission.try_start(admission.KIND_SPORTS_MATCH, 'sports schedules')
    if not ticket.granted:
        note = f'Waiting: {ticket.reason}. Tries again in {RETRY_MINUTES} minutes.'
        queue_sports_match(RETRY_MINUTES * 60, 'its last run was refused')
        _store_note(note, now, ran=False)
        log.info('Sports match deferred: %s', ticket.reason)
        return note
    started = time_mod.monotonic()
    try:
        clock = _clock()
        league_ids = [i for (i,) in db.session.query(SportsEvent.league_id).distinct().all()]
        db.session.rollback()
        totals = Counter()
        for league_id in league_ids:
            totals.update(match_league(league_id, now, pause, clock))
        from .sports_data import link_unfinished_recordings
        linked = link_unfinished_recordings(now)
        _queue_rule_checks(league_ids)
        seconds = time_mod.monotonic() - started
        note = (f'Found {counted(totals["matched"], "event")} of {totals["events"]} in the guide, on '
                f'{counted(totals["rows"], "listing")}, in {seconds:.1f} seconds.')
        if totals['channels']:
            note += (f' {counted(totals["named"], "event")} named by {counted(totals["channels"], "event channel")}, '
                     f'read from their names.')
        if linked:
            note += f' Linked {counted(linked, "recording")} to its event.'
        _store_note(note, now, ran=True)
        log.info('Sports match: %s', note)
        return note
    except Exception as exc:  # re-raised: this only keeps the page from showing the last good run
        db.session.rollback()
        log.exception('Sports match stopped')
        _store_note(f'The last run stopped with an error ({type(exc).__name__}). Matches from the run before '
                    f'it are kept, and the next sync, guide import or schedule update tries again.', now, ran=False)
        raise
    finally:
        admission.release(ticket)


def _queue_rule_checks(league_ids: list):
    """A rule that names a league judges its listings by these matches (design 10), and the guide
    import's own check can run before this matcher has caught up with it. So a run over a league an
    enabled rule names asks for one more check, which several asks fold into (dev/changelog/1490)."""
    from .database import AutoRule
    if not league_ids:
        return
    named = db.session.query(AutoRule.id).filter(AutoRule.enabled.is_(True),
                                                 AutoRule.sports_league_id.in_(league_ids)).first()
    db.session.rollback()
    if named is not None:
        from .auto_rules import queue_evaluation
        queue_evaluation('TheSportsDB matches were updated')


def _clock():
    tz, h24 = get_display_tz(), is_24h()
    return lambda dt: format_local(dt, 'clock', tz=tz, h24=h24)


def run_needed() -> bool:
    """Anything to match: an event is stored. Read by the import hook before it queues a run."""
    return db.session.query(SportsEvent.id).first() is not None


# ---------------------------------------------------------------------------
# What the league page reads and a person writes (design 5.1, 5.2, 11)
# ---------------------------------------------------------------------------

def _live_matches():
    """The guide matches that count: still listed, and not marked a wrong match."""
    return SportsMatch.query.filter(SportsMatch.kind == SPORTS_MATCH_KIND_GUIDE, SportsMatch.gone_since.is_(None),
                                    SportsMatch.rejected_at.is_(None))


def game_counts(event_ids: list) -> dict:
    """{event id: {channels, first, replay, unknown, wrong}} in one grouped query, over the listings
    still in the guide on channels not hidden. `channels` and the airings count only listings not
    marked a wrong match; `wrong` counts those that are, so a game whose every listing was marked
    still says so. A hide takes effect here at once: the matcher runs only after a sync, a guide
    import or a schedule update, and hiding is none of them."""
    if not event_ids:
        return {}
    ok = SportsMatch.rejected_at.is_(None)

    def n(airing):
        return db.func.sum(db.case(((ok & (SportsMatch.airing == airing)), 1), else_=0))
    rows = (db.session.query(
        SportsMatch.event_id, db.func.count(db.distinct(db.case((ok, SportsMatch.channel_id), else_=None))),
        n(SPORTS_AIRING_FIRST), n(SPORTS_AIRING_REPLAY), n(SPORTS_AIRING_UNKNOWN),
        db.func.sum(db.case((ok, 0), else_=1)))
        .join(Channel, Channel.id == SportsMatch.channel_id)
        .filter(SportsMatch.event_id.in_(event_ids), SportsMatch.kind == SPORTS_MATCH_KIND_GUIDE,
                SportsMatch.gone_since.is_(None), _offered())
        .group_by(SportsMatch.event_id).all())
    return {event_id: {'channels': channels, 'first': int(first or 0), 'replay': int(replay or 0),
                       'unknown': int(unknown or 0), 'wrong': int(wrong or 0)}
            for event_id, channels, first, replay, unknown, wrong in rows}


#: What the listings dialog calls each airing. Every value of the vocabulary is named here.
AIRING_LABELS = {SPORTS_AIRING_FIRST: 'Live', SPORTS_AIRING_REPLAY: 'Replay', SPORTS_AIRING_UNKNOWN: 'Not known'}
#: The badge an event channel's row carries for its airing. An unknown airing gets none: a name that
#: says nothing about it is the common case, and a badge on most rows saying so is noise.
NAME_AIRING_BADGES = {SPORTS_AIRING_FIRST: ('b-done', 'Live'), SPORTS_AIRING_REPLAY: ('b-abort', 'Replay'),
                      SPORTS_AIRING_UNKNOWN: None}


def name_counts(event_ids: list) -> dict:
    """{event id: {channels, wrong}} for the event channels naming each event, in one grouped query.
    `channels` leaves out the ones marked a wrong match and `wrong` counts them, as game_counts() does.
    Kept apart from game_counts(), which every guide reader shares: a name match has no listing."""
    if not event_ids:
        return {}
    ok = SportsMatch.rejected_at.is_(None)
    rows = (db.session.query(SportsMatch.event_id, db.func.sum(db.case((ok, 1), else_=0)),
                             db.func.sum(db.case((ok, 0), else_=1)))
            .join(Channel, Channel.id == SportsMatch.channel_id)
            .filter(SportsMatch.event_id.in_(event_ids), SportsMatch.kind == SPORTS_MATCH_KIND_CHANNEL_NAME,
                    SportsMatch.gone_since.is_(None), _offered())
            .group_by(SportsMatch.event_id).all())
    return {event_id: {'channels': int(kept or 0), 'wrong': int(wrong or 0)} for event_id, kept, wrong in rows}


def _name_why(reason: str) -> str:
    """The stored reason without its opening "Read from the channel's name..." clause: the dialog says
    that once, above the list. The stored reason keeps it, since other surfaces show it alone."""
    lead = f'{_NAME_PREFIX}: '
    rest = (reason or '')[len(lead):] if (reason or '').startswith(lead) else (reason or '')
    return rest[:1].upper() + rest[1:]


def name_rows(event_id: int) -> list:
    """The event channels naming one event, as the listings dialog lists them: still named, on a channel
    not hidden, strong before weak, then by name, wrong matches included and flagged. One query,
    whatever the size."""
    rows = (db.session.query(SportsMatch, Channel.name, Channel.category_name)
            .join(Channel, Channel.id == SportsMatch.channel_id)
            .filter(SportsMatch.event_id == event_id, SportsMatch.kind == SPORTS_MATCH_KIND_CHANNEL_NAME,
                    SportsMatch.gone_since.is_(None), _offered())
            .order_by(db.case((SportsMatch.strength == SPORTS_MATCH_WEAK, 1), else_=0), Channel.name).all())
    return [{'id': m.id, 'channel_id': m.channel_id, 'name': name, 'category': category or '',
             'why': _name_why(m.reason), 'badge': NAME_AIRING_BADGES[m.airing], 'wrong': m.rejected_at is not None}
            for m, name, category in rows]


def listing_groups(event_id: int, tz, h24: bool) -> list:
    """An event's guide matches as the listings dialog shows them: one group per way a listing is
    worded and aired, each with its channels, wrong matches included and flagged. Rows gone from the
    guide and rows on a hidden channel are left out. Two queries, whatever the size."""
    rows = (db.session.query(SportsMatch, Channel.name, Channel.category_name)
            .join(Channel, Channel.id == SportsMatch.channel_id)
            .filter(SportsMatch.event_id == event_id, SportsMatch.kind == SPORTS_MATCH_KIND_GUIDE,
                    SportsMatch.gone_since.is_(None), _offered())
            .order_by(SportsMatch.program_start_time, Channel.name).all())
    groups = {}
    for m, name, category in rows:
        key = (m.program_title, m.program_sub_title, m.airing, m.program_start_time)
        g = groups.get(key)
        if g is None:
            g = groups[key] = {'title': m.program_title or '', 'sub': m.program_sub_title or '', 'airing': m.airing,
                               'airing_label': AIRING_LABELS[m.airing],
                               'start': m.program_start_time,
                               'when': format_local(m.program_start_time, 'clock', tz=tz, h24=h24),
                               'reason': m.reason, 'strength': m.strength, 'channels': []}
        if m.strength == SPORTS_MATCH_WEAK:
            g['strength'] = SPORTS_MATCH_WEAK
        g['channels'].append({'id': m.id, 'channel_id': m.channel_id, 'name': name, 'category': category or '',
                              'wrong': m.rejected_at is not None})
    out = sorted(groups.values(), key=lambda g: (-len(g['channels']), g['start']))
    for g in out:
        g['start'] = g['start'].isoformat()
        g['all_wrong'] = all(c['wrong'] for c in g['channels'])
    return out


def mark_wrong(event_id: int, match_ids: list, wrong: bool):
    """A person says these listings are not this event, or takes that back, then the event is matched
    again: a wrong match takes no part in the guide's start, the first airing or the trust measurement,
    so those can change. Returns how many rows changed, or None when the event is gone."""
    from .scheduler import queue_sports_match
    changed = _write_wrong(event_id, match_ids, wrong)
    if changed:
        queue_sports_match(reason='a listing was marked a wrong match' if wrong else 'a wrong match was undone')
    return changed


@retry_on_locked()
def _write_wrong(event_id: int, match_ids: list, wrong: bool):
    """Only rows of this event move. The matcher never writes this, and keeps it through every
    later run (design 9)."""
    if db.session.get(SportsEvent, event_id) is None:
        db.session.rollback()
        return None
    now = datetime.utcnow()
    changed = 0
    for m in SportsMatch.query.filter(SportsMatch.event_id == event_id, SportsMatch.id.in_(match_ids or [])).all():
        if wrong and m.rejected_at is None:
            m.rejected_at = now
            changed += 1
        elif not wrong and m.rejected_at is not None:
            m.rejected_at = None
            changed += 1
    db.session.commit()
    return changed


def clean_terms(terms) -> list:
    """A league's names as a person typed them: trimmed, blank and repeated ones (ignoring case)
    dropped. Raises ValueError for none, since a league with no name could never match."""
    out, seen = [], set()
    for t in terms or []:
        t = ' '.join(str(t).split())[:120]
        if t and normalize(t) and t.lower() not in seen:
            seen.add(t.lower())
            out.append(t)
    if not out:
        raise ValueError('A league needs at least one name, or no listing could ever match it.')
    return out


@retry_on_locked()
def _write_terms(league_id: int, terms: list) -> bool:
    league = db.session.get(SportsLeague, league_id)
    if league is None:
        db.session.rollback()
        return False
    league.match_terms = json.dumps(terms)
    db.session.commit()
    return True


def set_terms(league_id: int, terms) -> list:
    """Save a league's names and match again. Returns what was saved, or None when the league is gone."""
    from .scheduler import queue_sports_match
    cleaned = clean_terms(terms)
    if not _write_terms(league_id, cleaned):
        return None
    queue_sports_match(reason='a league\'s names changed')
    return cleaned


def suggested_terms(league) -> list:
    """Known names for this league it does not have yet (design 5.1)."""
    from .sports_data import KNOWN_EXTRA_TERMS
    have = {t.lower() for t in json.loads(league.match_terms or '[]')}
    return [t for t in KNOWN_EXTRA_TERMS.get(league.tsdb_id, ()) if t.lower() not in have]


def category_counts() -> dict:
    """{category name: channels} across every account, in one grouped query."""
    return dict(db.session.query(Channel.category_name, db.func.count(Channel.id))
                .filter(Channel.category_name.isnot(None), Channel.category_name != '')
                .group_by(Channel.category_name).all())


def category_offer(league) -> list:
    """Categories on the user's accounts whose name holds one of the league's names, as
    [{name, channels, tied}], most channels first. Only a person turns one on (design 5.2)."""
    terms = [normalize(t) for t in json.loads(league.match_terms or '[]') if normalize(t)]
    tied = {c.category_name for c in SportsLeagueCategory.query.filter_by(league_id=league.id).all()}
    out = [{'name': name, 'channels': n, 'tied': name in tied}
           for name, n in category_counts().items() if any(_has(normalize(name), t) for t in terms)]
    return sorted(out, key=lambda c: (-c['channels'], c['name']))


@retry_on_locked()
def _write_categories(league_id: int, add: list, remove: list, replay: dict) -> bool:
    if db.session.get(SportsLeague, league_id) is None:
        db.session.rollback()
        return False
    have = {c.category_name: c for c in SportsLeagueCategory.query.filter_by(league_id=league_id).all()}
    for name in add:
        if name not in have:
            have[name] = SportsLeagueCategory(league_id=league_id, category_name=name,
                                              is_replay=bool(re.search(r'\breplays?\b', name, re.I)))
            db.session.add(have[name])
    for name in remove:
        if name in have:
            db.session.delete(have.pop(name))
    for name, value in replay.items():
        if name in have:
            have[name].is_replay = bool(value)
    db.session.commit()
    return True


def change_categories(league_id: int, add=(), remove=(), replay=None) -> bool:
    """Tie categories to a league, untie them, or flip Replays only, then match again. A name is
    kept as typed, since the search treats a category by name across accounts. A new one whose name
    says replay starts with Replays only on; the person can turn it off."""
    from .scheduler import queue_sports_match
    # As given, spaces and all: it has to equal Channel.category_name, which a sync stores raw.
    add = [n[:255] for n in add if n.strip()]
    if not _write_categories(league_id, add, list(remove), dict(replay or {})):
        return False
    queue_sports_match(reason='a league\'s categories changed')
    return True
