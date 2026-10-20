"""Sync faults over real HTTP, channel side: logins, the auth answer, the playlist and the
catalog (DESIGN-simulator.md §7, dev/changelog/1218).

Each test adds an account against the simulator, injects one fault from sim/faults.py, runs
ChannelBin's unmodified `_do_sync` over loopback and checks what a user would see: the sync
log's status, error and counts, the open alerts by type and source key, what was imported or
left alone, and - from the simulator's ledger - what was asked. A test that guards a past
defect cites its dev/docs/BUGS.md entry; every other test is a characterization test of
today's behavior against a provider fault, and says so.

No network: the simulator listens on loopback and netguard allows in-process sockets there.
"""
import copy
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.database import CHANNEL_URL_CHANGED, ChannelEvent  # noqa: E402
from sim.faults import FaultRow  # noqa: E402
from sim.lineup import TRIPLET_KINDS  # noqa: E402
from tests.support.sim_sync import FAULT_WORLD, SimSyncCase, names_a_cut_chunk  # noqa: E402


def _stream_ids(view):
    return {row.channel.stream_id for row in view.rows}


def _triplet_rows(rows):
    """The rows whose URL carries <user>/<pass>/<id>: a radio mount or a third-party HLS
    feed has no id in its URL, and ChannelBin keys it on a hash of the URL instead."""
    return [r for r in rows if r.channel.kind in TRIPLET_KINDS]


class LoginFaultTests(SimSyncCase):
    def test_a_refused_login_fails_the_sync_after_one_request(self):
        """Characterization: auth 0 fails the sync at once and asks nothing more."""
        aid = self.add_xtream()
        self.sim.inject('auth_refused', self.login())
        log = self.sync(aid)
        self.assertEqual(log.status, 'ERROR')
        self.assertEqual(log.error_message, 'Authentication failed - check username and password')
        self.assertAlert('SYNC_FAILED', f'account:{aid}:sync-failed')
        self.assertAsked('auth')

    def test_an_expired_account_stops_the_sync_and_changes_nothing(self):
        """Guards dev/docs/BUGS.md 2026-10-17 (expired account): the sync stops at the auth
        answer naming the status and its expiry date, records them on the account, and leaves
        every channel and listing as the last sync left them - no shrink, classification or
        guide alert standing in for the real reason."""
        aid = self.add_xtream()
        self.assertEqual(self.sync(aid).status, 'SUCCESS')
        before = {(c.id, c.last_seen_at, c.raw_stream_url) for c in self.channels(aid)}
        entries = self.entry_count(aid)

        fault = self.sim.inject('account_status', self.login(), status='Expired')
        log = self.sync(aid)

        self.assertEqual(log.status, 'ERROR')
        self.assertIn('The provider says this account is Expired (expiry date ', log.error_message)
        self.assertIn('no channels or listings were changed', log.error_message)
        account = self.account(aid)
        self.assertEqual(account.status, 'ERROR')
        self.assertEqual(account.provider_status, 'Expired')
        self.assertLess(account.provider_exp_date, self.now)
        alert = self.assertAlert('SYNC_FAILED', f'account:{aid}:sync-failed')
        self.assertEqual(alert.body, log.error_message)
        for quiet in ('SYNC_FEED_SHRUNK', 'SYNC_LIVE_CLASSIFY_UNAVAILABLE',
                      'EPG_SOURCE_FETCH_FAILED'):
            self.assertNoAlert(quiet)
        self.assertEqual({(c.id, c.last_seen_at, c.raw_stream_url) for c in self.channels(aid)},
                         before)
        self.assertEqual(self.entry_count(aid), entries)
        self.assertAsked('auth')

        self.sim.clear(fault)
        self.assertEqual(self.sync(aid).status, 'SUCCESS')
        self.assertNoAlert('SYNC_FAILED')
        self.assertEqual(self.account(aid).provider_status, 'Active')

    def test_a_first_sync_of_an_expired_account_imports_nothing_and_fails(self):
        """Guards dev/docs/BUGS.md 2026-10-17 (expired account): a first sync used to finish
        SUCCESS with no channels."""
        aid = self.add_xtream()
        self.sim.inject('account_status', self.login(), status='Expired')
        log = self.sync(aid)
        self.assertEqual(log.status, 'ERROR')
        self.assertIn('Expired', log.error_message)
        self.assertEqual(self.channels(aid), [])
        self.assertEqual(self.account(aid).provider_status, 'Expired')

    def test_banned_and_disabled_logins_are_named_and_known_facts_kept(self):
        """Guards dev/docs/BUGS.md 2026-10-17 (expired account): a refusal that names a status
        says so instead of "check username and password", and overwrites only what it said -
        the connection count the last good sync recorded stays."""
        aid = self.add_xtream()
        self.assertEqual(self.sync(aid).status, 'SUCCESS')
        for status in ('Banned', 'Disabled'):
            with self.subTest(status=status):
                fault = self.sim.inject('account_status', self.login(), status=status)
                log = self.sync(aid)
                self.assertEqual(log.status, 'ERROR')
                self.assertIn(f'The provider refused the login and says this account is {status}.',
                              log.error_message)
                account = self.account(aid)
                self.assertEqual(account.provider_status, status)
                self.assertEqual(account.provider_max_connections, 1)
                self.assertAsked('auth')
                self.sim.clear(fault)

    def test_a_login_over_its_sync_allowance_is_refused_then_banned(self):
        """Characterization (the policy is assumed): each refused sync asks once and stops -
        ChannelBin does not retry against a refusal - and the ban that follows is named."""
        aid = self.add_xtream()
        self.sim.inject('rate_limit', self.login(), syncs=1, ban_after=2)
        self.assertEqual(self.sync(aid).status, 'SUCCESS')
        for _ in range(2):
            log = self.sync(aid)
            self.assertEqual(log.status, 'ERROR')
            self.assertIn('429 Client Error', log.error_message)
            self.assertAsked('auth')
        log = self.sync(aid)
        self.assertEqual(log.status, 'ERROR')
        self.assertIn('says this account is Banned', log.error_message)
        self.assertEqual(self.account(aid).provider_status, 'Banned')

    def test_the_configured_user_agent_is_sent_and_a_refusal_of_it_fails_the_sync(self):
        """Characterization: every request carries `http.user_agent`; a host that refuses it
        fails the sync with the status named."""
        self.t.sandbox_config({'http': {'user_agent': 'Lavf/60.16.100'}})
        aid = self.add_xtream()
        self.sim.inject('user_agent_refused', 'north-tv', match='Lavf')
        log = self.sync(aid)
        self.assertEqual(log.status, 'ERROR')
        self.assertIn('403 Client Error', log.error_message)
        self.assertEqual({r.user_agent for r in self.asked}, {'Lavf/60.16.100'})

        self.t.sandbox_config({'http': {'user_agent': 'VLC/3.0.18 LibVLC/3.0.18'}})
        self.assertEqual(self.sync(aid).status, 'SUCCESS')
        self.assertEqual({r.user_agent for r in self.asked}, {'VLC/3.0.18 LibVLC/3.0.18'})
        self.assertEqual(len(self.asked), 5)


