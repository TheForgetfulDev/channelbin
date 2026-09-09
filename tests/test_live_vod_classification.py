"""Tier 1 - live-vs-VOD classification (changelog/256, DESIGN-live-vod.md).

Guards the replacement of the URL-shape heuristics with the provider's own
`get_live_streams` catalog as the authority. The defect being generalized away is BUGS.md
2026-07-22 ("VOD /series/ imported as channels"), whose first fix was a symptom patch.

Two committed fixtures under `tests/fixtures/` carry the two real provider shapes this was
designed against, with every host and credential scrubbed to `.invalid` placeholders
(regenerate via `tests/fixtures/make_xtream_fixtures.py`):
  * `rootless_portal` - rootless `/user/pass/<id>` URLs, no CUID, VOD present in the
    playlist but absent from the catalog, plus CDN-fronted entries reachable only via
    `direct_source`.
  * `mixed_cuid` - the decisive case: `/live/…ts` and rootless URLs in ONE playlist, CUID
    attributes present, and URL numeric tails that do NOT equal the catalog's stream_ids.

`mixed_cuid` is why match precedence is CUID-first: on the real account it came from,
matching the URL tail first keeps 157 of 13,106 channels. Any reordering must fail here.

Pure-function tests - no app, no DB, no network.
  python3 -m unittest tests.test_live_vod_classification
"""
import json
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.accounts import NORM_MPEGTS_LIVE, _parse_m3u_as_streams
from app.xtream_client import (
    CLASSIFY_APPLIED, CLASSIFY_REFUSED, CLASSIFY_UNAVAILABLE,
    _live_keys_from_json, _tag_api_sourced, classify_live_streams,
)

FIXTURES = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'fixtures')


def _m3u(*entries):
    """entries: (name, url) or (name, url, cuid)."""
    lines = ['#EXTM3U']
    for e in entries:
        name, url = e[0], e[1]
        cuid = f' CUID="{e[2]}"' if len(e) > 2 else ''
        lines.append(f'#EXTINF:-1{cuid} tvg-name="{name}",{name}')
        lines.append(url)
    return '\n'.join(lines)


def _catalog(*rows):
    """rows: stream_id, or (stream_id, direct_source)."""
    out = []
    for r in rows:
        if isinstance(r, tuple):
            out.append({'stream_id': r[0], 'direct_source': r[1]})
        else:
            out.append({'stream_id': r, 'direct_source': ''})
    return out


def _load_fixture(name):
    d = os.path.join(FIXTURES, name)
    with open(os.path.join(d, 'live_streams_m3u.m3u'), encoding='utf-8') as f:
        entries = _parse_m3u_as_streams(f.read())
    with open(os.path.join(d, 'live_streams_json_api.json'), encoding='utf-8') as f:
        catalog = json.load(f)
    return entries, catalog


