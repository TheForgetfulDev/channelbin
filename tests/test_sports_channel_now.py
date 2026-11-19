"""Channel it would use (dev/changelog/1639): for one game, the channel a recording would start on
if the game started now and the channel its sports channel on the IPTV output would play, each from
the code that does it (sports_view.channel_now() over auto_rules.game_sources(), Record's
seat_choice() and the play's own walk), asked through GET /api/sports/events/<id>/channel-now.

One answer opens that channel (`go`); two answers, or one with something to say, are both shown with
why; nothing to show says why. Asking takes no seat, no ticket and no row. The buttons that ask sit
beside Record on a league page and beside Go live now on the IPTV Output page, greyed out with the
reason until a guide lists the game or a channel's name gives it. No dev/docs/BUGS.md entry: a feature.

Made-up league and teams; a throwaway temp SQLite database.

  python3 -m unittest tests.test_sports_channel_now
"""
import json
import os
import re
import sys
import unittest
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tests.support.app import make_test_app  # noqa: E402
from tests.support.seed import (make_account, make_channel, make_group, make_guide_match,  # noqa: E402
                                make_recording)

import app.output_feed as feed  # noqa: E402
from app import admission, connection_limits, db, sports_feed  # noqa: E402
from app.database import (REC_STATUS_SCHEDULED, Recording, RecordingSports, SportsEvent,  # noqa: E402
                          SportsFollow, SportsLeague)

URL = '/api/sports/events/{}/channel-now'


class _GameCase(unittest.TestCase):
    """One game on now, a team channel for its home team on the IPTV output. No tests of its own."""

    def setUp(self):
        self.t = make_test_app(csrf=False)
        self.addCleanup(self.t.cleanup)
        self.addCleanup(admission.reset_for_tests)
        self.addCleanup(feed.stop_all)
        self.output = {'enabled': True, 'username': 'u', 'password': 'p', 'mode': 'redirect'}
        self.configure()
        self.now = datetime.utcnow().replace(second=0, microsecond=0)
        self.start = self.now - timedelta(minutes=30)
        self.league = SportsLeague(tsdb_id=9001, name='Pretend League', sport='Pretend Ball',
                                   current_season='2099', match_terms=json.dumps(['PFL']))
        db.session.add(self.league)
        db.session.flush()
        db.session.add(SportsFollow(league_id=self.league.id, team_tsdb_id=0, rule_id=0))
        self.event = SportsEvent(tsdb_id=7001, league_id=self.league.id, name='Harborview Gulls vs Ironvale Bears',
                                 home_team_id=101, away_team_id=102, home_name='Ironvale Bears',
                                 away_name='Harborview Gulls', start_at=self.start, guide_start=self.start,
                                 local_date=self.start.date())
        db.session.add(self.event)
        db.session.commit()
        self.event_id, self.league_id = self.event.id, self.league.id
        self.assertTrue(sports_feed.set_team_channel(self.league.id, 101, True))
        self._n = 0

    def configure(self, **output):
        self.t.sandbox_config({'integrations': {'output_feed': {**self.output, **output}},
                               'display': {'timezone': 'America/New_York', 'time_format': '12h'}})

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

    def ask(self, event_id=None):
        # The request reads through this same session (testing rules); nothing commits elsewhere.
        db.session.commit()
        db.session.expire_all()
        r = self.t.client.get(URL.format(event_id or self.event_id))
        return r.status_code, r.get_json()


