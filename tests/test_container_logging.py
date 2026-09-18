"""Where the application's log lines go, inside a container and outside one.

The two surfaces are not interchangeable and neither can substitute for the other. The Logs
page tails a file and has no second source (app/routes/logs.py::_log_file_path), and
`docker logs` shows a container's stdout and nothing else - so in a container the app has to
write both, and `_setup_logging`'s ordinary either/or branch silences one of them whichever
way it lands. A container with no `logging.file` left the Logs page permanently empty while
`docker logs` was full of exactly the lines it wanted; configuring a file instead would have
emptied `docker logs`, which is the only log surface an operator has before the UI is even
reachable (dev/changelog/981).

The stream handler is added for containers specifically rather than for everyone, and that
narrowness is the point: an install whose process manager already redirects the process's
stdout into the same path the file handler writes would log every line twice. That is not
hypothetical - restart.sh on the dev box does precisely that, `nohup python3 run.py >>
dvr.log` against a config.yaml whose logging.file is the same dvr.log.

logging.basicConfig is a no-op once the root logger has handlers, which it does by the time
any test runs, so these assert on the handler set built and handed to it rather than on the
root logger afterwards.

No BUGS.md entry of its own - the defect is logged under the file-mode entry it was found
beside, 2026-09-15.
"""
import logging
import logging.handlers
import os
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import app as app_pkg  # noqa: E402
from app import config as config_mod  # noqa: E402
from tests.support.config_sandbox import ConfigSandbox  # noqa: E402


class _HandlerCase(unittest.TestCase):

    def handlers_for(self, cfg, containerized, **kwargs):
        """The handler list _setup_logging hands to logging.basicConfig."""
        env = dict(os.environ)
        env.pop('CHANNELBIN_DOCKER', None)
        if containerized:
            env['CHANNELBIN_DOCKER'] = '1'
        captured = {}

        def fake_basic_config(**kwargs):
            captured['handlers'] = kwargs.get('handlers', [])
            captured['force'] = kwargs.get('force')

        with patch.dict(os.environ, env, clear=True), \
                patch('logging.basicConfig', fake_basic_config):
            app_pkg._setup_logging(cfg, **kwargs)
        self.captured = captured
        built = captured['handlers']
        # Closing them keeps an open file descriptor per case out of the suite; they were
        # never attached to the root logger, because basicConfig was replaced above.
        self.addCleanup(lambda: [h.close() for h in built])
        return built

    @staticmethod
    def kinds(handlers):
        out = []
        for h in handlers:
            if isinstance(h, logging.FileHandler):     # RotatingFileHandler subclasses it
                out.append('file')
            elif isinstance(h, logging.StreamHandler):
                out.append('stream')
            else:
                out.append(type(h).__name__)
        return out


class ContainerLoggingTests(_HandlerCase):

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix='cb-log-')
        self.log_file = os.path.join(self.tmp, 'dvr.log')

    def test_a_container_with_a_log_file_writes_to_the_file_and_to_stdout(self):
        kinds = self.kinds(self.handlers_for(
            {'logging': {'level': 'INFO', 'file': self.log_file}}, containerized=True))
        self.assertIn('file', kinds,
                      'nothing writes the file the Logs page tails, so that page is empty')
        self.assertIn('stream', kinds,
                      'nothing writes stdout, so `docker logs` is empty - and that is the '
                      'only log surface there is before the UI comes up')

    def test_outside_a_container_a_log_file_means_the_file_alone(self):
        """The narrowness is deliberate. restart.sh runs `nohup python3 run.py >> dvr.log`
        against a config whose logging.file is that same dvr.log, so a stream handler here
        would write every line into it twice."""
        kinds = self.kinds(self.handlers_for(
            {'logging': {'level': 'INFO', 'file': self.log_file}}, containerized=False))
        self.assertEqual(kinds, ['file'])

    def test_no_log_file_is_stdout_alone_either_way(self):
        for containerized in (True, False):
            with self.subTest(containerized=containerized):
                kinds = self.kinds(self.handlers_for(
                    {'logging': {'level': 'INFO'}}, containerized=containerized))
                self.assertEqual(kinds, ['stream'])

    def test_the_ordinary_startup_does_not_force_handlers_away(self):
        """force defaults off, so the second _setup_logging call create_app can make is the
        exception rather than the rule. basicConfig(force=True) closes the handlers it
        replaces, so calling it unconditionally would tear down working logging on a path
        that had no reason to touch it."""
        self.handlers_for({'logging': {'level': 'INFO', 'file': self.log_file}},
                          containerized=True)
        self.assertFalse(self.captured['force'])

    def test_force_is_passed_through_so_a_second_call_actually_replaces(self):
        """logging.basicConfig is a no-op once the root logger has handlers - which it does
        by the time create_app runs its migration. Without force the re-setup after config
        migration 5 would silently do nothing and the container would log to stdout only
        until its next restart."""
        self.handlers_for({'logging': {'level': 'INFO', 'file': self.log_file}},
                          containerized=True, force=True)
        self.assertTrue(self.captured['force'])

    def test_rotation_is_not_lost_when_the_stream_handler_is_added(self):
        """The file handler must still be the rotating one - an unbounded log on a
        container's /config volume grows until the cache pool fills."""
        handlers = self.handlers_for(
            {'logging': {'level': 'INFO', 'file': self.log_file, 'max_bytes': 1024}},
            containerized=True)
        files = [h for h in handlers if isinstance(h, logging.FileHandler)]
        self.assertEqual(len(files), 1)
        self.assertIsInstance(files[0], logging.handlers.RotatingFileHandler)


