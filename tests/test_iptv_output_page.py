"""The IPTV Output page and its actions (dev/changelog/1526, mockup 53).

What the page shows in each state of the output, that the password never reaches the page's
source, and that each action is enforced by its route: taking a channel off cuts the play of
it, an account switched off cuts the plays on it and the last one cannot go off, a number of
connections above the account's own is refused, and a login is saved whole or not at all.
No dev/docs/BUGS.md entry: a feature.
"""
import os
import re
import sys
import unittest
from unittest import mock
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tests.support.output_feed import FeedCase as _Case, feed_cfg as _cfg  # noqa: E402

import app.output_feed as feed  # noqa: E402
import app.output_feed_view as view  # noqa: E402
from app import db  # noqa: E402
from app.config import load_config  # noqa: E402
from app.database import (ChannelEvent, EPGEntry, FEED_PLAY_ENDED, FEED_PLAY_REDIRECTED, FEED_PLAY_REFUSED,  # noqa: E402
                          OUTPUT_FEED_SWITCHED, OutputFeedPlay)

SECRET = 'sofa-remote-42'


def _region(html: str, element_id: str) -> str:
    """The markup of one card, from its id to the next card's."""
    start = html.index(f'id="{element_id}"')
    nxt = re.search(r'<div class="card[^"]*" id="of-', html[start + 1:])
    return html[start:start + 1 + nxt.start()] if nxt else html[start:]


class _PageCase(_Case):
    FEED = _cfg(password=SECRET)

    def page(self, query=''):
        resp = self.client.get('/iptv-output' + query)
        self.assertEqual(resp.status_code, 200)
        return resp.get_data(as_text=True)

    def streams(self):
        return self.get('/player_api.php', action='get_live_streams', password=SECRET).get_json()

    def feed_cfg(self):
        return feed.feed_config(load_config())

    def post(self, path, body):
        return self.client.post(path, json=body)


class PageStateTests(_PageCase):
    def test_on_shows_every_card_and_the_action_bar(self):
        html = self.page()
        self.assertIn('data-state="on"', html)
        self.assertIn('3 channels on the output.', html)
        self.assertIn('Nobody is watching.', html)
        for card in ('of-connect', 'of-watching', 'of-channels', 'of-recent', 'of-accounts',
                     'of-settings'):
            self.assertIn(f'id="{card}"', html)
        self.assertNotIn(' inert', _region(html, 'of-channels').split('>')[0])

    def test_off_is_a_banner_and_what_is_offered_goes_inert(self):
        self.t.sandbox_config(_cfg(password=SECRET, enabled=False))
        html = self.page()
        self.assertIn('The IPTV output is off', html)
        self.assertIn('data-act="power" data-to="on"', html)
        self.assertNotIn('id="of-watching"', html)
        self.assertIn('Channels it will offer', html)
        for card in ('of-channels', 'of-accounts'):
            self.assertIn(' inert', _region(html, card).split('>')[0], card)
        # The login can still be changed while the output is off: only the card's body is inert.
        connect = _region(html, 'of-connect')
        self.assertNotIn(' inert', connect.split('>')[0])
        self.assertLess(connect.index('data-act="login"'), connect.index(' inert'))
        self.assertNotIn(' inert', _region(html, 'of-settings'))

    def test_not_set_up_offers_the_login_and_hides_the_connection_details(self):
        self.t.sandbox_config(_cfg(enabled=False, username='', password=''))
        html = self.page()
        self.assertIn('The IPTV output is not set up yet', html)
        self.assertIn('data-act="login" data-first="1"', html)
        self.assertNotIn('id="of-connect"', html)
        self.assertNotIn('id="of-recent"', html)

    def test_redirect_mode_says_what_it_cannot_see_and_offers_the_held_connections(self):
        self.t.sandbox_config(_cfg(password=SECRET, mode='redirect'))
        html = self.page()
        self.assertIn('In redirect mode ChannelBin cannot see who is watching.', html)
        accounts = _region(html, 'of-accounts')
        self.assertIn('Recordings leave alone', accounts)
        self.assertIn('>The only one</option>', accounts)
        self.assertNotIn('Nothing records from this account', accounts)
        self.t.sandbox_config(_cfg(password=SECRET, mode='redirect', held_seats={str(self.account.id): 1}))
        self.assertEqual(_region(self.page(), 'of-accounts').count('Nothing records from this account'), 1)
        self.assertNotIn('Current streams', accounts)
        self.assertNotIn('IPTV players at once', _region(html, 'of-settings'))

    def test_the_menu_has_the_page_last_under_channels_and_guide(self):
        html = self.page()
        nav = html[html.index('Channels &amp; guide'):]
        self.assertLess(nav.index('Sports Schedules'), nav.index('IPTV Output'))
        self.assertLess(nav.index('IPTV Output'), nav.index('Setup'))


