"""ChannelBin's own sports channels on the output feed (app/sports_feed.py, app/output_feed.py,
app/routes/sports.py, dev/changelog/1517 and 1518).

Covers which channels are served (a team's switch, the follow it needs, a row and its feed id
kept through off and on, a league's teardown), the guide (a game's time from the guide's own
listing, the note when nothing lists it, what has no time), a play (only a channel whose guide
lists the game now, ranked by health, refused by name with no alert when nothing is on), a
league's numbered channels (placed once, never moved, only growing), the league page's card and
the dashboard's refused plays. Made-up leagues and teams; a throwaway temp SQLite database.

  python3 -m unittest tests.test_sports_feed
"""
import json
import os
import sys
import unittest
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tests.support.app import make_test_app  # noqa: E402
from tests.support.seed import make_account, make_channel  # noqa: E402

import app.output_feed as feed  # noqa: E402
from app import db, sports_data, sports_feed, sports_match  # noqa: E402
from app.database import (Alert, SportsEvent, SportsFeedChannel, SportsFollow,  # noqa: E402
                          SportsLeague, SportsMatch, SportsTeam)

USER, PASS = 'tv', 'tv-secret'
BEARS, GULLS, PANTHERS, COMETS = 101, 102, 103, 104
TEAMS = {BEARS: 'Ironvale Bears', GULLS: 'Harborview Gulls', PANTHERS: 'Northgate Panthers',
         COMETS: 'Saint-Remy Comets'}


def _feed_cfg(mode='redirect', **overrides):
    block = {'enabled': True, 'username': USER, 'password': PASS, 'reserve_seconds': 0, 'mode': mode}
    block.update(overrides)
    return {'integrations': {'output_feed': block},
            'display': {'timezone': 'America/New_York', 'time_format': '12h'}}


class _Case(unittest.TestCase):
    """A league followed whole, four teams, and one account. `now` is the top of the minute."""

    def setUp(self):
        self.t = make_test_app(csrf=False)
        self.client = self.t.client
        self.addCleanup(self.t.cleanup)
        self.addCleanup(feed.stop_all)
        self.t.sandbox_config(_feed_cfg())
        self.now = datetime.utcnow().replace(second=0, microsecond=0)
        self.league = SportsLeague(tsdb_id=9001, name='Pretend League', sport='Pretend Ball',
                                   current_season='2099', match_terms=json.dumps(['PFL']))
        db.session.add(self.league)
        db.session.flush()
        db.session.add(SportsFollow(league_id=self.league.id, team_tsdb_id=0, rule_id=0))
        for tsdb_id, name in TEAMS.items():
            db.session.add(SportsTeam(tsdb_id=tsdb_id, full_name=name, names=json.dumps([name])))
        self.account = make_account('Acct', max_connections=1)
        self._tsdb = 7000
        db.session.commit()

    def event(self, home, away, start, **kw):
        self._tsdb += 1
        e = SportsEvent(tsdb_id=self._tsdb, league_id=self.league.id, name=f'{TEAMS[away]} vs {TEAMS[home]}',
                        home_team_id=home, away_team_id=away, home_name=TEAMS[home], away_name=TEAMS[away],
                        start_at=start, local_date=start.date(), **kw)
        db.session.add(e)
        db.session.flush()
        return e

    def channel(self, name, score=None, account=None, **kw):
        ch = make_channel(account or self.account, name=name, **kw)
        ch.health_score = score
        db.session.flush()
        return ch

    def listing(self, event, channel, start=None, hours=3.0, **kw):
        start = start or event.start_at
        fields = dict(kind='guide', strength='strong', airing='first', title_key='pfl football',
                      program_title='PFL Football', reason='Matched on the league and both teams.')
        fields.update(kw)
        m = SportsMatch(event_id=event.id, channel_id=channel.id, program_start_time=start,
                        program_stop_time=start + timedelta(hours=hours), **fields)
        db.session.add(m)
        db.session.flush()
        return m

    def team_on(self, team=BEARS):
        self.assertTrue(sports_feed.set_team_channel(self.league.id, team, True))
        return SportsFeedChannel.query.filter_by(league_id=self.league.id, team_tsdb_id=team).one()

    def get(self, path, **params):
        params.setdefault('username', USER)
        params.setdefault('password', PASS)
        return self.client.get(f'{path}?' + '&'.join(f'{k}={v}' for k, v in params.items()))

    def play(self, feed_id):
        return self.client.get(f'/live/{USER}/{PASS}/{feed_id}.ts')

    def nothing_on(self, feed_id):
        """Why `feed_id` has nothing to play, asserting that is so and not a fault. A tune-in then
        gets the channel's waiting screen (tests/test_sports_waiting.py); the clause is what the
        refusal said before that screen existed, and still says when the screen cannot be made."""
        play = feed.resolve_play(feed_id)
        self.assertIsNotNone(play)
        self.assertIsNone(play.member, play)
        self.assertFalse(play.fault, play.why_none)
        return play.why_none

    def streams(self):
        return self.get('/player_api.php', action='get_live_streams').get_json()


class FeedIdTests(unittest.TestCase):
    def test_a_sports_id_decodes_and_sits_clear_of_channels_and_groups(self):
        self.assertEqual(feed.decode_feed_id(feed.feed_id_for_sports(7)), (feed.KIND_SPORTS, 7))
        self.assertLess(feed.feed_id_for_sports(1_000_000), 2 ** 31)
        for row_id in (1, 2, 85_598, 10_000_000, 499_999_999):
            self.assertEqual(feed.decode_feed_id(feed.feed_id_for_channel(row_id)), (feed.KIND_CHANNEL, row_id))
            self.assertEqual(feed.decode_feed_id(feed.feed_id_for_group(row_id)), (feed.KIND_GROUP, row_id))


