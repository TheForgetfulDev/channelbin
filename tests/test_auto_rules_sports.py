"""An auto-record rule that names a league or team from Sports Schedules (DESIGN-sports-data.md 10,
DESIGN-autoschedule.md §3.3's stated exception, dev/changelog/1490).

  - SaveTests: naming a league writes a follow the rule owns, in the rule's own commit; changing
    or clearing it, or deleting the rule, moves or removes that follow and stops the league's calls
    once nothing else follows it; a person's own follow keeps them going. What is refused.
  - LeagueInUseTests: a league a rule names cannot be removed from Sports Schedules.
  - OneRecordingPerGameTests: of a game's first airings the listing most channels carry is
    recorded, another listing it covers is `skipped_existing`, and one it does not cover - a
    12-hour wrong time - is scheduled as well. A rule that names nothing is judged as before.
  - SkipReplayTests: a replay is skipped only while a first airing is scheduled or recorded; doubt
    and a failed first airing are never skipped.
  - GameAlertTests: one standing alert per rule for a game hours away that nothing lists.
  - MatcherTests: a match run over a league a rule names asks for a rule check.
  - PageTests: the rule page's Expected games card, game line, chips and dialog data.

Every match and event here is invented, the same as the IPTV simulator's accounts: nothing
received from TheSportsDB ships (DESIGN-sports-data.md 13).

    python3 -m unittest tests.test_auto_rules_sports
"""
import json
import os
import sys
import unittest
from datetime import datetime, timedelta
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tests.support import make_test_app  # noqa: E402
from tests.support import seed  # noqa: E402
from app import admission, auto_rules, db, sports_data, sports_match  # noqa: E402
from app.alerts import AUTO_RULE_GAME_UNLISTED  # noqa: E402
from app.database import (  # noqa: E402
    Alert, AutoRule, REC_STATUS_FAILED, RECORDING_RULE_GAME_CHANGED, RULE_MATCH_SCHEDULED,
    RULE_MATCH_SKIPPED_EXISTING, RULE_MATCH_SKIPPED_REPLAY, SPORTS_AIRING_FIRST,
    SPORTS_AIRING_REPLAY, SPORTS_AIRING_UNKNOWN, SPORTS_MATCH_KIND_CHANNEL_NAME,
    SPORTS_MATCH_KIND_GUIDE, SPORTS_MATCH_STRONG, SPORTS_MATCH_WEAK, Recording, RuleMatch, SportsEvent, SportsFetch,
    RecordingEvent, RecordingSports, SportsFollow, SportsLeague, SportsMatch, SportsState,
)
from app.program_titles import title_key  # noqa: E402

GULLS, BEARS, OTTERS = 101, 102, 103


def _at(hours):
    return (datetime.utcnow() + timedelta(hours=hours)).replace(minute=0, second=0, microsecond=0)


class _Case(unittest.TestCase):

    def setUp(self):
        self.t = make_test_app(csrf=False)
        self.armed = patch('app.routes.recordings.schedule_recording').start()
        self.addCleanup(patch.stopall)
        self.acc = seed.make_account()
        self._stream = 500
        self.league = SportsLeague(tsdb_id=9001, name='Pretend Football League',
                                   match_terms=json.dumps(['PFL']))
        self.other = SportsLeague(tsdb_id=9002, name='Pretend Hockey League', match_terms='[]')
        db.session.add_all([self.league, self.other])
        db.session.commit()
        self.kickoff = _at(30) + timedelta(minutes=15)

    def tearDown(self):
        admission.reset_for_tests()
        self.t.cleanup()

    # -- seeding ------------------------------------------------------------------------------

    def channel(self, name):
        self._stream += 1
        return seed.make_channel(self.acc, stream_id=self._stream, name=name)

    def game(self, start=None, home=BEARS, away=GULLS, tsdb_id=7001, league=None):
        names = {GULLS: 'Harborview Gulls', BEARS: 'Ironvale Bears', OTTERS: 'Saltmarsh Otters'}
        start = start or self.kickoff
        e = SportsEvent(tsdb_id=tsdb_id, league_id=(league or self.league).id,
                        name=f'{names[home]} vs {names[away]}', home_team_id=home, away_team_id=away,
                        home_name=names[home], away_name=names[away], season='2099', round=10,
                        venue='Ironvale Field', city='Ironvale', start_at=start, local_date=start.date())
        db.session.add(e)
        db.session.commit()
        return e

    def listing(self, event, channels, title, start, minutes=195, sub=None, airing=SPORTS_AIRING_FIRST,
                strength=SPORTS_MATCH_STRONG):
        """One listing of the game on each channel, each matched to it."""
        for ch in channels:
            seed.make_epg_entry(ch, title=title, sub_title=sub, start_time=start, duration_minutes=minutes)
            db.session.add(SportsMatch(
                event_id=event.id, channel_id=ch.id, kind=SPORTS_MATCH_KIND_GUIDE,
                program_start_time=start, program_stop_time=start + timedelta(minutes=minutes),
                title_key=title_key(title), episode_key=title_key(sub), program_title=title,
                strength=strength, airing=airing,
                reason='The league named in the listing, both teams by name, at the listed time.'))
        db.session.commit()

    def rule(self, search='grain=airings&q=Gulls', **kw):
        data = {'name': kw.pop('name', 'Gulls games'), 'search': search}
        data.update(kw)
        return auto_rules.create_rule(auto_rules.read_rule_values(data))

    def evaluate(self, rule_id):
        out = auto_rules.evaluate_now(rule_id)
        db.session.expire_all()
        return out

    def ledger(self, rule_id):
        return {(m.program_title, m.program_start_time): m for m in
                RuleMatch.query.filter_by(rule_id=rule_id).all()}

    def recordings(self, rule_id):
        db.session.expire_all()
        return Recording.query.filter_by(rule_id=rule_id).order_by(Recording.program_start_time).all()

    def mnf(self, channels=3):
        """Monday night's shape, measured on prod (mockup 52): the main listing on several channels,
        a pregame-early Spanish listing, and a differently worded listing at the kickoff."""
        g = self.game()
        main = [self.channel(f'Sports {i}') for i in range(channels)]
        spanish, other = self.channel('Deportes'), self.channel('Sports Extra')
        self.listing(g, main, 'PFL Football: Gulls at Bears', self.kickoff)
        self.listing(g, [spanish], 'Futbol PFL: Gulls vs. Bears', self.kickoff - timedelta(minutes=15))
        self.listing(g, [other], 'PFL Football - Gulls at Bears Live', self.kickoff)
        return g, main, spanish, other


