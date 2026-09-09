"""Backup destinations live on local disk, private (DESIGN-secrets.md §5).

A config backup is config.yaml verbatim (flask.secret_key, notification webhook tokens)
and a pre-migration DB snapshot is the whole database, whose accounts table stores
provider credentials in plaintext. Both used to default under /dvr - on this install a
CIFS share mounted sec=none - so every reader of the recordings share could read them.

These tests pin the three properties that keep that closed:
  * neither default points at /dvr,
  * a relative config path resolves against the app dir, never the process CWD
    (systemd, a Docker ENTRYPOINT and a shell all start the app from different places),
  * a backup dir the app creates is 0700, and one that already exists is left alone.

No app build needed: resolve_app_path/ensure_private_dir are pure, and do_backup takes
its paths as arguments.
"""
import os
import shutil
import stat
import sys
import tempfile
import unittest

import yaml

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import app.config as config_mod  # noqa: E402
from app.config import (DEFAULT_CONFIG_BACKUP_DIR, DEFAULT_DB_BACKUP_DIR, _DEFAULTS,
                        ensure_private_dir, resolve_app_path)  # noqa: E402
from app.config_backup import do_backup  # noqa: E402
from tests.support.app import make_test_app  # noqa: E402


def _mode(path):
    return stat.S_IMODE(os.stat(path).st_mode)


class BackupDefaultLocationTests(unittest.TestCase):
    """The defaults themselves - the whole point of the change."""

    def test_defaults_are_not_on_the_dvr_share(self):
        for value in (DEFAULT_CONFIG_BACKUP_DIR, DEFAULT_DB_BACKUP_DIR,
                      _DEFAULTS['config_backup']['backup_dir'],
                      _DEFAULTS['database']['backup_dir']):
            self.assertFalse(value.startswith('/dvr'),
                             f'{value!r} puts secrets back on the shared mount')

    def test_defaults_resolve_under_the_app_instance_dir(self):
        for value in (_DEFAULTS['config_backup']['backup_dir'],
                      _DEFAULTS['database']['backup_dir']):
            resolved = resolve_app_path(value)
            self.assertEqual(os.path.dirname(resolved),
                             os.path.join(config_mod._APP_ROOT, 'instance'))


class ResolveAppPathTests(unittest.TestCase):
    """A relative config value must not follow the process CWD."""

    def setUp(self):
        self._orig_cwd = os.getcwd()

    def tearDown(self):
        os.chdir(self._orig_cwd)

    def test_relative_path_anchors_to_app_root_regardless_of_cwd(self):
        expected = os.path.join(config_mod._APP_ROOT, 'instance', 'db-backups')
        from_here = resolve_app_path('instance/db-backups')
        os.chdir(tempfile.gettempdir())
        from_elsewhere = resolve_app_path('instance/db-backups')
        self.assertEqual(from_here, expected)
        self.assertEqual(from_elsewhere, expected)

    def test_absolute_path_is_never_rewritten(self):
        """A user who deliberately pointed backups somewhere keeps that exact path."""
        self.assertEqual(resolve_app_path('/dvr/db-backups'), '/dvr/db-backups')

    def test_empty_value_passes_through(self):
        self.assertEqual(resolve_app_path(''), '')


class EnsurePrivateDirTests(unittest.TestCase):

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)

    def test_created_dir_is_0700(self):
        target = os.path.join(self._tmp.name, 'db-backups')
        ensure_private_dir(target)
        self.assertEqual(_mode(target), 0o700)

    def test_nested_creation_is_0700_at_the_leaf(self):
        target = os.path.join(self._tmp.name, 'a', 'b', 'backups')
        ensure_private_dir(target)
        self.assertEqual(_mode(target), 0o700)

    def test_existing_dir_permissions_are_left_alone(self):
        """Permissions on a dir the user already made are the user's choice, and on a
        network mount a chmod would fail or be ignored anyway."""
        target = os.path.join(self._tmp.name, 'preexisting')
        os.makedirs(target, mode=0o755)
        os.chmod(target, 0o755)   # defeat umask so the assertion is about our behavior
        ensure_private_dir(target)
        self.assertEqual(_mode(target), 0o755)

    def test_is_idempotent(self):
        target = os.path.join(self._tmp.name, 'twice')
        ensure_private_dir(target)
        ensure_private_dir(target)
        self.assertEqual(_mode(target), 0o700)


