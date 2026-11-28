"""The IPTV Output page and its actions (app/output_feed_view.py, mockup 53,
dev/changelog/1526).

A logged-in blueprint, unlike app/routes/output_feed.py: that one answers an IPTV player on
the player's own login, this one is ChannelBin's own page about it. The output's switch and
its settings rows save through `/api/settings/field`, the one validator of those keys; what
is here are the actions that route has no shape for.
"""
import json
import logging

from flask import Blueprint, current_app, jsonify, render_template, request
from sqlalchemy.exc import IntegrityError

from .. import db, output_feed, output_feed_view, pip_channels, proxy_trust
from ..auth import lockout_key
from ..config import config_write_lock, load_config, load_for_edit, save_config, set_nested
from ..database import Account, OutputFeedLogin
from ..db_utils import retry_on_locked
from ..fmt_utils import counted

log = logging.getLogger(__name__)

iptv_output_bp = Blueprint('iptv_output', __name__)

@iptv_output_bp.route('/iptv-output')
def page():
    cfg = load_config()
    proxy = output_feed_view.proxy_state(cfg, request.headers,
                                         proxy_trust.wired(current_app).web,
                                         proxy_trust.is_trusted(request))
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
    if kind == output_feed.KIND_UP_NEXT:
        from .. import sports_up_next
        league_id = sports_up_next.league_of(row_id)
        try:
            done = sports_up_next.switch(league_id, on) if league_id is not None else None
        except sports_up_next.NotFollowed as exc:
            return jsonify({'error': str(exc)}), 409
        if done is None:
            return jsonify({'error': 'That Up next channel no longer exists'}), 404
        return jsonify({'success': True, 'name': done['name'], 'on': on, 'cut': done['cut']})
    if kind == output_feed.KIND_SPORTS:
        return jsonify({'error': 'A sports channel is turned on and off on its league\'s page '
                                 'in Sports Schedules'}), 400
    if kind == output_feed.KIND_PIP and output_feed.pip_swapped(feed_id):
        return jsonify({'error': 'A swapped copy follows its channel. Turn it off with Swapped copy, under '
                                 'Picture-in-picture channels'}), 400

    @retry_on_locked()
    def _switch_and_commit():
        row, moved = output_feed.set_on_output(kind, row_id, on)
        if row is None:
            return None, False
        db.session.commit()
        return row.name, moved

    name, moved = _switch_and_commit()
    if name is None:
        return jsonify({'error': 'That channel no longer exists' if kind == output_feed.KIND_PIP
                        else 'That channel is not in the TV Guide any more'}), 404
    if on:
        cut = 0
    elif kind == output_feed.KIND_PIP:
        cut = output_feed.stop_pip(row_id)      # its swapped copy goes with it
    else:
        cut = output_feed.stop_feed_id(feed_id, output_feed.REASON_TAKEN_OFF)
    if moved:
        log.info('IPTV output: %s %s (feed id %d)%s', name,
                 'put back on' if on else 'taken off', feed_id,
                 f', {counted(cut, "play")} cut' if cut else '')
    return jsonify({'success': True, 'name': name, 'on': on, 'cut': cut})


def _pip_numbers(row_id: int) -> dict:
    """The channel numbers a picture-in-picture channel and its swapped copy have right now, 0
    for one that is not on the output."""
    nums = {r.feed_id: r.num for r in output_feed.catalog(load_config())}
    return {'num': nums.get(output_feed.feed_id_for_pip(row_id), 0),
            'swapped_num': nums.get(output_feed.feed_id_for_pip(row_id, True), 0)}


def _pip_gone():
    return jsonify({'error': 'That picture-in-picture channel no longer exists'}), 404


@iptv_output_bp.route('/api/iptv-output/pip/pickers')
def pip_pickers():
    """What the setup dialog's pickers list: the coming games and the TV Guide's rows."""
    return jsonify({'success': True, **pip_channels.picker_data(load_config())})


@iptv_output_bp.route('/api/iptv-output/pip/would-play', methods=['POST'])
def pip_would_play():
    """What the two pictures of the draft in the setup dialog would start from right now, and
    the connections that takes. A read: nothing is saved and no connection is taken."""
    data = request.get_json(silent=True) or {}
    if not isinstance(data, dict):
        data = {}
    main, inset = (pip_channels.clean_draft_source(data.get(k)) for k in ('main', 'inset'))
    return jsonify({'success': True, **pip_channels.would_play(main, inset, load_config())})


