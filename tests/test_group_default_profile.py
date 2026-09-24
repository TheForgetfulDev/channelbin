"""A channel group can carry a default recording profile that the record modal
pre-selects for a showing on the group's row (dev/changelog/1117).

  * GroupDefaultProfileRouteTests - POST /api/channel-groups/<id>/default-profile validates
    its input, writes through the one writer and logs GROUP_DEFAULT_PROFILE_SET.
  * PreselectTests - /api/guide/epg and the search page's record-context endpoint hand the
    modal the group's default over the serving member's own, fall back to the member's when
    the group sets none, and name the recording with the group profile's filename template.
  * ProfileDeleteTests - deleting the profile clears the group's default through the writer,
    so the group's timeline says why.
"""
import json
import os
import sys
import unittest
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tests.support.app import make_test_app  # noqa: E402
from tests.support import seed  # noqa: E402
from app import db  # noqa: E402
from app.database import (ChannelGroup, ChannelGroupEvent, RecordingProfile,  # noqa: E402
                          GROUP_DEFAULT_PROFILE_SET)


class _Fixture(unittest.TestCase):
    def setUp(self):
        self.t = make_test_app()
        self.t.app.config['WTF_CSRF_ENABLED'] = False
        self.client = self.t.client
        self.acc = seed.make_account()
        self.member_profile = RecordingProfile(name='Member Default')
        self.group_profile = RecordingProfile(name='Sports', filename_template='SPORTS {title}')
        db.session.add_all([self.member_profile, self.group_profile])
        db.session.flush()
        self.lead = seed.make_channel(self.acc, stream_id=1, name='Lead Feed', health_score=90)
        self.lead.default_profile_id = self.member_profile.id
        self.other = seed.make_channel(self.acc, stream_id=2, name='Other Feed', health_score=40)
        self.grp = seed.make_group(name='Grp', members=[self.lead, self.other])
        db.session.commit()
        self.gid = self.grp.id
        self.gp_id = self.group_profile.id
        self.mp_id = self.member_profile.id

    def tearDown(self):
        self.t.cleanup()

    def _set(self, profile_id):
        return self.client.post(f'/api/channel-groups/{self.gid}/default-profile',
                                data=json.dumps({'profile_id': profile_id}),
                                content_type='application/json')

    def _events(self):
        db.session.expire_all()
        return (ChannelGroupEvent.query
                .filter_by(group_id=self.gid, event_type=GROUP_DEFAULT_PROFILE_SET)
                .order_by(ChannelGroupEvent.id).all())

    def _stored(self):
        db.session.expire_all()
        return db.session.get(ChannelGroup, self.gid).default_profile_id


class GroupDefaultProfileRouteTests(_Fixture):
    def test_set_writes_the_column_and_logs_one_event(self):
        r = self._set(self.gp_id)
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        body = r.get_json()
        self.assertTrue(body['moved'])
        self.assertEqual(body['profile_name'], 'Sports')
        self.assertEqual(self._stored(), self.gp_id)
        events = self._events()
        self.assertEqual(len(events), 1)
        self.assertIn('Sports', events[0].detail)
        self.assertEqual(json.loads(events[0].extra_data)['reason'], 'user')

    def test_repeat_is_a_no_op_and_null_clears(self):
        self._set(self.gp_id)
        self.assertFalse(self._set(self.gp_id).get_json()['moved'])
        self.assertTrue(self._set(None).get_json()['moved'])
        self.assertIsNone(self._stored())
        self.assertEqual(len(self._events()), 2)

    def test_bad_input_is_refused(self):
        self.assertEqual(self._set(99999).status_code, 400)
        self.assertEqual(self._set(True).status_code, 400)
        self.assertEqual(self._set(str(self.gp_id)).status_code, 400)
        self.assertEqual(self.client.post('/api/channel-groups/99999/default-profile',
                                          data='{}', content_type='application/json')
                         .status_code, 404)
        self.assertEqual(self._events(), [])

    def test_group_page_renders_the_setting(self):
        self._set(self.gp_id)
        page = self.client.get(f'/channel-groups/{self.gid}')
        self.assertEqual(page.status_code, 200)
        html = page.get_data(as_text=True)
        self.assertIn('"profile_name": "Sports"', html)
        self.assertIn('recordingProfiles:', html)


class PreselectTests(_Fixture):
    def setUp(self):
        super().setUp()
        self.start = datetime.utcnow().replace(minute=0, second=0, microsecond=0)
        self.entry = seed.make_epg_entry(self.lead, title='Big Game', start_time=self.start)
        db.session.commit()
        self.entry_id = self.entry.id

    def _row(self):
        end = self.start + timedelta(hours=3)
        r = self.client.get('/api/guide/epg?start=' + self.start.strftime('%Y-%m-%dT%H:%M:%S')
                            + '&end=' + end.strftime('%Y-%m-%dT%H:%M:%S'))
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        return next(c for c in r.get_json()['channels'] if c['id'] == f'g{self.gid}')

    def _context(self, group=True):
        url = f'/api/channels/airings/{self.entry_id}/record-context'
        if group:
            url += f'?group={self.gid}'
        r = self.client.get(url)
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        return r.get_json()

    def test_group_row_falls_back_to_the_serving_members_default(self):
        self.assertEqual(self._row()['default_profile_id'], self.mp_id)
        self.assertEqual(self._context()['channel']['default_profile_id'], self.mp_id)

    def test_group_default_wins_on_the_guide_row(self):
        self._set(self.gp_id)
        row = self._row()
        self.assertEqual(row['default_profile_id'], self.gp_id)
        game = next(p for p in row['programs'] if p['title'] == 'Big Game')
        self.assertTrue(game['suggested_name'].startswith('SPORTS Big Game'),
                        game['suggested_name'])

    def test_group_default_wins_in_search_only_for_the_group_row(self):
        self._set(self.gp_id)
        data = self._context()
        self.assertEqual(data['channel']['default_profile_id'], self.gp_id)
        self.assertTrue(data['program']['suggested_name'].startswith('SPORTS Big Game'))
        # The same showing opened from the channel's own row keeps the channel's default.
        self.assertEqual(self._context(group=False)['channel']['default_profile_id'],
                         self.mp_id)


class ProfileDeleteTests(_Fixture):
    def test_deleting_the_profile_clears_the_group_default_and_says_why(self):
        self._set(self.gp_id)
        r = self.client.delete(f'/api/profiles/{self.gp_id}')
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        self.assertIsNone(self._stored())
        last = self._events()[-1]
        self.assertEqual(json.loads(last.extra_data)['reason'], 'profile_deleted')
        self.assertIn('Sports', last.detail)

    def test_profiles_page_counts_groups(self):
        self._set(self.gp_id)
        html = self.client.get('/profiles').get_data(as_text=True)
        self.assertIn('1 group', html)


if __name__ == '__main__':
    unittest.main()
