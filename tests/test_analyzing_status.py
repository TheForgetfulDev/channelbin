"""The post-capture analysis phase has a status of its own, and every surface names it.

Guards dev/docs/BUGS.md 2026-08-30 @ 08:12:04 AM ET: do_postprocess() runs inside
do_concatenation()'s chain and nothing moved the row off CONCATENATING until the conversion
started, so one status stood for the concat (seconds - a single-segment concat is an
os.rename) and for the whole-file reads that follow it (~124s each on a 17.9 GB capture).
The UI named the first while doing the second, for a window that reaches hours, and the
resume path put the row *back* on CONCATENATING to do post-processing - which is why an
activity log could read "concat complete", then "resuming concatenation", then "concat
failed" for a recording sitting whole on disk.

No real ffmpeg or ffprobe anywhere here: the analysis helpers are patched, following the
pattern in tests/test_cancel_postcapture.py.
"""
import os
import sys
import time
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tests.support.app import make_test_app  # noqa: E402
from tests.support import seed  # noqa: E402
from app import db  # noqa: E402
from app.database import Recording, RecordingEvent  # noqa: E402


def _pp_config(**recording_overrides):
    """A load_config() stand-in that walks do_postprocess straight to its Complete step.
    Runtime load_config() reads the real config.yaml (CLAUDE.md, Testing), so the code under
    test is patched rather than fed make_test_app overrides."""
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


class AnalysisPhaseHasItsOwnStatusTests(unittest.TestCase):
    """The row stops naming the concat the moment the concat is over."""

    def setUp(self):
        self.t = make_test_app()
        self.acc = seed.make_account()
        self.ch = seed.make_channel(self.acc, stream_id=1, name='Ch')
        db.session.commit()

    def tearDown(self):
        self.t.cleanup()

    def _rec(self, status, **kw):
        rec = seed.make_recording(status=status, channel_id=self.ch.id, **kw)
        db.session.commit()
        return rec.id

    def test_the_row_reads_analyzing_while_the_file_is_being_read_back(self):
        """The defect itself: the status observed from inside the analysis step."""
        from app.postprocessor import do_postprocess
        rid = self._rec('CONCATENATING')
        seen = []

        def _spy(recording_id, ts_path, rec, cfg, **kwargs):
            db.session.expire_all()
            seen.append(db.session.get(Recording, recording_id).status)
            return {}, None

        with mock.patch('app.config.load_config', _pp_config(gather_health_data=True)), \
             mock.patch('app.postprocessor._gather_recording_health', _spy), \
             mock.patch('app.postprocessor._scan_recording_timeline', return_value=None), \
             mock.patch('app.postprocessor._detect_near_empty_segments'), \
             mock.patch('app.health_score.apply_capture_quality_correction'):
            do_postprocess(self.t.app, rid, os.path.join(self.t._tmpdir, 'gone.ts'))

        self.assertEqual(seen, ['ANALYZING'],
                         'the row still named the concat while the file was being read back')

    def test_entering_the_analysis_phase_is_logged_as_its_own_event(self):
        from app.postprocessor import do_postprocess
        rid = self._rec('CONCATENATING')
        with mock.patch('app.config.load_config', _pp_config()):
            do_postprocess(self.t.app, rid, os.path.join(self.t._tmpdir, 'gone.ts'))

        self.assertEqual(
            RecordingEvent.query.filter_by(
                recording_id=rid, event_type='POSTCAPTURE_ANALYSIS_STARTED').count(), 1,
            'nothing in the event log says the post-capture analysis phase began')

    def test_a_cancelled_row_is_not_resurrected_into_analyzing(self):
        """The entry status write is a status write like any other, so it takes the same
        preserve_cancelled_status guard the conversion's does."""
        from app.postprocessor import do_postprocess
        rid = self._rec('ABORTED')
        with mock.patch('app.config.load_config', _pp_config()):
            do_postprocess(self.t.app, rid, os.path.join(self.t._tmpdir, 'gone.ts'))

        db.session.expire_all()
        self.assertEqual(db.session.get(Recording, rid).status, 'ABORTED')
        self.assertEqual(
            RecordingEvent.query.filter_by(
                recording_id=rid, event_type='POSTCAPTURE_ANALYSIS_STARTED').count(), 0,
            'a cancelled recording was walked into the analysis phase anyway')

    def test_resuming_post_processing_does_not_put_the_row_back_on_concatenating(self):
        """The reported symptom, directly: "it says concat done, then later resuming concat".

        do_concatenation's already-concatenated branch exists *because* the concat is
        finished, so naming the concat there is the one place the old status was not merely
        stale but actively contradicted by the branch it sits in.
        """
        from app.concatenator import do_concatenation
        ts_path = os.path.join(self.t._tmpdir, 'done.ts')
        with open(ts_path, 'wb') as fh:
            fh.write(b'x' * 32)
        rid = self._rec('CONCATENATING', output_path=ts_path)
        seen = []

        def _spy(app, recording_id, path):
            db.session.expire_all()
            seen.append(db.session.get(Recording, recording_id).status)

        with mock.patch('app.config.load_config', _pp_config()), \
             mock.patch('app.postprocessor.do_postprocess', _spy):
            do_concatenation(self.t.app, rid)

        self.assertEqual(seen, ['ANALYZING'],
                         'the resume path re-announced a concatenation that was already done')


