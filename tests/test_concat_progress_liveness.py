"""Tier 2 - a concat that is still writing is never killed (dev/changelog/947).

Guards the replacement of `_run_concatenation()`'s fixed `ffmpeg.concat_timeout_seconds`
whole-job deadline with the two rules the conversion already used: a budget for the phase
before ffmpeg writes anything, then a no-growth stall budget as the sole authority once the
output file is growing.

The defect: the join reads and writes however many bytes the capture produced, so a fixed
300s budget is a bet on file size and a large recording loses it. Recording 19,
2026-09-13 - 16 valid segments totaling 42.6 GB - wrote 21.4 GB in 300s (71 MB/s, near
line rate for that CIFS mount), was killed at roughly the halfway mark, and the row went
FAILED with no output_path. Finishing needed about 600s.

Second half of the same failure, covered here too: the killed attempt left a 21.4 GB .ts
on disk that no row referenced, because output_path is committed on success only. Nothing
would ever have deleted it, and reserve_concat_output_path() judges a stem by its whole
extension family, so every future attempt at that recording would have been pushed onto a
`_2` name and stepped around the garbage permanently.

Covers, in order:
  - AdvancingConcatTests: the two rules, against a fake ffmpeg whose output file the test
    grows (or does not) on a schedule.
  - FailedConcatPartialTests: a failed concat removes what it wrote, and the segments it
    was built from survive - which is the only reason removing it is safe.
  - ConfigMigrationTests: config_version 4 drops the retired key.
  - SettingsClampTests: the new keys' floors are enforced server-side.

No real ffmpeg - a fake Popen whose "output" the test writes by hand. See CLAUDE.md
§Testing. Run standalone:
  python3 -m unittest tests.test_concat_progress_liveness
"""
import os
import re
import sys
import threading
import time
import unittest
from datetime import datetime
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import app.concatenator as catmod  # noqa: E402
import app.proc_utils as pumod  # noqa: E402
from app import db  # noqa: E402
from app.config import _cfg_m004_concat_progress_supervision, load_config  # noqa: E402
from app.database import Recording, RecordingSegment  # noqa: E402
from app.proc_utils import supervise_ffmpeg  # noqa: E402
from tests.support import seed  # noqa: E402
from tests.support.app import make_test_app  # noqa: E402


class _FakeJoin:
    """A stand-in for the concat ffmpeg. It never writes anything itself; the test drives
    the output file, which is exactly the signal the supervisor is supposed to watch."""

    def __init__(self, alive_ticks=None, returncode=0):
        self._left = alive_ticks
        self._rc = returncode
        self.returncode = None
        self.terminated = False
        self.killed = False
        self.signals = []

    def send_signal(self, sig):
        # terminate_or_kill() continues a possibly-suspended child before every teardown
        # (dev/changelog/952), so a fake that cannot take a signal is not a faithful one.
        self.signals.append(sig)

    def poll(self):
        if self._left is None:
            return None
        if self._left > 0:
            self._left -= 1
            return None
        self.returncode = self._rc
        return self._rc

    def terminate(self):
        self.terminated = True
        self.returncode = -15

    def kill(self):
        self.killed = True
        self.terminated = True
        self.returncode = -9

    def wait(self, timeout=None):
        self.returncode = self.returncode if self.returncode is not None else -9
        return self.returncode


class _SupervisedJoinCase(unittest.TestCase):
    """One supervised join whose output file the test grows on its own thread."""

    def setUp(self):
        self.t = make_test_app()
        self.out = os.path.join(self.t._tmpdir, 'join.ts')
        self.cmd = ['/usr/bin/ffmpeg', '-f', 'concat', '-i', 'list.txt', '-c', 'copy',
                    '-y', self.out]
        self._stop = threading.Event()
        self._writer = None

    def tearDown(self):
        self._stop.set()
        if self._writer is not None:
            self._writer.join(timeout=5)
        self.t.cleanup()

    def _grow_output(self, *, chunk=4096, every=0.01, stop_after=None):
        """Write to the output file on a background thread, the way a real join would."""
        def _run():
            written = 0
            while not self._stop.is_set():
                if stop_after is not None and written >= stop_after:
                    return
                with open(self.out, 'ab') as fh:
                    fh.write(b'\x00' * chunk)
                written += chunk
                time.sleep(every)

        self._writer = threading.Thread(target=_run, daemon=True)
        self._writer.start()

    def _run(self, fake, *, pre_output_timeout, stall_seconds, interval=0.02):
        with mock.patch.object(pumod.subprocess, 'Popen', return_value=fake):
            return supervise_ffmpeg(
                self.cmd, self.out,
                scratch_prefix='concat', scratch_key=1, interval=interval,
                pre_output_timeout=pre_output_timeout, stall_seconds=stall_seconds,
                progress_signal='size', noun='concat', label='test concat')


