"""Logins on the IPTV output (app/output_feed.py FeedLogin, the routes in
app/routes/iptv_output.py, dev/changelog/1760). The owner's own login is one of them since
dev/changelog/1781 (tests/test_output_feed_main_login.py).

What these hold is that the gate tells the logins apart, that every URL an answer carries is
the asking login's own, that a login's stream limit is held under the one lock, that two logins
behind one address never end each other's plays, that a login is never redirected to the provider, and that turning one
off, changing it or deleting it ends its plays and keeps its lines in Recent plays. A login's
channel list (dev/changelog/1764) narrows its catalog, its plays and its logos, numbers its
channels from 1, and ends the plays a new list leaves out. A login's own accounts
(dev/changelog/1765) take the place of the output's list in its catalog and in every walk its
plays ask, are named in its refusals, keep it off picture-in-picture channels, and end its plays
from an account a new list leaves out."""
import json
import time
import unittest
from unittest import mock

import app.output_feed as feed
from app import db
from app.database import (FEED_PLAY_ENDED, FEED_PLAY_REFUSED, OutputFeedLogin,
                          OutputFeedPlay)
from app.url_utils import mask_creds_in_text
from tests.support.output_feed import PASS, USER, FeedCase, feed_cfg
from tests.support.seed import make_channel, make_group

FRIEND, FRIEND_PASS = 'kim', 'couch42'


class _LoginCase(FeedCase):
    def setUp(self):
        super().setUp()
        self.login = OutputFeedLogin(name='Kim', username=FRIEND, password=FRIEND_PASS, max_streams=1)
        db.session.add(self.login)
        db.session.commit()

    def get_as(self, path, user=FRIEND, password=FRIEND_PASS, **params):
        return self.get(path, username=user, password=password, **params)

    def friend(self):
        return feed.feed_login_for(feed.feed_config(feed_cfg()), FRIEND, FRIEND_PASS)

    def play(self, feed_id, login=None, player='', client='192.168.1.50'):
        """A live relay session on `login` (None: the main login), registered as a real one is."""
        s = feed.RelaySession(
            id=f'p{feed_id}-{time.monotonic_ns()}', feed_id=feed_id, label='channel (Alpha)',
            client=client, group_id=None, channel_id=self.alpha.id, channel_name='Alpha',
            account_id=self.account.id, ring=feed.Ring(1024, 0), started_mono=time.monotonic(),
            app=self.t.app, player=player,
            feed_login_id=login.feed_login_id if login else None,
            feed_login=login.username if login else '')
        s.state = feed.STATE_STREAMING
        with feed._lock:
            feed._sessions[s.id] = s
        return s


class GateTests(_LoginCase):
    def test_a_login_for_another_user_is_let_in(self):
        resp = self.get_as('/player_api.php')
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.get_json()['user_info']['username'], FRIEND)

    def test_every_refusal_is_the_same_401(self):
        main_wrong = self.get('/player_api.php', password='nope')
        cases = {
            'wrong password': self.get_as('/player_api.php', password='nope'),
            'unknown user': self.get_as('/player_api.php', user='nobody'),
            'main user with the login password': self.get_as('/player_api.php', user=USER),
        }
        self.login.enabled = False
        db.session.commit()
        cases['login off'] = self.get_as('/player_api.php')
        for why, resp in cases.items():
            self.assertEqual(resp.status_code, 401, why)
            self.assertEqual(resp.get_json(), main_wrong.get_json(), why)

    def test_a_login_is_refused_while_the_output_is_off(self):
        self.t.sandbox_config(feed_cfg(enabled=False))
        self.assertEqual(self.get_as('/player_api.php').status_code, 401)

    def test_the_main_login_still_works(self):
        resp = self.get('/player_api.php')
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.get_json()['user_info']['username'], USER)


class ServedUrlTests(_LoginCase):
    """Every URL an answer carries is the asking login's own pair, never the main login's:
    otherwise every other user's playlist would hold the owner's password."""

    def setUp(self):
        super().setUp()
        self.alpha.logo_url = 'http://logos.example.test/alpha.png'
        db.session.commit()

    def test_the_playlist_carries_the_logins_own_pair(self):
        text = self.get_as('/get.php').get_data(as_text=True)
        self.assertIn(f'/live/{FRIEND}/{FRIEND_PASS}/', text)
        self.assertIn(f'/logo/{FRIEND}/{FRIEND_PASS}/', text)
        self.assertNotIn(PASS, text)

    def test_the_channel_list_carries_the_logins_own_pair(self):
        rows = self.get_as('/player_api.php', action='get_live_streams').get_json()
        icons = [r['stream_icon'] for r in rows if '/logo/' in r['stream_icon']]
        self.assertTrue(icons)
        for icon in icons:
            self.assertIn(f'/logo/{FRIEND}/{FRIEND_PASS}/', icon)

    def test_the_login_answer_is_the_logins_own(self):
        self.login.max_streams = 3
        db.session.commit()
        self.play(feed.feed_id_for_channel(self.alpha.id), login=self.friend())
        self.play(feed.feed_id_for_channel(self.beta.id))   # the main login's, not counted
        info = self.get_as('/player_api.php').get_json()['user_info']
        self.assertEqual((info['username'], info['password']), (FRIEND, FRIEND_PASS))
        self.assertEqual((info['max_connections'], info['active_cons']), ('3', '1'))

    def test_a_logo_answers_to_a_login(self):
        with mock.patch('app.logo_cache.download_logo', return_value=(b'png', 'image/png')):
            resp = self.client.get(f'/logo/{FRIEND}/{FRIEND_PASS}/{self.alpha.id}')
        self.assertEqual(resp.status_code, 200)

    def test_the_access_log_masks_the_logo_routes_pair(self):
        """dev/docs/BUGS.md 2026-11-26 11:37:08 AM: the logo route's pair reached the access log."""
        line = f'192.168.1.50 - - "GET /logo/{FRIEND}/{FRIEND_PASS}/{self.alpha.id} HTTP/1.1" 200 -'
        masked = mask_creds_in_text(line)
        self.assertNotIn(FRIEND_PASS, masked)
        self.assertIn(f'/logo/***/***/{self.alpha.id}', masked)


