"""What the Sports Schedules pages show (dev/docs/DESIGN-sports-data.md 11).

A fixed number of queries per page, however many leagues and games there are: tz and the clock
format are read once and handed down, and nothing is asked per row. Status comes from what the queue
recorded, never from a count of what is stored. "Round" is TheSportsDB's word for one call's worth of
games and means nothing to a viewer, so it appears only beside the dates in Schedule updates.
"""
import json
from dataclasses import replace
from datetime import date, datetime, time, timedelta

from sqlalchemy import func, or_

from . import db, sports_match, sports_skips, sports_words
from .database import (
    SPORTS_FETCH_CAPPED, SPORTS_FETCH_CAPPED_PAST, SPORTS_FETCH_EMPTY, SPORTS_FETCH_OK, SPORTS_FETCH_RATE_LIMITED, SPORTS_FETCH_REFUSED,
    SPORTS_FETCH_SHRUNK, SPORTS_KIND_EVENT, SPORTS_KIND_LEAGUE, SPORTS_KIND_NEXT, SPORTS_KIND_ROUND, SPORTS_KIND_SEARCH,
    SPORTS_KIND_TEAM, SPORTS_KIND_TEAM_NEXT,
    SPORTS_AIRING_UNKNOWN, SPORTS_MATCH_STRONG, EPGEntry, SportsEvent, SportsFetch, SportsFollow, SportsLeague, SportsLeagueCategory, SportsState, SportsTeam,
)
from .config import load_config
from .sports_times import Rules
from .fmt_utils import counted, fmt_duration_phrase, length_words, plural
from .sports_data import (
    MISSING_KEEP_DAYS, ONE_GAME_KINDS, ROUND_ROW_CAP, SHIPPED_LEAGUES, search_parts, _settings, capped_spans, event_game_name, event_shape_is_matchup,
    link_has_time, queue_line, seconds_until_call, _is_day,
)
from .tz_utils import ago, date_label, day_label, format_local, get_display_tz, is_24h, to_local, to_naive_utc

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
    SPORTS_FETCH_SHRUNK: ('b-warn', 'Fewer {many}'),
    SPORTS_FETCH_REFUSED: ('b-fail', 'Refused'),
    SPORTS_FETCH_RATE_LIMITED: ('b-warn', 'Slowed down'),
    SPORTS_FETCH_CAPPED: ('b-warn', f'Cut at {ROUND_ROW_CAP}'),
    SPORTS_FETCH_CAPPED_PAST: ('b-warn', f'Cut at {ROUND_ROW_CAP}'),
}
SECTIONS = ('about', 'times', 'upcoming', 'feed', 'matching', 'updates')
SECTION_NAMES = {'about': 'This league', 'times': 'Game time settings', 'upcoming': 'Upcoming games',
                 'feed': 'On your IPTV output',
                 'matching': 'Matching your guide',
                 'updates': 'Schedule updates'}
SECTIONS_PREF_KEY = 'sports_league_sections'
#: The latest queue rows Schedule updates shows. Team lookups only show when one has a problem.
UPDATES_SHOWN = 12
SCOPE_FOLLOWED = 'followed'
SCOPE_ALL = 'all'

