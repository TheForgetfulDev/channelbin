"""The live recording page follows the capture's real state, and comes back down when it does
(dev/docs/UX-first-time-user-2026-10-retest.md findings 2, 3 and 27; dev/changelog/1285).

Guards five dev/docs/BUGS.md entries, each class naming its own:
- 2026-10-25 06:09:12 PM - the live strip said "Recording" through restarts that got nothing,
  and its counters stood still while they ran;
- 2026-10-25 06:09:13 PM - the live strip's first render was dashes, a LIVE tag and "waiting
  for stream data" whatever the server knew;
- 2026-10-25 06:09:14 PM - a capture the retry brought back kept the failure run it tripped on;
- 2026-10-25 06:09:15 PM - Captured counted a restart with no data, then dropped it, and read
  "-" while the segments were joined;
- 2026-10-25 06:09:16 PM - Back brought the page back as it was at its last full load;
- 2026-10-25 06:09:17 PM - the live strip's size counted a discarded placeholder segment.
The rest (one place for the moving totals, the story saying the retry worked and leading with
the file, "finishing up", where a late start joined the program) is asserted as rendering.

No ffmpeg, no network, no /dvr: segment files are local files under the test's temp dir,
addressed through the row's own file_path.
"""
import json
import os
import queue
import re
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
from app import db, recorder  # noqa: E402
from app import events as evmod  # noqa: E402
from app import watchdog as wdmod  # noqa: E402
from app.database import (  # noqa: E402
    Recording, RecordingEvent, RECORDING_RESUMED, RECORDING_RETRY_SCHEDULED,
    RESTART_ATTEMPTED, SEGMENT_EXCLUDED_PLACEHOLDER, STALL_DETECTED,
    REC_STATUS_IN_PROGRESS, REC_STATUS_RETRYING,
)
from app.recording_story import what_happened  # noqa: E402
from app.routes.recordings import filesize_filter  # noqa: E402
from tests.support import seed  # noqa: E402
from tests.support.app import make_test_app  # noqa: E402
from tests.support.watchdog import WatchdogHarness, sleeper  # noqa: E402

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HARNESS = os.path.join(REPO, 'tests', 'support', 'recording_detail_live.mjs')
JSDOM = os.path.join(REPO, 'node_modules', 'jsdom')
HAVE_NODE = shutil.which('node') is not None and os.path.isdir(JSDOM)

CLOSED = 1_000_000
OPEN = 37_500_000


class _Live(unittest.TestCase):
    """An IN_PROGRESS recording 60 s into a one-hour window, registered as live. Segment 1
    is closed with CLOSED bytes; segment 2 is open and its file holds `open_bytes`."""

    open_bytes = OPEN
    closed_segment = True

    def setUp(self):
        self.t = make_test_app()
        self.now = datetime.utcnow()
        acct = seed.make_account()
        self.ch = seed.make_channel(acct)
        self.rec = seed.make_recording(status=REC_STATUS_IN_PROGRESS, channel_id=self.ch.id,
                                       name='live-rec', start_time=self.now - timedelta(seconds=60),
                                       stop_time=self.now + timedelta(seconds=3540),
                                       started_at=self.now - timedelta(seconds=60))
        if self.closed_segment:
            closed = seed.make_segment(self.rec, self.ch, self.now - timedelta(seconds=60),
                                       self.now - timedelta(seconds=20), segment_number=1)
            closed.bytes_recorded = CLOSED
        live = seed.make_segment(self.rec, self.ch, self.now - timedelta(seconds=10), None,
                                 segment_number=2)
        live.bytes_recorded = None
        live.file_path = os.path.join(self.t._tmpdir, 'live_seg_002.ts')
        with open(live.file_path, 'wb') as f:
            f.truncate(self.open_bytes)
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

    def _event(self, event_type, seconds_ago, **kw):
        db.session.add(RecordingEvent(recording_id=self.rid, event_type=event_type,
                                      timestamp=self.now - timedelta(seconds=seconds_ago), **kw))
        db.session.commit()

    def _resp(self, path=None):
        resp = self.t.app.test_client().get(path or f'/recordings/{self.rid}')
        self.assertEqual(resp.status_code, 200)
        return resp

    def _html(self):
        return self._resp().get_data(as_text=True)

    def _strip(self, html):
        return html.split('id="live-strip"', 1)[1].split('</div>', 1)[0]


