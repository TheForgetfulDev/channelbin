"""Scheduling several showings at once from the EPG search's airings list
(dev/changelog/1157).

`/api/recordings/bulk-preview` and `/api/recordings/bulk-schedule` share one planner, so the
preview is what the create does. Covered here: each showing becomes its own ordinary
SCHEDULED recording through the single create path; showings that cannot be scheduled are
skipped with a reason rather than dropped; a time overlap is a soft warning and an overlap on
one account without a free connection is a hard one - counting the OTHER showings in the same
request, not only recordings already stored; a group showing is scheduled as the group; a
failure part way names the showing and leaves the rest created; and bad input is a 400.
"""
import os
import sys
import unittest
from datetime import timedelta
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tests.support.app import make_test_app  # noqa: E402
from tests.support import seed  # noqa: E402
from app import db  # noqa: E402
from app.database import (  # noqa: E402
    Recording, RecordingEvent, RecordingProfile, RECORDING_CREATED_AFTER_EVENT_START,
    REC_STATUS_SCHEDULED, REC_STATUS_COMPLETED,
)


class _BulkCase(unittest.TestCase):
    def setUp(self):
        self.t = make_test_app()
        self.t.app.config['WTF_CSRF_ENABLED'] = False
        self.acct = seed.make_account(name='Solo', max_connections=1)
        self.other = seed.make_account(name='Other', max_connections=1)
        self.ch1 = seed.make_channel(self.acct, name='One')
        self.ch2 = seed.make_channel(self.acct, name='Two')
        self.ch3 = seed.make_channel(self.other, name='Three')
        db.session.commit()

    def tearDown(self):
        self.t.cleanup()

    def _post(self, path, items, profile='default'):
        body = {'items': items, 'profile': profile}
        with mock.patch('app.routes.recordings.schedule_recording') as sched:
            resp = self.t.client.post(f'/api/recordings/{path}', json=body)
        self.sched = sched
        return resp

    def _items(self, *entries, group=None):
        return [{'epg_id': e.id, **({'group_id': group.id} if group else {})} for e in entries]


class CreatesEachShowingTests(_BulkCase):

    def test_each_showing_becomes_one_scheduled_recording(self):
        a = seed.make_epg_entry(self.ch1, title='Alpha', offset_minutes=60)
        b = seed.make_epg_entry(self.ch3, title='Beta', offset_minutes=180)
        db.session.commit()
        resp = self._post('bulk-schedule', self._items(a, b))
        self.assertEqual(resp.status_code, 200)
        data = resp.get_json()
        self.assertTrue(data['success'])
        self.assertEqual(len(data['created']), 2)
        self.assertEqual(data['failed'], [])
        recs = Recording.query.order_by(Recording.id).all()
        self.assertEqual([r.channel_id for r in recs], [self.ch1.id, self.ch3.id])
        self.assertTrue(all(r.status == REC_STATUS_SCHEDULED for r in recs))
        self.assertEqual([r.program_title for r in recs], ['Alpha', 'Beta'])
        self.assertEqual(recs[0].start_time, a.start_time)
        self.assertEqual(self.sched.call_count, 2, 'every recording arms its own start')

    def test_the_profile_applies_to_every_showing_with_its_padding(self):
        prof = RecordingProfile(name='Padded', pre_padding_minutes=2, post_padding_minutes=5)
        db.session.add(prof)
        a = seed.make_epg_entry(self.ch1, offset_minutes=60)
        b = seed.make_epg_entry(self.ch3, offset_minutes=180)
        db.session.commit()
        self._post('bulk-schedule', self._items(a, b), profile=prof.id)
        for rec, entry in zip(Recording.query.order_by(Recording.id).all(), (a, b)):
            self.assertEqual(rec.profile_id, prof.id)
            self.assertEqual(rec.start_time, entry.start_time - timedelta(minutes=2))
            self.assertEqual(rec.stop_time, entry.stop_time + timedelta(minutes=5))

    def test_default_uses_each_channels_own_default_profile(self):
        prof = RecordingProfile(name='Chan default', post_padding_minutes=10)
        db.session.add(prof)
        db.session.flush()
        self.ch1.default_profile_id = prof.id
        a = seed.make_epg_entry(self.ch1, offset_minutes=60)
        b = seed.make_epg_entry(self.ch3, offset_minutes=180)
        db.session.commit()
        self._post('bulk-schedule', self._items(a, b))
        by_ch = {r.channel_id: r for r in Recording.query.all()}
        self.assertEqual(by_ch[self.ch1.id].profile_id, prof.id)
        self.assertIsNone(by_ch[self.ch3.id].profile_id)

    def test_a_showing_already_on_air_starts_now_and_says_so(self):
        a = seed.make_epg_entry(self.ch1, offset_minutes=-10, duration_minutes=60)
        db.session.commit()
        self._post('bulk-schedule', self._items(a))
        rec = Recording.query.one()
        self.assertGreater(rec.start_time, a.start_time)
        self.assertEqual(RecordingEvent.query.filter_by(
            recording_id=rec.id, event_type=RECORDING_CREATED_AFTER_EVENT_START).count(), 1)

    def test_preview_writes_nothing(self):
        a = seed.make_epg_entry(self.ch1, offset_minutes=60)
        db.session.commit()
        data = self._post('bulk-preview', self._items(a)).get_json()
        self.assertEqual(data['plans'][0]['verdict'], 'ok')
        self.assertEqual(Recording.query.count(), 0)
        self.assertNotIn('_create', data['plans'][0])


