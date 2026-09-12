"""Tier 0 - the nav's alert counter, rail pip, banner and details view, driven in a real DOM
(static/js/nav-alerts.js). Built in dev/changelog/924, rules from dev/changelog/923.

Python cannot reach any of this: the server renders both counts, the pip and the banner
hidden, which is also exactly what the page looks like if the script threw on line one.
tests/support/nav_alerts.mjs loads a page the real app rendered, evaluates the shipped
util.js and nav-alerts.js plus base.html's rail block into it, feeds it /api/nav-status
payloads, and reports. Every assertion lives here. The server's half - which alert the
banner gets and how the counts split - is tests/test_alert_banner.py.

What it cannot cover: jsdom computes no layout, so the title's ellipsis and the banner's
375px wrap are browser work.

  python3 -m unittest tests.test_nav_alerts_js
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tests.support.app import make_test_app  # noqa: E402

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HARNESS = os.path.join(REPO, 'tests', 'support', 'nav_alerts.mjs')
JSDOM = os.path.join(REPO, 'node_modules', 'jsdom')

_RESULT = None


def _observe():
    """Render one real page, drive the script over it once in node, and return every
    scenario's observations."""
    global _RESULT
    if _RESULT is not None:
        return _RESULT
    t = make_test_app()
    try:
        html = t.app.test_client().get('/').get_data(as_text=True)
    finally:
        t.cleanup()
    with tempfile.TemporaryDirectory() as tmp:
        page = os.path.join(tmp, 'page.html')
        with open(page, 'w', encoding='utf-8') as fh:
            fh.write(html)
        proc = subprocess.run([shutil.which('node'), HARNESS, REPO, page],
                              capture_output=True, text=True, cwd=REPO, timeout=120)
    if proc.returncode != 0:
        raise AssertionError(f'harness failed:\n{proc.stdout}\n{proc.stderr}')
    _RESULT = json.loads(proc.stdout)
    return _RESULT


