"""The EPG source scorecard (app/epg_scorecard.py, dev/changelog/1717).

  - ScanCountTests: an import's scan pass stores each directory row's detail counts and
    doubled listings, read by the same element reader the import loop writes listings with,
    so the directory's "described" equals the described listings stored.
  - RefreshHistoryTests: every refresh outcome is a row, the oldest past the limit go, and a
    deleted source takes its history.
  - Pure rules: outvoted(), best_source(), reliability().
  - AccountScorecardTests: coverage, only-here, one title, detail, doubles, reach and the
    account's own by-day breakdown, read from stored rows.
  - CheckTests: run_check() stores compare_listings()' verdict per channel and pair, rewrites
    it whole, clears hidden channels, defers on a refusal; a deleted channel or source takes
    its rows.
  - SurfaceTests: the API and the account page.

  python3 -m unittest tests.test_epg_scorecard
"""
import os
import sqlite3
import sys
import tempfile
import unittest
from datetime import datetime, timedelta
from unittest import mock
from zoneinfo import ZoneInfo

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import admission, db  # noqa: E402
from app import migrations as M  # noqa: E402
from app.accounts import import_source  # noqa: E402
from app.database import (Channel, EPGEntry, EpgAlternateEntry, EpgSource,  # noqa: E402
                          EpgSourceAgreement, EpgSourceChannel, EpgSourceRefresh,
                          EpgSourceSubscription, EPG_REFRESH_HISTORY_LIMIT, EPG_STATUS_FAILED,
                          EPG_STATUS_OK, EPG_STATUS_REFUSED)
from app.epg_scorecard import (ChannelSource, account_scorecard, best_source,  # noqa: E402
                               card_line, outvoted, reliability, run_check)
from app.epg_sources import (CMP_IDENTICAL, CMP_MOSTLY, CMP_UNRELATED,  # noqa: E402
                             delete_url_source, last_local_day)
from tests.support import make_test_app, seed  # noqa: E402

ET = ZoneInfo('America/New_York')
CFG = {'sync': {'epg_collapse_threshold_percent': 20}}


def _program(xml_id, start, minutes, title, desc=None, sub=None, cat=None, rating=None,
             new=False, live=False):
    stop = start + timedelta(minutes=minutes)
    inner = f'<title>{title}</title>'
    if sub is not None:
        inner += f'<sub-title>{sub}</sub-title>'
    if desc is not None:
        inner += f'<desc>{desc}</desc>'
    if cat is not None:
        inner += f'<category>{cat}</category>'
    if rating is not None:
        inner += f'<rating><value>{rating}</value></rating>'
    if new:
        inner += '<new/>'
    if live:
        inner += '<live/>'
    return (f'<programme start="{start:%Y%m%d%H%M%S} +0000" stop="{stop:%Y%m%d%H%M%S} +0000" '
            f'channel="{xml_id}">{inner}</programme>')


def _xml(programs):
    return ('<?xml version="1.0" encoding="UTF-8"?><tv>' + ''.join(programs) + '</tv>').encode()


def _url_source(account, name, priority):
    src = EpgSource(kind='url', owner_account_id=account.id, name=name,
                    url=f'http://{name.lower()}.test/epg.xml')
    db.session.add(src)
    db.session.flush()
    db.session.add(EpgSourceSubscription(source_id=src.id, account_id=account.id,
                                         priority=priority))
    db.session.flush()
    return src


