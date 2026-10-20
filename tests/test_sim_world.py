"""The simulator's worlds: built as declared, refused when they cannot be, and the same to the
byte for the same seed (dev/changelog/1217, DESIGN-simulator.md §2, §3.1).

No sockets here - every answer is read through `sim.provider.Provider` with a layout no socket
backs, which is exactly what the server and `python3 -m sim dump` serve.
"""
import datetime as dt
import functools
import hashlib
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from urllib.parse import urlsplit

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sim import SIM_VERSION, WorldError, build_world, load_world  # noqa: E402
from sim.guide import programs  # noqa: E402
from sim.lineup import KIND_NO_ID, KIND_RADIO, TRIPLET_KINDS  # noqa: E402
from sim.provider import Layout, Provider  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SMALL = os.path.join(ROOT, 'sim', 'worlds', 'small.yaml')
AT = dt.datetime(2026, 10, 17, 12, 0)

# sha256 of every answer the small world gives at AT on the dump layout. Changing what a
# generator produces changes these, and must raise SIM_VERSION in the same edit: a recorded
# run names the version whose bytes it was made against.
GOLDEN_VERSION = 4
GOLDEN = {
    'harbor-plus/7vkhrw3dg5/auth':
        '2826aa8f61adcba5dba8e90c79e039efd56a15eacc5e43fc06923a18bea35bd1',
    'harbor-plus/7vkhrw3dg5/categories':
        '3c38ca2305282c71c79d960357986c5ab0b991c671c51a6ff515e74549841236',
    'harbor-plus/7vkhrw3dg5/guide':
        '143b720ca79d577c55c31180d653fdbe03db634901d140b7b00ab5dc733aee0e',
    'harbor-plus/7vkhrw3dg5/playlist':
        '43d1574696a94e1e7411e8666a3c059f0e33154abe69da4cb3319937a0e367c8',
    'harbor-plus/7vkhrw3dg5/streams':
        'e9e0e0ec08b1e0766ef71eb778d21412a8ef733ddd50ca06ddcf0fe193472214',
    'harbor-tv/7vkhrw3dg5/auth':
        '8059c74736f6aa19b7505eac2798cf32065ffb3ea19371608dd7857f3c54e50e',
    'harbor-tv/7vkhrw3dg5/categories':
        '6b79ec765f1ffa165c173f96f6cb76e01f1cdc8fbaaef23a9c3c4ee3ce784710',
    'harbor-tv/7vkhrw3dg5/guide':
        '8d437603b64ffe3e2aa62631b81151d61cc895e2a3f0f21d1cb7901e1d4efd91',
    'harbor-tv/7vkhrw3dg5/playlist':
        '331d0c3c405b4174491f85c58291f8c1f69ea628c0932755e61030516cf8ded2',
    'harbor-tv/7vkhrw3dg5/streams':
        'eb9d1931a0cff08853486c7855d270a5207477e2ded70b19151f97367a82fe1f',
    'harbor-tv/x7m4n4y8tr/auth':
        '2fcc9b253c7bdcc7f7f5a8fb1d15a0096041d015e15d1b8d8bf117a90463fbb8',
    'harbor-tv/x7m4n4y8tr/categories':
        '6b79ec765f1ffa165c173f96f6cb76e01f1cdc8fbaaef23a9c3c4ee3ce784710',
    'harbor-tv/x7m4n4y8tr/guide':
        '8d437603b64ffe3e2aa62631b81151d61cc895e2a3f0f21d1cb7901e1d4efd91',
    'harbor-tv/x7m4n4y8tr/playlist':
        '54ce066465523b45481a8b905b9a876b869625d0dd370d780ba8d2d2b57da0ba',
    'harbor-tv/x7m4n4y8tr/streams':
        'eb9d1931a0cff08853486c7855d270a5207477e2ded70b19151f97367a82fe1f',
    'listings/guide':
        'f58e2961004353856b519886d1e3ce6c10d6ed064443014ccd24c32e073ef360',
    'meadow-m3u/pxcj6dajfr/guide':
        '2b9913e35143286ee2a325271d467100aaad587a6a766141bf4705bf21cd837b',
    'meadow-m3u/pxcj6dajfr/playlist':
        'fd3196ac504328008abf9eff3c8c778bcde515b006a4a97fd4be15b6751ca935',
}


