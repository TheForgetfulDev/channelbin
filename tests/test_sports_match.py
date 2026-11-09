"""Matching TheSportsDB's events to guide rows (app/sports_match.py, dev/docs/DESIGN-sports-data.md 6, 7, 9).

Every case is a shape measured in the real guide (dev/docs/sports-data-investigation.md and design 6.3,
6.7), with made-up leagues and teams: nothing received from TheSportsDB ships (design 13). Throwaway temp
SQLite DB - never the live dvr.db.
  python3 -m unittest tests.test_sports_match
"""
import json
import os
import sys
import unittest
from datetime import date, datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tests.support.app import make_test_app  # noqa: E402
from tests.support.seed import make_account, make_channel, make_epg_entry  # noqa: E402

from app import admission, db, sports_data, sports_match  # noqa: E402
from app.database import (  # noqa: E402
    EPGEntry, SPORTS_AIRING_FIRST, SPORTS_AIRING_REPLAY, SPORTS_MATCH_STRONG, SPORTS_MATCH_WEAK,
    SportsEvent, SportsLeague, SportsLeagueCategory, SportsMatch, SportsState, SportsTeam,
)

NOW = datetime(2099, 11, 8, 12, 0)
KICKOFF = datetime(2099, 11, 8, 18, 0)


class MatchCase(unittest.TestCase):
    def setUp(self):
        self.t = make_test_app(csrf=False)
        self.addCleanup(self.t.cleanup)
        self.account = make_account()
        self.football = self.league(9001, 'Pretend Football League', ['Pretend Football League', 'PFL'])
        self.hockey = self.league(9002, 'Pretend Hockey League', ['Pretend Hockey League', 'PHL'])
        self.teams = {}
        for tid, full, nick in ((1, 'Harborview Gulls', 'Gulls'), (2, 'Ironvale Bears', 'Bears'),
                                (3, 'Northgate Panthers', 'Panthers'), (4, 'Saint-Rémy Comets', 'Comets'),
                                (5, 'Metro City Giants', 'Giants'), (6, 'Metro City Jets', 'Jets'),
                                (11, 'Harborview Lightning', 'Lightning'), (12, 'Ironvale Blackhawks', 'Blackhawks'),
                                (13, 'Northgate Panthers', 'Panthers')):
            self.teams[tid] = full
            city = full[:-len(nick)].strip()
            db.session.add(SportsTeam(tsdb_id=tid, full_name=full, names=json.dumps([full, nick]), city=city,
                                      last_looked_up=NOW))
        db.session.commit()
        self._channels = 0

    def league(self, tsdb_id, name, terms, **kw):
        league = SportsLeague(tsdb_id=tsdb_id, name=name, match_terms=json.dumps(terms), **kw)
        db.session.add(league)
        db.session.commit()
        return league

    def game(self, home, away, start=KICKOFF, league=None, **kw):
        league = league or self.football
        event = SportsEvent(tsdb_id=kw.pop('tsdb_id', None) or (SportsEvent.query.count() + 100),
                            league_id=league.id, name=f'{self.teams[home]} vs {self.teams[away]}',
                            home_team_id=home, away_team_id=away, home_name=self.teams[home],
                            away_name=self.teams[away], start_at=start,
                            local_date=kw.pop('local_date', (start or KICKOFF).date()), **kw)
        db.session.add(event)
        db.session.commit()
        return event

    def race(self, name, start, local_date, league):
        event = SportsEvent(tsdb_id=SportsEvent.query.count() + 500, league_id=league.id, name=name,
                            start_at=start, local_date=local_date)
        db.session.add(event)
        db.session.commit()
        return event

    def channel(self, name=None, category=None):
        self._channels += 1
        return make_channel(self.account, name=name or f'Local {self._channels}', category_name=category)

    def row(self, title, start=KICKOFF, sub_title=None, minutes=180, channel=None, description=None, **kw):
        channel = channel or self.channel()
        entry = make_epg_entry(channel, title=title, sub_title=sub_title, start_time=start,
                               duration_minutes=minutes, description=description, **kw)
        db.session.commit()
        return channel, entry

    def run_match(self, now=NOW):
        note = sports_match.run(now=now, pause=0)
        db.session.expire_all()
        return note

    def matches(self, event, **kw):
        return SportsMatch.query.filter_by(event_id=event.id, **kw).order_by(SportsMatch.program_start_time).all()

    def trust(self, league):
        league.trust_sample, league.trust_agreed = 30, 30
        db.session.commit()