class ScanCountTests(unittest.TestCase):

    def setUp(self):
        self.t = make_test_app()
        self.acct = seed.make_account(name='Alpha')
        self.src = seed.make_epg_source(self.acct)
        self.ch = seed.make_channel(self.acct, name='Read', epg_channel_id='read.test')
        db.session.commit()

    def tearDown(self):
        self.t.cleanup()

    def _row(self, xml_id):
        return EpgSourceChannel.query.filter_by(source_id=self.src.id, xml_id=xml_id).one()

    def test_the_directory_counts_each_kind_of_detail_and_the_doubles(self):
        now = datetime.utcnow().replace(second=0, microsecond=0) + timedelta(hours=1)
        programs = [
            _program('read.test', now, 30, 'News', desc='About it', sub='Part 1', cat='News',
                     rating='TV-G', new=True),
            _program('read.test', now + timedelta(minutes=30), 30, 'Film', desc='',
                     cat='Movie'),
            _program('read.test', now + timedelta(minutes=60), 30, 'Sport', live=True),
            # A second program at a moment already listed.
            _program('read.test', now + timedelta(minutes=60), 30, 'Sport Again'),
            _program('unread.test', now, 30, 'Other', desc='x'),
            _program('unread.test', now, 30, 'Other twice'),
        ]
        import_source(self.src, _xml(programs), epg_days=3, cfg=CFG)
        row = self._row('read.test')
        self.assertEqual((row.entry_count, row.described, row.subtitled, row.categorized,
                          row.rated, row.flagged, row.doubled), (4, 1, 1, 2, 1, 2, 1))
        # An empty <desc/> is not a description, in the count and in the listing alike.
        described = EPGEntry.query.filter(EPGEntry.channel_id == self.ch.id,
                                          EPGEntry.description.isnot(None),
                                          EPGEntry.description != '').count()
        self.assertEqual(described, row.described)
        unread = self._row('unread.test')
        self.assertEqual(unread.described, 1)
        self.assertIsNone(unread.doubled, 'no channel reads it, so its starts were not kept')


class RefreshHistoryTests(unittest.TestCase):

    def setUp(self):
        self.t = make_test_app()
        self.acct = seed.make_account(name='Alpha')
        self.src = seed.make_epg_source(self.acct)
        seed.make_channel(self.acct, name='Read', epg_channel_id='read.test')
        db.session.commit()

    def tearDown(self):
        self.t.cleanup()

    def _history(self):
        return [(r.status, r.entries) for r in EpgSourceRefresh.query.filter_by(
            source_id=self.src.id).order_by(EpgSourceRefresh.id)]

    def test_each_refresh_is_a_row_with_how_it_ended(self):
        now = datetime.utcnow() + timedelta(hours=1)
        xml = _xml([_program('read.test', now + timedelta(minutes=30 * i), 30, f'P{i}')
                    for i in range(10)])
        import_source(self.src, xml, epg_days=3, cfg=CFG)
        # The collapse guard refuses a feed with one listing where ten were held.
        import_source(self.src, _xml([_program('read.test', now, 30, 'Only')]), epg_days=3,
                      cfg=CFG)
        self.assertEqual(self._history(), [(EPG_STATUS_OK, 10), (EPG_STATUS_REFUSED, 0)])

    def test_the_oldest_go_past_the_limit(self):
        from app.accounts import _report_source_outcome
        total = EPG_REFRESH_HISTORY_LIMIT + 3
        for i in range(total):
            _report_source_outcome(self.src.id, 'fetch failed: refused', None, entries=i)
        kept = self._history()
        self.assertEqual(len(kept), EPG_REFRESH_HISTORY_LIMIT)
        self.assertEqual([e for _s, e in kept], list(range(3, total)), 'the newest are kept')
        self.assertEqual(kept[0][0], EPG_STATUS_FAILED)

    def test_a_deleted_source_takes_its_history(self):
        from app.accounts import _report_source_outcome
        _report_source_outcome(self.src.id, None, None)
        delete_url_source(self.src.id, False)
        self.assertEqual(EpgSourceRefresh.query.count(), 0)


def _cs(sid, programs=10, titles=5, until=None, described=5, doubled=0):
    return ChannelSource(sid, programs, titles, until or datetime(2026, 11, 27, 12), described,
                         0, 0, 0, 0, doubled)


def _day(h):
    return h.date().isoformat()


