"""What a refused play tells a player outside the owner's own home (dev/docs/BUGS.md
2026-11-26 @ 10:45:57 PM, dev/changelog/1770).

A refusal's sentence is written for the owner: it names the IPTV Output page and its
settings, how the guide matched a game, a login's accounts and once the screens folder's
path. The play route sent it as the body to every player, so a login for another user and
anyone on the public port read the owner's setup. Only the main login at home is sent it
now; everyone else is told the kind of refusal, and Recent plays and the log keep the whole
sentence for the owner."""
import unittest

import app.output_feed as feed
from app import db, public_listener
from app.database import FEED_PLAY_REFUSED, OutputFeedLogin, OutputFeedPlay
from tests.support.output_feed import PASS, USER, FeedCase

FRIEND, FRIEND_PASS = 'kim', 'couch42'


class ShortRefusalTests(FeedCase):
    def setUp(self):
        super().setUp()
        db.session.add(OutputFeedLogin(name='Kim', username=FRIEND, password=FRIEND_PASS,
                                       max_streams=1))
        db.session.commit()
        # A row taken off the output: refused 409 with a sentence naming the IPTV Output page.
        self.switch(feed.KIND_CHANNEL, self.alpha.id, False)
        self.fid = feed.feed_id_for_channel(self.alpha.id)

    def play(self, user, password, feed_id=None, public=False):
        environ = {public_listener.PUBLIC_ENVIRON_KEY: True} if public else {}
        return self.client.get(f'/live/{user}/{password}/{feed_id or self.fid}.ts',
                               environ_base=environ)

    def refused_lines(self):
        feed.flush_play_log()
        return OutputFeedPlay.query.filter_by(kind=FEED_PLAY_REFUSED).all()

    def test_the_main_login_at_home_is_told_the_whole_reason(self):
        resp = self.play(USER, PASS)
        self.assertEqual(resp.status_code, 409)
        self.assertIn(feed.WHY_TAKEN_OFF, resp.get_json()['error'])

    def test_the_main_login_on_the_public_port_is_told_only_the_kind(self):
        with self.assertLogs('app.output_feed', 'INFO') as logs:
            resp = self.play(USER, PASS, public=True)
        self.assertEqual(resp.status_code, 409)
        self.assertEqual(resp.get_json()['error'], feed.SHORT_REFUSALS[409])
        self.assertNotIn(feed.WHY_TAKEN_OFF, resp.get_data(as_text=True))
        # The owner still has the whole reason, in Recent plays and in the log.
        self.assertIn(feed.WHY_TAKEN_OFF, self.refused_lines()[0].message)
        self.assertIn(feed.WHY_TAKEN_OFF, '\n'.join(logs.output))

    def test_a_login_for_another_user_is_told_only_the_kind(self):
        resp = self.play(FRIEND, FRIEND_PASS)
        self.assertIn(resp.status_code, feed.SHORT_REFUSALS)
        self.assertEqual(resp.get_json()['error'], feed.SHORT_REFUSALS[resp.status_code])
        self.assertNotIn('IPTV Output', resp.get_data(as_text=True))
        line = self.refused_lines()[0]
        self.assertEqual(line.feed_login, FRIEND)
        self.assertNotEqual(line.message, resp.get_json()['error'])

    def test_an_unknown_channel_on_the_public_port_is_a_plain_404(self):
        resp = self.play(USER, PASS, feed_id=999999, public=True)
        self.assertEqual(resp.status_code, 404)
        self.assertEqual(resp.get_json()['error'], feed.SHORT_REFUSALS[404])

    def test_a_wrong_login_is_still_the_one_401_body(self):
        resp = self.play(USER, 'nope', public=True)
        self.assertEqual(resp.status_code, 401)
        self.assertEqual(resp.get_json()['error'], feed.AUTH_FAILED_MESSAGE)


class ShortMessageTests(unittest.TestCase):
    def test_every_status_a_play_is_refused_with_has_its_own_short_reason(self):
        """start_session()'s docstring names 404, 503, 409 and 500; a play whose login changed
        while it started is 401."""
        for status in (401, 404, 409, 500, 503):
            with self.subTest(status=status):
                exc = feed.FeedRefused('The screens folder /home/x/screens cannot be written.',
                                       status)
                self.assertEqual(exc.short_message, feed.SHORT_REFUSALS[status])
                self.assertNotIn('/home', exc.short_message)

    def test_a_status_nobody_named_still_says_nothing_about_the_setup(self):
        exc = feed.FeedRefused('raise "IPTV players at once" in Settings', 418)
        self.assertEqual(exc.short_message, feed.SHORT_REFUSAL_OTHER)


if __name__ == '__main__':
    unittest.main()
