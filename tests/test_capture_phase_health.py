"""Tier 2 - the capture-phase health observation (changelog 279 / BUGS.md 2026-07-23 09:07 PM).

Guards the "a capture that ran to the end reaches its channel's health score no matter what
a later local step does with the segments" invariant. Before the fix the only observation on
this pipeline sat at the very end of post-processing, so a conversion give-up, a concat
error, or a full disk silently discarded every stall and restart the feed actually produced.

No real ffmpeg: a single seeded segment takes the rename path through do_concatenation, and
the conversion runner is stubbed.
"""
import os
import sys
import unittest
from datetime import datetime, timedelta
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import app.config as cfgmod  # noqa: E402
import app.concatenator as catmod  # noqa: E402
import app.postprocessor as ppmod  # noqa: E402
from tests.support.app import make_test_app  # noqa: E402
from tests.support import seed  # noqa: E402
from app import db  # noqa: E402
from app.concatenator import do_concatenation  # noqa: E402
from app.database import Channel, Recording, RecordingSegment  # noqa: E402
from app.postprocessor import ConversionResult, do_postprocess  # noqa: E402

# Metrics chosen so score_recording_metrics_quality lands well under 100 and well over the
# fail floor: 1800s downtime of a 7200s (2h) window (25%) → base 75, 2 restarts/2h × 2.0/hr
# instability → 73.
BAD_CAPTURE = {'total_stall_count': 3, 'total_restart_count': 2,
               'consecutive_failures_peak': 5, 'total_downtime_seconds': 1800}