class DoBackupTests(unittest.TestCase):
    """do_backup owns dir creation, so it must create private and resolve relatives."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.cfg_path = os.path.join(self._tmp.name, 'config.yaml')
        with open(self.cfg_path, 'w') as f:
            f.write('flask:\n  secret_key: not-a-real-secret\n')

    def test_creates_backup_dir_0700_and_writes_the_copy(self):
        target = os.path.join(self._tmp.name, 'config-backups')
        dest = do_backup(backup_dir=target, cfg_path=self.cfg_path)
        self.assertEqual(_mode(target), 0o700)
        self.assertTrue(os.path.isfile(dest))
        with open(dest) as f:
            self.assertIn('not-a-real-secret', f.read())

    def test_relative_backup_dir_does_not_follow_cwd(self):
        """A relative backup_dir writes under the app dir, not wherever the process
        happens to be running from."""
        orig_cwd = os.getcwd()
        self.addCleanup(os.chdir, orig_cwd)
        rel = os.path.join('instance', 'test-backup-dirs-tmp')
        expected_dir = os.path.join(config_mod._APP_ROOT, rel)
        self.addCleanup(shutil.rmtree, expected_dir, True)
        os.chdir(self._tmp.name)
        dest = do_backup(backup_dir=rel, cfg_path=self.cfg_path)
        self.assertEqual(os.path.dirname(dest), expected_dir)
        self.assertFalse(os.path.isdir(os.path.join(self._tmp.name, rel)),
                         'backup dir was created relative to the CWD')


class MigrateConfigBackupSandboxTests(unittest.TestCase):
    """dev/docs/BUGS.md 2026-08-13: config_backup.py kept its own module-level copy of
    _CONFIG_PATH, computed once at import time, so a test's patch of app.config._CONFIG_PATH
    never reached it - do_backup()'s default cfg_path was always the real config.yaml.
    Separately, migrate_config()'s pre-migration do_backup() call ignored config_overrides
    entirely, so even a test that did redirect config_backup.backup_dir never had that
    redirect honored at migration time. Together, every make_test_app() build copied the
    developer's real config.yaml into the real instance/config-backups/ (5,883 files, 47MB,
    by 2026-08-13)."""

    def _real_backup_dir_snapshot(self):
        real_dir = resolve_app_path(DEFAULT_CONFIG_BACKUP_DIR)
        if not os.path.isdir(real_dir):
            return set()
        return set(os.listdir(real_dir))

    def test_make_test_app_never_writes_into_the_real_backup_dir(self):
        before = self._real_backup_dir_snapshot()
        t = make_test_app()
        try:
            pass
        finally:
            t.cleanup()
        after = self._real_backup_dir_snapshot()
        self.assertEqual(before, after,
                         'building a throwaway test app must not touch the real '
                         'instance/config-backups/ directory')

    def test_do_backup_default_cfg_path_follows_a_patched_config_path(self):
        """do_backup(cfg_path=None) must resolve app.config._CONFIG_PATH at call time,
        not a stale constant computed when config_backup.py was imported."""
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        patched_cfg = os.path.join(tmp.name, 'config.yaml')
        with open(patched_cfg, 'w') as f:
            f.write('marker: this-is-the-patched-file-not-the-real-one\n')
        orig_path = config_mod._CONFIG_PATH
        config_mod._CONFIG_PATH = patched_cfg
        self.addCleanup(lambda: setattr(config_mod, '_CONFIG_PATH', orig_path))

        backup_dir = os.path.join(tmp.name, 'backups')
        dest = do_backup(backup_dir=backup_dir)
        with open(dest) as f:
            self.assertIn('this-is-the-patched-file-not-the-real-one', f.read(),
                          'do_backup() copied the wrong config.yaml - it ignored the '
                          'patched app.config._CONFIG_PATH')

    def test_migrate_config_backup_honors_config_overrides(self):
        """migrate_config()'s pre-migration backup must land in a test's redirected
        config_backup.backup_dir, not the real instance/config-backups/."""
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        patched_cfg = os.path.join(tmp.name, 'config.yaml')
        with open(patched_cfg, 'w') as f:
            # No config_version key - forces migrate_config()'s migrate-and-backup branch.
            yaml.dump({}, f)
        orig_path = config_mod._CONFIG_PATH
        config_mod._CONFIG_PATH = patched_cfg
        config_mod._yaml_cache = None
        self.addCleanup(lambda: setattr(config_mod, '_CONFIG_PATH', orig_path))
        self.addCleanup(lambda: setattr(config_mod, '_yaml_cache', None))

        before = self._real_backup_dir_snapshot()
        redirected = os.path.join(tmp.name, 'backups')
        config_mod.migrate_config(config_overrides={'config_backup': {'backup_dir': redirected}})
        after = self._real_backup_dir_snapshot()

        self.assertEqual(before, after,
                         'migrate_config() wrote its pre-migration backup into the real '
                         'backup dir instead of the redirected one')
        self.assertEqual(len(os.listdir(redirected)), 1,
                         'migrate_config() must still back up before rewriting')


if __name__ == '__main__':
    unittest.main()
