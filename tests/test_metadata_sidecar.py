"""The .nfo file and poster a finished recording uses to describe itself.

Guards dev/changelog/1057: with `recording.metadata_sidecar.enabled` on, a completed
recording gets `<basename>.nfo` and `<basename>-poster.jpg` beside its video, carrying the
program's real title, synopsis, air date and genre. The gate is global with a tri-state
per-profile override, both files are enumerated for teardown, and a write that fails says
so on the recording's own timeline rather than failing the recording.
"""
import os
import unittest
import xml.etree.ElementTree as ET
from datetime import datetime
from unittest.mock import patch

from app import db
from app.database import (
    RecordingEvent, RecordingProfile,
    METADATA_SIDECAR_WRITTEN, METADATA_SIDECAR_FAILED,
)
from app.metadata_sidecar import (
    NFO_SUFFIX, POSTER_SUFFIX, render_nfo, sidecar_enabled, sidecar_paths, write_sidecar,
)
from app.tz_utils import get_display_tz
from tests.support import make_test_app, seed


def _cfg(enabled=True):
    return {'recording': {'metadata_sidecar': {'enabled': enabled},
                          'images_dir': '/nonexistent-images'}}


class SidecarPathTests(unittest.TestCase):
    """The names, spelled in one place: the .nfo, then every name a poster can take."""

    def test_both_sidecars_hang_off_the_video_stem(self):
        nfo, poster, poster_png = sidecar_paths('/dvr/Race Day.mp4')
        self.assertEqual(nfo, '/dvr/Race Day.nfo')
        self.assertEqual(poster, '/dvr/Race Day-poster.jpg')
        # A poster pinned to the profile keeps its own format, so teardown has to know
        # the PNG name too (dev/changelog/1059).
        self.assertEqual(poster_png, '/dvr/Race Day-poster.png')

    def test_the_stem_is_taken_without_the_extension_not_by_appending(self):
        """A .ts source and its .mp4 conversion must resolve to the SAME sidecar names, or
        a recording converted after its sidecar was written would own two of each."""
        self.assertEqual(sidecar_paths('/dvr/Game.ts'), sidecar_paths('/dvr/Game.mp4'))

    def test_a_dot_in_the_folder_name_does_not_split_the_stem(self):
        nfo = sidecar_paths('/dvr/season.2026/Game.mp4')[0]
        self.assertEqual(nfo, '/dvr/season.2026/Game.nfo')


class GateTests(unittest.TestCase):
    """Global setting, overridden per profile by a tri-state that is never read as truthy."""

    def test_off_globally_with_no_profile(self):
        self.assertFalse(sidecar_enabled(_cfg(False), None))

    def test_on_globally_with_no_profile(self):
        self.assertTrue(sidecar_enabled(_cfg(True), None))

    def test_a_profile_that_inherits_follows_the_global(self):
        profile = RecordingProfile(name='P', metadata_sidecar_enabled=None)
        self.assertTrue(sidecar_enabled(_cfg(True), profile))
        self.assertFalse(sidecar_enabled(_cfg(False), profile))

    def test_a_profile_can_turn_it_off_while_the_global_is_on(self):
        """The case a truthiness test would silently drop: False and None are different
        answers, and only one of them is the user's (app/profile_forms.py)."""
        profile = RecordingProfile(name='P', metadata_sidecar_enabled=False)
        self.assertFalse(sidecar_enabled(_cfg(True), profile))

    def test_a_profile_can_turn_it_on_while_the_global_is_off(self):
        profile = RecordingProfile(name='P', metadata_sidecar_enabled=True)
        self.assertTrue(sidecar_enabled(_cfg(False), profile))

    def test_a_missing_config_block_reads_as_off(self):
        """An install whose config.yaml predates this key must not start writing files."""
        self.assertFalse(sidecar_enabled({'recording': {}}, None))


