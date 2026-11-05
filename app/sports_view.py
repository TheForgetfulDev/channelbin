"""What the Sports Schedules pages show (dev/docs/DESIGN-sports-data.md 11).

A fixed number of queries per page, however many leagues and games there are: tz and the clock
format are read once and handed down, and nothing is asked per row. Status comes from what the queue
recorded, never from a count of what is stored. "Round" is TheSportsDB's word for one call's worth of
games and means nothing to a viewer, so it appears only beside the dates in Schedule updates.
"""
from datetime import datetime, timedelta

from sqlalchemy import func, or_

from . import db
from .database import (
    SPORTS_FETCH_EMPTY, SPORTS_FETCH_OK, SPORTS_FETCH_RATE_LIMITED, SPORTS_FETCH_REFUSED,
    SPORTS_FETCH_SHRUNK, SPORTS_KIND_LEAGUE, SPORTS_KIND_NEXT, SPORTS_KIND_ROUND, SPORTS_KIND_TEAM,
    SportsEvent, SportsFetch, SportsFollow, SportsLeague, SportsState, SportsTeam,
)
from .fmt_utils import counted
from .sports_data import SHIPPED_LEAGUES, _settings, event_shape_is_matchup
from .tz_utils import ago, date_label, day_label, format_local, get_display_tz, is_24h, to_local

STATUS_OK = 'ok'
STATUS_FILLING = 'filling'
STATUS_LOOK = 'look'
STATUS_SLOW = 'slow'
STATUS_FAILED = 'failed'
STATUS_OFF = 'off'

#: status: (badge class, label, row edge class, tooltip, how it sorts, worst first)
STATUSES = {
    STATUS_FAILED: ('b-fail', "Couldn't update", 'rs-broken', 0,
                    'The last update failed. What is stored has not changed. Open the league to see why.'),
    STATUS_SLOW: ('b-warn', 'Slowed down', 'rs-cap', 1,
                  'TheSportsDB asked ChannelBin to slow down. Nothing stored has changed.'),
    STATUS_LOOK: ('b-warn', 'Needs a look', 'rs-cap', 2,
                  'The last update came back different from what was stored. Open the league to see what changed.'),
    STATUS_FILLING: ('b-running', 'Getting schedule', 'rs-new', 3,
                     'ChannelBin is still making the first set of calls for this league.'),
    STATUS_OK: ('b-done', 'Up to date', 'rs-on', 4, 'The last update was answered and looked right.'),
    STATUS_OFF: ('b-paused', 'Not followed', 'rs-paused', 5, 'No calls are being made for this league.'),
}

#: outcome: (badge class, label)
RESULTS = {
    SPORTS_FETCH_OK: ('b-done', 'OK'),
    SPORTS_FETCH_EMPTY: ('b-abort', 'Nothing listed'),
    SPORTS_FETCH_SHRUNK: ('b-warn', 'Fewer games'),
    SPORTS_FETCH_REFUSED: ('b-fail', 'Refused'),
    SPORTS_FETCH_RATE_LIMITED: ('b-warn', 'Slowed down'),
}

SECTIONS = ('about', 'upcoming', 'updates')
SECTION_NAMES = {'about': 'This league', 'upcoming': 'Upcoming games', 'updates': 'Schedule updates'}
SECTIONS_PREF_KEY = 'sports_league_sections'
#: The latest queue rows Schedule updates shows. Team lookups only show when one has a problem.
UPDATES_SHOWN = 12
SCOPE_FOLLOWED = 'followed'
SCOPE_ALL = 'all'

#: Why a league with a follow has no game to show, by its status. A refusal must not read as "no games".
NO_GAME_HEAD = {
    STATUS_FAILED: ('Not known', 'The last update failed'),
    STATUS_SLOW: ('Not known', 'Waiting to ask again'),
    STATUS_FILLING: ('Not known yet', 'Still getting the schedule'),
    STATUS_LOOK: ('Nothing listed', 'In the days this schedule covers'),
    STATUS_OK: ('Nothing listed', 'In the days this schedule covers'),
    STATUS_OFF: ('Not followed', 'No calls are being made'),
}