class TrialAccountTests(SimSyncCase):
    world_source = copy.deepcopy(FAULT_WORLD)
    world_source['logins'][0]['is_trial'] = True

    def test_a_trial_account_is_recorded(self):
        """Characterization: the trial flag reaches the account."""
        aid = self.add_xtream()
        self.assertEqual(self.sync(aid).status, 'SUCCESS')
        self.assertIs(self.account(aid).provider_is_trial, True)


class AuthAnswerTests(SimSyncCase):
    def test_an_auth_answered_with_a_playlist_imports_it_unclassified(self):
        """Characterization: a panel answering player_api.php with M3U is accepted; the
        playlist still supplies every channel, and the missing catalog is reported."""
        aid = self.add_xtream()
        self.sim.inject('auth_as_playlist', 'north-tv')
        log = self.sync(aid)
        self.assertEqual(log.status, 'SUCCESS')
        self.assertEqual(log.channels_synced, len(self.sim.view('north-tv').rows))
        self.assertAlert('SYNC_LIVE_CLASSIFY_UNAVAILABLE', f'account:{aid}:live-classify')
        account = self.account(aid)
        self.assertIsNone(account.provider_status)
        self.assertEqual(account.constructed_stream_url_count, 0)

    def test_a_broken_auth_answer_fails_the_sync_naming_what_came_back(self):
        """Characterization: each way the auth answer breaks fails the sync at once, says what
        the answer was, and never shows the credentials."""
        aid = self.add_xtream()
        password = self.sim.account('north-tv').password
        for kind, said in (('empty_body', 'Server returned HTTP 200 with empty body'),
                           ('html_page', 'non-JSON, non-M3U response (HTTP 200)'),
                           ('server_error', '503 Server Error'),
                           ('nonstandard_status', 'Server returned HTTP 884 with empty body')):
            with self.subTest(kind=kind):
                fault = self.sim.inject(kind, 'north-tv', endpoints=['auth'])
                log = self.sync(aid)
                self.assertEqual(log.status, 'ERROR')
                self.assertIn(said, log.error_message)
                self.assertNotIn(password, log.error_message)
                self.assertAlert('SYNC_FAILED', f'account:{aid}:sync-failed')
                self.assertAsked('auth')
                self.sim.clear(fault)


