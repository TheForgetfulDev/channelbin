"""Every folder ChannelBin writes to is checked for write access, and a failure reaches the
Readiness check and the Alert Center rather than only dvr.log (dev/changelog/1009).

The report this guards: a container running as uid 99 could not create
/dvr/live_thumbnails. It logged one WARNING at startup and one per recording, nothing else
said so, and the only writability check that existed was an entrypoint line on stdout
about /dvr - a folder that install did not record to at all.

Every directory here is under the test's own tmpdir, never /dvr. Permission denial is
real (chmod), so the cases that need it skip when the suite runs as root, which ignores
directory modes.
  python3 -m unittest tests.test_storage_dirs
"""
import os
import stat
import unittest
from unittest import mock

from app import fs_utils
from app.alerts import STORAGE_PATH_UNUSABLE
from app.database import Alert
from tests.support.app import make_test_app

_AS_ROOT = hasattr(os, 'geteuid') and os.geteuid() == 0


def _read_only(path):
    os.makedirs(path, exist_ok=True)
    os.chmod(path, stat.S_IRUSR | stat.S_IXUSR)


def _restore(path):
    if os.path.isdir(path):
        os.chmod(path, stat.S_IRWXU)


@unittest.skipIf(_AS_ROOT, 'root ignores directory modes, so nothing can be denied')
class ProbeWritableDirTests(unittest.TestCase):
    """dev/docs/BUGS.md 2026-09-17: a directory that is there but refuses writes read as OK."""

    def setUp(self):
        import tempfile
        self.root = tempfile.mkdtemp(prefix='dvr_test_writable_')
        self.locked = os.path.join(self.root, 'locked')
        _read_only(self.locked)

    def tearDown(self):
        import shutil
        _restore(self.locked)
        shutil.rmtree(self.root, ignore_errors=True)

    def test_a_writable_directory_is_ok(self):
        self.assertEqual(fs_utils.PATH_OK, fs_utils.probe_writable_dir(self.root).outcome)

    def test_an_existing_directory_that_refuses_writes_is_denied_and_names_the_uid(self):
        probe = fs_utils.probe_writable_dir(self.locked)
        self.assertEqual(fs_utils.PATH_DENIED, probe.outcome)
        self.assertIn(f'uid {os.getuid()}', probe.strerror)

    def test_a_missing_directory_that_can_be_created_is_ok(self):
        """Every writer creates its own folder on first use, so absent is not broken."""
        probe = fs_utils.probe_writable_dir(os.path.join(self.root, 'a', 'b'))
        self.assertEqual(fs_utils.PATH_OK, probe.outcome)

    def test_a_missing_directory_under_a_locked_parent_is_denied_and_names_the_parent(self):
        """The reported case exactly: /dvr/live_thumbnails absent, /dvr not writable."""
        path = os.path.join(self.locked, 'live_thumbnails')
        probe = fs_utils.probe_writable_dir(path)
        self.assertEqual(fs_utils.PATH_DENIED, probe.outcome)
        self.assertIn(self.locked, probe.strerror)
        self.assertIn(path, fs_utils.describe_dir_problem(path, probe))


