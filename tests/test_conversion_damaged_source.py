"""Tier 2 - conversion against a source file that is damaged at a fixed offset
(dev/docs/BUGS.md 2026-08-24, dev/changelog/799).

Recording 4's five-hour .ts held a burst of corrupt AAC around 2h53m. ffmpeg died there
four times in a row - byte-identical output each attempt - because a corrupt frame decoded
to a nonsense channel count, the auto-inserted resampler was torn down and could not be
rebuilt, and a filter-graph reinit failure is fatal. The restart loop then spent its whole
budget reproducing it.

Three invariants are guarded here:
  1. mp4 conversion commands carry the corruption-tolerance flags that stop one bad frame
     killing a whole file (-max_error_rate, -reinit_filter:a).
  2. Two deaths at the same output timestamp end the restart loop immediately and the
     failure message says the source is damaged, rather than burning the budget.
  3. A conversion that finished only by dropping undecodable frames says so - the whole
     point of tolerating damage is that the damage stays visible (product principle 1).
"""
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import app.config as cfgmod  # noqa: E402
import app.postprocessor as ppmod  # noqa: E402
from tests.support.app import make_test_app  # noqa: E402
from tests.support import seed  # noqa: E402
from app import db  # noqa: E402
from app.database import Recording, RecordingEvent, CONVERSION_RESTARTED, CONVERSION_DONE  # noqa: E402
from app.postprocessor import (  # noqa: E402
    ConversionResult, do_postprocess, _count_decode_errors,
    _active_conversions, _active_lock,
)


def _config(**pp_overrides):
    pp = dict({'enabled': True, 'format': 'mp4', 'delete_source': False,
               'reencode_mode': 'never', 'pre_output_timeout_seconds': 60,
               'auto_restart': True, 'max_restart_attempts': 3,
               'stall_seconds': 0, 'progress_interval_seconds': 5}, **pp_overrides)
    return cfgmod._deep_merge(cfgmod.load_config(), {'recording': {
        'gather_health_data': False,
        'move_on_complete': {'enabled': False},
        'post_script': {'enabled': False},
        'post_process': pp,
    }})


class _ConversionCase(unittest.TestCase):
    def setUp(self):
        self.t = make_test_app()
        self.rec = seed.make_recording(status='CONCATENATING', name='conv',
                                       recorded_duration_seconds=18000.0)
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

    def _run(self, cfg, side_effect):
        stub = mock.Mock(side_effect=side_effect)
        with mock.patch.object(cfgmod, 'load_config', return_value=cfg), \
             mock.patch.object(ppmod, 'run_conversion_supervised', stub):
            do_postprocess(self.t.app, self.rid, self.ts)
        db.session.expire_all()
        return stub

    def _events(self, event_type):
        return [e for e in RecordingEvent.query.filter_by(recording_id=self.rid).all()
                if e.event_type == event_type]


class CorruptionToleranceFlagsTests(_ConversionCase):
    """One corrupt frame in a five-hour file must not be able to kill the conversion."""

    def _cmd_for(self, **pp):
        stub = self._run(_config(**pp), lambda *a, **k: ConversionResult(True, 'success'))
        return stub.call_args[0][2]

    def test_stream_copy_mp4_carries_tolerance_flags(self):
        cmd = self._cmd_for(reencode_mode='never')
        self.assertIn('-max_error_rate', cmd,
                      'without it ffmpeg aborts once 2/3 of frames in its window fail to decode')
        self.assertEqual(cmd[cmd.index('-max_error_rate') + 1], '1.0')

    def test_reencode_mp4_carries_tolerance_flags(self):
        cmd = self._cmd_for(reencode_mode='always')
        self.assertIn('-c:v', cmd)
        self.assertIn('libx264', cmd, 'sanity: this is the re-encode path')
        self.assertIn('-max_error_rate', cmd)

    def test_tolerance_flags_precede_the_input(self):
        cmd = self._cmd_for(reencode_mode='never')
        self.assertLess(cmd.index('-max_error_rate'), cmd.index('-i'))

    def test_reinit_filter_is_never_disabled(self):
        # -reinit_filter:a 0 stops a corrupt frame's nonsense layout tearing down the
        # resampler, but it equally stops a LEGITIMATE mid-stream layout change (stereo
        # -> 5.1 at a program boundary) reconfiguring it, which truncates the audio and
        # fails the conversion. Measured on ffmpeg 6.1.1, dev/changelog/800.
        for mode in ('never', 'always'):
            cmd = self._cmd_for(reencode_mode=mode)
            self.assertNotIn('-reinit_filter:a', cmd,
                             f'{mode}: trades a common working case for a rare broken one')


