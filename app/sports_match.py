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

from . import admission, db, event_names, sports_skips
from .database import (
    Account, Channel, EPGEntry, SPORTS_AIRING_FIRST, SPORTS_AIRING_REPLAY, SPORTS_AIRING_UNKNOWN,
    SPORTS_MATCH_KIND_CHANNEL_NAME, SPORTS_MATCH_KIND_GUIDE, SPORTS_MATCH_STRONG, SPORTS_MATCH_WEAK, SportsEvent, SportsLeague,
    SportsLeagueCategory, SportsMatch, SportsState, SportsTeam,
)
from .db_utils import chunked, retry_on_locked
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
#: A guide start further than this from TheSportsDB's start: its time disagrees. A start inside it
#: always wins over one outside it (_guide_start).
DISAGREE = timedelta(minutes=90)
#: A start that far off is believed only when it is marked live or listed on at least this many
#: channels: one unmarked listing hours away is an event channel's placeholder (dev/changelog/1620).
FAR_MIN_CHANNELS = 2
#: A guide start this close to TheSportsDB's previous start, and closer to it than to the new one,
#: is the guide still listing a game TheSportsDB has moved (dev/changelog/1623). Mavericks-Warriors
#: moved from 9:30 to 8:00 PM ET and ESPN went on listing it at 9:35.
BEHIND_NEAR = timedelta(minutes=30)
#: The furthest a game that has started can be moved and still be the same airing running late
#: (kept_start, dev/changelog/1632). The longest rain delays mid-event run a few hours, and a delayed
#: three-hour listing then still ends inside sports_feed.MAX_LENGTH, so a sports channel and a
#: recording agree on its end. A bigger move is a new date.
MAX_DELAY = timedelta(hours=6)
#: A field event's day, from its local date, wide enough for any timezone (6.4).
BAND_MARGIN = timedelta(hours=12)
#: How long an event is taken to last where nothing says, for reading a channel's name beside it
#: (_event_spans). Not the shortest listing a game or race may have, which is the league's own
#: (app/sports_skips.py, dev/changelog/1634).
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

#: Words that say nothing about which race a row is (6.4).
_RACE_FILLER = frozenset('live new race series cup championship the at from in on of and coverage motor '
                         'motorsport motorsports racing nascar'.split())

#: What a field event, or a listing of one, is: the race itself or one of the sessions TheSportsDB
#: lists as its own event on the race weekend (dev/changelog/1633). Every value is named where it is
#: read. Worked out from the name each time, never stored: it is the name's to say.
SESSION_RACE = 'race'
SESSION_SPRINT = 'sprint'
SESSION_PRACTICE = 'practice'
SESSION_QUALIFYING = 'qualifying'
SESSION_SPRINT_QUALIFYING = 'sprint_qualifying'
SESSION_SHOOTOUT = 'shootout'
SESSION_LABELS = {SESSION_RACE: 'race', SESSION_SPRINT: 'sprint', SESSION_PRACTICE: 'practice',
                  SESSION_QUALIFYING: 'qualifying', SESSION_SPRINT_QUALIFYING: 'sprint qualifying',
                  SESSION_SHOOTOUT: 'shootout'}
#: The kinds matched as sessions rather than races.
SESSIONS = frozenset({SESSION_SPRINT, SESSION_PRACTICE, SESSION_QUALIFYING, SESSION_SPRINT_QUALIFYING,
                      SESSION_SHOOTOUT})
#: The sessions a rule records only when it asks for them. A sprint is a race, and is recorded as one.
NOT_RACES = frozenset({SESSION_PRACTICE, SESSION_QUALIFYING, SESSION_SPRINT_QUALIFYING, SESSION_SHOOTOUT})
#: Read in this order: a sprint's qualifying is neither qualifying nor the sprint. `sprint cup` is
#: NASCAR's old series name, never a sprint.
_SESSION_KINDS = (
    (SESSION_SPRINT_QUALIFYING, re.compile(r'\bsprint (?:qualifying|qualification|quali|shootout)\b')),
    (SESSION_SHOOTOUT, re.compile(r'\bshootout\b')),
    (SESSION_QUALIFYING, re.compile(r'\b(?:qualifying|qualification|quali)\b')),
    (SESSION_PRACTICE, re.compile(r'\b(?:practice|fp ?[1-9])\b')),
    (SESSION_SPRINT, re.compile(r'\bsprint\b(?! cup)')),
)
_PRACTICE_NUMBER = re.compile(r'\b(?:practice|fp) ?([1-9])\b|\b([1-9])(?:st|nd|rd|th) (?:free )?practice\b')
#: Supercars' `Qualifying Race 35`, `Top 10 Shootout Race 36`.
_RACE_NUMBER = re.compile(r'\brace (\d+)\b')
#: Words a session's name and its listings share whichever session it is, read past when comparing
#: which race weekend they name.
_SESSION_FILLER = frozenset('practice free qualifying qualification quali shootout sprint top ten session'.split())
_SESSION_TOKEN = re.compile(r'^(?:\d+|\d+(?:st|nd|rd|th)|fp\d)$')
#: How the guide spells a word TheSportsDB writes out: `Las Vegas F1 GP` is the Las Vegas Grand Prix.
_RACE_SPELLINGS = {'gp': ('grand', 'prix')}


@dataclass(frozen=True)
class Session:
    kind: str
    #: Practice 2's 2, or the race a Supercars session is for; None when the text gives none.
    number: int | None = None


def session_of(text: str) -> Session:
    """The session normalized text names, SESSION_RACE when it names none."""
    for kind, pattern in _SESSION_KINDS:
        if pattern.search(text):
            found = (_PRACTICE_NUMBER if kind == SESSION_PRACTICE else _RACE_NUMBER).search(text)
            number = next((int(g) for g in found.groups() if g), None) if found else None
            return Session(kind, number)
    return Session(SESSION_RACE)


def event_session(event) -> Session:
    """What a stored event is. A game between two teams is never a session."""
    if event.home_team_id or event.away_team_id:
        return Session(SESSION_RACE)
    return session_of(normalize(event.name))
#: A listing of a game already played, which a lookup by name would ask about on the wrong day. Read
#: only where a lookup is queued: a replay of a stored game still matches it, as a replay (6.5).
_RERUN = re.compile(r'\b(replay|rewind|encore|classics?)\b')
_YEAR = re.compile(r'\b(19\d\d|20\d\d)\b')
#: One word each for the sports a provider's sports channels carry. A league's category says which
#: channels carry it, not that every listing on them is its game: prod's "College Football Package"
#: carries college basketball, volleyball and soccer too (dev/docs/BUGS.md 2026-11-14 @ 11:28:49 am).
_SPORT_WORDS = frozenset('basketball baseball softball volleyball hockey soccer football lacrosse rugby cricket '
                         'wrestling tennis golf gymnastics swimming'.split())
_NOT_WORD = re.compile(r"[^\w']+")
#: `&` is read as `and`, the way TheSportsDB spells `Texas A and M` and a guide `Texas A&M`.
_SPELLING = str.maketrans({'’': "'", '‘': "'", '`': "'", '&': ' and '})


