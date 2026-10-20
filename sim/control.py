"""The control API: a running simulator's fixture operations over HTTP, on a port of its own.

In-process tests drive a `Simulator` directly (`inject()`, `set_login_status()`, `ledger`).
A simulator in a container is driven from another one, so the same operations are offered
here as JSON, on their own listening socket and never on a provider-facing host: a client of
the provider cannot reach them by any URL the world hands out (DESIGN-simulator.md §3.6).

    GET    /health                         {"success": true, "sim_version": ...}
    GET    /accounts                       every (panel, login) as a user would add it
    GET    /stream-urls?panel=P[&username=U]   {stream id: URL}, faults applied, with each
                                           channel's kind and ledger name
    GET    /ledger[?since=N]               rows with seq > N, oldest first
    GET    /connections?login=U            seats the login holds at its backend
    GET    /faults                         the faults being applied, with their counts
    POST   /faults                         {"kind", "target", ...settings} -> {"id"}
    DELETE /faults[/ID]                    stop one fault, or every fault
    POST   /logins/U/status                {"status": "Banned"}
    POST   /logins/U/cut                   end the login's open stream connections now
                                           ({"why": "..."} names it in the ledger)
    POST   /clock                          {"at": ISO time or null for real time}
    GET    /dns                            names, which are down, query counts
    POST   /dns/NAME/down, /dns/NAME/up    stop or resume resolving a name
    POST   /dns/alias                      {"name", "target"}: a second name for a host
    POST   /check                          the answer key over recording files the simulator
                                           can read: {"segments": [paths], "window", "serving",
                                           "channels", "min_gap"}

Answers follow the shape ChannelBin's own API uses: `{"success": true, ...}`, or
`{"error": "..."}` with a 4xx status for a request that could not be carried out.
"""
import datetime as _dt
import json
import logging
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

from . import SIM_VERSION
from .faults import FaultError

log = logging.getLogger(__name__)

THREAD_NAME = 'sim-control'


class ControlError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status


def _time(value):
    if value is None:
        return None
    t = _dt.datetime.fromisoformat(value)
    if t.tzinfo is not None:
        t = t.astimezone(_dt.timezone.utc).replace(tzinfo=None)
    return t


def _fault_dict(f) -> dict:
    t = f.trigger
    return {'id': f.id, 'kind': f.kind.name, 'target': f.target, 'describe': f.fault.describe(),
            'endpoints': list(f.endpoints), 'params': f.params, 'seen': f.seen, 'hits': f.hits,
            'start': t.start.isoformat() if t.start else None,
            'end': t.end.isoformat() if t.end else None, 'after': t.after, 'times': t.times}


