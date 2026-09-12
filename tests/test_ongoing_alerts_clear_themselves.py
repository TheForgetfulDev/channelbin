"""Guards dev/changelog/933: the two standing alerts that nothing used to take down.

RECORDING_WAITING_FOR_CONNECTION_SLOT and GROUP_GUIDE_NO_RECORDING_MEMBER each describe a
problem that is STILL TRUE while the row stands, which is what the Alerts page's "Active
alerts" card holds - and that card deliberately offers no Dismiss (dev/changelog/932). Both
were left out of the split because neither had a clearing path, so both landed under "Past
alerts", where a problem that is still happening could be dismissed and never heard about
again.

tests/test_self_clearing_alerts.py is the static half: it scans app/ and fails if a type
carries the self_clearing flag with no dismiss call site behind it. It cannot tell whether
the call site is reached on the paths that matter, which is the whole risk here - a wait
ends five different ways and a broken guide row is fixed three - so this file drives each
one and asserts the row actually comes down.

Two properties are worth naming because they are the reason for the keys chosen:

  * **The slot-wait alert is cleared by recording id, not by (type, source).** Its source
    names the ACCOUNT, so a source-keyed clear would take down every recording queued on
    that account the moment any one of them started.
  * **The broken-guide-row alert is cleared by a group-scoped source.** It used to carry a
    bare 'channel_groups', which one group's fix would have used to clear every other
    group's row.

No network, no real ffmpeg - ffmpeg is never spawned here because _launch_segment is
patched out at every start (CLAUDE.md §Testing).
Run standalone:
  python3 -m unittest tests.test_ongoing_alerts_clear_themselves
"""
import os
import sys
import unittest
from datetime import datetime, timedelta
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tests.support.app import make_test_app  # noqa: E402
from tests.support import seed  # noqa: E402
from app import db, recorder, channel_groups  # noqa: E402
from app import connection_limits as connlim  # noqa: E402
from app.database import (  # noqa: E402
    Alert, ChannelGroup, Recording,
    REC_STATUS_SCHEDULED, REC_STATUS_PAUSED, REC_STATUS_FAILED,
)

SLOT_WAIT = 'RECORDING_WAITING_FOR_CONNECTION_SLOT'
GUIDE_BROKEN = 'GROUP_GUIDE_NO_RECORDING_MEMBER'


def _open(alert_type, **kw):
    return Alert.query.filter_by(
        alert_type=alert_type, dismissed_at=None, **kw).count()


class _SlotCase(unittest.TestCase):
    """One account with a single connection, so the second recording on it always waits.

    Same shape as tests/test_connection_limit_ceiling.py's fixture, including the
    sandboxed DVR dir - without it start_recording's directory probe sends every case
    down the FAILED path before it ever reaches the slot acquire.
    """

    def setUp(self):
        self.t = make_test_app()
        self.t.app.config['WTF_CSRF_ENABLED'] = False
        self.client = self.t.client
        self.dvr = os.path.join(self.t._tmpdir, 'dvr')
        os.makedirs(self.dvr, exist_ok=True)
        self.t.sandbox_config({'recording': {
            'dvr_output_dir': self.dvr,
            'capture_log_dir': os.path.join(self.t._tmpdir, 'caplogs'),
            'live_thumbnail': {'enabled': False},
        }})
        self.account = seed.make_account(name='One Slot', max_connections=1)
        self.channel = seed.make_channel(self.account, name='The Only Feed')
        self.other_channel = seed.make_channel(self.account, name='Sibling Feed')
        db.session.commit()
        self.account_id = self.account.id
        self.channel_id = self.channel.id
        self.other_channel_id = self.other_channel.id
        connlim._holders.clear()

    def tearDown(self):
        connlim._holders.clear()
        self.t.cleanup()

    def _scheduled(self, channel_id=None, start_offset=-60, name='waiter'):
        now = datetime.utcnow()
        start = now + timedelta(seconds=start_offset)
        rec = seed.make_recording(
            status=REC_STATUS_SCHEDULED, name=name,
            channel_id=channel_id if channel_id is not None else self.channel_id,
            start_time=start, stop_time=start + timedelta(hours=1))
        db.session.commit()
        return rec.id

    def _paused(self, name='paused'):
        now = datetime.utcnow()
        rec = seed.make_recording(
            status=REC_STATUS_PAUSED, name=name, channel_id=self.channel_id,
            start_time=now - timedelta(minutes=30), stop_time=now + timedelta(hours=1))
        db.session.commit()
        return rec.id

    def _occupy_slot(self, holder_id=999999):
        self.assertTrue(connlim.try_acquire(self.account_id, 'recording', holder_id))
        return holder_id

    def _wait_once(self, rid):
        """Drive one deferral, leaving the recording queued with its alert standing."""
        with mock.patch('app.scheduler.reschedule_recording_start'), \
             mock.patch.object(recorder, '_launch_segment'):
            recorder.start_recording(self.t.app, rid)
        db.session.expire_all()
        self.assertEqual(1, _open(SLOT_WAIT, recording_id=rid),
                         'the wait must be announced before anything can clear it')


