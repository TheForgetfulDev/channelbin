"""The simulator's sockets: one HTTP server per host of a world.

Each host - a panel's API host, a backend's stream hosts, a guide site, the logo host - gets
its own listening socket, by default each on its own loopback address (127.0.0.2, 127.0.0.3
and on; Linux routes all of 127/8 to the loopback interface), so an API host and a stream
host differ the way they do on a real provider. A host the world declares down is bound but
never listened on, so a connection to it is refused at once.

`stop()` ends every connection still open and joins every thread the simulator started; a
simulator that has been stopped leaves nothing running.

The two wire faults are applied here, as the body is written: a chunked body whose
connection closes where the next chunk length should be, and a slow drip that pauses before
each piece (sim/faults.py). A live stream (sim/streams.py) is written here too, close-delimited,
until the client leaves, its seat is taken away or the simulator stops; two stream faults are
the server's to carry out, because they are about the socket rather than the bytes - waiting
before the first byte, and holding a request without ever answering it.

`listening()` names every address and port a simulator in this process has bound and not yet
released, so a test harness can let a child process reach a simulator and nothing else.
"""
import datetime as _dt
import logging
import os
import socket
import socketserver
import sys
import threading
import time
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from .faults import (EP_PLAYLIST, KIND_SLOW_DRIP, KIND_TRUNCATED_CHUNKED, ActiveFault,
                     FaultBook, make_fault)
from .content import Library
from .ledger import Ledger, split_target
from .provider import Layout, Provider, Response, spread_addresses
from .stream_faults import peer_gone
from .streams import ChannelClock, Headend
from .world import LOGIN_STATUSES

log = logging.getLogger(__name__)

THREAD_PREFIX = 'sim-'
_CHUNK = 16384

# (address, port) pairs bound by every simulator in this process that has not stopped.
_LISTENING: dict = {}
_LISTENING_LOCK = threading.Lock()


def listening() -> frozenset:
    """Every (address, port) a simulator in this process has bound and still holds."""
    with _LISTENING_LOCK:
        return frozenset(p for places in _LISTENING.values() for p in places)


def _utc_now() -> _dt.datetime:
    return _dt.datetime.now(_dt.timezone.utc).replace(tzinfo=None)


@dataclass(frozen=True)
class AccountView:
    """One login on one panel, as a user would add it to a client: what to type where."""
    panel: str
    kind: str
    username: str
    password: str
    status: str
    base_url: str | None    # Xtream: the API origin. None for a plain M3U panel.
    m3u_url: str            # the playlist
    epg_url: str            # the panel's own guide


