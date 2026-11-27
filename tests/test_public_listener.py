"""The IPTV output's public port (app/public_listener.py) and the wrong-login lockout
(output_feed.login_failures), dev/changelog/1761.

The public port exists so a reverse proxy can put the IPTV output on the internet while the web
UI stays at home. What these hold: the port answers the five feed routes and ChannelBin's own
logo and nothing else, not even a redirect; a play on it is relayed in every mode, so the
provider's stream URL never leaves; a port that cannot be opened is reported and never takes
the process down; and an address that gets the login wrong too often is refused, right password
included, until the lockout ends or is cleared from the page."""
import json
import socket
import urllib.error
import urllib.request
from unittest import mock

from werkzeug.test import Client

import app.output_feed as feed
from app import public_listener
from app.public_listener import FeedOnly, PUBLIC_ENDPOINTS
from tests.support.output_feed import PASS, USER, FeedCase, feed_cfg


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(('127.0.0.1', 0))
        return s.getsockname()[1]


class _PublicCase(FeedCase):
    def setUp(self):
        super().setUp()
        self.public = Client(FeedOnly(self.t.app))

    def public_get(self, path):
        return self.public.get(path)


class AllowlistTests(_PublicCase):
    def test_the_web_ui_is_not_on_the_public_port(self):
        for path in ('/', '/settings', '/iptv-output', '/api/settings/reveal?path=x',
                     '/api/iptv-output/logins', '/static/js/util.js', '/login', '/guide',
                     '/live/', '/logo/', '/player_api.php/../settings'):
            resp = self.public_get(path)
            self.assertEqual(resp.status_code, 404, path)
            self.assertNotIn(b'<html', resp.get_data().lower(), path)

    def test_the_feed_answers_on_the_public_port(self):
        q = f'?username={USER}&password={PASS}'
        for path in ('/player_api.php', '/player_api.php' + q + '&action=get_live_streams',
                     '/get.php', '/xmltv.php'):
            target = path if '?' in path else path + q
            self.assertEqual(self.public_get(target).status_code, 200, target)
        self.assertEqual(self.public_get(feed.OWN_LOGO_PATH).status_code, 200)

    def test_an_id_too_long_for_the_database_is_no_channel_not_an_error(self):
        """dev/docs/BUGS.md 2026-11-26: an id past SQLite's largest integer raised in the
        query, a 500 whose log line carried the address, login included."""
        huge = '9' * 26
        for path in (f'/live/{USER}/{PASS}/{huge}.ts', f'/live/{USER}/{PASS}/{huge}',
                     f'/logo/{USER}/{PASS}/{huge}'):
            for client in (self.public, self.t.client):
                resp = client.get(path)
                self.assertEqual(resp.status_code, 404, path)
                self.assertEqual(json.loads(resp.get_data())['error'],
                                 'No such channel on the IPTV output')

    def test_a_wrong_login_on_the_public_port_is_the_feeds_401_not_a_404(self):
        resp = self.public_get('/player_api.php?username=x&password=y')
        self.assertEqual(resp.status_code, 401)
        self.assertEqual(json.loads(resp.get_data())['error'], feed.AUTH_FAILED_MESSAGE)

    def test_a_tidier_path_redirect_is_never_followed_out_of_the_feed(self):
        """A route's merge-slashes redirect is a 404 here, so no answer ever points a player at
        a path this port does not serve."""
        self.assertEqual(self.public_get(f'/live//{USER}/{PASS}/1.ts').status_code, 404)
        resp = self.public.post(f'/player_api.php?username={USER}&password={PASS}')
        self.assertEqual(resp.status_code, 404)

    def test_the_allowlist_is_exactly_the_feeds_routes(self):
        """A route added to the output's blueprint is not public until it is named here."""
        feed_endpoints = {rule.endpoint for rule in self.t.app.url_map.iter_rules()
                          if rule.endpoint.startswith('output_feed.')}
        self.assertEqual(feed_endpoints, set(PUBLIC_ENDPOINTS))


