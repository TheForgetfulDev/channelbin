"""An account block's "until" time and a one-off health check's time keep the instant they
stand for across a DST change (dev/docs/BUGS.md 2026-11-08 @ 09:26:53 PM ET).

The recording forms have posted the instant beside the wall text since dev/changelog/1380;
these two fields still read bare text with parse_local_to_utc, so on the night DST ends a
time in the repeated 1 AM hour meant the first pass with nothing said, and a skipped 2:30 AM
landed at 3:30. Both routes now read `*_utc` beside the text through
app/tz_utils.py::parse_form_time, the way the recording forms do.

The US fall-back used here is 2027-11-07 (06:00 UTC), as in tests/test_dst_record_form.py.

  python3 -m unittest tests.test_dst_block_and_check_time
"""
import os
import sys
import unittest
from datetime import datetime
from unittest import mock
from zoneinfo import ZoneInfo

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import db, tz_utils  # noqa: E402
from app.database import AccountBlock, ChannelGroup, HealthCheckProfile  # noqa: E402
from app.routes import accounts as accounts_routes  # noqa: E402
from tests.support import seed  # noqa: E402
from tests.support.app import make_test_app  # noqa: E402

NY = ZoneInfo('America/New_York')
FIRST_130 = datetime(2027, 11, 7, 5, 30)    # 1:30 AM EDT
SECOND_130 = datetime(2027, 11, 7, 6, 30)   # 1:30 AM EST
NOW = datetime(2027, 11, 7, 5, 0)           # 1:00 AM EDT, an hour before the clocks go back


class _FrozenDatetime(datetime):
    """The route's `datetime.utcnow()`, held on the night of the change so a block "until
    1:30 AM" is in the future and inside the 24-hour bound."""
    @classmethod
    def utcnow(cls):
        return NOW


def _ny():
    return mock.patch.object(tz_utils, 'get_display_tz', return_value=NY)


class BlockUntilTests(unittest.TestCase):
    def setUp(self):
        self.t = make_test_app(csrf=False)
        self.acc = seed.make_account(name='TV Account', max_connections=1)
        db.session.commit()

    def tearDown(self):
        self.t.cleanup()

    def _block(self, **body):
        with _ny(), mock.patch.object(accounts_routes, 'datetime', _FrozenDatetime):
            resp = self.t.client.post(f'/api/accounts/{self.acc.id}/blocks', json=body)
        self.assertEqual(resp.status_code, 200, resp.get_json())
        return AccountBlock.query.filter_by(account_id=self.acc.id).one()

    def test_until_in_the_second_1am_hour_ends_the_block_in_it(self):
        block = self._block(until='2027-11-07T01:30', until_utc='2027-11-07T06:30:00.000Z')
        self.assertEqual(block.stop_time, SECOND_130)

    def test_bare_wall_text_still_reads_as_the_first_pass(self):
        block = self._block(until='2027-11-07T01:30')
        self.assertEqual(block.stop_time, FIRST_130)


class ScheduledCheckTimeTests(unittest.TestCase):
    def setUp(self):
        self.t = make_test_app(csrf=False)
        self.acc = seed.make_account()
        self.chans = [seed.make_channel(self.acc, name=f'ch{i}') for i in range(2)]
        self.profile = HealthCheckProfile(name='Quick', test_duration_seconds=30)
        db.session.add(self.profile)
        db.session.commit()

    def tearDown(self):
        self.t.cleanup()

    def _schedule(self, **body):
        base = {'group_name': 'Target', 'channel_ids': [c.id for c in self.chans],
                'profile_id': self.profile.id, 'action': 'schedule', 'recurring': False}
        base.update(body)
        # The scheduler echoes the registered run back and the route stores that, so the
        # stand-in answers with the moment the route just parsed - as the real one would.
        with _ny(), mock.patch('app.scheduler.schedule_on_demand_job',
                               side_effect=lambda job: ('aps-1', job.scheduled_start_time)):
            resp = self.t.client.post('/api/channel-tests/on-demand', json=base)
        self.assertEqual(resp.status_code, 200, resp.get_json())
        return ChannelGroup.query.filter_by(name='Target').one().check

    def test_a_check_in_the_second_1am_hour_is_scheduled_in_it(self):
        job = self._schedule(scheduled_time='2027-11-07T01:30',
                             scheduled_time_utc='2027-11-07T06:30:00.000Z')
        self.assertEqual(job.scheduled_start_time, SECOND_130)

    def test_bare_wall_text_still_reads_as_the_first_pass(self):
        job = self._schedule(scheduled_time='2027-11-07T01:30')
        self.assertEqual(job.scheduled_start_time, FIRST_130)


if __name__ == '__main__':
    unittest.main()
