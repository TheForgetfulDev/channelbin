"""Faults: the ways real providers have broken a sync or a stream, reproduced on command.

A fault is one kind from `CATALOG`, aimed at a target - a login, a panel or guide site, a
host, a channel or one connection to it - on some of that target's endpoints, with settings
of its own and a trigger saying when it applies. Every kind carries the evidence for the
behavior it reproduces and how firm that evidence is, in the four words the design's fault
catalog uses: measured, observed, reproduced or assumed. A test passing against an assumed
fault says less than one passing against a measured fault, and the catalog keeps the two
apart.

A kind works at one of seven layers. The first five are met in this order by every request:

- **login**: how the credentials are treated - refused, a status other than Active, rate
  limited
- **answer**: the whole answer replaced - a status code, an empty or HTML body, a bad logo
- **model**: what a panel renders from - rows dropped, added, renamed, repeated, re-spelled
- **bytes**: the rendered body rewritten - a comment above the header, a BOM, gzip, a cut
- **wire**: how the body goes onto the socket - a chunked body cut short, a slow drip

The other two belong to streams, and the headend applies them (sim/stream_faults.py):

- **open**: what a stream request is answered with - refused, held, the placeholder clip,
  another loop
- **stream**: what happens to a stream once it flows - silence, a close, video withheld,
  another pace, timestamps moved, an HLS playlist that misbehaves

A trigger is a window on the simulator's clock plus a count of the requests the fault aims
at: the first `after` of them pass untouched and the next `times` (every one, when None) get
the fault. Counting happens under one lock as each request is answered, so two requests at
once never both take a fault's last application. A stream fault aims only at the request that
serves a stream, never at the 302 that sends a client to it, so a count is a count of
connections. A channel's stream-layer fault is not counted at all: every connection to the
channel reads it as it plays and applies it by channel time, so a stream stays a pure function
of channel time and one already playing picks up a fault that starts later.
"""
import datetime as _dt
import threading
from collections import Counter
from dataclasses import dataclass

from .lineup import (KIND_LIVE, KIND_SEPARATOR, TRIPLET_KINDS, BackendChannel, GuideChannel,
                     PanelRow, channel_label, frac)
from .content import TS_PROFILES
from .names import invented_stem
from .rand import derive, rng
from .world import LOGIN_STATUSES, LOGO_HOST
from .writers import gzip_bytes

SCOPE_LOGIN = 'login'
SCOPE_PANEL = 'panel'
SCOPE_HOST = 'host'
SCOPE_CHANNEL = 'channel'
SCOPE_CONNECTION = 'connection'
STREAM_SCOPES = (SCOPE_CHANNEL, SCOPE_CONNECTION)

EP_AUTH = 'auth'
EP_CATEGORIES = 'categories'
EP_CATALOG = 'catalog'
EP_PLAYLIST = 'playlist'
EP_GUIDE = 'guide'
EP_LOGO = 'logo'
EP_STREAM = 'stream'
EP_OTHER = 'other'
SYNC_ENDPOINTS = (EP_AUTH, EP_CATEGORIES, EP_CATALOG, EP_PLAYLIST, EP_GUIDE, EP_LOGO)
_XTREAM_ENDPOINTS = (EP_AUTH, EP_CATEGORIES, EP_CATALOG, EP_PLAYLIST, EP_GUIDE)
_M3U_ENDPOINTS = (EP_PLAYLIST, EP_GUIDE)

LAYER_LOGIN = 'login'
LAYER_ANSWER = 'answer'
LAYER_MODEL = 'model'
LAYER_BYTES = 'bytes'
LAYER_WIRE = 'wire'
LAYER_OPEN = 'open'
LAYER_STREAM = 'stream'
LAYERS = (LAYER_LOGIN, LAYER_ANSWER, LAYER_MODEL, LAYER_BYTES, LAYER_WIRE, LAYER_OPEN,
          LAYER_STREAM)
# Layers whose behavior lives outside this module: the server applies wire kinds as it
# writes, the headend open and stream kinds (sim/stream_faults.py).
_APPLIED_ELSEWHERE = (LAYER_WIRE, LAYER_OPEN, LAYER_STREAM)

# What a stream request is, as a stream fault sees it.
FORM_TS = 'ts'                      # an MPEG-TS connection
FORM_HLS_PLAYLIST = 'hls_playlist'  # a media playlist
FORM_HLS_SEGMENT = 'hls_segment'
FORM_HLS_MASTER = 'hls_master'
FORM_RADIO = 'radio'
_HLS_FORMS = (FORM_HLS_PLAYLIST, FORM_HLS_SEGMENT)
STAGE_SERVE = 'serve'
STAGE_REDIRECT = 'redirect'

MEASURED = 'measured'
OBSERVED = 'observed'
REPRODUCED = 'reproduced'
ASSUMED = 'assumed'

# The wire kinds, which the server applies while it writes rather than the provider.
KIND_SLOW_DRIP = 'slow_drip'
KIND_TRUNCATED_CHUNKED = 'truncated_chunked'

INACTIVE_STATUSES = tuple(s for s in LOGIN_STATUSES if s != 'Active')
_BOM = b'\xef\xbb\xbf'
_HTML = 'text/html; charset=UTF-8'
_FIXED_KEYS = ('kind', 'target', 'endpoints', 'start', 'end', 'after', 'times')


class FaultError(ValueError):
    """A fault that cannot be built, with the reason in words."""


# --- settings ---------------------------------------------------------------------------------

def _share(value, where):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not 0 <= value <= 1:
        raise FaultError(f'{where} must be a number from 0 to 1, not {value!r}')
    return float(value)


def _count(low):
    def check(value, where):
        if isinstance(value, bool) or not isinstance(value, int) or value < low:
            raise FaultError(f'{where} must be a whole number of at least {low}, not {value!r}')
        return value
    return check


def _seconds(value, where):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value < 0:
        raise FaultError(f'{where} must be a number of seconds, not {value!r}')
    return float(value)


def _status(value, where):
    if isinstance(value, bool) or not isinstance(value, int) or not 100 <= value <= 999:
        raise FaultError(f'{where} must be an HTTP status from 100 to 999, not {value!r}')
    return value


def _choice(*options):
    def check(value, where):
        if value not in options:
            raise FaultError(f'{where} must be one of {", ".join(options)}, not {value!r}')
        return value
    return check


def _positive(value, where):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 0:
        raise FaultError(f'{where} must be a number above 0, not {value!r}')
    return float(value)


def _signed_seconds(value, where):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not value:
        raise FaultError(f'{where} must be a number of seconds other than 0, not {value!r}')
    return float(value)


def _optional(check):
    def inner(value, where):
        return None if value is None else check(value, where)
    return inner


def _ts_profile(value, where):
    if value not in TS_PROFILES:
        raise FaultError(f'{where} must be an MPEG-TS content profile, not {value!r}; profiles '
                         f'are {", ".join(TS_PROFILES)}')
    return value


def _text(value, where):
    if not isinstance(value, str) or not value:
        raise FaultError(f'{where} must be some text, not {value!r}')
    return value


_REQUIRED = object()


# --- the record -------------------------------------------------------------------------------

