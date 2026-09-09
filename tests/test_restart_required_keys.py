"""flask.behind_proxy / flask.serve_mockups are restart-required, and say so on save.

Both are read exactly once, inside create_app() - behind_proxy wires ProxyFix, serve_mockups
registers the /mockups blueprint - but neither was in RESTART_REQUIRED_KEYS, so the raw
config.yaml editor wrote them and flashed the bare "Settings saved." with no restart notice.
The user got a success message for a change that had not taken effect and would not until an
unrelated restart happened to pick it up (dev/docs/BUGS.md 2026-08-12 09:31, dev/changelog/586).

ConfigSandbox, not a bare TestCase: save_config() writes _CONFIG_PATH and the /settings POST
route reaches it through a module-top import, so an unsandboxed run would rewrite the real
repo-root config.yaml (tests/support/config_sandbox.py explains why patching load_config is
not enough).
"""
import unittest

import yaml

from app.config import (RESTART_REQUIRED_KEYS, is_restart_needed, load_config,
                        record_config_changes, set_restart_needed)
from tests.support.app import make_test_app
from tests.support.config_sandbox import ConfigSandbox


def _cfg(**flask_overrides):
    base = {'port': 5000, 'host': '0.0.0.0', 'debug': False, 'behind_proxy': False,
            'serve_mockups': False}
    base.update(flask_overrides)
    return {'flask': base, 'auth': {'enabled': False, 'session_timeout_minutes': 0}}


class RestartFlagTests(unittest.TestCase):
    """record_config_changes() raises the restart flag for a create_app()-time key."""

    def setUp(self):
        set_restart_needed(False)
        self.addCleanup(set_restart_needed, False)

    def test_flipping_behind_proxy_flags_a_restart(self):
        record_config_changes(_cfg(), _cfg(behind_proxy=True))
        self.assertTrue(is_restart_needed())

    def test_flipping_serve_mockups_flags_a_restart(self):
        record_config_changes(_cfg(), _cfg(serve_mockups=True))
        self.assertTrue(is_restart_needed())

    def test_flipping_a_2026_08_13_audit_key_flags_a_restart(self):
        """One representative of the ten keys added in dev/changelog/619 - record_config_changes()

        diffs whatever leaves are present in both dicts, so a minimal single-section pair is
        enough; the full set is asserted for registration above."""
        record_config_changes({'recording': {'capture_log_dir': 'capture-logs'}},
                              {'recording': {'capture_log_dir': 'other-dir'}})
        self.assertTrue(is_restart_needed())

    def test_nav_poll_interval_seconds_does_not_flag_a_restart(self):
        """Control case for the eleventh audited candidate - it must NOT trip the flag."""
        record_config_changes({'display': {'nav_poll_interval_seconds': 15}},
                              {'display': {'nav_poll_interval_seconds': 30}})
        self.assertFalse(is_restart_needed())

    def test_a_key_that_takes_effect_live_does_not_flag_a_restart(self):
        """The control case: the flag is a branch, not stuck on.

        auth.enabled is deliberately NOT restart-required - app/auth.py::refresh_auth()
        re-reads app.config['AUTH'] on every write to it (app/config.py:47-51).
        """
        old = _cfg()
        new = _cfg()
        new['auth'] = {'enabled': True, 'session_timeout_minutes': 0}
        changed = record_config_changes(old, new)
        self.assertEqual([p for p, _, _ in changed], ['auth.enabled'])
        self.assertFalse(is_restart_needed())

    def test_both_keys_are_registered(self):
        self.assertIn('flask.behind_proxy', RESTART_REQUIRED_KEYS)
        self.assertIn('flask.serve_mockups', RESTART_REQUIRED_KEYS)

    def test_pidfile_path_is_registered(self):
        """flask.pidfile_path is resolved once into app.config['PIDFILE_PATH'] inside
        create_app() (dev/docs/BUGS.md 2026-08-14), same read-once shape as the two keys
        above - a write must flag the same restart notice."""
        self.assertIn('flask.pidfile_path', RESTART_REQUIRED_KEYS)

    def test_the_2026_08_13_audit_keys_are_registered(self):
        """dev/changelog/619: eleven more create_app()-time keys were audited; ten were

        genuine misses (each read exactly once inside create_app(), same defect class as
        behind_proxy/serve_mockups above) and are asserted here.
        """
        for path in (
            'database.wal_size_limit_mb',
            'database.pool_size',
            'database.max_overflow',
            'database.pool_timeout',
            'database.background_pool_size',
            'database.background_max_overflow',
            'database.background_pool_timeout',
            'recording.capture_log_dir',
            'logging.max_bytes',
            'logging.backup_count',
        ):
            self.assertIn(path, RESTART_REQUIRED_KEYS)

    def test_nav_poll_interval_seconds_is_deliberately_excluded(self):
        """The eleventh audited candidate: it looked like a sibling of the keys above, but

        it's read inside inject_globals(), a context processor that calls load_config()
        fresh on every request - so it already takes effect live and must NOT be flagged.
        """
        self.assertNotIn('display.nav_poll_interval_seconds', RESTART_REQUIRED_KEYS)


