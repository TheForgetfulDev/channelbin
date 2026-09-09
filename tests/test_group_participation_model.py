"""Tier 2 - the unified channel-group model's schema and its one enforcement rule.

Guards the four things `dev/changelog/741` introduced that nothing else asserts:

  * **The three column defaults.** `recording_enabled` False and `test_enabled` True are
    what "a group is created as a health check and promoted deliberately" actually means
    (`dev/docs/DESIGN-channel-groups-model.md` §14), and `format_strategy` defaults to
    `health_check_only` for the same reason. Getting any of them backwards silently makes
    every new group a recording source nobody asked for.
  * **`format_strategy` is NOT NULL and validated server-side.** A NULL would put back the
    `is None` branch the NOT NULL removed, and an unvalidated value would store a strategy
    no engine knows how to read.
  * **Nothing but a human writes a participation switch** (§4.1), and the human's write
    goes through `set_participation()`, which logs it (`dev/changelog/748`). The format
    lock filters at selection time; it never unticks a member. This is the rule the whole
    model rests on - if an engine can write these columns again, the spiral the model was
    built to remove comes back with it, and a write that skips the one writer moves the
    switch with no trace on any surface.
  * **`ChannelGroupEvent` cascade-deletes with its group**, per the
    teardown-releases-everything rule: the events are group-scoped and have nowhere else
    to belong.
  * **Every surface reads "is this member sitting out" through one gate** - the group's
    format_strategy, via `participation_is_recording()`. Reading a health_check_only
    group through the Recording switch dims every row of every new group, since §14 says
    a group is created with nothing recording-enabled (`dev/changelog/743`).
  * **No page tells a group it records when nothing does.** The detail page's description
    of itself is gated on the recording-enabled members - the set the recorder and the
    guide actually draw from - not on "this is not the system group"
    (`dev/changelog/750`).
"""
import json
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tests.support.app import make_test_app  # noqa: E402
from tests.support.seed import (make_account, make_channel, make_group,  # noqa: E402
                                make_channel_test, make_test_job)
from app import db  # noqa: E402
from app.channel_groups import FORMAT_STRATEGIES, FORMAT_STRATEGY_LABELS  # noqa: E402
from app.database import (ChannelGroup, ChannelGroupMember, ChannelGroupEvent,  # noqa: E402
                          GROUP_FORMAT_STRATEGIES, GROUP_FORMAT_HEALTH_CHECK_ONLY,
                          GROUP_FORMAT_HIGHEST_SCORE, GROUP_MEMBER_PARTICIPATION,
                          GROUP_FORMAT_STRATEGY_APPLIED)


class SchemaDefaultsTests(unittest.TestCase):
    def setUp(self):
        self.t = make_test_app()
        self.ctx = self.t.app.app_context()
        self.ctx.push()
        self.acct = make_account()

    def tearDown(self):
        self.ctx.pop()
        self.t.cleanup()

    def test_a_new_group_is_health_check_only(self):
        grp = ChannelGroup(name='Fresh')
        db.session.add(grp)
        db.session.commit()
        self.assertEqual(GROUP_FORMAT_HEALTH_CHECK_ONLY, grp.format_strategy)

    def test_a_new_member_starts_recording_off_and_health_check_on(self):
        ch = make_channel(self.acct, name='Feed')
        grp = ChannelGroup(name='Fresh')
        db.session.add(grp)
        db.session.flush()
        m = ChannelGroupMember(group_id=grp.id, channel_id=ch.id)
        db.session.add(m)
        db.session.commit()
        self.assertFalse(m.recording_enabled, 'nothing records until a human says so')
        self.assertTrue(m.test_enabled, 'a new member is monitored by default')

    def test_format_strategy_is_not_nullable_in_the_database(self):
        """Raw SQL on purpose: the ORM substitutes its Python-side default for None, so
        going through ChannelGroup() would prove the default works, not the constraint."""
        from sqlalchemy.exc import IntegrityError
        with self.assertRaises(IntegrityError):
            db.session.execute(db.text(
                'INSERT INTO channel_groups (name, in_guide, format_strategy, '
                'health_score_sample_count, is_system) '
                "VALUES ('Null strategy', 0, NULL, 0, 0)"))
            db.session.commit()
        db.session.rollback()

    def test_the_participation_columns_are_not_nullable_in_the_database(self):
        from sqlalchemy.exc import IntegrityError
        ch = make_channel(self.acct, name='Feed')
        grp = ChannelGroup(name='G')
        db.session.add(grp)
        db.session.commit()
        for col in ('recording_enabled', 'test_enabled'):
            with self.subTest(column=col):
                with self.assertRaises(IntegrityError):
                    db.session.execute(db.text(
                        f'INSERT INTO channel_group_members '
                        f'(group_id, channel_id, position, recording_enabled, test_enabled) '
                        f"VALUES ({grp.id}, {ch.id}, 0, "
                        f"{'NULL' if col == 'recording_enabled' else '0'}, "
                        f"{'NULL' if col == 'test_enabled' else '0'})"))
                    db.session.commit()
                db.session.rollback()

    def test_the_value_list_is_the_eight_the_design_doc_names(self):
        self.assertEqual(
            ('health_check_only', 'highest_score', 'highest_bitrate', 'highest_resolution',
             'most_channels', 'balanced', 'manual', 'unmanaged'),
            GROUP_FORMAT_STRATEGIES)