@dataclass(frozen=True)
class Kind:
    """One way a provider breaks. `params` maps each setting to (default, check); a default
    of _REQUIRED means the setting has to be given."""
    name: str
    scope: str
    layer: str
    endpoints: tuple
    evidence: str
    basis: str
    summary: str
    params: dict
    panel_kinds: tuple = ('xtream', 'm3u')
    apply: object = None
    condition: object = None
    forms: tuple = ()           # the stream requests a stream kind acts on

    def defaults(self) -> dict:
        return {k: d for k, (d, _check) in self.params.items() if d is not _REQUIRED}

    @property
    def live(self) -> bool:
        """Read by every connection to its channel as it plays, never counted at a request."""
        return self.scope == SCOPE_CHANNEL and self.layer == LAYER_STREAM


@dataclass(frozen=True)
class Trigger:
    start: _dt.datetime | None = None
    end: _dt.datetime | None = None
    after: int = 0
    times: int | None = None


@dataclass(frozen=True)
class Fault:
    kind: Kind
    target: str
    endpoints: tuple
    params: dict
    trigger: Trigger

    def describe(self) -> str:
        bits = [f'{self.kind.name} on {self.kind.scope} {self.target}']
        if self.endpoints != self.kind.endpoints:
            bits.append(f'({", ".join(self.endpoints)})')
        if self.params:
            bits.append(' '.join(f'{k}={v}' for k, v in self.params.items()))
        return ' '.join(bits)


class ActiveFault:
    """A fault the simulator is applying, with its counts: `seen` is how many requests it
    aimed at inside its window, `hits` how many it took."""

    def __init__(self, fid: int, fault: Fault):
        self.id = fid
        self.fault = fault
        self.seen = 0
        self.hits = 0
        self.state = {}
        self.lock = threading.Lock()

    kind = property(lambda self: self.fault.kind)
    target = property(lambda self: self.fault.target)
    params = property(lambda self: self.fault.params)
    endpoints = property(lambda self: self.fault.endpoints)
    trigger = property(lambda self: self.fault.trigger)

    def note_hit(self) -> None:
        """A connection applied this live fault for the first time."""
        with self.lock:
            self.hits += 1

    @property
    def spent(self) -> bool:
        t = self.fault.trigger
        return t.times is not None and self.seen >= t.after + t.times

    def __repr__(self):
        return f'<fault {self.id}: {self.fault.describe()}, {self.hits} hit(s)>'


@dataclass
class Ask:
    """One request, as the fault layers see it."""
    host: str
    role: str
    endpoint: str
    login: str | None
    status: str | None
    user_agent: str
    at: _dt.datetime
    refused: bool = False          # answer as though the credentials named nobody
    answer: tuple | None = None    # (status, body, content type), set by a login fault
    channel: str | None = None     # a stream request's channel, backend:stream id
    backend: str | None = None     # and its backend
    stage: str | None = None       # serve, or redirect for a 302 to a play URL
    form: str | None = None        # ts, hls_playlist, hls_segment, hls_master or radio


def _aims_at(fault: Fault, ask: Ask) -> bool:
    if ask.endpoint not in fault.endpoints:
        return False
    scope = fault.kind.scope
    if scope in STREAM_SCOPES:
        if ask.stage != STAGE_SERVE or (fault.kind.forms and ask.form not in fault.kind.forms):
            return False
        return fault.target in (ask.channel, ask.backend) or (
            scope == SCOPE_CONNECTION and fault.target == ask.login)
    if scope == SCOPE_LOGIN:
        return ask.login == fault.target
    return ask.host == fault.target


class FaultBook:
    """The faults one simulator is applying, in the order they were added."""

    def __init__(self, faults=()):
        self._lock = threading.Lock()
        self._active: list[ActiveFault] = []
        self._next = 1
        for f in faults:
            self.add(f)

    def add(self, fault: Fault) -> ActiveFault:
        with self._lock:
            active = ActiveFault(self._next, fault)
            self._next += 1
            self._active.append(active)
        return active

    def remove(self, active: ActiveFault) -> None:
        with self._lock:
            self._active = [a for a in self._active if a is not active]

    def clear(self) -> None:
        with self._lock:
            self._active = []

    def active(self) -> list[ActiveFault]:
        with self._lock:
            return list(self._active)

    def take(self, ask: Ask) -> list[ActiveFault]:
        """The faults this request gets, counted."""
        return self._match(ask, count=True)

    def peek(self, ask: Ask) -> list[ActiveFault]:
        """The faults a request like this would get now, without counting it."""
        return self._match(ask, count=False)

    def live(self, channel: str, backend: str) -> list[ActiveFault]:
        """The channel faults a connection to `channel` reads as it plays - aimed at the
        channel or at its whole backend - whatever their window; the window is the
        connection's to apply, by channel time."""
        with self._lock:
            return [a for a in self._active
                    if a.kind.live and a.fault.target in (channel, backend)]

    def _match(self, ask, count):
        out = []
        with self._lock:
            for a in self._active:
                f = a.fault
                t = f.trigger
                if f.kind.live or not _aims_at(f, ask):
                    continue
                if (t.start is not None and ask.at < t.start) or (t.end is not None
                                                                  and ask.at >= t.end):
                    continue
                if f.kind.condition is not None and not f.kind.condition(f, ask):
                    continue
                n = a.seen + 1
                if count:
                    a.seen = n
                if n <= t.after or (t.times is not None and n > t.after + t.times):
                    continue
                if count:
                    a.hits += 1
                out.append(a)
        return out


def served_endpoints(world, scope: str, target: str) -> tuple:
    """The endpoints a target answers, so a fault that could never apply is refused."""
    if scope in STREAM_SCOPES:
        return (EP_STREAM,)
    if scope == SCOPE_LOGIN:
        out = []
        for p in world.logins[target].panels:
            out += served_endpoints(world, SCOPE_PANEL, p)
        return tuple(dict.fromkeys(out))
    if target in world.panels:
        return _XTREAM_ENDPOINTS if world.panels[target].kind == 'xtream' else _M3U_ENDPOINTS
    if target in world.guide_sites:
        return (EP_GUIDE,)
    if target == LOGO_HOST:
        return (EP_LOGO,)
    return (EP_STREAM,)


def is_channel_label(world, target) -> bool:
    """Whether `target` names a channel of the world the way the ledger does
    (sim/lineup.py channel_label)."""
    if not isinstance(target, str) or ':' not in target:
        return False
    backend, rest = target.split(':', 1)
    lu = world.lineups.get(backend)
    if lu is None:
        return False
    if rest.isdigit():
        return any(c.stream_id == int(rest) and c.kind in TRIPLET_KINDS for c in lu.channels)
    return any(channel_label(backend, c) == target for c in lu.channels
               if c.kind not in TRIPLET_KINDS)


def _time(value, where):
    if value is None or isinstance(value, _dt.datetime):
        return value
    if isinstance(value, _dt.date):
        return _dt.datetime(value.year, value.month, value.day)
    if isinstance(value, str):
        try:
            t = _dt.datetime.fromisoformat(value)
        except ValueError:
            raise FaultError(f'{where} must be a time like 2026-10-17T12:00, '
                             f'not {value!r}') from None
        if t.tzinfo is not None:
            t = t.astimezone(_dt.timezone.utc).replace(tzinfo=None)
        return t
    raise FaultError(f'{where} must be a time, not {value!r}')


