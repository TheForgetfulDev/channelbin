"""Tier 1 - stream-URL normalization modes (spec: changelog/258 "Spec").

Turns the old on/off toggle into a four-way choice of URL spelling. Two rules carry the
whole design and each has dedicated coverage here:

  Spec §2  Only URLs carrying a user/password/id triplet are rewritten. ~1% of real channels
        are third-party streams the provider aggregated in (Pluto TV, CloudFront, Icecast
        radio) with no triplet at all - there is nothing to rebuild them from, so they are
        returned untouched in every mode. The pre-dropdown normalizer had no such check and
        mangled 9 real channels by stripping a meaningful .m3u8/.ts.

  Spec §3  The origin and credentials the PROVIDER put in the URL are reproduced verbatim.
        They must never be rebuilt from Account.base_url/username/password: on two of the
        four real accounts ZERO stream URLs sit on the account's configured host, so
        substituting them would break every channel on those accounts.

Pure units - no Flask, no DB, no network.
  python3 -m unittest tests.test_url_normalization_modes
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.accounts import (  # noqa: E402
    NORM_DISABLED, NORM_HLS, NORM_MPEGTS, NORM_MPEGTS_LIVE,
    build_stream_url, coerce_normalization_mode, normalize_url,
    parse_stream_url_parts, resolve_normalization_mode, url_is_normalizable,
)


class FakeAccount:
    def __init__(self, url_normalization=None, base_url='http://ACCOUNT-HOST',
                 username='ACCTUSER', password='ACCTPASS'):
        self.url_normalization = url_normalization
        self.base_url = base_url
        self.username = username
        self.password = password


# The same stream, in each of the three spellings a provider might hand us.
THREE_FORMS = [
    'http://example.com/AAA/BBB/1',
    'http://example.com/live/AAA/BBB/1.ts',
    'http://example.com/live/AAA/BBB/1.m3u8',
]

EXPECTED = {
    NORM_MPEGTS:      'http://example.com/AAA/BBB/1',
    NORM_MPEGTS_LIVE: 'http://example.com/live/AAA/BBB/1.ts',
    NORM_HLS:         'http://example.com/live/AAA/BBB/1.m3u8',
}

# URLs with no user/pass/id triplet. Every one must survive untouched; the first four are
# among the 9 the old normalizer actually broke.
#
# These mirror real third-party streams a provider aggregated into its lineup. Every host
# is a reserved `.invalid` stand-in and every label and path token is invented. What is
# reproduced exactly is the SHAPE, which is the only thing these assert on: segment count,
# which segments are numeric, the non-standard ports, and the trailing extension. Keep that
# shape if you ever edit one - swapping in a `<user>/<pass>/<numeric id>` path makes the
# entry normalizable and silently turns the assertion vacuous.
#
# A reserved TLD proves nothing about the labels to its left. Three entries here once kept
# a real channel slug, a real CDN subdomain and a real path token while ending in
# `.invalid`, because the scrub replaced the domain and left everything else standing
# (dev/changelog/713). Invent every label; do not append a fake TLD to a real name.
NO_TRIPLET = [
    'https://epg-static.invalid/static/1.ts',
    'https://faithchan.streamhost.invalid/hls/faithchan/index.m3u8',
    # Three segments that LOOK like /user/pass/id but end non-numeric - the subtlest
    # near-miss in this list, and the one most likely to be broken by a careless edit.
    'http://radiolive.cdn-edge.invalid/strmRadio/userRadio/playlist.m3u8',
    'https://live-radiodemo.invalid/1000000001/index.m3u8',
    'https://edge-a.clusters.fake-stitcher.invalid/v1/stitch/embed/hls/channel/5db6/master.m3u8',
    'https://fake-cdn.invalid/v1/master/abcd1234/cc-aaa0/index.m3u8',
    # An Icecast radio mount: the trailing number is a mount point, NOT a stream id.
    # Rewriting this to /live/AAA/BBB/904.ts would destroy a working stream.
    'http://icecast.radio-902.invalid:8000/904',
    'http://live.radio-903.invalid:8010/903.mp3',
    # Junk the provider ships in its own feed - not our bug, but must not be mangled either.
    'http://line.example-cdn2.test/AAAA000000/BBBB000000/FAKECHANNEL.co',
    'https://fake-logos.invalid/someuser/Logos/blob/main/x.png?raw=true',
]


class ConversionMatrixTests(unittest.TestCase):
    """Every input spelling converts to every output spelling."""

    def test_every_form_converts_to_every_mode(self):
        for src in THREE_FORMS:
            parts = parse_stream_url_parts(src)
            self.assertIsNotNone(parts, f'{src} should be parseable')
            for mode, expected in EXPECTED.items():
                with self.subTest(src=src, mode=mode):
                    self.assertEqual(build_stream_url(parts, mode), expected)

    def test_conversion_is_idempotent(self):
        for mode, expected in EXPECTED.items():
            with self.subTest(mode=mode):
                once = build_stream_url(parse_stream_url_parts(expected), mode)
                twice = build_stream_url(parse_stream_url_parts(once), mode)
                self.assertEqual(once, expected)
                self.assertEqual(twice, expected)

    def test_port_is_preserved(self):
        parts = parse_stream_url_parts('http://host:8080/AAA/BBB/1.ts')
        self.assertEqual(build_stream_url(parts, NORM_MPEGTS), 'http://host:8080/AAA/BBB/1')

    def test_https_scheme_is_preserved(self):
        parts = parse_stream_url_parts('https://host/AAA/BBB/1')
        self.assertEqual(build_stream_url(parts, NORM_HLS), 'https://host/live/AAA/BBB/1.m3u8')


class ProviderCredentialsAreNeverSubstitutedTests(unittest.TestCase):
    """§3.2 - the rule that would silently break two of four real accounts if violated."""

    def test_provider_host_and_credentials_survive_normalization(self):
        # Account settings deliberately differ from everything in the URL.
        acct = FakeAccount(url_normalization=NORM_MPEGTS_LIVE,
                           base_url='http://ACCOUNT-HOST', username='ACCTUSER',
                           password='ACCTPASS')
        out = normalize_url('http://cdn.provider.net/PROVUSER/PROVPASS/42', acct)
        self.assertEqual(out, 'http://cdn.provider.net/live/PROVUSER/PROVPASS/42.ts')
        for leaked in ('ACCOUNT-HOST', 'ACCTUSER', 'ACCTPASS'):
            self.assertNotIn(leaked, out, 'account settings leaked into a provider URL')

    def test_cdn_fronted_account_keeps_the_cdn_host(self):
        """Mirrors the real account whose portal is one domain and whose streams are all
        served from a CDN on another - rebuilding from base_url would break every channel."""
        acct = FakeAccount(url_normalization=NORM_MPEGTS, base_url='http://portal.example')
        out = normalize_url('http://gold.cdn.example/U/P/604331.ts', acct)
        self.assertTrue(out.startswith('http://gold.cdn.example/'), out)

    def test_account_with_no_base_url_still_normalizes(self):
        """M3U accounts have no base_url at all; normalization must not depend on one."""
        acct = FakeAccount(url_normalization=NORM_MPEGTS, base_url=None)
        self.assertEqual(normalize_url('http://h/U/P/7.ts', acct), 'http://h/U/P/7')


class LeaveUntouchedTests(unittest.TestCase):
    """§3.1 - no triplet means no rewrite, in every mode."""

    def test_untouched_in_every_mode(self):
        for url in NO_TRIPLET:
            for mode in (NORM_MPEGTS, NORM_MPEGTS_LIVE, NORM_HLS):
                with self.subTest(url=url, mode=mode):
                    self.assertEqual(normalize_url(url, FakeAccount(mode)), url)

    def test_url_is_normalizable_reports_false(self):
        for url in NO_TRIPLET:
            with self.subTest(url=url):
                self.assertFalse(url_is_normalizable(url))

    def test_url_is_normalizable_reports_true_for_real_forms(self):
        for url in THREE_FORMS:
            with self.subTest(url=url):
                self.assertTrue(url_is_normalizable(url))

    def test_non_url_input_is_safe(self):
        for junk in ('', 'not a url', 'http', '/AAA/BBB/1'):
            with self.subTest(junk=junk):
                self.assertIsNone(parse_stream_url_parts(junk))
                self.assertEqual(normalize_url(junk, FakeAccount(NORM_MPEGTS)), junk)

    def test_non_numeric_id_is_not_treated_as_a_stream(self):
        self.assertIsNone(parse_stream_url_parts('http://h/AAA/BBB/CHANNEL.co'))


class DisabledModeTests(unittest.TestCase):
    def test_disabled_returns_input_unchanged(self):
        for url in THREE_FORMS + NO_TRIPLET:
            with self.subTest(url=url):
                self.assertEqual(normalize_url(url, FakeAccount(NORM_DISABLED)), url)

    def test_disabled_is_the_effective_default(self):
        """Ships off: never rewrite a user's URLs unless they ask."""
        self.assertEqual(resolve_normalization_mode(FakeAccount(None), {'sync': {}}),
                         NORM_DISABLED)


