"""Tier 2 - a group's "Updated" date moves when the user edits the group.

`BUGS.md` 2026-09-19 @ 06:47:38 PM. `ChannelGroup.updated_at` is rendered as the
"Updated" line on the group detail page and carries SQLAlchemy's `onupdate`, which fires
only when a column on the group row itself is written. The settings stored there - name,
guide flag, format lock and strategy, muted warnings - moved it; everything else did not,
so adding or removing channels, moving a participation switch, or editing the attached
health check's profile or schedule left the date sitting in the past while the page
around it showed the new state.

`channel_groups.py::touch_group()` is the one helper that closes that gap, and these
tests pin both halves of what it is for: the edits that must move the date, and the run
activity that must NOT - a check running writes the job's status and completion time, and
if that moved "Updated" too the date would answer two questions at once and the user
could no longer tell "I changed this" from "this ran".
"""
import os
import sys
import unittest
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tests.support.app import make_test_app  # noqa: E402
from tests.support.seed import (make_account, make_channel, make_group,  # noqa: E402
                                make_test_job)
from app import db  # noqa: E402
from app.channel_groups import set_participation, touch_group  # noqa: E402
from app.database import ChannelGroup, ChannelGroupMember  # noqa: E402


# Far enough back that any real "now" beats it, and unambiguous in an assertion message.
STALE = datetime(2020, 1, 1, 0, 0, 0)


class _GroupDateCase(unittest.TestCase):
    def setUp(self):
        self.t = make_test_app()
        # Every route exercised here is a CSRF-protected API POST and the token is not
        # what any of this is about - same as test_group_guide_invariant.py.
        self.t.app.config['WTF_CSRF_ENABLED'] = False
        self.ctx = self.t.app.app_context()
        self.ctx.push()
        self.client = self.t.client
        self.acct = make_account()

    def tearDown(self):
        self.ctx.pop()
        self.t.cleanup()

    def _stale(self, group):
        """Back-date the group so a touch is visible, WITHOUT going through the ORM's
        onupdate - a plain attribute write would itself be the thing under test."""
        db.session.query(ChannelGroup).filter_by(id=group.id).update(
            {'updated_at': STALE}, synchronize_session=False)
        db.session.commit()
        db.session.expire_all()

    def _updated_at(self, group_id):
        db.session.expire_all()
        return db.session.get(ChannelGroup, group_id).updated_at


class TouchGroupHelperTests(_GroupDateCase):
    def test_touch_moves_the_date_to_now(self):
        grp = make_group(members=[make_channel(self.acct)])
        self._stale(grp)
        grp = db.session.get(ChannelGroup, grp.id)
        touch_group(grp)
        db.session.commit()
        self.assertGreater(self._updated_at(grp.id), STALE)

    def test_touch_never_moves_the_date_backwards(self):
        """Timestamp anchors only move forward - observations can arrive out of order,
        and a date that can go backwards is worse than one that is merely coarse."""
        grp = make_group(members=[make_channel(self.acct)])
        future = datetime.utcnow() + timedelta(days=30)
        db.session.query(ChannelGroup).filter_by(id=grp.id).update(
            {'updated_at': future}, synchronize_session=False)
        db.session.commit()
        db.session.expire_all()
        grp = db.session.get(ChannelGroup, grp.id)
        touch_group(grp)
        db.session.commit()
        self.assertEqual(future, self._updated_at(grp.id))

    def test_touch_accepts_none(self):
        """A job need not have a group, and a caller holding one should not have to
        guard - the five health-check settings routes all call touch_group(job.group)."""
        touch_group(None)  # must not raise