class ConfiguredWriteDirsTests(unittest.TestCase):
    """Only the folders this install will actually write to are judged."""

    def _dirs(self, cfg):
        from app.storage_dirs import configured_write_dirs
        base = {'recording': {'dvr_output_dir': '/rec'},
                'database': {'backup_dir': '/b/db'},
                'config_backup': {'backup_dir': '/b/cfg'}}
        for key, value in cfg.items():
            base.setdefault(key, {}).update(value)
        return {path: role.what for path, role in configured_write_dirs(base, '/caplogs')}

    def test_a_switched_off_feature_is_not_judged(self):
        """An unwritable folder nobody will use is not a problem - reporting one is the
        false alarm the entrypoint's /dvr check raised."""
        dirs = self._dirs({
            'recording': {'images_dir': '/img',
                          'live_thumbnail': {'enabled': False},
                          'move_on_complete': {'enabled': False, 'destination': '/done'},
                          'logo_cache': {'enabled': False}},
            'channel_testing': {'screenshots_enabled': False}})
        for path in ('/img/thumbnails', '/done', '/img/logos', '/img/screenshots'):
            self.assertNotIn(path, dirs)
        self.assertIn('/rec', dirs)

    def test_every_switched_on_folder_is_judged(self):
        dirs = self._dirs({
            'recording': {'images_dir': '/img',
                          'live_thumbnail': {'enabled': True},
                          'move_on_complete': {'enabled': True, 'destination': '/done'},
                          'logo_cache': {'enabled': True}},
            'channel_testing': {'screenshots_enabled': True,
                                'capture_scratch_dir': '/scratch'}})
        for path in ('/rec', '/img/thumbnails', '/done', '/img/logos', '/img/screenshots',
                     '/scratch', '/caplogs',
                     '/b/db', '/b/cfg'):
            self.assertIn(path, dirs)

    def test_two_roles_on_one_path_are_one_entry(self):
        """The standing alert is keyed on the path, so one path is one row."""
        from app.storage_dirs import configured_write_dirs
        cfg = {'recording': {'dvr_output_dir': '/same/thumbnails', 'images_dir': '/same',
                             'live_thumbnail': {'enabled': True}}}
        paths = [p for p, _r in configured_write_dirs(cfg)]
        self.assertEqual(len(paths), len(set(paths)))


@unittest.skipIf(_AS_ROOT, 'root ignores directory modes, so nothing can be denied')
class StorageDirAlertTests(unittest.TestCase):
    """The sweep raises the same standing alert a stale DVR mount does, and clears it."""

    def setUp(self):
        self.t = make_test_app()
        self.dvr = os.path.join(self.t._tmpdir, 'rec')
        self.parent = os.path.join(self.t._tmpdir, 'share')
        self.thumbs = os.path.join(self.parent, 'thumbnails')
        os.makedirs(self.dvr, exist_ok=True)
        _read_only(self.parent)
        self.cfg = {'recording': {'dvr_output_dir': self.dvr, 'images_dir': self.parent,
                                  'live_thumbnail': {'enabled': True}}}
        self.t.sandbox_config(self.cfg)

    def tearDown(self):
        _restore(self.parent)
        _restore(self.dvr)
        self.t.cleanup()

    def _sweep(self):
        from app.config import load_config
        from app.storage_dirs import sweep_write_dirs
        with self.t.app.app_context():
            return sweep_write_dirs(load_config())

    def _alerts(self, source):
        with self.t.app.app_context():
            return Alert.query.filter_by(alert_type=STORAGE_PATH_UNUSABLE, source=source).all()

    def test_an_uncreatable_live_thumbnail_folder_raises_an_alert(self):
        self._sweep()
        rows = self._alerts(self.thumbs)
        self.assertEqual(1, len(rows), 'a folder the app cannot create raised no alert')
        self.assertIn('Live thumbnail directory', rows[0].title)
        self.assertIn(self.parent, rows[0].body)

    def test_repeat_sweeps_keep_one_row_and_a_fix_clears_it(self):
        self._sweep()
        self._sweep()
        self.assertEqual(1, len(self._alerts(self.thumbs)))
        _restore(self.parent)
        self._sweep()
        rows = self._alerts(self.thumbs)
        self.assertEqual(1, len(rows))
        self.assertIsNotNone(rows[0].dismissed_at,
                             'the alert must clear itself once the folder is writable')

    def test_the_disk_readout_and_the_sweep_agree_on_an_unwritable_dvr_dir(self):
        """Both report the DVR directory. If the readout still judged reachability alone it
        would call the path OK on every poll and dismiss the alert the sweep had just
        raised, flapping one row forever."""
        from app.routes.system import _system_stats_dict
        _read_only(self.dvr)
        with self.t.app.app_context():
            self._sweep()
            _system_stats_dict()
            self._sweep()
            _system_stats_dict()
        rows = self._alerts(self.dvr)
        self.assertEqual(1, len(rows))
        self.assertIsNone(rows[0].dismissed_at, 'the disk readout dismissed a real problem')


