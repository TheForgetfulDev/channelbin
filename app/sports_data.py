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
from sqlalchemy import and_, or_
from sqlalchemy.exc import SQLAlchemyError

from . import db
from .config import load_config
from .database import (
    REC_ENDED_STATUSES, RECORDING_SPORTS_LINKED, RECORDING_SPORTS_REMOVED, SPORTS_MATCH_KIND_GUIDE,
    SPORTS_MATCH_STRONG, SPORTS_MATCH_WEAK, ChannelGroupMember, Recording, RecordingEvent, RecordingSports,
    add_recording_event,
    SPORTS_FETCH_EMPTY, SPORTS_FETCH_OK, SPORTS_FETCH_RATE_LIMITED, SPORTS_FETCH_REFUSED,
    SPORTS_FETCH_SHRUNK, SPORTS_KIND_LEAGUE, SPORTS_KIND_NEXT, SPORTS_KIND_ROUND, SPORTS_KIND_TEAM,
    SportsEvent, SportsFetch, SportsFollow, SportsLeague, SportsLeagueCategory, SportsMatch, SportsState,
    SportsTeam,
)
from .db_utils import retry_on_locked
from .program_titles import title_key
from .tz_utils import to_local

log = logging.getLogger(__name__)

BASE_URL = 'https://www.thesportsdb.com/api/v1/json'
#: The free key. A user's own key is deliberately not supported, because that path could never be
#: tested here (design 8). It is not a secret, so nothing masks it.
FREE_KEY = '123'
TIMEOUT_SECONDS = 20
#: The fastest pace measured clean: 429s came at one call every 4 seconds, and about 85 calls at one
#: every 12 seconds were clean. The floor under every call, whatever the config says.
MIN_WAIT_SECONDS = 12
MAX_BACKOFF_SECONDS = 3600
#: Clean calls in a row that halve a backoff.
CLEAN_CALLS_TO_EASE = 5
MISSING_KEEP_DAYS = 7
EMPTY_RECHECK_DAYS = 3
REFUSED_RETRY_HOURS = 6
MATCH_AFTER_REFRESH_SECONDS = 180

