"""Tier 2 - Groups unification 2/4 behavior (DESIGN-groups-unification.md):

  * Busy-member skip rule (decided 2026-07-20): group member selection at record
    start and at failover prefers a free active member over one whose channel has
    another IN_PROGRESS recording; when no free alternative exists it knowingly
    takes the busy member, and the GROUP_MEMBER_SELECTED / GROUP_FAILOVER event
    extra says so (took_busy / skipped_busy_channel_ids).
  * The pinned system group is the one thing still refused server-side - guide
    toggle, format lock, format strategy and recording creation all reject it,
    because its membership is computed from the guide rather than stored. The old
    check_only restrictions are gone with `kind` (dev/changelog/741).
  * A group still on `health_check_only` accepts any mix of channels silently; a group
    that records gets a soft, proceedable format warning rather than a refusal
    (dev/changelog/762).

The guide-hiding rules this file used to carry are gone with the behavior they described
(dev/changelog/751): membership no longer suppresses a channel's own guide row, and
"add to guide" no longer redirects the write onto the member's group. What replaced them
is asserted in tests/test_group_membership_never_hides.py.

Runs against a throwaway temp SQLite DB - never the live dvr.db.
  python3 -m unittest tests.test_groups_membership
"""
import json
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tests.support import make_test_app  # noqa: E402
from tests.support.seed import (  # noqa: E402
    make_account, make_channel, make_channel_test, make_group, make_recording, make_test_job,
)
from app import db  # noqa: E402
from app.database import (  # noqa: E402
    Recording, RecordingEvent, ChannelGroup, ChannelGroupMember, ChannelTest,
    OnDemandTestJob, GROUP_MEMBER_SELECTED, GROUP_FAILOVER, GROUP_FORMAT_HIGHEST_SCORE,
)


def _missing_dvr_cfg(*a, **k):
    """Config whose dvr_output_dir doesn't exist - start_recording runs member
    selection + the handoff check, then marks FAILED and returns before spawning
    anything. Lets the selection logic run end-to-end with no ffmpeg."""
    from app.config import load_config as real_load_config
    cfg = real_load_config()
    cfg = json.loads(json.dumps(cfg))  # deep copy - never mutate the cached dict
    cfg['recording']['dvr_output_dir'] = '/nonexistent-busy-skip-test'
    return cfg


def _event_extra(recording_id, event_type):
    ev = (RecordingEvent.query
          .filter_by(recording_id=recording_id, event_type=event_type)
          .order_by(RecordingEvent.id.desc()).first())
    assert ev is not None, f'no {event_type} event for recording {recording_id}'
    return json.loads(ev.extra_data or '{}'), ev.detail


class BusySkipStartTests(unittest.TestCase):
    def setUp(self):
        self.t = make_test_app()
        self.acct = make_account()

    def tearDown(self):
        self.t.cleanup()

    def _start(self, rec_id):
        import app.recorder as recorder
        with mock.patch.object(recorder, 'load_config', _missing_dvr_cfg), \
             mock.patch.object(recorder, '_handoff_stop_old_recording') as handoff:
            recorder.start_recording(self.t.app, rec_id)
        return handoff

    def test_start_skips_busy_member_for_free_alternative(self):
        busy = make_channel(self.acct, name='Busy Best', health_score=90)
        free = make_channel(self.acct, name='Free Worse', health_score=50)
        grp = make_group(members=[busy, free])
        make_recording(status='IN_PROGRESS', channel_id=busy.id, name='occupier')
        rec = make_recording(status='SCHEDULED', group_id=grp.id, name='group_rec')
        db.session.commit()

        self._start(rec.id)
        db.session.expire_all()

        rec = db.session.get(Recording, rec.id)
        self.assertEqual(rec.channel_id, free.id,
                         'selection must skip the busy (higher-scored) member')
        extra, detail = _event_extra(rec.id, GROUP_MEMBER_SELECTED)
        self.assertFalse(extra['took_busy'])
        self.assertEqual(extra['skipped_busy_channel_ids'], [busy.id])
        self.assertIn('skipped busy channel', detail)

    def test_start_takes_busy_member_when_no_alternative(self):
        busy = make_channel(self.acct, name='Only Member', health_score=90)
        grp = make_group(members=[busy])
        make_recording(status='IN_PROGRESS', channel_id=busy.id, name='occupier')
        rec = make_recording(status='SCHEDULED', group_id=grp.id, name='group_rec')
        db.session.commit()

        handoff = self._start(rec.id)
        db.session.expire_all()

        rec = db.session.get(Recording, rec.id)
        self.assertEqual(rec.channel_id, busy.id)
        extra, detail = _event_extra(rec.id, GROUP_MEMBER_SELECTED)
        self.assertTrue(extra['took_busy'])
        self.assertIn('busy', detail)
        handoff.assert_called_once()


