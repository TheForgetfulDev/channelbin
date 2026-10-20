"""A world: the backends, panels, logins and guide sites one simulator serves.

A world is a YAML file plus a seed. Seed, world file and `SIM_VERSION` together fix every byte
the simulator serves, so a failing run can be replayed exactly. Every key a world file may set
is listed in this module; an unknown key is an error rather than something quietly ignored, so
a misspelled setting can never leave a world running on a default its author did not choose.

The shape, from the top down:

- A **backend** holds channels and streams. It owns the stream ids, the stream hosts, the
  seats, and how its streams play: which loop (`content`), how much a new connection gets at
  once (`burst_seconds`), whether a stream request is redirected to a play URL on a stream
  host (`stream_redirect`), the HLS segment length, window and how long an idle HLS client
  keeps its seat, and what happens to a login over its connection limit (`over_limit`).
- A **panel** is a front end on a backend: its own API host, a lineup drawn from the backend's
  channels (possibly renamed and re-categorized), and a guide. Two panels on one backend is the
  shape of one provider seen through two services. A panel is either `xtream` (player_api.php,
  get.php, xmltv.php) or `m3u` (a plain playlist URL and a plain guide URL).
- A **login** is a set of credentials on a backend, valid on one or more of its panels, with
  the account facts an Xtream auth answer carries.
- A **guide site** is a third-party listings source covering some backends' channels under
  its own id spelling.
- **Faults** (sim/faults.py) the world applies from the start, standing or on a trigger.
"""
import datetime as _dt
import os
import re
from dataclasses import dataclass, field

import yaml

from .calibration import Calibration
from .content import PROFILES, TS_PROFILES
from .lineup import build_backend, build_panel, build_site
from .rand import derive

LOGIN_STATUSES = ('Active', 'Expired', 'Banned', 'Disabled')
PANEL_KINDS = ('xtream', 'm3u')
SERVER_INFO = ('stream', 'api', 'none')
ID_SPELLINGS = ('callsign_dot_country', 'word_dash_number', 'numeric')
URL_SHAPES = ('bare', 'live_ts', 'live_m3u8', 'bare_with_ext')
LOGO_HOST = 'logos'
CONTENT_CALIBRATED = 'calibrated'
# What a backend does when a login opens more connections than it allows. kick_oldest is what
# a real provider did, seen once (DESIGN-simulator.md §3.10, dev/changelog/1220); refuse and
# allow_then_ban are assumed.
OVER_LIMIT_POLICIES = ('refuse', 'kick_oldest', 'allow_then_ban')

_NAME_RX = re.compile(r'^[a-z0-9][a-z0-9-]{0,40}$')
_CRED_RX = re.compile(r'^[A-Za-z0-9]{3,40}$')
_OFFSET_RX = re.compile(r'^[+-]\d{4}$')


class WorldError(ValueError):
    """A world file that cannot be built, with the reason in words."""


def _take(d: dict, where: str, allowed: dict) -> dict:
    """`allowed` maps each permitted key to its default. Unknown keys are an error."""
    if d is None:
        d = {}
    if not isinstance(d, dict):
        raise WorldError(f'{where} must be a mapping, not {type(d).__name__}')
    unknown = sorted(set(d) - set(allowed))
    if unknown:
        raise WorldError(f'{where}: unknown key(s) {", ".join(unknown)}; '
                         f'allowed: {", ".join(sorted(allowed))}')
    return {k: d.get(k, v) for k, v in allowed.items()}


def _name(value, where: str) -> str:
    if not isinstance(value, str) or not _NAME_RX.match(value):
        raise WorldError(f'{where}: name {value!r} must be lowercase letters, digits and dashes')
    return value


def _share(value, where: str) -> float:
    try:
        v = float(value)
    except (TypeError, ValueError):
        raise WorldError(f'{where} must be a number from 0 to 1, not {value!r}') from None
    if not 0.0 <= v <= 1.0:
        raise WorldError(f'{where} must be from 0 to 1, not {v}')
    return v


def _count(value, where: str, low: int = 0) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < low:
        raise WorldError(f'{where} must be a whole number of at least {low}, not {value!r}')
    return value


def _choice(value, where: str, options) -> str:
    if value not in options:
        raise WorldError(f'{where} must be one of {", ".join(options)}, not {value!r}')
    return value


