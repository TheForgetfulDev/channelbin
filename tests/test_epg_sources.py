"""Tier 2 - EPG sources: the source table, per-source import, resolution and the surfaces
(DESIGN-epg-sources.md, dev/changelog/1101).

Before this, an account WAS its EPG source: the importer derived its channel map, its
delete scope and its collapse-guard baseline from `account.id`, so a channel could not be
fed by a second feed - a second import would delete the first's rows, and its smaller count
would read as a collapse against the first's baseline. These tests pin the shape that
replaced it:

  - ResolveTests: `resolve_active_source`'s fixed order (§5.1), pure.
  - NormalizeTests: the §7.3 name rule keeps every script's letters.
  - ImportTests: one source's import stamps `source_id`, writes its directory, sets
    `Channel.epg_source_id` and the source's counts and status.
  - TwoSourceTests: two sources on one account - the winner's rows in `epg_entries`, the
    loser's in `epg_alternate_entries`, a banner-only source losing to a real schedule
    whatever the priority, per-source delete scope and baseline, and a change of winner
    MOVING rows (never both sources' rows in `epg_entries` at once).
  - CoverageLostTests: a channel that had a guide and has none from any source is named
    in EPG_SOURCE_COVERAGE_LOST; a refused refresh neither raises nor clears it.
  - TeardownTests: account delete takes its sources and every row under them; the hide
    purge and the retention prune cover the alternates table.
  - MigrationTests: m072 turns each account's EPG into a source, stamps the rows, and an
    interrupted run is resumed (WARNING) rather than inferred complete.
  - PageTests: the channel page names the source and shows a single-title channel's title;
    the account page's EPG sources card lists the source.

No network - every feed is a local byte string (CLAUDE.md §Testing).
"""
import os
import sqlite3
import sys
import tempfile
import threading
import unittest
from datetime import datetime, timedelta
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import db  # noqa: E402
from app import migrations as M  # noqa: E402
from app.accounts import _do_sync, cleanup_old_epg_entries, import_source  # noqa: E402
from app.channel_hiding import purge_hidden_epg  # noqa: E402
from app.database import (Account, Alert, Channel, ChannelEvent, EPGEntry,  # noqa: E402
                          EpgAlternateEntry, EpgSource, EpgSourceChannel,
                          EpgSourceSubscription, XtreamAccount, CHANNEL_EPG_SOURCE_CHANGED)
from app.epg_sources import (Coverage, REASON_NONE, REASON_ONLY_LISTING,  # noqa: E402
                             REASON_OVERRIDE, REASON_PRIORITY, REASON_REAL_SCHEDULE,
                             normalize_name, resolve_active_source)
from tests.support import make_test_app  # noqa: E402
from tests.support import seed  # noqa: E402
from tests.support.seed import make_epg_source  # noqa: E402

CFG = {'sync': {'epg_collapse_threshold_percent': 20}}


def _xmltv(listings, channels=()):
    """listings: (xml_id, offset_minutes, duration_minutes, title). channels: (xml_id,
    [display names])."""
    now = datetime.utcnow()
    parts = ['<?xml version="1.0" encoding="UTF-8"?><tv>']
    for xml_id, names in channels:
        parts.append(f'<channel id="{xml_id}">'
                     + ''.join(f'<display-name>{n}</display-name>' for n in names)
                     + '</channel>')
    for xml_id, offset, dur, title in listings:
        start = now + timedelta(minutes=offset)
        stop = start + timedelta(minutes=dur)
        parts.append(f'<programme start="{start:%Y%m%d%H%M%S} +0000" '
                     f'stop="{stop:%Y%m%d%H%M%S} +0000" channel="{xml_id}">'
                     f'<title>{title}</title></programme>')
    parts.append('</tv>')
    return ''.join(parts).encode()


def _schedule(xml_id, titles, start=60):
    """One listing per title, back to back - a real schedule when there are 3+ titles."""
    return [(xml_id, start + i * 30, 30, t) for i, t in enumerate(titles)]


REAL = ['News', 'Film', 'Sport']
BANNER = ['Channel Banner'] * 3


