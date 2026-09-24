"""A group's TV Guide row takes its listings from a chain of members, and the user can pin
which member leads it (dev/changelog/1116).

  * FillListingsTests - the pure stitcher: the first channel's listings go in whole, and
    each later channel fills only time nothing earlier covers, never overlapping it.
  * GuideRowListingsTests - /api/guide/epg: a lead member with one day of listings no
    longer holds a three-day row to one day (dev/docs/BUGS.md 2026-09-24 @ 11:46:16 AM), a
    pin leads the row whatever member records, and a pin on a channel that left the group
    is ignored.
  * GuideListingsRouteTests - POST /api/channel-groups/<id>/guide-listings validates its
    input, writes through the one writer and logs GROUP_GUIDE_LISTINGS_SET, and the
    member-removal route clears a pin on the channel it removes.
"""
import json
import os
import sys
import unittest
from datetime import datetime, timedelta
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tests.support.app import make_test_app  # noqa: E402
from tests.support import seed  # noqa: E402
from app import db  # noqa: E402
from app.channel_groups import fill_listings  # noqa: E402
from app.database import (ChannelGroup, ChannelGroupEvent, ChannelGroupMember,  # noqa: E402
                          GROUP_GUIDE_LISTINGS_SET)

T0 = datetime(2026, 9, 24, 0, 0, 0)


def _e(channel_id, start_h, dur_h=1, title=None):
    start = T0 + timedelta(hours=start_h)
    return SimpleNamespace(channel_id=channel_id, start_time=start,
                           stop_time=start + timedelta(hours=dur_h),
                           title=title or f'{channel_id}@{start_h}')


class FillListingsTests(unittest.TestCase):
    def test_later_channel_fills_the_tail_the_first_does_not_cover(self):
        by = {1: [_e(1, 0), _e(1, 1)], 2: [_e(2, 0), _e(2, 1), _e(2, 2), _e(2, 3)]}
        out = fill_listings([1, 2], by)
        self.assertEqual([(e.channel_id, e.start_time.hour) for e in out],
                         [(1, 0), (1, 1), (2, 2), (2, 3)])

    def test_later_channel_fills_a_gap_in_the_middle_and_at_the_start(self):
        by = {1: [_e(1, 1), _e(1, 3)], 2: [_e(2, h) for h in range(5)]}
        out = fill_listings([1, 2], by)
        self.assertEqual([(e.channel_id, e.start_time.hour) for e in out],
                         [(2, 0), (1, 1), (2, 2), (1, 3), (2, 4)])

    def test_a_program_overlapping_placed_time_is_dropped_not_trimmed(self):
        # The first channel ends at 01:00; the second's 00:30-01:30 program straddles that
        # boundary and must not appear with invented times or on top of the first's.
        by = {1: [_e(1, 0)],
              2: [_e(2, 0.5), _e(2, 1.5)]}
        out = fill_listings([1, 2], by)
        self.assertEqual([(e.channel_id, e.start_time, e.stop_time) for e in out],
                         [(1, T0, T0 + timedelta(hours=1)),
                          (2, T0 + timedelta(hours=1.5), T0 + timedelta(hours=2.5))])

    def test_first_channel_is_taken_whole_even_where_it_overlaps_itself(self):
        by = {1: [_e(1, 0, 2), _e(1, 1)]}
        self.assertEqual(len(fill_listings([1, 2], by)), 2)

    def test_empty_chain_and_missing_channels(self):
        self.assertEqual(fill_listings([], {1: [_e(1, 0)]}), [])
        self.assertEqual(fill_listings([7, 8], {}), [])


class _GroupFixture(unittest.TestCase):
    def setUp(self):
        self.t = make_test_app()
        # CSRF-protected API POSTs, and the token is not what these tests are about.
        self.t.app.config['WTF_CSRF_ENABLED'] = False
        self.client = self.t.client
        self.acc = seed.make_account()
        # Lead is the healthier member, so it is the one the row records from.
        self.lead = seed.make_channel(self.acc, stream_id=1, name='Lead Feed', health_score=90)
        self.long = seed.make_channel(self.acc, stream_id=2, name='Long Feed', health_score=40)
        self.grp = seed.make_group(name='Grp', members=[self.lead, self.long])
        db.session.commit()
        self.gid = self.grp.id

    def tearDown(self):
        self.t.cleanup()

    def _pin(self, channel_id):
        return self.client.post(f'/api/channel-groups/{self.gid}/guide-listings',
                                data=json.dumps({'channel_id': channel_id}),
                                content_type='application/json')

    def _events(self):
        db.session.expire_all()
        return (ChannelGroupEvent.query
                .filter_by(group_id=self.gid, event_type=GROUP_GUIDE_LISTINGS_SET)
                .order_by(ChannelGroupEvent.id).all())


