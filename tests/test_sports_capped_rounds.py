"""A round answer cut at 50 rows, and the games past the cut (app/sports_data.py, app/sports_match.py,
app/sports_view.py, dev/changelog/1549).

The free key's eventsround sends at most 50 rows with nothing saying it was cut, and which 50 changes
from call to call (measured 2026-11-14 on NCAA Division 1 Football). Prod marked three real games
missing because a later answer carried a different 50. Made-up leagues and teams, served by
tests/support/fake_sportsdb.py over real HTTP; nothing received from TheSportsDB ships. Throwaway temp
SQLite DB - never the live dvr.db.
  python3 -m unittest tests.test_sports_capped_rounds
"""
import json
import os
import sys
import unittest
import zlib
from datetime import datetime, timedelta
from unittest.mock import patch
from zoneinfo import ZoneInfo

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tests.support import seed  # noqa: E402
from tests.support.app import make_test_app  # noqa: E402
from tests.support.fake_sportsdb import LEAGUE_ID, FakeSportsDB, game  # noqa: E402

from app import db, sports_data, sports_match, sports_view  # noqa: E402
from app.database import (  # noqa: E402
    SPORTS_FETCH_CAPPED, SPORTS_FETCH_CAPPED_PAST, SPORTS_FETCH_EMPTY, SPORTS_FETCH_OK, SPORTS_KIND_ROUND,
    SPORTS_KIND_SEARCH,
    SportsEvent, SportsFetch, SportsFollow, SportsLeague, SportsLeagueCategory, SportsState, SportsTeam,
)

NOW = datetime(2099, 3, 2, 12, 0)
ROUND = f'round:{LEAGUE_ID}:2099:9'
TEAMS = [f'Team {chr(65 + i)}' for i in range(12)]


def iso(dt):
    return dt.strftime('%Y-%m-%dT%H:%M:%S')


def week(count=60, first=NOW + timedelta(days=1)):
    """`count` games of one round, ten minutes apart, over twelve teams: more than one call carries."""
    return [game(1000 + n, TEAMS[n % 12], TEAMS[(n + 5) % 12], iso(first + timedelta(minutes=10 * n)))
            for n in range(count)]


class _Case(unittest.TestCase):
    def setUp(self):
        self.t = make_test_app(csrf=False)
        self.addCleanup(self.t.cleanup)
        self.fake = FakeSportsDB()
        self.addCleanup(self.fake.stop)
        patcher = patch.object(sports_data, 'BASE_URL', self.fake.base_url)
        patcher.start()
        self.addCleanup(patcher.stop)

    def follow(self):
        payload = {'tsdb_id': LEAGUE_ID, 'name': 'Pretend League', 'sport': 'Pretend Ball', 'season': '2099',
                   'terms': ['Pretend League']}
        return sports_data.follow_league(payload, None)

    def drain(self, start=NOW, limit=200):
        now = start
        for _ in range(limit):
            db.session.expire_all()
            if sports_data.tick(now=now) == 'idle':
                return now
            now += timedelta(seconds=120)
        self.fail('the worker never ran out of due rows')

    def fetch(self, target):
        db.session.expire_all()
        return SportsFetch.query.filter_by(target=target).first()

    def ask_round_again(self, at):
        row = self.fetch(ROUND)
        row.next_due_at = at - timedelta(days=1)
        state = db.session.get(SportsState, 1)
        state.next_call_at = state.last_call_at = None
        db.session.commit()
        return sports_data.tick(now=at)

    def event(self, tsdb_id):
        db.session.expire_all()
        return SportsEvent.query.filter_by(tsdb_id=tsdb_id).first()