class _Handler(BaseHTTPRequestHandler):
    protocol_version = 'HTTP/1.1'
    server_version = 'nginx'
    sys_version = ''
    # An idle keep-alive connection is closed after this long; stop() closes them sooner.
    timeout = 30

    def do_GET(self):
        self._answer(send_body=True)

    def do_HEAD(self):
        self._answer(send_body=False)

    def _answer(self, send_body: bool):
        started = time.monotonic()
        sim = self.server.simulator
        host = self.server.host_name
        try:
            resp = sim.provider.handle(host, self.command, self.path, self.headers)
        except Exception:
            # The boundary of one request: whatever went wrong is answered as a 500 and
            # logged with its traceback, never left to kill the connection unexplained.
            log.exception('simulator: %s %s on %s raised', self.command, self.path, host)
            resp = Response(500, note='the simulator raised; see its log')
        wire = {f.kind.name: f for f in resp.wire}
        cut, drip = wire.get(KIND_TRUNCATED_CHUNKED), wire.get(KIND_SLOW_DRIP)
        body = resp.body if send_body else b''
        if cut is not None:
            body = body[:int(len(body) * cut.params['fraction'])]
        # Recorded before anything reaches the client, then amended once the write ends: a
        # client that has its whole answer must find the request in the ledger, and with the
        # row appended after the last byte a test reading the ledger at once could miss it.
        path, query = split_target(self.path)
        row = sim.ledger.record(
            at=sim.provider.now().replace(tzinfo=_dt.timezone.utc).timestamp(), host=host,
            role=sim.provider.role(host), client=self.client_address[0], method=self.command,
            path=path, query=query, endpoint=resp.endpoint,
            user_agent=self.headers.get('User-Agent') or '', login=resp.login,
            status=resp.status, bytes=len(body), seconds=round(time.monotonic() - started, 6),
            note=resp.note, faults=resp.faults, channel=resp.channel, offered=resp.offered,
            profile=resp.profile, withheld=resp.withheld,
            open=resp.stream is not None or resp.hold is not None)
        self._sent = 0
        note = resp.note
        if resp.hold is not None:
            self._hold(resp, row, started, sim)
            return
        if resp.delay and sim._stopping.wait(resp.delay):
            self.close_connection = True
            if resp.stream is not None:
                resp.stream.close()     # it holds a seat already
            sim.ledger.amend(row, bytes=0, seconds=round(time.monotonic() - started, 6),
                             note='; '.join(n for n in (note, 'the simulator stopped') if n),
                             open=False)
            return
        if resp.stream is not None:
            self._write_stream(resp, row, started, sim)
            return
        try:
            self.send_response(resp.status)
            self.send_header('Content-Type', resp.content_type)
            for name, value in resp.headers.items():
                self.send_header(name, value)
            if cut is not None:
                self.send_header('Transfer-Encoding', 'chunked')
                self.end_headers()
                self._write_cut_chunks(body)
                self.close_connection = True
            else:
                self.send_header('Content-Length', str(len(resp.body)))
                self.end_headers()
                if drip is not None:
                    self._write_drip(body, drip.params['chunk_bytes'],
                                     drip.params['gap_seconds'], sim._stopping)
                elif body:
                    self.wfile.write(body)
                    self._sent = len(body)
        except (BrokenPipeError, ConnectionResetError) as exc:
            note = f'{note}; client went away ({type(exc).__name__})'.lstrip('; ')
        sim.ledger.amend(row, bytes=self._sent, seconds=round(time.monotonic() - started, 6),
                         note=note)

    def _write_stream(self, resp, row, started, sim):
        """A live stream: no length, written until the client leaves, its seat is taken away
        or the simulator stops; the connection closes with it."""
        stream = resp.stream
        note = resp.note
        self.close_connection = True
        stream.client_gone = lambda: peer_gone(self.connection)
        ended = 'the simulator raised; see its log'
        try:
            self.send_response(resp.status)
            self.send_header('Content-Type', resp.content_type)
            self.send_header('Connection', 'close')
            for name, value in resp.headers.items():
                self.send_header(name, value)
            self.end_headers()
            for piece in stream.pieces(sim._stopping):
                self.wfile.write(piece)
                self._sent += len(piece)
            ended = stream.ended
        except (BrokenPipeError, ConnectionResetError, TimeoutError) as exc:
            ended = f'client went away ({type(exc).__name__})'
        finally:
            stream.close()
            # In the finally, so a row is never left open by an error nobody expected.
            late = tuple(n for n in stream.applied if n not in row.faults)
            sim.ledger.amend(row, bytes=self._sent,
                             seconds=round(time.monotonic() - started, 6),
                             note='; '.join(n for n in (note, ended) if n),
                             offered=stream.offered(), faults=row.faults + late,
                             withheld=stream.withheld, open=False)

    def _hold(self, resp, row, started, sim):
        """Accept and never answer: wait until the client gives up, the hold runs out or the
        simulator stops, then close without a byte."""
        self.close_connection = True
        end = time.monotonic() + resp.hold
        why = f'held {resp.hold:g} s, then closed'
        while time.monotonic() < end:
            if sim._stopping.wait(0.05):
                why = 'the simulator stopped'
                break
            if peer_gone(self.connection):
                why = 'the client gave up waiting'
                break
        sim.ledger.amend(row, bytes=0, seconds=round(time.monotonic() - started, 6),
                         note=f'{resp.note}; {why}', open=False)

    def _write_cut_chunks(self, keep: bytes):
        for i in range(0, len(keep), _CHUNK):
            piece = keep[i:i + _CHUNK]
            self.wfile.write(b'%x\r\n%s\r\n' % (len(piece), piece))
            self._sent += len(piece)
        # Nothing follows: the connection closes where the next chunk's length line should
        # be, so the client reads an empty length - what urllib3 2.0.7 reports as
        # InvalidChunkLength(got length b'', 0 bytes read) and 2.8.0 as "Response ended
        # prematurely".

    def _write_drip(self, body: bytes, chunk: int, gap: float, stopping: threading.Event):
        for i in range(0, len(body), chunk):
            if i and stopping.wait(gap):
                return
            self.wfile.write(body[i:i + chunk])
            self._sent += len(body[i:i + chunk])

    def log_message(self, fmt, *args):
        log.debug('simulator %s: ' + fmt, self.server.host_name, *args)


