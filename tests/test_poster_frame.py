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
import os
import unittest
from datetime import datetime, timedelta
from unittest.mock import patch

from app import db
from app.metadata_sidecar import (
    POSTER_FROM_FRAME, POSTER_FROM_PROFILE, POSTER_FROM_THUMBNAIL, POSTER_SUFFIX,
    write_sidecar,
)
from app.recorder import (
    FINISHED_IMAGE_LAST_FRAME, FINISHED_IMAGE_POSTER,
    POSTER_FRAME_AFTER_TARGET, POSTER_FRAME_AT_TARGET, POSTER_FRAME_PAST_END,
    finished_image_candidates, finished_image_path, persist_poster_frame,
    poster_frame_path, poster_frame_seek, recording_image_paths,
)
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

    def _capture(self, cfg=None, grab=None):
        """Run the capture with load_config() pinned at the temp tree and the frame grab
        faked. Returns the (filepath, seek_args) the grab was asked for, or None.

        load_config is patched rather than passed through make_test_app overrides: the
        code under test calls it at run time, where overrides are not visible and the real
        /dvr would be resolved (CLAUDE.md §Testing)."""
        asked = []

        def _fake(filepath, output_path, ffmpeg_path, probe=None, seek_args=None,
                  timeout=30):
            asked.append((filepath, seek_args))
            if grab is False:
                return False
            with open(output_path, 'wb') as fh:
                fh.write(b'posterbytes')
            return True

        with patch('app.recorder.load_config', return_value=cfg or self.cfg), \
                patch('app.screenshot.capture_screenshot', _fake):
            persist_poster_frame(self.rec.id)
        return asked[0] if asked else None


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
