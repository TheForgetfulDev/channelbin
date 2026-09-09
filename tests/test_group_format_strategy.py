"""Tier 2 - the standing format strategy, and the selection-time filter it feeds.

Guards what `dev/changelog/753` built, which is the two halves of
`dev/docs/DESIGN-channel-groups-model.md` §5's three-layer story that were missing:

  * **Layer 1, the standing strategy.** `ChannelGroup.format_strategy` was stored and
    validated by `dev/changelog/741` but nothing ever acted on it - the setting was inert.
    It now re-evaluates after each health check run and moves the format lock, which is
    what makes "today's recording might be 720p @ 30fps, and tomorrow's might be
    1080p @ 60fps, not because I changed anything" true (DECIDED 9).
  * **Layer 2, the filter.** §5 says the lock *filters* recording-enabled members where
    they are chosen. It was never built: `app/recorder.py` went straight from
    `recording_members()` to `pick_best_member()` and never read the lock. Item #5 deleted
    the old auto-untick write half on the understanding this read half replaced it, so
    until now a locked group's mismatched member could still be the one that recorded.

The rules with teeth, each of which is a separate way to get this wrong:

  * A strategy that manages no lock (`health_check_only`, `highest_score`, `manual`,
    `unmanaged`) never writes one, and `unmanaged` never filters even when a stale lock is
    still on the row - §16.2 promises it "records from whichever member ranks best,
    whatever its format".
  * An **untested** member survives the filter. Unknown is not proven-different, and the
    opposite call makes a never-tested member permanently unselectable in a locked group.
  * **Zero survivors is an override, never a skip** (§15.2). The recording runs from the
    best-ranked member anyway, and says so three times over.
  * The re-evaluation is keyed on the **channels tested**, not on the job's own group -
    the automatic TV Guide job's group is the system group, which has no strategy, so
    reading that would starve every scheduleless group the fallback exists to serve
    (`dev/changelog/752`).
  * It is **silent when nothing moved**, and logs the no-winner state once rather than
    nightly. A log that agrees with itself every night buries the night it did not.
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tests.support.app import make_test_app  # noqa: E402
from tests.support.seed import (make_account, make_channel, make_group,  # noqa: E402
                                make_channel_test)
from app import db  # noqa: E402
from app.channel_groups import (apply_format_strategy, format_eligible_members,  # noqa: E402
                                group_manages_format, strategy_lock_plan,
                                guide_row_targets, recording_members)
from app.database import (ChannelGroupEvent, ChannelGroupMember, Alert,  # noqa: E402
                          GROUP_FORMAT_STRATEGY_APPLIED, GROUP_FORMAT_STRATEGY_BLOCKED,
                          GROUP_FORMAT_HEALTH_CHECK_ONLY, GROUP_FORMAT_HIGHEST_SCORE,
                          GROUP_FORMAT_MANUAL, GROUP_FORMAT_UNMANAGED)

HD = ('1920x1080', 60)
SD = ('1280x720', 30)


def _tested(channel, key, score=None):
    """Give `channel` a passing test reporting `key`'s format, and a health score."""
    if score is not None:
        channel.health_score = score
    make_channel_test(channel, all_null=False, status='COMPLETED',
                      connected=True, resolution=key[0], fps=float(key[1]),
                      bitrate_kbps=5000)
    db.session.commit()


class _GroupCase(unittest.TestCase):
    def setUp(self):
        self.t = make_test_app()
        self.ctx = self.t.app.app_context()
        self.ctx.push()
        self.acct = make_account()

    def tearDown(self):
        self.ctx.pop()
        self.t.cleanup()

    def _group(self, strategy, formats, in_guide=False, scores=None):
        """A group of len(formats) members, member i reporting formats[i]. A None format
        leaves that member untested."""
        scores = scores or [90 - 10 * i for i in range(len(formats))]
        chans = []
        for i, key in enumerate(formats):
            ch = make_channel(self.acct, name=f'Feed {i}')
            if key is not None:
                _tested(ch, key, score=scores[i])
            else:
                ch.health_score = scores[i]
            chans.append(ch)
        db.session.commit()
        grp = make_group(name='FS1', members=chans, in_guide=in_guide,
                         format_strategy=strategy)
        db.session.commit()
        return grp, chans

    def _latest(self, channels):
        from app.routes.channel_tests import _latest_tests_by_channel
        return _latest_tests_by_channel([ch.id for ch in channels])

    def _events(self, group, event_type=None):
        q = ChannelGroupEvent.query.filter_by(group_id=group.id)
        if event_type:
            q = q.filter_by(event_type=event_type)
        return q.order_by(ChannelGroupEvent.id).all()


