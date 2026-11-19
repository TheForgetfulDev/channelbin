"""A game's recording and the sports channel playing it take the same channel (dev/changelog/1637;
dev/docs/BUGS.md 2026-11-19 @ 01:17:08 AM). An auto-record rule, a league page's Record and a sports
channel on the IPTV output answer "which channel is this game on" through one reader,
auto_rules.game_sources(), so in the same situation they land on the same channel.

Compared at one moment: the game is on now. The recording's side is what Record would pick if
pressed now (auto_rules.game_pick), the showing its seat choice keeps (auto_rules.seat_choice), and
for a channel group the member channel_groups.serving_member() names, which is the member record
start takes while no account is busy (the one place the two may differ, said in the changelog).
The sports channel's side is the member resolve_play() hands a tune-in.

Made-up league and teams; a throwaway temp SQLite database.

  python3 -m unittest tests.test_sports_same_channel
"""
import json
import os
import sys
import unittest
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tests.support.app import make_test_app  # noqa: E402
from tests.support.seed import (make_account, make_channel, make_channel_test, make_group,  # noqa: E402
                                make_guide_match, make_recording)

import app.output_feed as feed  # noqa: E402
from app import account_blocks, admission, auto_rules, db, sports_feed  # noqa: E402
from app.channel_groups import blocked_account_ids_now, recording_members, serving_member  # noqa: E402
from app.config import load_config  # noqa: E402
from app.database import (REC_STATUS_SCHEDULED, ChannelGroup, SportsEvent, SportsFeedChannel,  # noqa: E402
                          SportsFollow, SportsLeague)
from app.routes.channel_tests import _latest_tests_by_channel  # noqa: E402


