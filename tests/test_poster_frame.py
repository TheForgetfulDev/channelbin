"""The poster frame a finished recording takes from inside the program itself.

Guards dev/changelog/1060. The only per-recording image before it was the final-frame
thumbnail, grabbed from the last segment with data - for a ball game a postgame graphic,
a commercial or the provider's slate. The poster frame is taken a configurable offset
after the recording's own snapshotted program start time, so front padding cannot put a
countdown clock or the previous show on the cover, and it is what the app shows for a
finished recording by default.

Most of the file is poster_frame_seek(), which is where the rule can be wrong without
anything erroring: a recording is many segments with gaps between them, and the moment
asked for may sit in one of those gaps, before the capture joined the feed at all, or past
everything captured.
"""
import json
import os
import shutil
import subprocess
import tempfile
import unittest
from datetime import datetime, timedelta
from unittest.mock import patch

from sqlalchemy.exc import OperationalError

from app import db
from app.database import DIAGNOSTICS, RecordingEvent
from app.metadata_sidecar import (
    POSTER_FROM_FRAME, POSTER_FROM_PROFILE, POSTER_FROM_THUMBNAIL, POSTER_SUFFIX,
    write_sidecar,
)
from app.recorder import (
    DIAGNOSTIC_KIND_POSTER_FRAME, FINAL_FRAME_BLACK, FINAL_FRAME_MISSING,
    FINAL_FRAME_PICTURE, FINAL_FRAME_UNCHECKED, FINISHED_IMAGE_LAST_FRAME,
    FINISHED_IMAGE_POSTER, POSTER_FRAME_AFTER_TARGET, POSTER_FRAME_AT_TARGET,
    POSTER_FRAME_PAST_END, POSTER_FRAME_TRIES,
    final_frame_path, finished_image_candidates, finished_image_path, persist_poster_frame,
    poster_frame_path, poster_frame_seek, recording_image_paths,
)
from app.screenshot import frame_bright_percent, is_black_frame
from tests.support import make_test_app, seed

T0 = datetime(2026, 9, 20, 18, 0, 0)


class _Seg:
    """The three columns poster_frame_seek() reads. A stand-in rather than a real row,
    because the rule is pure and testing it must not need a database or a file."""

    def __init__(self, number, start_offset, length, path='/x.ts'):
        self.segment_number = number
        self.started_at = T0 + timedelta(seconds=start_offset)
        self.ended_at = (None if length is None
                         else T0 + timedelta(seconds=start_offset + length))
        self.content_duration_seconds = None
        self.file_path = path