def _weights(value, where: str, options) -> dict:
    if not isinstance(value, dict) or not value:
        raise WorldError(f'{where} must map one or more of {", ".join(options)} to a weight')
    out = {}
    for k, v in value.items():
        _choice(k, f'{where} key', options)
        try:
            out[k] = float(v)
        except (TypeError, ValueError):
            raise WorldError(f'{where}.{k} must be a number, not {v!r}') from None
        if out[k] < 0:
            raise WorldError(f'{where}.{k} must not be negative')
    if not any(out.values()):
        raise WorldError(f'{where} needs at least one positive weight')
    return out


@dataclass(frozen=True)
class OverLimit:
    policy: str = 'kick_oldest'
    status: int = 403           # the refusal's HTTP status (refuse)
    ban_after: int = 3          # over-limit connections before the login is banned
    kick_delay_seconds: float = 5.5     # how long the cut-off connection still flows (kick_oldest)


@dataclass(frozen=True)
class BackendSpec:
    name: str
    channels: int
    stream_hosts: int
    radio_share: float
    no_id_share: float
    content: str = CONTENT_CALIBRATED
    burst_seconds: float = 25.0
    stream_redirect: bool = True
    hls_segment_seconds: float = 10.0
    hls_window: int = 6
    hls_idle_seconds: float = 60.0
    hls_variant: str | None = 'h264_720p2997_aac'
    over_limit: OverLimit = OverLimit()

    def stream_host_names(self) -> list[str]:
        return [f'{self.name}-streams-{i + 1}' for i in range(self.stream_hosts)]

    @property
    def thirdparty_host(self) -> str:
        return f'{self.name}-thirdparty'


@dataclass(frozen=True)
class PanelSpec:
    name: str
    backend: str
    kind: str
    share: float
    rename: float
    recategorize: float
    url_shape: dict
    server_info: str
    streams_on_api_host: bool
    id_spelling: str
    guide_coverage: float
    guide_days_ahead: float
    guide_days_behind: float
    guide_offset: str
    guide_gzip: bool
    vod_rows: int
    cuid: bool
    extinf_duration: int


@dataclass(frozen=True)
class GuideSiteSpec:
    name: str
    backends: tuple
    coverage: float
    id_spelling: str
    days_ahead: float
    days_behind: float
    offset: str


@dataclass(frozen=True)
class LoginSpec:
    username: str
    password: str
    backend: str
    panels: tuple
    status: str
    max_connections: int
    is_trial: bool
    exp_date: _dt.datetime | None
    allowed_output_formats: tuple


@dataclass
class World:
    """A built world. Every lineup is generated when the world is built, so serving never
    draws a random number for anything but a guide slot, which is fixed by its own seed."""
    seed: int
    calibration: Calibration
    backends: dict
    panels: dict
    guide_sites: dict
    logins: dict
    hosts_down: frozenset
    lineups: dict = field(default_factory=dict)     # backend name -> lineup.BackendLineup
    panel_lineups: dict = field(default_factory=dict)  # panel name -> lineup.PanelLineup
    site_lineups: dict = field(default_factory=dict)   # guide site name -> lineup.SiteLineup
    faults: tuple = ()      # faults.Fault the world file declares, applied from the start

    def host_names(self) -> list[str]:
        """Every host the world serves, in the fixed order addresses are handed out."""
        hosts = [p for p in self.panels]
        for b in self.backends.values():
            hosts += b.stream_host_names()
            if self.lineups[b.name].needs_thirdparty:
                hosts.append(b.thirdparty_host)
        hosts += list(self.guide_sites)
        hosts.append(LOGO_HOST)
        return hosts

    def logins_on(self, panel: str) -> list:
        return [lg for lg in self.logins.values() if panel in lg.panels]

    def login_for(self, panel: str, username, password):
        """The login these credentials name on this panel, or None."""
        lg = self.logins.get(username)
        if lg is None or lg.password != password or panel not in lg.panels:
            return None
        return lg


def _seconds(value, where: str, low: float = 0.0) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value < low:
        raise WorldError(f'{where} must be a number of seconds, at least {low}, not {value!r}')
    return float(value)


def _flag(value, where: str) -> bool:
    if not isinstance(value, bool):
        raise WorldError(f'{where} must be true or false, not {value!r}')
    return value