class SameChannelTests(unittest.TestCase):
    """One game on now, listed at its believed start, and a team channel for its home team."""

    def setUp(self):
        self.t = make_test_app(csrf=False)
        self.addCleanup(self.t.cleanup)
        self.addCleanup(admission.reset_for_tests)
        self.addCleanup(feed.stop_all)
        self.t.sandbox_config({'integrations': {'output_feed': {'enabled': True, 'username': 'u', 'password': 'p',
                                                                'mode': 'redirect'}},
                               'display': {'timezone': 'America/New_York', 'time_format': '12h'}})
        self.now = datetime.utcnow().replace(second=0, microsecond=0)
        self.start = self.now - timedelta(minutes=30)
        league = SportsLeague(tsdb_id=9001, name='Pretend League', sport='Pretend Ball', current_season='2099',
                              match_terms=json.dumps(['PFL']))
        db.session.add(league)
        db.session.flush()
        db.session.add(SportsFollow(league_id=league.id, team_tsdb_id=0, rule_id=0))
        self.event = SportsEvent(tsdb_id=7001, league_id=league.id, name='Harborview Gulls vs Ironvale Bears',
                                 home_team_id=101, away_team_id=102, home_name='Ironvale Bears',
                                 away_name='Harborview Gulls', start_at=self.start, guide_start=self.start,
                                 local_date=self.start.date())
        db.session.add(self.event)
        db.session.commit()
        self.assertTrue(sports_feed.set_team_channel(league.id, 101, True))
        self.feed_id = feed.feed_id_for_sports(SportsFeedChannel.query.one().id)
        self._n = 0

    def channel(self, score, account=None):
        self._n += 1
        ch = make_channel(account or make_account(f'Account {self._n}', max_connections=1),
                          stream_id=500 + self._n, name=f'Local {self._n}')
        ch.health_score = score
        db.session.flush()
        return ch

    def listed(self, *channels, start=None):
        for ch in channels:
            make_guide_match(self.event, ch, start or self.start)
        db.session.commit()

    def recording_takes(self):
        """The channel a recording of the game made now starts on."""
        db.session.expire_all()
        cfg = load_config()
        pick = auto_rules.game_pick(self.event.id, self.now, cfg)
        self.assertEqual(pick.kind, auto_rules.PICK_LIVE, pick)
        showing = auto_rules.seat_choice(pick.program, 'default', cfg).showing
        if showing.group_id is None:
            return showing.channel_id
        group = db.session.get(ChannelGroup, showing.group_id)
        members = recording_members(group.memberships)
        return serving_member(group, _latest_tests_by_channel([m.id for m in members]),
                              blocked_account_ids=blocked_account_ids_now()).member.id

    def sports_channel_plays(self, exclude=()):
        db.session.expire_all()
        play = feed.resolve_play(self.feed_id, exclude_ids=frozenset(exclude), now=self.now)
        self.assertIsNotNone(play.member, play.why_none)
        return play.member.id

    def assertSame(self, expected):
        self.assertEqual(self.recording_takes(), expected.id, 'the recording')
        self.assertEqual(self.sports_channel_plays(), expected.id, 'the sports channel')

    def test_a_channel_group_is_taken_before_a_healthier_single_channel(self):
        """A rule's simulcast order puts a group before a single channel (DESIGN-autoschedule.md
        3.7); the sports channel ranked bare channels by health and played the single one."""
        single = self.channel(95)
        best, other = self.channel(80), self.channel(70)
        make_group('Locals', members=[best, other])
        self.listed(single, best, other)
        self.assertSame(best)

    def test_the_listing_at_the_games_start_beats_one_on_more_channels(self):
        """keepers()' order (dev/changelog/1625): the one channel listing the game at its believed
        start beats three listing it 90 minutes early. The sports channel pooled all four and
        played the healthiest."""
        near = self.channel(40)
        wide = [self.channel(90) for _ in range(3)]
        self.listed(near)
        self.listed(*wide, start=self.start - timedelta(minutes=90))
        self.assertSame(near)

    def test_an_account_block_moves_both_to_the_same_member(self):
        """A block is dropped where a member is chosen (app/account_blocks.py): the group's best
        member is on a blocked account, so both take its next member, not a single channel."""
        blocked_best, member = self.channel(90), self.channel(60)
        single = self.channel(85)
        make_group('Locals', members=[blocked_best, member])
        self.listed(blocked_best, member, single)
        account_blocks.add_account_block(blocked_best.account_id, stop=self.now + timedelta(hours=4),
                                         start=self.now - timedelta(hours=1))
        self.assertSame(member)

    def test_a_member_with_recording_off_is_never_played(self):
        """A group's Recording switch is the person's answer to "would I record from this feed";
        the sports channel read the match on the channel and played it anyway."""
        off, on = self.channel(99), self.channel(60)
        make_group('Locals', members=[off, on], disabled={off.id})
        self.listed(off, on)
        self.assertSame(on)

    def test_a_full_account_goes_to_the_same_next_channel(self):
        """The order past a first choice whose account has no connection: latest health check
        passed before never checked (app/seat_moves.py), whatever the scores. The sports channel
        walked down by health."""
        first = self.channel(95)
        untested = self.channel(80)
        passed = self.channel(50)
        make_channel_test(passed, all_null=False, status='COMPLETED')
        self.listed(first, untested, passed)
        busy = make_channel(first.account, stream_id=999, name='Busy')
        make_recording(status=REC_STATUS_SCHEDULED, name='Busy', channel_id=busy.id,
                       start_time=self.now - timedelta(hours=1), stop_time=self.now + timedelta(hours=4))
        db.session.commit()
        self.assertEqual(self.recording_takes(), passed.id, 'the recording moves off the full account')
        # A play refused its seat asks again without the channel it was refused on (_seat_for).
        self.assertEqual(self.sports_channel_plays(), first.id)
        self.assertEqual(self.sports_channel_plays(exclude={first.id}), passed.id, 'the sports channel')

    def test_once_every_channel_of_the_pick_failed_the_play_takes_the_games_other_listing(self):
        """A recording never leaves its listing; a play walks on to the game's other listings in
        keepers' order rather than end while another channel still carries the game."""
        near = self.channel(40)
        early = self.channel(90)
        self.listed(near)
        self.listed(early, start=self.start - timedelta(minutes=90))
        self.assertSame(near)
        self.assertEqual(self.sports_channel_plays(exclude={near.id}), early.id)

    def test_a_group_kept_on_a_weakly_matched_member_is_still_the_groups(self):
        """The search keeps one member's listing for a group, and the healthier member's match can be
        the weak one. Record still takes the group; the play held only strong listings and so lost
        the group to the single channel."""
        strong, weak = self.channel(50), self.channel(90)
        single = self.channel(95)
        make_group('Locals', members=[strong, weak])
        make_guide_match(self.event, weak, self.start, strength='weak')
        self.listed(strong, single)
        self.assertSame(weak)

    def test_a_listing_record_would_not_pick_is_refused_by_name(self):
        """One listing matched to two of the league's games at once is a question Record does not
        answer (GameIndex._one), so the sports channel does not play it either, and says why."""
        other = SportsEvent(tsdb_id=7002, league_id=self.event.league_id, name='Northgate Panthers vs Eastport Owls',
                            home_team_id=103, away_team_id=104, home_name='Eastport Owls',
                            away_name='Northgate Panthers', start_at=self.start, local_date=self.start.date())
        db.session.add(other)
        db.session.flush()
        ch = self.channel(90)
        make_guide_match(other, ch, self.start)
        self.listed(ch)
        self.assertEqual(auto_rules.game_pick(self.event.id, self.now).kind, auto_rules.PICK_NONE)
        play = feed.resolve_play(self.feed_id, now=self.now)
        self.assertIsNone(play.member)
        self.assertIn('on 1 channel now, but none of those listings is one Record would pick for it',
                      play.why_none)

    def test_a_channel_removed_as_the_play_is_worked_out_is_named_so(self):
        """Not "already failed this play", which nothing did."""
        from types import SimpleNamespace
        gone = SimpleNamespace(channel_id=987654, group_id=None)
        self.assertEqual(feed._sports_member([gone], set(), None, frozenset()), (None, feed._DROP_GONE, None))

    def test_the_answer_for_now_takes_nothing(self):
        """game_sources() is what a page asks for "if it started now": no seat, no ticket, no row."""
        from app import connection_limits as connlim
        from app.database import Recording
        ch = self.channel(90)
        self.listed(ch)
        before = Recording.query.count()
        found = auto_rules.game_sources(self.event.id, self.now)
        self.assertEqual([s.channel_id for s in found.sources], [ch.id])
        self.assertEqual(Recording.query.count(), before)
        self.assertEqual(connlim._holders.get(ch.account_id, []), [])
        self.assertEqual(admission.active_kinds(), set())


if __name__ == '__main__':
    unittest.main()