class CatalogAuthorityTests(unittest.TestCase):
    """The provider's catalog decides, not the URL shape."""

    def test_vod_without_a_vod_path_is_dropped(self):
        """The core new capability: VOD on a rootless URL carries no /movie/ or /series/
        marker, so ONLY the catalog can exclude it. The old architecture imported it."""
        entries = _parse_m3u_as_streams(_m3u(
            ('Real Channel', 'http://p.invalid/u/p/111'),
            ('Some Movie (2024)', 'http://p.invalid/u/p/222'),
        ))
        kept, outcome, _ = classify_live_streams(entries, _live_keys_from_json(_catalog(111)), 1, 20)
        self.assertEqual(outcome, CLASSIFY_APPLIED)
        self.assertEqual([e['name'] for e in kept], ['Real Channel'])

    def test_cuid_takes_precedence_over_url_tail(self):
        """The load-bearing precedence. The CUID is in the catalog, the URL tail is not;
        matching the tail first would drop a real channel (and did, 12,949 of them)."""
        entries = _parse_m3u_as_streams(_m3u(('NHL Network', 'http://cdn.invalid/u/p/604331', '157761')))
        self.assertEqual(entries[0]['_id_source'], 'cuid')
        kept, outcome, _ = classify_live_streams(entries, _live_keys_from_json(_catalog(157761)), 1, 20)
        self.assertEqual(outcome, CLASSIFY_APPLIED)
        self.assertEqual(len(kept), 1)

    def test_url_tail_id_matches_when_no_cuid(self):
        entries = _parse_m3u_as_streams(_m3u(('Rootless', 'http://p.invalid/u/p/978715')))
        self.assertEqual(entries[0]['_id_source'], 'url')
        kept, _, _ = classify_live_streams(entries, _live_keys_from_json(_catalog(978715)), 1, 20)
        self.assertEqual(len(kept), 1)

    def test_direct_source_keeps_cdn_entries_with_no_derivable_id(self):
        """CDN-fronted live channels (1,176 on one real account) have no id in the URL;
        exact direct_source equality is the only key that reaches them."""
        url = 'https://edge.invalid/v1/master/abc/index.m3u8'
        entries = _parse_m3u_as_streams(_m3u(
            ('Genuine', 'http://p.invalid/u/p/111'),
            ('Pluto Sitcoms', url),
        ))
        self.assertEqual(entries[1]['_id_source'], 'hash')
        kept, outcome, _stats = classify_live_streams(
            entries, _live_keys_from_json(_catalog(111, (5, url))), 2, 20)
        # Asserting the outcome matters: without it, losing direct_source matching would
        # drop this entry, trip the zero-kept refusal, and hand back an unfiltered list
        # that still satisfies a bare length check.
        self.assertEqual(outcome, CLASSIFY_APPLIED)
        self.assertEqual([e['name'] for e in kept], ['Genuine', 'Pluto Sitcoms'])

    def test_synthesized_hash_id_is_never_a_match_key(self):
        """A hash id that happens to equal a real catalog stream_id must not admit the row.
        Paired with a genuine channel so the drop is visible as a drop, rather than as the
        zero-kept refusal that would otherwise mask it."""
        url = 'https://edge.invalid/v1/master/abc/index.m3u8'
        hashed = _parse_m3u_as_streams(_m3u(('Hashed', url)))[0]
        collided = hashed['stream_id']

        entries = _parse_m3u_as_streams(_m3u(
            ('Genuine', 'http://p.invalid/u/p/111'),
            ('Hashed', url),
        ))
        # The catalog lists the colliding id, but carries no matching direct_source.
        kept, outcome, _stats = classify_live_streams(
            entries, _live_keys_from_json(_catalog(111, collided)), 1, 20)
        self.assertEqual(outcome, CLASSIFY_APPLIED)
        self.assertEqual([e['name'] for e in kept], ['Genuine'])

    def test_api_sourced_entries_bypass_classification(self):
        """Rows that came FROM the catalog are live by construction. Without this, a sync
        that fell back to the JSON API would filter the catalog against itself and drop
        every channel."""
        rows = _tag_api_sourced([{'stream_id': 1, 'name': 'A'}, {'stream_id': 2, 'name': 'B'}])
        kept, outcome, _ = classify_live_streams(rows, _live_keys_from_json(_catalog(999)), 2, 20)
        self.assertEqual(outcome, CLASSIFY_APPLIED)
        self.assertEqual(len(kept), 2)


class DegradationTests(unittest.TestCase):
    """Every failure mode imports unfiltered and reports - never wipes, never silent."""

    def _entries(self, n=10):
        return _parse_m3u_as_streams(_m3u(
            *[(f'Ch{i}', f'http://p.invalid/u/p/{i}') for i in range(n)]))

    def test_unavailable_catalog_imports_unfiltered(self):
        entries = self._entries()
        kept, outcome, stats = classify_live_streams(entries, None, 10, 20)
        self.assertEqual(outcome, CLASSIFY_UNAVAILABLE)
        self.assertEqual(len(kept), 10)
        self.assertEqual(stats['dropped'], 0)

    def test_collapse_below_threshold_is_refused_and_unfiltered(self):
        """A truncated catalog must not decimate a working channel list."""
        entries = self._entries()
        kept, outcome, stats = classify_live_streams(
            entries, _live_keys_from_json(_catalog(0)), 100, 20)
        self.assertEqual(outcome, CLASSIFY_REFUSED)
        self.assertEqual(len(kept), 10)
        self.assertEqual(stats['refused_kept'], 1)

    def test_zero_kept_is_refused_even_with_no_baseline(self):
        """First-ever sync has no baseline for the percentage guard to be a fraction of,
        so keeping nothing out of a non-empty playlist is refused unconditionally."""
        entries = self._entries()
        kept, outcome, _ = classify_live_streams(
            entries, _live_keys_from_json(_catalog(999999)), 0, 20)
        self.assertEqual(outcome, CLASSIFY_REFUSED)
        self.assertEqual(len(kept), 10)

    def test_threshold_zero_disables_the_percentage_guard(self):
        entries = self._entries()
        kept, outcome, _ = classify_live_streams(
            entries, _live_keys_from_json(_catalog(1)), 100, 0)
        self.assertEqual(outcome, CLASSIFY_APPLIED)
        self.assertEqual(len(kept), 1)

    def test_first_sync_is_exempt_from_the_percentage_guard(self):
        """Baseline 0 means no prior count to compare against; a legitimate first sync
        that keeps a small share of a VOD-heavy playlist must not be refused."""
        entries = self._entries()
        kept, outcome, _ = classify_live_streams(
            entries, _live_keys_from_json(_catalog(1)), 0, 20)
        self.assertEqual(outcome, CLASSIFY_APPLIED)
        self.assertEqual(len(kept), 1)

    def test_empty_playlist_is_not_refused(self):
        kept, outcome, _ = classify_live_streams([], _live_keys_from_json(_catalog(1)), 10, 20)
        self.assertEqual(outcome, CLASSIFY_APPLIED)
        self.assertEqual(kept, [])


