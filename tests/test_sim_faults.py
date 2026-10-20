"""The simulator's fault record: the catalog, how a fault is checked, when its trigger lets it
apply, and what the server does with one (sim/faults.py, DESIGN-simulator.md §3.4,
dev/changelog/1218).

What ChannelBin does about each fault is tested in tests/test_sim_sync_faults_*.py; these are
the simulator's own guarantees - that a fault does what it says, only when it says, and that
taking it away gives back exactly the bytes the world served before. Characterization tests
of the simulator, not guards for a past defect.
"""
import ast
import copy
import datetime as dt
import hashlib
import io
import os
import pathlib
import sys
import threading
import time
import unittest

import requests

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sim import Simulator, WorldError, build_world, serve_for_test  # noqa: E402
from sim.__main__ import cmd_faults  # noqa: E402
from sim.faults import (ASSUMED, CATALOG, MEASURED, OBSERVED, REPRODUCED, SYNC_KINDS,  # noqa: E402
                        Ask, FaultBook, FaultError, check_catalog, make_fault)
from sim.provider import Layout, Provider  # noqa: E402
from sim.server import THREAD_PREFIX  # noqa: E402
from tests.support.sim_sync import FAULT_WORLD  # noqa: E402

TESTS = pathlib.Path(__file__).resolve().parent
AT = dt.datetime(2026, 10, 17, 12, 0)


def _world(**extra):
    data = copy.deepcopy(FAULT_WORLD)
    data.update(extra)
    return build_world(data)


def _ask(world, host='north-tv', endpoint='playlist', login=None, at=AT, ua=''):
    return Ask(host, 'panel', endpoint, login, 'Active' if login else None, ua, at)


class CatalogTests(unittest.TestCase):
    def test_every_kind_is_well_formed(self):
        self.assertEqual(check_catalog(), [])
        words = {MEASURED, OBSERVED, REPRODUCED, ASSUMED}
        self.assertEqual({k.basis for k in CATALOG.values()} - words, set())

    def test_every_sync_kind_is_exercised_by_a_real_sync_test(self):
        """A kind nothing syncs against is a claim about ChannelBin that nobody checked. The
        stream kinds are read back in tests/test_sim_stream_faults.py."""
        named = set()
        for path in sorted(TESTS.glob('test_sim_sync_faults_*.py')):
            tree = ast.parse(path.read_text(encoding='utf-8'))
            named |= {n.value for n in ast.walk(tree)
                      if isinstance(n, ast.Constant) and isinstance(n.value, str)}
        self.assertEqual(sorted(set(SYNC_KINDS) - named), [])

    def test_the_command_line_lists_every_kind_with_its_evidence(self):
        out = io.StringIO()
        self.assertEqual(cmd_faults(None, out=out), 0)
        text = out.getvalue()
        for k in CATALOG.values():
            self.assertIn(f'{k.name}  [{k.scope}, {k.basis}]', text)
            self.assertIn(k.evidence, text)


class MakeFaultTests(unittest.TestCase):
    def setUp(self):
        self.world = _world()
        self.login = next(iter(self.world.logins))

    def refused(self, *args, said, **kwargs):
        with self.assertRaises(FaultError) as cm:
            make_fault(self.world, *args, **kwargs)
        self.assertIn(said, str(cm.exception))

    def test_a_fault_that_could_never_apply_is_refused_with_the_choices(self):
        self.refused('no_such_kind', 'north-tv', said='kinds are auth_refused')
        self.refused('playlist_down', 'north-tv', said='unknown setting(s) colour',
                     colour='red')
        self.refused('account_status', self.login, said="setting 'status' is required")
        self.refused('account_status', self.login, status='Active',
                     said='must be one of Expired, Banned, Disabled')
        self.refused('account_status', 'north-tv', status='Expired',
                     said="target must be a login's username")
        self.refused('playlist_down', 'nowhere', said='target must be a panel or guide site')
        self.refused('html_page', 'north-tv', endpoints=['stream'],
                     said='endpoints must be some of')
        self.refused('logo_svg', 'north-tv', said='answers none of the endpoints')
        self.refused('auth_as_playlist', 'vale-m3u', said='only applies to xtream panels')
        self.refused('lineup_change', 'north-tv', drop=1.5, said='from 0 to 1')
        self.refused('playlist_down', 'north-tv', start=AT, end=AT, said='end must be after')
        self.refused('playlist_down', 'north-tv', after=-1, said='at least 0')

    def test_defaults_and_narrowed_endpoints(self):
        f = make_fault(self.world, 'playlist_preamble', 'vale-m3u')
        self.assertEqual(f.params['lines'], 1)
        f = make_fault(self.world, 'server_error', 'north-tv', endpoints='guide', status=502)
        self.assertEqual((f.endpoints, f.params['status']), (('guide',), 502))
        # A host fault on a panel host narrows itself to what that host answers.
        self.assertNotIn('logo', make_fault(self.world, 'empty_body', 'north-tv').endpoints)

    def test_a_world_file_declares_faults_and_a_bad_one_is_refused(self):
        world = _world(faults=[{'kind': 'playlist_down', 'target': 'north-tv', 'after': 1}])
        self.assertEqual([f.kind.name for f in world.faults], ['playlist_down'])
        self.assertEqual(world.faults[0].trigger.after, 1)
        with self.assertRaises(WorldError) as cm:
            _world(faults=[{'kind': 'playlist_down', 'target': 'north-tv', 'loud': True}])
        self.assertIn('faults[0] (playlist_down): unknown setting(s) loud', str(cm.exception))


