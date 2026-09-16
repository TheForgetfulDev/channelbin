"""Every FAILED recording names why, and the detail page says the reason it names.

Guards dev/docs/BUGS.md 2026-09-16 @ 06:33:44 AM ET. Recording.failure_reason
was written only by the watchdog, so a recording failed anywhere else - a slot wait or a
conversion wait that outlasted its window, an unusable DVR directory, a window missed while
the service was down, a source .ts gone at restart, every join and conversion give-up -
reached the FAILED strip with the column NULL and fell through to "the stream could not be
reached before producing any segments". And a join whose every segment was the provider's
placeholder kept its files on disk, so the page read "the capture recorded data, but it was
never joined" and promoted Retry join, which refuses the same clips again.

Covers, in order:
  - *WriterTests: each FAILED writer outside the watchdog stores its own reason (the
    watchdog's three are covered by test_dead_stream_retry / test_fast_delivery_detection).
  - RetryClearsReasonTests: a Retry that takes a row out of FAILED clears the column.
  - StripTests: each reason renders its own sentence and the right next step.
  - TemplateCoverageTests: every value in FAILURE_REASONS has a branch in the strip.

No real ffmpeg, no network, nothing under the real /dvr.
  python3 -m unittest tests.test_failure_reason_disclosure
"""
import os
import re
import sys
import time
import unittest
from datetime import datetime, timedelta
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import app.config as cfgmod  # noqa: E402
import app.postprocessor as ppmod  # noqa: E402
from tests.support.app import make_test_app  # noqa: E402
from tests.support import seed  # noqa: E402
from app import db, recorder  # noqa: E402
from app import connection_limits as connlim  # noqa: E402
from app import database as dbmod  # noqa: E402
from app.database import (  # noqa: E402
    Recording, RecordingProfile, RecordingSegment, FAILURE_REASONS,
    REC_STATUS_FAILED, REC_STATUS_SCHEDULED, REC_STATUS_CONCATENATING, REC_STATUS_PAUSED,
    REC_STATUS_CONVERTING, SEGMENT_EXCLUDED_PLACEHOLDER,
    FAILURE_CONVERSION_COLLISION, FAILURE_CONNECTION_SLOT_TIMEOUT, FAILURE_DVR_DIR_UNUSABLE,
    FAILURE_LAUNCH_FAILED, FAILURE_MISSED_AT_STARTUP, FAILURE_SOURCE_MISSING,
    FAILURE_CONVERSION_FAILED, FAILURE_ALL_SEGMENTS_PLACEHOLDER, FAILURE_SEGMENT_FILES_MISSING,
    FAILURE_NO_VALID_SEGMENTS, FAILURE_PAUSED_NOTHING_CAPTURED, FAILURE_INSUFFICIENT_DISK_SPACE,
    FAILURE_CONCAT_ERROR,
)

TEMPLATE = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                        'templates', 'recording_detail.html')


class _SandboxCase(unittest.TestCase):
    start_scheduler = False

    def setUp(self):
        self.t = make_test_app(start_scheduler=self.start_scheduler)
        self.dvr = os.path.join(self.t._tmpdir, 'dvr')
        os.makedirs(self.dvr, exist_ok=True)
        self.t.sandbox_config(self._config())

    def _config(self, **post_process):
        pp = {'enabled': False}
        pp.update(post_process)
        return {'recording': {
            'dvr_output_dir': self.dvr,
            'capture_log_dir': os.path.join(self.t._tmpdir, 'caplogs'),
            'live_thumbnail': {'enabled': False},
            'serialize_concat': False,
            'gather_health_data': False,
            'move_on_complete': {'enabled': False},
            'post_script': {'enabled': False},
            'post_process': pp,
        }}

    def tearDown(self):
        connlim._holders.clear()
        self.t.cleanup()

    def _reason(self, rid):
        db.session.expire_all()
        rec = db.session.get(Recording, rid)
        self.assertEqual(REC_STATUS_FAILED, rec.status, 'setup: expected the FAILED path')
        return rec.failure_reason


