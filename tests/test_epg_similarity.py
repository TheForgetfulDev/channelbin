"""The EPG similarity scan (app/epg_similarity.py, dev/changelog/1325): which EPG ids carry
the same schedule, guide-wide.

Three layers: the scan over seeded listings (clusters, facets, links, the collapse's
identity), the run (admission, persistence, failure, the startup reconcile) and the EPG
Manager page and its API. Every DB-backed case builds a throwaway app.

  python3 -m unittest tests.test_epg_similarity
"""
import os
import sys
import unittest
from datetime import datetime, timedelta
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import admission, db  # noqa: E402
from app import epg_similarity as es  # noqa: E402
from app.database import (EPGEntry, EpgAlternateEntry, EpgScan, EpgScanLink,  # noqa: E402
                          EpgScanSchedule, EpgScanScheduleChannel, EpgSourceChannel,
                          EPG_SCAN_FAILED, EPG_SCAN_RUNNING, EPG_SCAN_SUCCESS)
from app.epg_sources import CMP_IDENTICAL, CMP_MOSTLY, CMP_SHIFTED  # noqa: E402
from tests.support import seed  # noqa: E402
from tests.support.app import make_test_app  # noqa: E402


def _hour(n):
    base = datetime.utcnow().replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)
    return base + timedelta(hours=n)


# A station's day: (hour offset, title, description, subtitle).
DAY = [(h, t, f'About {t}.', None) for h, t in enumerate(
    ('Morning News', 'Talk Show', 'Judge Show', 'Game Show', 'Evening News', 'Late Show',
     'Movie', 'Overnight'))]


def _rows(rows, *, desc=True, marks=False, subs=False, shift=0, hours=None, stop_min=60):
    out = []
    for h, t, d, s in rows:
        if hours is not None and h >= hours:
            continue
        out.append((h + shift / 60, (t + ' ᴸᶦᵛᵉ' if marks else t),
                    d if desc else None, ('Episode ' + t) if subs else s, stop_min))
    return out


class _Case(unittest.TestCase):
    def setUp(self):
        self.t = make_test_app()
        self.app = self.t.app
        self.client = self.app.test_client()
        self.acc = seed.make_account(name='Harbor', account_type='xtream')
        self.src = seed.make_epg_source(self.acc, name='Harbor guide')
        self.acc2 = seed.make_account(name='Northgate')
        self.src2 = seed.make_epg_source(self.acc2, name='Northgate XMLTV')
        db.session.commit()

    def tearDown(self):
        self.t.cleanup()

    def station(self, name, src, eid, rows=None, account=None, alt_src=None):
        """A channel on `src` keyed `eid` with `rows` as its guide - or, with `alt_src`,
        with `rows` held as that source's alternate listings for it."""
        account = account or (self.acc if src is self.src else self.acc2)
        ch = seed.make_channel(account, name=name, epg_channel_id=eid, epg_source_id=src.id)
        for h, title, desc, sub, mins in (rows or []):
            if alt_src is None:
                seed.make_epg_entry(ch, title=title, description=desc, sub_title=sub,
                                    start_time=_hour(h), duration_minutes=mins, source_id=src.id)
            else:
                start = _hour(h)
                db.session.add(EpgAlternateEntry(
                    channel_id=ch.id, source_id=alt_src.id, title=title, description=desc,
                    sub_title=sub, start_time=start, stop_time=start + timedelta(minutes=mins)))
        db.session.commit()
        return ch

    def scan(self):
        return es.scan_listings(cfg={})

    @staticmethod
    def by_id(result):
        return {(s.source_id, s.epg_id): s for s in result.schedules}


# ── The scan: clusters, facets, links ────────────────────────────────────────────