class LocalStationTests(MatchCase):
    """How NBC, CBS and FOX stations list a regional game, measured 2026-11-07 (design 6.7)."""

    def test_a_game_named_in_the_sub_title_matches_strong(self):
        event = self.game(2, 1)
        self.row('PFL Football ᴸᶦᵛᵉ', sub_title='Harborview Gulls at Ironvale Bears')
        self.run_match()
        (m,) = self.matches(event)
        self.assertEqual(m.strength, SPORTS_MATCH_STRONG)
        self.assertEqual(m.airing, SPORTS_AIRING_FIRST)
        self.assertIn('both teams by name', m.reason)
        self.assertEqual(m.program_title, 'PFL Football')

    def test_a_generic_row_does_not_match(self):
        event = self.game(2, 1)
        self.row('PFL Football ᴸᶦᵛᵉ', description='Football action from around the Pretend Football League.')
        self.run_match()
        self.assertEqual(self.matches(event), [])

    def test_the_description_is_never_read(self):
        """Description text gave 775 rows for one game in the investigation."""
        event = self.game(2, 1)
        self.row('PFL Football', description='The Harborview Gulls visit the Ironvale Bears.')
        self.run_match()
        self.assertEqual(self.matches(event), [])

    def test_emoji_and_tags_inside_the_team_names(self):
        event = self.game(2, 1)
        self.row('PFL Football [NEW] ᴺᵉʷ', sub_title='Harborview 🏈 [PFL] Gulls at Ironvale 🏈 [PFL] Bears')
        self.run_match()
        (m,) = self.matches(event)
        self.assertEqual(m.strength, SPORTS_MATCH_STRONG)

    def test_home_first_order_accents_and_live_prefix(self):
        event = self.game(1, 4)
        self.row('Live: PFL: Harborview Gulls - Saint-Remy Comets')
        self.row('PFL Football', sub_title='Saint-Rémy Comets at Harborview Gulls')
        self.run_match()
        self.assertEqual(len(self.matches(event)), 2)

    def test_postgame_and_short_rows_are_not_games(self):
        event = self.game(2, 1)
        self.row('PFL Postgame', sub_title='Harborview Gulls at Ironvale Bears', start=KICKOFF + timedelta(hours=3))
        self.row('PFL Gulls @ Bears Highlights')
        self.row('PFL: Harborview Gulls at Ironvale Bears', minutes=30)
        self.run_match()
        self.assertEqual(self.matches(event), [])

    def test_one_channels_game_is_never_another_channels(self):
        one = self.game(2, 1)
        two = self.game(4, 3)
        ch_a, _ = self.row('PFL Football', sub_title='Harborview Gulls at Ironvale Bears')
        ch_b, _ = self.row('PFL Football', sub_title='Saint-Rémy Comets at Northgate Panthers')
        self.run_match()
        self.assertEqual([m.channel_id for m in self.matches(one)], [ch_a.id])
        self.assertEqual([m.channel_id for m in self.matches(two)], [ch_b.id])