class ParticipationRouteTests(unittest.TestCase):
    def setUp(self):
        self.t = make_test_app()
        self.t.app.config['WTF_CSRF_ENABLED'] = False
        self.client = self.t.client
        self.ctx = self.t.app.app_context()
        self.ctx.push()
        self.acct = make_account()
        self.ch = make_channel(self.acct, name='Feed')
        self.grp = make_group(name='G', members=[self.ch], in_guide=False)
        db.session.commit()

    def tearDown(self):
        self.ctx.pop()
        self.t.cleanup()

    def _post(self, **body):
        return self.client.post(
            f'/api/channel-groups/{self.grp.id}/members/participation', json=body)

    def _membership(self):
        db.session.expire_all()
        return ChannelGroupMember.query.filter_by(
            group_id=self.grp.id, channel_id=self.ch.id).first()

    def test_turning_recording_off_writes_the_column_and_an_event(self):
        # `confirm` because this group's only member is the one being switched off, which
        # is DESIGN-channel-groups-model.md 15's breach path 2 (dev/changelog/757). The
        # switch still moves and still logs; what changed is that it is asked about first.
        resp = self._post(channel_id=self.ch.id, field='recording_enabled', enabled=False,
                          confirm=True)
        self.assertEqual(200, resp.status_code, resp.get_json())
        self.assertFalse(self._membership().recording_enabled)

        ev = ChannelGroupEvent.query.filter_by(
            group_id=self.grp.id, event_type=GROUP_MEMBER_PARTICIPATION).one()
        self.assertEqual(self.ch.id, ev.channel_id, 'a membership fact carries its channel')
        self.assertIn('Recording', ev.detail)
        self.assertIn('off', ev.detail)

    def test_the_health_check_switch_is_writable_too(self):
        resp = self._post(channel_id=self.ch.id, field='test_enabled', enabled=False)
        self.assertEqual(200, resp.status_code, resp.get_json())
        self.assertFalse(self._membership().test_enabled)

    def test_an_unknown_field_is_refused(self):
        """Enforcement lives server-side: the route validates the field itself rather
        than trusting whichever control posted to it, and never hands an unknown one to
        set_participation()."""
        resp = self._post(channel_id=self.ch.id, field='is_system', enabled=True)
        self.assertEqual(400, resp.status_code)
        self.assertIn('error', resp.get_json())

    def test_a_no_op_write_logs_no_event(self):
        """A switch that did not move is not something that happened - logging it would
        fill the Activity Timeline with lines saying nothing changed."""
        self._post(channel_id=self.ch.id, field='recording_enabled', enabled=True)
        self.assertEqual(0, ChannelGroupEvent.query.count())

    def test_a_channel_outside_the_group_is_a_404(self):
        other = make_channel(self.acct, name='Elsewhere', stream_id=999)
        db.session.commit()
        resp = self._post(channel_id=other.id, field='recording_enabled', enabled=False)
        self.assertEqual(404, resp.status_code)


class FormatStrategyRouteTests(unittest.TestCase):
    def setUp(self):
        self.t = make_test_app()
        self.t.app.config['WTF_CSRF_ENABLED'] = False
        self.client = self.t.client
        self.ctx = self.t.app.app_context()
        self.ctx.push()
        self.acct = make_account()
        self.grp = make_group(name='G', members=[make_channel(self.acct, name='Feed')],
                              in_guide=False)
        db.session.commit()

    def tearDown(self):
        self.ctx.pop()
        self.t.cleanup()

    def test_a_known_strategy_is_stored(self):
        resp = self.client.post(f'/api/channel-groups/{self.grp.id}/format-strategy',
                                json={'strategy': 'highest_bitrate'})
        self.assertEqual(200, resp.status_code, resp.get_json())
        db.session.expire_all()
        self.assertEqual('highest_bitrate',
                         db.session.get(ChannelGroup, self.grp.id).format_strategy)

    def test_an_unknown_strategy_is_refused(self):
        resp = self.client.post(f'/api/channel-groups/{self.grp.id}/format-strategy',
                                json={'strategy': 'whatever_sounds_best'})
        self.assertEqual(400, resp.status_code)
        db.session.expire_all()
        self.assertEqual(GROUP_FORMAT_HEALTH_CHECK_ONLY,
                         db.session.get(ChannelGroup, self.grp.id).format_strategy)