# ---------------------------------------------------------------------------
# Saving a rule
# ---------------------------------------------------------------------------

class SaveTests(_Case):

    def follows(self):
        db.session.expire_all()
        return sorted((f.league_id, f.team_tsdb_id, f.rule_id) for f in SportsFollow.query.all())

    def test_naming_a_team_writes_a_follow_the_rule_owns_and_queues_the_league(self):
        self.game()
        rid = self.rule(sports_league_id=self.league.id, sports_team_tsdb_id=GULLS)
        self.assertEqual(self.follows(), [(self.league.id, GULLS, rid)])
        self.assertTrue(SportsFetch.query.filter_by(league_id=self.league.id).count())
        rule = db.session.get(AutoRule, rid)
        self.assertEqual((rule.sports_league_id, rule.sports_team_tsdb_id), (self.league.id, GULLS))

    def test_moving_to_another_league_moves_the_follow_and_stops_the_old_calls(self):
        rid = self.rule(sports_league_id=self.league.id)
        values = auto_rules.read_rule_values({'sports_league_id': self.other.id},
                                             existing=db.session.get(AutoRule, rid))
        auto_rules.update_rule(rid, values)
        self.assertEqual(self.follows(), [(self.other.id, 0, rid)])
        self.assertEqual(SportsFetch.query.filter_by(league_id=self.league.id).count(), 0)
        self.assertTrue(SportsFetch.query.filter_by(league_id=self.other.id).count())

    def test_a_persons_own_follow_keeps_the_league_fetched(self):
        sports_data.set_following(self.league.id, [])
        rid = self.rule(sports_league_id=self.league.id)
        auto_rules.delete_rule(rid, cancel_scheduled=False)
        self.assertEqual(self.follows(), [(self.league.id, 0, 0)])
        self.assertTrue(SportsFetch.query.filter_by(league_id=self.league.id).count())

    def test_deleting_the_rule_removes_its_follow_and_the_calls(self):
        rid = self.rule(sports_league_id=self.league.id)
        auto_rules.delete_rule(rid, cancel_scheduled=False)
        self.assertEqual(self.follows(), [])
        self.assertEqual(SportsFetch.query.filter_by(league_id=self.league.id).count(), 0)

    def test_naming_nothing_clears_the_team_and_both_options(self):
        rid = self.rule(sports_league_id=self.league.id, sports_skip_replays=True,
                        sports_event_channels=True)
        values = auto_rules.read_rule_values({'sports_league_id': None, 'sports_skip_replays': True},
                                             existing=db.session.get(AutoRule, rid))
        self.assertEqual((values.sports_league_id, values.sports_team_tsdb_id,
                          values.sports_skip_replays, values.sports_event_channels),
                         (None, None, False, False))

    def test_a_body_that_says_nothing_about_sports_keeps_what_the_rule_names(self):
        """The Programs search's Save to rule sends only the search."""
        rid = self.rule(sports_league_id=self.league.id, sports_skip_replays=True)
        values = auto_rules.read_rule_values({'search': 'grain=airings&q=Bears'},
                                             existing=db.session.get(AutoRule, rid))
        self.assertEqual((values.sports_league_id, values.sports_skip_replays), (self.league.id, True))

    def test_a_league_not_on_sports_schedules_or_a_team_with_no_games_is_refused(self):
        with self.assertRaisesRegex(auto_rules.RuleError, 'no longer on Sports Schedules'):
            auto_rules.read_rule_values({'name': 'x', 'search': 'grain=airings&q=x',
                                         'sports_league_id': 99999})
        with self.assertRaisesRegex(auto_rules.RuleError, 'no games stored'):
            auto_rules.read_rule_values({'name': 'x', 'search': 'grain=airings&q=x',
                                         'sports_league_id': self.league.id, 'sports_team_tsdb_id': 555})

    def test_a_team_whose_games_were_pruned_still_saves(self):
        """Off-season retention prunes a team's games; a rule naming it must still save."""
        g = self.game()
        rid = self.rule(sports_league_id=self.league.id, sports_team_tsdb_id=GULLS)
        db.session.delete(g)
        db.session.commit()
        r = self.t.client.put(f'/api/auto-rules/{rid}', json={'name': 'Renamed'})
        self.assertEqual(r.status_code, 200, r.get_json())
        self.assertEqual(db.session.get(AutoRule, rid).sports_team_tsdb_id, GULLS)

    def test_the_edit_route_saves_the_league_and_says_so_on_the_page(self):
        self.game()
        rid = self.rule()
        r = self.t.client.put(f'/api/auto-rules/{rid}', json={
            'sports_league_id': self.league.id, 'sports_team_tsdb_id': GULLS, 'sports_skip_replays': True})
        self.assertEqual(r.status_code, 200, r.get_json())
        page = self.t.client.get(f'/rules/{rid}').get_data(as_text=True)
        self.assertIn('Harborview Gulls (Pretend Football League)', page)
        self.assertIn('Expected games', page)


