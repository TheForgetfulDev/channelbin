"""A team's other names: TheSportsDB's read again about weekly, and the ones a person adds on the
league page (app/sports_data.py, app/sports_match.py::set_team_names, dev/changelog/1580).

Calls run over real HTTP against tests/support/fake_sportsdb.py, with made-up teams. Throwaway temp
SQLite DB - never the live dvr.db.
  python3 -m unittest tests.test_sports_team_names
"""
import json
import os
import sys
import unittest
from datetime import datetime, timedelta
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tests.support.app import make_test_app  # noqa: E402
from tests.support.fake_sportsdb import LEAGUE_ID, FakeSportsDB, team_row  # noqa: E402
from tests.support.seed import make_account, make_channel, make_epg_entry  # noqa: E402

from app import db, sports_data, sports_match  # noqa: E402
from app.database import (  # noqa: E402
    SPORTS_KIND_TEAM, SPORTS_MATCH_KIND_GUIDE, SportsEvent, SportsFetch, SportsLeague, SportsMatch, SportsState,
    SportsTeam,
)

NOW = datetime(2099, 3, 2, 12, 0)



class _Case(unittest.TestCase):
    def setUp(self):
        self.t = make_test_app(csrf=False)
        self.addCleanup(self.t.cleanup)
        self.fake = FakeSportsDB()
        self.addCleanup(self.fake.stop)
        p = patch.object(sports_data, 'BASE_URL', self.fake.base_url)
        p.start()
        self.addCleanup(p.stop)
        self.league_id = sports_data.follow_league(
            {'tsdb_id': LEAGUE_ID, 'name': 'Pretend League', 'sport': 'Pretend Ball', 'season': '2099',
             'terms': ['Pretend League']})
        SportsFetch.query.delete()
        db.session.commit()

    def team(self, tsdb_id=11, name='Pretendville Reds', **kw):
        team = SportsTeam(tsdb_id=tsdb_id, full_name=name, names=json.dumps([name]), **kw)
        db.session.add(team)
        db.session.commit()
        return team

    def stored_game(self, home=11, away=12, start=None, names=('Pretendville Reds', 'Madeup City Blues')):
        start = start or NOW + timedelta(days=1)
        e = SportsEvent(tsdb_id=SportsEvent.query.count() + 100, league_id=self.league_id,
                        name=f'{names[0]} vs {names[1]}', home_team_id=home, away_team_id=away,
                        home_name=names[0], away_name=names[1], start_at=start, local_date=start.date())
        db.session.add(e)
        db.session.commit()
        return e

    def ask(self, target, at):
        row = SportsFetch.query.filter_by(target=target).first()
        if row is None:
            row = SportsFetch(target=target, kind=SPORTS_KIND_TEAM, league_id=self.league_id)
            db.session.add(row)
        row.next_due_at = at - timedelta(minutes=1)
        state = db.session.get(SportsState, 1)
        if state is not None:
            state.next_call_at = state.last_call_at = None
        db.session.commit()
        out = sports_data.tick(now=at)
        db.session.expire_all()
        return out


class RereadTests(_Case):
    def test_a_looked_up_team_is_asked_again_in_a_week_and_takes_the_new_name(self):
        """UTEP's `Texas-El Paso` could be added on TheSportsDB and still not reach the matcher until
        next season, since a team was read once."""
        self.stored_game()
        self.team()
        self.fake.teams[11] = team_row(11, 'Pretendville Reds', 'Reds')
        self.ask('team:11', NOW)
        row = SportsFetch.query.filter_by(target='team:11').one()
        self.assertEqual(row.next_due_at, NOW + timedelta(days=7))
        self.fake.teams[11] = team_row(11, 'Pretendville Reds', 'Reds,Pretendville-North')
        later = NOW + timedelta(days=7, minutes=5)
        self.assertEqual(sports_data.tick(now=later), 'ok')
        db.session.expire_all()
        self.assertIn('Pretendville-North', json.loads(SportsTeam.query.filter_by(tsdb_id=11).one().names))

    def test_a_team_with_no_stored_game_is_not_asked_again(self):
        self.team()
        self.fake.teams[11] = team_row(11, 'Pretendville Reds', 'Reds')
        self.ask('team:11', NOW)
        self.assertIsNone(SportsFetch.query.filter_by(target='team:11').one().next_due_at)

    def test_a_lapsed_team_is_asked_again_when_a_game_of_its_arrives(self):
        self.team(last_looked_up=NOW - timedelta(days=30))
        db.session.add(SportsFetch(target='team:11', kind=SPORTS_KIND_TEAM, league_id=self.league_id,
                                   last_at=NOW - timedelta(days=30)))
        db.session.commit()
        league = db.session.get(SportsLeague, self.league_id)
        sports_data._ensure_team_rows(league, [{'home_team_id': 11, 'home_name': 'Pretendville Reds',
                                                'away_team_id': None, 'away_name': None}], NOW)
        db.session.commit()
        self.assertEqual(SportsFetch.query.filter_by(target='team:11').one().next_due_at, NOW)

    def test_a_new_season_does_not_drop_the_teams_to_their_full_names(self):
        """With weekly re-reads a looked-up team's row is due a week out, so clearing its lookup at a
        season change would leave the whole league matched on full names until each turn came."""
        from tests.support.fake_sportsdb import league_row
        self.stored_game()
        self.team(last_looked_up=NOW, city='Pretendville')
        self.fake.leagues[LEAGUE_ID] = league_row(season='2100')
        db.session.add(SportsFetch(target=f'league:{LEAGUE_ID}', kind='league', league_id=self.league_id))
        db.session.commit()
        self.assertEqual(self.ask(f'league:{LEAGUE_ID}', NOW + timedelta(hours=1)), 'ok')
        self.assertEqual(db.session.get(SportsLeague, self.league_id).current_season, '2100')
        self.assertIsNotNone(SportsTeam.query.filter_by(tsdb_id=11).one().last_looked_up)

    def test_a_reread_keeps_the_names_a_person_added(self):
        self.stored_game()
        self.team(own_names=json.dumps(['Pretendville-El Norte']))
        self.fake.teams[11] = team_row(11, 'Pretendville Reds', 'Reds')
        self.ask('team:11', NOW)
        team = SportsTeam.query.filter_by(tsdb_id=11).one()
        self.assertEqual(json.loads(team.own_names), ['Pretendville-El Norte'])
        self.assertIn('Reds', json.loads(team.names))


