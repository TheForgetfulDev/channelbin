"""The rig for a picture-in-picture play through the engine (app/pip_engine.py): a real app,
a real relay and real ffmpeg over the synthesized clips of tests/support/feed_plays.py, with
the two things the suite cannot have stood in for (dev/docs/DESIGN-pip-channels.md 14):

* the GPU. The combiner is a small software `overlay` encoded with libx264, and the small
  picture's decoder is the app's own command on its CPU path. Both take what the app's
  builders take, so the engine drives them exactly as it drives the real ones. The real
  commands' argv is pinned by tests/test_pip_cmds.py and run in the GPU lab.
* the GPU checks: `gpu_state()` answers yes, or what `self.gpu` is set to.

`self.built` lists every command asked for, as ('combiner' | 'decoder', the keyword
arguments), and `self.failing` names the ones to replace with a command that fails: 'gpu'
dies blaming the GPU while `gpu_decode` is True, 'combiner' dies at once every time.
"""
import os
import sys
import threading
from unittest import mock

import app.connection_limits as connlim
import app.output_feed as feed
import app.pip_channels as pip
import app.pip_engine as engine
from app.config import load_config
from app.database import PIP_SOURCE_CHANNEL
from app.output_feed import feed_id_for_pip
from app.proc_utils import build_pip_small_decoder_cmd
from tests.support.feed_plays import ClipPlayCase, wait_for

GPU_ERROR = '[h264 @ 0x55d0] [error] Failed setup for format vaapi: hwaccel initialisation returned error.'
PLAIN_ERROR = '[in#0 @ 0x55d0] [error] Error opening input: Invalid data found when processing input'


def _dies(line: str) -> list:
    return [sys.executable, '-c',
            f'import sys, time; time.sleep(0.2); sys.stderr.write({line!r} + "\\n"); sys.exit(1)']


class StandIn:
    """The stand-in combiner, decoder and GPU answer, installed on a test case for its length.
    `scale` is the combiner's output size: the clip tests use a small one, the capture
    scenarios one big enough to tell the two pictures apart in."""

    def __init__(self, case, ready_seconds: float = 0.3):
        self.built = []
        self.failing = set()
        self.gpu = ('/dev/dri/stand-in', '')
        for name, value in (('build_combiner_cmd', self._combiner), ('build_decoder_cmd', self._decoder),
                            ('gpu_state', lambda cfg: self.gpu),
                            # The clips are a few KB a second: a source is ready at once.
                            ('READY_SECONDS', ready_seconds)):
            patcher = mock.patch.object(engine, name, value)
            patcher.start()
            case.addCleanup(patcher.stop)

    def _combiner(self, cfg, device, geometry, *, small_input='pipe:3', gpu_decode=True,
                  deinterlace=False, matrix='bt709', big_input=''):
        self.built.append(('combiner', {'gpu_decode': gpu_decode, 'big_input': big_input,
                                        'matrix': matrix, 'geometry': geometry}))
        if 'combiner' in self.failing:
            return _dies(PLAIN_ERROR)
        if 'gpu' in self.failing and gpu_decode and not big_input:
            return _dies(GPU_ERROR)
        g = geometry
        cmd = ['ffmpeg', '-hide_banner', '-nostdin', '-loglevel', 'error']
        cmd += (['-readrate', '1', '-stream_loop', '-1', '-i', f'file:{big_input}'] if big_input
                else ['-f', 'mpegts', '-i', 'pipe:0'])
        cmd += ['-f', 'rawvideo', '-pix_fmt', 'nv12', '-video_size', f'{g.small_w}x{g.small_h}',
                '-framerate', str(g.small_fps), '-i', small_input]
        cmd += ['-filter_complex', '[0:v:0]scale=320:180[big];[1:v:0]scale=96:54[small];'
                                   '[big][small]overlay=x=8:y=8[v]',
                '-map', '[v]', '-map', '0:a:0?', '-c:a', 'copy', '-c:v', 'libx264', '-preset', 'ultrafast',
                '-tune', 'zerolatency', '-g', '30', '-pix_fmt', 'yuv420p', '-f', 'mpegts', 'pipe:1']
        return cmd

    def _decoder(self, cfg, device, geometry, *, gpu_decode=True, deinterlace=False, matrix='bt709'):
        self.built.append(('decoder', {'gpu_decode': gpu_decode, 'matrix': matrix}))
        if 'gpu' in self.failing and gpu_decode:
            return _dies(GPU_ERROR)
        return build_pip_small_decoder_cmd(cfg, device, geometry, gpu_decode=False,
                                           deinterlace=deinterlace, matrix=matrix)

    def asked(self, what: str) -> list:
        return [kw for kind, kw in self.built if kind == what]


