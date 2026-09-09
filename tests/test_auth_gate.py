"""The optional password gate (app/auth.py, app/routes/auth.py).

Covers CLAUDE.md's password-gate decisions: deny-by-default over ALWAYS_OPEN (GateCoverageTests
plus AllowlistPinTests - read the former's docstring for what each one actually catches),
absolute (never sliding) session timeout, session cleared on login, generic failure messages,
lockout after 5 failed attempts within 15 minutes plus the AUTH_LOGIN_LOCKOUT alert, next=
open-redirect rejection, the enable-without-a-password 400 on both write surfaces, and
password_hash masked everywhere a sensitive config leaf is masked.

The security-review pass (dev/changelog/487) added: sessions bound to the password they were
issued against, a malformed session denying instead of 500ing, the failed-attempt tracker
staying bounded, and README.md's forgotten-password recovery path actually working.

Every TestCase here uses tests/support/config_sandbox.py::ConfigSandbox, which patches
app.config._CONFIG_PATH to a throwaway temp file - config.yaml writes must never touch the
real repo-root config.yaml (dev/docs/BUGS.md 2026-08-06 - an
ad-hoc script during this feature's own development did exactly that on the live install).

Runs against a throwaway temp SQLite DB - never the live dvr.db.
  python3 -m unittest tests.test_auth_gate
"""
import re
import unittest
from datetime import datetime, timedelta

import yaml
from werkzeug.security import generate_password_hash

from app import db
from app import config as cfgmod
from app import auth as authmod
from app.alerts import AUTH_GATE_INERT
from app.database import Alert
from tests.support.app import make_test_app
from tests.support.config_sandbox import ConfigSandbox

PASSWORD = 'correcthorse-battery-staple'


class _ConfigSandbox(ConfigSandbox):
    """The shared config.yaml sandbox plus this file's own lockout-tracker reset."""

    def setUp(self):
        super().setUp()
        # The failed-attempt tracker is module-level state shared across every test in
        # the process (nothing else in the app touches it) - reset per test for isolation.
        authmod._failures.clear()
        self.addCleanup(authmod._failures.clear)


def _csrf_meta(html):
    m = re.search(r'name="csrf-token" content="([^"]+)"', html)
    return m.group(1) if m else None


def _csrf_field(html):
    m = re.search(r'name="csrf_token" value="([^"]+)"', html)
    return m.group(1) if m else None


class GateOffByDefaultTests(_ConfigSandbox):
    """The default (auth.enabled: false) must leave every existing behavior unchanged."""

    def setUp(self):
        super().setUp()
        self.t = make_test_app()
        self.client = self.t.app.test_client()
        self.addCleanup(self.t.cleanup)

    def test_login_page_404s_when_gate_is_off(self):
        r = self.client.get('/login')
        self.assertEqual(r.status_code, 404)

    def test_landing_page_unaffected(self):
        r = self.client.get('/')
        self.assertEqual(r.status_code, 200)

    def test_settings_api_unaffected(self):
        r = self.client.get('/api/settings')
        self.assertEqual(r.status_code, 200)


class GateCoverageTests(_ConfigSandbox):
    """The load-bearing test: every route not in ALWAYS_OPEN must be gated.

    Read what this does and does not catch (measured, dev/changelog/487 - the original
    docstring here overclaimed). A route added with no auth consideration at all is gated
    *by construction*, because the gate denies by default over request.endpoint - adding a
    bare route and re-running this test leaves it green, and the route is safe. What this
    test actually pins is that the deny-by-default property still holds across every real
    route in the map: a blueprint mounted so the before_request never sees it, a view that
    answers from a cache ahead of the gate, or a path served under the exempt 'static'
    endpoint would each show up here.

    The one way to genuinely un-gate a route is to name it in ALWAYS_OPEN, which this test
    skips by design - AllowlistPinTests below is what makes that require a deliberate edit.
    Together they cover the defect class that left the Settings write endpoints (raw config
    write, per-field write, backup rollback, restart) reachable with zero auth
    (dev/changelog/485).
    """

    def setUp(self):
        super().setUp()
        pw_hash = generate_password_hash(PASSWORD)
        self.t = make_test_app(extra_overrides={
            'auth': {'enabled': True, 'password_hash': pw_hash, 'session_timeout_minutes': 0},
        })
        self.client = self.t.app.test_client()
        self.addCleanup(self.t.cleanup)

    @staticmethod
    def _dummy_url(rule):
        def sub(m):
            conv = m.group('conv')
            return '1' if conv == 'int' else 'x'
        return re.sub(r'<(?:(?P<conv>[a-z]+):)?(?P<name>\w+)>', sub, rule.rule)

    def test_every_non_exempt_route_is_gated(self):
        app = self.t.app
        failures = []
        for rule in app.url_map.iter_rules():
            if rule.endpoint in authmod.ALWAYS_OPEN:
                continue
            # Self-gated blueprints (e.g. 'ha') enforce their own independent
            # authentication and answer unauthenticated requests with their own denial
            # shape, not this gate's 401+X-Auth-Required/302-to-login pair - covered by
            # that blueprint's own test file instead (tests/test_ha_auth.py).
            if rule.endpoint.split('.')[0] in authmod.SELF_GATED_BLUEPRINTS:
                continue
            methods = rule.methods - {'HEAD', 'OPTIONS'}
            if not methods:
                continue
            method = sorted(methods)[0]
            url = self._dummy_url(rule)
            resp = self.client.open(url, method=method)
            ok = (resp.status_code == 401 and resp.headers.get('X-Auth-Required') == '1') or (
                resp.status_code == 302 and '/login' in (resp.headers.get('Location') or ''))
            if not ok:
                failures.append(f'{method} {url} ({rule.endpoint}) -> {resp.status_code}')
        self.assertEqual(failures, [], 'unauthenticated but not gated:\n' + '\n'.join(failures))

    def test_a_404_is_gated_too(self):
        """request.endpoint is None for a genuine 404 - must not leak past the gate."""
        r = self.client.get('/this-route-does-not-exist', follow_redirects=False)
        self.assertIn(r.status_code, (302, 401))

    def test_static_files_are_exempt(self):
        r = self.client.get('/static/css/style.css')
        self.assertEqual(r.status_code, 200)


