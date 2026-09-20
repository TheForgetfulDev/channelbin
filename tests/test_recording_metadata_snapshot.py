"""A recording's copy of how its program describes itself.

Guards dev/changelog/1055: `Recording.metadata_description` / `_category` / `_rating` are
written when a recording is scheduled and refreshed once at record start, and
`metadata_locked` suppresses that refresh. The values cannot be read back from
`epg_entries` later - `epg_keep_days` prunes a program's row within a day of it airing -
so a snapshot that does not happen is data nobody can recover.
"""
import unittest
from datetime import datetime, timedelta

from app import db
from app.database import (
    Recording, RecordingEvent, EPGEntry,
    RECORDING_METADATA_REFRESHED, RECORDING_METADATA_REFRESH_SKIPPED,
)
from app.recording_metadata import find_program_entry, refresh_from_guide
from tests.support import make_test_app, seed


class CreationSnapshotTests(unittest.TestCase):
    """POST /recordings/new-json copies the program's synopsis, genre and rating."""

    def setUp(self):
        # The route schedules the start job, so it needs a live scheduler. Every POST here
        # starts an hour out, so nothing fires and no capture is ever spawned.
        self.t = make_test_app(start_scheduler=True)
        # This class posts the real create form; CSRFProtect is app-wide and a test client
        # holds no token (tests/test_contention.py does the same).
        self.t.app.config['WTF_CSRF_ENABLED'] = False
        self.ctx = self.t.app.app_context()
        self.ctx.push()
        self.account = seed.make_account()
        self.channel = seed.make_channel(self.account, name='Snapshot Channel',
                                         in_guide=True)
        self.entry = seed.make_epg_entry(
            self.channel, title='The Big Race', offset_minutes=60,
            description='Forty cars, one rain delay.',
            category='Sports', rating='TV-PG')
        db.session.commit()

    def tearDown(self):
        self.ctx.pop()
        self.t.cleanup()

    def _post(self, **extra):
        start = datetime.now() + timedelta(hours=1)
        stop = start + timedelta(hours=1)
        data = {
            'name': 'snapshot_rec',
            'url': 'http://example.test/live/1',
            'start_time': start.strftime('%Y-%m-%dT%H:%M'),
            'stop_time': stop.strftime('%Y-%m-%dT%H:%M'),
            'channel_id': str(self.channel.id),
        }
        data.update(extra)
        return self.t.client.post('/recordings/new-json', data=data)

    def test_a_guide_recording_carries_all_three_values(self):
        resp = self._post(source_epg_id=str(self.entry.id))
        self.assertEqual(resp.status_code, 200, resp.get_data(as_text=True))
        rec = Recording.query.filter_by(name='snapshot_rec').one()
        self.assertEqual(rec.metadata_description, 'Forty cars, one rain delay.')
        self.assertEqual(rec.metadata_category, 'Sports')
        self.assertEqual(rec.metadata_rating, 'TV-PG')

    def test_a_manual_recording_carries_none_of_them(self):
        """No source_epg_id posted is the manual case, and it stays empty - exactly as it
        carries no program_title today."""
        resp = self._post()
        self.assertEqual(resp.status_code, 200, resp.get_data(as_text=True))
        rec = Recording.query.filter_by(name='snapshot_rec').one()
        self.assertIsNone(rec.metadata_description)
        self.assertIsNone(rec.metadata_category)
        self.assertIsNone(rec.metadata_rating)
        self.assertIsNone(rec.program_title)

    def test_the_lock_starts_off(self):
        self._post(source_epg_id=str(self.entry.id))
        rec = Recording.query.filter_by(name='snapshot_rec').one()
        self.assertFalse(rec.metadata_locked)


