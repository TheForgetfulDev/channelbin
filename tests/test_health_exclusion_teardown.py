"""A health-score exclusion dies with the row it excludes (dev/changelog/1129).

`ChannelHealthExclusion` names an observation by (channel_id, source_kind, source_id), and
foreign keys are off, so nothing removed an exclusion when its recording, test or channel
was deleted. `channel_tests` and `channel_events` also re-issued a top-of-table id, so a
new test could inherit a deleted one's exclusion and be silently left out of the score,
its timeline row reading "not counted".

Covers each path a source row dies on:
  - a recording delete (detach_recording_references, shared by every recording delete
    path) takes the recording-keyed kinds and nothing keyed on a channel event id;
  - a health-check delete (delete_tests_collecting_screenshots, shared by the job delete,
    member removal and the retention prune) takes the `test` rows for exactly those ids;
  - the missing-channel bulk delete, which skips the ORM cascade, takes every exclusion
    on the deleted channels;
  - and a fresh schema retires deleted test and event ids (the models'
    sqlite_autoincrement; the migrations are covered in test_migrations_runner.py).

No network, no real ffmpeg - see CLAUDE.md §Testing.
"""
import os
import sys
import unittest
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import db  # noqa: E402
from app.config import load_config  # noqa: E402
from app.database import (ChannelEvent, ChannelHealthExclusion, ChannelTest,  # noqa: E402
                          detach_recording_references)
from app.health_recompute import (SOURCE_CAPTURE_CORRECTION, SOURCE_FAILOVER,  # noqa: E402
                                  SOURCE_PLACEHOLDER, SOURCE_RECORDING, SOURCE_TEST,
                                  observation_ledger)
from tests.support import make_test_app  # noqa: E402
from tests.support import seed  # noqa: E402


class _Base(unittest.TestCase):
    def setUp(self):
        self.t = make_test_app()
        self.t.app.config['WTF_CSRF_ENABLED'] = False
        self.account = seed.make_account()
        self.channel = seed.make_channel(self.account, name='Excluded Channel')
        db.session.commit()

    def tearDown(self):
        self.t.cleanup()

    def _exclude(self, kind, source_id, channel=None):
        db.session.add(ChannelHealthExclusion(
            channel_id=(channel or self.channel).id, source_kind=kind,
            source_id=source_id, action='reset'))
        db.session.commit()

    @staticmethod
    def _keys(channel_id=None):
        q = ChannelHealthExclusion.query
        if channel_id is not None:
            q = q.filter_by(channel_id=channel_id)
        return {(e.source_kind, e.source_id) for e in q.all()}


class RecordingDeleteTests(_Base):
    """dev/docs/BUGS.md 2026-09-26 - exclusions outlived the recording they excluded."""

    def test_recording_delete_takes_its_own_exclusions(self):
        rec = seed.make_recording(status='COMPLETED', channel_id=self.channel.id)
        other = seed.make_recording(status='COMPLETED', channel_id=self.channel.id)
        db.session.commit()
        self._exclude(SOURCE_RECORDING, rec.id)
        self._exclude(SOURCE_CAPTURE_CORRECTION, rec.id)
        self._exclude(SOURCE_RECORDING, other.id)
        rec_id, other_id = rec.id, other.id

        resp = self.t.client.post(f'/recordings/{rec_id}/delete-json')
        self.assertEqual(resp.status_code, 200, resp.get_data(as_text=True))
        db.session.expire_all()

        self.assertEqual(self._keys(), {(SOURCE_RECORDING, other_id)},
                         'a deleted recording left its exclusions behind, or took another\'s')

    def test_event_keyed_kinds_sharing_the_number_survive(self):
        """The failover/stall/placeholder/fast-delivery kinds name a channel EVENT id. The
        event outlives the recording, and a match by number alone would delete an
        exclusion that has nothing to do with the recording."""
        rec = seed.make_recording(status='COMPLETED', channel_id=self.channel.id)
        db.session.commit()
        self._exclude(SOURCE_RECORDING, rec.id)
        self._exclude(SOURCE_FAILOVER, rec.id)
        self._exclude(SOURCE_PLACEHOLDER, rec.id)

        detach_recording_references(rec.id)
        db.session.commit()

        self.assertEqual(self._keys(), {(SOURCE_FAILOVER, rec.id),
                                        (SOURCE_PLACEHOLDER, rec.id)})


