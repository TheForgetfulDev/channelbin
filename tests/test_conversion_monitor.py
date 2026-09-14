"""Tier 2 - supervised conversion monitor (changelog 276 / BUGS.md conversion-monitor entry).

Guards the "a CONVERTING recording whose ffmpeg dies is either restarted (auto_restart on,
budget remaining) or marked FAILED with a CONVERSION_FAILED alert - it never stays CONVERTING
forever" invariant, plus the ETA debounce, the manual-retry counter reset, the double-run
guard, progress persistence, and the runner's death/stall detection (no real ffmpeg - a fake
Popen + a controlled progress file).
"""
import os
import sys
import time
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import app.config as cfgmod  # noqa: E402
import app.postprocessor as ppmod  # noqa: E402
import app.proc_utils as pumod  # noqa: E402
from tests.support.app import make_test_app  # noqa: E402
from tests.support import seed  # noqa: E402
from app import db  # noqa: E402
from app.database import (  # noqa: E402
    Recording, RecordingEvent, Alert, CONVERSION_RESTARTED,
)
from app.postprocessor import (  # noqa: E402
    EtaSmoother, ConversionResult, do_postprocess, run_conversion_supervised,
    _persist_conversion_snapshot, _active_conversions, _active_lock,
)


def _config(**pp_overrides):
    """A full config with post_process under our control and everything else that
    do_postprocess touches (health gather, move, post-script) turned off, so the only
    behavior under test is the conversion restart loop."""
    pp = dict({'enabled': True, 'format': 'mkv', 'delete_source': False,
               'reencode_mode': 'never', 'pre_output_timeout_seconds': 60,
               'auto_restart': True, 'max_restart_attempts': 3,
               'stall_seconds': 0, 'progress_interval_seconds': 5}, **pp_overrides)
    return cfgmod._deep_merge(cfgmod.load_config(), {'recording': {
        'gather_health_data': False,
        'move_on_complete': {'enabled': False},
        'post_script': {'enabled': False},
        'post_process': pp,
    }})


# ── ETA smoother (the [OPUS-GATED] §5 debounce) ───────────────────────────────
class EtaSmootherTests(unittest.TestCase):
    def test_estimating_until_enough_signal(self):
        s = EtaSmoother(expected_duration=1000)
        # First sample: only 5s of wall time, <1% done → None ("estimating").
        self.assertIsNone(s.update(wall_elapsed=5, out_time=2))

    def test_steady_conversion_eta_is_bounded_and_sane(self):
        # 2x realtime, constant: 1000s of media in 500s of wall. ETA should trend down
        # smoothly, never swinging wildly between adjacent 5s samples.
        s = EtaSmoother(expected_duration=1000)
        etas = []
        for wall in range(5, 500, 5):
            out_time = wall * 2.0  # constant 2x speed
            eta = s.update(wall_elapsed=wall, out_time=out_time)
            if eta is not None:
                etas.append(eta)
        self.assertTrue(etas, 'smoother never emitted an ETA on a healthy run')
        # No adjacent pair may swing more than the clamp (+rounding slack) allows.
        for a, b in zip(etas, etas[1:]):
            if a >= 60:  # ignore rounding noise on tiny end-of-run values
                self.assertLessEqual(abs(b - a) / a, 0.35,
                                     f'ETA swung {a}->{b}s between adjacent samples (yo-yo)')

    def test_midrun_slowdown_does_not_spike_eta(self):
        # Runs 4x for a while, then abruptly drops to 0.5x. A naive instantaneous ETA
        # would spike enormously on the first slow sample; the smoother must ramp.
        s = EtaSmoother(expected_duration=2000)
        out_time = 0.0
        prev = None
        wall = 0
        spikes = []
        for i in range(1, 120):
            wall = i * 5
            rate = 4.0 if wall < 200 else 0.5
            out_time += rate * 5
            eta = s.update(wall_elapsed=wall, out_time=out_time)
            if eta is not None and prev is not None and prev >= 60:
                spikes.append(abs(eta - prev) / prev)
            if eta is not None:
                prev = eta
        self.assertTrue(spikes)
        self.assertLessEqual(max(spikes), 0.35,
                             'a mid-run slowdown made the ETA spike between two samples')