class BusySkipFailoverTests(unittest.TestCase):
    def setUp(self):
        self.t = make_test_app()
        self.acct = make_account()

    def tearDown(self):
        import app.recorder as recorder
        with recorder._lock:
            recorder._active.clear()
        self.t.cleanup()

    def _failover(self, rec_id):
        import app.recorder as recorder
        import app.health_score as health_score
        with recorder._lock:
            recorder._active[rec_id] = recorder.RecordingState()
        with mock.patch.object(health_score, 'apply_failover_health_observation'):
            return recorder.failover_group_member(self.t.app, rec_id, 'test reason')

    def test_failover_skips_busy_member(self):
        current = make_channel(self.acct, name='Dying', health_score=95)
        busy = make_channel(self.acct, name='Busy Next', health_score=90)
        free = make_channel(self.acct, name='Free Last', health_score=50)
        grp = make_group(members=[current, busy, free])
        make_recording(status='IN_PROGRESS', channel_id=busy.id, name='occupier')
        rec = make_recording(status='IN_PROGRESS', channel_id=current.id,
                             group_id=grp.id, name='group_rec')
        db.session.commit()

        self.assertTrue(self._failover(rec.id))
        db.session.expire_all()

        rec = db.session.get(Recording, rec.id)
        self.assertEqual(rec.channel_id, free.id,
                         'failover must skip the busy member for the free one')
        extra, _ = _event_extra(rec.id, GROUP_FAILOVER)
        self.assertFalse(extra['took_busy'])
        self.assertEqual(extra['skipped_busy_channel_ids'], [busy.id])

    def test_failover_takes_busy_member_when_no_alternative(self):
        current = make_channel(self.acct, name='Dying', health_score=95)
        busy = make_channel(self.acct, name='Busy Only', health_score=90)
        grp = make_group(members=[current, busy])
        make_recording(status='IN_PROGRESS', channel_id=busy.id, name='occupier')
        rec = make_recording(status='IN_PROGRESS', channel_id=current.id,
                             group_id=grp.id, name='group_rec')
        db.session.commit()

        self.assertTrue(self._failover(rec.id))
        db.session.expire_all()

        rec = db.session.get(Recording, rec.id)
        self.assertEqual(rec.channel_id, busy.id)
        extra, _ = _event_extra(rec.id, GROUP_FAILOVER)
        self.assertTrue(extra['took_busy'])