class ClusterTests(_Case):
    def test_identical_copies_on_two_sources_are_one_cluster_identical_in_every_way(self):
        self.station('WXYZ', self.src, 'wxyz.us', _rows(DAY))
        self.station('XYZ on Northgate', self.src2, 'abc4wxyz.us', _rows(DAY))
        r = self.scan()
        a, b = self.by_id(r)[(self.src.id, 'wxyz.us')], self.by_id(r)[(self.src2.id, 'abc4wxyz.us')]
        self.assertEqual(a.cluster, b.cluster)
        self.assertEqual(a.facets.exact, b.facets.exact)
        self.assertEqual(r.clusters, 1)
        self.assertEqual(r.identical, 1)
        self.assertEqual(r.links, [])

    def test_descriptions_set_two_copies_apart_inside_one_cluster(self):
        self.station('Rich', self.src, 'rich.us', _rows(DAY))
        self.station('Bare', self.src2, 'bare.us', _rows(DAY, desc=False))
        r = self.scan()
        rich, bare = self.by_id(r)[(self.src.id, 'rich.us')], self.by_id(r)[(self.src2.id, 'bare.us')]
        self.assertEqual(rich.cluster, bare.cluster)
        self.assertNotEqual(rich.facets.descriptions, bare.facets.descriptions)
        self.assertEqual(rich.facets.titles, bare.facets.titles)
        self.assertEqual(rich.facets.stops, bare.facets.stops)
        self.assertEqual(rich.facets.subtitles, bare.facets.subtitles)
        self.assertEqual((rich.stats.described, bare.stats.described), (8, 0))
        self.assertGreater(rich.stats.desc_chars, 0)
        self.assertEqual(bare.stats.desc_chars, 0)
        self.assertEqual(r.identical, 0)
        self.assertEqual(r.clusters, 1)

    def test_title_marks_set_copies_apart_by_titles_only_and_are_counted(self):
        """The ᴸᶦᵛᵉ-style tags some feeds carry: comparison_title() drops them, so the two
        are the same programs, and the titles facet plus the count say which copy has
        them - what a person picking a guide for searching wants to know."""
        self.station('Marked', self.src, 'marked.us', _rows(DAY, marks=True))
        self.station('Plain', self.src2, 'plain.us', _rows(DAY))
        r = self.scan()
        m, p = self.by_id(r)[(self.src.id, 'marked.us')], self.by_id(r)[(self.src2.id, 'plain.us')]
        self.assertEqual(m.cluster, p.cluster)
        self.assertNotEqual(m.facets.titles, p.facets.titles)
        self.assertEqual(m.facets.descriptions, p.facets.descriptions)
        self.assertEqual((m.stats.marked, p.stats.marked), (8, 0))

    def test_subtitles_and_end_times_each_have_their_own_facet(self):
        self.station('Subs', self.src, 'subs.us', _rows(DAY, subs=True))
        self.station('Short', self.src2, 'short.us', _rows(DAY, stop_min=30))
        self.station('Plain', self.src2, 'plain.us', _rows(DAY))
        r = self.scan()
        ids = self.by_id(r)
        subs, short, plain = (ids[(self.src.id, 'subs.us')], ids[(self.src2.id, 'short.us')],
                              ids[(self.src2.id, 'plain.us')])
        self.assertEqual(len({subs.cluster, short.cluster, plain.cluster}), 1)
        self.assertNotEqual(subs.facets.subtitles, plain.facets.subtitles)
        self.assertEqual(subs.facets.stops, plain.facets.stops)
        self.assertNotEqual(short.facets.stops, plain.facets.stops)
        self.assertEqual(short.facets.subtitles, plain.facets.subtitles)
        self.assertEqual(subs.stats.subtitled, 8)

    def test_a_copy_stopping_sooner_is_its_own_cluster_linked_as_identical(self):
        """One source lists 24 hours and another 7 days: not the same fingerprint, but
        identical where both list - the link carries the comparator's verdict."""
        self.station('Long', self.src, 'long.us', _rows(DAY))
        self.station('Short', self.src2, 'short.us', _rows(DAY, hours=4))
        r = self.scan()
        long_, short = self.by_id(r)[(self.src.id, 'long.us')], self.by_id(r)[(self.src2.id, 'short.us')]
        self.assertNotEqual(long_.cluster, short.cluster)
        self.assertEqual(r.clusters, 0)
        self.assertEqual(len(r.links), 1)
        link = r.links[0]
        self.assertEqual(link.verdict, CMP_IDENTICAL)
        self.assertEqual({r.schedules[link.a].epg_id, r.schedules[link.b].epg_id},
                         {'long.us', 'short.us'})
        self.assertEqual(link.compared, 4)

    def test_shifted_and_mostly_are_linked_and_unrelated_is_not(self):
        """Candidates come from programs starting at the same minute, so a copy with
        half its programs shifted is found and judged shifted; a copy shifted wholesale
        shares no start and is not a candidate - a stated limit (DESIGN-epg-sources.md
        §12.8), caught instead by the group page's What's On card, which compares every
        schedule in a group directly."""
        self.station('Base', self.src, 'base.us', _rows(DAY))
        self.station('Shifted', self.src2, 'shifted.us',
                     _rows(DAY[:4]) + _rows(DAY[4:], shift=20))
        mostly = _rows(DAY)
        mostly[2] = (2, 'Infomercial', 'Buy it.', None, 60)
        self.station('Mostly', self.src2, 'mostly.us', mostly)
        self.station('Sports', self.src2, 'sports.us',
                     [(h, f'Sports {h}', None, None, 60) for h in range(8)])
        r = self.scan()
        ids = self.by_id(r)
        verdict_of = {}
        for link in r.links:
            pair = {r.schedules[link.a].epg_id, r.schedules[link.b].epg_id}
            verdict_of[frozenset(pair)] = link.verdict
        self.assertEqual(verdict_of.get(frozenset({'base.us', 'shifted.us'})), CMP_SHIFTED)
        self.assertEqual(verdict_of.get(frozenset({'base.us', 'mostly.us'})), CMP_MOSTLY)
        self.assertFalse(any('sports.us' in p for p in verdict_of))
        self.assertEqual(len({ids[(self.src.id, 'base.us')].cluster,
                              ids[(self.src2.id, 'shifted.us')].cluster,
                              ids[(self.src2.id, 'mostly.us')].cluster}), 3)

    def test_two_channels_reading_one_id_are_one_schedule_with_both_channels(self):
        a = self.station('HD', self.src, 'wxyz.us', _rows(DAY))
        b = self.station('SD', self.src, 'wxyz.us', _rows(DAY))
        r = self.scan()
        s = self.by_id(r)[(self.src.id, 'wxyz.us')]
        self.assertEqual(sorted(s.channel_ids), sorted([a.id, b.id]))
        self.assertFalse(s.split)
        self.assertEqual(len(r.schedules), 1)

    def test_case_variant_ids_the_file_lists_separately_stay_two_schedules(self):
        """The prod case (dev/changelog/1139, 1324): `FoxSports2.us` and `foxsports2.us`
        are two feeds in one file, so the scan keys on the file id read, never the
        case-folded key."""
        for xml_id in ('FoxSports2.us', 'foxsports2.us'):
            db.session.add(EpgSourceChannel(source_id=self.src.id, xml_id=xml_id, entry_count=8))
        db.session.commit()
        self.station('FS2', self.src, 'FoxSports2.us', _rows(DAY))
        self.station('FS2 London', self.src, 'foxsports2.us',
                     [(h, f'London {h}', None, None, 60) for h in range(8)])
        r = self.scan()
        self.assertEqual(len(r.schedules), 2)
        self.assertEqual({s.epg_id for s in r.schedules}, {'FoxSports2.us', 'foxsports2.us'})
        # Two schedules by identity, not one bucket the split check pulled apart.
        self.assertFalse(any(s.split for s in r.schedules))

    def test_an_alternate_sources_rows_land_on_that_sources_schedule(self):
        """A channel whose guide is Harbor's also holds Northgate's listings for it as an
        alternate. Those rows are Northgate's schedule under that id, exactly as they are
        for a channel reading Northgate as its guide - one schedule, both channels."""
        active = self.station('XYZ on Northgate', self.src2, 'xyz.uk', _rows(DAY, desc=False))
        holder = self.station('XYZ on Harbor', self.src, 'xyz.uk', _rows(DAY))
        for h, title, desc, sub, mins in _rows(DAY, desc=False):
            start = _hour(h)
            db.session.add(EpgAlternateEntry(
                channel_id=holder.id, source_id=self.src2.id, title=title, description=desc,
                sub_title=sub, start_time=start, stop_time=start + timedelta(minutes=mins)))
        db.session.commit()
        r = self.scan()
        north = self.by_id(r)[(self.src2.id, 'xyz.uk')]
        self.assertEqual(sorted(north.channel_ids), sorted([active.id, holder.id]))
        self.assertTrue(north.active)
        self.assertEqual(len(r.schedules), 2)
        self.assertEqual(r.listings, 24)

    def test_a_schedule_held_only_as_an_alternate_is_marked_not_active(self):
        holder = self.station('XYZ', self.src, 'xyz.uk', _rows(DAY))
        for h, title, desc, sub, mins in _rows(DAY):
            start = _hour(h)
            db.session.add(EpgAlternateEntry(
                channel_id=holder.id, source_id=self.src2.id, title=title, description=desc,
                sub_title=sub, start_time=start, stop_time=start + timedelta(minutes=mins)))
        db.session.commit()
        r = self.scan()
        self.assertFalse(self.by_id(r)[(self.src2.id, 'xyz.uk')].active)
        self.assertTrue(self.by_id(r)[(self.src.id, 'xyz.uk')].active)

    def test_channels_on_one_id_holding_different_rows_are_split_and_marked(self):
        """Checked, not assumed: the import maps every channel reading one id onto the
        same programs, so this never fires on prod, but a schedule may never claim a
        channel it does not describe."""
        self.station('A', self.src, 'odd.us', _rows(DAY))
        self.station('B', self.src, 'odd.us', _rows(DAY, desc=False))
        r = self.scan()
        parts = [s for s in r.schedules if s.epg_id == 'odd.us']
        self.assertEqual(len(parts), 2)
        self.assertTrue(all(s.split for s in parts))

    def test_listings_already_over_are_not_read(self):
        ch = self.station('Old', self.src, 'old.us', _rows(DAY))
        past = _hour(0) - timedelta(hours=30)
        db.session.add(EPGEntry(channel_id=ch.id, source_id=self.src.id, title='Yesterday',
                                start_time=past, stop_time=past + timedelta(hours=1)))
        db.session.commit()
        r = self.scan()
        self.assertEqual(r.listings, 8)
        self.assertEqual(self.by_id(r)[(self.src.id, 'old.us')].stats.listings, 8)

    def test_a_channel_with_no_key_carries_no_schedule(self):
        self.station('Keyless', self.src, None, _rows(DAY))
        self.assertEqual(self.scan().schedules, [])


