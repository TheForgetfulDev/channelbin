"""Tier 2 - GET /api/channel-groups/<id>/format-plan (app/routes/channel_groups.py::
group_format_plan), the read-only auto-select-format endpoint. Planned
2026-08-06, shipped in dev/changelog/494.

Runs against a throwaway temp SQLite DB - never the live dvr.db.
  python3 -m unittest tests.test_format_plan_routes
"""
import os
import re
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tests.support import make_test_app  # noqa: E402
from tests.support.seed import (  # noqa: E402
    make_account, make_channel, make_group, make_channel_test, make_test_job,
)
from app import db  # noqa: E402
from app.database import (ChannelGroupMember, ChannelEvent,  # noqa: E402
                          CHANNEL_UNGROUPED)


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

    def test_job_id_scopes_to_that_job_result(self):
        ch = make_channel(self.acct, name='Ch')
        grp = make_group(members=[ch])
        job_a = make_test_job(name='Job A', channels=[ch])
        job_b = make_test_job(name='Job B', channels=[ch])
        db.session.commit()
        # Same channel, two different jobs, two different formats.
        make_channel_test(ch, status='COMPLETED', resolution='1920x1080', fps=60,
                          job_id=job_a.id)
        make_channel_test(ch, status='COMPLETED', resolution='1280x720', fps=30,
                          job_id=job_b.id)
        db.session.commit()

        resp_a = self.t.client.get(f'/api/channel-groups/{grp.id}/format-plan?job_id={job_a.id}')
        resp_b = self.t.client.get(f'/api/channel-groups/{grp.id}/format-plan?job_id={job_b.id}')
        self.assertEqual(resp_a.get_json()['buckets'][0]['resolution'], '1920x1080')
        self.assertEqual(resp_b.get_json()['buckets'][0]['resolution'], '1280x720')

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