class MembershipMovesTheDateTests(_GroupDateCase):
    def test_adding_channels_moves_the_date(self):
        ch1 = make_channel(self.acct, name='Feed 1')
        ch2 = make_channel(self.acct, name='Feed 2')
        grp = make_group(members=[ch1])
        self._stale(grp)
        r = self.client.post(f'/api/channel-groups/{grp.id}/members',
                             json={'channel_ids': [ch2.id], 'force': True})
        self.assertEqual(200, r.status_code, r.get_data(as_text=True))
        self.assertGreater(self._updated_at(grp.id), STALE)

    def test_removing_a_channel_moves_the_date(self):
        ch1 = make_channel(self.acct, name='Feed 1')
        ch2 = make_channel(self.acct, name='Feed 2')
        grp = make_group(members=[ch1, ch2], in_guide=False)
        self._stale(grp)
        r = self.client.post(f'/api/channel-groups/{grp.id}/members/remove',
                             json={'channel_id': ch2.id, 'confirm': True})
        self.assertEqual(200, r.status_code, r.get_data(as_text=True))
        self.assertGreater(self._updated_at(grp.id), STALE)

    def test_adding_a_channel_to_a_health_check_moves_the_date(self):
        """The reported case: channels added to a health check from its own page, which
        writes the same membership rows through a different route."""
        ch1 = make_channel(self.acct, name='Feed 1')
        ch2 = make_channel(self.acct, name='Feed 2')
        job = make_test_job(name='FS2', channels=[ch1])
        self._stale(job.group)
        group_id = job.group_id
        r = self.client.post(f'/api/channel-tests/on-demand/{job.id}/channels',
                             json={'channel_ids': [ch2.id]})
        self.assertEqual(200, r.status_code, r.get_data(as_text=True))
        self.assertGreater(self._updated_at(group_id), STALE)

    def test_removing_a_channel_from_a_health_check_moves_the_date(self):
        ch1 = make_channel(self.acct, name='Feed 1')
        ch2 = make_channel(self.acct, name='Feed 2')
        job = make_test_job(name='FS2', channels=[ch1, ch2])
        self._stale(job.group)
        group_id = job.group_id
        r = self.client.delete(
            f'/api/channel-tests/on-demand/{job.id}/channels/{ch2.id}')
        self.assertEqual(200, r.status_code, r.get_data(as_text=True))
        self.assertGreater(self._updated_at(group_id), STALE)


class ParticipationMovesTheDateTests(_GroupDateCase):
    def test_moving_a_switch_moves_the_date(self):
        ch = make_channel(self.acct)
        grp = make_group(members=[ch])
        self._stale(grp)
        m = ChannelGroupMember.query.filter_by(group_id=grp.id, channel_id=ch.id).one()
        self.assertTrue(set_participation(m, 'test_enabled', False))
        db.session.commit()
        self.assertGreater(self._updated_at(grp.id), STALE)

    def test_a_switch_that_did_not_move_leaves_the_date_alone(self):
        """set_participation() returns False and logs nothing for a no-op; the date is
        held to the same standard, or re-saving an unchanged settings modal would claim
        an edit that never happened."""
        ch = make_channel(self.acct)
        grp = make_group(members=[ch])
        self._stale(grp)
        m = ChannelGroupMember.query.filter_by(group_id=grp.id, channel_id=ch.id).one()
        self.assertFalse(set_participation(m, 'test_enabled', True))
        db.session.commit()
        self.assertEqual(STALE, self._updated_at(grp.id))


class HealthCheckSettingsMoveTheDateTests(_GroupDateCase):
    def test_changing_the_profile_moves_the_date(self):
        """The profile lives on the OnDemandTestJob row, so nothing on the group row
        changes and `onupdate` never fires on its own."""
        job = make_test_job(name='FS2', channels=[make_channel(self.acct)])
        self._stale(job.group)
        group_id = job.group_id
        r = self.client.post(f'/api/channel-tests/on-demand/{job.id}/profile',
                             json={'profile_id': None})
        self.assertEqual(200, r.status_code, r.get_data(as_text=True))
        self.assertGreater(self._updated_at(group_id), STALE)

    def test_unscheduling_moves_the_date(self):
        job = make_test_job(name='FS2', channels=[make_channel(self.acct)],
                            status='SCHEDULED',
                            scheduled_start_time=datetime.utcnow() + timedelta(hours=2))
        self._stale(job.group)
        group_id = job.group_id
        r = self.client.post(f'/api/channel-tests/on-demand/{job.id}/unschedule')
        self.assertEqual(200, r.status_code, r.get_data(as_text=True))
        self.assertGreater(self._updated_at(group_id), STALE)


class RunActivityLeavesTheDateAloneTests(_GroupDateCase):
    def test_a_completed_run_does_not_move_the_date(self):
        """"Updated" answers "when did I last change this", and the page already has a
        separate "Last run" line. Moving it for a run would make one date mean two
        things - and on a recurring check it would move every night, burying every real
        edit the user ever made."""
        job = make_test_job(name='FS2', channels=[make_channel(self.acct)],
                            status='RUNNING')
        self._stale(job.group)
        group_id = job.group_id
        job.status = 'COMPLETED'
        job.completed_at = datetime.utcnow()
        db.session.commit()
        self.assertEqual(STALE, self._updated_at(group_id))


if __name__ == '__main__':
    unittest.main()
