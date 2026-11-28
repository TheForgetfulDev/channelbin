"""Replacing a channel the playlist stopped listing (app/channel_successors.py,
dev/docs/DESIGN-channel-successors.md, dev/changelog/1794).

Covers the doc's section 12 below the pages: what is unlisted, what is proposed and what never
is, what an accept moves and carries (and leaves alone on a value the new channel already
has), the health history read through the link and its switch, the undo, a bulk accept run
twice, and a delete of either row afterwards. The sync that strands members in the first
place is tests/test_sim_sync_faults_renumber.py, over the simulator.

Run standalone:
  python3 -m unittest tests.test_channel_successors
"""
import json
import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import channel_successors as successors  # noqa: E402
from app import db  # noqa: E402
from app import output_feed  # noqa: E402
from app.accounts import channel_unlisted, last_sync_bounds, unlisted_channel_expr  # noqa: E402
from app.config import load_config  # noqa: E402
from app.database import (  # noqa: E402
    AccountSyncLog, Channel, ChannelEvent, ChannelGroup, ChannelGroupEvent, ChannelGroupMember,
    ChannelHealthExclusion, ChannelReplacement, ChannelTest, OutputFeedLogin, PipChannel,
    Provider, Recording,
    CHANNEL_HEALTH_HISTORY_CARRIED, CHANNEL_REPLACED, GROUP_MEMBER_PARTICIPATION,
    GROUP_MEMBER_REPLACED, PIP_SOURCE_CHANNEL, REPLACE_MATCH_PICKED, REPLACE_MATCH_STREAM_ID,
    REPLACE_MATCH_URL,
)
from app.health_recompute import (  # noqa: E402
    apply_rollback, observation_ledger, replay,
)
from tests.support import make_test_app, seed  # noqa: E402
from tests.support import output_feed as output_rig  # noqa: E402

ALL = successors.CARRY_PIECES


def _synced(account, started, *, minutes=5):
    """A finished sync of `account` that began at `started`: the bound unlisted reads."""
    done = started + timedelta(minutes=minutes)
    account.last_sync_at = done
    db.session.add(AccountSyncLog(account_id=account.id, started_at=started, completed_at=done,
                                  status='SUCCESS'))
    db.session.flush()


class _Base(unittest.TestCase):
    app_kwargs = {}

    def setUp(self):
        self.t = make_test_app(**self.app_kwargs)
        self.addCleanup(self.t.cleanup)
        self.cfg = load_config()
        self.now = datetime.utcnow().replace(microsecond=0)
        self.acc = seed.make_account(name='Account A')
        self.sync_start = self.now - timedelta(hours=1)
        _synced(self.acc, self.sync_start)
        db.session.commit()

    def old_channel(self, name='ESPN', psid='1001', account=None, **kw):
        account = account or self.acc
        ch = seed.make_channel(account, name=name, **kw)
        ch.provider_stream_id = psid
        ch.last_seen_at = self.sync_start - timedelta(hours=3)
        db.session.flush()
        return ch

    def new_channel(self, name='ESPN HD', psid='1001', account=None, **kw):
        account = account or self.acc
        ch = seed.make_channel(account, name=name, **kw)
        ch.provider_stream_id = psid
        ch.last_seen_at = self.sync_start + timedelta(minutes=1)
        db.session.flush()
        return ch

    def scored_test(self, channel, quality, at, status='COMPLETED'):
        return seed.make_channel_test(channel, all_null=False, status=status,
                                      quality_score=quality, duration_seconds=60,
                                      test_started_at=at, test_ended_at=at)

    def accept(self, old, new, options=ALL, **kw):
        result = successors.accept(old.id, new.id, options, cfg=self.cfg, **kw)
        db.session.expire_all()
        return result


class UnlistedTests(_Base):
    def test_python_and_sql_agree_on_each_case(self):
        """Unlisted is the last sync's start, never last_sync_at, and an account with no known
        last sync reads as listed (section 3)."""
        other = seed.make_account(name='Never synced')
        old = self.old_channel()
        new = self.new_channel(psid='2')
        never = seed.make_channel(other, name='Never')
        never.last_seen_at = self.now - timedelta(days=30)
        blank = seed.make_channel(self.acc, name='Blank')
        blank.last_seen_at = None
        db.session.commit()
        bounds = last_sync_bounds()
        self.assertNotIn(other.id, bounds)
        python = {c.id for c in Channel.query.all() if channel_unlisted(c, bounds)}
        sql = {c.id for c in Channel.query.filter(unlisted_channel_expr(bounds)).all()}
        self.assertEqual(python, {old.id})
        self.assertEqual(sql, python)
        self.assertNotIn(new.id, python)

    def test_a_pruned_log_row_means_not_known(self):
        old = self.old_channel()
        db.session.commit()
        AccountSyncLog.query.delete()
        db.session.commit()
        self.assertFalse(channel_unlisted(old, last_sync_bounds()))

    def test_an_account_with_every_channel_listed_strands_nothing(self):
        ch = self.new_channel()
        seed.make_group(name='G', members=[ch])
        db.session.commit()
        self.assertEqual(successors.stranded_channels(), [])


