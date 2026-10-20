"""Command line: serve a world, write every answer it would give to a folder, list the faults
it can apply, or render the loops its streams are cut from.

    python3 -m sim serve --world sim/worlds/small.yaml
    python3 -m sim serve --world W --domain sim.test --address 10.0.0.128 --port 80 \
        --control-address 10.0.0.10 --control-port 8000 --dns-port 53
    python3 -m sim dump --world sim/worlds/small.yaml --out /some/folder --at 2026-10-17T12:00
    python3 -m sim faults
    python3 -m sim content build --world sim/worlds/small.yaml
    python3 -m sim content list

The loops live in the per-user cache directory unless SIM_CONTENT_DIR or --content-dir says
otherwise; `serve` renders what its world needs before it listens.
"""
import argparse
import datetime as _dt
import json
import os
import signal
import sys
import threading

from . import SIM_VERSION
from .content import DEFAULT_BUDGET_BYTES, PROFILES, ContentError, Library
from .control import Control, ControlServer
from .dns import DnsResponder
from .faults import CATALOG
from .provider import Layout, Provider
from .server import Simulator
from .world import WorldError, load_world

_DEFAULT_WORLD = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'worlds', 'small.yaml')


def _time(value: str) -> _dt.datetime:
    """An ISO time, read as UTC when it carries no offset."""
    t = _dt.datetime.fromisoformat(value)
    if t.tzinfo is not None:
        t = t.astimezone(_dt.timezone.utc).replace(tzinfo=None)
    return t


def _print_accounts(sim, out=sys.stdout):
    for a in sim.accounts():
        print(f'{a.panel} ({a.kind}) login {a.username} [{a.status}]', file=out)
        if a.base_url:
            print(f'  base url   {a.base_url}', file=out)
            print(f'  username   {a.username}', file=out)
            print(f'  password   {a.password}', file=out)
        print(f'  playlist   {a.m3u_url}', file=out)
        print(f'  guide      {a.epg_url}', file=out)
    for site in sim.world.guide_sites:
        print(f'{site} (guide site)', file=out)
        print(f'  guide      {sim.guide_site_url(site)}', file=out)
        print(f'             {sim.guide_site_url(site, gzipped=True)}', file=out)
    down = sorted(sim.world.hosts_down)
    if down:
        print(f'down (refusing connections): {", ".join(down)}', file=out)
    for f in sim.faults.active():
        print(f'fault: {f.fault.describe()}', file=out)


def _library(args) -> Library:
    return Library(args.content_dir, budget_bytes=int(args.budget_mb * 1024 * 1024))


def cmd_serve(args) -> int:
    world = load_world(args.world, seed=args.seed)
    if args.dns_port and not args.domain:
        print('--dns-port needs --domain: the responder answers for <host>.<domain>',
              file=sys.stderr)
        return 2
    sim = Simulator(world, address=args.address, spread=not args.single_address,
                    port=args.port, clock=_time(args.clock) if args.clock else None,
                    content=_library(args), domain=args.domain)
    if not args.no_build:
        sim.prepare_content(log=print)
    elif args.preload:
        # Parsed before the first connection asks: a first parse can outlast a client's read
        # timeout at open, a start-up cost no real provider has.
        for name in sim.content_needed():
            if sim.library.has(name):
                sim.library.load(name)
    sim.start()
    dns = control = None
    try:
        if args.dns_port:
            dns = DnsResponder(sim.dns_names(), args.control_address, args.dns_port).start()
        if args.control_port is not None:
            control = ControlServer(Control(sim, dns), args.control_address,
                                    args.control_port).start()
        print(f'simulator {SIM_VERSION}: world {args.world}, seed {world.seed}, '
              f'{sum(len(lu.channels) for lu in world.lineups.values())} backend channels')
        _print_accounts(sim)
        if dns is not None:
            print(f'DNS for *.{args.domain} on {dns.address[0]}:{dns.address[1]}/udp')
        if control is not None:
            print(f'control API on http://{control.address[0]}:{control.address[1]}')
        print('serving - Ctrl-C to stop', flush=True)
        done = threading.Event()
        signal.signal(signal.SIGTERM, lambda *_: done.set())
        try:
            done.wait()
        except KeyboardInterrupt:
            pass
    finally:
        if control is not None:
            control.stop()
        if dns is not None:
            dns.stop()
        sim.stop()
        print(f'stopped after {len(sim.ledger)} request(s)')
    return 0