class AdvancingConcatTests(_SupervisedJoinCase):

    def test_a_join_still_writing_outlives_its_pre_output_budget(self):
        """The whole point: recording 19's join. Under the old rule a join that passed the
        fixed budget was killed regardless of throughput, and 42.6 GB of segments could not
        be joined at all."""
        self._grow_output()
        fake = _FakeJoin(alive_ticks=25, returncode=0)

        run = self._run(fake, pre_output_timeout=0.2, stall_seconds=30)

        self.assertTrue(run.success,
                        f'a join that never stopped writing was killed: {run.reason} / {run.error_msg}')
        self.assertEqual(run.reason, 'success')
        self.assertFalse(fake.terminated,
                         'a join that never stopped writing was terminated')

    def test_a_join_that_writes_nothing_is_still_killed(self):
        """The pre-output budget is a real rule, not a disabled one - it is what still
        catches a genuinely hung ffmpeg now that the whole-job deadline is gone."""
        fake = _FakeJoin(alive_ticks=None)

        run = self._run(fake, pre_output_timeout=0.3, stall_seconds=30)

        self.assertFalse(run.success)
        self.assertEqual(run.reason, 'no_output')
        self.assertTrue(fake.terminated, 'a join producing nothing was not killed')

    def test_a_join_that_stops_growing_is_killed_as_stalled(self):
        """The other half: once the output file is growing, THIS is the authority. A join
        that started well and then wedged must not run forever."""
        self._grow_output(stop_after=8192)
        fake = _FakeJoin(alive_ticks=None)

        run = self._run(fake, pre_output_timeout=30, stall_seconds=0.4)

        self.assertFalse(run.success)
        self.assertEqual(run.reason, 'stalled')
        self.assertTrue(fake.terminated, 'a wedged join was not killed')
        self.assertIn('concat progress', run.error_msg or '')

    def test_the_stall_clock_does_not_start_before_the_first_byte(self):
        """Stall detection is gated on the join having written something, so the two rules
        cannot both judge the same silent stretch - the pre-output budget owns it."""
        fake = _FakeJoin(alive_ticks=None)

        run = self._run(fake, pre_output_timeout=0.4, stall_seconds=0.1)

        self.assertEqual(run.reason, 'no_output',
                         'a join that had not started was judged as a stall')


class FailedConcatPartialTests(unittest.TestCase):
    """A failed concat removes what it wrote - and only because the segments survive it."""

    def setUp(self):
        self.t = make_test_app()
        self.dvr = os.path.join(self.t._tmpdir, 'dvr')
        os.makedirs(self.dvr, exist_ok=True)
        self.rec = seed.make_recording(status='IN_PROGRESS', name='Long Race')
        db.session.commit()
        self.rid = self.rec.id
        self.segs = []
        for n in (1, 2):
            path = os.path.join(self.dvr, f'seg_{self.rid}_{n}.ts')
            with open(path, 'wb') as fh:
                fh.write(b'\x00' * 4096)
            self.segs.append(path)
            db.session.add(RecordingSegment(
                recording_id=self.rid, segment_number=n, file_path=path,
                started_at=datetime.utcnow(), exit_reason='STOP_TIME_REACHED',
                bytes_recorded=4096))
        db.session.commit()

        self.cfg = load_config()
        self.cfg['recording']['dvr_output_dir'] = self.dvr
        self.cfg['recording']['post_process']['enabled'] = False
        self.cfg['recording']['move_on_complete']['enabled'] = False

    def tearDown(self):
        self.t.cleanup()

    def _run_failing_concat(self, partial_bytes):
        """Drive _run_concatenation with a supervised join that fails after writing
        `partial_bytes` into the reserved output path."""
        written = {}

        def _fake_supervise(cmd, output_path, **kwargs):
            written['path'] = output_path
            with open(output_path, 'wb') as fh:
                fh.write(b'\x00' * partial_bytes)
            return pumod.SupervisedRun('stalled', error_msg='No concat progress for 300s')

        with mock.patch('app.config.load_config', return_value=self.cfg), \
             mock.patch.object(catmod, 'supervise_ffmpeg', side_effect=_fake_supervise), \
             mock.patch('app.recorder.persist_final_thumbnail'), \
             mock.patch('app.recorder.persist_poster_frame'), \
             mock.patch('app.health_score.apply_capture_phase_health_observation'), \
             mock.patch.object(catmod, '_measure_segment_content_durations'):
            catmod._run_concatenation(self.t.app, self.rid, reason='test')
        return written['path']

    def test_a_failed_concat_removes_its_multi_gigabyte_partial(self):
        """The 21.4 GB recording 19 stranded. output_path is committed on success only, so
        nothing references this file and nothing would ever delete it."""
        partial = self._run_failing_concat(8192)

        self.assertFalse(os.path.exists(partial),
                         'the failed concat left its partial output behind')

    def test_a_failed_concat_still_removes_an_empty_placeholder(self):
        """The case that already worked keeps working: the zero-byte reservation would
        otherwise push the next attempt onto a `_2` name."""
        partial = self._run_failing_concat(0)

        self.assertFalse(os.path.exists(partial))

    def test_the_segments_survive_a_failed_concat(self):
        """The premise the removal above rests on. If this ever goes red, the partial is
        the sole copy of the capture and deleting it is no longer safe."""
        self._run_failing_concat(8192)

        for path in self.segs:
            self.assertTrue(os.path.exists(path),
                            f'a failed concat destroyed segment {path}')

    def test_the_row_is_failed_and_names_no_output(self):
        self._run_failing_concat(8192)

        db.session.expire_all()
        row = db.session.get(Recording, self.rid)
        self.assertEqual(row.status, 'FAILED')
        self.assertIsNone(row.output_path)


