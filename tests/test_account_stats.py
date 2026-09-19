"""The per-account usage ledger and its readers (app/account_stats.py, dev/changelog/1028).

Guards dev/docs/BUGS.md 2026-09-18 03:41 PM - the account page credited a whole recording's
window to the account of the channel it ENDED on, so under group failover one account was
given hours another account captured.

The ledger half: each segment's wall clock is credited to the account of the channel that
captured it; nothing unfinished is folded until it finishes; a fold is idempotent; a
deleted source row's contribution stays; days are local; a timezone change rebuilds.
"""
import logging
import os
import sys
import unittest
from datetime import date, datetime, timedelta
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import account_stats, admission, db  # noqa: E402
from app.config import load_config as _real_load_config  # noqa: E402
from app.database import (AccountStatDay, AccountStatState, ChannelEvent, ChannelTest,  # noqa: E402
                          CHANNEL_ADDED_TO_GUIDE,
                          RecordingSegment, CHANNEL_FAILOVER_HEALTH_OBSERVATION,
                          CHANNEL_PLACEHOLDER_HEALTH_OBSERVATION)
from app.fmt_utils import fmt_duration  # noqa: E402
from tests.support import make_test_app  # noqa: E402
from tests.support import seed  # noqa: E402

# A fixed moment well away from any DST change: 2026-09-10 15:00 UTC = 11:00 in New York.
T0 = datetime(2026, 9, 10, 15, 0, 0)


def _tz_config(tz_name):
    """A load_config stand-in that reports `tz_name` as the display timezone. Patched onto
    app.config.load_config, which tz_utils imports inside each call."""
    def _load(*args, **kwargs):
        cfg = _real_load_config(*args, **kwargs)
        cfg.setdefault('display', {})['timezone'] = tz_name
        return cfg
    return _load


class _LedgerCase(unittest.TestCase):
    TZ = 'America/New_York'

    def setUp(self):
        self.t = make_test_app()
        self.t.app.config['WTF_CSRF_ENABLED'] = False
        patcher = mock.patch('app.config.load_config', _tz_config(self.TZ))
        patcher.start()
        self.addCleanup(patcher.stop)
        self.a = seed.make_account(name='Provider A')
        self.b = seed.make_account(name='Provider B')
        self.ch_a = seed.make_channel(self.a, name='A1')
        self.ch_b = seed.make_channel(self.b, name='B1')
        db.session.commit()

    def tearDown(self):
        account_stats.wait_for_catch_up(10)
        self.t.cleanup()

    def day(self, account, day):
        db.session.expire_all()
        return AccountStatDay.query.filter_by(account_id=account.id, day=day).first()

    def totals(self, account, window='all', today=date(2026, 9, 12)):
        db.session.expire_all()
        return account_stats.windowed_stats([account.id], window, today)[account.id]