class LockFiltersRatherThanUnticksTests(unittest.TestCase):
    """§4.1's rule, asserted end to end rather than as prose: locking a group to a format
    no member matches must leave every membership row exactly as the user left it."""

    def setUp(self):
        self.t = make_test_app()
        self.t.app.config['WTF_CSRF_ENABLED'] = False
        self.client = self.t.client
        self.ctx = self.t.app.app_context()
        self.ctx.push()
        self.acct = make_account()
        self.hd = make_channel(self.acct, name='HD feed')
        self.sd = make_channel(self.acct, name='SD feed', stream_id=2)
        make_channel_test(self.hd, status='COMPLETED', resolution='1920x1080', fps=60,
                          bitrate_kbps=4000)
        make_channel_test(self.sd, status='COMPLETED', resolution='1280x720', fps=30,
                          bitrate_kbps=2000)
        self.grp = make_group(name='G', members=[self.hd, self.sd], in_guide=False)
        db.session.commit()

    def tearDown(self):
        self.ctx.pop()
        self.t.cleanup()

    def _switches(self):
        db.session.expire_all()
        return {m.channel_id: (m.recording_enabled, m.test_enabled)
                for m in db.session.get(ChannelGroup, self.grp.id).memberships}

    def test_locking_the_format_unticks_nothing(self):
        before = self._switches()
        resp = self.client.post(f'/api/channel-groups/{self.grp.id}/format',
                                json={'resolution': '1920x1080', 'fps': 60})
        self.assertEqual(200, resp.status_code, resp.get_json())
        self.assertEqual(before, self._switches(),
                         'the lock filters at selection time; it never writes a member')

    def test_the_mismatch_is_still_detected_and_reported(self):
        """Subtracting the write half must not subtract the visibility - the mismatch is
        still a fact the group page and the event log show."""
        from app.channel_groups import plan_reconcile
        grp = db.session.get(ChannelGroup, self.grp.id)
        grp.set_locked_format('1920x1080', 60)
        db.session.commit()
        from app.routes.channel_tests import _latest_tests_by_channel
        memberships = list(grp.memberships)
        latest = _latest_tests_by_channel([m.channel_id for m in memberships])
        plan = plan_reconcile(grp, memberships, latest)
        self.assertEqual([self.sd.id], plan['outliers'])


class GroupEventTeardownTests(unittest.TestCase):
    def setUp(self):
        self.t = make_test_app()
        self.ctx = self.t.app.app_context()
        self.ctx.push()
        self.acct = make_account()

    def tearDown(self):
        self.ctx.pop()
        self.t.cleanup()

    def test_deleting_a_group_deletes_its_events(self):
        grp = make_group(name='G', members=[make_channel(self.acct, name='Feed')],
                         in_guide=False)
        db.session.add(ChannelGroupEvent(group_id=grp.id, event_type=GROUP_MEMBER_PARTICIPATION,
                                         detail='Recording turned off by hand'))
        db.session.commit()
        self.assertEqual(1, ChannelGroupEvent.query.count())

        db.session.delete(db.session.get(ChannelGroup, grp.id))
        db.session.commit()
        self.assertEqual(0, ChannelGroupEvent.query.count(),
                         'group-scoped events have nowhere else to belong')


