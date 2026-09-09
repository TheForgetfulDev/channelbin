"""Tier 1 pure units for the logging-layer credential backstop
(app/url_utils.py::mask_creds_in_text, CredentialMaskingFilter).

Guards BUGS.md 2026-07-18 (IPTV credentials written to dvr.log in plaintext). The
call-site fixes in accounts.py/recorder.py are necessary but not sufficient: `requests`
exceptions stringify with the full credentialed URL, so the message AND the formatted
traceback of any log record must come out masked no matter which site emitted it.
"""
import logging
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.url_utils import CredentialMaskingFilter, mask_creds_in_text  # noqa: E402

XTREAM_URL = 'http://host:8080/get.php?username=joe&password=s3cret&type=m3u'
BARE_URL = 'http://cdn.example/joe/s3cret/456.ts'


def _record(msg, args=(), exc_info=None):
    return logging.LogRecord('test', logging.ERROR, __file__, 1, msg, args, exc_info)


class MaskCredsInTextTests(unittest.TestCase):
    def test_masks_url_embedded_in_prose(self):
        out = mask_creds_in_text(f'Fetching M3U for account 3 from {XTREAM_URL} now')
        self.assertNotIn('joe', out)
        self.assertNotIn('s3cret', out)
        self.assertIn('Fetching M3U for account 3', out)

    def test_masks_bare_path_url_with_trailing_punctuation(self):
        # The bare <user>/<pass>/<id> rule is ^...$-anchored, so a trailing period would
        # defeat it unless the punctuation is split off first.
        out = mask_creds_in_text(f'Connection to {BARE_URL}. Retrying.')
        self.assertNotIn('joe', out)
        self.assertNotIn('s3cret', out)
        self.assertIn('456.ts', out)

    def test_masks_url_inside_parens(self):
        out = mask_creds_in_text(f'failed ({BARE_URL})')
        self.assertNotIn('s3cret', out)

    def test_text_without_url_passes_through_identically(self):
        text = 'Sync complete for account 3: 120 channels, 4000 EPG entries'
        self.assertEqual(mask_creds_in_text(text), text)

    def test_empty_and_none_are_safe(self):
        self.assertEqual(mask_creds_in_text(''), '')
        self.assertIsNone(mask_creds_in_text(None))

    def test_idempotent(self):
        once = mask_creds_in_text(f'from {XTREAM_URL}')
        self.assertEqual(mask_creds_in_text(once), once)


class CredentialMaskingFilterTests(unittest.TestCase):
    def setUp(self):
        self.filter = CredentialMaskingFilter()

    def test_masks_plain_message(self):
        rec = _record(f'Fetching XMLTV from {XTREAM_URL}')
        self.assertTrue(self.filter.filter(rec))
        self.assertNotIn('s3cret', rec.getMessage())
        self.assertNotIn('joe', rec.getMessage())

    def test_masks_message_built_from_args(self):
        rec = _record('Fetching M3U for account %d from %s', (3, XTREAM_URL))
        self.filter.filter(rec)
        msg = rec.getMessage()
        self.assertNotIn('s3cret', msg)
        self.assertIn('account 3', msg)

    def test_masks_exception_traceback(self):
        # The real leak shape: requests raises with the credentialed URL in its message,
        # and log.exception() renders the whole traceback into the handler's output.
        try:
            raise ValueError(f'Max retries exceeded with url: {XTREAM_URL}')
        except ValueError:
            rec = _record('Sync failed', (), sys.exc_info())
        self.filter.filter(rec)
        formatted = logging.Formatter('%(message)s').format(rec)
        self.assertNotIn('s3cret', formatted)
        self.assertNotIn('joe', formatted)
        self.assertIn('Max retries exceeded', formatted)

    def test_record_without_credentials_is_untouched(self):
        rec = _record('Parsed %d streams', (120,))
        self.filter.filter(rec)
        self.assertEqual(rec.args, (120,))
        self.assertEqual(rec.getMessage(), 'Parsed 120 streams')

    def test_attached_to_handler_masks_emitted_output(self):
        emitted = []

        class _Capture(logging.Handler):
            def emit(self, record):
                emitted.append(self.format(record))

        handler = _Capture()
        handler.addFilter(self.filter)
        logger = logging.getLogger('test_credential_log_masking.emit')
        logger.propagate = False
        logger.setLevel(logging.INFO)
        logger.addHandler(handler)
        try:
            logger.info('Fetching M3U for account %d from %s', 3, XTREAM_URL)
        finally:
            logger.removeHandler(handler)

        self.assertEqual(len(emitted), 1)
        self.assertNotIn('s3cret', emitted[0])
        self.assertNotIn('joe', emitted[0])


if __name__ == '__main__':
    unittest.main(verbosity=2)
