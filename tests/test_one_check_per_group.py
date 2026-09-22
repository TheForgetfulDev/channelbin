"""Tier 2 - a group carries exactly one health check, and "records" is a fact about its
members (dev/changelog/1077).

Guards the model change that folded the health-check half of a channel group back into
the group:

  * **Migration 71** rewrites the retired `health_check_only` strategy to `highest_score`,
    mints a QUEUED check for every group without one, merges a group's extra checks into
    one (re-pointing their test history, never losing it), and puts the unique index on
    `on_demand_test_jobs.group_id`. Re-runnable from the top.
  * **Monitored means recurring, not paused, and SCHEDULED or RUNNING.** A run flips a
    scheduled check to RUNNING for its whole duration, and reading SCHEDULED alone reported
    every member of a recurring check as unmonitored for exactly as long as it was being
    tested (dev/docs/BUGS.md 2026-09-21).
  * **The unmonitored count is scoped to participating members** - a member with both
    switches off is sitting out on purpose and is not a drift risk.
  * **`participation_is_recording()` reads the members**, never a strategy value.
  * **Promoting with `enable='matching'` and no reference turns on nothing** - the inline
    spelling counted every untested member as matching a None reference and switched
    Recording on for exactly the members nothing had measured (dev/docs/BUGS.md
    2026-09-21).
  * **A clone copies its source's schedule and profile only when asked**, and says so.

Runs against a throwaway temp SQLite DB - never the live dvr.db.
  python3 -m unittest tests.test_one_check_per_group
"""
import os
import sqlite3
import sys
import tempfile
import unittest
from datetime import datetime, timedelta
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tests.support import make_test_app  # noqa: E402
from tests.support.seed import (  # noqa: E402
    make_account, make_channel, make_channel_test, make_group, set_check,
)
from app import db  # noqa: E402
from app import migrations as M  # noqa: E402
from app.channel_groups import (  # noqa: E402
    active_recurring_jobs, participation_is_recording, schedule_is_live,
)
from app.database import (  # noqa: E402
    ChannelGroup, ChannelGroupMember, OnDemandTestJob,
    GROUP_FORMAT_HIGHEST_SCORE, OD_JOB_STATUS_RUNNING, OD_JOB_STATUS_SCHEDULED,
)