class RawYamlEditorFlashTests(ConfigSandbox):
    """The user-visible symptom: which flash the config.yaml tab shows after the save.

    Every POST here submits the *whole* merged config with one leaf changed, the way the
    editor's own textarea does. A partial dict would make every absent leaf read as a
    change - including restart-required ones like flask.secret_key - and the restart flash
    would then appear no matter which key the test flipped.
    """

    def setUp(self):
        super().setUp()
        self._write_cfg(_cfg())
        set_restart_needed(False)
        self.addCleanup(set_restart_needed, False)
        self.t = make_test_app()
        self.t.app.config['WTF_CSRF_ENABLED'] = False
        self.addCleanup(self.t.cleanup)
        self.client = self.t.app.test_client()

    def _post_with(self, section, key, value):
        cfg = load_config()
        cfg[section][key] = value
        html = self.client.post('/settings', data={'config_yaml': yaml.dump(cfg)},
                                follow_redirects=True).get_data(as_text=True)
        self.assertIn('Settings saved.', html)  # the save itself must have succeeded
        return html

    def test_saving_behind_proxy_tells_the_user_to_restart(self):
        html = self._post_with('flask', 'behind_proxy', True)
        self.assertIn('Restart the service for all changes to take effect.', html)

    def test_saving_serve_mockups_tells_the_user_to_restart(self):
        html = self._post_with('flask', 'serve_mockups', True)
        self.assertIn('Restart the service for all changes to take effect.', html)

    def test_a_live_effect_key_saves_without_a_restart_notice(self):
        """Control: the flash discriminates. auth.session_timeout_minutes is re-read live
        by app/auth.py::refresh_auth(), so it must not ask for a restart."""
        html = self._post_with('auth', 'session_timeout_minutes', 45)
        self.assertNotIn('Restart the service for all changes to take effect.', html)


class SettingsSystemCardTests(ConfigSandbox):
    """flask.behind_proxy is visible in the GUI at all.

    It was readable only by opening the raw YAML - the System card rendered port, host,
    debug, secret_key and database.path but not this one (dev/changelog/586).
    """

    def setUp(self):
        super().setUp()
        self._write_cfg(_cfg(behind_proxy=True))
        self.t = make_test_app()
        self.addCleanup(self.t.cleanup)
        self.html = self.t.app.test_client().get('/settings').get_data(as_text=True)

    def test_the_system_card_shows_behind_proxy(self):
        self.assertIn('data-path="flask.behind_proxy"', self.html)
        self.assertIn('Behind a reverse proxy', self.html)

    def test_it_renders_the_live_value(self):
        row = self.html.split('data-path="flask.behind_proxy"', 1)[1].split('</div>\n</div>', 1)[0]
        self.assertIn('value="true"', row)


if __name__ == '__main__':
    unittest.main()
