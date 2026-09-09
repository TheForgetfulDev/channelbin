"""Guards BUGS.md 2026-08-14 "ffmpeg stderr credentials reach ChannelTest.error_detail
unmasked" (dev/changelog/639).

`_extract_stderr_error`'s keyword list includes 'http', so the ffmpeg stderr line most
likely to be selected is the one embedding the full stream URL - ffmpeg routinely prints
the input URL in its error output. That line used to flow unmasked into `fail_class` ->
`error_detail`, which is persisted to ChannelTest.error_detail, rendered on health-check
and channel pages, echoed into the in-memory run log, and copied into recording events by
run_pre_check. Stream-URL path segments are credentials for Xtream accounts.

Invariants asserted here:
  * `_extract_stderr_error` masks credentials in the line it selects (unit-level, no DB).
  * A connect failure whose ffmpeg stderr embeds a credentialed URL persists a masked
    ChannelTest.error_detail, end to end through run_channel_test().
  * A Popen launch failure whose exception message embeds a credentialed URL is masked
    the same way ("Failed to launch ffmpeg: ...").
  * The generic catch-all exception handler masks its exception message the same way
    before it reaches the ring buffer / error_detail.

No real ffmpeg: subprocess.Popen is monkeypatched, following the pattern in
tests/test_tester_preemption.py.
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from unittest import mock  # noqa: E402

from tests.support.app import make_test_app  # noqa: E402
from tests.support import seed  # noqa: E402
from app import db  # noqa: E402
from app.database import ChannelTest  # noqa: E402
from app import channel_tester  # noqa: E402

CREDENTIALED_URL = 'http://example.test/live/testsecretuser/testsecretpass/12345'


class FakeProc:
    """Stands in for a Popen'd ffmpeg that has already exited with an error, so the
    tester classifies it via stderr rather than as a hung connection."""

    def __init__(self, stderr_lines=(), returncode=1):
        self.stderr = list(stderr_lines)
        self._returncode = returncode

    def poll(self):
        return self._returncode

    def terminate(self):
        self._returncode = self._returncode if self._returncode is not None else -15

    def kill(self):
        self._returncode = self._returncode if self._returncode is not None else -9

    def wait(self, timeout=None):
        return self._returncode


class ExtractStderrErrorMaskingTests(unittest.TestCase):
    """Pure unit coverage - no DB, no Flask."""

    def test_masks_credentials_in_the_selected_line(self):
        lines = [
            'Input #0, mpegts, from \'%s\':' % CREDENTIALED_URL,
            'HTTP error 403 Forbidden fetching %s' % CREDENTIALED_URL,
        ]
        result = channel_tester._extract_stderr_error(lines)
        self.assertNotIn('testsecretuser', result)
        self.assertNotIn('testsecretpass', result)
        self.assertIn('***', result)

    def test_no_url_present_is_unaffected(self):
        lines = ['some other error with no url in it']
        result = channel_tester._extract_stderr_error(lines)
        self.assertEqual(result, 'some other error with no url in it')

    def test_empty_input_returns_empty_string(self):
        self.assertEqual(channel_tester._extract_stderr_error([]), '')


class ChannelTesterErrorDetailMaskingTests(unittest.TestCase):
    def setUp(self):
        self.t = make_test_app()
        with self.t.app.app_context():
            acct = seed.make_account(name='Masking Test Account')
            ch = seed.make_channel(acct, name='Masking Test Channel')
            ch.stream_url = CREDENTIALED_URL
            db.session.commit()
            self.channel_id = ch.id
        # No _reset_run_state() here: make_test_app() has already swapped in a fresh
        # RunState via reset_module_globals(), and _reset_run_state() is a start-of-run
        # function that takes the KIND_TESTER admission ticket (dev/changelog/723).

    def tearDown(self):
        # _end_run(), not _reset_run_state(): a test that drove a run to completion may
        # still hold the ticket, and this is the one path that gives it back.
        channel_tester._end_run()
        self.t.cleanup()

    def _error_detail_for(self, test_id):
        with self.t.app.app_context():
            db.session.expire_all()
            row = db.session.get(ChannelTest, test_id)
            return row.error_detail

    def test_stderr_credentials_masked_in_persisted_error_detail(self):
        """Every connect attempt exits immediately with a stderr line embedding the
        credentialed stream URL - exactly what a real 403/404 from ffmpeg looks like."""
        def _popen(*a, **kw):
            return FakeProc(stderr_lines=[f'HTTP error 403 Forbidden: {CREDENTIALED_URL}'])

        with mock.patch.object(channel_tester.subprocess, 'Popen', _popen), \
             mock.patch.object(channel_tester, 'wait_for_file_data', lambda *a, **kw: False), \
             mock.patch.object(channel_tester, '_interruptible_sleep', lambda *a, **kw: None):
            test_id = channel_tester.run_channel_test(self.t.app, self.channel_id)

        self.assertIsNotNone(test_id)
        detail = self._error_detail_for(test_id)
        self.assertIsNotNone(detail)
        self.assertNotIn('testsecretuser', detail)
        self.assertNotIn('testsecretpass', detail)
        self.assertIn('403', detail)

    def test_popen_launch_failure_masks_credentials_in_error_detail(self):
        """A Popen() exception whose message embeds a credentialed URL - the
        'Failed to launch ffmpeg: ...' path."""
        def _popen(*a, **kw):
            raise OSError(f'could not exec ffmpeg for {CREDENTIALED_URL}')

        with mock.patch.object(channel_tester.subprocess, 'Popen', _popen):
            test_id = channel_tester.run_channel_test(self.t.app, self.channel_id)

        self.assertIsNotNone(test_id)
        detail = self._error_detail_for(test_id)
        self.assertIsNotNone(detail)
        self.assertNotIn('testsecretuser', detail)
        self.assertNotIn('testsecretpass', detail)
        self.assertIn('Failed to launch ffmpeg', detail)

    def test_generic_exception_masks_credentials_in_error_detail(self):
        """The outer catch-all - anything raised after a successful Popen() that
        stringifies with the credentialed URL embedded, e.g. a probing failure."""
        def _popen(*a, **kw):
            return FakeProc(stderr_lines=[], returncode=None)

        def _blow_up(*a, **kw):
            raise RuntimeError(f'unexpected failure talking to {CREDENTIALED_URL}')

        with mock.patch.object(channel_tester.subprocess, 'Popen', _popen), \
             mock.patch.object(channel_tester, 'wait_for_file_data', _blow_up):
            test_id = channel_tester.run_channel_test(self.t.app, self.channel_id)

        self.assertIsNotNone(test_id)
        detail = self._error_detail_for(test_id)
        self.assertIsNotNone(detail)
        self.assertNotIn('testsecretuser', detail)
        self.assertNotIn('testsecretpass', detail)


if __name__ == '__main__':
    unittest.main()
