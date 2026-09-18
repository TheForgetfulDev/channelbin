"""Thumbnails, health check screenshots and cached logos share one `recording.images_dir`,
each kind in its own subfolder (dev/changelog/1012).

Before it there were three folder keys, and Settings showed only the screenshot one, so an
install that pointed screenshots somewhere writable still had its recording thumbnails
failing against the `/dvr/live_thumbnails` default it had no field to change. The config
migration carries a folder the user actually chose over to the new key, and every reader
goes through `app/storage_dirs.py::image_dir`.
"""
import os
import shutil
import tempfile
import unittest

from app import config as cfgmod
from app import db
from app.database import REC_STATUS_COMPLETED
from tests.support import make_test_app, seed
from tests.support.config_sandbox import ConfigSandbox


class ImagesDirMigrationTests(unittest.TestCase):

    def migrate(self, cfg):
        from app.config import _cfg_m006_one_images_dir
        with self.assertLogs('app.config', level='WARNING') as logs:
            out = _cfg_m006_one_images_dir(cfg)
        return out, '\n'.join(logs.output)

    def test_a_chosen_screenshot_folder_becomes_images_dir(self):
        """The reported install: screenshots pointed at a mapped folder, thumbnails left on
        the default nobody could change in Settings."""
        out, log = self.migrate({
            'recording': {'live_thumbnail': {'enabled': True, 'dir': '/dvr/live_thumbnails'}},
            'channel_testing': {'screenshot_dir': '/screenshots'}})
        self.assertEqual(out['recording']['images_dir'], '/screenshots')
        self.assertNotIn('dir', out['recording']['live_thumbnail'])
        self.assertNotIn('channel_testing', out,
                         'a section left empty by the removal must go with it')
        self.assertIn('carried over from channel_testing.screenshot_dir', log)
        self.assertIn('/dvr/live_thumbnails', log, 'the warning must name where old files are')

    def test_the_screenshot_folder_wins_over_the_thumbnail_folder(self):
        out, _ = self.migrate({
            'recording': {'live_thumbnail': {'dir': '/thumbs'}},
            'channel_testing': {'screenshot_dir': '/shots'}})
        self.assertEqual(out['recording']['images_dir'], '/shots')

    def test_a_chosen_thumbnail_folder_carries_when_screenshots_were_default(self):
        out, _ = self.migrate({
            'recording': {'live_thumbnail': {'dir': '/thumbs'}},
            'channel_testing': {'screenshot_dir': '/dvr/channel_test_screenshots'}})
        self.assertEqual(out['recording']['images_dir'], '/thumbs')

    def test_old_defaults_are_not_a_choice(self):
        """A config.yaml that wrote the shipped defaults out - this dev box's, and every
        container's seeded logo folder - must land on the new default, not inside one of
        the old folders."""
        out, log = self.migrate({
            'recording': {'live_thumbnail': {'dir': '/dvr/live_thumbnails'},
                          'logo_cache': {'enabled': False,
                                         'dir': '/config/instance/logo-cache'}},
            'channel_testing': {'screenshot_dir': '/dvr/channel_test_screenshots'}})
        self.assertNotIn('images_dir', out['recording'])
        self.assertNotIn('dir', out['recording']['logo_cache'],
                         'the enabled switch stays, so the section does too')
        self.assertNotIn('carried over', log)

    def test_an_images_dir_already_set_is_kept(self):
        out, _ = self.migrate({'recording': {'images_dir': '/mine'},
                               'channel_testing': {'screenshot_dir': '/shots'}})
        self.assertEqual(out['recording']['images_dir'], '/mine')

    def test_a_config_with_none_of_the_old_keys_is_untouched(self):
        from app.config import _cfg_m006_one_images_dir
        cfg = {'recording': {'dvr_output_dir': '/dvr'}}
        self.assertEqual(_cfg_m006_one_images_dir(cfg), {'recording': {'dvr_output_dir': '/dvr'}})

    def test_it_is_registered_so_it_actually_runs(self):
        from app.config import CONFIG_MIGRATIONS, _cfg_m006_one_images_dir
        self.assertIn(_cfg_m006_one_images_dir, [fn for _, _, fn in CONFIG_MIGRATIONS])