class RealShapeFixtureTests(unittest.TestCase):
    """The two real provider shapes, scrubbed. These are the regression that matters."""

    def test_rootless_portal_excludes_vod_and_keeps_every_live_channel(self):
        entries, catalog = _load_fixture('rootless_portal')
        kept, outcome, stats = classify_live_streams(
            entries, _live_keys_from_json(catalog), 20, 20)
        self.assertEqual(outcome, CLASSIFY_APPLIED)
        self.assertEqual(stats['dropped'], 0, 'no live channel may be dropped')
        self.assertEqual(len(kept), len(entries))
        # The 6 VOD entries never survive parsing, so they cannot reach the catalog check.
        self.assertNotIn('/series/', '\n'.join(e['_stream_url'] for e in kept))

    def test_mixed_cuid_feed_loses_nothing(self):
        """The mixed /live/ + rootless feed is where a URL-shape rule would wipe half the
        account. Nothing may be dropped."""
        entries, catalog = _load_fixture('mixed_cuid')
        kept, outcome, stats = classify_live_streams(
            entries, _live_keys_from_json(catalog), 30, 20)
        self.assertEqual(outcome, CLASSIFY_APPLIED)
        self.assertEqual(stats['dropped'], 0)
        shapes = {'/live/' in e['_stream_url'] for e in kept}
        self.assertEqual(shapes, {True, False}, 'fixture must retain BOTH URL shapes')

    def test_mixed_cuid_would_collapse_without_cuid_precedence(self):
        """Proves the precedence is load-bearing rather than cosmetic: matching on the URL
        tail alone (what a careless reordering would do) drops most of the account."""
        entries, catalog = _load_fixture('mixed_cuid')
        live_ids, _ = _live_keys_from_json(catalog)
        tail_only = [e for e in entries if str(e['stream_id']) in live_ids
                     and e['_id_source'] != 'cuid']
        self.assertLess(len(tail_only), len(entries) // 2,
                        'fixture no longer exercises the CUID-vs-tail mismatch')

    # "The committed fixtures still contain no real provider host" was asserted here by a
    # loop over the real hostnames, which made this guard against leaking a leak itself.
    # The maintained term list and its scanner own that check now, across the whole tree
    # rather than these two directories, and keep the terms out of shipped code entirely
    # (dev/changelog/708).


class ConstructedUrlReportingTests(unittest.TestCase):
    """DESIGN-live-vod.md §4.3 - found by the account-4 blind test.

    A provider whose playlist endpoint is down but whose catalog is fine yields entries with
    no URL at all, so every stream URL gets constructed from account credentials. That used
    to finish as an entirely ordinary SUCCESS with no alert. The count driving the alert is
    derived from the same `_stream_url` field `_upsert_channels` branches on, so these
    assert on that field rather than on how the entries were obtained.
    """

    def _constructed(self, entries):
        # Mirrors _fetch_and_classify_xtream_streams' own count.
        return sum(1 for s in entries if not s.get('_stream_url'))

    def test_catalog_only_entries_have_no_urls(self):
        """The account-4 shape: JSON catalog rows carry neither _stream_url nor
        direct_source, so 100% of URLs are constructed."""
        rows = _tag_api_sourced([
            {'stream_id': 1, 'name': 'A', 'direct_source': ''},
            {'stream_id': 2, 'name': 'B', 'direct_source': ''},
        ])
        self.assertEqual(self._constructed(rows), 2)

    def test_playlist_entries_carry_real_urls(self):
        """The healthy path must report zero, or the alert would cry wolf every sync."""
        entries = _parse_m3u_as_streams(_m3u(('A', 'http://p.invalid/u/p/1')))
        self.assertEqual(self._constructed(entries), 0)

    def test_mixed_sources_count_only_the_url_less_ones(self):
        entries = _parse_m3u_as_streams(_m3u(('Real', 'http://p.invalid/u/p/1')))
        entries += _tag_api_sourced([{'stream_id': 2, 'name': 'NoUrl'}])
        self.assertEqual(self._constructed(entries), 1)

    def test_catalog_only_entries_still_classify_as_applied(self):
        """The constructed-URL problem must not be conflated with a classification
        failure - the catalog was read fine, it just had no URLs in it."""
        rows = _tag_api_sourced([{'stream_id': 1}, {'stream_id': 2}])
        _kept, outcome, _stats = classify_live_streams(
            rows, _live_keys_from_json(_catalog(1, 2)), 0, 20)
        self.assertEqual(outcome, CLASSIFY_APPLIED)


