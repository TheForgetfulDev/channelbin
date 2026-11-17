"""Which channel an auto-record rule's recording is planned on when the first choice's account
has no connection in the recording's window (app/auto_rules.py::seat_choice,
dev/changelog/1602; dev/docs/BUGS.md 2026-11-16).

  - a full or blocked account moves the pick to the same listing on an account with a
    connection, and the ledger row and the recording's event say so;
  - every copy full keeps the first choice and names each one tried;
  - a first choice with a connection costs one planner pass and says nothing;
  - another listing of the same game is never a stand-in.

A login sibling is refused by the plan itself now (tests/test_schedule_login_seats.py).

Throwaway temp SQLite DB through make_test_app(); no scheduler job is armed.

    python3 -m unittest tests.test_rule_seat_choice
"""
import json
import os
import sys
import unittest
from datetime import datetime, timedelta
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tests.support import make_test_app  # noqa: E402
from tests.support import seed  # noqa: E402
from app import account_blocks, admission, auto_rules, db  # noqa: E402
from app.database import (RECORDING_RULE_SCHEDULED, REC_STATUS_SCHEDULED,  # noqa: E402
                          Recording, RecordingEvent, RuleMatch)


def _future(hours):
    return (datetime.utcnow() + timedelta(hours=hours)).replace(second=0, microsecond=0)


# duplicated from tests/test_auto_rules_engine.py::_EngineCase - each rule suite owns its
# fixture so either can grow without reshaping the other's tests.
class _Case(unittest.TestCase):

    def setUp(self):
        self.t = make_test_app(csrf=False)
        patch('app.routes.recordings.schedule_recording').start()
        self.addCleanup(patch.stopall)
        self.one = seed.make_account(name='Account One')
        self.two = seed.make_account(name='Account Two')
        self._next_stream = 100
        self.start = _future(4)

    def tearDown(self):
        admission.reset_for_tests()
        self.t.cleanup()

    def channel(self, name, account, **kw):
        self._next_stream += 1
        return seed.make_channel(account, stream_id=self._next_stream, name=name, **kw)

    def listing(self, channel, title='Cup Race One', start=None):
        return seed.make_epg_entry(channel, title=title, start_time=start or self.start,
                                   duration_minutes=60)

    def busy(self, account, name='Busy'):
        """A recording by hand on another channel of `account`, across the listing's hour."""
        ch = self.channel(f'{name} channel', account)
        seed.make_recording(status=REC_STATUS_SCHEDULED, name=name, channel_id=ch.id,
                            start_time=self.start - timedelta(minutes=30),
                            stop_time=self.start + timedelta(minutes=90))
        return ch

    def run_rule(self, search='grain=airings&q=Cup+Race'):
        db.session.commit()
        rid = auto_rules.create_rule(auto_rules.read_rule_values(
            {'name': 'Test rule', 'search': search}))
        out = auto_rules.evaluate_now(rid)
        db.session.expire_all()
        return rid, out

    def only(self, rid):
        rec = Recording.query.filter_by(rule_id=rid).one()
        row = RuleMatch.query.filter_by(rule_id=rid, recording_id=rec.id).one()
        ev = RecordingEvent.query.filter_by(recording_id=rec.id,
                                            event_type=RECORDING_RULE_SCHEDULED).one()
        return rec, row, ev


