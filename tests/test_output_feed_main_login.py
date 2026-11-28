"""The main IPTV output login becomes one login in the list (dev/changelog/1781).

Until then the owner's own login lived in config.yaml: a typed password of any length, no stream
limit, and the only login sent straight to the IPTV provider or told a refusal's whole reason.
What these hold: startup moves that pair into a row with the same username and password, so no
TV is set up again, and takes it out of config.yaml only after the row is committed; a start
that stopped between the two is finished without a second row; a pair a row's username already
has with another password is dropped with an alert. Every login is held by its own stream limit
and its own counts; being sent straight to the provider and being told the whole reason are
switches on each login, off for a new one, and neither holds on the public port. Readiness names
a login whose password is short while the public port is open."""
import unittest
from unittest import mock

import app.output_feed as feed
from app import _move_main_output_login, db, public_listener
from app.config import _load_config_file, load_config
from app.database import Alert, OutputFeedLogin
from tests.support.app import make_test_app
from tests.support.output_feed import PASS, USER, FeedCase, add_login, feed_cfg
from tests.support.seed import make_channel

KIM, KIM_PASS = 'kim', 'couch42'


def _old_cfg(username=USER, password=PASS, **over):
    """A config.yaml from before dev/changelog/1781, the main login's pair in the block."""
    block = {'enabled': True, 'username': username, 'password': password}
    block.update(over)
    return {'integrations': {'output_feed': block}}


class MoveTests(unittest.TestCase):
    def setUp(self):
        self.t = make_test_app(csrf=False)
        self.addCleanup(self.t.cleanup)

    def block(self):
        return ((_load_config_file() or {}).get('integrations') or {}).get('output_feed') or {}

    def alerts(self, alert_type):
        return Alert.query.filter_by(alert_type=alert_type).all()

    def test_the_pair_becomes_a_row_and_leaves_config_yaml(self):
        self.t.sandbox_config(_old_cfg(max_viewers=3))
        _move_main_output_login()
        row = OutputFeedLogin.query.one()
        self.assertEqual((row.username, row.password, row.name), (USER, PASS, 'Main login'))
        self.assertEqual(row.max_streams, 3)
        self.assertTrue(row.enabled and row.provider_direct and row.full_refusals)
        self.assertIsNone(row.lineup)
        self.assertIsNone(row.accounts)
        self.assertNotIn('username', self.block())
        self.assertNotIn('password', self.block())
        self.assertTrue(self.block()['enabled'], 'the output stays on')
        self.assertEqual(len(self.alerts('SETTING_MOVED')), 1)
        # The TV set up with the old pair is let in as it was.
        resp = self.t.client.get(f'/player_api.php?username={USER}&password={PASS}')
        self.assertEqual(resp.status_code, 200)

    def test_the_stream_limit_is_held_to_the_most_a_login_may_have(self):
        self.t.sandbox_config(_old_cfg(max_viewers=99))
        _move_main_output_login()
        self.assertEqual(OutputFeedLogin.query.one().max_streams, feed.MAX_STREAMS_BOUNDS[1])

    def test_a_numeric_pair_from_a_hand_edited_config_moves_as_text(self):
        self.t.sandbox_config(_old_cfg(username=1, password=22))
        _move_main_output_login()
        row = OutputFeedLogin.query.one()
        self.assertEqual((row.username, row.password), ('1', '22'))
        self.assertEqual(self.t.client.get('/player_api.php?username=1&password=22').status_code, 200)

    def test_a_second_start_changes_nothing(self):
        self.t.sandbox_config(_old_cfg())
        _move_main_output_login()
        _move_main_output_login()
        self.assertEqual(OutputFeedLogin.query.count(), 1)
        self.assertEqual(len(self.alerts('SETTING_MOVED')), 1)

    def test_a_start_that_stopped_after_the_row_only_removes_the_leaves(self):
        """The row was committed and config.yaml was not written yet: the leaves are still the
        obligation, and the retry says so rather than adding a second row."""
        self.t.sandbox_config(_old_cfg())
        add_login(USER, PASS)
        with self.assertLogs('app', 'WARNING') as logs:
            _move_main_output_login()
        self.assertEqual(OutputFeedLogin.query.count(), 1)
        self.assertNotIn('username', self.block())
        self.assertIn('was already moved', '\n'.join(logs.output))
        # The first start stopped before it raised the alert; the retry raises it, once.
        self.assertEqual(len(self.alerts('SETTING_MOVED')), 1)
        _move_main_output_login()
        self.assertEqual(len(self.alerts('SETTING_MOVED')), 1)

    def test_a_username_another_login_has_is_dropped_with_an_alert(self):
        self.t.sandbox_config(_old_cfg())
        add_login(USER, 'another-password')
        _move_main_output_login()
        row = OutputFeedLogin.query.one()
        self.assertEqual(row.password, 'another-password')
        self.assertNotIn('password', self.block())
        self.assertEqual(len(self.alerts('SETTING_REMOVED')), 1)

    def test_an_empty_pair_is_removed_and_makes_no_row(self):
        self.t.sandbox_config(_old_cfg(password=''))
        _move_main_output_login()
        self.assertEqual(OutputFeedLogin.query.count(), 0)
        self.assertNotIn('username', self.block())
        self.assertEqual(self.alerts('SETTING_MOVED'), [])

    def test_create_app_runs_it(self):
        """The move is wired into startup, after the migrations."""
        import app as app_module
        with mock.patch.object(app_module, '_move_main_output_login') as move:
            t = make_test_app()
            self.addCleanup(t.cleanup)
        move.assert_called_once_with()


