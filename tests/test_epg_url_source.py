"""Tier 2 - a URL EPG source on any account (DESIGN-epg-sources.md §3, §8.1, §9.2, §9.5;
dev/changelog/1104).

An XMLTV URL is an `epg_sources` row the account owns, added and managed from the account
page's Sources card, never the dead `Account.epg_url` column. These tests pin:

  - AddAndEditTests: Add source validates, subscribes last and fetches at once (a new
    source's directory is empty, so until its first import it covers nothing); the
    add-account form's XMLTV field creates a source for either account type; Edit re-fetches
    only when the URL changed; the owner rule.
  - RemovalTests: deleting a url source, or stopping using the provider's guide, moves each
    channel it was the guide for onto its next source from rows already held - no fetch -
    and says how many had nowhere to go.
  - RefreshTests: one refresh per source at a time, `refresh_started_at` set only while one
    runs, and a standalone refresh leaves no sync-progress entry behind.
  - ScheduleTests: the source's own interval job, the deferral when admission refuses, and
    the retry that runs re-anchoring the interval on itself (the rule dev/changelog/1103
    set for account syncs).
  - SurfaceTests: the stale alert, the restart guards, /jobs and the guide's source name.

No network: every feed is a local byte string and every fetch is patched (CLAUDE.md §Testing).
"""
import os
import sqlite3
import sys
import unittest
from datetime import datetime, timedelta
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import admission, db  # noqa: E402
from app import accounts as accounts_mod  # noqa: E402
from app import scheduler as sched  # noqa: E402
from app.accounts import import_source  # noqa: E402
from app.database import (Account, Alert, Channel, ChannelEvent, EPGEntry,  # noqa: E402
                          EpgAlternateEntry, EpgChannelKey, EpgSource, EpgSourceChannel,
                          EpgSourceSubscription, XtreamAccount, CHANNEL_EPG_SOURCE_CHANGED)
from tests.support import make_test_app, seed  # noqa: E402
from tests.support.seed import make_epg_source  # noqa: E402
from tests.test_epg_sources import CFG, REAL, _Fixture, _schedule, _xmltv  # noqa: E402

OTHER = ['Other News', 'Other Film', 'Other Sport']


def _no_refresh():
    """start_source_refresh with nothing behind it: the refresh itself is RefreshTests'."""
    return mock.patch('app.routes.accounts.start_source_refresh',
                      return_value=(True, 'Refreshing now.'))


class _RouteFixture(_Fixture):

    def setUp(self):
        super().setUp()
        self.t.app.config['WTF_CSRF_ENABLED'] = False
        self.client = self.t.app.test_client()

    def _url(self, source_id=None, tail=''):
        base = f'/api/accounts/{self.acct.id}/epg-sources'
        return f'{base}/{source_id}{tail}' if source_id else base


