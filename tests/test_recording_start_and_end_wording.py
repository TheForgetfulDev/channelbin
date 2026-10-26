"""Tier 2 - what a recording says at its end: the join's reason, the conversion that follows
it, and the recording page on a phone.

Guards dev/docs/BUGS.md 2026-10-23 "A manual stop is logged as 'Stop time reached'" and
"A same-channel handoff is logged as 'Stop time reached'". stop_recording() handed the join
no reason for a Stop, so do_concatenation()'s default, "Stop time reached; beginning the
join", landed straight after RECORDING_MANUALLY_STOPPED; the handoff path did the same for a
recording that was cut short to make room for the next one. Also covers the wording half of
the same first-time-user finding group (dev/changelog/1275): the page says a conversion
follows the join, and why, before and while it runs, and a long event log is clipped on a
phone.

No network and no real ffmpeg: do_concatenation is replaced with a spy, and the teardown is
stubbed for the handoff because there is no live capture to tear down.
  python3 -m unittest tests.test_recording_start_and_end_wording
"""
import os
import re
import sys
import time
import unittest
from datetime import datetime, timedelta
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import app.recorder as recorder  # noqa: E402
from app import db  # noqa: E402
from app.database import (  # noqa: E402
    RecordingEvent, REC_STATUS_CONVERTING, REC_STATUS_COMPLETED,
    REC_STATUS_IN_PROGRESS, REC_STATUS_PAUSED, REC_STATUS_SCHEDULED,
)
from tests.support import make_test_app, seed  # noqa: E402

DEFAULT_JOIN_REASON = 'Stop time reached'