def league_status(follows: list, fetches: list, missing: int) -> str:
    """One word for a league, from what its queue recorded. Every state is named."""
    if not follows:
        return STATUS_OFF
    outcomes = {f.outcome for f in fetches}
    if SPORTS_FETCH_REFUSED in outcomes:
        return STATUS_FAILED
    if SPORTS_FETCH_RATE_LIMITED in outcomes:
        return STATUS_SLOW
    if any(f.last_at is None for f in fetches):
        return STATUS_FILLING
    if (SPORTS_FETCH_SHRUNK in outcomes or missing or any(f.dropped_count for f in fetches)
            or any(f.kind == SPORTS_KIND_LEAGUE and f.outcome == SPORTS_FETCH_EMPTY for f in fetches)):
        return STATUS_LOOK
    return STATUS_OK


def _noun(league, events=()) -> tuple:
    """(one, many). The shape comes from the events; a league with none yet reads by its sport."""
    if any(event_shape_is_matchup(e) for e in events):
        return 'game', 'games'
    if league.sport == 'Motorsport':
        return 'race', 'races'
    return ('event', 'events') if events else ('game', 'games')


def event_when(event, now, tz, h24) -> tuple:
    """(day, time) for a game. The time is strTimestamp, which is UTC, in the display timezone. A
    field event at exactly 00:00 has no real time, so it says so, and its day is TheSportsDB's own
    date: that placeholder moved into a time zone can land on the wrong day (design 7). No league
    has a trust measurement until the matcher exists, so a matchup at 00:00 shows as listed, since
    that is a real 7pm ET for half the NHL."""
    start = event.start_at
    if start is None or (not event_shape_is_matchup(event) and (start.hour, start.minute) == (0, 0)):
        return date_label(event.local_date, now, tz), ''
    return day_label(start, now, tz), format_local(start, 'clock', tz=tz, h24=h24)


def _when_text(day: str, clock: str) -> str:
    return f'{day} {clock}' if clock else day


def _game_row(event, now, tz, h24) -> dict:
    day, clock = event_when(event, now, tz, h24)
    matchup = event_shape_is_matchup(event)
    game = f'{event.away_name} at {event.home_name}' if matchup and event.away_name and event.home_name else event.name
    notes = []
    if event.postponed:
        notes.append('Postponed')
    # A field event's old start may have been the 00:00 placeholder, which is no time to have moved from.
    if matchup and event.previous_start is not None and event.start_at is not None:
        was_day, was_clock = day_label(event.previous_start, now, tz), format_local(
            event.previous_start, 'clock', tz=tz, h24=h24)
        notes.append(f'Moved from {_when_text(was_day, was_clock)}')
    if event.missing_since is not None:
        notes.append(f"Not in TheSportsDB's latest answer since {date_label(to_local(event.missing_since, tz).date(), now, tz)}")
    return {'day': day, 'time': clock, 'game': game, 'notes': notes,
            'where': ', '.join(x for x in (event.venue, event.city) if x)}


def _is_followed(event, follows) -> bool:
    teams = {f.team_tsdb_id for f in follows}
    return 0 in teams or event.home_team_id in teams or event.away_team_id in teams


def _upcoming_query(now, settings, tz):
    today = to_local(now, tz).date()
    return SportsEvent.query.filter(
        or_(SportsEvent.start_at >= now,
            # A placeholder's 00:00 can already be behind us on the day it means.
            SportsEvent.local_date >= today),
        or_(SportsEvent.start_at.is_(None),
            SportsEvent.start_at <= now + timedelta(days=settings['horizon_days'])),
    ).order_by(SportsEvent.start_at.is_(None), SportsEvent.start_at, SportsEvent.id)


def _still_ahead(event, now, tz) -> bool:
    if event.start_at is None or (not event_shape_is_matchup(event) and (event.start_at.hour, event.start_at.minute) == (0, 0)):
        return event.local_date >= to_local(now, tz).date()
    return event.start_at >= now


def _follow_text(league, follows, team_names, plural) -> str:
    teams = [team_names.get(f.team_tsdb_id, str(f.team_tsdb_id)) for f in follows if f.team_tsdb_id]
    if any(f.team_tsdb_id == 0 for f in follows):
        return f'All {plural}'
    return ', '.join(sorted(teams)) if teams else 'Nothing'


def _next_update(fetches, now, tz, h24) -> str:
    ahead = [f.next_due_at for f in fetches if f.next_due_at is not None]
    if not ahead:
        return 'None scheduled'
    soonest = min(ahead)
    if soonest <= now:
        return 'Next in the queue'
    return _when_text(day_label(soonest, now, tz), format_local(soonest, 'clock', tz=tz, h24=h24))


