"""Sync faults over real HTTP, guide side: the XMLTV a sync fetches, and the logo cache
(DESIGN-simulator.md §7, dev/changelog/1218).

As in test_sim_sync_faults_channels.py, each test runs ChannelBin's unmodified `_do_sync`
against the simulator with one fault applied and checks what a user would see. For a guide
fault that is mostly two things at once: the sync finishes PARTIAL naming the cause, with
the source's alert under its own key, and the listings already held are kept wherever the
cause allows it. A test that guards a past defect cites its dev/docs/BUGS.md entry; every
other test is a characterization test, and says so.
"""
import copy
import json
import os
import re
import sys
import unittest
from collections import defaultdict
from datetime import datetime, timedelta
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import accounts, db, sports_match  # noqa: E402
from app.config import load_config  # noqa: E402
from app.database import (SPORTS_MATCH_KIND_CHANNEL_NAME, Channel, EPGEntry,  # noqa: E402
                          EpgAlternateEntry, EpgSource, EpgSourceChannel, SportsEvent, SportsLeague,
                          SportsMatch, SportsTeam, XtreamAccount)
from app.epg_sources import add_url_source  # noqa: E402
from app.logo_cache import _fetch_one_logo  # noqa: E402
from tests.support.sim_sync import FAULT_WORLD, SimSyncCase, names_a_cut_chunk  # noqa: E402


class _GuideCase(SimSyncCase):
    def synced(self, aid):
        """A healthy sync, so the source holds listings for the collapse guard to protect."""
        log = self.sync(aid)
        self.assertEqual(log.status, 'SUCCESS', log.error_message)
        self.assertGreater(self.entry_count(aid), 0)
        return log

    def schedules(self, aid) -> dict:
        """{channel id: [(start, title)] in start order} for every channel with listings."""
        rows = (db.session.query(EPGEntry.channel_id, EPGEntry.start_time, EPGEntry.title)
                .join(Channel, Channel.id == EPGEntry.channel_id)
                .filter(Channel.account_id == aid)
                .order_by(EPGEntry.channel_id, EPGEntry.start_time))
        out = defaultdict(list)
        for cid, start, title in rows:
            out[cid].append((start, title))
        return dict(out)