class Migration71Tests(unittest.TestCase):
    """The step against the exact shape the dev database had on 2026-09-21: seven groups
    still on `health_check_only`, one group with no check, one with two."""

    def _db(self):
        td = tempfile.TemporaryDirectory()
        self.addCleanup(td.cleanup)
        conn = sqlite3.connect(os.path.join(td.name, 'scratch.db'))
        cur = conn.cursor()
        cur.execute('CREATE TABLE channel_groups (id INTEGER PRIMARY KEY, '
                    'name VARCHAR(255) NOT NULL, is_system BOOLEAN NOT NULL DEFAULT 0, '
                    "format_strategy VARCHAR(32) NOT NULL DEFAULT 'health_check_only')")
        cur.execute('CREATE TABLE on_demand_test_jobs (id INTEGER PRIMARY KEY, '
                    'name VARCHAR(512) NOT NULL, is_system BOOLEAN NOT NULL DEFAULT 0, '
                    'status VARCHAR(32) NOT NULL, created_at DATETIME, '
                    'recurring BOOLEAN NOT NULL DEFAULT 0, '
                    'recur_paused BOOLEAN NOT NULL DEFAULT 0, '
                    'recur_use_window BOOLEAN NOT NULL DEFAULT 0, '
                    'completed_at DATETIME, group_id INTEGER, scheduler_job_id VARCHAR(255))')
        cur.execute('CREATE TABLE channel_tests (id INTEGER PRIMARY KEY, job_id INTEGER)')
        cur.execute('CREATE TABLE apscheduler_jobs (id VARCHAR(191) PRIMARY KEY)')
        cur.executemany('INSERT INTO channel_groups (id, name, is_system, format_strategy) '
                        'VALUES (?, ?, ?, ?)', [
                            (1, 'TV Guide Channels', 1, 'health_check_only'),
                            (2, 'Fox', 0, 'highest_score'),
                            (3, 'No check', 0, 'health_check_only'),
                            (12, 'Two checks', 0, 'health_check_only'),
                            (13, 'Live wins', 0, 'manual'),
                        ])
        cur.executemany(
            'INSERT INTO on_demand_test_jobs (id, name, is_system, status, recurring, '
            'recur_paused, completed_at, group_id, scheduler_job_id) '
            'VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)', [
                (1, 'TV Guide Channels', 1, 'QUEUED', 0, 0, None, 1, None),
                (2, 'Fox', 0, 'SCHEDULED', 1, 0, '2026-09-21 11:28:57', 2, None),
                # Group 12: neither scheduled; 11 ran, 12 never did -> keep 11.
                (11, 'Two checks - health check', 0, 'QUEUED', 0, 0, '2026-09-21 14:54:20', 12, None),
                (12, 'Two checks - health check', 0, 'QUEUED', 0, 0, None, 12, None),
                # Group 13: 20 ran more recently, but 21 carries the live schedule.
                (20, 'Old one-off', 0, 'COMPLETED', 0, 0, '2026-09-20 00:00:00', 13, 'od_job_20'),
                (21, 'Nightly', 0, 'SCHEDULED', 1, 0, '2026-09-01 00:00:00', 13, 'od_job_21'),
            ])
        cur.executemany('INSERT INTO channel_tests (id, job_id) VALUES (?, ?)',
                        [(1, 11), (2, 12), (3, 12), (4, 20), (5, 21)])
        cur.executemany('INSERT INTO apscheduler_jobs (id) VALUES (?)',
                        [('od_job_20',), ('od_job_21',)])
        conn.commit()
        return conn, cur

    def test_the_step_leaves_every_group_with_exactly_one_check(self):
        conn, cur = self._db()
        M._m071_one_check_per_group(conn, cur)
        rows = cur.execute('SELECT group_id, COUNT(*) FROM on_demand_test_jobs '
                           'GROUP BY group_id ORDER BY group_id').fetchall()
        self.assertEqual([(1, 1), (2, 1), (3, 1), (12, 1), (13, 1)], rows)

    def test_the_retired_strategy_is_rewritten_to_the_default(self):
        conn, cur = self._db()
        M._m071_one_check_per_group(conn, cur)
        self.assertEqual(
            0, cur.execute("SELECT COUNT(*) FROM channel_groups "
                           "WHERE format_strategy='health_check_only'").fetchone()[0])
        self.assertEqual(
            'highest_score',
            cur.execute('SELECT format_strategy FROM channel_groups WHERE id=3').fetchone()[0])
        self.assertEqual(
            'manual',
            cur.execute('SELECT format_strategy FROM channel_groups WHERE id=13').fetchone()[0],
            'a real strategy is left alone')

    def test_a_group_with_no_check_gets_a_queued_one_named_after_it(self):
        conn, cur = self._db()
        M._m071_one_check_per_group(conn, cur)
        name, status, recurring = cur.execute(
            'SELECT name, status, recurring FROM on_demand_test_jobs WHERE group_id=3'
        ).fetchone()
        self.assertEqual(('No check - health check', 'QUEUED', 0), (name, status, recurring))

    def test_the_most_recently_completed_check_is_kept_and_history_merged(self):
        conn, cur = self._db()
        M._m071_one_check_per_group(conn, cur)
        self.assertEqual([(11,)], cur.execute(
            'SELECT id FROM on_demand_test_jobs WHERE group_id=12').fetchall())
        self.assertEqual(
            3, cur.execute('SELECT COUNT(*) FROM channel_tests WHERE job_id=11').fetchone()[0],
            "the loser's tests are re-pointed, never deleted")
        self.assertEqual(
            0, cur.execute('SELECT COUNT(*) FROM channel_tests WHERE job_id=12').fetchone()[0])

    def test_a_live_schedule_beats_a_more_recent_completion(self):
        conn, cur = self._db()
        M._m071_one_check_per_group(conn, cur)
        self.assertEqual([(21,)], cur.execute(
            'SELECT id FROM on_demand_test_jobs WHERE group_id=13').fetchall())
        self.assertEqual(
            2, cur.execute('SELECT COUNT(*) FROM channel_tests WHERE job_id=21').fetchone()[0])
        self.assertEqual([('od_job_21',)],
                         cur.execute('SELECT id FROM apscheduler_jobs ORDER BY id').fetchall(),
                         "the loser's scheduler entry goes with it; the keeper's stays")

    def test_the_unique_index_exists_and_a_rerun_is_a_no_op(self):
        conn, cur = self._db()
        M._m071_one_check_per_group(conn, cur)
        self.assertEqual(
            1, cur.execute("SELECT COUNT(*) FROM sqlite_master "
                           "WHERE name='uq_on_demand_test_jobs_group'").fetchone()[0])
        with self.assertRaises(sqlite3.IntegrityError):
            cur.execute("INSERT INTO on_demand_test_jobs (name, status, group_id) "
                        "VALUES ('Second', 'QUEUED', 2)")
        conn.rollback()
        before = cur.execute('SELECT id, group_id FROM on_demand_test_jobs ORDER BY id').fetchall()
        M._m071_one_check_per_group(conn, cur)
        after = cur.execute('SELECT id, group_id FROM on_demand_test_jobs ORDER BY id').fetchall()
        self.assertEqual(before, after)

    def test_it_is_registered_as_step_71(self):
        self.assertEqual(M._m071_one_check_per_group,
                         dict((v, fn) for v, _d, fn in M.SCHEMA_MIGRATIONS)[71])
        self.assertGreaterEqual(M.CURRENT_SCHEMA_VERSION, 71)