class SystemGroupEnforcementTests(unittest.TestCase):
    """What is still refused server-side, now that a group has no kind.

    This class used to guard the check-only restrictions: a check_only group could not be
    put in the guide, could not carry a format lock, could not back a recording, and
    skipped the format guard on add. All of those are gone - any group can do any of it,
    and whether it SHOULD is `format_strategy` plus the two participation switches
    (dev/changelog/741). What survives is the pinned system group, which is genuinely
    different: its membership is computed from the guide rather than stored, so the
    surfaces that would write to it are refused rather than quietly doing nothing.
    """

    def setUp(self):
        self.t = make_test_app()
        self.t.app.config['WTF_CSRF_ENABLED'] = False
        self.acct = make_account()
        self.sys_grp = ChannelGroup.query.filter_by(is_system=True).first()
        db.session.commit()

    def tearDown(self):
        self.t.cleanup()

    def test_system_group_guide_toggle_rejected(self):
        resp = self.t.client.post(f'/api/channel-groups/{self.sys_grp.id}/guide-toggle')
        self.assertEqual(resp.status_code, 400)
        self.assertFalse(db.session.get(ChannelGroup, self.sys_grp.id).in_guide)

    def test_system_group_format_lock_rejected(self):
        resp = self.t.client.post(f'/api/channel-groups/{self.sys_grp.id}/format',
                                  json={'resolution': '1920x1080', 'fps': 60})
        self.assertEqual(resp.status_code, 400)

    def test_system_group_format_strategy_rejected(self):
        resp = self.t.client.post(f'/api/channel-groups/{self.sys_grp.id}/format-strategy',
                                  json={'strategy': 'highest_bitrate'})
        self.assertEqual(resp.status_code, 400)

    def test_system_group_cannot_back_a_recording(self):
        from datetime import datetime, timedelta
        start = datetime.now() + timedelta(hours=1)
        stop = start + timedelta(hours=1)
        resp = self.t.client.post('/recordings/new-json', data={
            'name': 'nope',
            'url': 'http://example.test/live/1',
            'start_time': start.strftime('%Y-%m-%dT%H:%M'),
            'stop_time': stop.strftime('%Y-%m-%dT%H:%M'),
            'group_id': str(self.sys_grp.id),
        })
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(Recording.query.filter_by(name='nope').count(), 0)


class HealthCheckOnlyGroupTests(unittest.TestCase):
    """A group still on `health_check_only` is not a recording source yet, so it accepts
    any mix of channels - the format questions are asked when it is promoted, not while it
    is being assembled (DESIGN-channel-groups-model.md §14.1)."""

    def setUp(self):
        self.t = make_test_app()
        self.t.app.config['WTF_CSRF_ENABLED'] = False
        self.acct = make_account()
        self.grp = make_group(name='Check Bag', members=[], in_guide=False, recording=False)
        db.session.commit()

    def tearDown(self):
        self.t.cleanup()

    def _mixed_pair(self):
        from app.database import ChannelTest
        from datetime import datetime
        hd = make_channel(self.acct, name='HD feed')
        sd = make_channel(self.acct, name='SD feed')
        db.session.add_all([
            ChannelTest(channel_id=hd.id, test_started_at=datetime.utcnow(),
                        status='COMPLETED', resolution='1920x1080', fps=60.0),
            ChannelTest(channel_id=sd.id, test_started_at=datetime.utcnow(),
                        status='COMPLETED', resolution='1280x720', fps=30.0),
        ])
        db.session.commit()
        return hd, sd

    def test_add_members_skips_the_format_guard(self):
        hd, sd = self._mixed_pair()
        resp = self.t.client.post(f'/api/channel-groups/{self.grp.id}/members',
                                  json={'channel_ids': [hd.id, sd.id]})
        self.assertEqual(resp.status_code, 200, resp.get_data(as_text=True))
        self.assertTrue(resp.get_json()['success'])

    def test_a_group_that_records_warns_about_a_format_mix(self):
        """200 with a soft warning, not a refusal (dev/changelog/762). The mix is worth
        saying out loud for a group that records, but adding a member is never blocked
        over its format - the lock skips it where members are chosen instead."""
        hd, sd = self._mixed_pair()
        strict = make_group(name='Strict', members=[])
        strict.format_strategy = GROUP_FORMAT_HIGHEST_SCORE
        db.session.commit()
        resp = self.t.client.post(f'/api/channel-groups/{strict.id}/members',
                                  json={'channel_ids': [hd.id, sd.id]})
        self.assertEqual(resp.status_code, 200, resp.get_data(as_text=True))
        body = resp.get_json()
        self.assertFalse(body['success'])
        self.assertIn('format_mismatch', body, body)
        named = {c['channel_id']
                 for grp in body['format_mismatch']['groups'] for c in grp['channels']}
        self.assertEqual(named, {hd.id, sd.id},
                         'the warning names the channels whose formats disagree')
        self.assertEqual(ChannelGroupMember.query.filter_by(group_id=strict.id).count(), 0,
                         'an unconfirmed add writes no membership rows')

    def test_the_format_warning_is_proceedable(self):
        """The whole difference from the deleted rule: `force` gets through, and both
        mismatched members land (dev/changelog/762)."""
        hd, sd = self._mixed_pair()
        strict = make_group(name='Strict', members=[])
        strict.format_strategy = GROUP_FORMAT_HIGHEST_SCORE
        db.session.commit()
        resp = self.t.client.post(f'/api/channel-groups/{strict.id}/members',
                                  json={'channel_ids': [hd.id, sd.id], 'force': True})
        self.assertEqual(resp.status_code, 200, resp.get_data(as_text=True))
        self.assertTrue(resp.get_json()['success'])
        self.assertEqual(
            {m.channel_id for m in ChannelGroupMember.query.filter_by(group_id=strict.id)},
            {hd.id, sd.id})