class ServedTests(_Case):
    def test_nothing_is_served_until_a_switch_is_on(self):
        self.event(BEARS, GULLS, self.now + timedelta(hours=2))
        db.session.commit()
        self.assertEqual(self.streams(), [])
        self.assertEqual(sports_feed.served_channels(), [])

    def test_a_teams_channel_is_named_for_the_team_in_its_leagues_category(self):
        own = make_channel(self.account, name='News', in_guide=True, category_name='US News')
        db.session.commit()
        row = self.team_on(BEARS)
        streams = self.streams()
        self.assertEqual([s['name'] for s in streams], ['News', 'Ironvale Bears'])
        bears = streams[1]
        self.assertEqual(bears['stream_id'], feed.SPORTS_FEED_BASE + row.id)
        self.assertEqual(bears['epg_channel_id'], str(feed.SPORTS_FEED_BASE + row.id))
        self.assertEqual(bears['num'], 2)
        cats = {c['category_id']: c['category_name']
                for c in self.get('/player_api.php', action='get_live_categories').get_json()}
        self.assertEqual(cats[bears['category_id']], 'Pretend League')
        self.assertEqual(streams[0]['stream_id'], feed.feed_id_for_channel(own.id))
        self.assertIn(f'tvg-id="{bears["stream_id"]}"', self.get('/get.php').get_data(as_text=True))

    def test_off_and_on_again_is_the_same_feed_id(self):
        row = self.team_on(BEARS)
        first = row.id
        sports_feed.set_team_channel(self.league.id, BEARS, False)
        self.assertEqual(self.streams(), [])
        self.assertEqual(SportsFeedChannel.query.count(), 1, 'the row is kept: its id is the feed id')
        sports_feed.set_team_channel(self.league.id, BEARS, True)
        self.assertEqual([s['stream_id'] for s in self.streams()], [feed.SPORTS_FEED_BASE + first])

    def test_a_team_nothing_follows_is_not_served_and_comes_back_with_its_id(self):
        row = self.team_on(BEARS)
        sports_data.set_following(self.league.id, [GULLS])
        self.assertEqual(self.streams(), [], 'the follow ended, so the channel is not served')
        self.assertEqual(self.play(feed.SPORTS_FEED_BASE + row.id).status_code, 404)
        sports_data.set_following(self.league.id, [BEARS])
        self.assertEqual([s['stream_id'] for s in self.streams()], [feed.SPORTS_FEED_BASE + row.id])

    def test_a_rules_follow_counts_as_followed(self):
        SportsFollow.query.delete()
        db.session.add(SportsFollow(league_id=self.league.id, team_tsdb_id=BEARS, rule_id=5))
        db.session.commit()
        self.team_on(BEARS)
        self.assertEqual([s['name'] for s in self.streams()], ['Ironvale Bears'])

    def test_the_switch_is_refused_for_a_team_nothing_follows(self):
        sports_data.set_following(self.league.id, [GULLS])
        resp = self.client.put(f'/api/sports/leagues/{self.league.id}/feed-channels',
                               json={'team_id': BEARS, 'enabled': True})
        self.assertEqual(resp.status_code, 409)
        self.assertIn('Follow that team first', resp.get_json()['error'])
        self.assertEqual(SportsFeedChannel.query.count(), 0)

    def test_a_made_up_team_gets_no_channel(self):
        """A row is kept for good, so the route cannot be used to mint one per made-up id."""
        self.event(BEARS, GULLS, self.now + timedelta(hours=2))
        db.session.commit()
        url = f'/api/sports/leagues/{self.league.id}/feed-channels'
        resp = self.client.put(url, json={'team_id': 99999999, 'enabled': True})
        self.assertEqual(resp.status_code, 409)
        self.assertIn('has not read that team', resp.get_json()['error'])
        self.assertEqual(SportsFeedChannel.query.count(), 0)
        self.assertEqual(self.client.put(url, json={'team_id': GULLS, 'enabled': True}).status_code, 200)

    def test_the_route_turns_a_team_on_and_off_and_validates(self):
        url = f'/api/sports/leagues/{self.league.id}/feed-channels'
        self.assertEqual(self.client.put(url, json={'team_id': BEARS, 'enabled': True}).get_json(),
                         {'success': True})
        self.assertEqual([s['name'] for s in self.streams()], ['Ironvale Bears'])
        self.assertEqual(self.client.put(url, json={'team_id': BEARS, 'enabled': False}).status_code, 200)
        self.assertEqual(self.streams(), [])
        self.assertEqual(self.client.put(url, json={'team_id': BEARS}).status_code, 400)
        self.assertEqual(self.client.put(url, json={'team_id': 'x', 'enabled': True}).status_code, 400)
        self.assertEqual(self.client.put('/api/sports/leagues/999/feed-channels',
                                         json={'team_id': BEARS, 'enabled': True}).status_code, 404)

    def test_deleting_the_league_takes_its_channels_and_their_ids_are_never_issued_again(self):
        old = self.team_on(BEARS).id
        self.assertTrue(sports_data.delete_league(self.league.id))
        self.assertEqual(SportsFeedChannel.query.count(), 0)
        league = SportsLeague(tsdb_id=9002, name='Other League', match_terms='[]')
        db.session.add(league)
        db.session.flush()
        db.session.add(SportsFollow(league_id=league.id, team_tsdb_id=0, rule_id=0))
        db.session.add(SportsTeam(tsdb_id=GULLS, full_name=TEAMS[GULLS]))
        db.session.commit()
        sports_feed.set_team_channel(league.id, GULLS, True)
        self.assertGreater(SportsFeedChannel.query.one().id, old)


class GuideTests(_Case):
    def xml(self):
        return self.get('/xmltv.php').get_data(as_text=True)

    def test_a_game_is_in_the_guide_at_the_guides_own_time_with_where_it_plays_from(self):
        row = self.team_on(BEARS)
        start = self.now + timedelta(hours=2)
        e = self.event(BEARS, GULLS, start, venue='Ironvale Dome', city='Ironvale')
        e.guide_start = start + timedelta(minutes=10)
        self.listing(e, self.channel('Local 1'), start=e.guide_start, hours=3)
        self.listing(e, self.channel('Local 2'), start=e.guide_start, hours=3.5)
        # A later airing of the same game, far from its start: not the game's end.
        self.listing(e, self.channel('Local 3'), start=e.guide_start + timedelta(hours=8))
        db.session.commit()
        xml = self.xml()
        fid = feed.SPORTS_FEED_BASE + row.id
        self.assertIn(f'<channel id="{fid}"><display-name>Ironvale Bears</display-name>', xml)
        stop = e.guide_start + timedelta(hours=3.5)
        self.assertIn(f'<programme start="{e.guide_start:%Y%m%d%H%M%S} +0000" stop="{stop:%Y%m%d%H%M%S} +0000" '
                      f'channel="{fid}"><title>Harborview Gulls at Ironvale Bears</title>', xml)
        self.assertIn('Ironvale Dome, Ironvale. ChannelBin plays this from one of the 2 channels', xml)
        self.assertIn('<category>Pretend Ball</category>', xml)

    def test_a_game_no_guide_lists_says_no_stream_is_found_yet(self):
        self.team_on(BEARS)
        start = self.now + timedelta(hours=2)
        e = self.event(BEARS, GULLS, start)
        hidden = self.channel('Hidden local', hidden=True)
        self.listing(e, hidden)
        self.listing(e, self.channel('Replay channel'), airing='replay')
        self.listing(e, self.channel('Weak'), strength='weak')
        self.listing(e, self.channel('Wrong'), rejected_at=self.now)
        self.listing(e, self.channel('Gone'), gone_since=self.now)
        db.session.commit()
        xml = self.xml()
        stop = start + sports_feed.MATCHUP_LENGTH
        self.assertIn(f'start="{start:%Y%m%d%H%M%S} +0000" stop="{stop:%Y%m%d%H%M%S} +0000"', xml)
        self.assertIn('No stream found yet: none of your channels&apos; guides lists this.'.replace('&apos;', "'"), xml)

    def test_a_game_listed_only_where_the_feed_cannot_play_says_so(self):
        self.team_on(BEARS)
        e = self.event(BEARS, GULLS, self.now + timedelta(hours=2))
        self.listing(e, self.channel('Local 1'))
        db.session.commit()
        self.t.sandbox_config(_feed_cfg(accounts=[self.account.id + 50]))
        xml = self.xml()
        self.assertIn('No stream it can play: 1 channel lists this, on an account that is blocked', xml)
        self.assertNotIn('ChannelBin plays this from', xml)

    def test_a_game_with_no_time_is_left_out(self):
        self.team_on(BEARS)
        self.event(BEARS, GULLS, self.now + timedelta(hours=2), postponed=True)
        midnight = (self.now + timedelta(days=2)).replace(hour=0, minute=0)
        race = SportsEvent(tsdb_id=7999, league_id=self.league.id, name='Grand Prix', start_at=midnight,
                           local_date=midnight.date(), feed_number=1)
        db.session.add(race)
        self.league.feed_league_channels = True
        db.session.add(SportsFeedChannel(league_id=self.league.id, number=1))
        db.session.commit()
        self.assertNotIn('<programme', self.xml())
        race.guide_start = midnight + timedelta(hours=14)
        db.session.commit()
        self.assertIn('<title>Grand Prix</title>', self.xml())

    def test_a_game_ends_where_the_next_on_its_channel_starts(self):
        row = self.team_on(BEARS)
        first = self.event(BEARS, GULLS, self.now + timedelta(hours=2))
        second = self.event(COMETS, BEARS, self.now + timedelta(hours=4))
        db.session.commit()
        xml = self.xml()
        fid = feed.SPORTS_FEED_BASE + row.id
        self.assertIn(f'start="{first.start_at:%Y%m%d%H%M%S} +0000" stop="{second.start_at:%Y%m%d%H%M%S} +0000" '
                      f'channel="{fid}"', xml)

    def test_both_teams_channels_carry_their_game(self):
        self.team_on(BEARS)
        self.team_on(GULLS)
        self.event(BEARS, GULLS, self.now + timedelta(hours=2))
        self.event(PANTHERS, COMETS, self.now + timedelta(hours=2))
        db.session.commit()
        xml = self.xml()
        self.assertEqual(xml.count('<title>Harborview Gulls at Ironvale Bears</title>'), 2)
        self.assertNotIn('Northgate Panthers', xml)


