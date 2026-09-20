"""The poster image a Recording Profile pins, and the upload that puts it there.

Guards dev/changelog/1059: the first file upload this app takes. A profile can carry one
JPG or PNG, stored under a server-chosen name in images_dir's posters subfolder, and every
recording made under that profile gets it as the sidecar's artwork instead of a captured
frame. The file is recognized by its bytes, capped in size, never resized, and its stored
name is the only thing the serving and deleting paths will accept. A recording's teardown
never touches the profile's file; deleting the profile does.

Runs against a throwaway temp SQLite DB and a temp images folder - never the live dvr.db or
the real images_dir, which a runtime load_config() would otherwise resolve to.
"""
import copy
import io
import json
import os
import shutil
import struct
import subprocess
import sys
import unittest
import xml.etree.ElementTree as ET
import zlib
from datetime import datetime
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import db  # noqa: E402
from app.config import load_config  # noqa: E402
from app.database import RecordingEvent, RecordingProfile, METADATA_SIDECAR_WRITTEN  # noqa: E402
from app.metadata_sidecar import (  # noqa: E402
    NFO_SUFFIX, POSTER_SUFFIX, POSTER_FROM_PROFILE, POSTER_FROM_THUMBNAIL, write_sidecar,
)
from app.profile_posters import (  # noqa: E402
    FILENAME_RE, MAX_BYTES, MAX_REQUEST_BYTES, POSTER_HEIGHT, POSTER_WIDTH,
    discard_poster_file, poster_path, size_advice, sniff_image, store_poster,
)
from tests.support import make_test_app, seed  # noqa: E402

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def png_bytes(width, height):
    """A structurally valid PNG of the given size: signature, IHDR, one IDAT, IEND."""
    def chunk(kind, body):
        return (struct.pack('>I', len(body)) + kind + body
                + struct.pack('>I', zlib.crc32(kind + body) & 0xFFFFFFFF))
    ihdr = struct.pack('>IIBBBBB', width, height, 8, 2, 0, 0, 0)
    raw = b''.join(b'\x00' + b'\x00\x00\x00' * width for _ in range(height))
    return (b'\x89PNG\r\n\x1a\n' + chunk(b'IHDR', ihdr)
            + chunk(b'IDAT', zlib.compress(raw)) + chunk(b'IEND', b''))


def jpeg_bytes(width, height, app1_padding=0, sof=0xC0):
    """A JPEG header carrying the size in an SOF marker, optionally behind a large APP1
    segment the way a camera's EXIF block sits. The scan data is absent - the sniffer
    reads headers only, and that is what these cases pin."""
    out = b'\xff\xd8'
    out += b'\xff\xe0' + struct.pack('>H', 16) + b'JFIF\x00\x01\x01\x00\x00\x01\x00\x01\x00\x00'
    if app1_padding:
        body = b'Exif\x00\x00' + b'\x00' * app1_padding
        out += b'\xff\xe1' + struct.pack('>H', len(body) + 2) + body
    frame = struct.pack('>BHHB', 8, height, width, 3) + b'\x01\x22\x00\x02\x11\x01\x03\x11\x01'
    out += bytes([0xFF, sof]) + struct.pack('>H', len(frame) + 2) + frame
    return out + b'\xff\xd9'


