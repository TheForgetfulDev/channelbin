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
from unittest.mock import patch

from werkzeug.security import generate_password_hash

from app import db
from app.database import Alert
from tests.support.app import make_test_app
from tests.support.config_sandbox import ConfigSandbox
from tests.support.iocount import IOCounter, all_engines
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


class _StatusFixture(_ConfigSandbox):
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


class StatusEndpointShapeTests(_StatusFixture):
    def test_bare_object_not_success_envelope(self):
        data = self._get()
        self.assertNotIn('success', data)
        self.assertEqual(set(data.keys()),
                         {'app_version', 'recording', 'disk', 'alerts', 'accounts'})

    def test_reports_the_app_version(self):
        """The integration refuses to run against a server older than its declared minimum,
        and reads a missing version as too old (dev/changelog/1037)."""
        from app.version import __version__
        self.assertEqual(self._get()['app_version'], __version__)

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

    def test_capturing_lists_each_in_progress_recording(self):
        """dev/changelog/1146: Home Assistant could see that something was capturing but
        never which recording. The list mirrors next_recording's fields, naive UTC on the
        wire, and carries only IN_PROGRESS rows - never a converting or scheduled one."""
        acc = make_account()
        news = make_channel(acc, name='News HD')
        sports = make_channel(acc, name='Sports 1')
        now = datetime.utcnow().replace(microsecond=0)
        first = make_recording(status='IN_PROGRESS', name='Morning News', channel_id=news.id,
                               start_time=now - timedelta(minutes=30),
                               stop_time=now + timedelta(minutes=30),
                               started_at=now - timedelta(minutes=29))
        second = make_recording(status='IN_PROGRESS', name='The Match', channel_id=sports.id,
                                start_time=now - timedelta(minutes=10),
                                stop_time=now + timedelta(hours=2),
                                started_at=now - timedelta(minutes=10))
        make_recording(status='CONVERTING', name='Converting', channel_id=news.id)
        make_recording(status='SCHEDULED', name='Scheduled', channel_id=news.id,
                       start_time=now + timedelta(hours=1), stop_time=now + timedelta(hours=2))
        db.session.commit()

        rec = self._get()['recording']
        self.assertEqual(rec['capturing'], [
            {'id': first.id, 'name': 'Morning News', 'channel': 'News HD',
             'started_at': first.started_at.isoformat(),
             'stop_time': first.stop_time.isoformat()},
            {'id': second.id, 'name': 'The Match', 'channel': 'Sports 1',
             'started_at': second.started_at.isoformat(),
             'stop_time': second.stop_time.isoformat()},
        ])
        self.assertEqual(rec['capturing_count'], len(rec['capturing']))

    def test_capturing_row_without_a_channel_or_start_stamp_is_null_not_an_error(self):
        make_recording(status='IN_PROGRESS', name='Manual URL')
        db.session.commit()

        (entry,) = self._get()['recording']['capturing']
        self.assertIsNone(entry['channel'])
        self.assertIsNone(entry['started_at'])

    def test_nothing_capturing_is_an_empty_list(self):
        rec = self._get()['recording']
        self.assertEqual(rec['capturing'], [])
        self.assertEqual(rec['capturing_count'], 0)

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
        self.assertEqual((alerts['error_count'], alerts['warn_count']), (0, 0))
        self.assertIsNone(alerts['latest'])

    def test_latest_stays_the_newest_while_the_split_is_added(self):
        """dev/changelog/924: the nav banner now shows the most severe unread alert, but
        `latest` is an API the custom_component reads by name and keeps meaning the newest
        one, INFO included. The red/yellow split is additive."""
        t0 = datetime(2026, 9, 11, 5, 0)
        db.session.add_all([
            Alert(alert_type='TEST', severity='CRIT', title='Oldest crit', body='', created_at=t0),
            Alert(alert_type='TEST', severity='WARN', title='Warn', body='',
                  created_at=t0 + timedelta(minutes=5)),
            Alert(alert_type='TEST', severity='INFO', title='Newest info', body='',
                  created_at=t0 + timedelta(minutes=10)),
        ])
        db.session.commit()

        alerts = self._get()['alerts']
        self.assertEqual(alerts['unread_count'], 3)
        self.assertEqual(alerts['error_count'], 1)
        self.assertEqual(alerts['warn_count'], 1)
        self.assertEqual(alerts['latest']['title'], 'Newest info')

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
        self.assertEqual(accounts, {'total': 0, 'ok_count': 0, 'error_count': 0, 'list': []})


class CapturingListQueryCountTests(_StatusFixture):
    """dev/changelog/1146: the capturing list reads each row's channel name, so it must
    cost the same SQL whether one recording is capturing or several - Recording.channel is
    lazy='joined', and a lazy load per row would scale the poll HA makes every ~45s."""

    def _seed_capturing(self, acc, n):
        for i in range(n):
            ch = make_channel(acc, name=f'Capture {acc.id}-{i}')
            make_recording(status='IN_PROGRESS', name=f'Capture {i}', channel_id=ch.id,
                           started_at=datetime.utcnow())
        db.session.commit()

    def _measure(self):
        # A fresh session, so a channel the seeding left in the identity map cannot answer
        # a per-row lazy load without SQL and hide it.
        db.session.remove()
        with IOCounter(all_engines()) as c:
            data = self._get()
        return c.queries, c.config_parses, len(data['recording']['capturing'])

    def test_query_count_does_not_grow_with_capturing_rows(self):
        self._get()  # the app's first request runs one-off checks of its own
        self._seed_capturing(make_account(name='Small'), 1)
        small_q, small_cfg, small_n = self._measure()
        self._seed_capturing(make_account(name='Large'), 4)
        large_q, large_cfg, large_n = self._measure()

        self.assertEqual((small_n, large_n), (1, 5))
        self.assertEqual(large_q, small_q)
        self.assertEqual(large_cfg, small_cfg)


