"""The output feed: ChannelBin as an Xtream/M3U/XMLTV source for an IPTV player
(app/output_feed.py, app/routes/output_feed.py, dev/docs/DESIGN-output-feed.md,
dev/changelog/1364).

Covers the gate (off, no credential and wrong credential all answer one 401; the right pair
answers every endpoint; the stream path carries the pair), the catalog (feed ids, the guide's
order, a group as one channel with its members unlisted, the allowlist, categories, the
listings join), the guide half (xmltv.php from one query, UTC with an explicit offset,
get.php as the feed's own URLs), the self-sync proof (ChannelBin's own XtreamClient and
sync against the running feed over loopback), the relay (a real ffmpeg over a synthesized
clip: bytes reach the IPTV player, every end has a name and releases everything, a recording preempts
a viewer and both the alert and the recording's timeline say so, a dead group member is
replaced inside the same response, the cap and the connection limit refuse by name), the
ring's bounds, redirect mode (a 302 per tune-in, held seats subtracted from the recorder's
limit), the readiness check, the dashboard section and the support bundle.

  python3 -m unittest tests.test_output_feed
"""
import os
import shutil
import subprocess
import sys
import threading
import time
import unittest
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tests.support import clips  # noqa: E402
from tests.support.app import make_test_app  # noqa: E402
from tests.support.seed import (make_account, make_channel,  # noqa: E402
                                make_epg_entry, make_group, make_guide_match, make_recording)

import app.connection_limits as connlim  # noqa: E402
import app.output_feed as feed  # noqa: E402
from app import db  # noqa: E402
from app.config import load_config  # noqa: E402
from app.database import (Alert, ChannelGroupEvent, RecordingEvent, XtreamAccount,  # noqa: E402
                          DIAGNOSTICS, GROUP_VIEWER_FAILOVER)

_HAVE_FFMPEG = shutil.which('ffmpeg') is not None
USER, PASS = 'tv', 'tv-secret'
FEED_ON = {'integrations': {'output_feed': {'enabled': True, 'username': USER,
                                            'password': PASS, 'reserve_seconds': 0}}}


def _wait(predicate, timeout=15.0, interval=0.1):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return predicate()


def _fresh_count(query) -> int:
    """`query`'s count read in a new transaction: a row a play's own thread commits is not in the
    snapshot a read transaction the test already holds open was given."""
    db.session.commit()
    return query().count()


def _feed_cfg(**overrides):
    block = dict(FEED_ON['integrations']['output_feed'], **overrides)
    return {'integrations': {'output_feed': block}}


class _FeedCase(unittest.TestCase):
    """A fresh app with the feed on and one account; every test ends its own sessions."""

    FEED = None

    def setUp(self):
        self.t = make_test_app(csrf=False)
        self.client = self.t.client
        self.addCleanup(self.t.cleanup)
        self.addCleanup(feed.stop_all)
        self.t.sandbox_config(self.FEED or FEED_ON)
        self.account = make_account('Acct', max_connections=1)
        db.session.commit()

    def get(self, path, **params):
        params.setdefault('username', USER)
        params.setdefault('password', PASS)
        query = '&'.join(f'{k}={v}' for k, v in params.items())
        return self.client.get(f'{path}?{query}')

    def catalog(self):
        return self.get('/player_api.php', action='get_live_streams').get_json()


class OneNameTests(_FeedCase):
    """The feature has one name wherever a person reads it, "IPTV output" (dev/changelog/1588):
    the page and its menu entry were named that, and Settings, the alerts and a dozen sentences
    still said "output feed". Log lines and the settings search words keep the old name."""

    def test_the_settings_page_says_iptv_output(self):
        import re
        html = self.client.get('/settings').get_data(as_text=True)
        self.assertIn('data-path="integrations.output_feed.enabled"', html)
        row = re.search(r'<div class="frow[^>]*data-path="integrations\.output_feed\.enabled".*?</div>\s*</div>',
                        html, re.S).group(0)
        self.assertIn('output feed', re.search(r'data-hay="([^"]*)"', row).group(1),
                      'the old words still find the row')
        shown = re.sub(r'<script.*?</script>|<!--.*?-->|data-hay="[^"]*"', '', html, flags=re.S)
        self.assertNotIn('output feed', re.sub(r'<[^>]+>', ' ', shown).lower())
        self.assertIn('<span class="fl-name">IPTV output</span>', html)
        self.assertIn('Accounts the IPTV output may play from', html)

    def test_a_refusals_alert_and_the_login_answer_say_iptv_output(self):
        ch = make_channel(self.account, name='Paced')
        ch.in_guide = True
        db.session.commit()
        self.assertTrue(connlim.try_acquire(self.account.id, 'recording', 4242))
        self.addCleanup(connlim.release, self.account.id, 'recording', 4242)
        with self.t.app.test_request_context():
            with self.assertRaises(feed.FeedRefused):
                feed.start_session(feed.feed_id_for_channel(ch.id), 'tv')
        self.assertTrue(Alert.query.filter_by(alert_type='VIEWER_REFUSED').one().title.startswith('IPTV output: '))
        self.assertEqual(self.get('/player_api.php').get_json()['user_info']['message'], 'ChannelBin IPTV output')

    def test_the_readiness_check_and_the_alert_type_say_iptv_output(self):
        from app import alerts, readiness
        check = next(c for c in readiness.CHECKS if c[0] == 'output_feed')
        self.assertNotIn('output feed', ' '.join(v for v in check if isinstance(v, str)).lower())
        self.assertEqual(alerts.ALERT_TYPES['FEED_NO_MEMBER']['label'], 'IPTV Output Has Nothing To Play')


# ── Config and the gate ────────────────────────────────────────────────────────────────────

class ConfigTests(unittest.TestCase):
    def test_defaults_are_off_with_no_credential(self):
        t = make_test_app()
        self.addCleanup(t.cleanup)
        block = load_config()['integrations']['output_feed']
        self.assertFalse(block['enabled'])
        self.assertEqual((block['username'], block['password'], block['mode']), ('', '', 'proxy'))
        self.assertEqual(block['held_seats'], {})

    def test_the_password_is_a_sensitive_leaf_and_the_username_is_not(self):
        from app.config import _is_sensitive_path
        self.assertTrue(_is_sensitive_path('integrations.output_feed.password'))
        self.assertFalse(_is_sensitive_path('integrations.output_feed.username'))

    def test_the_password_is_masked_on_the_settings_read_surfaces(self):
        from app.config import MASK_SENTINEL
        t = make_test_app()
        self.addCleanup(t.cleanup)
        t.sandbox_config(FEED_ON)
        data = t.client.get('/api/settings').get_json()
        self.assertEqual(data['integrations']['output_feed']['password'], MASK_SENTINEL)
        self.assertEqual(data['integrations']['output_feed']['username'], USER)
        page = t.client.get('/settings').get_data(as_text=True)
        self.assertNotIn(f'value="{PASS}"', page)

    def test_credentials_are_compared_as_a_pair(self):
        fcfg = {'enabled': True, 'username': 'a', 'password': 'b'}
        self.assertTrue(feed.credentials_ok(fcfg, 'a', 'b'))
        self.assertFalse(feed.credentials_ok(fcfg, 'a', 'a'))
        self.assertFalse(feed.credentials_ok(fcfg, 'b', 'a'))
        self.assertFalse(feed.credentials_ok(fcfg, None, 'b'))
        self.assertFalse(feed.credentials_ok(dict(fcfg, enabled=False), 'a', 'b'))
        self.assertFalse(feed.credentials_ok(dict(fcfg, password=''), 'a', ''))

    def test_the_reserve_must_fit_the_ring(self):
        self.assertTrue(feed.reserve_fits({'buffer_mb': 16, 'reserve_seconds': 3}))
        self.assertFalse(feed.reserve_fits({'buffer_mb': 2, 'reserve_seconds': 3}))
        self.assertTrue(feed.reserve_fits({'buffer_mb': 2, 'reserve_seconds': 0}))
        self.assertEqual(feed.max_reserve_seconds(16), 12)


class AccessLogMaskingTests(unittest.TestCase):
    def test_the_feeds_credentials_are_masked_in_an_access_log_line(self):
        """The IPTV player sends the pair in the stream path and in the .php query, and werkzeug's
        access line reaches the log handlers through CredentialMaskingFilter
        (DESIGN-output-feed.md section 8)."""
        from app.url_utils import mask_creds_in_text
        line = '10.0.0.9 - - [31/Oct/2026 12:00:00] "GET /live/tv/tv-secret/14.ts HTTP/1.1" 200 -'
        self.assertEqual(mask_creds_in_text(line),
                         '10.0.0.9 - - [31/Oct/2026 12:00:00] "GET /live/***/***/14.ts HTTP/1.1" 200 -')
        line = '"GET /player_api.php?username=tv&password=tv-secret&action=get_live_streams HTTP/1.1"'
        self.assertEqual(mask_creds_in_text(line),
                         '"GET /player_api.php?username=***&password=***&action=get_live_streams HTTP/1.1"')
        # A three-segment page path is not a credential triplet and is left alone.
        line = '"GET /api/channels/14 HTTP/1.1" 200 -'
        self.assertEqual(mask_creds_in_text(line), line)

    def test_the_pair_is_masked_whatever_the_method_and_wherever_the_path_is_logged(self):
        """dev/docs/BUGS.md 2026-11-26: only a GET, HEAD or POST request line was masked, so a
        browser-based player's OPTIONS preflight, Flask's line for an unhandled error in a
        feed view and the server's line for a request it could not parse each put a working
        username and password in the log."""
        from app.url_utils import mask_creds_in_text
        for line in ('"OPTIONS /live/tv/tv-secret/14.ts HTTP/1.1" 200 -',
                     '"PUT /logo/tv/tv-secret/14 HTTP/1.1" 405 -',
                     '"GET /LIVE/tv/tv-secret/14.ts HTTP/1.1" 404 -',
                     '"GET /live/tv/tv-secret HTTP/1.1" 404 -',
                     'Exception on /live/tv/tv-secret/14.ts [GET]',
                     'Exception on /logo/tv/tv-secret/14 [GET]',
                     "code 400, message Bad request syntax ('GET /live/tv/tv-secret/14.ts')",
                     '"OPTIONS /player_api.php?username=tv&password=tv-secret HTTP/1.1" 200 -'):
            masked = mask_creds_in_text(line)
            self.assertNotIn('tv-secret', masked, line)
            self.assertIn('***', masked, line)
        # ChannelBin's own logo sits under a folder named logo and is not a login.
        for line in ('"GET /static/img/logo/channelbin-favicon/android-chrome-512x512.png HTTP/1.1" 200 -',
                     'Exception on /api/recordings/5 [POST]'):
            self.assertEqual(mask_creds_in_text(line), line)


class GateTests(unittest.TestCase):
    ENDPOINTS = ('/player_api.php', '/get.php', '/xmltv.php')

    def setUp(self):
        self.t = make_test_app(csrf=False)
        self.client = self.t.client
        self.addCleanup(self.t.cleanup)

    def _bodies(self, query):
        return {ep: self.client.get(f'{ep}?{query}') for ep in self.ENDPOINTS}

    def test_off_no_credential_and_wrong_credential_are_one_401(self):
        off = self._bodies(f'username={USER}&password={PASS}')
        self.t.sandbox_config(_feed_cfg(password=''))
        unset = self._bodies(f'username={USER}&password=')
        self.t.sandbox_config(FEED_ON)
        wrong = self._bodies(f'username={USER}&password=nope')
        missing = self._bodies('')
        for answers in (off, unset, wrong, missing):
            for ep, resp in answers.items():
                self.assertEqual(resp.status_code, 401, ep)
                self.assertEqual(resp.get_json(), {'error': feed.AUTH_FAILED_MESSAGE}, ep)
        # The stream path carries the pair, and answers the same 401.
        resp = self.client.get(f'/live/{USER}/nope/2.ts')
        self.assertEqual(resp.status_code, 401)
        self.assertEqual(resp.get_json(), {'error': feed.AUTH_FAILED_MESSAGE})

    def test_the_right_pair_answers_every_endpoint(self):
        self.t.sandbox_config(FEED_ON)
        for ep, resp in self._bodies(f'username={USER}&password={PASS}').items():
            self.assertEqual(resp.status_code, 200, ep)
        self.assertEqual(self.client.get(f'/live/{USER}/{PASS}/2.ts').status_code, 404)

    def test_a_tv_is_not_asked_to_log_in_at_the_browser_gate(self):
        from werkzeug.security import generate_password_hash
        self.t.sandbox_config({**FEED_ON, 'auth': {'enabled': True,
                                                   'password_hash': generate_password_hash('pw')}})
        from app.auth import refresh_auth
        refresh_auth(self.t.app)
        self.assertEqual(self.client.get('/settings').status_code, 302)
        self.assertEqual(
            self.client.get(f'/player_api.php?username={USER}&password={PASS}').status_code, 200)

    def test_enabling_needs_a_credential_and_the_reserve_must_fit(self):
        post = lambda path, value: self.client.post(  # noqa: E731
            '/api/settings/field', json={'path': f'integrations.output_feed.{path}', 'value': value})
        self.assertEqual(post('enabled', True).status_code, 400)
        self.assertEqual(post('username', USER).status_code, 200)
        self.assertEqual(post('password', PASS).status_code, 200)
        self.assertEqual(post('enabled', True).status_code, 200)
        self.assertTrue(load_config()['integrations']['output_feed']['enabled'])
        r = post('reserve_seconds', 60)
        self.assertEqual(r.status_code, 400)
        self.assertIn('50 seconds fits', r.get_json()['error'])
        self.assertEqual(post('mode', 'teleport').status_code, 400)
        self.assertEqual(post('mode', 'mixed').status_code, 200)
        self.assertEqual(load_config()['integrations']['output_feed']['mode'], 'mixed')
        settings = self.client.get('/settings').get_data(as_text=True)
        for value in ('proxy', 'mixed', 'redirect'):
            self.assertIn(f'<option value="{value}"', settings)
        # Ids 1 to 3; the third allows two connections at once, the others one.
        for n, limit in ((1, 1), (2, 1), (3, 2)):
            make_account(f'Acct {n}', max_connections=limit)
        db.session.commit()
        self.assertEqual(post('held_seats', ['2: 1', '3:2']).status_code, 200)
        self.assertEqual(load_config()['integrations']['output_feed']['held_seats'],
                         {'2': 1, '3': 2})
        self.assertEqual(post('held_seats', ['two']).status_code, 400)
        over = post('held_seats', ['2: 2'])
        self.assertEqual(over.status_code, 400)
        self.assertIn('allows 1 connection at once', over.get_json()['error'])
        self.assertIn('no account with id 9', post('held_seats', ['9: 1']).get_json()['error'])
        self.assertEqual(load_config()['integrations']['output_feed']['held_seats'],
                         {'2': 1, '3': 2})
        self.assertEqual(post('accounts', ['2', ' 3 ']).status_code, 200)
        self.assertEqual(load_config()['integrations']['output_feed']['accounts'], [2, 3])


