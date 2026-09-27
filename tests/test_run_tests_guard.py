"""run_tests.sh must refuse to run a suite that would silently shrink.

Guards dev/docs/BUGS.md 2026-09-10 09:38 PM. Roughly 408 tests gate themselves on
shutil.which('ffmpeg')/('ffprobe'), and unittest treats a skip as success - so with neither
binary on PATH the whole suite finishes in a fraction of the usual time, prints OK, and has
exercised no capture, probe, conversion or screenshot code whatsoever. Nothing in that output
distinguishes it from a real green run.

That is not hypothetical twice over: it is what CI did for months (dev/changelog/907), and it
reappeared on the dev box the moment ffmpeg moved out of /usr/bin, because any shell still
holding a PATH from before the move finds nothing and says nothing (dev/changelog/917).

The client-side suite has the same trap one layer over (dev/docs/BUGS.md 2026-09-26,
dev/changelog/1135): every jsdom-backed test gates on `node` and node_modules/jsdom, and
node_modules is gitignored, so a fresh clone runs none of them and still reports OK.

No test here ever reaches the suite itself - each one either stops at a guard or is given a
PATH with no python3 so the exec dies immediately. A test that actually let run_tests.sh
through would run the entire suite inside the suite.
"""
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPT = os.path.join(ROOT, 'run_tests.sh')
JSDOM = os.path.join(ROOT, 'node_modules', 'jsdom')
OVERRIDES = ('CHANNELBIN_ALLOW_MISSING_FFMPEG', 'CHANNELBIN_ALLOW_MISSING_NODE')


def _isolated_path(test):
    """A temp dir holding exactly what the script needs before its guards, and nothing else.

    The absence of a tool is then a property of the test rather than of the machine running
    it - a contributor whose /usr/bin has ffmpeg or node must still see the guard fire.
    """
    d = tempfile.mkdtemp(prefix='dvr_guard_path_')
    test.addCleanup(shutil.rmtree, d, True)
    for tool in ('dirname',):
        real = shutil.which(tool)
        if not real:
            test.skipTest(f'{tool} not available to build an isolated PATH')
        os.symlink(real, os.path.join(d, tool))
    return d


def _link_tools(test, into, tools):
    """Symlink the real `tools` into `into`, skipping the test when one is not installed."""
    os.makedirs(into, exist_ok=True)
    for tool in tools:
        real = shutil.which(tool)
        if not real:
            test.skipTest(f'no real {tool} on PATH to link into the isolated one')
        os.symlink(real, os.path.join(into, tool))
    return into


def _run(script, path, env_extra=None):
    env = dict(os.environ, PATH=path)
    for key in OVERRIDES:
        env.pop(key, None)
    env.update(env_extra or {})
    return subprocess.run([shutil.which('bash') or '/bin/bash', script],
                          capture_output=True, timeout=60, env=env)


@unittest.skipUnless(os.path.exists(SCRIPT), 'no run_tests.sh in this checkout')
class RunTestsFfmpegGuardTests(unittest.TestCase):

    def setUp(self):
        self._dir = _isolated_path(self)

    def _run(self, path, env_extra=None):
        # The node guard is not under test here; with it overridden, a refusal can only be
        # the ffmpeg one.
        return _run(SCRIPT, path, dict({'CHANNELBIN_ALLOW_MISSING_NODE': '1'}, **(env_extra or {})))

    def test_it_refuses_when_ffmpeg_is_missing(self):
        r = self._run(self._dir)
        self.assertEqual(
            r.returncode, 1,
            'run_tests.sh ran with no ffmpeg on PATH, so ~408 tests skipped themselves and '
            f'the run reported green over a smaller suite. stdout={r.stdout[:400]!r}')
        self.assertIn(b'ffmpeg', r.stderr)
        self.assertIn(b'ffprobe', r.stderr)

    def test_the_refusal_names_the_path_it_searched(self):
        """The fix is almost always the PATH itself, so print it rather than make them ask."""
        r = self._run(self._dir)
        self.assertIn(self._dir.encode(), r.stderr)

    def test_it_can_be_overridden_deliberately(self):
        """Skipping them stays possible - it just has to be chosen, not defaulted into.

        Given no python3 either, the run dies at the exec instead of running the suite, which
        is all this needs to prove: it got past the guard.
        """
        r = self._run(self._dir, {'CHANNELBIN_ALLOW_MISSING_FFMPEG': '1'})
        self.assertNotIn(b'Refusing', r.stderr)
        self.assertNotEqual(r.returncode, 0, 'expected the missing python3 to end this run')


