"""Restoring a config backup always asks for a restart (dev/docs/BUGS.md 2026-11-05 @
06:37:05 PM).

A restore replaces every setting in config.yaml while the app keeps running, and only some
settings are applied live. The restart banner used to follow RESTART_REQUIRED_KEYS alone, so
a restore that moved, say, sync.sync_interval_hours left the sync schedule on the old value
with nothing on screen saying so. Every successful restore of the live file now raises the
banner; a failed one does not.

Runs against make_test_app()'s sandboxed config.yaml, whose config-backup dir is a temp dir.
"""
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import yaml  # noqa: E402

import app.config as cfgmod  # noqa: E402
from app.config import _deep_merge  # noqa: E402
from tests.support.app import make_test_app  # noqa: E402


class RestoreRaisesTheRestartBannerTests(unittest.TestCase):

    def setUp(self):
        self.t = make_test_app(csrf=False)
        self.addCleanup(self.t.cleanup)
        self.client = self.t.app.test_client()
        from app.config_backup import get_backup_dir
        self.backup_dir = get_backup_dir()
        os.makedirs(self.backup_dir, exist_ok=True)
        cfgmod.set_restart_needed(False)

    def _backup_holding(self, data, name='2026-01-01-00-00-00'):
        """A backup file holding `data` over this app's sandboxed paths, so the restored
        config still keeps runtime reads in the temp dir."""
        from app.config_backup import _BACKUP_SUFFIX
        filename = f'{name}{_BACKUP_SUFFIX}'
        body = _deep_merge({'config_version': cfgmod.CURRENT_CONFIG_VERSION,
                            **self.t._sandbox_paths}, data)
        with open(os.path.join(self.backup_dir, filename), 'w') as f:
            yaml.safe_dump(body, f)
        return filename

    def _restore(self, filename):
        return self.client.post('/api/settings/rollback', json={'filename': filename})

    def test_a_restore_that_changes_no_restart_listed_key_raises_the_banner(self):
        self.assertNotIn('display.time_format', cfgmod.RESTART_REQUIRED_KEYS)
        name = self._backup_holding({'display': {'time_format': '24h'}})
        resp = self._restore(name)
        self.assertEqual(resp.status_code, 200, resp.data)
        self.assertEqual(cfgmod.load_config()['display']['time_format'], '24h')
        self.assertTrue(cfgmod.is_restart_needed(),
                        'a restore left the banner down because no restart-listed key moved')

    def test_a_restored_sync_interval_raises_the_banner(self):
        """The case that made it matter: a settings save applies the sync interval live,
        and a restore does not."""
        self.assertNotIn('sync.sync_interval_hours', cfgmod.RESTART_REQUIRED_KEYS)
        name = self._backup_holding({'sync': {'sync_interval_hours': 3}})
        self.assertEqual(self._restore(name).status_code, 200)
        self.assertTrue(cfgmod.is_restart_needed())

    def test_a_restore_identical_to_the_current_config_still_raises_the_banner(self):
        name = self._backup_holding({})
        self.assertEqual(self._restore(name).status_code, 200)
        self.assertTrue(cfgmod.is_restart_needed())

    def test_the_banner_reaches_the_status_poll_and_the_page(self):
        name = self._backup_holding({'display': {'time_format': '24h'}})
        self._restore(name)
        self.assertTrue(self.client.get('/api/settings/restart-status')
                        .get_json()['restart_needed'])
        page = self.client.get('/maintenance').get_data(as_text=True)
        self.assertIn('<div id="restart-banner" class="restart-banner">', page)

    def test_a_failed_restore_does_not_raise_the_banner(self):
        name = self._backup_holding({'display': {'time_format': '24h'}})
        with mock.patch('app.config._replace_config_file_from',
                        side_effect=OSError('disk full')):
            resp = self._restore(name)
        self.assertEqual(resp.status_code, 500)
        self.assertFalse(cfgmod.is_restart_needed(),
                         'a restore that changed nothing on disk asked for a restart')

    def test_a_restore_that_fails_after_replacing_the_file_still_raises_it(self):
        """A backup that will not parse is copied in and then fails in migrate_config():
        the route answers 500, but the live file is now the backup's, so the running app
        is still on the old config."""
        from app.config_backup import _BACKUP_SUFFIX
        filename = f'2026-01-03-00-00-00{_BACKUP_SUFFIX}'
        with open(os.path.join(self.backup_dir, filename), 'w') as f:
            f.write('display: [unclosed\n')
        try:
            resp = self._restore(filename)
        finally:
            # Put a parseable file back so the app's own cleanup can read its config.
            from tests.support.app import write_sandbox_config
            write_sandbox_config(cfgmod._CONFIG_PATH,
                                 {'config_version': cfgmod.CURRENT_CONFIG_VERSION,
                                  **self.t._sandbox_paths})
            cfgmod._yaml_cache = None
        self.assertEqual(resp.status_code, 500)
        self.assertTrue(cfgmod.is_restart_needed(),
                        'the live config.yaml was replaced and nothing asked for a restart')

    def test_an_unknown_backup_does_not_raise_the_banner(self):
        resp = self._restore('no-such-backup.yaml')
        self.assertEqual(resp.status_code, 404)
        self.assertFalse(cfgmod.is_restart_needed())

    def test_restoring_into_a_file_that_is_not_the_live_one_does_not(self):
        """apply_backup(cfg_path=...) writes some other file; the running config is
        untouched, so there is nothing for a restart to apply."""
        from app.config_backup import apply_backup
        name = self._backup_holding({'display': {'time_format': '24h'}})
        other = os.path.join(self.t._tmpdir, 'other-config.yaml')
        with open(other, 'w') as f:
            yaml.safe_dump({'config_version': cfgmod.CURRENT_CONFIG_VERSION}, f)
        apply_backup(os.path.join(self.backup_dir, name), cfg_path=other)
        self.assertFalse(cfgmod.is_restart_needed())


class RestoreDialogSaysARestartFollowsTests(unittest.TestCase):
    """UI text describing backend behavior is part of the change surface: the restore
    dialog and its toast say a restart follows, and the old "rolled back" toast is gone."""

    def test_the_dialog_and_toast_name_the_restart(self):
        path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                            'static', 'js', 'maintenance.js')
        with open(path) as f:
            js = f.read()
        self.assertIn('Applying a backup asks for a restart', js)
        self.assertIn('Backup applied - restart to finish', js)
        self.assertNotIn('Config rolled back', js)


if __name__ == '__main__':
    unittest.main(verbosity=2)
