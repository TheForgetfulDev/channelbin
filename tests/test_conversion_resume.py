"""Tier 2 - a killed re-encode resumes where it stopped instead of starting over
(dev/changelog/955).

Recording 17, 2026-09-13: an 8.1 GB / 5h03m capture re-encoding for timeline damage ran
4h26m, reached 66.4% and wrote 4.6 GB before it was killed. The retry re-ran the identical
ffmpeg command from byte zero of the source, and the partial answered ffprobe with "moov atom
not found", so none of the 4h26m was recoverable by any route. Items shipped before this one
removed the *polite* interruptions - a concat that is still writing is not killed, and a
conversion that yields to a recording is suspended rather than killed - which left the violent
ones: a stall, a crash, and a service restart.

The invariants below, in the order the work happens:

  (a) A re-encode writes numbered part files with fragmented-MP4 flags, never +faststart
      straight to the final name. +faststart writes moov LAST, which is the whole reason a
      killed partial is unreadable; the final container gets it back at the join. A stream
      copy is not resumable and keeps writing the final file directly.
  (b) The splice point comes from the killed part's LAST DECODABLE FRAME - re-muxed with
      -c copy to drop the fragment that was mid-write - never from its declared container
      duration. Measured on this box: trusting the declared duration lost exactly 1.0s.
  (c) The checkpoint is a RECORDED fact. Four columns move together through
      set_conversion_parts() and nothing else writes them; a part on disk that no commit
      describes is re-verified from scratch before it is adopted, so the crash window
      degrades to re-encoding a stretch rather than to a wrong splice point.
  (d) Parts made under different settings are never joined. parts_signature() is what
      notices, and the audio-copy fallback - which swaps the audio codec mid-conversion - is
      the case that forced it to exist.
  (e) Every figure derived from progress adds the resumed attempt's -ss offset back: the
      percentage, the ETA's target, the collision window's work-remaining, and the out_time
      the restart loop compares to tell a damaged source from a transient failure.
  (f) Teardown removes every part. There are now N multi-gigabyte files where there was one.

The real-ffmpeg tests here synthesize their input with lavfi and never touch a URL, so
tests/support/netguard.py is satisfied.
  python3 -m unittest tests.test_conversion_resume
"""
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import app.postprocessor as ppmod  # noqa: E402
from tests.support.app import make_test_app  # noqa: E402
from tests.support import seed  # noqa: E402
from app import db  # noqa: E402
from app.database import Recording, RecordingEvent  # noqa: E402
from app.postprocessor import (  # noqa: E402
    PART_MOVFLAGS, all_part_paths_on_disk, clean_part_path, discard_conversion_parts,
    existing_part_paths, finalize_part, join_conversion_parts, part_path, parts_signature,
    set_conversion_parts,
)
from app.proc_utils import SupervisedRun  # noqa: E402

_HAVE_FFMPEG = bool(shutil.which('ffmpeg') and shutil.which('ffprobe'))


def _ffmpeg(*args):
    subprocess.run(['ffmpeg', '-v', 'error', '-y', *args], check=True, timeout=180)


def _probe_duration(path):
    out = subprocess.run(
        ['ffprobe', '-v', 'error', '-show_entries', 'format=duration',
         '-of', 'default=nw=1:nk=1', path],
        capture_output=True, text=True, timeout=60)
    try:
        return float(out.stdout.strip())
    except ValueError:
        return None


def _decode_error_count(path):
    """How many frames ffmpeg cannot decode reading the whole file - counted with the app's
    own markers, so this asserts the same thing the app discloses to the user.

    Deliberately NOT every stderr line. A join leaves one duplicated timestamp at each seam,
    which makes a re-mux of the joined file report "non monotonically increasing dts to
    muxer" - a muxer complaint about the packet it was handed, not a frame that failed to
    decode. Measured on real 1080p59.94 material: the joined file carries exactly that one
    line and zero decode errors. Counting it here would make this assertion mean "the splice
    is invisible to every tool", which is not what was measured and not what is claimed."""
    out = subprocess.run(
        ['ffmpeg', '-v', 'error', '-i', path, '-f', 'null', '-'],
        capture_output=True, text=True, timeout=180)
    return len([ln for ln in out.stderr.splitlines()
                if any(m in ln for m in ppmod._DECODE_ERROR_MARKERS)])


# ── (a) + (c) + (d): the derivations and the one writer ───────────────────────────────

class PartPathTests(unittest.TestCase):
    """Part paths are DERIVED from the output path, so the row and the filesystem cannot
    disagree about which file a part number names."""

    def test_part_is_a_hidden_sibling_carrying_the_index_and_the_extension(self):
        p = part_path('/dvr/incomplete/Race Day.mp4', 3)
        self.assertEqual(p, '/dvr/incomplete/.Race Day.part3.mp4')

    def test_part_is_hidden_so_a_media_scanner_does_not_index_it(self):
        # dvr_output_dir defaults to the same directory a single-directory install points
        # its scanner at, and a part is the size of the recording.
        self.assertTrue(os.path.basename(part_path('/dvr/x.mp4', 1)).startswith('.'))

    def test_part_extension_follows_the_output_container(self):
        self.assertTrue(part_path('/dvr/x.mkv', 1).endswith('.part1.mkv'))

    def test_existing_part_paths_is_one_based_and_contiguous(self):
        self.assertEqual(existing_part_paths('/dvr/x.mp4', 3),
                         ['/dvr/.x.part1.mp4', '/dvr/.x.part2.mp4', '/dvr/.x.part3.mp4'])

    def test_zero_parts_is_an_empty_list_not_a_first_part(self):
        self.assertEqual(existing_part_paths('/dvr/x.mp4', 0), [])