class ResolveTests(unittest.TestCase):

    def test_first_in_priority_with_a_real_schedule_wins(self):
        cov = {1: Coverage(10, 5), 2: Coverage(10, 5)}
        self.assertEqual(resolve_active_source(None, [1, 2], cov), (1, REASON_PRIORITY))

    def test_a_banner_only_source_loses_to_a_real_schedule_whatever_the_order(self):
        cov = {1: Coverage(10, 1), 2: Coverage(10, 4)}
        self.assertEqual(resolve_active_source(None, [1, 2], cov), (2, REASON_REAL_SCHEDULE))

    def test_with_no_real_schedule_anywhere_the_first_covering_source_wins(self):
        cov = {2: Coverage(10, 1), 3: Coverage(10, 2)}
        self.assertEqual(resolve_active_source(None, [1, 2, 3], cov), (2, REASON_ONLY_LISTING))

    def test_the_override_wins_when_it_covers_the_channel(self):
        cov = {1: Coverage(10, 5), 2: Coverage(10, 1)}
        self.assertEqual(resolve_active_source(2, [1, 2], cov), (2, REASON_OVERRIDE))

    def test_an_override_with_no_coverage_falls_through(self):
        cov = {1: Coverage(10, 5)}
        self.assertEqual(resolve_active_source(2, [1, 2], cov), (1, REASON_PRIORITY))

    def test_nothing_covers_it(self):
        self.assertEqual(resolve_active_source(None, [1, 2], {}), (None, REASON_NONE))


class NormalizeTests(unittest.TestCase):

    def test_provider_delimiters_become_one_space(self):
        self.assertEqual(normalize_name('US| A&E HD'), normalize_name('US: A&E HD'))
        self.assertEqual(normalize_name('  UK - [BBC]  One  '), 'uk bbc one')

    def test_non_latin_letters_survive(self):
        # The loose rule deleted these and matched a Greek station to "FM" (§2.4).
        self.assertEqual(normalize_name('ΕΛΛΗΝΙΚΟΣ FM'), 'ελληνικοσ fm')
        self.assertNotEqual(normalize_name('ΕΛΛΗΝΙΚΟΣ FM'), normalize_name('FM'))


class _Fixture(unittest.TestCase):

    def setUp(self):
        self.t = make_test_app()
        self.acct = seed.make_account(name='Alpha')
        self.src = make_epg_source(self.acct)
        db.session.commit()

    def tearDown(self):
        self.t.cleanup()

    def _channel(self, name, epg_id, **kw):
        ch = seed.make_channel(self.acct, name=name, epg_channel_id=epg_id, **kw)
        db.session.commit()
        return ch

    def _second_source(self, priority=2, name='External'):
        src = EpgSource(kind='url', owner_account_id=self.acct.id, name=name,
                        url='http://external.test/epg.xml.gz')
        db.session.add(src)
        db.session.flush()
        db.session.add(EpgSourceSubscription(source_id=src.id, account_id=self.acct.id,
                                             priority=priority))
        db.session.commit()
        return src

    def _active(self, ch):
        return [(e.source_id, e.title) for e in
                EPGEntry.query.filter_by(channel_id=ch.id).order_by(EPGEntry.start_time)]

    def _alternates(self, ch):
        return [(e.source_id, e.title) for e in
                EpgAlternateEntry.query.filter_by(channel_id=ch.id)
                .order_by(EpgAlternateEntry.start_time)]

    def _winner(self, ch):
        db.session.expire_all()
        return db.session.get(Channel, ch.id).epg_source_id


