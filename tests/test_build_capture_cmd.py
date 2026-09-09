"""Tier 1 pure units for the shared ffmpeg capture-command builder
(app/proc_utils.py::build_capture_cmd). Pure - takes a cfg dict, returns a list.

This is the single canonical home for the recorder / channel tester / manual URL test
command (CLAUDE.md "External tools ... build capture commands only via
proc_utils.build_capture_cmd()"), so its shape is a contract: http(s) inputs get the
reconnect flags, -re precedes -i, -t appears only for a positive duration, and the
stream-copy-to-file tail is always last.
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.proc_utils import build_capture_cmd, is_hls_url  # noqa: E402

_RECONNECT_FLAGS = ('-reconnect', '-reconnect_streamed', '-reconnect_at_eof',
                    '-reconnect_delay_max')


def _cfg(extra_input=None, extra_output=None):
    return {'ffmpeg': {
        'path': 'ffmpeg',
        'extra_input_args': extra_input or [],
        'extra_output_args': extra_output or [],
    }}


class BuildCaptureCmdTests(unittest.TestCase):
    def test_http_gets_reconnect_flags(self):
        cmd = build_capture_cmd(_cfg(), 'http://h/live/1', '/out.ts')
        for flag in ('-reconnect', '-reconnect_streamed', '-reconnect_at_eof',
                     '-reconnect_delay_max'):
            self.assertIn(flag, cmd)

    def test_https_gets_reconnect_flags(self):
        cmd = build_capture_cmd(_cfg(), 'https://h/live/1', '/out.ts')
        self.assertIn('-reconnect', cmd)

    def test_non_http_input_has_no_reconnect_flags(self):
        cmd = build_capture_cmd(_cfg(), 'udp://239.0.0.1:1234', '/out.ts')
        self.assertNotIn('-reconnect', cmd)

    def test_re_precedes_input(self):
        cmd = build_capture_cmd(_cfg(), 'http://h/live/1', '/out.ts')
        self.assertEqual(cmd[cmd.index('-i') - 1], '-re')
        self.assertEqual(cmd[cmd.index('-i') + 1], 'http://h/live/1')

    def test_duration_zero_omits_t_flag(self):
        self.assertNotIn('-t', build_capture_cmd(_cfg(), 'http://h/1', '/out.ts', 0))

    def test_positive_duration_adds_t_after_input(self):
        cmd = build_capture_cmd(_cfg(), 'http://h/1', '/out.ts', 30)
        self.assertEqual(cmd[cmd.index('-t') + 1], '30')
        # -t comes after -i (input duration limit)
        self.assertGreater(cmd.index('-t'), cmd.index('-i'))

    def test_tail_is_stream_copy_to_output(self):
        cmd = build_capture_cmd(_cfg(), 'http://h/1', '/dvr/x.ts')
        self.assertEqual(cmd[-4:], ['-c', 'copy', '-y', '/dvr/x.ts'])

    def test_extra_input_args_precede_input_output_args_precede_copy(self):
        cmd = build_capture_cmd(
            _cfg(extra_input=['-timeout', '5000000'], extra_output=['-map', '0']),
            'http://h/1', '/out.ts')
        self.assertLess(cmd.index('-timeout'), cmd.index('-i'))
        self.assertLess(cmd.index('-map'), cmd.index('-c'))
        self.assertGreater(cmd.index('-map'), cmd.index('-i'))


class RealtimePacingFlagTests(unittest.TestCase):
    """-re is opt-out per caller as of dev/changelog/437: bounded captures need it (a
    content-time -t is only a wall-clock bound while paced), the recorder's unbounded
    ones do not. Which caller passes what is asserted in tests/test_re_pacing.py; this
    class owns the argv shape either way."""

    def test_default_is_paced(self):
        """Unchanged for any caller that does not reason about pacing - -re is the safe
        value because it is what every caller received before the flag existed."""
        self.assertIn('-re', build_capture_cmd(_cfg(), 'http://h/live/1', '/out.ts'))

    def test_pace_realtime_false_omits_re(self):
        cmd = build_capture_cmd(_cfg(), 'http://h/live/1', '/out.ts', pace_realtime=False)
        self.assertNotIn('-re', cmd)

    def test_unpaced_input_is_still_well_formed(self):
        """Removing the flag must not disturb -i adjacency, the reconnect flags, or the
        stream-copy tail."""
        cmd = build_capture_cmd(_cfg(), 'http://h/live/1', '/dvr/x.ts', pace_realtime=False)
        self.assertEqual(cmd[cmd.index('-i') + 1], 'http://h/live/1')
        self.assertIn('-reconnect_at_eof', cmd)
        self.assertEqual(cmd[-4:], ['-c', 'copy', '-y', '/dvr/x.ts'])

    def test_unpaced_extra_input_args_still_precede_input(self):
        cmd = build_capture_cmd(_cfg(extra_input=['-timeout', '5000000']),
                                'http://h/1', '/out.ts', pace_realtime=False)
        self.assertLess(cmd.index('-timeout'), cmd.index('-i'))

    def test_pace_realtime_is_keyword_only(self):
        """Positional would collide with duration_seconds and silently turn a segment
        length into a boolean."""
        with self.assertRaises(TypeError):
            build_capture_cmd(_cfg(), 'http://h/1', '/out.ts', 30, False)


class HlsReconnectFlagTests(unittest.TestCase):
    """HLS (.m3u8) inputs must NOT carry the reconnect flags - -reconnect_at_eof makes
    ffmpeg spin re-fetching the playlist (EOF every fetch) and capture zero bytes, so the
    app could not record any HLS stream at all (BUGS.md 2026-07-18 HLS recording)."""

    def test_hls_m3u8_has_no_reconnect_flags(self):
        cmd = build_capture_cmd(_cfg(), 'http://h/live/a/b/2095900.m3u8', '/out.ts')
        for flag in _RECONNECT_FLAGS:
            self.assertNotIn(flag, cmd)

    def test_hls_m3u8_with_query_string_has_no_reconnect_flags(self):
        # query string must not defeat the .m3u8 detection
        cmd = build_capture_cmd(_cfg(), 'http://h/live/a/b/s.m3u8?token=xyz', '/out.ts')
        self.assertNotIn('-reconnect_at_eof', cmd)

    def test_hls_detection_is_case_insensitive(self):
        self.assertTrue(is_hls_url('http://h/a/STREAM.M3U8'))
        cmd = build_capture_cmd(_cfg(), 'http://h/a/STREAM.M3U8', '/out.ts')
        self.assertNotIn('-reconnect_at_eof', cmd)

    def test_non_hls_http_still_gets_reconnect_flags(self):
        # extensionless TS-over-HTTP "line" URL (the format the app is normally fed)
        for flag in _RECONNECT_FLAGS:
            self.assertIn(flag, build_capture_cmd(_cfg(), 'http://h/a/b/2095900', '/out.ts'))
        self.assertIn('-reconnect_at_eof', build_capture_cmd(_cfg(), 'http://h/a/b/x.ts', '/out.ts'))

    def test_hls_is_still_a_valid_capture_command(self):
        # dropping the reconnect flags must not drop -re/-i or the stream-copy tail
        cmd = build_capture_cmd(_cfg(), 'http://h/live/a/b/s.m3u8', '/dvr/x.ts', 25)
        self.assertEqual(cmd[cmd.index('-i') - 1], '-re')
        self.assertEqual(cmd[cmd.index('-i') + 1], 'http://h/live/a/b/s.m3u8')
        self.assertEqual(cmd[cmd.index('-t') + 1], '25')
        self.assertEqual(cmd[-4:], ['-c', 'copy', '-y', '/dvr/x.ts'])


if __name__ == '__main__':
    unittest.main(verbosity=2)