@unittest.skipIf(shutil.which('node') is None, 'node not installed')
@unittest.skipIf(not os.path.isdir(JSDOM), 'jsdom not installed (npm install)')
class _Base(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.obs = _observe()


class BootTests(_Base):
    def test_no_script_error(self):
        self.assertEqual(self.obs['errors'], [])


class CounterTests(_Base):
    def test_red_and_yellow_counts_show_in_both_nav_copies(self):
        nav = self.obs['full']['nav']
        self.assertEqual(len(nav['bad']), 2, 'sidebar and phone drawer each carry the pair')
        self.assertEqual(nav['bad'], [{'text': '2', 'shown': True}] * 2)
        self.assertEqual(nav['warn'], [{'text': '1', 'shown': True}] * 2)

    def test_a_zero_count_hides(self):
        nav = self.obs['warnOnly']['nav']
        self.assertTrue(all(not c['shown'] for c in nav['bad']))
        self.assertTrue(all(c['shown'] for c in nav['warn']))

    def test_info_only_shows_no_count_and_no_pip(self):
        """`count` was 4 here, all INFO: nothing in the nav may claim them."""
        nav = self.obs['none']['nav']
        self.assertTrue(all(not c['shown'] for c in nav['bad'] + nav['warn']))
        self.assertTrue(all(not p['shown'] for p in nav['pips']))

    def test_the_rail_pip_takes_the_worst_color(self):
        # Both nav copies render the pip; only the sidebar's is ever visible (on the rail).
        self.assertEqual(self.obs['full']['nav']['pips'], [{'shown': True, 'bad': True}] * 2)
        self.assertEqual(self.obs['warnOnly']['nav']['pips'], [{'shown': True, 'bad': False}] * 2)

    def test_rail_tip_names_which_number_is_which(self):
        self.assertEqual(self.obs['full']['tip'], 'Alerts (2 errors, 1 warning)')
        self.assertEqual(self.obs['warnOnly']['tip'], 'Alerts (1 warning)')
        self.assertEqual(self.obs['oneError']['tip'], 'Alerts (1 error)')
        self.assertEqual(self.obs['none']['tip'], 'Alerts')

    def test_a_counts_only_update_moves_the_counts_and_leaves_the_banner(self):
        """The Alerts page's refresh sends no banner key; it must not blank one."""
        o = self.obs['countsOnly']
        self.assertTrue(all(not c['shown'] for c in o['nav']['bad']))
        self.assertTrue(all(c['text'] == '1' and c['shown'] for c in o['nav']['warn']))
        self.assertTrue(o['bannerShown'])


class BannerTests(_Base):
    def test_banner_shows_severity_title_and_more(self):
        b = self.obs['full']['banner']
        self.assertTrue(b['shown'])
        self.assertEqual(b['cls'], 'alert-banner alert-banner-sev-ERROR')
        self.assertEqual((b['sev'], b['sevCls']), ('ERROR', 'alert-severity sev-ERROR'))
        self.assertEqual(b['title'], 'Move failed: /dvr-complete is not reachable')
        self.assertTrue(b['titleInFirstSpan'])
        self.assertEqual(b['more'], '+2 more')
        self.assertTrue(b['moreShown'])
        self.assertIn('2 more unread.', b['moreTip'])

    def test_more_hides_when_nothing_is_behind_it(self):
        self.assertFalse(self.obs['warnOnly']['banner']['moreShown'])

    def test_no_banner_payload_hides_it(self):
        self.assertFalse(self.obs['none']['banner']['shown'])

    def test_x_marks_read_in_one_click(self):
        x = self.obs['x']
        self.assertTrue(x['hiddenAtOnce'])
        self.assertEqual(x['modals'], 0, 'no confirm or details step in between')
        self.assertIn({'url': '/api/alerts/9/read', 'method': 'POST'}, x['calls'])
        self.assertEqual(x['refreshes'], 1, 'the refresh brings up the next alert')

    def test_a_failed_mark_read_says_so(self):
        self.assertTrue(self.obs['xFailed']['toast'])
        self.assertEqual(self.obs['xFailed']['refreshes'], 1)


class DetailsTests(_Base):
    def test_title_opens_details_without_following_the_href(self):
        d = self.obs['details']
        self.assertTrue(d['prevented'])
        self.assertEqual(d['count'], 1)
        self.assertEqual(d['modal']['title'], 'Move failed: /dvr-complete is not reachable')
        self.assertEqual(d['modal']['body'], 'Line one\nLine two')
        self.assertIn('ERROR', d['modal']['meta'])
        self.assertIn('Sep 11, 2026 01:07 AM EDT', d['modal']['meta'])

    def test_two_buttons_with_the_destination_primary(self):
        m = self.obs['details']['modal']
        self.assertEqual(m['buttons'], [{'label': 'Mark read', 'cls': 'btn'},
                                        {'label': 'Account →', 'cls': 'btn btn-primary'}])

    def test_ignore_sits_behind_the_footer_more_menu(self):
        m = self.obs['details']['modal']
        self.assertTrue(m['firstChildIsMore'])
        self.assertEqual(m['menuItems'], ['Ignore future alerts like this'])

    def test_without_a_destination_mark_read_is_the_primary(self):
        self.assertEqual(self.obs['detailsNoLink']['buttons'],
                         [{'label': 'Mark read', 'cls': 'btn btn-primary'}])
        self.assertEqual(self.obs['detailsNoLink']['body'], 'EPG fetch failed',
                         'an empty body falls back to the title')

    def test_mark_read_in_details_closes_it_and_marks_read(self):
        o = self.obs['detailsMarkRead']
        self.assertEqual(o['modalsLeft'], 0)
        self.assertTrue(o['hiddenAtOnce'])
        self.assertIn({'url': '/api/alerts/9/read', 'method': 'POST'}, o['calls'])
        self.assertEqual(o['refreshes'], 1)

    def test_space_on_the_title_opens_details(self):
        self.assertEqual(self.obs['space']['count'], 1)

    def test_a_still_happening_alert_says_so_above_its_body(self):
        """dev/changelog/932: without this, Mark read looks like it disposed of the problem
        - the banner goes and the count drops while the condition is still live."""
        m = self.obs['detailsActiveError']
        self.assertIsNotNone(m['notice'])
        self.assertIn('Still active.', m['notice']['text'])
        self.assertIn('stays under Active alerts', m['notice']['text'])
        self.assertTrue(m['noticeBeforeBody'])
        self.assertEqual(m['body'], 'It answers with Stale file handle.')

    def test_the_notice_takes_the_severity_color(self):
        self.assertEqual(self.obs['detailsActiveError']['notice']['cls'], 'notice notice-bad')
        self.assertEqual(self.obs['detailsActiveWarn']['notice']['cls'], 'notice notice-warn')

    def test_a_one_time_alert_gets_no_notice(self):
        """The claim is about a condition that is still true; a conversion that failed last
        week is not one, and a notice on it would be false."""
        self.assertIsNone(self.obs['details']['modal']['notice'])
        self.assertIsNone(self.obs['detailsNoLink']['notice'])

    def test_ignore_replaces_details_with_the_existing_confirm(self):
        o = self.obs['ignore']
        self.assertTrue(o['menuOpen'])
        self.assertEqual(o['afterIgnoreClick'], ['Ignore future alerts like this'])
        self.assertIn({'url': '/api/alerts/9/ignore', 'method': 'POST'}, o['calls'])
        self.assertEqual(o['refreshes'], 1)
        self.assertFalse(o['bannerShown'])


if __name__ == '__main__':
    unittest.main()