#: The Add a league picks, in the order the dialog shows them: alphabetical. Names and numbers only,
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
#: (design 5.1). Both were seen in prod's guide: TheSportsDB calls 5093 `NASCAR Truck Series`, and
#: still calls 4573 `NASCAR Xfinity Series` after its 2025 rename. Seeded when a league is added,
#: and offered on its page while missing.
KNOWN_EXTRA_TERMS = {
    5093: ('NASCAR Craftsman Truck Series',),
    4573: ("NASCAR O'Reilly Auto Parts Series",),
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
        return Answer(SPORTS_FETCH_RATE_LIMITED, reason='HTTP 429, too many requests.')
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
                f'faster than that has been measured drawing "too many requests" from TheSportsDB. '
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
    """What any call does to the pace, the worker's or a person's. A 429 doubles the spacing up to
    an hour; clean calls bring it back down."""
    state.last_call_at = now
    if answer.outcome == SPORTS_FETCH_RATE_LIMITED:
        state.backoff_seconds = min(MAX_BACKOFF_SECONDS,
                                    max(settings['call_spacing_seconds'], state.backoff_seconds) * 2)
        state.clean_calls = 0
    elif state.backoff_seconds:
        state.clean_calls = (state.clean_calls or 0) + 1
        if state.clean_calls >= CLEAN_CALLS_TO_EASE:
            halved = state.backoff_seconds // 2
            state.backoff_seconds = 0 if halved <= settings['call_spacing_seconds'] else halved
            state.clean_calls = 0
    earliest = now + timedelta(seconds=_next_wait(settings, state.backoff_seconds))
    state.next_call_at = max(state.next_call_at, earliest) if state.next_call_at else earliest


# ---------------------------------------------------------------------------
# Reading an answer
# ---------------------------------------------------------------------------

def _int(value) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


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
        return None, 'wrong league'
    start = _parse_when(row.get('strTimestamp'))
    local_date = _parse_date(row.get('dateEventLocal')) or _parse_date(row.get('dateEvent'))
    if start is None and local_date is None:
        return None, 'no date'
    name = _text(row.get('strEvent'))
    if name is None:
        return None, 'no name'
    return {
        'tsdb_id': tsdb_id, 'season': _text(row.get('strSeason')), 'round': _int(row.get('intRound')) or None,
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


def event_shape_is_matchup(event) -> bool:
    """An event with team ids is a matchup; one with none is a field event, such as a race. The shape
    comes from the event, never a list of sports (6.1)."""
    return bool(event.home_team_id or event.away_team_id)


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


def _apply_round(row: SportsFetch, league: SportsLeague, answer: Answer, season: str, number: int,
                 settings: dict, offset: int, now: datetime):
    parsed, dropped, seen_ids = [], {}, set()
    for raw in answer.rows:
        event, why = parse_event(raw, league.tsdb_id)
        if event is None:
            dropped[why] = dropped.get(why, 0) + 1
        elif event['tsdb_id'] in seen_ids:
            dropped['a repeated id'] = dropped.get('a repeated id', 0) + 1
        else:
            seen_ids.add(event['tsdb_id'])
            parsed.append(event)

    ids = [e['tsdb_id'] for e in parsed]
    existing = {e.tsdb_id: e for e in SportsEvent.query.filter(SportsEvent.tsdb_id.in_(ids)).all()} if ids else {}
    for fields in parsed:
        event = existing.get(fields['tsdb_id'])
        if event is None:
            event = SportsEvent(tsdb_id=fields['tsdb_id'], league_id=league.id, first_seen=now)
            db.session.add(event)
        elif (fields['start_at'] and event.start_at and fields['start_at'] != event.start_at):
            event.previous_start = event.start_at
            event.start_changed_at = now
        for key, value in fields.items():
            setattr(event, key, value)
        event.league_id = league.id
        event.last_seen = now
        event.missing_since = None

    # A smaller answer is named and deletes nothing. A stored event missing from a good answer may
    # have moved rounds, so it is marked rather than dropped, and only when the answer held games.
    if parsed:
        gone = SportsEvent.query.filter(
            SportsEvent.league_id == league.id, SportsEvent.season == season,
            SportsEvent.round == number, SportsEvent.missing_since.is_(None),
            SportsEvent.tsdb_id.notin_(ids))
        for event in gone.all():
            event.missing_since = now

    count = len(parsed)
    previous = row.row_count
    if previous is not None and count < previous:
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

    starts = [e['start_at'] or datetime.combine(e['local_date'], time()) for e in parsed]
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


def _ensure_team_rows(league: SportsLeague, parsed: list, now: datetime):
    """A row per new team's lookup, once a season (6.2). Until it runs only the full name counts."""
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
        if team.last_looked_up is None:
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
    if not season or not number:
        row.dropped_count = 1
        _record_outcome(row, SPORTS_FETCH_OK, 'Its next event has no season or round, so its schedule could not be asked for.', now)
        return
    stored = SportsEvent.query.filter_by(tsdb_id=event['tsdb_id']).first() is not None
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
        # Team names are read once a season, so a new one is what asks again.
        SportsTeam.query.filter(SportsTeam.tsdb_id.in_(_league_team_ids(league.id))).update(
            {SportsTeam.last_looked_up: None}, synchronize_session=False)
    _record_outcome(row, SPORTS_FETCH_OK, '', now)


def _league_team_ids(league_id: int) -> list:
    rows = (db.session.query(SportsEvent.home_team_id, SportsEvent.away_team_id)
            .filter(SportsEvent.league_id == league_id).distinct().all())
    return list({i for pair in rows for i in pair if i})


def _apply_team(row, answer, tsdb_id, now):
    # A looked-up team has nothing further to ask until the season changes.
    row.next_due_at = None
    team = SportsTeam.query.filter_by(tsdb_id=tsdb_id).first()
    if not answer.rows:
        # Asked and answered: its full name from the schedule stands until the season changes.
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
    team.last_looked_up = now
    _record_outcome(row, SPORTS_FETCH_OK, '', now)


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
        due = (db.session.query(SportsFetch.id, SportsFetch.target, SportsFetch.kind, SportsLeague.tsdb_id)
               .join(SportsLeague, SportsLeague.id == SportsFetch.league_id)
               .filter(SportsFetch.next_due_at.isnot(None), SportsFetch.next_due_at <= now)
               .order_by(SportsFetch.next_due_at, SportsFetch.id).first())
        db.session.rollback()  # holds no read transaction across the outbound call
        if due is None:
            # With no schedule call due, the turn goes to an image a recording is waiting on, at the
            # same pace (app/sports_images.py).
            from .sports_images import fetch_next
            return fetch_next(cfg, settings, now) or 'idle'
        fetch_id, target, kind, league_tsdb_id = due
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
        if kind == SPORTS_KIND_ROUND and answer.rows:
            # Longer than the call spacing, so a first fill's rounds become one match after the last.
            from .scheduler import queue_sports_match
            queue_sports_match(MATCH_AFTER_REFRESH_SECONDS, f'{target} refreshed')
        return answer.outcome
    finally:
        _client_lock.release()


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
    """Past events after history_keep_days, missing ones after 7 days, and the queue rows of rounds
    nothing is left of. Events of a league nobody follows prune here too, which is what "they stay
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
    SportsFetch.query.filter(SportsFetch.kind == SPORTS_KIND_ROUND, SportsFetch.next_due_at.is_(None),
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
        db.session.rollback()
        if last is not None:
            left = MIN_WAIT_SECONDS - (now - last).total_seconds()
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
    return _league_payload(answer.rows[0])


def search_teams(text: str) -> list:
    """Find a team and its league with one call: the way in for most team sports (design 5). The
    answer is itself the check of that league, since the one-call pace would refuse a second call
    straight after it. The worker's first league call fills in the season and the alternate names."""
    answer = _person_call('searchteams', {'t': text}, 'teams')
    if answer.outcome != SPORTS_FETCH_OK:
        raise _failed_lookup(answer)
    out = []
    for raw in answer.rows:
        team_id, league_id = _int(raw.get('idTeam')), _int(raw.get('idLeague'))
        if team_id and league_id:
            out.append({'team_id': team_id, 'team': _text(raw.get('strTeam')) or str(team_id),
                        'league': {'tsdb_id': league_id, 'name': _text(raw.get('strLeague')) or str(league_id),
                                   'sport': _text(raw.get('strSport')), 'season': None,
                                   'terms': [t for t in [_text(raw.get('strLeague'))] if t]}})
    return out


_LEAGUE_LINK_RE = re.compile(r'thesportsdb\.com/league/(\d+)', re.I)


def parse_entry(text: str) -> tuple:
    """What was typed into Add a league: ('league', id), ('team', text), or ('badlink', text). The
    number is read from a link, and the page itself is never fetched, since the terms bar scraping."""
    text = (text or '').strip()
    m = _LEAGUE_LINK_RE.search(text)
    if m:
        return 'league', int(m.group(1))
    if re.match(r'https?://', text, re.I) or 'thesportsdb.com' in text.lower():
        return 'badlink', text
    if text.isdigit():
        return 'league', int(text)
    return 'team', text


def ensure_worker():
    """Keep the worker's job in step with whether anything is queued. Outside any commit unit."""
    from .scheduler import schedule_sports_worker, scheduler_app
    app = scheduler_app()
    if app is not None:
        schedule_sports_worker(app)


@retry_on_locked()
def follow_league(payload: dict, team: dict = None) -> int:
    """Add a checked league, or follow one already here. With `team`, follows just that team unless
    the league is already followed whole. Returns the league's id."""
    now = datetime.utcnow()
    league = SportsLeague.query.filter_by(tsdb_id=payload['tsdb_id']).first()
    if league is None:
        league = SportsLeague(tsdb_id=payload['tsdb_id'], name=payload['name'], sport=payload.get('sport'),
                              current_season=payload.get('season'),
                              match_terms=json.dumps(with_known_terms(payload['tsdb_id'], payload.get('terms') or [])),
                              created_at=now)
        db.session.add(league)
        db.session.flush()
    if team:
        if SportsTeam.query.filter_by(tsdb_id=team['team_id']).first() is None:
            db.session.add(SportsTeam(tsdb_id=team['team_id'], full_name=team['team'],
                                      names=json.dumps([team['team']])))
        whole = SportsFollow.query.filter_by(league_id=league.id, team_tsdb_id=0, rule_id=0).first()
        wanted = None if whole else team['team_id']
    else:
        wanted = 0
    if wanted is not None:
        if wanted == 0:
            SportsFollow.query.filter_by(league_id=league.id, rule_id=0).delete(synchronize_session=False)
        if SportsFollow.query.filter_by(league_id=league.id, team_tsdb_id=wanted, rule_id=0).first() is None:
            db.session.add(SportsFollow(league_id=league.id, team_tsdb_id=wanted, rule_id=0, created_at=now))
    _enqueue_league(league, now)
    db.session.commit()
    return league.id


@retry_on_locked()
def set_following(league_id: int, team_ids: list = None) -> bool:
    """A person's follow of this league, in one commit: the whole league with no `team_ids`, or just
    those teams. It costs no calls beyond the league's own, since a team is listed by its league's
    round. Returns False when the league is gone."""
    league = db.session.get(SportsLeague, league_id)
    if league is None:
        return False
    now = datetime.utcnow()
    SportsFollow.query.filter_by(league_id=league_id, rule_id=0).delete(synchronize_session=False)
    for team_id in (team_ids or [0]):
        db.session.add(SportsFollow(league_id=league_id, team_tsdb_id=int(team_id), rule_id=0, created_at=now))
    _enqueue_league(league, now)
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


@retry_on_locked()
def delete_league(league_id: int) -> bool:
    """Remove the league and everything only it holds: follows, queue rows, events, and any team no
    other league has events for. A recording's own snapshot is never touched (design 3)."""
    league = db.session.get(SportsLeague, league_id)
    if league is None:
        return False
    SportsFollow.query.filter_by(league_id=league_id).delete(synchronize_session=False)
    SportsFetch.query.filter_by(league_id=league_id).delete(synchronize_session=False)
    SportsLeagueCategory.query.filter_by(league_id=league_id).delete(synchronize_session=False)
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
# A matched recording's file name (design 11, dev/changelog/1466)
# ---------------------------------------------------------------------------

def file_names_on(cfg: dict) -> bool:
    """sports_data.file_names: name a matched recording's file from TheSportsDB. Off by default."""
    return bool((cfg.get('sports_data') or {}).get('file_names', False))


def link_has_time(link) -> bool:
    """Whether a link's start is a real time. A field event at exactly 00:00 has none: that is
    TheSportsDB's placeholder, and moved into a time zone it lands on the wrong day (design 7)."""
    start = link.event_start
    return start is not None and (bool(link.home_name and link.away_name) or (start.hour, start.minute) != (0, 0))


def file_name(link, tz) -> str | None:
    """`League YYYY-MM-DD Event`, the shape of TheSportsDB's own strFilename and what Plex's sports
    agents read, built from the link's fields. Never strFilename itself, whose date was wrong on 4 of
    6 Truck rows. The league is TheSportsDB's name for it, since that is what an agent looks it up by,
    even where a guide uses another (`NASCAR Xfinity Series`, not `NASCAR O'Reilly Auto Parts
    Series`). The date is the event's day in the display timezone, or TheSportsDB's own date when it
    gives no real time. None when the link has no date at all."""
    if link_has_time(link):
        day = to_local(link.event_start, tz).date()
    else:
        day = link.local_date
    if day is None:
        return None
    return f'{link.league_name} {day.isoformat()} {link.event_name}'


def recording_file_name(rec, cfg: dict, tz) -> tuple:
    """(name, note): what the join names a recording's file, and the sentence its event carries
    about that, or None to say nothing. With sports_data.file_names off, or no link, the recording's
    own name and nothing said. A link a person removed keeps the recording's own name and says so.

    Read-only, and it never raises: the join is one step from a recording's only file, and a
    name is never worth losing that."""
    own = rec.name
    if not file_names_on(cfg):
        return own, None
    try:
        link = db.session.get(RecordingSports, rec.id)
    except SQLAlchemyError:
        db.session.rollback()
        log.warning('Recording %d: could not read its TheSportsDB event, so its file keeps its own name',
                    rec.id, exc_info=True)
        return own, 'its TheSportsDB event could not be read, so the file keeps the recording\'s own name'
    if link is None:
        return own, None
    if link.removed_at is not None:
        return own, (f'not named from TheSportsDB\'s {link.league_name} event, since its match was '
                     f'removed')
    name = file_name(link, tz)
    if not name:
        return own, None
    weak = '' if link.strength == SPORTS_MATCH_STRONG else ', a weak match'
    return name, f'named from TheSportsDB\'s {link.league_name} event "{event_title(link)}"{weak}'