class AnalyzingIsBlockingAndRecoverableTests(unittest.TestCase):
    """A status nothing knows about is a row that strands."""

    def test_analyzing_blocks_a_restart(self):
        """Two ffprobe children and a chain thread are live in this window, so ./restart.sh
        must refuse - RESTART_BLOCKING_STATUSES is what both restart surfaces read."""
        from app.database import RESTART_BLOCKING_STATUSES, REC_STATUS_ANALYZING
        self.assertIn(REC_STATUS_ANALYZING, RESTART_BLOCKING_STATUSES)

    def test_analyzing_row_is_picked_up_at_startup(self):
        t = make_test_app(start_scheduler=True)
        try:
            from app.scheduler import resume_in_progress_recordings
            rec = seed.make_recording(status='ANALYZING', name='stranded analysis')
            db.session.commit()
            rid = rec.id

            with mock.patch('app.concatenator.do_concatenation') as fake:
                resume_in_progress_recordings(t.app)
                deadline = time.monotonic() + 2.0
                while time.monotonic() < deadline and not fake.called:
                    time.sleep(0.01)

            self.assertTrue(fake.called,
                            'an ANALYZING row at restart is stranded forever, and it blocks '
                            'every future restart')
            self.assertEqual(fake.call_args.args[1], rid)
        finally:
            t.cleanup()