class _TwoLogins(FeedCase):
    """The rig's login is the moved one (both switches on); Kim's is a new one (both off)."""

    def setUp(self):
        super().setUp()
        self.kim = add_login(KIM, KIM_PASS, name='Kim', max_streams=1, provider_direct=False,
                             full_refusals=False)

    def as_login(self, user, password):
        return feed.feed_login_for(feed.feed_config(feed_cfg()), user, password)


class SendStraightToTheProviderTests(_TwoLogins):
    def test_only_a_login_with_the_switch_is_sent_to_the_provider_and_never_on_the_public_port(self):
        fid = feed.feed_id_for_channel(self.alpha.id)
        main, kim = self.as_login(USER, PASS), self.as_login(KIM, KIM_PASS)
        for mode in (feed.MODE_REDIRECT, feed.MODE_MIXED):
            fcfg = {'mode': mode}
            self.assertFalse(feed.relays_for(fcfg, fid, main), mode)
            self.assertTrue(feed.relays_for(fcfg, fid, main, public=True), mode)
            self.assertTrue(feed.relays_for(fcfg, fid, kim), mode)
        self.assertTrue(feed.relays_for({'mode': feed.MODE_PROXY}, fid, main))

    def test_the_play_route_redirects_only_a_login_with_the_switch(self):
        self.t.sandbox_config(feed_cfg(mode=feed.MODE_REDIRECT))
        fid = feed.feed_id_for_channel(self.alpha.id)
        resp = self.client.get(f'/live/{USER}/{PASS}/{fid}.ts')
        self.assertEqual(resp.status_code, 302)
        with mock.patch.object(feed, 'start_session', side_effect=feed.FeedRefused('stub', 409)) as start:
            resp = self.client.get(f'/live/{USER}/{PASS}/{fid}.ts',
                                   environ_base={public_listener.PUBLIC_ENVIRON_KEY: True})
            self.assertEqual(resp.status_code, 409)
            resp = self.client.get(f'/live/{KIM}/{KIM_PASS}/{fid}.ts')
            self.assertEqual(resp.status_code, 409)
        self.assertEqual([c.kwargs['login'].username for c in start.call_args_list], [USER, KIM])

    def test_a_session_on_a_login_sent_to_the_provider_is_not_relayed_in_every_mode(self):
        s = feed.RelaySession(id='x', feed_id=1, label='', client='', group_id=None,
                              channel_id=None, channel_name='', account_id=None,
                              ring=feed.Ring(1024, 0), started_mono=0.0, feed_login_id=self.kim.id)
        self.assertTrue(s.always_relayed)
        s.provider_direct = True
        self.assertFalse(s.always_relayed)
        s.public = True
        self.assertTrue(s.always_relayed)


