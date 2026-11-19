"""The local schedule from TheSportsDB: its client, its queue and what is followed.

TheSportsDB describes events; it does not find channels. This keeps a copy of the real schedule for
only what someone follows, fetched slowly by one worker, so later work can compare it with what the
provider's guide claims. With nothing followed there is no job and no call.

Spec: dev/docs/DESIGN-sports-data.md. Pages: app/sports_view.py. An outbound call is never inside a
retry_on_locked closure, and nothing here can touch or delay a recording.
"""
import json
import logging
import random
import re
import threading
from dataclasses import dataclass, field
from datetime import datetime, time, timedelta

import requests
from sqlalchemy import and_, case, or_, update
from sqlalchemy.exc import SQLAlchemyError

from . import db
from .config import load_config
from .database import (
    REC_ENDED_STATUSES, RECORDING_SPORTS_LINKED, RECORDING_SPORTS_REMOVED, SPORTS_MATCH_KIND_CHANNEL_NAME, SPORTS_MATCH_KIND_GUIDE,
    SPORTS_MATCH_STRONG, SPORTS_MATCH_WEAK, ChannelGroupMember, Recording, RecordingEvent, RecordingSports,
    add_recording_event,
    SPORTS_FETCH_CAPPED, SPORTS_FETCH_CAPPED_PAST, SPORTS_FETCH_EMPTY, SPORTS_FETCH_OK, SPORTS_FETCH_RATE_LIMITED, SPORTS_FETCH_REFUSED,
    SPORTS_FETCH_SHRUNK, SPORTS_KIND_EVENT, SPORTS_KIND_LEAGUE, SPORTS_KIND_NEXT, SPORTS_KIND_ROUND, SPORTS_KIND_SEARCH,
    SPORTS_KIND_TEAM, SPORTS_KIND_TEAM_NEXT, SportsEvent, SportsFetch, SportsFollow, SportsLeague, SportsLeagueCategory, SportsMatch, SportsState,
    SportsTeam,
)
from .db_utils import retry_on_locked
from .fmt_utils import join_labels
from .program_titles import title_key
from .tz_utils import to_local

log = logging.getLogger(__name__)

BASE_URL = 'https://www.thesportsdb.com/api/v1/json'
#: The free key. A user's own key is deliberately not supported, because that path could never be
#: tested here (design 8). It is not a secret, so nothing masks it.
FREE_KEY = '123'
TIMEOUT_SECONDS = 20
#: The floor under every call, whatever the config says. The docs say 30 calls a minute; the free key
#: answers about 10 from one address: ten calls 2.5 seconds apart were answered and the eleventh drew
#: a 429 (2026-11-14, dev/changelog/1551). 8 seconds is at most 7.5 a minute, which leaves room for
#: another caller behind the same address.
MIN_WAIT_SECONDS = 8
#: What a 429 without a usable Retry-After waits. The measured header said 120, every call inside it
#: was refused, and the first one after it was answered (dev/changelog/1551). The docs say a minute,
#: so a header asking for less is not believed.
DEFAULT_RETRY_AFTER_SECONDS = 120
MIN_RETRY_AFTER_SECONDS = 60
MAX_BACKOFF_SECONDS = 3600
#: Clean calls in a row that halve a backoff.
CLEAN_CALLS_TO_EASE = 5
MISSING_KEEP_DAYS = 7
EMPTY_RECHECK_DAYS = 3
REFUSED_RETRY_HOURS = 6
MATCH_AFTER_REFRESH_SECONDS = 180
#: How often a team's lookup is asked again, so a name added on TheSportsDB reaches the matcher
#: within days instead of next season. About one call a team a week, behind every other call
#: (QUEUE_RANK_REREAD): 144 for college football, 24 minutes of the worker at the default pace.
TEAM_REREAD_DAYS = 7
#: The most rows the free key's eventsround sends. A college football week is bigger, and the answer
#: then carries 50 of its games with nothing saying it was cut. Which 50 changed from call to call on
#: NCAA Division 1 Football, and was the season's first 50 every time on the NBA's round 0 (measured
#: 2026-11-14, dev/changelog/1549 and 1550).
ROUND_ROW_CAP = 50
#: A stored game a cut answer left out is re-read by its number when it starts within this long.
EVENT_RECHECK_DAYS = 7
#: How far past a cut round's first and last stored games a guide listing still counts as in that week.
CAPPED_SPAN_MARGIN = timedelta(hours=13)
#: parse_event's reason for a row of another league. A lookup by name that finds only such a row found
#: another league's game of that name, which is an answer and not a row gone bad (dev/changelog/1552).
WRONG_LEAGUE = 'wrong league'
#: How long before its game a listing can open: one that opens this close to midnight UTC is looked up
#: on both days.
LOOKUP_LEAD = timedelta(hours=1)
#: In a league with a cut round, a team's next game is asked for again this long after it starts, when
#: it is over, so the game after it is found before it begins. eventsnext sends one game, home or away:
#: UTEP and Wyoming both gave the UTEP game round 11 left out, and the Nuggets their away game
#: (measured 2026-11-14, dev/changelog/1553). The docs' "home games only" is wrong.
TEAM_NEXT_AFTER_START = timedelta(hours=4)
#: The soonest a team is asked again, so an answer still naming a game already played (TheSportsDB is
#: slow to mark one finished) is not asked for on every tick.
TEAM_NEXT_MIN_WAIT = timedelta(hours=3)
#: Calls about one game, made after a round answer was cut. They are not the schedule itself, so they
#: never make a league read as still getting it, and Schedule updates shows one only when it failed;
#: the league's notice counts them.
ONE_GAME_KINDS = (SPORTS_KIND_EVENT, SPORTS_KIND_SEARCH, SPORTS_KIND_TEAM_NEXT)

#: The Add league or team picks, in the order the dialog shows them: alphabetical. Names and numbers only,
#: ChannelBin ships no event data (design 8), and following one is always the user's own click.
SHIPPED_LEAGUES = (
    {'tsdb_id': 4328, 'name': 'English Premier League', 'sport': 'Soccer'},
    {'tsdb_id': 4370, 'name': 'Formula 1', 'sport': 'Motorsport'},
    {'tsdb_id': 4393, 'name': 'NASCAR Cup Series', 'sport': 'Motorsport'},
    {'tsdb_id': 4387, 'name': 'NBA', 'sport': 'Basketball'},
    {'tsdb_id': 4391, 'name': 'NFL', 'sport': 'American Football'},
    {'tsdb_id': 4380, 'name': 'NHL', 'sport': 'Ice Hockey'},
)

#: Names guides use for a league that TheSportsDB does not list, by TheSportsDB's league number
#: (design 5.1). All were seen in prod's guide: TheSportsDB calls 5093 `NASCAR Truck Series`, and
#: still calls 4573 `NASCAR Xfinity Series` after its 2025 rename. 4479's event channels say only
#: `NCAAF 27: 1pm Arkansas State at Coastal Carolina`; all 186 channel names carrying the word on
#: 2026-11-14 were college football (dev/changelog/1562). Its guide listings say `College Football`,
#: which neither of TheSportsDB's names for it contains: about 4,800 listings on the weekend of
#: 2026-11-13, when 1 of 55 college games had a guide match (dev/changelog/1577). Seeded when a league is
#: added, and offered on its page while missing.
KNOWN_EXTRA_TERMS = {
    5093: ('NASCAR Craftsman Truck Series',),
    4573: ("NASCAR O'Reilly Auto Parts Series",),
    4479: ('NCAAF', 'College Football'),
}

_client_lock = threading.Lock()


class TryAgain(Exception):
    """A person's lookup came too soon after the last call. Never waits behind the queue (4.1)."""
    def __init__(self, seconds):
        super().__init__(f'try again in {seconds} seconds')
        self.seconds = seconds


class LookupFailed(Exception):
    """A person's lookup could not be answered. The message is written for them."""


class LeagueNotFound(LookupFailed):
    """TheSportsDB answered, and it has no such league."""


@dataclass
class Answer:
    outcome: str
    rows: list = field(default_factory=list)
    reason: str = ''
    #: The answer reached us and carried nothing, as opposed to failing.
    empty: bool = False
    #: Seconds a 429 asked for; 0 on any other answer, and on a 429 that came without one.
    retry_after: int = 0


def retry_after_seconds(value) -> int:
    """A Retry-After header as seconds, kept between a minute and the longest backoff. A missing or
    HTTP-date value is the measured 120."""
    try:
        seconds = int(str(value).strip())
    except (TypeError, ValueError):
        return DEFAULT_RETRY_AFTER_SECONDS
    return min(MAX_BACKOFF_SECONDS, max(MIN_RETRY_AFTER_SECONDS, seconds))


def _call(endpoint: str, params: dict, list_key: str) -> Answer:
    """One call. Never raises, and a failure is a named outcome rather than an empty list: a 429 or
    a refusal must not be read as "no games". Log and reason text is built from the endpoint and its
    parameters, never the URL."""
    url = f'{BASE_URL}/{FREE_KEY}/{endpoint}.php'
    try:
        resp = requests.get(url, params=params, timeout=TIMEOUT_SECONDS,
                            headers={'User-Agent': 'ChannelBin'})
    except requests.Timeout:
        return Answer(SPORTS_FETCH_REFUSED, reason=f'No answer within {TIMEOUT_SECONDS} seconds.')
    except requests.RequestException as exc:
        return Answer(SPORTS_FETCH_REFUSED, reason=f'Could not reach TheSportsDB ({type(exc).__name__}).')
    if resp.status_code == 429:
        wait = retry_after_seconds(resp.headers.get('Retry-After'))
        return Answer(SPORTS_FETCH_RATE_LIMITED, retry_after=wait,
                      reason=f'HTTP 429, too many requests. TheSportsDB asked for {wait} seconds without calls.')
    if resp.status_code != 200:
        return Answer(SPORTS_FETCH_REFUSED,
                      reason=f'HTTP {resp.status_code}, and the answer was not a schedule.')
    try:
        data = resp.json()
    except ValueError:
        return Answer(SPORTS_FETCH_REFUSED, reason='HTTP 200, but the answer was not JSON.')
    rows = data.get(list_key) if isinstance(data, dict) else None
    if isinstance(data, dict) and list_key in data and rows is None:
        return Answer(SPORTS_FETCH_OK, empty=True)
    if not isinstance(rows, list):
        return Answer(SPORTS_FETCH_REFUSED, reason='The answer was not in the expected shape.')
    return Answer(SPORTS_FETCH_OK, rows=[r for r in rows if isinstance(r, dict)], empty=not rows)


# ---------------------------------------------------------------------------
# Pace
# ---------------------------------------------------------------------------

def _settings(cfg: dict = None) -> dict:
    """The sports_data.* values as numbers. A bad value in config.yaml falls back to its default
    and logs, since a worker that stops over a typo says nothing to anyone."""
    section = (cfg if cfg is not None else load_config()).get('sports_data') or {}
    out = {}
    for key, default in (('call_spacing_seconds', 60), ('call_jitter_seconds', 20),
                         ('horizon_days', 21), ('history_keep_days', 30)):
        try:
            out[key] = max(0, int(section.get(key, default)))
        except (TypeError, ValueError):
            log.warning('sports_data.%s is %r, not a whole number - using %d',
                        key, section.get(key), default)
            out[key] = default
    return out