class ModeResolutionTests(unittest.TestCase):
    def test_account_overrides_global(self):
        cfg = {'sync': {'url_normalization': NORM_HLS}}
        self.assertEqual(resolve_normalization_mode(FakeAccount(NORM_MPEGTS), cfg), NORM_MPEGTS)

    def test_account_none_defers_to_global(self):
        cfg = {'sync': {'url_normalization': NORM_HLS}}
        self.assertEqual(resolve_normalization_mode(FakeAccount(None), cfg), NORM_HLS)

    def test_account_can_explicitly_disable_against_a_global_mode(self):
        cfg = {'sync': {'url_normalization': NORM_HLS}}
        self.assertEqual(resolve_normalization_mode(FakeAccount(NORM_DISABLED), cfg),
                         NORM_DISABLED)

    def test_unknown_mode_string_falls_back_rather_than_crashing(self):
        self.assertIsNone(coerce_normalization_mode('nonsense'))
        self.assertEqual(resolve_normalization_mode(FakeAccount('nonsense'), {'sync': {}}),
                         NORM_DISABLED)


class LegacyBooleanCoercionTests(unittest.TestCase):
    """A config or account row written before _m014 still holds a boolean. True meant the
    only behavior the old toggle had - the without-live form - so an existing install must
    keep exactly the URLs it already has."""

    def test_true_becomes_mpegts_without_live(self):
        self.assertEqual(coerce_normalization_mode(True), NORM_MPEGTS)

    def test_false_becomes_disabled(self):
        self.assertEqual(coerce_normalization_mode(False), NORM_DISABLED)

    def test_none_and_blank_mean_defer(self):
        self.assertIsNone(coerce_normalization_mode(None))
        self.assertIsNone(coerce_normalization_mode(''))

    def test_legacy_boolean_in_config_is_honored(self):
        self.assertEqual(resolve_normalization_mode(FakeAccount(None),
                                                    {'sync': {'url_normalization': True}}),
                         NORM_MPEGTS)

    def test_legacy_account_boolean_produces_the_old_output(self):
        self.assertEqual(normalize_url('http://h/live/AAA/BBB/1.ts', FakeAccount(True)),
                         'http://h/AAA/BBB/1')


