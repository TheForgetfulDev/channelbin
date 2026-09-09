"""A notification service URL saved as its own placeholder hint (dev/docs/BUGS.md
2026-08-11 09:00 PM, dev/changelog/614).

`pover://UserKey@AppToken/` (etc.) is example text shown in the URL field's placeholder,
never a real credential - typing it into the box to see what it looks like used to
silently replace a working credential, because the field auto-saves and then renders
masked, so there was no way to tell afterward the stored value was junk.

Three layers:
  * app/routes/settings.py::api_notifications_service_save rejects a URL exactly equal
    to its own SERVICE_URL_HINTS entry, server-side, with a 400 - and dismisses any open
    placeholder alert once the service is saved with a real value.
  * app/notifications.py::enqueue_push, hitting a still-broken URL (e.g. one that
    predates this fix), raises a standing NOTIFICATION_SERVICE_URL_PLACEHOLDER alert
    instead of silently dropping the push - deduped so repeated drops do not spam the
    Alert Center.
  * app/routes/settings.py::notifications_settings's boot payload carries a `broken` flag
    per service so the page itself says so, not only the Alert Center.
"""
import unittest
from unittest.mock import patch

from app import db
from app import notifications
from app.database import Alert
from tests.support.app import make_test_app


def _cfg(services):
    return {'notifications': {'push_rate_limit_seconds': 60, 'services': services}}


class EnqueuePushPlaceholderUrlTests(unittest.TestCase):
    """enqueue_push() must not enqueue a push to a service whose stored URL is still its
    own placeholder hint, and must raise (once, not repeatedly) the standing alert."""

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

    def test_placeholder_url_is_not_enqueued(self):
        with patch('app.config.load_config', return_value=_cfg({
            'pushover': {'enabled': True, 'url': notifications.SERVICE_URL_HINTS['pushover']},
        })):
            notifications.enqueue_push(['pushover'], 'title', 'body')
        self.assertNotIn('pushover', notifications._pending)
        self.assertNotIn('pushover', notifications._timers)

    def test_placeholder_url_raises_one_standing_alert(self):
        with patch('app.config.load_config', return_value=_cfg({
            'pushover': {'enabled': True, 'url': notifications.SERVICE_URL_HINTS['pushover']},
        })):
            notifications.enqueue_push(['pushover'], 'title', 'body')
        alerts = Alert.query.filter_by(
            alert_type='NOTIFICATION_SERVICE_URL_PLACEHOLDER', source='pushover').all()
        self.assertEqual(len(alerts), 1)
        self.assertIsNone(alerts[0].dismissed_at)

    def test_repeated_drops_do_not_duplicate_the_alert(self):
        with patch('app.config.load_config', return_value=_cfg({
            'pushover': {'enabled': True, 'url': notifications.SERVICE_URL_HINTS['pushover']},
        })):
            notifications.enqueue_push(['pushover'], 'first', 'body')
            notifications.enqueue_push(['pushover'], 'second', 'body')
            notifications.enqueue_push(['pushover'], 'third', 'body')
        self.assertEqual(
            Alert.query.filter_by(
                alert_type='NOTIFICATION_SERVICE_URL_PLACEHOLDER', source='pushover').count(),
            1)

    def test_a_real_url_is_enqueued_normally_and_raises_nothing(self):
        with patch('app.config.load_config', return_value=_cfg({
            'pushover': {'enabled': True, 'url': 'pover://realkey@realtoken/'},
        })):
            notifications.enqueue_push(['pushover'], 'title', 'body')
        self.assertIn('pushover', notifications._pending)
        self.assertEqual(
            Alert.query.filter_by(alert_type='NOTIFICATION_SERVICE_URL_PLACEHOLDER').count(), 0)


class DismissPlaceholderUrlAlertTests(unittest.TestCase):
    def setUp(self):
        self.t = make_test_app()

    def tearDown(self):
        self.t.cleanup()

    def test_dismisses_an_open_alert(self):
        a = Alert(alert_type='NOTIFICATION_SERVICE_URL_PLACEHOLDER', severity='WARN',
                   title='x', source='pushover')
        db.session.add(a)
        db.session.commit()
        notifications.dismiss_placeholder_url_alert('pushover')
        db.session.expire_all()
        self.assertIsNotNone(db.session.get(Alert, a.id).dismissed_at)

    def test_no_open_alert_is_a_no_op(self):
        notifications.dismiss_placeholder_url_alert('pushover')  # must not raise

    def test_only_the_named_service_is_dismissed(self):
        a1 = Alert(alert_type='NOTIFICATION_SERVICE_URL_PLACEHOLDER', severity='WARN',
                    title='x', source='pushover')
        a2 = Alert(alert_type='NOTIFICATION_SERVICE_URL_PLACEHOLDER', severity='WARN',
                    title='y', source='discord')
        db.session.add_all([a1, a2])
        db.session.commit()
        notifications.dismiss_placeholder_url_alert('pushover')
        db.session.expire_all()
        self.assertIsNotNone(db.session.get(Alert, a1.id).dismissed_at)
        self.assertIsNone(db.session.get(Alert, a2.id).dismissed_at)


