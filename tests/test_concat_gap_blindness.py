"""Tier 2 - the timeline scan must not report "0 gaps" on a file whose gaps were erased
before it looked (dev/docs/BUGS.md 2026-08-02, dev/changelog/433).

`app/concatenator.py` joins segments with the concat demuxer plus `-fflags +genpts`, which
lays one continuous timestamp run across every join. `app/probe.py::scan_video_timeline`
counts DTS discontinuities - the exact thing that join erased - so on a multi-segment
recording `gap_count` is structurally 0 no matter how much video was lost. Recording 71 lost
~30 minutes and its detail page read `380.4s of video missing ... - 0 decode-timeline gaps
>0.25s (largest 0.00s)`, which reads as if the file were clean.

`assess_seek_damage(joined_segments=N)` now says so in the prose and in `gap_basis`. Two
things must survive that: the damage verdict (gaps are still counted and still feed
`missing_seconds`), and the honest single-source cases - a one-segment recording is renamed
rather than concatenated, and the channel tester scans an unconcatenated clip.

Fixtures are synthesized locally with ffmpeg; nothing here touches the network or a provider.
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import db  # noqa: E402
from app.database import (  # noqa: E402
    DIAGNOSTICS, Recording, RecordingEvent, RecordingSegment,
)
from app.postprocessor import _joined_segment_count, _scan_recording_timeline  # noqa: E402
from app.probe import assess_seek_damage, scan_video_timeline  # noqa: E402
from tests.support import seed  # noqa: E402
from tests.support.app import make_test_app  # noqa: E402

_HAVE_FFMPEG = bool(shutil.which('ffmpeg') and shutil.which('ffprobe'))

# The caveat clause's load-bearing words. Asserting on a fragment rather than the whole
# sentence keeps the tests about the claim being made, not the wording.
_CAVEAT = 'not measurable across'
_WITHIN = 'inside a segment'


def _ffmpeg(*args):
    subprocess.run(['ffmpeg', '-v', 'error', '-y', *args], check=True, timeout=120)


def _build_gappy(path):
    """40s of timeline with 10s-25s dropped and the surviving frames keeping their original
    timestamps, so the file really is missing 15s of content - a genuine DAMAGED verdict with
    real DTS gaps in it. No B-frames, so reordering cannot muddy the measurement."""
    _ffmpeg('-f', 'lavfi', '-i', 'testsrc=size=192x108:rate=10', '-t', '40',
            '-vf', "select='not(between(t,10,25))'", '-fps_mode', 'passthrough',
            '-c:v', 'libx264', '-preset', 'ultrafast', '-bf', '0',
            '-pix_fmt', 'yuv420p', path)


@unittest.skipUnless(_HAVE_FFMPEG, 'ffmpeg/ffprobe not installed')
class ConcatGapProseTests(unittest.TestCase):
    """Pure functions over a local fixture - no app context, no DB."""

    @classmethod
    def setUpClass(cls):
        cls._dir = tempfile.mkdtemp(prefix='concatgap-')
        cls.gappy = os.path.join(cls._dir, 'gappy.ts')
        _build_gappy(cls.gappy)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls._dir, ignore_errors=True)

    def test_single_source_summary_is_unchanged(self):
        """Characterization, not a regression guard: a one-segment recording is renamed by
        the concatenator rather than concatenated, so its gap count really is complete and
        its prose must keep making the plain claim."""
        _damaged, metrics, summary = assess_seek_damage(self.gappy)

        self.assertIn('decode-timeline gap', summary)
        self.assertNotIn(_CAVEAT, summary)
        self.assertEqual(metrics['gap_basis'], 'dts')

    def test_post_concat_summary_says_the_gaps_cannot_be_counted(self):
        """The defect: the same number, presented as a finding, on a file where finding it
        was impossible. The prose must name the joins and scope the count to what survived
        inside segments."""
        _damaged, _metrics, summary = assess_seek_damage(self.gappy, joined_segments=4)

        self.assertIn(_CAVEAT, summary)
        self.assertIn('4 concatenated segments', summary)
        self.assertIn(_WITHIN, summary,
                      'the surviving gap count was not scoped to within-segment')

    def test_post_concat_summary_reconciles_with_content_missing(self):
        """The Content missing stat (dev/changelog/432) sits on the same page and reports a
        far larger number, because the concatenated span excludes the time between segments
        entirely. Without this clause the two read as a contradiction."""
        _damaged, _metrics, summary = assess_seek_damage(self.gappy, joined_segments=4)

        self.assertIn('Content missing', summary)

    def test_gap_basis_names_which_case_the_numbers_came_from(self):
        """One flag, one meaning: 0 means 'none found' from one source and 'none found
        inside segments' from a join, so the basis field has to distinguish them."""
        self.assertEqual(assess_seek_damage(self.gappy)[1]['gap_basis'], 'dts')
        self.assertEqual(
            assess_seek_damage(self.gappy, joined_segments=2)[1]['gap_basis'],
            'dts-post-concat')

    def test_damage_verdict_is_identical_either_way(self):
        """The load-bearing one. deficit_seconds is what correctly flagged recording 71 as
        DAMAGED and routed it to a re-encode; relabelling a blind gap count must not change
        any input to that decision."""
        one_damaged, one, _s = assess_seek_damage(self.gappy)
        many_damaged, many, _s2 = assess_seek_damage(self.gappy, joined_segments=6)

        self.assertTrue(one_damaged, 'the fixture stopped being damaged')
        self.assertEqual(one_damaged, many_damaged)
        for key in ('gap_count', 'gap_seconds', 'max_gap_seconds',
                    'deficit_seconds', 'missing_seconds', 'span_seconds'):
            self.assertEqual(one[key], many[key], f'{key} moved with joined_segments')

    def test_scanner_itself_is_untouched(self):
        """scan_video_timeline measures a file and cannot know where the file came from.
        The channel tester calls it directly on an unconcatenated clip and must be
        unaffected by any of this."""
        self.assertEqual(scan_video_timeline(self.gappy)['gap_basis'], 'dts')