class CheckRunChannelsKindTests(unittest.TestCase):
    """Groups unification 3/4: a health check's channel list IS its group's
    membership, and which members a RUN actually tests depends on the group kind."""

    def setUp(self):
        self.t = make_test_app()
        self.acct = make_account()

    def tearDown(self):
        self.t.cleanup()

    def test_a_run_needs_both_test_switches_on(self):
        # One rule for every group now that kind is gone: a member is tested when BOTH
        # its membership's test_enabled and the channel-wide Channel.test_enabled are on,
        # and the channel-wide one wins (DESIGN-channel-groups-model.md 4.2). Excluded
        # members stay LISTED either way, so they remain visible and toggleable.
        from app.channel_groups import check_run_channels, check_target_channels
        keep = make_channel(self.acct, name='Keep')
        off_member = make_channel(self.acct, name='OffForThisGroup')
        off_channel = make_channel(self.acct, name='OffEverywhere', test_enabled=False)
        grp = make_group(name='G', members=[keep, off_member, off_channel], in_guide=False,
                         test_disabled=[off_member.id])
        db.session.commit()

        channels, excluded_ids = check_target_channels(grp)
        self.assertEqual([c.id for c in channels],
                         [keep.id, off_member.id, off_channel.id])
        self.assertEqual(excluded_ids, {off_member.id, off_channel.id})
        self.assertEqual([c.id for c in check_run_channels(grp)], [keep.id])

    def test_system_group_membership_is_computed_not_stored(self):
        from app.channel_groups import check_run_channels, check_target_channels
        from app.database import ChannelGroupMember
        listed = make_channel(self.acct, name='Listed', in_guide=True)
        untested = make_channel(self.acct, name='Untested', in_guide=True,
                                test_enabled=False)
        make_channel(self.acct, name='NotInGuide', in_guide=False)
        grp = make_group(name='TV Guide Channels', members=(), in_guide=False,
                         recording=False, is_system=True)
        db.session.commit()
        self.assertEqual(
            ChannelGroupMember.query.filter_by(group_id=grp.id).count(), 0,
            'the system group must never store membership rows')
        channels, disabled_ids = check_target_channels(grp)
        self.assertEqual([c.id for c in channels], [listed.id, untested.id],
                         'computed from in_guide, in guide order')
        self.assertEqual(disabled_ids, {untested.id},
                         'test_enabled=False is the system group\'s "disabled"')
        self.assertEqual([c.id for c in check_run_channels(grp)], [listed.id])