# ── The catalog ────────────────────────────────────────────────────────────────────────────

class FeedIdTests(unittest.TestCase):
    def test_ids_are_decodable_and_never_collide(self):
        seen = set()
        for n in range(1, 50):
            c, g = feed.feed_id_for_channel(n), feed.feed_id_for_group(n)
            self.assertEqual(feed.decode_feed_id(c), (feed.KIND_CHANNEL, n))
            self.assertEqual(feed.decode_feed_id(g), (feed.KIND_GROUP, n))
            seen.update((c, g))
        self.assertEqual(len(seen), 98)


class CatalogTests(_FeedCase):
    def _seed(self):
        acc, other = self.account, make_account('Other')
        self.guide_a = make_channel(acc, name='Alpha', in_guide=True, category_name='News')
        self.guide_a.guide_sort_order = 3
        self.guide_b = make_channel(acc, name='Beta', in_guide=True)
        self.guide_b.guide_sort_order = 1
        self.hidden = make_channel(acc, name='Not in guide')
        self.m1 = make_channel(acc, name='Member one', category_name='Sports')
        self.m2 = make_channel(other, name='Member two', category_name='Sports')
        self.group = make_group('Game feeds', members=[self.m1, self.m2])
        self.group.guide_sort_order = 2
        self.other_guide = make_channel(other, name='Gamma', in_guide=True, category_name='News')
        self.other_guide.guide_sort_order = 4
        db.session.commit()

    def test_the_catalog_is_the_guide_in_the_guides_order(self):
        self._seed()
        rows = self.catalog()
        self.assertEqual([r['name'] for r in rows], ['Beta', 'Game feeds', 'Alpha', 'Gamma'])
        self.assertEqual([r['num'] for r in rows], [1, 2, 3, 4])
        ids = {r['name']: r['stream_id'] for r in rows}
        self.assertEqual(ids['Beta'], feed.feed_id_for_channel(self.guide_b.id))
        self.assertEqual(ids['Game feeds'], feed.feed_id_for_group(self.group.id))
        # The members are the group's failover set, never channels of their own.
        self.assertNotIn('Member one', ids)
        self.assertNotIn('Not in guide', ids)
        for r in rows:
            self.assertEqual(r['epg_channel_id'], str(r['stream_id']))
            self.assertEqual(r['direct_source'], '')
            self.assertEqual(r['stream_type'], 'live')

    def test_categories_are_the_providers_deduplicated_with_a_bucket_for_none(self):
        self._seed()
        cats = self.get('/player_api.php', action='get_live_categories').get_json()
        self.assertEqual([c['category_name'] for c in cats],
                         [feed.UNCATEGORIZED, 'Sports', 'News'])
        by_name = {c['category_name']: c['category_id'] for c in cats}
        rows = {r['name']: r for r in self.catalog()}
        self.assertEqual(rows['Alpha']['category_id'], by_name['News'])
        self.assertEqual(rows['Gamma']['category_id'], by_name['News'])
        self.assertEqual(rows['Game feeds']['category_id'], by_name['Sports'])
        self.assertEqual(rows['Beta']['category_id'], by_name[feed.UNCATEGORIZED])

    def test_the_allowlist_drops_a_channel_and_narrows_a_group(self):
        self._seed()
        self.t.sandbox_config(_feed_cfg(accounts=[self.account.id]))
        rows = {r['name']: r for r in self.catalog()}
        self.assertNotIn('Gamma', rows)
        self.assertIn('Game feeds', rows)
        play = feed.resolve_play(feed.feed_id_for_group(self.group.id))
        self.assertEqual(play.member.id, self.m1.id)
        self.t.sandbox_config(_feed_cfg(accounts=[self.m2.account_id]))
        play = feed.resolve_play(feed.feed_id_for_group(self.group.id))
        self.assertEqual(play.member.id, self.m2.id)
        self.t.sandbox_config(_feed_cfg(accounts=[9999]))
        self.assertIn('Game feeds', {r['name'] for r in self.catalog()},
                      'a group stays listed; the play is what refuses, by name')
        play = feed.resolve_play(feed.feed_id_for_group(self.group.id))
        self.assertIsNone(play.member)
        self.assertIn('not allowed to serve from', play.why_none)

    def test_a_group_plays_from_the_member_the_recorder_would_pick(self):
        self._seed()
        self.m1.health_score, self.m2.health_score = 40.0, 90.0
        db.session.commit()
        self.assertEqual(feed.resolve_play(feed.feed_id_for_group(self.group.id)).member.id,
                         self.m2.id)
        self.assertEqual(feed.resolve_play(feed.feed_id_for_group(self.group.id),
                                           exclude_ids={self.m2.id}).member.id, self.m1.id)

    def test_the_vod_and_series_actions_are_answered_empty(self):
        self._seed()
        for action in ('get_vod_categories', 'get_vod_streams', 'get_series_categories',
                       'get_series', 'something_new'):
            self.assertEqual(self.get('/player_api.php', action=action).get_json(), [], action)

    def test_the_auth_answer_names_the_request_host_and_what_our_own_client_reads(self):
        from app.accounts import check_xtream_auth, stream_origin_from_server_info
        data = self.get('/player_api.php').get_json()
        self.assertIs(check_xtream_auth(data), data)
        self.assertEqual(data['user_info']['allowed_output_formats'], ['ts'])
        self.assertEqual(stream_origin_from_server_info(data), 'http://localhost:80')
        self.assertNotIn('example.test', str(data))

    def test_the_playlist_lists_the_feeds_own_urls(self):
        self._seed()
        resp = self.get('/get.php')
        self.assertEqual(resp.mimetype, 'audio/x-mpegurl')
        body = resp.get_data(as_text=True)
        self.assertTrue(body.startswith('#EXTM3U'))
        urls = [ln for ln in body.splitlines() if ln.startswith('http')]
        self.assertEqual(len(urls), 4)
        gid = feed.feed_id_for_group(self.group.id)
        self.assertIn(f'http://localhost/live/{USER}/{PASS}/{gid}.ts', urls)
        self.assertIn(f'tvg-id="{gid}"', body)
        self.assertIn('group-title="Sports"', body)
        self.assertNotIn('example.test', body)

    def test_xmltv_carries_the_guide_under_the_feed_ids_in_utc(self):
        self._seed()
        now = datetime.utcnow()
        make_epg_entry(self.guide_a, title='A show', start_time=now - timedelta(minutes=10),
                       duration_minutes=60, sub_title='Ep 1', description='About it',
                       category='Drama', rating='TV-14')
        make_epg_entry(self.m2, title='The game', start_time=now, duration_minutes=120)
        make_epg_entry(self.m1, title='Other feed', start_time=now, duration_minutes=120)
        make_epg_entry(self.guide_a, title='Too far out',
                       start_time=now + timedelta(days=10), duration_minutes=60)
        make_epg_entry(self.hidden, title='Unlisted', start_time=now, duration_minutes=60)
        self.group.guide_listings_channel_id = self.m2.id
        db.session.commit()
        resp = self.get('/xmltv.php')
        self.assertEqual(resp.mimetype, 'application/xml')
        body = resp.get_data(as_text=True)
        a_id, g_id = feed.feed_id_for_channel(self.guide_a.id), feed.feed_id_for_group(self.group.id)
        self.assertIn(f'<channel id="{a_id}"><display-name>Alpha</display-name>', body)
        self.assertIn(f'<channel id="{g_id}"><display-name>Game feeds</display-name>', body)
        self.assertIn(f'channel="{a_id}"><title>A show</title><sub-title>Ep 1</sub-title>'
                      '<desc>About it</desc><category>Drama</category>'
                      '<rating><value>TV-14</value></rating>', body)
        self.assertIn(f'channel="{g_id}"><title>The game</title>', body)
        self.assertNotIn('Other feed', body)
        self.assertNotIn('Too far out', body)
        self.assertNotIn('Unlisted', body)
        self.assertRegex(body, r'start="\d{14} \+0000" stop="\d{14} \+0000"')

    def test_a_channel_fills_both_its_own_row_and_its_groups(self):
        self._seed()
        self.m1.in_guide = True
        make_epg_entry(self.m1, title='Shared', start_time=datetime.utcnow(), duration_minutes=60)
        db.session.commit()
        body = self.get('/xmltv.php').get_data(as_text=True)
        self.assertEqual(body.count('<title>Shared</title>'), 2)

    def test_every_channel_with_a_logo_lists_the_feeds_logo_route(self):
        """dev/docs/BUGS.md 2026-11-17 02:38 PM, IPTV output listed no logos: a channel's logo was
        listed only when the logo cache (off by default) held a copy, and then as a route the
        password gate keeps from an IPTV player. Cached or not, a channel with a logo lists
        the feed's own logo route; the provider's URL is never listed."""
        self._seed()
        self.guide_a.logo_url = 'http://provider.test/u/p/logo.png'
        self.guide_b.logo_url = 'http://provider.test/u/p/logo2.png'
        self.guide_b.logo_cache_path = 'abc.png'
        self.m1.logo_url = 'http://provider.test/u/p/m1.png'
        self.m2.logo_url = 'http://provider.test/u/p/m2.png'
        db.session.commit()
        rows = {r['name']: r for r in self.catalog()}
        base = f'http://localhost/logo/{USER}/{PASS}'
        self.assertEqual(rows['Alpha']['stream_icon'], f'{base}/{self.guide_a.id}')
        self.assertEqual(rows['Beta']['stream_icon'], f'{base}/{self.guide_b.id}')
        self.assertIn(rows['Game feeds']['stream_icon'],
                      (f'{base}/{self.m1.id}', f'{base}/{self.m2.id}'))
        self.assertEqual(rows['Gamma']['stream_icon'], '', 'no logo lists none, not a 404')
        m3u = self.get('/get.php').get_data(as_text=True)
        self.assertIn(f'tvg-logo="{base}/{self.guide_a.id}"', m3u)
        self.assertNotIn('provider.test', m3u + str(rows))


class LogoRouteTests(_FeedCase):
    """The feed's logo route (routes/output_feed.py::logo, dev/docs/BUGS.md 2026-11-17 02:38 PM, IPTV
    output listed no logos): the cached copy when there is one, else the provider's logo
    fetched through logo_cache.download_logo, behind the feed's own credentials and not the
    password gate."""

    def setUp(self):
        super().setUp()
        self.ch = make_channel(self.account, name='Alpha', in_guide=True,
                               logo_url='http://provider.test/u/p/logo.png')
        db.session.commit()

    def _get(self, channel_id, password=PASS, body=b'\x89PNG-real', content_type='image/png'):
        from unittest import mock
        from tests.support.fakes import fake_http_response
        import app.logo_cache as logo_cache
        with mock.patch.object(logo_cache.requests, 'get',
                               return_value=fake_http_response(content_type, body)) as get:
            resp = self.client.get(f'/logo/{USER}/{password}/{channel_id}')
        return resp, get

    def test_an_uncached_logo_is_fetched_from_the_provider_for_the_player(self):
        resp, get = self._get(self.ch.id)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.data, b'\x89PNG-real')
        self.assertEqual(resp.mimetype, 'image/png')
        self.assertEqual(resp.headers['X-Content-Type-Options'], 'nosniff')
        self.assertIn('max-age=', resp.headers['Cache-Control'])
        self.assertEqual(get.call_args.args[0], 'http://provider.test/u/p/logo.png')

    def test_a_cached_logo_is_served_from_the_cache_with_no_fetch(self):
        from app.logo_cache import get_logo_cache_dir
        cache_dir = get_logo_cache_dir(load_config())
        os.makedirs(cache_dir, exist_ok=True)
        with open(os.path.join(cache_dir, f'{self.ch.id}.png'), 'wb') as f:
            f.write(b'cached-bytes')
        self.ch.logo_cache_path = f'{self.ch.id}.png'
        db.session.commit()
        resp, get = self._get(self.ch.id)
        self.assertEqual((resp.status_code, resp.data), (200, b'cached-bytes'))
        get.assert_not_called()

    def test_it_answers_with_the_password_gate_on(self):
        from werkzeug.security import generate_password_hash
        self.t.app.config['AUTH'] = {'enabled': True, 'password_hash': generate_password_hash('x'),
                                     'session_timeout_minutes': 0}
        resp, _get = self._get(self.ch.id)
        self.assertEqual(resp.status_code, 200)

    def test_the_wrong_password_is_the_feeds_401_and_fetches_nothing(self):
        resp, get = self._get(self.ch.id, password='nope')
        self.assertEqual(resp.status_code, 401)
        get.assert_not_called()

    def test_a_channel_no_feed_row_could_carry_is_never_fetched(self):
        hidden = make_channel(self.account, name='Hidden', logo_url='http://provider.test/h.png')
        member = make_channel(self.account, name='Member', logo_url='http://provider.test/m.png')
        make_group('Grouped', members=[member])
        db.session.commit()
        resp, get = self._get(hidden.id)
        self.assertEqual(resp.status_code, 404)
        get.assert_not_called()
        resp, get = self._get(member.id)
        self.assertEqual(resp.status_code, 200, 'a member of an in-guide group is a feed row\'s logo')

    def test_a_logo_that_is_not_a_raster_image_is_refused(self):
        resp, _get = self._get(self.ch.id)
        self.assertEqual(resp.status_code, 200)
        resp, _get = self._get(self.ch.id, body=b'<svg/>', content_type='image/svg+xml')
        self.assertEqual(resp.status_code, 404)
        self.assertNotIn(b'<svg', resp.data)


