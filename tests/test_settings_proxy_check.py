"""The Settings page's ChannelBin's address and reverse-proxy rows, and the proxy check.

`flask.behind_proxy` is a Settings switch, saved as true or false and nothing else; every
other flask.* key but `flask.public_url` is refused by the field route, so only config.yaml
can change where ChannelBin listens. /api/settings/proxy-check reports what a request
arrived with from a proxy and what turning the switch on would make of it, reading the
request as it arrived even when ProxyFix is already wired, and echoes only the proxy
headers. dev/changelog/1648; no dev/docs/BUGS.md entry: a feature.

  python3 -m unittest tests.test_settings_proxy_check
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import output_feed_view as view  # noqa: E402
from app.config import load_config  # noqa: E402
from tests.support.app import make_test_app  # noqa: E402

PLANTED = view.PROXY_PROBE_ADDRESS


class _Case(unittest.TestCase):
    OVERRIDES = None

    def setUp(self):
        self.t = make_test_app(extra_overrides=self.OVERRIDES, csrf=False)
        self.addCleanup(self.t.cleanup)
        self.client = self.t.app.test_client()

    def check(self, headers=None, probe=False):
        resp = self.client.get('/api/settings/proxy-check' + ('?probe=1' if probe else ''),
                               headers=headers or {})
        self.assertEqual(resp.status_code, 200)
        return resp.get_json()


class ProxyCheckTests(_Case):
    APPENDING = {'X-Forwarded-For': f'{PLANTED}, 192.168.1.50', 'X-Forwarded-Proto': 'https',
                 'X-Forwarded-Host': 'channelbin.example.com'}

    def test_no_proxy_headers_is_not_proxied(self):
        d = self.check()
        self.assertFalse(d['proxied'])
        self.assertEqual(d['headers'], {})
        self.assertEqual(d['probe'], '')
        self.assertEqual(d['would'], d['direct'])
        self.assertFalse(d['configured'])
        self.assertFalse(d['wired'])
        self.assertEqual(d['planted'], PLANTED)

    def test_what_the_switch_would_make_of_the_headers(self):
        d = self.check({'X-Forwarded-For': '192.168.1.50', 'X-Forwarded-Proto': 'https',
                        'X-Forwarded-Host': 'channelbin.example.com'})
        self.assertTrue(d['proxied'])
        self.assertEqual(d['would'], {'client': '192.168.1.50',
                                      'origin': 'https://channelbin.example.com'})
        self.assertEqual(d['direct'], {'client': '127.0.0.1', 'origin': 'http://localhost'})
        self.assertEqual(d['forwarded_for'], ['192.168.1.50'])

    def test_one_hop_believes_only_the_last_address(self):
        d = self.check({'X-Forwarded-For': '1.2.3.4, 10.0.0.9, 192.168.1.50'})
        self.assertEqual(d['forwarded_for'], ['1.2.3.4', '10.0.0.9', '192.168.1.50'])
        self.assertEqual(d['would']['client'], '192.168.1.50')

    def test_the_probe_names_what_the_proxy_did_with_the_planted_address(self):
        cases = {
            f'{PLANTED}, 192.168.1.50': view.PROBE_APPENDED,
            '192.168.1.50': view.PROBE_REPLACED,
            PLANTED: view.PROBE_PASSED,
            f'192.168.1.50, {PLANTED}': view.PROBE_PASSED,
        }
        # A proxy that strips the header and sends none leaves every visitor looking like
        # itself, so it is its own answer, never "replaced".
        self.assertEqual(self.check({'X-Forwarded-Proto': 'https'}, probe=True)['probe'],
                         view.PROBE_DROPPED)
        for xff, expected in cases.items():
            self.assertEqual(self.check({'X-Forwarded-For': xff}, probe=True)['probe'], expected, xff)

    def test_without_probe_the_planted_address_is_not_judged(self):
        self.assertEqual(self.check({'X-Forwarded-For': PLANTED})['probe'], '')

    def test_only_the_proxy_headers_are_echoed(self):
        self.client.set_cookie('session', 'chocolate-chip')
        resp = self.client.get('/api/settings/proxy-check',
                               headers={**self.APPENDING, 'User-Agent': 'TellNoOne/1.0',
                                        'Authorization': 'Bearer hush'})
        body = resp.get_data(as_text=True)
        for leaked in ('chocolate-chip', 'TellNoOne', 'hush'):
            self.assertNotIn(leaked, body)
        self.assertEqual(set(resp.get_json()['headers']),
                         {'X-Forwarded-For', 'X-Forwarded-Proto', 'X-Forwarded-Host'})

    def test_configured_follows_the_file_not_the_process(self):
        self.t.sandbox_config({'flask': {'behind_proxy': True}})
        d = self.check()
        self.assertTrue(d['configured'])
        self.assertFalse(d['wired'])

    def test_it_writes_nothing(self):
        before = load_config()
        self.check(self.APPENDING, probe=True)
        self.assertEqual(load_config(), before)
        for method in ('post', 'put', 'delete'):
            self.assertEqual(getattr(self.client, method)('/api/settings/proxy-check').status_code, 405)


class ProxyCheckWhileWiredTests(_Case):
    """With the switch already on, ProxyFix has rewritten the request before the route sees
    it; the check still reports the request as it arrived."""
    OVERRIDES = {'flask': {'behind_proxy': True}}

    def test_the_app_is_wired(self):
        from app import proxy_trust
        self.assertTrue(proxy_trust.wired(self.t.app).web)

    def test_direct_is_the_real_peer_and_would_is_what_proxyfix_made(self):
        d = self.check({'X-Forwarded-For': '192.168.1.50', 'X-Forwarded-Proto': 'https',
                        'X-Forwarded-Host': 'channelbin.example.com'})
        self.assertTrue(d['wired'])
        self.assertEqual(d['direct'], {'client': '127.0.0.1', 'origin': 'http://localhost'})
        self.assertEqual(d['would'], {'client': '192.168.1.50',
                                      'origin': 'https://channelbin.example.com'})

    def test_a_direct_visit_is_unchanged(self):
        d = self.check()
        self.assertFalse(d['proxied'])
        self.assertEqual(d['would'], d['direct'])


class FieldRouteTests(_Case):
    """dev/docs/BUGS.md 2026-11-19 @ 04:27:38 PM: the field route saved every read-only
    flask.* key the page shows as config.yaml only."""

    def save(self, path, value):
        return self.client.post('/api/settings/field', json={'path': path, 'value': value})

    def test_behind_proxy_saves_as_a_bool_and_asks_for_a_restart(self):
        resp = self.save('flask.behind_proxy', True)
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(resp.get_json()['restart_required'])
        self.assertIs(load_config()['flask']['behind_proxy'], True)
        self.assertEqual(self.save('flask.behind_proxy', False).status_code, 200)
        self.assertIs(load_config()['flask']['behind_proxy'], False)

    def test_behind_proxy_refuses_anything_but_a_bool(self):
        for value in ('true', 1, None, 'yes'):
            self.assertEqual(self.save('flask.behind_proxy', value).status_code, 400, repr(value))
        self.assertIs(load_config()['flask']['behind_proxy'], False)

    def test_every_other_flask_key_is_refused(self):
        before = load_config()['flask']
        for path, value in (('flask.port', 1), ('flask.host', '127.0.0.1'), ('flask.debug', True),
                            ('flask.secret_key', 'x'), ('flask.serve_mockups', True),
                            ('flask.pidfile_path', '/tmp/x.pid'), ('flask.new_thing', 1)):
            resp = self.save(path, value)
            self.assertEqual(resp.status_code, 400, path)
            self.assertIn('config.yaml', resp.get_json()['error'])
        # The whole block at once is the same write by another name.
        for value in ({'port': 1, 'host': 'x'}, None):
            resp = self.save('flask', value)
            self.assertEqual(resp.status_code, 400, repr(value))
            self.assertIn('config.yaml', resp.get_json()['error'])
        self.assertEqual(load_config()['flask'], before)

    def test_the_address_is_still_saved(self):
        self.assertEqual(self.save('flask.public_url', 'https://cb.example.com').status_code, 200)
        self.assertEqual(load_config()['flask']['public_url'], 'https://cb.example.com')


class SystemCardTests(_Case):
    def setUp(self):
        super().setUp()
        self.html = self.client.get('/settings').get_data(as_text=True)

    def row(self, path):
        start = self.html.index(f'data-path="{path}"')
        return self.html[start:self.html.index('<div class="frow', start + 1)]

    def test_the_address_comes_first_with_its_examples_and_a_placeholder(self):
        system = self.html[self.html.index('data-sec="system"'):]
        self.assertLess(system.index('data-path="flask.public_url"'), system.index('data-path="flask.port"'))
        self.assertLess(system.index('data-path="flask.public_url"'), system.index('data-path="flask.host"'))
        row = self.row('flask.public_url')
        self.assertIn('placeholder="https://channelbin.example.com"', row)
        self.assertIn('<div class="fl-eg">For example: <code>https://channelbin.example.com</code> or '
                      '<code>http://192.168.1.1:5000</code></div>', row)
        self.assertIn('never where ChannelBin listens', row)
        self.assertIn('cannot cut you off from this page', row)

    def test_behind_proxy_is_a_switch_with_a_restart_badge_and_the_check(self):
        row = self.row('flask.behind_proxy')
        self.assertIn('<input type="checkbox" data-setting-path="flask.behind_proxy"', row)
        self.assertIn('restart required', row)
        self.assertNotIn('read-only', row)
        self.assertIn('Turning it on cannot lock you out', row)
        self.assertIn('id="proxy-check-btn"', self.html)
        self.assertIn('id="proxy-check-result"', self.html)

    def test_host_and_port_say_why_they_are_config_yaml_only_and_how_to_fix_them(self):
        for path in ('flask.host', 'flask.port'):
            row = self.row(path)
            self.assertIn('read-only - edit in config.yaml', row, path)
            self.assertIn('could not be used to', row, path)
            self.assertIn('/config under Docker', row, path)
        self.assertIn('This is not ChannelBin&#39;s address', self.row('flask.host'))


if __name__ == '__main__':
    unittest.main()
