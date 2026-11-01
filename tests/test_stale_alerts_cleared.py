"""Alerts that stood in Active alerts after what they described was over.

Guards dev/changelog/1361 (dev/docs/BUGS.md 2026-10-30 @ 10:09:14 PM). Three open alerts on
prod, each with no way out:

  - STORAGE_PATH_UNUSABLE on four folders the settings had since moved off. The alert
    clears when a probe of its path answers, and nothing probes a path that is no longer
    configured. UnconfiguredStoragePathTests.
  - RECORDING_CHANNEL_FAILING over a recording that had COMPLETED six weeks before. A
    recording created after its own start time began at once, ran its dismiss before the
    pre-start warning was raised, and the warning then stood. RecordingFailingAfterStartTests.
  - EPG_SOURCE_COVERAGE_LOST in Active alerts, which promises the app clears it once the
    problem is over - but it reports what one import did, and its only "clear" was the next
    import not losing anything new, while the channels stayed dark. CoverageLostIsAnEventTests.

No network, no real ffmpeg - see CLAUDE.md §Testing.
  python3 -m unittest tests.test_stale_alerts_cleared
"""
import os
import unittest
from datetime import datetime, timedelta

from app import db, fs_utils, recorder
from app.alerts import STORAGE_PATH_UNUSABLE, is_self_clearing
from app.config import load_config
from app.database import Alert, REC_STATUS_IN_PROGRESS
from app.health_score import _apply_failing_alert, reconcile_recording_failing_alerts
from app.storage_dirs import (clear_unconfigured_path_alerts, configured_write_dirs,
                              sweep_write_dirs)
from app.tz_utils import to_local
from tests.support import make_test_app, seed

RECFAIL = 'RECORDING_CHANNEL_FAILING'


def _storage_alert(path):
    alert = Alert(alert_type=STORAGE_PATH_UNUSABLE, severity='ERROR',
                  title='Live thumbnail directory is unusable', source=path,
                  created_at=datetime.utcnow())
    db.session.add(alert)
    db.session.commit()
    return alert.id


class UnconfiguredStoragePathTests(unittest.TestCase):

    def setUp(self):
        self.t = make_test_app(csrf=False)
        self.old_path = os.path.join(self.t._tmpdir, 'old-screenshots', 'thumbnails')

    def tearDown(self):
        fs_utils.forget_dir_outcome(self.old_path)
        self.t.cleanup()

    def _row(self, alert_id):
        db.session.expire_all()
        return db.session.get(Alert, alert_id)

    def test_a_folder_no_longer_configured_has_its_alert_cleared(self):
        old = _storage_alert(self.old_path)
        cleared = clear_unconfigured_path_alerts(load_config())
        self.assertEqual([self.old_path], cleared)
        row = self._row(old)
        self.assertIsNotNone(row.dismissed_at)
        self.assertTrue(row.cleared_by_app,
                        'the app took it down, so it belongs under Cleared recently, not '
                        'as something a person hid')

    def test_a_configured_folder_keeps_its_alert(self):
        cfg = load_config()
        configured = configured_write_dirs(cfg)[0][0]
        kept = _storage_alert(configured)
        clear_unconfigured_path_alerts(cfg)
        self.assertIsNone(self._row(kept).dismissed_at,
                          'a folder still in use clears only when a probe of it answers')

    def test_the_storage_check_clears_it(self):
        old = _storage_alert(self.old_path)
        sweep_write_dirs(load_config())
        self.assertIsNotNone(self._row(old).dismissed_at)

    def test_a_settings_save_clears_it_without_waiting_for_the_check(self):
        cfg = load_config()
        thumbs = next(p for p, role in configured_write_dirs(cfg)
                      if role.what == 'Live thumbnail directory')
        alert_id = _storage_alert(thumbs)
        r = self.t.client.post('/api/settings/field',
                               json={'path': 'recording.live_thumbnail.enabled',
                                     'value': False})
        self.assertEqual(200, r.status_code, r.get_data(as_text=True))
        self.assertIsNotNone(self._row(alert_id).dismissed_at,
                             'switching the feature off leaves its folder unused, so its '
                             'alert has nothing left to describe')

    def test_the_path_is_forgotten_so_it_can_alert_again(self):
        fs_utils._last_logged_outcome[self.old_path] = fs_utils.PATH_UNREACHABLE
        _storage_alert(self.old_path)
        clear_unconfigured_path_alerts(load_config())
        self.assertNotIn(self.old_path, fs_utils._last_logged_outcome,
                         'a remembered "unusable" would read as no change if the folder '
                         'were configured again while still broken')


