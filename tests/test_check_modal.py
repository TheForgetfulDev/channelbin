"""Tier 2 - the server half of the redesigned "Create a health check" modal
(dev/changelog/321, implemented in dev/changelog/325).

The modal is one shared static/js/check-modal.js opened from the Groups list and from the
group detail page. Everything it renders without a fetch has to be handed to it by the
server, so these guard the payload rather than the markup:

  * `health_check_profile_payload()` folds each HealthCheckProfile over the global
    channel_testing defaults ONCE per request and says, per field, which values fell back
    to a default (that is what earns the grey `default` pill). The trap it exists to stop
    is the `screenshots_enabled: false` tri-state: an explicit False is a profile value,
    not an unset one, and reading it as "unset" would show the wrong readout AND the wrong
    pill on the one setting a user is most likely to turn off deliberately.
  * Both page contexts actually ship that payload plus the busy-tester flag. The list page
    had no notion of tester state at all before this, so its Run-now option could not
    render its disabled state.
  * `inherited_check` - the "already covered by the TV Guide check" notice - exists on the
    DETAIL page. It only ever existed on the list page, which is why the two copies of the
    modal disagreed in the first place.
  * The three POST bodies the modal sends (`queue`, `start`, `schedule` recurring and
    one-off) do what the modal's copy promises against the real, unchanged endpoint. No
    new endpoint was added for this feature and this is what proves it did not need one.
"""
import os
import sys
import unittest
from datetime import datetime, timedelta
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tests.support.app import make_test_app  # noqa: E402
from tests.support import seed  # noqa: E402
from app import db  # noqa: E402
from app.database import ChannelGroup, HealthCheckProfile, OnDemandTestJob  # noqa: E402
from app.channel_tester import health_check_profile_payload  # noqa: E402
from app.routes.channel_groups import build_group_detail_context  # noqa: E402


CT_CFG = {
    'test_duration_seconds': 120,
    'wait_between_channels_seconds': 180,
    'screenshots_enabled': True,
    'connect_retries': 2,
    'connect_timeout_seconds': 15,
    'connect_retry_delay_seconds': 10,
}

ALL_FIELDS = set(CT_CFG)


class _Base(unittest.TestCase):

    def setUp(self):
        self.t = make_test_app()
        self.t.app.config['WTF_CSRF_ENABLED'] = False
        self.client = self.t.client
        self.ctx = self.t.app.app_context()
        self.ctx.push()
        self.acc = seed.make_account()

    def tearDown(self):
        self.ctx.pop()
        self.t.cleanup()

    def _profile(self, name='Quick', **fields):
        p = HealthCheckProfile(name=name, **fields)
        db.session.add(p)
        db.session.commit()
        return p


class ProfileFoldTests(_Base):
    """The readout is a pure render of this payload - if the fold is wrong, the modal
    tells the user something the run will not do."""

    def test_no_profile_entry_is_all_defaults(self):
        payload = health_check_profile_payload(CT_CFG, [])
        self.assertEqual(len(payload['profiles']), 1)
        entry = payload['profiles'][0]
        self.assertIsNone(entry['id'])
        self.assertEqual(entry['settings'], CT_CFG)
        self.assertEqual(set(entry['from_default']), ALL_FIELDS)
        self.assertEqual(payload['defaults'], CT_CFG)

    def test_profile_fields_override_and_unset_fields_fall_back(self):
        p = self._profile(test_duration_seconds=45)
        entry = health_check_profile_payload(CT_CFG, [p])['profiles'][1]
        self.assertEqual(entry['id'], p.id)
        self.assertEqual(entry['settings']['test_duration_seconds'], 45)
        self.assertEqual(entry['settings']['wait_between_channels_seconds'], 180)
        self.assertNotIn('test_duration_seconds', entry['from_default'])
        self.assertIn('wait_between_channels_seconds', entry['from_default'])

    def test_screenshots_enabled_false_is_a_profile_value_not_an_unset_one(self):
        """The tri-state trap: False is a set value. Treating it as "unset" would both
        show `Yes` in the readout and tag it `default`, on the one setting a user turns
        off on purpose."""
        p = self._profile(name='No shots', screenshots_enabled=False)
        entry = health_check_profile_payload(CT_CFG, [p])['profiles'][1]
        self.assertIs(entry['settings']['screenshots_enabled'], False)
        self.assertNotIn('screenshots_enabled', entry['from_default'])

    def test_zero_valued_profile_fields_are_profile_values(self):
        """Same class as the False case: 0 retries / 0s wait are real settings."""
        p = self._profile(name='Zeroes', connect_retries=0, wait_between_channels_seconds=0)
        entry = health_check_profile_payload(CT_CFG, [p])['profiles'][1]
        self.assertEqual(entry['settings']['connect_retries'], 0)
        self.assertEqual(entry['settings']['wait_between_channels_seconds'], 0)
        self.assertNotIn('connect_retries', entry['from_default'])
        self.assertNotIn('wait_between_channels_seconds', entry['from_default'])

    def test_defaults_come_from_the_config_dict_it_is_handed(self):
        """It must never reach for load_config() itself - the caller hoists that once
        per request (CLAUDE.md no-hidden-I/O)."""
        payload = health_check_profile_payload({'test_duration_seconds': 7}, [])
        self.assertEqual(payload['defaults']['test_duration_seconds'], 7)


