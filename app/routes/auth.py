import logging
from datetime import datetime

from flask import (Blueprint, current_app, redirect, render_template, request,
                   session, url_for)
from werkzeug.security import check_password_hash

from ..auth import (MAX_FAILURES, clear_session, client_ip, current_lockout, gate_active,
                    check_lockout, lockout_key, lockout_words, is_authenticated, password_epoch,
                    record_failure, record_success, steps_words, validate_next)
from ..tz_utils import format_local

log = logging.getLogger(__name__)

auth_bp = Blueprint('auth', __name__)


@auth_bp.route('/login', methods=['GET', 'POST'])
def login():
    auth_cfg = current_app.config.get('AUTH') or {}
    if not gate_active(auth_cfg):
        # Nothing to log into - a direct visit here when the gate is off/inert has no
        # meaning, and a 404 tells a scanning client nothing (CLAUDE.md 'enforcement lives
        # server-side' cuts both ways: don't imply a feature exists that is not active).
        from flask import abort
        abort(404)

    # is_authenticated(), not a bare session['auth_ok']: an expired or
    # different-password session is *not* signed in, and bouncing it to '/' only to have
    # the gate bounce it straight back here made the user watch two redirects to reach the
    # page they already asked for (dev/changelog/487).
    if is_authenticated(auth_cfg):
        return redirect(url_for('dashboard.dashboard'))

    ip = client_ip()
    next_value = validate_next(request.values.get('next'))
    insecure = not request.is_secure

    if request.method == 'GET':
        unlock_at = check_lockout(ip)
        error = None
        if unlock_at:
            error = 'Too many failed attempts. Try again after %s.' % format_local(unlock_at)
        return render_template('login.html', error=error, next=next_value, insecure=insecure)

    unlock_at = check_lockout(ip)
    if unlock_at:
        error = 'Too many failed attempts. Try again after %s.' % format_local(unlock_at)
        return render_template('login.html', error=error, next=next_value,
                               insecure=insecure), 429

    password = request.form.get('password', '')
    stored_hash = auth_cfg.get('password_hash') or ''
    # Never distinguish "no password set" from "wrong password" in the response - a
    # generic failure either way (CLAUDE.md 'failure paths must be observable' is
    # satisfied by the server-side log line below, not by telling the caller more).
    ok = bool(stored_hash) and check_password_hash(stored_hash, password)

    if ok:
        record_success(ip)
        clear_session()
        session['auth_ok'] = True
        session['auth_at'] = datetime.utcnow().isoformat()
        # Binds this session to the password it was issued against, so a later password
        # change evicts it (app/auth.py::password_epoch).
        session['auth_pw'] = password_epoch(auth_cfg)
        session.permanent = True
        timeout = auth_cfg.get('session_timeout_minutes') or 0
        log.info('Successful login from %s (session good for %s)',
                ip, ('%dm' % timeout) if timeout else 'indefinitely')
        return redirect(next_value or url_for('dashboard.dashboard'))

    just_locked = record_failure(ip)
    log.warning('Failed login attempt from %s', ip)
    if just_locked:
        found = current_lockout(ip)
        unlock_at = found.until if found else datetime.utcnow()
        from ..alerts import create_alert
        create_alert(
            'AUTH_LOGIN_LOCKOUT',
            title='Login lockout: too many failed attempts',
            body=('IP %s was locked out after %d failed login attempts. Locked out '
                 'until %s (%s). Each lockout of one IP is longer than its last: %s. A right '
                 'password after a lockout ends, 24 hours with no new lockout, or a restart '
                 'starts it again at the first. Behind a reverse proxy, the IP is the visitor\'s own only when '
                 '"The web UI is behind a reverse proxy" is on in Settings (System) and the '
                 'proxy is in "Reverse proxy addresses" or that list is empty - otherwise '
                 'every visit through the proxy looks like one IP. An IPv6 address is counted '
                 'by its /64.'
                 % (lockout_key(ip), MAX_FAILURES, format_local(unlock_at),
                    lockout_words(found) if found else 'already over', steps_words())),
            source='auth',
        )
        error = 'Too many failed attempts. Try again after %s.' % format_local(unlock_at)
        status = 429
    else:
        error = 'Incorrect password.'
        status = 401
    return render_template('login.html', error=error, next=next_value,
                           insecure=insecure), status


@auth_bp.route('/logout', methods=['POST'])
def logout():
    clear_session()
    return redirect(url_for('auth.login'))