class CutRoundTests(_Case):
    """dev/docs/BUGS.md 2026-11-14 07:57 AM: a cut round answer marked stored games missing."""

    def setUp(self):
        super().setUp()
        self.fake.next_event[LEAGUE_ID] = week(1)
        self.fake.rounds[(LEAGUE_ID, '2099', 9)] = week()
        # The first call gets the first 50, every later one the last 50, as the real answer's 50 change.
        self.fake.round_pick = lambda rows, n: rows if n == 1 else rows[10:]
        self.follow()
        self.done = self.drain()

    def test_fifty_rows_is_named_cut_not_ok(self):
        row = self.fetch(ROUND)
        self.assertEqual(row.outcome, SPORTS_FETCH_CAPPED)
        self.assertIn('50 rows', row.reason)
        self.assertIn('not marked missing', row.reason)
        self.assertEqual(SportsEvent.query.count(), 50)

    def test_a_game_left_out_of_a_cut_answer_is_not_marked_missing(self):
        later = self.done + timedelta(hours=6)
        self.ask_round_again(later)
        db.session.expire_all()
        self.assertEqual(SportsEvent.query.count(), 60)
        self.assertEqual(SportsEvent.query.filter(SportsEvent.missing_since.isnot(None)).count(), 0)

    def test_a_mark_from_an_earlier_answer_is_cleared_by_a_cut_one(self):
        """Prod: three games marked missing by a cut answer stay marked until something clears them."""
        e = self.event(1000)
        e.missing_since = self.done
        db.session.commit()
        self.ask_round_again(self.done + timedelta(hours=6))
        self.assertIsNone(self.event(1000).missing_since)

    def test_left_out_games_starting_soon_are_each_re_read_by_number(self):
        self.ask_round_again(self.done + timedelta(hours=6))
        for n in range(10):
            row = self.fetch(f'event:{1000 + n}')
            self.assertIsNotNone(row, n)
            self.assertIsNotNone(row.next_due_at)
        self.assertIsNone(self.fetch('event:1010'))

    def test_a_left_out_game_far_off_is_not_re_read(self):
        e = self.event(1000)
        e.start_at = self.done + timedelta(days=sports_data.EVENT_RECHECK_DAYS + 3)
        db.session.commit()
        self.ask_round_again(self.done + timedelta(hours=6))
        self.assertIsNone(self.fetch('event:1000'))
        self.assertIsNotNone(self.fetch('event:1001'))

    def test_a_re_read_brings_in_a_moved_time(self):
        """Prod: a game left out of every later answer kept its first time forever, 23 hours from
        the real one, so the guide's listing of it could never match."""
        moved = NOW + timedelta(days=2, hours=3)
        self.fake.rounds[(LEAGUE_ID, '2099', 9)][0]['strTimestamp'] = iso(moved)
        later = self.done + timedelta(hours=6)
        self.ask_round_again(later)
        self.drain(later + timedelta(minutes=5))
        self.assertEqual(self.event(1000).start_at, moved)
        self.assertTrue(any(c[1].get('id') == '1000' for c in self.fake.calls_to('lookupevent')))
        self.assertEqual(self.fetch('event:1000').outcome, SPORTS_FETCH_OK)

    def test_a_re_read_that_finds_nothing_marks_the_game_missing(self):
        self.fake.answer_overrides['lookupevent'] = {'events': None}
        later = self.done + timedelta(hours=6)
        self.ask_round_again(later)
        self.drain(later + timedelta(minutes=5))
        self.assertIsNotNone(self.event(1000).missing_since)
        row = self.fetch('event:1000')
        self.assertEqual(row.outcome, SPORTS_FETCH_EMPTY)
        self.assertIn('kept for 7 days', row.reason)

    def test_the_league_page_names_the_cut_and_what_was_done(self):
        later = self.done + timedelta(hours=6)
        self.ask_round_again(later)
        league = SportsLeague.query.filter_by(tsdb_id=LEAGUE_ID).first()
        text = self.t.client.get(f'/sports/{league.id}').get_data(as_text=True)
        self.assertIn('Cut at 50', text)
        self.assertIn('TheSportsDB sends at most 50 games for one call', text)
        self.assertIn('10 more lookups waiting', text)
        # Lookups about one game are not the schedule, so they never read as still getting it.
        self.assertNotIn('Getting schedule', text)


