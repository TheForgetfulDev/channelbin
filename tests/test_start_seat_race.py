"""Two starts of one recording share one connection seat, and neither may lose it for the other.

Guards dev/docs/BUGS.md 2026-11-26 "A start that loses the move race gives back the winner's
connection seat" and "connection_limits.try_acquire() can count one holder in two pools"
(dev/changelog/1755, 1756):

  - the start that loses the move's write leaves the seat the winner holds, stops, and neither
    defers nor writes anything on the recording;
  - when the two starts picked channels on different accounts the seat ends on the row's;
  - a start that yields its seat to an earlier recording does not strip another start that
    claimed, and a claim re-asserts its seat;
  - a wait never fails or re-arms a recording that is no longer SCHEDULED;
  - try_acquire() moves a holder seated on another account and never seats it twice;
  - a cancel, a delete or an edit gives back a seat held by a recording nothing captures.

Throwaway temp SQLite DB through make_test_app(); ffmpeg is never launched.

    python3 -m unittest tests.test_start_seat_race
"""
import os
import sys
import threading
import unittest
from datetime import datetime, timedelta
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tests.support.app import make_test_app  # noqa: E402
from tests.support import seed  # noqa: E402
from app import account_links, db, recorder, scheduler, seat_moves  # noqa: E402
from app import connection_limits as connlim  # noqa: E402
from app.database import (  # noqa: E402
    Recording, RecordingEvent, RECORDING_FAILED, RECORDING_MOVED_FOR_CONNECTION,
    RECORDING_START_DEFERRED, REC_STATUS_ABORTED, REC_STATUS_IN_PROGRESS, REC_STATUS_PAUSED,
    REC_STATUS_SCHEDULED,
)

TITLE = 'Cup Race One'
OTHER = 999999


class _Case(unittest.TestCase):
    """Account Full's one connection is held by another recording; the program is also on a
    channel of account Free (and of account Third, for the tests that want two candidates)."""

    def setUp(self):
        self.t = make_test_app()
        self.dvr = os.path.join(self.t._tmpdir, 'dvr')
        os.makedirs(self.dvr, exist_ok=True)
        self.t.sandbox_config({'recording': {
            'dvr_output_dir': self.dvr,
            'capture_log_dir': os.path.join(self.t._tmpdir, 'caplogs'),
            'live_thumbnail': {'enabled': False}}})
        self.full = seed.make_account(name='Full', max_connections=1)
        self.free = seed.make_account(name='Free', max_connections=1)
        self.third = seed.make_account(name='Third', max_connections=1)
        self.mine = seed.make_channel(self.full, name='Mine HD', health_score=90)
        db.session.commit()
        self.program_start = (datetime.utcnow() - timedelta(minutes=1)).replace(microsecond=0)
        self.listing(self.mine)
        self._clear_seats()
        self.full_id, self.free_id, self.third_id = self.full.id, self.free.id, self.third.id

    def tearDown(self):
        self._clear_seats()
        with recorder._lock:
            recorder._active.clear()
        self.t.cleanup()

    @staticmethod
    def _clear_seats():
        connlim._holders.clear()
        connlim._seat_of.clear()

    def listing(self, channel):
        seed.make_epg_entry(channel, title=TITLE, start_time=self.program_start,
                            duration_minutes=60)
        db.session.commit()

    def channel(self, account, name):
        ch = seed.make_channel(account, name=name)
        db.session.commit()
        self.listing(ch)
        return ch

    def scheduled(self, channel=None, **kw):
        now = datetime.utcnow()
        kw.setdefault('start_time', now - timedelta(seconds=30))
        kw.setdefault('stop_time', now + timedelta(hours=1))
        rec = seed.make_recording(
            status=REC_STATUS_SCHEDULED, name='race', channel_id=(channel or self.mine).id,
            program_title=TITLE, program_start_time=self.program_start, **kw)
        db.session.commit()
        return rec.id

    def seats(self, account_id):
        return list(connlim._holders.get(account_id, []))

    def events(self, rid, event_type):
        return RecordingEvent.query.filter_by(recording_id=rid, event_type=event_type).all()

    def row(self, rid):
        db.session.expire_all()
        return db.session.get(Recording, rid)