class _Base(unittest.TestCase):
    def setUp(self):
        self.t = make_test_app()
        self.t.app.config['WTF_CSRF_ENABLED'] = False
        self.client = self.t.client
        self.ctx = self.t.app.app_context()
        self.ctx.push()
        self.acct = make_account()

    def tearDown(self):
        self.ctx.pop()
        self.t.cleanup()


class MonitoredWhileRunningTests(_Base):
    """dev/docs/BUGS.md 2026-09-21: a recurring check's members read as unmonitored for
    the whole of every run, because the run flips the job to RUNNING and the two
    predicates read SCHEDULED alone."""

    def _recurring(self, status):
        # Two members, so the automatic check's scheduleless fallback (one probe per
        # group) cannot cover both by itself - only the group's own schedule does.
        ch = make_channel(self.acct, name='Feed')
        other = make_channel(self.acct, name='Other feed')
        grp = make_group(name='G', members=[ch, other], in_guide=False, recording=False)
        set_check(grp, status=status, recurring=True, recur_paused=False,
                  recur_day=0, recur_hour=3, recur_minute=0)
        db.session.commit()
        return grp, ch

    def test_a_running_recurring_check_is_still_live(self):
        grp, _ = self._recurring(OD_JOB_STATUS_RUNNING)
        self.assertTrue(schedule_is_live(grp.check))

    def test_a_running_recurring_check_still_counts_as_monitoring(self):
        from app.channel_tester import monitored_channel_ids
        grp, ch = self._recurring(OD_JOB_STATUS_RUNNING)
        self.assertIn(grp.check.id, [j.id for j in active_recurring_jobs()])
        self.assertTrue({m.channel_id for m in grp.memberships} <= monitored_channel_ids())

    def test_the_group_page_reports_nobody_unmonitored_mid_run(self):
        grp, _ = self._recurring(OD_JOB_STATUS_RUNNING)
        rows = self.client.get(f'/api/channel-groups/{grp.id}/detail-rows').get_json()
        self.assertEqual(0, rows['unmonitored_count'])
        self.assertEqual(0, rows['warnings']['unmonitored_count'])

    def test_a_paused_recurrence_is_still_not_live_whatever_its_status(self):
        for status in (OD_JOB_STATUS_SCHEDULED, OD_JOB_STATUS_RUNNING):
            with self.subTest(status=status):
                grp, _ = self._recurring(status)
                set_check(grp, recur_paused=True)
                db.session.commit()
                self.assertFalse(schedule_is_live(grp.check))
                self.assertNotIn(grp.check.id, [j.id for j in active_recurring_jobs()])
                db.session.delete(grp.check)
                db.session.delete(grp)
                db.session.commit()