class ContainerLogFileMigrationTests(unittest.TestCase):
    """Config migration 5.

    The seeded config.yaml is written once, on a container's very first start, and never
    again - that is what makes settings survive an image upgrade, and it is also why a key a
    later image adds never reaches a container that already exists. Every container created
    before this had no logging.file and therefore a permanently empty Logs page, and the
    answer cannot be "ask each of them to edit a file by hand" (dev/changelog/981).
    """

    def run_migration(self, cfg, containerized):
        from app.config import _cfg_m005_container_log_file
        env = dict(os.environ)
        env.pop('CHANNELBIN_DOCKER', None)
        if containerized:
            env['CHANNELBIN_DOCKER'] = '1'
        with patch.dict(os.environ, env, clear=True):
            return _cfg_m005_container_log_file(cfg)

    def test_a_container_with_no_log_file_gets_one_on_the_config_volume(self):
        from app.config import CONTAINER_LOG_FILE
        out = self.run_migration({'logging': {'level': 'INFO', 'file': None}}, True)
        self.assertEqual(out['logging']['file'], CONTAINER_LOG_FILE)
        self.assertTrue(CONTAINER_LOG_FILE.startswith('/config/'),
                        'a log outside a declared volume is discarded on image upgrade')

    def test_a_container_that_already_has_one_is_left_alone(self):
        out = self.run_migration({'logging': {'file': '/config/somewhere-else.log'}}, True)
        self.assertEqual(out['logging']['file'], '/config/somewhere-else.log')

    def test_a_missing_logging_section_is_created_rather_than_crashing(self):
        from app.config import CONTAINER_LOG_FILE
        out = self.run_migration({}, True)
        self.assertEqual(out['logging']['file'], CONTAINER_LOG_FILE)

    def test_a_normal_install_is_never_touched(self):
        """Outside a container "no log file" is a real choice - log to stdout and let a
        supervisor keep it. Writing one there would override a decision the user made."""
        out = self.run_migration({'logging': {'level': 'INFO', 'file': None}}, False)
        self.assertIsNone(out['logging']['file'])

    def test_it_is_registered_so_it_actually_runs(self):
        from app.config import CONFIG_MIGRATIONS, CURRENT_CONFIG_VERSION
        from app.config import _cfg_m005_container_log_file
        registered = [fn for _, _, fn in CONFIG_MIGRATIONS]
        self.assertIn(_cfg_m005_container_log_file, registered,
                      'the transform exists but nothing runs it, so no config is migrated')
        self.assertEqual(CURRENT_CONFIG_VERSION, CONFIG_MIGRATIONS[-1][0])

    def test_clearing_the_setting_afterwards_sticks(self):
        """The reason this is a migration and not a default resolved at read time. It has
        already run and stamped the file, so a user who then chooses stdout alone keeps it -
        a read-time default could not tell "never set" from "deliberately cleared" and would
        reimpose itself on every boot."""
        from app.config import CONFIG_MIGRATIONS, CURRENT_CONFIG_VERSION
        versions = [v for v, _, _ in CONFIG_MIGRATIONS]
        self.assertEqual(sorted(versions), versions, 'migrations must be in ascending order')
        self.assertEqual(len(set(versions)), len(versions), 'two migrations share a version')
        # A file already stamped current re-runs nothing - that is migrate_config's contract
        # and what makes the write above set-once.
        self.assertEqual(max(versions), CURRENT_CONFIG_VERSION)