class SearchTests(_Case):
    def setUp(self):
        super().setUp()
        self.follow()
        self.league = SportsLeague.query.filter_by(tsdb_id=LEAGUE_ID).first()
        for tid, name in ((136967, 'Tulsa'), (136888, 'Florida Atlantic')):
            db.session.add(SportsTeam(tsdb_id=tid, full_name=name, names=json.dumps([name]), last_looked_up=NOW))
        db.session.commit()
        self.drain()

    def test_a_game_past_the_cut_is_found_by_name_home_team_first(self):
        self.fake.games[4242] = game(4242, 'Tulsa', 'Florida Atlantic', iso(NOW + timedelta(days=1)),
                                     home_id=136967, away_id=136888, round_no=11)
        day = (NOW + timedelta(days=1)).date().isoformat()
        self.assertEqual(sports_data.queue_guide_lookups(self.league.id, [(day, 136967, 136888)], NOW), 1)
        self.drain(NOW + timedelta(hours=1))
        (call,) = self.fake.calls_to('searchevents')
        self.assertEqual(call[1], {'e': 'Tulsa_vs_Florida_Atlantic', 'd': day})
        event = self.event(4242)
        self.assertEqual((event.home_name, event.away_name, event.round), ('Tulsa', 'Florida Atlantic', 11))
        row = self.fetch(f'search:{LEAGUE_ID}:{day}:136967:136888')
        self.assertEqual(row.outcome, SPORTS_FETCH_OK)
        self.assertIsNone(row.next_due_at)

    def test_not_found_is_an_answer_and_is_never_asked_again(self):
        sports_data.queue_guide_lookups(self.league.id, [('2099-03-03', 136888, 136967)], NOW)
        self.drain(NOW + timedelta(hours=1))
        row = self.fetch(f'search:{LEAGUE_ID}:2099-03-03:136888:136967')
        self.assertEqual(row.outcome, SPORTS_FETCH_EMPTY)
        self.assertIn('"Florida Atlantic vs Tulsa" that day', row.reason)
        self.assertEqual(sports_data.queue_guide_lookups(self.league.id, [('2099-03-03', 136888, 136967)], NOW), 0)

    def test_two_meetings_in_a_season_are_told_apart_by_day(self):
        """dev/docs/BUGS.md 2026-11-14 08:58 AM: by season, TheSportsDB gave the NBA's Feb 13 game for
        a pair that also met in November, and the pair was then never asked again."""
        tonight = NOW + timedelta(days=1)
        self.fake.games[4243] = game(4243, 'Tulsa', 'Florida Atlantic', iso(tonight),
                                     home_id=136967, away_id=136888, round_no=0)
        self.fake.games[4244] = game(4244, 'Tulsa', 'Florida Atlantic', iso(NOW + timedelta(days=90)),
                                     home_id=136967, away_id=136888, round_no=0)
        sports_data.queue_guide_lookups(self.league.id, [(tonight.date().isoformat(), 136967, 136888)], NOW)
        self.drain(NOW + timedelta(hours=1))
        self.assertIsNotNone(self.event(4243))
        self.assertIsNone(self.event(4244))

    def test_a_row_from_before_lookups_went_by_day_is_still_asked_by_season(self):
        """Characterization: rows dev/changelog/1549 wrote hold a season and stay readable until they prune."""
        db.session.add(SportsFetch(target=f'search:{LEAGUE_ID}:2099:136967:136888', kind=SPORTS_KIND_SEARCH,
                                   league_id=self.league.id, next_due_at=NOW))
        db.session.commit()
        self.drain(NOW + timedelta(hours=1))
        (call,) = self.fake.calls_to('searchevents')
        self.assertEqual(call[1], {'e': 'Tulsa_vs_Florida_Atlantic', 's': '2099'})
        self.assertIn('this season', self.fetch(f'search:{LEAGUE_ID}:2099:136967:136888').reason)

    def test_a_listing_near_midnight_utc_is_asked_on_both_days(self):
        self.assertEqual(sports_data.lookup_days(datetime(2099, 11, 14, 23, 30)), ['2099-11-14', '2099-11-15'])
        self.assertEqual(sports_data.lookup_days(datetime(2099, 11, 15, 0, 0)), ['2099-11-15'])
        self.assertEqual(sports_data.lookup_days(datetime(2099, 11, 14, 20, 0)), ['2099-11-14'])

    def test_one_game_asked_four_ways_counts_as_one_on_the_page(self):
        for day in ('2099-03-03', '2099-03-04'):
            for home, away in ((136967, 136888), (136888, 136967)):
                db.session.add(SportsFetch(target=f'search:{LEAGUE_ID}:{day}:{home}:{away}', kind=SPORTS_KIND_SEARCH,
                                           league_id=self.league.id, last_at=NOW, outcome=SPORTS_FETCH_EMPTY))
        db.session.add(SportsFetch(target=f'search:{LEAGUE_ID}:2099-03-09:136967:136888', kind=SPORTS_KIND_SEARCH,
                                   league_id=self.league.id, last_at=NOW, outcome=SPORTS_FETCH_EMPTY))
        db.session.commit()
        games = sports_view._lookups_by_game(SportsFetch.query.all())
        self.assertEqual(sorted(len(asks) for asks in games.values()), [1, 4])

    def test_another_leagues_game_of_that_name_is_an_answer_not_a_bad_row(self):
        """dev/docs/BUGS.md 2026-11-14 11:28 AM: a basketball listing on a football channel was looked
        up, TheSportsDB found the basketball game, and the league page warned of rows it could not use."""
        day = (NOW + timedelta(days=1)).date().isoformat()
        self.fake.games[5000] = game(5000, 'Tulsa', 'Florida Atlantic', iso(NOW + timedelta(days=1)), league=7777,
                                     strLeague='Pretend Hoops League', home_id=136967, away_id=136888)
        sports_data.queue_guide_lookups(self.league.id, [(day, 136967, 136888)], NOW)
        self.drain(NOW + timedelta(hours=1))
        row = self.fetch(f'search:{LEAGUE_ID}:{day}:136967:136888')
        self.assertEqual(row.outcome, SPORTS_FETCH_EMPTY)
        self.assertEqual(row.dropped_count, 0)
        self.assertIn('only in another league, Pretend Hoops League', row.reason)
        self.assertIsNone(self.event(5000))

    def test_a_lookups_unusable_answer_is_told_where_it_is_shown(self):
        """dev/docs/BUGS.md 2026-11-14 11:28 AM: the warning sent the reader to Schedule updates, which
        never lists a lookup, and the lookup counted as found."""
        fetches = [
            SportsFetch(target=f'round:{LEAGUE_ID}:2099:9', kind=SPORTS_KIND_ROUND, outcome=SPORTS_FETCH_CAPPED,
                        last_at=NOW, row_count=50, dropped_count=0),
            SportsFetch(target=f'search:{LEAGUE_ID}:2099-03-03:136967:136888', kind=SPORTS_KIND_SEARCH,
                        outcome=SPORTS_FETCH_OK, last_at=NOW, row_count=1, dropped_count=1),
        ]
        notes = sports_view._notices(self.league, sports_view.STATUS_OK, fetches, [], 'game', 'games', 0, 7, NOW,
                                     ZoneInfo('UTC'), {}, {})
        self.assertFalse([n for n in notes if 'could not be used' in n[1]], notes)
        (cut,) = [n[2] for n in notes if 'may be missing' in n[1]]
        self.assertIn('looked up 1 game your guide lists by name and found 0', cut)
        self.assertIn('1 lookup came back with an answer ChannelBin could not use', cut)

    def test_a_league_not_followed_queues_nothing(self):
        SportsFollow.query.filter_by(league_id=self.league.id).delete()
        db.session.commit()
        self.assertEqual(sports_data.queue_guide_lookups(self.league.id, [('2099-03-03', 136967, 136888)], NOW), 0)
        self.assertEqual(SportsFetch.query.filter_by(kind=SPORTS_KIND_SEARCH).count(), 0)


