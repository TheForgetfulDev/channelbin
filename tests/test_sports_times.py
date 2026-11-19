"""Which time a game goes by (app/sports_times.py, dev/changelog/1630): the rule for one listing
under every answer, the wait, a started game, a field event's placeholder, and the config the
answers are kept in. Pure functions first, over plain values; the config cases run against the
test app's own sandboxed config.yaml."""
import os
import sys
import unittest
from datetime import datetime, timedelta
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tests.support.app import make_test_app  # noqa: E402
from app import sports_times as st  # noqa: E402
from app.config import load_config  # noqa: E402

M = timedelta(minutes=1)
H = timedelta(hours=1)
SEVEN = datetime(2099, 11, 20, 0, 0)          # 7:00 PM ET, the listing
TSDB = SEVEN + 75 * M                         # 8:15 PM ET, TheSportsDB
STOP = SEVEN + 5 * H + 15 * M                 # 12:15 AM ET
NEAR = SEVEN - 9 * H                          # within the 24-hour wait


def rule(go_by, limit=90, wait=24):
    return st.Rule(go_by=go_by, cover_both_minutes=limit, wait_hours=wait)


def event(start_at=TSDB, guide_start=SEVEN, behind=None, matchup=True, **kw):
    fields = dict(start_at=start_at, guide_start=guide_start, guide_behind_start=behind,
                  home_team_id=1 if matchup else 0, away_team_id=2 if matchup else 0,
                  previous_start=None, start_changed_at=None, postponed=False)
    fields.update(kw)
    return SimpleNamespace(**fields)


