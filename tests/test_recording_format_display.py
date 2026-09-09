"""Tier 2 - the stream format profile on the recording detail page (dev/changelog/336).

Mostly characterization, not regression guards: dev/changelog/335 taught the watchdog and the
health gather to persist the format profile, and until dev/changelog/336 none of it reached a
screen.
Two things here are real hazards rather than characterizations:

- NULL must stay distinguishable from a real value. `interlaced` and `is_vfr` are tri-state
  (NULL = unknown/undetermined), so rendering NULL as "Progressive"/"Constant" would assert
  something the probe never said - the one-flag-one-meaning defect class.
- Every column in the profile is NULL on every recording that predates the feature, which is
  the common case, not the edge case. A pre-feature row must render without a 500 and without
  six empty placeholder rows.

The helper tests build plain namespaces rather than ORM rows: _format_profile must be a pure
function of the object it is handed (it runs while rendering a page, and CLAUDE.md's
no-hidden-I/O rule covers per-row template work), so passing it something with no session at
all is itself the assertion.

No ffmpeg, no network, no /dvr - the render tests go through the test client.
"""
import os
import sys
import types
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tests.support.app import make_test_app  # noqa: E402
from tests.support import seed  # noqa: E402
from app import db  # noqa: E402
from app.database import RecordingSegment  # noqa: E402
from app.routes.recordings import _format_profile  # noqa: E402

_SEG_FIELDS = ('probe_video_codec', 'probe_pix_fmt', 'probe_bit_depth',
               'probe_chroma_subsampling', 'probe_interlaced', 'probe_coded_resolution',
               'probe_is_vfr', 'probe_fps')
_REC_FIELDS = ('recorded_video_codec', 'recorded_pix_fmt', 'recorded_bit_depth',
               'recorded_chroma_subsampling', 'recorded_interlaced',
               'recorded_coded_resolution', 'recorded_is_vfr', 'recorded_fps',
               'recorded_bits_per_pixel_frame', 'recorded_audio_sample_rate',
               'recorded_audio_bitrate_kbps', 'recorded_audio_language')

# A full capture-side profile, as the watchdog writes it.
FULL_SEG = {'probe_video_codec': 'hevc', 'probe_pix_fmt': 'yuv422p10le',
            'probe_bit_depth': 10, 'probe_chroma_subsampling': '422',
            'probe_interlaced': True, 'probe_coded_resolution': '1920x1088',
            'probe_is_vfr': False, 'probe_fps': 29.97}
FULL_REC = {'recorded_video_codec': 'h264', 'recorded_pix_fmt': 'yuv420p',
            'recorded_bit_depth': 8, 'recorded_chroma_subsampling': '420',
            'recorded_interlaced': False, 'recorded_coded_resolution': None,
            'recorded_is_vfr': True, 'recorded_fps': 59.94,
            'recorded_bits_per_pixel_frame': 0.0421}


def _seg(**kw):
    return types.SimpleNamespace(**{f: kw.get(f) for f in _SEG_FIELDS})


def _rec(segments=(), **kw):
    ns = types.SimpleNamespace(**{f: kw.get(f) for f in _REC_FIELDS})
    ns.segments = list(segments)
    return ns


def _row(view, label):
    for r in view['rows']:
        if r['label'] == label:
            return r
    raise AssertionError(f'no {label!r} row in {[r["label"] for r in view["rows"]]}')


class SourcePrecedenceTests(unittest.TestCase):
    """Same order as _tech_parts (DESIGN.md section 5): the capture-time segment probe
    describes the ORIGINAL stream and wins; recorded_* describes the final/converted file
    and is the labelled fallback."""

    def test_segment_probe_wins_over_the_output_columns(self):
        view = _format_profile(_rec(segments=[_seg(**FULL_SEG)], **FULL_REC))

        self.assertEqual(view['source'], 'capture')
        self.assertEqual(_row(view, 'Codec')['value'], 'hevc')
        self.assertEqual(_row(view, 'Bit depth')['value'], '10-bit')
        self.assertEqual(_row(view, 'Scan')['value'], 'Interlaced')

    def test_last_probed_segment_wins(self):
        early = dict(FULL_SEG, probe_video_codec='mpeg2video')
        view = _format_profile(_rec(segments=[_seg(**early), _seg(**FULL_SEG)]))

        self.assertEqual(_row(view, 'Codec')['value'], 'hevc')

    def test_a_segment_probed_before_the_format_columns_existed_is_skipped(self):
        """probe_resolution without probe_video_codec is a pre-migration-20 segment. It
        must not shadow the recording's own format columns with an empty profile."""
        view = _format_profile(_rec(segments=[_seg()], **FULL_REC))

        self.assertEqual(view['source'], 'output')
        self.assertEqual(_row(view, 'Codec')['value'], 'h264')

    def test_output_fallback_says_so_on_every_row_it_sourced(self):
        view = _format_profile(_rec(**FULL_REC))

        self.assertEqual(view['source'], 'output')
        for label in ('Codec', 'Bit depth', 'Chroma', 'Scan', 'Frame rate'):
            self.assertIn('final output file', _row(view, label)['tip'] or '',
                          f'{label} came from recorded_* and must say which file it describes')

    def test_capture_rows_do_not_claim_to_be_the_output_file(self):
        view = _format_profile(_rec(segments=[_seg(**FULL_SEG)]))

        for label in ('Codec', 'Bit depth', 'Scan', 'Frame rate'):
            self.assertNotIn('final output file', _row(view, label)['tip'] or '')


