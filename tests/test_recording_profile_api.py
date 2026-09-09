"""Tier 2 - the Recording Profile JSON API behind the create/edit modal
(app/routes/profiles.py, dev/changelog/356).

Sibling of tests/test_health_check_profile_api.py, and the reason both exist is the same:
the standalone form pages were replaced by a modal posting JSON, which moved parsing off
Flask's form dict (everything a string, absent == '') onto a real JSON body. What makes
THIS profile the sharper case is that its fields disagree with each other about what an
empty box means, and two of those readings are opposites:

  * `retention_days` - NULL means "use the global retention window", 0 means "never
    auto-delete, even if a global window is set". Collapsing them does not merely lose a
    setting, it inverts one, and the failure is invisible until files start disappearing
    (or stop).
  * `pre_padding_minutes` / `post_padding_minutes` - NOT NULL columns defaulting to 0, so
    an empty box is a real 0 and must never be stored as NULL.
  * everything else nullable - empty means inherit.

app/profile_forms.py is the one parser all of that runs through; these are the cases that
pin its behavior for this profile type.
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tests.support.app import make_test_app  # noqa: E402
from tests.support import seed  # noqa: E402
from app import db  # noqa: E402
from app.database import RecordingProfile, Channel  # noqa: E402


class _Base(unittest.TestCase):
    def setUp(self):
        self.t = make_test_app()
        # These endpoints are CSRF-protected app-wide like every other mutating route;
        # the token is a browser concern, not what this file is asserting.
        self.t.app.config['WTF_CSRF_ENABLED'] = False
        self.client = self.t.client

    def tearDown(self):
        self.t.cleanup()

    def make_profile(self, **kwargs):
        kwargs.setdefault('name', 'Existing')
        profile = RecordingProfile(**kwargs)
        db.session.add(profile)
        db.session.commit()
        return profile

    def post(self, **body):
        body.setdefault('name', 'New profile')
        return self.client.post('/api/profiles', json=body)

    def put(self, profile_id, **body):
        body.setdefault('name', 'New profile')
        return self.client.put(f'/api/profiles/{profile_id}', json=body)


class RetentionDaysTests(_Base):
    """The field where blank and 0 mean opposite things. Getting these two backwards
    either deletes recordings the user meant to keep forever, or keeps ones they meant
    to expire - and neither shows up anywhere until it has already happened."""

    def test_blank_retention_inherits_the_global_window(self):
        self.assertEqual(self.post(retention_days=None).status_code, 200)
        self.assertIsNone(RecordingProfile.query.one().retention_days)

    def test_zero_retention_means_never_delete_and_is_not_null(self):
        self.assertEqual(self.post(retention_days=0).status_code, 200)
        self.assertEqual(RecordingProfile.query.one().retention_days, 0)

    def test_editing_from_zero_back_to_blank_restores_inheritance(self):
        profile = self.make_profile(retention_days=0)
        self.assertEqual(self.put(profile.id, name='Existing', retention_days=None).status_code, 200)
        db.session.expire_all()
        self.assertIsNone(db.session.get(RecordingProfile, profile.id).retention_days)

    def test_editing_from_blank_to_zero_stores_zero(self):
        profile = self.make_profile(retention_days=None)
        self.assertEqual(self.put(profile.id, name='Existing', retention_days=0).status_code, 200)
        db.session.expire_all()
        self.assertEqual(db.session.get(RecordingProfile, profile.id).retention_days, 0)


class PaddingTests(_Base):
    """NOT NULL columns with a 0 default - a blank box is 0, never NULL. Storing NULL
    here would be an IntegrityError at best and a broken schedule calculation at worst."""

    def test_blank_padding_stores_zero_not_null(self):
        self.assertEqual(self.post(pre_padding_minutes=None,
                                   post_padding_minutes='').status_code, 200)
        profile = RecordingProfile.query.one()
        self.assertEqual(profile.pre_padding_minutes, 0)
        self.assertEqual(profile.post_padding_minutes, 0)

    def test_padding_values_are_kept(self):
        self.assertEqual(self.post(pre_padding_minutes=2, post_padding_minutes=5).status_code, 200)
        profile = RecordingProfile.query.one()
        self.assertEqual(profile.pre_padding_minutes, 2)
        self.assertEqual(profile.post_padding_minutes, 5)

    def test_clearing_padding_on_an_edit_stores_zero(self):
        profile = self.make_profile(pre_padding_minutes=5, post_padding_minutes=5)
        self.assertEqual(self.put(profile.id, name='Existing', pre_padding_minutes=None,
                                  post_padding_minutes=None).status_code, 200)
        db.session.expire_all()
        refreshed = db.session.get(RecordingProfile, profile.id)
        self.assertEqual(refreshed.pre_padding_minutes, 0)
        self.assertEqual(refreshed.post_padding_minutes, 0)


class InheritableFieldTests(_Base):
    def test_blank_watchdog_overrides_store_null(self):
        self.assertEqual(self.post(stall_timeout_seconds=None, restart_delay_seconds='',
                                   max_consecutive_failures=None).status_code, 200)
        profile = RecordingProfile.query.one()
        self.assertIsNone(profile.stall_timeout_seconds)
        self.assertIsNone(profile.restart_delay_seconds)
        self.assertIsNone(profile.max_consecutive_failures)

    def test_blank_filename_template_stores_null_not_empty_string(self):
        """'' would be a template that renders every recording to the same empty name;
        None is what the recorder reads as "use the global template"."""
        self.assertEqual(self.post(filename_template='').status_code, 200)
        self.assertIsNone(RecordingProfile.query.one().filename_template)

    def test_filename_template_is_stored_when_given(self):
        self.assertEqual(self.post(filename_template='  {title} - {date}  ').status_code, 200)
        self.assertEqual(RecordingProfile.query.one().filename_template, '{title} - {date}')

    def test_pre_check_tristate_round_trips(self):
        for sent, expected in ((True, True), (False, False), (None, None)):
            RecordingProfile.query.delete()
            db.session.commit()
            self.assertEqual(self.post(pre_check_enabled=sent).status_code, 200)
            self.assertIs(RecordingProfile.query.one().pre_check_enabled, expected)


class ValidationTests(_Base):
    def test_missing_name_is_rejected(self):
        self.assertEqual(self.post(name='').status_code, 400)
        self.assertEqual(RecordingProfile.query.count(), 0)

    def test_negative_number_is_rejected(self):
        self.assertEqual(self.post(stall_timeout_seconds=-1).status_code, 400)
        self.assertEqual(RecordingProfile.query.count(), 0)

    def test_fractional_number_is_rejected(self):
        self.assertEqual(self.post(retention_days=2.5).status_code, 400)
        self.assertEqual(RecordingProfile.query.count(), 0)

    def test_error_names_the_field_the_user_sees(self):
        resp = self.post(max_consecutive_failures=-3)
        self.assertIn('Max consecutive failures', resp.get_json()['error'])


class MissingProfileTests(_Base):
    def test_edit_of_missing_profile_is_404(self):
        self.assertEqual(self.put(999, name='Nope').status_code, 404)

    def test_delete_of_missing_profile_is_404(self):
        self.assertEqual(self.client.delete('/api/profiles/999').status_code, 404)


class DeleteTests(_Base):
    """Deleting must unlink both kinds of reference - a recording that used it and a
    channel that has it as a default - rather than leaving a dangling profile_id."""

    def test_delete_unlinks_recordings_and_channel_defaults(self):
        profile = self.make_profile()
        account = seed.make_account()
        channel = seed.make_channel(account, stream_id=1, name='Ch')
        channel.default_profile_id = profile.id
        recording = seed.make_recording(status='COMPLETED', channel_id=channel.id,
                                        profile_id=profile.id)
        db.session.commit()
        rec_id, chan_id = recording.id, channel.id

        resp = self.client.delete(f'/api/profiles/{profile.id}')
        self.assertEqual(resp.status_code, 200)
        db.session.expire_all()
        self.assertIsNone(db.session.get(RecordingProfile, profile.id))
        self.assertIsNone(db.session.get(Channel, chan_id).default_profile_id)
        from app.database import Recording
        self.assertIsNone(db.session.get(Recording, rec_id).profile_id)


class ListPageTests(_Base):
    def test_list_page_embeds_prefill_and_defaults(self):
        self.make_profile(name='Sports', pre_padding_minutes=2)
        body = self.client.get('/profiles').get_data(as_text=True)
        self.assertIn('Sports', body)
        self.assertIn('RP_CONFIG', body)
        self.assertIn('profile-modal.js', body)

    def test_zero_retention_renders_as_never_not_as_a_default(self):
        """The list is where the blank-vs-zero distinction becomes visible, so rendering
        0 as if it were unset would hide exactly the setting that matters most."""
        self.make_profile(name='Keep', retention_days=0)
        body = self.client.get('/profiles').get_data(as_text=True)
        self.assertIn('Never', body)

    def test_old_standalone_form_pages_are_gone(self):
        self.assertEqual(self.client.get('/profiles/new').status_code, 404)
        self.assertEqual(self.client.get('/profiles/1/edit').status_code, 404)


if __name__ == '__main__':
    unittest.main()