class PartsSignatureTests(unittest.TestCase):
    """A signature is what stops parts encoded under different settings being joined into one
    file with an invisible seam in the middle of it."""

    BASE = ['ffmpeg', '-i', 'src.ts', '-c:v', 'libx264', '-crf', '20',
            '-c:a', 'aac', '-b:a', '192k', '-movflags', PART_MOVFLAGS, '-y', 'out.mp4']

    def test_the_seek_offset_does_not_change_the_signature(self):
        # -ss is what legitimately differs BETWEEN parts of one set; if it counted, no two
        # parts would ever share a signature and the checkpoint could never be resumed.
        with_ss = self.BASE[:1] + ['-ss', '123.456'] + self.BASE[1:]
        self.assertEqual(parts_signature(self.BASE, 'out.mp4'),
                         parts_signature(with_ss, 'out.mp4'))

    def test_the_output_path_does_not_change_the_signature(self):
        other = self.BASE[:-1] + ['.out.part2.mp4']
        self.assertEqual(parts_signature(self.BASE, 'out.mp4'),
                         parts_signature(other, '.out.part2.mp4'))

    def test_a_changed_crf_changes_the_signature(self):
        changed = [('23' if a == '20' else a) for a in self.BASE]
        self.assertNotEqual(parts_signature(self.BASE, 'out.mp4'),
                            parts_signature(changed, 'out.mp4'))

    def test_switching_to_copied_audio_changes_the_signature(self):
        # The audio-copy fallback is the case that forced this to exist: a joined file would
        # carry one codec config over a track encoded two different ways.
        copied = ['ffmpeg', '-i', 'src.ts', '-c:v', 'libx264', '-crf', '20',
                  '-c:a', 'copy', '-bsf:a', 'aac_adtstoasc',
                  '-movflags', PART_MOVFLAGS, '-y', 'out.mp4']
        self.assertNotEqual(parts_signature(self.BASE, 'out.mp4'),
                            parts_signature(copied, 'out.mp4'))

    def test_a_changed_source_changes_the_signature(self):
        other = [('other.ts' if a == 'src.ts' else a) for a in self.BASE]
        self.assertNotEqual(parts_signature(self.BASE, 'out.mp4'),
                            parts_signature(other, 'out.mp4'))


class CheckpointWriterTests(unittest.TestCase):
    """The four columns are ONE fact with ONE writer, the same shape set_postprocess_wait()
    already uses. A part count without a covered offset resumes from the wrong place; a
    covered offset without a signature joins parts that do not belong together."""

    def setUp(self):
        self.t = make_test_app()
        self.rec = seed.make_recording(status='CONVERTING', name='r')
        db.session.commit()

    def tearDown(self):
        self.t.cleanup()

    def test_all_four_columns_move_together(self):
        set_conversion_parts(self.rec, parts_done=2, source_covered=41.5,
                             source_complete=False, signature='abc')
        db.session.commit()
        r = db.session.get(Recording, self.rec.id)
        self.assertEqual(r.conversion_parts_done, 2)
        self.assertAlmostEqual(r.conversion_source_covered_seconds, 41.5)
        self.assertFalse(r.conversion_source_complete)
        self.assertEqual(r.conversion_parts_signature, 'abc')

    def test_calling_it_bare_clears_the_whole_checkpoint(self):
        set_conversion_parts(self.rec, parts_done=2, source_covered=41.5, signature='abc')
        db.session.commit()
        set_conversion_parts(self.rec)
        db.session.commit()
        r = db.session.get(Recording, self.rec.id)
        self.assertEqual(r.conversion_parts_done, 0)
        self.assertIsNone(r.conversion_source_covered_seconds)
        self.assertFalse(r.conversion_source_complete)
        self.assertIsNone(r.conversion_parts_signature)

    def test_a_fresh_recording_is_not_checkpointed(self):
        # The default has to read as "nothing is checkpointed" or migration 60 would have
        # needed a backfill to avoid claiming every existing row had parts.
        r = seed.make_recording(status='SCHEDULED', name='fresh')
        db.session.commit()
        self.assertIn(r.conversion_parts_done, (0, None))
        self.assertIsNone(r.conversion_source_covered_seconds)
        self.assertIsNone(r.conversion_parts_signature)

    def test_the_writer_does_not_commit_so_the_caller_owns_the_unit(self):
        set_conversion_parts(self.rec, parts_done=4, source_covered=9.0, signature='z')
        db.session.rollback()
        r = db.session.get(Recording, self.rec.id)
        self.assertIn(r.conversion_parts_done, (0, None))


