"""Tier 1 pure units for the near-empty/slate segment detector's threshold math
(app/postprocessor.py::_near_empty_flags). Pure - a list of (segment_number, span_seconds,
bytes_per_sec) tuples, no DB, no I/O.

Calibrated against recording #64's real segment data (read-only sqlite query against
the development dvr.db during the health-score-fix design pass): segments 131/132
measure ~10.6%/12.1% of the recording's average bitrate (segments 130/133 are the normal
~104% ones), so a 20% ratio threshold has a wide margin on both sides.

Also carries one Tier 2 (DB-backed) regression class guarding
_detect_near_empty_segments' early-return branch (dev/docs/BUGS.md 2026-08-17).
"""
import json
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.postprocessor import _near_empty_flags, _detect_near_empty_segments  # noqa: E402
from app import db  # noqa: E402
from app.database import RecordingEvent, DIAGNOSTICS  # noqa: E402
from tests.support.app import make_test_app  # noqa: E402
from tests.support import seed  # noqa: E402

_RATIO = 0.20
_MIN_SPAN = 30

# (segment_number, span_seconds, bytes_per_sec) - recording #64's real numbers.
_RECORDING_64_SPANS = [
    (130, 6417.8, 203446.7),
    (131, 89.3, 20544.3),
    (132, 615.2, 23525.2),
    (133, 8462.7, 201737.0),
]


class NearEmptyFlagsTests(unittest.TestCase):
    def test_recording_64_flags_the_two_known_slate_segments(self):
        flagged, avg_bps = _near_empty_flags(_RECORDING_64_SPANS, _RATIO, _MIN_SPAN)
        self.assertEqual(flagged, {131, 132})
        self.assertAlmostEqual(avg_bps, 194368.0, delta=100)

    def test_normal_segment_not_flagged(self):
        # A single segment at the recording's own average bitrate is never flagged.
        spans = [(1, 3600, 200000.0), (2, 3600, 200000.0)]
        flagged, _avg = _near_empty_flags(spans, _RATIO, _MIN_SPAN)
        self.assertEqual(flagged, set())

    def test_short_low_bitrate_segment_below_min_span_not_flagged(self):
        # Below the min-span floor, even a near-zero bitrate segment is noise, not a flag.
        spans = [(1, 3600, 200000.0), (2, 10, 100.0)]
        flagged, _avg = _near_empty_flags(spans, _RATIO, _MIN_SPAN)
        self.assertEqual(flagged, set())

    def test_low_bitrate_segment_above_min_span_is_flagged(self):
        spans = [(1, 3600, 200000.0), (2, 60, 100.0)]
        flagged, _avg = _near_empty_flags(spans, _RATIO, _MIN_SPAN)
        self.assertEqual(flagged, {2})

    def test_empty_input_returns_no_flags(self):
        flagged, avg_bps = _near_empty_flags([], _RATIO, _MIN_SPAN)
        self.assertEqual(flagged, set())
        self.assertEqual(avg_bps, 0.0)


class NoSpansEventCommitTests(unittest.TestCase):
    """Guards dev/docs/BUGS.md 2026-08-17: the early-return branch of
    _detect_near_empty_segments (no data-bearing segments to evaluate) used to insert its
    DIAGNOSTICS event without committing it, so the pending insert only survived if some
    unrelated commit elsewhere in the session happened to sweep it up - and vanished
    silently if that other commit rolled back instead. The event now has its own
    @retry_on_locked commit, matching the normal-path branch's _commit_near_empty."""

    def setUp(self):
        self.t = make_test_app()
        acc = seed.make_account()
        channel = seed.make_channel(acc)
        # No with_segment=True, so RecordingSegment.query finds nothing for this
        # recording - _detect_near_empty_segments sees an empty `spans` list and takes
        # the early-return branch this test is guarding.
        self.rec = seed.make_recording(status='CONCATENATING', channel_id=channel.id)
        db.session.commit()

    def tearDown(self):
        self.t.cleanup()

    def test_no_data_bearing_segments_event_survives_an_unrelated_rollback(self):
        _detect_near_empty_segments(self.rec.id)
        # Stand in for "some unrelated commit later in the same request hit
        # database-is-locked and rolled back" - a rollback here must not be able to take
        # this event with it, since it now commits on its own.
        db.session.rollback()

        events = RecordingEvent.query.filter_by(
            recording_id=self.rec.id, event_type=DIAGNOSTICS).all()
        kinds = [json.loads(e.extra_data or '{}').get('kind') for e in events]
        self.assertIn('near_empty_scan', kinds,
                       'the no-data-bearing-segments event did not survive an unrelated '
                       'rollback - its insert is not committed on its own')


if __name__ == '__main__':
    unittest.main(verbosity=2)
