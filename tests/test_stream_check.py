"""An account's stream check: a few channels test-played to show its stream URLs play at all
(app/stream_check.py, dev/changelog/1665).

Three layers. The pure ones - which channels are picked, how one test's failure is read, what
the outcomes add up to - are tested on their own. The lifecycle - the opt-in at Add account,
the release after the first good sync, waiting on a busy tester, the alert, startup recovery,
the account delete - runs on a test app with the tester stubbed. And the reading of real ffmpeg
errors runs the real tester against the simulator: a refused login, a stream that does not
exist and one that plays, each as this box's ffmpeg reports it.
"""
import json
import os
import random
import shutil
import sys
import unittest
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import db  # noqa: E402
from app import stream_check as sc  # noqa: E402
from app.channel_tester import TestSignals  # noqa: E402
from app.database import (Account, AccountStreamCheck, Alert, ChannelTest,  # noqa: E402
                          ACCOUNT_STATUS_ERROR, ACCOUNT_STATUS_OK, ACCOUNT_STATUS_UNSYNCED,
                          STREAM_CHECK_DONE, STREAM_CHECK_PENDING_SYNC, STREAM_CHECK_RUNNING,
                          STREAM_CHECK_WAITING, TEST_STATUS_CANCELLED, TEST_STATUS_COMPLETED,
                          TEST_STATUS_FAILED)

from tests.support import make_test_app  # noqa: E402
from tests.support.csrf import csrf_token  # noqa: E402
from tests.support.seed import make_account, make_channel, make_epg_entry  # noqa: E402

# ffmpeg 7.1's own words, as account_links.py records them measured (dev/changelog/1168, 1169).
STDERR_401 = '[http @ 0x1] Server returned 401 Unauthorized (authorization failed)'
STDERR_403 = '[http @ 0x1] HTTP error 403 Forbidden\nServer returned 403 Forbidden (access denied)'
STDERR_404 = '[http @ 0x1] HTTP error 404 Not Found\nServer returned 404 Not Found'
STDERR_405 = 'HTTP error 405 Method Not Allowed\nServer returned 4XX Client Error, but not one of 40{0,1,3,4}'
STDERR_DNS = 'Failed to resolve hostname nowhere.invalid: Name or service not known'
STDERR_REFUSED = 'Connection to tcp://127.0.0.1:9 failed: Connection refused'
STDERR_TIMEOUT = 'Error opening input: Connection timed out'


def cand(cid, category, name=None, listings=True):
    return {'id': cid, 'name': name or f'Channel {cid}', 'category': category,
            'has_listings': listings}


def row(status=TEST_STATUS_FAILED, connected=False, error_detail=None):
    return SimpleNamespace(id=1, status=status, connected=connected, error_detail=error_detail)


