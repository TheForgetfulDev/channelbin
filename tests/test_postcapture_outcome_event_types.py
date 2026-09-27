"""The join and the conversion each carry their outcome in the event type.

Guards dev/docs/BUGS.md 2026-09-26 @ 07:00:11 PM ET: CONCATENATION_DONE was written for a
finished join and for every join that gave up, and CONVERSION_DONE for a finished
conversion, a give-up and a user cancel, with the outcome only in a `FAILED` prefix of the
detail. The detail page colors the event log by type, so "FAILED: no valid segments found"
rendered in the success color (dev/changelog/1140).

  * every failure writer of the join logs CONCATENATION_FAILED and no CONCATENATION_DONE
  * every give-up of the conversion logs CONVERSION_FAILED, every cancel CONVERSION_CANCELLED,
    and neither logs CONVERSION_DONE
  * the event log renders the failed types `bad` and the cancelled type `warn`
  * migration 79 retypes the old rows by their detail and leaves successes alone

Run:
  python3 -m unittest tests.test_postcapture_outcome_event_types
"""
import os
import sqlite3
import sys
import tempfile
import unittest
from datetime import datetime, timedelta
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import app.config as cfgmod  # noqa: E402
import app.migrations as M  # noqa: E402
import app.postprocessor as ppmod  # noqa: E402
from tests.support.app import make_test_app  # noqa: E402
from tests.support import seed  # noqa: E402
from tests.test_failure_reason_disclosure import _SandboxCase  # noqa: E402
from app import db  # noqa: E402
from app.database import (  # noqa: E402
    Recording, RecordingEvent, RecordingSegment, add_recording_event,
    CONCATENATION_DONE, CONCATENATION_FAILED,
    CONVERSION_DONE, CONVERSION_FAILED, CONVERSION_CANCELLED,
    REC_STATUS_ABORTED, REC_STATUS_CONCATENATING, REC_STATUS_CONVERTING, REC_STATUS_FAILED,
)


class _EventTypeCase(_SandboxCase):
    def _types(self, rid):
        db.session.expire_all()
        return [e.event_type for e in RecordingEvent.query.filter_by(recording_id=rid)]


class JoinFailureEventTypeTests(_EventTypeCase):
    """The three ways the join gives up, driven through the real do_concatenation."""

    def _recording(self, segments):
        now = datetime.utcnow()
        rec = seed.make_recording(status=REC_STATUS_CONCATENATING, name='join',
                                  start_time=now - timedelta(hours=1), stop_time=now)
        for n, (size_on_disk, bytes_recorded) in enumerate(segments, start=1):
            path = os.path.join(self.dvr, f'rec_{rec.id}_seg_{n:03d}.ts')
            with open(path, 'wb') as fh:
                fh.write(b'x' * size_on_disk)
            db.session.add(RecordingSegment(
                recording_id=rec.id, segment_number=n, file_path=path,
                started_at=rec.start_time, ended_at=rec.stop_time,
                exit_reason='STOP_TIME_REACHED', bytes_recorded=bytes_recorded))
        db.session.commit()
        return rec.id

    def _join(self, rid):
        from app.concatenator import do_concatenation
        do_concatenation(self.t.app, rid)

    def _assert_failed_not_done(self, rid):
        types = self._types(rid)
        self.assertEqual(REC_STATUS_FAILED, db.session.get(Recording, rid).status,
                         'setup: expected the FAILED path')
        self.assertEqual(1, types.count(CONCATENATION_FAILED), types)
        self.assertNotIn(CONCATENATION_DONE, types,
                         'CONCATENATION_DONE must mean a joined file and nothing else')

    def test_no_valid_segments(self):
        rid = self._recording([(0, 0)])
        self._join(rid)
        self._assert_failed_not_done(rid)

    def test_insufficient_disk_space(self):
        rid = self._recording([(4096, 4096)])
        with mock.patch('app.concatenator.shutil.disk_usage',
                        return_value=mock.Mock(free=1, total=100, used=99)):
            self._join(rid)
        self._assert_failed_not_done(rid)

    def test_concat_error(self):
        rid = self._recording([(4096, 4096)])
        with mock.patch('app.concatenator.os.rename', side_effect=OSError('boom')):
            self._join(rid)
        self._assert_failed_not_done(rid)

    def test_live_frame_carries_the_failed_type(self):
        """The recording page reloads on the frame's name, so the frame is typed too."""
        rid = self._recording([(0, 0)])
        with mock.patch('app.events.publish') as pub:
            self._join(rid)
        names = [c.args[1] for c in pub.call_args_list]
        self.assertIn(CONCATENATION_FAILED, names)
        self.assertNotIn(CONCATENATION_DONE, names)