class FalseMatchTests(MatchCase):
    """Every false match in the investigation came from city names with no league in the text."""

    def test_city_only_row_of_another_league_does_not_match(self):
        """`NHL TAMPA BAY - CHICAGO` matched the NFL's Buccaneers at Bears."""
        football = self.game(2, 1)
        hockey = self.game(12, 11, league=self.hockey)
        self.row('PHL HARBORVIEW - IRONVALE')
        self.run_match()
        self.assertEqual(self.matches(football), [])
        (m,) = self.matches(hockey)
        self.assertEqual(m.strength, SPORTS_MATCH_WEAK)
        self.assertIn('by city', m.reason)

    def test_a_nickname_in_another_league_does_not_match(self):
        """Nicknames repeat across leagues: Panthers, Jets, Kings, Rangers, Cardinals, Giants."""
        football = self.game(2, 3)
        self.row('PHL Hockey', sub_title='Northgate Panthers at Ironvale Bears')
        self.run_match()
        self.assertEqual(self.matches(football), [])

    def test_no_league_in_the_text_does_not_match(self):
        """`AFC TORONTO VS. MONTREAL ROSES FC` matched Canadiens at Maple Leafs."""
        event = self.game(2, 1)
        self.row('HARBORVIEW GULLS VS. IRONVALE BEARS FC')
        self.run_match()
        self.assertEqual(self.matches(event), [])

    def test_a_row_naming_another_league_in_this_leagues_category(self):
        """A tied category never proves a game by itself: `NFL Sunday Ticket` also holds other things."""
        event = self.game(2, 3)
        db.session.add(SportsLeagueCategory(league_id=self.football.id, category_name='PFL Sunday Ticket'))
        db.session.commit()
        self.row('PHL: Northgate Panthers at Ironvale Bears', channel=self.channel(category='PFL Sunday Ticket'))
        self.run_match()
        self.assertEqual(self.matches(event), [])

    def test_an_accent_in_a_city(self):
        """`Montréal` has an accent, and listings drop it."""
        event = self.game(1, 4)
        self.row('PFL: Saint-Remy at Harborview')
        self.run_match()
        (m,) = self.matches(event)
        self.assertEqual(m.strength, SPORTS_MATCH_WEAK)

    def test_a_term_with_an_accent_hyphen_or_curly_apostrophe_reaches_the_check(self):
        """The SQL narrows on raw text, so a folded term alone would never reach the Python check."""
        event = self.game(2, 1)
        self.football.match_terms = json.dumps(['Série PF', 'Pretend-Ball', 'O’Pretend'])
        db.session.commit()
        self.row('Serie PF: Harborview Gulls at Ironvale Bears')
        self.row('Série PF: Harborview Gulls at Ironvale Bears', start=KICKOFF + timedelta(minutes=1))
        self.row('Pretend Ball: Harborview Gulls at Ironvale Bears', start=KICKOFF + timedelta(minutes=2))
        self.row("O'Pretend: Harborview Gulls at Ironvale Bears", start=KICKOFF + timedelta(minutes=3))
        self.run_match()
        self.assertEqual(len(self.matches(event)), 4)

    def test_a_shared_city_reads_as_neither_before_the_other_teams_lookup(self):
        """`New York` is not the Giants' city while the Jets' lookup is still queued."""
        giants = db.session.query(SportsTeam).filter_by(tsdb_id=5).one()
        giants.last_looked_up = None
        db.session.commit()
        event = self.game(1, 6)
        self.game(5, 2, start=KICKOFF + timedelta(days=7))
        self.row('PFL: Metro City at Harborview')
        self.run_match()
        self.assertEqual(self.matches(event), [])

    def test_a_city_two_teams_share_reads_as_neither(self):
        event = self.game(1, 6)
        self.game(5, 2, start=KICKOFF + timedelta(days=7))   # the other Metro City team, a week on
        self.row('PFL: Metro City at Harborview')
        self.run_match()
        self.assertEqual(self.matches(event), [])