@unittest.skipUnless(_HAVE_FFMPEG, 'ffmpeg/ffprobe not installed')
class JoinedSegmentCountTests(unittest.TestCase):
    """The postprocessor's half: work out how many segments were joined, from rows alone -
    a successful concat has already deleted the segment files."""

    @classmethod
    def setUpClass(cls):
        cls._dir = tempfile.mkdtemp(prefix='concatgap-app-')
        cls.gappy = os.path.join(cls._dir, 'gappy.ts')
        _build_gappy(cls.gappy)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls._dir, ignore_errors=True)

    def setUp(self):
        self.t = make_test_app()
        self.dvr_dir = os.path.join(self.t._tmpdir, 'dvr')
        os.makedirs(self.dvr_dir, exist_ok=True)
        acc = seed.make_account()
        self.channel = seed.make_channel(acc, name='Concat Feed')
        now = datetime.utcnow()
        rec = seed.make_recording(
            status='CONCATENATING', name='concat gaps', channel_id=self.channel.id,
            start_time=now - timedelta(seconds=40), stop_time=now)
        self.rid = rec.id
        db.session.commit()

    def tearDown(self):
        self.t.cleanup()

    def _segments(self, *byte_counts):
        start = datetime.utcnow()
        for i, n in enumerate(byte_counts):
            db.session.add(RecordingSegment(
                recording_id=self.rid, segment_number=i,
                file_path=os.path.join(self.dvr_dir, f'seg_{i:03d}.ts'),
                started_at=start + timedelta(seconds=i),
                ended_at=start + timedelta(seconds=i + 1),
                exit_reason='STALL_KILLED', bytes_recorded=n))
        db.session.commit()

    def _ts(self):
        dest = os.path.join(self.dvr_dir, f'rec_{self.rid}.ts')
        shutil.copy2(self.gappy, dest)
        return dest

    def _diag(self):
        evs = [e for e in RecordingEvent.query.filter_by(
                   recording_id=self.rid, event_type=DIAGNOSTICS).all()
               if json.loads(e.extra_data or '{}').get('kind') == 'timeline_scan']
        self.assertEqual(len(evs), 1, 'expected exactly one timeline_scan diagnostic')
        return evs[0], json.loads(evs[0].extra_data)

    def test_empty_segments_do_not_count_as_joins(self):
        """concatenator.py skips a segment whose file is absent or zero-length, so a row
        that never wrote a byte was never joined and must not make the scan claim a
        blindness it does not have."""
        self._segments(4096, 0, None)

        self.assertEqual(_joined_segment_count(self.rid), 1)

    def test_multi_segment_recording_reports_the_blind_spot(self):
        self._segments(4096, 8192, 2048)
        _scan_recording_timeline(self.rid, self._ts())

        ev, extra = self._diag()
        self.assertIn(_CAVEAT, ev.detail)
        self.assertIn('3 concatenated segments', ev.detail)
        self.assertEqual(extra['gap_basis'], 'dts-post-concat')
        self.assertEqual(extra['joined_segments'], 3)

    def test_single_segment_recording_keeps_the_plain_claim(self):
        self._segments(4096)
        _scan_recording_timeline(self.rid, self._ts())

        ev, extra = self._diag()
        self.assertNotIn(_CAVEAT, ev.detail)
        self.assertEqual(extra['gap_basis'], 'dts')
        self.assertEqual(extra['joined_segments'], 1)

    def test_columns_and_damage_verdict_survive_the_relabelling(self):
        """The scan still has to write what it measured and still has to flag real damage -
        the fixture is missing 15s of a 40s timeline."""
        self._segments(4096, 4096, 4096)
        damaged, _metrics, _summary = _scan_recording_timeline(self.rid, self._ts())
        db.session.expire_all()

        rec = db.session.get(Recording, self.rid)
        self.assertTrue(damaged, 'a file missing 15s of content was not flagged')
        self.assertIs(rec.timeline_damaged, True)
        self.assertIsNotNone(rec.timeline_gap_count)
        self.assertGreater(rec.timeline_deficit_seconds, 10)


if __name__ == '__main__':
    unittest.main()
