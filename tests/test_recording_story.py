"""Trouble is told plainly where people look (dev/docs/UX-first-time-user-2026-10.md
findings 5, 6 and 19; dev/changelog/1270).

Guards four dev/docs/BUGS.md entries, each class naming its own:
- 2026-10-22 09:04:47 PM - a tap on the Recordings list's health pill did nothing;
- 2026-10-22 09:04:48 PM - an opened "why" on the recording page closed on every reload;
- 2026-10-22 09:04:45 PM - the dashboard timeline dropped a recording the moment it finished;
- 2026-10-22 09:04:46 PM - the dashboard's stall tile counted only what was capturing right then.
The guide's partial mark (2026-10-22 09:04:44 PM) is guarded in tests/test_guide_rec_chip_js.py and
tests/test_airing_search.py; its payload half is here.

The recording page's "What happened" card is the feature half: what it says is asserted
against the counters the page shows beside it, and against the alert it shares a sentence
with, so the three cannot drift into three phrasings again.
"""
import html
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import db  # noqa: E402
from app import watchdog as wdmod  # noqa: E402
from app.alerts import RECORDING_RETRYING  # noqa: E402
from app.database import (  # noqa: E402
    Alert, DIAGNOSTICS, EPGEntry, GROUP_FAILOVER, RecordingEvent, RECORDING_RETRY_SCHEDULED,
    RECORDING_STOPPED_EARLY, RESTART_ATTEMPTED, STALL_DETECTED,
)
from app.fmt_utils import fmt_bytes  # noqa: E402
from app.recording_story import dead_stream_clause, fault_sentence, what_happened  # noqa: E402
from tests.support import seed  # noqa: E402
from tests.support.app import make_test_app  # noqa: E402

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HARNESS = os.path.join(REPO, 'tests', 'support', 'recording_story_page.mjs')
JSDOM = os.path.join(REPO, 'node_modules', 'jsdom')
HAVE_NODE = shutil.which('node') is not None and os.path.isdir(JSDOM)


def _story_items(page):
    m = re.search(r'<ul class="story">(.*?)</ul>', page, re.S)
    return [html.unescape(i) for i in re.findall(r'<li>(.*?)</li>', m.group(1), re.S)] if m else []


class _Base(unittest.TestCase):
    def setUp(self):
        self.t = make_test_app()
        self.now = datetime.utcnow().replace(microsecond=0)
        acct = seed.make_account(name='Northwind')
        self.ch = seed.make_channel(acct, name='Northwind One')
        db.session.commit()

    def tearDown(self):
        self.t.cleanup()

    def _finished(self, minutes=60, **kw):
        """A COMPLETED recording one hour long, ended ten minutes ago, captured whole by one
        segment unless the caller says otherwise."""
        stop = self.now - timedelta(minutes=10)
        start = stop - timedelta(minutes=minutes)
        kw.setdefault('recorded_duration_seconds', minutes * 60)
        rec = seed.make_recording(status='COMPLETED', name='The Match', channel_id=self.ch.id,
                                  start_time=start, stop_time=stop, started_at=start, **kw)
        seed.make_segment(rec, self.ch, start, stop)
        db.session.commit()
        return rec

    def _event(self, rec, event_type, offset_min=5, **kw):
        db.session.add(RecordingEvent(recording_id=rec.id, event_type=event_type,
                                      timestamp=rec.start_time + timedelta(minutes=offset_min),
                                      **kw))
        db.session.commit()

    def _page(self, rec):
        return self.t.client.get(f'/recordings/{rec.id}').get_data(as_text=True)


