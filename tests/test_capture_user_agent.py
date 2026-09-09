"""Every network capture identifies itself with http.user_agent
(dev/docs/BUGS.md 2026-08-09, dev/changelog/523).

http.user_agent was wired only into account sync's requests headers
(accounts.py::_request_headers); no ffmpeg invocation set -user_agent at all, so every
recording, health check and manual URL test announced itself with ffmpeg's own default -
measured on this box as 'Lavf/60.16.100' - which providers commonly filter. The failure
mode is silent: a blocked stream looks like a channel that simply does not work.

build_capture_cmd() is the single builder for all three network-capture call sites
(recorder.py, channel_tester.py, routes/recordings.py), so these argv-level assertions
cover all three. The header cannot be asserted on the wire here: tests/support/netguard.py
refuses any subprocess.Popen whose argv contains an http(s) URL, which is the correct guard
and is deliberately not relaxed - the on-the-wire proof is the loopback measurement recorded
in dev/changelog/523.
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.proc_utils import build_capture_cmd  # noqa: E402


def _cfg(user_agent='VLC/3.0.18 LibVLC/3.0.18', extra_input_args=None):
    return {
        'ffmpeg': {'path': 'ffmpeg', 'extra_input_args': extra_input_args or [],
                   'extra_output_args': []},
        'http': {'user_agent': user_agent},
    }


class CaptureUserAgentTests(unittest.TestCase):

    def test_http_capture_sends_configured_user_agent(self):
        cmd = build_capture_cmd(_cfg(), 'http://example.test/live/9', '/tmp/out.ts')
        self.assertIn('-user_agent', cmd)
        self.assertEqual(cmd[cmd.index('-user_agent') + 1], 'VLC/3.0.18 LibVLC/3.0.18')

    def test_https_capture_sends_user_agent(self):
        cmd = build_capture_cmd(_cfg(), 'https://example.test/live/9', '/tmp/out.ts')
        self.assertIn('-user_agent', cmd)

    def test_hls_capture_sends_user_agent_too(self):
        """HLS is excluded from the reconnect flags but must still be identified - it is
        the shape most of the real streams take, and -user_agent propagates to the segment
        GETs, not just the playlist GET."""
        cmd = build_capture_cmd(_cfg(), 'https://example.test/live/9.m3u8', '/tmp/out.ts')
        self.assertIn('-user_agent', cmd)
        self.assertNotIn('-reconnect', cmd)

    def test_custom_user_agent_is_honored(self):
        cmd = build_capture_cmd(_cfg(user_agent='Mozilla/5.0 (TestAgent)'),
                                'http://example.test/live/9', '/tmp/out.ts')
        self.assertEqual(cmd[cmd.index('-user_agent') + 1], 'Mozilla/5.0 (TestAgent)')

    def test_non_http_input_gets_no_user_agent(self):
        """-user_agent is an http-protocol option; a local file input must not carry it."""
        cmd = build_capture_cmd(_cfg(), '/dvr/incomplete/seg0.ts', '/tmp/out.ts')
        self.assertNotIn('-user_agent', cmd)

    def test_extra_input_args_override_wins(self):
        """A user who sets their own -user_agent in extra_input_args must still win.
        ffmpeg takes the LAST occurrence of a repeated option, so ours has to be emitted
        first - if this ordering inverts, the escape hatch silently stops working."""
        cmd = build_capture_cmd(
            _cfg(extra_input_args=['-user_agent', 'CustomAgent/1.0']),
            'http://example.test/live/9', '/tmp/out.ts')
        first = cmd.index('-user_agent')
        last = len(cmd) - 1 - cmd[::-1].index('-user_agent')
        self.assertNotEqual(first, last, 'expected both the default and the override')
        self.assertEqual(cmd[last + 1], 'CustomAgent/1.0')

    def test_user_agent_precedes_the_input_flag(self):
        """It has to be an input option, i.e. before -i, or ffmpeg ignores it."""
        cmd = build_capture_cmd(_cfg(), 'http://example.test/live/9', '/tmp/out.ts')
        self.assertLess(cmd.index('-user_agent'), cmd.index('-i'))

    def test_missing_http_block_falls_back_to_the_shipped_default(self):
        """A config predating the http block must not crash the capture path."""
        cfg = {'ffmpeg': {'path': 'ffmpeg', 'extra_input_args': [], 'extra_output_args': []}}
        cmd = build_capture_cmd(cfg, 'http://example.test/live/9', '/tmp/out.ts')
        self.assertEqual(cmd[cmd.index('-user_agent') + 1], 'VLC/3.0.18 LibVLC/3.0.18')


if __name__ == '__main__':
    unittest.main()