class CapturePhaseHealthTests(unittest.TestCase):
    def setUp(self):
        self.t = make_test_app()
        self.dvr_dir = os.path.join(self.t._tmpdir, 'dvr')
        os.makedirs(self.dvr_dir, exist_ok=True)

        acc = seed.make_account()
        # Anchored 30 days back so the decay blend gives the new observation real weight;
        # an anchor at "now" would leave effective_alpha at 0 and the score unmoved.
        self.channel = seed.make_channel(
            acc, name='Bad Feed', health_score=100.0, health_score_sample_count=2,
            health_score_updated_at=datetime.utcnow() - timedelta(days=30))
        now = datetime.utcnow()
        self.rec = seed.make_recording(
            status='CONCATENATING', name='capture_phase', channel_id=self.channel.id,
            start_time=now - timedelta(hours=2), stop_time=now, **BAD_CAPTURE)
        self.rid = self.rec.id
        self.cid = self.channel.id

        self.seg_path = os.path.join(self.t._tmpdir, f'rec_{self.rid}_seg_000.ts')
        with open(self.seg_path, 'wb') as fh:
            fh.write(b'x' * 4096)
        db.session.add(RecordingSegment(
            recording_id=self.rid, segment_number=0, file_path=self.seg_path,
            started_at=self.rec.start_time, ended_at=self.rec.stop_time,
            exit_reason='STOP_TIME_REACHED', bytes_recorded=4096))
        db.session.commit()

    def tearDown(self):
        with ppmod._active_lock:
            ppmod._active_conversions.clear()
            ppmod._cancel_requested.clear()
        self.t.cleanup()

    def _config(self, convert=True):
        """Real config with the DVR dir pointed at the sandbox and everything
        do_postprocess touches except conversion turned off."""
        return cfgmod._deep_merge(cfgmod.load_config(), {'recording': {
            'dvr_output_dir': self.dvr_dir,
            'gather_health_data': False,
            'serialize_concat': False,
            'move_on_complete': {'enabled': False},
            'post_script': {'enabled': False},
            'post_process': {'enabled': convert, 'format': 'mkv', 'delete_source': False,
                             'reencode_mode': 'never', 'pre_output_timeout_seconds': 60,
                             'auto_restart': False, 'max_restart_attempts': 0,
                             'stall_seconds': 0, 'progress_interval_seconds': 5},
        }})

    def _run_concat(self, cfg, conversion=None):
        """Drive the real pipeline. `conversion` is the stubbed ConversionResult the
        supervised runner returns (None = post-processing disabled entirely)."""
        stub = mock.Mock(return_value=conversion)
        with mock.patch.object(cfgmod, 'load_config', return_value=cfg), \
             mock.patch.object(ppmod, 'run_conversion_supervised', stub):
            do_concatenation(self.t.app, self.rid)
        db.session.expire_all()
        return stub

    def _row(self):
        return db.session.get(Recording, self.rid), db.session.get(Channel, self.cid)

    def test_conversion_give_up_still_scores_the_capture(self):
        """The reported defect: capture ends clean, mp4 conversion gives up, and the
        stalls/restarts vanish (recording #64 on channel 142011, health_score 100)."""
        self._run_concat(self._config(),
                         conversion=ConversionResult(False, 'died', 'boom'))
        rec, ch = self._row()
        self.assertEqual(rec.status, 'FAILED', 'expected the conversion give-up path')
        self.assertIsNotNone(rec.health_quality_score,
                             'capture-phase quality was discarded by the conversion failure')
        self.assertLess(rec.health_quality_score, 100)
        self.assertLess(ch.health_score, 100.0,
                        'the channel score never saw this capture')
        self.assertEqual(ch.health_score_sample_count, 3)

    def test_concat_failure_still_scores_the_capture(self):
        """Same hole one step earlier: a full disk fails the concat after a clean capture."""
        cfg = self._config(convert=False)
        with mock.patch.object(catmod.shutil, 'disk_usage',
                               return_value=mock.Mock(free=1, total=100, used=99)):
            self._run_concat(cfg)
        rec, ch = self._row()
        self.assertEqual(rec.status, 'FAILED')
        self.assertIsNotNone(rec.health_quality_score,
                             'capture-phase quality was discarded by the disk-space failure')
        self.assertLess(ch.health_score, 100.0)

    def test_clean_run_scores_once_and_retry_does_not_re_blend(self):
        """One capture is one observation: a Retry conversion must not blend the same
        stalls into the channel score a second time."""
        cfg = self._config(convert=False)
        self._run_concat(cfg)
        rec, ch = self._row()
        self.assertEqual(rec.status, 'COMPLETED')
        first_score = ch.health_score
        self.assertEqual(ch.health_score_sample_count, 3)

        ts_path = rec.output_path
        with mock.patch.object(cfgmod, 'load_config', return_value=cfg):
            do_postprocess(self.t.app, self.rid, ts_path)
        db.session.expire_all()
        _, ch = self._row()
        self.assertEqual(ch.health_score_sample_count, 3,
                         'a retried post-process double-counted the same capture')
        self.assertEqual(ch.health_score, first_score)

    def test_a_capture_that_recorded_nothing_applies_the_fail_floor(self):
        """The one no-segments path that is genuinely channel-attributable keeps its fail
        floor - the capture-phase observation must not pre-empt it with a metrics score.

        bytes_recorded is what makes it that path. This test used to prove the point by
        deleting the segment file while leaving 4096 recorded bytes on the row, which is
        the *other* shape entirely - a capture that worked, whose files went missing
        afterwards - and blaming the feed for it cost a healthy channel 100 -> 22
        (dev/docs/BUGS.md 2026-08-24). The two are separated here.
        """
        os.unlink(self.seg_path)
        seg = RecordingSegment.query.filter_by(recording_id=self.rid).one()
        seg.bytes_recorded = 0
        db.session.commit()

        self._run_concat(self._config(convert=False))
        rec, ch = self._row()
        self.assertEqual(rec.status, 'FAILED')
        self.assertEqual(rec.health_quality_score, 5, 'expected the recording fail floor')
        self.assertLess(ch.health_score, 100.0)

    def test_segments_lost_after_a_real_capture_do_not_reach_the_channel(self):
        """Same branch, opposite attribution: the row records bytes the capture really
        pulled down, so the missing files are local and say nothing about the feed."""
        os.unlink(self.seg_path)
        self._run_concat(self._config(convert=False))
        rec, ch = self._row()
        self.assertEqual(rec.status, 'FAILED')
        self.assertEqual(ch.health_score, 100.0,
                         'a local post-capture failure must not move the channel score')


if __name__ == '__main__':
    unittest.main()
