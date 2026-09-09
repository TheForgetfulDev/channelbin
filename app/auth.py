"""
The optional password gate: one shared password, no usernames, no roles, no 2FA.

Per CLAUDE.md Product Principle 1, this is a door on something that was standing open
(POST /settings et al had zero auth), not a hardened auth system - it must say so rather
than imply otherwise (README.md, the Settings Security card, dev/docs/DESIGN.md).

Deny-by-default over an allowlist: ALWAYS_OPEN names the only endpoints reachable with no
session. Every other route - all ~167 of them, including any added after this file was
written - is gated by construction, not by a decorator someone has to remember to add.
"""
import hashlib
import hmac
import logging
import threading
from datetime import datetime, timedelta

from flask import current_app, g, jsonify, redirect, request, session, url_for

log = logging.getLogger(__name__)

# 'static' is exempt so the login page has CSS/images. Every file carrying user data
# (screenshots, thumbnails, the support bundle, mockups) is served by a route, not by
# the static file handler, so none of it is exempted here.
ALWAYS_OPEN = {'auth.login', 'auth.logout', 'static'}

# Blueprints that enforce their own independent authentication in their own
# before_request (e.g. routes/ha.py's X-API-Key gate for Home Assistant's polling
# requests, which has no browser to carry a session cookie). Bypassed here by blueprint
# name rather than by adding every one of its endpoints to ALWAYS_OPEN, so a route added
# to that blueprint later inherits the bypass automatically instead of silently also
# requiring a browser login on top of its own gate. Never add a blueprint here whose own
# gate is not deny-by-default - this bypass trusts it completely.
SELF_GATED_BLUEPRINTS = {'ha'}

# The source key for the standing "enabled but no password" alert. One constant, not the
# observing path's name, so startup and every settings save address the same row rather
# than each raising their own copy of the same condition.
GATE_INERT_SOURCE = 'auth-gate'

MAX_FAILURES = 5
LOCKOUT_WINDOW = timedelta(minutes=15)
# Hard ceiling on distinct IPs tracked at once. The tracker is keyed on an address the
# client can influence whenever flask.behind_proxy is on and the proxy forwards an
# attacker-supplied X-Forwarded-For unchanged (nginx's proxy_add_x_forwarded_for appends
# and is safe; passing $http_x_forwarded_for through is not), so without a ceiling one
# client could grow this dict without limit - on a box with no swap, that is the whole
# machine (dev/changelog/487). Over the cap, expired entries go first and the oldest
# survivors after that: lockout accuracy degrades under flood, memory does not.
MAX_TRACKED_IPS = 1024

# In-memory {ip: [failure datetimes]}. Reset on process restart, and only as meaningful
# as request.remote_addr - which is the single real client only when flask.behind_proxy
# is on (ProxyFix); otherwise every proxied request looks like one IP. This is a brute-
# force speed bump, not a durable audit log, so neither limitation is a defect here.
_failures = {}
_failures_lock = threading.Lock()


def wants_json(req) -> bool:
    """True when a denied request should get a 401 JSON body instead of an HTML redirect.

    Extracted from app/__init__.py's CSRF error handler (CLAUDE.md: search before you
    write) - that handler now imports this instead of carrying its own copy.
    """
    return (
        req.path.startswith('/api/')
        or req.is_json
        or 'application/json' in req.headers.get('Accept', '')
    )


def refresh_auth(app):
    """Resolve config['auth'] into app.config['AUTH'] - the only place the gate reads from.

    Never read via a runtime load_config() from inside the gate itself: that would read
    the real config.yaml under the test suite (make_test_app's extra_overrides are not
    visible to runtime load_config() calls - CLAUDE.md), so the moment auth is enabled for
    real, every test would start getting a 302. Called from the settings write paths
    whenever a changed leaf starts with 'auth.'; create_app() resolves the same dict from
    its own already-loaded cfg instead, so config_overrides survive (app/__init__.py).
    """
    from .config import load_config
    cfg = load_config()
    app.config['AUTH'] = dict(cfg.get('auth') or {})
    report_gate_state(app.config['AUTH'], source='settings')


def gate_active(auth_cfg: dict) -> bool:
    """'enabled' means the owner wants the gate; the gate is only *effective* with a hash
    too (CLAUDE.md 'one flag, one meaning' - these are deliberately two conditions, not
    one variable). Never lock the owner out because the hash went missing."""
    return bool(auth_cfg.get('enabled')) and bool(auth_cfg.get('password_hash'))