def normalize(text: str | None) -> str:
    """Text as matching reads it: the providers' marks off, accents folded, casefolded, and anything
    but letters, digits and apostrophes turned to spaces, so `Dallas 🏈 [NFL] Cowboys` reads as
    `dallas nfl cowboys` and `Montréal` as `montreal`. Marks come off before folding, since folding
    turns `ᴸᶦᵛᵉ` into the word `live`."""
    text = strip_marks(text).translate(_SPELLING)
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
    #: The words of the league's own sport, read once from league.sport.
    own_sport: frozenset | None = None
    #: Every stored team's normalized names, this run's games or not: what tells a team's name from
    #: a piece of a longer one (_standing_spans).
    every_name: frozenset = frozenset()
    #: The words and lengths that keep a listing out (app/sports_skips.py), read once per run.
    skips: sports_skips.Skips = None
    _longer: dict = field(default_factory=dict)

    def longer_names(self, phrase: str) -> tuple:
        """The stored team names that hold `phrase` as whole words and more: `texas tech` and
        `north texas` for `texas`. Worked out once per phrase."""
        got = self._longer.get(phrase)
        if got is None:
            got = self._longer[phrase] = tuple(n for n in self.every_name if n != phrase and _has(n, phrase))
        return got

    def evidence(self, text: str, category: str | None) -> str | None:
        """'text' or 'category' for this league, or None. A row that names another league and not
        this one, or sits only in another league's category, is never this league's (6.3)."""
        if any(_has(text, t) for t in self.terms):
            return 'text'
        if any(_has(text, t) for t in self.other_terms):
            return None
        if category is not None and category in self.categories and not self.names_other_sport(text):
            return 'category'
        return None

    def name_evidence(self, text: str, category: str | None) -> str | None:
        """evidence(), or 'sport' for a channel name that names the league's own sport and no other
        sport or league: ESPN+'s `Football: Duke vs. #7 Miami`, Flo's `football: Maine vs UAlbany`.
        Read for a game's name only, where both teams must face off in it, and never for a race: a
        tour's `Golf: ...` names would all read as the one tournament of the day (dev/changelog/1562)."""
        found = self.evidence(text, category)
        if (found is not None or any(_has(text, t) for t in self.other_terms) or self.names_other_sport(text)
                or category in self.other_categories):
            return found
        return 'sport' if any(w in self.sport_words() for w in text.split()) else None

    def sport_words(self) -> frozenset:
        """The words of the league's sport as TheSportsDB spells it that name a sport: `football` for
        American Football, `soccer` for Soccer. Never the `football` names_other_sport() allows a
        soccer league, or every soccer league would read ESPN+'s college football names."""
        return frozenset(normalize(self.league.sport).split()) & _SPORT_WORDS

    def names_other_sport(self, text: str) -> bool:
        """The row's text names a sport that is not this league's: `College Basketball` on a channel
        in a college football category. A league whose sport is not known reads nothing here."""
        own = self._own_sport()
        return bool(own) and any(w in _SPORT_WORDS and w not in own for w in text.split())

    def _own_sport(self) -> frozenset:
        if self.own_sport is None:
            sport = normalize(self.league.sport).split()
            self.own_sport = frozenset(sport) | (frozenset({'football'}) if 'soccer' in sport else frozenset())
        return self.own_sport


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


def _league_text(league: SportsLeague, others: list, categories: list, skip_rules=None) -> LeagueText:
    raw = [t for t in json.loads(league.match_terms or '[]') if t and normalize(t)]
    terms = [normalize(t) for t in raw]
    needles = sorted({n for t in raw for n in _like_needles(t)})
    mine = {c.category_name: c.is_replay for c in categories if c.league_id == league.id}
    other_terms = sorted({normalize(t) for o in others for t in json.loads(o.match_terms or '[]')} - set(terms))
    other_cats = {c.category_name for c in categories if c.league_id != league.id} - set(mine)
    return LeagueText(league, terms, needles, mine, [t for t in other_terms if t], other_cats,
                      skips=(skip_rules or sports_skips.BUILTIN).of(league.tsdb_id))


def leagues_for_listing(title, sub_title, category) -> list:
    """The leagues a listing belongs to by the matcher's own league rule (6.3): a match term in its
    title or episode name, or a category a person tied to the league. One query per table, for one
    listing; never call it per row."""
    leagues = SportsLeague.query.all()
    categories = SportsLeagueCategory.query.all()
    text = normalize(' '.join(x for x in (title, sub_title) if x))
    return [league for league in leagues
            if _league_text(league, [o for o in leagues if o.id != league.id], categories).evidence(text, category)]


def _load_teams(lt: LeagueText, events: list, every_team: bool = False):
    """Each team's names and usable city (6.2). Until a team's lookup has run only its full name from
    the event counts. A city shared by two of the league's teams, anywhere in its stored schedule and
    not only in this run's games, reads as neither. `every_team` loads all the league's stored teams
    rather than only this run's games', for finding listings of games not stored at all."""
    ids = {i for e in events for i in (e.home_team_id, e.away_team_id) if i}
    league_ids = {i for pair in db.session.query(SportsEvent.home_team_id, SportsEvent.away_team_id)
                  .filter(SportsEvent.league_id == lt.league.id).distinct().all() for i in pair if i} | ids
    if every_team:
        ids = league_ids
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
    every = set()
    for tid in league_ids:
        team = rows.get(tid)
        names = [fallback.get(tid) or '']
        city = ''
        if team is not None:
            names = [team.full_name] + json.loads(team.names or '[]')
            if team.last_looked_up is None:
                names = [team.full_name]
            elif team.city and cities[normalize(team.city)] == 1:
                city = normalize(team.city)
            # A person's own names count whether or not the lookup has run (set_team_names).
            names += json.loads(team.own_names or '[]')
        folded = {normalize(n) for n in names if n and normalize(n)}
        every |= folded
        if tid in ids:
            lt.teams[tid] = TeamText(sorted(folded, key=len, reverse=True), city)
    lt.every_name = frozenset(every)
    lt._longer.clear()


def _phrase_spans(text: str, phrase: str) -> list:
    """(start, end) of every whole-word mention of a normalized phrase, in normalized text or in a
    folded name that still has its punctuation."""
    words = r'\W+'.join(re.escape(w) for w in phrase.split())
    return [(m.start(), m.end()) for m in re.finditer(rf"(?<![\w']){words}(?![\w'])", text)]


def _uncovered(found: list, covers: list) -> list:
    """The spans of `found` that do not sit inside a longer span of `covers`."""
    return [s for s in found if not any(o[0] <= s[0] and s[1] <= o[1] and o != s for o in covers)]


def _standing_spans(text: str, phrases, lt: LeagueText) -> list:
    """Where `phrases` are mentioned in `text` outside every longer stored team name. A team named
    only inside another team's longer name is not named: `Texas` in `Texas Tech at Kansas State` is
    Texas Tech's, and `North Carolina` in `Duke vs. North Carolina State` is NC State's. A guide row
    and a channel name are read through this; a lookup by name (_named_pair) applies the same
    _uncovered() test among the teams its text names (dev/changelog/1578)."""
    out = []
    for phrase in phrases:
        found = _phrase_spans(text, phrase) if phrase else []
        if found:
            out += _uncovered(found, [s for n in lt.longer_names(phrase) for s in _phrase_spans(text, n)])
    return out


def _team_in(text: str, team: TeamText, lt: LeagueText) -> str | None:
    """'name' or 'city' for how a row's normalized text names the team, or None. _has() is the cheap
    test nearly every row fails; only a row that passes it is read for where the name stands."""
    if any(_has(text, n) for n in team.names) and _standing_spans(text, team.names, lt):
        return 'name'
    if team.city and _has(text, team.city) and _standing_spans(text, (team.city,), lt):
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


#: A ranking in front of a team, `#7 BYU vs. #23 Baylor`, which would otherwise stand between the joiner
#: and the team.
_RANKING = re.compile(r'#\s*\d+\s*')
_HOME_SECOND = frozenset(('at', '@'))


def _named_pair(parts: tuple, teams: dict):
    """(first team id, second team id, joiner) for a title or episode name that names exactly two of
    the league's teams by name with only a joiner between them, else None. A team named only inside
    another's longer name is not named: `Florida Atlantic at Tulsa` names Florida Atlantic, not
    Florida. Cities are never read here, since a lookup by a guessed team costs a call."""
    for part in parts:
        if not part:
            continue
        text = normalize(part)
        named = [tid for tid, team in teams.items() if any(_has(text, n) for n in team.names)]
        if len(named) < 2:
            continue
        folded = _RANKING.sub('', _fold(part))
        spans = {tid: _spans(folded, TeamText(teams[tid].names)) for tid in named}
        every = [(s, tid) for tid, found in spans.items() for s in found]
        kept = {tid for tid, found in spans.items()
                if _uncovered(found, [o for o, other in every if other != tid])}
        if len(kept) != 2:
            continue
        a, b = sorted(kept)
        for first, second in ((a, b), (b, a)):
            for s1 in spans[first]:
                for s2 in spans[second]:
                    if (s1[1] <= s2[0] and _JOINER.fullmatch(folded[s1[1]:s2[0]])
                            and not _HYPHEN_AFTER.match(folded[s2[1]:])):
                        return first, second, folded[s1[1]:s2[0]].strip()
    return None


#: A word hyphened onto the second team's name makes it another team: `Wyoming at Texas-El Paso` is not
#: Texas, and the guide's spelling matches none of TheSportsDB's (UTEP), so no longer name rules it out.
#: The first team needs no such check, since the text between it and the second must be a joiner. Only
#: a hyphen with no space: `Baylor at BYU - Live` is a separator.
_HYPHEN_AFTER = re.compile(r'-\w')