class AllowlistPinTests(unittest.TestCase):
    """ALWAYS_OPEN is the only way to un-gate a route, and GateCoverageTests skips whatever
    is in it - so widening it is invisible to every other test in this file. Pinning the
    exact set makes that a deliberate edit here rather than a silent one (dev/changelog/487).

    If you are here because this failed: adding an entry is allowed, but it means that
    endpoint is reachable by anyone who can reach the port. Confirm it serves nothing
    user-specific before updating the expected set.
    """

    def test_always_open_is_exactly_the_three_known_endpoints(self):
        self.assertEqual(authmod.ALWAYS_OPEN, {'auth.login', 'auth.logout', 'static'})


class SelfGatedBlueprintPinTests(unittest.TestCase):
    """SELF_GATED_BLUEPRINTS is the other way to bypass this gate - a whole blueprint
    rather than one endpoint - so it needs the same deliberate-edit pin as ALWAYS_OPEN
    above. Widening it means trusting a new blueprint's own before_request completely."""

    def test_self_gated_blueprints_is_exactly_ha(self):
        self.assertEqual(authmod.SELF_GATED_BLUEPRINTS, {'ha'})


class LoginFlowTests(_ConfigSandbox):
    def setUp(self):
        super().setUp()
        self.pw_hash = generate_password_hash(PASSWORD)
        self.t = make_test_app(extra_overrides={
            'auth': {'enabled': True, 'password_hash': self.pw_hash, 'session_timeout_minutes': 0},
        })
        self.client = self.t.app.test_client()
        self.addCleanup(self.t.cleanup)

    def _login_token(self):
        r = self.client.get('/login')
        return _csrf_field(r.get_data(as_text=True))

    def test_correct_password_redirects_to_root(self):
        tok = self._login_token()
        r = self.client.post('/login', data={'password': PASSWORD, 'csrf_token': tok},
                             follow_redirects=False)
        self.assertEqual(r.status_code, 302)
        self.assertEqual(r.headers['Location'], '/')

    def test_wrong_password_is_a_generic_failure_with_no_session(self):
        tok = self._login_token()
        r = self.client.post('/login', data={'password': 'not-it', 'csrf_token': tok})
        self.assertEqual(r.status_code, 401)
        body = r.get_data(as_text=True).lower()
        self.assertIn('incorrect', body)
        # Never a "no password set" vs "wrong password" distinction in the response.
        self.assertNotIn('no password', body)
        r2 = self.client.get('/', follow_redirects=False)
        self.assertEqual(r2.status_code, 302)

    def test_already_authenticated_get_redirects_away_from_login(self):
        tok = self._login_token()
        self.client.post('/login', data={'password': PASSWORD, 'csrf_token': tok})
        r = self.client.get('/login', follow_redirects=False)
        self.assertEqual(r.status_code, 302)

    def test_valid_local_next_is_honored(self):
        tok = self._login_token()
        r = self.client.post('/login?next=/settings',
                             data={'password': PASSWORD, 'csrf_token': tok}, follow_redirects=False)
        self.assertEqual(r.headers['Location'], '/settings')

    def test_open_redirect_next_values_are_rejected(self):
        for bad in ('//evil.test', 'http://evil.test', '\\\\evil.test', '/\\evil.test'):
            tok = self._login_token()
            r = self.client.post('/login?next=' + bad,
                                 data={'password': PASSWORD, 'csrf_token': tok}, follow_redirects=False)
            self.assertEqual(r.headers['Location'], '/', f'next={bad!r} was not rejected')


