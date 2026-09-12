"""A health check run settles its groups' formats once, at the end (dev/changelog/934).

The format re-check used to run after every single test, over a test map where half the
group's members carried this run's numbers and the rest carried the previous run's. The
format that ranking picks - the strategy's bucket, or the derived reference under
`highest_score` - therefore kept changing mid-run and settled only by accident: measured on
the live database, Fox Sports 1's reference moved to 1080p60 and back to 720p60 inside 18
minutes on 2026-09-11, and CW's lock moved 1920x1080 @ 60 -> 1280x720 @ 30 and back within
15 minutes on 09-04. Each move changes which member a recording would start on.

What is guarded here:

  * A test that belongs to a run defers its groups to the run's own settle pass; a one-off
    test or a pre-check, which IS the whole run, still reconciles immediately.
  * The settle pass does BOTH halves per group - apply the strategy, and reconcile - because
    apply_format_strategy() reconciles only when it moved or cleared a lock, so a strategy
    that manages no lock would otherwise never reconcile at all.
  * A run that is stopped part-way still settles, from whatever it did measure.

A format that follows the data is the point (DECIDED 9) - this changes only how often it is
asked, never which format wins.

No network, no real ffmpeg - see CLAUDE.md §Testing.
Run standalone:
  python3 -m unittest tests.test_group_format_settles_once_per_run
"""
import unittest
from unittest import mock

from app import channel_tester, db
from app.database import (ChannelGroup, ChannelGroupEvent, OnDemandTestJob,
                          CHANNEL_GROUP_FORMAT_MISMATCH, GROUP_FORMAT_STRATEGY_APPLIED,
                          GROUP_FORMAT_HIGHEST_SCORE)
from tests.support.app import make_test_app
from tests.support import seed

HD = ('1920x1080', 60)
SD = ('1280x720', 30)


class _Case(unittest.TestCase):
    def setUp(self):
        self.t = make_test_app()
        self.ctx = self.t.app.app_context()
        self.ctx.push()
        self.acct = seed.make_account()

    def tearDown(self):
        self.ctx.pop()
        self.t.cleanup()

    def _channel(self, name, key=None, score=90):
        ch = seed.make_channel(self.acct, name=name)
        ch.health_score = score
        if key is not None:
            seed.make_channel_test(ch, all_null=False, status='COMPLETED', connected=True,
                                   resolution=key[0], fps=float(key[1]), bitrate_kbps=5000)
        db.session.commit()
        return ch

    def _events(self, group_id, event_type):
        return ChannelGroupEvent.query.filter_by(
            group_id=group_id, event_type=event_type).all()


class DeferralDuringARunTests(_Case):
    """Which tests defer their group format work, and which do it on the spot."""

    def test_a_run_defers_every_test_to_its_own_settle_pass(self):
        ch = self._channel('Feed 0', HD)
        with mock.patch.object(channel_tester, 'run_channel_test') as spy:
            channel_tester._run_channel_loop(self.t.app, [ch], 0, job_id=7)
        self.assertTrue(spy.call_args.kwargs.get('defer_group_format'),
                        'a test inside a run must leave its groups to the run-end settle')

    def test_run_channel_test_forwards_the_deferral(self):
        ch = self._channel('Feed 0', HD)
        with mock.patch.object(channel_tester, '_run_channel_test_inner') as inner:
            channel_tester.run_channel_test(self.t.app, ch.id, job_id=7,
                                            defer_group_format=True)
        self.assertIs(True, inner.call_args.kwargs.get('defer_group_format'))

    def test_a_test_outside_a_run_still_reconciles_immediately(self):
        """A one-off test and a pre-check are the whole run, so there is no end-of-run pass
        to defer to - deferring there would mean never reconciling at all."""
        ch = self._channel('Feed 0', HD)
        with mock.patch.object(channel_tester, '_run_channel_test_inner') as inner:
            channel_tester.run_channel_test(self.t.app, ch.id)
        self.assertIs(False, inner.call_args.kwargs.get('defer_group_format'))


