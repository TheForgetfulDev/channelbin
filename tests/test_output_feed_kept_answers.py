"""What a logged-in IPTV output user can make ChannelBin do over and over (dev/changelog/1772).

A HEAD on a stream address started a real play that nobody read: an ffmpeg and a provider
connection held until the idle check cut it, 31 s on this box (dev/docs/BUGS.md 2026-11-27 @
08:33:18 AM). It is answered now without starting anything.

xmltv.php took 1.3 s of the app's own CPU per ask on prod's 7-day guide, and eight asks at
once took over 30 s each; it and the channel list are kept for a minute per login lineup and
accounts, and asks that arrive together wait for one build."""
import json
import threading
import time
import unittest
from unittest import mock

import app.output_feed as feed
from app import db
from app.database import OutputFeedLogin
from tests.support.output_feed import PASS, USER, FeedCase

FRIEND, FRIEND_PASS = 'kim', 'couch42'


class HeadStartsNoPlayTests(FeedCase):
    """dev/docs/BUGS.md 2026-11-27 @ 08:33:18 AM."""

    def head(self, feed_id, user=USER, password=PASS):
        with mock.patch.object(feed, 'start_session') as start, \
                mock.patch.object(feed, 'redirect_play') as redirect:
            resp = self.client.head(f'/live/{user}/{password}/{feed_id}.ts')
        self.assertFalse(start.called, 'a HEAD started a relayed play')
        self.assertFalse(redirect.called, 'a HEAD was answered as a play')
        self.assertEqual(feed.live_sessions(), [])
        return resp

    def test_a_head_on_a_channel_answers_without_starting_a_play(self):
        resp = self.head(feed.feed_id_for_channel(self.alpha.id))
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.mimetype, 'video/mp2t')
        self.assertEqual(resp.get_data(), b'')

    def test_a_head_on_an_id_the_output_does_not_serve_is_a_404(self):
        resp = self.head(feed.feed_id_for_channel(self.loose.id))
        self.assertEqual(resp.status_code, 404)

    def test_a_head_on_a_row_taken_off_is_the_409_its_get_answers(self):
        self.switch(feed.KIND_CHANNEL, self.alpha.id, False)
        self.assertEqual(self.head(feed.feed_id_for_channel(self.alpha.id)).status_code, 409)

    def test_a_head_outside_a_logins_channels_is_a_404(self):
        db.session.add(OutputFeedLogin(name='Kim', username=FRIEND, password=FRIEND_PASS,
                                       max_streams=1,
                                       lineup=json.dumps([feed.feed_id_for_channel(self.beta.id)])))
        db.session.commit()
        self.assertEqual(self.head(feed.feed_id_for_channel(self.alpha.id), FRIEND, FRIEND_PASS)
                         .status_code, 404)
        self.assertEqual(self.head(feed.feed_id_for_channel(self.beta.id), FRIEND, FRIEND_PASS)
                         .status_code, 200)