def make_fault(world, kind: str, target: str, *, endpoints=None, start=None, end=None,
               after: int = 0, times: int | None = None, where: str = 'fault',
               **params) -> Fault:
    """A fault, checked against the world it will run in. Anything that could never apply -
    an unknown kind, setting or target, an endpoint the target does not serve - is refused
    with the choices, never quietly ignored."""
    k = CATALOG.get(kind)
    if k is None:
        raise FaultError(f'{where}: unknown fault kind {kind!r}; kinds are {", ".join(CATALOG)}')
    where = f'{where} ({kind})'
    if k.scope in STREAM_SCOPES:
        if not is_channel_label(world, target) and target not in world.backends and not (
                k.scope == SCOPE_CONNECTION and target in world.logins):
            whom = ('a channel (backend:stream id), a backend or a login'
                    if k.scope == SCOPE_CONNECTION else 'a channel (backend:stream id) or a '
                                                        'backend')
            raise FaultError(f'{where}: target must be {whom}, not {target!r}; backends are '
                             f'{", ".join(world.backends)}')
        if k.live and (after or times is not None):
            raise FaultError(f'{where}: a channel stream fault applies by channel time, so it '
                             'takes start and end, not after or times (which count connections, '
                             'as a connection fault does)')
    elif k.scope == SCOPE_LOGIN:
        if target not in world.logins:
            raise FaultError(f'{where}: target must be a login\'s username, not {target!r}; '
                             f'logins are {", ".join(world.logins)}')
    elif k.scope == SCOPE_PANEL:
        names = list(world.panels) + list(world.guide_sites)
        if target not in names:
            raise FaultError(f'{where}: target must be a panel or guide site, not {target!r}; '
                             f'they are {", ".join(names)}')
        if target in world.panels and world.panels[target].kind not in k.panel_kinds:
            raise FaultError(f'{where}: only applies to {" or ".join(k.panel_kinds)} panels, '
                             f'and {target} is {world.panels[target].kind}')
    else:
        hosts = world.host_names()
        if target not in hosts:
            raise FaultError(f'{where}: target must be a host, not {target!r}; '
                             f'hosts are {", ".join(hosts)}')
    if endpoints is None:
        endpoints = k.endpoints
    if isinstance(endpoints, str):
        endpoints = (endpoints,)
    endpoints = tuple(endpoints)
    bad = [e for e in endpoints if e not in k.endpoints]
    if not endpoints or bad:
        raise FaultError(f'{where}: endpoints must be some of {", ".join(k.endpoints)}, '
                         f'not {", ".join(map(str, bad or endpoints)) or "none"}')
    served = served_endpoints(world, k.scope, target)
    endpoints = tuple(e for e in endpoints if e in served)
    if not endpoints:
        raise FaultError(f'{where}: {target} answers none of the endpoints this fault hits '
                         f'(it answers {", ".join(served)})')
    unknown = sorted(set(params) - set(k.params))
    if unknown:
        allowed = ', '.join(k.params) or 'none'
        raise FaultError(f'{where}: unknown setting(s) {", ".join(unknown)}; settings: {allowed}')
    checked = {}
    for name, (default, check) in k.params.items():
        if name in params:
            checked[name] = check(params[name], f'{where} {name}')
        elif default is _REQUIRED:
            raise FaultError(f'{where}: setting {name!r} is required')
        else:
            checked[name] = default
    after = _count(0)(after, f'{where} after')
    if times is not None:
        times = _count(1)(times, f'{where} times')
    trigger = Trigger(_time(start, f'{where} start'), _time(end, f'{where} end'), after, times)
    if trigger.start and trigger.end and trigger.end <= trigger.start:
        raise FaultError(f'{where}: end must be after start')
    if kind in NEEDS_START and trigger.start is None:
        raise FaultError(f'{where}: start is required - it is the moment the jump falls at')
    return Fault(k, target, endpoints, checked, trigger)


def faults_from_world(world, items) -> tuple:
    """The `faults:` list of a world file, each entry a mapping of `kind`, `target`, the
    optional `endpoints`/`start`/`end`/`after`/`times`, and the kind's own settings."""
    out = []
    for i, d in enumerate(items or []):
        where = f'faults[{i}]'
        if not isinstance(d, dict):
            raise FaultError(f'{where} must be a mapping, not {type(d).__name__}')
        d = dict(d)
        kind, target = d.pop('kind', None), d.pop('target', None)
        fixed = {k: d.pop(k) for k in _FIXED_KEYS if k in d}
        out.append(make_fault(world, kind, target, where=where, **fixed, **d))
    return tuple(out)


# --- what a panel renders from ----------------------------------------------------------------

class FaultRow(PanelRow):
    """A row a fault added whose URL line is not built from a host and credentials."""
    __slots__ = ('literal_url',)

    def __init__(self, *args, literal_url):
        super().__init__(*args)
        self.literal_url = literal_url


class View:
    """What one panel or guide site renders from for one request, with the model faults that
    request took applied. Built only when a model fault applies."""

    def __init__(self, world, name: str):
        self.world = world
        self.name = name
        self.panel = world.panels.get(name)
        if self.panel is not None:
            lu = world.panel_lineups[name]
            self.rows, self.categories, self.guide, self.vod = (
                lu.rows, lu.categories, lu.guide, lu.vod)
            self.cuid = self.panel.cuid
        else:
            self.rows, self.categories, self.vod = [], [], []
            self.guide = world.site_lineups[name].guide
            self.cuid = False
        self.url_id_offset = 0
        self.credentials = None
        self.drift_host = False
        # The ids of the faults applied, which is what tells one render from another.
        self.key = ()


def _row(row, **changes) -> PanelRow:
    fields = {k: getattr(row, k) for k in PanelRow.__slots__}
    fields.update(changes)
    return PanelRow(*(fields[k] for k in PanelRow.__slots__))


def _guide_row(g, **changes) -> GuideChannel:
    fields = {k: getattr(g, k) for k in GuideChannel.__slots__}
    fields.update(changes)
    return GuideChannel(*(fields[k] for k in GuideChannel.__slots__))


def _flip_case(text: str) -> str:
    return text.upper() if text != text.upper() else text.lower()


def _fresh_ids(view, n: int, tag: str) -> list[int]:
    """`n` stream ids no channel of the panel's backend carries."""
    top = max((c.stream_id for c in view.world.lineups[view.panel.backend].channels), default=0)
    first = top + 1 + derive(view.world.seed, 'fault-ids', view.name, tag) % 1000
    return list(range(first, first + n))


def _new_category(view, label: str) -> tuple[str, str]:
    ids = [int(cid) for cid, _ in view.categories if cid.isdigit()]
    names = {n for _, n in view.categories}
    name, n = label, 1
    while name in names:
        n += 1
        name = f'{label} {n}'
    return str(max(ids, default=0) + 1), name


def _model_placeholder_rows(view, f):
    seed, n = view.world.seed, f.params['count']
    r = rng(seed, 'fault-placeholder', view.name)
    cid, cname = _new_category(view, f'US| {invented_stem(r).upper()} PASS')
    dummy = f'dummy-{100000 + derive(seed, "fault-dummy-id", view.name) % 900000}'
    rows = list(view.rows)
    for sid in _fresh_ids(view, n, 'placeholder'):
        name = f'### {invented_stem(r).upper()} ###'
        ch = BackendChannel(-1, sid, KIND_SEPARATOR, name, '', None, None, None, None, 0, None)
        rows.append(FaultRow(ch, len(rows) + 1, name, cid, cname, dummy, 'placeholder',
                             literal_url='http'))
    view.rows = rows
    view.categories = list(view.categories) + [(cid, cname)]