class ConfigMigrationTests(unittest.TestCase):

    def test_the_retired_whole_job_key_is_dropped(self):
        """Not repointed. The old number answered "how long may this whole join take", a
        question the app no longer asks - as a stall budget it would make a user who had
        raised it wait hours on a wedged join."""
        cfg = {'ffmpeg': {'path': 'ffmpeg', 'concat_timeout_seconds': 9000}}

        out = _cfg_m004_concat_progress_supervision(cfg)

        self.assertNotIn('concat_timeout_seconds', out['ffmpeg'])
        self.assertNotIn('concat_stall_seconds', out['ffmpeg'],
                         'the dropped value was silently repointed at a new key')
        self.assertNotIn('concat_pre_output_timeout_seconds', out['ffmpeg'],
                         'the dropped value was silently repointed at a new key')

    def test_a_config_without_the_key_is_untouched(self):
        cfg = {'ffmpeg': {'path': 'ffmpeg'}}
        self.assertEqual(_cfg_m004_concat_progress_supervision(cfg), cfg)

    def test_the_new_keys_have_defaults(self):
        from app.config import _DEFAULTS
        self.assertEqual(_DEFAULTS['ffmpeg']['concat_pre_output_timeout_seconds'], 300)
        self.assertEqual(_DEFAULTS['ffmpeg']['concat_stall_seconds'], 300)


class SettingsClampTests(unittest.TestCase):
    """Server-side, per CLAUDE.md enforcement-lives-server-side."""

    def setUp(self):
        self.t = make_test_app()

    def tearDown(self):
        self.t.cleanup()

    def _save(self, path, value):
        page = self.t.client.get('/settings').get_data(as_text=True)
        tok = re.search(r'name="csrf-token" content="([^"]+)"', page).group(1)
        with mock.patch('app.routes.settings.save_config', return_value=[]) as save, \
             mock.patch('app.routes.settings.load_for_edit',
                        return_value=({'ffmpeg': {}}, {'ffmpeg': {}})):
            resp = self.t.client.post('/api/settings/field',
                                      json={'path': path, 'value': value},
                                      headers={'X-CSRFToken': tok})
        self.assertEqual(resp.status_code, 200, resp.get_data(as_text=True))
        return save.call_args[0][0]['ffmpeg']

    def test_the_pre_output_budget_has_a_floor_of_one(self):
        """Never a disable switch: a 0 would kill every join on its first poll, before
        ffmpeg could possibly have written anything."""
        saved = self._save('ffmpeg.concat_pre_output_timeout_seconds', 0)
        self.assertEqual(saved['concat_pre_output_timeout_seconds'], 1)

    def test_the_stall_budget_may_be_disabled(self):
        """0 is meaningful here - it is the documented off switch, and the pre-output
        budget then stands in as a whole-job fallback."""
        saved = self._save('ffmpeg.concat_stall_seconds', 0)
        self.assertEqual(saved['concat_stall_seconds'], 0)

    def test_a_negative_stall_budget_is_clamped_to_zero(self):
        saved = self._save('ffmpeg.concat_stall_seconds', -30)
        self.assertEqual(saved['concat_stall_seconds'], 0)


if __name__ == '__main__':
    unittest.main()
