"""What each host answers: the provider's endpoints as a pure function of the request.

`Provider.handle()` maps one request to one `Response` and does no socket work, so the
server, the `dump` command and the tests all read the same answers. An answer depends on the
world, the host layout, the simulator's clock and the faults it is applying: a guide on the
hour a request arrives in (the same for every request within that hour), and an auth answer
on the time, which a real panel reports in it.

Every request is first classified - which endpoint, which login - so the fault layers can
decide what it gets (sim/faults.py): a login fault changes how the credentials are treated,
an answer fault replaces the answer, a model fault changes what the panel renders from, a
bytes fault rewrites the body, and a wire fault rides along for the server to apply.

A stream request - on a stream host, or on a panel host that lists its streams there - is
handed to the headend (sim/streams.py) with the stream faults it took, and its answer may be a
live stream for the server to write until it ends. The headend also names the request's
channel, its stage (a 302 to a play URL, or the request that serves) and its form, so a stream
fault aims at exactly the requests it means to.

Where the simulator has to decide something no real provider has been seen doing, the choice
is labeled "assumed" here, as the design's fault catalog labels it.
"""
import datetime as _dt
import ipaddress
import threading
from dataclasses import dataclass, field

from .faults import (EP_AUTH, EP_CATALOG, EP_CATEGORIES, EP_GUIDE, EP_LOGO, EP_OTHER,
                     EP_PLAYLIST, EP_STREAM, LAYER_ANSWER, LAYER_BYTES, LAYER_LOGIN,
                     LAYER_MODEL, LAYER_OPEN, LAYER_STREAM, LAYER_WIRE, Ask, FaultBook, View)
from .guide import GuideShape, programs
from .ledger import split_target
from .lineup import KIND_NO_ID, KIND_RADIO
from .rand import derive
from .streams import is_stream_path
from .world import LOGO_HOST
from .writers import gzip_bytes, m3u, php_json, png, xmltv

HOUR = _dt.timedelta(hours=1)
GENERATOR = 'XMLTV listings'
_EPOCH = _dt.datetime(1970, 1, 1)
_CACHE_ENTRIES = 32

ROLE_PANEL = 'panel'
ROLE_STREAM = 'stream'
ROLE_GUIDE_SITE = 'guide-site'
ROLE_LOGOS = 'logos'

_ACTIONS = {'': EP_AUTH, 'get_live_categories': EP_CATEGORIES, 'get_live_streams': EP_CATALOG}


def spread_addresses(first: str, count: int, loopback: bool = True) -> list[str]:
    """`count` consecutive addresses starting at `first`, all of them loopback unless
    `loopback` is False (a container that holds the addresses itself)."""
    base = ipaddress.IPv4Address(first)
    out = [base + i for i in range(count)]
    if loopback and not all(a.is_loopback for a in out):
        raise ValueError(f'{count} addresses from {first} leave 127.0.0.0/8; spreading hosts '
                         'over addresses is for loopback only')
    return [str(a) for a in out]


def _epoch(when: _dt.datetime) -> int:
    return int((when - _EPOCH).total_seconds())


def _m3u_parts(path: str):
    """(playlist or guide, username, password, extension) for a plain M3U panel's URL."""
    parts = [p for p in path.split('/') if p]
    if len(parts) != 3 or parts[0] not in ('playlist', 'guide'):
        return None
    password, _, ext = parts[2].partition('.')
    return parts[0], parts[1], password, ext


@dataclass
class Response:
    status: int
    body: bytes = b''
    content_type: str = 'text/plain; charset=utf-8'
    login: str | None = None
    note: str = ''
    headers: dict = field(default_factory=dict)
    wire: tuple = ()            # wire-layer ActiveFaults, for the server to apply
    endpoint: str = EP_OTHER
    faults: tuple = ()          # names of the faults that shaped this answer
    stream: object = None       # a live stream for the server to write until it ends
    channel: str | None = None  # the channel a stream request named, backend:id
    offered: tuple | None = None    # channel time sent, (first, last), seconds since the epoch
    profile: str | None = None  # the loop a stream was cut from (sim/content.py)
    withheld: tuple = ()        # (fault name, first, last) channel seconds sent without video
    delay: float = 0.0          # seconds to wait before the first byte
    hold: float | None = None   # accept and never answer, for up to this many seconds