class RoundZeroTests(_Case):
    """dev/docs/BUGS.md 2026-11-14 08:58 AM: TheSportsDB files the NBA's whole regular season as round
    0. ChannelBin read 0 as no round, so it never asked for a schedule, and the round's answer is the
    season's first 50 games, all played, every time."""

    def setUp(self):
        super().setUp()
        opening = NOW - timedelta(days=25)
        season = [game(2000 + n, TEAMS[n % 12], TEAMS[(n + 5) % 12], iso(opening + timedelta(hours=3 * n)), round_no=0)
                  for n in range(80)]
        self.fake.rounds[(LEAGUE_ID, '2099', 0)] = season
        self.fake.next_event[LEAGUE_ID] = [game(2999, TEAMS[0], TEAMS[1], iso(NOW + timedelta(hours=8)), round_no=0)]
        self.follow()
        self.drain()

    def test_round_zero_is_asked_for(self):
        row = self.fetch(f'round:{LEAGUE_ID}:2099:0')
        self.assertIsNotNone(row)
        self.assertEqual(self.event(2000).round, 0)
        self.assertNotIn('no season or round', self.fetch(f'next:{LEAGUE_ID}').reason or '')

    def test_a_cut_answer_of_played_games_is_named_for_what_it_is(self):
        row = self.fetch(f'round:{LEAGUE_ID}:2099:0')
        self.assertEqual(row.outcome, SPORTS_FETCH_CAPPED_PAST)
        self.assertIn('already started', row.reason)
        league = SportsLeague.query.filter_by(tsdb_id=LEAGUE_ID).first()
        text = self.t.client.get(f'/sports/{league.id}').get_data(as_text=True)
        self.assertIn('Cut at 50', text)
        self.assertIn('sent only the first 50, all already played', text)


