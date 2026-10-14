"""Tier 1 - what a re-encoded recording is normalized TO (dev/changelog/1184).

app/normalize.py is pure: the segment rows, the whole-file survey and the file header go in,
a NormalizePlan comes out. These pin the decision table settled for it - every recording is
brought to its OWN biggest format and never to anyone else's, and a max resolution only ever
lowers that - plus the audio, loudness and filter-chain arithmetic the conversion builds its
command from. The conversions themselves, with real ffmpeg, are
tests/test_normalize_conversion.py.
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import normalize as nz  # noqa: E402
from app.probe import HDR_TRANSFERS, combine_loudness  # noqa: E402


def _src(*sizes, rates=(), channels=(), interlaced=False, hdr=None, header_rate=None,
         sars=None):
    """A SourceFormats from (w, h) pairs, as a survey would report them."""
    sars = sars or [1.0] * len(sizes)
    survey = {'video_sizes': [[w, h, sar] for (w, h), sar in zip(sizes, sars)],
              'interlaced': interlaced, 'transfers': [hdr] if hdr else [],
              'audio_channels': list(channels), 'keyframes': 10, 'audio_frames': 10}
    segments = [(None, r, None, None) for r in rates]
    return nz.gather_source_formats(segments, survey, {'r_frame_rate': header_rate},
                                    HDR_TRANSFERS)


HD720, HD1080, UHD = (1280, 720), (1920, 1080), (3840, 2160)


class BiggestFormatTableTests(unittest.TestCase):
    """The settled table (dev/changelog/1183 Decisions): per recording, never across."""

    def _size(self, *sizes, max_height=0):
        plan = nz.plan_normalize(_src(*sizes), max_height=max_height)
        return (plan.width, plan.height)

    def test_720p_only_stays_720p_with_no_cap(self):
        self.assertEqual(self._size(HD720), HD720)

    def test_720p_only_stays_720p_under_a_1080p_cap(self):
        """A cap never upscales: the recording's own biggest size is the ceiling."""
        self.assertEqual(self._size(HD720, max_height=1080), HD720)

    def test_720p_and_1080p_become_1080p(self):
        self.assertEqual(self._size(HD720, HD1080), HD1080)
        self.assertEqual(self._size(HD720, HD1080, max_height=1080), HD1080)

    def test_720_1080_and_4k_become_4k_or_the_cap(self):
        self.assertEqual(self._size(HD720, HD1080, UHD), UHD)
        self.assertEqual(self._size(HD720, HD1080, UHD, max_height=1080), HD1080)

    def test_4k_only_comes_down_only_under_a_cap(self):
        self.assertEqual(self._size(UHD), UHD)
        self.assertEqual(self._size(UHD, max_height=1080), HD1080)

    def test_a_cap_keeps_the_aspect_ratio_at_even_dimensions(self):
        plan = nz.plan_normalize(_src((720, 576), sars=[64 / 45]), max_height=480)
        self.assertEqual((plan.width, plan.height), (854, 480))


class ReasonTests(unittest.TestCase):
    """Each reason a recording is not normalized is its own trigger, named on its own."""

    def test_a_single_format_recording_has_no_reason_and_no_scale(self):
        plan = nz.plan_normalize(_src(HD1080, rates=(59.94,)))
        self.assertEqual(plan.reasons, ())
        self.assertFalse(plan.scale, 'a clean recording keeps the command it always had')

    def test_a_size_change_is_mixed_resolution(self):
        plan = nz.plan_normalize(_src(HD720, HD1080))
        self.assertEqual(plan.reasons, (nz.REASON_MIXED_RESOLUTION,))
        self.assertTrue(plan.scale)

    def test_a_recording_over_the_cap_is_above_max(self):
        plan = nz.plan_normalize(_src(HD1080), max_height=720)
        self.assertEqual(plan.reasons, (nz.REASON_ABOVE_MAX_RESOLUTION,))

    def test_a_mixed_audio_layout_is_not_a_video_trigger(self):
        """mp4 audio is re-encoded on every conversion; -ac fixes it without touching video."""
        plan = nz.plan_normalize(_src(HD1080, channels=(2, 6)))
        self.assertEqual(plan.reasons, ())
        self.assertEqual(plan.channels, 6)

    def test_non_square_pixels_scale_without_being_a_trigger(self):
        plan = nz.plan_normalize(_src((1440, 1080), sars=[4 / 3]))
        self.assertEqual(plan.reasons, ())
        self.assertTrue(plan.scale)
        self.assertEqual((plan.width, plan.height), HD1080)