class SelfSyncTests(_FeedCase):
    """ChannelBin's own Xtream client is the one consumer of the protocol written down in
    this tree, so syncing an account from the running feed is the cheapest proof the feed
    speaks it. Over loopback, which the network guard allows."""

    def setUp(self):
        super().setUp()
        from werkzeug.serving import make_server
        self.server = make_server('127.0.0.1', 0, self.t.app, threaded=True)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.base = f'http://127.0.0.1:{self.server.server_port}'
        self.guide = make_channel(self.account, name='Alpha', in_guide=True, category_name='News')
        m1 = make_channel(self.account, name='Member one')
        self.group = make_group('Game feeds', members=[m1])
        make_epg_entry(self.guide, title='A show', start_time=datetime.utcnow(),
                       duration_minutes=60)
        make_epg_entry(m1, title='The game', start_time=datetime.utcnow(), duration_minutes=60)
        db.session.commit()

    def test_our_own_client_reads_the_feed(self):
        from app.xtream_client import XtreamClient
        client = XtreamClient(self.base, USER, PASS, timeout=10)
        self.assertEqual(client.check_auth()['user_info']['auth'], 1)
        rows = client.get_live_streams()
        self.assertEqual({r['name'] for r in rows}, {'Alpha', 'Game feeds'})
        self.assertEqual(client.get_live_categories(), {'1': 'News', '2': feed.UNCATEGORIZED})

    def test_a_sync_from_the_feed_lands_the_guide_as_an_account(self):
        from app.accounts import _do_sync
        from app.database import Channel, EPGEntry
        mirror = XtreamAccount(name='Mirror', base_url=self.base, username=USER,
                               password=PASS, status='OK')
        db.session.add(mirror)
        db.session.commit()
        _do_sync(mirror.id, threading.Event())
        db.session.expire_all()
        synced = Channel.query.filter_by(account_id=mirror.id).order_by(Channel.name).all()
        self.assertEqual([c.name for c in synced], ['Alpha', 'Game feeds'])
        by_name = {c.name: c for c in synced}
        self.assertEqual(by_name['Alpha'].stream_id, feed.feed_id_for_channel(self.guide.id))
        self.assertEqual(by_name['Game feeds'].stream_id, feed.feed_id_for_group(self.group.id))
        self.assertTrue(by_name['Alpha'].stream_url.startswith(self.base))
        self.assertEqual(by_name['Alpha'].category_name, 'News')
        titles = {e.title for e in EPGEntry.query.filter(
            EPGEntry.channel_id.in_([c.id for c in synced])).all()}
        self.assertEqual(titles, {'A show', 'The game'})
        self.assertEqual(connlim._holders.get(self.account.id, []), [],
                         'a sync opens no stream and holds no seat')


# ── The relay ──────────────────────────────────────────────────────────────────────────────

class RingTests(unittest.TestCase):
    def test_capacity_drops_the_oldest_and_counts_what_was_lost(self):
        ring = feed.Ring(capacity=feed.READ_CHUNK * 2, reserve=0)
        for _ in range(3):
            ring.append(b'x' * feed.READ_CHUNK)
        self.assertEqual(ring.size, feed.READ_CHUNK * 2)
        self.assertEqual(ring.dropped, feed.READ_CHUNK)
        self.assertEqual(ring.total_in, feed.READ_CHUNK * 3)

    def test_a_chunk_is_released_only_once_it_has_aged_the_reserve(self):
        ring = feed.Ring(capacity=feed.READ_CHUNK * 4, reserve=0.5)
        ring.append(b'a')
        self.assertIsNone(ring.take(timeout=0.1))
        self.assertEqual(ring.take(timeout=1.0), b'a')
        self.assertIsNone(ring.take(timeout=0.05))

    def test_zero_reserve_is_pass_through(self):
        ring = feed.Ring(capacity=feed.READ_CHUNK, reserve=0)
        ring.append(b'a')
        self.assertEqual(ring.take(timeout=0.1), b'a')

    def test_a_ring_past_its_pressure_mark_releases_ahead_of_the_reserve_and_counts_it(self):
        """dev/docs/BUGS.md 2026-11-12: a provider's opening burst held for the whole reserve
        in a ring it did not fit was evicted before any of it could be taken, so the play
        ended at nothing sent. Past the pressure mark the head goes out whatever its age."""
        ring = feed.Ring(capacity=feed.READ_CHUNK * 8, reserve=100.0)
        self.assertEqual(ring.pressure, feed.READ_CHUNK * 6)
        for i in range(6):
            ring.append(bytes([i]) * feed.READ_CHUNK)
        self.assertIsNone(ring.take(timeout=0.05), 'at the mark, not past it: the reserve holds')
        ring.append(b'\x06' * feed.READ_CHUNK)
        self.assertEqual(ring.take(timeout=0.05)[0], 0, 'the oldest chunk goes first')
        self.assertEqual(ring.released_early, feed.READ_CHUNK)
        self.assertIsNone(ring.take(timeout=0.05), 'back at the mark, the reserve holds again')
        self.assertEqual((ring.dropped, ring.dropped_unripe), (0, 0))

    def test_the_default_ring_holds_a_providers_longest_burst_and_the_reserve(self):
        """29 s is the longest connect burst measured on a real account (dev/changelog/942),
        and 16 Mbps is twice the HD bitrate the reserve check assumes."""
        from app.config import config_default
        ring_bytes = config_default('integrations.output_feed.buffer_mb') * 1024 * 1024
        reserve = config_default('integrations.output_feed.reserve_seconds')
        self.assertGreaterEqual(ring_bytes, (29 + reserve) * 2 * feed.RESERVE_BYTES_PER_SECOND)

    def test_a_chunk_evicted_before_its_reserve_is_counted_apart_from_one_the_viewer_left(self):
        """A reserve the ring cannot hold at the stream's bitrate evicts chunks the viewer
        could never have taken; those must not read as the viewer being slow
        (dev/docs/BUGS.md 2026-11-01)."""
        unripe = feed.Ring(capacity=feed.READ_CHUNK * 2, reserve=100.0)
        for t in (0.0, 0.1, 0.2):
            unripe.append(b'x' * feed.READ_CHUNK, now=t)
        self.assertEqual(unripe.dropped, feed.READ_CHUNK)
        self.assertEqual(unripe.dropped_unripe, feed.READ_CHUNK)
        ripe = feed.Ring(capacity=feed.READ_CHUNK * 2, reserve=1.0)
        for t in (0.0, 0.1, 5.0):
            ripe.append(b'x' * feed.READ_CHUNK, now=t)
        self.assertEqual(ripe.dropped, feed.READ_CHUNK)
        self.assertEqual(ripe.dropped_unripe, 0)


class ReaperJudgementTests(_FeedCase):
    """_check() over sessions built by hand, with no ffmpeg: what the reaper may and may not
    conclude about a session from its state alone."""

    def _session(self, proc=None, ring=None):
        session = feed.RelaySession(
            id=f's{id(self)}{feed.live_sessions().__len__()}', feed_id=2, label='channel 1 (One)',
            client='10.0.0.2', group_id=None, channel_id=1, channel_name='One',
            account_id=self.account.id, ring=ring or feed.Ring(feed.READ_CHUNK * 2, 0),
            started_mono=time.monotonic(), proc=proc)
        with feed._lock:
            feed._sessions[session.id] = session
        return session

    def test_a_session_inside_its_launch_window_is_not_judged(self):
        """start_session() registers the session before _launch() adopts the ffmpeg, and
        launched_mono is still 0.0 then; a reaper another viewer keeps running must not read
        that as a connect timeout and kill the tune-in (dev/docs/BUGS.md 2026-11-01)."""
        session = self._session()
        self.assertIsNone(session.proc)
        feed._check(session)
        self.assertTrue(session.live)
        self.assertIsNone(session.reason)

    def test_drops_the_viewer_could_not_have_taken_name_the_buffer_not_the_viewer(self):
        """dev/docs/BUGS.md 2026-11-01: a chunk evicted before it aged the reserve was never
        releasable, so cutting the session as "the IPTV player fell behind" blamed the wrong
        party."""
        from unittest import mock
        proc = mock.Mock()
        proc.poll.return_value = None
        ring = feed.Ring(capacity=feed.READ_CHUNK * 2, reserve=100.0)
        for t in (0.0, 0.1, 0.2):
            ring.append(b'x' * feed.READ_CHUNK, now=t)
        session = self._session(proc=proc, ring=ring)
        session.launched_mono = time.monotonic()
        with mock.patch('app.output_feed.terminate_or_kill'):
            feed._check(session)
        self.assertFalse(session.live)
        self.assertEqual(session.reason, feed.REASON_BUFFER_TOO_SMALL)
        # The number is named for what it is: what was lost, beside what the buffer holds.
        self.assertIn(f'{feed.fmt_bytes(feed.READ_CHUNK)} of the stream was lost', session.detail)
        self.assertIn(f'{feed.fmt_bytes(feed.READ_CHUNK * 2)} buffer', session.detail)

    def test_a_play_that_ran_short_of_its_reserve_says_so_once_and_on_its_line(self):
        from unittest import mock
        proc = mock.Mock()
        proc.poll.return_value = None
        ring = feed.Ring(capacity=feed.READ_CHUNK * 4, reserve=100.0)
        for _ in range(4):
            ring.append(b'x' * feed.READ_CHUNK)
        self.assertIsNotNone(ring.take(timeout=0.05))
        session = self._session(proc=proc, ring=ring)
        session.launched_mono = time.monotonic()
        session.state = feed.STATE_STREAMING
        session.last_data_mono = time.monotonic()
        with self.assertLogs('app.output_feed', 'WARNING') as logs:
            feed._check(session)
            feed._check(session)
        self.assertTrue(session.live, 'less reserve than asked for is not a reason to cut')
        self.assertEqual(sum('less than its 100s reserve' in line for line in logs.output), 1)
        self.assertIn('went out sooner than 100s behind live', feed.reserve_short_note(ring))
        self.assertEqual(feed.reserve_short_note(feed.Ring(feed.READ_CHUNK, 0)), '')

    def test_drops_the_viewer_left_behind_still_name_the_viewer(self):
        from unittest import mock
        proc = mock.Mock()
        proc.poll.return_value = None
        ring = feed.Ring(capacity=feed.READ_CHUNK * 2, reserve=0)
        for _ in range(3):
            ring.append(b'x' * feed.READ_CHUNK)
        session = self._session(proc=proc, ring=ring)
        session.launched_mono = time.monotonic()
        with mock.patch('app.output_feed.terminate_or_kill'):
            feed._check(session)
        self.assertEqual(session.reason, feed.REASON_CLIENT_TOO_SLOW)


class PlayTimeByAccountTests(_FeedCase):
    """dev/changelog/1676: a play times each provider ffmpeg against the account it plays
    from, so Streamed to IPTV players credits each account its own part of a play."""

    def _session(self):
        session = feed.RelaySession(
            id=f'pt{id(self)}', feed_id=2, label='channel 1 (One)', client='10.0.0.2',
            group_id=None, channel_id=1, channel_name='One', account_id=self.account.id,
            ring=feed.Ring(feed.READ_CHUNK * 2, 0), started_mono=time.monotonic())
        with feed._lock:
            feed._sessions[session.id] = session
        return session

    def test_a_swapped_out_readers_end_leaves_the_next_ffmpegs_time_running(self):
        """The old ffmpeg's reader can drain after the next one was adopted: closing "whatever
        is running" there would cut the new account's time short."""
        session = self._session()
        old, new = object(), object()
        session._phase = (new, self.account.id, time.monotonic() - 2.0)
        with feed._lock:
            feed._close_phase_locked(session, old)
        self.assertIsNotNone(session._phase)
        self.assertEqual(session.account_seconds, {})
        with feed._lock:
            feed._close_phase_locked(session, new)
        self.assertIsNone(session._phase)
        self.assertAlmostEqual(session.account_seconds[self.account.id], 2.0, delta=0.5)

    def test_channelbins_own_clip_is_timed_against_no_account(self):
        """A waiting screen or Up next picture uses no provider connection: adopting its ffmpeg
        closes the provider ffmpeg's time and opens none."""
        session = self._session()
        session._phase = (object(), self.account.id, time.monotonic() - 3.0)
        proc = subprocess.Popen([sys.executable, '-c', 'pass'], stdin=subprocess.DEVNULL,
                                stdout=subprocess.PIPE)
        reader = feed._adopt(session, proc)
        self.assertIsNotNone(reader)
        reader.join(5)
        proc.wait(5)
        self.assertIsNone(session._phase)
        self.assertEqual(set(session.account_seconds), {self.account.id})
        self.assertAlmostEqual(session.account_seconds[self.account.id], 3.0, delta=0.5)

    def test_a_provider_ffmpeg_is_timed_until_its_pipe_ends(self):
        session = self._session()
        proc = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(1.0)'],
                                stdin=subprocess.DEVNULL, stdout=subprocess.PIPE)
        reader = feed._adopt(session, proc, account_id=self.account.id)
        self.assertEqual(session._phase[1], self.account.id)
        reader.join(5)
        proc.wait(5)
        self.assertIsNone(session._phase)
        # From launch to the pipe's end: the sleep, plus an interpreter start a busy box slows.
        self.assertGreaterEqual(session.account_seconds[self.account.id], 0.9)
        self.assertLess(session.account_seconds[self.account.id], 5.0)

    def test_the_ended_plays_line_carries_the_split_with_the_running_part_closed(self):
        """_finish closes the ffmpeg still being timed before the play's line is written: its
        reader may outlive the join, or be the thread finishing it."""
        import json
        from app.database import OutputFeedPlay
        session = self._session()
        session._phase = (object(), self.account.id, time.monotonic() - 4.0)
        session.account_seconds = {999: 10.0}
        self.assertTrue(feed._finish(session, feed.REASON_CLIENT_GONE))

        def written():
            # The writer thread may already hold the line; flush whatever is still queued.
            feed.flush_play_log()
            return _fresh_count(lambda: OutputFeedPlay.query.filter_by(kind=feed.FEED_PLAY_ENDED)) == 1

        self.assertTrue(_wait(written, 5))
        db.session.expire_all()
        row = OutputFeedPlay.query.filter_by(kind=feed.FEED_PLAY_ENDED).one()
        split = json.loads(row.account_seconds)
        self.assertEqual(split['999'], 10.0)
        self.assertAlmostEqual(split[str(self.account.id)], 4.0, delta=0.5)