class SecretTests(_PageCase):
    def test_the_password_is_never_in_the_page_source(self):
        for block in ({}, {'mode': 'redirect'}, {'enabled': False}):
            self.t.sandbox_config(_cfg(password=SECRET, **block))
            self.assertNotIn(SECRET, self.page())

    def test_the_playlist_addresses_carry_a_slot_where_the_password_goes(self):
        html = self.page()
        self.assertIn('/get.php?username=tv&amp;password={password}&amp;type=m3u_plus', html)
        self.assertIn('/xmltv.php?username=tv&amp;password={password}', html)
        self.assertIn('data-secret-text="{password}">********</span>', html)

    def test_the_address_is_the_setting_when_one_is_set(self):
        self.t.sandbox_config(dict(_cfg(password=SECRET), flask={'public_url': 'http://192.168.1.40:5000/'}))
        html = self.page()
        self.assertIn('<span class="v" data-copy-value>http://192.168.1.40:5000</span>', html)
        self.assertNotIn("ChannelBin's address is not set", html)

    def test_no_address_set_and_opened_on_localhost_is_a_warning(self):
        self.assertIn('this page was opened at localhost', self.page())


class BehindProxyTests(_PageCase):
    """dev/docs/BUGS.md 2026-11-12: behind a reverse proxy with flask.behind_proxy off, the
    page showed http for an https address and the proxy's address for every IPTV player, and
    said nothing about why."""
    PROXIED = {'X-Forwarded-Proto': 'https', 'X-Forwarded-For': '192.168.1.50'}

    def proxied_page(self):
        return self.client.get('/iptv-output', headers=self.PROXIED).get_data(as_text=True)

    def test_a_proxy_the_app_was_not_told_about_is_named_with_the_fix(self):
        connect = _region(self.proxied_page(), 'of-connect')
        self.assertIn('behind a reverse proxy and has not been told so', connect)
        self.assertIn('flask.behind_proxy: true', connect)
        self.assertIn('q=flask.behind_proxy', connect)
        # Named, never believed: the address is still the one the app itself can vouch for.
        self.assertIn('http://localhost', connect)
        self.assertNotIn('https://localhost', connect)

    def test_no_proxy_headers_no_notice(self):
        self.assertNotIn('reverse proxy', _region(self.page(), 'of-connect'))

    def test_a_proxy_the_app_trusts_is_not_a_problem(self):
        self.t.app.config['BEHIND_PROXY'] = True
        self.assertNotIn('reverse proxy', _region(self.proxied_page(), 'of-connect'))

    def test_turned_on_in_the_file_and_not_restarted_says_restart(self):
        self.t.sandbox_config(dict(_cfg(password=SECRET), flask={'behind_proxy': True}))
        connect = _region(self.proxied_page(), 'of-connect')
        self.assertIn('waits for a restart', connect)
        self.assertNotIn('has not been told so', connect)

    def test_the_app_factory_records_that_it_trusts_the_proxy(self):
        self.assertFalse(self.t.app.config.get('BEHIND_PROXY'))
        self.assertEqual(view.proxy_state({}, self.PROXIED, True), '')
        self.assertEqual(view.proxy_state({}, self.PROXIED, False), view.PROXY_UNSEEN)
        self.assertEqual(view.proxy_state({'flask': {'behind_proxy': True}}, self.PROXIED, False),
                         view.PROXY_RESTART)
        self.assertEqual(view.proxy_state({}, {}, False), '')


