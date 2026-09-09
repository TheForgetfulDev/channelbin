"""A hung ffprobe must not block scan_video_timeline forever.

Guards dev/docs/BUGS.md 2026-08-30 09:21:46 AM ET: `timeout` was only ever handed to
`proc.wait()`, which is reached AFTER `for line in proc.stdout` has already drained the
child to EOF. An ffprobe that stops producing lines mid-scan blocks in that loop forever
and the deadline never gets a chance to run. `scan_video_timeline` is called from
`do_postprocess`'s pre-conversion step, so a hang here strands a finished recording.

No real ffprobe is spawned - `subprocess.Popen` is patched so the command that actually
runs is a controlled python child, and `_nominal_fps` is patched directly so the fps
lookup (a separate `subprocess.run` call) never touches a process at all. Every assertion
that exercises the hang runs the call off the main thread with a bounded `join()`, so an
unfixed `scan_video_timeline` fails this test in a few seconds rather than hanging the
whole suite.
"""
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import probe  # noqa: E402

_RealPopen = subprocess.Popen

_IDLE_CMD = [sys.executable, '-c', 'import time; time.sleep(30)']


def _line_emitting_cmd(count, interval):
    """A child that prints `count` valid-looking CSV packet lines, `interval` apart, then
    exits - the shape a slow-but-healthy scan of a large file has."""
    return [sys.executable, '-c',
            'import sys, time\n'
            'n = int(sys.argv[1]); interval = float(sys.argv[2])\n'
            'for i in range(n):\n'
            '    print(f"{i * interval:.3f},{i * interval:.3f}")\n'
            '    sys.stdout.flush()\n'
            '    time.sleep(interval)\n',
            str(count), str(interval)]


class ScanVideoTimelineStallTests(unittest.TestCase):
    def setUp(self):
        fd, self.stub_path = tempfile.mkstemp(prefix='scan-timeline-stub-')
        os.close(fd)
        self._spawned = []

    def tearDown(self):
        for proc in self._spawned:
            if proc.poll() is None:
                proc.kill()
                proc.wait()
        try:
            os.unlink(self.stub_path)
        except OSError:
            pass  # best-effort scratch cleanup

    def _fake_popen(self, cmd):
        def _spawn(_cmd, **kwargs):
            proc = _RealPopen(cmd, **kwargs)
            self._spawned.append(proc)
            return proc
        return _spawn

    def test_a_hung_ffprobe_is_killed_within_the_stall_window(self):
        """The exact defect: a scan whose ffprobe stops emitting lines must return, not
        block forever, once its stall window elapses."""
        result = {}

        def _run():
            result['metrics'] = probe.scan_video_timeline(self.stub_path, timeout=1)

        with mock.patch.object(probe, '_nominal_fps', return_value=25.0), \
             mock.patch.object(probe.subprocess, 'Popen', side_effect=self._fake_popen(_IDLE_CMD)):
            started = time.monotonic()
            t = threading.Thread(target=_run, daemon=True)
            t.start()
            t.join(timeout=10)

        self.assertFalse(t.is_alive(),
                         'scan_video_timeline is still blocked on a hung ffprobe after 10s')
        self.assertLess(time.monotonic() - started, 10)
        self.assertEqual(result.get('metrics'), {}, 'a stalled scan must report failure, not damage')
        self.assertTrue(self._spawned, 'the fake ffprobe was never actually spawned')
        self.assertIsNotNone(self._spawned[0].poll(),
                             'the hung child was left running instead of being killed')

    def test_a_probe_that_keeps_emitting_lines_outlives_a_short_stall_window(self):
        """Progress is the deadline: a scan that keeps producing lines faster than the
        stall window must run to completion rather than being killed mid-read."""
        cmd = _line_emitting_cmd(count=10, interval=0.2)
        with mock.patch.object(probe, '_nominal_fps', return_value=25.0), \
             mock.patch.object(probe.subprocess, 'Popen', side_effect=self._fake_popen(cmd)):
            metrics = probe.scan_video_timeline(self.stub_path, timeout=1)

        self.assertTrue(metrics, 'a steadily-progressing scan was killed as if it had stalled')
        self.assertEqual(metrics['packet_count'], 10)


if __name__ == '__main__':
    unittest.main()