class RuleTests(unittest.TestCase):

    def test_outvoted_needs_two_others_that_agree(self):
        v = {(1, 2): CMP_UNRELATED, (1, 3): CMP_UNRELATED, (2, 3): CMP_IDENTICAL}
        self.assertEqual(outvoted(v, [1, 2, 3]), {1})
        # Two sources that disagree: neither is outvoted.
        self.assertEqual(outvoted({(1, 2): CMP_UNRELATED}, [1, 2]), set())
        # The other two disagree with each other too: no majority.
        v[(2, 3)] = CMP_UNRELATED
        self.assertEqual(outvoted(v, [1, 2, 3]), set())

    def test_the_outvoted_source_loses_first(self):
        v = {(1, 2): CMP_UNRELATED, (1, 3): CMP_UNRELATED, (2, 3): CMP_MOSTLY}
        best, reasons = best_source([_cs(1, described=10), _cs(2), _cs(3)], v,
                                    {1: 'A', 2: 'B', 3: 'C'}, _day)
        self.assertEqual(best, 2)
        self.assertIn('A does not match the other sources', reasons[0])

    def test_a_full_schedule_beats_one_program_all_day(self):
        best, reasons = best_source([_cs(1, titles=1), _cs(2)], {}, {1: 'A', 2: 'B'}, _day)
        self.assertEqual(best, 2)
        self.assertEqual(reasons, ['A lists one program all day'])

    def test_a_source_whose_doubles_were_not_counted_is_not_called_clean(self):
        best, reasons = best_source([_cs(1, doubled=1), _cs(2, doubled=None)], {},
                                    {1: 'A', 2: 'B'}, _day)
        self.assertEqual((best, reasons), (1, ["A is first in this account's order"]))

    def test_doubles_then_reach_then_detail_then_order(self):
        names = {1: 'A', 2: 'B'}
        self.assertEqual(best_source([_cs(1, doubled=2), _cs(2)], {}, names, _day)[0], 2)
        longer = _cs(2, until=datetime(2026, 11, 29, 12))
        best, reasons = best_source([_cs(1, described=10), longer], {}, names, _day)
        self.assertEqual((best, reasons), (2, ['B runs to 2026-11-29, A stops sooner']))
        best, reasons = best_source([_cs(1, described=2), _cs(2, described=9)], {}, names, _day)
        self.assertEqual((best, reasons), (2, ['B describes 90% of its listings, A 20%']))
        best, reasons = best_source([_cs(1, described=8), _cs(2, described=9)], {}, names, _day)
        self.assertEqual((best, reasons), (1, ["A is first in this account's order"]))

    def test_reliability_counts_by_outcome_and_names_the_last_trouble(self):
        t = datetime(2026, 11, 24, 12)
        r = reliability([(t, EPG_STATUS_OK), (t - timedelta(hours=1), EPG_STATUS_REFUSED),
                         (t - timedelta(hours=2), EPG_STATUS_OK)])
        self.assertEqual((r.refreshes, r.ok, r.refused, r.failed), (3, 2, 1, 0))
        self.assertEqual(r.last_trouble, t - timedelta(hours=1))
        self.assertEqual(r.since, t - timedelta(hours=2))
        self.assertEqual(reliability([]).refreshes, 0)