def _last_updated(fetches):
    done = [f.last_at for f in fetches if f.last_at is not None]
    return max(done) if done else None


def _group(rows, key) -> dict:
    # A league has many of each, so these are lists rather than a {league: row} keyed on a non-unique id.
    out = {}
    for row in rows:
        out.setdefault(key(row), []).append(row)
    return out


def _load_all():
    fetches = _group(SportsFetch.query.all(), lambda f: f.league_id)
    follows = _group(SportsFollow.query.all(), lambda f: f.league_id)
    missing = dict(db.session.query(SportsEvent.league_id, func.count(SportsEvent.id))
                   .filter(SportsEvent.missing_since.isnot(None)).group_by(SportsEvent.league_id).all())
    teams = {t.tsdb_id: t.full_name for t in SportsTeam.query.all()}
    return fetches, follows, missing, teams


def _banner(statuses, fetches_by_league, follows, state, settings, now, tz, h24):
    """What the one worker is doing, above the list. None when nothing is followed. It only says
    "up to date" when every followed league is."""
    followed = [lid for lid, rows in follows.items() if rows]
    if not followed:
        return None
    rows = [f for lid in followed for f in fetches_by_league.get(lid, [])]
    # eventsround is undocumented, so it is the call that can go away while the others still answer.
    rounds = [f for f in rows if f.last_at is not None and f.kind == SPORTS_KIND_ROUND]
    answered = rounds or [f for f in rows if f.last_at is not None]
    if answered and all(f.outcome == SPORTS_FETCH_REFUSED for f in answered):
        since = min(f.failing_since or f.last_at for f in answered)
        who = 'for every league ' if len(followed) > 1 else ''
        return {'tone': 'bad', 'title': f'TheSportsDB has refused schedule calls {who}since {date_label(to_local(since, tz).date(), now, tz)}.',
                'sub': 'Schedules have stopped updating, and ChannelBin keeps trying every few hours. '
                       'Recordings and auto-record rules work from the guide and are not affected.',
                'settings': False}
    if state is not None and state.backoff_seconds:
        limited = [f.last_at for f in rows if f.outcome == SPORTS_FETCH_RATE_LIMITED]
        at = f' at {format_local(max(limited), "clock", tz=tz, h24=h24)}' if limited else ''
        return {'tone': 'warn', 'title': f'TheSportsDB asked ChannelBin to slow down{at}.',
                'sub': f'Calls are now about {_minutes(state.backoff_seconds)} apart, and clean calls bring that back down. '
                       'Nothing stored has changed, and recordings are not affected.',
                'settings': True}
    failed = sum(1 for s in statuses if s == STATUS_FAILED)
    if failed:
        return {'tone': 'warn', 'title': f"{counted(failed, 'league')} could not update.",
                'sub': 'Open it to see why. What is stored has not changed, and ChannelBin tries again later.',
                'settings': False}
    filling = [f for f in rows if f.last_at is None]
    if filling:
        names = sorted({lid for lid in {f.league_id for f in filling}})
        return {'tone': 'plain', 'title': f'Getting schedules: at least {counted(len(filling), "more call")}.',
                'sub': f'ChannelBin makes one call at a time, about {_minutes(settings["call_spacing_seconds"])} apart, '
                       f'so {"a new league takes" if len(names) == 1 else "new leagues take"} a while.',
                'settings': True}
    look = sum(1 for s in statuses if s == STATUS_LOOK)
    if look:
        return {'tone': 'warn', 'title': f"{counted(look, 'league')} {'needs' if look == 1 else 'need'} a look.",
                'sub': 'Its last update came back different from what was stored. Nothing was deleted.',
                'settings': False}
    return {'tone': 'ok', 'title': f'Schedules are up to date, {settings["horizon_days"]} days ahead.',
            'sub': f'The next update is {_next_update(rows, now, tz, h24).lower()}. A schedule normally costs a few calls a day.',
            'settings': False}


def _minutes(seconds: int) -> str:
    if seconds < 90:
        return f'{seconds} seconds' if seconds != 60 else 'a minute'
    return counted(int(round(seconds / 60)), 'minute')