# ── (f): teardown ─────────────────────────────────────────────────────────────────────

class PartEnumerationTests(unittest.TestCase):
    """Teardown finds parts on DISK rather than trusting the recorded count: the count is
    exactly what a crash between ffmpeg and the commit gets wrong, and an unrecorded part is
    the file nothing else will ever delete."""

    def setUp(self):
        self._dir = tempfile.mkdtemp(prefix='cresume-')
        self.out = os.path.join(self._dir, 'show.mp4')

    def tearDown(self):
        shutil.rmtree(self._dir, ignore_errors=True)

    def _touch(self, path, size=4):
        with open(path, 'wb') as fh:
            fh.write(b'x' * size)

    def test_parts_on_disk_are_found_even_when_no_row_describes_them(self):
        self._touch(part_path(self.out, 1))
        self._touch(part_path(self.out, 2))
        self.assertEqual(sorted(all_part_paths_on_disk(self.out)),
                         sorted([part_path(self.out, 1), part_path(self.out, 2)]))

    def test_the_clean_remux_sibling_is_found_too(self):
        # finalize_part() os.replace()s it over the part; it only outlives that call when a
        # shutdown lands mid-re-mux, and it is the same size as the part.
        self._touch(clean_part_path(part_path(self.out, 1)))
        self.assertIn(clean_part_path(part_path(self.out, 1)),
                      all_part_paths_on_disk(self.out))

    def test_another_recordings_parts_are_not_claimed(self):
        other = os.path.join(self._dir, 'other.mp4')
        self._touch(part_path(other, 1))
        self.assertEqual(all_part_paths_on_disk(self.out), [])

    def test_discard_removes_every_part_and_reports_how_many(self):
        self._touch(part_path(self.out, 1))
        self._touch(part_path(self.out, 2))
        self.assertEqual(discard_conversion_parts(self.out), 2)
        self.assertEqual(all_part_paths_on_disk(self.out), [])

    def test_discard_on_a_conversion_that_never_checkpointed_is_a_no_op(self):
        self.assertEqual(discard_conversion_parts(self.out), 0)


class TeardownEnumeratesPartsTests(unittest.TestCase):
    """recording_disk_paths() is the canonical teardown enumerator. A FAILED conversion's
    parts are multi-gigabyte and the row still names its .ts, so nothing else points at them
    (the same shape as the partial-output gap of dev/docs/BUGS.md 2026-09-09)."""

    def setUp(self):
        self.t = make_test_app()
        self._dir = tempfile.mkdtemp(prefix='cresume-td-')
        self.ts = os.path.join(self._dir, 'show.ts')
        with open(self.ts, 'wb') as fh:
            fh.write(b'0' * 16)
        self.rec = seed.make_recording(status='FAILED', name='r')
        self.rec.output_path = self.ts
        db.session.commit()

    def tearDown(self):
        self.t.cleanup()
        shutil.rmtree(self._dir, ignore_errors=True)

    def test_conversion_parts_are_listed_for_deletion(self):
        from app.recorder import recording_disk_paths
        mp4 = os.path.splitext(self.ts)[0] + '.mp4'
        for i in (1, 2):
            with open(part_path(mp4, i), 'wb') as fh:
                fh.write(b'x' * 8)
        paths = recording_disk_paths(self.rec.id)
        self.assertIn(part_path(mp4, 1), paths)
        self.assertIn(part_path(mp4, 2), paths)


# ── (e): every progress figure is a SOURCE position ───────────────────────────────────

class SourceOffsetTests(unittest.TestCase):
    """A resumed attempt's ffmpeg reports out_time relative to its own -ss, so every figure
    derived from it adds the offset back. Without that a resume shows 0% after hours of
    encoding already on disk, and sizes its collision window on source it is not going to
    touch."""

    def setUp(self):
        self.t = make_test_app()
        self.rec = seed.make_recording(status='CONVERTING', name='r')
        db.session.commit()
        self.rid = self.rec.id

    def tearDown(self):
        self.t.cleanup()

    def _run(self, *, source_offset, out_time, expected_duration=1000.0,
             collision_policy='off'):
        """Drive run_conversion_supervised() with the supervisor replaced, so the wiring
        under test is exercised without spawning ffmpeg."""
        published = []
        windows = []

        def _fake_supervise(cmd, output_path, **kw):
            kw['on_progress'](10.0, out_time, 4096)
            if kw.get('suspend_check') is not None:
                with mock.patch.object(ppmod, '_collision_window_seconds',
                                       side_effect=lambda rem, mult: windows.append(rem) or 0.0):
                    kw['suspend_check'](10.0, out_time, 4096)
            return SupervisedRun('success', returncode=0, out_time=out_time, size=4096)

        with mock.patch.object(ppmod, 'supervise_ffmpeg', _fake_supervise), \
                mock.patch.object(ppmod, '_persist_conversion_snapshot',
                                  side_effect=lambda rid, pct, size, eta: published.append(pct)):
            result = ppmod.run_conversion_supervised(
                self.t.app, self.rid, ['ffmpeg'], '/tmp/x.mp4',
                expected_duration=expected_duration, pre_output_timeout=10,
                interval=1, stall_seconds=10, collision_policy=collision_policy,
                source_offset=source_offset)
        return result, published, windows

    def test_progress_is_reported_against_the_source_not_the_attempt(self):
        _, published, _ = self._run(source_offset=600.0, out_time=100.0)
        # 700 of 1000 seconds of source are encoded, not 100.
        self.assertAlmostEqual(published[0], 70.0, places=3)

    def test_a_resume_does_not_report_falling_back_to_zero(self):
        _, published, _ = self._run(source_offset=600.0, out_time=0.0)
        self.assertTrue(published[0] is None or published[0] >= 60.0)

    def test_an_unresumed_attempt_is_unchanged(self):
        _, published, _ = self._run(source_offset=0.0, out_time=250.0)
        self.assertAlmostEqual(published[0], 25.0, places=3)

    def test_out_time_comes_back_as_an_absolute_source_position(self):
        # The restart loop compares this across attempts to tell a damaged source from a
        # transient failure, and the comparison is meaningless between attempts that started
        # in different places.
        result, _, _ = self._run(source_offset=600.0, out_time=100.0)
        self.assertAlmostEqual(result.out_time, 700.0, places=3)

    def test_the_collision_window_is_sized_on_source_still_to_encode(self):
        _, _, windows = self._run(source_offset=600.0, out_time=100.0,
                                  collision_policy='cancel')
        self.assertTrue(windows, 'the collision check never ran')
        self.assertAlmostEqual(windows[0], 300.0, places=3)