class AddressSettingTests(_PageCase):
    def save(self, value):
        return self.post('/api/settings/field', {'path': 'flask.public_url', 'value': value})

    def test_a_whole_address_is_saved_and_shown(self):
        for value in ('https://channelbin.example.com', 'http://192.168.1.1:5000', ''):
            self.assertEqual(self.save(value).status_code, 200, value)
        self.save('https://channelbin.example.com/')
        self.assertIn('https://channelbin.example.com/get.php', self.page())

    def test_an_address_with_no_scheme_or_host_is_refused_with_the_examples(self):
        for value in ('channelbin.example.com', '192.168.1.1:5000', 'ftp://channelbin.example.com',
                      'https://'):
            resp = self.save(value)
            self.assertEqual(resp.status_code, 400, value)
            self.assertIn('https://channelbin.example.com or http://192.168.1.1:5000',
                          resp.get_json()['error'])
        self.assertEqual(load_config()['flask']['public_url'], '')

    def test_both_settings_rows_show_how_to_type_it(self):
        for path in ('/settings', '/settings/notifications'):
            html = self.client.get(path).get_data(as_text=True)
            self.assertIn('https://channelbin.example.com', html, path)
            self.assertIn('http://192.168.1.1:5000', html, path)


class ChannelPagingTests(_PageCase):
    def rows(self, html):
        return re.findall(r'<tr[^>]*data-feed="\d+"[^>]*>', _region(html, 'of-channels'))

    def test_a_short_list_has_every_row_shown_and_no_pager(self):
        html = self.page()
        self.assertEqual(sum(' hidden' in tr for tr in self.rows(html)), 0)
        self.assertIn('id="of-chan-pager" hidden', html)

    def test_past_one_page_the_server_draws_the_first_and_the_pager(self):
        """The first render has to be page one already: a script may page further, never
        be needed to hide rows the server showed."""
        from tests.support.seed import make_channel
        extra = view.CHANNELS_PER_PAGE + 4
        for i in range(extra):
            ch = make_channel(self.account, name=f'Extra {i:03d}', in_guide=True)
            ch.guide_sort_order = 10 + i
        db.session.commit()
        html = self.page()
        rows = self.rows(html)
        total = extra + 3
        self.assertEqual(len(rows), total)
        self.assertEqual([' hidden' in tr for tr in rows],
                         [False] * view.CHANNELS_PER_PAGE + [True] * (total - view.CHANNELS_PER_PAGE))
        channels = _region(html, 'of-channels')
        self.assertIn(f'data-per-page="{view.CHANNELS_PER_PAGE}"', channels)
        self.assertIn('id="of-chan-pager">', channels)
        self.assertIn(f'>1-{view.CHANNELS_PER_PAGE} of {total}<', channels)
        self.assertRegex(channels, r'data-dir="-1" disabled>')
        self.assertRegex(channels, r'data-dir="1">')


