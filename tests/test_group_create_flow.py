"""Tier 2 - the server half of the merged group + health check create flow
(static/js/group-create-flow.js, dev/changelog/831).

The flow's last screen posts ONE request that has to mint the channel group and its health
check together, because the round-3 guarantee is that nothing exists in the database until
that click. Three things follow, and each is a way the old ad hoc create path was not good
enough to be the one a real user group goes through:

  * The job's name is DERIVED, not demanded. A health check is a schedule its group carries
    (DESIGN-channel-groups-model.md DECIDED 2), so the modal renders no Name field, and a
    route that still required one would 400 on a field the user cannot see.
  * The group's name and the job's name are two strings. The ad hoc path used one for both,
    so a nameless caller would have created a group called "<name> - health check".
  * The group it mints is the same object POST /api/channel-groups mints - name-conflict
    checked, CHANNEL_GROUPED events on every member, hiding recomputed. It used to be a
    second-class one with none of those.

Plus the two reads the flow needs before it writes anything: each picked channel's measured
format (which the channel search's own row payload deliberately does not carry), and the
intro screen's dismissal, which is a VERSION in the generic user-prefs store rather than a
boolean in localStorage.
"""
import os
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tests.support.app import make_test_app  # noqa: E402
from tests.support import seed  # noqa: E402
from app.database import (ChannelEvent, ChannelGroup, ChannelGroupMember,  # noqa: E402
                          OnDemandTestJob, CHANNEL_GROUPED)


class _Base(unittest.TestCase):

    def setUp(self):
        self.t = make_test_app()
        self.t.app.config['WTF_CSRF_ENABLED'] = False
        self.client = self.t.client
        self.ctx = self.t.app.app_context()
        self.ctx.push()
        self.acc = seed.make_account()
        self.chans = [seed.make_channel(self.acc, name=f'Fox Sports 1 {i}') for i in range(3)]
        self.ids = [c.id for c in self.chans]

    def tearDown(self):
        self.ctx.pop()
        self.t.cleanup()

    def _jobs(self):
        """The jobs this test made. The pinned system TV Guide check exists in every app,
        so a bare .one() would be answering about it half the time."""
        return OnDemandTestJob.query.filter_by(is_system=False).all()

    def _create(self, **body):
        base = {'channel_ids': self.ids, 'group_name': 'Fox Sports 1', 'action': 'queue'}
        base.update(body)
        return self.client.post('/api/channel-tests/on-demand', json=base)


class NamelessCreateTests(_Base):
    """The flow posts no `name` at all - there is no field to type one into."""

    def test_a_request_with_no_name_creates_both_and_derives_the_jobs_name(self):
        resp = self._create()
        self.assertEqual(resp.status_code, 200)
        group = ChannelGroup.query.filter_by(name='Fox Sports 1').one()
        job = self._jobs()[0]
        self.assertEqual(job.group_id, group.id)
        self.assertEqual(job.name, 'Fox Sports 1 - health check')

    def test_the_group_takes_the_group_name_not_the_derived_job_name(self):
        """One string for both is what made this need two keys: the group would have come
        out called "Fox Sports 1 - health check"."""
        self._create()
        self.assertIsNotNone(ChannelGroup.query.filter_by(name='Fox Sports 1').first())
        self.assertIsNone(
            ChannelGroup.query.filter_by(name='Fox Sports 1 - health check').first())

    def test_attaching_to_an_existing_group_is_refused_because_it_already_has_one(self):
        """dev/changelog/1077: every group carries its one check, so the attach shape is
        answered with the group's page rather than a second job."""
        group = seed.make_group(name='Already Here', members=self.chans)
        before = len(self._jobs())
        resp = self.client.post('/api/channel-tests/on-demand',
                                json={'attach_group_id': group.id, 'action': 'queue'})
        self.assertEqual(resp.status_code, 409)
        self.assertEqual(len(self._jobs()), before)

    def test_a_request_with_neither_is_still_refused(self):
        """Nothing to derive from is the one unanswerable case, and it is the only one
        that may 400 - anything else would be an error the user cannot act on."""
        resp = self.client.post('/api/channel-tests/on-demand',
                                json={'channel_ids': self.ids, 'action': 'queue'})
        self.assertEqual(resp.status_code, 400)
        self.assertIn('name', resp.get_json()['error'].lower())

    def test_an_explicit_name_names_the_group_when_there_is_no_group_name(self):
        """The ad hoc "Test this channel" path has no group to derive from and still sends
        its own name: that name becomes the minted group's, and the check is named after
        the group like every check (dev/changelog/1077)."""
        resp = self._create(name='2026-08-27 09:15 - fox', group_name=None)
        self.assertEqual(resp.status_code, 200)
        self.assertIsNotNone(ChannelGroup.query.filter_by(name='2026-08-27 09:15 - fox').first())
        self.assertEqual(self._jobs()[0].name, '2026-08-27 09:15 - fox - health check')

    def test_the_group_name_wins_over_an_explicit_name(self):
        resp = self._create(name='2026-08-27 09:15 - fox')
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(self._jobs()[0].name, 'Fox Sports 1 - health check')


