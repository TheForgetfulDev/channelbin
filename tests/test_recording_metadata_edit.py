"""The surface that previews and corrects what a recording tells a media server.

Guards dev/changelog/1058: `metadata_title` overrides the written title without touching
the immutable `program_*` snapshot, `metadata_sidecar_enabled` is the innermost level of
the sidecar's three-level gate, and `apply_user_edit()` is the one writer of the lock -
moving the column and writing the event that explains it together, because a switch storing
the user's answer to a judgment call may never move with nothing on any surface saying so.
"""
import json
import os
import tempfile
import unittest
from datetime import datetime, timedelta

from app import db
from app.database import (
    Recording, RecordingEvent, RecordingProfile, RECORDING_METADATA_EDITED,
)
from app.metadata_sidecar import (
    NFO_SUFFIX, derived_title, globally_enabled, sidecar_enabled, sidecar_source,
)
from app.recording_metadata import UNSET, apply_user_edit, validate_edit
from tests.support import make_test_app, seed


def _cfg(enabled):
    return {'recording': {'metadata_sidecar': {'enabled': enabled}}}


class _Base(unittest.TestCase):
    def setUp(self):
        self.t = make_test_app()
        self.t.app.config['WTF_CSRF_ENABLED'] = False
        self.ctx = self.t.app.app_context()
        self.ctx.push()
        self.account = seed.make_account()
        self.channel = seed.make_channel(self.account, name='Edit Channel')
        db.session.commit()

    def tearDown(self):
        self.ctx.pop()
        self.t.cleanup()

    def _rec(self, **kw):
        kw.setdefault('channel_id', self.channel.id)
        rec = seed.make_recording(**kw)
        db.session.commit()
        return rec

    def _events(self, rec):
        return RecordingEvent.query.filter_by(
            recording_id=rec.id, event_type=RECORDING_METADATA_EDITED).all()


class WrittenTitleTests(_Base):
    """metadata_title is the written title; program_title keeps its one meaning."""

    def test_the_derived_title_joins_program_title_and_sub_title(self):
        rec = self._rec(program_title='NASCAR Cup Series',
                        program_sub_title='Enjoy Illinois 300')
        self.assertEqual(derived_title(rec),
                         'NASCAR Cup Series - Enjoy Illinois 300')

    def test_the_derived_title_falls_back_to_the_recordings_own_name(self):
        rec = self._rec(name='manual_capture')
        self.assertEqual(derived_title(rec), 'manual_capture')

    def test_a_typed_title_is_what_the_sidecar_writes(self):
        from app.metadata_sidecar import _display_title
        rec = self._rec(program_title='MLB Baseball')
        apply_user_edit(rec, {'metadata_title': 'Yankees at Red Sox'})
        db.session.commit()
        self.assertEqual(_display_title(rec), 'Yankees at Red Sox')

    def test_typing_a_title_never_touches_the_program_snapshot(self):
        """The whole reason metadata_title is its own column: program_* answers "what did
        we plan to record" and must not acquire a second meaning."""
        rec = self._rec(program_title='MLB Baseball', program_sub_title='Game 4')
        apply_user_edit(rec, {'metadata_title': 'Something else entirely'})
        db.session.commit()
        db.session.expire_all()
        rec = db.session.get(Recording, rec.id)
        self.assertEqual(rec.program_title, 'MLB Baseball')
        self.assertEqual(rec.program_sub_title, 'Game 4')

    def test_clearing_the_title_restores_the_derived_one(self):
        from app.metadata_sidecar import _display_title
        rec = self._rec(program_title='MLB Baseball', metadata_title='Typed')
        apply_user_edit(rec, {'metadata_title': ''})
        db.session.commit()
        self.assertIsNone(rec.metadata_title)
        self.assertEqual(_display_title(rec), 'MLB Baseball')


