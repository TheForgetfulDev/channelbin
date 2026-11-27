"""Whose word ChannelBin takes for a visitor's address (app/proxy_trust.py), and how a wrong
login is counted (app/auth.py::FailureTracker). dev/docs/BUGS.md 2026-11-26 @ 09:56:54 PM,
dev/changelog/1769.

One "Behind a reverse proxy" switch used to cover the web UI's port and the IPTV output's
public port alike, so the port with no proxy in front believed a made-up X-Forwarded-For and
a caller took a new address for every wrong login (measured: 40 wrong logins, 40 tracked
addresses, none locked out). And the lockout counted each IPv6 address by itself, where one
home is handed a whole /64. What these hold: a port believes a proxy only when its own switch
is on and the peer is on the list, an address is counted under one normalized key, the overall
count is an alarm that refuses nobody, the upgrade keeps a working proxy believed, and
readiness says what each port has heard."""
import ipaddress
import os
import tempfile
import unittest
from datetime import timedelta
from unittest import mock

from werkzeug.test import Client

import app.config as config_mod
import app.output_feed as feed
from app import auth, proxy_trust, public_listener, readiness
from app.proxy_trust import PORT_PUBLIC, PORT_WEB, Policy
from app.public_listener import FeedOnly
from tests.support.output_feed import PASS, USER, FeedCase, feed_cfg

WRONG = f'/player_api.php?username={USER}&password=nope'
RIGHT = f'/player_api.php?username={USER}&password={PASS}'
PROXY = '192.168.1.10'
SERVING = {'state': 'serving', 'host': '0.0.0.0', 'port': 38517, 'error': ''}


class _TrustCase(FeedCase):
    POLICY = Policy()

    def setUp(self):
        super().setUp()
        self.public = Client(FeedOnly(self.t.app))
        self.policy(self.POLICY)

    def policy(self, policy):
        self.t.app.config[proxy_trust.CONFIG_KEY] = policy

    def ask(self, client, path, peer='10.0.0.9', forwarded=None):
        headers = {'X-Forwarded-For': forwarded} if forwarded else {}
        return client.get(path, headers=headers, environ_base={'REMOTE_ADDR': peer})

    def locked(self):
        return [found.address for found in feed.login_failures.locked_out()]


class PerPortTests(_TrustCase):
    """The public port trusts its proxy; the web UI's port, reached directly, does not."""
    POLICY = Policy(public=True)

    def test_a_made_up_address_on_the_web_port_is_not_believed(self):
        for n in range(feed.LOGIN_MAX_FAILURES):
            self.assertEqual(self.ask(self.client, WRONG, forwarded=f'203.0.113.{n}').status_code, 401)
        self.assertEqual(self.locked(), ['10.0.0.9'])
        self.assertEqual(len(feed.login_failures.lockouts), 1)
        self.assertEqual(self.ask(self.client, RIGHT, forwarded='203.0.113.200').status_code, 401)

    def test_the_proxy_on_the_public_port_is_believed(self):
        for _ in range(feed.LOGIN_MAX_FAILURES):
            self.ask(self.public, WRONG, peer=PROXY, forwarded='198.51.100.4')
        self.assertEqual(self.locked(), ['198.51.100.4'])
        # Another player through the same proxy is not locked out with it.
        self.assertEqual(self.ask(self.public, RIGHT, peer=PROXY, forwarded='198.51.100.5').status_code,
                         200)

    def test_the_web_switch_does_not_cover_the_public_port(self):
        self.policy(Policy(web=True))
        for n in range(feed.LOGIN_MAX_FAILURES):
            self.ask(self.public, WRONG, peer=PROXY, forwarded=f'203.0.113.{n}')
        self.assertEqual(self.locked(), [PROXY])

    def test_the_web_login_lockout_is_not_dodged_either(self):
        self.t.app.config['AUTH'] = {'enabled': True, 'password_hash': 'x'}
        seen = []
        with mock.patch('app.routes.auth.record_failure',
                        side_effect=lambda ip: seen.append(ip) or False):
            for n in range(3):
                self.client.post('/login', data={'password': 'nope'},
                                 headers={'X-Forwarded-For': f'203.0.113.{n}'},
                                 environ_base={'REMOTE_ADDR': '10.0.0.9'})
        self.assertEqual(seen, ['10.0.0.9'] * 3)

    def test_a_play_names_its_player_only_when_the_proxy_was_believed(self):
        with self.t.app.test_request_context(
                '/', environ_base={proxy_trust.TRUSTED_ENVIRON_KEY: False}):
            from flask import request
            self.assertFalse(proxy_trust.is_trusted(request))
        seen = {}

        def app(environ, start_response):
            seen['trusted'] = environ[proxy_trust.TRUSTED_ENVIRON_KEY]
            seen['addr'] = environ['REMOTE_ADDR']
            start_response('200 OK', [])
            return [b'']
        wrapped = proxy_trust.ProxyTrust(app, self.t.app)
        Client(wrapped).get('/', headers={'X-Forwarded-For': '198.51.100.4'},
                            environ_base={'REMOTE_ADDR': PROXY})
        self.assertEqual(seen, {'trusted': False, 'addr': PROXY})
        Client(wrapped).get('/', headers={'X-Forwarded-For': '198.51.100.4'},
                            environ_base={'REMOTE_ADDR': PROXY, public_listener.PUBLIC_ENVIRON_KEY: True})
        self.assertEqual(seen, {'trusted': True, 'addr': '198.51.100.4'})