class TimeTests(MatchCase):
    """The time sets strength and airing, never which game a row is (design 6.3, 2026-11-07)."""

    def test_placeholders_listed_for_days_before_do_not_match(self):
        event = self.game(2, 1)
        package = self.channel('PFL Package 09', category='PFL Package')
        for hours in range(2, 14, 2):
            self.row('PFL | 09 - 1pm Gulls at Bears', start=KICKOFF - timedelta(hours=hours), minutes=120,
                     channel=package)
        ch, _ = self.row('PFL | 09 - 1pm Gulls at Bears', channel=package, minutes=120)
        self.run_match()
        self.assertEqual([m.program_start_time for m in self.matches(event)], [KICKOFF])

    def test_a_twelve_hour_typo_still_matches_and_shows_both_times(self):
        """TheSportsDB listed the Baptist Health 200 at 11:30am UTC for 11:30pm."""
        wrong = datetime(2099, 11, 8, 11, 30)
        real = wrong + timedelta(hours=12)
        event = self.game(2, 1, start=wrong)
        self.trust(self.football)
        for _ in range(3):
            self.row('PFL Football', sub_title='Harborview Gulls at Ironvale Bears', start=real)
        # The window is around TheSportsDB's time, so a replay 4 hours after the real start is past it.
        self.row('PFL Football', sub_title='Harborview Gulls at Ironvale Bears', start=real + timedelta(hours=4))
        self.run_match(now=wrong)
        found = self.matches(event)
        self.assertEqual([m.program_start_time for m in found], [real] * 3)
        self.assertEqual(event.guide_start, real)
        self.assertTrue(sports_match.time_disagrees(event))
        self.assertTrue(all(m.airing == SPORTS_AIRING_FIRST for m in found))
        self.assertIn('most channels list', found[0].reason)

    def test_a_disagreement_names_both_times(self):
        self.t.sandbox_config({'display': {'timezone': 'America/New_York', 'time_format': '12h'}})
        wrong = datetime(2099, 11, 8, 11, 30)
        event = self.game(2, 1, start=wrong)
        for _ in range(2):
            self.row('PFL Football', sub_title='Harborview Gulls at Ironvale Bears', start=wrong + timedelta(hours=12))
        self.run_match(now=wrong)
        reason = self.matches(event)[0].reason
        self.assertIn('at 6:30 PM, the time most channels list', reason)
        self.assertIn('TheSportsDB lists 6:30 AM', reason)

    def test_a_start_that_has_aired_is_kept_when_its_rows_leave_the_guide(self):
        """An import drops listings that have aired; the replays left must not become the game's start,
        call its time wrong, or spend the league's trust."""
        event = self.game(2, 1)
        live = [self.row('PFL Football', sub_title='Harborview Gulls at Ironvale Bears')[1] for _ in range(3)]
        for _ in range(2):
            self.row('PFL Football', sub_title='Harborview Gulls at Ironvale Bears', start=KICKOFF + timedelta(hours=6))
        self.run_match(now=KICKOFF - timedelta(hours=1))
        self.assertEqual(event.guide_start, KICKOFF)
        for entry in live:
            db.session.delete(db.session.get(EPGEntry, entry.id))
        db.session.commit()
        self.run_match(now=KICKOFF + timedelta(hours=8))
        self.assertEqual(event.guide_start, KICKOFF)
        self.assertFalse(sports_match.time_disagrees(event))
        replays = [m for m in self.matches(event) if m.program_start_time == KICKOFF + timedelta(hours=6)]
        self.assertEqual({m.airing for m in replays}, {SPORTS_AIRING_REPLAY})
        league = db.session.get(SportsLeague, self.football.id)
        self.assertEqual((league.trust_sample, league.trust_agreed), (1, 1))

    def test_nothing_past_thirteen_hours(self):
        event = self.game(2, 1)
        self.row('PFL Football', sub_title='Harborview Gulls at Ironvale Bears')
        self.row('PFL Football', sub_title='Harborview Gulls at Ironvale Bears', start=KICKOFF + timedelta(hours=14))
        self.run_match()
        self.assertEqual([m.program_start_time for m in self.matches(event)], [KICKOFF])

    def test_a_replay_in_a_trusted_league(self):
        event = self.game(2, 1)
        self.trust(self.football)
        self.row('PFL Football', sub_title='Harborview Gulls at Ironvale Bears')
        self.row('PFL Football', sub_title='Harborview Gulls at Ironvale Bears', start=KICKOFF + timedelta(hours=12))
        self.run_match()
        self.assertEqual([m.airing for m in self.matches(event)], [SPORTS_AIRING_FIRST, SPORTS_AIRING_REPLAY])

    def test_seventy_five_minutes_early_is_weak_in_a_trusted_league(self):
        """Sky's `NFL Jaguars @ Ravens` started 75 minutes early."""
        event = self.game(2, 1)
        self.trust(self.football)
        for _ in range(3):
            self.row('PFL Football', sub_title='Harborview Gulls at Ironvale Bears')
        self.row('PFL Gulls @ Bears', start=KICKOFF - timedelta(minutes=75))
        self.run_match()
        early = self.matches(event)[0]
        self.assertEqual(early.strength, SPORTS_MATCH_WEAK)
        self.assertIn('75 minutes early', early.reason)

    def test_an_untrusted_league_says_the_time_was_not_checked(self):
        event = self.game(2, 1)
        self.row('PFL Gulls @ Bears', start=KICKOFF - timedelta(minutes=75))
        self.run_match()
        (m,) = self.matches(event)
        self.assertEqual(m.strength, SPORTS_MATCH_STRONG)
        self.assertIn('not checked', m.reason)

    def test_two_games_of_one_pair_inside_the_window_go_to_the_nearer(self):
        """A doubleheader puts two games of one pair a few hours apart."""
        early = self.game(2, 1)
        late = self.game(2, 1, start=KICKOFF + timedelta(hours=5))
        self.row('PFL Football', sub_title='Harborview Gulls at Ironvale Bears', start=KICKOFF + timedelta(hours=5))
        self.row('PFL Football', sub_title='Harborview Gulls at Ironvale Bears')
        self.run_match()
        self.assertEqual([m.program_start_time for m in self.matches(early)], [KICKOFF])
        self.assertEqual([m.program_start_time for m in self.matches(late)], [KICKOFF + timedelta(hours=5)])

    def test_a_home_and_home_series_goes_to_the_nearer_game(self):
        first = self.game(2, 1)
        second = self.game(1, 2, start=KICKOFF + timedelta(days=1))
        self.row('PFL Football', sub_title='Harborview Gulls at Ironvale Bears')
        self.row('PFL Football', sub_title='Ironvale Bears at Harborview Gulls', start=KICKOFF + timedelta(days=1))
        self.run_match()
        self.assertEqual([m.program_start_time for m in self.matches(first)], [KICKOFF])
        self.assertEqual([m.program_start_time for m in self.matches(second)], [KICKOFF + timedelta(days=1)])


