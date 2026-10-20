"""The simulator over real HTTP: what each endpoint answers, what the ledger records, and that
stopping it leaves nothing running (dev/changelog/1217, DESIGN-simulator.md §3.2, §5).
"""
import json
import os
import shutil
import socket
import struct
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import zlib
from unittest import mock

import requests

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sim import Simulator, build_world, load_world, serve_for_test  # noqa: E402
from sim.server import THREAD_PREFIX  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SMALL = os.path.join(ROOT, 'sim', 'worlds', 'small.yaml')


def _sim_threads():
    return [t.name for t in threading.enumerate() if t.name.startswith(THREAD_PREFIX)]


class _Served(unittest.TestCase):
    world_source = SMALL

    def setUp(self):
        self.sim = serve_for_test(self, load_world(self.world_source))

    def get(self, host, path, **kw):
        return requests.get(self.sim.url(host, path), timeout=10, **kw)


class XtreamEndpointTests(_Served):
    def setUp(self):
        super().setUp()
        self.acct = self.sim.account('harbor-tv')
        self.q = {'username': self.acct.username, 'password': self.acct.password}

    def api(self, **extra):
        return self.get('harbor-tv', '/player_api.php', params=dict(self.q, **extra))

    def test_auth_answers_in_the_measured_shape(self):
        """Every numeric field a string, as a real panel sends it (dev/changelog/534)."""
        r = self.api()
        self.assertEqual(r.status_code, 200)
        data = r.json()
        info = data['user_info']
        self.assertEqual(info['auth'], 1)
        self.assertEqual(info['status'], 'Active')
        for k in ('exp_date', 'is_trial', 'active_cons', 'max_connections', 'created_at'):
            self.assertIsInstance(info[k], str, k)
        stream_host = self.sim.world.backends['harbor'].stream_host_names()[0]
        address, port = self.sim.layout.places[stream_host]
        self.assertEqual((data['server_info']['url'], data['server_info']['port']),
                         (address, str(port)))

    def test_json_escapes_slashes_and_non_ascii_like_php(self):
        r = self.api(action='get_live_streams')
        self.assertIn(b'http:\\/\\/', r.content)
        r.content.decode('ascii')

    def test_a_panel_that_declares_its_api_host_says_so(self):
        plus = self.sim.account('harbor-plus')
        r = self.get('harbor-plus', '/player_api.php',
                     params={'username': plus.username, 'password': plus.password})
        self.assertEqual(r.json()['server_info']['url'],
                         self.sim.layout.places['harbor-plus'][0])

    def test_an_unknown_action_answers_an_empty_list(self):
        self.assertEqual(self.api(action='get_vod_streams').json(), [])

    def test_wrong_credentials_are_refused(self):
        r = self.get('harbor-tv', '/player_api.php',
                     params={'username': self.acct.username, 'password': 'wrong'})
        self.assertEqual(r.json(), {'user_info': {'auth': 0}})
        r = self.get('harbor-tv', '/get.php', params={'username': 'nobody', 'password': 'x'})
        self.assertEqual(r.status_code, 401)

    def test_a_login_valid_elsewhere_is_refused_here(self):
        meadow = self.sim.account('meadow-m3u')
        r = self.get('harbor-tv', '/player_api.php',
                     params={'username': meadow.username, 'password': meadow.password})
        self.assertEqual(r.json()['user_info']['auth'], 0)

    def test_a_banned_login_reports_its_status_and_gets_nothing(self):
        self.sim.set_login_status(self.acct.username, 'Banned')
        self.assertEqual(self.api().json(), {'user_info': {'auth': 0, 'status': 'Banned'}})
        self.assertEqual(self.api(action='get_live_streams').json()['user_info']['auth'], 0)
        r = self.get('harbor-tv', '/get.php', params=self.q)
        self.assertEqual(r.status_code, 401)

    def test_an_expired_login_still_authenticates_and_gets_no_data(self):
        self.sim.set_login_status(self.acct.username, 'Expired')
        info = self.api().json()['user_info']
        self.assertEqual((info['auth'], info['status']), (1, 'Expired'))
        self.assertEqual(self.api(action='get_live_categories').json()['user_info']['auth'], 0)

    def test_playlist_and_catalog_agree(self):
        playlist = self.get('harbor-tv', '/get.php',
                            params=dict(self.q, type='m3u_plus', output='ts')).text
        self.assertTrue(playlist.startswith('#EXTM3U'))
        catalog = self.api(action='get_live_streams').json()
        urls = [ln for ln in playlist.splitlines() if ln and not ln.startswith('#')]
        live = [u for u in urls if '/movie/' not in u and '/series/' not in u]
        self.assertEqual(len(live), len(catalog))
        self.assertGreater(len(urls), len(live))
        tails = {u.rsplit('/', 1)[-1].split('.')[0] for u in live}
        self.assertTrue({str(row['stream_id']) for row in catalog} <= tails | {''})

    def test_guide_lists_the_panels_ids(self):
        r = self.get('harbor-tv', '/xmltv.php', params=self.q)
        self.assertEqual(r.status_code, 200)
        self.assertIn(b'<tv generator-info-name=', r.content)
        first = self.sim.world.panel_lineups['harbor-tv'].guide[0].guide_id
        self.assertIn(f'<channel id="{first}">'.encode(), r.content)

    def test_head_answers_headers_only(self):
        r = requests.head(self.sim.url('harbor-tv', '/get.php'), params=self.q, timeout=10)
        self.assertEqual(r.status_code, 200)
        self.assertGreater(int(r.headers['Content-Length']), 1000)
        self.assertEqual(r.content, b'')