class LeagueInUseTests(_Case):

    def test_a_league_a_rule_names_cannot_be_removed(self):
        self.rule(name='Gulls games', sports_league_id=self.league.id)
        r = self.t.client.delete(f'/api/sports/leagues/{self.league.id}')
        self.assertEqual(r.status_code, 409)
        self.assertIn('"Gulls games"', r.get_json()['error'])
        self.assertIsNotNone(db.session.get(SportsLeague, self.league.id))

    def test_a_league_only_a_rule_follows_offers_follow_not_stop_following(self):
        self.rule(name='Gulls games', sports_league_id=self.league.id)
        page = self.t.client.get(f'/sports/{self.league.id}').get_data(as_text=True)
        self.assertNotIn('data-act="stop"', page)
        self.assertIn('data-act="refollow">Follow<', page)
        self.assertIn('Followed by rules', page)
        self.assertIn('Gulls games</a> (All its games)', page)

    def test_both_sports_pages_tell_delete_which_rules_name_the_league(self):
        """So Delete league says it cannot before it is pressed (static/js/sports.js del())."""
        import html as html_mod
        self.rule(name='Gulls games', sports_league_id=self.league.id)
        for url in (f'/sports/{self.league.id}', '/sports'):
            page = html_mod.unescape(self.t.client.get(url).get_data(as_text=True))
            self.assertIn('"rules": ["Gulls games"]', page, url)

    def test_one_no_rule_names_is_removed(self):
        self.rule(sports_league_id=self.other.id)
        r = self.t.client.delete(f'/api/sports/leagues/{self.league.id}')
        self.assertEqual(r.status_code, 200)


# ---------------------------------------------------------------------------
# The judge
# ---------------------------------------------------------------------------

class OneRecordingPerGameTests(_Case):

    def test_the_listing_most_channels_carry_is_recorded_and_the_rest_are_covered(self):
        self.mnf()
        rid = self.rule(sports_league_id=self.league.id)
        self.evaluate(rid)
        recs = self.recordings(rid)
        self.assertEqual([r.program_title for r in recs], ['PFL Football: Gulls at Bears'])
        rows = self.ledger(rid)
        spanish = rows[('Futbol PFL: Gulls vs. Bears', self.kickoff - timedelta(minutes=15))]
        other = rows[('PFL Football - Gulls at Bears Live', self.kickoff)]
        for row in (spanish, other):
            self.assertEqual(row.action_taken, RULE_MATCH_SKIPPED_EXISTING)
            self.assertTrue(row.detail.startswith('Same game, covered by the recording of '
                                                  '"PFL Football: Gulls at Bears"'), row.detail)

    def test_a_rule_that_names_nothing_records_every_listing_as_before(self):
        self.mnf()
        rid = self.rule()
        self.evaluate(rid)
        self.assertEqual(len(self.recordings(rid)), 3)

    def test_a_listing_twelve_hours_off_is_recorded_as_well(self):
        """A wrong time never costs a recording (DESIGN-sports-data.md 10)."""
        g, *_ = self.mnf()
        stray = self.channel('Overnight')
        self.listing(g, [stray], 'PFL Football Encore Gulls', self.kickoff - timedelta(hours=12))
        rid = self.rule(sports_league_id=self.league.id)
        self.evaluate(rid)
        self.assertEqual(sorted(r.program_title for r in self.recordings(rid)),
                         ['PFL Football Encore Gulls', 'PFL Football: Gulls at Bears'])

    def test_a_listing_running_past_the_recording_is_recorded_as_well(self):
        g = self.game()
        main = [self.channel('A'), self.channel('B')]
        self.listing(g, main, 'PFL Football: Gulls at Bears', self.kickoff)
        self.listing(g, [self.channel('Long')], 'PFL Gulls Marathon', self.kickoff, minutes=195 + 120)
        rid = self.rule(sports_league_id=self.league.id)
        self.evaluate(rid)
        self.assertEqual(len(self.recordings(rid)), 2)

    def test_a_team_rule_ignores_a_game_of_other_teams(self):
        self.game(start=self.kickoff + timedelta(days=7), tsdb_id=7008)   # the Gulls play later
        g = self.game(home=OTTERS, away=BEARS, tsdb_id=7009)
        self.listing(g, [self.channel('A')], 'PFL Football: Bears at Otters (Gulls preview)', self.kickoff)
        self.listing(g, [self.channel('B')], 'PFL Football: Bears at Otters Gulls', self.kickoff)
        rid = self.rule(sports_league_id=self.league.id, sports_team_tsdb_id=GULLS)
        self.evaluate(rid)
        self.assertEqual(len(self.recordings(rid)), 2)

    def test_a_keeper_a_person_cancelled_is_not_recorded_from_another_listing(self):
        self.mnf()
        rid = self.rule(sports_league_id=self.league.id)
        self.evaluate(rid)
        rec = self.recordings(rid)[0]
        auto_rules._cancel_scheduled(rec)
        db.session.commit()
        self.evaluate(rid)
        self.assertEqual(self.recordings(rid), [])
        rows = self.ledger(rid)
        spanish = rows[('Futbol PFL: Gulls vs. Bears', self.kickoff - timedelta(minutes=15))]
        self.assertEqual(spanish.action_taken, RULE_MATCH_SKIPPED_EXISTING)
        self.assertIn('you cancelled', spanish.detail)

    def test_a_weak_match_never_skips_a_listing(self):
        """A weak match is not a game a rule can stand behind (DESIGN-sports-data.md 10)."""
        g = self.game()
        self.listing(g, [self.channel('A'), self.channel('B')], 'PFL Football: Gulls at Bears', self.kickoff)
        self.listing(g, [self.channel('C')], 'PFL Gulls special', self.kickoff, strength=SPORTS_MATCH_WEAK)
        rid = self.rule(sports_league_id=self.league.id)
        self.evaluate(rid)
        self.assertEqual(len(self.recordings(rid)), 2)

    def test_with_no_schedule_the_rule_judges_on_the_guide_alone(self):
        """TheSportsDB down: no events, no matches, so nothing to read as a game."""
        g, *_ = self.mnf()
        SportsMatch.query.delete()
        db.session.commit()
        rid = self.rule(sports_league_id=self.league.id)
        self.evaluate(rid)
        self.assertEqual(len(self.recordings(rid)), 3)