class PagePayloadTests(_Base):
    """Both surfaces open the same modal, so both have to ship it the same data."""

    def _group(self, name='G', n=2, in_guide=True):
        chans = [seed.make_channel(self.acc, name=f'{name} ch{i}', in_guide=in_guide)
                 for i in range(n)]
        return seed.make_group(name=name, members=chans, in_guide=in_guide), chans

    def test_groups_list_ships_profiles_and_tester_state(self):
        self._profile(name='Thorough', test_duration_seconds=300)
        self._group()
        html = self.client.get('/channel-groups').get_data(as_text=True)
        self.assertIn('checkProfiles:', html)
        self.assertIn('Thorough', html)
        self.assertIn('testerBusy:', html)
        self.assertIn('cc-schedule-fields', html)

    def test_group_detail_ships_profiles(self):
        self._profile(name='Thorough', test_duration_seconds=300)
        grp, _ = self._group(name='Detail group')
        with self.t.app.test_request_context():
            ctx = build_group_detail_context(grp, None)
        names = [p['name'] for p in ctx['check_profiles']['profiles']]
        self.assertEqual(names[0], 'Global defaults (no profile)')
        self.assertIn('Thorough', names)

    def test_detail_page_knows_about_inherited_tv_guide_coverage(self):
        """This is the notice the detail copy of the modal never had."""
        system = ChannelGroup(name='TV Guide Channels', is_system=True)
        db.session.add(system)
        db.session.commit()
        db.session.add(OnDemandTestJob(name='TV Guide Channels', group_id=system.id,
                                       status='SCHEDULED', recurring=True,
                                       recur_day=1, recur_hour=3, recur_minute=0))
        db.session.commit()
        grp, _ = self._group(name='Guide group', in_guide=True)
        with self.t.app.test_request_context():
            ctx = build_group_detail_context(grp, None)
        self.assertIsNotNone(ctx['inherited_check'])
        self.assertEqual(ctx['inherited_check']['channel_count'], 2)
        self.assertTrue(ctx['inherited_check']['recur_description'])

    def test_a_group_out_of_the_guide_inherits_coverage_from_the_fallback(self):
        """Premise inverted by dev/changelog/752. Being out of the guide used to mean the
        automatic check could not be covering you; the scheduleless-group fallback means
        it covers one member of any group that carries no schedule of its own, guide row
        or no guide row. What ends inheritance now is having your own schedule."""
        system = ChannelGroup(name='TV Guide Channels', is_system=True)
        db.session.add(system)
        db.session.commit()
        db.session.add(OnDemandTestJob(name='TV Guide Channels', group_id=system.id,
                                       status='SCHEDULED', recurring=True))
        db.session.commit()
        grp, _ = self._group(name='Offguide group', in_guide=False)
        with self.t.app.test_request_context():
            ctx = build_group_detail_context(grp, None)
        self.assertIsNotNone(ctx['inherited_check'])
        self.assertEqual(ctx['inherited_check']['channel_count'], 1,
                         'one member on the group\'s behalf, never the whole membership')

    def test_no_inherited_coverage_once_the_group_has_a_schedule_of_its_own(self):
        system = ChannelGroup(name='TV Guide Channels', is_system=True)
        db.session.add(system)
        db.session.commit()
        db.session.add(OnDemandTestJob(name='TV Guide Channels', group_id=system.id,
                                       status='SCHEDULED', recurring=True))
        db.session.commit()
        grp, _ = self._group(name='Self-scheduled group', in_guide=False)
        db.session.add(OnDemandTestJob(name='Own check', group_id=grp.id,
                                       status='SCHEDULED', recurring=True,
                                       recur_day=1, recur_hour=3, recur_minute=0))
        db.session.commit()
        with self.t.app.test_request_context():
            ctx = build_group_detail_context(grp, None)
        self.assertIsNone(ctx['inherited_check'])

    def test_channels_browse_page_ships_profiles_and_tester_state(self):
        """A third caller (fableUI #4 chunk 2, the Browse tab's "Test selected") - the
        browse page has no channel group of its own, so it ships the same
        health_check_profile_payload() list as a page-level global rather than a
        per-group readout, for the modal's ad hoc (channelIds) mode. It rides the search
        page's own config object now that the search is the Browse tab
        (dev/docs/DESIGN-channel-search.md), so the key names moved - what must not change
        is that the list and the tester's busy state are both in the first paint."""
        self._profile(name='Thorough', test_duration_seconds=300)
        seed.make_channel(self.acc, name='Some channel')
        html = self.client.get('/channels').get_data(as_text=True)
        self.assertIn('checkProfiles', html)
        self.assertIn('Thorough', html)
        self.assertIn('testerBusy', html)
        self.assertIn('cc-schedule-fields', html)