class PosterFrameSeekTests(unittest.TestCase):
    """Which segment holds a given moment, and how far into it to seek."""

    def test_the_target_inside_a_segment_seeks_to_it(self):
        segs = [_Seg(1, 0, 600)]
        seg, seek, how = poster_frame_seek(segs, T0 + timedelta(seconds=60))
        self.assertIs(seg, segs[0])
        self.assertEqual(seek, 60.0)
        self.assertEqual(how, POSTER_FRAME_AT_TARGET)

    def test_the_seek_is_measured_from_the_segments_own_start_not_the_recordings(self):
        """The whole point: segment 2 starts 600s in, so a target 660s into the recording
        is 60s into THAT file. Seeking 660s into it would land past its end, or worse,
        silently return a frame from the wrong minute."""
        segs = [_Seg(1, 0, 600), _Seg(2, 600, 600)]
        seg, seek, how = poster_frame_seek(segs, T0 + timedelta(seconds=660))
        self.assertIs(seg, segs[1])
        self.assertEqual(seek, 60.0)
        self.assertEqual(how, POSTER_FRAME_AT_TARGET)

    def test_a_target_in_a_gap_takes_the_top_of_the_next_segment(self):
        """The feed dropped at 600s and came back at 700s. Nothing exists at 650s, so the
        earliest content after the moment asked for is where the poster comes from."""
        segs = [_Seg(1, 0, 600), _Seg(2, 700, 600)]
        seg, seek, how = poster_frame_seek(segs, T0 + timedelta(seconds=650))
        self.assertIs(seg, segs[1])
        self.assertEqual(seek, 0.0)
        self.assertEqual(how, POSTER_FRAME_AFTER_TARGET)

    def test_a_target_before_the_capture_joined_takes_the_first_frame_it_has(self):
        """A recording that started after the program did - no padding, or a late start.
        Every frame it holds is already inside the program, so the first one is right."""
        segs = [_Seg(1, 300, 600)]
        seg, seek, how = poster_frame_seek(segs, T0 + timedelta(seconds=60))
        self.assertIs(seg, segs[0])
        self.assertEqual(seek, 0.0)
        self.assertEqual(how, POSTER_FRAME_AFTER_TARGET)

    def test_a_target_past_everything_captured_falls_back_to_the_last_segment(self):
        """A listing that said the program started later than it did. A frame from the
        wrong minute of this recording beats no cover at all."""
        segs = [_Seg(1, 0, 600), _Seg(2, 600, 600)]
        seg, seek, how = poster_frame_seek(segs, T0 + timedelta(seconds=5000))
        self.assertIs(seg, segs[1])
        self.assertEqual(seek, 0.0)
        self.assertEqual(how, POSTER_FRAME_PAST_END)

    def test_a_segment_whose_end_is_unknown_does_not_swallow_every_later_target(self):
        """ended_at NULL means the row never recorded where it stopped. Treating that as
        "covers everything after its start" would pin every poster to that one file."""
        segs = [_Seg(1, 0, None), _Seg(2, 600, 600)]
        seg, seek, how = poster_frame_seek(segs, T0 + timedelta(seconds=700))
        self.assertIs(seg, segs[1])
        self.assertEqual(how, POSTER_FRAME_AT_TARGET)

    def test_content_duration_stands_in_when_the_wall_clock_end_is_missing(self):
        segs = [_Seg(1, 0, None)]
        segs[0].content_duration_seconds = 600
        seg, seek, how = poster_frame_seek(segs, T0 + timedelta(seconds=60))
        self.assertIs(seg, segs[0])
        self.assertEqual(seek, 60.0)
        self.assertEqual(how, POSTER_FRAME_AT_TARGET)

    def test_nothing_to_grab_returns_no_segment(self):
        self.assertEqual(poster_frame_seek([], T0), (None, 0.0, None))


class _CaptureTestCase(unittest.TestCase):
    """A recording with real segment files on disk, and a fake frame grab."""

    def setUp(self):
        self.t = make_test_app()
        self.ctx = self.t.app.app_context()
        self.ctx.push()
        self.account = seed.make_account()
        self.channel = seed.make_channel(self.account, name='Speed HD')
        # Ten minutes of front padding: the capture starts at T0, the program at T0+600.
        self.rec = seed.make_recording(
            status='COMPLETED', channel_id=self.channel.id,
            program_title='NASCAR Cup Series',
            program_start_time=T0 + timedelta(seconds=600))
        db.session.commit()
        self.images = os.path.join(self.t._tmpdir, 'images')
        self.segdir = os.path.join(self.t._tmpdir, 'segments')
        os.makedirs(self.segdir, exist_ok=True)
        self.cfg = {'recording': {'images_dir': self.images,
                                  'live_thumbnail': {'enabled': True}}}

    def tearDown(self):
        self.ctx.pop()
        self.t.cleanup()

    def _segment(self, number, start_offset, length, data=b'tsbytes'):
        seg = seed.make_segment(self.rec, self.channel,
                                started_at=T0 + timedelta(seconds=start_offset),
                                ended_at=T0 + timedelta(seconds=start_offset + length),
                                segment_number=number)
        seg.file_path = os.path.join(self.segdir, f'seg{number}.ts')
        with open(seg.file_path, 'wb') as fh:
            fh.write(data)
        db.session.commit()
        return seg

    def _capture(self, cfg=None, grab=None, bright=()):
        """Run the capture with load_config() pinned at the temp tree and both the frame
        grab and the black test faked. Returns the (filepath, seek_args) the first grab was
        asked for, or None; self.asked holds every grab.

        `bright` scripts frame_bright_percent() in call order, and once it runs out every
        frame reads as a real picture. `grab` False fails every grab, and a number fails
        that grab (0-based) and every one after it.

        load_config is patched rather than passed through make_test_app overrides: the
        code under test calls it at run time, where overrides are not visible and the real
        /dvr would be resolved (CLAUDE.md §Testing)."""
        self.asked = asked = []
        scripted = list(bright)

        def _fake(filepath, output_path, ffmpeg_path, probe=None, seek_args=None,
                  timeout=30):
            asked.append((filepath, seek_args))
            if grab is False or (grab is not None and len(asked) > grab):
                return False
            with open(output_path, 'wb') as fh:
                fh.write(b'posterbytes')
            return True

        def _fake_bright(filepath, ffmpeg_path, timeout=10):
            return scripted.pop(0) if scripted else 50.0

        with patch('app.recorder.load_config', return_value=cfg or self.cfg), \
                patch('app.screenshot.capture_screenshot', _fake), \
                patch('app.screenshot.frame_bright_percent', _fake_bright):
            persist_poster_frame(self.rec.id)
        return asked[0] if asked else None

    def _seeks(self):
        return [(os.path.basename(path), args[1]) for path, args in self.asked]

    def _poster_event(self):
        """The one DIAGNOSTICS row the capture wrote, as (detail, extra), or None."""
        rows = RecordingEvent.query.filter_by(recording_id=self.rec.id,
                                              event_type=DIAGNOSTICS).all()
        self.assertLessEqual(len(rows), 1)
        return (rows[0].detail, json.loads(rows[0].extra_data)) if rows else None

    def _has_poster(self):
        return os.path.exists(poster_frame_path(self.rec.id, self.cfg))

    def _assert_no_temp_frame_left(self):
        folder = os.path.dirname(poster_frame_path(self.rec.id, self.cfg))
        names = os.listdir(folder) if os.path.isdir(folder) else []
        self.assertEqual([n for n in names if '.tmp.' in n], [])


