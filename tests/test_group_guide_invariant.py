"""Tier 2 - DESIGN-channel-groups-model.md §15, the guide invariant, and §14.1's
promotion walkthrough (`dev/changelog/757`).

    A group in the TV Guide always has at least one recording-enabled member.

This is the only thing in the channel-group model that is ENFORCED rather than warned
about, and until this changelog nothing enforced it: a user could turn Recording off on
the last enabled member and be left with a guide row that looks perfectly normal and
cannot produce a file - the exact failure mode this app exists to refuse.

What these tests hold down:

  * **All four destroyer paths take the SAME gate.** The single participation switch, the
    bulk switch, member removal, and apply-format-plan's remove option each go through
    `routes/channel_groups.py::_invariant_gate`, and each has its own class below. A guard
    a bulk action can walk around is not a guard, and selecting every row is the easiest
    way there is to empty a group's recording-enabled set.
  * **The guide row never moves silently** (§4.5, `dev/changelog/764`). Every path that
    puts the group in the guide or takes it out - the hand toggle, the promotion
    walkthrough's last step, the confirmed demotion - writes a `GROUP_GUIDE_ADDED` or
    `GROUP_GUIDE_REMOVED` event through the one writer, `channel_groups.py::
    log_guide_change()`, with the direction in the type and the cause in the detail. A
    refused move writes nothing: no row appeared, so there is no state change to claim.
  * **Adding to the guide with nothing enabled is blocked** (breach path 1) - the one
    place in this model where blocking is right, because §14.1's walkthrough makes the fix
    one click away.
  * **Unticking the last enabled member is confirmed, then carried out** (breach path 2):
    409 with the facts the dialog must name, and on `confirm` the switch moves, the guide
    row goes, and the scheduled recordings counting on it are cancelled - in ONE commit,
    because a half-applied breach is the very state being refused.
  * **A capture running right now refuses outright, and no confirm overrides it** (§15.1).
    Killing a live recording as a side effect of a checkbox is what product principle 2
    forbids; aborting one is its own deliberate verb.
  * **The no-human-present path reports instead of acting** (breach path 3). A bulk delete
    of channels the provider dropped cannot prompt, so the group KEEPS its guide row and
    the state is said out loud three ways - a `GROUP_GUIDE_BROKEN` event, an ERROR alert,
    and the group page's non-mutable banner. Quietly pulling a row overnight is the silent
    behavior §15 refuses.
  * **A clone never lands in the guide.** Its members take the model defaults, so Recording
    is off on every one of them and the row would have nothing behind it at the moment it
    was created. The refusal is reported (`guide_refused`), never swallowed.
  * **`POST .../promote` is one unit** (§14.1), and its ordering is load bearing: the pin
    is written before the strategy is applied, and the guide row is added last, after the
    switches - so the invariant is satisfied by construction rather than checked after.
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tests.support.app import make_test_app  # noqa: E402
from tests.support.seed import (make_account, make_channel, make_group,  # noqa: E402
                                make_channel_test, make_recording)


def _json(resp):
    return resp.get_json() or {}


class _Base(unittest.TestCase):
    def setUp(self):
        self.t = make_test_app()
        # These are all API POSTs and every one of them is CSRF-protected app-wide; the
        # token itself is not what any of this is about (same as StrategyRouteTests).
        self.t.app.config['WTF_CSRF_ENABLED'] = False
        self.client = self.t.client
        self.ctx = self.t.app.app_context()
        self.ctx.push()
        self.acct = make_account()

    def tearDown(self):
        self.ctx.pop()
        self.t.cleanup()

    def _part(self, group, channel, enabled, **extra):
        body = {'channel_id': channel.id, 'field': 'recording_enabled',
                'enabled': enabled}
        body.update(extra)
        return self.client.post(
            f'/api/channel-groups/{group.id}/members/participation', json=body)

    def _bulk(self, group, channels, enabled, **extra):
        body = {'channel_ids': [c.id for c in channels], 'field': 'recording_enabled',
                'enabled': enabled}
        body.update(extra)
        return self.client.post(
            f'/api/channel-groups/{group.id}/members/participation/bulk', json=body)

    def _remove(self, group, channel, **extra):
        body = {'channel_id': channel.id}
        body.update(extra)
        return self.client.post(
            f'/api/channel-groups/{group.id}/members/remove', json=body)


class GuideAddIsBlockedTests(_Base):
    """§15 breach path 1. A guide row exists to be recorded from."""

    def test_adding_a_group_with_nothing_enabled_is_refused(self):
        from app import db
        ch = make_channel(self.acct, name='Feed')
        grp = make_group(name='G', members=[ch], in_guide=False, recording=False)
        db.session.commit()

        resp = self.client.post(f'/api/channel-groups/{grp.id}/guide-toggle')
        self.assertEqual(409, resp.status_code, _json(resp))
        self.assertTrue(_json(resp)['needs_recording_member'],
                        'the client needs to know WHICH refusal this is, so it can offer '
                        'the walkthrough rather than only printing a message')
        db.session.expire_all()
        self.assertFalse(grp.in_guide)

    def test_adding_a_group_with_one_enabled_member_succeeds(self):
        from app import db
        ch = make_channel(self.acct, name='Feed')
        grp = make_group(name='G', members=[ch], in_guide=False, recording=True)
        db.session.commit()

        resp = self.client.post(f'/api/channel-groups/{grp.id}/guide-toggle')
        self.assertEqual(200, resp.status_code, _json(resp))
        db.session.expire_all()
        self.assertTrue(grp.in_guide)

    def test_taking_a_group_out_of_the_guide_is_never_refused(self):
        """Removing a row loses nothing and is not a breach - only ADDING one is."""
        from app import db
        ch = make_channel(self.acct, name='Feed')
        grp = make_group(name='G', members=[ch], in_guide=True, recording=False)
        db.session.commit()

        resp = self.client.post(f'/api/channel-groups/{grp.id}/guide-toggle')
        self.assertEqual(200, resp.status_code, _json(resp))
        db.session.expire_all()
        self.assertFalse(grp.in_guide)


class LastMemberConfirmTests(_Base):
    """§15 breach path 2 - confirmed, then carried out."""

    def _setup(self, in_guide=True, members=1):
        from app import db
        chans = [make_channel(self.acct, name=f'Feed {i}') for i in range(members)]
        grp = make_group(name='G', members=chans, in_guide=in_guide, recording=True)
        db.session.commit()
        return grp, chans

    def test_unticking_the_last_member_is_refused_without_confirm(self):
        from app import db
        grp, chans = self._setup()
        resp = self._part(grp, chans[0], False)
        self.assertEqual(409, resp.status_code, _json(resp))
        facts = _json(resp)['confirm_required']
        self.assertEqual('G', facts['group_name'])
        self.assertTrue(facts['in_guide'])
        self.assertEqual([chans[0].id], [x['channel_id'] for x in facts['losing']],
                         'the dialog names who is going off, not just that something is')
        db.session.expire_all()
        self.assertTrue(grp.memberships[0].recording_enabled,
                        'a refused action changes nothing')
        self.assertTrue(grp.in_guide)

    def test_confirming_moves_the_switch_and_takes_the_guide_row(self):
        from app import db
        from app.database import ChannelGroupEvent, GROUP_GUIDE_REMOVED
        grp, chans = self._setup()
        resp = self._part(grp, chans[0], False, confirm=True)
        self.assertEqual(200, resp.status_code, _json(resp))
        self.assertTrue(_json(resp)['left_guide'])
        db.session.expire_all()
        self.assertFalse(grp.memberships[0].recording_enabled)
        self.assertFalse(grp.in_guide)
        ev = ChannelGroupEvent.query.filter_by(
            group_id=grp.id, event_type=GROUP_GUIDE_REMOVED).one()
        self.assertIn('TV Guide', ev.detail)

    def test_it_cancels_the_scheduled_recordings_counting_on_the_row(self):
        from app import db
        from app.database import Recording, REC_STATUS_ABORTED
        grp, chans = self._setup()
        rec = make_recording(status='SCHEDULED', group_id=grp.id, channel_id=chans[0].id)
        db.session.commit()

        resp = self._part(grp, chans[0], False, confirm=True)
        self.assertEqual(200, resp.status_code, _json(resp))
        self.assertEqual(1, _json(resp)['cancelled_recordings'])
        db.session.expire_all()
        self.assertEqual(REC_STATUS_ABORTED, db.session.get(Recording, rec.id).status)

    def test_the_facts_name_the_scheduled_recordings_before_it_happens(self):
        grp, chans = self._setup()
        make_recording(status='SCHEDULED', group_id=grp.id, channel_id=chans[0].id)
        from app import db
        db.session.commit()

        resp = self._part(grp, chans[0], False)
        self.assertEqual(409, resp.status_code)
        self.assertEqual(1, _json(resp)['confirm_required']['scheduled_count'],
                         'the cancellation is named in the prompt, '
                         'not discovered afterwards')

    def test_a_group_not_in_the_guide_still_confirms_but_demotes_nothing(self):
        from app import db
        grp, chans = self._setup(in_guide=False)
        refused = self._part(grp, chans[0], False)
        self.assertEqual(409, refused.status_code,
                         'the user is still turning off the last one and is still told so')
        self.assertFalse(_json(refused)['confirm_required']['in_guide'])

        resp = self._part(grp, chans[0], False, confirm=True)
        self.assertEqual(200, resp.status_code, _json(resp))
        self.assertFalse(_json(resp)['left_guide'])
        db.session.expire_all()
        self.assertFalse(grp.memberships[0].recording_enabled)

    def test_unticking_a_member_that_is_not_the_last_is_not_gated(self):
        grp, chans = self._setup(members=2)
        resp = self._part(grp, chans[0], False)
        self.assertEqual(200, resp.status_code, _json(resp))
        self.assertFalse(_json(resp)['left_guide'])

    def test_turning_recording_ON_is_never_gated(self):
        """Only Recording-OFF can empty the set. Turning one on adds to it."""
        from app import db
        chans = [make_channel(self.acct, name='Feed')]
        grp = make_group(name='G', members=chans, in_guide=False, recording=False,
                         format_strategy='highest_score')
        db.session.commit()
        resp = self._part(grp, chans[0], True)
        self.assertEqual(200, resp.status_code, _json(resp))

    def test_the_health_check_switch_is_never_gated(self):
        """It has no bearing on whether the group can produce a file."""
        grp, chans = self._setup()
        resp = self.client.post(
            f'/api/channel-groups/{grp.id}/members/participation',
            json={'channel_id': chans[0].id, 'field': 'test_enabled', 'enabled': False})
        self.assertEqual(200, resp.status_code, _json(resp))


class BulkTakesTheSameGateTests(_Base):
    """A guard a bulk action can walk around is not a guard."""

    def _setup(self, members=3):
        from app import db
        chans = [make_channel(self.acct, name=f'Feed {i}') for i in range(members)]
        grp = make_group(name='G', members=chans, in_guide=True, recording=True)
        db.session.commit()
        return grp, chans

    def test_selecting_every_member_is_refused_without_confirm(self):
        from app import db
        grp, chans = self._setup()
        resp = self._bulk(grp, chans, False)
        self.assertEqual(409, resp.status_code, _json(resp))
        self.assertEqual(3, len(_json(resp)['confirm_required']['losing']))
        db.session.expire_all()
        self.assertTrue(all(m.recording_enabled for m in grp.memberships))

    def test_confirming_the_bulk_action_demotes_exactly_once(self):
        from app import db
        from app.database import ChannelGroupEvent, GROUP_GUIDE_REMOVED
        grp, chans = self._setup()
        resp = self._bulk(grp, chans, False, confirm=True)
        self.assertEqual(200, resp.status_code, _json(resp))
        self.assertEqual(3, _json(resp)['moved'])
        self.assertTrue(_json(resp)['left_guide'])
        db.session.expire_all()
        self.assertFalse(grp.in_guide)
        self.assertEqual(1, ChannelGroupEvent.query.filter_by(
            group_id=grp.id, event_type=GROUP_GUIDE_REMOVED).count(),
            'one demotion, however many switches moved to cause it')

    def test_selecting_a_subset_is_not_gated(self):
        grp, chans = self._setup()
        resp = self._bulk(grp, chans[:2], False)
        self.assertEqual(200, resp.status_code, _json(resp))
        self.assertFalse(_json(resp)['left_guide'])

    def test_members_already_off_do_not_count_as_being_taken_away(self):
        """`losing` is what actually moves. Including an already-off member in the
        selection is not something that happens, so it cannot cause a breach."""
        from app import db
        chans = [make_channel(self.acct, name=f'Feed {i}') for i in range(2)]
        grp = make_group(name='G', members=chans, in_guide=True, recording=True,
                         disabled=[chans[0].id])
        db.session.commit()
        resp = self._bulk(grp, [chans[0]], False)
        # chans[1] is still enabled, so nothing is emptied and nothing is gated.
        self.assertEqual(200, resp.status_code, _json(resp))


class RemovalTakesTheSameGateTests(_Base):
    """§15 breach path 3, the half where a human IS present."""

    def test_removing_the_last_enabled_member_is_refused_without_confirm(self):
        from app import db
        ch = make_channel(self.acct, name='Feed')
        grp = make_group(name='G', members=[ch], in_guide=True, recording=True)
        db.session.commit()

        resp = self._remove(grp, ch)
        self.assertEqual(409, resp.status_code, _json(resp))
        db.session.expire_all()
        self.assertEqual(1, len(list(grp.memberships)), 'a refused removal removes nothing')

    def test_confirming_removes_the_member_and_the_guide_row_together(self):
        from app import db
        ch = make_channel(self.acct, name='Feed')
        grp = make_group(name='G', members=[ch], in_guide=True, recording=True)
        db.session.commit()

        resp = self._remove(grp, ch, confirm=True)
        self.assertEqual(200, resp.status_code, _json(resp))
        self.assertTrue(_json(resp)['left_guide'])
        db.session.expire_all()
        self.assertEqual(0, len(list(grp.memberships)))
        self.assertFalse(grp.in_guide)

    def test_removing_a_member_that_was_not_enabled_is_not_gated(self):
        from app import db
        chans = [make_channel(self.acct, name=f'Feed {i}') for i in range(2)]
        grp = make_group(name='G', members=chans, in_guide=True, recording=True,
                         disabled=[chans[0].id])
        db.session.commit()
        resp = self._remove(grp, chans[0])
        self.assertEqual(200, resp.status_code, _json(resp))


class LiveRecordingRefusesOutrightTests(_Base):
    """§15.1. Scheduled and in-progress recordings are not the same thing."""

    def _setup(self, status):
        from app import db
        ch = make_channel(self.acct, name='Feed')
        grp = make_group(name='G', members=[ch], in_guide=True, recording=True)
        make_recording(status=status, group_id=grp.id, channel_id=ch.id, name='The Game')
        db.session.commit()
        return grp, ch

    def test_a_capture_in_progress_refuses_the_switch(self):
        grp, ch = self._setup('IN_PROGRESS')
        resp = self._part(grp, ch, False)
        self.assertEqual(409, resp.status_code, _json(resp))
        self.assertIn('recording_in_progress', _json(resp))
        self.assertNotIn('confirm_required', _json(resp),
                         'this one is not a question - offering a confirm would make it one')

    def test_confirm_does_NOT_override_a_live_capture(self):
        """The whole point of §15.1: no dialog makes killing a live capture acceptable."""
        from app import db
        grp, ch = self._setup('IN_PROGRESS')
        resp = self._part(grp, ch, False, confirm=True)
        self.assertEqual(409, resp.status_code, _json(resp))
        self.assertIn('recording_in_progress', _json(resp))
        db.session.expire_all()
        self.assertTrue(grp.memberships[0].recording_enabled)
        self.assertTrue(grp.in_guide)

    def test_the_refusal_names_the_recording_so_the_user_can_go_abort_it(self):
        grp, ch = self._setup('IN_PROGRESS')
        info = _json(self._part(grp, ch, False))['recording_in_progress']
        self.assertEqual('The Game', info['name'])
        self.assertTrue(info['recording_id'])

    def test_a_concatenating_recording_blocks_too(self):
        """RESTART_BLOCKING_STATUSES is the app's one answer to "a capture is under way",
        and concatenation is the step that produces the final file."""
        grp, ch = self._setup('CONCATENATING')
        self.assertEqual(409, self._part(grp, ch, False).status_code)

    def test_a_bulk_action_cannot_walk_around_the_live_check_either(self):
        grp, ch = self._setup('IN_PROGRESS')
        resp = self._bulk(grp, [ch], False, confirm=True)
        self.assertEqual(409, resp.status_code, _json(resp))
        self.assertIn('recording_in_progress', _json(resp))

    def test_a_finished_recording_does_not_block(self):
        grp, ch = self._setup('COMPLETED')
        resp = self._part(grp, ch, False)
        self.assertEqual(409, resp.status_code)
        self.assertIn('confirm_required', _json(resp),
                      'a terminal recording is not a capture under way - this is the '
                      'ordinary confirm, not the refusal')


class NoHumanPresentTests(_Base):
    """§15 breach path 3's other half: report, never act."""

    def test_a_bulk_missing_delete_leaves_the_row_and_raises_an_alert(self):
        from datetime import datetime, timedelta
        from app import db
        from app.database import (Alert, ChannelGroupEvent, GROUP_GUIDE_BROKEN)
        # missing_channels_query(): last_seen_at older than the cutoff AND older than the
        # account's own last sync - i.e. the provider stopped listing it, not that we
        # stopped asking.
        old = datetime.utcnow() - timedelta(days=365)
        self.acct.last_sync_at = datetime.utcnow()
        ch = make_channel(self.acct, name='Gone', last_seen_at=old)
        grp = make_group(name='G', members=[ch], in_guide=True, recording=True)
        db.session.commit()

        resp = self.client.post('/channels/missing-delete', json={'group_id': grp.id})
        self.assertEqual(200, resp.status_code, _json(resp))
        self.assertEqual(1, _json(resp)['deleted_count'])
        db.session.expire_all()
        # The row STAYS. Quietly pulling a guide row overnight is the silent behavior
        # §15 refuses - the user is told instead, and fixes it themselves.
        self.assertTrue(grp.in_guide)
        self.assertEqual(1, _json(resp)['broken_guide_groups'])
        self.assertEqual(1, ChannelGroupEvent.query.filter_by(
            group_id=grp.id, event_type=GROUP_GUIDE_BROKEN).count())
        self.assertEqual(1, Alert.query.filter_by(
            alert_type='GROUP_GUIDE_NO_RECORDING_MEMBER').count())

    def test_a_group_that_keeps_an_enabled_member_is_not_reported(self):
        from datetime import datetime, timedelta
        from app import db
        from app.database import Alert
        # missing_channels_query(): last_seen_at older than the cutoff AND older than the
        # account's own last sync - i.e. the provider stopped listing it, not that we
        # stopped asking.
        old = datetime.utcnow() - timedelta(days=365)
        self.acct.last_sync_at = datetime.utcnow()
        gone = make_channel(self.acct, name='Gone', last_seen_at=old)
        keeps = make_channel(self.acct, name='Fine')
        grp = make_group(name='G', members=[gone, keeps], in_guide=True, recording=True)
        db.session.commit()

        resp = self.client.post('/channels/missing-delete', json={'group_id': grp.id})
        self.assertEqual(200, resp.status_code, _json(resp))
        self.assertEqual(0, _json(resp)['broken_guide_groups'])
        self.assertEqual(0, Alert.query.filter_by(
            alert_type='GROUP_GUIDE_NO_RECORDING_MEMBER').count())

    def test_the_page_renders_the_broken_state_while_it_is_true(self):
        """The banner is decided server-side with every other one, so the page and the
        alert cannot disagree about whether the group is broken."""
        from app import db
        from app.routes.channel_groups import group_detail_rows
        ch = make_channel(self.acct, name='Feed')
        grp = make_group(name='G', members=[ch], in_guide=True, recording=False)
        db.session.commit()
        warnings = group_detail_rows(grp, None)['warnings']
        self.assertTrue(warnings['guide_broken'])

        grp.memberships[0].recording_enabled = True  # participation-write-ok: test fixture
        db.session.commit()
        self.assertFalse(group_detail_rows(grp, None)['warnings']['guide_broken'],
                         'it clears on its own once a member is switched back on')