def _names_past_year(text: str, season: str | None) -> bool:
    """A year before the season's first, `2022 Rose Bowl Ohio State vs Utah`: a game long played."""
    first = (season or '')[:4]
    return first.isdecimal() and any(int(y) < int(first) for y in _YEAR.findall(text))


def _unstored_games(lt: LeagueText, rows: list, hits: dict, capped: list, now: datetime) -> list:
    """[(UTC day, home id, away id)] to look up by name: listings where a round answer was cut, with
    the league, of a game not over yet, naming two of its teams side by side, that matched no stored
    game. `Away at Home` gives the order; any other joiner could be either, so both are asked. One
    check per distinct title a day, however many channels list it. A listing that says it is a
    replay, or names an earlier season's year, is a game already played and is never looked up: on
    prod they were asked about on the day they re-aired (dev/docs/BUGS.md 2026-11-14 @ 11:28:49 am)."""
    from .sports_data import lookup_days
    matched = {id(hit[0]) for found in hits.values() for hit in found}
    by_text = {}
    for row in rows:
        if id(row) in matched or row.stop <= now or _not_a_game(lt, row):
            continue
        if not any(lo <= row.start <= hi for lo, hi in capped) or lt.evidence(row.text, row.category) is None:
            continue
        if _RERUN.search(row.text) or _names_past_year(row.text, lt.league.current_season):
            continue
        key = (row.title, row.sub_title, row.start.date())
        if key not in by_text or row.start < by_text[key]:
            by_text[key] = row.start
    out = []
    for (title, sub_title, _day), start in sorted(by_text.items(), key=lambda kv: kv[1]):
        pair = _named_pair((title, sub_title), lt.teams)
        if pair is None:
            continue
        first, second, joiner = pair
        orders = [(second, first)] if joiner in _HOME_SECOND else [(first, second), (second, first)]
        out += [(day, home, away) for home, away in orders for day in lookup_days(start)]
    return list(dict.fromkeys(out))


def _not_a_game(lt: LeagueText, row) -> bool:
    """A listing too short to be a game, or saying a word that rules one out (app/sports_skips.py)."""
    return row.minutes < lt.skips.minutes(sports_skips.LENGTH_GAME) or lt.skips.says(sports_skips.KIND_GAME, row.text)


def _identify_matchups(lt: LeagueText, events: list, rows: list) -> dict:
    """{event id: [(row, league evidence, home, away)]}: the rows that are each game, before strength.
    A row with two candidate games of the same pair goes to the nearer (NHL home-and-home series)."""
    pairs = defaultdict(list)
    for e in events:
        pairs[frozenset((e.home_team_id, e.away_team_id))].append(e)
    out = defaultdict(list)
    for row in rows:
        if _not_a_game(lt, row):
            continue
        league = lt.evidence(row.text, row.category)
        if league is None:
            continue
        for pair, candidates in pairs.items():
            home_id, away_id = tuple(pair) if len(pair) == 2 else (next(iter(pair)),) * 2
            home, away = lt.teams.get(home_id), lt.teams.get(away_id)
            if home is None or away is None:
                continue
            got_home, got_away = _team_in(row.text, home, lt), _team_in(row.text, away, lt)
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


def _starts(rows: list) -> tuple:
    """({start: {channel ids}}, {start: whether any row there is marked live})."""
    by_start, live = defaultdict(set), defaultdict(bool)
    for row in rows:
        by_start[row.start].add(row.channel_id)
        live[row.start] |= row.is_live
    return by_start, live


def _guide_start(rows: list, event) -> tuple:
    """(start, channels): the start the game is given. TheSportsDB's time is what the rest of the app
    relies on, so a start within DISAGREE of it beats any start further off; then one marked live;
    then the most channels; then the nearest. 104 NFL Network feeds listing a 12:30 AM replay once
    outvoted the 48 channels carrying the 8:15 PM game live, and every listing of the live game was
    then dropped as too early (dev/changelog/1620). A start further off is used only when none is
    near, and only when it is marked live or on FAR_MIN_CHANNELS: the 12-hour typo case (6.3). With
    no time from TheSportsDB, any start the guide gives is used. (None, 0) when none qualifies.
    For a game TheSportsDB has moved, a start nearer its old time than its new one is not near:
    the guide listing the old time is behind, and any listing of the new time wins
    (dev/changelog/1623)."""
    by_start, live = _starts(rows)
    if not by_start:
        return None, 0
    best, near = _best_start(by_start, live, event, _moved(event))
    if event.start_at is not None and not near and not live[best] and len(by_start[best]) < FAR_MIN_CHANNELS:
        return None, 0
    return best, len(by_start[best])


def _best_start(by_start: dict, live: dict, event, moved: bool) -> tuple:
    """(start, whether it is near TheSportsDB's): the ranking games and races share. A start within
    DISAGREE of TheSportsDB's beats any further off, then one marked live, then the most channels,
    then the nearest. With `moved`, a start nearer TheSportsDB's previous time than its new one is
    not near (dev/changelog/1623)."""
    anchor, timed = _anchor(event), event.start_at is not None

    def near(s):
        if moved and abs(s - event.previous_start) < abs(s - event.start_at):
            return False
        return timed and abs(s - event.start_at) <= DISAGREE
    best = max(by_start, key=lambda s: (near(s), live[s], len(by_start[s]), -abs((s - anchor).total_seconds())))
    return best, near(best)


def _moved(event) -> bool:
    """TheSportsDB has moved this game's start by more than AGREE."""
    return (event.start_at is not None and event.previous_start is not None
            and abs(event.start_at - event.previous_start) > AGREE)


def _behind_start(event, computed):
    """`computed` when it is the guide still listing TheSportsDB's previous start for a game it has
    moved, else None (dev/changelog/1623). TheSportsDB's time is the one this app relies on, so
    its move wins over a guide that has not caught up."""
    if computed is None or not _moved(event):
        return None
    off_old, off_new = abs(computed - event.previous_start), abs(computed - event.start_at)
    return computed if off_old <= BEHIND_NEAR and off_old < off_new else None


def _start_settled(event, now: datetime) -> bool:
    """A guide start that has already aired is kept rather than measured again: an import drops
    listings that have aired, so the rows left an hour after a night game are its replays, and
    reading the start from them would call the game's time wrong and spend the league's trust.
    TheSportsDB's start has to have aired too: a stored start hours before it is measured again
    until then, so a placeholder's start that has passed cannot stand in for a game still to come
    (dev/changelog/1620)."""
    aired = event.guide_start is not None and event.guide_start + OFF_LIMIT < now
    return aired and (event.start_at is None or event.start_at + OFF_LIMIT < now)


def kept_start(event) -> bool:
    """The game had started by the start it was given when TheSportsDB moved it, so it keeps that
    start: a move during a game (a rain delay entered at the restart) never marks the guide behind
    or moves its listings, which would put the sports channels' waiting screen over a game on the
    air (dev/changelog/1626). `start_changed_at` is when the move was read and `guide_start` the
    start in use until then; one more than OFF_LIMIT from TheSportsDB's old start is not taken as
    the game having started, so a placeholder's passed start cannot hold a game still to come
    (dev/changelog/1620). The matcher holds matchups and races by it (races since dev/changelog/1632);
    the trust count leaves out any event it is true of. `previous_start` stays the start the game had
    before its first move once it is true (sports_data._store_parsed), so a delay entered in steps
    stays held, and sports_times.running_late() is the whole delay. A move further than MAX_DELAY
    from the start the game began at is a new date, not a delay (a race rained out to Monday, a
    suspended game resumed next week), so it is not held: its window is not stretched a day, and its
    listings are found again near the new time."""
    return (event.guide_start is not None and event.start_changed_at is not None
            and event.previous_start is not None and event.guide_start <= event.start_changed_at
            and abs(event.guide_start - event.previous_start) <= OFF_LIMIT
            and (event.start_at is None or abs(event.start_at - event.guide_start) <= MAX_DELAY))


def _settled_start(event, computed, channels: int, now: datetime) -> tuple:
    if _start_settled(event, now):
        return event.guide_start, event.guide_channels or 0
    return computed, channels


def _anchor_key(event_id: int, row) -> tuple:
    return event_id, row.channel_id, row.start, title_key(row.title)


