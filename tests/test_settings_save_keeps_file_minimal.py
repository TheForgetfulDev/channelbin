"""A GUI settings save persists what the user chose, not the whole default set.

Guards dev/docs/BUGS.md 2026-08-18: every GUI write path did `cfg = load_config()` (the
merged defaults+file dict), mutated one leaf, then `save_config(cfg)` - so one toggle in
Settings wrote all ~170 keys into config.yaml, including every value the user never
touched. Two consequences, both silent: a later change to a shipped default can never
reach an install that has saved once, and the deliberately-minimal
docker/config.docker.yaml becomes a full dump the first time anybody uses the UI.

The second half of the same entry: save_config() diffed its raw `data` argument against
the merged old config, so handing it the sparse file dict reported every unset key as
removed - ~170 phantom "Config changed" lines and a false restart banner, the same defect
dev/changelog/110 fixed. That was already live on the one path (filename_template_save_api)
that had the read right; fixing the other five without it would have spread it.

Everything here runs against a ConfigSandbox temp config.yaml, never the real file.
"""
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import app.config as cfgmod  # noqa: E402
from tests.support.app import make_test_app  # noqa: E402
from tests.support.config_sandbox import ConfigSandbox  # noqa: E402


def _leaves(data):
    return {path for path, _ in cfgmod._flatten(data)}


class _SavingRoutes(ConfigSandbox):
    """A test app over a deliberately minimal sandboxed config.yaml."""

    # Only what a container's seed file carries - the point is that everything else is
    # supplied by _DEFAULTS and must STAY there rather than being copied into the file.
    MINIMAL = {
        'config_version': cfgmod.CURRENT_CONFIG_VERSION,
        'display': {'timezone': 'America/New_York'},
    }

    def setUp(self):
        super().setUp()
        self._write_cfg(self.MINIMAL)
        self.t = make_test_app()
        self.t.app.config['WTF_CSRF_ENABLED'] = False
        self.addCleanup(self.t.cleanup)
        self.client = self.t.app.test_client()
        cfgmod.set_restart_needed(False)
        self.addCleanup(cfgmod.set_restart_needed, False)

    def file_cfg(self):
        """The raw config.yaml as stored, bypassing the merge with _DEFAULTS."""
        cfgmod._yaml_cache = None
        return cfgmod._load_config_file() or {}


class FieldSaveTests(_SavingRoutes):

    def test_a_field_save_does_not_bake_the_defaults_into_the_file(self):
        resp = self.client.post('/api/settings/field',
                                json={'path': 'display.time_format', 'value': '24h'})
        self.assertEqual(resp.status_code, 200)
        stored = _leaves(self.file_cfg())
        self.assertEqual(
            stored, _leaves(self.MINIMAL) | {'display.time_format'},
            'the save wrote keys the user never set - config.yaml must hold only what was '
            'chosen, so a later change to a shipped default still reaches this install')

    def test_the_saved_leaf_actually_persists(self):
        """Control: the assertion above is not satisfied by writing nothing at all."""
        self.client.post('/api/settings/field',
                         json={'path': 'display.time_format', 'value': '24h'})
        self.assertEqual(self.file_cfg()['display']['time_format'], '24h')
        self.assertEqual(cfgmod.load_config()['display']['time_format'], '24h')

    def test_an_unsaved_default_still_tracks_a_later_change_to_it(self):
        """The whole point of the item: a default the user never touched must keep
        flowing through from _DEFAULTS after an unrelated save, not be frozen at the
        value it happened to have on the day of that save."""
        self.client.post('/api/settings/field',
                         json={'path': 'display.time_format', 'value': '24h'})
        self.assertNotIn('watchdog', self.file_cfg(),
                         'an untouched section was written into the file')
        with mock.patch.dict(cfgmod._DEFAULTS['watchdog'],
                             {'stall_timeout_seconds': 999}):
            cfgmod._yaml_cache = None
            self.assertEqual(cfgmod.load_config()['watchdog']['stall_timeout_seconds'], 999)

    def test_one_field_save_reports_exactly_one_change(self):
        """Pins the route end of the phantom-change defect. It does NOT fail against the
        pre-fix code - a merged-dict save diffed against the merged old config reported
        one change too, and the flood only appears once a sparse dict reaches
        save_config(). SaveConfigDiffTests below carries the actual proof; this guards
        the combination from regressing later."""
        with mock.patch.object(cfgmod, 'log') as spy:
            self.client.post('/api/settings/field',
                             json={'path': 'display.time_format', 'value': '24h'})
        changed = [c for c in spy.info.call_args_list if 'Config changed' in c.args[0]]
        self.assertEqual(len(changed), 1,
                         f'expected one Config changed line, got {len(changed)}: {changed}')

    def test_a_field_save_does_not_raise_a_false_restart_banner(self):
        """display.time_format is not restart-required, and neither is anything else this
        save touches - but a diff of the sparse file dict against the merged old config
        reports every unset key as removed, several of which ARE restart-required.

        Same status as the test above: pins the route end, does not fail against the
        pre-fix code. SaveConfigDiffTests is where the defect itself is proven."""
        self.client.post('/api/settings/field',
                         json={'path': 'display.time_format', 'value': '24h'})
        self.assertFalse(cfgmod.is_restart_needed())

    def test_a_restart_required_key_still_raises_the_banner(self):
        """Control: the flag is still a branch, not stuck off."""
        self.client.post('/api/settings/field',
                         json={'path': 'logging.level', 'value': 'DEBUG'})
        self.assertTrue(cfgmod.is_restart_needed())

    def test_validation_still_reads_values_that_exist_only_as_defaults(self):
        """The route validates against the merged config on purpose: the window bounds are
        not in this file, so a file-dict-only read would see the other bound as absent and
        wave through the zero-length window it is supposed to reject."""
        default_start = cfgmod._DEFAULTS['channel_testing']['window']['start']
        self.assertNotIn('channel_testing', self.file_cfg())
        resp = self.client.post('/api/settings/field',
                                json={'path': 'channel_testing.window.end',
                                      'value': default_start})
        self.assertEqual(resp.status_code, 400)
        self.assertIn('cannot be the same', resp.get_json()['error'])