class CategoryTests(MatchCase):
    """A category a person tied to a league supplies the league (design 5.2)."""

    def test_a_tied_category_supplies_the_league(self):
        event = self.game(2, 1)
        db.session.add(SportsLeagueCategory(league_id=self.football.id, category_name='PFL Sunday Ticket'))
        db.session.commit()
        ch = self.channel(category='PFL Sunday Ticket')
        self.row('Football: Harborview at Ironvale', channel=ch)
        self.run_match()
        (m,) = self.matches(event)
        self.assertEqual(m.strength, SPORTS_MATCH_STRONG)
        self.assertIn('its category, PFL Sunday Ticket', m.reason)

    def test_a_replays_category_makes_a_replay(self):
        event = self.game(2, 1)
        db.session.add(SportsLeagueCategory(league_id=self.football.id, category_name='PFL REPLAY', is_replay=True))
        db.session.commit()
        self.row('Harborview Gulls at Ironvale Bears', channel=self.channel(category='PFL REPLAY'))
        self.run_match()
        (m,) = self.matches(event)
        self.assertEqual(m.airing, SPORTS_AIRING_REPLAY)

    def test_another_leagues_category_is_not_this_league(self):
        event = self.game(2, 3)
        db.session.add(SportsLeagueCategory(league_id=self.hockey.id, category_name='PHL Center Ice'))
        db.session.commit()
        self.row('Northgate Panthers at Ironvale Bears', channel=self.channel(category='PHL Center Ice'))
        self.run_match()
        self.assertEqual(self.matches(event), [])


class FieldEventTests(MatchCase):
    """Races: the series and the local date, with the name telling strong from weak (design 6.4)."""

    def setUp(self):
        super().setUp()
        self.series = self.league(9003, 'Pretend Stock Car Series', ['Pretend Stock Car Series'],
                                  sport='Motorsport')

    def test_the_local_date_not_the_utc_date(self):
        """`UNOH 200` is Sep 17 and its guide row is 00:00 UTC Sep 18."""
        event = self.race('Lakeside 200', datetime(2099, 9, 17, 23, 30), date(2099, 9, 17), self.series)
        self.row('Pretend Stock Car Series ᴸᶦᵛᵉ', sub_title='Lakeside 200', start=datetime(2099, 9, 18, 0, 0))
        self.run_match(now=datetime(2099, 9, 17, 12))
        (m,) = self.matches(event)
        self.assertEqual(m.strength, SPORTS_MATCH_STRONG)

    def test_practice_qualifying_classics_and_short_rows_do_not_match(self):
        event = self.race('Lakeside 200', datetime(2099, 9, 17, 23, 30), date(2099, 9, 17), self.series)
        start = datetime(2099, 9, 17, 16)
        self.row('Pretend Stock Car Series', sub_title='Championship Race, Free Practice', start=start)
        self.row('Pretend Stock Car Series Qualifying', start=start)
        self.row('Pretend Stock Car Series Classics', sub_title='Lakeside 200', start=start, minutes=187)
        self.row('Pretend Stock Car Series', sub_title='Lakeside 200', start=start, minutes=60)
        self.run_match(now=datetime(2099, 9, 17, 12))
        self.assertEqual(self.matches(event), [])

    def test_a_different_race_name_is_weak_and_names_both(self):
        event = self.race('Lakeside 200', datetime(2099, 9, 17, 23, 30), date(2099, 9, 17), self.series)
        self.row('Pretend Stock Car Series', sub_title='Lakeside 250', start=datetime(2099, 9, 17, 23, 30))
        self.run_match(now=datetime(2099, 9, 17, 12))
        (m,) = self.matches(event)
        self.assertEqual(m.strength, SPORTS_MATCH_WEAK)
        self.assertIn('"Lakeside 250"', m.reason)
        self.assertIn('"Lakeside 200"', m.reason)

    def test_replays_with_one_title_tell_first_from_later(self):
        """The Truck finale airs at 00:30 UTC and again at 07:00, 11:00 and 14:00 with one title."""
        event = self.race('Harbor 200', datetime(2099, 11, 6, 11, 30), date(2099, 11, 6), self.series)
        for hhmm in ((0, 30), (7, 0), (11, 0)):
            self.row('Pretend Stock Car Series', sub_title='Harbor 200', start=datetime(2099, 11, 7, *hhmm))
        self.run_match(now=datetime(2099, 11, 6, 12))
        self.assertEqual([m.airing for m in self.matches(event)],
                         [SPORTS_AIRING_FIRST, SPORTS_AIRING_REPLAY, SPORTS_AIRING_REPLAY])