class MintedGroupTests(_Base):
    """The group this route mints is the same object POST /api/channel-groups mints."""

    def test_every_member_gets_its_grouped_event(self):
        """A member's own timeline recorded nothing when it was grouped this way, so
        "when did this channel join a group" had no answer on the one path a real user
        group now comes through."""
        self._create()
        events = ChannelEvent.query.filter_by(event_type=CHANNEL_GROUPED).all()
        self.assertEqual({e.channel_id for e in events}, set(self.ids))
        for e in events:
            self.assertIn('Fox Sports 1', e.detail)

    def test_membership_keeps_the_order_it_was_given(self):
        self._create()
        group = ChannelGroup.query.filter_by(name='Fox Sports 1').one()
        rows = sorted(ChannelGroupMember.query.filter_by(group_id=group.id).all(),
                      key=lambda m: m.position)
        self.assertEqual([m.channel_id for m in rows], self.ids)

    def test_members_arrive_recording_off_and_health_check_on(self):
        """The whole of DESIGN-channel-groups-model.md 14's "created as a health check"."""
        self._create()
        group = ChannelGroup.query.filter_by(name='Fox Sports 1').one()
        for m in ChannelGroupMember.query.filter_by(group_id=group.id).all():
            self.assertFalse(m.recording_enabled)
            self.assertTrue(m.test_enabled)

    def test_a_name_already_taken_is_refused_rather_than_duplicated(self):
        """POST /api/channel-groups refuses a collision; this path silently made a second
        group whose name already meant something else."""
        existing = seed.make_group(name='Fox Sports 1', members=self.chans[:1])
        resp = self._create()
        self.assertEqual(resp.status_code, 409)
        self.assertEqual(ChannelGroup.query.filter_by(name='Fox Sports 1').count(), 1)
        self.assertEqual([j.id for j in self._jobs()], [existing.check.id],
                         'only the existing group\'s own check; nothing was minted')

    def test_the_collision_check_is_case_insensitive_like_the_other_path(self):
        seed.make_group(name='fox sports 1', members=self.chans[:1])
        self.assertEqual(self._create().status_code, 409)


class CreateResponseTests(_Base):
    """The closing toast links at the group this same request minted, so the id has to
    come back - it is knowable nowhere else."""

    def test_the_response_carries_the_group_it_made(self):
        data = self._create().get_json()
        group = ChannelGroup.query.filter_by(name='Fox Sports 1').one()
        self.assertTrue(data['success'])
        self.assertEqual(data['group_id'], group.id)
        self.assertEqual(data['group_name'], 'Fox Sports 1')
        self.assertEqual(data['detail_url'], f'/channel-groups/{group.id}')

    def test_the_refusal_carries_the_page_of_the_group_that_already_has_a_check(self):
        group = seed.make_group(name='Already Here', members=self.chans)
        data = self.client.post('/api/channel-tests/on-demand',
                                json={'attach_group_id': group.id, 'action': 'queue'}).get_json()
        self.assertEqual(data['detail_url'], f'/channel-groups/{group.id}')

    def test_a_scheduled_create_still_carries_it(self):
        """The flow's recommended path is a recurring schedule, so this is the branch the
        toast is actually built from most of the time."""
        with patch('app.scheduler.schedule_on_demand_job', return_value=('aps-1', None)):
            data = self._create(action='schedule', recurring=True, recur_day=0,
                                use_window=True).get_json()
        self.assertEqual(data['group_id'],
                         ChannelGroup.query.filter_by(name='Fox Sports 1').one().id)


