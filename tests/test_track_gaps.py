"""Tier 2 - the post-capture analysis says where a recording's picture is missing, where its
sound is, and where both are (dev/changelog/1649).

The damage scan reads the video alone, so it cannot tell a hole with the program's sound
playing through it from a stretch with nothing on either track. The converter holds the last
picture across both, so the second plays as a still picture over silence - recording 32's
6:03 and 7:09 stretches (dev/changelog/1422). This is measurement only, so that kind can be
counted on recordings made after the capture fixes; nothing acts on it.

Covers, in order:
  - SplitTrackGapsTests: the span arithmetic, pure.
  - TrackGapsProbeTests: probe.track_gaps() on real synthesized files - a picture hole and a
    sound hole that partly overlap, the two lined up, a picture hole only, a picture that
    stops before the sound, no sound track, no hole - and a truncated jump list.
  - TrackGapsSummaryTests: the event's sentence.
  - TrackGapsEventTests: the analysis phase records the event, on a real file through
    do_postprocess, and records a failure as a failure.

Real ffmpeg on small local files; no network. Run standalone:
  python3 -m unittest tests.test_track_gaps
"""
import json
import os
import shutil
import subprocess
import sys
import unittest
from datetime import datetime, timedelta
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import app.postprocessor as ppmod  # noqa: E402
from app import db, toolchain  # noqa: E402
from app.database import RecordingEvent, RecordingSegment, DIAGNOSTICS  # noqa: E402
from app.postprocessor import track_gaps_summary  # noqa: E402
from app.probe import split_track_gaps, track_gaps  # noqa: E402
from tests.support import clips, seed  # noqa: E402
from tests.support.app import make_test_app  # noqa: E402
from tests.support.conversion import postprocess_config, stubbed_postprocess  # noqa: E402

_HAVE_FFMPEG = bool(shutil.which('ffmpeg') and shutil.which('ffprobe'))

# Where each fixture's tracks drop out, in seconds of the source. The file's own timeline
# starts a little after zero (the mpegts muxer's offset, plus the B-frame delay on the
# video's decode timestamps), so places are asserted to within PLACE_TOLERANCE.
VIDEO_HOLE = (4.0, 7.0)
AUDIO_HOLE = (5.0, 9.0)
PLACE_TOLERANCE = 0.15
LENGTH_TOLERANCE = 0.1


def _ff(*args):
    subprocess.run(['ffmpeg', '-hide_banner', '-loglevel', 'error', '-y', *args],
                   check=True, timeout=120)


def _clip(path, video_hole=None, audio_hole=None, audio=True, seconds=14, video_seconds=None):
    """testsrc2 and noise, with the frames inside each hole dropped and every timestamp
    after it kept where it was - the shape a feed that went quiet leaves in a capture.
    video_seconds shorter than seconds stops the picture early with the sound running on."""
    args = ['-f', 'lavfi', '-i',
            f'testsrc2=size=192x108:rate=25:duration={video_seconds or seconds}']
    if audio:
        args += ['-f', 'lavfi', '-i', f'anoisesrc=amplitude=0.5:duration={seconds}:seed=7']
    if video_hole:
        args += ['-vf', f'select=not(between(t\\,{video_hole[0]}\\,{video_hole[1]}))']
    if audio and audio_hole:
        args += ['-af', f'aselect=not(between(t\\,{audio_hole[0]}\\,{audio_hole[1]}))']
    args += ['-fps_mode', 'passthrough', '-c:v', 'libx264', '-preset', 'veryfast',
             '-g', '25', '-bf', '2']
    args += ['-c:a', 'aac'] if audio else ['-an']
    _ff(*args, '-f', 'mpegts', path)


def _build_sources(d):
    _clip(os.path.join(d, 'overlap.ts'), VIDEO_HOLE, AUDIO_HOLE)
    _clip(os.path.join(d, 'same.ts'), VIDEO_HOLE, VIDEO_HOLE)
    _clip(os.path.join(d, 'video_ends_early.ts'), video_seconds=8)
    _clip(os.path.join(d, 'video_only.ts'), VIDEO_HOLE)
    _clip(os.path.join(d, 'no_sound.ts'), VIDEO_HOLE, audio=False)
    _clip(os.path.join(d, 'clean.ts'))


def _length(spans):
    return sum(end - start for start, end in spans)