class MoveRaceTests(_Case):
    """Two threads inside start_recording for one row on a full account. Both take the
    candidate's seat; the second is held there until the first has moved, claimed and
    launched, so its own move is decided against a row the winner already owns."""

    def _race(self, rid):
        barrier = threading.Barrier(2, timeout=10)
        real_candidates = seat_moves.start_candidates
        real_acquire = recorder._try_acquire_slot_with_preemption
        free_id = self.free_id
        launches = []
        guard = threading.Lock()
        seated = []
        waited = []
        winner_launched = threading.Event()

        def candidates_then_meet(*args, **kw):
            # Both starts have their candidates before either takes a seat: a start that
            # looks after the other's acquire sees no free account and simply waits.
            found = real_candidates(*args, **kw)
            barrier.wait()
            return found

        def acquire_then_let_the_first_win(app, recording_id, account_id, acquire=None):
            got = real_acquire(app, recording_id, account_id, acquire)
            if got and account_id == free_id:
                with guard:
                    seated.append(threading.get_ident())
                    second = len(seated) == 2
                if second:
                    # Holds the shared seat while the other start moves, claims and launches.
                    waited.append(winner_launched.wait(timeout=10))
            return got

        def launch(app, recording_id, seg_num):
            with guard:
                launches.append(recording_id)
            winner_launched.set()
            return recorder.LAUNCH_SPAWNED

        with mock.patch.object(seat_moves, 'start_candidates', candidates_then_meet), \
                mock.patch.object(recorder, '_try_acquire_slot_with_preemption',
                                  acquire_then_let_the_first_win), \
                mock.patch.object(recorder, '_launch_segment', launch), \
                mock.patch('app.scheduler.reschedule_recording_start') as rearm:
            threads = [threading.Thread(target=recorder.start_recording, args=(self.t.app, rid))
                       for _ in range(2)]
            for th in threads:
                th.start()
            for th in threads:
                th.join(timeout=30)
                self.assertFalse(th.is_alive(), 'a start_recording thread never finished')
        self.assertEqual(waited, [True], 'the second start never waited on the first')
        return launches, rearm

    def setUp(self):
        super().setUp()
        connlim.try_acquire(self.full_id, 'recording', OTHER)
        self.there = self.channel(self.free, 'There')
        self.there_id = self.there.id
        self.rid = self.scheduled()

    def test_the_winner_keeps_the_seat_it_records_on(self):
        launches, _ = self._race(self.rid)
        self.assertEqual(launches, [self.rid])
        rec = self.row(self.rid)
        self.assertEqual((rec.status, rec.channel_id), (REC_STATUS_IN_PROGRESS, self.there_id))
        self.assertEqual(self.seats(self.free_id), [('recording', self.rid)],
                         "the losing start gave back the winner's seat, so a second "
                         'connection can open on a one-connection account')

    def test_the_loser_neither_defers_nor_rearms_a_running_recording(self):
        _, rearm = self._race(self.rid)
        self.assertEqual(self.events(self.rid, RECORDING_START_DEFERRED), [])
        rearm.assert_not_called()
        self.assertEqual(len(self.events(self.rid, RECORDING_MOVED_FOR_CONNECTION)), 1)


class LostMoveSeatTests(_Case):
    """_settle_shared_seat() on its own: where the one seat ends for each state of the row."""

    def setUp(self):
        super().setUp()
        self.there = self.channel(self.free, 'There')
        self.elsewhere = self.channel(self.third, 'Elsewhere')

    def test_a_row_on_the_account_the_seat_is_on_keeps_it(self):
        for status in (REC_STATUS_SCHEDULED, REC_STATUS_IN_PROGRESS):
            with self.subTest(status=status):
                self._clear_seats()
                rid = self.scheduled(self.there)
                db.session.get(Recording, rid).status = status
                db.session.commit()
                connlim.try_acquire(self.free_id, 'recording', rid)
                recorder._settle_shared_seat(rid, self.free_id, scheduled_counts=True)
                self.assertEqual(self.seats(self.free_id), [('recording', rid)])

    def test_a_row_on_another_account_gets_the_seat_there(self):
        """The two starts picked different candidates: the second acquire carried the seat to
        its account, and the winner records on the first."""
        rid = self.scheduled(self.elsewhere)
        connlim.try_acquire(self.third_id, 'recording', rid)
        connlim.try_acquire(self.free_id, 'recording', rid)
        recorder._settle_shared_seat(rid, self.free_id, scheduled_counts=True)
        self.assertEqual(self.seats(self.third_id), [('recording', rid)])
        self.assertEqual(self.seats(self.free_id), [])

    def test_a_cancelled_or_deleted_row_gives_the_seat_back(self):
        rid = self.scheduled(self.there)
        db.session.get(Recording, rid).status = REC_STATUS_ABORTED
        db.session.commit()
        connlim.try_acquire(self.free_id, 'recording', rid)
        recorder._settle_shared_seat(rid, self.free_id, scheduled_counts=True)
        self.assertEqual(self.seats(self.free_id), [])
        connlim.try_acquire(self.free_id, 'recording', 424242)
        recorder._settle_shared_seat(424242, self.free_id, scheduled_counts=True)
        self.assertEqual(self.seats(self.free_id), [])


    def test_a_scheduled_row_counts_only_when_another_start_may_own_it(self):
        rid = self.scheduled(self.there)
        connlim.try_acquire(self.free_id, 'recording', rid)
        recorder._settle_shared_seat(rid, self.free_id, scheduled_counts=False)
        self.assertEqual(self.seats(self.free_id), [])

    def test_a_paused_recording_holds_no_seat(self):
        rid = self.scheduled(self.there)
        db.session.get(Recording, rid).status = REC_STATUS_PAUSED
        db.session.commit()
        connlim.try_acquire(self.free_id, 'recording', rid)
        recorder._settle_shared_seat(rid, self.free_id, scheduled_counts=True)
        self.assertEqual(self.seats(self.free_id), [])