class PlayTests(_Case):
    def on_now(self, home=BEARS, away=GULLS):
        return self.event(home, away, self.now - timedelta(minutes=30))

    def test_it_plays_the_healthiest_channel_whose_guide_lists_the_game_now(self):
        row = self.team_on(BEARS)
        e = self.on_now()
        low, high = self.channel('Local low', score=40.0), self.channel('Local high', score=90.0)
        self.listing(e, low)
        self.listing(e, high)
        db.session.commit()
        resp = self.play(feed.SPORTS_FEED_BASE + row.id)
        self.assertEqual(resp.status_code, 302)
        self.assertEqual(resp.headers['Location'], high.stream_url)
        redirect = feed.recent_redirects()[0]
        self.assertEqual(redirect['channel_id'], high.id)
        self.assertIn('Ironvale Bears', redirect['label'])
        self.assertIn('Harborview Gulls at Ironvale Bears', redirect['label'])

    def test_only_a_listing_that_counts_is_played_from(self):
        row = self.team_on(BEARS)
        e = self.on_now()
        self.listing(e, self.channel('Hidden', score=99.0, hidden=True))
        self.listing(e, self.channel('Replay', score=99.0), airing='replay')
        self.listing(e, self.channel('Weak', score=99.0), strength='weak')
        self.listing(e, self.channel('Wrong', score=99.0), rejected_at=self.now)
        self.listing(e, self.channel('Gone', score=99.0), gone_since=self.now)
        self.listing(e, self.channel('Later', score=99.0), start=self.now + timedelta(hours=1))
        self.listing(e, self.channel('Over', score=99.0), start=self.now - timedelta(hours=5), hours=3)
        named = self.channel('LIVE | Gulls @ Bears', score=99.0)
        db.session.add(SportsMatch(event_id=e.id, channel_id=named.id, kind='channel_name', strength='strong',
                                   airing='first', program_start_time=e.start_at, title_key='x', reason='name'))
        good = self.channel('Unknown airing', score=10.0)
        self.listing(e, good, airing='unknown')
        db.session.commit()
        resp = self.play(feed.SPORTS_FEED_BASE + row.id)
        self.assertEqual(resp.status_code, 302, resp.get_data(as_text=True))
        self.assertEqual(resp.headers['Location'], good.stream_url)

    def test_a_long_event_is_still_played_hours_in(self):
        row = self.team_on(BEARS)
        e = self.event(BEARS, GULLS, self.now - timedelta(hours=8))
        ch = self.channel('All day')
        self.listing(e, ch, hours=10)
        db.session.commit()
        resp = self.play(feed.SPORTS_FEED_BASE + row.id)
        self.assertEqual(resp.status_code, 302, resp.get_data(as_text=True))
        self.assertEqual(resp.headers['Location'], ch.stream_url)

    def test_a_pregame_listing_does_not_take_the_channel_from_the_game_still_on(self):
        row = self.team_on(BEARS)
        on = self.event(BEARS, GULLS, self.now - timedelta(hours=3))
        still, early = self.channel('Still on', score=10.0), self.channel('Pregame', score=99.0)
        self.listing(on, still, hours=4)
        coming = self.event(COMETS, BEARS, self.now + timedelta(minutes=30))
        self.listing(coming, early, start=self.now - timedelta(minutes=10))
        db.session.commit()
        fid = feed.SPORTS_FEED_BASE + row.id
        self.assertEqual(self.play(fid).headers['Location'], still.stream_url)
        SportsMatch.query.filter_by(event_id=on.id).update({'gone_since': self.now})
        db.session.commit()
        self.assertEqual(self.play(fid).headers['Location'], early.stream_url,
                         'with nothing started still listed, the pregame is what is on')

    def test_between_games_says_when_the_next_one_is(self):
        row = self.team_on(BEARS)
        self.event(BEARS, GULLS, self.now + timedelta(hours=20))
        db.session.commit()
        why = self.nothing_on(feed.SPORTS_FEED_BASE + row.id)
        self.assertIn('no game is on right now. Next: Harborview Gulls at Ironvale Bears, ', why)

    def test_a_game_on_that_no_guide_lists_says_no_stream_is_found(self):
        row = self.team_on(BEARS)
        self.on_now()
        db.session.commit()
        self.assertIn('no stream is found yet for Harborview Gulls at Ironvale Bears',
                      self.nothing_on(feed.SPORTS_FEED_BASE + row.id))

    def test_no_schedule_at_all_says_so(self):
        row = self.team_on(BEARS)
        self.assertIn('none is in the schedule', self.nothing_on(feed.SPORTS_FEED_BASE + row.id))

    def test_a_game_listed_only_on_an_account_the_feed_may_not_use_is_a_fault(self):
        row = self.team_on(BEARS)
        e = self.on_now()
        self.listing(e, self.channel('Local 1'))
        db.session.commit()
        self.t.sandbox_config(_feed_cfg(accounts=[self.account.id + 50]))
        resp = self.play(feed.SPORTS_FEED_BASE + row.id)
        self.assertEqual(resp.status_code, 409)
        self.assertIn('not allowed to serve from', resp.get_json()['error'])
        alert = Alert.query.filter_by(alert_type='FEED_NO_MEMBER').one()
        self.assertIn('Ironvale Bears', alert.title)
        self.assertEqual(len(feed.recent_refused()), 1)

    def test_a_failover_is_held_to_the_game_it_started_on(self):
        row = self.team_on(BEARS)
        e = self.on_now()
        a, b = self.channel('Local a', score=90.0), self.channel('Local b', score=50.0)
        self.listing(e, a)
        self.listing(e, b)
        other = self.event(COMETS, BEARS, self.now - timedelta(minutes=5))
        c = self.channel('Local c', score=99.0)
        self.listing(other, c)
        db.session.commit()
        fid = feed.SPORTS_FEED_BASE + row.id
        # The later-starting game is the one on now; held to the first, it never crosses over.
        self.assertEqual(feed.resolve_play(fid).member.id, c.id)
        held = feed.resolve_play(fid, event_id=e.id)
        self.assertEqual((held.member.id, held.event_id), (a.id, e.id))
        self.assertEqual(feed.resolve_play(fid, event_id=e.id, exclude_ids=frozenset({a.id})).member.id, b.id)
        done = feed.resolve_play(fid, event_id=e.id, exclude_ids=frozenset({a.id, b.id}))
        self.assertIsNone(done.member)
        self.assertIn('already failed this play', done.why_none)
        SportsMatch.query.filter_by(event_id=e.id).update({'gone_since': self.now})
        db.session.commit()
        over = feed.resolve_play(fid, event_id=e.id)
        self.assertIsNone(over.member)
        self.assertIn("no channel's guide lists Harborview Gulls at Ironvale Bears any more", over.why_none)

    def test_an_id_that_is_not_a_served_sports_channel_is_404(self):
        self.assertEqual(self.play(feed.SPORTS_FEED_BASE + 12345).status_code, 404)


