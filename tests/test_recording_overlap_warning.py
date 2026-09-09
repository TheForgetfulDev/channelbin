"""Scheduling an overlapping recording warns instead of staying silent or refusing
outright (dev/changelog/858).

Before this, the only overlap-aware check on the create/edit paths was
_check_account_connection_limit(): scoped to one account, and a hard 400 with no way to
proceed when it fired. Two overlapping recordings on different accounts - the case that
prompted this - passed in complete silence. Now every create/edit answers
{'success': False, 'overlap_warning': {...}, 'connection_limit_warning': {...}} when the
new window overlaps something else, and a 'force': '1' field lets the user proceed past
either warning. Same-channel and same-group overlap stays silent (a handoff, not a
conflict) exactly as it did before.
"""
import os
import sys
import unittest
from datetime import datetime, timedelta
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tests.support.app import make_test_app  # noqa: E402
from tests.support import seed  # noqa: E402
from app import db  # noqa: E402
from app.database import Recording, REC_STATUS_SCHEDULED  # noqa: E402
from app.tz_utils import local_input_value, parse_local_to_utc  # noqa: E402


class _RecordingOverlapCase(unittest.TestCase):
    def setUp(self):
        self.t = make_test_app()
        self.t.app.config['WTF_CSRF_ENABLED'] = False
        self.start = datetime.utcnow() + timedelta(hours=2)
        self.stop = self.start + timedelta(hours=1)

    def tearDown(self):
        self.t.cleanup()

    def _post_new(self, channel_id=None, group_id=None, start=None, stop=None, **extra):
        data = {
            'name': 'New Recording',
            'url': 'http://example.test/live/new',
            'start_time': local_input_value(start or self.start),
            'stop_time': local_input_value(stop or self.stop),
        }
        if channel_id is not None:
            data['channel_id'] = str(channel_id)
        if group_id is not None:
            data['group_id'] = str(group_id)
        data.update(extra)
        with mock.patch('app.routes.recordings.schedule_recording'):
            return self.t.client.post('/recordings/new-json', data=data)


class CrossAccountOverlapWarnsTests(_RecordingOverlapCase):
    """The silent case the item exists to fix: two overlapping recordings on different
    accounts used to pass with no notice at all."""

    def setUp(self):
        super().setUp()
        self.acct_a = seed.make_account(name='Account A')
        self.acct_b = seed.make_account(name='Account B')
        self.ch_a = seed.make_channel(self.acct_a, name='Channel A')
        self.ch_b = seed.make_channel(self.acct_b, name='Channel B')
        db.session.commit()
        self.existing = seed.make_recording(
            status=REC_STATUS_SCHEDULED, name='Existing Show', channel_id=self.ch_a.id,
            start_time=self.start, stop_time=self.stop)
        db.session.commit()

    def test_unforced_overlap_warns_and_creates_nothing(self):
        resp = self._post_new(channel_id=self.ch_b.id)
        self.assertEqual(resp.status_code, 200)
        payload = resp.get_json()
        self.assertFalse(payload['success'])
        self.assertIn('overlap_warning', payload)
        conflict_ids = {c['recording_id'] for c in payload['overlap_warning']['conflicts']}
        self.assertIn(self.existing.id, conflict_ids)
        self.assertEqual(Recording.query.filter_by(name='New Recording').count(), 0)

    def test_forced_overlap_proceeds(self):
        resp = self._post_new(channel_id=self.ch_b.id, force='1')
        self.assertEqual(resp.status_code, 200)
        payload = resp.get_json()
        self.assertTrue(payload['success'])
        self.assertEqual(Recording.query.filter_by(name='New Recording').count(), 1)

    def test_non_overlapping_window_is_silent(self):
        later_start = self.stop + timedelta(hours=1)
        later_stop = later_start + timedelta(hours=1)
        resp = self._post_new(channel_id=self.ch_b.id, start=later_start, stop=later_stop)
        self.assertEqual(resp.status_code, 200)
        payload = resp.get_json()
        self.assertTrue(payload['success'])


