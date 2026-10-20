"""The simulator's pieces for running in a container network: hosts named by DNS name, the
DNS responder that answers for them, and the control API that drives a simulator from
another process (sim/provider.py's Layout, sim/dns.py, sim/control.py; DESIGN-simulator.md
§3.14, dev/changelog/1224).

Characterization tests of the simulator, not guards for a past defect. Everything here runs
on loopback: the control API on 127.0.0.1, the DNS responder on a free UDP port, and the
world's hosts on their own loopback addresses, named under a domain.
"""
import copy
import os
import shutil
import socket
import sys
import tempfile
import threading
import unittest

import requests

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sim import build_world, serve_for_test  # noqa: E402
from sim.control import Control, ControlError, ControlServer  # noqa: E402
from sim.dns import RCODE_NXDOMAIN, TYPE_A, DnsResponder, build_query, parse_query  # noqa: E402
from tests.support.sim_streams import (library, live_ids, read_for, read_stream,  # noqa: E402
                                       stream_world, wait_until)
from tests.support.sim_sync import FAULT_WORLD  # noqa: E402

AAAA = 28


def _world():
    return build_world(copy.deepcopy(FAULT_WORLD))


def _by_address(sim, host):
    """A host's origin by address: in-process nothing resolves the .sim.test names, and
    netguard refuses a lookup of one."""
    return 'http://%s:%d' % sim.layout.places[host]


def _ask(dns, name, qtype=TYPE_A, tid=7):
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        s.settimeout(3)
        s.sendto(build_query(name, qtype, tid), dns.address)
        data, _ = s.recvfrom(1500)
    rcode = int.from_bytes(data[2:4], 'big') & 0xF
    answers = int.from_bytes(data[6:8], 'big')
    address = socket.inet_ntoa(data[-4:]) if answers else None
    return int.from_bytes(data[:2], 'big'), rcode, answers, address


class DomainLayoutTests(unittest.TestCase):
    def setUp(self):
        self.sim = serve_for_test(self, _world(), domain='sim.test')

    def test_every_url_names_a_host_by_its_dns_name(self):
        for a in self.sim.accounts():
            for url in (a.base_url, a.m3u_url, a.epg_url):
                if url:
                    self.assertRegex(url, r'^http://[a-z0-9-]+\.sim\.test:\d+(/|$)')
        urls = self.sim.stream_urls('north-tv')
        self.assertTrue(urls)
        for url in urls.values():
            self.assertIn('.sim.test:', url)

    def test_dns_names_map_every_host_to_the_address_it_listens_on(self):
        names = self.sim.dns_names()
        self.assertEqual(len(names), len(self.sim.world.host_names()))
        for host in self.sim.world.host_names():
            self.assertEqual(names[f'{host}.sim.test'], self.sim.layout.places[host][0])

    def test_server_info_names_the_host_too(self):
        a = self.sim.account('north-tv')
        auth = requests.get(f'{_by_address(self.sim, "north-tv")}/player_api.php',
                            params={'username': a.username, 'password': a.password},
                            timeout=10).json()
        info = auth.get('server_info')
        if info:
            self.assertTrue(info['url'].endswith('.sim.test'), info)

    def test_without_a_domain_urls_use_addresses_and_dns_names_is_empty(self):
        sim = serve_for_test(self, _world(), address='127.0.0.60')
        self.assertTrue(sim.account('north-tv').base_url.startswith('http://127.0.0.'))
        self.assertEqual(sim.dns_names(), {})


class DnsResponderTests(unittest.TestCase):
    def setUp(self):
        self.dns = DnsResponder({'north-tv.sim.test': '10.1.0.129',
                                 'north-s1.sim.test': '10.1.0.130'}, '127.0.0.1', 0).start()
        self.addCleanup(self.dns.stop)

    def test_a_held_name_answers_its_address(self):
        tid, rcode, n, address = _ask(self.dns, 'North-TV.sim.test', tid=4242)
        self.assertEqual((tid, rcode, n, address), (4242, 0, 1, '10.1.0.129'))

    def test_an_unknown_name_is_nxdomain(self):
        self.assertEqual(_ask(self.dns, 'nowhere.sim.test')[1:3], (RCODE_NXDOMAIN, 0))

    def test_another_record_type_is_nodata_not_nxdomain(self):
        """A resolver asks for AAAA beside A; answering that one NXDOMAIN would read as the
        name being gone."""
        self.assertEqual(_ask(self.dns, 'north-tv.sim.test', AAAA)[1:3], (0, 0))

    def test_down_then_up(self):
        self.dns.down('north-s1.sim.test')
        self.assertEqual(_ask(self.dns, 'north-s1.sim.test')[1], RCODE_NXDOMAIN)
        self.assertEqual(_ask(self.dns, 'north-tv.sim.test')[3], '10.1.0.129')
        self.dns.up('north-s1.sim.test')
        self.assertEqual(_ask(self.dns, 'north-s1.sim.test')[3], '10.1.0.130')

    def test_an_alias_outlives_its_target_going_down(self):
        self.dns.alias('north-s1-b.sim.test', 'north-s1.sim.test')
        self.dns.down('north-s1.sim.test')
        self.assertEqual(_ask(self.dns, 'north-s1-b.sim.test')[3], '10.1.0.130')
        self.assertEqual(self.dns.state()['queries']['north-s1-b.sim.test'], 1)

    def test_naming_an_unknown_name_is_refused_with_the_choices(self):
        with self.assertRaisesRegex(KeyError, 'north-tv.sim.test'):
            self.dns.down('typo.sim.test')

    def test_junk_is_ignored(self):
        self.assertIsNone(self.dns.answer(b'\x00\x01'))
        self.assertIsNone(parse_query(b'\x00' * 12))
        self.assertEqual(_ask(self.dns, 'north-tv.sim.test')[1], 0)