def pace_error(spacing, jitter) -> str:
    """Why this spacing and jitter may not be saved, or '' when they may."""
    if spacing - jitter < MIN_WAIT_SECONDS:
        return (f'Seconds between calls minus the variation has to be at least {MIN_WAIT_SECONDS}: '
                f'TheSportsDB answers about 10 calls a minute, then refuses every call for two minutes. '
                f'This would be {spacing - jitter}.')
    return ''


def _next_wait(settings: dict, backoff: int) -> int:
    wait = max(settings['call_spacing_seconds'], backoff)
    jitter = settings['call_jitter_seconds']
    return max(MIN_WAIT_SECONDS, wait + random.randint(-jitter, jitter))


def _state() -> SportsState:
    """The worker's row, inside the caller's unit. The first one picks this install's time of day."""
    state = db.session.get(SportsState, 1)
    if state is None:
        # Column defaults only land at INSERT, and the caller reads these before that.
        state = SportsState(id=1, backoff_seconds=0, clean_calls=0,
                            daily_offset_seconds=random.randint(0, 86399))
        db.session.add(state)
    return state


def _daily_at(now: datetime, offset_seconds: int, days: int = 1) -> datetime:
    return datetime.combine((now + timedelta(days=days)).date(), time()) + timedelta(seconds=offset_seconds)


def _note_call(state: SportsState, settings: dict, answer: Answer, now: datetime):
    """What any call does to the pace, the worker's or a person's. A 429 waits out the pause
    TheSportsDB asked for, then doubles the spacing up to an hour; clean calls bring it back down."""
    state.last_call_at = now
    limited = answer.outcome == SPORTS_FETCH_RATE_LIMITED
    if limited:
        state.backoff_seconds = min(MAX_BACKOFF_SECONDS,
                                    max(settings['call_spacing_seconds'], state.backoff_seconds) * 2)
        state.clean_calls = 0
    elif state.backoff_seconds:
        state.clean_calls = (state.clean_calls or 0) + 1
        if state.clean_calls >= CLEAN_CALLS_TO_EASE:
            halved = state.backoff_seconds // 2
            state.backoff_seconds = 0 if halved <= settings['call_spacing_seconds'] else halved
            state.clean_calls = 0
    wait = _next_wait(settings, state.backoff_seconds)
    if limited:
        # Every call inside the pause is refused as well, so the first one after a 429 waits it out.
        wait = max(wait, answer.retry_after or DEFAULT_RETRY_AFTER_SECONDS)
    earliest = now + timedelta(seconds=wait)
    state.next_call_at = max(state.next_call_at, earliest) if state.next_call_at else earliest


# ---------------------------------------------------------------------------
# Reading an answer
# ---------------------------------------------------------------------------

def _int(value) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _round_number(value):
    """intRound as a number, or None when it is not one. 0 is a real round: the NBA's whole regular
    season is round 0 (measured 2026-11-14, dev/changelog/1550)."""
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _text(value):
    return (value or '').strip() or None if isinstance(value, str) else None


def _parse_when(value):
    """A naive-UTC datetime from strTimestamp, which is UTC, or None."""
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.strip().replace('Z', '+00:00'))
    except ValueError:
        return None
    if parsed.tzinfo is not None:
        parsed = (parsed - parsed.utcoffset()).replace(tzinfo=None)
    return parsed


def _parse_date(value):
    if not isinstance(value, str):
        return None
    try:
        return datetime.strptime(value.strip(), '%Y-%m-%d').date()
    except ValueError:
        return None


def parse_event(row: dict, league_tsdb_id: int):
    """The fields kept from one event, or (None, why) for a row that fails validation at import. A
    game's time is strTimestamp, never strTimeLocal. `dateEventLocal` is only a date to fall back on
    (design 7)."""
    tsdb_id = _int(row.get('idEvent'))
    if not tsdb_id:
        return None, 'no event id'
    if _int(row.get('idLeague')) != league_tsdb_id:
        return None, WRONG_LEAGUE
    start = _parse_when(row.get('strTimestamp'))
    local_date = _parse_date(row.get('dateEventLocal')) or _parse_date(row.get('dateEvent'))
    if start is None and local_date is None:
        return None, 'no date'
    name = _text(row.get('strEvent'))
    if name is None:
        return None, 'no name'
    return {
        'tsdb_id': tsdb_id, 'season': _text(row.get('strSeason')), 'round': _round_number(row.get('intRound')),
        'name': name, 'home_team_id': _int(row.get('idHomeTeam')), 'away_team_id': _int(row.get('idAwayTeam')),
        'home_name': _text(row.get('strHomeTeam')), 'away_name': _text(row.get('strAwayTeam')),
        'venue': _text(row.get('strVenue')), 'city': _text(row.get('strCity')),
        'start_at': start, 'local_date': local_date or start.date(), 'status': _text(row.get('strStatus')),
        'postponed': (_text(row.get('strPostponed')) or '').lower() == 'yes',
        'poster_url': _text(row.get('strPoster')), 'thumb_url': _text(row.get('strThumb')),
    }, ''


def _team_names(row: dict):
    """(names, city) from one lookupteam row (6.2). The city is the full name with the nickname off
    its end; strLocation is not used, since Southampton's reads like a street address."""
    full = _text(row.get('strTeam')) or ''
    alternates = [a.strip() for a in (_text(row.get('strTeamAlternate')) or '').split(',') if a.strip()]
    names = list(dict.fromkeys([full] + alternates))
    city = None
    for alt in alternates:
        if full.lower().endswith(' ' + alt.lower()):
            city = full[:-len(alt)].strip()
            break
    return names, city


_COLOR_RE = re.compile(r'^#[0-9a-fA-F]{6}$')


def _team_look(row: dict) -> dict:
    """A team's colors, short name and badge address from one lookupteam row, as SportsTeam's
    columns. Measured 2026-11-13 (DESIGN-sports-data.md 2): colors are `#RRGGBB` in either case,
    and a team without one sends '', as it does for a missing short name. Anything else in a color
    is stored as unknown and named in the log, so a screen drawn in gray can be explained."""
    out = {}
    for n in (1, 2, 3):
        value = _text(row.get(f'strColour{n}'))
        if value is not None and not _COLOR_RE.match(value):
            log.warning('TheSportsDB team %s (%s): strColour%d %r is not a #RRGGBB color, so it is '
                        'drawn as unknown', row.get('idTeam'), row.get('strTeam'), n, value)
            value = None
        out[f'color{n}'] = value.lower() if value else None
    short = _text(row.get('strTeamShort'))
    out['short_name'] = short[:32] if short else None
    out['badge_url'] = _text(row.get('strBadge'))
    return out


def event_shape_is_matchup(event) -> bool:
    """An event with team ids is a matchup; one with none is a field event, such as a race. The shape
    comes from the event, never a list of sports (6.1)."""
    return bool(event.home_team_id or event.away_team_id)


def event_game_name(event) -> str:
    """What an event is called where one line names it: 'Away at Home' for a matchup that names
    both, else TheSportsDB's own name."""
    if event_shape_is_matchup(event) and event.away_name and event.home_name:
        return f'{event.away_name} at {event.home_name}'
    return event.name


# ---------------------------------------------------------------------------
# The queue
# ---------------------------------------------------------------------------

def _ensure_fetch(target: str, kind: str, league_id: int, now: datetime) -> SportsFetch:
    """The queue row for this target, due now if it is new or has nothing scheduled. No outbound
    work happens here, so it is safe inside a caller's commit unit."""
    row = SportsFetch.query.filter_by(target=target).first()
    if row is None:
        row = SportsFetch(target=target, kind=kind, league_id=league_id, next_due_at=now)
        db.session.add(row)
    elif row.next_due_at is None:
        row.next_due_at = now
    return row


#: The order due calls are made in (4.1). Calls that find games go first; a team's lookup that has not
#: had a good answer yet, which adds its other names, colors and badge, waits for them; and one whose
#: last answer was good, the weekly re-read, goes last. A league followed on 2026-11-13 waited three hours for one round call
#: behind 160 team lookups (dev/changelog/1574). An unfinished recording's poster and background take
#: their turn between the first and the second, in tick().
QUEUE_RANK_SCHEDULE, QUEUE_RANK_TEAM, QUEUE_RANK_REREAD = 0, 1, 2


def _due_query(now: datetime):
    """Every due queue row, in the order tick() takes them. The one place that order is written, so
    what a page says about the line (queue_line) cannot drift from what the worker does."""
    rank = case((SportsFetch.kind != SPORTS_KIND_TEAM, QUEUE_RANK_SCHEDULE),
                (or_(SportsFetch.outcome.is_(None), SportsFetch.outcome != SPORTS_FETCH_OK), QUEUE_RANK_TEAM),
                else_=QUEUE_RANK_REREAD).label('rank')
    return (db.session.query(SportsFetch.id, SportsFetch.target, SportsFetch.kind, SportsLeague.tsdb_id,
                             SportsFetch.league_id, SportsFetch.last_at, rank)
            .join(SportsLeague, SportsLeague.id == SportsFetch.league_id)
            .filter(SportsFetch.next_due_at.isnot(None), SportsFetch.next_due_at <= now)
            .order_by(rank, SportsFetch.next_due_at, SportsFetch.id))


def queue_line(now: datetime) -> list:
    """[(league id, kind, never run)] for every due call, first in line first. One query."""
    return [(r.league_id, r.kind, r.last_at is None) for r in _due_query(now).all()]


def seconds_until_call(position: int, now: datetime, settings: dict = None) -> int:
    """About how long until the call at `position` in the line (0 is next) is made: what is left of
    the worker's current wait, then one spacing per call ahead. The backed-off spacing while a 429
    is being waited out. An image's turn is not counted, so this is a floor."""
    settings = settings or _settings()
    state = db.session.get(SportsState, 1)
    spacing = max(settings['call_spacing_seconds'], state.backoff_seconds if state is not None else 0)
    left = (state.next_call_at - now).total_seconds() if state is not None and state.next_call_at else 0
    return int(max(0, left) + position * spacing)


def _enqueue_league(league: SportsLeague, now: datetime):
    """Register what a follow obliges, in the follow's own commit and before any call: a restart
    resumes from these rows rather than guessing from what is stored (data-model rule)."""
    _ensure_fetch(f'league:{league.tsdb_id}', SPORTS_KIND_LEAGUE, league.id, now)
    _ensure_fetch(f'next:{league.tsdb_id}', SPORTS_KIND_NEXT, league.id, now)
    state = _state()
    if state.next_call_at is None or state.next_call_at < now:
        state.next_call_at = now


def _round_next_due(starts: list, now: datetime, settings: dict, offset: int):
    """When to ask for this round again (4.2), from its events' starts. None means every game has
    been played, so there is nothing left to ask."""
    ahead = [s for s in starts if s is not None and s > now]
    if not ahead:
        return None
    first = min(ahead)
    until = first - now
    horizon = timedelta(days=settings['horizon_days'])
    if until > horizon:
        due = first - horizon
    elif until <= timedelta(days=7):
        due = _daily_at(now, offset)
    else:
        due = _daily_at(now, offset, days=3)
    just_before = first - timedelta(hours=6)
    return min(due, just_before) if just_before > now else due