class LiveStripFirstRenderTests(_Live):
    """dev/docs/BUGS.md 2026-10-25 06:09:13 PM: the first render says what the server knows -
    numbers rather than dashes, and the capture's state rather than "Recording" and LIVE."""

    def test_a_capturing_recording_renders_its_numbers_and_its_state(self):
        html = self._html()
        strip = self._strip(html)
        self.assertIn('id="live-state">Recording<', strip)
        self.assertRegex(strip, r'id="live-elapsed">1m [0-9]+s<')
        self.assertRegex(strip, r'id="live-remaining">5[89]m [0-9]+s<')
        self.assertIn(f'id="live-bytes">{filesize_filter(CLOSED + OPEN)}<', strip)
        self.assertNotIn('>-<', strip)
        self.assertIn('data-capturing="yes"', html)
        self.assertIn('id="live-thumb-tag-text">LIVE<', html)
        self.assertIn('LOADING THE PICTURE', html)
        self.assertNotIn('WAITING FOR STREAM DATA', html)

    def test_a_restart_waiting_for_data_is_not_called_recording(self):
        with open(os.path.join(self.t._tmpdir, 'live_seg_002.ts'), 'wb'):
            pass
        self._event(STALL_DETECTED, 12)
        self._event(RESTART_ATTEMPTED, 11)
        html = self._html()
        self.assertIn('id="live-state">Not capturing<', html)
        self.assertIn('data-reconnecting="yes"', html)
        self.assertIn('live-strip not-capturing', html)
        self.assertIn('id="live-thumb-tag-text">NO NEW VIDEO<', html)

    def test_the_strip_rides_on_the_failure_count_it_was_rendered_with(self):
        rec = db.session.get(Recording, self.rid)
        rec.total_stall_count, rec.consecutive_failures = 3, 2
        db.session.commit()
        strip = self._strip(self._html())
        self.assertIn('id="live-stalls">3<', strip)
        self.assertIn('id="live-failures">2<', strip)


class LiveStripConnectingTests(_Live):
    """The first segment, before its first byte: connecting, not failing."""

    open_bytes = 0
    closed_segment = False

    def test_nothing_yet_is_connecting(self):
        html = self._html()
        self.assertIn('id="live-state">Connecting<', html)
        self.assertIn('data-had-data="no"', html)
        self.assertIn('WAITING FOR STREAM DATA', html)
        self.assertIn('id="live-bytes">0 B<', html)


class MovingTotalsShownOnceTests(_Live):
    """While capturing, the size and length that move by themselves are the strip's alone:
    the card beside it is swapped every 15 s and showed other values for the same thing."""

    def _stats(self, html):
        return html.split('id="hero-right"', 1)[1].split('id="panel-segments-slot"', 1)[0]

    def test_details_leaves_the_strips_numbers_to_the_strip(self):
        stats = self._stats(self._html())
        self.assertNotIn('<span class="sk">Captured</span>', stats)
        self.assertNotIn('<span class="sk">Size</span>', stats)
        self.assertIn('<span class="sk">Avg bitrate</span>', stats)

    def test_a_recording_waiting_to_retry_still_shows_them(self):
        rec = db.session.get(Recording, self.rid)
        rec.status = REC_STATUS_RETRYING
        db.session.commit()
        with recorder._lock:
            recorder._active.pop(self.rid, None)
        stats = self._stats(self._html())
        self.assertIn('<span class="sk">Captured</span>', stats)
        self.assertIn('<span class="sk">Size</span>', stats)