class PickChannelsTests(unittest.TestCase):

    def test_local_and_network_categories_come_before_ppv_and_events(self):
        """A pay-per-view list is mostly dark at any moment, a local list mostly on."""
        cands = ([cand(i, 'US| LIVE PPV') for i in range(1, 20)]
                 + [cand(i, 'NFL SUNDAY TICKET') for i in range(20, 40)]
                 + [cand(40, 'US| LOCALS'), cand(41, 'US| NETWORKS'), cand(42, 'NEWS')])
        picked = sc.pick_channels(cands, random.Random(1), k=3)
        self.assertEqual({c['id'] for c in picked}, {40, 41, 42})

    def test_one_channel_per_category_while_categories_last(self):
        cands = ([cand(i, 'US| LOCALS') for i in range(1, 30)]
                 + [cand(i, 'UK| GENERAL') for i in range(30, 60)]
                 + [cand(60, 'CA| ENTERTAINMENT')])
        picked = sc.pick_channels(cands, random.Random(3), k=3)
        self.assertEqual(len({c['category'] for c in picked}), 3)

    def test_fills_from_a_category_already_used_when_categories_run_out(self):
        cands = [cand(i, 'US| LOCALS') for i in range(1, 10)]
        picked = sc.pick_channels(cands, random.Random(2), k=5)
        self.assertEqual(len(picked), 5)
        self.assertEqual(len({c['id'] for c in picked}), 5)

    def test_channels_with_listings_come_first_inside_a_rank(self):
        cands = ([cand(i, f'Cat {i}', listings=False) for i in range(1, 10)]
                 + [cand(10, 'Cat 10'), cand(11, 'Cat 11')])
        picked = sc.pick_channels(cands, random.Random(4), k=2)
        self.assertEqual({c['id'] for c in picked}, {10, 11})

    def test_an_event_name_ranks_with_the_unlikely_whatever_its_category(self):
        cands = [cand(1, 'US| LOCALS', name='PPV EVENT 04'), cand(2, 'Movies')]
        picked = sc.pick_channels(cands, random.Random(5), k=1)
        self.assertEqual(picked[0]['id'], 2)

    def test_unlikely_categories_are_still_used_when_nothing_else_is_there(self):
        cands = [cand(1, 'LIVE EVENTS'), cand(2, 'PPV')]
        self.assertEqual(len(sc.pick_channels(cands, random.Random(6), k=5)), 2)

    def test_same_seed_same_pick(self):
        cands = [cand(i, f'Cat {i % 7}') for i in range(1, 80)]
        a = sc.pick_channels(cands, random.Random(9))
        b = sc.pick_channels(cands, random.Random(9))
        self.assertEqual([c['id'] for c in a], [c['id'] for c in b])

    def test_category_rank(self):
        self.assertEqual(sc.category_rank('USA | PPV'), 2)
        self.assertEqual(sc.category_rank('US| LOCALS'), 0)
        self.assertEqual(sc.category_rank('Documentary'), 1)
        self.assertEqual(sc.category_rank(None), 1)


class ClassifyTests(unittest.TestCase):

    def check(self, expected, test, stderr='', hung=False, placeholder=False):
        self.assertEqual(sc.classify(test, TestSignals(stderr=stderr, hung=hung,
                                                       placeholder=placeholder)), expected)

    def test_each_outcome(self):
        self.assertEqual(sc.classify(None, None), sc.OUT_NOT_RUN)
        self.check(sc.OUT_INTERRUPTED, row(status=TEST_STATUS_CANCELLED))
        self.check(sc.OUT_OFFLINE, row(), placeholder=True)
        self.check(sc.OUT_PLAYED, row(status=TEST_STATUS_COMPLETED, connected=True))
        self.check(sc.OUT_REFUSED, row(), STDERR_401)
        self.check(sc.OUT_REFUSED, row(), STDERR_403)
        self.check(sc.OUT_NO_HOST, row(), STDERR_DNS)
        self.check(sc.OUT_UNREACHABLE, row(), STDERR_REFUSED)
        self.check(sc.OUT_NOT_FOUND, row(), STDERR_404)
        self.check(sc.OUT_HTTP_ERROR, row(), STDERR_405)
        self.check(sc.OUT_NO_ANSWER, row(), hung=True)
        self.check(sc.OUT_NO_ANSWER, row(), STDERR_TIMEOUT)
        self.check(sc.OUT_NO_DATA, row())

    def test_a_low_bitrate_failure_still_played(self):
        """The question is whether the URL plays, not how well."""
        self.check(sc.OUT_PLAYED, row(status=TEST_STATUS_FAILED, connected=True,
                                      error_detail='Low bitrate: 400 kbps'))

    def test_a_retry_that_connected_is_played_whatever_the_first_attempt_said(self):
        self.check(sc.OUT_PLAYED, row(status=TEST_STATUS_COMPLETED, connected=True), STDERR_401)