def list_context() -> dict:
    now = datetime.utcnow()
    tz, h24 = get_display_tz(), is_24h()
    settings = _settings()
    leagues = SportsLeague.query.order_by(SportsLeague.name).all()
    fetches, follows, missing, teams = _load_all()
    upcoming = _group([e for e in _upcoming_query(now, settings, tz).all() if _still_ahead(e, now, tz)],
                      lambda e: e.league_id)

    rows = []
    for league in leagues:
        lfollows = follows.get(league.id, [])
        lfetches = fetches.get(league.id, [])
        events = upcoming.get(league.id, [])
        status = league_status(lfollows, lfetches, missing.get(league.id, 0))
        badge, label, edge, rank, tip = STATUSES[status]
        mine = [e for e in events if _is_followed(e, lfollows)]
        _, plural = _noun(league, events)
        updated = _last_updated(lfetches)
        first = mine[0] if mine else None
        nxt = None
        if first is not None:
            nxt = _game_row(first, now, tz, h24)
        rows.append({
            'id': league.id, 'name': league.name, 'status': status, 'badge': badge, 'label': label,
            'edge': edge, 'rank': rank, 'tip': tip, 'followed': bool(lfollows),
            'summary': _follow_text(league, lfollows, teams, plural) if lfollows else (league.sport or 'Not followed'),
            'next': nxt, 'next_sort': _sort_key(first), 'no_game': NO_GAME_HEAD[status],
            'count': len(mine) if lfollows else 0,
            'updated': ago(updated, now), 'updated_at': format_local(updated, 'day_datetime', none_value='', tz=tz, h24=h24),
            'updated_sort': int((now - updated).total_seconds()) if updated else 10 ** 9,
            'has_teams': any(event_shape_is_matchup(e) for e in events),
            'dialog': _dialog(league, lfollows, teams, events, settings),
        })
    rows.sort(key=lambda r: (r['next_sort'], r['name']))
    followed = sum(1 for r in rows if r['followed'])
    return {
        'rows': rows, 'followed': followed, 'not_followed': len(rows) - followed,
        'upcoming_total': sum(r['count'] for r in rows),
        'banner': _banner([r['status'] for r in rows], fetches, follows, db.session.get(SportsState, 1),
                          settings, now, tz, h24),
    }


def _sort_key(event) -> str:
    """Soonest game first, and a league with no game last."""
    if event is None:
        return '9999'
    return (event.start_at or datetime.combine(event.local_date, datetime.min.time())).strftime('%Y%m%d%H%M')


def _dialog(league, follows, teams, events, settings) -> dict:
    """What the row's dialogs act on, read from the row itself so it is never older than the page."""
    names = {}
    for e in events:
        if e.home_team_id:
            names[e.home_team_id] = e.home_name
        if e.away_team_id:
            names[e.away_team_id] = e.away_name
    return {'id': league.id, 'name': league.name, 'stored': len(events), 'keep_days': settings['history_keep_days'],
            'chosen': sorted(f.team_tsdb_id for f in follows if f.team_tsdb_id),
            'whole': any(f.team_tsdb_id == 0 for f in follows),
            'followed': bool(follows),
            'teams': sorted(({'id': i, 'name': teams.get(i) or n} for i, n in names.items()),
                            key=lambda t: t['name'] or '')}


def _round_dates(league_id, now, tz) -> dict:
    """(season, round) to the dates its games fall on, for Schedule updates."""
    rows = (db.session.query(SportsEvent.season, SportsEvent.round, func.min(SportsEvent.start_at),
                             func.max(SportsEvent.start_at))
            .filter(SportsEvent.league_id == league_id, SportsEvent.start_at.isnot(None))
            .group_by(SportsEvent.season, SportsEvent.round).all())
    out = {}
    for season, number, lo, hi in rows:
        a, b = to_local(lo, tz), to_local(hi, tz)
        same = a.date() == b.date()
        out[(season, number)] = (f'{a:%b} {a.day}' if same else f'{a:%b} {a.day} to {b:%b} {b.day}', same)
    return out