def _wait_until(predicate, timeout=3.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return predicate()


class _Harness(unittest.TestCase):
    def setUp(self):
        self.t = make_test_app()
        self._stubs = [
            mock.patch('app.health_score.apply_recording_health_observation'),
            mock.patch.object(recorder, 'persist_final_thumbnail'),
        ]
        for s in self._stubs:
            s.start()

    def tearDown(self):
        for s in self._stubs:
            s.stop()
        self.t.cleanup()

    def _rec(self, status, **kw):
        now = datetime.utcnow()
        kw.setdefault('start_time', now - timedelta(minutes=10))
        kw.setdefault('stop_time', now + timedelta(hours=1))
        rec = seed.make_recording(status=status, name=f'wording {status}', **kw)
        db.session.commit()
        return rec.id

    @staticmethod
    def _join_reason(join):
        """The reason the join was handed, or None when it fell back to its default."""
        return join.call_args.kwargs.get('reason')


class ManualStopNamesTheStopTests(_Harness):
    def test_a_stop_on_a_paused_recording_tells_the_join_it_was_stopped(self):
        rid = self._rec(REC_STATUS_PAUSED)
        with mock.patch('app.concatenator.do_concatenation') as join:
            recorder.stop_recording(self.t.app, rid, reason='MANUAL_STOP')
            self.assertTrue(_wait_until(lambda: join.called))
        reason = self._join_reason(join)
        self.assertIsNotNone(reason, 'a Stop fell back to the join default "Stop time reached"')
        self.assertIn('Stopped by the user', reason)
        self.assertNotIn(DEFAULT_JOIN_REASON, reason)

    def test_a_stop_on_a_recording_still_capturing_tells_the_join_too(self):
        rid = self._rec(REC_STATUS_IN_PROGRESS)
        with mock.patch.object(recorder, '_teardown_active_ffmpeg', return_value=True), \
                mock.patch('app.concatenator.do_concatenation') as join:
            recorder.stop_recording(self.t.app, rid, reason='MANUAL_STOP')
            self.assertTrue(_wait_until(lambda: join.called))
        self.assertIn('Stopped by the user', self._join_reason(join) or '')

    def test_the_stop_time_still_reads_as_the_stop_time(self):
        """Characterization: the scheduled stop keeps the join's default wording."""
        rid = self._rec(REC_STATUS_PAUSED, stop_time=datetime.utcnow() - timedelta(seconds=5))
        with mock.patch('app.concatenator.do_concatenation') as join:
            recorder.stop_recording(self.t.app, rid)
            self.assertTrue(_wait_until(lambda: join.called))
        self.assertIsNone(self._join_reason(join))

    def test_the_manual_stop_event_is_still_written(self):
        rid = self._rec(REC_STATUS_PAUSED)
        with mock.patch('app.concatenator.do_concatenation') as join:
            recorder.stop_recording(self.t.app, rid, reason='MANUAL_STOP')
            self.assertTrue(_wait_until(lambda: join.called))
        db.session.expire_all()
        types = [e.event_type for e in RecordingEvent.query.filter_by(recording_id=rid)]
        self.assertIn('RECORDING_MANUALLY_STOPPED', types)


class HandoffNamesTheHandoffTests(_Harness):
    def test_a_handoff_tells_the_join_which_recording_took_over(self):
        old = self._rec(REC_STATUS_IN_PROGRESS)
        new = self._rec(REC_STATUS_SCHEDULED)
        with mock.patch.object(recorder, '_teardown_active_ffmpeg'), \
                mock.patch('app.concatenator.do_concatenation') as join:
            recorder._handoff_stop_old_recording(self.t.app, old, new)
            self.assertTrue(_wait_until(lambda: join.called))
        reason = self._join_reason(join)
        self.assertIsNotNone(reason, 'a handoff fell back to the join default "Stop time reached"')
        self.assertIn(f'#{new}', reason)
        self.assertNotIn(DEFAULT_JOIN_REASON, reason)


class _PageHarness(unittest.TestCase):
    def setUp(self):
        self.t = make_test_app()
        acc = seed.make_account(name='Acct One')
        self.ch = seed.make_channel(acc, name='Channel One')
        db.session.commit()

    def tearDown(self):
        self.t.cleanup()

    def _page(self, status, *, events=0, pp=None):
        if pp is not None:
            self.t.sandbox_config({'recording': {'post_process': pp}})
        now = datetime.utcnow()
        rec = seed.make_recording(status=status, name='wording page', channel_id=self.ch.id,
                                  start_time=now - timedelta(minutes=30),
                                  stop_time=now + timedelta(minutes=30))
        db.session.flush()
        for i in range(events):
            db.session.add(RecordingEvent(recording_id=rec.id, event_type='STALL_DETECTED',
                                          timestamp=now - timedelta(seconds=events - i),
                                          detail=f'event {i}'))
        db.session.commit()
        resp = self.t.client.get(f'/recordings/{rec.id}')
        self.assertEqual(resp.status_code, 200)
        return resp.get_data(as_text=True)

    @staticmethod
    def _after_join_row(html):
        m = re.search(r'<span class="sk">Finished file</span><span class="sv[^"]*"[^>]*>([^<]*)</span>',
                      html)
        return m.group(1) if m else None


class AfterTheJoinRowTests(_PageHarness):
    """Said as what will happen, without the word "join": "After the join: Converted to
    MP4" read as already done from the first second (dev/changelog/1287)."""

    def test_a_recording_in_progress_says_it_will_be_converted_and_the_ts_deleted(self):
        html = self._page(REC_STATUS_IN_PROGRESS,
                          pp={'enabled': True, 'format': 'mp4', 'delete_source': True})
        self.assertEqual(self._after_join_row(html), 'Will be converted to MP4, and the .ts deleted')
        self.assertIn('Post-process (remux)', html)

    def test_a_scheduled_recording_says_it_too(self):
        html = self._page(REC_STATUS_SCHEDULED,
                          pp={'enabled': True, 'format': 'mkv', 'delete_source': False})
        self.assertEqual(self._after_join_row(html), 'Will be converted to MKV, and the .ts kept')

    def test_conversion_off_says_the_ts_is_the_file(self):
        html = self._page(REC_STATUS_IN_PROGRESS, pp={'enabled': False})
        self.assertEqual(self._after_join_row(html), 'Will stay a .ts file')

    def test_a_finished_recording_has_no_forecast(self):
        html = self._page(REC_STATUS_COMPLETED)
        self.assertIsNone(self._after_join_row(html))


class ConvertingStripSaysWhyTests(_PageHarness):
    def test_the_strip_names_the_format_and_links_the_setting(self):
        html = self._page(REC_STATUS_CONVERTING,
                          pp={'enabled': True, 'format': 'mp4', 'delete_source': True})
        self.assertIn('Converting to <b>MP4</b>', html)
        self.assertIn('/settings?q=recording.post_process.enabled', html)


class PhoneEventLogClipTests(_PageHarness):
    """A completed recording's page was ~7,800px tall at 375 wide, nearly all event log.
    jsdom computes no layout, so this checks the markup the phone CSS keys on; the CSS rule
    itself is checked by reading style.css."""

    def test_a_long_log_is_clipped_with_a_show_all_button(self):
        html = self._page(REC_STATUS_COMPLETED, events=14)
        self.assertIn('class="evlog ev-clip"', html)
        self.assertIn('Show all 14 events', html)

    def test_a_short_log_is_left_alone(self):
        html = self._page(REC_STATUS_COMPLETED, events=4)
        self.assertIn('class="evlog" id="evlog"', html)
        self.assertNotIn('id="ev-more"', html)

    def test_the_clip_and_the_button_exist_on_the_phone_only(self):
        css = open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                                'static', 'css', 'style.css'), encoding='utf-8').read()
        self.assertRegex(css, r'\.ev-more \{ display: none; \}')
        block = css[css.index('.ev-more { display: none; }'):]
        media = block[block.index('@media (max-width: 960px)'):block.index('}\n}') + 3]
        self.assertIn('.evlog.ev-clip > .ev:nth-child(n+11) { display: none; }', media)
        self.assertIn('.ev-more { display: inline-flex; }', media)


if __name__ == '__main__':
    unittest.main()