class JudgeTests(unittest.TestCase):

    def test_one_channel_playing_is_proof(self):
        self.assertEqual(sc.judge([sc.OUT_REFUSED, sc.OUT_PLAYED], 5), sc.VERDICT_WORKS)

    def test_an_offline_clip_proves_the_login(self):
        self.assertEqual(sc.judge([sc.OUT_NO_DATA, sc.OUT_OFFLINE], 5), sc.VERDICT_LOGIN_OK)

    def test_one_refusal_is_not_the_account(self):
        """A single 403 can be one channel the package does not include."""
        self.assertEqual(sc.judge([sc.OUT_REFUSED, sc.OUT_NO_DATA, sc.OUT_NO_ANSWER,
                                   sc.OUT_NOT_FOUND, sc.OUT_HTTP_ERROR], 5),
                         sc.VERDICT_INCONCLUSIVE)

    def test_two_refusals_are_the_login(self):
        self.assertEqual(sc.judge([sc.OUT_REFUSED, sc.OUT_REFUSED], 5), sc.VERDICT_LOGIN_REFUSED)

    def test_a_lone_channel_refused_is_the_login(self):
        self.assertEqual(sc.judge([sc.OUT_REFUSED], 1), sc.VERDICT_LOGIN_REFUSED)

    def test_two_host_failures_are_the_server(self):
        self.assertEqual(sc.judge([sc.OUT_NO_HOST, sc.OUT_UNREACHABLE], 5),
                         sc.VERDICT_SERVER_UNREACHABLE)

    def test_every_channel_not_found_points_at_the_urls(self):
        self.assertEqual(sc.judge([sc.OUT_NOT_FOUND] * 5, 5), sc.VERDICT_URLS_NOT_FOUND)

    def test_some_not_found_is_just_channels(self):
        self.assertEqual(sc.judge([sc.OUT_NOT_FOUND] * 4 + [sc.OUT_NO_DATA], 5),
                         sc.VERDICT_INCONCLUSIVE)

    def test_dead_channels_say_nothing_about_the_account(self):
        """Several channels that happen not to work do not mean the account is broken."""
        self.assertEqual(sc.judge([sc.OUT_NO_DATA, sc.OUT_NO_ANSWER, sc.OUT_HTTP_ERROR,
                                   sc.OUT_NO_DATA, sc.OUT_NO_ANSWER], 5),
                         sc.VERDICT_INCONCLUSIVE)

    def test_nothing_tested_is_no_verdict(self):
        self.assertIsNone(sc.judge([sc.OUT_NOT_RUN, sc.OUT_INTERRUPTED], 5))

    def test_should_stop(self):
        self.assertTrue(sc.should_stop([sc.OUT_NO_DATA, sc.OUT_PLAYED]))
        self.assertTrue(sc.should_stop([sc.OUT_REFUSED, sc.OUT_NO_DATA, sc.OUT_REFUSED]))
        self.assertTrue(sc.should_stop([sc.OUT_NO_HOST, sc.OUT_UNREACHABLE]))
        self.assertFalse(sc.should_stop([sc.OUT_REFUSED, sc.OUT_NO_DATA, sc.OUT_NOT_FOUND]))


