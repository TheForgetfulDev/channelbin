"""A group recording shows a chip for the member it is recording from, beside the group's.

dev/changelog/1252: a running group recording's row and page used to carry only the group's
chip, so nothing on screen named the member actually capturing or led to its channel page.
The member chip is built from rec.channel - the serving member as stamped now, re-stamped by
every failover - so it follows the recording rather than the member it was scheduled on. The
group chip gives up the member's logo and account dot to it, so a row never shows two chips
wearing one channel's identity, and a group recording with no stamped member shows the group
chip alone.
"""
import os
import re
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tests.support.app import make_test_app  # noqa: E402
from tests.support import seed  # noqa: E402
from app import db  # noqa: E402
from app.database import REC_STATUS_IN_PROGRESS, REC_STATUS_COMPLETED  # noqa: E402

PILL = re.compile(r'<a class="ch-pill[^"]*"[^>]*>.*?</a>', re.S)


def _row(html, rec_id):
    rows = re.findall(r'<div class="row [^"]*" data-href="/recordings/%d"(.*?)<div class="c-time"'
                      % rec_id, html, re.S)
    assert len(rows) == 1, rows
    return rows[0]


class GroupMemberChipTests(unittest.TestCase):
    def setUp(self):
        self.t = make_test_app()
        self.ctx = self.t.app.app_context()
        self.ctx.push()
        self.account = seed.make_account(name='Acct A', color='#123456')
        self.member = seed.make_channel(
            self.account, name='Serving Member',
            logo_url='http://example.test/logos/serving-member.png')
        self.other = seed.make_channel(self.account, name='Other Member')
        self.group = seed.make_group(name='My Group', members=[self.member, self.other])
        self.rec = seed.make_recording(
            status=REC_STATUS_IN_PROGRESS, name='group_rec_live',
            channel_id=self.member.id, group_id=self.group.id, with_segment=True)
        db.session.commit()

    def tearDown(self):
        self.ctx.pop()
        self.t.cleanup()

    def _chips(self, html):
        return PILL.findall(html)

    def test_the_row_carries_the_group_chip_and_then_the_member_chip(self):
        html = self.t.client.get('/recordings').get_data(as_text=True)
        chips = [c for c in self._chips(_row(html, self.rec.id)) if 'acct-pill' not in c]
        self.assertEqual(len(chips), 2, chips)
        group_chip, member_chip = chips
        self.assertIn(f'/channel-groups/{self.group.id}', group_chip)
        self.assertRegex(group_chip, r'<span class="ch-logo"[^>]*>MYG</span>')
        self.assertNotIn('<img', group_chip, 'the group chip does not wear the member logo')
        self.assertNotIn('acct-dot', group_chip, 'a group has no account')
        self.assertIn(f'/channels/{self.member.id}', member_chip)
        self.assertIn('Serving Member', member_chip)
        self.assertIn('http://example.test/logos/serving-member.png', member_chip)
        self.assertIn('acct-dot', member_chip)
        self.assertIn('capturing from right now', member_chip)

    def test_the_row_searches_by_member_name(self):
        html = self.t.client.get('/recordings').get_data(as_text=True)
        self.assertIn('data-member="Serving Member"', _row(html, self.rec.id))

    def test_the_detail_header_carries_the_member_chip(self):
        html = self.t.client.get(f'/recordings/{self.rec.id}').get_data(as_text=True)
        head = re.search(r'<div class="dh-meta">(.*?)<div class="dh-actions">', html, re.S).group(1)
        chips = [c for c in self._chips(head) if 'acct-pill' not in c]
        self.assertEqual(len(chips), 2, chips)
        self.assertIn(f'/channels/{self.member.id}', chips[1])
        self.assertIn('capturing from right now', chips[1])

    def test_the_chip_follows_the_stamped_member(self):
        self.rec.channel_id = self.other.id
        db.session.commit()
        html = self.t.client.get('/recordings').get_data(as_text=True)
        row = _row(html, self.rec.id)
        self.assertIn(f'/channels/{self.other.id}', row)
        self.assertNotIn(f'/channels/{self.member.id}', row)

    def test_a_finished_recording_says_which_member_it_ended_on(self):
        self.rec.status = REC_STATUS_COMPLETED
        db.session.commit()
        html = self.t.client.get('/recordings').get_data(as_text=True)
        self.assertIn('this recording ended on', _row(html, self.rec.id))

    def test_a_single_channel_recording_has_no_member_chip(self):
        single = seed.make_recording(status=REC_STATUS_COMPLETED, name='single_rec',
                                     channel_id=self.member.id, with_segment=True)
        db.session.commit()
        html = self.t.client.get('/recordings').get_data(as_text=True)
        chips = [c for c in self._chips(_row(html, single.id)) if 'acct-pill' not in c]
        self.assertEqual(len(chips), 1, chips)
        self.assertIn(f'/channels/{self.member.id}', chips[0])
        self.assertIn('<img', chips[0], 'the one chip keeps the logo')

    def test_a_group_recording_with_no_member_shows_the_group_chip_alone(self):
        orphan = seed.make_recording(status=REC_STATUS_COMPLETED, name='orphan_rec',
                                     channel_id=None, group_id=self.group.id, with_segment=True)
        db.session.commit()
        html = self.t.client.get('/recordings').get_data(as_text=True)
        chips = [c for c in self._chips(_row(html, orphan.id)) if 'acct-pill' not in c]
        self.assertEqual(len(chips), 1, chips)
        self.assertIn(f'/channel-groups/{self.group.id}', chips[0])


if __name__ == '__main__':
    unittest.main()
