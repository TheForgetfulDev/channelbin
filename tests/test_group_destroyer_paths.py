"""Tier 2 - the two destroyer paths that predate DESIGN-channel-groups-model.md §15's gate
and were never wired to it (`dev/changelog/763`).

§15.1's split - a capture under way refuses outright, scheduled recordings are cancelled
on a confirm - was built for the participation switch and the member removal. Dissolving
the group is strictly the more destructive action of the two and took none of it, so:

  * **Deleting a group mid-capture succeeded.** SQLite runs with foreign keys off, so the
    row went and `Recording.group_id` was left dangling; `failover_group_member()` reads
    that as "no group" and stops failing over, with no alert, event or log line naming it.
    A checkbox was refused where the delete was not.
  * **Scheduled recordings were neither cancelled nor mentioned.** A SCHEDULED row
    pointing at a deleted group fires later and records with no member to resolve.
  * **Deleting an account could strand a guide group silently.** The account's cascade
    takes its channels and (via `Channel.group_memberships`) their memberships, so a group
    whose members span accounts can lose its last recording-enabled member - §15's breach
    path 3 with nobody present. `report_orphaned_guide_groups()` existed and had two
    callers; account delete was not one of them.

What these hold down: the refusal is server-side (a hand-crafted `confirm` payload does
not buy a live capture's death), the cancellation and the delete land in ONE commit, the
scheduler deregistration happens after that commit rather than inside it, and the account
delete's group ids are collected BEFORE the cascade - a post-commit query finds nothing,
so getting that ordering wrong produces a change that looks right and does nothing.
"""
import os
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tests.support.app import make_test_app  # noqa: E402
from tests.support.seed import (make_account, make_channel, make_group,  # noqa: E402
                                make_recording, make_test_job)


def _json(resp):
    return resp.get_json() or {}


class _Base(unittest.TestCase):
    def setUp(self):
        self.t = make_test_app()
        # Every one of these is a CSRF-protected API POST; the token is not what any of
        # this is about (same as the §15 gate suite).
        self.t.app.config['WTF_CSRF_ENABLED'] = False
        self.client = self.t.client
        self.ctx = self.t.app.app_context()
        self.ctx.push()
        self.acct = make_account()

    def tearDown(self):
        self.ctx.pop()
        self.t.cleanup()

    def _delete(self, group, **body):
        return self.client.post(f'/api/channel-groups/{group.id}/delete', json=body)


class DeleteGroupRefusesALiveCaptureTests(_Base):
    """§15.1's refusal, applied to the action that destroys the group outright."""

    def _setup(self, status):
        from app import db
        ch = make_channel(self.acct, name='Feed')
        grp = make_group(name='G', members=[ch], in_guide=True, recording=True)
        make_recording(status=status, group_id=grp.id, channel_id=ch.id, name='The Game')
        db.session.commit()
        return grp, ch

    def test_a_capture_in_progress_refuses_the_delete(self):
        from app import db
        from app.database import ChannelGroup
        grp, _ = self._setup('IN_PROGRESS')
        resp = self._delete(grp)
        self.assertEqual(409, resp.status_code, _json(resp))
        self.assertIn('recording_in_progress', _json(resp))
        self.assertNotIn('confirm_required', _json(resp),
                         'a live capture is not a question - offering a confirm makes it one')
        db.session.expire_all()
        self.assertIsNotNone(db.session.get(ChannelGroup, grp.id))

    def test_confirm_does_NOT_override_a_live_capture(self):
        """Enforcement lives server-side: the client's flag only says a sentence was
        shown, and no sentence makes killing a live capture acceptable."""
        from app import db
        from app.database import ChannelGroup
        grp, _ = self._setup('IN_PROGRESS')
        resp = self._delete(grp, confirm=True)
        self.assertEqual(409, resp.status_code, _json(resp))
        db.session.expire_all()
        self.assertIsNotNone(db.session.get(ChannelGroup, grp.id))

    def test_every_restart_blocking_status_refuses(self):
        """CONCATENATING and CONVERTING are captures under way too - the refusal reads
        RESTART_BLOCKING_STATUSES rather than a re-typed 'is it recording' tuple."""
        from app.database import RESTART_BLOCKING_STATUSES
        for status in RESTART_BLOCKING_STATUSES:
            with self.subTest(status=status):
                grp, _ = self._setup(status)
                resp = self._delete(grp, confirm=True)
                self.assertEqual(409, resp.status_code, _json(resp))

    def test_a_finished_recording_does_not_block_the_delete(self):
        """Only work in flight blocks. A COMPLETED recording has its file already."""
        from app import db
        from app.database import ChannelGroup
        grp, _ = self._setup('COMPLETED')
        resp = self._delete(grp)
        self.assertEqual(200, resp.status_code, _json(resp))
        db.session.expire_all()
        self.assertIsNone(db.session.get(ChannelGroup, grp.id))