class NamedPairTests(unittest.TestCase):
    """Which two teams a listing names, and in what order."""

    TEAMS = {1: ['florida'], 2: ['florida atlantic', 'owls'], 3: ['tulsa'], 4: ['byu'], 5: ['baylor'],
             6: ['miami'], 7: ['miami'], 8: ['ohio'], 9: ['texas'], 10: ['texas a and m'], 11: ['tennessee'],
             12: ['wyoming']}

    def pair(self, *parts):
        teams = {tid: sports_match.TeamText(names) for tid, names in self.TEAMS.items()}
        return sports_match._named_pair(parts, teams)

    def test_a_team_inside_a_longer_name_is_not_named(self):
        self.assertEqual(self.pair('NCAAF 60: 3:30pm Florida Atlantic at Tulsa'), (2, 3, 'at'))

    def test_rankings_stand_between_nothing(self):
        self.assertEqual(self.pair('#7 BYU vs. #23 Baylor Nov 12 8:00PM ET'), (4, 5, 'vs.'))

    def test_the_teams_can_be_in_the_episode_name(self):
        self.assertEqual(self.pair('College Football ᴸᶦᵛᵉ', 'Baylor at BYU'), (5, 4, 'at'))

    def test_one_team_or_a_schedule_of_several_is_not_a_game(self):
        self.assertIsNone(self.pair('BYU Sports Nation'))
        self.assertIsNone(self.pair('Baylor at BYU and Tulsa at Florida'))

    def test_two_teams_without_a_joiner_are_not_a_game(self):
        self.assertIsNone(self.pair('Baylor BYU Countdown'))

    def test_a_name_two_teams_share_is_left_alone(self):
        self.assertIsNone(self.pair('Miami at Ohio'))

    def test_an_ampersand_reads_as_and(self):
        """dev/docs/BUGS.md 2026-11-14 11:28 AM: `Tennessee vs. Texas A&M` was looked up as Texas."""
        self.assertEqual(self.pair('#8 Tennessee vs. Texas A&M (SkyCast)'), (11, 10, 'vs.'))

    def test_a_word_hyphened_onto_the_second_team_makes_it_another(self):
        """dev/docs/BUGS.md 2026-11-14 11:28 AM: `Wyoming at Texas-El Paso` was looked up as Texas."""
        self.assertIsNone(self.pair('College Football', 'Wyoming at Texas-El Paso'))
        self.assertEqual(self.pair('Wyoming at Texas - Live'), (12, 9, 'at'))
        self.assertEqual(self.pair('Baylor-BYU'), (5, 4, '-'))


KICKOFF = datetime(2099, 11, 8, 18, 0)
MATCH_NOW = datetime(2099, 11, 8, 12, 0)


