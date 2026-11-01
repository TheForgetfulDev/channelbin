"""The main menu's folding sections and the Recordings header's Schedule recording button,
driven in jsdom (dev/changelog/1363, mockup 48).

Invariants:

  (a) A folded section carries a copy of every red and amber count inside it, each tip
      naming the page it came from; an open section carries none.
  (b) Folding or opening a section in one copy of the menu moves both copies, and sends
      nothing: a fold lasts until the next page load (dev/changelog/1372).
  (c) A page inside a folding section renders that section open in both copies, and saves
      nothing at load, so the next page starts folded again (dev/changelog/1372).
  (d) Schedule recording opens its .menu on a desktop and, on a phone, the bottom sheet
      with the same three links as tiles - built from that menu, so the two cannot differ.

tests/support/menu_revamp.mjs runs the rendered page's own scripts; every assertion lives
here.

  python3 -m unittest tests.test_menu_revamp_js
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tests.support.app import make_test_app  # noqa: E402
from app import db, readiness  # noqa: E402
from app.database import Alert  # noqa: E402

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HARNESS = os.path.join(REPO, 'tests', 'support', 'menu_revamp.mjs')
JSDOM = os.path.join(REPO, 'node_modules', 'jsdom')

NAV = {'blocked': 1, 'degraded': 1, 'blocked_labels': ['Record a program'],
       'degraded_labels': ['See what is on']}


def _drive(html, mode, width='desktop'):
    with tempfile.TemporaryDirectory() as tmp:
        page = os.path.join(tmp, 'page.html')
        with open(page, 'w', encoding='utf-8') as fh:
            fh.write(html)
        proc = subprocess.run([shutil.which('node'), HARNESS, REPO, page, mode, width],
                              capture_output=True, text=True, cwd=REPO, timeout=120)
    if proc.returncode != 0:
        raise AssertionError(f'harness failed:\n{proc.stdout}\n{proc.stderr}')
    return json.loads(proc.stdout)


def _by_key(shell):
    return {s['key']: s for s in shell}


@unittest.skipIf(shutil.which('node') is None, 'node not installed')
@unittest.skipIf(not os.path.isdir(JSDOM), 'jsdom not installed (npm install)')
class FoldingSectionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        t = make_test_app()
        try:
            db.session.add(Alert(alert_type='test', severity='ERROR', title='Broken'))
            db.session.add(Alert(alert_type='test', severity='WARN', title='Slow'))
            db.session.commit()
            with readiness._lock:
                readiness._nav_cache.update(at=datetime.utcnow(), value=NAV)
            cls.home = _drive(t.client.get('/tags').get_data(as_text=True), 'folds')
            # Whatever the cache held when the menu was drawn is what the copies must show.
            cls.nav = readiness.cached_nav()
            cls.inside = _drive(t.client.get('/logs').get_data(as_text=True), 'folds')
        finally:
            with readiness._lock:
                readiness._nav_cache.update(at=None, value=None)
            t.cleanup()

    def test_the_harness_ran_clean(self):
        self.assertEqual(self.home['errors'], [])
        self.assertEqual(self.inside['errors'], [])

    def test_a_folded_section_carries_the_counts_inside_it(self):
        for shell in self.home['atLoad']:
            system = _by_key(shell)['system']
            self.assertTrue(system['folded'])
            self.assertEqual(system['expanded'], 'false')
            got = sorted((c['cls'].split()[-1], c['text']) for c in system['sum'])
            self.assertEqual(got, [('nav-count-bad', '1'),
                                   ('nav-count-ready-bad', str(self.nav['blocked'])),
                                   ('nav-count-ready-warn', str(self.nav['degraded'])),
                                   ('nav-count-warn', '1')])
            tips = {c['cls'].split()[-1]: c['tip'] for c in system['sum']}
            self.assertTrue(tips['nav-count-bad'].startswith('Alerts: 1 unread error'))
            self.assertTrue(tips['nav-count-ready-warn'].startswith('Readiness: '))
            self.assertIn('See what is on', tips['nav-count-ready-warn'])

    def test_an_open_section_carries_none(self):
        for shell in self.inside['atLoad']:
            system = _by_key(shell)['system']
            self.assertFalse(system['folded'])
            self.assertEqual(system['sum'], [])

    def test_channels_and_guide_is_not_a_folding_section(self):
        for shell in self.home['atLoad'] + self.inside['atLoad']:
            self.assertEqual(sorted(_by_key(shell)), ['setup', 'system'])

    def test_folding_in_one_copy_moves_both_and_saves_nothing(self):
        for shell in self.home['afterToggle']:
            system = _by_key(shell)['system']
            self.assertFalse(system['folded'])
            self.assertEqual(system['expanded'], 'true')
            self.assertEqual(system['sum'], [])
        self.assertEqual(self.home['sent'], [])

    def test_a_page_inside_a_folded_section_opens_it_and_saves_nothing(self):
        """dev/changelog/1372: the walkthrough opened Setup and System and saved them open,
        so they stayed open on every page after it."""
        for shell in self.inside['atLoad']:
            self.assertFalse(_by_key(shell)['system']['folded'])
            self.assertTrue(_by_key(shell)['setup']['folded'])
        self.assertEqual(self.inside['sentAtLoad'], 0)
        self.assertEqual(self.inside['sent'], [])


@unittest.skipIf(shutil.which('node') is None, 'node not installed')
@unittest.skipIf(not os.path.isdir(JSDOM), 'jsdom not installed (npm install)')
class ScheduleRecordingTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        t = make_test_app()
        try:
            html = t.client.get('/recordings').get_data(as_text=True)
        finally:
            t.cleanup()
        cls.desktop = _drive(html, 'schedule', 'desktop')
        cls.phone = _drive(html, 'schedule', 'phone')

    def test_the_harness_ran_clean(self):
        self.assertEqual(self.desktop['errors'], [])
        self.assertEqual(self.phone['errors'], [])

    def test_the_header_and_the_empty_page_both_have_it(self):
        self.assertEqual(self.desktop['buttons'], 2)

    def test_a_desktop_opens_the_menu_under_the_button(self):
        self.assertTrue(self.desktop['menuOpen'])
        self.assertEqual(self.desktop['sheet'], [])

    def test_a_phone_gets_the_same_three_links_as_tiles(self):
        self.assertFalse(self.phone['menuOpen'])
        self.assertEqual(self.phone['sheet'], [
            {'href': '/guide', 'label': 'From the TV Guide', 'icon': 'nav-guide'},
            {'href': '/channels?grain=airings', 'label': 'From a search', 'icon': 'airings'},
            {'href': '/recordings/new', 'label': 'Manual recording', 'icon': 'time'},
        ])
        self.assertIn('Schedule a recording', self.phone['sheetTitle'])
        self.assertIn('no program attached', self.phone['sheetNote'])


if __name__ == '__main__':
    unittest.main()