class WindowsTests(unittest.TestCase):
    """windows(start, stop, listed, tsdb, rule, now) for one listing."""

    def w(self, go_by, start=SEVEN, stop=STOP, listed=SEVEN, tsdb=TSDB, now=NEAR, **kw):
        return st.windows(start, stop, listed, tsdb, rule(go_by, **kw), now)

    def test_go_by_thesportsdb_moves_the_listing_and_its_end(self):
        """Texans at Colts on Nov 18: listed 7:00 PM-12:15 AM, TheSportsDB 8:15 PM."""
        self.assertEqual(self.w(st.GO_TSDB), [(TSDB, STOP + 75 * M)])

    def test_moved_earlier_the_end_stays_where_it_is_listed(self):
        late = SEVEN + 95 * M
        self.assertEqual(self.w(st.GO_TSDB, start=late, listed=late, tsdb=SEVEN), [(SEVEN, STOP)])

    def test_your_guide_is_as_listed(self):
        self.assertEqual(self.w(st.GO_GUIDE), [(SEVEN, STOP)])

    def test_cover_both_is_one_window_from_the_earlier_start_to_the_later_end(self):
        self.assertEqual(self.w(st.GO_BOTH), [(SEVEN, STOP + 75 * M)])

    def test_the_limit_is_inclusive_and_past_it_two_windows_that_do_not_touch(self):
        start, stop = SEVEN, SEVEN + H
        at90 = st.windows(start, stop, start, start + 90 * M, rule(st.GO_BOTH), NEAR)
        at91 = st.windows(start, stop, start, start + 91 * M, rule(st.GO_BOTH), NEAR)
        self.assertEqual(at90, [(start, stop + 90 * M)])
        self.assertEqual(at91, [(start, stop), (start + 91 * M, stop + 91 * M)])

    def test_cover_both_past_the_limit_merges_windows_that_overlap(self):
        """Mavericks at Warriors: 95 minutes apart, but the moved window overlaps the listing's."""
        late = SEVEN + 95 * M
        got = st.windows(late, late + 150 * M, late, SEVEN, rule(st.GO_BOTH), NEAR)
        self.assertEqual(got, [(SEVEN, late + 150 * M)])

    def test_a_listing_that_agrees_within_five_minutes_is_as_listed(self):
        self.assertEqual(self.w(st.GO_TSDB, tsdb=SEVEN + 5 * M), [(SEVEN, STOP)])
        self.assertEqual(self.w(st.GO_TSDB, tsdb=SEVEN + 6 * M), [(SEVEN + 6 * M, STOP + 6 * M)])

    def test_further_off_than_the_wait_is_as_listed(self):
        far = SEVEN - 25 * H
        self.assertEqual(self.w(st.GO_TSDB, now=far), [(SEVEN, STOP)])
        self.assertEqual(self.w(st.GO_BOTH, now=far), [(SEVEN, STOP)])
        self.assertEqual(self.w(st.GO_TSDB, now=SEVEN - 24 * H), [(TSDB, STOP + 75 * M)])

    def test_each_listing_moves_by_its_own_gap_and_a_replay_never_moves(self):
        """8:00 PM listings of the same game go to 8:15 PM, not 9:15 PM; the 12:30 AM replay stays."""
        eight = SEVEN + H
        self.assertEqual(self.w(st.GO_TSDB, start=eight, stop=STOP), [(TSDB, STOP + 15 * M)])
        replay = SEVEN + 5 * H + 30 * M
        self.assertEqual(self.w(st.GO_TSDB, start=replay, stop=replay + 3 * H), [(replay, replay + 3 * H)])

    def test_the_twelve_hour_typo_plays_at_thesportsdbs_time(self):
        """A guide listing a night game in the morning, on enough channels to be its start."""
        morning = TSDB - 12 * H
        got = st.windows(morning, morning + 3 * H, morning, TSDB, rule(st.GO_TSDB), morning - H)
        self.assertEqual(got, [(TSDB, TSDB + 3 * H)])

    def test_a_gap_past_the_matchers_window_is_as_listed(self):
        """TheSportsDB postponed the game a day and the matcher has not run since: its old listings
        are not moved a day on (review of 1630; airing_now's query reach relies on it)."""
        self.assertEqual(self.w(st.GO_TSDB, tsdb=SEVEN + 24 * H), [(SEVEN, STOP)])
        self.assertEqual(self.w(st.GO_TSDB, tsdb=SEVEN + 13 * H), [(SEVEN + 13 * H, STOP + 13 * H)])

    def test_no_time_from_either_side_is_as_listed(self):
        self.assertEqual(self.w(st.GO_TSDB, tsdb=None), [(SEVEN, STOP)])
        self.assertEqual(self.w(st.GO_TSDB, listed=None), [(SEVEN, STOP)])

    def test_an_unknown_answer_is_an_error_not_a_quiet_default(self):
        with self.assertRaises(ValueError):
            self.w('sometimes')