class ProposalTests(_Base):
    def test_same_provider_stream_id_on_the_same_account(self):
        old = self.old_channel()
        new = self.new_channel()
        self.new_channel(name='Other', psid='9')
        db.session.commit()
        props = successors.proposals([old])
        self.assertEqual([(c.id, k) for c, k in props[old.id]], [(new.id, REPLACE_MATCH_STREAM_ID)])

    def test_two_accounts_on_no_shared_provider_are_never_matched_on_stream_ids(self):
        """Unrelated backends share stream ids by chance (section 4)."""
        other = seed.make_account(name='Elsewhere')
        _synced(other, self.sync_start)
        old = self.old_channel()
        stranger = self.new_channel(account=other)
        stranger.stream_url = 'http://elsewhere.test/live/1'
        db.session.commit()
        self.assertEqual(successors.proposals([old])[old.id], [])

    def test_two_accounts_on_one_provider_are_matched(self):
        prov = Provider(name='Provider P')
        db.session.add(prov)
        db.session.flush()
        other = seed.make_account(name='Account B')
        self.acc.provider_id = other.provider_id = prov.id
        _synced(other, self.sync_start)
        old = self.old_channel()
        new = self.new_channel(account=other)
        db.session.commit()
        rows = successors.proposals([old])[old.id]
        self.assertEqual([(c.id, k) for c, k in rows], [(new.id, REPLACE_MATCH_STREAM_ID)])
        self.assertEqual(successors.for_target(rows, old, 'own'), [])
        self.assertEqual(len(successors.for_target(rows, old, other.id)), 1)

    def test_the_url_key_matches_on_any_account(self):
        other = seed.make_account(name='Elsewhere')
        _synced(other, self.sync_start)
        old = self.old_channel(psid=None)
        new = self.new_channel(account=other, psid=None)
        new.stream_url = old.stream_url
        db.session.commit()
        self.assertEqual([(c.id, k) for c, k in successors.proposals([old])[old.id]],
                         [(new.id, REPLACE_MATCH_URL)])

    def test_an_unlisted_channel_is_never_a_candidate(self):
        old = self.old_channel()
        self.old_channel(name='Also old')
        db.session.commit()
        self.assertEqual(successors.proposals([old])[old.id], [])

    def test_a_replaced_channel_is_never_a_candidate(self):
        old = self.old_channel()
        taken = self.new_channel()
        db.session.add(ChannelReplacement(old_channel_id=taken.id, new_channel_id=old.id,
                                          accepted_at=self.now, match_kind=REPLACE_MATCH_URL,
                                          health_carried=False, old_name=taken.name))
        db.session.commit()
        self.assertEqual(successors.proposals([old])[old.id], [])

    def test_a_listed_channel_never_seen_is_a_candidate_as_unlisted_reads_it(self):
        """channel_unlisted() reads a NULL last_seen_at as listed; the candidates' query must
        too, or the review and the accept disagree."""
        old = self.old_channel()
        new = self.new_channel()
        new.last_seen_at = None
        db.session.commit()
        self.assertFalse(channel_unlisted(new, last_sync_bounds()))
        self.assertEqual([c.id for c, _ in successors.proposals([old])[old.id]], [new.id])

    def test_stream_id_is_never_the_key(self):
        """The same Channel.stream_id on two accounts of one provider, with a different
        provider stream id and URL, is two channels (section 4)."""
        prov = Provider(name='Provider P')
        db.session.add(prov)
        db.session.flush()
        other = seed.make_account(name='Account B')
        self.acc.provider_id = other.provider_id = prov.id
        _synced(other, self.sync_start)
        old = self.old_channel(psid='1001')
        new = self.new_channel(account=other, psid='2002')
        new.stream_id = old.stream_id
        new.stream_url = 'http://example.test/live/other'
        db.session.commit()
        self.assertEqual(successors.proposals([old])[old.id], [])

    def test_several_matches_are_all_listed_and_none_is_chosen(self):
        old = self.old_channel()
        a = self.new_channel(name='ESPN')
        b = self.new_channel(name='ESPN (backup)')
        db.session.commit()
        rows = successors.proposals([old])[old.id]
        self.assertEqual({c.id for c, _ in rows}, {a.id, b.id})
        self.assertEqual(successors.single_match_pairs(successors.proposals([old]), [old], 'own'), [])