class WindowTests(_Case):
    """The sports window (dev/changelog/1529): a sports channel plays the channel whose guide lists
    its game from integrations.output_feed.sports_before_minutes ahead of the listing to
    sports_after_minutes past its end, 60 and 120 by default."""

    def fid(self, team=BEARS):
        return feed.SPORTS_FEED_BASE + self.team_on(team).id

    def zero(self):
        self.t.sandbox_config(_feed_cfg(sports_before_minutes=0, sports_after_minutes=0))

    def test_the_defaults_are_an_hour_before_and_two_hours_after(self):
        self.assertEqual(feed.sports_window(feed.feed_config()), (timedelta(hours=1), timedelta(hours=2)))
        self.assertEqual(feed.sports_window({}), (timedelta(0), timedelta(0)))
        self.assertEqual(feed.sports_window({'sports_before_minutes': 99999, 'sports_after_minutes': -5}),
                         (timedelta(minutes=720), timedelta(0)))

    def test_tuning_in_before_the_listing_plays_the_channel_that_will_carry_it(self):
        fid = self.fid()
        e = self.event(BEARS, GULLS, self.now + timedelta(minutes=45))
        ch = self.channel('Local 7')
        self.listing(e, ch)
        db.session.commit()
        resp = self.play(fid)
        self.assertEqual(resp.status_code, 302, resp.get_data(as_text=True))
        self.assertEqual(resp.headers['Location'], ch.stream_url)
        self.assertRegex(feed.recent_redirects()[0]['label'],
                         r'Harborview Gulls at Ironvale Bears \(its listing starts (Today|Tomorrow) ')
        play = feed.resolve_play(fid)
        self.assertNotIn('its listing', play.label, 'a proxy session keeps its label for the whole game')
        self.assertRegex(play.note, r'^its listing starts (Today|Tomorrow) ')
        self.zero()
        self.assertIn('no game is on right now. Next: Harborview Gulls at Ironvale Bears, ', self.nothing_on(fid),
                      'with no window, a game not yet listed as on has nothing to play')

    def test_tuning_in_after_the_listing_ended_plays_the_channel_that_carried_it(self):
        fid = self.fid()
        e = self.event(BEARS, GULLS, self.now - timedelta(hours=4))
        ch = self.channel('Local 7')
        self.listing(e, ch, hours=3)
        db.session.commit()
        resp = self.play(fid)
        self.assertEqual(resp.status_code, 302, resp.get_data(as_text=True))
        self.assertEqual(resp.headers['Location'], ch.stream_url)
        self.assertIn('may be running long', feed.recent_redirects()[0]['label'])
        self.zero()
        self.nothing_on(fid)

    def test_after_is_counted_from_the_listings_end_not_the_games_start(self):
        fid = self.fid()
        e = self.event(BEARS, GULLS, self.now - timedelta(hours=5))
        ch = self.channel('Local 7')
        self.listing(e, ch, hours=4)   # ended an hour ago; the start was five hours ago
        db.session.commit()
        self.assertEqual(feed.resolve_play(fid).member.id, ch.id)

    def test_a_game_further_off_than_the_window_is_still_refused_and_says_when_it_plays(self):
        fid = self.fid()
        e = self.event(BEARS, GULLS, self.now + timedelta(hours=3))
        self.listing(e, self.channel('Local 7'))
        gone = self.event(COMETS, BEARS, self.now - timedelta(hours=6))
        self.listing(gone, self.channel('Local 8'), hours=3)   # ended three hours ago
        db.session.commit()
        error = self.nothing_on(fid)
        self.assertIn('no game is on right now. Next: Harborview Gulls at Ironvale Bears, ', error)
        self.assertRegex(error, r'\. This channel plays it from (Today|Tomorrow) ')

    def test_the_game_just_ended_holds_the_channel_until_the_next_listing_starts(self):
        fid = self.fid()
        first = self.event(BEARS, GULLS, self.now - timedelta(hours=3, minutes=30))
        a = self.channel('Carried the first', score=10.0)
        self.listing(first, a, hours=3)                      # ended 30 min ago
        second = self.event(COMETS, BEARS, self.now + timedelta(minutes=30))
        b = self.channel('Carries the second', score=99.0)
        self.listing(second, b)                              # starts in 30 min
        db.session.commit()
        held = feed.resolve_play(fid)
        self.assertEqual((held.member.id, held.event_id), (a.id, first.id))
        later = feed.resolve_play(fid, now=self.now + timedelta(minutes=31))
        self.assertEqual((later.member.id, later.event_id), (b.id, second.id))

    def test_a_listing_on_now_beats_the_next_games_early_start(self):
        fid = self.fid()
        on = self.event(BEARS, GULLS, self.now - timedelta(hours=2))
        a = self.channel('On now', score=10.0)
        self.listing(on, a, hours=3)
        coming = self.event(COMETS, BEARS, self.now + timedelta(minutes=20))
        self.listing(coming, self.channel('Next up', score=99.0))
        db.session.commit()
        self.assertEqual(feed.resolve_play(fid).member.id, a.id)

    def test_a_failover_in_the_tail_stays_on_its_game(self):
        fid = self.fid()
        e = self.event(BEARS, GULLS, self.now - timedelta(hours=4))
        a, b = self.channel('Local a', score=90.0), self.channel('Local b', score=50.0)
        self.listing(e, a, hours=3)
        self.listing(e, b, hours=3)
        db.session.commit()
        moved = feed.resolve_play(fid, event_id=e.id, exclude_ids=frozenset({a.id}))
        self.assertEqual((moved.member.id, moved.event_id), (b.id, e.id))
        over = feed.resolve_play(fid, event_id=e.id, now=self.now + timedelta(hours=2))
        self.assertIsNone(over.member)
        self.assertIn('any more', over.why_none)

    def test_a_game_near_that_no_guide_lists_says_so_with_its_time(self):
        fid = self.fid()
        self.event(BEARS, GULLS, self.now + timedelta(minutes=30))
        db.session.commit()
        self.assertRegex(self.nothing_on(fid),
                         r'no stream is found yet for Harborview Gulls at Ironvale Bears, (Today|Tomorrow) ')

    def test_the_iptv_output_page_does_not_promise_a_play_from_an_account_it_cannot_use(self):
        """dev/docs/BUGS.md 2026-11-15 01:36 PM: the page counted every channel listing the game, so a game
        listed only on an account switched off for the output read "ChannelBin plays this from the
        1 channel", where a tune-in is refused."""
        self.t.sandbox_config(_feed_cfg(mode='proxy'))
        self.team_on(BEARS)
        e = self.event(BEARS, GULLS, self.now - timedelta(minutes=5), guide_start=self.now - timedelta(minutes=5))
        self.listing(e, self.channel('Local 7'))
        db.session.commit()
        self.t.sandbox_config(_feed_cfg(mode='proxy', accounts=[self.account.id + 50]))
        html = self.client.get('/iptv-output').get_data(as_text=True)
        self.assertIn('No stream it can play: 1 channel lists this, on an account that is blocked', html)
        self.assertNotIn('ChannelBin plays this from', html)
        # And for an account that is blocked rather than switched off.
        self.t.sandbox_config(_feed_cfg(mode='proxy'))
        from unittest import mock
        with mock.patch('app.channel_groups.blocked_account_ids_now', return_value=frozenset({self.account.id})):
            html = self.client.get('/iptv-output').get_data(as_text=True)
        self.assertIn('No stream it can play: 1 channel lists this, on an account that is blocked', html)
        self.assertNotIn('ChannelBin plays this from', html)

    def test_the_iptv_output_page_says_what_a_tune_in_plays_inside_the_window(self):
        self.t.sandbox_config(_feed_cfg(mode='proxy'))
        self.team_on(BEARS)
        e = self.event(BEARS, GULLS, self.now + timedelta(minutes=45), guide_start=self.now + timedelta(minutes=45))
        self.listing(e, self.channel('Local 7'))
        db.session.commit()
        html = self.client.get('/iptv-output').get_data(as_text=True)
        self.assertRegex(html, r'Harborview Gulls at Ironvale Bears, starts (today|tomorrow) at ')
        self.assertNotIn('Nothing on right now', html)
        self.t.sandbox_config(_feed_cfg(mode='proxy', sports_before_minutes=0, sports_after_minutes=0))
        html = self.client.get('/iptv-output').get_data(as_text=True)
        self.assertIn('Nothing on right now', html)

    def test_the_iptv_output_page_says_a_game_ended_may_still_be_on(self):
        self.t.sandbox_config(_feed_cfg(mode='proxy'))
        self.team_on(BEARS)
        start = self.now - timedelta(hours=4)
        e = self.event(BEARS, GULLS, start, guide_start=start)
        self.listing(e, self.channel('Local 7'), hours=3)
        db.session.commit()
        html = self.client.get('/iptv-output').get_data(as_text=True)
        self.assertRegex(html, r'Harborview Gulls at Ironvale Bears, listed until (today|yesterday) .+ and may be running long')

    def test_the_iptv_output_page_claims_no_play_for_a_near_game_no_guide_lists(self):
        self.t.sandbox_config(_feed_cfg(mode='proxy'))
        fid = self.fid()
        self.event(BEARS, GULLS, self.now + timedelta(minutes=45))
        self.event(COMETS, BEARS, self.now - timedelta(hours=4))
        db.session.commit()
        html = self.client.get('/iptv-output').get_data(as_text=True)
        self.assertIn('Nothing on right now', html)
        self.assertNotIn('may be running long', html)
        # The page and a tune-in agree: nothing to play, which is not a fault, so a tune-in gets
        # the channel's waiting screen (tests/test_sports_waiting.py).
        play = feed.resolve_play(fid)
        self.assertIsNone(play.member)
        self.assertFalse(play.fault)