class EventTimesTests(unittest.TestCase):

    def test_a_field_event_at_midnight_has_no_thesportsdb_time(self):
        race = event(start_at=datetime(2099, 11, 20, 0, 0), matchup=False)
        self.assertIsNone(st.tsdb_start(race))
        game = event(start_at=datetime(2099, 11, 20, 0, 0), matchup=True)
        self.assertEqual(st.tsdb_start(game), datetime(2099, 11, 20, 0, 0))

    def test_a_started_game_keeps_its_start(self):
        """Moved during the game (a rain delay entered at the restart): it keeps the start it had."""
        moved = event(start_at=SEVEN + 2 * H, guide_start=SEVEN, previous_start=SEVEN,
                      start_changed_at=SEVEN + 30 * M)
        self.assertEqual(st.tsdb_start(moved), SEVEN)
        self.assertEqual(st.event_windows(SEVEN, STOP, moved, rule(st.GO_TSDB), SEVEN + 40 * M)[0][0], SEVEN)

    def test_a_started_game_moved_later_ends_later_by_the_move(self):
        """The F1 rain delay (dev/changelog/1632): moved 90 minutes later at the green flag, it keeps its
        start and its end goes 90 minutes later, under TheSportsDB and Cover both, never the guide."""
        moved = event(start_at=SEVEN + 90 * M, guide_start=SEVEN, previous_start=SEVEN,
                      start_changed_at=SEVEN + 30 * M)
        now = SEVEN + 40 * M
        self.assertEqual(st.running_late(moved), 90 * M)
        self.assertEqual(st.event_windows(SEVEN, STOP, moved, rule(st.GO_TSDB), now), [(SEVEN, STOP + 90 * M)])
        self.assertEqual(st.event_windows(SEVEN, STOP, moved, rule(st.GO_BOTH), now), [(SEVEN, STOP + 90 * M)])
        self.assertEqual(st.event_windows(SEVEN, STOP, moved, rule(st.GO_GUIDE), now), [(SEVEN, STOP)])
        replay = SEVEN + 6 * H
        self.assertEqual(st.event_windows(replay, replay + 2 * H, moved, rule(st.GO_TSDB), now),
                         [(replay, replay + 2 * H)])

    def test_a_move_past_six_hours_is_a_new_date_and_adds_nothing(self):
        """A race rained out to Monday is not a delay (sports_match.MAX_DELAY)."""
        moved = event(start_at=SEVEN + 26 * H, guide_start=SEVEN, previous_start=SEVEN,
                      start_changed_at=SEVEN + 30 * M)
        self.assertEqual(st.running_late(moved), timedelta(0))
        six = event(start_at=SEVEN + 6 * H, guide_start=SEVEN, previous_start=SEVEN, start_changed_at=SEVEN + 30 * M)
        self.assertEqual(st.running_late(six), 6 * H)
        self.assertEqual(st.running_late(event(start_at=SEVEN + 90 * M, guide_start=SEVEN, previous_start=SEVEN,
                                               start_changed_at=SEVEN + 30 * M, postponed=True)), timedelta(0))

    def test_moved_earlier_or_not_started_adds_nothing(self):
        earlier = event(start_at=SEVEN - 30 * M, guide_start=SEVEN, previous_start=SEVEN,
                        start_changed_at=SEVEN + 30 * M)
        self.assertEqual(st.running_late(earlier), timedelta(0))
        coming = event(start_at=SEVEN + 90 * M, guide_start=SEVEN, previous_start=SEVEN,
                       start_changed_at=SEVEN - 30 * M)
        self.assertEqual(st.running_late(coming), timedelta(0))
        nudged = event(start_at=SEVEN + 4 * M, guide_start=SEVEN, previous_start=SEVEN,
                       start_changed_at=SEVEN + 30 * M)
        self.assertEqual(st.running_late(nudged), timedelta(0))

    def test_the_guide_behind_on_a_move_lists_the_old_time(self):
        behind = event(start_at=TSDB, guide_start=TSDB, behind=SEVEN)
        self.assertEqual(st.listed_start(behind), SEVEN)
        self.assertEqual(st.event_windows(SEVEN, STOP, behind, rule(st.GO_GUIDE), NEAR), [(SEVEN, STOP)])
        self.assertEqual(st.event_windows(SEVEN, STOP, behind, rule(st.GO_TSDB), NEAR), [(TSDB, STOP + 75 * M)])

    def test_kickoff_names_thesportsdb_once_the_answer_acts(self):
        e = event()
        self.assertEqual(st.kickoff(e, rule(st.GO_TSDB), NEAR), TSDB)
        self.assertEqual(st.kickoff(e, rule(st.GO_GUIDE), NEAR), SEVEN)
        self.assertEqual(st.kickoff(e, rule(st.GO_TSDB), SEVEN - 30 * H), SEVEN)
        self.assertEqual(st.kickoff(event(guide_start=None), rule(st.GO_GUIDE), NEAR), TSDB)


