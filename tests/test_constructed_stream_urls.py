"""Tier 2 - stream URLs the app builds itself (DESIGN-live-vod.md §4.3).

When a provider's playlist endpoint is unavailable, only its Xtream catalog can be read -
and the catalog carries no URLs. ChannelBin then builds each URL from the account's own
settings. This used to hardcode `<base>/live/<user>/<pass>/<id>.ts`, which is both the form
most often blocked by Cloudflare and a silent override of whatever URL Normalization mode
the user picked.

Guards asserted here:
  * construction renders the account's SELECTED mode, all three of them;
  * with normalization Disabled there is no format to build in, so the sync stops loudly
    (StreamUrlConstructionBlocked -> ERROR + SYNC_URL_CONSTRUCTION_BLOCKED) and imports
    nothing, rather than guessing a form and filling the account with dead channels;
  * that alert auto-resolves once a mode is set and the sync succeeds;
  * a provider-SUPPLIED URL is never rebuilt from account credentials (§7.5) - the account's
    host/user/pass are legal only when constructing from scratch;
  * _upsert_channels resolves the mode ONCE, not per row (CLAUDE.md "no hidden I/O in
    per-row loops").

Runs against a throwaway temp SQLite DB - never the live dvr.db.
  python3 -m unittest tests.test_constructed_stream_urls
"""
import json
import os
import sys
import threading
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import db  # noqa: E402
from app.accounts import (  # noqa: E402
    NORM_DISABLED, NORM_HLS, NORM_MPEGTS, NORM_MPEGTS_LIVE,
    StreamUrlConstructionBlocked, _do_sync, _upsert_channels, construct_stream_url,
)
from app.config import _deep_merge, load_config  # noqa: E402
from app.database import Alert, Channel, XtreamAccount  # noqa: E402
from tests.support import make_test_app  # noqa: E402

BASE_URL = 'http://panel.provider.test:8080'
USERNAME = 'acctuser'
PASSWORD = 'acctpass'


def _catalog_entry(sid):
    """A live-catalog row exactly as the JSON API returns it: no _stream_url, no
    direct_source. This is the account-4 shape - every URL must be constructed."""
    return {
        'stream_id': sid,
        'name': f'Channel {sid}',
        'category_id': '1',
        'epg_channel_id': f'ch{sid}.test',
    }


class ConstructedUrlFormTests(unittest.TestCase):
    """The form construction renders is the user's chosen mode, not a hardcoded one."""

    def setUp(self):
        self.t = make_test_app()

    def tearDown(self):
        self.t.cleanup()

    def _account(self, mode):
        acct = XtreamAccount(name=f'Acct {mode}', base_url=BASE_URL, username=USERNAME,
                             password=PASSWORD, status='OK', url_normalization=mode)
        db.session.add(acct)
        db.session.flush()
        return acct

    def test_each_mode_builds_its_own_form(self):
        cases = {
            NORM_MPEGTS: f'{BASE_URL}/{USERNAME}/{PASSWORD}/77',
            NORM_MPEGTS_LIVE: f'{BASE_URL}/live/{USERNAME}/{PASSWORD}/77.ts',
            NORM_HLS: f'{BASE_URL}/live/{USERNAME}/{PASSWORD}/77.m3u8',
        }
        for mode, expected in cases.items():
            with self.subTest(mode=mode):
                self.assertEqual(construct_stream_url(self._account(mode), 77, mode), expected)

    def test_trailing_slash_on_base_url_does_not_double(self):
        """A base_url typed with a trailing slash is a normal user input, not a bug to
        propagate into every URL on the account."""
        acct = self._account(NORM_MPEGTS)
        acct.base_url = BASE_URL + '/'
        self.assertEqual(construct_stream_url(acct, 5, NORM_MPEGTS),
                         f'{BASE_URL}/{USERNAME}/{PASSWORD}/5')

    def test_disabled_mode_refuses_to_guess(self):
        acct = self._account(NORM_DISABLED)
        with self.assertRaises(StreamUrlConstructionBlocked):
            construct_stream_url(acct, 5, NORM_DISABLED)

    def test_upsert_renders_the_accounts_mode(self):
        """End to end through the upsert loop, not just the helper."""
        acct = self._account(NORM_MPEGTS)
        _upsert_channels(acct, [_catalog_entry(1)], cfg=load_config())
        db.session.flush()

        ch = Channel.query.filter_by(account_id=acct.id).one()
        self.assertEqual(ch.raw_stream_url, f'{BASE_URL}/{USERNAME}/{PASSWORD}/1')
        # stream_url is the normalized view of raw_stream_url; construction already emitted
        # the chosen form, so normalizing it again must be a no-op rather than a second rewrite.
        self.assertEqual(ch.stream_url, ch.raw_stream_url)