class ProxyListTests(_TrustCase):
    """With the list set, forwarded headers are believed only from a peer on it, on either port."""
    POLICY = Policy(web=True, public=True, networks=(ipaddress.ip_network('192.168.1.10/32'),
                                                     ipaddress.ip_network('172.18.0.0/16')))

    def test_a_peer_off_the_list_is_not_believed_on_either_port(self):
        for client in (self.client, self.public):
            feed.login_failures.reset()
            with self.assertLogs('app.proxy_trust', 'WARNING') if client is self.client \
                    else mock.patch.object(proxy_trust.log, 'warning'):
                for n in range(feed.LOGIN_MAX_FAILURES):
                    self.ask(client, WRONG, peer='203.0.113.50', forwarded=f'198.51.100.{n}')
            self.assertEqual(self.locked(), ['203.0.113.50'])

    def test_a_listed_peer_is_believed_on_both_ports(self):
        for client, peer in ((self.client, PROXY), (self.public, '172.18.0.7')):
            feed.login_failures.reset()
            for _ in range(feed.LOGIN_MAX_FAILURES):
                self.ask(client, WRONG, peer=peer, forwarded='198.51.100.4')
            self.assertEqual(self.locked(), ['198.51.100.4'])

    def test_an_ignored_peer_is_logged_once_and_remembered(self):
        with self.assertLogs('app.proxy_trust', 'WARNING') as logs:
            for _ in range(3):
                self.ask(self.public, RIGHT, peer='203.0.113.50', forwarded='198.51.100.4')
        self.assertEqual(len(logs.output), 1)
        self.assertIn('203.0.113.50', logs.output[0])
        seen = proxy_trust.seen(PORT_PUBLIC)
        self.assertEqual((seen['ignored_recent'], seen['ignored_count']), ('203.0.113.50', 3))

    def test_one_stray_request_is_not_reported_for_ever(self):
        with mock.patch.object(proxy_trust.log, 'warning'):
            self.ask(self.public, RIGHT, peer='203.0.113.50', forwarded='198.51.100.4')
        with mock.patch.object(proxy_trust, 'IGNORED_REPORTED_FOR', timedelta(0)):
            self.assertEqual(proxy_trust.seen(PORT_PUBLIC)['ignored_recent'], '')

    def test_the_last_ignored_peer_is_named_past_the_log_cap(self):
        with mock.patch.object(proxy_trust, '_MAX_IGNORED_PEERS', 1), \
                mock.patch.object(proxy_trust.log, 'warning') as warned:
            self.ask(self.public, RIGHT, peer='203.0.113.50', forwarded='198.51.100.4')
            self.ask(self.public, RIGHT, peer='203.0.113.51', forwarded='198.51.100.4')
        self.assertEqual(warned.call_count, 1)
        self.assertEqual(proxy_trust.seen(PORT_PUBLIC)['ignored_recent'], '203.0.113.51')

    def test_the_pages_say_so_when_the_proxy_is_not_on_the_list(self):
        with mock.patch.object(proxy_trust.log, 'warning'):
            page = self.ask(self.client, '/iptv-output', peer='172.17.0.1',
                            forwarded='198.51.100.4').get_data(as_text=True)
        self.assertIn('through a reverse proxy that is not in "Reverse proxy addresses"', page)
        self.t.sandbox_config(feed_cfg(public_port=38517, public_url='https://tv.example.com'))
        with mock.patch.object(proxy_trust.log, 'warning'):
            self.ask(self.public, RIGHT, peer='172.17.0.1', forwarded='198.51.100.4')
        with mock.patch.object(public_listener, 'status', return_value=SERVING):
            page = self.ask(self.client, '/iptv-output', peer=PROXY,
                            forwarded='198.51.100.4').get_data(as_text=True)
        self.assertNotIn('through a reverse proxy that is not in', page)
        self.assertIn('only from what is in "Reverse proxy addresses"', page)
        self.assertIn('ignored a proxy\'s headers from 172.17.0.1', page.replace('&#39;', "'"))

    def test_a_mapped_ipv4_proxy_is_the_listed_ipv4(self):
        self.assertTrue(self.POLICY.believes(PORT_WEB, '::ffff:192.168.1.10'))
        self.assertFalse(self.POLICY.believes(PORT_WEB, '::ffff:192.168.1.11'))
        self.assertFalse(self.POLICY.believes(PORT_WEB, 'not-an-address'))