class AnalyzingIsNamedByEverySurfaceTests(unittest.TestCase):
    """CLAUDE.md's states-are-enumerated rule, applied to the status just added."""

    def setUp(self):
        self.t = make_test_app()
        self.t.app.config['WTF_CSRF_ENABLED'] = False
        self.acc = seed.make_account()
        self.ch = seed.make_channel(self.acc, stream_id=1, name='Ch')
        db.session.commit()

    def tearDown(self):
        self.t.cleanup()

    def _rec(self, status='ANALYZING', **kw):
        rec = seed.make_recording(status=status, channel_id=self.ch.id, **kw)
        db.session.commit()
        return rec.id

    def test_the_recordings_list_gives_it_its_own_row_rendering(self):
        """Not the st-abort/.get() fallback, which is how a real state renders as an
        unknown one without erroring."""
        from app.fmt_utils import REC_STATUS_DISPLAY
        from app.database import REC_STATUS_ANALYZING
        self.assertIn(REC_STATUS_ANALYZING, REC_STATUS_DISPLAY)
        section, _st, _badge, label, _pulse = REC_STATUS_DISPLAY[REC_STATUS_ANALYZING]
        self.assertEqual(section, 'live')
        self.assertEqual(label, 'ANALYZING')

    def test_the_restart_modal_explains_it_in_words(self):
        from app.routes.settings import _BLOCKING_PHRASE
        from app.database import RESTART_BLOCKING_STATUSES
        for status in RESTART_BLOCKING_STATUSES:
            self.assertIn(status, _BLOCKING_PHRASE,
                          f'the restart warning would show the raw {status} string')

    def test_cancel_is_accepted_and_keeps_the_recorded_file(self):
        """ANALYZING is cancellable where CONCATENATING is refused, and the difference is
        what is on disk: the concat committed its output, so the file is whole and the work
        still running only reads it."""
        ts_path = os.path.join(self.t._tmpdir, 'show.ts')
        with open(ts_path, 'wb') as fh:
            fh.write(b'x' * 16)
        rid = self._rec(output_path=ts_path)

        resp = self.t.client.post(f'/recordings/{rid}/cancel-json')
        self.assertEqual(resp.status_code, 200, resp.get_data(as_text=True))

        db.session.expire_all()
        self.assertEqual(db.session.get(Recording, rid).status, 'ABORTED')
        self.assertTrue(os.path.exists(ts_path),
                        'the recorded .ts was destroyed by cancelling the analysis of it')
        self.assertTrue(
            RecordingEvent.query.filter_by(
                recording_id=rid, event_type='RECORDING_ABORTED').count(),
            'the row went ABORTED with nothing explaining why')

    def test_cancel_does_not_land_in_the_unrecognized_status_branch(self):
        """tests/test_cancel_postcapture.py's invariant, for the new status: a real state
        answered by the trailing else is the defect that file exists for."""
        rid = self._rec()
        resp = self.t.client.post(f'/recordings/{rid}/cancel-json')
        self.assertEqual(resp.status_code, 200)
        self.assertNotIn('unrecognized', resp.get_data(as_text=True).lower())

    def test_retry_conversion_is_refused_while_a_chain_is_live(self):
        """is_conversion_active() only holds once ffmpeg has been spawned, so it is blind
        to the whole analysis phase. Without the live-chain claim a second Retry starts a
        duplicate chain over the same file."""
        from app.concatenator import _claim_concat, _release_concat
        ts_path = os.path.join(self.t._tmpdir, 'show.ts')
        with open(ts_path, 'wb') as fh:
            fh.write(b'x' * 16)
        rid = self._rec(output_path=ts_path)

        _claim_concat(rid)
        try:
            with mock.patch('app.concatenator.run_postprocess_claimed') as fake:
                self.t.client.post(f'/recordings/{rid}/retry-convert')
            self.assertFalse(fake.called,
                             'Retry conversion started a second chain over a live one')
        finally:
            _release_concat(rid)

    def test_retry_conversion_rescues_a_stranded_row_and_does_not_claim_to_be_converting(self):
        ts_path = os.path.join(self.t._tmpdir, 'show.ts')
        with open(ts_path, 'wb') as fh:
            fh.write(b'x' * 16)
        rid = self._rec(output_path=ts_path)

        with mock.patch('app.concatenator.run_postprocess_claimed') as fake:
            resp = self.t.client.post(f'/recordings/{rid}/retry-convert')
            self.assertEqual(resp.status_code, 302)
            deadline = time.monotonic() + 2.0
            while time.monotonic() < deadline and not fake.called:
                time.sleep(0.01)

        self.assertTrue(fake.called, 'an ANALYZING row has no manual recovery at all')
        db.session.expire_all()
        self.assertEqual(db.session.get(Recording, rid).status, 'ANALYZING',
                         'Retry marked the row CONVERTING before any ffmpeg existed')


if __name__ == '__main__':
    unittest.main()
