"""Tier 2 - a segment abandoned by group failover after a failed restart gets closed out.

Guards dev/docs/BUGS.md 2026-08-04 "Segment duration shows nonsense for a segment abandoned by
group failover". Design and reasoning: dev/changelog/463.

The defect was an omission, not a wrong branch: when a restart produces no data and the
recording fails over to another group member, the watchdog killed the dead ffmpeg and moved on
to the next segment without ever writing ended_at/exit_reason/bytes_recorded onto the segment
row it was abandoning - only the stall-detected path closed a segment out.
templates/recording_detail.html then fell back to `now` for that segment's duration, which is
correct for a genuinely still-running segment but renders an absurd number once the recording
is long since terminal.

No network and no provider host: every child here is `sys.executable -c ...`, a local argv
with no URL in it, which tests/support/netguard.py permits. Segment files are written under
make_test_app's temp dir, never /dvr.
"""
import os
import sys
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import app.recorder as recorder  # noqa: E402
from app import db  # noqa: E402
from app.database import SEGMENT_ENDED, RecordingSegment  # noqa: E402
from tests.test_downtime_accounting import _RestartHarness, _sleeper  # noqa: E402


class AbandonedSegmentClosedOnFailoverTests(_RestartHarness):

    def test_a_failed_restart_that_fails_over_closes_its_segment_row(self):
        """The headline defect: segment_number=1 is the restart that produced no data and
        was abandoned for failover. Its row must not be left with ended_at still NULL."""
        self.state.process = _sleeper()
        with mock.patch.object(recorder, 'failover_group_member', return_value=True):
            self._run_until_event(SEGMENT_ENDED, stall_timeout=3, restart_delay=2,
                                  produces_data=False)

        db.session.expire_all()
        abandoned = RecordingSegment.query.filter_by(
            recording_id=self.rid, segment_number=1).first()
        self.assertIsNotNone(abandoned, 'the abandoned restart segment row does not exist')
        self.assertIsNotNone(
            abandoned.ended_at,
            'abandoned segment row was left open after failover - its duration will render '
            'as the gap between its real end and whenever the page happens to be viewed')
        self.assertEqual(abandoned.exit_reason, 'ERROR')
        self.assertEqual(abandoned.bytes_recorded, 0)