def gate_enabled_without_password(auth_cfg: dict) -> bool:
    """The one state gate_active() fails open on: the owner asked for the gate and there is
    no password to check against, so every route serves unauthenticated.

    Deliberately a second predicate rather than a change to gate_active() - failing open
    here is the documented choice ('never lock the owner out'), and both write surfaces
    refuse to create the state. It arrives anyway by hand-edit: README's forgotten-password
    recovery walks the user into the `auth:` block and says clearing `password_hash` is
    harmless, which it is only if `enabled` is turned off in the same edit. A partial config
    restore does it too.

    Written as the exact complement of gate_active() rather than as its own reading of the
    same two keys, so the two can never drift into disagreeing about what "no password"
    means and leave a fail-open state with nothing reporting it.
    """
    return bool(auth_cfg.get('enabled')) and not gate_active(auth_cfg)


def report_gate_state(auth_cfg: dict, source: str):
    """Surface - or clear - the enabled-but-hashless state. Needs an app context for the
    alert half; without one it still logs, and skips only the row.

    Create-or-dismiss on a single standing alert keyed source=GATE_INERT_SOURCE, mirroring
    app/notifications.py::alert_placeholder_url. Both halves are load-bearing: refresh_auth()
    runs on every auth.* settings write, so a fresh alert per call would stack a row per
    field save, and setting a password has to clear the standing one rather than leave the
    install accused forever.

    `source` names which path observed the state and reaches only the log line, never the
    alert's own source key - that stays constant so the two paths address the same row.
    Nothing here names the hash, present or absent: auth.password_hash is a sensitive config
    leaf (CLAUDE.md Config secrets).
    """
    from flask import has_app_context
    from .alerts import AUTH_GATE_INERT, create_alert, dismiss_open_alerts, has_open_alert

    inert = gate_enabled_without_password(auth_cfg)
    if inert:
        log.warning(
            'The login gate is ON in config but no password is set, so the app is '
            'currently NOT password-protected - every page and API is being served '
            'without a login (observed at: %s). Set a password in Settings > Security, '
            'or set auth.enabled to false in config.yaml.', source)

    if not has_app_context():
        return
    try:
        if not inert:
            dismiss_open_alerts(AUTH_GATE_INERT, GATE_INERT_SOURCE)
        elif not has_open_alert(AUTH_GATE_INERT, GATE_INERT_SOURCE):
            create_alert(
                AUTH_GATE_INERT,
                'Login gate is on but has no password',
                body=('The login gate is switched on in config.yaml, but no password is '
                      'set, so ChannelBin is NOT password-protected right now - every page '
                      'and API is reachable by anyone who can reach this app on the '
                      'network. Set a password in Settings > Security to turn the gate on '
                      'for real, or set auth.enabled to false to stop asking for it. This '
                      'alert clears itself once either one is done.'),
                source=GATE_INERT_SOURCE)
    except Exception:
        # Never let the diagnostic break the thing it is describing: this runs inside
        # create_app() and inside every auth.* settings save, and a failed alert write must
        # not take down startup or turn a successful password change into a 500.
        log.exception('Could not update the login-gate configuration alert')


def client_ip() -> str:
    return request.remote_addr or 'unknown'


def clear_session():
    """session.clear() plus dropping the request-cached CSRF token.

    Flask-WTF's generate_csrf() caches its result on `g` for the life of the request (or,
    under the test harness's persistent app_context - CLAUDE.md testing section - for the
    life of a whole TestApp), keyed on session['csrf_token']. A bare session.clear() leaves
    that cached g value pointing at a CSRF secret that no longer exists in the session, so
    the next page rendered in the same context hands out a token that can never validate.
    Every session.clear() in this module goes through here instead of the bare call.
    """
    session.clear()
    field_name = current_app.config.get('WTF_CSRF_FIELD_NAME', 'csrf_token')
    g.pop(field_name, None)


def _prune_locked(now):
    """Drop entries with nothing live left, then enforce MAX_TRACKED_IPS. Caller holds
    the lock. An IP with no failures inside the window is indistinguishable from one that
    was never seen, so keeping its key buys nothing and costs memory forever."""
    for ip in [ip for ip, times in _failures.items() if not times]:
        del _failures[ip]
    if len(_failures) <= MAX_TRACKED_IPS:
        return
    # Oldest-first by that IP's most recent failure: the entries closest to expiring
    # anyway, and never the one actively being brute-forced.
    for ip, _ in sorted(_failures.items(), key=lambda kv: max(kv[1]))[
            :len(_failures) - MAX_TRACKED_IPS]:
        del _failures[ip]


def check_lockout(ip: str):
    """Return the UTC unlock time if `ip` is currently locked out, else None."""
    now = datetime.utcnow()
    with _failures_lock:
        times = [t for t in _failures.get(ip, []) if now - t < LOCKOUT_WINDOW]
        if times:
            _failures[ip] = times
        else:
            # Never write an empty list back: every GET /login called through here, so
            # storing one entry per IP that merely loaded the page grew this dict without
            # bound for the life of the process (dev/changelog/487).
            _failures.pop(ip, None)
        _prune_locked(now)
        if len(times) >= MAX_FAILURES:
            return times[0] + LOCKOUT_WINDOW
    return None


