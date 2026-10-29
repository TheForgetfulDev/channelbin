"""What a finished recording says about itself (dev/docs/UX-first-time-user-2026-10-retest.md
findings 4-7; dev/changelog/1287).

Guards three dev/docs/BUGS.md entries, each class naming its own:
- 2026-10-25 @ 07:39:17 pm - a red "rc 255, it gave up" beside every stop ChannelBin made;
- 2026-10-25 @ 07:39:18 pm - a dead stream's cause named ChannelBin's own stop;
- 2026-10-25 @ 07:39:19 pm - a clean recording a few seconds long asked who was at fault.

The rest is the feature half: the lengths a finished page gives agree with each other, the
restart sentence counts what it says it counts, the waiting-to-retry bar names a control
where it is, and the user's own Stop is said as the user's rather than as trouble.
"""
import html
import json
import os
import re
import sys
import unittest
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import db  # noqa: E402
from app.database import (  # noqa: E402
    EPGEntry, RecordingEvent, RECORDING_STOPPED_EARLY, RESTART_ATTEMPTED,
)
from app.proc_utils import stderr_cause  # noqa: E402
from app.recording_story import (  # noqa: E402
    dead_stream_clause, partial_note, stopped_early_note, what_happened,
)
from tests.support import seed  # noqa: E402
from tests.support.app import make_test_app  # noqa: E402

# What ffmpeg 7.1 writes when ChannelBin's SIGTERM interrupts a stalled read - measured on
# this box against a stalled HTTP feed (dev/changelog/1287).
OUR_STOP_TAIL = ('[in#0/mpegts @ 0x599c26402080] Error during demuxing: Immediate exit requested\n'
                 'Exiting normally, received signal 15.')


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

    def _rec(self, seconds, recorded, segments=None, **kw):
        """A COMPLETED recording `seconds` long ending two minutes ago. `segments` is a list of
        (start offset, end offset, extra segment columns); one whole-window segment by default."""
        stop = self.now - timedelta(minutes=2)
        start = stop - timedelta(seconds=seconds)
        rec = seed.make_recording(status='COMPLETED', name='The Match', channel_id=self.ch.id,
                                  start_time=start, stop_time=stop, started_at=start,
                                  recorded_duration_seconds=recorded, **kw)
        for n, (a, b, extra) in enumerate(segments or [(0, seconds, {})], start=1):
            seed.make_segment(rec, self.ch, start + timedelta(seconds=a),
                              start + timedelta(seconds=b), segment_number=n, **extra)
        db.session.commit()
        return rec

    def _stopped_early(self, gap=False):
        """Scheduled for 8m, stopped by the user after 2m 38s, 2m 35s of video in the file.
        `gap` leaves 30 s in the middle with no capture running."""
        segs = ([(0, 60, {}), (90, 158, {'exit_reason': 'MANUAL_STOP'})] if gap
                else [(0, 158, {'exit_reason': 'MANUAL_STOP'})])
        rec = self._rec(158, 155 if not gap else 125, segs)
        rec.scheduled_stop_time = rec.start_time + timedelta(minutes=8)
        # What recorder._log_manual_stop_and_commit writes for a Stop over a minute early.
        db.session.add(RecordingEvent(recording_id=rec.id, event_type=RECORDING_STOPPED_EARLY,
                                      timestamp=rec.stop_time))
        db.session.commit()
        return rec

    def _page(self, rec):
        resp = self.t.client.get(f'/recordings/{rec.id}')
        self.assertEqual(resp.status_code, 200)
        return resp.get_data(as_text=True)


class ExitCodeOfOurOwnStopTests(_Base):
    """dev/docs/BUGS.md 2026-10-25 @ 07:39:17 pm."""

    def _exit_codes(self, page):
        return re.findall(r'<span class="exit-code([^"]*)"[^>]*>(rc \d+)</span>', page)

    def test_255_after_a_stop_channelbin_made_is_not_a_failure(self):
        rec = self._rec(600, 600, [
            (0, 300, {'exit_reason': 'STALL_KILLED', 'ffmpeg_exit_code': 255}),
            (305, 600, {'exit_reason': 'MANUAL_STOP', 'ffmpeg_exit_code': 255}),
        ])
        codes = self._exit_codes(self._page(rec))
        self.assertEqual(len(codes), 2, codes)
        for cls, text in codes:
            self.assertEqual(text, 'rc 255')
            self.assertNotIn('bad', cls)

    def test_255_from_ffmpeg_quitting_on_its_own_is_still_red(self):
        rec = self._rec(600, 600, [
            (0, 600, {'exit_reason': 'PROCESS_EXITED', 'ffmpeg_exit_code': 255}),
        ])
        [(cls, text)] = self._exit_codes(self._page(rec))
        self.assertIn('bad', cls)