class SplitTrackGapsTests(unittest.TestCase):
    def test_only_the_overlap_is_both(self):
        both, video_only, audio_only = split_track_gaps([(4.0, 7.0)], [(5.0, 9.0)])
        self.assertEqual(both, [(5.0, 7.0)])
        self.assertEqual(video_only, [(4.0, 5.0)])
        self.assertEqual(audio_only, [(7.0, 9.0)])

    def test_a_picture_hole_with_sound_through_it_is_never_both(self):
        """The video-only kind must stay held: closing it is the out-of-sync defect
        dev/changelog/1199 fixed."""
        both, video_only, audio_only = split_track_gaps([(4.0, 7.0)], [])
        self.assertEqual(both, [])
        self.assertEqual(video_only, [(4.0, 7.0)])
        self.assertEqual(audio_only, [])

    def test_a_sound_hole_inside_a_picture_hole_splits_it(self):
        both, video_only, audio_only = split_track_gaps([(1.0, 10.0)], [(3.0, 4.0)])
        self.assertEqual(both, [(3.0, 4.0)])
        self.assertEqual(video_only, [(1.0, 3.0), (4.0, 10.0)])
        self.assertEqual(audio_only, [])

    def test_holes_that_never_meet_stay_apart(self):
        both, video_only, audio_only = split_track_gaps([(1.0, 2.0)], [(3.0, 4.0)])
        self.assertEqual(both, [])
        self.assertEqual(video_only, [(1.0, 2.0)])
        self.assertEqual(audio_only, [(3.0, 4.0)])

    def test_out_of_order_and_overlapping_holes_are_merged_first(self):
        """A joined file's decode timeline can step back at a join, so a stream's holes may
        arrive out of order."""
        both, video_only, _audio_only = split_track_gaps(
            [(8.0, 9.0), (1.0, 3.0), (2.0, 4.0)], [(0.0, 20.0)])
        self.assertEqual(both, [(1.0, 4.0), (8.0, 9.0)])
        self.assertEqual(video_only, [])

    def test_slivers_shorter_than_the_minimum_are_dropped(self):
        """Two tracks that drop out together never stop and resume on the same timestamp, so
        a clean both-track hole leaves a few hundredths of one track's hole at each end -
        counted, those read as one-track gaps of 0.0 s."""
        both, video_only, audio_only = split_track_gaps(
            [(4.08, 7.12)], [(4.09, 7.10)], min_length=0.25)
        self.assertEqual(both, [(4.09, 7.10)])
        self.assertEqual((video_only, audio_only), ([], []))

    def test_several_holes_on_each_track(self):
        both, video_only, audio_only = split_track_gaps(
            [(1.0, 3.0), (5.0, 6.0), (10.0, 12.0)], [(2.0, 5.5), (11.0, 13.0)])
        self.assertEqual(both, [(2.0, 3.0), (5.0, 5.5), (11.0, 12.0)])
        self.assertEqual(video_only, [(1.0, 2.0), (5.5, 6.0), (10.0, 11.0)])
        self.assertEqual(audio_only, [(3.0, 5.0), (12.0, 13.0)])