class LiveNowTests(_Case):
    """Go live now (dev/changelog/1530): a person opens one game early, and every sports channel
    carrying it plays it ahead of its window until the end fixed when the button was pressed,
    which is `sports_after_minutes` (120 by default) past the end of its listings."""

    def fid(self, team=BEARS):
        return feed.SPORTS_FEED_BASE + self.team_on(team).id

    def press(self, event, on=True):
        return self.client.put(f'/api/sports/events/{event.id}/live-now', json={'on': on})

    def far_game(self, hours=5, listing_hours=3.0):
        e = self.event(BEARS, GULLS, self.now + timedelta(hours=hours))
        ch = self.channel('Local 7')
        self.listing(e, ch, hours=listing_hours)
        db.session.commit()
        return e, ch

    def test_it_plays_a_game_hours_ahead_of_its_window(self):
        fid = self.fid()
        e, ch = self.far_game()
        self.nothing_on(fid)   # five hours off is outside the window
        resp = self.press(e)
        self.assertEqual(resp.status_code, 200, resp.get_data(as_text=True))
        self.assertTrue(resp.get_json()['success'])
        resp = self.play(fid)
        self.assertEqual(resp.status_code, 302, resp.get_data(as_text=True))
        self.assertEqual(resp.headers['Location'], ch.stream_url)
        self.assertRegex(feed.resolve_play(fid).note, r'^Go live now is on; its listing starts (Today|Tomorrow) ')

    def test_it_reaches_every_sports_channel_carrying_the_game(self):
        bears, gulls = self.fid(BEARS), self.fid(GULLS)
        e, ch = self.far_game()
        self.press(e)
        for fid in (bears, gulls):
            self.assertEqual(feed.resolve_play(fid).member.id, ch.id)

    def test_it_ends_after_the_listing_as_the_window_would(self):
        fid = self.fid()
        e, ch = self.far_game(hours=5, listing_hours=3)
        self.press(e)
        db.session.expire_all()
        stored = db.session.get(SportsEvent, e.id)
        self.assertEqual(stored.live_now_until, e.start_at + timedelta(hours=3) + timedelta(hours=2))
        self.assertEqual(stored.live_now_at.date(), datetime.utcnow().date())
        late = feed.resolve_play(fid, now=stored.live_now_until - timedelta(minutes=1))
        self.assertEqual(late.member.id, ch.id)
        over = feed.resolve_play(fid, now=stored.live_now_until)
        self.assertIsNone(over.member)

    def test_a_game_moved_to_next_week_does_not_bring_it_back(self):
        fid = self.fid()
        e, _ch = self.far_game()
        self.press(e)
        week = timedelta(days=7)
        e.start_at += week
        m = SportsMatch.query.filter_by(event_id=e.id).one()
        m.program_start_time += week
        m.program_stop_time += week
        db.session.commit()
        self.nothing_on(fid)
        self.assertIsNone(feed.resolve_play(fid, now=m.program_start_time - timedelta(hours=3)).member,
                          'three hours before its new time is outside the window, and the switch has run out')

    def test_the_game_opened_early_wins_over_one_running_long(self):
        fid = self.fid()
        before = self.event(COMETS, BEARS, self.now - timedelta(hours=4))
        self.listing(before, self.channel('Carried the last one', score=99.0), hours=3)
        e, ch = self.far_game(hours=4)
        self.assertNotEqual(feed.resolve_play(fid).member.id, ch.id)
        self.press(e)
        self.assertEqual(feed.resolve_play(fid).member.id, ch.id)

    def test_a_failover_stays_on_the_game_opened_early(self):
        fid = self.fid()
        e, a = self.far_game()
        b = self.channel('Local 8', score=10.0)
        self.listing(e, b)
        db.session.commit()
        self.press(e)
        moved = feed.resolve_play(fid, event_id=e.id, exclude_ids=frozenset({a.id}))
        self.assertEqual((moved.member.id, moved.event_id), (b.id, e.id))

    def test_turning_it_off_puts_the_window_back(self):
        fid = self.fid()
        e, _ch = self.far_game()
        self.press(e)
        resp = self.press(e, on=False)
        self.assertEqual(resp.status_code, 200)
        self.assertIn('off', resp.get_json()['message'])
        self.nothing_on(fid)
        db.session.expire_all()
        stored = db.session.get(SportsEvent, e.id)
        self.assertEqual((stored.live_now_at, stored.live_now_until), (None, None))

    def test_it_is_refused_when_there_is_nothing_to_open(self):
        self.fid()
        unlisted = self.event(BEARS, GULLS, self.now + timedelta(hours=5))
        postponed = self.event(BEARS, COMETS, self.now + timedelta(hours=8), postponed=True)
        self.listing(postponed, self.channel('Local 9'))
        nobody = self.event(PANTHERS, COMETS, self.now + timedelta(hours=6))
        self.listing(nobody, self.channel('Local 10'))
        db.session.commit()
        for event, words in ((unlisted, "No channel's guide lists"), (postponed, 'is postponed'),
                             (nobody, 'No sports channel on your IPTV output carries')):
            resp = self.press(event)
            self.assertEqual(resp.status_code, 409, words)
            self.assertIn(words, resp.get_json()['error'])
        db.session.expire_all()
        self.assertEqual(SportsEvent.query.filter(SportsEvent.live_now_until.isnot(None)).count(), 0)
        self.assertEqual(self.client.put('/api/sports/events/999999/live-now', json={'on': True}).status_code, 404)
        self.assertEqual(self.client.put(f'/api/sports/events/{nobody.id}/live-now', json={}).status_code, 400)

    def test_the_iptv_output_page_offers_it_and_says_when_the_game_starts(self):
        self.t.sandbox_config(_feed_cfg(mode='proxy'))
        self.fid(BEARS)
        self.fid(GULLS)
        e = self.event(BEARS, GULLS, self.now + timedelta(hours=5))
        self.listing(e, self.channel('Local 7'), start=e.start_at - timedelta(minutes=30))
        db.session.commit()
        html = self.client.get('/iptv-output').get_data(as_text=True)
        self.assertIn(f'data-act="live-now" data-event="{e.id}" data-on="0"', html)
        self.assertRegex(html, r'Harborview Gulls at Ironvale Bears is scheduled to start (today|tomorrow) at '
                               r'[^,]+, and your guide lists it from (today|tomorrow) at ')
        self.assertIn('Harborview Gulls', html)
        self.assertIn('&#34;Ironvale Bears&#34;', html, 'the confirm lists every sports channel it plays on')
        self.press(e)
        html = self.client.get('/iptv-output').get_data(as_text=True)
        self.assertIn('Turn off Go live now', html)
        self.assertIn('playing ahead of its listing', html)
        self.assertRegex(html, r'Go live now is on for Harborview Gulls at Ironvale Bears until (today|tomorrow) at ')

    def test_a_game_opened_early_whose_listing_went_says_so(self):
        fid = self.fid()
        e, _ch = self.far_game()
        self.press(e)
        SportsMatch.query.filter_by(event_id=e.id).delete()
        db.session.commit()
        self.assertIn("Go live now is on for Harborview Gulls at Ironvale Bears, but no channel's guide "
                      'lists it any more', self.nothing_on(fid))

    def test_a_game_whose_listings_all_ended_is_not_called_unlisted(self):
        self.fid()
        e = self.event(BEARS, GULLS, self.now - timedelta(hours=6))
        self.listing(e, self.channel('Local 7'), hours=3)
        db.session.commit()
        resp = self.press(e)
        self.assertEqual(resp.status_code, 409)
        self.assertIn('Every listing of Harborview Gulls at Ironvale Bears has ended', resp.get_json()['error'])

    def test_a_game_listed_only_where_the_output_cannot_play_is_refused_at_the_press(self):
        other = make_account('Other', max_connections=1)
        db.session.commit()
        self.t.sandbox_config(_feed_cfg(accounts=[other.id]))
        self.fid()
        e, _ch = self.far_game()
        resp = self.press(e)
        self.assertEqual(resp.status_code, 409)
        self.assertIn('is on an account that is blocked right now or that the IPTV output does not play from',
                      resp.get_json()['error'])

    def test_the_toast_names_the_day_it_turns_off(self):
        self.fid()
        e, _ch = self.far_game()
        self.assertRegex(self.press(e).get_json()['message'],
                         r'^Go live now is on for Harborview Gulls at Ironvale Bears until (today|tomorrow) at ')

    def test_the_iptv_output_page_offers_nothing_while_a_game_is_on(self):
        self.t.sandbox_config(_feed_cfg(mode='proxy'))
        self.fid()
        on = self.event(BEARS, GULLS, self.now - timedelta(minutes=30))
        self.listing(on, self.channel('Local 7'))
        later = self.event(COMETS, BEARS, self.now + timedelta(hours=8))
        self.listing(later, self.channel('Local 8'))
        db.session.commit()
        html = self.client.get('/iptv-output').get_data(as_text=True)
        self.assertIn('Harborview Gulls at Ironvale Bears, on now', html)
        self.assertNotIn('data-act="live-now"', html)
        self.assertNotIn('>Go live now<', html)

    def test_the_iptv_output_page_follows_an_opened_game_into_its_listing(self):
        self.t.sandbox_config(_feed_cfg(mode='proxy'))
        fid = self.fid()
        e, ch = self.far_game(hours=5)
        self.press(e)
        e.start_at = e.guide_start = self.now - timedelta(minutes=20)
        m = SportsMatch.query.filter_by(event_id=e.id).one()
        m.program_start_time = e.start_at
        m.program_stop_time = e.start_at + timedelta(hours=3)
        db.session.commit()
        self.assertEqual(feed.resolve_play(fid).note, '', 'the play reads as on now')
        html = self.client.get('/iptv-output').get_data(as_text=True)
        self.assertIn('Harborview Gulls at Ironvale Bears, on now', html)
        self.assertNotIn('playing ahead of its listing', html)
        self.assertIn('Turn off Go live now', html, 'it is still on, so it can still be turned off')

    def test_the_iptv_output_page_says_why_when_the_next_game_has_no_listing(self):
        self.t.sandbox_config(_feed_cfg(mode='proxy'))
        self.fid()
        self.event(BEARS, GULLS, self.now + timedelta(hours=5))
        db.session.commit()
        html = self.client.get('/iptv-output').get_data(as_text=True)
        # A greyed button that keeps its tap to say why, as on the league page: a tip on a
        # disabled button is out of a phone's reach.
        self.assertRegex(html, r'<button class="btn btn-sm disabled" data-act="live-now"[^>]*aria-disabled="true" '
                               r'data-why="No channel&#39;s guide lists Harborview Gulls at Ironvale Bears yet')
        self.assertNotIn('disabled>Go live now', html)

    def test_the_league_page_offers_it_only_for_a_game_found_in_the_guide(self):
        self.fid()
        e, _ch = self.far_game()
        unfound = self.event(BEARS, COMETS, self.now + timedelta(days=2))
        nobody = self.event(PANTHERS, COMETS, self.now + timedelta(hours=7))
        self.listing(nobody, self.channel('Local 10'))
        db.session.commit()
        html = self.client.get(f'/sports/{self.league.id}').get_data(as_text=True)
        self.assertIn(f'data-act="live-now" data-event="{e.id}" data-on="0"', html)
        self.assertNotIn(f'data-event="{unfound.id}" data-on', html)
        self.assertRegex(html, f'data-event="{nobody.id}" data-on="0"[^>]*data-why="No sports channel')
        self.press(e)
        html = self.client.get(f'/sports/{self.league.id}').get_data(as_text=True)
        self.assertIn(f'data-event="{e.id}" data-on="1"', html)
        self.assertIn('Turn off Go live now', html)
        self.assertRegex(html, r'Go live now is on until (today|tomorrow) at [^,]+, on Ironvale Bears<')