# ── Restart budget / give-up / counter / alert ────────────────────────────────
class RestartBudgetTests(unittest.TestCase):
    def setUp(self):
        self.t = make_test_app()
        self.rec = seed.make_recording(status='CONCATENATING', name='conv',
                                       recorded_duration_seconds=100.0)
        # A real .ts source on disk (success path calls getsize; give-up path keeps it).
        self.ts = os.path.join(self.t._tmpdir, f'rec_{self.rec.id}.ts')
        with open(self.ts, 'wb') as fh:
            fh.write(b'x' * 2048)
        # output_path is the .ts while CONVERTING (rewritten to .mkv only on success).
        self.rec.output_path = self.ts
        db.session.commit()
        self.rid = self.rec.id

    def tearDown(self):
        with _active_lock:
            _active_conversions.clear()
            ppmod._cancel_requested.clear()
        self.t.cleanup()

    def _run(self, cfg, supervised_side_effect):
        stub = mock.Mock(side_effect=supervised_side_effect)
        with mock.patch.object(cfgmod, 'load_config', return_value=cfg), \
             mock.patch.object(ppmod, 'run_conversion_supervised', stub):
            do_postprocess(self.t.app, self.rid, self.ts)
        db.session.expire_all()
        return stub

    def _counts(self):
        events = RecordingEvent.query.filter_by(recording_id=self.rid).all()
        restarts = sum(1 for e in events if e.event_type == CONVERSION_RESTARTED)
        alerts = Alert.query.filter_by(alert_type='CONVERSION_FAILED',
                                       recording_id=self.rid).count()
        return restarts, alerts

    def test_gives_up_and_alerts_after_budget(self):
        cfg = _config(max_restart_attempts=3, auto_restart=True)
        stub = self._run(cfg, lambda *a, **k: ConversionResult(False, 'died', 'boom'))
        rec = db.session.get(Recording, self.rid)
        self.assertEqual(rec.status, 'FAILED')
        self.assertEqual(rec.conversion_attempts, 3, 'counter should end at the budget')
        self.assertEqual(stub.call_count, 4, 'expected initial + 3 restarts = 4 spawns')
        restarts, alerts = self._counts()
        self.assertEqual(restarts, 3, 'one CONVERSION_RESTARTED per restart')
        self.assertEqual(alerts, 1, 'exactly one CONVERSION_FAILED alert on give-up')
        # The source .ts must survive so a manual retry still works.
        self.assertTrue(os.path.exists(self.ts))

    def test_auto_restart_disabled_fails_after_first_death(self):
        cfg = _config(max_restart_attempts=0, auto_restart=True)
        stub = self._run(cfg, lambda *a, **k: ConversionResult(False, 'died', 'boom'))
        rec = db.session.get(Recording, self.rid)
        self.assertEqual(rec.status, 'FAILED')
        self.assertEqual(rec.conversion_attempts, 0)
        self.assertEqual(stub.call_count, 1, 'no restarts when max_restart_attempts=0')
        restarts, alerts = self._counts()
        self.assertEqual(restarts, 0)
        self.assertEqual(alerts, 1)

    def test_success_first_try_completes_no_alert(self):
        cfg = _config(max_restart_attempts=3)
        stub = self._run(cfg, lambda *a, **k: ConversionResult(True, 'success'))
        rec = db.session.get(Recording, self.rid)
        self.assertEqual(rec.status, 'COMPLETED')
        self.assertEqual(stub.call_count, 1)
        restarts, alerts = self._counts()
        self.assertEqual((restarts, alerts), (0, 0))

    def test_cancel_aborts_without_restarting(self):
        cfg = _config(max_restart_attempts=3)

        def _cancel_and_die(*a, **k):
            # Simulate the user cancelling mid-attempt: the flag is set, ffmpeg is killed,
            # the runner reports a death. The loop must abort, not restart.
            with _active_lock:
                ppmod._cancel_requested.add(self.rid)
            return ConversionResult(False, 'died', 'killed by cancel')

        stub = self._run(cfg, _cancel_and_die)
        rec = db.session.get(Recording, self.rid)
        self.assertEqual(rec.status, 'ABORTED', 'a cancelled conversion becomes ABORTED')
        self.assertEqual(stub.call_count, 1, 'must not restart after a cancel')
        restarts, alerts = self._counts()
        self.assertEqual((restarts, alerts), (0, 0), 'cancel is not a failure - no restart, no alert')

    def test_restarts_then_succeeds(self):
        cfg = _config(max_restart_attempts=3)
        outcomes = iter([ConversionResult(False, 'died', 'boom'),
                         ConversionResult(False, 'stalled', 'no progress'),
                         ConversionResult(True, 'success')])
        stub = self._run(cfg, lambda *a, **k: next(outcomes))
        rec = db.session.get(Recording, self.rid)
        self.assertEqual(rec.status, 'COMPLETED')
        self.assertEqual(rec.conversion_attempts, 2, 'two restarts before success')
        self.assertEqual(stub.call_count, 3)
        restarts, alerts = self._counts()
        self.assertEqual((restarts, alerts), (2, 0))