class GuideFetchFaultTests(_GuideCase):
    def test_a_guide_cut_off_mid_chunk_keeps_the_listings_and_says_why(self):
        """Guards dev/docs/BUGS.md 2026-07-21 10:20 AM (sync_log 122): the truncated chunked
        body fails the fetch, the sync finishes PARTIAL with the reason - never SUCCESS -
        the old listings stay, and the next healthy sync clears the standing alert."""
        aid = self.add_xtream()
        self.synced(aid)
        held = self.entry_count(aid)
        sid = self.source_id(aid)
        fault = self.sim.inject('truncated_chunked', 'north-tv', endpoints=['guide'])

        log = self.sync(aid)
        self.assertEqual(log.status, 'PARTIAL')
        self.assertIn('fetch failed', log.error_message)
        self.assertTrue(names_a_cut_chunk(log.error_message), log.error_message)
        self.assertEqual(self.account(aid).status, 'OK')
        alert = self.assertAlert('EPG_SOURCE_FETCH_FAILED', f'epg-source:{sid}:fetch')
        self.assertIn('previous listings were kept', alert.body)
        self.assertEqual(self.entry_count(aid), held)

        self.sim.clear(fault)
        self.assertEqual(self.sync(aid).status, 'SUCCESS')
        self.assertNoAlert('EPG_SOURCE_FETCH_FAILED')

    def test_a_guide_that_stops_sending_past_the_read_timeout_is_a_failed_fetch(self):
        """Characterization: a drip slower than `sync.request_timeout_seconds` times out the
        fetch, and the listings are kept."""
        aid = self.add_xtream()
        self.synced(aid)
        held = self.entry_count(aid)
        # The one-second timeout is for the dripped fetch only. Set before the healthy sync
        # above, it timed out that sync's own guide read on a loaded box (dev/docs/BUGS.md
        # 2026-10-30 01:56 PM), and the characterization never ran.
        self.t.sandbox_config({'sync': {'request_timeout_seconds': 1}})
        self.sim.inject('slow_drip', 'north-tv', endpoints=['guide'], chunk_bytes=65536,
                        gap_seconds=1.5)
        log = self.sync(aid)
        self.assertEqual(log.status, 'PARTIAL')
        self.assertIn('timed out', log.error_message)
        self.assertEqual(self.entry_count(aid), held)

    def test_a_guide_answering_a_5xx_or_a_made_up_status_is_a_failed_fetch(self):
        """Guards dev/docs/BUGS.md 2026-09-23 06:35 AM for the made-up status (884 passed
        raise_for_status); the 5xx is characterization."""
        aid = self.add_xtream()
        self.synced(aid)
        held = self.entry_count(aid)
        for kind, said in (('server_error', '503 Server Error'),
                           ('nonstandard_status', 'the provider answered HTTP 884')):
            with self.subTest(kind=kind):
                fault = self.sim.inject(kind, 'north-tv', endpoints=['guide'])
                log = self.sync(aid)
                self.assertEqual(log.status, 'PARTIAL')
                self.assertIn(said, log.error_message)
                self.assertAlert('EPG_SOURCE_FETCH_FAILED',
                                 f'epg-source:{self.source_id(aid)}:fetch')
                self.assertEqual(self.entry_count(aid), held)
                self.sim.clear(fault)

    def test_a_guide_url_answering_a_playlist_is_named_as_a_playlist(self):
        """Guards dev/docs/BUGS.md 2026-11-01: a guide URL serving an #EXTM3U playlist is
        refused as a playlist - not as a malformed or empty feed - and the listings are
        kept, on a provider guide and on an M3U account's URL source alike."""
        for add, panel in ((self.add_xtream, 'north-tv'), (self.add_m3u, 'vale-m3u')):
            with self.subTest(panel=panel):
                aid = add()
                self.synced(aid)
                held = self.entry_count(aid)
                fault = self.sim.inject('guide_as_playlist', panel, endpoints=['guide'])
                log = self.sync(aid)
                self.assertEqual(log.status, 'PARTIAL')
                self.assertIn('the guide URL answered with an M3U playlist', log.error_message)
                self.assertNotIn('import refused', log.error_message)
                alert = self.assertAlert('EPG_SOURCE_FETCH_FAILED',
                                         f'epg-source:{self.source_id(aid)}:fetch')
                self.assertIn('M3U playlist', alert.body)
                self.assertEqual(self.entry_count(aid), held)
                self.sim.clear(fault)

    def test_a_url_guide_source_fails_the_same_way(self):
        """Characterization: an M3U account's guide is a URL source, refreshed and alerted
        under its own key like a provider guide."""
        aid = self.add_m3u()
        self.synced(aid)
        held = self.entry_count(aid)
        self.sim.inject('truncated_chunked', 'vale-m3u', endpoints=['guide'])
        log = self.sync(aid)
        self.assertEqual(log.status, 'PARTIAL')
        self.assertAlert('EPG_SOURCE_FETCH_FAILED', f'epg-source:{self.source_id(aid)}:fetch')
        self.assertEqual(self.entry_count(aid), held)


