"""The logo cache fetch job is registered only while the feature is on.

It used to be registered unconditionally and no-op on every tick, so an install with
recording.logo_cache.enabled off got a job on /jobs counting down to a 5-minute tick that
scanned the whole channels table and recorded a JobRun for doing nothing
(dev/changelog/1056).

What has to hold once the registration follows the setting is that the setting still moves
without a restart - in BOTH directions. Turning the feature off and finding the job gone is
the visible half; turning it back on and finding nothing ever runs, with no surface saying
why, is the half worth guarding. So every path that can write that config leaf is exercised
here against the real route, not against apply_logo_cache_schedule() alone.

Runs against a throwaway temp SQLite DB, a sandboxed config.yaml and a sandboxed
APScheduler jobstore - never the live dvr.db or the real config.yaml.
    python3 -m unittest tests.test_logo_cache_job_schedule
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import yaml  # noqa: E402

from tests.support.app import make_test_app  # noqa: E402
import app.config as cfgmod  # noqa: E402
from app.scheduler import get_scheduler  # noqa: E402

JOB_ID = 'logo_cache_fetch'


def apply_logo_cache_schedule():
    """Imported inside the call, not at module top, so this file still loads against a
    tree where the function does not exist yet. Every case below that drives a real route
    then still runs and still fails for its own reason, instead of the whole module
    collapsing into one ImportError that proves nothing (CLAUDE.md §Testing)."""
    from app.scheduler import apply_logo_cache_schedule as _apply
    return _apply()


class _LogoCacheJobCase(unittest.TestCase):
    def setUp(self):
        self.t = make_test_app(start_scheduler=True)
        self.t.app.config['WTF_CSRF_ENABLED'] = False
        self.client = self.t.app.test_client()
        self.addCleanup(self.t.cleanup)

    def job(self):
        return get_scheduler().get_job(JOB_ID)

    def register_job(self):
        """Put the job in the store without going through the code under test, so a case
        whose subject is the REMOVAL half does not depend on the registering half."""
        from app.scheduler import _add_job, _logo_cache_job
        _add_job(func=_logo_cache_job, trigger='interval', minutes=5,
                 id=JOB_ID, replace_existing=True)

    def set_enabled(self, value):
        """Write the leaf straight to the sandboxed config.yaml, no route involved - for
        the cases that are about apply_logo_cache_schedule() rather than about a caller.

        Merged into the stored file rather than handed to save_config() on its own:
        save_config() is a full replace, so passing one leaf would drop everything else a
        test had already set (RollbackTests' backup_dir, notably)."""
        stored = cfgmod._load_config_file() or {}
        stored.setdefault('recording', {}).setdefault('logo_cache', {})['enabled'] = value
        cfgmod.save_config(stored)


class ScheduleFollowsTheSettingTests(_LogoCacheJobCase):

    def test_disabled_config_registers_nothing(self):
        self.set_enabled(False)
        self.assertFalse(apply_logo_cache_schedule())
        self.assertIsNone(self.job())

    def test_enabled_config_registers_the_job(self):
        self.set_enabled(True)
        self.assertTrue(apply_logo_cache_schedule())
        job = self.job()
        self.assertIsNotNone(job)
        self.assertEqual(job.trigger.interval.total_seconds(), 5 * 60)

    def test_disabling_removes_an_already_registered_job(self):
        self.set_enabled(True)
        apply_logo_cache_schedule()
        self.assertIsNotNone(self.job())

        self.set_enabled(False)
        self.assertFalse(apply_logo_cache_schedule())
        self.assertIsNone(self.job())

    def test_off_by_default_registers_nothing(self):
        """The shipped default is off, so an install that never touches the setting gets
        no job at all - which is what app/logo_cache.py's module docstring has always
        claimed and what this change makes true."""
        self.assertFalse(apply_logo_cache_schedule())
        self.assertIsNone(self.job())

    def test_a_live_job_is_left_alone_rather_than_re_added(self):
        """A second call must not reset the interval and push the next tick out by a
        fresh 5 minutes - the same property the previous unconditional registration had,
        and the reason a restart does not starve the job."""
        self.set_enabled(True)
        apply_logo_cache_schedule()
        first_run = self.job().next_run_time

        apply_logo_cache_schedule()
        self.assertEqual(self.job().next_run_time, first_run)


class SettingsFieldSaveTests(_LogoCacheJobCase):
    """POST /api/settings/field - the toggle on the Settings page."""

    def save(self, value):
        resp = self.client.post('/api/settings/field',
                                json={'path': 'recording.logo_cache.enabled',
                                      'value': value})
        self.assertEqual(resp.status_code, 200, resp.data)

    def test_turning_it_on_registers_the_job_without_a_restart(self):
        self.assertIsNone(self.job())
        self.save(True)
        self.assertIsNotNone(self.job(),
                             'enabling logo caching left no job, so it would never run')

    def test_turning_it_off_removes_the_job_without_a_restart(self):
        self.save(True)
        self.assertIsNotNone(self.job())
        self.save(False)
        self.assertIsNone(self.job())


class RawYamlSaveTests(_LogoCacheJobCase):
    """POST /settings - the raw config.yaml editor, which saves whole documents rather
    than one leaf and so reaches the same setting by a different route."""

    def save(self, cfg):
        resp = self.client.post('/settings',
                                data={'config_yaml': yaml.dump(cfg)},
                                follow_redirects=False)
        self.assertIn(resp.status_code, (200, 302), resp.data)

    def test_enabling_via_the_raw_editor_registers_the_job(self):
        self.assertIsNone(self.job())
        self.save({'config_version': cfgmod.CURRENT_CONFIG_VERSION,
                   'recording': {'logo_cache': {'enabled': True}}})
        self.assertIsNotNone(self.job())

    def test_disabling_via_the_raw_editor_removes_the_job(self):
        # The stored config has to say enabled, not just the jobstore: the raw editor's
        # hook fires on a CHANGED leaf, and writing false over an unset (default false)
        # leaf changes nothing. Registering the job without that would be testing a state
        # production cannot reach.
        self.set_enabled(True)
        self.register_job()
        self.assertIsNotNone(self.job())
        self.save({'config_version': cfgmod.CURRENT_CONFIG_VERSION,
                   'recording': {'logo_cache': {'enabled': False}}})
        self.assertIsNone(self.job())


class RollbackTests(_LogoCacheJobCase):
    """POST /api/settings/rollback - restoring a config backup replaces the whole file,
    so it can move this setting too."""

    def setUp(self):
        super().setUp()
        # config_backup.backup_dir defaults to instance/config-backups under the APP ROOT,
        # and neither make_test_app() nor ConfigSandbox redirects it - so a test that let
        # get_backup_dir() answer for itself would deposit into (and list) the real
        # install's backups. Point it at this test's temp dir before anything reads it.
        self.backup_dir = os.path.join(self.t._tmpdir, 'config-backups')
        os.makedirs(self.backup_dir, exist_ok=True)
        cfgmod.save_config({'config_backup': {'backup_dir': self.backup_dir}})
        from app.config_backup import get_backup_dir
        self.assertEqual(get_backup_dir(), self.backup_dir)

    def _backup_holding(self, enabled):
        """Write a config backup whose stored config has logo caching at `enabled`.
        Carries backup_dir forward so the restored config still points here."""
        from app.config_backup import _BACKUP_SUFFIX
        name = f'2026-01-0{1 if enabled else 2}-00-00-00{_BACKUP_SUFFIX}'
        with open(os.path.join(self.backup_dir, name), 'w') as f:
            yaml.dump({'config_version': cfgmod.CURRENT_CONFIG_VERSION,
                       'config_backup': {'backup_dir': self.backup_dir},
                       'recording': {'logo_cache': {'enabled': enabled}}}, f)
        return name

    def rollback(self, filename):
        resp = self.client.post('/api/settings/rollback', json={'filename': filename})
        self.assertEqual(resp.status_code, 200, resp.data)

    def test_rolling_back_to_an_enabled_config_registers_the_job(self):
        name = self._backup_holding(True)
        self.assertIsNone(self.job())
        self.rollback(name)
        self.assertIsNotNone(
            self.job(),
            'a rollback that turns logo caching on left no job, so it would silently '
            'never run until the next restart')

    def test_rolling_back_to_a_disabled_config_removes_the_job(self):
        name = self._backup_holding(False)
        self.set_enabled(True)
        self.register_job()
        self.assertIsNotNone(self.job())
        self.rollback(name)
        self.assertIsNone(self.job())


if __name__ == '__main__':
    unittest.main(verbosity=2)