class AddAndEditTests(_RouteFixture):

    def test_add_subscribes_last_and_fetches_at_once(self):
        with _no_refresh() as start:
            r = self.client.post(self._url(), json={'url': 'https://guide.test/epg.xml.gz',
                                                    'name': 'Guide file'})
        self.assertEqual(r.status_code, 200, r.get_json())
        src = db.session.get(EpgSource, r.get_json()['source_id'])
        self.assertEqual((src.kind, src.name, src.url), ('url', 'Guide file',
                                                         'https://guide.test/epg.xml.gz'))
        self.assertIsNone(src.refresh_interval_hours)
        subs = EpgSourceSubscription.query.filter_by(account_id=self.acct.id).order_by(
            EpgSourceSubscription.priority).all()
        self.assertEqual([s.source_id for s in subs], [self.src.id, src.id])
        start.assert_called_once()
        self.assertEqual(start.call_args.args[1], src.id)

    def test_a_blank_name_takes_the_accounts(self):
        with _no_refresh():
            r = self.client.post(self._url(), json={'url': 'http://guide.test/x.xml'})
        self.assertEqual(db.session.get(EpgSource, r.get_json()['source_id']).name,
                         'Alpha XMLTV')

    def test_add_refuses_what_it_cannot_fetch(self):
        with _no_refresh() as start:
            for body in ({'url': ''}, {'url': 'ftp://guide.test/x.xml'},
                         {'url': 'guide.test/x.xml'},
                         {'url': 'http://guide.test/x', 'refresh_interval_hours': 5},
                         {'url': 'http://guide.test/x', 'refresh_interval_hours': 'soon'}):
                self.assertEqual(self.client.post(self._url(), json=body).status_code, 400, body)
        start.assert_not_called()
        self.assertEqual(EpgSource.query.count(), 1)

    def test_the_add_account_form_makes_the_xmltv_url_a_source_for_either_type(self):
        for account_type, extra in (('m3u', {'m3u_url': 'http://p.test/get.php'}),
                                    ('xtream', {'base_url': 'http://xt.test',
                                                'username': 'u', 'password': 'p'})):
            name = f'New {account_type}'
            with _no_refresh() as start:
                r = self.client.post('/accounts/new', data={
                    'name': name, 'account_type': account_type,
                    'epg_url': 'http://guide.test/epg.xml', **extra})
            self.assertEqual(r.status_code, 302, account_type)
            acct = Account.query.filter_by(name=name).one()
            self.assertIsNone(acct.epg_url, 'nothing writes the column any more')
            kinds = {s.kind: s for s in EpgSource.query.filter_by(owner_account_id=acct.id)}
            self.assertEqual(kinds['url'].url, 'http://guide.test/epg.xml')
            self.assertEqual('provider' in kinds, account_type == 'xtream')
            start.assert_called_once()

    def test_a_bad_xmltv_url_on_the_form_creates_nothing(self):
        with _no_refresh():
            r = self.client.post('/accounts/new', data={
                'name': 'Bad', 'account_type': 'm3u', 'm3u_url': 'http://p.test/get.php',
                'epg_url': 'not a url'})
        self.assertEqual(r.status_code, 200)
        self.assertIsNone(Account.query.filter_by(name='Bad').first())

    def test_editing_the_account_never_writes_epg_url(self):
        acct = db.session.get(Account, self.acct.id)
        acct.epg_url = None
        db.session.commit()
        r = self.client.post(f'/api/accounts/{self.acct.id}', json={
            'name': 'Alpha', 'account_type': 'm3u', 'm3u_url': 'http://p.test/get.php',
            'epg_url': 'http://ignored.test/epg.xml'})
        self.assertEqual(r.status_code, 200, r.get_json())
        db.session.expire_all()
        self.assertIsNone(db.session.get(Account, self.acct.id).epg_url)
        self.assertNotIn('epg_url', self.client.get(
            f'/api/accounts/{self.acct.id}').get_json()['account'])

    def test_edit_refetches_only_when_the_url_changed(self):
        body = {'url': self.src.url, 'name': 'Renamed', 'refresh_interval_hours': 12,
                'enabled': True}
        with _no_refresh() as start:
            self.assertEqual(self.client.post(self._url(self.src.id), json=body).status_code, 200)
            start.assert_not_called()
            body['url'] = 'http://moved.test/epg.xml'
            self.assertEqual(self.client.post(self._url(self.src.id), json=body).status_code, 200)
            start.assert_called_once()
        db.session.expire_all()
        src = db.session.get(EpgSource, self.src.id)
        self.assertEqual((src.name, src.url, src.refresh_interval_hours),
                         ('Renamed', 'http://moved.test/epg.xml', 12))

    def test_the_owner_edits_and_the_url_is_served_whole_only_to_it(self):
        r = self.client.get(self._url(self.src.id))
        self.assertEqual(r.get_json()['source']['url'], self.src.url)
        other = Account(name='Other', account_type='m3u', m3u_url='http://o.test', status='OK')
        db.session.add(other)
        db.session.commit()
        self.assertEqual(self.client.get(
            f'/api/accounts/{other.id}/epg-sources/{self.src.id}').status_code, 404)

    def test_the_provider_source_is_not_edited_or_deleted_here(self):
        acct = XtreamAccount(name='X', base_url='http://xt.test', username='u', password='p',
                             status='OK')
        db.session.add(acct)
        db.session.flush()
        prov = make_epg_source(acct)
        db.session.commit()
        base = f'/api/accounts/{acct.id}/epg-sources/{prov.id}'
        self.assertEqual(self.client.post(base, json={'url': 'http://x.test/e'}).status_code, 400)
        self.assertEqual(self.client.delete(base).status_code, 400)
        self.assertIsNotNone(db.session.get(EpgSource, prov.id))