class OwnNameTests(_Case):
    def listing(self, sub_title, start):
        ch = make_channel(make_account(), name='Local 4')
        make_epg_entry(ch, title='Pretend League Ball', sub_title=sub_title, start_time=start, duration_minutes=180)
        db.session.commit()
        return ch

    def test_a_listing_spelled_the_persons_way_matches_and_did_not_before(self):
        self.team(11, 'PVR', last_looked_up=NOW)
        self.team(12, 'Madeup City Blues', last_looked_up=NOW)
        start = NOW + timedelta(hours=6)
        event = self.stored_game(names=('PVR', 'Madeup City Blues'), start=start)
        ch = self.listing('Madeup City Blues at Pretendville-El Norte', start)
        sports_match.run(now=NOW, pause=0)
        self.assertEqual(SportsMatch.query.filter_by(event_id=event.id).count(), 0)
        with patch('app.scheduler.queue_sports_match') as queued:
            self.assertEqual(sports_match.set_team_names(11, ['  Pretendville-El  Norte ', 'pretendville-el norte', '']),
                             ['Pretendville-El Norte'])
        queued.assert_called_once()
        sports_match.run(now=NOW, pause=0)
        db.session.expire_all()
        (m,) = SportsMatch.query.filter_by(event_id=event.id, kind=SPORTS_MATCH_KIND_GUIDE).all()
        self.assertEqual(m.channel_id, ch.id)

    def test_a_name_counts_before_the_teams_lookup_has_run(self):
        self.team(11, 'PVR')
        self.team(12, 'Madeup City Blues')
        start = NOW + timedelta(hours=6)
        event = self.stored_game(names=('PVR', 'Madeup City Blues'), start=start)
        self.listing('Madeup City Blues at Pretendville-El Norte', start)
        sports_match.set_team_names(11, ['Pretendville-El Norte'])
        sports_match.run(now=NOW, pause=0)
        self.assertEqual(SportsMatch.query.filter_by(event_id=event.id).count(), 1)

    def test_the_route_saves_validates_and_answers_in_the_envelope(self):
        self.team()
        r = self.t.client.put('/api/sports/teams/11/names', json={'names': ['Pretendville-El Norte']})
        self.assertEqual((r.status_code, r.get_json()), (200, {'success': True, 'names': ['Pretendville-El Norte']}))
        self.assertEqual(self.t.client.put('/api/sports/teams/11/names', json={'names': 'x'}).status_code, 400)
        self.assertEqual(self.t.client.put('/api/sports/teams/11/names', json={'names': [{'a': 1}]}).status_code, 400)
        self.assertEqual(self.t.client.put('/api/sports/teams/999/names', json={'names': []}).status_code, 404)
        r = self.t.client.put('/api/sports/teams/11/names', json={'names': [f'Name {i}' for i in range(21)]})
        self.assertEqual(r.status_code, 400)
        self.assertIn('at most 20', r.get_json()['error'])
        r = self.t.client.put('/api/sports/teams/11/names', json={'names': []})
        self.assertEqual(r.get_json()['names'], [])

    def test_the_league_page_lists_a_teams_added_names_and_offers_every_team(self):
        self.stored_game()
        self.team(11, 'Pretendville Reds', own_names=json.dumps(['Pretendville-El Norte']))
        self.team(12, 'Madeup City Blues')
        page = self.t.client.get(f'/sports/{self.league_id}').get_data(as_text=True)
        card = page[page.index('id="sp-matching"'):]
        self.assertIn('Other names for a team', card)
        self.assertIn('data-act="remove-team-name" data-team="11" data-term="Pretendville-El Norte"', card)
        self.assertIn('data-act="team-name"', card)
        teams = sports_match.team_names_context(self.league_id)
        self.assertEqual([(t['id'], t['own']) for t in teams],
                         [(12, []), (11, ['Pretendville-El Norte'])])

    def test_a_league_with_no_teams_has_no_team_names_section(self):
        page = self.t.client.get(f'/sports/{self.league_id}').get_data(as_text=True)
        self.assertNotIn('Other names for a team', page)


if __name__ == '__main__':
    unittest.main()
