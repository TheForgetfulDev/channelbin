"""do_postprocess on a real local file with the encode stubbed out, for tests that assert on
the analysis phase or on the conversion command rather than on an encoded output.

The conversion's ffmpeg never runs: run_conversion_supervised writes a stand-in part, the part
join writes a stand-in output, and the returned Mock holds every call, so `call_args_list[0][0][2]`
is the first command the conversion would have run. Everything before the encode - the health
read, the damage scan, the picture-and-sound gap read, the format survey - runs for real on
the file. Shared by tests/test_conversion_gap_fill.py and tests/test_track_gaps.py
(dev/changelog/1649); tests/test_normalize_conversion.py and tests/test_converted_output_check.py
still carry variants of their own (GPU trial answers per codec, dying attempts, produced files).
"""
from unittest import mock

import app.config as cfgmod
import app.postprocessor as ppmod
from app import db, toolchain


def postprocess_config(gather_health_data=False, **pp_overrides):
    """The live config with post-processing set to a software mp4 conversion that moves and
    scripts nothing. `pp_overrides` replace recording.post_process keys."""
    pp = dict({'enabled': True, 'format': 'mp4', 'delete_source': False,
               'reencode_mode': 'damaged', 'pre_output_timeout_seconds': 60,
               'auto_restart': True, 'max_restart_attempts': 3, 'stall_seconds': 0,
               'progress_interval_seconds': 5, 'video_encoder': 'software',
               'video_codec': 'h264', 'max_height': 0, 'audio_channels': 'source',
               'loudness_normalize': False, 'vaapi_decode': False}, **pp_overrides)
    return cfgmod._deep_merge(cfgmod.load_config(), {'recording': {
        'gather_health_data': gather_health_data,
        'move_on_complete': {'enabled': False},
        'post_script': {'enabled': False},
        'post_process': pp,
    }})


def stubbed_postprocess(app, recording_id, ts_path, cfg, out_time, trial_rc=0):
    """Run do_postprocess under `cfg` with the encode stubbed; return the
    run_conversion_supervised Mock. `trial_rc` is the GPU trial's exit code."""
    def attempt(app, rid, cmd, part_file, **kw):
        with open(part_file, 'wb') as fh:
            fh.write(b'p' * 64)
        return ppmod.ConversionResult(True, 'success', out_time=out_time)

    def join(parts, output_path, *a, **k):
        with open(output_path, 'wb') as fh:
            fh.write(b'o' * 128)
        return ppmod.PartJoinResult(True, 'success', parts=len(parts))

    stub = mock.Mock(side_effect=attempt)
    with mock.patch.object(cfgmod, 'load_config', return_value=cfg), \
         mock.patch.object(ppmod, 'run_conversion_supervised', stub), \
         mock.patch.object(ppmod, 'join_conversion_parts', join), \
         mock.patch.object(ppmod, 'finalize_part', lambda part, *a, **k: 0.0), \
         mock.patch.object(toolchain, '_run_gpu_trial', lambda *a, **k: (trial_rc, '')):
        ppmod.do_postprocess(app, recording_id, ts_path)
    db.session.expire_all()
    return stub
