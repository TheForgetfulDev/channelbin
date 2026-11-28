"""The TV Guide's channel pill for a channel its account's last sync did not list, and its
replacement hint (static/js/guide.js renderLifecycleBadge() / replacementHint(),
DESIGN-channel-successors.md section 9, dev/changelog/1801).

Each case's row is the real /api/guide/epg answer for a seeded channel, drawn by the real
guide.js in jsdom (tests/support/guide_lifecycle_pill.mjs), so the server's fields and the
script that reads them are checked together.

Run standalone:
  python3 -m unittest tests.test_guide_lifecycle_pill_js
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import db  # noqa: E402
from app.database import AccountSyncLog  # noqa: E402
from tests.support import make_test_app, seed  # noqa: E402

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HARNESS = os.path.join(REPO, 'tests', 'support', 'guide_lifecycle_pill.mjs')
JSDOM = os.path.join(REPO, 'node_modules', 'jsdom')
FMT = '%Y-%m-%dT%H:%M:%S'


def _observe():
    """{case: what the pill and the hints say} for four guide rows: unlisted with one
    replacement, unlisted with none, missing for a week with two, and listed."""
    t = make_test_app()
    try:
        now = datetime.utcnow().replace(microsecond=0)
        start = now - timedelta(hours=1)
        acc = seed.make_account(name='Acct')
        acc.last_sync_at = start + timedelta(minutes=5)
        db.session.add(AccountSyncLog(account_id=acc.id, started_at=start,
                                      completed_at=acc.last_sync_at, status='SUCCESS'))

        def channel(name, psid, seen, in_guide=True):
            ch = seed.make_channel(acc, name=name, in_guide=in_guide)
            ch.provider_stream_id = psid
            ch.last_seen_at = seen
            return ch

        old, listed = start - timedelta(hours=3), start + timedelta(minutes=1)
        rows = {
            'one': channel('ESPN', '1', old),
            'none': channel('CNN', '2', old),
            'missing': channel('Gone', '3', now - timedelta(days=10)),
            'listed': channel('Fine', '4', listed),
        }
        channel('ESPN HD', '1', listed, in_guide=False)
        channel('Gone A', '3', listed, in_guide=False)
        channel('Gone B', '3', listed, in_guide=False)
        db.session.commit()
        r = t.client.get(f'/api/guide/epg?start={now.strftime(FMT)}'
                         f'&end={(now + timedelta(hours=3)).strftime(FMT)}')
        assert r.status_code == 200, r.get_data(as_text=True)
        by_id = {c['id']: c for c in r.get_json()['channels']}
        cases = {name: by_id[ch.id] for name, ch in rows.items()}
    finally:
        t.cleanup()
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, 'cases.json')
        with open(path, 'w', encoding='utf-8') as fh:
            json.dump(cases, fh)
        proc = subprocess.run(['node', HARNESS, path, REPO],
                              capture_output=True, text=True, timeout=120, cwd=REPO)
    if proc.returncode != 0:
        raise AssertionError(f'harness failed:\n{proc.stderr[-4000:]}')
    out = json.loads(proc.stdout)
    if out['errors']:
        raise AssertionError(f'page errors: {out["errors"]}')
    return out['cases']


@unittest.skipIf(shutil.which('node') is None, 'node not installed')
@unittest.skipIf(not os.path.isdir(JSDOM), 'jsdom not installed (npm install)')
class GuideLifecyclePillTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.seen = _observe()

    def test_an_unlisted_row_says_so_without_dimming_and_names_its_replacement(self):
        one = self.seen['one']
        self.assertEqual(one['pill'], 'Not in last sync')
        self.assertIn("Acct's last sync did not list this channel", one['tip'])
        self.assertIn('Another channel carries the same stream', one['tip'])
        self.assertFalse(one['dimmed'], 'an unlisted channel may still play')
        self.assertIn('tap Open channel below to replace it', one['sheet_hint'])

    def test_an_unlisted_row_with_no_replacement_offers_none(self):
        none = self.seen['none']
        self.assertEqual(none['pill'], 'Not in last sync')
        self.assertNotIn('carries the same stream', none['tip'])
        self.assertEqual(none['sheet_hint'], '')

    def test_a_missing_row_keeps_its_pill_and_dimming_and_counts_its_replacements(self):
        missing = self.seen['missing']
        self.assertEqual(missing['pill'], 'Missing')
        self.assertTrue(missing['dimmed'])
        self.assertIn('2 channels carry the same stream', missing['tip'])

    def test_a_listed_row_has_no_pill(self):
        listed = self.seen['listed']
        self.assertIsNone(listed['pill'])
        self.assertFalse(listed['dimmed'])


if __name__ == '__main__':
    unittest.main()