def _judge_matchup(event, lt: LeagueText, hits: list, trusted: bool, now: datetime, clock, rejected=frozenset()) -> tuple:
    """([Found], guide start, channels, behind) for one game. `clock` writes a time for a reason
    sentence; `rejected` holds the anchors a person marked a wrong match, which are left out of the
    guide's start. `behind` is the start the guide still lists for a game TheSportsDB has moved, and
    the guide start given is then TheSportsDB's (_behind_start). A game that had started when
    TheSportsDB moved it keeps the start it had (kept_start), and its listings are judged from that
    start rather than the moved one."""
    counted_rows = [h[0] for h in hits if _anchor_key(event.id, h[0]) not in rejected]
    held = kept_start(event)
    kept = held or _start_settled(event, now)
    if kept:
        guide_start, guide_channels, behind = event.guide_start, event.guide_channels or 0, event.guide_behind_start
    else:
        guide_start, guide_channels = _guide_start(counted_rows, event)
        behind = _behind_start(event, guide_start)
    # The start the listings themselves give, which judges early rows and airings.
    listed = behind or guide_start
    disagrees = (not held and behind is None and event.start_at is not None and guide_start is not None
                 and abs(guide_start - event.start_at) > DISAGREE)
    ref = listed if (held or behind or disagrees or event.start_at is None) else event.start_at
    checked = trusted and not held and behind is None and not disagrees and event.start_at is not None
    by_start, _live = _starts(counted_rows)
    found = []
    for row, league, home, away in hits:
        delta = row.start - ref
        # An early row is a placeholder unless more channels list its start than list the game's:
        # 397 FOX stations at 3:30 PM against 44 at TheSportsDB's 7:00 PM are not one channel's slate
        # repeated every 2 hours, and whichever is the game, it stays matched (dev/changelog/1620).
        if delta < -EARLY_LIMIT and (listed is None or len(by_start[row.start]) <= guide_channels):
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
        elif held:
            when = (f'near {clock(listed)}, where the guide lists it. The game had started at {clock(guide_start)} '
                    f'when TheSportsDB moved it to {clock(event.start_at)}, and a game that has started keeps '
                    f'its start')
        elif behind:
            when = (f'near {clock(behind)}, where the guide still lists it. TheSportsDB moved the game from '
                    f'{clock(event.previous_start)} to {clock(event.start_at)}, and that is the time used')
        elif disagrees:
            when = (f'at {clock(guide_start)}, the time the guide lists. TheSportsDB lists '
                    f'{clock(event.start_at)}')
        else:
            when = "near the listed time, which is not checked for this league yet"
        found.append(Found(row, strength, f'Matched on {where}, {evidence}, {when}.', evidence=evidence,
                           rejected=_anchor_key(event.id, row) in rejected))
    _airing(found, lt, ref, checked, floor=listed)
    # A kept start is returned as stored: one TheSportsDB moves after the game began stays put.
    return found, (event.start_at if behind and not kept else guide_start), guide_channels, behind


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
            elif (f.row.is_live or f.row.start - earliest <= OFF_LIMIT
                  or (floor is not None and abs(f.row.start - floor) <= OFF_LIMIT)):
                # The start the game is given is its first airing even when more channels list an
                # earlier one (dev/changelog/1620).
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
    return {s for w in text.split() if w not in _RACE_FILLER for s in _RACE_SPELLINGS.get(w, (w,))}


def _session_words(text: str, lt: LeagueText) -> set:
    """_race_words() less the session's own words and numbers, which are compared on their own: what
    is left says which race weekend it is."""
    return {w for w in _race_words(text, lt) if w not in _SESSION_FILLER and not _SESSION_TOKEN.match(w)}


def _nearest_alone(events: list, start: datetime):
    """The one event whose TheSportsDB start is within OFF_LIMIT of `start`, or None when none or
    more than one is."""
    near = [e for e in events if e.start_at is not None and abs(e.start_at - start) <= OFF_LIMIT]
    return near[0] if len(near) == 1 else None


def _race_hit(row, lt: LeagueText, events: list, kinds: dict):
    """(event, strength, why) for a listing that names no session, or None. Only a race is ever its
    event, but one that does not name the race is matched only when the race is the one event of any
    kind in its band: `Formula 1` on qualifying day is as likely the qualifying, and a race-day replay
    of another race is not a practice's (dev/changelog/1633)."""
    banded = [e for e in events if _band(e)[0] <= row.start < _band(e)[1]]
    in_band = [e for e in banded if kinds[e.id].kind == SESSION_RACE]
    if not in_band:
        return None
    words = _race_words(row.text, lt)
    agreeing = [e for e in in_band if (core := _race_words(normalize(e.name), lt)) and core <= words]
    if agreeing:
        event = min(agreeing, key=lambda e: abs(row.start - _anchor(e)))
        return event, SPORTS_MATCH_STRONG, 'Matched on the series and the date, and the listing names this race.'
    if len(banded) != 1:
        return None
    event = in_band[0]
    if not words:
        return (event, SPORTS_MATCH_STRONG,
                'Matched on the series and the date. The listing names no race, and this is the only one that day.')
    return (event, SPORTS_MATCH_WEAK,
            f'Matched on the series and the date, but the guide calls it "{strip_marks(row.sub_title or row.title)}" '
            f'and TheSportsDB calls it "{event.name}".')


def _session_hit(row, said: Session, lt: LeagueText, events: list, kinds: dict):
    """(event, strength, why) for a listing that names a session, or None. Only a session of the
    kind it names, and of its number when both give one, is ever its event: `Las Vegas F1 GP:
    Practice 2` is never Practice 1 and never the race. A weekend can hold several of one kind in
    a day's band (Supercars' three qualifyings at Adelaide), so a listing that leaves more than one
    open goes to the one whose start is within OFF_LIMIT of it, when only one is."""
    in_band = [e for e in events if kinds[e.id].kind == said.kind and _band(e)[0] <= row.start < _band(e)[1]
               and (said.number is None or kinds[e.id].number in (None, said.number))]
    if not in_band:
        return None
    label = SESSION_LABELS[said.kind]
    words = _session_words(row.text, lt)
    agreeing = [e for e in in_band if (core := _session_words(normalize(e.name), lt)) and core <= words]
    pool = agreeing or in_band
    event = pool[0] if len(pool) == 1 else _nearest_alone(pool, row.start)
    if event is None:
        return None
    which = 'the only one of the series that day' if len(pool) == 1 else 'the one starting nearest it'
    if agreeing:
        return (event, SPORTS_MATCH_STRONG,
                f'Matched on the series, the date and the {label}, and the listing names this race weekend.')
    if not words:
        return (event, SPORTS_MATCH_STRONG,
                f'Matched on the series, the date and the {label}. The listing names no race, and this {label} '
                f'is {which}.')
    if len(in_band) != 1:
        return None
    return (event, SPORTS_MATCH_WEAK,
            f'Matched on the series, the date and the {label}, but the guide calls it '
            f'"{strip_marks(row.sub_title or row.title)}" and TheSportsDB calls it "{event.name}".')


def _race_start(items: list, event) -> tuple:
    """(start, channels) a race is given, from its strong listings when it has any: a weak one is
    another series on the same day as often as this race, and an F1 Academy listing at 6:00 PM ET
    once set the Las Vegas Grand Prix's start five hours early and made replays of the 82 channels
    carrying it replays (dev/changelog/1627). They are ranked as a game's are (_best_start), except
    that a race always gets a start, however far off, and TheSportsDB's previous time is not read:
    the guide-behind rule of dev/changelog/1623 is for matchups only."""
    pool = [r for r, strength, _ in items if strength == SPORTS_MATCH_STRONG] or [r for r, _, _ in items]
    by_start, live = _starts(pool)
    best, _near = _best_start(by_start, live, event, moved=False)
    return best, len(by_start[best])