def _provider(world, at=AT):
    return Provider(world, Layout.virtual(world), lambda: at)


def _answers(world, at=AT) -> dict:
    """{name: bytes} for every endpoint of every account, as `dump` writes them."""
    p = _provider(world, at)
    out = {}
    for panel in world.panels.values():
        for lg in world.logins_on(panel.name):
            key = f'{panel.name}/{lg.username}'
            if panel.kind == 'xtream':
                q = f'username={lg.username}&password={lg.password}'
                for name, target in (('auth', f'/player_api.php?{q}'),
                                     ('categories', f'/player_api.php?{q}&action=get_live_categories'),
                                     ('streams', f'/player_api.php?{q}&action=get_live_streams'),
                                     ('playlist', f'/get.php?{q}&type=m3u_plus&output=ts'),
                                     ('guide', f'/xmltv.php?{q}')):
                    out[f'{key}/{name}'] = p.handle(panel.name, 'GET', target).body
            else:
                cred = f'{lg.username}/{lg.password}'
                out[f'{key}/playlist'] = p.handle(panel.name, 'GET', f'/playlist/{cred}.m3u').body
                out[f'{key}/guide'] = p.handle(panel.name, 'GET', f'/guide/{cred}.xml').body
    for site in world.guide_sites:
        out[f'{site}/guide'] = p.handle(site, 'GET', '/guide.xml').body
    return out


def _digest(answers: dict) -> dict:
    return {k: hashlib.sha256(v).hexdigest() for k, v in sorted(answers.items())}


@functools.lru_cache(maxsize=1)
def _small_digest() -> dict:
    """The small world's digests, computed once for the tests that only read them."""
    return _digest(_answers(load_world(SMALL)))


class WorldValidationTests(unittest.TestCase):
    """A world that cannot be built as written is refused with the reason, never built on a
    default nobody chose."""

    def _refused(self, data, *fragments):
        with self.assertRaises(WorldError) as ctx:
            build_world(data)
        for f in fragments:
            self.assertIn(f, str(ctx.exception))

    def test_unknown_keys_are_refused_at_every_level(self):
        self._refused({'backends': [{'name': 'a'}], 'color': 1}, 'color')
        self._refused({'backends': [{'name': 'a', 'chanels': 5}]}, 'backends[0]', 'chanels')
        self._refused({'backends': [{'name': 'a'}], 'panels': [{'name': 'a', 'backend': 'a',
                                                                 'sharee': 1}]}, 'sharee')

    def test_references_must_resolve(self):
        self._refused({'backends': [{'name': 'a'}], 'panels': [{'name': 'p', 'backend': 'b'}]},
                      'names no backend')
        self._refused({'backends': [{'name': 'a'}, {'name': 'b'}],
                       'panels': [{'name': 'pa', 'backend': 'a'}, {'name': 'pb', 'backend': 'b'}],
                       'logins': [{'backend': 'a', 'panels': ['pb']}]}, "backend 'b'")
        self._refused({'backends': [{'name': 'a'}], 'hosts_down': ['nowhere']}, 'nowhere')

    def test_values_are_checked(self):
        self._refused({'backends': [{'name': 'A b'}]}, 'lowercase')
        self._refused({'backends': [{'name': 'a', 'channels': 0}]}, 'at least 1')
        self._refused({'backends': [{'name': 'a'}], 'panels': [{'name': 'a', 'backend': 'a',
                                                                 'share': 1.5}]}, 'from 0 to 1')
        self._refused({'backends': [{'name': 'a'}], 'logins': [{'backend': 'a',
                                                                 'status': 'Frozen'}]}, 'Frozen')
        self._refused({'backends': [{'name': 'a'}], 'calibration': 'staging'}, 'staging')
        self._refused({'backends': [{'name': 'a'}, {'name': 'a'}]}, 'two backends')

    def test_a_panel_named_like_a_generated_host_is_refused(self):
        self._refused({'backends': [{'name': 'a'}],
                       'panels': [{'name': 'a-streams-1', 'backend': 'a'}]}, 'collide')

    def test_defaults_fill_a_one_line_world(self):
        w = build_world({'backends': [{'name': 'solo', 'channels': 50}]})
        self.assertEqual(list(w.panels), ['solo'])
        self.assertEqual(len(w.logins), 1)
        self.assertEqual(len(w.lineups['solo'].channels), 50)
        self.assertEqual(w.calibration.source, 'prod')