def _update_row(fetch, rounds, one, many, now, tz, h24) -> dict:
    sub = ''
    if fetch.kind == SPORTS_KIND_ROUND:
        _, _, season, number = fetch.target.split(':')
        dates = rounds.get((season, int(number)))
        what = (f'The {one} on {dates[0]}' if dates[1] else f'{many.capitalize()} from {dates[0]}') if dates else 'A round with nothing listed yet'
        sub = f'TheSportsDB calls this round {number}'
    elif fetch.kind == SPORTS_KIND_NEXT:
        what = f'The next {one} in the league'
    elif fetch.kind == SPORTS_KIND_LEAGUE:
        what = "The league's details"
    elif fetch.kind == SPORTS_KIND_TEAM:
        what = 'A team name'
    else:
        raise ValueError(f'unknown sports fetch kind {fetch.kind!r}')
    if fetch.last_at is None:
        return {'when': 'Waiting', 'what': what, 'sub': sub, 'badge': None, 'note': ''}
    badge = RESULTS[fetch.outcome]
    return {'when': _when_text(day_label(fetch.last_at, now, tz), format_local(fetch.last_at, 'clock', tz=tz, h24=h24)),
            'what': what, 'sub': sub, 'badge': badge, 'note': fetch.reason or ''}


def _notices(league, status, fetches, missing_events, one, many, stored, keep_days, now, tz) -> list:
    """Why the league's badge says what it says: (tone, strong, text). Each failure names itself."""
    out = []
    if status == STATUS_OFF:
        out.append(('info', 'Not followed.', f'No calls are being made. Its {counted(stored, one, many)} stay until {counted(keep_days, "day")} after each one.'))
    if any(f.kind == SPORTS_KIND_LEAGUE and f.outcome == SPORTS_FETCH_EMPTY for f in fetches):
        out.append(('warn', 'TheSportsDB no longer lists this league.', 'What is stored has not changed.'))
    for f in fetches:
        if f.kind == SPORTS_KIND_NEXT and f.dropped_count:
            out.append(('warn', f'Its next {one} could not be used.', f.reason or ''))
    for f in fetches:
        if f.outcome == SPORTS_FETCH_REFUSED:
            since = date_label(to_local(f.failing_since or f.last_at, tz).date(), now, tz)
            out.append(('bad', 'The last update failed.', f'{f.reason} First seen {since}. What is stored has not changed, and ChannelBin tries again later.'))
            break
    if any(f.outcome == SPORTS_FETCH_RATE_LIMITED for f in fetches):
        out.append(('warn', 'TheSportsDB asked ChannelBin to slow down.', 'That is not read as "no games", and nothing stored has changed.'))
    for f in fetches:
        if f.outcome == SPORTS_FETCH_SHRUNK:
            out.append(('warn', 'An update came back smaller.', f.reason or ''))
    for e in missing_events[:5]:
        since = date_label(to_local(e.missing_since, tz).date(), now, tz)
        out.append(('warn', f'A {one} is missing.', f"{e.away_name} at {e.home_name}" if event_shape_is_matchup(e) else e.name))
        out[-1] = (out[-1][0], out[-1][1], f"{out[-1][2]} was not in TheSportsDB's latest answer on {since}. It is kept for 7 days in case it moved.")
    dropped = sum(f.dropped_count or 0 for f in fetches if f.kind != SPORTS_KIND_NEXT)
    if dropped:
        out.append(('warn', f'{counted(dropped, "row")} from TheSportsDB could not be used.', 'Schedule updates below says why.'))
    if status == STATUS_FILLING:
        left = sum(1 for f in fetches if f.last_at is None)
        out.append(('info', "Getting this league's schedule.", f'At least {counted(left, "more call")}, spread out so they are easy on TheSportsDB.'))
    return out


def _no_games_note(status: str, horizon_days: int) -> str:
    """Why the Upcoming card is empty. Every status is named, and a refusal never reads as "no games"."""
    return {
        STATUS_FAILED: 'None stored. The last update failed, so this says nothing about what is coming up.',
        STATUS_SLOW: 'None stored. TheSportsDB asked ChannelBin to slow down, and it will ask again.',
        STATUS_FILLING: 'Nothing yet. ChannelBin is still getting this schedule.',
        STATUS_LOOK: f'TheSportsDB lists nothing in the next {horizon_days} days, and its last update came back different. See above.',
        STATUS_OK: f'TheSportsDB lists nothing in the next {horizon_days} days. That does not prove the season is over, so ChannelBin keeps checking.',
        STATUS_OFF: f'Nothing stored for the next {horizon_days} days, and no calls are being made.',
    }[status]