class _TwoSources(unittest.TestCase):
    """One account reading A then B. Channels: 'both' (A and B), 'a-only', 'banner' (A, one
    title), and 'hidden' (A, hidden - counts nowhere)."""

    def setUp(self):
        self.t = make_test_app(csrf=False)
        self.acct = seed.make_account(name='Alpha')
        self.a = _url_source(self.acct, 'Afile', 1)
        self.b = _url_source(self.acct, 'Bfile', 2)
        self.until_a = datetime.utcnow() + timedelta(days=2)
        self.until_b = datetime.utcnow() + timedelta(days=4)
        self.both = seed.make_channel(self.acct, name='Both', epg_channel_id='both.test',
                                      epg_source_id=self.a.id)
        self.a_only = seed.make_channel(self.acct, name='A only', epg_channel_id='aonly.test',
                                        epg_source_id=self.a.id)
        self.banner = seed.make_channel(self.acct, name='Banner', epg_channel_id='banner.test',
                                        epg_source_id=self.a.id)
        self.hidden = seed.make_channel(self.acct, name='Hidden', epg_channel_id='hidden.test',
                                        epg_source_id=self.a.id, hidden=True)
        for xml_id, titles, doubled in (('both.test', 10, 1), ('aonly.test', 8, 0),
                                        ('banner.test', 1, 0), ('hidden.test', 9, 0)):
            self._dir(self.a, xml_id, titles, self.until_a, described=10, doubled=doubled)
        self._dir(self.b, 'BOTH.test', 6, self.until_b, described=2, doubled=None)
        db.session.commit()

    def tearDown(self):
        self.t.cleanup()

    def _dir(self, src, xml_id, titles, until, described, doubled):
        db.session.add(EpgSourceChannel(source_id=src.id, xml_id=xml_id, entry_count=20,
                                        distinct_titles=titles, horizon_until=until,
                                        described=described, subtitled=0, categorized=20,
                                        rated=0, flagged=0, doubled=doubled))


class AccountScorecardTests(_TwoSources):

    def test_the_numbers_come_from_the_directory_and_the_keys(self):
        card = account_scorecard(self.acct.id, ET, case_sensitive=False)
        a, b = card.sources
        self.assertEqual((a.source_id, b.source_id), (self.a.id, self.b.id))
        self.assertEqual((a.covered, a.only_here, a.guides, a.real_schedule, a.one_title),
                         (3, 2, 3, 2, 1), 'the hidden channel counts nowhere')
        self.assertEqual((b.covered, b.only_here, b.guides), (1, 0, 0),
                         'the case-folded id is the import\'s own match')
        self.assertEqual((a.programs, a.counted, a.described, a.categorized), (60, 60, 30, 60))
        self.assertEqual((a.doubled_channels, a.doubled_unknown), (1, 0))
        self.assertEqual((b.doubled_channels, b.doubled_unknown), (0, 1))
        self.assertEqual(a.reach.day, last_local_day(self.until_a, ET))
        self.assertEqual(b.reach.day, last_local_day(self.until_b, ET))
        self.assertEqual(card.guided.channels, 3, 'each guided channel at its own guide\'s day')
        self.assertIn('Covers 3 channels here, 2 only here. 1 lists one program all day.',
                      card_line(a))

    def test_a_key_moves_a_channel_onto_another_id(self):
        from app.database import EpgChannelKey, EPG_KEY_ACCEPTED, EPG_KEY_ORIGIN_MANUAL
        db.session.add(EpgChannelKey(channel_id=self.a_only.id, source_id=self.b.id,
                                     key='both.test', origin=EPG_KEY_ORIGIN_MANUAL,
                                     status=EPG_KEY_ACCEPTED))
        db.session.commit()
        b = account_scorecard(self.acct.id, ET, case_sensitive=False).sources[1]
        self.assertEqual(b.covered, 2)

    def test_the_reads_do_not_grow_with_the_channels(self):
        """A fixed number of statements whatever the channel count: the card view is read
        on every account page load (CLAUDE.md no hidden I/O in per-row loops)."""
        from tests.support.iocount import IOCounter, all_engines

        def count():
            db.session.expire_all()
            with IOCounter(all_engines()) as c:
                account_scorecard(self.acct.id, ET, case_sensitive=False, with_channels=True)
            return c.queries
        small = count()
        for i in range(40):
            ch = seed.make_channel(self.acct, name=f'More {i}', epg_channel_id=f'm{i}.test',
                                   epg_source_id=self.a.id)
            for src in (self.a, self.b):
                self._dir(src, f'm{i}.test', 5, self.until_a, described=1, doubled=0)
            db.session.add(EpgSourceAgreement(channel_id=ch.id, source_id=self.a.id,
                                              other_source_id=self.b.id, verdict=CMP_IDENTICAL,
                                              compared=3, paired=3,
                                              checked_at=datetime.utcnow()))
            db.session.add(EpgSourceRefresh(source_id=self.a.id, started_at=datetime.utcnow(),
                                            finished_at=datetime.utcnow(),
                                            status=EPG_STATUS_OK, entries=1))
        db.session.commit()
        self.assertEqual(count(), small)

    def test_channels_two_sources_cover_get_a_best_source(self):
        card = account_scorecard(self.acct.id, ET, case_sensitive=False, with_channels=True)
        self.assertEqual([c.channel_id for c in card.channels], [self.both.id])
        c = card.channels[0]
        # B reaches two days further. A lists two programs at once here, but B's doubles were
        # not counted, so that is not a reason either way.
        self.assertEqual(c.best, self.b.id)
        self.assertEqual(c.current, self.a.id)
        self.assertEqual(len(c.reasons), 1)
        self.assertIn('Bfile runs to', c.reasons[0])


