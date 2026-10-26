"""Tier 0 - numbers that say what they count: the menu's badges and the footer at first
paint, the two free-space readings, the database's size, and what the Dashboard badge is
counting (dev/changelog/1292).

The second first-time-user test saw the sidebar's badges missing and the footer at
"CPU -% · RAM -% / Disk - free" on some page loads, a Maintenance count that rose during a
first sync with nothing to say why, "DISK FREE 13.8 TB" on a tile beside "14.1 TB free" in
the footer, and Maintenance's Storage reading "Database 4.0 KB" on an install holding 1,844
programs. Invariants:

  (a) With a fresh cached Readiness answer, unread alerts and a fresh stats reading, a page
      draws the Maintenance and Alerts counts and the footer before any poll answers, and
      each count's tooltip names what it is counting.
  (b) With nothing cached, the first paint is the nothing-active state: no count shown, the
      footer dashed.
  (c) The footer's free space and the Dashboard's Disk free tile print one number for one
      disk (dev/docs/BUGS.md 2026-10-26 09:35:02 AM).
  (d) The database size Maintenance shows is dvr.db plus its -wal, with the -wal share
      sent on its own (dev/docs/BUGS.md 2026-10-26 09:35:01 AM).
  (e) /api/nav-status names each thing the Dashboard count counts.
  (f) The Readiness nav block names the capabilities behind each count.

tests/support/nav_first_paint.mjs runs a rendered page's own inline scripts with a poll that
never answers; every assertion lives here.

  python3 -m unittest tests.test_nav_first_paint
"""
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tests.support.app import make_test_app  # noqa: E402
from tests.support import seed  # noqa: E402
from app import db, readiness  # noqa: E402
from app.database import ACCOUNT_STATUS_SYNCING, Alert  # noqa: E402
from app.fmt_utils import fmt_bytes  # noqa: E402

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HARNESS = os.path.join(REPO, 'tests', 'support', 'nav_first_paint.mjs')
JSDOM = os.path.join(REPO, 'node_modules', 'jsdom')

#: A disk where binary and decimal TB part in the first decimal: 13.8 TiB free, which a
#: GiB figure divided by 1000 printed as "14.1 TB".
FREE = 15_170_000_000_000
TOTAL = 20_000_000_000_000

NAV = {'blocked': 1, 'degraded': 2,
       'blocked_labels': ['Record a program right now'],
       'degraded_labels': ['See what is on', 'Search your channels and programs quickly']}


def _drive(html):
    with tempfile.TemporaryDirectory() as tmp:
        page = os.path.join(tmp, 'page.html')
        with open(page, 'w', encoding='utf-8') as fh:
            fh.write(html)
        proc = subprocess.run([shutil.which('node'), HARNESS, REPO, page],
                              capture_output=True, text=True, cwd=REPO, timeout=120)
    if proc.returncode != 0:
        raise AssertionError(f'harness failed:\n{proc.stdout}\n{proc.stderr}')
    return json.loads(proc.stdout)


def _tile(html, key):
    m = re.search(r'<div class="m-k">' + re.escape(key)
                  + r'</div>\s*<div class="m-v">([^<]*)</div>', html)
    return m.group(1).strip() if m else None


