# sim - a simulated IPTV provider

`sim/` is a fake IPTV provider that ChannelBin cannot tell from a real one. It serves generated
accounts, playlists, guides, logos and live streams over the same endpoints a real Xtream or M3U
provider serves, it breaks on command in the ways real providers have broken, and it keeps a
record of exactly what it sent, so a test can check ChannelBin's own numbers against the truth.

ChannelBin's test suite uses it to sync over real HTTP and to make real recordings with real
ffmpeg, without touching a real provider. It is a standalone package: it never imports
ChannelBin, and ChannelBin has no setting, flag or code path that knows it exists.

## What it serves

- **Xtream panels:** `player_api.php` (the login, live categories, live streams; an action it
  does not know answers `[]`, the way real panels do), `get.php` and `xmltv.php`.
- **Plain M3U and XMLTV URLs**, and a third-party guide site with its own channel ids.
- **Live streams** in every URL spelling providers use: MPEG-TS, HLS, and MP3 radio mounts,
  served on stream hosts separate from the API hosts, with a per-login connection limit.
- **Logos**, drawn from a hash.

Everything it serves is invented. Channel names are built from syllables, logos are generated,
and streams are cut from test-pattern loops it renders itself with ffmpeg. No real stream is
relayed and no real name, host or URL is copied in.

## Worlds

A world is a YAML file plus a seed: backends (the channels and their streams), panels (the
services a user signs in to, several of which may sit in front of one backend), logins, and
guide sites. The same world file, seed and `SIM_VERSION` produce the same bytes every time, so a
test written against a world keeps meaning the same thing. When a change alters what the
generators produce, `SIM_VERSION` goes up with it.

Shapes the generators do not set explicitly (name decorations, guide coverage, program lengths,
duplicate rates and so on) come from `sim/profiles/default.yaml`, a set of aggregate numbers
measured on real installs. It holds counts, shares and percentiles only, never a name.

Worlds that ship with it, in `sim/worlds/`:

| World | What it is for |
|---|---|
| `small.yaml` | a few hundred channels covering each shape a real install has met (one backend behind two panels, a shared login, a gzipped guide), for trying it by hand and for tests |
| `scale-prod.yaml` | a lineup the size of a real large install |
| `scale-large.yaml` | about 300,000 channels, for finding where the import path stops scaling |

## Using it

From Python, in a test or a script:

```python
from sim import load_world, Simulator

world = load_world('sim/worlds/small.yaml')
with Simulator(world) as s:
    account = s.accounts()[0]     # base_url, username, password, m3u_url, epg_url
    s.inject('truncated_chunked', 'harbor-tv', endpoints=['guide'])
    ...                           # point a client at account.base_url
    s.ledger.entries(endpoint='guide')   # every guide request answered, with its faults
```

In a unittest, `sim.serve_for_test(test_case, world)` starts one on an ephemeral loopback port
and registers its shutdown as the test's own cleanup.

From the command line:

```
python3 -m sim serve --world sim/worlds/small.yaml     serve a world until stopped
python3 -m sim dump --world sim/worlds/small.yaml --out FOLDER --at 2026-10-17T12:00
python3 -m sim faults                                  every fault kind and its evidence
python3 -m sim content build --world sim/worlds/small.yaml
python3 -m sim content list
```

A served simulator also listens on a control port of its own (`sim/control.py`), never on a
provider-facing host, so it can be driven from another process or container: inject and clear
faults, read the ledger, change a login's status, cut its connections, move the clock.

## Faults

`python3 -m sim faults` lists every fault kind. Each one is aimed at a target (a login, a panel,
a host, a channel, or one connection to a channel), can be limited to some endpoints, and has a
trigger: a window on the simulator's clock and a count of the requests it applies to.

They act at seven layers: how a **login** is treated (refused, expired, rate limited), the whole
**answer** replaced (a status code, an HTML error page), the **model** a panel renders from (rows
dropped, renamed, duplicated, re-spelled), the rendered **bytes** (a BOM, a comment above the
header, a cut), the **wire** (a chunked body cut short, a slow drip), how a stream request is
answered at **open** (refused, held, a placeholder clip), and what a **stream** does once it
flows (silence, a close, video withheld, a different pace, timestamps that jump).

Every kind carries the evidence for the behavior it reproduces and how firm that evidence is, in
one of four words:

- **measured** - the real behavior was measured with numbers, on a real feed or capture
- **observed** - seen on a real feed, but not measured closely enough to build from alone
- **reproduced** - rebuilt synthetically, from an observation or from reading the code
- **assumed** - nobody has seen it; the simulator offers it because the client handles it

A test passing against an assumed fault says less than one passing against a measured one, and
the catalog keeps the two apart rather than rounding up.

## The answer key

`sim/answer_key.py` reads a client's recording, places every frame on the channel's own clock
by its content, and checks it against the ledger: which seconds of the channel the recording
holds, which it lost, and why (not sent, withheld, refused, the placeholder clip). A test can
then compare the client's own reported figures with what really happened:

```python
from sim.answer_key import check

answer = check(s, ['segment-1.ts', 'segment-2.ts'])
answer.compare({'lost_seconds': 12.0})   # [] when the reported figure is right
```

## The content library

Streams are cut from loops rendered once by ffmpeg and cached, by default in
`~/.cache/channelbin-sim/content` (`SIM_CONTENT_DIR` or `--content-dir` moves it). Each loop is
keyed on its render spec and the ffmpeg version, so a different ffmpeg renders its own copy
rather than reusing one it did not make. The whole library is about 480 MB; a test renders only
the loops its world needs, and `serve` renders them before it starts listening.

## How ChannelBin's tests use it

| Tier | What runs | How to run it |
|---|---|---|
| Sync | ChannelBin's real sync over loopback HTTP against a simulated provider, with and without faults (`tests/test_sim_*.py`) | part of the default suite, `./run_tests.sh` |
| Capture | real scheduled recordings - recorder, watchdog, join and conversion on real ffmpeg - against simulated channels, each checked by the answer key (`tests/capture/scenario_*.py`) | `./run_tests.sh --capture`, about 10 minutes; nightly and on demand in CI |

The capture tier is never part of the default run; `tests/test_capture_smoke.py` is the short
slice of it that is. A capture scenario is admitted only after 20 clean passes in a row
(`./run_tests.sh --capture --repeat 20 --scenario NAME`): a flaky real-time test is a bug, not
something to re-run past.

The test suite's network guard refuses any child process pointed at a URL, with one exception:
an http(s) URL on the exact loopback address and port a simulator in the same process has bound
and not yet stopped. That is what lets a test point the recorder's real ffmpeg command at a
simulated channel, while every other URL is still refused.

## Rules it is held to

- **It never imports ChannelBin**, and ChannelBin never learns it exists. A static test enforces
  the first half.
- **Generated content only.** Nothing real is relayed, copied or fingerprinted.
- **Every fault names its evidence or says it is assumed.**
- **Deterministic to the byte** for a given world, seed and `SIM_VERSION`.
- **When a real provider causes a ChannelBin bug the simulator cannot yet reproduce,** the fix
  adds that behavior as a fault and a test that reproduces the bug through it. That is how the
  simulator keeps up with real providers instead of freezing at what was known the day it was
  built.