class CapturedNeverCountsAnEmptyRestartTests(unittest.TestCase):
    """dev/docs/BUGS.md 2026-10-25 06:09:15 PM: "Captured 53s", then 45s - the open restart
    was counted while it waited for a byte and dropped when it closed with none. And during
    the join, Captured and Bitrate went back to "-"."""

    def setUp(self):
        self.t = make_test_app()
        self.now = datetime.utcnow()
        self.ch = seed.make_channel(seed.make_account())
        db.session.commit()

    def tearDown(self):
        self.t.cleanup()

    def _rec(self, status, open_bytes):
        rec = seed.make_recording(status=status, channel_id=self.ch.id, name=f'r-{status}',
                                  start_time=self.now - timedelta(seconds=120),
                                  stop_time=self.now + timedelta(hours=1),
                                  started_at=self.now - timedelta(seconds=120))
        first = seed.make_segment(rec, self.ch, self.now - timedelta(seconds=120),
                                  self.now - timedelta(seconds=15), segment_number=1)
        first.bytes_recorded = 40_000_000
        restart = seed.make_segment(rec, self.ch, self.now - timedelta(seconds=8), None,
                                    segment_number=2)
        restart.bytes_recorded = None
        restart.file_path = os.path.join(self.t._tmpdir, f'seg_{rec.id}_002.ts')
        with open(restart.file_path, 'wb') as f:
            f.truncate(open_bytes)
        db.session.commit()
        return rec

    def _captured(self, rec, open_bytes):
        from app.routes.recordings import _dur_size_cells
        return _dur_size_cells(rec, datetime.utcnow(), open_bytes)['dur_secs']

    def test_a_restart_with_no_data_adds_nothing(self):
        rec = self._rec(REC_STATUS_IN_PROGRESS, 0)
        self.assertEqual(self._captured(rec, 0), 105)

    def test_a_restart_with_data_counts_from_its_start(self):
        rec = self._rec(REC_STATUS_IN_PROGRESS, 1024)
        self.assertGreaterEqual(self._captured(rec, 1024), 113)

    def test_the_join_keeps_what_the_segments_captured(self):
        rec = self._rec('CONCATENATING', 0)
        seg = rec.segments[-1]
        seg.ended_at = self.now
        seg.bytes_recorded = 8_000_000
        db.session.commit()
        html = self.t.client.get(f'/recordings/{rec.id}').get_data(as_text=True)
        m = re.search(r'<span class="sk">Captured</span>\s*<span[^>]*>(.*?)</span></div>', html, re.S)
        self.assertIsNotNone(m)
        # To the second since dev/changelog/1287, as What happened gives the file's length.
        self.assertRegex(re.sub(r'<[^>]+>', '', m.group(1)), r'1m \d+s of 1h 2m scheduled')
        rate = re.search(r'<span class="sk">Bitrate</span>\s*<span[^>]*>(.*?)</span>', html, re.S)
        self.assertNotEqual(rate.group(1).strip(), '-')


class FinishedPageWordingTests(unittest.TestCase):
    """The conversion at its last percent, and where a late start joined the program."""

    def setUp(self):
        self.t = make_test_app()
        self.now = datetime.utcnow()
        self.ch = seed.make_channel(seed.make_account())
        db.session.commit()

    def tearDown(self):
        self.t.cleanup()

    def test_a_conversion_at_100_percent_is_finishing_up(self):
        rec = seed.make_recording(status='CONVERTING', channel_id=self.ch.id, name='conv',
                                  start_time=self.now - timedelta(minutes=30),
                                  stop_time=self.now - timedelta(minutes=1),
                                  conversion_progress_pct=100.0, conversion_eta_seconds=None,
                                  with_segment=True)
        db.session.commit()
        html = self.t.client.get(f'/recordings/{rec.id}').get_data(as_text=True)
        strip = html.split('id="status-strip-slot"', 1)[1].split('</div>', 1)[0]
        self.assertIn('finishing up', strip)
        self.assertNotIn('estimating time left', strip)

    def test_a_late_start_says_how_far_into_the_program_it_joined(self):
        start = self.now - timedelta(minutes=10)
        rec = seed.make_recording(status='COMPLETED', channel_id=self.ch.id, name='late',
                                  start_time=start, stop_time=self.now,
                                  started_at=start, recorded_duration_seconds=590,
                                  program_start_time=start - timedelta(minutes=21),
                                  program_stop_time=self.now)
        seed.make_segment(rec, self.ch, start, self.now)
        db.session.commit()
        html = self.t.client.get(f'/recordings/{rec.id}').get_data(as_text=True)
        self.assertIn('of 10m scheduled, from 21m into the program', html)


class BackGetsAFreshPageTests(_Live):
    """dev/docs/BUGS.md 2026-10-25 06:09:16 PM: the page keeps itself current by swapping
    regions, so the copy a browser kept is the page as of its last full load."""

    def test_the_page_is_never_stored(self):
        self.assertEqual(self._resp().headers.get('Cache-Control'), 'no-store')


class RetryRecoveryStoryTests(_Live):
    """What happened says the retry got the stream back, and - while the "not capturing" alert
    still stands - when that alert goes."""

    def test_the_story_says_the_retry_got_the_stream_back(self):
        self._event(RECORDING_RETRY_SCHEDULED, 40,
                    extra_data=json.dumps({'attempt': 1, 'streak': 3}))
        self._event(RECORDING_RESUMED, 12, detail='Retry attempt 1 - reconnecting')
        rec = db.session.get(Recording, self.rid)
        self.assertNotIn('Trying again got the stream back.', what_happened(rec))
        self.assertIn('Trying again got the stream back.',
                      what_happened(rec, open_segment_bytes=OPEN))
        wdmod.raise_recording_retrying_alert(rec, 3, 1, 10, None, 60)
        html = self._html()
        self.assertRegex(html, r'Trying again got the stream back\. The &#34;Recording not '
                               r'capturing&#34; alert clears by itself once it has kept coming '
                               r'for \d+ s\.')

    def test_a_retry_that_got_nothing_says_nothing_of_the_kind(self):
        self._event(RECORDING_RETRY_SCHEDULED, 5,
                    extra_data=json.dumps({'attempt': 1, 'streak': 3}))
        rec = db.session.get(Recording, self.rid)
        self.assertNotIn('Trying again got the stream back.',
                         what_happened(rec, open_segment_bytes=OPEN))


