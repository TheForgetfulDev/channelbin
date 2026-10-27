"""Tier 2 - where a recording's format changes, and for how long, is measured over the whole
file rather than read off each segment's opening (dev/docs/BUGS.md 2026-10-15 @ 06:12:01 PM).

Recording 16's two restarts each opened on a minute or two of 720p29.97 inside four hours of
1080p59.94. The segment probes read the first 2 MB, so both segments were recorded as 720p30:
the recording was told its file changed format for good, the detail page's Video row showed
720p30, and the frame-deficit check divided by a rate blended from those probes. Recording 17's
feed dropped to the same lower format 25s into a segment whose probe had read 1080p59.94, and
nothing was logged at all (dev/changelog/1201).

The fixtures are synthesized locally with ffmpeg; no network, no /dvr.
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import db  # noqa: E402
from app.channel_groups import file_format_stretches  # noqa: E402
from app.database import (  # noqa: E402
    DIAGNOSTICS, RECORDING_FORMAT_CHANGED, REC_STATUS_COMPLETED, Recording, RecordingEvent,
    RecordingSegment, add_recording_event,
)
from app.postprocessor import describe_format_stretches  # noqa: E402
from app.probe import FORMAT_RUNS_LISTED, _FormatRuns, survey_formats  # noqa: E402
from tests.support import seed  # noqa: E402
from tests.support.app import make_test_app  # noqa: E402

_HAVE_FFMPEG = bool(shutil.which('ffmpeg') and shutil.which('ffprobe'))

HD, SD = ('1920x1080', 60), ('1280x720', 30)
# What the encoded fixtures use: the same two rates at sizes small enough to encode quickly.
BIG, SMALL = ('384x216', 60), ('256x144', 30)


def _ffmpeg(*args):
    subprocess.run(['ffmpeg', '-v', 'error', '-y', *args], check=True, timeout=120)


def _clip(path, size, rate, seconds, gop):
    _ffmpeg('-f', 'lavfi', '-i', f'testsrc=size={size}:rate={rate}', '-t', str(seconds),
            '-c:v', 'libx264', '-preset', 'ultrafast', '-g', str(gop), '-pix_fmt', 'yuv420p',
            path)


def _join(out, parts, workdir):
    """The concat concatenator.py runs, so the timestamps are what the survey really reads."""
    listfile = os.path.join(workdir, os.path.basename(out) + '.txt')
    with open(listfile, 'w') as fh:
        for part in parts:
            fh.write(f"file '{part}'\n")
    _ffmpeg('-err_detect', 'ignore_err', '-fflags', '+genpts+discardcorrupt', '-f', 'concat',
            '-safe', '0', '-i', listfile, '-c', 'copy', out)


def _survey(runs, totals=None):
    """A survey dict carrying only what file_format_stretches reads."""
    if totals is None:
        acc = {}
        for start, end, w, h, rate in runs:
            acc[(w, h, rate)] = acc.get((w, h, rate), 0.0) + (end - start)
        totals = [[w, h, rate, secs] for (w, h, rate), secs in acc.items()]
    return {'format_runs': runs, 'format_seconds': totals}


@unittest.skipUnless(_HAVE_FFMPEG, 'needs ffmpeg and ffprobe on PATH')
class SurveyStretchTests(unittest.TestCase):
    """20s at 59.94, a 10s stretch of a smaller picture at 29.97, then 30s of the first
    format again - a restart that landed in the provider's lower feed, in miniature.
    Keyframes every 1s."""

    @classmethod
    def setUpClass(cls):
        cls._dir = tempfile.mkdtemp(prefix='stretches-')
        d = cls._dir
        _clip(os.path.join(d, 'a.ts'), BIG[0], '60000/1001', 20, 60)
        _clip(os.path.join(d, 'b.ts'), SMALL[0], '30000/1001', 10, 30)
        _clip(os.path.join(d, 'c.ts'), BIG[0], '60000/1001', 30, 60)
        cls.joined = os.path.join(d, 'joined.ts')
        _join(cls.joined, [os.path.join(d, n) for n in ('a.ts', 'b.ts', 'c.ts')], d)
        cls.survey = survey_formats(cls.joined)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls._dir, ignore_errors=True)

    def test_each_stretch_is_placed_to_within_a_keyframe_interval(self):
        runs = self.survey['format_runs']
        self.assertEqual([r[2:4] for r in runs], [[384, 216], [256, 144], [384, 216]])
        self.assertAlmostEqual(runs[1][0], 20.0, delta=1.1)
        self.assertAlmostEqual(runs[1][1], 30.0, delta=1.1)
        self.assertAlmostEqual(runs[2][1], 60.0, delta=1.1)
        self.assertEqual(self.survey['format_run_count'], 3)

    def test_the_totals_say_the_lower_format_is_the_short_one(self):
        totals = {(w, h): secs for w, h, _rate, secs in self.survey['format_seconds']}
        self.assertAlmostEqual(totals[(256, 144)], 10.0, delta=1.1)
        self.assertAlmostEqual(totals[(384, 216)], 50.0, delta=1.1)

    def test_the_stretches_name_the_main_format_and_where_the_other_one_was(self):
        got = file_format_stretches(self.survey)
        self.assertEqual(got['main'], BIG)
        self.assertEqual(got['first'], BIG)
        [(key, seconds, spans)] = got['others']
        self.assertEqual(key, SMALL)
        self.assertAlmostEqual(seconds, 10.0, delta=1.1)
        self.assertEqual(len(spans), 1)

    def test_a_file_that_opens_on_the_short_stretch_is_still_mostly_the_long_one(self):
        """The recording-16 shape from the probe's side: what the file OPENS at is what
        its header and a 2 MB probe describe, and here that is the minority format."""
        joined = os.path.join(self._dir, 'opens_low.ts')
        _join(joined, [os.path.join(self._dir, n) for n in ('b.ts', 'c.ts')], self._dir)
        got = file_format_stretches(survey_formats(joined))
        self.assertEqual(got['first'], SMALL)
        self.assertEqual(got['main'], BIG)


class FormatRunsTests(unittest.TestCase):

    def test_a_keyframe_with_no_rate_is_not_a_new_stretch(self):
        runs = _FormatRuns()
        runs.add(0.0, 1920, 1080, 59.94)
        runs.add(1.0, 1920, 1080, None)
        runs.add(2.0, 1920, 1080, 59.94)
        runs.finish()
        self.assertEqual(runs.count, 1)
        self.assertEqual(runs.listed, [[0.0, 3.0, 1920, 1080, 59.94]])

    def test_a_flapping_feed_is_listed_up_to_the_cap_and_totalled_in_full(self):
        runs = _FormatRuns()
        n = FORMAT_RUNS_LISTED + 10
        for i in range(n):
            runs.add(float(i), 1920 if i % 2 == 0 else 1280, 1080 if i % 2 == 0 else 720,
                     60.0 if i % 2 == 0 else 30.0)
        runs.finish()
        self.assertEqual(runs.count, n)
        self.assertEqual(len(runs.listed), FORMAT_RUNS_LISTED)
        self.assertAlmostEqual(sum(t[3] for t in runs.totals()), float(n), places=3)


class FileFormatStretchesTests(unittest.TestCase):

    def test_nothing_recorded_is_none_not_one_format(self):
        """A survey that failed or predates the stretches must not read as uniform."""
        self.assertIsNone(file_format_stretches(None))
        self.assertIsNone(file_format_stretches({}))
        self.assertIsNone(file_format_stretches({'video_sizes': [[1920, 1080, 1.0]]}))

    def test_one_format_throughout_has_no_others(self):
        got = file_format_stretches(_survey([[0.0, 100.0, 1920, 1080, 59.94]]))
        self.assertEqual(got['others'], [])
        self.assertEqual(got['main'], HD)

    def test_59_94_and_60_are_one_format_and_their_stretches_merge(self):
        got = file_format_stretches(_survey([[0.0, 50.0, 1920, 1080, 59.94],
                                             [50.0, 100.0, 1920, 1080, 60.0]]))
        self.assertEqual(got['others'], [])

    def test_every_stretch_of_a_minority_format_is_kept_in_order(self):
        got = file_format_stretches(_survey([
            [0.0, 339.0, 1920, 1080, 59.94], [339.0, 441.0, 1280, 720, 29.97],
            [441.0, 12842.0, 1920, 1080, 59.94], [12842.0, 12910.0, 1280, 720, 29.97],
            [12910.0, 15748.0, 1920, 1080, 59.94]]))
        self.assertEqual(got['main'], HD)
        [(key, seconds, spans)] = got['others']
        self.assertEqual(key, SD)
        self.assertAlmostEqual(seconds, 170.0)
        self.assertEqual(spans, [(339.0, 441.0), (12842.0, 12910.0)])

    def test_a_format_first_seen_past_the_listed_stretches_is_still_reported(self):
        survey = _survey([[0.0, 100.0, 1920, 1080, 59.94]],
                         totals=[[1920, 1080, 59.94, 100.0], [1280, 720, 29.97, 5.0]])
        got = file_format_stretches(survey)
        self.assertEqual([(k, s) for k, s, _spans in got['others']], [(SD, 5.0)])

    def test_the_sentence_names_the_main_format_and_each_stretch(self):
        got = file_format_stretches(_survey([
            [0.0, 339.0, 1920, 1080, 59.94], [339.0, 441.0, 1280, 720, 29.97],
            [441.0, 1000.0, 1920, 1080, 59.94]]))
        text = describe_format_stretches(got)
        self.assertTrue(text.startswith('mostly 1920x1080 @ 60'), text)
        self.assertIn('1280x720 @ 30 for 1m 42s in all', text)
        self.assertIn('5m 39s to 7m 21s', text)


class _Seeded(unittest.TestCase):
    def setUp(self):
        self.t = make_test_app()
        self.now = datetime.utcnow()

    def tearDown(self):
        self.t.cleanup()

    def _rec(self, probes):
        rec = seed.make_recording(start_time=self.now - timedelta(seconds=3600),
                                  stop_time=self.now, status=REC_STATUS_COMPLETED)
        for n, (res, fps) in enumerate(probes, start=1):
            db.session.add(RecordingSegment(
                recording_id=rec.id, segment_number=n, file_path=f'/nonexistent-{n}.ts',
                started_at=self.now - timedelta(seconds=3600 - n * 600),
                ended_at=self.now - timedelta(seconds=3000 - n * 600),
                bytes_recorded=4096, stall_count=0, probe_resolution=res, probe_fps=fps))
        db.session.commit()
        return rec

    def _changed(self, rec_id):
        return RecordingEvent.query.filter_by(recording_id=rec_id,
                                              event_type=RECORDING_FORMAT_CHANGED).all()


@unittest.skipUnless(_HAVE_FFMPEG, 'needs ffmpeg and ffprobe on PATH')
class SurveyEventTests(_Seeded):
    """The survey writes the authoritative RECORDING_FORMAT_CHANGED, in its own commit."""

    @classmethod
    def setUpClass(cls):
        cls._dir = tempfile.mkdtemp(prefix='stretch-ev-')
        d = cls._dir
        _clip(os.path.join(d, 'a.ts'), BIG[0], '60000/1001', 6, 60)
        _clip(os.path.join(d, 'b.ts'), SMALL[0], '30000/1001', 3, 30)
        cls.mixed = os.path.join(d, 'mixed.ts')
        _join(cls.mixed, [os.path.join(d, 'a.ts'), os.path.join(d, 'b.ts'),
                          os.path.join(d, 'a.ts')], d)
        cls.steady = os.path.join(d, 'a.ts')

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls._dir, ignore_errors=True)

    def test_a_format_change_inside_the_file_is_disclosed_with_where_and_how_long(self):
        """Every segment probed the first format - the change is inside a segment, which
        the watchdog's live check cannot see (recording 17's segment 2)."""
        from app.postprocessor import _survey_recording
        rec = self._rec([(BIG[0], 59.94)])
        _survey_recording(rec.id, self.mixed)
        db.session.expire_all()
        [ev] = self._changed(rec.id)
        self.assertEqual(json.loads(ev.extra_data)['basis'], 'format_survey')
        self.assertIn('mostly 384x216 @ 60', ev.detail)
        self.assertIn('256x144 @ 30 for', ev.detail)
        self.assertIn('header describes only 384x216 @ 60', ev.detail)

    def test_the_verdict_is_written_to_the_recording_in_the_same_commit(self):
        """The Recordings list reads these three columns, not the survey event
        (dev/changelog/1307): the format the file holds longest, the one its header
        describes, and the seconds in any other format."""
        from app.postprocessor import _survey_recording
        rec = self._rec([(BIG[0], 59.94)])
        _survey_recording(rec.id, self.mixed)
        db.session.expire_all()
        rec = db.session.get(Recording, rec.id)
        self.assertEqual(rec.format_survey_main, '384x216 @ 60')
        self.assertEqual(rec.format_survey_first, '384x216 @ 60')
        self.assertAlmostEqual(rec.format_survey_other_seconds, 3.0, delta=1.5)

    def test_one_format_throughout_writes_no_change(self):
        from app.postprocessor import _survey_recording
        rec = self._rec([(BIG[0], 59.94)])
        _survey_recording(rec.id, self.steady)
        db.session.expire_all()
        self.assertEqual(self._changed(rec.id), [])
        rec = db.session.get(Recording, rec.id)
        self.assertEqual(rec.format_survey_main, '384x216 @ 60')
        self.assertEqual(rec.format_survey_other_seconds, 0.0,
                         'surveyed and one format throughout is a measured 0, not NULL')
        survey = [e for e in RecordingEvent.query.filter_by(
            recording_id=rec.id, event_type=DIAGNOSTICS).all()
            if json.loads(e.extra_data or '{}').get('kind') == 'format_survey']
        self.assertEqual(len(survey), 1)
        self.assertEqual(json.loads(survey[0].extra_data)['format_run_count'], 1)


class DetailPageTests(_Seeded):
    """The detail page reads the survey back; the list row keeps the segment probes."""

    RUNS = [[0.0, 339.0, 1920, 1080, 59.94], [339.0, 441.0, 1280, 720, 29.97],
            [441.0, 3000.0, 1920, 1080, 59.94]]

    def _record_survey(self, rec_id, runs):
        add_recording_event(rec_id, DIAGNOSTICS, detail='Format survey: test',
                            extra=dict(_survey(runs), kind='format_survey'))
        db.session.commit()

    def _video_row(self, rec_id):
        resp = self.t.app.test_client().get(f'/recordings/{rec_id}')
        self.assertEqual(resp.status_code, 200)
        html = resp.get_data(as_text=True)
        return html, html[html.index('<span class="sk">Video</span>'):][:900]

    def test_the_video_row_shows_the_format_the_file_holds_longest(self):
        """Recording 16: the latest segment opened at 720p30, the file is 1080p60."""
        rec = self._rec([('1920x1080', 59.94), ('1280x720', 29.97)])
        self._record_survey(rec.id, self.RUNS)
        html, row = self._video_row(rec.id)
        self.assertIn('1080p60 · format changed', row)
        self.assertIn('the format the file holds longest', row)
        self.assertIn('mostly 1920x1080 @ 60', html)
        self.assertNotIn('segments opened at', html)

    def test_a_survey_of_one_format_overrules_the_segment_openings(self):
        rec = self._rec([('1920x1080', 59.94), ('1280x720', 29.97)])
        self._record_survey(rec.id, [[0.0, 3000.0, 1920, 1080, 59.94]])
        html, row = self._video_row(rec.id)
        self.assertIn('1080p60', row)
        self.assertNotIn('format changed', html)

    def test_without_a_survey_the_page_says_what_the_segments_opened_at(self):
        rec = self._rec([('1920x1080', 59.94), ('1280x720', 29.97)])
        html, row = self._video_row(rec.id)
        self.assertIn('720p30 · format changed', row)
        self.assertIn('segments opened at 1920x1080 @ 60, then 1280x720 @ 30', html)
        self.assertIn('latest segment', row)


if __name__ == '__main__':
    unittest.main()