class StorageTests(MatchCase):
    """A match survives an import, is marked gone rather than deleted, and a rejection sticks (design 9)."""

    def test_a_reimport_keeps_the_same_match(self):
        event = self.game(2, 1)
        ch, entry = self.row('PFL Football', sub_title='Harborview Gulls at Ironvale Bears')
        self.run_match()
        (before,) = self.matches(event)
        db.session.delete(db.session.get(EPGEntry, entry.id))
        db.session.commit()
        make_epg_entry(ch, title='PFL Football ᴸᶦᵛᵉ', sub_title='Harborview Gulls at Ironvale Bears',
                       start_time=KICKOFF, duration_minutes=180)
        db.session.commit()
        self.run_match()
        (after,) = self.matches(event)
        self.assertEqual(after.id, before.id)
        self.assertIsNone(after.gone_since)

    def test_a_row_no_longer_listed_is_marked_gone_not_deleted(self):
        event = self.game(2, 1)
        _, entry = self.row('PFL Football', sub_title='Harborview Gulls at Ironvale Bears')
        self.run_match()
        db.session.delete(db.session.get(EPGEntry, entry.id))
        db.session.commit()
        self.run_match()
        (m,) = self.matches(event)
        self.assertIsNotNone(m.gone_since)

    def test_a_rejected_match_stays_rejected(self):
        event = self.game(2, 1)
        self.row('PFL Football', sub_title='Harborview Gulls at Ironvale Bears')
        self.run_match()
        (m,) = self.matches(event)
        m.rejected_at = NOW
        db.session.commit()
        self.run_match()
        (m,) = self.matches(event)
        self.assertIsNotNone(m.rejected_at)