class GuideGoneForDaysTests(_GuideCase):
    """A guide URL answering 404 on every refresh until the listings kept from its last
    good one have all passed - a third-party guide URL on prod, 2026-10-25 onward."""

    def _days_pass(self, sid, hours=96):
        """What the clock does to a source that cannot be refreshed: everything it holds
        and its directory's horizons are `hours` further in the past."""
        shift = timedelta(hours=hours)
        for model in (EPGEntry, EpgAlternateEntry):
            for e in model.query.filter_by(source_id=sid):
                e.start_time -= shift
                e.stop_time -= shift
        for d in EpgSourceChannel.query.filter(EpgSourceChannel.source_id == sid,
                                               EpgSourceChannel.horizon_until.isnot(None)):
            d.horizon_until -= shift
        db.session.commit()

    def test_channels_move_to_the_source_still_holding_listings_and_the_rest_are_named(self):
        """Guards dev/docs/BUGS.md 2026-11-05 @ 07:25 PM: 291 channels stayed on a source
        that held nothing, 61 of them with another source's listings unused, and nothing
        said so. The account reads an XMLTV URL first (the same backend's guide through
        north-plus) and its provider's own guide second; the URL is the one that dies."""
        aid = self.add_xtream()
        dying_id = add_url_source(self.account(aid), {
            'name': 'URL guide', 'url': self.sim.account('north-plus').epg_url}).id
        db.session.commit()
        self.synced(aid)
        provider_id = EpgSource.query.filter_by(owner_account_id=aid, kind='provider').one().id
        on_dying = {c.id for c in self.channels(aid) if c.epg_source_id == dying_id}
        spare = {cid for (cid,) in db.session.query(EpgAlternateEntry.channel_id).filter(
            EpgAlternateEntry.source_id == provider_id).distinct()} & on_dying
        self.assertTrue(spare, 'the world must give the provider guide listings to offer')
        # One channel only the dying source lists, so the loss has someone to name.
        alone = sorted(spare)[0]
        spare.discard(alone)
        EpgAlternateEntry.query.filter_by(channel_id=alone).delete()
        EpgSourceChannel.query.filter(
            EpgSourceChannel.source_id == provider_id,
            db.func.lower(EpgSourceChannel.xml_id)
            == db.session.get(Channel, alone).epg_channel_id.lower()).delete(
            synchronize_session=False)
        db.session.commit()

        self._days_pass(dying_id)
        self.t.sandbox_config({'sync': {'epg_coverage_lost_alert_percent': 0}})
        self.sim.inject('nonstandard_status', 'north-plus', endpoints=['guide'], status=404)
        with mock.patch('app.accounts._import_source', wraps=accounts._import_source) as imp:
            log = self.sync(aid)

        self.assertEqual(log.status, 'PARTIAL')
        self.assertIn('fetch failed', log.error_message)
        self.assertAlert('EPG_SOURCE_FETCH_FAILED', f'epg-source:{dying_id}:fetch')
        moved_first = [c.args[0].id for c in imp.call_args_list]
        self.assertEqual(moved_first, [provider_id], 'the dead source was never imported')
        now_on = {c.id: c.epg_source_id for c in self.channels(aid)}
        self.assertEqual({cid for cid in spare if now_on[cid] == provider_id}, spare,
                         'every channel the provider guide holds listings for moved to it')
        ahead = {cid for (cid,) in db.session.query(EPGEntry.channel_id).filter(
            EPGEntry.channel_id.in_(spare), EPGEntry.source_id == provider_id,
            EPGEntry.stop_time > datetime.utcnow()).distinct()}
        self.assertEqual(ahead, spare, 'and each has listings ahead again')
        alert = self.assertAlert('EPG_SOURCE_COVERAGE_LOST', f'epg-source:{dying_id}:coverage')
        self.assertIn('have no guide from any source now', alert.body)
        self.assertIn(db.session.get(Channel, alone).name, alert.body)
        self.assertIn(f'{len(spare):,} now take their guide from another source', alert.body)


