"""The Home Assistant integration is installed through HACS, which reads the repository as it
is published: one integration under custom_components/, a manifest carrying the keys HACS
requires, and brand images inside the integration directory (dev/changelog/1019).

The integration also declares the oldest ChannelBin it runs against and refuses a server older
than that (dev/changelog/1037). Home Assistant is not installed here, so compat.py is loaded
from its path rather than through the package, whose __init__ imports homeassistant."""
import importlib.util
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


def _app_version():
    return re.search(r"^__version__\s*=\s*'([^']+)'",
                     _read(os.path.join(ROOT, 'app', 'version.py')), re.M).group(1)


def _load_compat():
    spec = importlib.util.spec_from_file_location(
        'channelbin_compat', os.path.join(INTEGRATION, 'compat.py'))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


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
        self.assertEqual(self.manifest['version'], _app_version(),
                         "custom_components/channelbin/manifest.json's version does not match "
                         "app/version.py - bump both at release")

    def test_brand_icons_are_square_pngs_at_the_expected_sizes(self):
        """Home Assistant 2026.3+ reads a custom integration's icon from its own brand/ dir,
        and HACS requires at least icon.png."""
        for name, size in (('icon.png', 256), ('icon@2x.png', 512)):
            path = os.path.join(INTEGRATION, 'brand', name)
            self.assertTrue(os.path.isfile(path), f'missing brand/{name}')
            self.assertEqual(_png_size(path), (size, size), f'brand/{name}')


class MinimumServerVersionTests(unittest.TestCase):
    """The integration's declared minimum ChannelBin, and how it reads a server's version."""

    def setUp(self):
        self.compat = _load_compat()

    def test_declared_minimum_is_never_newer_than_the_app_being_released(self):
        """An integration that demands a newer ChannelBin than the one it ships with would
        refuse the server released alongside it."""
        minimum = self.compat.parse_version(self.compat.MIN_CHANNELBIN_VERSION)
        self.assertIsNotNone(minimum, 'MIN_CHANNELBIN_VERSION does not parse')
        self.assertLessEqual(minimum, self.compat.parse_version(_app_version()),
                             f'MIN_CHANNELBIN_VERSION {self.compat.MIN_CHANNELBIN_VERSION} is '
                             f'newer than app/version.py {_app_version()}')

    def test_the_app_version_itself_is_accepted(self):
        self.assertEqual(self.compat.server_too_old({'app_version': _app_version()}),
                         (False, _app_version()))

    def test_an_older_server_is_refused_and_named(self):
        self.compat.MIN_CHANNELBIN_VERSION = '0.12.0'
        self.assertEqual(self.compat.server_too_old({'app_version': '0.11.9'}),
                         (True, '0.11.9'))

    def test_versions_compare_as_numbers_not_text(self):
        """As strings, '0.9.0' sorts after '0.12.0'."""
        self.compat.MIN_CHANNELBIN_VERSION = '0.12.0'
        self.assertTrue(self.compat.server_too_old({'app_version': '0.9.0'})[0])
        self.assertFalse(self.compat.server_too_old({'app_version': '0.13.0'})[0])
        self.assertFalse(self.compat.server_too_old({'app_version': '1.0.0'})[0])

    def test_a_suffix_does_not_break_the_comparison(self):
        self.compat.MIN_CHANNELBIN_VERSION = '0.12.0'
        self.assertFalse(self.compat.server_too_old({'app_version': '0.12.0-dev'})[0])

    def test_a_server_that_reports_no_version_is_too_old(self):
        """Every server before the check sends no app_version. That must read as too old,
        never raise."""
        self.assertEqual(self.compat.server_too_old({'recording': {}}), (True, None))

    def test_an_unreadable_version_is_too_old(self):
        for bad in ('', 'banana', 12, None, ['0.12.0']):
            with self.subTest(bad=bad):
                self.assertTrue(self.compat.server_too_old({'app_version': bad})[0])

    def test_a_payload_that_is_not_an_object_is_too_old(self):
        for bad in (None, [], 'ok'):
            with self.subTest(bad=bad):
                self.assertEqual(self.compat.server_too_old(bad), (True, None))


class MinimumServerVersionStringsTests(unittest.TestCase):
    """The repair issue and the config-flow error keys the coordinator and config flow use
    must exist, with the placeholders they fill, in both string files - a missing key shows
    the user a raw key name instead of the reason."""

    KEYS = ('server_too_old', 'server_version_unknown')

    def test_every_key_is_in_both_string_files(self):
        const = _read(os.path.join(INTEGRATION, 'const.py'))
        for key in self.KEYS:
            self.assertIn(f'"{key}"', const)
        for rel in ('strings.json', os.path.join('translations', 'en.json')):
            strings = json.loads(_read(os.path.join(INTEGRATION, rel)))
            for key in self.KEYS:
                with self.subTest(file=rel, key=key):
                    error = strings['config']['error'][key]
                    issue = strings['issues'][key]
                    self.assertTrue(issue['title'])
                    for text in (error, issue['description']):
                        self.assertIn('{minimum}', text)
                        self.assertIn('{host}', text)
                    self.assertEqual('{reported}' in issue['description'],
                                     key == 'server_too_old')


if __name__ == '__main__':
    unittest.main()