class SniffTests(unittest.TestCase):
    """The file is what its bytes say it is - never what its name or the browser claims."""

    def test_png_size_comes_out_of_ihdr(self):
        self.assertEqual(sniff_image(png_bytes(1000, 1500)), ('png', 1000, 1500))

    def test_jpeg_size_comes_out_of_the_frame_header(self):
        self.assertEqual(sniff_image(jpeg_bytes(640, 480)), ('jpeg', 640, 480))

    def test_a_large_exif_block_before_the_frame_header_is_walked_past(self):
        """The size can sit behind tens of kilobytes of APPn segments; a fixed offset
        would read garbage as a size."""
        self.assertEqual(sniff_image(jpeg_bytes(1000, 1500, app1_padding=60000)),
                         ('jpeg', 1000, 1500))

    def test_progressive_jpeg_is_recognized(self):
        self.assertEqual(sniff_image(jpeg_bytes(300, 450, sof=0xC2)), ('jpeg', 300, 450))

    def test_not_an_image_is_none(self):
        for data in (b'', b'GIF89a' + b'\x00' * 40, b'<html>' * 10, b'\x89PNG\r\n\x1a\n',
                     b'\xff\xd8\xff\xd9', jpeg_bytes(1, 1)[:6]):
            self.assertIsNone(sniff_image(data), data[:12])

    def test_a_zero_dimension_is_none(self):
        self.assertIsNone(sniff_image(png_bytes(0, 10)))
        self.assertIsNone(sniff_image(jpeg_bytes(10, 0)))

    def test_a_jpeg_whose_scan_starts_before_any_frame_header_is_none(self):
        data = b'\xff\xd8' + b'\xff\xda' + struct.pack('>H', 4) + b'\x00\x00' + b'\xff\xd9'
        self.assertIsNone(sniff_image(data))


class SizeAdviceTests(unittest.TestCase):
    """The form states the size; the response says how the file differs, and only then."""

    def test_the_expected_size_gets_no_advice(self):
        self.assertIsNone(size_advice(POSTER_WIDTH, POSTER_HEIGHT))

    def test_the_right_shape_at_another_size_says_scaled(self):
        text = size_advice(500, 750)
        self.assertIn('500 x 750', text)
        self.assertIn('smaller', text)
        self.assertIn('scaled', text)

    def test_the_wrong_shape_says_cropped(self):
        text = size_advice(1920, 1080)
        self.assertIn('1920 x 1080', text)
        self.assertIn('cropped', text)
        self.assertIn(f'{POSTER_WIDTH} x {POSTER_HEIGHT}', text)


class StorageTests(unittest.TestCase):
    """Server-chosen names, atomic writes, and a delete that refuses to guess."""

    def setUp(self):
        self.t = make_test_app()
        self.cfg = {'recording': {'images_dir': os.path.join(self.t._tmpdir, 'images')}}
        self.folder = os.path.join(self.t._tmpdir, 'images', 'posters')

    def tearDown(self):
        self.t.cleanup()

    def test_the_stored_name_is_ours_and_the_temp_file_is_gone(self):
        info = sniff_image(png_bytes(2, 3))
        name = store_poster(self.cfg, 7, png_bytes(2, 3), info)
        self.assertRegex(name, FILENAME_RE)
        self.assertTrue(name.startswith('profile-7-'))
        self.assertTrue(name.endswith('.png'))
        self.assertEqual(os.listdir(self.folder), [name])

    def test_the_extension_follows_the_bytes_not_the_upload(self):
        info = sniff_image(jpeg_bytes(2, 3))
        self.assertTrue(store_poster(self.cfg, 1, jpeg_bytes(2, 3), info).endswith('.jpg'))

    def test_a_second_store_never_overwrites_the_first_in_place(self):
        """The previous file must stay intact until the row points at the new one."""
        info = sniff_image(png_bytes(2, 3))
        first = store_poster(self.cfg, 1, png_bytes(2, 3), info)
        second = store_poster(self.cfg, 1, png_bytes(2, 3), info)
        self.assertNotEqual(first, second)
        self.assertEqual(sorted(os.listdir(self.folder)), sorted([first, second]))

    def test_poster_path_refuses_a_name_that_is_not_ours(self):
        for bad in ('../../etc/passwd', 'profile-1.png', 'profile-1-abc.png', 'x/y.jpg', ''):
            with self.assertRaises(ValueError, msg=bad):
                poster_path(self.cfg, bad)

    def test_discard_removes_ours_and_leaves_anything_else_alone(self):
        os.makedirs(self.folder)
        stray = os.path.join(self.folder, 'stray.txt')
        with open(stray, 'w') as fh:
            fh.write('not ours')
        name = store_poster(self.cfg, 1, png_bytes(2, 3), sniff_image(png_bytes(2, 3)))
        discard_poster_file(self.cfg, 'stray.txt')
        discard_poster_file(self.cfg, name)
        discard_poster_file(self.cfg, name)   # already gone: not an error
        discard_poster_file(self.cfg, None)
        self.assertEqual(os.listdir(self.folder), ['stray.txt'])