class ServiceSaveRejectsPlaceholderTests(unittest.TestCase):
    """POST /api/notifications/services/<name> - the server-side reject."""

    def setUp(self):
        self.t = make_test_app()
        self.t.app.config['WTF_CSRF_ENABLED'] = False
        self.client = self.t.app.test_client()
        self.saved = {}

    def tearDown(self):
        self.t.cleanup()

    def _save(self, name, cfg, body):
        with patch('app.routes.settings.load_config', return_value=cfg), \
             patch('app.routes.settings.save_config',
                   side_effect=lambda c: self.saved.update(c) or []):
            return self.client.post(f'/api/notifications/services/{name}', json=body)

    def test_saving_the_exact_placeholder_is_rejected(self):
        cfg = _cfg({})
        resp = self._save('pushover', cfg, {
            'enabled': True, 'url': notifications.SERVICE_URL_HINTS['pushover']})
        self.assertEqual(resp.status_code, 400)
        self.assertIn('error', resp.get_json())
        self.assertEqual(self.saved, {}, 'a rejected save still wrote config.yaml')

    def test_a_real_url_still_saves(self):
        cfg = _cfg({})
        resp = self._save('pushover', cfg, {'enabled': True, 'url': 'pover://realkey@realtoken/'})
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(
            self.saved['notifications']['services']['pushover']['url'],
            'pover://realkey@realtoken/')

    def test_fixing_a_broken_url_dismisses_the_standing_alert(self):
        a = Alert(alert_type='NOTIFICATION_SERVICE_URL_PLACEHOLDER', severity='WARN',
                   title='x', source='pushover')
        db.session.add(a)
        db.session.commit()
        cfg = _cfg({'pushover': {
            'enabled': True, 'url': notifications.SERVICE_URL_HINTS['pushover']}})
        resp = self._save('pushover', cfg, {'enabled': True, 'url': 'pover://realkey@realtoken/'})
        self.assertEqual(resp.status_code, 200)
        db.session.expire_all()
        self.assertIsNotNone(db.session.get(Alert, a.id).dismissed_at)

    def test_removal_dismisses_the_standing_alert(self):
        a = Alert(alert_type='NOTIFICATION_SERVICE_URL_PLACEHOLDER', severity='WARN',
                   title='x', source='pushover')
        db.session.add(a)
        db.session.commit()
        cfg = _cfg({'pushover': {
            'enabled': True, 'url': notifications.SERVICE_URL_HINTS['pushover']}})
        with patch('app.routes.settings.load_config', return_value=cfg), \
             patch('app.routes.settings.save_config',
                   side_effect=lambda c: self.saved.update(c) or []):
            resp = self.client.delete('/api/notifications/services/pushover')
        self.assertEqual(resp.status_code, 200)
        db.session.expire_all()
        self.assertIsNotNone(db.session.get(Alert, a.id).dismissed_at)


class BootPayloadBrokenFlagTests(unittest.TestCase):
    """GET /settings/notifications - the boot payload's per-service `broken` flag."""

    def setUp(self):
        self.t = make_test_app()
        self.client = self.t.app.test_client()

    def tearDown(self):
        self.t.cleanup()

    def _boot(self, cfg):
        import json
        import re
        with patch('app.routes.settings.load_config', return_value=cfg):
            html = self.client.get('/settings/notifications').get_data(as_text=True)
        blob = re.search(r'<script type="application/json" id="notif-boot">(.*?)</script>',
                         html, re.S)
        return json.loads(blob.group(1))

    def test_enabled_placeholder_url_is_flagged_broken(self):
        boot = self._boot(_cfg({'pushover': {
            'enabled': True, 'url': notifications.SERVICE_URL_HINTS['pushover']}}))
        self.assertTrue(boot['services']['pushover']['broken'])

    def test_disabled_placeholder_url_is_not_flagged_broken(self):
        """The condition is specifically 'enabled with the placeholder' - a disabled
        service holding the same leftover text is not currently doing anything wrong."""
        boot = self._boot(_cfg({'pushover': {
            'enabled': False, 'url': notifications.SERVICE_URL_HINTS['pushover']}}))
        self.assertFalse(boot['services']['pushover']['broken'])

    def test_real_url_is_not_flagged_broken(self):
        boot = self._boot(_cfg({'pushover': {
            'enabled': True, 'url': 'pover://realkey@realtoken/'}}))
        self.assertFalse(boot['services']['pushover']['broken'])

    def test_broken_flag_does_not_leak_the_masked_url_comparison(self):
        """The boot payload's url field is masked (config secrets rule); `broken` must
        still be computed from the raw stored value, not the sentinel."""
        boot = self._boot(_cfg({'pushover': {
            'enabled': True, 'url': notifications.SERVICE_URL_HINTS['pushover']}}))
        self.assertNotEqual(boot['services']['pushover']['url'],
                            notifications.SERVICE_URL_HINTS['pushover'])
        self.assertTrue(boot['services']['pushover']['broken'])


if __name__ == '__main__':
    unittest.main()