class OtherHostTests(_Served):
    def test_m3u_panel_serves_its_playlist_and_a_gzipped_guide(self):
        a = self.sim.account('meadow-m3u')
        self.assertIsNone(a.base_url)
        r = requests.get(a.m3u_url, timeout=10)
        self.assertTrue(r.text.startswith('#EXTM3U'))
        r = requests.get(a.epg_url, timeout=10)
        self.assertTrue(a.epg_url.endswith('.xml.gz'))
        self.assertEqual(r.content[:2], b'\x1f\x8b')
        self.assertEqual(requests.get(a.m3u_url.replace(a.password, 'nope'), timeout=10)
                         .status_code, 401)

    def test_guide_site_serves_plain_and_gzipped_the_same_document(self):
        plain = requests.get(self.sim.guide_site_url('listings'), timeout=10).content
        gz = requests.get(self.sim.guide_site_url('listings', gzipped=True), timeout=10).content
        self.assertEqual(zlib.decompress(gz, 16 + zlib.MAX_WBITS), plain)

    def test_logos_are_png(self):
        row = next(r for r in self.sim.world.panel_lineups['harbor-tv'].rows if r.channel.logo)
        r = self.get('logos', f'/logos/{row.channel.logo}.png')
        self.assertEqual(r.headers['Content-Type'], 'image/png')
        self.assertEqual(r.content[:8], b'\x89PNG\r\n\x1a\n')
        width, height = struct.unpack('>II', r.content[16:24])
        self.assertEqual((width, height), (64, 64))
        self.assertEqual(self.get('logos', '/logos/not-hex.png').status_code, 404)

    def test_hosts_are_on_separate_addresses(self):
        addresses = {a for a, _ in self.sim.layout.places.values()}
        self.assertEqual(len(addresses), len(self.sim.layout.places))
        self.assertEqual(self.get('harbor-streams-1', '/a/b/1').status_code, 401)
        self.assertEqual(self.get('harbor-streams-1', '/nowhere').status_code, 404)
        self.assertEqual(self.get('harbor-tv', '/nowhere').status_code, 404)


class LedgerTests(_Served):
    def test_every_request_is_recorded_with_the_login_it_resolved_to(self):
        a = self.sim.account('harbor-tv')
        q = {'username': a.username, 'password': a.password}
        self.get('harbor-tv', '/player_api.php', params=q)
        self.get('harbor-tv', '/player_api.php', params=dict(q, action='get_live_categories'))
        self.get('harbor-tv', '/get.php', params={'username': 'nobody', 'password': 'x'})
        self.get('logos', '/logos/abc.png')
        rows = self.sim.ledger.entries()
        self.assertEqual([r.seq for r in rows], [1, 2, 3, 4])
        self.assertEqual([(r.host, r.path, r.status, r.login) for r in rows], [
            ('harbor-tv', '/player_api.php', 200, a.username),
            ('harbor-tv', '/player_api.php', 200, a.username),
            ('harbor-tv', '/get.php', 401, None),
            ('logos', '/logos/abc.png', 200, None)])
        self.assertEqual(self.sim.ledger.count(action='get_live_categories'), 1)
        self.assertGreater(rows[1].bytes, 0)
        self.assertEqual(rows[0].role, 'panel')

    def test_a_request_is_in_the_ledger_by_the_time_its_answer_arrives(self):
        """Guards dev/docs/BUGS.md 2026-10-17 (the ledger race): the row was appended after
        the last byte, so a client reading the ledger as soon as its answer arrived could miss
        it - the intermittent failure of the test above on a loaded box. Recording is slowed
        here so the old order misses the row every time."""
        record = self.sim.ledger.record

        def slow_record(**fields):
            time.sleep(0.3)
            return record(**fields)

        with mock.patch.object(self.sim.ledger, 'record', side_effect=slow_record):
            body = self.get('logos', '/logos/abc.png').content
        rows = self.sim.ledger.entries()
        self.assertEqual([(r.path, r.status) for r in rows], [('/logos/abc.png', 200)])
        self.assertEqual(rows[0].bytes, len(body))