class _HostServer(ThreadingHTTPServer):
    # Handler threads are tracked and joined by server_close(), so stop() can prove none is
    # left behind; the connections they serve are closed first so none waits on its timeout.
    daemon_threads = False
    block_on_close = True
    allow_reuse_address = True

    def __init__(self, address, simulator, host_name):
        self.simulator = simulator
        self.host_name = host_name
        self._open = set()
        self._open_lock = threading.Lock()
        super().__init__(address, _Handler)

    def server_bind(self):
        # HTTPServer.server_bind asks socket.getfqdn() for the bound address's name: a reverse
        # DNS query per host. On a network with no resolver - the full stack's internal network
        # - each one waits out its timeout, about 5 s a host, before the host listens. Nothing
        # here reads server_name, so the address stands in for it.
        socketserver.TCPServer.server_bind(self)
        self.server_name, self.server_port = self.server_address[:2]

    def process_request(self, request, client_address):
        with self._open_lock:
            self._open.add(request)
        super().process_request(request, client_address)

    def shutdown_request(self, request):
        with self._open_lock:
            self._open.discard(request)
        super().shutdown_request(request)

    def handle_error(self, request, client_address):
        # A client that hangs up mid-answer - a read timeout, a cap on how much it reads - is
        # what several faults exist to provoke, so it is logged quietly rather than printed
        # as a traceback the way socketserver does by default.
        exc = sys.exc_info()[1]
        if isinstance(exc, (BrokenPipeError, ConnectionResetError)):
            log.debug('simulator %s: %s went away (%s)', self.host_name, client_address[0],
                      type(exc).__name__)
            return
        log.exception('simulator %s: error serving %s', self.host_name, client_address[0])

    def close_open_connections(self):
        with self._open_lock:
            open_now = list(self._open)
        for sock in open_now:
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass  # its handler closed it between the snapshot and here


