"""Home Assistant integration: GET /api/ha/v1/status
(app/routes/ha.py, plus the app/routes/alerts.py::_unread_alert_summary created_at
extension it reuses).

Covers: the route stays behind the X-API-Key gate (app/routes/ha.py::_require_api_key,
401 missing/wrong key), a 200 with the correct key, and that the combined
recording/disk/alerts/accounts payload matches
the documented contract - counts, the next scheduled recording's id/name/channel/start_time,
disk byte fields, the latest unread alert's severity/title/created_at, and account
total/ok_count/error_count.

Runs against a throwaway temp SQLite DB and a sandboxed config.yaml
(tests/support/config_sandbox.py::ConfigSandbox) - never the live dvr.db/config.yaml.
    python3 -m unittest tests.test_ha_api
"""
import shutil
import tempfile
import unittest
from datetime import datetime, timedelta

from werkzeug.security import generate_password_hash

from app import db
from app.database import Alert
from tests.support.app import make_test_app
from tests.support.config_sandbox import ConfigSandbox
from tests.support.seed import make_account, make_channel, make_recording

API_KEY = 'a-real-generated-ha-key'


_ConfigSandbox = ConfigSandbox


class StatusEndpointGateTests(_ConfigSandbox):
    def setUp(self):
        super().setUp()
        self._write_cfg({'integrations': {'home_assistant': {
            'enabled': True, 'api_key_hash': generate_password_hash(API_KEY)}}})
        self.t = make_test_app()
        self.client = self.t.app.test_client()
        self.addCleanup(self.t.cleanup)

    def test_missing_key_is_401(self):
        r = self.client.get('/api/ha/v1/status')
        self.assertEqual(r.status_code, 401)

    def test_wrong_key_is_401(self):
        r = self.client.get('/api/ha/v1/status', headers={'X-API-Key': 'nope'})
        self.assertEqual(r.status_code, 401)

    def test_correct_key_is_200(self):
        r = self.client.get('/api/ha/v1/status', headers={'X-API-Key': API_KEY})
        self.assertEqual(r.status_code, 200)


class StatusEndpointShapeTests(_ConfigSandbox):
    def setUp(self):
        super().setUp()
        # dvr_output_dir has to be in the sandboxed config, not left to _DEFAULTS: the
        # route resolves it through a runtime load_config(), so a default value means the
        # disk assertion below stats the REAL /dvr on this machine. That is a network
        # mount, and when it went stale the test failed with `total_bytes unexpectedly
        # None` - correct behavior from _disk_bytes (dev/changelog/723) reported as an HA
        # API defect (dev/changelog/724).
        self._dvr_dir = tempfile.mkdtemp(prefix='dvr_test_ha_dvr_')
        self.addCleanup(shutil.rmtree, self._dvr_dir, True)
        self._write_cfg({
            'integrations': {'home_assistant': {
                'enabled': True, 'api_key_hash': generate_password_hash(API_KEY)}},
            'recording': {'dvr_output_dir': self._dvr_dir},
        })
        self.t = make_test_app()
        self.client = self.t.app.test_client()
        self.addCleanup(self.t.cleanup)

    def _get(self):
        r = self.client.get('/api/ha/v1/status', headers={'X-API-Key': API_KEY})
        self.assertEqual(r.status_code, 200)
        return r.get_json()

    def test_bare_object_not_success_envelope(self):
        data = self._get()
        self.assertNotIn('success', data)
        self.assertEqual(set(data.keys()), {'recording', 'disk', 'alerts', 'accounts'})

    def test_recording_counts_and_next_recording(self):
        acc = make_account()
        ch = make_channel(acc, name='News HD')
        now = datetime.utcnow()
        make_recording(status='IN_PROGRESS')
        make_recording(status='CONVERTING')
        soonest = make_recording(status='SCHEDULED', name='Soonest', channel_id=ch.id,
                                 start_time=now + timedelta(minutes=5),
                                 stop_time=now + timedelta(hours=1))
        make_recording(status='SCHEDULED', name='Later',
                       start_time=now + timedelta(hours=3),
                       stop_time=now + timedelta(hours=4))
        db.session.commit()

        rec = self._get()['recording']
        self.assertEqual(rec['capturing_count'], 1)
        self.assertEqual(rec['converting_count'], 1)
        self.assertEqual(rec['next_recording']['id'], soonest.id)
        self.assertEqual(rec['next_recording']['name'], 'Soonest')
        self.assertEqual(rec['next_recording']['channel'], 'News HD')
        self.assertEqual(rec['next_recording']['start_time'], soonest.start_time.isoformat())

    def test_no_scheduled_recording_is_null(self):
        rec = self._get()['recording']
        self.assertEqual(rec['capturing_count'], 0)
        self.assertEqual(rec['converting_count'], 0)
        self.assertIsNone(rec['next_recording'])

    def test_disk_fields_are_byte_counts(self):
        disk = self._get()['disk']
        for key in ('free_bytes', 'used_bytes', 'total_bytes', 'used_pct'):
            self.assertIn(key, disk)
        # Real stat of the temp dvr dir this test owns (see setUp) - not None, and
        # internally consistent.
        self.assertIsNotNone(disk['total_bytes'])
        self.assertEqual(disk['used_bytes'], disk['total_bytes'] - disk['free_bytes'])

    def test_alerts_latest_and_unread_count(self):
        db.session.add(Alert(alert_type='TEST', severity='ERROR', title='Old', body=''))
        db.session.flush()
        newest = Alert(alert_type='TEST', severity='CRIT', title='Newest', body='')
        db.session.add(newest)
        db.session.commit()

        alerts = self._get()['alerts']
        self.assertEqual(alerts['unread_count'], 2)
        self.assertEqual(alerts['latest']['severity'], 'CRIT')
        self.assertEqual(alerts['latest']['title'], 'Newest')
        self.assertEqual(alerts['latest']['created_at'], newest.created_at.isoformat())

    def test_no_alerts_is_null_latest(self):
        alerts = self._get()['alerts']
        self.assertEqual(alerts['unread_count'], 0)
        self.assertIsNone(alerts['latest'])

    def test_account_status_counts(self):
        make_account(name='Good 1')  # defaults to status='OK'
        make_account(name='Good 2')
        bad = make_account(name='Bad')
        bad.status = 'ERROR'
        fresh = make_account(name='Fresh')
        fresh.status = 'UNSYNCED'
        db.session.commit()

        accounts = self._get()['accounts']
        self.assertEqual(accounts['total'], 4)
        self.assertEqual(accounts['ok_count'], 2)
        self.assertEqual(accounts['error_count'], 1)

    def test_no_accounts_is_all_zero(self):
        accounts = self._get()['accounts']
        self.assertEqual(accounts, {'total': 0, 'ok_count': 0, 'error_count': 0})


if __name__ == '__main__':
    unittest.main()
