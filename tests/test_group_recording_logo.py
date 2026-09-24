"""A channel-group recording shows its serving member's logo on /recordings.

Guards dev/docs/BUGS.md 2026-08-22 - the group branch of _index_row's pill builder
hardcoded 'logo_url': None, so every group recording rendered with plain initials
even though rec.channel (the recording's stamped serving member - set at creation
by new_recording_json and re-stamped at record start/failover) was already
available and already used for the pill's acct_color on the very same line.
"""
import os
import re
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tests.support.app import make_test_app  # noqa: E402
from tests.support import seed  # noqa: E402
from app import db  # noqa: E402


class GroupRecordingLogoTests(unittest.TestCase):
    def setUp(self):
        self.t = make_test_app()
        self.ctx = self.t.app.app_context()
        self.ctx.push()
        self.account = seed.make_account()
        self.channel = seed.make_channel(
            self.account, name='Serving Member',
            logo_url='http://example.test/logos/serving-member.png')
        self.group = seed.make_group(name='My Group', members=[self.channel])
        # channel_id set alongside group_id, mirroring what new_recording_json /
        # recorder.py always stamp for a real group recording.
        self.rec = seed.make_recording(
            status='COMPLETED', name='group_rec_with_logo',
            channel_id=self.channel.id, group_id=self.group.id, with_segment=True)
        db.session.commit()

    def tearDown(self):
        self.ctx.pop()
        self.t.cleanup()

    def test_group_recording_shows_serving_members_logo(self):
        resp = self.t.client.get('/recordings')
        self.assertEqual(resp.status_code, 200)
        html = resp.get_data(as_text=True)
        self.assertIn('group_rec_with_logo', html)
        self.assertIn('http://example.test/logos/serving-member.png', html)

    def test_group_recording_with_no_stamped_channel_falls_back_to_initials(self):
        # Defensive path: a group recording somehow missing its stamped channel_id
        # must not crash the page, and must not fabricate a logo.
        orphan = seed.make_recording(
            status='COMPLETED', name='group_rec_no_channel',
            channel_id=None, group_id=self.group.id, with_segment=True)
        db.session.commit()
        resp = self.t.client.get('/recordings')
        self.assertEqual(resp.status_code, 200)
        html = resp.get_data(as_text=True)
        # The orphan's own row, found by its exact link rather than its id, which as a short
        # number is somewhere on any page (dev/changelog/1097).
        rows = re.findall(r'<div class="row [^"]*" data-href="/recordings/%d"(.*?)<div class="c-time"'
                          % orphan.id, html, re.S)
        self.assertEqual(len(rows), 1)
        self.assertIn('<div class="name">group_rec_no_channel</div>', rows[0])
        pills = re.findall(r'<a class="ch-pill"[^>]*>(.*?)</a>', rows[0], re.S)
        self.assertEqual(len(pills), 1)
        self.assertRegex(pills[0], r'<span class="ch-logo"[^>]*>MYG</span>')
        self.assertNotIn('<img', pills[0])
        self.assertNotIn('logo-fallback', rows[0])


if __name__ == '__main__':
    unittest.main()
