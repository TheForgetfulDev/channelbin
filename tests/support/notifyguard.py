"""Stop the test suite from sending real push notifications.

This is not hypothetical, and it is not fixable with `make_test_app` overrides.
`app/alerts.py::create_alert` routes ERROR/CRIT alerts to `enqueue_push()`, which calls
`load_config()` at **run time** - and a runtime `load_config()` reads the real
`config.yaml`, never the test overrides (CLAUDE.md §Testing spells this caveat out). On
a real install's `config.yaml` has a push service enabled with a real URL and
LOG_ERROR/LOG_CRIT routed to it, so any test that logged an ERROR queued a genuine push.
`enqueue_push` then arms a `threading.Timer` for `push_rate_limit_seconds` (60), and the
flush thread called apprise ~60s later - typically long after the test that caused it had
passed, which is why it surfaced as a random DNS lookup to the configured push service in
roughly one run in four.

Two mechanisms, deliberately:
  * `_send_service` is replaced by a recorder, so a send is never attempted at all; and
  * `cancel_pending()` (called from TestApp.cleanup) cancels the armed timer and drops the
    queue, so nothing is left to fire after the suite moves on.

netguard is still the backstop underneath both - if a new push path appears that neither
covers, the DNS lookup is refused and reported rather than silently succeeding.
"""
import threading

# Every push the suite *would* have sent, as (service, messages). A test that wants to
# assert on push behavior can read this instead of hitting the network.
sent: list[tuple] = []

#: The real notifications._send_service, kept so a test that is asserting on the send path
#: itself can call it deliberately. Everything else keeps getting the recorder.
real_send_service = None

_installed = False
_lock = threading.Lock()


def install():
    """Idempotent, for the same reason netguard.install() is."""
    global _installed, real_send_service
    with _lock:
        if _installed:
            return
        _installed = True

    from app import notifications
    real_send_service = notifications._send_service

    def recording_send_service(svc, messages):
        sent.append((svc, list(messages)))
        # Same (ok, reason) contract the real _send_service returns - _flush branches on
        # it to raise or clear the standing send-failure alert.
        return True, ''

    notifications._send_service = recording_send_service


def cancel_pending():
    """Cancel every armed per-service flush timer and drop the pending queue.

    Called from TestApp.cleanup(). Without this a timer armed during a test keeps a
    non-daemon-ish wait alive and fires into the next test's window.
    """
    from app import notifications

    with notifications._lock:
        timers = list(notifications._timers.values())
        notifications._timers.clear()
        notifications._pending.clear()
    for timer in timers:
        timer.cancel()