# ── Manual retry resets the counter; double-run guard ─────────────────────────
class RetryAndGuardTests(unittest.TestCase):
    def setUp(self):
        self.t = make_test_app()
        self.t.app.config['WTF_CSRF_ENABLED'] = False
        self.rec = seed.make_recording(status='FAILED', name='conv', conversion_attempts=3)
        self.ts = os.path.join(self.t._tmpdir, f'rec_{self.rec.id}.ts')
        with open(self.ts, 'wb') as fh:
            fh.write(b'x' * 2048)
        self.rec.output_path = self.ts
        db.session.commit()
        self.rid = self.rec.id

    def tearDown(self):
        with _active_lock:
            _active_conversions.clear()
            ppmod._cancel_requested.clear()
        self.t.cleanup()

    def test_retry_resets_counter_and_relaunches(self):
        with mock.patch.object(ppmod, 'do_postprocess') as launched:
            resp = self.t.client.post(f'/recordings/{self.rid}/retry-convert')
        self.assertEqual(resp.status_code, 302)
        db.session.expire_all()
        rec = db.session.get(Recording, self.rid)
        # A retry re-enters post-processing at its analysis phase, not at the conversion
        # (dev/changelog/867) - the .ts is read back in full before any ffmpeg is spawned.
        self.assertEqual(rec.status, 'ANALYZING')
        self.assertEqual(rec.conversion_attempts, 0, 'a manual retry buys a fresh budget')
        # Give the daemon thread a beat to call the patched target.
        for _ in range(50):
            if launched.called:
                break
            time.sleep(0.01)
        self.assertTrue(launched.called, 'retry did not launch a conversion thread')

    def test_retry_refused_when_conversion_live(self):
        with _active_lock:
            _active_conversions[self.rid] = object()  # a live conversion for this id
        with mock.patch.object(ppmod, 'do_postprocess') as launched:
            resp = self.t.client.post(f'/recordings/{self.rid}/retry-convert')
        self.assertEqual(resp.status_code, 302)
        db.session.expire_all()
        rec = db.session.get(Recording, self.rid)
        self.assertEqual(rec.status, 'FAILED', 'must not flip to CONVERTING while one is live')
        self.assertEqual(rec.conversion_attempts, 3, 'counter untouched on a refused retry')
        self.assertFalse(launched.called)

    def test_do_postprocess_refuses_duplicate_run(self):
        with _active_lock:
            _active_conversions[self.rid] = object()
        with mock.patch.object(ppmod, 'run_conversion_supervised') as supervised, \
             mock.patch.object(cfgmod, 'load_config', return_value=_config()):
            do_postprocess(self.t.app, self.rid, self.ts)
        self.assertFalse(supervised.called, 'a second ffmpeg must not spawn for a live id')


