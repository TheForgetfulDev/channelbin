"""A TV watches a picture-in-picture channel on the output feed while its two pictures meet
what a provider and a recording do to them (dev/docs/DESIGN-pip-channels.md 8, 9 and 14):

- the small picture's feed stops arriving on a connection that stays open, and later
  recovers: the big picture never stops, the small window says why, and the small picture
  comes back by itself;
- the small picture's feed is cut for good: the big picture plays on and nothing is left
  holding the small picture's connection;
- a recording takes the small picture's connection: the big picture plays on, the recording
  completes, and the small picture comes back when it has;
- a recording takes the big picture's connection: the big picture takes its small picture's
  and keeps playing, and both are back once the recording is done;
- the two pictures are swapped while watched: neither reconnects.

Real relay sessions against simulated streams, with the real recorder for the recordings. The
combiner is the suite's CPU stand-in (tests/support/pip_engine.py): this box has no GPU, and
the real commands are run in the GPU lab (dev/tools/gpu-lab/jobs/pip-*.sh).

A characterization of the feature end to end, not a guard for one past defect.
"""
import threading
import time
import unittest

import app.connection_limits as connlim
import app.output_feed as feed
import app.pip_channels as pip
import app.pip_engine as engine
from app import db
from app.database import (FEED_PLAY_PICTURE, PIP_SOURCE_CHANNEL, PIP_SOURCE_GROUP, Alert, Channel,
                          ChannelGroup, OutputFeedPlay, REC_STATUS_COMPLETED)

from ..support.pip_engine import StandIn
from .support import CaptureCase, at

USER, PASS = 'tv', 'sofa'


class Tv(threading.Thread):
    """An IPTV player: reads what it is sent for as long as it is sent, and remembers when."""

    def __init__(self, resp):
        super().__init__(name='capture-pip-tv', daemon=True)
        self.resp = resp
        self.got = 0
        self.stamps = [time.monotonic()]
        self.start()

    def run(self):
        try:
            for chunk in self.resp.response:
                self.got += len(chunk)
                self.stamps.append(time.monotonic())
        except Exception:  # noqa: BLE001 - the response was closed under the reader
            pass

    def longest_gap(self, since: float) -> float:
        stamps = [t for t in self.stamps if t >= since] + [time.monotonic()]
        return max((b - a for a, b in zip(stamps, stamps[1:])), default=0.0)