class RemovalTests(_RouteFixture):
    """The provider-first account in these tests: self.src at priority 1, ext at 2."""

    def setUp(self):
        super().setUp()
        self.ch = self._channel('Alpha One', 'a1.test')
        self.ext = self._second_source()

    def _import_both(self, ext_covers=True):
        import_source(self.src, _xmltv(_schedule('a1.test', REAL)), epg_days=3, cfg=CFG)
        ext_listings = _schedule('a1.test', OTHER) if ext_covers else _schedule('zz', OTHER)
        import_source(self.ext, _xmltv(ext_listings), epg_days=3, cfg=CFG)

    def test_deleting_the_winner_moves_its_channels_to_the_next_source_without_a_fetch(self):
        self._import_both()
        # The winner is the first source; delete it through the owner's route.
        self.assertEqual(self._winner(self.ch), self.src.id)
        with mock.patch('app.accounts.requests.get') as fetch:
            r = self.client.delete(self._url(self.src.id))
        fetch.assert_not_called()
        self.assertEqual(r.status_code, 200, r.get_json())
        self.assertIn('1 now take their guide from another source', r.get_json()['message'])
        self.assertEqual(self._winner(self.ch), self.ext.id)
        self.assertEqual({t for _s, t in self._active(self.ch)}, set(OTHER))
        self.assertEqual(self._alternates(self.ch), [])
        ev = ChannelEvent.query.filter_by(channel_id=self.ch.id,
                                          event_type=CHANNEL_EPG_SOURCE_CHANGED).all()
        self.assertTrue(ev and 'Alpha url' in ev[-1].detail, [e.detail for e in ev])

    def test_everything_the_source_owned_goes_with_it(self):
        self._import_both()
        db.session.add(EpgChannelKey(channel_id=self.ch.id, source_id=self.src.id,
                                     key='x', origin='manual', status='accepted'))
        db.session.add(Alert(alert_type='EPG_SOURCE_FETCH_FAILED', severity='WARN',
                             title='t', body='b', source=f'epg-source:{self.src.id}:fetch'))
        db.session.commit()
        sid = self.src.id
        self.client.delete(self._url(sid))
        for model in (EPGEntry, EpgAlternateEntry, EpgSourceChannel, EpgChannelKey,
                      EpgSourceSubscription):
            self.assertEqual(model.query.filter_by(source_id=sid).count(), 0, model.__name__)
        self.assertIsNone(db.session.get(EpgSource, sid))
        self.assertIsNotNone(Alert.query.filter_by(
            source=f'epg-source:{sid}:fetch').one().dismissed_at)

    def test_a_channel_with_nowhere_to_go_is_counted_as_lost(self):
        self._import_both(ext_covers=False)
        r = self.client.delete(self._url(self.src.id))
        self.assertIn('1 have no guide from any source now', r.get_json()['message'])
        self.assertIsNone(self._winner(self.ch))
        self.assertEqual(self._active(self.ch), [])

    def test_delete_waits_for_a_running_refresh(self):
        db.session.get(EpgSource, self.src.id).refresh_started_at = datetime.utcnow()
        db.session.commit()
        self.assertEqual(self.client.delete(self._url(self.src.id)).status_code, 409)
        self.assertIsNotNone(db.session.get(EpgSource, self.src.id))

    def test_stop_using_and_use_again(self):
        self._import_both()
        r = self.client.post(self._url(self.src.id, '/stop-using'))
        self.assertEqual(r.status_code, 200, r.get_json())
        db.session.expire_all()
        src = db.session.get(EpgSource, self.src.id)
        self.assertFalse(src.enabled, 'nobody reads it, so the fetch stops too')
        self.assertIsNone(EpgSourceSubscription.query.filter_by(source_id=src.id).first())
        self.assertEqual(self._winner(self.ch), self.ext.id)
        self.assertEqual({s for s, _t in self._active(self.ch)}, {self.ext.id})
        self.assertEqual(EpgSourceChannel.query.filter_by(source_id=src.id).count() > 0, True,
                         'the directory is kept for Use again')
        self.assertNotIn(src, accounts_mod.sources_refreshed_by_sync(self.acct.id))

        r = self.client.post(self._url(src.id, '/use-again'))
        self.assertEqual(r.status_code, 200, r.get_json())
        db.session.expire_all()
        src = db.session.get(EpgSource, self.src.id)
        self.assertTrue(src.enabled)
        sub = EpgSourceSubscription.query.filter_by(source_id=src.id).one()
        self.assertEqual(sub.priority, 3, 'back at the end of the order')

    def test_the_account_page_offers_use_again_for_a_source_it_stopped_using(self):
        self.client.post(self._url(self.src.id, '/stop-using'))
        page = self.client.get(f'/accounts/{self.acct.id}').get_data(as_text=True)
        self.assertIn('data-act="source-use"', page)
        self.assertIn('not used by this account', page)