class SlotWaitClearsTests(_SlotCase):

    def test_starting_clears_the_wait(self):
        holder = self._occupy_slot()
        rid = self._scheduled()
        self._wait_once(rid)

        connlim.release(self.account_id, 'recording', holder)
        with mock.patch('app.scheduler.reschedule_recording_start'), \
             mock.patch.object(recorder, '_launch_segment') as launch:
            recorder.start_recording(self.t.app, rid)

        db.session.expire_all()
        self.assertTrue(launch.called, 'the recording should have started')
        self.assertEqual(0, _open(SLOT_WAIT, recording_id=rid))
        self.assertEqual(1, Alert.query.filter_by(
            alert_type=SLOT_WAIT, recording_id=rid).count(),
            'the row is dismissed, not deleted - the wait is part of the history')

    def test_another_recordings_wait_is_untouched(self):
        """The reason the clear is keyed on the recording id. Both waiters are queued on
        ONE account, so a clear keyed on the alert's source - which names that account -
        would take down the still-waiting recording's row too."""
        holder = self._occupy_slot()
        first = self._scheduled(start_offset=-120, name='first')
        second = self._scheduled(channel_id=self.other_channel_id, start_offset=-60,
                                 name='second')
        self._wait_once(first)
        self._wait_once(second)

        connlim.release(self.account_id, 'recording', holder)
        with mock.patch('app.scheduler.reschedule_recording_start'), \
             mock.patch.object(recorder, '_launch_segment'):
            recorder.start_recording(self.t.app, first)

        db.session.expire_all()
        self.assertEqual(0, _open(SLOT_WAIT, recording_id=first))
        self.assertEqual(1, _open(SLOT_WAIT, recording_id=second),
                         'the recording that is still queued keeps its alert')

    def test_a_window_that_ends_while_waiting_clears_the_wait(self):
        self._occupy_slot()
        rid = self._scheduled()
        self._wait_once(rid)

        rec = db.session.get(Recording, rid)
        rec.stop_time = datetime.utcnow() - timedelta(seconds=1)
        db.session.commit()
        with mock.patch('app.scheduler.reschedule_recording_start'), \
             mock.patch.object(recorder, '_launch_segment'):
            recorder.start_recording(self.t.app, rid)

        db.session.expire_all()
        self.assertEqual(REC_STATUS_FAILED, db.session.get(Recording, rid).status)
        self.assertEqual(0, _open(SLOT_WAIT, recording_id=rid),
                         'nothing is waiting any more - it gave up')
        self.assertEqual(1, _open('RECORDING_FAILED_CONNECTION_LIMIT',
                                  recording_id=rid),
                         'and the failure that replaced it stands')

    def test_cancelling_a_queued_recording_clears_the_wait(self):
        self._occupy_slot()
        rid = self._scheduled()
        self._wait_once(rid)

        resp = self.client.post(f'/recordings/{rid}/cancel')
        self.assertIn(resp.status_code, (200, 302))
        db.session.expire_all()
        self.assertEqual(0, _open(SLOT_WAIT, recording_id=rid))

    def test_aborting_clears_the_wait(self):
        self._occupy_slot()
        rid = self._scheduled()
        self._wait_once(rid)

        with mock.patch('app.scheduler.unschedule_recording'):
            recorder.abort_recording(self.t.app, rid)

        db.session.expire_all()
        self.assertEqual(0, _open(SLOT_WAIT, recording_id=rid))

    def test_a_group_cancelling_its_schedule_clears_the_wait(self):
        """A guide demotion or a group delete aborts the recordings counting on the group.
        Both go through deregister_cancelled_recordings(), which is where the clear lives
        so neither caller can forget it."""
        self._occupy_slot()
        rid = self._scheduled()
        self._wait_once(rid)

        with mock.patch('app.scheduler.unschedule_recording'):
            channel_groups.deregister_cancelled_recordings([rid])

        db.session.expire_all()
        self.assertEqual(0, _open(SLOT_WAIT, recording_id=rid))

    def test_resuming_clears_the_wait(self):
        holder = self._occupy_slot()
        rid = self._paused()
        with mock.patch('app.scheduler.reschedule_recording_resume', create=True), \
             mock.patch.object(recorder, '_launch_segment'):
            recorder.resume_recording(self.t.app, rid)
            db.session.expire_all()
            self.assertEqual(1, _open(SLOT_WAIT, recording_id=rid))

            connlim.release(self.account_id, 'recording', holder)
            recorder.resume_recording(self.t.app, rid)

        db.session.expire_all()
        self.assertEqual(0, _open(SLOT_WAIT, recording_id=rid))

    def test_a_resume_whose_window_ended_clears_the_wait(self):
        """This path writes no terminal state - the recording's own stop job finalizes
        whatever was captured - but it does stop re-arming, so nothing is waiting."""
        self._occupy_slot()
        rid = self._paused()
        with mock.patch('app.scheduler.reschedule_recording_resume', create=True), \
             mock.patch.object(recorder, '_launch_segment'):
            recorder.resume_recording(self.t.app, rid)
            db.session.expire_all()
            self.assertEqual(1, _open(SLOT_WAIT, recording_id=rid))

            rec = db.session.get(Recording, rid)
            rec.stop_time = datetime.utcnow() - timedelta(seconds=1)
            db.session.commit()
            recorder.resume_recording(self.t.app, rid)

        db.session.expire_all()
        self.assertEqual(0, _open(SLOT_WAIT, recording_id=rid))


