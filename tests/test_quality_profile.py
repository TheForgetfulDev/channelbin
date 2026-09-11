"""Tier 1/2 tests for the stream quality profile (DESIGN-stream-quality-profile.md).

Guards the v1 data layer: pix_fmt/codec/field_order/coded-dims/CFR-VFR extraction in
app/probe.py, the bits-per-pixel-frame stat, and the _build_test_dict serializer keys.

Empirical portion (Class I: verify external tools on this machine) generates real clips
with ffmpeg and self-skips if ffmpeg/ffprobe are unavailable. Files are tiny and land in
a temp dir cleaned up in tearDown. No network (netguard) - lavfi sources only.

This is v1: informational only. Nothing here asserts any effect on health_score, and the
design forbids one - a companion invariant lives in the "no health_score change" review.
"""
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.probe import (  # noqa: E402
    parse_pix_fmt, bits_per_pixel_frame, detect_vfr, parse_ffprobe,
)
from app.config import resolve_ffmpeg_path  # noqa: E402

_FFMPEG = resolve_ffmpeg_path('ffmpeg')
_HAVE_FFMPEG = (
    (shutil.which(_FFMPEG) is not None
     or (os.path.isabs(_FFMPEG) and os.path.exists(_FFMPEG)))
    # ffprobe is the half that can be missing on its own - ffmpeg.path may name a binary
    # with no ffprobe beside it - and an ffmpeg-only check would pass on a machine where
    # parse_ffprobe can only ever return {}. This guard predates dev/changelog/911, which
    # removed the bundled-ffmpeg fallback that used to make that state a pip install away.
    and shutil.which('ffprobe') is not None)


class PixFmtParseTests(unittest.TestCase):
    """pix_fmt -> (bit_depth, chroma_subsampling)."""

    def test_8bit_420(self):
        self.assertEqual(parse_pix_fmt('yuv420p'), (8, '420'))

    def test_10bit_420(self):
        self.assertEqual(parse_pix_fmt('yuv420p10le'), (10, '420'))

    def test_8bit_422(self):
        self.assertEqual(parse_pix_fmt('yuv422p'), (8, '422'))

    def test_10bit_444(self):
        self.assertEqual(parse_pix_fmt('yuv444p10le'), (10, '444'))

    def test_12bit_444(self):
        self.assertEqual(parse_pix_fmt('yuv444p12le'), (12, '444'))

    def test_jpeg_range_is_8bit_420(self):
        # yuvj420p (full-range) must not read the 'j' as anything - still 8-bit 4:2:0.
        self.assertEqual(parse_pix_fmt('yuvj420p'), (8, '420'))

    def test_non_yuv_is_unknown(self):
        self.assertEqual(parse_pix_fmt('rgb24'), (None, None))
        self.assertEqual(parse_pix_fmt('gray'), (None, None))

    def test_empty(self):
        self.assertEqual(parse_pix_fmt(None), (None, None))
        self.assertEqual(parse_pix_fmt(''), (None, None))


class BitsPerPixelFrameTests(unittest.TestCase):
    def test_basic_math(self):
        # 6 Mbps / (1920*1080*30) = 0.0965...
        self.assertAlmostEqual(bits_per_pixel_frame(6_000_000, 1920, 1080, 30), 0.0965, places=3)

    def test_none_on_missing_input(self):
        self.assertIsNone(bits_per_pixel_frame(None, 1920, 1080, 30))
        self.assertIsNone(bits_per_pixel_frame(6_000_000, None, 1080, 30))
        self.assertIsNone(bits_per_pixel_frame(6_000_000, 1920, 1080, 0))

    def test_none_on_zero_pixels(self):
        self.assertIsNone(bits_per_pixel_frame(6_000_000, 0, 0, 30))


class DetectVfrTests(unittest.TestCase):
    def test_cfr_equal_rates(self):
        self.assertIs(detect_vfr('30/1', '30/1'), False)

    def test_ntsc_cfr_within_tolerance(self):
        # 30000/1001 vs 30000/1001 - identical, CFR.
        self.assertIs(detect_vfr('30000/1001', '30000/1001'), False)

    def test_vfr_large_mismatch(self):
        self.assertIs(detect_vfr('30/1', '15/1'), True)

    def test_undetermined_on_bad_rate(self):
        self.assertIsNone(detect_vfr('30/1', '0/0'))
        self.assertIsNone(detect_vfr('30/1', None))


