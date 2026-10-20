"""A badge for a raw status takes its `.b-*` class from the shared status tables, never from
the status glued onto a `badge-` prefix (dev/changelog/1212).

The legacy family colored a status by its stored name, so every new Recording.status needed
a new rule in style.css (`.badge-analyzing` was added exactly that way), and the same status
read in different colors on the Recordings list (`.b-*`) and the Dashboard or channel page
(`.badge-*`). A health check's badge keyed on the stored COMPLETED, so a test that played
badly (WARN) wore PASS's color and the word COMPLETED, while the group page said Warn.
"""
import html
import json
import os
import re
import sys
import unittest
from datetime import timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tests.support.app import make_test_app  # noqa: E402
from tests.support import seed  # noqa: E402
from app import db  # noqa: E402
from app.database import (  # noqa: E402
    REC_STATUS_ABORTED, REC_STATUS_ANALYZING, REC_STATUS_CONCATENATING,
    REC_STATUS_CONVERTING, REC_STATUS_IN_PROGRESS, REC_STATUS_PAUSED, REC_STATUS_RETRYING,
    REC_STATUS_SCHEDULED, TEST_STATUS_CANCELLED, TEST_STATUS_COMPLETED, TEST_STATUS_FAILED,
)
from app.fmt_utils import REC_STATUS_BADGES, REC_STATUS_DISPLAY, rec_status_display  # noqa: E402


class _PageCase(unittest.TestCase):
    def setUp(self):
        self.t = make_test_app()
        self.acc = seed.make_account()
        self.ch = seed.make_channel(self.acc, stream_id=1, name='Badge Channel')

    def tearDown(self):
        self.t.cleanup()

    def get(self, url):
        db.session.commit()
        db.session.remove()
        resp = self.t.client.get(url)
        self.assertEqual(resp.status_code, 200)
        return resp.get_data(as_text=True)


class RecordingStatusVocabTests(_PageCase):

    def test_every_status_has_a_badge_class_in_the_browser_vocab(self):
        """util.js::recStatusClass reads `badges` off base.html's meta tag."""
        body = self.get('/')
        m = re.search(r'<meta name="rec-status-labels" content="([^"]*)"', body)
        vocab = json.loads(html.unescape(m.group(1)))
        self.assertEqual(vocab['badges'], REC_STATUS_BADGES)
        self.assertEqual(set(vocab['badges']), set(REC_STATUS_DISPLAY))

    def test_the_dashboard_badges_each_live_status_from_the_shared_table(self):
        now_ids = {}
        for status in (REC_STATUS_IN_PROGRESS, REC_STATUS_PAUSED, REC_STATUS_RETRYING,
                       REC_STATUS_CONCATENATING, REC_STATUS_ANALYZING, REC_STATUS_CONVERTING,
                       REC_STATUS_SCHEDULED):
            rec = seed.make_recording(status=status, channel_id=self.ch.id)
            if status == REC_STATUS_SCHEDULED:
                rec.start_time = rec.scheduled_start_time = rec.start_time + timedelta(hours=2)
                rec.stop_time = rec.scheduled_stop_time = rec.stop_time + timedelta(hours=2)
            now_ids[rec.id] = status
        body = self.get('/')
        for rid, status in now_ids.items():
            m = re.search(rf'<span class="badge ([\w-]+)" id="badge-{rid}">', body)
            self.assertIsNotNone(m, f'no badge rendered for {status}')
            self.assertEqual(m.group(1), rec_status_display(status)[2], status)

    def test_the_channel_page_badges_a_recording_by_label_and_shared_class(self):
        """The observations table and the timeline both; the timeline used to print the
        stored ABORTED where every other surface says CANCELLED."""
        seed.make_recording(status=REC_STATUS_ABORTED, channel_id=self.ch.id,
                            health_quality_score=40)
        body = self.get(f'/channels/{self.ch.id}')
        self.assertGreaterEqual(body.count('<span class="badge b-abort">CANCELLED</span>'), 2)
        self.assertNotIn('>ABORTED<', body)


class HealthCheckBadgeTests(_PageCase):

    def _badges(self, body):
        return re.findall(r'<span class="badge (b-[\w-]+)[^"]*"[^>]*>(\w+)</span>', body)

    def test_a_clean_pass_is_done_and_says_pass(self):
        seed.make_channel_test(self.ch, status=TEST_STATUS_COMPLETED)
        self.assertIn(('b-done', 'PASS'), self._badges(self.get(f'/channels/{self.ch.id}')))

    def test_a_completed_test_with_a_problem_is_warn_not_pass(self):
        seed.make_channel_test(self.ch, status=TEST_STATUS_COMPLETED,
                               error_detail='slow connect')
        body = self.get(f'/channels/{self.ch.id}')
        self.assertIn(('b-warn', 'WARN'), self._badges(body))
        self.assertIn('data-tip="slow connect"', body)
        self.assertNotIn('>COMPLETED<', body)

    def test_failed_and_cancelled_tests_use_the_group_page_vocabulary(self):
        seed.make_channel_test(self.ch, status=TEST_STATUS_FAILED)
        seed.make_channel_test(self.ch, status=TEST_STATUS_CANCELLED)
        badges = self._badges(self.get(f'/channels/{self.ch.id}'))
        self.assertIn(('b-fail', 'FAIL'), badges)
        self.assertIn(('b-abort', 'CANCELLED'), badges)


if __name__ == '__main__':
    unittest.main()