class ApplyUserEditTests(_Base):
    """The one writer: what it moves, what it leaves alone, and what it logs."""

    def test_a_change_writes_one_event_naming_both_sides(self):
        rec = self._rec(metadata_description='Old blurb')
        apply_user_edit(rec, {'metadata_description': 'New blurb'})
        db.session.commit()
        events = self._events(rec)
        self.assertEqual(len(events), 1)
        self.assertIn('Old blurb', events[0].detail)
        self.assertIn('New blurb', events[0].detail)

    def test_an_unchanged_submission_writes_nothing(self):
        """An event on every Save saying nothing happened would bury the ones that matter."""
        rec = self._rec(metadata_description='Same')
        moved = apply_user_edit(rec, {'metadata_description': 'Same'})
        db.session.commit()
        self.assertEqual(moved, [])
        self.assertEqual(self._events(rec), [])

    def test_a_field_left_out_is_not_cleared(self):
        rec = self._rec(metadata_description='Keep me', metadata_category='Sports')
        apply_user_edit(rec, {'metadata_category': 'News'})
        db.session.commit()
        self.assertEqual(rec.metadata_description, 'Keep me')
        self.assertEqual(rec.metadata_category, 'News')

    def test_a_blank_field_is_stored_as_none_not_empty_string(self):
        rec = self._rec(metadata_category='Sports')
        apply_user_edit(rec, {'metadata_category': '   '})
        db.session.commit()
        self.assertIsNone(rec.metadata_category)

    def test_the_event_names_only_the_fields_that_actually_moved(self):
        rec = self._rec(metadata_category='Sports', metadata_rating='TV-PG')
        apply_user_edit(rec, {'metadata_category': 'Sports',    # unchanged
                              'metadata_rating': 'TV-14'})      # moved
        db.session.commit()
        extra = json.loads(self._events(rec)[0].extra_data)
        self.assertEqual(extra['fields'], ['metadata_rating'])

    def test_the_lock_moves_and_is_logged_in_the_same_call(self):
        rec = self._rec()
        self.assertFalse(rec.metadata_locked)
        apply_user_edit(rec, {}, lock=True)
        db.session.commit()
        self.assertTrue(rec.metadata_locked)
        self.assertIn('Lock turned on', self._events(rec)[0].detail)

    def test_unlocking_is_logged_too(self):
        rec = self._rec(metadata_locked=True)
        apply_user_edit(rec, {}, lock=False)
        db.session.commit()
        self.assertFalse(rec.metadata_locked)
        self.assertIn('Lock turned off', self._events(rec)[0].detail)

    def test_an_omitted_lock_leaves_the_lock_alone(self):
        """UNSET, not None: a caller that does not mention the lock must not clear it."""
        rec = self._rec(metadata_locked=True)
        apply_user_edit(rec, {'metadata_category': 'Sports'}, lock=UNSET)
        db.session.commit()
        self.assertTrue(rec.metadata_locked)

    def test_the_sidecar_switch_moves_and_is_logged(self):
        rec = self._rec()
        apply_user_edit(rec, {}, sidecar_enabled=False)
        db.session.commit()
        self.assertIs(rec.metadata_sidecar_enabled, False)
        self.assertIn('Metadata file', self._events(rec)[0].detail)

    def test_the_sidecar_switch_can_be_set_back_to_inherit(self):
        rec = self._rec(metadata_sidecar_enabled=False)
        apply_user_edit(rec, {}, sidecar_enabled=None)
        db.session.commit()
        self.assertIsNone(rec.metadata_sidecar_enabled)
        self.assertIn('follow its profile', self._events(rec)[0].detail)

    def test_apply_user_edit_does_not_commit(self):
        """The caller owns the commit so the whole read-modify-write stays in one
        retry_on_locked unit - committing here would split it in two."""
        rec = self._rec(metadata_category='Sports')
        apply_user_edit(rec, {'metadata_category': 'News'})
        db.session.rollback()
        db.session.expire_all()
        self.assertEqual(db.session.get(Recording, rec.id).metadata_category, 'Sports')


