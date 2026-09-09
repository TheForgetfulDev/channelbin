"""Home Assistant integration: API-key auth + config + Settings UI
(app/routes/ha.py, app/routes/settings.py's ha-api-key endpoint, app/config.py's
integrations.home_assistant defaults).

Covers: config defaults, api_key_hash masking (settings JSON, yaml dump, backup-diff
redaction), the generate/regenerate endpoint (show-once plaintext, hash-only storage,
regeneration invalidates the old key), the server-side enable-requires-key guard, that
disabling deliberately does NOT clear the stored key (unlike auth.password_hash - the HA
side keeps the plaintext and should keep working if re-enabled), and the ha blueprint's
own before_request gate (missing/wrong/disabled key all 401 generically; correct key 200;
gated independently of - and reachable through - the interactive session/password gate).

Runs against a throwaway temp SQLite DB and a sandboxed config.yaml - never the live
dvr.db/config.yaml (tests/support/config_sandbox.py::ConfigSandbox).
  python3 -m unittest tests.test_ha_auth
"""
import re
import unittest

from werkzeug.security import check_password_hash, generate_password_hash

from app import config as cfgmod
from tests.support.app import make_test_app
from tests.support.config_sandbox import ConfigSandbox

PASSWORD = 'correcthorse-battery-staple'


_ConfigSandbox = ConfigSandbox


def _csrf_meta(html):
    m = re.search(r'name="csrf-token" content="([^"]+)"', html)
    return m.group(1) if m else None


class ConfigDefaultsTests(_ConfigSandbox):
    """Sandboxed, not bare: a plain load_config() here reads the real repo-root
    config.yaml, so this asserted "the defaults are off" against whatever the developer's
    own machine happened to have enabled (dev/changelog/515)."""

    def test_defaults_are_off_with_no_key(self):
        cfg = cfgmod.load_config()
        self.assertEqual(cfg['integrations']['home_assistant'],
                         {'enabled': False, 'api_key_hash': ''})

    def test_api_key_hash_is_sensitive(self):
        self.assertTrue(cfgmod._is_sensitive_path('integrations.home_assistant.api_key_hash'))


class MaskingTests(_ConfigSandbox):
    def setUp(self):
        super().setUp()
        self.key_hash = generate_password_hash('some-generated-key')
        self._write_cfg({'integrations': {'home_assistant': {'enabled': True,
                                                              'api_key_hash': self.key_hash}}})
        self.t = make_test_app()
        self.client = self.t.app.test_client()
        self.addCleanup(self.t.cleanup)

    def test_masked_in_api_settings(self):
        r = self.client.get('/api/settings')
        data = r.get_json()
        self.assertEqual(data['integrations']['home_assistant']['api_key_hash'], cfgmod.MASK_SENTINEL)

    def test_masked_in_yaml_editor_dump(self):
        r = self.client.get('/settings')
        html = r.get_data(as_text=True)
        self.assertNotIn(self.key_hash, html)
        self.assertIn(cfgmod.MASK_SENTINEL, html)

    def test_redacted_in_backup_diff(self):
        lines = [f'+    api_key_hash: {self.key_hash}', '     enabled: true']
        out = cfgmod.redact_sensitive_diff_lines(lines)
        self.assertNotIn(self.key_hash, '\n'.join(out))
        self.assertIn(cfgmod.MASK_SENTINEL, out[0])


class GenerateKeyEndpointTests(_ConfigSandbox):
    def setUp(self):
        super().setUp()
        self.t = make_test_app()
        self.client = self.t.app.test_client()
        self.addCleanup(self.t.cleanup)

    def _csrf(self):
        return _csrf_meta(self.client.get('/settings').get_data(as_text=True))

    def test_generate_returns_plaintext_once_and_stores_only_a_hash(self):
        tok = self._csrf()
        r = self.client.post('/api/settings/ha-api-key', headers={'X-CSRFToken': tok})
        self.assertEqual(r.status_code, 200)
        data = r.get_json()
        self.assertTrue(data['success'])
        api_key = data['api_key']
        self.assertEqual(len(api_key), 64)  # secrets.token_hex(32)

        cfgmod._yaml_cache = None
        stored = cfgmod.load_config()['integrations']['home_assistant']['api_key_hash']
        self.assertNotEqual(stored, api_key)
        self.assertTrue(check_password_hash(stored, api_key))

    def test_regenerating_invalidates_the_old_key(self):
        tok = self._csrf()
        r1 = self.client.post('/api/settings/ha-api-key', headers={'X-CSRFToken': tok})
        old_key = r1.get_json()['api_key']

        tok = self._csrf()
        r2 = self.client.post('/api/settings/ha-api-key', headers={'X-CSRFToken': tok})
        new_key = r2.get_json()['api_key']

        self.assertNotEqual(old_key, new_key)
        cfgmod._yaml_cache = None
        stored = cfgmod.load_config()['integrations']['home_assistant']['api_key_hash']
        self.assertFalse(check_password_hash(stored, old_key))
        self.assertTrue(check_password_hash(stored, new_key))