class RepeatedDeathPositionTests(_ConversionCase):
    """A restart re-reads the same static file from byte zero, so a defect IN the file
    stops every attempt at the same place. Retrying is only worth it for a transient
    cause, which lands somewhere new each time."""

    def test_same_stop_point_twice_falls_back_then_ends_the_loop(self):
        # Spawn 1 dies, spawn 2 dies at the same place -> fall back to audio copy,
        # spawn 3 dies there too -> give up. Never the full 4-spawn budget.
        cfg = _config(max_restart_attempts=3)
        stub = self._run(cfg, lambda *a, **k: ConversionResult(
            False, 'died', 'Conversion failed!', out_time=10382.06))
        rec = db.session.get(Recording, self.rid)
        self.assertEqual(rec.status, 'FAILED')
        self.assertEqual(stub.call_count, 3,
                         'one ordinary restart, one fallback, then stop')

    def test_failure_message_names_the_damaged_position(self):
        cfg = _config(max_restart_attempts=3)
        self._run(cfg, lambda *a, **k: ConversionResult(
            False, 'died', 'Conversion failed!', out_time=10382.06))
        done = self._events(CONVERSION_DONE)
        self.assertEqual(len(done), 1)
        self.assertIn('2h 53m 2s', done[0].detail,
                      'the operator must be told WHERE the source is damaged')
        self.assertIn('same point', done[0].detail)