def _judge_field_events(lt: LeagueText, events: list, rows: list, trusted: bool, now: datetime,
                        rejected=frozenset()) -> dict:
    """{event id: ([Found], guide start, channels)} for a league's races and their sessions. A
    listing that names a session (practice, qualifying, a shootout, a sprint) is only ever a listing
    of that session, and one that names none only ever of a race: a race-day replay once landed on
    the practice that was the only event on its day, 84 weak matches on prod (dev/changelog/1633)."""
    kinds = {e.id: event_session(e) for e in events}
    hits = defaultdict(list)
    for row in rows:
        if lt.skips.says(sports_skips.KIND_RACE, row.text):
            continue
        said = session_of(row.text)
        if said.kind == SESSION_RACE:
            if row.minutes < lt.skips.minutes(sports_skips.LENGTH_RACE) or lt.evidence(row.text, row.category) is None:
                continue
            hit = _race_hit(row, lt, events, kinds)
        elif said.kind in SESSIONS:
            if row.minutes < lt.skips.minutes(sports_skips.LENGTH_SESSION) or lt.evidence(row.text, row.category) is None:
                continue
            hit = _session_hit(row, said, lt, events, kinds)
        else:
            raise ValueError(f'unknown session kind {said.kind!r}')
        if hit is not None:
            event, strength, why = hit
            hits[event.id].append((row, strength, why))
    out = {}
    by_id = {e.id: e for e in events}
    for event_id, items in hits.items():
        event = by_id[event_id]
        counted_items = [i for i in items if _anchor_key(event_id, i[0]) not in rejected] or items
        # A race TheSportsDB moved after it had started keeps its start, as a game does: a rain delay
        # moved to the green flag otherwise handed the start to whichever listing was nearer the new
        # time, a re-air, and the race on the air became its replay (dev/changelog/1632).
        held = kept_start(event)
        if held:
            earliest, channels = event.guide_start, event.guide_channels or 0
        else:
            seen, seen_channels = _race_start(counted_items, event)
            earliest, channels = _settled_start(event, seen, seen_channels, now)
        found = [Found(r, s, why, rejected=_anchor_key(event_id, r) in rejected)
                 for r, s, why in items if r.start - earliest <= WINDOW]
        disagrees = event.start_at is not None and abs(earliest - event.start_at) > DISAGREE
        checked = trusted and not held and not disagrees and event.start_at is not None
        _airing(found, lt, event.start_at if checked else earliest, checked, floor=earliest)
        out[event_id] = (found, earliest, channels)
    return out


# ---------------------------------------------------------------------------
# Event channels: a channel with no real guide listing at a game's time, whose name is the only one (6.8)
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
    """Channels not hidden whose name carries one of the league's terms or a word for its sport, or
    that sit in one of its tied categories. A tied category supplies the league, so `US| NHL PPV`'s
    names need not say NHL. LIKE only narrows; the whole-word check is in Python. Whether the
    channel's guide lists something real at a game's time is _drop_listed's question, asked once a
    name has picked the game."""
    conds = [Channel.name.ilike(f'%{needle}%') for needle in sorted(set(lt.needles) | lt.sport_words())]
    if lt.categories:
        conds.append(Channel.category_name.in_(list(lt.categories)))
    if not conds:
        return []
    q = db.session.query(Channel.id, Channel.account_id, Channel.category_name, Channel.name, Channel.last_seen_at
                         ).filter(or_(*conds), _offered())
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
        if lt.skips.says(sports_skips.KIND_GAME, ch.text):
            continue
        league = lt.name_evidence(ch.text, ch.category)
        if league is None:
            continue
        named = []
        folded = None
        for pair, candidates in pairs.items():
            a, b = lt.teams.get(pair[0]), lt.teams.get(pair[1])
            if a is None or b is None:
                continue
            got_a, got_b = _team_in(ch.text, a, lt), _team_in(ch.text, b, lt)
            # A sport word is shared by every league of that sport, so with it a city is not enough:
            # `Football: Pittsburgh vs. Miami` is college football, not the Steelers and Dolphins.
            if got_a and got_b and (league != 'sport' or got_a == got_b == 'name'):
                # ESPN+ ranks a team after the joiner: `Football: Duke vs. #7 Miami`.
                folded = folded if folded is not None else _RANKING.sub('', _fold(ch.name))
                if _faces_off(folded, a, b, lt):
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
    text = strip_marks(name).translate(_SPELLING)
    return ''.join(ch for ch in unicodedata.normalize('NFKD', text) if not unicodedata.combining(ch)).casefold()


def _spans(folded: str, team: 'TeamText') -> list:
    """(start, end) of every whole-word mention of the team's names and city in a folded name."""
    return [s for phrase in (*team.names, team.city) if phrase for s in _phrase_spans(folded, phrase)]


def _faces_off(folded: str, a: 'TeamText', b: 'TeamText', lt: LeagueText) -> bool:
    """The two teams sit side by side with only a joiner between them. A provider's team channels name
    their market's teams with none (`NFL TEAMS| CBS BILLS GIANTS JETS NEW YORK NY`, `... CHIEFS BEARS ST.
    LOUIS MO`), and with only filler in their guide they are read by name (dev/changelog/1473)."""
    sa = _standing_spans(folded, (*a.names, a.city), lt)
    sb = _standing_spans(folded, (*b.names, b.city), lt)
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
    if league == 'text':
        where = 'the league named in it'
    elif league == 'sport':
        where = 'the sport named in it'
    elif league == 'category':
        where = f'its category, {ch.category}'
    else:
        raise ValueError(f'league evidence nothing handles: {league!r}')
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
            return f'and {said} is {off}the time the guide lists', strength, False
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
    date in the name, so a name with no date is never matched to one: a series races most weeks.
    A race and a sprint only, each only by a name of its own kind: practice, qualifying and a
    shootout are matched by a guide listing that names them, never by a channel's name
    (dev/changelog/1633)."""
    out = defaultdict(list)
    kinds = {e.id: event_session(e).kind for e in events}
    for ch in channels:
        level = ch.read.clock.level
        if level == event_names.LEVEL_NONE or lt.skips.says(sports_skips.KIND_RACE, ch.text):
            continue
        kind = session_of(ch.text).kind
        if kind in NOT_RACES or lt.evidence(ch.text, ch.category) is None:
            continue
        if kind not in (SESSION_RACE, SESSION_SPRINT):
            raise ValueError(f'unknown session kind {kind!r}')
        if level == event_names.LEVEL_TZ:
            banded = [e for e in events if _band(e)[0] <= ch.read.clock.at < _band(e)[1]]
        else:
            banded = [e for e in events if abs(_event_day(e) - ch.read.clock.day) <= NAME_DAY_SLACK]
        # Only an event of the name's own kind is its event; one the name does not name is matched
        # only when it is the one event of any kind that day, as a guide listing is (_race_hit).
        in_band = [e for e in banded if kinds[e.id] == kind]
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
        elif len(banded) == 1 and not cores[in_band[0].id]:
            event, strength = in_band[0], SPORTS_MATCH_STRONG
            why = 'matched on the series and the date in it, the only race of the series that day'
        elif len(banded) == 1:
            event, strength = in_band[0], SPORTS_MATCH_WEAK
            why = f'matched on the series and the date in it, but TheSportsDB calls the race "{event.name}"'
        else:
            continue
        found = NameFound(ch, strength, f'{_NAME_PREFIX}: {why}.')
        found.airing = _name_airing(lt.categories.get(ch.category), ch, False)
        out[event.id].append(found)
    return out


def _event_spans(event) -> list:
    """From TheSportsDB's start (or the middle of its day) and from the guide's own start, when one is
    stored, for the event's minimum length: a listing far from TheSportsDB's time is still matched as
    a listing (6.5), and its channel must not be read by its name beside it."""
    length = timedelta(minutes=MIN_GAME_MINUTES if (event.home_team_id or event.away_team_id) else MIN_RACE_MINUTES)
    starts = {_anchor(event)} | ({event.guide_start} if event.guide_start is not None else set())
    return [(s, s + length) for s in sorted(starts)]


def _drop_listed(named: dict, events: dict) -> dict:
    """The name matches whose channel's guide has nothing real at the event's time: a channel with a
    listing then is read by its listing (6.3), never by its name as well. Asked per event rather than
    per channel, since an event channel renamed for its next game keeps its earlier blocks under its
    former name (`NCAAF 27:` before `NCAAF 27: 1pm Arkansas State at Coastal Carolina`), which no
    longer equal its name and so are not filler. Asking whether the channel had any real row at all
    left 10 of 2026-11-14's college football games matched to nothing (dev/changelog/1562)."""
    ids = sorted({f.channel.channel_id for items in named.values() for f in items})
    if not ids:
        return named
    spans = {eid: _event_spans(events[eid]) for eid, items in named.items() if items}
    lo = min(s[0] for each in spans.values() for s in each)
    hi = max(s[1] for each in spans.values() for s in each)
    listed = defaultdict(list)
    for chunk in chunked(ids, 500):
        for channel_id, start, stop in (
                db.session.query(EPGEntry.channel_id, EPGEntry.start_time, EPGEntry.stop_time)
                .join(Channel, Channel.id == EPGEntry.channel_id)
                .filter(EPGEntry.channel_id.in_(chunk), EPGEntry.start_time < hi, EPGEntry.stop_time > lo,
                        ~_filler()).all()):
            listed[channel_id].append((start, stop))
    return {eid: [f for f in items
                  if not any(start < hi_ and stop > lo_ for start, stop in listed[f.channel.channel_id]
                             for lo_, hi_ in spans[eid])]
            for eid, items in named.items()}


