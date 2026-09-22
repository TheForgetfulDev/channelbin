"""Tier 2 - what the unified group / health-check page's Activity Timeline is scoped to.

`dev/docs/BUGS.md` 2026-08-22 09:12 - the timeline's health-test entries were pulled by
"every test ever run on whoever is a member of this group right now", with no job scope
at all. That is the wrong question in both directions at once, and both directions are
visible to the user:

  * another group's check on a shared feed, and a one-off "Test now", were listed on this
    page as if this check had run them - which is principle 1 inverted, the page
    presenting work it never did as its own;
  * a channel that WAS in a run and has since been removed from the group vanished from
    its own history, taking the evidence of that run with it.

The fix makes a test entry a fact about the group's own work: the tests its attached
checks ran, plus the pre-recording checks run for its own recordings. Entering the page
by check URL narrows that half to the one pinned check, which is the only thing
`/channels/health-checks/<id>` has ever meant that `/channel-groups/<id>` does not.
"""
import os
import sys
import unittest
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tests.support.app import make_test_app  # noqa: E402
from tests.support.seed import (make_account, make_channel, make_group,  # noqa: E402
                                make_channel_test, make_recording, set_check,
)
from app import db  # noqa: E402
from app.database import ChannelGroupMember  # noqa: E402


class GroupTimelineScopeTests(unittest.TestCase):
    def setUp(self):
        self.t = make_test_app()
        self.t.app.config['WTF_CSRF_ENABLED'] = False
        self.client = self.t.client
        self.ctx = self.t.app.app_context()
        self.ctx.push()

        acct = make_account()
        self.feed_a = make_channel(acct, name='Feed A', stream_id=101)
        self.feed_b = make_channel(acct, name='Feed B', stream_id=102)
        self.departed = make_channel(acct, name='Departed Feed', stream_id=103)
        self.oneoff = make_channel(acct, name='One Off Feed', stream_id=104)

        self.grp = make_group(name='G', in_guide=False,
                              members=[self.feed_a, self.feed_b, self.departed, self.oneoff])
        # The group's one check (dev/changelog/1077) - every group carries exactly one.
        self.nightly = self._job('Nightly check', self.grp)

        # A second group sharing one feed, with a check of its own.
        self.other = make_group(name='Other', in_guide=False, members=[self.feed_a])
        self.foreign = self._job('Other group check', self.other)

        self._at = datetime.utcnow() - timedelta(hours=6)
        self._test(self.feed_a, job_id=self.nightly.id)
        self._test(self.feed_b, job_id=self.nightly.id)
        self._test(self.departed, job_id=self.nightly.id)
        self._test(self.feed_a, job_id=self.foreign.id)
        # A one-off "Test now": no job, no recording to protect.
        self._test(self.oneoff)
        # A pre-recording check, which carries the recording it protected rather than a
        # job id (DESIGN-prerecord-checks.md 3).
        self.rec = make_recording(status='COMPLETED', name='G rec', group_id=self.grp.id,
                                  channel_id=self.feed_b.id)
        self._test(self.feed_b, pre_check_recording_id=self.rec.id)

        # The departed channel leaves AFTER its run - the run still happened.
        ChannelGroupMember.query.filter_by(group_id=self.grp.id,
                                           channel_id=self.departed.id).delete()
        db.session.commit()

    def tearDown(self):
        self.ctx.pop()
        self.t.cleanup()

    def _test(self, channel, **kw):
        """A COMPLETED ChannelTest, each one a minute after the last so the timeline has
        a deterministic order. make_channel_test stamps its own start time, so this
        moves it rather than passing one in."""
        ct = make_channel_test(channel, status='COMPLETED', **kw)
        self._at += timedelta(minutes=1)
        ct.test_started_at = self._at
        db.session.flush()
        return ct

    def _job(self, name, group):
        return set_check(group, name=name, status='COMPLETED')

    def _timeline(self, path):
        """Just the Activity Timeline block - a name that appears anywhere else on the
        page (a member row, a tooltip, the checks section) would not prove the timeline
        lists it."""
        html = self.client.get(path).get_data(as_text=True)
        start = html.find('ch-timeline-log')
        self.assertNotEqual(start, -1, f'{path} rendered no timeline at all')
        # Bounded at the padding div that closes the last card: the modals and the
        # Channels table's JSON blob below it name every member, so an unbounded slice
        # would report a channel as "in the timeline" purely for being on the page.
        end = html.find('gm-botpad', start)
        self.assertNotEqual(end, -1, f'{path} rendered no end-of-sections marker')
        return html[start:end]

    def _group_page(self):
        return self._timeline(f'/channel-groups/{self.grp.id}')

    # ── Entered by group ────────────────────────────────────────────────────
    def test_group_page_lists_the_checks_own_runs(self):
        html = self._group_page()
        self.assertIn('Nightly check', html)

    def test_another_groups_check_on_a_shared_feed_is_not_this_groups_work(self):
        html = self._group_page()
        self.assertNotIn('Other group check', html)
        # Feed A was tested twice - once by this group's nightly check, once by the other
        # group's. Its name alone cannot separate them, so the count is the assertion:
        # one entry sources Feed A, not two.
        self.assertEqual(html.count(f'"/channels/{self.feed_a.id}"'), 1, html)

    def test_a_one_off_test_on_a_member_is_not_the_groups_work(self):
        self.assertNotIn('One Off Feed', self._group_page())

    def test_a_departed_members_run_survives_its_membership(self):
        self.assertIn('Departed Feed', self._group_page())

    def test_a_pre_recording_check_for_the_groups_own_recording_is_listed(self):
        self.assertIn('Pre-recording check', self._group_page())

    def test_every_test_entry_names_the_run_that_produced_it(self):
        html = self._group_page()
        # One label per test entry, so a reader can tell a check's result from a
        # pre-check's without inferring it from the numbers.
        self.assertEqual(html.count('Health Test'), 4, html.count('Health Test'))
        for label in ('Nightly check', 'Pre-recording check'):
            self.assertIn(f'· {label}', html)

    # ── Entered by check ────────────────────────────────────────────────────
    def test_the_check_url_is_the_groups_page(self):
        """dev/changelog/1077: a group carries exactly one check, so there is nothing
        for a check-shaped URL to pin. It redirects to the group."""
        resp = self.client.get(f'/channels/health-checks/{self.nightly.id}')
        self.assertEqual(302, resp.status_code)
        self.assertTrue(resp.headers['Location'].endswith(f'/channel-groups/{self.grp.id}'))

    # ── The scope is stated, not inferred ───────────────────────────────────
    def test_the_page_says_what_the_timeline_is_scoped_to(self):
        grp_html = self.client.get(
            f'/channel-groups/{self.grp.id}').get_data(as_text=True)
        self.assertIn('check and pre-recording checks ran', grp_html)