class SkipsTests(_BulkCase):
    def test_already_scheduled_ended_and_missing_are_skipped_with_a_reason(self):
        sched = seed.make_epg_entry(self.ch1, title='Taken', offset_minutes=60)
        ended = seed.make_epg_entry(self.ch2, title='Gone', offset_minutes=-120)
        ok = seed.make_epg_entry(self.ch3, title='Fine', offset_minutes=60)
        seed.make_recording(status=REC_STATUS_SCHEDULED, channel_id=self.ch1.id,
                            start_time=sched.start_time, stop_time=sched.stop_time)
        db.session.commit()
        items = self._items(sched, ended, ok) + [{'epg_id': 999999}]
        data = self._post('bulk-schedule', items).get_json()
        self.assertEqual([c['title'] for c in data['created']], ['Fine'])
        reasons = {s['epg_id']: s['reason'] for s in data['skipped']}
        self.assertEqual(reasons[sched.id], 'Already scheduled.')
        self.assertEqual(reasons[ended.id], 'Already over.')
        self.assertIn('no longer in the guide', reasons[999999])
        self.assertEqual(Recording.query.count(), 2)

    def test_a_recorded_showing_can_be_recorded_again(self):
        """Parity with the row's own Re-record button."""
        a = seed.make_epg_entry(self.ch1, offset_minutes=60)
        seed.make_recording(status=REC_STATUS_COMPLETED, channel_id=self.ch1.id,
                            start_time=a.start_time, stop_time=a.stop_time)
        db.session.commit()
        data = self._post('bulk-schedule', self._items(a)).get_json()
        self.assertEqual(len(data['created']), 1)

    def test_a_showing_listed_twice_is_scheduled_once(self):
        a = seed.make_epg_entry(self.ch1, offset_minutes=60)
        db.session.commit()
        data = self._post('bulk-schedule', self._items(a, a)).get_json()
        self.assertEqual(len(data['created']), 1)
        self.assertEqual(len(data['skipped']), 1)
        self.assertEqual(Recording.query.count(), 1)


class WarningsCountTheBatchTests(_BulkCase):
    """The existing checks read stored recordings only. Without counting the request's own
    earlier picks, two overlapping showings on a one-connection account would each preview
    clean and both be scheduled into a collision nobody was told about."""

    def test_two_overlapping_picks_on_one_full_account_are_a_hard_warning(self):
        a = seed.make_epg_entry(self.ch1, title='A', offset_minutes=60)
        b = seed.make_epg_entry(self.ch2, title='B', offset_minutes=60)
        db.session.commit()
        plans = self._post('bulk-preview', self._items(a, b)).get_json()['plans']
        self.assertEqual(plans[0]['verdict'], 'ok')
        self.assertEqual(plans[1]['verdict'], 'hard')
        conflict = plans[1]['warnings']['connection_limit_warning']['conflicts'][0]
        self.assertTrue(conflict['in_batch'])
        self.assertEqual(conflict['title'], plans[0]['name'])

    def test_overlap_on_another_account_is_only_a_soft_warning(self):
        a = seed.make_epg_entry(self.ch1, offset_minutes=60)
        c = seed.make_epg_entry(self.ch3, offset_minutes=60)
        db.session.commit()
        plans = self._post('bulk-preview', self._items(a, c)).get_json()['plans']
        self.assertEqual(plans[1]['verdict'], 'warn')
        self.assertIn('overlap_warning', plans[1]['warnings'])
        self.assertNotIn('connection_limit_warning', plans[1]['warnings'])

    def test_back_to_back_on_one_channel_is_a_handoff_not_a_conflict(self):
        a = seed.make_epg_entry(self.ch1, offset_minutes=60, duration_minutes=60)
        b = seed.make_epg_entry(self.ch1, offset_minutes=90, duration_minutes=60)
        db.session.commit()
        plans = self._post('bulk-preview', self._items(a, b)).get_json()['plans']
        self.assertEqual([p['verdict'] for p in plans], ['ok', 'ok'])

    def test_a_stored_recording_still_counts(self):
        a = seed.make_epg_entry(self.ch2, offset_minutes=60)
        seed.make_recording(status=REC_STATUS_SCHEDULED, channel_id=self.ch1.id,
                            start_time=a.start_time, stop_time=a.stop_time)
        db.session.commit()
        plans = self._post('bulk-preview', self._items(a)).get_json()['plans']
        self.assertEqual(plans[0]['verdict'], 'hard')

    def test_warned_showings_are_still_created(self):
        """Warnings are proceedable, as in the single record modal - the preview was the
        confirmation."""
        a = seed.make_epg_entry(self.ch1, offset_minutes=60)
        b = seed.make_epg_entry(self.ch2, offset_minutes=60)
        db.session.commit()
        data = self._post('bulk-schedule', self._items(a, b)).get_json()
        self.assertEqual(len(data['created']), 2)
        self.assertEqual(data['created'][1]['verdict'], 'hard')