class RefreshTests(_Fixture):

    def test_the_stamp_is_set_only_while_the_refresh_runs(self):
        seen = {}

        def _inner(source, *a, **kw):
            seen['during'] = db.session.get(EpgSource, source.id).refresh_started_at
            return 0, None

        with mock.patch('app.accounts._refresh_source', side_effect=_inner):
            accounts_mod.refresh_source(self.src, 5, 3)
        db.session.expire_all()
        self.assertIsNotNone(seen['during'])
        self.assertIsNone(db.session.get(EpgSource, self.src.id).refresh_started_at)

    def test_the_stamp_is_cleared_when_the_refresh_raises(self):
        with mock.patch('app.accounts._refresh_source', side_effect=RuntimeError('boom')):
            with self.assertRaises(RuntimeError):
                accounts_mod.refresh_source(self.src, 5, 3)
        db.session.expire_all()
        self.assertIsNone(db.session.get(EpgSource, self.src.id).refresh_started_at)

    def test_a_source_already_refreshing_is_skipped_not_fetched_twice(self):
        lock = accounts_mod._get_source_lock(self.src.id)
        lock.acquire()
        try:
            with mock.patch('app.accounts._refresh_source') as inner:
                self.assertEqual(accounts_mod.refresh_source(self.src, 5, 3), (0, None))
            inner.assert_not_called()
        finally:
            lock.release()

    def test_a_standalone_refresh_leaves_no_sync_progress_behind(self):
        # Process-global and keyed on an account id every test app reuses.
        accounts_mod._sync_progress.pop(self.acct.id, None)
        self._channel('Alpha One', 'a1.test')
        import_source(self.src, _xmltv(_schedule('a1.test', REAL)), epg_days=3, cfg=CFG,
                      track_progress=False)
        self.assertIsNone(accounts_mod.get_sync_progress(self.acct.id))

    def test_refresh_now_is_deferred_when_admission_refuses(self):
        other = admission.try_start(admission.KIND_SYNC, 'account 99')
        try:
            with mock.patch('app.scheduler.defer_source_refresh',
                            return_value=datetime.utcnow() + timedelta(minutes=20)) as defer, \
                 mock.patch('app.accounts.threading.Thread') as thread:
                started, message = accounts_mod.start_source_refresh(self.t.app, self.src.id)
        finally:
            admission.release(other)
        self.assertFalse(started)
        thread.assert_not_called()
        defer.assert_called_once()
        self.assertIn('waits', message)