class StartWriterTests(_SandboxCase):
    """The writers that fail a recording before it ever captured."""

    def _scheduled(self, *, past=False, channel_id=None, **kw):
        now = datetime.utcnow()
        start = now - timedelta(hours=2) if past else now - timedelta(minutes=1)
        rec = seed.make_recording(status=REC_STATUS_SCHEDULED, name='writer',
                                  channel_id=channel_id, start_time=start,
                                  stop_time=start + timedelta(hours=1), **kw)
        db.session.commit()
        return rec.id

    def test_conversion_collision(self):
        self.t.sandbox_config(self._config(collision_policy='wait'))
        rid = self._scheduled(past=True)
        with mock.patch.object(ppmod, 'has_active_conversion', return_value=True):
            recorder.start_recording(self.t.app, rid)
        self.assertEqual(FAILURE_CONVERSION_COLLISION, self._reason(rid))

    def test_connection_slot_timeout(self):
        acc = seed.make_account(name='One Slot', max_connections=1)
        ch = seed.make_channel(acc, name='Only Feed')
        db.session.commit()
        connlim._holders.clear()
        self.assertTrue(connlim.try_acquire(acc.id, 'recording', 999999))
        rid = self._scheduled(past=True, channel_id=ch.id)
        with mock.patch('app.scheduler.reschedule_recording_start'), \
             mock.patch.object(recorder, '_launch_segment'):
            recorder.start_recording(self.t.app, rid)
        self.assertEqual(FAILURE_CONNECTION_SLOT_TIMEOUT, self._reason(rid))

    def test_dvr_dir_unusable(self):
        cfg = self._config()
        cfg['recording']['dvr_output_dir'] = os.path.join(self.t._tmpdir, 'never-made')
        self.t.sandbox_config(cfg)
        rid = self._scheduled()
        recorder.start_recording(self.t.app, rid)
        self.assertEqual(FAILURE_DVR_DIR_UNUSABLE, self._reason(rid))

    def test_launch_failed(self):
        profile = RecordingProfile(name='one-shot', restart_delay_seconds=0,
                                   max_consecutive_failures=1)
        db.session.add(profile)
        db.session.flush()
        rid = self._scheduled(profile_id=profile.id)
        with mock.patch.object(recorder.subprocess, 'Popen',
                               side_effect=OSError('ffmpeg not found')):
            recorder.start_recording(self.t.app, rid)
        self.assertEqual(FAILURE_LAUNCH_FAILED, self._reason(rid))


class StartupSweepWriterTests(_SandboxCase):
    start_scheduler = True

    def _sweep(self):
        from app.scheduler import resume_in_progress_recordings
        resume_in_progress_recordings(self.t.app)

    def test_missed_at_startup(self):
        start = datetime.utcnow() - timedelta(hours=3)
        rec = seed.make_recording(status=REC_STATUS_SCHEDULED, name='missed',
                                  start_time=start, stop_time=start + timedelta(hours=1))
        db.session.commit()
        self._sweep()
        self.assertEqual(FAILURE_MISSED_AT_STARTUP, self._reason(rec.id))

    def test_source_missing(self):
        rec = seed.make_recording(status=REC_STATUS_CONVERTING, name='gone',
                                  output_path=os.path.join(self.dvr, 'gone.ts'))
        db.session.commit()
        self._sweep()
        self.assertEqual(FAILURE_SOURCE_MISSING, self._reason(rec.id))

    def test_conversion_budget_exhausted_at_restart(self):
        self.t.sandbox_config(self._config(enabled=True, auto_restart=True,
                                           max_restart_attempts=1))
        ts = os.path.join(self.dvr, 'show.ts')
        with open(ts, 'wb') as fh:
            fh.write(b'x' * 64)
        rec = seed.make_recording(status=REC_STATUS_CONVERTING, name='spent',
                                  output_path=ts, conversion_attempts=1)
        db.session.commit()
        self._sweep()
        self.assertEqual(FAILURE_CONVERSION_FAILED, self._reason(rec.id))


class JoinWriterTests(_SandboxCase):
    """The join's four FAILED branches, driven through the real do_concatenation."""

    def _recording(self, status=REC_STATUS_CONCATENATING, segments=()):
        now = datetime.utcnow()
        rec = seed.make_recording(status=status, name='join',
                                  start_time=now - timedelta(hours=1), stop_time=now)
        for n, (size_on_disk, bytes_recorded, excluded) in enumerate(segments, start=1):
            path = os.path.join(self.dvr, f'rec_{rec.id}_seg_{n:03d}.ts')
            if size_on_disk is not None:
                with open(path, 'wb') as fh:
                    fh.write(b'x' * size_on_disk)
            db.session.add(RecordingSegment(
                recording_id=rec.id, segment_number=n, file_path=path,
                started_at=rec.start_time, ended_at=rec.stop_time,
                exit_reason='STOP_TIME_REACHED', bytes_recorded=bytes_recorded,
                excluded_reason=SEGMENT_EXCLUDED_PLACEHOLDER if excluded else None))
        db.session.commit()
        return rec.id

    def _join(self, rid):
        from app.concatenator import do_concatenation
        do_concatenation(self.t.app, rid)

    def test_every_segment_a_placeholder(self):
        rid = self._recording(segments=[(4096, 4096, True), (4096, 4096, True)])
        self._join(rid)
        self.assertEqual(FAILURE_ALL_SEGMENTS_PLACEHOLDER, self._reason(rid))

    def test_segment_files_missing_after_capture(self):
        rid = self._recording(segments=[(None, 4096, False)])
        self._join(rid)
        self.assertEqual(FAILURE_SEGMENT_FILES_MISSING, self._reason(rid))

    def test_nothing_captured(self):
        rid = self._recording(segments=[(0, 0, False)])
        self._join(rid)
        self.assertEqual(FAILURE_NO_VALID_SEGMENTS, self._reason(rid))

    def test_paused_before_anything_was_captured(self):
        rid = self._recording(status=REC_STATUS_PAUSED, segments=[(0, 0, False)])
        self._join(rid)
        self.assertEqual(FAILURE_PAUSED_NOTHING_CAPTURED, self._reason(rid))

    def test_insufficient_disk_space(self):
        rid = self._recording(segments=[(4096, 4096, False)])
        with mock.patch('app.concatenator.shutil.disk_usage',
                        return_value=mock.Mock(free=1, total=100, used=99)):
            self._join(rid)
        self.assertEqual(FAILURE_INSUFFICIENT_DISK_SPACE, self._reason(rid))

    def test_concat_error(self):
        rid = self._recording(segments=[(4096, 4096, False)])
        with mock.patch('app.concatenator.os.rename', side_effect=OSError('boom')):
            self._join(rid)
        self.assertEqual(FAILURE_CONCAT_ERROR, self._reason(rid))


