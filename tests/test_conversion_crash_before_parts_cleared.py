"""Tier 2 - a crash between deleting the encoded parts and recording that they are gone must
not cost the finished converted file (dev/docs/BUGS.md 2026-09-15 @ 09:23:59 PM ET,
dev/changelog/985).

A resumable re-encode used to finish in three steps: unlink the part files, commit that the
checkpoint is clear, then commit the repointed output_path. A process death in the first gap -
a forced restart, an OOM kill, a reboot at the end of an hours-long encode - left the row
recording `parts_done=N, source_complete=True` with no parts on disk and a complete .mp4 at the
output path. The next attempt believed that checkpoint, skipped the encode, went straight to
assembly, got `missing_part` back, and DELETED the finished file before marking the recording
FAILED. CLAUDE.md's "'already done' is a fact you recorded, never one you inferred", failing in
the direction that rule warns about.

The invariants below:

  (a) The completion is committed FIRST - the checkpoint clear and the output_path repoint in
      one commit - and the parts are deleted only after it returns. The worst a crash can then
      leave is an orphan part beside a correctly-finished recording, which
      recording_disk_paths() already enumerates for teardown.
  (b) A `missing_part` join with a probe-clean file of the right length already at the output
      path ADOPTS that file rather than deleting it, and says so out loud.
  (c) Adoption is confirmed from the file, never from the row - the row is what is in doubt.
      Anything unreadable, truncated, wrong-length, or with no recorded span to check against
      falls through to the existing failure path rather than being adopted on a guess.

The real-ffmpeg tests here synthesize their input with lavfi and never touch a URL, so
tests/support/netguard.py is satisfied.
  python3 -m unittest tests.test_conversion_crash_before_parts_cleared
"""
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from contextlib import ExitStack
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import app.postprocessor as ppmod  # noqa: E402
from tests.support.app import make_test_app  # noqa: E402
from tests.support import seed  # noqa: E402
from app import db  # noqa: E402
from app.database import Recording, RecordingEvent  # noqa: E402
from app.postprocessor import (  # noqa: E402
    _adoptable_assembly, all_part_paths_on_disk, part_path, set_conversion_parts,
)

_HAVE_FFMPEG = bool(shutil.which('ffmpeg') and shutil.which('ffprobe'))

_SIGNATURE = 'fixedsignatureforthesetests'


def _ffmpeg(*args):
    subprocess.run(['ffmpeg', '-v', 'error', '-y', *args], check=True, timeout=180)


def _make_mp4(path, seconds):
    """A small, real, faststart mp4 - the shape a finished assembly has on disk."""
    _ffmpeg('-f', 'lavfi', '-i', 'testsrc=size=192x108:rate=30', '-t', str(seconds),
            '-c:v', 'libx264', '-preset', 'ultrafast', '-bf', '0', '-pix_fmt', 'yuv420p',
            '-movflags', '+faststart', path)


@unittest.skipUnless(_HAVE_FFMPEG, 'ffmpeg/ffprobe not available')
class AdoptableAssemblyTests(unittest.TestCase):
    """_adoptable_assembly() on its own: what it confirms and what it refuses.

    Every refusal here is a file the caller would otherwise delete, so each one is a deliberate
    decision that deleting is still right rather than an oversight."""

    @classmethod
    def setUpClass(cls):
        cls._dir = tempfile.mkdtemp(prefix='adoptcheck-')
        cls.good = os.path.join(cls._dir, 'good.mp4')
        _make_mp4(cls.good, 6)
        # An assembly killed mid-write. ffmpeg writes moov LAST and +faststart only relocates
        # it once the write finishes, so a kill leaves a file with no moov at all - which is
        # what the original incident saw ("moov atom not found"). Emulated by cutting a
        # non-faststart write short, measured on this box as probing empty.
        plain = os.path.join(cls._dir, 'plain.mp4')
        _ffmpeg('-f', 'lavfi', '-i', 'testsrc=size=192x108:rate=30', '-t', '6',
                '-c:v', 'libx264', '-preset', 'ultrafast', '-bf', '0', '-pix_fmt', 'yuv420p',
                plain)
        cls.killed = os.path.join(cls._dir, 'killed.mp4')
        with open(plain, 'rb') as src:
            data = src.read()
        with open(cls.killed, 'wb') as dst:
            dst.write(data[:int(len(data) * 0.6)])
        cls.garbage = os.path.join(cls._dir, 'garbage.mp4')
        with open(cls.garbage, 'wb') as fh:
            fh.write(b'not media' * 512)
        cls.empty = os.path.join(cls._dir, 'empty.mp4')
        open(cls.empty, 'wb').close()

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls._dir, ignore_errors=True)

    def test_a_finished_file_of_the_expected_length_is_adoptable(self):
        self.assertTrue(_adoptable_assembly(self.good, 6.0))

    def test_the_slack_is_wide_enough_for_the_frames_a_join_seam_costs(self):
        # A join loses about two frames per seam and the covered span is read from ffmpeg's
        # progress output, so a near-miss must still be adopted.
        self.assertTrue(_adoptable_assembly(self.good, 6.2))

    def test_a_file_that_does_not_exist_is_not_adoptable(self):
        self.assertFalse(_adoptable_assembly(os.path.join(self._dir, 'nope.mp4'), 6.0))

    def test_an_empty_file_is_not_adoptable(self):
        self.assertFalse(_adoptable_assembly(self.empty, 6.0))

    def test_an_unreadable_file_is_not_adoptable(self):
        self.assertFalse(_adoptable_assembly(self.garbage, 6.0))

    def test_an_assembly_killed_mid_write_is_not_adoptable(self):
        # The case a header-only probe genuinely settles: no moov, nothing to read.
        self.assertFalse(_adoptable_assembly(self.killed, 6.0))

    def test_a_readable_file_of_the_wrong_length_is_not_adoptable(self):
        self.assertFalse(_adoptable_assembly(self.good, 600.0))

    def test_nothing_to_measure_against_means_no_adoption(self):
        self.assertFalse(_adoptable_assembly(self.good, 0.0))
        self.assertFalse(_adoptable_assembly(self.good, None))


