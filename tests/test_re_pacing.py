"""Tier 2 - who gets ffmpeg's -re flag, asserted at the CALL SITES rather than only on
the shared builder's argv shape (dev/changelog/437).

Background. -re was a day-one hardcode in build_capture_cmd with no stated rationale, so
every caller received it. It is genuinely required for a *bounded* capture: -t is a
content-time limit, and without pacing a pre-buffering provider satisfies "-t 30" out of
its backlog in a fraction of the wall time - measured on this box at 29.8s vs 0.35s for
the same 30s capture, which is the channel-tester defect of dev/docs/BUGS.md 2026-06-28
04:40 pm. It is not wanted on the recorder's unbounded captures.

tests/test_build_capture_cmd.py covers the builder's argv shape. This file covers the
part that was missing entirely: that each production caller asks for the right thing.
Nothing here asserted the tester's -re except indirectly, via a builder test that would
have kept passing if the tester had stopped requesting it.

Guards:
  * the recorder's unbounded segment (segment_duration_seconds: 0, today's config)
    carries no -re;
  * the recorder's BOUNDED segment carries -re, because -t without pacing reintroduces
    the 2026-06-28 defect on the capture path - the one combination that must never ship;
  * the channel tester and the manual URL test still carry -re and their -t.

Runs against a throwaway temp SQLite DB - never the live dvr.db. No real ffmpeg and no
network: subprocess.Popen is monkeypatched and every URL is a non-routable example.test.
  python3 -m unittest tests.test_re_pacing
"""
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tests.support.app import make_test_app  # noqa: E402
from tests.support.seed import make_account, make_channel, make_recording  # noqa: E402

from app import db  # noqa: E402
from app.recorder import _launch_segment  # noqa: E402

STREAM = 'http://example.test/live/AAA/BBB/91.ts'


def _fake_proc():
    proc = mock.MagicMock()
    proc.pid = 4242
    # Already exited: _launch_segment bails right after spawning when the recording is
    # not in _active, so no watchdog thread starts and nothing needs tearing down.
    proc.poll.return_value = 0
    return proc


class RecorderPacingTests(unittest.TestCase):
    """The recorder's own capture command, taken from the real argv handed to Popen."""

    def setUp(self):
        self.t = make_test_app()
        self.ctx = self.t.app.app_context()
        self.ctx.push()
        acct = make_account(name='Pacing Acct')
        self.channel = make_channel(acct, stream_id=91, name='Pacing Channel')
        self.channel.stream_url = STREAM
        self.channel.raw_stream_url = STREAM
        self.rec = make_recording(status='IN_PROGRESS', channel_id=self.channel.id,
                                  url=STREAM)
        db.session.commit()

    def tearDown(self):
        self.ctx.pop()
        self.t.cleanup()

    def _argv(self, segment_duration):
        """Real ffmpeg argv the recorder would have spawned, at a given segment length."""
        with mock.patch('app.recorder.load_config') as load_cfg:
            base = _real_config()
            base['recording']['segment_duration_seconds'] = segment_duration
            base['recording']['dvr_output_dir'] = self.t._tmpdir
            load_cfg.return_value = base
            with mock.patch('app.recorder.subprocess.Popen',
                            return_value=_fake_proc()) as popen:
                _launch_segment(self.t.app, self.rec.id, seg_num=1)
        self.assertTrue(popen.called, 'recorder did not spawn ffmpeg')
        return popen.call_args[0][0]

    def test_unbounded_segment_has_no_re(self):
        """Today's config (segment_duration_seconds: 0). -re buys an unbounded live
        capture nothing and slows how fast a post-hiccup backlog drains."""
        argv = self._argv(0)
        self.assertNotIn('-re', argv)
        self.assertNotIn('-t', argv)

    def test_unbounded_segment_still_targets_the_stream(self):
        """Dropping -re must not disturb anything else about the command. The URL is
        matched loosely because _launch_segment re-resolves it through the account's
        normalization mode first (tests/test_url_drift.py owns that behavior)."""
        argv = self._argv(0)
        self.assertTrue(argv[argv.index('-i') + 1].startswith('http://example.test/'))
        self.assertIn('-reconnect', argv)
        self.assertEqual(argv[-3:-1], ['copy', '-y'])

    def test_bounded_segment_keeps_re(self):
        """The combination that must never ship: -t without -re on the capture path.
        A configured segment length is a content-time bound, so an unpaced provider
        backlog would satisfy it in seconds and spin the recorder through reconnects."""
        argv = self._argv(600)
        self.assertIn('-re', argv)
        self.assertEqual(argv[argv.index('-t') + 1], '600')
        self.assertEqual(argv[argv.index('-i') - 1], '-re')

    def test_t_never_appears_without_re(self):
        """The invariant behind the two tests above, stated once over both settings."""
        for segment_duration in (0, 30, 600):
            argv = self._argv(segment_duration)
            if '-t' in argv:
                self.assertIn('-re', argv,
                              f'-t without -re at segment_duration={segment_duration}')