class ParticipationDisplayGateTests(unittest.TestCase):
    """dev/docs/BUGS.md 2026-08-19 08:32 - the Groups list page dimmed every member of
    every freshly created group, because it read participation through the Recording
    switch that §14 says is off on a new group by design. The detail page had the
    format_strategy gate; the list page and the clone payload did not."""

    def setUp(self):
        self.t = make_test_app()
        self.ctx = self.t.app.app_context()
        self.ctx.push()
        self.acct = make_account()
        self.client = self.t.app.test_client()

    def tearDown(self):
        self.ctx.pop()
        self.t.cleanup()

    def _fresh_group(self):
        """A group in its documented starting state: health_check_only, nothing
        recording-enabled, every member health-checked."""
        chans = [make_channel(self.acct, name='Feed A'), make_channel(self.acct, name='Feed B')]
        grp = make_group(name='Brand New', members=chans, in_guide=False, recording=False)
        db.session.commit()
        return grp, chans

    def test_a_fresh_group_dims_nobody_on_the_groups_list_page(self):
        self._fresh_group()
        html = self.client.get('/channel-groups').get_data(as_text=True)
        self.assertNotIn('grp-mrow is-disabled', html,
                         'a member nobody has turned off is not disabled')
        self.assertNotIn('&#10005; disabled', html)

    def test_a_promoted_group_still_dims_a_member_with_recording_off(self):
        chans = [make_channel(self.acct, name='Feed A'), make_channel(self.acct, name='Feed B')]
        grp = make_group(name='Promoted', members=chans, in_guide=False,
                         disabled=[chans[1].id])
        grp.format_strategy = GROUP_FORMAT_HIGHEST_SCORE
        db.session.commit()
        html = self.client.get('/channel-groups').get_data(as_text=True)
        self.assertIn('grp-mrow is-disabled', html,
                      'once the group records, the Recording switch is the one that dims')
        self.assertIn('Recording is off for this member', html)

    def test_a_fresh_group_dims_a_member_whose_health_check_is_off(self):
        chans = [make_channel(self.acct, name='Feed A'), make_channel(self.acct, name='Feed B')]
        make_group(name='Brand New', members=chans, in_guide=False, recording=False,
                   test_disabled=[chans[1].id])
        db.session.commit()
        html = self.client.get('/channel-groups').get_data(as_text=True)
        self.assertIn('grp-mrow is-disabled', html)
        self.assertIn('the automatic health check skips it', html,
                      'the tooltip must name the switch that is actually off')

    def test_the_list_page_and_the_detail_page_agree(self):
        grp, _ = self._fresh_group()
        listed = self.client.get('/channel-groups').get_data(as_text=True)
        detail = self.client.get(f'/channel-groups/{grp.id}').get_data(as_text=True)
        self.assertNotIn('grp-mrow is-disabled', listed)
        self.assertNotIn('is-disabled', detail,
                         'one group cannot be disabled on one page and active on another')

    def test_clone_info_reports_a_fresh_group_as_fully_participating(self):
        grp, _ = self._fresh_group()
        payload = self.client.get(f'/api/channel-groups/{grp.id}/clone-info').get_json()
        self.assertTrue(all(m['disabled'] is None for m in payload['channels']),
                        'the clone screen preselects by this flag - dimming all of them '
                        'would offer a clone of nothing')


class GroupEventTimelineTests(unittest.TestCase):
    """dev/docs/BUGS.md 2026-08-19 10:07 - `ChannelGroupEvent` was written by the
    participation route and read by nothing: `_build_group_timeline` never queried the
    table and `_timeline.html` had no branch for it, so the table the commit added
    specifically so a group fact could be *seen* (§4.5) recorded facts silently. That is
    principle 1 inverted, and it is what this class exists to keep fixed."""

    def setUp(self):
        self.t = make_test_app()
        self.t.app.config['WTF_CSRF_ENABLED'] = False
        self.client = self.t.client
        self.ctx = self.t.app.app_context()
        self.ctx.push()
        self.acct = make_account()
        self.ch = make_channel(self.acct, name='Feed')
        self.grp = make_group(name='G', members=[self.ch], in_guide=False)
        db.session.commit()

    def tearDown(self):
        self.ctx.pop()
        self.t.cleanup()

    def _timeline_html(self):
        """Just the Activity Timeline block - a detail string that appears anywhere else on
        the page (a member row, a tooltip) would not prove the timeline shows it."""
        html = self.client.get(f'/channel-groups/{self.grp.id}').get_data(as_text=True)
        # The block is only rendered when the group has activity at all, so its absence is
        # itself the failure this class guards - return empty rather than raising, or the
        # test reports a ValueError instead of the missing entry.
        start = html.find('ch-timeline-log')
        return html[start:] if start != -1 else ''

    def test_a_participation_change_appears_in_the_activity_timeline(self):
        resp = self.client.post(
            f'/api/channel-groups/{self.grp.id}/members/participation',
            json={'channel_id': self.ch.id, 'field': 'recording_enabled', 'enabled': False,
                  # 15's breach path 2 - the only member is the one going off
                  # (dev/changelog/757). Confirmed, then carried out.
                  'confirm': True})
        self.assertEqual(200, resp.status_code, resp.get_json())
        ev = ChannelGroupEvent.query.filter_by(
            group_id=self.grp.id, event_type=GROUP_MEMBER_PARTICIPATION).one()

        timeline = self._timeline_html()
        self.assertIn(ev.detail, timeline,
                      'a fact the app recorded but never shows is the defect this table '
                      'was added to remove')
        self.assertIn('Participation Changed', timeline,
                      'the row needs a label, not a bare detail string')

    def test_the_entry_links_back_to_the_membership_it_is_about(self):
        """`channel_id` is nullable because some group facts have no member; when it is
        set, §4.5 says the timeline renders it as the entry's source."""
        self.client.post(
            f'/api/channel-groups/{self.grp.id}/members/participation',
            json={'channel_id': self.ch.id, 'field': 'test_enabled', 'enabled': False})
        timeline = self._timeline_html()
        self.assertIn(f'/channels/{self.ch.id}', timeline)

    def test_a_group_scoped_event_renders_without_a_member(self):
        """The lock moving is a fact about the group as a whole - a NULL channel_id must
        render a row rather than blow up resolving a source label."""
        db.session.add(ChannelGroupEvent(
            group_id=self.grp.id, event_type=GROUP_FORMAT_STRATEGY_APPLIED,
            detail='Locked to 1920x1080 60fps by highest score'))
        db.session.commit()
        timeline = self._timeline_html()
        self.assertIn('Locked to 1920x1080 60fps by highest score', timeline)
        self.assertIn('Format Strategy Applied', timeline)


