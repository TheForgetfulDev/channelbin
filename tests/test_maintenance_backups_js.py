"""Tier 0 - the Maintenance page's backup list keeps itself current, driven in a real DOM
(dev/changelog/1356).

`loadBackups()` ran at boot and after Back up now only, so the nightly config backup, and
one taken from another tab, were missing from the select until the page was reloaded.
Invariants:

  (a) A one-minute tick in a visible tab re-reads the list.
  (b) A hidden tab does not; the first tick after it is visible again does.
  (c) A backup the user has selected is never dropped by a tick - the list holds while one
      is selected and resumes once the choice is cleared. That hold, not the fetch, is the
      whole design question: loadBackups() rebuilds the <select>.

tests/support/maintenance_backups.mjs drives the shipped util.js + maintenance.js against
the markup the /maintenance route really rendered; every assertion lives here.

  python3 -m unittest tests.test_maintenance_backups_js
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

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HARNESS = os.path.join(REPO, 'tests', 'support', 'maintenance_backups.mjs')
JSDOM = os.path.join(REPO, 'node_modules', 'jsdom')

_RESULT = None


def _observe():
    global _RESULT
    if _RESULT is not None:
        return _RESULT
    t = make_test_app()
    tmp = tempfile.mkdtemp(prefix='maintenance_backups_js_')
    try:
        html = t.app.test_client().get('/maintenance').get_data(as_text=True)
        with open(os.path.join(tmp, 'page.html'), 'w', encoding='utf-8') as f:
            f.write(html)
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
class BackupListRefreshTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.obs = _observe()
        for name, scenario in cls.obs.items():
            if 'error' in scenario:
                raise AssertionError(f'{name}: {scenario["error"]}')
            if scenario.get('errors'):
                raise AssertionError(f'{name}: {scenario["errors"]}')

    def test_a_visible_tick_re_reads_the_list(self):
        """(a)"""
        s = self.obs['visibleTick']
        self.assertIn(60000, s['intervals'], 'no one-minute tick was registered')
        self.assertEqual(s['afterBoot'], 1)
        self.assertEqual(s['afterTick'], 2)
        self.assertIn('config-20261030.yaml', s['options'])

    def test_a_hidden_tab_waits_and_a_visible_one_catches_up(self):
        """(b)"""
        s = self.obs['hiddenTick']
        self.assertEqual(s['whileHidden'], s['afterBoot'])
        self.assertEqual(s['afterVisible'], s['afterBoot'] + 1)

    def test_a_selected_backup_is_never_dropped_by_the_tick(self):
        """(c)"""
        s = self.obs['selectionHolds']
        self.assertEqual(s['whileSelected']['calls'], s['afterBoot'])
        self.assertEqual(s['whileSelected']['selected'], 'config-20261030.yaml')
        self.assertEqual(s['afterCleared'], s['afterBoot'] + 1)


if __name__ == '__main__':
    unittest.main(verbosity=2)
