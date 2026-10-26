"""Tier 0 - the key for the TV Guide's health dots at phone width (dev/changelog/1290).

At 960px and under the channel column's numeric health badge gives way to a corner dot that
carries the band's colour and nothing else (DESIGN.md 13.5). A first-time user on a phone held
sideways saw orange, grey and green dots beside channel names with nothing saying what the
colours meant. The status bar now names each band that is on a listed channel, in the declared
order. Runs the shipped util.js + guide.js against the markup the real /guide route rendered;
tests/support/guide_health_key.mjs drives it and reports.

What it cannot cover: jsdom computes no layout, so that the key shows at phone width only is
a CSS rule (static/css/guide.css) this does not check.
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tests.support.app import make_test_app  # noqa: E402
from tests.support import seed  # noqa: E402
from app.database import TEST_STATUS_FAILED  # noqa: E402

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HARNESS = os.path.join(REPO, 'tests', 'support', 'guide_health_key.mjs')
JSDOM = os.path.join(REPO, 'node_modules', 'jsdom')

CASES = {
    'mixed': [{'id': 1, 'health_score': 95}, {'id': 2, 'health_score': 30},
              {'id': 3, 'health_score': None}, {'id': 4, 'health_score': 96}],
    'one_band': [{'id': 1, 'health_score': 85}],
    'nothing': [],
    # A failed channel the guide is hiding (Include failed channels is off by default)
    # does not put its band in the key.
    'hidden_failed': [{'id': 1, 'health_score': 85},
                      {'id': 2, 'health_score': 10, 'last_test_status': TEST_STATUS_FAILED}],
}

_RESULT = None


def _observe():
    global _RESULT
    if _RESULT is not None:
        return _RESULT
    t = make_test_app()
    tmp = tempfile.mkdtemp(prefix='guide_health_key_js_')
    try:
        with t.app.app_context():
            acc = seed.make_account()
            seed.make_channel(acc, name='Test Channel', in_guide=True)
            page = t.client.get('/guide').get_data(as_text=True)
        with open(os.path.join(tmp, 'page.html'), 'w', encoding='utf-8') as f:
            f.write(page)
        with open(os.path.join(tmp, 'cases.json'), 'w', encoding='utf-8') as f:
            json.dump(CASES, f)
        proc = subprocess.run(['node', HARNESS, tmp, REPO],
                              capture_output=True, text=True, timeout=120, cwd=REPO)
        if proc.returncode != 0:
            raise AssertionError(f'harness failed:\n{proc.stderr[-4000:]}')
        _RESULT = json.loads(proc.stdout)
        return _RESULT
    finally:
        t.cleanup()
        shutil.rmtree(tmp, ignore_errors=True)


@unittest.skipIf(shutil.which('node') is None, 'node not installed')
@unittest.skipIf(not os.path.isdir(JSDOM), 'jsdom not installed (npm install)')
class GuideHealthKeyTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.obs = _observe()

    def test_the_page_booted_clean_with_an_empty_key(self):
        self.assertEqual(self.obs['errors'], [])
        self.assertTrue(self.obs['host_present'])
        self.assertEqual(self.obs['host_html_at_boot'].strip(), '')

    def test_each_band_on_screen_is_named_once_in_declared_order(self):
        m = self.obs['mixed']
        self.assertEqual(m['bands'], ['great', 'poor', 'untested'])
        self.assertEqual(m['names'], ['Great', 'Poor', 'Never tested'])
        self.assertTrue(m['text'].startswith('Health'))

    def test_each_dot_wears_its_bands_colour_class(self):
        self.assertEqual(self.obs['mixed']['dot_classes'],
                         ['guide-legend-dot hb-great', 'guide-legend-dot hb-poor',
                          'guide-legend-dot hb-none'])

    def test_one_band_names_only_that_band(self):
        self.assertEqual(self.obs['one_band']['bands'], ['good'])

    def test_no_channels_means_no_key(self):
        self.assertEqual(self.obs['nothing']['text'], '')

    def test_a_channel_the_guide_hides_does_not_add_its_band(self):
        self.assertEqual(self.obs['hidden_failed']['bands'], ['good'])


if __name__ == '__main__':
    unittest.main()