class DeleteGroupConfirmsScheduledRecordingsTests(_Base):
    """§15.1's other half: nothing has been captured yet, so it is a question."""

    def _setup(self, count=1):
        from app import db
        ch = make_channel(self.acct, name='Feed')
        grp = make_group(name='G', members=[ch], in_guide=True, recording=True)
        recs = [make_recording(status='SCHEDULED', group_id=grp.id, channel_id=ch.id,
                               name=f'Show {i}') for i in range(count)]
        db.session.commit()
        return grp, recs

    def test_scheduled_recordings_refuse_an_unconfirmed_delete(self):
        from app import db
        from app.database import ChannelGroup, Recording, REC_STATUS_SCHEDULED
        grp, recs = self._setup(count=2)
        resp = self._delete(grp)
        self.assertEqual(409, resp.status_code, _json(resp))
        facts = _json(resp)['confirm_required']
        self.assertEqual(2, facts['scheduled_count'],
                         'the dialog names how many are at stake, so the count is the '
                         'servers to state, not the pages to count')
        self.assertEqual('G', facts['group_name'])
        db.session.expire_all()
        self.assertIsNotNone(db.session.get(ChannelGroup, grp.id))
        self.assertEqual(REC_STATUS_SCHEDULED,
                         db.session.get(Recording, recs[0].id).status)

    def test_confirm_deletes_the_group_and_aborts_its_schedule(self):
        from app import db
        from app.database import (ChannelGroup, Recording, RecordingEvent,
                                  REC_STATUS_ABORTED, RECORDING_ABORTED)
        grp, recs = self._setup(count=2)
        resp = self._delete(grp, confirm=True)
        self.assertEqual(200, resp.status_code, _json(resp))
        self.assertEqual(2, _json(resp)['cancelled_recordings'])
        db.session.expire_all()
        self.assertIsNone(db.session.get(ChannelGroup, grp.id))
        for rec in recs:
            row = db.session.get(Recording, rec.id)
            self.assertEqual(REC_STATUS_ABORTED, row.status)
            self.assertIsNotNone(row.completed_at)
            ev = RecordingEvent.query.filter_by(
                recording_id=rec.id, event_type=RECORDING_ABORTED).all()
            self.assertEqual(1, len(ev),
                             'a cancelled recording says why on its own detail page - it '
                             'outlives the group whose timeline would otherwise carry it')
            self.assertIn('G', ev[0].detail)

    def test_the_scheduler_jobs_are_dropped_after_the_commit(self):
        """A non-idempotent side effect never runs inside a retry_on_locked closure, and
        the surviving race is the harmless one: a job that fires early finds an ABORTED
        row, where deregistering first would leave a SCHEDULED row with no job."""
        grp, recs = self._setup(count=1)
        with patch('app.scheduler.unschedule_recording') as un:
            resp = self._delete(grp, confirm=True)
        self.assertEqual(200, resp.status_code, _json(resp))
        self.assertEqual([rec.id for rec in recs], [c.args[0] for c in un.call_args_list])

    def test_a_group_with_no_recordings_needs_no_confirm(self):
        from app import db
        from app.database import ChannelGroup
        ch = make_channel(self.acct, name='Feed')
        grp = make_group(name='G', members=[ch], in_guide=True, recording=True)
        db.session.commit()
        resp = self._delete(grp)
        self.assertEqual(200, resp.status_code, _json(resp))
        self.assertEqual(0, _json(resp)['cancelled_recordings'])
        db.session.expire_all()
        self.assertIsNone(db.session.get(ChannelGroup, grp.id))