class SkipReplayTests(_Case):

    def setUp(self):
        super().setUp()
        self.g = self.game()
        self.listing(self.g, [self.channel('Main')], 'PFL Football: Gulls at Bears', self.kickoff)
        self.replay_at = self.kickoff + timedelta(hours=5)

    def replay(self, airing=SPORTS_AIRING_REPLAY):
        self.listing(self.g, [self.channel('Replays')], 'PFL Rewind: Gulls at Bears', self.replay_at,
                     airing=airing)

    def replay_row(self, rid):
        return self.ledger(rid).get(('PFL Rewind: Gulls at Bears', self.replay_at))

    def test_a_replay_of_a_scheduled_first_airing_is_skipped(self):
        self.replay()
        rid = self.rule(sports_league_id=self.league.id, sports_skip_replays=True)
        self.evaluate(rid)
        row = self.replay_row(rid)
        self.assertEqual(row.action_taken, RULE_MATCH_SKIPPED_REPLAY)
        self.assertTrue(row.detail.startswith('The first airing, '), row.detail)
        self.assertEqual(len(self.recordings(rid)), 1)

    def test_with_the_option_off_the_replay_is_recorded(self):
        self.replay()
        rid = self.rule(sports_league_id=self.league.id)
        self.evaluate(rid)
        self.assertEqual(len(self.recordings(rid)), 2)

    def test_an_airing_in_doubt_is_never_skipped(self):
        self.replay(airing=SPORTS_AIRING_UNKNOWN)
        rid = self.rule(sports_league_id=self.league.id, sports_skip_replays=True)
        self.evaluate(rid)
        self.assertEqual(self.replay_row(rid).action_taken, RULE_MATCH_SCHEDULED)

    def test_a_failed_first_airing_frees_its_replay(self):
        self.replay()
        rid = self.rule(sports_league_id=self.league.id, sports_skip_replays=True)
        self.evaluate(rid)
        first = self.recordings(rid)[0]
        first.status = REC_STATUS_FAILED
        db.session.commit()
        self.evaluate(rid)
        self.assertEqual(self.replay_row(rid).action_taken, RULE_MATCH_SCHEDULED)

    def test_a_replay_with_no_first_airing_held_is_the_last_chance(self):
        """Nothing records the first airing, so the replay is recorded."""
        self.replay()
        rid = self.rule('grain=airings&q=Rewind', sports_league_id=self.league.id,
                        sports_skip_replays=True)
        self.evaluate(rid)
        self.assertEqual(self.replay_row(rid).action_taken, RULE_MATCH_SCHEDULED)