class ImportTests(_Fixture):

    def test_rows_carry_the_source_and_the_channel_names_its_winner(self):
        ch = self._channel('Alpha One', 'a1.test')
        synced, reason = import_source(self.src, _xmltv(_schedule('a1.test', REAL)),
                                       epg_days=3, cfg=CFG)
        self.assertIsNone(reason)
        self.assertEqual(synced, 3)
        self.assertEqual({s for s, _t in self._active(ch)}, {self.src.id})
        self.assertEqual(self._winner(ch), self.src.id)

    def test_the_directory_records_every_channel_in_the_file(self):
        self._channel('Alpha One', 'a1.test')
        xml = _xmltv(_schedule('a1.test', REAL) + _schedule('other.test', BANNER),
                     channels=[('a1.test', ['Alpha One', 'A1']), ('empty.test', ['Nothing'])])
        import_source(self.src, xml, epg_days=3, cfg=CFG)
        rows = {r.xml_id: r for r in EpgSourceChannel.query.filter_by(source_id=self.src.id)}
        self.assertEqual(set(rows), {'a1.test', 'other.test', 'empty.test'})
        self.assertEqual(rows['a1.test'].entry_count, 3)
        self.assertEqual(rows['a1.test'].distinct_titles, 3)
        self.assertIn('Alpha One', rows['a1.test'].display_names)
        self.assertEqual(rows['other.test'].distinct_titles, 1)
        self.assertEqual(rows['other.test'].sole_title, 'Channel Banner')
        self.assertEqual(rows['empty.test'].entry_count, 0)
        self.assertIsNotNone(rows['a1.test'].horizon_until)

    def test_the_source_records_its_outcome_and_counts(self):
        self._channel('Alpha One', 'a1.test')
        self._channel('Alpha Two', 'a2.test')
        import_source(self.src, _xmltv(_schedule('a1.test', REAL)), epg_days=3, cfg=CFG)
        db.session.expire_all()
        src = db.session.get(EpgSource, self.src.id)
        self.assertEqual(src.last_status, 'OK')
        self.assertIsNotNone(src.last_success_at)
        self.assertEqual(src.entry_count, 3)
        self.assertEqual(src.channel_count, 1)
        self.assertEqual(src.active_channel_count, 1)

    def test_a_single_title_channel_is_still_imported(self):
        # §5.3, decided: keep and label. The distinct-title gate ranks, it does not drop.
        ch = self._channel('Event', 'ev.test')
        import_source(self.src, _xmltv(_schedule('ev.test', ['NFL - Steelers vs Panthers'] * 4)),
                      epg_days=3, cfg=CFG)
        self.assertEqual(len(self._active(ch)), 4)
        self.assertEqual(self._winner(ch), self.src.id)


class TwoSourceTests(_Fixture):

    def test_the_winner_holds_the_guide_and_the_loser_is_kept_as_alternates(self):
        ch = self._channel('Alpha One', 'a1.test')
        ext = self._second_source()
        import_source(self.src, _xmltv(_schedule('a1.test', REAL)), epg_days=3, cfg=CFG)
        import_source(ext, _xmltv(_schedule('a1.test', ['X', 'Y', 'Z'])), epg_days=3, cfg=CFG)
        self.assertEqual({s for s, _t in self._active(ch)}, {self.src.id})
        self.assertEqual({s for s, _t in self._alternates(ch)}, {ext.id})
        self.assertEqual(self._winner(ch), self.src.id)

    def test_a_real_schedule_beats_a_higher_priority_banner_and_rows_move(self):
        ch = self._channel('Alpha One', 'a1.test')
        ext = self._second_source()
        import_source(self.src, _xmltv(_schedule('a1.test', BANNER)), epg_days=3, cfg=CFG)
        self.assertEqual(self._winner(ch), self.src.id)

        import_source(ext, _xmltv(_schedule('a1.test', REAL)), epg_days=3, cfg=CFG)

        self.assertEqual(self._winner(ch), ext.id)
        self.assertEqual({s for s, _t in self._active(ch)}, {ext.id},
                         'the old winner\'s rows must leave epg_entries, never sit beside '
                         'the new winner\'s')
        self.assertEqual({s for s, _t in self._alternates(ch)}, {self.src.id})
        ev = ChannelEvent.query.filter_by(channel_id=ch.id,
                                          event_type=CHANNEL_EPG_SOURCE_CHANGED).all()
        self.assertEqual(len(ev), 1)
        self.assertIn('External', ev[0].detail)

    def test_one_sources_refresh_never_deletes_the_others_rows(self):
        ch = self._channel('Alpha One', 'a1.test')
        ext = self._second_source()
        import_source(self.src, _xmltv(_schedule('a1.test', REAL)), epg_days=3, cfg=CFG)
        import_source(ext, _xmltv(_schedule('a1.test', ['X', 'Y', 'Z'])), epg_days=3, cfg=CFG)
        import_source(ext, _xmltv(_schedule('a1.test', ['X', 'Y', 'Z'])), epg_days=3, cfg=CFG)
        self.assertEqual(len(self._active(ch)), 3)
        self.assertEqual(len(self._alternates(ch)), 3)

    def test_a_smaller_second_source_is_not_refused_against_the_first_ones_baseline(self):
        # The account-wide baseline read a second, smaller feed as a 90% collapse (§1).
        chans = [self._channel(f'C{i}', f'c{i}.test') for i in range(10)]
        ext = self._second_source()
        big = sum((_schedule(c.epg_channel_id, REAL) for c in chans), [])
        import_source(self.src, _xmltv(big), epg_days=3, cfg=CFG)
        _synced, reason = import_source(ext, _xmltv(_schedule('c0.test', REAL)),
                                        epg_days=3, cfg=CFG)
        self.assertIsNone(reason)