# ── Cancel conversion ─────────────────────────────────────────────────────────
class CancelConversionTests(unittest.TestCase):
    def setUp(self):
        self.t = make_test_app()
        self.t.app.config['WTF_CSRF_ENABLED'] = False
        self.rec = seed.make_recording(status='CONVERTING', name='conv', conversion_attempts=1)
        self.ts = os.path.join(self.t._tmpdir, f'rec_{self.rec.id}.ts')
        with open(self.ts, 'wb') as fh:
            fh.write(b'x' * 2048)
        self.rec.output_path = self.ts
        db.session.commit()
        self.rid = self.rec.id

    def tearDown(self):
        with _active_lock:
            _active_conversions.clear()
            ppmod._cancel_requested.clear()
        self.t.cleanup()

    def test_request_cancel_returns_false_when_none_live(self):
        from app.postprocessor import request_cancel_conversion
        self.assertFalse(request_cancel_conversion(self.rid),
                         'no live conversion → caller must mark the row itself')

    def test_cancel_endpoint_aborts_stranded_row_and_deletes_partial(self):
        # A stranded CONVERTING row with no live ffmpeg: the endpoint marks it itself and
        # deletes the partial .mp4 sitting next to the .ts.
        partial = os.path.splitext(self.ts)[0] + '.mp4'
        with open(partial, 'wb') as fh:
            fh.write(b'garbage')
        resp = self.t.client.post(f'/recordings/{self.rid}/cancel-convert')
        self.assertEqual(resp.status_code, 302)
        db.session.expire_all()
        rec = db.session.get(Recording, self.rid)
        self.assertEqual(rec.status, 'ABORTED')
        self.assertFalse(os.path.exists(partial), 'partial .mp4 should be deleted on cancel')
        self.assertTrue(os.path.exists(self.ts), 'source .ts must be kept for retry')

    def test_cancel_endpoint_refuses_non_converting(self):
        seed_rec = seed.make_recording(status='COMPLETED', name='done')
        db.session.commit()
        resp = self.t.client.post(f'/recordings/{seed_rec.id}/cancel-convert')
        self.assertEqual(resp.status_code, 302)
        db.session.expire_all()
        self.assertEqual(db.session.get(Recording, seed_rec.id).status, 'COMPLETED')


# ── Progress persistence ──────────────────────────────────────────────────────
class ProgressPersistenceTests(unittest.TestCase):
    def setUp(self):
        self.t = make_test_app()
        self.rec = seed.make_recording(status='CONVERTING', name='conv')
        db.session.commit()
        self.rid = self.rec.id

    def tearDown(self):
        self.t.cleanup()

    def test_snapshot_written_to_row(self):
        _persist_conversion_snapshot(self.rid, pct=42.5, size=1234567, eta=90)
        db.session.expire_all()
        rec = db.session.get(Recording, self.rid)
        self.assertAlmostEqual(rec.conversion_progress_pct, 42.5)
        self.assertEqual(rec.conversion_out_size, 1234567)
        self.assertEqual(rec.conversion_eta_seconds, 90)
        self.assertIsNotNone(rec.conversion_updated_at)