class LeagueChannelTests(_Case):
    def on(self):
        self.assertTrue(sports_feed.set_league_channels(self.league.id, True))

    def numbers(self):
        db.session.expire_all()
        return {e.tsdb_id: e.feed_number for e in SportsEvent.query.order_by(SportsEvent.tsdb_id).all()}

    def test_games_at_once_get_a_channel_each_and_a_later_one_reuses_the_first(self):
        t = self.now + timedelta(hours=2)
        a = self.event(BEARS, GULLS, t)
        b = self.event(PANTHERS, COMETS, t + timedelta(minutes=30))
        c = self.event(BEARS, COMETS, t + timedelta(hours=6))
        db.session.commit()
        self.on()
        self.assertEqual(self.numbers(), {a.tsdb_id: 1, b.tsdb_id: 2, c.tsdb_id: 1})
        self.assertEqual([s['name'] for s in self.streams()], ['Pretend League 1', 'Pretend League 2'])
        xml = self.get('/xmltv.php').get_data(as_text=True)
        one = feed.SPORTS_FEED_BASE + SportsFeedChannel.query.filter_by(number=1).one().id
        two = feed.SPORTS_FEED_BASE + SportsFeedChannel.query.filter_by(number=2).one().id
        self.assertEqual(xml.count(f'channel="{one}"><title>'), 2)
        self.assertIn(f'channel="{two}"><title>Saint-Remy Comets at Northgate Panthers</title>', xml)

    def test_a_placed_game_never_moves_and_the_channels_only_grow(self):
        t = self.now + timedelta(hours=2)
        a = self.event(BEARS, GULLS, t)
        b = self.event(PANTHERS, COMETS, t)
        db.session.commit()
        self.on()
        ids = {r.number: r.id for r in SportsFeedChannel.query.all()}
        # An earlier game arrives later and overlaps both, so it takes a new channel.
        early = self.event(GULLS, COMETS, t - timedelta(hours=1))
        db.session.commit()
        self.assertEqual(sports_feed.place_all(self.now), 1)
        # A placed game is rescheduled onto the time another is then given.
        b.start_at = t + timedelta(hours=9)
        late = self.event(GULLS, PANTHERS, t + timedelta(hours=9))
        db.session.commit()
        self.assertEqual(sports_feed.place_all(self.now), 1)
        numbers = self.numbers()
        self.assertEqual((numbers[a.tsdb_id], numbers[b.tsdb_id]), (1, 2), 'placed games keep their channel')
        self.assertEqual(numbers[early.tsdb_id], 3, 'no free channel for its whole length, so a new one')
        self.assertEqual(numbers[late.tsdb_id], 1)
        self.assertEqual({r.number: r.id for r in SportsFeedChannel.query.all() if r.number in ids}, ids)
        self.assertEqual(sports_feed.place_all(self.now), 0, 're-runnable: nothing left to place')

    def test_a_game_moved_onto_anothers_time_is_placed_again_and_the_other_stays(self):
        """Left on its number, one of the two would have no channel: the guide can show only one
        game at a time and a play picks one. The game whose time changed is the one that goes,
        since the guide an IPTV player cached for it was already wrong about when it is."""
        t = self.now + timedelta(hours=2)
        a = self.event(BEARS, GULLS, t)
        b = self.event(PANTHERS, COMETS, t)
        c = self.event(GULLS, COMETS, t + timedelta(hours=6))
        db.session.commit()
        self.on()
        self.assertEqual(self.numbers(), {a.tsdb_id: 1, b.tsdb_id: 2, c.tsdb_id: 1})
        a.start_at, a.start_changed_at = c.start_at, self.now
        db.session.commit()
        with self.assertLogs('app.sports_feed', level='WARNING') as logs:
            self.assertEqual(sports_feed.place_all(self.now), 1)
        self.assertIn('placed again', logs.output[0])
        self.assertEqual(self.numbers(), {a.tsdb_id: 2, b.tsdb_id: 2, c.tsdb_id: 1})
        self.assertEqual(sports_feed.place_all(self.now), 0)

    def test_a_game_running_a_little_into_the_next_moves_nothing(self):
        t = self.now + timedelta(hours=2)
        a = self.event(BEARS, GULLS, t)
        c = self.event(PANTHERS, COMETS, t + sports_feed.MATCHUP_LENGTH)
        db.session.commit()
        self.on()
        self.assertEqual(self.numbers(), {a.tsdb_id: 1, c.tsdb_id: 1})
        c.start_at, c.start_changed_at = c.start_at - timedelta(minutes=20), self.now
        db.session.commit()
        self.assertEqual(sports_feed.place_all(self.now), 0)
        self.assertEqual(self.numbers(), {a.tsdb_id: 1, c.tsdb_id: 1})
        xml = self.get('/xmltv.php').get_data(as_text=True)
        self.assertIn(f'start="{a.start_at:%Y%m%d%H%M%S} +0000" stop="{c.start_at:%Y%m%d%H%M%S} +0000"', xml)

    def test_a_failed_placement_never_stops_its_caller(self):
        from unittest import mock
        with mock.patch('app.sports_feed.place_all', side_effect=RuntimeError('boom')), \
                self.assertLogs('app.sports_feed', level='ERROR') as logs:
            self.assertEqual(sports_feed.place_all_guarded(self.now), 0)
        self.assertIn('the next run tries again', logs.output[0])

    def test_off_keeps_the_layout_and_on_again_brings_back_the_same_ids(self):
        a = self.event(BEARS, GULLS, self.now + timedelta(hours=2))
        db.session.commit()
        self.on()
        before = [s['stream_id'] for s in self.streams()]
        sports_feed.set_league_channels(self.league.id, False)
        self.assertEqual(self.streams(), [])
        self.assertEqual(self.numbers()[a.tsdb_id], 1)
        self.on()
        self.assertEqual([s['stream_id'] for s in self.streams()], before)

    def test_it_needs_the_whole_league_followed(self):
        self.event(BEARS, GULLS, self.now + timedelta(hours=2))
        db.session.commit()
        self.on()
        sports_data.set_following(self.league.id, [BEARS])
        self.assertEqual(self.streams(), [], 'a team follow is not the league')
        resp = self.client.put(f'/api/sports/leagues/{self.league.id}/feed-channels', json={'enabled': True})
        self.assertEqual(resp.status_code, 409)
        self.assertIn('Follow the whole league first', resp.get_json()['error'])

    def test_a_schedule_update_places_the_games_it_stores(self):
        from app.database import SportsFetch, SportsState
        self.on()
        start = self.now + timedelta(days=1)
        row = SportsFetch(target=f'round:{self.league.tsdb_id}:2099:3', kind='round', league_id=self.league.id,
                          next_due_at=self.now)
        db.session.add_all([row, SportsState(id=1)])
        db.session.commit()
        raw = [{'idEvent': str(8000 + i), 'idLeague': '9001', 'strEvent': f'Game {i}', 'strSeason': '2099',
                'intRound': '3', 'idHomeTeam': str(BEARS + i), 'idAwayTeam': str(PANTHERS + i),
                'strHomeTeam': TEAMS[BEARS + i], 'strAwayTeam': TEAMS[PANTHERS + i],
                'strTimestamp': start.strftime('%Y-%m-%dT%H:%M:%S'), 'dateEvent': start.strftime('%Y-%m-%d')}
               for i in range(2)]
        from unittest import mock
        answer = sports_data.Answer(outcome='ok', rows=raw)
        with mock.patch('app.sports_data._call_for', return_value=answer), \
                mock.patch('app.scheduler.queue_sports_match'):
            self.assertEqual(sports_data.tick(now=self.now), 'ok')
        numbers = self.numbers()
        self.assertEqual((numbers[8000], numbers[8001]), (1, 2))
        self.assertEqual(SportsFeedChannel.query.filter_by(team_tsdb_id=0).count(), 2)

    def test_a_matcher_run_places_what_it_gave_a_time(self):
        """A field event at TheSportsDB's 00:00 placeholder has no time until the guide gives it
        one, so the matcher's run is the other place a game is placed."""
        from unittest import mock
        from app.database import SportsState
        db.session.add(SportsState(id=1))
        self.on()
        midnight = (self.now + timedelta(days=2)).replace(hour=0, minute=0)
        race = SportsEvent(tsdb_id=7999, league_id=self.league.id, name='Grand Prix', start_at=midnight,
                           local_date=midnight.date())
        db.session.add(race)
        db.session.commit()
        self.assertEqual(sports_feed.place_all(self.now), 0, 'no time yet, so nothing to place')

        def found(*_a, **_k):
            db.session.get(SportsEvent, race.id).guide_start = midnight + timedelta(hours=14)
            db.session.commit()
            return {}
        with mock.patch('app.sports_match.match_league', side_effect=found), \
                mock.patch('app.scheduler.queue_sports_match'):
            sports_match.run(self.now, pause=0)
        self.assertEqual(self.numbers()[7999], 1)

    def test_a_league_channel_plays_the_game_placed_on_it(self):
        e = self.event(BEARS, GULLS, self.now - timedelta(minutes=20))
        other = self.event(PANTHERS, COMETS, self.now - timedelta(minutes=20))
        ch1, ch2 = self.channel('Local 1'), self.channel('Local 2')
        self.listing(e, ch1)
        self.listing(other, ch2)
        db.session.commit()
        self.on()
        rows = {r.number: r.id for r in SportsFeedChannel.query.all()}
        self.assertEqual(self.play(feed.SPORTS_FEED_BASE + rows[1]).headers['Location'], ch1.stream_url)
        self.assertEqual(self.play(feed.SPORTS_FEED_BASE + rows[2]).headers['Location'], ch2.stream_url)