class ContainerLogFileMigrationEndToEndTests(ConfigSandbox):
    """The transform above, driven through migrate_config() against a real file on disk.

    This is the case that matters for an existing install: the container that is already
    running, whose config.yaml was seeded by an older image and will never be seeded again.
    The per-function tests prove the transform; this proves the user's file actually changes.
    """

    def setUp(self):
        super().setUp()
        self.backup_dir = tempfile.mkdtemp(prefix='cb-cfgbackup-')

    def _containerized(self):
        env = dict(os.environ)
        env['CHANNELBIN_DOCKER'] = '1'
        return patch.dict(os.environ, env, clear=True)

    def _migrate(self):
        with self._containerized():
            config_mod.migrate_config(config_overrides={
                'config_backup': {'backup_dir': self.backup_dir}})

    def test_an_existing_container_config_is_rewritten_on_startup(self):
        import yaml as _yaml
        from app.config import (CONFIG_MIGRATIONS, CONTAINER_LOG_FILE,
                                CURRENT_CONFIG_VERSION, _cfg_m005_container_log_file)
        # A config.yaml exactly as 0.9.1's entrypoint seeded it: stamped at the version
        # before this migration, and carrying no logging.file at all. Pinned to this
        # migration's own version rather than CURRENT-1, which stops exercising it the
        # moment a later migration is added.
        before = next(v for v, _d, fn in CONFIG_MIGRATIONS
                      if fn is _cfg_m005_container_log_file) - 1
        self._write_cfg({'config_version': before,
                         'logging': {'level': 'INFO'},
                         'database': {'path': '/config/dvr.db'}})
        self._migrate()

        # load_config() would merge _DEFAULTS over the file and report a value it may not
        # contain; _load_config_file() answers from the mtime cache rather than the disk.
        # Either would assert on something other than what the migration wrote.
        with open(config_mod._CONFIG_PATH) as fh:  # direct-config-read: asserting on stored bytes
            written = _yaml.safe_load(fh)
        self.assertEqual(written['logging']['file'], CONTAINER_LOG_FILE,
                         'the running container never gains a log file, so its Logs page '
                         'stays empty and the only fix is a hand edit')
        self.assertEqual(written['config_version'], CURRENT_CONFIG_VERSION,
                         'the stamp did not move, so this migration would run again forever')
        self.assertEqual(written['database']['path'], '/config/dvr.db',
                         'the migration disturbed a setting it has no business touching')

    def test_running_it_twice_does_not_undo_a_later_user_choice(self):
        """The set-once property, end to end: once the stamp is current, a user who clears
        the field keeps it cleared. This is the whole reason it is a migration rather than a
        default computed at read time."""
        import yaml as _yaml
        from app.config import CURRENT_CONFIG_VERSION
        self._write_cfg({'config_version': CURRENT_CONFIG_VERSION,
                         'logging': {'level': 'INFO', 'file': None}})
        self._migrate()
        # load_config() would merge _DEFAULTS over the file and report a value it may not
        # contain; _load_config_file() answers from the mtime cache rather than the disk.
        # Either would assert on something other than what the migration wrote.
        with open(config_mod._CONFIG_PATH) as fh:  # direct-config-read: asserting on stored bytes
            written = _yaml.safe_load(fh)
        self.assertIsNone(written['logging']['file'])


class ContainerSeedConfigTests(unittest.TestCase):
    """The seeded container config has to actually ask for the file, or every new install
    repeats the diagnosis this was found by."""

    def test_the_seed_config_points_the_log_at_the_config_volume(self):
        import yaml
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        with open(os.path.join(root, 'docker', 'config.docker.yaml')) as fh:
            cfg = yaml.safe_load(fh)
        log_file = cfg.get('logging', {}).get('file')
        self.assertTrue(log_file, 'the container seeds no logging.file, so a new install\'s '
                                  'Logs page is empty from the first boot')
        self.assertTrue(log_file.startswith('/config/'),
                        f'the container log file is {log_file}, which is outside the /config '
                        'volume - it would be discarded on the next image upgrade')


if __name__ == '__main__':
    unittest.main()