class SessionSemanticsTests(_ConfigSandbox):
    def _make(self, timeout_minutes):
        pw_hash = generate_password_hash(PASSWORD)
        self._auth_cfg = {'enabled': True, 'password_hash': pw_hash,
                         'session_timeout_minutes': timeout_minutes}
        self.t = make_test_app(extra_overrides={'auth': dict(self._auth_cfg)})
        self.client = self.t.app.test_client()
        self.addCleanup(self.t.cleanup)

    def _seed_session(self, auth_at):
        """Hand-build a signed-in session. auth_pw is not optional decoration: a session
        is only authenticated while it matches the password currently set
        (app/auth.py::password_epoch, dev/changelog/487), so a seeded session without it is
        rejected before the timeout these tests are about is ever consulted."""
        with self.client.session_transaction() as sess:
            sess['auth_ok'] = True
            sess['auth_pw'] = authmod.password_epoch(self._auth_cfg)
            sess['auth_at'] = auth_at

    def test_session_cleared_on_login_fixation(self):
        self._make(0)
        with self.client.session_transaction() as sess:
            sess['pre_existing_junk'] = 'should not survive'
        r = self.client.get('/login')
        tok = _csrf_field(r.get_data(as_text=True))
        self.client.post('/login', data={'password': PASSWORD, 'csrf_token': tok})
        with self.client.session_transaction() as sess:
            self.assertNotIn('pre_existing_junk', sess)
            self.assertTrue(sess.get('auth_ok'))

    def test_timeout_zero_never_expires(self):
        self._make(0)
        self._seed_session((datetime.utcnow() - timedelta(days=400)).isoformat())
        r = self.client.get('/')
        self.assertEqual(r.status_code, 200)

    def test_absolute_timeout_expires_the_session(self):
        self._make(30)
        self._seed_session((datetime.utcnow() - timedelta(minutes=31)).isoformat())
        r = self.client.get('/', follow_redirects=False)
        self.assertEqual(r.status_code, 302)
        self.assertIn('/login', r.headers['Location'])

    def test_timeout_is_absolute_not_extended_by_activity(self):
        """A request made partway through the window must not push auth_at forward."""
        self._make(30)
        original_auth_at = (datetime.utcnow() - timedelta(minutes=20)).isoformat()
        self._seed_session(original_auth_at)
        r = self.client.get('/')
        self.assertEqual(r.status_code, 200)
        with self.client.session_transaction() as sess:
            self.assertEqual(sess.get('auth_at'), original_auth_at)
        # 11 more minutes (31 total) now expires it - proving the clock ran from the
        # original login, not from the request made above.
        self._seed_session((datetime.utcnow() - timedelta(minutes=31)).isoformat())
        r2 = self.client.get('/', follow_redirects=False)
        self.assertEqual(r2.status_code, 302)

    def test_gate_inert_when_enabled_but_no_hash(self):
        """Never lock the owner out because the hash went missing."""
        self.t = make_test_app(extra_overrides={
            'auth': {'enabled': True, 'password_hash': '', 'session_timeout_minutes': 0},
        })
        self.client = self.t.app.test_client()
        self.addCleanup(self.t.cleanup)
        r = self.client.get('/')
        self.assertEqual(r.status_code, 200)
        r2 = self.client.get('/login')
        self.assertEqual(r2.status_code, 404)