# ── Runner death / stall detection (fake Popen, no real ffmpeg) ───────────────
class _FakePopen:
    """Stands in for a conversion ffmpeg. alive_ticks=None → never exits on its own;
    otherwise poll() returns None for that many calls, then the returncode."""
    def __init__(self, *args, alive_ticks=None, returncode=0, **kwargs):
        self._polls = 0
        self._alive_ticks = alive_ticks
        self._rc = returncode
        self.returncode = None
        self.terminated = False
        self.signals = []

    def send_signal(self, sig):
        # terminate_or_kill() continues a possibly-suspended child before every teardown
        # (dev/changelog/952), so a fake that cannot take a signal is not a faithful one.
        self.signals.append(sig)

    def poll(self):
        self._polls += 1
        if self._alive_ticks is not None and self._polls > self._alive_ticks:
            self.returncode = self._rc
            return self._rc
        return None

    def terminate(self):
        self.terminated = True
        if self.returncode is None:
            self.returncode = self._rc

    def kill(self):
        self.terminated = True
        if self.returncode is None:
            self.returncode = self._rc

    def wait(self, timeout=None):
        if self.returncode is None:
            self.returncode = self._rc
        return self.returncode


class RunnerDetectionTests(unittest.TestCase):
    def setUp(self):
        self.t = make_test_app()
        self.rec = seed.make_recording(status='CONVERTING', name='conv')
        db.session.commit()
        self.rid = self.rec.id
        self.out = os.path.join(self.t._tmpdir, f'out_{self.rid}.mkv')
        self.cmd = ['/usr/bin/ffmpeg', '-i', 'src.ts', '-c', 'copy', '-y', self.out]

    def tearDown(self):
        with _active_lock:
            _active_conversions.clear()
            ppmod._cancel_requested.clear()
        self.t.cleanup()

    def _run(self, fake, progress_return, stall_seconds=0, pre_output_timeout=30, interval=0.05):
        with mock.patch.object(pumod.subprocess, 'Popen', return_value=fake), \
             mock.patch.object(pumod, 'read_progress_tail', side_effect=progress_return):
            return run_conversion_supervised(
                self.t.app, self.rid, self.cmd, self.out,
                expected_duration=100, pre_output_timeout=pre_output_timeout,
                interval=interval, stall_seconds=stall_seconds)

    def test_detects_process_death(self):
        fake = _FakePopen(alive_ticks=1, returncode=1)
        result = self._run(fake, lambda p: (None, None, False))
        self.assertFalse(result.success)
        self.assertEqual(result.reason, 'died')
        with _active_lock:
            self.assertNotIn(self.rid, _active_conversions)

    def test_detects_clean_success(self):
        fake = _FakePopen(alive_ticks=1, returncode=0)
        result = self._run(fake, lambda p: (50_000_000, 1000, False))
        self.assertTrue(result.success)
        self.assertEqual(result.reason, 'success')

    def test_stall_kills_when_enabled(self):
        # Output started (out_time > 0) then froze → stall detector must kill it.
        fake = _FakePopen(alive_ticks=None)
        result = self._run(fake, lambda p: (5_000_000, 1000, False), stall_seconds=1, interval=0.1)
        self.assertFalse(result.success)
        self.assertEqual(result.reason, 'stalled')
        self.assertTrue(fake.terminated, 'stalled conversion was not killed')

    def test_no_stall_kill_when_disabled(self):
        # Same frozen output, but stall detection off; it finishes on its own.
        fake = _FakePopen(alive_ticks=3, returncode=0)
        result = self._run(fake, lambda p: (5_000_000, 1000, False), stall_seconds=0, interval=0.05)
        self.assertTrue(result.success)

    def test_no_false_stall_before_output_starts(self):
        # out_time stays 0 the whole time (ffmpeg still analysing a badly-damaged source,
        # #64's live false-positive). This is NOT a stall - the stall clock must not start
        # until output begins. The process finishes on its own past stall_seconds.
        fake = _FakePopen(alive_ticks=None)
        # Alive for ~0.6s of no-output, then let the pre-output budget end it cleanly.
        result = self._run(fake, lambda p: (0, 0, False), stall_seconds=0.3,
                           interval=0.05, pre_output_timeout=0.5)
        self.assertEqual(result.reason, 'no_output',
                         'a never-output conversion is caught by the pre-output budget, '
                         'not a false stall')
        self.assertNotEqual(result.reason, 'stalled')


