"""Tier 1 - the two ffmpeg commands a picture-in-picture play is made of (dev/changelog/1689).

The suite has no GPU, so these pin the argv: the shapes the GPU lab measured
(dev/tools/gpu-lab/jobs/pip-*.sh run the same builders on the real device). Each assertion is a
measured rule, named where it is checked.
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import normalize as nz  # noqa: E402
from app import proc_utils as pu  # noqa: E402

_CFG = {'ffmpeg': {'path': 'ffmpeg'}}
_DEVICE = '/dev/dri/renderD128'


def _graph(cmd):
    return cmd[cmd.index('-filter_complex') + 1]


class PipGeometryTests(unittest.TestCase):
    def test_every_corner_and_size_sits_inside_the_output_with_its_margin(self):
        for size, (w, h) in pu.PIP_CORNER_SIZES.items():
            for corner in pu.PIP_CORNERS:
                g = pu.pip_geometry(pu.PIP_LAYOUT_CORNER, corner, size)
                self.assertEqual((g.big_w, g.big_h, g.big_x, g.big_y), (1920, 1080, 0, 0))
                self.assertEqual((g.small_w, g.small_h, g.border, g.small_fps), (w, h, pu.PIP_BORDER, 30))
                box_w, box_h = w + 2 * g.border, h + 2 * g.border
                left = g.small_x == pu.PIP_MARGIN
                top = g.small_y == pu.PIP_MARGIN
                self.assertEqual(left, corner.endswith('l'), corner)
                self.assertEqual(top, corner.startswith('t'), corner)
                if not left:
                    self.assertEqual(g.small_x + box_w + pu.PIP_MARGIN, 1920)
                if not top:
                    self.assertEqual(g.small_y + box_h + pu.PIP_MARGIN, 1080)

    def test_side_by_side_is_two_halves_with_no_border(self):
        g = pu.pip_geometry(pu.PIP_LAYOUT_SIDE)
        self.assertEqual(g, pu.PipGeometry(960, 540, 0, 270, 960, 540, 960, 270, 0, 60))

    def test_an_unknown_layout_corner_or_size_is_an_error_not_a_default(self):
        for args in (('stacked',), ('corner', 'mid'), ('corner', 'br', 'xl')):
            with self.assertRaises(ValueError):
                pu.pip_geometry(*args)

    def test_a_raw_frame_is_one_and_a_half_bytes_a_pixel(self):
        self.assertEqual(pu.pip_frame_bytes(pu.pip_geometry('corner', 'br', 'm')), 640 * 360 * 3 // 2)


class SourceMatrixTests(unittest.TestCase):
    def test_a_named_matrix_wins_over_the_frame_size(self):
        self.assertEqual(nz.source_matrix('smpte170m', 1080), nz.MATRIX_BT601)
        self.assertEqual(nz.source_matrix('bt470bg', 720), nz.MATRIX_BT601)
        self.assertEqual(nz.source_matrix('BT709', 480), nz.MATRIX_BT709)

    def test_an_untagged_picture_goes_by_its_size(self):
        for unset in (None, '', 'unknown'):
            self.assertEqual(nz.source_matrix(unset, 480), nz.MATRIX_BT601)
            self.assertEqual(nz.source_matrix(unset, 576), nz.MATRIX_BT601)
            self.assertEqual(nz.source_matrix(unset, 720), nz.MATRIX_BT709)
            self.assertEqual(nz.source_matrix(unset, 1080), nz.MATRIX_BT709)
        self.assertEqual(nz.source_matrix(None, None), nz.MATRIX_BT709)

    def test_anything_else_is_treated_as_bt709(self):
        self.assertEqual(nz.source_matrix('bt2020nc', 2160), nz.MATRIX_BT709)
        self.assertEqual(nz.source_matrix('bt2020nc', 480), nz.MATRIX_BT709)


class GpuMeetFilterTests(unittest.TestCase):
    """Tagged with what the caller knows, converted, then pinned: the driver does not read the
    matrix from the stream (measured on color bars, dev/changelog/1689)."""

    def test_a_bt709_picture_is_tagged_bt709_first_and_pinned_last(self):
        vf = nz.gpu_meet_filter(1920, 1080)
        self.assertTrue(vf.startswith(nz.GPU_BT709_PIN + ',scale_vaapi='), vf)
        self.assertTrue(vf.endswith(',' + nz.GPU_BT709_PIN), vf)

    def test_a_bt601_picture_is_tagged_bt601_first(self):
        vf = nz.gpu_meet_filter(640, 360, nz.MATRIX_BT601)
        self.assertTrue(vf.startswith('setparams=colorspace=smpte170m:color_primaries=smpte170m'), vf)
        self.assertTrue(vf.endswith(',' + nz.GPU_BT709_PIN), vf)

    def test_the_conversion_names_the_primaries(self):
        # With the matrix alone the driver converted nothing.
        vf = nz.gpu_meet_filter(1920, 1080, nz.MATRIX_BT601)
        self.assertIn(':out_color_matrix=bt709:out_color_primaries=bt709:out_color_transfer=bt709'
                      ':out_range=tv,pad_vaapi=w=1920:h=1080', vf)

    def test_deinterlacing_sits_between_the_tag_and_the_fit(self):
        vf = nz.gpu_meet_filter(1920, 1080, deinterlace=True)
        self.assertIn(f'{nz.GPU_BT709_PIN},{nz.DEINTERLACE_VAAPI},scale_vaapi=', vf)
        self.assertNotIn('deinterlace', nz.gpu_meet_filter(1920, 1080))

    def test_the_plain_gpu_fit_is_unchanged(self):
        self.assertEqual(
            nz.gpu_fit_filter(1920, 1080),
            "scale_vaapi=w='if(gt(dar,1920/1080),1920,trunc(1080*dar/2)*2)'"
            ":h='if(gt(dar,1920/1080),trunc(1920/dar/2)*2,1080)':format=nv12,"
            "pad_vaapi=w=1920:h=1080:x=(ow-iw)/2:y=(oh-ih)/2")


class BuildPipCombinerCmdTests(unittest.TestCase):
    def setUp(self):
        self.g = pu.pip_geometry(pu.PIP_LAYOUT_CORNER, 'br', 'm')

    def test_one_device_shared_by_the_decoder_the_upload_and_the_overlay(self):
        cmd = pu.build_pip_combiner_cmd(_CFG, _DEVICE, self.g)
        i = cmd.index('-init_hw_device')
        self.assertEqual(cmd[i:i + 4], ['-init_hw_device', f'vaapi=pip:{_DEVICE}',
                                        '-filter_hw_device', 'pip'])
        h = cmd.index('-hwaccel')
        self.assertEqual(cmd[h:h + 6], ['-hwaccel', 'vaapi', '-hwaccel_device', 'pip',
                                        '-hwaccel_output_format', 'vaapi'])
        # A path there would open a second device beside the filters' one.
        self.assertNotIn(_DEVICE, cmd[h:])
        self.assertNotIn('-vaapi_device', cmd)

    def test_the_big_picture_is_stdin_and_the_small_one_raw_frames_on_its_own_pipe(self):
        cmd = pu.build_pip_combiner_cmd(_CFG, _DEVICE, self.g, small_input='pipe:7')
        first, second = [i for i, a in enumerate(cmd) if a == '-i']
        self.assertEqual(cmd[first - 2:first + 2], ['-f', 'mpegts', '-i', 'pipe:0'])
        self.assertLess(cmd.index('-hwaccel'), first)
        self.assertEqual(cmd[first + 2:second + 2],
                         ['-f', 'rawvideo', '-pix_fmt', 'nv12', '-video_size', '640x360',
                          '-framerate', '30', '-i', 'pipe:7'])
        # GPU decode options belong to the big input only: the small one is never handed over
        # as GPU-decoded frames ("invalid VABufferID", dev/changelog/1687).
        self.assertNotIn('-hwaccel', cmd[first:])

    def test_the_graph_fits_tags_borders_uploads_and_overlays(self):
        graph = _graph(pu.build_pip_combiner_cmd(_CFG, _DEVICE, self.g))
        big, small, overlay = graph.split(';')
        self.assertEqual(big, f'[0:v:0]{nz.gpu_meet_filter(1920, 1080)}[big]')
        self.assertEqual(small, f'[1:v:0]{nz.GPU_BT709_PIN},pad=648:368:4:4:color=white,'
                                'format=nv12,hwupload[small]')
        self.assertEqual(overlay, '[big][small]overlay_vaapi=x=1224:y=664[v]')

    def test_gpu_decode_carries_noautoscale_and_never_reinit_filter(self):
        cmd = pu.build_pip_combiner_cmd(_CFG, _DEVICE, self.g)
        self.assertIn('-noautoscale', cmd)
        self.assertNotIn('-reinit_filter:v', cmd)
        self.assertEqual(cmd[cmd.index('-loglevel') + 1], 'level+error')

    def test_the_output_is_h264_ts_on_stdout_with_the_big_pictures_sound_copied(self):
        cmd = pu.build_pip_combiner_cmd(_CFG, _DEVICE, self.g)
        tail = cmd[cmd.index('-filter_complex') + 2:]
        self.assertEqual(tail[:8], ['-map', '[v]', '-map', '0:a:0?', '-sn', '-dn', '-c:a', 'copy'])
        self.assertEqual(tail[tail.index('-c:v') + 1], 'h264_vaapi')
        self.assertEqual(tail[tail.index('-qp') + 1], str(pu.REENCODE_QP))
        self.assertEqual(tail[tail.index('-fps_mode:v') + 1], 'passthrough')
        self.assertEqual(tail[-3:], ['-f', 'mpegts', 'pipe:1'])
        self.assertNotIn('-stderr', cmd)

    def test_deinterlace_and_matrix_reach_the_big_pictures_chain(self):
        graph = _graph(pu.build_pip_combiner_cmd(_CFG, _DEVICE, self.g, deinterlace=True,
                                                 matrix=nz.MATRIX_BT601))
        self.assertIn(nz.gpu_meet_filter(1920, 1080, nz.MATRIX_BT601, deinterlace=True), graph)

    def test_side_by_side_fits_each_half_and_places_the_big_one_on_the_left(self):
        g = pu.pip_geometry(pu.PIP_LAYOUT_SIDE)
        cmd = pu.build_pip_combiner_cmd(_CFG, _DEVICE, g)
        big, small, overlay = _graph(cmd).split(';')
        self.assertEqual(big, f'[0:v:0]{nz.gpu_meet_filter(960, 540)},'
                              'pad_vaapi=w=1920:h=1080:x=0:y=270[big]')
        self.assertEqual(small, f'[1:v:0]{nz.GPU_BT709_PIN},format=nv12,hwupload[small]')
        self.assertEqual(overlay, '[big][small]overlay_vaapi=x=960:y=270[v]')
        self.assertEqual(cmd[cmd.index('-video_size') + 1], '960x540')
        self.assertEqual(cmd[cmd.index('-framerate') + 1], '60')

    def test_the_cpu_fallback_decodes_in_software_and_still_combines_on_the_gpu(self):
        cmd = pu.build_pip_combiner_cmd(_CFG, _DEVICE, self.g, gpu_decode=False,
                                        matrix=nz.MATRIX_BT601)
        self.assertNotIn('-hwaccel', cmd)
        self.assertNotIn('-noautoscale', cmd)
        self.assertIn('-init_hw_device', cmd)
        self.assertEqual(cmd[cmd.index('-reinit_filter:v') + 1], '0')
        self.assertEqual(cmd[cmd.index('-loglevel') + 1], 'error')
        big = _graph(cmd).split(';')[0]
        self.assertTrue(big.startswith('[0:v:0]scale=w='), big)
        self.assertIn(nz.DEINTERLACE_SOFTWARE, big)
        self.assertIn('scale=in_color_matrix=bt601:out_color_matrix=bt709', big)
        self.assertTrue(big.endswith(f'{nz.GPU_BT709_PIN},format=nv12,hwupload[big]'), big)
        self.assertNotIn('scale_vaapi', big)
        self.assertIn('overlay_vaapi', _graph(cmd))

    def test_the_cpu_fallback_places_a_side_by_side_big_picture_in_software(self):
        g = pu.pip_geometry(pu.PIP_LAYOUT_SIDE)
        big = _graph(pu.build_pip_combiner_cmd(_CFG, _DEVICE, g, gpu_decode=False)).split(';')[0]
        self.assertTrue(big.startswith(f'[0:v:0]{nz.fit_filter(960, 540, interlaced_aware=True)},'), big)
        self.assertTrue(big.endswith(f'format=nv12,pad=1920:1080:0:270,{nz.GPU_BT709_PIN},'
                                     'format=nv12,hwupload[big]'), big)
        self.assertNotIn('pad_vaapi', big)


class BuildPipCombinerCardTests(unittest.TestCase):
    """The big window's card as the combiner's big input (dev/docs/DESIGN-pip-channels.md 6.4)."""

    def setUp(self):
        self.g = pu.pip_geometry(pu.PIP_LAYOUT_CORNER, 'br', 'm')
        self.cmd = pu.build_pip_combiner_cmd(_CFG, _DEVICE, self.g, small_input='pipe:7',
                                             big_input='/clips/p1-card.ts')

    def test_the_card_is_looped_from_its_file_at_real_time_in_place_of_stdin(self):
        first, second = [i for i, a in enumerate(self.cmd) if a == '-i']
        self.assertEqual(self.cmd[first - 6:first + 2],
                         ['-readrate', '1', '-readrate_initial_burst', str(pu.SCREEN_PLAY_BURST_SECONDS),
                          '-stream_loop', '-1', '-i', 'file:/clips/p1-card.ts'])
        self.assertNotIn('pipe:0', self.cmd)
        self.assertEqual(self.cmd[second + 1], 'pipe:7', 'the small picture is still the second input')

    def test_everything_after_the_big_input_is_the_live_shape(self):
        live = pu.build_pip_combiner_cmd(_CFG, _DEVICE, self.g, small_input='pipe:7')
        self.assertEqual(self.cmd[self.cmd.index('-filter_complex'):], live[live.index('-filter_complex'):])
        # The card's sound is copied like a channel's, so the TV always has sound to follow.
        self.assertIn('0:a:0?', self.cmd)

    def test_with_no_card_named_the_command_is_what_it_was(self):
        self.assertEqual(pu.build_pip_combiner_cmd(_CFG, _DEVICE, self.g, big_input=''),
                         pu.build_pip_combiner_cmd(_CFG, _DEVICE, self.g))


class BuildPipSmallDecoderCmdTests(unittest.TestCase):
    def setUp(self):
        self.g = pu.pip_geometry(pu.PIP_LAYOUT_CORNER, 'tl', 's')

    def test_gpu_decode_fits_on_the_device_and_downloads_small_frames(self):
        cmd = pu.build_pip_small_decoder_cmd(_CFG, _DEVICE, self.g, deinterlace=True)
        self.assertEqual(cmd[cmd.index('-i') - 2:cmd.index('-i') + 2], ['-f', 'mpegts', '-i', 'pipe:0'])
        self.assertEqual(cmd[cmd.index('-vf') + 1],
                         f'{nz.gpu_meet_filter(480, 270, deinterlace=True)},hwdownload,format=nv12')
        self.assertIn('-noautoscale', cmd)
        self.assertNotIn('-reinit_filter:v', cmd)
        self.assertEqual(cmd[cmd.index('-hwaccel_device') + 1], 'pip')
        self.assertEqual(cmd[cmd.index('-loglevel') + 1], 'level+error')

    def test_raw_frames_at_the_sources_own_rate_on_stdout_and_no_sound(self):
        cmd = pu.build_pip_small_decoder_cmd(_CFG, _DEVICE, self.g)
        self.assertEqual(cmd[-5:], ['-fps_mode:v', 'passthrough', '-f', 'rawvideo', 'pipe:1'])
        self.assertIn('-an', cmd)
        # A rate filter would hold the picture still after a backward timestamp jump.
        self.assertNotIn('fps=', cmd[cmd.index('-vf') + 1])
        self.assertNotIn('-r', cmd)

    def test_the_cpu_fallback_touches_no_device(self):
        cmd = pu.build_pip_small_decoder_cmd(_CFG, _DEVICE, self.g, gpu_decode=False,
                                             matrix=nz.MATRIX_BT601)
        for gone in ('-hwaccel', '-init_hw_device', '-noautoscale'):
            self.assertNotIn(gone, cmd)
        vf = cmd[cmd.index('-vf') + 1]
        self.assertTrue(vf.startswith(nz.fit_filter(480, 270, interlaced_aware=True)), vf)
        self.assertTrue(vf.endswith('scale=in_color_matrix=bt601:out_color_matrix=bt709,format=nv12'), vf)
        self.assertNotIn('vaapi', vf)
        self.assertNotIn('in_color_matrix', pu.build_pip_small_decoder_cmd(
            _CFG, _DEVICE, self.g, gpu_decode=False)[cmd.index('-vf') + 1])


if __name__ == '__main__':
    unittest.main()