class StoredAnswersTests(unittest.TestCase):
    """The answers in config.yaml: the default, a league's own, and what a bad value falls back to."""

    def setUp(self):
        self.t = make_test_app(csrf=False)
        self.addCleanup(self.t.cleanup)

    def test_the_default_is_thesportsdb_90_minutes_and_24_hours(self):
        d = st.Rules().default
        self.assertEqual((d.go_by, d.cover_both_minutes, d.wait_hours), (st.GO_TSDB, 90, 24))
        self.assertTrue(st.Rules().of(4387).uses_default)

    def test_a_league_keeps_only_what_it_changed_and_follows_the_default_for_the_rest(self):
        st.set_league(4387, {'go_by': st.GO_BOTH, 'cover_both_minutes': 120})
        st.set_default({'wait_hours': 48})
        r = st.Rules().of(4387)
        self.assertEqual((r.go_by, r.cover_both_minutes, r.wait_hours), (st.GO_BOTH, 120, 48))
        self.assertEqual(r.own, {'go_by': st.GO_BOTH, 'cover_both_minutes': 120})
        self.assertEqual(st.Rules().of(4328).go_by, st.GO_TSDB)

    def test_none_goes_back_to_the_default_and_an_empty_league_leaves_the_file(self):
        st.set_league(4387, {'go_by': st.GO_GUIDE})
        st.set_league(4387, {'go_by': None})
        self.assertEqual(load_config()['sports_data']['game_times']['leagues'], {})
        from app.config import _load_config_file
        self.assertNotIn('game_times', _load_config_file().get('sports_data') or {},
                         'config.yaml holds only what was chosen')

    def test_forget_league_drops_its_answers(self):
        st.set_league(4387, {'go_by': st.GO_GUIDE})
        self.assertTrue(st.forget_league(4387))
        self.assertFalse(st.forget_league(4387))
        self.assertTrue(st.Rules().of(4387).uses_default)

    def test_a_bad_answer_is_refused_with_the_reason(self):
        for changes, says in (({'go_by': 'maybe'}, 'Choose'), ({'wait_hours': -1}, 'whole number'),
                              ({'cover_both_minutes': 'ten'}, 'whole number'), ({'color': 1}, 'no setting'),
                              ({}, 'Nothing')):
            with self.assertRaises(ValueError) as cm:
                st.set_league(4387, changes)
            self.assertIn(says, str(cm.exception))
        with self.assertRaises(ValueError):
            st.set_default({'go_by': None})

    def test_a_hand_edited_typo_falls_back_to_the_default(self):
        """An unquoted id reads back as an int, and a bad value is the default's, not a crash."""
        rules = st.Rules({'sports_data': {'game_times': {'go_by': 'sometimes', 'wait_hours': 'x',
                                                         'leagues': {4387: {'go_by': 'both'}}}}})
        self.assertEqual(rules.default.go_by, st.GO_TSDB)
        self.assertEqual(rules.default.wait_hours, 24)
        self.assertEqual(rules.of('4387').go_by, st.GO_BOTH)