class InheritedFailureRunTests(WatchdogHarness):
    """dev/docs/BUGS.md 2026-10-25 06:09:14 PM: a capture this watchdog was started on - the
    retry after a dead-stream wait - ends the failure run it resumed with once it gets data.
    It used to keep it: "failures in a row 4" over a stream that was back."""

    def _run_until(self, done, timeout=20):
        cfg = self._cfg(stall_timeout=60)
        with mock.patch.object(cfgmod, 'load_config', return_value=cfg):
            self.wd = wdmod.WatchdogThread(self.rid, self.state, self.t.app)
            self.wd.start()
            deadline = time.monotonic() + timeout
            while time.monotonic() < deadline:
                db.session.expire_all()
                if done(db.session.get(Recording, self.rid)):
                    break
                time.sleep(0.2)
            self.state.stop_event.set()
            self.wd.join(timeout=15)
        db.session.expire_all()
        return db.session.get(Recording, self.rid)

    def test_data_on_the_resumed_capture_ends_the_run(self):
        rec = db.session.get(Recording, self.rid)
        rec.consecutive_failures, rec.consecutive_failures_peak = 4, 4
        db.session.commit()
        self.state.process = sleeper()
        rec = self._run_until(lambda r: r.consecutive_failures == 0)
        self.assertEqual(rec.consecutive_failures, 0)
        self.assertEqual(rec.consecutive_failures_peak, 4, 'the peak is history and stays')


class SnapshotTotalTests(unittest.TestCase):
    """dev/docs/BUGS.md 2026-10-25 06:09:17 PM: the strip's size leaves out a discarded
    placeholder segment, as the page's Size does: it is not in the file."""

    def setUp(self):
        self.t = make_test_app()
        now = datetime.utcnow()
        ch = seed.make_channel(seed.make_account())
        self.rec = seed.make_recording(status=REC_STATUS_IN_PROGRESS, channel_id=ch.id,
                                       started_at=now - timedelta(minutes=2))
        kept = seed.make_segment(self.rec, ch, now - timedelta(minutes=2),
                                 now - timedelta(minutes=1), segment_number=1)
        kept.bytes_recorded = CLOSED
        clip = seed.make_segment(self.rec, ch, now - timedelta(minutes=1),
                                 now - timedelta(seconds=50), segment_number=2,
                                 excluded_reason=SEGMENT_EXCLUDED_PLACEHOLDER)
        clip.bytes_recorded = 14_472_616
        live = seed.make_segment(self.rec, ch, now - timedelta(seconds=45), None,
                                 segment_number=3)
        live.file_path = os.path.join(self.t._tmpdir, 'snap_seg_003.ts')
        with open(live.file_path, 'wb') as f:
            f.truncate(2048)
        db.session.commit()
        self.state = recorder.RecordingState(current_segment_num=3)

    def tearDown(self):
        self.t.cleanup()

    def test_the_discarded_clip_is_not_counted(self):
        q = evmod.subscribe(self.rec.id)
        try:
            wd = wdmod.WatchdogThread(self.rec.id, self.state, self.t.app)
            wd._publish_snapshot(cfgmod.load_config())
            frame = json.loads(q.get(timeout=5))
        except queue.Empty:
            self.fail('no snapshot was published')
        finally:
            evmod.unsubscribe(q, self.rec.id)
        self.assertEqual(frame['data']['total_bytes'], CLOSED + 2048)