class RecordStartRefreshTests(unittest.TestCase):
    """The refresh reads the listing again and only writes what actually moved."""

    def setUp(self):
        self.t = make_test_app()
        self.ctx = self.t.app.app_context()
        self.ctx.push()
        self.account = seed.make_account()
        self.channel = seed.make_channel(self.account, name='Refresh Channel',
                                         in_guide=True)
        self.air = datetime.utcnow().replace(microsecond=0) + timedelta(minutes=5)
        self.entry = seed.make_epg_entry(
            self.channel, title='The Big Race', start_time=self.air,
            description='Forty cars, one rain delay.',
            category='Sports', rating='TV-PG')
        self.rec = seed.make_recording(
            status='IN_PROGRESS', name='refresh_rec', channel_id=self.channel.id,
            program_start_time=self.air, program_title='The Big Race',
            metadata_description='Forty cars.', metadata_category='Sports')
        db.session.commit()

    def tearDown(self):
        self.ctx.pop()
        self.t.cleanup()

    def _events(self, event_type):
        return RecordingEvent.query.filter_by(
            recording_id=self.rec.id, event_type=event_type).all()

    def test_a_changed_synopsis_is_taken_and_logged(self):
        refresh_from_guide(self.rec.id)
        db.session.expire_all()
        rec = db.session.get(Recording, self.rec.id)
        self.assertEqual(rec.metadata_description, 'Forty cars, one rain delay.')
        self.assertEqual(rec.metadata_rating, 'TV-PG')
        events = self._events(RECORDING_METADATA_REFRESHED)
        self.assertEqual(len(events), 1)
        self.assertIn('Forty cars.', events[0].detail)
        self.assertIn('Forty cars, one rain delay.', events[0].detail)

    def test_an_unchanged_program_writes_no_event(self):
        """The ordinary case is silent. An event on every recording saying nothing
        happened would bury the two that mean something."""
        self.rec.metadata_description = 'Forty cars, one rain delay.'
        self.rec.metadata_rating = 'TV-PG'
        db.session.commit()
        refresh_from_guide(self.rec.id)
        db.session.expire_all()
        self.assertEqual(self._events(RECORDING_METADATA_REFRESHED), [])

    def test_a_vanished_listing_leaves_the_snapshot_intact(self):
        db.session.delete(self.entry)
        db.session.commit()
        refresh_from_guide(self.rec.id)
        db.session.expire_all()
        rec = db.session.get(Recording, self.rec.id)
        self.assertEqual(rec.metadata_description, 'Forty cars.')
        self.assertEqual(rec.metadata_category, 'Sports')

    def test_a_listing_that_lost_a_field_does_not_wipe_it(self):
        """A blind assignment would overwrite a captured value with NULL. The provider
        dropping a synopsis is not the provider saying there is none."""
        self.entry.description = None
        db.session.commit()
        refresh_from_guide(self.rec.id)
        db.session.expire_all()
        rec = db.session.get(Recording, self.rec.id)
        self.assertEqual(rec.metadata_description, 'Forty cars.')

    def test_a_program_that_moved_is_not_matched(self):
        """An exact start-time match is the point: a provider that shifted the program
        published a different airing, and describing this recording with whatever else now
        starts nearby would be worse than leaving the snapshot alone."""
        self.entry.start_time = self.air + timedelta(minutes=30)
        db.session.commit()
        refresh_from_guide(self.rec.id)
        db.session.expire_all()
        rec = db.session.get(Recording, self.rec.id)
        self.assertEqual(rec.metadata_description, 'Forty cars.')
        self.assertIsNone(rec.metadata_rating)

    def test_a_manual_recording_is_skipped_entirely(self):
        manual = seed.make_recording(status='IN_PROGRESS', name='manual_rec',
                                     channel_id=self.channel.id)
        db.session.commit()
        refresh_from_guide(manual.id)
        db.session.expire_all()
        rec = db.session.get(Recording, manual.id)
        self.assertIsNone(rec.metadata_description)
        self.assertEqual(RecordingEvent.query.filter_by(recording_id=manual.id).count(), 0)


class MetadataLockTests(unittest.TestCase):
    """The lock filters the refresh. It is never cleared, and the skip is never silent."""

    def setUp(self):
        self.t = make_test_app()
        self.ctx = self.t.app.app_context()
        self.ctx.push()
        self.account = seed.make_account()
        self.channel = seed.make_channel(self.account, in_guide=True)
        self.air = datetime.utcnow().replace(microsecond=0) + timedelta(minutes=5)
        seed.make_epg_entry(self.channel, title='The Big Race', start_time=self.air,
                            description='The provider rewrote this.',
                            category='Motorsport', rating='TV-14')
        self.rec = seed.make_recording(
            status='IN_PROGRESS', name='locked_rec', channel_id=self.channel.id,
            program_start_time=self.air, program_title='The Big Race',
            metadata_description='The wording the user typed.', metadata_category='Sports',
            metadata_locked=True)
        db.session.commit()

    def tearDown(self):
        self.ctx.pop()
        self.t.cleanup()

    def test_a_locked_recording_keeps_exactly_what_it_had(self):
        refresh_from_guide(self.rec.id)
        db.session.expire_all()
        rec = db.session.get(Recording, self.rec.id)
        self.assertEqual(rec.metadata_description, 'The wording the user typed.')
        self.assertEqual(rec.metadata_category, 'Sports')
        self.assertIsNone(rec.metadata_rating)

    def test_the_skip_is_announced(self):
        refresh_from_guide(self.rec.id)
        db.session.expire_all()
        events = RecordingEvent.query.filter_by(
            recording_id=self.rec.id,
            event_type=RECORDING_METADATA_REFRESH_SKIPPED).all()
        self.assertEqual(len(events), 1)

    def test_the_refresh_never_clears_the_lock(self):
        refresh_from_guide(self.rec.id)
        db.session.expire_all()
        rec = db.session.get(Recording, self.rec.id)
        self.assertTrue(rec.metadata_locked)