class TriggerTests(unittest.TestCase):
    def setUp(self):
        self.world = _world()

    def test_after_and_times_pick_out_the_requests_it_applies_to(self):
        book = FaultBook([make_fault(self.world, 'playlist_down', 'north-tv', after=1,
                                     times=2)])
        hits = [bool(book.take(_ask(self.world))) for _ in range(4)]
        self.assertEqual(hits, [False, True, True, False])
        (a,) = book.active()
        self.assertEqual((a.seen, a.hits, a.spent), (4, 2, True))

    def test_the_window_is_on_the_simulator_clock(self):
        book = FaultBook([make_fault(self.world, 'playlist_down', 'north-tv',
                                     start=AT, end=AT + dt.timedelta(hours=1))])
        early, inside, late = (AT - dt.timedelta(minutes=1), AT + dt.timedelta(minutes=30),
                               AT + dt.timedelta(hours=1))
        self.assertEqual([bool(book.take(_ask(self.world, at=t))) for t in (early, inside, late)],
                         [False, True, False])
        self.assertEqual(book.active()[0].seen, 1, 'requests outside the window are not counted')

    def test_only_the_named_endpoints_logins_and_user_agents_count(self):
        login = next(iter(self.world.logins))
        book = FaultBook([make_fault(self.world, 'auth_refused', login),
                          make_fault(self.world, 'user_agent_refused', 'north-tv', match='Lavf')])
        self.assertEqual(book.take(_ask(self.world, endpoint='logo')), [])
        self.assertEqual(book.take(_ask(self.world, endpoint='auth')), [])
        names = [a.kind.name for a in book.take(_ask(self.world, endpoint='auth', login=login,
                                                     ua='Lavf/60.16.100'))]
        self.assertEqual(names, ['auth_refused', 'user_agent_refused'])

    def test_peek_counts_nothing(self):
        book = FaultBook([make_fault(self.world, 'playlist_down', 'north-tv', times=1)])
        for _ in range(3):
            self.assertTrue(book.peek(_ask(self.world)))
        self.assertEqual(book.active()[0].seen, 0)


class ProviderFaultTests(unittest.TestCase):
    """Faults as answers, on a provider no socket backs: the bytes alone."""

    def provider(self, *faults):
        world = _world()
        return world, Provider(world, Layout.virtual(world), lambda: AT,
                               FaultBook([make_fault(world, *f[:2], **f[2]) for f in faults]))

    def playlist(self, world, provider):
        lg = next(iter(world.logins.values()))
        return provider.handle('north-tv', 'GET', f'/get.php?username={lg.username}&password='
                               f'{lg.password}&type=m3u_plus&output=ts')

    def test_the_same_fault_gives_the_same_bytes_every_time(self):
        spec = ('lineup_change', 'north-tv', {'drop': 0.3, 'add': 4, 'rename': 0.2})
        first = self.playlist(*self.provider(spec)).body
        again = self.playlist(*self.provider(spec)).body
        self.assertEqual(hashlib.sha256(first).hexdigest(), hashlib.sha256(again).hexdigest())
        self.assertNotEqual(first, self.playlist(*self.provider()).body)

    def test_an_answer_records_its_endpoint_and_the_faults_that_shaped_it(self):
        world, provider = self.provider(('playlist_preamble', 'north-tv', {}),
                                        ('playlist_bom', 'north-tv', {}))
        resp = self.playlist(world, provider)
        self.assertEqual(resp.endpoint, 'playlist')
        self.assertEqual(resp.faults, ('playlist_preamble', 'playlist_bom'))
        self.assertTrue(resp.body.startswith(b'\xef\xbb\xbf# Free TV'),
                        'the byte order mark stays first whatever order the two apply in')

    def test_a_login_fault_overrides_the_standing_status(self):
        world, provider = self.provider()
        login = next(iter(world.logins.values()))
        provider.faults.add(make_fault(world, 'account_status', login.username,
                                       status='Expired'))
        resp = provider.handle('north-tv', 'GET', f'/player_api.php?username={login.username}'
                               f'&password={login.password}')
        self.assertIn(b'"status":"Expired"', resp.body)
        self.assertEqual(provider.status[login.username], 'Active',
                         'the standing status is untouched')