class _Listings(_TwoSources):

    def _listings(self, channel, source, titles, active):
        model = EPGEntry if active else EpgAlternateEntry
        start = datetime.utcnow().replace(second=0, microsecond=0) + timedelta(minutes=30)
        for i, title in enumerate(titles):
            db.session.add(model(channel_id=channel.id, source_id=source.id, title=title,
                                 start_time=start + timedelta(minutes=30 * i),
                                 stop_time=start + timedelta(minutes=30 * (i + 1))))
        db.session.commit()


class CheckTests(_Listings):

    def _rows(self):
        return {(r.channel_id, r.source_id, r.other_source_id): r.verdict
                for r in EpgSourceAgreement.query}

    def test_the_verdict_of_each_pair_is_stored_and_rewritten(self):
        titles = ['News', 'Film', 'Sport', 'Talk', 'Quiz', 'Late']
        self._listings(self.both, self.a, titles, active=True)
        self._listings(self.both, self.b, titles, active=False)
        self._listings(self.a_only, self.a, titles, active=True)
        run_check()
        self.assertEqual(self._rows(), {(self.both.id, self.a.id, self.b.id): CMP_IDENTICAL})

        EpgAlternateEntry.query.filter_by(channel_id=self.both.id).delete()
        self._listings(self.both, self.b, ['Other'] * 6, active=False)
        run_check()
        self.assertEqual(self._rows(), {(self.both.id, self.a.id, self.b.id): CMP_UNRELATED})
        card = account_scorecard(self.acct.id, ET, case_sensitive=False)
        self.assertEqual((card.sources[0].compared, card.sources[0].disagree), (1, 1))

    def test_a_channel_hidden_since_keeps_no_verdict(self):
        titles = ['News', 'Film', 'Sport']
        self._listings(self.both, self.a, titles, active=True)
        self._listings(self.both, self.b, titles, active=False)
        run_check()
        db.session.get(Channel, self.both.id).hidden = True
        db.session.commit()
        run_check()
        self.assertEqual(self._rows(), {})

    def test_a_refused_run_queues_the_next_and_writes_nothing(self):
        held = admission.try_start(admission.KIND_SYNC, 'a sync', force=True)
        try:
            with mock.patch('app.scheduler.queue_epg_check') as queued:
                note = run_check()
        finally:
            admission.release(held)
        self.assertIn('waiting', note)
        queued.assert_called_once()
        self.assertEqual(EpgSourceAgreement.query.count(), 0)

    def test_a_deleted_source_or_channel_takes_its_verdicts(self):
        titles = ['News', 'Film', 'Sport']
        self._listings(self.both, self.a, titles, active=True)
        self._listings(self.both, self.b, titles, active=False)
        run_check()
        self.assertEqual(len(self._rows()), 1)
        delete_url_source(self.b.id, False)
        self.assertEqual(self._rows(), {})

    def test_the_channel_bulk_delete_and_the_orm_cascade_take_its_verdicts(self):
        for ch in (self.both, self.a_only):
            db.session.add(EpgSourceAgreement(channel_id=ch.id, source_id=self.a.id,
                                              other_source_id=self.b.id, verdict=CMP_IDENTICAL,
                                              compared=3, paired=3,
                                              checked_at=datetime.utcnow()))
        now = datetime.utcnow()
        db.session.get(Channel, self.both.id).last_seen_at = now - timedelta(days=30)
        self.acct.last_sync_at = now - timedelta(days=1)
        db.session.commit()
        resp = self.t.app.test_client().post('/channels/missing-delete',
                                             json={'channel_ids': [self.both.id]})
        self.assertEqual(resp.get_json()['deleted_count'], 1, resp.get_data(as_text=True))
        self.assertEqual([k[0] for k in self._rows()], [self.a_only.id])
        # The ORM cascade, which an account delete reaches.
        db.session.delete(db.session.get(Channel, self.a_only.id))
        db.session.commit()
        self.assertEqual(self._rows(), {})


