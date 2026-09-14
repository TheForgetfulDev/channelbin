"""Tier 2 - naming a finished segment whose video arrived faster than the clock, and
flagging the recording that holds it.

Guards dev/docs/BUGS.md 2026-09-14 @ 05:33:34 PM ET. A provider re-serving the same stretch
of coverage writes bytes at full rate and keeps ffmpeg's frame counter moving, so it never
stalls, never restarts and never trips a liveness check. The recording that exposed this ran
2h18m and 31.9 GB on one frozen lap of one race and the recordings list rendered it as a
green "clean capture" tick, with the only trace anywhere in the app being "222.1% of expected
frames" on a diagnostics line nobody can read.

Two halves, and this module is the second one. The live detector (dev/changelog/964) kills a
capture whose ROLLING delivery ratio stays above watchdog.fast_delivery_ratio; what is left
over is every segment that ran fast but stayed under it. Those are not proven bad - they hold
real content and length alone cannot say which parts are which - so they are kept, and the
recording says so instead.

The trigger is SURPLUS SECONDS, not the ratio, and that is the measurement rather than a
preference. The only legitimate source of early content is the per-connect back-buffer, a
fixed 13-29s (dev/changelog/942), so a constant threshold separates it identically at every
segment length. Measured over every segment in the real database carrying a content duration:
the worst honest surplus is +40.1s on a 47-minute segment reading 1.01x, with nothing between
there and the placeholder clips at +594s - while two ordinary 15s back-buffer segments read
2.43x and 1.60x, both of which a 1.5x ratio rule would have to special-case away.

The ratio is still what explains a finding in prose, so both numbers reach the event and only
one decides.

Fixtures are synthesized locally with ffmpeg - no network, no provider streams, no /dvr. The
recording that motivated this work has been deleted and was never available as one.
"""
import json
import os
import shutil
import subprocess
import sys
import unittest
from datetime import datetime, timedelta
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import db  # noqa: E402
from app.concatenator import (  # noqa: E402
    _measure_segment_content_durations, delivery_surplus_seconds, fast_delivery_detail,
    fast_delivery_findings, segment_wall_seconds,
)
from app.config import load_config  # noqa: E402
from app.database import (  # noqa: E402
    DIAGNOSTICS, REC_STATUS_COMPLETED, Recording, RecordingEvent, RecordingSegment,
)
from tests.support import seed  # noqa: E402
from tests.support.app import make_test_app  # noqa: E402

_HAVE_FFMPEG = bool(shutil.which('ffmpeg') and shutil.which('ffprobe'))


def _ffmpeg(*args):
    subprocess.run(['ffmpeg', '-v', 'error', '-y', *args], check=True, timeout=120)


def _measured(seg_id, seg_num, content, wall):
    """One row in the shape _measure_segment_content_durations hands the classifier."""
    return (seg_id, seg_num, content, wall)


