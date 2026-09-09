"""Settings secret masking + round-trip (DESIGN-secrets.md §6).

Guards the invariant that every sensitive config leaf (_is_sensitive_path: flask.secret_key +
notifications.services.<name>.url) is masked on every settings READ surface, and that saving a
masked read back unchanged is a no-op - the MASK_SENTINEL never overwrites a stored secret.

No BUGS.md entry (feature/hardening, matching the other DESIGN-secrets.md items); these are
regression guards for the round-trip invariant, proven to fail with the fix stashed.
"""
import os
import tempfile
import unittest
from unittest.mock import patch

import yaml

from app import config as cfgmod
from app.config import (MASK_SENTINEL, mask_config, restore_masked_secrets,
                        redact_sensitive_diff_lines, save_config)
from tests.support.app import make_test_app


def _sample_cfg():
    return {
        'flask': {'secret_key': 'topsecret-key', 'port': 5000},
        'notifications': {
            'base_url': 'http://dvr.local',
            'push_rate_limit_seconds': 60,
            'services': {
                'pushover': {'enabled': True, 'url': 'pover://tok3n@app/'},
                'discord': {'enabled': False, 'url': ''},
            },
        },
    }


class MaskConfigTests(unittest.TestCase):
    def test_masks_every_sensitive_leaf_with_a_value(self):
        m = mask_config(_sample_cfg())
        self.assertEqual(m['flask']['secret_key'], MASK_SENTINEL)
        self.assertEqual(m['notifications']['services']['pushover']['url'], MASK_SENTINEL)

    def test_leaves_empty_secret_and_nonsensitive_leaves_untouched(self):
        m = mask_config(_sample_cfg())
        # empty secret stays empty so the UI still reads it as "not set"
        self.assertEqual(m['notifications']['services']['discord']['url'], '')
        # base_url is a different key token, not a sensitive leaf
        self.assertEqual(m['notifications']['base_url'], 'http://dvr.local')
        self.assertEqual(m['flask']['port'], 5000)

    def test_does_not_mutate_the_input(self):
        cfg = _sample_cfg()
        mask_config(cfg)
        self.assertEqual(cfg['flask']['secret_key'], 'topsecret-key')


class RoundTripTests(unittest.TestCase):
    def test_masked_read_saved_back_unchanged_is_a_noop(self):
        """The core §6 invariant: restore(mask(cfg), cfg) == cfg."""
        cfg = _sample_cfg()
        submitted = mask_config(cfg)  # what a read surface hands back
        restore_masked_secrets(submitted, cfg)
        self.assertEqual(submitted, cfg)

    def test_sentinel_never_overwrites_a_stored_secret(self):
        stored = _sample_cfg()
        submitted = {'flask': {'secret_key': MASK_SENTINEL}}
        restore_masked_secrets(submitted, stored)
        self.assertEqual(submitted['flask']['secret_key'], 'topsecret-key')

    def test_a_real_new_value_is_written_through(self):
        stored = _sample_cfg()
        submitted = {'flask': {'secret_key': 'brand-new-secret'}}
        restore_masked_secrets(submitted, stored)
        self.assertEqual(submitted['flask']['secret_key'], 'brand-new-secret')

    def test_clearing_with_empty_string_is_preserved(self):
        stored = _sample_cfg()
        submitted = {'flask': {'secret_key': ''}}
        restore_masked_secrets(submitted, stored)
        self.assertEqual(submitted['flask']['secret_key'], '')

    def test_sentinel_for_unset_leaf_stores_empty_and_warns(self):
        stored = {'flask': {'secret_key': ''}}
        submitted = {'flask': {'secret_key': MASK_SENTINEL}}
        with self.assertLogs('app.config', level='WARNING') as cm:
            restore_masked_secrets(submitted, stored)
        self.assertEqual(submitted['flask']['secret_key'], '')
        self.assertTrue(any('flask.secret_key' in line for line in cm.output))

    def test_non_sensitive_leaf_equal_to_sentinel_is_left_alone(self):
        # A non-secret field literally set to the sentinel string is a real value, not a mask.
        stored = {'recording': {'filename_template': 'old'}}
        submitted = {'recording': {'filename_template': MASK_SENTINEL}}
        restore_masked_secrets(submitted, stored)
        self.assertEqual(submitted['recording']['filename_template'], MASK_SENTINEL)


class SaveConfigIntegrationTests(unittest.TestCase):
    """save_config() centralizes the round-trip, so every write surface inherits it."""

    def setUp(self):
        fd, self._path = tempfile.mkstemp(suffix='.yaml')
        os.close(fd)
        with open(self._path, 'w') as f:
            yaml.dump(_sample_cfg(), f)
        self._patch = patch.object(cfgmod, '_CONFIG_PATH', self._path)
        self._patch.start()
        cfgmod._yaml_cache = None

    def tearDown(self):
        self._patch.stop()
        cfgmod._yaml_cache = None
        os.remove(self._path)

    def _stored(self):
        cfgmod._yaml_cache = None
        with open(self._path) as f:
            return yaml.safe_load(f)

    def test_saving_masked_config_keeps_the_real_secret_on_disk(self):
        masked = mask_config(cfgmod.load_config())
        save_config(masked)
        stored = self._stored()
        self.assertEqual(stored['flask']['secret_key'], 'topsecret-key')
        self.assertEqual(stored['notifications']['services']['pushover']['url'], 'pover://tok3n@app/')

    def test_saving_masked_config_is_a_noop_change(self):
        masked = mask_config(cfgmod.load_config())
        changed = save_config(masked)
        # No leaf actually changed - the mask string never reached disk.
        self.assertEqual(changed, [])

    def test_a_real_edit_alongside_a_masked_secret_persists_the_edit_only(self):
        masked = mask_config(cfgmod.load_config())
        masked['notifications']['push_rate_limit_seconds'] = 120  # real edit
        save_config(masked)
        stored = self._stored()
        self.assertEqual(stored['notifications']['push_rate_limit_seconds'], 120)
        self.assertEqual(stored['flask']['secret_key'], 'topsecret-key')  # secret intact