class GuideGzipTests(_GuideCase):
    def test_a_guide_sent_as_a_gzip_file_imports_like_the_plain_one(self):
        """Guards dev/docs/BUGS.md 2026-08-09 12:08 PM: a .xml.gz answer (application/gzip,
        no Content-Encoding) imports exactly what the same guide does uncompressed."""
        aid = self.add_xtream()
        self.synced(aid)
        plain = (self.guide_keys(aid), self.sync_span)
        self.sim.inject('gzip_file', 'north-tv')
        log = self.sync(aid)
        self.assertEqual(log.status, 'SUCCESS')
        self.assertSameGuide(plain, (self.guide_keys(aid), self.sync_span))
        self.assertEqual([r.faults for r in self.asked if r.endpoint == 'guide'], [('gzip_file',)])

    def test_a_gzip_file_that_will_not_decompress_is_a_failed_fetch(self):
        """Guards dev/docs/BUGS.md 2026-08-09 12:08 PM: gzip bytes that stop halfway are named
        as such, and the listings already held are left untouched."""
        aid = self.add_xtream()
        self.synced(aid)
        held = self.entry_count(aid)
        self.sim.inject('gzip_corrupt', 'north-tv')
        log = self.sync(aid)
        self.assertEqual(log.status, 'PARTIAL')
        self.assertIn('looked gzipped but would not decompress', log.error_message)
        self.assertAlert('EPG_SOURCE_FETCH_FAILED', f'epg-source:{self.source_id(aid)}:fetch')
        self.assertEqual(self.entry_count(aid), held)

    def test_a_gzip_encoded_guide_cut_short_reads_as_a_short_guide(self):
        """Characterization, measured here: a Content-Encoding: gzip answer that stops halfway
        raises nothing in requests - the client gets the part that decoded - so it lands as
        a truncated guide: imported and named on a first sync, refused with the listings
        kept once there are listings to keep."""
        aid = self.add_xtream()
        fault = self.sim.inject('gzip_corrupt', 'north-tv', delivery='encoding')
        log = self.sync(aid)
        sid = self.source_id(aid)
        self.assertEqual(log.status, 'PARTIAL')
        self.assertIn('import truncated:', log.error_message)
        self.assertAlert('EPG_SOURCE_IMPORT_TRUNCATED', f'epg-source:{sid}:truncated')
        self.assertEqual(self.entry_count(aid), log.epg_entries_synced)

        self.sim.clear(fault)
        self.synced(aid)
        held = self.entry_count(aid)
        self.sim.inject('gzip_corrupt', 'north-tv', delivery='encoding')
        log = self.sync(aid)
        self.assertEqual(log.status, 'PARTIAL')
        self.assertIn('import refused: the XMLTV feed is truncated', log.error_message)
        self.assertEqual(self.entry_count(aid), held)