class RateTests(unittest.TestCase):
    def test_the_highest_rate_wins_never_the_first(self):
        plan = nz.plan_normalize(_src(HD720, HD1080, rates=(29.97, 59.94),
                                      header_rate='30000/1001'))
        self.assertEqual(plan.rate, '60000/1001')

    def test_the_headers_exact_rational_is_kept_when_it_is_the_top_rate(self):
        plan = nz.plan_normalize(_src(HD1080, rates=(25.0,), header_rate='25/1'))
        self.assertEqual(plan.rate, '25/1')

    def test_an_interlaced_source_is_output_at_field_rate(self):
        plan = nz.plan_normalize(_src(HD1080, rates=(29.97,), interlaced=True,
                                      header_rate='30000/1001'))
        self.assertTrue(plan.deinterlace)
        self.assertEqual(plan.rate, '60000/1001')

    def test_a_progressive_59_94_beside_interlaced_is_not_doubled(self):
        plan = nz.plan_normalize(_src(HD720, HD1080, rates=(29.97, 59.94), interlaced=True))
        self.assertEqual(plan.rate, '60000/1001')

    def test_rates_map_back_to_their_rationals(self):
        self.assertEqual(nz.rate_rational(59.94), '60000/1001')
        self.assertEqual(nz.rate_rational(29.97), '30000/1001')
        self.assertEqual(nz.rate_rational(50.0), '50')
        self.assertIsNone(nz.rate_rational(None))


class AudioTests(unittest.TestCase):
    def test_channels_follow_the_widest_layout(self):
        self.assertEqual(nz.plan_normalize(_src(HD1080, channels=(2, 6))).channels, 6)

    def test_stereo_overrides_the_source(self):
        plan = nz.plan_normalize(_src(HD1080, channels=(2, 6)),
                                 audio_channels=nz.AUDIO_CHANNELS_STEREO)
        self.assertEqual(plan.channels, 2)

    def test_unknown_channels_leave_ac_out(self):
        self.assertIsNone(nz.plan_normalize(_src(HD1080)).channels)

    def test_bitrate_is_per_stereo_pair(self):
        self.assertEqual(nz.audio_bitrate_kbps(192, 2), 192)
        self.assertEqual(nz.audio_bitrate_kbps(192, 6), 576)
        self.assertEqual(nz.audio_bitrate_kbps(192, 8), 768)
        self.assertEqual(nz.audio_bitrate_kbps(192, 1), 192)
        self.assertEqual(nz.audio_bitrate_kbps(192, None), 192)


class LoudnessTests(unittest.TestCase):
    def test_a_loud_channel_is_turned_down_by_the_measured_amount(self):
        self.assertEqual(nz.loudness_gain_db(-19.8), -4.2)

    def test_a_quiet_recording_is_boosted_at_most_twelve_db(self):
        self.assertEqual(nz.loudness_gain_db(-30.0), 6.0)
        self.assertEqual(nz.loudness_gain_db(-55.0), nz.LOUDNESS_MAX_BOOST_DB)

    def test_silence_and_a_failed_measurement_are_left_alone(self):
        self.assertIsNone(nz.loudness_gain_db(-70.0))
        self.assertIsNone(nz.loudness_gain_db(None))
        self.assertIsNone(nz.audio_filter(None))

    def test_the_filter_is_a_gain_and_a_limiter(self):
        self.assertEqual(nz.audio_filter(-4.2),
                         'volume=-4.20dB,alimiter=limit=0.79:level=false')

    def test_instances_combine_by_energy_not_by_averaging_lufs(self):
        """Two equal halves at -21.8 and -14.0 LUFS (what a stereo-then-5.1 file measured
        as two ebur128 instances) integrate to -16.3, not their mean of -17.9."""
        self.assertAlmostEqual(combine_loudness([(6.0, -21.8), (6.0, -14.0)]), -16.34, 1)
        self.assertEqual(combine_loudness([(10.0, -70.0)]), -70.0)
        self.assertIsNone(combine_loudness([]))