class ChannelRowTests(_PageCase):
    def test_every_guide_row_is_listed_with_its_number_and_what_it_plays_from(self):
        rows = re.findall(r'<tr[^>]*data-feed="(\d+)"[^>]*data-state="(\w+)"', self.page())
        self.assertEqual(rows, [(str(feed.feed_id_for_channel(self.alpha.id)), 'on'),
                                (str(feed.feed_id_for_group(self.group.id)), 'on'),
                                (str(feed.feed_id_for_channel(self.beta.id)), 'on')])
        channels = _region(self.page(), 'of-channels')
        self.assertIn('The best of 1 channel it can play right now, of 1 in the group', channels)
        self.assertIn('>Member</a>', channels)

    def test_taking_a_row_off_is_stored_evented_and_drawn(self):
        fid = feed.feed_id_for_channel(self.alpha.id)
        resp = self.post(f'/api/iptv-output/rows/{fid}', {'on': False})
        self.assertEqual(resp.get_json(), {'success': True, 'name': 'Alpha', 'on': False, 'cut': 0})
        self.assertFalse(db.session.get(type(self.alpha), self.alpha.id).on_output)
        self.assertEqual(ChannelEvent.query.filter_by(event_type=OUTPUT_FEED_SWITCHED).count(), 1)
        self.assertNotIn('Alpha', [s['name'] for s in self.streams()])
        html = self.page()
        self.assertIn(f'data-feed="{fid}" data-name="Alpha"', html)
        self.assertRegex(html, rf'data-feed="{fid}"[^>]*data-state="hidden"')
        self.assertIn('2 channels on the output.', html)
        back = self.post(f'/api/iptv-output/rows/{fid}', {'on': True}).get_json()
        self.assertTrue(back['on'])
        self.assertIn('Alpha', [s['name'] for s in self.streams()])

    def test_taking_a_watched_row_off_cuts_that_play_by_name(self):
        fid = feed.feed_id_for_channel(self.alpha.id)
        s = self.session(fid)
        self.assertRegex(self.page(), rf'data-feed="{fid}"[^>]*data-watching="1"')
        resp = self.post(f'/api/iptv-output/rows/{fid}', {'on': False})
        self.assertEqual(resp.get_json()['cut'], 1)
        self.assertEqual((s.live, s.reason), (False, feed.REASON_TAKEN_OFF))

    def test_the_row_route_refuses_what_it_cannot_switch(self):
        fid = feed.feed_id_for_channel(self.alpha.id)
        self.assertEqual(self.post(f'/api/iptv-output/rows/{fid}', {'on': 'no'}).status_code, 400)
        self.assertEqual(self.post(f'/api/iptv-output/rows/{fid}', {}).status_code, 400)
        loose = feed.feed_id_for_channel(self.loose.id)
        self.assertEqual(self.post(f'/api/iptv-output/rows/{loose}', {'on': False}).status_code, 404)
        sports = feed.feed_id_for_sports(1)
        resp = self.post(f'/api/iptv-output/rows/{sports}', {'on': False})
        self.assertEqual(resp.status_code, 400)
        self.assertIn('Sports Schedules', resp.get_json()['error'])


class BlockedAccountTests(_PageCase):
    def test_a_channel_on_a_blocked_account_says_it_cannot_play(self):
        with mock.patch('app.channel_groups.blocked_account_ids_now',
                        return_value=frozenset({self.account.id})):
            html = self.page()
        self.assertRegex(html, r'data-name="Alpha"[^>]*data-state="on" data-problem="1"')
        self.assertIn('Nothing to play: its account is blocked right now.', html)
        self.assertRegex(html, r'data-show="problem">Cannot play<span class="n">2</span>')


class WatchingTests(_PageCase):
    def test_a_play_is_listed_with_the_program_its_guide_lists_now(self):
        now = datetime.utcnow()
        db.session.add(EPGEntry(channel_id=self.alpha.id, title='Evening News',
                                start_time=now - timedelta(minutes=10),
                                stop_time=now + timedelta(minutes=20)))
        db.session.add(EPGEntry(channel_id=self.alpha.id, title='Later Show',
                                start_time=now + timedelta(minutes=20),
                                stop_time=now + timedelta(minutes=80)))
        db.session.commit()
        self.session(feed.feed_id_for_channel(self.alpha.id))
        watching = _region(self.page(), 'of-watching')
        self.assertIn('192.168.1.23', watching)
        self.assertIn('Evening News', watching)
        self.assertNotIn('Later Show', watching)
        self.assertIn('1 IPTV player watching.', self.page())

    def test_at_the_limit_the_page_says_the_next_one_is_refused(self):
        self.t.sandbox_config(_cfg(password=SECRET, max_viewers=1))
        self.session(feed.feed_id_for_channel(self.alpha.id))
        self.assertIn('which is the most allowed at once', self.page())

    def test_a_move_is_said_in_words_and_never_as_its_reason_code(self):
        """dev/docs/BUGS.md 2026-11-15 @ 03:51:40 PM: the tip printed the move's REASON_* code."""
        s = self.session(feed.feed_id_for_channel(self.alpha.id))
        s.failovers.append(('Alpha', 'Beta', feed.REASON_FFMPEG_EXITED))
        s.failovers.append(('Beta', 'Gamma', feed.REASON_CONNECT_TIMEOUT))
        watching = _region(self.page(), 'of-watching')
        self.assertIn('Moved to another channel 2 times', watching)
        self.assertIn('&#34;Alpha&#34; stopped delivering.', watching)
        self.assertIn('&#34;Beta&#34; sent nothing in time.', watching)
        for code in (feed.REASON_FFMPEG_EXITED, feed.REASON_CONNECT_TIMEOUT):
            self.assertNotIn(code, watching)

    def test_an_ended_play_keeps_why_it_moved_in_recent_plays(self):
        """A sports channel has no group timeline, so the play's own line is where a move's
        reason outlives the play (dev/changelog/1568)."""
        s = self.session(feed.feed_id_for_channel(self.alpha.id))
        s.failovers.append(('Alpha', 'Beta', feed.REASON_FFMPEG_EXITED))
        with mock.patch.object(feed, '_queue_play') as queued:
            feed._finish(s, feed.REASON_CLIENT_GONE)
        line = queued.call_args[0][1]
        self.assertEqual(line['moves'], 1)
        self.assertIn('Moved from "Alpha" to "Beta": "Alpha" stopped delivering.', line['message'])

    def test_a_play_on_less_reserve_than_set_says_so_while_it_plays(self):
        s = self.session(feed.feed_id_for_channel(self.alpha.id))
        self.assertNotIn('went out sooner than', _region(self.page(), 'of-watching'))
        s.ring.released_early = 5 * 1024 * 1024
        watching = _region(self.page(), 'of-watching')
        self.assertIn('5.0 MB went out sooner than', watching)
        self.assertIn('buffer was nearly full', watching)

    def test_stop_ends_the_play_as_stopped_from_the_page(self):
        s = self.session(feed.feed_id_for_channel(self.alpha.id))
        resp = self.post(f'/api/iptv-output/plays/{s.id}/stop', {})
        self.assertEqual(resp.get_json(), {'success': True})
        self.assertEqual((s.live, s.reason), (False, feed.REASON_STOPPED))
        self.assertEqual(self.post(f'/api/iptv-output/plays/{s.id}/stop', {}).status_code, 404)
        self.assertEqual(self.post('/api/iptv-output/plays/nothing/stop', {}).status_code, 404)