class ValidationTests(unittest.TestCase):
    """Server-side limits, spelled once in recording_metadata.MAX_LENGTHS."""

    def test_a_value_within_the_limit_is_accepted(self):
        self.assertIsNone(validate_edit({'metadata_rating': 'TV-PG'}))

    def test_an_over_length_value_is_refused_by_name(self):
        problem = validate_edit({'metadata_rating': 'x' * 65})
        self.assertIsNotNone(problem)
        self.assertIn('Rating', problem)

    def test_a_non_string_is_refused(self):
        self.assertIsNotNone(validate_edit({'metadata_category': 42}))

    def test_none_is_allowed(self):
        self.assertIsNone(validate_edit({'metadata_category': None}))


class SidecarGateLevelTests(_Base):
    """The three-level gate, innermost first."""

    def test_a_recording_override_beats_its_profile(self):
        profile = RecordingProfile(name='P', metadata_sidecar_enabled=True)
        db.session.add(profile)
        db.session.flush()
        rec = self._rec(profile_id=profile.id, metadata_sidecar_enabled=False)
        self.assertEqual(sidecar_source(_cfg(True), profile, rec), ('recording', False))

    def test_a_recording_override_beats_the_global_setting(self):
        rec = self._rec(metadata_sidecar_enabled=True)
        self.assertEqual(sidecar_source(_cfg(False), None, rec), ('recording', True))

    def test_no_recording_override_falls_through_to_the_profile(self):
        profile = RecordingProfile(name='P', metadata_sidecar_enabled=False)
        db.session.add(profile)
        db.session.flush()
        rec = self._rec(profile_id=profile.id)
        self.assertEqual(sidecar_source(_cfg(True), profile, rec), ('profile', False))

    def test_no_override_anywhere_falls_through_to_the_global_setting(self):
        rec = self._rec()
        self.assertEqual(sidecar_source(_cfg(True), None, rec), ('global', True))

    def test_a_recording_override_of_false_is_an_answer_not_an_absence(self):
        """The case a truthiness test silently drops."""
        rec = self._rec(metadata_sidecar_enabled=False)
        self.assertFalse(sidecar_enabled(_cfg(True), None, rec))

    def test_globally_enabled_ignores_every_override(self):
        """The switch that decides whether the UI exists is not the resolved answer."""
        rec = self._rec(metadata_sidecar_enabled=True)
        self.assertFalse(globally_enabled(_cfg(False)))
        self.assertTrue(sidecar_enabled(_cfg(False), None, rec))


