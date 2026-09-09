"""Tier 1 pure units for app/url_utils.py::redact_urls_in_text - the blunter,
scheme-agnostic redactor used only by the support bundle export (app/support_bundle.py).

Unlike mask_creds/mask_url_path (which preserve the host and only mask the
credential-shaped part, for on-screen/log diagnostics that never leave this machine),
this function keeps only the scheme and replaces everything after '://' - per the
call that even a bare domain is private in the IPTV context this app serves, for an
artifact (the support bundle) that might leave the machine.
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.url_utils import redact_urls_in_text, REDACTED_URL_TEXT  # noqa: E402


class RedactUrlsInTextTests(unittest.TestCase):

    def test_https_url_keeps_scheme_loses_everything_else(self):
        self.assertEqual(redact_urls_in_text('https://example.com'),
                         'https://[url redacted]')

    def test_http_url_with_path_keeps_scheme_only(self):
        self.assertEqual(redact_urls_in_text('http://example.com/1/2/3'),
                         'http://[url redacted]')

    def test_rtsp_scheme_is_preserved(self):
        self.assertEqual(redact_urls_in_text('rtsp://example.com/a/b/c'),
                         'rtsp://[url redacted]')

    def test_udp_scheme_is_preserved(self):
        self.assertEqual(redact_urls_in_text('udp://1.2.3.4.example.com/test'),
                         'udp://[url redacted]')

    def test_arbitrary_uppercase_scheme_is_preserved_verbatim(self):
        """Not restricted to a known protocol list - any <scheme>://... token matches,
        exactly the stated requirement ('basically anything ://')."""
        self.assertEqual(redact_urls_in_text('ANYTHING://example.com'),
                         'ANYTHING://[url redacted]')

    def test_url_embedded_in_a_log_line(self):
        out = redact_urls_in_text(
            'Fetching M3U for account 1 from https://example-provider.test/TESTPATHTOKEN2/'
            '?movies=false&series=false')
        self.assertEqual(
            out, 'Fetching M3U for account 1 from https://[url redacted]')

    def test_real_world_query_token_shape_is_fully_redacted(self):
        """The Pluto TV / mediatailor shape found via live verification during this
        item's work: a filled-in token query param that mask_creds() (only recognizing
        username=/password=) did not catch. Host and token are fake stand-ins - only
        the shape is load-bearing (dev/changelog/519)."""
        out = redact_urls_in_text(
            'https://fake-cdn.invalid/v1/master/xyz/master.m3u8'
            '?token=deadbeefdeadbeefdeadbeefdeadbeefdeadbeefdeadbeefdeadbeefdeadbeef')
        self.assertEqual(out, 'https://[url redacted]')

    def test_trailing_sentence_punctuation_survives_outside_the_placeholder(self):
        out = redact_urls_in_text('sentence ending in a url http://foo.bar/baz.')
        self.assertEqual(out, 'sentence ending in a url http://[url redacted].')

    def test_multiple_urls_in_one_string_are_each_redacted(self):
        out = redact_urls_in_text('see https://a.test/1 and also rtsp://b.test/2')
        self.assertEqual(out, 'see https://[url redacted] and also rtsp://[url redacted]')

    def test_text_with_no_url_is_unchanged(self):
        self.assertEqual(redact_urls_in_text('no url here at all'), 'no url here at all')

    def test_empty_and_none_pass_through(self):
        self.assertEqual(redact_urls_in_text(''), '')
        self.assertIsNone(redact_urls_in_text(None))

    def test_result_is_a_fixed_point_a_second_pass_changes_nothing(self):
        """The strongest form of the invariant: if anything URL-shaped survived, running
        this again would change the output."""
        out = redact_urls_in_text('https://a.test/1 rtsp://b.test/2 plain text')
        self.assertEqual(redact_urls_in_text(out), out)

    def test_placeholder_constant_is_what_call_sites_can_rely_on(self):
        self.assertEqual(REDACTED_URL_TEXT, '[url redacted]')


if __name__ == '__main__':
    unittest.main()