class ProviderSuppliedUrlsAreNotRebuiltTests(unittest.TestCase):
    """§7.5: account credentials are legal ONLY when constructing from scratch.

    Measured on real provider accounts: two of four serve every stream from a host that is
    not the account's configured base_url, so substituting account settings into a
    provider-supplied URL would break every channel on them."""

    def setUp(self):
        self.t = make_test_app()
        self.account = XtreamAccount(
            name='CDN Acct', base_url=BASE_URL, username=USERNAME, password=PASSWORD,
            status='OK', url_normalization=NORM_MPEGTS)
        db.session.add(self.account)
        db.session.flush()

    def tearDown(self):
        self.t.cleanup()

    def test_supplied_url_keeps_provider_host_and_credentials(self):
        entry = _catalog_entry(9)
        entry['_stream_url'] = 'http://cdn.elsewhere.test/PROVUSER/PROVPASS/9'
        _upsert_channels(self.account, [entry], cfg=load_config())
        db.session.flush()

        ch = Channel.query.filter_by(account_id=self.account.id).one()
        for own in (BASE_URL, USERNAME, PASSWORD):
            self.assertNotIn(own, ch.stream_url)
        self.assertIn('cdn.elsewhere.test', ch.stream_url)
        self.assertIn('PROVUSER', ch.stream_url)
        self.assertIn('PROVPASS', ch.stream_url)

    def test_supplied_url_is_reshaped_but_not_relocated(self):
        """Normalization may change the SPELLING of a supplied URL; it must not change
        where the URL points or who it authenticates as."""
        entry = _catalog_entry(9)
        entry['_stream_url'] = 'http://cdn.elsewhere.test/live/PROVUSER/PROVPASS/9.ts'
        _upsert_channels(self.account, [entry], cfg=load_config())
        db.session.flush()

        ch = Channel.query.filter_by(account_id=self.account.id).one()
        self.assertEqual(ch.stream_url, 'http://cdn.elsewhere.test/PROVUSER/PROVPASS/9')


class ModeResolvedOncePerUpsertTests(unittest.TestCase):
    """CLAUDE.md "no hidden I/O in per-row loops".

    resolve_normalization_mode() falls through to load_config() whenever an account defers
    to the global default - which every account does by default - so resolving it inside the
    row loop meant one config lookup per channel, tens of thousands per real sync."""

    def setUp(self):
        self.t = make_test_app()
        # url_normalization=None is the point: this is the branch that reads global config.
        self.account = XtreamAccount(name='Defers', base_url=BASE_URL, username=USERNAME,
                                     password=PASSWORD, status='OK', url_normalization=None)
        db.session.add(self.account)
        db.session.flush()

    def tearDown(self):
        self.t.cleanup()

    def _load_config_calls_for(self, n_rows):
        cfg = _deep_merge(load_config(), {'sync': {'url_normalization': NORM_MPEGTS}})
        calls = []
        with mock.patch('app.accounts.load_config',
                        side_effect=lambda *a, **k: (calls.append(1), cfg)[1]):
            _upsert_channels(self.account, [_catalog_entry(i) for i in range(1, n_rows + 1)])
            db.session.flush()
        return len(calls)

    def test_config_lookups_do_not_scale_with_channel_count(self):
        few = self._load_config_calls_for(3)
        many = self._load_config_calls_for(40)
        self.assertEqual(few, many,
                         f'config lookups scale with row count ({few} at 3 rows vs '
                         f'{many} at 40) - the mode is being resolved inside the loop')
        self.assertLessEqual(many, 1)


