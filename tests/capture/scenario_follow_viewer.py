"""Someone watches a recording live while it captures, through a stall and the restart that
follows it (dev/changelog/1316).

A viewer reads the segment file the recorder is writing. The rule it lives under is that a
diagnostic must never harm the capture it is diagnosing, so the capture is judged here exactly
as scenario_stall_restart judges one nobody watched: the answer key's reading of what was
captured agrees with ChannelBin's own figures, the only content lost is what the trickle
withheld, and the join drops nothing the segments held. And the viewer's side: the follower on
the stalled segment ends by name when the recorder moves on, the next one picks up the new
segment and says why the recording moved in the recording's own words, and the last one ends
when the capture does.
"""
from app import preview
from app.database import REC_STATUS_COMPLETED, RESTART_SUCCEEDED, STALL_DETECTED
from sim import answer_key as ak

from ..support.csrf import csrf_token
from .support import BURST, CaptureCase, at


class FollowViewerScenario(CaptureCase):
    # The viewer here fetches nothing, so the idle reaper must not be what ends it.
    config = {'preview': {'idle_timeout_seconds': 600}}

    def _play(self, rid, body):
        resp = self.t.client.post(f'/api/recordings/{rid}/playback', json=body,
                                  headers={'X-CSRFToken': self.token})
        return resp.status_code, resp.get_json()

    def _follow(self, rid, body, timeout):
        """Start a live session, asking again while the capture is between segments (409),
        as the player does; then wait for it to have something to play."""
        def _started():
            code, data = self._play(rid, body)
            return data if code == 200 else None
        data = self.wait_for(_started, timeout, f'a live session on recording {rid}', poll=1.0)
        session = preview.get_session(data['session_id'])
        self.wait_for(lambda: session.state == preview.STATE_READY or not session.live, 60,
                      lambda: f'the follower to be playable ({session.to_dict()})')
        self.assertEqual(session.state, preview.STATE_READY, session.to_dict())
        return data, session

    def test_a_viewer_follows_across_a_stall_restart_and_the_capture_loses_nothing(self):
        self.addCleanup(preview.stop_all)
        self.token = csrf_token(self.t.client)
        ch = self.channel()
        rid = self.record(ch, seconds=45)
        self.wait_capturing(rid, 40, min_bytes=500_000)

        first, s1 = self._follow(rid, {'live': True}, 30)
        self.assertTrue(first['following'] and first['capturing'], first)
        stalled_segment = first['capture']['segment_number']

        trickle = self.sim.inject('throttle', self.label(ch.id), rate=0.02, start=at(1),
                                  end=at(600))
        self.wait_event(rid, STALL_DETECTED, 60)
        self.sim.clear(trickle)

        # The recorder moved on; the follower on the stalled segment ends by name.
        self.wait_for(lambda: not s1.live, 30, lambda: f'the first follower to end ({s1.to_dict()})')
        self.assertEqual(s1.reason, preview.REASON_SEGMENT_CHANGED, s1.to_dict())

        second, s2 = self._follow(rid, {'live': True, 'moved_from': stalled_segment}, 60)
        self.assertGreater(second['capture']['segment_number'], stalled_segment)
        self.assertIn('stalled', second['moved_because'])

        rec = self.finish(rid, 150)
        self.wait_for(lambda: not s2.live, 15, lambda: f'the last follower to end ({s2.to_dict()})')
        self.assertEqual(s2.reason, preview.REASON_CAPTURE_STOPPED, s2.to_dict())

        # The capture, judged as if nobody had watched it (scenario_stall_restart).
        self.assertEqual(rec.status, REC_STATUS_COMPLETED, self.event_types(rid))
        self.assertEvent(rid, RESTART_SUCCEEDED)
        answer = self.answer(rid)
        self.assertNoProblems(answer)
        self.assertAgrees(answer, self.claims(rid))
        unsent = self.lost(answer, ak.REASON_NOT_SENT)
        self.assertEqual(len(unsent), 1, answer.describe())
        self.assertEqual({x.reason for x in answer.lost},
                         {ak.REASON_NOT_SENT, ak.REASON_NOT_RECORDED}, answer.describe())
        self.assertGreaterEqual(rec.total_downtime_seconds, unsent[0].seconds - BURST,
                                answer.describe())
        joined = self.answer_joined(rid)
        self.assertNoProblems(joined)
        self.assertAlmostEqual(joined.distinct_seconds, answer.distinct_seconds, delta=0.3)