class EnableRequiresKeyTests(_ConfigSandbox):
    """Enforcement lives server-side (CLAUDE.md): the Settings toggle is disabled
    client-side with no key, but the route must refuse it too."""

    def setUp(self):
        super().setUp()
        self.t = make_test_app()
        self.client = self.t.app.test_client()
        self.addCleanup(self.t.cleanup)

    def _csrf(self):
        return _csrf_meta(self.client.get('/settings').get_data(as_text=True))

    def test_enabling_with_no_key_is_rejected(self):
        tok = self._csrf()
        r = self.client.post('/api/settings/field',
                             json={'path': 'integrations.home_assistant.enabled', 'value': True},
                             headers={'X-CSRFToken': tok})
        self.assertEqual(r.status_code, 400)
        cfgmod._yaml_cache = None
        self.assertFalse(cfgmod.load_config()['integrations']['home_assistant']['enabled'])

    def test_enabling_after_generating_a_key_succeeds(self):
        tok = self._csrf()
        self.client.post('/api/settings/ha-api-key', headers={'X-CSRFToken': tok})
        tok = self._csrf()
        r = self.client.post('/api/settings/field',
                             json={'path': 'integrations.home_assistant.enabled', 'value': True},
                             headers={'X-CSRFToken': tok})
        self.assertEqual(r.status_code, 200)

    def test_disabling_does_not_clear_the_stored_key(self):
        """Deliberately different from auth.enabled: HA keeps the plaintext key in its own
        config, so re-enabling later should keep working with it, not force a fresh key."""
        tok = self._csrf()
        self.client.post('/api/settings/ha-api-key', headers={'X-CSRFToken': tok})
        cfgmod._yaml_cache = None
        key_hash = cfgmod.load_config()['integrations']['home_assistant']['api_key_hash']

        tok = self._csrf()
        self.client.post('/api/settings/field',
                         json={'path': 'integrations.home_assistant.enabled', 'value': True},
                         headers={'X-CSRFToken': tok})
        tok = self._csrf()
        r = self.client.post('/api/settings/field',
                             json={'path': 'integrations.home_assistant.enabled', 'value': False},
                             headers={'X-CSRFToken': tok})
        self.assertEqual(r.status_code, 200)

        cfgmod._yaml_cache = None
        self.assertEqual(cfgmod.load_config()['integrations']['home_assistant']['api_key_hash'], key_hash)


class HaBlueprintGateTests(_ConfigSandbox):
    """The independent X-API-Key gate on app/routes/ha.py's own before_request."""

    API_KEY = 'a-real-generated-ha-key'

    def setUp(self):
        super().setUp()
        self._write_cfg({'integrations': {'home_assistant': {
            'enabled': True, 'api_key_hash': generate_password_hash(self.API_KEY)}}})
        self.t = make_test_app()
        self.client = self.t.app.test_client()
        self.addCleanup(self.t.cleanup)

    def test_missing_key_is_401(self):
        r = self.client.get('/api/ha/v1/ping')
        self.assertEqual(r.status_code, 401)
        self.assertIn('error', r.get_json())

    def test_wrong_key_is_401(self):
        r = self.client.get('/api/ha/v1/ping', headers={'X-API-Key': 'not-the-key'})
        self.assertEqual(r.status_code, 401)

    def test_correct_key_is_200(self):
        r = self.client.get('/api/ha/v1/ping', headers={'X-API-Key': self.API_KEY})
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.get_json()['success'])

    def test_disabled_integration_denies_even_the_correct_key(self):
        self._write_cfg({'integrations': {'home_assistant': {
            'enabled': False, 'api_key_hash': generate_password_hash(self.API_KEY)}}})
        r = self.client.get('/api/ha/v1/ping', headers={'X-API-Key': self.API_KEY})
        self.assertEqual(r.status_code, 401)

    def test_no_key_configured_denies_everything(self):
        self._write_cfg({'integrations': {'home_assistant': {'enabled': True, 'api_key_hash': ''}}})
        r = self.client.get('/api/ha/v1/ping', headers={'X-API-Key': 'anything'})
        self.assertEqual(r.status_code, 401)


class HaBypassesSessionGateTests(_ConfigSandbox):
    """The ha blueprint must be reachable with a valid API key even while the interactive
    session/password gate is ALSO on - and still 401 (not a 302 login redirect) with no
    key, proving its own gate governs it rather than the session gate silently passing it
    through unauthenticated."""

    API_KEY = 'a-real-generated-ha-key'

    def setUp(self):
        super().setUp()
        self._write_cfg({
            'auth': {'enabled': True, 'password_hash': generate_password_hash(PASSWORD),
                     'session_timeout_minutes': 0},
            'integrations': {'home_assistant': {
                'enabled': True, 'api_key_hash': generate_password_hash(self.API_KEY)}},
        })
        self.t = make_test_app()
        self.client = self.t.app.test_client()
        self.addCleanup(self.t.cleanup)

    def test_valid_api_key_reaches_the_route_with_no_browser_session(self):
        r = self.client.get('/api/ha/v1/ping', headers={'X-API-Key': self.API_KEY})
        self.assertEqual(r.status_code, 200)

    def test_no_api_key_is_401_not_a_login_redirect(self):
        r = self.client.get('/api/ha/v1/ping', follow_redirects=False)
        self.assertEqual(r.status_code, 401)
        self.assertNotIn('location', {k.lower() for k in r.headers.keys()})


if __name__ == '__main__':
    unittest.main()
