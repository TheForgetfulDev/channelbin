"""Tier 0 - the TV Guide says why a channel has no stream details (static/js/guide.js,
dev/changelog/1276).

The line under a guide channel's name read a bare "no stream data" whatever the reason, and a
first-time user who saw it beside the `--` health badge could explain neither. It now names
the reason on the line itself - the tooltip is not something a person reads before they are
confused - and the phone's channel sheet uses the same words through the same helper.

Runs the shipped util.js + guide.js against the markup the real /guide route rendered
(tests/support/guide_tech_line.mjs); every assertion is here. jsdom computes no layout, so
this says nothing about how the line looks.

Runs against a throwaway temp SQLite DB - never the live one.
  python3 -m unittest tests.test_guide_tech_line_js
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
from app.database import (  # noqa: E402
    TEST_STATUS_CANCELLED, TEST_STATUS_COMPLETED, TEST_STATUS_FAILED,
)

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HARNESS = os.path.join(REPO, 'tests', 'support', 'guide_tech_line.mjs')
JSDOM = os.path.join(REPO, 'node_modules', 'jsdom')

CASES = {
    'never_tested': {'id': 9001, 'last_test_status': None},
    'failed': {'id': 9002, 'last_test_status': TEST_STATUS_FAILED},
    'cancelled': {'id': 9003, 'last_test_status': TEST_STATUS_CANCELLED},
    'passed_but_empty': {'id': 9004, 'last_test_status': TEST_STATUS_COMPLETED},
    'measured': {'id': 9005, 'last_test_status': TEST_STATUS_COMPLETED,
                 'last_test_resolution': '1920x1080', 'last_test_fps': 59.94},
}

_RESULT = None


def _observe():
    global _RESULT
    if _RESULT is not None:
        return _RESULT
    t = make_test_app()
    tmp = tempfile.mkdtemp(prefix='guide_tech_line_js_')
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
class GuideTechLineTests(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.obs = _observe()
        cls.cases = cls.obs['cases']

    def test_the_page_booted_clean(self):
        self.assertEqual([], self.obs['errors'])

    def test_each_reason_has_its_own_words(self):
        expected = {
            'never_tested': 'not tested yet',
            'failed': 'last check failed',
            'cancelled': 'last check cancelled',
            'passed_but_empty': 'no stream details',
        }
        for case, words in expected.items():
            self.assertEqual(words, self.cases[case]['row_text'], case)

    def test_the_phone_sheet_says_the_same_thing(self):
        for case, got in self.cases.items():
            if case != 'measured':
                self.assertEqual(got['row_text'], got['sheet_text'], case)

    def test_the_tooltip_still_explains_where_the_details_come_from(self):
        self.assertIn('health check', self.cases['never_tested']['row_tip'])

    def test_a_measured_channel_shows_its_numbers(self):
        self.assertIn('1080p', self.cases['measured']['row_text'])
        self.assertIn('60fps', self.cases['measured']['row_text'])

    def test_the_old_catch_all_is_gone(self):
        for case, got in self.cases.items():
            self.assertNotIn('no stream data', got['row_text'], case)


if __name__ == '__main__':
    unittest.main()