class AccountTests(_PageCase):
    def allow(self, account, on):
        return self.post(f'/api/iptv-output/accounts/{account.id}', {'allow': on})

    def test_switching_an_account_off_stores_the_rest_and_its_channels_leave(self):
        resp = self.allow(self.account, False)
        self.assertEqual(resp.get_json(), {'success': True, 'name': 'Acct', 'allow': False, 'cut': 0})
        self.assertEqual(self.feed_cfg()['accounts'], [self.other.id])
        html = self.page()
        self.assertRegex(html, r'data-name="Alpha"[^>]*data-state="left_out"')
        self.assertIn('Not on the output: its account is switched off below.', html)
        self.assertIn('1 channel on the output.', html)

    def test_every_account_on_again_is_stored_as_the_empty_list(self):
        self.allow(self.account, False)
        self.allow(self.account, True)
        self.assertEqual(self.feed_cfg()['accounts'], [])

    def test_the_last_account_cannot_be_switched_off(self):
        self.allow(self.account, False)
        resp = self.allow(self.other, False)
        self.assertEqual(resp.status_code, 400)
        self.assertIn('at least one account', resp.get_json()['error'])
        self.assertEqual(self.feed_cfg()['accounts'], [self.other.id])

    def test_switching_an_account_off_cuts_the_plays_on_it_by_name(self):
        s = self.session(feed.feed_id_for_channel(self.alpha.id))
        self.assertEqual(self.allow(self.account, False).get_json()['cut'], 1)
        self.assertEqual((s.live, s.reason), (False, feed.REASON_ACCOUNT_OFF))
        self.assertIn(feed.REASON_ACCOUNT_OFF, feed.REASON_TEXT)

    def test_an_unknown_account_and_an_empty_change_are_refused(self):
        self.assertEqual(self.post('/api/iptv-output/accounts/9999', {'allow': False}).status_code, 404)
        self.assertEqual(self.post(f'/api/iptv-output/accounts/{self.account.id}', {}).status_code, 400)
        self.assertEqual(self.allow(self.account, 'off').status_code, 400)

    def test_connections_left_alone_are_stored_and_bounded_by_the_account(self):
        self.t.sandbox_config(_cfg(password=SECRET, mode='redirect'))
        url = f'/api/iptv-output/accounts/{self.account.id}'
        self.assertEqual(self.post(url, {'held': 1}).get_json()['held'], 1)
        self.assertEqual(feed.held_seats_map(self.feed_cfg()), {self.account.id: 1})
        over = self.post(url, {'held': 2})
        self.assertEqual(over.status_code, 400)
        self.assertIn('allows 1 connection', over.get_json()['error'])
        self.assertEqual(self.post(url, {'held': -1}).status_code, 400)
        self.assertEqual(self.post(url, {'held': 'two'}).status_code, 400)
        self.post(url, {'held': 0})
        self.assertEqual(feed.held_seats_map(self.feed_cfg()), {})