# ── Layer 2: the lock filters where members are chosen ────────────────────────

class FormatFilterTests(_GroupCase):
    def test_a_locked_group_drops_the_members_that_do_not_match(self):
        grp, chans = self._group(GROUP_FORMAT_MANUAL, [SD, HD, HD])
        grp.set_locked_format(*HD)
        db.session.commit()
        sel = format_eligible_members(grp, recording_members(grp.memberships),
                                      self._latest(chans))
        self.assertEqual([chans[1].id, chans[2].id], [c.id for c in sel.members])
        self.assertEqual([chans[0].id], [c.id for c in sel.filtered])
        self.assertFalse(sel.override)

    def test_an_untested_member_survives_the_filter(self):
        """Unknown is not proven-different - the same call group_format_outliers()
        already makes. Filtering it out would make a never-tested member permanently
        unselectable in a locked group with no health check to prove it either way."""
        grp, chans = self._group(GROUP_FORMAT_MANUAL, [None, SD])
        grp.set_locked_format(*HD)
        db.session.commit()
        sel = format_eligible_members(grp, recording_members(grp.memberships),
                                      self._latest(chans))
        self.assertEqual([chans[0].id], [c.id for c in sel.members])

    def test_an_unlocked_group_filters_nothing(self):
        grp, chans = self._group(GROUP_FORMAT_HIGHEST_SCORE, [SD, HD])
        sel = format_eligible_members(grp, recording_members(grp.memberships),
                                      self._latest(chans))
        self.assertEqual(2, len(sel.members))
        self.assertIsNone(sel.reference)

    def test_unmanaged_never_filters_even_with_a_lock_still_on_the_row(self):
        """§16.2's own copy promises unmanaged "records from whichever member ranks best,
        whatever its format". A lock left behind by a previous strategy must not quietly
        keep filtering after the user has said to stop managing format."""
        grp, chans = self._group(GROUP_FORMAT_UNMANAGED, [SD, HD])
        grp.set_locked_format(*HD)
        db.session.commit()
        sel = format_eligible_members(grp, recording_members(grp.memberships),
                                      self._latest(chans))
        self.assertEqual(2, len(sel.members))
        self.assertFalse(group_manages_format(grp))

    def test_health_check_only_never_filters(self):
        grp, chans = self._group(GROUP_FORMAT_HEALTH_CHECK_ONLY, [SD, HD])
        grp.set_locked_format(*HD)
        db.session.commit()
        sel = format_eligible_members(grp, recording_members(grp.memberships),
                                      self._latest(chans))
        self.assertEqual(2, len(sel.members))

    def test_zero_survivors_is_an_override_that_hands_back_everything(self):
        """§15.2: "that should force the recording to happen but be loud about it". An
        empty list here would be a silent skip, which is the one answer the model forbids."""
        grp, chans = self._group(GROUP_FORMAT_MANUAL, [SD, SD])
        grp.set_locked_format(*HD)
        db.session.commit()
        sel = format_eligible_members(grp, recording_members(grp.memberships),
                                      self._latest(chans))
        self.assertTrue(sel.override)
        self.assertEqual(2, len(sel.members))
        self.assertEqual([], sel.filtered)
        self.assertEqual(HD, sel.reference)

    def test_no_recording_enabled_members_is_not_an_override(self):
        """The user disabling everything is a choice, not the filtered-to-nothing state
        §15.2 alerts about - the two must not collapse into one signal."""
        grp, chans = self._group(GROUP_FORMAT_MANUAL, [SD])
        grp.set_locked_format(*HD)
        for m in grp.memberships:
            m.recording_enabled = False  # participation-write-ok: seeding a fixture state
        db.session.commit()
        sel = format_eligible_members(grp, recording_members(grp.memberships),
                                      self._latest(chans))
        self.assertFalse(sel.override)
        self.assertEqual([], sel.members)

    def test_the_guide_row_serves_the_member_the_recorder_would_pick(self):
        """One definition, or the row names one feed and Record starts another."""
        grp, chans = self._group(GROUP_FORMAT_MANUAL, [SD, HD], in_guide=True,
                                 scores=[99, 50])
        grp.set_locked_format(*HD)
        db.session.commit()
        rows = guide_row_targets(latest_by_channel=self._latest(chans))
        serving = [s for kind, obj, s in rows if kind == 'group' and obj.id == grp.id]
        self.assertEqual([chans[1].id], [s.id for s in serving],
                         'the higher-scored SD member is off the lock and must not serve')


