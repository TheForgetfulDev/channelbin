"""Tier 0 - the Settings page's reverse-proxy check in a real DOM.

static/js/settings.js asks /api/settings/proxy-check once as the browser sends it and, only
when that saw a proxy, once more with a planted X-Forwarded-For, then shows one line that
names its case: no proxy, came direct while on, passed through (unsafe), dropped, the proxy's
address does not match the browser's, or looks right - each followed by what the switch is
doing. tests/support/proxy_check.mjs runs the shipped util.js and settings.js against the
page the Flask app really answered, with the endpoint scripted per scenario; every assertion
lives here. dev/changelog/1648; no dev/docs/BUGS.md entry: a feature.

  python3 -m unittest tests.test_settings_proxy_check_js
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.output_feed_view import PROXY_PROBE_ADDRESS  # noqa: E402
from tests.support.app import make_test_app  # noqa: E402

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HARNESS = os.path.join(REPO, 'tests', 'support', 'proxy_check.mjs')
JSDOM = os.path.join(REPO, 'node_modules', 'jsdom')
HERE = 'http://localhost:5000'   # the harness's location.origin


def _plain(proxied=True, configured=False, wired=False, origin=HERE, chain=('192.168.1.50',)):
    return {'success': True, 'proxied': proxied, 'configured': configured, 'wired': wired,
            'planted': PROXY_PROBE_ADDRESS, 'forwarded_for': list(chain) if proxied else [],
            'would': {'client': chain[-1] if proxied else '127.0.0.1', 'origin': origin},
            'direct': {'client': '127.0.0.1', 'origin': HERE}, 'headers': {}, 'probe': ''}


def _probe(answer):
    return {**_plain(), 'probe': answer}


SCENARIOS = {
    'none': {'plain': _plain(proxied=False)},
    'direct_on': {'plain': _plain(proxied=False, configured=True, wired=True)},
    'right_off': {'plain': _plain(), 'probe': _probe('appended')},
    'right_restart': {'plain': _plain(configured=True), 'probe': _probe('replaced')},
    'right_on': {'plain': _plain(configured=True, wired=True), 'probe': _probe('appended')},
    'off_waits': {'plain': _plain(wired=True), 'probe': _probe('appended')},
    'passed': {'plain': _plain(), 'probe': _probe('passed')},
    'dropped': {'plain': _plain(chain=('',)), 'probe': _probe('dropped')},
    'mismatch': {'plain': _plain(origin='http://127.0.0.1:5000'), 'probe': _probe('appended')},
    'hops': {'plain': _plain(chain=('1.2.3.4', '192.168.1.50')), 'probe': _probe('appended')},
    'unknown_probe': {'plain': _plain(), 'probe': _probe('sideways')},
    'fail': {'fail': True, 'plain': _plain()},
}

_RESULT = None


def _observe():
    global _RESULT
    if _RESULT is None:
        t = make_test_app()
        tmp = tempfile.mkdtemp(prefix='proxy_check_js_')
        try:
            html = t.app.test_client().get('/settings').get_data(as_text=True)
            page = os.path.join(tmp, 'settings.html')
            with open(page, 'w', encoding='utf-8') as f:
                f.write(html)
            scen = os.path.join(tmp, 'scenarios.json')
            with open(scen, 'w', encoding='utf-8') as f:
                json.dump(SCENARIOS, f)
            proc = subprocess.run([shutil.which('node'), HARNESS, page, REPO, scen],
                                  capture_output=True, text=True, timeout=120, cwd=REPO)
            if proc.returncode != 0:
                raise AssertionError(f'harness failed:\n{proc.stdout[-2000:]}\n{proc.stderr[-4000:]}')
            _RESULT = json.loads(proc.stdout)
        finally:
            t.cleanup()
            shutil.rmtree(tmp, ignore_errors=True)
    return _RESULT


@unittest.skipIf(shutil.which('node') is None, 'node not installed')
@unittest.skipIf(not os.path.isdir(JSDOM), 'jsdom not installed (npm install)')
class ProxyCheckResultTests(unittest.TestCase):
    def obs(self, name):
        self.assertTrue(_observe()['_firstHidden'])
        o = _observe()[name]
        self.assertEqual(o['errors'], [], name)
        self.assertFalse(o['hidden'], name)
        self.assertFalse(o['disabled'], name)
        return o

    def test_no_proxy_asks_once_and_says_so(self):
        o = self.obs('none')
        self.assertEqual(len(o['requests']), 1)
        self.assertEqual(o['tone'], 'notice-info')
        self.assertIn('No reverse proxy seen', o['text'])
        self.assertIn('The switch is off.', o['text'])

    def test_on_but_came_direct(self):
        o = self.obs('direct_on')
        self.assertEqual(len(o['requests']), 1)
        self.assertIn('came straight to ChannelBin, not through a proxy', o['text'])
        self.assertIn('The switch is on.', o['text'])

    def test_the_probe_plants_the_documentation_address(self):
        o = self.obs('right_off')
        self.assertEqual(len(o['requests']), 2)
        self.assertEqual(o['requests'][0]['query'], '')
        self.assertNotIn('X-Forwarded-For', o['requests'][0]['headers'])
        self.assertEqual(o['requests'][1]['query'], '?probe=1')
        self.assertEqual(o['requests'][1]['headers'].get('X-Forwarded-For'), PROXY_PROBE_ADDRESS)

    def test_right_and_off_says_safe_to_turn_on(self):
        o = self.obs('right_off')
        self.assertEqual(o['tone'], 'notice-ok')
        self.assertIn(f'would see you as 192.168.1.50 at {HERE}', o['text'])
        self.assertIn('adds your address to X-Forwarded-For', o['text'])
        self.assertIn('The switch is off, and it is safe to turn on.', o['text'])

    def test_each_switch_state_is_named(self):
        self.assertIn('writes X-Forwarded-For itself', self.obs('right_restart')['text'])
        self.assertIn('The switch is on and waits for a restart.', self.obs('right_restart')['text'])
        self.assertIn('The switch is on.', self.obs('right_on')['text'])
        self.assertIn('The switch is off and waits for a restart.', self.obs('off_waits')['text'])

    def test_pass_through_is_unsafe(self):
        o = self.obs('passed')
        self.assertEqual(o['tone'], 'notice-bad')
        self.assertIn('Not safe yet', o['text'])
        self.assertIn('$proxy_add_x_forwarded_for', o['text'])
        self.assertNotIn('safe to turn on', o['text'])

    def test_a_proxy_that_sends_no_forwarded_for_is_not_right(self):
        o = self.obs('dropped')
        self.assertEqual(o['tone'], 'notice-warn')
        self.assertIn('Your proxy sends no X-Forwarded-For', o['text'])
        self.assertNotIn('safe to turn on', o['text'])

    def test_a_proxy_address_that_is_not_the_browsers_is_named(self):
        o = self.obs('mismatch')
        self.assertEqual(o['tone'], 'notice-warn')
        self.assertIn(f'would think this page is at http://127.0.0.1:5000, but your browser is at {HERE}', o['text'])

    def test_more_than_one_address_before_the_last_is_said(self):
        o = self.obs('hops')
        self.assertIn('X-Forwarded-For carries 2 addresses (1.2.3.4, 192.168.1.50)', o['text'])

    def test_an_answer_it_does_not_know_is_an_error_not_a_verdict(self):
        o = self.obs('unknown_probe')
        self.assertEqual(o['tone'], 'notice-warn')
        self.assertIn('The test could not run: Unknown proxy probe answer "sideways"', o['text'])

    def test_a_failed_read_says_the_check_could_not_run(self):
        o = self.obs('fail')
        self.assertIn('The test could not run: boom', o['text'])


if __name__ == '__main__':
    unittest.main()
