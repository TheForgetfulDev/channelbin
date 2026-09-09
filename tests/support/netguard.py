"""Process-wide guard: the test suite must never touch the network.

Installed from `tests/__init__.py`, so it is active for `./run_tests.sh`,
`python3 -m unittest discover -s tests`, and a single `python3 -m unittest
tests.test_foo` alike - all three invocations CLAUDE.md documents.

Why a spawn hook and not just a socket hook: the escape this was written for was
`ffmpeg -i http://example.test/live/9`, spawned by a WatchdogThread that a test leaked.
ffmpeg resolves and connects *in the child process*, so an in-process socket hook sees
nothing at all. Blocking the spawn is the only thing that catches it. The socket hook is
still here for in-process HTTP (account sync, EPG fetch) - nothing does that under test
today, but it is the obvious next foot-gun.

Both hooks raise rather than warn. A test that needs a stream, a playlist, or an EPG
document fakes it: a local file, a seeded row, `FileXtreamClient`, or a patched client.
Local-path child processes are unaffected - `test_screenshot_uniform` legitimately runs
ffmpeg against synthesized local jpgs and must keep working.
"""
import re
import socket
import threading
import subprocess
import traceback

# Schemes that mean "go out on the wire". A bare local path never matches.
_REMOTE_URL_RE = re.compile(r'\b(?:https?|rtmp|rtmps|rtsp|udp|tcp|tls|ftp|srt)://', re.I)

_LOOPBACK_HOSTS = {'localhost', 'localhost.localdomain', '127.0.0.1', '::1', ''}

_installed = False

# Every blocked attempt, as a message string. Raising is not enough on its own: the
# escape this was written for happened on an APScheduler worker thread inside
# start_recording(), whose broad error handling turns the exception into a FAILED
# recording - the network call is correctly blocked, but the test still reports OK and
# nobody ever learns. TestApp.cleanup() drains this list and fails the test, so a
# violation on any thread is loud instead of silent.
violations: list[str] = []


class NetworkAccessInTestError(AssertionError):
    """Raised when test code tries to reach the network. Not catchable as an
    ordinary failure by accident - it subclasses AssertionError so unittest reports
    it as a failing test rather than swallowing it somewhere as a generic Exception."""

    def __init__(self, message):
        # Record the thread, because attribution is inherently fuzzy: an attempt made on a
        # background thread is drained by whichever TestApp.cleanup() runs next, which may
        # belong to a later test than the one that caused it. If the thread here is not
        # MainThread, suspect an earlier test that left work running.
        violations.append(f'[thread={threading.current_thread().name}] {message}')
        super().__init__(message)


def drain_violations() -> list:
    """Return and clear the recorded violations (called by TestApp.cleanup())."""
    found = list(violations)
    violations.clear()
    return found


def _test_frames() -> str:
    """The tests/ and app/ frames of the current stack, for a message that names the
    culprit instead of dumping the whole interpreter stack."""
    frames = [ln.strip() for ln in traceback.format_stack()
              if '/channelbin/tests/' in ln or '/channelbin/app/' in ln]
    return '\n    '.join(frames[-4:]) or '<no project frames>'


def _is_loopback(host) -> bool:
    if not isinstance(host, str):
        return False
    return host in _LOOPBACK_HOSTS or host.startswith('127.')


def install():
    """Idempotent - `tests/__init__.py` may be imported more than once per process
    (discovery + an explicit module import), and double-wrapping would stack the
    hooks and mangle the error messages."""
    global _installed
    if _installed:
        return
    _installed = True

    real_popen_init = subprocess.Popen.__init__
    real_connect = socket.socket.connect
    real_getaddrinfo = socket.getaddrinfo

    def guarded_popen_init(self, args, *a, **kw):
        argv = args if isinstance(args, (list, tuple)) else [str(args)]
        for arg in argv:
            if isinstance(arg, (str, bytes)):
                text = arg.decode('utf-8', 'replace') if isinstance(arg, bytes) else arg
                if _REMOTE_URL_RE.search(text):
                    raise NetworkAccessInTestError(
                        f'Test spawned a child process pointed at a network URL: {text!r}\n'
                        f'  (argv[0]={argv[0]!r})\n'
                        f'The test suite must never reach the network. Use a local file, a\n'
                        f'seeded DB row, or a patched client instead - see CLAUDE.md §Testing.\n'
                        f'  {_test_frames()}')
        return real_popen_init(self, args, *a, **kw)

    def guarded_connect(self, address):
        # AF_UNIX addresses are plain path strings; only AF_INET/AF_INET6 carry a tuple.
        if isinstance(address, tuple) and address and not _is_loopback(address[0]):
            raise NetworkAccessInTestError(
                f'Test opened a socket to {address!r}. The test suite must never reach the\n'
                f'network - fake the response instead (CLAUDE.md §Testing).\n'
                f'  {_test_frames()}')
        return real_connect(self, address)

    def guarded_getaddrinfo(host, port, *a, **kw):
        if not _is_loopback(host):
            raise NetworkAccessInTestError(
                f'Test resolved hostname {host!r}. The test suite must never reach the\n'
                f'network - fake the response instead (CLAUDE.md §Testing).\n'
                f'  {_test_frames()}')
        return real_getaddrinfo(host, port, *a, **kw)

    subprocess.Popen.__init__ = guarded_popen_init
    socket.socket.connect = guarded_connect
    socket.getaddrinfo = guarded_getaddrinfo
