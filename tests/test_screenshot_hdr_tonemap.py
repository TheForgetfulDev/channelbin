"""Tier 1 unit for the HDR branch of the frame grab (app/screenshot.py).

Guards dev/docs/BUGS.md 2026-09-10 11:41 PM - two defects in one branch, both of which turn
on the same wrong question. The branch asked "is this frame 3840 wide", and answered "then
tonemap it":

  1. A 4K *SDR* source has no PQ curve to undo, so tonemapping it darkens it. Measured on a
     bar pattern whose mean luma is 102: 66 through the chain, a third of the picture's
     brightness gone, and that much closer to the uniformity check that marks a screenshot
     blank and depresses a channel's health score.
  2. A source that states its transfer and nothing else - a real broadcast shape - reached
     zscale with no matrix, primaries or range, and zscale refuses to guess: "code 3074: no
     path between colorspaces" failed the whole graph, and the untonemapped fallback ran.

The invariant, in one sentence: the transfer function decides whether to tonemap, and when
it says yes the chain states every colorspace field zscale needs rather than hoping the
frame carries them (dev/changelog/915).

Fixtures are encoded on this machine with the real ffmpeg (CLAUDE.md: verify external tools
empirically), because which colorspace tags survive into a bitstream is exactly the kind of
thing that cannot be asserted from documentation. The classes needing them self-skip without
ffmpeg or libx265; the detection and command-shape tests below need neither and always run.
"""
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.screenshot import (  # noqa: E402
    UNSPECIFIED_COLOR, capture_screenshot, hdr_input_params)

# Gated on a SYSTEM ffmpeg, for the reason spelled out in
# tests/test_screenshot_keyframe_capture.py: a resolver whose job is to supply a path is the
# wrong source for "is this tool installed".
_FFMPEG = 'ffmpeg'
_HAVE_FFMPEG = bool(shutil.which('ffmpeg') and shutil.which('ffprobe'))


def _have_x265() -> bool:
    if not _HAVE_FFMPEG:
        return False
    try:
        r = subprocess.run([_FFMPEG, '-hide_banner', '-encoders'],
                           capture_output=True, timeout=30)
    except (OSError, subprocess.SubprocessError):
        return False
    return b'libx265' in r.stdout


_HAVE_X265 = _have_x265()

# The chain as it stood before dev/changelog/915 - no declaration of what is coming in.
PRE_FIX_TONEMAP_VF = ('scale=1920:1080,zscale=t=linear:npl=100,format=gbrpf32le,'
                      'zscale=p=bt709,tonemap=hable,zscale=t=bt709:m=bt709:r=tv,'
                      'format=yuv420p')


class HdrDetectionTests(unittest.TestCase):
    """Only a transfer function proves HDR - never a frame size."""

    def test_width_alone_is_not_hdr(self):
        self.assertIsNone(hdr_input_params({'vid_width': 3840, 'vid_height': 2160}))

    def test_a_4k_sdr_source_is_not_hdr(self):
        for transfer in ('bt709', 'bt2020-10', 'smpte170m'):
            with self.subTest(transfer=transfer):
                self.assertIsNone(hdr_input_params(
                    {'vid_width': 3840, 'color_transfer': transfer}))

    def test_no_probe_at_all_is_not_hdr(self):
        """The two live-thumbnail callers pass none, and must never reach the chain."""
        self.assertIsNone(hdr_input_params(None))
        self.assertIsNone(hdr_input_params({}))

    def test_pq_is_hdr_whatever_the_size(self):
        params = hdr_input_params({'vid_width': 1280, 'color_transfer': 'smpte2084'})
        self.assertIsNotNone(params)
        self.assertIn('color_trc=smpte2084', params)

    def test_hlg_declares_its_own_transfer(self):
        params = hdr_input_params({'color_transfer': 'arib-std-b67'})
        self.assertIn('color_trc=arib-std-b67', params)

    def test_every_field_zscale_needs_is_declared(self):
        params = hdr_input_params({'color_transfer': 'smpte2084'})
        for field in ('color_trc=', 'color_primaries=', 'colorspace=', 'range='):
            self.assertIn(field, params)

    def test_unstated_fields_fall_back_to_what_a_pq_transfer_implies(self):
        params = hdr_input_params({'color_transfer': 'smpte2084'})
        self.assertIn('color_primaries=bt2020', params)
        self.assertIn('colorspace=bt2020nc', params)
        self.assertIn('range=tv', params)

    def test_ffprobes_word_for_unstated_is_not_passed_through_as_a_value(self):
        """ffprobe prints 'unknown', which is not a colorspace zscale can convert from."""
        params = hdr_input_params({'color_transfer': 'smpte2084',
                                   'color_primaries': 'unknown',
                                   'color_space': 'unknown',
                                   'color_range': 'unknown'})
        self.assertNotIn('unknown', params)
        self.assertIn('color_primaries=bt2020', params)

    def test_what_the_stream_actually_stated_wins_over_the_default(self):
        params = hdr_input_params({'color_transfer': 'smpte2084',
                                   'color_primaries': 'bt709',
                                   'color_space': 'bt709',
                                   'color_range': 'pc'})
        self.assertIn('color_primaries=bt709', params)
        self.assertIn('colorspace=bt709', params)
        self.assertIn('range=pc', params)