@iptv_output_bp.route('/api/iptv-output/pip', methods=['POST'])
def pip_create():
    """Create a picture-in-picture channel. Nothing is on the output until this."""
    cfg = load_config()
    try:
        spec = pip_channels.clean(request.get_json(silent=True) or {})
        pip_channels.check_sources(spec, cfg)
    except pip_channels.PipRefused as exc:
        return jsonify({'error': exc.message}), exc.status
    row = pip_channels.create(spec)
    return jsonify({'success': True, 'id': row.id, 'name': row.name, **_pip_numbers(row.id)})


@iptv_output_bp.route('/api/iptv-output/pip/<int:row_id>', methods=['POST'])
def pip_change(row_id):
    """Save a picture-in-picture channel's name, pictures and layout. A play on its card asks
    again at once; one on its two pictures changes in place (`changing`); one on its big
    picture alone keeps it until it tunes in again (`kept`)."""
    cfg = load_config()
    try:
        spec = pip_channels.clean(request.get_json(silent=True) or {})
        pip_channels.check_sources(spec, cfg)
    except pip_channels.PipRefused as exc:
        return jsonify({'error': exc.message}), exc.status
    row = pip_channels.change(row_id, spec)
    if row is None:
        return _pip_gone()
    cut = 0 if row.swapped_copy else output_feed.stop_pip(row_id, swapped_only=True)
    _asked, kept, changing = output_feed.refresh_pip(row_id)
    return jsonify({'success': True, 'id': row.id, 'name': row.name, 'kept': kept, 'changing': changing,
                    'cut': cut, **_pip_numbers(row.id)})


@iptv_output_bp.route('/api/iptv-output/pip/<int:row_id>/swap', methods=['POST'])
def pip_swap(row_id):
    try:
        row = pip_channels.swap(row_id)
    except pip_channels.PipRefused as exc:
        return jsonify({'error': exc.message}), exc.status
    if row is None:
        return _pip_gone()
    _asked, kept, changing = output_feed.refresh_pip(row_id)
    return jsonify({'success': True, 'name': row.name, 'kept': kept, 'changing': changing})


@iptv_output_bp.route('/api/iptv-output/pip/<int:row_id>/clear', methods=['POST'])
def pip_clear(row_id):
    """Take both pictures off. Its swapped copy leaves the output, so a play of that ends by
    name; a play of the channel itself goes to the card saying nothing is set up."""
    row = pip_channels.clear(row_id)
    if row is None:
        return _pip_gone()
    cut = output_feed.stop_pip(row_id, swapped_only=True)
    moved = output_feed.clear_pip(row_id)
    return jsonify({'success': True, 'name': row.name, 'cut': cut, 'moved': moved})


@iptv_output_bp.route('/api/iptv-output/pip/<int:row_id>/swapped-copy', methods=['POST'])
def pip_swapped_copy(row_id):
    data = request.get_json(silent=True) or {}
    if not isinstance(data.get('on'), bool):
        return jsonify({'error': 'Say whether the swapped copy is offered'}), 400
    try:
        row = pip_channels.set_swapped_copy(row_id, data['on'])
    except pip_channels.PipRefused as exc:
        return jsonify({'error': exc.message}), exc.status
    if row is None:
        return _pip_gone()
    cut = 0 if data['on'] else output_feed.stop_pip(row_id, swapped_only=True)
    return jsonify({'success': True, 'name': row.name, 'on': data['on'], 'cut': cut, **_pip_numbers(row.id)})


@iptv_output_bp.route('/api/iptv-output/pip/<int:row_id>', methods=['DELETE'])
def pip_remove(row_id):
    """Remove a picture-in-picture channel from the output: the row is deleted and its plays
    end by name, after the write and never inside it."""
    name = pip_channels.remove(row_id)
    if name is None:
        return _pip_gone()
    cut = output_feed.stop_pip(row_id)
    return jsonify({'success': True, 'name': name, 'cut': cut})


