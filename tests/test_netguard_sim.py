"""netguard's one scoped exception: a child process may be pointed at a simulator this process
started and has not stopped, and at nothing else (DESIGN-simulator.md §5, D2;
dev/changelog/1219).

Both directions matter. Without the exception no test can capture a stream over a connection,
which is the gap the simulator exists to close. With it, every URL that is not a live
simulator's must still raise and still be recorded - `http://example.test/live/9` above all,
the leaked capture netguard was written for (BUGS.md 2026-07-20).
"""
import copy
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.config import _DEFAULTS  # noqa: E402
from app.proc_utils import build_capture_cmd  # noqa: E402
from sim import Simulator, listening  # noqa: E402

from tests.support import netguard  # noqa: E402
from tests.support.sim_streams import library, live_ids, origin, stream_world  # noqa: E402

CFG = copy.deepcopy(_DEFAULTS)


class _Plain(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header('Content-Length', '0')
        self.end_headers()

    def log_message(self, *args):
        pass


class SimulatorExceptionTests(unittest.TestCase):
    def setUp(self):
        self.lib = library()
        netguard.drain_violations()
        self.addCleanup(netguard.drain_violations)
        self.tmp = tempfile.mkdtemp(prefix='netguard-sim-')
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def start(self, **kw) -> Simulator:
        sim = Simulator(stream_world(), content=self.lib, **kw).start()
        self.addCleanup(sim.stop)
        return sim

    def url(self, sim) -> str:
        world = sim.world
        return sim.stream_urls('cove-tv', 'reeduser')[live_ids(world)[0]]

    def capture(self, url, seconds=3):
        """The recorder's own argv, bounded so the test ends."""
        out = os.path.join(self.tmp, 'cap.ts')
        cmd = build_capture_cmd(CFG, url, out, seconds, pace_realtime=True,
                                keep_video_track=True)
        with open(os.path.join(self.tmp, 'cap.log'), 'wb') as err:
            rc = subprocess.run(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                stderr=err, timeout=60).returncode
        return rc, os.path.getsize(out) if os.path.exists(out) else 0

    def assertBlocked(self, url):
        with self.assertRaises(netguard.NetworkAccessInTestError):
            self.capture(url)
        found = netguard.drain_violations()
        self.assertEqual(len(found), 1, found)
        self.assertIn(url, found[0])

    def test_a_running_simulator_can_be_captured_with_the_recorders_argv(self):
        sim = self.start()
        rc, size = self.capture(self.url(sim))
        self.assertEqual(rc, 0)
        self.assertGreater(size, 50_000)
        self.assertEqual(netguard.drain_violations(), [])

    def test_an_hls_capture_from_a_simulator_is_allowed_too(self):
        sim = self.start()
        sid = live_ids(sim.world)[0]
        rc, size = self.capture(f'{origin(self.url(sim))}/live/fernuser/fernpass1/{sid}.m3u8',
                                seconds=2)
        self.assertEqual(rc, 0)
        self.assertGreater(size, 30_000)
        self.assertEqual(netguard.drain_violations(), [])

    def test_a_live_loopback_server_that_is_not_a_simulator_is_refused(self):
        srv = ThreadingHTTPServer(('127.0.0.1', 0), _Plain)
        threading.Thread(target=srv.serve_forever, kwargs={'poll_interval': 0.05},
                         daemon=True).start()
        self.addCleanup(srv.server_close)
        self.addCleanup(srv.shutdown)
        self.start()        # a simulator is running, just not on this port
        self.assertBlocked(f'http://127.0.0.1:{srv.server_address[1]}/live/u/p/1.ts')

    def test_the_escape_netguard_was_written_for_is_still_refused(self):
        self.start()
        self.assertBlocked('http://example.test/live/9')

    def test_the_port_must_be_on_the_address_the_simulator_bound(self):
        sim = self.start()
        url = self.url(sim)
        host, port = url.split('/')[2].split(':')
        self.assertTrue(host.startswith('127.0.0.'))
        other = '127.0.0.200' if host != '127.0.0.200' else '127.0.0.201'
        self.assertBlocked(url.replace(f'{host}:{port}', f'{other}:{port}'))
        self.assertBlocked(url.replace(f'{host}:{port}', f'localhost:{port}'))

    def test_a_stopped_simulator_is_refused(self):
        sim = self.start()
        url = self.url(sim)
        sim.stop()
        self.assertNotIn(tuple(url.split('/')[2].split(':')), {(a, str(p)) for a, p in listening()})
        self.assertBlocked(url)

    def test_a_second_url_inside_one_argv_word_is_judged_on_its_own(self):
        """ffmpeg's `concat:a|b` protocol opens every URL in the word, and a URL can sit in
        another's query string; the guard reads each one, so a simulator URL cannot carry a
        foreign one past it (dev/changelog/1233). The embedded URL here is a live loopback
        server that is not a simulator, so a guard that let it through would still reach
        nothing off the box."""
        srv = ThreadingHTTPServer(('127.0.0.1', 0), _Plain)
        threading.Thread(target=srv.serve_forever, kwargs={'poll_interval': 0.05},
                         daemon=True).start()
        self.addCleanup(srv.server_close)
        self.addCleanup(srv.shutdown)
        sim = self.start()
        other = f'http://127.0.0.1:{srv.server_address[1]}/live/u/p/1.ts'
        for word in (f'concat:{self.url(sim)}|{other}', f'{self.url(sim)}?next={other}'):
            self.assertFalse(netguard._argv_is_simulator_only(word), word)
            with self.assertRaises(netguard.NetworkAccessInTestError):
                subprocess.run(['ffmpeg', '-i', word, '-f', 'null', '-'], timeout=30)
            found = netguard.drain_violations()
            self.assertEqual(len(found), 1, found)
            self.assertIn(other, found[0])
        self.assertTrue(netguard._argv_is_simulator_only(self.url(sim) + '?a=1,b=2'))

    def test_one_url_that_is_not_a_simulator_blocks_the_whole_argv(self):
        sim = self.start()
        with self.assertRaises(netguard.NetworkAccessInTestError):
            subprocess.run(['ffmpeg', '-i', self.url(sim), '-i', 'http://example.test/x',
                            '-f', 'null', '-'], timeout=30)
        self.assertEqual(len(netguard.drain_violations()), 1)

    def test_the_socket_hook_is_unchanged(self):
        self.start()
        with self.assertRaises(netguard.NetworkAccessInTestError):
            socket.create_connection(('192.0.2.1', 80), timeout=1)
        self.assertEqual(len(netguard.drain_violations()), 1)


if __name__ == '__main__':
    unittest.main()
