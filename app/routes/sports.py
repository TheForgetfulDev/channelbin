"""The Sports Schedules pages and their actions (app/sports_data.py, DESIGN-sports-data.md 5, 11)."""
import logging
import threading
import time

from flask import Blueprint, abort, jsonify, render_template, request, url_for

from .. import db, sports_data, sports_match, sports_skips, sports_times, sports_view
from ..database import SportsLeague
from ..fmt_utils import join_labels

sports_bp = Blueprint('sports', __name__)
log = logging.getLogger(__name__)

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
    from ..database import RecordingProfile
    from .recordings import read_section_pref
    args = request.args
    ctx = sports_view.page_context(league_id, scope=args.get('scope', sports_view.SCOPE_FOLLOWED),
                                   page=args.get('page', 1, type=int), feed_page=args.get('feed_page', 1, type=int),
                                   feed_q=args.get('feed_q', ''))
    if ctx is None:
        abort(404)
    return render_template(
        'sports_league.html', add_config=sports_view.add_dialog_config(),
        # The shared record dialog's profile picker (_record_modal.html, dev/changelog/1539).
        profiles=RecordingProfile.query.order_by(RecordingProfile.name).all(),
        sections=list(sports_view.SECTIONS), section_pref=read_section_pref(sports_view.SECTIONS_PREF_KEY),
        sections_pref_key=sports_view.SECTIONS_PREF_KEY, **ctx)


def _stored_league(tsdb_id):
    """The league ChannelBin already has with this TheSportsDB number, or None."""
    if not isinstance(tsdb_id, int) or isinstance(tsdb_id, bool):
        return None
    return SportsLeague.query.filter_by(tsdb_id=tsdb_id).first()


def _stored_payload(league) -> dict:
    """A league already here, in the shape a lookup answers, read from what is stored. Its names
    are not in it: following a league already here never rewrites them."""
    return {'tsdb_id': league.tsdb_id, 'name': league.name, 'sport': league.sport,
            'season': league.current_season}


def _with_here(body: dict) -> dict:
    """A lookup's answer, with what the person follows now in its league when that league is here,
    so the dialog's picker starts from it (dev/changelog/1641)."""
    stored = _stored_league(body['league']['tsdb_id']) if body.get('league') else None
    body['here'] = sports_view.follow_state(stored) if stored is not None else None
    return body


def _team_ids(raw):
    """(sorted team numbers, None), or (None, the error). Whole team numbers only, each once. 0 is
    how the whole league is stored, so a 0 beside a team would store both, and a repeat breaks the
    follow's unique key (dev/docs/BUGS.md 2026-11-15 @ 12:58:58 PM). A bool is an int to Python and
    is refused with the rest."""
    raw = raw if raw is not None else []
    if not isinstance(raw, list):
        return None, 'team_ids must be a list.'
    if not all(isinstance(t, int) and not isinstance(t, bool) and t > 0 for t in raw):
        return None, 'team_ids must be team numbers. Send none to follow the whole league.'
    return sorted(set(raw)), None


def _vouched(league: dict, stored, team_ids: list) -> tuple:
    """(teams a lookup just checked in this league, None), or ([], the 409 naming a team that is
    neither that nor one ChannelBin already knows plays in the league, sports_data.known_team_ids()).
    A follow may name only these, so a request cannot invent a team (design 5)."""
    checked, rest = [], []
    for team_id in team_ids:
        team = _recall(_checked_teams, team_id)
        if team is not None and team['league']['tsdb_id'] == league['tsdb_id']:
            checked.append(team)
        else:
            rest.append(team_id)
    if stored is not None and rest:
        known = sports_data.known_team_ids(stored.id)
        rest = [t for t in rest if t not in known]
    if not rest:
        return checked, None
    team = _recall(_checked_teams, rest[0])
    if team is not None:
        why = f'{team["team"]} plays in the {team["league"]["name"]}, not the {league["name"]}.'
    else:
        why = f'ChannelBin has not checked team {rest[0]} in the {league["name"]}, or the check has expired.'
    return [], (jsonify({'error': f'{why} Look the team up again first.'}), 409)


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
        text = data.get('text')
        kind, value = sports_data.parse_entry(text if isinstance(text, str) else '')
    if kind == 'team' and not value:
        return jsonify({'error': 'Type a league, a team or a link.'}), 400
    if kind == 'badlink':
        return jsonify({'error': 'That link is not a league or a team. Links look like '
                                 'https://www.thesportsdb.com/league/4391-NFL or '
                                 'https://www.thesportsdb.com/team/134936-philadelphia-eagles.'}), 400
    if kind == 'league' and not str(value).isdecimal():
        return jsonify({'error': 'That is not a league number.'}), 400
    if kind == 'league':
        # A league already here is answered from what is stored, with no call (dev/changelog/1641).
        stored = _stored_league(int(value))
        if stored is not None:
            return jsonify(_with_here({'success': True, 'league': _stored_payload(stored)}))
    try:
        if kind == 'league':
            league = sports_data.lookup_league(int(value))
            _remember(league)
            return jsonify({'success': True, 'league': league, 'here': None})
        if kind == 'teamlink':
            team = sports_data.lookup_team(value)
            _remember(team['league'], team)
            return jsonify(_with_here({'success': True, 'league': team['league'], 'team': team}))
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
        return jsonify(_with_here({'success': True, 'league': teams[0]['league'], 'team': teams[0],
                                   'best_match': True, 'skipped': skipped}))
    # Each choice says what is followed in its league, as a single answer does (_with_here).
    choices = []
    for team in teams:
        stored = _stored_league(team['league']['tsdb_id'])
        choices.append({**team, 'here': sports_view.follow_state(stored) if stored is not None else None})
    return jsonify({'success': True, 'choices': choices, 'skipped': skipped})