class NoHiddenConfigReadTests(unittest.TestCase):
    """BUGS.md 2026-07-15 10:34 class: a passed-in cfg must prevent any disk read, so this
    stays O(1) in config parses no matter how many rows a caller loops over."""

    def test_passed_cfg_avoids_load_config(self):
        import app.accounts as accounts_mod
        original = accounts_mod.load_config
        calls = []
        accounts_mod.load_config = lambda *a, **k: calls.append(1) or {'sync': {}}
        try:
            resolve_normalization_mode(FakeAccount(None), {'sync': {'url_normalization': NORM_HLS}})
        finally:
            accounts_mod.load_config = original
        self.assertEqual(calls, [], 'read config from disk despite a passed cfg')


if __name__ == '__main__':
    unittest.main(verbosity=2)


class MigrationBackfillTests(unittest.TestCase):
    """_m014 converts the pre-dropdown boolean column in place.

    SQLite stores booleans as 1/0 and its dynamic typing lets the same column hold the new
    strings, so the migration is an UPDATE rather than a table rebuild. What matters is the
    mapping: True was the old "enabled", whose only behavior was the without-live form.
    """

    def _run_on(self, rows):
        import sqlite3
        from app.migrations import _m014_url_normalization_mode
        conn = sqlite3.connect(':memory:')
        cur = conn.cursor()
        cur.execute('CREATE TABLE accounts (id INTEGER PRIMARY KEY, url_normalization)')
        cur.executemany('INSERT INTO accounts (id, url_normalization) VALUES (?, ?)',
                        list(enumerate(rows, start=1)))
        _m014_url_normalization_mode(conn, cur)
        return [r[0] for r in cur.execute('SELECT url_normalization FROM accounts ORDER BY id')]

    def test_boolean_rows_map_to_modes(self):
        self.assertEqual(self._run_on([1, 0, None]), [NORM_MPEGTS, NORM_DISABLED, None])

    def test_existing_mode_strings_are_left_alone(self):
        """Idempotent: re-running must not rewrite rows that already hold a mode."""
        modes = [NORM_DISABLED, NORM_MPEGTS, NORM_MPEGTS_LIVE, NORM_HLS]
        self.assertEqual(self._run_on(modes), modes)

    def test_rerunning_is_stable(self):
        import sqlite3
        from app.migrations import _m014_url_normalization_mode
        conn = sqlite3.connect(':memory:')
        cur = conn.cursor()
        cur.execute('CREATE TABLE accounts (id INTEGER PRIMARY KEY, url_normalization)')
        cur.execute('INSERT INTO accounts (id, url_normalization) VALUES (1, 1)')
        _m014_url_normalization_mode(conn, cur)
        _m014_url_normalization_mode(conn, cur)
        self.assertEqual(cur.execute('SELECT url_normalization FROM accounts').fetchone()[0],
                         NORM_MPEGTS)