class AcceptMovesMembershipsTests(_Base):
    def test_swap_keeps_position_and_both_switches_and_logs_both_channels(self):
        filler = self.new_channel(name='Filler', psid='7')
        old = self.old_channel()
        new = self.new_channel()
        group = seed.make_group(name='Sports', members=[filler, old], test_disabled={old.id})
        db.session.commit()
        before = ChannelGroupMember.query.filter_by(group_id=group.id, channel_id=old.id).one()
        position, rec_on, test_on = before.position, before.recording_enabled, before.test_enabled

        self.accept(old, new)

        m = ChannelGroupMember.query.filter_by(group_id=group.id, channel_id=new.id).one()
        self.assertEqual((m.position, m.recording_enabled, m.test_enabled),
                         (position, rec_on, test_on))
        self.assertEqual(ChannelGroupMember.query.filter_by(channel_id=old.id).count(), 0)
        ev = ChannelGroupEvent.query.filter_by(group_id=group.id,
                                               event_type=GROUP_MEMBER_REPLACED).one()
        self.assertEqual(json.loads(ev.extra_data)['old_channel_id'], old.id)
        self.assertEqual(ev.channel_id, new.id)
        link = ChannelReplacement.query.one()
        self.assertEqual((link.old_channel_id, link.new_channel_id), (old.id, new.id))
        self.assertEqual(ChannelEvent.query.filter_by(event_type=CHANNEL_REPLACED).count(), 2)
        self.assertIsNotNone(db.session.get(Channel, old.id))

    def test_merge_carries_each_switch_through_set_participation(self):
        old = self.old_channel()
        new = self.new_channel()
        group = seed.make_group(name='Sports', members=[old, new], disabled={new.id})
        db.session.commit()

        self.accept(old, new)

        m = ChannelGroupMember.query.filter_by(group_id=group.id, channel_id=new.id).one()
        self.assertTrue(m.recording_enabled)
        self.assertEqual(ChannelGroupMember.query.filter_by(group_id=group.id).count(), 1)
        moved = ChannelGroupEvent.query.filter_by(group_id=group.id,
                                                  event_type=GROUP_MEMBER_PARTICIPATION).one()
        self.assertEqual(json.loads(moved.extra_data)['surface'], 'replace')

    def test_recording_enabled_count_never_empties(self):
        old = self.old_channel()
        new = self.new_channel()
        group = seed.make_group(name='Sports', members=[old])
        db.session.commit()
        self.accept(old, new)
        g = db.session.get(ChannelGroup, group.id)
        self.assertEqual(sum(1 for m in g.memberships if m.recording_enabled), 1)

    def test_the_guard_refuses_a_move_that_would_empty_a_group(self):
        """The assertion behind section 8.1: if a merge ever left a group that had a
        recording-enabled member with none, the accept raises and nothing commits."""
        old = self.old_channel()
        new = self.new_channel()
        group = seed.make_group(name='Sports', members=[old, new], disabled={new.id})
        db.session.commit()
        with mock.patch.object(successors, 'set_participation'):
            with self.assertRaises(RuntimeError):
                self.accept(old, new)
        db.session.rollback()
        self.assertEqual(ChannelReplacement.query.count(), 0)
        self.assertEqual(ChannelGroupMember.query.filter_by(group_id=group.id).count(), 2)

    def test_a_pinned_guide_listings_member_follows(self):
        old = self.old_channel()
        new = self.new_channel()
        group = seed.make_group(name='Sports', members=[old])
        group.guide_listings_channel_id = old.id
        db.session.commit()
        self.accept(old, new)
        self.assertEqual(db.session.get(ChannelGroup, group.id).guide_listings_channel_id, new.id)

    def test_a_running_recording_is_never_touched_and_a_plain_scheduled_one_moves(self):
        old = self.old_channel()
        new = self.new_channel()
        running = seed.make_recording(status='IN_PROGRESS', channel_id=old.id)
        later = seed.make_recording(status='SCHEDULED', channel_id=old.id,
                                    start_time=self.now + timedelta(days=1),
                                    stop_time=self.now + timedelta(days=1, hours=1))
        seed.make_group(name='G', members=[old])
        db.session.commit()
        self.accept(old, new)
        self.assertEqual(db.session.get(Recording, running.id).channel_id, old.id)
        self.assertEqual(db.session.get(Recording, later.id).channel_id, new.id)


class RefusalTests(_Base):
    def test_a_listed_old_channel_is_refused(self):
        a = self.new_channel()
        b = self.new_channel(name='Other')
        db.session.commit()
        with self.assertRaises(successors.ReplaceRefused):
            self.accept(a, b)

    def test_an_unlisted_new_channel_is_refused(self):
        old = self.old_channel()
        other = self.old_channel(name='Also old')
        db.session.commit()
        with self.assertRaises(successors.ReplaceRefused):
            self.accept(old, other)

    def test_a_pairing_that_is_not_a_proposal_needs_picked(self):
        old = self.old_channel()
        stranger = self.new_channel(name='Stranger', psid='77')
        stranger.stream_url = 'http://example.test/live/stranger'
        seed.make_group(name='G', members=[old])
        db.session.commit()
        with self.assertRaises(successors.ReplaceRefused):
            self.accept(old, stranger)
        self.accept(old, stranger, picked=True)
        self.assertEqual(ChannelReplacement.query.one().match_kind, REPLACE_MATCH_PICKED)

    def test_a_second_replacement_of_one_old_channel_names_the_first(self):
        old = self.old_channel()
        new = self.new_channel()
        other = self.new_channel(name='ESPN 2')
        seed.make_group(name='G', members=[old])
        db.session.commit()
        self.accept(old, new)
        with self.assertRaises(successors.ReplaceRefused) as err:
            self.accept(old, other)
        self.assertIn('ESPN HD', str(err.exception))