def _model_cuid_url_mismatch(view, f):
    view.cuid = True
    view.url_id_offset = f.params['offset']


def _model_lineup_change(view, f):
    p, seed, name = f.params, view.world.seed, view.name
    rows = []
    for row in view.rows:
        sid = row.channel.stream_id
        if p['drop'] and frac(seed, 'fault-drop', name, sid) < p['drop']:
            continue
        if (p['rename'] and row.channel.station is not None
                and frac(seed, 'fault-rename', name, sid) < p['rename']):
            r = rng(seed, 'fault-rename-name', name, sid)
            row = _row(row, name=f'{invented_stem(r).upper()} {row.channel.station.genre.upper()}')
        rows.append(row)
    if p['add']:
        r = rng(seed, 'fault-add', name)
        cats = view.categories or [('1', 'NEW')]
        hosts = view.world.backends[view.panel.backend].stream_hosts
        for i, sid in enumerate(_fresh_ids(view, p['add'], 'add')):
            cid, cname = cats[i % len(cats)]
            ch_name = f'{invented_stem(r).upper()} TV'
            ch = BackendChannel(-1, sid, KIND_LIVE, ch_name, '', None, None, None, None,
                                i % hosts, None)
            rows.append(PanelRow(ch, len(rows) + 1, ch_name, cid, cname, '', 'bare'))
    view.rows = rows