class WhatHappenedCardTests(_Base):
    """The recording page says, in sentences, what went wrong and what ChannelBin did."""

    def test_a_clean_recording_has_nothing_to_tell_and_no_card(self):
        rec = self._finished()
        self.assertEqual(what_happened(rec), [])
        page = self._page(rec)
        self.assertIn('id="story-slot"', page)
        self.assertNotIn('id="panel-story"', page)

    def test_stalls_and_restarts_are_counted_the_way_the_restarts_row_counts_them(self):
        rec = self._finished(total_stall_count=6, total_restart_count=1)
        for i in range(6):
            self._event(rec, RESTART_ATTEMPTED, offset_min=5 + i)
        page = self._page(rec)
        self.assertIn('6 tried · 1 got the stream back', page)
        items = _story_items(page)
        self.assertIn('The stream stopped delivering video 6 times. ChannelBin restarted the '
                      'capture 6 times, and 1 of those restarts got the stream back.', items)

    def test_a_dead_stream_wait_is_worded_the_way_the_alert_words_it(self):
        """One phrasing, not three: the alert's clause comes from the same function."""
        rec = self._finished(total_stall_count=3)
        cause = '[http @ 0x1] HTTP error 403 Forbidden'
        self._event(rec, RECORDING_RETRY_SCHEDULED,
                    extra_data=json.dumps({'attempt': 1, 'streak': 3, 'cause': cause}))
        clause = dead_stream_clause(3, cause)
        items = _story_items(self._page(rec))
        self.assertTrue(any(i.startswith(clause[0].upper() + clause[1:]) for i in items), items)
        self.assertTrue(any('HTTP 403 Forbidden' in i for i in items), items)

        rec.stop_time = self.now + timedelta(hours=1)
        db.session.commit()
        wdmod.raise_recording_retrying_alert(rec, 3, 1, 10, cause, 60)
        body = Alert.query.filter_by(alert_type=RECORDING_RETRYING).one().body
        self.assertIn(dead_stream_clause(3, cause, 'Northwind One'), body)

    def test_a_retry_from_before_the_streak_was_recorded_still_reads(self):
        rec = self._finished(total_stall_count=2)
        self._event(rec, RECORDING_RETRY_SCHEDULED, detail='Retrying (attempt 1/10) in 1 min')
        self._event(rec, RECORDING_RETRY_SCHEDULED, detail='Retrying (attempt 2/10) in 2 min')
        items = what_happened(rec)
        self.assertIn('The channel kept failing right after each reconnect, so ChannelBin '
                      'waited before trying again (2 waits).', items)

    def test_a_failover_a_late_start_and_an_early_stop_are_each_said(self):
        start = self.now - timedelta(minutes=70)
        rec = self._finished(total_stall_count=1, program_start_time=start - timedelta(minutes=20))
        rec.scheduled_stop_time = rec.stop_time + timedelta(minutes=40)
        db.session.commit()
        self._event(rec, GROUP_FAILOVER)
        self._event(rec, RECORDING_STOPPED_EARLY, detail='Stopped 40.0 min before scheduled end')
        items = what_happened(rec)
        self.assertIn('It started 20m after the program began, so the start of the program '
                      'is not in the file.', items)
        self.assertIn('It moved to another channel in its group 1 time.', items)
        self.assertIn('You stopped it 40m before its scheduled end.', items)

    def test_a_finished_recording_with_trouble_leads_with_the_file_that_is_left(self):
        """dev/changelog/1285: the line a reader opens the card for comes first. As the last
        one it sat below the fold on a phone."""
        rec = self._finished(total_stall_count=1, final_file_size=91_100_000)
        items = what_happened(rec)
        self.assertEqual(items[0],
                         f'You still have a file: 1h 0m 0s of video, {fmt_bytes(91_100_000)}.')

    def test_the_card_rides_the_pages_refresh(self):
        """The slot is in the 15s swap list, so a live page's account grows as it happens."""
        page = self._page(self._finished(total_stall_count=1))
        self.assertIn('id="panel-story"', page)
        self.assertRegex(page, r"const SWAP_IDS = \[[^\]]*'story-slot'")


