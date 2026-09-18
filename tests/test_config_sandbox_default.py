"""tests/support/app.py::TestApp sandboxes config.yaml by default - dev/docs/BUGS.md
2026-08-08 08:27 PM, dev/changelog/605.

make_test_app() never sandboxed config.yaml itself: extra_overrides only reached
create_app()'s own one-time cfg variable, so any OTHER runtime load_config() call (a route,
a scheduled job) re-read the developer's real repo-root file. Three tests silently depended
on whatever that file happened to contain and stayed broken through several green suite
runs before being caught. tests/support/config_sandbox.py::ConfigSandbox fixed this per test, but
opt-in - nothing stopped the next test from skipping it.

This file proves the fix that closes the escape hatch instead: TestApp.__init__ now
sandboxes config.yaml unconditionally, unless something outer has already pointed
_CONFIG_PATH somewhere else first (ConfigSandbox, or a hand-rolled equivalent - several
tests predate ConfigSandbox and patch _CONFIG_PATH directly in their own setUp). The
cooperative half is load-bearing, not cosmetic: an earlier, unconditional version of this
patch broke 20 real tests, all shaped exactly like CooperatesWithAnOuterSandboxTests below -
files that already sandbox themselves on purpose (an HA API key, a comment-preservation
fixture, a deliberately-missing config path) - and none of the 20 were a genuine dependency
on the real file.
"""
import os
import tempfile
import threading
import unittest
from unittest import mock

import yaml

from app import config as cfgmod
import tests.support.app as testapp_mod
from tests.support.app import make_test_app, write_sandbox_config


class RealConfigNeverLeaksIntoASandboxedAppTests(unittest.TestCase):
    """Simulates "the real config.yaml" carrying a distinguishing, machine-specific value -
    the exact shape of the 2026-08-08 incident - and proves a make_test_app()-built app never
    sees it, only _DEFAULTS."""

    MARKER = 'machine-specific-marker-abc123'

    def setUp(self):
        fd, self._fake_real_path = tempfile.mkstemp(suffix='.yaml')
        os.close(fd)
        with open(self._fake_real_path, 'w') as f:
            yaml.dump({'integrations': {'home_assistant': {
                'enabled': True, 'api_key_hash': self.MARKER}}}, f)

        # Stand in for "this is what _CONFIG_PATH looks like before any test has touched it" -
        # TestApp.__init__ compares against this exact name to decide whether to sandbox.
        real_path_patch = mock.patch.object(testapp_mod, '_REAL_CONFIG_PATH', self._fake_real_path)
        real_path_patch.start()
        self.addCleanup(real_path_patch.stop)

        self._orig_cfg_path = cfgmod._CONFIG_PATH
        cfgmod._CONFIG_PATH = self._fake_real_path
        cfgmod._yaml_cache = None
        self.addCleanup(self._restore_real_path)
        self.addCleanup(lambda: os.path.exists(self._fake_real_path) and os.remove(self._fake_real_path))

    def _restore_real_path(self):
        cfgmod._CONFIG_PATH = self._orig_cfg_path
        cfgmod._yaml_cache = None

    def test_a_direct_load_config_call_sees_defaults_not_the_marker(self):
        t = make_test_app()
        try:
            cfg = cfgmod.load_config()
            self.assertEqual(cfg['integrations']['home_assistant'],
                             {'enabled': False, 'api_key_hash': ''})
        finally:
            t.cleanup()

    def test_a_route_that_reads_config_at_runtime_sees_defaults_not_the_marker(self):
        """The second half of the real incident: a rendered page, not a direct call."""
        t = make_test_app()
        try:
            r = t.client.get('/api/settings')
            data = r.get_json()
            self.assertEqual(data['integrations']['home_assistant']['enabled'], False)
            self.assertNotIn(self.MARKER, r.get_data(as_text=True))
        finally:
            t.cleanup()