class PolicyFromConfigTests(unittest.TestCase):
    def test_each_port_has_its_own_switch(self):
        policy = proxy_trust.policy_from({'flask': {'behind_proxy': True, 'trusted_proxies': ['10.0.0.0/8']},
                                          'integrations': {'output_feed': {}}})
        self.assertEqual((policy.web, policy.public), (True, False))
        self.assertEqual(policy.networks, (ipaddress.ip_network('10.0.0.0/8'),))
        policy = proxy_trust.policy_from({'integrations': {'output_feed': {'public_behind_proxy': True}}})
        self.assertEqual((policy.web, policy.public, policy.networks), (False, True, ()))

    def test_a_list_with_nothing_readable_believes_nobody_not_everybody(self):
        with self.assertLogs('app.proxy_trust', 'WARNING'):
            policy = proxy_trust.policy_from({'flask': {'behind_proxy': True,
                                                        'trusted_proxies': ['my-proxy']}})
        self.assertFalse(policy.believes(PORT_WEB, '192.168.1.10'))
        self.assertFalse(policy.believes(PORT_WEB, '127.0.0.1'))

    def test_what_the_list_accepts(self):
        self.assertEqual(proxy_trust.proxies_problem(['192.168.1.10', '172.18.0.0/16', 'fd00::/8']), '')
        self.assertEqual(proxy_trust.proxies_problem([]), '')
        self.assertIn('my-proxy', proxy_trust.proxies_problem(['192.168.1.10', 'my-proxy']))
        self.assertTrue(proxy_trust.proxies_problem('192.168.1.10'))
        # Every address there is, and a range quietly wider than what was typed.
        for entry in ('0.0.0.0/0', '::/0', '192.168.1.10/24'):
            self.assertIn(entry, proxy_trust.proxies_problem([entry]))

    def test_a_hand_written_single_address_is_one_entry(self):
        policy = proxy_trust.policy_from({'flask': {'behind_proxy': True,
                                                    'trusted_proxies': '192.168.1.10'}})
        self.assertEqual(policy.networks, (ipaddress.ip_network('192.168.1.10/32'),))

    def test_a_list_of_every_address_believes_nobody(self):
        with self.assertLogs('app.proxy_trust', 'WARNING'):
            policy = proxy_trust.policy_from({'flask': {'behind_proxy': True,
                                                        'trusted_proxies': ['0.0.0.0/0']}})
        self.assertFalse(policy.believes(PORT_WEB, '192.168.1.10'))


class LockoutKeyTests(unittest.TestCase):
    def tracker(self):
        return auth.FailureTracker(10, timedelta(minutes=15))

    def test_ten_wrong_logins_spread_over_one_64_lock_it_out(self):
        tracker = self.tracker()
        for n in range(10):
            tracker.record_failure(f'2001:db8:1:2::{n + 1:x}')
        self.assertIsNotNone(tracker.check_lockout('2001:db8:1:2:ffff:ffff:ffff:ffff'))
        self.assertEqual([found.address for found in tracker.locked_out()], ['2001:db8:1:2::/64'])
        self.assertIsNone(tracker.check_lockout('2001:db8:1:3::1'))

    def test_a_mapped_ipv4_address_is_the_ipv4(self):
        tracker = self.tracker()
        for n in range(10):
            tracker.record_failure('::ffff:203.0.113.9' if n % 2 else '203.0.113.9')
        self.assertEqual([found.address for found in tracker.locked_out()], ['203.0.113.9'])
        self.assertIsNotNone(tracker.check_lockout('::ffff:203.0.113.9'))
        self.assertIsNone(tracker.check_lockout('203.0.113.10'))

    def test_a_right_login_clears_the_key_it_was_counted_under(self):
        tracker = self.tracker()
        tracker.record_failure('2001:db8:1:2::1')
        tracker.record_success('2001:db8:1:2::99')
        self.assertEqual(tracker.failures, {})

    def test_text_that_is_no_address_is_its_own_key(self):
        self.assertEqual(auth.lockout_key('unknown'), 'unknown')
        self.assertEqual(auth.lockout_key('fe80::1%eth0'), 'fe80::/64')

    def test_the_web_login_counts_the_same_way(self):
        self.addCleanup(auth.web_login.reset)
        for n in range(auth.MAX_FAILURES):
            auth.record_failure(f'2001:db8:9:9::{n + 1:x}')
        self.assertIsNotNone(auth.check_lockout('2001:db8:9:9::abcd'))


