"""Guards dev/docs/BUGS.md 2026-08-11: a health check actively running against a channel
rendered as "Failed" (badge-failed, status text FAILED) on that channel's detail page, and
only corrected itself once the check finished and the row's real terminal status was
written.

Root cause: ChannelTest rows are created with status='FAILED' as a placeholder
(app/channel_tester.py::run_channel_test's _create_test_row_and_commit) and only updated to
COMPLETED/FAILED/CANCELLED at _finalize_test - so the placeholder is indistinguishable from a
real failure by status alone. templates/channels/detail.html rendered latest_test.status
directly with no explicit "still running" branch, in three places on the same page.
"""
import os
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tests.support.app import make_test_app  # noqa: E402
from tests.support import seed  # noqa: E402
from app import db  # noqa: E402


def _running_status(channel_id):
    return {
        'is_running': True, 'stop_requested': False,
        'current_channel_id': channel_id, 'current_channel_name': 'Test Channel',
        'current_channel_url': '', 'current_phase': 'testing',
        'current_test_started_at': None, 'current_test_drop_count': 0,
        'current_test_screenshot_url': None, 'current_live_bytes': 0,
        'current_connect_attempt': 1, 'max_connect_attempts': 1,
        'wait_started_at': None, 'wait_duration_seconds': None,
        'last_channel_name': None, 'last_channel_id': None,
        'next_channel_name': None, 'next_channel_id': None,
        'total_channels': 1, 'completed_channels': 0, 'run_started_at': None,
        'last_skip_reason': None, 'current_job_id': None, 'run_kind': 'job',
        'pre_check_recording_id': None, 'logs': [],
    }


class ChannelDetailInProgressStatusTests(unittest.TestCase):
    def setUp(self):
        self.t = make_test_app()
        self.acc = seed.make_account()
        self.ch = seed.make_channel(self.acc, stream_id=1, name='Test Channel')

    def tearDown(self):
        self.t.cleanup()

    def test_running_test_does_not_render_as_failed(self):
        """The channel's only ChannelTest row is the in-progress placeholder (status=
        FAILED, test_ended_at=None) and the tester module reports it running against this
        exact channel - the page must say so, not render Failed/FAIL."""
        seed.make_channel_test(self.ch)  # status='FAILED', test_ended_at=None (in progress)
        db.session.commit()

        with patch('app.routes.channels.get_status', return_value=_running_status(self.ch.id)):
            resp = self.t.client.get(f'/channels/{self.ch.id}')
        self.assertEqual(resp.status_code, 200)
        body = resp.get_data(as_text=True)
        self.assertNotIn('badge b-fail', body)
        self.assertNotIn('>FAILED<', body)
        self.assertIn('Testing now', body)
        self.assertIn('badge-in_progress', body)

    def test_finished_failed_test_still_renders_as_failed(self):
        """A genuinely terminal FAILED test (tester idle) must still say Failed - the fix
        must not blanket-suppress the real FAILED state."""
        seed.make_channel_test(self.ch)  # status='FAILED', test_ended_at=None
        db.session.commit()

        with patch('app.routes.channels.get_status',
                    return_value={'is_running': False, 'current_channel_id': None}):
            resp = self.t.client.get(f'/channels/{self.ch.id}')
        self.assertEqual(resp.status_code, 200)
        body = resp.get_data(as_text=True)
        self.assertIn('badge b-fail', body)
        self.assertNotIn('Testing now', body)

    def test_running_test_on_a_different_channel_does_not_upgrade_this_one(self):
        """The tester is busy, but on some other channel - this channel's own stale
        FAILED test must not be mislabeled as in-progress."""
        other = seed.make_channel(self.acc, stream_id=2, name='Other Channel')
        seed.make_channel_test(self.ch)
        db.session.commit()

        with patch('app.routes.channels.get_status', return_value=_running_status(other.id)):
            resp = self.t.client.get(f'/channels/{self.ch.id}')
        self.assertEqual(resp.status_code, 200)
        body = resp.get_data(as_text=True)
        self.assertIn('badge b-fail', body)
        self.assertNotIn('Testing now', body)


class GroupDetailTimelineInProgressStatusTests(unittest.TestCase):
    """Same defect, second surface: the health-check-detail page (channels/group_detail.html)
    shares _timeline.html with the channel page. Its Channels table is corrected live by
    group-detail.js, but the Activity Timeline below it is server-rendered only."""

    def setUp(self):
        self.t = make_test_app()
        self.acc = seed.make_account()
        self.ch = seed.make_channel(self.acc, stream_id=1, name='Test Channel')
        self.job = seed.make_test_job(name='Job', channels=[self.ch])

    def tearDown(self):
        self.t.cleanup()

    def test_running_test_in_timeline_does_not_render_as_failed(self):
        seed.make_channel_test(self.ch, job_id=self.job.id)  # in-progress placeholder
        db.session.commit()

        with patch('app.channel_tester.get_status', return_value=_running_status(self.ch.id)):
            resp = self.t.client.get(f'/channel-groups/{self.job.group_id}')
        self.assertEqual(resp.status_code, 200)
        body = resp.get_data(as_text=True)
        self.assertNotIn('>FAILED<', body)
        self.assertIn('badge-in_progress', body)

    def test_finished_failed_test_in_timeline_still_renders_as_failed(self):
        seed.make_channel_test(self.ch, job_id=self.job.id)
        db.session.commit()

        with patch('app.channel_tester.get_status',
                    return_value={'is_running': False, 'current_channel_id': None}):
            resp = self.t.client.get(f'/channel-groups/{self.job.group_id}')
        self.assertEqual(resp.status_code, 200)
        body = resp.get_data(as_text=True)
        self.assertIn('>FAILED<', body)


if __name__ == '__main__':
    unittest.main()