class LoginTests(_PageCase):
    def login(self, **body):
        return self.post('/api/iptv-output/login', body)

    def test_the_first_login_is_saved_and_does_not_turn_the_output_on(self):
        self.t.sandbox_config(_cfg(enabled=False, username='', password=''))
        resp = self.login(username=' den ', password='tv1')
        self.assertEqual(resp.get_json(), {'success': True, 'first': True})
        fcfg = self.feed_cfg()
        self.assertEqual((fcfg['username'], fcfg['password'], fcfg['enabled']), ('den', 'tv1', False))
        self.assertIn('The IPTV output is off', self.page())

    def test_a_first_login_under_a_switch_left_on_does_not_start_serving(self):
        self.t.sandbox_config(_cfg(enabled=True, username='', password=''))
        self.assertIn('The IPTV output is not set up yet', self.page())
        self.login(username='den', password='tv1')
        self.assertFalse(self.feed_cfg()['enabled'])
        self.assertIn('The IPTV output is off', self.page())

    def test_changing_a_login_leaves_the_output_on(self):
        self.login(username='den', password='tv2')
        self.assertTrue(self.feed_cfg()['enabled'])

    def test_a_blank_password_keeps_the_stored_one(self):
        self.assertEqual(self.login(username='den', password='').get_json()['first'], False)
        fcfg = self.feed_cfg()
        self.assertEqual((fcfg['username'], fcfg['password']), ('den', SECRET))

    def test_a_login_missing_a_half_or_holding_a_path_character_is_refused_whole(self):
        self.assertEqual(self.login(username='', password='x').status_code, 400)
        for bad in ({'username': 'a/b', 'password': 'x'}, {'username': 'tv', 'password': 'a b'},
                    {'username': 'tv', 'password': 'a?b'}, {'username': 'x' * 61, 'password': 'p'}):
            self.assertEqual(self.login(**bad).status_code, 400, bad)
        self.t.sandbox_config(_cfg(enabled=False, username='', password=''))
        self.assertEqual(self.login(username='den', password='').status_code, 400)
        fcfg = self.feed_cfg()
        self.assertEqual((fcfg['username'], fcfg['password']), ('', ''))


class SettingsRouteTests(_PageCase):
    """What the page's Settings card and any other caller get from /api/settings/field."""

    def field(self, leaf, value):
        return self.post('/api/settings/field', {'path': f'integrations.output_feed.{leaf}', 'value': value})

    def test_the_guide_length_help_names_the_guide_depth_that_is_set(self):
        """dev/docs/BUGS.md 2026-11-15 @ 03:51:40 PM: the help said the TV Guide's depth
        "is 3" whatever sync.epg_days_ahead was."""
        self.t.sandbox_config({**_cfg(password=SECRET), 'sync': {'epg_days_ahead': 7}})
        html = self.client.get('/settings').get_data(as_text=True)
        self.assertIn('which is set to 7 days', html)
        self.assertNotIn('own depth is 3', html)

    def test_a_number_outside_its_bounds_is_moved_to_the_nearest_end(self):
        for leaf, (low, high) in feed.SETTING_BOUNDS.items():
            if leaf in ('buffer_mb', 'reserve_seconds'):
                continue    # the pair is also held to each other, which has its own test
            self.assertTrue(self.field(leaf, high + 1000).get_json()['success'], leaf)
            self.assertEqual(self.feed_cfg()[leaf], high, leaf)
            self.field(leaf, low - 5)
            self.assertEqual(self.feed_cfg()[leaf], low, leaf)

    def test_the_page_draws_each_number_with_the_bounds_the_route_holds(self):
        settings = _region(self.page(), 'of-settings')
        for leaf, (low, high) in feed.SETTING_BOUNDS.items():
            self.assertRegex(settings, rf'min="{low}" max="{high}" value="\d+" data-setting-path="integrations.output_feed.{leaf}"')

    def test_half_a_login_saved_through_settings_is_held_to_the_same_check(self):
        self.assertEqual(self.field('username', 'a/b').status_code, 400)
        self.assertEqual(self.field('password', 'a b').status_code, 400)
        cleared = self.field('password', '')
        self.assertEqual(cleared.status_code, 400)
        self.assertIn('Turn the IPTV output off', cleared.get_json()['error'])
        self.assertEqual(self.feed_cfg()['password'], SECRET)
        self.t.sandbox_config(_cfg(password=SECRET, enabled=False))
        self.assertTrue(self.field('password', '').get_json()['success'])

    def test_the_readiness_check_sends_the_user_to_the_page(self):
        from app import readiness
        check = next(c for c in readiness.CHECKS if c.id == 'output_feed')
        self.assertEqual(check.link.endpoint, 'iptv_output.page')
        self.assertIn('IPTV Output page', check.setup)
        self.assertNotIn('Settings >', check.setup)


