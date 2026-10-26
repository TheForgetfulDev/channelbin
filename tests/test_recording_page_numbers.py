"""The recording page's numbers agree with each other, with the event log and with the
Recordings list (dev/docs/UX-first-time-user-2026-10.md findings 2, 3 and 4).

Guards five dev/docs/BUGS.md entries, each class naming its own:
- 2026-10-22 08:13:54 PM - a RETRYING wait was never counted as downtime;
- 2026-10-22 08:13:55 PM - a live page kept saying Recording and LIVE after a status change;
- 2026-10-22 08:13:56 PM - the list's live size left out the open segment and never moved;
- 2026-10-22 08:13:57 PM - a duration under a minute rendered as "0m";
- 2026-10-22 08:13:58 PM - a sub-second gap total was written as "0s" (in
  tests/test_converted_output_check.py, which has the conversion rig).
The labels that were only wording (Restarts, Restarts with no data, Failures in a row, the
join strip's "of N", the worded health badge) are asserted here too, as the rendering of
what each counts.

No ffmpeg, no network, no /dvr: open segments are local files under the test's temp dir,
addressed through the row's own file_path.
"""
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timedelta
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import db, recorder  # noqa: E402
from app import watchdog as wdmod  # noqa: E402
from app.database import (  # noqa: E402
    Recording, RecordingEvent, RECORDING_RETRY_SCHEDULED, RESTART_ATTEMPTED, REC_STATUS_IN_PROGRESS, REC_STATUS_RETRYING,
)
from app.fmt_utils import fmt_duration  # noqa: E402
from tests.support import seed  # noqa: E402
from tests.support.app import make_test_app  # noqa: E402

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HARNESS = os.path.join(REPO, 'tests', 'support', 'recording_detail_live.mjs')
JSDOM = os.path.join(REPO, 'node_modules', 'jsdom')
HAVE_NODE = shutil.which('node') is not None and os.path.isdir(JSDOM)


class RetryWaitIsDowntimeTests(unittest.TestCase):
    """dev/docs/BUGS.md 2026-10-22 08:13:54 PM: the backoff wait a RETRYING recording sits
    in is wall clock with nothing written, so every way out of it charges it to downtime,
    once."""

    WAIT = 60

    def setUp(self):
        self.t = make_test_app()
        now = datetime.utcnow()
        rec = seed.make_recording(
            status=REC_STATUS_RETRYING, name='retry-wait', start_time=now - timedelta(minutes=10),
            stop_time=now + timedelta(hours=1), dead_stream_retry_count=1,
            next_retry_at=now + timedelta(minutes=1), total_downtime_seconds=10.0)
        db.session.add(RecordingEvent(recording_id=rec.id, event_type=RECORDING_RETRY_SCHEDULED,
                                      timestamp=now - timedelta(seconds=self.WAIT),
                                      detail='Retrying (attempt 1/10) in 1 min'))
        db.session.commit()
        self.rid = rec.id
        self._patches = [mock.patch.object(recorder, 'persist_final_thumbnail'),
                         mock.patch('app.health_score.apply_recording_health_observation')]
        for p in self._patches:
            p.start()

    def tearDown(self):
        for p in reversed(self._patches):
            p.stop()
        self.t.cleanup()

    def _rec(self):
        db.session.remove()
        return db.session.get(Recording, self.rid)

    def _assert_charged(self, rec):
        self.assertAlmostEqual(rec.total_downtime_seconds, 10.0 + self.WAIT, delta=5)
        self.assertIsNone(rec.next_retry_at)

    def test_a_resume_charges_the_wait(self):
        with mock.patch.object(recorder, '_launch_segment'), \
             mock.patch.object(recorder, '_try_acquire_slot_with_preemption'):
            recorder.resume_recording(self.t.app, self.rid)
        rec = self._rec()
        self.assertEqual(rec.status, REC_STATUS_IN_PROGRESS)
        self._assert_charged(rec)

    def test_a_manual_stop_charges_the_wait_and_a_second_stop_does_not_again(self):
        with mock.patch('app.concatenator.do_concatenation'):
            recorder.stop_recording(self.t.app, self.rid, reason='MANUAL_STOP')
            self._assert_charged(self._rec())
            recorder.stop_recording(self.t.app, self.rid, reason='STOP_TIME_REACHED')
        self._assert_charged(self._rec())

    def test_an_abort_charges_the_wait(self):
        recorder.abort_recording(self.t.app, self.rid)
        self._assert_charged(self._rec())

    def test_the_window_running_out_charges_the_wait(self):
        with self.t.app.app_context():
            wdmod.finalize_dead_stream_retry_exhausted(self.t.app, self.rid, cause='window ended')
        rec = self._rec()
        self.assertEqual(rec.status, 'FAILED')
        self._assert_charged(rec)

    def test_the_wait_is_clipped_to_the_window(self):
        rec = db.session.get(Recording, self.rid)
        rec.stop_time = datetime.utcnow() - timedelta(seconds=self.WAIT - 20)
        db.session.commit()
        with self.t.app.app_context():
            wdmod.finalize_dead_stream_retry_exhausted(self.t.app, self.rid)
        self.assertAlmostEqual(self._rec().total_downtime_seconds, 30.0, delta=5)


