"""Tier 2 - recording usage stats exclude never-started recordings.

Guards the BUGS.md 2026-07-18 entry: a recording canceled before it ever started
(status ABORTED, started_at IS NULL) - or one still merely SCHEDULED - must NOT
count toward the per-profile "N recordings" usage stat on /profiles. Only
recordings that actually started (started_at IS NOT NULL) count.
"""
import os
import sys
import unittest
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tests.support.app import make_test_app  # noqa: E402
from tests.support import seed  # noqa: E402
from app import db  # noqa: E402
from app.database import RecordingProfile  # noqa: E402


class ProfileRecordingCountTests(unittest.TestCase):
    def setUp(self):
        self.t = make_test_app()
        self.acc = seed.make_account()
        self.ch = seed.make_channel(self.acc, stream_id=1, name='Ch')
        self.profile = RecordingProfile(name='Prof')
        db.session.add(self.profile)
        db.session.commit()

    def tearDown(self):
        self.t.cleanup()

    def test_only_started_recordings_count_toward_profile(self):
        now = datetime.utcnow()
        # Actually started (and completed) - counts.
        seed.make_recording(status='COMPLETED', channel_id=self.ch.id,
                            profile_id=self.profile.id, started_at=now - timedelta(hours=2))
        # Canceled before it ever started - must NOT count.
        seed.make_recording(status='ABORTED', channel_id=self.ch.id,
                            profile_id=self.profile.id, started_at=None)
        # Still scheduled, never ran - must NOT count.
        seed.make_recording(status='SCHEDULED', channel_id=self.ch.id,
                            profile_id=self.profile.id, started_at=None)
        db.session.commit()

        resp = self.t.client.get('/profiles')
        self.assertEqual(resp.status_code, 200)
        body = resp.get_data(as_text=True)
        # The label reads "1 recording · N channels" - never "3 recordings".
        self.assertIn('1 recording ·', body)
        self.assertNotIn('3 recording ·', body)


if __name__ == '__main__':
    unittest.main()
