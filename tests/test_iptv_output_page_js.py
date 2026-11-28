"""The IPTV Output page's script (static/js/iptv-output.js), driven in jsdom over the page as
the app rendered it (tests/support/iptv_output_page.mjs), per mockup 53, dev/changelog/1526.
Throwaway temp SQLite DB - never the live dvr.db. No dev/docs/BUGS.md entry: a feature.
  python3 -m unittest tests.test_iptv_output_page_js
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tests.support.output_feed import FeedCase  # noqa: E402

import app.output_feed as feed  # noqa: E402
from app import db  # noqa: E402
from app.database import OutputFeedLogin  # noqa: E402

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HARNESS = os.path.join(REPO, 'tests', 'support', 'iptv_output_page.mjs')
JSDOM = os.path.join(REPO, 'node_modules', 'jsdom')
_OBSERVED = {}
# The stored password. The harness answers the reveal with one of its own, so a page that
# carried this one would show it without the script ever asking.
STORED = 'sofa-remote-42'
LAN = 'http://192.168.1.40:5000'


@unittest.skipIf(shutil.which('node') is None or not os.path.isdir(JSDOM), 'node or jsdom not installed')
class IptvOutputScriptTests(FeedCase):
    PASSWORD = STORED

    def setUp(self):
        # One render and one harness run for the whole class; the rig builds its app per test.
        if _OBSERVED:
            self.obs = _OBSERVED
            return
        super().setUp()
        self.alpha_feed = feed.feed_id_for_channel(self.alpha.id)
        self.session(self.alpha_feed)
        # A login with a channel list of its own, for the Choose channels dialog.
        db.session.add(OutputFeedLogin(name='Kim', username='kim', password='couch42',
                                       lineup=json.dumps([self.alpha_feed])))
        db.session.commit()
        # Opened on a LAN address, so ChannelBin's address being unset is the hideable notice
        # rather than the localhost warning.
        html = self.client.get('/iptv-output', base_url=LAN).get_data(as_text=True)
        with tempfile.TemporaryDirectory() as tmp:
            with open(os.path.join(tmp, 'page.html'), 'w', encoding='utf-8') as fh:
                fh.write(html)
            proc = subprocess.run([shutil.which('node'), HARNESS, tmp, REPO], capture_output=True,
                                  text=True, cwd=REPO, timeout=120)
        if proc.returncode != 0:
            raise AssertionError(f'harness failed:\n{proc.stdout}\n{proc.stderr[-4000:]}')
        _OBSERVED.update(json.loads(proc.stdout), alpha_feed=self.alpha_feed,
                         login_id=OutputFeedLogin.query.filter_by(username='kim').one().id,
                         login_ids={r.username: r.id for r in OutputFeedLogin.query},
                         beta_feed=feed.feed_id_for_channel(self.beta.id),
                         account_id=self.account.id, stored_in_page=STORED in html)
        self.obs = _OBSERVED

    def test_no_script_errors(self):
        self.assertEqual(self.obs['errors'], [])

    def test_search_and_chips_filter_the_rendered_rows(self):
        self.assertEqual(self.obs['all_rows'], ['Alpha', 'Game feeds', 'Beta'])
        self.assertEqual(self.obs['searched'], ['Beta'])
        self.assertTrue(self.obs['none_hidden_on_a_hit'])
        self.assertEqual(self.obs['hidden_chip_rows'], [])
        self.assertTrue(self.obs['none_shown_on_no_hit'])
        self.assertTrue(self.obs['hidden_chip_active'])

    def test_the_channels_are_paged_over_what_the_search_leaves(self):
        self.assertTrue(self.obs['pager_hidden_on_one_page'])
        self.assertEqual(self.obs['page_one'], {'rows': ['Alpha', 'Game feeds'], 'text': '1-2 of 3',
                                                'hidden': False, 'prev_off': True, 'next_off': False})
        self.assertEqual(self.obs['page_two'], {'rows': ['Beta'], 'text': '3-3 of 3',
                                                'prev_off': False, 'next_off': True})
        # A search starts from its own first page, or a match on page one would stay hidden.
        self.assertEqual(self.obs['search_from_page_two'], {'rows': ['Alpha'], 'pager_hidden': True})
        self.assertEqual(self.obs['back_to_page_one'], ['Alpha', 'Game feeds'])

    def test_a_switch_posts_once_then_takes_the_servers_render_and_keeps_the_search(self):
        self.assertEqual(self.obs['unwatched_off'],
                         [f'POST /api/iptv-output/rows/{self.obs["beta_feed"]}', 'GET /iptv-output'])
        self.assertEqual(self.obs['unwatched_body'], {'on': False})
        self.assertFalse(self.obs['modal_for_unwatched'])
        self.assertEqual(self.obs['search_kept_after_swap'], {'value': 'BET', 'rows': ['Beta']})
        self.assertTrue(self.obs['hide_toast'])

    def test_hiding_a_watched_channel_asks_first(self):
        self.assertIn('1 IPTV player is watching it right now', self.obs['watched_asks'])
        self.assertEqual(self.obs['cancel_posts'], [])
        self.assertTrue(self.obs['cancel_leaves_on'])
        self.assertEqual(self.obs['confirm_posts'][0], f'POST /api/iptv-output/rows/{self.obs["alpha_feed"]}')

    def test_a_refused_switch_goes_back_and_says_why(self):
        self.assertTrue(self.obs['refused_back_on'])
        self.assertTrue(self.obs['refused_toast'])

    def test_the_password_arrives_only_when_asked_for(self):
        self.assertFalse(self.obs['stored_in_page'])
        self.assertTrue(self.obs['playlist_shown'])
        self.assertIn('password=********', self.obs['m3u_masked'])
        self.assertIn('password=p%26w%3D1&type=m3u_plus', self.obs['m3u_revealed'])
        self.assertEqual(self.obs['m3u_masked_again'], self.obs['m3u_masked'])
        self.assertEqual((self.obs['xtream_revealed'], self.obs['xtream_masked_again']),
                         ('p&w=1', '********'))
        # The playlist address percent-encodes it; the Xtream password is copied as typed.
        self.assertIn('password=p%26w%3D1', self.obs['copied'][0])
        self.assertEqual(self.obs['copied'][1], 'p&w=1')
        self.assertEqual(self.obs['copied'][2], LAN)

    def test_the_connect_card_shows_the_login_picked_and_reveals_its_password(self):
        """dev/changelog/1781: every login is a row, so the card shows one at a time."""
        self.assertEqual(self.obs['conn_picker'], ['Kim', 'Main login'])
        self.assertEqual(self.obs['conn_shown_first'], ['kim'])
        self.assertEqual(self.obs['conn_shown_picked'], ['tv'])
        tv = self.obs['login_ids']['tv']
        self.assertEqual(self.obs['conn_reveal'],
                         {'paths': [f'/api/iptv-output/logins/{tv}/password'], 'picked': str(tv)})

    def test_an_account_switch_posts_allow(self):
        self.assertEqual(self.obs['account_post'],
                         {'path': f'/api/iptv-output/accounts/{self.obs["account_id"]}',
                          'body': {'allow': False}})

    def test_a_setting_saves_through_the_settings_route_as_its_own_type(self):
        self.assertEqual(self.obs['setting_post'],
                         {'path': '/api/settings/field',
                          'body': {'path': 'integrations.output_feed.max_viewers', 'value': 5}})
        self.assertEqual(self.obs['mode_post'], {'path': 'integrations.output_feed.mode', 'value': 'redirect'})

    def test_a_saved_number_does_not_replace_the_row_being_typed_in(self):
        self.assertEqual(self.obs['half_typed_survives_a_save'], '4')
        self.assertEqual(self.obs['saved_field_shows_what_was_saved'], '5')

    def test_a_recent_plays_chip_swaps_its_card_and_moves_the_address(self):
        self.assertEqual(self.obs['chip_fetch'], ['GET /iptv-output?plays=refused'])
        self.assertEqual(self.obs['chip_address'], '/iptv-output?plays=refused')

    def test_stop_asks_then_posts(self):
        self.assertEqual(self.obs['stop_asks'], 'Stop this play?')
        self.assertRegex(self.obs['stop_posts'][0], r'^POST /api/iptv-output/plays/.+/stop$')

    def test_change_login_prefills_the_username_and_switches_and_never_a_password(self):
        """dev/changelog/1781: every login is a row, the owner's own included, and its two
        home-only switches are set in its Change dialog."""
        self.assertEqual(self.obs['login_prefill'],
                         {'user': 'tv', 'pass': '', 'direct': True, 'whole': True})
        post = self.obs['login_post']
        self.assertEqual(post['path'], f"/api/iptv-output/logins/{post['row']}")
        self.assertNotIn('password', post['body'])
        self.assertEqual((post['body']['provider_direct'], post['body']['full_refusals']),
                         (False, True))

    def test_the_live_refresh_waits_for_an_open_dialog(self):
        self.assertEqual(self.obs['live_ms'], [15000])
        self.assertEqual(self.obs['tick_fetches'], ['GET /iptv-output'])
        self.assertEqual(self.obs['tick_under_dialog'], [])

    def test_turning_off_says_who_is_cut_and_posts_the_switch(self):
        self.assertIn('No IPTV player can connect', self.obs['power_asks'])
        self.assertIn('1 IPTV player watching right now is cut.', self.obs['power_asks'])
        self.assertEqual(self.obs['power_post']['body'],
                         {'path': 'integrations.output_feed.enabled', 'value': False})


    def test_hide_stores_the_notice_version_then_redraws_the_cards(self):
        """dev/changelog/1648: the address notice's Hide posts the version the page was
        handed under the pref it names, then swaps the cards for the server's render."""
        from app.output_feed_view import ADDRESS_NOTICE_PREF, ADDRESS_NOTICE_VERSION
        self.assertEqual(self.obs['hide_offered'], {'pref': ADDRESS_NOTICE_PREF,
                                                     'version': str(ADDRESS_NOTICE_VERSION)})
        reqs = self.obs['hide_requests']
        self.assertEqual(reqs[0], {'method': 'POST', 'path': f'/api/user-prefs/{ADDRESS_NOTICE_PREF}',
                                   'body': {'value': ADDRESS_NOTICE_VERSION}})
        self.assertIn('GET', [r['method'] for r in reqs[1:]])

    def test_choose_channels_reads_the_logins_list_and_posts_feed_ids(self):
        """dev/changelog/1764: the dialog draws the login's own read, a search narrows it and
        Pick all shown picks only what it left; Save posts feed ids, nothing picked posts
        nothing, and Everything posts null."""
        lu = self.obs
        self.assertEqual([x for x in lu['lu_fetch'] if x.startswith('GET /api/')],
                         [f'GET /api/iptv-output/logins/{self.obs["login_id"]}/channels'])
        self.assertEqual(lu['lu_initial']['title'], 'Channels for Kim')
        self.assertEqual(lu['lu_initial']['picked'], [2])
        self.assertTrue(lu['lu_initial']['pick_shown'])
        self.assertEqual(lu['lu_initial']['n'], '1 picked')
        self.assertEqual(lu['lu_initial']['heads'], ['News', 'Sports', 'ChannelBin'])
        self.assertIn('Beta Taken off the output', lu['lu_initial']['notes'])
        self.assertIn('PiP 1 and its swapped copy', lu['lu_initial']['notes'])
        self.assertEqual(lu['lu_searched_category'], [2, 4])   # a category's name finds its channels
        self.assertEqual(lu['lu_searched'], [3])
        self.assertEqual(lu['lu_heads_shown'], ['Sports'])
        self.assertEqual(lu['lu_after_pick_all'], {'picked': [2, 3], 'n': '2 picked'})
        self.assertEqual(lu['lu_post'], [{'path': f'/api/iptv-output/logins/{self.obs["login_id"]}',
                                          'body': {'lineup': [2, 3]}}])
        self.assertTrue(lu['lu_closed'])
        self.assertEqual(lu['lu_empty'], {'posts': 0, 'open': True, 'toast': True})
        self.assertTrue(lu['lu_everything_hides_list'])
        self.assertEqual(lu['lu_everything_post'], [{'lineup': None}])


    def test_choose_accounts_reads_the_logins_accounts_and_posts_ids(self):
        """dev/changelog/1765: the dialog draws the login's own read with the list hidden while
        it plays from the output's accounts; nothing picked posts nothing, a pick posts account
        ids and the output's accounts post null."""
        la, login = self.obs, self.obs['login_id']
        self.assertEqual([x for x in la['la_fetch'] if x.startswith('GET /api/')],
                         [f'GET /api/iptv-output/logins/{login}/accounts'])
        self.assertEqual(la['la_initial']['title'], 'Accounts for Kim')
        self.assertTrue(la['la_initial']['list_hidden'])
        self.assertEqual(la['la_initial']['rows'], ['Acct', "Other Not one of the output's accounts"])
        self.assertTrue(la['la_list_shown'])
        self.assertEqual(la['la_empty'], {'posts': 0, 'open': True, 'toast': True})
        self.assertEqual(la['la_post'], [{'path': f'/api/iptv-output/logins/{login}', 'body': {'accounts': [2]}}])
        self.assertTrue(la['la_closed'])
        self.assertEqual(la['la_output_post'], [{'accounts': None}])


if __name__ == '__main__':
    unittest.main()