class DurationUnderAMinuteTests(unittest.TestCase):
    """dev/docs/BUGS.md 2026-10-22 08:13:57 PM: "Captured 0m" 24 seconds in."""

    def test_seconds_are_shown_when_there_is_no_minute(self):
        self.assertEqual(fmt_duration(24), '24s')
        self.assertEqual(fmt_duration(0), '0m')
        self.assertEqual(fmt_duration(90), '1m')
        self.assertEqual(fmt_duration(3600), '1h')

    @unittest.skipUnless(HAVE_NODE, 'node and jsdom are needed')
    def test_the_js_twin_agrees(self):
        # util.js needs a DOM to load, so the one function is read out of it and run alone.
        with open(os.path.join(REPO, 'static', 'js', 'util.js'), encoding='utf-8') as f:
            body = re.search(r'function fmtDur\(.*?\n}\n', f.read(), re.S).group(0)
        out = subprocess.run(
            ['node', '-e', body + 'console.log(JSON.stringify([fmtDur(24, false), fmtDur(0, false), '
                                  'fmtDur(90, false), fmtDur(24)]))'],
            capture_output=True, text=True, timeout=30, check=True).stdout
        self.assertEqual(json.loads(out), ['24s', '0m', '1m', '24s'])


class _LivePage:
    """An IN_PROGRESS recording 24 s into a one-hour window, with one closed segment and an
    open one whose file is on disk, registered as live so the page renders its live parts."""

    CLOSED = 1_000_000
    OPEN = 37_500_000

    def setUp(self):
        self.t = make_test_app()
        now = datetime.utcnow()
        acct = seed.make_account()
        ch = seed.make_channel(acct, logo_url='http://example.test/logo.png')
        self.rec = seed.make_recording(status=REC_STATUS_IN_PROGRESS, channel_id=ch.id,
                                       name='live-rec', start_time=now - timedelta(seconds=24),
                                       stop_time=now + timedelta(seconds=3576),
                                       started_at=now - timedelta(seconds=24))
        closed = seed.make_segment(self.rec, ch, now - timedelta(seconds=24),
                                   now - timedelta(seconds=20), segment_number=1)
        closed.bytes_recorded = self.CLOSED
        live = seed.make_segment(self.rec, ch, now - timedelta(seconds=20), None,
                                 segment_number=2)
        live.bytes_recorded = None
        live.file_path = os.path.join(self.t._tmpdir, 'live_seg_002.ts')
        with open(live.file_path, 'wb') as f:
            f.truncate(self.OPEN)
        db.session.commit()
        self.rid = self.rec.id
        state = recorder.RecordingState()
        state.current_segment_num = 2
        with recorder._lock:
            recorder._active[self.rid] = state

    def tearDown(self):
        with recorder._lock:
            recorder._active.pop(self.rid, None)
        self.t.cleanup()

    def _get(self, path):
        resp = self.t.app.test_client().get(path)
        self.assertEqual(resp.status_code, 200)
        return resp