class GameAlertTests(_Case):

    def alerts(self, rid):
        db.session.expire_all()
        return Alert.query.filter_by(alert_type=AUTO_RULE_GAME_UNLISTED, source=f'auto-rule:{rid}:game',
                                     dismissed_at=None).all()

    def test_a_game_hours_away_that_nothing_lists_raises_one_alert_that_clears_itself(self):
        soon = self.game(start=_at(3), tsdb_id=7100)
        seed.make_epg_entry(self.channel('Filler'), title='Not a game', start_time=_at(1),
                            duration_minutes=60)
        rid = self.rule(sports_league_id=self.league.id)
        self.evaluate(rid)
        self.evaluate(rid)
        self.assertEqual(len(self.alerts(rid)), 1)
        self.assertIn('Harborview Gulls at Ironvale Bears', self.alerts(rid)[0].body)
        self.listing(soon, [self.channel('Main')], 'PFL Football: Gulls at Bears', soon.start_at)
        self.evaluate(rid)
        self.assertEqual(self.alerts(rid), [])

    def test_an_event_channel_the_rule_cannot_record_from_does_not_hold_the_alert_back(self):
        """A rule that does not record from event channels will still miss a game only an event
        channel names, and the alert says why rather than implying the channel helps."""
        soon = self.game(start=_at(3), tsdb_id=7103)
        db.session.add(SportsMatch(event_id=soon.id, channel_id=self.channel('Event 1').id,
                                   kind=SPORTS_MATCH_KIND_CHANNEL_NAME, title_key='pfl 1',
                                   strength=SPORTS_MATCH_STRONG, airing=SPORTS_AIRING_UNKNOWN,
                                   reason="Read from the channel's name."))
        db.session.commit()
        rid = self.rule(sports_league_id=self.league.id)
        self.evaluate(rid)
        alerts = self.alerts(rid)
        self.assertEqual(len(alerts), 1)
        self.assertIn('does not record from the event channel that does', alerts[0].body)

    def test_a_game_days_away_is_not_an_alert(self):
        self.game(start=_at(48), tsdb_id=7101)
        rid = self.rule(sports_league_id=self.league.id)
        self.evaluate(rid)
        self.assertEqual(self.alerts(rid), [])

    def test_pausing_the_rule_clears_it(self):
        self.game(start=_at(3), tsdb_id=7102)
        rid = self.rule(sports_league_id=self.league.id)
        self.evaluate(rid)
        self.assertEqual(len(self.alerts(rid)), 1)
        auto_rules.set_rule_enabled(rid, False)
        self.assertEqual(self.alerts(rid), [])


class MatcherTests(_Case):

    def test_a_match_run_over_a_named_league_asks_for_a_rule_check(self):
        self.game()
        self.rule(sports_league_id=self.league.id)
        with patch('app.auto_rules.queue_evaluation') as queued:
            sports_match.run(pause=0)
        queued.assert_called_once()

    def test_no_rule_naming_it_asks_for_none(self):
        self.game()
        self.rule()
        with patch('app.auto_rules.queue_evaluation') as queued:
            sports_match.run(pause=0)
        queued.assert_not_called()


class PageTests(_Case):

    def test_the_page_shows_the_games_the_game_line_and_the_chips(self):
        g, *_ = self.mnf()
        self.game(start=_at(24 * 6), home=GULLS, away=OTTERS, tsdb_id=7200)
        db.session.add(SportsState(id=1, last_match_at=datetime.utcnow()))   # the matcher has run
        db.session.commit()
        rid = self.rule(sports_league_id=self.league.id, sports_team_tsdb_id=GULLS,
                        sports_skip_replays=True)
        self.evaluate(rid)
        r = self.t.client.get(f'/rules/{rid}')
        self.assertEqual(r.status_code, 200)
        page = r.get_data(as_text=True)
        self.assertIn('data-section="games"', page)
        self.assertLess(page.index('data-section="games"'), page.index('data-section="coming"'))
        self.assertIn('rl-game', page)
        self.assertIn('Harborview Gulls at Ironvale Bears', page)
        self.assertIn('Saltmarsh Otters at Harborview Gulls', page)
        self.assertIn('first airing', page)
        self.assertIn('Not in your guide yet', page)
        self.assertIn('Skip game replays', page)
        self.assertIn('"sports_league_id": %d' % self.league.id, page.replace('&#34;', '"'))

    def test_a_rule_that_names_nothing_has_no_games_card(self):
        rid = self.rule()
        page = self.t.client.get(f'/rules/{rid}').get_data(as_text=True)
        self.assertNotIn('data-section="games"', page)
        self.assertNotIn('rl-game', page)

    def test_a_scheduled_game_reads_as_scheduled_on_its_row(self):
        self.mnf()
        rid = self.rule(sports_league_id=self.league.id)
        self.evaluate(rid)
        from app import auto_rules_view
        with self.t.app.test_request_context():
            ctx = auto_rules_view.page_context(rid)
        row = ctx['sports']['rows'][0]
        self.assertEqual(row['rule']['badge'][1].lower(), 'scheduled')
        self.assertEqual(len([s for s in row['rule']['subs'] if not isinstance(s, str)]), 0)


if __name__ == '__main__':
    unittest.main()


# ---------------------------------------------------------------------------
# Part B: recording from event channels (dev/changelog/1491)
# ---------------------------------------------------------------------------

class _EventCase(_Case):

    def named(self, event, channel, start=None, strength=SPORTS_MATCH_STRONG, name=None):
        """A channel whose name names the game; `start` is the clock its name gives with a time
        zone, None for a bare clock."""
        db.session.add(SportsMatch(
            event_id=event.id, channel_id=channel.id, kind=SPORTS_MATCH_KIND_CHANNEL_NAME,
            program_start_time=start, title_key=title_key(name or channel.name),
            program_title=name or channel.name, strength=strength, airing=SPORTS_AIRING_UNKNOWN,
            reason="Read from the channel's name, not a guide listing: the league named in it and both teams."))
        db.session.commit()

    def event_rule(self, search='grain=airings&q=Gulls', **kw):
        kw.setdefault('sports_league_id', self.league.id)
        kw.setdefault('sports_event_channels', True)
        return self.rule(search, **kw)

    def trust(self):
        self.league.trust_sample, self.league.trust_agreed = 12, 12
        db.session.commit()