class GroupsTabScopeTests(unittest.TestCase):
    """Every group a health check's channel list belongs to is just a group, and the
    Groups tab lists them all in one section (dev/changelog/741). The one group that is
    still special is the pinned system group: its membership is COMPUTED from the guide
    rather than stored, so nothing may offer to add channels to it."""

    def setUp(self):
        self.t = make_test_app()
        self.acct = make_account()

    def tearDown(self):
        self.t.cleanup()

    def test_groups_page_lists_every_group_in_one_section(self):
        ch = make_channel(self.acct, name='Member')
        make_group(name='Real Channel Group', members=[ch])
        make_test_job(name='Nightly Check Bag', channels=[ch])
        db.session.commit()
        body = self.t.client.get('/channel-groups').get_data(as_text=True)
        self.assertIn('Real Channel Group', body)
        self.assertIn('Nightly Check Bag', body,
                      'a health check\'s group is a group and belongs in the one list')

    def test_list_groups_api_offers_every_group_but_the_system_one(self):
        """The dropdown used to exclude check-only bags, on the grounds that adding a
        channel to one would silently put it in somebody's health check. With one noun
        that reasoning is gone - any group can take a channel, and what it does with it
        is the group's own configuration. The pinned system group stays excluded for a
        different and durable reason: its membership is computed, so adding to it would
        silently do nothing (dev/changelog/741)."""
        ch = make_channel(self.acct, name='Member')
        make_group(name='Real Channel Group', members=[ch])
        make_test_job(name='Nightly Check Bag', channels=[ch])
        db.session.commit()
        names = [g['name'] for g in
                 self.t.client.get('/api/channel-groups').get_json()['groups']]
        self.assertEqual(sorted(names), ['Nightly Check Bag', 'Real Channel Group'])
        self.assertNotIn('TV Guide Channels', names,
                         'the system group\'s membership is computed - adding to it is a no-op')


class GroupJobDeleteGuardTests(unittest.TestCase):
    """Groups unification 3/4 teardown: a group backing a health check can't be
    dissolved out from under it, and deleting a check cleans up the 1:1 bag it
    owns without touching a bag another check still uses."""

    def setUp(self):
        self.t = make_test_app()
        self.t.app.config['WTF_CSRF_ENABLED'] = False
        self.acct = make_account()

    def tearDown(self):
        self.t.cleanup()

    def test_deleting_group_cascades_its_attached_job(self):
        """Deleting a group with an attached (non-running) health check cascades:
        the group, the job, and the job's ChannelTest rows all go, rather than the
        old flat 409 refusal with no path forward."""
        ch = make_channel(self.acct, name='Member')
        job = make_test_job(name='Nightly', channels=[ch])
        make_channel_test(ch, status='COMPLETED', job_id=job.id)
        db.session.commit()
        group_id = job.group_id
        job_id = job.id
        resp = self.t.client.post(f'/api/channel-groups/{group_id}/delete')
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(resp.get_json()['success'])
        db.session.expire_all()
        self.assertIsNone(db.session.get(ChannelGroup, group_id))
        self.assertIsNone(db.session.get(OnDemandTestJob, job_id))
        self.assertEqual(ChannelTest.query.filter_by(job_id=job_id).count(), 0)

    def test_deleting_group_cascades_a_directly_attached_job_on_a_paired_channel_group(self):
        """The linked-pair case (a kind='channel' group with its own directly-attached
        job, as opposed to make_test_job's auto-created check-only bag) cascades the
        same way."""
        ch = make_channel(self.acct, name='Member')
        grp = make_group(name='Paired group', members=[ch])
        job = OnDemandTestJob(name='Attached check', status='QUEUED', group_id=grp.id)
        db.session.add(job)
        db.session.commit()
        job_id = job.id
        resp = self.t.client.post(f'/api/channel-groups/{grp.id}/delete')
        self.assertEqual(resp.status_code, 200)
        db.session.expire_all()
        self.assertIsNone(db.session.get(ChannelGroup, grp.id))
        self.assertIsNone(db.session.get(OnDemandTestJob, job_id))

    def test_deleting_group_blocked_while_its_job_is_running(self):
        """A RUNNING job can't be safely cascaded through, so this still 409s and
        names the job - only the RUNNING case keeps the hard block."""
        ch = make_channel(self.acct, name='Member')
        job = make_test_job(name='Nightly', channels=[ch], status='RUNNING')
        db.session.commit()
        group_id = job.group_id
        resp = self.t.client.post(f'/api/channel-groups/{group_id}/delete')
        self.assertEqual(resp.status_code, 409)
        self.assertIn('Nightly', resp.get_json()['error'])
        self.assertIsNotNone(db.session.get(ChannelGroup, group_id))
        self.assertIsNotNone(db.session.get(OnDemandTestJob, job.id))

    def test_deleting_job_deletes_its_sole_owned_group(self):
        ch = make_channel(self.acct, name='Member')
        job = make_test_job(name='Nightly', channels=[ch])
        db.session.commit()
        group_id = job.group_id
        resp = self.t.client.delete(f'/api/channel-tests/on-demand/{job.id}')
        self.assertEqual(resp.status_code, 200)
        db.session.expire_all()
        self.assertIsNone(db.session.get(ChannelGroup, group_id),
                          'an auto-created 1:1 check bag has no other owner and no '
                          'pre-#22 UI to remove it - it must go with its last job')

    def test_deleting_job_keeps_group_a_second_job_still_uses(self):
        ch = make_channel(self.acct, name='Member')
        first = make_test_job(name='Nightly quick', channels=[ch])
        db.session.commit()
        second = OnDemandTestJob(name='Weekly deep', status='QUEUED',
                                 group_id=first.group_id)
        db.session.add(second)
        db.session.commit()
        group_id = first.group_id
        resp = self.t.client.delete(f'/api/channel-tests/on-demand/{first.id}')
        self.assertEqual(resp.status_code, 200)
        db.session.expire_all()
        self.assertIsNotNone(db.session.get(ChannelGroup, group_id),
                             'several jobs may share one group - the survivor still '
                             'needs its channel list')
        self.assertEqual(db.session.get(OnDemandTestJob, second.id).group_id, group_id)