class HdrTests(unittest.TestCase):
    def test_software_never_normalizes_an_hdr_source(self):
        plan = nz.plan_normalize(_src(HD720, HD1080, hdr='smpte2084'), hdr_ok=False)
        self.assertFalse(plan.normalize_video)
        self.assertEqual(plan.reasons, (), 'nothing to trigger: it will not be normalized')
        self.assertIsNone(nz.software_video_filter(plan))

    def test_the_gpu_normalizes_hdr_and_says_it_is_hdr(self):
        plan = nz.plan_normalize(_src(HD720, HD1080, hdr='smpte2084'), hdr_ok=True)
        self.assertTrue(plan.hdr)
        self.assertEqual(plan.reasons, (nz.REASON_MIXED_RESOLUTION,))

    def test_an_unstated_transfer_is_not_hdr(self):
        self.assertFalse(nz.plan_normalize(_src(UHD)).hdr)


class GatherTests(unittest.TestCase):
    def test_a_null_probe_contributes_nothing(self):
        src = nz.gather_source_formats([(None, None, None, None), ('1920x1080', 59.94, 2, False)],
                                       {}, {}, HDR_TRANSFERS)
        self.assertEqual(src.sizes, (HD1080,))
        self.assertEqual(src.channels, (2,))

    def test_the_survey_sees_a_change_the_rows_cannot(self):
        """A change inside one segment: the row saw only the start."""
        src = nz.gather_source_formats(
            [('1280x720', 29.97, 2, False)],
            {'video_sizes': [[1280, 720, 1.0], [1920, 1080, 1.0]], 'audio_channels': [2, 6]},
            {}, HDR_TRANSFERS)
        self.assertTrue(src.mixed_resolution)
        self.assertEqual(src.channels, (2, 6))

    def test_single_format_needs_the_survey(self):
        """GPU decode is refused unless the whole file was read and held one format."""
        rows_only = nz.gather_source_formats([('1920x1080', 59.94, 2, False)], {}, {},
                                             HDR_TRANSFERS)
        self.assertFalse(rows_only.single_format)
        self.assertTrue(_src(HD1080, rates=(59.94,)).single_format)
        self.assertFalse(_src(HD720, HD1080).single_format)


class FilterChainTests(unittest.TestCase):
    def test_the_fit_chain_is_evaluated_per_frame(self):
        """Under -reinit_filter:v 0 only per-frame expressions follow a size change."""
        chain = nz.fit_filter(1920, 1080)
        self.assertEqual(chain.count('eval=frame'), 2)
        self.assertTrue(chain.endswith('setsar=1'))

    def test_the_gpu_upload_chain_ends_on_the_device(self):
        plan = nz.plan_normalize(_src(HD720, HD1080))
        self.assertTrue(nz.gpu_upload_filter(plan, hevc=False).endswith('format=nv12,hwupload'))

    def test_hdr_on_the_gpu_keeps_ten_bits_for_hevc_and_tone_maps_for_h264(self):
        plan = nz.plan_normalize(_src(UHD, hdr='smpte2084'))
        hevc = nz.gpu_upload_filter(plan, hevc=True)
        h264 = nz.gpu_upload_filter(plan, hevc=False)
        self.assertTrue(hevc.endswith('format=p010,hwupload'))
        self.assertNotIn('tonemap', hevc)
        self.assertTrue(h264.endswith('format=p010,hwupload,' + nz.TONEMAP_VAAPI))
        self.assertIn(nz.TONEMAP_VAAPI, nz.gpu_decode_filter(plan, hevc=False))
        self.assertIn('format=p010', nz.gpu_decode_filter(plan, hevc=True))

    def test_deinterlacing_comes_first(self):
        plan = nz.plan_normalize(_src(HD1080, rates=(29.97,), interlaced=True))
        self.assertTrue(nz.software_video_filter(plan).startswith(nz.DEINTERLACE_SOFTWARE))
        self.assertTrue(nz.gpu_decode_filter(plan, hevc=False).startswith(nz.DEINTERLACE_VAAPI))


if __name__ == '__main__':
    unittest.main()
