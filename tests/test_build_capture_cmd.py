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

from app.proc_utils import (build_capture_cmd, is_hls_url,  # noqa: E402
                            read_timeout_is_inert)

_RECONNECT_FLAGS = ('-reconnect', '-reconnect_streamed', '-reconnect_at_eof',
                    '-reconnect_delay_max')


def _cfg(extra_input=None, extra_output=None, read_timeout=None):
    ff = {
        'path': 'ffmpeg',
        'extra_input_args': extra_input or [],
        'extra_output_args': extra_output or [],
    }
    if read_timeout is not None:
        ff['read_timeout_seconds'] = read_timeout
    return {'ffmpeg': ff}


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


class ReadTimeoutFlagTests(unittest.TestCase):
    """-rw_timeout on every http(s) capture, so a provider that goes silent without closing
    the socket cannot block ffmpeg's read forever - which is what made ffmpeg ignore the
    watchdog's SIGTERM and cost a SIGKILL plus a whole new segment per stall
    (dev/docs/BUGS.md 2026-09-14 @ 07:05:04 AM ET)."""

    def test_value_is_microseconds_not_seconds(self):
        """The 1,000,000x bug this test exists for: seconds on the wire would time out
        every read after 5 microseconds and reconnect continuously."""
        cmd = build_capture_cmd(_cfg(read_timeout=5), 'http://h/live/1', '/out.ts')
        self.assertEqual(cmd[cmd.index('-rw_timeout') + 1], '5000000')

    def test_flag_precedes_input(self):
        cmd = build_capture_cmd(_cfg(read_timeout=5), 'http://h/live/1', '/out.ts')
        self.assertLess(cmd.index('-rw_timeout'), cmd.index('-i'))

    def test_user_extra_input_args_can_override_it(self):
        """Emitted BEFORE extra_input_args, like -user_agent: ffmpeg takes the LAST
        occurrence of a repeated option, so a user's own value has to come after ours."""
        cmd = build_capture_cmd(_cfg(extra_input=['-rw_timeout', '9000000'], read_timeout=5),
                                'http://h/live/1', '/out.ts')
        occurrences = [i for i, a in enumerate(cmd) if a == '-rw_timeout']
        self.assertEqual(len(occurrences), 2)
        self.assertEqual(cmd[occurrences[0] + 1], '5000000')
        self.assertEqual(cmd[occurrences[1] + 1], '9000000')

    def test_zero_disables_the_flag(self):
        self.assertNotIn('-rw_timeout',
                         build_capture_cmd(_cfg(read_timeout=0), 'http://h/live/1', '/out.ts'))

    def test_missing_key_disables_the_flag(self):
        """A config dict from before this setting existed must not crash or guess."""
        self.assertNotIn('-rw_timeout',
                         build_capture_cmd(_cfg(), 'http://h/live/1', '/out.ts'))

    def test_non_http_input_has_no_read_timeout(self):
        self.assertNotIn('-rw_timeout',
                         build_capture_cmd(_cfg(read_timeout=5), 'udp://239.0.0.1:1234',
                                           '/out.ts'))

    def test_hls_does_get_the_read_timeout(self):
        """Deliberately unlike the reconnect flags: those are excluded from HLS because a
        playlist GET returns EOF by design, which is not a read that hangs. Measured over
        six real .m3u8 channels with and without - same bytes, same exit codes, same wall
        clock (dev/changelog/958)."""
        cmd = build_capture_cmd(_cfg(read_timeout=5), 'http://h/live/a/s.m3u8', '/out.ts')
        self.assertEqual(cmd[cmd.index('-rw_timeout') + 1], '5000000')
        self.assertNotIn('-reconnect_at_eof', cmd)

    def test_fractional_seconds_round_to_whole_microseconds(self):
        cmd = build_capture_cmd(_cfg(read_timeout=2.5), 'http://h/live/1', '/out.ts')
        self.assertEqual(cmd[cmd.index('-rw_timeout') + 1], '2500000')


class ReadTimeoutInertTests(unittest.TestCase):
    """A read timeout at or above watchdog.stall_timeout_seconds never fires - the watchdog
    kills the capture first - so the setting is on but does nothing. Warned, never blocked:
    both numbers are legitimately the user's to choose."""

    def _cfg(self, read_timeout, stall_timeout):
        return {'ffmpeg': {'read_timeout_seconds': read_timeout},
                'watchdog': {'stall_timeout_seconds': stall_timeout}}

    def test_read_timeout_below_stall_timeout_is_live(self):
        self.assertFalse(read_timeout_is_inert(self._cfg(5, 10))[0])

    def test_equal_is_inert(self):
        self.assertTrue(read_timeout_is_inert(self._cfg(10, 10))[0])

    def test_above_is_inert(self):
        self.assertTrue(read_timeout_is_inert(self._cfg(30, 10))[0])

    def test_disabled_read_timeout_is_not_inert(self):
        """0 is off on purpose, which is not the same as on-but-useless."""
        self.assertFalse(read_timeout_is_inert(self._cfg(0, 10))[0])

    def test_missing_keys_are_not_inert(self):
        self.assertFalse(read_timeout_is_inert({})[0])

    def test_returns_both_numbers_for_the_message(self):
        inert, read_timeout, stall_timeout = read_timeout_is_inert(self._cfg(30, 10))
        self.assertTrue(inert)
        self.assertEqual((read_timeout, stall_timeout), (30, 10))


if __name__ == '__main__':
    unittest.main(verbosity=2)