@sports_bp.route('/api/sports/leagues', methods=['POST'])
def add_league():
    """Follow a league, whole or just `team_ids` of it, replacing what the person followed in it. A
    league or team must be one a lookup just checked, or one ChannelBin already has, so a request
    cannot invent a name or a number (design 5)."""
    data = request.get_json(silent=True) or {}
    team_ids, error = _team_ids(data.get('team_ids'))
    if error:
        return jsonify({'error': error}), 400
    # Anything but a whole number names no league, and a list or an object cannot be looked up at all.
    tsdb_id = data.get('tsdb_id')
    if not isinstance(tsdb_id, int) or isinstance(tsdb_id, bool):
        tsdb_id = None
    stored = _stored_league(tsdb_id)
    league = _recall(_checked_leagues, tsdb_id) or (_stored_payload(stored) if stored else None)
    if league is None:
        return jsonify({'error': 'Look that league up again first: the check has expired.'}), 409
    checked, refused = _vouched(league, stored, team_ids)
    if refused:
        return refused
    before = _sports_served()
    league_id = sports_data.follow_league(league, team_ids, checked)
    sports_data.ensure_worker()
    return jsonify({'success': True, 'id': league_id, 'cut': _cut_unserved(before)})


@sports_bp.route('/api/sports/leagues/<int:league_id>/following', methods=['PUT'])
def set_following(league_id):
    """What a person follows in this league: `team_ids`, or the whole league with none. Each team is
    one a lookup just checked in the league, stored with its name, or one ChannelBin already has."""
    data = request.get_json(silent=True) or {}
    team_ids, error = _team_ids(data.get('team_ids') or [])
    if error:
        return jsonify({'error': error}), 400
    league = db.session.get(SportsLeague, league_id)
    if league is None:
        return jsonify({'error': 'That league no longer exists.'}), 404
    checked, refused = _vouched({'tsdb_id': league.tsdb_id, 'name': league.name}, league, team_ids)
    if refused:
        return refused
    before = _sports_served()
    if not sports_data.set_following(league_id, team_ids, checked):
        return jsonify({'error': 'That league no longer exists.'}), 404
    sports_data.ensure_worker()
    return jsonify({'success': True, 'cut': _cut_unserved(before)})


@sports_bp.route('/api/sports/leagues/<int:league_id>/following', methods=['DELETE'])
def stop_following(league_id):
    before = _sports_served()
    if not sports_data.stop_following(league_id):
        return jsonify({'error': 'That league no longer exists.'}), 404
    sports_data.ensure_worker()
    return jsonify({'success': True, 'cut': _cut_unserved(before)})


@sports_bp.route('/api/sports/leagues/<int:league_id>/feed-channels', methods=['PUT'])
def set_feed_channel(league_id):
    """Turn one of this league's sports channels on the output feed on or off: a team's channel
    (`team_id`), or the league's numbered channels with none (app/sports_feed.py)."""
    from .. import output_feed, sports_feed
    data = request.get_json(silent=True) or {}
    if not isinstance(data.get('enabled'), bool):
        return jsonify({'error': 'enabled must be true or false.'}), 400
    try:
        team_id = int(data.get('team_id') or 0)
    except (TypeError, ValueError):
        return jsonify({'error': 'team_id must be a number.'}), 400
    if team_id < 0:
        return jsonify({'error': 'team_id must be a number.'}), 400
    before = None if data['enabled'] else _sports_served()
    try:
        if team_id:
            done = sports_feed.set_team_channel(league_id, team_id, data['enabled'])
        else:
            done = sports_feed.set_league_channels(league_id, data['enabled'], output_feed.guide_horizon())
    except sports_feed.NotFollowed as exc:
        return jsonify({'error': str(exc)}), 409
    if done is None:
        return jsonify({'error': 'That league no longer exists.'}), 404
    return jsonify({'success': True, 'cut': _cut_unserved(before)})


