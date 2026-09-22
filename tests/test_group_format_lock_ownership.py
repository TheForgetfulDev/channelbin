"""Tier 2 - the two halves of dev/changelog/762: adding a channel is never refused over
its format, and a stored format lock belongs to the `manual` strategy and to nothing else.

Guards, in order:
  * create_group and add_members give the same answer to the same question. Until this
    shipped, grouping mixed-format channels from the Browse tab was refused outright
    while creating the group empty and adding the identical channels one request later
    succeeded - two answers to one question, both visible to the user. Since
    dev/changelog/1077 the question is gated on the group recording from somebody, so a
    new group - which records from nobody - is never asked at all.
  * A group nobody records from - every new group, and the shape of the sweep case
    (DESIGN-channel-groups-model.md 7) - is asked no format or duplicate question at all.
  * POST /format writes the `manual` strategy along with the pin, so pinning is one
    request that cannot strand half-done.
  * clone_group refuses to store a pin under a strategy that owns none, and reports it.
  * apply_format_strategy clears a pin it finds under a disowning strategy and logs why -
    the recovery half, for groups already carrying one.

Runs against a throwaway temp SQLite DB - never the live dvr.db.
  python3 -m unittest tests.test_group_format_lock_ownership
"""
import json
import os
import sys
import unittest
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tests.support import make_test_app  # noqa: E402
from tests.support.seed import make_account, make_channel, make_group  # noqa: E402
from app import db  # noqa: E402
from app.database import (  # noqa: E402
    ChannelGroup, ChannelGroupEvent, ChannelTest,
    GROUP_FORMAT_HIGHEST_SCORE, GROUP_FORMAT_MANUAL,
    GROUP_FORMAT_MOST_CHANNELS, GROUP_FORMAT_STRATEGY_APPLIED, GROUP_FORMAT_UNMANAGED,
)


class _Base(unittest.TestCase):
    def setUp(self):
        self.t = make_test_app()
        self.t.app.config['WTF_CSRF_ENABLED'] = False
        self.client = self.t.client
        self.acct = make_account()

    def tearDown(self):
        self.t.cleanup()

    def _measured(self, name, resolution, fps):
        ch = make_channel(self.acct, name=name)
        db.session.add(ChannelTest(channel_id=ch.id, test_started_at=datetime.utcnow(),
                                   status='COMPLETED', resolution=resolution, fps=fps))
        db.session.commit()
        return ch

    def _mixed_pair(self):
        return (self._measured('HD feed', '1920x1080', 60.0),
                self._measured('SD feed', '1280x720', 30.0))


class CreateAndAddGiveTheSameAnswerTests(_Base):
    """The defect a user could hit by doing something ordinary: the Browse tab's
    "Group Selected" refused a mixed selection that adding one request later accepted."""

    def test_creating_a_default_group_from_mixed_channels_just_works(self):
        hd, sd = self._mixed_pair()
        resp = self.client.post('/api/channel-groups',
                                json={'name': 'Sweep', 'channel_ids': [hd.id, sd.id]})
        self.assertEqual(200, resp.status_code, resp.get_data(as_text=True))
        self.assertTrue(resp.get_json()['success'], resp.get_json())
        grp = ChannelGroup.query.filter_by(name='Sweep').one()
        self.assertEqual(GROUP_FORMAT_HIGHEST_SCORE, grp.format_strategy)
        self.assertFalse(any(m.recording_enabled for m in grp.memberships))
        self.assertEqual({hd.id, sd.id},
                         {m.channel_id for m in grp.memberships})

    def test_create_and_add_agree_on_a_default_group(self):
        """The symmetry itself: the same channels, the same group, the same answer
        whether they arrive at creation or one request later."""
        hd, sd = self._mixed_pair()
        created = self.client.post('/api/channel-groups',
                                   json={'name': 'A', 'channel_ids': [hd.id, sd.id]})
        empty = self.client.post('/api/channel-groups', json={'name': 'B'})
        gid = empty.get_json()['group_id']
        added = self.client.post(f'/api/channel-groups/{gid}/members',
                                 json={'channel_ids': [hd.id, sd.id]})
        self.assertEqual(created.status_code, added.status_code)
        self.assertEqual(created.get_json()['success'], added.get_json()['success'])

    def test_a_sweep_group_is_asked_no_duplicate_question_either(self):
        """The duplicate warning says duplicate feeds "add no failover redundancy", which
        is a sentence about a group that records. A sweep is the default shape of a new
        group (DESIGN-channel-groups-model.md 7), so it rides the same strategy gate the
        format question does - otherwise the most ordinary thing a user does with this app
        stops to ask a question that does not apply to it."""
        a = make_channel(self.acct, name='Dup A')
        b = make_channel(self.acct, name='Dup B')
        b.stream_url = a.stream_url
        db.session.commit()
        resp = self.client.post('/api/channel-groups',
                                json={'name': 'Sweep', 'channel_ids': [a.id, b.id]})
        self.assertEqual(200, resp.status_code, resp.get_data(as_text=True))
        self.assertTrue(resp.get_json()['success'], resp.get_json())

    def test_creating_a_group_from_mixed_channels_never_warns_whatever_the_strategy(self):
        """A new group records from nobody whatever strategy it is given, so the format
        question belongs to its promotion (dev/changelog/1077). The question is still
        asked where it means something - adding to a group that records - as a warning
        with a way through, never a refusal."""
        hd, sd = self._mixed_pair()
        resp = self.client.post('/api/channel-groups',
                                json={'name': 'Records', 'channel_ids': [hd.id, sd.id],
                                      'format_strategy': GROUP_FORMAT_MOST_CHANNELS})
        self.assertEqual(200, resp.status_code, resp.get_data(as_text=True))
        self.assertTrue(resp.get_json()['success'], resp.get_json())
        grp = ChannelGroup.query.filter_by(name='Records').one()
        self.assertEqual({hd.id, sd.id}, {m.channel_id for m in grp.memberships})

    def test_adding_to_a_group_that_records_still_warns(self):
        hd, sd = self._mixed_pair()
        anchor = self._measured('Anchor', '1920x1080', 60.0)
        grp = make_group(name='Records', members=[anchor], recording=True)
        db.session.commit()
        resp = self.client.post(f'/api/channel-groups/{grp.id}/members',
                                json={'channel_ids': [hd.id, sd.id]})
        self.assertEqual(200, resp.status_code, resp.get_data(as_text=True))
        body = resp.get_json()
        self.assertFalse(body['success'])
        self.assertIn('format_mismatch', body)