class _RouteBase(unittest.TestCase):
    def setUp(self):
        self.t = make_test_app()
        self.t.app.config['WTF_CSRF_ENABLED'] = False
        self.client = self.t.client
        self.ctx = self.t.app.app_context()
        self.ctx.push()
        self.images = os.path.join(self.t._tmpdir, 'images')
        self.folder = os.path.join(self.images, 'posters')
        # The routes read config at request time, which would resolve the REAL images_dir;
        # a copy of the merged config keeps every other key intact for the page render.
        cfg = copy.deepcopy(load_config())
        cfg['recording']['images_dir'] = self.images
        self._patch = patch('app.routes.profiles.load_config', return_value=cfg)
        self._patch.start()
        self.profile = RecordingProfile(name='NASCAR')
        db.session.add(self.profile)
        db.session.commit()

    def tearDown(self):
        self._patch.stop()
        self.ctx.pop()
        self.t.cleanup()

    def upload(self, data, filename='logo.png', profile_id=None):
        return self.client.post(
            f'/api/profiles/{profile_id or self.profile.id}/poster',
            data={'poster': (io.BytesIO(data), filename)},
            content_type='multipart/form-data')

    def stored(self):
        db.session.expire_all()
        return db.session.get(RecordingProfile, self.profile.id)

    def files(self):
        return os.listdir(self.folder) if os.path.isdir(self.folder) else []


class UploadRouteTests(_RouteBase):
    """POST /api/profiles/<id>/poster - every rule enforced on the server."""

    def test_a_png_is_stored_under_our_name_and_the_row_points_at_it(self):
        resp = self.upload(png_bytes(1000, 1500))
        self.assertEqual(resp.status_code, 200, resp.get_data(as_text=True))
        body = resp.get_json()
        self.assertTrue(body['success'])
        self.assertIsNone(body['advice'])
        p = self.stored()
        self.assertRegex(p.poster_file, FILENAME_RE)
        self.assertEqual((p.poster_width, p.poster_height), (1000, 1500))
        self.assertEqual(self.files(), [p.poster_file])
        poster = body['profile']['poster']
        self.assertEqual((poster['width'], poster['height'], poster['kind']), (1000, 1500, 'PNG'))
        self.assertTrue(poster['url'].startswith(f'/api/profiles/{p.id}/poster?v='))

    def test_the_uploaded_filename_is_never_used(self):
        """A JPEG called .png is stored as a .jpg, and a name that tries to walk out of
        the folder never reaches the filesystem at all."""
        resp = self.upload(jpeg_bytes(1000, 1500), filename='../../evil.png')
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(self.stored().poster_file.endswith('.jpg'))
        self.assertFalse(os.path.exists(os.path.join(self.t._tmpdir, 'evil.png')))
        self.assertEqual(len(self.files()), 1)

    def test_the_wrong_size_still_uploads_and_says_how_it_differs(self):
        resp = self.upload(png_bytes(1920, 1080))
        self.assertEqual(resp.status_code, 200)
        self.assertIn('1920 x 1080', resp.get_json()['advice'])
        self.assertIn('1920 x 1080', resp.get_json()['profile']['poster']['advice'])

    def test_not_an_image_is_refused_and_nothing_lands(self):
        resp = self.upload(b'<svg xmlns="http://www.w3.org/2000/svg"/>', filename='logo.png')
        self.assertEqual(resp.status_code, 400)
        self.assertIn('not a JPG or PNG', resp.get_json()['error'])
        self.assertIsNone(self.stored().poster_file)
        self.assertEqual(self.files(), [])

    def test_no_file_is_a_400(self):
        resp = self.client.post(f'/api/profiles/{self.profile.id}/poster',
                                data={}, content_type='multipart/form-data')
        self.assertEqual(resp.status_code, 400)

    def test_an_unknown_profile_is_a_404(self):
        self.assertEqual(self.upload(png_bytes(2, 3), profile_id=999).status_code, 404)
        self.assertEqual(self.files(), [])

    def test_an_oversize_image_is_a_json_413_and_nothing_lands(self):
        """Refused by the app-wide cap or the route's own bounded read - either way the
        answer is a JSON error the modal can show, not Werkzeug's HTML page."""
        big = png_bytes(2, 3) + b'\x00' * MAX_BYTES
        resp = self.upload(big)
        self.assertEqual(resp.status_code, 413)
        self.assertIn('error', resp.get_json())
        self.assertEqual(self.files(), [])
        self.assertIsNone(self.stored().poster_file)

    def test_the_cap_is_on_the_app_because_csrf_parses_the_form_first(self):
        self.assertEqual(self.t.app.config['MAX_CONTENT_LENGTH'], MAX_REQUEST_BYTES)

    def test_replacing_a_poster_discards_the_previous_file(self):
        self.upload(png_bytes(2, 3))
        first = self.stored().poster_file
        self.upload(jpeg_bytes(4, 6))
        second = self.stored().poster_file
        self.assertNotEqual(first, second)
        self.assertEqual(self.files(), [second])
        self.assertEqual((self.stored().poster_width, self.stored().poster_height), (4, 6))

    def test_the_json_save_does_not_touch_the_poster(self):
        """The poster is not a ProfileField: a PUT that carries no poster key, and even one
        that carries a forged poster_file, leaves the columns as the upload set them."""
        self.upload(png_bytes(2, 3))
        name = self.stored().poster_file
        resp = self.client.put(f'/api/profiles/{self.profile.id}',
                               json={'name': 'Renamed', 'poster_file': '../x.png'})
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(self.stored().name, 'Renamed')
        self.assertEqual(self.stored().poster_file, name)