class CauseIsNotOurOwnStopTests(unittest.TestCase):
    """dev/docs/BUGS.md 2026-10-25 @ 07:39:18 pm."""

    def test_the_line_our_sigterm_produces_is_not_a_cause(self):
        self.assertEqual(stderr_cause(OUR_STOP_TAIL), '')

    def test_a_real_cause_before_it_is_still_found(self):
        tail = '[http @ 0x1] HTTP error 403 Forbidden\n' + OUR_STOP_TAIL
        self.assertEqual(stderr_cause(tail), '[http @ 0x1] HTTP error 403 Forbidden')

    def test_the_plain_account_never_quotes_ffmpeg(self):
        for raw in (OUR_STOP_TAIL.split('\n')[0],
                    '[in#0/mpegts @ 0x1] Error during demuxing: Input/output error'):
            self.assertEqual(dead_stream_clause(3, raw), 'the channel failed on 3 connections in a row')

    def test_a_cause_it_can_word_is_worded(self):
        self.assertEqual(dead_stream_clause(3, '[http @ 0x1] HTTP error 403 Forbidden'),
                         'the channel failed on 3 connections in a row '
                         '(the provider\'s server answered HTTP 403 Forbidden)')
        self.assertIn('could not reach the provider\'s server: Connection refused',
                      dead_stream_clause(2, '[tcp @ 0x1] Connection to tcp://x.test failed: '
                                            'Connection refused'))
        self.assertIn('(the provider closed the stream)',
                      dead_stream_clause(2, '[http @ 0x1] Will reconnect at 1 in 0 second(s), '
                                            'error=End of file.'))


class NoFaultWhereNothingWasLostTests(_Base):
    """dev/docs/BUGS.md 2026-10-25 @ 07:39:19 pm."""

    def test_a_file_longer_than_its_capture_alone_names_no_fault(self):
        lines = what_happened(self._rec(600, 630))
        self.assertTrue(any('30s longer than the time ChannelBin spent capturing' in s
                            for s in lines), lines)
        self.assertFalse(any('at fault' in s for s in lines), lines)

    def test_with_a_stall_as_well_the_fault_sentence_comes_back(self):
        lines = what_happened(self._rec(600, 630, total_stall_count=1))
        self.assertTrue(any('at fault' in s for s in lines), lines)


class RestartSentenceTests(_Base):
    """The noun follows the count beside it: "0 of those restart" (finding 5)."""

    def _line(self, attempts, back):
        rec = self._rec(600, 600, total_stall_count=attempts, total_restart_count=back)
        return next(s for s in what_happened(rec, restart_attempts=attempts)
                    if s.startswith('The stream stopped'))

    def test_none_of_several(self):
        self.assertTrue(self._line(4, 0).endswith(
            'ChannelBin restarted the capture 4 times, and none of those restarts got the '
            'stream back.'))

    def test_some_of_several(self):
        self.assertTrue(self._line(4, 2).endswith('and 2 of those restarts got the stream back.'))

    def test_a_single_restart(self):
        self.assertTrue(self._line(1, 0).endswith(
            'ChannelBin restarted the capture once, and it did not get the stream back.'))
        self.assertTrue(self._line(1, 1).endswith('once, and it got the stream back.'))


class LengthsAgreeTests(_Base):
    """Finding 4: one file said one way, and the gap squared with Downtime."""

    def test_the_captured_row_and_the_story_give_one_length(self):
        page = self._page(self._stopped_early())
        self.assertRegex(page, r'<span class="sk">Captured</span>.*?2m 35s of 8m scheduled')
        self.assertEqual(_story_items(page)[0], 'You still have a file: 2m 35s of video.')
        self.assertIn('2m 35s captured</span>', page)   # the timeline under the header

    def test_the_gap_sentence_names_downtime_when_it_is_the_larger(self):
        rec = self._rec(600, 477, [(0, 200, {}), (323, 600, {})], total_downtime_seconds=139)
        lines = what_happened(rec)
        self.assertIn('Nothing reached the file for 2m 19s, and for 2m 3s of that no capture '
                      'was running at all.', lines)
        self.assertRegex(self._page(rec), r'<span class="sk">Downtime</span><span class="sv[^"]*"[^>]*>2m 19s')

    def test_a_gap_larger_than_downtime_is_said_alone(self):
        rec = self._rec(600, 477, [(0, 200, {}), (323, 600, {})], total_downtime_seconds=60)
        self.assertIn('2m 3s of the recording window with nothing capturing at all.',
                      what_happened(rec))