@sports_bp.route('/api/sports/up-next', methods=['PUT'])
def set_up_next():
    """Turn an Up next channel on the output feed on or off: a league's (`league_id`), or the one
    for everything followed with `league_id` 0 (app/sports_up_next.py)."""
    from .. import sports_up_next
    data = request.get_json(silent=True) or {}
    if not isinstance(data.get('enabled'), bool):
        return jsonify({'error': 'enabled must be true or false.'}), 400
    try:
        league_id = int(data.get('league_id') or 0)
    except (TypeError, ValueError):
        return jsonify({'error': 'league_id must be a number.'}), 400
    if league_id < 0:
        return jsonify({'error': 'league_id must be a number.'}), 400
    try:
        done = sports_up_next.switch(league_id, data['enabled'])
    except sports_up_next.NotFollowed as exc:
        return jsonify({'error': str(exc)}), 409
    if done is None:
        return jsonify({'error': 'That league no longer exists.'}), 404
    return jsonify({'success': True, **done})


def _sports_served():
    """Before a person's act that can stop a sports channel being served: what is served now
    (output_feed.sports_served_now; None and no query while no sports channel is being played)."""
    from .. import output_feed
    return output_feed.sports_served_now()


def _cut_unserved(before) -> int:
    """After that act has committed: end the plays of every sports channel the act stopped being
    served (output_feed.stop_unserved_sports)."""
    from .. import output_feed
    return output_feed.stop_unserved_sports(before)


@sports_bp.route('/api/sports/events/<int:event_id>/live-now', methods=['PUT'])
def set_live_now(event_id):
    """Start broadcast or Stop broadcast for one game (app/sports_feed.py::set_live_now,
    dev/changelog/1530, 1644).
    The page names the game and nothing else: which channels play it and until when are worked
    out here, from what is stored now."""
    from datetime import datetime
    from .. import output_feed, sports_feed
    from ..channel_groups import blocked_account_ids_now
    from ..tz_utils import get_display_tz, is_24h
    data = request.get_json(silent=True) or {}
    if not isinstance(data.get('on'), bool):
        return jsonify({'error': 'Say whether to start or stop the broadcast.'}), 400
    fcfg = output_feed.feed_config()
    _before, after = output_feed.sports_window(fcfg)
    now = datetime.utcnow()
    try:
        done = sports_feed.set_live_now(event_id, data['on'], after, now=now, blocked=blocked_account_ids_now(),
                                        allowed=output_feed.allowed_account_ids(fcfg))
    except sports_feed.LiveNowRefused as exc:
        return jsonify({'error': str(exc)}), 409
    if done is None:
        return jsonify({'error': 'That event is no longer in the schedule.'}), 404
    if done['on']:
        log.info('Sports channels: broadcast started early for %s (event %d) on %s, until %s UTC',
                 done['game'], event_id, join_labels(done['channels']), done['until'])
        # A play on a waiting screen moves to the game now, not at its next re-check.
        output_feed.recheck_waiting()
        message = (f'Broadcasting {done["game"]} early, until '
                   f'{sports_feed.at_clause(done["until"], now, get_display_tz(), is_24h())}.')
    else:
        if done['was_on']:
            log.info('Sports channels: early broadcast stopped for %s (event %d)', done['game'], event_id)
        message = f'Stopped the early broadcast of {done["game"]}.'
    return jsonify({'success': True, 'on': done['on'], 'message': message})