class LifecycleTests(unittest.TestCase):
    def test_stop_leaves_no_thread_and_no_listener(self):
        before = _sim_threads()
        sim = Simulator(load_world(SMALL)).start()
        address, port = sim.layout.places['harbor-tv']
        self.assertTrue(_sim_threads())
        sim.stop()
        self.assertEqual(_sim_threads(), before)
        with self.assertRaises(ConnectionRefusedError):
            socket.create_connection((address, port), timeout=2).close()
        sim.stop()

    def test_stop_does_not_wait_on_an_idle_keep_alive_connection(self):
        sim = Simulator(load_world(SMALL)).start()
        address, port = sim.layout.places['logos']
        conn = socket.create_connection((address, port), timeout=5)
        try:
            conn.sendall(b'GET /logos/ab.png HTTP/1.1\r\nHost: x\r\n\r\n')
            conn.recv(65536)
            started = time.monotonic()
            sim.stop()
            self.assertLess(time.monotonic() - started, 3)
            self.assertEqual(_sim_threads(), [])
        finally:
            conn.close()

    def test_starting_never_asks_dns_for_a_hosts_name(self):
        """dev/docs/BUGS.md 2026-10-19 05:13 AM - http.server looked up each host's fully
        qualified name as it bound, a reverse DNS query that waits out its timeout on the full
        stack's network with no resolver: about 5 s a host before the simulator listened."""
        with mock.patch('socket.getfqdn', side_effect=AssertionError('reverse DNS lookup')):
            sim = serve_for_test(self, load_world(SMALL))
        self.assertEqual(requests.get(sim.url('logos', '/logos/ab'), timeout=5).status_code, 200)

    def test_a_host_declared_down_refuses_connections(self):
        world = build_world({'backends': [{'name': 'b', 'channels': 20}],
                             'hosts_down': ['b']})
        sim = serve_for_test(self, world)
        with self.assertRaises(requests.ConnectionError):
            requests.get(sim.url('b', '/player_api.php'), timeout=5)
        self.assertEqual(requests.get(sim.url('logos', '/logos/ab'), timeout=5).status_code, 200)

    def test_single_address_puts_every_host_on_its_own_port(self):
        sim = serve_for_test(self, load_world(SMALL), address='127.0.0.1', spread=False)
        places = sim.layout.places.values()
        self.assertEqual({a for a, _ in places}, {'127.0.0.1'})
        self.assertEqual(len({p for _, p in places}), len(sim.layout.places))
        self.assertEqual(requests.get(sim.account('harbor-tv').m3u_url, timeout=10).status_code,
                         200)

    def test_a_proxy_in_the_environment_cannot_reroute_a_fetch(self):
        """requests honors HTTP_PROXY; the fixture adds loopback to no_proxy for the test, so
        a fetch reaches the simulator rather than a proxy (which the suite's network guard
        would refuse, confusingly)."""
        proxy = mock.patch.dict(os.environ, {'HTTP_PROXY': 'http://192.0.2.1:3128',
                                             'http_proxy': 'http://192.0.2.1:3128'})
        proxy.start()
        # Registered before the fixture's own environment patch, so the two unwind in order.
        self.addCleanup(proxy.stop)
        sim = serve_for_test(self, load_world(SMALL))
        r = requests.get(sim.account('harbor-tv').m3u_url, timeout=10)
        self.assertEqual(r.status_code, 200)


class CommandLineTests(unittest.TestCase):
    def test_dump_writes_every_answer_and_a_manifest(self):
        out = tempfile.mkdtemp(prefix='sim-dump-')
        self.addCleanup(shutil.rmtree, out, True)
        subprocess.run([sys.executable, '-m', 'sim', 'dump', '--out', out,
                        '--at', '2026-10-17T12:00'], cwd=ROOT, check=True,
                       capture_output=True, timeout=120)
        with open(os.path.join(out, 'manifest.json'), encoding='utf-8') as f:
            manifest = json.load(f)
        paths = {e['path'] for e in manifest['files']}
        a = load_world(SMALL).logins_on('harbor-tv')[0].username
        self.assertIn(f'harbor-tv/{a}/playlist.m3u', paths)
        self.assertIn('listings/guide.xml', paths)
        for e in manifest['files']:
            self.assertEqual(os.path.getsize(os.path.join(out, e['path'])), e['bytes'])

    def test_a_bad_world_file_is_reported_not_raised(self):
        r = subprocess.run([sys.executable, '-m', 'sim', 'dump', '--out', '/nonexistent',
                            '--world', os.path.join(ROOT, 'README.md')], cwd=ROOT,
                           capture_output=True, text=True, timeout=60)
        self.assertEqual(r.returncode, 2)
        self.assertIn('world file error', r.stderr)


if __name__ == '__main__':
    unittest.main()