class DeliverySurplusMathTests(unittest.TestCase):
    """The pure half: no app, no rows, so the threshold's behavior at its own boundary is
    checkable without a capture."""

    def test_surplus_is_content_beyond_the_seconds_the_capture_ran(self):
        self.assertAlmostEqual(delivery_surplus_seconds(31122.0, 8275.0), 22847.0, places=1)
        self.assertAlmostEqual(delivery_surplus_seconds(709.8, 693.5), 16.3, places=1)

    def test_a_segment_that_delivered_less_than_real_time_reports_a_negative(self):
        """A minus is a real answer (the feed stayed connected and fell behind), not a
        clamp at zero - netting the two directions together is the defect the capture-gap
        stats were split apart to fix."""
        self.assertAlmostEqual(delivery_surplus_seconds(844.9, 867.5), -22.6, places=1)

    def test_an_unmeasurable_pair_says_so_rather_than_reporting_zero(self):
        """None is "cannot say" and 0 is "arrived exactly on time" - a caller that treats
        them alike flags every segment whose clock is missing."""
        self.assertIsNone(delivery_surplus_seconds(None, 100.0))
        self.assertIsNone(delivery_surplus_seconds(100.0, None))
        self.assertIsNone(delivery_surplus_seconds(100.0, 0))
        self.assertIsNone(delivery_surplus_seconds(100.0, -5))

    def test_the_threshold_is_exclusive_at_its_own_boundary(self):
        rows = [_measured(1, 3, 220.0, 100.0)]  # +120.0 exactly
        self.assertEqual(fast_delivery_findings(rows, 120), [])
        rows = [_measured(1, 3, 220.5, 100.0)]  # +120.5
        self.assertEqual([f.segment_number for f in fast_delivery_findings(rows, 120)], [3])

    def test_the_worst_honest_surplus_ever_measured_here_stays_under_the_default(self):
        """+40.1s on a 47-minute segment (1.01x) is the largest surplus in the real
        database that was not a placeholder clip. The default sits three times clear of it,
        and this test is what notices if that margin is ever narrowed."""
        default = load_config()['watchdog']['fast_delivery_surplus_seconds']
        self.assertEqual(fast_delivery_findings([_measured(1, 23, 2874.1, 2834.0)], default), [])

    def test_a_short_back_buffer_segment_does_not_fire_although_its_ratio_is_high(self):
        """The reason this is a surplus rule and not a ratio one: these two are ordinary
        15-second reconnects carrying a normal back-buffer, and they read 2.43x and 1.60x.
        A ratio trigger has to bolt a minimum-length gate on to survive them; a constant
        does not, because the back-buffer is itself a constant."""
        default = load_config()['watchdog']['fast_delivery_surplus_seconds']
        rows = [_measured(1, 8, 37.5, 15.4), _measured(2, 11, 24.5, 15.3)]
        self.assertEqual(fast_delivery_findings(rows, default), [])

    def test_a_long_segment_running_quietly_fast_fires_although_its_ratio_is_low(self):
        """The other direction, and the one a ratio rule misses outright: eight hours of
        capture holding 48 minutes more than the clock is 1.10x - under any ratio threshold
        worth setting, and three quarters of an hour of video nobody can account for."""
        rows = [_measured(1, 4, 28800.0 + 2880.0, 28800.0)]
        findings = fast_delivery_findings(rows, 120)
        self.assertEqual(len(findings), 1)
        self.assertLess(findings[0].ratio, 1.15)

    def test_a_zero_threshold_disables_the_flag_entirely(self):
        """Matching every other trip-wire in the watchdog config block."""
        rows = [_measured(1, 19, 31122.0, 8275.0)]
        self.assertEqual(fast_delivery_findings(rows, 0), [])
        self.assertEqual(fast_delivery_findings(rows, None), [])

    def test_a_segment_with_no_measurement_is_never_flagged(self):
        """NULL content means ffprobe could not read the file. Unknown is not proven fast,
        exactly as an untested group member is not proven format-mismatched."""
        rows = [_measured(1, 2, None, 100.0), _measured(2, 3, 5000.0, None)]
        self.assertEqual(fast_delivery_findings(rows, 120), [])

    def test_the_finding_carries_the_ratio_from_the_one_shared_helper(self):
        """proc_utils.delivery_ratio() is the single home for this measurement
        (dev/changelog/964) - the live detector and the sentence describing a finished
        segment must not be able to disagree about what a delivery rate is."""
        from app.proc_utils import delivery_ratio
        finding = fast_delivery_findings([_measured(1, 19, 31122.0, 8275.0)], 120)[0]
        self.assertEqual(finding.ratio, delivery_ratio(31122.0, 8275.0))
        self.assertAlmostEqual(finding.ratio, 3.76, places=2)


class EventWordingTests(unittest.TestCase):
    """What the event is allowed to claim. This is the half that shipped wrong once."""

    def _detail(self):
        return fast_delivery_detail(
            fast_delivery_findings([_measured(1, 19, 31122.0, 8275.0)], 120)[0])

    def test_the_sentence_carries_every_number_behind_the_finding(self):
        detail = self._detail()
        self.assertIn('Segment 19', detail)
        self.assertIn('8h 38m 42s', detail)   # content
        self.assertIn('2h 17m 55s', detail)   # wall clock
        self.assertIn('6h 20m 47s', detail)   # the surplus that triggered it
        self.assertIn('3.76x real time', detail)

    def test_it_never_claims_the_content_was_buffered(self):
        """An earlier draft said the provider "served buffered content faster than live, so
        the finished file holds more than the recording window" - inferred from the ratio
        and false on the very recording that motivated the work, where every frame across
        8h38m read the same lap of the same race. The event states the measurement and
        names both possible causes without choosing one."""
        detail = self._detail()
        self.assertIn('buffered content served after a reconnect', detail)
        self.assertIn('the same stretch re-served over and over', detail)
        self.assertIn('Nothing here compares frames', detail)
        self.assertNotIn('holds more than the recording window', detail)

    def test_it_says_the_segment_was_kept(self):
        """The difference from a discarded placeholder is the whole point: there is real
        content here, so the reader is told to check rather than told it was thrown away."""
        self.assertIn('kept and joined into the final file', self._detail())


