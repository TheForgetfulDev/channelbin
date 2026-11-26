"""The IPTV output's public port: a second listener in this process that answers the Xtream
paths and nothing else, for a reverse proxy to put on the internet (dev/changelog/1761,
dev/docs/DESIGN-output-feed.md section 6.5).

The same process, not a second one, because a play has to share the recorder's connection
seats and preemption (app/connection_limits.py), which live here. A second port rather than
one port and a path allowlist in the proxy, because then one mistake in the proxy puts the
whole web UI on the internet. The allowlist is here, server-side: a request is answered only
when its path matches one of the output's own routes (PUBLIC_ENDPOINTS) or is ChannelBin's own
logo, and everything else - the pages, the API, every other static file, a redirect to a
tidier path - is a bare 404 that never reaches the app.

A request that passes is marked in its environ (PUBLIC_ENVIRON_KEY): the output never sends a
play from here a 302 to the provider, whose stream URL carries the provider login
(output_feed.relays_for).
"""
import logging
import threading
from typing import Optional

from werkzeug.exceptions import HTTPException
from werkzeug.routing import RequestRedirect

log = logging.getLogger(__name__)

PUBLIC_ENVIRON_KEY = 'channelbin.public_port'

#: The output's routes the public port answers (app/routes/output_feed.py). Named one by one,
#: so a route added to that blueprint is not public until it is added here
#: (tests/test_public_listener.py holds the two lists equal).
PUBLIC_ENDPOINTS = frozenset({
    'output_feed.player_api', 'output_feed.get_php', 'output_feed.xmltv',
    'output_feed.logo', 'output_feed.live',
})

_NOT_FOUND = b'Not found\n'

#: What the listener is doing (status()['state']): not open (no port set, or set since the
#: process started), open, or could not be opened (with the reason in 'error').
STATE_OFF = 'off'
STATE_SERVING = 'serving'
STATE_FAILED = 'failed'
STATES = (STATE_OFF, STATE_SERVING, STATE_FAILED)

# Reentrant: a SIGTERM landing while start() holds it runs run.py's shutdown, and stop(), on
# the same thread.
_lock = threading.RLock()
# Read by the readiness check and the IPTV Output page.
_status = {'state': STATE_OFF, 'host': '', 'port': 0, 'error': ''}
_server = None
_thread: Optional[threading.Thread] = None


def is_public(request) -> bool:
    """Whether `request` came in on the public port."""
    return bool(request.environ.get(PUBLIC_ENVIRON_KEY))


def public_port(fcfg: dict) -> int:
    """`integrations.output_feed.public_port` as a whole number, 0 (off) when unset or unreadable."""
    try:
        return int(fcfg.get('public_port') or 0)
    except (TypeError, ValueError):
        log.warning('integrations.output_feed.public_port is not a whole number (%r); the '
                    'public port is off', fcfg.get('public_port'))
        return 0


def public_host(fcfg: dict) -> str:
    return str(fcfg.get('public_host') or '0.0.0.0').strip() or '0.0.0.0'


class FeedOnly:
    """The WSGI app the public port serves: `flask_app` behind the allowlist. It calls the
    Flask app itself, so `app.wsgi_app` and the ProxyFix around it still apply."""

    def __init__(self, flask_app):
        self.flask_app = flask_app
        from .output_feed import OWN_LOGO_PATH
        self.own_logo = OWN_LOGO_PATH

    def allowed(self, environ) -> bool:
        path = environ.get('PATH_INFO') or ''
        if path == self.own_logo:
            return environ.get('REQUEST_METHOD', 'GET') in ('GET', 'HEAD')
        try:
            endpoint, _args = self.flask_app.url_map.bind_to_environ(environ).match()
        except (HTTPException, RequestRedirect):
            # No route, a method the route does not take, or a redirect to a tidier path,
            # which the player would then follow to somewhere this port does not answer.
            return False
        return endpoint in PUBLIC_ENDPOINTS

    def __call__(self, environ, start_response):
        if not self.allowed(environ):
            start_response('404 NOT FOUND', [('Content-Type', 'text/plain; charset=utf-8'),
                                             ('Content-Length', str(len(_NOT_FOUND)))])
            return [_NOT_FOUND]
        environ[PUBLIC_ENVIRON_KEY] = True
        return self.flask_app(environ, start_response)


def _bind(host: str, port: int):
    """A listening socket on host:port. Bound here rather than by make_server, which prints
    to stderr and exits the whole process when the port is in use."""
    import socket
    from werkzeug.serving import get_sockaddr, select_address_family
    family = select_address_family(host, port)
    sock = socket.socket(family, socket.SOCK_STREAM)
    try:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind(get_sockaddr(host, port, family))
        sock.listen(128)
    except OSError:
        sock.close()
        raise
    return sock


def _web_port(cfg: dict) -> int:
    try:
        return int((cfg.get('flask') or {}).get('port') or 0)
    except (TypeError, ValueError):
        return 0


def _failed(host: str, port: int, why: str) -> dict:
    """Record and log a public port that could not be opened. Caller holds _lock."""
    _status.update(state=STATE_FAILED, host=host, port=port, error=why)
    log.error('The IPTV output\'s public port %s:%s could not be opened (%s). The web UI is '
              'unaffected; nothing answers on the public port until this is fixed and '
              'ChannelBin is restarted.', host, port, why)
    return dict(_status)


def start(flask_app, cfg: dict) -> dict:
    """Start the public port when one is set. Never raises: a port that cannot be bound is an
    ERROR in the log and a 'failed' status the readiness check reports, and the web UI starts
    regardless. Returns the status."""
    global _server, _thread
    from werkzeug.serving import make_server
    fcfg = (cfg.get('integrations') or {}).get('output_feed') or {}
    port = public_port(fcfg)
    host = public_host(fcfg)
    with _lock:
        if _server is not None:
            return dict(_status)
        if not port:
            _status.update(state=STATE_OFF, host='', port=0, error='')
            return dict(_status)
        if port == _web_port(cfg):
            # Bound first, it would leave the web UI unable to open its own port.
            return _failed(host, port, 'it is the web UI\'s own port')
        try:
            sock = _bind(host, port)
        except OSError as exc:
            return _failed(host, port, exc.strerror or str(exc))
        try:
            server = make_server(host, port, FeedOnly(flask_app), threaded=True,
                                 fd=sock.fileno())
        finally:
            # make_server keeps a duplicate of the descriptor.
            sock.close()
        _server = server
        _thread = threading.Thread(target=server.serve_forever, name='output-feed-public-port',
                                   daemon=True)
        _thread.start()
        _status.update(state=STATE_SERVING, host=host, port=port, error='')
    log.info('IPTV output public port listening on %s:%s - it answers only the IPTV output '
             '(player_api.php, get.php, xmltv.php, /live/, /logo/) and every play on it is '
             'relayed through ChannelBin', host, port)
    return dict(_status)


def stop():
    """Stop the public port, if it is serving. Called on shutdown (run.py)."""
    global _server, _thread
    with _lock:
        server, thread = _server, _thread
        _server = _thread = None
        if server is not None:
            _status.update(state=STATE_OFF, error='')
    if server is None:
        return
    server.shutdown()
    server.server_close()
    if thread is not None:
        thread.join(timeout=5)


def status() -> dict:
    """{'state': one of STATES, 'host', 'port', 'error'}."""
    with _lock:
        return dict(_status)


def reset_for_tests():
    stop()
    with _lock:
        _status.update(state=STATE_OFF, host='', port=0, error='')
