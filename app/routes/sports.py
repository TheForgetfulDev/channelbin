"""The Sports Schedules pages and their actions (app/sports_data.py, DESIGN-sports-data.md 5, 11)."""
import threading
import time

from flask import Blueprint, abort, jsonify, render_template, request

from .. import sports_data, sports_match, sports_view
from ..fmt_utils import join_labels

sports_bp = Blueprint('sports', __name__)

#: A follow must name something a lookup just checked (design 5), so a request cannot invent a
#: league's name or number. Kept in memory: a restart only means looking it up again.
_CHECKED_TTL_SECONDS = 3600
_checked_lock = threading.Lock()
_checked_leagues = {}
_checked_teams = {}


def _remember(league: dict, team: dict = None):
    now = time.monotonic()
    with _checked_lock:
        _checked_leagues[league['tsdb_id']] = (now, league)
        if team:
            _checked_teams[team['team_id']] = (now, team)


def _recall(store: dict, key):
    with _checked_lock:
        hit = store.get(key)
    if hit is None or time.monotonic() - hit[0] > _CHECKED_TTL_SECONDS:
        return None
    return hit[1]


@sports_bp.route('/sports')
def sports_page():
    return render_template('sports.html', add_config=sports_view.add_dialog_config(),
                           **sports_view.list_context())


@sports_bp.route('/sports/<int:league_id>')
def league_page(league_id):
    from .recordings import read_section_pref
    ctx = sports_view.page_context(league_id, scope=request.args.get('scope', sports_view.SCOPE_FOLLOWED))
    if ctx is None:
        abort(404)
    return render_template(
        'sports_league.html', add_config=sports_view.add_dialog_config(),
        sections=list(sports_view.SECTIONS), section_pref=read_section_pref(sports_view.SECTIONS_PREF_KEY),
        sections_pref_key=sports_view.SECTIONS_PREF_KEY, **ctx)


def _too_soon(exc: sports_data.TryAgain):
    return jsonify({'error': f'ChannelBin makes one call at a time to TheSportsDB, and one just went out. '
                             f'Try again in {exc.seconds} seconds.', 'retry_after': exc.seconds}), 409


@sports_bp.route('/api/sports/lookup', methods=['POST'])
def lookup():
    """Check what was picked or typed, with one call. Saves nothing. `best_match` marks the answer
    to a name search, which on the free key is TheSportsDB's single best match, never a list."""
    data = request.get_json(silent=True) or {}
    if data.get('tsdb_id'):
        kind, value = 'league', data['tsdb_id']
    else:
        kind, value = sports_data.parse_entry(data.get('text') or '')
    if kind == 'team' and not value:
        return jsonify({'error': 'Type a league, a team or a link.'}), 400
    if kind == 'badlink':
        return jsonify({'error': 'That link is not a league or a team. Links look like '
                                 'https://www.thesportsdb.com/league/4391-NFL or '
                                 'https://www.thesportsdb.com/team/134936-philadelphia-eagles.'}), 400
    if kind == 'league' and not str(value).isdigit():
        return jsonify({'error': 'That is not a league number.'}), 400
    try:
        if kind == 'league':
            league = sports_data.lookup_league(int(value))
            _remember(league)
            return jsonify({'success': True, 'league': league})
        if kind == 'teamlink':
            team = sports_data.lookup_team(value)
            _remember(team['league'], team)
            return jsonify({'success': True, 'league': team['league'], 'team': team})
        teams, defunct = sports_data.search_teams(value)
    except sports_data.TryAgain as exc:
        return _too_soon(exc)
    except sports_data.LeagueNotFound as exc:
        return jsonify({'error': str(exc)}), 404
    except sports_data.LookupFailed as exc:
        return jsonify({'error': str(exc)}), 502
    if not teams and defunct:
        found = (f'TheSportsDB\'s best match for "{value}" is {defunct[0]["team"]}, a defunct team, so it is'
                 if len(defunct) == 1 else
                 f'TheSportsDB found only defunct teams for "{value}" ({join_labels([t["team"] for t in defunct])}), '
                 'so they are')
        return jsonify({'error': f'{found} left out. Type more of the name, such as the city and nickname.'}), 404
    if not teams:
        return jsonify({'error': f'TheSportsDB found no team called "{value}". A league with no teams, such as a '
                                 'racing series, can be added with its number or a link to its page on TheSportsDB.'}), 404
    for team in teams:
        _remember(team['league'], team)
    skipped = [t['team'] for t in defunct]
    if len(teams) == 1:
        return jsonify({'success': True, 'league': teams[0]['league'], 'team': teams[0],
                        'best_match': True, 'skipped': skipped})
    return jsonify({'success': True, 'choices': teams, 'skipped': skipped})


@sports_bp.route('/api/sports/leagues', methods=['POST'])
def add_league():
    """Follow a checked league, whole or just one team of it."""
    data = request.get_json(silent=True) or {}
    league = _recall(_checked_leagues, data.get('tsdb_id'))
    if league is None:
        return jsonify({'error': 'Look that league up again first: the check has expired.'}), 409
    team = None
    if data.get('team_id'):
        team = _recall(_checked_teams, data['team_id'])
        if team is None or team['league']['tsdb_id'] != league['tsdb_id']:
            return jsonify({'error': 'Look that team up again first: the check has expired.'}), 409
    league_id = sports_data.follow_league(league, team)
    sports_data.ensure_worker()
    return jsonify({'success': True, 'id': league_id})