class DeterminismTests(unittest.TestCase):
    """Same world, seed and SIM_VERSION, same bytes (DESIGN-simulator.md §2)."""

    def test_two_builds_answer_identically(self):
        self.assertEqual(_answers(load_world(SMALL)), _answers(load_world(SMALL)))

    def test_another_seed_answers_differently(self):
        a, b = _answers(load_world(SMALL)), _answers(load_world(SMALL, seed=8))
        self.assertNotEqual(a['listings/guide'], b['listings/guide'])

    def test_another_process_with_another_hash_seed_answers_identically(self):
        """Python's own hash() is salted per process; nothing the simulator draws may depend
        on it. `python3 -m sim dump` writes the same answers on the same layout as _answers()."""
        names = {'auth.json': 'auth', 'live_categories.json': 'categories',
                 'live_streams.json': 'streams', 'playlist.m3u': 'playlist',
                 'xmltv.xml': 'guide', 'guide.xml': 'guide'}
        expected = _small_digest()
        for hash_seed in ('1', '2'):
            out = tempfile.mkdtemp(prefix='sim-dump-')
            self.addCleanup(shutil.rmtree, out, True)
            subprocess.run([sys.executable, '-m', 'sim', 'dump', '--out', out,
                            '--at', AT.isoformat()], cwd=ROOT, check=True, capture_output=True,
                           env=dict(os.environ, PYTHONHASHSEED=hash_seed), timeout=120)
            got = {}
            for dirpath, _dirs, files in os.walk(out):
                for f in files:
                    if f == 'manifest.json':
                        continue
                    rel = os.path.relpath(os.path.join(dirpath, f), out)
                    with open(os.path.join(dirpath, f), 'rb') as fh:
                        got[f'{os.path.dirname(rel)}/{names[f]}'] = (
                            hashlib.sha256(fh.read()).hexdigest())
            self.assertEqual(dict(sorted(got.items())), expected, f'PYTHONHASHSEED={hash_seed}')

    def test_the_small_world_matches_its_recorded_bytes(self):
        self.assertEqual(SIM_VERSION, GOLDEN_VERSION,
                         'SIM_VERSION moved: re-record GOLDEN for the new version')
        self.assertEqual(_small_digest(), GOLDEN,
                         'the generators produce different bytes for an unchanged world file. '
                         'If that was intended, raise SIM_VERSION and re-record GOLDEN.')


