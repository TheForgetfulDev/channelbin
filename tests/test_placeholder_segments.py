"""Tier 2 - a provider's "channel offline" placeholder clip is detected and discarded
instead of being recorded as program content.

Guards dev/docs/BUGS.md 2026-09-14 "A provider placeholder clip is joined into the final
file as if it were the channel". Design, evidence and the measured ratios:
dev/changelog/957.

The defect: when a channel is down, the upstream serves a finite black clip instead of a
live stream. ffmpeg drains the whole thing in about five seconds and exits 0, the watchdog
reads that as an ordinary "process exited, restart", and `_run_concatenation` joins any
segment whose file is non-empty - so every copy landed in the final file. Recording 17
(2026-09-12) carries 60 minutes of black mid-race and recording 19 two hours, and the same
clip produced their false DAMAGED verdicts and their unreadable "222% of expected frames".

No network anywhere: the placeholder stand-in is generated locally with ffmpeg from
`lavfi`, every child process is a local argv, and every file is written under
make_test_app's temp dir (tests/support/netguard.py would refuse otherwise).
"""
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from datetime import datetime, timedelta
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import app.concatenator as concatmod  # noqa: E402
import app.config as cfgmod  # noqa: E402
import app.recorder as recorder  # noqa: E402
from app import db  # noqa: E402
from app.database import (  # noqa: E402
    SEGMENT_DISCARDED, SEGMENT_EXCLUDED_PLACEHOLDER, CHANNEL_PLACEHOLDER_HEALTH_OBSERVATION,
    Channel, ChannelEvent, Recording, RecordingEvent, RecordingSegment,
)
from app.recorder import RecordingState, recording_format_pin  # noqa: E402
from app.watchdog import (  # noqa: E402
    WatchdogThread, classify_placeholder_segment, member_has_delivered,
)
from tests.support import seed  # noqa: E402
from tests.support.app import make_test_app  # noqa: E402

HAVE_FFMPEG = bool(shutil.which('ffmpeg') and shutil.which('ffprobe'))

#: Seconds of content in the synthetic placeholder. Long enough that the ratio stays far
#: above the default factor of 10 even if the watchdog takes several seconds to notice the
#: exited process, and small/low-resolution enough to generate in about 1.5s.
CLIP_SECONDS = 120