@unittest.skipIf(shutil.which('node') is None, 'node not installed')
@unittest.skipIf(not os.path.isdir(JSDOM), 'jsdom not installed (npm install)')
class FirstPaintTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from app.routes.system import _system_stats_dict
        t = make_test_app()
        try:
            # Nothing cached and nothing unread: the page as a cold start draws it.
            cls.cold = _drive(t.client.get('/recordings').get_data(as_text=True))
            db.session.add(Alert(alert_type='test', severity='ERROR', title='Broken'))
            db.session.add(Alert(alert_type='test', severity='WARN', title='Slow'))
            db.session.commit()
            with readiness._lock:
                readiness._nav_cache.update(at=datetime.utcnow(), value=NAV)
            with mock.patch('app.routes.system._disk_bytes', return_value=(TOTAL, FREE)):
                _system_stats_dict()
                cls.dash_html = t.client.get('/').get_data(as_text=True)
            # The Dashboard may evaluate Readiness itself before the menu renders; whatever
            # the cache held when the menu was drawn is what the badges must show.
            cls.nav = readiness.cached_nav()
            cls.warm = _drive(cls.dash_html)
        finally:
            t.cleanup()

    def test_the_harness_ran_clean(self):
        self.assertEqual(self.cold['errors'], [])
        self.assertEqual(self.warm['errors'], [])

    def test_cached_answers_draw_the_badges_before_any_poll(self):
        self.assertTrue(self.nav['blocked'] and self.nav['degraded'],
                        'the seed must leave both counts above zero, or this proves nothing')
        self.assertEqual((self.warm['readyBad']['text'], self.warm['readyBad']['shown']),
                         (str(self.nav['blocked']), True))
        self.assertEqual((self.warm['readyWarn']['text'], self.warm['readyWarn']['shown']),
                         (str(self.nav['degraded']), True))
        self.assertEqual((self.warm['alertBad']['text'], self.warm['alertBad']['shown']), ('1', True))
        self.assertEqual((self.warm['alertWarn']['text'], self.warm['alertWarn']['shown']), ('1', True))
        self.assertNotEqual(self.warm['cpu'], '-')
        self.assertNotEqual(self.warm['disk'], '-')

    def test_each_badge_says_what_it_counts(self):
        for label in self.nav['blocked_labels']:
            self.assertIn(label, self.warm['readyBad']['tip'])
        for label in self.nav['degraded_labels']:
            self.assertIn(label, self.warm['readyWarn']['tip'])
        self.assertIn('unread error', self.warm['alertBad']['tip'])
        self.assertIn('unread warning', self.warm['alertWarn']['tip'])
        self.assertIn('Every recording on this install', self.warm['recordings']['tip'])

    def test_nothing_cached_is_the_nothing_active_state(self):
        for key in ('readyBad', 'readyWarn', 'alertBad', 'alertWarn'):
            self.assertFalse(self.cold[key]['shown'], key)
        self.assertEqual(self.cold['cpu'], '-')
        self.assertEqual(self.cold['disk'], '-')

    def test_the_footer_and_the_tile_print_one_free_space(self):
        tile = _tile(self.dash_html, 'Disk free')
        self.assertEqual(tile, fmt_bytes(FREE))
        self.assertEqual(self.warm['disk'], tile)


class StorageSizeTests(unittest.TestCase):
    def setUp(self):
        self.t = make_test_app()
        self.tmp = tempfile.mkdtemp(prefix='storage_db_')

    def tearDown(self):
        self.t.cleanup()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_the_database_size_counts_its_write_ahead_log(self):
        """dev/docs/BUGS.md 2026-10-26 09:35:01 AM: "Database 4.0 KB" beside 1,844 programs, because
        nearly everything on a new install is still in dvr.db-wal."""
        from app.config import load_config
        path = os.path.join(self.tmp, 'dvr.db')
        with open(path, 'wb') as fh:
            fh.write(b'\0' * 4096)
        with open(path + '-wal', 'wb') as fh:
            fh.write(b'\0' * 4_000_000)
        cfg = load_config()
        cfg['database']['path'] = path
        with mock.patch('app.routes.system.load_config', return_value=cfg):
            payload = self.t.client.get('/api/system/storage-details').get_json()
        self.assertEqual(payload['db_bytes'], 4096 + 4_000_000)
        self.assertEqual(payload['db_wal_bytes'], 4_000_000)


class CountLabelTests(unittest.TestCase):
    def setUp(self):
        self.t = make_test_app()

    def tearDown(self):
        self.t.cleanup()

    def test_nav_status_names_what_the_dashboard_count_counts(self):
        acc = seed.make_account(name='Northwind')
        acc.status = ACCOUNT_STATUS_SYNCING
        db.session.commit()
        activity = self.t.client.get('/api/nav-status').get_json()['activity']
        self.assertEqual(activity['dashboard_activity_count'], 1)
        self.assertEqual(len(activity['dashboard_activity_labels']), 1)
        self.assertIn('Northwind', activity['dashboard_activity_labels'][0])

    def test_the_readiness_nav_names_the_capabilities_it_counts(self):
        payload = readiness.evaluate()
        caps = payload['headline_capabilities']
        nav = payload['nav']
        self.assertEqual(nav['blocked_labels'],
                         [c['label'] for c in caps if c['state'] == readiness.CANNOT])
        self.assertEqual(nav['degraded_labels'],
                         [c['label'] for c in caps if c['state'] == readiness.DEGRADED])
        self.assertEqual(nav['blocked'], len(nav['blocked_labels']))
        self.assertTrue(nav['blocked'] or nav['degraded'],
                        'an empty install has something blocked, or this proves nothing')


if __name__ == '__main__':
    unittest.main()
