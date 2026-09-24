"""A push service that accepts its configuration but fails to deliver (dev/changelog/721).

The placeholder-URL case already had a standing alert. The strictly more likely case - a
revoked webhook, an expired token, a typo'd host - had none: `_send_service` logged one
`Apprise notify returned False` at WARNING, `_flush` had already popped the messages off
the queue, and the log->alert handler only promotes ERROR and above. Every push was
dropped forever with nothing on any surface the user reads.

Three things under test here:
  * `_send_service` reports an outcome instead of returning None, covering all four ways
    a send ends (apprise missing, no URL at flush time, the service rejecting it, success).
  * `_flush` turns that outcome into a standing per-service alert, deduped while it stands
    and dismissed on the next successful send. It runs on a threading.Timer thread, so the
    Flask app it records against has to be the one enqueue_push captured.
  * Nothing on the path assumes the service key is one of the five the settings UI offers
    a card for - Apprise accepts any scheme it supports and config.yaml can name one
    directly, so a future service must alert under its own key.

The alert body is built only from this module's own fixed phrasings. Apprise URLs carry
credentials in schemes (`pover://`, `discord://`) that app/url_utils.py's maskers do not
match, and the body is rendered in the UI and can be pushed off-box.
"""
import re
import sys
import threading
import unittest
from unittest.mock import patch

from app import db, notifications
from app.alerts import NOTIFICATION_SERVICE_SEND_FAILED as SEND_FAILED
from app.database import Alert
from tests.support import notifyguard
from tests.support.app import make_test_app

REAL_URL = 'pover://SECRETUSERKEY@SECRETAPPTOKEN/'


def _cfg(services, global_rate_limit=60):
    return {'notifications': {'push_rate_limit_seconds': global_rate_limit,
                              'services': services}}


class _FakeApprise:
    """Stand-in for the apprise module. Never touches the network, so netguard stays
    satisfied while the real _send_service runs end to end."""

    def __init__(self, notify_result=True, raises=None):
        self._notify_result = notify_result
        self._raises = raises
        self.added = []

    # `import apprise` then `apprise.Apprise()` - the module and the class are the same
    # object here, which is enough for the one call shape _send_service uses.
    def Apprise(self):
        return self

    def add(self, url):
        self.added.append(url)

    def notify(self, title=None, body=None):
        if self._raises is not None:
            raise self._raises
        return self._notify_result


def _with_apprise(fake):
    return patch.dict(sys.modules, {'apprise': fake})


class SendServiceOutcomeTests(unittest.TestCase):
    """_send_service must report (ok, reason) for every way a send can end, so _flush has
    something to route. It used to return None on three of the four."""

    def _send(self, cfg, fake):
        # notifyguard swaps the module-global out for a recorder suite-wide; the real
        # function is what this class is about.
        with patch('app.config.load_config', return_value=cfg):
            if fake is None:
                return notifyguard.real_send_service('pushover', [{'title': 't', 'body': 'b'}])
            with _with_apprise(fake):
                return notifyguard.real_send_service('pushover', [{'title': 't', 'body': 'b'}])

    def test_success_reports_ok(self):
        ok, reason = self._send(_cfg({'pushover': {'enabled': True, 'url': REAL_URL}}),
                                _FakeApprise(notify_result=True))
        self.assertTrue(ok)
        self.assertEqual(reason, '')

    def test_rejected_send_reports_a_reason(self):
        ok, reason = self._send(_cfg({'pushover': {'enabled': True, 'url': REAL_URL}}),
                                _FakeApprise(notify_result=False))
        self.assertFalse(ok)
        self.assertTrue(reason, 'a failed send must name a reason, not an empty string')

    def test_missing_apprise_package_reports_a_reason(self):
        # ImportError out of `import apprise`, which is how a non-installed package fails.
        real_import = __builtins__['__import__'] if isinstance(__builtins__, dict) \
            else __builtins__.__import__

        def _no_apprise(name, *args, **kwargs):
            if name == 'apprise':
                raise ImportError('No module named apprise')
            return real_import(name, *args, **kwargs)

        with patch('builtins.__import__', side_effect=_no_apprise):
            ok, reason = self._send(_cfg({'pushover': {'enabled': True, 'url': REAL_URL}}),
                                    None)
        self.assertFalse(ok)
        self.assertIn('apprise', reason)

    def test_url_cleared_between_enqueue_and_flush_reports_a_reason(self):
        ok, reason = self._send(_cfg({'pushover': {'enabled': True, 'url': ''}}),
                                _FakeApprise())
        self.assertFalse(ok)
        self.assertIn('URL', reason)