class NoEligibleMemberAlertTests(_GroupCase):
    """§15.2 voice 1: the moment a group's lock leaves nothing eligible."""

    def _open_alerts(self):
        return Alert.query.filter_by(alert_type='GROUP_NO_ELIGIBLE_MEMBER',
                                     dismissed_at=None).all()

    def test_the_alert_fires_once_and_clears_itself(self):
        from app.channel_groups import evaluate_and_reconcile_group
        grp, chans = self._group(GROUP_FORMAT_MANUAL, [SD])
        grp.set_locked_format(*HD)
        db.session.commit()

        evaluate_and_reconcile_group(grp)
        self.assertEqual(1, len(self._open_alerts()))
        evaluate_and_reconcile_group(grp)
        self.assertEqual(1, len(self._open_alerts()), 'must not re-fire every evaluation')

        # The member comes back onto the locked format - the alert clears on its own,
        # exactly as the mismatch alert does. Nothing had to be re-enabled to get here.
        _tested(chans[0], HD)
        evaluate_and_reconcile_group(grp)
        self.assertEqual([], self._open_alerts())

    def test_no_alert_when_the_user_simply_disabled_everything(self):
        from app.channel_groups import evaluate_and_reconcile_group
        grp, chans = self._group(GROUP_FORMAT_MANUAL, [SD])
        grp.set_locked_format(*HD)
        for m in grp.memberships:
            m.recording_enabled = False  # participation-write-ok: seeding a fixture state
        db.session.commit()
        evaluate_and_reconcile_group(grp)
        self.assertEqual([], self._open_alerts())


# ── Layer 1: the standing strategy ───────────────────────────────────────────

class StrategyLockPlanTests(_GroupCase):
    def test_the_four_non_bucket_values_manage_no_lock(self):
        for strategy in (GROUP_FORMAT_HEALTH_CHECK_ONLY, GROUP_FORMAT_HIGHEST_SCORE,
                         GROUP_FORMAT_MANUAL, GROUP_FORMAT_UNMANAGED):
            with self.subTest(strategy=strategy):
                grp, chans = self._group(strategy, [HD, HD, SD])
                plan = strategy_lock_plan(grp, [m.channel for m in grp.memberships],
                                          self._latest(chans))
                self.assertFalse(plan['manages_lock'])
                self.assertIsNone(plan['entry'])

    def test_most_channels_picks_the_bucket_with_the_most_healthy_members(self):
        grp, chans = self._group('most_channels', [HD, SD, SD])
        plan = strategy_lock_plan(grp, [m.channel for m in grp.memberships],
                                  self._latest(chans))
        self.assertTrue(plan['manages_lock'])
        self.assertEqual(SD, plan['entry']['key'])


