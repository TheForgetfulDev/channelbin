"""What the Sports Schedules pages show (dev/docs/DESIGN-sports-data.md 11).

A fixed number of queries per page, however many leagues and games there are: tz and the clock
format are read once and handed down, and nothing is asked per row. Status comes from what the queue
recorded, never from a count of what is stored. "Round" is TheSportsDB's word for one call's worth of
games and means nothing to a viewer, so it appears only beside the dates in Schedule updates.
"""
import json
from datetime import datetime, timedelta

from sqlalchemy import func, or_

from . import db, sports_match
from .database import (
    SPORTS_FETCH_EMPTY, SPORTS_FETCH_OK, SPORTS_FETCH_RATE_LIMITED, SPORTS_FETCH_REFUSED,
    SPORTS_FETCH_SHRUNK, SPORTS_KIND_LEAGUE, SPORTS_KIND_NEXT, SPORTS_KIND_ROUND, SPORTS_KIND_TEAM,
    SPORTS_AIRING_UNKNOWN, SPORTS_MATCH_STRONG, EPGEntry, SportsEvent, SportsFetch, SportsFollow, SportsLeague, SportsLeagueCategory, SportsState, SportsTeam,
)
from .config import load_config
from .fmt_utils import counted
from .sports_data import SHIPPED_LEAGUES, _settings, event_shape_is_matchup, link_has_time
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
    STATUS_OFF: ('b-paused', 'Not followed', 'rs-paused', 5, "ChannelBin isn't checking TheSportsDB for this league."),
}

#: outcome: (badge class, label)
RESULTS = {
    SPORTS_FETCH_OK: ('b-done', 'OK'),
    SPORTS_FETCH_EMPTY: ('b-abort', 'Nothing listed'),
    SPORTS_FETCH_SHRUNK: ('b-warn', 'Fewer games'),
    SPORTS_FETCH_REFUSED: ('b-fail', 'Refused'),
    SPORTS_FETCH_RATE_LIMITED: ('b-warn', 'Slowed down'),
}

SECTIONS = ('about', 'upcoming', 'matching', 'updates')
SECTION_NAMES = {'about': 'This league', 'upcoming': 'Upcoming games', 'matching': 'Matching your guide',
                 'updates': 'Schedule updates'}
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
    STATUS_OFF: ('Not followed', 'Not checking for games'),
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


def event_noun(league, event) -> str:
    """What one event is called in prose: game, race or event."""
    return _noun(league, [event])[0] if league is not None else 'game'


def event_when(event, now, tz, h24) -> tuple:
    """(day, time) for a game. The time is strTimestamp, which is UTC, in the display timezone. A
    field event at exactly 00:00 has no real time, so it says so, and its day is TheSportsDB's own
    date: that placeholder moved into a time zone can land on the wrong day (design 7). The
    league's trust measurement (sports_match.league_trusted()) is not read here, so a matchup at
    00:00 shows as listed, since that is a real 7pm ET for half the NHL."""
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
    from .database import AutoRule
    rule_names = dict(db.session.query(AutoRule.id, AutoRule.name).filter(AutoRule.sports_league_id.isnot(None)))

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
            # The person's own follow, as on the league's page; a rule's is changed on the rule.
            'dialog': _dialog(league, [f for f in lfollows if not f.rule_id], teams, events, settings,
                              sorted(rule_names[f.rule_id] for f in lfollows if f.rule_id in rule_names)),
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