class AccountDeleteReportsOrphanedGroupsTests(_Base):
    """§15 breach path 3, second destroyer. Nobody is looking at the stranded group, so
    it keeps its guide row and the state is said out loud instead."""

    def _cross_account_group(self):
        """A group in the guide whose only recording-enabled member belongs to `other`."""
        from app import db
        other = make_account(name='Other')
        mine = make_channel(self.acct, name='Mine')
        theirs = make_channel(other, name='Theirs')
        grp = make_group(name='Cross', members=[mine, theirs], in_guide=True,
                         recording=True, disabled=[mine.id])
        db.session.commit()
        return grp, other

    def test_deleting_the_account_holding_the_last_member_reports_the_group(self):
        from app import db
        from app.database import ChannelGroup, ChannelGroupEvent, GROUP_GUIDE_BROKEN
        grp, other = self._cross_account_group()
        resp = self.client.delete(f'/api/accounts/{other.id}')
        self.assertEqual(200, resp.status_code, _json(resp))
        db.session.expire_all()
        self.assertTrue(db.session.get(ChannelGroup, grp.id).in_guide,
                        'the row stays - quietly pulling one out with nobody present is '
                        'the silent behavior §15 refuses')
        self.assertEqual(1, ChannelGroupEvent.query.filter_by(
            group_id=grp.id, event_type=GROUP_GUIDE_BROKEN).count())

    def test_the_report_raises_an_alert_naming_the_group(self):
        from app.database import Alert
        grp, other = self._cross_account_group()
        self.client.delete(f'/api/accounts/{other.id}')
        alerts = Alert.query.filter_by(
            alert_type='GROUP_GUIDE_NO_RECORDING_MEMBER').all()
        self.assertEqual(1, len(alerts))
        self.assertIn(grp.name, alerts[0].title)

    def test_a_group_that_keeps_a_recording_member_is_not_reported(self):
        """The report is about the invariant, not about losing any member at all."""
        from app import db
        from app.database import ChannelGroupEvent, GROUP_GUIDE_BROKEN
        other = make_account(name='Other')
        mine = make_channel(self.acct, name='Mine')
        theirs = make_channel(other, name='Theirs')
        grp = make_group(name='Cross', members=[mine, theirs], in_guide=True,
                         recording=True)
        db.session.commit()
        self.client.delete(f'/api/accounts/{other.id}')
        db.session.expire_all()
        self.assertEqual(0, ChannelGroupEvent.query.filter_by(
            group_id=grp.id, event_type=GROUP_GUIDE_BROKEN).count())


class NoCheckDeleteRouteTests(_Base):
    """The check-delete route was a second door onto the damage delete_group is gated
    against (dev/changelog/869), and it is gone: a check is its group's one schedule and
    dies only with the group, through delete_group's gate (dev/changelog/1077)."""

    def test_the_check_delete_route_no_longer_exists(self):
        from app import db
        from app.database import ChannelGroup, OnDemandTestJob
        ch = make_channel(self.acct, name='Feed')
        job = make_test_job(name='Nightly', channels=[ch])
        db.session.commit()
        resp = self.client.delete(f'/api/channel-tests/on-demand/{job.id}')
        self.assertEqual(404, resp.status_code, 'no route answers a lone check delete')
        db.session.expire_all()
        self.assertIsNotNone(db.session.get(OnDemandTestJob, job.id))
        self.assertIsNotNone(db.session.get(ChannelGroup, job.group_id))


if __name__ == '__main__':
    unittest.main()