@unittest.skipUnless(_HAVE_FFMPEG, 'ffmpeg/ffprobe not installed')
class ConcatTimeDisclosureTests(unittest.TestCase):
    """The measuring pass itself: the event, the rollup, and re-entry."""

    def setUp(self):
        self.t = make_test_app()
        self.now = datetime.utcnow()

    def tearDown(self):
        self.t.cleanup()

    def _rec(self):
        rec = seed.make_recording(start_time=self.now - timedelta(seconds=3600),
                                  stop_time=self.now)
        db.session.commit()
        return rec

    def _ts(self, name, seconds):
        path = os.path.join(self.t._tmpdir, name)
        _ffmpeg('-f', 'lavfi', '-i', 'testsrc=size=192x108:rate=10', '-t', str(seconds),
                '-c:v', 'libx264', '-preset', 'ultrafast', '-bf', '0',
                '-pix_fmt', 'yuv420p', path)
        return path

    def _seg(self, rec, number, content_seconds, wall_seconds):
        """A segment holding content_seconds of real video whose clocks say it ran for
        wall_seconds - which is exactly the shape a fast feed leaves behind."""
        seg = RecordingSegment(
            recording_id=rec.id, segment_number=number,
            file_path=self._ts(f'seg{number}.ts', content_seconds),
            started_at=self.now - timedelta(seconds=wall_seconds),
            ended_at=self.now, bytes_recorded=4096)
        db.session.add(seg)
        db.session.commit()
        return seg

    def _measure(self, rec, segs, threshold=5):
        cfg = load_config()
        cfg['watchdog'] = dict(cfg['watchdog'], fast_delivery_surplus_seconds=threshold)
        with patch('app.config.load_config', return_value=cfg):
            _measure_segment_content_durations(rec.id, segs)
        db.session.expire_all()

    def _events(self, rec_id):
        return [e for e in RecordingEvent.query.filter_by(
            recording_id=rec_id, event_type=DIAGNOSTICS).all()
            if e.extra_data and json.loads(e.extra_data).get('kind') == 'delivery_rate']

    def test_a_segment_over_the_threshold_gets_an_event_and_moves_the_rollup(self):
        rec = self._rec()
        fast = self._seg(rec, 0, content_seconds=8, wall_seconds=1)

        self._measure(rec, [fast])

        events = self._events(rec.id)
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].segment_number, 0)
        self.assertIn('real time', events[0].detail)
        row = db.session.get(Recording, rec.id)
        self.assertEqual(row.fast_delivery_segment_count, 1)
        self.assertAlmostEqual(row.fast_delivery_seconds, 8.0, delta=0.6)

    def test_a_recording_whose_video_arrived_on_time_records_a_zero_not_a_null(self):
        """0 is the real answer "this was checked and its video arrived on time"; NULL is
        the different fact that nothing ever looked. A page cannot tell a reader which one
        it is looking at if the two collapse."""
        rec = self._rec()
        clean = self._seg(rec, 0, content_seconds=6, wall_seconds=6)

        self._measure(rec, [clean])

        self.assertEqual(self._events(rec.id), [])
        row = db.session.get(Recording, rec.id)
        self.assertEqual(row.fast_delivery_segment_count, 0)
        self.assertEqual(row.fast_delivery_seconds, 0)

    def test_retrying_the_join_does_not_report_the_same_finding_twice(self):
        """"Retry join" re-enters this function from the top, re-probing every segment. The
        guard reads the committed events rather than inferring from the stored duration:
        the duration is written by the phase BEFORE the event, so inferring would silence
        a finding permanently if a crash landed between the two."""
        rec = self._rec()
        fast = self._seg(rec, 0, content_seconds=8, wall_seconds=1)

        self._measure(rec, [fast])
        self._measure(rec, [fast])

        self.assertEqual(len(self._events(rec.id)), 1)
        self.assertEqual(db.session.get(Recording, rec.id).fast_delivery_segment_count, 1)

    def test_the_event_extra_carries_only_what_has_no_column(self):
        """CLAUDE.md's strict partition: content duration, started_at and ended_at are all
        columns on the segment row and the ratio derives from them, so the only thing left
        for extra_data is which measurement this is."""
        rec = self._rec()
        self._measure(rec, [self._seg(rec, 0, content_seconds=8, wall_seconds=1)])

        self.assertEqual(json.loads(self._events(rec.id)[0].extra_data),
                         {'kind': 'delivery_rate'})

    def test_a_zero_threshold_measures_durations_but_flags_nothing(self):
        """Disabling the flag must not disable the measurement underneath it - the content
        duration is what the frame-deficit weighting reads (dev/changelog/962)."""
        rec = self._rec()
        fast = self._seg(rec, 0, content_seconds=8, wall_seconds=1)

        self._measure(rec, [fast], threshold=0)

        self.assertEqual(self._events(rec.id), [])
        self.assertEqual(db.session.get(Recording, rec.id).fast_delivery_segment_count, 0)
        self.assertIsNotNone(
            db.session.get(RecordingSegment, fast.id).content_duration_seconds)

    def test_an_unprobeable_segment_leaves_the_flag_alone_and_never_raises(self):
        """A diagnostic must never harm the capture it is diagnosing - this runs between
        the capture and the file the user is waiting for."""
        rec = self._rec()
        gone = RecordingSegment(
            recording_id=rec.id, segment_number=0,
            file_path=os.path.join(self.t._tmpdir, 'absent.ts'),
            started_at=self.now - timedelta(seconds=1), ended_at=self.now,
            bytes_recorded=4096)
        db.session.add(gone)
        db.session.commit()

        self._measure(rec, [gone])  # must not raise

        self.assertEqual(self._events(rec.id), [])

    def test_the_wall_clock_helper_reports_nothing_for_a_segment_still_open(self):
        """A segment with no ended_at is still capturing, so it has no wall span to judge -
        and judging one against `now` would flag a long-running segment the moment its
        buffer arrived."""
        rec = self._rec()
        open_seg = RecordingSegment(recording_id=rec.id, segment_number=0,
                                    file_path='/nonexistent.ts',
                                    started_at=self.now, ended_at=None)
        db.session.add(open_seg)
        db.session.commit()
        self.assertIsNone(segment_wall_seconds(open_seg))