def page_context(league_id: int, scope: str = SCOPE_FOLLOWED):
    league = db.session.get(SportsLeague, league_id)
    if league is None:
        return None
    now = datetime.utcnow()
    tz, h24 = get_display_tz(), is_24h()
    settings = _settings()
    fetches = SportsFetch.query.filter_by(league_id=league_id).all()
    follows = SportsFollow.query.filter_by(league_id=league_id).all()
    teams = {t.tsdb_id: t.full_name for t in SportsTeam.query.all()}
    all_events = [e for e in _upcoming_query(now, settings, tz).filter(SportsEvent.league_id == league_id).all()
                  if _still_ahead(e, now, tz)]
    missing_events = SportsEvent.query.filter(SportsEvent.league_id == league_id,
                                              SportsEvent.missing_since.isnot(None)).order_by(SportsEvent.start_at).all()
    stored = SportsEvent.query.filter_by(league_id=league_id).count()
    one, many = _noun(league, all_events)
    status = league_status(follows, fetches, len(missing_events))
    badge, label, _edge, _rank, tip = STATUSES[status]
    whole = any(f.team_tsdb_id == 0 for f in follows)
    picked_teams = bool(follows) and not whole
    mine = [e for e in all_events if _is_followed(e, follows)] if follows else all_events
    shown = all_events if (scope == SCOPE_ALL or not picked_teams) else mine
    updated = _last_updated(fetches)
    follow_text = _follow_text(league, follows, teams, many)
    if picked_teams:
        lead = f'Following {follow_text.replace(", ", " and ")}.'
    elif whole:
        lead = f'Following all {many}.'
    else:
        lead = 'Not followed.'
    bits = [b for b in (league.sport, f'{league.current_season} season' if league.current_season else None) if b]
    rounds = _round_dates(league_id, now, tz)
    update_rows = sorted(
        (f for f in fetches if f.kind != SPORTS_KIND_TEAM or f.outcome in (SPORTS_FETCH_REFUSED, SPORTS_FETCH_RATE_LIMITED)),
        key=lambda f: (f.last_at is None, -(f.last_at.timestamp() if f.last_at else 0)))[:UPDATES_SHOWN]
    next_update = _next_update(fetches, now, tz, h24) if follows else 'None'
    if follows:
        message = (f'Updated {ago(updated, now)}. Next update: {next_update.lower()}.' if updated
                   else f'Not updated yet. Next update: {next_update.lower()}.')
    else:
        message = f'Last updated {ago(updated, now)}.' if updated else 'No calls are being made.'
    return {
        'league': league, 'status': status, 'badge': badge, 'label': label, 'tip': tip, 'lead': f'{lead} {", ".join(bits)}.' if bits else lead,
        'added': format_local(league.created_at, 'date', tz=tz, h24=h24), 'message': message, 'followed': bool(follows),
        'notices': _notices(league, status, fetches, missing_events, one, many, stored, settings['history_keep_days'], now, tz),
        'no_games_note': _no_games_note(status, settings['horizon_days']),
        'games': [_game_row(e, now, tz, h24) for e in shown], 'one': one, 'many': many,
        'picked_teams': picked_teams, 'scope': SCOPE_ALL if scope == SCOPE_ALL and picked_teams else SCOPE_FOLLOWED,
        'mine_total': len(mine), 'all_total': len(all_events), 'follow_text': follow_text if follows else 'Nothing',
        'next_update': next_update, 'horizon_days': settings['horizon_days'],
        'updates': [_update_row(f, rounds, one, many, now, tz, h24) for f in update_rows],
        'dialog': _dialog(league, follows, teams, all_events, settings),
        'can_change_teams': any(event_shape_is_matchup(e) for e in all_events),
        'section_names': {**SECTION_NAMES, 'upcoming': f'Upcoming {many}'},
        'league_url': f'https://www.thesportsdb.com/league/{league.tsdb_id}',
    }


def add_dialog_config() -> dict:
    """The shipped list, for Add a league. Which are followed already comes with the page."""
    followed = {tsdb for (tsdb,) in db.session.query(SportsLeague.tsdb_id).join(
        SportsFollow, SportsFollow.league_id == SportsLeague.id).distinct().all()}
    return {'shipped': [{'tsdb_id': s['tsdb_id'], 'name': s['name'], 'sport': s['sport'],
                         'followed': s['tsdb_id'] in followed} for s in SHIPPED_LEAGUES],
            'site': 'https://www.thesportsdb.com/'}