class LivePickPrefersAFreeAccountTests(_FeedCase):
    """dev/changelog/1642 (A1): a play's own pick of a group's member prefers one on an account
    with a connection free, as a recording's start does; a page asking what would play is told
    the member a recording would start on with every account free."""

    def setUp(self):
        super().setUp()
        self.other = make_account('Other', max_connections=1)
        self.a = make_channel(self.account, name='Feed a')
        self.b = make_channel(self.other, name='Feed b')
        self.a.health_score, self.b.health_score = 90.0, 50.0
        self.a.in_guide = self.b.in_guide = False
        self.group = make_group('Game feeds', members=[self.a, self.b])
        self.group.in_guide = True
        db.session.commit()
        self.assertTrue(connlim.try_acquire(self.account.id, 'recording', 4242))
        self.addCleanup(connlim.release, self.account.id, 'recording', 4242)

    def test_a_group_play_picks_the_member_on_the_free_account(self):
        play = feed.resolve_play(feed.feed_id_for_group(self.group.id))
        self.assertEqual(play.member.id, self.b.id)

    def test_a_play_does_not_count_its_own_seat_against_the_member_it_is_on(self):
        connlim.release(self.account.id, 'recording', 4242)
        self.assertTrue(connlim.try_acquire(self.account.id, 'viewer', 'v1'))
        self.addCleanup(connlim.release, self.account.id, 'viewer', 'v1')
        play = feed.resolve_play(feed.feed_id_for_group(self.group.id), viewer_id='v1')
        self.assertEqual(play.member.id, self.a.id)
        self.assertEqual(feed.resolve_play(feed.feed_id_for_group(self.group.id)).member.id, self.b.id)

    def test_a_sports_plays_group_source_prefers_the_free_account_and_a_page_does_not(self):
        from types import SimpleNamespace
        source = SimpleNamespace(channel_id=self.a.id, group_id=self.group.id)
        self.assertEqual(feed._sports_member([source], set(), None, frozenset(), live=True)[0].id,
                         self.b.id)
        self.assertEqual(feed.sports_member_now([source], set(), None)[0].id, self.a.id)

    def test_the_guide_rows_member_is_not_moved_by_who_is_connected(self):
        from app.channel_groups import serving_member
        self.assertEqual(serving_member(self.group).member.id, self.a.id)


class ReasonVocabularyTests(unittest.TestCase):
    def test_every_reason_has_user_facing_text(self):
        reasons = {v for k, v in vars(feed).items() if k.startswith('REASON_') and k != 'REASON_TEXT'}
        self.assertEqual(reasons, set(feed.REASON_TEXT))
        for text in feed.REASON_TEXT.values():
            self.assertTrue(text.strip())


def _build_clips(d):
    for name, size in (('a.ts', '192x108'), ('b.ts', '256x144')):
        subprocess.run(
            ['ffmpeg', '-v', 'error', '-y', '-f', 'lavfi', '-i', f'testsrc=size={size}:rate=30',
             '-f', 'lavfi', '-i', 'sine=frequency=440:sample_rate=48000', '-t', '40',
             '-c:v', 'libx264', '-preset', 'ultrafast', '-g', '30', '-pix_fmt', 'yuv420p',
             '-c:a', 'aac', '-f', 'mpegts', os.path.join(d, name)],
            check=True, timeout=180)


@unittest.skipUnless(_HAVE_FFMPEG, 'ffmpeg not available')
class _Relay(_FeedCase):
    @classmethod
    def setUpClass(cls):
        cls.clips = clips.shared('output-feed-relay', _build_clips)

    def setUp(self):
        super().setUp()
        self.open = []
        self.addCleanup(self._close_all)

    def _close_all(self):
        for resp in self.open:
            try:
                resp.close()
            except Exception:  # noqa: BLE001 - a response already closed by the test
                pass

    def _clip_channel(self, name, clip='a.ts', account=None):
        ch = make_channel(account or self.account, name=name, pace_realtime=True)
        ch.stream_url = os.path.join(self.clips, clip)
        db.session.flush()
        return ch

    def _play(self, feed_id, expect=200):
        resp = self.client.get(f'/live/{USER}/{PASS}/{feed_id}.ts')
        self.assertEqual(resp.status_code, expect, resp.get_data(as_text=True)[:300]
                         if resp.status_code != 200 else '')
        if expect == 200:
            self.open.append(resp)
        return resp

    def _session(self):
        live = feed.live_sessions()
        self.assertEqual(len(live), 1, live)
        return live[0]

    @staticmethod
    def _read(resp, n=1):
        it = iter(resp.response)
        return [next(it) for _ in range(n)]


class RelayLifecycleTests(_Relay):
    def test_bytes_reach_the_tv_and_closing_releases_everything(self):
        ch = self._clip_channel('Paced')
        ch.in_guide = True
        db.session.commit()
        resp = self._play(feed.feed_id_for_channel(ch.id))
        self.assertEqual(resp.mimetype, 'video/mp2t')
        session = self._session()
        self.assertEqual(connlim._holders.get(self.account.id), [('viewer', session.id)])
        chunks = self._read(resp, 3)
        self.assertTrue(all(len(c) for c in chunks))
        self.assertEqual(chunks[0][0], 0x47, 'a TS sync byte leads the first chunk')
        self.assertEqual(session.state, feed.STATE_STREAMING)
        self.assertTrue(os.path.exists(session.stderr_path))
        proc = session.proc
        resp.close()
        self.assertTrue(_wait(lambda: not session.live, 5))
        self.assertEqual(session.reason, feed.REASON_CLIENT_GONE)
        self.assertIsNotNone(proc.poll(), 'ffmpeg must be dead once the IPTV player is gone')
        self.assertEqual(connlim._holders.get(self.account.id, []), [])
        self.assertFalse(os.path.exists(session.stderr_path))
        self.assertEqual(session.ring.size, 0)
        self.assertGreater(session.bytes_sent, 0)

    def test_the_stream_route_answers_404_for_an_id_the_feed_does_not_serve(self):
        ch = self._clip_channel('Off guide')
        db.session.commit()
        resp = self.client.get(f'/live/{USER}/{PASS}/{feed.feed_id_for_channel(ch.id)}.ts')
        self.assertEqual(resp.status_code, 404)
        self.assertEqual(feed.live_sessions(), [])

    def test_refused_at_the_connection_limit_naming_the_holder(self):
        ch = self._clip_channel('Paced')
        ch.in_guide = True
        db.session.commit()
        self.assertTrue(connlim.try_acquire(self.account.id, 'recording', 4242))
        self.addCleanup(connlim.release, self.account.id, 'recording', 4242)
        resp = self._play(feed.feed_id_for_channel(ch.id), expect=409)
        self.assertIn('a recording', resp.get_json()['error'])
        self.assertEqual(feed.live_sessions(), [])
        alert = Alert.query.filter_by(alert_type='VIEWER_REFUSED').one()
        self.assertIn('a recording', alert.body)

    def test_the_viewer_cap_refuses_the_next_tv_once_per_condition(self):
        self.t.sandbox_config(_feed_cfg(max_viewers=1))
        ch = self._clip_channel('Paced')
        ch.in_guide = True
        other = make_account('Other', max_connections=1)
        ch2 = self._clip_channel('Second', account=other)
        ch2.in_guide = True
        db.session.commit()
        self._play(feed.feed_id_for_channel(ch.id))
        from unittest import mock
        with mock.patch.object(feed, 'SWITCH_GRACE_SECONDS', 0.3):
            for _ in range(2):
                asked = time.monotonic()
                resp = self._play(feed.feed_id_for_channel(ch2.id), expect=503)
                self.assertIn('as many IPTV players as it allows at once (1)',
                              resp.get_json()['error'])
                self.assertIn('"IPTV players at once"', resp.get_json()['error'])
                self.assertGreaterEqual(time.monotonic() - asked, 0.3,
                                        'refused only after the wait for a channel change')
        self.assertEqual(Alert.query.filter_by(alert_type='VIEWER_REFUSED').count(), 1)
        self.assertEqual(len(feed.live_sessions()), 1)

    def _change_channel(self, first, second):
        """Play `first`, ask for `second` while it is still live, and close `first` half a
        second later: the order an IPTV player changes channel in. Returns the old session."""
        old_resp = self._play(feed.feed_id_for_channel(first.id))
        old = self._session()
        self._read(old_resp)
        closer = threading.Timer(0.5, old_resp.close)
        closer.start()
        self.addCleanup(closer.cancel)
        asked = time.monotonic()
        self._play(feed.feed_id_for_channel(second.id))
        waited = time.monotonic() - asked
        self.assertGreaterEqual(waited, 0.4, 'the new play can only have started after the old one ended')
        self.assertLess(waited, feed.SWITCH_GRACE_SECONDS)
        return old

    def _assert_changed_without_a_refusal(self, old, second):
        # _finish releases the seat before it marks the session STOPPED, so the new play can
        # start while the old body's thread is still between the two; its teardown lock is
        # held across both (dev/docs/BUGS.md 2026-11-18 @ 09:25:19 AM).
        with old._teardown_lock:
            pass
        self.assertEqual((old.live, old.reason), (False, feed.REASON_CLIENT_GONE))
        new = self._session()
        self.assertEqual(new.channel_id, second.id)
        self.assertEqual(Alert.query.filter_by(alert_type='VIEWER_REFUSED').count(), 0)
        self.assertEqual(feed.recent_refused(), [])

    def test_a_channel_change_at_the_viewer_cap_waits_for_the_old_play_and_is_not_refused(self):
        """dev/docs/BUGS.md 2026-11-12: the player asks for the new channel before the old
        connection is seen to close, and at a cap of 1 every change of channel was refused
        three or four times first."""
        self.t.sandbox_config(_feed_cfg(max_viewers=1))
        ch = self._clip_channel('Paced')
        ch.in_guide = True
        other = make_account('Other', max_connections=1)
        ch2 = self._clip_channel('Second', account=other)
        ch2.in_guide = True
        db.session.commit()
        old = self._change_channel(ch, ch2)
        self._assert_changed_without_a_refusal(old, ch2)

    def test_a_channel_change_on_a_one_connection_account_waits_for_the_seat(self):
        """The same race one step later: under the viewer cap, with both channels on an
        account that allows one connection, the old play still holds the seat."""
        self.t.sandbox_config(_feed_cfg(max_viewers=3))
        ch = self._clip_channel('Paced')
        ch.in_guide = True
        ch2 = self._clip_channel('Second')
        ch2.in_guide = True
        db.session.commit()
        old = self._change_channel(ch, ch2)
        self._assert_changed_without_a_refusal(old, ch2)
        self.assertEqual(connlim._holders.get(self.account.id), [('viewer', self._session().id)])

    def test_a_recording_preempts_the_viewer_and_both_surfaces_say_so(self):
        from app.recorder import _try_acquire_slot_with_preemption
        ch = self._clip_channel('Paced')
        ch.in_guide = True
        rec = make_recording(status='SCHEDULED', name='The big game', channel_id=ch.id)
        db.session.commit()
        resp = self._play(feed.feed_id_for_channel(ch.id))
        self._read(resp, 1)
        session = self._session()
        proc = session.proc
        self.assertTrue(_try_acquire_slot_with_preemption(self.t.app, rec.id, self.account.id))
        self.addCleanup(connlim.release, self.account.id, 'recording', rec.id)
        self.assertFalse(session.live)
        self.assertEqual(session.reason, feed.REASON_PREEMPTED)
        self.assertIn('recording', session.reason_text())
        self.assertIsNotNone(proc.poll())
        self.assertEqual(connlim._holders.get(self.account.id), [('recording', rec.id)])
        alert = Alert.query.filter_by(alert_type='VIEWER_PREEMPTED').one()
        self.assertIn('The big game', alert.title)
        self.assertEqual(alert.recording_id, rec.id)
        event = RecordingEvent.query.filter_by(recording_id=rec.id, event_type=DIAGNOSTICS).one()
        self.assertIn('viewer_preempted', event.extra_data)
        self.assertIn('Paced', event.detail)
        # The body ends on its next turn rather than hanging on a dead ring.
        with self.assertRaises(StopIteration):
            it = iter(resp.response)
            while True:
                next(it)

    def test_preempted_inside_the_launch_window_never_runs_seatless(self):
        from unittest import mock
        ch = self._clip_channel('Paced')
        ch.in_guide = True
        rec = make_recording(status='SCHEDULED', name='Window', channel_id=ch.id)
        db.session.commit()
        real_popen = subprocess.Popen
        launched = []

        def popen_then_preempt(*args, **kwargs):
            proc = real_popen(*args, **kwargs)
            launched.append(proc)
            connlim.preempt_viewers_for_slot(self.account.id)
            feed.preempt_for_account(self.t.app, self.account.id, rec.id)
            return proc

        with mock.patch('app.output_feed.subprocess.Popen', side_effect=popen_then_preempt):
            resp = self.client.get(f'/live/{USER}/{PASS}/{feed.feed_id_for_channel(ch.id)}.ts')
        self.assertEqual(resp.status_code, 409)
        self.assertEqual(feed.live_sessions(), [])
        ended = feed.recent_ended()
        self.assertEqual(ended[0].reason, feed.REASON_PREEMPTED)
        # The ffmpeg that launched on the stripped seat was never adopted and is dead.
        self.assertIsNone(ended[0].proc)
        self.assertEqual(len(launched), 1)
        self.assertIsNotNone(launched[0].poll())
        self.assertFalse(os.path.exists(ended[0].stderr_path))

    def test_a_tv_that_stops_reading_is_cut_as_idle(self):
        self.t.sandbox_config(_feed_cfg(idle_seconds=1))
        ch = self._clip_channel('Paced')
        ch.in_guide = True
        db.session.commit()
        resp = self._play(feed.feed_id_for_channel(ch.id))
        self._read(resp, 1)
        session = self._session()
        self.assertTrue(_wait(lambda: not session.live, 10))
        self.assertEqual(session.reason, feed.REASON_IDLE)
        self.assertEqual(connlim._holders.get(self.account.id, []), [])

    def test_ffmpeg_ending_on_its_own_is_named_with_its_exit_code(self):
        ch = self._clip_channel('Paced')
        ch.in_guide = True
        db.session.commit()
        resp = self._play(feed.feed_id_for_channel(ch.id))
        self._read(resp, 1)
        session = self._session()
        session.proc.kill()
        self.assertTrue(_wait(lambda: not session.live, 10))
        self.assertEqual(session.reason, feed.REASON_FFMPEG_EXITED)
        self.assertIn('exited with code', session.detail)

    def test_a_source_ffmpeg_cannot_open_is_named_from_its_stderr(self):
        ch = self._clip_channel('Broken')
        ch.in_guide = True
        ch.stream_url = os.path.join(self.clips, 'missing.ts')
        db.session.commit()
        resp = self._play(feed.feed_id_for_channel(ch.id))
        # ffmpeg dies at open, so the reaper may have ended the session before this reads it.
        self.assertTrue(_wait(lambda: feed.recent_ended(), 10))
        session = feed.recent_ended()[0]
        self.assertEqual(session.reason, feed.REASON_FFMPEG_EXITED)
        self.assertIn('missing.ts', session.detail)
        resp.close()

    def test_shutdown_and_disabling_kill_the_live_viewer_by_name(self):
        ch = self._clip_channel('Paced')
        ch.in_guide = True
        db.session.commit()
        resp = self._play(feed.feed_id_for_channel(ch.id))
        self._read(resp, 1)
        session = self._session()
        self.client.post('/api/settings/field',
                         json={'path': 'integrations.output_feed.enabled', 'value': False})
        self.assertFalse(session.live)
        self.assertEqual(session.reason, feed.REASON_DISABLED)
        self.t.sandbox_config(FEED_ON)
        resp = self._play(feed.feed_id_for_channel(ch.id))
        self._read(resp, 1)
        session = self._session()
        feed.kill_all_viewers()
        self.assertFalse(session.live)
        self.assertEqual(session.reason, feed.REASON_SHUTDOWN)
        self.assertIsNotNone(session.proc.poll())