class TestDeleteTests(_Base):
    """dev/docs/BUGS.md 2026-09-26 - a deleted test's exclusion waited for its id to be
    issued again."""

    def test_deleting_tests_takes_exactly_their_test_exclusions(self):
        from app.channel_tester import delete_tests_collecting_screenshots
        gone = seed.make_channel_test(self.channel, status='COMPLETED')
        kept = seed.make_channel_test(self.channel, status='COMPLETED')
        db.session.commit()
        self._exclude(SOURCE_TEST, gone.id)
        self._exclude(SOURCE_TEST, kept.id)
        # A recording sharing the number is a different id space and must survive.
        self._exclude(SOURCE_RECORDING, gone.id)
        gone_id, kept_id = gone.id, kept.id

        delete_tests_collecting_screenshots(ChannelTest.query.filter_by(id=gone_id))
        db.session.commit()

        self.assertEqual(self._keys(), {(SOURCE_TEST, kept_id), (SOURCE_RECORDING, gone_id)})

    def test_the_retention_prune_takes_the_pruned_tests_exclusions(self):
        from app.channel_tester import _cleanup_old_tests
        old = seed.make_channel_test(self.channel, status='COMPLETED')
        new = seed.make_channel_test(self.channel, status='COMPLETED')
        old.test_started_at = datetime.utcnow() - timedelta(days=2)
        new.test_started_at = datetime.utcnow() - timedelta(hours=1)
        db.session.commit()
        self._exclude(SOURCE_TEST, old.id)
        self._exclude(SOURCE_TEST, new.id)
        old_id, new_id = old.id, new.id

        _cleanup_old_tests(self.t.app, self.channel.id, None, keep_count=1)
        db.session.expire_all()

        self.assertIsNone(db.session.get(ChannelTest, old_id), 'fixture: the prune must run')
        self.assertEqual(self._keys(), {(SOURCE_TEST, new_id)})

    def test_a_test_created_after_a_delete_is_counted(self):
        """The failure the user would see: delete the newest check, run another, and the
        new one reads "not counted". Both halves stop it - the exclusion goes with its test,
        and the id is never handed out again."""
        from app.channel_tester import delete_tests_collecting_screenshots
        first = seed.make_channel_test(self.channel, status='COMPLETED', quality_score=80)
        db.session.commit()
        self._exclude(SOURCE_TEST, first.id)
        first_id = first.id

        delete_tests_collecting_screenshots(ChannelTest.query.filter_by(id=first_id))
        db.session.commit()
        second = seed.make_channel_test(self.channel, status='COMPLETED', quality_score=90)
        db.session.commit()

        self.assertNotEqual(second.id, first_id, 'a deleted test id was issued again')
        ledger = observation_ledger(self.channel.id, load_config())
        self.assertEqual([(o.source_id, o.excluded) for o in ledger], [(second.id, False)])


class MissingChannelDeleteTests(_Base):
    """dev/docs/BUGS.md 2026-09-26 - the bulk delete skipped Channel.health_exclusions'
    ORM cascade."""

    def test_bulk_delete_takes_every_exclusion_on_the_deleted_channels(self):
        now = datetime.utcnow()
        missing = seed.make_channel(self.account, name='Gone From Feed')
        missing.last_seen_at = now - timedelta(days=30)
        self.account.last_sync_at = now - timedelta(days=1)
        db.session.commit()
        ct = seed.make_channel_test(missing, status='COMPLETED')
        db.session.add(ChannelEvent(channel_id=missing.id, event_type='CHANNEL_FAILOVER'))
        db.session.commit()
        self._exclude(SOURCE_TEST, ct.id, channel=missing)
        self._exclude(SOURCE_FAILOVER, 1, channel=missing)
        self._exclude(SOURCE_TEST, 99, channel=self.channel)
        missing_id = missing.id

        resp = self.t.client.post('/channels/missing-delete',
                                  json={'account_id': self.account.id})
        self.assertEqual(resp.get_json().get('deleted_count'), 1, resp.get_data(as_text=True))
        db.session.expire_all()

        self.assertEqual(self._keys(missing_id), set(),
                         'the bulk delete left exclusions naming a deleted channel')
        self.assertEqual(self._keys(self.channel.id), {(SOURCE_TEST, 99)})


class FreshSchemaRetiresIdsTests(_Base):
    """A fresh install never runs migrations 77/78; the models carry AUTOINCREMENT."""

    def test_deleted_test_and_event_ids_are_not_issued_again(self):
        for model, make in (
                (ChannelTest, lambda: seed.make_channel_test(self.channel)),
                (ChannelEvent, lambda: ChannelEvent(channel_id=self.channel.id,
                                                    event_type='CHANNEL_FAILOVER'))):
            with self.subTest(table=model.__tablename__):
                row = make()
                db.session.add(row)
                db.session.commit()
                top = row.id
                db.session.delete(row)
                db.session.commit()
                again = make()
                db.session.add(again)
                db.session.commit()
                self.assertGreater(again.id, top,
                                   f'a deleted {model.__tablename__} id was issued again')


if __name__ == '__main__':
    unittest.main()
