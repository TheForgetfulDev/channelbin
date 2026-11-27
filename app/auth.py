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
import ipaddress
import logging
import threading
from collections import deque, namedtuple
from datetime import datetime, timedelta

from flask import current_app, g, jsonify, redirect, request, session, url_for

from .fmt_utils import length_words
from .proxy_trust import parse_address

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
SELF_GATED_BLUEPRINTS = {'ha', 'output_feed'}

# The source key for the standing "enabled but no password" alert. One constant, not the
# observing path's name, so startup and every settings save address the same row rather
# than each raising their own copy of the same condition.
GATE_INERT_SOURCE = 'auth-gate'

#: Wrong passwords inside LOCKOUT_WINDOW that lock an address out.
MAX_FAILURES = 5
LOCKOUT_WINDOW = timedelta(minutes=15)
#: How long each lockout of one address lasts: the first is the first step, each one after it
#: the next, and the last step every time past the end. Shared by the web login and the IPTV
#: output (dev/changelog/1774).
LOCKOUT_STEPS = (timedelta(minutes=5), timedelta(minutes=15), timedelta(minutes=30),
                 timedelta(hours=1), timedelta(hours=6), timedelta(hours=12),
                 timedelta(hours=24))
#: With no new lockout for this long after its latest one ended, an address starts again at
#: the first step.
LOCKOUT_FORGET = timedelta(hours=24)
# Hard ceiling on distinct IPs tracked at once. The tracker is keyed on an address the
# client can influence whenever a reverse proxy is believed (app/proxy_trust.py) and it forwards an
# attacker-supplied X-Forwarded-For unchanged (nginx's proxy_add_x_forwarded_for appends
# and is safe; passing $http_x_forwarded_for through is not), so without a ceiling one
# client could grow this dict without limit - on a box with no swap, that is the whole
# machine (dev/changelog/487). Over the cap, expired entries go first and the oldest
# survivors after that: lockout accuracy degrades under flood, memory does not.
MAX_TRACKED_IPS = 1024


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


def lockout_key(address: str) -> str:
    """What a wrong login from `address` is counted against: an IPv4 address itself, an
    IPv4-mapped IPv6 address as that IPv4, and any other IPv6 address as its /64, since one
    home connection is handed a whole /64 and a caller could otherwise take a new address for
    every few guesses (dev/changelog/1769). Text that is no address is its own key."""
    ip = parse_address(address)
    if ip is None:
        return str(address)
    if ip.version == 6:
        return str(ipaddress.ip_network((int(ip) >> 64 << 64, 64)))
    return str(ip)


class Lockout(namedtuple('Lockout', 'address until times length')):
    """One address locked out now: its lockout key, when the lockout ends (UTC), how many
    times it has been locked out counting this one, and how long this one is."""


