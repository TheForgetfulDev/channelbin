"""A missing ffprobe is loud, and is never mistaken for a fact about the stream.

`parse_ffprobe()` returns {} for two entirely different things: "this file has no video
stream", which is a correct answer about the file, and "there is no ffprobe on this
machine", which is a configuration error. Until dev/changelog/911 both arrived as the same
empty dict down one `log.debug` line, so a pip install without a system ffmpeg produced a
working recorder and a dead probe path - no format detection, no recording health numbers,
no format lock - with nothing anywhere saying why, and three surfaces then rendered a
verdict off that empty dict which blamed the provider's stream for ChannelBin's own
install. CLAUDE.md's "one flag, one meaning" and principle 1, in one defect.

What each case guards:

  * The three ffprobe spawn sites tell a spawn that failed on argv[0] from every other
    failure, and report the first kind. The distinction is drawn from the OSError itself,
    never from a cached lookup, so it is still right for a tool removed after startup.
  * Reporting resets the toolchain cache and re-runs report_tool_state, which is what
    raises the standing EXTERNAL_TOOL_MISSING alert outside create_app() and a settings
    save - the only two places it otherwise runs.
  * It is latched: one report per process, not one per segment probe.
  * The three verdict surfaces name the missing binary instead of the stream.

Never spawns a real ffprobe: `resolve_ffprobe_path` is patched to a name that cannot
exist, which produces the genuine FileNotFoundError the production path sees rather than a
simulated one.

Runs against a throwaway temp SQLite DB - never the live dvr.db.
  python3 -m unittest tests.test_missing_ffprobe
"""
import json
import os
import tempfile
import unittest
from unittest.mock import patch

from app import probe as probe_mod
from app import toolchain
from app.alerts import EXTERNAL_TOOL_MISSING
from app.database import Alert
from tests.support.app import make_test_app

# An argv[0] that cannot resolve on any machine, so subprocess raises the real
# FileNotFoundError rather than the test asserting against a mock of it.
NO_SUCH_PROBE = '/nonexistent/channelbin-test/ffprobe'


class _MissingProbeMixin:
    def setUp(self):
        probe_mod._missing_reported = False
        toolchain.reset_cache()

    def tearDown(self):
        probe_mod._missing_reported = False
        toolchain.reset_cache()

    def _tempfile(self):
        fd, path = tempfile.mkstemp(suffix='.ts')
        os.write(fd, b'\x47' + b'\x00' * 512)
        os.close(fd)
        self.addCleanup(lambda: os.path.exists(path) and os.remove(path))
        return path