class SaveRouteTests(_Base):
    """POST /recordings/<id>/metadata-json."""

    def _post(self, rec, payload):
        return self.t.client.post(f'/recordings/{rec.id}/metadata-json',
                                  json=payload)

    def test_a_save_stores_the_fields_and_reports_what_changed(self):
        rec = self._rec()
        resp = self._post(rec, {'metadata_category': 'Sports', 'locked': True})
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(resp.get_json()['success'])
        self.assertTrue(resp.get_json()['changed'])
        db.session.expire_all()
        rec = db.session.get(Recording, rec.id)
        self.assertEqual(rec.metadata_category, 'Sports')
        self.assertTrue(rec.metadata_locked)

    def test_an_unknown_recording_is_a_404(self):
        resp = self.t.client.post('/recordings/999999/metadata-json', json={})
        self.assertEqual(resp.status_code, 404)
        self.assertIn('error', resp.get_json())

    def test_an_over_length_value_is_refused_with_400(self):
        rec = self._rec()
        resp = self._post(rec, {'metadata_rating': 'x' * 200})
        self.assertEqual(resp.status_code, 400)
        self.assertIn('error', resp.get_json())

    def test_a_non_boolean_lock_is_refused(self):
        rec = self._rec()
        resp = self._post(rec, {'locked': 'yes'})
        self.assertEqual(resp.status_code, 400)

    def test_the_sidecar_switch_is_refused_when_the_feature_is_off_globally(self):
        """The UI hides this control, and presentation is never the gate."""
        rec = self._rec()
        resp = self._post(rec, {'sidecar_enabled': True})
        self.assertEqual(resp.status_code, 400)
        self.assertIn('Settings', resp.get_json()['error'])
        db.session.expire_all()
        self.assertIsNone(db.session.get(Recording, rec.id).metadata_sidecar_enabled)

    def test_the_response_carries_the_refreshed_panel(self):
        rec = self._rec(program_title='MLB Baseball')
        resp = self._post(rec, {'metadata_title': 'Yankees at Red Sox'})
        meta = resp.get_json()['meta']
        self.assertEqual(meta['title'], 'Yankees at Red Sox')
        self.assertEqual(meta['derived_title'], 'MLB Baseball')

    def test_a_scheduled_recording_reports_the_before_phase(self):
        rec = self._rec(status='SCHEDULED')
        meta = self._post(rec, {}).get_json()['meta']
        self.assertEqual(meta['phase'], 'before')

    def test_a_completed_recording_reports_the_after_phase(self):
        rec = self._rec(status='COMPLETED')
        meta = self._post(rec, {}).get_json()['meta']
        self.assertEqual(meta['phase'], 'after')

    def test_a_save_that_changes_nothing_writes_no_event(self):
        rec = self._rec(metadata_category='Sports')
        resp = self._post(rec, {'metadata_category': 'Sports'})
        self.assertEqual(resp.get_json()['changed'], [])
        self.assertEqual(self._events(rec), [])


class SidecarRewriteTests(_Base):
    """A correction to a finished recording reaches the file on disk."""

    def setUp(self):
        super().setUp()
        self._dir = tempfile.TemporaryDirectory()
        self.addCleanup(self._dir.cleanup)
        self.video = os.path.join(self._dir.name, 'game.ts')
        with open(self.video, 'w') as fh:
            fh.write('not really video')

    def _finished(self, **kw):
        return self._rec(status='COMPLETED', output_path=self.video,
                         program_title='MLB Baseball',
                         program_start_time=datetime.utcnow() - timedelta(hours=2),
                         **kw)

    def _post(self, rec, payload):
        return self.t.client.post(f'/recordings/{rec.id}/metadata-json', json=payload)

    def test_editing_a_finished_recording_rewrites_its_nfo(self):
        rec = self._finished(metadata_sidecar_enabled=True)
        resp = self._post(rec, {'metadata_description': 'Corrected by hand.'})
        self.assertTrue(resp.get_json()['sidecar_rewritten'])
        nfo = os.path.splitext(self.video)[0] + NFO_SUFFIX
        self.assertTrue(os.path.exists(nfo))
        with open(nfo) as fh:
            self.assertIn('Corrected by hand.', fh.read())

    def test_a_typed_title_lands_in_the_nfo(self):
        rec = self._finished(metadata_sidecar_enabled=True)
        self._post(rec, {'metadata_title': 'Yankees at Red Sox'})
        nfo = os.path.splitext(self.video)[0] + NFO_SUFFIX
        with open(nfo) as fh:
            body = fh.read()
        self.assertIn('<title>Yankees at Red Sox</title>', body)
        self.assertNotIn('MLB Baseball', body)

    def test_turning_the_switch_off_leaves_an_existing_file_alone(self):
        """Deleting a user's metadata files as a side effect of a checkbox is the thing
        the app refuses to do - the modal says so instead."""
        rec = self._finished(metadata_sidecar_enabled=True)
        self._post(rec, {'metadata_description': 'First pass.'})
        nfo = os.path.splitext(self.video)[0] + NFO_SUFFIX
        self.assertTrue(os.path.exists(nfo))

        rec.metadata_sidecar_enabled = False
        db.session.commit()
        resp = self._post(rec, {'metadata_description': 'Second pass.'})
        self.assertFalse(resp.get_json()['sidecar_rewritten'])
        self.assertTrue(os.path.exists(nfo))
        with open(nfo) as fh:
            self.assertIn('First pass.', fh.read())

    def test_a_scheduled_recording_writes_no_file(self):
        """Nothing has been captured, so there is no video for a sidecar to describe."""
        rec = self._rec(status='SCHEDULED', output_path=self.video,
                        metadata_sidecar_enabled=True)
        resp = self._post(rec, {'metadata_description': 'Too early.'})
        self.assertFalse(resp.get_json()['sidecar_rewritten'])
        self.assertFalse(
            os.path.exists(os.path.splitext(self.video)[0] + NFO_SUFFIX))

    def test_a_missing_video_writes_no_file(self):
        """A sidecar beside a video that is gone would be read onto whatever else the
        server matches."""
        rec = self._finished(metadata_sidecar_enabled=True)
        os.remove(self.video)
        resp = self._post(rec, {'metadata_description': 'Nothing to describe.'})
        self.assertFalse(resp.get_json()['sidecar_rewritten'])