@iptv_output_bp.route('/api/iptv-output/lockouts/clear', methods=['POST'])
def clear_lockout():
    """Let one address locked out of the IPTV output after wrong logins try again now, and
    forget how many times it has been locked out. One address a call, never every one
    (output_feed.login_failures, dev/changelog/1774)."""
    address = (request.get_json(silent=True) or {}).get('address')
    if not isinstance(address, str) or not address.strip():
        return jsonify({'error': 'Say which address to let back in'}), 400
    address = address.strip()
    cleared = output_feed.login_failures.clear(address)
    if cleared:
        log.info('IPTV output: the wrong-login lockout of %s was cleared from the IPTV Output '
                 'page, with its count of past lockouts', lockout_key(address))
    return jsonify({'success': True, 'cleared': cleared})


@iptv_output_bp.route('/api/iptv-output/plays/<session_id>/stop', methods=['POST'])
def stop_play(session_id):
    if not output_feed.stop_session(session_id):
        return jsonify({'error': 'That play has already ended'}), 404
    return jsonify({'success': True})


@iptv_output_bp.route('/api/iptv-output/plays/<session_id>/move', methods=['POST'])
def move_play(session_id):
    """Watching now's "Move to another channel", on the dashboard and this page
    (output_feed.move_by_hand, dev/changelog/1675). `message` is the sentence both pages show."""
    try:
        done = output_feed.move_by_hand(session_id)
    except output_feed.FeedRefused as exc:
        return jsonify({'error': exc.message}), exc.status
    left = f'"{done.from_name}"'
    if done.outcome == output_feed.MOVE_ENDED:
        session = output_feed.get_session(session_id)
        why = ' '.join(x for x in (session.reason_text(), session.detail) if x) if session else ''
        return jsonify({'error': f'Nothing else would start, so the play ended. {why}'.strip()}), 409
    if done.outcome == output_feed.MOVE_TO_CARD:
        return jsonify({'error': 'Nothing else would start, so it is showing its card. It tries again by itself.'}), 409
    if done.outcome == output_feed.MOVE_ADJUSTED:
        said = (f' {left} now has a score adjustment of {output_feed.HAND_MOVE_ADJUSTMENT}, so it '
                f'is tried last, for a play or a recording, until you set it back on its channel page.')
    elif done.outcome == output_feed.MOVE_ALREADY:
        said = f' {left} already had a score adjustment of {output_feed.HAND_MOVE_ADJUSTMENT}.'
    elif done.outcome == output_feed.MOVE_CHANNEL_GONE:
        said = f' {left} is no longer in your channels.'
    elif done.outcome == output_feed.MOVE_UNSAVED:
        said = (f' The score adjustment on {left} could not be saved, so it may be picked again: '
                f'{done.error}')
    else:
        raise ValueError(f'Unknown move outcome "{done.outcome}"')
    log.info('IPTV output: play %s moved by hand from %s to "%s"', session_id, left,
             done.to_name)
    return jsonify({'success': True, 'moved_to': done.to_name, 'left': done.from_name,
                    'left_channel_id': done.from_id,
                    'adjusted': done.outcome == output_feed.MOVE_ADJUSTED,
                    'warning': done.outcome == output_feed.MOVE_UNSAVED,
                    'message': f'Moved to "{done.to_name}".{said}'})


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


# ── Logins (dev/changelog/1760, 1781) ────────────────────────────────────────────────────────

#: A username another save took between this one's check and its commit (the column is unique).
_TAKEN = 'That username was just taken by another IPTV output login. Pick another one.'
#: The longest name a login may carry: who it is for, shown only on this page.
LOGIN_NAME_MAX = 100