class ConversionWriterTests(_SandboxCase):
    def test_conversion_give_up(self):
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
             mock.patch.object(ppmod, 'run_conversion_supervised', stub):
            ppmod.do_postprocess(self.t.app, rec.id, ts)
        self.assertEqual(FAILURE_CONVERSION_FAILED, self._reason(rec.id))


class RetryClearsReasonTests(_SandboxCase):
    def setUp(self):
        super().setUp()
        self.t.app.config['WTF_CSRF_ENABLED'] = False

    def test_retry_join_clears_the_reason(self):
        seg = os.path.join(self.dvr, 'seg_001.ts')
        with open(seg, 'wb') as fh:
            fh.write(b'x' * 4096)
        rec = seed.make_recording(status=REC_STATUS_FAILED, name='retry join',
                                  failure_reason=FAILURE_CONCAT_ERROR)
        db.session.add(RecordingSegment(recording_id=rec.id, segment_number=1, file_path=seg,
                                        started_at=rec.start_time, bytes_recorded=4096))
        db.session.commit()
        with mock.patch('app.concatenator.do_concatenation'):
            self.t.client.post(f'/recordings/{rec.id}/retry-concat')
            time.sleep(0.05)
        db.session.expire_all()
        rec = db.session.get(Recording, rec.id)
        self.assertEqual(REC_STATUS_CONCATENATING, rec.status)
        self.assertIsNone(rec.failure_reason)

    def test_retry_conversion_clears_the_reason(self):
        ts = os.path.join(self.dvr, 'show.ts')
        with open(ts, 'wb') as fh:
            fh.write(b'x' * 64)
        rec = seed.make_recording(status=REC_STATUS_FAILED, name='retry convert',
                                  output_path=ts, failure_reason=FAILURE_CONVERSION_FAILED)
        db.session.commit()
        with mock.patch('app.concatenator.run_postprocess_claimed'):
            self.t.client.post(f'/recordings/{rec.id}/retry-convert')
            time.sleep(0.05)
        db.session.expire_all()
        self.assertIsNone(db.session.get(Recording, rec.id).failure_reason)