class StreamLimitTests(_LoginCase):
    """A login's own limit, held as the output's cap is: counted and reserved under one lock,
    with the channel change of the same player given its play's place."""

    def setUp(self):
        super().setUp()
        patcher = mock.patch.object(feed, 'SWITCH_GRACE_SECONDS', 0.2)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.feed_id = feed.feed_id_for_channel(self.alpha.id)

    def stub_play(self, feed_id=None):
        return mock.Mock(feed_id=feed_id or self.feed_id, label='channel (Alpha)')

    def test_a_login_at_its_limit_is_refused_by_name(self):
        self.play(self.feed_id, login=self.friend(), player='10.0.0.9')
        room = feed._LoginRoom(self.friend())
        with self.assertRaises(feed.FeedRefused) as cm:
            room.claim(self.stub_play(), '10.0.0.8', '10.0.0.8', alert=False)
        self.assertEqual(cm.exception.status, 503)
        self.assertIn(FRIEND, cm.exception.message)
        self.assertFalse(room.held)

    def test_two_tune_ins_at_once_cannot_both_pass(self):
        first = feed._LoginRoom(self.friend())
        first.claim(self.stub_play(), 'a', 'a', alert=False)
        second = feed._LoginRoom(self.friend())
        with self.assertRaises(feed.FeedRefused):
            second.claim(self.stub_play(), 'b', 'b', alert=False)
        first.release()
        self.assertEqual(feed._login_starting, {})

    def test_the_main_logins_plays_do_not_count(self):
        self.play(self.feed_id, player='10.0.0.9')
        room = feed._LoginRoom(self.friend())
        room.claim(self.stub_play(), 'a', 'a', alert=False)
        self.assertTrue(room.held)
        room.release()

    def test_the_same_player_changing_channel_takes_its_plays_place(self):
        old = self.play(self.feed_id, login=self.friend(), player='10.0.0.9')
        room = feed._LoginRoom(self.friend())
        room.claim(self.stub_play(feed.feed_id_for_channel(self.beta.id)), '10.0.0.9', '10.0.0.9',
                   alert=False)
        self.assertTrue(room.held)
        self.assertEqual(old.reason, feed.REASON_REPLACED)
        room.release()


class SameAddressTests(_LoginCase):
    def test_two_logins_behind_one_address_never_end_each_others_play(self):
        """Behind one router two users are one address: the play a player is leaving is only
        ever one on its own login (dev/changelog/1760)."""
        mine = self.play(feed.feed_id_for_channel(self.alpha.id), player='203.0.113.7')
        other = feed._leaving_play_of('203.0.113.7', self.login.id, feed.feed_id_for_channel(self.beta.id))
        self.assertIsNone(other)
        self.assertIs(feed._leaving_play_of('203.0.113.7', None, feed.feed_id_for_channel(self.beta.id)),
                      mine)


class RelayTests(_LoginCase):
    def test_a_login_is_never_redirected_to_the_provider(self):
        """A redirect sends the provider's own stream URL, with the owner's provider login in it."""
        for mode in (feed.MODE_REDIRECT, feed.MODE_MIXED):
            fcfg = {'mode': mode}
            fid = feed.feed_id_for_channel(self.alpha.id)
            self.assertTrue(feed.relays_for(fcfg, fid, self.friend()), mode)
            # The rig's own login is the moved main one, whose switch is on (dev/changelog/1781).
            main = feed.feed_login_for(feed.feed_config(feed_cfg()), USER, PASS)
            self.assertFalse(feed.relays_for(fcfg, fid, main), mode)

    def test_the_live_route_relays_a_login_in_redirect_mode(self):
        self.t.sandbox_config(feed_cfg(mode=feed.MODE_REDIRECT))
        fid = feed.feed_id_for_channel(self.alpha.id)
        with mock.patch.object(feed, 'start_session', side_effect=feed.FeedRefused('stub', 409)) as start, \
                mock.patch.object(feed, 'redirect_play') as redirect:
            resp = self.client.get(f'/live/{FRIEND}/{FRIEND_PASS}/{fid}.ts')
        self.assertEqual(resp.status_code, 409)
        self.assertEqual(start.call_args.kwargs['login'].username, FRIEND)
        redirect.assert_not_called()


class LoginRouteTests(_LoginCase):
    def test_create_generates_a_password_a_player_can_take(self):
        resp = self.client.post('/api/iptv-output/logins', json={'name': 'Sam', 'username': 'sam'})
        self.assertEqual(resp.status_code, 200)
        row = db.session.get(OutputFeedLogin, resp.get_json()['id'])
        self.assertEqual(len(row.password), feed.GENERATED_PASSWORD_LENGTH)
        self.assertEqual(feed.login_problem('password', row.password), '')
        self.assertEqual((row.max_streams, row.enabled), (1, True))

    def test_a_username_in_use_is_refused(self):
        for taken in (USER, FRIEND):
            resp = self.client.post('/api/iptv-output/logins', json={'name': 'X', 'username': taken})
            self.assertEqual(resp.status_code, 400, taken)

    def test_bad_fields_are_refused(self):
        for body in ({'name': '', 'username': 'a'}, {'name': 'A', 'username': 'a b'},
                     {'name': 'A', 'username': 'a', 'password': 'p/q'},
                     {'name': 'A', 'username': 'a', 'max_streams': 0},
                     {'name': 'A', 'username': 'a', 'max_streams': 'two'}):
            self.assertEqual(self.client.post('/api/iptv-output/logins', json=body).status_code, 400, body)

    def test_the_password_is_read_only_through_its_reveal(self):
        resp = self.client.get(f'/api/iptv-output/logins/{self.login.id}/password')
        self.assertEqual(resp.get_json()['password'], FRIEND_PASS)
        page = self.client.get('/iptv-output').get_data(as_text=True)
        self.assertIn('<h2>Logins <span class="cnt">2</span></h2>', page)
        self.assertIn(FRIEND, page)
        self.assertNotIn(FRIEND_PASS, page)

    def test_turning_a_login_off_ends_its_plays(self):
        s = self.play(feed.feed_id_for_channel(self.alpha.id), login=self.friend())
        resp = self.client.post(f'/api/iptv-output/logins/{self.login.id}', json={'enabled': False})
        self.assertEqual(resp.get_json()['ended'], 1)
        self.assertEqual(s.reason, feed.REASON_LOGIN_OFF)

    def test_a_new_password_ends_its_plays_and_a_new_name_does_not(self):
        s = self.play(feed.feed_id_for_channel(self.alpha.id), login=self.friend())
        self.client.post(f'/api/iptv-output/logins/{self.login.id}', json={'name': 'Kim B', 'max_streams': 2})
        self.assertTrue(s.live)
        self.client.post(f'/api/iptv-output/logins/{self.login.id}', json={'password': 'new1'})
        self.assertEqual(s.reason, feed.REASON_LOGIN_OFF)

    def test_delete_ends_its_plays_and_keeps_its_lines(self):
        s = self.play(feed.feed_id_for_channel(self.alpha.id), login=self.friend())
        resp = self.client.delete(f'/api/iptv-output/logins/{self.login.id}')
        self.assertEqual(resp.get_json()['ended'], 1)
        self.assertEqual(s.reason, feed.REASON_LOGIN_OFF)
        feed.flush_play_log()
        line = OutputFeedPlay.query.filter_by(kind=FEED_PLAY_ENDED).one()
        self.assertEqual(line.feed_login, FRIEND)
        self.assertIsNone(db.session.get(OutputFeedLogin, self.login.id))


