"""load_config() mtime cache (BUGS.md 2026-07-20: `/` took ~3s because the local_time*
template filters re-read + re-parsed config.yaml 4x per recording row).

Same defect class as tests/test_scaling.py (per-row disk I/O, BUGS.md 2026-07-15 10:34),
but the fix shape differs: load_config() is still *called* per filter invocation - it is
the file parse that must be O(1) per request. So the scaling test here counts actual
parses via the app.config._parse_config_file seam, not load_config() calls.

Cache-behavior tests point _CONFIG_PATH at a temp file so the real config.yaml is never
touched; every test resets the module-global _yaml_cache so counts are deterministic.
"""
import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import app.config as config_mod  # noqa: E402
from tests.support.app import make_test_app  # noqa: E402
from tests.support.iocount import IOCounter  # noqa: E402
from tests.support import seed  # noqa: E402
from app import db  # noqa: E402


class ConfigCacheTests(unittest.TestCase):
    """Cache semantics against a throwaway config file."""

    def setUp(self):
        fd, self._tmp_path = tempfile.mkstemp(suffix='.yaml')
        os.close(fd)
        self._orig_path = config_mod._CONFIG_PATH
        config_mod._CONFIG_PATH = self._tmp_path
        config_mod._yaml_cache = None
        self._write("display:\n  timezone: America/Chicago\nffmpeg:\n  extra_input_args: ['-timeout', '5000000']\n")

    def tearDown(self):
        config_mod._CONFIG_PATH = self._orig_path
        config_mod._yaml_cache = None
        os.unlink(self._tmp_path)

    def _write(self, text, mtime_ns=None):
        with open(self._tmp_path, 'w') as f:
            f.write(text)
        if mtime_ns is not None:
            os.utime(self._tmp_path, ns=(mtime_ns, mtime_ns))

    def test_unchanged_file_parses_once(self):
        with IOCounter() as counter:
            for _ in range(50):
                config_mod.load_config()
        self.assertEqual(counter.config_parses, 1,
                         f'unchanged config.yaml was parsed {counter.config_parses}x across 50 '
                         f'load_config() calls - the mtime cache is not being hit')

    def test_modified_file_is_picked_up(self):
        self.assertEqual(config_mod.load_config()['display']['timezone'], 'America/Chicago')
        # Force a distinct mtime explicitly - a same-nanosecond rewrite would be
        # invisible to the cache key and make this test flaky.
        new_ns = os.stat(self._tmp_path).st_mtime_ns + 1_000_000
        self._write('display:\n  timezone: America/Denver\n', mtime_ns=new_ns)
        self.assertEqual(config_mod.load_config()['display']['timezone'], 'America/Denver')

    def test_deleted_file_falls_back_to_defaults(self):
        config_mod.load_config()
        os.unlink(self._tmp_path)
        try:
            cfg = config_mod.load_config()
            self.assertEqual(cfg['display']['timezone'],
                             config_mod._DEFAULTS['display']['timezone'])
        finally:
            self._write('')  # recreate so tearDown's unlink succeeds

    def test_mutating_returned_config_does_not_poison_cache(self):
        cfg1 = config_mod.load_config()
        cfg1['ffmpeg']['extra_input_args'].append('-mutated')
        cfg1['display']['timezone'] = 'UTC'
        cfg2 = config_mod.load_config()
        self.assertEqual(cfg2['ffmpeg']['extra_input_args'], ['-timeout', '5000000'])
        self.assertEqual(cfg2['display']['timezone'], 'America/Chicago')

    def test_overrides_seam_unaffected_by_cache(self):
        cfg = config_mod.load_config({'display': {'timezone': 'Europe/London'}})
        self.assertEqual(cfg['display']['timezone'], 'Europe/London')
        # ...and the override must not leak into the next plain call via the cache.
        self.assertEqual(config_mod.load_config()['display']['timezone'], 'America/Chicago')