class DiffRedactionTests(unittest.TestCase):
    def test_redacts_secret_key_and_service_url_lines_only(self):
        lines = [
            '--- config.yaml (current)',
            '+++ backup (backup)',
            ' flask:',
            '-  secret_key: oldsecret',
            '+  secret_key: newsecret',
            '   base_url: http://keep.me',      # non-sensitive, kept
            '-      url: pover://tok@x/',        # service url, redacted
            '+      url: pover://new@y/',
            '   port: 5000',                     # non-sensitive, kept
        ]
        out = redact_sensitive_diff_lines(lines)
        self.assertEqual(out[3], f'-  secret_key: {MASK_SENTINEL}')
        self.assertEqual(out[4], f'+  secret_key: {MASK_SENTINEL}')
        self.assertEqual(out[5], '   base_url: http://keep.me')
        self.assertEqual(out[6], f'-      url: {MASK_SENTINEL}')
        self.assertEqual(out[7], f'+      url: {MASK_SENTINEL}')
        self.assertEqual(out[8], '   port: 5000')


class ReadSurfaceRouteTests(unittest.TestCase):
    """The four settings READ surfaces render MASK_SENTINEL, never the raw secret."""

    def setUp(self):
        self.t = make_test_app()

    def tearDown(self):
        self.t.cleanup()

    def test_api_settings_json_is_masked(self):
        with patch('app.routes.settings.load_config', return_value=_sample_cfg()):
            resp = self.t.client.get('/api/settings')
        body = resp.get_json()
        self.assertEqual(body['flask']['secret_key'], MASK_SENTINEL)
        self.assertEqual(body['notifications']['services']['pushover']['url'], MASK_SENTINEL)
        self.assertNotIn('topsecret-key', resp.get_data(as_text=True))
        self.assertNotIn('tok3n', resp.get_data(as_text=True))

    def test_settings_yaml_editor_dump_is_masked(self):
        with patch('app.routes.settings.load_config', return_value=_sample_cfg()):
            resp = self.t.client.get('/settings')
        text = resp.get_data(as_text=True)
        self.assertNotIn('topsecret-key', text)
        self.assertNotIn('tok3n', text)
        self.assertIn(MASK_SENTINEL, text)

    def test_notifications_service_url_input_is_masked(self):
        with patch('app.routes.settings.load_config', return_value=_sample_cfg()):
            resp = self.t.client.get('/settings/notifications')
        text = resp.get_data(as_text=True)
        self.assertNotIn('tok3n', text)
        self.assertIn(MASK_SENTINEL, text)

    def test_diff_route_redacts_secret_lines(self):
        diff = ['-  secret_key: leaked', '+  secret_key: alsoleaked',
                '-      url: pover://leak@x/']
        with patch('app.config_backup.list_backups',
                   return_value=[{'filename': 'b.yaml', 'path': '/tmp/b.yaml'}]), \
             patch('app.config_backup.get_diff', return_value=diff):
            resp = self.t.client.get('/api/settings/diff/b.yaml')
        text = resp.get_data(as_text=True)
        self.assertNotIn('leaked', text)
        self.assertNotIn('leak@x', text)
        self.assertIn(MASK_SENTINEL, text)


class RevealEndpointTests(unittest.TestCase):
    """GET /api/settings/reveal returns the real secret, gated to sensitive leaves only."""

    def setUp(self):
        self.t = make_test_app()

    def tearDown(self):
        self.t.cleanup()

    def test_reveals_a_sensitive_leaf_value(self):
        with patch('app.routes.settings.load_config', return_value=_sample_cfg()):
            resp = self.t.client.get(
                '/api/settings/reveal?path=notifications.services.pushover.url')
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.get_json()['value'], 'pover://tok3n@app/')

    def test_reveals_secret_key(self):
        with patch('app.routes.settings.load_config', return_value=_sample_cfg()):
            resp = self.t.client.get('/api/settings/reveal?path=flask.secret_key')
        self.assertEqual(resp.get_json()['value'], 'topsecret-key')

    def test_rejects_a_non_sensitive_path(self):
        with patch('app.routes.settings.load_config', return_value=_sample_cfg()):
            resp = self.t.client.get('/api/settings/reveal?path=notifications.base_url')
        self.assertEqual(resp.status_code, 400)
        self.assertIn('error', resp.get_json())

    def test_rejects_missing_path(self):
        resp = self.t.client.get('/api/settings/reveal')
        self.assertEqual(resp.status_code, 400)

    def test_unset_sensitive_leaf_reveals_empty_string(self):
        with patch('app.routes.settings.load_config', return_value=_sample_cfg()):
            resp = self.t.client.get(
                '/api/settings/reveal?path=notifications.services.discord.url')
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.get_json()['value'], '')


if __name__ == '__main__':
    unittest.main()
