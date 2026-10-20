"""The toolchain payload carries the words, so the two surfaces that describe a binary's
provenance cannot drift (dev/docs/BUGS.md 2026-10-20, dev/changelog/1244).

readiness._TOOL_SOURCE and maintenance.js::toolSource each mapped the same `source` key to
prose and had already disagreed on `configured` ("set in Settings" against "set in
ffmpeg.path"). Now describe_tools() ships `source_label`, the readiness row prints it, and
the Maintenance card prints it. The "what breaks without it" sentence had the same two
homes - toolchain's missing-tool alert and the readiness `without` - and is now one.

  python3 -m unittest tests.test_tool_source_label
"""
import os
import unittest
from unittest.mock import patch

from app import readiness, toolchain
from tests.test_toolchain import _fake_probe

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _read(rel):
    with open(os.path.join(ROOT, rel), encoding='utf-8') as fh:
        return fh.read()


class SourceLabelTests(unittest.TestCase):
    def tearDown(self):
        toolchain.reset_cache()

    def test_the_payload_names_the_provenance_in_words(self):
        with patch.object(toolchain, '_probe_version', _fake_probe()):
            tools = toolchain.describe_tools_uncached('ffmpeg')
        self.assertEqual(tools['ffmpeg']['source_label'], 'found on PATH')

    def test_a_configured_binary_names_its_own_setting(self):
        self.assertEqual(toolchain.source_label(toolchain.SOURCE_CONFIGURED, 'ffmpeg.ffprobe_path'),
                         'set in ffmpeg.ffprobe_path')
        self.assertEqual(toolchain.source_label(toolchain.SOURCE_SIBLING, 'ffmpeg.ffprobe_path'),
                         'found beside the configured ffmpeg')

    def test_a_missing_tool_carries_no_label(self):
        with patch.object(toolchain, '_probe_version', _fake_probe(found=())):
            tools = toolchain.describe_tools_uncached('ffmpeg')
        self.assertIsNone(tools['ffprobe']['source_label'])

    def test_the_readiness_row_prints_the_payload_words(self):
        info = {'found': True, 'path': '/opt/bin/ffprobe', 'version': '7.1',
                'source': 'configured', 'source_label': 'set in ffmpeg.ffprobe_path'}
        self.assertEqual(readiness.CHECKS_BY_ID['ffprobe'].run(
            type('Ctx', (), {'tools': {'ffprobe': info}})()).found,
            'Version 7.1, set in ffmpeg.ffprobe_path')

    def test_neither_renderer_keeps_a_map_of_its_own(self):
        self.assertNotIn('_TOOL_SOURCE', _read('app/readiness.py'))
        self.assertNotIn("'found on PATH'", _read('static/js/maintenance.js'))
        self.assertIn('source_label', _read('static/js/maintenance.js'))


class ConsequenceTests(unittest.TestCase):
    def test_the_readiness_rows_read_the_alerts_sentence(self):
        for name in ('ffmpeg', 'ffprobe'):
            self.assertEqual(readiness.CHECKS_BY_ID[name].without, toolchain.CONSEQUENCE[name])


if __name__ == '__main__':
    unittest.main(verbosity=2)