class CloneNeverJoinsTheGuideTests(_Base):
    def test_a_clone_asking_for_the_guide_is_refused_and_told_so(self):
        from app import db
        ch = make_channel(self.acct, name='Feed')
        src = make_group(name='Src', members=[ch], in_guide=True, recording=True,
                         format_strategy='highest_score')
        db.session.commit()

        resp = self.client.post(f'/api/channel-groups/{src.id}/clone',
                                json={'name': 'Copy', 'channel_ids': [ch.id],
                                      'in_guide': True,
                                      'format_strategy': 'highest_score'})
        self.assertEqual(200, resp.status_code, _json(resp))
        self.assertTrue(_json(resp)['guide_refused'])
        from app.database import ChannelGroup
        new = ChannelGroup.query.filter_by(name='Copy').first()
        self.assertFalse(new.in_guide)
        self.assertFalse(any(m.recording_enabled for m in new.memberships),
                         'which is exactly why the guide row would have been a lie')


class PromoteTests(_Base):
    """§14.1's walkthrough, as one server-side unit."""

    def _group(self, in_guide=False):
        from app import db
        chans = [make_channel(self.acct, name=f'Feed {i}') for i in range(3)]
        # Two at 1920x1080@60, one at 1280x720@60, so "matching" and "all" differ.
        for ch, res in zip(chans, ['1920x1080', '1920x1080', '1280x720']):
            make_channel_test(ch, all_null=False, status='COMPLETED', connected=True,
                              resolution=res, fps=60.0, bitrate_kbps=5000)
        grp = make_group(name='G', members=chans, in_guide=in_guide, recording=False)
        db.session.commit()
        return grp, chans

    def _promote(self, grp, **body):
        payload = {'strategy': 'most_channels', 'enable': 'matching',
                   'unmatched_checks': 'keep', 'add_to_guide': False}
        payload.update(body)
        return self.client.post(f'/api/channel-groups/{grp.id}/promote', json=payload)

    def test_it_sets_the_strategy_and_switches_on_the_matching_members(self):
        from app import db
        grp, chans = self._group()
        resp = self._promote(grp)
        self.assertEqual(200, resp.status_code, _json(resp))
        self.assertEqual('most_channels', _json(resp)['format_strategy'])
        self.assertEqual(2, _json(resp)['enabled'])
        db.session.expire_all()
        by_id = {m.channel_id: m for m in grp.memberships}
        self.assertTrue(by_id[chans[0].id].recording_enabled)
        self.assertTrue(by_id[chans[1].id].recording_enabled)
        self.assertFalse(by_id[chans[2].id].recording_enabled,
                         '"matching" is the conservative choice and must stay conservative')

    def test_enable_all_switches_on_the_mismatched_member_too(self):
        from app import db
        grp, chans = self._group()
        self._promote(grp, enable='all')
        db.session.expire_all()
        self.assertTrue(all(m.recording_enabled for m in grp.memberships))

    def test_it_can_add_the_group_to_the_guide_in_the_same_call(self):
        from app import db
        grp, _ = self._group()
        resp = self._promote(grp, add_to_guide=True)
        self.assertTrue(_json(resp)['joined_guide'])
        db.session.expire_all()
        self.assertTrue(grp.in_guide)

    def test_it_refuses_to_add_a_guide_row_when_nothing_got_switched_on(self):
        """§15 satisfied by CONSTRUCTION: the flag is written after the switches and only
        if something is actually on. Otherwise the walkthrough would be a fifth way to
        breach the invariant."""
        from app import db
        grp, _ = self._group()
        resp = self._promote(grp, enable='none', add_to_guide=True)
        self.assertEqual(200, resp.status_code, _json(resp))
        self.assertFalse(_json(resp)['joined_guide'])
        db.session.expire_all()
        self.assertFalse(grp.in_guide)

    def test_the_pending_member_from_the_click_is_switched_on_too(self):
        """The walkthrough completes the action that opened it - `enable_channel_ids` is
        whatever switch or selection the user actually clicked."""
        from app import db
        grp, chans = self._group()
        self._promote(grp, enable='none', enable_channel_ids=[chans[2].id])
        db.session.expire_all()
        by_id = {m.channel_id: m for m in grp.memberships}
        self.assertTrue(by_id[chans[2].id].recording_enabled)

    def test_stopping_checks_on_the_unmatched_leaves_the_untested_alone(self):
        """Unknown is not proven-different. A member with no test survives the
        comparison, or it ends up recordable with no data behind it forever."""
        from app import db
        grp, chans = self._group()
        untested = make_channel(self.acct, name='Untested')
        self.client.post(f'/api/channel-groups/{grp.id}/members',
                         json={'channel_ids': [untested.id]})
        self._promote(grp, unmatched_checks='stop')
        db.session.expire_all()
        by_id = {m.channel_id: m for m in grp.memberships}
        self.assertFalse(by_id[chans[2].id].test_enabled, 'measured and different')
        self.assertTrue(by_id[untested.id].test_enabled, 'never measured at all')

    def test_manual_pins_the_format_it_was_given(self):
        from app import db
        grp, _ = self._group()
        resp = self._promote(grp, strategy='manual', resolution='1280x720', fps=60)
        self.assertEqual(200, resp.status_code, _json(resp))
        db.session.expire_all()
        self.assertEqual(('1280x720', 60), grp.locked_format_key)

    def test_manual_without_a_format_is_a_400(self):
        grp, _ = self._group()
        self.assertEqual(400, self._promote(grp, strategy='manual').status_code)

    def test_health_check_only_is_refused_as_a_promotion(self):
        """Promoting means becoming a recording source. Offering "not a recording source"
        as the answer would be offering to do nothing."""
        grp, _ = self._group()
        self.assertEqual(400, self._promote(grp, strategy='health_check_only').status_code)

    def test_an_unknown_strategy_is_refused(self):
        grp, _ = self._group()
        self.assertEqual(400, self._promote(grp, strategy='cheapest').status_code)

    def test_an_unknown_enable_mode_is_refused(self):
        """Enforcement lives server-side, never in whichever control posted it."""
        grp, _ = self._group()
        self.assertEqual(400, self._promote(grp, enable='everything').status_code)

    def test_every_switch_it_moves_is_logged(self):
        """It writes through set_participation() like every other participation writer,
        so the Activity Timeline reads the same whichever control was used."""
        from app.database import ChannelGroupEvent, GROUP_MEMBER_PARTICIPATION
        grp, _ = self._group()
        self._promote(grp, enable='all')
        self.assertEqual(3, ChannelGroupEvent.query.filter_by(
            group_id=grp.id, event_type=GROUP_MEMBER_PARTICIPATION).count())

    def test_joining_the_guide_here_is_logged_like_the_hand_toggle(self):
        """The walkthrough's last step moves the same state the toggle does, so it leaves
        the same trace - one writer, one event (dev/changelog/764)."""
        from app.database import ChannelGroupEvent, GROUP_GUIDE_ADDED
        grp, _ = self._group()
        self._promote(grp, add_to_guide=True)
        ev = ChannelGroupEvent.query.filter_by(
            group_id=grp.id, event_type=GROUP_GUIDE_ADDED).one()
        self.assertIn('promoted', ev.detail,
                      'the type carries the direction, the detail carries the cause')

    def test_a_refused_guide_row_logs_nothing(self):
        """No row appeared, so there is nothing for the timeline to say. An event written
        anyway would be the log claiming a state change that did not happen."""
        from app.database import ChannelGroupEvent, GROUP_GUIDE_ADDED
        grp, _ = self._group()
        self._promote(grp, enable='none', add_to_guide=True)
        self.assertEqual(0, ChannelGroupEvent.query.filter_by(
            group_id=grp.id, event_type=GROUP_GUIDE_ADDED).count())