# ── The run: admission, rows, failure, startup ──────────────────────────────────

class RunTests(_Case):
    def setUp(self):
        super().setUp()
        self.station('WXYZ', self.src, 'wxyz.us', _rows(DAY))
        self.station('XYZ on Northgate', self.src2, 'abc4wxyz.us', _rows(DAY))
        self.station('Short', self.src2, 'short.us', _rows(DAY, hours=4))

    def tearDown(self):
        admission.reset_for_tests()
        super().tearDown()

    def _run(self):
        scan, reason = es.start_scan(self.app)
        self.assertIsNone(reason)
        return scan

    def _wait(self, scan_id):
        import time
        for _ in range(200):
            db.session.remove()
            scan = db.session.get(EpgScan, scan_id)
            if scan.status != EPG_SCAN_RUNNING:
                return scan
            time.sleep(0.05)
        self.fail('the scan never finished')

    def test_a_run_persists_the_result_and_releases_its_ticket(self):
        scan = self._wait(self._run().id)
        self.assertEqual(scan.status, EPG_SCAN_SUCCESS)
        self.assertEqual(scan.schedules, 3)
        self.assertEqual((scan.clusters, scan.identical, scan.links), (1, 1, 2))
        self.assertEqual(EpgScanSchedule.query.filter_by(scan_id=scan.id).count(), 3)
        self.assertEqual(EpgScanScheduleChannel.query.filter_by(scan_id=scan.id).count(), 3)
        self.assertEqual(EpgScanLink.query.filter_by(scan_id=scan.id).count(), 2)
        self.assertEqual(admission.active_kinds(), set())
        self.assertIsNotNone(scan.elapsed_ms)

    def test_a_second_run_replaces_the_first_runs_rows(self):
        first = self._wait(self._run().id).id
        second = self._wait(self._run().id).id
        self.assertEqual([s.id for s in EpgScan.query.all()], [second])
        self.assertEqual(EpgScanSchedule.query.filter_by(scan_id=first).count(), 0)
        self.assertEqual(EpgScanLink.query.filter_by(scan_id=first).count(), 0)
        self.assertEqual(EpgScanSchedule.query.filter_by(scan_id=second).count(), 3)

    def test_admission_refuses_a_scan_during_a_sync_and_creates_no_row(self):
        ticket = admission.try_start(admission.KIND_SYNC, 'account 1')
        try:
            scan, reason = es.start_scan(self.app)
        finally:
            admission.release(ticket)
        self.assertIsNone(scan)
        self.assertIn('sync', reason)
        self.assertEqual(EpgScan.query.count(), 0)

    def test_maintenance_yields_to_a_running_scan(self):
        ticket = admission.try_start(admission.KIND_EPG_SCAN, 'EPG Manager')
        try:
            refused = admission.try_start(admission.KIND_MAINTENANCE, 'database maintenance')
            self.assertFalse(refused.granted)
            self.assertIn('EPG similarity scan', refused.reason)
        finally:
            admission.release(ticket)

    def test_a_failed_run_is_marked_failed_with_the_reason_and_releases_its_ticket(self):
        with mock.patch.object(es, 'scan_listings', side_effect=RuntimeError('disk fell over')):
            scan = self._wait(self._run().id)
        self.assertEqual(scan.status, EPG_SCAN_FAILED)
        self.assertIn('disk fell over', scan.error)
        self.assertIsNotNone(scan.finished_at)
        self.assertEqual(admission.active_kinds(), set())

    def test_a_running_row_left_by_a_dead_process_is_failed_at_startup(self):
        db.session.add(EpgScan(started_at=datetime.utcnow() - timedelta(hours=1),
                               status=EPG_SCAN_RUNNING))
        db.session.commit()
        self.assertEqual(es.reconcile_interrupted_scans(), 1)
        scan = EpgScan.query.one()
        self.assertEqual(scan.status, EPG_SCAN_FAILED)
        self.assertIn('restart', scan.error)
        self.assertEqual(es.reconcile_interrupted_scans(), 0)