class AlwaysRelayedTests(_PublicCase):
    FEED = feed_cfg(mode=feed.MODE_REDIRECT)

    def test_relays_for_relays_the_public_port_in_every_mode(self):
        main = feed.main_login(feed_cfg()['integrations']['output_feed'])
        fid = feed.feed_id_for_channel(self.alpha.id)
        for mode in (feed.MODE_REDIRECT, feed.MODE_MIXED):
            self.assertFalse(feed.relays_for({'mode': mode}, fid, main), mode)
            self.assertTrue(feed.relays_for({'mode': mode}, fid, main, public=True), mode)

    def test_a_main_login_play_on_the_public_port_is_relayed_in_redirect_mode(self):
        fid = feed.feed_id_for_channel(self.alpha.id)
        with mock.patch.object(feed, 'start_session', side_effect=feed.FeedRefused('stub', 409)) as start, \
                mock.patch.object(feed, 'redirect_play') as redirect:
            resp = self.public_get(f'/live/{USER}/{PASS}/{fid}.ts')
        self.assertEqual(resp.status_code, 409)
        self.assertIs(start.call_args.kwargs['public'], True)
        redirect.assert_not_called()
        # The same tune-in on the web UI's port is still redirected.
        with mock.patch.object(feed, 'redirect_play', return_value=('http://provider/x', None)), \
                mock.patch.object(feed, 'start_session') as start:
            resp = self.client.get(f'/live/{USER}/{PASS}/{fid}.ts')
        self.assertEqual(resp.status_code, 302)
        start.assert_not_called()

    def test_watching_now_lists_a_public_play_in_redirect_mode(self):
        s = self.session(feed.feed_id_for_channel(self.alpha.id))
        self.assertFalse(feed.seen_in_mode(s, feed.MODE_REDIRECT))
        s.public = True
        self.assertTrue(feed.seen_in_mode(s, feed.MODE_REDIRECT))


class ListenerTests(FeedCase):
    def test_it_serves_the_feed_and_nothing_else_on_a_real_port(self):
        port = _free_port()
        cfg = feed_cfg(public_port=port, public_host='127.0.0.1')
        self.addCleanup(public_listener.stop)
        status = public_listener.start(self.t.app, cfg)
        self.assertEqual((status['state'], status['port']), ('serving', port))
        base = f'http://127.0.0.1:{port}'
        with urllib.request.urlopen(f'{base}/player_api.php?username={USER}&password={PASS}',
                                    timeout=10) as resp:
            self.assertEqual(json.loads(resp.read())['user_info']['username'], USER)
        with self.assertRaises(urllib.error.HTTPError) as caught:
            urllib.request.urlopen(f'{base}/settings', timeout=10)
        self.assertEqual(caught.exception.code, 404)
        public_listener.stop()
        self.assertEqual(public_listener.status()['state'], 'off')
        with self.assertRaises(OSError):
            urllib.request.urlopen(f'{base}/player_api.php', timeout=2)

    def test_a_port_in_use_is_reported_and_does_not_end_the_process(self):
        with socket.socket() as taken:
            taken.bind(('127.0.0.1', 0))
            taken.listen(1)
            port = taken.getsockname()[1]
            with self.assertLogs('app.public_listener', 'ERROR'):
                status = public_listener.start(self.t.app,
                                               feed_cfg(public_port=port, public_host='127.0.0.1'))
        self.assertEqual(status['state'], 'failed')
        self.assertTrue(status['error'])

    def test_no_port_set_opens_nothing(self):
        self.assertEqual(public_listener.start(self.t.app, feed_cfg())['state'], 'off')


class LockoutTests(FeedCase):
    def wrong(self):
        return self.get('/player_api.php', password='nope')

    def test_too_many_wrong_logins_lock_the_address_out_right_password_included(self):
        for _ in range(feed.LOGIN_MAX_FAILURES - 1):
            self.assertEqual(self.wrong().status_code, 401)
        # A right login does not clear the count: with many logins on one door, knowing your own
        # says nothing about anyone else's, and clearing would let a login holder guess forever.
        self.assertEqual(self.get('/player_api.php').status_code, 200)
        with self.assertLogs('app.routes.output_feed', 'WARNING') as logs:
            self.wrong()
        self.assertIn('locked out', logs.output[0])
        resp = self.get('/player_api.php')
        self.assertEqual(resp.status_code, 401)
        self.assertEqual(resp.get_json()['error'], feed.AUTH_FAILED_MESSAGE)
        self.assertEqual([ip for ip, _ in feed.login_failures.locked_out()], ['127.0.0.1'])

    def test_the_page_lists_a_lockout_and_clears_it(self):
        for _ in range(feed.LOGIN_MAX_FAILURES):
            self.wrong()
        page = self.client.get('/iptv-output').get_data(as_text=True)
        self.assertIn('id="of-lockouts"', page)
        self.assertIn('127.0.0.1 until', page)
        resp = self.client.post('/api/iptv-output/lockouts/clear')
        self.assertEqual(resp.get_json(), {'success': True, 'cleared': 1})
        self.assertEqual(self.get('/player_api.php').status_code, 200)
        self.assertNotIn('id="of-lockouts"', self.client.get('/iptv-output').get_data(as_text=True))

    def test_the_web_login_has_its_own_counter(self):
        from app import auth
        for _ in range(feed.LOGIN_MAX_FAILURES):
            self.wrong()
        self.assertIsNone(auth.check_lockout('127.0.0.1'))

    def test_nothing_counts_while_the_output_is_off(self):
        self.t.sandbox_config(feed_cfg(enabled=False))
        for _ in range(feed.LOGIN_MAX_FAILURES + 2):
            self.wrong()
        self.assertEqual(feed.login_failures.locked_out(), [])