class CoverageLostTests(_Fixture):

    def _coverage_alert(self):
        return Alert.query.filter_by(alert_type='EPG_SOURCE_COVERAGE_LOST',
                                     source=f'epg-source:{self.src.id}:coverage').first()

    def test_a_channel_left_with_no_guide_is_named(self):
        keep = self._channel('Keeper', 'k.test')
        lost = self._channel('US: NHL NETWORK', 'nhl.test')
        import_source(self.src, _xmltv(_schedule('k.test', REAL) + _schedule('nhl.test', REAL)),
                      epg_days=3, cfg={'sync': {'epg_collapse_threshold_percent': 0}})
        import_source(self.src, _xmltv(_schedule('k.test', REAL)), epg_days=3,
                      cfg={'sync': {'epg_collapse_threshold_percent': 0}})
        alert = self._coverage_alert()
        self.assertIsNotNone(alert)
        self.assertIn('US: NHL NETWORK', alert.body)
        self.assertIsNone(self._winner(lost))
        self.assertEqual(self._winner(keep), self.src.id)

    def test_a_refused_refresh_leaves_the_alert_standing(self):
        self._channel('Keeper', 'k.test')
        self._channel('Lost', 'l.test')
        no_guard = {'sync': {'epg_collapse_threshold_percent': 0}}
        import_source(self.src, _xmltv(_schedule('k.test', REAL) + _schedule('l.test', REAL)),
                      epg_days=3, cfg=no_guard)
        import_source(self.src, _xmltv(_schedule('k.test', REAL)), epg_days=3, cfg=no_guard)
        self.assertIsNone(self._coverage_alert().dismissed_at)

        _synced, reason = import_source(self.src, _xmltv([]), epg_days=3, cfg=CFG)
        self.assertTrue(reason.startswith('import refused:'))
        db.session.expire_all()
        self.assertIsNone(self._coverage_alert().dismissed_at,
                          'a refusal changed no channel\'s guide, so it cannot clear this')

    def test_a_healthy_refresh_clears_it(self):
        self._channel('Keeper', 'k.test')
        self._channel('Lost', 'l.test')
        no_guard = {'sync': {'epg_collapse_threshold_percent': 0}}
        import_source(self.src, _xmltv(_schedule('k.test', REAL) + _schedule('l.test', REAL)),
                      epg_days=3, cfg=no_guard)
        import_source(self.src, _xmltv(_schedule('k.test', REAL)), epg_days=3, cfg=no_guard)
        import_source(self.src, _xmltv(_schedule('k.test', REAL)), epg_days=3, cfg=no_guard)
        db.session.expire_all()
        self.assertIsNotNone(self._coverage_alert().dismissed_at)


class TeardownTests(_Fixture):

    def test_deleting_the_account_takes_its_sources_and_every_row_under_them(self):
        ch = self._channel('Alpha One', 'a1.test')
        ext = self._second_source()
        import_source(self.src, _xmltv(_schedule('a1.test', REAL)), epg_days=3, cfg=CFG)
        import_source(ext, _xmltv(_schedule('a1.test', ['X', 'Y', 'Z'])), epg_days=3, cfg=CFG)
        self.assertTrue(self._alternates(ch))

        from app.routes.accounts import _delete_account_and_jobs
        with self.t.app.test_request_context():
            ok, _name = _delete_account_and_jobs(self.acct.id)
        self.assertTrue(ok)
        db.session.expire_all()
        for model in (EpgSource, EpgSourceSubscription, EpgSourceChannel,
                      EpgAlternateEntry, EPGEntry):
            self.assertEqual(model.query.count(), 0, model.__tablename__)

    def test_the_hide_purge_clears_alternates_and_the_winner(self):
        ch = self._channel('Alpha One', 'a1.test')
        ext = self._second_source()
        import_source(self.src, _xmltv(_schedule('a1.test', REAL)), epg_days=3, cfg=CFG)
        import_source(ext, _xmltv(_schedule('a1.test', ['X', 'Y', 'Z'])), epg_days=3, cfg=CFG)
        ch.hidden = True   # hidden-cache-write-ok: exercising the purge directly
        db.session.commit()
        purge_hidden_epg()
        db.session.commit()
        self.assertEqual(self._active(ch), [])
        self.assertEqual(self._alternates(ch), [])
        self.assertIsNone(self._winner(ch))

    def test_the_retention_prune_covers_alternates(self):
        ch = self._channel('Alpha One', 'a1.test')
        old = datetime.utcnow() - timedelta(days=5)
        db.session.add(EpgAlternateEntry(source_id=self.src.id, channel_id=ch.id, title='Old',
                                         start_time=old, stop_time=old + timedelta(hours=1)))
        db.session.commit()
        with mock.patch('app.accounts.load_config',
                        return_value={'sync': {'epg_keep_days': 1}}), \
             mock.patch('app.search_index.rebuild_search_indexes'):
            cleanup_old_epg_entries(self.t.app)
        db.session.expire_all()
        self.assertEqual(EpgAlternateEntry.query.count(), 0)