class CooperatesWithAnOuterSandboxTests(unittest.TestCase):
    """A test that already sandboxed _CONFIG_PATH itself before calling make_test_app() owns
    that file's content on purpose - TestApp must defer to it, not overwrite it."""

    def setUp(self):
        fd, self._outer_path = tempfile.mkstemp(suffix='.yaml')
        os.close(fd)
        with open(self._outer_path, 'w') as f:
            yaml.dump({'integrations': {'home_assistant': {
                'enabled': True, 'api_key_hash': 'outer-sandbox-marker-xyz'}}}, f)
        self._orig_cfg_path = cfgmod._CONFIG_PATH
        cfgmod._CONFIG_PATH = self._outer_path
        cfgmod._yaml_cache = None
        self.addCleanup(self._restore)
        self.addCleanup(lambda: os.path.exists(self._outer_path) and os.remove(self._outer_path))

    def _restore(self):
        cfgmod._CONFIG_PATH = self._orig_cfg_path
        cfgmod._yaml_cache = None

    def test_the_outer_sandboxed_content_survives_make_test_app(self):
        t = make_test_app()
        try:
            cfg = cfgmod.load_config()
            self.assertEqual(cfg['integrations']['home_assistant']['api_key_hash'],
                             'outer-sandbox-marker-xyz')
        finally:
            t.cleanup()

    def test_cleanup_does_not_repoint_config_path_away_from_the_outer_sandbox(self):
        """Restoring _CONFIG_PATH on cleanup is the outer sandbox's job, not TestApp's - it
        never touched it in the first place."""
        t = make_test_app()
        t.cleanup()
        self.assertEqual(cfgmod._CONFIG_PATH, self._outer_path)


class DefaultSandboxRestoresOnCleanupTests(unittest.TestCase):
    def test_config_path_is_restored_and_temp_file_removed(self):
        orig_path = cfgmod._CONFIG_PATH
        t = make_test_app()
        sandbox_path = cfgmod._CONFIG_PATH
        self.assertNotEqual(sandbox_path, orig_path)
        self.assertTrue(os.path.exists(sandbox_path))
        t.cleanup()
        self.assertEqual(cfgmod._CONFIG_PATH, orig_path)
        self.assertFalse(os.path.exists(sandbox_path))


class SandboxWritesAreAtomicTests(unittest.TestCase):
    """A concurrent reader must never observe a half-written sandbox config.

    The suite runs real background threads (APScheduler workers, watchdogs, check-window
    dispatch), and every one of them can call load_config() at a moment nobody chose. With
    a truncate-then-dump write, that reader parses whatever bytes exist so far and raises a
    ruamel ParserError about a file no test edited - which is how an unexplained
    `ParserError: while parsing a flow node` landed in test_contention's setUp under a
    sharded run (dev/changelog/724).
    """

    def setUp(self):
        fd, self._path = tempfile.mkstemp(suffix='.yaml')
        os.close(fd)
        write_sandbox_config(self._path, {'config_version': cfgmod.CURRENT_CONFIG_VERSION})
        self._orig = cfgmod._CONFIG_PATH
        cfgmod._CONFIG_PATH = self._path
        cfgmod._yaml_cache = None
        self.addCleanup(lambda: setattr(cfgmod, '_yaml_cache', None))
        self.addCleanup(lambda: setattr(cfgmod, '_CONFIG_PATH', self._orig))
        self.addCleanup(lambda: os.path.exists(self._path) and os.remove(self._path))

    def test_a_reader_thread_never_sees_a_torn_file(self):
        # The payload size is measured, not guessed: it has to make a truncate-then-dump
        # write observably mid-flight without being so large that parsing it slows the
        # reader down enough to miss the window. At 1500 keys the unsafe writer went
        # UNCAUGHT in every trial for exactly that reason; at 400 over 12 rewrites it is
        # caught in every trial, in under half a second.
        big = {'config_version': cfgmod.CURRENT_CONFIG_VERSION,
               'recording': {f'key_{i}': f'value-{i}' * 20 for i in range(400)}}
        small = {'config_version': cfgmod.CURRENT_CONFIG_VERSION,
                 'recording': {'key_0': 'value-0' * 20}}

        errors = []
        torn = []
        stop = threading.Event()

        def _read():
            # _load_config_file(), not load_config(): it is the seam that actually opens
            # and parses the file - load_config() is that plus a _DEFAULTS deepcopy and
            # merge, which is pure per-read cost here and slows the reader down by enough
            # to change how often it lands mid-write. Same parse, more chances to catch it.
            while not stop.is_set():
                try:
                    cfg = cfgmod._load_config_file()
                except Exception as exc:      # the defect: any parse error at all
                    errors.append(repr(exc))
                    return
                # Every write carries the version stamp, so a config without one is a
                # complete parse of an incomplete file - torn in a way YAML tolerated.
                if cfg is not None and 'config_version' not in cfg:
                    torn.append(sorted(cfg)[:5])
                    return

        reader = threading.Thread(target=_read, daemon=True)
        reader.start()
        try:
            for i in range(12):
                write_sandbox_config(self._path, big if i % 2 else small)
        finally:
            stop.set()
            reader.join(timeout=5)

        self.assertEqual(errors, [], f'reader parsed a half-written config: {errors}')
        self.assertEqual(torn, [], f'reader parsed a truncated config: {torn}')