class PosterFrameCaptureTests(_CaptureTestCase):
    """Taking the frame, from a file already on disk."""

    def test_the_frame_is_taken_from_the_segment_holding_the_program_start_offset(self):
        self._segment(1, 0, 600)      # padding
        self._segment(2, 600, 1800)   # the program
        filepath, seek_args = self._capture()
        self.assertEqual(os.path.basename(filepath), 'seg2.ts')
        # 60s past the program's start, which is 60s into the segment that begins with it.
        self.assertEqual(seek_args, ['-ss', '60.00'])
        self.assertTrue(os.path.exists(poster_frame_path(self.rec.id, self.cfg)))

    def test_the_offset_is_anchored_on_the_program_not_on_the_recording(self):
        """The defect this whole item exists to prevent: with ten minutes of padding, an
        offset measured from the capture's start lands on the previous show."""
        self._segment(1, 0, 3000)
        filepath, seek_args = self._capture()
        self.assertEqual(seek_args, ['-ss', '660.00'])

    def test_the_offset_is_configurable(self):
        self._segment(1, 0, 3000)
        cfg = {'recording': {'images_dir': self.images,
                             'live_thumbnail': {'enabled': True,
                                                'poster_frame_offset_seconds': 300}}}
        _, seek_args = self._capture(cfg=cfg)
        self.assertEqual(seek_args, ['-ss', '900.00'])

    def test_a_manual_recording_with_no_program_falls_back_to_its_own_start(self):
        """A URL-only recording carries no program_start_time, exactly as it carries no
        program_title. 60s into what it captured still beats the final frame."""
        self.rec.program_start_time = None
        db.session.commit()
        self._segment(1, 0, 3000)
        _, seek_args = self._capture()
        self.assertEqual(seek_args, ['-ss', '60.00'])

    def test_an_excluded_segment_is_never_the_source(self):
        """A discarded placeholder clip is the provider's slate, which is the one image
        that must not become a cover."""
        placeholder = self._segment(1, 600, 600)
        placeholder.excluded_reason = 'PROVIDER_PLACEHOLDER'
        db.session.commit()
        self._segment(2, 1200, 600)
        filepath, _ = self._capture()
        self.assertEqual(os.path.basename(filepath), 'seg2.ts')

    def test_a_segment_whose_file_is_gone_is_never_the_source(self):
        gone = self._segment(1, 600, 600)
        os.remove(gone.file_path)
        self._segment(2, 1200, 600)
        filepath, _ = self._capture()
        self.assertEqual(os.path.basename(filepath), 'seg2.ts')

    def test_nothing_captured_means_no_poster_and_no_crash(self):
        self.assertIsNone(self._capture())
        self.assertFalse(os.path.exists(poster_frame_path(self.rec.id, self.cfg)))

    def test_a_failed_grab_leaves_no_poster_and_does_not_raise(self):
        """Never fails the recording: the capture is finished and on disk by now."""
        self._segment(1, 0, 3000)
        self._capture(grab=False)
        self.assertFalse(os.path.exists(poster_frame_path(self.rec.id, self.cfg)))

    def test_the_live_thumbnail_switch_turns_the_capture_off(self):
        self._segment(1, 0, 3000)
        cfg = {'recording': {'images_dir': self.images,
                             'live_thumbnail': {'enabled': False}}}
        self.assertIsNone(self._capture(cfg=cfg))

    def test_a_nonsense_offset_falls_back_to_the_program_start(self):
        self._segment(1, 0, 3000)
        cfg = {'recording': {'images_dir': self.images,
                             'live_thumbnail': {'enabled': True,
                                                'poster_frame_offset_seconds': 'soon'}}}
        _, seek_args = self._capture(cfg=cfg)
        self.assertEqual(seek_args, ['-ss', '600.00'])