class AudioCopyFallbackTests(_ConversionCase):
    """Re-encoding audio is what cannot get past a corrupt frame; copying it never
    decodes, so the same bytes pass straight through. Tried once, and only after the
    source has been shown to be the cause."""

    def test_fallback_switches_the_audio_to_a_copy(self):
        cfg = _config(max_restart_attempts=3)
        stub = self._run(cfg, lambda *a, **k: ConversionResult(
            False, 'died', 'Conversion failed!', out_time=10382.06))
        first = stub.call_args_list[0][0][2]
        last = stub.call_args_list[-1][0][2]
        self.assertIn('aac', first, 'the first attempt re-encodes audio')
        self.assertNotIn('-bsf:a', first)
        self.assertEqual(last[last.index('-c:a') + 1], 'copy')
        self.assertIn('-bsf:a', last)
        self.assertEqual(last[last.index('-bsf:a') + 1], 'aac_adtstoasc',
                         'mp4 needs ADTS AAC converted to raw AAC when copying')

    def test_fallback_keeps_the_video_treatment(self):
        cfg = _config(max_restart_attempts=3, reencode_mode='always')
        stub = self._run(cfg, lambda *a, **k: ConversionResult(
            False, 'died', 'boom', out_time=500.0))
        last = stub.call_args_list[-1][0][2]
        self.assertIn('libx264', last, 'only the audio changes in the fallback')

    def test_fallback_is_announced(self):
        cfg = _config(max_restart_attempts=3)
        self._run(cfg, lambda *a, **k: ConversionResult(
            False, 'died', 'Conversion failed!', out_time=10382.06))
        said = [e for e in self._events(CONVERSION_RESTARTED)
                if 'copied instead' in (e.detail or '')]
        self.assertEqual(len(said), 1)
        self.assertIn('2h 53m 2s', said[0].detail)

    def test_fallback_is_tried_only_once(self):
        cfg = _config(max_restart_attempts=8)
        stub = self._run(cfg, lambda *a, **k: ConversionResult(
            False, 'died', 'boom', out_time=10382.06))
        self.assertEqual(stub.call_count, 3,
                         'a second repeat after the fallback gives up, it does not loop')

    def test_success_after_fallback_discloses_the_copied_audio(self):
        cfg = _config(max_restart_attempts=3)
        outcomes = iter([
            ConversionResult(False, 'died', 'boom', out_time=10382.06),
            ConversionResult(False, 'died', 'boom', out_time=10382.06),
            ConversionResult(True, 'success', out_time=18000.0),
        ])
        self._run(cfg, lambda *a, **k: next(outcomes))
        rec = db.session.get(Recording, self.rid)
        self.assertEqual(rec.status, 'COMPLETED')
        notes = [e for e in self._events('DIAGNOSTICS')
                 if 'copied rather than re-encoded' in (e.detail or '')]
        self.assertEqual(len(notes), 1,
                         'a file whose audio is a copy must say so - a mid-stream format '
                         'change in the source will not decode')

    def test_clean_conversion_never_mentions_the_fallback(self):
        cfg = _config()
        self._run(cfg, lambda *a, **k: ConversionResult(True, 'success', out_time=18000.0))
        notes = [e for e in self._events('DIAGNOSTICS')
                 if 'copied rather than re-encoded' in (e.detail or '')]
        self.assertEqual(notes, [])

    def test_no_fallback_when_the_output_is_not_mp4(self):
        # A .ts/.mkv output is already `-c copy` end to end - nothing decodes, so a
        # repeated death there is not the audio decoder and copying changes nothing.
        cfg = _config(format='mkv', max_restart_attempts=3)
        stub = self._run(cfg, lambda *a, **k: ConversionResult(
            False, 'died', 'boom', out_time=10382.06))
        self.assertEqual(stub.call_count, 2, 'straight to give-up, no fallback attempt')

    def test_moving_stop_point_still_uses_the_full_budget(self):
        # A transient cause (a killed process, a storage blip) dies somewhere new each
        # time. That is what the restart budget is for and it must be left alone.
        cfg = _config(max_restart_attempts=3)
        times = iter([100.0, 900.0, 4000.0, 12000.0])
        stub = self._run(cfg, lambda *a, **k: ConversionResult(
            False, 'died', 'boom', out_time=next(times)))
        self.assertEqual(stub.call_count, 4, 'initial + 3 restarts')
        done = self._events(CONVERSION_DONE)
        self.assertNotIn('same point', done[0].detail)

    def test_stall_at_the_same_point_is_not_treated_as_source_damage(self):
        # A stall is a kill by our own supervisor, not ffmpeg reporting a defect, and its
        # out_time is wherever the kill happened to land. Only 'died' carries the signal.
        cfg = _config(max_restart_attempts=3)
        stub = self._run(cfg, lambda *a, **k: ConversionResult(
            False, 'stalled', 'No conversion progress for 300s', out_time=500.0))
        self.assertEqual(stub.call_count, 4)

    def test_death_without_a_position_does_not_short_circuit(self):
        # A conversion that dies before emitting any progress has out_time 0/None. Two of
        # those are not evidence of anything and must still get the full budget.
        cfg = _config(max_restart_attempts=3)
        stub = self._run(cfg, lambda *a, **k: ConversionResult(
            False, 'died', 'ffmpeg exited 1', out_time=0.0))
        self.assertEqual(stub.call_count, 4)


