"""Tier 1 - VOD leak past the live-only filter (BUGS.md 2026-07-22 "VOD /series/ imported as channels").

`_parse_m3u_as_streams` used to apply its VOD filter (`skip anything without /live/`) *only*
to stream URLs whose host matched the Xtream portal's own host. That host check compared
`urlparse().netloc` (which includes the port) and was skipped for any host mismatch - whether a
genuinely different domain (CDN/multi-host) OR merely the same host with a differing port
(Account 3: `base_url` host vs. that host `:80`). Whenever it was skipped, the
else-branch included everything, and on-demand `/movie/` and `/series/` entries leaked into the
live channel list as fake channels (confirmed live: 6 series episodes).

The host comparison has since been deleted outright (DESIGN-live-vod.md §3.2) and the
authoritative live-vs-VOD decision moved to the provider's own catalog - see
`tests/test_live_vod_classification.py`. What remains here is the parser's conservative,
negative-only VOD-path exclusion, which is still the fallback whenever no catalog is
available (a plain M3U account, or an Xtream provider whose catalog can't be read), so these
guards still matter:
  * `/movie/` and `/series/` URLs are skipped regardless of host;
  * genuine live streams with no `/live/` segment (rootless `/user/pass/id` style) are kept -
    the common case for real-world providers, and the reason no positive live-only rule exists.

Pure-function test - no app, no DB, no network.
  python3 -m unittest tests.test_vod_filter
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.accounts import _parse_m3u_as_streams


def _m3u(*entries):
    lines = ['#EXTM3U']
    for name, url in entries:
        lines.append(f'#EXTINF:-1 tvg-name="{name}",{name}')
        lines.append(url)
    return '\n'.join(lines)


class VodFilterCdnHostTests(unittest.TestCase):
    PORTAL = 'http://portal.example.net:8080'
    CDN = 'http://edge.cdn.example.org'  # different host than the portal

    def _names(self, streams):
        return {s['name'] for s in streams}

    def test_cdn_series_and_movie_are_skipped(self):
        """VOD on a CDN host (host != portal) must be dropped, not imported."""
        m3u = _m3u(
            ('Live Rootless', f'{self.CDN}/user/pass/12345'),
            ('VOD Series Ep', f'{self.CDN}/series/user/pass/2012190.mkv'),
            ('VOD Movie', f'{self.CDN}/movie/user/pass/999.mkv'),
        )
        streams = _parse_m3u_as_streams(m3u)
        self.assertEqual(self._names(streams), {'Live Rootless'})

    def test_cdn_live_rootless_is_kept(self):
        """A CDN-fronted live stream with no /live/ segment is still a real channel."""
        m3u = _m3u(('Live Rootless', f'{self.CDN}/aae/477/55555'))
        streams = _parse_m3u_as_streams(m3u)
        self.assertEqual(len(streams), 1)
        self.assertEqual(streams[0]['name'], 'Live Rootless')

    def test_same_host_vod_path_still_skipped(self):
        """Same-host: /live/ kept, /movie/ skipped. Host is now irrelevant to the decision -
        what matters is that a VOD path is excluded and a live one is not."""
        m3u = _m3u(
            ('Same Host Live', f'{self.PORTAL}/live/user/pass/42.ts'),
            ('Same Host Movie', f'{self.PORTAL}/movie/user/pass/42.mkv'),
        )
        streams = _parse_m3u_as_streams(m3u)
        self.assertEqual(self._names(streams), {'Same Host Live'})


if __name__ == '__main__':
    unittest.main()