#: Why a league with a follow has no game to show, by its status. A refusal must not read as "no games".
#: `{many}` is the league's noun.
NO_GAME_HEAD = {
    STATUS_FAILED: ('Not known', 'The last update failed'),
    STATUS_SLOW: ('Not known', 'Waiting to ask again'),
    STATUS_FILLING: ('Not known yet', 'Still getting the schedule'),
    STATUS_LOOK: ('Nothing listed', 'In the days this schedule covers'),
    STATUS_OK: ('Nothing listed', 'In the days this schedule covers'),
    STATUS_OFF: ('Not followed', 'Not checking for {many}'),
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
    if any(f.last_at is None and f.kind not in ONE_GAME_KINDS for f in fetches):
        return STATUS_FILLING
    if (SPORTS_FETCH_SHRUNK in outcomes or missing or any(f.dropped_count for f in fetches)
            or any(f.kind == SPORTS_KIND_LEAGUE and f.outcome == SPORTS_FETCH_EMPTY for f in fetches)):
        return STATUS_LOOK
    return STATUS_OK


def _noun(league, events=()) -> tuple:
    """(one, many): game, match, race or event (app/sports_words.py). The shape comes from the
    events; a league with none yet reads by its sport."""
    events = list(events)
    matchup = any(event_shape_is_matchup(e) for e in events) if events else None
    return sports_words.noun(league.sport, matchup)


def event_noun(league, event) -> str:
    """What one event is called in prose: game, match, race or event."""
    return _noun(league, [event])[0] if league is not None else sports_words.GAME[0]


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


def _game_row(event, rule, now, tz, h24) -> dict:
    """One game's row. `rule` is its league's answer to which time a game goes by
    (app/sports_times.py), which the note on a game the guide is behind on names."""
    day, clock = event_when(event, now, tz, h24)
    matchup = event_shape_is_matchup(event)
    game = event_game_name(event)
    notes = []
    if event.postponed:
        notes.append('Postponed')
    # A field event's old start may have been the 00:00 placeholder, which is no time to have moved from.
    if matchup and event.previous_start is not None and event.start_at is not None:
        was_day, was_clock = day_label(event.previous_start, now, tz), format_local(
            event.previous_start, 'clock', tz=tz, h24=h24)
        notes.append(f'Moved from {_when_text(was_day, was_clock)}')
    if sports_match.kept_start(event):
        from .sports_times import GO_GUIDE, running_late
        started = format_local(event.guide_start, 'clock', tz=tz, h24=h24)
        # A started game keeps its start (dev/changelog/1626), races too, and one moved later ends
        # later by the move (dev/changelog/1632).
        note = f'It had already started when TheSportsDB moved it, so it keeps its {started} start'
        late = running_late(event)
        if late and rule.go_by != GO_GUIDE:
            note += (f'. Sports channels and automated recordings run '
                     f'{counted(int(late.total_seconds() // 60), "minute")} later to match')
        notes.append(note)
    elif matchup and event.guide_behind_start is not None:
        # The guide did not follow TheSportsDB's move (dev/changelog/1623); what follows is the
        # league's answer to which time a game goes by (dev/changelog/1630).
        from .sports_times import GO_BOTH, GO_GUIDE, GO_TSDB, acts, tsdb_start
        listed = format_local(event.guide_behind_start, 'clock', tz=tz, h24=h24)
        on = f' on {counted(event.guide_channels, "channel")}' if event.guide_channels else ''
        who = 'Sports channels and Record on this page'
        if rule.go_by == GO_GUIDE:
            then = f'{who} go by the listing'
        elif not acts(event.guide_behind_start, tsdb_start(event), rule, now):
            then = (f"{who} go by the listing until {counted(rule.wait_hours, 'hour')} before it, then "
                    + ("by TheSportsDB's time" if rule.go_by == GO_TSDB else 'cover both times'))
        elif rule.go_by == GO_TSDB:
            then = f"{who} go by TheSportsDB's time"
        elif rule.go_by == GO_BOTH:
            then = f'{who} cover both times'
        else:
            raise ValueError(f'unknown answer {rule.go_by!r}')
        notes.append(f'The guide still lists it at the old time, {listed}{on}. {then}')
    if event.missing_since is not None:
        notes.append(f"Not in TheSportsDB's latest answer since {date_label(to_local(event.missing_since, tz).date(), now, tz)}")
    return {'day': day, 'time': clock, 'game': game, 'notes': notes,
            'where': ', '.join(x for x in (event.venue, event.city) if x)}


def _is_followed(event, follows) -> bool:
    teams = {f.team_tsdb_id for f in follows}
    return 0 in teams or event.home_team_id in teams or event.away_team_id in teams


def _upcoming_query(now, settings, tz, since=None):
    """The events from `since` (default now) to the horizon. `since` earlier than now is the
    league page's _league_page_since()."""
    today = to_local(now, tz).date()
    return SportsEvent.query.filter(
        or_(SportsEvent.start_at >= (since or now),
            # A placeholder's 00:00 can already be behind us on the day it means.
            SportsEvent.local_date >= today),
        or_(SportsEvent.start_at.is_(None),
            SportsEvent.start_at <= now + timedelta(days=settings['horizon_days'])),
    ).order_by(SportsEvent.start_at.is_(None), SportsEvent.start_at, SportsEvent.id)


def _still_ahead(event, now, tz) -> bool:
    if event.start_at is None or (not event_shape_is_matchup(event) and (event.start_at.hour, event.start_at.minute) == (0, 0)):
        return event.local_date >= to_local(now, tz).date()
    return event.start_at >= now


#: How long after its start a league page keeps a game that began before today: a late game is
#: still on, or still recording, after midnight (dev/docs/BUGS.md 2026-11-14).
LEAGUE_PAGE_KEEP = timedelta(hours=6)


def _league_page_since(now, tz) -> datetime:
    """The earliest start a league page shows, as naive UTC: the start of today in the display
    time zone, or LEAGUE_PAGE_KEEP ago when that is earlier."""
    day_start = to_naive_utc(datetime.combine(to_local(now, tz).date(), time.min, tzinfo=tz))
    return min(day_start, now - LEAGUE_PAGE_KEEP)


def _on_league_page(event, since, now, tz) -> bool:
    """A league page keeps a game for the rest of the day it starts on, and at least
    LEAGUE_PAGE_KEEP after its start, so a game being recorded, one on now and a morning game's
    later replay can still be acted on there (dev/changelog/1539). The All Leagues list's next
    game is still the next one to start (_still_ahead)."""
    if event.start_at is None or (not event_shape_is_matchup(event) and (event.start_at.hour, event.start_at.minute) == (0, 0)):
        return event.local_date >= to_local(now, tz).date()
    return event.start_at >= since


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
                'sub': f'ChannelBin stops for as long as TheSportsDB asks, then calls about {_minutes(state.backoff_seconds)} '
                       'apart until clean calls bring that back down. '
                       'Nothing stored has changed, and recordings are not affected.',
                'settings': True}
    failed = sum(1 for s in statuses if s == STATUS_FAILED)
    if failed:
        return {'tone': 'warn', 'title': f"{counted(failed, 'league')} could not update.",
                'sub': 'Open it to see why. What is stored has not changed, and ChannelBin tries again later.',
                'settings': False}
    filling = [f for f in rows if f.last_at is None and f.kind not in ONE_GAME_KINDS]
    if filling:
        names = sorted({lid for lid in {f.league_id for f in filling}})
        line = queue_line(now)
        last = max((i for i, (lid, kind, never) in enumerate(line)
                    if lid in names and never and kind not in ONE_GAME_KINDS), default=None)
        # The line holds other calls too, so the wait is read from the last one's place in it.
        when = (f' The last of them runs in {fmt_duration_phrase(seconds_until_call(last, now, settings))}.'
                if last is not None else '')
        return {'tone': 'plain', 'title': f'Getting schedules: at least {counted(len(filling), "more call")}.',
                'sub': f'ChannelBin makes one call at a time, about {_minutes(settings["call_spacing_seconds"])} apart, '
                       f'so {"a new league takes" if len(names) == 1 else "new leagues take"} a while.{when}',
                'settings': True}
    look = sum(1 for s in statuses if s == STATUS_LOOK)
    if look:
        return {'tone': 'warn', 'title': f"{counted(look, 'league')} {'needs' if look == 1 else 'need'} a look.",
                'sub': 'Its last update came back different from what was stored. Nothing was deleted.',
                'settings': False}
    return {'tone': 'ok', 'title': f'Schedules are up to date, {settings["horizon_days"]} days ahead.',
            'sub': f'The next update is {_next_update(rows, now, tz, h24).lower()}. A schedule normally costs a few calls a day, plus one every 30 minutes around each event being recorded or played.',
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

    rules = Rules()
    rows = []
    for league in leagues:
        lfollows = follows.get(league.id, [])
        lfetches = fetches.get(league.id, [])
        events = upcoming.get(league.id, [])
        status = league_status(lfollows, lfetches, missing.get(league.id, 0))
        badge, label, edge, rank, tip = STATUSES[status]
        mine = [e for e in events if _is_followed(e, lfollows)]
        one, plural = _noun(league, events)
        updated = _last_updated(lfetches)
        first = mine[0] if mine else None
        nxt = None
        if first is not None:
            nxt = _game_row(first, rules.of(league.tsdb_id), now, tz, h24)
        rows.append({
            'id': league.id, 'name': league.name, 'status': status, 'badge': badge, 'label': label,
            'edge': edge, 'rank': rank, 'tip': tip, 'followed': bool(lfollows),
            'summary': _follow_text(league, lfollows, teams, plural) if lfollows else (league.sport or 'Not followed'),
            'next': nxt, 'next_sort': _sort_key(first),
            'no_game': tuple(text.format(many=plural) for text in NO_GAME_HEAD[status]),
            'count': len(mine) if lfollows else 0,
            'updated': ago(updated, now), 'updated_at': format_local(updated, 'day_datetime', none_value='', tz=tz, h24=h24),
            'updated_sort': int((now - updated).total_seconds()) if updated else 10 ** 9,
            'has_teams': _can_change_teams([f for f in lfollows if not f.rule_id], events),
            # The person's own follow, as on the league's page; a rule's is changed on the rule.
            'dialog': _dialog(league, [f for f in lfollows if not f.rule_id], teams, events, settings,
                              sorted(rule_names[f.rule_id] for f in lfollows if f.rule_id in rule_names),
                              one=one, many=plural),
        })
    rows.sort(key=lambda r: (r['next_sort'], r['name']))
    followed = sum(1 for r in rows if r['followed'])
    return {
        'times': default_times_card(leagues, rules, h24),
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


def _dialog(league, follows, teams, events, settings, rules=(), one='game', many='games') -> dict:
    """What the row's dialogs act on, read from the row itself so it is never older than the page.
    `rules` names the auto-record rules that name the league, so Delete league says it cannot before
    it is pressed (dev/changelog/1491). `one` and `many` are the league's noun, so a dialog never
    calls a race a game. `teams` lists the teams in `events`, which Change teams ticks; with none
    and `has_schedule`, the league has no teams to pick (a racing series). `chosen_names` names a
    followed team with no game here yet, which the dialog adds to by search (dev/changelog/1641)."""
    names = {}
    for e in events:
        if e.home_team_id:
            names[e.home_team_id] = e.home_name
        if e.away_team_id:
            names[e.away_team_id] = e.away_name
    chosen = sorted(f.team_tsdb_id for f in follows if f.team_tsdb_id)
    return {'id': league.id, 'tsdb_id': league.tsdb_id, 'name': league.name, 'stored': len(events),
            'keep_days': settings['history_keep_days'], 'one': one, 'many': many,
            'chosen': chosen, 'chosen_names': {i: teams.get(i) or names.get(i) or str(i) for i in chosen},
            'whole': any(f.team_tsdb_id == 0 for f in follows),
            'followed': bool(follows), 'rules': list(rules), 'has_schedule': bool(events),
            'teams': sorted(({'id': i, 'name': teams.get(i) or n} for i, n in names.items()),
                            key=lambda t: t['name'] or '')}


def _can_change_teams(person, events) -> bool:
    """Change teams shows for a league with games between teams, and for one a person follows by team
    before any of its games are here, whose teams the dialog finds by search (dev/changelog/1641)."""
    return any(event_shape_is_matchup(e) for e in events) or any(f.team_tsdb_id for f in person)


def follow_state(league) -> dict:
    """What a lookup says about a league already here, beside TheSportsDB's own answer: the same
    data as the league's row on the list page, so the Add dialog's picker starts from what the person
    follows now. A fixed number of queries."""
    from .database import AutoRule
    now = datetime.utcnow()
    tz = get_display_tz()
    settings = _settings()
    events = [e for e in _upcoming_query(now, settings, tz).filter(SportsEvent.league_id == league.id).all()
              if _still_ahead(e, now, tz)]
    follows = SportsFollow.query.filter_by(league_id=league.id).all()
    ids = {f.team_tsdb_id for f in follows if f.team_tsdb_id}
    ids |= {i for e in events for i in (e.home_team_id, e.away_team_id) if i}
    teams = ({t.tsdb_id: t.full_name for t in SportsTeam.query.filter(SportsTeam.tsdb_id.in_(ids))} if ids else {})
    rule_ids = {f.rule_id for f in follows if f.rule_id}
    rules = (sorted(n for (n,) in db.session.query(AutoRule.name).filter(AutoRule.id.in_(rule_ids))) if rule_ids else [])
    one, many = _noun(league, events)
    return _dialog(league, [f for f in follows if not f.rule_id], teams, events, settings, rules, one=one, many=many)


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


def _update_row(fetch, rounds, one, many, now, tz, h24, teams=None, games=None) -> dict:
    """`teams` ({tsdb id: name}) and `games` ({tsdb event id: event}) name the game a one-game call
    was about."""
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
    elif fetch.kind == SPORTS_KIND_EVENT:
        what = f'A {one} re-read by its number'
        event = (games or {}).get(int(fetch.target.split(':')[1]))
        sub = event_game_name(event) if event is not None else ''
    elif fetch.kind == SPORTS_KIND_SEARCH:
        what = f'A {one} from your guide, looked up by name'
        _season, home, away = search_parts(fetch.target)
        sub = f'{(teams or {}).get(away, away)} at {(teams or {}).get(home, home)}'
    elif fetch.kind == SPORTS_KIND_TEAM_NEXT:
        what = f"A team's next {one}"
        team = int(fetch.target.split(':')[2])
        sub = (teams or {}).get(team, str(team))
    else:
        raise ValueError(f'unknown sports fetch kind {fetch.kind!r}')
    if fetch.last_at is None:
        return {'when': 'Waiting', 'what': what, 'sub': sub, 'badge': None, 'note': ''}
    cls, label = RESULTS[fetch.outcome]
    badge = (cls, label.format(many=many))
    return {'when': _when_text(day_label(fetch.last_at, now, tz), format_local(fetch.last_at, 'clock', tz=tz, h24=h24)),
            'what': what, 'sub': sub, 'badge': badge, 'note': fetch.reason or ''}


def _lookups_by_game(fetches) -> dict:
    """The answered lookups by name, grouped into the games they were for. A listing that does not
    say which team is home is asked both ways round, and one that opens near midnight UTC on both
    days, so one game can be up to four calls: the same two teams on the same or the next day are
    one game."""
    asks = []
    for f in fetches:
        if f.kind == SPORTS_KIND_SEARCH and f.last_at is not None:
            when, home, away = search_parts(f.target)
            asks.append((frozenset((home, away)), when, f, home, away))
    games, last = {}, {}
    for pair, when, f, home, away in sorted(asks, key=lambda a: (sorted(a[0]), a[1])):
        try:
            day = date.fromisoformat(when)
        except ValueError:
            day = None   # a season, from a row written before lookups went by day
        prev = last.get(pair)
        if prev is not None and day is not None and prev[1] is not None and 0 <= (day - prev[1]).days <= 1:
            # Measured from the group's first day, so a chain of days never grows into one game. Two
            # meetings on back-to-back days count as one here: this is a count on a notice, and
            # telling them apart would need the listing each lookup came from.
            key = prev[0]
        else:
            key = (pair, when)
            last[pair] = (key, day)
        games.setdefault(key, []).append((f, home, away))
    return games


def _coming_lookups(fetches, now: datetime) -> list:
    """`fetches` less the lookups by name whose day has passed. A lookup row is kept for
    history_keep_days so its pair is never asked twice, and counting every stored one made a
    morning's bad lookups read as missing games for a month (dev/changelog/1581). The day in a
    lookup's target is the UTC day it was asked about, so a game still to come is never left out.
    A row that holds a season where the day goes (written before lookups went by day) names no
    day and is left out too."""
    today = now.date().isoformat()
    return [f for f in fetches
            if f.kind != SPORTS_KIND_SEARCH or (_is_day(search_parts(f.target)[0]) and search_parts(f.target)[0] >= today)]


def _cut_notice(fetches, rounds, one, many, teams, now: datetime) -> tuple | None:
    """What a round answer cut at ROUND_ROW_CAP means and what was done about it, or None. Its
    lookups by name are the ones for games still to come (_coming_lookups)."""
    fetches = _coming_lookups(fetches, now)
    cut = [f for f in fetches if f.kind == SPORTS_KIND_ROUND
           and f.outcome in (SPORTS_FETCH_CAPPED, SPORTS_FETCH_CAPPED_PAST)]
    if not cut:
        return None
    weeks = []
    for f in sorted(cut, key=lambda f: f.target):
        if f.outcome != SPORTS_FETCH_CAPPED:
            continue
        _, _, season, number = f.target.split(':')
        dates = rounds.get((season, int(number)))
        if dates:
            weeks.append(dates[0])
    text = f'TheSportsDB sends at most {ROUND_ROW_CAP} {many} for one call.'
    if any(f.outcome == SPORTS_FETCH_CAPPED for f in cut):
        which = f" for {' and '.join(weeks)}" if weeks else ''
        text += (f' Its answer{which} came back with exactly that many, so it may have left some out. A {one} '
                 f'left out of a cut answer is not marked missing.')
    if any(f.outcome == SPORTS_FETCH_CAPPED_PAST for f in cut):
        # The NBA files its whole season as one round, and the answer is its first 50 (dev/changelog/1550).
        text += (f' TheSportsDB files this whole stretch of the schedule as one round, and it sent only the '
                 f'first {ROUND_ROW_CAP}, all already played.')
    asked = [f for f in fetches if f.kind == SPORTS_KIND_TEAM_NEXT and f.last_at is not None]
    if asked:
        # eventsnext sends one game, so a team is asked again after each game it plays (dev/changelog/1553).
        named = sum(1 for f in asked if _found(f))
        text += (f' ChannelBin asked {counted(len(asked), "team")} for its next {one}, and {named} named one in '
                 f'this league. Each is asked again after that {one}, so the one after it is found before it starts.')
    reread = [f for f in fetches if f.kind == SPORTS_KIND_EVENT and f.last_at is not None]
    waiting = sum(1 for f in fetches if f.kind in ONE_GAME_KINDS and f.last_at is None)
    pairs = _lookups_by_game(fetches)
    if reread:
        text += f' ChannelBin re-read {counted(len(reread), one)} it already had by number.'
    if pairs:
        found = sum(1 for asks in pairs.values() if any(_found(f) for f, _h, _a in asks))
        text += f' It looked up {counted(len(pairs), one)} your guide lists by name and found {found}.'
        absent = [asks[0] for asks in pairs.values() if all(f.outcome == SPORTS_FETCH_EMPTY for f, _h, _a in asks)]
        if absent:
            names = [f'{teams.get(away, away)} at {teams.get(home, home)}' for _f, home, away in absent[:5]]
            more = f' and {len(absent) - 5} more' if len(absent) > 5 else ''
            text += f" Not found by name: {', '.join(names)}{more}."
    unusable = sum(1 for f in fetches if f.kind in ONE_GAME_KINDS and f.dropped_count)
    if unusable:
        text += f' {counted(unusable, "lookup")} came back with an answer ChannelBin could not use.'
    if waiting:
        text += f' {counted(waiting, "more lookup")} waiting.'
    return ('info', f'Some {many} may be missing from TheSportsDB\'s answer.', text)


def _found(fetch) -> bool:
    """A lookup's answer held a game that was stored: answered, with more rows than it dropped."""
    return fetch.outcome == SPORTS_FETCH_OK and (fetch.row_count or 0) > (fetch.dropped_count or 0)


def _notices(league, status, fetches, missing_events, one, many, stored, keep_days, now, tz,
             rounds=None, teams=None, line=None) -> list:
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
        out.append(('warn', 'TheSportsDB asked ChannelBin to slow down.', f'That is not read as "no {many}", and nothing stored has changed.'))
    for f in fetches:
        if f.outcome == SPORTS_FETCH_SHRUNK:
            out.append(('warn', 'An update came back smaller.', f.reason or ''))
    cut = _cut_notice(fetches, rounds or {}, one, many, teams or {}, now)
    if cut is not None:
        out.append(cut)
    for e in missing_events[:5]:
        since = date_label(to_local(e.missing_since, tz).date(), now, tz)
        out.append(('warn', f'A {one} is missing.', f"{e.away_name} at {e.home_name}" if event_shape_is_matchup(e) else e.name))
        out[-1] = (out[-1][0], out[-1][1], f"{out[-1][2]} was not in TheSportsDB's latest answer on {since}. It is kept for {counted(MISSING_KEEP_DAYS, 'day')} in case it moved.")
    # Only the calls Schedule updates lists, which is where this sends the reader. A lookup's unusable
    # answer is named in the cut notice (dev/docs/BUGS.md 2026-11-14 @ 11:28:49 am).
    dropped = sum(f.dropped_count or 0 for f in fetches if f.kind not in (SPORTS_KIND_NEXT, SPORTS_KIND_TEAM, *ONE_GAME_KINDS))
    if dropped:
        out.append(('warn', f'{counted(dropped, "row")} from TheSportsDB could not be used.', 'Schedule updates below says why.'))
    if status == STATUS_FILLING:
        out.append(('info', *_filling_text(league.id, fetches, line or [], now)))
    return out


def _filling_text(league_id: int, fetches: list, line: list, now: datetime) -> tuple:
    """(title, text) for a league still being read for the first time: how many of its calls are
    left, how many calls are ahead of them and about when they run. `line` is queue_line(), the
    order the worker takes, so a count that sits still explains itself (dev/changelog/1575)."""
    waiting = [f for f in fetches if f.last_at is None and f.kind not in ONE_GAME_KINDS]
    names = sum(1 for f in waiting if f.kind == SPORTS_KIND_TEAM)
    schedule = len(waiting) - names
    mine = [i for i, (lid, kind, never) in enumerate(line)
            if lid == league_id and never and kind not in ONE_GAME_KINDS]
    if schedule:
        first = next((i for i in mine if line[i][1] != SPORTS_KIND_TEAM), None)
        text = f'At least {counted(schedule, "more call")} for its schedule, spread out so they are easy on TheSportsDB.'
        if first == 0:
            text += f' {"It is" if schedule == 1 else "They are"} next in line.'
        elif first:
            text += (f' {counted(first, "other call")} {plural(first, "is", "are")} ahead of '
                     f'{"it" if schedule == 1 else "them"} in line, so the next one runs in '
                     f'{fmt_duration_phrase(seconds_until_call(first, now))}.')
        if names:
            text += (f' After that, {counted(names, "team lookup")} {plural(names, "adds", "add")} each '
                     "team's other names, colors and badge.")
        return "Getting this league's schedule.", text
    text = (f"Its schedule is in. {counted(names, 'team lookup')} still to run, which {plural(names, 'adds', 'add')} "
            "each team's other names, colors and badge, and until then a team is matched by its full name only. "
            'They wait for every schedule call.')
    if mine:
        text += f' The last runs in {fmt_duration_phrase(seconds_until_call(mine[-1], now))}.'
    return "Getting this league's team names.", text


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


#: How many rows a page of the league page's Upcoming games or On your IPTV output holds (mockup 57).
#: A page of games runs on to the end of the day it reaches this on, so no day is split.
PAGE_SIZE = 25
#: The longest team filter the IPTV output card takes, as the route reads it.
FEED_FILTER_MAX = 120


def day_pages(items: list, day_of, size: int = PAGE_SIZE) -> list:
    """`items`, in their own order, as pages that end at the end of a day once they hold `size`.
    A busy Saturday is one long page rather than two halves you page between, and today's games are
    all on page 1. `day_of` reads an item's day label; consecutive items with the same label are one
    day. Never empty: no items is one empty page."""
    pages, cur = [], []
    for i, item in enumerate(items):
        cur.append(item)
        last = i + 1 == len(items)
        if last or (len(cur) >= size and day_of(items[i + 1]) != day_of(item)):
            pages.append(cur)
            cur = []
    return pages or [[]]


def _pager(page: int, pages: int, first: str, last: str, link) -> dict:
    """The pager under a paged card, None when everything fits on one page. `first` and `last` name
    what the page runs from and to; `link(n)` is page n's address."""
    if pages <= 1:
        return None
    span = first if first == last else f'{first} to {last}'
    return {'text': f'Page {page} of {pages}: {span}',
            'prev': link(page - 1) if page > 1 else '', 'next': link(page + 1) if page < pages else ''}


def _clamp(page: int, pages: int) -> int:
    return min(max(1, page or 1), pages)


def _feed_page(feed: dict, league, q: str, page: int, link) -> dict:
    """One page of the IPTV output card's team channels, filtered first by `q` (a piece of the
    team's name, any case) and then paged. The league's own row is not a team: it is on every page,
    unless a filter is in and the league's name does not have it."""
    teams = feed['teams']
    needle = q.casefold()
    shown = [t for t in teams if needle in t['name'].casefold()] if needle else teams
    pages = [shown[i:i + PAGE_SIZE] for i in range(0, len(shown), PAGE_SIZE)] or [[]]
    page = _clamp(page, len(pages))
    here = pages[page - 1]
    return {'q': q, 'page': page, 'filterable': len(teams) > PAGE_SIZE or bool(needle),
            'show_league': feed['whole'] and (not needle or needle in league.name.casefold()),
            'show_up_next': feed['followed'] and (not needle or needle in feed['up_next_name'].casefold()),
            'page_teams': here,
            'pager': _pager(page, len(pages), here[0]['name'], here[-1]['name'], lambda n: link(feed_page=n)) if here else None}


def page_context(league_id: int, scope: str = SCOPE_FOLLOWED, page: int = 1, feed_page: int = 1, feed_q: str = ''):
    """The league page. `page` and `feed_page` pick the page of Upcoming games and of On your IPTV
    output, `feed_q` filters the latter by team; every link the page draws keeps the other card's."""
    from flask import url_for
    from . import sports_feed
    league = db.session.get(SportsLeague, league_id)
    if league is None:
        return None
    from .config import load_config
    cfg = load_config()
    now = datetime.utcnow()
    tz, h24 = get_display_tz(), is_24h()
    settings = _settings()
    fetches = SportsFetch.query.filter_by(league_id=league_id).all()
    follows = SportsFollow.query.filter_by(league_id=league_id).all()
    teams = {t.tsdb_id: t.full_name for t in SportsTeam.query.all()}
    since = _league_page_since(now, tz)
    all_events = [e for e in _upcoming_query(now, settings, tz, since=since)
                  .filter(SportsEvent.league_id == league_id).all()
                  if _on_league_page(e, since, now, tz)]
    missing_events = SportsEvent.query.filter(SportsEvent.league_id == league_id,
                                              SportsEvent.missing_since.isnot(None)).order_by(SportsEvent.start_at).all()
    stored = SportsEvent.query.filter_by(league_id=league_id).count()
    one, many = _noun(league, all_events)
    status = league_status(follows, fetches, len(missing_events))
    badge, label, _edge, _rank, tip = STATUSES[status]
    whole = any(f.team_tsdb_id == 0 for f in follows)
    picked_teams = bool(follows) and not whole
    mine = [e for e in all_events if _is_followed(e, follows)] if follows else all_events
    scope = SCOPE_ALL if scope == SCOPE_ALL and picked_teams else SCOPE_FOLLOWED
    shown = all_events if (scope == SCOPE_ALL or not picked_teams) else mine
    feed_q = (feed_q or '').strip()[:FEED_FILTER_MAX]
    rules = Rules(cfg)
    rule = rules.of(league.tsdb_id)
    paged = day_pages([(_game_row(e, rule, now, tz, h24), e) for e in shown], lambda pair: pair[0]['day'])
    page = _clamp(page, len(paged))
    here = paged[page - 1]
    where = {'scope': scope, 'page': page, 'feed_page': feed_page or 1, 'feed_q': feed_q}

    def link(**over):
        s = {**where, **over}
        args = {'scope': SCOPE_ALL} if s['scope'] == SCOPE_ALL else {}
        args.update({k: s[k] for k in ('page', 'feed_page') if s[k] > 1})
        if s['feed_q']:
            args['feed_q'] = s['feed_q']
        return url_for('sports.league_page', league_id=league.id, **args)

    updated = _last_updated(fetches)
    follow_text = _follow_text(league, follows, teams, many)
    # A rule's follow is changed on the rule, so the buttons and the Following row are about the
    # person's own; the rules that follow it have a row of their own (dev/changelog/1490).
    person = [f for f in follows if not f.rule_id]
    rule_follows = _rule_follows(follows, teams, many)
    if picked_teams:
        lead = f'Following {follow_text.replace(", ", " and ")}.'
    elif whole:
        lead = f'Following all {many}.'
    else:
        lead = 'Not followed.'
    bits = [b for b in (league.sport, f'{league.current_season} season' if league.current_season else None) if b]
    rounds = _round_dates(league_id, now, tz)
    update_rows = sorted(
        (f for f in fetches if f.kind not in (SPORTS_KIND_TEAM, *ONE_GAME_KINDS)
         or f.outcome in (SPORTS_FETCH_REFUSED, SPORTS_FETCH_RATE_LIMITED)),
        key=lambda f: (f.last_at is None, -(f.last_at.timestamp() if f.last_at else 0)))[:UPDATES_SHOWN]
    reread_ids = [int(f.target.split(':')[1]) for f in update_rows if f.kind == SPORTS_KIND_EVENT]
    reread = ({e.tsdb_id: e for e in SportsEvent.query.filter(SportsEvent.tsdb_id.in_(reread_ids)).all()}
              if reread_ids else {})
    next_update = _next_update(fetches, now, tz, h24) if follows else 'None'
    state = db.session.get(SportsState, 1)
    checked_at = state.last_match_at if state is not None else None
    counts = sports_match.game_counts([e.id for e in all_events])
    names = sports_match.name_counts([e.id for _row, e in here])
    if follows:
        message = (f'Updated {ago(updated, now)}. Next update: {next_update.lower()}.' if updated
                   else f'Not updated yet. Next update: {next_update.lower()}.')
    else:
        message = f'Last updated {ago(updated, now)}.' if updated else f'Not checking for {many}.'
    feed = sports_feed.card_context(league, follows, teams, all_events, cfg)
    feed.update(_feed_page(feed, league, feed_q, feed_page, link))
    where['feed_page'] = feed['page']
    return {
        'league': league, 'status': status, 'badge': badge, 'label': label, 'tip': tip, 'lead': f'{lead} {", ".join(bits)}.' if bits else lead,
        'added': format_local(league.created_at, 'date', tz=tz, h24=h24), 'message': message, 'followed': bool(person),
        'notices': _notices(league, status, fetches, missing_events, one, many, stored, settings['history_keep_days'], now, tz,
                            rounds=rounds, teams=teams,
                            line=queue_line(now) if status == STATUS_FILLING else None),
        'no_games_note': _no_games_note(status, settings['horizon_days']),
        'games': _enrich_games([row for row, _e in here], [e for _row, e in here], counts, names, checked_at,
                               cfg, now, tz, h24, league=league, one=one),
        'games_total': len(shown),
        'games_pager': _pager(page, len(paged), here[0][0]['day'], here[-1][0]['day'],
                              lambda n: link(page=n)) if here else None,
        'scope_urls': {SCOPE_FOLLOWED: link(scope=SCOPE_FOLLOWED, page=1), SCOPE_ALL: link(scope=SCOPE_ALL, page=1)},
        'one': one, 'many': many, 'a_one': sports_words.a(one), 'teamed': sports_words.teamed(league.sport),
        'a_teamed': sports_words.a(sports_words.teamed(league.sport)),
        'trust': _trust_text(league), 'upcoming_note': _upcoming_note(league, many, settings['horizon_days']),
        'times': times_card(league, one, many, rules, settings['history_keep_days'],
                            min((f.created_at for f in follows if f.created_at), default=None), now, h24),
        'match_note': _match_note(state, sum(1 for e in all_events if (counts.get(e.id) or {}).get('channels')),
                                  len(all_events), one, many, now, tz, h24),
        'match_waiting': bool(state and (state.match_note or '').startswith(('Waiting', 'The last run'))),
        'matching': _matching_context(league, now, cfg, one),
        'picked_teams': picked_teams, 'scope': scope,
        'mine_total': len(mine), 'all_total': len(all_events),
        'follow_text': _follow_text(league, person, teams, many) if person else 'Nothing',
        'next_update': next_update, 'horizon_days': settings['horizon_days'],
        'updates': [_update_row(f, rounds, one, many, now, tz, h24, teams, reread) for f in update_rows],
        # Change teams and Stop following act on the person's own follow only (sports_data.
        # set_following); a rule's follow is changed on the rule (dev/changelog/1490).
        'dialog': _dialog(league, person, teams, all_events, settings,
                          sorted({r['name'] for r in rule_follows}), one=one, many=many),
        'rule_follows': rule_follows,
        'feed': feed,
        'can_change_teams': _can_change_teams(person, all_events),
        'section_names': {**SECTION_NAMES, 'upcoming': f'Upcoming {many}', 'times': f'{one.capitalize()} time settings'},
        'league_url': f'https://www.thesportsdb.com/league/{league.tsdb_id}',
    }


def _rule_follows(follows, teams, many='games') -> list:
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
        what = teams.get(f.team_tsdb_id, f'Team #{f.team_tsdb_id}') if f.team_tsdb_id else f'All its {many}'
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
    text = f'Matched your guide on {agreed} of the last {counted(sample, "event")}'
    if sports_match.league_trusted(league):
        return text, ''
    if sample < sports_match.TRUST_MIN_SAMPLE:
        return text, 'Too few checked yet to rely on'
    return text, 'Not enough to rely on'


def _record_notice(league, one, many, keep_days, followed_since, now) -> dict:
    """{tone, text, asks}: the measured record on a league's Game time settings card, as advice on
    which answer to pick (app/sports_times.py, sports_match.league_trusted()). `asks` is the case of
    a league followed (`followed_since`, its current follow) longer than the history is kept with fewer than 10
    measured: it plays too rarely ever to reach 10, so the card asks the person to choose, and the
    ask goes once they have."""
    sample, agreed = league.trust_sample or 0, league.trust_agreed or 0
    need = sports_match.TRUST_MIN_SAMPLE
    if not sample:
        return {'tone': 'info', 'asks': False,
                'text': (f'Not measured yet. ChannelBin checks each {one} against your guide after the next guide '
                         "update, and says here how often TheSportsDB's times agree with it.")}
    if sports_match.league_trusted(league):
        return {'tone': 'ok', 'asks': False,
                'text': (f"TheSportsDB's times matched your guide, within 5 minutes, on {agreed} of the last {sample} "
                         f'{many}, so going by TheSportsDB is a safe choice here.')}
    if sample < need:
        old = followed_since is not None and followed_since < now - timedelta(days=keep_days)
        if old:
            verb = 'race' if one == 'race' else 'play'
            return {'tone': 'warn', 'asks': True,
                    'text': (f'{counted(sample, one, many)} checked. ChannelBin needs {need} before it relies on '
                             f'these times, and this {"series" if one == "race" else "league"} does not {verb} often '
                             'enough to get there, so choose below which time it goes by.')}
        return {'tone': 'info', 'asks': False,
                'text': (f'{counted(sample, one, many)} checked so far, and {agreed} matched your guide. ChannelBin '
                         f"needs {need} before it can say how far TheSportsDB's times can be relied on here.")}
    return {'tone': 'warn', 'asks': False,
            'text': (f"TheSportsDB's times matched your guide on only {agreed} of the last {sample} {many}. Cover "
                     'both is the safer choice here: it records from the earlier start to the later end.')}


def wait_example(hours: int, h24: bool) -> tuple:
    """('8:15 PM on Sunday', '8:15 PM on Saturday'): a game, and when it starts going by
    TheSportsDB's time, for the wait row's worked example. A plain clock and weekday in the display
    clock format: an example, not a real time, so no timezone applies."""
    game = datetime(2026, 11, 22, 20, 15)  # a Sunday

    def clock(at):
        return at.strftime('%H:%M') if h24 else f'{at.hour % 12 or 12}:{at.minute:02d} {"AM" if at.hour < 12 else "PM"}'
    at = game - timedelta(hours=hours)
    day = 'the Sunday' if at.date() == game.date() else at.strftime('%A')
    return f'{clock(game)} on Sunday', f'{clock(at)} on {day}'


def times_card(league, one, many, rules, keep_days, followed_since, now, h24) -> dict:
    """What a league page's Game time settings card draws (app/sports_times.py, mockup 58): the
    league's own answers, the default beside them, the answer in force, and the measured record."""
    from .sports_times import GO, GO_BY_LABELS, MAX_COVER_BOTH_MINUTES, MAX_WAIT_HOURS
    rule = rules.of(league.tsdb_id)
    record = _record_notice(league, one, many, keep_days, followed_since, now)
    sunday, example = wait_example(rule.wait_hours, h24)
    return {'heading': f'{one.capitalize()} time settings', 'own': rule.own, 'rule': rule,
            'default': rules.default, 'labels': GO_BY_LABELS, 'record': record,
            'show_record': not (record['asks'] and not rule.uses_default), 'sunday': sunday, 'example': example,
            'max_cover_both': MAX_COVER_BOTH_MINUTES, 'max_wait': MAX_WAIT_HOURS, 'go': GO}


def default_times_card(leagues, rules, h24) -> dict:
    """Sports Schedules' Game time settings card: the default every league goes by, and the leagues
    with their own choice named beside it."""
    from .sports_times import GO, GO_BY_LABELS, MAX_COVER_BOTH_MINUTES, MAX_WAIT_HOURS
    by_tsdb = {str(lg.tsdb_id): lg.name for lg in leagues}
    named = []
    for tsdb_id, own in sorted(rules.overrides().items(), key=lambda kv: by_tsdb.get(kv[0], kv[0]).lower()):
        if tsdb_id not in by_tsdb:
            continue
        bits = ([GO_BY_LABELS[own['go_by']]] if 'go_by' in own else []) + (
            [f"up to {counted(own['cover_both_minutes'], 'minute')}"] if 'cover_both_minutes' in own else []) + (
            [f"from {counted(own['wait_hours'], 'hour')} before"] if 'wait_hours' in own else [])
        named.append(f'{by_tsdb[tsdb_id]} ({", ".join(bits)})')
    sunday, example = wait_example(rules.default.wait_hours, h24)
    return {'rule': rules.default, 'own': {}, 'labels': GO_BY_LABELS,
            'overrides': (f'Leagues with their own choice: {"; ".join(named)}.' if named
                          else 'No league has its own choice.'),
            'sunday': sunday, 'example': example,
            'max_cover_both': MAX_COVER_BOTH_MINUTES, 'max_wait': MAX_WAIT_HOURS, 'go': GO}


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


def _live_now(events, counts, cfg, now, tz, h24) -> dict:
    """{event id: Start broadcast's offer} for each shown game the guide has found or only a channel's
    name gives (dev/changelog/1643), and none for the rest: before that there is no channel to play
    (dev/changelog/1530). None for a game whose own time on air has come (dev/changelog/1644). The playable listings, the games only a name gives, the served sports
    channels and the blocked accounts are read once for every game together."""
    from . import output_feed, sports_feed
    from .channel_groups import blocked_account_ids_now
    if not events:
        return {}
    named = sports_feed.name_sources({e.league_id for e in events}, now)
    found = [e for e in events if (counts.get(e.id) or {}).get('channels') or e.id in named]
    if not found:
        return {}
    fcfg = output_feed.feed_config(cfg)
    before, after = output_feed.sports_window(fcfg)
    blocked, allowed = blocked_account_ids_now(), output_feed.allowed_account_ids(fcfg)
    listings = sports_match.listing_windows([e.id for e in found], now)
    carrying = sports_feed.carriers(sports_feed.served_channels(), found)
    return {e.id: sports_feed.live_now_offer(e, listings.get(e.id, ()), [c.name for c in carrying.get(e.id, ())],
                                             now, before, after, tz, h24, blocked=blocked, allowed=allowed,
                                             named=named.get(e.id))
            for e in found}


def _enrich_games(rows, events, counts, names, checked_at, cfg, now, tz, h24, league=None, one='game') -> list:
    """The league page's extra columns, from counts read once for every shown event. Both times show
    only for a game with live listings and a real time from TheSportsDB: a race's 00:00 placeholder is
    not a time to disagree with (7). Each row's Record button or recording comes from one batch too
    (_record_rows)."""
    guide_end = db.session.query(func.max(EPGEntry.start_time)).scalar()
    live = _live_now(events, counts, cfg, now, tz, h24)
    acts = _record_rows(events, counts, names, league, guide_end, one, now, tz, h24) if league is not None else {}
    for row, event in zip(rows, events):
        c = counts.get(event.id)
        row['id'] = event.id
        row['tsdb_url'] = f'https://www.thesportsdb.com/event/{event.tsdb_id}'
        row['guide'] = _guide_cell(event, c, names.get(event.id), checked_at, guide_end, now, tz)
        row['live'] = live.get(event.id)
        row['guide_time'] = (format_local(event.guide_start, 'clock', tz=tz, h24=h24)
                             if row['guide']['state'] == 'found' and row['time'] and sports_match.time_disagrees(event)
                             else '')
        row.update(acts.get(event.id) or {})
    return rows


# ---------------------------------------------------------------------------
# Record, Search programs and a game's recording (design 11, dev/changelog/1539)
# ---------------------------------------------------------------------------

#: What a game's recording shows as, by its status. Every status is named. A failed or aborted one
#: frees Record, and still shows, so the row says what happened (principle 1).
HELD_SCHEDULED = 'scheduled'
HELD_RECORDING = 'recording'
HELD_FINISHING = 'finishing'
HELD_RECORDED = 'recorded'
HELD_FAILED = 'failed'
HELD_ABORTED = 'aborted'


def _held_state(status):
    from .database import (
        REC_STATUS_ABORTED, REC_STATUS_ANALYZING, REC_STATUS_COMPLETED, REC_STATUS_CONCATENATING,
        REC_STATUS_CONVERTING, REC_STATUS_FAILED, REC_STATUS_SCHEDULED, WINDOW_OPEN_STATUSES,
    )
    if status == REC_STATUS_SCHEDULED:
        return HELD_SCHEDULED
    if status in WINDOW_OPEN_STATUSES:
        return HELD_RECORDING
    if status in (REC_STATUS_CONCATENATING, REC_STATUS_ANALYZING, REC_STATUS_CONVERTING):
        return HELD_FINISHING
    if status == REC_STATUS_COMPLETED:
        return HELD_RECORDED
    if status == REC_STATUS_FAILED:
        return HELD_FAILED
    if status == REC_STATUS_ABORTED:
        return HELD_ABORTED
    raise ValueError(f'a recording status the league page does not name: {status!r}')


#: Which of a game's recordings the row shows when it has several: the one recording now, then
#: the next one to start, then one finishing, then the latest done.
_HELD_RANK = {HELD_RECORDING: 0, HELD_SCHEDULED: 1, HELD_FINISHING: 2, HELD_RECORDED: 3}


def _held_by_event(events, now, tz, h24) -> dict:
    """{event id: {'main', 'replay', 'failed'}}, each a held recording or None, from the games'
    recording_sports links, in a fixed four queries. A replay's recording is kept apart: the live
    airing can still be recorded beside it. So is the latest one that failed or was aborted, which
    leaves Record offered and says what became of it."""
    from flask import url_for
    from .database import SPORTS_AIRING_REPLAY, AutoRule, Channel, ChannelGroup, Recording, RecordingSports, rule_status
    by_tsdb = {e.tsdb_id: e.id for e in events}
    if not by_tsdb:
        return {}
    rows = (db.session.query(RecordingSports.tsdb_event_id, RecordingSports.airing, Recording)
            .join(Recording, Recording.id == RecordingSports.recording_id)
            .filter(RecordingSports.tsdb_event_id.in_(sorted(by_tsdb)), RecordingSports.removed_at.is_(None)).all())
    if not rows:
        return {}
    chans = sorted({r.channel_id for _t, _a, r in rows if r.channel_id})
    groups = sorted({r.group_id for _t, _a, r in rows if r.group_id})
    rules = sorted({r.rule_id for _t, _a, r in rows if r.rule_id})
    cnames = dict(db.session.query(Channel.id, Channel.name).filter(Channel.id.in_(chans))) if chans else {}
    gnames = dict(db.session.query(ChannelGroup.id, ChannelGroup.name).filter(ChannelGroup.id.in_(groups))) if groups else {}
    rnames = dict(db.session.query(AutoRule.id, AutoRule.name).filter(AutoRule.id.in_(rules))) if rules else {}
    from .routes.recordings import schedule_edit_payload
    out = {}
    for tsdb, airing, rec in rows:
        state = _held_state(rule_status(rec.status, rec.gave_up_reason))
        held = {'id': rec.id, 'name': rec.name, 'state': state, 'start': rec.start_time,
                'where': gnames.get(rec.group_id) or cnames.get(rec.channel_id) or 'a channel no longer in your channels',
                'rule': rnames.get(rec.rule_id) or '', 'url': url_for('recordings.recording_detail', recording_id=rec.id),
                'rule_url': url_for('auto_rules.rule_page', rule_id=rec.rule_id) if rec.rule_id in rnames else '',
                'edit': schedule_edit_payload(rec) if state == HELD_SCHEDULED else None}
        held['text'], held['sub'] = _held_words(held, airing == SPORTS_AIRING_REPLAY, now, tz, h24)
        # The pill is the record red while it will record or is recording (mockup 54), the done
        # green once it has. Manage recording offers what the Recordings list does in each state.
        held['badge'] = ('b-done' if state in (HELD_FINISHING, HELD_RECORDED)
                         else 'b-fail' if state in (HELD_FAILED, HELD_ABORTED) else 'b-live')
        held['pulse'] = state == HELD_RECORDING
        held['mark'] = state == HELD_SCHEDULED
        held['can_edit'] = held['can_cancel'] = state == HELD_SCHEDULED
        held['can_stop'] = state == HELD_RECORDING
        held['link'] = {HELD_FAILED: 'Failed recording', HELD_ABORTED: 'Aborted recording'}.get(state, '')
        slot = out.setdefault(by_tsdb[tsdb], {'main': None, 'replay': None, 'failed': None})
        if state in (HELD_FAILED, HELD_ABORTED):
            if slot['failed'] is None or held['start'] > slot['failed']['start']:
                slot['failed'] = held
            continue
        key = 'replay' if airing == SPORTS_AIRING_REPLAY else 'main'
        if slot[key] is None or (_HELD_RANK[state], held['start']) < (_HELD_RANK[slot[key]['state']], slot[key]['start']):
            slot[key] = held
    return out


def _held_words(held, replay, now, tz, h24) -> tuple:
    """(pill text, the line under it) for one game's recording."""
    by = f', by the {held["rule"]} rule' if held['rule'] else ''
    when = f'{day_label(held["start"], now, tz)} {format_local(held["start"], "clock", tz=tz, h24=h24)}'
    state = held['state']
    if state == HELD_SCHEDULED:
        if replay:
            return 'Replay scheduled', f'{when} on {held["where"]}{by}'
        return 'Recording scheduled', f'On {held["where"]}{by}'
    if state == HELD_RECORDING:
        return 'Recording now', f'On {held["where"]} since {format_local(held["start"], "clock", tz=tz, h24=h24)}{by}'
    if state == HELD_FINISHING:
        return 'Recorded', f'On {held["where"]}{by}. Its file is being finished.'
    if state == HELD_RECORDED:
        return 'Recorded', f'On {held["where"]}{by}'
    if state == HELD_FAILED:
        return 'Recording failed', f'{when} on {held["where"]}{by}. Open recording says what happened.'
    if state == HELD_ABORTED:
        return 'Recording aborted', f'{when} on {held["where"]}{by}.'
    raise ValueError(f'a held state with no words: {state!r}')


def _listings_ahead(event_ids, now) -> dict:
    """{event id: [(title key, episode key, start, stop, channel id, airing, strong)]} for the guide
    listings of these events not yet over, on channels the user is offered, in one query."""
    from .database import SPORTS_MATCH_KIND_GUIDE, Channel, SportsMatch
    if not event_ids:
        return {}
    rows = (db.session.query(SportsMatch.event_id, SportsMatch.title_key, SportsMatch.episode_key,
                             SportsMatch.program_start_time, SportsMatch.program_stop_time, SportsMatch.channel_id,
                             SportsMatch.airing, SportsMatch.strength)
            .join(Channel, Channel.id == SportsMatch.channel_id)
            .filter(SportsMatch.event_id.in_(event_ids), SportsMatch.kind == SPORTS_MATCH_KIND_GUIDE,
                    SportsMatch.rejected_at.is_(None), SportsMatch.gone_since.is_(None),
                    SportsMatch.program_start_time.isnot(None), SportsMatch.program_stop_time > now,
                    sports_match._offered()).all())
    out = {}
    for eid, tkey, ep, start, stop, cid, airing, strength in rows:
        out.setdefault(eid, []).append((tkey or '', ep or '', start, stop, cid, airing, strength == SPORTS_MATCH_STRONG))
    return out


def _preview_pick(event, listings):
    """(kind, starts at) of what Record would pick for a game from its strong listings ahead, in
    auto_rules_sports.keepers()'s own order: the button's label. The dialog's pick is
    auto_rules.game_pick(), which runs the same order over the Programs search."""
    from types import SimpleNamespace
    from .auto_rules import PICK_LIVE, PICK_REPLAY
    from .auto_rules_sports import keepers
    progs = {}
    for tkey, ep, start, _stop, cid, airing, strong in listings:
        if strong:
            p = progs.setdefault((tkey, ep, start), {'channels': set(), 'airings': []})
            p['channels'].add(cid)
            p['airings'].append(airing)
    stub = SimpleNamespace(id=event.id, guide_start=event.guide_start)
    programs = [SimpleNamespace(identity=k, showings=sorted(v['channels']), lead=SimpleNamespace(start=k[2]))
                for k, v in progs.items()]
    games_of = {k: (stub, sports_match.combined_airing(v['airings'])) for k, v in progs.items()}
    for kind, replays in ((PICK_LIVE, False), (PICK_REPLAY, True)):
        keep = keepers(programs, games_of, replays=replays)
        if keep:
            return kind, next(iter(keep))[2]
    return None, None


def pick_why(why, one, guide_end, now, tz, said='') -> str:
    """Why Record has nothing to record, every auto_rules.WHY_* named. `said` is event_pick()'s
    sentence for WHY_NO_START, which names the one start that is missing."""
    from . import auto_rules as ar
    reach = f' Your guide reaches {day_label(guide_end, now, tz)}.' if guide_end else ''
    if why == ar.WHY_ENDED:
        return f'Every listing of this {one} in your guide has ended, so there is nothing left to record.'
    if why == ar.WHY_UNSURE:
        return (f'ChannelBin is not sure enough which listing in your guide is this {one} to pick one for you. '
                'Listings on your guide shows what it found, and Search programs shows every listing.')
    if why == ar.WHY_NO_START:
        said = said or ('An event channel names it, but there is no start time ChannelBin can stand behind.')
        return (f'This {one} has no listing in your guide. {said} Event channels naming it shows the channel, '
                'if you want to schedule it by hand.')
    if why == ar.WHY_UNLISTED:
        return f'This {one} is not in your guide yet, so there is nothing to record.{reach} ChannelBin looks again after each guide update.'
    if why == ar.WHY_UNKNOWN:
        return f'This {one} is outside the days ChannelBin reads to record from.'
    raise ValueError(f'a Record reason with no words: {why!r}')


def search_url(event) -> str:
    """The Programs search narrowed to one event: Search programs, and the record dialog's way to
    another listing."""
    from .channel_search import DimensionFilter
    from .routes.channels import airing_search_url
    return airing_search_url(filters=(DimensionFilter('event', (str(event.tsdb_id),)),))


def _record_rows(events, counts, names, league, guide_end, one, now, tz, h24) -> dict:
    """{event id: {record, held, replay_held, search}} for each shown game: Record's label or why it
    is greyed, the game's recording for the pill and Manage recording, and Search programs' link or
    why not. A fixed number of queries for any number of games; an event channel's pick needs the
    league's GameIndex, read only when some game has nothing but an event channel."""
    from . import auto_rules as ar
    held = _held_by_event(events, now, tz, h24)
    ahead = _listings_ahead([e.id for e in events], now)
    named_only = [e for e in events if not (counts.get(e.id) or {}).get('channels')
                  and not (counts.get(e.id) or {}).get('wrong') and (names.get(e.id) or {}).get('channels')]
    games = None
    if named_only:
        from .auto_rules_sports import GameIndex
        games = GameIndex.load([league.id], now, lookback=ar.PICK_LOOKBACK)
    out = {}
    for e in events:
        c = counts.get(e.id) or {}
        h = held.get(e.id) or {}
        row = {'held': h.get('main'), 'replay_held': h.get('replay'), 'failed': h.get('failed'),
               'search': ({'url': search_url(e)} if c.get('channels') else
                          {'why': (f"Programs searches guide listings, and this {one} has none yet: only an event "
                                   f"channel's name mentions it. Event channels naming it shows that channel."
                                   if (names.get(e.id) or {}).get('channels') else
                                   f'This {one} has no listing in your guide, so a search would find nothing.'
                                   + (f' Your guide reaches {day_label(guide_end, now, tz)}.' if guide_end else ''))})}
        if row['held'] is None:
            button = _record_button(e, c, ahead.get(e.id, ()), games, guide_end, one, now, tz)
            if button.pop('replay', False) and row['replay_held'] is not None:
                # Record would pick the replay the game already has a recording of: that recording
                # is the game's now, and Manage recording takes Record's place.
                row['held'], row['replay_held'] = row['replay_held'], None
            else:
                row['record'] = button
        out[e.id] = row
    return out


def _day_words(dt, now, tz) -> str:
    """'today', 'tomorrow' or 'on Mon Nov 16', to follow a clock in a sentence."""
    day = day_label(dt, now, tz)
    return day.lower() if day in ('Today', 'Tomorrow', 'Yesterday') else f'on {day}'


def _goes_by_words(league, rule) -> str:
    """'NBA goes by TheSportsDB, the default for every league,': the clause a notice leads its
    times with (app/sports_times.py)."""
    from .sports_times import GO_BOTH, GO_GUIDE, GO_TSDB
    verb = {GO_TSDB: "goes by TheSportsDB", GO_GUIDE: 'goes by your guide', GO_BOTH: 'covers both times'}[rule.go_by]
    return f'{league.name} {verb}, the default for every league,' if rule.uses_default else f'{league.name} {verb},'


def listing_times(event, league, rule, start, stop, one, now, tz, h24) -> tuple:
    """((start, stop), sentence): the times a league page's Record gives the listing it picked, by
    the league's answer to which time a game goes by, and the sentence saying so. Under Cover both
    with the two starts too far apart to cover in one, the times are the listing's and the sentence
    says ChannelBin schedules a second recording at TheSportsDB's time (dev/changelog/1630). Saved at
    these times, the recording follows the game's time from then on (app/sports_follow.py,
    dev/changelog/1631), and every sentence that acts on TheSportsDB says so."""
    from .sports_times import GO_BOTH, GO_GUIDE, GO_TSDB, AGREE, OFF_LIMIT, acts, listed_start, tsdb_start, windows
    clock = (lambda dt: format_local(dt, 'clock', tz=tz, h24=h24))
    as_listed = "The times are the listing's."
    tsdb, listed = tsdb_start(event), listed_start(event)
    if tsdb is None or listed is None or abs(tsdb - start) <= AGREE or abs(start - listed) > OFF_LIMIT:
        return (start, stop), as_listed
    wins = windows(start, stop, listed, tsdb, rule, now)
    both = f'TheSportsDB has the {one} at {clock(tsdb)} and the listing starts at {clock(start)}.'
    if rule.go_by == GO_GUIDE:
        return wins[0], f"TheSportsDB has the {one} at {clock(tsdb)}. {_goes_by_words(league, rule)} so the recording keeps the listing's times."
    if not acts(listed, tsdb, rule, now):
        away = min(tsdb, listed) - now
        far = (counted(round(away / timedelta(days=1)), 'day') if away >= timedelta(hours=48)
               else length_words(away))
        then = ("moves it to TheSportsDB's time" if rule.go_by == GO_TSDB else 'widens it to cover both')
        return wins[0], (f"{both} {league.name} goes by TheSportsDB's time only from "
                         f"{counted(rule.wait_hours, 'hour')} before a {one}, and this one is {far} away, so the "
                         f"recording keeps the listing's times for now. If the two still differ then, ChannelBin "
                         f"{then}, unless you change its times below.")
    goes = _goes_by_words(league, rule)
    follows = f' If either time changes before the {one}, the recording follows, unless you change its times below.'
    if rule.go_by == GO_TSDB:
        w_start, w_stop = wins[0]
        end = (f'ends with the listing, at {clock(w_stop)}' if w_stop == stop
               else f'ends {length_words(w_stop - stop)} after the listing, at {clock(w_stop)}')
        return wins[0], f'{both} {goes} so the recording starts at {clock(w_start)} and {end}.{follows}'
    if rule.go_by != GO_BOTH:
        raise ValueError(f'unknown answer {rule.go_by!r}')
    if len(wins) == 1:
        return wins[0], f'{both} {goes} so the recording runs from {clock(wins[0][0])} to {clock(wins[0][1])}.{follows}'
    return wins[0], (f"TheSportsDB has the {one} at {clock(tsdb)}, {length_words(abs(tsdb - start))} from the "
                     f"listing's {clock(start)}. That is more than the {rule.cover_both_minutes}-minute "
                     f"limit for covering both in one recording, so this recording keeps the listing's times and "
                     f"ChannelBin schedules a second one at {clock(tsdb)}, in case TheSportsDB is right. Change the "
                     f"times below and it records only the times you set.")


def _pick_notice(pick, event, league, one, many, now, tz, h24, times_text="The times are the listing's.") -> dict:
    """{tone, strong, text}: the record dialog's one notice, saying which listing Record picked and
    why. Every GamePick kind that records is named. `times_text` is listing_times()'s sentence for a
    live pick."""
    from . import auto_rules as ar
    from .auto_rules_sports import START_FROM_NAME, START_FROM_TSDB
    from .database import SPORTS_AIRING_FIRST
    clock = (lambda dt: format_local(dt, 'clock', tz=tz, h24=h24))
    if pick.kind in (ar.PICK_LIVE, ar.PICK_REPLAY):
        s = pick.showing
        c = sports_match.game_counts([event.id]).get(event.id) or {}
        if s.group_id is not None:
            source = (f'your channel group "{s.group_name}", which goes ahead of a single channel because it can '
                      'switch channels if one fails')
        else:
            first = pick.program is None or s is pick.program.lead
            source = (s.channel_name if pick.channels == 1 or not first
                      else f'{s.channel_name}, the healthiest of them')
        if pick.kind == ar.PICK_REPLAY:
            lead = (f'The live airing of this {one} is over' if c.get('first') or c.get('unknown')
                    else f'Only replays of this {one} are in your guide')
            return {'tone': 'warn', 'strong': 'This is a replay.',
                    'text': (f'{lead}, so ChannelBin picked the replay most channels carry '
                             f'({counted(pick.channels, "channel")}), recorded from {source}, at {clock(s.start)} '
                             f'{_day_words(s.start, now, tz)}. An auto-record rule would not record a replay here.')}
        what = 'the live airing' if pick.airing == SPORTS_AIRING_FIRST else 'the listing'
        doubt = ('' if pick.airing == SPORTS_AIRING_FIRST
                 else ' Your guide does not say yet whether it is live or a replay.')
        total = max(c.get('channels') or 0, pick.channels)
        passed = ''
        if pick.wider is not None:
            carried = f'{what} at the {one}\'s start, on {pick.channels} of the {total} channels with this {one}'
            passed = (f' {counted(len(pick.wider.showings), "channel")} list it at {clock(pick.wider.lead.start)} '
                      f'instead, too far from {clock(event.guide_start)}, when ChannelBin believes it starts.')
        elif pick.channels == total:
            carried = (f'{what}, on the one channel with this {one}' if total == 1
                       else f'{what}, on all {total} channels with this {one}')
        else:
            carried = f'{what} most of your channels carry ({pick.channels} of the {total} channels with this {one})'
        return {'tone': 'info', 'strong': 'ChannelBin picked this listing.',
                'text': (f'It is {carried}, recorded from {source}.{passed} An auto-record rule for this league would pick '
                         f"the same one.{doubt} {times_text}")}
    if pick.kind == ar.PICK_EVENT:
        ep = pick.event
        moves = ''
        if ep.source == START_FROM_NAME:
            start = f'Its name gives the start, {clock(ep.start)} {_day_words(ep.start, now, tz)},'
        elif ep.source == START_FROM_TSDB:
            start = (f"Its name gives no time zone, so the start is TheSportsDB's ({clock(ep.start)} "
                     f"{_day_words(ep.start, now, tz)}), which {league.name} goes by,")
            moves = (f' If TheSportsDB moves the {one} before it starts, the recording moves with it, unless you '
                     'change its times below.')
        else:
            raise ValueError(f'unknown event start source {ep.source!r}')
        return {'tone': 'warn', 'strong': "From an event channel's name.",
                'text': (f'This {one} has no listing in your guide yet. "{ep.named.said_by}" mentions it, so '
                         f'Record uses {_record_channel_words(ep.named)}. {start} and it records for {length_words(ep.stop - ep.start)}, '
                         f'since there is no listing to say how long. Change either time below if you know better.{moves} '
                         f'Providers rename these channels during the week, so look again closer to the {one}.')}
    raise ValueError(f'a GamePick with no notice: {pick.kind!r}')


def _record_channel_words(named) -> str:
    """Which channel an event pick records from: the one whose name was read, or the kept copy of
    its stream when the name read is a duplicate copy's."""
    if named.source_name:
        return f'"{named.channel_name}", the copy of that stream ChannelBin keeps'
    return 'that channel'


def record_offer(event_id, cfg=None) -> tuple:
    """(payload, HTTP status) for a league page's Record on one game: what the record dialog opens
    on, worked out here from the event id alone (auto_rules.game_pick), never from anything the
    page sent. A listing answers its EPG id and group for the shared record-context endpoint; an
    event channel answers the dialog's program itself, since it has no listing. A game that has a
    recording already, or nothing to record, is a 409 saying so: the page was out of date."""
    from . import auto_rules as ar
    from .accounts import default_profile_for
    from .database import Channel
    from .routes.recordings import BULK_SKIP, _plan_event_schedule
    cfg = cfg or load_config()
    now = datetime.utcnow()
    tz, h24 = get_display_tz(), is_24h()
    event = db.session.get(SportsEvent, event_id)
    if event is None:
        return {'error': 'That event is no longer stored.'}, 404
    league = db.session.get(SportsLeague, event.league_id)
    one, many = _noun(league, [event])
    held = (_held_by_event([event], now, tz, h24).get(event.id) or {}).get('main')
    if held is not None:
        has = {HELD_SCHEDULED: 'already has a recording scheduled', HELD_RECORDING: 'is being recorded now',
               HELD_FINISHING: 'has been recorded', HELD_RECORDED: 'has been recorded'}[held['state']]
        return {'error': f'This {one} {has}. Reload the page to see it.'}, 409
    pick = ar.game_pick(event.id, now, cfg)
    if pick.kind == ar.PICK_REPLAY and (_held_by_event([event], now, tz, h24).get(event.id) or {}).get('replay'):
        return {'error': f'This {one} already has its replay scheduled. Reload the page to see it.'}, 409
    if pick.kind == ar.PICK_NONE:
        guide_end = db.session.query(func.max(EPGEntry.start_time)).scalar()
        return {'error': pick_why(pick.why, one, guide_end, now, tz, pick.said)}, 409
    moved = None
    if pick.kind in (ar.PICK_LIVE, ar.PICK_REPLAY):
        # The same answer a rule's scheduling step gives: the listing's first channel, unless
        # its account has no connection in the window and another channel's does.
        choice = ar.seat_choice(pick.program, 'default', cfg)
        if choice.moved:
            moved = choice
            pick = replace(pick, showing=choice.showing)
    times, times_text = None, "The times are the listing's."
    if pick.kind == ar.PICK_LIVE:
        from .sports_times import Rules
        times, times_text = listing_times(event, league, Rules(cfg).of(league.tsdb_id), pick.showing.start,
                                          pick.showing.stop, one, now, tz, h24)
    notice = _pick_notice(pick, event, league, one, many, now, tz, h24, times_text)
    if moved is not None:
        notice = {**notice, 'text': f'{notice["text"]} {moved.sentence()}'}
    if pick.kind in (ar.PICK_LIVE, ar.PICK_REPLAY):
        iso = (lambda dt: dt.strftime('%Y-%m-%dT%H:%M:%S'))
        out = {'success': True, 'kind': pick.kind, 'epg_id': pick.showing.entry_id, 'sports_event_id': event.id,
               'group_id': pick.showing.group_id, 'notice': notice, 'search_url': search_url(event)}
        if times is not None:
            # The dialog's times before the profile's padding: the listing's own, or where the
            # league's answer moves them (listing_times()).
            out['times'] = {'start_time': iso(times[0]), 'stop_time': iso(times[1])}
        return out, 200
    if pick.kind != ar.PICK_EVENT:
        raise ValueError(f'a GamePick kind record_offer does not name: {pick.kind!r}')
    ep = pick.event
    plan = _plan_event_schedule(ep.named.channel_id, pick.game.title, ep.start, ep.stop, 'default', cfg)
    if plan['verdict'] == BULK_SKIP or not plan.get('_create'):
        return {'error': plan.get('reason') or 'The event channel cannot be recorded from.'}, 409
    args = plan['_create']
    ch = db.session.get(Channel, ep.named.channel_id)
    profile = default_profile_for(ch, None)
    iso = (lambda dt: dt.strftime('%Y-%m-%dT%H:%M:%S'))
    return {'success': True, 'kind': pick.kind, 'notice': notice, 'sports_event_id': event.id,
            'program': {'id': None, 'sports_event_id': event.id, 'title': pick.game.title, 'sub_title': '',
                        'start_time': iso(ep.start), 'stop_time': iso(ep.stop), 'channel_id': ch.id,
                        'channel_name': ch.name, 'group_id': None, 'stream_url': args['url'],
                        'suggested_name': args['name'],
                        'suggested_name_template': args.get('sports_name_template') or '',
                        'has_recording': False, 'description': None},
            'channel': {'id': ch.id, 'default_profile_id': profile.id if profile is not None else None}}, 200


def channel_now(event_id, cfg=None) -> tuple:
    """(payload, HTTP status) for "Channel it would use" on one game (dev/changelog/1639): the
    channel a recording of it would start on if it started now, and the channel its sports channel
    on the IPTV output would play, each from the code that does it, never a copy. Both start from
    auto_rules.game_sources(). The recording then takes Record's seat_choice() and, for a group,
    the member the group would start on (output_feed.sports_member_now() with no allowlist); the
    play walks the same order with the blocks and the IPTV output's allowlist, as a tune-in does.

    `go` is the channel page to open when there is one answer to give - the two agree, or no sports
    channel would play it at all - and nothing else needs saying; otherwise the page shows both and
    why. Reads only: no seat, no admission ticket, no row."""
    from flask import url_for
    from . import auto_rules as ar
    from . import output_feed, sports_feed
    from .channel_groups import blocked_account_ids_now
    from .database import Account, Channel, Recording
    cfg = cfg or load_config()
    now = datetime.utcnow()
    tz, h24 = get_display_tz(), is_24h()
    event = db.session.get(SportsEvent, event_id)
    if event is None:
        return {'error': 'That event is no longer stored.'}, 404
    league = db.session.get(SportsLeague, event.league_id)
    one, _many = _noun(league, [event])
    game = event_game_name(event)
    found = ar.game_sources(event.id, now, cfg)
    pick = found.pick
    blocked = blocked_account_ids_now()
    fcfg = output_feed.feed_config(cfg)

    def chosen(channel_id, group_id=None, group_name=''):
        return {'channel_id': channel_id, 'group_id': group_id, 'group_name': group_name}

    # The recording: Record's own steps (record_offer), then the member a group starts on.
    record = {'why': '', 'note': ''}
    if pick.kind == ar.PICK_NONE:
        guide_end = db.session.query(func.max(EPGEntry.start_time)).scalar()
        record['why'] = pick_why(pick.why, one, guide_end, now, tz, pick.said)
    elif pick.kind == ar.PICK_EVENT:
        from .routes.recordings import BULK_SKIP, _plan_event_schedule
        ep = pick.event
        plan = _plan_event_schedule(ep.named.channel_id, pick.game.title, ep.start, ep.stop, 'default', cfg)
        if plan['verdict'] == BULK_SKIP or not plan.get('_create'):
            record['why'] = plan.get('reason') or 'The event channel cannot be recorded from.'
        else:
            record.update(chosen(ep.named.channel_id))
            record['note'] = (f'Your guide has no listing of this {one}, so it would be recorded from the channel '
                              'whose name mentions it.')
    elif pick.kind in (ar.PICK_LIVE, ar.PICK_REPLAY):
        choice = ar.seat_choice(pick.program, 'default', cfg)
        showing = choice.showing
        notes = []
        if pick.kind == ar.PICK_REPLAY:
            notes.append(f'Every live listing of this {one} has ended, so it would record a replay.')
        if choice.moved or choice.refused or choice.kept is not None:
            notes.append(choice.sentence())
        member, why, _from = output_feed.sports_member_now([showing], blocked, None)
        if member is None:
            where = f'the group "{showing.group_name}"' if showing.group_id is not None else f'"{showing.channel_name}"'
            if why == output_feed._DROP_OTHER_GAME:
                # The recorder starts anyway and says so on the recording (recorder.game_members()).
                record['why'] = (f'Record would pick {where}, but {_record_drop_words(why)}. A recording would '
                                 'still start, on the best of them, and say so on the recording.')
            else:
                record['why'] = (f'Record would pick {where}, but it could not start now: '
                                 f'{_record_drop_words(why)}.')
        else:
            record.update(chosen(member.id, showing.group_id, showing.group_name or ''))
        record['note'] = ' '.join(notes)
    else:
        raise ValueError(f'a GamePick kind channel_now does not name: {pick.kind!r}')

    # The sports channel: a tune-in's walk over the same order (output_feed._resolve_sports_play).
    # While the game is in a carrier's window now, over the listings that play holds, ended ones
    # included, as the tune-in does; otherwise over the listings not yet over, as if it started now.
    feed_on = output_feed.is_enabled(fcfg)
    served = sports_feed.carriers(sports_feed.served_channels(), [event]).get(event.id, ())
    play = {'why': '', 'note': '', 'applies': feed_on and bool(served), 'carriers': [s.name for s in served]}
    if not feed_on:
        play['note'] = 'The IPTV output is off, so nothing plays it there.'
    elif not served:
        play['note'] = (f'No sports channel on your IPTV output carries this {one}. Switch one on for it in '
                        "this league's On your IPTV output card.")
    if play['applies']:
        before, after = output_feed.sports_window(fcfg)
        on = sports_feed.playing_now(served[0], now, event_id=event.id, before=before, after=after)
        play_pick, sources = pick.kind, found.sources
        if on.channels and on.named is None:
            held_by_play = ar.game_sources(event.id, now, cfg, listings=on.listings)
            sources = held_by_play.sources if held_by_play is not None else []
            play_pick = ar.PICK_LIVE if sources else None
        if play_pick is None:
            play['why'] = (f'Your guide lists {game} on {counted(len(on.channels), "channel")} now, but none of '
                           'those listings is one Record would pick for it.')
        elif play_pick in (ar.PICK_LIVE, ar.PICK_EVENT):
            named = play_pick == ar.PICK_EVENT
            member, why, source = output_feed.sports_member_now(sources, blocked,
                                                                output_feed.allowed_account_ids(fcfg))
            if member is None:
                play['why'] = ('A sports channel would have nothing to play: '
                               f'{output_feed.sports_drop_words(why, game, named=named)}.')
            else:
                play.update(chosen(member.id, source.group_id, source.group_name or ''))
                if named:
                    play['note'] = (f'Your guide has no listing of this {one}, so a sports channel plays it from '
                                    'the channel whose name mentions it, as a recording would.')
        elif play_pick == ar.PICK_REPLAY:
            play['why'] = (f'Every live listing of this {one} has ended, and a sports channel plays only the live '
                           f'{one}, never a replay.')
        elif play_pick == ar.PICK_NONE:
            play['why'] = 'There is nothing for a sports channel to play either.'
        else:
            raise ValueError(f'a GamePick kind channel_now does not name: {play_pick!r}')

    ids = [s['channel_id'] for s in (record, play) if s.get('channel_id')]
    rows = {c.id: (c, a) for c, a in db.session.query(Channel, Account).join(
        Account, Account.id == Channel.account_id).filter(Channel.id.in_(ids))} if ids else {}
    for side in (record, play):
        cid = side.pop('channel_id', None)
        gid, gname = side.pop('group_id', None), side.pop('group_name', '')
        side['channel'] = side['group'] = None
        if cid is None:
            continue
        if cid not in rows:
            side['why'] = 'The channel it would use was removed as this was worked out. Ask again.'
            continue
        ch, acct = rows[cid]
        side['channel'] = {'id': ch.id, 'name': ch.name,
                           'url': url_for('channels.channel_detail', channel_id=ch.id),
                           'account': {'id': acct.id, 'name': acct.name, 'color': acct.color,
                                       'url': url_for('accounts.account_detail', account_id=acct.id)}}
        if gid is not None:
            side['group'] = {'id': gid, 'name': gname,
                             'url': url_for('channel_groups.group_detail', group_id=gid)}

    rec_ch, play_ch = record['channel'], play['channel']
    same = rec_ch is not None and play_ch is not None and rec_ch['id'] == play_ch['id']
    footnote = ''
    if record['group'] is not None:
        footnote = (f'A recording of a group picks its channel as it starts, so one that starts while an '
                    f"account's connection is in use can start on another channel of \"{record['group']['name']}\".")
    slot = 'replay' if pick.kind == ar.PICK_REPLAY else 'main'
    held = (_held_by_event([event], now, tz, h24).get(event.id) or {}).get(slot)
    if held is not None and held['state'] in (HELD_SCHEDULED, HELD_RECORDING):
        # A recording already made keeps its own listing; said only where it is not this answer.
        rec = db.session.get(Recording, held['id'])
        mine = (rec.group_id is not None and record['group'] is not None and rec.group_id == record['group']['id']) or (
            rec.group_id is None and rec_ch is not None and rec.channel_id == rec_ch['id'])
        if not mine:
            what = 'replay' if slot == 'replay' else 'recording'
            has = (f'is being recorded, on {held["where"]}' if held['state'] == HELD_RECORDING
                   else f'already has its {what} scheduled, on {held["where"]}')
            record['note'] = ' '.join(x for x in (record['note'], f'This {one} {has}, which keeps the listing it '
                                                                   'was made from. The channel above is the one a '
                                                                   'new recording would use now.') if x)

    go, toast = None, ''
    if rec_ch is not None and not record['note'] and (same or not play['applies']):
        go = rec_ch['url']
        group = record['group']
        toast = (f'If {game} started now, ChannelBin would record it from this channel'
                 + (f', the one the group "{group["name"]}" would start on' if group is not None else '')
                 + (', and its sports channel on your IPTV output would play it from here too.' if same
                    else f'. {play["note"]}'))
        if footnote:
            toast += f' {footnote}'
    return {'success': True, 'game': game, 'one': one, 'same': same, 'record': record, 'play': play,
            'footnote': footnote, 'go': go, 'toast': toast}, 200


def _record_drop_words(why) -> str:
    """Why the channel Record would pick could not start now (channel_now), for each _DROP_* one
    showing can answer with nothing yet tried and no allowlist."""
    from . import output_feed as of
    return {
        of._DROP_BLOCKED: 'its account is blocked right now, or every channel of the group is on one that is',
        of._DROP_RECORDING: 'the group has no channel with Recording on',
        of._DROP_OTHER_GAME: 'every channel in the group with Recording on shows something else at that time',
        of._DROP_GONE: 'it was removed as this was worked out',
    }[why]


def event_record_check(event_id, channel_id):
    """(stand-in listing, None) for a recording a person is saving from a league page's Record on a
    game only an event channel names, or (None, the sentence why not). The pick is worked out again
    here, and the channel the dialog opened on must still be it (dev/changelog/1539)."""
    from types import SimpleNamespace
    from . import auto_rules as ar
    event = db.session.get(SportsEvent, event_id)
    if event is None:
        return None, 'That event is no longer stored.'
    pick = ar.game_pick(event.id)
    one = event_noun(db.session.get(SportsLeague, event.league_id), event)
    if pick.kind != ar.PICK_EVENT:
        return None, (f'What Record picks for this {one} changed since the dialog opened. Close it and press '
                      'Record again.')
    ep = pick.event
    if ep.named.channel_id != channel_id:
        return None, (f'The event channel naming this {one} changed since the dialog opened. Close it and press '
                      'Record again.')
    return SimpleNamespace(channel_id=channel_id, start_time=ep.start, stop_time=ep.stop, title=pick.game.title,
                           sub_title=None, description=None, category=None, rating=None,
                           sports_event_tsdb_id=event.tsdb_id, start_from=ep.source), None


def record_reason(event_id, entry, group_id):
    """The auto_rules.PickReason for a recording a person is saving from a league page's Record, or
    None when the channel saved is not one Record's pick has: the pick is worked out again here,
    and a channel a person chose instead is theirs, with no reason of ChannelBin's to give. `entry`
    is the listing saved, or event_record_check()'s stand-in for an event channel."""
    from . import auto_rules as ar
    found = ar.game_sources(event_id)
    if found is None or found.why is None:
        return None
    pick = found.pick
    if pick.kind == ar.PICK_EVENT:
        return found.why if pick.event.named.channel_id == entry.channel_id else None
    if pick.kind not in (ar.PICK_LIVE, ar.PICK_REPLAY):
        return None
    used = next((s for s in pick.program.showings
                 if s.entry_id == getattr(entry, 'id', None) and s.group_id == group_id), None)
    if used is None:
        return None
    # Moved off its first channel for a connection: the listing's half stands, and the dialog's
    # own sentence about the move is the channel's.
    return found.why if used is pick.program.lead else found.why.moved()


def record_mark(event_id, entry):
    """TheSportsDB's id for the game when `entry` is a listing the matcher found for it and nobody
    marked wrong, else None: what Recording.sports_record_tsdb_id holds for a league page's Record
    on a listing (dev/changelog/1630). One query. The page's event id is never taken on trust."""
    from .database import SPORTS_MATCH_KIND_GUIDE, SportsMatch
    row = (db.session.query(SportsEvent.tsdb_id)
           .join(SportsMatch, SportsMatch.event_id == SportsEvent.id)
           .filter(SportsEvent.id == event_id, SportsMatch.kind == SPORTS_MATCH_KIND_GUIDE,
                   SportsMatch.channel_id == entry.channel_id, SportsMatch.program_start_time == entry.start_time,
                   SportsMatch.rejected_at.is_(None)).first())
    return row[0] if row is not None else None


def _record_button(event, c, listings, games, guide_end, one, now, tz) -> dict:
    """{label} for a Record that records, or {label, why} for a greyed one."""
    from . import auto_rules as ar
    from .auto_rules_sports import event_pick
    said = ''
    kind, start = _preview_pick(event, listings)
    if kind is not None:
        label = 'Record the replay' if kind == ar.PICK_REPLAY else 'Record'
        return {'label': f'{label} now' if start <= now else label, 'replay': kind == ar.PICK_REPLAY}
    if not c.get('channels') and c.get('wrong'):
        return {'label': 'Record', 'why': (f'Every listing ChannelBin found for this {one} is marked a wrong match, '
                                           'so there is nothing to record. Listings on your guide can undo that.')}
    if c.get('channels'):
        why = ar.WHY_UNSURE if listings else ar.WHY_ENDED
    elif games is not None and event.id in games.games:
        pick, said = event_pick(games, games.games[event.id])
        if pick is not None and pick.stop > now:
            return {'label': 'Record now' if pick.start <= now else 'Record'}
        why = ar.WHY_ENDED if pick is not None else (ar.WHY_NO_START if games.named_by.get(event.id) else ar.WHY_UNLISTED)
    else:
        why = ar.WHY_UNLISTED
    return {'label': 'Record', 'why': pick_why(why, one, guide_end, now, tz, said)}


def _matching_context(league, now: datetime, cfg: dict, one: str) -> dict:
    """The Matching your guide card: the league's names, a known name it lacks, its categories with
    how many channels each has now, and the words and lengths that keep a listing out. A category gone
    from every account is kept and says so (5.2). A Replays-only category says how many coming games
    it lists at their own start."""
    counts = sports_match.category_counts()
    cats = SportsLeagueCategory.query.filter_by(league_id=league.id).order_by(SportsLeagueCategory.category_name).all()
    live = (sports_match.replay_category_live_games(league.id, now)
            if any(c.is_replay for c in cats) else {})
    return {
        'terms': json.loads(league.match_terms or '[]'),
        'suggested': sports_match.suggested_terms(league),
        'teams': sports_match.team_names_context(league.id),
        'categories': [{'name': c.category_name, 'channels': counts.get(c.category_name, 0),
                        'replay': c.is_replay, 'live_games': live.get(c.category_name, 0)} for c in cats],
        'skips': skips_context(league, cfg, one),
    }


def skips_context(league, cfg: dict, one: str) -> dict:
    """The words and lengths at the end of Matching your guide (mockup 59, dev/changelog/1634). Which
    list is the page's own noun's (_noun): a game's for a league of games, the race list for races and
    any other field event. The page carries the list it shows, and its changes are written to that one.
    The session length shows only for a league whose events include sessions, as a rule's Practice and
    qualifying switch does (dev/changelog/1633)."""
    from .auto_rules_sports import league_has_sessions
    skips = sports_skips.SkipRules(cfg).of(league.tsdb_id)
    game = one in (sports_words.GAME[0], sports_words.MATCH[0])
    kind = sports_skips.KIND_GAME if game else sports_skips.KIND_RACE
    a_one = sports_words.a(one)
    if game:
        lengths = [(sports_skips.LENGTH_GAME, f'Shortest {one} listing (minutes)',
                    f'A listing shorter than this is never the {one}, even when it names both teams: a 30-minute '
                    'show about it often does.')]
        # The examples are the built-in words themselves (sports_skips), which are spelled for a game.
        shows, space = 'a pre-game show, a highlights show or a betting show', ('pre game', 'Pregame')
    elif kind == sports_skips.KIND_RACE:
        lengths = [(sports_skips.LENGTH_RACE, f'Shortest {one} listing (minutes)',
                    f'A listing shorter than this is never the {one}: a one-hour wrap-up show names it too.')]
        if one == 'race':
            shows, space = "a highlights show, a pre-race show or a driver's onboard camera", ('pre race', 'Prerace')
        else:
            shows, space = 'a highlights show or a preview', ('best of', 'Bestof')
        if league_has_sessions(league.id):
            lengths.append((sports_skips.LENGTH_SESSION, 'Shortest practice, qualifying or sprint listing (minutes)',
                            'Sessions run shorter than races, so they get their own limit. A pre-show a few minutes '
                            'long stays out.'))
    else:
        raise ValueError(f'unknown list {kind!r}')
    def quoted(words):
        return ', '.join(f'"{w}"' for w in words)
    changes = []
    if skips.added:
        changes.append(f'added {quoted(skips.added)}')
    if skips.removed:
        changes.append(f'removed {quoted(skips.removed)}')
    names = {sports_skips.LENGTH_GAME: one, sports_skips.LENGTH_RACE: one,
             sports_skips.LENGTH_SESSION: 'practice, qualifying or sprint'}
    for length in sports_skips.LENGTHS:
        if length in skips.own_minutes:
            changes.append(f'the shortest {names[length]} listing set to {skips.own_minutes[length]} minutes')
    own = sports_skips.LEAGUE_WORDS.get(str(league.tsdb_id))
    return {
        'game': game, 'one': one, 'a_one': a_one, 'kind': kind, 'shows': shows, 'space': space,
        'words': list(skips.words(kind)),
        'lengths': [{'key': key, 'label': label, 'desc': desc, 'value': skips.own_minutes.get(key, ''),
                     'default': sports_skips.BUILTIN_MINUTES[key]} for key, label, desc in lengths],
        'max_minutes': sports_skips.MAX_MINUTES, 'max_word': sports_skips.MAX_WORD,
        'own_note': (f'{quoted(own["words"])} are built in for {league.name} only: {own["why"]}' if own else ''),
        'changes': '; '.join(changes), 'changed': skips.changed,
    }


def add_dialog_config() -> dict:
    """The shipped list, for Add league or team. `following` says what the person follows in a
    league already here ("Following all games", "Following 3 teams", "Followed by a rule", "Not
    followed"), or is None for
    one ChannelBin has never had, whose row shows its sport. Every row can be picked: a league here
    opens the team picker on what is followed now (dev/changelog/1641). Two queries."""
    tsdb_ids = [s['tsdb_id'] for s in SHIPPED_LEAGUES]
    here = {lg.id: lg for lg in SportsLeague.query.filter(SportsLeague.tsdb_id.in_(tsdb_ids)).all()}
    every = _group(SportsFollow.query.filter(SportsFollow.league_id.in_(list(here))).all() if here else [],
                   lambda f: f.league_id)
    following = {}
    for league_id, league in here.items():
        follows = [f for f in every.get(league_id, []) if not f.rule_id]
        if not follows:
            # A rule's follow is changed on the rule (dev/changelog/1490), so the picker starts empty.
            following[league.tsdb_id] = 'Followed by a rule' if every.get(league_id) else 'Not followed'
        elif any(f.team_tsdb_id == 0 for f in follows):
            following[league.tsdb_id] = f'Following all {_noun(league)[1]}'
        else:
            following[league.tsdb_id] = f'Following {counted(len(follows), "team")}'
    return {'shipped': [{'tsdb_id': s['tsdb_id'], 'name': s['name'], 'sport': s['sport'],
                         'following': following.get(s['tsdb_id'])} for s in SHIPPED_LEAGUES],
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


def _link_noun(link) -> str:
    """The word for the event a recording is linked to, from its league's sport while the event is
    still stored, and from the snapshot's shape alone once it is pruned. One query."""
    row = (db.session.query(SportsLeague.sport).join(SportsEvent, SportsEvent.league_id == SportsLeague.id)
           .filter(SportsEvent.tsdb_id == link.tsdb_event_id).first())
    return sports_words.noun(row[0] if row is not None else None, bool(link.home_name and link.away_name))[0]


def _cover_pair(rec, one, tz, h24):
    """{label, before, other_id, after} for a recording that is one of Cover both's two (app/sports_follow.py,
    dev/changelog/1631), or None. One query."""
    from .database import Recording
    clock = (lambda dt: format_local(dt, 'day_datetime', tz=tz, h24=h24))
    if rec.sports_cover_for_id is not None:
        return {'label': 'Second recording',
                'before': f"ChannelBin's second recording of this {one}, at TheSportsDB's time, in case it is right. ",
                'other_id': rec.sports_cover_for_id, 'after': " records the listing's time. This one is removed if "
                'the two times come back together.'}
    other = (db.session.query(Recording.id, Recording.start_time).filter(Recording.sports_cover_for_id == rec.id)
             .order_by(Recording.id.desc()).first())
    if other is None:
        return None
    return {'label': 'Second recording', 'before': f"TheSportsDB has this {one} at another time, too far from the "
            "listing's for one recording, so ", 'other_id': other.id,
            'after': f' records it there too, from {clock(other.start_time)}, in case it is right.'}


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
        one = _link_noun(link)
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
                'file_name': _future_file_name(rec, link, tz),
                # ChannelBin moves it with its event (app/sports_follow.py): a rule's, a Record one saved
                # at Record's times, and Cover both's second. A rule's one read from an event channel's
                # name on that name's own clock is the exception, and its events say so.
                'follows': (rec.rule_id is not None or rec.sports_cover_for_id is not None
                            or (rec.sports_record_tsdb_id is not None and rec.sports_window_start is not None)),
                'one': one, 'pair': _cover_pair(rec, one, tz, h24)}
    note = _unlinked_note(rec)
    if _name_waiting(rec):
        waiting = ('Its name has TheSportsDB fields, which fill in if its event is matched before its '
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
    pinned_poster = profile is not None and bool(profile.poster_file)
    if ready:
        what = ' and '.join(names[r] for r in ready).capitalize()
        # A pinned poster beats the thumb standing in for a missing one, whatever the profile prefers.
        out.append(f'{what} from TheSportsDB.' if link.poster_url or pinned_poster else
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
    # Which pinned image wins, poster and background alike (dev/changelog/1505, 1508).
    for role, has_pin, url in ((POSTER, pinned_poster, link.poster_url),
                               (BACKGROUND, profile is not None and bool(profile.background_file),
                                link.thumb_url)):
        if not has_pin:
            continue
        name, pin = names[role], f'The image pinned to profile "{profile.name}"'
        if not profile.prefer_sports_art:
            out.append(f'{pin} is the {name}.')
        elif url and art[role][0] == READY:
            out.append(f'Profile "{profile.name}" puts this {name} ahead of its pinned one.')
        elif url and art[role][0] in (MISSED, REFUSED):
            out.append(f'{pin} is the {name}, since TheSportsDB\'s was not downloaded.')
        elif url:
            out.append(f'{pin} is the {name} until TheSportsDB\'s is downloaded.')
        else:
            out.append(f'{pin} is the {name}, since the event has none.')
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
    # A week whose round answer was cut may simply not have sent this game (dev/changelog/1549).
    cut = ('' if not any(lo <= pst <= hi for lo, hi in capped_spans(league.id)) else
           f" TheSportsDB's answer for that part of the schedule was cut at {ROUND_ROW_CAP} events, the most it "
           f'sends for one call, so this one may not have been sent. A listing that names both teams is looked '
           f'up by name.')
    if not nearby:
        hours = int(sports_match.WINDOW.total_seconds() // 3600)
        return (f'No {league.name} event within {hours} hours of this program is stored from TheSportsDB, so '
                f'there was nothing to match it to.{cut}')
    lead = (f'The one {league.name} event around then did not match its listing.' if nearby == 1 else
            f'None of the {nearby} {league.name} events around then matched its listing.')
    return (f'{lead} A match needs the league and both teams, or for a race the series and the date, in the '
            f'title or episode name. The description is read only for a game whose title names the league '
            f'and no team, and then only its first sentence.{cut}')