class AlarmTests(_TrustCase):
    """Wrong logins from many addresses at once are an alarm, and refuse nobody."""
    POLICY = Policy(public=True)

    def flood(self, n):
        for i in range(n):
            self.ask(self.public, WRONG, peer=PROXY, forwarded=f'198.51.{i // 250}.{i % 250 + 1}')

    def test_under_the_ceiling_there_is_no_alarm(self):
        self.flood(feed.LOGIN_ALARM_PER_MINUTE - 1)
        self.assertFalse(feed.login_failures.alarm()['active'])
        self.assertNotIn('id="of-login-alarm"', self.client.get('/iptv-output').get_data(as_text=True))

    def test_the_alarm_is_logged_once_and_shown_and_a_right_login_still_works(self):
        with self.assertLogs('app.routes.output_feed', 'WARNING') as logs:
            self.flood(feed.LOGIN_ALARM_PER_MINUTE + 10)
        guessing = [line for line in logs.output if 'guessing' in line]
        self.assertEqual(len(guessing), 1)
        alarm = feed.login_failures.alarm()
        self.assertTrue(alarm['active'])
        self.assertEqual(alarm['addresses'], feed.LOGIN_ALARM_PER_MINUTE + 10)
        self.assertEqual(self.locked(), [])
        # Never a refusal: a right login from a new address and from a guessing one both work.
        self.assertEqual(self.ask(self.public, RIGHT, peer=PROXY, forwarded='192.0.2.77').status_code, 200)
        self.assertEqual(self.ask(self.public, RIGHT, peer=PROXY, forwarded='198.51.0.1').status_code, 200)
        with mock.patch.object(public_listener, 'status', return_value=SERVING):
            page = self.client.get('/iptv-output').get_data(as_text=True)
        self.assertIn('id="of-login-alarm"', page)
        self.assertIn('Someone is guessing logins', page)

    def test_a_second_alarm_is_logged_again(self):
        tracker = auth.FailureTracker(10, timedelta(minutes=15), alarm_max=3,
                                      alarm_window=timedelta(seconds=0.05))
        for n in range(3):
            tracker.record_failure(f'198.51.100.{n}')
        self.assertIsNotNone(tracker.take_alarm_notice())
        self.assertIsNone(tracker.take_alarm_notice())
        import time
        time.sleep(0.08)
        for n in range(3):
            tracker.record_failure(f'198.51.101.{n}')
        self.assertIsNotNone(tracker.take_alarm_notice())

    def test_the_web_login_has_no_alarm(self):
        self.assertEqual(auth.web_login.alarm_max, 0)

    def test_letting_one_address_back_in_leaves_the_alarm(self):
        self.flood(feed.LOGIN_ALARM_PER_MINUTE)
        feed.login_failures.clear('198.51.0.1')
        self.assertTrue(feed.login_failures.alarm()['active'])