class RelayFailoverTests(_Relay):
    def _group(self, *clip_names):
        members = [self._clip_channel(f'Feed {n}', clip=n) for n in clip_names]
        group = make_group('Game feeds', members=members)
        db.session.commit()
        return group, members

    def test_a_dead_member_is_replaced_inside_the_same_response(self):
        other = make_account('Other', max_connections=1)
        a = self._clip_channel('Feed a', clip='a.ts')
        b = self._clip_channel('Feed b', clip='b.ts', account=other)
        a.health_score, b.health_score = 90.0, 50.0
        group = make_group('Game feeds', members=[a, b])
        db.session.commit()
        resp = self._play(feed.feed_id_for_group(group.id))
        self._read(resp, 1)
        session = self._session()
        self.assertEqual(session.channel_id, a.id)
        first_proc = session.proc
        first_proc.kill()
        # The failover record is written after the new member's launch, so it is what says
        # the switch is complete - channel_id alone moves a moment earlier.
        self.assertTrue(_wait(lambda: bool(session.failovers) and session.channel_id == b.id, 15),
                        session)
        self.assertTrue(session.live)
        self.assertEqual(session.account_id, other.id)
        self.assertEqual(connlim._holders.get(other.id), [('viewer', session.id)])
        self.assertEqual(connlim._holders.get(self.account.id, []), [])
        self.assertEqual([f[:2] for f in session.failovers], [('Feed a', 'Feed b')])
        # Bytes keep flowing from the new member on the same response.
        before = session.bytes_sent
        self._read(resp, 2)
        self.assertGreater(session.bytes_sent, before)
        # Written after the move is recorded on the session, by the play's own thread.
        self.assertTrue(_wait(lambda: _fresh_count(lambda: ChannelGroupEvent.query.filter_by(
            group_id=group.id, event_type=GROUP_VIEWER_FAILOVER)) == 1, 5))
        event = ChannelGroupEvent.query.filter_by(group_id=group.id,
                                                  event_type=GROUP_VIEWER_FAILOVER).one()
        self.assertEqual(event.channel_id, b.id)
        self.assertIn('"Feed a"', event.detail)
        self.assertIn('"Feed b"', event.detail)
        self.assertEqual(session.failed_ids, {a.id})

    def test_a_group_tune_in_tries_the_next_member_when_the_best_ones_account_is_full(self):
        """dev/docs/BUGS.md 2026-11-16 (the tune-in walk): a group's play was refused 409 on the
        best member's account with a member on another account free. The pick prefers the free
        account itself since dev/changelog/1642, so the walk is what catches the account that
        filled between the pick and the seat: the pick here is told nothing about seats."""
        from unittest import mock
        other = make_account('Other', max_connections=1)
        a = self._clip_channel('Feed a', clip='a.ts')
        b = self._clip_channel('Feed b', clip='b.ts', account=other)
        a.health_score, b.health_score = 90.0, 50.0
        group = make_group('Game feeds', members=[a, b])
        db.session.commit()
        self.assertTrue(connlim.try_acquire(self.account.id, 'recording', 4242))
        self.addCleanup(connlim.release, self.account.id, 'recording', 4242)
        with mock.patch.object(feed, '_viewer_standing', return_value={}):
            resp = self._play(feed.feed_id_for_group(group.id))
        self._read(resp, 1)
        session = self._session()
        self.assertEqual(session.channel_id, b.id)
        self.assertEqual(session.account_id, other.id)
        self.assertEqual(connlim._holders.get(other.id), [('viewer', session.id)])
        self.assertEqual(session.failovers, [], 'starting lower down is not a move')
        self.assertIn('Started on "Feed b"', feed.play_notes(session))
        self.assertEqual(Alert.query.filter_by(alert_type='VIEWER_REFUSED').count(), 0)

    def test_a_walked_play_is_not_ended_for_a_recording_on_the_account_it_left(self):
        """A recording starting on the best member's account while the play waits there flags
        every seatless play on that account as preempted (preempt_for_account). The play then
        starts on another account, where nothing asked for its seat."""
        from unittest import mock
        other = make_account('Other', max_connections=1)
        a = self._clip_channel('Feed a', clip='a.ts')
        b = self._clip_channel('Feed b', clip='b.ts', account=other)
        a.health_score, b.health_score = 90.0, 50.0
        group = make_group('Game feeds', members=[a, b])
        db.session.commit()
        self.assertTrue(connlim.try_acquire(self.account.id, 'recording', 4242))
        self.addCleanup(connlim.release, self.account.id, 'recording', 4242)

        def refused_and_flagged(session, member, first, deadline=0.0):
            session.preempted, session.preempted_by = True, 4242
            return False

        with mock.patch.object(feed, '_take_seat', refused_and_flagged), \
                mock.patch.object(feed, '_viewer_standing', return_value={}):
            resp = self._play(feed.feed_id_for_group(group.id))
        self._read(resp, 1)
        session = self._session()
        self.assertEqual((session.channel_id, session.preempted), (b.id, False))

    def _two_account_group(self):
        other = make_account('Other', max_connections=1)
        a = self._clip_channel('Feed a', clip='a.ts')
        b = self._clip_channel('Feed b', clip='b.ts', account=other)
        a.health_score, b.health_score = 90.0, 50.0
        group = make_group('Game feeds', members=[a, b])
        rec = make_recording(status='SCHEDULED', name='The big game', channel_id=a.id)
        db.session.commit()
        return other, a, b, group, rec

    def test_a_group_tune_in_starts_on_the_member_whose_account_is_free(self):
        """dev/changelog/1642 (A1): the pick itself prefers a member on an account with a
        connection free, as a recording's start does, so the play starts there without being
        refused a seat first."""
        from unittest import mock
        other, a, b, group, _rec = self._two_account_group()
        self.assertTrue(connlim.try_acquire(self.account.id, 'recording', 4242))
        self.addCleanup(connlim.release, self.account.id, 'recording', 4242)
        with mock.patch.object(feed, '_seat_for', wraps=feed._seat_for) as walk:
            resp = self._play(feed.feed_id_for_group(group.id))
        self._read(resp, 1)
        session = self._session()
        self.assertEqual((session.channel_id, session.account_id), (b.id, other.id))
        walk.assert_not_called()
        self.assertEqual(feed.play_notes(session), '', 'no seat was refused on the way')

    def _preempt(self, rec):
        from app.recorder import _try_acquire_slot_with_preemption
        self.assertTrue(_try_acquire_slot_with_preemption(self.t.app, rec.id, self.account.id))
        self.addCleanup(connlim.release, self.account.id, 'recording', rec.id)

    def test_a_recording_moves_a_group_play_to_a_member_on_a_free_account(self):
        """dev/changelog/1642 (A3): the recording takes the connection, and the play moves to
        the group's member on the idle account in the same response instead of ending, saying
        why on the play, the group's timeline and the recording's."""
        other, a, b, group, rec = self._two_account_group()
        resp = self._play(feed.feed_id_for_group(group.id))
        self._read(resp, 1)
        session = self._session()
        self.assertEqual(session.channel_id, a.id)
        old_proc = session.proc
        self._preempt(rec)
        self.assertIsNotNone(old_proc.poll(), 'the old connection is closed before the recording connects')
        self.assertTrue(_wait(lambda: bool(session.failovers), 15), session)
        self.assertTrue(session.live)
        self.assertEqual((session.channel_id, session.account_id), (b.id, other.id))
        self.assertEqual(connlim._holders.get(other.id), [('viewer', session.id)])
        self.assertEqual(connlim._holders.get(self.account.id), [('recording', rec.id)])
        self.assertEqual(session.failovers, [('Feed a', 'Feed b', feed.REASON_MOVED_FOR_RECORDING)])
        self.assertIn('a recording needed the connection', feed.move_text(*session.failovers[0]))
        before = session.bytes_sent
        self._read(resp, 2)
        self.assertGreater(session.bytes_sent, before)
        self.assertTrue(_wait(lambda: _fresh_count(lambda: RecordingEvent.query.filter_by(
            recording_id=rec.id, event_type=DIAGNOSTICS)) == 1, 5))
        self.assertEqual(Alert.query.filter_by(alert_type='VIEWER_PREEMPTED').count(), 0,
                         'the play kept playing')
        event = RecordingEvent.query.filter_by(recording_id=rec.id, event_type=DIAGNOSTICS).one()
        self.assertIn('moved to "Feed b"', event.detail)
        group_event = ChannelGroupEvent.query.filter_by(group_id=group.id,
                                                        event_type=GROUP_VIEWER_FAILOVER).one()
        self.assertIn('needed the connection', group_event.detail)
        self.assertEqual(group_event.channel_id, b.id)

    def test_a_moved_play_credits_each_account_its_own_part(self):
        """dev/changelog/1676: a play's log row kept only the account it ended on, so the time
        it spent on the first account would have gone to the second. Each ffmpeg's run is
        credited to its own account, and the parts add up to no more than the play."""
        import json
        from app.database import OutputFeedPlay
        other, a, b, group, rec = self._two_account_group()
        resp = self._play(feed.feed_id_for_group(group.id))
        self._read(resp, 3)
        session = self._session()
        # A second on each account: ffmpeg's opening burst can deliver the first chunks in
        # milliseconds, and a part that short rounds to 0.0 on the play's line.
        time.sleep(1.0)
        self._preempt(rec)
        self.assertTrue(_wait(lambda: bool(session.failovers), 15), session)
        self._read(resp, 3)
        time.sleep(1.0)
        resp.close()
        self.assertTrue(_wait(lambda: not session.live, 5))

        def written():
            # The writer thread may already hold the line; flush whatever is still queued.
            feed.flush_play_log()
            return _fresh_count(lambda: OutputFeedPlay.query.filter_by(kind=feed.FEED_PLAY_ENDED)) == 1

        self.assertTrue(_wait(written, 5))
        db.session.expire_all()
        row = OutputFeedPlay.query.filter_by(kind=feed.FEED_PLAY_ENDED).one()
        split = {int(k): v for k, v in json.loads(row.account_seconds).items()}
        self.assertEqual(set(split), {self.account.id, other.id}, row.account_seconds)
        self.assertTrue(all(v >= 0.8 for v in split.values()), split)
        self.assertLessEqual(sum(split.values()), row.seconds + 1.0)

    def test_the_recording_holds_its_seat_before_the_move_asks_for_one(self):
        """dev/changelog/1642: the move starts only once the recording's acquire is done, so it
        can never take back the seat being freed for the recording."""
        from unittest import mock
        other, a, b, group, rec = self._two_account_group()
        self._read(self._play(feed.feed_id_for_group(group.id)), 1)
        session = self._session()
        seen = []
        real = feed._seat_for

        def asked(*args, **kwargs):
            seen.append(list(connlim._holders.get(self.account.id, [])))
            return real(*args, **kwargs)

        with mock.patch.object(feed, '_seat_for', asked):
            self._preempt(rec)
            self.assertTrue(_wait(lambda: bool(session.failovers), 15), session)
            self.assertTrue(_wait(lambda: _fresh_count(lambda: RecordingEvent.query.filter_by(
                recording_id=rec.id, event_type=DIAGNOSTICS)) == 1, 5))
        self.assertEqual(seen[0], [('recording', rec.id)])

    def test_a_move_whose_target_fills_first_ends_by_name_and_releases_everything(self):
        """The free account fills between the move's pick and its seat (the check is advisory,
        dev/changelog/855): the play ends preempted, with the alert, no ffmpeg and no seat."""
        from unittest import mock
        other, a, b, group, rec = self._two_account_group()
        resp = self._play(feed.feed_id_for_group(group.id))
        self._read(resp, 1)
        session = self._session()
        old_proc = session.proc
        real = feed.resolve_play

        def then_fill(*args, **kwargs):
            play = real(*args, **kwargs)
            if not connlim.seated_holder_ids('recording') - {rec.id}:
                connlim.try_acquire(other.id, 'recording', 4343)
            return play

        self.addCleanup(connlim.release, other.id, 'recording', 4343)
        with mock.patch.object(feed, 'resolve_play', then_fill):
            self._preempt(rec)
            self.assertTrue(_wait(lambda: not session.live, 15), session)
        self.assertEqual(session.reason, feed.REASON_PREEMPTED)
        self.assertIsNotNone(old_proc.poll())
        self.assertIsNone(session.proc)
        self.assertFalse(os.path.exists(session.stderr_path))
        self.assertNotIn(session.id, connlim.seated_holder_ids('viewer'))
        self.assertTrue(_wait(lambda: _fresh_count(
            lambda: Alert.query.filter_by(alert_type='VIEWER_PREEMPTED')) == 1, 5))

    def test_a_second_recording_does_not_blame_a_play_that_is_moving(self):
        """A play between channels holds no seat on the account it left, so a second recording
        on that account takes nothing from it and says nothing about it."""
        other, a, b, group, rec = self._two_account_group()
        self._read(self._play(feed.feed_id_for_group(group.id)), 1)
        session = self._session()
        with feed._lock:
            session._moving = True
        try:
            self.assertFalse(feed.preempt_for_account(self.t.app, self.account.id, 4545, movers=[]))
        finally:
            with feed._lock:
                session._moving = False
        self.assertFalse(session.preempted)

    def test_a_timeline_write_that_fails_never_ends_a_play_that_moved(self):
        from unittest import mock
        from sqlalchemy.exc import OperationalError
        other, a, b, group, rec = self._two_account_group()
        resp = self._play(feed.feed_id_for_group(group.id))
        self._read(resp, 1)
        session = self._session()
        boom = OperationalError('INSERT', {}, Exception('disk I/O error'))
        with mock.patch('app.db_utils.retry_on_locked', side_effect=lambda *a, **k: (lambda fn: fn)), \
                mock.patch.object(db.session, 'commit', side_effect=boom):
            self._preempt_quiet(rec)
            self.assertTrue(_wait(lambda: bool(session.failovers), 15), session)
            time.sleep(0.5)
        self.assertTrue(session.live, session)
        self.assertEqual(session.channel_id, b.id)

    def _preempt_quiet(self, rec):
        from app.recorder import _try_acquire_slot_with_preemption
        self.assertTrue(_try_acquire_slot_with_preemption(self.t.app, rec.id, self.account.id))
        self.addCleanup(connlim.release, self.account.id, 'recording', rec.id)

    def test_a_player_that_leaves_mid_move_is_still_on_the_recordings_timeline(self):
        """The recording cut it as surely as if it had ended there: the recording's own page
        says what it took, with no alert for a play the recording did not end."""
        from unittest import mock
        other, a, b, group, rec = self._two_account_group()
        resp = self._play(feed.feed_id_for_group(group.id))
        self._read(resp, 1)
        session = self._session()
        real = feed.resolve_play

        def player_left(*args, **kwargs):
            if kwargs.get('viewer_id') == session.id:
                resp.close()
                _wait(lambda: not session.live, 5)
            return real(*args, **kwargs)

        with mock.patch.object(feed, 'resolve_play', player_left):
            self._preempt(rec)
            self.assertTrue(_wait(lambda: _fresh_count(lambda: RecordingEvent.query.filter_by(
                recording_id=rec.id, event_type=DIAGNOSTICS)) == 1, 10))
        self.assertEqual(session.reason, feed.REASON_CLIENT_GONE)
        self.assertEqual(Alert.query.filter_by(alert_type='VIEWER_PREEMPTED').count(), 0)

    def test_a_group_with_every_account_full_is_still_refused_naming_the_best_member(self):
        other = make_account('Other', max_connections=1)
        a = self._clip_channel('Feed a', clip='a.ts')
        b = self._clip_channel('Feed b', clip='b.ts', account=other)
        a.health_score, b.health_score = 90.0, 50.0
        group = make_group('Game feeds', members=[a, b])
        db.session.commit()
        for n, acct in enumerate((self.account, other)):
            self.assertTrue(connlim.try_acquire(acct.id, 'recording', 4242 + n))
            self.addCleanup(connlim.release, acct.id, 'recording', 4242 + n)
        resp = self._play(feed.feed_id_for_group(group.id), expect=409)
        self.assertIn('connection limit', resp.get_json()['error'])
        self.assertEqual(feed.live_sessions(), [])
        self.assertEqual(connlim._holders.get(other.id), [('recording', 4243)])

    def test_a_group_with_nothing_left_ends_by_name(self):
        group, (a,) = self._group('a.ts')
        resp = self._play(feed.feed_id_for_group(group.id))
        self._read(resp, 1)
        session = self._session()
        session.proc.kill()
        self.assertTrue(_wait(lambda: not session.live, 15))
        self.assertEqual(session.reason, feed.REASON_NO_MEMBER)
        self.assertIn('"Feed a.ts"', session.detail)
        self.assertEqual(connlim._holders.get(self.account.id, []), [])

    def test_a_group_with_no_playable_member_is_refused_with_the_alert(self):
        a = self._clip_channel('Feed a')
        group = make_group('Game feeds', members=[a], recording=False)
        db.session.commit()
        resp = self._play(feed.feed_id_for_group(group.id), expect=409)
        self.assertIn('Recording on', resp.get_json()['error'])
        alert = Alert.query.filter_by(alert_type='FEED_NO_MEMBER').one()
        self.assertIn('Game feeds', alert.title)