class RecordingFailingAfterStartTests(unittest.TestCase):

    def setUp(self):
        self.t = make_test_app()
        self.acct = seed.make_account()
        self.channel = seed.make_channel(self.acct, name='Poor Channel', health_score=7)
        db.session.commit()

    def tearDown(self):
        self.t.cleanup()

    def _open(self):
        db.session.expire_all()
        return Alert.query.filter_by(alert_type=RECFAIL, dismissed_at=None).all()

    def test_a_warning_raised_after_the_start_does_not_stand(self):
        rec = seed.make_recording(status='SCHEDULED', channel_id=self.channel.id)
        db.session.commit()
        # The start lands between the caller's SCHEDULED read and the raise.
        db.session.query(type(rec)).filter_by(id=rec.id).update(
            {'status': REC_STATUS_IN_PROGRESS})
        db.session.commit()
        _apply_failing_alert(rec, self.channel.name, 'effective health score 7 is Poor',
                             source=f'recfail:rec:{rec.id}:ch:{self.channel.id}',
                             is_group=False)
        self.assertEqual([], self._open())

    def test_a_scheduled_recording_keeps_its_warning(self):
        rec = seed.make_recording(status='SCHEDULED', channel_id=self.channel.id)
        db.session.commit()
        _apply_failing_alert(rec, self.channel.name, 'effective health score 7 is Poor',
                             source=f'recfail:rec:{rec.id}:ch:{self.channel.id}',
                             is_group=False)
        self.assertEqual(1, len(self._open()))

    def test_startup_clears_warnings_over_recordings_that_left_scheduled(self):
        done = seed.make_recording(status='COMPLETED', channel_id=self.channel.id)
        waiting = seed.make_recording(status='SCHEDULED', channel_id=self.channel.id)
        db.session.commit()
        for rec_id in (done.id, waiting.id, 999999):
            db.session.add(Alert(alert_type=RECFAIL, severity='WARN', title='t',
                                 source=f'recfail:rec:{rec_id}:ch:{self.channel.id}',
                                 recording_id=rec_id, created_at=datetime.utcnow()))
        db.session.commit()

        self.assertEqual(2, reconcile_recording_failing_alerts())

        still_open = self._open()
        self.assertEqual([waiting.id], [a.recording_id for a in still_open])
        cleared = Alert.query.filter(Alert.alert_type == RECFAIL,
                                     Alert.dismissed_at.isnot(None)).all()
        self.assertTrue(all(a.cleared_by_app for a in cleared))


class RecordNowRaisesNoPreStartWarningTests(unittest.TestCase):
    """The prod case end to end: a recording made after its program had begun, on a Poor
    channel. Its start is stubbed, so the recording stays SCHEDULED and no start-path
    dismiss can mask whether the warning was raised at all."""

    def setUp(self):
        self.t = make_test_app(start_scheduler=True, csrf=False)
        self._real_start = recorder.start_recording
        recorder.start_recording = lambda *a, **kw: None
        self.acct = seed.make_account()
        self.channel = seed.make_channel(self.acct, name='Poor Channel', health_score=7)
        db.session.commit()

    def tearDown(self):
        # Cleanup first: it stops the scheduler and waits out a start job already handed to a
        # worker, and that job must still find the stub. Restored first, a job delayed by a
        # busy box ran the real start, reached for the network and held account 1's
        # connection slot into the next test (dev/docs/BUGS.md 2026-10-31 10:40:49 AM).
        try:
            self.t.cleanup()
        finally:
            recorder.start_recording = self._real_start

    def _post(self, start):
        # The form posts wall-clock times in the display timezone and, beside them, the
        # instants they stand for - wall text alone is two instants in the hour a DST
        # fall-back repeats (dev/docs/BUGS.md 2026-10-31 @ 11:27:31 PM ET).
        stop = start + timedelta(hours=2)
        return self.t.client.post('/recordings/new-json', data={
            'name': 'Rec', 'url': 'http://example.test/live/1',
            'channel_id': str(self.channel.id),
            'start_time': to_local(start).strftime('%Y-%m-%dT%H:%M'),
            'start_time_utc': f'{start:%Y-%m-%dT%H:%M:%S}Z',
            'stop_time': to_local(stop).strftime('%Y-%m-%dT%H:%M'),
            'stop_time_utc': f'{stop:%Y-%m-%dT%H:%M:%S}Z'})

    def test_a_recording_starting_now_gets_no_pre_start_warning(self):
        r = self._post(datetime.utcnow() - timedelta(minutes=10))
        self.assertEqual(200, r.status_code, r.get_data(as_text=True))
        self.assertEqual(0, Alert.query.filter_by(alert_type=RECFAIL).count())

    def test_a_future_recording_still_gets_it(self):
        r = self._post(datetime.utcnow() + timedelta(hours=1))
        self.assertEqual(200, r.status_code, r.get_data(as_text=True))
        self.assertEqual(1, Alert.query.filter_by(alert_type=RECFAIL, dismissed_at=None).count())


class CoverageLostIsAnEventTests(unittest.TestCase):

    def test_it_is_not_listed_under_active_alerts(self):
        self.assertFalse(is_self_clearing('EPG_SOURCE_COVERAGE_LOST'))


if __name__ == '__main__':
    unittest.main()
