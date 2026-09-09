"""Tier 2 - a recording on a channel with no TV Guide presence (dev/changelog/488).

Verification pass, not a defect fix: nothing here was found broken. Nothing previously proved
that a channel with in_guide=False and zero EPGEntry rows works end to end for a scheduled
recording - manual (non-EPG) recordings are an established supported path, but the risk was
display-side, a template or query implicitly assuming an EPGEntry/program dict is present.

Covers the three concrete risk points named in the task: filename-template rendering (the
exact entry=None shape app/routes/guide.py::_program_dict already uses for a no-EPG guide
cell), the recording detail page, and the recordings index/list page. All render through the
real Flask test client and real templates - the same render_template() call and Jinja engine
production uses - so a template hazard here would surface the same way it would in production.

No ffmpeg, no network, no /dvr - these render seeded rows through the test client.
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tests.support.app import make_test_app  # noqa: E402
from tests.support import seed  # noqa: E402
from app import db  # noqa: E402
from app.accounts import render_filename_template  # noqa: E402


class FilenameTemplateNoGuideDataTests(unittest.TestCase):
    """The entry=None shape app/routes/guide.py::_program_dict builds for a channel with no
    current EPG entry - the same dict a manual recording's suggested-name preview renders."""

    def test_renders_without_crashing_or_leftover_placeholders(self):
        program = {
            'start_time': None,
            'stop_time': None,
            'title': 'My Channel',
            'sub_title': '',
            'description': '',
            'channel_name': 'My Channel',
            'category': '',
        }
        name = render_filename_template(
            '{date} - {title} - {channel}', program, tag_cleanup=[], tags_by_name={})
        self.assertNotIn('None', name)
        self.assertNotIn('{date}', name)
        self.assertNotIn('{title}', name)
        self.assertNotIn('{channel}', name)
        self.assertIn('My Channel', name)


class RecordingNoGuideChannelPageTests(unittest.TestCase):
    """A recording against a channel with in_guide=False and no EPGEntry rows, exercised
    through the real detail and index pages."""

    def setUp(self):
        self.t = make_test_app()
        self.ctx = self.t.app.app_context()
        self.ctx.push()
        self.account = seed.make_account()
        self.channel = seed.make_channel(self.account, name='No Guide Channel', in_guide=False)
        # No seed.make_epg_entry() call - this channel has zero EPGEntry rows, and the
        # recording below carries no program_start_time/program_title (a manual recording,
        # the same shape new_recording_json() produces when no source_epg_id is posted).
        self.scheduled = seed.make_recording(
            status='SCHEDULED', name='no_guide_scheduled', channel_id=self.channel.id)
        self.completed = seed.make_recording(
            status='COMPLETED', name='no_guide_completed', channel_id=self.channel.id,
            with_events=True, with_segment=True)
        db.session.commit()

    def tearDown(self):
        self.ctx.pop()
        self.t.cleanup()

    def test_detail_page_renders_for_scheduled_recording(self):
        resp = self.t.client.get(f'/recordings/{self.scheduled.id}')
        self.assertEqual(resp.status_code, 200)
        html = resp.get_data(as_text=True)
        self.assertIn('no_guide_scheduled', html)
        self.assertIn('No Guide Channel', html)

    def test_detail_page_renders_for_completed_recording(self):
        resp = self.t.client.get(f'/recordings/{self.completed.id}')
        self.assertEqual(resp.status_code, 200)
        html = resp.get_data(as_text=True)
        self.assertIn('no_guide_completed', html)
        self.assertIn('No Guide Channel', html)

    def test_index_page_renders_both_recordings(self):
        resp = self.t.client.get('/recordings')
        self.assertEqual(resp.status_code, 200)
        html = resp.get_data(as_text=True)
        self.assertIn('no_guide_scheduled', html)
        self.assertIn('no_guide_completed', html)
        self.assertIn('No Guide Channel', html)


if __name__ == '__main__':
    unittest.main()