class FoldCorrectnessTests(_LedgerCase):

    def test_failover_credits_each_account_only_its_own_segments(self):
        """BUGS.md 2026-09-18 03:41 PM: a recording that failed over from A to B credits A
        its hour and B its half hour - not the whole window to B, where it ended."""
        rec = seed.make_recording(status='COMPLETED', channel_id=self.ch_b.id,
                                  start_time=T0, stop_time=T0 + timedelta(hours=7))
        seed.make_segment(rec, self.ch_a, T0, T0 + timedelta(hours=1), 0, stall_count=2)
        seed.make_segment(rec, self.ch_b, T0 + timedelta(hours=1),
                          T0 + timedelta(hours=1, minutes=30), 1)
        db.session.commit()
        account_stats.refresh_ledger()
        a, b = self.totals(self.a), self.totals(self.b)
        self.assertEqual(a['capture_seconds'], 3600)
        self.assertEqual(b['capture_seconds'], 1800)
        self.assertEqual((a['recordings'], b['recordings']), (1, 1))
        self.assertEqual((a['stalls'], b['stalls']), (2, 0))

    def test_several_segments_on_one_account_count_one_recording(self):
        rec = seed.make_recording(status='COMPLETED', channel_id=self.ch_a.id)
        for i in range(3):
            seed.make_segment(rec, self.ch_a, T0 + timedelta(minutes=10 * i),
                              T0 + timedelta(minutes=10 * i + 5), i)
        db.session.commit()
        account_stats.refresh_ledger()
        self.assertEqual(self.totals(self.a)['recordings'], 1)
        self.assertEqual(self.totals(self.a)['segments'], 3)
        self.assertEqual(self.totals(self.a)['capture_seconds'], 900)

    def test_excluded_segment_adds_no_capture_and_no_recording(self):
        """A placeholder clip kept out of the file is not capture, but its stall was real."""
        rec = seed.make_recording(status='COMPLETED', channel_id=self.ch_a.id)
        seed.make_segment(rec, self.ch_a, T0, T0 + timedelta(minutes=10), 0,
                          stall_count=1, excluded_reason='PROVIDER_PLACEHOLDER')
        db.session.commit()
        account_stats.refresh_ledger()
        a = self.totals(self.a)
        self.assertEqual(a['capture_seconds'], 0)
        self.assertEqual(a['recordings'], 0)
        self.assertEqual(a['stalls'], 1)
        self.assertEqual(a['segments'], 1)

    def test_excluded_first_segment_does_not_hide_the_recording(self):
        """The recording is credited on its first JOINED segment on the account."""
        rec = seed.make_recording(status='COMPLETED', channel_id=self.ch_a.id)
        seed.make_segment(rec, self.ch_a, T0, T0 + timedelta(minutes=1), 0,
                          excluded_reason='PROVIDER_PLACEHOLDER')
        seed.make_segment(rec, self.ch_a, T0 + timedelta(minutes=1),
                          T0 + timedelta(minutes=11), 1)
        db.session.commit()
        account_stats.refresh_ledger()
        self.assertEqual(self.totals(self.a)['recordings'], 1)
        self.assertEqual(self.totals(self.a)['capture_seconds'], 600)

    def test_checks_count_by_status_and_cancelled_counts_nowhere(self):
        for status in ('COMPLETED', 'COMPLETED', 'FAILED', 'CANCELLED'):
            seed.make_channel_test(self.ch_a, status=status, test_started_at=T0,
                                   test_ended_at=T0 + timedelta(seconds=30))
        db.session.commit()
        account_stats.refresh_ledger()
        a = self.totals(self.a)
        self.assertEqual((a['checks_passed'], a['checks_failed']), (2, 1))
        self.assertAlmostEqual(a['pass_rate'], 2 / 3)

    def test_no_checks_is_no_pass_rate_not_zero(self):
        account_stats.refresh_ledger()
        self.assertIsNone(self.totals(self.a)['pass_rate'])

    def test_failover_events_count_and_other_events_do_not(self):
        for event_type in (CHANNEL_FAILOVER_HEALTH_OBSERVATION,
                           CHANNEL_PLACEHOLDER_HEALTH_OBSERVATION, CHANNEL_ADDED_TO_GUIDE):
            db.session.add(ChannelEvent(channel_id=self.ch_a.id, timestamp=T0,
                                        event_type=event_type))
        db.session.commit()
        account_stats.refresh_ledger()
        self.assertEqual(self.totals(self.a)['failovers_away'], 2)
        self.assertEqual(self.totals(self.b)['failovers_away'], 0)

    def test_running_segment_holds_the_watermark_until_it_finishes(self):
        """A lower-id segment still capturing blocks a finished later one, so neither is
        folded with numbers that could still change - and both land once it ends."""
        rec1 = seed.make_recording(status='IN_PROGRESS', channel_id=self.ch_a.id)
        running = seed.make_segment(rec1, self.ch_a, T0, None, 0)
        rec2 = seed.make_recording(status='COMPLETED', channel_id=self.ch_b.id)
        seed.make_segment(rec2, self.ch_b, T0, T0 + timedelta(minutes=5), 0)
        db.session.commit()
        account_stats.refresh_ledger()
        self.assertEqual(self.totals(self.b)['capture_seconds'], 0)
        self.assertEqual(db.session.get(AccountStatState, 1).segment_watermark, running.id - 1)

        seg = db.session.get(RecordingSegment, running.id)
        seg.ended_at = T0 + timedelta(minutes=20)
        db.session.commit()
        account_stats.refresh_ledger()
        self.assertEqual(self.totals(self.a)['capture_seconds'], 1200)
        self.assertEqual(self.totals(self.b)['capture_seconds'], 300)

    def test_unfinished_test_holds_the_watermark_until_it_finishes(self):
        running = seed.make_channel_test(self.ch_a, status='FAILED', test_started_at=T0,
                                         test_ended_at=None)
        seed.make_channel_test(self.ch_b, status='COMPLETED', test_started_at=T0)
        db.session.commit()
        account_stats.refresh_ledger()
        self.assertEqual(self.totals(self.b)['checks_passed'], 0)

        test = db.session.get(ChannelTest, running.id)
        test.status = 'COMPLETED'
        test.test_ended_at = T0 + timedelta(seconds=30)
        db.session.commit()
        account_stats.refresh_ledger()
        # The FAILED default a running test carries was never counted.
        self.assertEqual((self.totals(self.a)['checks_passed'],
                          self.totals(self.a)['checks_failed']), (1, 0))
        self.assertEqual(self.totals(self.b)['checks_passed'], 1)

    def test_second_refresh_changes_nothing(self):
        rec = seed.make_recording(status='COMPLETED', channel_id=self.ch_a.id)
        seed.make_segment(rec, self.ch_a, T0, T0 + timedelta(hours=1), 0)
        seed.make_channel_test(self.ch_a, status='COMPLETED', test_started_at=T0)
        db.session.commit()
        account_stats.refresh_ledger()
        before = self.totals(self.a)
        self.assertEqual(account_stats.refresh_ledger(), 0)
        self.assertEqual(self.totals(self.a), before)

    def test_deleting_source_rows_after_a_fold_leaves_the_ledger(self):
        """The honesty property: pruned tests and deleted recordings do not shrink history."""
        rec = seed.make_recording(status='COMPLETED', channel_id=self.ch_a.id)
        seed.make_segment(rec, self.ch_a, T0, T0 + timedelta(hours=1), 0)
        seed.make_channel_test(self.ch_a, status='FAILED', test_started_at=T0)
        db.session.commit()
        account_stats.refresh_ledger()
        RecordingSegment.query.delete()
        ChannelTest.query.delete()
        db.session.commit()
        account_stats.refresh_ledger()
        a = self.totals(self.a)
        self.assertEqual((a['capture_seconds'], a['checks_failed']), (3600, 1))

    def test_rows_on_a_deleted_channel_are_passed_without_credit(self):
        rec = seed.make_recording(status='COMPLETED')
        seg = seed.make_segment(rec, None, T0, T0 + timedelta(hours=1), 0)
        db.session.commit()
        account_stats.refresh_ledger()
        self.assertEqual(AccountStatDay.query.count(), 0)
        self.assertEqual(db.session.get(AccountStatState, 1).segment_watermark, seg.id)

    def test_folds_in_chunks(self):
        """A backlog larger than one chunk is folded completely, one commit per chunk."""
        rec = seed.make_recording(status='COMPLETED', channel_id=self.ch_a.id)
        for i in range(7):
            seed.make_segment(rec, self.ch_a, T0 + timedelta(minutes=i),
                              T0 + timedelta(minutes=i + 1), i)
        db.session.commit()
        with mock.patch.object(account_stats, 'FOLD_CHUNK', 3):
            account_stats.refresh_ledger()
        self.assertEqual(self.totals(self.a)['segments'], 7)
        self.assertEqual(self.totals(self.a)['recordings'], 1)