def _record_outcome(row: SportsFetch, outcome: str, reason: str, now: datetime):
    failing = outcome in (SPORTS_FETCH_REFUSED, SPORTS_FETCH_RATE_LIMITED)
    row.failing_since = (row.failing_since or now) if failing else None
    row.outcome = outcome
    row.reason = reason or None
    row.last_at = now


def _parse_rows(rows: list, league: SportsLeague) -> tuple:
    """(parsed, {why: count dropped}) for one answer's rows."""
    parsed, dropped, seen_ids = [], {}, set()
    for raw in rows:
        event, why = parse_event(raw, league.tsdb_id)
        if event is None:
            dropped[why] = dropped.get(why, 0) + 1
        elif event['tsdb_id'] in seen_ids:
            dropped['a repeated id'] = dropped.get('a repeated id', 0) + 1
        else:
            seen_ids.add(event['tsdb_id'])
            parsed.append(event)
    return parsed, dropped


def _store_parsed(league: SportsLeague, parsed: list, now: datetime):
    """Write each parsed event over its stored copy, or as a new one. Being in an answer is what
    clears a game's missing mark. A game that had started when TheSportsDB first moved it keeps the
    start it had before then as its previous one, whatever moves follow: a delay entered in steps
    (8:00, 9:00, 10:00, 10:30) would otherwise stop reading as a started game once the steps passed
    OFF_LIMIT, and be shifted forward over the game on the air (dev/changelog/1632)."""
    from .sports_match import kept_start
    ids = [e['tsdb_id'] for e in parsed]
    existing = {e.tsdb_id: e for e in SportsEvent.query.filter(SportsEvent.tsdb_id.in_(ids)).all()} if ids else {}
    for fields in parsed:
        event = existing.get(fields['tsdb_id'])
        if event is None:
            event = SportsEvent(tsdb_id=fields['tsdb_id'], league_id=league.id, first_seen=now)
            db.session.add(event)
        elif (fields['start_at'] and event.start_at and fields['start_at'] != event.start_at):
            if not kept_start(event):
                event.previous_start = event.start_at
            event.start_changed_at = now
        for key, value in fields.items():
            setattr(event, key, value)
        event.league_id = league.id
        event.last_seen = now
        event.missing_since = None


def _recheck_left_out(league: SportsLeague, season: str, number: int, ids: list, now: datetime):
    """After a cut answer: the round's stored games it left out, starting within EVENT_RECHECK_DAYS,
    each get one lookup by number, so a moved time still reaches the matcher. Their missing marks are
    cleared, since a cut answer is no evidence; the lookup marks a game again if TheSportsDB has
    really dropped it."""
    left_out = SportsEvent.query.filter(
        SportsEvent.league_id == league.id, SportsEvent.season == season, SportsEvent.round == number,
        SportsEvent.tsdb_id.notin_(ids)).all()
    soon = now + timedelta(days=EVENT_RECHECK_DAYS)
    for event in left_out:
        event.missing_since = None
        start = event.start_at or datetime.combine(event.local_date, time(23, 59))
        if now - timedelta(hours=3) <= start <= soon:
            _ensure_fetch(f'event:{event.tsdb_id}', SPORTS_KIND_EVENT, league.id, now)


def _apply_round(row: SportsFetch, league: SportsLeague, answer: Answer, season: str, number: int,
                 settings: dict, offset: int, now: datetime):
    parsed, dropped = _parse_rows(answer.rows, league)
    ids = [e['tsdb_id'] for e in parsed]
    _store_parsed(league, parsed, now)
    capped = len(answer.rows) >= ROUND_ROW_CAP

    # A smaller answer is named and deletes nothing. A stored event missing from a good answer may
    # have moved rounds, so it is marked rather than dropped, and only when the answer held games.
    # A cut answer marks nothing: which 50 games it carries can change from call to call.
    if capped:
        _recheck_left_out(league, season, number, ids, now)
    elif parsed:
        gone = SportsEvent.query.filter(
            SportsEvent.league_id == league.id, SportsEvent.season == season,
            SportsEvent.round == number, SportsEvent.missing_since.is_(None),
            SportsEvent.tsdb_id.notin_(ids))
        for event in gone.all():
            event.missing_since = now

    count = len(parsed)
    previous = row.row_count
    starts = [e['start_at'] or datetime.combine(e['local_date'], time()) for e in parsed]
    if capped and not any(s > now for s in starts) and _is_current_round(league, season, number, now):
        # The answer is the round's first 50 and the round is still the league's current one, so its
        # games go on past the cut: the NBA files its whole season as round 0, and the same 50 come
        # back every time (measured 2026-11-14). A college week re-read once it is over is not: the
        # league's next game is in a later round (dev/docs/BUGS.md 2026-11-14 @ 05:42:50 pm).
        outcome = SPORTS_FETCH_CAPPED_PAST
        reason = (f'{len(answer.rows)} rows, the most TheSportsDB sends for one call, and every one had already '
                  f'started, so the rest of this round comes after them and will not be sent. Each team is asked '
                  f'for its next game instead, again after each game it plays.')
    elif capped:
        outcome = SPORTS_FETCH_CAPPED
        reason = (f'{len(answer.rows)} rows, the most TheSportsDB sends for one call, so this round may have more '
                  f'games than came back. A game left out is not marked missing, and each team with no game in '
                  f'the answer is asked for its next one.')
    elif previous is not None and count < previous:
        outcome = SPORTS_FETCH_SHRUNK
        reason = f'{count} rows, down from {previous}. Nothing was deleted, in case the answer is wrong.'
    elif count == 0:
        outcome = SPORTS_FETCH_EMPTY
        reason = 'Nothing listed for this round yet.'
    else:
        outcome = SPORTS_FETCH_OK
        reason = ''
    if dropped:
        said = ', '.join(f'{n} with {why}' for why, n in sorted(dropped.items()))
        reason = f'{reason} {sum(dropped.values())} rows dropped: {said}.'.strip()
        log.warning('TheSportsDB eventsround league=%s round=%s: %s', league.tsdb_id, number, reason)
    _record_outcome(row, outcome, reason, now)
    row.prev_row_count = previous
    # The smaller count becomes the one to beat, so a game really gone is named once, not daily.
    row.row_count = count
    row.dropped_count = sum(dropped.values())

    if not parsed:
        # May just not be published yet, so it is asked again, but never a past season's: a round
        # that does not exist, such as NFL 19, would otherwise make calls for as long as it is followed.
        row.next_due_at = now + timedelta(days=EMPTY_RECHECK_DAYS) if season == league.current_season else None
    else:
        row.next_due_at = _round_next_due(starts, now, settings, offset)

    # Walk forward until an event falls past the horizon or a round comes back empty (4.3).
    if parsed and starts and max(starts) <= now + timedelta(days=settings['horizon_days']):
        _ensure_fetch(f'round:{league.tsdb_id}:{season}:{number + 1}', SPORTS_KIND_ROUND, league.id, now)
    _ensure_team_rows(league, parsed, now)
    if outcome in (SPORTS_FETCH_CAPPED, SPORTS_FETCH_CAPPED_PAST):
        _ask_teams_next(league, _teams_without_a_game(league, season, number, outcome, now), now)


def _is_current_round(league: SportsLeague, season: str, number: int, now: datetime) -> bool:
    """Whether the league's soonest stored game still to come is in this round. eventsnextleague's
    game is stored, so this is the round TheSportsDB says is next. Nothing stored ahead reads as
    current, which is what a cut answer of played games meant before this was asked."""
    soonest = (db.session.query(SportsEvent.round)
               .filter(SportsEvent.league_id == league.id, SportsEvent.season == season, SportsEvent.start_at > now)
               .order_by(SportsEvent.start_at).first())
    return soonest is None or soonest[0] == number


def _teams_without_a_game(league: SportsLeague, season: str, number: int, outcome: str, now: datetime) -> set:
    """The league's teams a cut answer may have left a game out for: those with no stored game in the
    round, or, when the round runs on past the cut, with no stored game still to come. A team is known
    from the league's stored games, since lookup_all_teams answers every league with the same English
    League 1 sides on the free key (measured 2026-11-14)."""
    teams = set(_league_team_ids(league.id))
    covered = db.session.query(SportsEvent.home_team_id, SportsEvent.away_team_id).filter(SportsEvent.league_id == league.id)
    if outcome == SPORTS_FETCH_CAPPED_PAST:
        covered = covered.filter(or_(SportsEvent.start_at > now,
                                     and_(SportsEvent.start_at.is_(None), SportsEvent.local_date >= now.date())))
    else:
        covered = covered.filter(SportsEvent.season == season, SportsEvent.round == number)
    return teams - {i for pair in covered.all() for i in pair if i}


def _teams_followed(league: SportsLeague):
    """The team ids a follow of this league covers, or None for the whole league."""
    ids = {t for (t,) in db.session.query(SportsFollow.team_tsdb_id).filter_by(league_id=league.id).all()}
    return None if 0 in ids else ids


def _league_is_cut(league: SportsLeague) -> bool:
    """Whether a round of the league's current season last came back cut. A team's next game is asked
    for again after each game only while one has."""
    return league.current_season is not None and SportsFetch.query.filter(
        SportsFetch.league_id == league.id, SportsFetch.kind == SPORTS_KIND_ROUND,
        SportsFetch.target.like(f'round:{league.tsdb_id}:{league.current_season}:%'),
        SportsFetch.outcome.in_((SPORTS_FETCH_CAPPED, SPORTS_FETCH_CAPPED_PAST))).first() is not None


def _ask_teams_next(league: SportsLeague, team_ids: set, now: datetime):
    """A row asking each team for its next game, due now, for the followed teams among `team_ids`. A row
    already waiting keeps its time: it is set for after the game its last answer named, and that game is
    the team's next. One that stopped is made due again after EMPTY_RECHECK_DAYS."""
    followed = _teams_followed(league)
    wanted = {t for t in team_ids if t and (followed is None or t in followed)}
    if not wanted:
        return
    targets = {f'teamnext:{league.tsdb_id}:{t}': t for t in wanted}
    rows = {r.target: r for r in SportsFetch.query.filter(SportsFetch.target.in_(list(targets))).all()}
    for target in sorted(targets):
        row = rows.get(target)
        if row is None:
            db.session.add(SportsFetch(target=target, kind=SPORTS_KIND_TEAM_NEXT, league_id=league.id, next_due_at=now))
        elif row.next_due_at is None and (row.last_at is None or now - row.last_at >= timedelta(days=EMPTY_RECHECK_DAYS)):
            row.next_due_at = now


def _raw_start(row: dict):
    """When an answer row's game starts, from the raw row, for a game parse_event does not keep."""
    start = _parse_when(row.get('strTimestamp'))
    if start is None:
        day = _parse_date(row.get('dateEventLocal')) or _parse_date(row.get('dateEvent'))
        start = datetime.combine(day, time(23, 59)) if day else None
    return start


