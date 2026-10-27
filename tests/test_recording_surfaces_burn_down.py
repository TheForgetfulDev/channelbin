"""Fourteen Recording items closed in one pass (dev/changelog/1247), each guarded here.

Every class names the dev/docs/BUGS.md 2026-10-20 entry it guards. Where a fix is a
template or wording change the test reads the rendered page; where it is a counter or a
recorded fact it drives the real code path.

  * FileMoveFailedTests        a failed move-on-complete is FILE_MOVE_FAILED, rendered bad,
                                and migration 97 retypes the old rows
  * EventLogClassTests         the seven unclassed event types and the two timeline marks
  * DashboardPostCaptureTests  a JOINING/ANALYZING row shows its phase, not capture clocks
  * ParkedLabelTests           the channel page names a parked row WAITING like the others
  * FinishingWordTests         "finishing" is the umbrella word, "converting" one phase
  * BitrateBasisTests          every rate display names what it divides by
  * CancelledRetryPromotionTests  a cancelled row with a kept .ts promotes Retry conversion
  * NearEmptyKeyTests          the near-empty scan publishes bytes per second as such
  * OutputFormatReprobeTests   the recorded_* format columns describe the converted file
  * ResumeAfterConversionTests a restart after CONVERSION_DONE finishes the recording
  * CaptureGapTests            a zero-byte segment covers none of the window
  * TimebaseRateTests          a 90000/1 r_frame_rate is no rate
  * RestartFailureCountTests   a restart that produced nothing is one failure, not two

Run:
  python3 -m unittest tests.test_recording_surfaces_burn_down
"""
import json
import os
import re
import sqlite3
import sys
import tempfile
import time
import unittest
from datetime import datetime, timedelta
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import app.config as cfgmod  # noqa: E402
import app.migrations as M  # noqa: E402
import app.postprocessor as ppmod  # noqa: E402
import app.probe as probemod  # noqa: E402
from app import db  # noqa: E402
from app import concatenator as concatmod  # noqa: E402
from app import normalize as nz  # noqa: E402
from app.database import (  # noqa: E402
    Alert, Recording, RecordingEvent, RecordingSegment, add_recording_event,
    CONVERSION_DONE, CONVERSION_FAILED, CONVERSION_STARTED, DIAGNOSTICS, FILE_MOVED,
    FILE_MOVE_FAILED, GROUP_FAILOVER, PRE_CHECK_FAILED, RECORDING_HANDOFF,
    RECORDING_REPOINTED, RECORDING_RESUME_REFUSED, RECORDING_STARTED_LATE,
    RECORDING_URL_RERESOLVED, RESTART_FAILED, SEGMENT_DISCARDED, SOURCE_DELETED,
    STALL_DETECTED, CANCEL_DURING_CONVERSION,
    REC_STATUS_ABORTED, REC_STATUS_ANALYZING, REC_STATUS_COMPLETED, REC_STATUS_CONCATENATING,
    REC_STATUS_CONVERTING, REC_STATUS_IN_PROGRESS,
)
from app.routes.recordings import _index_row  # noqa: E402
from app.tz_utils import get_display_tz  # noqa: E402
from tests.support import seed  # noqa: E402
from tests.support.app import make_test_app  # noqa: E402
from tests.support.watchdog import RestartHarness  # noqa: E402
import app.recorder as recorder  # noqa: E402
from app.watchdog import WatchdogThread  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DETAIL_TEMPLATE = os.path.join(ROOT, 'templates', 'recording_detail.html')


def _types(rid):
    db.session.expire_all()
    return [e.event_type for e in RecordingEvent.query.filter_by(recording_id=rid)
            .order_by(RecordingEvent.id)]


def _diag_kinds(rid):
    db.session.expire_all()
    out = []
    for e in RecordingEvent.query.filter_by(recording_id=rid, event_type=DIAGNOSTICS):
        out.append(json.loads(e.extra_data or '{}'))
    return out


def _template_map(name):
    """The `{% set <name> = {...} %}` literal of the detail template as a dict."""
    with open(DETAIL_TEMPLATE) as fh:
        src = fh.read()
    block = re.search(r'\{%\s*set\s+' + name + r'\s*=\s*\{(.*?)\}\s*%\}', src, re.S).group(1)
    return dict(re.findall(r"'([A-Z_]+)':\s*'([a-z]+)'", block))


class _PostCase(unittest.TestCase):
    """A sandboxed app with post-processing off unless a test turns it on."""

    start_scheduler = False

    def setUp(self):
        self.t = make_test_app(start_scheduler=self.start_scheduler)
        self.dvr = os.path.join(self.t._tmpdir, 'dvr')
        os.makedirs(self.dvr, exist_ok=True)
        self.t.sandbox_config(self._config())

    def tearDown(self):
        with ppmod._active_lock:
            ppmod._active_conversions.clear()
            ppmod._cancel_requested.clear()
        self.t.cleanup()

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

    def _cfg(self, **overrides):
        return cfgmod._deep_merge(cfgmod.load_config(), overrides)