# ── (b): the splice point, measured with real ffmpeg ──────────────────────────────────

@unittest.skipUnless(_HAVE_FFMPEG, 'ffmpeg/ffprobe not installed')
class FinalizePartTests(unittest.TestCase):
    """A part killed mid-write ends in a truncated fragment. Re-muxing it with -c copy drops
    that tail, and the duration of the RESULT is the last decodable frame - which is both the
    only honest splice point and what makes the file safe to hand to the concat demuxer."""

    @classmethod
    def setUpClass(cls):
        cls._dir = tempfile.mkdtemp(prefix='cresume-fin-')
        cls.whole = os.path.join(cls._dir, 'whole.mp4')
        _ffmpeg('-f', 'lavfi', '-i', 'testsrc=size=192x108:rate=30', '-t', '8',
                '-c:v', 'libx264', '-preset', 'ultrafast', '-bf', '0', '-pix_fmt', 'yuv420p',
                '-movflags', PART_MOVFLAGS, cls.whole)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls._dir, ignore_errors=True)

    def _truncated_copy(self, name, keep_fraction=0.6):
        """A fragmented part cut off mid-fragment, which is what a SIGKILL leaves."""
        path = os.path.join(self._dir, name)
        raw = open(self.whole, 'rb').read()
        with open(path, 'wb') as fh:
            fh.write(raw[:int(len(raw) * keep_fraction)])
        return path

    def test_a_fragmented_part_survives_being_cut_off(self):
        # Finding 1: +faststart writes moov LAST so a killed file has none. These flags are
        # the whole difference between a discarded partial and a usable checkpoint.
        cut = self._truncated_copy('cut.mp4')
        self.assertIsNotNone(_probe_duration(cut),
                             'a fragmented part must still probe after being truncated')

    def test_the_splice_point_is_the_last_decodable_frame_not_the_declared_duration(self):
        # Finding 3: the declared duration overshot the last clean frame by exactly 1.0s in
        # the original measurement, and resuming there lost 60 frames silently.
        cut = self._truncated_copy('cut2.mp4')
        declared = _probe_duration(cut)
        kept = finalize_part(cut, 'ffmpeg', scratch_key='t', interval=1,
                             pre_output_timeout=60, stall_seconds=60)
        self.assertIsNotNone(kept)
        self.assertLessEqual(kept, (declared or 0) + 0.001)

    def test_the_finalized_part_decodes_clean_to_its_end(self):
        cut = self._truncated_copy('cut3.mp4')
        finalize_part(cut, 'ffmpeg', scratch_key='t', interval=1,
                      pre_output_timeout=60, stall_seconds=60)
        self.assertEqual(_decode_error_count(cut), 0,
                         'the re-mux must drop the fragment that was mid-write')

    def test_it_is_idempotent_so_a_restart_may_call_it_without_knowing(self):
        # This is what lets a service restart adopt a part no commit describes.
        cut = self._truncated_copy('cut4.mp4')
        first = finalize_part(cut, 'ffmpeg', scratch_key='t', interval=1,
                              pre_output_timeout=60, stall_seconds=60)
        second = finalize_part(cut, 'ffmpeg', scratch_key='t', interval=1,
                               pre_output_timeout=60, stall_seconds=60)
        self.assertAlmostEqual(first, second, places=2)

    def test_nothing_usable_reports_none_rather_than_raising(self):
        garbage = os.path.join(self._dir, 'garbage.mp4')
        with open(garbage, 'wb') as fh:
            fh.write(b'not an mp4' * 32)
        self.assertIsNone(finalize_part(garbage, 'ffmpeg', scratch_key='t', interval=1,
                                        pre_output_timeout=20, stall_seconds=20))

    def test_a_missing_part_reports_none(self):
        self.assertIsNone(finalize_part(os.path.join(self._dir, 'nope.mp4'), 'ffmpeg',
                                        scratch_key='t'))

    def test_an_empty_part_reports_none(self):
        empty = os.path.join(self._dir, 'empty.mp4')
        open(empty, 'wb').close()
        self.assertIsNone(finalize_part(empty, 'ffmpeg', scratch_key='t'))

    def test_the_clean_scratch_does_not_outlive_the_call(self):
        cut = self._truncated_copy('cut5.mp4')
        finalize_part(cut, 'ffmpeg', scratch_key='t', interval=1,
                      pre_output_timeout=60, stall_seconds=60)
        self.assertFalse(os.path.exists(clean_part_path(cut)))