BLACK = 0.0
PICTURE = 50.0


class BlackPosterFrameRetryTests(_CaptureTestCase):
    """A black frame is not used: the target moves forward and it grabs again, from disk.

    Recording 17's cover was solid black, and nothing looked (dev/changelog/1433)."""

    def test_a_black_frame_moves_the_target_forward_one_step(self):
        self._segment(1, 0, 3000)
        self._capture(bright=[BLACK, PICTURE])
        self.assertEqual(self._seeks(), [('seg1.ts', '660.00'), ('seg1.ts', '720.00')])
        self.assertTrue(self._has_poster())
        self._assert_no_temp_frame_left()

    def test_the_recording_says_which_frame_was_skipped_and_which_was_used(self):
        self._segment(1, 0, 3000)
        self._capture(bright=[BLACK, BLACK, PICTURE])
        detail, extra = self._poster_event()
        self.assertEqual(extra['kind'], DIAGNOSTIC_KIND_POSTER_FRAME)
        self.assertEqual(extra['black_frames'],
                         ['segment 1 at 660.00s', 'segment 1 at 720.00s'])
        self.assertEqual(extra['frame_used'], 'segment 1 at 780.00s')
        self.assertIn('skipped 2 black frames', detail)
        self.assertIn('used segment 1 at 780.00s', detail)

    def test_the_log_names_each_skipped_frame_and_the_one_used(self):
        self._segment(1, 0, 3000)
        with self.assertLogs('app.recorder', 'INFO') as logs:
            self._capture(bright=[BLACK, PICTURE])
        text = '\n'.join(logs.output)
        self.assertIn('skipped a black poster frame, segment 1 at 660.00s', text)
        self.assertIn('poster frame taken 120s after program start, 720.00s into segment 1', text)

    def test_a_real_frame_on_the_first_try_grabs_once_and_says_nothing(self):
        self._segment(1, 0, 3000)
        self._capture(bright=[PICTURE])
        self.assertEqual(len(self.asked), 1)
        self.assertIsNone(self._poster_event())

    def test_the_step_is_configurable(self):
        self._segment(1, 0, 3000)
        cfg = {'recording': {'images_dir': self.images,
                             'live_thumbnail': {'enabled': True,
                                                'poster_frame_retry_step_seconds': 300}}}
        self._capture(cfg=cfg, bright=[BLACK, PICTURE])
        self.assertEqual(self._seeks(), [('seg1.ts', '660.00'), ('seg1.ts', '960.00')])

    def test_a_step_that_would_grab_the_same_frame_again_takes_the_default(self):
        self._segment(1, 0, 3000)
        for nonsense in (0, -5, 'soon', None):
            with self.subTest(step=nonsense):
                cfg = {'recording': {'images_dir': self.images,
                                     'live_thumbnail': {
                                         'enabled': True,
                                         'poster_frame_retry_step_seconds': nonsense}}}
                self._capture(cfg=cfg, bright=[BLACK, PICTURE])
                self.assertEqual(self._seeks(), [('seg1.ts', '660.00'), ('seg1.ts', '720.00')])

    def test_the_tries_have_a_limit(self):
        """Every try happens inside the join, so "keep looking" has to end."""
        self._segment(1, 0, 30000)
        self._capture(bright=[BLACK] * 20)
        self.assertEqual(len(self.asked), 5)
        self.assertFalse(self._has_poster())
        self._assert_no_temp_frame_left()
        _, extra = self._poster_event()
        self.assertEqual(len(extra['black_frames']), 5)

    def test_a_gap_does_not_grab_the_same_frame_at_every_try(self):
        """The feed is gone from 630s to 1000s. A step from the target asked for lands on
        the top of segment 2 six times over, and 5 tries would be spent on one frame."""
        self._segment(1, 600, 30)
        self._segment(2, 1000, 600)
        self._capture(bright=[BLACK, PICTURE])
        self.assertEqual(self._seeks(), [('seg2.ts', '0.00'), ('seg2.ts', '60.00')])

    def test_a_retry_past_the_end_stops_instead_of_going_backward(self):
        """Past the end answers the top of the last segment, which here is front padding
        and could be the previous show. A retry only ever looks later."""
        self._segment(1, 0, 680)
        self._capture(bright=[BLACK])
        self.assertEqual(self._seeks(), [('seg1.ts', '660.00')])
        self.assertFalse(self._has_poster())

    def test_a_black_frame_from_past_the_end_is_not_grabbed_again(self):
        self._segment(1, 0, 100)
        self._capture(bright=[BLACK])
        self.assertEqual(self._seeks(), [('seg1.ts', '0.00')])
        self.assertFalse(self._has_poster())
        self._assert_no_temp_frame_left()

    def test_a_grab_that_fails_after_a_black_frame_still_says_what_it_found(self):
        self._segment(1, 0, 3000)
        self._capture(grab=1, bright=[BLACK])
        self.assertEqual(len(self.asked), 2)
        self.assertFalse(self._has_poster())
        _, extra = self._poster_event()
        self.assertEqual(extra['black_frames'], ['segment 1 at 660.00s'])

    def test_a_frame_that_cannot_be_checked_is_kept(self):
        """Throwing a picture away on no evidence is the worse mistake."""
        self._segment(1, 0, 3000)
        self._capture(bright=[None])
        self.assertEqual(len(self.asked), 1)
        self.assertTrue(self._has_poster())
        self.assertIsNone(self._poster_event())

    def test_a_database_failure_on_the_event_does_not_reach_the_join(self):
        """A diagnostic must never harm what it describes: the poster is already in place."""
        self._segment(1, 0, 3000)
        with patch('app.recorder.add_recording_event',
                   side_effect=OperationalError('INSERT', {}, Exception('disk I/O error'))):
            self._capture(bright=[BLACK, PICTURE])
        self.assertTrue(self._has_poster())