class MatcherLookupTests(unittest.TestCase):
    """The matcher queues a lookup by name for a listing of a game no answer carried."""

    def setUp(self):
        self.t = make_test_app(csrf=False)
        self.addCleanup(self.t.cleanup)
        self.account = seed.make_account()
        self.league = SportsLeague(tsdb_id=9001, name='Pretend Football League',
                                   match_terms=json.dumps(['Pretend Football League', 'PFL']))
        db.session.add(self.league)
        db.session.commit()
        db.session.add(SportsFollow(league_id=self.league.id))
        self.names = {1: 'Harborview Gulls', 2: 'Ironvale Bears', 3: 'Northgate Panthers', 4: 'Saint-Rémy Comets'}
        for tid, full in self.names.items():
            db.session.add(SportsTeam(tsdb_id=tid, full_name=full, names=json.dumps([full]), last_looked_up=MATCH_NOW))
        # Two stored games of round 10 put Northgate and Saint-Rémy among the league's teams.
        self.game(1, 2, KICKOFF, 100)
        self.game(3, 1, KICKOFF + timedelta(days=1), 101)
        self.game(2, 4, KICKOFF + timedelta(days=3), 102)
        self.capped = SportsFetch(target='round:9001:2099:10', kind=SPORTS_KIND_ROUND, league_id=self.league.id,
                                  outcome=SPORTS_FETCH_CAPPED, last_at=MATCH_NOW, row_count=50)
        db.session.add(self.capped)
        db.session.commit()

    def game(self, home, away, start, tsdb_id, round_no=10):
        db.session.add(SportsEvent(tsdb_id=tsdb_id, league_id=self.league.id, season='2099', round=round_no,
                                   name=f'{self.names[home]} vs {self.names[away]}', home_team_id=home,
                                   away_team_id=away, home_name=self.names[home], away_name=self.names[away],
                                   start_at=start, local_date=start.date()))

    def listing(self, title, sub_title=None, start=KICKOFF + timedelta(hours=2)):
        channel = seed.make_channel(self.account)
        seed.make_epg_entry(channel, title=title, sub_title=sub_title, start_time=start, duration_minutes=180)
        db.session.commit()

    def searches(self):
        db.session.expire_all()
        return sorted(f.target for f in SportsFetch.query.filter_by(kind=SPORTS_KIND_SEARCH).all())

    def test_an_away_at_home_listing_of_an_unstored_game_is_looked_up_home_first(self):
        self.listing('PFL Football ᴸᶦᵛᵉ', 'Saint-Rémy Comets at Northgate Panthers')
        note = sports_match.run(now=MATCH_NOW, pause=0)
        self.assertEqual(self.searches(), ['search:9001:2099-11-08:3:4'])
        self.assertIn('Queued 1 lookup by name', note)

    def test_a_listing_that_does_not_say_who_is_home_is_looked_up_both_ways(self):
        self.listing('PFL: Northgate Panthers vs. Saint-Rémy Comets')
        sports_match.run(now=MATCH_NOW, pause=0)
        self.assertEqual(self.searches(), ['search:9001:2099-11-08:3:4', 'search:9001:2099-11-08:4:3'])

    def test_a_listing_of_a_stored_game_is_not_looked_up(self):
        self.listing('PFL Football', 'Harborview Gulls at Ironvale Bears', start=KICKOFF)
        sports_match.run(now=MATCH_NOW, pause=0)
        self.assertEqual(self.searches(), [])

    def test_nothing_is_looked_up_when_the_week_was_not_cut(self):
        self.capped.outcome = SPORTS_FETCH_OK
        db.session.commit()
        self.listing('PFL Football', 'Saint-Rémy Comets at Northgate Panthers')
        sports_match.run(now=MATCH_NOW, pause=0)
        self.assertEqual(self.searches(), [])

    def test_a_listing_outside_the_cut_week_is_not_looked_up(self):
        self.listing('PFL Football', 'Saint-Rémy Comets at Northgate Panthers', start=KICKOFF + timedelta(days=5))
        sports_match.run(now=MATCH_NOW, pause=0)
        self.assertEqual(self.searches(), [])

    def test_a_round_cut_with_every_game_played_looks_up_the_whole_guide(self):
        """dev/docs/BUGS.md 2026-11-14 08:58 AM: the NBA's stored games were all weeks old, so no
        listing fell near one and nothing was ever looked up."""
        for event in SportsEvent.query.all():
            event.start_at -= timedelta(days=30)
            event.local_date = event.start_at.date()
        self.capped.outcome = SPORTS_FETCH_CAPPED_PAST
        db.session.commit()
        later = KICKOFF + timedelta(days=5)
        self.listing('PFL Football', 'Saint-Rémy Comets at Northgate Panthers', start=later)
        sports_match.run(now=MATCH_NOW, pause=0)
        self.assertEqual(self.searches(), [f'search:9001:{later.date().isoformat()}:3:4'])

    def tie_category(self, name='Pretend Football Package'):
        self.league.sport = 'American Football'
        db.session.add(SportsLeagueCategory(league_id=self.league.id, category_name=name))
        db.session.commit()
        return seed.make_channel(self.account, category_name=name)

    def test_another_sport_on_the_leagues_channels_is_not_looked_up(self):
        """dev/docs/BUGS.md 2026-11-14 11:28 AM: prod's "College Football Package" carries college
        basketball, volleyball and soccer, and each of their listings was looked up as football."""
        channel = self.tie_category()
        for title in ('College Basketball', "Women's College Volleyball", 'Soccer'):
            seed.make_epg_entry(channel, title=title, sub_title='Saint-Rémy Comets at Northgate Panthers',
                                start_time=KICKOFF + timedelta(hours=2), duration_minutes=180)
        db.session.commit()
        sports_match.run(now=MATCH_NOW, pause=0)
        self.assertEqual(self.searches(), [])

    def test_the_leagues_own_sport_on_its_channels_is_looked_up(self):
        channel = self.tie_category()
        seed.make_epg_entry(channel, title='College Football', sub_title='Saint-Rémy Comets at Northgate Panthers',
                            start_time=KICKOFF + timedelta(hours=2), duration_minutes=180)
        db.session.commit()
        sports_match.run(now=MATCH_NOW, pause=0)
        self.assertEqual(self.searches(), ['search:9001:2099-11-08:3:4'])

    def test_a_replay_is_not_looked_up(self):
        """dev/docs/BUGS.md 2026-11-14 11:28 AM: `Full Game Replay` listings were looked up on the day
        they re-aired."""
        self.listing('PFL Full Game Replay: Saint-Rémy Comets at Northgate Panthers')
        sports_match.run(now=MATCH_NOW, pause=0)
        self.assertEqual(self.searches(), [])

    def test_an_earlier_seasons_game_is_not_looked_up(self):
        """dev/docs/BUGS.md 2026-11-14 11:28 AM: `2022 Rose Bowl Ohio State vs Utah` was looked up."""
        self.league.current_season = '2099'
        db.session.commit()
        self.listing('PFL : 2097 Bowl Saint-Rémy Comets at Northgate Panthers')
        self.listing('PFL 2099 : Saint-Rémy Comets at Northgate Panthers', start=KICKOFF + timedelta(hours=26))
        sports_match.run(now=MATCH_NOW, pause=0)
        self.assertEqual(self.searches(), ['search:9001:2099-11-09:3:4'])

    def test_a_recording_with_no_event_in_a_cut_week_says_it_may_not_have_been_sent(self):
        """Prod read "no event stored" as if the game did not exist on TheSportsDB."""
        rec = seed.make_recording(program_start_time=KICKOFF + timedelta(days=2),
                                  program_stop_time=KICKOFF + timedelta(days=2, hours=3),
                                  program_title='PFL Football', start_time=KICKOFF, stop_time=KICKOFF)
        db.session.commit()
        note = sports_view._unlinked_note(rec)
        self.assertIn('No Pretend Football League event within 13 hours', note)
        self.assertIn('cut at 50', note)
        self.capped.outcome = SPORTS_FETCH_OK
        db.session.commit()
        self.assertNotIn('cut at 50', sports_view._unlinked_note(rec))