def _apply_team_next(row, league, answer, team_id, now):
    """One team's next game. Asked again once that game is over, while the league has a cut round and
    the team is followed, so every game is some team's next one before it starts."""
    parsed, dropped = _parse_rows(answer.rows, league)
    elsewhere = dropped.pop(WRONG_LEAGUE, 0)
    row.row_count = len(answer.rows)
    row.dropped_count = sum(dropped.values())
    starts = [s for s in (_raw_start(r) for r in answer.rows) if s is not None]
    followed = _teams_followed(league)
    again = _league_is_cut(league) and (followed is None or team_id in followed)
    if starts and again:
        row.next_due_at = max(max(starts) + TEAM_NEXT_AFTER_START, now + TEAM_NEXT_MIN_WAIT)
    else:
        row.next_due_at = now + timedelta(days=EMPTY_RECHECK_DAYS) if again else None
    if parsed:
        game = parsed[0]
        stored = SportsEvent.query.filter_by(tsdb_id=game['tsdb_id']).first() is not None
        _store_parsed(league, parsed, now)
        _ensure_team_rows(league, parsed, now)
        said = f'Its next game, {game["name"]}, was already stored.' if stored else f'Found {game["name"]}, which the schedule had left out.'
        _record_outcome(row, SPORTS_FETCH_OK, said, now)
        return
    if dropped:
        why = ', '.join(sorted(dropped))
        _record_outcome(row, SPORTS_FETCH_OK, f'Its answer could not be used ({why}).', now)
        return
    if elsewhere:
        others = sorted({_text(r.get('strLeague')) for r in answer.rows if _text(r.get('strLeague'))})
        where = f', {join_labels(others)}' if others else ''
        _record_outcome(row, SPORTS_FETCH_EMPTY, f'Its next game is in another league{where}.', now)
        return
    _record_outcome(row, SPORTS_FETCH_EMPTY, 'TheSportsDB lists no next game for this team.', now)


def _ensure_team_rows(league: SportsLeague, parsed: list, now: datetime):
    """A row per new team's lookup (6.2). Until it runs only the full name counts. A team whose
    weekly re-read lapsed, because it had no stored game when its turn came, is asked again when a
    game of its arrives."""
    wanted = {}
    for e in parsed:
        if e['home_team_id']:
            wanted[e['home_team_id']] = e['home_name']
        if e['away_team_id']:
            wanted[e['away_team_id']] = e['away_name']
    if not wanted:
        return
    known = {t.tsdb_id: t for t in SportsTeam.query.filter(SportsTeam.tsdb_id.in_(list(wanted))).all()}
    for tsdb_id, name in wanted.items():
        team = known.get(tsdb_id)
        if team is None:
            team = SportsTeam(tsdb_id=tsdb_id, full_name=name or str(tsdb_id),
                              names=json.dumps([name] if name else []))
            db.session.add(team)
        if team.last_looked_up is None or now - team.last_looked_up >= timedelta(days=TEAM_REREAD_DAYS):
            _ensure_fetch(f'team:{tsdb_id}', SPORTS_KIND_TEAM, league.id, now)


def _apply_next(row, league, answer, settings, offset, now):
    row.next_due_at = _daily_at(now, offset)
    if not answer.rows:
        _record_outcome(row, SPORTS_FETCH_EMPTY, 'TheSportsDB lists nothing next for this league.', now)
        row.row_count = 0
        return
    raw = answer.rows[0]
    event, why = parse_event(raw, league.tsdb_id)
    row.row_count = len(answer.rows)
    row.dropped_count = 0
    if event is None:
        row.dropped_count = 1
        _record_outcome(row, SPORTS_FETCH_OK, f'Its next event could not be read: {why}.', now)
        return
    season, number = event['season'], event['round']
    if not season or number is None:
        row.dropped_count = 1
        _record_outcome(row, SPORTS_FETCH_OK, 'Its next event has no season or round, so its schedule could not be asked for.', now)
        return
    stored = SportsEvent.query.filter_by(tsdb_id=event['tsdb_id']).first() is not None
    # Stored, so the round it names reads as the league's current one (_is_current_round) even when a
    # cut answer of that round leaves it out.
    _store_parsed(league, [event], now)
    _ensure_team_rows(league, [event], now)
    # The self-check: whatever its number, this is what finds NFL round 160 in January without a
    # list of rounds, since rounds are not consecutive (4.3). It runs for a stored event too, because
    # following a league again has no round rows left, and the walk forward starts here.
    _ensure_fetch(f'round:{league.tsdb_id}:{season}:{number}', SPORTS_KIND_ROUND, league.id, now)
    _record_outcome(row, SPORTS_FETCH_OK, 'Already stored.' if stored else '', now)


def _apply_league(row, league, answer, offset, now):
    row.next_due_at = _daily_at(now, offset, days=7)
    if not answer.rows:
        _record_outcome(row, SPORTS_FETCH_EMPTY, 'TheSportsDB no longer lists this league.', now)
        return
    raw = answer.rows[0]
    league.name = _text(raw.get('strLeague')) or league.name
    league.sport = _text(raw.get('strSport')) or league.sport
    if row.last_at is None:
        # A league added from a team search has only its name to go on until this first call.
        known = json.loads(league.match_terms or '[]')
        seen = {t.lower() for t in known}
        league.match_terms = json.dumps(known + [t for t in _seed_terms(raw) if t.lower() not in seen])
    season = _text(raw.get('strCurrentSeason'))
    if season and season != league.current_season:
        old_rounds = f'round:{league.tsdb_id}:{league.current_season}:%'
        SportsFetch.query.filter(SportsFetch.target.like(old_rounds), SportsFetch.outcome == SPORTS_FETCH_EMPTY).update(
            {SportsFetch.next_due_at: None}, synchronize_session=False)
        league.current_season = season
        # Teams are not asked again here: each is re-read weekly (TEAM_REREAD_DAYS), and clearing
        # last_looked_up would drop every team to its full name until its turn came round.
    _record_outcome(row, SPORTS_FETCH_OK, '', now)


def known_team_ids(league_id: int) -> set:
    """The teams ChannelBin knows play in this league: those in its stored games, and those followed
    in it already. A follow may name one of these without a fresh lookup."""
    followed = {t for (t,) in db.session.query(SportsFollow.team_tsdb_id).filter_by(league_id=league_id) if t}
    return set(_league_team_ids(league_id)) | followed


def _league_team_ids(league_id: int) -> list:
    rows = (db.session.query(SportsEvent.home_team_id, SportsEvent.away_team_id)
            .filter(SportsEvent.league_id == league_id).distinct().all())
    return list({i for pair in rows for i in pair if i})


def _apply_team(row, answer, tsdb_id, now):
    # Asked again in a week while the league still stores a game of the team's; a team with none
    # stops, and _ensure_team_rows() starts it again when one arrives. A person's own names are
    # another column and are never touched here.
    plays = tsdb_id in _league_team_ids(row.league_id)
    row.next_due_at = now + timedelta(days=TEAM_REREAD_DAYS) if plays and answer.rows else None
    team = SportsTeam.query.filter_by(tsdb_id=tsdb_id).first()
    if not answer.rows:
        # Asked and answered: its full name from the schedule stands until a game of its arrives a
        # week or more from now.
        if team is not None:
            team.last_looked_up = now
        _record_outcome(row, SPORTS_FETCH_EMPTY, 'TheSportsDB has no team by this number.', now)
        return
    names, city = _team_names(answer.rows[0])
    if team is None:
        team = SportsTeam(tsdb_id=tsdb_id, full_name=names[0] if names else str(tsdb_id))
        db.session.add(team)
    if names:
        team.full_name = names[0]
        team.names = json.dumps(names)
    team.city = city
    for column, value in _team_look(answer.rows[0]).items():
        setattr(team, column, value)
    team.last_looked_up = now
    _record_outcome(row, SPORTS_FETCH_OK, '', now)


def _apply_event(row, league, answer, tsdb_id, now):
    """One stored game re-read by its number. An empty answer is the one thing here that marks a game
    missing: it was asked for by itself, not left out of a cut list. Some other event in the answer
    says nothing about this one."""
    row.next_due_at = None
    event = SportsEvent.query.filter_by(tsdb_id=tsdb_id).first()
    parsed, dropped = _parse_rows([r for r in answer.rows if _int(r.get('idEvent')) == tsdb_id], league)
    row.row_count = len(answer.rows)
    row.dropped_count = sum(dropped.values())
    if parsed:
        _store_parsed(league, parsed, now)
        _ensure_team_rows(league, parsed, now)
        _record_outcome(row, SPORTS_FETCH_OK, '', now)
        return
    if dropped:
        why = ', '.join(sorted(dropped))
        _record_outcome(row, SPORTS_FETCH_OK, f'Its answer could not be used ({why}), so the stored copy stands.', now)
        return
    if answer.rows:
        _record_outcome(row, SPORTS_FETCH_OK, 'TheSportsDB answered with a different event, so the stored copy stands.', now)
        return
    if event is not None and event.missing_since is None:
        event.missing_since = now
    _record_outcome(row, SPORTS_FETCH_EMPTY, 'TheSportsDB has no event by this number any more. It is kept for '
                    f'{MISSING_KEEP_DAYS} days in case it comes back.', now)


def _apply_search(row, league, answer, name, now):
    """One game the guide names, by its name on TheSportsDB. Asked once: not found stays an answer."""
    row.next_due_at = None
    parsed, dropped = _parse_rows(answer.rows, league)
    elsewhere = dropped.pop(WRONG_LEAGUE, 0)
    row.row_count = len(answer.rows)
    row.dropped_count = sum(dropped.values())
    if parsed:
        _store_parsed(league, parsed, now)
        _ensure_team_rows(league, parsed, now)
        _record_outcome(row, SPORTS_FETCH_OK, f'Found {parsed[0]["name"]}.', now)
        return
    if dropped:
        why = ', '.join(sorted(dropped))
        _record_outcome(row, SPORTS_FETCH_OK, f'Its answer could not be used ({why}).', now)
        return
    on = 'that day' if _is_day(search_parts(row.target)[0]) else 'this season'
    if elsewhere:
        # The listing was another league's game: prod's college football channels carry basketball
        # (dev/docs/BUGS.md 2026-11-14 @ 11:28:49 am).
        others = sorted({_text(r.get('strLeague')) for r in answer.rows
                         if _int(r.get('idLeague')) != league.tsdb_id and _text(r.get('strLeague'))})
        where = f', {join_labels(others)}' if others else ''
        _record_outcome(row, SPORTS_FETCH_EMPTY, f'TheSportsDB has a game named "{name}" {on} only in another '
                        f'league{where}, so it is not a {league.name} game.', now)
        return
    _record_outcome(row, SPORTS_FETCH_EMPTY, f'TheSportsDB has no game named "{name}" {on}.', now)


def capped_spans(league_id: int) -> list:
    """[(lo, hi)] around each of the league's rounds whose last answer was cut, from its stored
    games' first and last starts, CAPPED_SPAN_MARGIN wider each side. A round cut with every game
    already started has no end in sight, so its hi is datetime.max. Two queries."""
    capped = {tuple(f.target.split(':')[2:4]): f.outcome for f in SportsFetch.query.filter(
        SportsFetch.league_id == league_id, SportsFetch.kind == SPORTS_KIND_ROUND,
        SportsFetch.outcome.in_((SPORTS_FETCH_CAPPED, SPORTS_FETCH_CAPPED_PAST))).all()}
    if not capped:
        return []
    rows = (db.session.query(SportsEvent.season, SportsEvent.round, db.func.min(SportsEvent.start_at),
                             db.func.max(SportsEvent.start_at))
            .filter(SportsEvent.league_id == league_id, SportsEvent.start_at.isnot(None))
            .group_by(SportsEvent.season, SportsEvent.round).all())
    out = []
    for season, number, lo, hi in rows:
        outcome = capped.get((season, str(number)))
        if outcome is not None:
            open_top = outcome == SPORTS_FETCH_CAPPED_PAST
            out.append((lo - CAPPED_SPAN_MARGIN, datetime.max if open_top else hi + CAPPED_SPAN_MARGIN))
    return out