class LiveListSizeTests(_LivePage, unittest.TestCase):
    """dev/docs/BUGS.md 2026-10-22 08:13:56 PM: the list's size for a running recording
    left out its open segment and stayed frozen until the next status change."""

    def _expected(self):
        from app.routes.recordings import _size_parts
        return _size_parts(self.CLOSED + self.OPEN)

    def test_the_list_counts_the_open_segment(self):
        html = self._get('/recordings').get_data(as_text=True)
        num, unit = self._expected()
        row = html.split(f'data-id="{self.rid}"', 1)[1].split('data-col="health"', 1)[0]
        self.assertIn(f'{num} <span class="unit">{unit}</span>', row)

    def test_the_minute_tick_carries_the_live_rows_cells(self):
        done = seed.make_recording(status='COMPLETED', name='done', with_segment=True)
        db.session.commit()
        rows = self._get('/api/recordings/times').get_json()['rows']
        cells = rows[str(self.rid)]['cells']
        num, unit = self._expected()
        self.assertEqual((cells['size_num'], cells['size_unit']), (num, unit))
        self.assertEqual(cells['dur_str'], '24s')
        self.assertNotIn('cells', rows[str(done.id)])

    def test_a_live_thumbnail_that_fails_falls_back_to_the_logo(self):
        html = self._get('/recordings').get_data(as_text=True)
        row = html.split(f'data-id="{self.rid}"', 1)[1].split('class="txt"', 1)[0]
        self.assertIn('data-logo="http://example.test/logo.png"', row)
        self.assertIn('t.dataset.logo', row)


class LivePageNumbersTests(_LivePage, unittest.TestCase):

    def test_the_live_length_shows_seconds(self):
        """dev/docs/BUGS.md 2026-10-22 08:13:57 PM. While capturing, the strip carries the
        recording's length (dev/changelog/1285); its first render says 24s, not 0m."""
        html = self._get(f'/recordings/{self.rid}').get_data(as_text=True)
        self.assertRegex(html, r'id="live-elapsed">2[4-9]s<')

    def test_the_bitrate_says_it_is_an_average_while_live(self):
        html = self._get(f'/recordings/{self.rid}').get_data(as_text=True)
        self.assertIn('>Avg bitrate<', html)
        self.assertIn('Shown once a minute has been captured.', html)
        self.assertIn('after the first minute', html)

    def test_the_health_badge_has_words(self):
        html = self._get(f'/recordings/{self.rid}').get_data(as_text=True)
        head = html.split('id="detail-head-slot"', 1)[1].split('dh-actions', 1)[0]
        self.assertIn('✓ no stalls', head)

    def test_the_strip_says_failures_in_a_row(self):
        html = self._get(f'/recordings/{self.rid}').get_data(as_text=True)
        self.assertIn('failures in a row <span class="mono" id="live-failures">', html)


class FinishedPageNumbersTests(unittest.TestCase):
    """A stalled recording's finished page: the counters name what they count."""

    def setUp(self):
        self.t = make_test_app()
        now = datetime.utcnow()
        acct = seed.make_account()
        ch = seed.make_channel(acct)
        rec = seed.make_recording(
            status='COMPLETED', channel_id=ch.id, name='stalled',
            start_time=now - timedelta(minutes=10), stop_time=now - timedelta(minutes=4),
            completed_at=now - timedelta(minutes=3), total_stall_count=6,
            total_restart_count=1, consecutive_failures=0, consecutive_failures_peak=6,
            final_file_size=91_100_000, recorded_duration_seconds=180.0,
            program_start_time=now - timedelta(minutes=30),
            program_stop_time=now + timedelta(minutes=70))
        t0 = now - timedelta(minutes=10)
        for n in range(1, 8):
            seg = seed.make_segment(rec, ch, t0 + timedelta(seconds=40 * (n - 1)),
                                    t0 + timedelta(seconds=40 * n), segment_number=n,
                                    exit_reason='RESTART_NO_DATA' if n in (3, 4, 5, 6) else 'STALL_KILLED')
            seg.bytes_recorded = 0 if n in (2, 3, 4, 5, 6) else 40_000_000
        for _ in range(6):
            db.session.add(RecordingEvent(recording_id=rec.id, event_type=RESTART_ATTEMPTED,
                                          detail='Waiting 5s before restart'))
        db.session.commit()
        self.rid = rec.id
        self.html = self.t.app.test_client().get(f'/recordings/{self.rid}').get_data(as_text=True)

    def tearDown(self):
        self.t.cleanup()

    def _stat(self, label):
        m = re.search(r'<span class="sk">' + re.escape(label) + r'</span>\s*<span[^>]*>(.*?)</span>',
                      self.html, re.S)
        self.assertIsNotNone(m, f'no {label} row')
        return re.sub(r'<[^>]+>', '', m.group(1)).strip()

    def test_restarts_counts_what_the_event_log_lists(self):
        self.assertEqual(self._stat('Restarts'), '6 tried · 1 got the stream back')

    def test_restarts_with_no_data_and_failures_in_a_row(self):
        self.assertEqual(self._stat('Restarts with no data'), '4')
        self.assertEqual(self._stat('Failures in a row'), '0 now · 6 at most')
        self.assertNotIn('>Errors<', self.html)
        self.assertNotIn('>Consecutive failures<', self.html)

    def test_the_join_strip_says_what_it_left_out(self):
        strip = self.html.split('class="live-strip done-strip"', 1)[1].split('</div>', 1)[0]
        self.assertRegex(strip, r'Joined <b>\d+ of 7 segments</b>')
        self.assertIn('5 held no data', strip)

    def test_the_program_length_says_why_it_differs(self):
        self.assertEqual(self._stat('Program length'), '1h 40m')
        self.assertIn('scheduled to start 20m after the program began', self.html)


