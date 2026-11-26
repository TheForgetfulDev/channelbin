"""The picture-in-picture engine's parts that need no play (app/pip_engine.py,
dev/docs/DESIGN-pip-channels.md 6.1, 6.4, 7.1): the picture buffer and its readers, the small
window's card frame, reading a picture's format from bytes already held, and the GPU checks
with their Readiness line. No dev/docs/BUGS.md entry: a feature.

  python3 -m unittest tests.test_pip_engine_units
"""
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tests.support.app import make_test_app  # noqa: E402

import app.pip_engine as engine  # noqa: E402
from app import config as cfgmod  # noqa: E402
from app import normalize as nz  # noqa: E402
from app import readiness, toolchain  # noqa: E402

DEVICE = '/dev/dri/renderD128'


class PictureBufferTests(unittest.TestCase):
    def setUp(self):
        self.buf = engine.PictureBuffer()
        self.buf.mark()

    def test_two_readers_each_keep_their_own_place(self):
        a, b = self.buf.reader(), self.buf.reader()
        self.buf.append(b'one', now=100.0)
        self.buf.append(b'two', now=100.1)
        self.assertEqual([a.take(0), a.take(0), a.take(0)], [b'one', b'two', None])
        self.assertEqual(b.take(0), b'one', 'what one reader took is still there for the other')

    def test_a_new_reader_starts_six_seconds_back_not_at_the_live_edge(self):
        with mock.patch.object(engine.time, 'monotonic', return_value=110.0):
            for at, data in ((101.0, b'old'), (104.5, b'back'), (109.0, b'new')):
                self.buf.append(data, now=at)
            reader = self.buf.reader()
        self.assertEqual(engine.READER_BACK_SECONDS, 6.0)
        self.assertEqual(reader.take(0), b'back')

    def test_it_keeps_ten_seconds_and_never_more_than_its_cap(self):
        self.buf.append(b'a' * 10, now=100.0)
        self.buf.append(b'b' * 10, now=105.0)
        self.buf.append(b'c' * 10, now=111.0)
        self.assertEqual(self.buf.size, 20, 'the chunk older than BUFFER_KEEP_SECONDS is gone')
        with mock.patch.object(engine, 'BUFFER_MAX_BYTES', 25):
            self.buf.append(b'd' * 10, now=111.1)
        self.assertEqual(self.buf.size, 20)
        self.assertEqual(self.buf.total_in, 40)

    def test_a_reader_that_fell_behind_goes_on_from_the_oldest_byte_held(self):
        reader = self.buf.reader()
        self.buf.append(b'lost', now=100.0)
        self.buf.append(b'kept', now=120.0)
        self.buf.append(b'newest', now=120.1)
        self.assertEqual(reader.take(0), b'kept')
        self.assertEqual(reader.skipped, 1)

    def test_a_new_source_ends_the_readers_of_the_old_one_and_a_new_reader_starts_on_it(self):
        self.buf.append(b'old source', now=100.0)
        reader = self.buf.reader()
        self.buf.mark()
        self.assertIs(reader.take(0), engine.NEW_SOURCE)
        self.assertIs(reader.take(0), engine.NEW_SOURCE, 'and every time after')
        with mock.patch.object(engine.time, 'monotonic', return_value=100.5):
            self.buf.append(b'new source', now=100.4)
            fresh = self.buf.reader()
        self.assertEqual(fresh.take(0), b'new source', 'never the old source\'s bytes, however recent')

    def test_ready_once_a_source_holds_enough_or_has_been_arriving_long_enough(self):
        self.assertFalse(self.buf.ready(now=100.0))
        self.buf.append(b'x' * 10, now=100.0)
        self.assertFalse(self.buf.ready(now=100.1))
        self.assertTrue(self.buf.ready(now=100.0 + engine.READY_SECONDS))
        self.buf.mark()
        self.assertFalse(self.buf.ready(now=200.0), 'a new source starts from nothing')
        self.buf.append(b'x' * engine.READY_BYTES, now=200.0)
        self.assertTrue(self.buf.ready(now=200.0))

    def test_the_snapshot_is_the_current_sources_start_up_to_a_limit(self):
        self.buf.append(b'old', now=100.0)
        self.buf.mark()
        self.buf.append(b'abc', now=100.1)
        self.buf.append(b'def', now=100.2)
        self.assertEqual(self.buf.snapshot(), b'abcdef')
        self.assertEqual(self.buf.snapshot(limit=4), b'abcd')

    def test_it_answers_what_the_relay_asks_of_a_ring_and_nothing_a_reader_does_reaches_it(self):
        self.buf.append(b'x' * 100, now=100.0)
        self.buf.clear()
        self.buf.set_reserve(8.0)
        self.assertEqual((self.buf.size, self.buf.reserve), (100, 0.0), 'a stopped session leaves its bytes')
        self.assertEqual((self.buf.dropped, self.buf.dropped_unripe, self.buf.released_early), (0, 0, 0))
        self.assertEqual(self.buf.untaken_age(), 0.0, 'a picture is never an idle IPTV player')