class StaleProgressFileTests(unittest.TestCase):
    """dev/docs/BUGS.md 2026-07-23 "stale -progress file latches the stall monitor".

    The poll loop reads the progress file on its FIRST pass, milliseconds after Popen and
    inside the ~0.25s window before ffmpeg truncates it. On a fixed per-recording filename
    that read returned the PREVIOUS attempt's out_time; GrowthMonitor latched it as a
    high-water mark the new attempt could never beat, and the conversion was killed as
    stalled at exactly stall_seconds. Recording #64 died this way twice, each time ~301s
    after start, ending FAILED with no output from a capture that was fine.

    Unlike RunnerDetectionTests these must NOT patch _read_progress_tail - the real file
    read against a real stale file on disk is the entire point.
    """

    def setUp(self):
        self.t = make_test_app()
        self.rec = seed.make_recording(status='CONVERTING', name='stale')
        db.session.commit()
        self.rid = self.rec.id
        self.out = os.path.join(self.t._tmpdir, f'out_{self.rid}.mkv')
        self.cmd = ['/usr/bin/ffmpeg', '-i', 'src.ts', '-c', 'copy', '-y', self.out]
        # A previous attempt that got 90 minutes into the encode before being killed by a
        # service shutdown, so its finally-block unlink never ran. Both the legacy fixed
        # name and a per-attempt name are seeded.
        self.stale = []
        for name in (f'.conv-progress-{self.rid}.txt',
                     f'.conv-progress-{self.rid}-deadbeef.txt'):
            p = os.path.join(self.t._tmpdir, name)
            with open(p, 'w') as fh:
                fh.write('frame=135000\nout_time_us=5400000000\n'
                         'total_size=999999999\nprogress=continue\n')
            self.stale.append(p)

    def tearDown(self):
        with _active_lock:
            _active_conversions.clear()
            ppmod._cancel_requested.clear()
        self.t.cleanup()

    def _run(self, fake, stall_seconds, pre_output_timeout, interval=0.05):
        with mock.patch.object(pumod.subprocess, 'Popen', return_value=fake):
            return run_conversion_supervised(
                self.t.app, self.rid, self.cmd, self.out,
                expected_duration=100, pre_output_timeout=pre_output_timeout,
                interval=interval, stall_seconds=stall_seconds)

    def test_stale_progress_file_does_not_latch_the_stall_monitor(self):
        # The fake process writes no progress at all, so the only out_time readable
        # anywhere on disk is the stale 5400s one. It must never reach the stall monitor.
        fake = _FakePopen(alive_ticks=None)
        result = self._run(fake, stall_seconds=0.3, pre_output_timeout=1.0)
        self.assertNotEqual(result.reason, 'stalled',
                            'a leftover progress file from an earlier attempt killed a '
                            'healthy conversion as stalled')
        self.assertEqual(result.reason, 'no_output')

    def test_leftover_scratch_files_are_reaped_before_spawn(self):
        self._run(_FakePopen(alive_ticks=1, returncode=0), stall_seconds=0,
                  pre_output_timeout=1.0)
        for p in self.stale:
            self.assertFalse(os.path.exists(p),
                             f'stale scratch file left on disk: {os.path.basename(p)}')

    def test_progress_path_is_unique_per_attempt(self):
        seen = set()

        def _record(argv, *a, **kw):
            seen.add(argv[argv.index('-progress') + 1])
            return _FakePopen(alive_ticks=1, returncode=0)

        for _ in range(3):
            with mock.patch.object(pumod.subprocess, 'Popen', side_effect=_record):
                run_conversion_supervised(
                    self.t.app, self.rid, self.cmd, self.out,
                    expected_duration=100, pre_output_timeout=1.0, interval=0.05,
                    stall_seconds=0)
        self.assertEqual(len(seen), 3, f'attempts reused a progress path: {seen}')


if __name__ == '__main__':
    unittest.main(verbosity=2)