class FormatStrategyEventTests(unittest.TestCase):
    """dev/docs/BUGS.md 2026-08-19 11:32 - `GROUP_FORMAT_STRATEGY_APPLIED` was declared and
    never written by anything, so the one route that actually moves a group's format lock
    in response to a strategy (`apply_format_plan`) left no trace of it. §4.5 says that
    event carries the old format, the new format, the strategy that chose it and the
    numbers behind it, and that the numbers are `_format_strategy_entry()`'s own
    `rationale` rather than a second wording of the same decision."""

    def setUp(self):
        self.t = make_test_app()
        self.t.app.config['WTF_CSRF_ENABLED'] = False
        self.client = self.t.client
        self.ctx = self.t.app.app_context()
        self.ctx.push()
        self.acct = make_account()
        # Two 1080p60 members and one 720p30 outlier: every strategy picks the HD bucket.
        self.hd1 = make_channel(self.acct, name='HD 1')
        self.hd2 = make_channel(self.acct, name='HD 2')
        self.sd = make_channel(self.acct, name='SD 1')
        make_channel_test(self.hd1, status='COMPLETED', resolution='1920x1080', fps=60,
                          bitrate_kbps=3740)
        make_channel_test(self.hd2, status='COMPLETED', resolution='1920x1080', fps=60,
                          bitrate_kbps=3800)
        make_channel_test(self.sd, status='COMPLETED', resolution='1280x720', fps=30,
                          bitrate_kbps=3600)
        self.grp = make_group(name='G', members=[self.hd1, self.hd2, self.sd], in_guide=False)
        db.session.commit()

    def tearDown(self):
        self.ctx.pop()
        self.t.cleanup()

    def _apply(self, strategy='most_channels', non_matching='keep'):
        resp = self.client.post(f'/api/channel-groups/{self.grp.id}/apply-format-plan',
                                json={'strategy': strategy, 'non_matching': non_matching})
        self.assertEqual(200, resp.status_code, resp.get_json())
        return resp

    def _event(self):
        return ChannelGroupEvent.query.filter_by(
            group_id=self.grp.id, event_type=GROUP_FORMAT_STRATEGY_APPLIED).one()

    def _timeline_html(self):
        html = self.client.get(f'/channel-groups/{self.grp.id}').get_data(as_text=True)
        start = html.find('ch-timeline-log')
        return html[start:] if start != -1 else ''

    def _rationale(self, strategy='most_channels'):
        """The picker's own sentence for this strategy, recomputed the way the route does -
        asserting against a hand-typed copy would pass even if the event wrote its own."""
        from app.channel_groups import plan_format_selection
        from app.routes.channel_tests import _latest_tests_by_channel
        members = [self.hd1, self.hd2, self.sd]
        latest = _latest_tests_by_channel([ch.id for ch in members])
        return plan_format_selection(members, latest)['strategies'][strategy]['rationale']

    def test_applying_a_strategy_records_the_lock_move(self):
        expected = self._rationale()
        self._apply()
        ev = self._event()
        self.assertIsNone(ev.channel_id, 'the lock is a fact about the whole group')
        self.assertIn('1920x1080 @ 60', ev.detail)
        self.assertIn('Most members', ev.detail, 'the detail names the strategy that chose it')
        self.assertIn(expected, ev.detail,
                      "§4.5: reuse the picker's rationale rather than writing a second "
                      'explanation of the same decision')

    def test_the_event_reaches_the_group_activity_timeline(self):
        self._apply()
        timeline = self._timeline_html()
        self.assertIn(self._event().detail, timeline,
                      'a lock move nobody can see is the silence principle 1 refuses')
        self.assertIn('Format Strategy Applied', timeline)

    def test_the_detail_names_the_format_the_lock_moved_from(self):
        grp = db.session.get(ChannelGroup, self.grp.id)
        grp.set_locked_format('1280x720', 30)
        db.session.commit()
        self._apply()
        self.assertIn('moved from 1280x720 @ 30 to 1920x1080 @ 60', self._event().detail)

    def test_removing_the_non_matching_members_is_counted_in_the_same_event(self):
        self._apply(non_matching='remove')
        ev = self._event()
        self.assertIn('1 non-matching member removed', ev.detail)
        self.assertEqual(1, json.loads(ev.extra_data)['removed'])

    def test_every_strategy_has_a_label_matching_the_pickers(self):
        """The detail names the strategy in the same words the format picker offered it
        under; a key with no label would put a bare `highest_bitrate` in front of the user."""
        self.assertEqual(set(FORMAT_STRATEGIES), set(FORMAT_STRATEGY_LABELS))
        js = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                          'static', 'js', 'format-plan.js')
        with open(js) as fh:
            src = fh.read()
        # The client's list carries a third column - one sentence of help text per value
        # (dev/changelog/756) - so the pair is matched by prefix rather than exactly.
        for key, label in FORMAT_STRATEGY_LABELS.items():
            self.assertIn(f"['{key}', '{label}',", src,
                          'the server-side label and the dropdown label are one wording')