class SpawnFailureIsReportedTests(_MissingProbeMixin, unittest.TestCase):
    """The probe module itself - no app context, so no alert half.

    Asserted through the WARNING these emit rather than through a mock of the reporting
    function, so what fails without the fix is the behavior (nothing is said) rather than
    a mock target that does not exist yet.
    """

    def _probe_with_no_ffprobe(self, call):
        """Run `call` with an unresolvable ffprobe and return its result.

        Two assertions, because either alone is satisfiable by code that does not draw the
        distinction. The WARNING has to name the binary and where it was looked for -
        scan_video_timeline already logged one that named neither, blaming the file it was
        reading. And report_tool_state has to run, which is what carries the fact out of
        the log and onto a surface an operator sees without going digging.
        """
        with patch('app.probe.resolve_ffprobe_path', lambda: NO_SUCH_PROBE), \
             patch.object(toolchain, 'report_tool_state') as report_state, \
             self.assertLogs('app.probe', level='WARNING') as logs:
            result = call()
        self.assertTrue(any(NO_SUCH_PROBE in line and 'Could not run ffprobe' in line
                            for line in logs.output),
                        f'no warning naming the missing binary: {logs.output}')
        report_state.assert_called_once()
        return result

    def test_parse_ffprobe_reports_a_missing_binary(self):
        path = self._tempfile()
        self.assertEqual(
            self._probe_with_no_ffprobe(
                lambda: probe_mod.parse_ffprobe(path, count_packets=False)),
            {})

    def test_count_packets_path_reports_it_too(self):
        """The two spawn shapes are different call sites - run_probe_until_stalled uses
        Popen and subprocess.run is only the header-only branch - so a fix applied to one
        leaves the other silent."""
        path = self._tempfile()
        self.assertEqual(
            self._probe_with_no_ffprobe(
                lambda: probe_mod.parse_ffprobe(path, count_packets=True)),
            {})

    def test_nominal_video_rate_reports_a_missing_binary(self):
        path = self._tempfile()
        self.assertIsNone(
            self._probe_with_no_ffprobe(lambda: probe_mod.nominal_video_rate(path)))

    def test_scan_video_timeline_reports_a_missing_binary(self):
        path = self._tempfile()
        self.assertEqual(
            self._probe_with_no_ffprobe(lambda: probe_mod.scan_video_timeline(path)), {})

    def test_a_probe_failure_that_is_about_the_file_is_not_reported(self):
        """The other half of the invariant, and the one that makes this a distinction
        rather than a blanket alarm: a probe that failed over what it was reading must NOT
        be reported as a missing tool. Characterization - it holds before the fix too, and
        it is here to stop a later widening of the catch from swallowing the difference
        back up."""
        path = self._tempfile()

        with patch('app.probe.subprocess.run', side_effect=ValueError('unparseable')), \
             self.assertNoLogs('app.probe', level='WARNING'):
            self.assertEqual(probe_mod.parse_ffprobe(path, count_packets=False), {})

    def test_the_report_is_latched(self):
        """Every segment of every recording reaches a spawn site, so an unlatched report
        would re-run the toolchain probe - two process spawns - per segment."""
        path = self._tempfile()
        with patch('app.probe.resolve_ffprobe_path', lambda: NO_SUCH_PROBE), \
             patch.object(toolchain, 'report_tool_state') as report_state:
            for _ in range(3):
                probe_mod.parse_ffprobe(path, count_packets=False)
        self.assertEqual(report_state.call_count, 1)


class MissingProbeRaisesTheStandingAlertTests(_MissingProbeMixin, unittest.TestCase):
    """The alert half, which is what makes a tool that vanished AFTER startup loud."""

    def setUp(self):
        super().setUp()
        self.t = make_test_app()
        self.addCleanup(self.t.cleanup)

    def test_a_probe_that_cannot_run_ffprobe_raises_the_standing_alert(self):
        path = self._tempfile()
        self.assertEqual(Alert.query.filter_by(alert_type=EXTERNAL_TOOL_MISSING).count(), 0)
        # Two patches, two consumers: app/probe.py asks for a path, app/toolchain.py asks
        # describe_ffprobe_resolution for the path AND its provenance. Patching only the
        # first leaves the standing alert describing this machine's real ffprobe.
        with patch('app.probe.resolve_ffprobe_path', lambda: NO_SUCH_PROBE), \
             patch('app.config.describe_ffprobe_resolution',
                   lambda *a, **k: (NO_SUCH_PROBE, 'path')):
            probe_mod.parse_ffprobe(path, count_packets=False)
        alerts = Alert.query.filter_by(alert_type=EXTERNAL_TOOL_MISSING,
                                       source=f'{toolchain.ALERT_SOURCE_PREFIX}ffprobe').all()
        self.assertEqual(len(alerts), 1)
        self.assertIn('ffprobe', alerts[0].title)
        self.assertIn('External tools', alerts[0].body)