@unittest.skipUnless(_HAVE_FFMPEG, 'ffmpeg/ffprobe not available')
class ParseFfprobeQualityTests(unittest.TestCase):
    """End-to-end field extraction from real clips - the empirical guard that the ffprobe
    JSON field names this feature reads (codec_name/pix_fmt/field_order/coded_*) are the
    real ones on this machine's ffprobe."""

    def setUp(self):
        self._dir = tempfile.mkdtemp(prefix='dvr_qp_test_')

    def tearDown(self):
        shutil.rmtree(self._dir, ignore_errors=True)

    def _gen(self, name, args):
        path = os.path.join(self._dir, name)
        subprocess.run([_FFMPEG, '-y', '-hide_banner', '-loglevel', 'error',
                        '-f', 'lavfi', '-i', 'testsrc2=size=640x360:rate=30:duration=1',
                        *args, path], capture_output=True, check=True)
        return path

    def test_h264_8bit_progressive(self):
        p = self._gen('prog.mp4', ['-pix_fmt', 'yuv420p', '-c:v', 'libx264'])
        info = parse_ffprobe(p)
        self.assertEqual(info['video_codec'], 'h264')
        self.assertEqual(info['pix_fmt'], 'yuv420p')
        self.assertEqual(info['bit_depth'], 8)
        self.assertEqual(info['chroma_subsampling'], '420')
        self.assertIs(info['interlaced'], False)

    def test_10bit_extraction(self):
        p = self._gen('ten.mp4', ['-pix_fmt', 'yuv420p10le', '-c:v', 'libx264'])
        info = parse_ffprobe(p)
        self.assertEqual(info['bit_depth'], 10)
        self.assertEqual(info['chroma_subsampling'], '420')

    def test_interlaced_detected(self):
        p = self._gen('int.mpg', ['-flags', '+ildct+ilme', '-top', '1', '-c:v', 'mpeg2video'])
        info = parse_ffprobe(p)
        self.assertIs(info['interlaced'], True)

    def test_single_track_streams_list_length_one(self):
        # The common case: one video + one audio stream still populates the new list
        # fields (length 1), matching the existing scalar fields at index 0.
        path = os.path.join(self._dir, 'single.ts')
        subprocess.run([
            _FFMPEG, '-y', '-hide_banner', '-loglevel', 'error',
            '-f', 'lavfi', '-i', 'testsrc2=size=320x240:rate=25:duration=1',
            '-f', 'lavfi', '-i', 'sine=duration=1',
            '-map', '0:v', '-map', '1:a',
            '-c:v', 'libx264', '-c:a', 'aac',
            '-f', 'mpegts', path,
        ], capture_output=True, check=True)
        info = parse_ffprobe(path)
        self.assertEqual(len(info['video_tracks']), 1)
        self.assertEqual(len(info['audio_tracks']), 1)
        self.assertEqual(info['video_tracks'][0]['codec'], info['video_codec'])
        self.assertEqual(info['audio_tracks'][0]['codec'], info['audio_codec'])


@unittest.skipUnless(_HAVE_FFMPEG, 'ffmpeg/ffprobe not available')
class ParseFfprobeMultiTrackTests(unittest.TestCase):
    """Multi-track detection (dev/changelog/564) - parse_ffprobe must enumerate every
    stream instead of stopping at the first video/first audio match. Empirical: generates
    a real 2-audio-track mpegts clip and asserts against ffprobe's actual JSON shape on
    this machine, per CLAUDE.md's external-tools-verified-empirically rule."""

    def setUp(self):
        self._dir = tempfile.mkdtemp(prefix='dvr_mt_test_')

    def tearDown(self):
        shutil.rmtree(self._dir, ignore_errors=True)

    def _gen_multi_audio(self):
        path = os.path.join(self._dir, 'multi.ts')
        subprocess.run([
            _FFMPEG, '-y', '-hide_banner', '-loglevel', 'error',
            '-f', 'lavfi', '-i', 'testsrc2=size=320x240:rate=25:duration=1',
            '-f', 'lavfi', '-i', 'sine=frequency=1000:duration=1',
            '-f', 'lavfi', '-i', 'sine=frequency=440:duration=1',
            '-map', '0:v', '-map', '1:a', '-map', '2:a',
            '-c:v', 'libx264', '-c:a', 'aac',
            '-metadata:s:a:0', 'language=eng',
            '-metadata:s:a:1', 'language=spa',
            '-f', 'mpegts', path,
        ], capture_output=True, check=True)
        return path

    def test_extra_audio_tracks_enumerated(self):
        p = self._gen_multi_audio()
        info = parse_ffprobe(p)
        self.assertEqual(len(info['video_tracks']), 1)
        self.assertEqual(len(info['audio_tracks']), 2)
        self.assertEqual(info['audio_tracks'][0]['language'], 'eng')
        self.assertEqual(info['audio_tracks'][1]['language'], 'spa')

    def test_scalar_fields_still_describe_track_zero(self):
        # Every other caller (recorder, postprocessor, watchdog) reads only the scalar
        # fields - they must be unaffected by the extra tracks now being enumerated.
        p = self._gen_multi_audio()
        info = parse_ffprobe(p)
        self.assertEqual(info['audio_codec'], info['audio_tracks'][0]['codec'])
        self.assertEqual(info['audio_language'], 'eng')


