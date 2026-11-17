"""A team named only inside another team's longer name is not named (app/sports_match.py
::_standing_spans, dev/docs/DESIGN-sports-data.md 6.2, dev/changelog/1578).

College schools are stored by bare name, `Texas` beside `Texas Tech`, so the shorter one reads inside
the longer. Made-up schools here: nothing received from TheSportsDB ships. Throwaway temp SQLite DB
- never the live dvr.db.
  python3 -m unittest tests.test_sports_longer_names
"""
import json
import os
import sys
import unittest
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tests.support.app import make_test_app  # noqa: E402
from tests.support.seed import make_account, make_channel, make_epg_entry  # noqa: E402

from app import db, sports_match  # noqa: E402
from app.database import (  # noqa: E402
    SPORTS_MATCH_KIND_CHANNEL_NAME, SPORTS_MATCH_KIND_GUIDE, SportsEvent, SportsLeague, SportsMatch, SportsTeam,
)

NOW = datetime(2099, 11, 7, 12, 0)
KICKOFF = datetime(2099, 11, 8, 18, 0)
TEAMS = {1: 'Ashford', 2: 'Ashford Tech', 3: 'Brell', 4: 'Brell State', 5: 'Dunmore', 6: 'North Corvin',
         7: 'North Corvin State'}


class _Case(unittest.TestCase):
    def setUp(self):
        self.t = make_test_app(csrf=False)
        self.addCleanup(self.t.cleanup)
        self.account = make_account()
        self.league = SportsLeague(tsdb_id=9001, name='Pretend College Football', sport='American Football',
                                   match_terms=json.dumps(['Pretend College Football', 'PCF']))
        db.session.add(self.league)
        for tid, name in TEAMS.items():
            db.session.add(SportsTeam(tsdb_id=tid, full_name=name, names=json.dumps([name]), last_looked_up=NOW))
        db.session.commit()

    def game(self, home, away, start=KICKOFF):
        event = SportsEvent(tsdb_id=SportsEvent.query.count() + 100, league_id=self.league.id,
                            name=f'{TEAMS[home]} vs {TEAMS[away]}', home_team_id=home, away_team_id=away,
                            home_name=TEAMS[home], away_name=TEAMS[away], start_at=start, local_date=start.date())
        db.session.add(event)
        db.session.commit()
        return event

    def row(self, sub_title, start=KICKOFF):
        ch = make_channel(self.account, name=f'Local {sub_title} {start}')
        make_epg_entry(ch, title='PCF Football', sub_title=sub_title, start_time=start, duration_minutes=180)
        db.session.commit()
        return ch

    def run_match(self):
        sports_match.run(now=NOW, pause=0)
        db.session.expire_all()

    def channels(self, event, kind):
        return {m.channel_id for m in SportsMatch.query.filter_by(event_id=event.id, kind=kind).all()}


class GuideRowTests(_Case):
    """dev/docs/BUGS.md 2026-11-16: `Texas Tech at Kansas State` was a strong match to Texas at
    Kansas as well."""

    def test_a_listing_of_the_longer_named_game_is_not_the_shorter_named_game(self):
        short = self.game(3, 1)
        long_ = self.game(4, 2, start=KICKOFF + timedelta(hours=4))
        theirs = self.row('Ashford Tech at Brell State', start=KICKOFF + timedelta(hours=4))
        mine = self.row('Ashford at Brell')
        self.run_match()
        self.assertEqual(self.channels(short, SPORTS_MATCH_KIND_GUIDE), {mine.id})
        self.assertEqual(self.channels(long_, SPORTS_MATCH_KIND_GUIDE), {theirs.id})

    def test_the_longer_name_counts_when_the_shorter_school_is_not_in_this_runs_games(self):
        """The longer name is ruled out by every stored team of the league, not only the teams of
        the games being matched: Ashford Tech's game here is long past the run's window."""
        short = self.game(3, 1)
        self.game(4, 2, start=KICKOFF + timedelta(days=40))
        self.row('Ashford Tech at Brell State')
        self.run_match()
        self.assertEqual(self.channels(short, SPORTS_MATCH_KIND_GUIDE), set())

    def test_a_school_named_beside_a_longer_one_is_still_named(self):
        game = self.game(1, 2)
        ch = self.row('Ashford Tech at Ashford')
        self.run_match()
        self.assertEqual(self.channels(game, SPORTS_MATCH_KIND_GUIDE), {ch.id})


class ChannelNameTests(_Case):
    def name(self, text):
        ch = make_channel(self.account, name=text)
        db.session.commit()
        return ch

    def test_a_name_with_no_date_is_not_the_shorter_named_game(self):
        """`Duke vs. North Carolina State` faced off as Duke and North Carolina."""
        short = self.game(6, 5)
        self.game(7, 3, start=KICKOFF + timedelta(days=40))
        self.name('PCF 12: Dunmore vs. North Corvin State')
        self.run_match()
        self.assertEqual(self.channels(short, SPORTS_MATCH_KIND_CHANNEL_NAME), set())

    def test_with_both_games_stored_the_name_names_one_and_is_kept(self):
        """It yielded two pairs, and a name naming two games is dropped, so the real game lost its
        channel."""
        short = self.game(6, 5)
        long_ = self.game(7, 5, start=KICKOFF + timedelta(days=2))
        ch = self.name('PCF 12: Dunmore vs. North Corvin State')
        self.run_match()
        self.assertEqual(self.channels(short, SPORTS_MATCH_KIND_CHANNEL_NAME), set())
        self.assertEqual(self.channels(long_, SPORTS_MATCH_KIND_CHANNEL_NAME), {ch.id})


if __name__ == '__main__':
    unittest.main()
