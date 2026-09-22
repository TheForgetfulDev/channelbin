"""Tier 2 - GET /api/channel-groups/<id>/format-plan (app/routes/channel_groups.py::
group_format_plan), the read-only auto-select-format endpoint. Planned
2026-08-06, shipped in dev/changelog/494.

Runs against a throwaway temp SQLite DB - never the live dvr.db.
  python3 -m unittest tests.test_format_plan_routes
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tests.support import make_test_app  # noqa: E402
from tests.support.seed import (  # noqa: E402
    make_account, make_channel, make_group, make_channel_test, make_test_job,
)
from app import db  # noqa: E402


class FormatPlanRouteTests(unittest.TestCase):
    def setUp(self):
        self.t = make_test_app()
        self.acct = make_account()

    def tearDown(self):
        self.t.cleanup()

    def test_404_for_missing_group(self):
        resp = self.t.client.get('/api/channel-groups/999999/format-plan')
        self.assertEqual(resp.status_code, 404)
        self.assertIn('error', resp.get_json())

    def test_a_groups_members_resolve_and_pick_a_format(self):
        hd1 = make_channel(self.acct, name='HD 1')
        hd2 = make_channel(self.acct, name='HD 2')
        sd1 = make_channel(self.acct, name='SD 1')
        make_channel_test(hd1, status='COMPLETED', resolution='1920x1080', fps=60,
                          bitrate_kbps=3740)
        make_channel_test(hd2, status='COMPLETED', resolution='1920x1080', fps=60,
                          bitrate_kbps=3800)
        make_channel_test(sd1, status='COMPLETED', resolution='1280x720', fps=30,
                          bitrate_kbps=3600)
        grp = make_group(members=[hd1, hd2, sd1])
        db.session.commit()

        resp = self.t.client.get(f'/api/channel-groups/{grp.id}/format-plan')
        self.assertEqual(resp.status_code, 200)
        data = resp.get_json()
        self.assertTrue(data['success'])
        self.assertEqual(data['total'], 3)
        self.assertEqual(data['eligible_count'], 3)
        self.assertEqual(data['excluded_count'], 0)
        self.assertEqual(len(data['buckets']), 2)
        self.assertEqual(set(data['strategies']), {'highest_bitrate', 'highest_resolution',
                                                    'most_channels', 'balanced'})
        # 2 HD channels beat 1 SD channel on every strategy here.
        for strategy in data['strategies']:
            entry = data['strategies'][strategy]
            self.assertEqual(entry['resolution'], '1920x1080')
            self.assertEqual(entry['fps'], 60)
            self.assertEqual(entry['count'], 2)
            self.assertIn(hd1.id, entry['channel_ids'])
            self.assertIn(hd2.id, entry['channel_ids'])

    def test_unhealthy_and_untested_channels_are_excluded_but_counted(self):
        hd1 = make_channel(self.acct, name='HD 1')
        failed = make_channel(self.acct, name='Failed')
        untested = make_channel(self.acct, name='Untested')
        make_channel_test(hd1, status='COMPLETED', resolution='1920x1080', fps=60,
                          bitrate_kbps=3000)
        make_channel_test(failed, status='FAILED')
        grp = make_group(members=[hd1, failed, untested])
        db.session.commit()

        resp = self.t.client.get(f'/api/channel-groups/{grp.id}/format-plan')
        data = resp.get_json()
        self.assertEqual(data['total'], 3)
        self.assertEqual(data['eligible_count'], 1)
        self.assertEqual(data['excluded_count'], 2)

    def test_no_eligible_format_returns_observable_rationale_not_500(self):
        untested = make_channel(self.acct, name='Untested')
        grp = make_group(members=[untested])
        db.session.commit()

        resp = self.t.client.get(f'/api/channel-groups/{grp.id}/format-plan')
        self.assertEqual(resp.status_code, 200)
        data = resp.get_json()
        for strategy in data['strategies']:
            entry = data['strategies'][strategy]
            self.assertIsNone(entry['key'])
            self.assertTrue(entry['rationale'])

    def test_a_job_id_no_longer_scopes_the_plan(self):
        """dev/docs/BUGS.md 2026-09-09 07:02. The preview must read what the engine reads.

        `job_id` used to narrow the plan to one health check's results while
        apply_format_strategy() ranked over each channel's newest test from any job, so
        the picker and the stored lock could name different formats indefinitely. The
        parameter is now ignored: whatever is passed, the newest test wins.
        """
        ch = make_channel(self.acct, name='Ch')
        grp = make_group(members=[ch])
        job_a = make_test_job(name='Job A', channels=[ch])
        job_b = make_test_job(name='Job B', channels=[ch])
        db.session.commit()
        # Same channel, two different jobs, two different formats. Job B is newer.
        make_channel_test(ch, status='COMPLETED', resolution='1920x1080', fps=60,
                          job_id=job_a.id)
        make_channel_test(ch, status='COMPLETED', resolution='1280x720', fps=30,
                          job_id=job_b.id)
        db.session.commit()

        for qs in ('', f'?job_id={job_a.id}', f'?job_id={job_b.id}'):
            resp = self.t.client.get(f'/api/channel-groups/{grp.id}/format-plan{qs}')
            self.assertEqual('1280x720', resp.get_json()['buckets'][0]['resolution'],
                             f'{qs or "no job_id"} must still see the newest test')

    def test_the_plan_matches_the_lock_apply_format_strategy_writes(self):
        """dev/docs/BUGS.md 2026-09-09 07:02. The preview and the engine, same answer.

        The end-to-end statement of the rule above: whatever the endpoint says
        `highest_bitrate` would pick, running the strategy must actually pick. Asserted
        over a group whose job-scoped and any-job views genuinely differ.
        """
        from app.channel_groups import apply_format_strategy
        from app.database import ChannelGroup
        hd = make_channel(self.acct, name='HD')
        sd = make_channel(self.acct, name='SD')
        grp = make_group(members=[hd, sd], recording=True)
        job = make_test_job(name='Group check', channels=[hd, sd])
        db.session.commit()
        # The group's own check saw HD at a high bitrate; a later test from another job
        # re-measured it much lower, which is what flips the winner.
        make_channel_test(hd, status='COMPLETED', resolution='1920x1080', fps=60,
                          bitrate_kbps=9000, job_id=job.id)
        make_channel_test(sd, status='COMPLETED', resolution='1280x720', fps=30,
                          bitrate_kbps=4000, job_id=job.id)
        make_channel_test(hd, status='COMPLETED', resolution='1920x1080', fps=60,
                          bitrate_kbps=1000)
        db.session.commit()

        resp = self.t.client.get(f'/api/channel-groups/{grp.id}/format-plan?job_id={job.id}')
        previewed = resp.get_json()['strategies']['highest_bitrate']
        self.assertEqual('1280x720', previewed['resolution'])

        grp.format_strategy = 'highest_bitrate'
        db.session.commit()
        apply_format_strategy(grp)
        db.session.expire_all()
        written = db.session.get(ChannelGroup, grp.id)
        self.assertEqual((previewed['resolution'], int(previewed['fps'])),
                         written.locked_format_key)

    def test_missing_job_id_falls_back_to_any_job(self):
        # groups.js::openCreateGroupModalFor() opens the create-group modal without a
        # job_id - the endpoint must not 400 or 404 on that, per the spec.
        ch = make_channel(self.acct, name='Ch')
        grp = make_group(members=[ch])
        job = make_test_job(name='Job', channels=[ch])
        db.session.commit()
        make_channel_test(ch, status='COMPLETED', resolution='1920x1080', fps=60, job_id=job.id)
        db.session.commit()

        resp = self.t.client.get(f'/api/channel-groups/{grp.id}/format-plan')
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.get_json()['buckets'][0]['resolution'], '1920x1080')

    def test_health_check_only_system_group_uses_check_target_channels(self):
        # is_system=True routes through check_target_channels' in_guide branch instead of
        # static memberships - membership rows are irrelevant for a system group.
        in_guide_ch = make_channel(self.acct, name='Guide Ch', in_guide=True)
        make_channel_test(in_guide_ch, status='COMPLETED', resolution='1920x1080', fps=60,
                          bitrate_kbps=3000)
        not_in_guide_ch = make_channel(self.acct, name='Not In Guide', in_guide=False)
        sys_grp = make_group(name='TV Guide Channels', is_system=True, recording=False)
        db.session.commit()

        resp = self.t.client.get(f'/api/channel-groups/{sys_grp.id}/format-plan')
        self.assertEqual(resp.status_code, 200)
        data = resp.get_json()
        self.assertEqual(data['eligible_count'], 1)
        self.assertIn(in_guide_ch.id, data['buckets'][0]['channel_ids'])
        self.assertNotIn(not_in_guide_ch.id, data['buckets'][0]['channel_ids'])


class PlanPopulationTests(unittest.TestCase):
    """dev/docs/BUGS.md 2026-09-09 07:02. The endpoint ranks over the members the lock
    would actually filter, and says how many of them there are."""

    def setUp(self):
        self.t = make_test_app()
        self.acct = make_account()

    def tearDown(self):
        self.t.cleanup()

    def _mixed_group(self):
        """Two 720p30 members the group does not record from, one 1080p60 it does - the
        shape that used to lock group 5 to a format none of its 29 recording-enabled
        members had."""
        hd = make_channel(self.acct, name='HD')
        sd1 = make_channel(self.acct, name='SD 1')
        sd2 = make_channel(self.acct, name='SD 2')
        for ch, res, fps in ((hd, '1920x1080', 60), (sd1, '1280x720', 30),
                             (sd2, '1280x720', 30)):
            make_channel_test(ch, status='COMPLETED', resolution=res, fps=fps,
                              bitrate_kbps=5000)
        grp = make_group(members=[hd, sd1, sd2], recording=True,
                         disabled=[sd1.id, sd2.id])
        db.session.commit()
        return grp

    def test_the_plan_ranks_over_the_recording_enabled_members(self):
        grp = self._mixed_group()
        data = self.t.client.get(f'/api/channel-groups/{grp.id}/format-plan').get_json()
        self.assertEqual('1920x1080', data['strategies']['most_channels']['resolution'])
        self.assertEqual(3, data['total'])
        self.assertEqual(1, data['rank_total'])

    def test_rank_scope_all_opts_out_for_the_clone_preview(self):
        """A clone's members are all Recording-off, so its own engine ranks over everyone
        and the preview has to match that rather than the source group's switches."""
        grp = self._mixed_group()
        data = self.t.client.get(
            f'/api/channel-groups/{grp.id}/format-plan?rank_scope=all').get_json()
        self.assertEqual('1280x720', data['strategies']['most_channels']['resolution'])
        self.assertEqual(3, data['rank_total'])

    def test_every_bucket_reports_both_counts_and_its_pass_warn_split(self):
        grp = self._mixed_group()
        data = self.t.client.get(f'/api/channel-groups/{grp.id}/format-plan').get_json()
        by_res = {b['resolution']: b for b in data['buckets']}
        self.assertEqual((2, 0), (by_res['1280x720']['count'],
                                  by_res['1280x720']['rank_count']))
        self.assertEqual((1, 1), (by_res['1920x1080']['count'],
                                  by_res['1920x1080']['rank_count']))
        self.assertEqual(1, by_res['1920x1080']['pass_count'])
        self.assertEqual(0, by_res['1920x1080']['warn_count'])

    def test_rank_measured_counts_only_the_ranking_population(self):
        """It feeds the "measures a different format" vs "never tested" split, which is
        about the members the lock filters - not about every member on the page."""
        grp = self._mixed_group()
        untested = make_channel(self.acct, name='Untested')
        make_group(name='holder', members=[untested])
        db.session.commit()
        data = self.t.client.get(f'/api/channel-groups/{grp.id}/format-plan').get_json()
        self.assertEqual(1, data['rank_measured'])