def lookup_days(start: datetime) -> list:
    """The UTC days to look a listing's game up on. searchevents' `d` is TheSportsDB's dateEvent,
    the UTC date (measured 2026-11-14), and a listing can open up to an hour before a game that
    starts after midnight UTC, which is 7 PM Eastern, so such a listing asks both days."""
    days = [start.date().isoformat()]
    after = (start + LOOKUP_LEAD).date().isoformat()
    return days + [after] if after != days[0] else days


@retry_on_locked()
def queue_guide_lookups(league_id: int, wants: list, now: datetime) -> int:
    """A search row for each (UTC day, home tsdb id, away tsdb id) the matcher found in the guide with
    no stored game near it, where a round answer was cut. Each pair is asked once a day: a row that
    exists is never made due again, so "not found" stays an answer. By day, never by season: two
    teams can meet several times a season, and a search by season finds whichever one it likes
    (dev/changelog/1550). Only for a followed league. Returns how many were queued."""
    league = db.session.get(SportsLeague, league_id)
    followed = SportsFollow.query.filter_by(league_id=league_id).first() is not None
    targets = {f'search:{league.tsdb_id}:{day}:{home}:{away}' for day, home, away in wants} if league else set()
    if not (followed and targets):
        db.session.rollback()
        return 0
    have = {t for (t,) in db.session.query(SportsFetch.target).filter(SportsFetch.target.in_(list(targets))).all()}
    new = sorted(targets - have)
    for target in new:
        db.session.add(SportsFetch(target=target, kind=SPORTS_KIND_SEARCH, league_id=league_id, next_due_at=now))
    db.session.commit()
    return len(new)


def search_name(home: str, away: str) -> str:
    """The name searchevents finds a game by: TheSportsDB's own `Home vs Away`, home team first,
    the reverse of a TV listing's `Away at Home` (measured on NCAA Division 1 Football 2026-11-14)."""
    return f'{home} vs {away}'


_DAY_RE = re.compile(r'^\d{4}-\d{2}-\d{2}$')


def _is_day(when: str) -> bool:
    """A search target's third part is a UTC day (`2026-11-15`). Rows written by dev/changelog/1549
    hold a season (`2026`, `2026-2027`) there instead, and are still read as one until they prune."""
    return bool(_DAY_RE.match(when))


def search_parts(target: str) -> tuple:
    """(day or season, home tsdb id, away tsdb id) from a `search:<league>:<day>:<home>:<away>`
    target. Neither a day nor a season holds a colon."""
    _, _, when, home, away = target.split(':')
    return when, int(home), int(away)


def _search_teams(target: str) -> tuple:
    """(day or season, the name to search for) for a search row, from the stored team names."""
    when, home_id, away_id = search_parts(target)
    names = dict(db.session.query(SportsTeam.tsdb_id, SportsTeam.full_name)
                 .filter(SportsTeam.tsdb_id.in_([home_id, away_id])).all())
    return when, search_name(names.get(home_id, str(home_id)), names.get(away_id, str(away_id)))


def _call_for(kind: str, target: str, tsdb_league_id: int) -> Answer:
    parts = target.split(':')
    if kind == SPORTS_KIND_LEAGUE:
        return _call('lookupleague', {'id': tsdb_league_id}, 'leagues')
    if kind == SPORTS_KIND_NEXT:
        return _call('eventsnextleague', {'id': tsdb_league_id}, 'events')
    if kind == SPORTS_KIND_ROUND:
        return _call('eventsround', {'id': tsdb_league_id, 's': parts[2], 'r': parts[3]}, 'events')
    if kind == SPORTS_KIND_TEAM:
        return _call('lookupteam', {'id': parts[1]}, 'teams')
    if kind == SPORTS_KIND_EVENT:
        return _call('lookupevent', {'id': parts[1]}, 'events')
    if kind == SPORTS_KIND_TEAM_NEXT:
        return _call('eventsnext', {'id': parts[2]}, 'events')
    if kind == SPORTS_KIND_SEARCH:
        when, name = _search_teams(target)
        db.session.rollback()  # holds no read transaction across the outbound call
        # TheSportsDB's own spelling of a name in a URL: underscores for spaces. The key is `event`,
        # singular, unlike every other schedule call (measured 2026-11-14).
        return _call('searchevents', {'e': name.replace(' ', '_'), 'd' if _is_day(when) else 's': when}, 'event')
    raise ValueError(f'unknown sports fetch kind {kind!r}')


def tick(now: datetime = None) -> str:
    """Make the one call that is due, if any. Returns what happened, for the job's log and tests."""
    if not _client_lock.acquire(blocking=False):
        return 'busy'
    try:
        now = now or datetime.utcnow()
        cfg = load_config()
        settings = _settings(cfg)
        _prune_if_due(cfg, settings, now)
        state = db.session.get(SportsState, 1)
        if state is not None:
            if state.next_call_at and state.next_call_at > now:
                return 'waiting'
            if state.last_call_at and (now - state.last_call_at).total_seconds() < MIN_WAIT_SECONDS:
                return 'waiting'
        # A followed game near its time is read every 30 minutes, so a move entered during it is seen
        # (app/sports_reads.py, dev/changelog/1632). Before the pick, so a read it brings forward is the
        # call this tick makes.
        from .sports_reads import arm_guarded
        arm_guarded(now, cfg)
        due = _due_query(now).first()
        db.session.rollback()  # holds no read transaction across the outbound call
        if due is None or due.rank >= QUEUE_RANK_TEAM:
            # An image a recording is waiting on has a deadline, the end of its recording, so it goes
            # ahead of every team lookup; a team's badge waits until no call at all is due. At the
            # same pace either way (app/sports_images.py).
            from .sports_images import fetch_next
            image = fetch_next(cfg, settings, now, recordings_only=due is not None)
            if image or due is None:
                return image or 'idle'
        fetch_id, target, kind, league_tsdb_id = due.id, due.target, due.kind, due.tsdb_id
        answer = _call_for(kind, target, league_tsdb_id)
        log.info('TheSportsDB %s: %s%s', target, answer.outcome, f' ({answer.reason})' if answer.reason else '')
        try:
            _apply_answer(fetch_id, answer, settings, now)
        except Exception as exc:  # the pace below must survive whatever an answer does to the write
            # Rolling back also takes the stored pace with it, so an answer that cannot be stored
            # would be asked for again on every tick, forever, with nothing said.
            db.session.rollback()
            log.exception('TheSportsDB %s: the answer could not be stored', target)
            _record_unstorable(fetch_id, answer, type(exc).__name__, settings, now)
            return SPORTS_FETCH_REFUSED
        if kind in (SPORTS_KIND_ROUND, SPORTS_KIND_NEXT, *ONE_GAME_KINDS) and answer.rows:
            # New games take their place on a league's numbered feed channels once they are
            # stored (app/sports_feed.py). Its own unit, after the schedule's, so nothing it does
            # can undo the answer just stored.
            from .sports_feed import place_all_guarded
            place_all_guarded(now)
        if kind in (SPORTS_KIND_ROUND, *ONE_GAME_KINDS) and answer.rows:
            # Longer than the call spacing, so the answers that arrive while one run is armed share it.
            # An armed run is never moved later (scheduler.queue_sports_match).
            from .scheduler import queue_sports_match
            queue_sports_match(MATCH_AFTER_REFRESH_SECONDS, f'{target} refreshed')
        return answer.outcome
    finally:
        _client_lock.release()


def next_call_at():
    """The worker's stored earliest next call, naive UTC, or None. The job wakes itself then rather
    than at its next interval tick (app/scheduler.py::_sports_worker_job)."""
    state = db.session.get(SportsState, 1)
    when = state.next_call_at if state is not None else None
    db.session.rollback()
    return when


@retry_on_locked()
def _apply_answer(fetch_id: int, answer: Answer, settings: dict, now: datetime):
    """The whole write for one answer, re-read inside the unit. A row deleted while the call was out
    (a follow removed) still counts toward the pace and stores nothing."""
    state = _state()
    _note_call(state, settings, answer, now)
    row = db.session.get(SportsFetch, fetch_id)
    league = db.session.get(SportsLeague, row.league_id) if row is not None else None
    if row is None or league is None:
        db.session.commit()
        return
    if answer.outcome == SPORTS_FETCH_RATE_LIMITED:
        # Not an empty result: nothing stored moves, and this row goes again once the wait is over.
        _record_outcome(row, answer.outcome, answer.reason, now)
    elif answer.outcome == SPORTS_FETCH_REFUSED:
        _record_outcome(row, answer.outcome, answer.reason, now)
        row.next_due_at = now + timedelta(hours=REFUSED_RETRY_HOURS)
    else:
        offset = state.daily_offset_seconds or 0
        if row.kind == SPORTS_KIND_ROUND:
            _, _, season, number = row.target.split(':')
            _apply_round(row, league, answer, season, int(number), settings, offset, now)
        elif row.kind == SPORTS_KIND_NEXT:
            _apply_next(row, league, answer, settings, offset, now)
        elif row.kind == SPORTS_KIND_LEAGUE:
            _apply_league(row, league, answer, offset, now)
        elif row.kind == SPORTS_KIND_TEAM:
            _apply_team(row, answer, int(row.target.split(':')[1]), now)
        elif row.kind == SPORTS_KIND_EVENT:
            _apply_event(row, league, answer, int(row.target.split(':')[1]), now)
        elif row.kind == SPORTS_KIND_SEARCH:
            _apply_search(row, league, answer, _search_teams(row.target)[1], now)
        elif row.kind == SPORTS_KIND_TEAM_NEXT:
            _apply_team_next(row, league, answer, int(row.target.split(':')[2]), now)
        else:
            raise ValueError(f'unknown sports fetch kind {row.kind!r}')
    db.session.commit()


@retry_on_locked()
def _record_unstorable(fetch_id: int, answer: Answer, what: str, settings: dict, now: datetime):
    _note_call(_state(), settings, answer, now)
    row = db.session.get(SportsFetch, fetch_id)
    if row is not None:
        _record_outcome(row, SPORTS_FETCH_REFUSED, f'The answer could not be stored ({what}).', now)
        row.next_due_at = now + timedelta(hours=REFUSED_RETRY_HOURS)
    db.session.commit()


def _prune_if_due(cfg: dict, settings: dict, now: datetime):
    state = db.session.get(SportsState, 1)
    if state is None or (state.last_pruned_at and now - state.last_pruned_at < timedelta(days=1)):
        return
    _prune(settings, now)
    from .sports_images import prune
    prune(cfg)


