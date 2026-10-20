"""util.js's shared page pieces, driven in a real DOM (dev/changelog/1244).

Three things every page used to spell for itself: the mobile breakpoint (`matchMedia('(max-
width: 960px)')` in seven page scripts, each behind its own isPhone/MOBILE_MQ), the boot-
block reader (`JSON.parse(el.textContent)` seven ways with three failure behaviors) and the
phone's bottom sheet (three builders of one DESIGN.md 17.5 component). Each is one thing in
util.js now; these hold what the one thing does.

  python3 -m unittest tests.test_util_shared_js
"""
import json
import os
import re
import shutil
import subprocess
import unittest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HARNESS = os.path.join(REPO, 'tests', 'support', 'util_shared.mjs')
JSDOM = os.path.join(REPO, 'node_modules', 'jsdom')
JS_DIR = os.path.join(REPO, 'static', 'js')

_RESULTS = {}


def _observe(width):
    if width not in _RESULTS:
        proc = subprocess.run([shutil.which('node'), HARNESS, REPO, str(width)],
                              capture_output=True, text=True, cwd=REPO, timeout=120)
        if proc.returncode != 0:
            raise AssertionError(f'harness failed:\n{proc.stdout}\n{proc.stderr}')
        _RESULTS[width] = json.loads(proc.stdout)
    return _RESULTS[width]


@unittest.skipIf(shutil.which('node') is None, 'node not installed')
@unittest.skipIf(not os.path.isdir(JSDOM), 'jsdom not installed (npm install)')
class SharedUtilTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.phone = _observe(390)
        cls.desktop = _observe(1280)

    def test_no_script_error(self):
        self.assertEqual(self.phone['errors'], [])
        self.assertEqual(self.desktop['errors'], [])

    # ── breakpoint ──
    def test_is_phone_reads_the_one_breakpoint(self):
        self.assertEqual(self.phone['mq']['media'], '(max-width: 960px)')
        self.assertTrue(self.phone['mq']['isPhone'])
        self.assertFalse(self.desktop['mq']['isPhone'])

    def test_a_change_listener_lands_on_the_same_object(self):
        self.assertEqual(self.phone['mq']['listenerQuery'], '(max-width: 960px)')

    def test_no_page_script_declares_its_own_breakpoint_or_reader(self):
        bad = []
        for name in sorted(os.listdir(JS_DIR)):
            if not name.endswith('.js') or name == 'util.js':
                continue
            with open(os.path.join(JS_DIR, name), encoding='utf-8') as fh:
                src = fh.read()
            if 'matchMedia(' in src:
                bad.append(f'{name}: matchMedia')
            if re.search(r'JSON\.parse\([\w.]*textContent', src):
                bad.append(f'{name}: JSON.parse(...textContent)')
            if re.search(r"className = 'acct-sheet'", src):
                bad.append(f'{name}: builds its own sheet')
        self.assertEqual(bad, [], 'use util.js: MOBILE_MQ/isPhone, readBootJson, openMenuSheet')

    # ── boot-block reader ──
    def test_a_good_block_parses(self):
        self.assertEqual(self.phone['boot']['good'], {'answer': 42, 'list': [1, 2]})

    def test_a_missing_block_answers_with_the_fallback_quietly(self):
        self.assertIsNone(self.phone['boot']['missingNull'])
        self.assertEqual(self.phone['boot']['missingFallback'], {})

    def test_an_unparseable_block_answers_with_the_fallback_and_says_so(self):
        boot = self.phone['boot']
        self.assertEqual(boot['bad'], {'fell': 'back'})
        self.assertEqual(boot['warningsAfterBad'], boot['warningsBeforeBad'] + 1)
        self.assertTrue(any('bad-boot' in w for w in self.phone['warnings']))

    # ── bottom sheet from a menu ──
    def test_the_sheet_copies_every_menu_row_with_its_attributes(self):
        rows = self.phone['menuSheet']['rows']
        self.assertEqual(self.phone['menuSheet']['title'], 'Row name')
        self.assertEqual([r['cls'] for r in rows],
                         ['sheet-act', 'sheet-act', 'sheet-act', 'sheet-sep', 'sheet-act danger'])
        self.assertEqual([r['act'] for r in rows], ['sync', None, 'pause', None, 'delete'])
        self.assertEqual(rows[0]['accountId'], '7', 'data attributes travel with the copy')
        self.assertEqual(rows[1]['tag'], 'A')
        self.assertEqual(rows[1]['href'], '/accounts/7/channels')
        self.assertTrue(rows[2]['disabled'])
        self.assertEqual(self.phone['menuSheet']['menuStillHasItems'], 5, 'copies, not moves')

    def test_a_disabled_row_does_nothing(self):
        self.assertEqual(self.phone['menuSheet']['actsAfterDisabledTap'], 0)

    def test_a_tap_closes_first_then_acts_with_the_original_and_never_bubbles(self):
        ms = self.phone['menuSheet']
        self.assertEqual(ms['overlaysBefore'], 1)
        self.assertEqual(len(ms['acts']), 1)
        act = ms['acts'][0]
        self.assertEqual(act['act'], 'delete')
        self.assertEqual(act['overlaysWhenCalled'], 0, 'the sheet is gone before the action runs')
        self.assertTrue(act['originalIsMenuChild'])
        self.assertEqual(act['originalAct'], 'delete')
        self.assertEqual(ms['overlaysAfter'], 0)
        self.assertEqual(ms['bubbledToDocument'], 0,
                         "a page's delegated [data-act] handler must not see the tap")

    # ── bottom sheet from an item list ──
    def test_a_list_builds_the_same_rows(self):
        rows = self.phone['listSheet']['rows']
        self.assertEqual([r['cls'] for r in rows],
                         ['sheet-act', 'sheet-act', 'sheet-sep', 'sheet-act', 'sheet-act danger'])
        self.assertEqual(rows[1]['tag'], 'A')
        self.assertEqual(rows[1]['href'], '/accounts/7/channels')
        self.assertTrue(rows[3]['disabled'])
        self.assertEqual(rows[4]['text'], 'Delete account')

    def test_a_list_item_acts_with_no_original(self):
        ls = self.phone['listSheet']
        self.assertEqual(ls['acts'], [{'act': 'block', 'original': None}])
        self.assertEqual(ls['overlaysAfter'], 0)


if __name__ == '__main__':
    unittest.main(verbosity=2)