def _dialog(league, follows, teams, events, settings, rules=()) -> dict:
    """What the row's dialogs act on, read from the row itself so it is never older than the page.
    `rules` names the auto-record rules that name the league, so Delete league says it cannot before
    it is pressed (dev/changelog/1491)."""
    names = {}
    for e in events:
        if e.home_team_id:
            names[e.home_team_id] = e.home_name
        if e.away_team_id:
            names[e.away_team_id] = e.away_name
    return {'id': league.id, 'name': league.name, 'stored': len(events), 'keep_days': settings['history_keep_days'],
            'chosen': sorted(f.team_tsdb_id for f in follows if f.team_tsdb_id),
            'whole': any(f.team_tsdb_id == 0 for f in follows),
            'followed': bool(follows), 'rules': list(rules),
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
        kept = f" It still has {counted(stored, one, many)}, and deletes each one {counted(keep_days, 'day')} after it's over." if stored else ''
        out.append(('info', 'Not followed.', f"ChannelBin isn't checking TheSportsDB for new {many}.{kept}"))
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
        STATUS_OFF: f"Nothing stored for the next {horizon_days} days, and ChannelBin isn't checking for more.",
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
    # A rule's follow is changed on the rule, so the buttons and the Following row are about the
    # person's own; the rules that follow it have a row of their own (dev/changelog/1490).
    person = [f for f in follows if not f.rule_id]
    rule_follows = _rule_follows(follows, teams)
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
    state = db.session.get(SportsState, 1)
    checked_at = state.last_match_at if state is not None else None
    counts = sports_match.game_counts([e.id for e in all_events])
    names = sports_match.name_counts([e.id for e in shown])
    if follows:
        message = (f'Updated {ago(updated, now)}. Next update: {next_update.lower()}.' if updated
                   else f'Not updated yet. Next update: {next_update.lower()}.')
    else:
        message = f'Last updated {ago(updated, now)}.' if updated else 'Not checking for games.'
    return {
        'league': league, 'status': status, 'badge': badge, 'label': label, 'tip': tip, 'lead': f'{lead} {", ".join(bits)}.' if bits else lead,
        'added': format_local(league.created_at, 'date', tz=tz, h24=h24), 'message': message, 'followed': bool(person),
        'notices': _notices(league, status, fetches, missing_events, one, many, stored, settings['history_keep_days'], now, tz),
        'no_games_note': _no_games_note(status, settings['horizon_days']),
        'games': _enrich_games([_game_row(e, now, tz, h24) for e in shown], shown, counts, names, checked_at,
                               now, tz, h24),
        'one': one, 'many': many,
        'trust': _trust_text(league), 'upcoming_note': _upcoming_note(league, many, settings['horizon_days']),
        'match_note': _match_note(state, sum(1 for e in all_events if (counts.get(e.id) or {}).get('channels')),
                                  len(all_events), one, many, now, tz, h24),
        'match_waiting': bool(state and (state.match_note or '').startswith(('Waiting', 'The last run'))),
        'matching': _matching_context(league),
        'picked_teams': picked_teams, 'scope': SCOPE_ALL if scope == SCOPE_ALL and picked_teams else SCOPE_FOLLOWED,
        'mine_total': len(mine), 'all_total': len(all_events),
        'follow_text': _follow_text(league, person, teams, many) if person else 'Nothing',
        'next_update': next_update, 'horizon_days': settings['horizon_days'],
        'updates': [_update_row(f, rounds, one, many, now, tz, h24) for f in update_rows],
        # Change teams and Stop following act on the person's own follow only (sports_data.
        # set_following); a rule's follow is changed on the rule (dev/changelog/1490).
        'dialog': _dialog(league, person, teams, all_events, settings,
                          sorted({r['name'] for r in rule_follows})),
        'rule_follows': rule_follows,
        'can_change_teams': any(event_shape_is_matchup(e) for e in all_events),
        'section_names': {**SECTION_NAMES, 'upcoming': f'Upcoming {many}'},
        'league_url': f'https://www.thesportsdb.com/league/{league.tsdb_id}',
    }


def _rule_follows(follows, teams) -> list:
    """[{name, url, what}] for each auto-record rule that follows this league (design 3): a follow a
    person cannot remove here, so the page says whose it is."""
    from flask import url_for
    from .database import AutoRule
    ids = sorted({f.rule_id for f in follows if f.rule_id})
    if not ids:
        return []
    names = dict(db.session.query(AutoRule.id, AutoRule.name).filter(AutoRule.id.in_(ids)))
    out = []
    for f in sorted((f for f in follows if f.rule_id in names), key=lambda f: names[f.rule_id].lower()):
        what = teams.get(f.team_tsdb_id, f'Team #{f.team_tsdb_id}') if f.team_tsdb_id else 'All its games'
        out.append({'name': names[f.rule_id], 'what': what,
                    'url': url_for('auto_rules.rule_page', rule_id=f.rule_id)})
    return out


def _match_note(state, found: int, total: int, one: str, many: str, now, tz, h24) -> str:
    """The Checked against your guide row: this league's own figure and when the matcher last ran, or
    what it is waiting for, with the last good run's time kept (6.6). The run's stored note is across
    every league, so only its waiting or failure sentence is shown here."""
    if state is None or state.last_match_at is None:
        lead = state.match_note if state is not None and state.match_note else 'Not yet.'
        return f'{lead} It runs after the next guide update.' if lead == 'Not yet.' else lead
    when = _when_text(day_label(state.last_match_at, now, tz), format_local(state.last_match_at, 'clock', tz=tz, h24=h24))
    figure = f'Found {found} of {counted(total, "upcoming " + one, "upcoming " + many)} in your guide.' if total else ''
    note = state.match_note or ''
    if note.startswith(('Waiting', 'The last run')):
        return f'{note} Last checked {when.lower() if when[:1] in "TY" else when}. {figure}'.strip()
    return f'{when}. {figure}'.strip()


def _trust_text(league) -> tuple:
    """(value, note) for the Start times row (design 7)."""
    sample, agreed = league.trust_sample or 0, league.trust_agreed or 0
    if not sample:
        return 'Not measured yet', ''
    text = f'Match your guide on {agreed} of the last {counted(sample, "event")}'
    if sports_match.league_trusted(league):
        return text, ''
    if sample < sports_match.TRUST_MIN_SAMPLE:
        return text, 'Too few checked yet to rely on'
    return text, 'Not enough to rely on'


def _upcoming_note(league, many, horizon_days) -> str:
    sample, agreed = league.trust_sample or 0, league.trust_agreed or 0
    head = f'The next {horizon_days} days. Times are what TheSportsDB lists.'
    if not sample:
        return (f'{head} ChannelBin has not checked enough of these {many} against your guide to rely on them yet, '
                'so the earliest listing of each counts as its first airing.')
    if sports_match.league_trusted(league):
        return (f'{head} They matched your guide on {agreed} of the last {sample}. Where TheSportsDB and your guide '
                'disagree, both times are shown.')
    if sample < sports_match.TRUST_MIN_SAMPLE:
        return (f'{head} They matched your guide on {agreed} of the {sample} checked so far, too few to rely on yet, so '
                'ChannelBin goes by the earliest listing to tell a first airing from a replay.')
    return (f'{head} They matched your guide on only {agreed} of the last {sample}, so ChannelBin shows them but '
            'goes by the earliest listing to tell a first airing from a replay.')


def _airing_text(c: dict) -> str:
    """How a game's live listings air, every combination named."""
    first, replay, unknown = c['first'], c['replay'], c['unknown']
    if first:
        tail = [x for x in (counted(replay, 'replay') if replay else '', f'{unknown} not known' if unknown else '') if x]
        return f'Live, and {" and ".join(tail)} later' if replay and not unknown else (
            f'Live, {", ".join(tail)}' if tail else 'Live')
    if replay and unknown:
        return f'{counted(replay, "replay")}, {unknown} not known if live'
    if replay:
        return 'Replays only'
    if unknown:
        return 'Not known yet if live or replay'
    raise ValueError(f'a game with live listings and no airing: {c!r}')


def _guide_cell(event, c, n, checked_at, guide_end, now, tz) -> dict:
    """What the Where it's on column says for one event: its guide listings, else the event channels
    naming it (design 6.8), else why there are none. Every state is named, so an empty cell is never the
    rendering of "not checked". `names_only` picks the dialog's title and the row menu's wording."""
    kept, wrong_names = (n['channels'], n['wrong']) if n else (0, 0)
    also = f'Also named by {counted(kept, "event channel")}' if kept else ''
    if c and c['channels']:
        return {'state': 'found', 'text': f'On {counted(c["channels"], "channel")}', 'sub': _airing_text(c),
                'also': also, 'open': True, 'names_only': False}
    if c and c['wrong']:
        return {'state': 'all_wrong', 'text': 'On 0 channels', 'open': True, 'also': also, 'names_only': False,
                'sub': f'All {counted(c["wrong"], "listing")} marked a wrong match. Open to undo.'}
    start = event.start_at or datetime.combine(event.local_date, datetime.min.time())
    past_guide = guide_end is None or start > guide_end
    if kept:
        # Two facts, not one: "it reaches" read as when the game would show up (dev/changelog/1490).
        where = (f'Not in your guide yet. Your guide reaches {day_label(guide_end, now, tz)}.' if guide_end and past_guide
                 else 'Not in your guide yet.' if past_guide else 'No listing in your guide names it.')
        read = 'Read from its name.' if kept == 1 else 'Read from their names.'
        return {'state': 'names', 'text': f'On {counted(kept, "event channel")}', 'sub': f'{read} {where}',
                'also': '', 'open': True, 'names_only': True}
    if wrong_names:
        return {'state': 'names_wrong', 'text': 'On 0 event channels', 'also': '', 'open': True, 'names_only': True,
                'sub': f'All {counted(wrong_names, "event channel")} marked a wrong match. Open to undo.'}
    if checked_at is None or (event.first_seen is not None and event.first_seen > checked_at):
        return {'state': 'unchecked', 'text': 'Not checked yet', 'open': False,
                'sub': 'ChannelBin looks for it in your guide after the next guide update.'}
    if past_guide:
        reach = f'Your guide reaches {day_label(guide_end, now, tz)}. ' if guide_end else ''
        return {'state': 'later', 'text': 'Not in your guide yet', 'open': False,
                'sub': f'{reach}Checked again after each guide update.'}
    what = ('No listing names the league and both teams near this time.' if event_shape_is_matchup(event)
            else 'No listing names the series on this day.')
    return {'state': 'missing', 'text': 'Not found in your guide', 'open': False, 'sub': what}


def _enrich_games(rows, events, counts, names, checked_at, now, tz, h24) -> list:
    """The league page's extra columns, from counts read once for every shown event. Both times show
    only for a game with live listings and a real time from TheSportsDB: a race's 00:00 placeholder is
    not a time to disagree with (7)."""
    guide_end = db.session.query(func.max(EPGEntry.start_time)).scalar()
    for row, event in zip(rows, events):
        c = counts.get(event.id)
        row['id'] = event.id
        row['tsdb_url'] = f'https://www.thesportsdb.com/event/{event.tsdb_id}'
        row['guide'] = _guide_cell(event, c, names.get(event.id), checked_at, guide_end, now, tz)
        row['guide_time'] = (format_local(event.guide_start, 'clock', tz=tz, h24=h24)
                             if row['guide']['state'] == 'found' and row['time'] and sports_match.time_disagrees(event)
                             else '')
    return rows


def _matching_context(league) -> dict:
    """The Matching your guide card: the league's names, a known name it lacks, and its categories with
    how many channels each has now. A category gone from every account is kept and says so (5.2)."""
    counts = sports_match.category_counts()
    cats = SportsLeagueCategory.query.filter_by(league_id=league.id).order_by(SportsLeagueCategory.category_name).all()
    return {
        'terms': json.loads(league.match_terms or '[]'),
        'suggested': sports_match.suggested_terms(league),
        'categories': [{'name': c.category_name, 'channels': counts.get(c.category_name, 0),
                        'replay': c.is_replay} for c in cats],
    }


def add_dialog_config() -> dict:
    """The shipped list, for Add league or team. Which are followed already comes with the page."""
    followed = {tsdb for (tsdb,) in db.session.query(SportsLeague.tsdb_id).join(
        SportsFollow, SportsFollow.league_id == SportsLeague.id).distinct().all()}
    return {'shipped': [{'tsdb_id': s['tsdb_id'], 'name': s['name'], 'sport': s['sport'],
                         'followed': s['tsdb_id'] in followed} for s in SHIPPED_LEAGUES],
            'site': 'https://www.thesportsdb.com/'}


# ---------------------------------------------------------------------------
# A recording's event, on its page (design 11)
# ---------------------------------------------------------------------------

#: Within this of the program's own start, TheSportsDB's start is not shown again.
_START_SHOWN_PAST = timedelta(minutes=5)


def _differs(minutes: int, later: bool) -> str:
    if minutes >= 120 and minutes % 60 == 0:
        amount = counted(minutes // 60, 'hour')
    else:
        amount = counted(minutes, 'minute')
    return f'{amount} {"after" if later else "before"} the guide'


def recording_context(rec, now, tz, h24, cfg=None):
    """What the recording page's Program card says about TheSportsDB, or None to say nothing.

    A fixed handful of queries for one recording. A recording with no link says nothing unless a
    followed league's text or category is on its listing, and then says why it was not matched,
    or its name is waiting for TheSportsDB fields, and then says when they fill in."""
    from .database import RecordingSports
    from .sports_data import event_title
    cfg = cfg if cfg is not None else load_config()
    link = db.session.get(RecordingSports, rec.id)
    if link is not None and link.removed_at is not None:
        return {'state': 'removed', 'event': event_title(link), 'league': link.league_name,
                'removed_on': date_label(to_local(link.removed_at, tz).date(), now, tz),
                'keeps_name': _name_waiting(rec)}
    if link is not None:
        season = ' '.join(x for x in (link.league_name, link.season) if x)
        if link.round and 0 < link.round < 100:
            season += f', round {link.round}'
        start = None
        if link_has_time(link) and rec.program_start_time is not None:
            gap = link.event_start - rec.program_start_time
            if abs(gap) > _START_SHOWN_PAST:
                start = (f'{day_label(link.event_start, now, tz)} '
                         f'{format_local(link.event_start, "clock", tz=tz, h24=h24)}, '
                         f'{_differs(int(abs(gap).total_seconds() // 60), gap > timedelta(0))}')
        return {'state': 'linked', 'event': event_title(link), 'season': season,
                'where': ', '.join(x for x in (link.venue, link.city) if x), 'start': start,
                'strong': link.strength == SPORTS_MATCH_STRONG,
                'airing': sports_match.AIRING_LABELS.get(link.airing, sports_match.AIRING_LABELS[SPORTS_AIRING_UNKNOWN]),
                'reason': link.reason,
                'tsdb_url': f'https://www.thesportsdb.com/event/{link.tsdb_event_id}',
                'art': _artwork(rec, link, cfg, now, tz, h24),
                'file_name': _future_file_name(rec, link, tz)}
    note = _unlinked_note(rec)
    if _name_waiting(rec):
        waiting = ('Its name has TheSportsDB fields, which fill in if the game is matched before its '
                   'file is written.')
        note = f'{note} {waiting}' if note else waiting
    return {'state': 'none', 'note': note} if note else None


def _name_waiting(rec) -> bool:
    """Whether this recording's name came from a template with {sports:...} fields and its file is
    still to be written, so what the join will fill in is worth saying (dev/changelog/1498). Once
    written, the page shows the file itself and its join event says how it was named."""
    from .database import REC_ENDED_STATUSES
    return bool(rec.sports_name_template) and not rec.output_path and rec.status not in REC_ENDED_STATUSES


def _future_file_name(rec, link, tz):
    """The name the join will give a linked recording's file, as it will be on disk, or None."""
    from .recorder import _safe_name
    from .sports_data import recording_file_name
    if not _name_waiting(rec):
        return None
    name, _ = recording_file_name(rec, tz)
    return _safe_name(name) if name else None


def _artwork(rec, link, cfg, now, tz, h24):
    """The Artwork row: which of TheSportsDB's images this recording has, and for each it does not,
    why (app/sports_images.py, dev/changelog/1465). `links` are the roles a View link opens."""
    from .database import REC_ENDED_STATUSES
    from .metadata_sidecar import sidecar_enabled
    from .sports_images import BACKGROUND, MISSED, OFF, POSTER, READY, REFUSED, UNUSED, artwork, images_for
    art = artwork(cfg, link, finished=rec.status in REC_ENDED_STATUSES,
                  used=sidecar_enabled(cfg, rec.profile, rec))
    if not art:
        return {'text': 'TheSportsDB has no poster or background for this event.', 'cls': 'na', 'links': []}
    if any(state == OFF for state, _, _ in art.values()):
        return {'text': 'Downloading posters and backgrounds is off in Settings.', 'cls': 'na', 'links': []}
    # With no poster the thumb is both, one file: it is named once, as the background.
    roles = ([POSTER] if link.poster_url else []) + ([BACKGROUND] if link.thumb_url else [])
    names = {POSTER: 'poster', BACKGROUND: 'background'}
    ready = [r for r in roles if art[r][0] == READY]
    out = []
    profile = rec.profile
    pinned = profile is not None and bool(profile.poster_file)
    if ready:
        what = ' and '.join(names[r] for r in ready).capitalize()
        # A pinned poster beats the thumb standing in for a missing one, whatever the profile prefers.
        out.append(f'{what} from TheSportsDB.' if link.poster_url or pinned else
                   f'{what} from TheSportsDB, also used as the poster, since the event has none.')
    known = images_for(link)
    unused = False
    for role in roles:
        state, _, reason = art[role]
        if state == READY:
            continue
        name = names[role].capitalize()
        why = f' {reason}' if reason else ''
        if state == REFUSED:
            out.append(f'{name} not downloaded.{why}')
        elif state == UNUSED:
            unused = True
        elif state == MISSED:
            out.append(f'{name} was not downloaded before the recording finished.{why}')
        else:
            img = known.get(link.poster_url if role == POSTER else link.thumb_url)
            when = (f' Tries again {day_label(img.next_try_at, now, tz)} '
                    f'{format_local(img.next_try_at, "clock", tz=tz, h24=h24)}.'
                    if img is not None and img.next_try_at and img.next_try_at > now else '')
            out.append(f'{name} not downloaded yet.{why}{when}')
    if unused:
        out.append('Not downloaded: this recording writes no metadata file, the one place they are used.')
    if pinned:
        if not profile.prefer_sports_poster:
            out.append(f'The image pinned to profile "{profile.name}" is the poster.')
        elif link.poster_url and art[POSTER][0] == READY:
            out.append(f'Profile "{profile.name}" puts this poster ahead of its pinned one.')
        elif link.poster_url and art[POSTER][0] in (MISSED, REFUSED):
            out.append(f'The image pinned to profile "{profile.name}" is the poster, since TheSportsDB\'s '
                       'was not downloaded.')
        elif link.poster_url:
            out.append(f'The image pinned to profile "{profile.name}" is the poster until TheSportsDB\'s '
                       'is downloaded.')
        else:
            out.append(f'The image pinned to profile "{profile.name}" is the poster, since the event has none.')
    return {'text': ' '.join(out), 'cls': 'ok' if len(ready) == len(roles) else 'warn',
            'links': [{'role': r, 'label': names[r]} for r in ready]}


def _unlinked_note(rec):
    """Why a recording whose listing is in a followed league has no event, in one sentence. None, and
    the page says nothing, when no followed league's terms or tied categories are on its listing."""
    from .program_titles import title_key
    from .sports_data import pick_match, recording_matches
    if rec.program_start_time is None or not rec.program_title:
        return None
    followed = {i for (i,) in db.session.query(SportsFollow.league_id).distinct()}
    category = rec.channel.category_name if rec.channel is not None else None
    leagues = [lg for lg in sports_match.leagues_for_listing(rec.program_title, rec.program_sub_title, category)
               if lg.id in followed]
    if not leagues:
        return None
    league = leagues[0]
    matches = recording_matches(rec, rejected=True)
    if any(m.rejected_at is not None for m in matches) and not any(m.rejected_at is None for m in matches):
        return (f'Its listing was marked a wrong match on the {league.name} page, so it is not linked to a '
                f'{league.name} event.')
    match, ambiguous = pick_match([m for m in matches if m.rejected_at is None], title_key(rec.program_title))
    if ambiguous:
        return (f'Its listing matched more than one {league.name} event equally well, so it was not linked to '
                f'either.')
    if match is not None:
        return ('Its listing matches a TheSportsDB event that is not linked yet. The next match run links it, '
                'and so does the recording finishing.')
    pst = rec.program_start_time
    lo, hi = pst - sports_match.WINDOW, pst + sports_match.WINDOW
    nearby = SportsEvent.query.filter(
        SportsEvent.league_id == league.id,
        or_(SportsEvent.start_at.between(lo, hi),
            (SportsEvent.start_at.is_(None) & SportsEvent.local_date.between(lo.date(), hi.date())))).count()
    if not nearby:
        hours = int(sports_match.WINDOW.total_seconds() // 3600)
        return (f'No {league.name} event within {hours} hours of this program is stored from TheSportsDB, so '
                f'there was nothing to match it to.')
    lead = (f'The one {league.name} event around then did not match its listing.' if nearby == 1 else
            f'None of the {nearby} {league.name} events around then matched its listing.')
    return (f'{lead} A match needs the league and both teams, or for a race the series and the date, in the '
            f'title or episode name, and the description is never read.')