class BuildTestDictQualityTests(unittest.TestCase):
    """The serializer surfaces every quality-profile field to the JSON API."""

    @classmethod
    def setUpClass(cls):
        from tests.support import make_test_app
        cls.t = make_test_app()

    @classmethod
    def tearDownClass(cls):
        cls.t.cleanup()

    def test_dict_carries_quality_fields(self):
        from app import db
        from tests.support.seed import make_account, make_channel, make_channel_test
        from app.routes.channel_tests import _build_test_dict
        with self.t.app.app_context():
            acc = make_account()
            ch = make_channel(acc)
            t = make_channel_test(
                ch, status='COMPLETED', connected=True,
                video_codec='hevc', pix_fmt='yuv420p10le', bit_depth=10,
                chroma_subsampling='420', interlaced=False, coded_resolution='1920x1088',
                is_vfr=True, bits_per_pixel_frame=0.1234,
                timeline_gap_count=3, timeline_gap_seconds=2.5)
            db.session.commit()
            d = _build_test_dict(t)
        self.assertEqual(d['video_codec'], 'hevc')
        self.assertEqual(d['bit_depth'], 10)
        self.assertEqual(d['chroma_subsampling'], '420')
        self.assertIs(d['interlaced'], False)
        self.assertEqual(d['coded_resolution'], '1920x1088')
        self.assertIs(d['is_vfr'], True)
        self.assertEqual(d['bits_per_pixel_frame'], 0.1234)
        self.assertEqual(d['timeline_gap_count'], 3)
        self.assertEqual(d['timeline_gap_seconds'], 2.5)

    def test_all_null_test_serializes_cleanly(self):
        # An old/failed test row (all quality columns NULL) must serialize without error
        # and carry None, not raise - the detail page renders these.
        from app import db
        from tests.support.seed import make_account, make_channel, make_channel_test
        from app.routes.channel_tests import _build_test_dict
        with self.t.app.app_context():
            acc = make_account()
            ch = make_channel(acc)
            t = make_channel_test(ch, all_null=True)
            db.session.commit()
            d = _build_test_dict(t)
        self.assertIsNone(d['video_codec'])
        self.assertIsNone(d['bits_per_pixel_frame'])
        self.assertIsNone(d['interlaced'])


class FmtChromaTests(unittest.TestCase):
    """app/fmt_utils.fmt_chroma - the one home for '422' -> '4:2:2' (dev/changelog/351)."""

    def test_expands_digits(self):
        from app.fmt_utils import fmt_chroma
        self.assertEqual(fmt_chroma('420'), '4:2:0')
        self.assertEqual(fmt_chroma('422'), '4:2:2')
        self.assertEqual(fmt_chroma('444'), '4:4:4')

    def test_none_and_empty_pass_through(self):
        from app.fmt_utils import fmt_chroma
        self.assertIsNone(fmt_chroma(None))
        self.assertIsNone(fmt_chroma(''))