class SilentCombinerTests(unittest.TestCase):
    """Combined.silent_for(), the combiner hang check (dev/docs/BUGS.md 2026-11-25 @ 08:02:19 PM),
    on a play stood in for by its fields."""

    def _combined(self):
        c = object.__new__(engine.Combined)
        c.session = SimpleNamespace(last_data_mono=100.0)
        c.big = SimpleNamespace(live=True, buffer=SimpleNamespace(total_in=1000, source_no=1))
        c.big_now = (engine.INPUT_LIVE, 1)
        c._out_mark = (None, 0, None)
        return c

    def test_silence_runs_from_the_big_picture_arriving_again_not_from_the_last_output(self):
        c = self._combined()
        self.assertIsNone(c.silent_for(100.5))          # the output's mark
        self.assertIsNone(c.silent_for(110.0), 'the big picture paused: its stall, not the combiner\'s')
        c.big.buffer.total_in = 1500                     # it comes back 29 s after the last output
        self.assertEqual(c.silent_for(129.0), 0.0)
        self.assertEqual(c.silent_for(140.0), 11.0)

    def test_output_moving_again_resets_it(self):
        c = self._combined()
        c.silent_for(100.5)
        c.big.buffer.total_in = 1500
        self.assertEqual(c.silent_for(101.0), 0.0)
        self.assertEqual(c.silent_for(101.5), 0.5)
        c.session.last_data_mono = 102.0
        self.assertIsNone(c.silent_for(102.5))

    def test_a_big_picture_between_sources_or_on_its_card_never_counts(self):
        c = self._combined()
        c.silent_for(100.5)
        c.big.buffer.total_in = 1500
        c.big.buffer.source_no = 2
        self.assertIsNone(c.silent_for(140.0))
        c.big.buffer.source_no = 1
        c.big_now = (engine.INPUT_CARD, 'over', 'A game', '')
        self.assertIsNone(c.silent_for(140.0))