def _ts_profile(value, where: str) -> str:
    if value not in TS_PROFILES:
        raise WorldError(f'{where} must name an MPEG-TS content profile, not {value!r}; '
                         f'profiles are {", ".join(TS_PROFILES)}')
    return value


def _over_limit(d, where: str) -> OverLimit:
    default = OverLimit()
    v = _take(d, where, {'policy': default.policy, 'status': default.status,
                         'ban_after': default.ban_after,
                         'kick_delay_seconds': default.kick_delay_seconds})
    status = v['status']
    if isinstance(status, bool) or not isinstance(status, int) or not 400 <= status <= 999:
        raise WorldError(f'{where}.status must be an HTTP status from 400 to 999, not {status!r}')
    return OverLimit(_choice(v['policy'], f'{where}.policy', OVER_LIMIT_POLICIES), status,
                     _count(v['ban_after'], f'{where}.ban_after', 1),
                     _seconds(v['kick_delay_seconds'], f'{where}.kick_delay_seconds'))


def _backend(d, i, cal) -> BackendSpec:
    where = f'backends[{i}]'
    v = _take(d, where, {'name': None, 'channels': 200, 'stream_hosts': 1,
                         'radio_share': 0.002, 'no_id_share': None,
                         'content': CONTENT_CALIBRATED,
                         'burst_seconds': cal.buffer_burst_seconds,
                         'stream_redirect': BackendSpec.stream_redirect,
                         'hls_segment_seconds': BackendSpec.hls_segment_seconds,
                         'hls_window': BackendSpec.hls_window,
                         'hls_idle_seconds': BackendSpec.hls_idle_seconds,
                         'hls_variant': 'h264_720p2997_aac', 'over_limit': None})
    no_id = v['no_id_share']
    if no_id is None:
        no_id = cal.stream_url_shape.get('no_id', 0.0)
    content = v['content']
    if content != CONTENT_CALIBRATED:
        if content not in PROFILES:
            raise WorldError(f'{where}.content must be {CONTENT_CALIBRATED} or a content '
                             f'profile, not {content!r}; profiles are {", ".join(TS_PROFILES)}')
        _ts_profile(content, f'{where}.content')
    variant = v['hls_variant']
    if variant is not None:
        _ts_profile(variant, f'{where}.hls_variant')
    return BackendSpec(
        name=_name(v['name'], f'{where}.name'),
        channels=_count(v['channels'], f'{where}.channels', 1),
        stream_hosts=_count(v['stream_hosts'], f'{where}.stream_hosts', 1),
        radio_share=_share(v['radio_share'], f'{where}.radio_share'),
        no_id_share=_share(no_id, f'{where}.no_id_share'),
        content=content,
        burst_seconds=_seconds(v['burst_seconds'], f'{where}.burst_seconds'),
        stream_redirect=_flag(v['stream_redirect'], f'{where}.stream_redirect'),
        hls_segment_seconds=_seconds(v['hls_segment_seconds'], f'{where}.hls_segment_seconds',
                                     1.0),
        hls_window=_count(v['hls_window'], f'{where}.hls_window', 1),
        hls_idle_seconds=_seconds(v['hls_idle_seconds'], f'{where}.hls_idle_seconds', 1.0),
        hls_variant=variant,
        over_limit=_over_limit(v['over_limit'], f'{where}.over_limit'))


def _default_url_shape(cal) -> dict:
    shape = cal.stream_url_shape
    out = {'bare': shape.get('bare', 0.0), 'live_ts': shape.get('live_ts', 0.0),
           'live_m3u8': shape.get('hls_other', 0.0) + shape.get('live_m3u8', 0.0),
           'bare_with_ext': shape.get('bare_with_ext', 0.0)}
    return {k: v for k, v in out.items() if v > 0} or {'bare': 1.0}