class Control:
    """The operations, as plain methods taking and returning JSON-shaped values, so they are
    testable without a socket. `dns` is the simulator's DNS responder, if it has one."""

    def __init__(self, sim, dns=None):
        self.sim = sim
        self.dns = dns
        self._lock = threading.Lock()

    def dispatch(self, method: str, target: str, body: dict) -> dict:
        parts = urlsplit(target)
        path = [p for p in parts.path.split('/') if p]
        query = {k: v[0] for k, v in parse_qs(parts.query).items()}
        sim = self.sim
        if method == 'GET' and path == ['health']:
            return {'sim_version': SIM_VERSION, 'seed': sim.world.seed}
        if method == 'GET' and path == ['accounts']:
            return {'accounts': [vars(a) for a in sim.accounts()],
                    'hosts': {h: sim.url(h) for h in sim.world.host_names()}}
        if method == 'GET' and path == ['stream-urls']:
            if 'panel' not in query:
                raise ControlError(400, 'name a panel: /stream-urls?panel=P')
            try:
                urls = sim.stream_urls(query['panel'], query.get('username'))
            except KeyError as exc:
                raise ControlError(404, f'no such panel or login: {exc}') from None
            backend = sim.world.panels[query['panel']].backend
            kinds = {c.stream_id: c.kind for c in sim.world.lineups[backend].channels}
            return {'urls': {str(k): v for k, v in urls.items()},
                    'kinds': {str(k): kinds.get(k) for k in urls},
                    'channels': {str(k): f'{backend}:{k}' for k in urls}}
        if method == 'GET' and path == ['ledger']:
            since = int(query.get('since', 0))
            rows = [r.as_dict() for r in sim.ledger.entries() if r.seq > since]
            return {'rows': rows}
        if method == 'GET' and path == ['connections']:
            login = query.get('login')
            if login not in sim.world.logins:
                raise ControlError(404, f'no login {login!r}')
            return {'login': login, 'connections': sim.connections(login)}
        if path[:1] == ['faults']:
            return self._faults(method, path[1:], body)
        if method == 'POST' and len(path) == 3 and path[0] == 'logins' and path[2] == 'status':
            try:
                sim.set_login_status(path[1], body.get('status'))
            except (KeyError, ValueError) as exc:
                raise ControlError(400, str(exc).strip('"\'')) from None
            return {'login': path[1], 'status': body.get('status')}
        if method == 'POST' and len(path) == 3 and path[0] == 'logins' and path[2] == 'cut':
            try:
                n = sim.cut(path[1], body.get('why') or 'closed by the provider')
            except KeyError as exc:
                raise ControlError(404, str(exc).strip('"\'')) from None
            return {'login': path[1], 'ended': n}
        if method == 'POST' and path == ['clock']:
            try:
                sim.set_clock(_time(body.get('at')))
            except (TypeError, ValueError) as exc:
                raise ControlError(400, f'at must be an ISO time or null: {exc}') from None
            return {'now': sim.provider.now().isoformat()}
        if path[:1] == ['dns']:
            return self._dns(method, path[1:], body)
        if method == 'POST' and path == ['check']:
            return self._check(body)
        raise ControlError(404, f'no control operation {method} {parts.path}')

    def _faults(self, method, rest, body):
        sim = self.sim
        if method == 'GET' and not rest:
            return {'faults': [_fault_dict(f) for f in sim.faults.active()]}
        if method == 'POST' and not rest:
            settings = dict(body)
            kind, target = settings.pop('kind', None), settings.pop('target', None)
            if not kind or not target:
                raise ControlError(400, 'a fault needs a kind and a target')
            try:
                f = sim.inject(kind, target, **settings)
            except (FaultError, TypeError) as exc:
                raise ControlError(400, str(exc)) from None
            return {'fault': _fault_dict(f)}
        if method == 'DELETE' and not rest:
            sim.clear()
            return {'cleared': 'all'}
        if method == 'DELETE' and len(rest) == 1 and rest[0].isdigit():
            fid = int(rest[0])
            for f in sim.faults.active():
                if f.id == fid:
                    sim.clear(f)
                    return {'cleared': fid}
            raise ControlError(404, f'no fault {fid} is being applied')
        raise ControlError(404, 'faults: GET, POST, DELETE or DELETE /faults/ID')

    def _dns(self, method, rest, body):
        if self.dns is None:
            raise ControlError(409, 'this simulator runs no DNS responder')
        try:
            if method == 'GET' and not rest:
                return self.dns.state()
            if method == 'POST' and rest == ['alias']:
                self.dns.alias(body.get('name') or '', body.get('target') or '')
                return self.dns.state()
            if method == 'POST' and len(rest) == 2 and rest[1] in ('down', 'up'):
                (self.dns.down if rest[1] == 'down' else self.dns.up)(rest[0])
                return self.dns.state()
        except KeyError as exc:
            raise ControlError(404, str(exc).strip('"\'')) from None
        raise ControlError(404, 'dns: GET, POST /dns/alias, POST /dns/NAME/down or /up')

    def _check(self, body):
        from . import answer_key as ak
        segments = body.get('segments')
        if not isinstance(segments, list) or not segments:
            raise ControlError(400, 'segments must be a list of file paths')
        window = body.get('window')
        if window is not None:
            window = tuple(_time(w) if isinstance(w, str) else w for w in window)
        try:
            answer = ak.check(self.sim, segments, window=window,
                              channels=body.get('channels'), serving=body.get('serving'),
                              min_gap=float(body.get('min_gap', 0.0)))
        except ak.AnswerKeyError as exc:
            raise ControlError(409, str(exc)) from None
        return {'answer': answer.as_dict(), 'describe': answer.describe()}


class _Handler(BaseHTTPRequestHandler):
    protocol_version = 'HTTP/1.1'
    server_version = 'sim-control'
    sys_version = ''

    def _serve(self):
        length = int(self.headers.get('Content-Length') or 0)
        raw = self.rfile.read(length) if length else b''
        status, out = 200, None
        try:
            body = json.loads(raw) if raw else {}
            if not isinstance(body, dict):
                raise ControlError(400, 'the body must be a JSON object')
            out = {'success': True, **self.server.control.dispatch(self.command, self.path,
                                                                     body)}
        except ControlError as exc:
            status, out = exc.status, {'error': str(exc)}
        except ValueError as exc:
            status, out = 400, {'error': f'bad request: {exc}'}
        except Exception as exc:
            # The boundary of one request: answered as a 500 naming the error, and logged
            # with its traceback, never left to kill the connection unexplained.
            log.exception('control: %s %s raised', self.command, self.path)
            status, out = 500, {'error': f'{type(exc).__name__}: {exc}'}
        data = json.dumps(out, default=str).encode('utf-8')
        self.send_response(status)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    do_GET = do_POST = do_DELETE = _serve

    def log_message(self, fmt, *args):
        log.debug('control: ' + fmt, *args)


class ControlServer:
    """The control API on (address, port), serving `control` until stopped."""

    def __init__(self, control: Control, address: str = '127.0.0.1', port: int = 0):
        self._srv = ThreadingHTTPServer((address, port), _Handler)
        self._srv.daemon_threads = True
        self._srv.control = control
        self.address = self._srv.server_address[:2]
        self._thread = None

    def start(self) -> 'ControlServer':
        self._thread = threading.Thread(target=self._srv.serve_forever,
                                        kwargs={'poll_interval': 0.1}, name=THREAD_NAME,
                                        daemon=True)
        self._thread.start()
        return self

    def stop(self) -> None:
        if self._thread is not None:
            self._srv.shutdown()
            self._thread.join(timeout=5)
            self._thread = None
        self._srv.server_close()