class SettingsTests(FeedCase):
    def save(self, leaf, value):
        return self.client.post('/api/settings/field',
                                json={'path': f'integrations.output_feed.{leaf}', 'value': value})

    def test_the_public_port_is_checked(self):
        for bad in ('abc', 80, 70000, 5000):
            self.assertEqual(self.save('public_port', bad).status_code, 400, bad)
        self.assertEqual(self.save('public_port', feed.SUGGESTED_PUBLIC_PORT).status_code, 200)
        self.assertEqual(self.save('public_port', 0).status_code, 200)

    def test_public_host_is_config_yaml_only_and_public_url_is_a_whole_address(self):
        self.assertEqual(self.save('public_host', '127.0.0.1').status_code, 400)
        self.assertEqual(self.save('public_url', 'tv.example.com').status_code, 400)
        self.assertEqual(self.save('public_url', 'https://tv.example.com/').status_code, 200)


class PageTests(FeedCase):
    def test_the_details_to_send_name_the_outside_address_once_the_port_is_open(self):
        self.t.sandbox_config(feed_cfg(public_port=38517, public_url='https://tv.example.com/'))
        page = self.client.get('/iptv-output').get_data(as_text=True)
        self.assertNotIn('data-server="https://tv.example.com"', page, 'not open yet')
        self.assertIn('opens when ChannelBin is restarted', page)
        with mock.patch.object(public_listener, 'status',
                               return_value={'state': 'serving', 'host': '0.0.0.0',
                                             'port': 38517, 'error': ''}):
            page = self.client.get('/iptv-output').get_data(as_text=True)
        self.assertIn('data-server="https://tv.example.com"', page)

    def test_the_mode_help_says_the_public_port_is_relayed(self):
        self.t.sandbox_config(feed_cfg(mode=feed.MODE_REDIRECT, public_port=38517))
        with mock.patch.object(public_listener, 'status',
                               return_value={'state': 'serving', 'host': '0.0.0.0',
                                             'port': 38517, 'error': ''}):
            page = self.client.get('/iptv-output').get_data(as_text=True)
        self.assertIn('A play on the public port always goes through ChannelBin', page)


class ReadinessTests(FeedCase):
    def check(self, status, behind_proxy=True, **over):
        from app import readiness
        self.t.sandbox_config(feed_cfg(**over))
        from app import proxy_trust
        self.t.app.config[proxy_trust.CONFIG_KEY] = proxy_trust.Policy(public=behind_proxy)
        self.addCleanup(self.t.app.config.pop, proxy_trust.CONFIG_KEY, None)
        with mock.patch.object(public_listener, 'status', return_value=status):
            ctx = readiness.Context()
            return readiness._check_output_feed(ctx)

    def serving(self, port=38517):
        return {'state': 'serving', 'host': '0.0.0.0', 'port': port, 'error': ''}

    def test_a_port_that_could_not_open_is_a_problem(self):
        from app.readiness import PROBLEM
        r = self.check({'state': 'failed', 'host': '0.0.0.0', 'port': 38517,
                        'error': 'Address already in use'}, public_port=38517)
        self.assertEqual(r.status, PROBLEM)
        self.assertIn('Address already in use', r.found)

    def test_the_proxy_must_be_declared(self):
        from app.readiness import PROBLEM
        r = self.check(self.serving(), behind_proxy=False, public_port=38517,
                       public_url='https://tv.example.com')
        self.assertEqual(r.status, PROBLEM)
        self.assertIn('The public port is behind a reverse proxy', r.found)

    def test_a_port_set_but_not_open_waits_for_a_restart(self):
        from app.readiness import ATTENTION
        r = self.check({'state': 'off', 'host': '', 'port': 0, 'error': ''}, public_port=38517)
        self.assertEqual(r.status, ATTENTION)
        self.assertIn('Restart', r.found)

    def test_ready_names_the_outside_address(self):
        from app.readiness import READY
        r = self.check(self.serving(), public_port=38517, public_url='https://tv.example.com')
        self.assertEqual(r.status, READY)
        self.assertIn('https://tv.example.com', r.detail)