@retry_on_locked()
def _prune(settings: dict, now: datetime):
    """Past events after history_keep_days, missing ones after 7 days, and the queue rows of rounds,
    re-reads and lookups nothing is left of. Events of a league nobody follows prune here too, which is what "they stay
    until retention" means (design 3)."""
    state = _state()
    keep = settings['history_keep_days']
    cutoff = now - timedelta(days=keep)
    old = SportsEvent.query.filter(
        or_(SportsEvent.start_at < cutoff,
            and_(SportsEvent.start_at.is_(None), SportsEvent.local_date < cutoff.date()),
            SportsEvent.missing_since < now - timedelta(days=MISSING_KEEP_DAYS)))
    # A match goes with its event, rejected ones included (design 9).
    SportsMatch.query.filter(SportsMatch.event_id.in_(old.with_entities(SportsEvent.id).scalar_subquery())
                             ).delete(synchronize_session=False)
    old.delete(synchronize_session=False)
    SportsFetch.query.filter(SportsFetch.kind.in_((SPORTS_KIND_ROUND, *ONE_GAME_KINDS)),
                             SportsFetch.next_due_at.is_(None),
                             SportsFetch.last_at < cutoff).delete(synchronize_session=False)
    state.last_pruned_at = now
    db.session.commit()


# ---------------------------------------------------------------------------
# Following
# ---------------------------------------------------------------------------

def _seed_terms(raw: dict) -> list:
    """The words in guide text that mean this league (5.1), leaving out strCountry: the Premier
    League's alternates include `England`."""
    country = (_text(raw.get('strCountry')) or '').lower()
    terms = [_text(raw.get('strLeague'))]
    terms += [t.strip() for t in (_text(raw.get('strLeagueAlternate')) or '').split(',')]
    seen, out = set(), []
    for term in terms:
        if term and term.lower() != country and term.lower() not in seen:
            seen.add(term.lower())
            out.append(term)
    return out


def with_known_terms(tsdb_id: int, terms: list) -> list:
    """`terms` plus any known extra name for this league it lacks, compared without case."""
    seen = {t.lower() for t in terms}
    return list(terms) + [t for t in KNOWN_EXTRA_TERMS.get(tsdb_id, ()) if t.lower() not in seen]


def _person_call(endpoint: str, params: dict, list_key: str) -> Answer:
    """A person's own lookup: runs in the request, one call at a time with the worker, and counts
    toward its spacing. It answers "try again" rather than waiting behind the queue (4.1)."""
    if not _client_lock.acquire(blocking=False):
        raise TryAgain(MIN_WAIT_SECONDS)
    try:
        now = datetime.utcnow()
        state = db.session.get(SportsState, 1)
        last = state.last_call_at if state is not None else None
        # While backed off, a person waits like the worker: a call inside a 429's pause is refused too.
        held_until = state.next_call_at if state is not None and state.backoff_seconds else None
        db.session.rollback()
        left = MIN_WAIT_SECONDS - (now - last).total_seconds() if last is not None else 0
        if held_until is not None:
            left = max(left, (held_until - now).total_seconds())
        if left > 0:
            raise TryAgain(int(left) + 1)
        answer = _call(endpoint, params, list_key)
        _note_person_call(answer, now)
        return answer
    finally:
        _client_lock.release()


@retry_on_locked()
def _note_person_call(answer: Answer, now: datetime):
    _note_call(_state(), _settings(), answer, now)
    db.session.commit()


def _failed_lookup(answer: Answer) -> LookupFailed:
    if answer.outcome == SPORTS_FETCH_RATE_LIMITED:
        return LookupFailed('TheSportsDB asked ChannelBin to slow down. Nothing was saved. Try again in a minute.')
    return LookupFailed(f'TheSportsDB could not answer that: {answer.reason} Nothing was saved. Try again in a minute.')


def _league_payload(raw: dict) -> dict:
    tsdb_id = _int(raw.get('idLeague'))
    return {'tsdb_id': tsdb_id, 'name': _text(raw.get('strLeague')) or str(tsdb_id),
            'sport': _text(raw.get('strSport')), 'season': _text(raw.get('strCurrentSeason')),
            'terms': _seed_terms(raw)}


def lookup_league(tsdb_id: int) -> dict:
    """Check a league with one call. Every way of adding one comes through here (design 5)."""
    answer = _person_call('lookupleague', {'id': tsdb_id}, 'leagues')
    if answer.outcome != SPORTS_FETCH_OK:
        raise _failed_lookup(answer)
    if not answer.rows or not _int(answer.rows[0].get('idLeague')):
        raise LeagueNotFound(f'TheSportsDB has no league with the number {tsdb_id}.')
    league = _league_payload(answer.rows[0])
    if league['name'].startswith('_'):
        raise LeagueNotFound(f'League {tsdb_id} is {league["name"]}, where TheSportsDB files teams that no longer '
                             'play, so there are no games to follow.')
    return league


def _team_payload(raw: dict):
    """A team and its league from one `searchteams` or `lookupteam` row, or None without both ids.
    The row is itself the check of that league, since the one-call pace would refuse a second call
    straight after it. The worker's first league call fills in the season and the alternate names."""
    team_id, league_id = _int(raw.get('idTeam')), _int(raw.get('idLeague'))
    if not (team_id and league_id):
        return None
    return {'team_id': team_id, 'team': _text(raw.get('strTeam')) or str(team_id),
            'league': {'tsdb_id': league_id, 'name': _text(raw.get('strLeague')) or str(league_id),
                       'sport': _text(raw.get('strSport')), 'season': None,
                       'terms': [t for t in [_text(raw.get('strLeague'))] if t]}}


def is_defunct(team: dict) -> bool:
    """TheSportsDB files a team that no longer plays under a holding league whose name starts with
    an underscore, such as `_Defunct American Football Teams` (4809) or `_Defunct Ice Hockey Teams`
    (4810). No live league seen on the free key starts with one (dev/changelog/1497).
    `lookup_league()` refuses such a league by number for the same reason."""
    return team['league']['name'].startswith('_')


def search_teams(text: str) -> tuple:
    """Find a team and its league with one call: the way in for most team sports (design 5).
    Returns (teams, defunct). A defunct team is never offered, and is handed back so the answer can
    say it was found and why it was left out. The free key gives one row, its best match for the
    name, never a list (measured 2026-11-10)."""
    answer = _person_call('searchteams', {'t': text}, 'teams')
    if answer.outcome != SPORTS_FETCH_OK:
        raise _failed_lookup(answer)
    teams, defunct = [], []
    for raw in answer.rows:
        team = _team_payload(raw)
        if team is not None:
            (defunct if is_defunct(team) else teams).append(team)
    return teams, defunct


def lookup_team(tsdb_id: int) -> dict:
    """Check a team link with one `lookupteam` call. The answer has the team's league, so following
    it needs no `lookupleague` after it. A row for some other team is not this one."""
    answer = _person_call('lookupteam', {'id': tsdb_id}, 'teams')
    if answer.outcome != SPORTS_FETCH_OK:
        raise _failed_lookup(answer)
    team = _team_payload(answer.rows[0]) if answer.rows else None
    if team is None or team['team_id'] != tsdb_id:
        raise LeagueNotFound(f'TheSportsDB has no team with the number {tsdb_id}.')
    if is_defunct(team):
        raise LeagueNotFound(f'That link is to {team["team"]}, a defunct team, so there are no games to follow.')
    return team


_LEAGUE_LINK_RE = re.compile(r'thesportsdb\.com/league/(\d+)', re.I)
_TEAM_LINK_RE = re.compile(r'thesportsdb\.com/team/(\d+)', re.I)


def parse_entry(text: str) -> tuple:
    """What was typed into Add league or team: ('league', id), ('teamlink', id), ('team', text), or
    ('badlink', text). The number is read from a link, and the page itself is never fetched, since
    the terms bar scraping."""
    text = (text or '').strip()
    m = _LEAGUE_LINK_RE.search(text)
    if m:
        return 'league', int(m.group(1))
    m = _TEAM_LINK_RE.search(text)
    if m:
        return 'teamlink', int(m.group(1))
    if re.match(r'https?://', text, re.I) or 'thesportsdb.com' in text.lower():
        return 'badlink', text
    if text.isdecimal():
        return 'league', int(text)
    return 'team', text


def ensure_worker():
    """Keep the worker's job in step with whether anything is queued. Outside any commit unit."""
    from .scheduler import schedule_sports_worker, scheduler_app
    app = scheduler_app()
    if app is not None:
        schedule_sports_worker(app)


def _add_team_rows(teams):
    """A `SportsTeam` row for each checked team ChannelBin has never stored, so a team followed before
    its league's schedule arrives has its name. Inside the caller's commit unit."""
    ids = [t['team_id'] for t in teams]
    have = {i for (i,) in db.session.query(SportsTeam.tsdb_id).filter(SportsTeam.tsdb_id.in_(ids))} if ids else set()
    for team in teams:
        if team['team_id'] not in have:
            db.session.add(SportsTeam(tsdb_id=team['team_id'], full_name=team['team'], names=json.dumps([team['team']])))
            have.add(team['team_id'])


def _replace_follow(league: SportsLeague, team_ids: list, now: datetime):
    """The person's follow of `league` becomes exactly these teams, or the whole league with none. A
    rule's follow is the rule's, and stays. Inside the caller's commit unit."""
    SportsFollow.query.filter_by(league_id=league.id, rule_id=0).delete(synchronize_session=False)
    for team_id in (team_ids or [0]):
        db.session.add(SportsFollow(league_id=league.id, team_tsdb_id=int(team_id), rule_id=0, created_at=now))
    _enqueue_league(league, now)


@retry_on_locked()
def follow_league(payload: dict, team_ids: list = None, checked: list = None) -> int:
    """Add a checked league, or follow one already here: just `team_ids`, or the whole league with
    none. What a person follows in the league is replaced, so the dialog's ticked list is the follow
    (dev/changelog/1641). `checked` are teams a lookup just found ({'team_id', 'team'}), stored so a
    team with no game here yet has its name. Returns the league's id."""
    now = datetime.utcnow()
    league = SportsLeague.query.filter_by(tsdb_id=payload['tsdb_id']).first()
    if league is None:
        league = SportsLeague(tsdb_id=payload['tsdb_id'], name=payload['name'], sport=payload.get('sport'),
                              current_season=payload.get('season'),
                              match_terms=json.dumps(with_known_terms(payload['tsdb_id'], payload.get('terms') or [])),
                              created_at=now)
        db.session.add(league)
        db.session.flush()
    _add_team_rows(checked or [])
    _replace_follow(league, team_ids, now)
    db.session.commit()
    return league.id


@retry_on_locked()
def set_following(league_id: int, team_ids: list = None, checked: list = None) -> bool:
    """A person's follow of this league, in one commit: the whole league with no `team_ids`, or just
    those teams. It costs no calls beyond the league's own, since a team is listed by its league's
    round. `checked` are teams a lookup just found ({'team_id', 'team'}), stored so a team with no
    game here yet has its name. Returns False when the league is gone."""
    league = db.session.get(SportsLeague, league_id)
    if league is None:
        return False
    _add_team_rows(checked or [])
    _replace_follow(league, team_ids, datetime.utcnow())
    db.session.commit()
    return True