class PinningIsOneRequestTests(_Base):
    """16.2's rule is "one strategy owns the lock, and it is manual". The pin used to be
    two sequential requests, so a failed second one stranded it under the old strategy."""

    def _group_on(self, strategy):
        # With a recording member: the Settings-side format writers refuse a group nobody
        # records from (dev/changelog/1077), and pinning is a Settings-side write.
        grp = make_group(name='Grp', members=[self._measured('Anchor', '1920x1080', 60.0)],
                         recording=True)
        grp.format_strategy = strategy
        db.session.commit()
        return grp

    def test_pinning_a_format_writes_the_manual_strategy_with_it(self):
        grp = self._group_on(GROUP_FORMAT_HIGHEST_SCORE)
        resp = self.client.post(f'/api/channel-groups/{grp.id}/format',
                                json={'resolution': '1920x1080', 'fps': 60})
        self.assertEqual(200, resp.status_code, resp.get_data(as_text=True))
        self.assertEqual(GROUP_FORMAT_MANUAL, resp.get_json()['format_strategy'])
        db.session.expire_all()
        grp = db.session.get(ChannelGroup, grp.id)
        self.assertEqual(GROUP_FORMAT_MANUAL, grp.format_strategy)
        self.assertEqual(('1920x1080', 60), grp.locked_format_key)

    def test_a_lock_can_never_come_to_rest_under_a_disowning_strategy(self):
        """Enforcement lives server-side: a hand-crafted single POST is the case the
        two-request client proves the server has to own."""
        for strategy in (GROUP_FORMAT_HIGHEST_SCORE, GROUP_FORMAT_UNMANAGED,
                         GROUP_FORMAT_MOST_CHANNELS):
            with self.subTest(strategy=strategy):
                grp = make_group(name=f'G-{strategy}',
                                 members=[self._measured(f'A-{strategy}', '1920x1080', 60.0)],
                                 recording=True)
                grp.format_strategy = strategy
                db.session.commit()
                self.client.post(f'/api/channel-groups/{grp.id}/format',
                                 json={'resolution': '1280x720', 'fps': 30})
                db.session.expire_all()
                grp = db.session.get(ChannelGroup, grp.id)
                self.assertIsNotNone(grp.locked_format_key)
                self.assertEqual(GROUP_FORMAT_MANUAL, grp.format_strategy,
                                 'a stored pin implies the manual strategy')

    def test_clearing_the_lock_leaves_the_strategy_alone(self):
        """Clearing is the repair action, and a group on `manual` with no pin filters
        nothing - a legal resting state. Only STORING a pin implies the strategy."""
        grp = self._group_on(GROUP_FORMAT_HIGHEST_SCORE)
        resp = self.client.post(f'/api/channel-groups/{grp.id}/format', json={'clear': True})
        self.assertEqual(200, resp.status_code, resp.get_data(as_text=True))
        db.session.expire_all()
        grp = db.session.get(ChannelGroup, grp.id)
        self.assertEqual(GROUP_FORMAT_HIGHEST_SCORE, grp.format_strategy)
        self.assertIsNone(grp.locked_format_key)