class ApplyFormatStrategyTests(_GroupCase):
    def test_it_moves_the_lock_and_says_why(self):
        grp, chans = self._group('most_channels', [HD, SD, SD])
        plan = apply_format_strategy(grp)
        self.assertTrue(plan['moved'])
        self.assertEqual(SD, grp.locked_format_key)
        events = self._events(grp, GROUP_FORMAT_STRATEGY_APPLIED)
        self.assertEqual(1, len(events))
        self.assertIn('Most members', events[0].detail)
        self.assertIn('1280x720 @ 30', events[0].detail)

    def test_it_is_silent_when_the_lock_does_not_move(self):
        """A nightly re-evaluation that logs its own agreement with yesterday buries the
        night it disagreed."""
        grp, chans = self._group('most_channels', [HD, SD, SD])
        apply_format_strategy(grp)
        before = len(self._events(grp))
        plan = apply_format_strategy(grp)
        self.assertFalse(plan['moved'])
        self.assertEqual(before, len(self._events(grp)))

    def test_a_non_bucket_strategy_never_touches_the_lock(self):
        grp, chans = self._group(GROUP_FORMAT_HIGHEST_SCORE, [HD, SD, SD])
        apply_format_strategy(grp)
        self.assertIsNone(grp.locked_format_key)
        self.assertEqual([], self._events(grp))

    def test_the_no_winner_state_is_logged_once_not_every_run(self):
        """Right after a database wipe no group has any test history, so every strategy
        returns the no-winner rationale. It has to reach a surface rather than silently
        doing nothing - and then stop repeating itself."""
        grp, chans = self._group('highest_bitrate', [None, None])
        apply_format_strategy(grp)
        blocked = self._events(grp, GROUP_FORMAT_STRATEGY_BLOCKED)
        self.assertEqual(1, len(blocked))
        self.assertIn('No format has enough healthy channels', blocked[0].detail)

        apply_format_strategy(grp)
        self.assertEqual(1, len(self._events(grp, GROUP_FORMAT_STRATEGY_BLOCKED)))

    def test_a_winner_after_a_blocked_run_logs_the_move(self):
        grp, chans = self._group('highest_bitrate', [None, None])
        apply_format_strategy(grp)
        _tested(chans[0], HD)
        apply_format_strategy(grp)
        self.assertEqual(HD, grp.locked_format_key)
        self.assertEqual(1, len(self._events(grp, GROUP_FORMAT_STRATEGY_APPLIED)))

    def test_the_system_group_has_no_strategy_to_apply(self):
        grp, _chans = self._group('most_channels', [HD])
        grp.is_system = True
        db.session.commit()
        self.assertIsNone(apply_format_strategy(grp))