class TonemapCommandShapeTests(unittest.TestCase):
    """Which ffmpeg commands the branch actually issues."""

    def _runs(self, probe, results=None):
        """Every argv capture_screenshot issues; `results` sets each run's exit code."""
        seen = []
        codes = list(results or [])

        def fake_run(cmd, **_):
            seen.append(cmd)
            code = codes.pop(0) if codes else 1
            return subprocess.CompletedProcess(cmd, code, b'', b'')

        with mock.patch('app.screenshot.subprocess.run', side_effect=fake_run), \
                mock.patch('app.screenshot.os.makedirs'), \
                mock.patch('app.screenshot.os.path.exists', return_value=True), \
                mock.patch('app.screenshot.os.path.getsize', return_value=1000):
            capture_screenshot('/nonexistent/clip.ts', '/tmp/x/out.jpg', 'ffmpeg',
                               probe=probe, seek_args=['-ss', '2.50'])
        return seen

    @staticmethod
    def _is_tonemap(argv):
        return 'tonemap' in argv[argv.index('-vf') + 1]

    def test_a_4k_sdr_source_never_pays_a_tonemap_run(self):
        runs = self._runs({'vid_width': 3840, 'color_transfer': 'bt709'})
        self.assertEqual([], [c for c in runs if self._is_tonemap(c)])

    def test_an_untagged_source_never_pays_a_tonemap_run(self):
        runs = self._runs({'vid_width': 3840})
        self.assertEqual([], [c for c in runs if self._is_tonemap(c)])

    def test_a_pq_source_declares_its_input_before_the_chain(self):
        runs = self._runs({'color_transfer': 'smpte2084'})
        vf = runs[0][runs[0].index('-vf') + 1]
        self.assertTrue(vf.startswith('setparams='), vf)
        self.assertLess(vf.index('setparams='), vf.index('zscale'))

    def test_the_declaration_carries_what_the_probe_read(self):
        runs = self._runs({'color_transfer': 'arib-std-b67', 'color_range': 'pc'})
        vf = runs[0][runs[0].index('-vf') + 1]
        self.assertIn('color_trc=arib-std-b67', vf)
        self.assertIn('range=pc', vf)

    def test_a_tonemap_failure_on_one_seek_does_not_disable_it_on_the_next(self):
        """The rung that failed may simply have held no frame - the wider one is why."""
        runs = self._runs({'color_transfer': 'smpte2084'})
        seeks = [c[c.index('-ss') + 1] for c in runs if self._is_tonemap(c)]
        self.assertIn('0', seeks, 'the widened seek got no tonemap attempt')
        self.assertGreater(len(seeks), 1)

    def test_a_successful_tonemap_returns_without_a_second_run(self):
        runs = self._runs({'color_transfer': 'smpte2084'}, results=[0])
        self.assertEqual(1, len(runs))

    def test_the_failure_warning_is_only_logged_once_a_frame_is_known_to_exist(self):
        """'Tonemapping failed' said of a seek that held no frame is simply untrue."""
        with self.assertLogs('app.screenshot', level='WARNING') as caught:
            self._runs({'color_transfer': 'smpte2084'}, results=[1, 0])
        self.assertEqual(1, len(caught.records))

        with mock.patch('app.screenshot.log') as fake_log:
            self._runs({'color_transfer': 'smpte2084'}, results=[1, 1, 1, 1, 1, 1])
            fake_log.warning.assert_not_called()

    def test_an_undeclared_wide_source_says_why_it_was_not_tonemapped(self):
        with self.assertLogs('app.screenshot', level='INFO') as caught:
            self._runs({'vid_width': 3840, 'vid_height': 2160, 'bit_depth': 10})
        self.assertIn('no color transfer', caught.output[0])

    def test_an_ordinary_sdr_source_needs_no_such_line(self):
        with mock.patch('app.screenshot.log') as fake_log:
            self._runs({'vid_width': 1920, 'bit_depth': 8, 'color_transfer': 'bt709'})
            fake_log.info.assert_not_called()

    def test_a_10bit_4k_sdr_source_is_neither_tonemapped_nor_asked_about(self):
        """The shape a real 4K SDR broadcast actually has (dev/changelog/915).

        10-bit HEVC at 3840x2160 tagged bt709 satisfies both halves of the
        looks-like-it-could-be-HDR test, so it is the one SDR source that could
        wrongly draw an explanatory line as well as a tonemap run. It states its
        transfer, which answers the question outright.
        """
        probe = {'vid_width': 3840, 'vid_height': 2160, 'bit_depth': 10,
                 'color_transfer': 'bt709', 'color_primaries': 'bt709',
                 'color_space': 'bt709', 'color_range': 'tv'}
        self.assertIsNone(hdr_input_params(probe))
        with mock.patch('app.screenshot.log') as fake_log:
            runs = self._runs(probe)
            fake_log.info.assert_not_called()
        self.assertEqual([], [c for c in runs if self._is_tonemap(c)])


