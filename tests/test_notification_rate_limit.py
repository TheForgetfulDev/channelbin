"""Per-service push rate limit override (dev/changelog/553).

notifications.services.<name>.rate_limit_seconds lets one service (e.g. Home
Assistant) flush immediately while another (e.g. Pushover) stays on the global
push_rate_limit_seconds window. Unset must mean "use the global value"; 0 must
be a distinct, valid "unlimited" choice - not collapsed into unset.

Covers effective_rate_limit() directly (the pure decision function) and
enqueue_push()'s timer arming, which had to move from one shared timer for
every service to one timer per service so two services with different
effective limits don't have to share a flush schedule.
"""
import time
import unittest
from unittest.mock import patch

from app import notifications
from tests.support import notifyguard


def _cfg(services, global_rate_limit=60):
    return {
        'notifications': {
            'push_rate_limit_seconds': global_rate_limit,
            'services': services,
        }
    }


class EffectiveRateLimitTests(unittest.TestCase):
    def test_unset_key_inherits_global(self):
        self.assertEqual(notifications.effective_rate_limit({}, 60), 60)

    def test_none_value_inherits_global(self):
        self.assertEqual(
            notifications.effective_rate_limit({'rate_limit_seconds': None}, 60), 60)

    def test_zero_means_unlimited_not_global(self):
        self.assertEqual(
            notifications.effective_rate_limit({'rate_limit_seconds': 0}, 60), 0)

    def test_positive_override_wins_over_global(self):
        self.assertEqual(
            notifications.effective_rate_limit({'rate_limit_seconds': 5}, 60), 5)

    def test_negative_override_clamps_to_zero(self):
        self.assertEqual(
            notifications.effective_rate_limit({'rate_limit_seconds': -3}, 60), 0)


class EnqueuePushTimerTests(unittest.TestCase):
    """enqueue_push must arm one timer per service, each at that service's own
    effective delay - not one shared timer for the whole batch."""

    def setUp(self):
        # Local imports inside notifications.py re-resolve load_config() every call,
        # so patching app.config.load_config (not app.notifications.load_config)
        # actually reaches enqueue_push - CLAUDE.md's testing-seam caveat.
        self.patcher = patch('app.config.load_config')
        self.mock_load_config = self.patcher.start()

    def tearDown(self):
        self.patcher.stop()
        with notifications._lock:
            timers = list(notifications._timers.values())
            notifications._timers.clear()
            notifications._pending.clear()
        for t in timers:
            t.cancel()

    def test_service_with_override_gets_its_own_delay(self):
        # Delays are long enough that the background Timer thread cannot fire (and
        # pop itself out of _timers) before this test reads .interval and tearDown
        # cancels it - a delay of 0 here would race the assertion against the flush.
        self.mock_load_config.return_value = _cfg({
            'home_assistant': {'enabled': True, 'url': 'hassio://x@y/', 'rate_limit_seconds': 10},
            'pushover': {'enabled': True, 'url': 'pover://x@y/'},
        }, global_rate_limit=60)

        notifications.enqueue_push(['home_assistant', 'pushover'], 'title', 'body')

        self.assertEqual(notifications._timers['home_assistant'].interval, 10)
        self.assertEqual(notifications._timers['pushover'].interval, 60)

    def test_zero_override_flushes_immediately_not_on_global_window(self):
        """0 means unlimited: the service's own queue flushes right away rather than
        waiting out the (much longer) global window."""
        notifyguard.sent.clear()
        self.mock_load_config.return_value = _cfg({
            'home_assistant': {'enabled': True, 'url': 'hassio://x@y/', 'rate_limit_seconds': 0},
        }, global_rate_limit=3600)

        notifications.enqueue_push(['home_assistant'], 'title', 'body')

        deadline = time.monotonic() + 2
        while time.monotonic() < deadline and not notifyguard.sent:
            time.sleep(0.02)

        self.assertEqual(len(notifyguard.sent), 1)
        self.assertEqual(notifyguard.sent[0][0], 'home_assistant')

    def test_two_services_with_different_overrides_stay_independent(self):
        self.mock_load_config.return_value = _cfg({
            'home_assistant': {'enabled': True, 'url': 'hassio://x@y/', 'rate_limit_seconds': 5},
            'pushover': {'enabled': True, 'url': 'pover://x@y/', 'rate_limit_seconds': 300},
        }, global_rate_limit=60)

        notifications.enqueue_push(['home_assistant', 'pushover'], 'title', 'body')

        self.assertEqual(notifications._timers['home_assistant'].interval, 5)
        self.assertEqual(notifications._timers['pushover'].interval, 300)

    def test_disabled_service_gets_no_timer(self):
        self.mock_load_config.return_value = _cfg({
            'pushover': {'enabled': False, 'url': 'pover://x@y/', 'rate_limit_seconds': 0},
        })

        notifications.enqueue_push(['pushover'], 'title', 'body')

        self.assertNotIn('pushover', notifications._timers)
        self.assertNotIn('pushover', notifications._pending)

    def test_second_enqueue_within_window_does_not_rearm(self):
        """A timer already armed for a service must not be replaced by a later
        enqueue_push call within the same window - only the pending queue grows."""
        self.mock_load_config.return_value = _cfg({
            'pushover': {'enabled': True, 'url': 'pover://x@y/'},
        }, global_rate_limit=60)

        notifications.enqueue_push(['pushover'], 'first', 'body')
        first_timer = notifications._timers['pushover']
        notifications.enqueue_push(['pushover'], 'second', 'body')

        self.assertIs(notifications._timers['pushover'], first_timer)
        self.assertEqual(len(notifications._pending['pushover']), 2)


if __name__ == '__main__':
    unittest.main()