class GroupProgramLookupTests(unittest.TestCase):
    """A group recording finds its program on whichever member carries the listing."""

    def setUp(self):
        self.t = make_test_app()
        self.ctx = self.t.app.app_context()
        self.ctx.push()
        self.account = seed.make_account()
        self.scheduled_on = seed.make_channel(self.account, name='Feed A')
        self.serving = seed.make_channel(self.account, name='Feed B')
        self.group = seed.make_group(name='Race Group',
                                     members=[self.scheduled_on, self.serving])
        self.air = datetime.utcnow().replace(microsecond=0) + timedelta(minutes=5)
        # Only the member that is NOT serving carries the listing - the shape that breaks
        # a lookup keyed on the recording's re-resolved channel_id alone.
        seed.make_epg_entry(self.scheduled_on, title='The Big Race', start_time=self.air,
                            description='Forty cars, one rain delay.', category='Sports')
        self.rec = seed.make_recording(
            status='IN_PROGRESS', name='group_rec', group_id=self.group.id,
            channel_id=self.serving.id, program_start_time=self.air,
            program_title='The Big Race')
        db.session.commit()

    def tearDown(self):
        self.ctx.pop()
        self.t.cleanup()

    def test_the_listing_is_found_on_a_sibling_member(self):
        entry = find_program_entry(self.rec)
        self.assertIsNotNone(entry)
        self.assertEqual(entry.channel_id, self.scheduled_on.id)

    def test_the_refresh_takes_it(self):
        refresh_from_guide(self.rec.id)
        db.session.expire_all()
        rec = db.session.get(Recording, self.rec.id)
        self.assertEqual(rec.metadata_description, 'Forty cars, one rain delay.')

    def test_the_serving_member_wins_when_both_carry_it(self):
        """Two members carrying the same airing must resolve the same way on every run,
        or the same recording gets a different synopsis depending on query order."""
        db.session.add(EPGEntry(
            channel_id=self.serving.id, title='The Big Race', start_time=self.air,
            stop_time=self.air + timedelta(hours=1),
            description='The serving feed\'s own wording.'))
        db.session.commit()
        entry = find_program_entry(self.rec)
        self.assertEqual(entry.channel_id, self.serving.id)

    def test_a_program_on_a_channel_outside_the_group_is_not_matched(self):
        outsider = seed.make_channel(self.account, name='Unrelated', in_guide=True)
        seed.make_epg_entry(outsider, title='Something Else', start_time=self.air,
                            description='Not this.')
        lone = seed.make_recording(
            status='IN_PROGRESS', name='lone_rec', channel_id=self.serving.id,
            program_start_time=self.air, program_title='The Big Race')
        db.session.commit()
        self.assertIsNone(find_program_entry(lone))


class DetailPageTests(unittest.TestCase):
    """The Program card renders off the row, and stays away when there is no program."""

    def setUp(self):
        self.t = make_test_app()
        self.ctx = self.t.app.app_context()
        self.ctx.push()
        self.account = seed.make_account()
        self.channel = seed.make_channel(self.account, in_guide=True)
        db.session.commit()

    def tearDown(self):
        self.ctx.pop()
        self.t.cleanup()

    def test_the_card_shows_the_synopsis_genre_and_rating(self):
        rec = seed.make_recording(
            status='COMPLETED', name='shown_rec', channel_id=self.channel.id,
            program_title='The Big Race',
            metadata_description='Forty cars, one rain delay.',
            metadata_category='Sports', metadata_rating='TV-PG')
        db.session.commit()
        html = self.t.client.get(f'/recordings/{rec.id}').get_data(as_text=True)
        self.assertIn('Forty cars, one rain delay.', html)
        self.assertIn('Sports', html)
        self.assertIn('TV-PG', html)

    def test_a_manual_recording_gets_no_program_card(self):
        rec = seed.make_recording(status='COMPLETED', name='manual_rec',
                                  channel_id=self.channel.id)
        db.session.commit()
        html = self.t.client.get(f'/recordings/{rec.id}').get_data(as_text=True)
        # The id also appears in the page script's SWAP_IDS list, so the card's own markup
        # is what is asserted on rather than the bare string.
        self.assertNotIn('id="panel-program"', html)

    def test_a_locked_recording_says_so(self):
        rec = seed.make_recording(
            status='COMPLETED', name='locked_shown', channel_id=self.channel.id,
            program_title='The Big Race', metadata_description='Forty cars.',
            metadata_locked=True)
        db.session.commit()
        html = self.t.client.get(f'/recordings/{rec.id}').get_data(as_text=True)
        self.assertIn('Locked', html)


if __name__ == '__main__':
    unittest.main()