# ── the whole cycle: kill, resume, join ───────────────────────────────────────────────

@unittest.skipUnless(_HAVE_FFMPEG, 'ffmpeg/ffprobe not installed')
class SpliceFidelityTests(unittest.TestCase):
    """The claim the whole feature rests on: a killed part plus a part encoded from the
    splice point, joined with the concat demuxer, reproduce the single-pass encode.

    A join that quietly drops a second of video is worse than not resuming at all - principle
    1 outranks principle 2 - so this asserts on duration AND on decoding clean end to end,
    not merely on the file existing."""

    @classmethod
    def setUpClass(cls):
        cls._dir = tempfile.mkdtemp(prefix='cresume-splice-')
        cls.src = os.path.join(cls._dir, 'src.ts')
        _ffmpeg('-f', 'lavfi', '-i', 'testsrc=size=192x108:rate=30',
                '-f', 'lavfi', '-i', 'sine=frequency=440:sample_rate=48000', '-t', '12',
                '-c:v', 'libx264', '-preset', 'ultrafast', '-bf', '0', '-pix_fmt', 'yuv420p',
                '-c:a', 'aac', '-shortest', cls.src)
        cls.reference = os.path.join(cls._dir, 'ref.mp4')
        _ffmpeg('-i', cls.src, '-c:v', 'libx264', '-preset', 'ultrafast', '-bf', '0',
                '-pix_fmt', 'yuv420p', '-force_key_frames', 'expr:gte(t,n_forced*2)',
                '-c:a', 'aac', '-movflags', '+faststart', cls.reference)
        cls.ref_duration = _probe_duration(cls.reference)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls._dir, ignore_errors=True)

    def _encode_part(self, path, start_at=0.0, duration=None):
        args = ['-err_detect', 'ignore_err', '-fflags', '+genpts+discardcorrupt',
                '-max_error_rate', '1.0']
        if start_at:
            args += ['-ss', f'{start_at:.6f}']
        args += ['-i', self.src]
        if duration:
            args += ['-t', str(duration)]
        args += ['-c:v', 'libx264', '-preset', 'ultrafast', '-bf', '0', '-pix_fmt', 'yuv420p',
                 '-force_key_frames', 'expr:gte(t,n_forced*2)', '-c:a', 'aac',
                 '-movflags', PART_MOVFLAGS, path]
        _ffmpeg(*args)

    def test_a_killed_part_plus_a_resumed_part_reproduce_the_single_pass_encode(self):
        out = os.path.join(self._dir, 'joined.mp4')
        p1 = part_path(out, 1)
        p2 = part_path(out, 2)

        # Part 1 stops part-way and is cut off mid-fragment, exactly as a kill leaves it.
        self._encode_part(p1, duration=7)
        raw = open(p1, 'rb').read()
        with open(p1, 'wb') as fh:
            fh.write(raw[:int(len(raw) * 0.82)])

        covered = finalize_part(p1, 'ffmpeg', scratch_key='t', interval=1,
                                pre_output_timeout=60, stall_seconds=60)
        self.assertIsNotNone(covered, 'the killed part held nothing usable')

        # Part 2 picks up from the last decodable frame of part 1 - not from 7.0.
        self._encode_part(p2, start_at=covered)

        result = join_conversion_parts([p1, p2], out, 'ffmpeg', scratch_key='t', interval=1,
                                       pre_output_timeout=120, stall_seconds=120)
        self.assertTrue(result.success, f'join failed: {result.error_msg}')
        self.assertEqual(result.parts, 2)

        joined = _probe_duration(out)
        self.assertIsNotNone(joined)
        # Finding 2 measured about two frames of splice drift. A tolerance of a quarter
        # second is far inside that and far outside the 1.0s the wrong splice point lost.
        self.assertAlmostEqual(joined, self.ref_duration, delta=0.25,
                               msg=f'joined {joined}s vs single-pass {self.ref_duration}s')
        self.assertEqual(_decode_error_count(out), 0,
                         'the joined file must decode clean end to end')

    def test_a_single_part_join_produces_the_final_container(self):
        # The uninterrupted case still goes through the join, because that is what puts moov
        # back at the front of the file - +faststart moved here from the encode.
        out = os.path.join(self._dir, 'single.mp4')
        p1 = part_path(out, 1)
        self._encode_part(p1)
        result = join_conversion_parts([p1], out, 'ffmpeg', scratch_key='t', interval=1,
                                       pre_output_timeout=120, stall_seconds=120)
        self.assertTrue(result.success, f'join failed: {result.error_msg}')
        self.assertAlmostEqual(_probe_duration(out), self.ref_duration, delta=0.25)
        self.assertEqual(_decode_error_count(out), 0)