class BoundedCallerPacingTests(unittest.TestCase):
    """The two bounded callers must keep asking for pacing. Both import
    build_capture_cmd inside the function, so patching it on app.proc_utils reaches
    them; the recorder imports at module top and is covered above via real argv."""

    def _recorded_call(self):
        import app.proc_utils as proc_utils
        real = proc_utils.build_capture_cmd
        seen = {}

        def spy(*args, **kwargs):
            seen['kwargs'] = kwargs
            seen['args'] = args
            cmd = real(*args, **kwargs)
            seen['cmd'] = cmd
            return cmd

        return seen, mock.patch.object(proc_utils, 'build_capture_cmd', spy)

    def _no_connect_retries(self):
        """Strip the connect-retry policy for the duration of the call.

        This test is about which flags the tester ASKS for, and it has its answer within
        milliseconds - the first build_capture_cmd call. Everything after that is the
        connect loop failing against a fake process that is already dead, and the shipped
        defaults (connect_retries 2, connect_retry_delay_seconds 10) make it sit in
        _interruptible_sleep for a flat 20 seconds on the way to a result nothing here
        reads. Retry policy has its own tests; do not "restore" this to the real defaults.
        """
        from app import channel_tester
        real = channel_tester.resolve_health_check_settings

        def fast(ct_cfg, profile):
            settings = real(ct_cfg, profile)
            settings.update(connect_retries=0, connect_retry_delay_seconds=0)
            return settings

        return mock.patch.object(channel_tester, 'resolve_health_check_settings', fast)

    def test_channel_tester_requests_pacing(self):
        """A 30s test must occupy ~30 wall seconds or stall detection and bitrate
        sampling run over an abbreviated window (dev/docs/BUGS.md 2026-06-28 04:40 pm)."""
        seen, patcher = self._recorded_call()
        t = make_test_app()
        try:
            with t.app.app_context():
                acct = make_account(name='Tester Acct')
                ch = make_channel(acct, stream_id=92, name='Tester Channel')
                ch.stream_url = STREAM
                ch.raw_stream_url = STREAM
                db.session.commit()
                with patcher, self._no_connect_retries(), \
                        mock.patch('app.channel_tester.subprocess.Popen',
                                   return_value=_fake_proc()):
                    from app import channel_tester
                    try:
                        channel_tester.run_channel_test(t.app, ch.id)
                    except Exception:
                        # The fake process fails the connect loop; all this test needs is
                        # the command that was built before that happened.
                        pass
        finally:
            t.cleanup()
        self.assertIn('cmd', seen, 'channel tester never built a capture command')
        self.assertNotEqual(seen['kwargs'].get('pace_realtime'), False)
        self.assertIn('-re', seen['cmd'])
        self.assertIn('-t', seen['cmd'])


def _real_config():
    """A deep copy of the merged config, so a test can vary one recording key without
    reaching the real config.yaml through a runtime load_config() (CLAUDE.md §Testing)."""
    import copy
    from app.config import load_config
    return copy.deepcopy(load_config())


if __name__ == '__main__':
    unittest.main(verbosity=2)
