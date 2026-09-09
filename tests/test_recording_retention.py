"""Recording retention sweep + delete-with-files.

Guards changelog/178 (recording retention/cleanup): the daily retention job must
delete terminal recordings AND their on-disk files once past their effective window
(global recording.retention_days, overridable per profile), never touch active/
scheduled rows, and honor a profile's 0 = keep-forever override.

Test-seam note: runtime load_config() returns the real config.yaml (make_test_app
overrides only reach create_app time), so the global window is injected by patching
app.config.load_config for the job call. Recording files live under the temp dir and are
addressed via the DB row's own output_path/segment paths (recording_disk_paths reads
those from the row), keeping the test fully isolated from real /dvr.
"""
import os
import sys
import unittest
from datetime import datetime, timedelta
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tests.support.app import make_test_app  # noqa: E402
from tests.support import seed  # noqa: E402
from app import db  # noqa: E402
from app import scheduler  # noqa: E402
import app.config as appconfig  # noqa: E402
from app.database import Recording, RecordingSegment, RecordingProfile  # noqa: E402

_REAL_LOAD_CONFIG = appconfig.load_config


def _load_config_with_retention(days, delete_file=True):
    """A load_config replacement that forces recording.retention_days = days and
    recording.retention_delete_file = delete_file (the job re-imports load_config
    locally at call time, so patching app.config catches it)."""
    def _inner(overrides=None):
        cfg = _REAL_LOAD_CONFIG(overrides)
        rec = cfg.setdefault('recording', {})
        rec['retention_days'] = days
        rec['retention_delete_file'] = delete_file
        return cfg
    return _inner


def _touch(path, data=b'\x00' * 64):
    with open(path, 'wb') as f:
        f.write(data)


class RecordingRetentionTests(unittest.TestCase):
    def setUp(self):
        # start_scheduler=True wires scheduler._app + scheduler._scheduler (the job and
        # its unschedule_recording call both need them) against this test's temp DB.
        self.t = make_test_app(start_scheduler=True)
        self.tmp = self.t._tmpdir
        self.acc = seed.make_account()
        self.ch = seed.make_channel(self.acc, stream_id=1, name='Ch')
        db.session.commit()

    def tearDown(self):
        self.t.cleanup()

    def _make(self, *, status='COMPLETED', age_days=40, profile_id=None):
        """A terminal recording that finished age_days ago, with an output file and one
        segment file both under the temp dir. Returns (id, [output, segment])."""
        finished = datetime.utcnow() - timedelta(days=age_days)
        out = os.path.join(self.tmp, f'ret_{status}_{age_days}.ts')
        rec = seed.make_recording(status=status, channel_id=self.ch.id,
                                  completed_at=finished, stop_time=finished,
                                  output_path=out, profile_id=profile_id)
        seg = os.path.join(self.tmp, f'ret_{rec.id}_seg_000.ts')
        db.session.add(RecordingSegment(
            recording_id=rec.id, segment_number=0, file_path=seg,
            started_at=finished, ended_at=finished,
            exit_reason='STOP_TIME_REACHED', bytes_recorded=64))
        _touch(out)
        _touch(seg)
        db.session.commit()
        return rec.id, [out, seg]

    def _run_job(self, global_days, delete_file=True):
        with mock.patch('app.config.load_config',
                         _load_config_with_retention(global_days, delete_file)):
            scheduler._recording_retention_job()
        db.session.expire_all()  # the job commits in its own nested session

    def test_old_recording_deleted_with_all_files(self):
        rid, paths = self._make(age_days=40)
        self._run_job(30)
        self.assertIsNone(db.session.get(Recording, rid), 'old recording row not deleted')
        for p in paths:
            self.assertFalse(os.path.exists(p), f'file left on disk: {p}')

    def test_recent_recording_kept(self):
        rid, paths = self._make(age_days=5)  # inside the 30-day window
        self._run_job(30)
        self.assertIsNotNone(db.session.get(Recording, rid), 'recent recording wrongly deleted')
        for p in paths:
            self.assertTrue(os.path.exists(p), f'file wrongly removed: {p}')

    def test_active_and_scheduled_never_deleted(self):
        old = datetime.utcnow() - timedelta(days=99)  # past the window, but not terminal
        sched = seed.make_recording(status='SCHEDULED', channel_id=self.ch.id)
        prog = seed.make_recording(status='IN_PROGRESS', channel_id=self.ch.id,
                                   completed_at=old, stop_time=old)
        db.session.commit()
        sched_id, prog_id = sched.id, prog.id
        self._run_job(30)
        self.assertIsNotNone(db.session.get(Recording, sched_id), 'SCHEDULED deleted')
        self.assertIsNotNone(db.session.get(Recording, prog_id), 'IN_PROGRESS deleted')

    def test_profile_zero_overrides_global_keep_forever(self):
        prof = RecordingProfile(name='Keep', retention_days=0)
        db.session.add(prof)
        db.session.commit()
        rid, _ = self._make(age_days=999, profile_id=prof.id)
        self._run_job(30)
        self.assertIsNotNone(db.session.get(Recording, rid),
                             'profile retention_days=0 must keep forever despite global window')

    def test_profile_shorter_window_overrides_global(self):
        prof = RecordingProfile(name='Aggressive', retention_days=3)
        db.session.add(prof)
        db.session.commit()
        # 10 days old: inside the 30-day global window (would be kept) but past the
        # profile's 3-day window (must be deleted).
        rid, _ = self._make(age_days=10, profile_id=prof.id)
        self._run_job(30)
        self.assertIsNone(db.session.get(Recording, rid),
                          'profile 3-day window must override the longer global window')

    def test_disabled_global_deletes_nothing(self):
        rid, paths = self._make(age_days=9999)
        self._run_job(0)
        self.assertIsNotNone(db.session.get(Recording, rid),
                             'retention_days=0 global must never delete')
        for p in paths:
            self.assertTrue(os.path.exists(p), f'file wrongly removed with retention disabled: {p}')

    def test_retention_delete_file_off_deletes_row_but_keeps_files(self):
        rid, paths = self._make(age_days=40)
        self._run_job(30, delete_file=False)
        self.assertIsNone(db.session.get(Recording, rid),
                          'row must still be deleted with retention_delete_file off')
        for p in paths:
            self.assertTrue(os.path.exists(p), f'file wrongly removed with retention_delete_file off: {p}')

    def test_retention_delete_file_default_is_off(self):
        self.assertIs(appconfig._DEFAULTS['recording']['retention_delete_file'], False,
                      'retention_delete_file must default to off (files kept)')


if __name__ == '__main__':
    unittest.main(verbosity=2)
