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

    def test_the_page_refreshes_itself_on_a_timer(self):
        """dev/changelog/1495: the list and a league's page were drawn once and never again, so a
        schedule filling while the page was open showed the same calls left and the same games
        until a reload."""
        self.assertTrue(self.obs['live_interval'])
        self.assertEqual(self.obs['live_idle']['gets'], 1)
        self.assertEqual(self.obs['live_idle']['mark'], 'idle')

    def test_a_refresh_keeps_the_chosen_sort(self):
        self.assertEqual(self.obs['live_sorted'][0], 'zebra league')
        self.assertEqual(self.obs['live_idle']['order'], self.obs['live_sorted'])

    def test_a_hidden_tab_waits_and_catches_up_when_shown(self):
        self.assertEqual(self.obs['live_hidden_gets'], 0)
        self.assertEqual(self.obs['live_shown_mark'], 'shown')

    def test_no_refresh_while_the_page_is_in_use(self):
        self.assertTrue(self.obs['kebab_menu_open'])
        self.assertEqual(self.obs['live_menu_gets'], 0, 'an open menu')
        self.assertEqual(self.obs['live_typing_gets'], 0, 'a text field holding something')
        self.assertEqual(self.obs['live_selection_gets'], 0, 'text selected')

    def test_a_failed_refresh_leaves_the_page_alone(self):
        self.assertEqual(self.obs['live_fail_gets'], 1)
        self.assertEqual(self.obs['live_fail'], {'navigations': 0, 'rows': 3})

    def test_an_action_refresh_lands_after_a_timed_one_already_in_flight(self):
        self.assertEqual(self.obs['live_race'], {'mark': 'action', 'deletes': 1})

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

    def test_the_dialog_is_named_for_leagues_and_teams(self):
        """dev/changelog/1497."""
        self.assertEqual(self.obs['add_title'], 'Add league or team')
        self.assertEqual(self.obs['add_button'], 'Add league or team')

    def test_search_is_the_footer_primary_and_nothing_in_the_body(self):
        """dev/changelog/1497: the search sat in the body, where it looked like a link out to TheSportsDB."""
        self.assertEqual(self.obs['entry_foot'], ['Cancel', 'Search'])
        self.assertEqual(self.obs['body_buttons'], 0)
        self.assertTrue(self.obs['search_disabled_empty'])
        self.assertTrue(self.obs['search_disabled_shipped_name'])
        self.assertFalse(self.obs['search_disabled_typed'])
        self.assertNotIn('One call', self.obs['dialog_text'])

    def test_enter_searches(self):
        self.assertIsNotNone(self.obs['not_found_link'])

    def test_back_returns_to_the_entry_with_the_text_kept(self):
        """dev/changelog/1497: after a search there was no way back to try another name."""
        self.assertEqual(self.obs['confirm_foot'], ['Back', 'Cancel', 'Follow'])
        self.assertEqual(self.obs['back_value'], 'reds')
        self.assertEqual(self.obs['back_foot'], ['Cancel', 'Search'])
        self.assertTrue(self.obs['back_search_enabled'])

    def test_several_rows_ask_which_one_with_back_and_name_the_left_out(self):
        """dev/changelog/1497: only a paid key or a changed API returns more than one row."""
        self.assertEqual(len(self.obs['choices']), 2)
        self.assertEqual(self.obs['choices_foot'], ['Back', 'Cancel'])
        self.assertIn('Left out, as defunct: Old Reds.', self.obs['choices_text'])
        self.assertIn('Only Reds Reserves', self.obs['choice_confirm_team'])
        self.assertFalse(self.obs['best_match_after_choice'])

    def test_escape_on_a_later_step_goes_back_with_the_text_kept(self):
        self.assertTrue(self.obs['escape_dialog_open'])
        self.assertEqual(self.obs['escape_value'], 'two')

    def test_only_a_name_search_says_it_is_the_best_match(self):
        """dev/changelog/1497: the free key gives one row for a name, which read as the only team."""
        self.assertTrue(self.obs['best_match_after_search'])
        self.assertFalse(self.obs['best_match_after_pick'])
        self.assertNotIn('costs the same calls', self.obs['confirm_text'])


if __name__ == '__main__':
    unittest.main()