class GuideContentFaultTests(_GuideCase):
    def test_a_truncated_guide_is_named_and_never_reported_healthy(self):
        """Guards dev/docs/BUGS.md 2026-08-17 08:02 PM: a guide that stops parsing finishes
        PARTIAL. On a first sync it is imported up to the cut, the count reported is what the
        database holds, and only the truncated alert stands; once there is a baseline it is
        refused before anything is deleted."""
        aid = self.add_xtream()
        fault = self.sim.inject('xmltv_truncated', 'north-tv')
        log = self.sync(aid)
        sid = self.source_id(aid)
        self.assertEqual(log.status, 'PARTIAL')
        self.assertTrue(log.error_message.startswith('EPG source "north-tv provider guide": '
                                                     'import truncated:'))
        self.assertGreater(log.epg_entries_synced, 0)
        self.assertEqual(self.entry_count(aid), log.epg_entries_synced)
        self.assertAlert('EPG_SOURCE_IMPORT_TRUNCATED', f'epg-source:{sid}:truncated')
        self.assertNoAlert('EPG_SOURCE_FETCH_FAILED')
        self.assertNoAlert('EPG_SOURCE_COLLAPSE_REFUSED')

        self.sim.clear(fault)
        self.synced(aid)
        self.assertNoAlert('EPG_SOURCE_IMPORT_TRUNCATED')
        held = self.entry_count(aid)
        self.sim.inject('xmltv_truncated', 'north-tv')
        log = self.sync(aid)
        self.assertEqual(log.status, 'PARTIAL')
        self.assertIn('import refused: the XMLTV feed is truncated or malformed', log.error_message)
        self.assertAlert('EPG_SOURCE_COLLAPSE_REFUSED', f'epg-source:{sid}:collapse')
        self.assertEqual(self.entry_count(aid), held)

    def test_a_guide_with_channels_and_no_programs_is_refused_and_named(self):
        """Guards dev/docs/BUGS.md 2026-09-23 06:35 AM: a feed listing its channels and no
        programs is refused naming that cause - with no baseline as well as with one, where
        the listings already held are kept."""
        aid = self.add_xtream()
        fault = self.sim.inject('xmltv_no_programs', 'north-tv')
        listed = len(self.sim.view('north-tv', endpoint='guide').guide)
        log = self.sync(aid)
        sid = self.source_id(aid)
        self.assertEqual(log.status, 'PARTIAL')
        self.assertIn('contained no program listings at all', log.error_message)
        self.assertIn(f'{listed} channel entries, 0 programs', log.error_message)
        self.assertIn('there was no previous EPG to keep', log.error_message)
        self.assertAlert('EPG_SOURCE_COLLAPSE_REFUSED', f'epg-source:{sid}:collapse')

        self.sim.clear(fault)
        self.synced(aid)
        held = self.entry_count(aid)
        self.sim.inject('xmltv_no_programs', 'north-tv')
        log = self.sync(aid)
        self.assertEqual(log.status, 'PARTIAL')
        self.assertIn('contained no program listings at all', log.error_message)
        self.assertEqual(self.entry_count(aid), held)

    def test_a_guide_a_fraction_of_its_last_size_is_refused(self):
        """Characterization of dev/changelog/244: the collapse guard refuses a guide that
        would leave a tenth of the listings, and keeps the ones held."""
        aid = self.add_xtream()
        self.synced(aid)
        held = self.entry_count(aid)
        self.sim.inject('guide_shrink', 'north-tv', keep=0.1)
        log = self.sync(aid)
        self.assertEqual(log.status, 'PARTIAL')
        self.assertIn('below the 20% collapse threshold', log.error_message)
        self.assertAlert('EPG_SOURCE_COLLAPSE_REFUSED',
                         f'epg-source:{self.source_id(aid)}:collapse')
        self.assertEqual(self.entry_count(aid), held)

    def test_channels_whose_guide_ids_differ_only_in_case_all_get_listings(self):
        """Guards dev/docs/BUGS.md 2026-07-11 03:59 PM: a channel whose id is spelled in
        another case than the guide's - beside a sibling spelling it the guide's way - gets
        the same listings as its sibling, never none."""
        aid = self.add_xtream()
        self.sim.inject('guide_id_case', 'north-tv')
        covered = {g.guide_id for g in self.sim.view('north-tv', endpoint='guide').guide
                   if g.has_programs}
        self.assertEqual(self.sync(aid).status, 'SUCCESS')
        schedules = self.schedules(aid)
        by_id = defaultdict(list)
        for c in self.channels(aid):
            if c.epg_channel_id and c.epg_channel_id.lower() in {g.lower() for g in covered}:
                by_id[c.epg_channel_id.lower()].append(c)
        flipped = [c for chans in by_id.values() for c in chans if c.epg_channel_id not in covered]
        self.assertTrue(flipped, 'the fault spelled no channel id differently')
        for chans in by_id.values():
            first = schedules.get(chans[0].id)
            self.assertTrue(first)
            for c in chans:
                self.assertEqual(schedules.get(c.id), first, c.epg_channel_id)

    def test_a_case_twin_in_the_guide_does_not_double_a_channels_listings(self):
        """Guards dev/docs/BUGS.md 2026-09-26 06:15 PM: the guide listing a second id that
        differs only in case, with another feed's schedule, leaves every channel holding
        exactly its own schedule - no second program at the same start."""
        aid = self.add_xtream()
        self.synced(aid)
        plain = self.schedules(aid)
        self.sim.inject('guide_id_twins', 'north-tv')
        plain_ids = len(self.world.panel_lineups['north-tv'].guide)
        self.assertGreater(len(self.sim.view('north-tv', endpoint='guide').guide), plain_ids)
        self.assertEqual(self.sync(aid).status, 'SUCCESS')
        self.assertEqual(self.schedules(aid), plain)

    def test_many_channels_on_one_guide_id_all_get_its_listings(self):
        """Characterization: every channel sharing one id holds that id's whole schedule."""
        aid = self.add_xtream()
        self.sim.inject('shared_guide_id', 'north-tv', count=12)
        shared = next(g.guide_id for g in self.world.panel_lineups['north-tv'].guide
                      if g.has_programs)
        self.assertEqual(self.sync(aid).status, 'SUCCESS')
        schedules = self.schedules(aid)
        on_it = [c for c in self.channels(aid) if c.epg_channel_id == shared]
        self.assertGreaterEqual(len(on_it), 12)
        first = schedules.get(on_it[0].id)
        self.assertTrue(first)
        self.assertTrue(all(schedules.get(c.id) == first for c in on_it))

    def test_an_entity_expansion_guide_is_stopped_and_imports_nothing_from_it(self):
        """Characterization of dev/changelog/482's hardening: the entity chain is refused by
        the parser, the sync finishes PARTIAL naming it, and no expanded text is stored."""
        aid = self.add_xtream()
        fault = self.sim.inject('xml_entities', 'north-tv')
        log = self.sync(aid)
        self.assertEqual(log.status, 'PARTIAL')
        self.assertIn('EntitiesForbidden', log.error_message)
        self.assertEqual(EPGEntry.query.filter(EPGEntry.title.like('%lol%')).count(), 0)

        self.sim.clear(fault)
        self.synced(aid)
        held = self.entry_count(aid)
        self.sim.inject('xml_entities', 'north-tv')
        self.assertEqual(self.sync(aid).status, 'PARTIAL')
        self.assertEqual(self.entry_count(aid), held)