class _AppCase(unittest.TestCase):

    def setUp(self):
        self.t = make_test_app()
        self.app = self.t.app
        self.addCleanup(self.t.cleanup)
        self.client = self.app.test_client()
        acc = make_account(name='Cove')
        self.account_id = acc.id
        cats = ['US| LOCALS', 'US| NEWS', 'UK| GENERAL', 'MOVIES', 'KIDS', 'LIVE PPV']
        self.channel_ids = [make_channel(acc, name=f'Ch {i}', category_name=cats[i % 6]).id
                            for i in range(12)]
        db.session.commit()

    def check_row(self, **fields):
        fields.setdefault('trigger', sc.TRIGGER_MANUAL)
        fields.setdefault('status', STREAM_CHECK_WAITING)
        c = AccountStreamCheck(account_id=self.account_id, **fields)
        db.session.add(c)
        db.session.commit()
        return c.id

    def fetch(self, check_id):
        db.session.remove()
        return db.session.get(AccountStreamCheck, check_id)

    def fake_sample(self, outcomes_by_index, busy=None):
        """A run_channel_sample stand-in: each channel's test row is written with the given
        outcome's signature, and stop_after is honored as the real one honors it."""
        app = self.app

        def _sample(_app, account_id, channel_ids, settings, wait, stop_after=None):
            if busy:
                return [], busy
            results = []
            for i, cid in enumerate(channel_ids):
                spec = outcomes_by_index[min(i, len(outcomes_by_index) - 1)]
                with app.app_context():
                    t = ChannelTest(channel_id=cid, test_started_at=datetime.utcnow(),
                                    status=spec.get('status', TEST_STATUS_FAILED),
                                    connected=spec.get('connected', False),
                                    error_detail=spec.get('detail'))
                    db.session.add(t)
                    db.session.commit()
                    tid = t.id
                result = {'channel_id': cid, 'test_id': tid,
                          'signals': TestSignals(stderr=spec.get('stderr', ''))}
                results.append(result)
                if stop_after and stop_after(result):
                    break
            return results, None
        return mock.patch('app.channel_tester.run_channel_sample', side_effect=_sample)