# ---------------------------------------------------------------------------
# Storing
# ---------------------------------------------------------------------------

def _log_behind(event, behind):
    """Say when a game starts or stops going by TheSportsDB's time over a guide still listing its
    old one (dev/changelog/1623). Called only on a change, so a run does not repeat it."""
    if behind is not None:
        log.warning('Sports match: TheSportsDB moved %s (event %s) from %s to %s UTC and the guide still '
                    'lists it at %s UTC; going by TheSportsDB', event.name, event.tsdb_id,
                    _utc(event.previous_start), _utc(event.start_at), _utc(behind))
    else:
        log.info("Sports match: the guide no longer lists %s (event %s) at TheSportsDB's old time",
                 event.name, event.tsdb_id)


def _log_kept(event):
    """Say once per move that a game TheSportsDB moved after it had started keeps its start
    (kept_start)."""
    log.warning('Sports match: TheSportsDB moved %s (event %s) from %s to %s UTC after it had started at '
                '%s UTC; it keeps that start', event.name, event.tsdb_id, _utc(event.previous_start),
                _utc(event.start_at), _utc(event.guide_start))


def _utc(when) -> str:
    return when.strftime('%Y-%m-%d %H:%M') if when else 'none'


@retry_on_locked()
def _store_event(event_id: int, found: list, guide_start, guide_channels: int, now: datetime,
                 behind=None) -> tuple:
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
    # Written even when None: a start the guide no longer gives, before the game has aired, is
    # cleared rather than kept, so the game falls back to TheSportsDB's time (dev/changelog/1620).
    # One that has aired arrives here already kept by _settled_start.
    event.guide_start, event.guide_channels = guide_start, (guide_channels if guide_start is not None else None)
    if behind != event.guide_behind_start:
        _log_behind(event, behind)
    event.guide_behind_start = behind
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
    # A game the guide still lists at TheSportsDB's old time says nothing about the league's times:
    # its guide start is TheSportsDB's own (dev/changelog/1623). Nor does one TheSportsDB moved after
    # it started, whose guide start is kept from before the move (dev/changelog/1626).
    q = (db.session.query(SportsEvent.start_at, SportsEvent.guide_start, SportsEvent.previous_start,
                          SportsEvent.start_changed_at)
         .filter(SportsEvent.league_id == league_id, SportsEvent.guide_start.isnot(None),
                 SportsEvent.guide_behind_start.is_(None))
         .order_by(SportsEvent.guide_start.desc()))
    rows = []
    for row in q.yield_per(100):
        if not kept_start(row):
            rows.append((row.start_at, row.guide_start))
            if len(rows) == TRUST_KEEP:
                break
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


def match_league(league_id: int, now: datetime, pause: float = PAUSE_SECONDS, clock=None,
                 last_run: datetime = None, skip_rules=None) -> dict:
    """Match one league's events that the guide can still hold, then the event channels whose names
    name one. Returns counts for the run's note. `clock` writes a time into a reason sentence, in the
    display timezone; run() builds it once, as it does `skip_rules` (app/sports_skips.py). `last_run`
    is when the last run finished, so a game TheSportsDB moved after it started is logged on the
    first run to see the move, not every run."""
    clock = clock or _clock()
    empty = {'events': 0, 'matched': 0, 'rows': 0, 'named': 0, 'channels': 0, 'lookups': 0, 'behind': 0,
             'kept': 0}
    league = db.session.get(SportsLeague, league_id)
    if league is None:
        return empty
    # An empty guide still runs, so what it no longer lists is marked gone.
    guide_end = db.session.query(db.func.max(EPGEntry.start_time)).scalar() or now
    events = _events_in_range(league_id, now - LOOKBACK, max(guide_end, now) + WINDOW)
    name_events = _events_in_range(league_id, now - LOOKBACK, now + NAME_AHEAD)
    from .sports_data import capped_spans
    # Clipped to the guide still to come: a round cut with every game already started reaches to
    # datetime.max, and its stored games can all be over (the NBA's round 0, dev/changelog/1550).
    capped = [(max(lo, now - LOOKBACK), min(hi, max(guide_end, now) + WINDOW)) for lo, hi in capped_spans(league_id)]
    capped = [(lo, hi) for lo, hi in capped if lo <= hi]
    if not events and not name_events and not capped:
        return empty
    leagues = SportsLeague.query.all()
    lt = _league_text(league, [o for o in leagues if o.id != league.id], SportsLeagueCategory.query.all(),
                      skip_rules or sports_skips.SkipRules())
    _load_teams(lt, list({e.id: e for e in events + name_events}.values()), every_team=bool(capped))
    trusted = league_trusted(league)
    matchups = [e for e in events if e.home_team_id or e.away_team_id]
    fields = [e for e in events if not (e.home_team_id or e.away_team_id)]
    results = {}
    rows = []
    hits = {}
    unstored = []
    kept = 0
    spans = [(_anchor(e) - WINDOW, _anchor(e) + WINDOW) for e in matchups] + [_band(e) for e in fields] + capped
    if spans:
        rows = _candidate_rows(lt, min(s[0] for s in spans), max(s[1] for s in spans))
    if events:
        rejected = {(m.event_id, m.channel_id, m.program_start_time, m.title_key) for m in SportsMatch.query.filter(
            SportsMatch.event_id.in_([e.id for e in events]), SportsMatch.kind == SPORTS_MATCH_KIND_GUIDE,
            SportsMatch.rejected_at.isnot(None)).all()}
        hits = _identify_matchups(lt, matchups, rows)
        for event in matchups:
            results[event.id] = _judge_matchup(event, lt, hits.get(event.id, []), trusted, now, clock, rejected)
            if kept_start(event):
                kept += 1
                if last_run is None or event.start_changed_at > last_run:
                    _log_kept(event)
        field_results = _judge_field_events(lt, fields, rows, trusted, now, rejected)
        for event in fields:
            unheard = (([], event.guide_start, event.guide_channels or 0) if kept_start(event)
                       else ([], *_settled_start(event, None, 0, now)))
            results[event.id] = (*(field_results.get(event.id) or unheard), None)
    if capped:
        unstored = _unstored_games(lt, rows, hits, capped, now)
    # A name is judged against the guide start already stored, so an event whose time disagrees is
    # at worst one run behind.
    named = {}
    if name_events:
        channels = _candidate_names(lt, now)
        named = _match_name_matchups(lt, [e for e in name_events if e.home_team_id or e.away_team_id],
                                     channels, trusted, now, clock)
        named.update(_match_name_races(lt, [e for e in name_events if not (e.home_team_id or e.away_team_id)],
                                       channels))
        named = _drop_listed(named, {e.id: e for e in name_events})
    db.session.rollback()   # no read transaction held across the writes and pauses below

    matched = rows_matched = 0
    behind = 0
    for event_id, (found, guide_start, channels_n, behind_start) in results.items():
        _store_event(event_id, found, guide_start, channels_n, now, behind_start)
        behind += behind_start is not None
        matched += bool(found)
        rows_matched += len(found)
        if pause:
            time_mod.sleep(pause)
    _store_names([e.id for e in name_events], named, now)
    _store_trust(league_id, now)
    from .sports_data import queue_guide_lookups
    lookups = queue_guide_lookups(league_id, unstored, now) if unstored else 0
    return {'events': len(events), 'matched': matched, 'rows': rows_matched,
            'named': sum(1 for items in named.values() if items),
            'channels': len({f.channel.channel_id for items in named.values() for f in items}),
            'lookups': lookups, 'behind': behind, 'kept': kept}


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
        skip_rules = sports_skips.SkipRules()
        league_ids = [i for (i,) in db.session.query(SportsEvent.league_id).distinct().all()]
        state = db.session.get(SportsState, 1)
        last_run = state.last_match_at if state is not None else None
        db.session.rollback()
        totals = Counter()
        for league_id in league_ids:
            totals.update(match_league(league_id, now, pause, clock, last_run, skip_rules))
        from .sports_data import ROUND_ROW_CAP, link_unfinished_recordings
        linked = link_unfinished_recordings(now)
        # Automated recordings of a game follow the time it goes by (dev/changelog/1631).
        from .sports_follow import follow_guarded
        followed = follow_guarded(now=now)
        _queue_rule_checks(league_ids)
        # A guide match is what gives a field event its time, so a league's numbered channels on
        # the output feed take their new games here as well as at a schedule update.
        from .sports_feed import place_all_guarded
        place_all_guarded(now)
        seconds = time_mod.monotonic() - started
        note = (f'Found {counted(totals["matched"], "event")} of {totals["events"]} in the guide, on '
                f'{counted(totals["rows"], "listing")}, in {seconds:.1f} seconds.')
        if totals['channels']:
            note += (f' {counted(totals["named"], "event")} named by {counted(totals["channels"], "event channel")}, '
                     f'read from their names.')
        if totals['behind']:
            note += (f' {counted(totals["behind"], "game")} TheSportsDB has moved still listed at the old time, '
                     f"going by TheSportsDB's.")
        if totals['kept']:
            one = totals['kept'] == 1
            note += (f' {counted(totals["kept"], "game")} TheSportsDB moved after '
                     f'{"it had started keeps its start" if one else "they had started keep their start"}.')
        if linked:
            note += f' Linked {counted(linked, "recording")} to its event.'
        if followed.get('moved') or followed.get('added') or followed.get('removed'):
            note += (f' Moved {counted(followed.get("moved", 0), "recording")} to its game\'s time'
                     + (f', added {counted(followed["added"], "second recording")}' if followed.get('added') else '')
                     + (f', removed {counted(followed["removed"], "second recording")}' if followed.get('removed') else '')
                     + '.')
        if totals['lookups']:
            note += (f' Queued {counted(totals["lookups"], "lookup")} by name for games the guide lists where '
                     f"TheSportsDB's schedule came back cut at {ROUND_ROW_CAP} games.")
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