class FaultIsNamedOnlyWhereKnownTests(unittest.TestCase):
    """Whether the provider, the network or the channel was at fault is said only where the
    app actually has evidence for it."""

    def test_the_providers_placeholder_clip_is_the_providers(self):
        self.assertIn('the channel was down at the provider\'s end',
                      fault_sentence([], placeholder=True))

    def test_a_refused_login_is_the_providers(self):
        self.assertEqual(fault_sentence([], login_refused=True),
                         'The provider refused the account\'s login.')

    def test_a_specific_http_answer_beats_the_generic_one(self):
        s = fault_sentence(['Server returned 4XX Client Error, but not one of 40{0,1,3,4}',
                            '[http @ 0x1] HTTP error 405 Method Not Allowed'])
        self.assertIn('HTTP 405 Method Not Allowed', s)
        self.assertNotIn('40{0', s)

    def test_a_generic_http_answer_is_reduced_to_its_class(self):
        s = fault_sentence(['Server returned 4XX Client Error, but not one of 40{0,1,3,4}'])
        self.assertIn('an HTTP 4xx error', s)

    def test_an_unreachable_server_does_not_pick_between_network_and_provider(self):
        s = fault_sentence(['Connection to tcp://x.test:80 failed: Connection timed out'])
        self.assertIn('Connection timed out', s)
        self.assertIn('cannot tell which', s)

    def test_no_evidence_names_no_one(self):
        s = fault_sentence(['Error during demuxing: Input/output error'])
        self.assertEqual(s, 'Nothing ChannelBin saw says whether the provider, the network or '
                            'the channel was at fault.')


class GuidePartialPayloadTests(_Base):
    """dev/docs/BUGS.md 2026-10-22 09:04:44 PM: the guide's payload says why a finished recording is
    partial, on the program cell and on the row's recording band; a whole one says nothing."""

    def _guide(self, rec):
        self.ch.in_guide = True
        db.session.add(EPGEntry(channel_id=self.ch.id, title='The Match',
                                start_time=rec.start_time, stop_time=rec.stop_time))
        db.session.commit()
        fmt = '%Y-%m-%dT%H:%M:%S'
        start = (rec.start_time - timedelta(hours=1)).strftime(fmt)
        end = (rec.stop_time + timedelta(hours=1)).strftime(fmt)
        data = self.t.client.get(f'/api/guide/epg?start={start}&end={end}').get_json()
        row = next(r for r in data['channels'] if r['id'] == self.ch.id)
        prog = next(p for p in row['programs'] if p['recording_id'] == rec.id)
        band = next(b for b in row['recordings'] if b['id'] == rec.id)
        return prog, band

    def test_a_three_minute_capture_of_an_hour_is_partial(self):
        rec = self._finished(recorded_duration_seconds=180)
        prog, band = self._guide(rec)
        self.assertEqual(prog['recording_partial'], '3m captured of 1h scheduled.')
        self.assertEqual(band['partial'], prog['recording_partial'])

    def test_a_whole_recording_is_not(self):
        prog, band = self._guide(self._finished())
        self.assertIsNone(prog['recording_partial'])
        self.assertIsNone(band['partial'])