class ProfileSummaryTests(unittest.TestCase):
    """The compact one-line profile used by the Test History Profile column."""

    def test_clean_progressive_cfr(self):
        from app.fmt_utils import profile_summary
        self.assertEqual(
            profile_summary('h264', 8, '420', False, False, resolution='1920x1080'),
            'h264 - 8-bit - 4:2:0')

    def test_interlaced_uses_scan_height(self):
        from app.fmt_utils import profile_summary
        self.assertEqual(
            profile_summary('h264', 8, '420', True, False, resolution='1920x1080'),
            'h264 - 8-bit - 4:2:0 - 1080i')

    def test_interlaced_without_resolution_falls_back_to_word(self):
        from app.fmt_utils import profile_summary
        self.assertEqual(profile_summary('h264', 8, '420', True, False),
                         'h264 - 8-bit - 4:2:0 - Interlaced')

    def test_vfr_flag_appended(self):
        from app.fmt_utils import profile_summary
        self.assertEqual(
            profile_summary('hevc', 10, '422', True, True, resolution='1920x1088'),
            'hevc - 10-bit - 4:2:2 - 1088i - VFR')

    def test_partial_row_skips_missing_parts(self):
        from app.fmt_utils import profile_summary
        self.assertEqual(profile_summary('h264', None, None, False, False), 'h264')

    def test_empty_row_is_none(self):
        # A test predating quality capture must yield None so the cell renders its own
        # placeholder instead of a run of bare separators.
        from app.fmt_utils import profile_summary
        self.assertIsNone(profile_summary(None, None, None, None, None))

    def test_no_em_dash_in_separator(self):
        # CLAUDE.md bans em dashes in anything this project outputs.
        from app.fmt_utils import profile_summary
        out = profile_summary('h264', 10, '422', True, True, resolution='1920x1080')
        self.assertNotIn('—', out)
        self.assertNotIn('–', out)


class QualityProfileStatsTests(unittest.TestCase):
    """channels.py::_quality_profile_stats - the Health card's stream-quality cards."""

    @classmethod
    def setUpClass(cls):
        from tests.support import make_test_app
        cls.t = make_test_app()

    @classmethod
    def tearDownClass(cls):
        cls.t.cleanup()

    def _stats(self, **kw):
        from app import db
        from tests.support.seed import make_account, make_channel, make_channel_test
        from app.routes.channels import _quality_profile_stats
        with self.t.app.app_context():
            ch = make_channel(make_account())
            t = make_channel_test(ch, status='COMPLETED', connected=True, **kw)
            db.session.commit()
            return {s['label']: s for s in _quality_profile_stats(t)}

    def test_pre_feature_test_yields_no_cards(self):
        # An old row must leave the grid at its original six cards, not add a row of dashes.
        from app.routes.channels import _quality_profile_stats
        from app import db
        from tests.support.seed import make_account, make_channel, make_channel_test
        with self.t.app.app_context():
            ch = make_channel(make_account())
            t = make_channel_test(ch, all_null=True)
            db.session.commit()
            self.assertEqual(_quality_profile_stats(t), [])

    def test_none_test_yields_no_cards(self):
        from app.routes.channels import _quality_profile_stats
        self.assertEqual(_quality_profile_stats(None), [])

    def test_clean_profile_cards(self):
        s = self._stats(video_codec='h264', pix_fmt='yuv420p', bit_depth=8,
                        chroma_subsampling='420', interlaced=False, is_vfr=False,
                        bits_per_pixel_frame=0.0821)
        self.assertEqual(s['Codec']['value'], 'h264')
        self.assertEqual(s['Bit Depth']['value'], '8-bit')
        self.assertEqual(s['Chroma']['value'], '4:2:0')
        self.assertEqual(s['Scan']['value'], 'Progressive')
        self.assertEqual(s['Frame Rate']['value'], 'Constant')
        self.assertEqual(s['Efficiency']['value'], '0.0821')
        self.assertIsNone(s['Scan']['cls'])
        self.assertIsNone(s['Frame Rate']['cls'])

    def test_interlaced_and_vfr_are_amber(self):
        s = self._stats(video_codec='mpeg2video', bit_depth=8, chroma_subsampling='420',
                        interlaced=True, is_vfr=True)
        self.assertEqual(s['Scan']['value'], 'Interlaced')
        self.assertEqual(s['Scan']['cls'], 'text-warning')
        self.assertEqual(s['Frame Rate']['value'], 'Variable')
        self.assertEqual(s['Frame Rate']['cls'], 'text-warning')

    def test_tristate_null_renders_unknown_not_a_missing_card(self):
        # NULL interlaced/is_vfr mean "not determined", which IS the measurement - the card
        # must still appear rather than silently vanishing.
        s = self._stats(video_codec='h264', interlaced=None, is_vfr=None)
        self.assertEqual(s['Scan']['value'], 'Unknown')
        self.assertEqual(s['Frame Rate']['value'], 'Unknown')

    def test_zero_timeline_gaps_render_no_card(self):
        s = self._stats(video_codec='h264', timeline_gap_count=0, timeline_gap_seconds=0.0)
        self.assertNotIn('Timeline Gaps', s)

    def test_timeline_gaps_card_is_red_when_present(self):
        s = self._stats(video_codec='h264', timeline_gap_count=3, timeline_gap_seconds=2.53)
        self.assertEqual(s['Timeline Gaps']['value'], '3 (2.5s)')
        self.assertEqual(s['Timeline Gaps']['cls'], 'text-danger')

    def test_missing_optional_values_skip_their_card(self):
        s = self._stats(video_codec='h264', bit_depth=None, chroma_subsampling=None,
                        bits_per_pixel_frame=None)
        self.assertNotIn('Bit Depth', s)
        self.assertNotIn('Chroma', s)
        self.assertNotIn('Efficiency', s)

    def test_single_track_counts_render_no_tracks_card(self):
        # The normal case (1 video, 1 audio) must not add a card - only a genuine
        # multi-track finding is worth a line in the grid.
        s = self._stats(video_codec='h264', video_track_count=1, audio_track_count=1)
        self.assertNotIn('Tracks', s)

    def test_multi_audio_tracks_render_a_card(self):
        import json
        extra = [{'type': 'audio', 'codec': 'aac', 'channels': 2, 'language': 'spa'}]
        s = self._stats(video_codec='h264', video_track_count=1, audio_track_count=2,
                        extra_tracks=json.dumps(extra))
        self.assertIn('Tracks', s)
        self.assertEqual(s['Tracks']['value'], '2 audio')
        self.assertIn('aac', s['Tracks']['tip'])
        self.assertIn('2ch', s['Tracks']['tip'])
        self.assertIn('spa', s['Tracks']['tip'])

    def test_multi_video_and_audio_tracks_both_named(self):
        s = self._stats(video_codec='h264', video_track_count=2, audio_track_count=3,
                        extra_tracks='[]')
        self.assertEqual(s['Tracks']['value'], '2 video, 3 audio')

    def test_null_track_counts_render_no_card(self):
        # Old rows (predating this feature) leave these columns NULL - must not 500 or
        # add a phantom card.
        s = self._stats(video_codec='h264', video_track_count=None, audio_track_count=None)
        self.assertNotIn('Tracks', s)