@unittest.skipUnless(os.path.exists(SCRIPT), 'no run_tests.sh in this checkout')
class RunTestsNodeGuardTests(unittest.TestCase):
    """Guards dev/docs/BUGS.md 2026-09-26 @ "run_tests.sh reported green with no node or jsdom"."""

    def setUp(self):
        self._dir = _isolated_path(self)

    def _run(self, path, script=SCRIPT, env_extra=None):
        # The ffmpeg guard is not under test here; with it overridden, a refusal can only be
        # the node one.
        return _run(script, path,
                    dict({'CHANNELBIN_ALLOW_MISSING_FFMPEG': '1'}, **(env_extra or {})))

    def test_it_refuses_when_node_is_missing(self):
        r = self._run(self._dir)
        self.assertEqual(
            r.returncode, 1,
            'run_tests.sh ran with no node on PATH, so every jsdom-backed test skipped itself '
            f'and the run reported green. stdout={r.stdout[:400]!r}')
        self.assertIn(b'node', r.stderr)
        self.assertIn(self._dir.encode(), r.stderr, 'the refusal must name the PATH it searched')

    def test_it_refuses_when_jsdom_is_not_installed(self):
        """node on PATH is not enough: node_modules is gitignored, so a fresh clone has none.

        The script is run from a copy in an empty directory, so the missing node_modules is a
        property of this test and the real one is never touched.
        """
        _link_tools(self, self._dir, ('node',))
        bare = tempfile.mkdtemp(prefix='dvr_guard_checkout_')
        self.addCleanup(shutil.rmtree, bare, True)
        script = os.path.join(bare, 'run_tests.sh')
        shutil.copy(SCRIPT, script)
        r = self._run(self._dir, script=script)
        self.assertEqual(
            r.returncode, 1,
            'run_tests.sh ran with no node_modules/jsdom, so every jsdom-backed test skipped '
            f'itself and the run reported green. stdout={r.stdout[:400]!r}')
        self.assertIn(b'node_modules/jsdom', r.stderr)
        self.assertIn(b'npm ci', r.stderr, 'the refusal must say how to fix it')

    def test_it_can_be_overridden_deliberately(self):
        r = self._run(self._dir, env_extra={'CHANNELBIN_ALLOW_MISSING_NODE': '1'})
        self.assertNotIn(b'Refusing', r.stderr)
        self.assertNotEqual(r.returncode, 0, 'expected the missing python3 to end this run')

    def test_a_missing_ffmpeg_and_node_are_both_named_in_one_run(self):
        """A fresh checkout missing both hears about both, not one per attempt."""
        r = _run(SCRIPT, self._dir)
        self.assertEqual(r.returncode, 1)
        self.assertIn(b'ffprobe', r.stderr)
        self.assertIn(b'node', r.stderr)


@unittest.skipUnless(os.path.exists(SCRIPT), 'no run_tests.sh in this checkout')
class RunTestsPresentToolchainTests(unittest.TestCase):

    def test_a_present_toolchain_is_not_refused(self):
        """Neither guard may fire on the machines they are meant to let through."""
        if not os.path.isdir(JSDOM):
            self.skipTest('no node_modules/jsdom in this checkout to prove the negative')
        d = _isolated_path(self)
        fake_bin = _link_tools(self, os.path.join(d, 'bin'), ('ffmpeg', 'ffprobe', 'node'))
        # No python3 on this PATH either, so the guards pass and the exec fails - the point
        # is only that the failure is not a refusal.
        r = _run(SCRIPT, os.pathsep.join([fake_bin, d]))
        self.assertNotIn(b'Refusing', r.stderr)
        self.assertNotEqual(r.returncode, 0, 'expected the missing python3 to end this run')


if __name__ == '__main__':
    sys.exit(unittest.main())