class PublicPlayIsAlwaysRelayedTests(FeedCase):
    """A main-login play on the public port is relayed in redirect mode, so everywhere a
    redirected play is handled differently treats it as relayed (dev/changelog/1761)."""
    FEED = feed_cfg(mode=feed.MODE_REDIRECT)

    def test_its_waiting_screen_moves_to_the_game_in_place(self):
        play = feed.Play(feed.feed_id_for_channel(self.alpha.id), 'NHL 1', sports=True)
        play.member = self.alpha
        for public, ended in ((True, False), (False, True)):
            s = self.session(play.feed_id)
            s.public = public
            with mock.patch.object(feed, 'resolve_play', return_value=play), \
                    mock.patch.object(feed, '_to_game', return_value=None):
                feed._recheck_locked(s)
            self.assertEqual(s.reason == feed.REASON_WINDOW_OPENED, ended, public)

    def test_its_waiting_screen_says_the_game_starts_by_itself(self):
        play = feed.Play(feed.feed_id_for_channel(self.alpha.id), 'NHL 1', sports=True)
        seen = {}

        class _Screens:
            @staticmethod
            def screen_for(*_a, mode=None, **_k):
                seen['mode'] = mode

        with mock.patch.object(feed, '_screen_mod', return_value=_Screens):
            token = feed._tuning_public.set(True)
            try:
                feed._screen_of(play, feed_cfg(mode=feed.MODE_REDIRECT),
                                always_relayed=feed._tuning_always_relayed())
            finally:
                feed._tuning_public.reset(token)
            self.assertEqual(seen['mode'], feed.MODE_PROXY)
            feed._screen_of(play, feed_cfg(mode=feed.MODE_REDIRECT),
                            always_relayed=feed._tuning_always_relayed())
            self.assertEqual(seen['mode'], feed.MODE_REDIRECT)

    def test_start_session_marks_the_play_public(self):
        fid = feed.feed_id_for_channel(self.alpha.id)
        made = {}

        def start(*_a, **_k):
            made['always'] = feed._tuning_always_relayed()
            return self.session(fid)

        with mock.patch.object(feed, '_start_session', side_effect=start):
            s = feed.start_session(fid, 'a', public=True)
        self.assertTrue(s.public and s.always_relayed and made['always'])
        self.assertFalse(feed._tuning_always_relayed(), 'the flag ends with the tune-in')


class WebPortTests(FeedCase):
    def test_the_web_uis_port_is_never_taken(self):
        """Bound first, it would leave the web UI unable to open its own and end the process."""
        cfg = feed_cfg(public_port=5000)
        cfg['flask'] = {'port': 5000}
        with self.assertLogs('app.public_listener', 'ERROR'):
            status = public_listener.start(self.t.app, cfg)
        self.assertEqual(status['state'], public_listener.STATE_FAILED)
        self.assertIn('web UI', status['error'])

    def test_the_raw_editor_refuses_the_web_uis_port(self):
        from app.routes.settings import _raw_yaml_problems
        text = 'flask:\n  port: 5000\nintegrations:\n  output_feed:\n    public_port: 5000\n'
        _data, problems = _raw_yaml_problems(text)
        self.assertIn('integrations.output_feed.public_port', [p['path'] for p in problems])
        text = 'integrations:\n  output_feed:\n    public_port: 38517\n    public_url: tv\n'
        _data, problems = _raw_yaml_problems(text)
        paths = [p['path'] for p in problems]
        self.assertNotIn('integrations.output_feed.public_port', paths)
        self.assertIn('integrations.output_feed.public_url', paths)
