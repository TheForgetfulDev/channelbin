"""A deleted channel takes its id out of the history rows that outlive it
(dev/docs/BUGS.md 2026-11-15, dev/changelog/1569).

channels.id is not AUTOINCREMENT, so the id of a channel deleted from the top of the table
goes to the next channel a sync creates. Both delete paths - the missing-channel delete and
an account's delete - left recording_segments.channel_id and channel_group_events.channel_id
holding the gone id, and the account's delete left recordings.channel_id too, so each of
those rows read as the new channel's from then on.

No network, no real ffmpeg.
  python3 -m unittest tests.test_channel_delete_detaches_history
"""
import os
import sys
import unittest
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import db  # noqa: E402
from app.database import (  # noqa: E402
    Channel, ChannelGroupEvent, Recording, RecordingSegment, GROUP_MEMBER_PARTICIPATION,
    REC_STATUS_COMPLETED, REC_STATUS_CONVERTING, REC_STATUS_IN_PROGRESS,
)
from tests.support import make_test_app  # noqa: E402
from tests.support import seed  # noqa: E402


def _history(channel, group):
    """A finished recording with one segment on `channel`, and a group event naming it."""
    rec = seed.make_recording(status=REC_STATUS_COMPLETED, channel_id=channel.id, name='Kept')
    db.session.flush()
    now = datetime.utcnow()
    seg = seed.make_segment(rec, channel, now - timedelta(hours=1), now)
    event = ChannelGroupEvent(group_id=group.id, channel_id=channel.id, timestamp=now,
                              event_type=GROUP_MEMBER_PARTICIPATION, detail='"Gone" switched off')
    db.session.add(event)
    db.session.commit()
    return rec.id, seg.id, event.id


class _Base(unittest.TestCase):
    def setUp(self):
        self.t = make_test_app(csrf=False)
        self.account = seed.make_account(name='Going')
        self.other = seed.make_account(name='Staying')
        db.session.commit()
        self.kept = seed.make_channel(self.other, stream_id=1, name='Stays')
        db.session.commit()
        self.group = seed.make_group(name='G', members=[self.kept], in_guide=False)
        db.session.commit()

    def tearDown(self):
        self.t.cleanup()

    def _assert_detached(self, channel_id, rec_id, seg_id, event_id):
        db.session.expire_all()
        self.assertIsNone(db.session.get(Channel, channel_id))
        rec = db.session.get(Recording, rec_id)
        self.assertIsNotNone(rec, 'the recording itself is history and stays')
        self.assertIsNone(rec.channel_id)
        seg = db.session.get(RecordingSegment, seg_id)
        self.assertIsNotNone(seg)
        self.assertIsNone(seg.channel_id)
        event = db.session.get(ChannelGroupEvent, event_id)
        self.assertIsNotNone(event, 'the event keeps its row and its text')
        self.assertIsNone(event.channel_id)
        self.assertEqual(event.detail, '"Gone" switched off')


class MissingChannelDeleteTests(_Base):
    def test_segment_and_group_event_lose_the_deleted_channels_id(self):
        ch = seed.make_channel(self.account, stream_id=2, name='Gone')
        now = datetime.utcnow()
        ch.last_seen_at = now - timedelta(days=30)
        self.account.last_sync_at = now - timedelta(days=1)
        db.session.commit()
        channel_id = ch.id
        ids = _history(ch, self.group)

        resp = self.t.client.post('/channels/missing-delete', json={'channel_ids': [channel_id]})
        self.assertEqual(resp.status_code, 200, resp.get_data(as_text=True))
        self.assertEqual(resp.get_json()['deleted_count'], 1)
        self.assertEqual(resp.get_json()['recordings_preserved'], 1)
        self._assert_detached(channel_id, *ids)


    def test_a_converting_recording_is_unlinked_too(self):
        """CONVERTING neither blocks the delete nor was in the three statuses it used to
        clear, so such a recording kept the gone id."""
        ch = seed.make_channel(self.account, stream_id=2, name='Gone')
        now = datetime.utcnow()
        ch.last_seen_at = now - timedelta(days=30)
        self.account.last_sync_at = now - timedelta(days=1)
        db.session.commit()
        channel_id = ch.id
        rec = seed.make_recording(status=REC_STATUS_CONVERTING, channel_id=channel_id, name='Mid')
        db.session.commit()
        rec_id = rec.id

        resp = self.t.client.post('/channels/missing-delete', json={'channel_ids': [channel_id]})
        self.assertEqual(resp.get_json().get('deleted_count'), 1, resp.get_data(as_text=True))
        db.session.expire_all()
        self.assertIsNone(db.session.get(Recording, rec_id).channel_id)