class SurfaceTests(_Listings):

    def setUp(self):
        super().setUp()
        self.client = self.t.app.test_client()

    def test_the_api_carries_every_number_and_the_contested_channels(self):
        body = self.client.get(f'/api/accounts/{self.acct.id}/epg-scorecard').get_json()
        self.assertTrue(body['success'])
        self.assertEqual([s['source_id'] for s in body['sources']], [self.a.id, self.b.id])
        self.assertEqual(body['sources'][0]['covered'], 3)
        self.assertIn('name_matches', body['sources'][0])
        self.assertEqual([c['channel_id'] for c in body['channels']], [self.both.id])
        self.assertEqual(body['channels'][0]['best'], self.b.id)
        self.assertEqual(self.client.get('/api/accounts/9999/epg-scorecard').status_code, 404)

    def test_the_card_view_carries_the_lines_and_the_days_as_text(self):
        body = self.client.get(f'/api/accounts/{self.acct.id}/epg-scorecard?view=card').get_json()
        self.assertEqual([r['source_id'] for r in body['sources']], [self.a.id, self.b.id])
        self.assertTrue(body['sources'][0]['line'].startswith('Covers 3 channels here, 2 only here.'))
        self.assertIn('described', body['sources'][0]['tip'])
        self.assertTrue(body['guide_days'].startswith(
            '3 channels with a guide here, by the last day it runs through:'))
        self.assertNotIn('channels', body, 'the card view skips the per-channel list')

    def test_the_account_page_draws_a_hidden_place_for_each_line(self):
        html = self.client.get(f'/accounts/{self.acct.id}').get_data(as_text=True)
        for src in (self.a, self.b):
            self.assertIn(f'<div class="small" data-score-for="{src.id}" hidden></div>', html)
        self.assertNotIn('Covers 3 channels', html, 'the page never waits for the scorecard')


class MigrationTests(unittest.TestCase):

    def test_m146_adds_the_columns_and_tables_and_runs_twice(self):
        with tempfile.TemporaryDirectory() as td:
            conn = sqlite3.connect(os.path.join(td, 'm.db'))
            cur = conn.cursor()
            cur.executescript("""
                CREATE TABLE channels (id INTEGER PRIMARY KEY);
                CREATE TABLE epg_sources (id INTEGER PRIMARY KEY);
                CREATE TABLE epg_source_channels (id INTEGER PRIMARY KEY, source_id INTEGER,
                                                  xml_id VARCHAR(255));
            """)
            M._m146_epg_scorecard(conn, cur)
            M._m146_epg_scorecard(conn, cur)
            cols = {r[1] for r in cur.execute('PRAGMA table_info(epg_source_channels)')}
            self.assertTrue({'described', 'subtitled', 'categorized', 'rated', 'flagged',
                             'doubled'} <= cols)
            tables = {r[0] for r in cur.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            self.assertTrue({'epg_source_refreshes', 'epg_source_agreements'} <= tables)
            conn.close()


if __name__ == '__main__':
    unittest.main()