class PipEngineCase(ClipPlayCase):
    def setUp(self):
        super().setUp()
        self.stand_in = StandIn(self)
        self.built, self.failing = self.stand_in.built, self.stand_in.failing

    @property
    def gpu(self):
        return self.stand_in.gpu

    @gpu.setter
    def gpu(self, value):
        self.stand_in.gpu = value

    def asked(self, what: str) -> list:
        return self.stand_in.asked(what)


def src(kind, ref=None, query=''):
    return {'kind': kind, 'ref': ref, 'query': query}


def engine_threads() -> list:
    return [t.name for t in threading.enumerate() if t.name.startswith('pip-') and t.is_alive()]


class PipPlayCase(PipEngineCase):
    """A picture-in-picture channel with a big and a small picture, each a plain channel on an
    account of its own with one connection, and what the tests of a play of it ask."""

    def setUp(self):
        super().setUp()
        self.big = self.plain_channel('Big one', self.account)
        self.small = self.plain_channel('Small one')
        self.row = self.make()
        self.feed_id = feed_id_for_pip(self.row.id)

    def make(self, **over):
        data = {'name': 'PiP 1', 'main': src(PIP_SOURCE_CHANNEL, self.big.id),
                'inset': src(PIP_SOURCE_CHANNEL, self.small.id), 'layout': 'corner', 'corner': 'br',
                'size': 's', 'swapped_copy': True}
        data.update(over)
        spec = pip.clean(data)
        pip.check_sources(spec, load_config())
        return pip.create(spec)

    def seats(self, account):
        return connlim._holders.get(account.id, [])

    def record_on(self, account, recording_id=77):
        """A recording takes `account`'s one connection, as the recorder's acquire does: the
        viewers' seats stripped, its own taken, the plays on it told."""
        from app import recorder
        self.assertTrue(recorder._try_acquire_slot_with_preemption(self.t.app, recording_id, account.id))
        self.addCleanup(connlim.release, account.id, 'recording', recording_id)

    def pictures(self, row=None):
        pair = engine._pairs[(row or self.row).id]
        return pair.pictures[pip.ROLE_MAIN], pair.pictures[pip.ROLE_INSET]

    def combined(self, feed_id=None):
        """Tune in and wait for the combined picture to reach the TV. (response, session)."""
        before = {s.id for s in feed.live_sessions()}
        resp = self.play(feed_id)
        s = next(x for x in feed.live_sessions() if x.id not in before)
        self.assertIsNotNone(s.combined, 'the play is on its two pictures combined')
        self.assertTrue(wait_for(lambda: s.state == feed.STATE_STREAMING, 20), s)
        return resp, s

    def assert_nothing_left(self, *sessions):
        self.assertTrue(wait_for(lambda: not feed._internal and not engine._pairs, 15),
                        (feed._internal, engine._pairs))
        self.assertTrue(wait_for(lambda: not engine_threads(), 10), engine_threads())
        for account in (self.account, self.small.account):
            self.assertEqual(self.seats(account), [])
        for s in sessions:
            self.assertFalse(s.live)
            # One that ended between two ffmpegs (a failover or a rebuild under way, which a
            # loaded box makes likelier: the rig's clips are 20 s long) holds none at all.
            if s.proc is not None:
                self.assertIsNotNone(s.proc.poll(), 'its ffmpeg is dead')
            self.assertFalse(os.path.exists(s.stderr_path), 'its stderr spool is removed')