def _playable():
    """The guide matches a sports channel on the output feed may play from: a strong match still in
    the guide, not marked wrong, and not a listing the guide calls a replay. An unknown airing is
    doubt, which nothing skips (6.5)."""
    return _live_matches().filter(SportsMatch.strength == SPORTS_MATCH_STRONG,
                                  SportsMatch.airing != SPORTS_AIRING_REPLAY)


def _windows_query(event_ids: list):
    return (_playable().with_entities(SportsMatch.event_id, SportsMatch.channel_id, Channel.account_id,
                                      SportsMatch.program_start_time, SportsMatch.program_stop_time)
            .join(Channel, Channel.id == SportsMatch.channel_id)
            .filter(SportsMatch.event_id.in_(event_ids), _offered()))


def _timed(event_ids: list, rows, now: datetime, rules) -> list:
    """[(event id, channel id, account id, start, stop, guide start)] for `rows`, each listing at
    the times its league's answer gives it (app/sports_times.py), and last the start the guide
    gives it: one listing may be two windows under Cover both. The events and their leagues are
    read in one query."""
    from .sports_times import Rules, event_windows
    rules = rules if rules is not None else Rules()
    events = {e.id: (e, tsdb_id) for e, tsdb_id in
              db.session.query(SportsEvent, SportsLeague.tsdb_id)
              .join(SportsLeague, SportsLeague.id == SportsEvent.league_id)
              .filter(SportsEvent.id.in_(event_ids)).all()}
    out = []
    for event_id, channel_id, account_id, start, stop in rows:
        found = events.get(event_id)
        if found is None:
            continue
        event, tsdb_id = found
        for begin, end in event_windows(start, stop, event, rules.of(tsdb_id), now):
            out.append((event_id, channel_id, account_id, begin, end, start))
    return out


def airing_now(event_ids: list, now: datetime, before: timedelta = timedelta(0),
               after: timedelta = timedelta(0), rules=None, listed=False) -> list:
    """[(event id, channel id, program start, program stop)] for each listing of these events on a
    visible channel that is within reach of `now`: it starts no more than `before` from now and
    ended no more than `after` ago. With both at zero that is the listings on at `now`. Each listing
    counts at the times its league's answer gives it (app/sports_times.py), which can move it by up
    to the matcher's WINDOW, so the query reaches that much further and the times are checked
    after. With `listed`, each tuple also ends with the listing's own start in the guide, which is
    how a play names the listings it holds (auto_rules.game_sources()). Two queries
    (app/sports_feed.py decides which of them a play takes)."""
    if not event_ids:
        return []
    rows = (_windows_query(event_ids)
            .filter(SportsMatch.program_start_time <= now + before + WINDOW,
                    SportsMatch.program_stop_time > now - after - WINDOW).distinct().all())
    out = []
    for event_id, channel_id, _account, start, stop, listed_at in _timed(event_ids, rows, now, rules):
        if start is not None and stop is not None and start <= now + before and stop > now - after:
            out.append((event_id, channel_id, start, stop) + ((listed_at,) if listed else ()))
    return list(dict.fromkeys(out))


def listing_windows(event_ids: list, now: datetime, rules=None) -> dict:
    """{event id: [(channel id, account id, program start, program stop)]} over the listings a
    sports channel may play from, on visible channels, each at the times its league's answer gives
    it (app/sports_times.py), in two queries (app/sports_feed.py). `rules` is a sports_times.Rules
    the caller already holds, else one is read."""
    out = {}
    if not event_ids:
        return out
    for event_id, channel_id, account_id, start, stop, _listed in _timed(event_ids, _windows_query(event_ids).all(),
                                                                         now, rules):
        out.setdefault(event_id, []).append((channel_id, account_id, start, stop))
    return out


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


# The account columns the listings dialog draws beside each channel (its pill), read in the same query.
_ACCOUNT_COLS = (Account.id, Account.name, Account.color)


def name_rows(event_id: int) -> list:
    """The event channels naming one event, as the listings dialog lists them: still named, on a channel
    not hidden, strong before weak, then by name, wrong matches included and flagged, each with the
    account it is on. One query, whatever the size."""
    rows = (db.session.query(SportsMatch, Channel.name, Channel.category_name, *_ACCOUNT_COLS)
            .join(Channel, Channel.id == SportsMatch.channel_id)
            .join(Account, Account.id == Channel.account_id)
            .filter(SportsMatch.event_id == event_id, SportsMatch.kind == SPORTS_MATCH_KIND_CHANNEL_NAME,
                    SportsMatch.gone_since.is_(None), _offered())
            .order_by(db.case((SportsMatch.strength == SPORTS_MATCH_WEAK, 1), else_=0), Channel.name).all())
    return [{'id': m.id, 'channel_id': m.channel_id, 'name': name, 'category': category or '',
             'account': {'id': acct_id, 'name': acct_name, 'color': acct_color},
             'why': _name_why(m.reason), 'badge': NAME_AIRING_BADGES[m.airing], 'wrong': m.rejected_at is not None}
            for m, name, category, acct_id, acct_name, acct_color in rows]


def listing_groups(event_id: int, tz, h24: bool) -> list:
    """An event's guide matches as the listings dialog shows them: one group per way a listing is
    worded and aired, each with its channels, wrong matches included and flagged. Rows gone from the
    guide and rows on a hidden channel are left out. Each channel carries the account it is on. One
    query, whatever the size."""
    rows = (db.session.query(SportsMatch, Channel.name, Channel.category_name, *_ACCOUNT_COLS)
            .join(Channel, Channel.id == SportsMatch.channel_id)
            .join(Account, Account.id == Channel.account_id)
            .filter(SportsMatch.event_id == event_id, SportsMatch.kind == SPORTS_MATCH_KIND_GUIDE,
                    SportsMatch.gone_since.is_(None), _offered())
            .order_by(SportsMatch.program_start_time, Channel.name).all())
    groups = {}
    for m, name, category, acct_id, acct_name, acct_color in rows:
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
                              'account': {'id': acct_id, 'name': acct_name, 'color': acct_color},
                              'wrong': m.rejected_at is not None})
    out = sorted(groups.values(), key=lambda g: (-len(g['channels']), g['start']))
    for g in out:
        g['start'] = g['start'].isoformat()
        g['all_wrong'] = all(c['wrong'] for c in g['channels'])
    return out


