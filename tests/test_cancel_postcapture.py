"""Cancelling a recording that is past capture must reach the running work, and must stick.

Guards dev/docs/BUGS.md 2026-08-15 @ 11:27 AM ET: the cancel routes ended in a trailing
`else` that stood for two real statuses (CONCATENATING and CONVERTING) and routed both to
abort_recording, which only tears down recorder._active - empty once capture is over - so
the concat/conversion work kept running and later wrote COMPLETED or FAILED over the
ABORTED row the user had asked for.

Two halves, tested separately:
  * routing - each post-capture status is named and handled by the route that can act on it
  * sticking - a terminal write in the concat/convert chain never resurrects a cancelled row
"""
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tests.support.app import make_test_app  # noqa: E402
from tests.support import seed  # noqa: E402
from app import db  # noqa: E402
from app.database import Recording, RecordingEvent  # noqa: E402


def _pp_config(**recording_overrides):
    """A load_config() stand-in that walks do_postprocess straight to its Complete step:
    no ffprobe, no conversion, no move, no post-script. Runtime load_config() reads the
    real config.yaml (CLAUDE.md, Testing), so the code under test has to be patched rather
    than fed make_test_app overrides."""
    cfg = {
        'recording': {
            'gather_health_data': False,
            'post_process': {'enabled': False},
            'move_on_complete': {'enabled': False},
            'post_script': {'enabled': False},
            'dvr_output_dir': '/nonexistent-test-dir',
            'serialize_concat': False,
        },
        'ffmpeg': {'path': 'ffmpeg', 'concat_pre_output_timeout_seconds': 60, 'concat_stall_seconds': 60},
    }
    cfg['recording'].update(recording_overrides)
    return lambda *a, **kw: cfg


class CancelRoutingByStatusTests(unittest.TestCase):
    """Every post-capture status is named explicitly; none falls into a generic abort."""

    def setUp(self):
        self.t = make_test_app()
        self.t.app.config['WTF_CSRF_ENABLED'] = False
        self.acc = seed.make_account()
        self.ch = seed.make_channel(self.acc, stream_id=1, name='Ch')
        db.session.commit()

    def tearDown(self):
        self.t.cleanup()

    def _rec(self, status):
        rec = seed.make_recording(status=status, channel_id=self.ch.id)
        db.session.commit()
        return rec.id

    def test_converting_cancel_delegates_to_the_conversion_cancel_path(self):
        rid = self._rec('CONVERTING')
        with mock.patch('app.postprocessor.request_cancel_conversion', return_value=True) as req, \
             mock.patch('app.routes.recordings.abort_recording') as abort:
            resp = self.t.client.post(f'/recordings/{rid}/cancel-json')
        self.assertEqual(resp.status_code, 200, resp.get_data(as_text=True))
        req.assert_called_once_with(rid)
        self.assertFalse(abort.called,
                         'a CONVERTING cancel reached abort_recording, which cannot stop ffmpeg')

    def test_converting_cancel_with_nothing_live_marks_aborted_and_keeps_the_ts(self):
        rid = self._rec('CONVERTING')
        ts_path = os.path.join(self.t._tmpdir, 'show.ts')
        partial = os.path.join(self.t._tmpdir, 'show.mp4')
        for p in (ts_path, partial):
            with open(p, 'wb') as fh:
                fh.write(b'x' * 16)
        rec = db.session.get(Recording, rid)
        rec.output_path = ts_path
        db.session.commit()

        resp = self.t.client.post(f'/recordings/{rid}/cancel-json')
        self.assertEqual(resp.status_code, 200, resp.get_data(as_text=True))

        db.session.expire_all()
        self.assertEqual(db.session.get(Recording, rid).status, 'ABORTED')
        self.assertTrue(os.path.exists(ts_path), 'the source .ts must survive so Retry works')
        self.assertFalse(os.path.exists(partial), 'the unreadable partial output was left behind')

    def test_concatenating_cancel_is_refused_and_changes_nothing(self):
        rid = self._rec('CONCATENATING')
        resp = self.t.client.post(f'/recordings/{rid}/cancel-json')
        self.assertEqual(resp.status_code, 409, resp.get_data(as_text=True))
        self.assertIn('error', resp.get_json())

        db.session.expire_all()
        self.assertEqual(db.session.get(Recording, rid).status, 'CONCATENATING')
        self.assertEqual(
            RecordingEvent.query.filter_by(recording_id=rid, event_type='RECORDING_ABORTED').count(),
            0, 'a refused cancel still logged RECORDING_ABORTED against a running concat')

    def test_concatenating_cancel_form_route_also_refused(self):
        rid = self._rec('CONCATENATING')
        resp = self.t.client.post(f'/recordings/{rid}/cancel')
        self.assertEqual(resp.status_code, 302, resp.get_data(as_text=True))
        db.session.expire_all()
        self.assertEqual(db.session.get(Recording, rid).status, 'CONCATENATING')

    def test_unrecognized_status_is_an_error_not_an_abort(self):
        rid = self._rec('CONCATENATING')
        rec = db.session.get(Recording, rid)
        rec.status = 'SOMETHING_NEW'
        db.session.commit()

        with mock.patch('app.routes.recordings.abort_recording') as abort:
            resp = self.t.client.post(f'/recordings/{rid}/cancel-json')
        self.assertEqual(resp.status_code, 400, resp.get_data(as_text=True))
        self.assertFalse(abort.called)
        db.session.expire_all()
        self.assertEqual(db.session.get(Recording, rid).status, 'SOMETHING_NEW')

    def test_scheduled_cancel_deletes_the_row(self):
        """The named branches must not have stolen the status the else legitimately served -
        a SCHEDULED recording never captured anything, so cancelling it deletes the row
        rather than marking it ABORTED (dev/changelog/814)."""
        rid = self._rec('SCHEDULED')
        resp = self.t.client.post(f'/recordings/{rid}/cancel-json')
        self.assertEqual(resp.status_code, 200, resp.get_data(as_text=True))
        db.session.expire_all()
        self.assertIsNone(db.session.get(Recording, rid))