class ControlTests(unittest.TestCase):
    def setUp(self):
        self.sim = serve_for_test(self, _world(), domain='sim.test')
        self.dns = DnsResponder(self.sim.dns_names(), '127.0.0.1', 0).start()
        self.addCleanup(self.dns.stop)
        self.server = ControlServer(Control(self.sim, self.dns)).start()
        self.addCleanup(self.server.stop)
        self.base = 'http://%s:%d' % self.server.address

    def call(self, method, path, body=None, status=200):
        r = requests.request(method, self.base + path, json=body, timeout=10)
        self.assertEqual(r.status_code, status, r.text)
        return r.json()

    def test_health_and_accounts(self):
        self.assertTrue(self.call('GET', '/health')['success'])
        accounts = self.call('GET', '/accounts')['accounts']
        self.assertEqual({a['panel'] for a in accounts}, set(self.sim.world.panels))

    def test_a_fault_is_injected_listed_and_cleared(self):
        login = next(iter(self.sim.world.logins))
        f = self.call('POST', '/faults', {'kind': 'auth_refused', 'target': login})['fault']
        self.assertEqual([x['id'] for x in self.call('GET', '/faults')['faults']], [f['id']])
        self.assertEqual(self.call('DELETE', f'/faults/{f["id"]}')['cleared'], f['id'])
        self.assertEqual(self.call('GET', '/faults')['faults'], [])
        self.assertIn('unknown fault kind', self.call(
            'POST', '/faults', {'kind': 'nope', 'target': login}, status=400)['error'])

    def test_login_status_and_ledger(self):
        a = self.sim.account('north-tv')
        self.call('POST', f'/logins/{a.username}/status', {'status': 'Banned'})
        requests.get(f'{_by_address(self.sim, "north-tv")}/player_api.php',
                     params={'username': a.username, 'password': a.password}, timeout=10)
        rows = self.call('GET', '/ledger')['rows']
        self.assertEqual(rows[-1]['login'], a.username)
        self.assertEqual(self.call('GET', f'/ledger?since={rows[-1]["seq"]}')['rows'], [])
        self.assertIn('status must be', self.call(
            'POST', f'/logins/{a.username}/status', {'status': 'Gone'}, status=400)['error'])

    def test_dns_operations(self):
        name = next(iter(self.sim.dns_names()))
        self.assertIn(name, self.call('POST', f'/dns/{name}/down')['down'])
        self.assertEqual(_ask(self.dns, name)[1], RCODE_NXDOMAIN)
        self.call('POST', f'/dns/{name}/up')
        state = self.call('POST', '/dns/alias', {'name': 'spare.sim.test', 'target': name})
        self.assertEqual(state['names']['spare.sim.test'], state['names'][name])
        self.call('POST', '/dns/typo.sim.test/down', status=404)

    def test_an_unknown_operation_is_a_404_naming_it(self):
        self.assertIn('no control operation', self.call('GET', '/nothing', status=404)['error'])

    def test_dispatch_without_a_socket(self):
        c = Control(self.sim)
        with self.assertRaises(ControlError) as cm:
            c.dispatch('GET', '/dns', {})
        self.assertEqual(cm.exception.status, 409)


@unittest.skipUnless(shutil.which('ffmpeg') and shutil.which('ffprobe'),
                     'the answer key reads files with ffprobe')
class ControlCheckTests(unittest.TestCase):
    """/check is the answer key behind the control API: the same answer `check()` gives a
    test holding the simulator itself."""

    def test_check_places_a_stream_read_as_a_file(self):
        world = stream_world()
        sim = serve_for_test(self, world, content=library())
        sid = live_ids(world)[0]
        status, data = read_for(sim.stream_urls('cove-tv')[sid], 6)
        self.assertEqual(status, 200)
        folder = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, folder, True)
        path = os.path.join(folder, 'read.ts')
        with open(path, 'wb') as fh:
            fh.write(data)
        got = Control(sim).dispatch('POST', '/check', {'segments': [path]})
        self.assertEqual(list(got['answer']['channels']), [f'cove:{sid}'], got['describe'])
        self.assertGreater(got['answer']['held_seconds'], 4.0, got['describe'])
        with self.assertRaises(ControlError) as cm:
            Control(sim).dispatch('POST', '/check', {'segments': []})
        self.assertEqual(cm.exception.status, 400)


class ControlCutTests(unittest.TestCase):
    def test_cut_ends_a_login_s_open_connection_and_says_why(self):
        world = stream_world()
        sim = serve_for_test(self, world, content=library())
        url = sim.stream_urls('cove-tv')[live_ids(world)[0]]
        timer = threading.Timer(2.0, lambda: Control(sim).dispatch(
            'POST', '/logins/reeduser/cut', {'why': 'dropped for the test'}))
        timer.start()
        self.addCleanup(timer.cancel)
        got = read_stream(url, 10)
        self.assertEqual(got['ended'], 'eof', got)
        self.assertTrue(wait_until(lambda: [r for r in sim.ledger.entries(endpoint='stream')
                                            if 'dropped for the test' in r.note]))
        with self.assertRaises(ControlError) as cm:
            Control(sim).dispatch('POST', '/logins/nobody/cut', {})
        self.assertEqual(cm.exception.status, 404)


if __name__ == '__main__':
    unittest.main()