if __name__ == '__main__':
    unittest.main()


def tid(name):
    """The team id tests/support/fake_sportsdb.py::game gives a team by name."""
    return zlib.crc32(name.encode()) % 100000 + 1


FIRST = NOW + timedelta(days=1)


class TeamNextTests(_Case):
    """dev/changelog/1553: a cut round leaves games out, and each team with no game in the answer is
    asked for its next one, which eventsnext sends home or away, so no game past the cut stays unknown.
    Team M and Team N play only in the round's 61st row, and are known from an earlier round."""

    def setUp(self):
        super().setUp()
        self.late = FIRST + timedelta(minutes=600)
        self.fake.next_event[LEAGUE_ID] = week(1)
        self.fake.rounds[(LEAGUE_ID, '2099', 9)] = week() + [game(1060, 'Team M', 'Team N', iso(self.late))]
        self.follow()
        self.league = SportsLeague.query.filter_by(tsdb_id=LEAGUE_ID).first()
        past = NOW - timedelta(days=6)
        db.session.add(SportsEvent(tsdb_id=900, league_id=self.league.id, season='2099', round=8, name='Team M vs Team N',
                                   home_team_id=tid('Team M'), away_team_id=tid('Team N'), home_name='Team M',
                                   away_name='Team N', start_at=past, local_date=past.date()))
        db.session.commit()

    def asked(self):
        return sorted(int(c[1]['id']) for c in self.fake.calls_to('eventsnext'))

    def due_now(self, target, at):
        row = self.fetch(target)
        row.next_due_at = at
        state = db.session.get(SportsState, 1)
        state.next_call_at = state.last_call_at = None
        db.session.commit()

    def test_a_team_with_no_game_in_a_cut_answer_is_asked_and_its_game_found(self):
        self.drain()
        self.assertEqual(self.fetch(ROUND).outcome, SPORTS_FETCH_CAPPED)
        self.assertEqual(self.asked(), sorted([tid('Team M'), tid('Team N')]))
        self.assertEqual(self.event(1060).start_at, self.late)
        rows = [self.fetch(f'teamnext:{LEAGUE_ID}:{tid(t)}') for t in ('Team M', 'Team N')]
        self.assertEqual({r.outcome for r in rows}, {SPORTS_FETCH_OK})
        # Whichever team is asked second gets the same game, by then stored.
        self.assertEqual(sorted(r.reason for r in rows),
                         ['Found Team M vs Team N, which the schedule had left out.',
                          'Its next game, Team M vs Team N, was already stored.'])

    def test_a_team_is_asked_again_once_its_game_is_over_and_the_next_is_found(self):
        self.fake.games[1100] = game(1100, 'Team A', 'Team M', iso(FIRST + timedelta(days=7)), round_no=10)
        self.drain()
        target = f'teamnext:{LEAGUE_ID}:{tid("Team M")}'
        again = self.fetch(target).next_due_at
        self.assertEqual(again, self.late + sports_data.TEAM_NEXT_AFTER_START)
        self.assertIsNone(self.event(1100))
        self.fake.clock = iso(again)
        self.drain(again)
        self.assertEqual(self.event(1100).round, 10)
        self.assertEqual(self.fetch(target).next_due_at, FIRST + timedelta(days=7) + sports_data.TEAM_NEXT_AFTER_START)

    def test_only_followed_teams_are_asked(self):
        sports_data.set_following(self.league.id, [tid('Team M')])
        self.drain()
        self.assertEqual(self.asked(), [tid('Team M')])

    def test_a_next_game_in_another_league_is_an_answer_not_a_bad_row(self):
        early = FIRST + timedelta(minutes=60)
        self.fake.games[1061] = game(1061, 'Team N', 'Team Z', iso(early), league=7777, strLeague='Pretend Hoops League')
        self.drain()
        row = self.fetch(f'teamnext:{LEAGUE_ID}:{tid("Team N")}')
        self.assertEqual(row.outcome, SPORTS_FETCH_EMPTY)
        self.assertEqual(row.dropped_count, 0)
        self.assertIn('another league, Pretend Hoops League', row.reason)
        self.assertIsNone(self.event(1061))
        self.assertEqual(row.next_due_at, early + sports_data.TEAM_NEXT_AFTER_START)

    def test_a_team_is_not_asked_again_once_no_round_is_cut(self):
        self.drain()
        target = f'teamnext:{LEAGUE_ID}:{tid("Team M")}'
        self.fetch(ROUND).outcome = SPORTS_FETCH_OK
        for row in SportsFetch.query.all():
            row.next_due_at = None
        db.session.commit()
        at = self.late + timedelta(hours=5)
        self.due_now(target, at)
        sports_data.tick(now=at)
        self.assertIsNone(self.fetch(target).next_due_at)

    def test_the_league_page_says_teams_were_asked(self):
        self.drain()
        text = self.t.client.get(f'/sports/{self.league.id}').get_data(as_text=True)
        self.assertIn('asked 2 teams for its next game, and 2 named one in this league', text)