class CardFrameTests(unittest.TestCase):
    def test_a_card_is_one_nv12_frame_of_the_windows_size_in_tv_range(self):
        frame = engine.card_frame('Paused for a recording', 480, 270)
        self.assertEqual(len(frame), 480 * 270 * 3 // 2)
        luma, chroma = frame[:480 * 270], frame[480 * 270:]
        self.assertGreaterEqual(min(luma), 16)
        self.assertLessEqual(max(luma), 235)
        self.assertGreater(max(luma), 180, 'the words are drawn light')
        self.assertLess(luma[0], 60, 'on a dark ground')
        self.assertTrue(all(100 < c < 156 for c in chroma[:64]), 'the ground is near neutral')

    def test_different_words_are_different_frames_and_the_same_words_are_drawn_once(self):
        a = engine.card_frame('Reconnecting', 640, 360)
        self.assertIsNot(a, engine.card_frame('Starts at 4:25 PM', 640, 360))
        self.assertNotEqual(a, engine.card_frame('Starts at 4:25 PM', 640, 360))
        self.assertIs(a, engine.card_frame('Reconnecting', 640, 360))

    def test_long_words_wrap_inside_the_window(self):
        frame = engine.card_frame('Paused: the big picture needed its connection', 480, 270)
        rows = [frame[y * 480:(y + 1) * 480] for y in range(270)]
        lit = [y for y, row in enumerate(rows) if max(row) > 150]
        self.assertGreater(lit[-1] - lit[0], 270 // 9 * 1.5, 'more than one line of words')
        self.assertTrue(all(max(row[:12]) < 60 and max(row[-12:]) < 60 for row in rows), 'none at the edges')


@unittest.skipUnless(shutil.which('ffmpeg') and shutil.which('ffprobe'), 'ffmpeg not available')
class ProbePictureTests(unittest.TestCase):
    CFG = {'ffmpeg': {'path': 'ffmpeg'}}

    def _ts(self, size, extra=()):
        d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, d, ignore_errors=True)
        path = os.path.join(d, 'x.ts')
        subprocess.run(['ffmpeg', '-v', 'error', '-y', '-f', 'lavfi', '-i', f'testsrc=size={size}:rate=25',
                        '-t', '2', '-c:v', 'libx264', '-preset', 'ultrafast', '-pix_fmt', 'yuv420p',
                        *extra, '-f', 'mpegts', path], check=True, timeout=120)
        with open(path, 'rb') as fh:
            return fh.read()

    def test_the_streams_own_matrix_wins_and_an_untagged_one_goes_by_its_size(self):
        self.assertEqual(engine.probe_picture(self.CFG, self._ts('320x240')), (nz.MATRIX_BT601, False))
        self.assertEqual(engine.probe_picture(self.CFG, self._ts('1280x720')), (nz.MATRIX_BT709, False))
        tagged = self._ts('320x240', ('-colorspace', 'bt709', '-color_primaries', 'bt709', '-color_trc', 'bt709'))
        self.assertEqual(engine.probe_picture(self.CFG, tagged), (nz.MATRIX_BT709, False))

    def test_an_interlaced_stream_is_named_interlaced(self):
        data = self._ts('720x576', ('-flags', '+ilme+ildct', '-top', '1', '-x264opts', 'tff=1'))
        self.assertEqual(engine.probe_picture(self.CFG, data), (nz.MATRIX_BT601, True))

    def test_bytes_that_are_no_stream_are_bt709_and_not_interlaced_and_nothing_raises(self):
        self.assertEqual(engine.probe_picture(self.CFG, b'\x00' * 4096), (nz.MATRIX_BT709, False))
        self.assertEqual(engine.probe_picture({'ffmpeg': {'ffprobe_path': '/nowhere/ffprobe'}}, b'x'),
                         (nz.MATRIX_BT709, False))


def _trial(rc, tail=''):
    calls = []

    def run(ffmpeg_path, device, *a):
        calls.append((ffmpeg_path, device))
        return rc, tail
    run.calls = calls
    return run


class GpuOverlayTrialTests(unittest.TestCase):
    def setUp(self):
        self.t = make_test_app()
        self.addCleanup(self.t.cleanup)
        toolchain.reset_cache()
        self.addCleanup(toolchain.reset_cache)

    def test_the_trial_lays_one_uploaded_picture_over_another_and_encodes_it(self):
        cmd = toolchain.gpu_overlay_trial_cmd('ffmpeg', DEVICE)
        self.assertEqual(cmd[cmd.index('-vaapi_device') + 1], DEVICE)
        graph = cmd[cmd.index('-filter_complex') + 1]
        self.assertEqual(graph.count('hwupload'), 2)
        self.assertIn('overlay_vaapi', graph)
        self.assertIn('h264_vaapi', cmd)
        self.assertEqual(cmd[-3:], ['-f', 'null', '-'])

    def test_a_pass_is_cached_and_a_failure_is_asked_again(self):
        ok = _trial(0)
        with mock.patch.object(toolchain, '_run_gpu_overlay_trial', ok):
            first = toolchain.check_gpu_overlay('ffmpeg', DEVICE)
            second = toolchain.check_gpu_overlay('ffmpeg', DEVICE)
        self.assertTrue(first.ok and second.ok and second.cached)
        self.assertEqual(len(ok.calls), 1)
        toolchain.reset_cache()
        bad = _trial(234, 'No such filter: overlay_vaapi')
        with mock.patch.object(toolchain, '_run_gpu_overlay_trial', bad):
            one = toolchain.check_gpu_overlay('ffmpeg', DEVICE)
            toolchain.check_gpu_overlay('ffmpeg', DEVICE)
        self.assertFalse(one.ok)
        self.assertIn('overlay_vaapi', one.error)
        self.assertEqual(len(bad.calls), 2)

    def test_an_encode_pass_is_never_read_as_an_overlay_pass(self):
        with mock.patch.object(toolchain, '_run_gpu_trial', _trial(0)):
            toolchain.check_gpu_encoder('ffmpeg', DEVICE, 'h264', report=False)
        bad = _trial(1, 'overlay failed')
        with mock.patch.object(toolchain, '_run_gpu_overlay_trial', bad):
            self.assertFalse(toolchain.check_gpu_overlay('ffmpeg', DEVICE).ok)
        self.assertEqual(len(bad.calls), 1)

    def _state(self, device_exists=True, encode=0, overlay=0, tail=''):
        cfg = cfgmod._deep_merge(cfgmod.load_config(), {'recording': {'post_process': {'vaapi_device': DEVICE}}})
        with mock.patch.object(engine.os.path, 'exists', return_value=device_exists), \
                mock.patch.object(toolchain, '_run_gpu_trial', _trial(encode, tail)), \
                mock.patch.object(toolchain, '_run_gpu_overlay_trial', _trial(overlay, tail)):
            return engine.gpu_state(cfg), engine.gpu_notice(cfg)

    def test_the_engine_combines_only_when_the_device_is_there_and_passes_both_trials(self):
        self.assertEqual(self._state(), ((DEVICE, ''), ''))
        (device, why), _notice = self._state(device_exists=False)
        self.assertEqual(device, '')
        self.assertIn('no GPU device', why)
        toolchain.reset_cache()
        (device, why), notice = self._state(encode=1, tail='Failed to open the VA display')
        self.assertEqual(device, '')
        self.assertIn('test encode', why)
        self.assertEqual(notice, why, 'the page says what the last tune-in found, and runs nothing')
        toolchain.reset_cache()
        (device, why), notice = self._state(overlay=1, tail='No such filter: overlay_vaapi')
        self.assertEqual(device, '')
        self.assertIn('combining two pictures', why)
        self.assertIn('overlay_vaapi', notice)

    def test_a_passing_gpu_is_tried_once_however_often_a_play_asks(self):
        """Characterization (dev/changelog/1737): a card play asks gpu_state() at every
        re-check and every tune-in asks it too, and the two trials' passes are kept for the
        life of the process (toolchain._gpu_trial), so the asking spawns nothing after the
        first pass. A cache dropped here would put two GPU processes on every 30 s re-check."""
        cfg = cfgmod._deep_merge(cfgmod.load_config(), {'recording': {'post_process': {'vaapi_device': DEVICE}}})
        encode, overlay = _trial(0), _trial(0)
        with mock.patch.object(engine.os.path, 'exists', return_value=True), \
                mock.patch.object(toolchain, '_run_gpu_trial', encode), \
                mock.patch.object(toolchain, '_run_gpu_overlay_trial', overlay):
            answers = [engine.gpu_state(cfg) for _ in range(5)]
        self.assertEqual(answers, [(DEVICE, '')] * 5)
        self.assertEqual((len(encode.calls), len(overlay.calls)), (1, 1))

    def test_a_conversion_running_does_not_stop_a_play_combining(self):
        """Characterization: the engine asks nothing about conversions today, so this passes
        until someone makes it yield as the browser preview does, which is the decision it
        holds (dev/docs/DESIGN-pip-channels.md 7.3)."""
        import app.postprocessor as pp
        with mock.patch.object(pp, 'has_active_conversion', return_value=True):
            self.assertEqual(self._state()[0], (DEVICE, ''))

    def test_the_pages_notice_starts_no_process(self):
        cfg = cfgmod._deep_merge(cfgmod.load_config(), {'recording': {'post_process': {'vaapi_device': DEVICE}}})
        run = _trial(1, 'x')
        with mock.patch.object(engine.os.path, 'exists', return_value=True), \
                mock.patch.object(toolchain, '_run_gpu_trial', run), \
                mock.patch.object(toolchain, '_run_gpu_overlay_trial', run):
            self.assertEqual(engine.gpu_notice(cfg), '')
        self.assertEqual(run.calls, [])

    # -- Readiness ---------------------------------------------------------------------------

    def _ctx(self, device=DEVICE, found=True):
        cfg = cfgmod._deep_merge(cfgmod.load_config(), {'recording': {'post_process': {'vaapi_device': device}}})
        return SimpleNamespace(cfg=cfg, tools={'ffmpeg': {'found': found, 'path': '/x/ffmpeg'}})

    def _pip(self):
        import app.pip_channels as pip
        from app.database import PipChannel
        from app import db
        db.session.add(PipChannel(name='PiP 1', layout='corner', corner='br', size='m'))
        db.session.commit()
        return pip

    def test_with_no_pip_channel_the_check_is_ready_and_starts_nothing(self):
        run = _trial(1, 'x')
        with mock.patch.object(toolchain, '_run_gpu_overlay_trial', run):
            got = readiness._check_gpu_overlay(self._ctx())
        self.assertEqual(got.status, readiness.READY)
        self.assertIn('Not in use', got.found)
        self.assertEqual(run.calls, [])

    def test_with_a_pip_channel_a_pass_is_ready_and_a_failure_needs_attention_and_says_what_plays(self):
        self._pip()
        with mock.patch.object(readiness.os.path, 'exists', return_value=True):
            with mock.patch.object(toolchain, '_run_gpu_overlay_trial', _trial(0)):
                got = readiness._check_gpu_overlay(self._ctx())
            self.assertEqual(got.status, readiness.READY)
            self.assertIn(DEVICE, got.found)
            toolchain.reset_cache()
            with mock.patch.object(toolchain, '_run_gpu_overlay_trial', _trial(1, 'No such filter')):
                got = readiness._check_gpu_overlay(self._ctx())
        self.assertEqual(got.status, readiness.ATTENTION)
        self.assertIn('big picture alone', got.found)
        self.assertIn('No such filter', got.found)

    def test_with_a_pip_channel_and_no_device_it_needs_attention_without_a_trial(self):
        self._pip()
        run = _trial(0)
        with mock.patch.object(readiness.os.path, 'exists', return_value=False), \
                mock.patch.object(toolchain, '_run_gpu_overlay_trial', run):
            got = readiness._check_gpu_overlay(self._ctx())
        self.assertEqual(got.status, readiness.ATTENTION)
        self.assertIn('no GPU device', got.found)
        self.assertEqual(run.calls, [])

    def test_the_check_is_registered_on_demand(self):
        check = next(c for c in readiness.CHECKS if c.id == 'gpu_overlay')
        self.assertEqual(check.cost, readiness.ON_DEMAND)


if __name__ == '__main__':
    unittest.main()