class DerivedReferenceTests(unittest.TestCase):
    """dev/docs/BUGS.md 2026-09-09 07:02, raised against group 7: the settings
    picker labelled "Healthiest member's format" with the group's own LOCK.

    group_reference_key() returns the lock when one is set - correct for its own callers,
    and wrong as the answer to "which format would highest_score follow", which pins
    nothing. Two questions, two values (CLAUDE.md 'one flag, one meaning')."""

    def setUp(self):
        self.t = make_test_app()
        self.acct = make_account()

    def tearDown(self):
        self.t.cleanup()

    def test_the_derived_reference_ignores_the_lock(self):
        from app.routes.channel_groups import group_detail_rows
        healthy = make_channel(self.acct, name='Healthy 1080p50')
        other = make_channel(self.acct, name='Lower 4K')
        healthy.health_score = 100
        other.health_score = 89
        make_channel_test(healthy, status='COMPLETED', resolution='1920x1080', fps=50,
                          bitrate_kbps=9000)
        make_channel_test(other, status='COMPLETED', resolution='3840x2160', fps=50,
                          bitrate_kbps=10800)
        grp = make_group(members=[healthy, other], recording=True)
        grp.format_strategy = 'manual'
        grp.set_locked_format('3840x2160', 50)
        db.session.commit()

        payload = group_detail_rows(grp, grp.check)
        self.assertEqual('3840x2160 @ 50', payload['lock_label'])
        self.assertEqual('3840x2160 @ 50', payload['reference_label'],
                         'the effective reference is still the lock - that is its job')
        self.assertEqual('1920x1080 @ 50', payload['derived_reference_label'],
                         'what the data points at, which is what highest_score follows')

    def test_a_member_nobody_records_from_never_becomes_the_derived_reference(self):
        """It follows whichever member is healthiest AND would serve the group, so a
        member the group does not record from cannot be the one it names."""
        from app.routes.channel_groups import group_detail_rows
        recording = make_channel(self.acct, name='Records')
        watched = make_channel(self.acct, name='Watched only')
        recording.health_score = 60
        watched.health_score = 100
        make_channel_test(recording, status='COMPLETED', resolution='1280x720', fps=30,
                          bitrate_kbps=3000)
        make_channel_test(watched, status='COMPLETED', resolution='3840x2160', fps=50,
                          bitrate_kbps=9000)
        grp = make_group(members=[recording, watched], recording=True,
                         disabled=[watched.id])
        db.session.commit()

        payload = group_detail_rows(grp, grp.check)
        self.assertEqual('1280x720 @ 30', payload['derived_reference_label'])