class PlaylistFaultTests(SimSyncCase):
    def expected_urls(self, panel):
        p = self.world.panels[panel]
        login = self.world.logins[self.login(panel)]
        return {self.sim.provider.stream_url(p, row, login) for row in self.sim.view(panel).rows}

    def test_a_playlist_down_falls_back_to_the_catalog_on_the_declared_stream_origin(self):
        """Characterization of dev/changelog/1100 and 261: get.php answering 884 with no body
        leaves the catalog as the channel list, every URL built on server_info's origin."""
        aid = self.add_xtream('north-tv', normalization='mpegts')
        self.sim.inject('playlist_down', 'north-tv')
        log = self.sync(aid)
        self.assertEqual(log.status, 'SUCCESS')
        origin = self.sim.url(self.world.backends['north'].stream_host_names()[0])
        view = self.sim.account('north-tv')
        channels = self.channels(aid)
        self.assertEqual(len(channels), len(self.sim.view('north-tv').rows))
        self.assertEqual(self.account(aid).constructed_stream_url_count, len(channels))
        self.assertEqual(self.account(aid).provider_stream_origin, origin)
        for c in channels:
            self.assertEqual(c.raw_stream_url,
                             f'{origin}/{view.username}/{view.password}/{c.stream_id}')
        self.assertEqual([r.status for r in self.asked if r.endpoint == 'playlist'], [884])

    def test_a_playlist_down_without_server_info_builds_on_the_base_url(self):
        """Characterization (DESIGN-live-vod.md §4.3): no declared stream origin leaves the
        account's own base URL as the guess."""
        aid = self.add_xtream('north-plus', normalization='mpegts')
        self.sim.inject('playlist_down', 'north-plus')
        self.assertEqual(self.sync(aid).status, 'SUCCESS')
        base = self.sim.account('north-plus').base_url
        self.assertIsNone(self.account(aid).provider_stream_origin)
        self.assertTrue(self.channels(aid))
        self.assertTrue(all(c.raw_stream_url.startswith(base + '/') for c in self.channels(aid)))

    def test_a_playlist_down_with_no_url_format_imports_nothing_and_says_why(self):
        """Characterization: with URL Normalization Disabled there is no form to build a URL
        in, so the sync stops and the standing alert names the setting."""
        aid = self.add_xtream('north-tv')
        self.sim.inject('playlist_down', 'north-tv')
        self.assertEqual(self.sync(aid).status, 'ERROR')
        self.assertEqual(self.channels(aid), [])
        self.assertAlert('SYNC_URL_CONSTRUCTION_BLOCKED', f'account:{aid}:url-construction-blocked')

    def test_a_comment_above_the_header_reads_as_a_playlist(self):
        """Guards dev/docs/BUGS.md 2026-08-09 12:11 PM (the M3U playlist) and 2026-10-17
        (the Xtream get.php, which still refused it and built every URL itself)."""
        for panel, add in (('vale-m3u', self.add_m3u), ('north-tv', self.add_xtream)):
            with self.subTest(panel=panel):
                aid = add(panel)
                self.sim.inject('playlist_preamble', panel)
                log = self.sync(aid)
                self.assertEqual(log.status, 'SUCCESS')
                self.assertEqual({c.raw_stream_url for c in self.channels(aid)},
                                 self.expected_urls(panel))
                self.assertEqual(self.account(aid).constructed_stream_url_count or 0, 0)

    def test_a_byte_order_mark_reads_as_a_playlist(self):
        """Guards dev/docs/BUGS.md 2026-08-09 12:11 PM (the M3U playlist) and 2026-10-17
        (the Xtream get.php)."""
        for panel, add in (('vale-m3u', self.add_m3u), ('north-tv', self.add_xtream)):
            with self.subTest(panel=panel):
                aid = add(panel)
                self.sim.inject('playlist_bom', panel)
                self.assertEqual(self.sync(aid).status, 'SUCCESS')
                self.assertEqual({c.raw_stream_url for c in self.channels(aid)},
                                 self.expected_urls(panel))
                self.assertEqual(self.account(aid).constructed_stream_url_count or 0, 0)

    def test_a_header_beyond_the_scan_limit_and_a_web_page_are_refused(self):
        """Guards dev/docs/BUGS.md 2026-08-09 12:11 PM: the scan stays bounded, so an HTML page
        in place of a playlist still fails the sync rather than importing nothing quietly."""
        aid = self.add_m3u()
        for kind, settings in (('playlist_preamble', {'lines': 11}),
                               ('html_page', {'endpoints': ['playlist']})):
            with self.subTest(kind=kind):
                fault = self.sim.inject(kind, 'vale-m3u', **settings)
                log = self.sync(aid)
                self.assertEqual(log.status, 'ERROR')
                self.assertIn('Expected M3U playlist', log.error_message)
                self.assertEqual(self.channels(aid), [])
                self.sim.clear(fault)

    def test_placeholder_rows_are_skipped_and_counted_as_malformed(self):
        """Guards dev/docs/BUGS.md 2026-07-14 06:16 PM (rows whose URL line is `http` are not
        imported) and 2026-10-17 (without CUID they were counted as one malformed URL and
        the rest as duplicate stream ids)."""
        for panel, add in (('north-plus', self.add_xtream), ('vale-m3u', self.add_m3u)):
            with self.subTest(panel=panel):
                aid = add(panel)
                self.sim.inject('placeholder_rows', panel)
                view = self.sim.view(panel)
                placeholders = [r for r in view.rows if isinstance(r, FaultRow)]
                log = self.sync(aid)
                self.assertEqual(log.status, 'SUCCESS')
                self.assertEqual(log.skipped_malformed_urls, len(placeholders))
                self.assertEqual(log.skipped_duplicate_stream_ids, 0)
                self.assertEqual(log.channels_synced, len(view.rows) - len(placeholders))
                self.assertTrue(all('://' in c.raw_stream_url for c in self.channels(aid)))

    def test_mixed_url_spellings_on_the_api_host_import_as_spelled_and_vod_stays_out(self):
        """Characterization of DESIGN-live-vod.md §2's mixed playlist: bare and /live/ URLs in
        one playlist, on the API host itself, with VOD rows - every live row imported under
        its own spelling, no VOD row imported."""
        aid = self.add_xtream('north-plus')
        self.assertEqual(self.sync(aid).status, 'SUCCESS')
        urls = {c.raw_stream_url for c in self.channels(aid)}
        self.assertEqual(urls, self.expected_urls('north-plus'))
        base = self.sim.account('north-plus').base_url
        panel = self.world.panels['north-plus']
        login = self.world.logins[self.login('north-plus')]
        on_api = {self.sim.provider.stream_url(panel, r, login)
                  for r in _triplet_rows(self.sim.view('north-plus').rows)}
        self.assertTrue(any('/live/' in u for u in on_api))
        self.assertTrue(any('/live/' not in u for u in on_api))
        self.assertTrue(all(u.startswith(base + '/') for u in on_api))
        self.assertFalse(any('/movie/' in u or '/series/' in u for u in urls))
        self.assertTrue(self.world.panel_lineups['north-plus'].vod)

    def test_cuid_classifies_when_the_url_tail_is_another_number(self):
        """Characterization of DESIGN-live-vod.md §3.1: the CUID attribute, not the URL tail,
        is the stream id, so every channel stays live and keeps the catalog's id."""
        aid = self.add_xtream()
        fault = self.sim.inject('cuid_url_mismatch', 'north-tv')
        offset = fault.params['offset']
        log = self.sync(aid)
        self.assertEqual(log.status, 'SUCCESS')
        self.assertNoAlert('SYNC_LIVE_CLASSIFY_REFUSED')
        self.assertNoAlert('SYNC_LIVE_CLASSIFY_UNAVAILABLE')
        channels = self.channels(aid)
        rows = self.sim.view('north-tv').rows
        self.assertEqual({c.stream_id for c in channels}, _stream_ids(self.sim.view('north-tv')))
        with_triplet = [c for c in channels if c.provider_stream_id is not None]
        self.assertEqual(len(with_triplet), len(_triplet_rows(rows)))
        for c in with_triplet:
            self.assertEqual(c.provider_stream_id, str(c.stream_id + offset))

    def test_repeated_stream_ids_are_counted_and_the_first_kept(self):
        """Guards dev/docs/BUGS.md 2026-08-30 10:23 AM: a stream id listed twice is counted on
        the sync log, imported once, and keeps its first listing's category."""
        aid = self.add_xtream()
        rows = self.world.panel_lineups['north-tv'].rows
        plain = {r.channel.stream_id: r.category_name for r in _triplet_rows(rows)}
        fault = self.sim.inject('duplicate_ids', 'north-tv')
        log = self.sync(aid)
        self.assertEqual(log.status, 'SUCCESS')
        self.assertEqual(log.skipped_duplicate_stream_ids, fault.params['count'])
        channels = self.channels(aid)
        self.assertEqual(len(channels), len(rows))
        self.assertEqual({c.stream_id: c.category_name for c in channels
                          if c.stream_id in plain}, plain)

    def test_a_broken_playlist_on_an_xtream_panel_falls_back_to_the_catalog(self):
        """Characterization: whatever breaks get.php - an empty body, a web page, a 5xx, a
        chunked body cut short - the catalog becomes the channel list and every URL is
        built, which the account's constructed-URL count says."""
        aid = self.add_xtream('north-tv', normalization='mpegts')
        rows = len(self.sim.view('north-tv').rows)
        for kind in ('empty_body', 'html_page', 'server_error', 'truncated_chunked'):
            with self.subTest(kind=kind):
                fault = self.sim.inject(kind, 'north-tv', endpoints=['playlist'])
                log = self.sync(aid)
                self.assertEqual(log.status, 'SUCCESS')
                self.assertEqual(log.channels_synced, rows)
                self.assertEqual(self.account(aid).constructed_stream_url_count, rows)
                self.sim.clear(fault)

    def test_a_cut_m3u_playlist_fails_the_sync_and_imports_nothing(self):
        """Characterization: an M3U account has no catalog to fall back on."""
        aid = self.add_m3u()
        self.sim.inject('truncated_chunked', 'vale-m3u', endpoints=['playlist'])
        log = self.sync(aid)
        self.assertEqual(log.status, 'ERROR')
        self.assertTrue(names_a_cut_chunk(log.error_message), log.error_message)
        self.assertEqual(self.channels(aid), [])

    def test_a_slow_playlist_inside_the_timeout_is_read_in_full(self):
        """Characterization: a drip that never pauses as long as the read timeout is read to
        the end, so nothing falls back to the catalog."""
        aid = self.add_xtream()
        self.sim.inject('slow_drip', 'north-tv', endpoints=['playlist'], chunk_bytes=4096,
                        gap_seconds=0.02)
        log = self.sync(aid)
        self.assertEqual(log.status, 'SUCCESS')
        self.assertEqual(self.account(aid).constructed_stream_url_count, 0)
        self.assertEqual(log.channels_synced, len(self.sim.view('north-tv').rows))

    def test_an_unreadable_catalog_imports_the_playlist_and_says_so(self):
        """Characterization (DESIGN-live-vod.md §4): with no catalog to classify by, the
        playlist is imported unfiltered and the standing alert says so."""
        aid = self.add_xtream()
        self.sim.inject('nonstandard_status', 'north-tv', endpoints=['catalog'])
        log = self.sync(aid)
        self.assertEqual(log.status, 'SUCCESS')
        self.assertEqual(log.channels_synced, len(self.sim.view('north-tv').rows))
        self.assertAlert('SYNC_LIVE_CLASSIFY_UNAVAILABLE', f'account:{aid}:live-classify')

    def test_gzip_on_the_wire_changes_nothing(self):
        """Characterization: every answer compressed with Content-Encoding is read exactly as
        the plain one."""
        aid = self.add_xtream()
        plain = self.sync(aid)
        urls = {c.raw_stream_url for c in self.channels(aid)}
        self.sim.inject('gzip_encoding', 'north-tv')
        log = self.sync(aid)
        self.assertEqual(log.status, 'SUCCESS')
        self.assertTrue(all('gzip_encoding' in r.faults for r in self.asked))
        self.assertEqual({c.raw_stream_url for c in self.channels(aid)}, urls)
        self.assertEqual(log.epg_entries_synced, plain.epg_entries_synced)