class EveryPosterTryBlackTests(_CaptureTestCase):
    """The fallback is checked rather than assumed to be better."""

    def _all_black(self, final_frame_bright, write_thumbnail=True):
        self._segment(1, 0, 30000)
        if write_thumbnail:
            path = final_frame_path(self.rec.id, self.cfg)
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, 'wb') as fh:
                fh.write(b'thumb')
        self._capture(bright=[BLACK] * POSTER_FRAME_TRIES + [final_frame_bright])
        self.assertFalse(self._has_poster())
        return self._poster_event()

    def test_a_real_final_frame_is_the_cover(self):
        detail, extra = self._all_black(PICTURE)
        self.assertEqual(extra['final_frame'], FINAL_FRAME_PICTURE)
        self.assertIn('so the cover is the final frame', detail)

    def test_a_black_final_frame_is_said_out_loud(self):
        with self.assertLogs('app.recorder', 'WARNING') as logs:
            detail, extra = self._all_black(BLACK)
        self.assertEqual(extra['final_frame'], FINAL_FRAME_BLACK)
        self.assertIn('the final frame is black too', detail)
        self.assertIn('the final frame is black too', '\n'.join(logs.output))

    def test_a_missing_final_frame_means_no_cover(self):
        detail, extra = self._all_black(None, write_thumbnail=False)
        self.assertEqual(extra['final_frame'], FINAL_FRAME_MISSING)
        self.assertIn('there is no cover', detail)

    def test_a_final_frame_that_cannot_be_checked_says_so(self):
        detail, extra = self._all_black(None)
        self.assertEqual(extra['final_frame'], FINAL_FRAME_UNCHECKED)
        self.assertIn('could not be checked for black', detail)