# ── The page and its API ────────────────────────────────────────────────────────

class PageTests(_Case):
    def setUp(self):
        super().setUp()
        # The route bodies, not the CSRF gate (tests/support/csrf.py covers that).
        self.t.cleanup()
        self.t = make_test_app(csrf=False)
        self.app = self.t.app
        self.client = self.app.test_client()
        self.acc = seed.make_account(name='Harbor', account_type='xtream')
        self.src = seed.make_epg_source(self.acc, name='Harbor guide')
        self.acc2 = seed.make_account(name='Northgate')
        self.src2 = seed.make_epg_source(self.acc2, name='Northgate XMLTV')
        db.session.commit()
        self.station('WXYZ', self.src, 'wxyz.us', _rows(DAY))
        self.station('XYZ on Northgate', self.src2, 'abc4wxyz.us', _rows(DAY))
        self.station('Bare', self.src2, 'bare.us', _rows(DAY, desc=False))
        self.station('Short', self.src2, 'short.us', _rows(DAY, hours=4))
        self.station('Lonely', self.src2, 'lonely.us',
                     [(h, f'Lonely {h}', None, None, 60) for h in range(8)])

    def tearDown(self):
        admission.reset_for_tests()
        super().tearDown()

    def _scan(self):
        scan = EpgScan(started_at=datetime.utcnow(), status=EPG_SCAN_RUNNING)
        db.session.add(scan)
        db.session.commit()
        es._persist(scan.id, es.scan_listings(cfg={}))
        return scan.id

    def test_the_page_says_so_before_any_scan(self):
        html = self.client.get('/epg-manager').get_data(as_text=True)
        self.assertIn('Not scanned yet', html)
        self.assertIn('No scan result yet', html)
        self.assertIn('data-act="epg-scan"', html)

    def test_the_page_lists_the_cluster_with_every_copy_and_what_differs(self):
        self._scan()
        html = self.client.get('/epg-manager').get_data(as_text=True)
        self.assertIn('Same programs, details differ', html)
        for eid in ('wxyz.us', 'abc4wxyz.us', 'bare.us'):
            self.assertIn(eid, html)
        self.assertIn('Descriptions', html)   # bare.us differs from the lead in descriptions
        self.assertIn('Identical where both list', html)   # short.us, linked
        self.assertNotIn('lonely.us', html)   # alone and related to nothing

    def test_the_kind_tabs_filter_and_count(self):
        self._scan()
        html = self.client.get('/epg-manager?kind=identical').get_data(as_text=True)
        self.assertIn('Nothing matches', html)
        html = self.client.get('/epg-manager?kind=details').get_data(as_text=True)
        self.assertIn('wxyz.us', html)
        html = self.client.get('/epg-manager?kind=related').get_data(as_text=True)
        self.assertIn('short.us', html)
        # Both clusters with a link: short.us's one-copy cluster and the three-copy one
        # it relates to - a one-copy cluster is listed here only for its links.
        self.assertEqual(html.count('class="em-cluster"'), 2)
        self.assertIn('b-abort">One copy', html)

    def test_search_matches_an_id_or_a_channel_name(self):
        self._scan()
        html = self.client.get('/epg-manager?q=Northgate').get_data(as_text=True)
        self.assertIn('abc4wxyz.us', html)
        html = self.client.get('/epg-manager?q=nowhere').get_data(as_text=True)
        self.assertIn('Nothing matches', html)

    def test_the_source_and_group_filters_narrow_to_clusters_touching_them(self):
        self._scan()
        html = self.client.get(f'/epg-manager?source={self.src.id}').get_data(as_text=True)
        self.assertIn('wxyz.us', html)
        from app.database import Channel
        short = Channel.query.filter_by(name='Short').one()
        group = seed.make_group(name='Shorts', members=[short])
        db.session.commit()
        html = self.client.get(f'/epg-manager?group_id={group.id}').get_data(as_text=True)
        self.assertIn('short.us', html)
        self.assertIn('Group: Shorts', html)
        # short.us is alone in its cluster; the three-copy cluster it links to is named
        # in its links, not listed as a cluster of its own.
        self.assertNotIn('b-sched">Same programs, details differ', html)
        self.assertEqual(html.count('class="em-cluster"'), 1)
        self.assertEqual(self.client.get('/epg-manager?group_id=999').status_code, 404)

    def test_the_api_starts_a_scan_and_refuses_a_second_while_it_runs(self):
        with mock.patch.object(es.threading.Thread, 'start'):
            r = self.client.post('/api/epg-manager/scan', json={})
            self.assertEqual(r.status_code, 202)
            self.assertEqual(r.get_json()['scan']['status'], EPG_SCAN_RUNNING)
            r = self.client.post('/api/epg-manager/scan', json={})
            self.assertEqual(r.status_code, 409)
            self.assertIn('already running', r.get_json()['error'])
        admission.reset_for_tests()
        status = self.client.get('/api/epg-manager/scan').get_json()
        self.assertEqual(status['scan']['status'], EPG_SCAN_RUNNING)

    def test_the_api_names_the_blocker_when_admission_refuses(self):
        ticket = admission.try_start(admission.KIND_SYNC, 'account 1')
        try:
            r = self.client.post('/api/epg-manager/scan', json={})
        finally:
            admission.release(ticket)
        self.assertEqual(r.status_code, 409)
        self.assertIn('sync', r.get_json()['error'])

    def test_the_group_card_links_to_the_page_filtered_to_the_group(self):
        from app.database import Channel
        ch = Channel.query.filter_by(name='WXYZ').one()
        group = seed.make_group(name='XYZ feeds', members=[ch])
        db.session.commit()
        html = self.client.get(f'/channel-groups/{group.id}').get_data(as_text=True)
        self.assertIn(f'/epg-manager?group_id={group.id}', html)

    def test_the_maintenance_card_and_the_nav_entry_render(self):
        html = self.client.get('/maintenance').get_data(as_text=True)
        self.assertIn('id="epg-scan-card"', html)
        self.assertIn('data-act="epg-scan"', html)
        self.assertIn('js/epg-manager.js', html)
        self.assertIn('EPG Manager', html)
        self.assertIn('i-nav-epg-manager', html)


if __name__ == '__main__':
    unittest.main()
