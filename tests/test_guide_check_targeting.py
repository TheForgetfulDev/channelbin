"""Tier 2 - what the automatic "TV Guide Channels" health check actually tests.

It used to resolve as `Channel.query.filter_by(in_guide=True)`. Once dev/changelog/751
made that column mean only "this channel is its own guide row", that query stopped
describing the guide at all: it tested channels that carry a row and no group member,
however many groups the guide was painting. dev/changelog/752 retargets it to **one probe
per guide row, plus one per group with no schedule of its own** - the shape
`dev/docs/DESIGN-channel-groups-model.md` §6 specifies by name.

What each assertion here guards:

  * a standalone in-guide channel is probed, and the member serving an in-guide group's
    row is probed - the two things the guide paints;
  * a group's NON-serving members are not, which is the whole retarget (his words: "It
    should not check every channel in every group");
  * a group with no active recurring schedule of its own contributes exactly ONE member,
    so a scheduleless group is never left unmonitored and a 50,000-member sweep group can
    never turn a nightly run into 83 hours;
  * a group that carries its own schedule contributes only through its guide row;
  * `Channel.test_enabled=False` still excludes, and an excluded channel stays LISTED so it
    remains visible and toggleable;
  * `monitored_channel_ids()` agrees with all of it, which is what stops a guide group with
    no schedule of its own from reporting its serving member as unmonitored.

Runs against a throwaway temp SQLite DB - never the live dvr.db.
  python3 -m unittest tests.test_guide_check_targeting
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tests.support import make_test_app  # noqa: E402
from tests.support.seed import (  # noqa: E402
    make_account, make_channel, make_channel_test, make_group, set_check,
)
from app import db  # noqa: E402
from app.database import GROUP_FORMAT_HIGHEST_SCORE, OnDemandTestJob  # noqa: E402


def _system_group():
    from app.database import ChannelGroup
    grp = ChannelGroup.query.filter_by(is_system=True).first()
    assert grp is not None, 'create_app must have made the pinned system group'
    return grp


def _schedule(group, recurring=True, paused=False, status='SCHEDULED'):
    """Put a schedule on `group`'s one check. Only `recurring AND (SCHEDULED or
    RUNNING) AND NOT paused` counts as a schedule of the group's own
    (channel_groups.active_recurring_jobs)."""
    return set_check(group, name=f'{group.name} check', status=status,
                     recurring=recurring, recur_paused=paused, recur_day=0,
                     recur_hour=3, recur_minute=0)


class SystemCheckTargetTests(unittest.TestCase):
    def setUp(self):
        self.t = make_test_app()
        self.ctx = self.t.app.app_context()
        self.ctx.push()
        self.acct = make_account()

    def tearDown(self):
        self.ctx.pop()
        self.t.cleanup()

    def _target_ids(self):
        # Through check_target_channels, the production entry point every caller uses -
        # not the new helper behind it, so these assert on behavior rather than on a
        # symbol existing (CLAUDE.md Testing: an ImportError is not evidence).
        from app.channel_groups import check_target_channels
        return [ch.id for ch in check_target_channels(_system_group())[0]]

    def test_standalone_guide_channel_is_probed(self):
        solo = make_channel(self.acct, name='Solo', in_guide=True)
        make_channel(self.acct, name='NotInGuide', in_guide=False)
        db.session.commit()
        self.assertEqual(self._target_ids(), [solo.id])

    def test_only_the_serving_member_of_a_guide_group_is_probed(self):
        # The retarget itself. Three recording-enabled members, one guide row: the check
        # probes the member the guide is actually showing and leaves the other two alone.
        best = make_channel(self.acct, name='Best')
        mid = make_channel(self.acct, name='Mid')
        worst = make_channel(self.acct, name='Worst')
        for ch, score in ((best, 95), (mid, 60), (worst, 20)):
            ch.health_score = score
        grp = make_group(name='FS1', members=[best, mid, worst], in_guide=True,
                         format_strategy=GROUP_FORMAT_HIGHEST_SCORE)
        _schedule(grp)   # its own schedule, so the fallback is not what is being measured
        db.session.commit()

        ids = self._target_ids()
        self.assertEqual(ids, [best.id],
                         'one probe per guide row, not one per member')
        self.assertNotIn(mid.id, ids)
        self.assertNotIn(worst.id, ids)

    def test_the_probe_follows_the_member_the_guide_is_painting(self):
        # Not "the first member" and not "every member": whichever member
        # guide_row_targets() says is serving. Flipping the scores moves the probe.
        from app.routes.guide import _guide_row_entries
        a = make_channel(self.acct, name='A')
        b = make_channel(self.acct, name='B')
        a.health_score, b.health_score = 30, 90
        grp = make_group(name='G', members=[a, b], in_guide=True,
                         format_strategy=GROUP_FORMAT_HIGHEST_SCORE)
        _schedule(grp)
        db.session.commit()
        self.assertEqual(self._target_ids(), [b.id])

        a.health_score, b.health_score = 90, 30
        db.session.commit()
        self.assertEqual(self._target_ids(), [a.id])
        serving = [e[2] for e in _guide_row_entries() if e[0] == 'group']
        self.assertEqual([ch.id for ch in serving], [a.id],
                         'the guide and the check must name the same serving member')

    def test_a_guide_group_with_no_recording_member_paints_nothing_and_is_not_probed_as_a_row(self):
        # It paints no guide row (nothing recording-enabled), so there is no row to probe.
        # It is still picked up by the scheduleless fallback below - here it carries its
        # own schedule, so nothing about it should reach the automatic check.
        a = make_channel(self.acct, name='A')
        b = make_channel(self.acct, name='B')
        grp = make_group(name='Unpromoted', members=[a, b], in_guide=True, recording=False)
        _schedule(grp)
        db.session.commit()
        self.assertEqual(self._target_ids(), [])

    def test_a_group_with_no_schedule_of_its_own_contributes_exactly_one_member(self):
        # The fallback. Without it a user with 40 scheduleless groups gets zero
        # monitoring; bounded at one member so a sweep group cannot blow the run up.
        members = [make_channel(self.acct, name=f'M{i}') for i in range(6)]
        for i, ch in enumerate(members):
            ch.health_score = 10 * i
        make_group(name='NoSchedule', members=members, in_guide=False, recording=False)
        db.session.commit()

        ids = self._target_ids()
        self.assertEqual(len(ids), 1, 'one probe for the whole group, never one per member')
        self.assertEqual(ids, [members[-1].id], 'and it is the best-ranked participant')

    def test_the_fallback_reads_the_switch_the_group_is_actually_using(self):
        # A group nobody records from yet has nothing recording-enabled, so asking for its
        # recording members would hand back nothing and the fallback would cover exactly
        # the groups it exists for (DESIGN-channel-groups-model.md 14).
        a = make_channel(self.acct, name='A')
        b = make_channel(self.acct, name='B')
        a.health_score, b.health_score = 10, 90
        make_group(name='StillAHealthCheck', members=[a, b], in_guide=False, recording=False)
        db.session.commit()
        self.assertEqual(self._target_ids(), [b.id])

    def test_a_group_with_an_active_recurring_schedule_gets_no_fallback_probe(self):
        a = make_channel(self.acct, name='A')
        grp = make_group(name='Scheduled', members=[a], in_guide=False, recording=False)
        _schedule(grp)
        db.session.commit()
        self.assertEqual(self._target_ids(), [])

    def test_a_one_shot_or_paused_schedule_is_not_a_schedule_of_its_own(self):
        # Ongoing monitoring is what the fallback is checking for: a one-off job has
        # already run and a paused recurring one has no live trigger behind it.
        a = make_channel(self.acct, name='A')
        b = make_channel(self.acct, name='B')
        one_shot = make_group(name='OneShot', members=[a], in_guide=False, recording=False)
        paused = make_group(name='Paused', members=[b], in_guide=False, recording=False)
        _schedule(one_shot, recurring=False)
        _schedule(paused, paused=True)
        db.session.commit()
        self.assertEqual(sorted(self._target_ids()), sorted([a.id, b.id]))

    def test_a_group_with_no_participating_member_contributes_nothing(self):
        a = make_channel(self.acct, name='A')
        make_group(name='AllOff', members=[a], in_guide=False, recording=False,
                   test_disabled=[a.id])
        db.session.commit()
        self.assertEqual(self._target_ids(), [])

    def test_the_system_group_is_never_its_own_fallback(self):
        # It IS the fallback; counting it as a group would be circular.
        # The system group has no membership rows and its pinned job is recurring +
        # SCHEDULED, so a fallback that counted it would probe it recursively or refuse to
        # probe anything. One standalone channel in, one probe out.
        make_channel(self.acct, name='Solo', in_guide=True)
        db.session.commit()
        self.assertEqual(len(self._target_ids()), 1)
        from app.channel_groups import groups_with_own_schedule_ids
        self.assertNotIn(_system_group().id, groups_with_own_schedule_ids())

    def test_a_channel_is_probed_once_however_many_rows_reach_it(self):
        shared = make_channel(self.acct, name='Shared', in_guide=True)
        shared.health_score = 90
        make_group(name='G', members=[shared], in_guide=True,
                   format_strategy=GROUP_FORMAT_HIGHEST_SCORE)
        db.session.commit()
        self.assertEqual(self._target_ids(), [shared.id])

    def test_channel_wide_test_enabled_off_excludes_but_still_lists(self):
        from app.channel_groups import check_run_channels, check_target_channels
        listed = make_channel(self.acct, name='Listed', in_guide=True)
        untested = make_channel(self.acct, name='Untested', in_guide=True,
                                test_enabled=False)
        db.session.commit()

        channels, excluded = check_target_channels(_system_group())
        self.assertEqual([c.id for c in channels], [listed.id, untested.id],
                         'an excluded channel stays listed, so it stays toggleable')
        self.assertEqual(excluded, {untested.id})
        self.assertEqual([c.id for c in check_run_channels(_system_group())], [listed.id])


class MonitoredCoverageTests(unittest.TestCase):
    """monitored_channel_ids() has to agree with the target set - two answers to "is this
    monitored on a schedule" is a disagreement the user sees on the group row."""

    def setUp(self):
        self.t = make_test_app()
        self.ctx = self.t.app.app_context()
        self.ctx.push()
        self.acct = make_account()
        # The pinned system job is created SCHEDULED+recurring by _ensure_system_health_job.
        sys_job = OnDemandTestJob.query.filter_by(is_system=True).first()
        assert sys_job is not None and sys_job.recurring and not sys_job.recur_paused

    def tearDown(self):
        self.ctx.pop()
        self.t.cleanup()

    def test_a_guide_group_with_no_schedule_has_its_serving_member_monitored(self):
        from app.channel_tester import monitored_channel_ids
        best = make_channel(self.acct, name='Best')
        other = make_channel(self.acct, name='Other')
        best.health_score, other.health_score = 90, 10
        make_group(name='FS1', members=[best, other], in_guide=True,
                   format_strategy=GROUP_FORMAT_HIGHEST_SCORE)
        db.session.commit()

        monitored = monitored_channel_ids()
        self.assertIn(best.id, monitored,
                      'the serving member of a guide row is covered by the automatic check')
        self.assertNotIn(other.id, monitored,
                         'its other members are not - that is what a schedule of its own is for')

    def test_a_groups_own_schedule_covers_every_tested_member(self):
        from app.channel_tester import monitored_channel_ids
        a = make_channel(self.acct, name='A')
        b = make_channel(self.acct, name='B')
        grp = make_group(name='G', members=[a, b], in_guide=False, recording=False)
        _schedule(grp)
        db.session.commit()
        self.assertEqual(monitored_channel_ids() & {a.id, b.id}, {a.id, b.id})


class InheritedCoverageNoticeTests(unittest.TestCase):
    """The group detail page's "already covered by the automatic check" notice reads the
    check's own target set. It used to count members carrying Channel.in_guide, which
    since dev/changelog/751 answers an unrelated question, and it was gated on the group
    being in the guide, which the scheduleless fallback makes wrong too."""

    def setUp(self):
        self.t = make_test_app()
        self.ctx = self.t.app.app_context()
        self.ctx.push()
        self.acct = make_account()

    def tearDown(self):
        self.ctx.pop()
        self.t.cleanup()

    def _inherited(self, group):
        from app.routes.channel_groups import build_group_detail_context
        # The builder calls url_for for the detail links, so it needs a request context.
        with self.t.app.test_request_context('/'):
            ctx = build_group_detail_context(group)
        return ctx['inherited_check']

    def test_a_scheduleless_group_outside_the_guide_is_reported_as_covered(self):
        a = make_channel(self.acct, name='A')
        make_channel_test(a, all_null=False, status='COMPLETED', resolution='1920x1080',
                          fps=60.0)
        grp = make_group(name='NoSchedule', members=[a], in_guide=False, recording=False)
        db.session.commit()
        notice = self._inherited(grp)
        self.assertIsNotNone(notice, 'the fallback covers it, so the page must say so')
        self.assertEqual(notice['channel_count'], 1)

    def test_a_guide_group_is_covered_even_though_no_member_holds_its_own_row(self):
        # The old count was `members WHERE Channel.in_guide` - zero here, so the page
        # claimed nothing covered this group while the automatic check was probing the
        # very member filling its guide row.
        best = make_channel(self.acct, name='Best', in_guide=False)
        other = make_channel(self.acct, name='Other', in_guide=False)
        best.health_score, other.health_score = 90, 10
        grp = make_group(name='FS1', members=[best, other], in_guide=True,
                         format_strategy=GROUP_FORMAT_HIGHEST_SCORE)
        db.session.commit()
        notice = self._inherited(grp)
        self.assertIsNotNone(notice)
        self.assertEqual(notice['channel_count'], 1,
                         'one member covered, not the whole membership')

    def test_a_group_with_its_own_schedule_is_not_reported_as_inheriting(self):
        a = make_channel(self.acct, name='A', in_guide=False)
        grp = make_group(name='G', members=[a], in_guide=False, recording=False)
        _schedule(grp)
        db.session.commit()
        self.assertIsNone(self._inherited(grp))


if __name__ == '__main__':
    unittest.main()