class JobChannelToggleLoggingTests(unittest.TestCase):
    """dev/docs/BUGS.md 2026-08-19 12:04 - the health check's own channel list toggled a
    member's Health check switch by assigning the column and committing, so a
    participation change made from that page reached no surface at all: no
    `ChannelGroupEvent`, nothing in the group's Activity Timeline. §4.5 says a change to
    either participation column writes a `GROUP_MEMBER_PARTICIPATION` event, and the route
    it was written next to claimed in its docstring to be the only writer of the column
    while this one existed."""

    def setUp(self):
        self.t = make_test_app()
        self.t.app.config['WTF_CSRF_ENABLED'] = False
        self.client = self.t.client
        self.ctx = self.t.app.app_context()
        self.ctx.push()
        self.acct = make_account()
        self.ch = make_channel(self.acct, name='Feed')
        self.job = make_test_job(name='Nightly check', channels=[self.ch])
        self.grp_id = self.job.group_id
        db.session.commit()

    def tearDown(self):
        self.ctx.pop()
        self.t.cleanup()

    def _toggle(self, channel_id=None, job_id=None):
        return self.client.post(
            f'/api/channel-tests/on-demand/{job_id or self.job.id}'
            f'/channels/{channel_id or self.ch.id}/toggle')

    def _events(self):
        db.session.expire_all()
        return ChannelGroupEvent.query.filter_by(
            group_id=self.grp_id, event_type=GROUP_MEMBER_PARTICIPATION).order_by(
                ChannelGroupEvent.id).all()

    def _membership(self):
        db.session.expire_all()
        return ChannelGroupMember.query.filter_by(
            group_id=self.grp_id, channel_id=self.ch.id).first()

    def test_toggling_a_member_off_writes_the_column_and_an_event(self):
        resp = self._toggle()
        self.assertEqual(200, resp.status_code, resp.get_json())
        self.assertFalse(self._membership().test_enabled)

        events = self._events()
        self.assertEqual(1, len(events), 'the switch moved, so the timeline gets a line')
        ev = events[0]
        self.assertEqual(self.ch.id, ev.channel_id, 'a membership fact carries its channel')
        self.assertIn('Health check', ev.detail)
        self.assertIn('off', ev.detail)

    def test_the_event_names_the_surface_the_human_was_standing_on(self):
        """Two pages write these columns; §4.5 asks the event to carry who changed it and
        why, which is not answerable from the column alone."""
        self._toggle()
        ev = self._events()[0]
        self.assertIn("health check's channel list", ev.detail)
        self.assertEqual('check_channel_list', json.loads(ev.extra_data)['surface'])

    def test_toggling_back_on_logs_the_other_direction(self):
        self._toggle()
        self._toggle()
        self.assertTrue(self._membership().test_enabled)
        details = [e.detail for e in self._events()]
        self.assertEqual(2, len(details), 'both moves happened, so both are recorded')
        self.assertIn('off', details[0])
        self.assertIn('on', details[1])

    def test_the_change_reaches_the_groups_activity_timeline(self):
        """The event exists to be seen - a row written into a table nothing renders is the
        same silence as no row at all (dev/changelog/744 built the display half)."""
        from markupsafe import escape
        self._toggle()
        detail = self._events()[0].detail
        html = self.client.get(f'/channel-groups/{self.grp_id}').get_data(as_text=True)
        # The block only renders when the group has activity at all, so its absence is
        # itself the failure - return empty rather than raising on the missing marker.
        start = html.find('ch-timeline-log')
        timeline = html[start:] if start != -1 else ''
        self.assertIn(str(escape(detail)), timeline)
        self.assertIn('Participation Changed', timeline)

    def test_the_system_group_branch_still_writes_the_channel_wide_switch(self):
        """Characterization, not a defect guard: it passes either way and is here so the
        membership fix cannot leak into the branch beside it. The TV Guide Channels check
        has no membership rows to write - its channel list is computed - so it flips
        Channel.test_enabled, a different column with channel-wide meaning."""
        from app.database import Channel, OnDemandTestJob
        sys_job = OnDemandTestJob.query.filter_by(is_system=True).first()
        self.assertIsNotNone(sys_job, 'the system check is created at app startup')
        guide_ch = make_channel(self.acct, name='In the guide', stream_id=771, in_guide=True)
        db.session.commit()

        resp = self._toggle(channel_id=guide_ch.id, job_id=sys_job.id)
        self.assertEqual(200, resp.status_code, resp.get_json())
        db.session.expire_all()
        self.assertFalse(db.session.get(Channel, guide_ch.id).test_enabled)
        self.assertEqual(0, ChannelGroupEvent.query.count(),
                         'no membership moved, so there is no membership fact to log')