def skipped_listings(event_id: int, tz, h24: bool, skip_rules=None, one: str = None) -> list:
    """The listings dialog's Skipped section (dev/changelog/1634): the listings that name the event's
    league where it could be on, and for a game both its teams, that a word or a length kept out,
    each with every reason. A race's are of its own kind, a race's listings for a race and a
    session's for that session, in its day's band. Worked out when the dialog opens, never stored:
    a change to the words shows the next time it opens. `one` is what the page calls the event
    (sports_view.event_noun), for the reason's words. A few queries, whatever the guide's size."""
    event = db.session.get(SportsEvent, event_id)
    if event is None:
        return []
    league = db.session.get(SportsLeague, event.league_id)
    leagues = SportsLeague.query.all()
    lt = _league_text(league, [o for o in leagues if o.id != league.id], SportsLeagueCategory.query.all(),
                      skip_rules or sports_skips.SkipRules())
    sk = lt.skips
    matchup = bool(event.home_team_id or event.away_team_id)
    if matchup:
        _load_teams(lt, [event])
        home, away = lt.teams.get(event.home_team_id), lt.teams.get(event.away_team_id)
        if home is None or away is None:
            return []
        lo, hi = _anchor(event) - WINDOW, _anchor(event) + WINDOW
        kind, wanted = sports_skips.KIND_GAME, None
    else:
        lo, hi = _band(event) if event.local_date is not None else (_anchor(event) - WINDOW, _anchor(event) + WINDOW)
        kind, wanted = sports_skips.KIND_RACE, event_session(event).kind
    groups = {}
    for row in _candidate_rows(lt, lo, hi):
        if lt.evidence(row.text, row.category) is None:
            continue
        if matchup:
            if _team_in(row.text, home, lt) is None or _team_in(row.text, away, lt) is None:
                continue
            length = sports_skips.LENGTH_GAME
        else:
            said_kind = session_of(row.text).kind
            if said_kind != wanted:
                continue
            length = sports_skips.LENGTH_RACE if said_kind == SESSION_RACE else sports_skips.LENGTH_SESSION
        minutes = round(row.minutes)
        why = sports_skips.reason(sk, sk.said(kind, row.text), minutes, length, one or kind)
        if not why:
            continue
        g = groups.setdefault((row.title, row.sub_title, row.start), {
            'title': row.title or '', 'sub': row.sub_title or '', 'start': row.start,
            'when': format_local(row.start, 'clock', tz=tz, h24=h24), 'why': why, 'channels': set()})
        g['channels'].add(row.channel_id)
    out = sorted(groups.values(), key=lambda g: (g['start'], -len(g['channels']), g['title']))
    for g in out:
        g['start'] = g['start'].isoformat()
        g['channels'] = len(g['channels'])
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


#: The most names a person may add to one team.
TEAM_NAMES_MAX = 20


def clean_team_names(names) -> list:
    """A team's added names as a person typed them: trimmed, blank and repeated ones (ignoring
    case) dropped. None is allowed, since TheSportsDB's own names still stand."""
    out, seen = [], set()
    for n in names or []:
        n = ' '.join(str(n).split())[:120]
        if n and normalize(n) and n.lower() not in seen:
            seen.add(n.lower())
            out.append(n)
    if len(out) > TEAM_NAMES_MAX:
        raise ValueError(f'A team can have at most {TEAM_NAMES_MAX} added names.')
    return out


@retry_on_locked()
def _write_team_names(tsdb_id: int, names: list) -> bool:
    team = SportsTeam.query.filter_by(tsdb_id=tsdb_id).first()
    if team is None:
        db.session.rollback()
        return False
    team.own_names = json.dumps(names)
    db.session.commit()
    return True


def set_team_names(tsdb_id: int, names) -> list:
    """Save the names a person added for a team and match again: the one writer of
    SportsTeam.own_names. They are read like TheSportsDB's names and survive every lookup.
    Returns what was saved, or None when the team is gone. Raises ValueError for too many."""
    from .scheduler import queue_sports_match
    cleaned = clean_team_names(names)
    if not _write_team_names(tsdb_id, cleaned):
        return None
    queue_sports_match(reason='a team\'s names changed')
    return cleaned


def team_names_context(league_id: int) -> list:
    """[{id, name, theirs, own}] for each team the league's stored schedule names, by name: what
    TheSportsDB calls it and what a person added. Two queries."""
    from .sports_data import _league_team_ids
    ids = _league_team_ids(league_id)
    if not ids:
        return []
    out = []
    for t in SportsTeam.query.filter(SportsTeam.tsdb_id.in_(ids)).all():
        theirs = list(dict.fromkeys([t.full_name] + json.loads(t.names or '[]')))
        out.append({'id': t.tsdb_id, 'name': t.full_name, 'theirs': theirs, 'own': json.loads(t.own_names or '[]')})
    return sorted(out, key=lambda t: t['name'].casefold())


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
def _write_categories(league_id: int, add: list, remove: list, replay: dict):
    """[(name, replays only)] for each category newly tied, or None when the league is gone."""
    if db.session.get(SportsLeague, league_id) is None:
        db.session.rollback()
        return None
    have = {c.category_name: c for c in SportsLeagueCategory.query.filter_by(league_id=league_id).all()}
    added = []
    for name in add:
        if name not in have:
            have[name] = SportsLeagueCategory(league_id=league_id, category_name=name,
                                              is_replay=bool(re.search(r'\breplays?\b', name, re.I)))
            db.session.add(have[name])
            added.append(name)
    for name in remove:
        if name in have:
            db.session.delete(have.pop(name))
    for name, value in replay.items():
        if name in have:
            have[name].is_replay = bool(value)
    added = [(name, bool(have[name].is_replay)) for name in added if name in have]
    db.session.commit()
    return added


def change_categories(league_id: int, add=(), remove=(), replay=None):
    """Tie categories to a league, untie them, or flip Replays only, then match again. A name is
    kept as typed, since the search treats a category by name across accounts. A new one whose name
    says replay starts with Replays only on; the person can turn it off. Returns [(name, replays
    only)] for each category newly tied, so the page can say what it switched on by itself, or None
    when the league is gone."""
    from .scheduler import queue_sports_match
    # As given, spaces and all: it has to equal Channel.category_name, which a sync stores raw.
    add = [n[:255] for n in add if n.strip()]
    added = _write_categories(league_id, add, list(remove), dict(replay or {}))
    if added is None:
        return None
    queue_sports_match(reason='a league\'s categories changed')
    return added


def replay_category_live_games(league_id: int, now: datetime) -> dict:
    """{category: games} for the league's Replays-only categories that hold a strong listing of a
    game still to come at that game's own start. Such a listing reads as the live airing, and the
    category makes it a replay (_airing), which an auto-record rule and a sports channel then skip:
    prod's NHL Center Ice lost its live games this way for a day (dev/changelog/1582). One query."""
    rows = (db.session.query(Channel.category_name, SportsMatch.event_id, SportsMatch.program_start_time,
                             SportsEvent.start_at, SportsEvent.guide_start)
            .join(SportsEvent, SportsEvent.id == SportsMatch.event_id)
            .join(Channel, Channel.id == SportsMatch.channel_id)
            .join(SportsLeagueCategory, and_(SportsLeagueCategory.league_id == SportsEvent.league_id,
                                             SportsLeagueCategory.category_name == Channel.category_name))
            .filter(SportsEvent.league_id == league_id, SportsLeagueCategory.is_replay.is_(True),
                    SportsMatch.kind == SPORTS_MATCH_KIND_GUIDE, SportsMatch.strength == SPORTS_MATCH_STRONG,
                    SportsMatch.rejected_at.is_(None), SportsMatch.gone_since.is_(None),
                    SportsMatch.program_start_time >= now - AGREE).all())
    games = defaultdict(set)
    for category, event_id, listed, start, guide_start in rows:
        if any(s is not None and abs(listed - s) <= AGREE for s in (start, guide_start)):
            games[category].add(event_id)
    return {category: len(ids) for category, ids in games.items()}