@sports_bp.route('/api/sports/leagues/<int:league_id>/following', methods=['PUT'])
def set_following(league_id):
    """What a person follows in this league: `team_ids`, or the whole league with none."""
    data = request.get_json(silent=True) or {}
    raw = data.get('team_ids') or []
    if not isinstance(raw, list):
        return jsonify({'error': 'team_ids must be a list.'}), 400
    try:
        team_ids = [int(t) for t in raw]
    except (TypeError, ValueError):
        return jsonify({'error': 'team_ids must be numbers.'}), 400
    if not sports_data.set_following(league_id, team_ids):
        return jsonify({'error': 'That league no longer exists.'}), 404
    sports_data.ensure_worker()
    return jsonify({'success': True})


@sports_bp.route('/api/sports/leagues/<int:league_id>/following', methods=['DELETE'])
def stop_following(league_id):
    if not sports_data.stop_following(league_id):
        return jsonify({'error': 'That league no longer exists.'}), 404
    sports_data.ensure_worker()
    return jsonify({'success': True})


@sports_bp.route('/api/sports/leagues/<int:league_id>', methods=['DELETE'])
def delete_league(league_id):
    try:
        deleted = sports_data.delete_league(league_id)
    except sports_data.LeagueInUse as exc:
        return jsonify({'error': str(exc)}), 409
    if not deleted:
        return jsonify({'error': 'That league no longer exists.'}), 404
    sports_data.ensure_worker()
    return jsonify({'success': True})


# ---------------------------------------------------------------------------
# Matches, names and categories (app/sports_match.py, design 5.1, 5.2, 11)
# ---------------------------------------------------------------------------

def _ids(raw):
    if not isinstance(raw, list):
        raise ValueError('match_ids must be a list.')
    return [int(i) for i in raw]


@sports_bp.route('/api/sports/events/<int:event_id>/listings')
def event_listings(event_id):
    """The listings dialog: every guide listing matched to this event, grouped by wording, and the
    event channels whose names name it."""
    from ..database import SportsEvent, SportsLeague
    from ..tz_utils import get_display_tz, is_24h
    from .. import db
    event = db.session.get(SportsEvent, event_id)
    if event is None:
        return jsonify({'error': 'That event is no longer stored.'}), 404
    groups = sports_match.listing_groups(event_id, get_display_tz(), is_24h())
    league = db.session.get(SportsLeague, event.league_id)
    return jsonify({'success': True, 'groups': groups, 'names': sports_match.name_rows(event_id),
                    'one': sports_view.event_noun(league, event),
                    'listings': sum(1 for g in groups for c in g['channels'] if not c['wrong']),
                    'channels': len({c['channel_id'] for g in groups for c in g['channels'] if not c['wrong']}),
                    'tsdb_url': f'https://www.thesportsdb.com/event/{event.tsdb_id}'})


@sports_bp.route('/api/sports/events/<int:event_id>/wrong', methods=['POST'])
def mark_wrong(event_id):
    """Mark listings a wrong match for this event (`wrong: true`), or undo it."""
    data = request.get_json(silent=True) or {}
    try:
        ids = _ids(data.get('match_ids'))
    except (TypeError, ValueError):
        return jsonify({'error': 'match_ids must be a list of numbers.'}), 400
    if not ids:
        return jsonify({'error': 'No listings were named.'}), 400
    changed = sports_match.mark_wrong(event_id, ids, bool(data.get('wrong', True)))
    if changed is None:
        return jsonify({'error': 'That event is no longer stored.'}), 404
    return jsonify({'success': True, 'changed': changed})


@sports_bp.route('/api/sports/leagues/<int:league_id>/terms', methods=['PUT'])
def set_terms(league_id):
    """The names that mean this league in the guide (design 5.1)."""
    data = request.get_json(silent=True) or {}
    if not isinstance(data.get('terms'), list):
        return jsonify({'error': 'terms must be a list.'}), 400
    try:
        saved = sports_match.set_terms(league_id, data['terms'])
    except ValueError as exc:
        return jsonify({'error': str(exc)}), 400
    if saved is None:
        return jsonify({'error': 'That league no longer exists.'}), 404
    return jsonify({'success': True, 'terms': saved})


@sports_bp.route('/api/sports/leagues/<int:league_id>/categories/offer')
def category_offer(league_id):
    from ..database import SportsLeague
    from .. import db
    league = db.session.get(SportsLeague, league_id)
    if league is None:
        return jsonify({'error': 'That league no longer exists.'}), 404
    return jsonify({'success': True, 'categories': sports_match.category_offer(league)})


@sports_bp.route('/api/sports/leagues/<int:league_id>/categories', methods=['PUT'])
def change_categories(league_id):
    """Tie categories (`add`), untie them (`remove`), or set Replays only (`replay`: {name: bool})."""
    data = request.get_json(silent=True) or {}
    add, remove, replay = data.get('add') or [], data.get('remove') or [], data.get('replay') or {}
    if (not isinstance(add, list) or not isinstance(remove, list) or not isinstance(replay, dict)
            or not all(isinstance(n, str) for n in add + remove + list(replay))
            or not all(isinstance(v, bool) for v in replay.values())):
        return jsonify({'error': 'add and remove must be lists of names, replay a map of name to true or false.'}), 400
    if not sports_match.change_categories(league_id, add, remove, replay):
        return jsonify({'error': 'That league no longer exists.'}), 404
    return jsonify({'success': True})
