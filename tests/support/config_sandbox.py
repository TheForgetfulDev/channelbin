"""The canonical config.yaml sandbox for tests.

`make_test_app()` now sandboxes config.yaml by default too (tests/support/app.py::TestApp,
dev/changelog/605) - a plain `self.t = make_test_app()` with no `ConfigSandbox` already gets
an isolated config.yaml for the app's whole lifetime, holding only its own temp output and
backup dirs (dev/changelog/1130), so this class is no longer the only thing standing between
a test and the real repo-root file. Reach for `ConfigSandbox` now specifically when a test
needs to (a) control the whole of what the sandboxed file contains - the `_write_cfg()`
below; `TestApp.sandbox_config()` covers the usual case of adding values - or (b) sandbox
`load_config()` outright, with no `make_test_app()`/Flask app involved at all. The runtime
output and backup dirs are sandboxed here too: the file starts out naming temp dirs of this
class's own, and `_write_cfg()` merges them under whatever the test writes, so a runtime read
never resolves the real `/dvr` or config-backups folder. A test about the defaults or about
the file's exact contents passes `keep_sandbox_paths=False`, and a `make_test_app()` built
on top refuses to start while any of them points outside the temp dir.

Before the default existed, this was the *only* fix: `make_test_app()` passed
`extra_overrides` to `create_app()` once, and those overrides were never stored - so any
code-under-test that called `load_config()` at *run time* (a route, a scheduled job, a
background thread) read the **real repo-root config.yaml**, not the test's config. That is
the sandbox escape CLAUDE.md's Testing section warns about, and it was not theoretical:
three tests asserting on `integrations.home_assistant` defaults failed on any machine whose
real config.yaml had the Home Assistant integration enabled, and one of them printed the
real stored `api_key_hash` into the failure output (dev/changelog/515, dev/docs/BUGS.md
2026-08-09).

Patching `app.config.load_config` does not reliably fix that: a module-top
`from ..config import load_config` (which app/routes/settings.py uses) binds the original
function, so the patch never reaches it. Patching `_CONFIG_PATH` does work everywhere,
because `load_config()` resolves it on every parse - which is what this class does.

Subclass it for any test that asserts on config-derived behavior:

    class MyTests(ConfigSandbox):
        def setUp(self):
            super().setUp()
            self._write_cfg({'integrations': {'home_assistant': {'enabled': True}}})

With no `_write_cfg()` call the sandbox carries a config_version stamp and the temp dirs,
i.e. `_DEFAULTS` everywhere but those four paths - which is what "the defaults" tests should
assert against, unless the default they assert on is one of the paths.
`_write_cfg()` overwrites the stamp too, so a test that wants to force a real migration
must put `config_version` back deliberately (or omit it) rather than relying on the
sandbox default.
"""
import os
import shutil
import tempfile
import unittest
from unittest import mock

from app import config as cfgmod
from app.config import _deep_merge
from tests.support.app import sandbox_runtime_paths, write_sandbox_config


class ConfigSandbox(unittest.TestCase):
    """Point app.config at a temp config.yaml for the life of one test."""

    def setUp(self):
        self._sandbox_dir = tempfile.mkdtemp(prefix='dvr_cfg_sandbox_')
        self.addCleanup(shutil.rmtree, self._sandbox_dir, ignore_errors=True)
        self._sandbox_paths = sandbox_runtime_paths(self._sandbox_dir)
        fd, self._cfg_path = tempfile.mkstemp(suffix='.yaml')
        os.close(fd)
        # Stamped at the current config_version so a make_test_app() built on top of this
        # sandbox doesn't take migrate_config()'s backup-then-migrate branch on every build
        # (dev/changelog/620) - matches tests/support/app.py::TestApp's own default sandbox.
        self._write_cfg({'config_version': cfgmod.CURRENT_CONFIG_VERSION})
        self._cfg_patch = mock.patch.object(cfgmod, '_CONFIG_PATH', self._cfg_path)
        self._cfg_patch.start()
        # The mtime cache keys on the path it last parsed, so it has to be dropped both on
        # the way in and on the way out - otherwise a sandboxed parse leaks into the next
        # test, or the real file's cached parse leaks into this one.
        cfgmod._yaml_cache = None
        self.addCleanup(self._cfg_patch.stop)
        self.addCleanup(lambda: setattr(cfgmod, '_yaml_cache', None))
        self.addCleanup(lambda: os.path.exists(self._cfg_path) and os.remove(self._cfg_path))

    def _write_cfg(self, data, keep_sandbox_paths=True):
        """Replace the sandboxed file with `data`, merged over this sandbox's temp output
        and backup dirs - a dir `data` names itself wins. keep_sandbox_paths=False writes
        `data` alone, for a test about the defaults or the file's exact contents; runtime
        reads then resolve those dirs to the real ones, so such a test must not reach a
        capture, a probe or a config backup."""
        if keep_sandbox_paths:
            data = _deep_merge(self._sandbox_paths, data)
        write_sandbox_config(self._cfg_path, data)
