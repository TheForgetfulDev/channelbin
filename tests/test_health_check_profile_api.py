"""Tier 2 - the Health Check Profile JSON API behind the create/edit modal
(app/routes/health_check_profiles.py, dev/changelog/356).

The standalone `/health-check-profiles/new` and `/…/edit` form pages were replaced by a
modal posting JSON, which moved parsing off Flask's form dict (everything a string,
absent == '') and onto a JSON body (real ints, real bools, real nulls). That is where the
interesting failure lives, and it is the reason this file exists:

**Unset is not zero.** A blank numeric field means "inherit the global default" and
travels as null; 0 is a real value the user picked, and it means something entirely
different - "test each channel for 0 seconds" vs. "test for the configured 120". A parse
that collapses one into the other rewrites a profile's meaning without saying so. The
same trap sits on the tri-state boolean, where screenshots_enabled=False is a profile
value and None is inheritance, and any truthiness test conflates them.
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tests.support.app import make_test_app  # noqa: E402
from app import db  # noqa: E402
from app.database import HealthCheckProfile, OnDemandTestJob  # noqa: E402


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
        profile = HealthCheckProfile(**kwargs)
        db.session.add(profile)
        db.session.commit()
        return profile

    def post(self, **body):
        body.setdefault('name', 'New profile')
        return self.client.post('/api/health-check-profiles', json=body)

    def put(self, profile_id, **body):
        body.setdefault('name', 'New profile')
        return self.client.put(f'/api/health-check-profiles/{profile_id}', json=body)


class UnsetIsNotZeroTests(_Base):
    """The headline invariant. Blank/null must persist as NULL (inherit) and 0 must
    persist as 0 (an explicit override) - never each other."""

    def test_blank_numeric_field_stores_null_not_zero(self):
        resp = self.post(test_duration_seconds=None, connect_retries='')
        self.assertEqual(resp.status_code, 200)
        profile = HealthCheckProfile.query.one()
        self.assertIsNone(profile.test_duration_seconds)
        self.assertIsNone(profile.connect_retries)

    def test_explicit_zero_stores_zero_not_null(self):
        resp = self.post(test_duration_seconds=0, connect_retries=0)
        self.assertEqual(resp.status_code, 200)
        profile = HealthCheckProfile.query.one()
        self.assertEqual(profile.test_duration_seconds, 0)
        self.assertEqual(profile.connect_retries, 0)

    def test_screenshots_false_is_a_value_not_inheritance(self):
        resp = self.post(screenshots_enabled=False)
        self.assertEqual(resp.status_code, 200)
        self.assertIs(HealthCheckProfile.query.one().screenshots_enabled, False)

    def test_screenshots_null_is_inheritance(self):
        resp = self.post(screenshots_enabled=None)
        self.assertEqual(resp.status_code, 200)
        self.assertIsNone(HealthCheckProfile.query.one().screenshots_enabled)

    def test_edit_can_clear_a_value_back_to_inherited(self):
        profile = self.make_profile(test_duration_seconds=45, screenshots_enabled=False)
        resp = self.put(profile.id, name='Existing', test_duration_seconds=None,
                        screenshots_enabled=None)
        self.assertEqual(resp.status_code, 200)
        db.session.expire_all()
        refreshed = db.session.get(HealthCheckProfile, profile.id)
        self.assertIsNone(refreshed.test_duration_seconds)
        self.assertIsNone(refreshed.screenshots_enabled)


class ValidationTests(_Base):
    """The modal runs the same checks, but the API is the enforcement point - a payload
    that never went through the modal must be rejected here."""

    def test_missing_name_is_rejected(self):
        resp = self.post(name='')
        self.assertEqual(resp.status_code, 400)
        self.assertIn('error', resp.get_json())
        self.assertEqual(HealthCheckProfile.query.count(), 0)

    def test_whitespace_only_name_is_rejected(self):
        self.assertEqual(self.post(name='   ').status_code, 400)
        self.assertEqual(HealthCheckProfile.query.count(), 0)

    def test_negative_number_is_rejected(self):
        resp = self.post(test_duration_seconds=-1)
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(HealthCheckProfile.query.count(), 0)

    def test_fractional_number_is_rejected(self):
        resp = self.post(test_duration_seconds=1.5)
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(HealthCheckProfile.query.count(), 0)

    def test_boolean_is_not_accepted_as_a_number(self):
        """bool subclasses int in Python, so an unguarded int() would store True as 1."""
        resp = self.post(connect_retries=True)
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(HealthCheckProfile.query.count(), 0)

    def test_error_names_the_field_the_user_sees(self):
        resp = self.post(connect_timeout_seconds=-5)
        self.assertIn('Connect timeout', resp.get_json()['error'])

    def test_name_is_trimmed(self):
        self.assertEqual(self.post(name='  Padded  ').status_code, 200)
        self.assertEqual(HealthCheckProfile.query.one().name, 'Padded')


class MissingProfileTests(_Base):
    def test_edit_of_missing_profile_is_404(self):
        self.assertEqual(self.put(999, name='Nope').status_code, 404)

    def test_delete_of_missing_profile_is_404(self):
        self.assertEqual(self.client.delete('/api/health-check-profiles/999').status_code, 404)


class DeleteTests(_Base):
    """Deleting a profile must unlink the health checks pointing at it rather than
    orphaning a dangling profile_id - they fall back to the global defaults."""

    def test_delete_removes_the_row_and_unlinks_its_checks(self):
        profile = self.make_profile()
        job = OnDemandTestJob(name='Nightly', status='SCHEDULED', profile_id=profile.id)
        db.session.add(job)
        db.session.commit()
        job_id = job.id

        resp = self.client.delete(f'/api/health-check-profiles/{profile.id}')
        self.assertEqual(resp.status_code, 200)
        db.session.expire_all()
        self.assertIsNone(db.session.get(HealthCheckProfile, profile.id))
        self.assertIsNone(db.session.get(OnDemandTestJob, job_id).profile_id)


class ListPageTests(_Base):
    """The list page hands the modal its prefill data and the resolved defaults, so both
    have to actually be in the page - a modal that opens with an empty form is the
    failure this catches."""

    def test_list_page_embeds_profile_prefill_and_defaults(self):
        self.make_profile(name='Deep Scan', test_duration_seconds=300)
        body = self.client.get('/health-check-profiles').get_data(as_text=True)
        self.assertIn('Deep Scan', body)
        self.assertIn('HCP_CONFIG', body)
        self.assertIn('profile-modal.js', body)

    def test_old_standalone_form_pages_are_gone(self):
        """They were replaced by the modal; leaving them reachable would mean two code
        paths for one form, which is how the two diverge."""
        self.assertEqual(self.client.get('/health-check-profiles/new').status_code, 404)
        self.assertEqual(self.client.get('/health-check-profiles/1/edit').status_code, 404)


if __name__ == '__main__':
    unittest.main()
