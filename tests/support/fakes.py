"""Process stand-ins shared by the conversion and join supervisor tests.

FakePopen stands in for a supervised ffmpeg. alive_ticks=None means it never exits on its
own; otherwise poll() answers None for that many calls, then the returncode. Signals are
recorded rather than swallowed, for two reasons the two former copies each gave: a fake
that cannot take a signal is not a faithful one, since terminate_or_kill() continues a
possibly-suspended child before every teardown (dev/changelog/952); and SIGSTOP/SIGCONT are
how a conversion yields without dying, so a fake that accepted SIGSTOP silently would let
a suspension that never happened look exactly like one that did.

One copy, here, where tests/test_conversion_monitor.py and tests/test_conversion_collision.py
each used to spell it (dev/changelog/1246).
"""
import errno
import os
import signal


def stale_stat(paths):
    """os.stat that answers ESTALE for `paths` and delegates everything else - how a
    storage test makes a directory under its own tmpdir behave like a dead network
    mount, which no clean checkout can provoke. Patching os.stat globally without the
    delegation takes the interpreter down with it."""
    real = os.stat
    paths = {str(p) for p in paths}

    def fake(path, *a, **kw):
        if str(path) in paths:
            raise OSError(errno.ESTALE, os.strerror(errno.ESTALE), str(path))
        return real(path, *a, **kw)
    return fake


class FakePopen:
    def __init__(self, *args, alive_ticks=None, returncode=0, **kwargs):
        self._polls = 0
        self._alive_ticks = alive_ticks
        self._rc = returncode
        self.returncode = None
        self.terminated = False
        self.signals = []

    def send_signal(self, sig):
        self.signals.append(sig)

    @property
    def suspended(self):
        stops = [s for s in self.signals if s in (signal.SIGSTOP, signal.SIGCONT)]
        return bool(stops) and stops[-1] == signal.SIGSTOP

    def poll(self):
        self._polls += 1
        if self._alive_ticks is not None and self._polls > self._alive_ticks:
            self.returncode = self._rc
            return self._rc
        return None

    def terminate(self):
        self.terminated = True
        if self.returncode is None:
            self.returncode = self._rc

    def kill(self):
        self.terminated = True
        if self.returncode is None:
            self.returncode = self._rc

    def wait(self, timeout=None):
        if self.returncode is None:
            self.returncode = self._rc
        return self.returncode