class CatalogOnlyProviderTests(unittest.TestCase):
    """End-to-end through the real `_fetch_and_classify_xtream_streams` + `_upsert_channels`
    pair - the same two calls `_do_sync` makes, against the committed `catalog_only` fixture
    (the exact account-4 shape: playlist endpoint returns HTTP 884, catalog fine, no
    direct_source on any row).

    This goes through production code rather than re-deriving the count, so it actually
    guards the reported number instead of restating it.
    """

    def setUp(self):
        from tests.support.app import make_test_app
        self.t = make_test_app()
        self.ctx = self.t.app.app_context()
        self.ctx.push()

    def tearDown(self):
        self.ctx.pop()
        self.t.cleanup()

    def _run(self):
        from app import db
        from app.database import Account
        from app.accounts import _upsert_channels
        from app.xtream_client import FileXtreamClient, _fetch_and_classify_xtream_streams
        # A mode must be pinned: this fixture supplies no URLs, so all 30 are constructed,
        # and construction renders the account's URL Normalization mode. Disabled (which is
        # what an unset mode falls through to) is a hard stop on this path - there would be
        # no form to build them in (DESIGN-live-vod.md §4.3).
        acct = Account(name='catalog-only', account_type='xtream',
                       base_url='http://portal.invalid', username='u', password='p',
                       url_normalization=NORM_MPEGTS_LIVE)
        db.session.add(acct)
        db.session.commit()
        client = FileXtreamClient(os.path.join(FIXTURES, 'catalog_only'),
                                  acct.base_url, acct.username, acct.password)
        streams, outcome, constructed = _fetch_and_classify_xtream_streams(
            acct, client, {'sync': {}})
        result = (*_upsert_channels(acct, streams, {'sync': {}}), outcome, constructed)
        db.session.commit()
        return acct, result

    def test_every_url_is_reported_as_constructed(self):
        from app.database import Channel
        acct, (synced, _malformed, _dup, _drift, _new, outcome, constructed) = self._run()
        self.assertEqual(outcome, CLASSIFY_APPLIED)
        self.assertEqual(synced, 30)
        self.assertEqual(constructed, 30,
                         'a provider supplying no URLs must report every one as constructed')
        self.assertEqual(Channel.query.filter_by(account_id=acct.id).count(), 30)

    def test_constructed_urls_use_the_documented_xtream_form(self):
        """The constructed shape is the account's SELECTED mode, not an invented or hardcoded
        one. This account is pinned to MPEG-TS-with-live, a form one real provider's own
        playlist serves verbatim (3,390 entries on account 2), so it is a legitimate shape -
        but it is used here because it was chosen, not because it is baked in."""
        from app.database import Channel
        acct, _ = self._run()
        ch = Channel.query.filter_by(account_id=acct.id).first()
        self.assertRegex(ch.raw_stream_url, r'^http://portal\.invalid/live/u/p/\d+\.ts$')

    def test_healthy_playlist_provider_reports_zero_constructed(self):
        """Control: the alert must not fire for a provider that supplies its own URLs, or it
        would be standing permanently and carry no information."""
        from app import db
        from app.database import Account
        from app.accounts import _upsert_channels
        from app.xtream_client import FileXtreamClient, _fetch_and_classify_xtream_streams
        acct = Account(name='playlist-ok', account_type='xtream',
                       base_url='http://portal.invalid', username='u', password='p')
        db.session.add(acct)
        db.session.commit()
        client = FileXtreamClient(os.path.join(FIXTURES, 'mixed_cuid'),
                                  acct.base_url, acct.username, acct.password)
        streams, outcome, constructed = _fetch_and_classify_xtream_streams(
            acct, client, {'sync': {}})
        _upsert_channels(acct, streams, {'sync': {}})
        self.assertEqual(outcome, CLASSIFY_APPLIED)
        self.assertEqual(constructed, 0)


