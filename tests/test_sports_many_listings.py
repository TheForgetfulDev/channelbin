"""A game carried on more channels than a rule's search may act on is still picked and played
(dev/docs/BUGS.md 2026-11-22 @ 08:20:00 PM, dev/changelog/1691).

On prod a Sunday night network game was listed on 1,722 channels (every local station on four
accounts). game_sources() read the game's listings through the rule search's 1,000-showing cap,
got "too many", and answered no channel: the sports channel carrying it answered every tune-in
with a 409, and Record on the league page had nothing to record. The cap is shrunk here (4
showings, pages of 2) so seven channels are past it.

Made-up league and teams; a throwaway temp SQLite database.

  python3 -m unittest tests.test_sports_many_listings
"""
import json
import os
import sys
import unittest
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tests.support.app import make_test_app  # noqa: E402
from tests.support.seed import make_account, make_channel, make_guide_match  # noqa: E402

import app.output_feed as feed  # noqa: E402
from app import admission, auto_rules, channel_search, db, sports_feed  # noqa: E402
from app import connection_limits as connlim  # noqa: E402
from app.config import load_config  # noqa: E402
from app.database import SportsEvent, SportsFeedChannel, SportsFollow, SportsLeague  # noqa: E402

CHANNELS = 7


class ManyListingsTests(unittest.TestCase):

    def setUp(self):
        self.t = make_test_app(csrf=False)
        self.addCleanup(self.t.cleanup)
        self.addCleanup(admission.reset_for_tests)
        self.addCleanup(feed.stop_all)
        self.t.sandbox_config({'integrations': {'output_feed': {'enabled': True, 'username': 'u', 'password': 'p',
                                                                'mode': 'redirect'}},
                               'display': {'timezone': 'America/New_York', 'time_format': '12h'}})
        for patch in (mock.patch.object(auto_rules, 'MAX_SHOWINGS', 4),
                      mock.patch.object(channel_search, 'MAX_PAGE_SIZE', 2)):
            patch.start()
            self.addCleanup(patch.stop)
        connlim._holders.clear()
        self.addCleanup(connlim._holders.clear)
        self.now = datetime.utcnow().replace(second=0, microsecond=0)
        start = self.now - timedelta(minutes=30)
        league = SportsLeague(tsdb_id=9001, name='Pretend League', sport='Pretend Ball', current_season='2099',
                              match_terms=json.dumps(['PFL']))
        db.session.add(league)
        db.session.flush()
        db.session.add(SportsFollow(league_id=league.id, team_tsdb_id=0, rule_id=0))
        self.event = SportsEvent(tsdb_id=7001, league_id=league.id, name='Ironvale Bears vs Harborview Gulls',
                                 home_team_id=101, away_team_id=102, home_name='Ironvale Bears',
                                 away_name='Harborview Gulls', start_at=start, guide_start=start,
                                 local_date=start.date())
        db.session.add(self.event)
        db.session.flush()
        self.best = None
        for n in range(CHANNELS):
            ch = make_channel(make_account(f'Account {n}', max_connections=1), stream_id=500 + n,
                              name=f'Station {n}')
            ch.health_score = 50 + n
            self.best = ch
            make_guide_match(self.event, ch, start)
        db.session.commit()
        self.league_id = league.id

    def test_record_picks_a_channel_past_the_rule_cap(self):
        sources = auto_rules.game_sources(self.event.id, now=self.now)
        self.assertEqual(sources.pick.kind, auto_rules.PICK_LIVE)
        self.assertEqual(sources.pick.showing.channel_id, self.best.id)
        self.assertEqual(sources.pick.channels, CHANNELS)

    def test_the_sports_channel_plays_it(self):
        self.assertTrue(sports_feed.set_team_channel(self.league_id, 101, True))
        feed_id = feed.feed_id_for_sports(SportsFeedChannel.query.one().id)
        db.session.expire_all()
        play = feed.resolve_play(feed_id, now=self.now)
        self.assertIsNotNone(play.member, play.why_none)
        self.assertEqual(play.member.id, self.best.id)

    def test_a_rules_search_is_still_capped(self):
        """The cap still stands where it means something: a rule's search past it is refused."""
        state = channel_search.SearchState(grain=channel_search.GRAIN_AIRINGS, q='PFL')
        ctx = channel_search.SearchContext.build(load_config())
        entries, _wins = auto_rules._search_rule(state, SimpleNamespace(ctx=ctx))
        self.assertIsNone(entries)


if __name__ == '__main__':
    unittest.main()
