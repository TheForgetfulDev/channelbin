"""When a group recording's feed dies, the failover tries a member off the dead feed's
provider first, then one off its login, before a member that shares the backend
(app/recorder.py::_members_near and _pick_by_tiers, dev/changelog/1603).

  - a member on another provider is taken ahead of a higher-scoring one on the dead feed's
    provider, its account or its login, and the GROUP_FAILOVER event names what was passed;
  - same provider on another login is taken ahead of the dead feed's own login;
  - a free connection still comes first, and a member on a failing streak is never preferred
    for where it sits;
  - a stall move (demote) ranks as before;
  - with no member sharing the backend nothing changes and nothing is said.

Throwaway temp SQLite DB through make_test_app(); no ffmpeg, no network.

    python3 -m unittest tests.test_failover_backend_preference
"""
import json
import os
import sys
import unittest
from datetime import datetime, timedelta
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tests.support.app import make_test_app  # noqa: E402
from tests.support import seed  # noqa: E402
from app import db, recorder  # noqa: E402
from app import connection_limits as connlim  # noqa: E402
from app.database import (  # noqa: E402
    GROUP_FAILOVER, REC_STATUS_IN_PROGRESS, Provider, Recording, RecordingEvent,
)


class _Case(unittest.TestCase):
    """A recording running on `dead` (account One, on provider Backend), in a group whose
    other members each test adds."""

    def setUp(self):
        self.t = make_test_app()
        self.backend = Provider(name='Backend')
        db.session.add(self.backend)
        db.session.flush()
        self.one = seed.make_account(name='One', max_connections=1,
                                     provider_id=self.backend.id)
        self.dead = seed.make_channel(self.one, name='Dead Feed', health_score=100)
        self.group = seed.make_group(name='Sports', members=[self.dead])
        self.rid = None
        connlim._holders.clear()

    def tearDown(self):
        if self.rid is not None:
            recorder._active.pop(self.rid, None)
        connlim._holders.clear()
        self.t.cleanup()

    def member(self, name, score, account=None, provider=False, **kw):
        """A group member on its own account unless one is given; `provider=True` puts a
        new account on the dead feed's provider."""
        from app.database import ChannelGroupMember
        if account is None:
            account = seed.make_account(name=f'{name} account', max_connections=1,
                                        provider_id=self.backend.id if provider else None)
        ch = seed.make_channel(account, name=name, health_score=score, **kw)
        db.session.add(ChannelGroupMember(
            group_id=self.group.id, channel_id=ch.id, position=ch.id,
            recording_enabled=True, test_enabled=True))
        db.session.flush()
        return ch

    def fail_over(self, **kw):
        now = datetime.utcnow()
        rec = seed.make_recording(
            status=REC_STATUS_IN_PROGRESS, name='live', channel_id=self.dead.id,
            group_id=self.group.id, start_time=now - timedelta(minutes=10),
            stop_time=now + timedelta(hours=1))
        db.session.commit()
        self.rid = rec.id
        self.assertTrue(connlim.try_acquire(self.one.id, 'recording', self.rid))
        recorder._active[self.rid] = recorder.RecordingState(current_segment_num=1)
        self.assertTrue(recorder.failover_group_member(self.t.app, self.rid, 'stream died', **kw))
        db.session.expire_all()
        ev = RecordingEvent.query.filter_by(recording_id=self.rid,
                                            event_type=GROUP_FAILOVER).one()
        return db.session.get(Recording, self.rid).channel_id, ev, json.loads(ev.extra_data)


class FeedDeathTests(_Case):

    def test_another_provider_is_tried_ahead_of_a_higher_score_on_the_dead_feeds_provider(self):
        near = self.member('Same Backend', 95, provider=True)
        far = self.member('Elsewhere', 70)
        to, ev, extra = self.fail_over()
        self.assertEqual(to, far.id)
        self.assertIn('"Same Backend" (Same Backend account, score 95) ranks higher but shares '
                      'its provider with the feed that died', ev.detail)
        self.assertEqual(extra['passed_near_channel_id'], near.id)

    def test_the_dead_feeds_own_account_is_passed_over_too_with_no_provider_set(self):
        self.one.provider_id = None
        near = self.member('Other Copy', 95, account=self.one)
        far = self.member('Elsewhere', 70)
        to, ev, extra = self.fail_over()
        self.assertEqual(to, far.id)
        self.assertIn('shares its account with the feed that died', ev.detail)
        self.assertEqual(extra['passed_near_channel_id'], near.id)

    def test_same_provider_on_another_login_is_tried_ahead_of_the_dead_feeds_login(self):
        sibling = self.member('Shared Login', 95, provider=True)
        apart = self.member('Own Login', 80, provider=True)
        with mock.patch('app.account_links.login_siblings',
                        return_value={self.one.id: {sibling.account_id: [1]}}):
            to, ev, _extra = self.fail_over()
        self.assertEqual(to, apart.id)
        self.assertIn('shares its login with the feed that died', ev.detail)

    def test_a_free_connection_still_comes_first(self):
        near = self.member('Same Backend', 95, provider=True)
        far = self.member('Elsewhere', 70)
        connlim.try_acquire(far.account_id, 'recording', 999999)
        to, ev, extra = self.fail_over()
        self.assertEqual(to, near.id)
        self.assertIsNone(extra['passed_near_channel_id'])
        self.assertNotIn('with the feed that died', ev.detail)

    def test_a_member_on_a_failing_streak_is_never_preferred_for_where_it_sits(self):
        near = self.member('Same Backend', 95, provider=True)
        self.member('Elsewhere', 70, consecutive_test_failures=99)
        to, _ev, extra = self.fail_over()
        self.assertEqual(to, near.id)
        self.assertIsNone(extra['passed_near_channel_id'])

    def test_with_nothing_else_the_shared_backend_is_still_taken(self):
        near = self.member('Same Backend', 95, provider=True)
        to, ev, _extra = self.fail_over()
        self.assertEqual(to, near.id)
        self.assertNotIn('with the feed that died', ev.detail)

    def test_no_member_on_the_backend_changes_nothing_and_says_nothing(self):
        best = self.member('Elsewhere A', 95)
        self.member('Elsewhere B', 70)
        to, ev, extra = self.fail_over()
        self.assertEqual(to, best.id)
        self.assertIsNone(extra['passed_near_channel_id'])
        self.assertNotIn('with the feed that died', ev.detail)


class StallMoveTests(_Case):

    def test_a_stall_move_ranks_by_health_score_alone(self):
        """Nothing died, so nothing is known about the backend."""
        near = self.member('Same Backend', 95, provider=True)
        self.member('Elsewhere', 70)
        to, _ev, extra = self.fail_over(demote=True)
        self.assertEqual(to, near.id)
        self.assertIsNone(extra['passed_near_channel_id'])


if __name__ == '__main__':
    unittest.main()