class _GuideCase(unittest.TestCase):
    """A group holding a guide row with nothing switched on for recording - §15's breach
    path 3, reported rather than acted on because no human is present to confirm."""

    def setUp(self):
        self.t = make_test_app()
        self.t.app.config['WTF_CSRF_ENABLED'] = False
        self.client = self.t.client
        self.ctx = self.t.app.app_context()
        self.ctx.push()
        self.acct = seed.make_account()

    def tearDown(self):
        self.ctx.pop()
        self.t.cleanup()

    def _broken_group(self, name='Broken'):
        ch = seed.make_channel(self.acct, name=f'{name} Feed')
        grp = seed.make_group(name=name, members=[ch], in_guide=True, recording=False)
        db.session.commit()
        gid, cid = grp.id, ch.id
        self.assertEqual([gid], channel_groups.report_orphaned_guide_groups([gid]))
        db.session.expire_all()
        # Scoped to this group's own source, not a global count: one case below builds two
        # broken groups precisely to prove they clear independently.
        self.assertEqual(1, _open(GUIDE_BROKEN,
                                  source=f'group:{gid}:guide-no-recording-member'))
        return gid, cid

    def _part(self, gid, cid, enabled, field='recording_enabled'):
        return self.client.post(
            f'/api/channel-groups/{gid}/members/participation',
            json={'channel_id': cid, 'field': field, 'enabled': enabled})


class BrokenGuideRowClearsTests(_GuideCase):

    def test_the_alert_names_the_group_in_its_source(self):
        """A bare module name as the source would make one group's fix clear them all,
        and resolves to no deep link either."""
        gid, _ = self._broken_group()
        alert = Alert.query.filter_by(alert_type=GUIDE_BROKEN).first()
        self.assertEqual(f'group:{gid}:guide-no-recording-member', alert.source)

    def test_turning_recording_back_on_clears_it(self):
        gid, cid = self._broken_group()
        resp = self._part(gid, cid, True)
        self.assertEqual(200, resp.status_code, resp.get_json())
        db.session.expire_all()
        self.assertEqual(0, _open(GUIDE_BROKEN))

    def test_the_bulk_switch_clears_it(self):
        gid, cid = self._broken_group()
        resp = self.client.post(
            f'/api/channel-groups/{gid}/members/participation/bulk',
            json={'channel_ids': [cid], 'field': 'recording_enabled', 'enabled': True})
        self.assertEqual(200, resp.status_code, resp.get_json())
        db.session.expire_all()
        self.assertEqual(0, _open(GUIDE_BROKEN))

    def test_taking_the_group_out_of_the_guide_clears_it(self):
        gid, _ = self._broken_group()
        resp = self.client.post(f'/api/channel-groups/{gid}/guide-toggle', json={})
        self.assertEqual(200, resp.status_code, resp.get_json())
        self.assertFalse(resp.get_json()['in_guide'])
        db.session.expire_all()
        self.assertEqual(0, _open(GUIDE_BROKEN),
                         'there is no guide row left to be unable to record from')

    def test_deleting_the_group_clears_it(self):
        gid, _ = self._broken_group()
        resp = self.client.post(f'/api/channel-groups/{gid}/delete',
                                json={'confirm': True})
        self.assertEqual(200, resp.status_code, resp.get_json())
        db.session.expire_all()
        self.assertIsNone(db.session.get(ChannelGroup, gid))
        self.assertEqual(0, _open(GUIDE_BROKEN),
                         'the alert named a group that no longer exists')

    def test_it_stands_while_the_group_is_still_broken(self):
        """The clear re-asks the invariant rather than trusting that a switch moved. This
        one moves the HEALTH CHECK switch, which has no bearing on whether the group can
        produce a file - so the row is still broken and the alert is still true."""
        gid, cid = self._broken_group()
        resp = self._part(gid, cid, False, field='test_enabled')
        self.assertEqual(200, resp.status_code, resp.get_json())
        db.session.expire_all()
        self.assertEqual(1, _open(GUIDE_BROKEN))

    def test_one_groups_fix_does_not_clear_anothers(self):
        first_gid, first_cid = self._broken_group(name='First')
        second_gid, _ = self._broken_group(name='Second')
        self.assertEqual(2, _open(GUIDE_BROKEN))

        self.assertEqual(200, self._part(first_gid, first_cid, True).status_code)
        db.session.expire_all()
        self.assertEqual(
            1, _open(GUIDE_BROKEN),
            'the group nobody fixed keeps its alert')
        still_open = Alert.query.filter_by(
            alert_type=GUIDE_BROKEN, dismissed_at=None).first()
        self.assertEqual(f'group:{second_gid}:guide-no-recording-member',
                         still_open.source)


if __name__ == '__main__':
    unittest.main(verbosity=2)