class _ConversionPhaseHarness(unittest.TestCase):
    """do_postprocess()'s conversion phase over a seeded checkpoint, with the encode stubbed.

    The join is deliberately NOT stubbed in the subclasses that exercise adoption: what is
    under test is the real refusal join_conversion_parts() returns when its parts are gone."""

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

    def _checkpoint(self, *, parts_done=1, source_covered=1000.0):
        """Record a complete encode, the way the attempt that crashed had already recorded it."""
        r = db.session.get(Recording, self.rid)
        set_conversion_parts(r, parts_done=parts_done, source_covered=source_covered,
                             source_complete=True, signature=_SIGNATURE)
        db.session.commit()

    def _drive(self, *, join=None, supervised=None):
        """Run the phase. Returns how many times the encode supervisor was called."""
        import app.config as cfgmod
        calls = []

        def _supervised(app_, rid, cmd, dest, **kw):
            calls.append(dest)
            if supervised is None:
                raise AssertionError('the encode must not re-run over a recorded complete one')
            if dest != self.ts and not os.path.exists(dest):
                with open(dest, 'wb') as fh:
                    fh.write(b'p' * 64)
            return supervised[len(calls) - 1]

        with ExitStack() as stack:
            stack.enter_context(
                mock.patch.object(cfgmod, 'load_config', return_value=self._config()))
            stack.enter_context(
                mock.patch.object(ppmod, 'nominal_video_rate', create=True, return_value=None))
            stack.enter_context(
                mock.patch.object(ppmod, 'parts_signature', return_value=_SIGNATURE))
            stack.enter_context(
                mock.patch.object(ppmod, 'run_conversion_supervised', _supervised))
            if join is not None:
                stack.enter_context(mock.patch.object(ppmod, 'join_conversion_parts', join))
            ppmod.do_postprocess(self.t.app, self.rid, self.ts)
        db.session.expire_all()
        return calls

    def _events_of_kind(self, kind):
        found = []
        for ev in RecordingEvent.query.filter_by(recording_id=self.rid).all():
            if ev.extra_data and f'"{kind}"' in ev.extra_data:
                found.append(ev)
        return found


class TheCompletionIsCommittedBeforeThePartsGoTests(_ConversionPhaseHarness):
    """(a) The ordering that closes the crash window."""

    def setUp(self):
        super().setUp()
        self._checkpoint(parts_done=2)
        for i in (1, 2):
            with open(part_path(self.out, i), 'wb') as fh:
                fh.write(b'part' * 32)
        self.row_at_discard = {}

        real_discard = ppmod.discard_conversion_parts

        def _spy(output_path):
            # The row as it stands at the instant the parts stop existing. A crash here is the
            # one the whole fix is about, so this is the state that has to already be right.
            db.session.expire_all()
            r = db.session.get(Recording, self.rid)
            self.row_at_discard = {
                'output_path': r.output_path,
                'parts_done': r.conversion_parts_done,
                'source_complete': r.conversion_source_complete,
                'signature': r.conversion_parts_signature,
            }
            return real_discard(output_path)

        self.discard_patch = mock.patch.object(ppmod, 'discard_conversion_parts', _spy)
        self.discard_patch.start()
        self.addCleanup(self.discard_patch.stop)

        def _join(parts, output_path, ffmpeg_path, **kw):
            with open(output_path, 'wb') as fh:
                fh.write(b'j' * 128)
            return ppmod.PartJoinResult(True, 'success', parts=len(parts))

        self._drive(join=_join)

    def test_the_output_is_already_named_when_the_parts_are_deleted(self):
        self.assertEqual(self.row_at_discard.get('output_path'), self.out)

    def test_the_checkpoint_is_already_cleared_when_the_parts_are_deleted(self):
        self.assertEqual(self.row_at_discard.get('parts_done'), 0)
        self.assertFalse(self.row_at_discard.get('source_complete'))
        self.assertIsNone(self.row_at_discard.get('signature'))

    def test_the_parts_are_gone_by_the_end(self):
        self.assertEqual(all_part_paths_on_disk(self.out), [])

    def test_the_recording_completed(self):
        r = db.session.get(Recording, self.rid)
        self.assertEqual(r.output_path, self.out)
        self.assertNotEqual(r.status, 'FAILED')