class PagesTests(unittest.TestCase):
    """The Game time settings cards and their routes (mockup 58)."""

    def setUp(self):
        from app import db
        from app.database import SportsFollow, SportsLeague
        self.t = make_test_app(csrf=False)
        self.addCleanup(self.t.cleanup)
        self.db = db
        self.nba = SportsLeague(tsdb_id=4387, name='NBA', sport='Basketball', match_terms='["NBA"]',
                                trust_sample=30, trust_agreed=27, created_at=datetime.utcnow())
        self.f1 = SportsLeague(tsdb_id=4370, name='Formula 1', sport='Motorsport', match_terms='["F1"]',
                               trust_sample=1, trust_agreed=0, created_at=datetime.utcnow() - timedelta(days=60))
        db.session.add_all([self.nba, self.f1])
        db.session.flush()
        # Formula 1 followed for 60 days: longer than the 30 the history keeps (review of 1630: the
        # follow's date, not the league row's, which a follow-again keeps).
        db.session.add_all([SportsFollow(league_id=self.nba.id, team_tsdb_id=0, rule_id=0, created_at=datetime.utcnow()),
                            SportsFollow(league_id=self.f1.id, team_tsdb_id=0, rule_id=0,
                                         created_at=datetime.utcnow() - timedelta(days=60))])
        db.session.commit()
        self.client = self.t.client

    def page(self, url):
        r = self.client.get(url)
        self.assertEqual(r.status_code, 200)
        return r.get_data(as_text=True)

    def put(self, url, body):
        return self.client.put(url, json=body)

    def test_a_league_page_has_the_card_on_the_default_with_its_record_as_advice(self):
        html = self.page(f'/sports/{self.nba.id}')
        self.assertIn('id="sp-times"', html)
        self.assertIn('<option value="" selected>Default (TheSportsDB)</option>', html)
        self.assertIn('notice notice-ok', html)
        self.assertIn('on 27 of the last 30', html)
        self.assertIn('Matched your guide on 27 of the last 30 events', html)
        self.assertIn('href="https://www.thesportsdb.com/" target="_blank" rel="noopener">TheSportsDB</a>\'s time', html)
        self.assertNotIn('Cover both up to', html)
        self.assertNotIn('Saved as you change it', html)

    def test_a_motorsport_says_race_and_its_ask_goes_once_it_has_an_answer(self):
        """Race for every Motorsport league, from sports_view._noun(), never a named series."""
        html = self.page(f'/sports/{self.f1.id}')
        self.assertIn('<h2>Race time settings</h2>', html)
        self.assertIn('does not race often enough to get there', html)
        r = self.put(f'/api/sports/leagues/{self.f1.id}/game-times', {'go_by': 'both'})
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        html = self.page(f'/sports/{self.f1.id}')
        self.assertNotIn('does not race often enough', html)
        self.assertIn('Cover both up to (minutes apart)', html)

    def test_a_league_followed_again_lately_is_not_told_it_cannot_reach_ten(self):
        from app.database import SportsFollow
        SportsFollow.query.filter_by(league_id=self.f1.id).update({'created_at': datetime.utcnow()})
        self.db.session.commit()
        html = self.page(f'/sports/{self.f1.id}')
        self.assertNotIn('does not race often enough', html)
        self.assertIn('1 race checked so far', html)

    def test_an_empty_choice_or_box_goes_back_to_the_default(self):
        url = f'/api/sports/leagues/{self.nba.id}/game-times'
        self.assertEqual(self.put(url, {'go_by': 'guide', 'wait_hours': 48}).status_code, 200)
        self.assertEqual(st.Rules().of(4387).own, {'go_by': 'guide', 'wait_hours': 48})
        self.assertEqual(self.put(url, {'go_by': '', 'wait_hours': ''}).status_code, 200)
        self.assertTrue(st.Rules().of(4387).uses_default)
        r = self.put(url, {'wait_hours': -3})
        self.assertEqual(r.status_code, 400)
        self.assertIn('whole number', r.get_json()['error'])

    def test_sports_schedules_sets_the_default_and_names_leagues_with_their_own(self):
        self.put(f'/api/sports/leagues/{self.f1.id}/game-times', {'go_by': 'both'})
        html = self.page('/sports')
        self.assertIn('<h2>Game time settings</h2>', html)
        self.assertIn('Leagues with their own choice: Formula 1 (Cover both).', html)
        self.assertEqual(self.put('/api/sports/game-times', {'go_by': 'guide'}).status_code, 200)
        self.assertEqual(st.Rules().default.go_by, st.GO_GUIDE)
        self.assertEqual(self.put('/api/sports/game-times', {'go_by': ''}).status_code, 400)
        self.assertIn('Default (Your guide)', self.page(f'/sports/{self.nba.id}'))

    def test_deleting_a_league_forgets_its_answers_and_stopping_keeps_them(self):
        self.put(f'/api/sports/leagues/{self.nba.id}/game-times', {'go_by': 'guide'})
        self.assertEqual(self.client.delete(f'/api/sports/leagues/{self.nba.id}/following').status_code, 200)
        self.assertEqual(st.Rules().of(4387).go_by, st.GO_GUIDE)
        self.assertEqual(self.client.delete(f'/api/sports/leagues/{self.nba.id}').status_code, 200)
        self.assertTrue(st.Rules().of(4387).uses_default)


if __name__ == '__main__':
    unittest.main()