class StreamOriginFromServerInfoTests(unittest.TestCase):
    """`server_info` says where the provider's STREAMS live - a different endpoint from the
    account's base_url, which is where the API/playlist/EPG are fetched from.

    Measured on real dumps: account 2's API is https while all 12,696 of its stream URLs are
    plain http, and account 3's declared origin matches its actual stream origin exactly,
    `:80` included, where base_url states no port at all. Pure function, no app needed."""

    def _origin(self, **server_info):
        from app.accounts import stream_origin_from_server_info
        return stream_origin_from_server_info({'server_info': server_info})

    def test_protocol_and_port_come_from_the_provider(self):
        self.assertEqual(
            self._origin(url='s.test', port='80', https_port='443', server_protocol='http'),
            'http://s.test:80')

    def test_https_uses_the_https_port_not_the_plain_one(self):
        """Reading `port` while the protocol is https would emit https-on-80, which is not
        a service that exists."""
        self.assertEqual(
            self._origin(url='s.test', port='80', https_port='8443', server_protocol='https'),
            'https://s.test:8443')

    def test_non_standard_port_is_preserved(self):
        self.assertEqual(self._origin(url='s.test', port='9000', server_protocol='http'),
                         'http://s.test:9000')

    def test_missing_pieces_degrade_sensibly(self):
        self.assertEqual(self._origin(url='s.test'), 'http://s.test')
        self.assertEqual(self._origin(url='http://s.test', port='80'), 'http://s.test:80')
        self.assertEqual(self._origin(url='s.test:8080', port='80'), 'http://s.test:8080')

    def test_nothing_usable_returns_none_so_the_caller_can_fall_back(self):
        """One real dump carries no server_info key at all, and panels have been seen
        echoing junk - neither may produce a URL that nothing can open."""
        from app.accounts import stream_origin_from_server_info
        self.assertIsNone(self._origin(url='', port='80'))
        self.assertIsNone(self._origin(url='s.test', server_protocol='rtmp'))
        self.assertIsNone(stream_origin_from_server_info({'user_info': {}}))
        self.assertIsNone(stream_origin_from_server_info(None))


class ConstructionUsesDeclaredStreamOriginTests(unittest.TestCase):
    """Construction builds on where the provider says its streams are, not on base_url.

    base_url is the API endpoint. Using it for streams is a guess - correct on account 4
    today, but account 2 proves the two can diverge completely (its API is on one domain,
    its streams on a CDN)."""

    def setUp(self):
        self.t = make_test_app()
        self.account = XtreamAccount(name='Split Endpoints', base_url=BASE_URL,
                                     username=USERNAME, password=PASSWORD, status='OK',
                                     url_normalization=NORM_MPEGTS)
        db.session.add(self.account)
        db.session.flush()

    def tearDown(self):
        self.t.cleanup()

    def test_declared_origin_wins_over_base_url(self):
        url = construct_stream_url(self.account, 7, NORM_MPEGTS,
                                   origin='https://streams.cdn.test:8443')
        self.assertEqual(url, f'https://streams.cdn.test:8443/{USERNAME}/{PASSWORD}/7')
        self.assertNotIn('panel.provider.test', url)

    def test_base_url_is_the_fallback_when_nothing_is_declared(self):
        self.assertEqual(construct_stream_url(self.account, 7, NORM_MPEGTS, origin=None),
                         f'{BASE_URL}/{USERNAME}/{PASSWORD}/7')

    def test_upsert_builds_every_url_on_the_declared_origin(self):
        _upsert_channels(self.account, [_catalog_entry(1), _catalog_entry(2)],
                         cfg=load_config(), stream_origin='http://streams.cdn.test:9000')
        db.session.flush()

        urls = [c.stream_url for c in Channel.query.filter_by(account_id=self.account.id)]
        self.assertEqual(len(urls), 2)
        for url in urls:
            self.assertTrue(url.startswith('http://streams.cdn.test:9000/'), url)


class NormalizationPreservesSchemeAndPortTests(unittest.TestCase):
    """Normalization rewrites the PATH only. Scheme and port are part of where a stream
    lives, so changing either would repoint the URL at a different service - and a
    constructed URL may legitimately carry https or a non-standard port from the provider's
    declared origin. Pure function, no app needed."""

    def test_scheme_and_port_survive_every_mode(self):
        from app.accounts import normalize_url_with_mode
        for url in ('https://secure.test/live/U/P/1.ts',
                    'http://host.test:8443/live/U/P/1.ts',
                    'https://host.test:9443/U/P/1',
                    'http://host.test:80/live/U/P/1.ts'):
            scheme, _, rest = url.partition('://')
            authority = rest.split('/', 1)[0]
            for mode in (NORM_MPEGTS, NORM_MPEGTS_LIVE, NORM_HLS):
                with self.subTest(url=url, mode=mode):
                    out = normalize_url_with_mode(url, mode)
                    self.assertTrue(out.startswith(f'{scheme}://{authority}/'),
                                    f'{url} -> {out} changed scheme or authority')