class GuideRowListingsTests(_GroupFixture):
    def setUp(self):
        super().setUp()
        self.start = datetime.utcnow().replace(minute=0, second=0, microsecond=0)
        # One day on the lead, three on the other member.
        for h in range(24):
            seed.make_epg_entry(self.lead, title=f'Lead {h}',
                                start_time=self.start + timedelta(hours=h))
        for h in range(72):
            seed.make_epg_entry(self.long, title=f'Long {h}',
                                start_time=self.start + timedelta(hours=h))
        db.session.commit()

    def _row(self):
        end = self.start + timedelta(hours=72)
        r = self.client.get('/api/guide/epg?start=' + self.start.strftime('%Y-%m-%dT%H:%M:%S')
                            + '&end=' + end.strftime('%Y-%m-%dT%H:%M:%S'))
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        return next(c for c in r.get_json()['channels'] if c['id'] == f'g{self.gid}')

    def test_short_lead_no_longer_truncates_the_row(self):
        """dev/docs/BUGS.md 2026-09-24 @ 11:46:16 AM - a lead with one day of listings held
        the whole three-day row to one day, because the fallback fired only when the lead
        had no listing at all in the window."""
        row = self._row()
        self.assertEqual(row['active_channel_id'], self.lead.id)
        titles = [p['title'] for p in row['programs']]
        self.assertEqual(titles, [f'Lead {h}' for h in range(24)]
                         + [f'Long {h}' for h in range(24, 72)])

    def test_borrowed_listings_are_named_on_the_row_and_on_each_program(self):
        row = self._row()
        self.assertEqual(row['listings_from'], ['Long Feed'])
        self.assertIsNone(row['listings_pinned_name'])
        by_title = {p['title']: p for p in row['programs']}
        self.assertIsNone(by_title['Lead 3']['listings_from'])
        self.assertEqual(by_title['Long 30']['listings_from'], 'Long Feed')
        # The row still records from the lead, whoever's listing the cell is.
        self.assertEqual(by_title['Long 30']['channel_id'], self.lead.id)

    def test_a_pin_leads_the_row_without_changing_who_records(self):
        self.assertEqual(self._pin(self.long.id).status_code, 200)
        row = self._row()
        self.assertEqual(row['active_channel_id'], self.lead.id)
        self.assertEqual([p['title'] for p in row['programs']],
                         [f'Long {h}' for h in range(72)])
        self.assertEqual(row['listings_pinned_name'], 'Long Feed')
        self.assertTrue(all(p['channel_id'] == self.lead.id for p in row['programs']))

    def test_a_pin_on_a_channel_that_left_the_group_is_ignored(self):
        self._pin(self.long.id)
        # A cascade path with nobody present: the membership goes, the pin stays stored.
        ChannelGroupMember.query.filter_by(group_id=self.gid, channel_id=self.long.id).delete()
        db.session.commit()
        row = self._row()
        self.assertIsNone(row['listings_pinned_name'])
        self.assertEqual([p['title'] for p in row['programs']], [f'Lead {h}' for h in range(24)])
        page = self.client.get(f'/channel-groups/{self.gid}')
        self.assertEqual(page.status_code, 200)
        self.assertIn('"stale": true', page.get_data(as_text=True))


class GuideListingsRouteTests(_GroupFixture):
    def test_pin_writes_the_column_and_logs_one_event(self):
        r = self._pin(self.long.id)
        self.assertEqual(r.status_code, 200)
        body = r.get_json()
        self.assertTrue(body['success'])
        self.assertTrue(body['moved'])
        self.assertEqual(body['channel_id'], self.long.id)
        db.session.expire_all()
        self.assertEqual(db.session.get(ChannelGroup, self.gid).guide_listings_channel_id,
                         self.long.id)
        events = self._events()
        self.assertEqual(len(events), 1)
        self.assertIn('Long Feed', events[0].detail)
        self.assertEqual(json.loads(events[0].extra_data)['reason'], 'user')

    def test_repeating_the_same_pin_is_a_no_op_with_no_event(self):
        self._pin(self.long.id)
        r = self._pin(self.long.id)
        self.assertFalse(r.get_json()['moved'])
        self.assertEqual(len(self._events()), 1)

    def test_null_sets_it_back_to_automatic(self):
        self._pin(self.long.id)
        r = self._pin(None)
        self.assertTrue(r.get_json()['moved'])
        db.session.expire_all()
        self.assertIsNone(db.session.get(ChannelGroup, self.gid).guide_listings_channel_id)
        self.assertEqual(len(self._events()), 2)

    def test_bad_input_is_refused(self):
        outsider = seed.make_channel(self.acc, stream_id=9, name='Outsider')
        db.session.commit()
        self.assertEqual(self._pin(outsider.id).status_code, 400)
        self.assertEqual(self._pin(True).status_code, 400)
        self.assertEqual(self._pin('2').status_code, 400)
        self.assertEqual(self.client.post('/api/channel-groups/99999/guide-listings',
                                          data='{}', content_type='application/json')
                         .status_code, 404)
        self.assertEqual(self._events(), [])

    def test_removing_the_pinned_member_clears_the_pin_and_says_why(self):
        self._pin(self.long.id)
        r = self.client.post(f'/api/channel-groups/{self.gid}/members/remove',
                             data=json.dumps({'channel_ids': [self.long.id]}),
                             content_type='application/json')
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        db.session.expire_all()
        self.assertIsNone(db.session.get(ChannelGroup, self.gid).guide_listings_channel_id)
        last = self._events()[-1]
        self.assertEqual(json.loads(last.extra_data)['reason'], 'removed')
        self.assertIn('Long Feed', last.detail)

    def test_removing_another_member_leaves_the_pin_alone(self):
        third = seed.make_channel(self.acc, stream_id=3, name='Third')
        db.session.add(ChannelGroupMember(group_id=self.gid, channel_id=third.id, position=2,
                                          recording_enabled=True, test_enabled=True))
        db.session.commit()
        self._pin(self.long.id)
        self.client.post(f'/api/channel-groups/{self.gid}/members/remove',
                         data=json.dumps({'channel_ids': [third.id]}),
                         content_type='application/json')
        db.session.expire_all()
        self.assertEqual(db.session.get(ChannelGroup, self.gid).guide_listings_channel_id,
                         self.long.id)


if __name__ == '__main__':
    unittest.main()