class LifecycleTests(_AppCase):

    def test_add_account_opt_in_owes_a_check_after_the_first_sync(self):
        token = csrf_token(self.client, '/accounts/new')
        with mock.patch('app.scheduler.schedule_account_sync'):
            resp = self.client.post('/accounts/new', data={
                'csrf_token': token, 'name': 'New One', 'account_type': 'm3u',
                'm3u_url': 'http://example.test/new.m3u', 'stream_check': 'on'})
        self.assertEqual(resp.status_code, 302)
        acc = Account.query.filter_by(name='New One').one()
        checks = AccountStreamCheck.query.filter_by(account_id=acc.id).all()
        self.assertEqual([(c.status, c.trigger) for c in checks],
                         [(STREAM_CHECK_PENDING_SYNC, sc.TRIGGER_FIRST_SYNC)])

    def test_add_account_without_the_opt_in_owes_nothing(self):
        token = csrf_token(self.client, '/accounts/new')
        with mock.patch('app.scheduler.schedule_account_sync'):
            self.client.post('/accounts/new', data={
                'csrf_token': token, 'name': 'Plain', 'account_type': 'm3u',
                'm3u_url': 'http://example.test/plain.m3u'})
        acc = Account.query.filter_by(name='Plain').one()
        self.assertEqual(AccountStreamCheck.query.filter_by(account_id=acc.id).count(), 0)

    def test_a_good_first_sync_releases_the_owed_check(self):
        cid = self.check_row(status=STREAM_CHECK_PENDING_SYNC, trigger=sc.TRIGGER_FIRST_SYNC)
        with mock.patch.object(sc, 'start') as start:
            sc.after_sync(self.app, self.account_id)
        start.assert_called_once_with(self.app, cid)
        self.assertEqual(self.fetch(cid).status, STREAM_CHECK_WAITING)

    def test_a_failed_first_sync_keeps_it_owed(self):
        acc = db.session.get(Account, self.account_id)
        acc.status = ACCOUNT_STATUS_ERROR
        db.session.commit()
        cid = self.check_row(status=STREAM_CHECK_PENDING_SYNC, trigger=sc.TRIGGER_FIRST_SYNC)
        with mock.patch.object(sc, 'start') as start:
            sc.after_sync(self.app, self.account_id)
        start.assert_not_called()
        self.assertEqual(self.fetch(cid).status, STREAM_CHECK_PENDING_SYNC)

    def test_sync_account_hands_over_to_the_check_after_it_lets_go(self):
        from app import accounts
        with mock.patch.object(accounts, '_do_sync'), \
                mock.patch('app.stream_check.after_sync') as after:
            accounts.sync_account(self.app, self.account_id)
        after.assert_called_once_with(self.app, self.account_id)

    def test_asking_twice_while_running_starts_nothing_new(self):
        self.check_row(status=STREAM_CHECK_RUNNING)
        with mock.patch.object(sc, 'start') as start:
            check_id, message = sc.request_check(self.app, self.account_id)
        start.assert_not_called()
        self.assertIn('already running', message)
        self.assertEqual(AccountStreamCheck.query.count(), 1)

    def test_asking_starts_a_check(self):
        with mock.patch.object(sc, 'start') as start:
            check_id, _ = sc.request_check(self.app, self.account_id)
        start.assert_called_once_with(self.app, check_id)
        self.assertEqual(self.fetch(check_id).status, STREAM_CHECK_WAITING)

    def test_api_refuses_an_account_never_synced(self):
        acc = db.session.get(Account, self.account_id)
        acc.status = ACCOUNT_STATUS_UNSYNCED
        db.session.commit()
        token = csrf_token(self.client, f'/accounts/{self.account_id}')
        resp = self.client.post(f'/api/accounts/{self.account_id}/stream-check',
                                headers={'X-CSRFToken': token})
        self.assertEqual(resp.status_code, 409)
        self.assertIn('error', resp.get_json())

    def test_api_starts_one(self):
        token = csrf_token(self.client, f'/accounts/{self.account_id}')
        with mock.patch.object(sc, 'start'):
            resp = self.client.post(f'/api/accounts/{self.account_id}/stream-check',
                                    headers={'X-CSRFToken': token})
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(resp.get_json()['success'])

    def test_a_busy_tester_puts_it_back_to_waiting_with_the_reason(self):
        cid = self.check_row()
        with self.fake_sample([], busy='Health check "Nightly"'), \
                mock.patch('app.scheduler.schedule_stream_check', return_value=True) as sched:
            sc.run_check(self.app, cid)
        check = self.fetch(cid)
        self.assertEqual(check.status, STREAM_CHECK_WAITING)
        self.assertIn('Health check "Nightly"', check.wait_reason)
        self.assertGreater(check.retry_at, datetime.utcnow() + timedelta(minutes=5))
        sched.assert_called_once()

    def test_the_first_channel_that_plays_ends_it(self):
        cid = self.check_row()
        with self.fake_sample([{'stderr': STDERR_404}, {'connected': True,
                                                        'status': TEST_STATUS_COMPLETED}]):
            sc.run_check(self.app, cid)
        check = self.fetch(cid)
        self.assertEqual((check.status, check.verdict), (STREAM_CHECK_DONE, sc.VERDICT_WORKS))
        self.assertEqual((check.tested_count, check.played_count), (2, 1))
        self.assertEqual([r['outcome'] for r in check.result_list()],
                         [sc.OUT_NOT_FOUND, sc.OUT_PLAYED])

    def test_a_refused_login_stops_at_two_and_raises_the_alert(self):
        cid = self.check_row()
        with self.fake_sample([{'stderr': STDERR_401}]):
            sc.run_check(self.app, cid)
        check = self.fetch(cid)
        self.assertEqual(check.verdict, sc.VERDICT_LOGIN_REFUSED)
        self.assertEqual(check.tested_count, 2)
        alert = Alert.query.filter_by(alert_type=sc.ALERT_TYPE).one()
        self.assertEqual(alert.source, f'account:{self.account_id}:stream-check')
        self.assertIn('refused', alert.title)

    def test_a_working_check_clears_the_alert(self):
        cid = self.check_row()
        with self.fake_sample([{'stderr': STDERR_401}]):
            sc.run_check(self.app, cid)
        cid2 = self.check_row()
        with self.fake_sample([{'connected': True, 'status': TEST_STATUS_COMPLETED}]):
            sc.run_check(self.app, cid2)
        db.session.remove()
        self.assertIsNotNone(Alert.query.filter_by(alert_type=sc.ALERT_TYPE).one().dismissed_at)

    def test_channels_that_simply_do_not_play_raise_no_alert(self):
        cid = self.check_row()
        with self.fake_sample([{'stderr': STDERR_TIMEOUT}, {'stderr': STDERR_405}, {}]):
            sc.run_check(self.app, cid)
        check = self.fetch(cid)
        self.assertEqual(check.verdict, sc.VERDICT_INCONCLUSIVE)
        self.assertEqual(check.tested_count, sc.SAMPLE_SIZE)
        self.assertEqual(Alert.query.filter_by(alert_type=sc.ALERT_TYPE).count(), 0)

    def test_a_check_preempted_by_a_recording_is_still_owed(self):
        cid = self.check_row()
        with self.fake_sample([{'status': TEST_STATUS_CANCELLED}]), \
                mock.patch('app.scheduler.schedule_stream_check', return_value=True):
            sc.run_check(self.app, cid)
        check = self.fetch(cid)
        self.assertEqual(check.status, STREAM_CHECK_WAITING)
        self.assertIn('recording', check.wait_reason)

    def test_an_account_with_nothing_to_test_says_so(self):
        from app.database import Channel
        Channel.query.filter_by(account_id=self.account_id).update({'test_enabled': False})
        db.session.commit()
        cid = self.check_row()
        sc.run_check(self.app, cid)
        self.assertEqual(self.fetch(cid).verdict, sc.VERDICT_NO_CHANNELS)

    def test_a_check_only_runs_from_waiting(self):
        """A retry job and a click landing together must run it once."""
        cid = self.check_row(status=STREAM_CHECK_RUNNING)
        with mock.patch('app.channel_tester.run_channel_sample') as sample:
            sc.run_check(self.app, cid)
        sample.assert_not_called()

    def test_restart_puts_a_running_check_back_to_waiting(self):
        cid = self.check_row(status=STREAM_CHECK_RUNNING)
        with mock.patch('app.scheduler.schedule_stream_check', return_value=True) as sched:
            sc.recover_at_startup(self.app)
        check = self.fetch(cid)
        self.assertEqual(check.status, STREAM_CHECK_WAITING)
        self.assertIn('restarted', check.wait_reason)
        sched.assert_called_once()

    def test_deleting_the_account_takes_its_checks_and_their_jobs(self):
        from app.routes.accounts import _delete_account_and_jobs
        cid = self.check_row()
        with mock.patch('app.scheduler.remove_job_if_exists') as remove:
            deleted, _ = _delete_account_and_jobs(self.account_id)
        self.assertTrue(deleted)
        self.assertEqual(AccountStreamCheck.query.count(), 0)
        self.assertIn(mock.call(f'stream_check_{cid}'), remove.call_args_list)

    def test_candidates_prefer_channels_with_listings(self):
        listed = self.channel_ids[3]
        from app.database import Channel
        make_epg_entry(db.session.get(Channel, listed), offset_minutes=0)
        db.session.commit()
        cands = sc._candidates(self.account_id, datetime.utcnow())
        self.assertEqual({c['id'] for c in cands if c['has_listings']}, {listed})
        self.assertEqual(len(cands), len(self.channel_ids))