class MigrationTests(unittest.TestCase):
    """Config migration 10: a proxy that was believed on the public port stays believed."""

    def run_m010(self, cfg):
        return config_mod._cfg_m010_public_behind_proxy(cfg)

    def test_behind_proxy_with_a_public_port_turns_the_public_switch_on(self):
        cfg = self.run_m010({'flask': {'behind_proxy': True},
                             'integrations': {'output_feed': {'public_port': 38517}}})
        self.assertIs(cfg['integrations']['output_feed']['public_behind_proxy'], True)
        self.assertIs(cfg['flask']['behind_proxy'], True)

    def test_nothing_is_turned_on_that_was_not_believed_before(self):
        for cfg in ({'flask': {'behind_proxy': False},
                     'integrations': {'output_feed': {'public_port': 38517}}},
                    {'flask': {'behind_proxy': True}, 'integrations': {'output_feed': {'public_port': 0}}},
                    {'flask': {'behind_proxy': True}},
                    {}):
            out = self.run_m010(cfg)
            self.assertNotIn('public_behind_proxy',
                             (out.get('integrations') or {}).get('output_feed') or {})

    def test_a_value_already_written_wins(self):
        cfg = self.run_m010({'flask': {'behind_proxy': True}, 'integrations': {
            'output_feed': {'public_port': 38517, 'public_behind_proxy': False}}})
        self.assertIs(cfg['integrations']['output_feed']['public_behind_proxy'], False)

    def test_a_version_9_file_is_migrated_on_startup(self):
        fd, path = tempfile.mkstemp(suffix='.yaml')
        os.close(fd)
        self.addCleanup(os.unlink, path)
        orig = config_mod._CONFIG_PATH
        config_mod._CONFIG_PATH = path
        config_mod._yaml_cache = None

        def restore():
            config_mod._CONFIG_PATH = orig
            config_mod._yaml_cache = None
        self.addCleanup(restore)
        with open(path, 'w') as f:
            f.write('config_version: 9\nflask:\n  behind_proxy: true  # my proxy\n'
                    'integrations:\n  output_feed:\n    public_port: 38517\n')
        config_mod._yaml_cache = None
        with tempfile.TemporaryDirectory() as backups:
            config_mod.migrate_config(config_overrides={'config_backup': {'backup_dir': backups}})
        cfg = config_mod.load_config()
        self.assertIs(cfg['integrations']['output_feed']['public_behind_proxy'], True)
        self.assertEqual(cfg['config_version'], config_mod.CURRENT_CONFIG_VERSION)
        self.assertEqual((proxy_trust.policy_from(cfg).web, proxy_trust.policy_from(cfg).public),
                         (True, True))

    def test_it_is_the_newest_migration_and_the_keys_need_a_restart(self):
        self.assertEqual(config_mod.CONFIG_MIGRATIONS[-1][2], config_mod._cfg_m010_public_behind_proxy)
        for key in ('flask.behind_proxy', 'flask.trusted_proxies',
                    'integrations.output_feed.public_behind_proxy'):
            self.assertIn(key, config_mod.RESTART_REQUIRED_KEYS)


class ReadinessTests(_TrustCase):
    def check(self, policy, listener=SERVING):
        self.policy(policy)
        self.t.sandbox_config(feed_cfg(public_port=38517, public_url='https://tv.example.com'))
        with mock.patch.object(public_listener, 'status', return_value=listener):
            return readiness._check_proxy_trust(readiness.Context())

    def test_no_port_believing_a_proxy_is_nothing_to_report(self):
        self.assertEqual(self.check(Policy()).status, readiness.NOTHING)
        # The public switch with the port not open is no port at all.
        off = {'state': 'off', 'host': '', 'port': 0, 'error': ''}
        self.assertEqual(self.check(Policy(public=True), off).status, readiness.NOTHING)

    def test_an_empty_list_says_what_it_lets_a_caller_do(self):
        r = self.check(Policy(public=True))
        self.assertEqual(r.status, readiness.ATTENTION)
        self.assertIn('"Reverse proxy addresses" is empty', r.found)
        self.assertIn('made-up address with every wrong password', r.found)
        self.assertIn('go on guessing passwords', r.found)
        self.assertNotIn('would be believed', r.found)

    def test_the_public_port_reached_directly_is_a_problem(self):
        self.policy(Policy(public=True))
        self.ask(self.public, RIGHT, peer='203.0.113.50')
        r = self.check(Policy(public=True))
        self.assertEqual(r.status, readiness.PROBLEM)
        self.assertIn('reached straight from 203.0.113.50', r.found)
        self.assertIn('go on guessing passwords', r.found)

    def test_the_web_port_reached_directly_is_named_with_both_ways_out(self):
        self.policy(Policy(web=True))
        self.ask(self.client, RIGHT, peer='192.168.1.77')
        r = self.check(Policy(web=True))
        self.assertEqual(r.status, readiness.ATTENTION)
        self.assertIn('192.168.1.77', r.found)
        self.assertIn('turn "The web UI is behind a reverse proxy" off', r.found)

    def test_this_machine_reaching_the_port_is_not_evidence(self):
        self.policy(Policy(web=True))
        self.ask(self.client, RIGHT, peer='127.0.0.1')
        self.assertEqual(proxy_trust.seen(PORT_WEB)['direct_peer'], '')

    def test_a_proxy_left_off_the_list_is_named(self):
        listed = Policy(public=True, networks=(ipaddress.ip_network('192.168.1.10/32'),))
        self.policy(listed)
        with self.assertLogs('app.proxy_trust', 'WARNING'):
            self.ask(self.public, RIGHT, peer='172.17.0.1', forwarded='198.51.100.4')
        r = self.check(listed)
        self.assertEqual(r.status, readiness.ATTENTION)
        self.assertIn('172.17.0.1 is not in "Reverse proxy addresses"', r.found)
        self.assertIn('lock all of them out', r.found)

    def test_an_empty_list_never_calls_a_caller_your_proxy(self):
        self.policy(Policy(public=True))
        self.ask(self.public, RIGHT, peer='203.0.113.50', forwarded='198.51.100.4')
        r = self.check(Policy(public=True))
        self.assertIn('203.0.113.50', r.detail)
        self.assertNotIn('Your proxy last connected', r.detail)
        self.assertIn('only if you know', r.detail)

    def test_a_listed_proxy_is_ready(self):
        r = self.check(Policy(public=True, networks=(ipaddress.ip_network('192.168.1.10/32'),)))
        self.assertEqual(r.status, readiness.READY)

    def test_the_support_bundle_does_not_spell_an_outside_address(self):
        self.policy(Policy(public=True))
        self.ask(self.public, RIGHT, peer='93.184.216.34')
        with mock.patch.object(public_listener, 'status', return_value=SERVING):
            r = readiness._check_proxy_trust(readiness.Context(pseudonymize=True))
        self.assertNotIn('93.184.216.34', r.found)
        self.assertIn('an address outside your network', r.found)


