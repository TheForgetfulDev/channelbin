"""The team picker of Add league or team and Change teams (static/js/sports.js, mockup 60,
dev/changelog/1641), driven in jsdom over the Sports Schedules list as the app rendered it
(tests/support/sports_team_picker.mjs). A league already here is answered by the app's own lookup
route, so the picker reads what the server really sends. Throwaway temp SQLite DB - never the live
dvr.db.
  python3 -m unittest tests.test_sports_team_picker_js
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
from app.database import SportsEvent, SportsFollow, SportsLeague, SportsTeam  # noqa: E402

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HARNESS = os.path.join(REPO, 'tests', 'support', 'sports_team_picker.mjs')
JSDOM = os.path.join(REPO, 'node_modules', 'jsdom')
NFL = 4391


def _observe() -> dict:
    t = make_test_app(csrf=False)
    try:
        now = datetime.utcnow()
        nfl = SportsLeague(tsdb_id=NFL, name='NFL', sport='American Football', current_season='2026')
        early = SportsLeague(tsdb_id=9001, name='Early League', sport='Pretend Ball', current_season='2099')
        db.session.add_all([nfl, early])
        db.session.flush()
        db.session.add(SportsFollow(league_id=nfl.id, team_tsdb_id=0, rule_id=0))
        db.session.add(SportsFollow(league_id=early.id, team_tsdb_id=77, rule_id=0))
        db.session.add(SportsTeam(tsdb_id=77, full_name='Early Team', names=json.dumps(['Early Team'])))
        start = now + timedelta(days=1)
        db.session.add(SportsEvent(tsdb_id=501, league_id=nfl.id, name='Reds vs Blues', season='2026', round=1,
                                   home_team_id=11, away_team_id=12, home_name='Pretendville Reds',
                                   away_name='Pretendville Blues', start_at=start, local_date=start.date()))
        db.session.commit()
        html = t.client.get('/sports').get_data(as_text=True)
        # The lookup of a league already here makes no call, so the route answers it in the test.
        nfl_answer = t.client.post('/api/sports/lookup', json={'tsdb_id': NFL}).get_json()
        reds = {**nfl_answer, 'team': {'team_id': 11, 'team': 'Pretendville Reds', 'league': nfl_answer['league']},
                'best_match': True, 'skipped': []}
        answers = {'nfl': nfl_answer, 'reds': reds, 'early_id': early.id,
                   'early_league': {'tsdb_id': 9001, 'name': 'Early League', 'sport': 'Pretend Ball', 'season': '2099'}}
    finally:
        t.cleanup()
    with tempfile.TemporaryDirectory() as tmp:
        for name, body in (('list.html', html), ('answers.json', json.dumps(answers))):
            with open(os.path.join(tmp, name), 'w', encoding='utf-8') as fh:
                fh.write(body)
        proc = subprocess.run([shutil.which('node'), HARNESS, tmp, REPO], capture_output=True, text=True,
                              cwd=REPO, timeout=120)
    if proc.returncode != 0:
        raise AssertionError(f'harness failed:\n{proc.stdout}\n{proc.stderr[-4000:]}')
    return json.loads(proc.stdout)


@unittest.skipIf(shutil.which('node') is None or not os.path.isdir(JSDOM), 'node or jsdom not installed')
class TeamPickerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.obs = _observe()

    def test_no_script_errors(self):
        self.assertEqual(self.obs['errors'], [])

    def test_a_league_followed_is_offered_again_and_says_what_is_followed(self):
        picks = {p['text']: p['disabled'] for p in self.obs['picks']}
        self.assertIn('NFL Following all games', picks)
        self.assertFalse(picks['NFL Following all games'], 'a followed league is no longer greyed out')
        self.assertIn('English Premier League Soccer', picks)

    def test_a_team_in_a_league_followed_whole_starts_on_all_games_and_says_it_changes_nothing(self):
        w = self.obs['whole']
        self.assertEqual(w['now'], 'You follow all NFL games now.')
        self.assertTrue(w['all_checked'])
        self.assertIn('All games already covers Pretendville Reds, so following it changes nothing', w['effect']['text'])
        self.assertTrue(w['effect']['warn'], 'why Save is disabled stands out')
        self.assertIs(w['save'], True)
        self.assertFalse(w['follow'], 'a followed league is saved, not followed again')
        self.assertEqual(w['rows'], 2, 'its teams are the ticked list from its games')
        self.assertIn('untick it and tick the right one', w['best'])

    def test_only_these_teams_says_what_it_stops_and_ticks_more_with_no_call(self):
        self.assertIn("follow only Pretendville Reds, and stop following NFL's other games", self.obs['narrowed']['effect']['text'])
        self.assertFalse(self.obs['narrowed']['effect']['warn'], 'what Save would do is a plain line')
        self.assertIs(self.obs['narrowed']['save'], False)
        self.assertIn('Pretendville Blues and Pretendville Reds', self.obs['two']['text'])
        self.assertEqual(self.obs['ticking_lookups'], 0)

    def test_save_sends_the_ticked_list_and_says_so_after_the_reload(self):
        self.assertEqual(self.obs['whole_post'], {'method': 'POST', 'path': '/api/sports/leagues',
                                                  'body': {'tsdb_id': NFL, 'team_ids': [11, 12]}})
        self.assertEqual(self.obs['whole_toast']['message'], 'NFL: following Pretendville Blues and Pretendville Reds.')

    def test_a_team_found_with_no_upcoming_game_has_a_row_to_untick(self):
        self.assertEqual(self.obs['no_game_row'], [
            {'name': 'Pretendville Blues', 'checked': False},
            {'name': 'Pretendville Greys', 'checked': True},
            {'name': 'Pretendville Reds', 'checked': False}])

    def test_a_league_never_here_adds_teams_by_search(self):
        f = self.obs['fresh']
        self.assertIsNone(f['now'])
        self.assertEqual(f['chips'], ['Arsenal'])
        self.assertTrue(f['teams_checked'])
        self.assertIs(f['follow'], False)
        self.assertIn('does not have the English Premier League schedule yet, so each team is found with a search', f['text'])
        self.assertIn('Getting a new schedule can take up to an hour', f['text'])
        self.assertNotIn('more teams can be picked once it is here', f['text'])
        a = self.obs['after_chelsea']
        self.assertEqual(a['chips'], ['Arsenal', 'Chelsea'])
        self.assertIn("Added Chelsea, TheSportsDB's best match", a['msg'])
        self.assertEqual(a['effect']['text'], 'ChannelBin will follow only Arsenal and Chelsea.')
        self.assertIn('already on the list', self.obs['again'])

    def test_a_team_of_another_league_is_refused_and_named(self):
        o = self.obs['other_league']
        self.assertIn('is Elsewhere Owls, in the Other League, not the English Premier League', o['msg'])
        self.assertEqual(o['chips'], ['Arsenal', 'Chelsea'])

    def test_one_call_at_a_time_is_waited_out_and_the_search_sent_again(self):
        self.assertIn('one call at a time to TheSportsDB. Searching for "late" in 1 second.', self.obs['waiting']['msg'])
        self.assertTrue(self.obs['waiting']['held'])
        self.assertEqual(self.obs['waited']['chips'], ['Arsenal', 'Chelsea', 'Liverpool'])
        self.assertFalse(self.obs['waited']['held'])

    def test_follow_sends_the_chips(self):
        self.assertEqual(self.obs['removed'], ['Arsenal', 'Liverpool'])
        self.assertEqual(self.obs['fresh_post']['body'], {'tsdb_id': 4328, 'team_ids': [101, 104]})

    def test_change_teams_before_any_games_uses_chips_and_search(self):
        self.assertTrue(self.obs['early_menu'], 'Change teams is in the row menu before any games are here')
        c = self.obs['change']
        self.assertEqual(c['title'], 'Early League: teams')
        self.assertEqual(c['chips'], ['Early Team'])
        self.assertEqual(c['now'], 'You follow Early Team now.')
        self.assertTrue(c['effect']['warn'])
        self.assertIs(c['save'], True)
        a = self.obs['change_added']
        self.assertEqual(a['chips'], ['Early Team', 'Pretendville Greens'])
        self.assertIn('This adds Pretendville Greens', a['effect']['text'])
        self.assertIs(a['save'], False)
        self.assertEqual(self.obs['change_post']['method'], 'PUT')
        self.assertEqual(self.obs['change_post']['body'], {'team_ids': [77, 78]})


if __name__ == '__main__':
    unittest.main()
