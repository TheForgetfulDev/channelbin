"""A whole-file probe is bounded by progress, not by a wall clock.

Guards dev/docs/BUGS.md 2026-08-24: parse_ffprobe() gave -count_packets a fixed 60s
deadline, but -count_packets reads the entire file, so its runtime scales with size. A
17.9 GB capture needs ~124s on this machine's mount and so could never finish inside its
own deadline - every recording that large silently lost its health data, and the failure
looked identical to a genuinely hung probe.

No ffprobe here: the helper takes any argv, so these drive python children whose reading
behavior is exactly controlled. Local files only - netguard forbids the network.
"""
import os
import subprocess
import sys
import time
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.probe import run_probe_until_stalled, _bytes_read  # noqa: E402


class ProgressDeadlineTests(unittest.TestCase):
    def setUp(self):
        self.big = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'fixtures',
                                '_probe_progress_scratch.bin')
        with open(self.big, 'wb') as fh:
            fh.write(b'\0' * (4 * 1024 * 1024))

    def tearDown(self):
        try:
            os.unlink(self.big)
        except OSError:
            pass  # best-effort scratch cleanup

    def _reader_cmd(self, seconds):
        """A child that keeps reading its file for `seconds`, then reports success."""
        return [sys.executable, '-c',
                'import sys,time\n'
                'deadline = time.time() + float(sys.argv[2])\n'
                'while time.time() < deadline:\n'
                '    with open(sys.argv[1], "rb") as fh:\n'
                '        while fh.read(65536):\n'
                '            pass\n'
                '    time.sleep(0.05)\n'
                'sys.stdout.write("done")\n',
                self.big, str(seconds)]

    def _idle_cmd(self, seconds):
        """A child that reads nothing at all - the shape a hung probe has."""
        return [sys.executable, '-c', 'import sys,time; time.sleep(float(sys.argv[1]))',
                str(seconds)]

    def test_a_probe_that_keeps_reading_outlives_the_wall_clock(self):
        """The defect, directly: work that is plainly progressing must not be killed just
        because it passed a fixed deadline.

        The old code path is run here too rather than described, so this test carries its
        own proof: reverting app/probe.py only breaks this module's import, which is not
        evidence that the invariant is covered (CLAUDE.md, Testing).
        """
        # What parse_ffprobe used to do with this child: killed on the clock, mid-read.
        with self.assertRaises(subprocess.TimeoutExpired):
            subprocess.run(self._reader_cmd(3), capture_output=True, text=True, timeout=1)

        started = time.monotonic()
        rc, out = run_probe_until_stalled(self._reader_cmd(3), stall_timeout=5,
                                          fallback_timeout=1)
        elapsed = time.monotonic() - started
        self.assertEqual(rc, 0)
        self.assertEqual(out, 'done')
        self.assertGreater(elapsed, 1.5,
                           'the child was supposed to run well past fallback_timeout')

    def test_a_probe_that_stops_reading_is_killed(self):
        """Progress is the deadline, so no progress still has to end the probe."""
        started = time.monotonic()
        with self.assertRaises(subprocess.TimeoutExpired):
            run_probe_until_stalled(self._idle_cmd(30), stall_timeout=1,
                                    fallback_timeout=60)
        self.assertLess(time.monotonic() - started, 10,
                        'a stalled probe must not be allowed to hang the pipeline')

    def test_no_progress_signal_falls_back_to_the_wall_clock(self):
        """An unreadable /proc must not turn every probe unbounded."""
        with mock.patch('app.probe._bytes_read', return_value=None):
            started = time.monotonic()
            with self.assertRaises(subprocess.TimeoutExpired):
                run_probe_until_stalled(self._idle_cmd(30), stall_timeout=60,
                                        fallback_timeout=1)
            self.assertLess(time.monotonic() - started, 10)

    def test_bytes_read_advances_for_a_reading_process(self):
        """The progress signal itself. rchar, not read_bytes: read_bytes stays at 0 for a
        file on a network mount, which is where this app's long probes actually run."""
        proc = subprocess.Popen(self._reader_cmd(3), stdout=subprocess.DEVNULL,
                                stderr=subprocess.DEVNULL)
        try:
            time.sleep(0.5)
            first = _bytes_read(proc.pid)
            time.sleep(1.0)
            second = _bytes_read(proc.pid)
        finally:
            proc.kill()
            proc.wait()
        self.assertIsNotNone(first, '/proc/<pid>/io is the progress signal on this platform')
        self.assertGreater(second, first)

    def test_bytes_read_is_none_for_a_dead_process(self):
        self.assertIsNone(_bytes_read(999999999))


class ParseFfprobeRoutingTests(unittest.TestCase):
    """count_packets is what makes a probe whole-file, so it is what selects the
    progress-bounded path. A header-only probe should finish in about a second and keeps
    its plain deadline, where a slow one really is a hung one."""

    def test_count_packets_uses_the_progress_bounded_runner(self):
        from app import probe

        with mock.patch.object(probe, 'run_probe_until_stalled',
                               return_value=(0, '{"streams": [], "format": {}}')) as fake:
            probe.parse_ffprobe(__file__, count_packets=True)
        self.assertTrue(fake.called)

    def test_header_only_probe_keeps_the_plain_deadline(self):
        from app import probe

        with mock.patch.object(probe, 'run_probe_until_stalled') as fake:
            probe.parse_ffprobe(__file__, count_packets=False, timeout=1)
        self.assertFalse(fake.called,
                         'a header-only probe must not be given an unbounded runtime')


if __name__ == '__main__':
    unittest.main()