@retry_on_locked()
def stop_following(league_id: int) -> bool:
    """Remove a person's follow. If nothing else follows the league its queue rows go, which stops
    its calls; its events stay until retention prunes them and the league stays (design 3)."""
    league = db.session.get(SportsLeague, league_id)
    if league is None:
        return False
    SportsFollow.query.filter_by(league_id=league_id, rule_id=0).delete(synchronize_session=False)
    db.session.flush()
    if SportsFollow.query.filter_by(league_id=league_id).first() is None:
        SportsFetch.query.filter_by(league_id=league_id).delete(synchronize_session=False)
    db.session.commit()
    return True


def _drop_queue_if_unfollowed(league_ids):
    """Delete the queue rows of each league nothing follows any more, which stops its calls (design 3).
    Inside the caller's commit unit, after its follow rows were changed and flushed."""
    for league_id in league_ids:
        if SportsFollow.query.filter_by(league_id=league_id).first() is None:
            SportsFetch.query.filter_by(league_id=league_id).delete(synchronize_session=False)


def set_rule_follow(rule_id: int, league_id, team_tsdb_id, now: datetime) -> bool:
    """The follow an auto-record rule owns: the league it names, or one team in it, and nothing when
    it names none (design 3, 10, dev/changelog/1490). Mutates without committing: the rule's own save
    or delete is the commit unit, so the rule and its follow can never disagree. A league newly
    followed has its queue rows registered in the same commit. Returns True when that happened, so the
    caller starts the worker once the commit is done."""
    before = {f.league_id for f in SportsFollow.query.filter_by(rule_id=rule_id).all()}
    SportsFollow.query.filter_by(rule_id=rule_id).delete(synchronize_session=False)
    added = False
    if league_id:
        league = db.session.get(SportsLeague, league_id)
        if league is not None:
            db.session.add(SportsFollow(league_id=league_id, team_tsdb_id=int(team_tsdb_id or 0),
                                        rule_id=rule_id, created_at=now))
            if SportsFetch.query.filter_by(league_id=league_id).first() is None:
                _enqueue_league(league, now)
                added = True
    db.session.flush()
    _drop_queue_if_unfollowed(before - {league_id})
    return added


class LeagueInUse(Exception):
    """A league an auto-record rule names cannot be removed: the rule would silently stop being about
    anything. The message names the rules."""


def rules_naming(league_id: int) -> list:
    """[(rule id, rule name)] of the auto-record rules that name this league, by name."""
    from .database import AutoRule
    return [(i, n) for i, n in db.session.query(AutoRule.id, AutoRule.name).filter(
        AutoRule.sports_league_id == league_id).order_by(AutoRule.name, AutoRule.id).all()]


@retry_on_locked()
def delete_league(league_id: int) -> bool:
    """Remove the league and everything only it holds: follows, queue rows, events, and any team no
    other league has events for. A recording's own snapshot is never touched (design 3). Refused with
    LeagueInUse while an auto-record rule names the league. The write lock is taken first, by a no-op
    UPDATE of the league's row, so a rule saved while this decides is seen: a plain read runs outside
    the write's transaction under pysqlite and would miss it."""
    if not db.session.execute(update(SportsLeague).where(SportsLeague.id == league_id)
                              .values(id=SportsLeague.id)
                              .execution_options(synchronize_session=False)).rowcount:
        db.session.rollback()
        return False
    league = db.session.get(SportsLeague, league_id)
    naming = rules_naming(league_id)
    if naming:
        db.session.rollback()
        names = ', '.join(f'"{n}"' for _i, n in naming)
        raise LeagueInUse(f'{"An auto-record rule names" if len(naming) == 1 else "Auto-record rules name"} '
                          f'this league: {names}. Change {"it" if len(naming) == 1 else "them"} to name '
                          'something else first.')
    SportsFollow.query.filter_by(league_id=league_id).delete(synchronize_session=False)
    SportsFetch.query.filter_by(league_id=league_id).delete(synchronize_session=False)
    SportsLeagueCategory.query.filter_by(league_id=league_id).delete(synchronize_session=False)
    from .sports_feed import delete_for_league
    delete_for_league(league_id)
    SportsMatch.query.filter(SportsMatch.event_id.in_(
        db.session.query(SportsEvent.id).filter(SportsEvent.league_id == league_id).scalar_subquery())
    ).delete(synchronize_session=False)
    SportsEvent.query.filter_by(league_id=league_id).delete(synchronize_session=False)
    # Whatever no remaining league has events or a follow for, which also reaches a team added from
    # a search before any of its games arrived, or whose games have since been pruned.
    still = db.session.query(SportsEvent.home_team_id, SportsEvent.away_team_id).distinct().all()
    kept = {i for pair in still for i in pair if i}
    kept |= {t for (t,) in db.session.query(SportsFollow.team_tsdb_id).distinct().all() if t}
    SportsTeam.query.filter(SportsTeam.tsdb_id.notin_(kept)).delete(synchronize_session=False)
    db.session.delete(league)
    db.session.commit()
    return True


def worker_needed() -> bool:
    """Something is queued, or stored events still have retention to run: pruning is the worker's,
    and "they stay until retention prunes them" (design 3) has to stay true after the last follow. An
    unfinished recording's artwork still to download is work too (app/sports_images.py)."""
    from .sports_images import needed
    return (db.session.query(SportsFetch.id).first() is not None
            or db.session.query(SportsEvent.id).first() is not None
            or needed())


# ---------------------------------------------------------------------------
# A recording's event (design 9, `recording_sports`)
# ---------------------------------------------------------------------------

#: What link_recording() did. Every value is named where it is read.
LINK_LINKED = 'linked'          # a new link, or one that moved to another event
LINK_REFRESHED = 'refreshed'    # the same event, its snapshot brought up to date
LINK_REMOVED = 'removed'        # a person removed it, so it is left alone
LINK_AMBIGUOUS = 'ambiguous'    # the listing matched more than one event equally well
LINK_NONE = 'none'              # no listing, or no match for it
LINK_DROPPED = 'dropped'        # its listing was marked a wrong match on the league page
LINK_FAILED = 'failed'          # something went wrong, and it was logged

_STRENGTH_RANK = {SPORTS_MATCH_STRONG: 0, SPORTS_MATCH_WEAK: 1}

#: How far back the unfinished-recordings pass looks: a recording still capturing or joining
#: whose program started this long ago is still worth bringing up to date.
_UNFINISHED_LOOKBACK = timedelta(hours=24)


def recording_channel_ids(rec) -> list:
    """The channels a recording's listing can be on: its own, and every member of its group, since
    a group's guide row comes from one member and the recording may be on another."""
    ids = {rec.channel_id} if rec.channel_id else set()
    if rec.group_id:
        ids |= {i for (i,) in db.session.query(ChannelGroupMember.channel_id).filter(
            ChannelGroupMember.group_id == rec.group_id)}
    return sorted(ids)


def recording_matches(rec, rejected: bool = False) -> list:
    """The guide matches for the listing a recording was made from: any of its channels, at its
    program's start. The anchor is the start and the channel, not the title, because a provider can
    reword a title after the recording was set up; a channel lists one program at a time."""
    if rec.program_start_time is None:
        return []
    ids = recording_channel_ids(rec)
    if not ids:
        return []
    q = SportsMatch.query.filter(SportsMatch.kind == SPORTS_MATCH_KIND_GUIDE, SportsMatch.channel_id.in_(ids),
                                 SportsMatch.program_start_time == rec.program_start_time)
    if not rejected:
        q = q.filter(SportsMatch.rejected_at.is_(None))
    return q.all()


#: How near a channel-name match's game must start to a recording's program start to be the game
#: that recording is: an event slot is renamed for the next game days later (design 6.8).
NAME_LINK_WINDOW = timedelta(hours=3)


def recording_name_matches(rec) -> list:
    """For a recording with no guide listing - one an auto-record rule made from an event channel
    (dev/changelog/1491), or one a person made on it - the strong channel-name matches on its
    channels whose game starts near its program start. Not rejected. A duplicate copy of one of its
    channels counts as that channel: a game can be named only by a copy's name and recorded from the
    kept copy of the stream (dev/changelog/1579)."""
    if rec.program_start_time is None:
        return []
    ids = recording_channel_ids(rec)
    if not ids:
        return []
    from .database import Channel
    clusters = [c for (c,) in db.session.query(Channel.duplicate_cluster_id).filter(
        Channel.id.in_(ids), Channel.duplicate_cluster_id.isnot(None)).distinct()]
    if clusters:
        ids = sorted(set(ids) | {i for (i,) in db.session.query(Channel.id).filter(
            Channel.duplicate_cluster_id.in_(clusters))})
    rows = db.session.query(SportsMatch, SportsEvent.start_at).join(
        SportsEvent, SportsEvent.id == SportsMatch.event_id).filter(
        SportsMatch.kind == SPORTS_MATCH_KIND_CHANNEL_NAME, SportsMatch.channel_id.in_(ids),
        SportsMatch.strength == SPORTS_MATCH_STRONG, SportsMatch.rejected_at.is_(None)).all()
    out = []
    for m, event_start in rows:
        start = m.program_start_time or event_start
        if start is not None and abs(start - rec.program_start_time) <= NAME_LINK_WINDOW:
            out.append(m)
    return out


def pick_match(matches: list, wanted_title: str) -> tuple:
    """(match, ambiguous). Still listed beats gone, strong beats weak, and a match on the
    recording's own title wins a tie. Two events tied at the best still-listed strength is not a
    choice to make for a person: neither is linked."""
    if not matches:
        return None, False

    def tier(m):
        return (m.gone_since is not None, _STRENGTH_RANK.get(m.strength, 2))
    best = min(tier(m) for m in matches)
    top = [m for m in matches if tier(m) == best]
    if len({m.event_id for m in top}) > 1:
        return None, True
    return min(top, key=lambda m: (m.title_key != wanted_title, m.id)), False


def event_title(link) -> str:
    """How a linked event reads: `Away at Home` for a matchup, the way guides list it and the
    league page shows it, and the event's own name for a race."""
    if link.home_name and link.away_name:
        return f'{link.away_name} at {link.home_name}'
    return link.event_name


def link_recording(recording_id: int) -> str:
    """Link a recording to the event its listing was matched to, or bring its snapshot up to date.
    Runs when a recording is created, after each matcher run while it is unfinished, and when it
    completes. Returns one of the LINK_* outcomes.

    Nothing here may fail or delay a recording: its callers are the create path and the post-process
    chain one step from COMPLETED. So no error leaves here, and the recording is never written to. A
    removed link is never touched, and no match leaves an existing link alone, since the listing aging
    out of the guide is no reason to forget what it was. A listing marked a wrong match on the league
    page is a person's answer, so it drops the link."""
    try:
        return _link_recording(recording_id)
    except SQLAlchemyError:
        # The database stayed locked past every retry. The next matcher run or the completion tries again.
        db.session.rollback()
        log.warning('Recording %d: could not link it to a TheSportsDB event', recording_id, exc_info=True)
        return LINK_FAILED
    except Exception:
        # Anything else is a defect here. Logged at ERROR, which raises an alert naming the recording,
        # and still kept from the post-process chain, which would otherwise never reach COMPLETED.
        db.session.rollback()
        log.error('Recording %d: linking it to a TheSportsDB event failed', recording_id, exc_info=True,
                  extra={'recording_id': recording_id})
        return LINK_FAILED