class ConversionOutcomeEventTypeTests(_EventTypeCase):
    """The in-loop give-up and cancel, driven through the real do_postprocess."""

    def _run(self, *, cancel):
        cfg = cfgmod._deep_merge(cfgmod.load_config(), self._config(
            enabled=True, format='mkv', delete_source=False, reencode_mode='never',
            pre_output_timeout_seconds=60, auto_restart=False, max_restart_attempts=0,
            stall_seconds=0, progress_interval_seconds=5))
        ts = os.path.join(self.dvr, 'show.ts')
        with open(ts, 'wb') as fh:
            fh.write(b'x' * 4096)
        rec = seed.make_recording(status=REC_STATUS_CONCATENATING, name='convert',
                                  output_path=ts)
        db.session.commit()
        stub = mock.Mock(return_value=ppmod.ConversionResult(False, 'died', 'boom'))
        with mock.patch.object(cfgmod, 'load_config', return_value=cfg), \
             mock.patch.object(ppmod, 'run_conversion_supervised', stub), \
             mock.patch.object(ppmod, '_consume_cancel', return_value=cancel):
            ppmod.do_postprocess(self.t.app, rec.id, ts)
        return rec.id

    def test_give_up_is_conversion_failed(self):
        rid = self._run(cancel=False)
        types = self._types(rid)
        self.assertEqual(REC_STATUS_FAILED, db.session.get(Recording, rid).status)
        self.assertEqual(1, types.count(CONVERSION_FAILED), types)
        self.assertNotIn(CONVERSION_DONE, types)

    def test_user_cancel_is_conversion_cancelled(self):
        rid = self._run(cancel=True)
        types = self._types(rid)
        self.assertEqual(REC_STATUS_ABORTED, db.session.get(Recording, rid).status)
        self.assertEqual(1, types.count(CONVERSION_CANCELLED), types)
        self.assertNotIn(CONVERSION_DONE, types)


class StartupConversionGiveUpEventTypeTests(_EventTypeCase):
    """The two give-ups the startup sweep makes for a row left CONVERTING."""
    start_scheduler = True

    def _sweep(self):
        from app.scheduler import resume_in_progress_recordings
        resume_in_progress_recordings(self.t.app)

    def test_source_missing(self):
        rec = seed.make_recording(status=REC_STATUS_CONVERTING, name='gone',
                                  output_path=os.path.join(self.dvr, 'gone.ts'))
        db.session.commit()
        self._sweep()
        types = self._types(rec.id)
        self.assertEqual(1, types.count(CONVERSION_FAILED), types)
        self.assertNotIn(CONVERSION_DONE, types)

    def test_budget_exhausted(self):
        self.t.sandbox_config(self._config(enabled=True, auto_restart=True,
                                           max_restart_attempts=1))
        ts = os.path.join(self.dvr, 'show.ts')
        with open(ts, 'wb') as fh:
            fh.write(b'x' * 64)
        rec = seed.make_recording(status=REC_STATUS_CONVERTING, name='spent',
                                  output_path=ts, conversion_attempts=1)
        db.session.commit()
        self._sweep()
        types = self._types(rec.id)
        self.assertEqual(1, types.count(CONVERSION_FAILED), types)
        self.assertNotIn(CONVERSION_DONE, types)


class StrandedCancelRouteEventTypeTests(unittest.TestCase):
    """The cancel route's own branch, for a CONVERTING row with no live conversion."""

    def setUp(self):
        self.t = make_test_app()
        self.t.app.config['WTF_CSRF_ENABLED'] = False

    def tearDown(self):
        self.t.cleanup()

    def test_stranded_cancel_is_conversion_cancelled(self):
        ts = os.path.join(self.t._tmpdir, 'stranded.ts')
        with open(ts, 'wb') as fh:
            fh.write(b'x' * 64)
        rec = seed.make_recording(status=REC_STATUS_CONVERTING, name='stranded',
                                  output_path=ts)
        db.session.commit()
        rid = rec.id
        resp = self.t.client.post(f'/recordings/{rid}/cancel-json')
        self.assertEqual(200, resp.status_code, resp.get_data(as_text=True))
        db.session.expire_all()
        types = [e.event_type for e in RecordingEvent.query.filter_by(recording_id=rid)]
        self.assertEqual(1, types.count(CONVERSION_CANCELLED), types)
        self.assertNotIn(CONVERSION_DONE, types)