class HandoffOverlapIsSilentTests(_RecordingOverlapCase):
    """Same-channel and same-group overlap is a handoff, not a conflict - no warning.

    max_connections=2: with the default of 1, new_recording_json's own account-rollover
    logic (dev/changelog/855) would reassign the group recording away from the busy
    member before this check ever runs, which would test the rollover instead of the
    warning. Two slots keeps both members eligible so the supplied member is kept as-is.
    """

    def setUp(self):
        super().setUp()
        self.acct = seed.make_account(name='Handoff Account', max_connections=2)
        self.ch1 = seed.make_channel(self.acct, name='Member 1')
        self.ch2 = seed.make_channel(self.acct, name='Member 2')
        db.session.commit()

    def test_same_channel_overlap_is_silent(self):
        seed.make_recording(status=REC_STATUS_SCHEDULED, name='Earlier',
                            channel_id=self.ch1.id,
                            start_time=self.start, stop_time=self.stop)
        db.session.commit()
        resp = self._post_new(channel_id=self.ch1.id)
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(resp.get_json()['success'])

    def test_same_group_overlap_across_different_members_is_silent(self):
        group = seed.make_group(name='Fox Sports', members=[self.ch1, self.ch2])
        db.session.commit()
        seed.make_recording(status=REC_STATUS_SCHEDULED, name='Earlier',
                            channel_id=self.ch1.id, group_id=group.id,
                            start_time=self.start, stop_time=self.stop)
        db.session.commit()
        resp = self._post_new(channel_id=self.ch2.id, group_id=group.id)
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(resp.get_json()['success'])


class ConnectionLimitIsNowASoftWarningTests(_RecordingOverlapCase):
    """_check_account_connection_limit()'s hard 400 is now folded into the same
    proceedable warning (dev/changelog/854 made runtime enforcement the real ceiling, so
    this schedule-time check has nothing left to protect by refusing)."""

    def setUp(self):
        super().setUp()
        self.acct = seed.make_account(name='One Slot Account', max_connections=1)
        self.ch1 = seed.make_channel(self.acct, name='Busy Feed')
        self.ch2 = seed.make_channel(self.acct, name='New Feed')
        db.session.commit()
        self.existing = seed.make_recording(
            status=REC_STATUS_SCHEDULED, name='Existing Show', channel_id=self.ch1.id,
            start_time=self.start, stop_time=self.stop)
        db.session.commit()

    def test_unforced_over_limit_warns_not_400s(self):
        resp = self._post_new(channel_id=self.ch2.id)
        self.assertEqual(resp.status_code, 200)
        payload = resp.get_json()
        self.assertFalse(payload['success'])
        self.assertIn('connection_limit_warning', payload)
        self.assertIn('connection limit', payload['connection_limit_warning']['message'])
        self.assertEqual(Recording.query.filter_by(name='New Recording').count(), 0)

    def test_forced_over_limit_proceeds(self):
        resp = self._post_new(channel_id=self.ch2.id, force='1')
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(resp.get_json()['success'])
        self.assertEqual(Recording.query.filter_by(name='New Recording').count(), 1)


class EditPathWarnsTests(_RecordingOverlapCase):
    """The edit path gets the same treatment - re-verified against the recording's own
    stored channel_id/group_id, never re-resolved (that half is out of scope here)."""

    def setUp(self):
        super().setUp()
        self.acct_a = seed.make_account(name='Account A')
        self.acct_b = seed.make_account(name='Account B')
        self.ch_a = seed.make_channel(self.acct_a, name='Channel A')
        self.ch_b = seed.make_channel(self.acct_b, name='Channel B')
        db.session.commit()
        self.blocker = seed.make_recording(
            status=REC_STATUS_SCHEDULED, name='Blocker', channel_id=self.ch_a.id,
            start_time=self.start, stop_time=self.stop)
        self.editable = seed.make_recording(
            status=REC_STATUS_SCHEDULED, name='Editable', channel_id=self.ch_b.id,
            start_time=self.start + timedelta(hours=5),
            stop_time=self.stop + timedelta(hours=5))
        db.session.commit()

    def _edit(self, **extra):
        data = {
            'name': 'Editable',
            'url': 'http://example.test/live/edit',
            'start_time': local_input_value(self.start),
            'stop_time': local_input_value(self.stop),
        }
        data.update(extra)
        return self.t.client.post(f'/recordings/{self.editable.id}/edit-json', data=data)

    def test_unforced_edit_into_overlap_warns_and_does_not_change_the_row(self):
        original_start = self.editable.start_time
        resp = self._edit()
        self.assertEqual(resp.status_code, 200)
        payload = resp.get_json()
        self.assertFalse(payload['success'])
        self.assertIn('overlap_warning', payload)
        db.session.expire_all()
        rec = db.session.get(Recording, self.editable.id)
        self.assertEqual(rec.start_time, original_start)

    def test_forced_edit_into_overlap_applies(self):
        # Form fields round-trip through a minute-precision datetime-local input, so
        # compare against the same round trip rather than self.start directly.
        expected_start = parse_local_to_utc(local_input_value(self.start))
        with mock.patch('app.routes.recordings.unschedule_recording'), \
             mock.patch('app.routes.recordings.schedule_recording'):
            resp = self._edit(force='1')
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(resp.get_json()['success'])
        db.session.expire_all()
        rec = db.session.get(Recording, self.editable.id)
        self.assertEqual(rec.start_time, expected_start)


if __name__ == '__main__':
    unittest.main()
