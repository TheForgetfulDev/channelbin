"""Whose word ChannelBin takes for a visitor's address: decided per request, by the port it
came in on and by who connected (dev/changelog/1769).

A reverse proxy says who its visitor is in X-Forwarded-For, and anyone else can send the same
header. Believing it from a caller that is not the proxy lets that caller pick a new address
for every request, which is the address both wrong-login lockouts count against
(app/auth.py::FailureTracker). So a request's forwarded headers are believed only when

  - the port it arrived on is switched to trust a proxy: `flask.behind_proxy` for the web
    UI's port, `integrations.output_feed.public_behind_proxy` for the IPTV output's public
    port (app/public_listener.py), and
  - the peer that connected is on `flask.trusted_proxies`, when that list holds anything. An
    empty list believes any peer on a switched-on port.

The policy is read once, at startup (all three keys need a restart). What each port has
since heard is kept here for the readiness check and the pages to show: a diagnostic only,
never an input to the decision.
"""
import ipaddress
import logging
import threading
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Optional

from werkzeug.middleware.proxy_fix import ProxyFix

from .config import PROXY_FIX_ARGS
from .public_listener import PUBLIC_ENVIRON_KEY

log = logging.getLogger(__name__)

PORT_WEB = 'web'
PORT_PUBLIC = 'public'
PORTS = (PORT_WEB, PORT_PUBLIC)

#: Set on every request: whether its forwarded headers were believed, so request.remote_addr
#: is the visitor's own address and not a proxy's.
TRUSTED_ENVIRON_KEY = 'channelbin.proxy_trusted'

#: app.config key holding the Policy this process runs, as against what config.yaml says now.
CONFIG_KEY = 'PROXY_TRUST'

#: Peers remembered per port for having had their forwarded headers ignored. Each is logged
#: once; past this many the log line is skipped, the count is not.
_MAX_IGNORED_PEERS = 64


def parse_address(text):
    """`text` as an ipaddress object, an IPv4-mapped IPv6 address as its IPv4 and a scope id
    dropped, or None when it is not an address. The one reading of an address for the proxy
    list and for the lockout key, so the two can never spell one caller two ways."""
    try:
        ip = ipaddress.ip_address(str(text or '').strip().split('%', 1)[0])
    except ValueError:
        return None
    if ip.version == 6 and ip.ipv4_mapped is not None:
        return ip.ipv4_mapped
    return ip


def parse_proxies(value) -> tuple:
    """(networks, unreadable entries) for a `flask.trusted_proxies` value: one address or
    CIDR range per entry."""
    networks, bad = [], []
    if isinstance(value, str):
        # A hand-written `trusted_proxies: 192.168.1.10` is one entry, not its characters.
        value = [value]
    for raw in (value or []):
        entry = str(raw or '').strip()
        if not entry:
            continue
        try:
            # Strict: 192.168.1.10/24 is refused, not quietly widened to the whole /24.
            network = ipaddress.ip_network(entry)
        except ValueError:
            bad.append(entry)
            continue
        if network.prefixlen == 0:
            # Every address there is: a list that believes anyone is not a list.
            bad.append(entry)
            continue
        networks.append(network)
    return tuple(networks), bad


def proxies_problem(value) -> str:
    """Why `value` cannot be `flask.trusted_proxies`, or ''."""
    if value is None:
        return ''
    if not isinstance(value, (list, tuple)):
        return 'Reverse proxy addresses is a list: one address or range per line'
    _networks, bad = parse_proxies(value)
    if bad:
        return (f'"{bad[0]}" is not an address or a range ChannelBin can use. Write one per '
                f'line, like 192.168.1.10 or 172.18.0.0/16. A range starts at its first '
                f'address (192.168.1.0/24, not 192.168.1.10/24) and cannot be every address')
    return ''


@dataclass(frozen=True)
class Policy:
    """Which ports believe a proxy, and from which peers. `networks` empty means any peer."""
    web: bool = False
    public: bool = False
    networks: tuple = ()

    def switched(self, port: str) -> bool:
        if port == PORT_WEB:
            return self.web
        if port == PORT_PUBLIC:
            return self.public
        raise ValueError(f'Unknown port "{port}"')

    def listed(self, peer: str) -> bool:
        """Whether `peer` may speak as a proxy: always with no list, else only when on it."""
        if not self.networks:
            return True
        ip = parse_address(peer)
        return ip is not None and any(ip.version == net.version and ip in net
                                      for net in self.networks)

    def believes(self, port: str, peer: str) -> bool:
        return self.switched(port) and self.listed(peer)