class WholeReasonTests(_TwoLogins):
    def setUp(self):
        super().setUp()
        self.fid = feed.feed_id_for_channel(self.alpha.id)
        patcher = mock.patch.object(feed, 'start_session',
                                    side_effect=feed.FeedRefused('The whole sentence for the owner.', 409))
        patcher.start()
        self.addCleanup(patcher.stop)

    def body(self, user, password, public=False):
        environ = {public_listener.PUBLIC_ENVIRON_KEY: True} if public else {}
        return self.client.get(f'/live/{user}/{password}/{self.fid}.ts',
                               environ_base=environ).get_json()['error']

    def test_only_a_login_with_the_switch_is_told_the_whole_reason_and_only_at_home(self):
        self.assertEqual(self.body(USER, PASS), 'The whole sentence for the owner.')
        self.assertEqual(self.body(USER, PASS, public=True), feed.SHORT_REFUSALS[409])
        self.assertEqual(self.body(KIM, KIM_PASS), feed.SHORT_REFUSALS[409])
        self.kim.full_refusals = True
        db.session.commit()
        self.assertEqual(self.body(KIM, KIM_PASS), 'The whole sentence for the owner.')


class EveryLoginIsHeldTheSameTests(_TwoLogins):
    def test_the_moved_login_reports_its_own_limit_and_plays(self):
        info = self.get('/player_api.php').get_json()['user_info']
        self.assertEqual(info['max_connections'], '20')
        self.assertEqual(info['active_cons'], '0')

    def test_its_plays_are_held_by_its_own_stream_limit(self):
        main = self.as_login(USER, PASS)
        self.assertEqual(main.max_streams, 20)
        # start_session builds a room for every login now, the moved one included.
        with mock.patch.object(feed, '_start_session', side_effect=feed.FeedRefused('stop', 409)) as start:
            with self.assertRaises(feed.FeedRefused):
                feed.start_session(self.fid_alpha(), 'tv', login=main)
        self.assertIsNotNone(start.call_args.args[6])

    def fid_alpha(self):
        return feed.feed_id_for_channel(self.alpha.id)

    def test_every_play_carries_its_logins_provider_switch(self):
        """A play _start_session already put a login on still takes the switch, so in redirect
        mode a relayed play on a login sent to the provider reads as one from before the mode
        changed, not as one relayed in every mode."""
        main = self.as_login(USER, PASS)

        def built(*args, **kwargs):
            s = feed.RelaySession(id='built', feed_id=self.fid_alpha(), label='', client='tv',
                                  group_id=None, channel_id=self.alpha.id, channel_name='Alpha',
                                  account_id=self.account.id, ring=feed.Ring(1024, 0),
                                  started_mono=0.0, feed_login_id=main.feed_login_id,
                                  feed_login=main.username)
            return s

        with mock.patch.object(feed, '_start_session', side_effect=built):
            session = feed.start_session(self.fid_alpha(), 'tv', login=main)
        self.addCleanup(lambda: feed._sessions.pop(session.id, None))
        self.assertTrue(session.provider_direct)
        self.assertFalse(session.always_relayed)
        self.assertFalse(feed.seen_in_mode(session, feed.MODE_REDIRECT))