class LosingExitTests(_Case):
    """Each way a start stops for another start, driven through start_recording()."""

    def start(self, rid):
        with mock.patch('app.scheduler.reschedule_recording_start') as rearm, \
                mock.patch.object(recorder, '_launch_segment',
                                  return_value=recorder.LAUNCH_SPAWNED) as launch:
            recorder.start_recording(self.t.app, rid)
        return rearm, launch

    def owner_takes(self, rid, channel_id):
        db.session.execute(
            db.text('UPDATE recordings SET status = :s, channel_id = :c WHERE id = :i'),
            {'s': REC_STATUS_IN_PROGRESS, 'c': channel_id, 'i': rid})
        db.session.commit()

    def test_a_slow_start_that_acquires_on_the_old_account_puts_the_seat_back(self):
        """The owner moved the recording to Free and is recording there. This start read the
        row on Full, whose seat has freed, so its acquire carries the shared seat to Full; it
        then meets the owner's live state and must leave the seat on Free."""
        there_id = self.channel(self.free, 'There').id
        rid = self.scheduled()
        real_probe = recorder.probe_dir

        def probe_while_the_owner_moves(path):
            self.owner_takes(rid, there_id)
            connlim.try_acquire(self.free_id, 'recording', rid)
            with recorder._lock:
                recorder._active[rid] = recorder.RecordingState()
            return real_probe(path)

        with mock.patch.object(recorder, 'probe_dir', probe_while_the_owner_moves):
            _, launch = self.start(rid)
        launch.assert_not_called()
        self.assertEqual(self.seats(self.free_id), [('recording', rid)])
        self.assertEqual(self.seats(self.full_id), [])

    def test_a_start_that_loses_the_claim_on_another_account_puts_the_seat_back(self):
        there_id = self.channel(self.free, 'There').id
        rid = self.scheduled()
        real_claim = recorder._claim_live_state

        def claim_after_the_owner_claims(recording_id, state):
            self.owner_takes(rid, there_id)
            return real_claim(recording_id, state)

        connlim.try_acquire(self.free_id, 'recording', rid)
        with mock.patch.object(recorder, '_claim_live_state', claim_after_the_owner_claims):
            _, launch = self.start(rid)
        launch.assert_not_called()
        self.assertEqual(self.seats(self.free_id), [('recording', rid)])
        self.assertEqual(self.seats(self.full_id), [])

    def test_a_move_whose_write_raises_gives_back_a_seat_nobody_else_holds(self):
        connlim.try_acquire(self.full_id, 'recording', OTHER)
        self.channel(self.free, 'There')
        rid = self.scheduled()
        with mock.patch.object(recorder, 'add_recording_event', side_effect=RuntimeError('boom')):
            with self.assertRaises(RuntimeError):
                self.start(rid)
        self.assertEqual(self.seats(self.free_id), [])
        self.assertEqual(self.row(rid).channel_id, self.mine.id)

    def test_a_move_whose_write_raises_leaves_the_seat_of_a_start_that_moved_it(self):
        connlim.try_acquire(self.full_id, 'recording', OTHER)
        there_id = self.channel(self.free, 'There').id
        rid = self.scheduled()

        def the_other_start_moved_it(*args, **kw):
            db.session.rollback()
            db.session.execute(db.text('UPDATE recordings SET channel_id = :c WHERE id = :i'),
                               {'c': there_id, 'i': rid})
            db.session.commit()
            raise RuntimeError('boom')

        with mock.patch.object(recorder, 'add_recording_event',
                               side_effect=the_other_start_moved_it):
            with self.assertRaises(RuntimeError):
                self.start(rid)
        self.assertEqual(self.seats(self.free_id), [('recording', rid)])

    def test_a_start_that_would_yield_stops_when_the_recording_is_claimed(self):
        rid = self.scheduled()
        mine_id = self.mine.id

        def an_earlier_waiter_and_a_claim(recording_id, account_id, start_time):
            self.owner_takes(rid, mine_id)
            return mock.Mock(id=OTHER)

        with mock.patch.object(recorder, '_earlier_waiter_for_slot', an_earlier_waiter_and_a_claim), \
                mock.patch.object(connlim, 'at_limit', return_value=True):
            rearm, launch = self.start(rid)
        launch.assert_not_called()
        rearm.assert_not_called()
        self.assertEqual(self.seats(self.full_id), [('recording', rid)])
        self.assertEqual(self.events(rid, RECORDING_START_DEFERRED), [])

    def test_a_claim_asserts_a_seat_given_away_before_it(self):
        rid = self.scheduled()
        real_claim = recorder._claim_live_state

        def claim_after_the_seat_is_given_away(recording_id, state):
            connlim.release(self.full_id, 'recording', rid)
            return real_claim(recording_id, state)

        with mock.patch.object(recorder, '_claim_live_state', claim_after_the_seat_is_given_away):
            _, launch = self.start(rid)
        launch.assert_called_once()
        self.assertEqual(self.seats(self.full_id), [('recording', rid)])