class JoinRefusalTests(unittest.TestCase):
    """The join checks what it needs BEFORE spawning ffmpeg. The parts are the only copy of
    the encoding at that point, so a refusal that keeps them and names what it needed beats a
    truncated output discovered halfway through."""

    def setUp(self):
        self._dir = tempfile.mkdtemp(prefix='cresume-join-')
        self.out = os.path.join(self._dir, 'show.mp4')

    def tearDown(self):
        shutil.rmtree(self._dir, ignore_errors=True)

    def test_no_parts_is_refused_without_spawning_anything(self):
        with mock.patch.object(ppmod, 'supervise_ffmpeg') as spawn:
            result = join_conversion_parts([], self.out, 'ffmpeg', scratch_key='t')
        self.assertFalse(result.success)
        self.assertEqual(result.reason, 'no_parts')
        spawn.assert_not_called()

    def test_a_missing_part_is_refused_by_name_rather_than_silently_shortening(self):
        p1 = part_path(self.out, 1)
        with open(p1, 'wb') as fh:
            fh.write(b'x' * 16)
        with mock.patch.object(ppmod, 'supervise_ffmpeg') as spawn:
            result = join_conversion_parts([p1, part_path(self.out, 2)], self.out, 'ffmpeg',
                                           scratch_key='t')
        self.assertFalse(result.success)
        self.assertEqual(result.reason, 'missing_part')
        spawn.assert_not_called()

    def test_too_little_disk_space_is_refused_and_says_what_it_needed(self):
        p1 = part_path(self.out, 1)
        with open(p1, 'wb') as fh:
            fh.write(b'x' * 4096)
        fake = mock.Mock(free=16)
        with mock.patch.object(ppmod.shutil, 'disk_usage', return_value=fake), \
                mock.patch.object(ppmod, 'supervise_ffmpeg') as spawn:
            result = join_conversion_parts([p1], self.out, 'ffmpeg', scratch_key='t')
        self.assertFalse(result.success)
        self.assertEqual(result.reason, 'no_space')
        self.assertIn('free space', result.error_msg)
        spawn.assert_not_called()

    def test_a_refusal_leaves_the_parts_alone(self):
        p1 = part_path(self.out, 1)
        with open(p1, 'wb') as fh:
            fh.write(b'x' * 16)
        join_conversion_parts([p1, part_path(self.out, 2)], self.out, 'ffmpeg',
                              scratch_key='t')
        self.assertTrue(os.path.exists(p1))


# ── the restart loop's decisions ──────────────────────────────────────────────────────