class NullHandlingTests(unittest.TestCase):
    """NULL means unknown and must never render as a measured value."""

    def test_unknown_scan_is_not_rendered_as_progressive(self):
        view = _format_profile(_rec(segments=[_seg(probe_video_codec='h264')]))

        scan = _row(view, 'Scan')
        self.assertEqual(scan['value'], 'Unknown')
        self.assertIsNone(scan['cls'], 'unknown scan must not be flagged amber')

    def test_undetermined_frame_rate_is_not_rendered_as_constant(self):
        view = _format_profile(_rec(segments=[_seg(probe_video_codec='h264', probe_fps=25.0)]))

        rate = _row(view, 'Frame rate')
        self.assertEqual(rate['value'], '25.0 fps')
        self.assertNotIn('constant', rate['value'])
        self.assertIsNone(rate['cls'])

    def test_missing_fields_render_the_dash_placeholder_never_an_em_dash(self):
        """CLAUDE.md bans new em dashes; this page uses '-' for every missing value."""
        view = _format_profile(_rec(segments=[_seg(probe_video_codec='h264')]))

        for label in ('Bit depth', 'Chroma'):
            value = _row(view, label)['value']
            self.assertEqual(value, '-')
            self.assertNotIn('—', value)
            self.assertNotIn('mdash', value)

    def test_nothing_known_produces_no_rows(self):
        view = _format_profile(_rec(segments=[_seg()]))

        self.assertIsNone(view['source'])
        self.assertEqual(view['rows'], [])
        self.assertIsNone(view['coded'])


class FlagAndFormatTests(unittest.TestCase):

    def test_interlaced_is_amber(self):
        view = _format_profile(_rec(segments=[_seg(probe_video_codec='h264',
                                                  probe_interlaced=True)]))

        self.assertEqual(_row(view, 'Scan')['cls'], 'warn')

    def test_progressive_is_not_amber(self):
        view = _format_profile(_rec(segments=[_seg(probe_video_codec='h264',
                                                  probe_interlaced=False)]))

        scan = _row(view, 'Scan')
        self.assertEqual(scan['value'], 'Progressive')
        self.assertIsNone(scan['cls'])

    def test_vfr_is_amber(self):
        view = _format_profile(_rec(segments=[_seg(probe_video_codec='h264',
                                                  probe_is_vfr=True)]))

        rate = _row(view, 'Frame rate')
        self.assertEqual(rate['value'], 'Variable')
        self.assertEqual(rate['cls'], 'warn')

    def test_cfr_reports_the_rate_and_is_not_amber(self):
        view = _format_profile(_rec(segments=[_seg(probe_video_codec='h264',
                                                  probe_is_vfr=False, probe_fps=59.94)]))

        rate = _row(view, 'Frame rate')
        self.assertEqual(rate['value'], '59.9 fps constant')
        self.assertIsNone(rate['cls'])

    def test_chroma_digits_become_colon_form(self):
        view = _format_profile(_rec(segments=[_seg(probe_video_codec='h264',
                                                  probe_chroma_subsampling='422')]))

        self.assertEqual(_row(view, 'Chroma')['value'], '4:2:2')

    def test_coded_resolution_is_returned_for_a_tooltip_not_a_row(self):
        """Agreed design: Resolution already conveys the frame size, so there is no
        Coded row - at most a note on the existing Video row."""
        view = _format_profile(_rec(segments=[_seg(**FULL_SEG)]))

        self.assertEqual(view['coded'], '1920x1088')
        self.assertNotIn('Coded', [r['label'] for r in view['rows']])


class EfficiencyTests(unittest.TestCase):
    """bpp has no probe_* counterpart - a still-growing segment cannot give a reliable
    bitrate (dev/changelog/335) - so it is always output-derived and must say so."""

    def test_efficiency_omitted_when_unknown(self):
        view = _format_profile(_rec(segments=[_seg(**FULL_SEG)]))

        self.assertNotIn('Efficiency', [r['label'] for r in view['rows']])

    def test_efficiency_says_which_file_it_measured_even_on_a_capture_profile(self):
        view = _format_profile(_rec(segments=[_seg(**FULL_SEG)],
                                    recorded_bits_per_pixel_frame=0.0421))

        eff = _row(view, 'Efficiency')
        self.assertEqual(eff['value'], '0.0421')
        self.assertEqual(view['source'], 'capture')
        self.assertIn('final output file', eff['tip'])
        self.assertIn('not a better/worse verdict', eff['tip'])