class AbortedStatusSticksTests(unittest.TestCase):
    """A cancel that lands while the chain is working is not undone when the work ends."""

    def setUp(self):
        self.t = make_test_app()
        self.acc = seed.make_account()
        self.ch = seed.make_channel(self.acc, stream_id=1, name='Ch')
        db.session.commit()

    def tearDown(self):
        self.t.cleanup()

    def _rec(self, status):
        rec = seed.make_recording(status=status, channel_id=self.ch.id)
        db.session.commit()
        return rec.id

    def test_postprocess_stops_when_the_row_was_cancelled(self):
        from app.postprocessor import do_postprocess
        rid = self._rec('ABORTED')
        with mock.patch('app.config.load_config', _pp_config()):
            do_postprocess(self.t.app, rid, os.path.join(self.t._tmpdir, 'gone.ts'))

        db.session.expire_all()
        self.assertEqual(db.session.get(Recording, rid).status, 'ABORTED')
        self.assertTrue(
            RecordingEvent.query.filter_by(recording_id=rid, event_type='RECORDING_ABORTED').count(),
            'post-processing stopped for a cancelled recording without saying so')

    def test_completion_write_will_not_resurrect_a_row_cancelled_at_the_last_moment(self):
        """The phase-boundary checks cannot see a cancel that commits after they run - the
        conversion registry drops its process handle before the restart loop consumes the
        cancel flag, so that window is real. The terminal write is the backstop.

        The cancel is injected from the post-completion script, the last thing that runs
        before the Complete step, so this exercises the write guard itself rather than any
        earlier check.
        """
        from app.postprocessor import do_postprocess
        rid = self._rec('CONCATENATING')

        def _cancel_from_the_post_script(*args, **kwargs):
            r = db.session.get(Recording, rid)
            r.status = 'ABORTED'
            db.session.commit()
            return mock.Mock(returncode=0, stdout=b'', stderr=b'')

        cfg = _pp_config(post_script={'enabled': True, 'path': '/bin/true', 'timeout_seconds': 5})
        with mock.patch('app.config.load_config', cfg), \
             mock.patch('app.postprocessor.subprocess.run', _cancel_from_the_post_script):
            do_postprocess(self.t.app, rid, os.path.join(self.t._tmpdir, 'gone.ts'))

        db.session.expire_all()
        self.assertEqual(db.session.get(Recording, rid).status, 'ABORTED',
                         'post-processing wrote COMPLETED over a cancelled recording')
        self.assertTrue(
            RecordingEvent.query.filter_by(recording_id=rid, event_type='RECORDING_ABORTED').count(),
            'the row stayed ABORTED but nothing explains why it never completed')

    def test_postprocess_still_completes_a_recording_that_was_not_cancelled(self):
        """Control: the guards must not stop an ordinary post-process from finishing."""
        from app.postprocessor import do_postprocess
        rid = self._rec('CONCATENATING')
        with mock.patch('app.config.load_config', _pp_config()):
            do_postprocess(self.t.app, rid, os.path.join(self.t._tmpdir, 'gone.ts'))

        db.session.expire_all()
        self.assertEqual(db.session.get(Recording, rid).status, 'COMPLETED')

    def test_concat_failure_does_not_overwrite_a_row_cancelled_mid_concat(self):
        """do_concatenation marks CONCATENATING itself, so the cancel is injected after
        that - which is where it lands in production too."""
        from app.concatenator import do_concatenation
        rid = self._rec('IN_PROGRESS')

        def _cancel_midway(recording_id):
            r = db.session.get(Recording, recording_id)
            r.status = 'ABORTED'
            db.session.commit()

        with mock.patch('app.config.load_config', _pp_config()), \
             mock.patch('app.recorder.persist_final_thumbnail', _cancel_midway):
            # No segment rows at all, so the concat takes its no-valid-segments FAILED path.
            do_concatenation(self.t.app, rid)

        db.session.expire_all()
        self.assertEqual(db.session.get(Recording, rid).status, 'ABORTED',
                         'a failed concat wrote FAILED over a recording the user cancelled')
        self.assertTrue(
            RecordingEvent.query.filter_by(recording_id=rid, event_type='RECORDING_ABORTED').count(),
            'the concat gave up on a cancelled recording without leaving an event')


if __name__ == '__main__':
    unittest.main()