class RecentPlaysTests(_LoginCase):
    def test_a_refusal_names_the_login_and_is_kept_apart_from_the_main_ones(self):
        token = feed._tuning_login.set(self.friend())
        try:
            feed._note_refused(5, 'channel (Alpha)', 'a', 'Nothing to play.')
        finally:
            feed._tuning_login.reset(token)
        feed._note_refused(5, 'channel (Alpha)', 'b', 'Nothing to play.')
        feed.flush_play_log()
        lines = OutputFeedPlay.query.filter_by(kind=FEED_PLAY_REFUSED).order_by(OutputFeedPlay.id).all()
        self.assertEqual([x.feed_login for x in lines], [FRIEND, None])

    def test_watching_now_and_recent_plays_name_the_login(self):
        self.play(feed.feed_id_for_channel(self.alpha.id), login=self.friend())
        page = self.client.get('/iptv-output').get_data(as_text=True)
        self.assertIn(f'Login {FRIEND}', page)


class RedirectModeVisibilityTests(_LoginCase):
    """A login's play is relayed in redirect mode too, so it is ChannelBin's own stream there
    and Watching now lists it beside ChannelBin's own pictures."""
    FEED = feed_cfg(mode=feed.MODE_REDIRECT)

    def test_watching_now_lists_a_logins_play_in_redirect_mode(self):
        mine = self.play(feed.feed_id_for_channel(self.alpha.id), login=self.friend())
        main = self.play(feed.feed_id_for_channel(self.beta.id))
        self.assertTrue(feed.seen_in_mode(mine, feed.MODE_REDIRECT))
        self.assertFalse(feed.seen_in_mode(main, feed.MODE_REDIRECT))
        page = self.client.get('/iptv-output').get_data(as_text=True)
        self.assertIn(f'Login {FRIEND}', page)
        # The relay's own settings apply to a login's play in every mode.
        self.assertIn('data-setting-path="integrations.output_feed.max_viewers"', page)

    def test_a_logins_waiting_screen_is_not_cut_when_its_game_comes_on(self):
        """In redirect mode a main-login waiting screen ends when the game comes on, since a
        redirected player has to tune in again; a login's play is relayed, so it moves to the
        game in place."""
        play = feed.Play(feed.feed_id_for_channel(self.alpha.id), 'NHL 1', sports=True)
        play.member = self.alpha
        for login, ended in ((self.friend(), False), (None, True)):
            s = self.play(play.feed_id, login=login)
            with mock.patch.object(feed, 'resolve_play', return_value=play), \
                    mock.patch.object(feed, '_to_game', return_value=None):
                feed._recheck_locked(s)
            self.assertEqual(s.reason == feed.REASON_WINDOW_OPENED, ended, login)


class StartWrapperTests(_LoginCase):
    """start_session()'s own work for a login: the play carries the login on every path, the
    play it replaced, and a login changed while it started is refused."""

    def stub_start(self, feed_id, **fields):
        def start(*_a, **_k):
            s = self.play(feed_id)
            for k, v in fields.items():
                setattr(s, k, v)
            return s
        return mock.patch.object(feed, '_start_session', side_effect=start)

    def test_a_waiting_screen_play_carries_its_login(self):
        fid = feed.feed_id_for_channel(self.alpha.id)
        with self.stub_start(fid, state=feed.STATE_WAITING):
            s = feed.start_session(fid, 'a', login=self.friend())
        self.assertEqual((s.feed_login_id, s.feed_login), (self.login.id, FRIEND))
        self.assertEqual(feed.login_plays(self.login.id), [s])

    def test_a_login_changed_while_its_play_started_is_refused(self):
        login = self.friend()            # what the gate read
        feed.stop_login(self.login.id)   # the owner turned it off before the play registered
        fid = feed.feed_id_for_channel(self.alpha.id)
        with self.stub_start(fid):
            with self.assertRaises(feed.FeedRefused) as cm:
                feed.start_session(fid, 'a', login=login)
        self.assertEqual(cm.exception.status, 401)
        self.assertEqual(feed.login_plays(self.login.id), [])

    def test_the_play_it_took_over_from_is_remembered(self):
        """Without it two players behind one address on one login cut each other on every
        retry (dev/changelog/1744's guard)."""
        patcher = mock.patch.object(feed, 'SWITCH_GRACE_SECONDS', 0.2)
        patcher.start()
        self.addCleanup(patcher.stop)
        alpha, beta = (feed.feed_id_for_channel(c.id) for c in (self.alpha, self.beta))
        old = self.play(alpha, login=self.friend(), player='203.0.113.7')
        room = feed._LoginRoom(self.friend())
        room.claim(mock.Mock(feed_id=beta, label='Beta'), 'x', '203.0.113.7', alert=False)
        self.assertEqual(old.reason, feed.REASON_REPLACED)
        self.assertEqual(room.replaced_feed_id, alpha)
        room.release()

    def test_a_waiting_screen_is_handed_over_on_a_channel_change(self):
        """A login's limit counts a waiting screen, which the output's cap does not, so the
        player leaving one is handed over too."""
        patcher = mock.patch.object(feed, 'SWITCH_GRACE_SECONDS', 0.2)
        patcher.start()
        self.addCleanup(patcher.stop)
        old = self.play(feed.feed_id_for_channel(self.alpha.id), login=self.friend(), player='p')
        old.state = feed.STATE_WAITING
        room = feed._LoginRoom(self.friend())
        room.claim(mock.Mock(feed_id=feed.feed_id_for_channel(self.beta.id), label='Beta'), 'x', 'p',
                   alert=False)
        self.assertTrue(room.held)
        self.assertEqual(old.reason, feed.REASON_REPLACED)
        room.release()

    def test_recent_plays_names_the_login(self):
        s = self.play(feed.feed_id_for_channel(self.alpha.id), login=self.friend())
        feed._finish(s, feed.REASON_CLIENT_GONE)
        feed.flush_play_log()
        page = self.client.get('/iptv-output').get_data(as_text=True)
        recent = page[page.index('id="of-recent"'):]
        self.assertIn(f'Login {FRIEND}', recent)