class IndexPageScalingTests(unittest.TestCase):
    """config.yaml parse count during a `/recordings` render must be independent of row count."""

    def _parses_for(self, n_recordings):
        t = make_test_app()
        try:
            base = datetime.utcnow() + timedelta(days=1)
            for i in range(n_recordings):
                seed.make_recording(status='COMPLETED', name=f'rec_{i}',
                                    start_time=base + timedelta(hours=i),
                                    stop_time=base + timedelta(hours=i, minutes=30))
            db.session.commit()
            config_mod._yaml_cache = None
            with IOCounter() as counter:
                resp = t.client.get('/recordings')
            self.assertEqual(resp.status_code, 200,
                             f'/recordings returned {resp.status_code}, not 200')
            return counter.config_parses
        finally:
            t.cleanup()

    def test_index_config_parses_do_not_scale_with_rows(self):
        small = self._parses_for(5)
        large = self._parses_for(100)
        self.assertEqual(
            small, large,
            f'config.yaml parse count scales with recording rows ({small} at 5 rows vs '
            f'{large} at 100 rows) - a per-row code path is defeating the load_config() '
            f'mtime cache (BUGS.md 2026-07-20).')


class CreateAppConfigReadTests(unittest.TestCase):
    """create_app() must open config.yaml at most once (BUGS.md 2026-07-24).

    Counts real open() calls on the file rather than using IOCounter, deliberately: that
    counter patches app.config._parse_config_file, which is precisely the seam a direct
    yaml.safe_load bypasses. Both offending startup paths were invisible to it - they
    opened the file themselves. Anything that counts only the cached reader cannot see
    this defect class, so this test counts the syscall-level thing instead.
    """

    def _opens_during_create_app(self):
        import builtins
        real_open = builtins.open
        target = os.path.realpath(config_mod._CONFIG_PATH)
        seen = []

        def counting_open(file, mode='r', *a, **kw):
            try:
                if 'w' not in mode and 'a' not in mode and 'x' not in mode \
                        and os.path.realpath(file) == target:
                    seen.append(mode)
            except (TypeError, ValueError, OSError):
                pass  # fd/PathLike we can't resolve - not the config file we're watching
            return real_open(file, mode, *a, **kw)

        # Cold cache, so the count is deterministic rather than depending on whatever
        # earlier tests left parsed.
        config_mod._yaml_cache = None
        builtins.open = counting_open
        try:
            t = make_test_app()
        finally:
            builtins.open = real_open
        t.cleanup()
        return len(seen)

    def test_create_app_reads_config_yaml_at_most_once(self):
        opens = self._opens_during_create_app()
        self.assertLessEqual(
            opens, 1,
            f'create_app() opened config.yaml {opens}x for reading - it must go through '
            f'load_config()/_load_config_file() so the mtime cache serves every read after '
            f'the first. Each extra parse costs ~13ms on every app build, which the test '
            f'suite pays once per test (BUGS.md 2026-07-24).')


class LoadConfigDefaultsIsolationTests(unittest.TestCase):
    """load_config() must never hand back a dict that shares a nested object with
    _DEFAULTS itself (BUGS.md 2026-08-06).

    Found via the password-gate feature: set_nested() mutates in place via
    dict.setdefault(), and a top-level section absent from config.yaml (true of every
    section until its first save) used to pass straight through as a bare reference to
    _DEFAULTS's own nested dict - so writing one field on a never-customized section
    permanently corrupted the process-wide defaults for every other config.yaml, for the
    rest of the process's life.
    """

    def setUp(self):
        fd, self._tmp_path = tempfile.mkstemp(suffix='.yaml')
        os.close(fd)
        self._orig_path = config_mod._CONFIG_PATH
        config_mod._CONFIG_PATH = self._tmp_path
        config_mod._yaml_cache = None
        with open(self._tmp_path, 'w') as f:
            f.write('config_version: 1\n')  # a section nothing has ever customized

    def tearDown(self):
        config_mod._CONFIG_PATH = self._orig_path
        config_mod._yaml_cache = None
        os.unlink(self._tmp_path)

    def test_mutating_an_uncustomized_section_does_not_poison_defaults(self):
        cfg = config_mod.load_config()
        config_mod.set_nested(cfg, 'auth.password_hash', 'poisoned-value')
        self.assertEqual(config_mod._DEFAULTS['auth']['password_hash'], '',
                         'set_nested() on a load_config() result mutated the global '
                         '_DEFAULTS - every load_config() call in this process from now '
                         'on would inherit the poisoned value as its default')

    def test_a_second_unrelated_load_never_sees_the_first_calls_mutation(self):
        cfg1 = config_mod.load_config()
        config_mod.set_nested(cfg1, 'auth.password_hash', 'poisoned-value')
        cfg2 = config_mod.load_config()
        self.assertEqual(cfg2['auth']['password_hash'], '')


if __name__ == '__main__':
    unittest.main(verbosity=2)
