"""Tier 2 - another account's EPG as a source (DESIGN-epg-sources.md §3, §9.5;
dev/changelog/1106).

Reading a source another account owns is one subscription row: nothing is fetched twice and
no credentials are copied. These tests pin:

  - SubscribeTests: the subscription goes last in the reader's priority order, so the
    borrowed guide only fills channels nothing above it covers (or covers with one program
    all day, §5.3); what the database already holds is copied at once and nothing is
    fetched; a covered key no reader holds waits for the owner's next refresh; the owner's
    next import writes the same rows the copy did.
  - BorrowableTests: Add source's list of other accounts' sources and what each would do.
  - TeardownTests: deleting an owner account, or a url source by hand, moves the other
    accounts' channels onto their next source and raises EPG_SOURCE_REMOVED naming them;
    deleting a reader recounts what it read.

No network: every feed is a local byte string (CLAUDE.md §Testing).
"""
import os
import sys
from datetime import datetime
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import db  # noqa: E402
from app.accounts import import_source  # noqa: E402
from app.database import (Alert, Channel, ChannelEvent, EpgSource,  # noqa: E402
                          EpgSourceSubscription, CHANNEL_EPG_SOURCE_CHANGED)
from app.epg_sources import borrowable_sources, subscribe_to_source  # noqa: E402
from tests.support import seed  # noqa: E402
from tests.support.seed import make_epg_source  # noqa: E402
from tests.test_epg_sources import BANNER, CFG, REAL, _Fixture, _schedule, _xmltv  # noqa: E402

OTHER = ['Other News', 'Other Film', 'Other Sport']


class _TwoAccounts(_Fixture):
    """self.acct (Alpha) owns self.src, whose file lists `a1.test`, `shared.test` and
    `ev.test` with a real schedule and `wait.test` that no Alpha channel carries. Beta reads
    its own source first: a real schedule for `shared.test`, one program for `ev.test`."""

    def setUp(self):
        super().setUp()
        self.t.app.config['WTF_CSRF_ENABLED'] = False
        self.client = self.t.app.test_client()
        for name, key in (('Alpha One', 'a1.test'), ('Alpha Shared', 'shared.test'),
                          ('Alpha Event', 'ev.test')):
            self._channel(name, key)
        import_source(self.src, _xmltv(
            _schedule('a1.test', OTHER) + _schedule('shared.test', OTHER)
            + _schedule('ev.test', OTHER) + _schedule('wait.test', OTHER)), epg_days=3, cfg=CFG)
        self.beta = seed.make_account(name='Beta')
        self.bsrc = make_epg_source(self.beta)
        self.gap = seed.make_channel(self.beta, name='Beta Gap', epg_channel_id='a1.test')
        self.has = seed.make_channel(self.beta, name='Beta Has', epg_channel_id='shared.test')
        self.ev = seed.make_channel(self.beta, name='Beta Event', epg_channel_id='ev.test')
        self.wait = seed.make_channel(self.beta, name='Beta Wait', epg_channel_id='wait.test')
        db.session.commit()
        import_source(self.bsrc, _xmltv(_schedule('shared.test', REAL)
                                        + _schedule('ev.test', BANNER)), epg_days=3, cfg=CFG)

    def _subscribe(self, source_id=None):
        return self.client.post(f'/api/accounts/{self.beta.id}/epg-sources/subscribe',
                                json={'source_id': source_id or self.src.id})