def _model_duplicate_ids(view, f):
    picks = [row for row in view.rows if row.channel.kind in TRIPLET_KINDS
             and not isinstance(row, FaultRow)]
    if not picks or not view.categories:
        return
    n = f.params['count']
    chosen = picks[::max(1, len(picks) // n)][:n]
    order = [cid for cid, _ in view.categories]
    names = dict(view.categories)
    extra = []
    for row in chosen:
        cid = order[(order.index(row.category_id) + 1) % len(order)]
        # The same id under another spelling of its URL - /id beside /live/.../id.ts - is the
        # collision shape the importer has to count rather than drop silently.
        shape = 'live_ts' if row.shape == 'bare' else 'bare'
        extra.append(_row(row, category_id=cid, category_name=names[cid], shape=shape))
    view.rows = list(view.rows) + extra


def drifted_credentials(seed, panel: str) -> tuple[str, str]:
    """The (username, password) a `url_drift` fault with `to: credentials` puts in every one of
    a panel's stream URLs. The stream hosts accept them for as long as the fault exists."""
    alphabet = 'abcdefghjkmnpqrstuvwxyz23456789'

    def token(label):
        n, out = derive(seed, 'fault-drift', panel, label), []
        for _ in range(10):
            n, k = divmod(n, len(alphabet))
            out.append(alphabet[k])
        return ''.join(out)

    return token('user'), token('pass')


def _model_url_drift(view, f):
    if f.params['to'] == 'host':
        view.drift_host = True
        return
    view.credentials = drifted_credentials(view.world.seed, view.name)


def _model_category_change(view, f):
    n = min(f.params['rename'], len(view.categories))
    r = rng(view.world.seed, 'fault-category', view.name)
    names = {cname for _, cname in view.categories}
    renamed = {}
    for cid, cname in view.categories[:n]:
        new = f'{cname} {invented_stem(r).upper()}'
        while new in names:
            new += ' 2'
        names.add(new)
        renamed[cid] = new
    view.categories = [(cid, renamed.get(cid, cname)) for cid, cname in view.categories]
    view.rows = [_row(row, category_name=renamed[row.category_id])
                 if row.category_id in renamed else row for row in view.rows]


def _model_guide_id_case(view, f):
    share, seed = f.params['share'], view.world.seed
    per_id = Counter(row.tvg_id for row in view.rows if row.tvg_id)
    seen = Counter()
    rows = []
    for row in view.rows:
        tvg = row.tvg_id
        if tvg:
            seen[tvg] += 1
            # Of the channels sharing an id, every second one spells it the other way, which
            # is the shape that shadowed one of them; a channel alone on its id is flipped by
            # chance, so a key spelled unlike the file's is covered too.
            if per_id[tvg] > 1:
                flip = seen[tvg] % 2 == 0
            else:
                flip = frac(seed, 'fault-case', view.name, tvg) < share
            if flip:
                row = _row(row, tvg_id=_flip_case(tvg))
        rows.append(row)
    view.rows = rows


def _model_guide_id_twins(view, f):
    share, seed = f.params['share'], view.world.seed
    guide = list(view.guide)
    twins = []
    for i, g in enumerate(guide):
        twin = _flip_case(g.guide_id)
        if twin == g.guide_id or not g.has_programs:
            continue
        if frac(seed, 'fault-twin', view.name, g.guide_id) < share:
            other = guide[(i + 1) % len(guide)].station
            twins.append(GuideChannel(twin, g.display_name, other, True, g.icon))
    view.guide = guide + twins


def _model_shared_guide_id(view, f):
    target = next((g.guide_id for g in view.guide if g.has_programs), None)
    if target is None:
        return
    left = f.params['count']
    rows = []
    for row in view.rows:
        if left and row.channel.kind == KIND_LIVE and row.tvg_id != target:
            row = _row(row, tvg_id=target)
            left -= 1
        rows.append(row)
    view.rows = rows


def _model_no_programs(view, f):
    view.guide = [_guide_row(g, has_programs=False) for g in view.guide]


def _model_guide_shrink(view, f):
    keep, seed = f.params['keep'], view.world.seed
    view.guide = [g if frac(seed, 'fault-shrink', view.name, g.guide_id) < keep
                  else _guide_row(g, has_programs=False) for g in view.guide]


# --- answers ----------------------------------------------------------------------------------

def nginx_page(status: int, reason: str) -> bytes:
    """The error page an nginx front end writes by default."""
    return (f'<html>\r\n<head><title>{status} {reason}</title></head>\r\n<body>\r\n'
            f'<center><h1>{status} {reason}</h1></center>\r\n<hr><center>nginx</center>\r\n'
            '</body>\r\n</html>\r\n').encode('ascii')


_MAINTENANCE = (b'<!DOCTYPE html>\n<html lang="en">\n<head><meta charset="utf-8">'
                b'<title>Maintenance</title></head>\n<body>\n<h1>We will be back soon</h1>\n'
                b'<p>The service is down for maintenance. Please try again later.</p>\n'
                b'</body>\n</html>\n')

_SVG = (b'<?xml version="1.0" encoding="UTF-8"?>\n'
        b'<svg xmlns="http://www.w3.org/2000/svg" width="64" height="64">'
        b'<rect width="64" height="64" fill="#336699"/>'
        b'<script>document.title = "logo script ran"</script></svg>\n')

_REASONS = {403: 'Forbidden', 429: 'Too Many Requests', 500: 'Internal Server Error',
            502: 'Bad Gateway', 503: 'Service Temporarily Unavailable', 504: 'Gateway Time-out'}


def _answer_empty(provider, ask, f):
    return (200, b'', _HTML)


def _answer_html_page(provider, ask, f):
    return (f.params['status'], _MAINTENANCE, _HTML)


def _answer_server_error(provider, ask, f):
    s = f.params['status']
    return (s, nginx_page(s, _REASONS.get(s, 'Error')), _HTML)


def _answer_bare_status(provider, ask, f):
    return (f.params['status'], b'', _HTML)


def _answer_auth_as_playlist(provider, ask, f):
    if ask.login is None or ask.refused:
        return None
    panel = provider.world.panels[ask.host]
    return (200, provider.playlist(panel, provider.world.logins[ask.login]),
            'application/octet-stream')


def _answer_user_agent(provider, ask, f):
    s = f.params['status']
    return (s, nginx_page(s, _REASONS.get(s, 'Forbidden')), _HTML)


def _answer_logo_svg(provider, ask, f):
    return (200, _SVG, 'image/svg+xml')


def _answer_logo_oversized(provider, ask, f):
    return (200, b'\x89PNG\r\n\x1a\n' + bytes(f.params['bytes'] - 8), 'image/png')


def _answer_logo_wrong_type(provider, ask, f):
    return (200, _MAINTENANCE, f.params['content_type'])


# --- logins -----------------------------------------------------------------------------------

def _login_refused(ask, f):
    ask.refused = True


def _login_status(ask, f):
    ask.status = f.params['status']


def _login_rate_limit(ask, f):
    # assumed: no provider has been seen rate limiting a sync, so the policy is the simplest
    # one that names both halves - refuse a login over its allowance, ban it after repeats.
    p, st = f.params, f.state
    with f.lock:
        if st.get('banned'):
            ask.status = 'Banned'
            return
        start = st.get('window_start')
        if start is None or (ask.at - start).total_seconds() >= p['window_seconds']:
            st['window_start'], st['syncs'] = ask.at, 0
        if ask.endpoint == EP_AUTH:
            st['syncs'] += 1
            if st['syncs'] > p['syncs']:
                st['refused'] = st.get('refused', 0) + 1
                if p['ban_after'] and st['refused'] >= p['ban_after']:
                    st['banned'] = True
        if st['syncs'] > p['syncs']:
            s = p['status']
            ask.answer = (s, nginx_page(s, _REASONS.get(s, 'Too Many Requests')), _HTML)


# --- bytes ------------------------------------------------------------------------------------

def _bytes_preamble(body, ctype, f, ask):
    lines = [f'# {f.params["text"]}'] + ['# -'] * (f.params['lines'] - 1)
    head = ('\n'.join(lines) + '\n').encode('utf-8')
    if body.startswith(_BOM):
        return _BOM + head + body[len(_BOM):], ctype, {}
    return head + body, ctype, {}


def _bytes_bom(body, ctype, f, ask):
    return (body if body.startswith(_BOM) else _BOM + body), ctype, {}


def _bytes_gzip_file(body, ctype, f, ask):
    return gzip_bytes(body), 'application/gzip', {}


def _bytes_gzip_encoding(body, ctype, f, ask):
    return gzip_bytes(body), ctype, {'Content-Encoding': 'gzip'}


def _bytes_gzip_corrupt(body, ctype, f, ask):
    gz = gzip_bytes(body)
    cut = gz[:max(20, len(gz) // 2)]
    if f.params['delivery'] == 'file':
        return cut, 'application/gzip', {}
    return cut, ctype, {'Content-Encoding': 'gzip'}


def _bytes_truncate(body, ctype, f, ask):
    return body[:int(len(body) * f.params['fraction'])], ctype, {}


_LAUGHS = ('<!DOCTYPE tv [\n  <!ENTITY lol0 "lol">\n'
           + ''.join(f'  <!ENTITY lol{i} "{f"&lol{i - 1};" * 10}">\n' for i in range(1, 10))
           + ']>').encode('ascii')


def _bytes_entities(body, ctype, f, ask):
    body = body.replace(b'<!DOCTYPE tv SYSTEM "xmltv.dtd">', _LAUGHS, 1)
    start = body.find(b'<title lang="en">')
    if start < 0:
        start = body.find(b'<display-name>')
    if start >= 0:
        open_end = body.index(b'>', start) + 1
        close = body.index(b'</', open_end)
        body = body[:open_end] + b'&lol9;' + body[close:]
    return body, ctype, {}


# --- the catalog ------------------------------------------------------------------------------

_ROWS = (EP_CATALOG, EP_PLAYLIST)


def _kind(name, scope, layer, endpoints, basis, evidence, summary, params=None, **kw):
    return Kind(name, scope, layer, tuple(endpoints), evidence, basis, summary, params or {},
                **kw)


_KINDS = (
    _kind('auth_refused', SCOPE_LOGIN, LAYER_LOGIN, _XTREAM_ENDPOINTS, ASSUMED,
          'app/xtream_client.py::check_auth; the answer\'s shape from dev/changelog/534; no '
          'refusal recorded on either install',
          'the panel answers this login as unknown credentials: auth 0, data refused',
          apply=_login_refused),
    _kind('account_status', SCOPE_LOGIN, LAYER_LOGIN, _XTREAM_ENDPOINTS,
          ASSUMED,
          'dev/changelog/534 measured the fields; every Xtream account on both installs reads '
          'Active, so the other values are assumed',
          'the login reads Expired, Banned or Disabled while the fault holds',
          {'status': (_REQUIRED, _choice(*INACTIVE_STATUSES))}, apply=_login_status),
    _kind('auth_as_playlist', SCOPE_PANEL, LAYER_ANSWER, (EP_AUTH, EP_CATEGORIES, EP_CATALOG),
          ASSUMED,
          'check_auth\'s M3U branch, there since the first Xtream client; neither install\'s '
          'logs record it',
          'player_api.php answers a valid login with the M3U playlist instead of JSON',
          panel_kinds=('xtream',), apply=_answer_auth_as_playlist),
    _kind('playlist_down', SCOPE_PANEL, LAYER_ANSWER, (EP_PLAYLIST,), MEASURED,
          'dev/changelog/1100 (get.php answering HTTP 884 with an empty body on every sync '
          'since August), 261, DESIGN-live-vod.md §4.3; observed in prod',
          'the playlist endpoint answers a made-up status with an empty body, so a client '
          'falls back to the JSON catalog and builds its own URLs',
          {'status': (884, _status)}, apply=_answer_bare_status),
    _kind('playlist_preamble', SCOPE_PANEL, LAYER_BYTES, (EP_PLAYLIST,), MEASURED,
          'dev/changelog/523, BUGS.md 2026-08-09 12:11 PM (a public free source)',
          'comment lines above #EXTM3U',
          {'lines': (1, _count(1)),
           'text': ('Free TV - playlist in M3U format with TV and radio. Public streams only.',
                    _text)},
          apply=_bytes_preamble),
    _kind('playlist_bom', SCOPE_PANEL, LAYER_BYTES, (EP_PLAYLIST,), ASSUMED,
          'BUGS.md 2026-08-09 12:11 PM names it as a case, not a sighting',
          'a UTF-8 byte order mark before #EXTM3U', apply=_bytes_bom),
    _kind('placeholder_rows', SCOPE_PANEL, LAYER_MODEL, (EP_CATEGORIES,) + _ROWS, MEASURED,
          'BUGS.md 2026-07-14 06:16 PM, dev/changelog/112; 250 skipped on one account\'s every '
          'sync; observed in prod',
          'template rows named ### NAME ###, sharing one dummy guide id, whose URL line is the '
          'bare word http', {'count': (8, _count(1))}, apply=_model_placeholder_rows),
    _kind('cuid_url_mismatch', SCOPE_PANEL, LAYER_MODEL, (EP_PLAYLIST,), MEASURED,
          'tests/fixtures/mixed_cuid, from a real dump (dev/changelog/713); DESIGN-live-vod.md '
          '§2: 13,106 of 13,106 rows carry CUID and no URL tail equals its stream id',
          'every playlist row carries CUID with the catalog\'s stream id while its URL ends in '
          'a different number', {'offset': (1_000_000, _count(1))},
          panel_kinds=('xtream',), apply=_model_cuid_url_mismatch),
    _kind('lineup_change', SCOPE_PANEL, LAYER_MODEL, _ROWS, MEASURED,
          'DESIGN-sync-resilience.md §3, §5; per sync the 99th percentile adds 2.5% and '
          'removes 1.0% of a lineup; event slots retitled between syncs',
          'channels gone, new and renamed since the last sync',
          {'drop': (0.0, _share), 'add': (0, _count(0)), 'rename': (0.0, _share)},
          apply=_model_lineup_change),
    _kind('duplicate_ids', SCOPE_PANEL, LAYER_MODEL, _ROWS, MEASURED,
          'BUGS.md 2026-08-30 10:23 AM, dev/changelog/871, DESIGN-account-providers.md §2; 625 '
          'and 214 repeats on two accounts',
          'stream ids listed twice in one playlist, the copy in another category and URL '
          'spelling', {'count': (6, _count(1))}, apply=_model_duplicate_ids),
    _kind('url_drift', SCOPE_PANEL, LAYER_MODEL, _ROWS, ASSUMED,
          'DESIGN-url-drift.md; neither install\'s retained events hold a provider-side change',
          'every stream URL moves to another host, or carries new credentials',
          {'to': ('host', _choice('host', 'credentials'))}, apply=_model_url_drift),
    _kind('category_change', SCOPE_PANEL, LAYER_MODEL, (EP_CATEGORIES,) + _ROWS, MEASURED,
          'dev/changelog/1211; the largest prod account gained 15 categories and lost 12 in a '
          'month',
          'categories renamed, so the old names are gone and new ones appear',
          {'rename': (2, _count(1))}, apply=_model_category_change),
    _kind('empty_body', SCOPE_HOST, LAYER_ANSWER, SYNC_ENDPOINTS, ASSUMED,
          'XtreamClient._fetch',
          'HTTP 200 with an empty body', apply=_answer_empty),
    _kind('html_page', SCOPE_HOST, LAYER_ANSWER, SYNC_ENDPOINTS, ASSUMED,
          'XtreamClient._fetch, _looks_like_m3u',
          'an HTML maintenance page in place of the answer', {'status': (200, _status)},
          apply=_answer_html_page),
    _kind('server_error', SCOPE_HOST, LAYER_ANSWER, SYNC_ENDPOINTS, ASSUMED,
          'XtreamClient._fetch',
          'a 5xx with nginx\'s error page', {'status': (503, _status)},
          apply=_answer_server_error),
    _kind('nonstandard_status', SCOPE_HOST, LAYER_ANSWER, SYNC_ENDPOINTS, MEASURED,
          'dev/changelog/1100 (HTTP 884, empty body)',
          'a status no standard defines, with an empty body', {'status': (884, _status)},
          apply=_answer_bare_status),
    _kind(KIND_SLOW_DRIP, SCOPE_HOST, LAYER_WIRE, SYNC_ENDPOINTS, ASSUMED,
          'XtreamClient._fetch, requests\' read timeout',
          'the answer written a piece at a time with a pause before each',
          {'chunk_bytes': (4096, _count(1)), 'gap_seconds': (2.0, _seconds)}),
    _kind(KIND_TRUNCATED_CHUNKED, SCOPE_HOST, LAYER_WIRE, SYNC_ENDPOINTS, OBSERVED,
          'sync_log 122\'s InvalidChunkLength (DESIGN-sync-resilience.md §1, BUGS.md '
          '2026-07-21 10:20 AM); observed on the dev instance',
          'a chunked answer whose connection closes where the next chunk length should be',
          {'fraction': (0.5, _share)}),
    _kind('gzip_file', SCOPE_PANEL, LAYER_BYTES, (EP_GUIDE,), MEASURED,
          'dev/changelog/523, BUGS.md 2026-08-09 12:08 PM (a real .xml.gz source)',
          'the guide sent as a gzip file - application/gzip, no Content-Encoding',
          apply=_bytes_gzip_file),
    _kind('gzip_encoding', SCOPE_PANEL, LAYER_BYTES, _XTREAM_ENDPOINTS, ASSUMED,
          'HTTP transfer compression; no provider has been recorded sending it',
          'the answer compressed on the wire with Content-Encoding: gzip',
          apply=_bytes_gzip_encoding),
    _kind('gzip_corrupt', SCOPE_PANEL, LAYER_BYTES, (EP_GUIDE,), ASSUMED,
          'BUGS.md 2026-08-09 12:08 PM names it as a case, not a sighting',
          'gzip that stops halfway, sent as a file or as Content-Encoding',
          {'delivery': ('file', _choice('file', 'encoding'))}, apply=_bytes_gzip_corrupt),
    _kind('xmltv_truncated', SCOPE_PANEL, LAYER_BYTES, (EP_GUIDE,), REPRODUCED,
          'dev/changelog/719, BUGS.md 2026-08-17 08:02 PM; built from a synthetic cut, the real '
          'cause is the truncated chunked body',
          'the guide cut off partway through', {'fraction': (0.5, _share)},
          apply=_bytes_truncate),
    _kind('xmltv_no_programs', SCOPE_PANEL, LAYER_MODEL, (EP_GUIDE,), MEASURED,
          'dev/changelog/1100 (8,350 channels, 0 programs), BUGS.md 2026-09-23 06:35 AM; '
          'observed in prod',
          'a guide that lists its channels and no programs at all',
          apply=_model_no_programs),
    _kind('guide_shrink', SCOPE_PANEL, LAYER_MODEL, (EP_GUIDE,), OBSERVED,
          'dev/changelog/244, BUGS.md 2026-09-23 06:35 AM; 2 collapse refusals in prod\'s 215 '
          'syncs; observed in prod',
          'a guide carrying programs for only a fraction of its channels',
          {'keep': (0.1, _share)}, apply=_model_guide_shrink),
    _kind('guide_id_case', SCOPE_PANEL, LAYER_MODEL, _ROWS, MEASURED,
          'BUGS.md 2026-07-11 03:59 PM; 21% of one account\'s distinct guide ids have a case '
          'twin',
          'playlist guide ids spelled in another case than the guide\'s',
          {'share': (0.5, _share)}, apply=_model_guide_id_case),
    _kind('guide_id_twins', SCOPE_PANEL, LAYER_MODEL, (EP_GUIDE,), MEASURED,
          'dev/changelog/1139 (2,297 channels), BUGS.md 2026-09-26 06:15 PM',
          'the guide lists a channel under a second id differing only in case, carrying '
          'another feed\'s schedule', {'share': (0.3, _share)}, apply=_model_guide_id_twins),
    _kind('shared_guide_id', SCOPE_PANEL, LAYER_MODEL, _ROWS, MEASURED,
          'BUGS.md 2026-08-16 06:38 PM; 16% to 69% of an account\'s channels with an id share '
          'it',
          'many channels in the playlist on one guide id', {'count': (12, _count(1))},
          apply=_model_shared_guide_id),
    _kind('xml_entities', SCOPE_PANEL, LAYER_BYTES, (EP_GUIDE,), ASSUMED,
          'dev/changelog/482 (hardening)',
          'a guide whose DTD declares a chain of entities that would expand a billion-fold',
          apply=_bytes_entities),
    _kind('logo_svg', SCOPE_HOST, LAYER_ANSWER, (EP_LOGO,), ASSUMED,
          'BUGS.md 2026-08-14 09:42 AM (hardening), app/logo_cache.py',
          'a logo served as SVG carrying a script', apply=_answer_logo_svg),
    _kind('logo_oversized', SCOPE_HOST, LAYER_ANSWER, (EP_LOGO,), ASSUMED,
          'app/logo_cache.py\'s size cap (hardening)',
          'a logo far larger than any logo', {'bytes': (6 * 1024 * 1024, _count(9))},
          apply=_answer_logo_oversized),
    _kind('logo_wrong_type', SCOPE_HOST, LAYER_ANSWER, (EP_LOGO,), ASSUMED,
          'app/logo_cache.py\'s type allowlist (hardening)',
          'a logo URL answering a web page', {'content_type': (_HTML, _text)},
          apply=_answer_logo_wrong_type),
    _kind('user_agent_refused', SCOPE_HOST, LAYER_ANSWER, SYNC_ENDPOINTS, ASSUMED,
          'dev/changelog/523 measured what ChannelBin sends, not a provider refusing it',
          'requests whose User-Agent contains `match` are refused',
          {'match': (_REQUIRED, _text), 'status': (403, _status)},
          apply=_answer_user_agent,
          condition=lambda f, ask: f.params['match'] in (ask.user_agent or '')),
    _kind('rate_limit', SCOPE_LOGIN, LAYER_LOGIN, _XTREAM_ENDPOINTS, ASSUMED,
          'the one-sync-per-account budget real accounts are held to; never measured',
          'a login that syncs more than `syncs` times in a window is refused, and banned after '
          '`ban_after` refused syncs',
          {'syncs': (1, _count(1)), 'window_seconds': (3600.0, _seconds),
           'status': (429, _status), 'ban_after': (2, _count(0))},
          apply=_login_rate_limit),

    # --- streams (sim/stream_faults.py applies these) -----------------------------------------
    _kind('silent_socket', SCOPE_CONNECTION, LAYER_STREAM, (EP_STREAM,), MEASURED,
          'dev/changelog/958, BUGS.md 2026-09-14 07:05 AM: the frame counter froze with no log '
          'line until -rw_timeout made ffmpeg reconnect inside the segment',
          'after `after_bytes`, the connection sends nothing more and stays open until the '
          'client leaves or `hold_seconds` pass',
          {'after_bytes': (1_048_576, _count(0)), 'hold_seconds': (3600.0, _seconds)},
          forms=(FORM_TS,)),
    _kind('close_mid_stream', SCOPE_CONNECTION, LAYER_STREAM, (EP_STREAM,), OBSERVED,
          'capture stderr in DIAGNOSTICS events (dev/changelog/430): "Stream ends prematurely" '
          'then "Will reconnect ... error=Input/output error" in 23% of prod\'s segment tails '
          'and 1.7% of dev\'s; observed in prod',
          'after `after_bytes`, the server closes the connection as if the stream had ended',
          {'after_bytes': (1_048_576, _count(0))}, forms=(FORM_TS,)),
    _kind('slow_first_byte', SCOPE_CONNECTION, LAYER_OPEN, (EP_STREAM,), MEASURED,
          'dev/changelog/958: time to first byte 0.25 to 1.26 s on four accounts',
          'the answer waits `seconds` before its first byte',
          {'seconds': (1.26, _seconds)},
          forms=(FORM_TS, FORM_HLS_PLAYLIST, FORM_HLS_SEGMENT, FORM_HLS_MASTER, FORM_RADIO)),
    _kind('no_answer', SCOPE_CONNECTION, LAYER_OPEN, (EP_STREAM,), OBSERVED,
          '3.7% of prod\'s health checks end "Connection timed out - server did not respond" '
          '(DESIGN-simulator.md §6.1); observed in prod. Dev\'s 3 "No route to host" need a '
          'network to reproduce and are not built',
          'the connection is accepted and never answered, until the client leaves or '
          '`seconds` pass',
          {'seconds': (3600.0, _seconds)},
          forms=(FORM_TS, FORM_HLS_PLAYLIST, FORM_HLS_SEGMENT, FORM_HLS_MASTER, FORM_RADIO)),
    _kind('refuse_at_open', SCOPE_CHANNEL, LAYER_OPEN, (EP_STREAM,), MEASURED,
          'dev/changelog/436 (HTTP 405 Method Not Allowed at input open, every attempt, the '
          'zero-byte death spiral), 465, 105 (a dead stream that reconnects and dies); 593 and '
          'dev recording 18 (back about 90 s later, a window); 8.3% of prod\'s health checks '
          'and 19.4% of dev\'s',
          'every stream request for the channel is refused with `status` and an nginx page',
          {'status': (405, _status)},
          forms=(FORM_TS, FORM_HLS_PLAYLIST, FORM_HLS_SEGMENT, FORM_HLS_MASTER, FORM_RADIO)),
    _kind('placeholder', SCOPE_CHANNEL, LAYER_OPEN, (EP_STREAM,), MEASURED,
          'dev/changelog/957: a finite 600 s black clip, the same 14,472,616 bytes on every '
          'request, drained in about 5 s with exit 0; 86x to 150x real time, 117x the median '
          '(dev/changelog/964); 9.9% of prod\'s health checks',
          'the channel answers with a finite clip of `clip_seconds`, the same bytes every '
          'time, as a file - its length given, a range honored, nothing past its end - sent at '
          'up to `speed` times real time',
          {'profile': ('placeholder_1080p30', _ts_profile), 'clip_seconds': (600.0, _positive),
           'speed': (117.0, _positive)}, forms=(FORM_TS,)),
    _kind('format_change', SCOPE_CHANNEL, LAYER_OPEN, (EP_STREAM,), OBSERVED,
          'dev/changelog/754, 866 (a feed that came back at another frame rate after a stall '
          'restart), 1150; prod recording 17; a third of multi-segment recordings change '
          '(DESIGN-simulator.md §6.1); observed in prod',
          'a connection opened while the fault holds gets `profile` - another resolution or '
          'frame rate - on the same channel clock; one already open keeps its own',
          {'profile': ('h264_1080p5994_aac', _ts_profile)}, forms=(FORM_TS, FORM_HLS_PLAYLIST)),
    _kind('audio_layout_change', SCOPE_CHANNEL, LAYER_OPEN, (EP_STREAM,), REPRODUCED,
          'dev/changelog/1163, 1184, BUGS.md 2026-08-24 12:31 PM; seen only across group '
          'members (dev recording 18 took 5.1 AC-3 from one), never yet within one feed',
          'a connection opened while the fault holds gets `profile`, stereo to 5.1 or back',
          {'profile': ('h264_1080p5994_ac3_51', _ts_profile)},
          forms=(FORM_TS, FORM_HLS_PLAYLIST)),
    _kind('picture', SCOPE_CHANNEL, LAYER_OPEN, (EP_STREAM,), OBSERVED,
          'black and solid pictures in 1.2% of prod\'s health checks and 3.2% of dev\'s '
          '(BUGS.md 2026-06-27 06:15 PM, app/channel_tester.py _check_screenshot_uniform); '
          'observed in prod. A still frozen frame is assumed',
          'a connection opened while the fault holds gets a black, frozen or uniform picture '
          '(`profile`)', {'profile': ('black_1080p5994', _ts_profile)},
          forms=(FORM_TS, FORM_HLS_PLAYLIST)),
    _kind('low_bitrate', SCOPE_CHANNEL, LAYER_OPEN, (EP_STREAM,), OBSERVED,
          'the health check\'s low-bitrate finding: 0.5% of prod\'s checks, 0.3% of dev\'s; '
          'observed in prod',
          'a connection opened while the fault holds gets `profile`, far below the usual rate '
          'for its resolution', {'profile': ('h264_720p2997_lowrate', _ts_profile)},
          forms=(FORM_TS, FORM_HLS_PLAYLIST)),
    _kind('live_edge_chunks', SCOPE_CHANNEL, LAYER_STREAM, (EP_STREAM,), MEASURED,
          'dev/changelog/997 (about 25 s on connect, then 5-second chunks), 998; uneven lumps '
          'after the burst again on a real provider (DESIGN-simulator.md §3.10)',
          'once a connection has caught up after its burst, it is sent nothing until '
          '`chunk_seconds` of channel are due, then all of it at once; with `spread_seconds` '
          'each lump comes up to that much early or late (assumed: 997\'s reconnects at a 5 s '
          'read timeout say real gaps run past 5 s at times, and how far is not measured)',
          {'chunk_seconds': (5.0, _positive), 'spread_seconds': (0.0, _seconds)},
          forms=(FORM_TS,)),
    _kind('fast_reserve', SCOPE_CHANNEL, LAYER_STREAM, (EP_STREAM,), OBSERVED,
          'dev/changelog/964, BUGS.md 2026-09-14 03:08 PM: recording 19 took 8h38m of content '
          'in 2h18m of wall clock, 3.76x sustained; 6 fast-delivery stops in prod',
          'while the fault holds, a connection is sent channel `speed` times faster than real '
          'time, running ahead of the live edge; after it, nothing until the clock catches up '
          '(assumed)', {'speed': (3.76, _positive)}, forms=(FORM_TS,)),
    _kind('throttle', SCOPE_CHANNEL, LAYER_STREAM, (EP_STREAM,), OBSERVED,
          'dev/changelog/429 (recording 71: delivery while connected fell from 1,030 kB/s to '
          '610-740, 237 and 24; 75 stalls in 91 minutes); observed on the dev instance',
          'while the fault holds, a connection is sent channel at `rate` times real time, '
          'sliding to `rate_end` across the window when that is given; after it, the backlog '
          'at once (assumed)',
          {'rate': (0.6, _share), 'rate_end': (None, _optional(_share))}, forms=(FORM_TS,)),
    _kind('video_stops', SCOPE_CHANNEL, LAYER_STREAM, (EP_STREAM,), OBSERVED,
          'dev/changelog/1209 (recording 76, observed on the dev instance; reproduced on '
          'loopback), 1210 (connecting while the video is out), 1199 (a video-only gap over '
          '10 s)',
          'the channel\'s video packets are withheld for the window while audio, PAT and PMT '
          'flow on', forms=(FORM_TS, FORM_HLS_SEGMENT)),
    _kind('timestamp_jump', SCOPE_CHANNEL, LAYER_STREAM, (EP_STREAM,), ASSUMED,
          'none; no backward timestamp in any of the 32 timeline scans the two installs hold',
          'from the window\'s start every timestamp is moved by `seconds`',
          {'seconds': (30.0, _signed_seconds)}, forms=(FORM_TS, FORM_HLS_SEGMENT)),
    _kind('pts_wrap', SCOPE_CHANNEL, LAYER_STREAM, (EP_STREAM,), ASSUMED,
          'none; the 33-bit PTS wraps every 26.5 hours, and no recording has been seen across one',
          'the channel\'s timestamps are shifted so the 33-bit wrap falls at `wrap_at`',
          {'wrap_at': (_REQUIRED, lambda v, w: _time(v, w))},
          forms=(FORM_TS, FORM_HLS_SEGMENT)),
    _kind('hls_stale', SCOPE_CHANNEL, LAYER_STREAM, (EP_STREAM,), ASSUMED,
          'none; HLS is under 0.1% of both installs\' lineups',
          'the media playlist stops moving on: it answers the window as of the fault\'s start',
          forms=_HLS_FORMS),
    _kind('hls_missing_segment', SCOPE_CHANNEL, LAYER_STREAM, (EP_STREAM,), ASSUMED,
          'none; HLS is under 0.1% of both installs\' lineups',
          'segments starting in the window are listed but answer 404', forms=_HLS_FORMS),
    _kind('hls_discontinuity', SCOPE_CHANNEL, LAYER_STREAM, (EP_STREAM,), ASSUMED,
          'none; HLS is under 0.1% of both installs\' lineups',
          'from the window\'s start the segments\' timestamps jump by `seconds`, and the '
          'playlist marks the first of them with EXT-X-DISCONTINUITY',
          {'seconds': (30.0, _signed_seconds)}, forms=_HLS_FORMS),
)

# Stream kinds whose effect happens at a moment, which the trigger's start names.
NEEDS_START = ('timestamp_jump', 'hls_discontinuity')

CATALOG = {k.name: k for k in _KINDS}
SYNC_KINDS = tuple(k.name for k in _KINDS if k.scope not in STREAM_SCOPES)
STREAM_KINDS = tuple(k.name for k in _KINDS if k.scope in STREAM_SCOPES)


def check_catalog() -> list[str]:
    """Problems with the catalog itself - every kind needs evidence, a basis from the four
    words, an apply function unless it works on the wire, and defaults its own checks pass."""
    problems = []
    for k in _KINDS:
        if k.basis not in (MEASURED, OBSERVED, REPRODUCED, ASSUMED):
            problems.append(f'{k.name}: basis {k.basis!r}')
        if not k.evidence or not k.summary:
            problems.append(f'{k.name}: evidence and summary are required')
        if k.layer not in LAYERS:
            problems.append(f'{k.name}: layer {k.layer!r}')
        if (k.apply is None) != (k.layer in _APPLIED_ELSEWHERE):
            problems.append(f'{k.name}: only a wire, open or stream kind may have no apply '
                            'function')
        if (k.scope in STREAM_SCOPES) != (k.layer in (LAYER_OPEN, LAYER_STREAM)):
            problems.append(f'{k.name}: a channel or connection kind works at the open or '
                            'stream layer, and only such a kind does')
        if k.scope in STREAM_SCOPES and (k.endpoints != (EP_STREAM,) or not k.forms):
            problems.append(f'{k.name}: a stream kind hits the stream endpoint and names the '
                            'stream requests it acts on')
        for name, (default, check) in k.params.items():
            if default is not _REQUIRED:
                try:
                    check(default, name)
                except FaultError as exc:
                    problems.append(f'{k.name}: default {exc}')
    return problems


__all__ = ['ASSUMED', 'CATALOG', 'MEASURED', 'OBSERVED', 'REPRODUCED', 'STREAM_KINDS',
           'SYNC_KINDS', 'ActiveFault', 'Ask', 'Fault', 'FaultBook', 'FaultError', 'Kind',
           'Trigger', 'View', 'faults_from_world', 'is_channel_label', 'make_fault']