def _panel(d, i, cal, backends) -> PanelSpec:
    where = f'panels[{i}]'
    v = _take(d, where, {
        'name': None, 'backend': None, 'kind': 'xtream', 'share': 1.0, 'rename': 0.0,
        'recategorize': 0.0, 'url_shape': None, 'server_info': 'stream',
        'streams_on_api_host': False, 'id_spelling': 'callsign_dot_country',
        'guide_coverage': cal.guide_coverage_of_tvg_ids, 'guide_days_ahead': cal.days_ahead,
        'guide_days_behind': 0.25, 'guide_offset': '+0000', 'guide_gzip': False,
        'vod_rows': None, 'cuid': False, 'extinf_duration': -1})
    name = _name(v['name'], f'{where}.name')
    if v['backend'] not in backends:
        raise WorldError(f'{where}.backend {v["backend"]!r} names no backend')
    vod = v['vod_rows']
    if vod is None:
        # VOD rows are rare in every playlist measured (0.05% at most), but some are served
        # so the playlist always has something for the importer to leave out.
        vod = max(2, round(backends[v['backend']].channels * 0.0005))
    offset = v['guide_offset']
    if not isinstance(offset, str) or not _OFFSET_RX.match(offset):
        raise WorldError(f'{where}.guide_offset must look like +0000 or -0500, not {offset!r}')
    if v['extinf_duration'] not in (-1, 0):
        raise WorldError(f'{where}.extinf_duration must be -1 or 0')
    return PanelSpec(
        name=name, backend=v['backend'], kind=_choice(v['kind'], f'{where}.kind', PANEL_KINDS),
        share=_share(v['share'], f'{where}.share'),
        rename=_share(v['rename'], f'{where}.rename'),
        recategorize=_share(v['recategorize'], f'{where}.recategorize'),
        url_shape=_weights(v['url_shape'] or _default_url_shape(cal), f'{where}.url_shape',
                           URL_SHAPES),
        server_info=_choice(v['server_info'], f'{where}.server_info', SERVER_INFO),
        streams_on_api_host=bool(v['streams_on_api_host']),
        id_spelling=_choice(v['id_spelling'], f'{where}.id_spelling', ID_SPELLINGS),
        guide_coverage=_share(v['guide_coverage'], f'{where}.guide_coverage'),
        guide_days_ahead=float(v['guide_days_ahead']),
        guide_days_behind=float(v['guide_days_behind']),
        guide_offset=offset, guide_gzip=bool(v['guide_gzip']),
        vod_rows=_count(vod, f'{where}.vod_rows'), cuid=bool(v['cuid']),
        extinf_duration=v['extinf_duration'])


def _site(d, i, cal, backends) -> GuideSiteSpec:
    where = f'guide_sites[{i}]'
    v = _take(d, where, {'name': None, 'backends': None, 'coverage': 0.8,
                         'id_spelling': 'word_dash_number', 'days_ahead': 3.0,
                         'days_behind': 0.25, 'offset': '+0000'})
    names = v['backends'] or list(backends)
    for b in names:
        if b not in backends:
            raise WorldError(f'{where}.backends names no backend {b!r}')
    if not isinstance(v['offset'], str) or not _OFFSET_RX.match(v['offset']):
        raise WorldError(f'{where}.offset must look like +0000 or -0500')
    return GuideSiteSpec(
        name=_name(v['name'], f'{where}.name'), backends=tuple(names),
        coverage=_share(v['coverage'], f'{where}.coverage'),
        id_spelling=_choice(v['id_spelling'], f'{where}.id_spelling', ID_SPELLINGS),
        days_ahead=float(v['days_ahead']), days_behind=float(v['days_behind']),
        offset=v['offset'])


def _invented_credential(seed, *parts) -> str:
    alphabet = 'abcdefghjkmnpqrstuvwxyz23456789'
    n = derive(seed, 'credential', *parts)
    out = []
    for _ in range(10):
        n, k = divmod(n, len(alphabet))
        out.append(alphabet[k])
    return ''.join(out)


def _login(d, i, seed, backends, panels) -> LoginSpec:
    where = f'logins[{i}]'
    v = _take(d, where, {'username': None, 'password': None, 'backend': None, 'panels': None,
                         'status': 'Active', 'max_connections': 1, 'is_trial': False,
                         'exp_date': None,
                         'allowed_output_formats': ['m3u8', 'ts', 'rtmp']})
    if v['backend'] not in backends:
        raise WorldError(f'{where}.backend {v["backend"]!r} names no backend')
    username = v['username'] or _invented_credential(seed, 'user', i)
    password = v['password'] or _invented_credential(seed, 'pass', i)
    for label, cred in (('username', username), ('password', password)):
        if not isinstance(cred, str) or not _CRED_RX.match(cred):
            raise WorldError(f'{where}.{label} must be 3 to 40 letters and digits')
    on = v['panels'] or [p.name for p in panels.values() if p.backend == v['backend']]
    for p in on:
        if p not in panels:
            raise WorldError(f'{where}.panels names no panel {p!r}')
        if panels[p].backend != v['backend']:
            raise WorldError(f'{where}: panel {p!r} is on backend {panels[p].backend!r}, '
                             f'not this login\'s backend {v["backend"]!r}')
    exp = v['exp_date']
    if exp is not None:
        if isinstance(exp, _dt.date) and not isinstance(exp, _dt.datetime):
            exp = _dt.datetime(exp.year, exp.month, exp.day)
        if not isinstance(exp, _dt.datetime):
            raise WorldError(f'{where}.exp_date must be a date like 2027-01-31')
    return LoginSpec(
        username=username, password=password, backend=v['backend'], panels=tuple(on),
        status=_choice(v['status'], f'{where}.status', LOGIN_STATUSES),
        max_connections=_count(v['max_connections'], f'{where}.max_connections', 1),
        is_trial=bool(v['is_trial']), exp_date=exp,
        allowed_output_formats=tuple(v['allowed_output_formats'] or ()))