class AccountDeleteTests(_Base):
    def test_a_recording_still_capturing_keeps_its_link(self):
        """A NULL channel_id on a running recording reads as "recorded from a typed URL", so
        the delete leaves a live recording and its open segment as they are."""
        from app.routes.accounts import _delete_account_and_jobs
        ch = seed.make_channel(self.account, stream_id=2, name='Gone')
        db.session.commit()
        channel_id = ch.id
        rec = seed.make_recording(status=REC_STATUS_IN_PROGRESS, channel_id=channel_id, name='Live')
        db.session.flush()
        seg = seed.make_segment(rec, ch, datetime.utcnow(), None)
        db.session.commit()
        rec_id, seg_id = rec.id, seg.id

        deleted, _name = _delete_account_and_jobs(self.account.id)
        self.assertTrue(deleted)
        db.session.expire_all()
        self.assertEqual(db.session.get(Recording, rec_id).channel_id, channel_id)
        self.assertEqual(db.session.get(RecordingSegment, seg_id).channel_id, channel_id)


    def test_recording_segment_and_group_event_lose_the_deleted_channels_id(self):
        from app.routes.accounts import _delete_account_and_jobs
        ch = seed.make_channel(self.account, stream_id=2, name='Gone')
        db.session.commit()
        channel_id = ch.id
        ids = _history(ch, self.group)

        deleted, _name = _delete_account_and_jobs(self.account.id)
        self.assertTrue(deleted)
        self._assert_detached(channel_id, *ids)

    def test_a_channel_given_the_same_id_inherits_nothing(self):
        from app.routes.accounts import _delete_account_and_jobs
        ch = seed.make_channel(self.account, stream_id=2, name='Gone')
        db.session.commit()
        channel_id = ch.id
        rec_id, seg_id, event_id = _history(ch, self.group)
        _delete_account_and_jobs(self.account.id)

        reborn = seed.make_channel(self.other, stream_id=3, name='Someone else')
        db.session.commit()
        self.assertEqual(reborn.id, channel_id, 'SQLite re-issues the top id; the premise of the test')
        db.session.expire_all()
        self.assertIsNone(db.session.get(Recording, rec_id).channel)
        self.assertEqual(RecordingSegment.query.filter_by(channel_id=reborn.id).count(), 0)
        self.assertEqual(ChannelGroupEvent.query.filter_by(channel_id=reborn.id).count(), 0)


def _three_more(channel_id, *, rule_recording_id=None):
    """A rule's ledger row, a Recent plays line and a similarity-scan row naming the channel
    (dev/docs/BUGS.md 2026-11-16, dev/changelog/1594). Foreign keys are off, so the rule and
    the scan need no rows of their own."""
    from app.database import (EpgScanScheduleChannel, OutputFeedPlay, RuleMatch,
                              RULE_MATCH_SCHEDULED)
    now = datetime.utcnow()
    match = RuleMatch(rule_id=1, channel_id=channel_id, title_key='game', episode_key='',
                      program_start_time=now, program_stop_time=now + timedelta(hours=1),
                      action_taken=RULE_MATCH_SCHEDULED, recording_id=rule_recording_id)
    play = OutputFeedPlay(kind='play', at=now, first_at=now, feed_id=7, channel_id=channel_id,
                          channel_name='Gone')
    scan = EpgScanScheduleChannel(scan_id=1, schedule_id=1, channel_id=channel_id)
    db.session.add_all([match, play, scan])
    db.session.commit()
    return match.id, play.id, scan.id