class ApplyFormatPlanTakesTheSameGateTests(_Base):
    """§15's fourth destroyer path: apply-format-plan with `non_matching='remove'`.

    Structurally the same breach as removing members by hand - it deletes membership rows
    in bulk - and it is the path whose gate call was added last, so it is the one a
    regression would most plausibly hit. Mirrors RemovalTakesTheSameGateTests above.
    """

    def _group(self):
        """An in-guide group whose ONLY recording-enabled member is the format outlier.

        Two members at 1280x720@30 with Recording off make `most_channels` pick that
        format, which leaves the single 1920x1080@60 member as the one to remove - and it
        is the only one the group can record from. The group keeps members afterwards, so
        the route's "this would remove every member" check does not fire: that check is a
        different and weaker question than the invariant's.
        """
        from app import db
        hd = make_channel(self.acct, name='HD')
        sd1 = make_channel(self.acct, name='SD 1')
        sd2 = make_channel(self.acct, name='SD 2')
        for ch, res, fps in ((hd, '1920x1080', 60.0), (sd1, '1280x720', 30.0),
                             (sd2, '1280x720', 30.0)):
            make_channel_test(ch, all_null=False, status='COMPLETED', connected=True,
                              resolution=res, fps=fps, bitrate_kbps=5000)
        grp = make_group(name='G', members=[hd, sd1, sd2], in_guide=True,
                         recording=True, disabled=[sd1.id, sd2.id])
        db.session.commit()
        return grp, hd

    def _apply(self, group, **extra):
        body = {'strategy': 'most_channels', 'non_matching': 'remove'}
        body.update(extra)
        return self.client.post(
            f'/api/channel-groups/{group.id}/apply-format-plan', json=body)

    def test_removing_the_last_enabled_member_is_refused_without_confirm(self):
        from app import db
        grp, hd = self._group()
        resp = self._apply(grp)
        self.assertEqual(409, resp.status_code, _json(resp))
        self.assertIn('confirm_required', _json(resp))
        db.session.expire_all()
        self.assertEqual(3, len(list(grp.memberships)), 'a refused plan removes nothing')
        self.assertTrue(grp.in_guide)

    def test_confirming_removes_the_member_and_the_guide_row_together(self):
        from app import db
        grp, hd = self._group()
        resp = self._apply(grp, confirm=True)
        self.assertEqual(200, resp.status_code, _json(resp))
        self.assertTrue(_json(resp)['left_guide'])
        db.session.expire_all()
        self.assertNotIn(hd.id, [m.channel_id for m in grp.memberships])
        self.assertEqual(2, len(list(grp.memberships)))
        self.assertFalse(grp.in_guide)

    def test_the_demotion_is_logged_like_every_other_one(self):
        from app.database import ChannelGroupEvent, GROUP_GUIDE_REMOVED
        grp, _ = self._group()
        self._apply(grp, confirm=True)
        self.assertEqual(1, ChannelGroupEvent.query.filter_by(
            group_id=grp.id, event_type=GROUP_GUIDE_REMOVED).count())

    def test_a_live_capture_refuses_outright_and_confirm_does_not_override(self):
        """§15.1 again. A plan is not a reason to kill a recording either."""
        from app import db
        grp, hd = self._group()
        make_recording(status='IN_PROGRESS', group_id=grp.id, channel_id=hd.id,
                       name='The Game')
        db.session.commit()

        resp = self._apply(grp, confirm=True)
        self.assertEqual(409, resp.status_code, _json(resp))
        self.assertIn('recording_in_progress', _json(resp))
        self.assertNotIn('confirm_required', _json(resp))
        db.session.expire_all()
        self.assertEqual(3, len(list(grp.memberships)))
        self.assertTrue(grp.in_guide)

    def test_the_keep_path_is_never_gated(self):
        """'keep' removes nothing, so it cannot empty anything - the lock filters instead
        (DESIGN-channel-groups-model.md 4.1)."""
        from app import db
        grp, _ = self._group()
        resp = self._apply(grp, non_matching='keep')
        self.assertEqual(200, resp.status_code, _json(resp))
        db.session.expire_all()
        self.assertEqual(3, len(list(grp.memberships)))
        self.assertTrue(grp.in_guide)


