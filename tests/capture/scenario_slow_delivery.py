"""A feed slows to a trickle and stays connected, sending too little to stall on, and both
readers of it move on by the same check (dev/changelog/1799, dev/docs/BUGS.md 2026-11-28 @
12:21:46 PM):

- an IPTV player watching a group through the output feed: the member it plays trickles at 5%
  of real time, which still reaches the relay's pipe every few seconds, and the play moves to
  the other member, saying why;
- a recording of a channel trickling at 30%, which still lands a 256 KiB block in the file
  every few seconds: the capture is closed as a slow-delivery stall and restarted, and the
  recording completes.

Without the slow-delivery check the play stays on the trickling member for as long as the
provider keeps sending, and the recording rides the trickle with nothing said. The window is
scaled down with the tier's other timings (60 s by default, 20 s here; one in-process reconnect
at this tier's 2 s read timeout dips nowhere near 0.4x across it).
"""
import threading
import unittest

import app.output_feed as feed
from app import db
from app.database import (ChannelGroup, REC_STATUS_COMPLETED, RESTART_SUCCEEDED,
                          STALL_DETECTED, RecordingSegment, SEGMENT_EXIT_SLOW_DELIVERY)

from ..support.output_feed import ensure_login
from .support import CaptureCase, at

USER, PASS = 'tv', 'sofa'


class _Tv(threading.Thread):
    """An IPTV player reading what it is sent, so the play is never cut as idle."""

    def __init__(self, resp):
        super().__init__(name='capture-slow-tv', daemon=True)
        self.resp = resp
        self.start()

    def run(self):
        try:
            for _chunk in self.resp.response:
                pass
        except Exception:  # noqa: BLE001 - the response was closed under the reader
            pass


class SlowDeliveryScenario(CaptureCase):
    config = {'watchdog': {'slow_delivery_window_seconds': 20}}

    def test_a_play_on_a_trickling_member_moves_to_the_other_one(self):
        self.configure({'integrations': {'output_feed': {
            'enabled': True, 'max_viewers': 1, 'reserve_seconds': 0}}})
        ensure_login(USER, PASS)
        first, second = self.channel('cove-tv'), self.channel('reed-tv')
        group = self.group([first, second])
        row = db.session.get(ChannelGroup, group.id)
        row.in_guide = True
        db.session.commit()

        resp = self.t.client.get(f'/live/{USER}/{PASS}/{feed.feed_id_for_group(group.id)}.ts')
        self.assertEqual(resp.status_code, 200, resp.get_data(as_text=True)[:300]
                         if resp.status_code != 200 else '')
        tv = _Tv(resp)
        self.addCleanup(self._leave, tv)
        play = feed.live_sessions()[0]
        self.wait_for(lambda: play.state == feed.STATE_STREAMING, 40, 'the play to reach the TV')
        playing = play.channel_id
        other = second.id if playing == first.id else first.id
        # 5%: the SD loop's 231 kB/s becomes about 11.5 kB/s, which fills the relay's 32 KiB
        # pipe writes every few seconds - well inside the 8 s silent-stall timeout, so only
        # the slow-delivery check can see it.
        self.sim.inject('throttle', self.label(playing), rate=0.05, start=at(0), end=at(900))
        self.wait_for(lambda: play.failovers, 90, lambda: f'the play to move: {play.to_dict()}')

        self.assertEqual(play.failovers[0][2], feed.REASON_TOO_SLOW, play.failovers)
        self.assertTrue(play.live, play.to_dict())
        self.assertEqual(play.channel_id, other)
        self.assertIn('slowed to a trickle', feed.move_text(*play.failovers[0]))

    @staticmethod
    def _leave(tv):
        feed.stop_all()
        tv.join(timeout=10)
        tv.resp.close()

    def test_a_recording_of_a_trickling_channel_restarts_and_completes(self):
        ch = self.channel()
        rid = self.record(ch, seconds=50)
        self.wait_capturing(rid, 30, min_bytes=300_000)
        # 30%: about 69 kB/s, a 256 KiB block every 4 s - the file never stands still for the
        # 8 s growth check, and the content arrives at under the 0.4 line.
        trickle = self.sim.inject('throttle', self.label(ch.id), rate=0.3, start=at(0), end=at(900))
        self.wait_event(rid, STALL_DETECTED, 90)
        self.sim.clear(trickle)
        rec = self.finish(rid, 150)

        self.assertEqual(rec.status, REC_STATUS_COMPLETED, self.event_types(rid))
        stall = self.assertEvent(rid, STALL_DETECTED)
        self.assertIn('seconds of video per second', stall.detail)
        self.assertIn('"stall_reason": "slow_delivery"', stall.extra_data)
        self.assertEvent(rid, RESTART_SUCCEEDED)
        db.session.remove()
        first = RecordingSegment.query.filter_by(recording_id=rid).order_by(
            RecordingSegment.segment_number).first()
        self.assertEqual(first.exit_reason, SEGMENT_EXIT_SLOW_DELIVERY)


if __name__ == '__main__':
    unittest.main()