def build_world(data: dict, seed: int | None = None) -> World:
    """A world from its parsed YAML. `seed` overrides the file's own."""
    v = _take(data, 'world', {'seed': 1, 'calibration': 'prod', 'backends': None,
                              'panels': None, 'guide_sites': [], 'logins': None,
                              'hosts_down': [], 'faults': []})
    seed = v['seed'] if seed is None else seed
    _count(seed, 'seed')
    try:
        cal = Calibration(v['calibration'])
    except ValueError as exc:
        raise WorldError(str(exc)) from None
    if not v['backends']:
        raise WorldError('a world needs at least one backend')

    backends = {}
    for i, d in enumerate(v['backends']):
        b = _backend(d, i, cal)
        if b.name in backends:
            raise WorldError(f'two backends are named {b.name!r}')
        backends[b.name] = b
    panels = {}
    for i, d in enumerate(v['panels'] or [{'name': n, 'backend': n} for n in backends]):
        p = _panel(d, i, cal, backends)
        if p.name in panels:
            raise WorldError(f'two panels are named {p.name!r}')
        panels[p.name] = p
    sites = {}
    for i, d in enumerate(v['guide_sites'] or []):
        s = _site(d, i, cal, backends)
        sites[s.name] = s
    logins = {}
    login_data = v['logins'] or [{'backend': b} for b in backends]
    for i, d in enumerate(login_data):
        lg = _login(d, i, seed, backends, panels)
        if lg.username in logins:
            raise WorldError(f'two logins share the username {lg.username!r}')
        logins[lg.username] = lg

    world = World(seed=seed, calibration=cal, backends=backends, panels=panels,
                  guide_sites=sites, logins=logins, hosts_down=frozenset())
    for b in backends.values():
        world.lineups[b.name] = build_backend(world, b)
    for p in panels.values():
        world.panel_lineups[p.name] = build_panel(world, p)
    for s in sites.values():
        world.site_lineups[s.name] = build_site(world, s)

    names = world.host_names()
    if len(set(names)) != len(names):
        dupes = sorted({n for n in names if names.count(n) > 1})
        raise WorldError(f'host names collide: {", ".join(dupes)} (a panel or guide site is '
                         'named like a generated stream host)')
    down = set(v['hosts_down'] or [])
    unknown = sorted(down - set(names))
    if unknown:
        raise WorldError(f'hosts_down names no host: {", ".join(unknown)}; '
                         f'hosts are {", ".join(names)}')
    world.hosts_down = frozenset(down)
    # Imported here: a fault is checked against the built world, and sim.faults reads this
    # module's constants, so a module-level import would be circular.
    from .faults import FaultError, faults_from_world
    try:
        world.faults = faults_from_world(world, v['faults'])
    except FaultError as exc:
        raise WorldError(str(exc)) from None
    return world


def load_world(source, seed: int | None = None) -> World:
    """A world from a YAML path, YAML text, or an already-parsed mapping."""
    if isinstance(source, dict):
        return build_world(source, seed)
    if isinstance(source, str) and '\n' not in source and os.path.exists(source):
        with open(source, encoding='utf-8') as f:
            source = f.read()
    try:
        data = yaml.safe_load(source)
    except yaml.YAMLError as exc:
        raise WorldError(f'not readable as YAML: {exc}') from None
    if not isinstance(data, dict):
        raise WorldError('a world file must be a YAML mapping')
    return build_world(data, seed)