@unittest.skipUnless(_HAVE_FFMPEG, 'needs ffmpeg and ffprobe on PATH')
class TrackGapsProbeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.src = clips.shared(__name__, _build_sources, prefix='trackgaps-src-')

    def _gaps(self, name):
        gaps = track_gaps(os.path.join(self.src, name))
        self.assertTrue(gaps, f'{name} could not be read')
        return gaps

    def _assert_span(self, span, start, end):
        self.assertAlmostEqual(span[0], start, delta=PLACE_TOLERANCE)
        self.assertAlmostEqual(span[1], end, delta=PLACE_TOLERANCE)

    def test_partly_overlapping_holes_are_split_into_three(self):
        gaps = self._gaps('overlap.ts')
        self.assertEqual(len(gaps['both']), 1, gaps)
        self._assert_span(gaps['both'][0], AUDIO_HOLE[0], VIDEO_HOLE[1])
        self.assertAlmostEqual(_length(gaps['both']), VIDEO_HOLE[1] - AUDIO_HOLE[0],
                               delta=LENGTH_TOLERANCE)
        self.assertEqual(len(gaps['video_only']), 1, gaps)
        self._assert_span(gaps['video_only'][0], VIDEO_HOLE[0], AUDIO_HOLE[0])
        self.assertEqual(len(gaps['audio_only']), 1, gaps)
        self._assert_span(gaps['audio_only'][0], VIDEO_HOLE[1], AUDIO_HOLE[1])

    def test_holes_that_line_up_are_both_and_nothing_else(self):
        """The shape that matters most - recording 32's 6:03 and 7:09 - must not also
        count as one-track gaps."""
        gaps = self._gaps('same.ts')
        self.assertEqual(len(gaps['both']), 1, gaps)
        self.assertAlmostEqual(_length(gaps['both']), VIDEO_HOLE[1] - VIDEO_HOLE[0],
                               delta=LENGTH_TOLERANCE)
        self.assertEqual((gaps['video_only'], gaps['audio_only']), ([], []))

    def test_a_picture_that_stops_before_the_sound_is_an_edge(self):
        """Not a hole between two of the video's packets, so no jump shows it; the end of
        each track is compared on its own."""
        gaps = self._gaps('video_ends_early.ts')
        self.assertAlmostEqual(gaps['edges']['video']['early'], 6.0, delta=PLACE_TOLERANCE)
        self.assertEqual(gaps['edges']['audio']['early'], 0.0)
        self.assertEqual((gaps['both'], gaps['video_only'], gaps['audio_only']), ([], [], []))

    def test_a_picture_hole_with_sound_playing_is_video_only(self):
        gaps = self._gaps('video_only.ts')
        self.assertEqual(gaps['both'], [])
        self.assertEqual(gaps['audio_only'], [])
        self.assertEqual(len(gaps['video_only']), 1, gaps)
        self.assertAlmostEqual(_length(gaps['video_only']), VIDEO_HOLE[1] - VIDEO_HOLE[0],
                               delta=LENGTH_TOLERANCE)

    def test_a_file_with_no_sound_track_says_so(self):
        gaps = self._gaps('no_sound.ts')
        self.assertIsNone(gaps['audio_stream'])
        self.assertEqual(gaps['audio_streams'], 0)
        self.assertEqual(gaps['both'], [])
        self.assertEqual(len(gaps['video_only']), 1, gaps)

    def test_a_continuous_file_has_no_gaps(self):
        gaps = self._gaps('clean.ts')
        self.assertEqual((gaps['both'], gaps['video_only'], gaps['audio_only']), ([], [], []))
        self.assertEqual((gaps['video_stream'], gaps['audio_stream']), (0, 1))
        for kind in ('video', 'audio'):
            self.assertEqual(gaps['edges'][kind], {'empty': False, 'late': 0.0, 'early': 0.0})

    def test_past_a_truncated_list_nothing_is_compared(self):
        """A track whose jumps ran past the listing cap has unlisted holes after its last
        listed one, so the other track's holes there cannot be called one-track."""
        scan = {0: {'packets': 900, 'first_dts': 0.0, 'last_dts': 100.0, 'truncated': True,
                    'jumps': [(9.96, 12.0, 0.04)]},
                1: {'packets': 900, 'first_dts': 0.0, 'last_dts': 100.0, 'truncated': False,
                    'jumps': [(10.0, 11.0, 0.02), (50.0, 60.0, 0.02)]}}
        with mock.patch('app.probe.stream_layout', return_value=[('video', 256), ('audio', 257)]), \
             mock.patch('app.probe.scan_timeline_jumps', return_value=scan):
            gaps = track_gaps(os.path.join(self.src, 'clean.ts'))
        self.assertTrue(gaps['truncated'])
        self.assertAlmostEqual(gaps['compared_until'], 12.0)
        self.assertEqual(gaps['audio_only'], [])
        self.assertEqual(len(gaps['both']), 1)

    def test_an_unreadable_file_is_an_empty_answer(self):
        path = os.path.join(self.src, 'clean.ts') + '.missing'
        self.assertEqual(track_gaps(path), {})


class TrackGapsSummaryTests(unittest.TestCase):
    @staticmethod
    def _gaps(**kw):
        clean = {'empty': False, 'late': 0.0, 'early': 0.0}
        base = {'video_stream': 0, 'audio_stream': 1, 'audio_streams': 1, 'truncated': False,
                'compared_until': None, 'gap_threshold': 0.25, 'both': [], 'video_only': [],
                'audio_only': [], 'edges': {'video': dict(clean), 'audio': dict(clean)}}
        base.update(kw)
        return base

    def test_a_stretch_with_nothing_on_either_track_is_named_with_its_place(self):
        text = track_gaps_summary(self._gaps(both=[(363.0, 367.67)], video_only=[(500.0, 501.0)]))
        self.assertIn('Nothing on either track for 4.7 s in 1 stretch (at 6m 3s for 4.7 s)', text)
        self.assertIn('The picture stops with the sound playing on for 1.0 s', text)

    def test_a_clean_file_says_it_was_checked(self):
        text = track_gaps_summary(self._gaps())
        self.assertIn('none - the picture and sound run from start to end without a gap', text)

    def test_a_picture_that_ends_early_is_not_called_clean(self):
        gaps = self._gaps()
        gaps['edges']['video']['early'] = 6.0
        gaps['edges']['video']['late'] = 0.8
        text = track_gaps_summary(gaps)
        self.assertIn('The picture ends 6.0 s before the sound', text)
        self.assertIn('The picture starts 0.8 s after the sound', text)
        self.assertNotIn('none', text)

    def test_an_empty_track_is_named(self):
        gaps = self._gaps()
        gaps['edges']['audio']['empty'] = True
        self.assertIn('The sound track holds no packets at all', track_gaps_summary(gaps))

    def test_a_truncated_comparison_says_where_it_stops(self):
        text = track_gaps_summary(self._gaps(truncated=True, compared_until=3725.0))
        self.assertIn('compared only up to 1h 2m 5s', text)

    def test_a_file_with_no_sound_track_does_not_claim_sound_played(self):
        gaps = self._gaps(audio_stream=None, audio_streams=0, video_only=[(10.0, 12.0)])
        del gaps['edges']['audio']
        text = track_gaps_summary(gaps)
        self.assertIn('the file has no sound track', text)
        self.assertNotIn('sound playing on', text)

    def test_past_the_named_limit_the_rest_is_a_count(self):
        spans = [(float(i * 10), float(i * 10 + 1)) for i in range(ppmod.TRACK_GAPS_NAMED + 3)]
        text = track_gaps_summary(self._gaps(both=spans))
        self.assertIn(f'in {len(spans)} stretches', text)
        self.assertIn('and 3 more', text)

    def test_a_joined_recording_says_what_the_read_cannot_see(self):
        text = track_gaps_summary(self._gaps(), joined_segments=3)
        self.assertIn('between the 3 joined segments', text)
        self.assertIn('Capture gaps', text)