class GroupShowingTests(_BulkCase):
    def test_a_group_showing_is_scheduled_as_the_group(self):
        grp = seed.make_group(name='Grp', members=[self.ch1, self.ch3])
        a = seed.make_epg_entry(self.ch1, offset_minutes=60)
        db.session.commit()
        data = self._post('bulk-schedule', self._items(a, group=grp)).get_json()
        self.assertEqual(len(data['created']), 1)
        rec = Recording.query.one()
        self.assertEqual(rec.group_id, grp.id)
        self.assertIn(rec.channel_id, (self.ch1.id, self.ch3.id))

    def test_a_group_rolls_onto_the_account_an_earlier_pick_left_free(self):
        """The group resolver counts the batch too: with One's account taken by the first
        pick, the group's showing lands on Three's account."""
        grp = seed.make_group(name='Grp', members=[self.ch2, self.ch3])
        a = seed.make_epg_entry(self.ch1, offset_minutes=60)
        b = seed.make_epg_entry(self.ch2, offset_minutes=60)
        db.session.commit()
        items = self._items(a) + self._items(b, group=grp)
        plans = self._post('bulk-preview', items).get_json()['plans']
        self.assertEqual(plans[1]['member_name'], 'Three')
        self.assertNotEqual(plans[1]['verdict'], 'hard')

    def test_a_group_the_showing_is_not_in_is_skipped(self):
        grp = seed.make_group(name='Grp', members=[self.ch3])
        a = seed.make_epg_entry(self.ch1, offset_minutes=60)
        db.session.commit()
        data = self._post('bulk-schedule', self._items(a, group=grp)).get_json()
        self.assertEqual(data['created'], [])
        self.assertIn('not on a member', data['skipped'][0]['reason'])


class PartialFailureTests(_BulkCase):
    def test_one_failure_is_named_and_the_rest_are_still_created(self):
        a = seed.make_epg_entry(self.ch1, title='First', offset_minutes=60)
        b = seed.make_epg_entry(self.ch3, title='Second', offset_minutes=60)
        c = seed.make_epg_entry(self.ch2, title='Third', offset_minutes=300)
        db.session.commit()
        from app.routes import recordings as rr
        real = rr._create_scheduled_recording

        def flaky(cfg, **kw):
            if kw['entry'].title == 'Second':
                raise RuntimeError('disk on fire')
            return real(cfg, **kw)

        with mock.patch.object(rr, '_create_scheduled_recording', side_effect=flaky):
            data = self._post('bulk-schedule', self._items(a, b, c)).get_json()
        self.assertEqual([c['title'] for c in data['created']], ['First', 'Third'])
        self.assertEqual(len(data['failed']), 1)
        self.assertEqual(data['failed'][0]['title'], 'Second')
        self.assertIn('disk on fire', data['failed'][0]['error'])
        self.assertEqual(Recording.query.count(), 2)


class BadInputTests(_BulkCase):
    def test_bad_bodies_are_400(self):
        a = seed.make_epg_entry(self.ch1, offset_minutes=60)
        db.session.commit()
        for body in ({}, {'items': []}, {'items': 'x'}, {'items': [{'epg_id': 'x'}]},
                     {'items': [{'epg_id': True}]},
                     {'items': [{'epg_id': a.id, 'group_id': 'g'}]},
                     {'items': [{'epg_id': a.id}], 'profile': 'fancy'},
                     {'items': [{'epg_id': a.id}], 'profile': 999999},
                     {'items': [{'epg_id': a.id}] * 301}):
            for path in ('bulk-preview', 'bulk-schedule'):
                resp = self.t.client.post(f'/api/recordings/{path}', json=body)
                self.assertEqual(resp.status_code, 400, (path, body))
                self.assertIn('error', resp.get_json())
        self.assertEqual(Recording.query.count(), 0)

    def test_profile_none_means_no_profile(self):
        prof = RecordingProfile(name='Chan default', post_padding_minutes=10)
        db.session.add(prof)
        db.session.flush()
        self.ch1.default_profile_id = prof.id
        a = seed.make_epg_entry(self.ch1, offset_minutes=60)
        db.session.commit()
        self._post('bulk-schedule', self._items(a), profile='none')
        rec = Recording.query.one()
        self.assertIsNone(rec.profile_id)
        self.assertEqual(rec.stop_time, a.stop_time)


if __name__ == '__main__':
    unittest.main()