class SubscribeTests(_TwoAccounts):

    def test_it_fills_only_the_gaps_and_goes_last(self):
        with mock.patch('app.accounts.requests.get') as fetch:
            r = self._subscribe()
        fetch.assert_not_called()
        self.assertEqual(r.status_code, 200, r.get_json())
        subs = EpgSourceSubscription.query.filter_by(account_id=self.beta.id).order_by(
            EpgSourceSubscription.priority).all()
        self.assertEqual([s.source_id for s in subs], [self.bsrc.id, self.src.id])
        self.assertEqual(self._winner(self.gap), self.src.id)
        self.assertEqual({t for _s, t in self._active(self.gap)}, set(OTHER))
        self.assertEqual(self._winner(self.has), self.bsrc.id, 'a real guide is kept')
        self.assertEqual({t for _s, t in self._active(self.has)}, set(REAL))
        self.assertEqual({t for s, t in self._alternates(self.has) if s == self.src.id},
                         set(OTHER), 'the borrowed rows are kept aside, as an import keeps them')

    def test_one_program_all_day_switches_to_a_real_schedule(self):
        self._subscribe()
        self.assertEqual(self._winner(self.ev), self.src.id)
        self.assertEqual({t for _s, t in self._active(self.ev)}, set(OTHER))
        self.assertEqual({t for s, t in self._alternates(self.ev) if s == self.bsrc.id},
                         set(BANNER))
        ev = ChannelEvent.query.filter_by(channel_id=self.ev.id,
                                          event_type=CHANNEL_EPG_SOURCE_CHANGED).one()
        self.assertIn(self.src.name, ev.detail)

    def test_a_covered_key_nobody_holds_waits_for_the_next_refresh(self):
        r = self._subscribe()
        self.assertIsNone(self._winner(self.wait), 'not pointed at a source holding nothing')
        self.assertEqual(self._active(self.wait), [])
        msg = r.get_json()['message']
        self.assertIn('3 channel(s) got its listings right away', msg)
        self.assertIn('2 of them now take their guide from it', msg)
        self.assertIn('1 more it covers get their listings at its next refresh', msg)

    def test_the_next_import_writes_what_the_copy_did(self):
        self._subscribe()
        before = sorted(self._active(self.gap))
        import_source(db.session.get(EpgSource, self.src.id), _xmltv(
            _schedule('a1.test', OTHER) + _schedule('shared.test', OTHER)
            + _schedule('ev.test', OTHER) + _schedule('wait.test', OTHER)), epg_days=3, cfg=CFG)
        self.assertEqual(sorted(self._active(self.gap)), before)
        self.assertEqual(self._winner(self.wait), self.src.id, 'the waiting key arrives')

    def test_a_hidden_channel_gets_nothing(self):
        db.session.get(Channel, self.gap.id).hidden = True
        db.session.commit()
        self._subscribe()
        self.assertEqual(self._active(self.gap) + self._alternates(self.gap), [])

    def test_an_unrefreshed_source_copies_nothing_and_says_so(self):
        fresh = EpgSource(kind='url', owner_account_id=self.acct.id, name='Fresh',
                          url='http://fresh.test/x.xml')
        db.session.add(fresh)
        db.session.commit()
        r = self._subscribe(fresh.id)
        self.assertEqual(r.status_code, 200, r.get_json())
        self.assertIn('has no successful refresh yet', r.get_json()['message'])

    def test_own_already_read_and_refreshing_sources_are_refused(self):
        r = self.client.post(f'/api/accounts/{self.acct.id}/epg-sources/subscribe',
                             json={'source_id': self.src.id})
        self.assertEqual(r.status_code, 400)
        self.assertEqual(self._subscribe().status_code, 200)
        self.assertEqual(self._subscribe().status_code, 400)
        busy = EpgSource(kind='url', owner_account_id=self.acct.id, name='Busy',
                         url='http://busy.test/x.xml', refresh_started_at=datetime.utcnow())
        db.session.add(busy)
        db.session.commit()
        self.assertEqual(self._subscribe(busy.id).status_code, 409)
        self.assertIsNone(EpgSourceSubscription.query.filter_by(source_id=busy.id).first())

    def test_a_provider_source_nobody_read_is_turned_back_on_a_url_one_is_not(self):
        prov = EpgSource(kind='provider', owner_account_id=self.acct.id, name='Alpha guide',
                         enabled=False)
        off = EpgSource(kind='url', owner_account_id=self.acct.id, name='Off',
                        url='http://off.test/x.xml', enabled=False)
        db.session.add_all([prov, off])
        db.session.commit()
        self.assertTrue(subscribe_to_source(self.beta.id, prov.id, False).turned_on)
        self.assertFalse(subscribe_to_source(self.beta.id, off.id, False).turned_on)
        db.session.expire_all()
        self.assertTrue(db.session.get(EpgSource, prov.id).enabled)
        self.assertFalse(db.session.get(EpgSource, off.id).enabled, "the owner's switch")

    def test_stopping_a_borrowed_source_leaves_it_on_for_its_owner(self):
        self._subscribe()
        r = self.client.post(
            f'/api/accounts/{self.beta.id}/epg-sources/{self.src.id}/stop-using')
        self.assertEqual(r.status_code, 200, r.get_json())
        db.session.expire_all()
        self.assertTrue(db.session.get(EpgSource, self.src.id).enabled)
        self.assertIsNone(self._winner(self.gap))
        self.assertEqual(self._winner(self.ev), self.bsrc.id)


class BorrowableTests(_TwoAccounts):

    def test_each_option_says_what_it_would_do_here(self):
        # A source Beta owns and stopped using is Use again's, never offered here.
        db.session.add(EpgSource(kind='url', owner_account_id=self.beta.id, name='Beta idle',
                                 url='http://idle.test/x.xml', enabled=False))
        db.session.commit()
        opts = {o['id']: o for o in borrowable_sources(self.beta.id, False)}
        self.assertEqual(set(opts), {self.src.id}, "never the account's own sources")
        o = opts[self.src.id]
        self.assertEqual((o['covers'], o['fills'], o['switches']), (4, 2, 1))
        self.assertEqual(o['owner_name'], 'Alpha')

    def test_an_unrefreshed_source_is_unknown_not_zero(self):
        db.session.add(EpgSource(kind='url', owner_account_id=self.acct.id, name='Fresh',
                                 url='http://fresh.test/x.xml'))
        db.session.commit()
        fresh = [o for o in borrowable_sources(self.beta.id, False) if o['name'] == 'Fresh']
        self.assertIsNone(fresh[0]['covers'])

    def test_a_source_already_read_is_not_offered(self):
        self._subscribe()
        self.assertEqual(borrowable_sources(self.beta.id, False), [])
        r = self.client.get(f'/api/accounts/{self.beta.id}/epg-sources/borrowable')
        self.assertEqual(r.get_json()['sources'], [])


