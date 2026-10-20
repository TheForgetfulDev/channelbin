"""The user blocks an account mid-recording (a TV wants to watch live on it): the watchdog
moves the group recording to the member on the other account at a segment boundary it makes
itself, and the member it leaves is neither scored nor demoted (dev/changelog/1151, 1223).

The block lands once the first segment is capturing; the watchdog checks for blocks every
15 s, so the move follows within that.

A characterization of today's pipeline, not a guard for one past defect.
"""
import datetime as dt
import json
import unittest

from app import db
from app.account_blocks import add_account_block
from app.database import (Channel, ChannelEvent, GROUP_FAILOVER, REC_STATUS_COMPLETED,
                          RESTART_SUCCEEDED)
from sim import answer_key as ak

from .support import BURST, CaptureCase


class AccountBlockScenario(CaptureCase):
    def test_a_block_moves_the_recording_to_the_other_account_and_scores_nothing(self):
        first, second = self.channel('cove-tv'), self.channel('reed-tv')
        first_label, second_label = self.label(first.id), self.label(second.id)
        group = self.group([first, second])
        rid = self.record(group=group, seconds=50)
        self.wait_capturing(rid, 30)
        score = self.fresh(Channel, first.id).health_score
        observations = ChannelEvent.query.filter_by(channel_id=first.id).count()
        add_account_block(self.accounts['cove-tv'],
                          dt.datetime.utcnow() + dt.timedelta(minutes=10))
        rec = self.finish(rid, 150)

        self.assertEqual(rec.status, REC_STATUS_COMPLETED, self.event_types(rid))
        move = self.assertEvent(rid, GROUP_FAILOVER)
        self.assertTrue(json.loads(move.extra_data)['block_move'])
        self.assertNotIn(RESTART_SUCCEEDED, self.event_types(rid), 'nothing stalled')
        self.assertEqual(rec.channel_id, second.id)
        db.session.remove()
        self.assertEqual(db.session.get(Channel, first.id).health_score, score)
        self.assertEqual(ChannelEvent.query.filter_by(channel_id=first.id).count(),
                         observations, 'a block move writes no observation on the member left')

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
