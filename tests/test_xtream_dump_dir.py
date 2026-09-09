"""Xtream dump/debug-replay directory: default location and private permissions.

Moved off dev/samples/xtream (excluded from the shipped tree, world-readable) onto
instance/xtream-dumps - the same instance/ convention DEFAULT_CONFIG_BACKUP_DIR and
DEFAULT_DB_BACKUP_DIR already use, and for the same reason: a dump embeds the account's
plaintext username/password in every stream URL it captures, so it belongs beside the
other secret-bearing instance/ content, not in a folder .publishignore excludes but never
locks down (dev/changelog/550).

No app build needed: _dump_base/_make_new_dump_dir are pure functions of a cfg dict and a
directory path.
"""
import os
import stat
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import app.config as config_mod  # noqa: E402
from app.config import DEFAULT_XTREAM_DUMP_DIR, resolve_app_path  # noqa: E402
from app.xtream_client import _dump_base, _make_new_dump_dir  # noqa: E402


def _mode(path):
    return stat.S_IMODE(os.stat(path).st_mode)


class DumpDirDefaultLocationTests(unittest.TestCase):

    def test_default_is_not_under_dev(self):
        self.assertFalse(DEFAULT_XTREAM_DUMP_DIR.startswith('dev/'),
                         'default dump dir must not live under dev/, which .publishignore '
                         'excludes from the shipped tree')

    def test_default_resolves_under_the_app_instance_dir(self):
        resolved = resolve_app_path(DEFAULT_XTREAM_DUMP_DIR)
        self.assertEqual(os.path.dirname(resolved),
                         os.path.join(config_mod._APP_ROOT, 'instance'))


class DumpBaseTests(unittest.TestCase):

    def test_no_override_resolves_the_default_under_app_root(self):
        self.assertEqual(_dump_base({}), resolve_app_path(DEFAULT_XTREAM_DUMP_DIR))
        self.assertEqual(_dump_base({'debug': {}}), resolve_app_path(DEFAULT_XTREAM_DUMP_DIR))

    def test_absolute_override_passes_through_unchanged(self):
        self.assertEqual(_dump_base({'debug': {'xtream_dump_dir': '/custom/dumps'}}),
                         '/custom/dumps')

    def test_relative_override_still_anchors_to_app_root(self):
        self.assertEqual(_dump_base({'debug': {'xtream_dump_dir': 'somewhere/else'}}),
                         resolve_app_path('somewhere/else'))


class MakeNewDumpDirTests(unittest.TestCase):
    """Dumps carry plaintext provider credentials (DESIGN-secrets.md §5), so the account
    dir must be created private - same contract as the config/DB backup dirs."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.cfg = {'debug': {'xtream_dump_dir': self._tmp.name}}

    def test_account_dir_is_created_0700(self):
        dump_dir = _make_new_dump_dir(self.cfg, 7)
        account_dir = os.path.join(self._tmp.name, '7')
        self.assertEqual(_mode(account_dir), 0o700)
        self.assertTrue(os.path.isdir(dump_dir))

    def test_existing_account_dir_permissions_are_left_alone(self):
        account_dir = os.path.join(self._tmp.name, '7')
        os.makedirs(account_dir, mode=0o755)
        os.chmod(account_dir, 0o755)   # defeat umask so the assertion is about our behavior
        _make_new_dump_dir(self.cfg, 7)
        self.assertEqual(_mode(account_dir), 0o755)

    def test_second_dump_same_day_increments_the_suffix(self):
        first = _make_new_dump_dir(self.cfg, 7)
        second = _make_new_dump_dir(self.cfg, 7)
        self.assertNotEqual(first, second)
        self.assertTrue(os.path.basename(first).endswith('_01'))
        self.assertTrue(os.path.basename(second).endswith('_02'))


if __name__ == '__main__':
    unittest.main()