@unittest.skipUnless(HAVE_NODE, 'node and jsdom are needed')
class LivePageFollowsStatusTests(_LivePage, unittest.TestCase):
    """dev/docs/BUGS.md 2026-10-22 08:13:55 PM: the live page follows a move into RETRYING,
    and its screenshot tag stops claiming LIVE when no bytes are arriving."""

    def _observe(self):
        html = self._get(f'/recordings/{self.rid}').get_data(as_text=True)
        swap = html.replace(f'data-status="{REC_STATUS_IN_PROGRESS}"',
                            f'data-status="{REC_STATUS_RETRYING}"')
        self.assertNotEqual(html, swap)

        def snap(grown):
            # The open segment (number 2) is OPEN bytes at render; what it has grown by since.
            n = self.OPEN + grown
            return {'event': 'STATS_SNAPSHOT',
                    'data': {'status': REC_STATUS_IN_PROGRESS, 'total_bytes': self.CLOSED + n,
                             'current_segment_number': 2, 'current_segment_bytes': n,
                             'elapsed_seconds': 30, 'remaining_seconds': 600,
                             'stall_count': 0, 'consecutive_failures': 0}}

        scenarios = {
            'same_status': {'frames': [snap(100)]},
            'retry_frame': {'frames': [{'event': RECORDING_RETRY_SCHEDULED,
                                        'data': {'status': REC_STATUS_RETRYING, 'attempt': 1}}]},
            'swap_retrying': {'swap': True},
            'stalled': {'frames': [snap(100)], 'advanceMs': 20000, 'after': [snap(100)]},
            'growing': {'frames': [snap(100)], 'advanceMs': 20000, 'after': [snap(200)]},
        }
        tmp = tempfile.mkdtemp(prefix='rec_detail_live_')
        try:
            for name, body in (('page.html', html), ('swap.html', swap),
                               ('scenarios.json', json.dumps(scenarios))):
                with open(os.path.join(tmp, name), 'w', encoding='utf-8') as f:
                    f.write(body)
            proc = subprocess.run(['node', HARNESS, tmp, REPO], capture_output=True,
                                  text=True, timeout=120, cwd=REPO)
            if proc.returncode != 0:
                raise AssertionError(f'harness failed:\n{proc.stderr[-4000:]}')
            return json.loads(proc.stdout)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_status_changes_reload_and_nothing_else_does(self):
        r = self._observe()
        self.assertEqual(r['same_status']['reloads'], 0, r['same_status'])
        self.assertGreaterEqual(r['retry_frame']['reloads'], 1, r['retry_frame'])
        self.assertGreaterEqual(r['swap_retrying']['reloads'], 1, r['swap_retrying'])

    def test_the_screenshot_tag_follows_the_bytes(self):
        r = self._observe()
        self.assertEqual(r['stalled']['tag'], 'NO NEW VIDEO', r['stalled'])
        self.assertTrue(r['stalled']['stale'])
        self.assertEqual(r['growing']['tag'], 'LIVE', r['growing'])
        self.assertFalse(r['growing']['stale'])
        self.assertEqual(r['same_status']['tag'], 'LIVE')


if __name__ == '__main__':
    unittest.main()
