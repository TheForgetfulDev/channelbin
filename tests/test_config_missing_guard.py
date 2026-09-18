"""A missing config.yaml on an established install must be loud, not silently absorbed as
"run on defaults" (dev/docs/BUGS.md 2026-07-24: a git rename of config.yaml -> config.example.yaml
deleted the live config with the only symptom being a blank /logs page).

Points app.config._CONFIG_PATH at a nonexistent temp path (same pattern as
tests/test_config_cache.py) so create_app() sees a "missing config.yaml" the same way the
2026-07-18 incident did, without touching the real config.yaml on this box.
"""
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import app.config as config_mod  # noqa: E402
from tests.support.app import deliberately_missing_config, make_test_app  # noqa: E402
from app.database import Alert  # noqa: E402


class ConfigFileMissingGuardTests(unittest.TestCase):

    def setUp(self):
        tmpdir = tempfile.mkdtemp(prefix='dvr_test_missing_config_')
        self._orig_path = config_mod._CONFIG_PATH
        config_mod._CONFIG_PATH = os.path.join(tmpdir, 'does-not-exist.yaml')
        config_mod._yaml_cache = None
        self._tmpdir = tmpdir
        # A _CONFIG_PATH pointing at a file that isn't there is otherwise how a stranded
        # patch from an earlier test looks, and make_test_app() refuses to build on one.
        # Here it is the fixture, so say so (dev/changelog/724).
        self._declared = deliberately_missing_config()
        self._declared.__enter__()

    def tearDown(self):
        self._declared.__exit__(None, None, None)
        config_mod._CONFIG_PATH = self._orig_path
        config_mod._yaml_cache = None

    def test_missing_config_with_populated_db_raises_alert(self):
        # make_test_app()'s default (fresh_schema=False) copies the preseeded template DB,
        # so is_fresh_db() sees an already-populated database -- the "established install"
        # case this guard exists for.
        t = make_test_app()
        try:
            alerts = Alert.query.filter_by(alert_type='CONFIG_FILE_MISSING').all()
            self.assertEqual(len(alerts), 1,
                             'expected exactly one CONFIG_FILE_MISSING alert when config.yaml '
                             'is missing on an install with a populated DB')
            self.assertEqual(alerts[0].severity, 'CRIT')
            # recording.dvr_output_dir's built-in default has always been /dvr, never
            # /dvr/incomplete (dev/changelog/725 fixed the same false claim in README), and
            # post_process.enabled defaults to True -- so the alert must not assert either
            # falsehood about what "pure defaults" does (dev/changelog/822).
            self.assertNotIn('/dvr/incomplete', alerts[0].body)
            self.assertNotIn('effectively disabled', alerts[0].body)
        finally:
            t.cleanup()

    def test_missing_config_with_fresh_db_and_no_backups_stays_silent(self):
        # fresh_schema=True builds the schema live via db.create_all(), so is_fresh_db()
        # is True -- a genuinely fresh clone, matching changelog/185's deliberate
        # "no config file, no history -> silent defaults" behavior.
        t = make_test_app(fresh_schema=True)
        try:
            alerts = Alert.query.filter_by(alert_type='CONFIG_FILE_MISSING').all()
            self.assertEqual(len(alerts), 0,
                             'a genuinely fresh install (no config.yaml, no populated DB, no '
                             'backups) must stay silent per changelog/185')
        finally:
            t.cleanup()


if __name__ == '__main__':
    unittest.main(verbosity=2)


class EmptySectionKeepsItsDefaultsTests(unittest.TestCase):
    """`logo_cache:` with nothing under it is "I set nothing here", not a null section.

    dev/docs/BUGS.md 2026-09-17: such a section reached load_config() as None, and
    /settings 500'd on `rec.get('logo_cache', {}).get('enabled')` - the `.get` default
    never fires for a key that is present, so every reader of that shape breaks at once.
    """

    def setUp(self):
        self.t = make_test_app()
        self.t.app.config['WTF_CSRF_ENABLED'] = False
        self.t.sandbox_config({'recording': {'dvr_output_dir': '/dvr', 'logo_cache': None,
                                             'live_thumbnail': None},
                               'channel_testing': None,
                               'logging': {'file': None}})

    def tearDown(self):
        self.t.cleanup()

    def test_the_section_reads_as_its_defaults(self):
        from app.config import _DEFAULTS, load_config
        cfg = load_config()
        self.assertEqual(cfg['recording']['logo_cache'],
                         _DEFAULTS['recording']['logo_cache'])
        self.assertEqual(cfg['recording']['live_thumbnail'],
                         _DEFAULTS['recording']['live_thumbnail'])
        self.assertEqual(cfg['channel_testing'], _DEFAULTS['channel_testing'])
        self.assertEqual(cfg['recording']['dvr_output_dir'], '/dvr',
                         'a value set beside an empty section was dropped')

    def test_an_empty_leaf_is_still_a_real_choice(self):
        """Only a section gets the defaults back. `logging.file:` with no value means log
        to stdout, and overriding that would re-impose a file nobody asked for."""
        from app.config import load_config
        self.assertIsNone(load_config()['logging']['file'])

    def test_the_settings_page_still_renders(self):
        resp = self.t.client.get('/settings')
        self.assertEqual(resp.status_code, 200)