class GroupTimelineWithoutChecksTests(unittest.TestCase):
    """A group whose own check has never run lists no health tests - the members' own
    latest results are what the Channels table above is for. Before the fix this page
    listed whatever any other check had run on its members."""

    def setUp(self):
        self.t = make_test_app()
        self.client = self.t.client
        self.ctx = self.t.app.app_context()
        self.ctx.push()
        acct = make_account()
        self.ch = make_channel(acct, name='Shared Feed', stream_id=201)
        self.grp = make_group(name='No check', in_guide=False, members=[self.ch])
        self.other = make_group(name='Tester', in_guide=False, members=[self.ch])
        job = set_check(self.other, name='Somebody elses check', status='COMPLETED')
        make_channel_test(self.ch, job_id=job.id, status='COMPLETED')
        # Something of its own, so the timeline block exists either way and an empty
        # assertion cannot pass by the page simply having no timeline.
        make_recording(status='COMPLETED', name='own rec', group_id=self.grp.id,
                       channel_id=self.ch.id)
        db.session.commit()

    def tearDown(self):
        self.ctx.pop()
        self.t.cleanup()

    def test_no_health_tests_are_claimed(self):
        html = self.client.get(f'/channel-groups/{self.grp.id}').get_data(as_text=True)
        start = html.find('ch-timeline-log')
        self.assertNotEqual(start, -1, 'the group has a recording, so it has a timeline')
        timeline = html[start:html.find('gm-botpad', start)]
        self.assertIn('own rec', timeline)
        self.assertNotIn('Health Test', timeline)


if __name__ == '__main__':
    unittest.main()