def record_failure(ip: str) -> bool:
    """Record a failed login attempt. Returns True if this attempt just tripped lockout."""
    now = datetime.utcnow()
    with _failures_lock:
        times = [t for t in _failures.get(ip, []) if now - t < LOCKOUT_WINDOW]
        times.append(now)
        _failures[ip] = times
        _prune_locked(now)
        return len(times) == MAX_FAILURES


def record_success(ip: str):
    with _failures_lock:
        _failures.pop(ip, None)


def password_epoch(auth_cfg: dict) -> str:
    """A short, non-reversible marker of *which* password a session was issued against.

    Stamped into the session at login and re-checked on every request, so changing the
    password invalidates every session issued against the old one - which is what the
    Settings card has always promised ('Changing it signs out every other device') and
    what gives the owner a revocation lever at all. Without it the only way to evict a
    session was rotating flask.secret_key by hand, and with the default
    session_timeout_minutes of 0 a session otherwise never ends (dev/changelog/487).

    A digest, not the hash itself: the session cookie is signed but not encrypted, so its
    contents are readable by whoever holds it, and the stored hash must not be in there.
    """
    stored = auth_cfg.get('password_hash') or ''
    return hashlib.sha256(stored.encode()).hexdigest()[:16] if stored else ''


def is_authenticated(auth_cfg: dict) -> bool:
    if not session.get('auth_ok'):
        return False
    # compare_digest over two public digests is habit, not a threat model - neither side
    # is a secret. A session predating this check has no auth_pw at all and fails here,
    # which is correct: it was issued with no password binding to verify.
    if not hmac.compare_digest(str(session.get('auth_pw') or ''), password_epoch(auth_cfg)):
        log.info('Session rejected: issued against a different password than the one now set')
        clear_session()
        return False
    timeout = auth_cfg.get('session_timeout_minutes') or 0
    if timeout <= 0:
        return True
    auth_at_raw = session.get('auth_at')
    if not auth_at_raw:
        clear_session()
        return False
    try:
        auth_at = datetime.fromisoformat(auth_at_raw)
    except (TypeError, ValueError):
        # TypeError as well as ValueError: fromisoformat() raises TypeError - not
        # ValueError - for a non-string, so a session carrying an int auth_at reached the
        # gate as an unhandled 500 instead of a denial (dev/docs/BUGS.md 2026-08-06 09:00).
        # Anything unparseable is "not authenticated", never an error page.
        clear_session()
        return False
    # Absolute timeout: compared against the login timestamp only, never rewritten on
    # activity. A sliding window would never expire here - every open page polls
    # /api/nav-status every 15s, so a tab left open would keep resetting an idle timer.
    if datetime.utcnow() - auth_at >= timedelta(minutes=timeout):
        clear_session()
        return False
    return True


def _next_path() -> str:
    return request.full_path if request.query_string else request.path


def validate_next(value):
    """Only a local, same-app path may be used as a post-login redirect target.

    Rejects anything carrying a scheme or netloc (//evil.test, http://evil.test) and the
    backslash form some browsers/parsers still treat as a path separator (\\evil.test).
    Returns None on anything not obviously local; callers fall back to the app root.
    """
    if not value or not value.startswith('/'):
        return None
    if value.startswith('//') or value.startswith('/\\'):
        return None
    from urllib.parse import urlsplit
    parts = urlsplit(value)
    if parts.scheme or parts.netloc:
        return None
    return value


def _deny():
    if wants_json(request):
        resp = jsonify({'error': 'Authentication required'})
        resp.status_code = 401
        resp.headers['X-Auth-Required'] = '1'
        return resp
    return redirect(url_for('auth.login', next=_next_path()))


def install(app):
    """Register the before_request gate. Must run after register_blueprints() in
    create_app(), so request.endpoint values for every blueprint already exist by the
    time this closure is defined and compared against."""

    @app.before_request
    def _auth_gate():
        auth_cfg = current_app.config.get('AUTH') or {}
        if not gate_active(auth_cfg):
            return None
        # request.endpoint is None for a genuine 404 (no route matched) - gated too, so
        # an unauthenticated request can't use a 404-vs-redirect difference to fingerprint
        # which routes exist.
        if request.endpoint in ALWAYS_OPEN or request.blueprint in SELF_GATED_BLUEPRINTS:
            return None
        if is_authenticated(auth_cfg):
            return None
        return _deny()