def _stand_in_screens(case):
    """Stand in for what a waiting screen, an Up next picture and a card are drawn and played
    with, for the rest of `case`'s test, so a tune-in reaches them through the real
    start_session()."""
    clip = mock.Mock(pages=[1], state='card', text='Nothing on')
    for name, value in (('_screen_of', clip), ('_rotation_of', clip), ('_card_of', clip),
                        ('_clip_for', ('clip.ts', False)), ('_launch_clip', None),
                        ('_set_screen', None), ('_set_card', None), ('_note_badges', None),
                        ('_ensure_reaper', None)):
        patcher = mock.patch.object(feed, name, return_value=value)
        patcher.start()
        case.addCleanup(patcher.stop)


class StartLogLineTests(_LoginCase):
    """Every Viewer log line a tune-in writes names the login for another user it came in on,
    whatever it starts on: a waiting screen, an Up next picture, a picture-in-picture card or
    its two pictures combined, or a refusal; the main login's lines name none
    (dev/docs/BUGS.md 2026-11-26 03:12:35 PM). Each of those lines was written before
    start_session() put the login on the play, so only a relayed play's said who it was.

    What each kind's screen or picture is made of is stood in for: what is tested is the line,
    through the real start_session()."""

    def setUp(self):
        super().setUp()
        self.fid = feed.feed_id_for_channel(self.alpha.id)
        _stand_in_screens(self)

    def lines(self, play, login, refused=False):
        """The output feed's log lines while `play` is tuned in on `login` (None: the main one)."""
        with mock.patch.object(feed, 'resolve_play', return_value=play), \
                self.assertLogs('app.output_feed', level='INFO') as logs:
            if refused:
                with self.assertRaises(feed.FeedRefused):
                    feed.start_session(self.fid, '203.0.113.7', login=login)
            else:
                feed.start_session(self.fid, '203.0.113.7', login=login)
        return [line for line in logs.output if 'Viewer' in line]

    def assert_named(self, make_play, refused=False):
        mine = self.lines(make_play(), self.friend(), refused)
        self.assertTrue(mine)
        for line in mine:
            self.assertIn(f'from 203.0.113.7 on login {FRIEND}', line)
        feed.stop_all()
        for line in self.lines(make_play(), None, refused):
            self.assertNotIn(' on login ', line)

    def waiting(self):
        play = feed.Play(self.fid, 'NHL 1', sports=True, fault=False, why_none='no game on')
        play.served = mock.Mock(row=mock.Mock(id=1))
        return play

    def pip(self, set_up):
        play = feed.Play(self.fid, 'PiP 1')
        play.pip = mock.Mock(row_id=1, set_up=set_up)
        play.pip.name = 'PiP 1'
        return play

    def test_a_waiting_screen_names_the_login(self):
        self.assert_named(self.waiting)

    def test_an_up_next_picture_names_the_login(self):
        def up_next():
            play = feed.Play(self.fid, 'Up next')
            play.up_next = mock.Mock(number=0)
            return play
        self.assert_named(up_next)

    def test_a_picture_in_picture_card_names_the_login(self):
        self.assert_named(lambda: self.pip(set_up=False))

    def test_two_pictures_combined_name_the_login(self):
        engine = mock.Mock(gpu_state=mock.Mock(return_value=('/dev/dri/renderD128', '')),
                           begin=mock.Mock(return_value=True))
        with mock.patch.object(feed, '_pip_engine', return_value=engine):
            self.assert_named(lambda: self.pip(set_up=True))

    def test_a_refusal_with_nothing_on_names_the_login(self):
        self.assert_named(lambda: feed.Play(self.fid, 'channel (Alpha)', fault=False, why_none='nothing on'),
                          refused=True)

    def test_a_waiting_screen_that_could_not_be_shown_names_the_login(self):
        import app.sports_screen as ss
        with mock.patch.object(feed, '_clip_for', side_effect=ss.ClipFailed('no font')):
            self.assert_named(self.waiting, refused=True)

    def test_an_up_next_picture_that_could_not_be_shown_names_the_login(self):
        import app.sports_screen as ss

        def up_next():
            play = feed.Play(self.fid, 'Up next')
            play.up_next = mock.Mock(number=0)
            play.up_next.name = 'Up next'
            return play
        with mock.patch.object(feed, '_clip_for', side_effect=ss.ClipFailed('no font')):
            self.assert_named(up_next, refused=True)

    def test_a_card_that_could_not_be_shown_names_the_login(self):
        import app.sports_screen as ss
        with mock.patch.object(feed, '_clip_for', side_effect=ss.ClipFailed('no font')):
            self.assert_named(lambda: self.pip(set_up=False), refused=True)


class _LineupCase(_LoginCase):
    """A login's channel list (FeedLogin.lineup, dev/changelog/1764): Alpha, Game feeds and Beta
    are the output's rows 1-3, and a lineup of Beta alone is that login's channel 1."""

    def setUp(self):
        super().setUp()
        self.fid_alpha = feed.feed_id_for_channel(self.alpha.id)
        self.fid_beta = feed.feed_id_for_channel(self.beta.id)
        self.fid_group = feed.feed_id_for_group(self.group.id)

    def set_lineup(self, ids):
        self.login.lineup = None if ids is None else json.dumps(ids)
        db.session.commit()

    def streams_as(self):
        return self.get_as('/player_api.php', action='get_live_streams').get_json()