class SettlePassTests(_Case):
    """The pass itself: both halves, once, per group the run gathered data for."""

    def test_it_reconciles_a_group_whose_strategy_writes_no_lock(self):
        """`highest_score` pins nothing, so apply_format_strategy() never reconciles for
        it. With the per-test pass gone, this is the only thing that logs which members
        differ from what such a group would record as - Fox Sports 1 is exactly this
        group."""
        best = self._channel('Feed best', HD, score=90)
        other = self._channel('Feed other', SD, score=80)
        grp = seed.make_group(name='FS1', members=[best, other], in_guide=True,
                              format_strategy=GROUP_FORMAT_HIGHEST_SCORE)
        db.session.commit()
        gid = grp.id

        channel_tester._settle_group_formats(self.t.app, [best.id, other.id])
        db.session.expire_all()

        events = self._events(gid, CHANNEL_GROUP_FORMAT_MISMATCH)
        self.assertEqual(1, len(events),
                         'the member that differs from the derived reference is logged')
        self.assertEqual(other.id, events[0].channel_id)
        self.assertIsNone(db.session.get(ChannelGroup, gid).locked_format_key,
                          'highest_score still pins nothing')

    def test_a_lock_move_is_logged_once_and_not_reconciled_twice(self):
        odd = self._channel('Feed odd', HD, score=90)
        a = self._channel('Feed a', SD, score=80)
        b = self._channel('Feed b', SD, score=70)
        grp = seed.make_group(name='CW', members=[odd, a, b], in_guide=True,
                              format_strategy='most_channels')
        db.session.commit()
        gid = grp.id

        channel_tester._settle_group_formats(self.t.app, [odd.id, a.id, b.id])
        db.session.expire_all()

        self.assertEqual(SD, db.session.get(ChannelGroup, gid).locked_format_key)
        self.assertEqual(1, len(self._events(gid, GROUP_FORMAT_STRATEGY_APPLIED)))
        mismatched = self._events(gid, CHANNEL_GROUP_FORMAT_MISMATCH)
        self.assertEqual([odd.id], [e.channel_id for e in mismatched],
                         'the member off the new lock is logged exactly once')

    def test_a_second_pass_over_unchanged_data_writes_nothing(self):
        """The settle pass is what now runs nightly, so it has to stay silent when it
        agrees with itself - a log that repeats every night buries the night it did not."""
        odd = self._channel('Feed odd', HD, score=90)
        a = self._channel('Feed a', SD, score=80)
        b = self._channel('Feed b', SD, score=70)
        grp = seed.make_group(name='CW', members=[odd, a, b], in_guide=True,
                              format_strategy='most_channels')
        db.session.commit()
        gid = grp.id
        ids = [odd.id, a.id, b.id]

        channel_tester._settle_group_formats(self.t.app, ids)
        db.session.expire_all()
        before = ChannelGroupEvent.query.filter_by(group_id=gid).count()

        channel_tester._settle_group_formats(self.t.app, ids)
        db.session.expire_all()
        self.assertEqual(before, ChannelGroupEvent.query.filter_by(group_id=gid).count())


class StoppedRunTests(_Case):
    """A run stopped part-way still settles, from whatever it did measure."""

    def test_a_stopped_run_settles_its_groups(self):
        ch = self._channel('Feed 0', SD, score=90)
        other = self._channel('Feed 1', SD, score=80)
        grp = seed.make_group(name='CW', members=[ch, other], in_guide=True,
                              format_strategy='most_channels')
        job = seed.make_test_job(name='Nightly', channels=[ch, other], status='RUNNING')
        db.session.commit()
        gid, job_id = grp.id, job.id

        # False = the loop stopped early, as request_stop() makes it return.
        with mock.patch.object(channel_tester, 'imminent_recording_conflict',
                               return_value=None), \
             mock.patch.object(channel_tester, '_run_channel_loop', return_value=False):
            channel_tester.run_on_demand_test_job(self.t.app, job_id)
        db.session.expire_all()

        self.assertEqual('CANCELLED', db.session.get(OnDemandTestJob, job_id).status)
        self.assertEqual(SD, db.session.get(ChannelGroup, gid).locked_format_key,
                         'a cancelled run still settles on the best option it measured')


if __name__ == '__main__':
    unittest.main()