class KeptAnswerRouteTests(FeedCase):
    def builds(self):
        return mock.patch.object(feed, 'catalog', wraps=feed.catalog)

    def test_a_second_xmltv_ask_inside_the_minute_is_not_built_again(self):
        with self.builds() as catalog:
            first = self.get('/xmltv.php')
            second = self.get('/xmltv.php')
        self.assertEqual(first.status_code, 200)
        self.assertEqual(first.get_data(), second.get_data())
        self.assertEqual(catalog.call_count, 1)

    def test_the_channel_list_and_playlist_are_kept_too(self):
        with self.builds() as catalog:
            for _ in range(2):
                self.get('/player_api.php', action='get_live_streams')
                self.get('/player_api.php', action='get_live_categories')
                self.get('/get.php')
        self.assertEqual(catalog.call_count, 3)

    def test_an_answer_older_than_the_window_is_built_again(self):
        with self.builds() as catalog, mock.patch.object(feed, 'ANSWER_KEEP_SECONDS', 0.0):
            self.get('/xmltv.php')
            self.get('/xmltv.php')
        self.assertEqual(catalog.call_count, 2)

    def test_a_switch_turned_reaches_the_next_ask_at_once(self):
        alpha = feed.feed_id_for_channel(self.alpha.id)
        self.assertIn(alpha, [s['stream_id'] for s in self.streams()])
        self.assertIn(f'<channel id="{alpha}">', self.get('/xmltv.php').get_data(as_text=True))
        self.switch(feed.KIND_CHANNEL, self.alpha.id, False)
        self.assertNotIn(alpha, [s['stream_id'] for s in self.streams()])
        self.assertNotIn(f'<channel id="{alpha}">', self.get('/xmltv.php').get_data(as_text=True))

    def test_any_change_the_owner_makes_in_the_web_ui_drops_the_kept_answers(self):
        alpha = feed.feed_id_for_channel(self.alpha.id)
        with self.builds() as catalog:
            self.get('/xmltv.php')
            refused = self.client.post(f'/api/iptv-output/rows/{alpha}', json={'on': 'maybe'})
            self.assertEqual(refused.status_code, 400)
            self.get('/xmltv.php')
            self.assertEqual(catalog.call_count, 1, 'a refused request dropped them')
            # A change that does not go through the output switch at all.
            self.assertEqual(self.client.post('/api/iptv-output/lockouts/clear',
                                              json={'address': '192.0.2.1'}).status_code, 200)
            self.get('/xmltv.php')
        self.assertEqual(catalog.call_count, 2)

    def test_a_login_with_its_own_channels_never_gets_another_logins_answer(self):
        db.session.add(OutputFeedLogin(name='Kim', username=FRIEND, password=FRIEND_PASS,
                                       max_streams=1,
                                       lineup=json.dumps([feed.feed_id_for_channel(self.beta.id)])))
        db.session.commit()
        alpha = f'channel="{feed.feed_id_for_channel(self.alpha.id)}"'
        alpha_row = f'<channel id="{feed.feed_id_for_channel(self.alpha.id)}">'
        self.assertIn(alpha_row, self.get('/xmltv.php').get_data(as_text=True))
        theirs = self.get('/xmltv.php', username=FRIEND, password=FRIEND_PASS).get_data(as_text=True)
        self.assertNotIn(alpha_row, theirs)
        self.assertNotIn(alpha, theirs)
        # Their channels chosen again: the next ask is built for the new list at once, not a
        # minute later.
        row = OutputFeedLogin.query.filter_by(username=FRIEND).one()
        row.lineup = json.dumps([feed.feed_id_for_channel(self.alpha.id)])
        db.session.commit()
        theirs = self.get('/xmltv.php', username=FRIEND, password=FRIEND_PASS).get_data(as_text=True)
        self.assertIn(alpha_row, theirs)

    def test_stream_addresses_carry_the_asking_logins_own_pair(self):
        """Two logins nobody narrowed share a guide, never a channel list: its addresses carry
        the login's username and password."""
        db.session.add(OutputFeedLogin(name='Kim', username=FRIEND, password=FRIEND_PASS,
                                       max_streams=1))
        self.alpha.logo_url = 'http://logos.example.test/alpha.png'
        db.session.commit()
        self.streams()
        self.get('/get.php')
        icons = [s['stream_icon'] for s in self.get('/player_api.php', action='get_live_streams',
                                                    username=FRIEND, password=FRIEND_PASS).get_json()]
        self.assertIn(f'/logo/{FRIEND}/{FRIEND_PASS}/{self.alpha.id}', ' '.join(icons))
        playlist = self.get('/get.php', username=FRIEND, password=FRIEND_PASS).get_data(as_text=True)
        self.assertIn(f'/live/{FRIEND}/{FRIEND_PASS}/', playlist)
        self.assertIn(f'/logo/{FRIEND}/{FRIEND_PASS}/', playlist)
        for text in (' '.join(icons), playlist):
            self.assertNotIn(f'/{USER}/{PASS}/', text)

    def test_the_address_asked_on_is_part_of_the_answer(self):
        self.get('/get.php')
        resp = self.client.get(f'/get.php?username={USER}&password={PASS}',
                               base_url='http://tv.example.test:9000')
        urls = [line for line in resp.get_data(as_text=True).splitlines() if '/live/' in line]
        self.assertTrue(urls)
        self.assertTrue(all(u.startswith('http://tv.example.test:9000/') for u in urls), urls)


class KeptAnswerTests(unittest.TestCase):
    def setUp(self):
        feed.forget_answers()
        self.addCleanup(feed.forget_answers)

    def test_asks_that_arrive_together_wait_for_one_build(self):
        calls = []

        def build():
            calls.append(1)
            time.sleep(0.2)
            return 'the guide'

        got = []
        threads = [threading.Thread(target=lambda: got.append(feed.kept_answer(('k',), build)))
                   for _ in range(6)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(5)
        self.assertEqual(got, ['the guide'] * 6)
        self.assertEqual(len(calls), 1)

    def test_an_answer_that_raised_is_not_kept(self):
        def broken():
            raise RuntimeError('database went away')

        with self.assertRaises(RuntimeError):
            feed.kept_answer(('k',), broken)
        self.assertEqual(feed.kept_answer(('k',), lambda: 'built'), 'built')

    def test_a_build_under_way_when_the_owner_changed_something_is_not_kept(self):
        def build():
            feed.forget_answers()  # the owner's change lands while this build reads
            return 'from before the change'

        self.assertEqual(feed.kept_answer(('k',), build), 'from before the change')
        self.assertEqual(feed.kept_answer(('k',), lambda: 'fresh'), 'fresh')

    def test_no_more_than_the_cap_are_kept(self):
        with mock.patch.object(feed, 'MAX_KEPT_ANSWERS', 3):
            for n in range(5):
                feed.kept_answer(('host', n), lambda: 'x')
        self.assertEqual(set(feed._answers), {('host', 2), ('host', 3), ('host', 4)})

    def test_old_answers_are_dropped_when_a_new_one_is_kept(self):
        feed.kept_answer(('old',), lambda: 'x')
        with mock.patch.object(feed, 'ANSWER_KEEP_SECONDS', 0.0):
            feed.kept_answer(('new',), lambda: 'y')
        self.assertEqual(set(feed._answers), {('new',)})
        self.assertNotIn(('old',), feed._answer_builds)


if __name__ == '__main__':
    unittest.main()