class _LogoCase(SimSyncCase):
    def fetch_logo(self):
        """The logo cache fetching the first served logo, as its scheduled job does."""
        view = self.sim.account('north-tv')
        row = next(r for r in self.world.panel_lineups['north-tv'].rows if r.channel.logo)
        account = XtreamAccount(name='north-tv', base_url=view.base_url,
                                username=view.username, password=view.password, status='OK')
        db.session.add(account)
        db.session.flush()
        url = self.sim.provider.logo_url(row.channel.logo)
        channel = Channel(account_id=account.id, stream_id=row.channel.stream_id, name=row.name,
                          stream_url='http://unused.invalid/', in_guide=True, logo_url=url)
        db.session.add(channel)
        db.session.commit()
        cache_dir = os.path.join(self.t._tmpdir, 'logos')
        _fetch_one_logo(channel, cache_dir, load_config())
        db.session.expire_all()
        return db.session.get(Channel, channel.id), url, cache_dir


class LogoFaultTests(_LogoCase):
    def test_a_logo_that_is_not_a_raster_image_is_not_cached(self):
        """Guards dev/docs/BUGS.md 2026-08-14 09:42 AM for the SVG (a provider's script on the
        app's own origin); the oversized and web-page logos are characterization. Each is
        tried once, remembered as tried, and never written to the cache."""
        for kind in ('logo_svg', 'logo_oversized', 'logo_wrong_type'):
            with self.subTest(kind=kind):
                fault = self.sim.inject(kind, 'logos')
                channel, url, cache_dir = self.fetch_logo()
                self.assertIsNone(channel.logo_cache_path)
                self.assertEqual(channel.logo_cache_source_url, url)
                self.assertFalse(os.path.isdir(cache_dir) and os.listdir(cache_dir))
                self.assertEqual(self.sim.ledger.entries(fault=kind)[-1].endpoint, 'logo')
                self.sim.clear(fault)


class DeadLogoHostTests(_LogoCase):
    world_source = copy.deepcopy(FAULT_WORLD)
    world_source['hosts_down'] = ['logos']

    def test_a_logo_host_refusing_connections_is_remembered_as_tried(self):
        """Characterization: a dead logo host is one failed attempt, not a retry every run."""
        channel, url, _cache_dir = self.fetch_logo()
        self.assertIsNone(channel.logo_cache_path)
        self.assertEqual(channel.logo_cache_source_url, url)