class FlushRaisesTheStandingAlertTests(unittest.TestCase):
    """_flush must turn a failed send into one standing alert for that service."""

    def setUp(self):
        self.t = make_test_app()

    def tearDown(self):
        self.t.cleanup()

    def _flush_with(self, result, svc='pushover', messages=2):
        queued = [{'title': f't{i}', 'body': 'b'} for i in range(messages)]
        with notifications._lock:
            notifications._pending[svc] = queued
        with patch.object(notifications, '_send_service', return_value=result):
            notifications._flush(svc, self.t.app)

    def _open(self, svc='pushover'):
        return Alert.query.filter_by(
            alert_type=SEND_FAILED, source=svc, dismissed_at=None).all()

    def test_a_failed_flush_raises_one_alert(self):
        self._flush_with((False, 'the service rejected the notification'))
        alerts = self._open()
        self.assertEqual(len(alerts), 1)
        self.assertIn('Pushover', alerts[0].title)

    def test_the_alert_names_how_many_messages_were_dropped(self):
        self._flush_with((False, 'the service rejected the notification'), messages=3)
        body = self._open()[0].body
        self.assertEqual(re.findall(r'and (\d+) queued notification\(s\) were dropped', body),
                         ['3'])

    def test_repeated_failures_do_not_stack_alerts(self):
        for _ in range(4):
            self._flush_with((False, 'the service rejected the notification'))
        self.assertEqual(
            Alert.query.filter_by(alert_type=SEND_FAILED, source='pushover').count(), 1)

    def test_a_successful_flush_dismisses_the_standing_alert(self):
        self._flush_with((False, 'the service rejected the notification'))
        self.assertEqual(len(self._open()), 1)
        self._flush_with((True, ''))
        db.session.expire_all()
        self.assertEqual(self._open(), [])

    def test_a_successful_flush_raises_nothing(self):
        self._flush_with((True, ''))
        self.assertEqual(Alert.query.filter_by(alert_type=SEND_FAILED).count(), 0)

    def test_each_service_gets_its_own_alert(self):
        self._flush_with((False, 'rejected'), svc='pushover')
        self._flush_with((False, 'rejected'), svc='discord')
        self.assertEqual(len(self._open('pushover')), 1)
        self.assertEqual(len(self._open('discord')), 1)

    def test_dismissing_one_service_leaves_the_other_standing(self):
        self._flush_with((False, 'rejected'), svc='pushover')
        self._flush_with((False, 'rejected'), svc='discord')
        self._flush_with((True, ''), svc='pushover')
        db.session.expire_all()
        self.assertEqual(self._open('pushover'), [])
        self.assertEqual(len(self._open('discord')), 1)

    def test_an_exception_from_the_send_still_raises_the_alert(self):
        with notifications._lock:
            notifications._pending['pushover'] = [{'title': 't', 'body': 'b'}]
        with patch.object(notifications, '_send_service',
                          side_effect=RuntimeError(f'boom while posting to {REAL_URL}')):
            notifications._flush('pushover', self.t.app)
        alerts = self._open()
        self.assertEqual(len(alerts), 1)
        self.assertIn('RuntimeError', alerts[0].body)

    def test_the_alert_body_never_carries_the_service_url(self):
        """url_utils' maskers are anchored to http(s), so a pover:// URL quoted by a
        third-party exception would survive them - the body must be built from our own
        phrasings only."""
        with notifications._lock:
            notifications._pending['pushover'] = [{'title': 't', 'body': 'b'}]
        with patch.object(notifications, '_send_service',
                          side_effect=RuntimeError(f'boom while posting to {REAL_URL}')):
            notifications._flush('pushover', self.t.app)
        body = self._open()[0].body
        self.assertNotIn('SECRETUSERKEY', body)
        self.assertNotIn('SECRETAPPTOKEN', body)
        self.assertNotIn('pover://', body)

    def test_an_empty_queue_flushes_without_raising_anything(self):
        notifications._flush('pushover', self.t.app)
        self.assertEqual(Alert.query.filter_by(alert_type=SEND_FAILED).count(), 0)