class EventChannelTests(_EventCase):

    def test_a_game_with_no_listing_is_recorded_from_the_channel_naming_it(self):
        g = self.game()
        ch = self.channel('PFL | 01 - Gulls at Bears 8:15pm ET')
        self.named(g, ch, start=self.kickoff)
        rid = self.event_rule()
        self.evaluate(rid)
        recs = self.recordings(rid)
        self.assertEqual(len(recs), 1)
        rec = recs[0]
        self.assertEqual((rec.channel_id, rec.program_start_time, rec.program_title),
                         (ch.id, self.kickoff, 'Harborview Gulls at Ironvale Bears'))
        self.assertGreaterEqual(rec.program_stop_time - rec.program_start_time, timedelta(hours=4))
        row = RuleMatch.query.filter_by(rule_id=rid, recording_id=rec.id).one()
        self.assertEqual(row.sports_event_tsdb_id, g.tsdb_id)
        self.assertTrue(row.detail.startswith('Read from the channel name "PFL | 01'), row.detail)
        self.assertIsNotNone(db.session.get(RecordingSports, rec.id), 'linked to its game')

    def test_without_the_option_nothing_is_recorded_from_a_name(self):
        g = self.game()
        self.named(g, self.channel('PFL | 01'), start=self.kickoff)
        rid = self.event_rule(sports_event_channels=False)
        self.evaluate(rid)
        self.assertEqual(self.recordings(rid), [])

    def test_a_game_the_guide_lists_is_the_searchs_to_decide(self):
        """The tighter test: a search that leaves out every listing is not overruled by a name."""
        g = self.game()
        self.listing(g, [self.channel('Espanol')], 'Futbol PFL: Gulls vs. Bears', self.kickoff)
        self.named(g, self.channel('PFL | 01'), start=self.kickoff)
        rid = self.event_rule('grain=airings&q=Gulls+-Futbol')
        self.evaluate(rid)
        self.assertEqual(self.recordings(rid), [])

    def test_a_bare_clock_in_a_league_not_trusted_is_not_scheduled(self):
        g = self.game()
        self.named(g, self.channel('PFL | 01 - Gulls at Bears 8pm'))
        rid = self.event_rule()
        self.evaluate(rid)
        self.assertEqual(self.recordings(rid), [])
        from app import auto_rules_view
        with self.t.app.test_request_context():
            ctx = auto_rules_view.page_context(rid)
        self.assertEqual(ctx['sports']['rows'][0]['rule']['badge'][1], 'Not scheduled')

    def test_a_bare_clock_in_a_trusted_league_is_scheduled_at_thesportsdbs_time_unconfirmed(self):
        self.trust()
        g = self.game()
        self.named(g, self.channel('PFL | 01 - Gulls at Bears 8pm'))
        rid = self.event_rule()
        self.evaluate(rid)
        rec = self.recordings(rid)[0]
        self.assertEqual(rec.program_start_time, self.kickoff)
        row = RuleMatch.query.filter_by(rule_id=rid).one()
        self.assertIn('Start not confirmed', row.detail)

    def test_a_weak_name_or_a_duplicate_copy_is_never_recorded_from(self):
        g = self.game()
        self.named(g, self.channel('PFL maybe Gulls'), start=self.kickoff, strength=SPORTS_MATCH_WEAK)
        loser = self.channel('PFL | 01 copy')
        loser.is_duplicate_loser = True
        db.session.commit()
        self.named(g, loser, start=self.kickoff)
        rid = self.event_rule()
        self.evaluate(rid)
        self.assertEqual(self.recordings(rid), [])

    def test_the_searchs_account_and_day_filters_apply(self):
        g = self.game()
        self.named(g, self.channel('PFL | 01'), start=self.kickoff)
        other = seed.make_account(name='Other account')
        rid = self.event_rule(f'grain=airings&q=Gulls&f.acct={other.id}')
        self.evaluate(rid)
        self.assertEqual(self.recordings(rid), [])
        from app.channel_search import DOW_VALUES
        from app.tz_utils import to_local
        wrong_day = DOW_VALUES[(to_local(self.kickoff).weekday() + 1) % 7]
        rid2 = self.event_rule(f'grain=airings&q=Gulls&f.dow={wrong_day}', name='Other day')
        self.evaluate(rid2)
        self.assertEqual(self.recordings(rid2), [])

    def test_a_cancelled_event_pick_stays_cancelled(self):
        g = self.game()
        self.named(g, self.channel('PFL | 01'), start=self.kickoff)
        rid = self.event_rule()
        self.evaluate(rid)
        auto_rules._cancel_scheduled(self.recordings(rid)[0])
        db.session.commit()
        self.evaluate(rid)
        self.assertEqual(self.recordings(rid), [])
        # Decided by its ledger row, found by the game - not by a create the anchor refuses.
        self.assertFalse(db.session.get(AutoRule, rid).last_problems)
        self.assertEqual(RuleMatch.query.filter_by(rule_id=rid).count(), 1)

    def test_a_cancelled_event_pick_covers_the_listing_that_arrives_later(self):
        """A person's cancellation sticks across the two paths (R6)."""
        g = self.game()
        self.named(g, self.channel('PFL | 01'), start=self.kickoff)
        rid = self.event_rule()
        self.evaluate(rid)
        auto_rules._cancel_scheduled(self.recordings(rid)[0])
        db.session.commit()
        self.listing(g, [self.channel('Main')], 'PFL Football: Gulls at Bears', self.kickoff)
        self.evaluate(rid)
        self.assertEqual(self.recordings(rid), [])
        row = self.ledger(rid)[('PFL Football: Gulls at Bears', self.kickoff)]
        self.assertIn('you cancelled', row.detail)

    def test_a_cancelled_listing_pick_is_not_recorded_from_an_event_channel_once_the_listing_goes(self):
        g = self.game()
        self.listing(g, [self.channel('Main')], 'PFL Football: Gulls at Bears', self.kickoff)
        rid = self.event_rule()
        self.evaluate(rid)
        auto_rules._cancel_scheduled(self.recordings(rid)[0])
        SportsMatch.query.filter_by(kind=SPORTS_MATCH_KIND_GUIDE).update({'gone_since': datetime.utcnow()})
        db.session.commit()
        self.named(g, self.channel('PFL | 01'), start=self.kickoff)
        self.evaluate(rid)
        self.assertEqual(self.recordings(rid), [])

    def test_tonights_event_game_is_not_capped_behind_next_weeks_listing(self):
        """Event games join the listings in start order (§4.3)."""
        tonight = self.game(start=_at(5), tsdb_id=7300)
        later = self.game(start=_at(24 * 7), tsdb_id=7301)
        self.named(tonight, self.channel('PFL | 01'), start=tonight.start_at)
        self.listing(later, [self.channel('Main')], 'PFL Football: Gulls at Bears', later.start_at)
        rid = self.event_rule(max_upcoming=1)
        self.evaluate(rid)
        recs = self.recordings(rid)
        self.assertEqual([r.program_start_time for r in recs], [tonight.start_at])

    def test_the_guide_listing_that_arrives_later_is_covered_by_the_event_recording(self):
        g = self.game()
        self.named(g, self.channel('PFL | 01'), start=self.kickoff)
        rid = self.event_rule()
        self.evaluate(rid)
        self.listing(g, [self.channel('Main')], 'PFL Football: Gulls at Bears', self.kickoff)
        self.evaluate(rid)
        self.assertEqual(len(self.recordings(rid)), 1)
        row = self.ledger(rid)[('PFL Football: Gulls at Bears', self.kickoff)]
        self.assertEqual(row.action_taken, RULE_MATCH_SKIPPED_EXISTING)

    def test_a_channel_that_stops_naming_the_game_is_said_once_and_the_slot_kept(self):
        g = self.game()
        ch = self.channel('PFL | 01')
        self.named(g, ch, start=self.kickoff)
        rid = self.event_rule()
        self.evaluate(rid)
        rec_id = self.recordings(rid)[0].id
        SportsMatch.query.filter_by(kind=SPORTS_MATCH_KIND_CHANNEL_NAME).update({'gone_since': datetime.utcnow()})
        db.session.commit()
        self.evaluate(rid)
        self.evaluate(rid)
        rule = db.session.get(AutoRule, rid)
        self.assertIn('no longer names the game', rule.last_problems or '')
        rec = db.session.get(Recording, rec_id)
        self.assertEqual(rec.program_start_time, self.kickoff)
        notes = RecordingEvent.query.filter_by(recording_id=rec_id,
                                               event_type=RECORDING_RULE_GAME_CHANGED).all()
        self.assertEqual(len(notes), 1)
        self.assertEqual(json.loads(notes[0].extra_data)['reason'], 'renamed')

    def test_turning_the_option_off_asks_about_its_scheduled_picks(self):
        g = self.game()
        self.named(g, self.channel('PFL | 01'), start=self.kickoff)
        rid = self.event_rule()
        self.evaluate(rid)
        rec_id = self.recordings(rid)[0].id
        r = self.t.client.put(f'/api/auto-rules/{rid}', json={'sports_event_channels': False})
        self.assertEqual(r.status_code, 409)
        body = r.get_json()
        self.assertEqual(body['what'], 'settings')
        self.assertEqual([u['id'] for u in body['unmatched']], [rec_id])
        r = self.t.client.put(f'/api/auto-rules/{rid}', json={
            'sports_event_channels': False, 'unmatched': {'action': 'cancel', 'ids': [rec_id]}})
        self.assertEqual(r.status_code, 200, r.get_json())
        db.session.expire_all()
        self.assertIsNone(db.session.get(Recording, rec_id))

    def test_the_page_says_where_an_event_pick_came_from(self):
        g = self.game()
        self.named(g, self.channel('PFL | 01'), start=self.kickoff)
        rid = self.event_rule()
        self.evaluate(rid)
        page = self.t.client.get(f'/rules/{rid}').get_data(as_text=True)
        self.assertIn('From an event channel', page)
        self.assertIn('From the event channel &#34;PFL | 01&#34;, at the time its name gives.', page)
        self.assertIn('Event channels', page)
        self.assertIn('its account, day and time filters apply to those channels', page)