class CarryTests(_Base):
    """Every piece of section 8.2 is carried into an unset value and left alone on a set one,
    with the skipped piece named in the event."""

    def _pair(self):
        old = self.old_channel(in_guide=True)
        new = self.new_channel()
        old.guide_sort_order = 42
        old.notes = 'the good feed'
        old.manual_health_adjustment = 15
        old.manual_health_note = 'trusted'
        old.pace_realtime = True
        old.hidden_override = False
        seed.make_group(name='G', members=[old])
        db.session.commit()
        return old, new

    def test_each_piece_carried_into_an_unset_value(self):
        old, new = self._pair()
        result = self.accept(old, new)
        new = db.session.get(Channel, new.id)
        old = db.session.get(Channel, old.id)
        self.assertTrue(new.in_guide)
        self.assertEqual(new.guide_sort_order, 42)
        self.assertFalse(old.in_guide)
        self.assertEqual(new.notes, 'the good feed')
        self.assertEqual((new.manual_health_adjustment, new.manual_health_note), (15, 'trusted'))
        self.assertTrue(new.pace_realtime)
        self.assertIs(new.hidden_override, False)
        for piece in (successors.CARRY_GUIDE, successors.CARRY_NOTES,
                      successors.CARRY_ADJUSTMENT, successors.CARRY_PACE, successors.CARRY_HIDE):
            self.assertEqual(result['carried'][piece], successors.CARRIED, piece)
        stored = json.loads(ChannelReplacement.query.one().carried)
        self.assertEqual(stored[successors.CARRY_NOTES], successors.CARRIED)

    def test_a_value_the_new_channel_has_is_never_overwritten_and_is_named(self):
        old, new = self._pair()
        new.in_guide = True
        new.guide_sort_order = 7
        new.notes = 'mine'
        new.manual_health_adjustment = -5
        new.pace_realtime = False
        new.hidden_override = True
        db.session.commit()
        result = self.accept(old, new)
        new = db.session.get(Channel, new.id)
        self.assertEqual((new.guide_sort_order, new.notes, new.manual_health_adjustment,
                          new.pace_realtime, new.hidden_override), (7, 'mine', -5, False, True))
        for piece in (successors.CARRY_GUIDE, successors.CARRY_NOTES,
                      successors.CARRY_ADJUSTMENT, successors.CARRY_PACE, successors.CARRY_HIDE):
            self.assertEqual(result['carried'][piece], successors.KEPT_OWN, piece)
        ev = ChannelEvent.query.filter_by(channel_id=new.id, event_type=CHANNEL_REPLACED).one()
        self.assertIn('kept its own', ev.detail)
        self.assertEqual(json.loads(ev.extra_data)['carried'][successors.CARRY_NOTES],
                         successors.KEPT_OWN)

    def test_an_unchecked_piece_is_not_asked_and_not_moved(self):
        old, new = self._pair()
        result = self.accept(old, new, options=set(ALL) - {successors.CARRY_NOTES})
        self.assertIsNone(db.session.get(Channel, new.id).notes)
        self.assertEqual(result['carried'][successors.CARRY_NOTES], successors.NOT_ASKED)

    def test_picture_in_picture_pictures_follow(self):
        old, new = self._pair()
        row = PipChannel(name='Two games', main_kind=PIP_SOURCE_CHANNEL, main_ref=old.id,
                         inset_kind=PIP_SOURCE_CHANNEL, inset_ref=999)
        db.session.add(row)
        db.session.commit()
        self.accept(old, new)
        row = db.session.get(PipChannel, row.id)
        self.assertEqual((row.main_ref, row.inset_ref), (new.id, 999))