@unittest.skipUnless(_HAVE_FFMPEG, 'needs ffmpeg and ffprobe on PATH')
class TrackGapsEventTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.src = clips.shared(__name__, _build_sources, prefix='trackgaps-src-')

    def setUp(self):
        toolchain.reset_cache()
        self.t = make_test_app()
        now = datetime.utcnow()
        self.rec = seed.make_recording(status='CONCATENATING', name='trackgaps',
                                       start_time=now - timedelta(seconds=14), stop_time=now,
                                       recorded_duration_seconds=14.0)
        self.ts = os.path.join(self.t._tmpdir, f'rec_{self.rec.id}.ts')
        self.rec.output_path = self.ts
        db.session.commit()
        self.rid = self.rec.id

    def tearDown(self):
        with ppmod._active_lock:
            ppmod._active_conversions.clear()
            ppmod._cancel_requested.clear()
        toolchain.reset_cache()
        self.t.cleanup()

    def _use(self, name):
        shutil.copyfile(os.path.join(self.src, name), self.ts)
        start = datetime.utcnow()
        db.session.add(RecordingSegment(
            recording_id=self.rid, segment_number=0,
            file_path=os.path.join(self.t._tmpdir, 'seg_0.ts'),
            started_at=start, ended_at=start + timedelta(seconds=14),
            exit_reason='COMPLETED', bytes_recorded=4096, probe_resolution='192x108',
            probe_fps=25.0, probe_audio_channels=1, probe_interlaced=False,
            content_duration_seconds=14.0))
        db.session.commit()

    def _postprocess(self):
        stubbed_postprocess(self.t.app, self.rid, self.ts,
                            postprocess_config(gather_health_data=True), out_time=14.0)

    def _events(self):
        evs = RecordingEvent.query.filter_by(recording_id=self.rid, event_type=DIAGNOSTICS).all()
        return [e for e in evs if json.loads(e.extra_data or '{}').get('kind') == 'track_gaps']

    def test_the_analysis_phase_records_where_both_tracks_are_missing(self):
        self._use('overlap.ts')
        self._postprocess()
        events = self._events()
        self.assertEqual(len(events), 1, [e.detail for e in events])
        extra = json.loads(events[0].extra_data)
        self.assertEqual((extra['both_count'], extra['video_only_count'],
                          extra['audio_only_count']), (1, 1, 1))
        self.assertAlmostEqual(extra['both_seconds'], VIDEO_HOLE[1] - AUDIO_HOLE[0],
                               delta=LENGTH_TOLERANCE)
        self.assertEqual(len(extra['both']), 1)
        self.assertTrue(events[0].detail.startswith('Picture and sound gaps: Nothing on '
                                                    'either track'), events[0].detail)

    def test_a_picture_hole_with_sound_is_recorded_as_video_only(self):
        self._use('video_only.ts')
        self._postprocess()
        extra = json.loads(self._events()[0].extra_data)
        self.assertEqual((extra['both_count'], extra['video_only_count']), (0, 1))
        self.assertEqual(extra['both_seconds'], 0)

    def test_a_file_that_cannot_be_read_is_recorded_as_not_measured(self):
        """An absent event would read as never measured, a quiet one as no gaps."""
        self._use('clean.ts')
        with mock.patch('app.probe.track_gaps', return_value={}):
            ppmod._scan_track_gaps(self.rid, self.ts)
        events = self._events()
        self.assertEqual(len(events), 1)
        self.assertTrue(json.loads(events[0].extra_data).get('scan_failed'))
        self.assertIn('could not be measured', events[0].detail)


if __name__ == '__main__':
    unittest.main()