class EventLogClassTests(unittest.TestCase):
    """The event log colors each outcome by its type."""

    def setUp(self):
        self.t = make_test_app()

    def tearDown(self):
        self.t.cleanup()

    def test_each_outcome_renders_in_its_own_class(self):
        rec = seed.make_recording(status=REC_STATUS_FAILED, name='outcomes')
        db.session.flush()
        expected = {
            CONCATENATION_DONE: 'ok', CONVERSION_DONE: 'ok',
            CONCATENATION_FAILED: 'bad', CONVERSION_FAILED: 'bad',
            CONVERSION_CANCELLED: 'warn',
        }
        for event_type in expected:
            add_recording_event(rec.id, event_type, detail=f'{event_type} detail')
        db.session.commit()
        html = self.t.client.get(f'/recordings/{rec.id}').get_data(as_text=True)
        db.session.expire_all()
        for evt in RecordingEvent.query.filter_by(recording_id=rec.id):
            cls = expected[evt.event_type]
            self.assertIn(f'<li class="ev {cls}" data-ev-id="{evt.id}">', html,
                          f'{evt.event_type} must render as {cls}')


class RetypeMigrationTests(unittest.TestCase):
    """Migration 79 - the old rows move to the type they should have had. Raw scratch-DB
    characterization like the other pure-SQL steps in tests/test_migrations_runner.py."""

    ROWS = [
        ('CONCATENATION_DONE', 'Concat complete: /dvr/a.ts (1.2 GB)', 'CONCATENATION_DONE'),
        ('CONCATENATION_DONE', 'FAILED: no valid segments found', 'CONCATENATION_FAILED'),
        ('CONCATENATION_DONE', 'FAILED: not enough disk space - need 2 GB', 'CONCATENATION_FAILED'),
        ('CONVERSION_DONE', 'Conversion complete: a.mp4 (900 MB)', 'CONVERSION_DONE'),
        ('CONVERSION_DONE', 'FAILED after 4 attempt(s) (gave up: boom)', 'CONVERSION_FAILED'),
        ('CONVERSION_DONE', 'FAILED: source .ts missing at restart', 'CONVERSION_FAILED'),
        ('CONVERSION_DONE', 'Conversion cancelled by user - source .ts kept for retry.',
         'CONVERSION_CANCELLED'),
        # Other types whose detail happens to start FAILED are not this step's business.
        ('FILE_MOVED', 'Move FAILED: denied', 'FILE_MOVED'),
        ('RECORDING_FAILED', 'FAILED: dead', 'RECORDING_FAILED'),
    ]

    def _run(self, times):
        with tempfile.TemporaryDirectory() as td:
            conn = sqlite3.connect(os.path.join(td, 'scratch.db'))
            cur = conn.cursor()
            cur.execute('CREATE TABLE recording_events (id INTEGER PRIMARY KEY, '
                        'recording_id INTEGER, event_type VARCHAR(64), detail TEXT)')
            for old, detail, _new in self.ROWS:
                cur.execute('INSERT INTO recording_events (recording_id, event_type, detail) '
                            'VALUES (1, ?, ?)', (old, detail))
            conn.commit()
            for _ in range(times):
                M._m079_postcapture_outcome_event_types(conn, cur)
                conn.commit()
            got = [r[0] for r in cur.execute(
                'SELECT event_type FROM recording_events ORDER BY id')]
            conn.close()
        return got

    def test_retypes_by_detail_and_leaves_successes_alone(self):
        self.assertEqual([new for _old, _detail, new in self.ROWS], self._run(1))

    def test_rerunnable(self):
        self.assertEqual([new for _old, _detail, new in self.ROWS], self._run(2))

    def test_registered_as_migration_79(self):
        self.assertIn((79, M._m079_postcapture_outcome_event_types),
                      [(v, fn) for v, _d, fn in M.SCHEMA_MIGRATIONS])


if __name__ == '__main__':
    unittest.main()