class PostBodyTests(_Base):
    """The exact bodies static/js/check-modal.js::ccPayload builds, against the real
    endpoint. `attach_group_id` + `profile_id` are always sent; the schedule keys are
    added only for action='schedule'.

    CHARACTERIZATION, not a regression guard: the endpoint is deliberately unchanged by
    this feature, so every method here passes against the pre-redesign code too. They are
    here to pin the contract the new modal now depends on - the schedule path in
    particular had no test at all before, which is why the UI could go years without
    offering it and nobody noticed the backend was ready."""

    def setUp(self):
        super().setUp()
        chans = [seed.make_channel(self.acc, name=f'ch{i}') for i in range(2)]
        self.grp = seed.make_group(name='Target', members=chans)
        self.profile = self._profile(name='Quick', test_duration_seconds=30)

    def _post(self, **body):
        base = {'name': 'Target - health check', 'attach_group_id': self.grp.id,
                'profile_id': self.profile.id}
        base.update(body)
        return self.client.post('/api/channel-tests/on-demand', json=base)

    def test_save_for_later_queues_without_running(self):
        resp = self._post(action='queue')
        self.assertEqual(resp.status_code, 200)
        job = OnDemandTestJob.query.filter_by(name='Target - health check').one()
        self.assertEqual(job.status, 'QUEUED')
        self.assertEqual(job.group_id, self.grp.id)
        self.assertEqual(job.profile_id, self.profile.id)
        self.assertFalse(job.recurring)

    def test_run_now_starts_the_run(self):
        with patch('app.routes.channel_tests._start_job_run') as start:
            resp = self._post(action='start')
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(start.called)

    def test_run_now_is_refused_while_another_check_runs(self):
        """The modal disables this option when the tester is busy, but the 409 is the
        real gate - another tab can always beat it."""
        with patch('app.channel_tester.get_status', return_value={'is_running': True}):
            resp = self._post(action='start')
        self.assertEqual(resp.status_code, 409)
        self.assertIsNone(OnDemandTestJob.query.filter_by(name='Target - health check').first())

    def test_schedule_recurring_stores_the_recurrence(self):
        run_at = datetime.utcnow() + timedelta(days=1)
        with patch('app.scheduler.schedule_on_demand_job', return_value=('aps-1', run_at)):
            resp = self._post(action='schedule', recurring=True, recur_day=1, recur_time='03:00')
        self.assertEqual(resp.status_code, 200)
        job = OnDemandTestJob.query.filter_by(name='Target - health check').one()
        self.assertEqual(job.status, 'SCHEDULED')
        self.assertTrue(job.recurring)
        self.assertEqual((job.recur_day, job.recur_hour, job.recur_minute), (1, 3, 0))

    def test_schedule_one_off_stores_the_moment(self):
        future = (datetime.utcnow() + timedelta(days=2)).replace(microsecond=0)
        with patch('app.scheduler.schedule_on_demand_job', return_value=('aps-2', future)):
            resp = self._post(action='schedule', recurring=False,
                              scheduled_time=future.strftime('%Y-%m-%dT%H:%M'))
        self.assertEqual(resp.status_code, 200)
        job = OnDemandTestJob.query.filter_by(name='Target - health check').one()
        self.assertEqual(job.status, 'SCHEDULED')
        self.assertFalse(job.recurring)
        self.assertIsNotNone(job.scheduled_start_time)

    def test_empty_group_is_refused(self):
        """The modal disables its button and says why; the 400 is what actually enforces
        it (CLAUDE.md "Enforcement lives server-side")."""
        empty = seed.make_group(name='Empty', members=[])
        resp = self.client.post('/api/channel-tests/on-demand',
                                json={'name': 'Empty - health check',
                                      'attach_group_id': empty.id, 'profile_id': None,
                                      'action': 'queue'})
        self.assertEqual(resp.status_code, 400)

    def test_null_profile_id_means_global_defaults(self):
        resp = self._post(action='queue', profile_id=None)
        self.assertEqual(resp.status_code, 200)
        job = OnDemandTestJob.query.filter_by(name='Target - health check').one()
        self.assertIsNone(job.profile_id)


if __name__ == '__main__':
    unittest.main()