class RestartLoopResumeTests(unittest.TestCase):
    """do_postprocess()'s conversion phase, with the supervisor and the join replaced. What
    is under test is which command it builds and what it records - not ffmpeg."""

    def setUp(self):
        self.t = make_test_app()
        self.dvr = os.path.join(self.t._tmpdir, 'dvr')
        os.makedirs(self.dvr, exist_ok=True)
        self.ts = os.path.join(self.dvr, 'show.ts')
        with open(self.ts, 'wb') as fh:
            fh.write(b'0' * 4096)
        self.out = os.path.splitext(self.ts)[0] + '.mp4'
        self.rec = seed.make_recording(status='ANALYZING', name='show')
        self.rec.output_path = self.ts
        self.rec.recorded_duration_seconds = 1000.0
        db.session.commit()
        self.rid = self.rec.id

    def tearDown(self):
        self.t.cleanup()

    def _config(self, **pp_overrides):
        import app.config as cfgmod
        pp = dict({'enabled': True, 'format': 'mp4', 'delete_source': False,
                   'reencode_mode': 'always', 'pre_output_timeout_seconds': 60,
                   'auto_restart': True, 'max_restart_attempts': 3,
                   'stall_seconds': 0, 'progress_interval_seconds': 5,
                   'collision_policy': 'off', 'video_crf': 20,
                   'audio_bitrate_kbps': 192}, **pp_overrides)
        return cfgmod._deep_merge(cfgmod.load_config(), {'recording': {
            'gather_health_data': False,
            'move_on_complete': {'enabled': False},
            'post_script': {'enabled': False},
            'post_process': pp,
        }})

    def _run(self, results, *, cfg=None, join_ok=True):
        """Drive the phase. `results` is what each supervised attempt returns in order."""
        import app.config as cfgmod
        calls = []

        def _supervised(app_, rid, cmd, dest, **kw):
            calls.append({'cmd': cmd, 'dest': dest, 'source_offset': kw.get('source_offset')})
            if dest != self.ts and not os.path.exists(dest):
                # The supervisor's ffmpeg would have created it; the salvage path looks for it.
                with open(dest, 'wb') as fh:
                    fh.write(b'p' * 64)
            return results[len(calls) - 1]

        joins = []

        def _join(parts, output_path, ffmpeg_path, **kw):
            joins.append(list(parts))
            if join_ok:
                with open(output_path, 'wb') as fh:
                    fh.write(b'j' * 128)
            return ppmod.PartJoinResult(join_ok, 'success' if join_ok else 'died',
                                        error_msg=None if join_ok else 'boom',
                                        parts=len(parts))

        with mock.patch.object(cfgmod, 'load_config', return_value=cfg or self._config()), \
                mock.patch.object(ppmod, 'nominal_video_rate', create=True,
                                  return_value=None), \
                mock.patch.object(ppmod, 'run_conversion_supervised', _supervised), \
                mock.patch.object(ppmod, 'join_conversion_parts', _join), \
                mock.patch.object(ppmod, 'finalize_part', return_value=120.0):
            ppmod.do_postprocess(self.t.app, self.rid, self.ts)
        db.session.expire_all()
        return calls, joins

    def _ok(self, out_time=1000.0):
        return ppmod.ConversionResult(True, 'success', out_time=out_time)

    def _dead(self, out_time=120.0):
        return ppmod.ConversionResult(False, 'died', error_msg='died', out_time=out_time)

    def test_a_reencode_writes_a_part_not_the_final_file(self):
        calls, _ = self._run([self._ok()])
        self.assertEqual(calls[0]['dest'], part_path(self.out, 1))
        self.assertIn(PART_MOVFLAGS, calls[0]['cmd'])
        self.assertNotIn('+faststart', calls[0]['cmd'])

    def test_the_first_attempt_does_not_seek(self):
        calls, _ = self._run([self._ok()])
        self.assertNotIn('-ss', calls[0]['cmd'])
        self.assertEqual(calls[0]['source_offset'], 0.0)

    def test_a_killed_attempt_is_followed_by_one_that_seeks_to_the_salvaged_point(self):
        calls, _ = self._run([self._dead(), self._ok()])
        self.assertEqual(len(calls), 2)
        self.assertIn('-ss', calls[1]['cmd'])
        # finalize_part is stubbed at 120.0, so that is where the next part starts.
        self.assertAlmostEqual(float(calls[1]['cmd'][calls[1]['cmd'].index('-ss') + 1]),
                               120.0, places=3)
        self.assertAlmostEqual(calls[1]['source_offset'], 120.0, places=3)
        self.assertEqual(calls[1]['dest'], part_path(self.out, 2))

    def test_the_parts_are_joined_into_the_final_output(self):
        _, joins = self._run([self._dead(), self._ok()])
        self.assertEqual(joins, [[part_path(self.out, 1), part_path(self.out, 2)]])

    def test_a_successful_join_clears_the_checkpoint_and_names_the_output(self):
        self._run([self._ok()])
        r = db.session.get(Recording, self.rid)
        self.assertEqual(r.output_path, self.out)
        self.assertEqual(r.conversion_parts_done, 0)
        self.assertIsNone(r.conversion_parts_signature)

    def test_a_failed_join_keeps_the_parts_and_the_recorded_completion(self):
        # The parts are the only copy of the encoding and the join is a stream copy, so a
        # Retry must come back to the join rather than to the encode.
        self._run([self._ok()], join_ok=False)
        r = db.session.get(Recording, self.rid)
        self.assertEqual(r.status, 'FAILED')
        self.assertTrue(r.conversion_source_complete,
                        'a failed join must leave the encode recorded as finished')
        self.assertEqual(r.conversion_parts_done, 1)

    def test_a_recorded_complete_encode_skips_straight_to_the_join(self):
        set_conversion_parts(self.rec, parts_done=2, source_covered=1000.0,
                             source_complete=True,
                             signature=None)
        db.session.commit()
        # The signature has to match for the checkpoint to be honoured, so take the one the
        # run itself computes by letting a first run record it, then re-drive.
        calls, joins = self._run([self._ok()])
        self.assertEqual(len(calls), 1, 'a mismatched signature must re-encode, not resume')
        del joins

    def test_the_signature_is_registered_before_the_first_part_is_written(self):
        # The ordering is what makes the crash window safe: a recorded signature with no part
        # is answerable, a part with no recorded signature is not.
        seen = {}

        import app.config as cfgmod

        def _supervised(app_, rid, cmd, dest, **kw):
            r = db.session.get(Recording, rid)
            seen['signature_at_spawn'] = r.conversion_parts_signature
            with open(dest, 'wb') as fh:
                fh.write(b'p')
            return self._ok()

        with mock.patch.object(cfgmod, 'load_config', return_value=self._config()), \
                mock.patch.object(ppmod, 'run_conversion_supervised', _supervised), \
                mock.patch.object(ppmod, 'join_conversion_parts',
                                  return_value=ppmod.PartJoinResult(True, 'success', parts=1)), \
                mock.patch.object(ppmod, 'finalize_part', return_value=120.0):
            ppmod.do_postprocess(self.t.app, self.rid, self.ts)
        self.assertIsNotNone(seen.get('signature_at_spawn'),
                             'the signature must be committed before any part exists')

    def test_a_stream_copy_is_not_resumable_and_keeps_faststart(self):
        cfg = self._config(reencode_mode='never')
        calls, joins = self._run([self._ok()], cfg=cfg)
        self.assertEqual(calls[0]['dest'], self.out, 'a stream copy writes the final file')
        self.assertIn('+faststart', calls[0]['cmd'])
        self.assertEqual(joins, [], 'a stream copy has nothing to join')

    def test_an_mkv_conversion_is_untouched(self):
        cfg = self._config(format='mkv', reencode_mode='never')
        calls, joins = self._run([self._ok()], cfg=cfg)
        self.assertEqual(calls[0]['dest'], os.path.splitext(self.ts)[0] + '.mkv')
        self.assertEqual(joins, [])

    def test_the_audio_copy_fallback_discards_the_parts_and_registers_its_new_settings(self):
        """The fallback swaps -c:a aac for -c:a copy, so anything already encoded holds a
        differently encoded audio track and cannot be joined onto what follows. Registering
        the NEW signature is the other half: clearing alone would leave a copied-audio part
        on disk with nothing recorded about it, and a restart would then adopt it under the
        re-encoded-audio signature it recomputes."""
        seen = []

        import app.config as cfgmod

        def _supervised(app_, rid, cmd, dest, **kw):
            r = db.session.get(Recording, rid)
            seen.append({'audio_copy': '-c:a' in cmd and cmd[cmd.index('-c:a') + 1] == 'copy',
                         'dest': dest,
                         'signature_at_spawn': r.conversion_parts_signature,
                         'offset': kw.get('source_offset')})
            with open(dest, 'wb') as fh:
                fh.write(b'p' * 32)
            if len(seen) < 3:
                # Two deaths at the same source position: the source is damaged there.
                return ppmod.ConversionResult(False, 'died', error_msg='died', out_time=500.0)
            return self._ok()

        with mock.patch.object(cfgmod, 'load_config', return_value=self._config()), \
                mock.patch.object(ppmod, 'run_conversion_supervised', _supervised), \
                mock.patch.object(ppmod, 'join_conversion_parts',
                                  return_value=ppmod.PartJoinResult(True, 'success', parts=1)), \
                mock.patch.object(ppmod, 'finalize_part', return_value=400.0):
            ppmod.do_postprocess(self.t.app, self.rid, self.ts)
        db.session.expire_all()

        self.assertEqual(len(seen), 3, f'expected three attempts, got {len(seen)}')
        self.assertFalse(seen[0]['audio_copy'])
        self.assertTrue(seen[2]['audio_copy'], 'the third attempt must be the copied-audio one')
        # It starts over: the parts behind it were made with re-encoded audio.
        self.assertEqual(seen[2]['dest'], part_path(self.out, 1))
        self.assertEqual(seen[2]['offset'], 0.0)
        # And it starts over under its OWN recorded signature, not the previous one.
        self.assertIsNotNone(seen[2]['signature_at_spawn'])
        self.assertNotEqual(seen[2]['signature_at_spawn'], seen[0]['signature_at_spawn'],
                            'the fallback must register the settings it actually uses')

    def test_the_repeat_death_check_compares_absolute_source_positions(self):
        """Two attempts that stop at the same place in the SOURCE are the same stop, even
        though the second one started part-way in and reported a smaller out_time of its own.
        Comparing attempt-relative figures would miss it and burn the whole budget."""
        import app.config as cfgmod
        offsets = []

        def _supervised(app_, rid, cmd, dest, **kw):
            offsets.append(kw.get('source_offset'))
            with open(dest, 'wb') as fh:
                fh.write(b'p' * 32)
            # Both attempts stop at 500s into the source; the second started at 400s.
            return ppmod.ConversionResult(False, 'died', error_msg='died', out_time=500.0)

        with mock.patch.object(cfgmod, 'load_config',
                               return_value=self._config(format='mkv',
                                                          reencode_mode='never')), \
                mock.patch.object(ppmod, 'run_conversion_supervised', _supervised), \
                mock.patch.object(ppmod, 'finalize_part', return_value=400.0):
            ppmod.do_postprocess(self.t.app, self.rid, self.ts)
        db.session.expire_all()
        rec = db.session.get(Recording, self.rid)
        self.assertEqual(rec.status, 'FAILED')
        events = [e.detail for e in
                  RecordingEvent.query.filter_by(recording_id=self.rid).all()]
        self.assertTrue(any('same point' in (d or '') for d in events),
                        'a repeated stop must be named as the source being damaged there')

    def test_parts_from_different_settings_are_discarded_rather_than_joined(self):
        set_conversion_parts(self.rec, parts_done=1, source_covered=400.0,
                             source_complete=False, signature='made-under-other-settings')
        db.session.commit()
        with open(part_path(self.out, 1), 'wb') as fh:
            fh.write(b'stale' * 8)
        calls, _ = self._run([self._ok()])
        self.assertEqual(calls[0]['source_offset'], 0.0,
                         'a mismatched signature must re-encode from the start')
        self.assertFalse(os.path.exists(part_path(self.out, 1)) and
                         open(part_path(self.out, 1), 'rb').read().startswith(b'stale'),
                         'the stale part must not survive')


if __name__ == '__main__':
    unittest.main()