class FairnessYieldTests(_Case):

    def test_a_start_that_yields_leaves_a_claimed_recording_its_seat(self):
        rid = self.scheduled()
        connlim.try_acquire(self.full_id, 'recording', rid)
        db.session.get(Recording, rid).status = REC_STATUS_IN_PROGRESS
        db.session.commit()
        self.assertFalse(recorder._yield_start_seat(rid, self.full_id))
        self.assertEqual(self.seats(self.full_id), [('recording', rid)])

    def test_a_claim_between_the_release_and_the_recheck_gets_the_seat_back(self):
        rid = self.scheduled()
        connlim.try_acquire(self.full_id, 'recording', rid)
        real = connlim.release

        def release_then_the_other_start_claims(account_id, kind, holder_id):
            real(account_id, kind, holder_id)
            db.session.execute(db.text('UPDATE recordings SET status = :s WHERE id = :i'),
                               {'s': REC_STATUS_IN_PROGRESS, 'i': rid})
            db.session.commit()

        with mock.patch.object(connlim, 'release', release_then_the_other_start_claims):
            self.assertFalse(recorder._yield_start_seat(rid, self.full_id))
        self.assertEqual(self.seats(self.full_id), [('recording', rid)])

    def test_a_scheduled_recording_still_yields(self):
        rid = self.scheduled()
        connlim.try_acquire(self.full_id, 'recording', rid)
        self.assertTrue(recorder._yield_start_seat(rid, self.full_id))
        self.assertEqual(self.seats(self.full_id), [])

    def test_a_claim_takes_back_a_seat_given_away_before_it(self):
        rid = self.scheduled()
        recorder._hold_seat_after_claim(rid, self.full_id)
        self.assertEqual(self.seats(self.full_id), [('recording', rid)])