class ApplyFormatPlanRouteTests(unittest.TestCase):
    """POST /api/channel-groups/<id>/apply-format-plan (app/routes/channel_groups.py::
    apply_format_plan) - item 2 of the auto-select-format feature, dev/changelog/495.
    Applies a strategy to an EXISTING group's current members: locks the format, then
    either keeps (leaving every member's Recording switch untouched - the lock alone
    filters the outliers wherever a member is chosen) or removes the members that
    don't match."""

    def setUp(self):
        self.t = make_test_app()
        self.t.app.config['WTF_CSRF_ENABLED'] = False
        self.acct = make_account()

    def tearDown(self):
        self.t.cleanup()

    def _post(self, group_id, strategy='most_channels', non_matching='keep'):
        return self.t.client.post(
            f'/api/channel-groups/{group_id}/apply-format-plan',
            json={'strategy': strategy, 'non_matching': non_matching})

    def test_404_for_missing_group(self):
        resp = self._post(999999)
        self.assertEqual(resp.status_code, 404)

    def test_400_for_a_health_check_only_group(self):
        ch = make_channel(self.acct, name='Ch')
        grp = make_group(members=[ch], recording=False)
        db.session.commit()
        resp = self._post(grp.id)
        self.assertEqual(resp.status_code, 400)

    def test_400_for_invalid_strategy(self):
        ch = make_channel(self.acct, name='Ch')
        grp = make_group(members=[ch])
        db.session.commit()
        resp = self._post(grp.id, strategy='fastest')
        self.assertEqual(resp.status_code, 400)

    def test_400_for_invalid_non_matching(self):
        ch = make_channel(self.acct, name='Ch')
        grp = make_group(members=[ch])
        db.session.commit()
        resp = self._post(grp.id, non_matching='delete')
        self.assertEqual(resp.status_code, 400)

    def test_400_with_observable_rationale_when_no_eligible_format(self):
        untested = make_channel(self.acct, name='Untested')
        grp = make_group(members=[untested])
        db.session.commit()
        resp = self._post(grp.id)
        self.assertEqual(resp.status_code, 400)
        self.assertTrue(resp.get_json()['error'])

    def test_keep_path_locks_format_and_leaves_the_outlier_enabled(self):
        hd1 = make_channel(self.acct, name='HD 1')
        hd2 = make_channel(self.acct, name='HD 2')
        sd1 = make_channel(self.acct, name='SD 1')
        make_channel_test(hd1, status='COMPLETED', resolution='1920x1080', fps=60, bitrate_kbps=3800)
        make_channel_test(hd2, status='COMPLETED', resolution='1920x1080', fps=60, bitrate_kbps=3900)
        make_channel_test(sd1, status='COMPLETED', resolution='1280x720', fps=30, bitrate_kbps=3600)
        grp = make_group(members=[hd1, hd2, sd1])
        db.session.commit()

        resp = self._post(grp.id, strategy='most_channels', non_matching='keep')
        self.assertEqual(resp.status_code, 200)
        data = resp.get_json()
        self.assertTrue(data['success'])
        self.assertEqual(data['format']['resolution'], '1920x1080')
        self.assertEqual(data['format']['fps'], 60)
        self.assertEqual(data['kept'], 2)
        self.assertEqual(data['filtered'], 1)
        self.assertEqual(data['removed'], 0)

        db.session.expire_all()
        grp = db.session.get(type(grp), grp.id)
        self.assertEqual(grp.format_resolution, '1920x1080')
        self.assertEqual(grp.format_fps, 60)
        # 'keep' sets the lock and NOTHING else: the outlier keeps its Recording switch,
        # and the lock filters it out wherever a member is chosen instead
        # (DESIGN-channel-groups-model.md 4.1). Nothing was unticked.
        enabled = {m.channel_id: m.recording_enabled for m in grp.memberships}
        self.assertTrue(enabled[sd1.id])
        self.assertTrue(enabled[hd1.id])
        self.assertTrue(enabled[hd2.id])

    def test_remove_path_deletes_the_outlier_membership_and_logs_an_event(self):
        hd1 = make_channel(self.acct, name='HD 1')
        hd2 = make_channel(self.acct, name='HD 2')
        sd1 = make_channel(self.acct, name='SD 1')
        make_channel_test(hd1, status='COMPLETED', resolution='1920x1080', fps=60, bitrate_kbps=3800)
        make_channel_test(hd2, status='COMPLETED', resolution='1920x1080', fps=60, bitrate_kbps=3900)
        make_channel_test(sd1, status='COMPLETED', resolution='1280x720', fps=30, bitrate_kbps=3600)
        grp = make_group(members=[hd1, hd2, sd1])
        db.session.commit()

        resp = self._post(grp.id, strategy='most_channels', non_matching='remove')
        self.assertEqual(resp.status_code, 200)
        data = resp.get_json()
        self.assertEqual(data['kept'], 2)
        self.assertEqual(data['filtered'], 0)
        self.assertEqual(data['removed'], 1)

        remaining = {m.channel_id for m in ChannelGroupMember.query.filter_by(group_id=grp.id).all()}
        self.assertEqual(remaining, {hd1.id, hd2.id})
        events = ChannelEvent.query.filter_by(channel_id=sd1.id, event_type=CHANNEL_UNGROUPED).all()
        self.assertEqual(len(events), 1)

    def test_best_ranked_outlier_keeps_its_recording_switch(self):
        # sd_best is the highest-scored member but the minority format - most_channels
        # picks the HD bucket, so sd_best would be an outlier under the new lock. But
        # this route only ever writes the group's format lock - it never touches a
        # member's Recording switch (test_a_members_recording_switch_is_never_written_
        # by_this_route, below) - so sd_best keeps recording_enabled regardless of rank.
        sd_best = make_channel(self.acct, name='SD Best', health_score=99)
        hd1 = make_channel(self.acct, name='HD 1')
        hd2 = make_channel(self.acct, name='HD 2')
        make_channel_test(sd_best, status='COMPLETED', resolution='1280x720', fps=30, bitrate_kbps=3600)
        make_channel_test(hd1, status='COMPLETED', resolution='1920x1080', fps=60, bitrate_kbps=3800)
        make_channel_test(hd2, status='COMPLETED', resolution='1920x1080', fps=60, bitrate_kbps=3900)
        grp = make_group(members=[sd_best, hd1, hd2])
        db.session.commit()

        resp = self._post(grp.id, strategy='most_channels', non_matching='keep')
        data = resp.get_json()
        self.assertEqual(data['format']['resolution'], '1920x1080')

        db.session.expire_all()
        m = ChannelGroupMember.query.filter_by(group_id=grp.id, channel_id=sd_best.id).first()
        self.assertTrue(m.recording_enabled)

    def test_untested_member_keeps_its_recording_switch(self):
        hd1 = make_channel(self.acct, name='HD 1')
        hd2 = make_channel(self.acct, name='HD 2')
        untested = make_channel(self.acct, name='Untested')
        make_channel_test(hd1, status='COMPLETED', resolution='1920x1080', fps=60, bitrate_kbps=3800)
        make_channel_test(hd2, status='COMPLETED', resolution='1920x1080', fps=60, bitrate_kbps=3900)
        grp = make_group(members=[hd1, hd2, untested])
        db.session.commit()

        resp = self._post(grp.id, strategy='most_channels', non_matching='keep')
        self.assertEqual(resp.status_code, 200)

        db.session.expire_all()
        m = ChannelGroupMember.query.filter_by(group_id=grp.id, channel_id=untested.id).first()
        self.assertTrue(m.recording_enabled)

    def test_a_members_recording_switch_is_never_written_by_this_route(self):
        hd1 = make_channel(self.acct, name='HD 1')
        hd2 = make_channel(self.acct, name='HD 2')
        sd1 = make_channel(self.acct, name='SD 1')
        make_channel_test(hd1, status='COMPLETED', resolution='1920x1080', fps=60, bitrate_kbps=3800)
        make_channel_test(hd2, status='COMPLETED', resolution='1920x1080', fps=60, bitrate_kbps=3900)
        make_channel_test(sd1, status='COMPLETED', resolution='1280x720', fps=30, bitrate_kbps=3600)
        grp = make_group(members=[hd1, hd2, sd1], disabled=[sd1.id])
        db.session.commit()

        resp = self._post(grp.id, strategy='most_channels', non_matching='keep')
        self.assertEqual(resp.status_code, 200)

        db.session.expire_all()
        m = ChannelGroupMember.query.filter_by(group_id=grp.id, channel_id=sd1.id).first()
        self.assertFalse(m.recording_enabled, 'the user turned this off; nothing else may touch it')

    def test_default_client_non_matching_value_round_trips_successfully(self):
        """dev/docs/BUGS.md 2026-08-22: format-picker-modal.js's default nonMatching value
        was 'disable', which this endpoint has never accepted - leaving the modal's
        "Members that do not match" choice on its default and clicking Apply always 400'd.
        Reads the real default straight out of the shipped JS (never duplicates it as a
        literal) and posts it through the real endpoint, so this fails again if the two
        ever drift apart."""
        js_path = os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            'static', 'js', 'format-picker-modal.js')
        with open(js_path) as f:
            src = f.read()
        m = re.search(r"let nonMatching = '([^']+)';", src)
        self.assertIsNotNone(m, 'could not find the nonMatching default in format-picker-modal.js')
        default_value = m.group(1)

        hd1 = make_channel(self.acct, name='HD 1')
        hd2 = make_channel(self.acct, name='HD 2')
        make_channel_test(hd1, status='COMPLETED', resolution='1920x1080', fps=60, bitrate_kbps=3800)
        make_channel_test(hd2, status='COMPLETED', resolution='1920x1080', fps=60, bitrate_kbps=3900)
        grp = make_group(members=[hd1, hd2])
        db.session.commit()

        resp = self._post(grp.id, strategy='most_channels', non_matching=default_value)
        self.assertEqual(resp.status_code, 200, resp.get_json())

    def test_client_supplied_channel_list_is_never_trusted(self):
        """The body only ever accepts strategy/non_matching - there is no channel_ids
        field at all, so a client cannot influence which members are picked."""
        hd1 = make_channel(self.acct, name='HD 1')
        sd1 = make_channel(self.acct, name='SD 1')
        make_channel_test(hd1, status='COMPLETED', resolution='1920x1080', fps=60, bitrate_kbps=3800)
        make_channel_test(sd1, status='COMPLETED', resolution='1280x720', fps=30, bitrate_kbps=3600)
        grp = make_group(members=[hd1, sd1])
        db.session.commit()

        resp = self.t.client.post(
            f'/api/channel-groups/{grp.id}/apply-format-plan',
            json={'strategy': 'most_channels', 'non_matching': 'remove',
                  'channel_ids': [sd1.id]})   # ignored - most_channels' own winner is hd1's format
        data = resp.get_json()
        self.assertEqual(data['format']['resolution'], '1920x1080')
        remaining = {m.channel_id for m in ChannelGroupMember.query.filter_by(group_id=grp.id).all()}
        self.assertEqual(remaining, {hd1.id})


if __name__ == '__main__':
    unittest.main(verbosity=2)