class DetailPageSelfDescriptionTests(unittest.TestCase):
    """dev/docs/BUGS.md 2026-08-19 12:26 - the group detail page described every
    non-system group as "A channel group. Records the best-scoring channel and fails
    over...", because the copy was gated on `is_stored` (= not the system group) rather
    than on anything about recording. §14 says a group is created with nothing
    recording-enabled, so the page told every new group it records when the recorder and
    the guide both draw from `recording_members()` and would find nobody.

    The gate is the recording-enabled members, not `format_strategy`: a promoted group
    with nobody enabled records nothing either, so `participation_is_recording()` - which
    answers a different question, which switch a member ROW is drawn by - would only have
    narrowed the false claim."""

    def setUp(self):
        self.t = make_test_app()
        self.client = self.t.client
        self.ctx = self.t.app.app_context()
        self.ctx.push()
        self.acct = make_account()

    def tearDown(self):
        self.ctx.pop()
        self.t.cleanup()

    def _page(self, group):
        resp = self.client.get(f'/channel-groups/{group.id}')
        self.assertEqual(200, resp.status_code)
        return resp.get_data(as_text=True)

    @staticmethod
    def _block(html, marker, end='</p>'):
        """One region of the page, whitespace collapsed. A claim made in a modal's copy
        elsewhere in the markup would not prove what the header says, and the template
        wraps its sentences across lines, so a raw substring search reads false."""
        start = html.index(marker)
        return ' '.join(html[start:html.index(end, start)].split())

    def _lead(self, group):
        return self._block(self._page(group), '<p class="gd-sub-lead">')

    def _status_msg(self, group):
        return self._block(self._page(group), '<span class="gd-ab-msg">', end='</span>')

    def _icon_tip(self, group):
        return self._block(self._page(group), '<span class="gd-kind tip-plain"', end='>')

    def _members(self):
        return [make_channel(self.acct, name='Feed A'), make_channel(self.acct, name='Feed B')]

    def _fresh(self, in_guide=False, **kw):
        """The documented starting state: health_check_only, nothing recording-enabled."""
        grp = make_group(name='Brand New', members=self._members(), in_guide=in_guide,
                         recording=False, **kw)
        db.session.commit()
        return grp

    def _promoted(self, in_guide=False, **kw):
        grp = make_group(name='Promoted', members=self._members(), in_guide=in_guide, **kw)
        grp.format_strategy = GROUP_FORMAT_HIGHEST_SCORE
        db.session.commit()
        return grp

    def test_a_fresh_group_does_not_say_it_records(self):
        lead = self._lead(self._fresh())
        self.assertNotIn('Records the best', lead,
                         'nothing is enabled for recording, so nothing records')
        self.assertNotIn('records with failover', lead)
        self.assertIn('no member enabled for recording', lead)

    def test_the_two_pages_do_not_describe_one_group_two_ways(self):
        """The finding itself: groups.html says "a group with no member enabled for
        recording is simply a health check" while this page, for the identical group,
        called it a channel group that records."""
        grp = self._fresh()
        listed = ' '.join(self.client.get('/channel-groups').get_data(as_text=True).split())
        self.assertIn('no member enabled for recording is simply a health check', listed,
                      'the list page sentence the detail page is held to')
        lead = self._lead(grp)
        self.assertNotIn('Records the best', lead)
        self.assertNotIn('records with failover', lead)

    def test_a_fresh_group_with_a_check_says_the_check_is_what_it_does(self):
        grp = self._fresh()
        job = make_test_job(name='Nightly', channels=[])
        job.group_id = grp.id
        db.session.commit()
        lead = self._lead(grp)
        self.assertNotIn('records with failover', lead)
        self.assertIn('testing those feeds on a schedule', lead)

    def test_a_promoted_group_with_nobody_enabled_still_does_not_claim_to_record(self):
        """The state that decided the gate: `format_strategy` is a real recording
        strategy, but no member has Recording on, so a recording would find no member to
        start from. A strategy-keyed flag would have gone on claiming failover here."""
        lead = self._lead(self._promoted(recording=False))
        self.assertNotIn('Records the best', lead)
        self.assertIn('no member enabled for recording', lead)

    def test_a_group_in_the_guide_with_nobody_enabled_says_the_guide_has_no_row(self):
        """_guide_row_entries() skips a group whose recording_members() is empty, so the
        status bar's "Recording from the best channel" was a claim about a row the guide
        does not draw."""
        grp = self._fresh(in_guide=True)
        msg = self._status_msg(grp)
        self.assertNotIn('Recording from the best channel', msg)
        self.assertIn('draws no row', msg)

    def test_the_title_icon_tooltip_makes_the_same_claim_as_the_lead(self):
        tip = self._icon_tip(self._fresh())
        self.assertNotIn('Records the best', tip)
        self.assertIn('nothing records from it', tip)

    def test_a_group_that_records_still_says_so(self):
        """Characterization, not a defect guard - it passes before and after the fix. It
        is here so a later change cannot make the fresh-group copy honest by taking the
        recording group's own description away from it."""
        lead = self._lead(self._promoted())
        self.assertIn('fails over to the next-best feed', lead)
        self.assertNotIn('no member enabled for recording', lead)

    def test_a_group_that_records_and_is_in_the_guide_still_says_so(self):
        """Characterization, same reason: the status-bar branch that was correct."""
        grp = self._promoted(in_guide=True)
        self.assertIn('Recording from the best channel', self._status_msg(grp))