class SecretSaveTests(_SavingRoutes):

    def test_password_save_stores_only_the_hash_leaf(self):
        resp = self.client.post('/api/settings/password',
                                json={'new_password': 'hunter2hunter2',
                                      'confirm_password': 'hunter2hunter2'})
        self.assertEqual(resp.status_code, 200)
        stored = self.file_cfg()
        self.assertEqual(_leaves(stored),
                         _leaves(self.MINIMAL) | {'auth.password_hash'})
        self.assertTrue(stored['auth']['password_hash'])

    def test_a_masked_secret_round_trip_is_still_a_no_op(self):
        """save_config()'s MASK_SENTINEL restore is inherited by the raw-file-dict write
        path too - a masked read saved back must never overwrite the stored secret."""
        self.client.post('/api/settings/password',
                         json={'new_password': 'hunter2hunter2',
                               'confirm_password': 'hunter2hunter2'})
        real_hash = self.file_cfg()['auth']['password_hash']
        resp = self.client.post('/api/settings/field',
                                json={'path': 'auth.password_hash',
                                      'value': cfgmod.MASK_SENTINEL})
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(self.file_cfg()['auth']['password_hash'], real_hash)

    def test_ha_api_key_save_stores_only_the_hash_leaf(self):
        resp = self.client.post('/api/settings/ha-api-key', json={})
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(
            _leaves(self.file_cfg()),
            _leaves(self.MINIMAL) | {'integrations.home_assistant.api_key_hash'})


class NotificationSaveTests(_SavingRoutes):

    def test_service_save_stores_only_that_service(self):
        resp = self.client.post('/api/notifications/services/pushover',
                                json={'enabled': True, 'url': 'pover://tok@user'})
        self.assertEqual(resp.status_code, 200)
        stored = self.file_cfg()
        self.assertEqual(_leaves(stored), _leaves(self.MINIMAL) | {
            'notifications.services.pushover.enabled',
            'notifications.services.pushover.url'})
        self.assertNotIn('discord', stored['notifications']['services'],
                         'an untouched sibling service was written into the file')

    def test_service_save_still_rejects_the_placeholder_url(self):
        """Control: the placeholder guard (dev/docs/BUGS.md 2026-08-11 09:00 PM) survives
        the switch to the raw file dict."""
        from app.notifications import SERVICE_URL_HINTS
        resp = self.client.post('/api/notifications/services/pushover',
                                json={'url': SERVICE_URL_HINTS['pushover']})
        self.assertEqual(resp.status_code, 400)
        self.assertNotIn('notifications', self.file_cfg())

    def test_routing_save_stores_only_the_posted_rows(self):
        resp = self.client.post('/api/notifications/routing',
                                json={'LOG_ERROR': {'in_app': False,
                                                    'push_services': ['pushover']}})
        self.assertEqual(resp.status_code, 200)
        routing = self.file_cfg()['notifications']['routing']
        self.assertEqual(list(routing), ['LOG_ERROR'],
                         'the untouched default routing rows were written into the file')
        self.assertEqual(routing['LOG_ERROR'],
                         {'in_app': False, 'push_services': ['pushover']})

    def test_rate_limit_save_stores_only_that_leaf(self):
        resp = self.client.post('/api/notifications/rate-limit', json={'seconds': 90})
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(_leaves(self.file_cfg()),
                         _leaves(self.MINIMAL) | {'notifications.push_rate_limit_seconds'})

    def test_service_removal_stores_only_that_service(self):
        self.client.post('/api/notifications/services/pushover',
                         json={'enabled': True, 'url': 'pover://tok@user'})
        resp = self.client.delete('/api/notifications/services/pushover')
        self.assertEqual(resp.status_code, 200)
        stored = self.file_cfg()
        self.assertEqual(stored['notifications']['services']['pushover'],
                         {'enabled': False, 'url': ''})
        self.assertNotIn('routing', stored['notifications'],
                         'removal wrote the default routing rows into the file')