class HealthCarryTests(_Base):
    def _history(self):
        old = self.old_channel()
        new = self.new_channel()
        for i, q in enumerate((90, 40, 70)):
            self.scored_test(old, q, self.now - timedelta(days=3 - i))
        seed.make_group(name='G', members=[old])
        db.session.commit()
        return old, new

    def test_the_new_score_is_a_replay_of_the_old_ledger_up_to_the_accept(self):
        old, new = self._history()
        expected, count, _ = replay(observation_ledger(old.id, self.cfg), self.cfg)
        result = self.accept(old, new)
        new = db.session.get(Channel, new.id)
        self.assertAlmostEqual(new.health_score, expected)
        self.assertEqual(new.health_score_sample_count, count)
        self.assertEqual(result['carried'][successors.CARRY_HEALTH], successors.CARRIED)
        self.assertEqual(ChannelEvent.query.filter_by(
            channel_id=new.id, event_type=CHANNEL_HEALTH_HISTORY_CARRIED).count(), 1)
        # The old channel keeps its own score and history.
        self.assertEqual(ChannelTest.query.filter_by(channel_id=old.id).count(), 3)

    def test_an_observation_after_the_accept_stays_the_old_channels(self):
        old, new = self._history()
        self.accept(old, new)
        link = ChannelReplacement.query.one()
        late = self.scored_test(db.session.get(Channel, old.id), 0,
                                link.accepted_at + timedelta(minutes=5))
        db.session.commit()
        keys = {o.key for o in observation_ledger(new.id, self.cfg)}
        self.assertNotIn(('test', late.id), keys)

    def test_a_step_back_on_the_new_channel_excludes_the_old_channels_newest(self):
        old, new = self._history()
        self.accept(old, new)
        newest = ChannelTest.query.filter_by(channel_id=old.id).order_by(
            ChannelTest.test_started_at.desc()).first()
        new = db.session.get(Channel, new.id)
        apply_rollback(new, 'step_back', self.cfg)
        db.session.commit()
        ex = ChannelHealthExclusion.query.one()
        self.assertEqual((ex.channel_id, ex.source_kind, ex.source_id), (new.id, 'test', newest.id))
        counting = [o for o in observation_ledger(new.id, self.cfg) if not o.excluded]
        self.assertEqual(len(counting), 2)

    def test_turning_the_switch_off_returns_the_new_channel_to_its_own(self):
        old, new = self._history()
        own = self.scored_test(new, 100, self.now - timedelta(hours=2))
        db.session.commit()
        self.accept(old, new)
        moved = successors.set_health_carried(ChannelReplacement.query.one().id, False,
                                              cfg=self.cfg)
        self.assertIsNotNone(moved)
        db.session.expire_all()
        new = db.session.get(Channel, new.id)
        self.assertEqual([o.key for o in observation_ledger(new.id, self.cfg)], [('test', own.id)])
        self.assertAlmostEqual(new.health_score, 100.0, places=3)
        self.assertEqual(ChannelEvent.query.filter_by(
            channel_id=new.id, event_type=CHANNEL_HEALTH_HISTORY_CARRIED).count(), 2)
        self.assertIsNone(successors.set_health_carried(ChannelReplacement.query.one().id, False,
                                                        cfg=self.cfg))

    def test_not_carried_leaves_the_new_score_alone(self):
        old, new = self._history()
        self.accept(old, new, options=set(ALL) - {successors.CARRY_HEALTH})
        self.assertIsNone(db.session.get(Channel, new.id).health_score)
        self.assertFalse(ChannelReplacement.query.one().health_carried)


class UndoTests(_Base):
    def test_a_replacement_already_replaced_cannot_be_undone_onto_the_first(self):
        """A by B, then B by C: replacing B with A again is refused, or C would stop counting
        A's history with nothing said."""
        a = self.old_channel()
        b = self.new_channel()
        seed.make_group(name='G', members=[a])
        db.session.commit()
        self.accept(a, b)
        b = db.session.get(Channel, b.id)
        b.last_seen_at = self.sync_start - timedelta(minutes=30)
        c = self.new_channel(name='ESPN 4K')
        db.session.commit()
        self.accept(b, c)
        with self.assertRaises(successors.ReplaceRefused):
            self.accept(b, a)
        self.assertEqual(ChannelReplacement.query.count(), 2)

    def test_replacing_back_removes_the_link_and_the_carried_history(self):
        old = self.old_channel()
        new = self.new_channel()
        self.scored_test(old, 80, self.now - timedelta(days=1))
        seed.make_group(name='G', members=[old])
        db.session.commit()
        self.accept(old, new)
        self.assertIsNotNone(db.session.get(Channel, new.id).health_score)

        result = self.accept(new, old)

        self.assertTrue(result['undo'])
        self.assertEqual(ChannelReplacement.query.count(), 0)
        self.assertIsNone(db.session.get(Channel, new.id).health_score)
        self.assertEqual(ChannelGroupMember.query.filter_by(channel_id=old.id).count(), 1)


class BulkTests(_Base):
    def test_an_interrupted_bulk_accept_run_again_replaces_each_once(self):
        pairs = []
        for i in range(4):
            old = self.old_channel(name=f'Old {i}', psid=str(100 + i))
            new = self.new_channel(name=f'New {i}', psid=str(100 + i))
            seed.make_group(name=f'G{i}', members=[old])
            pairs.append((old.id, new.id))
        db.session.commit()

        done, skipped = successors.accept_many(pairs[:2], ALL, cfg=self.cfg)
        self.assertEqual((len(done), skipped), (2, []))
        done, skipped = successors.accept_many(pairs, ALL, cfg=self.cfg)
        self.assertEqual(len(done), 2)
        self.assertEqual({old for old, _ in skipped}, {pairs[0][0], pairs[1][0]})
        self.assertEqual(ChannelReplacement.query.count(), 4)
        self.assertEqual(ChannelGroupEvent.query.filter_by(
            event_type=GROUP_MEMBER_REPLACED).count(), 4)

    def test_single_match_pairs_skip_a_member_with_two_matches(self):
        one = self.old_channel(name='One', psid='1')
        two = self.old_channel(name='Two', psid='2')
        n1 = self.new_channel(name='One new', psid='1')
        self.new_channel(name='Two a', psid='2')
        self.new_channel(name='Two b', psid='2')
        db.session.commit()
        olds = [one, two]
        pairs = successors.single_match_pairs(successors.proposals(olds), olds, 'own')
        self.assertEqual(pairs, [(one.id, n1.id)])