class ListPagePillTests(unittest.TestCase):
    """The recordings list health pill - the surface that called the worst recording in the
    set a clean capture."""

    def setUp(self):
        self.t = make_test_app()
        self.now = datetime.utcnow()

    def tearDown(self):
        self.t.cleanup()

    def _row(self, rec_id):
        from app.routes.recordings import _index_row
        from app.tz_utils import get_display_tz
        db.session.expire_all()
        return _index_row(db.session.get(Recording, rec_id), datetime.utcnow(),
                          get_display_tz(), set())

    def _rec_with_one_clean_segment(self, **rec_kw):
        rec = seed.make_recording(start_time=self.now - timedelta(seconds=3600),
                                  stop_time=self.now, **rec_kw)
        db.session.add(RecordingSegment(
            recording_id=rec.id, segment_number=0, file_path='/nonexistent.ts',
            started_at=self.now - timedelta(seconds=3600), ended_at=self.now,
            bytes_recorded=4096, stall_count=0))
        db.session.commit()
        return rec

    def test_a_fast_segment_stops_the_row_reading_as_a_clean_capture(self):
        """A frozen feed never stalls, so every other signal this pill reads stays clean.
        Without this branch the row renders a green tick on a recording the app itself has
        flagged, which is worse than saying nothing."""
        rec = self._rec_with_one_clean_segment(
            fast_delivery_segment_count=1, fast_delivery_seconds=31122.0)

        health = self._row(rec.id)['health']

        self.assertEqual(health['cls'], 'warn')
        self.assertNotIn('✓', health['label'])
        self.assertIn('faster than real time', health['tip'])
        self.assertIn('worth checking', health['tip'])

    def test_a_recording_with_nothing_flagged_still_reads_clean(self):
        """The flag has to stay rare enough to mean something."""
        rec = self._rec_with_one_clean_segment(
            fast_delivery_segment_count=0, fast_delivery_seconds=0)

        health = self._row(rec.id)['health']

        self.assertEqual(health['cls'], 'ok')
        self.assertIn('✓', health['label'])
        self.assertNotIn('faster than real time', health['tip'])

    def test_a_stalling_recording_reports_both_its_stalls_and_the_flag(self):
        """The stall branch owns the pill when both are true - the count is the more
        actionable number - but the tooltip must not drop the flag on the floor."""
        rec = self._rec_with_one_clean_segment(
            fast_delivery_segment_count=2, fast_delivery_seconds=600.0)
        seg = RecordingSegment.query.filter_by(recording_id=rec.id).first()
        seg.stall_count = 3
        db.session.commit()

        health = self._row(rec.id)['health']

        self.assertEqual(health['cls'], 'warn')
        self.assertIn('stalls', health['tip'])
        self.assertIn('faster than real time', health['tip'])