class LineupTests(unittest.TestCase):
    def setUp(self):
        self.world = load_world(SMALL)
        self.p = _provider(self.world)

    def test_two_panels_on_one_backend_share_its_stream_ids(self):
        tv = {r.channel.stream_id for r in self.world.panel_lineups['harbor-tv'].rows}
        plus = {r.channel.stream_id for r in self.world.panel_lineups['harbor-plus'].rows}
        meadow = {r.channel.stream_id for r in self.world.panel_lineups['meadow-m3u'].rows}
        self.assertEqual(len(tv), 300)
        self.assertTrue(plus <= tv)
        self.assertGreater(len(plus), 0.9 * len(tv))
        self.assertLess(len(tv & meadow), 0.05 * len(meadow))

    def test_a_renamed_panel_names_some_channels_differently(self):
        tv = {r.channel.stream_id: r.name for r in self.world.panel_lineups['harbor-tv'].rows}
        plus = {r.channel.stream_id: r.name for r in self.world.panel_lineups['harbor-plus'].rows}
        renamed = sum(1 for sid, name in plus.items() if tv[sid] != name)
        self.assertGreater(renamed, 0.1 * len(plus))

    def test_every_triplet_url_has_its_spelling_and_the_rest_have_none(self):
        panel = self.world.panels['harbor-plus']
        lg = self.world.logins_on('harbor-plus')[0]
        shapes = set()
        for row in self.world.panel_lineups['harbor-plus'].rows:
            path = urlsplit(self.p.stream_url(panel, row, lg)).path
            if row.channel.kind in TRIPLET_KINDS:
                cred = f'{lg.username}/{lg.password}/{row.channel.stream_id}'
                expected = {'bare': f'/{cred}', 'live_ts': f'/live/{cred}.ts',
                            'live_m3u8': f'/live/{cred}.m3u8', 'bare_with_ext': f'/{cred}.ts'}
                self.assertEqual(path, expected[row.shape])
                shapes.add(row.shape)
            else:
                self.assertNotIn(lg.password, path)
        self.assertEqual(shapes, {'bare', 'live_ts'})

    def test_radio_and_third_party_streams_carry_no_id(self):
        w = build_world({'backends': [{'name': 'b', 'channels': 600, 'radio_share': 0.02,
                                       'no_id_share': 0.02}]})
        p = _provider(w)
        lg = next(iter(w.logins.values()))
        panel = w.panels['b']
        rows = [r for r in w.panel_lineups['b'].rows if r.channel.kind in (KIND_RADIO, KIND_NO_ID)]
        self.assertTrue(rows)
        for r in rows:
            url = p.stream_url(panel, r, lg)
            self.assertTrue(url.startswith(p.layout.origin('b-thirdparty')))
            self.assertNotRegex(urlsplit(url).path, r'/\d+(\.ts|\.m3u8)?$')

    def test_vod_rows_are_in_the_playlist_and_not_the_catalog(self):
        lg = self.world.logins_on('harbor-tv')[0]
        panel = self.world.panels['harbor-tv']
        playlist = self.p.playlist(panel, lg).decode()
        self.assertGreaterEqual(playlist.count('/movie/') + playlist.count('/series/'), 2)
        catalog = self.p.live_streams(panel, lg)
        self.assertEqual(len(catalog), len(self.world.panel_lineups['harbor-tv'].rows))

    def test_a_numbered_guide_id_is_the_guides_own(self):
        """A third-party site spells ids its own way, so the provider's ids cannot simply be
        looked up in it - the case name matching exists for (dev/changelog/1105)."""
        site = {g.station.key: g.guide_id for g in self.world.site_lineups['listings'].guide}
        plus = {g.station.key: g.guide_id for g in self.world.panel_lineups['harbor-plus'].guide}
        shared = set(site) & set(plus)
        self.assertTrue(shared)
        self.assertTrue(all(site[k] != plus[k] for k in shared))


class GuideTests(unittest.TestCase):
    def setUp(self):
        self.world = load_world(SMALL)
        self.shape = _provider(self.world).shape
        self.stations = [g.station for g in self.world.panel_lineups['harbor-tv'].guide][:40]

    def _schedule(self, st, start, end):
        return [(p.start, p.stop, p.title, p.desc)
                for p in programs(self.world.seed, st, start, end, self.shape)]

    def test_a_later_window_repeats_every_program_the_two_share(self):
        """A re-sync a day later must see the programs it already saw, unchanged."""
        day1, day2 = AT + dt.timedelta(days=1), AT + dt.timedelta(days=2)
        for st in self.stations:
            a = self._schedule(st, AT, day2)
            b = self._schedule(st, day1, AT + dt.timedelta(days=3))
            self.assertEqual([p for p in a if p[1] > day1], [p for p in b if p[0] < day2])

    def test_programs_run_back_to_back_and_some_cross_midnight(self):
        crossing = 0
        for st in self.stations:
            sched = self._schedule(st, AT, AT + dt.timedelta(days=3))
            for (s1, e1, *_), (s2, *_rest) in zip(sched, sched[1:]):
                self.assertLess(s1, s2)
                self.assertLessEqual(s2 - e1, dt.timedelta(minutes=30))
            crossing += sum(1 for s, e, *_ in sched if s.date() != e.date()
                            and e.time() != dt.time(0))
        self.assertGreater(crossing, 0)


class BudgetTests(unittest.TestCase):
    """A large world builds in linear time. The measured cost (dev/changelog/1217): 300,000
    channels build in about 10 s and 300 MB on the reference box, so 30,000 take about 1 s.
    The ceiling here is ten times that, loose enough for a busy box and still far below what a
    quadratic step anywhere in the build would cost."""

    def test_thirty_thousand_channels_build_inside_the_ceiling(self):
        started = time.monotonic()
        w = build_world({'backends': [{'name': 'big', 'channels': 30_000}],
                         'panels': [{'name': 'big-tv', 'backend': 'big'},
                                    {'name': 'big-plus', 'backend': 'big', 'share': 0.9,
                                     'rename': 0.2}]})
        elapsed = time.monotonic() - started
        self.assertEqual(len(w.lineups['big'].channels), 30_000)
        self.assertLess(elapsed, 15.0)


if __name__ == '__main__':
    unittest.main()