class DeleteLaterTests(_Base):
    def _accepted(self):
        old = self.old_channel()
        new = self.new_channel()
        for i, q in enumerate((90, 40, 70)):
            self.scored_test(old, q, self.now - timedelta(days=3 - i))
        seed.make_group(name='G', members=[old])
        db.session.commit()
        self.accept(old, new)
        return old.id, new.id

    def _delete(self, ids):
        successors.before_channel_delete(ids, self.cfg)
        ChannelTest.query.filter(ChannelTest.channel_id.in_(ids)).delete(synchronize_session=False)
        ChannelEvent.query.filter(ChannelEvent.channel_id.in_(ids)).delete(synchronize_session=False)
        ChannelHealthExclusion.query.filter(ChannelHealthExclusion.channel_id.in_(ids)).delete(
            synchronize_session=False)
        Channel.query.filter(Channel.id.in_(ids)).delete(synchronize_session=False)
        db.session.commit()
        db.session.expire_all()

    def test_deleting_the_old_row_leaves_the_new_score_unchanged(self):
        old_id, new_id = self._accepted()
        score = db.session.get(Channel, new_id).health_score
        self._delete([old_id])

        link = ChannelReplacement.query.one()
        self.assertIsNone(link.old_channel_id)
        self.assertEqual(link.old_name, 'ESPN')
        new = db.session.get(Channel, new_id)
        self.assertEqual(ChannelTest.query.filter_by(channel_id=new_id).count(), 3)
        score_after, _, _ = replay(observation_ledger(new_id, self.cfg), self.cfg)
        self.assertAlmostEqual(score_after, score)
        self.assertAlmostEqual(new.health_score, score)

    def test_a_later_channel_handed_the_old_id_inherits_nothing(self):
        old_id, new_id = self._accepted()
        self._delete([old_id])
        reborn = Channel(id=old_id, account_id=self.acc.id, stream_id=9999, name='Stranger',
                         stream_url='http://example.test/live/9999')
        db.session.add(reborn)
        db.session.commit()
        self.assertEqual(observation_ledger(old_id, self.cfg), [])
        self.assertEqual(successors.replaced_old_ids([old_id]), set())
        self.assertEqual(len(observation_ledger(new_id, self.cfg)), 3)

    def test_deleting_the_new_row_removes_its_link(self):
        old_id, new_id = self._accepted()
        self._delete([new_id])
        self.assertEqual(ChannelReplacement.query.count(), 0)
        self.assertEqual(ChannelTest.query.filter_by(channel_id=old_id).count(), 3)

    def test_a_deleted_middle_link_repoints_the_chain(self):
        """A by B, B by C, B deleted: B's counted rows move to C and A's link ends at C."""
        a_id, b_id = self._accepted()
        b = db.session.get(Channel, b_id)
        b.last_seen_at = self.sync_start - timedelta(minutes=30)
        c = self.new_channel(name='ESPN 4K')
        seed.make_group(name='G2', members=[b])
        self.scored_test(b, 20, self.now - timedelta(minutes=50))
        db.session.commit()
        self.accept(b, c)
        expected = db.session.get(Channel, c.id).health_score
        self._delete([b_id])
        link = ChannelReplacement.query.filter_by(old_channel_id=a_id).one()
        self.assertEqual(link.new_channel_id, c.id)
        score, _, _ = replay(observation_ledger(c.id, self.cfg), self.cfg)
        self.assertAlmostEqual(score, expected)


class DeleteChainTests(DeleteLaterTests):
    def test_deleting_both_old_rows_of_a_chain_keeps_the_last_score(self):
        """A by B, B by C, A and B deleted in one batch: A's history moves to C too."""
        a_id, b_id = self._accepted()
        b = db.session.get(Channel, b_id)
        b.last_seen_at = self.sync_start - timedelta(minutes=30)
        c = self.new_channel(name='ESPN 4K')
        seed.make_group(name='G2', members=[b])
        db.session.commit()
        self.accept(b, c)
        expected = db.session.get(Channel, c.id).health_score
        self._delete([a_id, b_id])
        self.assertEqual(ChannelTest.query.filter_by(channel_id=c.id).count(), 3)
        score, _, _ = replay(observation_ledger(c.id, self.cfg), self.cfg)
        self.assertAlmostEqual(score, expected)
        self.assertTrue(all(link.old_channel_id is None for link in ChannelReplacement.query))

    def test_a_check_after_the_accept_stays_uncounted_when_its_recording_moves(self):
        """A recording that finished before the accept moves at the delete; its post-process
        check, which landed after, is excluded on the new channel so the score holds."""
        old = self.old_channel()
        new = self.new_channel()
        rec = seed.make_recording(status='COMPLETED', channel_id=old.id,
                                  start_time=self.now - timedelta(hours=5),
                                  stop_time=self.now - timedelta(hours=4))
        rec.health_quality_score = 80
        rec.completed_at = self.now - timedelta(hours=4)
        seed.make_group(name='G', members=[old])
        db.session.commit()
        self.accept(old, new)
        link = ChannelReplacement.query.one()
        rec = db.session.get(Recording, rec.id)
        rec.capture_quality_breakdown = json.dumps({
            'final': 10, 'observed_at': (link.accepted_at + timedelta(hours=1)).isoformat()})
        db.session.commit()
        expected, _, _ = replay(observation_ledger(new.id, self.cfg), self.cfg)
        self._delete([old.id])
        self.assertEqual(db.session.get(Recording, rec.id).channel_id, new.id)
        ex = ChannelHealthExclusion.query.filter_by(channel_id=new.id).one()
        self.assertEqual((ex.source_kind, ex.action),
                         ('capture_correction', successors.EXCLUDED_PAST_CUTOFF))
        score, _, _ = replay(observation_ledger(new.id, self.cfg), self.cfg)
        self.assertAlmostEqual(score, expected)