class SandboxFailuresAreNamedTests(unittest.TestCase):
    """Both ways the config sandbox can be unavailable have to say so out loud."""

    def setUp(self):
        self._orig = cfgmod._CONFIG_PATH
        self.addCleanup(lambda: setattr(cfgmod, '_yaml_cache', None))
        self.addCleanup(lambda: setattr(cfgmod, '_CONFIG_PATH', self._orig))

    def test_sandbox_config_refuses_instead_of_silently_doing_nothing(self):
        """A TestApp that deferred to an outer sandbox cannot honor sandbox_config(), and
        returning quietly makes the test fail later on a defaulted value that looks like an
        application bug."""
        fd, outer = tempfile.mkstemp(suffix='.yaml')
        os.close(fd)
        self.addCleanup(lambda: os.path.exists(outer) and os.remove(outer))
        with open(outer, 'w') as f:
            yaml.dump({'config_version': cfgmod.CURRENT_CONFIG_VERSION}, f)
        cfgmod._CONFIG_PATH = outer
        cfgmod._yaml_cache = None

        t = make_test_app()
        try:
            with self.assertRaises(AssertionError) as caught:
                t.sandbox_config({'channel_testing': {'capture_scratch_dir': '/nope'}})
        finally:
            t.cleanup()
        self.assertIn(outer, str(caught.exception),
                      'the message must name the path that pre-empted the sandbox')

    def test_a_stranded_config_path_is_named_at_app_build_time(self):
        """A patch some earlier test never restored: the file is gone but the global still
        points at it, so every config read in the process quietly serves defaults."""
        fd, stranded = tempfile.mkstemp(suffix='.yaml')
        os.close(fd)
        os.remove(stranded)
        cfgmod._CONFIG_PATH = stranded
        cfgmod._yaml_cache = None

        with self.assertRaises(AssertionError) as caught:
            make_test_app()
        self.assertIn(stranded, str(caught.exception))


if __name__ == '__main__':
    unittest.main(verbosity=2)


class SandboxOutputDirsTests(unittest.TestCase):
    """dev/docs/BUGS.md 2026-09-18: a runtime load_config() in a test app saw the default
    /dvr, so a request probing it raised a real storage alert on a machine without /dvr.
    sandbox_output_dirs() is the opt-in that points those runtime reads at the temp dirs."""

    def setUp(self):
        self.t = make_test_app()
        self.addCleanup(self.t.cleanup)

    def test_runtime_load_config_points_every_output_dir_into_the_temp_dir(self):
        self.t.sandbox_output_dirs()
        rec = cfgmod.load_config()['recording']
        for key in ('dvr_output_dir', 'capture_log_dir', 'images_dir'):
            self.assertTrue(rec[key].startswith(self.t._tmpdir), f'{key} = {rec[key]}')