class WaitOnAClaimedRowTests(_Case):
    """_defer_start_for_slot() and its three FAILED writers on a row another start claimed."""

    def setUp(self):
        super().setUp()
        connlim.try_acquire(self.full_id, 'recording', OTHER)

    def _claimed(self, **kw):
        rid = self.scheduled(**kw)
        db.session.get(Recording, rid).status = REC_STATUS_IN_PROGRESS
        db.session.commit()
        return rid

    def test_a_running_recording_gets_no_wait_note_and_no_rearm(self):
        rid = self._claimed()
        with mock.patch('app.scheduler.reschedule_recording_start') as rearm:
            recorder._defer_start_for_slot(self.t.app, rid, self.full_id)
        rearm.assert_not_called()
        self.assertEqual(self.events(rid, RECORDING_START_DEFERRED), [])

    def test_a_running_recording_past_its_stop_time_is_not_failed(self):
        now = datetime.utcnow()
        rid = self._claimed(start_time=now - timedelta(hours=2),
                            stop_time=now - timedelta(minutes=1))
        recorder._defer_start_for_slot(self.t.app, rid, self.full_id)
        self.assertEqual(self.row(rid).status, REC_STATUS_IN_PROGRESS)
        self.assertEqual(self.events(rid, RECORDING_FAILED), [])

    def test_each_failed_writer_checks_the_row_it_fails(self):
        """The row is SCHEDULED when the wait reads it and claimed before the write."""
        now = datetime.utcnow()
        real_get = db.session.get

        def cases():
            yield 'slot', lambda rec: recorder._defer_start_for_slot(self.t.app, rec.id, self.full_id)
            yield 'block', lambda rec: recorder._defer_start_for_block(
                rec, self.full_id, now + timedelta(hours=1), now)
            yield 'switch', lambda rec: recorder._defer_start_switched_off(
                rec, self.full_id, 'every login on "Full" is turned off', now)

        for name, call in cases():
            with self.subTest(writer=name):
                rid = self.scheduled(start_time=now - timedelta(hours=2),
                                     stop_time=now - timedelta(minutes=1))
                rec = db.session.get(Recording, rid)
                seen = []

                def get_then_claim(model, ident, **kw):
                    # The second read of the row is the writer's own, inside its closure.
                    if model is Recording and ident == rid:
                        seen.append(1)
                        if len(seen) == 2:
                            db.session.execute(
                                db.text('UPDATE recordings SET status = :s WHERE id = :i'),
                                {'s': REC_STATUS_IN_PROGRESS, 'i': rid})
                            db.session.commit()
                    return real_get(model, ident, **kw)

                if name == 'slot':
                    with mock.patch.object(db.session, 'get', get_then_claim):
                        call(rec)
                else:
                    seen.append(1)
                    with mock.patch.object(db.session, 'get', get_then_claim):
                        call(rec)
                self.assertEqual(self.row(rid).status, REC_STATUS_IN_PROGRESS)
                self.assertEqual(self.events(rid, RECORDING_FAILED), [])


class OneHolderOneSeatTests(_Case):

    def test_a_holder_seated_on_another_account_is_moved_not_seated_twice(self):
        self.assertTrue(connlim.try_acquire(self.full_id, 'recording', 7))
        self.assertTrue(connlim.try_acquire(self.free_id, 'recording', 7))
        self.assertEqual(self.seats(self.full_id), [])
        self.assertEqual(self.seats(self.free_id), [('recording', 7)])
        connlim.release(self.free_id, 'recording', 7)
        self.assertTrue(connlim.try_acquire(self.full_id, 'test', 'probe'),
                        'a stray seat was left on the first account')

    def test_a_stray_in_a_login_pool_does_not_outlive_the_release(self):
        """The shape release() could never undo: the first seat is in a login's pool, so the
        fallback pool (the account id) does not find it."""
        account_links.add_login(self.full_id, 'main', 'main', 'pw1', 1)
        db.session.expire_all()
        self.assertTrue(connlim.try_acquire(self.full_id, 'recording', 7))
        self.assertTrue(connlim.try_acquire(self.free_id, 'recording', 7))
        connlim.release(self.free_id, 'recording', 7)
        self.assertEqual(connlim.holder_counts(), {})
        self.assertTrue(connlim.try_acquire(self.full_id, 'recording', 8))

    def test_a_refusal_leaves_the_holder_where_it_was(self):
        connlim.try_acquire(self.full_id, 'recording', 7)
        connlim.try_acquire(self.free_id, 'recording', 8)
        self.assertFalse(connlim.try_acquire(self.free_id, 'recording', 7))
        self.assertEqual(self.seats(self.full_id), [('recording', 7)])
        self.assertEqual(connlim._seat_of[('recording', 7)][0], self.full_id)


class UnstartedSeatTests(_Case):
    """A start that raised after its acquire leaves a SCHEDULED recording seated."""

    def test_unscheduling_gives_the_seat_back(self):
        rid = self.scheduled()
        connlim.try_acquire(self.full_id, 'recording', rid)
        scheduler.unschedule_recording(rid)
        self.assertEqual(self.seats(self.full_id), [])

    def test_a_recording_with_live_state_keeps_its_seat(self):
        rid = self.scheduled()
        connlim.try_acquire(self.full_id, 'recording', rid)
        with recorder._lock:
            recorder._active[rid] = recorder.RecordingState()
        scheduler.unschedule_recording(rid)
        self.assertEqual(self.seats(self.full_id), [('recording', rid)])


if __name__ == '__main__':
    unittest.main()