def cmd_dump(args) -> int:
    """Every endpoint's answer for every account, as files, from a layout no socket backs."""
    world = load_world(args.world, seed=args.seed)
    at = _time(args.at) if args.at else _dt.datetime.now(_dt.timezone.utc).replace(tzinfo=None)
    provider = Provider(world, Layout.virtual(world, args.address, args.port), lambda: at)
    os.makedirs(args.out, exist_ok=True)
    written = []

    def write(rel, body):
        path = os.path.join(args.out, rel)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, 'wb') as f:
            f.write(body)
        written.append((rel, len(body)))

    for panel in world.panels.values():
        for lg in world.logins_on(panel.name):
            base = f'{panel.name}/{lg.username}'
            if panel.kind == 'xtream':
                q = f'username={lg.username}&password={lg.password}'
                for rel, target in (('auth.json', f'/player_api.php?{q}'),
                                    ('live_categories.json',
                                     f'/player_api.php?{q}&action=get_live_categories'),
                                    ('live_streams.json',
                                     f'/player_api.php?{q}&action=get_live_streams'),
                                    ('playlist.m3u', f'/get.php?{q}&type=m3u_plus&output=ts'),
                                    ('xmltv.xml', f'/xmltv.php?{q}')):
                    write(f'{base}/{rel}', provider.handle(panel.name, 'GET', target).body)
            else:
                cred = f'{lg.username}/{lg.password}'
                write(f'{base}/playlist.m3u',
                      provider.handle(panel.name, 'GET', f'/playlist/{cred}.m3u').body)
                write(f'{base}/guide.xml',
                      provider.handle(panel.name, 'GET', f'/guide/{cred}.xml').body)
    for site in world.guide_sites:
        write(f'{site}/guide.xml', provider.handle(site, 'GET', '/guide.xml').body)
    manifest = {'sim_version': SIM_VERSION, 'seed': world.seed, 'at': at.isoformat(),
                'files': [{'path': p, 'bytes': n} for p, n in written]}
    write('manifest.json', json.dumps(manifest, indent=2).encode('utf-8'))
    print(f'wrote {len(written) - 1} file(s) to {args.out}')
    return 0


def cmd_faults(args, out=sys.stdout) -> int:
    """Every fault kind, with where its behavior comes from."""
    for k in CATALOG.values():
        print(f'{k.name}  [{k.scope}, {k.basis}]', file=out)
        print(f'  {k.summary}', file=out)
        print(f'  endpoints: {", ".join(k.endpoints)}', file=out)
        if k.params:
            settings = ', '.join(f'{n}={"required" if n not in k.defaults() else k.defaults()[n]}'
                                 for n in k.params)
            print(f'  settings: {settings}', file=out)
        print(f'  evidence: {k.evidence}', file=out)
    return 0


def cmd_content(args, out=sys.stdout) -> int:
    lib = _library(args)
    if args.action == 'list':
        print(f'content cache: {lib.directory} ({lib.size() / 1e6:.1f} MB of a '
              f'{lib.budget_bytes / 1e6:.0f} MB budget)', file=out)
        for name, p in PROFILES.items():
            m = lib.find(name)
            state = f'{m["bytes"] / 1e6:6.1f} MB' if m else '   not built'
            print(f'  {state}  {name:24s} {p.summary}', file=out)
        stale = [m for m in lib.entries() if not m['current']]
        if stale:
            print(f'  {len(stale)} render(s) of specs no profile has any more; `content prune` '
                  'deletes them', file=out)
        return 0
    if args.action == 'prune':
        removed = lib.prune()
        print(f'removed {len(removed)} file(s)', file=out)
        return 0
    names = list(args.profile or [])
    if args.world:
        names += Simulator(load_world(args.world)).content_needed()
    if args.all:
        names += list(PROFILES)
    if not names:
        print('name --profile, --world or --all', file=sys.stderr)
        return 2
    results = lib.build(names, jobs=args.jobs, log=lambda m: print(m, file=out, flush=True))
    built = [r for r in results if r.rendered]
    print(f'{len(built)} rendered, {len(results) - len(built)} already in {lib.directory}',
          file=out)
    return 0