class RecentPlaysTests(_PageCase):
    def seed_plays(self, ended, refused):
        now = datetime.utcnow()
        fid = feed.feed_id_for_channel(self.alpha.id)
        for i in range(ended):
            db.session.add(OutputFeedPlay(
                kind=FEED_PLAY_ENDED, at=now - timedelta(minutes=i), first_at=now, feed_id=fid,
                label=f'channel {self.alpha.id} (Alpha)', client='192.168.1.23',
                channel_name='Alpha', message=f'Ended line {i:03d}.', seconds=600))
        for i in range(refused):
            db.session.add(OutputFeedPlay(
                kind=FEED_PLAY_REFUSED, at=now - timedelta(hours=2, minutes=i), first_at=now,
                feed_id=fid, label=f'channel {self.alpha.id} (Alpha)', client='192.168.1.44',
                message=f'Nothing to play for channel {self.alpha.id} (Alpha): refused {i:03d}.',
                count=3))
        db.session.add(OutputFeedPlay(
            kind=FEED_PLAY_ENDED, at=now - timedelta(days=feed.PLAY_LOG_DAYS + 1), first_at=now,
            feed_id=fid, label='old', message='Too old to show.'))
        db.session.commit()

    def test_a_line_reads_in_the_names_the_user_knows(self):
        self.seed_plays(1, 1)
        recent = _region(self.page(), 'of-recent')
        self.assertRegex(recent, r'Played <span>Alpha</span> for 10m')
        self.assertIn('Nothing to play: refused 000.', recent)
        self.assertIn('Asked 3 times, last at', recent)
        self.assertNotIn('Too old to show.', recent)

    def test_the_list_is_paged_and_filtered_by_kind(self):
        self.seed_plays(view.PLAYS_PER_PAGE + 5, 4)
        first = _region(self.page(), 'of-recent')
        self.assertIn(f'1-{view.PLAYS_PER_PAGE} of {view.PLAYS_PER_PAGE + 9}', first)
        self.assertIn('Ended line 000.', first)
        self.assertNotIn('refused 000', first)
        second = _region(self.page('?page=2'), 'of-recent')
        self.assertIn('refused 000', second)
        self.assertIn(f'{view.PLAYS_PER_PAGE + 1}-{view.PLAYS_PER_PAGE + 9} of', second)
        refused = _region(self.page('?plays=refused'), 'of-recent')
        self.assertEqual(refused.count('badge b-warn'), 4)
        self.assertNotIn('Ended line', refused)
        self.assertNotIn('of-pager', refused)
        # A page past the end, or a kind that is not one, lands on something real.
        self.assertIn('Ended line 000.', _region(self.page('?plays=nonsense&page=x'), 'of-recent'))
        self.assertIn('refused 000', _region(self.page('?page=99'), 'of-recent'))