class HandMoveTests(_Relay):
    """Watching now's "Move to another channel" (output_feed.move_by_hand, dev/changelog/1675):
    a play moves off the channel it is on at a person's word, never comes back to it, and the
    channel it left is passed over by everything else until its score adjustment is set back.
    When there is nowhere to go the play is left exactly as it was."""

    def _group(self, accounts=None):
        other = make_account('Other', max_connections=1)
        a = self._clip_channel('Feed a', clip='a.ts')
        b = self._clip_channel('Feed b', clip='b.ts', account=accounts or other)
        a.health_score, b.health_score = 90.0, 50.0
        group = make_group('Game feeds', members=[a, b])
        db.session.commit()
        return other, a, b, group

    def _move(self, session, expect=200):
        resp = self.client.post(f'/api/iptv-output/plays/{session.id}/move')
        self.assertEqual(resp.status_code, expect, resp.get_json())
        return resp.get_json()

    def test_a_move_puts_the_play_on_the_next_channel_and_marks_the_one_it_left(self):
        from app.database import Channel, ChannelEvent, CHANNEL_HEALTH_OVERRIDE_CHANGED
        other, a, b, group = self._group()
        resp = self._play(feed.feed_id_for_group(group.id))
        self._read(resp, 1)
        session = self._session()
        self.assertEqual(session.channel_id, a.id)
        old_proc = session.proc
        body = self._move(session)
        self.assertTrue(body['success'])
        self.assertEqual(body['moved_to'], 'Feed b')
        self.assertIn('score adjustment of -100, so it is tried last', body['message'])
        self.assertTrue(body['adjusted'])
        self.assertIsNotNone(old_proc.poll(), 'the channel it left is stopped')
        self.assertTrue(session.live)
        self.assertEqual((session.channel_id, session.account_id), (b.id, other.id))
        self.assertEqual(connlim._holders.get(other.id), [('viewer', session.id)])
        self.assertEqual(connlim._holders.get(self.account.id, []), [])
        self.assertEqual(session.failovers, [('Feed a', 'Feed b', feed.REASON_MOVED_BY_HAND)])
        self.assertEqual(feed.move_text(*session.failovers[0]),
                         'Moved from "Feed a" to "Feed b" by hand, from Watching now.')
        self.assertIn(a.id, session.failed_ids)
        before = session.bytes_sent
        self._read(resp, 2)
        self.assertGreater(session.bytes_sent, before, 'the same response plays on')
        db.session.expire_all()
        left = db.session.get(Channel, a.id)
        self.assertEqual(left.manual_health_adjustment, feed.HAND_MOVE_ADJUSTMENT)
        self.assertEqual(left.manual_health_note, feed.HAND_MOVE_NOTE)
        event = ChannelEvent.query.filter_by(channel_id=a.id,
                                             event_type=CHANNEL_HEALTH_OVERRIDE_CHANGED).one()
        self.assertIn('-100', event.detail)
        self.assertIn('moved off it by hand', event.detail)
        self.assertEqual(db.session.get(Channel, b.id).manual_health_adjustment, 0)
        group_event = ChannelGroupEvent.query.filter_by(group_id=group.id,
                                                        event_type=GROUP_VIEWER_FAILOVER).one()
        self.assertIn('by hand', group_event.detail)

    def test_the_next_play_of_the_group_starts_on_the_channel_it_did_not_leave(self):
        _other, a, b, group = self._group()
        resp = self._play(feed.feed_id_for_group(group.id))
        self._read(resp, 1)
        self._move(self._session())
        resp.close()
        self.assertTrue(_wait(lambda: not feed.live_sessions(), 5))
        resp = self._play(feed.feed_id_for_group(group.id))
        self._read(resp, 1)
        self.assertEqual(self._session().channel_id, b.id,
                         'the -100 puts the channel it left below one scored 50')

    def test_a_second_move_never_returns_to_the_channel_it_left(self):
        _other, a, b, group = self._group()
        resp = self._play(feed.feed_id_for_group(group.id))
        self._read(resp, 1)
        session = self._session()
        self._move(session)
        proc = session.proc
        body = self._move(session, expect=409)
        self.assertIn('No other channel', body['error'])
        self.assertIn('stays on "Feed b"', body['error'])
        self.assertIs(session.proc, proc)
        self.assertEqual(session.channel_id, b.id)
        self.assertEqual(len(session.failovers), 1)

    def test_with_nowhere_to_go_the_play_and_the_channel_are_left_alone(self):
        from app.database import Channel
        other, a, b, group = self._group()
        self.assertTrue(connlim.try_acquire(other.id, 'recording', 4242))
        self.addCleanup(connlim.release, other.id, 'recording', 4242)
        resp = self._play(feed.feed_id_for_group(group.id))
        self._read(resp, 1)
        session = self._session()
        proc = session.proc
        body = self._move(session, expect=409)
        self.assertIn('"Feed b"', body['error'])
        self.assertIn('no connection free', body['error'])
        self.assertIsNone(proc.poll(), 'nothing was stopped')
        self.assertIs(session.proc, proc)
        self.assertEqual((session.channel_id, session.failovers, session.failed_ids), (a.id, [], set()))
        self.assertFalse(session._failing_over)
        db.session.expire_all()
        self.assertEqual(db.session.get(Channel, a.id).manual_health_adjustment, 0)
        before = session.bytes_sent
        self._read(resp, 2)
        self.assertGreater(session.bytes_sent, before)

    def test_a_move_goes_past_a_channel_on_a_full_account_to_one_with_a_connection_free(self):
        """The pick's own preference for a free account (resolve_play, dev/changelog/1642) is
        what passes "Feed b" over; the move must not refuse on the better-ranked full one."""
        other = make_account('Other', max_connections=1)
        third = make_account('Third', max_connections=1)
        a = self._clip_channel('Feed a', clip='a.ts')
        b = self._clip_channel('Feed b', clip='b.ts', account=other)
        c = self._clip_channel('Feed c', clip='b.ts', account=third)
        a.health_score, b.health_score, c.health_score = 90.0, 50.0, 30.0
        group = make_group('Game feeds', members=[a, b, c])
        db.session.commit()
        self.assertTrue(connlim.try_acquire(other.id, 'recording', 4242))
        self.addCleanup(connlim.release, other.id, 'recording', 4242)
        resp = self._play(feed.feed_id_for_group(group.id))
        self._read(resp, 1)
        session = self._session()
        self.assertEqual(session.channel_id, a.id)
        body = self._move(session)
        self.assertEqual(body['moved_to'], 'Feed c')
        self.assertEqual((session.channel_id, session.account_id), (c.id, third.id))
        self.assertEqual(connlim._holders.get(third.id), [('viewer', session.id)])

    def test_an_error_after_the_old_channel_stopped_ends_the_play_by_name(self):
        from unittest import mock
        other, a, b, group = self._group()
        resp = self._play(feed.feed_id_for_group(group.id))
        self._read(resp, 1)
        session = self._session()
        old_proc = session.proc
        with mock.patch.object(feed, '_launch', side_effect=RuntimeError('boom')):
            body = self._move(session, expect=409)
        self.assertIn('Nothing else would start, so the play ended.', body['error'])
        self.assertIn('boom', body['error'])
        self.assertIsNotNone(old_proc.poll())
        self.assertFalse(session.live, 'a play with no ffmpeg is never reaped, so it is ended here')
        self.assertEqual(session.reason, feed.REASON_LAUNCH_FAILED)
        self.assertEqual(connlim._holders.get(self.account.id, []), [])
        self.assertFalse(session._failing_over)
        self.assertFalse(session._swap_lock.locked())

    def test_a_move_on_the_same_account_keeps_its_seat(self):
        self.account.max_connections = 1
        _other, a, b, group = self._group(accounts=self.account)
        resp = self._play(feed.feed_id_for_group(group.id))
        self._read(resp, 1)
        session = self._session()
        self._move(session)
        self.assertEqual((session.channel_id, session.account_id), (b.id, self.account.id))
        self.assertEqual(connlim._holders.get(self.account.id), [('viewer', session.id)])

    def test_a_single_channel_says_there_is_nowhere_to_go(self):
        from app.database import Channel
        ch = self._clip_channel('Paced')
        ch.in_guide = True
        db.session.commit()
        resp = self._play(feed.feed_id_for_channel(ch.id))
        self._read(resp, 1)
        session = self._session()
        self.assertEqual(feed.move_offer(session), feed.MOVE_NOWHERE)
        self.assertEqual(session.to_dict()['move_why'], feed.MOVE_NOWHERE_TEXT)
        proc = session.proc
        body = self._move(session, expect=409)
        self.assertIn('single channel', body['error'])
        self.assertIs(session.proc, proc)
        db.session.expire_all()
        self.assertEqual(db.session.get(Channel, ch.id).manual_health_adjustment, 0)


class MoveOfferTests(_FeedCase):
    """What Watching now offers a play built by hand, with no ffmpeg (dev/changelog/1675)."""

    def _session(self, **kw):
        fields = dict(id=f'm{time.monotonic_ns()}', feed_id=2, label='group 1 (Game)',
                      client='10.0.0.2', group_id=1, channel_id=1, channel_name='One',
                      account_id=self.account.id, ring=feed.Ring(1024, 0),
                      started_mono=time.monotonic(), fails_over=True,
                      state=feed.STATE_STREAMING)
        fields.update(kw)
        session = feed.RelaySession(**fields)
        with feed._lock:
            feed._sessions[session.id] = session
        return session

    def test_each_kind_of_play_gets_its_own_answer(self):
        self.assertEqual(feed.move_offer(self._session()), feed.MOVE_OFFERED)
        self.assertEqual(feed.move_offer(self._session(fails_over=False)), feed.MOVE_NOWHERE)
        self.assertEqual(feed.move_offer(self._session(state=feed.STATE_WAITING)),
                         feed.MOVE_NO_CHANNEL)
        self.assertEqual(feed.move_offer(self._session(state=feed.STATE_SHOWING)),
                         feed.MOVE_NO_CHANNEL)
        self.assertEqual(feed.move_offer(self._session(state=feed.STATE_STOPPED)),
                         feed.MOVE_NO_CHANNEL)
        with self.assertRaises(ValueError):
            feed.move_offer(self._session(state='SOMETHING_NEW'))

    def test_a_play_already_changing_channel_is_refused_and_left_alone(self):
        session = self._session(app=self.t.app, proc=object())
        self.addCleanup(setattr, session, 'proc', None)
        session._failing_over = True
        resp = self.client.post(f'/api/iptv-output/plays/{session.id}/move')
        self.assertEqual(resp.status_code, 409)
        self.assertIn('changing channel', resp.get_json()['error'])
        self.assertTrue(session._failing_over, 'the failover in flight still owns its flag')
        self.assertEqual(session.failovers, [])

    def test_an_unknown_play_is_404(self):
        resp = self.client.post('/api/iptv-output/plays/nope/move')
        self.assertEqual(resp.status_code, 404)