class ChannelDetailQualityRenderTests(unittest.TestCase):
    """The channel detail page actually renders the profile (dev/changelog/351)."""

    @classmethod
    def setUpClass(cls):
        from tests.support import make_test_app
        cls.t = make_test_app()

    @classmethod
    def tearDownClass(cls):
        cls.t.cleanup()

    def _page(self, **kw):
        from app import db
        from tests.support.seed import make_account, make_channel, make_channel_test
        with self.t.app.app_context():
            ch = make_channel(make_account())
            make_channel_test(ch, status='COMPLETED', connected=True,
                              resolution='1920x1080', **kw)
            db.session.commit()
            cid = ch.id
        return self.t.app.test_client().get(f'/channels/{cid}').get_data(as_text=True)

    def test_profile_cards_and_column_render(self):
        html = self._page(video_codec='hevc', pix_fmt='yuv422p10le', bit_depth=10,
                          chroma_subsampling='422', interlaced=True, is_vfr=False,
                          bits_per_pixel_frame=0.1234, coded_resolution='1920x1088')
        self.assertIn('Bit Depth', html)
        self.assertIn('10-bit', html)
        self.assertIn('4:2:2', html)
        self.assertIn('Interlaced', html)
        self.assertIn('0.1234', html)
        # Test History gains a Profile column carrying the compact summary.
        self.assertIn('>Profile<', html)
        self.assertIn('hevc - 10-bit - 4:2:2 - 1080i', html)
        # Coded size rides as a tooltip on Resolution, never its own card.
        self.assertIn('Coded 1920x1088', html)
        self.assertNotIn('>Coded<', html)

    def test_pre_feature_test_page_renders_without_profile(self):
        # Nullable columns in a Jinja loop are a known defect class - an old row must not 500.
        html = self._page(all_null=True)
        self.assertIn('Test History', html)
        self.assertIn('>Profile<', html)
        self.assertNotIn('Bit Depth', html)

    def test_multi_track_card_renders_on_page(self):
        import json
        extra = [{'type': 'audio', 'codec': 'aac', 'channels': 2, 'language': 'spa'}]
        html = self._page(video_codec='h264', video_track_count=1, audio_track_count=2,
                          extra_tracks=json.dumps(extra))
        self.assertIn('Tracks', html)
        self.assertIn('2 audio', html)


if __name__ == '__main__':
    unittest.main()
