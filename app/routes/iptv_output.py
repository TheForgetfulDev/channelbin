"""The IPTV Output page and its actions (app/output_feed_view.py, mockup 53,
dev/changelog/1526).

A logged-in blueprint, unlike app/routes/output_feed.py: that one answers an IPTV player on
the player's own login, this one is ChannelBin's own page about it. The output's switch and
its settings rows save through `/api/settings/field`, the one validator of those keys; what
is here are the actions that route has no shape for.
"""
import logging

from flask import Blueprint, current_app, jsonify, render_template, request

from .. import db, output_feed, output_feed_view
from ..config import config_write_lock, load_config, load_for_edit, save_config, set_nested
from ..database import Account
from ..db_utils import retry_on_locked
from ..fmt_utils import counted

log = logging.getLogger(__name__)

iptv_output_bp = Blueprint('iptv_output', __name__)

@iptv_output_bp.route('/iptv-output')
def page():
    cfg = load_config()
    proxy = output_feed_view.proxy_state(cfg, request.headers,
                                         bool(current_app.config.get('BEHIND_PROXY')))
    return render_template('iptv_output.html',
                           **output_feed_view.page_context(cfg, request.host_url,
                                                           request.args, proxy))


@iptv_output_bp.route('/api/iptv-output/rows/<int:feed_id>', methods=['POST'])
def set_row(feed_id):
    """Take one channel or group off the output, or put it back. A play of a row that went
    off is ended by name, after the write and never inside it."""
    data = request.get_json(silent=True) or {}
    if not isinstance(data.get('on'), bool):
        return jsonify({'error': 'Say whether the channel is on the output'}), 400
    on = data['on']
    kind, row_id = output_feed.decode_feed_id(feed_id)
    if kind == output_feed.KIND_SPORTS:
        return jsonify({'error': 'A sports channel is turned on and off on its league\'s page '
                                 'in Sports Schedules'}), 400

    @retry_on_locked()
    def _switch_and_commit():
        row, moved = output_feed.set_on_output(kind, row_id, on)
        if row is None:
            return None, False
        db.session.commit()
        return row.name, moved

    name, moved = _switch_and_commit()
    if name is None:
        return jsonify({'error': 'That channel is not in the TV Guide any more'}), 404
    cut = 0 if on else output_feed.stop_feed_id(feed_id, output_feed.REASON_TAKEN_OFF)
    if moved:
        log.info('IPTV output: %s %s (feed id %d)%s', name,
                 'put back on' if on else 'taken off', feed_id,
                 f', {counted(cut, "play")} cut' if cut else '')
    return jsonify({'success': True, 'name': name, 'on': on, 'cut': cut})


@iptv_output_bp.route('/api/iptv-output/plays/<session_id>/stop', methods=['POST'])
def stop_play(session_id):
    if not output_feed.stop_session(session_id):
        return jsonify({'error': 'That play has already ended'}), 404
    return jsonify({'success': True})


@iptv_output_bp.route('/api/iptv-output/accounts/<int:account_id>', methods=['POST'])
def set_account(account_id):
    """Whether the output may play from one account (`allow`), or how many of its
    connections recordings leave alone (`held`). Both are config keys the Settings page
    holds too, written under the same lock."""
    data = request.get_json(silent=True) or {}
    # Read before the config lock, so the lock is held no longer than the save needs.
    accounts = {a.id: a.name for a in Account.query.all()}
    if account_id not in accounts:
        return jsonify({'error': 'There is no such account'}), 404
    name = accounts[account_id]

    if 'allow' in data:
        if not isinstance(data['allow'], bool):
            return jsonify({'error': 'Say whether the account is allowed'}), 400
        allow = data['allow']
        with config_write_lock:
            cfg, file_cfg = load_for_edit()
            stored = output_feed.allowed_account_ids(output_feed.feed_config(cfg))
            # An empty list means every account, including one added later.
            allowed = set(accounts) if stored is None else set(stored) & set(accounts)
            if allow:
                allowed.add(account_id)
            else:
                allowed.discard(account_id)
            if not allowed:
                return jsonify({'error': 'The IPTV output needs at least one account to play '
                                         'from. To stop it altogether, turn the output off.'}), 400
            value = [] if allowed == set(accounts) else sorted(allowed)
            set_nested(file_cfg, 'integrations.output_feed.accounts', value)
            save_config(file_cfg)
        cut = 0 if allow else output_feed.stop_account(account_id)
        log.info('IPTV output: account %d (%s) %s%s', account_id, name,
                 'allowed' if allow else 'switched off',
                 f', {counted(cut, "play")} cut' if cut else '')
        return jsonify({'success': True, 'name': name, 'allow': allow, 'cut': cut})

    if 'held' in data:
        try:
            held = int(data['held'])
        except (TypeError, ValueError):
            return jsonify({'error': 'The number of connections must be a whole number'}), 400
        if held < 0:
            return jsonify({'error': 'The number of connections cannot be below zero'}), 400
        with config_write_lock:
            cfg, file_cfg = load_for_edit()
            seats = {str(a): n for a, n in
                     output_feed.held_seats_map(output_feed.feed_config(cfg)).items()}
            seats.pop(str(account_id), None)
            if held:
                seats[str(account_id)] = held
            problem = output_feed.held_seats_problem(seats, cfg)
            if problem:
                return jsonify({'error': problem}), 400
            set_nested(file_cfg, 'integrations.output_feed.held_seats', seats)
            save_config(file_cfg)
        return jsonify({'success': True, 'name': name, 'held': held})

    return jsonify({'error': 'Nothing to change'}), 400


@iptv_output_bp.route('/api/iptv-output/login', methods=['POST'])
def set_login():
    """The username and password an IPTV player logs in with, saved together. A blank
    password keeps the stored one, since the page never holds it to send back. Setting a
    login never turns the output on."""
    data = request.get_json(silent=True) or {}
    username = str(data.get('username') or '').strip()
    password = str(data.get('password') or '')
    with config_write_lock:
        cfg, file_cfg = load_for_edit()
        fcfg = output_feed.feed_config(cfg)
        had = output_feed.credentials_set(fcfg)
        if not password:
            password = output_feed.feed_password(fcfg)
        if not username or not password:
            return jsonify({'error': 'Both a username and a password are needed.'}), 400
        problem = (output_feed.login_problem('username', username)
                   or output_feed.login_problem('password', password))
        if problem:
            return jsonify({'error': problem}), 400
        set_nested(file_cfg, 'integrations.output_feed.username', username)
        set_nested(file_cfg, 'integrations.output_feed.password', password)
        if not had:
            # A switch left on from before there was a login (a hand-edited config, a login
            # cleared in the file) would start serving the moment the login is saved.
            set_nested(file_cfg, 'integrations.output_feed.enabled', False)
        save_config(file_cfg)
    log.info('IPTV output: login %s', 'changed' if had else 'set')
    return jsonify({'success': True, 'first': not had})
