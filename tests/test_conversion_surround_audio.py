"""Tier 2 - 5.1 audio through the mp4 conversion, with real ffmpeg on local files
(dev/docs/BUGS.md 2026-09-29 11:57, dev/changelog/1163).

Broadcast 5.1 arrives as AC-3 or E-AC-3. Two defects in the audio-copy fallback:
  1. It always named -bsf:a aac_adtstoasc, a filter ffmpeg refuses on any codec but AAC, so
     every fallback attempt on an AC-3/E-AC-3 (or MP2) source failed at startup.
  2. A re-encode's resumable parts are fragmented mp4 without delay_moov: a copied AC-3 track
     cannot be written into one at all, and copied 5.1 AAC is written with a header that
     decodes as errors all the way through while ffmpeg exits 0.
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.postprocessor import (  # noqa: E402
    ConversionResult, finalize_part, join_conversion_parts,
)
from tests.test_conversion_damaged_source import _ConversionCase, _config  # noqa: E402

_HAVE_FFMPEG = bool(shutil.which('ffmpeg') and shutil.which('ffprobe'))


def _make_source(path, codec, *, channels=6, bitrate='384k'):
    """Four seconds of video plus six distinct tones (or one, for stereo) in an mpegts."""
    cmd = ['ffmpeg', '-hide_banner', '-loglevel', 'error', '-y',
           '-f', 'lavfi', '-i', 'testsrc2=size=320x180:rate=25']
    if channels == 6:
        for f in (220, 330, 440, 550, 660, 770):
            cmd += ['-f', 'lavfi', '-i', f'sine=f={f}:sample_rate=48000']
        # 5.1(side), what broadcast AC-3 decodes as: copied into a fragmented part without
        # delay_moov, AAC in this layout decodes as "channel element 1.0 is not allocated"
        # where plain 5.1 happens not to.
        cmd += ['-filter_complex',
                '[1][2][3][4][5][6]join=inputs=6:channel_layout=5.1(side)[a]',
                '-map', '0:v', '-map', '[a]']
    else:
        cmd += ['-f', 'lavfi', '-i', 'sine=f=440:sample_rate=48000', '-ac', '2']
    cmd += ['-t', '4', '-c:v', 'libx264', '-preset', 'ultrafast', '-c:a', codec,
            '-b:a', bitrate, '-f', 'mpegts', path]
    subprocess.run(cmd, check=True)


def _audio(path):
    out = subprocess.run(['ffprobe', '-v', 'error', '-select_streams', 'a',
                          '-show_entries', 'stream=codec_name,channels', '-of', 'json', path],
                         capture_output=True, text=True, check=True).stdout
    return [(s['codec_name'], s['channels']) for s in json.loads(out)['streams']]


def _audio_decode_errors(path):
    run = subprocess.run(['ffmpeg', '-v', 'error', '-i', path, '-map', '0:a', '-f', 'null', '-'],
                         capture_output=True, text=True)
    return [ln for ln in run.stderr.splitlines() if ln.strip()]


@unittest.skipUnless(_HAVE_FFMPEG, 'needs ffmpeg and ffprobe on PATH')
class SurroundConversionTests(_ConversionCase):

    @classmethod
    def setUpClass(cls):
        cls._sources = tempfile.mkdtemp(prefix='surround-src-')

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls._sources, ignore_errors=True)

    def _use_source(self, codec, *, channels=6, bitrate='384k'):
        """Put the source at the recording's .ts path, encoding each variant once per class."""
        cached = os.path.join(self._sources, f'{codec}_{channels}_{bitrate}.ts')
        if not os.path.exists(cached):
            _make_source(cached, codec, channels=channels, bitrate=bitrate)
        shutil.copyfile(cached, self.ts)

    def _commands(self, **pp):
        """The first and the last conversion command, the last being the audio-copy
        fallback because every attempt dies at the same source position."""
        stub = self._run(_config(**pp), lambda *a, **k: ConversionResult(
            False, 'died', 'Conversion failed!', out_time=2.0))
        return stub.call_args_list[0][0][2], stub.call_args_list[-1][0][2]

    def _execute(self, cmd, name):
        out = os.path.join(self.t._tmpdir, name)
        run = subprocess.run(cmd[:-1] + [out], stdout=subprocess.DEVNULL,
                             stderr=subprocess.PIPE, text=True)
        self.assertEqual(run.returncode, 0, run.stderr[-400:])
        return out

    def _fallback(self, codec, bitrate, reencode_mode, *, channels=6):
        self._use_source(codec, channels=channels, bitrate=bitrate)
        _first, fallback = self._commands(reencode_mode=reencode_mode)
        self.assertEqual(fallback[fallback.index('-c:a') + 1], 'copy', 'sanity: the fallback')
        return fallback

    # ── defect 1: the AAC-only filter named on every codec ──────────────────────────

    def test_fallback_converts_ac3_51(self):
        out = self._execute(self._fallback('ac3', '384k', 'never'), 'ac3.mp4')
        self.assertEqual(_audio(out), [('ac3', 6)])
        self.assertEqual(_audio_decode_errors(out), [])

    def test_fallback_converts_eac3_51(self):
        out = self._execute(self._fallback('eac3', '192k', 'never'), 'eac3.mp4')
        self.assertEqual(_audio(out), [('eac3', 6)])
        self.assertEqual(_audio_decode_errors(out), [])

    def test_fallback_names_the_adts_filter_for_aac_only(self):
        self.assertNotIn('-bsf:a', self._fallback('ac3', '384k', 'never'))
        self.assertIn('-bsf:a', self._fallback('aac', '128k', 'never', channels=2))

    def test_fallback_still_converts_stereo_adts_aac(self):
        out = self._execute(self._fallback('aac', '128k', 'never', channels=2), 'aac.mp4')
        self.assertEqual(_audio(out), [('aac', 2)])
        self.assertEqual(_audio_decode_errors(out), [])

    # ── defect 2: the resumable part container ──────────────────────────────────────

    def test_fallback_part_holds_ac3_51_under_a_video_reencode(self):
        out = self._execute(self._fallback('ac3', '384k', 'always'), 'part.mp4')
        self.assertEqual(_audio(out), [('ac3', 6)])
        self.assertEqual(_audio_decode_errors(out), [])

    def test_fallback_part_holding_aac_51_decodes_cleanly(self):
        # Without delay_moov ffmpeg exits 0 here and every audio frame fails to decode.
        out = self._execute(self._fallback('aac', '384k', 'always'), 'part.mp4')
        self.assertEqual(_audio(out), [('aac', 6)])
        self.assertEqual(_audio_decode_errors(out), [])

    def test_killed_ac3_part_is_kept_and_joins(self):
        part = self._execute(self._fallback('ac3', '384k', 'always'), '.out.part1.mp4')
        raw = open(part, 'rb').read()
        with open(part, 'wb') as fh:
            fh.write(raw[:int(len(raw) * 0.7)])
        kept = finalize_part(part, 'ffmpeg', scratch_key='t', interval=1,
                             pre_output_timeout=30, stall_seconds=30)
        self.assertIsNotNone(kept, 'the part up to its last whole fragment is usable')
        out = os.path.join(self.t._tmpdir, 'joined.mp4')
        result = join_conversion_parts([part], out, 'ffmpeg', scratch_key='t', interval=1,
                                       pre_output_timeout=30, stall_seconds=30)
        self.assertTrue(result.success, result.error_msg)
        self.assertEqual(_audio(out), [('ac3', 6)])

    # ── characterization: the default path already kept 5.1 ────────────────────────

    def test_default_audio_reencode_keeps_six_channels(self):
        self._use_source('ac3')
        first, _fallback = self._commands(reencode_mode='never')
        self.assertEqual(first[first.index('-c:a') + 1], 'aac', 'sanity: the default path')
        out = self._execute(first, 'reencoded.mp4')
        self.assertEqual(_audio(out), [('aac', 6)])


if __name__ == '__main__':
    unittest.main()