class FullAccountTests(_Case):

    def test_a_full_account_moves_the_pick_to_the_same_listing_on_a_free_one(self):
        """dev/docs/BUGS.md 2026-11-16: the healthier channel is on an account whose one
        connection is committed in the window; the same listing on another account is taken."""
        first = self.channel('Sports One HD', self.one, health_score=90)
        other = self.channel('Sports One', self.two, health_score=60)
        self.listing(first)
        self.listing(other)
        self.busy(self.one)
        rid, out = self.run_rule()
        self.assertEqual(out['scheduled'], 1)
        rec, row, ev = self.only(rid)
        self.assertEqual(rec.channel_id, other.id)
        self.assertEqual(row.channel_id, other.id)
        for text in (row.detail, ev.detail):
            self.assertIn('Scheduled on Sports One.', text)
            self.assertIn('Moved to the same listing on an account with a connection free', text)
            self.assertIn('Passed over "Sports One HD": This would exceed "Account One"', text)
            self.assertNotIn('Chosen over', text)
        extra = json.loads(ev.extra_data)
        self.assertEqual(extra['moved_from_channel_id'], first.id)
        self.assertEqual(extra['passed_over_channel_ids'], [first.id])
        self.assertNotIn('connection_limit_warning', extra['warnings'])

    def test_a_blocked_account_moves_the_pick_too(self):
        first = self.channel('Sports One HD', self.one, health_score=90)
        other = self.channel('Sports One', self.two, health_score=60)
        self.listing(first)
        self.listing(other)
        db.session.commit()
        account_blocks.add_account_block(self.one.id, stop=self.start + timedelta(hours=3),
                                         start=self.start - timedelta(hours=1))
        rid, _out = self.run_rule()
        rec, row, _ev = self.only(rid)
        self.assertEqual(rec.channel_id, other.id)
        self.assertIn('is blocked for all of this window', row.detail)

    def test_every_copy_full_keeps_the_first_choice_and_names_each_one_tried(self):
        first = self.channel('Sports One HD', self.one, health_score=90)
        other = self.channel('Sports One', self.two, health_score=60)
        self.listing(first)
        self.listing(other)
        self.busy(self.one, 'Busy A')
        self.busy(self.two, 'Busy B')
        rid, out = self.run_rule()
        self.assertEqual(out['scheduled'], 1)
        rec, row, ev = self.only(rid)
        self.assertEqual(rec.channel_id, first.id)
        self.assertIn('No other channel with this listing could take it', row.detail)
        self.assertIn('Tried "Sports One HD": This would exceed "Account One"', row.detail)
        self.assertIn('"Sports One": This would exceed "Account Two"', row.detail)
        self.assertNotIn('Chosen over', row.detail)
        self.assertIn('Scheduled with a warning:', row.detail)
        extra = json.loads(ev.extra_data)
        self.assertNotIn('moved_from_channel_id', extra)
        self.assertEqual(extra['passed_over_channel_ids'], [first.id, other.id])

    def test_a_first_choice_with_a_connection_is_one_planner_pass_and_says_nothing(self):
        first = self.channel('Sports One HD', self.one, health_score=90)
        other = self.channel('Sports One', self.two, health_score=60)
        self.listing(first)
        self.listing(other)
        from app.routes import recordings as rec_routes
        with patch.object(rec_routes, '_plan_bulk_schedule',
                          wraps=rec_routes._plan_bulk_schedule) as planner:
            rid, _out = self.run_rule()
        self.assertEqual(planner.call_count, 1)
        rec, row, _ev = self.only(rid)
        self.assertEqual(rec.channel_id, first.id)
        self.assertNotIn('Passed over', row.detail)
        self.assertIn('Chosen over Sports One.', row.detail)

    def test_another_listing_of_the_game_is_never_a_stand_in(self):
        """Only a showing of the same program (title, episode, start) may stand in."""
        first = self.channel('Sports One HD', self.one, health_score=90)
        other = self.channel('Sports One', self.two, health_score=60)
        self.listing(first)
        self.listing(other, title='Cup Race One Replay')
        self.busy(self.one)
        rid, out = self.run_rule()
        self.assertEqual(out['scheduled'], 2)
        mine = Recording.query.filter_by(rule_id=rid, program_title='Cup Race One').one()
        self.assertEqual(mine.channel_id, first.id)

    def test_a_copy_on_the_full_account_itself_is_never_taken(self):
        """A plan on the channel the overlapping recording is on reads clean, because the
        limit check sees a same-channel handoff; taking it would cut that recording short."""
        first = self.channel('Sports One HD', self.one, health_score=90)
        self.listing(first)
        held = self.channel('Sports One', self.one, health_score=60)
        self.listing(held)
        # Part of the hour only: a recording covering the whole listing is the program already
        # recorded, and the rule would skip it before any seat is chosen.
        seed.make_recording(status=REC_STATUS_SCHEDULED, name='by hand', channel_id=held.id,
                            start_time=self.start - timedelta(minutes=30),
                            stop_time=self.start + timedelta(minutes=30))
        rid, _out = self.run_rule()
        rec, row, _ev = self.only(rid)
        self.assertEqual(rec.channel_id, first.id)
        self.assertNotIn('Moved to the same listing', row.detail)

    def test_a_copy_failing_its_health_checks_is_not_taken_for_its_connection(self):
        first = self.channel('Sports One HD', self.one, health_score=90)
        down = self.channel('Sports One', self.two, health_score=5,
                            consecutive_test_failures=99)
        self.listing(first)
        self.listing(down)
        self.busy(self.one)
        rid, _out = self.run_rule()
        rec, row, _ev = self.only(rid)
        self.assertEqual(rec.channel_id, first.id)
        self.assertIn('"Sports One": It is failing its health checks.', row.detail)

    def test_the_ledger_row_carries_the_listing_the_recording_was_made_from(self):
        first = self.channel('Sports One HD', self.one, health_score=90)
        other = self.channel('Sports One', self.two, health_score=60)
        self.listing(first)
        seed.make_epg_entry(other, title='Cup Race One', start_time=self.start,
                            duration_minutes=120)
        self.busy(self.one)
        rid, _out = self.run_rule()
        rec, row, _ev = self.only(rid)
        self.assertEqual(rec.channel_id, other.id)
        self.assertEqual(row.program_stop_time, self.start + timedelta(minutes=120))
        self.assertEqual(row.program_stop_time, rec.program_stop_time)

    def test_at_most_max_seat_tries_channels_are_planned_and_the_rest_counted(self):
        first = self.channel('Sports One HD', self.one, health_score=90)
        self.listing(first)
        for n in range(auto_rules.MAX_SEAT_TRIES + 1):
            self.listing(self.channel(f'Copy {n}', self.two, health_score=50))
        self.busy(self.one)
        self.busy(self.two, 'Busy B')
        from app.routes import recordings as rec_routes
        with patch.object(rec_routes, '_plan_bulk_schedule',
                          wraps=rec_routes._plan_bulk_schedule) as planner:
            rid, _out = self.run_rule()
        self.assertEqual(planner.call_count, auto_rules.MAX_SEAT_TRIES)
        _rec, row, _ev = self.only(rid)
        self.assertIn('2 more channels with this listing were not tried.', row.detail)


if __name__ == '__main__':
    unittest.main()