class Simulator:
    """A world served over HTTP.

    `address` is the first address handed out; with `spread` each host takes the next one,
    without it every host shares `address` on its own port. `port` 0 asks the system for a
    free port per host. `clock` is None for real time, a naive-UTC datetime to hold the clock
    still, or a callable returning one.

    `domain` names every host `<host>.<domain>` in the URLs it hands out, for a network where
    a DNS responder answers for those names (`dns_names()`, sim/dns.py); the addresses need
    not be loopback then, since the container running the simulator holds them itself.
    """

    def __init__(self, world, *, address: str = '127.0.0.2', spread: bool = True, port: int = 0,
                 clock=None, content: Library | str | None = None, domain: str | None = None):
        self.world = world
        self.domain = domain
        # Streams are cut from this library of rendered loops (sim/content.py); a directory
        # name, or None for the default cache. Nothing is rendered unless asked.
        self.library = content if isinstance(content, Library) else Library(content)
        self.headend = None
        self.address = address
        self.spread = spread
        self.port = port
        self.ledger = Ledger()
        self.faults = FaultBook(world.faults)
        self.provider = None
        self.layout = None
        self._clock = clock
        # Set by stop() before anything else, so a slow drip mid-pause ends at once.
        self._stopping = threading.Event()
        self._servers = []
        self._threads = []
        self._down = []
        self._started = False

    # --- the clock ------------------------------------------------------------------------

    def set_clock(self, clock) -> None:
        """Set the simulator's clock. Every channel's stream moves to the new time and plays
        on from it in real time."""
        self._clock = clock
        if self.headend is not None:
            self.headend.clock.set(self._now())

    def _now(self) -> _dt.datetime:
        c = self._clock
        if c is None:
            return _utc_now()
        if callable(c):
            return c()
        return c

    # --- lifecycle ------------------------------------------------------------------------

    def start(self) -> 'Simulator':
        if self._started:
            raise RuntimeError('this simulator is already started')
        names = self.world.host_names()
        if self.spread:
            addresses = spread_addresses(self.address, len(names), loopback=not self.domain)
        else:
            addresses = [self.address] * len(names)
        places = {}
        try:
            for i, (name, addr) in enumerate(zip(names, addresses)):
                port = self.port if (self.spread or not self.port) else self.port + i
                if name in self.world.hosts_down:
                    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                    self._down.append(sock)
                    sock.bind((addr, port))
                    places[name] = sock.getsockname()[:2]
                    continue
                srv = _HostServer((addr, port), self, name)
                self._servers.append(srv)
                places[name] = srv.server_address[:2]
        except OSError as exc:
            self._close_sockets()
            hint = (' Pass spread=False (the CLI\'s --single-address) to put every host on one '
                    'address.' if self.spread else '')
            raise OSError(exc.errno, f'could not bind {name} on {addr}: {exc.strerror}.{hint}'
                          ) from exc
        self.layout = Layout(places, {n: self.host_name(n) for n in names} if self.domain
                             else None)
        self.provider = Provider(self.world, self.layout, self._now, self.faults)
        self.headend = Headend(self.world, self.library, ChannelClock(self._now()),
                               self.provider.status, self.faults, origin=self.layout.origin)
        self.provider.headend = self.headend
        self._stopping.clear()
        with _LISTENING_LOCK:
            _LISTENING[id(self)] = frozenset(places.values())
        for srv in self._servers:
            t = threading.Thread(target=srv.serve_forever, kwargs={'poll_interval': 0.1},
                                 name=f'{THREAD_PREFIX}{srv.host_name}', daemon=True)
            t.start()
            self._threads.append(t)
        self._started = True
        return self

    def stop(self) -> None:
        """Stop serving, close every connection and socket, and join every thread. Safe to call
        more than once, and on a simulator that never started."""
        with _LISTENING_LOCK:
            _LISTENING.pop(id(self), None)
        self._stopping.set()
        if self.headend is not None:
            self.headend.stop()
        # shutdown() blocks until that server's loop notices, up to one poll interval; asking
        # every server at once makes stop() cost one interval rather than one per host.
        stoppers = [threading.Thread(target=srv.shutdown, name=f'{THREAD_PREFIX}stop')
                    for srv in self._servers]
        for t in stoppers:
            t.start()
        for t in stoppers:
            t.join()
        for srv in self._servers:
            srv.close_open_connections()
        self._close_sockets()
        for t in self._threads:
            t.join(timeout=10)
        stuck = [t.name for t in self._threads if t.is_alive()]
        self._threads = []
        self._started = False
        if stuck:
            raise RuntimeError(f'simulator threads did not stop: {", ".join(stuck)}')

    def _close_sockets(self):
        for srv in self._servers:
            srv.server_close()
        self._servers = []
        for sock in self._down:
            sock.close()
        self._down = []

    def __enter__(self) -> 'Simulator':
        return self.start()

    def __exit__(self, *exc) -> None:
        self.stop()

    # --- what to point a client at --------------------------------------------------------

    def url(self, host: str, path: str = '') -> str:
        return self.layout.origin(host) + path

    def host_name(self, host: str) -> str:
        """What a URL calls a world host: its DNS name under `domain`, else its address."""
        if self.domain:
            return f'{host}.{self.domain}'
        return self.layout.places[host][0]

    def dns_names(self) -> dict:
        """{DNS name: address} for every host, down ones included - a host that is down is
        refused at its address, which is a different failure from a name that will not
        resolve. Empty without a `domain`."""
        if not self.domain or self.layout is None:
            return {}
        return {self.host_name(h): addr for h, (addr, _port) in self.layout.places.items()}

    def accounts(self) -> list[AccountView]:
        """Every (panel, login) pair the world holds, panels in world order."""
        out = []
        for panel in self.world.panels.values():
            for lg in self.world.logins_on(panel.name):
                out.append(self._view(panel, lg))
        return out

    def account(self, panel: str, username: str | None = None) -> AccountView:
        for view in self.accounts():
            if view.panel == panel and (username is None or view.username == username):
                return view
        raise KeyError(f'no login {username or ""} on panel {panel!r}'.replace('  ', ' '))

    def _view(self, panel, lg) -> AccountView:
        origin = self.layout.origin(panel.name)
        u, p = lg.username, lg.password
        status = self.provider.status[u]
        if panel.kind == 'xtream':
            return AccountView(panel.name, panel.kind, u, p, status, origin,
                               f'{origin}/get.php?username={u}&password={p}&type=m3u_plus&output=ts',
                               f'{origin}/xmltv.php?username={u}&password={p}')
        guide = f'{origin}/guide/{u}/{p}.xml' + ('.gz' if panel.guide_gzip else '')
        return AccountView(panel.name, panel.kind, u, p, status, None,
                           f'{origin}/playlist/{u}/{p}.m3u', guide)

    def guide_site_url(self, site: str, gzipped: bool = False) -> str:
        return self.url(site, '/guide.xml.gz' if gzipped else '/guide.xml')

    def stream_urls(self, panel: str, username: str | None = None) -> dict:
        """{stream id: URL} for every channel a panel lists, as it lists them to one login,
        with the panel's faults applied."""
        lg = self.world.logins[username] if username else self.world.logins_on(panel)[0]
        spec = self.world.panels[panel]
        view = self.provider.view(panel, login=lg.username)
        return {row.channel.stream_id: self.provider.stream_url(spec, row, lg, view)
                for row in view.rows}

    # --- streams --------------------------------------------------------------------------

    def content_needed(self) -> list[str]:
        """Every loop this world's channels would serve."""
        return Headend(self.world, self.library, ChannelClock(self._now()), {},
                       self.faults).profiles_in_use()

    def prepare_content(self, names=None, log=None, jobs=None):
        """Render whichever of `names` (default: every loop the world uses) the library lacks.
        A stream whose loop is missing is answered 503, naming the command that renders it."""
        return self.library.build(names or self.content_needed(), jobs=jobs, log=log)

    def connections(self, username: str) -> int:
        """How many connections a login holds right now, at its backend."""
        return self.headend.connections(username) if self.headend else 0

    # --- standing state a test may change -------------------------------------------------

    def set_login_status(self, username: str, status: str) -> None:
        if status not in LOGIN_STATUSES:
            raise ValueError(f'status must be one of {", ".join(LOGIN_STATUSES)}')
        if username not in self.provider.status:
            raise KeyError(f'no login {username!r}')
        self.provider.status[username] = status

    def cut(self, username: str, why: str = 'closed by the provider') -> int:
        """End every stream connection the login holds right now - what a provider does when
        it drops a viewer, which a connection fault cannot reach because it is decided when a
        connection opens. Returns how many were ended."""
        if username not in self.world.logins:
            raise KeyError(f'no login {username!r}')
        return self.headend.cut(username, why) if self.headend else 0

    # --- faults ---------------------------------------------------------------------------

    def inject(self, kind: str, target: str, **settings) -> ActiveFault:
        """Start applying a fault (sim/faults.py). `settings` are `endpoints`, `start`, `end`,
        `after`, `times` and the kind's own; the fault is checked against this world first."""
        return self.faults.add(make_fault(self.world, kind, target, **settings))

    def clear(self, fault: ActiveFault | None = None) -> None:
        """Stop applying one fault, or every fault - those the world file declared too."""
        if fault is None:
            self.faults.clear()
        else:
            self.faults.remove(fault)

    def view(self, name: str, endpoint: str = EP_PLAYLIST, login: str | None = None):
        """What a panel or guide site renders from right now, faults applied: the rows,
        categories and guide a request to `endpoint` would be answered from."""
        return self.provider.view(name, endpoint, login)


def serve_for_test(testcase, world, **kwargs) -> Simulator:
    """A started simulator that `testcase` stops when it ends, pass or fail.

    For the test's duration loopback is added to `no_proxy`, so a proxy set in the
    environment cannot route a fetch of the simulator away from it."""
    from unittest import mock

    sim = Simulator(world, **kwargs).start()
    testcase.addCleanup(sim.stop)
    existing = [v for v in (os.environ.get('no_proxy'), os.environ.get('NO_PROXY')) if v]
    merged = ','.join(existing + ['127.0.0.0/8', 'localhost'])
    patcher = mock.patch.dict(os.environ, {'no_proxy': merged, 'NO_PROXY': merged})
    patcher.start()
    testcase.addCleanup(patcher.stop)
    return sim