@sports_bp.route('/api/sports/leagues/<int:league_id>', methods=['DELETE'])
def delete_league(league_id):
    before = _sports_served()
    league = db.session.get(SportsLeague, league_id)
    tsdb_id = league.tsdb_id if league is not None else None
    try:
        deleted = sports_data.delete_league(league_id)
    except sports_data.LeagueInUse as exc:
        return jsonify({'error': str(exc)}), 409
    if not deleted:
        return jsonify({'error': 'That league no longer exists.'}), 404
    # Its Game time settings and skip words go with it; Stop following keeps them (app/sports_times.py,
    # app/sports_skips.py).
    for what, forget in (('game time settings', sports_times.forget_league),
                         ('skip words and lengths', sports_skips.forget_league)):
        try:
            if tsdb_id is not None and forget(tsdb_id):
                log.info('Sports: league %s deleted, and its %s with it', tsdb_id, what)
        except OSError as exc:
            # The league is gone either way; left behind, its answers only name a league nobody follows.
            log.warning('Sports: league %s deleted, but its %s could not be removed from config.yaml: %s',
                        tsdb_id, what, exc)
    sports_data.ensure_worker()
    return jsonify({'success': True, 'cut': _cut_unserved(before)})


# ---------------------------------------------------------------------------
# Which time a game goes by (app/sports_times.py, dev/changelog/1630)
# ---------------------------------------------------------------------------

def _game_time_changes(data: dict, league: bool) -> dict:
    """The submitted answers as sports_times takes them: an empty choice or box on a league page
    is None, its default again. A number that is not a whole number is left as sent, for
    sports_times.problem() to name."""
    out = {}
    for key, value in data.items():
        if league and value in ('', None):
            out[key] = None
        elif key in ('cover_both_minutes', 'wait_hours') and isinstance(value, str) and value.strip().isdecimal():
            out[key] = int(value.strip())
        else:
            out[key] = value
    return out


@sports_bp.route('/api/sports/leagues/<int:league_id>/game-times', methods=['PUT'])
def set_league_game_times(league_id):
    """One league's own answers to which time a game goes by; an empty one uses the default."""
    league = db.session.get(SportsLeague, league_id)
    if league is None:
        return jsonify({'error': 'That league no longer exists.'}), 404
    data = request.get_json(silent=True) or {}
    try:
        rule = sports_times.set_league(league.tsdb_id, _game_time_changes(data, league=True))
    except ValueError as exc:
        return jsonify({'error': str(exc)}), 400
    log.info('Sports: %s game time settings %s', league.name, rule.own or 'back to the default')
    return jsonify({'success': True, 'go_by': rule.go_by, 'own': rule.own})


@sports_bp.route('/api/sports/game-times', methods=['PUT'])
def set_default_game_times():
    """The default every league goes by unless its own page says otherwise."""
    data = request.get_json(silent=True) or {}
    try:
        rule = sports_times.set_default(_game_time_changes(data, league=False))
    except ValueError as exc:
        return jsonify({'error': str(exc)}), 400
    log.info('Sports: default game time settings: go by %s, cover both up to %d minutes, from %d hours before',
             rule.go_by, rule.cover_both_minutes, rule.wait_hours)
    return jsonify({'success': True, 'go_by': rule.go_by})


# ---------------------------------------------------------------------------
# The words and lengths that keep a listing out (app/sports_skips.py, dev/changelog/1634)
# ---------------------------------------------------------------------------

@sports_bp.route('/api/sports/leagues/<int:league_id>/skip-words', methods=['PUT'])
def set_skip_words(league_id):
    """Add one word to a league's list (`add`) or take one off (`remove`), on the list the page shows
    (`kind`)."""
    league = db.session.get(SportsLeague, league_id)
    if league is None:
        return jsonify({'error': 'That league no longer exists.'}), 404
    data = request.get_json(silent=True) or {}
    # The list the page shows, which it names (sports_view.skips_context).
    kind = data.get('kind')
    if kind not in sports_skips.KINDS:
        return jsonify({'error': 'Say which list: game or race.'}), 400
    try:
        if isinstance(data.get('add'), str) and 'remove' not in data:
            _skips, word = sports_skips.add_word(league.tsdb_id, kind, data['add'])
            log.info('Sports: %s skips listings that say "%s"', league.name, word)
        elif isinstance(data.get('remove'), str) and 'add' not in data:
            sports_skips.remove_word(league.tsdb_id, kind, data['remove'])
            word = sports_skips.clean_word(data['remove'])
            log.info('Sports: %s no longer skips listings that say "%s"', league.name, word)
        else:
            return jsonify({'error': 'Send one word to add or to remove.'}), 400
    except ValueError as exc:
        return jsonify({'error': str(exc)}), 400
    return jsonify({'success': True, 'word': word})