def policy_from(cfg: dict) -> Policy:
    """The policy config.yaml asks for. An entry of the list that cannot be read is logged and
    left out, which believes fewer peers, never more; a list with nothing readable left
    believes no peer at all rather than every one."""
    flask_cfg = cfg.get('flask') or {}
    feed_cfg = (cfg.get('integrations') or {}).get('output_feed') or {}
    networks, bad = parse_proxies(flask_cfg.get('trusted_proxies'))
    for entry in bad:
        log.warning('flask.trusted_proxies: "%s" is not an address or a range and is ignored', entry)
    if bad and not networks:
        log.warning('flask.trusted_proxies has no readable entry, so no reverse proxy is believed '
                    'until it is corrected')
        networks = (ipaddress.ip_network('255.255.255.255/32'),)
    return Policy(web=bool(flask_cfg.get('behind_proxy')),
                  public=bool(feed_cfg.get('public_behind_proxy')),
                  networks=networks)


def wired(app) -> Policy:
    """The policy this process runs (config.yaml's may differ until a restart)."""
    return app.config.get(CONFIG_KEY) or Policy()


def is_trusted(request) -> bool:
    """Whether this request's forwarded headers were believed, so its remote_addr is the
    visitor's own."""
    return bool(request.environ.get(TRUSTED_ENVIRON_KEY))


_lock = threading.Lock()


def _blank() -> dict:
    return {
        # The last request that arrived with no X-Forwarded-For from a peer that is not this
        # machine, on a port switched to trust a proxy with no list: someone reaches the port
        # without the proxy, and would be believed if they sent the header.
        'direct_peer': '', 'direct_at': None,
        # The last request whose forwarded headers were believed. With no list this is any
        # caller that sent them, so it is never called "your proxy" on its own word.
        'believed_peer': '', 'believed_at': None,
        # The last peer whose forwarded headers were ignored for not being on the list, how
        # many requests were, and the peers already logged (each once, up to a cap).
        'ignored_peer': '', 'ignored_at': None, 'ignored_count': 0, 'ignored_logged': [],
    }


_seen = {port: _blank() for port in PORTS}


def _note(port: str, peer: str, forwarded: bool, policy: Policy) -> Optional[str]:
    """Record what a port just heard. Returns a peer to log as newly ignored, or None. No I/O
    under the lock."""
    if not policy.switched(port):
        return None
    now = datetime.utcnow()
    listed = policy.listed(peer)
    with _lock:
        seen = _seen[port]
        if forwarded and listed:
            seen['believed_peer'], seen['believed_at'] = peer, now
            return None
        if forwarded:
            seen['ignored_count'] += 1
            seen['ignored_peer'], seen['ignored_at'] = peer, now
            if peer in seen['ignored_logged'] or len(seen['ignored_logged']) >= _MAX_IGNORED_PEERS:
                return None
            seen['ignored_logged'].append(peer)
            return peer
        ip = parse_address(peer)
        if not policy.networks and ip is not None and not ip.is_loopback:
            seen['direct_peer'], seen['direct_at'] = peer, now
    return None


#: How long an ignored peer goes on being reported. A proxy left off the list is ignored on
#: every request, so it never ages out; one stray request does.
IGNORED_REPORTED_FOR = timedelta(hours=1)


def seen(port: str) -> dict:
    """A copy of what `port` has heard since startup (see _blank). 'ignored_recent' is the
    last ignored peer when that was inside IGNORED_REPORTED_FOR, else ''."""
    now = datetime.utcnow()
    with _lock:
        out = dict(_seen[port])
        out['ignored_logged'] = list(out['ignored_logged'])
    recent = out['ignored_at'] is not None and now - out['ignored_at'] < IGNORED_REPORTED_FOR
    out['ignored_recent'] = out['ignored_peer'] if recent else ''
    return out


def reset_for_tests():
    with _lock:
        for port in PORTS:
            _seen[port] = _blank()


PORT_NAMES = {PORT_WEB: 'the web UI\'s port', PORT_PUBLIC: 'the IPTV output\'s public port'}


class ProxyTrust:
    """The WSGI wrapper around the Flask app: runs werkzeug's ProxyFix over a request only when
    the app's policy (wired()) believes its peer on its port, and marks every request with the
    answer."""

    def __init__(self, wsgi_app, flask_app):
        self.plain = wsgi_app
        self.fixed = ProxyFix(wsgi_app, **PROXY_FIX_ARGS)
        self.flask_app = flask_app

    def __call__(self, environ, start_response):
        policy = wired(self.flask_app)
        port = PORT_PUBLIC if environ.get(PUBLIC_ENVIRON_KEY) else PORT_WEB
        peer = environ.get('REMOTE_ADDR') or ''
        trusted = policy.believes(port, peer)
        environ[TRUSTED_ENVIRON_KEY] = trusted
        ignored = _note(port, peer, bool(environ.get('HTTP_X_FORWARDED_FOR')), policy)
        if ignored is not None:
            log.warning('A request on %s came from %s with an X-Forwarded-For header, and %s is '
                        'not in "Reverse proxy addresses" (flask.trusted_proxies), so the header '
                        'was ignored and the request counts as coming from %s itself. If that is '
                        'your reverse proxy, add it to the list and restart; until then every '
                        'visitor through it shares one address, and one person\'s wrong passwords '
                        'lock all of them out.', PORT_NAMES[port], ignored, ignored, ignored)
        return (self.fixed if trusted else self.plain)(environ, start_response)
