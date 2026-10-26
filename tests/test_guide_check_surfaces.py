"""The automatic "TV Guide health check", as the rest of the app describes it
(dev/changelog/1271).

The first-time-user test (finding 16, 18 and half of 15) found the group ChannelBin makes for
itself invisible or miscounted everywhere outside Channel Groups:

  * Add to Guide also enrolled the channel in that check, and nothing said so.
  * The channel page then said "Never health-tested - its score comes from 2 recording
    observations" after one recording, with no word about the check it had joined.
  * Readiness said nothing was set up to survive a feed dropping, minutes after a recording
    reconnected through one, and its empty rows gave no way to set anything up.
  * The account page counted "In a group 0" for a channel that group held, without saying
    the automatic group is not counted.

Runs against a throwaway temp SQLite DB - never the live dvr.db.
  python3 -m unittest tests.test_guide_check_surfaces
"""
import os
import sys
import unittest
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tests.support.app import make_test_app  # noqa: E402
from tests.support import seed  # noqa: E402
from app import db, readiness  # noqa: E402
from app.database import OnDemandTestJob, REC_STATUS_COMPLETED  # noqa: E402


def _system_job():
    return OnDemandTestJob.query.filter_by(is_system=True).first()


class _Case(unittest.TestCase):
    def setUp(self):
        self.t = make_test_app(csrf=False)
        self.acc = seed.make_account()
        self.ch = seed.make_channel(self.acc, stream_id=1, name='Northwind News')
        db.session.commit()

    def tearDown(self):
        self.t.cleanup()


class GuideCheckNoteTests(_Case):
    """The one sentence every Add to Guide path carries."""

    def test_a_scheduled_check_names_itself_and_when_it_runs(self):
        from app.routes.channel_tests import guide_check_note
        note = guide_check_note(_system_job(), {})
        self.assertIn('"TV Guide health check"', note)
        self.assertIn('every day', note)

    def test_a_check_with_no_schedule_says_nothing_tests_on_its_own(self):
        from app.routes.channel_tests import guide_check_note
        job = _system_job()
        job.recur_paused = True
        db.session.commit()
        note = guide_check_note(job, {})
        self.assertIn('no schedule', note)
        self.assertNotIn('every day', note)

    def test_the_page_flash_says_the_channel_is_now_tested(self):
        resp = self.t.client.post(f'/channels/{self.ch.id}/toggle', follow_redirects=True,
                                  data={'next': f'/channels/{self.ch.id}'})
        body = resp.get_data(as_text=True)
        self.assertIn('added to guide.', body)
        self.assertIn('TV Guide health check', body.split('added to guide.', 1)[1][:400])

    def test_the_api_returns_the_note_for_the_search_toast(self):
        data = self.t.client.post(f'/api/guide/channels/{self.ch.id}/add').get_json()
        self.assertTrue(data['success'])
        self.assertIn('TV Guide health check', data['note'])

    def test_no_note_for_a_channel_whose_health_checks_are_off(self):
        """The check skips such a channel, so saying it will test it would be false."""
        self.ch.test_enabled = False  # participation-write-ok: test fixture
        db.session.commit()
        data = self.t.client.post(f'/api/guide/channels/{self.ch.id}/add').get_json()
        self.assertIsNone(data['note'])

    def test_the_add_button_tooltip_says_it_too(self):
        body = self.t.client.get(f'/channels/{self.ch.id}').get_data(as_text=True)
        tip = body.split('Makes this channel a TV Guide row', 1)[1].split('">', 1)[0]
        self.assertIn('TV Guide health check', tip)


class ScoredFromRecordingsTests(_Case):
    """One recording leaves two observations; the page must not read that as two."""

    def _scored_once(self):
        rec = seed.make_recording(status=REC_STATUS_COMPLETED, channel_id=self.ch.id)
        rec.completed_at = datetime.utcnow() - timedelta(minutes=5)
        rec.health_quality_score = 90
        rec.capture_quality_breakdown = '{"final": 90}'
        rec.analysis_completed_at = datetime.utcnow()
        self.ch.health_score = 90
        self.ch.health_score_sample_count = 2
        self.ch.in_guide = True
        db.session.commit()
        return rec

    def test_the_status_bar_counts_recordings_not_observations(self):
        self._scored_once()
        body = self.t.client.get(f'/channels/{self.ch.id}').get_data(as_text=True)
        self.assertIn('Its score comes from 2 observations made during 1 recording.', body)
        self.assertNotIn('2 recording observation', body)

    def test_the_status_bar_says_which_check_will_test_it(self):
        self._scored_once()
        body = self.t.client.get(f'/channels/{self.ch.id}').get_data(as_text=True)
        self.assertIn('"TV Guide health check" will test it every day', body)

    def test_a_channel_in_no_check_says_so(self):
        body = self.t.client.get(f'/channels/{self.ch.id}').get_data(as_text=True)
        self.assertIn('No health check includes it', body)


class ReadinessGroupRowsTests(_Case):
    """Finding 18: the failover capability's empty state."""

    def setUp(self):
        super().setUp()
        self.ch.in_guide = True
        db.session.commit()
        self.payload = readiness.evaluate()
        self.rows = {r['id']: r for r in self.payload['checks']}

    def test_the_capability_is_about_a_backup_channel_not_reconnecting(self):
        cap = next(c for c in self.payload['capabilities'] if c['id'] == 'failover')
        self.assertIn('backup channel', cap['label'])
        self.assertNotIn('Survive', cap['label'])

    def test_the_automatic_group_is_not_called_a_guide_group(self):
        row = self.rows['guide_groups']
        self.assertEqual(row['status'], readiness.NOTHING)
        self.assertIn('No channel group of yours', row['found'])

    def test_an_empty_group_row_says_how_to_set_it_up_and_links_there(self):
        row = self.rows['guide_groups']
        self.assertIn('reconnects', row['setup'])
        self.assertIn('TV Guide health check', row['setup'])
        self.assertEqual(row['link']['label'], 'Channel Groups')

    def test_the_report_carries_the_set_up_line(self):
        self.assertIn('set up:  A recording already reconnects', self.payload['report'])

    def test_a_row_with_no_setup_says_none(self):
        self.assertIsNone(self.rows['ffmpeg']['setup'])

    def test_the_tested_row_names_groups_not_members(self):
        row = self.rows['channels_tested']
        self.assertNotIn('member', row['found'])
        self.assertIn('group', row['found'])


class AccountGroupCountTests(_Case):
    """Half of finding 15: the counts say which groups they count."""

    def test_the_labels_and_tips_exclude_the_automatic_group_out_loud(self):
        self.ch.in_guide = True
        db.session.commit()
        body = self.t.client.get(f'/accounts/{self.acc.id}').get_data(as_text=True)
        self.assertIn('In your groups', body)
        self.assertIn('Recording in groups', body)
        self.assertIn('The TV Guide health check, which ChannelBin keeps by itself, is not', body)
        self.assertNotIn('>In a channel group<', body)


if __name__ == '__main__':
    unittest.main()