class ProviderRequestCountTests(unittest.TestCase):
    """One sync must never ask the provider the same question twice.

    Every real provider seen so far reports `max_connections: 1`, so a redundant round trip
    is not just waste - it is another chance to be refused, and account 4's playlist refusal
    (HTTP 884) happened partway through exactly such a burst. Before the per-client catalog
    cache, a failed playlist made `get_live_streams()` and `get_live_stream_ids()` issue
    back-to-back identical `action=get_live_streams` calls.

    These drive a fake `_fetch`, so nothing touches the network (netguard would block it).
    """

    def _client(self, playlist_ok, catalog):
        from app.xtream_client import XtreamClient

        class Resp:
            def __init__(self, text, js=None):
                self.text = text
                self.content = text.encode()
                self.status_code = 200
                self._js = js

            def json(self):
                if self._js is None:
                    raise ValueError('not json')
                return self._js

        c = XtreamClient.__new__(XtreamClient)
        c.base_url = 'http://p.invalid'
        c.username, c.password, c.timeout = 'u', 'p', 1
        c._base_params = {'username': 'u', 'password': 'p'}
        c._headers = {}
        c._catalog_cache = None
        c._catalog_attempted = False
        c._catalog_m3u_text = None
        calls = []

        def fake_fetch(url, params):
            calls.append('get.php' if 'get.php' in url
                         else f"player_api:{params.get('action')}")
            if 'get.php' in url:
                if not playlist_ok:
                    raise ValueError('HTTP 884 with empty body')
                return Resp('#EXTM3U\n#EXTINF:-1 tvg-name="A",A\nhttp://p.invalid/u/p/1\n')
            if catalog == 'json':
                return Resp('[]', [{'stream_id': 1, 'direct_source': ''}])
            if catalog == 'm3u':
                return Resp('#EXTM3U\n#EXTINF:-1 tvg-name="B",B\nhttp://p.invalid/u/p/2\n')
            raise ValueError('catalog endpoint down')

        c._fetch = fake_fetch
        return c, calls

    def _run(self, playlist_ok, catalog):
        c, calls = self._client(playlist_ok, catalog)
        c.get_live_streams()
        c.get_live_stream_ids()
        return calls

    def test_healthy_provider_makes_two_requests(self):
        calls = self._run(True, 'json')
        self.assertEqual(calls, ['get.php', 'player_api:get_live_streams'])

    def test_refused_playlist_does_not_double_ask_the_catalog(self):
        """The account-4 shape. This is the regression: it used to be three calls with the
        last two identical."""
        calls = self._run(False, 'json')
        self.assertEqual(len(calls), 2)
        self.assertEqual(len(calls), len(set(calls)), 'no request may be repeated')

    def test_failed_catalog_is_not_retried(self):
        """A provider that just refused the catalog must not be immediately re-asked -
        caching only successes would leave this path knocking twice."""
        calls = self._run(False, 'down')
        self.assertEqual(calls.count('player_api:get_live_streams'), 1)

    def test_m3u_answering_catalog_is_not_retried(self):
        calls = self._run(False, 'm3u')
        self.assertEqual(calls.count('player_api:get_live_streams'), 1)

    def test_repeated_calls_never_refetch(self):
        c, calls = self._client(True, 'json')
        for _ in range(4):
            c.get_live_stream_ids()
        self.assertEqual(calls.count('player_api:get_live_streams'), 1)


class ParserContractTests(unittest.TestCase):
    """No positive live-only rule, and _id_source is recorded for the classifier."""

    def test_no_positive_live_rule_rootless_and_hls_are_kept(self):
        entries = _parse_m3u_as_streams(_m3u(
            ('Rootless', 'http://p.invalid/u/p/1'),
            ('HLS', 'http://p.invalid/u/p/2.m3u8'),
            ('Live TS', 'http://p.invalid/live/u/p/3.ts'),
        ))
        self.assertEqual(len(entries), 3)

    def test_id_source_is_recorded(self):
        entries = _parse_m3u_as_streams(_m3u(
            ('C', 'http://p.invalid/u/p/1', '900'),
            ('U', 'http://p.invalid/u/p/2'),
            ('H', 'http://p.invalid/master/index'),
        ))
        self.assertEqual([e['_id_source'] for e in entries], ['cuid', 'url', 'hash'])


if __name__ == '__main__':
    unittest.main()