class PasswordChangeRevocationTests(_ConfigSandbox):
    """Changing the password invalidates every session issued against the old one.

    The Settings Security card has always said "Changing it signs out every other device";
    before dev/changelog/487 it did not - an existing session stayed valid, and with the
    default session_timeout_minutes of 0 it stayed valid forever, leaving no way to evict a
    session short of hand-rotating flask.secret_key and restarting.
    """

    def setUp(self):
        super().setUp()
        # Written to the sandboxed config.yaml, not extra_overrides: the password endpoint
        # re-reads config via load_config()/refresh_auth() at run time, which does not see
        # a test app's overrides (CLAUDE.md testing section).
        with open(self._cfg_path, 'w') as f:
            yaml.dump({'auth': {'enabled': True, 'password_hash': generate_password_hash(PASSWORD),
                                'session_timeout_minutes': 0, 'cookie_secure': False}}, f)
        cfgmod._yaml_cache = None
        self.t = make_test_app()
        self.addCleanup(self.t.cleanup)

    def _signed_in_client(self):
        c = self.t.app.test_client()
        r = c.get('/login')
        tok = _csrf_field(r.get_data(as_text=True))
        r = c.post('/login', data={'password': PASSWORD, 'csrf_token': tok},
                   follow_redirects=False)
        self.assertEqual(r.status_code, 302)
        return c

    @staticmethod
    def _csrf_for(client):
        """A CSRF token valid for *this* client's session.

        Two test clients under one TestApp share a single app context, and so a single `g`
        - where Flask-WTF caches the token it generated for whichever session rendered
        last. In production every request gets its own `g` and this cannot happen; here the
        cache has to be dropped between clients or the second one is handed the first's
        token and the POST 400s on CSRF, not on anything being tested.
        """
        from flask import g
        g.pop('csrf_token', None)
        return _csrf_meta(client.get('/settings').get_data(as_text=True))

    def test_other_sessions_are_signed_out_and_the_changing_one_is_not(self):
        changer = self._signed_in_client()
        other = self._signed_in_client()
        self.assertEqual(other.get('/', follow_redirects=False).status_code, 200)

        tok = self._csrf_for(changer)
        r = changer.post('/api/settings/password',
                         json={'current_password': PASSWORD, 'new_password': 'a-brand-new-one',
                               'confirm_password': 'a-brand-new-one'},
                         headers={'X-CSRFToken': tok})
        self.assertEqual(r.status_code, 200)

        r = other.get('/', follow_redirects=False)
        self.assertEqual(r.status_code, 302, 'the other device kept its session')
        self.assertIn('/login', r.headers['Location'])
        self.assertEqual(changer.get('/', follow_redirects=False).status_code, 200,
                         'the browser that changed the password was signed out too')

    def test_password_change_does_not_extend_the_absolute_timeout(self):
        """Re-stamping the changing session must not reset auth_at into a fresh window."""
        c = self._signed_in_client()
        with c.session_transaction() as sess:
            sess['auth_at'] = (datetime.utcnow() - timedelta(minutes=20)).isoformat()
            original = sess['auth_at']
        tok = self._csrf_for(c)
        c.post('/api/settings/password',
               json={'current_password': PASSWORD, 'new_password': 'a-brand-new-one',
                     'confirm_password': 'a-brand-new-one'},
               headers={'X-CSRFToken': tok})
        with c.session_transaction() as sess:
            self.assertEqual(sess.get('auth_at'), original)

    def test_session_with_no_password_binding_is_rejected(self):
        """A cookie predating the binding (auth_ok, no auth_pw) is not authenticated."""
        c = self.t.app.test_client()
        with c.session_transaction() as sess:
            sess['auth_ok'] = True
            sess['auth_at'] = datetime.utcnow().isoformat()
        r = c.get('/', follow_redirects=False)
        self.assertEqual(r.status_code, 302)
        self.assertIn('/login', r.headers['Location'])


class MalformedSessionTests(_ConfigSandbox):
    """An unparseable session must deny, never raise. datetime.fromisoformat() raises
    TypeError - not ValueError - for a non-string, so an auth_at of the wrong type reached
    the gate as an unhandled 500 (dev/docs/BUGS.md 2026-08-06 09:00)."""

    def setUp(self):
        super().setUp()
        self.auth_cfg = {'enabled': True, 'password_hash': generate_password_hash(PASSWORD),
                        'session_timeout_minutes': 30}
        self.t = make_test_app(extra_overrides={'auth': dict(self.auth_cfg)})
        self.client = self.t.app.test_client()
        self.addCleanup(self.t.cleanup)

    def _seed(self, auth_at):
        with self.client.session_transaction() as sess:
            sess.clear()
            sess['auth_ok'] = True
            sess['auth_pw'] = authmod.password_epoch(self.auth_cfg)
            if auth_at is not None:
                sess['auth_at'] = auth_at

    def test_non_string_auth_at_denies_instead_of_500ing(self):
        for bad in (12345, 1.5, [], {'a': 1}, True):
            with self.subTest(auth_at=bad):
                self._seed(bad)
                r = self.client.get('/', follow_redirects=False)
                self.assertEqual(r.status_code, 302, f'auth_at={bad!r} did not deny cleanly')

    def test_unparseable_string_auth_at_denies(self):
        for bad in ('', 'not-a-date', '2026-13-45T99:99:99'):
            with self.subTest(auth_at=bad):
                self._seed(bad)
                r = self.client.get('/', follow_redirects=False)
                self.assertEqual(r.status_code, 302)


class ExpiredSessionLoginPageTests(_ConfigSandbox):
    """GET /login with a dead session renders the login page directly.

    It used to answer 302 -> '/' (the view checked session['auth_ok'] raw), and the gate
    then bounced that straight back to /login - two redirects to reach the page that was
    asked for (dev/changelog/487)."""

    def test_expired_session_gets_the_login_page_not_a_bounce(self):
        self.t = make_test_app(extra_overrides={
            'auth': {'enabled': True, 'password_hash': generate_password_hash(PASSWORD),
                    'session_timeout_minutes': 30},
        })
        self.addCleanup(self.t.cleanup)
        c = self.t.app.test_client()
        with c.session_transaction() as sess:
            sess['auth_ok'] = True
            sess['auth_at'] = (datetime.utcnow() - timedelta(minutes=99)).isoformat()
        r = c.get('/login', follow_redirects=False)
        self.assertEqual(r.status_code, 200)
        self.assertIn('name="password"', r.get_data(as_text=True))