class RemoveAndServeTests(_RouteBase):
    """DELETE and GET on the same URL."""

    def test_remove_clears_the_row_and_the_file(self):
        self.upload(png_bytes(2, 3))
        resp = self.client.delete(f'/api/profiles/{self.profile.id}/poster')
        self.assertEqual(resp.status_code, 200)
        self.assertIsNone(resp.get_json()['profile']['poster'])
        p = self.stored()
        self.assertIsNone(p.poster_file)
        self.assertIsNone(p.poster_width)
        self.assertEqual(self.files(), [])

    def test_removing_nothing_is_fine(self):
        self.assertEqual(self.client.delete(f'/api/profiles/{self.profile.id}/poster').status_code, 200)

    def test_the_poster_is_served_with_its_own_type_and_nosniff(self):
        data = jpeg_bytes(2, 3)
        self.upload(data)
        resp = self.client.get(f'/api/profiles/{self.profile.id}/poster')
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.mimetype, 'image/jpeg')
        self.assertEqual(resp.headers.get('X-Content-Type-Options'), 'nosniff')
        self.assertEqual(resp.data, data)

    def test_no_poster_is_a_404(self):
        self.assertEqual(self.client.get(f'/api/profiles/{self.profile.id}/poster').status_code, 404)
        self.assertEqual(self.client.get('/api/profiles/999/poster').status_code, 404)

    def test_a_row_naming_a_file_that_is_not_ours_is_a_404_not_a_read(self):
        """Defense in depth: the column is only ever written by the upload route, but a
        row that somehow names another path is refused before send_from_directory sees it."""
        secret = os.path.join(self.t._tmpdir, 'secret.txt')
        with open(secret, 'w') as fh:
            fh.write('do not serve')
        self.profile.poster_file = '../../secret.txt'
        db.session.commit()
        resp = self.client.get(f'/api/profiles/{self.profile.id}/poster')
        self.assertEqual(resp.status_code, 404)

    def test_deleting_the_profile_removes_its_poster_file(self):
        self.upload(png_bytes(2, 3))
        self.assertEqual(len(self.files()), 1)
        resp = self.client.delete(f'/api/profiles/{self.profile.id}')
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(self.files(), [])