class GuideMembershipIsLoggedTests(_Base):
    """The group's guide row is its most visible state, so the Activity Timeline is not
    allowed to be silent about it moving (§4.5, dev/changelog/764)."""

    def _events(self, group, event_type):
        from app.database import ChannelGroupEvent
        return ChannelGroupEvent.query.filter_by(
            group_id=group.id, event_type=event_type).all()

    def test_adding_by_hand_writes_the_added_event(self):
        from app import db
        from app.database import GROUP_GUIDE_ADDED
        ch = make_channel(self.acct, name='Feed')
        grp = make_group(name='G', members=[ch], in_guide=False, recording=True)
        db.session.commit()

        self.client.post(f'/api/channel-groups/{grp.id}/guide-toggle')
        events = self._events(grp, GROUP_GUIDE_ADDED)
        self.assertEqual(1, len(events))
        self.assertTrue(events[0].detail)
        self.assertIsNone(events[0].channel_id,
                          'a fact about the group as a whole carries no member')

    def test_removing_by_hand_writes_the_removed_event(self):
        from app import db
        from app.database import GROUP_GUIDE_REMOVED
        ch = make_channel(self.acct, name='Feed')
        grp = make_group(name='G', members=[ch], in_guide=True, recording=True)
        db.session.commit()

        self.client.post(f'/api/channel-groups/{grp.id}/guide-toggle')
        events = self._events(grp, GROUP_GUIDE_REMOVED)
        self.assertEqual(1, len(events))

    def test_a_hand_removal_does_not_claim_the_invariant_demoted_it(self):
        """One type for one visible state change, with the CAUSE in the detail - so the
        hand toggle must not inherit the demotion's "no member is switched on" wording,
        which would be false here."""
        from app import db
        from app.database import GROUP_GUIDE_REMOVED
        ch = make_channel(self.acct, name='Feed')
        grp = make_group(name='G', members=[ch], in_guide=True, recording=True)
        db.session.commit()

        self.client.post(f'/api/channel-groups/{grp.id}/guide-toggle')
        detail = self._events(grp, GROUP_GUIDE_REMOVED)[0].detail
        self.assertNotIn('switched on for recording', detail)

    def test_a_refused_add_writes_nothing(self):
        """§15 breach path 1 refuses, so no row appeared and nothing happened to log."""
        from app import db
        from app.database import GROUP_GUIDE_ADDED
        ch = make_channel(self.acct, name='Feed')
        grp = make_group(name='G', members=[ch], in_guide=False, recording=False)
        db.session.commit()

        resp = self.client.post(f'/api/channel-groups/{grp.id}/guide-toggle')
        self.assertEqual(409, resp.status_code)
        self.assertEqual([], self._events(grp, GROUP_GUIDE_ADDED))

    def test_the_confirmed_demotion_still_carries_what_it_cancelled(self):
        """The one writer serves every path, and the demotion's extra_data survived being
        routed through it."""
        import json
        from app import db
        from app.database import GROUP_GUIDE_REMOVED
        ch = make_channel(self.acct, name='Feed')
        grp = make_group(name='G', members=[ch], in_guide=True, recording=True)
        rec = make_recording(status='SCHEDULED', group_id=grp.id, channel_id=ch.id)
        db.session.commit()

        self._part(grp, ch, False, confirm=True)
        ev = self._events(grp, GROUP_GUIDE_REMOVED)[0]
        self.assertEqual([rec.id], json.loads(ev.extra_data)['cancelled_recording_ids'])


if __name__ == '__main__':
    unittest.main()