class MeasuredFormatsTests(_Base):
    """GET /api/channel-tests/formats - what the picked-channel list names and what the
    one true warning buckets the selection with."""

    def _url(self, ids):
        return '/api/channel-tests/formats?channel_ids=' + ','.join(str(i) for i in ids)

    def test_a_tested_channel_reports_its_latest_measurement(self):
        seed.make_channel_test(self.chans[0], status='COMPLETED', resolution='1280x720', fps=59.94)
        seed.make_channel_test(self.chans[0], status='COMPLETED', resolution='1920x1080', fps=30.0)
        data = self.client.get(self._url(self.ids)).get_json()
        self.assertEqual(data['formats'][str(self.chans[0].id)],
                         {'resolution': '1920x1080', 'fps': 30.0})

    def test_an_untested_channel_is_absent_rather_than_null(self):
        """Absent is what lets the caller tell "unknown" from "proven different" - those
        are not the same claim and only the second is ever filtered on."""
        seed.make_channel_test(self.chans[0], status='COMPLETED', resolution='1280x720', fps=59.94)
        data = self.client.get(self._url(self.ids)).get_json()
        self.assertNotIn(str(self.chans[1].id), data['formats'])

    def test_a_test_that_measured_nothing_is_absent_too(self):
        """A FAILED check produces a row but no resolution - that is still no format, not
        a format of ''."""
        seed.make_channel_test(self.chans[0], status='FAILED')
        data = self.client.get(self._url(self.ids)).get_json()
        self.assertNotIn(str(self.chans[0].id), data['formats'])

    def test_junk_ids_are_dropped_rather_than_500ing(self):
        resp = self.client.get('/api/channel-tests/formats?channel_ids=,abc,%20,7x')
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.get_json()['formats'], {})

    def test_an_oversized_request_is_refused(self):
        resp = self.client.get(self._url(range(5001)))
        self.assertEqual(resp.status_code, 400)


class IntroDismissalTests(_Base):
    """The intro screen's dismissal. A VERSION, not a boolean, so a release that changes
    what the screen says can show it once more - and server state, not localStorage, so it
    follows the user between browsers."""

    def test_the_page_starts_at_zero_when_nothing_was_ever_dismissed(self):
        html = self.client.get('/channels?q=fox').get_data(as_text=True)
        self.assertIn('groupIntroSeenVersion: 0', html)

    def test_a_dismissal_round_trips_into_the_next_page_load(self):
        self.client.post('/api/user-prefs/group_intro_seen_version', json={'value': 1})
        html = self.client.get('/channels?q=fox').get_data(as_text=True)
        self.assertIn('groupIntroSeenVersion: 1', html)

    def test_the_rendered_page_carries_it(self):
        self.client.post('/api/user-prefs/group_intro_seen_version', json={'value': 3})
        html = self.client.get('/channels?q=fox').get_data(as_text=True)
        self.assertIn('groupIntroSeenVersion: 3', html)


class SelectionBarTests(_Base):
    """One primary action on the bar, and only one. Two buttons that did substantially the
    same job - and neither styled as the primary - is what this whole flow replaced."""

    def test_the_bar_no_longer_offers_a_separate_bulk_test(self):
        html = self.client.get('/channels?q=fox').get_data(as_text=True)
        self.assertNotIn('id="sel-test"', html)

    def test_the_group_action_is_the_bars_only_primary(self):
        html = self.client.get('/channels?q=fox').get_data(as_text=True)
        bar = html[html.index('id="sel-bar"'):html.index('id="sel-guide-note"')]
        primaries = [line for line in bar.splitlines() if 'btn-primary' in line]
        # The action context's own "+ Add Channels" is a different bar state - it is shown
        # only while adding to a named group and never alongside the selection actions.
        primaries = [line for line in primaries if 'sel-ctx-add' not in line]
        self.assertEqual(len(primaries), 1, primaries)
        self.assertIn('id="sel-group"', primaries[0])

    def test_the_flows_script_is_loaded_after_the_modal_it_borrows_from(self):
        """It draws the server's soft warnings through group-modal.js's own builder, so
        the two cannot paraphrase one response differently."""
        html = self.client.get('/channels?q=fox').get_data(as_text=True)
        self.assertLess(html.index('js/group-modal.js'), html.index('js/group-create-flow.js'))


if __name__ == '__main__':
    unittest.main()