class ProgramCardTests(_Base):
    """What the detail page renders, and the payload the modal opens from."""

    def test_the_card_carries_its_own_data_meta_payload(self):
        rec = self._rec(program_title='MLB Baseball')
        html = self.t.client.get(f'/recordings/{rec.id}').get_data(as_text=True)
        self.assertIn('data-meta=', html)
        self.assertIn('data-meta-edit', html)

    def test_a_typed_title_is_shown_as_written_as(self):
        rec = self._rec(program_title='MLB Baseball',
                        metadata_title='Yankees at Red Sox')
        html = self.t.client.get(f'/recordings/{rec.id}').get_data(as_text=True)
        self.assertIn('Written as', html)
        self.assertIn('Yankees at Red Sox', html)

    def test_a_manual_recording_shows_no_card_while_the_feature_is_off(self):
        rec = self._rec(name='manual_capture', channel_id=None)
        html = self.t.client.get(f'/recordings/{rec.id}').get_data(as_text=True)
        self.assertNotIn('id="panel-program"', html)

    def _with_sidecars_on(self):
        """Turn the global setting on for one render.

        Patched on the route's own binding rather than passed as a make_test_app override:
        the route calls `load_config()` at request time, so it reads the real config.yaml
        and an override would never reach it (CLAUDE.md §Testing).
        """
        from unittest import mock
        from app.routes import recordings as routes
        cfg = dict(routes.load_config())
        cfg['recording'] = dict(cfg.get('recording', {}),
                                metadata_sidecar={'enabled': True})
        return mock.patch.object(routes, 'load_config', lambda *a, **k: cfg)

    def test_a_manual_recording_gets_the_card_once_the_feature_is_on(self):
        """It has no program behind it, but it does get a .nfo, and Edit details is the
        only way to give that file anything to say."""
        rec = self._rec(name='manual_capture', channel_id=None)
        with self._with_sidecars_on():
            html = self.t.client.get(f'/recordings/{rec.id}').get_data(as_text=True)
        self.assertIn('id="panel-program"', html)
        self.assertIn('data-meta-edit', html)
        self.assertIn('Nothing is known about what this recording contains', html)

    def test_the_card_names_which_level_decided_the_sidecar_answer(self):
        profile = RecordingProfile(name='No sidecars', metadata_sidecar_enabled=False)
        db.session.add(profile)
        db.session.flush()
        rec = self._rec(program_title='MLB Baseball', profile_id=profile.id)
        with self._with_sidecars_on():
            html = self.t.client.get(f'/recordings/{rec.id}').get_data(as_text=True)
        self.assertIn('Metadata file', html)
        self.assertIn('Off via its profile', html)


if __name__ == '__main__':
    unittest.main()