_HAVE_FFMPEG = bool(shutil.which('ffmpeg'))


def _make_frame(path, source):
    subprocess.run(['ffmpeg', '-v', 'error', '-y', '-f', 'lavfi', '-i', source,
                    '-frames:v', '1', '-q:v', '3', path], check=True, timeout=30)


@unittest.skipUnless(_HAVE_FFMPEG, 'ffmpeg not available')
class BlackFrameRuleTests(unittest.TestCase):
    """The rule against real ffmpeg output, at the size a poster frame actually is."""

    SIZE = 's=1920x1080'

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)

    def _bright(self, source):
        path = os.path.join(self.tmp, 'frame.jpg')
        _make_frame(path, source)
        return frame_bright_percent(path, 'ffmpeg')

    def _box(self, x, y, w, h, color='white'):
        return f'drawbox=x={x}:y={y}:w={w}:h={h}:color={color}:t=fill'

    def test_pure_black_is_black(self):
        bright = self._bright(f'color=black:{self.SIZE}')
        self.assertEqual(bright, 0.0)
        self.assertTrue(is_black_frame(bright))

    def test_broadcast_black_with_grain_is_black(self):
        self.assertTrue(is_black_frame(
            self._bright(f'color=c=0x101010:{self.SIZE},noise=alls=8:allf=t')))

    def test_a_station_logo_alone_on_black_is_black(self):
        self.assertTrue(is_black_frame(
            self._bright(f'color=black:{self.SIZE},{self._box(1700, 950, 160, 70)}')))

    def test_a_dark_scene_with_some_light_in_it_is_not_black(self):
        """The shape the darkest real pictures had: 6% or more of the frame lit."""
        bright = self._bright(f'color=c=0x080808:{self.SIZE},'
                              f'{self._box(300, 200, 600, 300, "0x505050")}')
        self.assertGreater(bright, 5.0)
        self.assertFalse(is_black_frame(bright))

    def test_scattered_lights_on_a_dark_track_are_not_black(self):
        """A night race: most of the frame is dark and the lit share is barely over the line."""
        lights = ','.join(self._box(x, y, 60, 60) for x, y in
                          ((100, 100), (500, 300), (900, 200), (1300, 400), (1500, 150), (300, 700)))
        bright = self._bright(f'color=c=0x0a0a0a:{self.SIZE},{lights}')
        self.assertFalse(is_black_frame(bright))

    def test_an_unreadable_file_measures_as_nothing_and_is_not_black(self):
        path = os.path.join(self.tmp, 'junk.jpg')
        with open(path, 'wb') as fh:
            fh.write(b'not a picture')
        bright = frame_bright_percent(path, 'ffmpeg')
        self.assertIsNone(bright)
        self.assertFalse(is_black_frame(bright))


@unittest.skipUnless(_HAVE_FFMPEG, 'ffmpeg not available')
class BlackPosterFrameEndToEndTests(_CaptureTestCase):
    """The real grab and the real test over a real segment file, with nothing faked."""

    def test_a_program_that_opens_black_gets_a_real_frame_from_later(self):
        self.rec.program_start_time = None
        db.session.commit()
        seg = self._segment(1, 0, 180)
        # 90 seconds of black, then 90 of picture, with a keyframe every second.
        subprocess.run(
            ['ffmpeg', '-v', 'error', '-y',
             '-f', 'lavfi', '-i', 'color=black:s=320x180:r=5:d=90',
             '-f', 'lavfi', '-i', 'testsrc2=s=320x180:r=5:d=90',
             '-filter_complex', '[0:v][1:v]concat=n=2:v=1[v]', '-map', '[v]',
             '-c:v', 'libx264', '-preset', 'ultrafast', '-g', '5', '-f', 'mpegts',
             seg.file_path], check=True, timeout=120)
        with patch('app.recorder.load_config', return_value=self.cfg):
            persist_poster_frame(self.rec.id)
        path = poster_frame_path(self.rec.id, self.cfg)
        self.assertTrue(os.path.exists(path))
        self.assertFalse(is_black_frame(frame_bright_percent(path, 'ffmpeg')))
        _, extra = self._poster_event()
        self.assertEqual(extra['black_frames'], ['segment 1 at 60.00s'])
        self.assertEqual(extra['frame_used'], 'segment 1 at 120.00s')
        self._assert_no_temp_frame_left()