@unittest.skipUnless(_HAVE_FFMPEG and _HAVE_X265, 'ffmpeg with libx265 not available')
class RealTonemapTests(unittest.TestCase):
    """The real defect, on real encoded video carrying real colorspace tags."""

    @classmethod
    def setUpClass(cls):
        cls._dir = tempfile.mkdtemp(prefix='dvr_hdr_shot_')
        # Identical pictures, differing only in what they say about themselves. That
        # premise is what every byte-comparison below rests on, and it is load-bearing
        # enough to be asserted rather than assumed - see test_the_fixtures_are_the_same
        # _picture.
        cls.pq_full = cls._encode(
            'pq_full', 'yuv420p10le',
            'colorprim=bt2020:transfer=smpte2084:colormatrix=bt2020nc:range=limited')
        cls.pq_transfer_only = cls._encode(
            'pq_trconly', 'yuv420p10le', 'transfer=smpte2084')
        cls.sdr = cls._encode(
            'sdr', 'yuv420p',
            'colorprim=bt709:transfer=bt709:colormatrix=bt709:range=limited')

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls._dir, ignore_errors=True)

    @classmethod
    def _encode(cls, name, pix_fmt, x265_params):
        """A 1s clip whose colorspace tags reach the bitstream, not just the container.

        The tags are set through `-x265-params` alone, never through ffmpeg's own
        `-color_primaries`/`-colorspace`/`-color_range` output options. Those describe the
        colorspace the *output* is to be in, and ffmpeg 7 honors that by inserting a
        conversion filter to get there: asking for bt2020 converts the bt709 bars on the
        way to the encoder, so the fully-tagged clip stops being the same picture as the
        transfer-only one and the comparisons below fail for a reason that has nothing to
        do with the chain under test. ffmpeg 6 inserted no such filter, which is why this
        only showed up when CI moved onto the shipped series (dev/changelog/916). mpegts
        carries no colorspace metadata of its own anyway, so the bitstream VUI that
        x265 writes is the only tagging that was ever reaching the code under test.
        """
        path = os.path.join(cls._dir, f'{name}.ts')
        subprocess.run(
            [_FFMPEG, '-y', '-v', 'error', '-f', 'lavfi',
             '-i', 'smptehdbars=size=640x360:rate=25:duration=1',
             '-pix_fmt', pix_fmt, '-c:v', 'libx265',
             '-x265-params', f'keyint=12:log-level=none:{x265_params}',
             '-f', 'mpegts', path],
            capture_output=True, check=True)
        return path

    def _vf_grab(self, clip, vf, name):
        """One explicit ffmpeg run - the reference each capture is compared against."""
        out = os.path.join(self._dir, f'{name}.jpg')
        subprocess.run(
            [_FFMPEG, '-y', '-v', 'error', '-ss', '0', '-skip_frame', 'nokey',
             '-i', clip, '-vf', vf, '-vframes', '1', '-q:v', '3', out],
            capture_output=True)
        return out

    def _capture(self, clip, probe, name):
        out = os.path.join(self._dir, f'{name}.jpg')
        if os.path.exists(out):
            os.unlink(out)
        ok = capture_screenshot(clip, out, _FFMPEG, probe=probe, seek_args=['-ss', '0'])
        return ok, out

    @staticmethod
    def _bytes(path):
        with open(path, 'rb') as fh:
            return fh.read()

    def _first_frame_md5(self, clip):
        r = subprocess.run(
            [_FFMPEG, '-v', 'error', '-ss', '0', '-skip_frame', 'nokey', '-i', clip,
             '-vframes', '1', '-f', 'framemd5', '-'],
            capture_output=True)
        lines = [ln for ln in r.stdout.decode().splitlines() if ln and not ln.startswith('#')]
        return lines[-1].split(',')[-1].strip() if lines else None

    def _tags(self, clip):
        r = subprocess.run(
            ['ffprobe', '-v', 'error', '-select_streams', 'v:0', '-show_entries',
             'stream=color_transfer,color_primaries', '-of', 'csv=p=0', clip],
            capture_output=True)
        trc, prim = (r.stdout.decode().splitlines()[0] + ',').split(',')[:2]
        return trc.strip(), prim.strip()

    def test_the_fixtures_are_the_same_picture(self):
        """The premise every byte comparison here rests on, asserted rather than assumed.

        These two clips must differ in what they declare and in nothing else. When that
        stopped being true under ffmpeg 7's automatic colorspace conversion, the byte
        comparisons below failed with a message blaming the filter chain, which is the
        wrong place to go looking (dev/changelog/916).
        """
        full = self._first_frame_md5(self.pq_full)
        transfer_only = self._first_frame_md5(self.pq_transfer_only)
        self.assertIsNotNone(full, 'no decodable keyframe in the fully-tagged fixture')
        self.assertEqual(
            full, transfer_only,
            'the two fixtures no longer decode to the same picture, so a byte difference '
            'downstream says nothing about the tonemap chain. Something in the encode is '
            'converting one of them - check for color options that describe the OUTPUT.')

    def test_the_fixtures_declare_what_each_case_needs(self):
        """The other half of the premise: the tags themselves must still differ.

        A fixture change that makes both clips fully tagged would leave every test here
        passing while testing nothing, which is the failure mode a byte comparison cannot
        see. Primaries are the field the transfer-only case turns on - it is the one
        zscale cannot infer, and its absence is what returned code 3074.
        """
        full_trc, full_prim = self._tags(self.pq_full)
        only_trc, only_prim = self._tags(self.pq_transfer_only)
        self.assertEqual(full_trc, 'smpte2084')
        self.assertEqual(full_prim, 'bt2020', 'the fully-tagged fixture lost its primaries')
        self.assertEqual(only_trc, 'smpte2084',
                         'the transfer-only fixture stopped stating its transfer, so it no '
                         'longer reaches the HDR branch at all')
        self.assertIn(only_prim, UNSPECIFIED_COLOR,
                      f'the transfer-only fixture now states primaries ({only_prim}), so it '
                      'is no longer the under-tagged broadcast shape these tests exist for')

    def test_the_undeclared_chain_is_what_used_to_fail(self):
        """Characterization: zscale refuses a source that states only its transfer."""
        out = self._vf_grab(self.pq_transfer_only, PRE_FIX_TONEMAP_VF, 'prefix_trconly')
        self.assertFalse(os.path.exists(out) and os.path.getsize(out) > 0,
                         'the pre-fix chain was expected to produce nothing here')

    def test_a_pq_source_tagged_only_with_its_transfer_now_tonemaps(self):
        ok, out = self._capture(self.pq_transfer_only,
                                {'color_transfer': 'smpte2084'}, 'shot_trconly')
        self.assertTrue(ok, 'no screenshot produced at all')
        reference = self._vf_grab(self.pq_full, PRE_FIX_TONEMAP_VF, 'ref_full')
        self.assertEqual(self._bytes(reference), self._bytes(out),
                         'completing the tags did not reproduce the fully-tagged answer')

    def test_a_fully_tagged_pq_source_is_unchanged_by_the_declaration(self):
        ok, out = self._capture(self.pq_full, {'color_transfer': 'smpte2084',
                                               'color_primaries': 'bt2020',
                                               'color_space': 'bt2020nc',
                                               'color_range': 'tv'}, 'shot_full')
        self.assertTrue(ok)
        reference = self._vf_grab(self.pq_full, PRE_FIX_TONEMAP_VF, 'ref_full2')
        self.assertEqual(self._bytes(reference), self._bytes(out))

    def test_a_4k_sdr_source_is_scaled_rather_than_tonemapped(self):
        """The probe claims 4K over an SDR clip - the shape the old width trigger caught."""
        ok, out = self._capture(self.sdr, {'vid_width': 3840, 'vid_height': 2160,
                                           'color_transfer': 'bt709',
                                           'color_primaries': 'bt709',
                                           'color_space': 'bt709',
                                           'color_range': 'tv'}, 'shot_sdr')
        self.assertTrue(ok)
        darkened = self._vf_grab(self.sdr, PRE_FIX_TONEMAP_VF, 'ref_sdr_tonemapped')
        plain = self._vf_grab(
            self.sdr,
            'scale=min(iw\\,1920):min(ih\\,1080):force_original_aspect_ratio=decrease',
            'ref_sdr_plain')
        self.assertNotEqual(self._bytes(darkened), self._bytes(out),
                            'SDR content went through the tonemap chain')
        self.assertEqual(self._bytes(plain), self._bytes(out))


if __name__ == '__main__':
    unittest.main()
