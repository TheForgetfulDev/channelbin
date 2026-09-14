"""Guards dev/docs/BUGS.md 2026-08-14 "Channel-test capture clips are written to the
system temp dir instead of /dvr/tmp" as refined by dev/changelog/642: the capture-clip
scratch directory defaults to the system temp dir (not a hardcoded app-output path) and
is redirectable via channel_testing.capture_scratch_dir for an operator whose system temp
dir is constrained.

No real ffmpeg: subprocess.Popen is monkeypatched, following the pattern in
tests/test_channel_tester_stderr_masking.py. tempfile.mkstemp itself is spied on (still
delegating to the real implementation) so the test can assert which directory the clip
actually landed in.
"""
import os
import sys
import tempfile as _tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from unittest import mock  # noqa: E402

from tests.support.app import make_test_app  # noqa: E402
from tests.support import seed  # noqa: E402
from app import db  # noqa: E402
from app import channel_tester  # noqa: E402


class FakeProc:
    """A Popen'd ffmpeg that has already exited, so the tester classifies it via
    stderr/exit code rather than waiting on a real connection."""

    def __init__(self):
        self.stderr = []
        self._returncode = 1
        self.signals = []

    def send_signal(self, sig):
        # terminate_or_kill() continues a possibly-suspended child before terminating it
        # (dev/changelog/952), so a stand-in that cannot take a signal is not a faithful one.
        self.signals.append(sig)

    def poll(self):
        return self._returncode

    def terminate(self):
        self._returncode = -15

    def kill(self):
        self._returncode = -9

    def wait(self, timeout=None):
        return self._returncode


class ChannelTestScratchDirTests(unittest.TestCase):
    def setUp(self):
        self.t = make_test_app()
        with self.t.app.app_context():
            acct = seed.make_account(name='Scratch Dir Account')
            ch = seed.make_channel(acct, name='Scratch Dir Channel')
            ch.stream_url = 'http://example.test/live/1'
            db.session.commit()
            self.channel_id = ch.id
        # No _reset_run_state() here: make_test_app() has already swapped in a fresh
        # RunState via reset_module_globals(), and _reset_run_state() is a start-of-run
        # function that takes the KIND_TESTER admission ticket (dev/changelog/723).

    def tearDown(self):
        # _end_run(), not _reset_run_state(): a test that drove a run to completion may
        # still hold the ticket, and this is the one path that gives it back.
        channel_tester._end_run()
        self.t.cleanup()

    def _run_with_spied_mkstemp(self):
        dirs_used = []
        real_mkstemp = _tempfile.mkstemp

        def _spy_mkstemp(*a, **kw):
            dirs_used.append(kw.get('dir'))
            return real_mkstemp(*a, **kw)

        def _popen(*a, **kw):
            return FakeProc()

        with mock.patch.object(channel_tester.subprocess, 'Popen', _popen), \
             mock.patch.object(channel_tester, 'wait_for_file_data', lambda *a, **kw: False), \
             mock.patch.object(channel_tester, '_interruptible_sleep', lambda *a, **kw: None), \
             mock.patch.object(channel_tester.tempfile, 'mkstemp', _spy_mkstemp):
            test_id = channel_tester.run_channel_test(self.t.app, self.channel_id)

        self.assertIsNotNone(test_id)
        self.assertEqual(len(dirs_used), 1, 'expected exactly one capture-clip mkstemp call')
        return dirs_used[0]

    def test_default_capture_dir_is_the_system_temp_dir_not_a_hardcoded_path(self):
        used_dir = self._run_with_spied_mkstemp()
        self.assertIsNone(used_dir, 'dir=None lets tempfile resolve the system temp dir itself')

    def test_configured_capture_scratch_dir_is_honored_and_created(self):
        configured_dir = os.path.join(self.t._tmpdir, 'custom_scratch')
        self.t.sandbox_config({'channel_testing': {'capture_scratch_dir': configured_dir}})

        used_dir = self._run_with_spied_mkstemp()

        self.assertEqual(used_dir, configured_dir)
        self.assertTrue(os.path.isdir(configured_dir), 'configured scratch dir must be created if missing')


if __name__ == '__main__':
    unittest.main()