class DecodeErrorDisclosureTests(_ConversionCase):
    """Tolerating damage is only acceptable because the damage is still reported."""

    def test_successful_conversion_reports_dropped_frames(self):
        cfg = _config()
        self._run(cfg, lambda *a, **k: ConversionResult(
            True, 'success', out_time=18000.0, decode_errors=47))
        rec = db.session.get(Recording, self.rid)
        self.assertEqual(rec.status, 'COMPLETED')
        diags = [e for e in self._events('DIAGNOSTICS')
                 if 'concealed damage' in (e.detail or '')]
        self.assertEqual(len(diags), 1,
                         'a file that only converted by dropping frames must say so')
        self.assertIn('47', diags[0].detail)
        self.assertIn('conversion_decode_errors', diags[0].extra_data)

    def test_clean_conversion_stays_quiet(self):
        cfg = _config()
        self._run(cfg, lambda *a, **k: ConversionResult(
            True, 'success', out_time=18000.0, decode_errors=0))
        diags = [e for e in self._events('DIAGNOSTICS')
                 if 'concealed damage' in (e.detail or '')]
        self.assertEqual(diags, [], 'no damage, no event')

    def test_errors_from_an_earlier_attempt_are_not_lost(self):
        # The successful attempt may be clean while an earlier one hit the damage; the
        # output still came from the same damaged source.
        cfg = _config(max_restart_attempts=3)
        outcomes = iter([
            ConversionResult(False, 'died', 'boom', out_time=100.0, decode_errors=12),
            ConversionResult(True, 'success', out_time=18000.0, decode_errors=0),
        ])
        self._run(cfg, lambda *a, **k: next(outcomes))
        diags = [e for e in self._events('DIAGNOSTICS')
                 if 'concealed damage' in (e.detail or '')]
        self.assertEqual(len(diags), 1)
        self.assertIn('12', diags[0].detail)


class CountDecodeErrorsTests(unittest.TestCase):
    """The count is read from the whole stderr spool, not its tail - the interesting
    damage is usually thousands of lines back."""

    def setUp(self):
        self.t = make_test_app()
        self.path = os.path.join(self.t._tmpdir, 'stderr.log')

    def tearDown(self):
        self.t.cleanup()

    def _write(self, text):
        with open(self.path, 'w') as fh:
            fh.write(text)

    def test_counts_both_spellings(self):
        self._write(
            '[aist#0:1/aac] Error submitting packet to decoder: Invalid data found\n'
            'size=  100kB time=00:00:01.00 bitrate=1.0kbits/s speed=1x\n'
            '[aist#0:1/aac] Decoding error: Invalid data found when processing input\n'
            '[aist#0:1/aac] Error submitting packet to decoder: Invalid data found\n'
        )
        self.assertEqual(_count_decode_errors(self.path), 3)

    def test_clean_log_counts_zero(self):
        self._write('size=  100kB time=00:00:01.00 bitrate=1.0kbits/s speed=1x\n')
        self.assertEqual(_count_decode_errors(self.path), 0)

    def test_missing_file_is_zero_not_an_error(self):
        self.assertEqual(_count_decode_errors(os.path.join(self.t._tmpdir, 'nope.log')), 0)

    def test_collapsed_repeat_lines_are_counted(self):
        # ffmpeg writes "Last message repeated N times" instead of repeating a line, so
        # counting lines alone reports 1 where the source lost 201 frames.
        self._write(
            '[aist#0:1/aac] Decoding error: Invalid data found\n'
            'Last message repeated 200 times\n'
        )
        self.assertEqual(_count_decode_errors(self.path), 201)

    def test_repeats_of_a_harmless_line_are_not_counted(self):
        # The collapsed line belongs to whatever preceded it. A repeated progress or
        # warning line must not inflate the damage figure.
        self._write(
            '[aac] Reserved bit set.\n'
            'Last message repeated 500 times\n'
            '[aist#0:1/aac] Decoding error: Invalid data found\n'
        )
        self.assertEqual(_count_decode_errors(self.path), 1)

    def test_consecutive_repeat_lines_all_belong_to_the_same_message(self):
        self._write(
            '[aist#0:1/aac] Decoding error: Invalid data found\n'
            'Last message repeated 10 times\n'
            'Last message repeated 12 times\n'
        )
        self.assertEqual(_count_decode_errors(self.path), 23)

    def test_counts_beyond_the_tail_window(self):
        head = '[aist#0:1/aac] Decoding error: Invalid data found\n' * 5
        tail = 'size=  100kB time=00:00:01.00 bitrate=1.0kbits/s speed=1x\n' * 5000
        self._write(head + tail)
        self.assertEqual(_count_decode_errors(self.path), 5,
                         'errors early in a long run must still be counted')


if __name__ == '__main__':
    unittest.main()
