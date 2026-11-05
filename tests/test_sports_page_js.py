"""The Sports Schedules list's script (static/js/sports.js), driven in jsdom over the page as the app
rendered it (tests/support/sports_page.mjs), per mockup 50, dev/changelog/1444. Throwaway temp SQLite
DB - never the live dvr.db.
  python3 -m unittest tests.test_sports_page_js
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

from tests.support.app import make_test_app  # noqa: E402

from app import db  # noqa: E402
from app.database import SportsEvent, SportsFetch, SportsFollow, SportsLeague  # noqa: E402

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HARNESS = os.path.join(REPO, 'tests', 'support', 'sports_page.mjs')
JSDOM = os.path.join(REPO, 'node_modules', 'jsdom')


def _observe() -> dict:
    t = make_test_app(csrf=False)
    try:
        now = datetime.utcnow()
        for tsdb_id, name, hours, games in ((1, 'Zebra League', 9, 1), (2, 'Alpha League', 2, 3), (3, 'Middle League', 5, 2)):
            league = SportsLeague(tsdb_id=tsdb_id, name=name, sport='Pretend Ball', current_season='2099')
            db.session.add(league)
            db.session.flush()
            db.session.add(SportsFollow(league_id=league.id, team_tsdb_id=0, rule_id=0))
            db.session.add(SportsFetch(target=f'next:{tsdb_id}', kind='next', league_id=league.id, last_at=now,
                                       next_due_at=now + timedelta(hours=9), outcome='ok'))
            for i in range(games):
                start = now + timedelta(hours=hours + i)
                db.session.add(SportsEvent(tsdb_id=tsdb_id * 100 + i, league_id=league.id, name='x', season='2099', round=1,
                                           home_team_id=11, away_team_id=12, home_name='Reds', away_name='Blues',
                                           start_at=start, local_date=start.date()))
        db.session.commit()
        html = t.client.get('/sports').get_data(as_text=True)
    finally:
        t.cleanup()
    with tempfile.TemporaryDirectory() as tmp:
        with open(os.path.join(tmp, 'list.html'), 'w', encoding='utf-8') as fh:
            fh.write(html)
        proc = subprocess.run([shutil.which('node'), HARNESS, tmp, REPO], capture_output=True, text=True,
                              cwd=REPO, timeout=120)
    if proc.returncode != 0:
        raise AssertionError(f'harness failed:\n{proc.stdout}\n{proc.stderr[-4000:]}')
    return json.loads(proc.stdout)


@unittest.skipIf(shutil.which('node') is None or not os.path.isdir(JSDOM), 'node or jsdom not installed')
class SportsListScriptTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.obs = _observe()

    def test_no_script_errors(self):
        self.assertEqual(self.obs['errors'], [])

    def test_soonest_game_first_by_default(self):
        self.assertEqual(self.obs['default_order'], ['alpha league', 'middle league', 'zebra league'])

    def test_a_header_sorts_marks_and_flips(self):
        self.assertEqual(self.obs['by_name'][0], 'alpha league')
        self.assertTrue(self.obs['sorted_marked'])
        self.assertEqual(self.obs['by_name_flipped'][0], 'zebra league')

    def test_a_number_sorts_largest_first(self):
        self.assertEqual(self.obs['by_count'][0], 'alpha league')
        self.assertIn('Upcoming', self.obs['chip'])

    def test_a_row_opens_its_league_and_the_kebab_does_not(self):
        self.assertEqual(len(self.obs['opened']), 1)
        self.assertRegex(self.obs['opened'][0], r'^/sports/\d+$')
        self.assertEqual(self.obs['kebab_opened_page'], 0)

    def test_thesportsdb_is_a_link_in_a_new_tab_in_the_dialog(self):
        self.assertEqual(self.obs['hint_link']['text'], 'TheSportsDB')
        self.assertEqual(self.obs['hint_link']['target'], '_blank')
        self.assertIn('noopener', self.obs['hint_link']['rel'])
        self.assertEqual(self.obs['not_found_link'], {'text': 'TheSportsDB', 'target': '_blank'})
        self.assertEqual(self.obs['league_link']['target'], '_blank')
        self.assertIn('thesportsdb.com/league/9001', self.obs['league_link']['href'])

    def test_typing_filters_the_list_without_a_call(self):
        self.assertEqual(self.obs['picks'], 6)
        self.assertEqual(self.obs['filtered_picks'], 2)
        self.assertEqual([r for r in self.obs['requests'][:self.obs['calls_while_typing']] if 'lookup' in r['path']], [])

    def test_a_lookup_too_soon_says_when(self):
        self.assertIn('Try again in 8 seconds', self.obs['too_soon'])

    def test_a_team_search_offers_that_team_and_follows_it(self):
        self.assertTrue(self.obs['only_team_checked'])
        self.assertEqual(self.obs['follow_body'], [{'tsdb_id': 9001, 'team_id': 11}])


if __name__ == '__main__':
    unittest.main()