class StoppedEarlyByYouTests(_Base):
    """Finding 6, decided 2026-10-25: the user's own Stop is said as the user's, in a quiet
    mark, not as Partial and trouble. Anything else missing still makes it partial."""

    def test_the_story_helpers_tell_the_two_apart(self):
        rec = self._stopped_early()
        self.assertEqual(stopped_early_note(rec), 'You stopped it after 2m 35s of 8m scheduled.')
        self.assertIsNone(partial_note(rec))
        gapped = self._stopped_early(gap=True)
        self.assertIsNone(stopped_early_note(gapped))
        self.assertIsNotNone(partial_note(gapped))

    def test_the_guide_payload_carries_it_apart_from_partial(self):
        rec = self._stopped_early()
        self.ch.in_guide = True
        db.session.add(EPGEntry(channel_id=self.ch.id, title='The Match',
                                start_time=rec.start_time, stop_time=rec.scheduled_stop_time))
        db.session.commit()
        fmt = '%Y-%m-%dT%H:%M:%S'
        data = self.t.client.get(
            f'/api/guide/epg?start={(rec.start_time - timedelta(hours=1)).strftime(fmt)}'
            f'&end={(rec.scheduled_stop_time + timedelta(hours=1)).strftime(fmt)}').get_json()
        row = next(r for r in data['channels'] if r['id'] == self.ch.id)
        prog = next(p for p in row['programs'] if p['recording_id'] == rec.id)
        self.assertIsNone(prog['recording_partial'])
        self.assertEqual(prog['recording_stopped'], stopped_early_note(rec))

    def test_the_dashboard_says_it_and_does_not_call_it_trouble(self):
        rec = self._stopped_early()
        page = self.t.client.get('/').get_data(as_text=True)
        blob = re.search(r'<script type="application/json" id="dash-timeline">(.*?)</script>',
                         page, re.S).group(1)
        bar = next(r for r in json.loads(blob)['recordings'] if r['id'] == rec.id)
        self.assertFalse(bar['trouble'])
        self.assertEqual(bar['note'], 'You stopped it after 2m 35s of 8m scheduled.')

    def test_the_list_and_the_page_mark_it_quietly(self):
        rec = self._stopped_early()
        listing = self.t.client.get('/recordings').get_data(as_text=True)
        self.assertIn('<span class="dur-flag dur-quiet tip-plain" '
                      'data-tip="You stopped it after 2m 35s of 8m scheduled.">', listing)
        page = self._page(rec)
        self.assertIn('<span class="dur-flag dur-quiet">', page)
        self.assertNotRegex(page, r'<span class="sk">Captured</span><span class="sv warn')


class WhatComesNextTests(_Base):
    """Finding 7, and finding 5's Abort: the page says what will happen, and where the
    controls it names are."""

    def test_the_retry_bar_says_where_abort_is(self):
        rec = self._rec(600, None)
        rec.status = 'RETRYING'
        rec.stop_time = self.now + timedelta(hours=1)
        db.session.commit()
        self.assertIn('Abort, in the <svg class="icon" data-icon="more"', self._page(rec))

    def test_the_segments_table_says_a_joined_segments_file_is_gone(self):
        rec = self._rec(600, 600)
        rec.output_path = '/nonexistent/The Match.mp4'
        db.session.commit()
        page = self._page(rec)
        self.assertIn('· no longer on disk</span>', page)
        self.assertIn('joined into the finished file and then deleted', page)

    def test_not_while_capturing(self):
        rec = self._rec(600, None)
        rec.status = 'IN_PROGRESS'
        rec.stop_time = self.now + timedelta(hours=1)
        db.session.commit()
        self.assertNotIn('no longer on disk', self._page(rec))


class RestartAttemptsReadFromTheLogTests(_Base):
    """The page's Restarts row and the story count attempts from the same events."""

    def test_the_page_story_uses_the_logged_attempts(self):
        rec = self._rec(600, 600, total_stall_count=3, total_restart_count=0)
        for i in range(3):
            db.session.add(RecordingEvent(recording_id=rec.id, event_type=RESTART_ATTEMPTED,
                                          timestamp=rec.start_time + timedelta(minutes=i + 1)))
        db.session.commit()
        items = _story_items(self._page(rec))
        self.assertTrue(any(i.endswith('3 times, and none of those restarts got the stream '
                                       'back.') for i in items), items)


if __name__ == '__main__':
    unittest.main()
