"""A playlist whose #EXTM3U is preceded by comment lines is still a playlist
(dev/docs/BUGS.md 2026-08-09, dev/changelog/523).

The M3U fetch required the response to literally start with '#EXTM3U', so a
provider that opens with a branding or licence comment failed the whole sync with zero
channels. Found against a real free provider: m3upt.com's playlist begins
'# M3UPT.com - IPTV playlist ... Public and official streams only.' before its #EXTM3U
line, and every other player reads it fine.

The guard still has a job, and these tests pin that it kept it: an HTML error page or a
login redirect served in place of a playlist has no #EXTM3U near the top and is still
refused. A tolerance that accepted anything would be worse than the bug.
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.accounts import _looks_like_m3u, _M3U_HEADER_SCAN_LINES  # noqa: E402

BODY = '#EXTINF:-1 tvg-id="ch1",Channel 1\nhttp://provider.test/1.ts\n'


class M3uHeaderToleranceTests(unittest.TestCase):

    def test_plain_header_still_accepted(self):
        self.assertTrue(_looks_like_m3u('#EXTM3U\n' + BODY))

    def test_header_with_attributes_accepted(self):
        self.assertTrue(_looks_like_m3u('#EXTM3U url-tvg="http://e.test/g.xml"\n' + BODY))

    def test_leading_comment_line_accepted(self):
        """The real m3upt.com shape."""
        text = ('# M3UPT.com - IPTV playlist in M3U format. Public and official streams only.\n'
                '\n'
                '#EXTM3U url-tvg="https://e.test/epg.xml.gz"\n' + BODY)
        self.assertTrue(_looks_like_m3u(text))

    def test_several_leading_comments_and_blanks_accepted(self):
        text = '\n'.join(['# line one', '', '# line two', '', '#EXTM3U']) + '\n' + BODY
        self.assertTrue(_looks_like_m3u(text))

    def test_html_error_page_still_refused(self):
        self.assertFalse(_looks_like_m3u(
            '<!DOCTYPE html>\n<html>\n<head><title>403 Forbidden</title></head>\n'
            '<body>Access denied</body>\n</html>'))

    def test_login_redirect_page_still_refused(self):
        self.assertFalse(_looks_like_m3u(
            '<html><body>Please <a href="/login">sign in</a> to continue</body></html>'))

    def test_empty_response_refused(self):
        self.assertFalse(_looks_like_m3u(''))

    def test_header_buried_past_the_scan_limit_refused(self):
        """Bounded on purpose - a document that mentions #EXTM3U hundreds of lines down is
        not a playlist, and an unbounded scan would make the guard meaningless."""
        text = '\n'.join(f'# filler {n}' for n in range(_M3U_HEADER_SCAN_LINES + 5))
        self.assertFalse(_looks_like_m3u(text + '\n#EXTM3U\n' + BODY))


if __name__ == '__main__':
    unittest.main()
