"""The output feed: ChannelBin as an Xtream/M3U/XMLTV source for an IPTV app
(app/output_feed.py, app/routes/output_feed.py, dev/docs/DESIGN-output-feed.md,
dev/changelog/1364).

Covers the gate (off, no credential and wrong credential all answer one 401; the right pair
answers every endpoint; the stream path carries the pair), the catalog (feed ids, the guide's
order, a group as one channel with its members unlisted, the allowlist, categories, the
listings join), the guide half (xmltv.php from one query, UTC with an explicit offset,
get.php as the feed's own URLs), the self-sync proof (ChannelBin's own XtreamClient and
sync against the running feed over loopback), the relay (a real ffmpeg over a synthesized
clip: bytes reach the IPTV app, every end has a name and releases everything, a recording preempts
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
                                make_epg_entry, make_group, make_recording)

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
        """The IPTV app sends the pair in the stream path and in the .php query, and werkzeug's
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
        self.assertIn('12 seconds fits', r.get_json()['error'])
        self.assertEqual(post('mode', 'teleport').status_code, 400)
        self.assertEqual(post('held_seats', ['2: 1', '3:2']).status_code, 200)
        self.assertEqual(load_config()['integrations']['output_feed']['held_seats'],
                         {'2': 1, '3': 2})
        self.assertEqual(post('held_seats', ['two']).status_code, 400)
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

    def test_a_logo_is_the_apps_own_route_never_the_providers_url(self):
        self._seed()
        self.guide_a.logo_url = 'http://provider.test/u/p/logo.png'
        self.guide_b.logo_url = 'http://provider.test/u/p/logo2.png'
        self.guide_b.logo_cache_path = 'abc.png'
        db.session.commit()
        rows = {r['name']: r for r in self.catalog()}
        self.assertEqual(rows['Alpha']['stream_icon'], '')
        self.assertEqual(rows['Beta']['stream_icon'],
                         f'http://localhost/api/channels/{self.guide_b.id}/logo')


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
        releasable, so cutting the session as "the IPTV app fell behind" blamed the wrong
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
        self.assertIn('reserve', session.detail)

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
        self.assertIsNotNone(proc.poll(), 'ffmpeg must be dead once the IPTV app is gone')
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
        for _ in range(2):
            resp = self._play(feed.feed_id_for_channel(ch2.id), expect=503)
            self.assertIn('viewer cap (1 at once)', resp.get_json()['error'])
        self.assertEqual(Alert.query.filter_by(alert_type='VIEWER_REFUSED').count(), 1)
        self.assertEqual(len(feed.live_sessions()), 1)

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
        event = ChannelGroupEvent.query.filter_by(group_id=group.id,
                                                  event_type=GROUP_VIEWER_FAILOVER).one()
        self.assertEqual(event.channel_id, b.id)
        self.assertIn('"Feed a"', event.detail)
        self.assertIn('"Feed b"', event.detail)
        self.assertEqual(session.failed_ids, {a.id})

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
        self.assertIn('group ' + str(group.id) + ' (G)', html)
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


if __name__ == '__main__':
    unittest.main()