class EventAlertTests(_EventCase):

    def alerts(self, rid):
        db.session.expire_all()
        return Alert.query.filter_by(alert_type=AUTO_RULE_GAME_UNLISTED, source=f'auto-rule:{rid}:game',
                                     dismissed_at=None).all()

    def test_a_name_the_rule_cannot_record_from_does_not_hold_the_alert_back(self):
        """With the option on, only a channel the rule would actually record from holds it back: not
        a weak name, and not a bare clock in a league whose times are not trusted."""
        soon = self.game(start=_at(3), tsdb_id=7400)
        self.named(soon, self.channel('PFL maybe'), start=soon.start_at, strength=SPORTS_MATCH_WEAK)
        self.named(soon, self.channel('PFL | 01 8pm'))
        rid = self.event_rule('grain=airings&q=zzz')
        self.evaluate(rid)
        alerts = self.alerts(rid)
        self.assertEqual(len(alerts), 1)
        self.assertIn('no event channel naming it is one this rule can record from', alerts[0].body)

    def test_a_name_it_records_from_clears_it(self):
        soon = self.game(start=_at(3), tsdb_id=7401)
        self.named(soon, self.channel('PFL | 01'), start=soon.start_at)
        rid = self.event_rule('grain=airings&q=zzz')
        self.evaluate(rid)
        self.assertEqual(self.alerts(rid), [])
        self.assertEqual(len(self.recordings(rid)), 1)