class StrategyRouteTests(_GroupCase):
    def setUp(self):
        super().setUp()
        self.t.app.config['WTF_CSRF_ENABLED'] = False

    def test_setting_the_strategy_applies_it_immediately(self):
        """A standing setting that visibly does nothing until the next run reads as
        broken."""
        grp, _chans = self._group(GROUP_FORMAT_HEALTH_CHECK_ONLY, [HD, SD, SD])
        resp = self.t.client.post(f'/api/channel-groups/{grp.id}/format-strategy',
                                  json={'strategy': 'most_channels'})
        self.assertEqual(200, resp.status_code)
        self.assertTrue(resp.get_json()['lock_moved'])
        db.session.expire_all()
        self.assertEqual(SD, db.session.get(type(grp), grp.id).locked_format_key)

    def test_leaving_manual_clears_the_pin(self):
        """`manual` is the ONLY strategy whose lock belongs to the user. Every other value
        either writes the lock itself or does not use it, and group_reference_key() returns
        the lock ahead of anything derived - so a pin left behind would make "Healthiest
        member's format" go on enforcing yesterday's pin (dev/changelog/756).

        Unreachable before this change: nothing but the default was ever written to
        format_strategy, so no group could leave `manual` at all."""
        grp, _chans = self._group(GROUP_FORMAT_MANUAL, [HD, SD, SD])
        grp.set_locked_format(*HD)
        db.session.commit()
        gid = grp.id

        resp = self.t.client.post(f'/api/channel-groups/{gid}/format-strategy',
                                  json={'strategy': 'highest_score'})
        self.assertEqual(200, resp.status_code)
        db.session.expire_all()
        self.assertIsNone(db.session.get(type(grp), gid).locked_format_key)

    def test_an_engine_strategy_replaces_the_pin_rather_than_inheriting_it(self):
        """Clearing first is what leaves the four bucket-ranking strategies a clean slate:
        they write their own winner immediately after."""
        grp, _chans = self._group(GROUP_FORMAT_MANUAL, [HD, SD, SD])
        grp.set_locked_format(*HD)
        db.session.commit()
        gid = grp.id

        resp = self.t.client.post(f'/api/channel-groups/{gid}/format-strategy',
                                  json={'strategy': 'most_channels'})
        self.assertEqual(200, resp.status_code)
        db.session.expire_all()
        self.assertEqual(SD, db.session.get(type(grp), gid).locked_format_key)

    def test_an_engine_strategy_that_finds_no_winner_leaves_nothing_pinned(self):
        """The half of the rule above that the winning case cannot prove: when the engine
        picks nothing, an inherited pin would go on filtering to a format nothing chose,
        and the group would silently enforce the old value while its own banner said the
        strategy could not choose one."""
        grp, _chans = self._group(GROUP_FORMAT_MANUAL, [None, None, None])
        grp.set_locked_format(*HD)
        db.session.commit()
        gid = grp.id

        resp = self.t.client.post(f'/api/channel-groups/{gid}/format-strategy',
                                  json={'strategy': 'most_channels'})
        self.assertEqual(200, resp.status_code)
        self.assertFalse(resp.get_json()['lock_moved'], 'no bucket had anything to win')
        db.session.expire_all()
        self.assertIsNone(db.session.get(type(grp), gid).locked_format_key)

    def test_staying_on_manual_keeps_the_pin(self):
        grp, _chans = self._group(GROUP_FORMAT_MANUAL, [HD, SD, SD])
        grp.set_locked_format(*HD)
        db.session.commit()
        gid = grp.id

        resp = self.t.client.post(f'/api/channel-groups/{gid}/format-strategy',
                                  json={'strategy': GROUP_FORMAT_MANUAL})
        self.assertEqual(200, resp.status_code)
        db.session.expire_all()
        self.assertEqual(HD, db.session.get(type(grp), gid).locked_format_key)


class RunTriggerTests(_GroupCase):
    """The trigger is keyed on the channels a run tested, not on the job's own group."""

    def test_every_group_with_a_tested_member_is_re_evaluated(self):
        from app.channel_tester import _apply_group_format_strategies
        grp_a, chans_a = self._group('most_channels', [HD, SD, SD])
        grp_b, chans_b = self._group('most_channels', [HD, HD, SD])
        # Only one member of each group was probed, which is what the automatic TV Guide
        # check does for a scheduleless group (dev/changelog/752).
        _apply_group_format_strategies(self.t.app, [chans_a[0].id, chans_b[0].id])
        db.session.expire_all()
        self.assertEqual(SD, db.session.get(type(grp_a), grp_a.id).locked_format_key)
        self.assertEqual(HD, db.session.get(type(grp_b), grp_b.id).locked_format_key)

    def test_a_group_with_no_member_in_the_run_is_left_alone(self):
        from app.channel_tester import _apply_group_format_strategies
        grp, chans = self._group('most_channels', [HD, SD, SD])
        other = make_channel(self.acct, name='Unrelated')
        db.session.commit()
        _apply_group_format_strategies(self.t.app, [other.id])
        db.session.expire_all()
        self.assertIsNone(db.session.get(type(grp), grp.id).locked_format_key)