class FailureTracker:
    """Wrong-password counts per address, and the lockout they trip: `max_failures` inside
    `window` locks an address out, and each lockout of the same address is longer than its
    last.

    A lockout runs its whole step from the wrong login that tripped it: `steps[0]` the first
    time, the next step the next time, and the last step every time past the end. How many
    times an address has been locked out is forgotten once `forget` has passed since its
    latest lockout ended with no new one. A wrong login during a lockout counts for nothing,
    so a player that keeps asking does not lengthen its own lockout, and a lockout starts the
    count again from none (dev/changelog/1774).

    Every address is counted under lockout_key(), applied here at record and at check, so a
    caller never spells the key itself. In memory, so a restart forgets every count, lockout
    and step, and only as meaningful as request.remote_addr - which is the single real client
    only when the request's reverse proxy was believed (app/proxy_trust.py); otherwise every
    proxied request looks like one address. A brute-force brake, not a durable audit log, so
    neither limitation is a defect here. One instance per door, so a lockout at one never
    locks anyone out of another: the web login's is `web_login` below, the IPTV output's is
    output_feed.login_failures (dev/changelog/1761).

    `alarm_max` wrong logins from everywhere inside `alarm_window` is an alarm and nothing
    else: it refuses nobody and changes no count. A limit across addresses that never refuses
    a right password cannot slow guessing, and one that does would let anyone lock every user
    out, so what it buys is the owner being told (dev/changelog/1769)."""

    def __init__(self, max_failures: int, window: timedelta, max_tracked: int = MAX_TRACKED_IPS,
                 alarm_max: int = 0, alarm_window: timedelta = timedelta(minutes=1),
                 steps: tuple = LOCKOUT_STEPS, forget: timedelta = LOCKOUT_FORGET):
        self.max_failures = max_failures
        self.window = window
        self.max_tracked = max_tracked
        self.alarm_max = alarm_max
        self.alarm_window = alarm_window
        self.steps = tuple(steps)
        self.forget = forget
        # {lockout key: [failure datetimes]}, wrong logins inside the window and outside
        # any lockout.
        self.failures = {}
        # {lockout key: (times locked out, when the latest lockout ends)}. Outlives the
        # lockout by `forget`, which is what makes the next one longer.
        self.lockouts = {}
        # (time, key) of the latest wrong logins from everywhere, for the alarm. Bounded, so
        # past its length the count reads "at least".
        self._recent = deque(maxlen=max(1, alarm_max) * 20)
        self._alarm_first = None    # when the alarm running now first tripped
        self._alarm_last = None     # the last wrong login counted while it was tripped
        self._alarm_peak = 0
        self._alarm_logged = None   # when take_alarm_notice() last handed one out
        self._lock = threading.Lock()

    def _prune_locked(self, now, keep=None):
        """Drop what no longer says anything, then hold each table to max_tracked. Caller
        holds the lock. `keep` is the key just written, whose lockout is never the one
        dropped: a new first-step lockout is the soonest-ending one in a full table. An address with no failures inside the window and no lockout inside
        `forget` is indistinguishable from one that was never seen, so keeping its key buys
        nothing and costs memory forever."""
        for ip in [ip for ip, times in self.failures.items() if not times]:
            del self.failures[ip]
        for ip in [ip for ip, (_n, until) in self.lockouts.items() if now - until >= self.forget]:
            del self.lockouts[ip]
        if len(self.failures) > self.max_tracked:
            # Oldest-first by that address's most recent failure: the entries closest to
            # expiring anyway, and never the one actively being brute-forced.
            for ip, _ in sorted(self.failures.items(), key=lambda kv: max(kv[1]))[
                    :len(self.failures) - self.max_tracked]:
                del self.failures[ip]
        if len(self.lockouts) > self.max_tracked:
            # Soonest-ending first: a lockout already over, then the one with least left.
            for ip in sorted((ip for ip in self.lockouts if ip != keep),
                             key=lambda ip: self.lockouts[ip][1])[
                    :len(self.lockouts) - self.max_tracked]:
                del self.lockouts[ip]

    def _step(self, times: int) -> timedelta:
        return self.steps[min(times, len(self.steps)) - 1]

    def _lockout_locked(self, key, now):
        """`key`'s lockout if it is locked out now, else None. Caller holds the lock."""
        times, until = self.lockouts.get(key, (0, None))
        if until is None or now >= until:
            return None
        return Lockout(key, until, times, self._step(times))

    def _live_failures_locked(self, key, now) -> list:
        """`key`'s wrong logins still inside the window, written back. Caller holds the lock."""
        times = [t for t in self.failures.get(key, []) if now - t < self.window]
        if times:
            self.failures[key] = times
        else:
            # Never write an empty list back: every GET /login calls through here, so
            # storing one entry per address that merely loaded the page grew this dict
            # without bound for the life of the process (dev/changelog/487).
            self.failures.pop(key, None)
        return times

    def lockout(self, ip: str):
        """The Lockout `ip` is under now, else None."""
        key = lockout_key(ip)
        now = datetime.utcnow()
        with self._lock:
            self._live_failures_locked(key, now)
            self._prune_locked(now)
            return self._lockout_locked(key, now)

    def check_lockout(self, ip: str):
        """Return the UTC unlock time if `ip` is currently locked out, else None."""
        found = self.lockout(ip)
        return found.until if found else None

    def _recent_locked(self, now) -> list:
        while self._recent and now - self._recent[0][0] >= self.alarm_window:
            self._recent.popleft()
        return list(self._recent)

    def record_failure(self, ip: str) -> bool:
        """Record a failed attempt. Returns True if this attempt just tripped a lockout."""
        key = lockout_key(ip)
        now = datetime.utcnow()
        with self._lock:
            tripped = False
            if self._lockout_locked(key, now) is None:
                times = self._live_failures_locked(key, now) + [now]
                if len(times) >= self.max_failures:
                    self.failures.pop(key, None)
                    past, ended = self.lockouts.get(key, (0, None))
                    if ended is not None and now - ended >= self.forget:
                        past = 0
                    self.lockouts[key] = (past + 1, now + self._step(past + 1))
                    tripped = True
                else:
                    self.failures[key] = times
            self._prune_locked(now, keep=key)
            if self.alarm_max:
                self._recent.append((now, key))
                count = len(self._recent_locked(now))
                if count >= self.alarm_max:
                    if self._alarm_first is None or now - self._alarm_last >= self.alarm_window:
                        # A new alarm, and so a new line in the log.
                        self._alarm_first, self._alarm_peak, self._alarm_logged = now, 0, None
                    self._alarm_last = now
                    self._alarm_peak = max(self._alarm_peak, count)
            return tripped

    def record_success(self, ip: str):
        """A right password from `ip`, at a door one password opens: its count and its
        lockout steps are forgotten."""
        self.clear(ip)

    def alarm(self) -> dict:
        """The wrong-login alarm: {'active', 'count' and 'addresses' inside the alarm window
        now, 'first' and 'last' (UTC) of the latest time it tripped or None, 'peak'}."""
        now = datetime.utcnow()
        with self._lock:
            recent = self._recent_locked(now)
            return {
                'active': bool(self.alarm_max) and len(recent) >= self.alarm_max,
                'count': len(recent), 'addresses': len({key for _t, key in recent}),
                'first': self._alarm_first, 'last': self._alarm_last, 'peak': self._alarm_peak,
            }

    def take_alarm_notice(self, every: timedelta = timedelta(minutes=10)):
        """alarm() when it is tripped and has not been handed out inside `every`, else None:
        the caller's cue to log it once, not once per wrong login."""
        state = self.alarm()
        if not state['active']:
            return None
        with self._lock:
            now = datetime.utcnow()
            if self._alarm_logged is not None and now - self._alarm_logged < every:
                return None
            self._alarm_logged = now
        return state

    def locked_out(self) -> list:
        """A Lockout for every address locked out now, soonest unlock first. An IPv6 address
        is its /64 (lockout_key)."""
        now = datetime.utcnow()
        with self._lock:
            found = [self._lockout_locked(key, now) for key in self.lockouts]
        return sorted((l for l in found if l is not None), key=lambda l: l.until)

    def clear(self, ip: str) -> bool:
        """Forget `ip`'s count, its lockout and how many times it has been locked out, so it
        may try again now and its next lockout is the first step. `ip` is an address or a
        lockout key as locked_out() names it. Returns whether it was locked out."""
        key = lockout_key(ip)
        with self._lock:
            was = self._lockout_locked(key, datetime.utcnow()) is not None
            self.failures.pop(key, None)
            self.lockouts.pop(key, None)
        return was

    def reset(self):
        """Forget every count, lockout, step and the alarm. tests/support/app.py."""
        with self._lock:
            self.failures.clear()
            self.lockouts.clear()
            self._recent.clear()
            self._alarm_first = self._alarm_last = self._alarm_logged = None
            self._alarm_peak = 0


def lockout_words(found: Lockout) -> str:
    """'5 minutes, its first lockout' / '30 minutes, its 3rd lockout in a row': how long a lockout is and which one it is, for
    a log line, an alert and the IPTV Output page."""
    n = found.times
    suffix = 'th' if 10 <= n % 100 <= 20 else {1: 'st', 2: 'nd', 3: 'rd'}.get(n % 10, 'th')
    which = 'first lockout' if n == 1 else f'{n}{suffix} lockout in a row'
    return f'{length_words(found.length)}, its {which}'


def steps_words(steps=None) -> str:
    """'5 minutes, then 15 minutes, ... and 24 hours every time after that': the steps in a
    sentence."""
    words = [length_words(step) for step in (LOCKOUT_STEPS if steps is None else steps)]
    return ', then '.join(words) + ' every time after that' if len(words) > 1 else words[0]


web_login = FailureTracker(MAX_FAILURES, LOCKOUT_WINDOW)
check_lockout = web_login.check_lockout
current_lockout = web_login.lockout
record_failure = web_login.record_failure
record_success = web_login.record_success


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
