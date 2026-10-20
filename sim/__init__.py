"""A synthetic IPTV provider for testing an IPTV client against.

It serves generated accounts, playlists, guides and logos over the endpoints a real Xtream or
M3U provider serves (`player_api.php`, `get.php`, `xmltv.php`, plain playlist and guide URLs),
live streams in every URL spelling (MPEG-TS, HLS, MP3 radio mounts), and records every request
it answers. Everything it serves is invented: names are built from syllables that belong to no
network, logos are drawn from a hash, and streams are cut from test-pattern loops it renders
itself with ffmpeg (sim/content.py; `python3 -m sim content build`).

    from sim import load_world, Simulator
    world = load_world('sim/worlds/small.yaml')
    with Simulator(world) as s:
        account = s.accounts()[0]       # base_url, username, password, m3u_url, epg_url

From the command line: `python3 -m sim serve --world sim/worlds/small.yaml`.

It can also fail on command, in the ways real providers have failed (sim/faults.py,
`python3 -m sim faults` for the list):

    with Simulator(world) as s:
        s.inject('truncated_chunked', 'harbor-tv', endpoints=['guide'])
        s.inject('video_stops', 'harbor:1001', start=..., end=...)   # a channel, by its time
        s.inject('silent_socket', 'harbor:1001', times=1)         # one connection to it

And it can say what a client's recording of a stream holds - which seconds of the channel,
which it lost and why - from the recording's files and the ledger (sim/answer_key.py):

    from sim.answer_key import check
    answer = check(s, ['segment-1.ts', 'segment-2.ts'])
    answer.compare({'lost_seconds': 12.0})      # [] when the reported figure is right

The package never imports the application it is used to test. A world file, its seed and
`SIM_VERSION` fix every byte served; change what a generator produces and `SIM_VERSION`
changes with it.
"""
from .content import PROFILES, ContentError, Library
from .faults import CATALOG, Fault, FaultError, make_fault
from .ledger import Ledger, Request
from .server import AccountView, Simulator, listening, serve_for_test
from .world import World, WorldError, build_world, load_world

# Raised whenever a change alters what the generators produce for an existing world file, so
# a recorded run says which output it was made against.
SIM_VERSION = 4

__all__ = ['CATALOG', 'PROFILES', 'SIM_VERSION', 'AccountView', 'ContentError', 'Fault',
           'FaultError', 'Ledger', 'Library', 'Request', 'Simulator', 'World', 'WorldError',
           'build_world', 'listening', 'load_world', 'make_fault', 'serve_for_test']