class XtreamProviderSourceTests(unittest.TestCase):

    def setUp(self):
        self.t = make_test_app()

    def tearDown(self):
        self.t.cleanup()

    def test_an_xtream_sync_gives_an_account_without_one_its_provider_source(self):
        acct = XtreamAccount(name='X', base_url='http://xt.test', username='u',
                             password='p', status='OK')
        db.session.add(acct)
        db.session.commit()
        with mock.patch('app.xtream_client._fetch_and_classify_xtream_streams',
                        return_value=([], None, 0)), \
             mock.patch('app.xtream_client.XtreamClient.check_auth', return_value={}), \
             mock.patch('app.accounts.refresh_source', return_value=(0, None)) as refresh:
            _do_sync(acct.id, threading.Event())
        src = EpgSource.query.filter_by(owner_account_id=acct.id).one()
        self.assertEqual(src.kind, 'provider')
        self.assertEqual(EpgSourceSubscription.query.filter_by(
            account_id=acct.id, source_id=src.id).count(), 1)
        self.assertEqual(refresh.call_count, 1)

    def test_creating_an_xtream_account_creates_its_provider_source(self):
        self.t.app.config['WTF_CSRF_ENABLED'] = False
        client = self.t.app.test_client()
        with mock.patch('app.scheduler.schedule_account_sync'):
            resp = client.post('/accounts/new', data={
                'name': 'Fresh', 'account_type': 'xtream', 'base_url': 'http://xt.test',
                'username': 'u', 'password': 'p', 'color': '#58a6ff'})
        self.assertIn(resp.status_code, (302, 303), resp.get_data(as_text=True)[:500])
        acct = Account.query.filter_by(name='Fresh').one()
        self.assertEqual(EpgSource.query.filter_by(owner_account_id=acct.id,
                                                   kind='provider').count(), 1)


_PRE72 = (
    'CREATE TABLE accounts (id INTEGER PRIMARY KEY, name VARCHAR(255) NOT NULL, '
    'account_type VARCHAR(32) NOT NULL, epg_url VARCHAR(2048))',
    'CREATE TABLE channels (id INTEGER PRIMARY KEY, account_id INTEGER NOT NULL)',
    'CREATE TABLE epg_entries (id INTEGER PRIMARY KEY, channel_id INTEGER NOT NULL, '
    'title VARCHAR(512) NOT NULL, stop_time DATETIME)',
    'CREATE TABLE alerts (id INTEGER PRIMARY KEY, alert_type VARCHAR(64) NOT NULL, '
    'dismissed_at DATETIME)',
)


class _CrashOn:
    def __init__(self, cur, trigger):
        self._cur, self._trigger, self.fired = cur, trigger, False

    def execute(self, sql, *args):
        if not self.fired and self._trigger in sql:
            self.fired = True
            raise RuntimeError('simulated crash')
        return self._cur.execute(sql, *args)

    def __getattr__(self, name):
        return getattr(self._cur, name)