class ScheduleTests(unittest.TestCase):
    """Needs the live scheduler, against the temp jobstore: the assertions are about jobs."""

    def setUp(self):
        self.t = make_test_app(start_scheduler=True)
        self.acct = seed.make_account(name='Alpha')
        self.src = make_epg_source(self.acct)
        self.src.refresh_interval_hours = 6
        db.session.commit()

    def tearDown(self):
        self.t.cleanup()

    def _job(self, retry=False):
        jid = (sched.epg_source_retry_job_id if retry else sched.epg_source_job_id)(self.src.id)
        return sched.get_scheduler().get_job(jid)

    def test_an_interval_gets_a_job_and_none_or_off_removes_it(self):
        sched.schedule_epg_source_refresh(self.t.app, self.src.id)
        self.assertEqual(self._job().trigger.interval, timedelta(hours=6))
        db.session.get(EpgSource, self.src.id).enabled = False
        db.session.commit()
        sched.schedule_epg_source_refresh(self.t.app, self.src.id)
        self.assertIsNone(self._job())

    def test_the_first_run_follows_the_last_attempt(self):
        last = datetime.utcnow() - timedelta(hours=1)
        db.session.get(EpgSource, self.src.id).last_refresh_at = last
        db.session.commit()
        sched.schedule_epg_source_refresh(self.t.app, self.src.id)
        nxt = sched.to_naive_utc(self._job().next_run_time)
        self.assertGreaterEqual(nxt, last + timedelta(hours=6))
        self.assertLess(nxt, last + timedelta(hours=6, minutes=30))

    def test_a_refused_run_queues_a_retry_and_leaves_the_interval_alone(self):
        sched.schedule_epg_source_refresh(self.t.app, self.src.id)
        before = self._job().next_run_time
        refusal = admission.Refusal(kind=admission.KIND_SYNC, blocked_by=admission.KIND_SYNC,
                                    reason='an account sync is already running')
        with mock.patch('app.accounts.refresh_source_standalone', return_value=refusal):
            sched._epg_source_refresh_job(self.src.id)
        retry = self._job(retry=True)
        self.assertIsNotNone(retry)
        self.assertTrue(retry.kwargs['retry'])
        self.assertEqual(self._job().next_run_time, before)

    def test_a_retry_that_runs_re_anchors_the_interval_on_itself(self):
        sched.schedule_epg_source_refresh(self.t.app, self.src.id)
        before = sched.to_naive_utc(self._job().next_run_time)

        def _ran(app, source_id):
            db.session.get(EpgSource, source_id).last_refresh_at = datetime.utcnow()
            db.session.commit()

        started = datetime.utcnow()
        with mock.patch('app.accounts.refresh_source_standalone', side_effect=_ran):
            sched._epg_source_refresh_job(self.src.id, retry=True)
        after = sched.to_naive_utc(self._job().next_run_time)
        self.assertNotEqual(after, before)
        self.assertGreaterEqual(after, started + timedelta(hours=6))

    def test_an_on_time_run_does_not_move_it(self):
        sched.schedule_epg_source_refresh(self.t.app, self.src.id)
        before = self._job().next_run_time

        def _ran(app, source_id):
            db.session.get(EpgSource, source_id).last_refresh_at = datetime.utcnow()
            db.session.commit()

        with mock.patch('app.accounts.refresh_source_standalone', side_effect=_ran):
            sched._epg_source_refresh_job(self.src.id)
        self.assertEqual(self._job().next_run_time, before)

    def test_jobs_page_names_the_source_and_its_retry(self):
        from app.routes.jobs import _build_job_list
        sched.schedule_epg_source_refresh(self.t.app, self.src.id)
        with mock.patch('app.config.load_config', return_value={'sync': {}}):
            sched.defer_source_refresh(self.src.id, 'busy')
        with self.t.app.test_request_context():
            names = {j['display_name']: j for j in _build_job_list()}
        self.assertIn(f'EPG refresh: {self.src.name}', names)
        self.assertIn(f'EPG refresh retry: {self.src.name}', names)
        self.assertEqual(names[f'EPG refresh: {self.src.name}']['schedule_description'],
                         'every 6 hours')

    def test_deleting_the_source_or_its_account_removes_both_jobs(self):
        sched.schedule_epg_source_refresh(self.t.app, self.src.id)
        with mock.patch('app.config.load_config', return_value={'sync': {}}):
            sched.defer_source_refresh(self.src.id, 'busy')
        self.assertIsNotNone(self._job(retry=True))
        client = self.t.app.test_client()
        self.t.app.config['WTF_CSRF_ENABLED'] = False
        r = client.delete(f'/api/accounts/{self.acct.id}')
        self.assertEqual(r.status_code, 200, r.get_json())
        self.assertIsNone(self._job())
        self.assertIsNone(self._job(retry=True))