class Layout:
    """Where each host of a world listens: {host name: (address, port)}. With `names`, the
    URLs the simulator hands out name each host by {host name: DNS name} instead of by its
    address - in a container network, where a DNS responder (sim/dns.py) answers for them."""

    def __init__(self, places: dict, names: dict | None = None):
        self.places = dict(places)
        self.names = dict(names or {})

    def where(self, host: str) -> tuple:
        """(what a URL names the host by, port)."""
        address, port = self.places[host]
        return self.names.get(host, address), port

    def origin(self, host: str) -> str:
        name, port = self.where(host)
        return f'http://{name}:{port}'

    @classmethod
    def virtual(cls, world, first_address: str = '127.0.0.2', port: int = 8080) -> 'Layout':
        """The layout `serve` would use with every host on its own address and one port."""
        names = world.host_names()
        return cls(dict(zip(names, ((a, port) for a in spread_addresses(first_address,
                                                                         len(names))))))


class Provider:
    """`headend` serves the streams (sim/streams.py); without one - the `dump` command - a
    stream request is answered 503."""

    def __init__(self, world, layout: Layout, clock, faults: FaultBook | None = None,
                 headend=None):
        self.world = world
        self.headend = headend
        self.layout = layout
        self.clock = clock
        self.faults = faults if faults is not None else FaultBook(world.faults)
        self.shape = GuideShape(world.calibration)
        self.status = {u: lg.status for u, lg in world.logins.items()}
        self._roles = {}
        for name in world.panels:
            self._roles[name] = ROLE_PANEL
        for b in world.backends.values():
            for h in b.stream_host_names():
                self._roles[h] = ROLE_STREAM
            self._roles[b.thirdparty_host] = ROLE_STREAM
        for name in world.guide_sites:
            self._roles[name] = ROLE_GUIDE_SITE
        self._roles[LOGO_HOST] = ROLE_LOGOS
        self._cache = {}
        self._cache_lock = threading.Lock()

    def role(self, host: str) -> str:
        return self._roles[host]

    def now(self) -> _dt.datetime:
        """The simulator's clock, naive UTC."""
        return self.clock()

    # --- the dispatcher -------------------------------------------------------------------

    def ask(self, host: str, path: str, query: dict, headers=None) -> Ask:
        """Which endpoint a request is for and which login its credentials name."""
        role = self._roles[host]
        endpoint, lg, stream = EP_OTHER, None, None
        if role == ROLE_PANEL and is_stream_path(path):
            # A panel that lists its streams on its own host serves them there too.
            endpoint, stream = EP_STREAM, self._describe_stream(host, path)
        elif role == ROLE_PANEL:
            panel = self.world.panels[host]
            if panel.kind == 'xtream':
                if path == '/player_api.php':
                    endpoint = _ACTIONS.get(query.get('action') or '', EP_OTHER)
                elif path == '/get.php':
                    endpoint = EP_PLAYLIST
                elif path == '/xmltv.php':
                    endpoint = EP_GUIDE
                lg = self.world.login_for(host, query.get('username'), query.get('password'))
            else:
                parts = _m3u_parts(path)
                if parts:
                    endpoint = EP_PLAYLIST if parts[0] == 'playlist' else EP_GUIDE
                    lg = self.world.login_for(host, parts[1], parts[2])
        elif role == ROLE_GUIDE_SITE:
            endpoint = EP_GUIDE if path in ('/guide.xml', '/guide.xml.gz') else EP_OTHER
        elif role == ROLE_LOGOS:
            endpoint = EP_LOGO
        else:
            endpoint, stream = EP_STREAM, self._describe_stream(host, path)
        if stream is not None:
            lg = stream['login']
        ua = (headers.get('User-Agent') if headers is not None else None) or ''
        ask = Ask(host, role, endpoint, lg.username if lg else None,
                  self.status.get(lg.username) if lg else None, ua, self.now())
        if stream is not None:
            ask.channel, ask.backend = stream['channel'], stream['backend']
            ask.stage, ask.form = stream['stage'], stream['form']
        return ask

    def _describe_stream(self, host, path):
        return self.headend.describe(host, path) if self.headend is not None else None

    def handle(self, host: str, method: str, target: str, headers=None) -> Response:
        path, query = split_target(target)
        if method not in ('GET', 'HEAD'):
            return Response(405, note='only GET and HEAD are served')
        ask = self.ask(host, path, query, headers)
        taken = self.faults.take(ask)
        applied = []
        for a in taken:
            if a.kind.layer == LAYER_LOGIN:
                a.kind.apply(ask, a)
                applied.append(a.kind.name)
        resp = None
        if ask.answer is not None:
            resp = Response(*ask.answer, login=ask.login, note='refused by a fault')
        else:
            for a in taken:
                if a.kind.layer == LAYER_ANSWER:
                    got = a.kind.apply(self, ask, a)
                    if got is not None:
                        resp = Response(*got, login=ask.login, note=f'answered by {a.kind.name}')
                        applied.append(a.kind.name)
                        break
        if resp is None and ask.endpoint == EP_STREAM:
            stream = [a for a in taken if a.kind.layer in (LAYER_OPEN, LAYER_STREAM)]
            resp = self._stream(ask, method, path, stream,
                                headers.get('Range') if headers is not None else None)
            applied += [a.kind.name for a in stream] + list(resp.faults)
        elif resp is None:
            model = [a for a in taken if a.kind.layer == LAYER_MODEL]
            resp = self._dispatch(ask, path, query, self._view(host, model))
            applied += [a.kind.name for a in model]
        for a in taken:
            if a.kind.layer == LAYER_BYTES:
                resp.body, resp.content_type, extra = a.kind.apply(
                    resp.body, resp.content_type, a, ask)
                resp.headers.update(extra)
                applied.append(a.kind.name)
        wire = [a for a in taken if a.kind.layer == LAYER_WIRE]
        resp.wire = tuple(wire)
        resp.endpoint = ask.endpoint
        resp.faults = tuple(applied + [a.kind.name for a in wire])
        return resp

    def _view(self, name: str, model) -> View | None:
        if not model:
            return None
        view = View(self.world, name)
        for a in model:
            a.kind.apply(view, a)
        view.key = tuple(a.id for a in model)
        return view

    def view(self, name: str, endpoint: str = EP_PLAYLIST, login: str | None = None) -> View:
        """What `name` renders from for a request to `endpoint` right now, with the model
        faults such a request would get applied, without counting one against a trigger."""
        ask = Ask(name, self._roles[name], endpoint, login,
                  self.status.get(login) if login else None, '', self.now())
        model = [a for a in self.faults.peek(ask) if a.kind.layer == LAYER_MODEL]
        return self._view(name, model) or View(self.world, name)

    def _dispatch(self, ask, path, query, view) -> Response:
        role = ask.role
        if role == ROLE_PANEL:
            panel = self.world.panels[ask.host]
            if panel.kind == 'xtream':
                return self._xtream(panel, ask, path, query, view)
            return self._m3u_panel(panel, ask, path, view)
        if role == ROLE_GUIDE_SITE:
            return self._site(self.world.guide_sites[ask.host], path, view)
        if role == ROLE_LOGOS:
            return self._logo(path)
        return Response(404)

    def _stream(self, ask, method, path, taken=(), byte_range=None) -> Response:
        if self.headend is None:
            return Response(503, note='no stream engine here (the dump command serves none)')
        lg = None if ask.refused or ask.login is None else self.world.logins[ask.login]
        a = self.headend.answer(ask.host, method, path, lg, ask.status, taken, byte_range)
        return Response(a.status, a.body, a.content_type, a.login, a.note, dict(a.headers),
                        faults=tuple(a.faults), stream=a.stream, channel=a.channel,
                        offered=a.offered, profile=a.profile, withheld=a.withheld,
                        delay=a.delay, hold=a.hold)

    # --- Xtream panels --------------------------------------------------------------------

    def _xtream(self, panel, ask, path, q, view) -> Response:
        lg = None if ask.refused or ask.login is None else self.world.logins[ask.login]
        status = ask.status
        who = ask.login
        key = view.key if view is not None else ()
        if path == '/player_api.php':
            action = q.get('action')
            if lg is None:
                # assumed: a real panel's answer to unknown credentials was never recorded.
                return self._json({'user_info': {'auth': 0}}, who,
                                  'refused by a fault' if ask.refused else 'unknown credentials')
            if not action:
                return self._json(self.auth_payload(panel, lg, status), who)
            if status != 'Active':
                # assumed: a login that is not Active is refused every data action.
                return self._json({'user_info': {'auth': 0, 'status': status}}, who,
                                  f'login {status}')
            if action == 'get_live_categories':
                return self._cached_json(('categories', panel.name, key), who,
                                         lambda: self.live_categories(panel, view))
            if action == 'get_live_streams':
                return self._cached_json(('streams', panel.name, lg.username, key), who,
                                         lambda: self.live_streams(panel, lg, view))
            # Real panels answer an action they do not know with an empty list.
            return self._json([], who, f'unknown action {action}')
        if path == '/get.php':
            if lg is None or status != 'Active':
                return Response(401, login=who, note='refused (assumed)')
            body = self._cached(('playlist', panel.name, lg.username, key),
                                lambda: self.playlist(panel, lg, view))
            return Response(200, body, 'application/octet-stream', who)
        if path == '/xmltv.php':
            if lg is None or status != 'Active':
                return Response(401, login=who, note='refused (assumed)')
            return Response(200, self.panel_guide(panel, view), 'application/xml', who)
        return Response(404, login=who)

    def auth_payload(self, panel, lg, status) -> dict:
        if status in ('Banned', 'Disabled'):
            # assumed: no install has seen either; reported as a refusal naming the status.
            return {'user_info': {'auth': 0, 'status': status}}
        today = self.now().replace(hour=0, minute=0, second=0, microsecond=0)
        if lg.exp_date:
            exp = lg.exp_date
        elif status == 'Expired':
            # assumed: an expired subscription reports the day it ran out, not one ahead.
            exp = today - _dt.timedelta(days=3)
        else:
            exp = today + _dt.timedelta(days=365)
        now = self.now()
        user_info = {
            'username': lg.username, 'password': lg.password, 'message': '', 'auth': 1,
            'status': status, 'exp_date': str(_epoch(exp)), 'is_trial': '1' if lg.is_trial else '0',
            'active_cons': str(self.headend.connections(lg.username) if self.headend else 0),
            'created_at': str(_epoch(today - _dt.timedelta(days=200))),
            'max_connections': str(lg.max_connections),
            'allowed_output_formats': list(lg.allowed_output_formats),
        }
        payload = {'user_info': user_info}
        if panel.server_info != 'none':
            if panel.server_info == 'api':
                address, port = self.layout.where(panel.name)
            else:
                hosts = self.world.backends[panel.backend].stream_host_names()
                address, port = self.layout.where(hosts[0])
            payload['server_info'] = {
                'url': address, 'port': str(port), 'https_port': '443',
                'server_protocol': 'http', 'rtmp_port': '1935', 'timezone': 'UTC',
                'timestamp_now': _epoch(now), 'time_now': f'{now:%Y-%m-%d %H:%M:%S}',
                'process': True,
            }
        return payload

    def _rows(self, panel, view):
        return view.rows if view is not None else self.world.panel_lineups[panel.name].rows

    def live_categories(self, panel, view=None) -> list:
        cats = (view.categories if view is not None
                else self.world.panel_lineups[panel.name].categories)
        return [{'category_id': cid, 'category_name': name, 'parent_id': 0}
                for cid, name in cats]

    def live_streams(self, panel, lg, view=None) -> list:
        out = []
        for row in self._rows(panel, view):
            ch = row.channel
            direct = (self.stream_url(panel, row, lg, view) if ch.kind in (KIND_RADIO, KIND_NO_ID)
                      else '')
            out.append({
                'num': row.num, 'name': row.name,
                'stream_type': 'radio_streams' if ch.kind == KIND_RADIO else 'live',
                'stream_id': ch.stream_id, 'stream_icon': self.logo_url(ch.logo),
                'epg_channel_id': row.tvg_id or None,
                'added': str(1_600_000_000 + derive(self.world.seed, 'added', ch.stream_id)
                             % 100_000_000),
                'is_adult': 0, 'category_id': row.category_id,
                'category_ids': [int(row.category_id)], 'custom_sid': '', 'tv_archive': 0,
                'direct_source': direct, 'tv_archive_duration': 0,
            })
        return out

    def stream_url(self, panel, row, lg, view=None) -> str:
        """The URL a panel lists for one channel and one login, in that row's spelling."""
        literal = getattr(row, 'literal_url', None)
        if literal is not None:
            return literal
        ch = row.channel
        backend = self.world.backends[panel.backend]
        if ch.kind == KIND_RADIO:
            return f'{self.layout.origin(backend.thirdparty_host)}/radio/{ch.slug}.mp3'
        if ch.kind == KIND_NO_ID:
            return f'{self.layout.origin(backend.thirdparty_host)}/hls/{ch.slug}/index.m3u8'
        hosts = backend.stream_host_names()
        on_api, index = panel.streams_on_api_host, ch.host
        if view is not None and view.drift_host:
            # Every URL moves: off the API host onto the backend's own, from one stream host
            # to the next, or - with only one - onto the API host.
            if on_api:
                on_api = False
            elif len(hosts) > 1:
                index = (ch.host + 1) % len(hosts)
            else:
                on_api = True
        base = self.layout.origin(panel.name if on_api else hosts[index])
        user, password = (view.credentials if view is not None and view.credentials
                          else (lg.username, lg.password))
        sid = ch.stream_id + (view.url_id_offset if view is not None else 0)
        cred = f'{user}/{password}/{sid}'
        if row.shape == 'bare':
            return f'{base}/{cred}'
        if row.shape == 'live_ts':
            return f'{base}/live/{cred}.ts'
        if row.shape == 'live_m3u8':
            return f'{base}/live/{cred}.m3u8'
        if row.shape == 'bare_with_ext':
            return f'{base}/{cred}.ts'
        raise ValueError(f'unknown url shape {row.shape!r}')

    def logo_url(self, key) -> str:
        return f'{self.layout.origin(LOGO_HOST)}/logos/{key}.png' if key else ''

    def playlist(self, panel, lg, view=None) -> bytes:
        cuid = view.cuid if view is not None else panel.cuid
        entries = []
        for row in self._rows(panel, view):
            attrs = []
            if cuid:
                attrs.append(('CUID', row.channel.stream_id))
            attrs += [('tvg-id', row.tvg_id), ('tvg-name', row.name),
                      ('tvg-logo', self.logo_url(row.channel.logo)),
                      ('group-title', row.category_name)]
            entries.append((attrs, row.name, self.stream_url(panel, row, lg, view)))
        stream_host = self.world.backends[panel.backend].stream_host_names()[0]
        origin = self.layout.origin(panel.name if panel.streams_on_api_host else stream_host)
        vod = view.vod if view is not None else self.world.panel_lineups[panel.name].vod
        for v in vod:
            url = f'{origin}/{v.kind}/{lg.username}/{lg.password}/{v.vod_id}.{v.ext}'
            entries.append(([('tvg-id', ''), ('tvg-name', v.name), ('tvg-logo', ''),
                             ('group-title', v.category)], v.name, url))
        return m3u(entries, panel.extinf_duration)

    # --- plain M3U panels -----------------------------------------------------------------

    def _m3u_panel(self, panel, ask, path, view) -> Response:
        parts = _m3u_parts(path)
        if parts is None:
            return Response(404)
        kind, _username, _password, ext = parts
        lg = None if ask.refused or ask.login is None else self.world.logins[ask.login]
        who = ask.login
        if lg is None or ask.status != 'Active':
            return Response(401, login=who, note='refused (assumed)')
        key = view.key if view is not None else ()
        if kind == 'playlist' and ext == 'm3u':
            body = self._cached(('playlist', panel.name, lg.username, key),
                                lambda: self.playlist(panel, lg, view))
            return Response(200, body, 'audio/x-mpegurl', who)
        if kind == 'guide' and ext == 'xml':
            return Response(200, self.panel_guide(panel, view), 'application/xml', who)
        if kind == 'guide' and ext == 'xml.gz':
            return Response(200, gzip_bytes(self.panel_guide(panel, view)), 'application/gzip',
                            who)
        return Response(404, login=who)

    # --- guides ---------------------------------------------------------------------------

    def window(self, days_behind: float, days_ahead: float):
        hour = self.now().replace(minute=0, second=0, microsecond=0)
        return (hour - _dt.timedelta(days=days_behind), hour + _dt.timedelta(days=days_ahead))

    def _guide(self, owner, directory, offset, days_behind, days_ahead, key=()) -> bytes:
        start, end = self.window(days_behind, days_ahead)

        def render():
            seed = self.world.seed
            channels = [(g.guide_id, g.display_name, self.logo_url(g.icon)) for g in directory]
            progs = []
            for g in directory:
                if not g.has_programs:
                    continue
                for p in programs(seed, g.station, start, end, self.shape):
                    icon = f'{self.layout.origin(LOGO_HOST)}/art/{p.icon}.png' if p.icon else None
                    progs.append((g.guide_id, p, icon))
            return xmltv(channels, progs, offset, GENERATOR)

        return self._cached(('guide', owner, start, key), render)

    def panel_guide(self, panel, view=None) -> bytes:
        guide = view.guide if view is not None else self.world.panel_lineups[panel.name].guide
        return self._guide(panel.name, guide, panel.guide_offset, panel.guide_days_behind,
                           panel.guide_days_ahead, view.key if view is not None else ())

    def site_guide(self, site, view=None) -> bytes:
        guide = view.guide if view is not None else self.world.site_lineups[site.name].guide
        return self._guide(site.name, guide, site.offset, site.days_behind, site.days_ahead,
                           view.key if view is not None else ())

    def _site(self, site, path, view=None) -> Response:
        if path == '/guide.xml':
            return Response(200, self.site_guide(site, view), 'application/xml')
        if path == '/guide.xml.gz':
            return Response(200, gzip_bytes(self.site_guide(site, view)), 'application/gzip')
        return Response(404)

    def _logo(self, path) -> Response:
        parts = [p for p in path.split('/') if p]
        if len(parts) == 2 and parts[0] in ('logos', 'art'):
            key, _, ext = parts[1].partition('.')
            if ext in ('', 'png') and key and all(c in '0123456789abcdef' for c in key):
                return Response(200, png(key), 'image/png')
        return Response(404)

    # --- plumbing -------------------------------------------------------------------------

    def _cached(self, key, render) -> bytes:
        with self._cache_lock:
            hit = self._cache.get(key)
        if hit is not None:
            return hit
        body = render()
        with self._cache_lock:
            if len(self._cache) >= _CACHE_ENTRIES:
                self._cache.pop(next(iter(self._cache)))
            self._cache[key] = body
        return body

    def _cached_json(self, key, who, build) -> Response:
        return Response(200, self._cached(key, lambda: php_json(build())), 'application/json', who)

    def _json(self, obj, who, note='') -> Response:
        return Response(200, php_json(obj), 'application/json', who, note)