class UnmonitoredCountScopeTests(_Base):
    """The drift-coverage count is over the PARTICIPATING members (either switch on): a
    member sitting out with both switches off is not a drift risk, and a group nobody
    records from still gets the warning through its tested members."""

    def _group(self):
        a = make_channel(self.acct, name='A')
        b = make_channel(self.acct, name='B')
        c = make_channel(self.acct, name='C')
        grp = make_group(name='G', members=[a, b, c], in_guide=False, recording=True,
                         disabled=[c.id], test_disabled=[c.id])
        db.session.commit()
        return grp

    def test_a_member_with_both_switches_off_is_not_counted(self):
        # A and B participate; the automatic check's scheduleless fallback already probes
        # the serving member (A), so exactly one participating member is unmonitored. The
        # all-members spelling would have counted C too and answered 2.
        grp = self._group()
        rows = self.client.get(f'/api/channel-groups/{grp.id}/detail-rows').get_json()
        self.assertEqual(1, rows['unmonitored_count'])
        self.assertEqual(1, rows['warnings']['unmonitored_count'])

    def test_the_list_page_counts_the_same_way(self):
        from app.routes.channel_groups import _group_view
        grp = self._group()
        view = _group_view(grp, monitored_ids=set(), memberships=list(grp.memberships))
        self.assertEqual(2, view['unmonitored_count'])


class RecordingSourceIsAFactAboutMembersTests(_Base):
    def test_a_group_with_a_recording_member_records_whatever_its_strategy(self):
        ch = make_channel(self.acct, name='Feed')
        grp = make_group(name='G', members=[ch], recording=True,
                         format_strategy=GROUP_FORMAT_HIGHEST_SCORE)
        self.assertTrue(participation_is_recording(grp))
        m = ChannelGroupMember.query.filter_by(group_id=grp.id).one()
        m.recording_enabled = False  # participation-write-ok: test fixture setup
        db.session.commit()
        self.assertFalse(participation_is_recording(grp))
        self.assertFalse(participation_is_recording(grp, [m]))

    def test_the_search_row_check_only_flag_reads_the_same_fact(self):
        """channel_search_rows.py's `check_only` used to read the strategy value."""
        ch = make_channel(self.acct, name='Only feed')
        grp = make_group(name='Nightly checks', members=[ch], recording=False, in_guide=False,
                         format_strategy=GROUP_FORMAT_HIGHEST_SCORE)
        db.session.commit()
        resp = self.client.get('/api/channels/search?q=nightly')
        self.assertEqual(200, resp.status_code, resp.get_json())
        rows = [r for r in resp.get_json()['rows'] if r['kind'] == 'group']
        self.assertEqual(1, len(rows))
        self.assertTrue(rows[0]['check_only'])
        self.assertEqual(GROUP_FORMAT_HIGHEST_SCORE, grp.format_strategy)