class LineupChangeTests(SimSyncCase):
    def test_channels_gone_new_and_renamed_are_counted_and_none_deleted(self):
        """Characterization of DESIGN-sync-resilience.md §3 and §5: the sync log counts what
        went and came, renames land, nothing is deleted, and a shrink past half the lineup
        raises the standing alert that the next full feed clears."""
        aid = self.add_xtream()
        self.assertEqual(self.sync(aid).status, 'SUCCESS')
        before = {c.stream_id: c.name for c in self.channels(aid)}
        fault = self.sim.inject('lineup_change', 'north-tv', drop=0.7, add=5, rename=0.2)
        view = self.sim.view('north-tv')
        gone = set(before) - _stream_ids(view)
        renamed = {r.channel.stream_id: r.name for r in view.rows
                   if r.channel.stream_id in before and r.name != before[r.channel.stream_id]}
        self.assertGreaterEqual(len(gone), len(before) / 2)
        self.assertTrue(renamed)

        log = self.sync(aid)
        self.assertEqual(log.status, 'SUCCESS')
        self.assertEqual(log.channels_removed, len(gone))
        self.assertEqual(log.channels_added, 5)
        self.assertEqual(log.channels_synced, len(view.rows))
        names = {c.stream_id: c.name for c in self.channels(aid)}
        self.assertEqual(len(names), len(before) + 5)
        for sid, name in renamed.items():
            self.assertEqual(names[sid], name)
        self.assertAlert('SYNC_FEED_SHRUNK', f'account:{aid}:feed-shrunk')

        self.sim.clear(fault)
        self.assertEqual(self.sync(aid).status, 'SUCCESS')
        self.assertNoAlert('SYNC_FEED_SHRUNK')

    def test_every_stream_url_moving_is_reported_once_and_per_channel(self):
        """Characterization of DESIGN-url-drift.md: a new host or new credentials in every
        URL rewrites the channels in place, raises one account alert and one event per
        channel, and adds or removes nothing."""
        for to in ('host', 'credentials'):
            with self.subTest(to=to):
                aid = self.add_xtream('north-tv')
                self.assertEqual(self.sync(aid).status, 'SUCCESS')
                before = {c.id: c.raw_stream_url for c in self.channels(aid)}
                fault = self.sim.inject('url_drift', 'north-tv', to=to)
                log = self.sync(aid)
                self.assertEqual(log.status, 'SUCCESS')
                self.assertEqual((log.channels_added, log.channels_removed), (0, 0))
                after = {c.id: c.raw_stream_url for c in self.channels(aid)}
                moved = [cid for cid in before if before[cid] != after[cid]]
                self.assertEqual(set(after), set(before))
                self.assertGreaterEqual(len(moved), 50)
                self.assertAlert('PROVIDER_URLS_CHANGED', f'account:{aid}:url-drift')
                events = ChannelEvent.query.filter(ChannelEvent.channel_id.in_(moved),
                                                   ChannelEvent.event_type == CHANNEL_URL_CHANGED)
                self.assertEqual(events.count(), len(moved))
                if to == 'credentials':
                    self.assertFalse(any(self.sim.account('north-tv').password in after[c]
                                         for c in moved))
                self.sim.clear(fault)

    def test_renamed_categories_are_named_new_and_gone(self):
        """Characterization of dev/changelog/1211."""
        aid = self.add_xtream()
        self.assertEqual(self.sync(aid).status, 'SUCCESS')
        old = dict(self.world.panel_lineups['north-tv'].categories)
        self.sim.inject('category_change', 'north-tv', rename=2)
        new = dict(self.sim.view('north-tv', endpoint='categories').categories)
        changed = [cid for cid in old if old[cid] != new[cid]]
        self.assertEqual(len(changed), 2)

        changes = self.sync(aid).category_changes()
        self.assertEqual(sorted(c['name'] for c in changes['added']),
                         sorted(new[cid] for cid in changed))
        self.assertEqual(sorted(c['name'] for c in changes['removed']),
                         sorted(old[cid] for cid in changed))


if __name__ == '__main__':
    unittest.main()