class OverHttpTests(unittest.TestCase):
    def setUp(self):
        self.world = _world()
        self.sim = serve_for_test(self, self.world, clock=AT)
        self.acct = self.sim.account('north-tv')

    def auth(self):
        return requests.get(self.acct.base_url + '/player_api.php', timeout=10,
                            params={'username': self.acct.username,
                                    'password': self.acct.password})

    def test_clearing_a_fault_gives_back_the_exact_bytes(self):
        before = requests.get(self.acct.m3u_url, timeout=10).content
        fault = self.sim.inject('lineup_change', 'north-tv', drop=0.5)
        self.assertNotEqual(requests.get(self.acct.m3u_url, timeout=10).content, before)
        self.sim.clear(fault)
        self.assertEqual(requests.get(self.acct.m3u_url, timeout=10).content, before)

    def test_the_ledger_records_endpoint_user_agent_and_faults(self):
        self.sim.inject('gzip_encoding', 'north-tv', endpoints=['auth'])
        self.auth()
        row = self.sim.ledger.entries()[-1]
        self.assertEqual((row.endpoint, row.faults), ('auth', ('gzip_encoding',)))
        self.assertTrue(row.user_agent.startswith('python-requests/'))
        self.assertEqual(self.sim.ledger.count(fault='gzip_encoding'), 1)

    def test_a_cut_chunked_body_reads_the_way_sync_log_122_did(self):
        """The signature dev/docs/BUGS.md 2026-07-21 10:20 AM recorded from a real fetch."""
        self.sim.inject('truncated_chunked', 'north-tv', endpoints=['guide'])
        with self.assertRaises(requests.exceptions.ChunkedEncodingError) as cm:
            requests.get(self.acct.epg_url, timeout=10)
        self.assertIn("InvalidChunkLength(got length b'', 0 bytes read)", str(cm.exception))

    def test_gzip_on_the_wire_is_decoded_by_the_client(self):
        plain = requests.get(self.acct.epg_url, timeout=10)
        self.sim.inject('gzip_encoding', 'north-tv', endpoints=['guide'])
        coded = requests.get(self.acct.epg_url, timeout=10)
        self.assertEqual(coded.headers['Content-Encoding'], 'gzip')
        self.assertEqual(coded.content, plain.content)
        self.assertLess(self.sim.ledger.entries()[-1].bytes, len(plain.content))

    def test_the_rate_limit_refuses_then_bans(self):
        self.sim.inject('rate_limit', self.acct.username, syncs=1, ban_after=2)
        self.assertEqual(self.auth().json()['user_info']['auth'], 1)
        self.assertEqual([self.auth().status_code for _ in range(2)], [429, 429])
        self.assertEqual(self.auth().json()['user_info'], {'auth': 0, 'status': 'Banned'})


class StopTests(unittest.TestCase):
    def test_stop_ends_a_slow_drip_at_once_and_leaves_nothing_running(self):
        sim = Simulator(_world(), clock=AT).start()
        self.addCleanup(sim.stop)
        acct = sim.account('north-tv')
        drip = sim.inject('slow_drip', 'north-tv', endpoints=['playlist'], gap_seconds=30)

        def fetch():
            try:
                requests.get(acct.m3u_url, timeout=60)
            except requests.RequestException:
                pass  # the stop cuts the answer off; that is what is being tested

        client = threading.Thread(target=fetch, name='drip-client')
        client.start()
        # A hit is counted when the answer is decided, just before its first piece is written.
        deadline = time.monotonic() + 10
        while drip.hits == 0 and time.monotonic() < deadline:
            time.sleep(0.02)
        self.assertEqual(drip.hits, 1)
        started = time.monotonic()
        sim.stop()
        self.assertLess(time.monotonic() - started, 5)
        client.join(timeout=10)
        self.assertFalse(client.is_alive())
        self.assertEqual([t.name for t in threading.enumerate()
                          if t.name.startswith(THREAD_PREFIX)], [])
        self.assertEqual(sim.ledger.entries()[-1].faults, ('slow_drip',))


if __name__ == '__main__':
    unittest.main()