class SaveConfigDiffTests(ConfigSandbox):
    """save_config()'s change list is effective-against-effective, whatever shape of dict
    it is handed - the sparse file dict is a legitimate argument (load_for_edit)."""

    def setUp(self):
        super().setUp()
        cfgmod.set_restart_needed(False)
        self.addCleanup(cfgmod.set_restart_needed, False)

    def test_a_sparse_argument_reports_only_the_real_change(self):
        self._write_cfg({'config_version': cfgmod.CURRENT_CONFIG_VERSION,
                         'display': {'time_format': '12h'}})
        raw = cfgmod._load_config_file()
        raw['display']['time_format'] = '24h'
        changed = cfgmod.save_config(raw)
        self.assertEqual([p for p, _, _ in changed], ['display.time_format'])
        self.assertFalse(cfgmod.is_restart_needed())

    def test_dropping_a_key_reports_the_default_it_falls_back_to(self):
        """A key removed from config.yaml means "use the default", so the change is
        old value -> default, not old value -> nothing."""
        self._write_cfg({'config_version': cfgmod.CURRENT_CONFIG_VERSION,
                         'logging': {'level': 'DEBUG'}})
        changed = cfgmod.save_config(
            {'config_version': cfgmod.CURRENT_CONFIG_VERSION})
        self.assertEqual(changed,
                         [('logging.level', 'DEBUG', cfgmod._DEFAULTS['logging']['level'])])
        self.assertTrue(cfgmod.is_restart_needed())

    def test_saving_an_unchanged_sparse_file_reports_nothing(self):
        self._write_cfg({'config_version': cfgmod.CURRENT_CONFIG_VERSION,
                         'display': {'time_format': '12h'}})
        self.assertEqual(cfgmod.save_config(cfgmod._load_config_file()), [])
        self.assertFalse(cfgmod.is_restart_needed())

    def test_the_merged_argument_shape_still_works(self):
        """The raw-YAML editor deliberately saves the merged dump; that path is out of
        scope for this change and must be unaffected."""
        self._write_cfg({'config_version': cfgmod.CURRENT_CONFIG_VERSION})
        merged = cfgmod.load_config()
        merged['display']['time_format'] = '24h'
        changed = cfgmod.save_config(merged)
        self.assertEqual([p for p, _, _ in changed], ['display.time_format'])


class LoadForEditTests(ConfigSandbox):

    def test_it_returns_the_merged_config_and_the_raw_file(self):
        self._write_cfg({'config_version': cfgmod.CURRENT_CONFIG_VERSION,
                         'display': {'time_format': '24h'}})
        merged, file_cfg = cfgmod.load_for_edit()
        self.assertEqual(merged['display']['time_format'], '24h')
        self.assertEqual(file_cfg['display']['time_format'], '24h')
        self.assertIn('watchdog', merged)
        self.assertNotIn('watchdog', file_cfg)

    def test_the_file_dict_is_a_copy_the_caller_may_mutate(self):
        self._write_cfg({'config_version': cfgmod.CURRENT_CONFIG_VERSION,
                         'display': {'time_format': '24h'}})
        _, file_cfg = cfgmod.load_for_edit()
        file_cfg['display']['time_format'] = 'mutated'
        self.assertEqual(cfgmod.load_for_edit()[1]['display']['time_format'], '24h')

    def test_a_missing_config_file_yields_an_empty_dict(self):
        os.remove(self._cfg_path)
        cfgmod._yaml_cache = None
        merged, file_cfg = cfgmod.load_for_edit()
        self.assertEqual(file_cfg, {})
        self.assertIn('watchdog', merged)


if __name__ == '__main__':
    unittest.main()
