"""Tier 2 - the unified "Clone" action (dev/changelog/542) and its
GET /api/channel-groups/<id>/clone-info seed-data route.

The clone flow itself is client-side JS (clone-modal.js -> create-group-modal.js /
clone-check-modal.js -> check-modal.js), reusing the already-covered
POST /api/channel-groups/<id>/clone (tests/test_channel_groups.py) and
POST /api/channel-tests/on-demand (tests/test_check_modal.py) endpoints. What's new and
worth testing directly: clone-info's seed-data shape, and the kebab's single "Clone" item
- present exactly once regardless of what role(s) the group plays, absent for the system
group, and the old broken bare clone and the separate "Clone health check..." action are
both gone.
"""
import os
import re
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tests.support.app import make_test_app  # noqa: E402
from tests.support import seed  # noqa: E402
from app import db  # noqa: E402
from app.database import OnDemandTestJob  # noqa: E402
from app.routes.channel_groups import clone_info  # noqa: E402


class CloneKebabTests(unittest.TestCase):
    def setUp(self):
        self.t = make_test_app()
        self.acc = seed.make_account()

    def tearDown(self):
        self.t.cleanup()

    def test_present_once_for_a_plain_health_check(self):
        ch = seed.make_channel(self.acc, stream_id=1, name='Solo')
        job = seed.make_test_job(channels=[ch])
        db.session.commit()
        body = self.t.client.get(f'/channels/health-checks/{job.id}').get_data(as_text=True)
        self.assertEqual(body.count('data-act="clone"'), 1)
        self.assertNotIn('data-act="clone-check"', body)

    def test_present_once_for_a_plain_channel_group(self):
        ch = seed.make_channel(self.acc, stream_id=1, name='Solo')
        grp = seed.make_group(name='Failover Group', members=[ch])
        db.session.commit()
        body = self.t.client.get(f'/channel-groups/{grp.id}').get_data(as_text=True)
        self.assertEqual(body.count('data-act="clone"'), 1)

    def test_present_once_for_a_group_carrying_a_schedule(self):
        # A group with its own attached (non-inherited) health check is still one entity -
        # the kebab must offer exactly one Clone, not one per role.
        ch = seed.make_channel(self.acc, stream_id=1, name='Solo')
        grp = seed.make_group(name='Pair Group', members=[ch])
        job = OnDemandTestJob(name='Pair Check', status='QUEUED', group_id=grp.id)
        db.session.add(job)
        db.session.commit()
        body = self.t.client.get(f'/channel-groups/{grp.id}').get_data(as_text=True)
        self.assertEqual(body.count('data-act="clone"'), 1)

    def test_absent_for_the_system_job(self):
        # is_system lives on the GROUP, not just the job - build it the way
        # test_format_plan_routes.py's equivalent case does.
        seed.make_channel(self.acc, stream_id=1, name='Solo', in_guide=True, test_enabled=True)
        sys_grp = seed.make_group(name='TV Guide Channels', is_system=True,
                                  recording=False, in_guide=False)
        job = OnDemandTestJob(name='TV Guide Channels', status='QUEUED',
                              group_id=sys_grp.id, is_system=True)
        db.session.add(job)
        db.session.commit()
        body = self.t.client.get(f'/channels/health-checks/{job.id}').get_data(as_text=True)
        self.assertNotIn('data-act="clone"', body)


class CloneInfoRouteTests(unittest.TestCase):
    def setUp(self):
        self.t = make_test_app()
        self.acc = seed.make_account()

    def tearDown(self):
        self.t.cleanup()

    def test_plain_channel_group_shape(self):
        ch = seed.make_channel(self.acc, stream_id=1, name='Solo')
        grp = seed.make_group(name='Chan Group', members=[ch], in_guide=True)
        db.session.commit()
        resp = self.t.client.get(f'/api/channel-groups/{grp.id}/clone-info')
        self.assertEqual(resp.status_code, 200)
        data = resp.get_json()
        # `kind` and `linked_pair` are gone from this payload: there is one kind of group,
        # and what the modal actually needs to know is whether a schedule comes with it
        # (dev/changelog/741).
        self.assertNotIn('kind', data)
        self.assertFalse(data['has_schedule'])
        self.assertIsNotNone(data['channel_settings'])
        self.assertTrue(data['channel_settings']['in_guide'])
        self.assertEqual('health_check_only', data['channel_settings']['format_strategy'])
        self.assertIsNone(data['check'])
        self.assertEqual([c['id'] for c in data['channels']], [ch.id])

    def test_a_groups_settings_are_always_offered(self):
        """Every group has guide and format settings now - there is no second kind for
        which they would be meaningless, so `channel_settings` is never None."""
        ch = seed.make_channel(self.acc, stream_id=1, name='Solo')
        job = seed.make_test_job(name='Nightly', channels=[ch])
        db.session.commit()
        resp = self.t.client.get(f'/api/channel-groups/{job.group_id}/clone-info')
        self.assertEqual(resp.status_code, 200)
        data = resp.get_json()
        self.assertTrue(data['has_schedule'])
        self.assertIsNotNone(data['channel_settings'])
        self.assertIsNotNone(data['check'])
        self.assertEqual(data['check']['job_id'], job.id)

    def test_a_group_with_a_schedule_reports_both_halves(self):
        ch = seed.make_channel(self.acc, stream_id=1, name='Solo')
        grp = seed.make_group(name='Pair Group', members=[ch])
        job = OnDemandTestJob(name='Pair Check', status='QUEUED', group_id=grp.id)
        db.session.add(job)
        db.session.commit()
        resp = self.t.client.get(f'/api/channel-groups/{grp.id}/clone-info')
        self.assertEqual(resp.status_code, 200)
        data = resp.get_json()
        self.assertTrue(data['has_schedule'])
        self.assertIsNotNone(data['channel_settings'])
        self.assertIsNotNone(data['check'])
        self.assertEqual(data['check']['job_id'], job.id)

    def test_the_docstring_documents_only_fields_the_route_returns(self):
        """A caller writes against this route's docstring, not against the diff, so a
        response field that outlives its key there is a defect of its own: `linked_pair`
        was deleted from the payload by dev/changelog/741 and stayed in the prose. The
        convention this holds to is stated in the docstring itself - a backticked
        lowercase name there is a field of the response, so anything else quoted in it
        (a filename, a helper) carries no backticks.
        """
        ch = seed.make_channel(self.acc, stream_id=1, name='Solo')
        grp = seed.make_group(name='Pair Group', members=[ch])
        db.session.add(OnDemandTestJob(name='Pair Check', status='QUEUED', group_id=grp.id))
        db.session.commit()
        data = self.t.client.get(f'/api/channel-groups/{grp.id}/clone-info').get_json()

        fields = set(data)
        for nested in (data['channel_settings'], data['check'], *data['channels']):
            fields.update(nested)
        documented = set(re.findall(r'`([a-z][a-z0-9_]*)`', clone_info.__doc__))
        self.assertEqual(set(), documented - fields,
                         'the docstring names response fields the route does not return')

    def test_system_group_400s(self):
        seed.make_channel(self.acc, stream_id=1, name='Solo', in_guide=True, test_enabled=True)
        sys_grp = seed.make_group(name='TV Guide Channels', is_system=True,
                                  recording=False, in_guide=False)
        db.session.commit()
        resp = self.t.client.get(f'/api/channel-groups/{sys_grp.id}/clone-info')
        self.assertEqual(resp.status_code, 400)


if __name__ == '__main__':
    unittest.main()