@sports_bp.route('/api/sports/leagues/<int:league_id>/skip-words', methods=['DELETE'])
def restore_skip_words(league_id):
    """Restore defaults: the built-in words and lengths for this league."""
    league = db.session.get(SportsLeague, league_id)
    if league is None:
        return jsonify({'error': 'That league no longer exists.'}), 404
    sports_skips.restore(league.tsdb_id)
    log.info('Sports: %s skip words and lengths restored to the defaults', league.name)
    return jsonify({'success': True})


@sports_bp.route('/api/sports/leagues/<int:league_id>/skip-minutes', methods=['PUT'])
def set_skip_minutes(league_id):
    """A league's own shortest listing of one kind; empty is the built-in again."""
    league = db.session.get(SportsLeague, league_id)
    if league is None:
        return jsonify({'error': 'That league no longer exists.'}), 404
    data = request.get_json(silent=True) or {}
    value = data.get('minutes')
    if value == '':
        value = None
    elif isinstance(value, str) and value.strip().isdecimal():
        value = int(value.strip())
    try:
        skips = sports_skips.set_minutes(league.tsdb_id, data.get('length'), value)
    except ValueError as exc:
        return jsonify({'error': str(exc)}), 400
    log.info('Sports: %s shortest %s listing is %d minutes', league.name, data.get('length'),
             skips.minutes(data.get('length')))
    return jsonify({'success': True, 'minutes': skips.minutes(data.get('length'))})


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
    names = sports_match.name_rows(event_id)
    for row in [c for g in groups for c in g['channels']] + names:
        row['url'] = url_for('channels.channel_detail', channel_id=row['channel_id'])
        row['account']['url'] = url_for('accounts.account_detail', account_id=row['account']['id'])
    league = db.session.get(SportsLeague, event.league_id)
    return jsonify({'success': True, 'groups': groups, 'names': names,
                    'skipped': sports_match.skipped_listings(event_id, get_display_tz(), is_24h(),
                                                             one=sports_view.event_noun(league, event)),
                    'league': league.name if league is not None else '',
                    'matchup': bool(event.home_team_id or event.away_team_id),
                    'one': sports_view.event_noun(league, event),
                    'listings': sum(1 for g in groups for c in g['channels'] if not c['wrong']),
                    'channels': len({c['channel_id'] for g in groups for c in g['channels'] if not c['wrong']}),
                    'tsdb_url': f'https://www.thesportsdb.com/event/{event.tsdb_id}'})


@sports_bp.route('/api/sports/events/<int:event_id>/record')
def event_record(event_id):
    """What a league page's Record opens the record dialog on for this game (dev/changelog/1539):
    the listing a rule for the league would pick, a replay, or an event channel, each with the
    dialog's notice saying which and why. Reads only; the save is the ordinary create path."""
    payload, status = sports_view.record_offer(event_id)
    return jsonify(payload), status


@sports_bp.route('/api/sports/events/<int:event_id>/channel-now')
def event_channel_now(event_id):
    """Channel it would use (dev/changelog/1639): the channel a recording of this game would start
    on if it started now, and the one its sports channel would play, each with why. Reads only."""
    payload, status = sports_view.channel_now(event_id)
    return jsonify(payload), status


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
    wrong = data.get('wrong', True)
    if not isinstance(wrong, bool):
        # A string "false" is truthy, and would mark the listings wrong when the caller meant to undo.
        return jsonify({'error': 'wrong must be true or false.'}), 400
    changed = sports_match.mark_wrong(event_id, ids, wrong)
    if changed is None:
        return jsonify({'error': 'That event is no longer stored.'}), 404
    return jsonify({'success': True, 'changed': changed})


@sports_bp.route('/api/sports/teams/<int:tsdb_id>/names', methods=['PUT'])
def set_team_names(tsdb_id):
    """The names a person adds for one team, beside TheSportsDB's own (design 6.2)."""
    data = request.get_json(silent=True) or {}
    names = data.get('names')
    if not isinstance(names, list) or not all(isinstance(n, str) for n in names):
        return jsonify({'error': 'names must be a list of names.'}), 400
    try:
        saved = sports_match.set_team_names(tsdb_id, data['names'])
    except ValueError as exc:
        return jsonify({'error': str(exc)}), 400
    if saved is None:
        return jsonify({'error': 'Team not found.'}), 404
    return jsonify({'success': True, 'names': saved})


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
    added = sports_match.change_categories(league_id, add, remove, replay)
    if added is None:
        return jsonify({'error': 'That league no longer exists.'}), 404
    # What was newly tied, and which of those arrived with Replays only already on, so the page
    # can say what it switched on by itself.
    return jsonify({'success': True, 'added': [name for name, _replay in added],
                    'replay_on': [name for name, is_replay in added if is_replay]})