class FinishedImageChoiceTests(_CaptureTestCase):
    """Which of the two images a finished recording shows."""

    def _write(self, path):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, 'wb') as fh:
            fh.write(b'img')
        return path

    def _cfg(self, choice):
        return {'recording': {'images_dir': self.images,
                              'live_thumbnail': {'enabled': True,
                                                 'finished_image': choice}}}

    def _thumb(self):
        return self._write(os.path.join(self.images, 'thumbnails', f'{self.rec.id}.jpg'))

    def _poster(self):
        return self._write(poster_frame_path(self.rec.id, self.cfg))

    def test_the_poster_frame_is_the_default(self):
        self._thumb()
        poster = self._poster()
        self.assertEqual(finished_image_path(self.rec.id, self.cfg), poster)

    def test_the_setting_selects_the_final_frame_instead(self):
        thumb = self._thumb()
        self._poster()
        self.assertEqual(
            finished_image_path(self.rec.id, self._cfg(FINISHED_IMAGE_LAST_FRAME)), thumb)

    def test_the_unchosen_image_is_the_fallback_in_both_directions(self):
        """Every recording made before the poster frame existed has only a thumbnail, and
        a recording whose poster capture failed has only one too. Neither shows a
        placeholder just because the preferred file is absent."""
        thumb = self._thumb()
        self.assertEqual(
            finished_image_path(self.rec.id, self._cfg(FINISHED_IMAGE_POSTER)), thumb)
        os.remove(thumb)
        poster = self._poster()
        self.assertEqual(
            finished_image_path(self.rec.id, self._cfg(FINISHED_IMAGE_LAST_FRAME)), poster)

    def test_neither_image_returns_none(self):
        self.assertIsNone(finished_image_path(self.rec.id, self.cfg))

    def test_the_preference_orders_the_candidates(self):
        poster_first = finished_image_candidates(
            self.rec.id, self._cfg(FINISHED_IMAGE_POSTER))
        thumb_first = finished_image_candidates(
            self.rec.id, self._cfg(FINISHED_IMAGE_LAST_FRAME))
        self.assertEqual(poster_first, list(reversed(thumb_first)))

    def test_an_unknown_stored_value_shows_the_poster_rather_than_nothing(self):
        """The route refuses anything but the two choices, so this can only come from a
        hand-edited config.yaml - which must still render a page."""
        self._thumb()
        poster = self._poster()
        self.assertEqual(finished_image_path(self.rec.id, self._cfg('surprise')), poster)


class PosterFrameTeardownTests(_CaptureTestCase):
    """The poster frame is removed on every delete, including a keep-the-files one."""

    def test_the_poster_frame_is_enumerated_with_the_thumbnail(self):
        paths = recording_image_paths(self.rec.id, self.cfg)
        self.assertIn(poster_frame_path(self.rec.id, self.cfg), paths)
        self.assertIn(os.path.join(self.images, 'thumbnails', f'{self.rec.id}.jpg'), paths)

    def test_an_interrupted_capture_leaves_no_temp_file_behind(self):
        folder = os.path.join(self.images, 'poster-frames')
        os.makedirs(folder, exist_ok=True)
        stale = os.path.join(folder, f'{self.rec.id}.abc123.tmp.jpg')
        with open(stale, 'wb') as fh:
            fh.write(b'partial')
        self.assertIn(stale, recording_image_paths(self.rec.id, self.cfg))

    def test_another_recordings_files_are_never_enumerated(self):
        """Anchored on f'{id}.' so recording 6 never matches recording 64's files."""
        folder = os.path.join(self.images, 'poster-frames')
        os.makedirs(folder, exist_ok=True)
        other = os.path.join(folder, f'{self.rec.id}0.def456.tmp.jpg')
        with open(other, 'wb') as fh:
            fh.write(b'someone else')
        self.assertNotIn(other, recording_image_paths(self.rec.id, self.cfg))