def _content_args(p):
    p.add_argument('--content-dir', help='where the rendered loops live (default: '
                   'SIM_CONTENT_DIR, else the per-user cache)')
    p.add_argument('--budget-mb', type=float, default=DEFAULT_BUDGET_BYTES / 1024 / 1024,
                   help='refuse a render that takes the cache past this (default: %(default)s)')


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog='python3 -m sim', description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest='command', required=True)
    sub.add_parser('faults', help='list every fault kind and its evidence')
    content = sub.add_parser('content', help='render, list or prune the loops streams are '
                             'cut from')
    content.add_argument('action', choices=('build', 'list', 'prune'))
    content.add_argument('--profile', action='append', choices=list(PROFILES),
                         help='a profile to render (repeatable)')
    content.add_argument('--world', help='render every loop this world file uses')
    content.add_argument('--all', action='store_true', help='render every profile')
    content.add_argument('--jobs', type=int, help='renders at once (default: one per core)')
    _content_args(content)
    for name, helptext in (('serve', 'serve a world over HTTP until stopped'),
                           ('dump', 'write every answer a world gives to a folder')):
        p = sub.add_parser(name, help=helptext)
        p.add_argument('--world', default=_DEFAULT_WORLD, help='world file (default: %(default)s)')
        p.add_argument('--seed', type=int, help="override the world file's seed")
        p.add_argument('--address', default='127.0.0.2',
                       help='first address handed to a host (default: %(default)s)')
    serve = sub.choices['serve']
    serve.add_argument('--port', type=int, default=0,
                       help='port for every host (default: a free one per host)')
    serve.add_argument('--single-address', action='store_true',
                       help='every host on --address, each on its own port')
    serve.add_argument('--clock', help='hold the clock at this ISO time (UTC unless it says)')
    serve.add_argument('--no-build', action='store_true',
                       help='serve without rendering missing loops (their streams answer 503)')
    serve.add_argument('--preload', action='store_true',
                       help='with --no-build, parse every loop the world uses before listening')
    serve.add_argument('--domain', help='name every host <host>.<DOMAIN> in the URLs handed '
                       'out, and allow addresses outside loopback (a container that holds '
                       'them)')
    serve.add_argument('--control-port', type=int,
                       help='serve the control API (sim/control.py) on this port')
    serve.add_argument('--control-address', default='127.0.0.1',
                       help='address for the control API and the DNS responder '
                       '(default: %(default)s)')
    serve.add_argument('--dns-port', type=int,
                       help='answer DNS for the --domain names on this UDP port')
    _content_args(serve)
    dump = sub.choices['dump']
    dump.add_argument('--out', required=True, help='folder to write into')
    dump.add_argument('--at', help='the time to answer as of, ISO (default: now)')
    dump.add_argument('--port', type=int, default=8080,
                      help='port the URLs inside the answers name (default: %(default)s)')
    args = ap.parse_args(argv)
    if args.command == 'faults':
        return cmd_faults(args)
    try:
        if args.command == 'content':
            return cmd_content(args)
        return cmd_serve(args) if args.command == 'serve' else cmd_dump(args)
    except WorldError as exc:
        print(f'world file error: {exc}', file=sys.stderr)
        return 2
    except ContentError as exc:
        print(f'content error: {exc}', file=sys.stderr)
        return 2


if __name__ == '__main__':
    sys.exit(main())