@unittest.skipUnless(_HAVE_FFMPEG, 'ffmpeg/ffprobe not available')
class AFinishedFileWhosePartsAreGoneIsAdoptedTests(_ConversionPhaseHarness):
    """(b) The reproduction from the backlog item: a row recording a complete encode, no parts
    on disk, and a valid file at the output path. The file must survive."""

    def setUp(self):
        super().setUp()
        _make_mp4(self.out, 6)
        self.size_before = os.path.getsize(self.out)
        # The crashed attempt recorded how much source its parts covered; the finished file
        # runs that long, which is what makes it recognisable as the assembly of those parts.
        self._checkpoint(parts_done=2, source_covered=6.0)
        self.encode_calls = self._drive()

    def test_the_finished_file_survives(self):
        self.assertTrue(os.path.exists(self.out), 'the finished conversion was deleted')
        self.assertEqual(os.path.getsize(self.out), self.size_before)

    def test_the_recording_is_not_failed(self):
        r = db.session.get(Recording, self.rid)
        self.assertNotEqual(r.status, 'FAILED')

    def test_the_row_names_the_adopted_file(self):
        r = db.session.get(Recording, self.rid)
        self.assertEqual(r.output_path, self.out)
        self.assertEqual(r.final_file_size, self.size_before)

    def test_the_encode_is_not_re_run(self):
        self.assertEqual(self.encode_calls, [])

    def test_the_checkpoint_is_cleared_so_the_next_pass_does_not_loop(self):
        r = db.session.get(Recording, self.rid)
        self.assertEqual(r.conversion_parts_done, 0)
        self.assertFalse(r.conversion_source_complete)
        self.assertIsNone(r.conversion_parts_signature)

    def test_the_adoption_is_said_out_loud(self):
        # A resumed attempt that quietly adopts a file nobody can account for is the silence
        # this app exists to remove.
        events = self._events_of_kind('conversion_output_adopted')
        self.assertEqual(len(events), 1, 'the adoption must be disclosed exactly once')
        self.assertIn('Adopted the converted file', events[0].detail)


@unittest.skipUnless(_HAVE_FFMPEG, 'ffmpeg/ffprobe not available')
class AnUnconfirmableFileIsNotAdoptedTests(_ConversionPhaseHarness):
    """(c) The refusals, driven end-to-end: today's failure path is still what happens when the
    file at the output path cannot be confirmed as this conversion's finished assembly."""

    def test_garbage_at_the_output_path_still_fails_the_conversion(self):
        with open(self.out, 'wb') as fh:
            fh.write(b'not media' * 512)
        self._checkpoint(parts_done=2, source_covered=6.0)
        self._drive()
        r = db.session.get(Recording, self.rid)
        self.assertEqual(r.status, 'FAILED')
        self.assertFalse(os.path.exists(self.out))
        self.assertEqual(self._events_of_kind('conversion_output_adopted'), [])

    def test_a_file_of_the_wrong_length_still_fails_the_conversion(self):
        _make_mp4(self.out, 6)
        # The row says the parts covered sixteen minutes; a six-second file is not them.
        self._checkpoint(parts_done=2, source_covered=1000.0)
        self._drive()
        r = db.session.get(Recording, self.rid)
        self.assertEqual(r.status, 'FAILED')
        self.assertEqual(self._events_of_kind('conversion_output_adopted'), [])

    def test_nothing_at_the_output_path_still_fails_the_conversion(self):
        self._checkpoint(parts_done=2, source_covered=6.0)
        self._drive()
        r = db.session.get(Recording, self.rid)
        self.assertEqual(r.status, 'FAILED')
        self.assertEqual(self._events_of_kind('conversion_output_adopted'), [])

    def test_a_join_that_failed_for_another_reason_keeps_its_parts(self):
        # Adoption is gated on missing_part. A join that died with its parts intact must still
        # keep them, so a Retry comes back to the join rather than to the encode.
        _make_mp4(self.out, 6)
        self._checkpoint(parts_done=1, source_covered=6.0)
        with open(part_path(self.out, 1), 'wb') as fh:
            fh.write(b'part' * 32)

        def _join(parts, output_path, ffmpeg_path, **kw):
            return ppmod.PartJoinResult(False, 'died', error_msg='boom', parts=len(parts))

        self._drive(join=_join)
        r = db.session.get(Recording, self.rid)
        self.assertEqual(r.status, 'FAILED')
        self.assertTrue(r.conversion_source_complete)
        self.assertEqual(r.conversion_parts_done, 1)
        self.assertEqual(self._events_of_kind('conversion_output_adopted'), [])


if __name__ == '__main__':
    unittest.main()