class FutureApprseServiceTests(unittest.TestCase):
    """Nothing on this path may assume the service key is one of the five the settings UI
    offers a card for. Apprise supports many more, config.yaml can name one directly, and
    a service ChannelBin has no label for still has to be nameable when it breaks."""

    def setUp(self):
        self.t = make_test_app()

    def tearDown(self):
        self.t.cleanup()

    def _flush_unknown(self, result, svc='ntfy'):
        with notifications._lock:
            notifications._pending[svc] = [{'title': 't', 'body': 'b'}]
        with patch.object(notifications, '_send_service', return_value=result):
            notifications._flush(svc, self.t.app)

    def test_an_unlabelled_service_still_raises_its_alert(self):
        self._flush_unknown((False, 'rejected'))
        alerts = Alert.query.filter_by(alert_type=SEND_FAILED, source='ntfy').all()
        self.assertEqual(len(alerts), 1)

    def test_an_unlabelled_service_is_named_by_its_own_key(self):
        self._flush_unknown((False, 'rejected'), svc='matrix')
        alert = Alert.query.filter_by(alert_type=SEND_FAILED, source='matrix').one()
        self.assertIn('matrix', alert.title)

    def test_an_unlabelled_service_dismisses_the_same_way(self):
        self._flush_unknown((False, 'rejected'))
        # Assert it stood first - otherwise "no open alert" passes vacuously against a
        # build that never raised one.
        self.assertEqual(
            Alert.query.filter_by(
                alert_type=SEND_FAILED, source='ntfy', dismissed_at=None).count(), 1)
        self._flush_unknown((True, ''))
        db.session.expire_all()
        self.assertEqual(
            Alert.query.filter_by(
                alert_type=SEND_FAILED, source='ntfy', dismissed_at=None).all(), [])

    def test_service_label_falls_back_to_the_key(self):
        self.assertEqual(notifications.service_label('pushover'), 'Pushover')
        self.assertEqual(notifications.service_label('ntfy'), 'ntfy')

    def test_an_unlabelled_service_is_never_read_as_a_placeholder_url(self):
        """SERVICE_URL_HINTS.get() returns None for an unknown key; a real URL must not
        compare equal to it and be dropped as 'still the placeholder'."""
        with patch('app.config.load_config', return_value=_cfg({
                'ntfy': {'enabled': True, 'url': 'ntfy://topic@host/'}})):
            notifications.enqueue_push(['ntfy'], 'title', 'body')
        try:
            self.assertIn('ntfy', notifications._pending)
            self.assertEqual(
                Alert.query.filter_by(
                    alert_type='NOTIFICATION_SERVICE_URL_PLACEHOLDER').count(), 0)
        finally:
            with notifications._lock:
                timers = list(notifications._timers.values())
                notifications._timers.clear()
                notifications._pending.clear()
            for timer in timers:
                timer.cancel()


class FlushWithoutAnAppContextTests(unittest.TestCase):
    """enqueue_push is reachable from a bare thread with no app context. The flush must
    degrade to logging rather than raising out of a Timer thread nobody is watching."""

    def test_a_failed_flush_with_no_app_does_not_raise(self):
        with notifications._lock:
            notifications._pending['pushover'] = [{'title': 't', 'body': 'b'}]
        with patch.object(notifications, '_send_service', return_value=(False, 'rejected')):
            notifications._flush('pushover', None)   # must not raise

    def test_a_successful_flush_with_no_app_does_not_raise(self):
        with notifications._lock:
            notifications._pending['pushover'] = [{'title': 't', 'body': 'b'}]
        with patch.object(notifications, '_send_service', return_value=(True, '')):
            notifications._flush('pushover', None)   # must not raise


class EnqueuePushCarriesTheAppToTheFlushTests(unittest.TestCase):
    """The captured app is the only route to a DB write from the Timer thread, so it has
    to actually reach _flush - a flush that gets None records nothing."""

    def setUp(self):
        self.t = make_test_app()

    def tearDown(self):
        with notifications._lock:
            timers = list(notifications._timers.values())
            notifications._timers.clear()
            notifications._pending.clear()
        for timer in timers:
            timer.cancel()
        self.t.cleanup()

    def test_the_armed_timer_is_given_the_current_app(self):
        with patch('app.config.load_config', return_value=_cfg({
                'pushover': {'enabled': True, 'url': REAL_URL}}, global_rate_limit=600)):
            notifications.enqueue_push(['pushover'], 'title', 'body')
        self.assertIs(notifications._timers['pushover'].args[1], self.t.app)

    def test_a_real_flush_records_the_failure_it_hit(self):
        """End to end through the timer: enqueue, let it fire, and find the alert."""
        done = threading.Event()

        def _failing(svc, messages):
            try:
                return False, 'the service rejected the notification'
            finally:
                done.set()

        with patch.object(notifications, '_send_service', side_effect=_failing), \
             patch('app.config.load_config', return_value=_cfg(
                 {'pushover': {'enabled': True, 'url': REAL_URL, 'rate_limit_seconds': 0}})):
            notifications.enqueue_push(['pushover'], 'title', 'body')
            self.assertTrue(done.wait(5), 'the flush timer never fired')

        deadline = threading.Event()
        for _ in range(100):
            db.session.expire_all()
            if Alert.query.filter_by(alert_type=SEND_FAILED, source='pushover').count():
                break
            deadline.wait(0.02)
        self.assertEqual(
            Alert.query.filter_by(alert_type=SEND_FAILED, source='pushover').count(), 1)