class VerdictSurfacesNameTheRealReasonTests(_MissingProbeMixin, unittest.TestCase):
    """The three places that rendered a verdict off the ambiguous {}."""

    def setUp(self):
        super().setUp()
        self.t = make_test_app()
        # CSRF is app-wide and a token is the part a browser supplies; tests/test_csrf.py
        # owns the protection itself.
        self.t.app.config['WTF_CSRF_ENABLED'] = False
        self.addCleanup(self.t.cleanup)

    def test_capture_health_distinguishes_the_two_empty_probes(self):
        """The DIAGNOSTICS event is the only record of a recording's missing numbers that
        survives on the artifact, so it is the one that must not say the capture failed
        when the truth is that nothing could read it."""
        from app.postprocessor import _gather_recording_health
        path = self._tempfile()
        cfg = {'recording': {'gather_health_data': True}}

        with patch('app.probe.parse_ffprobe', return_value={}), \
             patch('app.toolchain.ffprobe_missing', return_value=True):
            fields, diags = _gather_recording_health(1, path, None, cfg)
        self.assertIsNone(fields)
        self.assertTrue(diags['extra'].get('probe_unavailable'), diags)
        self.assertNotIn('probe_failed', diags['extra'])
        self.assertIn('ffprobe is not installed', diags['detail'])

        with patch('app.probe.parse_ffprobe', return_value={}), \
             patch('app.toolchain.ffprobe_missing', return_value=False):
            fields, diags = _gather_recording_health(1, path, None, cfg)
        self.assertTrue(diags['extra']['probe_failed'])
        self.assertNotIn('probe_unavailable', diags['extra'])

    def test_a_health_check_log_does_not_blame_the_channel(self):
        from app.channel_tester import empty_probe_warning
        with patch('app.channel_tester.ffprobe_missing', return_value=True):
            line = empty_probe_warning()
        self.assertIn('ffprobe is not installed', line)
        self.assertIn('External tools', line)
        self.assertNotIn('found no video stream', line)

        with patch('app.channel_tester.ffprobe_missing', return_value=False):
            self.assertEqual(empty_probe_warning(),
                             'ffprobe found no video stream in recording')

    def test_test_url_does_not_report_a_good_stream_as_unusable(self):
        """The worst of the three: bytes demonstrably arrived, so "received no usable
        audio/video stream" is ChannelBin reporting its own missing binary as a fault in
        the provider's feed."""
        def _fake_run(cmd, *a, **kw):
            with open(cmd[-1], 'wb') as fh:      # the capture ffmpeg would have written
                fh.write(b'\x47' + b'\x00' * 4096)

            class _R:
                returncode = 0
                stdout = ''
                stderr = ''
            return _R()

        with patch('subprocess.run', side_effect=_fake_run), \
             patch('app.probe.parse_ffprobe', return_value={}), \
             patch('app.toolchain.ffprobe_missing', return_value=True):
            resp = self.t.client.post('/api/recordings/test-url',
                                      json={'url': 'http://example.test/live/1'})
        self.assertEqual(resp.status_code, 503)
        body = resp.get_json()['error']
        self.assertIn('ffprobe is not installed', body)
        self.assertNotIn('no usable audio/video stream', body)

        with patch('subprocess.run', side_effect=_fake_run), \
             patch('app.probe.parse_ffprobe', return_value={}), \
             patch('app.toolchain.ffprobe_missing', return_value=False):
            resp = self.t.client.post('/api/recordings/test-url',
                                      json={'url': 'http://example.test/live/1'})
        self.assertEqual(resp.status_code, 502)
        self.assertIn('no usable audio/video stream', resp.get_json()['error'])

    def test_the_diagnostics_event_renders_the_new_key_without_a_code_change(self):
        """DIAGNOSTICS is a generic carrier - the recording detail page must show the new
        flag with no per-key work, or the event exists and says nothing on screen."""
        from app.routes.recordings import diag_view_filter
        view = diag_view_filter(json.dumps({'kind': 'capture_health',
                                            'probe_unavailable': True}))
        self.assertEqual(view['rows'], [('Probe unavailable', 'yes')])


if __name__ == '__main__':
    unittest.main()