class LoginSwitchRouteTests(_TwoLogins):
    def test_a_new_login_starts_with_both_switches_off_and_a_save_moves_each(self):
        resp = self.client.post('/api/iptv-output/logins', json={'name': 'Sam', 'username': 'sam'})
        row = db.session.get(OutputFeedLogin, resp.get_json()['id'])
        self.assertFalse(row.provider_direct or row.full_refusals)
        resp = self.client.post(f'/api/iptv-output/logins/{row.id}',
                                json={'provider_direct': True, 'full_refusals': True})
        self.assertEqual(resp.status_code, 200)
        db.session.refresh(row)
        self.assertTrue(row.provider_direct and row.full_refusals)
        resp = self.client.post(f'/api/iptv-output/logins/{row.id}', json={'provider_direct': 'yes'})
        self.assertEqual(resp.status_code, 400)

    def test_the_old_main_login_route_is_gone(self):
        resp = self.client.post('/api/iptv-output/login', json={'username': 'x', 'password': 'y'})
        self.assertIn(resp.status_code, (404, 405))

    def test_the_first_login_turns_an_output_left_on_off(self):
        OutputFeedLogin.query.delete()
        db.session.commit()
        self.assertTrue(feed.is_enabled(feed.feed_config(load_config())))
        resp = self.client.post('/api/iptv-output/logins', json={'name': 'Me', 'username': 'me'})
        self.assertTrue(resp.get_json()['first'])
        self.assertFalse(feed.is_enabled(feed.feed_config(load_config())))
        resp = self.client.post('/api/iptv-output/logins', json={'name': 'Sam', 'username': 'sam'})
        self.assertFalse(resp.get_json()['first'])

    def test_deleting_the_last_login_turns_the_output_off(self):
        """An output that is on with no login lets nobody in, and its page then has no switch to
        turn it off."""
        resp = self.client.delete(f'/api/iptv-output/logins/{self.kim.id}')
        self.assertEqual((resp.get_json()['last'], resp.get_json()['turned_off']), (False, False))
        self.assertTrue(feed.is_enabled(feed.feed_config(load_config())))
        resp = self.client.delete(f'/api/iptv-output/logins/{self.login.id}')
        self.assertEqual((resp.get_json()['last'], resp.get_json()['turned_off']), (True, True))
        self.assertFalse(feed.is_enabled(feed.feed_config(load_config())))

    def test_the_page_draws_one_card_for_every_login_and_a_picker(self):
        html = self.client.get('/iptv-output').get_data(as_text=True)
        self.assertIn('<h2>Logins <span class="cnt">2</span></h2>', html)
        self.assertNotIn('Logins for other users', html)
        self.assertIn('id="of-conn-login"', html)
        self.assertIn(f'data-conn-login="{self.kim.id}"', html)
        self.assertIn('Plays straight from your provider at home', html)
        self.assertNotIn('data-act="login"', html)

    def test_with_no_login_the_page_is_not_set_up_and_offers_add_a_login(self):
        OutputFeedLogin.query.delete()
        db.session.commit()
        html = self.client.get('/iptv-output').get_data(as_text=True)
        self.assertIn('The IPTV output is not set up yet', html)
        self.assertIn('id="of-logins"', html)
        self.assertNotIn('id="of-connect"', html)


class ShortPasswordReadinessTests(_TwoLogins):
    """The moved login keeps the password a person typed; the open public port puts it in
    reach of guessing from anywhere."""

    def facts(self):
        return feed.readiness_facts(feed_cfg())

    def answer(self, short):
        from app.readiness import _output_public_port
        facts = dict(self.facts(), public_port=38517, public_url='https://tv.example',
                     listener={'state': public_listener.STATE_SERVING, 'port': 38517,
                               'host': '0.0.0.0', 'error': ''})
        return _output_public_port(facts, 'tested', 'detail', mock.Mock(public=True), short)

    def test_the_facts_name_every_login_on_with_a_short_password(self):
        self.assertEqual(self.facts()['short_passwords'], ['Main login', 'Kim'])
        self.kim.password = 'x' * feed.STRONG_PASSWORD_LENGTH
        db.session.commit()
        self.assertEqual(self.facts()['short_passwords'], ['Main login'])
        self.login_row().enabled = False
        db.session.commit()
        self.assertEqual(self.facts()['short_passwords'], [], 'a login turned off lets nobody in')

    def login_row(self):
        return OutputFeedLogin.query.filter_by(username=USER).one()

    def test_the_open_public_port_names_them(self):
        from app.readiness import ATTENTION
        r = self.answer(self.facts()['short_passwords'])
        self.assertEqual(r.status, ATTENTION)
        self.assertIn('Main login and Kim', r.found)
        self.assertIsNone(self.answer([]))

    def test_the_summary_says_who_redirect_mode_sends_to_the_provider(self):
        from app.readiness import Context, _check_output_feed
        make_channel(self.account, name='On', in_guide=True)
        db.session.commit()
        self.t.sandbox_config(feed_cfg(mode=feed.MODE_REDIRECT))
        self.assertIn('played straight from the provider on 1 login set to',
                      _check_output_feed(Context()).found)
        self.login.provider_direct = False
        db.session.commit()
        self.assertIn('relayed through ChannelBin, since no login that is on is set to play',
                      _check_output_feed(Context()).found)

    def test_the_support_bundle_counts_them_rather_than_naming_them(self):
        from app.readiness import Context, _check_output_feed
        with mock.patch('app.readiness._output_public_port', return_value=None) as port:
            _check_output_feed(Context(pseudonymize=True))
        self.assertEqual(port.call_args.args[4], ['2 logins'])