class DetailPageTests(unittest.TestCase):
    """The detail page's two surfaces: the recording-level stat and the per-row badge."""

    def setUp(self):
        self.t = make_test_app()
        self.client = self.t.app.test_client()
        self.now = datetime.utcnow()

    def tearDown(self):
        self.t.cleanup()

    def _page(self, rec_id):
        resp = self.client.get(f'/recordings/{rec_id}')
        self.assertEqual(resp.status_code, 200)
        return resp.get_data(as_text=True)

    def _rec(self, *, content, wall, **rec_kw):
        # COMPLETED because the stats block is a finished recording's surface - a SCHEDULED
        # row renders the upcoming-airing panel instead and has nothing to report yet.
        rec = seed.make_recording(start_time=self.now - timedelta(seconds=3600),
                                  stop_time=self.now, status=REC_STATUS_COMPLETED, **rec_kw)
        db.session.add(RecordingSegment(
            recording_id=rec.id, segment_number=0, file_path='/nonexistent.ts',
            started_at=self.now - timedelta(seconds=wall), ended_at=self.now,
            bytes_recorded=4096, content_duration_seconds=content))
        db.session.commit()
        return rec

    def test_the_stat_row_names_how_much_of_the_file_is_suspect(self):
        rec = self._rec(content=31122.0, wall=8275.0,
                        fast_delivery_segment_count=1, fast_delivery_seconds=31122.0)

        html = self._page(rec.id)

        self.assertIn('Arrived faster than real time', html)
        self.assertIn('1 segment', html)

    def test_a_flagged_segment_carries_a_badge_on_its_own_row(self):
        """Labelled in the row rather than only summarized, the same way a discarded
        segment is - a reader scanning the segment list has to be able to see which one."""
        rec = self._rec(content=31122.0, wall=8275.0,
                        fast_delivery_segment_count=1, fast_delivery_seconds=31122.0)

        html = self._page(rec.id)

        self.assertIn('>fast</span>', html)

    def test_an_ordinary_segment_gets_no_badge_and_no_stat(self):
        rec = self._rec(content=3600.0, wall=3600.0,
                        fast_delivery_segment_count=0, fast_delivery_seconds=0)

        html = self._page(rec.id)

        self.assertNotIn('>fast</span>', html)
        self.assertNotIn('Arrived faster than real time', html)

    def test_the_content_tooltip_quotes_the_ratio_when_it_is_not_about_one(self):
        rec = self._rec(content=31122.0, wall=8275.0)

        html = self._page(rec.id)

        self.assertIn('3.76x real time', html)

    def test_the_content_tooltip_leaves_the_ratio_off_an_ordinary_segment(self):
        """A '1.00x real time' on every clean row is noise that buries the one row that
        matters."""
        rec = self._rec(content=3601.0, wall=3600.0)

        html = self._page(rec.id)

        self.assertNotIn('1.00x real time', html)

    def test_the_page_no_longer_blames_a_buffer_for_every_surplus(self):
        """The 'Content vs capture time' tooltip named replayed buffer as the only cause,
        which reads as an explanation of a frozen feed's surplus and is the wrong one."""
        rec = self._rec(content=31122.0, wall=8275.0)

        html = self._page(rec.id)

        self.assertNotIn('it is very often duplicated video', html)


if __name__ == '__main__':
    unittest.main()