class DashboardFinishedRecordingTests(_Base):
    """The dashboard a few minutes after a stalled recording ended still shows it."""

    def _dash(self):
        page = self.t.client.get('/').get_data(as_text=True)
        blob = re.search(r'<script type="application/json" id="dash-timeline">(.*?)</script>',
                         page, re.S).group(1)
        tiles = {k.strip(): (re.sub(r'<[^>]+>', '', v).strip(), s.strip()) for k, v, s in
                 re.findall(r'<div class="m-k">(.*?)</div>\s*<div class="m-v">(.*?)</div>'
                            r'\s*<div class="m-s">(.*?)</div>', page, re.S)}
        return json.loads(blob), tiles

    def _stall(self, rec, minutes_ago):
        db.session.add(RecordingEvent(recording_id=rec.id, event_type=STALL_DETECTED,
                                      timestamp=self.now - timedelta(minutes=minutes_ago)))
        db.session.commit()

    def test_a_just_finished_recording_stays_on_the_timeline_and_says_how_it_went(self):
        """dev/docs/BUGS.md 2026-10-22 09:04:45 PM."""
        rec = self._finished(total_stall_count=6, recorded_duration_seconds=180)
        timeline, _ = self._dash()
        bar = next((r for r in timeline['recordings'] if r['id'] == rec.id), None)
        self.assertIsNotNone(bar, 'the finished recording is missing from the timeline')
        self.assertTrue(bar['trouble'])
        self.assertIn('Partial: 3m captured of 1h scheduled.', bar['note'])
        self.assertIn('Finished after 6 stalls.', bar['note'])

    def test_a_recording_that_finished_long_ago_is_not_drawn(self):
        stop = self.now - timedelta(hours=5)
        rec = seed.make_recording(status='COMPLETED', name='old', channel_id=self.ch.id,
                                  start_time=stop - timedelta(hours=1), stop_time=stop,
                                  started_at=stop - timedelta(hours=1))
        db.session.commit()
        timeline, _ = self._dash()
        self.assertNotIn(rec.id, [r['id'] for r in timeline['recordings']])

    def test_the_stall_tile_counts_the_last_hour_whatever_the_recording_is_doing_now(self):
        """dev/docs/BUGS.md 2026-10-22 09:04:46 PM."""
        rec = self._finished(total_stall_count=6)
        for m in (5, 12, 20, 30):
            self._stall(rec, m)
        self._stall(rec, 70)   # outside the hour: not this hour's stall
        _, tiles = self._dash()
        self.assertEqual(tiles['Stalls, 60 min'], ('4', 'on 1 recording'))

    def test_the_error_tile_says_it_counts_log_errors(self):
        _, tiles = self._dash()
        self.assertIn('Errors, 60 min', tiles)
        self.assertNotIn('Errors, last hour', tiles)


@unittest.skipUnless(HAVE_NODE, 'node and node_modules/jsdom are required')
class BrowserHalfTests(_Base):
    """The two halves that live in the browser: the list's pill and the page's "why"."""

    def _observe(self):
        rec = self._finished(total_stall_count=2)
        db.session.add(RecordingEvent(
            recording_id=rec.id, event_type=DIAGNOSTICS, segment_number=0,
            timestamp=rec.start_time + timedelta(minutes=5),
            extra_data=json.dumps({'kind': 'capture_stderr',
                                   'stderr_tail': 'Error during demuxing: Input/output error'})))
        db.session.commit()
        tmp = tempfile.mkdtemp(prefix='recording_story_js_')
        try:
            pages = {'list.html': self.t.client.get('/recordings'),
                     'detail.html': self.t.client.get(f'/recordings/{rec.id}')}
            for name, resp in pages.items():
                with open(os.path.join(tmp, name), 'w', encoding='utf-8') as f:
                    f.write(resp.get_data(as_text=True))
            with open(os.path.join(tmp, 'ids.json'), 'w', encoding='utf-8') as f:
                json.dump({'list_row': rec.id, 'detail_rec': rec.id, 'detail_seg': 0}, f)
            proc = subprocess.run(['node', HARNESS, tmp, REPO], capture_output=True,
                                  text=True, timeout=120, cwd=REPO)
            self.assertEqual(proc.returncode, 0, proc.stderr)
            return json.loads(proc.stdout)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_a_tap_on_the_health_pill_opens_the_recording(self):
        """dev/docs/BUGS.md 2026-10-22 09:04:47 PM."""
        obs = self._observe()['list']
        self.assertEqual(obs['errors'], [])
        self.assertTrue(obs['found_pill'])
        self.assertEqual(obs['pill_navigations'], 1)
        self.assertTrue(obs['found_other_tip'])
        self.assertEqual(obs['other_tip_navigations'], 0,
                         'every other tooltip on the row should still keep its click')

    def test_an_opened_why_survives_a_reload(self):
        """dev/docs/BUGS.md 2026-10-22 09:04:48 PM."""
        obs = self._observe()['detail']
        self.assertEqual(obs['errors'], [])
        self.assertTrue(obs['found_why'])
        self.assertTrue(obs['open_after_reload'], obs['stored'])


if __name__ == '__main__':
    unittest.main()