class FillerGuideTests(_GuideCase):
    def test_a_guide_sources_filler_does_not_hide_an_event_slot(self):
        """Guards dev/docs/BUGS.md 2026-11-08 10:07 through the simulator's filler_guide fault: a
        real sync stores a guide that only repeats an event slot's name, and the slot is still
        matched by its name, never by its filler."""
        aid = self.add_xtream()
        self.sim.inject('filler_guide', 'north-tv')
        log = self.sync(aid)
        self.assertEqual(log.status, 'SUCCESS', log.error_message)
        filler = {g.filler for g in self.sim.view('north-tv', endpoint='guide').guide if g.filler}
        slot = re.compile(r'\b([A-Za-z]+) (?:EVENT|GAME|MATCH|PPV) \d+: ([A-Za-z]+) vs ([A-Za-z]+)\b', re.I)
        found = next(((ch, m) for ch in Channel.query.filter_by(account_id=aid).all()
                      if ch.name in filler and (m := slot.search(ch.name))), None)
        self.assertIsNotNone(found, 'the fault world carries no timed event slot')
        ch, m = found
        self.assertEqual({e.title for e in EPGEntry.query.filter_by(channel_id=ch.id)}, {ch.name})
        sport, away, home = m.groups()
        league = SportsLeague(tsdb_id=9100, name=sport, match_terms=json.dumps([sport]))
        db.session.add(league)
        for tid, team in ((901, home), (902, away)):
            db.session.add(SportsTeam(tsdb_id=tid, full_name=team, names=json.dumps([team]), city='',
                                      last_looked_up=self.now))
        db.session.flush()
        start = self.now + timedelta(hours=6)
        event = SportsEvent(tsdb_id=7001, league_id=league.id, name=f'{home} vs {away}', home_team_id=901,
                            away_team_id=902, home_name=home, away_name=away, start_at=start, local_date=start.date())
        db.session.add(event)
        db.session.commit()
        sports_match.run(pause=0)
        db.session.expire_all()
        mine = SportsMatch.query.filter_by(event_id=event.id, channel_id=ch.id)
        self.assertEqual({x.kind for x in mine}, {SPORTS_MATCH_KIND_CHANNEL_NAME})


class TeamChannelTests(_GuideCase):
    def test_a_providers_team_channel_is_not_read_as_a_game(self):
        """Guards dev/docs/BUGS.md 2026-11-08 01:18 through the simulator's team_channels fault: a team
        channel names its market's teams with nothing between them, and with only filler in its guide
        it is a name-match candidate, but it is never the game between two of those teams."""
        aid = self.add_xtream()
        self.sim.inject('team_channels', 'north-tv')
        log = self.sync(aid)
        self.assertEqual(log.status, 'SUCCESS', log.error_message)
        shape = re.compile(r'^([A-Z]+) TEAMS\| CBS ([A-Z]+) ([A-Z]+) METRO CITY$')
        found = next(((ch, m) for ch in Channel.query.filter_by(account_id=aid).all()
                      if (m := shape.match(ch.name))), None)
        self.assertIsNotNone(found, 'the fault world carries no team channel')
        ch, m = found
        self.assertEqual({e.title for e in EPGEntry.query.filter_by(channel_id=ch.id)}, {ch.name})
        sport, home, away = (x.title() for x in m.groups())
        league = SportsLeague(tsdb_id=9101, name=sport, match_terms=json.dumps([sport]))
        db.session.add(league)
        for tid, team in ((911, home), (912, away)):
            db.session.add(SportsTeam(tsdb_id=tid, full_name=team, names=json.dumps([team]), city='',
                                      last_looked_up=self.now))
        db.session.flush()
        start = self.now + timedelta(hours=6)
        event = SportsEvent(tsdb_id=7002, league_id=league.id, name=f'{home} vs {away}', home_team_id=911,
                            away_team_id=912, home_name=home, away_name=away, start_at=start, local_date=start.date())
        db.session.add(event)
        db.session.commit()
        sports_match.run(pause=0)
        db.session.expire_all()
        self.assertEqual(SportsMatch.query.filter_by(event_id=event.id, channel_id=ch.id).count(), 0)


if __name__ == '__main__':
    unittest.main()