class ThreeMoreColumnsTests(_Base):
    def _assert_cleared(self, ids):
        from app.database import EpgScanScheduleChannel, OutputFeedPlay, RuleMatch
        match_id, play_id, scan_id = ids
        db.session.expire_all()
        match = db.session.get(RuleMatch, match_id)
        self.assertIsNotNone(match, "the ledger row is the rule's history and stays")
        self.assertIsNone(match.channel_id)
        play = db.session.get(OutputFeedPlay, play_id)
        self.assertIsNone(play.channel_id)
        self.assertEqual('Gone', play.channel_name, 'the line keeps its own text')
        self.assertIsNone(db.session.get(EpgScanScheduleChannel, scan_id))

    def test_the_account_delete_clears_all_three(self):
        from app.routes.accounts import _delete_account_and_jobs
        ch = seed.make_channel(self.account, stream_id=2, name='Gone')
        db.session.commit()
        ids = _three_more(ch.id)
        deleted, _name = _delete_account_and_jobs(self.account.id)
        self.assertTrue(deleted)
        self._assert_cleared(ids)

    def test_the_missing_channel_delete_clears_all_three(self):
        ch = seed.make_channel(self.account, stream_id=2, name='Gone')
        now = datetime.utcnow()
        ch.last_seen_at = now - timedelta(days=30)
        self.account.last_sync_at = now - timedelta(days=1)
        db.session.commit()
        ids = _three_more(ch.id)
        resp = self.t.client.post('/channels/missing-delete', json={'channel_ids': [ch.id]})
        self.assertEqual(resp.status_code, 200, resp.get_data(as_text=True))
        self._assert_cleared(ids)

    def test_a_rule_reaches_through_no_stranger_after_the_id_is_reissued(self):
        """The harm itself: the ledger's channel is one of the rule's reach inputs."""
        from app import auto_rules
        from app.routes.accounts import _delete_account_and_jobs
        ch = seed.make_channel(self.account, stream_id=2, name='Gone')
        db.session.commit()
        gone_id = ch.id
        _three_more(gone_id)
        _delete_account_and_jobs(self.account.id)
        db.session.expire_all()
        by_rule, _members, _sources = auto_rules._reach_inputs([1])
        self.assertEqual([None], [r.channel_id for r in by_rule[1]])

    def test_the_ledger_row_of_a_recording_still_capturing_keeps_its_link(self):
        from app.database import RuleMatch
        from app.routes.accounts import _delete_account_and_jobs
        ch = seed.make_channel(self.account, stream_id=2, name='Gone')
        db.session.commit()
        channel_id = ch.id
        rec = seed.make_recording(status=REC_STATUS_IN_PROGRESS, channel_id=channel_id, name='Live')
        db.session.commit()
        match_id, _play, _scan = _three_more(channel_id, rule_recording_id=rec.id)
        _delete_account_and_jobs(self.account.id)
        db.session.expire_all()
        self.assertEqual(channel_id, db.session.get(RuleMatch, match_id).channel_id)

    def test_other_channels_rows_are_left_alone(self):
        from app.database import (EpgScanScheduleChannel, OutputFeedPlay, RuleMatch,
                                  detach_channel_references)
        match_id, play_id, scan_id = _three_more(self.kept.id)
        detach_channel_references([self.kept.id + 1000])
        db.session.commit()
        self.assertEqual(self.kept.id, db.session.get(RuleMatch, match_id).channel_id)
        self.assertEqual(self.kept.id, db.session.get(OutputFeedPlay, play_id).channel_id)
        self.assertIsNotNone(db.session.get(EpgScanScheduleChannel, scan_id))


class HelperTests(_Base):
    def test_other_channels_rows_are_left_alone(self):
        """A characterization of the helper's scope, not a guard of the defect."""
        from app.database import detach_channel_references
        ids = _history(self.kept, self.group)
        self.assertEqual(detach_channel_references([self.kept.id + 1000]), 0)
        self.assertEqual(detach_channel_references([]), 0)
        db.session.commit()
        rec_id, seg_id, event_id = ids
        self.assertEqual(db.session.get(Recording, rec_id).channel_id, self.kept.id)
        self.assertEqual(db.session.get(RecordingSegment, seg_id).channel_id, self.kept.id)
        self.assertEqual(db.session.get(ChannelGroupEvent, event_id).channel_id, self.kept.id)


if __name__ == '__main__':
    unittest.main()