class LineupCatalogTests(_LineupCase):
    def test_a_login_with_no_lineup_gets_everything_and_a_row_added_later(self):
        gamma = make_channel(self.account, name='Gamma', in_guide=True)
        gamma.guide_sort_order = 4
        db.session.commit()
        self.assertEqual([r['name'] for r in self.streams_as()], [r['name'] for r in self.streams()])
        self.assertIn('Gamma', [r['name'] for r in self.streams_as()])

    def test_a_lineup_numbers_its_channels_from_one_and_keeps_their_feed_ids(self):
        self.set_lineup([self.fid_beta])
        rows = self.streams_as()
        self.assertEqual([(r['name'], r['num'], r['stream_id']) for r in rows], [('Beta', 1, self.fid_beta)])
        main = {r['name']: r['num'] for r in self.streams()}
        self.assertEqual(main['Beta'], 3)

    def test_a_row_added_later_is_not_added_to_a_lineup(self):
        self.set_lineup([self.fid_beta])
        make_channel(self.account, name='Gamma', in_guide=True)
        db.session.commit()
        self.assertEqual([r['name'] for r in self.streams_as()], ['Beta'])

    def test_the_playlist_and_the_guide_carry_only_its_channels(self):
        self.set_lineup([self.fid_beta, self.fid_group])
        m3u = self.get_as('/get.php').get_data(as_text=True)
        self.assertIn(f'/{self.fid_beta}.ts', m3u)
        self.assertIn(f'/{self.fid_group}.ts', m3u)
        self.assertNotIn(f'/{self.fid_alpha}.ts', m3u)
        xml = self.get_as('/xmltv.php').get_data(as_text=True)
        self.assertIn(f'<channel id="{self.fid_beta}">', xml)
        self.assertNotIn(f'<channel id="{self.fid_alpha}">', xml)

    def test_a_picture_in_picture_pick_brings_its_swapped_copy(self):
        own, swapped = feed.feed_id_for_pip(3), feed.feed_id_for_pip(3, swapped=True)
        self.assertTrue(feed.lineup_offers(frozenset({own}), swapped))
        self.assertFalse(feed.lineup_offers(frozenset({feed.feed_id_for_pip(4)}), swapped))
        self.assertEqual(feed.lineup_problem([swapped, own]), ([own], ''))

    def test_a_damaged_lineup_gets_nothing_rather_than_everything(self):
        self.login.lineup = 'not json'
        db.session.commit()
        with self.assertLogs('app.output_feed', 'WARNING'):
            self.assertEqual(feed.lineup_of(self.login), frozenset())


class LineupPlayTests(_LineupCase):
    def test_a_channel_outside_the_lineup_is_refused_by_name(self):
        """Without the check the play route served any feed id a player asked for."""
        self.set_lineup([self.fid_beta])
        resp = self.client.get(f'/live/{FRIEND}/{FRIEND_PASS}/{self.fid_alpha}.ts')
        self.assertEqual(resp.status_code, 404)
        # The player is told the kind; the owner's Recent plays keeps the name
        # (dev/changelog/1770).
        self.assertEqual(resp.get_json()['error'], feed.SHORT_REFUSALS[404])
        feed.flush_play_log()
        line = OutputFeedPlay.query.filter_by(kind=FEED_PLAY_REFUSED).one()
        self.assertEqual((line.feed_id, line.feed_login), (self.fid_alpha, FRIEND))
        self.assertIn(f'IPTV output login "{FRIEND}"', line.message)

    def test_a_channel_in_the_lineup_gets_past_it(self):
        self.set_lineup([self.fid_beta])
        with mock.patch.object(feed, '_claim_slot', return_value=False), \
                mock.patch.object(feed, 'SWITCH_GRACE_SECONDS', 0):
            resp = self.client.get(f'/live/{FRIEND}/{FRIEND_PASS}/{self.fid_beta}.ts')
        self.assertEqual(resp.status_code, 503)    # the output's own cap, past the lineup

    def test_another_login_is_not_held_to_one_logins_lineup(self):
        self.set_lineup([self.fid_beta])
        main = feed.feed_login_for(feed.feed_config(feed_cfg()), USER, PASS)
        self.assertTrue(main.offers(self.fid_alpha))

    def test_the_logo_route_refuses_a_channel_outside_the_lineup(self):
        self.alpha.logo_url = 'http://logos.example.test/alpha.png'
        self.member.logo_url = 'http://logos.example.test/member.png'
        db.session.commit()
        self.set_lineup([self.fid_group])
        with mock.patch('app.logo_cache.download_logo', return_value=(b'png', 'image/png')):
            member = self.client.get(f'/logo/{FRIEND}/{FRIEND_PASS}/{self.member.id}')
            alpha = self.client.get(f'/logo/{FRIEND}/{FRIEND_PASS}/{self.alpha.id}')
            main = self.client.get(f'/logo/{USER}/{PASS}/{self.alpha.id}')
        self.assertEqual((member.status_code, alpha.status_code, main.status_code), (200, 404, 200))


class LineupSaveTests(_LineupCase):
    def post_lineup(self, value):
        return self.client.post(f'/api/iptv-output/logins/{self.login.id}', json={'lineup': value})

    def test_a_lineup_is_saved_and_everything_clears_it(self):
        swapped = feed.feed_id_for_pip(2, swapped=True)
        self.assertEqual(self.post_lineup([self.fid_beta, swapped]).status_code, 200)
        db.session.expire_all()
        self.assertEqual(json.loads(self.login.lineup), sorted([self.fid_beta, swapped - 1]))
        self.assertEqual(self.post_lineup(None).status_code, 200)
        db.session.expire_all()
        self.assertIsNone(self.login.lineup)

    def test_a_bad_lineup_is_refused(self):
        for value in ([], 'all', [True], [0], ['5'], [1.5]):
            self.assertEqual(self.post_lineup(value).status_code, 400, value)
        db.session.expire_all()
        self.assertIsNone(self.login.lineup)

    def test_a_lineup_change_ends_only_the_plays_it_leaves_out(self):
        self.login.max_streams = 3
        db.session.commit()
        out = self.play(self.fid_alpha, login=self.friend())
        kept = self.play(self.fid_beta, login=self.friend())
        mains = self.play(self.fid_alpha)
        resp = self.post_lineup([self.fid_beta])
        self.assertEqual(resp.get_json()['ended'], 1)
        self.assertEqual(out.reason, feed.REASON_LINEUP)
        self.assertTrue(kept.live)
        self.assertTrue(mains.live)
        self.assertTrue(feed.REASON_TEXT[feed.REASON_LINEUP])

    def test_a_play_let_in_on_the_old_lineup_is_ended_when_the_new_one_leaves_it_out(self):
        login = self.friend()                                     # what the gate read
        feed.set_login_lineup(self.login.id, frozenset({self.fid_beta}))   # chosen again meanwhile
        started = []

        def start(feed_id, *_a, **_k):
            started.append(self.play(feed_id))
            return started[-1]
        with mock.patch.object(feed, '_start_session', side_effect=start):
            with self.assertRaises(feed.FeedRefused) as cm:
                feed.start_session(self.fid_alpha, 'a', login=login)
            kept = feed.start_session(self.fid_beta, 'a', login=login)
        self.assertEqual(cm.exception.status, 404)
        self.assertEqual(started[0].reason, feed.REASON_LINEUP)
        self.assertEqual(feed.login_plays(self.login.id), [kept])     # still in the new list
        feed.flush_play_log()
        self.assertEqual([(x.kind, x.feed_id) for x in OutputFeedPlay.query.all()],
                         [(FEED_PLAY_ENDED, self.fid_alpha)])          # one line, not two


