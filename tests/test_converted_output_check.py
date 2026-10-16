"""Tier 2 - a finished conversion is checked for missing video before its source may be
deleted, and the source's fate is recorded either way (dev/changelog/1200). Guards
dev/docs/BUGS.md 2026-10-15 @ 05:08:33 PM: "A converted file with two 7-minute video holes
was marked COMPLETED and its source deleted unchecked".

Real ffprobe over small synthesized local files. The conversion itself is stubbed: each
case hands do_postprocess a prepared output - one with a hole, one without - so what is
asserted is the check, the keep-or-delete decision and what the recording says about it.
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from datetime import datetime, timedelta
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import app.config as cfgmod  # noqa: E402
import app.postprocessor as ppmod  # noqa: E402
from app import db, toolchain  # noqa: E402
from app.database import (  # noqa: E402
    Alert, Recording, RecordingEvent, RecordingSegment, DIAGNOSTICS, CONVERSION_DONE,
    CONVERTED_FILE_GAPS, SOURCE_DELETED, SOURCE_KEPT, add_recording_event,
)
from app.postprocessor import ConversionResult, do_postprocess  # noqa: E402
from app.probe import scan_video_timeline  # noqa: E402
from tests.support import seed  # noqa: E402
from tests.support.app import make_test_app  # noqa: E402

_HAVE_FFMPEG = bool(shutil.which('ffmpeg') and shutil.which('ffprobe'))


def _clip(path, drop=None, fmt=None):
    """20s of testsrc2 + tone. `drop` is a select expression's list of (from, to) spans whose
    frames are removed with their timestamps kept, which is what a hole in the video is."""
    vf = []
    if drop:
        expr = '+'.join(f'between(t\\,{a}\\,{b})' for a, b in drop)
        vf = ['-vf', f'select=not({expr})', '-fps_mode', 'passthrough']
    subprocess.run(['ffmpeg', '-hide_banner', '-loglevel', 'error', '-y',
                    '-f', 'lavfi', '-i', 'testsrc2=size=320x240:rate=30000/1001:d=20',
                    '-f', 'lavfi', '-i', 'sine=f=440:sample_rate=48000:d=20', *vf,
                    '-c:v', 'libx264', '-preset', 'ultrafast', '-g', '15',
                    '-pix_fmt', 'yuv420p', '-c:a', 'aac', '-b:a', '96k',
                    *(['-f', fmt] if fmt else []), path],
                   check=True, timeout=120)


def _config(**pp_overrides):
    # duplicated from tests/test_conversion_gap_fill.py - a test module stays self-contained
    pp = dict({'enabled': True, 'format': 'mp4', 'delete_source': True,
               'reencode_mode': 'always', 'pre_output_timeout_seconds': 60,
               'auto_restart': True, 'max_restart_attempts': 3, 'stall_seconds': 0,
               'progress_interval_seconds': 5, 'video_encoder': 'software',
               'video_codec': 'h264', 'max_height': 0, 'audio_channels': 'source',
               'loudness_normalize': False, 'vaapi_decode': False}, **pp_overrides)
    return cfgmod._deep_merge(cfgmod.load_config(), {'recording': {
        'gather_health_data': False,
        'move_on_complete': {'enabled': False},
        'post_script': {'enabled': False},
        'post_process': pp,
    }})


@unittest.skipUnless(_HAVE_FFMPEG, 'needs ffmpeg and ffprobe on PATH')
class _OutputCheckCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.src = tempfile.mkdtemp(prefix='outcheck-src-')
        d = cls.src
        _clip(os.path.join(d, 'clean.mp4'))
        _clip(os.path.join(d, 'hole.mp4'), drop=[(5, 13)])
        _clip(os.path.join(d, 'clean.ts'), fmt='mpegts')
        _clip(os.path.join(d, 'hole.ts'), drop=[(5, 13)], fmt='mpegts')
        _clip(os.path.join(d, 'three.mp4'), drop=[(2, 3), (6, 9), (12, 14)])

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.src, ignore_errors=True)

    def setUp(self):
        toolchain.reset_cache()
        self.t = make_test_app()
        now = datetime.utcnow()
        self.rec = seed.make_recording(status='CONCATENATING', name='outcheck',
                                       start_time=now - timedelta(seconds=20), stop_time=now,
                                       recorded_duration_seconds=20.0)
        self.ts = os.path.join(self.t._tmpdir, f'rec_{self.rec.id}.ts')
        self.rec.output_path = self.ts
        db.session.commit()
        self.rid = self.rec.id
        self.out = os.path.splitext(self.ts)[0] + '.mp4'

    def tearDown(self):
        with ppmod._active_lock:
            ppmod._active_conversions.clear()
            ppmod._cancel_requested.clear()
        toolchain.reset_cache()
        self.t.cleanup()

    def _source(self, name):
        shutil.copyfile(os.path.join(self.src, name), self.ts)
        start = datetime.utcnow()
        db.session.add(RecordingSegment(
            recording_id=self.rid, segment_number=1,
            file_path=os.path.join(self.t._tmpdir, 'seg_1.ts'),
            started_at=start, ended_at=start + timedelta(seconds=20),
            exit_reason='COMPLETED', bytes_recorded=4096, probe_resolution='320x240',
            probe_fps=29.97, probe_audio_channels=1, probe_interlaced=False,
            content_duration_seconds=20.0))
        db.session.commit()

    def _convert(self, cfg, produced, check=None):
        """Run do_postprocess with the encode stubbed to produce `produced` (a fixture name,
        or raw bytes for an unreadable output)."""
        def write(path):
            if isinstance(produced, bytes):
                with open(path, 'wb') as fh:
                    fh.write(produced)
            else:
                shutil.copyfile(os.path.join(self.src, produced), path)

        def attempt(app, rid, cmd, part_file, **kw):
            write(part_file)
            return ConversionResult(True, 'success', out_time=20.0)

        def join(parts, output_path, *a, **k):
            write(output_path)
            return ppmod.PartJoinResult(True, 'success', parts=len(parts))

        patches = [mock.patch.object(cfgmod, 'load_config', return_value=cfg),
                   mock.patch.object(ppmod, 'run_conversion_supervised', attempt),
                   mock.patch.object(ppmod, 'join_conversion_parts', join),
                   mock.patch.object(ppmod, 'finalize_part', lambda part, *a, **k: 0.0)]
        if check is not None:
            patches.append(mock.patch.object(ppmod, 'check_converted_output', check))
        for p in patches:
            p.start()
        try:
            do_postprocess(self.t.app, self.rid, self.ts)
        finally:
            for p in reversed(patches):
                p.stop()
        db.session.expire_all()
        return db.session.get(Recording, self.rid)

    def _events(self, event_type):
        return (RecordingEvent.query.filter_by(recording_id=self.rid, event_type=event_type)
                .order_by(RecordingEvent.id).all())

    def _extra(self, evt):
        return json.loads(evt.extra_data or '{}')

    def _alerts(self):
        return Alert.query.filter_by(recording_id=self.rid,
                                     alert_type='CONVERTED_FILE_GAPS').all()


class ReencodeOutputTests(_OutputCheckCase):
    def test_a_hole_the_converter_made_keeps_the_source_and_says_where(self):
        """Recording 16's shape: a re-encode whose output is missing video. The source must
        survive delete_source, the recording must still complete, and the hole must be named
        with its times on the recording, in a column, and in an alert."""
        self._source('clean.ts')
        rec = self._convert(_config(), 'hole.mp4')
        self.assertEqual(rec.status, 'COMPLETED')
        self.assertTrue(os.path.exists(self.ts), 'the only copy of the video was deleted')
        self.assertAlmostEqual(rec.converted_gap_seconds, 8.0, delta=0.2)
        gaps = self._events(CONVERTED_FILE_GAPS)
        self.assertEqual(len(gaps), 1)
        (start, end), = self._extra(gaps[0])['gaps']
        self.assertAlmostEqual(start, 5.0, delta=0.1)
        self.assertAlmostEqual(end, 13.0, delta=0.1)
        self.assertIn('4s to 13s', gaps[0].detail)
        kept = self._events(SOURCE_KEPT)
        self.assertEqual(len(kept), 1)
        self.assertEqual(self._extra(kept[0]),
                         {'path': self.ts, 'reason': 'converted_file_gaps'})
        self.assertEqual(self._events(SOURCE_DELETED), [])
        alerts = self._alerts()
        self.assertEqual(len(alerts), 1)
        self.assertEqual(alerts[0].severity, 'WARN')

    def test_a_clean_output_deletes_the_source_and_says_so(self):
        self._source('clean.ts')
        rec = self._convert(_config(), 'clean.mp4')
        self.assertEqual(rec.status, 'COMPLETED')
        self.assertFalse(os.path.exists(self.ts))
        self.assertEqual(rec.converted_gap_seconds, 0.0)
        deleted = self._events(SOURCE_DELETED)
        self.assertEqual(len(deleted), 1)
        self.assertEqual(self._extra(deleted[0]), {'path': self.ts})
        self.assertEqual(self._events(CONVERTED_FILE_GAPS), [])
        self.assertEqual(self._events(SOURCE_KEPT), [])
        self.assertEqual(self._alerts(), [])
        scans = [e for e in self._events(DIAGNOSTICS)
                 if self._extra(e).get('kind') == 'output_timeline_scan']
        self.assertEqual(len(scans), 1, 'a clean check says it ran')
        self.assertIn('no video gaps', scans[0].detail)

    def test_a_clean_reconversion_dismisses_the_open_gaps_alert(self):
        from app import alerts
        self._source('clean.ts')
        alerts.create_alert('CONVERTED_FILE_GAPS', 'Converted file has gaps: outcheck',
                            body='x', source='postprocessor', recording_id=self.rid)
        self._convert(_config(), 'clean.mp4')
        self.assertTrue(all(a.dismissed_at is not None for a in self._alerts()))

    def test_delete_source_off_keeps_it_and_names_the_setting(self):
        self._source('clean.ts')
        self._convert(_config(delete_source=False), 'clean.mp4')
        self.assertTrue(os.path.exists(self.ts))
        kept = self._events(SOURCE_KEPT)
        self.assertEqual(self._extra(kept[0])['reason'], 'delete_source_off')

    def test_an_output_that_cannot_be_read_keeps_the_source(self):
        """Unchecked is not clean: the source is kept and the column says not-checked."""
        self._source('clean.ts')
        rec = self._convert(_config(), b'not a video' * 20)
        self.assertTrue(os.path.exists(self.ts))
        self.assertIsNone(rec.converted_gap_seconds)
        kept = self._events(SOURCE_KEPT)
        self.assertEqual(self._extra(kept[0])['reason'], 'converted_file_unchecked')
        scans = [e for e in self._events(DIAGNOSTICS)
                 if self._extra(e).get('kind') == 'output_timeline_scan']
        self.assertTrue(self._extra(scans[0]).get('scan_failed'))

    def test_the_check_runs_before_the_row_names_the_output(self):
        """A restart during the scan must find a row that still names its .ts and has no
        CONVERSION_DONE, so startup recovery resumes the conversion rather than treating
        an unchecked file as finished."""
        self._source('clean.ts')
        seen = {}
        real = ppmod.check_converted_output

        def spy(*a, **k):
            with self.t.app.app_context():
                r = db.session.get(Recording, self.rid)
                seen['output_path'] = r.output_path
                seen['done'] = RecordingEvent.query.filter_by(
                    recording_id=self.rid, event_type=CONVERSION_DONE).count()
            return real(*a, **k)

        self._convert(_config(), 'clean.mp4', check=spy)
        self.assertEqual(seen, {'output_path': self.ts, 'done': 0})


class StreamCopyOutputTests(_OutputCheckCase):
    def test_a_copy_keeps_its_sources_gap_and_is_not_blamed_for_it(self):
        """A stream copy carries the source's own gap through. That is not the converter's
        loss, so the source is deleted as configured - and the source scan runs even with
        health data off, because the comparison needs it."""
        self._source('hole.ts')
        rec = self._convert(_config(reencode_mode='never'), 'hole.mp4')
        self.assertEqual(rec.converted_gap_seconds, 0.0)
        self.assertFalse(os.path.exists(self.ts))
        self.assertEqual(self._events(CONVERTED_FILE_GAPS), [])
        self.assertAlmostEqual(rec.timeline_gap_seconds, 8.0, delta=0.2)

    def test_a_copy_missing_more_than_its_source_is_flagged(self):
        self._source('clean.ts')
        rec = self._convert(_config(reencode_mode='never'), 'hole.mp4')
        self.assertAlmostEqual(rec.converted_gap_seconds, 8.0, delta=0.2)
        self.assertTrue(os.path.exists(self.ts))
        gaps = self._events(CONVERTED_FILE_GAPS)
        self.assertEqual(len(self._extra(gaps[0])['gaps']), 1)


@unittest.skipUnless(_HAVE_FFMPEG, 'needs ffmpeg and ffprobe on PATH')
class ScanGapListTests(_OutputCheckCase):
    def setUp(self):
        pass

    def tearDown(self):
        pass

    def test_gaps_are_listed_with_their_positions_largest_first_kept(self):
        m = scan_video_timeline(os.path.join(self.src, 'three.mp4'), max_gaps_listed=2)
        self.assertEqual(m['gap_count'], 3, 'the count is not capped')
        self.assertEqual(len(m['gaps']), 2)
        (a1, b1), (a2, b2) = m['gaps']
        self.assertLess(a1, a2, 'in file order')
        self.assertAlmostEqual(b1 - a1, 3.0, delta=0.1)
        self.assertAlmostEqual(b2 - a2, 2.0, delta=0.1)
        self.assertAlmostEqual(a1, 6.0, delta=0.1)


class RetryFromKeptSourceTests(unittest.TestCase):
    def setUp(self):
        self.t = make_test_app()
        self.t.app.config['WTF_CSRF_ENABLED'] = False
        self.ts = os.path.join(self.t._tmpdir, 'show.ts')
        self.mp4 = os.path.join(self.t._tmpdir, 'done', 'show.mp4')
        os.makedirs(os.path.dirname(self.mp4))
        for p in (self.ts, self.mp4):
            with open(p, 'wb') as fh:
                fh.write(b'x' * 16)
        rec = seed.make_recording(status='COMPLETED', name='show', output_path=self.mp4,
                                  converted_gap_seconds=885.0)
        self.rid = rec.id
        add_recording_event(self.rid, SOURCE_KEPT, detail='kept',
                            extra={'path': self.ts, 'reason': 'converted_file_gaps'})
        db.session.commit()

    def tearDown(self):
        self.t.cleanup()

    def _retry(self):
        with mock.patch('app.concatenator.run_postprocess_claimed') as fake:
            resp = self.t.client.post(f'/recordings/{self.rid}/retry-convert')
            deadline = time.monotonic() + 2.0
            while time.monotonic() < deadline and not fake.called:
                time.sleep(0.01)
        db.session.expire_all()
        return resp, fake

    def test_retry_converts_again_from_the_kept_source(self):
        resp, fake = self._retry()
        self.assertEqual(resp.status_code, 302)
        self.assertTrue(fake.called, 'a COMPLETED recording with gaps could not be retried')
        self.assertEqual(fake.call_args[0][2], self.ts)
        rec = db.session.get(Recording, self.rid)
        self.assertEqual(rec.status, 'ANALYZING')
        self.assertEqual(rec.output_path, self.ts)
        self.assertIsNone(rec.converted_gap_seconds)
        self.assertFalse(os.path.exists(self.mp4), 'the file with the gaps was left to collide')
        self.assertTrue(os.path.exists(self.ts))

    def test_retry_is_refused_on_a_clean_completed_recording(self):
        rec = db.session.get(Recording, self.rid)
        rec.converted_gap_seconds = 0.0
        db.session.commit()
        resp, fake = self._retry()
        self.assertFalse(fake.called)
        self.assertTrue(os.path.exists(self.mp4))

    def test_retry_is_refused_when_the_kept_source_is_gone(self):
        os.unlink(self.ts)
        resp, fake = self._retry()
        self.assertFalse(fake.called)
        self.assertTrue(os.path.exists(self.mp4), 'deleted the only file left')

    def test_the_detail_page_offers_the_retry_and_names_the_loss(self):
        body = self.t.client.get(f'/recordings/{self.rid}').get_data(as_text=True)
        self.assertIn('data-act="retry-convert-gaps"', body)
        self.assertIn('gaps-note', body)

    def test_the_recordings_list_badges_the_row(self):
        body = self.t.client.get('/recordings').get_data(as_text=True)
        self.assertIn('Gaps in file', body)


class ReconcileGapsAlertTests(unittest.TestCase):
    def setUp(self):
        self.t = make_test_app()

    def tearDown(self):
        self.t.cleanup()

    def test_startup_dismisses_a_gaps_alert_once_it_is_over(self):
        """Over means a later conversion checked clean, or the kept source is gone - deleting
        it by hand is how the damaged file is accepted, since an Active alert has no
        Dismiss. A recording still holding its source keeps the alert."""
        from app import alerts
        kept = os.path.join(self.t._tmpdir, 'kept.ts')
        with open(kept, 'wb') as fh:
            fh.write(b'x')
        clean = seed.make_recording(status='COMPLETED', name='a', converted_gap_seconds=0.0)
        holed = seed.make_recording(status='COMPLETED', name='b', converted_gap_seconds=9.0)
        gone = seed.make_recording(status='COMPLETED', name='c', converted_gap_seconds=9.0)
        add_recording_event(holed.id, SOURCE_KEPT, detail='kept',
                            extra={'path': kept, 'reason': 'converted_file_gaps'})
        add_recording_event(gone.id, SOURCE_KEPT, detail='kept',
                            extra={'path': kept + '.deleted', 'reason': 'converted_file_gaps'})
        db.session.commit()
        for r in (clean, holed, gone):
            alerts.create_alert('CONVERTED_FILE_GAPS', f'gaps {r.name}', body='x',
                                source='postprocessor', recording_id=r.id)
        ppmod.reconcile_failure_alerts()
        db.session.expire_all()
        still_open = {a.recording_id for a in Alert.query.filter_by(
            alert_type='CONVERTED_FILE_GAPS').all() if a.dismissed_at is None}
        self.assertEqual(still_open, {holed.id})

if __name__ == '__main__':
    unittest.main()