class GroupNameUniquenessTests(unittest.TestCase):
    """No uniqueness check existed on group name before this - create/rename/clone
    could all produce two groups sharing a name. The check is case-insensitive and
    covers the pinned system group's name too (decided 2026-07-25)."""

    def setUp(self):
        self.t = make_test_app()
        self.t.app.config['WTF_CSRF_ENABLED'] = False
        self.acct = make_account()
        self.existing = make_group(name='My Group', members=[])
        db.session.commit()

    def tearDown(self):
        self.t.cleanup()

    def test_create_blocked_on_exact_name(self):
        resp = self.t.client.post('/api/channel-groups', json={'name': 'My Group'})
        self.assertEqual(resp.status_code, 409)
        self.assertIn('My Group', resp.get_json()['error'])

    def test_create_blocked_case_insensitive(self):
        resp = self.t.client.post('/api/channel-groups', json={'name': 'my group'})
        self.assertEqual(resp.status_code, 409)

    def test_create_blocked_against_system_group_name(self):
        db.session.add(ChannelGroup(name='TV Guide Channels', is_system=True))
        db.session.commit()
        resp = self.t.client.post('/api/channel-groups', json={'name': 'tv guide channels'})
        self.assertEqual(resp.status_code, 409)

    def test_create_allowed_with_distinct_name(self):
        resp = self.t.client.post('/api/channel-groups', json={'name': 'Totally Different'})
        self.assertEqual(resp.status_code, 200, resp.get_data(as_text=True))
        self.assertTrue(resp.get_json()['success'])

    def test_rename_blocked_on_collision(self):
        other = make_group(name='Other Group', members=[])
        db.session.commit()
        resp = self.t.client.post(f'/api/channel-groups/{other.id}/rename',
                                  json={'name': 'MY GROUP'})
        self.assertEqual(resp.status_code, 409)
        db.session.expire_all()
        self.assertEqual(db.session.get(ChannelGroup, other.id).name, 'Other Group')

    def test_rename_to_own_current_name_allowed(self):
        resp = self.t.client.post(f'/api/channel-groups/{self.existing.id}/rename',
                                  json={'name': 'My Group'})
        self.assertEqual(resp.status_code, 200, resp.get_data(as_text=True))

    def test_rename_to_own_name_different_case_allowed(self):
        resp = self.t.client.post(f'/api/channel-groups/{self.existing.id}/rename',
                                  json={'name': 'MY GROUP'})
        self.assertEqual(resp.status_code, 200, resp.get_data(as_text=True))

    def test_clone_blocked_on_collision(self):
        ch = make_channel(self.acct, name='Src Member')
        src = make_group(name='Source Group', members=[ch])
        db.session.commit()
        resp = self.t.client.post(f'/api/channel-groups/{src.id}/clone',
                                  json={'name': 'my group'})
        self.assertEqual(resp.status_code, 409)


if __name__ == '__main__':
    unittest.main(verbosity=2)