class PageTests(_Case):
    def test_the_league_page_offers_the_league_and_each_team_with_its_switch(self):
        self.event(BEARS, GULLS, self.now + timedelta(hours=2))
        db.session.commit()
        self.team_on(BEARS)
        html = self.client.get(f'/sports/{self.league.id}').get_data(as_text=True)
        self.assertIn('data-section="feed"', html)
        self.assertIn('data-act="feed-channel" data-team="0" aria-label="Pretend League league channels on the IPTV output">', html)
        self.assertIn(f'data-team="{BEARS}" aria-label="Ironvale Bears on the IPTV output" checked>', html)
        self.assertIn(f'data-team="{GULLS}" aria-label="Harborview Gulls on the IPTV output">', html)
        self.assertNotIn('The IPTV output is off', html)

    def test_it_says_when_the_feed_is_off_and_when_nothing_is_followed(self):
        self.t.sandbox_config({'integrations': {'output_feed': {'enabled': False}}})
        html = self.client.get(f'/sports/{self.league.id}').get_data(as_text=True)
        self.assertIn('The IPTV output is off', html)
        # The notice sends the reader where the output is turned on, which is its own page.
        self.assertIn('turned on, on the <a href="/iptv-output">IPTV Output</a> page', html)
        SportsFollow.query.delete()
        db.session.commit()
        html = self.client.get(f'/sports/{self.league.id}').get_data(as_text=True)
        self.assertIn('Follow this league, or a team in it, to give it a channel.', html)
        self.assertNotIn('data-act="feed-channel"', html)

    def test_a_team_follow_offers_only_that_team(self):
        sports_data.set_following(self.league.id, [GULLS])
        html = self.client.get(f'/sports/{self.league.id}').get_data(as_text=True)
        self.assertIn(f'data-team="{GULLS}"', html)
        self.assertNotIn('data-team="0"', html)
        self.assertNotIn(f'data-team="{BEARS}"', html)

    def test_the_dashboard_lists_a_refused_play_with_its_reason(self):
        # Nothing on is not refused any more (it gets the waiting screen); a game listed only
        # where the output may not play from still is.
        row = self.team_on(BEARS)
        e = self.event(BEARS, GULLS, self.now - timedelta(minutes=10))
        self.listing(e, self.channel('Local 1'))
        db.session.commit()
        self.t.sandbox_config(_feed_cfg(accounts=[self.account.id + 50]))
        self.assertEqual(self.play(feed.SPORTS_FEED_BASE + row.id).status_code, 409)
        html = self.client.get('/').get_data(as_text=True)
        self.assertIn('REFUSED', html)
        self.assertIn('not allowed to serve from', html)
        self.t.sandbox_config(_feed_cfg(mode='proxy', accounts=[self.account.id + 50]))
        html = self.client.get('/').get_data(as_text=True)
        self.assertIn('not allowed to serve from', html)
        self.assertEqual(feed.bundle_facts()['recent_refused'][0]['feed_id'], feed.SPORTS_FEED_BASE + row.id)


class MatchReaderTests(_Case):
    def test_listing_windows_and_airing_now_agree_on_what_counts(self):
        e = self.event(BEARS, GULLS, self.now - timedelta(minutes=10))
        good = self.channel('Good')
        self.listing(e, good)
        self.listing(e, self.channel('Replay'), airing='replay')
        self.listing(e, self.channel('Hidden', hidden=True))
        db.session.commit()
        self.assertEqual([(ev, ch) for ev, ch, _s, _e in sports_match.airing_now([e.id], self.now)],
                         [(e.id, good.id)])
        self.assertEqual([c for c, _a, _s, _e in sports_match.listing_windows([e.id])[e.id]], [good.id])
        self.assertEqual(sports_match.airing_now([], self.now), [])


if __name__ == '__main__':
    unittest.main()