def _login_fields(data: dict, login_id=None) -> tuple:
    """(fields to write, error) from a create or edit body. Only the keys present are read,
    so an edit changes what it names and nothing else."""
    out = {}
    if 'name' in data:
        name = str(data.get('name') or '').strip()
        if not name:
            return None, 'Give the login a name, so you can tell who it is for.'
        if len(name) > LOGIN_NAME_MAX:
            return None, f'The name can be at most {LOGIN_NAME_MAX} characters.'
        out['name'] = name
    if 'username' in data:
        username = str(data.get('username') or '').strip()
        if not username:
            return None, 'A username is needed.'
        problem = output_feed.login_problem('username', username)
        if problem:
            return None, problem
        if output_feed.login_taken(username, except_login_id=login_id):
            return None, f'"{username}" is already in use by another IPTV output login. Pick another one.'
        out['username'] = username
    if 'password' in data:
        password = str(data.get('password') or '')
        if not password:
            return None, 'A password is needed.'
        problem = output_feed.login_problem('password', password)
        if problem:
            return None, problem
        out['password'] = password
    if 'max_streams' in data:
        problem = output_feed.max_streams_problem(data.get('max_streams'))
        if problem:
            return None, problem
        out['max_streams'] = int(data.get('max_streams'))
    for key in ('enabled', 'provider_direct', 'full_refusals'):
        if key in data:
            if not isinstance(data.get(key), bool):
                return None, 'The switch takes true or false.'
            out[key] = data[key]
    if 'lineup' in data:
        ids, problem = output_feed.lineup_problem(data.get('lineup'))
        if problem:
            return None, problem
        out['lineup'] = None if ids is None else json.dumps(ids)
    if 'accounts' in data:
        existing = {a.id for a in Account.query.with_entities(Account.id)}
        ids, problem = output_feed.accounts_problem(data.get('accounts'), existing)
        if problem:
            return None, problem
        out['accounts'] = None if ids is None else json.dumps(ids)
    return out, ''


@iptv_output_bp.route('/api/iptv-output/logins', methods=['POST'])
def create_login():
    """A new login. A blank password is generated. Both of its home-only switches start off
    (dev/changelog/1781). The first login never turns the output on: a switch left on from before
    there was a login (a hand-edited config, the last login deleted) is turned off, so the output
    does not start serving the moment a login exists."""
    data = request.get_json(silent=True) or {}
    if not str(data.get('password') or ''):
        data['password'] = output_feed.generated_password()
    for key in ('name', 'username'):
        data.setdefault(key, '')
    data.setdefault('max_streams', 1)
    fields, error = _login_fields(data)
    if error:
        return jsonify({'error': error}), 400
    first = not output_feed.has_login()

    @retry_on_locked()
    def _create():
        row = OutputFeedLogin(**fields)
        db.session.add(row)
        db.session.commit()
        return row.id

    try:
        login_id = _create()
    except IntegrityError:
        db.session.rollback()
        return jsonify({'error': _TAKEN}), 409
    log.info('IPTV output: login %s added', fields['username'])
    if first:
        with config_write_lock:
            cfg, file_cfg = load_for_edit()
            if output_feed.is_enabled(output_feed.feed_config(cfg)):
                set_nested(file_cfg, 'integrations.output_feed.enabled', False)
                save_config(file_cfg)
                log.info('IPTV output: turned off, so the first login does not start it serving')
    return jsonify({'success': True, 'id': login_id, 'first': first})


@iptv_output_bp.route('/api/iptv-output/logins/<int:login_id>', methods=['POST'])
def edit_login(login_id):
    """Change a login. Turning it off or changing its username or password ends the plays on
    it, after the commit: an IPTV player holding the old pair is not that login any more. A new
    channel list ends its plays of the channels the list leaves out, and new accounts its plays
    from outside them, after the commit too."""
    data = request.get_json(silent=True) or {}
    fields, error = _login_fields(data, login_id=login_id)
    if error:
        return jsonify({'error': error}), 400
    if not fields:
        return jsonify({'error': 'Nothing to change'}), 400

    @retry_on_locked()
    def _edit():
        row = db.session.get(OutputFeedLogin, login_id)
        if row is None:
            return None
        cut = ((fields.get('enabled') is False and row.enabled)
               or ('username' in fields and fields['username'] != row.username)
               or ('password' in fields and fields['password'] != row.password))
        before, accounts_before = output_feed.lineup_of(row), output_feed.accounts_of(row)
        for key, value in fields.items():
            setattr(row, key, value)
        db.session.commit()
        after, accounts_after = output_feed.lineup_of(row), output_feed.accounts_of(row)
        return (cut, (after if after != before else _UNCHANGED),
                (accounts_after if accounts_after != accounts_before else _UNCHANGED), row.username)

    try:
        done = _edit()
    except IntegrityError:
        db.session.rollback()
        return jsonify({'error': _TAKEN}), 409
    if done is None:
        return jsonify({'error': 'That login no longer exists'}), 404
    cut, lineup, accounts, username = done
    ended = output_feed.stop_login(login_id) if cut else 0
    if lineup is not _UNCHANGED:
        # After the commit: the plays its new channels leave out end by name (dev/changelog/1764).
        ended += output_feed.set_login_lineup(login_id, lineup)
        log.info('IPTV output: login %s now gets %s', username,
                 'every channel on the output' if lineup is None else counted(len(lineup), 'channel'))
    if accounts is not _UNCHANGED:
        # After the commit: its plays take the new accounts, and those outside them end by name
        # (dev/changelog/1765).
        ended += output_feed.set_login_accounts(login_id, accounts)
        log.info('IPTV output: login %s now plays from %s', username,
                 "the IPTV output's accounts" if accounts is None
                 else f'its own {counted(len(accounts), "account")} ({", ".join(map(str, sorted(accounts)))})')
    log.info('IPTV output: login %d changed (%s)%s', login_id, ', '.join(sorted(fields)),
             f', {counted(ended, "play")} ended' if ended else '')
    return jsonify({'success': True, 'ended': ended})