class LocalDayTests(_LedgerCase):

    def test_early_utc_morning_lands_on_the_previous_local_day(self):
        early = datetime(2026, 9, 10, 3, 30)
        seed.make_channel_test(self.ch_a, status='COMPLETED', test_started_at=early)
        db.session.commit()
        account_stats.refresh_ledger()
        self.assertIsNotNone(self.day(self.a, '2026-09-09'))
        self.assertIsNone(self.day(self.a, '2026-09-10'))

    def test_timezone_change_rebuilds_and_rebuckets(self):
        early = datetime(2026, 9, 10, 3, 30)
        seed.make_channel_test(self.ch_a, status='COMPLETED', test_started_at=early)
        db.session.commit()
        account_stats.refresh_ledger()
        with mock.patch('app.config.load_config', _tz_config('UTC')):
            with self.assertLogs('app.account_stats', logging.WARNING) as logs:
                account_stats.refresh_ledger()
        self.assertIn('display timezone changed', '\n'.join(logs.output))
        self.assertIsNone(self.day(self.a, '2026-09-09'))
        self.assertEqual(self.day(self.a, '2026-09-10').checks_passed, 1)
        state = db.session.get(AccountStatState, 1)
        self.assertEqual(state.tz_name, 'UTC')
        self.assertIsNotNone(state.rebuilt_at)

    def test_interrupted_rebuild_is_resumed_out_loud(self):
        account_stats.refresh_ledger()
        state = db.session.get(AccountStatState, 1)
        state.rebuild_started_at = datetime.utcnow()
        state.rebuilt_at = None
        db.session.commit()
        with self.assertLogs('app.account_stats', logging.WARNING) as logs:
            account_stats.refresh_ledger()
        self.assertIn('Resuming an interrupted', '\n'.join(logs.output))
        db.session.expire_all()
        self.assertIsNotNone(db.session.get(AccountStatState, 1).rebuilt_at)