class SportsRelayTests(_Relay):
    """A sports channel through the relay (app/sports_feed.py, dev/changelog/1517): it plays the
    channel whose guide lists the game, and a dead one is replaced by another carrying the same
    game, with no group to log against."""

    def _team_channel(self, *clip_names, scores=(90.0, 50.0)):
        from app import sports_feed
        from app.database import SportsEvent, SportsFollow, SportsLeague
        now = datetime.utcnow()
        league = SportsLeague(tsdb_id=9001, name='Pretend League', match_terms='[]')
        db.session.add(league)
        db.session.flush()
        db.session.add(SportsFollow(league_id=league.id, team_tsdb_id=0, rule_id=0))
        start = now - timedelta(minutes=30)
        event = SportsEvent(tsdb_id=7001, league_id=league.id, name='g', home_team_id=101, away_team_id=102,
                            home_name='Ironvale Bears', away_name='Harborview Gulls', start_at=start,
                            local_date=start.date())
        db.session.add(event)
        db.session.flush()
        members = []
        for clip, score in zip(clip_names, scores):
            account = self.account if not members else make_account(f'Other {len(members)}', max_connections=1)
            ch = self._clip_channel(f'Local {clip}', clip=clip, account=account)
            ch.health_score = score
            make_guide_match(event, ch, start, title='g', reason='r')
            members.append(ch)
        db.session.commit()
        sports_feed.set_team_channel(league.id, 101, True)
        from app.database import SportsFeedChannel
        return feed.feed_id_for_sports(SportsFeedChannel.query.one().id), members

    def test_a_dead_channel_is_replaced_by_another_carrying_the_game(self):
        feed_id, (a, b) = self._team_channel('a.ts', 'b.ts')
        resp = self._play(feed_id)
        self._read(resp, 1)
        session = self._session()
        self.assertEqual(session.channel_id, a.id)
        self.assertIsNone(session.group_id)
        self.assertIn('Harborview Gulls at Ironvale Bears', session.label)
        session.proc.kill()
        self.assertTrue(_wait(lambda: bool(session.failovers) and session.channel_id == b.id, 15), session)
        self.assertTrue(session.live)
        self.assertEqual(connlim._holders.get(b.account_id), [('viewer', session.id)])
        self.assertEqual(connlim._holders.get(self.account.id, []), [])
        before = session.bytes_sent
        self._read(resp, 2)
        self.assertGreater(session.bytes_sent, before)
        self.assertEqual(ChannelGroupEvent.query.count(), 0, 'a sports channel is not a group')

    def test_a_failover_stays_on_the_game_the_play_started_on(self):
        from app.database import SportsEvent
        feed_id, (a, b) = self._team_channel('a.ts', 'b.ts')
        resp = self._play(feed_id)
        self._read(resp, 1)
        session = self._session()
        first = SportsEvent.query.one()
        self.assertEqual(session.event_id, first.id)
        # The team's next game starts being listed, on a healthier channel, while this play runs.
        start = datetime.utcnow() - timedelta(minutes=1)
        later = SportsEvent(tsdb_id=7002, league_id=first.league_id, name='h', home_team_id=103,
                            away_team_id=101, home_name='Northgate Panthers', away_name='Ironvale Bears',
                            start_at=start, local_date=start.date())
        db.session.add(later)
        db.session.flush()
        c = self._clip_channel('Local c', clip='a.ts', account=make_account('Third', max_connections=1))
        c.health_score = 99.0
        make_guide_match(later, c, start, title='h', reason='r')
        db.session.commit()
        self.assertEqual(feed.resolve_play(feed_id).member.id, c.id, 'a new tune-in gets the later game')
        session.proc.kill()
        self.assertTrue(_wait(lambda: bool(session.failovers), 15), session)
        self.assertEqual(session.channel_id, b.id)

    def test_nothing_left_carrying_the_game_ends_by_name_in_sports_words(self):
        feed_id, (a,) = self._team_channel('a.ts')
        resp = self._play(feed_id)
        self._read(resp, 1)
        session = self._session()
        session.proc.kill()
        self.assertTrue(_wait(lambda: not session.live, 15))
        self.assertEqual(session.reason, feed.REASON_NO_MEMBER)
        self.assertEqual(session.reason_text(), feed.SPORTS_REASON_TEXT[feed.REASON_NO_MEMBER])
        self.assertNotIn('group', session.reason_text())
        self.assertIn('already failed this play', session.detail)
        self.assertEqual(connlim._holders.get(self.account.id, []), [])


# ── Redirect mode ──────────────────────────────────────────────────────────────────────────

class RedirectModeTests(_FeedCase):
    FEED = _feed_cfg(mode='redirect')

    def test_a_play_is_a_302_to_the_member_chosen_now(self):
        a = make_channel(self.account, name='A')
        b = make_channel(self.account, name='B')
        a.health_score, b.health_score = 90.0, 50.0
        group = make_group('G', members=[a, b])
        db.session.commit()
        resp = self.client.get(f'/live/{USER}/{PASS}/{feed.feed_id_for_group(group.id)}.ts')
        self.assertEqual(resp.status_code, 302)
        self.assertEqual(resp.headers['Location'], a.stream_url)
        a.health_score = 10.0
        db.session.commit()
        resp = self.client.get(f'/live/{USER}/{PASS}/{feed.feed_id_for_group(group.id)}.ts')
        self.assertEqual(resp.headers['Location'], b.stream_url)
        self.assertEqual(feed.live_sessions(), [], 'nothing flows through ChannelBin')
        self.assertEqual([r['channel_id'] for r in feed.recent_redirects()], [b.id, a.id])
        # The dashboard's redirect branch renders the tune-ins and says what it cannot see.
        html = self.client.get('/').get_data(as_text=True)
        self.assertIn('REDIRECTED', html)
        # Named as the IPTV Output page names it, never by its log label (dev/changelog/1746).
        self.assertIn('<span class="d-name">G</span>', html)
        self.assertIn('Channel group &middot; B<', html)
        self.assertNotIn('group ' + str(group.id) + ' (G)', html)
        self.assertIn('cannot see when a redirected play ends', html)

    def test_a_numeric_login_in_a_hand_edited_config_still_works(self):
        self.t.sandbox_config(_feed_cfg(mode='redirect', username=1, password=22))
        self.assertEqual(self.client.get('/player_api.php?username=1&password=22').status_code, 200)
        self.assertEqual(self.client.get('/player_api.php?username=1&password=2').status_code, 401)
        self.assertEqual(self.client.get('/').status_code, 200)

    def test_held_seats_are_subtracted_where_a_connection_is_taken(self):
        self.t.sandbox_config(_feed_cfg(mode='redirect', held_seats={str(self.account.id): 1}))
        self.assertFalse(connlim.try_acquire(self.account.id, 'recording', 1))
        self.t.sandbox_config(_feed_cfg(mode='proxy', held_seats={str(self.account.id): 1}))
        self.assertTrue(connlim.try_acquire(self.account.id, 'recording', 1))
        connlim.release(self.account.id, 'recording', 1)
        self.t.sandbox_config(_feed_cfg(mode='redirect', held_seats={str(self.account.id): 1},
                                        enabled=False))
        self.assertTrue(connlim.try_acquire(self.account.id, 'recording', 1))
        connlim.release(self.account.id, 'recording', 1)


# ── Mixed mode (dev/changelog/1536) ────────────────────────────────────────────────────────

class MixedModeDecisionTests(unittest.TestCase):
    def test_relays_is_decided_by_the_feed_id_kind(self):
        ch, grp, sp = feed.feed_id_for_channel(3), feed.feed_id_for_group(3), feed.feed_id_for_sports(3)
        self.assertEqual([feed.relays({'mode': 'proxy'}, f) for f in (ch, grp, sp)], [True, True, True])
        self.assertEqual([feed.relays({'mode': 'mixed'}, f) for f in (ch, grp, sp)], [False, True, True])
        self.assertEqual([feed.relays({'mode': 'redirect'}, f) for f in (ch, grp, sp)], [False, False, False])
        self.assertTrue(feed.relays({'mode': 'nonsense'}, ch), 'an unknown mode is proxy, as mode_of says')
        self.assertIn('mixed', feed.MODES)


class MixedModeHeldSeatTests(_FeedCase):
    """In mixed mode the held seats are the IPTV players': a relayed play ("viewer") is never
    kept off one, and one it sits on is not taken from recordings a second time."""

    def _mixed(self, account, held, connections):
        account.max_connections = connections
        db.session.commit()
        self.t.sandbox_config(_feed_cfg(mode='mixed', held_seats={str(account.id): held}))

    def _seat(self, kind, holder):
        self.assertTrue(connlim.try_acquire(self.account.id, kind, holder), (kind, holder))
        self.addCleanup(connlim.release, self.account.id, kind, holder)

    def test_held_seats_apply_in_mixed_mode(self):
        self._mixed(self.account, held=1, connections=1)
        self.assertFalse(connlim.try_acquire(self.account.id, 'recording', 1))

    def test_a_relayed_play_may_use_a_held_seat(self):
        self._mixed(self.account, held=1, connections=1)
        self._seat('viewer', 'v1')

    def test_a_relayed_play_on_a_held_seat_leaves_the_other_seat_to_recordings(self):
        self._mixed(self.account, held=1, connections=2)
        self._seat('viewer', 'v1')
        self._seat('recording', 7)
        self.assertFalse(connlim.try_acquire(self.account.id, 'recording', 8), 'two seats, both used')

    def test_a_recording_never_ends_a_play_sitting_on_a_held_seat(self):
        from app.recorder import _try_acquire_slot_with_preemption
        self._mixed(self.account, held=1, connections=1)
        self._seat('viewer', 'v1')
        rec = make_recording(status='SCHEDULED', name='Held', channel_id=None)
        db.session.commit()
        self.addCleanup(connlim.release, self.account.id, 'recording', rec.id)
        self.assertEqual(connlim.preempt_viewers_for_slot(self.account.id), [])
        self.assertFalse(_try_acquire_slot_with_preemption(self.t.app, rec.id, self.account.id))
        self.assertEqual(connlim._holders.get(self.account.id), [('viewer', 'v1')])

    def test_only_the_plays_beyond_the_held_seats_are_preempted(self):
        self._mixed(self.account, held=1, connections=2)
        self._seat('viewer', 'v1')
        self._seat('viewer', 'v2')
        self.assertFalse(connlim.try_acquire(self.account.id, 'recording', 9))
        self.assertEqual(connlim.preempt_viewers_for_slot(self.account.id), [('v2', self.account.id)])
        self.assertTrue(connlim.try_acquire(self.account.id, 'recording', 9))
        connlim.release(self.account.id, 'recording', 9)

    def test_a_recording_waiting_on_held_seats_says_so(self):
        """DESIGN-output-feed.md 5.3 promised it; the wait blamed "another recording"."""
        from app.recorder import _slot_wait_reason
        self.assertIn('in use by another recording', _slot_wait_reason(self.account.id))
        self._mixed(self.account, held=1, connections=1)
        why = _slot_wait_reason(self.account.id)
        self.assertIn('1 connection is left alone for IPTV players', why)
        self.assertNotIn('another recording', why)
        self.t.sandbox_config(_feed_cfg(mode='redirect', held_seats={str(self.account.id): 1}))
        self.assertIn('left alone for IPTV players', _slot_wait_reason(self.account.id))

    def test_a_recordings_seat_standing_counts_a_play_on_a_held_seat_as_in_its_way(self):
        """dev/changelog/1642: a play on a held seat is never preempted (dev/changelog/1536), so
        an account whose only seat it holds is full for a recording, not "free by cutting it
        off" - and one with a second seat is free outright."""
        self._mixed(self.account, held=1, connections=1)
        self._seat('viewer', 'v1')
        self.assertEqual(connlim.seat_standing([self.account.id], 'recording'),
                         {self.account.id: connlim.SEAT_NONE})
        self._mixed(self.account, held=1, connections=2)
        self.assertEqual(connlim.seat_standing([self.account.id], 'recording'),
                         {self.account.id: connlim.SEAT_FREE})
        self._seat('viewer', 'v2')
        self.assertEqual(connlim.seat_standing([self.account.id], 'recording'),
                         {self.account.id: connlim.SEAT_BY_CUTTING_VIEWER})

    def test_recent_plays_offers_both_kinds_a_mixed_output_writes(self):
        from app.output_feed_view import _recent
        from app.database import FEED_PLAY_ENDED, FEED_PLAY_REDIRECTED, FEED_PLAY_REFUSED
        with self.t.app.test_request_context('/iptv-output'):
            chips = _recent({}, feed.MODE_MIXED, {}, {}, datetime.utcnow(), None, False)['chips']
        self.assertEqual([c['key'] for c in chips],
                         ['all', FEED_PLAY_ENDED, FEED_PLAY_REDIRECTED, FEED_PLAY_REFUSED])

    def test_proxy_mode_still_preempts_every_viewer(self):
        self.account.max_connections = 1
        db.session.commit()
        self.t.sandbox_config(_feed_cfg(mode='proxy', held_seats={str(self.account.id): 1}))
        self._seat('viewer', 'v1')
        self.assertEqual(connlim.preempt_viewers_for_slot(self.account.id), [('v1', self.account.id)])