class ChannelNowTests(_GameCase):
    """What GET /api/sports/events/<id>/channel-now answers."""

    def test_one_channel_both_agree_on_opens_it(self):
        ch = self.channel(80)
        self.listed(ch)
        status, a = self.ask()
        self.assertEqual(status, 200, a)
        self.assertTrue(a['same'])
        self.assertEqual(a['record']['channel']['id'], ch.id)
        self.assertEqual(a['play']['channel']['id'], ch.id)
        self.assertEqual(a['go'], f'/channels/{ch.id}')
        self.assertIn('would record it from this channel, and its sports channel on your IPTV output would play it '
                      'from here too.', a['toast'])
        self.assertEqual(a['record']['channel']['account']['name'], ch.account.name)
        self.assertEqual(a['record']['channel']['account']['url'], f'/accounts/{ch.account_id}')

    def test_a_group_opens_the_member_it_would_start_on_and_names_the_group(self):
        best, other = self.channel(90), self.channel(40)
        make_group('Locals', members=[best, other])
        self.listed(best, other)
        status, a = self.ask()
        self.assertEqual(status, 200, a)
        self.assertEqual(a['record']['channel']['id'], best.id)
        self.assertEqual(a['record']['group']['name'], 'Locals')
        self.assertEqual(a['play']['group']['name'], 'Locals')
        self.assertTrue(a['same'])
        self.assertEqual(a['go'], f'/channels/{best.id}')
        self.assertIn('from this channel, the one the group "Locals" would start on, and its sports', a['toast'])
        self.assertIn("connection is in use can start on another channel", a['footnote'])

    def test_an_account_the_output_may_not_use_shows_both_and_opens_neither(self):
        """The IPTV output's account allowlist is the play's alone: a recording still takes the
        listing's healthiest channel, the sports channel the next one it may serve from."""
        first, second = self.channel(90), self.channel(50)
        self.listed(first, second)
        self.configure(accounts=[second.account_id])
        status, a = self.ask()
        self.assertEqual(status, 200, a)
        self.assertEqual(a['record']['channel']['id'], first.id)
        self.assertEqual(a['play']['channel']['id'], second.id)
        self.assertFalse(a['same'])
        self.assertIsNone(a['go'])

    def test_with_every_channel_off_the_allowlist_the_play_says_why(self):
        ch = self.channel(90)
        other = make_account('Elsewhere')
        self.listed(ch)
        self.configure(accounts=[other.id])
        _status, a = self.ask()
        self.assertIsNone(a['play']['channel'])
        self.assertIn('is on an account the IPTV output is not allowed to serve from', a['play']['why'])
        self.assertIsNone(a['go'])

    def test_no_sports_channel_carrying_it_opens_the_recordings_channel_and_says_so(self):
        ch = self.channel(80)
        self.listed(ch)
        self.assertTrue(sports_feed.set_team_channel(self.league_id, 101, False))
        status, a = self.ask()
        self.assertEqual(status, 200, a)
        self.assertFalse(a['play']['applies'])
        self.assertIsNone(a['play']['channel'])
        self.assertEqual(a['go'], f'/channels/{ch.id}')
        self.assertIn('ChannelBin would record it from this channel', a['toast'])
        self.assertIn('No sports channel on your IPTV output carries this game', a['toast'])

    def test_the_output_off_is_said(self):
        ch = self.channel(80)
        self.listed(ch)
        self.configure(enabled=False)
        _status, a = self.ask()
        self.assertFalse(a['play']['applies'])
        self.assertIn('The IPTV output is off', a['toast'])

    def test_a_game_no_guide_lists_names_why_and_opens_nothing(self):
        status, a = self.ask()
        self.assertEqual(status, 200, a)
        self.assertIsNone(a['record']['channel'])
        self.assertIsNone(a['play']['channel'])
        self.assertIsNone(a['go'])
        self.assertIn('not in your guide yet', a['record']['why'])

    def test_a_recording_already_made_elsewhere_is_said_and_not_opened_past(self):
        """A recording keeps the listing it was made from, so a different answer now is said rather
        than the page jumping to a channel the recording is not on."""
        ch, before = self.channel(80), self.channel(30)
        self.listed(ch)
        rec = make_recording(status=REC_STATUS_SCHEDULED, name='Gulls at Bears', channel_id=before.id,
                             start_time=self.start, stop_time=self.start + timedelta(hours=3))
        db.session.add(RecordingSports(recording_id=rec.id, tsdb_event_id=7001,
                                       league_name='Pretend League', event_name='Harborview Gulls vs Ironvale Bears',
                                       strength='strong', airing='live', reason='test'))
        db.session.commit()
        _status, a = self.ask()
        self.assertEqual(a['record']['channel']['id'], ch.id)
        self.assertIn('already has its recording scheduled, on Local 2, which keeps the listing',
                      a['record']['note'])
        self.assertNotIn('Record would pick', a['record']['note'])
        self.assertIsNone(a['go'])

    def test_a_recording_already_on_the_same_channel_needs_no_word(self):
        ch = self.channel(80)
        self.listed(ch)
        rec = make_recording(status=REC_STATUS_SCHEDULED, name='Gulls at Bears', channel_id=ch.id,
                             start_time=self.start, stop_time=self.start + timedelta(hours=3))
        db.session.add(RecordingSports(recording_id=rec.id, tsdb_event_id=7001,
                                       league_name='Pretend League', event_name='Harborview Gulls vs Ironvale Bears',
                                       strength='strong', airing='live', reason='test'))
        db.session.commit()
        _status, a = self.ask()
        self.assertEqual(a['record']['note'], '')
        self.assertEqual(a['go'], f'/channels/{ch.id}')

    def match(self, ch, **fields):
        make_guide_match(self.event, ch, **fields)
        db.session.commit()

    def test_only_a_replay_left_records_it_and_the_play_says_it_plays_no_replay(self):
        ch = self.channel(80)
        self.match(ch, start=self.now + timedelta(hours=5), airing='replay')
        _status, a = self.ask()
        self.assertEqual(a['record']['channel']['id'], ch.id)
        self.assertIn('so it would record a replay', a['record']['note'])
        self.assertIsNone(a['play']['channel'])
        self.assertIn('never a replay', a['play']['why'])
        self.assertIsNone(a['go'])

    def test_a_replay_already_scheduled_elsewhere_is_said(self):
        ch, other = self.channel(80), self.channel(30)
        later = self.now + timedelta(hours=5)
        self.match(ch, start=later, airing='replay')
        rec = make_recording(status=REC_STATUS_SCHEDULED, name='Gulls at Bears', channel_id=other.id,
                             start_time=later, stop_time=later + timedelta(hours=3))
        db.session.add(RecordingSports(recording_id=rec.id, tsdb_event_id=7001, league_name='Pretend League',
                                       event_name='Harborview Gulls vs Ironvale Bears', strength='strong',
                                       airing='replay', reason='test'))
        db.session.commit()
        _status, a = self.ask()
        self.assertIn('already has its replay scheduled, on Local 2', a['record']['note'])

    def test_an_event_channel_is_where_both_the_recording_and_the_play_take_it(self):
        """A game no guide lists that an event channel's name gives plays from that channel, the one
        Record takes (dev/changelog/1643)."""
        from app import sports_match
        from app.database import SportsMatch
        from app.program_titles import title_key
        ch = make_channel(make_account('Events'), stream_id=900, name='PFL 05: Gulls @ Bears 7:00 PM ET')
        start = self.now + timedelta(hours=2)
        db.session.add(SportsMatch(
            event_id=self.event_id, channel_id=ch.id, kind='channel_name', program_start_time=start,
            title_key=title_key(ch.name), episode_key='', program_title=ch.name, strength='strong',
            airing='unknown', reason=f'{sports_match._NAME_PREFIX}: matched on the league and both teams in it.'))
        self.event.start_at = self.event.guide_start = start
        db.session.commit()
        _status, a = self.ask()
        self.assertEqual(a['record']['channel']['id'], ch.id, a['record'])
        self.assertIn('recorded from the channel whose name mentions it', a['record']['note'])
        self.assertEqual(a['play']['why'], '')
        self.assertEqual(a['play']['channel']['id'], ch.id, a['play'])
        self.assertIn('plays it from the channel whose name mentions it', a['play']['note'])
        self.assertTrue(a['same'])

    def test_a_blocked_account_says_so_on_both_sides(self):
        from app import account_blocks
        ch = self.channel(80)
        self.listed(ch)
        account_blocks.add_account_block(ch.account_id, stop=self.now + timedelta(hours=4),
                                         start=self.now - timedelta(hours=1))
        status, a = self.ask()
        self.assertEqual(status, 200, a)
        self.assertIsNone(a['record']['channel'])
        self.assertIn('blocked', a['record']['why'])
        self.assertIsNone(a['play']['channel'])
        self.assertIn('blocked', a['play']['why'])
        self.assertIsNone(a['go'])

    def test_a_game_running_past_its_listing_still_names_what_the_sports_channel_plays(self):
        """The play holds an ended listing for a while (the sports window), so the answer does too:
        the recording side has nothing left to record, the sports channel is still playing it."""
        ch = self.channel(80)
        ended = self.now - timedelta(hours=3, minutes=10)
        self.listed(ch, start=ended)
        _status, a = self.ask()
        self.assertIsNone(a['record']['channel'])
        self.assertIn('has ended', a['record']['why'])
        self.assertEqual(a['play']['channel']['id'], ch.id, a['play'])
        self.assertIsNone(a['go'])

    def test_asking_takes_no_seat_no_ticket_and_no_row(self):
        best, other = self.channel(90), self.channel(40)
        make_group('Locals', members=[best, other])
        self.listed(best, other)
        recordings = Recording.query.count()
        self.ask()
        self.assertEqual(admission.active_kinds(), set())
        self.assertEqual(connection_limits.holder_counts(), {})
        db.session.remove()
        self.assertEqual(Recording.query.count(), recordings)
        self.assertEqual(feed.live_sessions(), [])

    def test_an_event_no_longer_stored_is_a_404(self):
        status, a = self.ask(987654)
        self.assertEqual(status, 404)
        self.assertIn('no longer stored', a['error'])