# ── FILE_MOVE_FAILED ────────────────────────────────────────────────────────

class FileMoveFailedTests(_PostCase):
    """A failed move-on-complete gets its own event type (BUGS.md 2026-10-20)."""

    def _finish_with_failing_move(self):
        out = os.path.join(self.dvr, 'show.mp4')
        with open(out, 'wb') as fh:
            fh.write(b'm' * 64)
        rec = seed.make_recording(status=REC_STATUS_CONVERTING, name='mover', output_path=out)
        db.session.commit()
        cfg = self._cfg(recording={'move_on_complete': {
            'enabled': True, 'destination': os.path.join(self.t._tmpdir, 'dest')}})
        with mock.patch.object(ppmod.shutil, 'move', side_effect=OSError('denied')):
            ppmod._finish_postprocess(self.t.app, rec.id, out, cfg,
                                      stop_if_cancelled=lambda detail: False)
        return rec.id

    def test_a_failed_move_is_file_move_failed_and_nothing_else(self):
        rid = self._finish_with_failing_move()
        types = _types(rid)
        self.assertEqual(1, types.count(FILE_MOVE_FAILED), types)
        self.assertNotIn(FILE_MOVED, types, 'FILE_MOVED must mean the file moved')
        self.assertEqual(REC_STATUS_COMPLETED, db.session.get(Recording, rid).status,
                         'a failed move still completes the recording, file in place')
        self.assertTrue(Alert.query.filter_by(alert_type='RECORDING_MOVE_FAILED',
                                              recording_id=rid).first())

    def test_the_event_log_renders_it_as_a_failure(self):
        rid = self._finish_with_failing_move()
        html = self.t.client.get(f'/recordings/{rid}').get_data(as_text=True)
        db.session.expire_all()
        evt = RecordingEvent.query.filter_by(recording_id=rid, event_type=FILE_MOVE_FAILED).one()
        self.assertIn(f'<li class="ev bad" data-ev-id="{evt.id}">', html)

    def test_a_successful_move_is_still_file_moved(self):
        out = os.path.join(self.dvr, 'show.mp4')
        with open(out, 'wb') as fh:
            fh.write(b'm' * 64)
        rec = seed.make_recording(status=REC_STATUS_CONVERTING, name='mover', output_path=out)
        db.session.commit()
        dest = os.path.join(self.t._tmpdir, 'dest')
        cfg = self._cfg(recording={'move_on_complete': {'enabled': True, 'destination': dest}})
        ppmod._finish_postprocess(self.t.app, rec.id, out, cfg,
                                  stop_if_cancelled=lambda detail: False)
        types = _types(rec.id)
        self.assertIn(FILE_MOVED, types)
        self.assertNotIn(FILE_MOVE_FAILED, types)
        self.assertTrue(os.path.exists(os.path.join(dest, 'show.mp4')))