class LegacyBooleanCoercionTests(unittest.TestCase):
    """A stored value must mean the same thing before and after a DB round-trip.

    url_normalization became a String column when the on/off toggle turned into a four-way
    mode. SQLite is dynamically typed, so a Python False written to it comes back as 0 -
    and mapping only the bools made `False` read as "disabled" in memory but "defer to the
    global default" once re-read, which is a different mode entirely. Pure function, no app
    needed."""

    def test_legacy_falsey_spellings_all_mean_disabled(self):
        from app.accounts import coerce_normalization_mode
        for value in (False, 0, '0', 'false', 'False'):
            with self.subTest(value=value):
                self.assertEqual(coerce_normalization_mode(value), NORM_DISABLED)

    def test_legacy_truthy_spellings_all_mean_mpegts(self):
        from app.accounts import coerce_normalization_mode
        for value in (True, 1, '1', 'true', 'True'):
            with self.subTest(value=value):
                self.assertEqual(coerce_normalization_mode(value), NORM_MPEGTS)

    def test_unset_stays_unset_and_garbage_is_rejected(self):
        """None/'' must stay None - that is "defer to the global default", NOT disabled -
        and an unrecognized string must not be passed through as if it were a mode."""
        from app.accounts import coerce_normalization_mode
        for value in (None, ''):
            with self.subTest(value=value):
                self.assertIsNone(coerce_normalization_mode(value))
        self.assertIsNone(coerce_normalization_mode('mpeg_ts_maybe'))


class BlockedSyncTests(unittest.TestCase):
    """The whole-sync behavior when there is no format to build URLs in."""

    def setUp(self):
        self.t = make_test_app()
        self.dump_base = os.path.join(self.t._tmpdir, 'xtream_dumps')
        self.account = XtreamAccount(
            name='No Format', base_url=BASE_URL, username=USERNAME, password=PASSWORD,
            status='OK', url_normalization=NORM_DISABLED)
        db.session.add(self.account)
        db.session.commit()
        self._write_dump([1, 2, 3])

    def tearDown(self):
        self.t.cleanup()

    def _write_dump(self, stream_ids):
        """A catalog-only dump - no get.php playlist, exactly account 4's situation."""
        d = os.path.join(self.dump_base, str(self.account.id), '2026-01-01_01')
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, 'auth.json'), 'w', encoding='utf-8') as f:
            json.dump({'user_info': {'auth': 1, 'status': 'Active'}}, f)
        with open(os.path.join(d, 'live_streams_json_api.json'), 'w', encoding='utf-8') as f:
            json.dump([_catalog_entry(sid) for sid in stream_ids], f)

    def _sync(self):
        cfg = _deep_merge(load_config(), {'debug': {'xtream_dump_dir': self.dump_base}})
        with mock.patch('app.accounts.load_config', return_value=cfg):
            _do_sync(self.account.id, threading.Event(), use_dump=True)
        db.session.expire_all()

    def _blocked_alerts(self):
        return Alert.query.filter_by(alert_type='SYNC_URL_CONSTRUCTION_BLOCKED',
                                     dismissed_at=None).all()

    def test_nothing_is_imported_and_the_sync_errors(self):
        self._sync()

        self.assertEqual(Channel.query.filter_by(account_id=self.account.id).count(), 0)
        self.assertEqual(self.account.status, 'ERROR')

    def test_the_failure_names_the_reason_and_the_remedy(self):
        """A failure path that sets ERROR must say why (CLAUDE.md defect-class rules) -
        a blank error here would look like an unreachable provider."""
        self._sync()

        alerts = self._blocked_alerts()
        self.assertEqual(len(alerts), 1)
        self.assertIn('URL Normalization', alerts[0].body)
        self.assertIn('MPEG-TS without live', alerts[0].body)
        self.assertIn('URL Normalization', self.account.last_error)

    def test_alert_resolves_once_a_mode_is_set(self):
        self._sync()
        self.assertEqual(len(self._blocked_alerts()), 1)

        self.account.url_normalization = NORM_MPEGTS
        db.session.commit()
        self._sync()

        self.assertEqual(self._blocked_alerts(), [])
        self.assertEqual(self.account.status, 'OK')
        self.assertEqual(Channel.query.filter_by(account_id=self.account.id).count(), 3)

    def test_the_account_page_names_the_mode_actually_used(self):
        """Prose describing the constructed URL form is part of the change surface: the form
        is a user setting, and a constructed URL that does not play is nearly always the
        wrong form. This lived in the SYNC_STREAM_URLS_CONSTRUCTED alert body until
        dev/changelog/928 retired it and moved it onto the account page's standing banner -
        which is better placed, since it is true for as long as the URLs are constructed."""
        self.account.url_normalization = NORM_HLS
        db.session.commit()
        self._sync()

        self.assertEqual(
            [], Alert.query.filter_by(alert_type='SYNC_STREAM_URLS_CONSTRUCTED').all(),
            'constructed URLs are shown on the account, never raised as an alert')

        html = self.t.client.get(f'/accounts/{self.account.id}').get_data(as_text=True)
        self.assertIn('stream URLs were built by ChannelBin', html)
        self.assertIn('HLS', html)


if __name__ == '__main__':
    unittest.main()