class MigrationTests(unittest.TestCase):

    def setUp(self):
        self.td = tempfile.TemporaryDirectory()
        self.conn = sqlite3.connect(os.path.join(self.td.name, 'scratch.db'))
        self.cur = self.conn.cursor()
        for ddl in _PRE72:
            self.cur.execute(ddl)
        self.cur.executemany('INSERT INTO accounts VALUES (?, ?, ?, ?)', [
            (1, 'Xt', 'xtream', None), (2, 'M3u', 'm3u', 'http://m3u.test/epg.xml'),
            (3, 'Bare', 'm3u', None)])
        self.cur.executemany('INSERT INTO channels VALUES (?, ?)',
                             [(10, 1), (11, 1), (20, 2), (30, 3)])
        self.cur.executemany('INSERT INTO epg_entries (channel_id, title) VALUES (?, ?)',
                             [(10, 'a'), (10, 'b'), (20, 'c')])
        self.cur.executemany('INSERT INTO alerts (alert_type) VALUES (?)',
                             [('SYNC_EPG_FETCH_FAILED',), ('SYNC_FAILED',)])
        self.conn.commit()

    def tearDown(self):
        self.conn.close()
        self.td.cleanup()

    def _q(self, sql):
        return self.cur.execute(sql).fetchall()

    def _assert_migrated(self):
        sources = dict(((a, k), sid) for sid, a, k in
                       self._q('SELECT id, owner_account_id, kind FROM epg_sources'))
        self.assertEqual(set(sources), {(1, 'provider'), (2, 'url')})
        self.assertEqual(self._q("SELECT url FROM epg_sources WHERE kind='url'"),
                         [('http://m3u.test/epg.xml',)])
        self.assertEqual(sorted(self._q('SELECT account_id, priority FROM '
                                        'epg_source_subscriptions')), [(1, 1), (2, 1)])
        self.assertEqual(dict(self._q('SELECT channel_id, source_id FROM epg_entries')),
                         {10: sources[(1, 'provider')], 20: sources[(2, 'url')]})
        self.assertEqual(dict(self._q('SELECT id, epg_source_id FROM channels')),
                         {10: sources[(1, 'provider')], 11: None,
                          20: sources[(2, 'url')], 30: None})
        self.assertEqual(self._q("SELECT entry_count, active_channel_count FROM epg_sources "
                                 "WHERE owner_account_id = 1"), [(2, 1)])
        self.assertEqual(dict(self._q('SELECT alert_type, dismissed_at IS NOT NULL '
                                      'FROM alerts')),
                         {'SYNC_EPG_FETCH_FAILED': 1, 'SYNC_FAILED': 0})

    def test_every_accounts_epg_becomes_a_source(self):
        M._m072_epg_sources(self.conn, self.cur)
        self._assert_migrated()

    def test_rerunning_changes_nothing(self):
        M._m072_epg_sources(self.conn, self.cur)
        M._m072_epg_sources(self.conn, self.cur)
        self._assert_migrated()

    def test_an_interrupted_backfill_is_resumed_and_says_so(self):
        crashing = _CrashOn(self.cur, 'UPDATE channels SET epg_source_id')
        with self.assertRaises(RuntimeError):
            M._m072_epg_sources(self.conn, crashing)
        self.conn.rollback()
        with self.assertLogs('app.migrations', level='WARNING') as logs:
            M._m072_epg_sources(self.conn, self.cur)
        self.assertTrue(any('m072.epg_sources' in line for line in logs.output))
        self._assert_migrated()

    def test_registered_at_its_own_version(self):
        self.assertIs({v: fn for v, _d, fn in M.SCHEMA_MIGRATIONS}.get(72),
                      M._m072_epg_sources)


class PageTests(_Fixture):

    def test_the_channel_page_names_the_source_and_a_single_titles_title(self):
        ch = self._channel('Event', 'ev.test')
        import_source(self.src, _xmltv(_schedule('ev.test', ['NFL - Steelers vs Panthers'] * 4)),
                      epg_days=3, cfg=CFG)
        html = self.t.app.test_client().get(f'/channels/{ch.id}').get_data(as_text=True)
        self.assertIn('data-section="guide"', html)
        self.assertIn(f'Listings come from <strong>{self.src.name}</strong>', html)
        self.assertIn('listing one program all day: <em>NFL - Steelers vs Panthers</em>', html)
        self.assertNotIn('filler', html.lower())

    def test_the_account_page_lists_its_sources(self):
        self._channel('Alpha One', 'a1.test')
        import_source(self.src, _xmltv(_schedule('a1.test', REAL)), epg_days=3, cfg=CFG)
        html = self.t.app.test_client().get(f'/accounts/{self.acct.id}').get_data(as_text=True)
        self.assertIn('data-section="sources"', html)
        self.assertIn(self.src.name, html)
        self.assertNotIn('http://example.test/epg.xml', html,
                         'a url source is account-owned and masked in full')


if __name__ == '__main__':
    unittest.main()