class ClonedPinTests(_Base):
    """The second path that could store a lock under a strategy that disowns it."""

    def _source(self):
        ch = self._measured('Feed', '1920x1080', 60.0)
        src = make_group(name='Source', members=[ch])
        db.session.commit()
        return src

    def test_a_pin_is_refused_under_a_strategy_that_owns_none(self):
        src = self._source()
        resp = self.client.post(f'/api/channel-groups/{src.id}/clone',
                                json={'name': 'Copy',
                                      'format_strategy': GROUP_FORMAT_HIGHEST_SCORE,
                                      'format_resolution': '1280x720', 'format_fps': 30})
        self.assertEqual(200, resp.status_code, resp.get_data(as_text=True))
        self.assertTrue(resp.get_json()['format_pin_refused'],
                        'reported, not silently dropped')
        copy = ChannelGroup.query.filter_by(name='Copy').one()
        self.assertEqual(GROUP_FORMAT_HIGHEST_SCORE, copy.format_strategy)
        self.assertIsNone(copy.locked_format_key)

    def test_a_pin_under_manual_is_kept(self):
        src = self._source()
        resp = self.client.post(f'/api/channel-groups/{src.id}/clone',
                                json={'name': 'Copy',
                                      'format_strategy': GROUP_FORMAT_MANUAL,
                                      'format_resolution': '1280x720', 'format_fps': 30})
        self.assertEqual(200, resp.status_code, resp.get_data(as_text=True))
        self.assertFalse(resp.get_json()['format_pin_refused'])
        copy = ChannelGroup.query.filter_by(name='Copy').one()
        self.assertEqual(('1280x720', 30), copy.locked_format_key)


class StrandedLockSelfHealTests(_Base):
    """Recovery, for groups already carrying the state the write paths now refuse. The
    write-side fix cannot reach a row that is already wrong."""

    def _stranded(self, strategy):
        ch = self._measured('Feed', '1920x1080', 60.0)
        grp = make_group(name='Stranded', members=[ch])
        grp.format_strategy = strategy
        grp.format_resolution, grp.format_fps = '1280x720', 30
        db.session.commit()
        return grp

    def test_a_disowned_lock_is_cleared_and_the_clearing_is_logged(self):
        from app.channel_groups import apply_format_strategy
        grp = self._stranded(GROUP_FORMAT_HIGHEST_SCORE)
        plan = apply_format_strategy(grp)
        self.assertTrue(plan['moved'])
        db.session.expire_all()
        grp = db.session.get(ChannelGroup, grp.id)
        self.assertIsNone(grp.locked_format_key)
        ev = (ChannelGroupEvent.query
              .filter_by(group_id=grp.id, event_type=GROUP_FORMAT_STRATEGY_APPLIED)
              .order_by(ChannelGroupEvent.id.desc()).first())
        self.assertIsNotNone(ev, 'a lock disappearing is never silent')
        self.assertIn('1280x720', ev.detail)
        self.assertTrue(json.loads(ev.extra_data)['cleared_as_disowned'])

    def test_a_manual_groups_pin_is_never_touched(self):
        """`manual` is the exception the whole rule is built around - its pin is the
        user's, and an engine that cleared it would be writing an answer to a judgment
        call (DESIGN-channel-groups-model.md 4.1)."""
        from app.channel_groups import apply_format_strategy
        grp = self._stranded(GROUP_FORMAT_MANUAL)
        plan = apply_format_strategy(grp)
        self.assertFalse(plan['moved'])
        db.session.expire_all()
        grp = db.session.get(ChannelGroup, grp.id)
        self.assertEqual(('1280x720', 30), grp.locked_format_key)

    def test_a_group_with_no_lock_writes_no_event(self):
        """Silent when nothing moved - a re-evaluation that logs its own agreement with
        yesterday buries the times it disagreed."""
        from app.channel_groups import apply_format_strategy
        ch = self._measured('Feed', '1920x1080', 60.0)
        grp = make_group(name='Clean', members=[ch])
        grp.format_strategy = GROUP_FORMAT_HIGHEST_SCORE
        db.session.commit()
        plan = apply_format_strategy(grp)
        self.assertFalse(plan['moved'])
        self.assertEqual(0, ChannelGroupEvent.query.filter_by(
            group_id=grp.id, event_type=GROUP_FORMAT_STRATEGY_APPLIED).count())


if __name__ == '__main__':
    unittest.main()