class ProfilesPageTests(_RouteBase):
    """What the list shows, and what the modal is told."""

    def test_the_row_shows_the_posters_size(self):
        self.upload(png_bytes(1000, 1500))
        html = self.client.get('/profiles').get_data(as_text=True)
        self.assertIn('<span class="p-ov"><span class="p-ok">Poster</span>1000 x 1500</span>', html)

    def test_a_profile_without_a_poster_shows_no_poster_pill(self):
        html = self.client.get('/profiles').get_data(as_text=True)
        self.assertNotIn('<span class="p-ok">Poster</span>', html)

    def test_the_modal_is_told_the_size_and_the_cap_by_the_server(self):
        html = self.client.get('/profiles').get_data(as_text=True)
        spec = json.loads(html.split('posterSpec: ')[1].split(',\n')[0])
        self.assertEqual(spec, {'width': POSTER_WIDTH, 'height': POSTER_HEIGHT,
                                'maxMb': MAX_BYTES // (1024 * 1024)})


class SidecarPosterTests(unittest.TestCase):
    """The pinned image is what lands beside the recording, in its own format."""

    def setUp(self):
        self.t = make_test_app()
        self.ctx = self.t.app.app_context()
        self.ctx.push()
        self.images = os.path.join(self.t._tmpdir, 'images')
        self.cfg = {'recording': {'metadata_sidecar': {'enabled': True},
                                  'images_dir': self.images}}
        self.thumbs = os.path.join(self.images, 'thumbnails')
        self.dvr = os.path.join(self.t._tmpdir, 'dvr')
        os.makedirs(self.thumbs, exist_ok=True)
        os.makedirs(self.dvr, exist_ok=True)
        self.video = os.path.join(self.dvr, 'Race.mp4')
        with open(self.video, 'wb') as fh:
            fh.write(b'not really video')
        self.profile = RecordingProfile(name='NASCAR')
        db.session.add(self.profile)
        db.session.flush()
        account = seed.make_account()
        channel = seed.make_channel(account, name='Speed HD')
        self.rec = seed.make_recording(
            status='COMPLETED', channel_id=channel.id, profile_id=self.profile.id,
            program_title='NASCAR Cup Series', program_start_time=datetime(2026, 9, 20, 18, 30))
        db.session.commit()

    def tearDown(self):
        self.ctx.pop()
        self.t.cleanup()

    def _pin(self, data, kind):
        info = sniff_image(data)
        self.assertEqual(info.kind, kind)
        self.profile.poster_file = store_poster(self.cfg, self.profile.id, data, info)
        self.profile.poster_width, self.profile.poster_height = info.width, info.height
        db.session.commit()

    def _thumbnail(self):
        with open(os.path.join(self.thumbs, f'{self.rec.id}.jpg'), 'wb') as fh:
            fh.write(b'framebytes')

    def _event(self):
        events = RecordingEvent.query.filter_by(
            recording_id=self.rec.id, event_type=METADATA_SIDECAR_WRITTEN).all()
        self.assertEqual(len(events), 1)
        return events[0]

    def _thumb_text(self):
        with open(os.path.join(self.dvr, 'Race' + NFO_SUFFIX)) as fh:
            return ET.fromstring(fh.read()).findtext('thumb')

    def test_the_pinned_png_wins_over_the_thumbnail_and_keeps_its_format(self):
        self._thumbnail()
        self._pin(png_bytes(2, 3), 'png')
        self.assertTrue(write_sidecar(self.rec.id, self.video, self.cfg))
        poster = os.path.join(self.dvr, 'Race-poster.png')
        self.assertTrue(os.path.exists(poster))
        with open(poster, 'rb') as fh:
            self.assertEqual(fh.read(), png_bytes(2, 3))
        self.assertFalse(os.path.exists(os.path.join(self.dvr, 'Race' + POSTER_SUFFIX)))
        self.assertEqual(self._thumb_text(), 'Race-poster.png')
        event = self._event()
        self.assertIn('pinned to its profile', event.detail)
        self.assertEqual(json.loads(event.extra_data)['poster_source'], POSTER_FROM_PROFILE)

    def test_a_pinned_jpeg_lands_under_the_jpg_name(self):
        self._pin(jpeg_bytes(2, 3), 'jpeg')
        write_sidecar(self.rec.id, self.video, self.cfg)
        self.assertEqual(self._thumb_text(), 'Race' + POSTER_SUFFIX)

    def test_a_stale_poster_from_an_earlier_write_is_removed(self):
        """A -poster.jpg left beside a -poster.png lets a server that picks artwork by
        name choose between them by luck."""
        self._thumbnail()
        write_sidecar(self.rec.id, self.video, self.cfg)
        self.assertTrue(os.path.exists(os.path.join(self.dvr, 'Race' + POSTER_SUFFIX)))
        self._pin(png_bytes(2, 3), 'png')
        RecordingEvent.query.delete()
        db.session.commit()
        write_sidecar(self.rec.id, self.video, self.cfg)
        self.assertEqual(sorted(os.listdir(self.dvr)), ['Race-poster.png', 'Race.mp4', 'Race.nfo'])

    def test_a_pinned_file_gone_from_disk_falls_back_to_the_frame_and_says_so(self):
        self._thumbnail()
        self._pin(png_bytes(2, 3), 'png')
        shutil.rmtree(os.path.join(self.images, 'posters'))
        self.assertTrue(write_sidecar(self.rec.id, self.video, self.cfg))
        self.assertEqual(self._thumb_text(), 'Race' + POSTER_SUFFIX)
        event = self._event()
        self.assertIn('missing from disk', event.detail)
        self.assertIn('NASCAR', event.detail)
        self.assertEqual(json.loads(event.extra_data)['poster_source'], POSTER_FROM_THUMBNAIL)

    def test_a_pinned_file_gone_and_no_frame_says_both(self):
        self._pin(png_bytes(2, 3), 'png')
        shutil.rmtree(os.path.join(self.images, 'posters'))
        self.assertTrue(write_sidecar(self.rec.id, self.video, self.cfg))
        self.assertIsNone(self._thumb_text())
        detail = self._event().detail
        self.assertIn('No poster was available', detail)
        self.assertIn('missing from disk', detail)

    def test_no_pin_is_the_frame_as_before(self):
        self._thumbnail()
        write_sidecar(self.rec.id, self.video, self.cfg)
        self.assertEqual(self._thumb_text(), 'Race' + POSTER_SUFFIX)
        self.assertIn('final frame', self._event().detail)
        self.assertEqual(json.loads(self._event().extra_data)['poster_source'],
                         POSTER_FROM_THUMBNAIL)

    def test_a_recordings_teardown_lists_both_poster_names_and_never_the_profiles_file(self):
        """The copy beside the video is the recording's; the source is shared by every
        recording made under the profile and goes only when the profile does."""
        from app.recorder import recording_disk_paths, recording_image_paths

        self._pin(png_bytes(2, 3), 'png')
        pinned = poster_path(self.cfg, self.profile.poster_file)
        self.rec.output_path = self.video
        db.session.commit()
        cfg = dict(self.cfg)
        cfg['recording'] = dict(self.cfg['recording'],
                                post_process={'enabled': True, 'format': 'mp4'})
        paths = recording_disk_paths(self.rec.id, cfg)
        self.assertIn(os.path.join(self.dvr, 'Race-poster.jpg'), paths)
        self.assertIn(os.path.join(self.dvr, 'Race-poster.png'), paths)
        self.assertNotIn(pinned, paths)
        self.assertNotIn(pinned, recording_image_paths(self.rec.id, cfg))


class WriteDirsTests(unittest.TestCase):
    """The posters folder is judged like every other folder the app writes into."""

    def _dirs(self, enabled):
        from app.storage_dirs import configured_write_dirs
        cfg = {'recording': {'dvr_output_dir': '/rec', 'images_dir': '/img',
                             'metadata_sidecar': {'enabled': enabled}},
               'database': {'backup_dir': '/b/db'},
               'config_backup': {'backup_dir': '/b/cfg'}}
        return {path: role.what for path, role in configured_write_dirs(cfg)}

    def test_judged_when_the_sidecar_is_on(self):
        self.assertEqual(self._dirs(True).get('/img/posters'), 'Profile poster directory')

    def test_left_out_when_the_feature_is_off(self):
        self.assertNotIn('/img/posters', self._dirs(False))


@unittest.skipIf(shutil.which('node') is None, 'node not installed')
class ModalJsTests(unittest.TestCase):
    """The pure helpers behind the modal's poster section (static/js/profile-modal.js),
    evaluated in node the way tests/test_profile_modal_js.py does."""

    _EXPORTS = 'pmSizeAdvice, pmPosterSectionHtml'
    _HARNESS = f"""
const escHtml = (s) => String(s).replace(/[&<>"']/g, (c) => ({{'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}})[c]);
const fieldRow = (o) => `<div class="gd-field"><div class="gd-field-lbl">${{o.label}}</div><div class="gd-field-meta">${{o.meta}}</div><div class="gd-field-ctl">${{o.control}}</div></div>`;
const fs = require('fs');
const src = fs.readFileSync(process.argv[1], 'utf8');
const api = new Function('escHtml', 'fieldRow', src + '\\nreturn {{{_EXPORTS}}};')(escHtml, fieldRow);
const {{{_EXPORTS}}} = api;
console.log(JSON.stringify(eval(process.argv[2])));
"""
    SPEC = "{width:1000,height:1500,maxMb:10}"

    def evaluate(self, expr):
        proc = subprocess.run(
            ['node', '-e', self._HARNESS, os.path.join(REPO, 'static', 'js', 'profile-modal.js'), expr],
            capture_output=True, text=True, cwd=REPO, timeout=60)
        if proc.returncode != 0:
            self.fail(f'node failed evaluating `{expr}`:\n{proc.stderr}')
        return json.loads(proc.stdout)

    def test_size_advice_matches_the_server(self):
        self.assertIsNone(self.evaluate(f'pmSizeAdvice(1000, 1500, {self.SPEC})'))
        self.assertEqual(self.evaluate(f'pmSizeAdvice(500, 750, {self.SPEC})'),
                         size_advice(500, 750))
        self.assertEqual(self.evaluate(f'pmSizeAdvice(1920, 1080, {self.SPEC})'),
                         size_advice(1920, 1080))

    def test_the_form_states_the_size_and_the_cap(self):
        html = self.evaluate(f'pmPosterSectionHtml(null, {self.SPEC})')
        self.assertIn('1000 x 1500 pixels', html)
        self.assertIn('up to 10 MB', html)
        self.assertIn('nothing is resized', html)
        self.assertIn('type="file"', html)
        self.assertIn('accept="image/jpeg,image/png"', html)
        self.assertNotIn('data-poster-remove', html)

    def test_a_pinned_poster_is_shown_with_its_advice_and_a_remove_button(self):
        poster = "{url:'/api/profiles/3/poster?v=abc',width:800,height:600,kind:'PNG',advice:'This image is 800 x 600.'}"
        html = self.evaluate(f'pmPosterSectionHtml({poster}, {self.SPEC})')
        self.assertIn('/api/profiles/3/poster?v=abc', html)
        self.assertIn('PNG, 800 x 600', html)
        self.assertIn('This image is 800 x 600.', html)
        self.assertIn('data-poster-remove', html)


if __name__ == '__main__':
    unittest.main()