class RecentPlaysLinkTests(_PageCase):
    """A Recent plays line links the channel it played and names its account, linked too, from
    the ids the line kept (dev/changelog/1638). What is gone since reads as text."""

    def add_play(self, kind, **fields):
        now = datetime.utcnow()
        db.session.add(OutputFeedPlay(kind=kind, at=now, first_at=now,
                                      feed_id=feed.feed_id_for_channel(self.alpha.id),
                                      label=f'channel {self.alpha.id} (Alpha)', client='192.168.1.23',
                                      seconds=600, **fields))
        db.session.commit()
        return _region(self.page(), 'of-recent')

    def acct_pill(self, account):
        return f'<a class="ch-pill acct-pill" href="/accounts/{account.id}">'

    def test_an_ended_play_links_its_channel_and_its_account(self):
        recent = self.add_play(FEED_PLAY_ENDED, channel_id=self.alpha.id, channel_name='Alpha',
                               account_id=self.account.id)
        self.assertRegex(recent, r'Played <a class="name-link" href="/channels/%d">Alpha</a>'
                                 r'<a class="ch-pill acct-pill" href="/accounts/%d">.*?Acct</a> for 10m'
                         % (self.alpha.id, self.account.id))

    def test_a_redirect_links_its_channel_and_its_account(self):
        recent = self.add_play(FEED_PLAY_REDIRECTED, channel_id=self.beta.id, channel_name='Beta',
                               account_id=self.other.id)
        self.assertRegex(recent, r'To <a class="name-link" href="/channels/%d">Beta</a>'
                                 r'<a class="ch-pill acct-pill" href="/accounts/%d">.*?Other</a>\. ChannelBin'
                         % (self.beta.id, self.other.id))

    def test_a_deleted_channel_is_text_and_its_account_still_named(self):
        # A channel's delete clears the id from the lines naming it (detach_channel_references).
        recent = self.add_play(FEED_PLAY_ENDED, channel_id=None, channel_name='Gone channel',
                               account_id=self.account.id)
        self.assertIn('Played <span>Gone channel</span>', recent)
        self.assertNotIn('href="/channels/', recent)
        self.assertIn(self.acct_pill(self.account), recent)

    def test_an_account_no_longer_there_draws_no_pill(self):
        recent = self.add_play(FEED_PLAY_ENDED, channel_id=self.alpha.id, channel_name='Alpha',
                               account_id=99999)
        self.assertIn(f'<a class="name-link" href="/channels/{self.alpha.id}">Alpha</a> for 10m', recent)
        self.assertNotIn('acct-pill', recent)

    def test_the_waiting_screen_is_neither_a_link_nor_an_account(self):
        recent = self.add_play(FEED_PLAY_ENDED, channel_id=None, channel_name=feed.SCREEN_NAME,
                               account_id=None)
        self.assertIn(f'Played <span>{feed.SCREEN_NAME}</span> for 10m', recent)
        self.assertNotIn('acct-pill', recent)
        self.assertNotIn('href="/channels/', recent)

    def test_a_refusal_names_no_channel(self):
        recent = self.add_play(FEED_PLAY_REFUSED, message='Nothing to play.', count=2)
        self.assertIn('Asked 2 times, last at', recent)
        self.assertNotIn('name-link', recent)


class SettingsPageTests(_PageCase):
    def test_the_login_is_no_longer_a_settings_row_and_the_page_is_named_there(self):
        html = self.client.get('/settings').get_data(as_text=True)
        self.assertNotIn('data-setting-path="integrations.output_feed.username"', html)
        self.assertNotIn('data-setting-path="integrations.output_feed.password"', html)
        self.assertNotIn('exactly the TV Guide', html)
        self.assertIn('href="/iptv-output"', html)

    def test_the_settings_card_saves_through_the_settings_route(self):
        settings = _region(self.page(), 'of-settings')
        for key in ('mode', 'xmltv_days', 'max_viewers', 'reserve_seconds', 'idle_seconds',
                    'max_session_hours', 'buffer_mb'):
            self.assertIn(f'data-setting-path="integrations.output_feed.{key}"', settings)
        resp = self.post('/api/settings/field', {'path': 'integrations.output_feed.max_viewers', 'value': 5})
        self.assertTrue(resp.get_json()['success'])
        self.assertIn('data-setting-path="integrations.output_feed.max_viewers" data-label="IPTV players at once"',
                      _region(self.page(), 'of-settings'))
        self.assertIn('value="5"', _region(self.page(), 'of-settings'))


if __name__ == '__main__':
    unittest.main()