@unittest.skipIf(_AS_ROOT, 'root ignores directory modes, so nothing can be denied')
class ReadinessStorageDirsTests(unittest.TestCase):
    """The folders reach the Readiness check, which is where someone setting up looks."""

    def setUp(self):
        self.t = make_test_app()
        self.ctx = self.t.app.app_context()
        self.ctx.push()
        self.dvr = os.path.join(self.t._tmpdir, 'rec')
        self.parent = os.path.join(self.t._tmpdir, 'share')
        os.makedirs(self.dvr, exist_ok=True)
        _read_only(self.parent)

    def tearDown(self):
        _restore(self.parent)
        _restore(self.dvr)
        self.ctx.pop()
        self.t.cleanup()

    def _checks(self):
        from app import readiness
        return {row['id']: row for row in readiness.evaluate(ignored=set())['checks']}

    def test_an_unusable_folder_is_attention_and_names_its_role_and_path(self):
        thumbs = os.path.join(self.parent, 'thumbnails')
        self.t.sandbox_config({'recording': {'dvr_output_dir': self.dvr,
                                             'images_dir': self.parent,
                                             'live_thumbnail': {'enabled': True}}})
        row = self._checks()['storage_dirs']
        self.assertEqual('attention', row['status'])
        self.assertIn('Live thumbnail directory', row['found'])
        self.assertIn(thumbs, row['found'])

    def test_every_folder_writable_is_ready(self):
        self.t.sandbox_config({'recording': {
            'dvr_output_dir': self.dvr,
            'images_dir': os.path.join(self.t._tmpdir, 'img'),
            'live_thumbnail': {'enabled': True}},
            'database': {'backup_dir': os.path.join(self.t._tmpdir, 'dbb')},
            'config_backup': {'backup_dir': os.path.join(self.t._tmpdir, 'cfgb')}})
        self.assertEqual('ready', self._checks()['storage_dirs']['status'])

    def test_a_dvr_dir_that_cannot_be_written_blocks_recording(self):
        """It used to be os.access() on an existing path only; a missing DVR folder under
        a locked parent still fails every recording, and must say so."""
        self.t.sandbox_config({'recording': {'dvr_output_dir': self.dvr}})
        _read_only(self.dvr)
        row = self._checks()['storage_dvr']
        self.assertEqual('problem', row['status'])
        self.assertIn(f'uid {os.getuid()}', row['found'])


class EntrypointTests(unittest.TestCase):
    def test_the_entrypoint_does_not_judge_a_folder_the_app_may_never_write(self):
        """Its /dvr check fired for an install recording to other mounts, and landed on
        stdout, which neither the Logs page nor an alert can show."""
        path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                            'docker', 'entrypoint.sh')
        with open(path, encoding='utf-8') as fh:
            code = [line.split('#')[0] for line in fh]
        self.assertFalse([c for c in code if 'test -w' in c],
                         'the entrypoint checks folder writability again')


class SchedulerWiringTests(unittest.TestCase):
    def test_the_sweep_is_registered_on_an_interval_and_fires_soon_after_start(self):
        from datetime import datetime, timedelta
        import app.scheduler as sched
        with mock.patch.object(sched, '_add_job') as add_job:
            sched.schedule_storage_dirs_check(None)
        kwargs = add_job.call_args.kwargs
        self.assertEqual('storage_dirs_check', kwargs['id'])
        self.assertEqual('interval', kwargs['trigger'])
        self.assertLess(kwargs['next_run_time'], datetime.utcnow() + timedelta(minutes=1))


if __name__ == '__main__':
    unittest.main()