class LineupPageTests(_LineupCase):
    def test_the_dialog_lists_every_row_and_the_logins_picks(self):
        self.switch(feed.KIND_CHANNEL, self.alpha.id, False)
        self.set_lineup([self.fid_alpha, 999_998])     # the second names a row that is gone
        d = self.client.get(f'/api/iptv-output/logins/{self.login.id}/channels').get_json()
        self.assertFalse(d['everything'])
        self.assertEqual(d['picked'], [self.fid_alpha])
        rows = {r['name']: r for r in d['rows']}
        self.assertEqual(set(rows), {'Alpha', 'Game feeds', 'Beta'})
        self.assertEqual(rows['Alpha']['off'], 'Taken off the output')
        self.assertEqual(rows['Beta']['off'], '')

    def test_the_logins_card_says_what_its_player_lists(self):
        page = self.client.get('/iptv-output').get_data(as_text=True)
        self.assertIn('>Everything<', page)
        self.switch(feed.KIND_CHANNEL, self.alpha.id, False)
        self.set_lineup([self.fid_alpha, self.fid_beta])
        page = self.client.get('/iptv-output').get_data(as_text=True)
        logins = page[page.index('id="of-logins"'):page.index('id="of-watching"')]
        self.assertIn('1 channel<', logins)
        self.assertIn('1 more picked, not on the output now', logins)
        self.assertIn('data-act="login-channels"', logins)


class LineupMigrationTests(unittest.TestCase):
    def test_step_153_adds_the_column_and_runs_again(self):
        import sqlite3
        from app.migrations import _m153_output_feed_login_lineup
        conn = sqlite3.connect(':memory:')
        conn.execute('CREATE TABLE output_feed_logins (id INTEGER PRIMARY KEY, username TEXT)')
        conn.execute("INSERT INTO output_feed_logins (username) VALUES ('kim')")
        for _ in range(2):
            _m153_output_feed_login_lineup(conn, conn.cursor())
        cols = [r[1] for r in conn.execute('PRAGMA table_info(output_feed_logins)')]
        self.assertIn('lineup', cols)
        self.assertEqual(conn.execute('SELECT lineup FROM output_feed_logins').fetchone(), (None,))


class _AccountsCase(_LoginCase):
    """A login's own accounts (FeedLogin.accounts, dev/changelog/1765). The rig's Alpha and Beta
    are on `account`, the group's one channel on `other`."""

    def setUp(self):
        super().setUp()
        self.fid_alpha = feed.feed_id_for_channel(self.alpha.id)
        self.fid_beta = feed.feed_id_for_channel(self.beta.id)
        self.fid_group = feed.feed_id_for_group(self.group.id)

    def set_accounts(self, ids):
        self.login.accounts = None if ids is None else json.dumps(ids)
        db.session.commit()

    def streams_as(self):
        return self.get_as('/player_api.php', action='get_live_streams').get_json()

    def own(self, *accounts):
        return feed.LoginAccounts(a.id for a in accounts)


class AccountsCatalogTests(_AccountsCase):
    def test_a_login_on_its_own_accounts_lists_only_their_channels(self):
        self.set_accounts([self.other.id])
        rows = self.streams_as()
        # A group stays listed whatever its channels' accounts: its play refuses by name.
        self.assertEqual([(r['name'], r['num']) for r in rows], [('Game feeds', 1)])
        self.assertEqual([r['name'] for r in self.streams()], ['Alpha', 'Game feeds', 'Beta'])

    def test_its_own_accounts_take_the_place_of_the_outputs(self):
        gamma = make_channel(self.other, name='Gamma', in_guide=True)
        gamma.guide_sort_order = 4
        db.session.commit()
        self.t.sandbox_config(feed_cfg(accounts=[self.account.id]))
        self.set_accounts([self.other.id])
        self.assertNotIn('Gamma', [r['name'] for r in self.streams()])
        self.assertEqual([r['name'] for r in self.streams_as()], ['Game feeds', 'Gamma'])
        self.assertNotIn(f'<channel id="{self.fid_alpha}">', self.get_as('/xmltv.php').get_data(as_text=True))

    def test_a_login_on_the_outputs_list_is_unchanged(self):
        self.assertEqual(self.streams_as(), self.streams())

    def test_a_picture_in_picture_channel_is_left_out_for_a_login_on_its_own_accounts(self):
        row = mock.Mock(kind=feed.KIND_PIP, left_out='')
        self.assertEqual(feed.left_out_for_login(row, self.own(self.account)), feed.LEFT_OUT_PIP_OWN_ACCOUNTS)
        self.assertEqual(feed.left_out_for_login(row, None), '')

    def test_damaged_accounts_play_from_none_rather_than_the_outputs(self):
        self.login.accounts = '{"a": 1}'
        db.session.commit()
        with self.assertLogs('app.output_feed', 'WARNING'):
            self.assertEqual(feed.accounts_of(self.login), frozenset())
        self.assertIsInstance(feed.accounts_of(self.login), feed.LoginAccounts)