class PipPicturesScenario(CaptureCase):
    def setUp(self):
        super().setUp()
        self.stand_in = StandIn(self, ready_seconds=engine.READY_SECONDS)
        self.configure({'integrations': {'output_feed': {
            'enabled': True, 'username': USER, 'password': PASS, 'max_viewers': 2, 'reserve_seconds': 0}}})
        self.addCleanup(feed.stop_all)

    def guide(self, ch):
        row = db.session.get(Channel, ch.id)
        row.in_guide = True
        db.session.commit()
        return ch

    def make(self, main, inset):
        row = pip.create(pip.clean({'name': 'Two games', 'main': main, 'inset': inset, 'layout': 'corner',
                                    'corner': 'br', 'size': 's', 'swapped_copy': True}))
        return row.id

    def tune_in(self, row_id):
        """(the TV, its play, the big picture, the small picture), with both pictures on."""
        resp = self.t.client.get(f'/live/{USER}/{PASS}/{feed.feed_id_for_pip(row_id)}.ts')
        self.assertEqual(resp.status_code, 200, resp.get_data(as_text=True)[:300]
                         if resp.status_code != 200 else '')
        tv = Tv(resp)
        self.addCleanup(self._leave, tv)
        play = feed.live_sessions()[0]
        self.assertIsNotNone(play.combined, play.to_dict())
        pair = engine._pairs[row_id]
        big, small = pair.pictures[pip.ROLE_MAIN], pair.pictures[pip.ROLE_INSET]
        self.wait_for(lambda: play.state == feed.STATE_STREAMING and big.live and small.live
                      and play.combined.had_frame, 40, 'the combined picture to reach the TV')
        return tv, play, big, small

    @staticmethod
    def _leave(tv):
        # The play ends first, so the TV's reader finishes by itself: a response cannot be
        # closed while another thread is reading it.
        feed.stop_all()
        tv.join(timeout=10)
        tv.resp.close()

    def lines(self) -> list:
        feed.flush_play_log()
        db.session.remove()
        return [p.message for p in OutputFeedPlay.query.filter_by(kind=FEED_PLAY_PICTURE)
                .order_by(OutputFeedPlay.id).all()]

    def lines_once_back(self) -> list:
        """The lines, once the last one says the small picture is back. The reaper writes that
        line on its next turn after the picture is live again, so a read made the moment
        `small.live` turns true can come before it (dev/docs/BUGS.md 2026-11-25)."""
        self.wait_for(lambda: (said := self.lines()) and 'is back' in said[-1], 10,
                      lambda: f'the "is back" line, have {self.lines()}')
        return self.lines()

    def simple(self):
        big, small = self.guide(self.channel('cove-tv', 0)), self.guide(self.channel('reed-tv', 0))
        row_id = self.make({'kind': PIP_SOURCE_CHANNEL, 'ref': big.id}, {'kind': PIP_SOURCE_CHANNEL, 'ref': small.id})
        return big, small, row_id

    def assertBigNeverStopped(self, tv, play, since, combiner=None):
        self.assertTrue(play.live, play.to_dict())
        self.assertLess(tv.longest_gap(since), 5.0, 'the TV kept getting a picture')
        if combiner is not None:
            self.assertIs(play.proc, combiner, 'nothing about the small picture restarts the combiner')
            self.assertIsNone(combiner.poll())

    def test_a_small_picture_that_stalls_comes_back_and_the_big_one_never_stops(self):
        _big_ch, small_ch, row_id = self.simple()
        tv, play, big, small = self.tune_in(row_id)
        combiner, big_session, since = play.proc, big.session, time.monotonic()
        trickle = self.sim.inject('throttle', self.label(small_ch.id), rate=0.0, start=at(0), end=at(900))
        self.wait_for(lambda: not small.live, 90, 'the small picture to be called stalled')
        self.assertIn(small.pane.state, (pip.PANE_RECONNECTING, pip.PANE_CANNOT), small.pane)
        self.wait_for(lambda: play.combined.cards > 0, 15, 'the small window to show its card')
        self.assertBigNeverStopped(tv, play, since, combiner)
        self.assertIs(big.session, big_session)
        self.sim.clear(trickle)
        self.wait_for(lambda: small.live, 60, 'the small picture to come back by itself')
        at_back = play.combined.frames.at
        self.wait_for(lambda: play.combined.frames.at > at_back, 30, 'its frames to be shown again')
        self.assertBigNeverStopped(tv, play, since, combiner)
        said = self.lines_once_back()
        self.assertEqual(len(said), 2, said)
        self.assertIn('stopped', said[0])
        self.assertIn('is back', said[1])

    def test_a_small_picture_cut_for_good_leaves_the_big_one_playing(self):
        _big_ch, small_ch, row_id = self.simple()
        tv, play, big, small = self.tune_in(row_id)
        combiner, since = play.proc, time.monotonic()
        label = self.label(small_ch.id)
        # The connection it has dries up, and every new one is refused.
        self.sim.inject('throttle', label, rate=0.0, start=at(0), end=at(900))
        self.sim.inject('refuse_at_open', label, start=at(0), end=at(900))
        self.wait_for(lambda: not small.live, 90, 'the small picture to be called gone')
        # Past one periodic re-ask: it is asked again, refused again, and still says so.
        time.sleep(feed.SCREEN_RECHECK_SECONDS + 5)
        self.assertFalse(small.live)
        self.assertIn(small.pane.state, (pip.PANE_RECONNECTING, pip.PANE_CANNOT), small.pane)
        self.assertEqual(connlim.seated_holder_ids('viewer'), {big.session.id},
                         'nothing holds the small picture\'s connection')
        self.assertBigNeverStopped(tv, play, since, combiner)
        self.assertGreater(play.combined.cards, 0)

    def test_a_recording_takes_the_small_pictures_connection_and_it_comes_back_after(self):
        _big_ch, _small_ch, row_id = self.simple()
        tv, play, big, small = self.tune_in(row_id)
        combiner, big_session, since = play.proc, big.session, time.monotonic()
        rid = self.record(self.channel('reed-tv', 1), seconds=25)
        self.wait_for(lambda: small.pane.state == pip.PANE_TAKEN, 30, 'the small picture to be paused')
        self.assertEqual(small.words[0], 'Paused for a recording')
        rec = self.finish(rid, 150)
        self.assertEqual(rec.status, REC_STATUS_COMPLETED, self.event_types(rid))
        self.wait_for(lambda: small.live, 60, 'the small picture to come back by itself')
        self.assertIs(big.session, big_session, 'the big picture kept its connection throughout')
        self.assertBigNeverStopped(tv, play, since, combiner)
        db.session.remove()
        self.assertEqual(Alert.query.filter_by(alert_type='VIEWER_PREEMPTED').count(), 0,
                         'the TV kept a picture, so nothing is raised')
        said = self.lines_once_back()
        self.assertIn('paused for a recording', said[0])
        self.assertIn('is back', said[-1])

    def test_a_recording_takes_the_big_pictures_connection_and_the_big_picture_takes_its_small_ones(self):
        first = self.guide(self.channel('cove-tv', 0))
        second = self.guide(self.channel('reed-tv', 1))
        small_ch = self.guide(self.channel('reed-tv', 0))
        group = self.group([first, second], name='The game')
        row = db.session.get(ChannelGroup, group.id)
        row.in_guide = True
        db.session.commit()
        row_id = self.make({'kind': PIP_SOURCE_GROUP, 'ref': group.id},
                           {'kind': PIP_SOURCE_CHANNEL, 'ref': small_ch.id})
        tv, play, big, small = self.tune_in(row_id)
        self.assertEqual((big.session.channel_id, small.session.channel_id), (first.id, small_ch.id))
        since = time.monotonic()
        rid = self.record(self.channel('cove-tv', 1), seconds=25)
        self.wait_for(lambda: big.live and big.session.channel_id == second.id, 40,
                      'the big picture to take its small picture\'s connection')
        self.wait_for(lambda: small.pane.state == pip.PANE_YIELDED, 15, 'the small window to say why')
        self.assertEqual(small.words[0], 'Paused: the big picture needed its connection')
        rec = self.finish(rid, 150)
        self.assertEqual(rec.status, REC_STATUS_COMPLETED, self.event_types(rid))
        # With the recording done there is a connection for each again: the big picture goes
        # back to the free account so the small one can have its own.
        self.wait_for(lambda: small.live and big.live and big.session.channel_id == first.id, 90,
                      'both pictures to be back on a connection each')
        self.assertTrue(play.live, play.to_dict())
        self.assertLess(tv.longest_gap(since), 15.0, 'the TV was never left without a picture for long')
        self.assertEqual(Alert.query.filter_by(alert_type='VIEWER_PREEMPTED').count(), 0)

    def test_a_swap_while_watched_keeps_both_connections(self):
        _big_ch, _small_ch, row_id = self.simple()
        tv, play, big, small = self.tune_in(row_id)
        sessions, first, since = (big.session, small.session), play.proc, time.monotonic()
        asked = time.monotonic()
        pip.swap(row_id)
        self.assertEqual(feed.refresh_pip(row_id), (0, 0, 1))
        self.wait_for(lambda: play.proc is not None and play.proc is not first
                      and play.state == feed.STATE_STREAMING, 30, 'the swapped picture to reach the TV', poll=0.02)
        took = time.monotonic() - asked
        self.wait_for(lambda: play.combined.had_frame, 30, 'the new small picture to be decoded', poll=0.02)
        small_took = time.monotonic() - asked
        print(f'\n[PIP-SWAP] picture out {took:.2f} s after the swap was asked for, new small picture '
              f'{small_took:.2f} s (stand-in combiner, simulated streams)', flush=True)
        self.assertEqual((big.session, small.session), sessions, 'neither picture reconnected')
        self.assertTrue(all(s.live for s in sessions))
        self.assertIs(play.combined.big, small)
        self.assertLess(took, 10.0)
        self.assertLess(tv.longest_gap(since), 10.0)
        self.assertEqual(self.lines(), [], 'a swap is not the small picture stopping')


if __name__ == '__main__':
    unittest.main()