class ContainerConfigStaysReadableTests(ConfigSandbox):
    """The migration driven through migrate_config() against a real commented file.

    dev/docs/BUGS.md 2026-09-17: the per-function tests above all pass on plain dicts,
    which carry no comments, so none of them could see that the same transform wrote YAML
    ruamel cannot read back when the keys it removes have comments attached - which is
    every container, because the image seeds a commented config.yaml.
    """

    SEED = (
        'config_version: 5\n'
        '\n'
        'recording:\n'
        '  # The recordings volume.\n'
        '  dvr_output_dir: /dvr\n'
        '  logo_cache:\n'
        '    # On the /config volume, so cached logos survive an image upgrade.\n'
        '    dir: /config/instance/logo-cache\n'
        '\n'
        'channel_testing:\n'
        '  screenshot_dir: /screenshots\n'
        '\n'
        'database:\n'
        '  # On the /config volume.\n'
        '  path: /config/dvr.db\n'
    )

    def setUp(self):
        super().setUp()
        with open(self._cfg_path, 'w') as f:
            f.write(self.SEED)
        cfgmod._yaml_cache = None
        self.backup_dir = tempfile.mkdtemp(prefix='cb-cfgbackup-')
        self.addCleanup(shutil.rmtree, self.backup_dir, ignore_errors=True)

    def test_the_migrated_file_still_parses_and_keeps_what_it_should(self):
        cfgmod.migrate_config(config_overrides={'config_backup': {'backup_dir': self.backup_dir}})
        cfgmod._yaml_cache = None
        written = cfgmod._load_config_file()  # raises if the file no longer parses
        self.assertEqual(written['recording']['images_dir'], '/screenshots')
        self.assertEqual(written['recording']['dvr_output_dir'], '/dvr')
        self.assertEqual(written['database']['path'], '/config/dvr.db')
        self.assertEqual(written['config_version'], cfgmod.CURRENT_CONFIG_VERSION)
        with open(self._cfg_path) as f:  # direct-config-read: asserting on stored bytes
            text = f.read()
        self.assertIn('# The recordings volume.', text,
                      'a surviving key lost the comment above it')
        self.assertNotIn('cached logos survive', text,
                         'the comment outlived the key it described')


class ImageDirTests(unittest.TestCase):

    def test_each_kind_is_a_subfolder_of_images_dir(self):
        from app.storage_dirs import LOGOS, SCREENSHOTS, THUMBNAILS, image_dir
        cfg = {'recording': {'images_dir': '/img'}}
        self.assertEqual(image_dir(cfg, THUMBNAILS), '/img/thumbnails')
        self.assertEqual(image_dir(cfg, SCREENSHOTS), '/img/screenshots')
        self.assertEqual(image_dir(cfg, LOGOS), '/img/logos')

    def test_a_relative_images_dir_is_anchored_to_the_app_root(self):
        from app.config import resolve_app_path
        from app.storage_dirs import THUMBNAILS, image_dir
        self.assertEqual(image_dir({'recording': {'images_dir': 'pics'}}, THUMBNAILS),
                         os.path.join(resolve_app_path('pics'), 'thumbnails'))

    def test_the_old_per_kind_keys_are_gone_from_the_defaults(self):
        from app.config import _DEFAULTS
        self.assertNotIn('dir', _DEFAULTS['recording']['live_thumbnail'])
        self.assertNotIn('dir', _DEFAULTS['recording']['logo_cache'])
        self.assertNotIn('screenshot_dir', _DEFAULTS['channel_testing'])


class ImagesAreServedFromImagesDirTests(unittest.TestCase):
    """The readers, not just the helper: a changed images_dir is what gets served."""

    def setUp(self):
        self.t = make_test_app()
        self.t.app.config['WTF_CSRF_ENABLED'] = False
        self.images = os.path.join(self.t._tmpdir, 'chosen-images')
        self.t.sandbox_config({'recording': {'images_dir': self.images}})

    def tearDown(self):
        self.t.cleanup()

    def _write(self, kind, name, data=b'\xff\xd8jpeg'):
        folder = os.path.join(self.images, kind)
        os.makedirs(folder, exist_ok=True)
        with open(os.path.join(folder, name), 'wb') as f:
            f.write(data)

    def test_a_health_check_screenshot_is_served_from_the_screenshots_subfolder(self):
        self._write('screenshots', 'ch_1_20260917_120000.jpg', b'shot')
        resp = self.t.client.get('/channel-tests/screenshots/ch_1_20260917_120000.jpg')
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.data, b'shot')

    def test_a_finished_recordings_thumbnail_is_served_from_the_thumbnails_subfolder(self):
        with self.t.app.app_context():
            acc = seed.make_account()
            ch = seed.make_channel(acc, stream_id=1, name='Ch')
            rec = seed.make_recording(status=REC_STATUS_COMPLETED, channel_id=ch.id)
            db.session.commit()
            rid = rec.id
        self._write('thumbnails', f'{rid}.jpg', b'thumb')
        resp = self.t.client.get(f'/recordings/{rid}/live-thumbnail.jpg')
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.data, b'thumb')

    def test_storage_details_reports_the_images_folder(self):
        self._write('logos', '1.png', b'12345')
        self._write('thumbnails', '2.jpg', b'123')
        data = self.t.client.get('/api/system/storage-details').get_json()
        self.assertEqual(data['images_dir'], self.images)
        self.assertEqual(data['images_bytes'], 8)


if __name__ == '__main__':
    unittest.main()
