"""Saving settings through the GUI drops hand-written config.yaml comments (BUGS.md
2026-08-12): save_config() dumped its `data` argument with plain PyYAML, which has no
concept of comments at all, so every save silently wiped whatever the user had written in
the file - even when only one field actually changed.

_parse_config_file() now reads with ruamel.yaml's round-trip mode (a comment-carrying
CommentedMap instead of a plain dict) and save_config() merges new values into that
existing structure in place, instead of dumping `data` fresh. These tests write directly
against a temp config.yaml (same pattern as tests/test_config_cache.py) so the real
config.yaml on this box is never touched.
"""
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import app.config as config_mod  # noqa: E402
from tests.support.app import make_test_app  # noqa: E402


class ConfigCommentPreservationTests(unittest.TestCase):
    """Direct save_config()/_parse_config_file() round trips - no Flask app needed."""

    def setUp(self):
        fd, self._tmp_path = tempfile.mkstemp(suffix='.yaml')
        os.close(fd)
        self._orig_path = config_mod._CONFIG_PATH
        config_mod._CONFIG_PATH = self._tmp_path
        config_mod._yaml_cache = None

    def tearDown(self):
        config_mod._CONFIG_PATH = self._orig_path
        config_mod._yaml_cache = None
        os.unlink(self._tmp_path)

    def _write(self, text):
        with open(self._tmp_path, 'w') as f:
            f.write(text)
        config_mod._yaml_cache = None

    def _read_raw(self):
        with open(self._tmp_path, 'r') as f:
            return f.read()

    def test_untouched_sibling_keeps_its_comment(self):
        self._write(
            "display:\n"
            "  timezone: America/Chicago  # the TZ used everywhere in the UI\n"
            "recording:\n"
            "  retention_days: 0\n"
        )
        data = config_mod.load_config()
        data['recording']['retention_days'] = 30
        config_mod.save_config(data)
        raw = self._read_raw()
        self.assertIn('# the TZ used everywhere in the UI', raw,
                       'saving an unrelated field must not drop an existing comment on a '
                       'sibling key')
        self.assertIn('retention_days: 30', raw, 'the actual field change must still land')

    def test_changed_key_keeps_its_own_comment(self):
        self._write(
            "recording:\n"
            "  retention_days: 0  # 0 = never auto-delete terminal recordings\n"
        )
        data = config_mod.load_config()
        data['recording']['retention_days'] = 14
        config_mod.save_config(data)
        raw = self._read_raw()
        self.assertIn('# 0 = never auto-delete terminal recordings', raw,
                       "a key's own comment must survive its value changing")
        self.assertIn('retention_days: 14', raw)

    def test_top_of_file_comment_survives_a_save(self):
        self._write(
            "# ChannelBin config - hand-edited, do not clobber my comments\n"
            "display:\n"
            "  timezone: UTC\n"
        )
        data = config_mod.load_config()
        data['display']['timezone'] = 'America/New_York'
        config_mod.save_config(data)
        raw = self._read_raw()
        self.assertIn('# ChannelBin config - hand-edited, do not clobber my comments', raw)

    def test_save_on_fresh_install_with_no_existing_file_does_not_crash(self):
        os.unlink(self._tmp_path)
        config_mod._yaml_cache = None
        data = config_mod.load_config()
        data['display']['timezone'] = 'America/Denver'
        config_mod.save_config(data)
        reloaded = config_mod.load_config()
        self.assertEqual(reloaded['display']['timezone'], 'America/Denver')

    def test_key_removed_from_data_is_still_removed_from_file(self):
        # save_config() has always been full-replace: whatever isn't in `data` doesn't
        # survive the save. The comment-preserving merge must not accidentally change that.
        self._write(
            "display:\n"
            "  timezone: UTC\n"
            "sync:\n"
            "  interval_hours: 6\n"
        )
        data = config_mod.load_config()
        del data['sync']
        config_mod.save_config(data)
        raw = self._read_raw()
        self.assertNotIn('interval_hours', raw,
                         'a key absent from the saved data must still be removed from the '
                         'file, not left behind by the comment-preserving merge')

    def test_migrate_config_rewrite_preserves_comments(self):
        # config_version 0 (absent) + the legacy 'xtream' section forces migrate_config()'s
        # one real write path - proves the ruamel dumper swap there didn't just move the
        # crash (plain yaml.dump can't represent the CommentedMap _load_config_file() now
        # returns) rather than actually fixing it.
        self._write(
            "# do not delete this comment\n"
            "xtream:\n"
            "  base_url: https://example.test\n"
        )
        with tempfile.TemporaryDirectory() as backup_dir:
            # migrate_config() backs up before rewriting - redirect it here, not the real
            # instance/config-backups/, the same way make_test_app() does for every other
            # test that reaches this path (dev/changelog/620).
            config_mod.migrate_config(config_overrides={'config_backup': {'backup_dir': backup_dir}})
            self.assertEqual(len(os.listdir(backup_dir)), 1,
                             'migrate_config() must back up config.yaml before rewriting it')
        raw = self._read_raw()
        self.assertIn('# do not delete this comment', raw)
        self.assertIn('sync:', raw)
        self.assertNotIn('xtream:', raw)


class LegacyChannelTestingKeyStripPreservesCommentsTests(unittest.TestCase):
    """app/__init__.py::_ensure_system_health_job() strips legacy channel_testing keys and
    rewrites config.yaml directly (a second write site outside app/config.py that also had
    to move off plain yaml.dump once _load_config_file() started returning a CommentedMap -
    a bare yaml.dump(CommentedMap) raises, so an untested version of this fix would crash
    create_app() outright for any install still carrying those legacy keys)."""

    def setUp(self):
        fd, self._tmp_path = tempfile.mkstemp(suffix='.yaml')
        os.close(fd)
        self._orig_path = config_mod._CONFIG_PATH
        config_mod._CONFIG_PATH = self._tmp_path
        config_mod._yaml_cache = None
        with open(self._tmp_path, 'w') as f:
            f.write(
                "# hand-written note for future me\n"
                "config_version: 1\n"
                "channel_testing:\n"
                "  enabled: true\n"
                "  schedule_hour: 3\n"
                "  test_days: [1, 3, 5]\n"
            )
        config_mod._yaml_cache = None

    def tearDown(self):
        config_mod._CONFIG_PATH = self._orig_path
        config_mod._yaml_cache = None
        os.unlink(self._tmp_path)

    def test_legacy_key_strip_does_not_crash_and_keeps_the_comment(self):
        t = make_test_app()
        try:
            with open(self._tmp_path, 'r') as f:
                raw = f.read()
            self.assertNotIn('schedule_hour', raw,
                             'legacy channel_testing keys must still be stripped')
            self.assertIn('# hand-written note for future me', raw,
                         'stripping the legacy keys must not drop an unrelated comment')
        finally:
            t.cleanup()