class RecordStartTests(unittest.TestCase):
    """The safety-critical half: what the recorder actually picks.

    Uses the same no-ffmpeg seam as tests/test_groups_membership.py - a DVR directory
    that does not exist, so start_recording() runs member selection and the handoff check
    and then marks the recording FAILED before spawning anything. No app context is
    pushed here on purpose: make_test_app() already pushed one, and a nested context's
    teardown pulls the connection out from under the outer session."""

    def setUp(self):
        self.t = make_test_app()
        self.acct = make_account()

    def tearDown(self):
        self.t.cleanup()

    def _start(self, rec_id):
        import json
        from unittest import mock
        import app.recorder as recorder
        from app.config import load_config as real_load_config

        def _missing_dvr_cfg(*_a, **_kw):
            cfg = json.loads(json.dumps(real_load_config()))
            cfg['recording']['dvr_output_dir'] = '/nonexistent-format-filter-test'
            return cfg

        with mock.patch.object(recorder, 'load_config', _missing_dvr_cfg), \
             mock.patch.object(recorder, '_handoff_stop_old_recording'):
            recorder.start_recording(self.t.app, rec_id)
        db.session.expire_all()

    def _events(self, rec_id, event_type):
        from app.database import RecordingEvent
        return (RecordingEvent.query
                .filter_by(recording_id=rec_id, event_type=event_type).all())

    def _locked_group(self, formats_and_scores):
        chans = []
        for name, key, score in formats_and_scores:
            ch = make_channel(self.acct, name=name, health_score=score)
            _tested(ch, key)
            chans.append(ch)
        grp = make_group(name='FS1', members=chans, in_guide=False,
                         format_strategy=GROUP_FORMAT_MANUAL)
        grp.set_locked_format(*HD)
        db.session.commit()
        return grp, chans

    def test_record_start_skips_a_member_off_the_locked_format(self):
        """The defect this closes: before dev/changelog/753 the recorder read only the
        health score, so a locked group would happily record its highest-scored member
        even when that member's format was the one the lock excluded."""
        from tests.support.seed import make_recording
        from app.database import Recording
        grp, chans = self._locked_group([('SD Best', SD, 99), ('HD Worse', HD, 40)])
        rec = make_recording(status='SCHEDULED', group_id=grp.id, name='group_rec')
        db.session.commit()

        self._start(rec.id)
        self.assertEqual(chans[1].id, db.session.get(Recording, rec.id).channel_id,
                         'the higher-scored SD member is off the lock')

    def test_a_group_with_nothing_matching_records_anyway_and_says_so(self):
        """15.2's override, and the two voices that land at record time."""
        from tests.support.seed import make_recording
        from app.database import Recording, RECORDING_FORMAT_OVERRIDE, Alert
        grp, chans = self._locked_group([('SD Only', SD, 80)])
        rec = make_recording(status='SCHEDULED', group_id=grp.id, name='group_rec')
        db.session.commit()

        self._start(rec.id)
        self.assertEqual(chans[0].id, db.session.get(Recording, rec.id).channel_id,
                         'principle 2: the recording runs rather than being skipped')
        events = self._events(rec.id, RECORDING_FORMAT_OVERRIDE)
        self.assertEqual(1, len(events), 'the fact must survive onto the artifact')
        self.assertIn('1280x720 @ 30', events[0].detail)
        self.assertIn('1920x1080 @ 60', events[0].detail)
        self.assertEqual(1, len(Alert.query.filter_by(
            alert_type='RECORDING_FORMAT_OVERRIDE').all()))

    def test_no_override_event_when_the_lock_is_satisfied(self):
        from tests.support.seed import make_recording
        from app.database import RECORDING_FORMAT_OVERRIDE
        grp, _chans = self._locked_group([('HD', HD, 80)])
        rec = make_recording(status='SCHEDULED', group_id=grp.id, name='group_rec')
        db.session.commit()

        self._start(rec.id)
        self.assertEqual([], self._events(rec.id, RECORDING_FORMAT_OVERRIDE))


class MembershipUntouchedTests(_GroupCase):
    """§4.1, restated as a guard: the filter is a READ. If any of this work can write a
    participation column, the auto-disable spiral the model was built to remove is back."""

    def test_nothing_here_writes_a_participation_switch(self):
        grp, chans = self._group('most_channels', [HD, SD, SD])
        apply_format_strategy(grp)
        format_eligible_members(grp, recording_members(grp.memberships),
                                self._latest(chans))
        db.session.expire_all()
        rows = ChannelGroupMember.query.filter_by(group_id=grp.id).all()
        self.assertTrue(all(m.recording_enabled for m in rows),
                        'the SD members are filtered at selection, never unticked')
        self.assertTrue(all(m.test_enabled for m in rows))


if __name__ == '__main__':
    unittest.main()