class FailureTrackerBoundsTests(_ConfigSandbox):
    """The in-memory failed-attempt tracker must stay bounded.

    check_lockout() runs on every GET /login and used to write an empty list back for the
    caller's IP, so one dict entry accumulated per IP that merely loaded the page and was
    never evicted. The key can be attacker-chosen whenever flask.behind_proxy is on and the
    proxy forwards X-Forwarded-For verbatim, so unbounded growth is reachable from outside
    (dev/changelog/487). This box has no swap - unbounded is the whole machine.
    """

    def setUp(self):
        super().setUp()
        self.t = make_test_app()
        self.addCleanup(self.t.cleanup)

    def test_ips_that_never_failed_are_not_tracked(self):
        with self.t.app.test_request_context('/login'):
            for i in range(500):
                authmod.check_lockout('10.0.%d.%d' % (i // 256, i % 256))
        self.assertEqual(authmod._failures, {})

    def test_tracked_ips_are_capped(self):
        with self.t.app.test_request_context('/login'):
            for i in range(authmod.MAX_TRACKED_IPS + 250):
                authmod.record_failure('10.1.%d.%d' % (i // 256, i % 256))
        self.assertLessEqual(len(authmod._failures), authmod.MAX_TRACKED_IPS)

    def test_expired_entries_are_dropped(self):
        stale = datetime.utcnow() - (authmod.LOCKOUT_WINDOW + timedelta(minutes=1))
        authmod._failures['192.0.2.9'] = [stale, stale]
        with self.t.app.test_request_context('/login'):
            self.assertIsNone(authmod.check_lockout('192.0.2.9'))
        self.assertNotIn('192.0.2.9', authmod._failures)


class RecoveryPathTests(_ConfigSandbox):
    """README.md's forgotten-password recovery path, followed literally.

    Its steps leave auth.password_hash in place on purpose (only auth.enabled is edited),
    and Settings > Security then refused to accept a new password without the current one -
    which is the password the user has just declared forgotten. The path was a dead end
    (dev/changelog/487). With the gate inert there is no boundary for that check to defend:
    whoever can reach this route can already rewrite post_script and run code.
    """

    def _app_with(self, auth_block):
        with open(self._cfg_path, 'w') as f:
            yaml.dump({'auth': auth_block}, f)
        cfgmod._yaml_cache = None
        self.t = make_test_app()
        self.addCleanup(self.t.cleanup)
        return self.t.app.test_client()

    def test_new_password_accepted_with_the_gate_off_and_an_old_hash_present(self):
        c = self._app_with({'enabled': False, 'password_hash': generate_password_hash('forgotten'),
                            'session_timeout_minutes': 0, 'cookie_secure': False})
        tok = _csrf_meta(c.get('/settings').get_data(as_text=True))
        r = c.post('/api/settings/password',
                   json={'new_password': 'a-new-one-1', 'confirm_password': 'a-new-one-1'},
                   headers={'X-CSRFToken': tok})
        self.assertEqual(r.status_code, 200, r.get_json())
        cfgmod._yaml_cache = None
        self.assertTrue(cfgmod.load_config()['auth']['password_hash'])

    def test_current_password_is_still_required_while_the_gate_is_live(self):
        c = self._app_with({'enabled': True, 'password_hash': generate_password_hash(PASSWORD),
                            'session_timeout_minutes': 0, 'cookie_secure': False})
        r = c.get('/login')
        tok = _csrf_field(r.get_data(as_text=True))
        c.post('/login', data={'password': PASSWORD, 'csrf_token': tok})
        tok = _csrf_meta(c.get('/settings').get_data(as_text=True))
        r = c.post('/api/settings/password',
                   json={'new_password': 'a-new-one-1', 'confirm_password': 'a-new-one-1'},
                   headers={'X-CSRFToken': tok})
        self.assertEqual(r.status_code, 400)


class LockoutTests(_ConfigSandbox):
    def setUp(self):
        super().setUp()
        pw_hash = generate_password_hash(PASSWORD)
        self.t = make_test_app(extra_overrides={
            'auth': {'enabled': True, 'password_hash': pw_hash, 'session_timeout_minutes': 0},
        })
        self.client = self.t.app.test_client()
        self.addCleanup(self.t.cleanup)

    def test_locked_out_after_five_failures_and_alert_is_raised(self):
        for _ in range(5):
            r = self.client.get('/login')
            tok = _csrf_field(r.get_data(as_text=True))
            self.client.post('/login', data={'password': 'wrong', 'csrf_token': tok})

        r = self.client.get('/login')
        tok = _csrf_field(r.get_data(as_text=True))
        r = self.client.post('/login', data={'password': PASSWORD, 'csrf_token': tok})
        self.assertEqual(r.status_code, 429)
        self.assertIn('too many', r.get_data(as_text=True).lower())

        alert = Alert.query.filter_by(alert_type='AUTH_LOGIN_LOCKOUT').first()
        self.assertIsNotNone(alert)
        self.assertEqual(alert.severity, 'WARN')


class PasswordEndpointTests(_ConfigSandbox):
    def setUp(self):
        super().setUp()
        self.t = make_test_app()
        self.client = self.t.app.test_client()
        self.addCleanup(self.t.cleanup)

    def _csrf(self):
        r = self.client.get('/settings')
        return _csrf_meta(r.get_data(as_text=True))

    def test_enabling_without_a_password_is_rejected_via_field_endpoint(self):
        tok = self._csrf()
        r = self.client.post('/api/settings/field',
                             json={'path': 'auth.enabled', 'value': True},
                             headers={'X-CSRFToken': tok})
        self.assertEqual(r.status_code, 400)

    def test_enabling_without_a_password_is_rejected_via_yaml_editor(self):
        tok = self._csrf()
        raw = yaml.dump({'auth': {'enabled': True, 'password_hash': ''}})
        r = self.client.post('/settings', data={'config_yaml': raw, 'csrf_token': tok},
                             follow_redirects=True)
        self.assertEqual(r.status_code, 200)
        cfgmod._yaml_cache = None
        stored = cfgmod.load_config()
        self.assertFalse(stored['auth']['enabled'])

    def test_set_password_then_enable_succeeds_and_takes_effect_without_restart(self):
        tok = self._csrf()
        r = self.client.post('/api/settings/password',
                             json={'new_password': 'brand-new-pw-1', 'confirm_password': 'brand-new-pw-1'},
                             headers={'X-CSRFToken': tok})
        self.assertEqual(r.status_code, 200)

        tok = self._csrf()
        r = self.client.post('/api/settings/field', json={'path': 'auth.enabled', 'value': True},
                             headers={'X-CSRFToken': tok})
        self.assertEqual(r.status_code, 200)

        # No restart in between - refresh_auth() must have updated app.config['AUTH'] live.
        r = self.client.get('/', follow_redirects=False)
        self.assertEqual(r.status_code, 302)
        self.assertIn('/login', r.headers['Location'])

    def test_changing_password_requires_current_password(self):
        """With the gate live. Inert, it is deliberately not required - RecoveryPathTests."""
        tok = self._csrf()
        self.client.post('/api/settings/password',
                         json={'new_password': 'first-password-1', 'confirm_password': 'first-password-1'},
                         headers={'X-CSRFToken': tok})
        tok = self._csrf()
        r = self.client.post('/api/settings/field', json={'path': 'auth.enabled', 'value': True},
                             headers={'X-CSRFToken': tok})
        self.assertEqual(r.status_code, 200)
        r = self.client.get('/login')
        tok = _csrf_field(r.get_data(as_text=True))
        r = self.client.post('/login', data={'password': 'first-password-1', 'csrf_token': tok},
                             follow_redirects=False)
        self.assertEqual(r.status_code, 302)

        tok = self._csrf()
        r = self.client.post('/api/settings/password',
                             json={'current_password': 'totally-wrong',
                                   'new_password': 'second-password-1',
                                   'confirm_password': 'second-password-1'},
                             headers={'X-CSRFToken': tok})
        self.assertEqual(r.status_code, 400)

        tok = self._csrf()
        r = self.client.post('/api/settings/password',
                             json={'current_password': 'first-password-1',
                                   'new_password': 'second-password-1',
                                   'confirm_password': 'second-password-1'},
                             headers={'X-CSRFToken': tok})
        self.assertEqual(r.status_code, 200)

    def test_mismatched_confirmation_is_rejected(self):
        tok = self._csrf()
        r = self.client.post('/api/settings/password',
                             json={'new_password': 'aaaaaaaaaa', 'confirm_password': 'bbbbbbbbbb'},
                             headers={'X-CSRFToken': tok})
        self.assertEqual(r.status_code, 400)


class DisableClearsPasswordTests(_ConfigSandbox):
    """Disabling auth.enabled must clear auth.password_hash too, so re-enabling later never
    traps the user behind a forgotten password they set once for testing (decided 2026-08-06)."""

    def setUp(self):
        super().setUp()
        self.t = make_test_app()
        self.client = self.t.app.test_client()
        self.addCleanup(self.t.cleanup)

    def _csrf(self):
        r = self.client.get('/settings')
        return _csrf_meta(r.get_data(as_text=True))

    def _set_password_and_enable(self, password):
        tok = self._csrf()
        r = self.client.post('/api/settings/password',
                             json={'new_password': password, 'confirm_password': password},
                             headers={'X-CSRFToken': tok})
        self.assertEqual(r.status_code, 200)
        tok = self._csrf()
        r = self.client.post('/api/settings/field', json={'path': 'auth.enabled', 'value': True},
                             headers={'X-CSRFToken': tok})
        self.assertEqual(r.status_code, 200)
        # The gate is now live for this same test client's session too - log in, or every
        # request after this point (including this test's own follow-up calls) 401s.
        r = self.client.get('/login')
        tok = _csrf_field(r.get_data(as_text=True))
        r = self.client.post('/login', data={'password': password, 'csrf_token': tok},
                             follow_redirects=False)
        self.assertEqual(r.status_code, 302)

    def test_disabling_without_current_password_is_rejected(self):
        self._set_password_and_enable(PASSWORD)
        tok = self._csrf()
        r = self.client.post('/api/settings/field', json={'path': 'auth.enabled', 'value': False},
                             headers={'X-CSRFToken': tok})
        self.assertEqual(r.status_code, 400)
        cfgmod._yaml_cache = None
        stored = cfgmod.load_config()
        self.assertTrue(stored['auth']['enabled'])
        self.assertTrue(stored['auth']['password_hash'])

    def test_disabling_with_wrong_current_password_is_rejected(self):
        self._set_password_and_enable(PASSWORD)
        tok = self._csrf()
        r = self.client.post('/api/settings/field',
                             json={'path': 'auth.enabled', 'value': False,
                                   'current_password': 'totally-wrong'},
                             headers={'X-CSRFToken': tok})
        self.assertEqual(r.status_code, 400)
        cfgmod._yaml_cache = None
        self.assertTrue(cfgmod.load_config()['auth']['enabled'])

    def test_disabling_with_correct_current_password_clears_the_hash(self):
        self._set_password_and_enable(PASSWORD)
        tok = self._csrf()
        r = self.client.post('/api/settings/field',
                             json={'path': 'auth.enabled', 'value': False,
                                   'current_password': PASSWORD},
                             headers={'X-CSRFToken': tok})
        self.assertEqual(r.status_code, 200)
        cfgmod._yaml_cache = None
        stored = cfgmod.load_config()
        self.assertFalse(stored['auth']['enabled'])
        self.assertEqual(stored['auth']['password_hash'], '')

    def test_reenabling_after_disable_requires_a_brand_new_password(self):
        self._set_password_and_enable(PASSWORD)
        tok = self._csrf()
        self.client.post('/api/settings/field',
                         json={'path': 'auth.enabled', 'value': False,
                               'current_password': PASSWORD},
                         headers={'X-CSRFToken': tok})
        # The old password no longer exists to enable with - and re-enabling with none set
        # is still rejected exactly as it is for a first-time setup.
        tok = self._csrf()
        r = self.client.post('/api/settings/field', json={'path': 'auth.enabled', 'value': True},
                             headers={'X-CSRFToken': tok})
        self.assertEqual(r.status_code, 400)

    def test_disabling_when_no_password_was_ever_set_needs_no_current_password(self):
        # Edge case: auth.enabled toggled false while already false / no hash exists (e.g. a
        # hand-edited config.yaml). Nothing to protect and nothing to clear, so this must not
        # demand a password that was never set.
        tok = self._csrf()
        r = self.client.post('/api/settings/field', json={'path': 'auth.enabled', 'value': False},
                             headers={'X-CSRFToken': tok})
        self.assertEqual(r.status_code, 200)


class MaskingTests(_ConfigSandbox):
    def setUp(self):
        super().setUp()
        self.pw_hash = generate_password_hash(PASSWORD)
        # Gate left off here on purpose - masking is a property of the config value, not
        # of gate state, and leaving it off keeps these pages reachable with no session.
        raw = {'auth': {'enabled': False, 'password_hash': self.pw_hash,
                        'session_timeout_minutes': 0, 'cookie_secure': False}}
        with open(self._cfg_path, 'w') as f:
            yaml.dump(raw, f)
        cfgmod._yaml_cache = None
        self.t = make_test_app()
        self.client = self.t.app.test_client()
        self.addCleanup(self.t.cleanup)

    def test_password_hash_masked_in_api_settings(self):
        r = self.client.get('/api/settings')
        data = r.get_json()
        self.assertEqual(data['auth']['password_hash'], cfgmod.MASK_SENTINEL)

    def test_password_hash_masked_in_yaml_editor_dump(self):
        r = self.client.get('/settings')
        html = r.get_data(as_text=True)
        self.assertNotIn(self.pw_hash, html)
        self.assertIn(cfgmod.MASK_SENTINEL, html)

    def test_password_hash_redacted_in_backup_diff(self):
        lines = [f'+  password_hash: {self.pw_hash}', '   enabled: true']
        out = cfgmod.redact_sensitive_diff_lines(lines)
        self.assertNotIn(self.pw_hash, '\n'.join(out))
        self.assertIn(cfgmod.MASK_SENTINEL, out[0])

    def test_password_hash_redacted_in_config_change_log(self):
        with self.assertLogs('app.config', level='INFO') as cm:
            cfgmod.record_config_changes(
                {'auth': {'password_hash': self.pw_hash}},
                {'auth': {'password_hash': 'a-different-hash'}},
            )
        joined = '\n'.join(cm.output)
        self.assertNotIn(self.pw_hash, joined)
        self.assertIn('<redacted>', joined)


class InertGateSurfaceTests(_ConfigSandbox):
    """auth.enabled on with no password_hash serves every route unauthenticated, and used
    to do it in total silence - no log line, no alert, and /login 404s, so the install was
    indistinguishable from one where auth was never configured (dev/changelog/726,
    dev/docs/BUGS.md 2026-08-18 @ 04:52:03 AM ET).

    The fail-open behavior itself is deliberate and stays; only the silence was the defect.
    Both write surfaces refuse to create this state, so every test here reaches it the way
    a real install does: by hand-editing config.yaml, which is exactly what README's
    forgotten-password recovery tells the user to do.
    """

    def _app_with_auth(self, auth_block):
        self._write_cfg({'auth': auth_block})
        self.t = make_test_app()
        self.addCleanup(self.t.cleanup)
        return self.t

    @staticmethod
    def _inert_alerts():
        return Alert.query.filter_by(alert_type=AUTH_GATE_INERT, dismissed_at=None).all()

    def test_startup_raises_one_alert_when_enabled_without_a_password(self):
        self._app_with_auth({'enabled': True, 'password_hash': ''})
        rows = self._inert_alerts()
        self.assertEqual(len(rows), 1, 'startup must raise exactly one standing alert')
        self.assertEqual(rows[0].severity, 'WARN')
        self.assertEqual(rows[0].source, authmod.GATE_INERT_SOURCE)
        # The wording has to say the app is NOT protected - an alert that only says
        # "misconfigured" leaves the reader unsure whether they are exposed right now.
        self.assertIn('not password-protected', rows[0].body.lower())

    def test_startup_logs_a_warning_naming_the_state(self):
        self._write_cfg({'auth': {'enabled': True, 'password_hash': ''}})
        with self.assertLogs('app.auth', level='WARNING') as cm:
            self.t = make_test_app()
            self.addCleanup(self.t.cleanup)
        joined = '\n'.join(cm.output).lower()
        self.assertIn('not password-protected', joined)

    def test_no_alert_when_the_gate_is_off(self):
        self._app_with_auth({'enabled': False, 'password_hash': ''})
        self.assertEqual(self._inert_alerts(), [])

    def test_no_alert_when_the_gate_is_fully_configured(self):
        self._app_with_auth({'enabled': True,
                             'password_hash': generate_password_hash(PASSWORD)})
        self.assertEqual(self._inert_alerts(), [])

    def test_repeated_reports_do_not_stack_rows(self):
        t = self._app_with_auth({'enabled': True, 'password_hash': ''})
        with t.app.app_context():
            for _ in range(4):
                authmod.report_gate_state(t.app.config['AUTH'], source='settings')
            self.assertEqual(len(self._inert_alerts()), 1)

    def test_setting_a_password_dismisses_the_standing_alert(self):
        """The recovery flow end to end: hand-edited config raises it at startup, and the
        settings password save clears it through refresh_auth()."""
        t = self._app_with_auth({'enabled': True, 'password_hash': ''})
        self.assertEqual(len(self._inert_alerts()), 1)

        client = t.app.test_client()
        # The gate is inert, so /settings is reachable with no session - which is the whole
        # point of the state, and what makes this recovery path work at all.
        tok = _csrf_meta(client.get('/settings').get_data(as_text=True))
        r = client.post('/api/settings/password',
                        json={'new_password': PASSWORD, 'confirm_password': PASSWORD},
                        headers={'X-CSRFToken': tok})
        self.assertEqual(r.status_code, 200, r.get_json())
        db.session.expire_all()  # the route committed in its own request context
        self.assertEqual(self._inert_alerts(), [])

    def test_predicate_is_the_exact_complement_of_gate_active(self):
        """gate_enabled_without_password() must never disagree with gate_active() about
        what counts as no password - a state both call 'not mine' is a fail-open one with
        nothing reporting it."""
        for hash_value in ('', None, generate_password_hash(PASSWORD)):
            for enabled in (True, False):
                cfg = {'enabled': enabled, 'password_hash': hash_value}
                inert = authmod.gate_enabled_without_password(cfg)
                if enabled:
                    self.assertEqual(inert, not authmod.gate_active(cfg), cfg)
                else:
                    self.assertFalse(inert, cfg)

    def test_report_without_an_app_context_still_logs_and_does_not_raise(self):
        """report_gate_state is called from create_app() and from every auth.* settings
        save; a caller with no app context must get the log line rather than a RuntimeError
        out of the alert query."""
        with self.assertLogs('app.auth', level='WARNING'):
            authmod.report_gate_state({'enabled': True, 'password_hash': ''},
                                      source='no-context')


if __name__ == '__main__':
    unittest.main()