def _link_recording(recording_id: int) -> str:
    rec = db.session.get(Recording, recording_id)
    if rec is None or rec.program_start_time is None:
        return LINK_NONE
    existing = db.session.get(RecordingSports, recording_id)
    if existing is not None and existing.removed_at is not None:
        return LINK_REMOVED
    matches = recording_matches(rec, rejected=True)
    if not matches:
        matches = recording_name_matches(rec)
    live = [m for m in matches if m.rejected_at is None]
    match, ambiguous = pick_match(live, title_key(rec.program_title))
    if match is None and existing is not None:
        linked = {i for (i,) in db.session.query(SportsEvent.id).filter(SportsEvent.tsdb_id == existing.tsdb_event_id)}
        if any(m.event_id in linked for m in matches) and not any(m.event_id in linked for m in live):
            tsdb_event_id = existing.tsdb_event_id
            db.session.rollback()
            return _drop_link(recording_id, tsdb_event_id)
    if match is None:
        if ambiguous:
            log.info('Recording %d: its listing matched more than one TheSportsDB event, so it was not linked',
                     recording_id)
        db.session.rollback()
        return LINK_AMBIGUOUS if ambiguous else LINK_NONE
    event = db.session.get(SportsEvent, match.event_id)
    league = db.session.get(SportsLeague, event.league_id) if event is not None else None
    if league is None:
        db.session.rollback()
        return LINK_NONE
    snap = {'tsdb_event_id': event.tsdb_id, 'league_name': league.name, 'event_name': event.name,
            'home_name': event.home_name if event_shape_is_matchup(event) else None,
            'away_name': event.away_name if event_shape_is_matchup(event) else None,
            'season': event.season, 'round': event.round, 'venue': event.venue, 'city': event.city,
            'event_start': event.start_at, 'local_date': event.local_date,
            'strength': match.strength, 'airing': match.airing, 'reason': match.reason,
            # Its artwork, downloaded for it by app/sports_images.py (dev/changelog/1465).
            'poster_url': event.poster_url, 'thumb_url': event.thumb_url}
    db.session.rollback()
    return _write_link(recording_id, snap)


@retry_on_locked()
def _write_link(recording_id: int, snap: dict) -> str:
    if db.session.get(Recording, recording_id) is None:
        db.session.rollback()
        return LINK_NONE
    row = db.session.get(RecordingSports, recording_id)
    if row is not None and row.removed_at is not None:
        db.session.rollback()
        return LINK_REMOVED
    now = datetime.utcnow()
    was = event_title(row) if row is not None and row.tsdb_event_id != snap['tsdb_event_id'] else None
    moved = row is None or was is not None
    if row is None:
        row = RecordingSports(recording_id=recording_id, linked_at=now)
        db.session.add(row)
    for key, value in snap.items():
        setattr(row, key, value)
    row.updated_at = now
    if moved:
        detail = (f'Matched to TheSportsDB\'s {row.league_name} event "{event_title(row)}", a {row.strength} '
                  f'match: {row.reason}')
        if was is not None:
            detail += f' It was matched to "{was}" before.'
        add_recording_event(recording_id, RECORDING_SPORTS_LINKED, detail=detail)
    db.session.commit()
    return LINK_LINKED if moved else LINK_REFRESHED


@retry_on_locked()
def _drop_link(recording_id: int, tsdb_event_id: int) -> str:
    """Deleted rather than marked removed, so a listing later matched to the right event links again."""
    row = db.session.get(RecordingSports, recording_id)
    if row is None or row.removed_at is not None or row.tsdb_event_id != tsdb_event_id:
        db.session.rollback()
        return LINK_NONE
    add_recording_event(recording_id, RECORDING_SPORTS_REMOVED,
                        detail=f'Its listing was marked a wrong match on the {row.league_name} page, so it is no '
                               f'longer matched to TheSportsDB\'s event "{event_title(row)}".')
    db.session.delete(row)
    db.session.commit()
    return LINK_DROPPED


def link_unfinished_recordings(now: datetime = None) -> int:
    """After a matcher run: link or refresh every recording that has not finished, so one set up
    before its game was matched says what it is before it airs rather than only when it completes.
    Returns how many were newly linked."""
    now = now or datetime.utcnow()
    ids = [i for (i,) in db.session.query(Recording.id).filter(
        Recording.status.notin_(REC_ENDED_STATUSES), Recording.program_start_time.isnot(None),
        Recording.program_start_time >= now - _UNFINISHED_LOOKBACK).all()]
    db.session.rollback()
    return sum(link_recording(i) == LINK_LINKED for i in ids)


def set_link_removed(recording_id: int, removed: bool):
    """A person says this recording is not its event, or takes that back. The listing matches it
    marked wrong are marked too, so the league page agrees and the matcher works the event out again,
    and an undo puts back only those: a listing someone marked wrong on the league page stays wrong.
    Returns the link, or None when the recording has none."""
    from .sports_match import mark_wrong
    rec = db.session.get(Recording, recording_id)
    link = db.session.get(RecordingSports, recording_id)
    if rec is None or link is None:
        return None
    event = SportsEvent.query.filter_by(tsdb_id=link.tsdb_event_id).first()
    event_id = event.id if event is not None else None
    if removed:
        ids = [m.id for m in recording_matches(rec) if m.event_id == event_id]
    else:
        ids = _ids_marked_by_removal(recording_id)
    db.session.rollback()
    changed = _write_removed(recording_id, removed, ids)
    if changed is None:
        return None
    if changed and event_id is not None and ids:
        mark_wrong(event_id, ids, removed)
    return db.session.get(RecordingSports, recording_id)


def _ids_marked_by_removal(recording_id: int) -> list:
    """The listing matches the recording's last Wrong match marked, from its event."""
    ev = (RecordingEvent.query.filter_by(recording_id=recording_id, event_type=RECORDING_SPORTS_REMOVED)
          .order_by(RecordingEvent.id.desc()).first())
    try:
        return list((json.loads(ev.extra_data) if ev is not None and ev.extra_data else {}).get('match_ids') or [])
    except (ValueError, TypeError) as exc:
        log.warning('Recording %d: its Wrong match event carries unreadable data: %s', recording_id, exc)
        return []


@retry_on_locked()
def _write_removed(recording_id: int, removed: bool, match_ids: list):
    """True when it moved, False when it was already so, None when there is no link. `match_ids` are
    the listing matches the removal marks wrong, kept on its event so an undo reverses only those."""
    row = db.session.get(RecordingSports, recording_id)
    if row is None:
        db.session.rollback()
        return None
    if (row.removed_at is not None) == removed:
        db.session.rollback()
        return False
    row.removed_at = datetime.utcnow() if removed else None
    if removed:
        add_recording_event(recording_id, RECORDING_SPORTS_REMOVED,
                            detail=f'You said this is not TheSportsDB\'s {row.league_name} event "{event_title(row)}", '
                                   f'so the recording no longer carries its details and its listing was marked a '
                                   f'wrong match.', extra={'match_ids': match_ids})
    else:
        add_recording_event(recording_id, RECORDING_SPORTS_LINKED,
                            detail=f'You put back the match to TheSportsDB\'s {row.league_name} event '
                                   f'"{event_title(row)}".')
    db.session.commit()
    return True


def link_facts(link) -> str:
    """One sentence of what TheSportsDB says about a linked event, with its credit: what a media
    server shows under the synopsis. A round outside 1 to 99 is one of TheSportsDB's codes for a
    preseason or playoff round (0, 125, 160, 500), which means nothing to a viewer, so it is left out."""
    parts = [link.league_name]
    if link.season:
        parts.append(f'{link.season} season')
    if link.round and 0 < link.round < 100:
        parts.append(f'round {link.round}')
    where = ', '.join(x for x in (link.venue, link.city) if x)
    text = ', '.join(parts) + (f', at {where}' if where else '')
    return f'{text}. Event details from TheSportsDB.'


# ---------------------------------------------------------------------------
# TheSportsDB's fields in a recording's name (design 11, dev/changelog/1498)
# ---------------------------------------------------------------------------

def link_has_time(link) -> bool:
    """Whether a link's start is a real time. A field event at exactly 00:00 has none: that is
    TheSportsDB's placeholder, and moved into a time zone it lands on the wrong day (design 7)."""
    start = link.event_start
    return start is not None and (bool(link.home_name and link.away_name) or (start.hour, start.minute) != (0, 0))


def event_day(link, tz):
    """The event's day: in the display timezone when TheSportsDB gives a real time, else its own
    date. Never from strTimeLocal or strFilename, whose date was wrong on 4 of 6 Truck rows. None
    when the link has no date at all."""
    if link_has_time(link):
        return to_local(link.event_start, tz).date()
    return link.local_date


def template_values(link, tz) -> dict:
    """{field: text} for the filename template's {sports:field} placeholders
    (accounts.SPORTS_TEMPLATE_FIELDS). The league is TheSportsDB's name for it, since that is what
    a media server's sports agent looks it up by, even where a guide uses another. A round outside
    1 to 99 is one of TheSportsDB's codes for a preseason or playoff round, as in link_facts()."""
    day = event_day(link, tz)
    return {
        'league': link.league_name or '',
        'event': link.event_name or '',
        'home': link.home_name or '',
        'away': link.away_name or '',
        'season': link.season or '',
        'round': str(link.round) if link.round and 0 < link.round < 100 else '',
        'venue': link.venue or '',
        'city': link.city or '',
        'date': day.isoformat() if day else '',
        'start_time': to_local(link.event_start, tz).strftime('%H%M') if link_has_time(link) else '',
    }


def recording_file_name(rec, tz) -> tuple:
    """(name, note): what the join names a recording, and the sentence its event carries about
    that, or None to say nothing.

    Only a recording whose name came from a filename template with {sports:...} fields carries
    Recording.sports_name_template; any other keeps its own name and nothing is said. One that
    does gets those fields filled from its TheSportsDB event, or left empty when it has none or a
    person removed it, and the note says which. Its name is checked against the template's
    empty rendering first, so a name changed since it was scheduled is never rewritten, whichever
    path changed it.

    Read-only, and it never raises: the join is one step from a recording's only file, and a
    name is never worth losing that."""
    from .accounts import fill_sports_fields, sports_name_matches
    own = rec.name
    pending = rec.sports_name_template
    if not pending:
        return own, None
    if not sports_name_matches(own, pending, rec.program_title or ''):
        return own, ('its name was changed after it was scheduled, so its TheSportsDB fields were '
                     'not filled in')
    try:
        link = db.session.get(RecordingSports, rec.id)
    except SQLAlchemyError:
        db.session.rollback()
        log.warning('Recording %d: could not read its TheSportsDB event, so its TheSportsDB fields '
                    'stay empty', rec.id, exc_info=True)
        return own, 'its TheSportsDB event could not be read, so its TheSportsDB fields are empty'
    if link is None:
        return own, 'no TheSportsDB event was matched, so its TheSportsDB fields are empty'
    if link.removed_at is not None:
        return own, (f'its match to TheSportsDB\'s {link.league_name} event was removed, so its '
                     f'TheSportsDB fields are empty')
    name = fill_sports_fields(pending, template_values(link, tz)) or own
    weak = '' if link.strength == SPORTS_MATCH_STRONG else ', a weak match'
    return name, (f'TheSportsDB fields filled from its {link.league_name} event '
                  f'"{event_title(link)}"{weak}')