def _make_placeholder_clip(path):
    """A finite MPEG-TS standing in for the provider's offline clip: the shape that matters
    is a real container whose duration far exceeds the wall clock it was captured in, which
    is what ffprobe reads back."""
    subprocess.run(
        ['ffmpeg', '-hide_banner', '-loglevel', 'error',
         '-f', 'lavfi', '-i', 'color=black:s=320x240:r=30',
         '-t', str(CLIP_SECONDS), '-c:v', 'libx264', '-b:v', '60k', '-preset', 'ultrafast',
         '-f', 'mpegts', path, '-y'],
        check=True, timeout=180, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


class ClassifierTests(unittest.TestCase):
    """The pure rule, at its boundaries. Both conditions are load-bearing and the test says
    so by failing each one on its own."""

    def test_measured_placeholder_is_classified(self):
        # The real shape: 600.046s of content in 5.2s of wall clock, exit 0.
        self.assertTrue(classify_placeholder_segment(600.046, 5.2, 0, True, 10))

    def test_ratio_alone_is_not_enough_without_a_clean_eof(self):
        """The measured counterexample: a buffer replay prepends a roughly constant 13-29s
        at each connect, which on a 5s segment reaches ~6x - and on a 2s one clears 10x. It
        is never an exit 0, because the feed was still running when the watchdog killed it,
        which is the whole reason the EOF condition exists."""
        self.assertFalse(classify_placeholder_segment(29.0, 2.0, -15, True, 10))
        self.assertFalse(classify_placeholder_segment(29.0, 2.0, 1, True, 10))

    def test_clean_eof_alone_is_not_enough_without_the_ratio(self):
        """A short real segment that happened to end cleanly is kept."""
        self.assertFalse(classify_placeholder_segment(5.0, 5.0, 0, True, 10))

    def test_fastest_real_delivery_is_kept(self):
        """Recording 19 segment 19 delivered 8h38m in 2h18m - 3.76x, the fastest sustained
        real delivery ever measured here - and must never be discarded."""
        self.assertFalse(classify_placeholder_segment(31122.0, 8275.0, 0, True, 10))

    def test_boundary_is_strictly_greater_than_the_factor(self):
        self.assertFalse(classify_placeholder_segment(100.0, 10.0, 0, True, 10))
        self.assertTrue(classify_placeholder_segment(100.01, 10.0, 0, True, 10))

    def test_a_killed_process_is_never_a_placeholder(self):
        """proc_exited False means WE ended it, which says nothing about the feed."""
        self.assertFalse(classify_placeholder_segment(600.0, 5.0, 0, False, 10))

    def test_unmeasurable_content_fails_open(self):
        """A probe that read nothing keeps the segment - discarding real capture on a failed
        diagnostic is the one outcome worse than joining a placeholder."""
        self.assertFalse(classify_placeholder_segment(None, 5.0, 0, True, 10))
        self.assertFalse(classify_placeholder_segment(600.0, None, 0, True, 10))

    def test_zero_factor_disables_the_detector(self):
        self.assertFalse(classify_placeholder_segment(600.0, 5.0, 0, True, 0))


class _PlaceholderDbCase(unittest.TestCase):
    """One recording whose segment rows can be shaped per test."""

    def setUp(self):
        self.t = make_test_app()
        now = datetime.utcnow()
        self.now = now
        self.account = seed.make_account()
        self.ch_a = seed.make_channel(self.account, stream_id='1', name='Member A')
        self.ch_b = seed.make_channel(self.account, stream_id='2', name='Member B')
        db.session.commit()
        self.rec = seed.make_recording(status='IN_PROGRESS', name='ph',
                                       channel_id=self.ch_a.id, started_at=now,
                                       start_time=now, stop_time=now + timedelta(hours=1))
        db.session.commit()

    def tearDown(self):
        self.t.cleanup()

    def _seg(self, number, *, channel_id=None, start_offset=0, span=10, bytes_recorded=5000,
             excluded=None, fps=None, resolution=None):
        started = self.now + timedelta(seconds=start_offset)
        seg = RecordingSegment(
            recording_id=self.rec.id, segment_number=number,
            channel_id=channel_id if channel_id is not None else self.ch_a.id,
            file_path=os.path.join(self.t._tmpdir, f'seg_{number}.ts'),
            started_at=started, ended_at=started + timedelta(seconds=span),
            bytes_recorded=bytes_recorded, excluded_reason=excluded,
            probe_fps=fps, probe_resolution=resolution, exit_reason='PROCESS_EXITED')
        db.session.add(seg)
        db.session.commit()
        return seg


class CoverageTests(_PlaceholderDbCase):
    """An excluded segment covers nothing, so the capture gap grows by its wall clock -
    the direct answer to "is recording 19's 61s capture gap right" (it was not: twelve
    placeholder windows sat inside it, each counted as coverage)."""

    def test_excluded_segment_does_not_cover_its_window(self):
        self._seg(1, start_offset=0, span=100)
        self._seg(2, start_offset=100, span=50, excluded=SEGMENT_EXCLUDED_PLACEHOLDER)
        db.session.expire_all()
        rec = db.session.get(Recording, self.rec.id)
        self.assertAlmostEqual(rec.covered_capture_seconds, 100.0, places=1)

    def test_capture_gap_grows_by_the_discarded_window(self):
        self._seg(1, start_offset=0, span=100)
        self._seg(2, start_offset=100, span=50, excluded=SEGMENT_EXCLUDED_PLACEHOLDER)
        db.session.expire_all()
        rec = db.session.get(Recording, self.rec.id)
        rec.status = 'COMPLETED'
        rec.completed_at = self.now + timedelta(seconds=150)
        db.session.commit()
        # The window is an hour; 100s of it was covered, so everything else is gap -
        # including the 50s the placeholder segment used to "cover".
        self.assertAlmostEqual(rec.capture_gap_seconds, 3600.0 - 100.0, places=1)

    def test_captured_duration_excludes_discarded_segments(self):
        self._seg(1, start_offset=0, span=100)
        self._seg(2, start_offset=100, span=50, excluded=SEGMENT_EXCLUDED_PLACEHOLDER)
        db.session.expire_all()
        rec = db.session.get(Recording, self.rec.id)
        self.assertAlmostEqual(rec.captured_duration_seconds, 100.0, places=1)


class FormatPinTests(_PlaceholderDbCase):
    """A placeholder is 1080p30. Letting one set the recording's format pin locks a 59.94
    fps recording to 30 fps and filters out every real member for the rest of the run."""

    def test_discarded_segment_one_does_not_set_the_pin(self):
        self._seg(1, excluded=SEGMENT_EXCLUDED_PLACEHOLDER,
                  fps=30.0, resolution='1920x1080')
        self._seg(2, start_offset=20, fps=59.94, resolution='1920x1080')
        db.session.expire_all()
        pin = recording_format_pin(self.rec.id)
        # segment_format_key rounds the rate, so the pin reads 60 rather than 59.94 - what
        # matters is that it is the REAL segment's rate and not the placeholder's 30.
        self.assertEqual(pin, ('1920x1080', 60))


class MemberDeliveredTests(_PlaceholderDbCase):
    """Which of the two failover rules a placeholder gets, decided from the rows."""

    def test_first_tune_on_a_member_has_delivered_nothing(self):
        self.assertFalse(member_has_delivered(self.rec.id, self.ch_b.id, 1))

    def test_a_member_that_produced_a_kept_segment_has_delivered(self):
        self._seg(1, channel_id=self.ch_b.id)
        self.assertTrue(member_has_delivered(self.rec.id, self.ch_b.id, 2))

    def test_a_members_own_earlier_placeholder_does_not_count_as_delivery(self):
        """Otherwise the second clip from the same dead feed would buy it three strikes."""
        self._seg(1, channel_id=self.ch_b.id, excluded=SEGMENT_EXCLUDED_PLACEHOLDER)
        self.assertFalse(member_has_delivered(self.rec.id, self.ch_b.id, 2))

    def test_another_members_delivery_does_not_count(self):
        self._seg(1, channel_id=self.ch_a.id)
        self.assertFalse(member_has_delivered(self.rec.id, self.ch_b.id, 2))

    def test_a_later_segment_does_not_count(self):
        """Only what the member had already shown BEFORE this segment can earn it strikes."""
        self._seg(5, channel_id=self.ch_b.id)
        self.assertFalse(member_has_delivered(self.rec.id, self.ch_b.id, 2))


class HealthObservationTests(_PlaceholderDbCase):
    """The member that served the clip takes the score hit, and the hit replays."""

    def test_observation_lands_on_the_channel_and_is_replayable(self):
        from app.health_score import apply_placeholder_health_observation
        from app.health_recompute import SOURCE_PLACEHOLDER, observation_ledger

        ch = db.session.get(Channel, self.ch_b.id)
        ch.health_score = 100.0
        ch.health_score_sample_count = 5
        ch.health_score_updated_at = self.now
        db.session.commit()

        apply_placeholder_health_observation(self.t.app, self.ch_b.id, self.rec.id, 3)

        db.session.expire_all()
        ch = db.session.get(Channel, self.ch_b.id)
        self.assertLess(ch.health_score, 100.0)

        evt = ChannelEvent.query.filter_by(
            channel_id=self.ch_b.id,
            event_type=CHANNEL_PLACEHOLDER_HEALTH_OBSERVATION).one()
        self.assertIn('placeholder', evt.detail.lower())

        ledger = observation_ledger(self.ch_b.id, cfgmod.load_config())
        kinds = [o.kind for o in ledger]
        self.assertIn(SOURCE_PLACEHOLDER, kinds)

    def test_a_recording_with_no_channel_is_a_no_op(self):
        from app.health_score import apply_placeholder_health_observation
        apply_placeholder_health_observation(self.t.app, None, self.rec.id, 1)
        self.assertEqual(ChannelEvent.query.filter_by(
            event_type=CHANNEL_PLACEHOLDER_HEALTH_OBSERVATION).count(), 0)


@unittest.skipUnless(HAVE_FFMPEG, 'ffmpeg/ffprobe not on PATH')
class ConcatExclusionTests(_PlaceholderDbCase):
    """The join list and the delete list are different lists, and a discarded segment is on
    exactly one of them."""

    def test_discarded_segment_is_not_joinable(self):
        kept = self._seg(1)
        dropped = self._seg(2, start_offset=20, excluded=SEGMENT_EXCLUDED_PLACEHOLDER)
        for seg in (kept, dropped):
            with open(seg.file_path, 'wb') as fh:
                fh.write(b'\x47' * 4096)
        joinable = concatmod.joinable_segments([kept, dropped])
        self.assertEqual([s.segment_number for s in joinable], [1])

    def test_discarded_file_is_still_deleted_by_the_concat(self):
        """Teardown releases everything the create path acquired - a discarded placeholder
        must not sit on /dvr forever just because it was not joined."""
        kept = self._seg(1)
        dropped = self._seg(2, start_offset=20, excluded=SEGMENT_EXCLUDED_PLACEHOLDER)
        for seg, seconds in ((kept, 3), (dropped, 3)):
            _make_short_clip(seg.file_path, seconds)
        db.session.expire_all()
        rec = db.session.get(Recording, self.rec.id)
        rec.status = 'CONCATENATING'
        db.session.commit()

        with mock.patch.object(cfgmod, 'load_config',
                               return_value=self._concat_cfg()), \
             mock.patch('app.postprocessor.do_postprocess'), \
             mock.patch('app.recorder.persist_final_thumbnail'):
            concatmod._run_concatenation(self.t.app, self.rec.id, reason='test')

        self.assertFalse(os.path.exists(dropped.file_path),
                         'the discarded segment file was left on disk')

    def test_rollup_counts_the_discarded_segments(self):
        self._seg(1)
        dropped = self._seg(2, start_offset=20, excluded=SEGMENT_EXCLUDED_PLACEHOLDER)
        dropped.content_duration_seconds = 600.046
        db.session.commit()
        concatmod._record_discarded_rollup(
            self.rec.id, [db.session.get(RecordingSegment, dropped.id)])
        db.session.expire_all()
        rec = db.session.get(Recording, self.rec.id)
        self.assertEqual(rec.discarded_segment_count, 1)
        self.assertAlmostEqual(rec.discarded_seconds, 600.046, places=2)

    def test_rollup_records_zero_rather_than_null_when_nothing_was_discarded(self):
        """0 means "checked and kept everything"; NULL means the recording never got here.
        Two different facts, and the detail page reads them differently."""
        concatmod._record_discarded_rollup(self.rec.id, [])
        db.session.expire_all()
        rec = db.session.get(Recording, self.rec.id)
        self.assertEqual(rec.discarded_segment_count, 0)

    def _concat_cfg(self):
        return cfgmod._deep_merge(cfgmod.load_config(), {
            'recording': {'dvr_output_dir': self.t._tmpdir,
                          'post_process': {'enabled': False},
                          'move_on_complete': {'enabled': False}},
        })


def _make_short_clip(path, seconds):
    subprocess.run(
        ['ffmpeg', '-hide_banner', '-loglevel', 'error',
         '-f', 'lavfi', '-i', 'color=black:s=160x120:r=10',
         '-t', str(seconds), '-c:v', 'libx264', '-b:v', '40k', '-preset', 'ultrafast',
         '-f', 'mpegts', path, '-y'],
        check=True, timeout=120, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


@unittest.skipUnless(HAVE_FFMPEG, 'ffmpeg/ffprobe not on PATH')
class WatchdogDiscardTests(unittest.TestCase):
    """The whole path, driven by the real WatchdogThread against a real clip on disk: an
    exited capture whose file holds two minutes of content after a couple of seconds of wall
    clock is discarded, and says so."""

    @classmethod
    def setUpClass(cls):
        cls._clip_dir = tempfile.mkdtemp(prefix='dvr_ph_')
        cls._clip = os.path.join(cls._clip_dir, 'placeholder.ts')
        _make_placeholder_clip(cls._clip)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls._clip_dir, ignore_errors=True)

    def setUp(self):
        self.t = make_test_app()
        self.wd = None
        self.launched = []
        self.state = RecordingState(current_segment_num=0)
        now = datetime.utcnow()
        rec = seed.make_recording(status='IN_PROGRESS', name='wdph', started_at=now,
                                  start_time=now, stop_time=now + timedelta(hours=1))
        self.rid = rec.id
        self.seg_path = os.path.join(self.t._tmpdir, f'rec_{self.rid}_seg_000.ts')
        shutil.copyfile(self._clip, self.seg_path)
        db.session.add(RecordingSegment(recording_id=self.rid, segment_number=0,
                                        file_path=self.seg_path, started_at=now))
        db.session.commit()
        with recorder._lock:
            recorder._active[self.rid] = self.state

    def tearDown(self):
        self.state.stop_event.set()
        if self.wd is not None:
            self.wd.join(timeout=15)
        proc = self.state.process
        if isinstance(proc, subprocess.Popen) and proc.poll() is None:
            proc.kill()
            proc.wait(timeout=10)
        with recorder._lock:
            recorder._active.pop(self.rid, None)
        self.t.cleanup()

    def _exited_cleanly(self):
        """An ffmpeg stand-in that has already exited 0 - what draining a finite clip looks
        like from the watchdog's side."""
        path, fh = recorder._open_segment_stderr_spool(self.t.app, self.rid, 0)
        self.state.stderr_path, self.state.stderr_fh = path, fh
        proc = subprocess.Popen(
            [sys.executable, '-c',
             'import sys; sys.stderr.write("Error during demuxing: Input/output error\\n"); '
             'sys.stderr.flush(); sys.exit(0)'],
            stdout=subprocess.DEVNULL, stderr=(fh or subprocess.DEVNULL))
        proc.wait(timeout=30)
        self.state.process = proc
        return proc

    def _cfg(self, ratio=10):
        return cfgmod._deep_merge(cfgmod.load_config(), {'watchdog': {
            'poll_interval_seconds': 1,
            'stall_timeout_seconds': 5,
            'restart_delay_seconds': 0,
            'max_consecutive_failures': 99,
            'early_fail_abort_count': 99,
            'placeholder_content_ratio': ratio,
        }})

    def _run(self, ratio=10, timeout=45):
        def _stub_launch(app, recording_id, seg_num):
            self.launched.append(seg_num)
            self.state.stop_event.set()

        self._exited_cleanly()
        with mock.patch.object(cfgmod, 'load_config', return_value=self._cfg(ratio)), \
             mock.patch.object(recorder, '_launch_segment', _stub_launch):
            self.wd = WatchdogThread(self.rid, self.state, self.t.app)
            self.wd.start()
            deadline = time.monotonic() + timeout
            while time.monotonic() < deadline:
                db.session.expire_all()
                seg = RecordingSegment.query.filter_by(
                    recording_id=self.rid, segment_number=0).first()
                if seg.ended_at is not None:
                    break
                time.sleep(0.2)
            self.state.stop_event.set()
            self.wd.join(timeout=15)
        db.session.expire_all()
        return RecordingSegment.query.filter_by(
            recording_id=self.rid, segment_number=0).first()

    def test_placeholder_segment_is_marked_excluded(self):
        seg = self._run()
        self.assertEqual(seg.excluded_reason, SEGMENT_EXCLUDED_PLACEHOLDER)

    def test_exit_reason_still_says_how_the_segment_ended(self):
        """One flag, one meaning: the exclusion is its own column and PROCESS_EXITED stays
        true beside it."""
        seg = self._run()
        self.assertEqual(seg.exit_reason, 'PROCESS_EXITED')

    def test_content_duration_is_stored_on_the_row(self):
        """The concat's measuring pass only ever sees the segments it is about to join, so
        a discarded one has to carry its own measurement."""
        seg = self._run()
        self.assertIsNotNone(seg.content_duration_seconds)
        self.assertAlmostEqual(seg.content_duration_seconds, CLIP_SECONDS, delta=2)

    def test_the_discard_is_announced_on_the_recording(self):
        """Failure paths must be observable - a discard nobody can see is the original
        defect, not a fix for it."""
        self._run()
        evt = RecordingEvent.query.filter_by(
            recording_id=self.rid, event_type=SEGMENT_DISCARDED).one()
        self.assertIn('placeholder', evt.detail.lower())
        self.assertIn('real time', evt.detail)

    def test_a_disabled_detector_keeps_the_segment(self):
        seg = self._run(ratio=0)
        self.assertIsNone(seg.excluded_reason)
        self.assertEqual(RecordingEvent.query.filter_by(
            recording_id=self.rid, event_type=SEGMENT_DISCARDED).count(), 0)


if __name__ == '__main__':
    unittest.main()
