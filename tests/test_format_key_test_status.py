"""Only a COMPLETED health check measures a channel's format, and a row says which check it read.

dev/docs/BUGS.md 2026-09-14. Two stacked defects, found on group 5's member 4137:

  * A FAILED check that connected to the provider's offline black placeholder measured
    `1920x1080 @ 30` off that clip, and `format_key()` honored it - so the lock filtered a
    healthy 1080p60 member out of every recording. Its sibling feed failed the same way with
    no usable probe output, so `format_key()` returned None and it stayed eligible: two
    failures on the same placeholder, opposite eligibility.
  * The member row rendered the attached check's result while the mismatch pill judged the
    channel's newest check, with nothing on screen saying so - the row read
    "1920x1080 @ 60" beside a Format mismatch pill whose tooltip named the same format on
    both sides of its own sentence.

Runs against a throwaway temp SQLite DB - never the live dvr.db.
  python3 -m unittest tests.test_format_key_test_status
"""
import os
import sys
import unittest
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tests.support import make_test_app  # noqa: E402
from tests.support.seed import make_account, make_channel, make_group  # noqa: E402
from app import db  # noqa: E402
from app.database import ChannelTest  # noqa: E402
from app.channel_groups import format_key, format_eligible_members  # noqa: E402
from app.routes.channel_groups import group_detail_rows  # noqa: E402

HD = ('1920x1080', 60)


class FormatKeyStatusTests(unittest.TestCase):
    """The gate itself, with no database in the way."""

    class T:
        def __init__(self, status, resolution='1920x1080', fps=59.94):
            self.status = status
            self.resolution = resolution
            self.fps = fps

    def test_completed_test_measures_a_format(self):
        self.assertEqual(format_key(self.T('COMPLETED')), HD)

    def test_failed_test_measures_nothing(self):
        """The black-placeholder case: real numbers, off a clip that is not the feed."""
        self.assertIsNone(format_key(self.T('FAILED', '1920x1080', 30.0)))

    def test_cancelled_test_measures_nothing(self):
        self.assertIsNone(format_key(self.T('CANCELLED')))

    def test_no_test_is_still_unknown(self):
        self.assertIsNone(format_key(None))


class FailedTestDoesNotFilterAMemberTests(unittest.TestCase):
    """The consequence that cost a recording source: unknown is not proven-different."""

    def setUp(self):
        self.t = make_test_app()
        self.acct = make_account()
        self.ch = make_channel(self.acct, name='CW feed')
        # A second member that matches the lock, so a filtered `self.ch` leaves a survivor.
        # Without one the lock filters everybody and 15.2's zero-survivor override bypasses
        # it, which would make every assertion here pass for the wrong reason.
        self.keeper = make_channel(self.acct, name='CW backup')
        self.grp = make_group(name='CW', members=[self.ch, self.keeper],
                              format_resolution=HD[0], format_fps=HD[1],
                              format_strategy='manual')
        db.session.add(ChannelTest(channel_id=self.keeper.id, status='COMPLETED',
                                   resolution=HD[0], fps=59.94,
                                   test_started_at=datetime.utcnow()))
        db.session.commit()

    def tearDown(self):
        self.t.cleanup()

    def _test_row(self, status, resolution, fps, minutes_ago=0):
        row = ChannelTest(
            channel_id=self.ch.id, status=status, resolution=resolution, fps=fps,
            test_started_at=datetime.utcnow() - timedelta(minutes=minutes_ago))
        db.session.add(row)
        db.session.commit()
        return row

    def _filtered_ids(self):
        latest = {ch.id: (ChannelTest.query.filter_by(channel_id=ch.id)
                          .order_by(ChannelTest.id.desc()).first())
                  for ch in (self.ch, self.keeper)}
        sel = format_eligible_members(self.grp, [self.ch, self.keeper], latest)
        self.assertFalse(sel.override, 'the override would mask what this asserts')
        return {c.id for c in sel.filtered}

    def test_a_failed_check_carrying_another_format_does_not_filter_the_member(self):
        self._test_row('COMPLETED', '1920x1080', 59.94, minutes_ago=10)
        self._test_row('FAILED', '1920x1080', 30.0)
        self.assertEqual(self._filtered_ids(), set())

    def test_a_completed_check_carrying_another_format_still_filters(self):
        """The gate narrows what counts as a measurement; it does not disarm the lock."""
        self._test_row('COMPLETED', '1280x720', 30.0)
        self.assertEqual(self._filtered_ids(), {self.ch.id})