class FileMoveFailedMigrationTests(unittest.TestCase):
    """Migration 97 - the old FILE_MOVED rows that recorded a failure move over."""

    ROWS = [
        ('FILE_MOVED', 'Moved to /mnt/plex/show.mp4', 'FILE_MOVED'),
        ('FILE_MOVED', 'Already at the destination: /mnt/plex/show.mp4', 'FILE_MOVED'),
        ('FILE_MOVED', 'Move FAILED: [Errno 13] denied - file remains at /dvr/show.mp4',
         'FILE_MOVE_FAILED'),
        ('CONVERSION_FAILED', 'Move FAILED: not this step\'s business', 'CONVERSION_FAILED'),
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
                M._m097_file_move_failed_event_type(conn, cur)
                conn.commit()
            got = [r[0] for r in cur.execute(
                'SELECT event_type FROM recording_events ORDER BY id')]
            conn.close()
        return got

    def test_retypes_failed_moves_and_leaves_the_rest_alone(self):
        self.assertEqual([new for _o, _d, new in self.ROWS], self._run(1))

    def test_rerunnable(self):
        self.assertEqual([new for _o, _d, new in self.ROWS], self._run(2))

    def test_registered_as_migration_97(self):
        self.assertIn((97, M._m097_file_move_failed_event_type),
                      [(v, fn) for v, _d, fn in M.SCHEMA_MIGRATIONS])


# ── Event-log classes and timeline marks ────────────────────────────────────

class EventLogClassTests(unittest.TestCase):
    """The detail page's curated views name what the log does (BUGS.md 2026-10-20)."""

    EXPECTED = {
        RECORDING_RESUME_REFUSED: 'bad', GROUP_FAILOVER: 'warn', PRE_CHECK_FAILED: 'warn',
        RECORDING_STARTED_LATE: 'warn', RECORDING_HANDOFF: 'info',
        RECORDING_URL_RERESOLVED: 'info', RECORDING_REPOINTED: 'info',
        FILE_MOVE_FAILED: 'bad',
    }

    def test_every_one_of_the_seven_has_an_explicit_class(self):
        ev_cls = _template_map('ev_cls')
        for event_type, cls in self.EXPECTED.items():
            self.assertEqual(cls, ev_cls.get(event_type),
                             f'{event_type} has no explicit ev_cls decision')

    def test_failover_and_discard_have_a_timeline_label_and_dot(self):
        dots = _template_map('event_dot')
        for event_type in (GROUP_FAILOVER, SEGMENT_DISCARDED):
            self.assertIn(event_type, dots, f'{event_type} gets no run-timeline dot')
        with open(DETAIL_TEMPLATE) as fh:
            src = fh.read()
        block = re.search(r'\{%\s*set\s+timeline_label\s*=\s*\{(.*?)\}\s*%\}', src, re.S).group(1)
        for event_type in (GROUP_FAILOVER, SEGMENT_DISCARDED):
            self.assertIn(f"'{event_type}':", block, f'{event_type} gets no run-timeline label')

    def test_a_failover_renders_in_the_warn_class(self):
        t = make_test_app()
        try:
            rec = seed.make_recording(status=REC_STATUS_COMPLETED, name='failover')
            db.session.flush()
            add_recording_event(rec.id, GROUP_FAILOVER, detail='moved to another member')
            db.session.commit()
            html = t.client.get(f'/recordings/{rec.id}').get_data(as_text=True)
            db.session.expire_all()
            evt = RecordingEvent.query.filter_by(recording_id=rec.id,
                                                 event_type=GROUP_FAILOVER).one()
            self.assertIn(f'<li class="ev warn" data-ev-id="{evt.id}">', html)
        finally:
            t.cleanup()


# ── Dashboard post-capture cells ────────────────────────────────────────────

class DashboardPostCaptureTests(unittest.TestCase):
    """A JOINING or ANALYZING row's three cells describe the phase (BUGS.md 2026-10-20)."""

    def setUp(self):
        self.t = make_test_app()
        now = datetime.utcnow()
        self.join = seed.make_recording(status=REC_STATUS_CONCATENATING, name='joiner',
                                        start_time=now - timedelta(hours=2),
                                        stop_time=now - timedelta(minutes=5),
                                        started_at=now - timedelta(hours=2))
        self.check = seed.make_recording(status=REC_STATUS_ANALYZING, name='checker',
                                         start_time=now - timedelta(hours=2),
                                         stop_time=now - timedelta(minutes=5),
                                         started_at=now - timedelta(hours=2))
        for rec in (self.join, self.check):
            db.session.add(RecordingSegment(
                recording_id=rec.id, segment_number=0, file_path='/nope/seg.ts',
                started_at=rec.start_time, ended_at=rec.stop_time, bytes_recorded=4096,
                exit_reason='STOP_TIME_REACHED'))
        db.session.commit()

    def tearDown(self):
        with concatmod._concat_progress_lock:
            concatmod._concat_progress.pop(self.join.id, None)
        with ppmod._analysis_progress_lock:
            ppmod._analysis_progress.pop(self.check.id, None)
        self.t.cleanup()

    def _row(self, rid):
        html = self.t.client.get('/').get_data(as_text=True)
        return re.search(rf'id="card-{rid}".*?</div>', html, re.S).group(0)

    def test_no_capture_clocks_on_a_post_capture_row(self):
        for rec in (self.join, self.check):
            row = self._row(rec.id)
            self.assertNotIn(f'id="elapsed-{rec.id}"', row)
            self.assertNotIn(f'id="remaining-{rec.id}"', row)
            self.assertNotIn('Remaining', row)

    def test_a_queued_join_and_an_idle_check_say_so(self):
        row = self._row(self.join.id)
        self.assertIn('Joining', row)
        self.assertIn('queued', row)
        row = self._row(self.check.id)
        self.assertIn('Checking', row)
        self.assertIn('between passes', row)

    def test_a_running_join_and_check_show_their_progress(self):
        with concatmod._concat_progress_lock:
            concatmod._concat_progress[self.join.id] = {
                'of': 8, 'total_bytes': 100, 'bytes': 40, 'joined': 3, 'pct': 40.0,
                'eta_seconds': None, 'started': time.monotonic(), '_offsets': [],
            }
        with ppmod._analysis_progress_lock:
            ppmod._analysis_progress[self.check.id] = {
                'pass_label': 'damage scan', 'pass_number': 2, 'of_passes': 2,
                'pct': 60.0, 'bytes': 60, 'total_bytes': 100, 'started': time.monotonic(),
            }
        row = self._row(self.join.id)
        self.assertIn('40%', row)
        self.assertIn('3 of 8', row)
        row = self._row(self.check.id)
        self.assertIn('damage scan (2 of 2)', row)
        self.assertIn('60%', row)

    def test_a_capture_row_keeps_its_clocks_and_names_them(self):
        now = datetime.utcnow()
        live = seed.make_recording(status=REC_STATUS_IN_PROGRESS, name='live',
                                   start_time=now - timedelta(minutes=10),
                                   stop_time=now + timedelta(minutes=50),
                                   started_at=now - timedelta(minutes=10))
        db.session.commit()
        row = self._row(live.id)
        self.assertIn(f'id="elapsed-{live.id}"', row)
        self.assertIn('Since the capture started', row)
        self.assertIn('Until the scheduled stop time', row)


# ── Parked row on the channel page ──────────────────────────────────────────

class ParkedLabelTests(unittest.TestCase):
    """The fourth surface names a parked row WAITING too (BUGS.md 2026-10-20)."""

    def setUp(self):
        self.t = make_test_app()

    def tearDown(self):
        self.t.cleanup()

    def test_channel_page_recordings_table_says_waiting(self):
        acc = seed.make_account()
        ch = seed.make_channel(acc, name='Parked Channel')
        now = datetime.utcnow()
        seed.make_recording(status=REC_STATUS_ANALYZING, name='parked', channel_id=ch.id,
                            completed_at=now, health_quality_score=80,
                            postprocess_waiting_since=now)
        db.session.commit()
        html = self.t.client.get(f'/channels/{ch.id}').get_data(as_text=True)
        self.assertIn('>WAITING<', html)
        self.assertNotIn('>ANALYZING<', html)

    def test_the_filter_takes_the_parked_stamp(self):
        from app.fmt_utils import rec_status_label
        self.assertEqual('WAITING', rec_status_label(REC_STATUS_CONVERTING, waiting=True))
        self.assertEqual('CONVERTING', rec_status_label(REC_STATUS_CONVERTING))
        self.assertEqual('JOINING', rec_status_label(REC_STATUS_CONCATENATING, waiting=True),
                         'a join is never parked, so the stamp changes nothing there')


# ── The umbrella word ───────────────────────────────────────────────────────

class FinishingWordTests(unittest.TestCase):
    """"Converting" is one phase's badge; the umbrella is "finishing" (BUGS.md 2026-10-20)."""

    def setUp(self):
        self.t = make_test_app()

    def tearDown(self):
        self.t.cleanup()

    def test_the_tile_sub_line_counts_finishing_recordings(self):
        now = datetime.utcnow()
        seed.make_recording(status=REC_STATUS_CONCATENATING, name='joiner',
                            start_time=now - timedelta(hours=2), stop_time=now,
                            started_at=now - timedelta(hours=2))
        db.session.commit()
        html = self.t.client.get('/').get_data(as_text=True)
        self.assertIn('1 finishing', html)
        self.assertNotIn('1 converting', html)
        self.assertNotIn('nothing converting', html)

    def test_the_timeline_legend_uses_the_same_word(self):
        """The HA sensor's name is asserted in tests/test_ha_integration_packaging.py, the
        dev-only module that may read custom_components/."""
        with open(os.path.join(ROOT, 'static', 'js', 'dashboard.js')) as fh:
            src = fh.read()
        legend = re.search(r'<i class="converting"></i>([^<]+)</span>', src).group(1)
        self.assertEqual('Finishing', legend)


# ── Bitrate basis ───────────────────────────────────────────────────────────

class BitrateBasisTests(unittest.TestCase):
    """The three rate displays name what they divide by (BUGS.md 2026-10-20)."""

    def setUp(self):
        self.t = make_test_app()

    def tearDown(self):
        self.t.cleanup()

    def _row(self, **kw):
        now = datetime.utcnow()
        rec = seed.make_recording(start_time=now - timedelta(hours=2), stop_time=now, **kw)
        db.session.add(RecordingSegment(
            recording_id=rec.id, segment_number=0, file_path='/nope/seg.ts',
            started_at=rec.start_time, ended_at=rec.stop_time, bytes_recorded=5_000_000_000,
            exit_reason='STOP_TIME_REACHED'))
        db.session.commit()
        # A request context, not a bare app context: _index_row builds url_for() links.
        with self.t.app.test_request_context('/recordings'):
            return _index_row(rec, now, get_display_tz(), set()), rec

    def test_each_basis_has_its_own_tooltip(self):
        row, _ = self._row(status=REC_STATUS_COMPLETED, name='file', final_file_size=4_000_000_000,
                           recorded_duration_seconds=7100.0)
        self.assertIn('content length ffprobe measured', row['rate_tip'])
        row, _ = self._row(status=REC_STATUS_ABORTED, name='segments', final_file_size=4_000_000_000)
        self.assertIn('span of the segments', row['rate_tip'])
        row, _ = self._row(status=REC_STATUS_IN_PROGRESS, name='live',
                           started_at=datetime.utcnow() - timedelta(hours=2))
        self.assertIn('captured so far', row['rate_tip'])

    def test_the_detail_card_and_the_list_carry_it(self):
        _, rec = self._row(status=REC_STATUS_COMPLETED, name='file', final_file_size=4_000_000_000,
                           recorded_duration_seconds=7100.0)
        html = self.t.client.get(f'/recordings/{rec.id}').get_data(as_text=True)
        self.assertRegex(html, r'<span class="sk">Bitrate</span><span class="sv tip-plain" '
                               r'data-tip="File size over the content length')
        self.assertIn('>Avg rate</th>', html)
        self.assertIn('data-tip="Size over the segment', html)
        html = self.t.client.get('/recordings').get_data(as_text=True)
        self.assertRegex(html, r'<span class="sub tip-plain" data-tip="File size over')


# ── Cancelled row with a kept .ts ───────────────────────────────────────────

class CancelledRetryPromotionTests(unittest.TestCase):
    """Retry conversion is promoted where the strip names it (BUGS.md 2026-10-20)."""

    def setUp(self):
        self.t = make_test_app()

    def tearDown(self):
        self.t.cleanup()

    def _page(self, status, with_ts):
        ts = os.path.join(self.t._tmpdir, 'kept.ts')
        if with_ts:
            with open(ts, 'wb') as fh:
                fh.write(b'x' * 64)
        rec = seed.make_recording(status=status, name='kept', output_path=ts,
                                  cancel_reason=CANCEL_DURING_CONVERSION)
        db.session.commit()
        return self.t.client.get(f'/recordings/{rec.id}').get_data(as_text=True)

    def test_a_cancelled_row_with_a_ts_promotes_retry_and_demotes_delete(self):
        html = self._page(REC_STATUS_ABORTED, with_ts=True)
        primary = re.findall(r'<button class="btn btn-primary" data-act="retry-convert">', html)
        self.assertEqual(2, len(primary), 'header and mobile bar each promote it')
        self.assertNotIn('<button class="menu-item" data-act="retry-convert">', html,
                         'the promoted control is not also buried in the kebab')
        kebab_delete = html.count('<button class="menu-item danger" data-act="delete">Delete recording</button>')
        self.assertEqual(2, kebab_delete, 'Delete is in the desktop kebab and the phone kebab')
        # The phone bar holds two of: a promoted retry, Play, Delete (DESIGN.md 9.5 as amended
        # in dev/changelog/1312). The kept .ts plays through the preview pipeline
        # (dev/changelog/1314), so Play takes the second slot and Delete moves to the kebab;
        # Record again is in the kebab either way.
        bar = html[html.index('<div class="mobile-actbar">'):]
        bar = bar[:bar.index('<div class="menu-wrap">')]
        self.assertEqual(re.findall(r'data-act="([\w-]+)"', bar), ['retry-convert', 'play'])
        self.assertNotIn('Record again', bar)

    def test_a_cancelled_row_without_a_ts_promotes_nothing(self):
        html = self._page(REC_STATUS_ABORTED, with_ts=False)
        self.assertNotIn('data-act="retry-convert"', html)
        self.assertIn('<button class="btn btn-danger" data-act="delete">Delete</button>', html)

    def test_a_completed_row_with_a_ts_keeps_the_kebab_item(self):
        html = self._page(REC_STATUS_COMPLETED, with_ts=True)
        self.assertNotIn('<button class="btn btn-primary" data-act="retry-convert">', html)
        self.assertIn('<button class="menu-item" data-act="retry-convert">', html)


# ── Near-empty scan key ─────────────────────────────────────────────────────

class NearEmptyKeyTests(unittest.TestCase):
    """The published number is bytes per second and says so (BUGS.md 2026-10-20)."""

    def setUp(self):
        self.t = make_test_app()

    def tearDown(self):
        self.t.cleanup()

    def test_the_event_names_bytes_per_second(self):
        now = datetime.utcnow()
        rec = seed.make_recording(status=REC_STATUS_ANALYZING, name='scan',
                                  start_time=now - timedelta(minutes=10), stop_time=now)
        for n in range(2):
            db.session.add(RecordingSegment(
                recording_id=rec.id, segment_number=n, file_path=f'/nope/{n}.ts',
                started_at=now - timedelta(minutes=10 - 5 * n),
                ended_at=now - timedelta(minutes=5 - 5 * n), bytes_recorded=60 * 1_000_000,
                exit_reason='STOP_TIME_REACHED'))
        db.session.commit()
        ppmod._detect_near_empty_segments(rec.id)
        scans = [d for d in _diag_kinds(rec.id) if d.get('kind') == 'near_empty_scan']
        self.assertEqual(1, len(scans), scans)
        self.assertEqual(200_000, scans[0]['avg_bytes_per_sec'])
        self.assertNotIn('avg_bitrate_bps', scans[0])


# ── Output format re-probe ──────────────────────────────────────────────────

class OutputFormatReprobeTests(_PostCase):
    """The recorded_* format columns describe the converted file (BUGS.md 2026-10-20)."""

    TS_PROBE = {'resolution': '1920x1080', 'fps': 50.0, 'duration': 45.0, 'bitrate_bps': 5_000_000,
                'vid_width': 1920, 'vid_height': 1080, 'video_codec': 'h264',
                'audio_codec': 'ac3', 'audio_channels': 2, 'audio_sample_rate': 48000,
                'audio_bitrate_kbps': 97.0, 'audio_language': 'eng'}
    MP4_PROBE = dict(TS_PROBE, audio_codec='aac', audio_channels=6, audio_bitrate_kbps=195.0,
                     bitrate_bps=5_100_000)

    def test_a_finished_conversion_rewrites_the_profile_from_its_output(self):
        self.t.sandbox_config(self._config(
            enabled=True, format='mp4', delete_source=False, reencode_mode='never',
            pre_output_timeout_seconds=60, auto_restart=False, max_restart_attempts=0,
            stall_seconds=0, progress_interval_seconds=5, video_encoder='software'))
        now = datetime.utcnow()
        rec = seed.make_recording(status=REC_STATUS_CONCATENATING, name='reprobe',
                                  start_time=now - timedelta(seconds=45), stop_time=now,
                                  recorded_duration_seconds=45.0,
                                  recorded_audio_bitrate_kbps=97.0, recorded_audio_channels=2)
        ts = os.path.join(self.dvr, f'rec_{rec.id}.ts')
        with open(ts, 'wb') as fh:
            fh.write(b'x' * 4096)
        rec.output_path = ts
        db.session.commit()
        rid = rec.id

        def attempt(app, rid_, cmd, part_file, **kw):
            with open(part_file, 'wb') as fh:
                fh.write(b'p' * 64)
            return ppmod.ConversionResult(True, 'success', out_time=45.0)

        def join(parts, output_path, *a, **k):
            with open(output_path, 'wb') as fh:
                fh.write(b'o' * 128)
            return ppmod.PartJoinResult(True, 'success', parts=len(parts))

        def probe(path, **kw):
            return dict(self.MP4_PROBE if path.endswith('.mp4') else self.TS_PROBE)

        clean = {'metrics': {'gap_count': 0, 'gap_seconds': 0.0, 'max_gap_seconds': 0.0,
                             'gap_threshold': 1.0, 'span_seconds': 45.0, 'packet_count': 10},
                 'lost_seconds': 0.0, 'gaps': []}
        with mock.patch.object(ppmod, 'run_conversion_supervised', attempt), \
             mock.patch.object(ppmod, 'join_conversion_parts', join), \
             mock.patch.object(ppmod, 'finalize_part', lambda part, *a, **k: 0.0), \
             mock.patch.object(ppmod, 'check_converted_output', return_value=clean), \
             mock.patch.object(ppmod, '_survey_recording', return_value={}), \
             mock.patch.object(probemod, 'nominal_video_rate', return_value=None), \
             mock.patch.object(probemod, 'parse_ffprobe', probe):
            ppmod.do_postprocess(self.t.app, rid, ts)
        db.session.expire_all()
        rec = db.session.get(Recording, rid)
        self.assertEqual(REC_STATUS_COMPLETED, rec.status, _types(rid))
        self.assertTrue(rec.output_path.endswith('.mp4'))
        self.assertEqual(195.0, rec.recorded_audio_bitrate_kbps)
        self.assertEqual(6, rec.recorded_audio_channels)
        self.assertEqual('aac', rec.recorded_audio_codec)
        self.assertAlmostEqual(5100.0, rec.recorded_bitrate_kbps)

    def test_the_profile_builder_leaves_the_capture_measurements_out(self):
        fields = ppmod._format_profile_fields(self.MP4_PROBE)
        self.assertEqual(195.0, fields['recorded_audio_bitrate_kbps'])
        for capture_only in ('recorded_frame_count', 'recorded_duration_seconds',
                             'recorded_frame_pct', 'health_gathered_at'):
            self.assertNotIn(capture_only, fields)


# ── Resume after CONVERSION_DONE ────────────────────────────────────────────

class ResumeAfterConversionTests(_PostCase):
    """A restart between CONVERSION_DONE and COMPLETED finishes the recording rather than
    failing it as "source .ts missing" (BUGS.md 2026-10-20)."""

    start_scheduler = True

    def _sweep(self):
        from app.scheduler import resume_in_progress_recordings
        resume_in_progress_recordings(self.t.app)

    def _wait_terminal(self, rid, timeout=15):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            db.session.expire_all()
            rec = db.session.get(Recording, rid)
            if rec.status != REC_STATUS_CONVERTING:
                return rec
            time.sleep(0.1)
        self.fail(f'recording {rid} stayed CONVERTING: {_types(rid)}')

    def _done_recording(self, *, with_ts=True, extra_events=()):
        self.t.sandbox_config(self._config(enabled=True, format='mp4', delete_source=True))
        mp4 = os.path.join(self.dvr, 'show.mp4')
        with open(mp4, 'wb') as fh:
            fh.write(b'm' * 128)
        if with_ts:
            with open(os.path.join(self.dvr, 'show.ts'), 'wb') as fh:
                fh.write(b't' * 4096)
        rec = seed.make_recording(status=REC_STATUS_CONVERTING, name='done', output_path=mp4,
                                  converted_gap_seconds=0.0)
        add_recording_event(rec.id, CONVERSION_STARTED, detail='Converting show.ts → show.mp4')
        add_recording_event(rec.id, CONVERSION_DONE, detail='Conversion complete: show.mp4')
        for event_type, detail in extra_events:
            add_recording_event(rec.id, event_type, detail=detail)
        db.session.commit()
        return rec.id

    def test_conversion_finished_reads_the_recorded_facts(self):
        rid = self._done_recording()
        self.assertTrue(ppmod.conversion_finished(rid))
        add_recording_event(rid, CONVERSION_STARTED, detail='Retry: converting again')
        db.session.commit()
        self.assertFalse(ppmod.conversion_finished(rid),
                         'a newer start means the old DONE no longer speaks for it')

    def test_the_sweep_finishes_the_recording_instead_of_failing_it(self):
        rid = self._done_recording()
        self._sweep()
        rec = self._wait_terminal(rid)
        types = _types(rid)
        self.assertEqual(REC_STATUS_COMPLETED, rec.status, types)
        self.assertIsNone(rec.failure_reason)
        self.assertNotIn(CONVERSION_FAILED, types)
        self.assertIn(SOURCE_DELETED, types, 'the disposition the restart interrupted runs')
        self.assertFalse(os.path.exists(os.path.join(self.dvr, 'show.ts')))
        kinds = [d.get('kind') for d in _diag_kinds(rid)]
        self.assertIn('postprocess_resumed_after_conversion', kinds, 'the resume says so')

    def test_a_recorded_move_is_not_repeated(self):
        rid = self._done_recording(with_ts=False, extra_events=(
            (SOURCE_DELETED, 'Deleted the source show.ts'),
            (FILE_MOVED, 'Moved to /mnt/plex/show.mp4')))
        self.t.sandbox_config(cfgmod._deep_merge(
            self._config(enabled=True, format='mp4'),
            {'recording': {'move_on_complete': {
                'enabled': True, 'destination': os.path.join(self.t._tmpdir, 'dest')}}}))
        with mock.patch.object(ppmod.shutil, 'move', side_effect=OSError('must not run')):
            self._sweep()
            rec = self._wait_terminal(rid)
        types = _types(rid)
        self.assertEqual(REC_STATUS_COMPLETED, rec.status, types)
        self.assertNotIn(FILE_MOVE_FAILED, types)
        self.assertEqual(1, types.count(FILE_MOVED))
        self.assertEqual(1, types.count(SOURCE_DELETED), 'no second disposition either')


# ── Capture gap ─────────────────────────────────────────────────────────────

class CaptureGapTests(unittest.TestCase):
    """A segment that recorded zero bytes covers none of the window (BUGS.md 2026-10-20)."""

    def setUp(self):
        self.t = make_test_app()

    def tearDown(self):
        self.t.cleanup()

    def test_refused_attempts_fall_into_the_gap_and_unknown_ones_do_not(self):
        start = datetime.utcnow() - timedelta(seconds=100)
        rec = seed.make_recording(status=REC_STATUS_COMPLETED, name='gaps', start_time=start,
                                  stop_time=start + timedelta(seconds=100),
                                  recorded_duration_seconds=80.0)
        for n, (a, b, size) in enumerate(((0, 40, 1024), (40, 60, 0), (60, 100, None))):
            db.session.add(RecordingSegment(
                recording_id=rec.id, segment_number=n, file_path=f'/nope/{n}.ts',
                started_at=start + timedelta(seconds=a), ended_at=start + timedelta(seconds=b),
                bytes_recorded=size, exit_reason='STOP_TIME_REACHED'))
        db.session.commit()
        db.session.expire_all()
        rec = db.session.get(Recording, rec.id)
        self.assertAlmostEqual(80.0, rec.covered_capture_seconds)
        self.assertAlmostEqual(20.0, rec.capture_gap_seconds)


# ── Timebase as a rate ──────────────────────────────────────────────────────

class TimebaseRateTests(unittest.TestCase):
    """90000/1 is the MPEG-TS clock, not a frame rate (BUGS.md 2026-10-20)."""

    def test_a_timebase_is_no_nominal_rate(self):
        self.assertIsNone(probemod.plausible_nominal_rate(
            {'r_frame_rate': '90000/1', 'avg_frame_rate': '0/0'}))

    def test_a_plausible_average_stands_in_for_a_timebase(self):
        self.assertEqual('30000/1001', probemod.plausible_nominal_rate(
            {'r_frame_rate': '90000/1', 'avg_frame_rate': '30000/1001'}))

    def test_a_real_nominal_rate_is_returned_as_ffprobe_spelled_it(self):
        self.assertEqual('60000/1001', probemod.plausible_nominal_rate(
            {'r_frame_rate': '60000/1001', 'avg_frame_rate': '59/1'}))

    def test_plausible_fps_bounds(self):
        self.assertIsNone(probemod.plausible_fps(90000.0))
        self.assertIsNone(probemod.plausible_fps(0))
        self.assertEqual(119.88, probemod.plausible_fps(119.88))

    def test_the_plan_falls_back_on_the_segments_rate_without_a_header_rate(self):
        src = nz.SourceFormats(sizes=((1920, 1080),), stored_sizes=((1920, 1080, 1.0),),
                               rates=(29.97,), header_rate=None, channels=(2,),
                               interlaced=False, hdr_transfer=None, surveyed=True)
        self.assertEqual('30000/1001', nz.target_rate(src))


# ── Restart failure count ───────────────────────────────────────────────────

class RestartFailureCountTests(RestartHarness):
    """A restart that produced nothing is one consecutive failure (BUGS.md 2026-10-20).

    The opener has exited (one failure), the restart produces no data (RESTART_FAILED, a
    second), and the dead restart is then closed out on that verdict - which used to count a
    third. Same shape as test_watchdog_restart_no_data's run; the harness's non-producing
    stub leaves the dead opener in state.process, so the close-out takes the proc_exited
    label, and the dedupe keys on the restart having produced nothing, not on the label."""

    def test_the_dead_restart_is_not_counted_twice(self):
        self._exited_capture()
        # A 5s restart delay holds the NEXT restart off long enough to read the counter
        # after segment 1 closes; the thread is then stopped before it launches one.
        with mock.patch.object(cfgmod, 'load_config', return_value=self._cfg(2, 5)), \
             mock.patch.object(recorder, '_launch_segment',
                               self._stub_launch_next(produces_data=False)):
            self.wd = WatchdogThread(self.rid, self.state, self.t.app)
            self.wd.start()
            deadline = time.monotonic() + 30
            closed = None
            while time.monotonic() < deadline:
                db.session.expire_all()
                closed = RecordingSegment.query.filter_by(recording_id=self.rid,
                                                          segment_number=1).first()
                if closed is not None and closed.ended_at is not None:
                    break
                time.sleep(0.1)
            db.session.expire_all()
            rec = db.session.get(Recording, self.rid)
            failures, peak = rec.consecutive_failures, rec.consecutive_failures_peak
            self.state.stop_event.set()
            self.wd.join(timeout=15)
        self.assertIsNotNone(closed, 'the dead restart segment never closed')
        self.assertIsNotNone(closed.ended_at, 'the dead restart segment never closed')
        self.assertTrue(RecordingEvent.query.filter_by(
            recording_id=self.rid, event_type=RESTART_FAILED, segment_number=1).first())
        stalls = RecordingEvent.query.filter_by(recording_id=self.rid,
                                                event_type=STALL_DETECTED).count()
        self.assertEqual(2, stalls, 'the opener and the dead restart each close with a stall')
        self.assertEqual(2, failures,
                         f'{failures} failures for two attempts - the dead restart was '
                         f'counted by RESTART_FAILED and again at close-out')
        self.assertEqual(2, peak)


if __name__ == '__main__':
    unittest.main()