class SurfaceTests(_Fixture):

    def _stale(self):
        return Alert.query.filter_by(alert_type='EPG_SOURCE_STALE',
                                     dismissed_at=None).first()

    def test_stale_after_two_intervals_without_a_good_refresh_and_cleared_by_one(self):
        src = db.session.get(EpgSource, self.src.id)
        src.refresh_interval_hours = 6
        src.last_success_at = datetime.utcnow() - timedelta(hours=13)
        db.session.commit()
        accounts_mod.update_source_stale_alert(src.id)
        self.assertIsNotNone(self._stale())
        src.last_success_at = datetime.utcnow()
        db.session.commit()
        accounts_mod.update_source_stale_alert(src.id)
        self.assertIsNone(self._stale())

    def test_one_interval_late_or_riding_the_sync_is_not_stale(self):
        src = db.session.get(EpgSource, self.src.id)
        src.refresh_interval_hours = 6
        src.last_success_at = datetime.utcnow() - timedelta(hours=7)
        db.session.commit()
        accounts_mod.update_source_stale_alert(src.id)
        self.assertIsNone(self._stale())
        src.refresh_interval_hours = None
        src.last_success_at = datetime.utcnow() - timedelta(days=5)
        db.session.commit()
        accounts_mod.update_source_stale_alert(src.id)
        self.assertIsNone(self._stale())

    def test_both_restart_guards_name_a_standalone_refresh_but_not_one_inside_a_sync(self):
        from tools.check_busy import refreshing_epg_sources
        from app.routes.settings import _restart_blocking_rows
        db.session.get(EpgSource, self.src.id).refresh_started_at = datetime.utcnow()
        db.session.commit()
        path = self.t.app.config['SQLALCHEMY_DATABASE_URI'].replace('sqlite:///', '')
        self.assertEqual(refreshing_epg_sources(path)[0], [self.src.name])
        with self.t.app.test_request_context():
            self.assertIn('REFRESHING', [r['status'] for r in _restart_blocking_rows([])])
        db.session.get(Account, self.acct.id).status = 'SYNCING'
        db.session.commit()
        self.assertEqual(refreshing_epg_sources(path)[0], [])

    def test_check_busy_reads_a_database_from_before_the_column_as_idle(self):
        path = os.path.join(self.t._tmpdir, 'old.db')
        conn = sqlite3.connect(path)
        conn.execute('CREATE TABLE epg_sources (id INTEGER, name TEXT, owner_account_id INTEGER)')
        conn.commit()
        conn.close()
        from tools.check_busy import refreshing_epg_sources
        names, note = refreshing_epg_sources(path)
        self.assertEqual(names, [])
        self.assertIn('assuming idle', note)

    def test_the_guide_names_where_a_showing_came_from(self):
        from app.routes.guide import _program_dict, epg_source_names
        ch = self._channel('Alpha One', 'a1.test')
        import_source(self.src, _xmltv(_schedule('a1.test', REAL)), epg_days=3, cfg=CFG)
        entry = EPGEntry.query.filter_by(channel_id=ch.id).first()
        with self.t.app.test_request_context():
            prog = _program_dict(db.session.get(Channel, ch.id), entry, entry.start_time,
                                 entry.stop_time, stream_url='', template='{title}',
                                 tag_cleanup=None, rec=None, all_tags=[], tags_by_name={},
                                 tz=None, source_names=epg_source_names())
        self.assertEqual(prog['source_name'], self.src.name)


if __name__ == '__main__':
    unittest.main()