class DeleteRouteTests(_Base):
    """Both delete paths reach before_channel_delete() before the cascade (section 7)."""

    app_kwargs = {'csrf': False}

    def _replaced(self, old_account, new_account):
        old = self.old_channel(account=old_account)
        old.last_seen_at = self.now - timedelta(days=10)
        new = self.new_channel(account=new_account)
        shot = tempfile.NamedTemporaryFile(dir=self.t._tmpdir, suffix='.jpg', delete=False)
        shot.write(b'jpg')
        shot.close()
        for i, q in enumerate((90, 40)):
            self.scored_test(old, q, self.now - timedelta(days=12 - i))
        ChannelTest.query.filter_by(channel_id=old.id).first().screenshot_path = shot.name
        seed.make_group(name='G', members=[old])
        db.session.commit()
        self.accept(old, new)
        return old.id, new.id, shot.name

    def test_the_missing_channel_delete_moves_the_history_and_keeps_the_screenshot(self):
        old_id, new_id, shot = self._replaced(self.acc, self.acc)
        score = db.session.get(Channel, new_id).health_score
        resp = self.t.client.post('/channels/missing-delete', json={'channel_ids': [old_id]})
        self.assertEqual(resp.status_code, 200, resp.get_json())
        self.assertEqual(resp.get_json()['deleted_count'], 1)
        db.session.expire_all()
        self.assertIsNone(db.session.get(Channel, old_id))
        self.assertEqual(ChannelTest.query.filter_by(channel_id=new_id).count(), 2)
        self.assertTrue(os.path.exists(shot))
        self.assertIsNone(ChannelReplacement.query.one().old_channel_id)
        self.assertAlmostEqual(replay(observation_ledger(new_id, self.cfg), self.cfg)[0], score)

    def test_an_account_delete_moves_the_history_and_keeps_the_screenshot(self):
        prov = Provider(name='Provider P')
        db.session.add(prov)
        db.session.flush()
        keep = seed.make_account(name='Account B')
        self.acc.provider_id = keep.provider_id = prov.id
        _synced(keep, self.sync_start)
        db.session.commit()
        old_id, new_id, shot = self._replaced(self.acc, keep)
        resp = self.t.client.delete(f'/api/accounts/{self.acc.id}')
        self.assertEqual(resp.status_code, 200, resp.get_json())
        db.session.expire_all()
        self.assertIsNone(db.session.get(Channel, old_id))
        self.assertEqual(ChannelTest.query.filter_by(channel_id=new_id).count(), 2)
        self.assertTrue(os.path.exists(shot))
        self.assertIsNone(ChannelReplacement.query.one().old_channel_id)


class DedupPathTests(_Base):
    def test_replace_with_no_link_writes_none(self):
        a = self.new_channel(name='A', in_guide=True)
        b = self.new_channel(name='B')
        db.session.commit()
        result = successors.replace(a, b, successors.DEDUP_PIECES, successors.SURFACE_DEDUP,
                                    cfg=self.cfg)
        db.session.commit()
        self.assertEqual(ChannelReplacement.query.count(), 0)
        self.assertEqual(result['carried'][successors.CARRY_GUIDE], successors.CARRIED)
        self.assertNotIn(successors.CARRY_HEALTH, result['carried'])


