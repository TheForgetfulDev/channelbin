"""Tier 1 pure units for credential masking (app/url_utils.py::mask_creds).
Pure regex over a URL string - the guard that a stream URL rendered in the UI or logs
never leaks the Xtream username/password. Every recognized credential shape must be
masked; a URL with no credentials must pass through byte-identical.

Guards BUGS.md 2026-07-18 (IPTV credentials written to dvr.log in plaintext). The
implementation moved here from app/routes/recordings.py so non-route modules can import
it without depending on the routes layer; the Jinja `mask_creds` filter now delegates.
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.url_utils import mask_creds as _mask_creds_str  # noqa: E402
from app.url_utils import mask_account_urls_in_text, mask_url_path  # noqa: E402


class MaskCredsTests(unittest.TestCase):
    def test_xtream_live_path(self):
        out = _mask_creds_str('http://h:8080/live/joe/s3cret/123.ts')
        self.assertNotIn('joe', out)
        self.assertNotIn('s3cret', out)
        self.assertIn('123', out)

    def test_xtream_movie_path(self):
        self.assertEqual(_mask_creds_str('http://h/movie/joe/s3cret/9.mkv'),
                         'http://h/movie/***/***/9.mkv')

    def test_query_param_username_password(self):
        out = _mask_creds_str('http://h/x?username=joe&password=s3cret&id=1')
        self.assertNotIn('joe', out)
        self.assertNotIn('s3cret', out)
        self.assertIn('id=1', out)

    def test_query_param_case_insensitive(self):
        out = _mask_creds_str('http://h/x?Username=joe&PASSWORD=s3cret')
        self.assertNotIn('joe', out)
        self.assertNotIn('s3cret', out)

    def test_bare_user_pass_id_path(self):
        # scheme://host/<user>/<pass>/<id> with no /live/ anchor (CDN-redirected form)
        out = _mask_creds_str('http://cdn.example/joe/s3cret/456')
        self.assertNotIn('joe', out)
        self.assertNotIn('s3cret', out)
        self.assertIn('456', out)

    def test_userinfo_in_authority(self):
        # http://user:pass@host/... - the shape some M3U playlist URLs use
        out = _mask_creds_str('http://joe:s3cret@h/playlist.m3u')
        self.assertNotIn('joe', out)
        self.assertNotIn('s3cret', out)
        self.assertIn('playlist.m3u', out)

    def test_no_credentials_passes_through(self):
        url = 'http://h/stream/index.m3u8'
        self.assertEqual(_mask_creds_str(url), url)

    def test_longer_non_credential_path_not_eaten(self):
        # a 4-segment path is not the bare user/pass/id shape (exactly 3 segments)
        url = 'http://h/a/b/c/d/e.ts'
        self.assertEqual(_mask_creds_str(url), url)


class MaskUrlPathTests(unittest.TestCase):
    """Tier 1 pure units for mask_url_path (DESIGN-secrets.md §4.2).

    An Account's own m3u_url/epg_url/base_url is secret in full: for path-token providers
    the path IS the credential and mask_creds' shape heuristics leave it untouched.
    """

    def test_path_token_is_masked_where_mask_creds_leaves_it(self):
        url = 'https://example-provider.test/TESTPATHTOKEN1'
        self.assertEqual(_mask_creds_str(url), url)      # the gap this closes
        self.assertEqual(mask_url_path(url), 'https://example-provider.test/***')

    def test_query_string_is_dropped_entirely(self):
        out = mask_url_path('http://h/get.php?username=joe&password=s3cret&type=m3u')
        self.assertEqual(out, 'http://h/***')

    def test_host_and_port_preserved(self):
        self.assertEqual(mask_url_path('http://h.example:8080/a/b/c'), 'http://h.example:8080/***')

    def test_userinfo_masked_too(self):
        out = mask_url_path('http://joe:s3cret@h/token')
        self.assertNotIn('joe', out)
        self.assertNotIn('s3cret', out)
        self.assertEqual(out, 'http://***:***@h/***')

    def test_url_without_path_passes_through(self):
        self.assertEqual(mask_url_path('https://example-provider.test'), 'https://example-provider.test')
        self.assertEqual(mask_url_path('https://example-provider.test/'), 'https://example-provider.test/')

    def test_non_url_and_empty_pass_through(self):
        self.assertEqual(mask_url_path('not a url'), 'not a url')
        self.assertEqual(mask_url_path(''), '')
        self.assertIsNone(mask_url_path(None))

    def test_idempotent(self):
        once = mask_url_path('https://example-provider.test/TESTPATHTOKEN1')
        self.assertEqual(mask_url_path(once), once)


class MaskAccountUrlsInTextTests(unittest.TestCase):
    """Tier 1 pure units for mask_account_urls_in_text (DESIGN-secrets.md §4.2).

    requests exceptions stringify with the full fetched URL, and those strings are
    persisted to account.last_error / AccountSyncLog.error_message, rendered in the UI,
    and pushed off-box as alerts.
    """

    def test_account_url_in_prose_loses_its_path(self):
        url = 'https://example-provider.test/TESTPATHTOKEN1'
        out = mask_account_urls_in_text(f'HTTPError 404 for url: {url} (attempt 1)', url)
        self.assertNotIn('TESTPATHTOKEN1', out)
        self.assertIn('https://example-provider.test/***', out)
        self.assertIn('attempt 1', out)

    def test_longer_url_masked_before_its_own_prefix(self):
        base = 'https://example-provider.test/TESTPATHTOKEN1'
        epg = base + '/epg.xml'
        out = mask_account_urls_in_text(f'failed on {epg}', base, epg)
        self.assertNotIn('TESTPATHTOKEN1', out)
        self.assertNotIn('epg.xml', out)

    def test_other_urls_still_get_the_heuristics(self):
        out = mask_account_urls_in_text(
            'stream http://h/live/joe/s3cret/1.ts failed', 'https://example-provider.test/TOKEN')
        self.assertNotIn('joe', out)
        self.assertNotIn('s3cret', out)

    def test_no_urls_supplied_behaves_like_mask_creds_in_text(self):
        text = 'plain failure, no url here'
        self.assertEqual(mask_account_urls_in_text(text), text)

    def test_unreachable_host_names_the_path_token_without_a_scheme(self):
        """BUGS.md 2026-09-18: urllib3 names only the request target when the host is
        unreachable, so the full-URL substitution above never matches it."""
        url = 'https://example-provider.test/TESTPATHTOKEN1/get.php?type=m3u'
        out = mask_account_urls_in_text(
            "HTTPSConnectionPool(host='example-provider.test', port=443): Max retries "
            'exceeded with url: /TESTPATHTOKEN1/get.php?type=m3u (Caused by X)', url)
        self.assertNotIn('TESTPATHTOKEN1', out)
        self.assertIn('Max retries exceeded with url: /***', out)

    def test_a_url_with_no_path_masks_no_bare_slash(self):
        out = mask_account_urls_in_text('see /docs for help', 'https://example-provider.test/')
        self.assertEqual(out, 'see /docs for help')

    def test_empty_and_none_are_safe(self):
        self.assertEqual(mask_account_urls_in_text('', 'https://example-provider.test/T'), '')
        self.assertIsNone(mask_account_urls_in_text(None, 'https://example-provider.test/T'))
        # a None among the urls (epg_url/base_url are nullable) must not blow up
        self.assertEqual(mask_account_urls_in_text('x', None, None), 'x')


if __name__ == '__main__':
    unittest.main(verbosity=2)