class AccountsPlayTests(_AccountsCase):
    def test_a_channel_outside_its_accounts_is_refused_naming_them(self):
        self.set_accounts([self.other.id])
        resp = self.client.get(f'/live/{FRIEND}/{FRIEND_PASS}/{self.fid_alpha}.ts')
        self.assertEqual(resp.status_code, 409)
        self.assertEqual(resp.get_json()['error'], feed.SHORT_REFUSALS[409])
        feed.flush_play_log()
        line = OutputFeedPlay.query.filter_by(kind=FEED_PLAY_REFUSED).one()
        self.assertIn("this IPTV output login's own accounts", line.message)

    def test_a_group_is_walked_over_its_accounts_alone(self):
        """Every re-ask of a play (a failover, a move, a seat walk) goes through _resolve_again,
        and each must be held to the play's login's accounts, not the output's."""
        near = make_channel(self.account, name='Near')
        far = make_channel(self.other, name='Far')
        both = make_group('Both', members=[near, far])
        db.session.commit()
        cfg = feed_cfg()
        session = self.play(feed.feed_id_for_group(both.id), login=self.friend())
        found = {}
        for who in (self.account, self.other):
            session.login_accounts = self.own(who)
            found[who.name] = feed._resolve_again(session, cfg, frozenset()).member.name
        self.assertEqual(found, {'Acct': 'Near', 'Other': 'Far'})
        session.login_accounts = feed.LoginAccounts({self.account.id + self.other.id + 99})
        play = feed._resolve_again(session, cfg, frozenset())
        self.assertIsNone(play.member)
        self.assertIn("this IPTV output login's own accounts", play.why_none)

    def test_a_sports_walk_names_the_logins_accounts(self):
        words = feed.sports_drop_words(feed._drop_allowed(self.own(self.account)), 'Game')
        self.assertIn("this IPTV output login's own accounts", words)
        self.assertNotIn("login's own", feed.sports_drop_words(feed._drop_allowed(frozenset({1})), 'Game'))

    def test_every_ask_of_a_play_carries_its_accounts(self):
        """A call of resolve_play() that left them out would walk the output's list, and a login
        tied to one account would fail over onto another. Every call in the module passes
        `accounts=`, whatever its spelling, but redirect_target()'s, which is handed the play
        redirect_play() resolved with them (and only the main login is redirected)."""
        import ast
        import inspect
        tree = ast.parse(inspect.getsource(feed))
        missing, seen = [], 0
        for fn in ast.walk(tree):
            if not isinstance(fn, ast.FunctionDef) or fn.name in ('resolve_play', 'redirect_target'):
                continue
            for call in ast.walk(fn):
                if isinstance(call, ast.Call) and getattr(call.func, 'id', '') == 'resolve_play':
                    seen += 1
                    if 'accounts' not in {k.arg for k in call.keywords}:
                        missing.append(f'{fn.name}: {ast.unparse(call)}')
        self.assertGreaterEqual(seen, 9)
        self.assertEqual(missing, [])

    def test_a_move_resolved_on_the_old_accounts_ends_rather_than_plays_outside_the_new(self):
        """A failover, a move or a game put on that was already walking when the list changed
        names its account in _launch(), in the hold set_login_accounts() hands over the new list
        in: one outside it ends there, by name, rather than playing from an account the login
        no longer has."""
        session = self.play(self.fid_group, login=self.friend())
        session.login_accounts = self.own(self.account)          # the list just set
        with mock.patch.object(feed, '_take_seat', return_value=True):
            with self.assertRaises(feed.FeedRefused):
                feed._launch(session, self.member, feed_cfg(), first=False)   # Member is on `other`
        self.assertEqual(session.reason, feed.REASON_ACCOUNTS)

    def test_a_refusal_over_its_accounts_raises_no_alert(self):
        """Its player asks again every half minute and nothing is broken, as a channel outside
        its list is (_not_in_lineup); an alert would also name no login."""
        from app.database import Alert
        self.set_accounts([self.other.id])
        for _ in range(2):
            resp = self.client.get(f'/live/{FRIEND}/{FRIEND_PASS}/{self.fid_alpha}.ts')
            self.assertEqual(resp.status_code, 409)
        self.assertEqual(Alert.query.count(), 0)
        feed.flush_play_log()
        line = OutputFeedPlay.query.filter_by(kind=FEED_PLAY_REFUSED).one()
        self.assertEqual((line.feed_id, line.feed_login, line.count), (self.fid_alpha, FRIEND, 2))
        self.assertIn(f'IPTV output login "{FRIEND}"', line.message)

    def test_a_picture_in_picture_channel_is_refused_by_name(self):
        from app import pip_channels
        from app.database import PipChannel
        row = PipChannel(name='PiP 1', on_output=True)
        db.session.add(row)
        db.session.commit()
        fid = feed.feed_id_for_pip(row.id)
        play = feed.resolve_play(fid, feed_cfg(), accounts=self.own(self.account))
        self.assertEqual(play.why_none, feed.WHY_PIP_OWN_ACCOUNTS)
        self.assertIsNone(play.pip)
        self.assertIsNotNone(pip_channels.resolve_feed(fid, feed_cfg()).pip)
        self.set_accounts([self.account.id])
        resp = self.client.get(f'/live/{FRIEND}/{FRIEND_PASS}/{fid}.ts')
        self.assertEqual(resp.status_code, 409)
        feed.flush_play_log()
        line = OutputFeedPlay.query.filter_by(kind=FEED_PLAY_REFUSED).one()
        self.assertIn('shared by everyone watching', line.message)

    def test_a_waiting_screen_carries_its_logins_accounts(self):
        """Built before start_session() puts the login on the play, so the first recheck of a
        sports channel's waiting screen would otherwise walk the output's list."""
        _stand_in_screens(self)
        self.set_accounts([self.other.id])
        play = feed.Play(self.fid_alpha, 'NHL 1', sports=True, fault=False, why_none='no game on')
        play.served = mock.Mock(row=mock.Mock(id=1))
        with mock.patch.object(feed, 'resolve_play', return_value=play):
            session = feed.start_session(self.fid_alpha, '203.0.113.7', login=self.friend())
        self.assertEqual(session.state, feed.STATE_WAITING)
        self.assertEqual(session.login_accounts, {self.other.id})

    def test_a_relayed_play_carries_its_logins_accounts(self):
        self.set_accounts([self.account.id])
        with mock.patch.object(feed, '_launch'), mock.patch.object(feed, '_ensure_reaper'):
            session = feed.start_session(self.fid_alpha, '203.0.113.7', login=self.friend())
        self.assertEqual(session.login_accounts, {self.account.id})


