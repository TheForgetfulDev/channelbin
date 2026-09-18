"""The Home Assistant integration is installed through HACS, which reads the repository as it
is published: one integration under custom_components/, a manifest carrying the keys HACS
requires, and brand images inside the integration directory (dev/changelog/1019)."""
import json
import os
import re
import struct
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
COMPONENTS = os.path.join(ROOT, 'custom_components')
INTEGRATION = os.path.join(COMPONENTS, 'channelbin')

HACS_REQUIRED_MANIFEST_KEYS = ('domain', 'documentation', 'issue_tracker', 'codeowners',
                               'name', 'version')


def _read(path, mode='r'):
    with open(path, mode, **({} if 'b' in mode else {'encoding': 'utf-8'})) as fh:
        return fh.read()


def _png_size(path):
    data = _read(path, 'rb')
    if data[:8] != b'\x89PNG\r\n\x1a\n':
        raise AssertionError(f'{path} is not a PNG')
    return struct.unpack('>II', data[16:24])


class HacsPackagingTests(unittest.TestCase):

    def setUp(self):
        self.manifest = json.loads(_read(os.path.join(INTEGRATION, 'manifest.json')))

    def test_exactly_one_integration_under_custom_components(self):
        """HACS refuses a repository with more than one integration directory."""
        dirs = [d for d in os.listdir(COMPONENTS)
                if os.path.isdir(os.path.join(COMPONENTS, d)) and d != '__pycache__']
        self.assertEqual(dirs, ['channelbin'])

    def test_manifest_carries_every_key_hacs_requires(self):
        for key in HACS_REQUIRED_MANIFEST_KEYS:
            self.assertTrue(self.manifest.get(key), f'manifest.json has no {key!r}')
        self.assertEqual(self.manifest['domain'], 'channelbin')

    def test_manifest_version_tracks_the_app_version(self):
        """HACS and Home Assistant show the manifest version as the installed version. It sat
        at 0.1.0 through ten releases, so it is pinned to app/version.py and bumped with it."""
        version = re.search(r"^__version__\s*=\s*'([^']+)'",
                            _read(os.path.join(ROOT, 'app', 'version.py')), re.M).group(1)
        self.assertEqual(self.manifest['version'], version,
                         "custom_components/channelbin/manifest.json's version does not match "
                         "app/version.py - bump both at release")

    def test_brand_icons_are_square_pngs_at_the_expected_sizes(self):
        """Home Assistant 2026.3+ reads a custom integration's icon from its own brand/ dir,
        and HACS requires at least icon.png."""
        for name, size in (('icon.png', 256), ('icon@2x.png', 512)):
            path = os.path.join(INTEGRATION, 'brand', name)
            self.assertTrue(os.path.isfile(path), f'missing brand/{name}')
            self.assertEqual(_png_size(path), (size, size), f'brand/{name}')


if __name__ == '__main__':
    unittest.main()