class RenderTests(unittest.TestCase):
    """What the XML actually says."""

    def setUp(self):
        self.t = make_test_app()
        self.ctx = self.t.app.app_context()
        self.ctx.push()
        self.account = seed.make_account()
        self.channel = seed.make_channel(self.account, name='Speed HD')
        self.rec = seed.make_recording(
            status='COMPLETED', name='2026-09-20 - Race - Daytona - Speed HD',
            channel_id=self.channel.id,
            program_title='NASCAR Cup Series',
            program_sub_title='Daytona 500',
            program_start_time=datetime(2026, 9, 20, 18, 30),
            metadata_description='Forty cars, one rain delay.',
            metadata_category='Sports',
            metadata_rating='TV-PG')
        db.session.commit()
        self.tz = get_display_tz()

    def tearDown(self):
        self.ctx.pop()
        self.t.cleanup()

    def _root(self, **kw):
        return ET.fromstring(render_nfo(self.rec, self.tz, **kw))

    def test_the_document_is_a_movie(self):
        self.assertEqual(self._root().tag, 'movie')

    def test_it_declares_its_encoding(self):
        """Without the declaration a reader guesses, and a synopsis carrying an accented
        character is what it guesses wrong on."""
        self.assertTrue(render_nfo(self.rec, self.tz).startswith(
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'))

    def test_the_title_joins_the_program_title_and_its_sub_title(self):
        """A provider's title names the strand, not the showing - every race is "NASCAR
        Cup Series" - so a library built on it alone is a wall of identical rows."""
        self.assertEqual(self._root().findtext('title'),
                         'NASCAR Cup Series - Daytona 500')

    def test_the_title_is_the_program_title_alone_when_there_is_no_sub_title(self):
        self.rec.program_sub_title = None
        self.assertEqual(self._root().findtext('title'), 'NASCAR Cup Series')

    def test_a_manual_recording_falls_back_to_its_own_name(self):
        """A URL-only recording carries no program_title, exactly as it carries no
        program_start_time - and an untitled library item is useless."""
        self.rec.program_title = None
        self.rec.program_sub_title = None
        self.assertEqual(self._root().findtext('title'),
                         '2026-09-20 - Race - Daytona - Speed HD')

    def test_the_sub_title_is_not_also_emitted_as_a_tagline(self):
        """It is already inside <title>, and a reader that renders both prints it twice -
        seen on a real recording: "NASCAR Cup Series - Enjoy Illinois 300" with "Enjoy
        Illinois 300" again directly underneath."""
        self.assertIsNone(self._root().find('tagline'))

    def test_the_synopsis_genre_and_rating_come_off_the_row(self):
        root = self._root()
        self.assertEqual(root.findtext('plot'), 'Forty cars, one rain delay.')
        self.assertEqual(root.findtext('genre'), 'Sports')
        self.assertEqual(root.findtext('mpaa'), 'TV-PG')

    def test_the_air_date_is_the_programs_own_start_in_local_time(self):
        """Anchored on program_start_time, not on when capture began: padding, a late
        start or a retry all move the recording's own start and none of them move the
        date the program aired."""
        with patch('app.metadata_sidecar.to_local',
                   side_effect=lambda dt, tz: dt.replace(hour=14)):
            root = self._root()
        self.assertEqual(root.findtext('premiered'), '2026-09-20')
        self.assertEqual(root.findtext('year'), '2026')

    def test_a_manual_recording_carries_no_air_date_rather_than_a_wrong_one(self):
        self.rec.program_start_time = None
        root = self._root()
        self.assertIsNone(root.find('premiered'))
        self.assertIsNone(root.find('year'))

    def test_the_channel_is_named_as_the_studio(self):
        self.assertEqual(self._root().findtext('studio'), 'Speed HD')

    def test_an_empty_field_is_absent_rather_than_an_empty_element(self):
        """Some readers take <plot></plot> as "known to be blank" and stop looking, so an
        absent element is the honest rendering of "this recording has no synopsis"."""
        self.rec.metadata_description = ''
        self.rec.metadata_category = None
        root = self._root()
        self.assertIsNone(root.find('plot'))
        self.assertIsNone(root.find('genre'))

    def test_the_poster_is_referenced_by_basename_only(self):
        """An absolute path stops resolving the moment the library is mounted somewhere
        else - a container, another machine, a renamed share."""
        thumb = self._root(poster_name='Race-poster.jpg').find('thumb')
        self.assertEqual(thumb.text, 'Race-poster.jpg')
        self.assertEqual(thumb.get('aspect'), 'poster')

    def test_no_thumb_element_when_no_poster_was_written(self):
        self.assertIsNone(self._root().find('thumb'))

    def test_markup_in_provider_text_is_escaped_rather_than_injected(self):
        """Provider EPG is external feed data and is not trusted to be XML-safe; an
        unescaped ampersand alone makes the whole file unparseable."""
        self.rec.metadata_description = 'Tom & Jerry <b>live</b>'
        root = self._root()
        self.assertEqual(root.findtext('plot'), 'Tom & Jerry <b>live</b>')
        self.assertIn('&amp;', render_nfo(self.rec, self.tz))


class WriteTests(unittest.TestCase):
    """Writing the pair beside a real file, and saying so."""

    def setUp(self):
        self.t = make_test_app()
        self.ctx = self.t.app.app_context()
        self.ctx.push()
        self.account = seed.make_account()
        self.channel = seed.make_channel(self.account, name='Speed HD')
        self.rec = seed.make_recording(
            status='COMPLETED', channel_id=self.channel.id,
            program_title='NASCAR Cup Series',
            program_start_time=datetime(2026, 9, 20, 18, 30),
            metadata_description='Forty cars, one rain delay.')
        db.session.commit()
        self.dvr = os.path.join(self.t._tmpdir, 'dvr')
        self.thumbs = os.path.join(self.t._tmpdir, 'images', 'thumbnails')
        os.makedirs(self.dvr, exist_ok=True)
        os.makedirs(self.thumbs, exist_ok=True)
        self.video = os.path.join(self.dvr, 'Race.mp4')
        with open(self.video, 'wb') as fh:
            fh.write(b'not really video')

    def tearDown(self):
        self.ctx.pop()
        self.t.cleanup()

    def _cfg(self, enabled=True):
        # images_dir points at the temp tree, never the real one: a runtime load_config()
        # would resolve the production /dvr and the test would write into it.
        return {'recording': {'metadata_sidecar': {'enabled': enabled},
                              'images_dir': os.path.join(self.t._tmpdir, 'images')}}

    def _thumbnail(self):
        path = os.path.join(self.thumbs, f'{self.rec.id}.jpg')
        with open(path, 'wb') as fh:
            fh.write(b'jpegbytes')
        return path

    def _events(self, event_type):
        return RecordingEvent.query.filter_by(
            recording_id=self.rec.id, event_type=event_type).all()

    def test_nothing_is_written_when_the_feature_is_off(self):
        self.assertFalse(write_sidecar(self.rec.id, self.video, self._cfg(False)))
        self.assertEqual(os.listdir(self.dvr), ['Race.mp4'])
        self.assertEqual(self._events(METADATA_SIDECAR_WRITTEN), [])

    def test_the_nfo_lands_beside_the_video(self):
        self.assertTrue(write_sidecar(self.rec.id, self.video, self._cfg()))
        nfo = os.path.join(self.dvr, 'Race' + NFO_SUFFIX)
        self.assertTrue(os.path.exists(nfo))
        with open(nfo) as fh:
            self.assertEqual(ET.fromstring(fh.read()).findtext('plot'),
                             'Forty cars, one rain delay.')

    def test_the_poster_is_copied_from_the_recordings_thumbnail(self):
        self._thumbnail()
        write_sidecar(self.rec.id, self.video, self._cfg())
        poster = os.path.join(self.dvr, 'Race' + POSTER_SUFFIX)
        self.assertTrue(os.path.exists(poster))
        with open(poster, 'rb') as fh:
            self.assertEqual(fh.read(), b'jpegbytes')

    def test_the_nfo_points_at_the_poster_only_when_one_was_copied(self):
        write_sidecar(self.rec.id, self.video, self._cfg())
        with open(os.path.join(self.dvr, 'Race' + NFO_SUFFIX)) as fh:
            self.assertIsNone(ET.fromstring(fh.read()).find('thumb'))

        self._thumbnail()
        write_sidecar(self.rec.id, self.video, self._cfg())
        with open(os.path.join(self.dvr, 'Race' + NFO_SUFFIX)) as fh:
            self.assertEqual(ET.fromstring(fh.read()).findtext('thumb'),
                             'Race' + POSTER_SUFFIX)

    def test_a_missing_thumbnail_is_not_a_failure(self):
        """The live-thumbnail feature can be off, and a recording whose segments were all
        unreadable never got one. Neither is a reason to withhold the synopsis."""
        self.assertTrue(write_sidecar(self.rec.id, self.video, self._cfg()))
        self.assertEqual(len(self._events(METADATA_SIDECAR_WRITTEN)), 1)
        self.assertEqual(self._events(METADATA_SIDECAR_FAILED), [])

    def test_a_write_names_what_it_wrote_on_the_recordings_timeline(self):
        write_sidecar(self.rec.id, self.video, self._cfg())
        events = self._events(METADATA_SIDECAR_WRITTEN)
        self.assertEqual(len(events), 1)
        self.assertIn('Race.nfo', events[0].detail)

    def test_a_failed_write_is_an_event_and_never_an_exception(self):
        """The recording is complete and on disk by now. A file that only DESCRIBES the
        artifact may never be allowed to damage it or to fail its post-processing."""
        with patch('app.metadata_sidecar.open', side_effect=OSError('read-only file system')):
            self.assertFalse(write_sidecar(self.rec.id, self.video, self._cfg()))
        events = self._events(METADATA_SIDECAR_FAILED)
        self.assertEqual(len(events), 1)
        self.assertIn('read-only file system', events[0].detail)
        self.assertEqual(self._events(METADATA_SIDECAR_WRITTEN), [])

    def test_a_profiles_override_decides_instead_of_the_global(self):
        profile = RecordingProfile(name='No sidecars', metadata_sidecar_enabled=False)
        db.session.add(profile)
        db.session.flush()
        self.rec.profile_id = profile.id
        db.session.commit()
        self.assertFalse(write_sidecar(self.rec.id, self.video, self._cfg(True)))
        self.assertEqual(os.listdir(self.dvr), ['Race.mp4'])


class TeardownTests(unittest.TestCase):
    """Deleting a recording takes its sidecars with it."""

    def setUp(self):
        self.t = make_test_app()
        self.ctx = self.t.app.app_context()
        self.ctx.push()

    def tearDown(self):
        self.ctx.pop()
        self.t.cleanup()

    def test_both_sidecars_are_listed_among_a_recordings_files(self):
        """Without this they are orphaned on every delete: nothing else on disk or in the
        database remembers that a recording owned them."""
        from app.recorder import recording_disk_paths

        rec = seed.make_recording(status='COMPLETED', output_path='/dvr/Race.mp4')
        db.session.commit()
        paths = recording_disk_paths(rec.id, {'recording': {
            'images_dir': '/tmp-images',
            'post_process': {'enabled': True, 'format': 'mp4'}}})
        self.assertIn('/dvr/Race.nfo', paths)
        self.assertIn('/dvr/Race-poster.jpg', paths)

    def test_a_keep_the_files_delete_leaves_them_beside_the_video(self):
        """The keep-files dialog promises only what ChannelBin holds is removed. These
        describe the video the user is choosing to keep, so removing them would strip that
        library item of its title and synopsis as a side effect."""
        from app.recorder import recording_image_paths

        rec = seed.make_recording(status='COMPLETED', output_path='/dvr/Race.mp4')
        db.session.commit()
        paths = recording_image_paths(rec.id, {'recording': {'images_dir': '/tmp-images'}})
        self.assertNotIn('/dvr/Race.nfo', paths)
        self.assertNotIn('/dvr/Race-poster.jpg', paths)


if __name__ == '__main__':
    unittest.main()
