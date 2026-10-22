"""The user switches a running group recording to another channel by hand, from the
recording's page: the watchdog moves it at a boundary it makes itself, the member left is
neither scored nor demoted, and the file holds both channels in order with little lost at
the hand-off (dev/changelog/1254).

Two simulated accounts on two backends, one seat each, so the switch crosses accounts and
the seat moves with it. The switch is posted through the route the page's dialog posts.

A characterization of today's pipeline, not a guard for one past defect.
"""
import json
import unittest

from app import db
from app.database import (Channel, ChannelEvent, GROUP_FAILOVER, REC_STATUS_COMPLETED,
                          RESTART_SUCCEEDED)
from sim import answer_key as ak
from tests.support.csrf import csrf_token

from .support import BURST, CaptureCase


class ChannelSwitchScenario(CaptureCase):
    def test_a_switch_by_hand_moves_the_recording_and_scores_nothing(self):
        first, second = self.channel('cove-tv'), self.channel('reed-tv')
        first_label, second_label = self.label(first.id), self.label(second.id)
        group = self.group([first, second])
        rid = self.record(group=group, seconds=50)
        self.wait_capturing(rid, 30)
        score = self.fresh(Channel, first.id).health_score
        observations = ChannelEvent.query.filter_by(channel_id=first.id).count()

        first_name = self.fresh(Channel, first.id).name
        second_name = self.fresh(Channel, second.id).name
        token = csrf_token(self.t.client)
        resp = self.t.client.post(f'/api/recordings/{rid}/switch-channel',
                                  json={'channel_id': second.id, 'leave': 'keep'},
                                  headers={'X-CSRFToken': token} if token else {})
        self.assertEqual(resp.status_code, 200, resp.get_json())
        body = resp.get_json()
        self.assertTrue(body['moved'], body)
        self.assertEqual((body['from'], body['to']), (first_name, second_name))
        rec = self.finish(rid, 150)

        self.assertEqual(rec.status, REC_STATUS_COMPLETED, self.event_types(rid))
        move = self.assertEvent(rid, GROUP_FAILOVER)
        self.assertTrue(json.loads(move.extra_data)['manual'])
        self.assertIn('you asked to move', move.detail)
        self.assertNotIn(RESTART_SUCCEEDED, self.event_types(rid), 'nothing stalled')
        self.assertEqual(rec.channel_id, second.id)
        db.session.remove()
        self.assertEqual(db.session.get(Channel, first.id).health_score, score)
        self.assertEqual(ChannelEvent.query.filter_by(channel_id=first.id).count(),
                         observations, 'a switch by hand writes no observation on the member left')
        exits = [s.exit_reason for s in self.segments(rid)]
        self.assertIn('CHANNEL_SWITCHED', exits, exits)

        answer = self.answer(rid)
        self.assertNoProblems(answer)
        self.assertAgrees(answer, self.claims(rid))
        self.assertEqual(answer.channels, (first_label, second_label))
        # A move at a boundary ChannelBin chose costs little content: the new member's
        # connect burst covers the relaunch.
        self.assertLess(sum(x.seconds for x in self.lost(answer, ak.REASON_NOT_SENT)), BURST,
                        answer.describe())


if __name__ == '__main__':
    unittest.main()