class MixedModeRouteTests(_Relay):
    FEED = _feed_cfg(mode='mixed')

    def test_a_plain_channel_is_redirected_and_a_group_is_relayed(self):
        plain = self._clip_channel('Plain')
        plain.in_guide = True
        member = self._clip_channel('Member', clip='b.ts')
        group = make_group('G', members=[member])
        db.session.commit()
        resp = self.client.get(f'/live/{USER}/{PASS}/{feed.feed_id_for_channel(plain.id)}.ts')
        self.assertEqual(resp.status_code, 302)
        self.assertEqual(resp.headers['Location'], plain.stream_url)
        self.assertEqual([r['channel_id'] for r in feed.recent_redirects()], [plain.id])
        self.assertEqual(feed.live_sessions(), [])
        resp = self._play(feed.feed_id_for_group(group.id))
        self.assertEqual(resp.mimetype, 'video/mp2t')
        self._read(resp, 1)
        session = self._session()
        self.assertEqual(session.channel_id, member.id)
        self.assertEqual(connlim._holders.get(self.account.id), [('viewer', session.id)])

    def test_a_recording_ends_only_the_play_beyond_the_held_seats(self):
        from app.recorder import _try_acquire_slot_with_preemption
        self.account.max_connections = 2
        db.session.commit()
        self.t.sandbox_config(_feed_cfg(mode='mixed', held_seats={str(self.account.id): 1}))
        a = self._clip_channel('A')
        b = self._clip_channel('B', clip='b.ts')
        ga, gb = make_group('GA', members=[a]), make_group('GB', members=[b])
        rec = make_recording(status='SCHEDULED', name='The big game', channel_id=a.id)
        db.session.commit()
        self._read(self._play(feed.feed_id_for_group(ga.id)), 1)
        first = feed.live_sessions()[0]
        self._read(self._play(feed.feed_id_for_group(gb.id)), 1)
        second = [s for s in feed.live_sessions() if s is not first][0]
        self.assertTrue(_try_acquire_slot_with_preemption(self.t.app, rec.id, self.account.id))
        self.addCleanup(connlim.release, self.account.id, 'recording', rec.id)
        self.assertTrue(first.live, 'the play on the held seat plays on')
        # A group's play is moved rather than ended when it can be; with nowhere to go, the
        # move ends it by the same name (dev/changelog/1642).
        self.assertTrue(_wait(lambda: not second.live, 10))
        self.assertEqual(second.reason, feed.REASON_PREEMPTED)
        self.assertTrue(_wait(lambda: _fresh_count(
            lambda: Alert.query.filter_by(alert_type='VIEWER_PREEMPTED')) == 1, 5),
            'the move thread has finished with the play before the test tears down')
        self.assertEqual(sorted(map(str, connlim._holders.get(self.account.id))),
                         sorted(map(str, [('viewer', first.id), ('recording', rec.id)])))

    def test_the_page_dashboard_and_readiness_say_how_each_plays(self):
        from html import unescape
        plain = self._clip_channel('Plain')
        plain.in_guide = True
        member = self._clip_channel('Member', clip='b.ts')
        make_group('G', members=[member])
        db.session.commit()
        self.client.get(f'/live/{USER}/{PASS}/{feed.feed_id_for_channel(plain.id)}.ts')
        html = unescape(self.client.get('/iptv-output').get_data(as_text=True))
        self.assertIn('Through ChannelBin', html)
        self.assertIn('Straight from your IPTV provider', html)
        self.assertIn('In mixed mode only channel groups, sports channels, Up next channels and every play on a login for another user go through ChannelBin', html)
        self.assertIn('Recordings leave alone', html)
        self.assertIn('Current streams', html)
        self.assertIn('<option value="mixed" selected>', html)
        dash = self.client.get('/').get_data(as_text=True)
        self.assertIn('REDIRECTED', dash)
        self.assertIn('sent to Plain', dash)
        from app.readiness import Context, _check_output_feed
        self.assertIn('groups and sports channels relayed through ChannelBin',
                      _check_output_feed(Context()).found)


# ── Surfaces ───────────────────────────────────────────────────────────────────────────────

class ReadinessCheckTests(_FeedCase):
    def _result(self):
        from app.readiness import Context, _check_output_feed
        return _check_output_feed(Context())

    def test_off_is_nothing_to_check(self):
        from app.readiness import NOTHING
        self.t.sandbox_config({})
        self.assertEqual(self._result().status, NOTHING)

    def test_on_with_no_credential_is_a_problem(self):
        from app.readiness import PROBLEM
        self.t.sandbox_config(_feed_cfg(password=''))
        r = self._result()
        self.assertEqual(r.status, PROBLEM)
        self.assertIn('no username or password', r.found)

    def test_a_reserve_the_ring_cannot_hold_is_a_problem(self):
        from app.readiness import PROBLEM
        self.t.sandbox_config(_feed_cfg(buffer_mb=1, reserve_seconds=5))
        self.assertEqual(self._result().status, PROBLEM)

    def test_an_empty_guide_needs_attention_and_a_served_guide_is_ready(self):
        from app.readiness import ATTENTION, READY
        self.assertEqual(self._result().status, ATTENTION)
        make_channel(self.account, name='A', in_guide=True)
        db.session.commit()
        r = self._result()
        self.assertEqual(r.status, READY)
        self.assertIn('1 channel served', r.found)

    def test_held_seats_for_a_missing_account_need_attention(self):
        from app.readiness import ATTENTION
        make_channel(self.account, name='A', in_guide=True)
        db.session.commit()
        self.t.sandbox_config(_feed_cfg(mode='redirect', held_seats={'9999': 1}))
        r = self._result()
        self.assertEqual(r.status, ATTENTION)
        self.assertIn('9999', r.found)


class DashboardAndBundleTests(_FeedCase):
    def test_the_dashboard_lists_a_live_viewer_and_the_last_ends(self):
        ch = make_channel(self.account, name='Alpha', in_guide=True)
        db.session.commit()
        session = feed.RelaySession(
            id='s1', feed_id=feed.feed_id_for_channel(ch.id), label=f'channel {ch.id} (Alpha)',
            client='10.0.0.9', group_id=None, channel_id=ch.id, channel_name='Alpha',
            account_id=self.account.id, ring=feed.Ring(1024, 0), started_mono=time.monotonic())
        ended = feed.RelaySession(
            id='s0', feed_id=session.feed_id, label=session.label, client='10.0.0.9',
            group_id=None, channel_id=ch.id, channel_name='Alpha', account_id=self.account.id,
            ring=feed.Ring(1024, 0), state=feed.STATE_STOPPED, reason=feed.REASON_PREEMPTED,
            detail='recording 7 took the connection', ended_at=datetime.utcnow())
        with feed._lock:
            feed._sessions.update({'s1': session, 's0': ended})
        html = self.client.get('/').get_data(as_text=True)
        self.assertIn('data-sec="viewers"', html)
        self.assertIn('WATCHING', html)
        self.assertIn('10.0.0.9', html)
        self.assertIn(feed.REASON_TEXT[feed.REASON_PREEMPTED], html)
        self.assertIn('recording 7 took the connection', html)

    def test_the_bundle_carries_the_feed_without_its_credentials(self):
        import io
        import json
        import zipfile
        from app.support_bundle import build_support_bundle
        data = json.loads(zipfile.ZipFile(io.BytesIO(build_support_bundle()))
                          .read('output_feed.json'))
        self.assertTrue(data['enabled'])
        self.assertTrue(data['credentials_set'])
        self.assertNotIn('username', data)
        self.assertNotIn('password', data)
        self.assertEqual(data['viewers_now'], 0)


class ChannelChangeTakeOverTests(_Relay):
    """dev/docs/BUGS.md 2026-11-26 @ 12:53:03 AM: an IPTV player changing channel kept the old
    connection open 10 to 27 s, past SWITCH_GRACE_SECONDS, so the new channel was held or
    refused by the play it replaced. With flask.behind_proxy on, the address names the player,
    and its own old play is ended at once."""

    def setUp(self):
        super().setUp()
        from app import proxy_trust
        self.t.app.config[proxy_trust.CONFIG_KEY] = proxy_trust.Policy(web=True)

    def _play_from(self, address, feed_id, expect=200):
        resp = self.client.get(f'/live/{USER}/{PASS}/{feed_id}.ts',
                               environ_base={'REMOTE_ADDR': address})
        self.assertEqual(resp.status_code, expect, resp.get_data(as_text=True)[:300]
                         if resp.status_code != 200 else '')
        if expect == 200:
            self.open.append(resp)
        return resp

    def _two_channels(self, same_account=False):
        ch = self._clip_channel('Paced')
        ch.in_guide = True
        account = self.account if same_account else make_account('Other', max_connections=1)
        ch2 = self._clip_channel('Second', account=account)
        ch2.in_guide = True
        db.session.commit()
        return ch, ch2

    def _ended(self, session):
        with session._teardown_lock:
            pass
        return session.live, session.reason

    def test_at_the_viewer_cap_the_same_player_changing_channel_ends_its_old_play_at_once(self):
        self.t.sandbox_config(_feed_cfg(max_viewers=1))
        ch, ch2 = self._two_channels()
        self._read(self._play_from('10.0.0.5', feed.feed_id_for_channel(ch.id)))
        old = self._session()
        asked = time.monotonic()
        self._play_from('10.0.0.5', feed.feed_id_for_channel(ch2.id))
        self.assertLess(time.monotonic() - asked, feed.SWITCH_GRACE_SECONDS / 2,
                        'the change must not wait out the grace for a play its own player is leaving')
        self.assertEqual(self._ended(old), (False, feed.REASON_REPLACED))
        self.assertEqual(self._session().channel_id, ch2.id)
        self.assertEqual(feed.recent_refused(), [])
        self.assertEqual(Alert.query.filter_by(alert_type='VIEWER_REFUSED').count(), 0)

    def test_on_a_one_connection_account_the_same_player_takes_its_old_plays_seat(self):
        self.t.sandbox_config(_feed_cfg(max_viewers=3))
        ch, ch2 = self._two_channels(same_account=True)
        self._read(self._play_from('10.0.0.5', feed.feed_id_for_channel(ch.id)))
        old = self._session()
        asked = time.monotonic()
        self._play_from('10.0.0.5', feed.feed_id_for_channel(ch2.id))
        self.assertLess(time.monotonic() - asked, feed.SWITCH_GRACE_SECONDS / 2)
        self.assertEqual(self._ended(old), (False, feed.REASON_REPLACED))
        new = self._session()
        self.assertEqual(connlim._holders.get(self.account.id), [('viewer', new.id)])

    def test_a_seat_on_another_account_is_not_a_reason_to_end_the_old_play(self):
        """Under the cap, on a free account: nothing is in the way, so nothing is ended -
        a multi-view TV adding a second channel keeps the first."""
        self.t.sandbox_config(_feed_cfg(max_viewers=3))
        ch, ch2 = self._two_channels()
        self._read(self._play_from('10.0.0.5', feed.feed_id_for_channel(ch.id)))
        old = self._session()
        self._play_from('10.0.0.5', feed.feed_id_for_channel(ch2.id))
        self.assertTrue(old.live)
        self.assertEqual(len(feed.live_sessions()), 2)

    def test_another_players_play_is_waited_for_and_never_ended(self):
        self.t.sandbox_config(_feed_cfg(max_viewers=1))
        ch, ch2 = self._two_channels()
        self._read(self._play_from('10.0.0.5', feed.feed_id_for_channel(ch.id)))
        old = self._session()
        from unittest import mock
        with mock.patch.object(feed, 'SWITCH_GRACE_SECONDS', 0.3):
            self._play_from('10.0.0.6', feed.feed_id_for_channel(ch2.id), expect=503)
        self.assertTrue(old.live)

    def test_without_behind_proxy_the_address_names_no_player_and_the_old_play_is_waited_for(self):
        from app import proxy_trust
        self.t.app.config[proxy_trust.CONFIG_KEY] = proxy_trust.Policy()
        self.t.sandbox_config(_feed_cfg(max_viewers=1))
        ch, ch2 = self._two_channels()
        self._read(self._play_from('10.0.0.5', feed.feed_id_for_channel(ch.id)))
        old = self._session()
        from unittest import mock
        with mock.patch.object(feed, 'SWITCH_GRACE_SECONDS', 0.3):
            self._play_from('10.0.0.5', feed.feed_id_for_channel(ch2.id), expect=503)
        self.assertTrue(old.live)
        self.assertEqual(old.player, '')

    def test_tuning_back_to_the_channel_a_play_took_over_from_waits(self):
        """Two players behind one address past the cap: the one that was cut tunes back in to
        its channel, and ending the play that cut it would start the two cutting each other on
        every retry. A new channel still takes over at once."""
        self.t.sandbox_config(_feed_cfg(max_viewers=1))
        ch, ch2 = self._two_channels()
        third = make_account('Third', max_connections=1)
        ch3 = self._clip_channel('Third', account=third)
        ch3.in_guide = True
        db.session.commit()
        self._read(self._play_from('10.0.0.5', feed.feed_id_for_channel(ch.id)))
        first = self._session()
        self._play_from('10.0.0.5', feed.feed_id_for_channel(ch2.id))
        self.assertEqual(self._ended(first), (False, feed.REASON_REPLACED))
        second = self._session()
        from unittest import mock
        with mock.patch.object(feed, 'SWITCH_GRACE_SECONDS', 0.3):
            self._play_from('10.0.0.5', feed.feed_id_for_channel(ch.id), expect=503)
        self.assertTrue(second.live)
        self._play_from('10.0.0.5', feed.feed_id_for_channel(ch3.id))
        self.assertEqual(self._ended(second), (False, feed.REASON_REPLACED))
        self.assertEqual(self._session().channel_id, ch3.id)

    def test_a_seat_the_old_play_is_not_in_the_way_of_is_not_a_reason_to_end_it(self):
        """The new channel's account is full because of a recording; the old play is on another
        account, so ending it would free nothing."""
        self.t.sandbox_config(_feed_cfg(max_viewers=3))
        ch, ch2 = self._two_channels()
        self.assertTrue(connlim.try_acquire(self.account.id, 'recording', 4242))
        self.addCleanup(connlim.release, self.account.id, 'recording', 4242)
        self._read(self._play_from('10.0.0.5', feed.feed_id_for_channel(ch2.id)))
        old = self._session()
        from unittest import mock
        with mock.patch.object(feed, 'SWITCH_GRACE_SECONDS', 0.3):
            self._play_from('10.0.0.5', feed.feed_id_for_channel(ch.id), expect=409)
        self.assertTrue(old.live)

    def test_a_reconnect_to_the_same_channel_is_not_called_a_channel_change(self):
        self.t.sandbox_config(_feed_cfg(max_viewers=1))
        ch, _ch2 = self._two_channels()
        self._read(self._play_from('10.0.0.5', feed.feed_id_for_channel(ch.id)))
        old = self._session()
        self._play_from('10.0.0.5', feed.feed_id_for_channel(ch.id))
        self.assertEqual(self._ended(old), (False, feed.REASON_REPLACED))
        self.assertNotIn('changed channel', old.reason_text())
        self.assertIn('tuned in again', old.reason_text())

    def test_a_player_with_two_plays_has_neither_ended(self):
        """A multi-view TV at the cap: which of its two plays it is leaving cannot be told."""
        self.t.sandbox_config(_feed_cfg(max_viewers=2))
        ch, ch2 = self._two_channels()
        third = make_account('Third', max_connections=1)
        ch3 = self._clip_channel('Third', account=third)
        ch3.in_guide = True
        db.session.commit()
        self._read(self._play_from('10.0.0.5', feed.feed_id_for_channel(ch.id)))
        self._read(self._play_from('10.0.0.5', feed.feed_id_for_channel(ch2.id)))
        from unittest import mock
        with mock.patch.object(feed, 'SWITCH_GRACE_SECONDS', 0.3):
            self._play_from('10.0.0.5', feed.feed_id_for_channel(ch3.id), expect=503)
        self.assertEqual(len(feed.live_sessions()), 2)


if __name__ == '__main__':
    unittest.main()
