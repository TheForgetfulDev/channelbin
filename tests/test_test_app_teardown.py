"""TestApp.cleanup() ends every ffmpeg child a recording can leave behind, including the
join and the conversion that run after the capture (dev/docs/BUGS.md 2026-10-18,
dev/changelog/1223).

The capture tier records for real, so a scenario that fails while its recording is still
converting hands a live ffmpeg to nobody: its thread is a daemon and its temp files are about
to be deleted. Stand-in children are a local sleep, never ffmpeg, so the test is about
teardown and nothing else.
"""
import os
import subprocess
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import app.concatenator as concatenator  # noqa: E402
import app.postprocessor as postprocessor  # noqa: E402
from tests.support import make_test_app  # noqa: E402


def _sleeper():
    return subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(120)'],
                            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL)


class CleanupEndsPostCaptureChildrenTests(unittest.TestCase):
    def setUp(self):
        self.procs = []
        self.addCleanup(self._reap)

    def _reap(self):
        for p in self.procs:
            if p.poll() is None:
                p.kill()
            p.wait()

    def test_a_live_conversion_and_a_live_join_do_not_outlive_the_app(self):
        t = make_test_app()
        conversion, join = _sleeper(), _sleeper()
        self.procs += [conversion, join]
        with postprocessor._active_lock:
            postprocessor._active_conversions[901] = conversion
        partial = os.path.join(t._tmpdir, 'join-partial.ts')
        open(partial, 'wb').close()
        with concatenator._active_join_procs_lock:
            concatenator._active_join_procs[902] = (join, partial)
        t.cleanup()
        self.assertIsNotNone(conversion.poll(), 'the conversion child is still running')
        self.assertIsNotNone(join.poll(), 'the join child is still running')
        self.assertNotIn(901, postprocessor._active_conversions)
        self.assertNotIn(902, concatenator._active_join_procs)


if __name__ == '__main__':
    unittest.main()