class OutputFeedIdTests(_Base):
    """Section 8.3: a channel with its own guide row on the IPTV output changes feed id when it
    is replaced, so the old id is answered with the new channel while the old row exists, and
    each login's channel list has the old id swapped for the new one."""

    app_kwargs = {'csrf': False}

    def setUp(self):
        super().setUp()
        self.t.sandbox_config(output_rig.feed_cfg())
        self.addCleanup(output_feed.stop_all)
        self.old = self.old_channel(in_guide=True)
        self.new = self.new_channel()
        db.session.commit()
        self.old_fid = output_feed.feed_id_for_channel(self.old.id)
        self.new_fid = output_feed.feed_id_for_channel(self.new.id)

    def head(self, feed_id):
        return self.t.client.head(f'/live/{output_rig.USER}/{output_rig.PASS}/{feed_id}')

    def delete_old(self):
        ids = [self.old.id]
        successors.before_channel_delete(ids, self.cfg)
        ChannelEvent.query.filter(ChannelEvent.channel_id.in_(ids)).delete(synchronize_session=False)
        Channel.query.filter(Channel.id.in_(ids)).delete(synchronize_session=False)
        db.session.commit()
        db.session.expire_all()

    def test_the_old_id_plays_the_new_channel_while_the_old_row_exists(self):
        output_rig.add_login()
        self.assertEqual(self.head(self.old_fid).status_code, 200)
        self.assertEqual(self.head(self.new_fid).status_code, 404)
        self.accept(self.old, self.new)

        self.assertEqual(output_feed.successor_feed_id(self.old_fid), self.new_fid)
        self.assertEqual(self.head(self.old_fid).status_code, 200)
        self.assertEqual(self.head(self.new_fid).status_code, 200)

        self.delete_old()
        self.assertEqual(output_feed.successor_feed_id(self.old_fid), self.old_fid)
        self.assertEqual(self.head(self.old_fid).status_code, 404)

    def test_no_answer_when_the_guide_row_did_not_move(self):
        """The guide row left unchecked, or the new channel keeping a row of its own: the old id
        names no channel that took its place."""
        self.accept(self.old, self.new, options=set(ALL) - {successors.CARRY_GUIDE})
        self.assertEqual(output_feed.successor_feed_id(self.old_fid), self.old_fid)

    def test_a_new_channel_with_its_own_row_answers_nothing_for_the_old_id(self):
        new = db.session.get(Channel, self.new.id)
        new.in_guide = True
        db.session.commit()
        self.accept(self.old, self.new)
        self.assertEqual(output_feed.successor_feed_id(self.old_fid), self.old_fid)

    def test_the_answer_follows_a_chain_and_ends_at_an_undo(self):
        self.accept(self.old, self.new)
        b = db.session.get(Channel, self.new.id)
        b.last_seen_at = self.sync_start - timedelta(minutes=30)
        c = self.new_channel(name='ESPN 4K')
        db.session.commit()
        self.accept(b, c)
        c_fid = output_feed.feed_id_for_channel(c.id)
        self.assertEqual(output_feed.successor_feed_id(self.old_fid), c_fid)
        self.assertEqual(output_feed.successor_feed_id(self.new_fid), c_fid)

        self.accept(c, b)    # the undo of B by C
        self.assertEqual(output_feed.successor_feed_id(self.old_fid), self.new_fid)

    def test_a_channel_with_its_own_row_reads_no_replacement(self):
        """A normal play pays nothing: the lookup runs only for a channel with no row."""
        with mock.patch.object(successors, 'guide_successor_id') as lookup:
            self.assertEqual(output_feed.successor_feed_id(self.old_fid), self.old_fid)
            self.assertEqual(output_feed.successor_feed_id(output_feed.feed_id_for_group(5)),
                             output_feed.feed_id_for_group(5))
        lookup.assert_not_called()

    def test_each_login_list_has_the_old_id_swapped_for_the_new(self):
        only = output_rig.add_login('only', 'pw1', lineup=json.dumps([self.old_fid, 99]))
        but = output_rig.add_login('but', 'pw2', lineup_except=json.dumps([self.old_fid]))
        every = output_rig.add_login('every', 'pw3')
        before = dict(output_feed._lineup_changes)

        self.accept(self.old, self.new)

        only, but, every = (db.session.get(OutputFeedLogin, r.id) for r in (only, but, every))
        self.assertEqual(json.loads(only.lineup), sorted([self.new_fid, 99]))
        self.assertEqual(json.loads(but.lineup_except), [self.new_fid])
        self.assertIsNone(every.lineup)
        self.assertIsNone(every.lineup_except)
        # Handed to the live plays after the commit, as a save in the Logins dialog is.
        for row in (only, but):
            self.assertEqual(output_feed._lineup_changes.get(row.id, 0),
                             before.get(row.id, 0) + 1)
        self.assertNotIn(every.id, output_feed._lineup_changes)
        # A narrowed login gets the channel on its old id too, since it is the new one's.
        self.assertTrue(output_feed.lineup_offers(output_feed.lineup_of(only),
                                                  output_feed.successor_feed_id(self.old_fid)))

    def test_undo_swaps_the_lists_back(self):
        only = output_rig.add_login('only', 'pw1', lineup=json.dumps([self.old_fid]))
        self.accept(self.old, self.new)
        self.assertEqual(json.loads(db.session.get(OutputFeedLogin, only.id).lineup),
                         [self.new_fid])
        self.accept(self.new, self.old)
        self.assertEqual(json.loads(db.session.get(OutputFeedLogin, only.id).lineup),
                         [self.old_fid])


if __name__ == '__main__':
    unittest.main()