class ChannelNowButtonTests(_GameCase):
    """The buttons that ask: beside Record on a league page and in its menu, beside Go live now on
    the IPTV Output page, greyed out with the reason until a guide lists the game."""

    def page(self, url):
        db.session.remove()
        r = self.t.client.get(url)
        self.assertEqual(r.status_code, 200)
        return r.get_data(as_text=True)

    def buttons(self, html):
        return re.findall(r'<button[^>]*data-act="channel-now"[^>]*>', html)

    def test_league_page_offers_it_for_a_listed_game_beside_record_and_in_the_menu(self):
        self.event.start_at = self.event.guide_start = self.now + timedelta(hours=2)
        self.listed(self.channel(80), start=self.now + timedelta(hours=2))
        found = self.buttons(self.page(f'/sports/{self.league_id}'))
        self.assertEqual(len(found), 2, found)
        for b in found:
            self.assertIn(f'data-event="{self.event_id}"', b)
            self.assertNotIn('data-why', b)

    def test_league_page_greys_it_out_with_the_reason_before_the_guide_lists_it(self):
        self.event.start_at = self.now + timedelta(hours=2)
        db.session.commit()
        found = self.buttons(self.page(f'/sports/{self.league_id}'))
        self.assertEqual(len(found), 2, found)
        for b in found:
            self.assertIn('aria-disabled="true"', b)
            self.assertIn('Not checked yet, so there is no channel to show.', b)

    def test_league_page_reason_follows_the_guide_cell_once_checked(self):
        from unittest import mock
        self.event.start_at = self.now + timedelta(hours=2)
        db.session.commit()
        with mock.patch('app.sports_view._guide_cell', return_value={
                'state': 'later', 'text': 'Not in your guide yet', 'open': False, 'sub': ''}):
            found = self.buttons(self.page(f'/sports/{self.league_id}'))
        self.assertTrue(found)
        for b in found:
            self.assertIn('Not in your guide yet, so there is no channel to show.', b)

    def test_iptv_output_page_offers_it_on_the_sports_row(self):
        self.listed(self.channel(80))
        found = self.buttons(self.page('/iptv-output'))
        self.assertEqual(len(found), 1, found)
        self.assertIn(f'data-event="{self.event_id}"', found[0])
        self.assertNotIn('data-why', found[0])

    def test_iptv_output_page_greys_it_out_before_the_guide_lists_it(self):
        self.event.start_at = self.now + timedelta(hours=2)
        db.session.commit()
        found = self.buttons(self.page('/iptv-output'))
        self.assertEqual(len(found), 1, found)
        self.assertIn('aria-disabled="true"', found[0])
        self.assertIn("No channel&#39;s guide lists", found[0])


if __name__ == '__main__':
    unittest.main()