class AccountListTests(_StatusFixture):
    """dev/changelog/1147: one entry per account for the integration's per-account devices,
    beside the three counts the shipped integration reads by name."""

    def test_each_account_carries_its_own_fields(self):
        acc = make_account(name='Main')
        acc.status = 'ERROR'
        acc.last_sync_at = datetime(2026, 9, 26, 12, 0, 0)
        acc.channel_count = 120
        acc.hidden_channel_count = 7
        acc.provider_exp_date = datetime(2026, 12, 1, 0, 0, 0)
        acc.max_connections = 3
        db.session.commit()

        (entry,) = self._get()['accounts']['list']
        self.assertEqual(entry['id'], acc.id)
        self.assertEqual(entry['name'], 'Main')
        self.assertEqual(entry['type'], acc.account_type)
        self.assertEqual(entry['status'], 'ERROR')
        self.assertEqual(entry['last_sync_at'], '2026-09-26T12:00:00')
        self.assertEqual(entry['channel_count'], 120)
        self.assertEqual(entry['hidden_channel_count'], 7)
        self.assertEqual(entry['provider_exp_date'], '2026-12-01T00:00:00')
        self.assertEqual(entry['max_connections'], 3)
        self.assertEqual(entry['connections_in_use'], 0)

    def test_counts_keep_their_meaning_beside_the_list(self):
        make_account(name='Good')
        bad = make_account(name='Bad')
        bad.status = 'ERROR'
        syncing = make_account(name='Busy')
        syncing.status = 'SYNCING'
        db.session.commit()

        accounts = self._get()['accounts']
        self.assertEqual((accounts['total'], accounts['ok_count'], accounts['error_count']),
                         (3, 1, 1))
        self.assertEqual([a['name'] for a in accounts['list']], ['Good', 'Bad', 'Busy'])

    def test_last_error_masks_the_accounts_own_url(self):
        """An account URL is secret in full, path included (dev/docs/DESIGN-secrets.md
        4.2), so a stored error that still names it must not leave the box as-is."""
        acc = make_account(name='Leaky')
        acc.m3u_url = 'http://provider.test/get.php?username=u5er&password=pa55'
        acc.status = 'ERROR'
        acc.last_error = f'Fetch failed: {acc.m3u_url} timed out'
        db.session.commit()

        err = self._get()['accounts']['list'][0]['last_error']
        self.assertNotIn('pa55', err)
        self.assertNotIn('u5er', err)
        self.assertIn('timed out', err)

    def test_connections_in_use_counts_every_holder_on_that_account(self):
        from app import connection_limits as connlim
        busy = make_account(name='Busy')
        busy.max_connections = 2
        idle = make_account(name='Idle')
        db.session.commit()
        self.assertTrue(connlim.try_acquire(busy.id, 'recording', 1))
        self.assertTrue(connlim.try_acquire(busy.id, 'test', 2))
        self.addCleanup(connlim.release, busy.id, 'recording', 1)
        self.addCleanup(connlim.release, busy.id, 'test', 2)

        by_id = {a['id']: a for a in self._get()['accounts']['list']}
        self.assertEqual(by_id[busy.id]['connections_in_use'], 2)
        self.assertEqual(by_id[busy.id]['max_connections'], 2)
        self.assertEqual(by_id[idle.id]['connections_in_use'], 0)

    def test_next_sync_is_the_scheduled_attempt_not_the_stored_column(self):
        """The stored column goes stale when a sync is deferred (dev/changelog/941)."""
        acc = make_account(name='Deferred')
        acc.next_sync_at = datetime(2026, 9, 1, 0, 0, 0)
        off = make_account(name='Off')
        off.sync_enabled = False
        off.next_sync_at = datetime(2026, 9, 1, 0, 0, 0)
        db.session.commit()
        real = datetime(2026, 9, 27, 18, 30, 0)

        with patch('app.scheduler.next_sync_attempts', return_value={acc.id: real}):
            by_id = {a['id']: a for a in self._get()['accounts']['list']}
        self.assertEqual(by_id[acc.id]['next_sync_at'], '2026-09-27T18:30:00')
        self.assertIsNone(by_id[off.id]['next_sync_at'])


class AccountListQueryCountTests(_StatusFixture):
    """dev/changelog/1147: the account list is polled every ~45s, so its SQL and config
    reads must not grow with the number of accounts."""

    def _measure(self):
        db.session.remove()
        with IOCounter(all_engines()) as c:
            data = self._get()
        return c.queries, c.config_parses, len(data['accounts']['list'])

    def test_query_count_does_not_grow_with_accounts(self):
        self._get()
        make_account(name='One')
        db.session.commit()
        small = self._measure()
        for i in range(5):
            make_account(name=f'More {i}')
        db.session.commit()
        large = self._measure()

        self.assertEqual((small[2], large[2]), (1, 6))
        self.assertEqual(large[:2], small[:2])


if __name__ == '__main__':
    unittest.main()