class SidecarPosterSourceTests(unittest.TestCase):
    """The poster copied beside the video: pinned profile image, then the poster frame,
    then the final-frame thumbnail."""

    def setUp(self):
        self.t = make_test_app()
        self.ctx = self.t.app.app_context()
        self.ctx.push()
        self.account = seed.make_account()
        self.channel = seed.make_channel(self.account, name='Speed HD')
        self.rec = seed.make_recording(status='COMPLETED', channel_id=self.channel.id,
                                       program_title='NASCAR Cup Series')
        db.session.commit()
        self.images = os.path.join(self.t._tmpdir, 'images')
        self.dvr = os.path.join(self.t._tmpdir, 'dvr')
        os.makedirs(self.dvr, exist_ok=True)
        self.video = os.path.join(self.dvr, 'Race.mp4')
        with open(self.video, 'wb') as fh:
            fh.write(b'not really video')
        self.cfg = {'recording': {'metadata_sidecar': {'enabled': True},
                                  'images_dir': self.images}}

    def tearDown(self):
        self.ctx.pop()
        self.t.cleanup()

    def _write(self, path, data):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, 'wb') as fh:
            fh.write(data)

    def _source(self):
        from app.database import METADATA_SIDECAR_WRITTEN, RecordingEvent
        import json
        ev = RecordingEvent.query.filter_by(
            recording_id=self.rec.id, event_type=METADATA_SIDECAR_WRITTEN).first()
        return json.loads(ev.extra_data)['poster_source']

    def test_the_poster_frame_beats_the_final_frame_thumbnail(self):
        self._write(os.path.join(self.images, 'thumbnails', f'{self.rec.id}.jpg'), b'last')
        self._write(poster_frame_path(self.rec.id, self.cfg), b'program')
        write_sidecar(self.rec.id, self.video, self.cfg)
        with open(os.path.join(self.dvr, 'Race' + POSTER_SUFFIX), 'rb') as fh:
            self.assertEqual(fh.read(), b'program')
        self.assertEqual(self._source(), POSTER_FROM_FRAME)

    def test_the_thumbnail_is_still_the_floor(self):
        """A recording made before the poster frame existed, or one whose capture failed,
        keeps the cover it always had rather than losing its artwork."""
        self._write(os.path.join(self.images, 'thumbnails', f'{self.rec.id}.jpg'), b'last')
        write_sidecar(self.rec.id, self.video, self.cfg)
        with open(os.path.join(self.dvr, 'Race' + POSTER_SUFFIX), 'rb') as fh:
            self.assertEqual(fh.read(), b'last')
        self.assertEqual(self._source(), POSTER_FROM_THUMBNAIL)

    def test_a_pinned_profile_image_still_wins_over_the_poster_frame(self):
        """The one source the user chose outright (dev/changelog/1059)."""
        from app.database import RecordingProfile
        # A name store_poster() could actually have produced - poster_path() refuses any
        # other, which is the directory-traversal guard doing its job.
        stored = 'profile-1-abcdef123456.jpg'
        self._write(os.path.join(self.images, 'posters', stored), b'logo')
        profile = RecordingProfile(name='NASCAR', poster_file=stored)
        db.session.add(profile)
        db.session.commit()
        self._write(poster_frame_path(self.rec.id, self.cfg), b'program')
        write_sidecar(self.rec.id, self.video, self.cfg, profile=profile)
        with open(os.path.join(self.dvr, 'Race' + POSTER_SUFFIX), 'rb') as fh:
            self.assertEqual(fh.read(), b'logo')
        self.assertEqual(self._source(), POSTER_FROM_PROFILE)

    def test_the_event_says_where_the_cover_came_from(self):
        """Principle 1: a recording whose cover is not what the user expects can say why
        from its own timeline."""
        from app.database import METADATA_SIDECAR_WRITTEN, RecordingEvent
        self._write(poster_frame_path(self.rec.id, self.cfg), b'program')
        write_sidecar(self.rec.id, self.video, self.cfg)
        ev = RecordingEvent.query.filter_by(
            recording_id=self.rec.id, event_type=METADATA_SIDECAR_WRITTEN).first()
        self.assertIn('a frame from inside the program', ev.detail)


if __name__ == '__main__':
    unittest.main()