class PageTests(_AppCase):

    def page(self):
        db.session.remove()
        resp = self.client.get(f'/accounts/{self.account_id}')
        self.assertEqual(resp.status_code, 200)
        return resp.get_data(as_text=True)

    def test_never_checked(self):
        html = self.page()
        self.assertNotIn('id="acct-stream-check"', html)
        self.assertIn('Never run', html)
        self.assertIn('data-act="stream-check"', html)

    def test_a_failed_check_names_each_channel_and_offers_another_try(self):
        results = [sc.result_entry(self.channel_ids[0], 'Ch 0', 'US| LOCALS',
                                   SimpleNamespace(id=1, error_detail='Server returned 401'),
                                   sc.OUT_REFUSED)] * 2
        self.check_row(status=STREAM_CHECK_DONE, verdict=sc.VERDICT_LOGIN_REFUSED,
                       finished_at=datetime.utcnow(), tested_count=2, played_count=0,
                       results=json.dumps(results))
        html = self.page()
        self.assertIn('id="acct-stream-check"', html)
        self.assertIn('notice-bad', html)
        self.assertIn('the provider refused the login', html)
        self.assertIn('Test other channels', html)
        self.assertIn('0 of 2 channels played', html)

    def test_a_passed_check_leaves_the_banner_after_a_day(self):
        self.check_row(status=STREAM_CHECK_DONE, verdict=sc.VERDICT_WORKS,
                       finished_at=datetime.utcnow() - timedelta(days=2),
                       tested_count=1, played_count=1, results='[]')
        html = self.page()
        self.assertNotIn('id="acct-stream-check"', html)
        self.assertIn('1 of 1 channel played', html)

    def test_waiting_says_why_and_when(self):
        self.check_row(status=STREAM_CHECK_WAITING, wait_reason='A recording is using it.',
                       retry_at=datetime.utcnow() + timedelta(minutes=10))
        html = self.page()
        self.assertIn('Stream check waiting', html)
        self.assertIn('A recording is using it.', html)

    def test_add_account_page_offers_the_opt_in_off(self):
        html = self.client.get('/accounts/new').get_data(as_text=True)
        self.assertIn('name="stream_check"', html)
        self.assertNotIn('name="stream_check"\n                 checked', html)