class WindowAndTrendTests(_LedgerCase):

    def _day(self, account, day, **counts):
        row = AccountStatDay(account_id=account.id, day=day, capture_seconds=0, segments=0,
                             recordings=0, stalls=0, checks_passed=0, checks_failed=0,
                             failovers_away=0)
        for key, value in counts.items():
            setattr(row, key, value)
        db.session.add(row)

    def test_windows_count_local_days_back_including_today(self):
        today = date(2026, 9, 12)
        self._day(self.a, '2026-09-06', checks_passed=1)    # today - 6: inside 7d
        self._day(self.a, '2026-09-05', checks_passed=10)   # today - 7: outside 7d
        self._day(self.a, '2026-06-01', checks_passed=100)  # outside 90d
        db.session.commit()
        got = {w: self.totals(self.a, w, today)['checks_passed'] for w in account_stats.WINDOWS}
        self.assertEqual(got, {'7d': 1, '30d': 11, '90d': 11, 'all': 111})

    def test_unknown_window_is_an_error_not_a_fallback(self):
        with self.assertRaises(ValueError):
            account_stats.window_bounds('14d', date(2026, 9, 12))

    def test_seven_days_is_daily(self):
        trend = account_stats.trend([self.a.id], '7d', date(2026, 9, 12))
        self.assertEqual(trend['unit'], 'day')
        self.assertEqual(len(trend['buckets']), 7)
        self.assertEqual(trend['buckets'][-1]['start'], date(2026, 9, 12))

    def test_weeks_run_sunday_to_saturday(self):
        # 2026-09-12 is a Saturday, 2026-09-13 a Sunday.
        self._day(self.a, '2026-09-12', checks_passed=1)
        self._day(self.a, '2026-09-13', checks_passed=2)
        db.session.commit()
        trend = account_stats.trend([self.a.id], '30d', date(2026, 9, 14))
        self.assertEqual(trend['unit'], 'week')
        starts = [b['start'] for b in trend['buckets']]
        self.assertTrue(all(s.weekday() == 6 for s in starts))
        last_two = [b['values'][self.a.id]['checks_passed'] for b in trend['buckets'][-2:]]
        self.assertEqual(last_two, [1, 2])

    def test_all_time_steps_up_to_months_past_26_weeks(self):
        self._day(self.a, '2025-01-15', capture_seconds=60)
        self._day(self.a, '2026-09-01', capture_seconds=30)
        db.session.commit()
        trend = account_stats.trend([self.a.id, self.b.id], 'all', date(2026, 9, 12))
        self.assertEqual(trend['unit'], 'month')
        self.assertLessEqual(len(trend['buckets']), account_stats.MAX_TREND_BUCKETS)
        self.assertEqual(trend['buckets'][0]['start'], date(2025, 1, 1))
        self.assertEqual(trend['buckets'][0]['values'][self.a.id]['capture_seconds'], 60)
        self.assertEqual(trend['buckets'][-1]['values'][self.a.id]['capture_seconds'], 30)
        self.assertEqual(trend['buckets'][-1]['values'][self.b.id]['capture_seconds'], 0)

    def test_all_time_uses_quarters_then_years(self):
        self._day(self.a, '2023-02-01', checks_passed=1)
        db.session.commit()
        self.assertEqual(account_stats.trend([self.a.id], 'all', date(2026, 9, 12))['unit'],
                         'quarter')
        self._day(self.a, '2018-02-01', checks_passed=1)
        db.session.commit()
        self.assertEqual(account_stats.trend([self.a.id], 'all', date(2026, 9, 12))['unit'],
                         'year')

    def test_all_time_with_no_history_has_no_buckets(self):
        self.assertEqual(account_stats.trend([self.a.id], 'all', date(2026, 9, 12))['buckets'],
                         [])