class PromoteMatchingWithNoReferenceTests(_Base):
    """dev/docs/BUGS.md 2026-09-21: `enable='matching'` against a None reference used to
    switch Recording on for exactly the untested members."""

    def test_no_reference_turns_on_nothing(self):
        tested = make_channel(self.acct, name='Tested')
        untested = make_channel(self.acct, name='Untested')
        # A test that measured nothing: no resolution, so no format and no reference.
        make_channel_test(tested, status='FAILED')
        grp = make_group(name='G', members=[tested, untested], in_guide=False,
                         recording=False)
        db.session.commit()
        resp = self.client.post(f'/api/channel-groups/{grp.id}/promote',
                                json={'strategy': 'unmanaged', 'enable': 'matching',
                                      'unmatched_checks': 'keep', 'add_to_guide': False})
        self.assertEqual(200, resp.status_code, resp.get_json())
        self.assertEqual(0, resp.get_json()['enabled'])
        db.session.expire_all()
        self.assertFalse(any(m.recording_enabled
                             for m in ChannelGroupMember.query.filter_by(group_id=grp.id)))

    def test_matching_is_derived_from_the_outliers_helper(self):
        hd1 = make_channel(self.acct, name='HD 1')
        hd2 = make_channel(self.acct, name='HD 2')
        sd = make_channel(self.acct, name='SD')
        untested = make_channel(self.acct, name='Untested')
        for ch, res in ((hd1, '1920x1080'), (hd2, '1920x1080'), (sd, '1280x720')):
            make_channel_test(ch, all_null=False, status='COMPLETED', connected=True,
                              resolution=res, fps=60.0, bitrate_kbps=5000)
        grp = make_group(name='G', members=[hd1, hd2, sd, untested], in_guide=False,
                         recording=False)
        db.session.commit()
        resp = self.client.post(f'/api/channel-groups/{grp.id}/promote',
                                json={'strategy': 'most_channels', 'enable': 'matching',
                                      'unmatched_checks': 'keep', 'add_to_guide': False})
        self.assertEqual(200, resp.status_code, resp.get_json())
        db.session.expire_all()
        on = {m.channel_id for m in ChannelGroupMember.query.filter_by(group_id=grp.id)
              if m.recording_enabled}
        self.assertEqual({hd1.id, hd2.id}, on,
                         'the two measured matches, never the outlier and never the untested')


class CloneCopiesTheScheduleWhenAskedTests(_Base):
    def _source(self):
        from app.database import HealthCheckProfile
        profile = HealthCheckProfile(name='Quick', test_duration_seconds=15)
        db.session.add(profile)
        ch = make_channel(self.acct, name='Feed')
        src = make_group(name='Source', members=[ch], in_guide=False)
        set_check(src, status=OD_JOB_STATUS_SCHEDULED, recurring=True, recur_day=0,
                  recur_use_window=True, profile_id=profile.id)
        db.session.commit()
        return src, ch, profile

    def test_the_clone_has_its_own_unscheduled_check_by_default(self):
        src, ch, _ = self._source()
        resp = self.client.post(f'/api/channel-groups/{src.id}/clone',
                                json={'name': 'Copy', 'channel_ids': [ch.id]})
        self.assertEqual(200, resp.status_code, resp.get_json())
        self.assertFalse(resp.get_json()['schedule_copied'])
        copy = ChannelGroup.query.filter_by(name='Copy').one()
        self.assertIsNotNone(copy.check)
        self.assertEqual('QUEUED', copy.check.status)
        self.assertIsNone(copy.check.profile_id)

    def test_copy_schedule_carries_the_recurrence_and_profile_over(self):
        src, ch, profile = self._source()
        run_at = datetime.utcnow() + timedelta(days=1)
        with patch('app.scheduler.schedule_on_demand_job', return_value=(None, run_at)):
            resp = self.client.post(f'/api/channel-groups/{src.id}/clone',
                                    json={'name': 'Copy', 'channel_ids': [ch.id],
                                          'copy_schedule': True})
        self.assertEqual(200, resp.status_code, resp.get_json())
        self.assertTrue(resp.get_json()['schedule_copied'])
        self.assertTrue(resp.get_json()['profile_copied'])
        copy = ChannelGroup.query.filter_by(name='Copy').one()
        self.assertEqual(OD_JOB_STATUS_SCHEDULED, copy.check.status)
        self.assertTrue(copy.check.recurring)
        self.assertTrue(copy.check.recur_use_window)
        self.assertEqual(profile.id, copy.check.profile_id)
        self.assertEqual(OnDemandTestJob.query.filter_by(group_id=copy.id).count(), 1)


if __name__ == '__main__':
    unittest.main(verbosity=2)
