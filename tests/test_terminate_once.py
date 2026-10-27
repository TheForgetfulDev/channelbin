"""A child gets one SIGTERM from this app, however many callers reach terminate_or_kill().

Guards dev/docs/BUGS.md 2026-10-26 (a scheduled stop lost the last 3 to 4 seconds of the
recording): the stop job SIGTERMed ffmpeg and the watchdog, woken by the same stop event,
SIGTERMed it again a few milliseconds later. ffmpeg 7.1 treats a second signal as an
immediate exit - no trailer, and whatever the muxer still held is dropped.
"""
import signal
import subprocess
import sys
import threading
import time
import unittest

from app.proc_utils import terminate_or_kill


class _StubbornChild:
    """Alive until killed: terminate() is noted and ignored, the way ffmpeg mid-trailer
    looks to a caller that polls it a moment after the first SIGTERM."""

    def __init__(self):
        self.returncode = None
        self.calls = []

    def send_signal(self, sig):
        self.calls.append(signal.Signals(sig).name)

    def poll(self):
        return self.returncode

    def terminate(self):
        self.calls.append('terminate')

    def kill(self):
        self.calls.append('kill')
        self.returncode = -9

    def wait(self, timeout=None):
        if self.returncode is None:
            raise subprocess.TimeoutExpired('child', timeout)
        return self.returncode


class SecondCallerTests(unittest.TestCase):
    def test_a_second_graceful_caller_waits_and_escalates_without_a_second_sigterm(self):
        child = _StubbornChild()
        terminate_or_kill(child, timeout=0.01)
        terminate_or_kill(child, timeout=0.01)
        self.assertEqual(child.calls.count('terminate'), 1, child.calls)
        self.assertIn('kill', child.calls)

    def test_a_hard_caller_still_kills_at_once(self):
        child = _StubbornChild()
        terminate_or_kill(child, timeout=0.01, hard=True)
        self.assertEqual(child.calls, ['SIGCONT', 'kill'])


# Counts its SIGTERMs; the first starts a 0.6 s "trailer", and the exit code is the count.
_CHILD = r'''
import signal, sys, time
n = 0
def on_term(sig, frame):
    global n
    n += 1
signal.signal(signal.SIGTERM, on_term)
sys.stdout.write('ready\n'); sys.stdout.flush()
while n == 0:
    time.sleep(0.01)
end = time.monotonic() + 0.6
while time.monotonic() < end:
    time.sleep(0.01)
sys.exit(n)
'''


class RealChildTests(unittest.TestCase):
    def test_two_callers_a_few_milliseconds_apart_send_one_sigterm(self):
        proc = subprocess.Popen([sys.executable, '-c', _CHILD], stdout=subprocess.PIPE,
                                stdin=subprocess.DEVNULL, text=True)
        self.addCleanup(lambda: proc.poll() is None and proc.kill())
        self.assertEqual(proc.stdout.readline(), 'ready\n')
        first = threading.Thread(target=terminate_or_kill, args=(proc,))
        first.start()
        time.sleep(0.05)
        terminate_or_kill(proc)
        first.join(10)
        self.assertEqual(proc.returncode, 1, 'the child counted a different number of SIGTERMs')


if __name__ == '__main__':
    unittest.main()