@unittest.skipUnless(HAVE_NODE, 'node and jsdom are needed')
class LiveStripFollowsTheWatchdogTests(_Live):
    """dev/docs/BUGS.md 2026-10-25 06:09:12 PM: the strip says whether data is arriving and
    what the watchdog is doing, from the watchdog's own events as they arrive, and the rest of
    the page is asked to follow at once. Also the back-forward cache half of 2026-10-25
    06:09:16 PM."""

    def _observe(self):
        html = self._html()

        def snap(grown):
            n = OPEN + grown
            return {'event': 'STATS_SNAPSHOT',
                    'data': {'status': REC_STATUS_IN_PROGRESS, 'total_bytes': CLOSED + n,
                             'current_segment_number': 2, 'current_segment_bytes': n,
                             'elapsed_seconds': 60, 'remaining_seconds': 3540,
                             'stall_count': 0, 'consecutive_failures': 0}}

        stall = {'event': 'STALL_DETECTED', 'data': {'segment_number': 2, 'stall_count': 1,
                                                     'consecutive_failures': 1}}
        attempted = {'event': 'RESTART_ATTEMPTED', 'data': {'delay': 5, 'consecutive_failures': 1}}
        failed = {'event': 'RESTART_FAILED', 'data': {'segment_number': 3,
                                                      'consecutive_failures': 2}}
        back = {'event': 'RESTART_SUCCEEDED', 'data': {'segment_number': 4,
                                                       'consecutive_failures': 0}}
        scenarios = {
            'stall': {'frames': [snap(100), stall, attempted]},
            'restarts_then_back': {'frames': [snap(100), stall, attempted, failed],
                                   'advanceMs': 30000, 'after': [back]},
            'restarts_get_nothing': {'frames': [snap(100), stall, attempted, failed],
                                     'advanceMs': 30000},
            'stopped_arriving': {'frames': [snap(100)], 'advanceMs': 20000,
                                 'after': [snap(100)]},
            'growing': {'frames': [snap(100)], 'advanceMs': 20000, 'after': [snap(200)]},
            'never_seen': {'advanceMs': 20000},
            'from_the_bfcache': {'frames': [snap(100)], 'pageshow': True},
        }
        tmp = tempfile.mkdtemp(prefix='rec_live_state_')
        try:
            for name, body in (('page.html', html), ('swap.html', html),
                               ('scenarios.json', json.dumps(scenarios))):
                with open(os.path.join(tmp, name), 'w', encoding='utf-8') as f:
                    f.write(body)
            proc = subprocess.run(['node', HARNESS, tmp, REPO], capture_output=True,
                                  text=True, timeout=120, cwd=REPO)
            if proc.returncode != 0:
                raise AssertionError(f'harness failed:\n{proc.stderr[-4000:]}')
            out = json.loads(proc.stdout)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
        for name, r in out.items():
            self.assertEqual(r['errors'], [], name)
        return out

    @classmethod
    def setUpClass(cls):
        cls._cache = None

    def _obs(self):
        if LiveStripFollowsTheWatchdogTests._cache is None:
            LiveStripFollowsTheWatchdogTests._cache = self._observe()
        return LiveStripFollowsTheWatchdogTests._cache

    def test_a_stall_is_not_recording_and_says_what_is_being_done(self):
        r = self._obs()['stall']
        self.assertEqual(r['label'], 'Not capturing for 0s')
        self.assertEqual(r['why'], ' - reconnecting to the stream')
        self.assertTrue(r['notCapturing'])
        self.assertEqual(r['tag'], 'NO NEW VIDEO')

    def test_the_counts_move_with_the_events_not_the_next_snapshot(self):
        r = self._obs()['stall']
        self.assertEqual((r['stalls'], r['failures']), ('1', '1'))
        self.assertGreaterEqual(r['earlySwaps'], 1, 'the rest of the page was not asked to follow')

    def test_restarts_that_get_nothing_stay_not_capturing(self):
        r = self._obs()['restarts_get_nothing']
        self.assertEqual(r['label'], 'Not capturing for 30s')
        self.assertEqual(r['failures'], '2')

    def test_a_restart_that_gets_the_stream_back_is_recording_again(self):
        r = self._obs()['restarts_then_back']
        self.assertEqual(r['label'], 'Recording')
        self.assertEqual(r['why'], '')
        self.assertEqual(r['failures'], '0')
        self.assertFalse(r['notCapturing'])
        self.assertEqual(r['tag'], 'LIVE')

    def test_data_that_stops_arriving_is_said_with_how_long(self):
        r = self._obs()['stopped_arriving']
        self.assertEqual(r['label'], 'Not capturing for 20s')
        self.assertEqual(r['why'], ' - no new data from the stream')

    def test_data_that_keeps_arriving_is_recording(self):
        r = self._obs()['growing']
        self.assertEqual((r['label'], r['tag']), ('Recording', 'LIVE'))

    def test_the_servers_reading_alone_prints_no_duration(self):
        r = self._obs()['never_seen']
        self.assertEqual(r['label'], 'Not capturing')

    def test_a_page_from_the_back_forward_cache_reloads(self):
        self.assertGreaterEqual(self._obs()['from_the_bfcache']['reloads'], 1)
        self.assertEqual(self._obs()['growing']['reloads'], 0)


if __name__ == '__main__':
    unittest.main()