class FreshDatabaseOnlyMigrationTests(unittest.TestCase):
    """Step 41 has no upgrade path and says so. A database old enough to reach it predates
    the columns the current models require, so starting against it would fail somewhere far
    from the cause - the refusal is what turns that into an explanation."""

    def test_step_41_refuses_and_names_the_fix(self):
        import app.migrations as M
        with self.assertRaises(SystemExit) as caught:
            M._m041_channel_group_model_requires_fresh_db(None, None)
        msg = str(caught.exception)
        self.assertIn('dvr.db', msg)
        self.assertIn('-wal', msg, 'moving the db without its WAL loses committed data')
        self.assertIn('move, do not delete', msg)

    def test_it_is_registered_at_version_41_and_nothing_can_skip_it(self):
        """Deliberately NOT "41 is the last step" - later steps are expected and 42 landed
        in dev/changelog/756. What must hold is that the refusal sits AT 41, so every
        database below it runs into the refusal before any later step can touch a schema
        that predates the columns those steps assume."""
        import app.migrations as M
        by_version = {v: fn for v, _desc, fn in M.SCHEMA_MIGRATIONS}
        self.assertIs(M._m041_channel_group_model_requires_fresh_db, by_version[41])
        self.assertGreaterEqual(M.CURRENT_SCHEMA_VERSION, 41)
        self.assertEqual(sorted(by_version), [v for v, _d, _f in M.SCHEMA_MIGRATIONS],
                         'steps run in version order, so nothing runs before the refusal')


if __name__ == '__main__':
    unittest.main()
