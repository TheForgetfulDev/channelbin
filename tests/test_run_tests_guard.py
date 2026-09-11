"""run_tests.sh must refuse to run a suite that would silently shrink.

Guards dev/docs/BUGS.md 2026-09-10 09:38 PM. Roughly 408 tests gate themselves on
shutil.which('ffmpeg')/('ffprobe'), and unittest treats a skip as success - so with neither
binary on PATH the whole suite finishes in a fraction of the usual time, prints OK, and has
exercised no capture, probe, conversion or screenshot code whatsoever. Nothing in that output
distinguishes it from a real green run.

That is not hypothetical twice over: it is what CI did for months (dev/changelog/907), and it
reappeared on the dev box the moment ffmpeg moved out of /usr/bin, because any shell still
holding a PATH from before the move finds nothing and says nothing (dev/changelog/917).

Neither test here ever reaches the suite itself - one stops at the guard, and the other is
given a PATH with no python3 so the exec dies immediately. A test that actually let
run_tests.sh through would run the entire suite inside the suite.
"""
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPT = os.path.join(ROOT, 'run_tests.sh')


@unittest.skipUnless(os.path.exists(SCRIPT), 'no run_tests.sh in this checkout')
class RunTestsFfmpegGuardTests(unittest.TestCase):

    def setUp(self):
        # A PATH holding exactly what the script needs before the guard and nothing else, so
        # the absence of ffmpeg is a property of this test rather than of the machine running
        # it - a contributor whose /usr/bin has ffmpeg must still see the guard fire.
        self._dir = tempfile.mkdtemp(prefix='dvr_guard_path_')
        self.addCleanup(shutil.rmtree, self._dir, True)
        for tool in ('dirname',):
            real = shutil.which(tool)
            if not real:
                self.skipTest(f'{tool} not available to build an isolated PATH')
            os.symlink(real, os.path.join(self._dir, tool))

    def _run(self, path, env_extra=None):
        env = dict(os.environ, PATH=path)
        env.pop('CHANNELBIN_ALLOW_MISSING_FFMPEG', None)
        env.update(env_extra or {})
        return subprocess.run([shutil.which('bash') or '/bin/bash', SCRIPT],
                              capture_output=True, timeout=60, env=env)

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
        self.assertNotIn(b'Refusing instead', r.stderr)
        self.assertNotEqual(r.returncode, 0, 'expected the missing python3 to end this run')

    def test_a_present_toolchain_is_not_refused(self):
        """The guard must not fire on the machines it is meant to let through."""
        if not (shutil.which('ffmpeg') and shutil.which('ffprobe')):
            self.skipTest('no real ffmpeg/ffprobe on PATH to prove the negative')
        fake_bin = os.path.join(self._dir, 'bin')
        os.makedirs(fake_bin)
        for tool in ('ffmpeg', 'ffprobe'):
            os.symlink(shutil.which(tool), os.path.join(fake_bin, tool))
        # No python3 on this PATH either, so the guard passes and the exec fails - the point
        # is only that the failure is not the refusal.
        r = self._run(os.pathsep.join([fake_bin, self._dir]))
        self.assertNotIn(b'Refusing instead', r.stderr)


if __name__ == '__main__':
    sys.exit(unittest.main())