class AccountsChangeTests(_AccountsCase):
    def post_accounts(self, value):
        return self.client.post(f'/api/iptv-output/logins/{self.login.id}', json={'accounts': value})

    def test_accounts_are_saved_and_the_outputs_list_clears_them(self):
        self.assertEqual(self.post_accounts([self.other.id]).status_code, 200)
        db.session.expire_all()
        self.assertEqual(json.loads(self.login.accounts), [self.other.id])
        self.assertEqual(self.post_accounts(None).status_code, 200)
        db.session.expire_all()
        self.assertIsNone(self.login.accounts)

    def test_bad_accounts_are_refused(self):
        for value in ([], 'all', [True], ['1'], [1.5], [999_999]):
            self.assertEqual(self.post_accounts(value).status_code, 400, value)
        db.session.expire_all()
        self.assertIsNone(self.login.accounts)

    def test_a_change_ends_only_the_plays_outside_the_new_accounts(self):
        self.login.max_streams = 3
        db.session.commit()
        out = self.play(self.fid_alpha, login=self.friend())
        kept = self.play(self.fid_group, login=self.friend())
        kept.account_id = self.other.id
        mains = self.play(self.fid_alpha)
        resp = self.post_accounts([self.other.id])
        self.assertEqual(resp.get_json()['ended'], 1)
        self.assertEqual(out.reason, feed.REASON_ACCOUNTS)
        self.assertTrue(kept.live)
        self.assertEqual(kept.login_accounts, {self.other.id})     # its next re-ask is held to them
        self.assertTrue(mains.live)
        self.assertIsNone(mains.login_accounts)
        self.assertTrue(feed.REASON_TEXT[feed.REASON_ACCOUNTS])

    def test_back_to_the_outputs_list_ends_a_play_the_output_leaves_out(self):
        self.t.sandbox_config(feed_cfg(accounts=[self.other.id]))
        self.set_accounts([self.account.id])
        session = self.play(self.fid_alpha, login=self.friend())
        session.login_accounts = self.own(self.account)
        self.assertEqual(feed.set_login_accounts(self.login.id, None), 1)
        self.assertEqual(session.reason, feed.REASON_ACCOUNTS)

    def test_a_picture_in_picture_play_ends_when_the_login_gets_its_own_accounts(self):
        session = self.play(feed.feed_id_for_pip(1), login=self.friend())
        session.pip_row_id = 1
        session.account_id = None
        self.assertEqual(feed.set_login_accounts(self.login.id, self.own(self.account)), 1)
        self.assertEqual(session.reason, feed.REASON_ACCOUNTS)

    def test_a_play_let_in_on_the_old_accounts_takes_the_new_ones(self):
        login = self.friend()                                     # what the gate read
        feed.set_login_accounts(self.login.id, self.own(self.other))    # chosen again meanwhile
        started = []

        def start(feed_id, *_a, **_k):
            started.append(self.play(feed_id))
            return started[-1]
        with mock.patch.object(feed, '_start_session', side_effect=start):
            with self.assertRaises(feed.FeedRefused) as cm:
                feed.start_session(self.fid_alpha, 'a', login=login)
            started_ok = []

            def start_other(feed_id, *_a, **_k):
                s = self.play(feed_id)
                s.account_id = self.other.id
                started_ok.append(s)
                return s
            with mock.patch.object(feed, '_start_session', side_effect=start_other):
                kept = feed.start_session(self.fid_group, 'a', login=login)
        self.assertEqual(cm.exception.status, 409)
        self.assertEqual(started[0].reason, feed.REASON_ACCOUNTS)
        self.assertTrue(kept.live)
        self.assertEqual(kept.login_accounts, {self.other.id})

    def test_the_outputs_account_switch_spares_a_login_on_its_own_accounts(self):
        self.login.max_streams = 2
        db.session.commit()
        own = self.play(self.fid_alpha, login=self.friend())
        own.login_accounts = self.own(self.account)
        follows = self.play(self.fid_beta, login=self.friend())
        mains = self.play(self.fid_alpha)
        self.assertEqual(feed.stop_account(self.account.id), 2)
        self.assertTrue(own.live)
        self.assertEqual((follows.reason, mains.reason), (feed.REASON_ACCOUNT_OFF, feed.REASON_ACCOUNT_OFF))


class AccountsPageTests(_AccountsCase):
    def logins_card(self):
        page = self.client.get('/iptv-output').get_data(as_text=True)
        return page[page.index('id="of-logins"'):page.index('id="of-watching"')]

    def test_the_card_names_its_accounts_or_the_outputs(self):
        self.assertIn('Same as the output', self.logins_card())
        self.set_accounts([self.other.id, 999_999])
        card = self.logins_card()
        self.assertIn('>Other<', card)
        self.assertIn('1 picked account deleted since', card)
        self.assertIn('data-act="login-accounts"', card)

    def test_the_card_counts_what_its_own_accounts_list(self):
        self.login.lineup = json.dumps([self.fid_alpha, self.fid_group])
        self.set_accounts([self.other.id])
        card = self.logins_card()
        self.assertIn('1 channel<', card)
        self.assertIn('1 more picked, not on its accounts', card)
        self.assertNotIn('not on the output now', card)

    def test_the_accounts_dialog_lists_every_account_and_the_picks(self):
        self.t.sandbox_config(feed_cfg(accounts=[self.account.id]))
        d = self.client.get(f'/api/iptv-output/logins/{self.login.id}/accounts').get_json()
        self.assertFalse(d['own'])
        self.assertEqual({a['name']: a['on_output'] for a in d['accounts']}, {'Acct': True, 'Other': False})
        self.set_accounts([self.other.id, 999_999])
        d = self.client.get(f'/api/iptv-output/logins/{self.login.id}/accounts').get_json()
        self.assertEqual((d['own'], d['picked']), (True, [self.other.id]))

    def test_the_channels_dialog_marks_rows_its_accounts_leave_out(self):
        self.set_accounts([self.other.id])
        d = self.client.get(f'/api/iptv-output/logins/{self.login.id}/channels').get_json()
        rows = {r['name']: r['off'] for r in d['rows']}
        self.assertEqual(rows['Alpha'], f'Not for this login: {feed.LEFT_OUT_LOGIN_ACCOUNT}')
        self.assertEqual(rows['Game feeds'], '')


class AccountsMigrationTests(unittest.TestCase):
    def test_step_154_adds_the_column_and_runs_again(self):
        import sqlite3
        from app.migrations import _m154_output_feed_login_accounts
        conn = sqlite3.connect(':memory:')
        conn.execute('CREATE TABLE output_feed_logins (id INTEGER PRIMARY KEY, username TEXT)')
        conn.execute("INSERT INTO output_feed_logins (username) VALUES ('kim')")
        for _ in range(2):
            _m154_output_feed_login_accounts(conn, conn.cursor())
        cols = [r[1] for r in conn.execute('PRAGMA table_info(output_feed_logins)')]
        self.assertIn('accounts', cols)
        self.assertEqual(conn.execute('SELECT accounts FROM output_feed_logins').fetchone(), (None,))