class CurrentStatsTests(_LedgerCase):

    def test_bands_failing_group_and_guide_numbers(self):
        cfg = _real_load_config()
        cfg.setdefault('channel_testing', {}).update(failing_band='poor',
                                                     failing_streak_threshold=3)
        good = seed.make_channel(self.a, name='good', health_score=95.0)
        poor = seed.make_channel(self.a, name='poor', health_score=20.0)
        # Scores great, but is on a losing streak - failing by the streak rule alone.
        seed.make_channel(self.a, name='streak', health_score=90.0, consecutive_test_failures=4)
        seed.make_channel(self.a, name='hidden', health_score=10.0, hidden=True)
        db.session.flush()
        seed.make_group(name='G', members=[good, poor, self.ch_b], in_guide=True)
        db.session.commit()
        stats = account_stats.current_stats([self.a.id, self.b.id], cfg)
        a = stats[self.a.id]
        self.assertEqual(a['tested'], 3)
        self.assertEqual(a['bands']['great'], 2)
        self.assertEqual(a['bands']['poor'], 1)
        self.assertEqual(a['failing'], 2)
        self.assertAlmostEqual(a['avg_score'], (95 + 20 + 90) / 3)
        self.assertEqual(a['in_group'], 2)
        self.assertEqual(stats[self.b.id]['in_group'], 1)
        self.assertIsNone(stats[self.b.id]['avg_score'])
        self.assertEqual(a['guide_rows_total'], 1)
        self.assertEqual((a['guide_rows_fed'], stats[self.b.id]['guide_rows_fed']), (1, 1))

    def test_membership_counts_driven_from_the_membership_table(self):
        """current_stats counts memberships from channel_group_members, looking each
        member's account up (dev/changelog/1029). A channel in two groups is ONE channel in
        a group but TWO recording-on memberships, and a group row counts as fed only through
        a member with Recording on."""
        cfg = _real_load_config()
        twice = seed.make_channel(self.a, name='twice')
        db.session.flush()
        seed.make_group(name='G1', members=[twice], in_guide=True)
        seed.make_group(name='G2', members=[twice], in_guide=True)
        # Account B's only member has Recording off, so B feeds no group row.
        seed.make_group(name='G3', members=[self.ch_b], in_guide=True, recording=False)
        db.session.commit()
        stats = account_stats.current_stats([self.a.id, self.b.id], cfg)
        a, b = stats[self.a.id], stats[self.b.id]
        self.assertEqual((a['in_group'], a['recording_memberships']), (1, 2))
        self.assertEqual((b['in_group'], b['recording_memberships']), (1, 0))
        self.assertEqual((a['guide_rows_fed'], b['guide_rows_fed']), (2, 0))
        self.assertEqual(a['guide_rows_total'], 3)


