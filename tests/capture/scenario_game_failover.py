"""Prod recording 88 (Supercars, 2026-11-27) on the simulator: a recording of a game, made from
the one event channel of its listing and in no group, finds that channel open and close at zero
bytes on every try ("Stream ends prematurely at 0"), and moves to another channel carrying the
game on a wider listing, which records the rest (dev/docs/BUGS.md 2026-11-28, dev/changelog/1793).

Before the fix the recording had nowhere to go: it waited and retried on the dead channel until
max_consecutive_failures failed it, with nothing recorded.

The game is a made-up race matched to both channels' listings the way the matcher leaves them
(tests/support/seed.make_guide_match); the event channel's listing starts near the race, the
other channel's started earlier and runs past it, as on prod. The two channels sit on the two
backends, one seat each, so the move crosses accounts.
"""
import datetime as dt
import json
import unittest

from app import db
from app.database import (GROUP_FAILOVER, REC_STATUS_COMPLETED, REC_STATUS_SCHEDULED,
                          Channel, Recording, SportsEvent, SportsFollow, SportsLeague)
from app.scheduler import schedule_recording
from sim import answer_key as ak

from ..support.seed import make_guide_match
from .support import BURST, KEYFRAME_SECONDS, CaptureCase, at

TITLE = 'PFL Racing'


class GameFailoverScenario(CaptureCase):
    def _race(self, lone, wide, race_at):
        """The race, its event channel's listing near the start and the wider one before it."""
        def _add():
            league = SportsLeague(tsdb_id=9201, name='Pretend Racing', sport='Motorsport',
                                  current_season='2099', match_terms=json.dumps(['PFL']))
            db.session.add(league)
            db.session.flush()
            db.session.add(SportsFollow(league_id=league.id, team_tsdb_id=0, rule_id=0))
            event = SportsEvent(tsdb_id=8201, league_id=league.id, name='Pretend Grand Final Race 35',
                                start_at=race_at, guide_start=race_at, local_date=race_at.date())
            db.session.add(event)
            db.session.flush()
            make_guide_match(event, db.session.get(Channel, lone.id), race_at - dt.timedelta(minutes=5),
                             hours=1.0, title=TITLE)
            make_guide_match(event, db.session.get(Channel, wide.id), race_at - dt.timedelta(minutes=65),
                             hours=3.5, title=TITLE)
            db.session.commit()
            return event.id
        return _add()

    def _record_game(self, lone, program_start, seconds):
        """A recording from the event channel's listing, as Record on the league page makes it:
        its own channel, no group, and the listing it was made from."""
        start = dt.datetime.utcnow().replace(microsecond=0) + dt.timedelta(seconds=1)
        stop = start + dt.timedelta(seconds=seconds)

        def _add():
            rec = Recording(name='race', url=lone.stream_url, start_time=start, stop_time=stop,
                            scheduled_start_time=start, scheduled_stop_time=stop,
                            status=REC_STATUS_SCHEDULED, channel_id=lone.id,
                            source_channel_id=lone.id, program_start_time=program_start,
                            program_stop_time=program_start + dt.timedelta(hours=1),
                            program_title=TITLE)
            db.session.add(rec)
            db.session.commit()
            return rec.id

        rid = _add()
        schedule_recording(self.t.app, rid, start, stop)
        return rid

    def test_a_dead_event_channel_moves_to_another_channel_carrying_the_game(self):
        lone, wide = self.channel('cove-tv'), self.channel('reed-tv')
        lone_label, wide_label = self.label(lone.id), self.label(wide.id)
        race_at = dt.datetime.utcnow().replace(microsecond=0) + dt.timedelta(minutes=3)
        event_id = self._race(lone, wide, race_at)
        # Every connection to the event channel is answered and closed before a byte, the way
        # recording 88's was on every try.
        self.sim.inject('close_mid_stream', lone_label, after_bytes=0, start=at(-1), end=at(900))
        rid = self._record_game(lone, race_at - dt.timedelta(minutes=5), seconds=75)
        rec = self.finish(rid, 240)

        self.assertEqual(rec.status, REC_STATUS_COMPLETED, self.event_types(rid))
        self.assertEqual(rec.channel_id, wide.id)
        move = self.assertEvent(rid, GROUP_FAILOVER)
        self.assertIn('another channel carrying', move.detail)
        extra = json.loads(move.extra_data)
        self.assertEqual((extra['game_event_id'], extra['group_id']), (event_id, None))

        answer = self.answer(rid, serving=[lone_label, wide_label])
        self.assertNoProblems(answer)
        self.assertAgrees(answer, self.claims(rid))
        # Only the channel carrying the game delivered anything.
        self.assertEqual(answer.channels, (wide_label,))
        # What the move cost is the time the event channel took to be called dead, put down to
        # the dead channel; the wider channel's connect burst repeats no more than a burst.
        self.assertLessEqual({x.reason for x in answer.lost},
                             {ak.REASON_REFUSED, ak.REASON_NOT_SENT, ak.REASON_NOT_RECORDED},
                             answer.describe())
        self.assertLessEqual(answer.repeated_seconds, BURST + KEYFRAME_SECONDS, answer.describe())


if __name__ == '__main__':
    unittest.main()