class CappedPastTeamTests(_Case):
    """dev/changelog/1553: when a round runs on past the cut with every game sent already played, as the
    NBA's season-long round 0 does, every team with no game still to come is asked for its next one."""

    def setUp(self):
        super().setUp()
        opening = NOW - timedelta(days=25)
        self.fake.rounds[(LEAGUE_ID, '2099', 0)] = [
            game(2000 + n, TEAMS[n % 12], TEAMS[(n + 5) % 12], iso(opening + timedelta(hours=3 * n)), round_no=0, status='FT')
            for n in range(60)]
        self.tonight = NOW + timedelta(hours=8)
        self.fake.next_event[LEAGUE_ID] = [game(2999, TEAMS[0], TEAMS[1], iso(self.tonight), round_no=0)]
        self.fake.games[2999] = self.fake.next_event[LEAGUE_ID][0]
        self.fake.games[2998] = game(2998, TEAMS[2], TEAMS[3], iso(self.tonight + timedelta(days=1)), round_no=0)
        self.fake.clock = iso(NOW)
        self.follow()
        self.drain()

    def test_every_team_with_nothing_to_come_is_asked(self):
        self.assertEqual(self.fetch(f'round:{LEAGUE_ID}:2099:0').outcome, SPORTS_FETCH_CAPPED_PAST)
        # The league's next game is stored from eventsnextleague, so its two teams have one to come.
        self.assertEqual(sorted(int(c[1]['id']) for c in self.fake.calls_to('eventsnext')),
                         sorted(tid(t) for t in TEAMS[2:]))
        self.assertEqual(self.event(2998).start_at, self.tonight + timedelta(days=1))

    def test_a_team_with_no_next_game_is_asked_again_in_a_few_days(self):
        row = self.fetch(f'teamnext:{LEAGUE_ID}:{tid(TEAMS[5])}')
        self.assertEqual(row.outcome, SPORTS_FETCH_EMPTY)
        self.assertEqual(row.next_due_at - row.last_at, timedelta(days=sports_data.EMPTY_RECHECK_DAYS))


class FinishedCutWeekTests(_Case):
    """dev/docs/BUGS.md 2026-11-14 05:42 PM: a cut college week re-read the morning after its games was
    taken for a round running on past the cut, like the NBA's round 0, so every listing from then on
    would be looked up by name and the page would say the league files its season as one round."""

    def setUp(self):
        super().setUp()
        self.fake.next_event[LEAGUE_ID] = week(1)
        self.fake.rounds[(LEAGUE_ID, '2099', 9)] = week()
        self.fake.rounds[(LEAGUE_ID, '2099', 10)] = [game(3000, TEAMS[0], TEAMS[1], iso(FIRST + timedelta(days=7)), round_no=10)]
        self.follow()
        self.done = self.drain()

    def test_a_cut_week_re_read_after_its_games_stays_cut_not_past(self):
        self.fake.next_event[LEAGUE_ID] = [self.fake.rounds[(LEAGUE_ID, '2099', 10)][0]]
        after = FIRST + timedelta(days=1)
        # The whole queue, in its order: the daily next-game call can fall before or after the round.
        self.ask_round_again(after)
        self.drain(after + timedelta(minutes=2))
        row = self.fetch(ROUND)
        self.assertEqual(row.outcome, SPORTS_FETCH_CAPPED)
        self.assertIsNone(row.next_due_at)
        league = SportsLeague.query.filter_by(tsdb_id=LEAGUE_ID).first()
        self.assertTrue(all(hi < datetime.max for _lo, hi in sports_data.capped_spans(league.id)))

    def test_the_leagues_next_game_is_stored_so_its_round_reads_as_current(self):
        """A cut answer can leave out the very game eventsnextleague named; storing it is what makes its
        round read as the current one. RoundZeroTests keeps the NBA's round 0 cut past."""
        later = self.done + timedelta(hours=6)
        self.fake.next_event[LEAGUE_ID] = [game(3100, TEAMS[2], TEAMS[3], iso(later + timedelta(hours=2)))]
        row = self.fetch(f'next:{LEAGUE_ID}')
        row.next_due_at = later
        state = db.session.get(SportsState, 1)
        state.next_call_at = state.last_call_at = None
        db.session.commit()
        sports_data.tick(now=later)
        self.assertEqual(self.event(3100).round, 9)