class TeardownTests(_TwoAccounts):

    def setUp(self):
        super().setUp()
        self._subscribe()

    def _removed(self):
        return Alert.query.filter_by(alert_type='EPG_SOURCE_REMOVED').all()

    def test_deleting_the_owner_moves_readers_to_their_next_source_and_says_so(self):
        r = self.client.delete(f'/api/accounts/{self.acct.id}')
        self.assertEqual(r.status_code, 200, r.get_json())
        self.assertEqual(self._winner(self.ev), self.bsrc.id, 'back to its own one program')
        self.assertEqual({t for _s, t in self._active(self.ev)}, set(BANNER))
        self.assertIsNone(self._winner(self.gap))
        self.assertEqual(self._active(self.gap), [])
        (alert,) = self._removed()
        self.assertEqual(alert.source, f'epg-source:{self.src.id}:removed')
        self.assertIn('"Beta"', alert.body)
        self.assertIn('with its account "Alpha"', alert.body)
        self.assertIn('1 of those channel(s) now take their guide from another source',
                      alert.body)
        self.assertIn('1 have no guide from any source now: Beta Gap', alert.body)

    def test_deleting_a_url_source_by_hand_does_the_same(self):
        r = self.client.delete(f'/api/accounts/{self.acct.id}/epg-sources/{self.src.id}')
        self.assertEqual(r.status_code, 200, r.get_json())
        self.assertEqual(self._winner(self.ev), self.bsrc.id)
        (alert,) = self._removed()
        self.assertIn('by hand on "Alpha"', alert.body)

    def test_a_source_nobody_else_read_raises_nothing(self):
        self.client.post(f'/api/accounts/{self.beta.id}/epg-sources/{self.src.id}/stop-using')
        self.client.delete(f'/api/accounts/{self.acct.id}')
        self.assertEqual(self._removed(), [])

    def test_deleting_the_reader_recounts_and_stops_a_source_nobody_reads(self):
        self.client.post(f'/api/accounts/{self.acct.id}/epg-sources/{self.src.id}/stop-using')
        db.session.expire_all()
        self.assertTrue(db.session.get(EpgSource, self.src.id).enabled, 'Beta still reads it')
        self.client.delete(f'/api/accounts/{self.beta.id}')
        db.session.expire_all()
        src = db.session.get(EpgSource, self.src.id)
        self.assertFalse(src.enabled)
        self.assertEqual(src.active_channel_count, 0)

    def test_the_confirm_and_the_card_name_the_readers(self):
        r = self.client.get(f'/api/accounts/{self.acct.id}/epg-readers')
        self.assertEqual(r.get_json()['readers'],
                         [{'id': self.beta.id, 'name': 'Beta', 'guided': 2}])
        page = self.client.get(f'/accounts/{self.acct.id}').get_data(as_text=True)
        self.assertIn('Also read by', page)
        self.assertIn('Beta (2 channels guided from here)', page)


class QueryPlanTests(_TwoAccounts):
    """dev/docs/BUGS.md 2026-09-23 @ 12:50:20 PM: a statement naming both a source and its
    channels on epg_entries walked ix_epg_entries_source - every row the source holds -
    once per statement; 682 s to copy one feed to 3,795 channels."""

    def _plans(self, fn):
        from sqlalchemy import event
        seen = []

        def _grab(conn, cursor, statement, params, context, executemany):
            if 'epg_entries' in statement and 'channel_id' in statement and not executemany:
                seen.append((statement, params))
        engine = db.engine
        event.listen(engine, 'before_cursor_execute', _grab)
        try:
            fn()
        finally:
            event.remove(engine, 'before_cursor_execute', _grab)
        raw = engine.raw_connection()
        try:
            cur = raw.cursor()
            return [(s, cur.execute('EXPLAIN QUERY PLAN ' + s, p).fetchall()) for s, p in seen
                    if s.lstrip().upper().startswith(('SELECT', 'INSERT', 'DELETE'))]
        finally:
            raw.close()

    def _assert_channel_index(self, fn):
        checked = 0
        for statement, plan in self._plans(fn):
            if 'channel_id' not in statement.split('FROM', 1)[-1]:
                continue    # a whole-source read, which the source index is right for
            steps = ' | '.join(str(row[-1]) for row in plan)
            if 'epg_entries ' not in steps:
                continue
            checked += 1
            self.assertNotIn('ix_epg_entries_source', steps, statement)
            self.assertIn('(channel_id=?)', steps, statement)
        self.assertTrue(checked, 'nothing reached epg_entries by channel')

    def test_the_copy_and_the_moves_reach_rows_by_channel(self):
        from app.epg_sources import demote
        self._assert_channel_index(lambda: self._subscribe())
        self._assert_channel_index(lambda: demote([self.gap.id], self.src.id))
