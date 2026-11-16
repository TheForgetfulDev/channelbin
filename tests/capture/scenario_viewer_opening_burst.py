"""An IPTV player tunes in through the output feed and the provider opens the connection the
way a real one does: 25 seconds of the channel at once, then real time (dev/changelog/942,
997; dev/docs/BUGS.md 2026-11-12).

The relay holds what arrives for `reserve_seconds` before the player gets it, so the whole
burst has to wait in the ring. In a ring it does not fit, the head of the stream was evicted
before any of it could be released and the play ended at nothing sent, every time. Two plays
here, on a backend whose burst is the real length rather than the tier's scaled-down one:

- with the default settings the ring holds the burst and the reserve, and the player gets
  the stream from its first byte with nothing lost and nothing sent early;
- with a ring smaller than the burst the play still reaches the player - the ring gives the
  stream out ahead of the reserve instead of losing it - and its line in Recent plays says so.
"""
import copy
import time

import app.output_feed as feed
from app import db
from app.database import Channel, FEED_PLAY_ENDED, OutputFeedPlay
from sim.content import PROFILES

from .support import CAPTURE_WORLD, CHANNEL_LOOP, CaptureCase

USER, PASS = 'tv', 'sofa'
BURST_SECONDS = 25.0
SMALL_RING_MB = 4

WORLD = copy.deepcopy(CAPTURE_WORLD)
for _backend in WORLD['backends']:
    _backend['burst_seconds'] = BURST_SECONDS


class ViewerOpeningBurstScenario(CaptureCase):
    world_source = WORLD

    def _feed(self, **over):
        block = {'enabled': True, 'username': USER, 'password': PASS, 'max_viewers': 1}
        block.update(over)
        self.configure({'integrations': {'output_feed': block}})

    def _tune_in(self):
        ch = self.channel()
        row = db.session.get(Channel, ch.id)
        row.in_guide = True
        db.session.commit()
        resp = self.t.client.get(f'/live/{USER}/{PASS}/{feed.feed_id_for_channel(ch.id)}.ts')
        self.addCleanup(resp.close)
        self.addCleanup(feed.stop_all)
        self.assertEqual(resp.status_code, 200, resp.get_data(as_text=True)[:300]
                         if resp.status_code != 200 else '')
        # The test client reads the body's first chunk before it returns, so a play that was
        # cut with nothing sent is already over here.
        live = feed.live_sessions()
        self.assertEqual(len(live), 1, 'the play ended before its first byte reached the player: '
                         f'{[s.to_dict() for s in feed.recent_ended()]}')
        return resp, live[0]

    def _read(self, resp, want: int, seconds: float) -> bytes:
        """What the player is sent, until it has `want` bytes, the body ends or `seconds` pass."""
        got = bytearray()
        end = time.monotonic() + seconds
        for chunk in resp.response:
            got += chunk
            if len(got) >= want or time.monotonic() > end:
                break
        return bytes(got)

    def _burst_bytes(self) -> int:
        """A floor on what the opening burst weighs: the loop's video at its target rate."""
        return int(PROFILES[CHANNEL_LOOP].video_kbps * 1000 / 8 * BURST_SECONDS)

    def test_the_default_ring_holds_the_burst_and_the_player_gets_all_of_it(self):
        self._feed()
        burst = self._burst_bytes()
        resp, session = self._tune_in()
        self.assertGreater(session.ring.capacity, burst * 2)
        got = self._read(resp, burst, 40)
        self.assertGreaterEqual(len(got), burst, f'{session.to_dict()}')
        self.assertEqual(got[0], 0x47, 'the stream starts on a TS packet')
        self.assertTrue(session.live, session.to_dict())
        ring = session.ring
        self.assertEqual((ring.dropped, ring.released_early), (0, 0))

    def test_a_ring_smaller_than_the_burst_still_plays_and_says_it_ran_short(self):
        self._feed(buffer_mb=SMALL_RING_MB, reserve_seconds=3)
        burst = self._burst_bytes()
        self.assertGreater(burst, SMALL_RING_MB * 1024 * 1024,
                           'the burst has to be bigger than the ring for this to be the case')
        resp, session = self._tune_in()
        got = self._read(resp, burst, 40)
        self.assertGreaterEqual(len(got), burst, f'{session.to_dict()}')
        self.assertTrue(session.live, session.to_dict())
        ring = session.ring
        self.assertEqual(ring.dropped, 0, 'nothing of the stream was lost')
        self.assertGreater(ring.released_early, 0)
        resp.close()
        self.wait_for(lambda: not session.live, 10, 'the play to end')
        self.assertEqual(session.reason, feed.REASON_CLIENT_GONE)
        feed.flush_play_log()
        line = OutputFeedPlay.query.filter_by(kind=FEED_PLAY_ENDED).one()
        self.assertIn('went out sooner than 3s behind live', line.message)