class StripTests(unittest.TestCase):
    """What the FAILED strip and its actions say for each reason."""

    def setUp(self):
        self.t = make_test_app()

    def tearDown(self):
        self.t.cleanup()

    def _page(self, reason, *, ts=False, segment_file=False):
        rec = seed.make_recording(status=REC_STATUS_FAILED, name=f'r {reason}',
                                  failure_reason=reason, consecutive_failures_peak=3)
        if ts:
            out = os.path.join(self.t._tmpdir, f'{rec.id}.ts')
            with open(out, 'wb') as fh:
                fh.write(b'x' * 64)
            rec.output_path = out
        if segment_file:
            seg = os.path.join(self.t._tmpdir, f'{rec.id}_seg_001.ts')
            with open(seg, 'wb') as fh:
                fh.write(b'x' * 64)
            db.session.add(RecordingSegment(recording_id=rec.id, segment_number=1,
                                            file_path=seg, started_at=rec.start_time,
                                            bytes_recorded=64,
                                            excluded_reason=(SEGMENT_EXCLUDED_PLACEHOLDER
                                                             if reason == FAILURE_ALL_SEGMENTS_PLACEHOLDER
                                                             else None)))
        db.session.commit()
        resp = self.t.client.get(f'/recordings/{rec.id}')
        self.assertEqual(200, resp.status_code)
        html = resp.get_data(as_text=True)
        strip = re.search(r'<div class="live-strip fail-strip">(.*?)</div>', html, re.S)
        self.assertIsNotNone(strip, 'no FAILED strip rendered')
        return html, re.sub(r'<[^>]+>', '', strip.group(1))

    def _assert_names(self, reason, phrase, cta, **kw):
        html, strip = self._page(reason, **kw)
        self.assertIn(phrase, strip)
        self.assertIn(cta, strip)
        self.assertNotIn('could not be reached', strip)
        self.assertNotIn('no failure reason was recorded', strip)
        return html, strip

    def test_conversion_collision(self):
        self._assert_names(FAILURE_CONVERSION_COLLISION, 'waited for a running mp4 conversion',
                           'nothing was captured - use Record again')

    def test_connection_slot_timeout(self):
        self._assert_names(FAILURE_CONNECTION_SLOT_TIMEOUT, 'no free connection slot',
                           'nothing was captured - use Record again')

    def test_dvr_dir_unusable(self):
        self._assert_names(FAILURE_DVR_DIR_UNUSABLE, 'DVR output directory could not be used',
                           'nothing was captured - use Record again')

    def test_missed_at_startup(self):
        self._assert_names(FAILURE_MISSED_AT_STARTUP, 'service was not running during its window',
                           'nothing was captured - use Record again')

    def test_launch_failed(self):
        self._assert_names(FAILURE_LAUNCH_FAILED, 'ffmpeg itself could not be started',
                           'use Record again')

    def test_no_valid_segments(self):
        self._assert_names(FAILURE_NO_VALID_SEGMENTS, 'never delivered any data',
                           'nothing was captured - use Record again')

    def test_paused_nothing_captured(self):
        self._assert_names(FAILURE_PAUSED_NOTHING_CAPTURED, 'paused before the capture',
                           'nothing was captured - use Record again')

    def test_segment_files_missing(self):
        self._assert_names(FAILURE_SEGMENT_FILES_MISSING, 'went missing',
                           'nothing recoverable is left on disk')

    def test_source_missing(self):
        self._assert_names(FAILURE_SOURCE_MISSING, '.ts was gone',
                           'nothing recoverable is left on disk')

    def test_insufficient_disk_space_offers_retry_join(self):
        html, _ = self._assert_names(FAILURE_INSUFFICIENT_DISK_SPACE, 'not enough free disk space',
                                     'use Retry join', segment_file=True)
        self.assertIn('class="btn btn-primary" data-act="retry-concat"', html)

    def test_concat_error_offers_retry_join(self):
        html, _ = self._assert_names(FAILURE_CONCAT_ERROR, 'joining its segments failed',
                                     'use Retry join', segment_file=True)
        self.assertIn('class="btn btn-primary" data-act="retry-concat"', html)

    def test_conversion_failed_offers_retry_conversion(self):
        html, _ = self._assert_names(FAILURE_CONVERSION_FAILED, 'the conversion gave up',
                                     'use Retry conversion', ts=True)
        self.assertIn('class="btn btn-primary" data-act="retry-convert"', html)

    def test_an_all_placeholder_join_offers_record_again_not_retry_join(self):
        """The files are on disk, and a Retry join would refuse them for the same reason."""
        html, strip = self._assert_names(FAILURE_ALL_SEGMENTS_PLACEHOLDER, 'placeholder clip',
                                         'use Record again', segment_file=True)
        self.assertNotIn('Retry join', strip)
        self.assertNotIn('data-act="retry-concat"', html,
                         'no surface - header, kebab or mobile bar - may offer a join that '
                         'will refuse the same clips again')

    def test_max_consecutive_failures_keeps_its_sentence(self):
        self._assert_names(dbmod.FAILURE_MAX_CONSECUTIVE_FAILURES,
                           '3 consecutive restart attempts', 'use Record again')


class TemplateCoverageTests(unittest.TestCase):
    def test_every_reason_has_a_branch_in_the_failed_strip(self):
        """A reason with no branch would land in the strip's fallback sentence - the defect
        this vocabulary exists to end."""
        with open(TEMPLATE, encoding='utf-8') as fh:
            src = fh.read()
        missing = [r for r in FAILURE_REASONS
                   if f"rec.failure_reason == '{r}'" not in src]
        self.assertEqual([], missing)

    def test_the_vocabulary_names_every_constant(self):
        declared = {v for k, v in vars(dbmod).items() if k.startswith('FAILURE_')
                    and isinstance(v, str)}
        self.assertEqual(declared, set(FAILURE_REASONS))


if __name__ == '__main__':
    unittest.main()
