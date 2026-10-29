"""Tier 0 - the Readiness card's empty failover row and its run-all button, driven in a
real DOM (dev/changelog/1271).

The first-time-user test opened "Survive a feed dropping mid-recording" right after a
recording had reconnected through a dropped feed, read "Nothing is set up for this yet. The
guide groups: No groups are in the guide" and found no way to set anything up, although the
card's intro promises one. "Run 4 checks that cost something" never said what they cost.
Invariants:

  (a) A capability with nothing set up, whose empty check says how to set it up, offers that
      check's link on its summary row, and the opened row carries a "How to set it up" line.
  (b) An empty check with no setup sentence offers no button - nothing there is the user's
      to change.
  (c) The run-all button's tooltip names every check it would run and what each costs, in
      the check's own words.

tests/support/readiness_card.mjs drives the shipped util.js + readiness.js against the markup
/maintenance rendered and the payload readiness.evaluate() built; every assertion lives here.

  python3 -m unittest tests.test_readiness_card_js
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
from app import db, readiness  # noqa: E402

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HARNESS = os.path.join(REPO, 'tests', 'support', 'readiness_card.mjs')
JSDOM = os.path.join(REPO, 'node_modules', 'jsdom')

_RESULT = None


def _observe():
    global _RESULT
    if _RESULT is not None:
        return _RESULT
    t = make_test_app()
    tmp = tempfile.mkdtemp(prefix='readiness_card_js_')
    try:
        acc = seed.make_account()
        seed.make_channel(acc, stream_id=1, name='Northwind News', in_guide=True)
        db.session.commit()
        page = t.client.get('/maintenance').get_data(as_text=True)
        payload = readiness.evaluate()
        with open(os.path.join(tmp, 'page.html'), 'w', encoding='utf-8') as f:
            f.write(page)
        with open(os.path.join(tmp, 'payload.json'), 'w', encoding='utf-8') as f:
            json.dump(payload, f, default=str)
        proc = subprocess.run(['node', HARNESS, tmp, REPO],
                              capture_output=True, text=True, timeout=120, cwd=REPO)
        if proc.returncode != 0:
            raise AssertionError(f'harness failed:\n{proc.stderr[-4000:]}')
        _RESULT = {'dom': json.loads(proc.stdout), 'payload': payload}
        return _RESULT
    finally:
        t.cleanup()
        shutil.rmtree(tmp, ignore_errors=True)


@unittest.skipIf(shutil.which('node') is None, 'node not installed')
@unittest.skipIf(not os.path.isdir(JSDOM), 'jsdom not installed (npm install)')
class ReadinessCardTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.r = _observe()['dom']
        cls.payload = _observe()['payload']

    def test_the_harness_ran_clean(self):
        self.assertEqual(self.r['errors'], [])

    def test_the_failover_row_names_a_backup_channel(self):
        self.assertIn('backup channel', self.r['capName'])
        self.assertIn('Nothing is set up for this yet', self.r['capWhy'])

    def test_the_summary_row_links_to_where_it_is_set_up(self):
        self.assertEqual(self.r['capAction'], 'Channel Groups')

    def test_the_opened_row_says_how_to_set_it_up(self):
        self.assertEqual(self.r['rowAction'], 'Channel Groups')
        self.assertIn('How to set it up', self.r['rowDetail'])
        self.assertIn('reconnects to its own channel', self.r['rowDetail'])

    def test_an_empty_check_with_no_setup_offers_nothing(self):
        self.assertIs(self.r['testedLink'], False)

    def test_the_summary_line_adds_up_to_its_total(self):
        """dev/docs/BUGS.md 2026-10-26 09:35:03 AM: "24 checks · 16 ready · 2 need attention · 4 not run
        yet" added to 22 - the checks with nothing to look at were counted in the total and
        named nowhere."""
        import re
        parts = self.r['meta'].split(' · ')
        total = int(re.match(r'(\d+) checks$', parts[0]).group(1))
        named = sum(int(p.split(' ')[0]) for p in parts[1:] if not p.endswith('ignored'))
        self.assertEqual(named, total, self.r['meta'])
        self.assertTrue(self.payload['counts'][readiness.NOTHING],
                        'the seed must have a check with nothing to look at, or this proves nothing')

    def test_the_head_pill_uses_the_verdicts_words(self):
        """"DEGRADED" sat beside "Ready to record, but 1 thing needs attention" and read as
        a second, different answer (dev/changelog/1292)."""
        self.assertEqual(self.r['degradedPill'], 'Needs attention')

    def test_the_run_all_tooltip_says_what_each_check_costs(self):
        pending = [c for c in self.payload['checks'] if c['status'] == readiness.NOT_RUN]
        self.assertTrue(pending)
        for check in pending:
            self.assertIn(f"{check['label']}: {check['note']}", self.r['runAllTip'])


if __name__ == '__main__':
    unittest.main()