class ReaderQueryCountTests(unittest.TestCase):
    """Every reader answers for a list of accounts in a fixed number of queries - the
    Accounts list will call them for every account at once (CLAUDE.md, no hidden I/O in
    per-row loops). The failing count is the one most likely to tempt a per-account loop."""

    def _count(self, n_accounts):
        from tests.support.iocount import IOCounter, all_engines
        t = make_test_app()
        try:
            ids = []
            for i in range(n_accounts):
                acc = seed.make_account(name=f'Acct {i}')
                ch = seed.make_channel(acc, name=f'C{i}', health_score=40.0 + i,
                                       consecutive_test_failures=i % 4)
                seed.make_group(name=f'G{i}', members=[ch], in_guide=True)
                rec = seed.make_recording(status='COMPLETED', channel_id=ch.id)
                seed.make_segment(rec, ch, T0 - timedelta(days=i), T0 - timedelta(days=i)
                                  + timedelta(minutes=30))
                ids.append(acc.id)
            db.session.commit()
            account_stats.refresh_ledger()
            cfg = _real_load_config()
            with IOCounter(all_engines()) as counter:
                account_stats.current_stats(ids, cfg)
                for window in account_stats.WINDOWS:
                    account_stats.windowed_stats(ids, window, date(2026, 9, 12))
                    account_stats.trend(ids, window, date(2026, 9, 12))
            return counter.queries
        finally:
            t.cleanup()

    def test_query_count_does_not_grow_with_accounts(self):
        self.assertEqual(self._count(3), self._count(30))


class CatchUpTests(_LedgerCase):

    def _backlog(self):
        rec = seed.make_recording(status='COMPLETED', channel_id=self.ch_a.id)
        seed.make_segment(rec, self.ch_a, T0, T0 + timedelta(hours=1), 0)
        db.session.commit()

    def test_small_backlog_folds_inline(self):
        self._backlog()
        self.assertIsNone(account_stats.ensure_fresh(self.t.app))
        self.assertEqual(self.totals(self.a)['capture_seconds'], 3600)

    def test_large_backlog_folds_in_the_background_and_releases_its_ticket(self):
        self._backlog()
        with mock.patch.object(account_stats, 'INLINE_FOLD_LIMIT', 0):
            notice = account_stats.ensure_fresh(self.t.app)
            self.assertIn('being brought up to date', notice['text'])
            account_stats.wait_for_catch_up(10)
        self.assertEqual(self.totals(self.a)['capture_seconds'], 3600)
        self.assertNotIn(admission.KIND_LEDGER, admission.active_kinds())

    def test_refused_catch_up_says_what_refused_it(self):
        self._backlog()
        ticket = admission.try_start(admission.KIND_SYNC, 'Provider A')
        self.addCleanup(admission.release, ticket)
        with mock.patch.object(account_stats, 'INLINE_FOLD_LIMIT', 0):
            notice = account_stats.ensure_fresh(self.t.app)
        self.assertIn('an account sync is already running', notice['text'])
        self.assertEqual(self.totals(self.a)['capture_seconds'], 0)


class AccountPageTests(_LedgerCase):

    def test_content_card_shows_captured_time_not_the_recording_window(self):
        """BUGS.md 2026-09-18 03:41 PM: a 7h recording that ended on account B but captured
        2h on account A shows 2h on A's page - it used to show nothing there and 7h on B's."""
        rec = seed.make_recording(status='COMPLETED', channel_id=self.ch_b.id,
                                  start_time=T0, stop_time=T0 + timedelta(hours=7))
        seed.make_segment(rec, self.ch_a, T0, T0 + timedelta(hours=2), 0)
        seed.make_segment(rec, self.ch_b, T0 + timedelta(hours=2),
                          T0 + timedelta(hours=2, minutes=30), 1)
        db.session.commit()
        page_a = self.t.client.get(f'/accounts/{self.a.id}').get_data(as_text=True)
        page_b = self.t.client.get(f'/accounts/{self.b.id}').get_data(as_text=True)
        self.assertIn(fmt_duration(7200), page_a)
        self.assertIn(fmt_duration(1800), page_b)
        self.assertNotIn(fmt_duration(7 * 3600), page_b)

    def test_account_delete_removes_its_ledger_rows(self):
        rec = seed.make_recording(status='COMPLETED', channel_id=self.ch_a.id)
        seed.make_segment(rec, self.ch_a, T0, T0 + timedelta(hours=1), 0)
        db.session.commit()
        account_stats.refresh_ledger()
        self.assertEqual(AccountStatDay.query.filter_by(account_id=self.a.id).count(), 1)
        resp = self.t.client.delete(f'/api/accounts/{self.a.id}')
        self.assertEqual(resp.status_code, 200, resp.get_data(as_text=True))
        db.session.expire_all()
        self.assertEqual(AccountStatDay.query.filter_by(account_id=self.a.id).count(), 0)


if __name__ == '__main__':
    unittest.main()