#: What _edit() hands back for a lineup or accounts the save left as they were, since None means
#: everything (the lineup) or the output's list (the accounts).
_UNCHANGED = object()


@iptv_output_bp.route('/api/iptv-output/logins/<int:login_id>/channels')
def login_channels(login_id):
    """What the Choose channels dialog lists for one login: every row the output has, whether
    or not it is offered right now (a pick outlives a channel taken off for a while), with a
    picture-in-picture channel's swapped copy folded into its channel, and the login's picks
    (dev/changelog/1764). One catalog."""
    row = db.session.get(OutputFeedLogin, login_id)
    if row is None:
        return jsonify({'error': 'That login no longer exists'}), 404
    return jsonify({'success': True, 'name': row.name,
                    **output_feed_view.lineup_choices(load_config(), output_feed.lineup_of(row),
                                                      output_feed.accounts_of(row))})


@iptv_output_bp.route('/api/iptv-output/logins/<int:login_id>/accounts')
def login_accounts(login_id):
    """What the Choose accounts dialog lists for one login: every account, whether the output's
    own list has it, and the login's own picks, or none when it plays from the output's list
    (dev/changelog/1765)."""
    row = db.session.get(OutputFeedLogin, login_id)
    if row is None:
        return jsonify({'error': 'That login no longer exists'}), 404
    return jsonify({'success': True, 'name': row.name,
                    **output_feed_view.account_choices(load_config(), output_feed.accounts_of(row))})


@iptv_output_bp.route('/api/iptv-output/logins/<int:login_id>', methods=['DELETE'])
def delete_login(login_id):
    """Delete a login and end its plays. Its lines in Recent plays keep its username. Deleting
    the last one turns the output off, as adding the first one does: an output that is on with no
    login lets nobody in, and its page has no switch to turn it off (dev/changelog/1781)."""
    @retry_on_locked()
    def _delete():
        row = db.session.get(OutputFeedLogin, login_id)
        if row is None:
            return None
        username = row.username
        db.session.delete(row)
        db.session.commit()
        return username

    username = _delete()
    if username is None:
        return jsonify({'error': 'That login no longer exists'}), 404
    ended = output_feed.stop_login(login_id)
    log.info('IPTV output: login %s deleted%s', username,
             f', {counted(ended, "play")} ended' if ended else '')
    turned_off = False
    if not output_feed.has_login():
        with config_write_lock:
            cfg, file_cfg = load_for_edit()
            if output_feed.is_enabled(output_feed.feed_config(cfg)):
                set_nested(file_cfg, 'integrations.output_feed.enabled', False)
                save_config(file_cfg)
                turned_off = True
        if turned_off:
            log.info('IPTV output: turned off, since its last login was deleted')
    return jsonify({'success': True, 'ended': ended, 'last': not output_feed.has_login(),
                    'turned_off': turned_off})


@iptv_output_bp.route('/api/iptv-output/logins/<int:login_id>/password')
def reveal_login_password(login_id):
    """The password of one login, for the page's eye and Copy buttons: the page's source never
    holds it."""
    row = db.session.get(OutputFeedLogin, login_id)
    if row is None:
        return jsonify({'error': 'That login no longer exists'}), 404
    return jsonify({'success': True, 'password': row.password})