class AudioNoteTests(unittest.TestCase):
    """The three audio columns dev/changelog/335 added have no row of their own; they ride
    on the existing Audio row's tooltip, which the 'AAC 2.0' label drops."""

    def test_audio_note_carries_the_recorded_trio(self):
        view = _format_profile(_rec(recorded_audio_sample_rate=48000,
                                    recorded_audio_bitrate_kbps=192.0,
                                    recorded_audio_language='eng'))

        self.assertIn('48 kHz', view['audio_note'])
        self.assertIn('192 kb/s', view['audio_note'])
        self.assertIn('eng', view['audio_note'])
        self.assertIn('Output audio', view['audio_note'])

    def test_audio_note_is_none_when_nothing_was_measured(self):
        self.assertIsNone(_format_profile(_rec())['audio_note'])

    def test_partial_audio_detail_still_renders(self):
        view = _format_profile(_rec(recorded_audio_language='spa'))

        self.assertEqual(view['audio_note'], 'Output audio: spa')


class DetailPageRenderTests(unittest.TestCase):

    def setUp(self):
        self.t = make_test_app()

    def tearDown(self):
        self.t.cleanup()

    def _get(self, rec_id):
        res = self.t.client.get(f'/recordings/{rec_id}')
        self.assertEqual(res.status_code, 200)
        return res.get_data(as_text=True)

    def _completed(self, **kw):
        rec = seed.make_recording(status='COMPLETED', name='fmt-render', **kw)
        db.session.commit()
        return rec

    def _add_segment(self, rec, **kw):
        db.session.add(RecordingSegment(recording_id=rec.id, segment_number=0,
                                        file_path='/dvr/seg.ts',
                                        started_at=rec.start_time, ended_at=rec.stop_time,
                                        **kw))
        db.session.commit()

    def test_capture_profile_renders(self):
        rec = self._completed()
        self._add_segment(rec, probe_resolution='1920x1080', **FULL_SEG)

        html = self._get(rec.id)
        for label in ('Codec', 'Bit depth', 'Chroma', 'Scan', 'Frame rate'):
            self.assertIn(f'>{label}<', html)
        self.assertIn('hevc', html)
        self.assertIn('10-bit', html)
        self.assertIn('4:2:2', html)
        self.assertIn('Interlaced', html)

    def test_interlaced_row_is_amber(self):
        rec = self._completed()
        self._add_segment(rec, **FULL_SEG)

        html = self._get(rec.id)
        self.assertIn('class="sv warn', html.split('>Scan<')[1][:200])

    def test_coded_size_rides_on_the_video_tooltip(self):
        rec = self._completed(recorded_resolution='1920x1080', recorded_fps=29.97)
        self._add_segment(rec, probe_resolution='1920x1080', **FULL_SEG)

        html = self._get(rec.id)
        self.assertIn('Encoder frame size: 1920x1088.', html)

    def test_audio_note_reaches_the_audio_row_tooltip(self):
        rec = self._completed(recorded_audio_codec='aac', recorded_audio_channels=2,
                              recorded_audio_sample_rate=48000,
                              recorded_audio_bitrate_kbps=192.0,
                              recorded_audio_language='eng')
        db.session.commit()

        html = self._get(rec.id)
        self.assertIn('Output audio: 48 kHz', html)

    def test_pre_feature_recording_shows_one_explanatory_row_not_six_empty_ones(self):
        rec = self._completed(recorded_resolution='1920x1080', recorded_fps=29.97)
        db.session.commit()

        html = self._get(rec.id)
        self.assertIn('predates stream format capture', html)
        self.assertNotIn('>Bit depth<', html)
        self.assertNotIn('>Chroma<', html)

    def test_scheduled_recording_renders_no_profile_at_all(self):
        rec = seed.make_recording(status='SCHEDULED', name='fmt-sched')
        db.session.commit()

        html = self._get(rec.id)
        self.assertNotIn('>Codec<', html)
        self.assertNotIn('predates stream format capture', html)

    def test_all_null_recording_does_not_500(self):
        """Every format column is NULL on every pre-feature row - the common case."""
        rec = self._completed(with_segment=True)
        db.session.commit()

        html = self._get(rec.id)
        self.assertIn('predates stream format capture', html)

    def test_output_only_profile_labels_itself(self):
        rec = self._completed(**FULL_REC)
        db.session.commit()

        html = self._get(rec.id)
        self.assertIn('h264', html)
        self.assertIn('Variable', html)
        self.assertIn('final output file', html)
        self.assertIn('0.0421', html)


if __name__ == '__main__':
    unittest.main()