class SettingsTests(_TrustCase):
    def save(self, path, value):
        return self.client.post('/api/settings/field', json={'path': path, 'value': value})

    def test_the_list_is_saved_trimmed_and_refuses_what_is_no_address(self):
        self.assertEqual(self.save('flask.trusted_proxies', [' 192.168.1.10 ', '', '172.18.0.0/16']).status_code, 200)
        self.assertEqual(config_mod.load_config()['flask']['trusted_proxies'],
                         ['192.168.1.10', '172.18.0.0/16'])
        resp = self.save('flask.trusted_proxies', ['my-proxy'])
        self.assertEqual(resp.status_code, 400)
        self.assertIn('my-proxy', resp.get_json()['error'])
        self.assertEqual(config_mod.load_config()['flask']['trusted_proxies'],
                         ['192.168.1.10', '172.18.0.0/16'])

    def test_the_public_switch_is_a_bool(self):
        path = 'integrations.output_feed.public_behind_proxy'
        self.assertEqual(self.save(path, 'yes').status_code, 400)
        self.assertEqual(self.save(path, True).status_code, 200)
        self.assertIs(config_mod.load_config()['integrations']['output_feed']['public_behind_proxy'], True)

    def test_the_settings_page_has_all_three_and_the_test_names_the_proxy(self):
        page = self.client.get('/settings').get_data(as_text=True)
        for path in ('flask.behind_proxy', 'flask.trusted_proxies',
                     'integrations.output_feed.public_behind_proxy'):
            self.assertIn(f'data-setting-path="{path}"', page)
        self.policy(Policy(web=True, networks=(ipaddress.ip_network('192.168.1.10/32'),)))
        with mock.patch.object(proxy_trust.log, 'warning'):
            facts = self.ask(self.client, '/api/settings/proxy-check', peer='172.17.0.1',
                             forwarded='198.51.100.4').get_json()
        self.assertEqual((facts['peer'], facts['list_set'], facts['listed']), ('172.17.0.1', True, False))

    def test_the_page_says_when_the_public_port_has_not_been_told(self):
        self.policy(Policy(web=True))
        self.t.sandbox_config(feed_cfg(public_port=38517, public_url='https://tv.example.com'))
        with mock.patch.object(public_listener, 'status', return_value=SERVING):
            page = self.client.get('/iptv-output').get_data(as_text=True)
        self.assertIn('The public port has not been told it is behind a reverse proxy', page)
        self.policy(Policy(public=True))
        with mock.patch.object(public_listener, 'status', return_value=SERVING):
            page = self.client.get('/iptv-output').get_data(as_text=True)
        self.assertNotIn('has not been told it is behind', page)
        self.assertIn('believes any caller that says it is a reverse proxy', page)


if __name__ == '__main__':
    unittest.main()