class DetailPageReadsWhatTheRecorderReadsTests(unittest.TestCase):
    """dev/docs/BUGS.md 2026-09-09 07:02. The member table is scoped to the attached
    health check on purpose, but every lock-derived fact on the page is not.

    format_eligible_members() is the one helper every selection site asks, and the
    recorder hands it each member's newest test regardless of job. Handing it the
    job-scoped map instead made the page answer "which members may serve" differently
    from the code that serves them - the row could show no block while every recording
    fell through the zero-survivor override (CLAUDE.md "Format lock filters, health score
    ranks")."""

    def setUp(self):
        self.t = make_test_app()
        self.acct = make_account()

    def tearDown(self):
        self.t.cleanup()

    def _group_with_own_check(self, channels):
        """A locked group holding its own health check, so group_detail_rows renders the
        job-scoped table the real page renders."""
        from tests.support.seed import set_check
        grp = make_group(members=channels, recording=True, format_strategy='manual')
        grp.set_locked_format('1920x1080', 60)
        job = set_check(grp, name='Group check', status='COMPLETED')
        db.session.commit()
        return grp, job

    def test_a_newer_test_from_another_job_decides_who_the_lock_blocks(self):
        from app.routes.channel_groups import group_detail_rows
        ch = make_channel(self.acct, name='Drifted')
        keeper = make_channel(self.acct, name='Steady')
        grp, job = self._group_with_own_check([ch, keeper])
        # The group's own check saw both at the locked format; a later test from another
        # job caught one of them drifting to 720p30.
        for c in (ch, keeper):
            make_channel_test(c, status='COMPLETED', resolution='1920x1080', fps=60,
                              bitrate_kbps=5000, job_id=job.id)
        make_channel_test(ch, status='COMPLETED', resolution='1280x720', fps=30,
                          bitrate_kbps=5000)
        db.session.commit()

        payload = group_detail_rows(grp, job)
        blocked = {r['channel_id'] for r in payload['rows'] if r.get('format_blocked')}
        self.assertEqual({ch.id}, blocked,
                         'the drifted member is what the recorder would skip, so it is '
                         'what the row must say')
        self.assertEqual(1, payload['warnings']['format_blocked_count'])
        self.assertEqual(1, payload['mismatch_count'])

    def test_the_rows_themselves_still_show_the_attached_checks_results(self):
        """The other half of the split: scoping the TABLE to a job is deliberate and must
        survive. Only the lock-derived facts moved."""
        from app.routes.channel_groups import group_detail_rows
        ch = make_channel(self.acct, name='Drifted')
        grp, job = self._group_with_own_check([ch])
        make_channel_test(ch, status='COMPLETED', resolution='1920x1080', fps=60,
                          bitrate_kbps=5000, job_id=job.id)
        make_channel_test(ch, status='COMPLETED', resolution='1280x720', fps=30,
                          bitrate_kbps=5000)
        db.session.commit()

        payload = group_detail_rows(grp, job)
        self.assertEqual('1920x1080', payload['rows'][0]['last_test']['resolution'])


if __name__ == '__main__':
    unittest.main(verbosity=2)