class FormatSourceDisclosureTests(unittest.TestCase):
    """A member row never states one format while its verdict was reached on another
    without naming the check that measured it."""

    def setUp(self):
        self.t = make_test_app()
        self.acct = make_account()
        self.ch = make_channel(self.acct, name='CW feed')
        # As above: a lock that filters every member is bypassed, so the group needs a
        # member on the locked format for `format_blocked` to mean anything.
        self.keeper = make_channel(self.acct, name='CW backup')
        self.grp = make_group(name='CW', members=[self.ch, self.keeper],
                              format_resolution=HD[0], format_fps=HD[1],
                              format_strategy='manual')
        self.job = self.grp.check
        self.job.status = 'SCHEDULED'
        # A second check on another group - a group carries exactly one of its own
        # (dev/changelog/1077), and the disclosure is about a test ANOTHER job produced.
        other_grp = make_group(name='TV Guide Channels', members=[self.ch],
                               job_name='TV Guide Channels', job={'status': 'SCHEDULED'})
        self.other = other_grp.check
        db.session.commit()

    def tearDown(self):
        self.t.cleanup()

    def _seed(self, job, status, resolution, fps, minutes_ago=0, channel=None):
        db.session.add(ChannelTest(
            channel_id=(channel or self.ch).id, job_id=job.id, status=status,
            resolution=resolution, fps=fps,
            test_started_at=datetime.utcnow() - timedelta(minutes=minutes_ago)))
        db.session.commit()

    def _row(self):
        rows = group_detail_rows(self.grp, self.job)['rows']
        return next(r for r in rows if r['channel_id'] == self.ch.id)

    def _seed_keeper(self):
        for job in (self.job, self.other):
            self._seed(job, 'COMPLETED', HD[0], 59.94, minutes_ago=5, channel=self.keeper)

    def test_agreeing_checks_disclose_nothing(self):
        self._seed_keeper()
        self._seed(self.job, 'COMPLETED', '1920x1080', 59.94, minutes_ago=10)
        self._seed(self.other, 'COMPLETED', '1920x1080', 59.94)
        self.assertIsNone(self._row()['format_source'])

    def test_a_newer_check_measuring_another_format_is_named_on_the_row(self):
        self._seed_keeper()
        self._seed(self.job, 'COMPLETED', '1920x1080', 59.94, minutes_ago=10)
        self._seed(self.other, 'COMPLETED', '1280x720', 30.0)
        row = self._row()
        # The row renders the attached check's 1080p60 and is blocked on the other one's
        # 720p30, so the row has to carry both or it contradicts itself.
        self.assertEqual(row['last_test']['resolution'], '1920x1080')
        self.assertTrue(row['format_blocked'])
        self.assertEqual(row['format_source']['label'], '1280x720 @ 30')
        self.assertEqual(row['format_source']['job_name'], 'TV Guide Channels')
        self.assertEqual(row['format_source']['status'], 'PASS')

    def test_a_newer_failed_check_is_named_but_blocks_nobody(self):
        """4137 exactly: the newer check measured numbers off a placeholder, so it
        carries no format, filters nothing, and is still disclosed rather than hidden."""
        self._seed_keeper()
        self._seed(self.job, 'COMPLETED', '1920x1080', 59.94, minutes_ago=10)
        self._seed(self.other, 'FAILED', '1920x1080', 30.0)
        row = self._row()
        self.assertFalse(row['format_blocked'])
        self.assertIsNone(row['format_source']['label'])
        self.assertEqual(row['format_source']['measured'], '1920x1080 @ 30')
        self.assertEqual(row['format_source']['status'], 'FAIL')

    def test_a_newer_check_that_measured_nothing_discloses_nothing(self):
        """A check that failed before ffprobe got an answer has not disagreed with
        anybody, so it is not what this surface says - it would be noise on every row
        whose newest check happened to fail."""
        self._seed_keeper()
        self._seed(self.job, 'COMPLETED', '1920x1080', 59.94, minutes_ago=10)
        self._seed(self.other, 'FAILED', None, None)
        self.assertIsNone(self._row()['format_source'])

    def test_a_pre_check_is_named_as_one_rather_than_as_a_check(self):
        """A test with no job is a recording's pre-check; "another check" would send the
        user looking for a scheduled check that does not exist."""
        self._seed_keeper()
        self._seed(self.job, 'COMPLETED', '1920x1080', 59.94, minutes_ago=10)
        db.session.add(ChannelTest(
            channel_id=self.ch.id, job_id=None, status='COMPLETED',
            resolution='1280x720', fps=30.0, test_started_at=datetime.utcnow()))
        db.session.commit()
        src = self._row()['format_source']
        self.assertTrue(src['pre_check'])
        self.assertIsNone(src['job_name'])


if __name__ == '__main__':
    unittest.main()