class TeardownTests(MatchCase):
    """Channel ids are re-issued and foreign keys are off, so a match left behind would be inherited."""

    def test_a_channel_deleted_during_a_run_gets_no_match(self):
        event = self.game(2, 1)
        ch, entry = self.row('PFL Football', sub_title='Harborview Gulls at Ironvale Bears')
        row = sports_match.Row(ch.id, None, entry.title, entry.sub_title, entry.start_time, entry.stop_time, False)
        found = [sports_match.Found(row, SPORTS_MATCH_STRONG, 'x')]
        db.session.delete(db.session.get(EPGEntry, entry.id))
        db.session.delete(ch)
        db.session.commit()
        sports_match._store_event(event.id, found, KICKOFF, 1, NOW)
        self.assertEqual(SportsMatch.query.count(), 0)

    def test_deleting_the_account_deletes_its_channels_matches(self):
        from app.routes.accounts import _delete_account_and_jobs
        event = self.game(2, 1)
        self.row('PFL Football', sub_title='Harborview Gulls at Ironvale Bears')
        self.run_match()
        self.assertEqual(len(self.matches(event)), 1)
        deleted, _ = _delete_account_and_jobs(self.account.id)
        self.assertTrue(deleted)
        db.session.expire_all()
        self.assertEqual(SportsMatch.query.count(), 0)

    def test_the_missing_channel_delete_deletes_its_matches(self):
        event = self.game(2, 1)
        ch, _ = self.row('PFL Football', sub_title='Harborview Gulls at Ironvale Bears')
        self.run_match()
        self.assertEqual(len(self.matches(event)), 1)
        now = datetime.utcnow()
        ch.last_seen_at = now - timedelta(days=30)
        self.account.last_sync_at = now - timedelta(days=1)
        db.session.commit()
        resp = self.t.client.post('/channels/missing-delete', json={'account_id': self.account.id})
        self.assertEqual(resp.get_json().get('deleted_count'), 1, resp.get_data(as_text=True))
        db.session.expire_all()
        self.assertEqual(SportsMatch.query.count(), 0)

    def test_removing_the_league_deletes_its_matches_and_categories(self):
        event = self.game(2, 1)
        db.session.add(SportsLeagueCategory(league_id=self.football.id, category_name='PFL Package'))
        db.session.commit()
        self.row('PFL Football', sub_title='Harborview Gulls at Ironvale Bears')
        self.run_match()
        self.assertEqual(len(self.matches(event)), 1)
        self.assertTrue(sports_data.delete_league(self.football.id))
        self.assertEqual(SportsMatch.query.count(), 0)
        self.assertEqual(SportsLeagueCategory.query.count(), 0)

    def test_pruning_an_event_deletes_its_matches(self):
        event = self.game(2, 1)
        self.row('PFL Football', sub_title='Harborview Gulls at Ironvale Bears')
        self.run_match()
        self.assertEqual(len(self.matches(event)), 1)
        db.session.add(SportsState(id=1))
        db.session.commit()
        sports_data._prune({'history_keep_days': 1}, KICKOFF + timedelta(days=5))
        db.session.expire_all()
        self.assertEqual(SportsMatch.query.count(), 0)


class TrustAndRunTests(MatchCase):
    def test_trust_counts_events_the_guide_identified(self):
        for day in range(12):
            start = KICKOFF - timedelta(days=day)
            event = self.game(2, 1, start=start, tsdb_id=1000 + day)
            offset = timedelta(hours=2) if day == 0 else timedelta(0)
            self.row('PFL Football', sub_title='Harborview Gulls at Ironvale Bears', start=start + offset)
            self.assertIsNotNone(event)
        self.run_match(now=KICKOFF)
        sports_match.match_league(self.football.id, KICKOFF - timedelta(days=11), pause=0)
        db.session.expire_all()
        league = db.session.get(SportsLeague, self.football.id)
        self.assertEqual((league.trust_sample, league.trust_agreed), (12, 11))
        self.assertTrue(sports_match.league_trusted(league))

    def test_a_refused_run_says_what_it_waits_for(self):
        db.session.add(SportsState(id=1))
        db.session.commit()
        self.game(2, 1)
        ticket = admission.try_start(admission.KIND_SYNC, 'Account 1')
        self.addCleanup(admission.release, ticket)
        note = self.run_match()
        self.assertIn('Waiting: an account sync', note)
        self.assertEqual(db.session.get(SportsState, 1).match_note, note)

    def test_a_run_that_stops_says_so_instead_of_the_last_good_one(self):
        from unittest.mock import patch
        db.session.add(SportsState(id=1, match_note='Found 3 events of 3 in the guide.'))
        db.session.commit()
        self.game(2, 1)
        with patch.object(sports_match, 'match_league', side_effect=RuntimeError('boom')), \
                self.assertLogs('app.sports_match', 'ERROR'):
            with self.assertRaises(RuntimeError):
                sports_match.run(now=NOW, pause=0)
        db.session.expire_all()
        self.assertIn('stopped with an error (RuntimeError)', db.session.get(SportsState, 1).match_note)
        self.assertEqual(admission.active_kinds(), set())

    def test_a_run_names_what_it_found(self):
        db.session.add(SportsState(id=1))
        db.session.commit()
        self.game(2, 1)
        self.game(4, 3)
        self.row('PFL Football', sub_title='Harborview Gulls at Ironvale Bears')
        note = self.run_match()
        self.assertIn('Found 1 event of 2 in the guide, on 1 listing', note)
        self.assertIsNotNone(db.session.get(SportsState, 1).last_match_at)


if __name__ == '__main__':
    unittest.main()