class RealTesterTests(_AppCase):
    """The real tester and this box's ffmpeg against the simulator: what a refused login, a
    stream that does not exist and one that plays actually look like on the way in."""

    def setUp(self):
        if not shutil.which('ffmpeg') or not shutil.which('ffprobe'):
            self.skipTest('ffmpeg and ffprobe are needed')
        super().setUp()
        from sim import serve_for_test
        from tests.support.sim_streams import library, stream_world
        self.world = stream_world()
        self.sim = serve_for_test(self, self.world, content=library())
        live = [r.channel.stream_id for r in self.world.panel_lineups['cove-tv'].rows
                if r.channel.kind == 'live']
        self.urls = self.sim.stream_urls('cove-tv', 'reeduser')
        self.live = live
        acc = db.session.get(Account, self.account_id)
        acc.url_normalization = 'disabled'
        acc.status = ACCOUNT_STATUS_OK
        db.session.commit()
        for p in (mock.patch.object(sc, 'TEST_SECONDS', 2),
                  mock.patch.object(sc, 'WAIT_SECONDS', 0),
                  mock.patch.object(sc, 'RETRY_DELAY_SECONDS', 0)):
            p.start()
            self.addCleanup(p.stop)

    def point_channels(self, make_url):
        from app.database import Channel
        for cid, sid in zip(self.channel_ids, self.live):
            ch = db.session.get(Channel, cid)
            ch.stream_url = ch.raw_stream_url = make_url(self.urls[sid], sid)
        db.session.commit()

    def run_one(self):
        cid = self.check_row()
        with mock.patch('app.scheduler.schedule_stream_check', return_value=True):
            sc.run_check(self.app, cid)
        return self.fetch(cid)

    def test_a_playing_stream(self):
        self.point_channels(lambda url, sid: url)
        check = self.run_one()
        self.assertEqual(check.verdict, sc.VERDICT_WORKS, check.result_list())
        self.assertEqual(check.played_count, 1)

    def test_a_wrong_password_is_a_refused_login(self):
        self.point_channels(lambda url, sid: url.replace('reedpass1', 'wrongpass'))
        check = self.run_one()
        self.assertEqual(check.verdict, sc.VERDICT_LOGIN_REFUSED, check.result_list())
        self.assertEqual(check.tested_count, 2)

    def test_streams_that_do_not_exist_point_at_the_urls(self):
        self.point_channels(lambda url, sid: url.replace(f'/{sid}', f'/9{sid}99'))
        check = self.run_one()
        self.assertEqual(check.verdict, sc.VERDICT_URLS_NOT_FOUND, check.result_list())


if __name__ == '__main__':
    unittest.main()