class SendTestDismissesTheAlertTests(unittest.TestCase):
    """The settings Test button is the most direct way a user confirms a fix, so a
    successful test is a successful send and clears the standing alert."""

    def setUp(self):
        self.t = make_test_app()

    def tearDown(self):
        self.t.cleanup()

    def _standing(self):
        row = Alert(alert_type=SEND_FAILED, severity='WARN', title='x', source='pushover')
        db.session.add(row)
        db.session.commit()
        return row

    def test_a_successful_test_dismisses_it(self):
        row = self._standing()
        with patch('app.config.load_config', return_value=_cfg({
                'pushover': {'enabled': True, 'url': REAL_URL}})), \
             _with_apprise(_FakeApprise(notify_result=True)):
            ok, _ = notifications.send_test('pushover')
        self.assertTrue(ok)
        db.session.expire_all()
        self.assertIsNotNone(db.session.get(Alert, row.id).dismissed_at)

    def test_a_failed_test_leaves_it_standing(self):
        row = self._standing()
        with patch('app.config.load_config', return_value=_cfg({
                'pushover': {'enabled': True, 'url': REAL_URL}})), \
             _with_apprise(_FakeApprise(notify_result=False)):
            ok, _ = notifications.send_test('pushover')
        self.assertFalse(ok)
        db.session.expire_all()
        self.assertIsNone(db.session.get(Alert, row.id).dismissed_at)


class SettingsRoutesDismissTheAlertTests(unittest.TestCase):
    """A service that was cleared or disabled produces no further successful send, so
    without this its alert would stand accused forever."""

    def setUp(self):
        self.t = make_test_app()
        self.t.app.config['WTF_CSRF_ENABLED'] = False
        self.client = self.t.app.test_client()
        self.saved = {}

    def tearDown(self):
        self.t.cleanup()

    def _standing(self):
        row = Alert(alert_type=SEND_FAILED, severity='WARN', title='x', source='pushover')
        db.session.add(row)
        db.session.commit()
        return row

    def _patched(self):
        return patch('app.routes.settings.load_config',
                     return_value=_cfg({'pushover': {'enabled': True, 'url': REAL_URL}})), \
            patch('app.routes.settings.save_config',
                  side_effect=lambda c: self.saved.update(c) or [])

    def test_saving_the_service_dismisses_it(self):
        row = self._standing()
        load_patch, save_patch = self._patched()
        with load_patch, save_patch:
            resp = self.client.post('/api/notifications/services/pushover',
                                    json={'enabled': True, 'url': 'pover://new@token/'})
        self.assertEqual(resp.status_code, 200)
        db.session.expire_all()
        self.assertIsNotNone(db.session.get(Alert, row.id).dismissed_at)

    def test_removing_the_service_dismisses_it(self):
        row = self._standing()
        load_patch, save_patch = self._patched()
        with load_patch, save_patch:
            resp = self.client.delete('/api/notifications/services/pushover')
        self.assertEqual(resp.status_code, 200)
        db.session.expire_all()
        self.assertIsNotNone(db.session.get(Alert, row.id).dismissed_at)


class RecursionTerminatesTests(unittest.TestCase):
    """Routing the failure alert back to the service that is failing must terminate: the
    alert it raises enqueues one doomed push, whose flush finds the alert already open and
    creates nothing further. Same property the placeholder alert relies on."""

    def setUp(self):
        self.t = make_test_app()

    def tearDown(self):
        with notifications._lock:
            timers = list(notifications._timers.values())
            notifications._timers.clear()
            notifications._pending.clear()
        for timer in timers:
            timer.cancel()
        self.t.cleanup()

    def test_routing_the_alert_to_the_failing_service_settles_at_one_alert(self):
        cfg = _cfg({'pushover': {'enabled': True, 'url': REAL_URL}})
        cfg['notifications']['routing'] = {
            SEND_FAILED: {'in_app': True, 'push_services': ['pushover']}}

        with patch('app.config.load_config', return_value=cfg):
            for _ in range(3):
                with notifications._lock:
                    notifications._pending['pushover'] = [{'title': 't', 'body': 'b'}]
                with patch.object(notifications, '_send_service',
                                  return_value=(False, 'rejected')):
                    notifications._flush('pushover', self.t.app)

        self.assertEqual(
            Alert.query.filter_by(alert_type=SEND_FAILED, source='pushover').count(), 1)


if __name__ == '__main__':
    unittest.main()