class StartFilterTests(_Case):
    """channel_search.start_passes() is the Python reading of the When, Day of week and Time of
    day predicates, held equal to the SQL over seeded rows."""

    def test_the_python_reading_agrees_with_the_search(self):
        from app import channel_search
        ch = self.channel('Times')
        starts = [_at(h) for h in (2, 9, 20, 30, 47, 70, 100, 140)] + [
            (datetime.utcnow() - timedelta(minutes=10)).replace(second=0, microsecond=0)]
        for i, st in enumerate(starts):
            seed.make_epg_entry(ch, title=f'Clock {i}', start_time=st, duration_minutes=30)
        db.session.commit()
        ctx = channel_search.SearchContext.build(self.t.app.config.get('CHANNELBIN_CONFIG') or {})
        from app.tz_utils import to_local
        lo = to_local(_at(8)).strftime('%Y-%m-%dT%H:%M')
        hi = to_local(_at(72)).strftime('%Y-%m-%dT%H:%M')
        for extra in ('f.when=now', f'f.when=custom:{lo}..{hi}', f'f.when=custom:..{hi}',
                      'f.when=today', 'f.when=tomorrow', 'f.when=next:3:days', 'f.dow=sat',
                      'x.dow=sun', 'f.tod=tod:18:00..23:30', 'f.tod=tod:22:00..06:00', 'f.tod=tod:..09:00',
                      'f.dow=mon&f.dow=tue&f.tod=tod:12:00..'):
            state = auto_rules.parse_search(f'grain=airings&q=Clock&{extra}')
            got = {r.start_time for r in channel_search.search(
                state, ctx, want_counts=False, want_facets=False).rows}
            mine = {st for st in starts if channel_search.start_passes(state, st, ctx)}
            self.assertEqual(mine, got, extra)

    def test_the_account_reading_agrees_with_the_search(self):
        from app import channel_search
        other = seed.make_account(name='Other')
        mine_ch, theirs = self.channel('Mine'), seed.make_channel(other, stream_id=9999, name='Theirs')
        for ch in (mine_ch, theirs):
            seed.make_epg_entry(ch, title='Acct show', start_time=_at(5), duration_minutes=30)
        db.session.commit()
        ctx = channel_search.SearchContext.build({})
        for extra in (f'f.acct={self.acc.id}', f'x.acct={self.acc.id}', f'f.acct={self.acc.id}&f.acct={other.id}'):
            state = auto_rules.parse_search(f'grain=airings&q=Acct&{extra}')
            got = {r.channel_id for r in channel_search.search(
                state, ctx, want_counts=False, want_facets=False).rows}
            accounts = {mine_ch.id: self.acc.id, theirs.id: other.id}
            mine = {cid for cid, aid in accounts.items() if channel_search.account_passes(state, aid)}
            self.assertEqual(mine, got, extra)


class EventSearchChangeTests(_EventCase):

    def test_a_new_search_whose_day_filter_drops_an_event_pick_asks_about_it(self):
        from app.channel_search import DOW_VALUES
        from app.tz_utils import to_local
        g = self.game()
        self.named(g, self.channel('PFL | 01'), start=self.kickoff)
        rid = self.event_rule()
        self.evaluate(rid)
        rec_id = self.recordings(rid)[0].id
        wrong_day = DOW_VALUES[(to_local(self.kickoff).weekday() + 1) % 7]
        found = auto_rules.unmatched_by_search(rid, f'grain=airings&q=Gulls&f.dow={wrong_day}')
        self.assertEqual([r['id'] for r in found.recordings], [rec_id])
        self.assertEqual(auto_rules.unmatched_by_search(rid, 'grain=airings&q=Bears').recordings, [],
                         'a text change does not apply to an event channel')
