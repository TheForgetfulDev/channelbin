"""The rig for a play of ChannelBin's own clip through the relay: a sports channel's waiting
screen (tests/test_sports_waiting.py) and a picture-in-picture channel's card
(tests/test_pip_play.py). A real app, a real relay and real ffmpeg over two small synthesized
TS files; the clips are not drawn: sports_screen.render_clip is replaced by a copy of one of
them, so what is tested is the relay's handling of a clip, never the picture."""
import os
import shutil
import subprocess
import time
import unittest
from datetime import datetime
from unittest import mock

import app.output_feed as feed
import app.sports_screen as ss
from app import db
from tests.support import clips
from tests.support.app import make_test_app
from tests.support.output_feed import add_login
from tests.support.seed import make_account, make_channel

HAVE_FFMPEG = shutil.which('ffmpeg') is not None
USER, PASS = 'tv', 'tv-secret'


def feed_cfg(**overrides):
    block = {'enabled': True, 'reserve_seconds': 0}
    block.update(overrides)
    return {'integrations': {'output_feed': block}}


def wait_for(predicate, timeout=15.0, interval=0.05):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return predicate()


def _build(d):
    for name, size in (('screen.ts', '192x108'), ('game.ts', '256x144')):
        subprocess.run(
            ['ffmpeg', '-v', 'error', '-y', '-f', 'lavfi', '-i', f'testsrc=size={size}:rate=30',
             '-f', 'lavfi', '-i', 'sine=frequency=440:sample_rate=48000', '-t', '20',
             '-c:v', 'libx264', '-preset', 'ultrafast', '-g', '30', '-bf', '0', '-pix_fmt', 'yuv420p',
             '-c:a', 'aac', '-f', 'mpegts', os.path.join(d, name)],
            check=True, timeout=180)


@unittest.skipUnless(HAVE_FFMPEG, 'ffmpeg not available')
class ClipPlayCase(unittest.TestCase):
    FEED = feed_cfg()

    @classmethod
    def setUpClass(cls):
        cls.media = clips.shared('sports-waiting', _build)

    def setUp(self):
        self.t = make_test_app(csrf=False)
        self.client = self.t.client
        self.addCleanup(self.t.cleanup)
        self.addCleanup(feed.stop_all)
        self.t.sandbox_config(self.FEED)
        self.login = add_login(USER, PASS)
        self.account = make_account('Acct', max_connections=1)
        db.session.commit()
        self.renders = []
        self.render_delay = 0.0
        patcher = mock.patch.object(ss, 'render_clip', self._fake_render)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.open = []
        self.addCleanup(self._close_all)
        self.now = datetime.utcnow()

    def _fake_render(self, cfg, screen, path, *, still=False, background=False):
        self.renders.append((path, still, screen))
        if self.render_delay and not still:
            time.sleep(self.render_delay)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        shutil.copyfile(os.path.join(self.media, 'screen.ts'), path)

    def _close_all(self):
        for resp in self.open:
            try:
                resp.close()
            except Exception:  # noqa: BLE001 - a response the test already closed
                pass

    def play(self, feed_id=None, expect=200):
        resp = self.client.get(f'/live/{USER}/{PASS}/{feed_id or self.feed_id}.ts')
        self.assertEqual(resp.status_code, expect, resp.get_data(as_text=True)[:300] if expect != 200 else '')
        if expect == 200:
            self.open.append(resp)
        return resp

    @staticmethod
    def read(resp, n=1):
        it = iter(resp.response)
        return [next(it) for _ in range(n)]

    def only_session(self):
        live = feed.live_sessions()
        self.assertEqual(len(live), 1, live)
        return live[0]

    def plain_channel(self, name='Plain', account=None):
        ch = make_channel(account or make_account(f'{name} acct', max_connections=1), name=name, pace_realtime=True)
        ch.stream_url = os.path.join(self.media, 'game.ts')
        ch.in_guide = True
        db.session.commit()
        return ch
